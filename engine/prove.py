"""S5 provers (plan 11.5). Any one valid proof is enough.

- native: `prove next` writes a brief for the orchestrator, which writes the
  test and fix and calls `verify-proof`.
- scripted: `prove run` asks a model for a test file and a fix patch, runs
  `verify`, and sends the failure back for another attempt.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path

from . import browse, calls, find, ledger, prompts, schema, verify
from .calls import CallSpec
from .merge import card


def _seat(cfg, seat_id: str) -> dict:
    seat = next((s for s in cfg.seats("prover") if s["id"] == seat_id), None)
    if seat is None:
        raise verify.VerifyRefused(f"{seat_id} is not an enabled prover seat")
    return seat


def queue(conn, cfg, seat_id: str) -> list[str]:
    """Upheld clusters this prover can still work on: most raising labs first,
    then most critic support."""
    labs = {s["id"]: s.get("lab") for role in ("finder", "trial_finder") for s in cfg.seats(role, True)}
    out = []
    for k in conn.execute("SELECT cluster_id FROM clusters WHERE state = 'upheld'").fetchall():
        cid = k["cluster_id"]
        if verify.exhausted(conn, cfg, cid, seat_id):
            continue
        seats = [r[0] for r in conn.execute(
            "SELECT DISTINCT c.seat_id FROM cluster_members m JOIN candidates d ON d.candidate_id ="
            " m.candidate_id JOIN calls c ON c.call_id = d.call_id WHERE m.cluster_id = ?", (cid,))]
        support = conn.execute("SELECT COUNT(*) FROM verdicts WHERE cluster_id = ? AND verdict = 'uphold'",
                               (cid,)).fetchone()[0]
        out.append((-len({labs.get(s, s) for s in seats}), -support, cid))
    return [cid for _, _, cid in sorted(out)]


def brief_parts(conn, cfg, cluster_id: str, feedback: str = "") -> dict:
    run = ledger.run(conn)
    k = verify.cluster_row(conn, cluster_id)
    unit = conn.execute("SELECT * FROM units WHERE unit_id = ?", (k["unit_id"],)).fetchone()
    text, context = find.unit_text(run["repo_path"], unit)
    dest = verify.test_destination(cluster_id, json.loads(k["locations_json"])[0]["path"])
    pc = cfg.run.get("provers") or {}
    system_t, user_t, version = prompts.load("prover_brief.md")
    values = {
        "invariant": k["invariant"],
        "lens_invariants": "\n".join(f"- {x}" for x in (cfg.lenses.get(k["lens"]) or {}).get("invariants", [])),
        "proof_shape": (cfg.lenses.get(k["lens"]) or {}).get("proof_shape", ""),
        "max_fix_lines": pc.get("max_fix_lines", 40), "max_fix_files": pc.get("max_fix_files", 3),
        "claim": card({**dict(k), "class": k["cclass"]}) + f"\nTest sketch: {k['test_sketch']}",
        "test_destination": dest, "test_command": shlex.join(verify.test_argv(cfg, [dest])),
        "unit": text, "context": context, "feedback": feedback}
    return {"system": prompts.fill(system_t, values), "user": prompts.fill(user_t, values),
            "version": version, "dest": dest, "cluster": k, "unit": unit}


def next_brief(conn, cfg, run_dir, seat_id: str) -> dict | None:
    _seat(cfg, seat_id)
    q = queue(conn, cfg, seat_id)
    if not q:
        return None
    cid = q[0]
    parts = brief_parts(conn, cfg, cid)
    attempt = conn.execute("SELECT COALESCE(MAX(attempt), 0) FROM proofs WHERE cluster_id = ? AND prover_seat = ?",
                           (cid, seat_id)).fetchone()[0] + 1
    out_dir = Path(run_dir) / "proofs" / cid / seat_id / f"attempt_{attempt}"
    out_dir.mkdir(parents=True, exist_ok=True)
    brief = out_dir / "brief.md"
    last = conn.execute("SELECT reason, dir FROM proofs WHERE cluster_id = ? AND prover_seat = ?"
                        " ORDER BY attempt DESC", (cid, seat_id)).fetchone()
    brief.write_text(
        f"# Proof brief: cluster {cid}, attempt {attempt}\n\n{parts['system']}\n\n{parts['user']}\n"
        + (f"\nThe previous attempt failed: {last['reason']}. Its logs are in {last['dir']}.\n" if last else "")
        + f"\n## How to deliver\n\n"
        f"1. Work in the audit clone: `{ledger.run(conn)['repo_path']}`.\n"
        f"2. Save the test as `{out_dir / 'proof_spec.rb'}` and the fix as a unified diff "
        f"`{out_dir / 'fix.patch'}` (paths relative to the repository root).\n"
        f"3. Restore the clone so `git status` is clean.\n"
        f"4. Run: `habeas verify-proof {cid} --test {out_dir / 'proof_spec.rb'} "
        f"--fix {out_dir / 'fix.patch'} --prover {seat_id}`\n"
        f"5. To decline: `habeas prove refuse {cid} --prover {seat_id}`\n\n"
        "Only the result of `habeas verify-proof` counts.\n", encoding="utf-8")
    return {"cluster_id": cid, "brief": str(brief), "attempt": attempt, "queue_length": len(q),
            "test_destination": parts["dest"]}


def refuse(conn, cfg, cluster_id: str, seat_id: str, reason: str) -> None:
    _seat(cfg, seat_id)
    verify.cluster_row(conn, cluster_id)
    verify.record(conn, cfg, cluster_id, seat_id, "refused", reason=reason or "declined")


def _related_material(conn, cfg, k) -> str:
    """Names of the specs next to the claimed code, so the prover knows what to read first."""
    repo = Path(ledger.run(conn)["repo_path"])
    paths = [l["path"] for l in json.loads(k["locations_json"])]
    root = verify.package_root(paths[0])
    specs = browse.Browser(repo, cfg).list([f"{root}/spec" if root else "spec"])
    related = verify.related_specs(repo, paths)
    return ("\nExisting specs for the claimed files: " + (", ".join(related) or "none found")
            + f"\n\nSpec files in this package:\n{specs}\n")


def _attempt(conn, cfg, run_dir, seat, cid, attempt, feedback, agg, retry_errors=False,
             fallback_for=None) -> tuple[str, dict]:
    """One proof attempt by a scripted prover: up to `max_steps` model calls. The model may
    read, list and search the audit clone, then submits a test and edits, or declines.
    Returns (pending | refused | error | submitted, details)."""
    bc = cfg.get("provers.browse") or {}
    can_browse = seat.get("browse", True) and not seat.get("non_sensitive_only")
    max_steps = int(bc.get("max_steps") or 8) if can_browse else 1
    budget = int(bc.get("max_chars") or 80000)
    repo = Path(ledger.run(conn)["repo_path"])
    with verify.clone_read(run_dir):
        browser = browse.Browser(repo, cfg)
        parts = brief_parts(conn, cfg, cid, feedback)
    system = parts["system"] + browse.PROTOCOL.replace("{{schema}}", json.dumps(schema.PROVER, indent=1)) \
        .replace("{{max_steps}}", str(max_steps))
    with verify.clone_read(run_dir):
        transcript = _related_material(conn, cfg, parts["cluster"]) if can_browse else ""
    for step in range(1, max_steps + 1):
        last = step == max_steps or len(transcript) > budget
        note = ("\nThis is your last step: answer with action `submit` or `decline`.\n" if last
                else f"\nStep {step} of {max_steps}. Two steps are left after this one: stop exploring"
                     " and prepare your submission.\n" if step == max_steps - 2
                else f"\nStep {step} of {max_steps}.\n")
        # on the last step the endpoint is only offered the two closing actions
        step_schema = schema.PROVER
        if last:
            step_schema = json.loads(json.dumps(schema.PROVER))
            step_schema["properties"]["action"]["enum"] = ["submit", "decline"]
        user = parts["user"] + transcript + note
        spec = CallSpec(
            role="prover", seat=seat, prompt_version=parts["version"],
            messages=lambda user=user: (system, user),
            subject_hash=hashlib.sha256(f"{cid}|{attempt}|{step}|{user}".encode()).hexdigest(),
            unit_id=parts["cluster"]["unit_id"], lens=parts["cluster"]["lens"], run_index=attempt,
            effort=seat.get("effort"), schema=step_schema, schema_name="prover_output",
            extra={"fallback_for": fallback_for} if fallback_for else {})
        s = calls.run_calls(conn, cfg, run_dir, [spec], retry_errors=retry_errors)
        for key in ("planned", "already_done", "executed", "pending"):
            agg[key] += s[key]
        for status, n in s["by_status"].items():
            agg["by_status"][status] = agg["by_status"].get(status, 0) + n
        if s["pending"]:
            return "pending", {}
        res = calls.result_of(conn, spec, cfg)
        parsed = res["parsed"]
        if res["status"] == "refused" or (parsed and parsed["action"] == "decline"):
            return "refused", {"reason": (parsed or {}).get("reason") or res["reason"] or "refused"}
        if not parsed:
            if last and str(res["reason"] or "").startswith("invalid_json: $.action"):
                return "error", {"reason": f"no submission within {max_steps} steps"}
            return "error", {"reason": f"prover call {res['status']}: {res['reason']}"}
        action = parsed["action"]
        if action == "submit" and last and not parsed["test_file"].strip() and not parsed["edits"]:
            # forced to close on the last step, the model handed in nothing
            return "error", {"reason": f"no submission within {max_steps} steps"}
        if action == "submit":
            with verify.clone_read(run_dir):
                patch, problem = browse.build_patch(browser, parsed["edits"], parts["dest"])
            if problem is None and not parsed["test_file"].strip():
                problem = "test_file is empty"
            if problem is None:
                return "submitted", {"test_file": parsed["test_file"], "patch": patch, "steps": step,
                                     "test_package": (parsed.get("test_package") or "").strip() or None}
            if last:
                return "error", {"reason": f"submission not usable: {problem}"}
            transcript += f"\n[step {step}] Your submission was not usable: {problem}. Correct it and submit again.\n"
            continue
        if last:
            return "error", {"reason": f"no submission within {max_steps} steps"}
        with verify.clone_read(run_dir):
            if action == "read":
                out = browser.read(parsed["paths"])
            elif action == "list":
                out = browser.list(parsed["paths"])
            else:
                out = browser.search(parsed["query"], parsed["paths"])
        transcript += f"\n[step {step}] You asked to {action} {parsed['paths'] or parsed['query']!r}:\n{out}\n"
    return "error", {"reason": "no submission"}


def run_scripted(conn, cfg, run_dir, args: dict) -> dict:
    """Run one scripted prover over the proof queue."""
    seat = _seat(cfg, args["prover"])
    if seat.get("endpoint") == "native" or seat.get("mode") != "scripted":
        raise verify.VerifyRefused(f"{seat['id']} is not a scripted prover; use `habeas prove next`")
    run_dir = Path(run_dir)
    agg = {"planned": 0, "already_done": 0, "executed": 0, "pending": 0, "by_status": {}, "warnings": []}
    out = {"clusters": 0, "valid": 0, "invalid": 0, "refused": 0, "errors": 0}
    if args.get("retry_declines") and calls.fallback_seat(seat, cfg):
        # declines recorded before the fallback existed: the fallback model now gets its turn
        with conn:
            n = conn.execute("DELETE FROM proofs WHERE prover_seat = ? AND status = 'refused'"
                             " AND reason NOT LIKE '%| fallback %' AND cluster_id IN"
                             " (SELECT cluster_id FROM clusters WHERE state IN ('upheld', 'unproven'))",
                             (seat["id"],)).rowcount
            conn.execute("UPDATE clusters SET state = 'upheld' WHERE state = 'unproven'")
        with open(run_dir / "proof_redo.log", "a") as f:
            f.write(f"{seat['id']}: {n} declines given back for the fallback model\n")
    # Attempts lost to a failed platform call (rate limit, credits, gateway) are always given
    # back, on the seat's model or its fallback: no cluster is settled on a call that never
    # reached a model. `finalize` refuses to close a run while any are left.
    with conn:
        n = conn.execute(f"DELETE FROM proofs WHERE prover_seat = ? AND {verify.LOST_ATTEMPT} AND cluster_id IN"
                         " (SELECT cluster_id FROM clusters WHERE state IN ('upheld', 'unproven'))",
                         (seat["id"],)).rowcount
        if n:
            conn.execute("UPDATE clusters SET state = 'upheld' WHERE state = 'unproven' AND cluster_id NOT IN"
                         " (SELECT cluster_id FROM outcomes)")
    if n:
        with open(run_dir / "proof_redo.log", "a") as f:
            f.write(f"{seat['id']}: {n} attempts lost to failed platform calls given back\n")
        args = {**args, "retry_errors": True}        # the failed calls must be sent again, not read back
    if args.get("retry_errors"):
        # attempts lost to a failed model call are given back; the failed calls stay in `calls`
        with conn:
            n = conn.execute("DELETE FROM proofs WHERE prover_seat = ? AND status = 'error'"
                             " AND (reason LIKE 'prover call error:%' OR reason LIKE '%| fallback %: prover call error:%')"
                             " AND cluster_id IN"
                             " (SELECT cluster_id FROM clusters WHERE state = 'upheld')", (seat["id"],)).rowcount
        with open(run_dir / "proof_redo.log", "a") as f:
            f.write(f"{seat['id']}: {n} attempts lost to call errors given back\n")
    todo = queue(conn, cfg, seat["id"])
    if args.get("lens"):
        keep = {r[0] for r in conn.execute("SELECT cluster_id FROM clusters WHERE lens = ?", (args["lens"],))}
        todo = [c for c in todo if c in keep]
    if args.get("after"):
        # only clusters another prover has finished with, so two provers can run at the same time
        todo = [c for c in todo if verify.exhausted(conn, cfg, c, args["after"])]
    if args.get("shard"):
        # "i/n": this process takes every cluster whose hash falls in slot i, so n processes
        # can browse and write proofs side by side; the clone itself is locked per proof
        i, n = (int(x) for x in str(args["shard"]).split("/"))
        todo = [c for c in todo if int(hashlib.sha256(c.encode()).hexdigest(), 16) % n == i]
    if args.get("max_clusters"):
        todo = todo[:int(args["max_clusters"])]
    halted = False
    for cid in todo:
        out["clusters"] += 1
        feedback = ""
        while not verify.exhausted(conn, cfg, cid, seat["id"]) and \
                verify.cluster_row(conn, cid)["state"] == "upheld":
            attempt = conn.execute("SELECT COALESCE(MAX(attempt), 0) FROM proofs WHERE cluster_id = ? AND prover_seat = ?",
                                   (cid, seat["id"])).fetchone()[0] + 1
            # a failed call is always sent again: replaying a stored failure can never help an attempt
            kind, d = _attempt(conn, cfg, run_dir, seat, cid, attempt, feedback, agg, True)
            other = calls.fallback_seat(seat, cfg) if kind == "refused" else None
            author = seat
            if other is not None:
                # a decline of any kind is not final: the fallback model gets the same attempt
                first = d["reason"]
                kind, d = _attempt(conn, cfg, run_dir, other, cid, attempt, feedback, agg,
                                   True, fallback_for=seat["model"])
                if kind in ("refused", "error"):
                    d = {"reason": f"{seat['model']} declined: {first} | fallback {other['model']}: {d['reason']}"}
                elif kind == "submitted":
                    author = other
                    out["fallback_submissions"] = out.get("fallback_submissions", 0) + 1
            if kind == "pending":
                halted = True
                break
            if kind == "refused":
                verify.record(conn, cfg, cid, seat["id"], "refused", reason=d["reason"])
                out["refused"] += 1
                break
            if kind == "error":
                verify.record(conn, cfg, cid, seat["id"], "error", reason=d["reason"])
                out["errors"] += 1
                feedback = f"\nYour previous attempt ended without a usable submission: {d['reason']}.\n"
                continue
            work = run_dir / "proofs" / cid / seat["id"] / f"attempt_{attempt}"
            work.mkdir(parents=True, exist_ok=True)
            (work / "proof_spec.rb").write_text(d["test_file"], encoding="utf-8")
            (work / "fix.patch").write_text(d["patch"], encoding="utf-8")
            try:
                v = verify.verify(conn, cfg, run_dir, cid, work / "proof_spec.rb", work / "fix.patch", seat["id"],
                                  test_package=d.get("test_package"), model=author.get("model"),
                                  lab=calls.model_lab(author.get("model"), cfg))
            except verify.VerifyRefused:
                break           # another prover settled the cluster in the meantime
            if v["status"] == "valid":
                out["valid"] += 1
                break
            out["invalid"] += 1
            reason = str(v["reason"])
            name = ("green_1.log" if reason.startswith("test_still_fails")
                    else "regress_after.log" if reason.startswith("regression") else "red_1.log")
            feedback = (f"\nYour previous attempt was not accepted: {v['reason']}.\n"
                        f"Previous test:\n{d['test_file']}\n\nPrevious fix:\n{d['patch']}\n\n"
                        f"Log:\n{verify._tail(Path(v['dir']) / name, 4000)}\n")
        if halted:
            break
    agg["state"] = ledger.run(conn)["state"]
    agg["budget"] = calls.budget_status(conn, cfg)
    out["calls"] = agg
    out["queue_left"] = len(queue(conn, cfg, seat["id"]))
    return out


def lines(s: dict) -> list[str]:
    return [f"prove: {s['clusters']} clusters worked: {s['valid']} proven, {s['invalid']} attempts "
            f"not accepted, {s['refused']} refused, {s['errors']} call errors",
            f"proof queue left for this prover: {s['queue_left']}"]
