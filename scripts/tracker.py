#!/usr/bin/env python3
"""Push emitted findings to an external issue tracker (flag-gated; Jira).

usage: tracker.py push    [--repo <dir>] [--fingerprint <fp>] [--dry-run]
       tracker.py refresh [--repo <dir>] [--fingerprint <fp>]
       tracker.py attach  [--repo <dir>] [--fingerprint <fp>]
       tracker.py verify  [--repo <dir>] [--fingerprint <fp>]

push creates tickets for emitted findings (after a JQL pre-check on the
fp-<fingerprint> label, so a wiped state recovers existing tickets instead
of duplicating) and uploads the finding's artifacts (ISSUE.md, proof_spec.rb,
proof_run.log) as attachments; refresh re-PUTs summary+description of
existing tickets after a format improvement; attach backfills/repairs
attachments (idempotent — filenames the ticket already carries are skipped);
verify re-runs the proof specs of ticketed findings against current HEAD and
comments once on tickets whose spec now passes (defect appears fixed).

Configured via the `tracker:` section of config.yaml:

  tracker:
    kind: jira                    # null disables (the default)
    jira_base_url: "https://yourorg.atlassian.net"
    jira_project: AR
    jira_issue_type: Task         # JWM ("core") projects ship with Task
    jira_email: you@yourorg.com   # or env JIRA_EMAIL
    tracks: [provable, advisory]  # which finding tracks become tickets

Credentials: the API token comes ONLY from the environment — JIRA_API_TOKEN
(or HABEAS_JIRA_TOKEN); never from config. Auth is Jira Cloud basic auth
(email + API token, https://id.atlassian.com/manage-profile/security/api-tokens).

Behavior: pushes every finding with status `emitted` that has no recorded
ticket yet (state entry `tracker`), builds a ticket with the full claim,
failure scenario, evidence, mechanical replication steps, and resolution
guidance, POSTs to /rest/api/2/issue, and records {key, url, ts} back into
state via state.py's annotate (so re-runs never duplicate tickets). A failed
push is reported and skipped — emission is never blocked by tracker outages.
On a 400 the payload is retried once without optional fields (labels), since
project screens vary. Advisory findings are pushed only when `advisory` is in
tracker.tracks, and their tickets carry the opinion banner.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state  # noqa: E402
from paths import audit_workspace, proof_artifact, within

TIMEOUT_S = 30


class TrackerError(Exception):
    pass


def tracker_config(repo: str) -> dict:
    audit_workspace(repo)
    cfg = state.load_config(repo).get("tracker") or {}
    if not cfg.get("kind"):
        raise TrackerError("no tracker configured (tracker.kind is null); nothing to push")
    if cfg["kind"] != "jira":
        raise TrackerError(f"unsupported tracker.kind: {cfg['kind']}")
    for key in ("jira_base_url", "jira_project"):
        if not cfg.get(key):
            raise TrackerError(f"tracker.{key} is required for kind: jira")
    cfg.setdefault("jira_issue_type", "Task")
    cfg.setdefault("tracks", ["provable", "advisory"])
    return cfg


def credentials(cfg: dict) -> tuple:
    email = os.environ.get("JIRA_EMAIL") or cfg.get("jira_email")
    token = os.environ.get("JIRA_API_TOKEN") or os.environ.get("HABEAS_JIRA_TOKEN")
    if not email:
        raise TrackerError("no Jira email (set tracker.jira_email or JIRA_EMAIL)")
    if not token:
        raise TrackerError("no Jira API token in the environment "
                           "(set JIRA_API_TOKEN; tokens never live in config)")
    return email, token


def _repo_ref(repo: str) -> str:
    proc = subprocess.run(["git", "-C", repo, "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True)
    sha = proc.stdout.strip() if proc.returncode == 0 else "unknown"
    return f"{Path(repo).resolve().name} @ {sha}"


def description(record: dict, entry: dict, repo: str) -> str:
    advisory = record.get("track") == "advisory"
    lines_ref = record.get("file", "")
    lr = record.get("lines") or []
    if len(lr) == 2:
        lines_ref += f":{lr[0]}-{lr[1]}"
    proof = record.get("proof") or {}
    sketch = record.get("proof_sketch") or {}
    issue_id = entry.get("issue_id") or "?"
    parts = []
    if advisory:
        parts.append("*Advisory finding: opinion, not evidence."
                     " Severity capped at medium.*\n")
    parts.append("Found by habeas (proof-gated codebase audit) in "
                 f"{_repo_ref(repo)}.\n")
    parts.append("h2. Claim\n" + record.get("hypothesis", "(none)"))
    parts.append("h2. Failure scenario\n"
                 + (record.get("failure_scenario") or "(none provided)"))
    parts.append("h2. Evidence\n{{" + lines_ref + "}}\n"
                 "{code}\n" + record.get("evidence_snippet", "").rstrip() + "\n{code}")
    if not advisory:
        spec_text = proof_spec_text(repo, record, entry)
        fingerprint = record.get("fingerprint", "finding")
        proof_cfg = state.load_config(repo).get("proof", {})
        if sketch.get("type") == "jest":
            save_as = f"{proof_cfg['jest_dir']}/habeas_{fingerprint}.test.tsx"
            lang, template = "typescript", proof_cfg.get("test_cmd_jest")
        elif proof_cfg.get("framework") == "pytest":
            save_as = f"{proof_cfg['spec_dir']}/test_habeas_{fingerprint}.py"
            lang, template = "python", proof_cfg.get("test_cmd")
        else:
            save_as = f"{proof_cfg['spec_dir']}/habeas_{fingerprint}_spec.rb"
            lang, template = "ruby", proof_cfg.get("test_cmd")
        proof_cmd = (template or "{file}").replace("{file}", save_as)
        replicate = ["h2. How to replicate"]
        if spec_text:
            replicate.append(
                "The complete proof spec is inlined below — the ticket is"
                " self-contained; nothing outside this ticket is needed.\n"
                f"Save it as {{{{{save_as}}}}} (any path works), then run from"
                " the repo root:\n"
                "{code}\n" + proof_cmd + "\n{code}\n"
                f"Proof spec (save as {save_as}):\n"
                f"{{code:{lang}}}\n" + spec_text.rstrip() + "\n{code}")
        else:
            replicate.append("(proof spec source unavailable at push time;"
                             " see the failure scenario above)")
        replicate.append(
            "Expected: the spec fails on current HEAD, first failure matching "
            "{{" + sketch.get("expected_failure_pattern", "") + "}}:\n"
            "{code}\n" + (proof.get("failure_message") or "").rstrip() + "\n{code}\n"
            "The failure was reproduced twice (deterministic)."
            + (" A fix diff was verified to turn the proof green (fail-then-pass)."
               if proof.get("fail_then_pass") is True else ""))
        parts.append("\n\n".join(replicate))
    parts.append(
        "h2. How to resolve\n"
        + (("Suggested fix direction:\n{code}\n"
            + record["fix_sketch"].rstrip() + "\n{code}\n")
           if record.get("fix_sketch") else
           "No fix sketch was recorded; resolve per the failure scenario above.\n")
        + ("The finding is resolved when the proof spec inlined above PASSES "
           "against the fixed code — its assertions state the correct behavior."
           if not advisory else
           "Advisory: resolve at the team's discretion; there is no mechanical proof."))
    parts.append("h2. Metadata\n"
                 f"* severity: {record.get('severity')}\n"
                 f"* category: {record.get('category')} | track: {record.get('track')}\n"
                 f"* fingerprint: {record.get('fingerprint')}\n"
                 f"* habeas issue id: {issue_id} (reporter's audit workspace)")
    return "\n\n".join(parts)


def proof_spec_text(repo: str, record: dict, entry: dict) -> str | None:
    """The proof spec source: from the emitted issue folder, else the
    recorded proof.spec_path. Inlined into tickets so they are self-contained
    (issues/ may not be committed; ticket readers may not have the repo)."""
    for name, path in artifact_files(repo, entry):
        if name.startswith("proof_spec."):
            return path.read_text(encoding="utf-8", errors="replace")
    spec_path = (record.get("proof") or {}).get("spec_path")
    if spec_path:
        spec = proof_artifact(repo, spec_path)
        if spec.is_file():
            return spec.read_text(encoding="utf-8", errors="replace")
    return None


def summary_line(record: dict, entry: dict, limit: int = 72) -> str:
    """Short ticket title: the expert-written `title` when present (the
    pipeline requires one on proceed since v2), else a hard-capped first
    sentence of the hypothesis. The full claim lives in the body."""
    text = (record.get("title") or "").strip()
    if not text:
        text = record.get("hypothesis", "finding").strip().split(". ")[0].rstrip(".")
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return f"[habeas {entry.get('issue_id') or '?'}] {text}"


def build_payload(record: dict, entry: dict, cfg: dict, repo: str) -> dict:
    return {"fields": {
        "project": {"key": cfg["jira_project"]},
        "summary": summary_line(record, entry),
        "description": description(record, entry, repo),
        "issuetype": {"name": cfg["jira_issue_type"]},
        "labels": ["habeas", f"category-{record.get('category')}",
                   f"severity-{record.get('severity')}",
                   f"track-{record.get('track')}",
                   f"fp-{record.get('fingerprint')}"],
    }}


def find_existing_ticket(cfg: dict, fingerprint: str, email: str, token: str) -> str | None:
    """JQL pre-check on the fingerprint label: the last uniqueness line of
    defense when .audit/state.json was lost. Returns the ticket key or None;
    a search failure returns None (push proceeds — best effort)."""
    import urllib.parse
    jql = f'project = "{cfg["jira_project"]}" AND labels = "fp-{fingerprint}"'
    url = (cfg["jira_base_url"].rstrip("/") + "/rest/api/2/search?"
           + urllib.parse.urlencode({"jql": jql, "fields": "key", "maxResults": 1}))
    req = urllib.request.Request(url, headers={"Authorization": _auth(email, token)})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            data = json.loads(resp.read())
        issues = data.get("issues", [])
        return issues[0]["key"] if issues else None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
        return None


def _auth(email: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


def _request(method: str, url: str, body: dict, email: str, token: str) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": _auth(email, token)},
        method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        raw = resp.read()
        return json.loads(raw) if raw.strip() else {}


# ------------------------------------------------------------- attachments

def artifact_files(repo: str, entry: dict) -> list:
    """[(filename, path)] — the finding's issue-folder artifacts: ISSUE.md,
    proof_spec.rb, proof_run.log (whichever exist)."""
    issue_id = entry.get("issue_id") or ""
    if not str(issue_id).isdigit():
        return []
    issues = within(repo, "issues")
    folder = None
    for d in sorted(issues.glob(f"{issue_id}-*")):
        folder = d
    if folder is None:
        return []
    return [(name, within(issues, folder / name))
            for name in ("ISSUE.md", "proof_spec.rb", "proof_spec.py", "proof_spec.test.tsx", "proof_run.log")
            if within(issues, folder / name).is_file()]


def existing_attachment_names(cfg: dict, key: str, email: str, token: str) -> set:
    url = cfg["jira_base_url"].rstrip("/") + f"/rest/api/2/issue/{key}?fields=attachment"
    req = urllib.request.Request(url, headers={"Authorization": _auth(email, token)})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        data = json.loads(resp.read())
    return {a.get("filename") for a in data.get("fields", {}).get("attachment", [])}


def attach_artifacts(cfg: dict, key: str, files: list, email: str, token: str) -> list:
    """Upload the given [(filename, path)] to the ticket, skipping filenames
    it already carries (idempotent). Returns uploaded filenames."""
    try:
        present = existing_attachment_names(cfg, key, email, token)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TrackerError(f"could not list attachments of {key}: {e}")
    todo = [(n, p) for n, p in files if n not in present]
    if not todo:
        return []
    boundary = "habeas" + os.urandom(12).hex()
    body = b""
    for name, path in todo:
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f"name=\"file\"; filename=\"{name}\"\r\n"
                 "Content-Type: application/octet-stream\r\n\r\n").encode()
        body += Path(path).read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    url = cfg["jira_base_url"].rstrip("/") + f"/rest/api/2/issue/{key}/attachments"
    req = urllib.request.Request(
        url, data=body,
        headers={"Authorization": _auth(email, token),
                 "X-Atlassian-Token": "no-check",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S * 2) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        raise TrackerError(f"attachment upload to {key} failed: HTTP {e.code}: "
                           f"{e.read().decode(errors='replace')[:200]}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TrackerError(f"attachment upload to {key} failed: {e}")
    return [n for n, _ in todo]


def post_issue(cfg: dict, payload: dict, email: str, token: str) -> dict:
    url = cfg["jira_base_url"].rstrip("/") + "/rest/api/2/issue"
    minimal = {"fields": {k: v for k, v in payload["fields"].items() if k != "labels"}}
    last_error = None
    for body in (payload, minimal):
        try:
            return _request("POST", url, body, email, token)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            last_error = f"HTTP {e.code}: {detail}"
            if e.code != 400:
                break  # only field problems are worth the minimal retry
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_error = str(e)
            break
    raise TrackerError(last_error or "unknown error")


def refresh(repo: str, fingerprint: str | None) -> dict:
    """Re-build and PUT summary+description for findings that already have a
    ticket — used after a description-format improvement."""
    cfg = tracker_config(repo)
    st = state.load_state(repo)
    email, token = credentials(cfg)
    updated, failed = [], []
    for fp, entry in st["findings"].items():
        key = (entry.get("tracker") or {}).get("key")
        if not key or (fingerprint and fp != fingerprint):
            continue
        payload = build_payload(entry["record"], entry, cfg, repo)
        body = {"fields": {"summary": payload["fields"]["summary"],
                           "description": payload["fields"]["description"]}}
        url = cfg["jira_base_url"].rstrip("/") + f"/rest/api/2/issue/{key}"
        try:
            _request("PUT", url, body, email, token)
            updated.append({"fingerprint": fp, "key": key})
        except urllib.error.HTTPError as e:
            failed.append({"fingerprint": fp, "key": key,
                           "error": f"HTTP {e.code}: {e.read().decode(errors='replace')[:200]}"})
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            failed.append({"fingerprint": fp, "key": key, "error": str(e)})
    return {"updated": updated, "failed": failed}


def push(repo: str, fingerprint: str | None, dry_run: bool) -> dict:
    cfg = tracker_config(repo)
    st = state.load_state(repo)
    targets = []
    for fp, entry in st["findings"].items():
        if entry["status"] != "emitted" or entry.get("tracker"):
            continue
        if fingerprint and fp != fingerprint:
            continue
        if entry["record"].get("track") not in cfg["tracks"]:
            continue
        targets.append((fp, entry))
    if not targets:
        return {"pushed": [], "failed": [], "note": "nothing to push"}

    email = token = None
    if not dry_run:
        email, token = credentials(cfg)

    pushed, failed = [], []
    for fp, entry in targets:
        payload = build_payload(entry["record"], entry, cfg, repo)
        if dry_run:
            pushed.append({"fingerprint": fp, "issue_id": entry.get("issue_id"),
                           "dry_run": True, "payload": payload})
            continue
        try:
            existing = find_existing_ticket(cfg, fp, email, token)
            if existing:
                url = cfg["jira_base_url"].rstrip("/") + f"/browse/{existing}"
                state.annotate(repo, fp, {
                    "kind": "jira", "key": existing, "url": url, "attachments": [],
                    "recovered": True,
                    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
                pushed.append({"fingerprint": fp, "issue_id": entry.get("issue_id"),
                               "key": existing, "url": url, "recovered": True})
                continue
            resp = post_issue(cfg, payload, email, token)
            key = resp.get("key", "")
            url = cfg["jira_base_url"].rstrip("/") + f"/browse/{key}"
            uploaded, attach_error = [], None
            try:
                uploaded = attach_artifacts(cfg, key, artifact_files(repo, entry),
                                            email, token)
            except TrackerError as e:
                attach_error = str(e)  # ticket exists; never block the annotate
            state.annotate(repo, fp, {
                "kind": "jira", "key": key, "url": url, "attachments": uploaded,
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
            result = {"fingerprint": fp, "issue_id": entry.get("issue_id"),
                      "key": key, "url": url, "attachments": uploaded}
            if attach_error:
                result["attach_error"] = attach_error
            pushed.append(result)
        except TrackerError as e:
            failed.append({"fingerprint": fp, "issue_id": entry.get("issue_id"),
                           "error": str(e)})
    return {"pushed": pushed, "failed": failed}


VERIFY_MARKER = "habeas verify:"


def _has_verify_comment(cfg: dict, key: str, email: str, token: str) -> bool:
    url = cfg["jira_base_url"].rstrip("/") + f"/rest/api/2/issue/{key}/comment"
    req = urllib.request.Request(url, headers={"Authorization": _auth(email, token)})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        data = json.loads(resp.read())
    return any(VERIFY_MARKER in (c.get("body") or "") for c in data.get("comments", []))


def verify(repo: str, fingerprint: str | None) -> dict:
    """Re-run the proof specs of ticketed findings against current HEAD. A
    spec that now PASSES means the defect appears fixed: comment on the
    ticket (once — a prior 'habeas verify:' comment suppresses repeats).
    Turns every emitted proof spec into a standing regression sentinel."""
    import subprocess as sp
    import tempfile
    cfg = tracker_config(repo)
    st = state.load_state(repo)
    email, token = credentials(cfg)
    scripts = Path(__file__).resolve().parent
    results = []
    wt = Path(tempfile.mkdtemp(prefix="habeas-verify-")) / "wt"
    sp.run(["git", "-C", repo, "worktree", "add", "-q", str(wt), "HEAD"],
           check=True, capture_output=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for fp, entry in st["findings"].items():
                key = (entry.get("tracker") or {}).get("key")
                if not key or (fingerprint and fp != fingerprint):
                    continue
                if entry["status"] != "emitted" or entry["record"].get("track") != "provable":
                    continue
                spec = next((p for n, p in artifact_files(repo, entry)
                             if n.startswith("proof_spec.")), None)
                if spec is None:
                    results.append({"fingerprint": fp, "key": key,
                                    "status": "inconclusive", "reason": "no proof spec"})
                    continue
                record_json = Path(tmp) / f"{fp}.json"
                record_json.write_text(json.dumps(entry["record"]))
                proc = sp.run([str(scripts / "prove.sh"), str(record_json), str(spec),
                               "--worktree", str(wt), "--repo", repo,
                               "--log-dir", str(Path(tmp) / fp)],
                              capture_output=True, text=True)
                try:
                    pr = json.loads(proc.stdout)
                except ValueError:
                    results.append({"fingerprint": fp, "key": key, "status": "inconclusive",
                                    "reason": proc.stderr.strip()[:200]})
                    continue
                if pr.get("result") == "red":
                    results.append({"fingerprint": fp, "key": key, "status": "still_red"})
                elif pr.get("reason") == "zero_failures":
                    commented = False
                    try:
                        if not _has_verify_comment(cfg, key, email, token):
                            url = (cfg["jira_base_url"].rstrip("/")
                                   + f"/rest/api/2/issue/{key}/comment")
                            _request("POST", url, {"body": (
                                f"{VERIFY_MARKER} the proof spec for this finding PASSES "
                                f"at {_repo_ref(repo)} — the defect appears fixed. "
                                "Re-run the spec inlined in the description to confirm, "
                                "then resolve.")}, email, token)
                            commented = True
                    except (urllib.error.URLError, TimeoutError, OSError) as e:
                        results.append({"fingerprint": fp, "key": key, "status": "fixed",
                                        "commented": False, "comment_error": str(e)})
                        continue
                    results.append({"fingerprint": fp, "key": key, "status": "fixed",
                                    "commented": commented})
                else:
                    results.append({"fingerprint": fp, "key": key, "status": "inconclusive",
                                    "reason": pr.get("reason")})
    finally:
        sp.run(["git", "-C", repo, "worktree", "remove", "--force", str(wt)],
               capture_output=True)
    return {"verified": results}


def attach(repo: str, fingerprint: str | None) -> dict:
    """Backfill/repair ticket attachments for findings that already have a
    ticket. Idempotent: filenames the ticket already carries are skipped."""
    cfg = tracker_config(repo)
    st = state.load_state(repo)
    email, token = credentials(cfg)
    attached, failed = [], []
    for fp, entry in st["findings"].items():
        key = (entry.get("tracker") or {}).get("key")
        if not key or (fingerprint and fp != fingerprint):
            continue
        files = artifact_files(repo, entry)
        if not files:
            failed.append({"fingerprint": fp, "key": key,
                           "error": "no local artifacts (issues/ folder missing)"})
            continue
        try:
            uploaded = attach_artifacts(cfg, key, files, email, token)
            attached.append({"fingerprint": fp, "key": key, "uploaded": uploaded})
        except TrackerError as e:
            failed.append({"fingerprint": fp, "key": key, "error": str(e)})
    return {"attached": attached, "failed": failed}


def main(argv: list) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="subcmd", required=True)
    p = sub.add_parser("push")
    p.add_argument("--repo", default=".")
    p.add_argument("--fingerprint", default=None)
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("refresh", help="re-PUT summary+description of existing tickets")
    p.add_argument("--repo", default=".")
    p.add_argument("--fingerprint", default=None)
    p = sub.add_parser("attach", help="backfill issue-folder artifacts onto existing tickets")
    p.add_argument("--repo", default=".")
    p.add_argument("--fingerprint", default=None)
    p = sub.add_parser("verify", help="re-run proof specs of ticketed findings; comment when fixed")
    p.add_argument("--repo", default=".")
    p.add_argument("--fingerprint", default=None)
    args = ap.parse_args(argv[1:])
    try:
        if args.subcmd == "push":
            result = push(args.repo, args.fingerprint, args.dry_run)
        elif args.subcmd == "refresh":
            result = refresh(args.repo, args.fingerprint)
        elif args.subcmd == "verify":
            result = verify(args.repo, args.fingerprint)
        else:
            result = attach(args.repo, args.fingerprint)
    except (TrackerError, state.StateError, OSError, ValueError) as e:
        print(f"tracker.py: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0 if not result.get("failed") else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
