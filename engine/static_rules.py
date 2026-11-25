"""S1b: the deterministic `static-rules` seat. Raises candidates from
repo-specific patterns with no model call (config/static_rules.yaml)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import ledger, preflight, rubyindex, schema, units

SEAT = "static-rules"
CODE_VERSION = "1"
CHECKS = ("controller_authorisation", "state_machine_bypass", "write_to_base_class", "cross_engine_model")
CALLBACK_RE = re.compile(r"^\s*(?:prepend_|append_)?(?:before|around|after)_action\b(.*)$")


def _symbols(text: str) -> list[str]:
    out = re.findall(r":(\w+[?!]?)", text)
    for group in re.findall(r"%i\[([^\]]*)\]", text):
        out += group.split()
    return out


def _option(args: str, name: str) -> list[str] | None:
    m = re.search(rf"\b{name}:\s*(\[[^\]]*\]|%i\[[^\]]*\]|:\w+[?!]?)", args)
    return _symbols(m.group(1)) if m else None


def _hit(rule, path, start, end, title, violation, trigger, sketch):
    return {"rule": rule, "path": path, "candidate": {
        "title": title[:120], "class": rule.get("class", "correctness"),
        "invariant": rule["invariant"], "violation": violation,
        "locations": [{"path": path, "start_line": start, "end_line": end}],
        "trigger": trigger, "test_sketch": sketch, "confidence": 0.5}}


def controller_authorisation(rule, index, files, package_of):
    calls = "|".join(re.escape(c) for c in rule.get("authorise_calls") or ["authorize"])
    call_re = re.compile(rf"(?<![\w.])(?:{calls})\b")
    skip = re.compile(rule.get("skip_class_pattern") or r"$^")
    hits = []
    for path in files:
        if not preflight.matches(path, rule.get("paths") or ["**/app/controllers/**/*.rb"]):
            continue
        if rule.get("engines") and package_of(path) not in rule["engines"]:
            continue
        for d in index.by_path.get(path, []):
            short = d["name"].split("::")[-1]
            if d["kind"] != "class" or not short.endswith("Controller") or skip.search(short):
                continue
            chain = [path] + [p for p, _ in index.ancestors(d)]
            chain_methods = {m["name"]: m for p in reversed(chain) for m in rubyindex.methods(index.text[p])}
            guards = []      # (only, except) of every callback that authorises
            for p in chain:
                for line in index.text[p].splitlines():
                    m = CALLBACK_RE.match(line)
                    if not m:
                        continue
                    args = m.group(1)
                    head = re.split(r"\b(?:only|except|if|unless):", args)[0]
                    named = [chain_methods[s] for s in _symbols(head) if s in chain_methods]
                    if call_re.search(head) or any(call_re.search(x["body"]) for x in named):
                        guards.append((_option(args, "only"), _option(args, "except")))
            for meth in rubyindex.methods(index.text[path]):
                if (meth["class_method"] or meth["visibility"] != "public"
                        or not d["line"] < meth["line"] <= d["end_line"] or meth["name"] == "initialize"):
                    continue
                covered = call_re.search(meth["body"]) or any(
                    (only is None or meth["name"] in only) and (exc is None or meth["name"] not in exc)
                    for only, exc in guards)
                if not covered:
                    hits.append(_hit(
                        rule, path, meth["line"], meth["end_line"],
                        f"{short}#{meth['name']} has no authorisation call",
                        f"The action `{meth['name']}` in {d['name']} makes no authorisation call, and no "
                        "callback in the controller or its ancestors authorises it.",
                        f"A request to `{meth['name']}` from an account that does not own the record.",
                        "A request spec where account B calls the action on a record of account A and "
                        "must be denied."))
    return hits


def _state_machine_models(rule, index):
    markers = rule.get("markers") or ["include AASM"]
    out = []
    for path, defs in index.by_path.items():
        if any(m in index.text[path] for m in markers):
            cols = set(rule.get("columns") or ["status"])
            cols |= set(re.findall(r"aasm\W+column:\s*:(\w+)", index.text[path]))
            out += [(path, d, cols) for d in defs if d["kind"] == "class"]
    return out


def state_machine_bypass(rule, index, files, package_of):
    hits = []
    for mpath, d, cols in _state_machine_models(rule, index):
        var = rubyindex.underscore(d["name"])
        col = "|".join(re.escape(c) for c in sorted(cols))
        short = re.escape(d["name"].split("::")[-1])
        recv = rf"(?:@?\w*{var}|self)"
        patterns = [
            re.compile(rf"{recv}\.(?:update!?|update_columns|update_attributes!?|assign_attributes)\b[^#\n]*\b(?:{col}):"),
            re.compile(rf"{recv}\.(?:update_column|update_attribute|write_attribute)\(?\s*:(?:{col})\b"),
            re.compile(rf"{recv}\.(?:{col})\s*=(?!=)"),
            re.compile(rf"\b{short}\b[^#\n]*\.update_all\b[^#\n]*\b(?:{col})\b"),
        ]
        for path in files:
            for n, line in enumerate(index.text.get(path, "").splitlines(), 1):
                if any(p.search(line) for p in patterns):
                    hits.append(_hit(
                        rule, path, n, n,
                        f"Direct write to the state column of {d['name']}",
                        f"`{line.strip()[:160]}` sets the state column of {d['name']} directly. That "
                        f"model has a state machine ({mpath}), so guards and transitions are skipped.",
                        "Call the code when the record is in a state the state machine would not allow "
                        "the change from.",
                        "A test that runs the code twice, or from a disallowed state, and asserts the "
                        "state machine's rule holds."))
    return hits


def write_to_base_class(rule, index, files, package_of):
    base = rule.get("base_class") or "V1ApplicationRecord"
    hits = []
    models = [d for defs in index.by_path.values() for d in defs
              if d["kind"] == "class" and index.descends_from(d, base)]
    for d in models:
        var = rubyindex.underscore(d["name"])
        short = re.escape(d["name"].split("::")[-1])
        patterns = [
            re.compile(rf"\b{short}\.(?:create!?|insert!?|insert_all!?|upsert|upsert_all|update_all|"
                       rf"delete_all|destroy_all|update|destroy|delete|find_or_create_by!?|"
                       rf"create_or_find_by!?|first_or_create!?)\b"),
            re.compile(rf"(?<![\w.])@?\w*{var}\.(?:save!?|update!?|update_columns?|update_attributes?!?|"
                       rf"destroy!?|delete|increment!|decrement!|toggle!|touch)\b"),
        ]
        for path in files:
            for n, line in enumerate(index.text.get(path, "").splitlines(), 1):
                if any(p.search(line) for p in patterns):
                    hits.append(_hit(
                        rule, path, n, n, f"Write to {d['name']}, which inherits from {base}",
                        f"`{line.strip()[:160]}` writes to {d['name']}, a model that inherits from {base}.",
                        "Run the code path that reaches this line.",
                        f"A test that runs the code and asserts no {d['name']} row was created or changed."))
    return hits


def cross_engine_model(rule, index, files, package_of):
    spec = rule.get("model_paths") or "engines/{package}/app/models/**/*.rb"
    rx = re.compile(preflight.glob_re(spec.replace("{package}", "\0")).pattern.replace("\0", "([^/]+)"))
    models = []
    for path, defs in index.by_path.items():
        m = rx.fullmatch(path)
        if m:
            models += [(m.group(1), d["name"]) for d in defs if d["kind"] == "class" and "::" in d["name"]]
    hits = []
    for path in files:
        pkg = package_of(path)
        for n, line in enumerate(index.text.get(path, "").splitlines(), 1):
            for owner, name in models:
                if owner != pkg and re.search(rf"(?<![\w:]){re.escape(name)}\b", line):
                    hits.append({"rule": rule, "path": path, "line": n, "model": name, "owner": owner})
    return hits


def run(conn, cfg, run_dir) -> dict:
    """Run every rule over the unblocked units. Idempotent per unit."""
    doc = cfg.static or {}
    rules = [r for r in doc.get("rules") or [] if r.get("enabled", True)]
    repo = Path(ledger.run(conn)["repo_path"])
    tracked = [p for p in preflight.git(repo, "ls-files", "-z").split("\0") if p]
    index = preflight.build_index(repo, tracked)
    pkg_spec = (cfg.run.get("units") or {}).get("package_from_path")

    def package_of(path):
        return preflight.package_of(path, pkg_spec)

    rows = conn.execute("SELECT * FROM units WHERE blocked_reason IS NULL").fetchall()
    by_path = {}
    for row in rows:
        for seg in units.segments(row["paths_json"]):
            by_path.setdefault(seg["path"], []).append(row)
    files = sorted(by_path)
    version = hashlib.sha256(json.dumps([doc, CODE_VERSION], sort_keys=True).encode()).hexdigest()[:12]
    per_unit: dict[str, list] = {row["unit_id"]: [] for row in rows}
    debt = []
    for rule in rules:
        for hit in globals()[rule["check"]](rule, index, files, package_of):
            if rule.get("output") == "tech_debt":
                debt.append(hit)
                continue
            loc = hit["candidate"]["locations"][0]
            for row in by_path.get(hit["path"], []):
                if units.covers(row["paths_json"], loc):
                    per_unit[row["unit_id"]].append(hit)
    added = 0
    with conn:
        for row in rows:
            key = hashlib.sha256(json.dumps([row["sha256"], SEAT, version]).encode()).hexdigest()
            if conn.execute("SELECT 1 FROM calls WHERE idempotency_key = ?", (key,)).fetchone():
                continue
            call_id = ledger.new_id()
            conn.execute(
                "INSERT INTO calls (call_id, run_id, unit_id, seat_id, role, prompt_version,"
                " requested_model, status, input_tokens, output_tokens, cost_usd, created_at,"
                " idempotency_key) VALUES (?,?,?,?,?,?,?,?,0,0,0,?,?)",
                (call_id, ledger.run(conn)["run_id"], row["unit_id"], SEAT, "static", version,
                 "none", "ok", ledger.now(), key))
            for i, hit in enumerate(per_unit[row["unit_id"]]):
                c = hit["candidate"]
                assert not schema.errors(c, schema.FINDER["properties"]["candidates"]["items"])
                conn.execute(
                    "INSERT INTO candidates (candidate_id, call_id, unit_id, lens, class, title,"
                    " invariant, violation, locations_json, trigger, test_sketch, confidence)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"{call_id}-{i}", call_id, row["unit_id"], hit["rule"]["lens"], c["class"],
                     c["title"], c["invariant"], c["violation"], json.dumps(c["locations"]),
                     c["trigger"], c["test_sketch"], c["confidence"]))
                added += 1
    lines = ["# Tech debt", "",
             "References to another engine's ActiveRecord model from outside that engine.",
             "These are not defect candidates.", ""]
    lines += [f"- `{h['path']}:{h['line']}` references `{h['model']}` (engine `{h['owner']}`)" for h in debt]
    (Path(run_dir) / "tech_debt.md").write_text("\n".join(lines) + "\n")
    return {"static_candidates": added, "tech_debt": len(debt), "rules": len(rules)}
