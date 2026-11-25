#!/usr/bin/env python3
"""Emit one finding: issue folder, advisory ISSUE.md, or quarantine entry
(plan 6.5 / 7.7).

usage: emit.py <fingerprint> [--repo <dir>]

Dispatch by finding status:
  proved          -> issues/<NNNN>-<slug>/{ISSUE.md, proof_spec.rb, proof_run.log}
  critic_advisory -> issues/<NNNN>-<slug>/ISSUE.md (advisory banner, no proof)
  proof_failed    -> append to quarantine.md, status -> quarantined
  anything else   -> refused.

Defense in depth (deliberately redundant with the pipeline): a provable-track
finding is refused unless its record carries proof.result == "red" AND
proof_review.verdict == "valid", plus a real spec file at proof.spec_path and
run logs at proof.log_path. The advisory severity cap and
gates.max_emissions_per_run are enforced here as well (quarantine entries are
not emissions and bypass the cap). Appends `.audit/` to the target's
.gitignore when missing.

Decisions noted (plan 14):
- advisory findings get the same issues/<NNNN>-<slug>/ folder, holding only
  ISSUE.md — uniform id allocation and layout.
- proof_run.log is the concatenation of run1/run2 (+ runfix when present)
  from proof.log_path.
- the emitted transition's history note is the current run id; the per-run
  emission count is derived from those notes.
- quarantine transitions use actor "emit" (emit.py performs the write).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state  # noqa: E402
from paths import audit_workspace, output_path, proof_artifact, within


class EmitError(Exception):
    pass


def slugify(text: str, limit: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(slug) > limit:
        slug = slug[:limit].rsplit("-", 1)[0]
    return slug or "finding"


def next_issue_id(issues_dir: Path) -> str:
    max_id = 0
    if issues_dir.exists():
        for child in issues_dir.iterdir():
            m = re.match(r"^(\d+)-", child.name)
            if m:
                max_id = max(max_id, int(m.group(1)))
    return f"{max_id + 1:04d}"


def ensure_gitignore(repo: Path) -> None:
    gi = within(repo, ".gitignore")
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    if not any(line.strip().rstrip("/") == ".audit" for line in lines):
        with gi.open("a", encoding="utf-8") as f:
            if lines and lines[-1].strip():
                f.write("\n")
            f.write(".audit/\n")


def current_run_id(st: dict) -> str:
    for run in reversed(st["runs"]):
        if "ended" not in run and not run.get("abandoned"):
            return run["id"]
    return ""


def emitted_in_run(st: dict, rid: str) -> int:
    return sum(
        1 for e in st["findings"].values()
        if e["status"] == "emitted"
        and any(h["to"] == "emitted" and h.get("note") == rid for h in e["history"]))


def clamp_severity(record: dict, cap: str) -> None:
    order = state.SEVERITY_ORDER
    sev = record.get("severity")
    if sev in order and cap in order and order.index(sev) > order.index(cap):
        record["severity"] = cap


def lines_ref(record: dict) -> str:
    lines = record.get("lines") or []
    if len(lines) == 2:
        return f"{record['file']}:{lines[0]}-{lines[1]}"
    return record["file"]


def frontmatter(pairs: list) -> str:
    out = ["---"]
    for key, value in pairs:
        out.append(f"{key}: {json.dumps(value)}")
    out.append("---")
    return "\n".join(out)


def short_claim(record: dict, limit: int = 120) -> str:
    """The expert's title when present; else first sentence of the
    hypothesis, hard-capped. Used for the H1, slug, and scope line."""
    title = (record.get("title") or "").strip()
    if title:
        return title
    text = record.get("hypothesis", "finding").strip()
    m = re.match(r"(.+?[.!?])(?:\s|$)", text)
    if m:
        text = m.group(1).rstrip(".!?")
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def issue_body(record: dict, proof_section: str | None, advisory: bool) -> str:
    parts = []
    if advisory:
        parts.append("> Advisory finding: opinion, not evidence. Severity capped at medium.\n")
    parts.append(f"# {short_claim(record)}\n")
    parts.append("## Claim\n\n" + record.get("hypothesis", "(none)") + "\n")
    parts.append("## Failure scenario\n\n"
                 + (record.get("failure_scenario") or "(none provided)") + "\n")
    snippet = record.get("evidence_snippet", "").rstrip()
    parts.append("## Evidence\n\n- `" + lines_ref(record) + "`\n\n```\n" + snippet + "\n```\n")
    if proof_section is not None:
        parts.append(proof_section)
    parts.append("## Suggested fix direction\n\n"
                 + (record.get("fix_sketch") or "(none provided)") + "\n")
    parts.append("## Scope\n\nSingle concern: " + short_claim(record)
                 + ". If a fix would span more than this, the finding should have been"
                   " split upstream.\n")
    return "\n".join(parts)


def build_proof_artifacts(repo: Path, record: dict, issue_dir: Path) -> tuple:
    proof = record.get("proof") or {}
    review = record.get("proof_review") or {}
    if proof.get("result") != "red":
        raise EmitError(f"refusing to emit: proof.result is "
                        f"{proof.get('result')!r}, not \"red\"")
    if review.get("verdict") != "valid":
        raise EmitError(f"refusing to emit: proof_review.verdict is "
                        f"{review.get('verdict')!r}, not \"valid\"")
    spec_path = proof.get("spec_path")
    runs = within(repo, ".audit/runs")
    if not spec_path or not proof_artifact(repo, spec_path).is_file():
        raise EmitError(f"refusing to emit: proof.spec_path missing or absent: {spec_path}")
    log_dir = proof_artifact(repo, proof.get("log_path") or "")
    if not all(within(runs, log_dir / n).is_file() for n in ("run1.log", "run2.log")):
        raise EmitError(f"refusing to emit: no run logs for both red runs under {log_dir}")
    if record.get("category") == "security" and (
            proof.get("fail_then_pass") is not True or not within(runs, log_dir / "runfix.log").is_file()):
        raise EmitError("refusing to emit: security proof requires a verified green fix run")
    log_parts = []
    for name, label in [("run1.log", "run 1 (red on HEAD)"),
                        ("run2.log", "run 2 (determinism check)"),
                        ("runfix.log", "run with fix applied")]:
        p = within(runs, log_dir / name)
        if p.exists():
            log_parts.append(f"=== {label} ===\n"
                             + p.read_text(encoding="utf-8", errors="replace"))

    cfg = state.load_config(str(repo))
    is_jest = (record.get("proof_sketch") or {}).get("type") == "jest"
    if is_jest:
        spec_name = "proof_spec.test.tsx"
    elif cfg.get("proof", {}).get("framework") == "pytest":
        spec_name = "proof_spec.py"
    else:
        spec_name = "proof_spec.rb"
    test_cmd = cfg.get("proof", {}).get("test_cmd_jest" if is_jest else "test_cmd",
                                        "bundle exec rspec {file}")
    rel_spec = f"issues/{issue_dir.name}/{spec_name}"
    proof_command = test_cmd.replace("{file}", rel_spec)
    pattern = (record.get("proof_sketch") or {}).get("expected_failure_pattern", "")
    failure = (proof.get("failure_message") or "").rstrip()
    section = ("## Proof\n\n"
               f"Run: `{proof_command}`\n\n"
               f"Expected: red on HEAD, first failure matching `{pattern}`:\n\n"
               "```\n" + failure + "\n```\n\n"
               f"Fail-then-pass: {json.dumps(proof.get('fail_then_pass'))}\n")
    return proof_artifact(repo, spec_path).read_text(encoding="utf-8"), "\n".join(log_parts), \
        section, proof_command, spec_name


def emit_issue(repo: Path, fp: str, entry: dict, st: dict, cfg: dict) -> dict:
    record = entry["record"]
    status = entry["status"]
    advisory = status == "critic_advisory"
    if advisory:
        if record.get("track") != "advisory":
            raise EmitError("critic_advisory finding whose record.track is not advisory")
        clamp_severity(record, cfg.get("gates", {}).get("advisory_severity_cap", "medium"))
    else:
        if record.get("track") != "provable":
            raise EmitError("proved finding whose record.track is not provable")

    rid = current_run_id(st)
    cap = int(cfg.get("gates", {}).get("max_emissions_per_run", 5))
    if emitted_in_run(st, rid) >= cap:
        raise EmitError(f"emission cap reached for run {rid or '(none)'} "
                        f"({cap}); finding stays in state for the next run")

    issues_dir = within(repo, "issues")
    issue_id = next_issue_id(issues_dir)
    slug = slugify(record.get("title") or record.get("hypothesis", fp))
    issue_dir = issues_dir / f"{issue_id}-{slug}"

    spec_text = run_log = proof_section = proof_command = None
    if not advisory:
        spec_text, run_log, proof_section, proof_command, spec_name = \
            build_proof_artifacts(repo, record, issue_dir)

    fm = frontmatter([
        ("id", issue_id), ("slug", slug), ("title", record.get("title")),
        ("fingerprint", fp),
        ("track", record.get("track")), ("category", record.get("category")),
        ("severity", record.get("severity")), ("status", "open"),
        ("discovered", datetime.now(timezone.utc).strftime("%Y-%m-%d")),
        ("run", rid), ("files", [record.get("file")]),
        ("proof_command", proof_command),
        ("fail_then_pass", (record.get("proof") or {}).get("fail_then_pass")),
    ])
    issue_dir.mkdir(parents=True)
    (issue_dir / "ISSUE.md").write_text(
        fm + "\n\n" + issue_body(record, proof_section, advisory), encoding="utf-8")
    if not advisory:
        (issue_dir / spec_name).write_text(spec_text, encoding="utf-8")
        (issue_dir / "proof_run.log").write_text(run_log, encoding="utf-8")

    ensure_gitignore(repo)
    state.transition(str(repo), fp, "emitted", "emit", note=rid, issue_id=issue_id)
    return {"action": "emitted", "fingerprint": fp, "issue_id": issue_id,
            "path": str(issue_dir)}


def emit_quarantine(repo: Path, fp: str, entry: dict, st: dict) -> dict:
    record = entry["record"]
    rid = current_run_id(st)
    proof = record.get("proof") or {}
    critic = record.get("critic") or {}
    slug = slugify(record.get("hypothesis", fp))
    qpath = within(repo, "quarantine.md")
    if not qpath.exists():
        qpath.write_text(
            "# Quarantine\n\nFindings that survived the adversarial critic but"
            " failed the mechanical proof gate. Append-only; entries are"
            " candidates for manual review and for growing"
            " references/negative-examples.md.\n", encoding="utf-8")
    snippet = record.get("evidence_snippet", "").rstrip()
    entry_md = "\n".join([
        "",
        f"## {fp} — {slug}",
        "",
        f"- Quarantined: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        f" (run {rid or 'n/a'})",
        f"- Category: {record.get('category')} | severity: {record.get('severity')}"
        f" | `{lines_ref(record)}`",
        f"- Claim: {record.get('failure_scenario') or record.get('hypothesis', '')}",
        f"- Why proof failed: {proof.get('reason') or proof.get('result') or 'unknown'}"
        f" (runs: {json.dumps(proof.get('runs', []))})",
        f"- Full logs: `{proof.get('log_path', '(none)')}`",
        f"- Critic confidence: {critic.get('confidence', 'n/a')}",
        "",
        "```",
        snippet,
        "```",
        ""])
    with qpath.open("a", encoding="utf-8") as f:
        f.write(entry_md)
    ensure_gitignore(repo)
    state.transition(str(repo), fp, "quarantined", "emit",
                     note=f"{rid}: {proof.get('reason') or 'proof failed'}")
    return {"action": "quarantined", "fingerprint": fp, "path": str(qpath)}


def emit(repo_s: str, fp: str) -> dict:
    repo = Path(repo_s)
    audit_workspace(repo)
    # Validate before creating any artifacts or appending to existing files.
    for name in (".gitignore", "issues", "quarantine.md"):
        output_path(repo, name)
    st = state.load_state(repo_s)
    entry = st["findings"].get(fp)
    if entry is None:
        raise EmitError(f"unknown fingerprint: {fp}")
    status = entry["status"]
    cfg = state.load_config(repo_s)
    if status == "proof_failed":
        return emit_quarantine(repo, fp, entry, st)
    if status in ("proved", "critic_advisory"):
        return emit_issue(repo, fp, entry, st, cfg)
    raise EmitError(f"finding {fp} is not emittable from status {status!r} "
                    "(needs proved, critic_advisory, or proof_failed)")


def main(argv: list) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("fingerprint")
    ap.add_argument("--repo", default=".")
    args = ap.parse_args(argv[1:])
    try:
        result = emit(args.repo, args.fingerprint)
    except (EmitError, state.StateError, OSError, ValueError) as e:
        print(f"emit.py: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
