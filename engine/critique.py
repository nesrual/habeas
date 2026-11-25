"""S4 critic panel. A cluster is refuted only when every critic returns
`refute`; a refused or failed critic is `unavailable`, which is not a refute."""

from __future__ import annotations

import json

from . import calls, find, ledger, prompts, schema
from .calls import CallSpec
from .merge import card


def run(conn, cfg, run_dir, args: dict) -> dict:
    run = ledger.run(conn)
    seats = cfg.seats("critic")
    system_t, user_t, version = prompts.load("critic.md")
    system = prompts.fill(system_t, {"schema": json.dumps(schema.CRITIC, indent=1)})
    clusters = conn.execute(
        "SELECT k.*, c.unit_id, c.title, c.invariant, c.violation, c.locations_json, c.trigger,"
        " c.class AS cclass, u.sensitive, u.sha256 FROM clusters k"
        " JOIN candidates c ON c.candidate_id = k.representative_candidate_id"
        " JOIN units u ON u.unit_id = c.unit_id WHERE k.state = 'merged' ORDER BY k.cluster_id").fetchall()
    units = {u["unit_id"]: u for u in conn.execute("SELECT * FROM units")}
    plan = []
    for k in clusters:
        for seat in seats:
            def messages(k=k):
                text, context = find.unit_text(run["repo_path"], units[k["unit_id"]])
                claim = card({**dict(k), "class": k["cclass"]})
                return system, prompts.fill(user_t, {"claim": claim, "unit": text, "context": context})
            skip = None
            if seat.get("non_sensitive_only") and k["sensitive"]:
                skip = "seat is non_sensitive_only and the unit is sensitive"
            plan.append((k, seat, CallSpec(
                role="critic", seat=seat, prompt_version=version, messages=messages,
                subject_hash=f"{k['cluster_id']}|{k['sha256']}", unit_id=k["unit_id"], lens=k["lens"],
                effort=seat.get("effort"), schema=schema.CRITIC, schema_name="critic_verdict",
                skip_reason=skip)))
    s = calls.run_calls(conn, cfg, run_dir, [p[2] for p in plan],
                        retry_errors=bool(args.get("retry_errors")))
    out = {"calls": s, "clusters": len(clusters), "refuted": 0, "upheld": 0, "undecided": 0}
    with conn:
        for k in clusters:
            results = [(seat, calls.result_of(conn, spec, cfg)) for kk, seat, spec in plan if kk is k]
            if any(r is None for _, r in results):
                out["undecided"] += 1      # a call is still pending; decide on resume
                continue
            verdicts = []
            for seat, r in results:
                parsed = r["parsed"]
                v = parsed["verdict"] if parsed else "unavailable"
                reason = parsed["reason"] if parsed else f"{r['status']}: {r['reason'] or ''}"
                verdicts.append(v)
                conn.execute("INSERT OR REPLACE INTO verdicts VALUES (?,?,?,?,?)",
                             (k["cluster_id"], seat["id"], r["call_id"], v, reason))
            refuted = bool(verdicts) and all(v == "refute" for v in verdicts)
            conn.execute("UPDATE clusters SET state = ? WHERE cluster_id = ?",
                         ("refuted" if refuted else "upheld", k["cluster_id"]))
            if refuted:
                conn.execute("INSERT INTO outcomes (cluster_id, outcome, detail) VALUES (?,?,?)",
                             (k["cluster_id"], "refuted", "every critic returned refute"))
            out["refuted" if refuted else "upheld"] += 1
        if not s["pending"]:
            ledger.mark_stage(conn, "critique")
    return out


def lines(s: dict) -> list[str]:
    return [f"critique: {s['clusters']} clusters judged: {s['upheld']} upheld, {s['refuted']} refuted"
            + (f", {s['undecided']} waiting for pending calls" if s["undecided"] else "")]
