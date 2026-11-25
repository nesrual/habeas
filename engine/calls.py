"""Idempotent, budget-checked execution of planned model calls (plan 8.2, 8.4).

Worker threads only do HTTP. The calling thread is the single ledger writer:
it records each finished call, and that call's results, in one transaction.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import adapter, ledger

FINISHED = ("ok", "refused", "downgraded", "error", "skipped")


@dataclass
class CallSpec:
    role: str
    seat: dict
    subject_hash: str                  # content hash of what the call is about
    prompt_version: str
    messages: Callable[[], tuple[str, str]] | None = None   # -> (system, user)
    unit_id: str | None = None
    lens: str | None = None
    run_index: int | None = None
    prompt_variant: str | None = None
    effort: str | None = None
    schema: dict | None = None
    schema_name: str = "result"
    skip_reason: str | None = None     # recorded as `skipped`, no model call
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        parts = [self.subject_hash, self.seat["id"], self.role, self.lens, self.run_index,
                 self.prompt_variant, self.prompt_version, self.seat.get("model")]
        return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def fallback_seat(seat: dict, cfg) -> dict | None:
    """The seat with its model swapped for the one that takes over after a refusal or a
    decline: `alternate_model`, else `refusal_fallback.model`. None when there is none."""
    if seat.get("endpoint") == "native":
        return None
    general = (cfg.models.get("refusal_fallback") or {}) if cfg is not None else {}
    if general.get("enabled") is False or seat.get("refusal_fallback") is False:
        return None
    model = seat.get("alternate_model") or (None if seat.get("open_weights") else general.get("model"))
    if not model or model == seat.get("model"):
        return None
    other = {k: v for k, v in seat.items() if k not in ("alternate_model", "canonical_model")}
    other["model"] = model
    return other


def model_lab(model: str | None, cfg) -> str | None:
    """The lab a model belongs to, from the seats that name it (a fallback model keeps the
    seat's `lab` field, which may be another lab's)."""
    if not model:
        return None
    for seats in ((cfg.models.get("seats") or {}) if cfg is not None else {}).values():
        for s in seats or []:
            if isinstance(s, dict) and s.get("model") == model and s.get("lab"):
                return s["lab"]
    return None


def fallback_spec(spec: CallSpec, cfg) -> CallSpec | None:
    """The same call on another model, used when the seat's own model refuses it.

    The model is the seat's `alternate_model` (same lab, so a panel keeps its mix of labs),
    else `refusal_fallback.model` from models.yaml. The call keeps the seat's id, so its
    answer counts for that seat; its own row shows the model that answered and why.
    A seat pinned to approved hosts falls back only to its own alternate."""
    seat = spec.seat
    if spec.extra.get("fallback_for") or spec.skip_reason:
        return None
    other = fallback_seat(seat, cfg)
    if other is None:
        return None
    return CallSpec(role=spec.role, seat=other, subject_hash=spec.subject_hash,
                    prompt_version=spec.prompt_version, messages=spec.messages, unit_id=spec.unit_id,
                    lens=spec.lens, run_index=spec.run_index, prompt_variant=spec.prompt_variant,
                    effort=spec.effort, schema=spec.schema, schema_name=spec.schema_name,
                    extra={**spec.extra, "fallback_for": seat.get("model")})


def result_of(conn, spec: CallSpec, cfg=None) -> dict | None:
    """The finished call for a spec: its row plus `parsed`, or None if not finished.
    When the seat's model refused and a fallback model answered, that answer is returned."""
    row = conn.execute("SELECT * FROM calls WHERE idempotency_key = ?", (spec.key,)).fetchone()
    if row is None:
        return None
    if row["status"] == "refused":
        other = fallback_spec(spec, cfg)
        again = result_of(conn, other) if other else None
        if again is not None:
            return again
    out = dict(row)
    parsed = conn.execute("SELECT result_json FROM call_results WHERE idempotency_key = ?",
                          (spec.key,)).fetchone()
    out["parsed"] = json.loads(parsed[0]) if parsed and row["status"] in ("ok", "downgraded") else None
    return out


def finished_keys(conn, retry_errors: bool = False) -> set[str]:
    q = "SELECT idempotency_key FROM calls"
    if retry_errors:
        q += " WHERE status != 'error'"
    return {r[0] for r in conn.execute(q)}


