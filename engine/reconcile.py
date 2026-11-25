"""The conservation check (plan 7). Failures give a non-zero exit. On a run
that is not finished, unfinished work is reported as pending instead."""

from __future__ import annotations

import json
from pathlib import Path

from . import calls, find, ledger

TERMINAL = ("proven", "quarantined", "refuted", "unproven_dropped", "known")


def counts(conn) -> dict:
    one = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
    return {
        "units": one("SELECT COUNT(*) FROM units"),
        "candidates": one("SELECT COUNT(*) FROM candidates"),
        "clusters": one("SELECT COUNT(*) FROM clusters"),
        "outcomes": one("SELECT COUNT(*) FROM outcomes"),
        "by_outcome": dict(conn.execute("SELECT outcome, COUNT(*) FROM outcomes GROUP BY outcome").fetchall()),
    }


def check(conn, cfg, run_dir) -> dict:
    run = ledger.run(conn)
    finished = run["state"] == "finished"
    stages = ledger.stages(conn)
    failures, pending = [], []

    def unfinished(message):
        (failures if finished else pending).append(message)

    args = json.loads(run["stage_args_json"] or "{}").get("find", {})
    planned = {s.key for s in find.plan(conn, cfg, run["repo_path"], args.get("engine"), args.get("max_units"))}
    missing = len(planned - calls.finished_keys(conn))
    if missing:
        unfinished(f"{missing} of {len(planned)} planned finder calls have no row in calls")

    n = conn.execute("SELECT COUNT(*) FROM candidates c WHERE NOT EXISTS"
                     " (SELECT 1 FROM calls WHERE calls.call_id = c.call_id)").fetchone()[0]
    if n:
        failures.append(f"{n} candidates point at a call that is not in the ledger")
    n = conn.execute("SELECT COUNT(*) FROM candidates c WHERE NOT EXISTS"
                     " (SELECT 1 FROM units WHERE units.unit_id = c.unit_id)").fetchone()[0]
    if n:
        failures.append(f"{n} candidates point at a unit that is not in the ledger")

    orphans = conn.execute(
        "SELECT COUNT(*) FROM candidates c WHERE (SELECT COUNT(*) FROM cluster_members m"
        " JOIN clusters k ON k.cluster_id = m.cluster_id"
        " WHERE m.candidate_id = c.candidate_id) != 1").fetchone()[0]
    if orphans:
        message = f"{orphans} candidates are not a member of exactly one cluster"
        (failures if finished or "merge" in stages else pending).append(message)
    n = conn.execute("SELECT COUNT(*) FROM cluster_members m WHERE NOT EXISTS"
                     " (SELECT 1 FROM candidates c WHERE c.candidate_id = m.candidate_id)").fetchone()[0]
    if n:
        failures.append(f"{n} cluster members point at a candidate that is not in the ledger")
    n = conn.execute("SELECT COUNT(*) FROM clusters k WHERE NOT EXISTS"
                     " (SELECT 1 FROM cluster_members m WHERE m.cluster_id = k.cluster_id)").fetchone()[0]
    if n:
        failures.append(f"{n} clusters have no members")

    n = conn.execute("SELECT COUNT(*) FROM clusters k WHERE NOT EXISTS"
                     " (SELECT 1 FROM outcomes o WHERE o.cluster_id = k.cluster_id)").fetchone()[0]
    if n:
        unfinished(f"{n} clusters have no outcome yet")
    n = conn.execute("SELECT COUNT(*) FROM outcomes o WHERE NOT EXISTS"
                     " (SELECT 1 FROM clusters k WHERE k.cluster_id = o.cluster_id)").fetchone()[0]
    if n:
        failures.append(f"{n} outcomes point at a cluster that is not in the ledger")
    n = conn.execute(f"SELECT COUNT(*) FROM outcomes WHERE outcome NOT IN ({','.join('?' * len(TERMINAL))})",
                     TERMINAL).fetchone()[0]
    if n:
        failures.append(f"{n} outcomes are not a named terminal state")
    for row in conn.execute("SELECT cluster_id, issue_path FROM outcomes WHERE outcome = 'proven'"):
        if not row["issue_path"] or not (Path(run_dir) / row["issue_path"]).exists():
            failures.append(f"proven cluster {row['cluster_id']} has no issue file")

    ledger_counts = counts(conn)
    report = Path(run_dir) / "report.json"
    if report.exists():
        reported = json.loads(report.read_text()).get("counts") or {}
        for key in ("candidates", "clusters", "outcomes", "by_outcome"):
            if reported.get(key) != ledger_counts[key]:
                failures.append(f"report.json says {key} = {reported.get(key)}, the ledger says {ledger_counts[key]}")
    elif finished:
        failures.append("the run is finished but report.json does not exist")
    return {"ok": not failures, "state": run["state"], "failures": failures, "pending": pending,
            "counts": ledger_counts}
