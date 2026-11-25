"""The `habeas` command (plan 14). Prints a short summary; full model output
goes to disk under the run directory."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from . import (adapter, calls, config, critique, finalize, find, ledger, merge, preflight, prove,
               reconcile, report, static_rules, units, verify, schema)

EXIT_ERRORS, EXIT_TODO, EXIT_HALTED = 1, 2, 3


class CliError(Exception):
    pass


def runs_root() -> Path:
    return Path(os.environ.get("HABEAS_RUNS_DIR") or config.SKILL_ROOT / "runs")


_conns: list = []


def _connect(run_dir):
    conn = ledger.connect(run_dir)
    _conns.append(conn)
    return conn


def run_dir_of(args) -> Path:
    if args.run_dir:
        return Path(args.run_dir)
    if os.environ.get("HABEAS_RUN_DIR"):
        return Path(os.environ["HABEAS_RUN_DIR"])
    pointer = runs_root() / "current"
    if pointer.exists():
        return runs_root() / pointer.read_text().strip()
    raise CliError("no run selected: pass --run-dir or set HABEAS_RUN_DIR")


def emit(args, data: dict, lines: list[str]) -> None:
    print(json.dumps(data, indent=1) if args.json else "\n".join(lines[:20]))


def _money(v) -> str:
    return "none" if v is None else f"${v:,.2f}"


def budget_lines(b: dict) -> list[str]:
    lines = [f"spend: {_money(b['spent_usd'])} of {_money(b['limit_usd'])}, "
             f"{b['spent_tokens']:,} tokens of "
             f"{'no limit' if b['limit_tokens'] is None else format(b['limit_tokens'], ',')}"]
    if b["projected_more_usd"] is not None:
        lines.append(f"projection to finish: about {_money(b['projected_more_usd'])} "
                     f"and {b['projected_more_tokens']:,} tokens more")
    for unit in b["warn"]:
        lines.append(f"WARNING: past the warning level of the {unit} limit")
    for unit in b["at_limit"]:
        lines.append(f"AT LIMIT: the {unit} limit is reached")
    return lines


# ------------------------------------------------------------------ commands

def cmd_config_check(args) -> int:
    cfg = config.load(args.config_dir)
    online: list[str] = []
    if args.online:
        online = check_online(cfg)
    data = {"errors": cfg.errors, "todos": cfg.todos, "online": online}
    lines = [f"config: {cfg.dir}",
             f"errors: {len(cfg.errors)}, TODO values: {len(cfg.todos)}"
             + (f", online problems: {len(online)}" if args.online else "")]
    lines += [f"  error: {e}" for e in cfg.errors]
    lines += [f"  TODO: {t}" for t in cfg.todos]
    lines += [f"  online: {o}" for o in online]
    emit(args, data, lines)
    if cfg.errors or online:
        return EXIT_ERRORS
    return EXIT_TODO if cfg.todos else 0


def check_online(cfg) -> list[str]:
    """Each model ID must be listed by its endpoint and accept its effort levels."""
    problems, listings = [], {}
    for role in config.ROLES:
        for seat in cfg.seats(role):
            if seat.get("endpoint") == "native":
                continue
            ep = cfg.endpoint(seat)
            name = seat["endpoint"]
            if name not in listings:
                try:
                    req = urllib.request.Request(ep["base_url"].rstrip("/") + "/models")
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        listings[name] = {m["id"]: m for m in json.load(resp)["data"]}
                except Exception as e:
                    listings[name] = None
                    problems.append(f"endpoint {name}: cannot list models ({e})")
            models = listings[name]
            if models is None:
                continue
            efforts = set(seat.get("effort_schedule") or [])
            if seat.get("effort"):
                efforts.add(seat["effort"])
            for key in ("model", "alternate_model"):
                model = seat.get(key)
                if not model:
                    continue
                if model not in models:
                    problems.append(f"seat {seat['id']}: {key} {model} is not listed by {name}")
                    continue
                supported = (models[model].get("reasoning") or {}).get("supported_efforts")
                if supported is not None:
                    bad = sorted(efforts - set(supported))
                    if bad:
                        problems.append(f"seat {seat['id']}: {model} does not accept effort "
                                        f"{', '.join(bad)} (accepts {', '.join(supported)})")
    return problems


def cmd_preflight(args) -> int:
    cfg = config.load(args.config_dir)
    cfg.require_ready()
    root = runs_root()
    root.mkdir(parents=True, exist_ok=True)
    run_dir, s = preflight.run(cfg, root, mode=args.mode)
    conn = _connect(run_dir)
    s.update(static_rules.run(conn, cfg, run_dir))
    run = ledger.run(conn)
    s["finder_calls_planned"] = len(find.plan(conn, cfg, run["repo_path"]))
    lines = [f"run {s['run_id']} created at {s['run_dir']}",
             f"audited commit: {s['commit']}",
             f"units: {s['units']} from {s['files']} files ({s['packed_units']} packed, "
             f"{s['split_units']} split), about {s['unit_tokens_estimate']:,} tokens",
             f"blocked units: {s['blocked_units']}" + (" (see blocked_units.json)" if s["blocked_units"] else ""),
             f"scanner hits: {s['scanner_hits']}, baseline entries: {s['baseline_entries']}"]
    lines += [f"scanner {name}: {status}" for name, status in s["scanners"].items()]
    lines += [f"note: {n}" for n in s["notes"][:3]]
    lines += [f"static rules: {s['static_candidates']} candidates, {s['tech_debt']} tech-debt notes",
              f"finder calls planned: {s['finder_calls_planned']}"]
    emit(args, s, lines)
    return 0


def cmd_scout(args) -> int:
    cfg, run_dir, conn = _open(args)
    s = preflight.scout(conn)
    emit(args, s, [f"queue ordered: {s['units']} units, {s['with_scanner_hits']} with scanner hits first"])
    return 0


def cmd_reconcile(args) -> int:
    cfg, run_dir, conn = _open(args)
    r = reconcile.check(conn, cfg, run_dir)
    c = r["counts"]
    lines = [f"reconcile: {'OK' if r['ok'] else 'FAILED'} (run {r['state']})",
             f"candidates {c['candidates']}, clusters {c['clusters']}, outcomes {c['outcomes']}"]
    lines += [f"  FAIL: {f}" for f in r["failures"][:10]]
    lines += [f"  pending: {p}" for p in r["pending"][:6]]
    emit(args, r, lines)
    return 0 if r["ok"] else EXIT_ERRORS


def _open(args):
    run_dir = run_dir_of(args)
    kept = Path(run_dir) / "config"
    # a finished run is read with the configuration it finished under, so a later change of
    # lenses or seats cannot make its report or its reconciliation look incomplete
    if not args.config_dir and kept.is_dir() and (Path(run_dir) / "ledger.sqlite").exists():
        conn = _connect(run_dir)
        if ledger.run(conn)["state"] == "finished":
            return config.load(kept), run_dir, conn
        conn.close()
    cfg = config.load(args.config_dir)
    cfg.require_ready()
    return cfg, run_dir, _connect(run_dir)


def _detach(args, run_dir: Path, stage: str) -> int:
    argv = [a for a in sys.argv[1:] if a != "--detach"]
    log = open(run_dir / f"{stage}.log", "ab")
    proc = subprocess.Popen([sys.executable, "-m", "engine.cli", *argv], stdout=log, stderr=log,
                            stdin=subprocess.DEVNULL, start_new_session=True,
                            env={**os.environ, "PYTHONPATH": str(config.SKILL_ROOT)})
    emit(args, {"detached": True, "pid": proc.pid, "log": str(run_dir / f"{stage}.log")},
         [f"{stage} started in the background (pid {proc.pid})",
          f"log: {run_dir / f'{stage}.log'}", "poll with: habeas status"])
    return 0


def stage_find(conn, cfg, run_dir, a: dict) -> dict:
    run = ledger.run(conn)
    specs = find.plan(conn, cfg, run["repo_path"], a.get("engine"), a.get("max_units"))
    s = calls.run_calls(conn, cfg, run_dir, specs, find.record_candidates,
                        retry_errors=bool(a.get("retry_errors")))
    if not s["pending"]:
        with conn:
            ledger.mark_stage(conn, "find")
    return {"calls": s, "candidates_total": conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]}


def find_lines(s: dict) -> list[str]:
    return [f"candidates in ledger: {s['candidates_total']}"]


# stage name -> (function(conn, cfg, run_dir, args) -> summary with "calls", lines(summary))
STAGES = {
    "find": (stage_find, find_lines),
    "merge": (merge.run, merge.lines),
    "critique": (critique.run, critique.lines),
    "prove": (prove.run_scripted, prove.lines),
    "finalize": (finalize.run, finalize.lines),
}


def _run_stage(args, cfg, run_dir, conn, stage: str, stage_args: dict) -> int:
    shard = stage_args.get("shard")
    if not shard and calls.pid_alive(run_dir):
        raise CliError("a stage is already running for this run; poll `habeas status`")
    run = ledger.run(conn)
    all_args = json.loads(run["stage_args_json"] or "{}")
    all_args[stage] = stage_args
    # on a paused or stopped run, keep the interrupted stage as the one `resume` continues
    current = stage if run["state"] == "running" else run["stage"]
    with conn:
        conn.execute("UPDATE runs SET stage = ?, stage_args_json = ?", (current, json.dumps(all_args)))
    pid_file = run_dir / ("stage.pid" if not shard else
                          f"stage.{stage_args.get('prover') or stage}.shard{shard.replace('/', 'of')}.pid")
    pid_file.write_text(str(os.getpid()))
    fn, describe = STAGES[stage]
    try:
        s = fn(conn, cfg, run_dir, stage_args)
    finally:
        pid_file.unlink(missing_ok=True)
    c = s["calls"]
    lines = [f"{stage}: {c['planned']} calls planned, {c['already_done']} already done, "
             f"{c['executed']} executed now, {c['pending']} pending",
             "this pass by status: " + (", ".join(f"{k} {v}" for k, v in sorted(c["by_status"].items())) or "none")]
    lines += describe(s) + [f"run state: {c['state']}"] + budget_lines(c["budget"])
    if c["state"] == "paused_budget":
        lines.append("PAUSED at a budget limit. Ask the user; then `habeas budget raise` and `habeas resume`.")
    if c.get("account_error"):
        lines.append(f"STOPPED: the endpoint rejected the account: {c['account_error'][:150]}")
        lines.append("fix the key or credits, then `habeas resume` (failed calls: add `find --retry-errors`).")
    elif c["state"] == "stopped":
        lines.append("stopped on request; `habeas resume` continues.")
    emit(args, s, lines)
    return EXIT_HALTED if c["pending"] else 0


def _stage_cmd(stage: str, collect):
    def cmd(args) -> int:
        cfg, run_dir, conn = _open(args)
        state = ledger.run(conn)["state"]
        if state != "running":
            raise CliError(f"run is {state}; use `habeas resume`"
                           + (" after `habeas budget raise`" if state == "paused_budget" else ""))
        if getattr(args, "detach", False):
            return _detach(args, run_dir, stage)
        return _run_stage(args, cfg, run_dir, conn, stage, collect(args))
    return cmd


cmd_find = _stage_cmd("find", lambda a: {"engine": a.engine, "max_units": a.max_units,
                                         "retry_errors": a.retry_errors})
cmd_merge = _stage_cmd("merge", lambda a: {"retry_errors": a.retry_errors})
cmd_critique = _stage_cmd("critique", lambda a: {"retry_errors": a.retry_errors})


cmd_prove_run = _stage_cmd("prove", lambda a: {"prover": a.prover, "max_clusters": a.max_clusters,
                                                    **({"shard": a.shard} if a.shard else {}),
                                                    **({"retry_errors": True} if a.retry_errors else {}),
                                                    **({"after": a.after} if a.after else {}),
                                                    **({"lens": a.lens} if a.lens else {}),
                                                    **({"retry_declines": True} if a.retry_declines else {})})


def cmd_finalize(args) -> int:
    cfg, run_dir, conn = _open(args)
    state = ledger.run(conn)["state"]
    if state == "finished":
        raise CliError("run is finished; `habeas report` rewrites the report")
    # a declined budget raise leaves the run paused: finalize still works on what exists
    return _run_stage(args, cfg, run_dir, conn, "finalize", {"close": args.close, "drift_ref": args.drift_ref,
                                                             "accept_lost_attempts": args.accept_lost_attempts})


def cmd_report(args) -> int:
    cfg, run_dir, conn = _open(args)
    d = report.write(conn, cfg, run_dir)
    sm = d["summary"]
    lines = [f"report for run {d['run']['run_id']} ({d['run']['state']})" + (" PARTIAL" if d["partial"] else ""),
             f"proven issues: {sm['proven']} ("
             + ", ".join(f"{n} {sev}" for sev, n in sm["proven_by_severity"].items()) + ")",
             f"quarantined: {sm['quarantined']}, coverage: {sm['coverage']}",
             f"reconcile: {'OK' if d['reconcile']['ok'] else 'FAILED'}"]
    lines += [f"  partial: {p}" for p in d["partial"][:4]]
    lines += [f"  FAIL: {f}" for f in d["reconcile"]["failures"][:5]]
    lines += [f"written: {run_dir / 'report.md'}", f"written: {run_dir / 'report.json'}"]
    emit(args, {k: d[k] for k in ("run", "partial", "summary", "counts", "reconcile")}, lines)
    return 0


def cmd_show(args) -> int:
    cfg, run_dir, conn = _open(args)
    k = verify.cluster_row(conn, args.cluster)
    cid = k["cluster_id"]
    data = {
        "cluster_id": cid, "state": k["state"], "lens": k["lens"], "class": k["cclass"],
        "severity": k["severity"], "known_ref": k["known_ref"],
        "claim": {"title": k["title"], "invariant": k["invariant"], "violation": k["violation"],
                  "locations": json.loads(k["locations_json"]), "trigger": k["trigger"],
                  "test_sketch": k["test_sketch"]},
        "members": [dict(r) for r in conn.execute(
            "SELECT d.candidate_id, c.seat_id, d.title FROM cluster_members m JOIN candidates d ON"
            " d.candidate_id = m.candidate_id JOIN calls c ON c.call_id = d.call_id WHERE m.cluster_id = ?", (cid,))],
        "verdicts": [dict(r) for r in conn.execute(
            "SELECT critic_seat, verdict, reason FROM verdicts WHERE cluster_id = ?", (cid,))],
        "proofs": [dict(r) for r in conn.execute(
            "SELECT prover_seat, attempt, status, mechanical, reason, checker_seat, checker_verdict,"
            " checker_reason, dir FROM proofs WHERE cluster_id = ? ORDER BY created_at", (cid,))],
        "outcome": [dict(r) for r in conn.execute("SELECT * FROM outcomes WHERE cluster_id = ?", (cid,))],
    }
    c = data["claim"]
    lines = [f"cluster {cid}: {k['state']}, lens {k['lens']}, class {k['cclass']}",
             f"title: {c['title']}", f"invariant: {c['invariant']}", f"violation: {c['violation']}",
             "locations: " + ", ".join(f"{l['path']}:{l['start_line']}-{l['end_line']}" for l in c["locations"]),
             f"trigger: {c['trigger']}", f"test sketch: {c['test_sketch']}",
             "raised by: " + ", ".join(sorted({m["seat_id"] for m in data["members"]}))]
    lines += [f"critic {v['critic_seat']}: {v['verdict']}: {str(v['reason'])[:160]}" for v in data["verdicts"]]
    lines += [f"proof {p['prover_seat']} #{p['attempt']}: {p['status']}"
              + (f" ({p['reason']})" if p["reason"] else "") for p in data["proofs"]]
    lines += [f"outcome: {o['outcome']}: {o['detail']}" for o in data["outcome"]]
    emit(args, data, lines)
    return 0


def cmd_prove_next(args) -> int:
    cfg, run_dir, conn = _open(args)
    b = prove.next_brief(conn, cfg, run_dir, args.prover)
    if b is None:
        emit(args, {"cluster_id": None}, [f"no cluster is waiting for {args.prover}"])
        return 0
    emit(args, b, [f"cluster {b['cluster_id']} (attempt {b['attempt']}, {b['queue_length']} in the queue)",
                   f"brief: {b['brief']}"])
    return 0


def cmd_prove_refuse(args) -> int:
    cfg, run_dir, conn = _open(args)
    prove.refuse(conn, cfg, args.cluster, args.prover, args.reason)
    state = verify.cluster_row(conn, args.cluster)["state"]
    emit(args, {"cluster_id": args.cluster, "state": state},
         [f"recorded: {args.prover} declined cluster {args.cluster} (cluster is {state})"])
    return 0


def cmd_verify_proof(args) -> int:
    cfg, run_dir, conn = _open(args)
    v = verify.verify(conn, cfg, run_dir, args.cluster, args.test, args.fix, args.prover,
                      test_package=args.test_package)
    lines = [f"proof for cluster {v['cluster_id']} by {v['prover']}, attempt {v['attempt']}: {v['status'].upper()}",
             f"mechanical red-green: {v['mechanical']}" + (f" ({v['reason']})" if v["reason"] else "")]
    if v.get("checker"):
        lines.append(f"proof check by {v['checker']}: {v['checker_verdict']}: {str(v['checker_reason'])[:200]}")
    lines += [f"audit clone clean: {'yes' if v['clone_clean'] else 'NO'}", f"logs: {v['dir']}"]
    emit(args, v, lines)
    return 0 if v["status"] == "valid" else EXIT_HALTED


def cmd_prove_recheck(args) -> int:
    cfg, run_dir, conn = _open(args)
    r = verify.recheck(conn, cfg, run_dir, args.reason, args.max_proofs)
    emit(args, r, [f"recheck: {r['considered']} stored proofs match, {r['rechecked']} run again: "
                   f"{r['now_valid']} now accepted, {r['now_rejected']} now rejected by the checker, "
                   f"{r['unchanged']} unchanged, {r['skipped']} skipped"])
    return 0


def cmd_review(args) -> int:
    cfg, run_dir, conn = _open(args)
    k = verify.cluster_row(conn, args.cluster)
    note = Path(args.note_file).read_text(encoding="utf-8") if args.note_file else (args.note or "")
    if not note.strip():
        raise CliError("a review needs a note: pass --note or --note-file")
    with conn:
        conn.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?,?,?)",
                     (k["cluster_id"], args.severity, note.strip(), args.reviewer, ledger.now()))
    emit(args, {"cluster_id": k["cluster_id"], "severity": args.severity},
         [f"review recorded for cluster {k['cluster_id']}: severity {args.severity}",
          "it is written into the issue file at finalize"])
    return 0


def cmd_resume(args) -> int:
    cfg, run_dir, conn = _open(args)
    run = ledger.run(conn)
    if run["state"] not in ("paused_budget", "stopped"):
        raise CliError(f"run is {run['state']}; nothing to resume")
    if run["stage"] not in STAGES:
        raise CliError(f"cannot resume stage {run['stage']!r}")
    if args.detach:
        return _detach(args, run_dir, run["stage"])
    (run_dir / "STOP").unlink(missing_ok=True)
    with conn:
        ledger.set_state(conn, "running")
    stage_args = json.loads(run["stage_args_json"] or "{}").get(run["stage"], {})
    return _run_stage(args, cfg, run_dir, conn, run["stage"], stage_args)


def cmd_stop(args) -> int:
    run_dir = run_dir_of(args)
    conn = _connect(run_dir)
    if calls.pid_alive(run_dir):
        (run_dir / "STOP").write_text("stop requested\n")
        msg = "stop requested: calls in flight will finish, then the stage exits"
    else:
        with conn:
            if ledger.run(conn)["state"] == "running":
                ledger.set_state(conn, "stopped")
        msg = "no stage is running; run marked stopped"
    emit(args, {"message": msg, "state": ledger.run(conn)["state"]}, [msg])
    return 0


def cmd_budget_raise(args) -> int:
    run_dir = run_dir_of(args)
    conn = _connect(run_dir)
    run = ledger.run(conn)
    if (args.usd is None) == (args.tokens is None):
        raise CliError("give exactly one of --usd or --tokens: the new limit the user approved")
    unit, new = ("usd", args.usd) if args.usd is not None else ("tokens", args.tokens)
    old = run[f"limit_{unit}"]
    spent_usd, spent_tokens, _ = ledger.spend(conn)
    spent = spent_usd if unit == "usd" else spent_tokens
    if old is not None and new <= old:
        raise CliError(f"the new {unit} limit must be above the current one ({old:g})")
    if new <= spent:
        raise CliError(f"the new {unit} limit must be above what is already used ({spent:g})")
    with conn:
        conn.execute(f"UPDATE runs SET limit_{unit} = ?", (new,))
        ledger.budget_event(conn, "raise", unit, old=old, new=new, approved_by=args.approved_by)
    emit(args, {"unit": unit, "old_limit": old, "new_limit": new, "approved_by": args.approved_by},
         [f"{unit} limit raised from {old if old is not None else 'none'} to {new:g}, "
          f"approved by {args.approved_by}", "continue with: habeas resume"])
    return 0


def cmd_status(args) -> int:
    cfg = config.load(args.config_dir)
    run_dir = run_dir_of(args)
    conn = _connect(run_dir)
    run = ledger.run(conn)
    by_status = dict(conn.execute("SELECT status, COUNT(*) FROM calls GROUP BY status").fetchall())
    stage_args = json.loads(run["stage_args_json"] or "{}").get("find", {})
    pending = None
    if not cfg.errors and not cfg.todos:
        specs = find.plan(conn, cfg, run["repo_path"], stage_args.get("engine"), stage_args.get("max_units"))
        pending = len({s.key for s in specs} - calls.finished_keys(conn))
    budget = calls.budget_status(conn, cfg, pending or 0)
    events = [dict(r) for r in conn.execute(
        "SELECT kind, unit, old_limit, new_limit, approved_by, created_at FROM budget_events ORDER BY event_id")]
    data = {"run_id": run["run_id"], "state": run["state"], "stage": run["stage"],
            "stage_running": calls.pid_alive(run_dir), "calls": by_status,
            "finder_calls_pending": pending, "partial": bool(pending),
            "units": conn.execute("SELECT COUNT(*) FROM units").fetchone()[0],
            "candidates": conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0],
            "budget": budget, "budget_events": events}
    lines = [f"run {run['run_id']}: {run['state']}"
             + (f", stage {run['stage']}" + (" (running)" if data["stage_running"] else "") if run["stage"] else ""),
             f"units: {data['units']}, candidates: {data['candidates']}",
             "calls: " + (", ".join(f"{k} {v}" for k, v in sorted(by_status.items())) or "none")]
    if pending is not None:
        lines.append(f"finder calls pending: {pending}" + (" (results so far are partial)" if pending else ""))
    lines += budget_lines(budget)
    raises = [e for e in events if e["kind"] == "raise"]
    for e in raises[-3:]:
        lines.append(f"limit raised: {e['unit']} {e['old_limit']} -> {e['new_limit']} by {e['approved_by']}")
    if run["state"] == "paused_budget":
        lines.append("PAUSED at a budget limit: ask the user before `habeas budget raise`.")
    emit(args, data, lines)
    return 0


# ---------------------------------------------------------------------- main

def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config-dir")
    common.add_argument("--run-dir")
    common.add_argument("--json", action="store_true")
    p = argparse.ArgumentParser(prog="habeas", parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("config", parents=[common]).add_subparsers(dest="sub", required=True)
    cc = c.add_parser("check", parents=[common])
    cc.add_argument("--online", action="store_true")
    cc.set_defaults(fn=cmd_config_check)

    f = sub.add_parser("find", parents=[common])
    f.add_argument("--detach", action="store_true")
    f.add_argument("--engine")
    f.add_argument("--max-units", type=int)
    f.add_argument("--retry-errors", action="store_true",
                   help="call again the calls recorded as error")
    f.set_defaults(fn=cmd_find)

    for name, fn in (("merge", cmd_merge), ("critique", cmd_critique)):
        sp = sub.add_parser(name, parents=[common])
        sp.add_argument("--detach", action="store_true")
        sp.add_argument("--retry-errors", action="store_true")
        sp.set_defaults(fn=fn)

    pr = sub.add_parser("prove", parents=[common]).add_subparsers(dest="sub", required=True)
    pn = pr.add_parser("next", parents=[common])
    pn.add_argument("--prover", required=True)
    pn.set_defaults(fn=cmd_prove_next)
    prr = pr.add_parser("run", parents=[common])
    prr.add_argument("--prover", required=True)
    prr.add_argument("--max-clusters", type=int)
    prr.add_argument("--detach", action="store_true")
    prr.add_argument("--retry-errors", action="store_true",
                     help="give back attempts lost to failed model calls and call again")
    prr.add_argument("--retry-declines", action="store_true",
                     help="send clusters this prover declined earlier to its fallback model")
    prr.add_argument("--lens", help="take only clusters of this lens")
    prr.add_argument("--after", help="prover seat: take only clusters that seat has finished with")
    prr.add_argument("--shard", help="i/n: work only slot i of n, to run n processes side by side")
    prr.set_defaults(fn=cmd_prove_run)
    prc = pr.add_parser("recheck", parents=[common])
    prc.add_argument("--reason", required=True,
                     help="SQL LIKE pattern on the failure reason, for example 'red_is_not_an_assertion_failure%%'")
    prc.add_argument("--max-proofs", type=int)
    prc.set_defaults(fn=cmd_prove_recheck)
    pf2 = pr.add_parser("refuse", parents=[common])
    pf2.add_argument("cluster")
    pf2.add_argument("--prover", default="prover-native")
    pf2.add_argument("--reason", default="declined")
    pf2.set_defaults(fn=cmd_prove_refuse)
    vp = sub.add_parser("verify-proof", parents=[common])
    vp.add_argument("cluster")
    vp.add_argument("--test", required=True)
    vp.add_argument("--fix", required=True)
    vp.add_argument("--prover", default="prover-native")
    vp.add_argument("--test-package", help="place the test in this package (for example engines/billing) "
                    "when the code to fix lives there; the fix must change a file in it")
    vp.set_defaults(fn=cmd_verify_proof)

    fz = sub.add_parser("finalize", parents=[common])
    fz.add_argument("--close", action="store_true",
                    help="settle upheld clusters whose provers did not finish as unproven")
    fz.add_argument("--accept-lost-attempts", action="store_true",
                    help="settle clusters even though a proof attempt was lost to a failed model call")
    fz.add_argument("--drift-ref", help="repeat each proof's red step on this git ref")
    fz.set_defaults(fn=cmd_finalize)
    sub.add_parser("report", parents=[common]).set_defaults(fn=cmd_report)
    sh = sub.add_parser("show", parents=[common])
    sh.add_argument("cluster")
    sh.set_defaults(fn=cmd_show)

    rv = sub.add_parser("review", parents=[common])
    rv.add_argument("cluster")
    rv.add_argument("--severity", required=True, choices=schema.SEVERITIES)
    rv.add_argument("--note")
    rv.add_argument("--note-file")
    rv.add_argument("--reviewer", default="owner")
    rv.set_defaults(fn=cmd_review)
    r = sub.add_parser("resume", parents=[common])
    r.add_argument("--detach", action="store_true")
    r.set_defaults(fn=cmd_resume)

    pf = sub.add_parser("preflight", parents=[common])
    pf.add_argument("--mode", choices=["audit", "bakeoff"], default="audit")
    pf.set_defaults(fn=cmd_preflight)
    sub.add_parser("scout", parents=[common]).set_defaults(fn=cmd_scout)
    sub.add_parser("reconcile", parents=[common]).set_defaults(fn=cmd_reconcile)
    sub.add_parser("stop", parents=[common]).set_defaults(fn=cmd_stop)
    sub.add_parser("status", parents=[common]).set_defaults(fn=cmd_status)

    b = sub.add_parser("budget", parents=[common]).add_subparsers(dest="sub", required=True)
    br = b.add_parser("raise", parents=[common])
    br.add_argument("--usd", type=float)
    br.add_argument("--tokens", type=int)
    br.add_argument("--approved-by", default=getpass.getuser())
    br.set_defaults(fn=cmd_budget_raise)
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.fn(args)
    except (CliError, config.ConfigError, ledger.LedgerError, adapter.AdapterError,
            preflight.PreflightError, units.UnitError, verify.VerifyRefused) as e:
        print(f"habeas: {e}", file=sys.stderr)
        return EXIT_ERRORS
    finally:
        while _conns:
            _conns.pop().close()


if __name__ == "__main__":
    sys.exit(main())