def _execute(spec: CallSpec, cfg) -> dict:
    try:
        system, user = spec.messages()
        return adapter.complete(spec.seat, cfg.endpoint(spec.seat), cfg.provider(spec.seat),
                                system, user, spec.effort, spec.schema, spec.schema_name)
    except Exception as e:  # recorded, never lost: a failed call is an `error` row
        return {"status": "error", "reason": f"{type(e).__name__}: {e}"[:300], "parsed": None,
                "raw": {"exception": repr(e)}}


def _record(conn, run_dir: Path, spec: CallSpec, result: dict, on_result) -> None:
    call_id = ledger.new_id()
    raw_path = None
    if result.get("raw") is not None:
        raw_path = f"raw/{call_id}.json"
        (run_dir / raw_path).write_text(json.dumps(
            {"call_id": call_id, "seat": spec.seat["id"], "role": spec.role,
             "status": result["status"], "reason": result.get("reason"), **result["raw"]},
            indent=1), encoding="utf-8")
    with conn:
        conn.execute("DELETE FROM calls WHERE idempotency_key = ? AND status = 'error'", (spec.key,))
        conn.execute(
            "INSERT INTO calls (call_id, run_id, unit_id, seat_id, role, lens, run_index,"
            " prompt_variant, prompt_version, requested_model, served_model, served_provider,"
            " status, finish_reason, input_tokens, output_tokens, cost_usd, latency_ms, raw_path,"
            " created_at, idempotency_key, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (call_id, ledger.run(conn)["run_id"], spec.unit_id, spec.seat["id"], spec.role,
             spec.lens, spec.run_index, spec.prompt_variant, spec.prompt_version,
             spec.seat.get("model") or "native", result.get("served_model"),
             result.get("served_provider"), result["status"], result.get("finish_reason"),
             result.get("input_tokens"), result.get("output_tokens"), result.get("cost_usd"),
             result.get("latency_ms"), raw_path, ledger.now(), spec.key, result.get("reason")))
        if result.get("parsed") is not None and spec.role != "finder":
            conn.execute("INSERT OR REPLACE INTO call_results VALUES (?,?,?)",
                         (spec.key, call_id, json.dumps(result["parsed"])))
        if on_result:
            on_result(conn, call_id, spec, result)


def budget_status(conn, cfg, remaining: int = 0) -> dict:
    """Spend against the limits, with a projection for the calls still planned."""
    run = ledger.run(conn)
    usd, tokens, n = ledger.spend(conn)
    warn_pct = cfg.get("budget.warn_at_pct", 80) or 80
    out = {"spent_usd": round(usd, 4), "spent_tokens": tokens,
           "limit_usd": run["limit_usd"], "limit_tokens": run["limit_tokens"],
           "at_limit": [], "warn": [],
           "projected_more_usd": round(usd / n * remaining, 2) if n else None,
           "projected_more_tokens": int(tokens / n * remaining) if n else None}
    for unit, spent, limit in (("usd", usd, run["limit_usd"]), ("tokens", tokens, run["limit_tokens"])):
        if limit is None:
            continue
        if spent >= limit:
            out["at_limit"].append(unit)
        elif spent >= limit * warn_pct / 100:
            out["warn"].append(unit)
    return out


def _warn_once(conn, budget: dict) -> list[str]:
    """Record a warning the first time a limit's warning level is crossed."""
    fresh = []
    for unit in budget["warn"]:
        limit = budget[f"limit_{unit}"]
        seen = conn.execute(
            "SELECT 1 FROM budget_events WHERE kind = 'warning' AND unit = ? AND old_limit = ?",
            (unit, limit)).fetchone()
        if not seen:
            with conn:
                ledger.budget_event(conn, "warning", unit, old=limit)
            fresh.append(unit)
    return fresh


