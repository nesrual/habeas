"""S7 outcomes: issue files for proven clusters, quarantine entries, the
drift check, and closing the run (plan 9 S7, 12.4)."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
from pathlib import Path

from . import calls, ledger, prompts, reconcile, schema, verify
from .calls import CallSpec
from .merge import card

RANK = {s: i for i, s in enumerate(schema.SEVERITIES)}      # critical first


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50] or "issue"


def _front(fields: dict) -> str:
    out = ["---"]
    for k, v in fields.items():
        out.append(f"{k}: {json.dumps(v) if isinstance(v, (list, dict, bool)) or v is None else json.dumps(str(v))}")
    return "\n".join(out + ["---", ""])


def _raising_seats(conn, cluster_id: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT c.seat_id FROM cluster_members m JOIN candidates d ON d.candidate_id ="
        " m.candidate_id JOIN calls c ON c.call_id = d.call_id WHERE m.cluster_id = ? ORDER BY 1",
        (cluster_id,))]


def _units(conn, cluster_id: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT d.unit_id FROM cluster_members m JOIN candidates d ON d.candidate_id ="
        " m.candidate_id WHERE m.cluster_id = ? ORDER BY 1", (cluster_id,))]


def _read(path) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace") if path and Path(path).exists() else ""


def write_issue(conn, cfg, run_dir: Path, k, proof, written: dict | None) -> str:
    run = ledger.run(conn)
    checker_sev = proof["checker_severity"]
    writer_sev = (written or {}).get("severity")
    mismatch = bool(writer_sev and checker_sev and writer_sev != checker_sev)
    severity = min([s for s in (writer_sev, checker_sev) if s], key=RANK.get, default="medium")
    note = (f"writer proposed {writer_sev}, proof checker rated {checker_sev}; the higher level is "
            "shown until the owner decides") if mismatch else None
    review = conn.execute("SELECT * FROM reviews WHERE cluster_id = ?", (k["cluster_id"],)).fetchone()
    if review:
        # a person checked this by hand: their severity stands, the models' levels stay on record
        note = (f"set by {review['reviewer']} after a manual check; the proof checker rated it "
                f"{checker_sev or 'nothing'}" + (f", the writer {writer_sev}" if writer_sev else ""))
        severity, mismatch = review["severity"], False
    title = " ".join(((written or {}).get("title") or k["title"]).split())
    if len(title) > 120:
        title = title[:117].rstrip() + "..."
    dest = proof["test_destination"] or verify.test_destination(
        k["cluster_id"], json.loads(k["locations_json"])[0]["path"])
    rel = f"issues/{k['cluster_id']}-{_slug(title)}.md"
    front = _front({
        "id": k["cluster_id"], "title": title, "lens": k["lens"], "class": k["cclass"],
        "severity": severity, "severity_mismatch": mismatch, "units": _units(conn, k["cluster_id"]),
        "raising_seats": _raising_seats(conn, k["cluster_id"]),
        "proof_test": proof["test_path"], "proof_fix": proof["fix_patch_path"],
        "proof_logs": proof["dir"], "prover": proof["prover_seat"],
        "prover_model": proof["prover_model"] or "", "test_destination": dest,
        "proof_checker": proof["checker_seat"],
        "audited_commit": run["repo_commit"], "written_by": "writer" if written else "fallback"})
    locations = ", ".join(f"`{l['path']}:{l['start_line']}`" for l in json.loads(k["locations_json"]))
    body = [
        f"# {title}", "", f"Severity: **{severity}**" + (f" ({note})" if note else ""), "",
        "## Invariant", "", k["invariant"], "",
        "## How the code breaks it", "", (written or {}).get("how_it_breaks") or k["violation"], "",
        f"Where: {locations}", "",
        *(["## Manual review", "", review["note"].strip(), ""] if review else []),
        "## How to reproduce", "",
        f"Save the test below as `{dest}` at commit `{run['repo_commit'][:12]}` and run:", "",
        "```", shlex.join(verify.test_argv(cfg, [dest])), "```", "",
        "It fails on the current code and passes with the fix applied.", "",
        "```ruby", _read(proof["test_path"]).rstrip(), "```", "",
        "## Minimal fix", "",
        "This patch is evidence that the defect is real. It is not a merge candidate: the real fix "
        "goes through normal review.", "",
        "```diff", _read(proof["fix_patch_path"]).rstrip(), "```", ""]
    (run_dir / rel).write_text(front + "\n".join(body), encoding="utf-8")
    with conn:
        conn.execute("UPDATE clusters SET severity = ?, severity_note = ? WHERE cluster_id = ?",
                     (severity, note, k["cluster_id"]))
        conn.execute("INSERT INTO outcomes (cluster_id, outcome, detail, issue_path) VALUES (?,?,?,?)",
                     (k["cluster_id"], "proven", f"proof by {proof['prover_seat']}", rel))
    return rel


def write_quarantine(conn, run_dir: Path, k, detail: str) -> str:
    cid = k["cluster_id"]
    rel = f"quarantine/{cid}.md"
    proofs = conn.execute("SELECT * FROM proofs WHERE cluster_id = ? ORDER BY created_at", (cid,)).fetchall()
    lines = [_front({"id": cid, "title": k["title"], "lens": k["lens"], "class": k["cclass"],
                     "triage_state": "open", "raising_seats": _raising_seats(conn, cid)}),
             f"# {k['title']}", "",
             "Not an issue: this candidate survived the critics but has no accepted proof. "
             "Set `triage_state` above to `confirmed`, `dismissed` or `needs_proof`.", "",
             f"Why it is here: {detail}", "", "## Claim", "", "```json",
             card({**dict(k), "class": k["cclass"]}), "```", "", "## Critics", ""]
    for v in conn.execute("SELECT * FROM verdicts WHERE cluster_id = ? ORDER BY critic_seat", (cid,)):
        lines.append(f"- {v['critic_seat']}: **{v['verdict']}**: {v['reason']}")
    lines += ["", "## Proof attempts", ""]
    for p in proofs:
        lines.append(f"- {p['prover_seat']} attempt {p['attempt']}: **{p['status']}**"
                     + (f", mechanical check {p['mechanical']}" if p["mechanical"] != "not_run" else "")
                     + (f": {p['reason']}" if p["reason"] else ""))
        if p["mechanical"] == "pass":
            lines.append(f"  - passed red-green but was not accepted. Checker {p['checker_seat'] or 'none'}: "
                         f"{p['checker_reason'] or 'no answer'}")
            lines.append(f"  - test: `{p['test_path']}`, fix: `{p['fix_patch_path']}`, logs: `{p['dir']}`")
    if not proofs:
        lines.append("- none")
    (run_dir / rel).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rel


def _settle_unproven(conn, cfg, run_dir: Path, k, closed: bool) -> str:
    cid = k["cluster_id"]
    proofs = conn.execute("SELECT * FROM proofs WHERE cluster_id = ?", (cid,)).fetchall()
    statuses = [p["status"] for p in proofs]
    if any(p["mechanical"] == "pass" for p in proofs):
        outcome, detail = "quarantined", "a proof passed the mechanical check but was not accepted by a proof checker"
    else:
        if closed:
            detail = "the run was closed before every prover finished"
        elif statuses and all(s == "refused" for s in statuses):
            detail = "all_provers_refused"
        else:
            detail = "no valid proof: " + ", ".join(sorted(set(statuses)) or ["no attempts"])
        outcome = "quarantined" if k["lens"] in (cfg.get("quarantine.lenses") or []) else "unproven_dropped"
    path = write_quarantine(conn, run_dir, k, detail) if outcome == "quarantined" else None
    with conn:
        conn.execute("INSERT INTO outcomes (cluster_id, outcome, detail, issue_path, triage_state)"
                     " VALUES (?,?,?,?,?)", (cid, outcome, detail, path, "open" if path else None))
    return outcome


def drift_check(conn, cfg, ref: str) -> dict:
    """Repeat the red step of every proof on a newer commit. A test that no
    longer fails marks the issue `fixed_since` (plan 12.4)."""
    run = ledger.run(conn)
    repo = Path(run["repo_path"])
    sha = verify.git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False).stdout.strip()
    if not sha:
        return {"ran": False, "note": f"drift check skipped: cannot resolve {ref}"}
    if sha == run["repo_commit"]:
        return {"ran": False, "note": "drift check: the branch has not moved since the audited commit"}
    verify.preconditions(conn, cfg, repo)
    rows = conn.execute(
        "SELECT o.cluster_id, p.test_path, p.test_destination, c.locations_json FROM outcomes o"
        " JOIN proofs p ON p.cluster_id = o.cluster_id AND p.status = 'valid'"
        " JOIN clusters k ON k.cluster_id = o.cluster_id"
        " JOIN candidates c ON c.candidate_id = k.representative_candidate_id"
        " WHERE o.outcome = 'proven' AND o.fixed_since IS NULL").fetchall()
    out = {"ran": True, "ref": ref, "commit": sha, "checked": 0, "fixed": 0, "unclear": 0}
    verify.git(repo, "checkout", "-q", "--detach", sha)
    try:
        for r in rows:
            dest = r["test_destination"] or verify.test_destination(
                r["cluster_id"], json.loads(r["locations_json"])[0]["path"])
            target = repo / dest
            made = not target.parent.exists()
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(r["test_path"], target)
            try:
                log = Path(r["test_path"]).parent / "drift.log"
                code, text = verify.run_tests(repo, verify.test_argv(cfg, [dest]), 60 * float(
                    cfg.get("provers.timeout_minutes") or 30), log, verify.test_env(cfg))
                kind = verify.classify(code, text)
            finally:
                target.unlink(missing_ok=True)
                if made:
                    try:
                        target.parent.rmdir()
                    except OSError:
                        pass
            out["checked"] += 1
            if kind == "green":
                out["fixed"] += 1
                with conn:
                    conn.execute("UPDATE outcomes SET fixed_since = ? WHERE cluster_id = ?", (sha, r["cluster_id"]))
            elif kind != "red":
                out["unclear"] += 1
    finally:
        verify.git(repo, "checkout", "-q", "--", ".", check=False)
        verify.git(repo, "checkout", "-q", "--detach", run["repo_commit"], check=False)
    return out


def keep_config(cfg, run_dir) -> None:
    """Copy the configuration files into the run, for later reports on the finished run."""
    if not getattr(cfg, "dir", None) or not Path(cfg.dir).is_dir():
        return
    dest = Path(run_dir) / "config"
    dest.mkdir(exist_ok=True)
    for f in Path(cfg.dir).glob("*.yaml"):
        shutil.copyfile(f, dest / f.name)


def run(conn, cfg, run_dir, args: dict) -> dict:
    run_dir = Path(run_dir)
    system_t, user_t, version = prompts.load("writer.md")
    seat = (cfg.seats("writer") or [None])[0]
    proven = conn.execute(
        "SELECT cluster_id FROM clusters WHERE state = 'proven' AND cluster_id NOT IN"
        " (SELECT cluster_id FROM outcomes) ORDER BY cluster_id").fetchall()
    plan = []
    for row in proven:
        k = verify.cluster_row(conn, row["cluster_id"])
        proof = conn.execute("SELECT * FROM proofs WHERE cluster_id = ? AND status = 'valid'"
                             " ORDER BY created_at", (k["cluster_id"],)).fetchone()
        spec = None
        if seat:
            def messages(k=k, proof=proof):
                return (prompts.fill(system_t, {"schema": json.dumps(schema.WRITER, indent=1),
                                                "rubric": json.dumps(cfg.severity.get("levels") or {}, indent=1)}),
                        prompts.fill(user_t, {"claim": card({**dict(k), "class": k["cclass"]}),
                                              "test": _read(proof["test_path"]), "fix": _read(proof["fix_patch_path"])}))
            spec = CallSpec(role="writer", seat=seat, prompt_version=version, messages=messages,
                            subject_hash=hashlib.sha256(f"{k['cluster_id']}|{proof['proof_id']}".encode()).hexdigest(),
                            lens=k["lens"], effort=seat.get("effort"), schema=schema.WRITER,
                            schema_name="issue_writer")
        plan.append((k, proof, spec))
    s = calls.run_calls(conn, cfg, run_dir, [p[2] for p in plan if p[2]])
    out = {"calls": s, "issues": 0, "quarantined": 0, "unproven_dropped": 0, "drift": None, "blocking": []}
    for k, proof, spec in plan:
        res = calls.result_of(conn, spec, cfg) if spec else {"parsed": None}
        if res is None:
            continue                     # writer call still pending; written on resume
        write_issue(conn, cfg, run_dir, k, proof, res["parsed"])
        out["issues"] += 1

    # Guardrail: a cluster is never settled while one of its proof attempts was lost to a
    # failed platform call (rate limit, credits, gateway), fallback attempts included.
    lost = [] if args.get("accept_lost_attempts") else verify.lost_attempts(conn)
    lost_ids = {l["cluster_id"] for l in lost}
    states = ["unproven"] + (["upheld"] if args.get("close") else [])
    rows = conn.execute(
        f"SELECT cluster_id, state FROM clusters WHERE state IN ({','.join('?' * len(states))}) AND"
        " cluster_id NOT IN (SELECT cluster_id FROM outcomes) ORDER BY cluster_id", states).fetchall()
    for row in rows:
        if row["cluster_id"] in lost_ids:
            continue
        k = verify.cluster_row(conn, row["cluster_id"])
        out[_settle_unproven(conn, cfg, run_dir, k, closed=row["state"] == "upheld")] += 1

    ref = args.get("drift_ref") or cfg.get("target.drift_ref")
    out["drift"] = drift_check(conn, cfg, ref) if ref else {
        "ran": False, "note": "drift check skipped: set target.drift_ref or pass --drift-ref"}

    r = reconcile.check(conn, cfg, run_dir)
    out["blocking"] = [p for p in r["pending"]]
    if lost:
        seats = sorted({l["prover_seat"] for l in lost})
        out["blocking"].insert(0, (
            f"{sum(l['n'] for l in lost)} proof attempts on {len(lost_ids)} clusters were lost to failed model "
            f"calls (rate limit, credits or gateway) and those clusters are not settled: run `habeas prove run "
            f"--prover <seat>` again for {', '.join(seats)} (it repeats them), or pass "
            "--accept-lost-attempts to settle them as they are"))
    out["lost_attempts"] = lost
    with conn:
        if not s["pending"]:
            ledger.mark_stage(conn, "finalize")
        if not out["blocking"] and not s["pending"] and ledger.run(conn)["state"] == "running":
            conn.execute("UPDATE runs SET state = 'finished', finished_at = ?", (ledger.now(),))
            keep_config(cfg, run_dir)
    from . import report
    report.write(conn, cfg, run_dir)
    out["calls"]["state"] = ledger.run(conn)["state"]
    return out


def lines(s: dict) -> list[str]:
    d = s["drift"] or {}
    out = [f"finalize: {s['issues']} issue files written, {s['quarantined']} quarantined, "
           f"{s['unproven_dropped']} dropped as unproven",
           (f"drift check against {d['ref']}: {d['checked']} proofs repeated, {d['fixed']} fixed since, "
            f"{d['unclear']} unclear") if d.get("ran") else d.get("note", "")]
    if s["blocking"]:
        out.append("the run is PARTIAL and stays open:")
        out += [f"  - {b}" for b in s["blocking"][:4]]
        out.append("finish the stages, or pass --close to settle upheld clusters as unproven")
    out.append("report written: report.md, report.json")
    return out
