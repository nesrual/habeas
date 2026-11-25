"""S8 report: report.md and report.json (plan 9 S8, 12.3)."""

from __future__ import annotations

import itertools
import json
import re
import statistics
from pathlib import Path

from . import calls, find, ledger, reconcile, schema

SURVIVED = ("upheld", "proven", "unproven")


def sync_triage(conn, run_dir: Path) -> None:
    """First version of triage: the reviewer edits `triage_state` in the entry's file."""
    with conn:
        for row in conn.execute("SELECT cluster_id, issue_path FROM outcomes WHERE outcome = 'quarantined'").fetchall():
            path = run_dir / (row["issue_path"] or "")
            if not path.is_file():
                continue
            m = re.search(r'^triage_state:\s*"?(\w+)"?\s*$', path.read_text(encoding="utf-8"), re.M)
            if m and m.group(1) in ("open", "confirmed", "dismissed", "needs_proof"):
                conn.execute("UPDATE outcomes SET triage_state = ? WHERE cluster_id = ?",
                             (m.group(1), row["cluster_id"]))


def _seat_stats(conn, cfg) -> list[dict]:
    raised: dict[str, set] = {}
    for r in conn.execute(
            "SELECT DISTINCT c.seat_id, m.cluster_id FROM cluster_members m JOIN candidates d ON"
            " d.candidate_id = m.candidate_id JOIN calls c ON c.call_id = d.call_id"):
        raised.setdefault(r["seat_id"], set()).add(r["cluster_id"])
    state = {r["cluster_id"]: r["state"] for r in conn.execute("SELECT cluster_id, state FROM clusters")}
    proven_by: dict[str, set] = {}
    for seat, clusters in raised.items():
        for c in clusters:
            if state.get(c) == "proven":
                proven_by.setdefault(c, set()).add(seat)
    out = []
    for r in conn.execute(
            "SELECT seat_id, role, COUNT(*) calls, SUM(status = 'ok') ok, SUM(status = 'refused') refused,"
            " SUM(status = 'downgraded') downgraded, SUM(status = 'error') errors,"
            " SUM(status = 'skipped') skipped, COALESCE(SUM(cost_usd), 0) cost,"
            " COALESCE(SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), 0) tokens"
            " FROM calls GROUP BY seat_id, role ORDER BY role, seat_id"):
        d = dict(r)
        reached = d["calls"] - d["skipped"]
        mine = raised.get(d["seat_id"], set()) if d["role"] in ("finder", "static") else set()
        d.update(
            cost=round(d["cost"], 4),
            refusal_rate=round(d["refused"] / reached, 3) if reached else None,
            downgraded_rate=round(d["downgraded"] / reached, 3) if reached else None,
            candidates=conn.execute("SELECT COUNT(*) FROM candidates d JOIN calls c ON c.call_id = d.call_id"
                                    " WHERE c.seat_id = ? AND c.role = ?", (d["seat_id"], d["role"])).fetchone()[0],
            upheld=sum(1 for c in mine if state.get(c) in SURVIVED),
            proven=sum(1 for c in mine if state.get(c) == "proven"),
            unique_proven=sum(1 for c in mine if proven_by.get(c) == {d["seat_id"]}))
        d["_proven_set"] = {c for c in mine if state.get(c) == "proven"}
        out.append(d)
    return out


def _estimate(seats: list[dict]) -> dict:
    """Chapman estimate of findable bugs from the overlap of each finder pair (plan 12.3)."""
    finders = [s for s in seats if s["role"] == "finder"]
    values = []
    for a, b in itertools.combinations(finders, 2):
        n1, n2 = len(a["_proven_set"]), len(b["_proven_set"])
        m = len(a["_proven_set"] & b["_proven_set"])
        if n1 and n2:
            values.append(round((n1 + 1) * (n2 + 1) / (m + 1) - 1, 1))
    if not values:
        return {"pairs": 0, "note": "not enough proven findings from two finder seats to estimate"}
    return {"pairs": len(values), "median": statistics.median(values), "min": min(values), "max": max(values),
            "note": "a floor: finders share blind spots, so the true total is higher"}