def run_calls(conn, cfg, run_dir, specs: list[CallSpec], on_result=None,
              retry_errors: bool = False) -> dict:
    """Run every spec whose idempotency key is not finished. Pauses at a budget
    limit and stops on a STOP file; calls in flight always finish and are recorded."""
    run_dir = Path(run_dir)
    done = finished_keys(conn, retry_errors)
    seen: set[str] = set()
    todo = []
    refused = {r[0] for r in conn.execute("SELECT idempotency_key FROM calls WHERE status = 'refused'")}
    for s in specs:
        if s.key not in done and s.key not in seen:
            seen.add(s.key)
            todo.append(s)
        elif s.key in refused:
            # refused in an earlier pass: the fallback model still owes an answer
            other = fallback_spec(s, cfg)
            if other and other.key not in done and other.key not in seen:
                seen.add(other.key)
                todo.append(other)
    summary = {"planned": len(specs), "already_done": len(specs) - len(todo), "executed": 0,
               "by_status": {}, "warnings": []}
    for s in {s.seat["id"]: s for s in todo if not s.skip_reason}.values():
        adapter.api_key(cfg.endpoint(s.seat))  # fail before any call, not midway

    def finish(spec, result):
        _record(conn, run_dir, spec, result, on_result)
        summary["executed"] += 1
        summary["by_status"][result["status"]] = summary["by_status"].get(result["status"], 0) + 1

    capacity = {name: int(ep.get("max_concurrency") or 1)
                for name, ep in (cfg.models.get("endpoints") or {}).items()}
    busy = {name: 0 for name in capacity}
    inflight: dict = {}
    halted = None
    stop_file = run_dir / "STOP"
    with ThreadPoolExecutor(max_workers=max(1, sum(capacity.values()))) as pool:
        while todo or inflight:
            if not halted and todo:
                if stop_file.exists():
                    halted = "stopped"
                else:
                    budget = budget_status(conn, cfg, len(todo) + len(inflight))
                    summary["warnings"] += _warn_once(conn, budget)
                    if budget["at_limit"]:
                        halted = "paused_budget"
                        with conn:
                            for unit in budget["at_limit"]:
                                ledger.budget_event(conn, "pause", unit, old=budget[f"limit_{unit}"])
            if halted and not inflight:
                break
            if not halted:
                rest = []
                for spec in todo:
                    ep = spec.seat.get("endpoint")
                    if spec.skip_reason:
                        finish(spec, {"status": "skipped", "reason": spec.skip_reason})
                    elif inflight and budget_would_overrun(conn, cfg, inflight):
                        rest.append(spec)
                    elif busy.get(ep, 0) < capacity.get(ep, 1):
                        busy[ep] = busy.get(ep, 0) + 1
                        inflight[pool.submit(_execute, spec, cfg)] = spec
                    else:
                        rest.append(spec)
                todo = rest
            if not inflight:
                continue
            for fut in wait(inflight, return_when=FIRST_COMPLETED).done:
                spec = inflight.pop(fut)
                busy[spec.seat.get("endpoint")] -= 1
                result = fut.result()
                if spec.extra.get("fallback_for") and result["status"] != "refused":
                    result["reason"] = (f"fallback after refusal by {spec.extra['fallback_for']}"
                                        + (f": {result['reason']}" if result.get("reason") else ""))
                finish(spec, result)
                if result["status"] == "refused":
                    other = fallback_spec(spec, cfg)
                    if other and other.key not in done and other.key not in seen:
                        seen.add(other.key)
                        todo.append(other)
                # an account problem (bad key, no credits) fails every call: stop, do not burn the queue
                if not halted and re.match(r"http_40[12]\b", str(result.get("reason") or "")):
                    halted = "stopped"
                    summary["account_error"] = result["reason"]

    with conn:
        if halted:
            ledger.set_state(conn, halted)
    stop_file.unlink(missing_ok=True)
    summary["pending"] = len(todo)
    summary["state"] = halted or ledger.run(conn)["state"]
    summary["budget"] = budget_status(conn, cfg, len(todo))
    return summary


def budget_would_overrun(conn, cfg, inflight: dict) -> bool:
    """With calls in flight, hold back new ones when the average cost of the calls
    in flight would already reach a limit. Keeps the overshoot to about one call."""
    run = ledger.run(conn)
    usd, tokens, n = ledger.spend(conn)
    if not n:
        return run["limit_usd"] is not None or run["limit_tokens"] is not None
    pending = len(inflight)
    return ((run["limit_usd"] is not None and usd + usd / n * pending >= run["limit_usd"])
            or (run["limit_tokens"] is not None and tokens + tokens / n * pending >= run["limit_tokens"]))


def pid_alive(run_dir) -> bool:
    try:
        pid = int((Path(run_dir) / "stage.pid").read_text())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False
