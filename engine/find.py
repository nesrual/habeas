"""S2 finders: every unblocked unit x every finder seat x runs_per_finder.
Run i uses lens i and effort_schedule[i] (plan 9, S2)."""

from __future__ import annotations

import json

from . import prompts, schema, units
from .calls import CallSpec

def unit_text(repo_path, row) -> tuple[str, str]:
    """(numbered unit text, context text). Fails if the unit changed since preflight."""
    text, sha = units.read(repo_path, row["paths_json"])
    context = json.loads(row["context_json"] or "[]")
    if units.subject_hash(sha, context) != row["sha256"]:
        raise units.UnitError(f"unit {row['unit_id']} changed since preflight")
    return text, "\n\n".join(context) or "(none)"


def plan(conn, cfg, repo_path, engine: str | None = None, max_units: int | None = None,
         roles=("finder",)) -> list[CallSpec]:
    variant = cfg.get("prompt_variant", "invariant")
    system_t, user_t, version = prompts.load(f"finder.{variant}.md")
    q = "SELECT * FROM units WHERE blocked_reason IS NULL"
    args: list = []
    if engine:
        q += " AND package = ?"
        args.append(engine)
    q += " ORDER BY order_rank IS NULL, order_rank, unit_id"
    rows = conn.execute(q, args).fetchall()
    if max_units:
        rows = rows[:max_units]
    lenses = cfg.run["lenses"]
    seats = [s for role in roles for s in cfg.seats(role)]
    specs = []
    for row in rows:
        for seat in seats:
            for i in range(cfg.run["runs_per_finder"]):
                lens = lenses[i]
                sched = seat.get("effort_schedule")
                system = prompts.fill(system_t, {
                    "invariants": "\n".join(f"- {x}" for x in cfg.lenses[lens]["invariants"]),
                    "schema": json.dumps(schema.FINDER, indent=1)})

                def messages(row=row, system=system):
                    text, context = unit_text(repo_path, row)
                    return system, prompts.fill(user_t, {"unit": text, "context": context})

                skip = None
                if seat.get("non_sensitive_only") and row["sensitive"]:
                    skip = "seat is non_sensitive_only and the unit is sensitive"
                specs.append(CallSpec(
                    role="finder", seat=seat, subject_hash=row["sha256"], prompt_version=version,
                    messages=messages, unit_id=row["unit_id"], lens=lens, run_index=i,
                    prompt_variant=variant, effort=sched[i] if sched else seat.get("effort"),
                    schema=schema.FINDER, schema_name="finder_candidates", skip_reason=skip,
                    extra={"paths_json": row["paths_json"], "class": cfg.lenses[lens]["class"]}))
    return specs


def _short(title: str, limit: int = 120) -> str:
    """A long title is cut, not rejected: one wordy title must not cost the whole answer."""
    title = " ".join(str(title).split())
    return title if len(title) <= limit else title[:limit - 3].rstrip() + "..."


def record_candidates(conn, call_id: str, spec: CallSpec, result: dict) -> None:
    """Store the candidates of a usable finder answer. A candidate whose
    locations are not all inside the unit itself is rejected and counted."""
    if result["status"] not in ("ok", "downgraded") or not result.get("parsed"):
        return
    rejected = 0
    for i, c in enumerate(result["parsed"]["candidates"]):
        locs = c["locations"]
        if not locs or not all(units.covers(spec.extra["paths_json"], loc) for loc in locs):
            rejected += 1
            continue
        conn.execute(
            "INSERT INTO candidates (candidate_id, call_id, unit_id, lens, class, title, invariant,"
            " violation, locations_json, trigger, test_sketch, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{call_id}-{i}", call_id, spec.unit_id, spec.lens, c["class"], _short(c["title"]),
             c["invariant"], c["violation"], json.dumps(locs), c["trigger"], c["test_sketch"],
             c["confidence"]))
    if rejected:
        conn.execute("UPDATE calls SET rejected_candidates = ? WHERE call_id = ?", (rejected, call_id))