def _coverage(conn, cfg, run) -> dict:
    args = json.loads(run["stage_args_json"] or "{}").get("find", {})
    specs = find.plan(conn, cfg, run["repo_path"], args.get("engine"), args.get("max_units"))
    planned: dict[tuple, set] = {}
    for s in specs:
        planned.setdefault((s.unit_id, s.lens), set()).add(s.seat["id"])
    ok: dict[tuple, set] = {}
    for r in conn.execute("SELECT unit_id, lens, seat_id FROM calls WHERE role = 'finder' AND status = 'ok'"):
        ok.setdefault((r["unit_id"], r["lens"]), set()).add(r["seat_id"])
    per_unit: dict[str, dict] = {}
    histogram: dict[str, int] = {}
    for (unit, lens), seats in sorted(planned.items()):
        n, m = len(ok.get((unit, lens), set()) & seats), len(seats)
        per_unit.setdefault(unit, {})[lens] = {"scanned_by": n, "of": m}
        histogram[f"{n} of {m}"] = histogram.get(f"{n} of {m}", 0) + 1
    full = sum(1 for u in per_unit.values() for c in u.values() if c["scanned_by"] == c["of"])
    total = sum(len(u) for u in per_unit.values())
    blocked = [dict(r) for r in conn.execute("SELECT unit_id, blocked_reason FROM units WHERE blocked_reason IS NOT NULL")]
    scope = conn.execute("SELECT COUNT(*) FROM units WHERE blocked_reason IS NULL").fetchone()[0]
    return {"unit_lens_pairs": total, "fully_scanned": full, "histogram": histogram, "per_unit": per_unit,
            "units_planned": len(per_unit), "units_not_planned": scope - len(per_unit), "blocked_units": blocked,
            "finder_calls_planned": len(specs),
            "finder_calls_pending": len({s.key for s in specs} - calls.finished_keys(conn))}


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? ")


def _hunks(patch_path) -> list[tuple[str, int, int]]:
    """(file, first line, last line) of each hunk of a fix patch."""
    try:
        text = Path(patch_path).read_text(encoding="utf-8", errors="replace")
    except (OSError, TypeError):
        return []
    out, current = [], None
    for line in text.splitlines():
        if line.startswith("--- "):
            current = line[4:].split("\t")[0].removeprefix("a/")
        m = HUNK_RE.match(line)
        if m and current:
            start = int(m.group(1))
            out.append((current, start, start + int(m.group(2) or 1)))
    return out


def fix_groups(proven: list[dict]) -> list[list[str]]:
    """Proven clusters grouped by where their fixes land: two clusters share a group when
    their patches change overlapping lines of the same file. A rough count of distinct defects."""
    hunks = {p["cluster_id"]: _hunks(p.get("fix_patch_path")) for p in proven}
    parent = {c: c for c in hunks}

    def find_root(c):
        while parent[c] != c:
            parent[c] = parent[parent[c]]
            c = parent[c]
        return c
    ids = sorted(hunks)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if any(fa == fb and sa <= eb and sb <= ea for fa, sa, ea in hunks[a] for fb, sb, eb in hunks[b]):
                parent[find_root(a)] = find_root(b)
    groups: dict[str, list[str]] = {}
    for c in ids:
        groups.setdefault(find_root(c), []).append(c)
    return sorted(groups.values(), key=lambda g: (-len(g), g))


def build(conn, cfg, run_dir: Path) -> dict:
    run = ledger.run(conn)
    sync_triage(conn, run_dir)
    seats = _seat_stats(conn, cfg)
    coverage = _coverage(conn, cfg, run)
    counts = reconcile.counts(conn)
    proven = [dict(r) for r in conn.execute(
        "SELECT o.cluster_id, o.issue_path, o.fixed_since, k.severity, k.severity_note, k.lens, c.title,"
        " u.package, (SELECT p.prover_model FROM proofs p WHERE p.cluster_id = o.cluster_id AND"
        " p.status = 'valid' LIMIT 1) prover_model, (SELECT p.fix_patch_path FROM proofs p WHERE"
        " p.cluster_id = o.cluster_id AND p.status = 'valid' LIMIT 1) fix_patch_path"
        " FROM outcomes o JOIN clusters k ON k.cluster_id = o.cluster_id"
        " JOIN candidates c ON c.candidate_id = k.representative_candidate_id"
        " JOIN units u ON u.unit_id = c.unit_id WHERE o.outcome = 'proven' ORDER BY o.cluster_id")]
    groups = fix_groups(proven)
    proofs = [dict(r) for r in conn.execute("SELECT status, duration_s FROM proofs")]
    timed = [p["duration_s"] for p in proofs if p["duration_s"]]
    usd, tokens, n_calls = ledger.spend(conn)
    partial = []
    if coverage["finder_calls_pending"]:
        partial.append(f"{coverage['finder_calls_pending']} planned finder calls did not run")
    if coverage["units_not_planned"]:
        partial.append(f"{coverage['units_not_planned']} units were outside the last `find` (--engine or --max-units)")
    open_clusters = counts["clusters"] - counts["outcomes"]
    if open_clusters:
        partial.append(f"{open_clusters} clusters have no outcome yet")
    if run["state"] != "finished":
        partial.append(f"the run is {run['state']}, not finished")
    by_sev = {s: sum(1 for p in proven if p["severity"] == s) for s in schema.SEVERITIES}
    by_engine: dict[str, int] = {}
    for p in proven:
        by_engine[p["package"] or "(root)"] = by_engine.get(p["package"] or "(root)", 0) + 1
    data = {
        "run": {"run_id": run["run_id"], "mode": run["mode"], "state": run["state"],
                "audited_commit": run["repo_commit"], "started_at": run["started_at"],
                "finished_at": run["finished_at"]},
        "partial": partial,
        "summary": {"proven": len(proven), "proven_by_severity": by_sev, "proven_by_engine": by_engine,
                    "quarantined": counts["by_outcome"].get("quarantined", 0),
                    "coverage": f"{coverage['fully_scanned']} of {coverage['unit_lens_pairs']} unit-lens "
                                "pairs scanned by every planned finder"},
        "counts": {**counts, "cluster_states": dict(conn.execute(
            "SELECT state, COUNT(*) FROM clusters GROUP BY state").fetchall())},
        "seats": [{k: v for k, v in s.items() if not k.startswith("_")} for s in seats],
        "coverage": coverage,
        "estimate_findable": _estimate(seats),
        "proofs": {"attempts": len(proofs),
                   "by_status": {st: sum(1 for p in proofs if p["status"] == st) for st in sorted({p["status"] for p in proofs})},
                   "minutes_per_attempt": round(statistics.mean(timed) / 60, 2) if timed else None},
        "per_unit": {"units": counts["units"],
                     "candidates_per_unit": round(counts["candidates"] / counts["units"], 2) if counts["units"] else None,
                     "cost_per_unit_usd": round(usd / coverage["units_planned"], 4) if coverage["units_planned"] else None},
        "budget": {"spent_usd": round(usd, 4), "spent_tokens": tokens, "limit_usd": run["limit_usd"],
                   "limit_tokens": run["limit_tokens"],
                   "events": [dict(r) for r in conn.execute(
                       "SELECT kind, unit, old_limit, new_limit, approved_by, created_at FROM budget_events"
                       " ORDER BY event_id")]},
        "proven": proven,
        "fix_groups": groups,
        "severity_mismatches": [p["cluster_id"] for p in proven if p["severity_note"]],
        "fixed_since": [p["cluster_id"] for p in proven if p["fixed_since"]],
        "quarantine": [dict(r) for r in conn.execute(
            "SELECT cluster_id, detail, issue_path, triage_state FROM outcomes WHERE outcome = 'quarantined'"
            " ORDER BY cluster_id")],
        "known": [dict(r) for r in conn.execute(
            "SELECT o.cluster_id, k.known_ref FROM outcomes o JOIN clusters k ON k.cluster_id = o.cluster_id"
            " WHERE o.outcome = 'known' ORDER BY o.cluster_id")],
    }
    return data


def markdown(d: dict) -> str:
    s, c, b = d["summary"], d["counts"], d["budget"]
    out = [f"# Audit report: run {d['run']['run_id']}", "",
           f"Commit `{d['run']['audited_commit'][:12]}`, run state **{d['run']['state']}**.", ""]
    if d["partial"]:
        out += ["**This report is partial.**", ""] + [f"- {p}" for p in d["partial"]] + [""]
    out += ["## Summary", "",
            f"- Proven issues: **{s['proven']}** ("
            + ", ".join(f"{n} {sev}" for sev, n in s["proven_by_severity"].items()) + ")",
            "- By engine: " + (", ".join(f"{e} {n}" for e, n in sorted(s["proven_by_engine"].items())) or "none"),
            f"- Quarantined for human triage: **{s['quarantined']}**",
            f"- Coverage: {s['coverage']}",
            f"- Reconciliation: **{'OK' if d['reconcile']['ok'] else 'FAILED'}**"
            + (f" ({len(d['reconcile']['pending'])} items pending)" if d["reconcile"]["pending"] else ""),
            f"- Spend: ${b['spent_usd']:,.2f} of ${b['limit_usd'] or 0:,.2f}, {b['spent_tokens']:,} tokens"
            + (f" of {b['limit_tokens']:,}" if b["limit_tokens"] else ""), ""]
    out += ["## Totals", "",
            f"Units {c['units']}, candidates {c['candidates']}, clusters {c['clusters']}, outcomes {c['outcomes']}.", "",
            "| Outcome | Clusters |", "| --- | --- |"]
    out += [f"| {k} | {v} |" for k, v in sorted(c["by_outcome"].items())]
    out += [""] + [f"- FAIL: {f}" for f in d["reconcile"]["failures"]] + [f"- pending: {p}" for p in d["reconcile"]["pending"]]
    out += ["", "## Proven issues", ""]
    out += [f"- **{p['severity']}** [{p['title']}]({p['issue_path']}) ({p['package'] or 'root'}, {p['lens']})"
            + (" *fixed since the audited commit*" if p["fixed_since"] else "")
            + (f" *severity to confirm: {p['severity_note']}*" if p["severity_note"] else "")
            for p in d["proven"]] or ["None."]
    shared = [g for g in d.get("fix_groups") or [] if len(g) > 1]
    if d["proven"]:
        out += ["", f"The {len(d['proven'])} proven clusters change {len(d.get('fix_groups') or [])} distinct places "
                "in the code. Clusters whose fixes touch the same lines are probably one defect:", ""]
        out += ["- " + ", ".join(g) for g in shared] or ["- none share a fix location"]
    out += ["", "## Quarantine", ""]
    out += [f"- [{q['cluster_id']}]({q['issue_path']}) ({q['triage_state']}): {q['detail']}" for q in d["quarantine"]] or ["None."]
    out += ["", "## Known findings (matched the baseline, not filed again)", ""]
    out += [f"- {k['cluster_id']}: {k['known_ref']}" for k in d["known"]] or ["None."]
    out += ["", "## Seats", "",
            "| Seat | Role | Calls | Refused | Downgraded | Errors | Candidates | Upheld | Proven | Unique proven | Cost |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    out += [f"| {x['seat_id']} | {x['role']} | {x['calls']} | {x['refused']} | {x['downgraded']} | {x['errors']} | "
            f"{x['candidates']} | {x['upheld']} | {x['proven']} | {x['unique_proven']} | ${x['cost']:.2f} |"
            for x in d["seats"]]
    cov = d["coverage"]
    out += ["", "## Coverage", "",
            f"{cov['units_planned']} units planned, {len(cov['blocked_units'])} blocked, "
            f"{cov['finder_calls_pending']} of {cov['finder_calls_planned']} finder calls pending.", "",
            "| Unit-lens pairs | Scanned by |", "| --- | --- |"]
    out += [f"| {n} | {k} finders |" for k, n in sorted(cov["histogram"].items())]
    out += ["", "Per-unit detail is in `report.json` under `coverage.per_unit`.", ""]
    e = d["estimate_findable"]
    out += ["## Estimate of what is left", "",
            (f"Median {e['median']} findable defects (range {e['min']} to {e['max']}, from {e['pairs']} "
             f"finder pairs). This is {e['note']}." if e["pairs"] else e["note"].capitalize() + "."), ""]
    p, u = d["proofs"], d["per_unit"]
    out += ["## Rates", "",
            f"- Proof attempts: {p['attempts']} (" + (", ".join(f"{k} {v}" for k, v in p["by_status"].items()) or "none")
            + f"), minutes per attempt: {p['minutes_per_attempt']}",
            f"- Candidates per unit: {u['candidates_per_unit']}, cost per unit: {u['cost_per_unit_usd']}", "",
            "## Budget", ""]
    out += [f"- {ev['created_at'][:19]} {ev['kind']} ({ev['unit']})"
            + (f": {ev['old_limit']} -> {ev['new_limit']}, approved by {ev['approved_by']}" if ev["kind"] == "raise" else "")
            for ev in b["events"]] or ["No warnings, pauses or raises."]
    return "\n".join(out) + "\n"


def write(conn, cfg, run_dir) -> dict:
    run_dir = Path(run_dir)
    data = build(conn, cfg, run_dir)
    data["reconcile"] = {"ok": True, "failures": [], "pending": []}
    (run_dir / "report.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    r = reconcile.check(conn, cfg, run_dir)       # includes: report counts equal ledger counts
    data["reconcile"] = {"ok": r["ok"], "failures": r["failures"], "pending": r["pending"]}
    (run_dir / "report.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    (run_dir / "report.md").write_text(markdown(data), encoding="utf-8")
    return data
