"""S3 merge: candidates join one cluster only when the merger and the merge
auditor both say they share a root cause (plan 9, S3)."""

from __future__ import annotations

import hashlib
import itertools
import json

from . import calls, find, ledger, preflight, prompts, schema
from .calls import CallSpec


def card(c) -> str:
    return json.dumps({"title": c["title"], "class": c["class"], "invariant": c["invariant"],
                       "violation": c["violation"], "locations": json.loads(c["locations_json"]),
                       "trigger": c["trigger"]}, indent=1)


def _linked(a, b, gap: int, across_lenses: bool = False) -> bool:
    if a["lens"] != b["lens"] and not across_lenses:
        return False
    for x in json.loads(a["locations_json"]):
        for y in json.loads(b["locations_json"]):
            if (x["path"] == y["path"] and x["start_line"] <= y["end_line"] + gap
                    and y["start_line"] <= x["end_line"] + gap):
                return True
    return False


def _components(ids: list[str], edges) -> list[list[str]]:
    parent = {i: i for i in ids}

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in edges:
        parent[root(a)] = root(b)
    groups: dict[str, list[str]] = {}
    for i in ids:
        groups.setdefault(root(i), []).append(i)
    return list(groups.values())


def pregroup(cands: list, gap: int = 2, across_lenses: bool = False) -> list[list[str]]:
    """Deterministic: same lens, same file, overlapping or adjacent line ranges.

    The plan's rule is same lens. With merge.across_lenses the lens condition is dropped,
    because one root cause is often raised under several lenses; the merger and the merge
    auditor still both have to agree before two candidates are joined."""
    edges = [(a["candidate_id"], b["candidate_id"])
             for a, b in itertools.combinations(cands, 2) if _linked(a, b, gap, across_lenses)]
    return _components([c["candidate_id"] for c in cands], edges)


def _chunks(group: list[str], by_id: dict, size: int) -> list[list[str]]:
    """A large group in windows of `size`, ordered by place in the code so that reports
    about the same lines land in the same window."""
    def place(i):
        loc = json.loads(by_id[i]["locations_json"])[0]
        return (loc["path"], loc["start_line"], i)
    ordered = sorted(group, key=place)
    return [ordered[i:i + size] for i in range(0, len(ordered), size)]


def run_partition(conn, cfg, run_dir, args, run, cands, by_id, groups, units, seats) -> tuple[dict, list, list]:
    """Sort each pre-group in one call per seat instead of one call per pair.

    Pairwise comparison grows with the square of a group: 2,470 candidates gave 43,792 pairs.
    Here the merger and the merge auditor each sort a whole group; two candidates join only
    when both put them in the same group, which is the plan's rule applied to their answers."""
    size = int(cfg.get("merge.partition_max") or 30)
    system_t, user_t, version = prompts.load("merge_partition.md")
    work = []
    for group in groups:
        if len(group) < 2:
            continue
        for chunk in _chunks(group, by_id, size):
            if len(chunk) < 2:
                continue
            item = {"ids": chunk, "specs": {}}
            unit_ids = list(dict.fromkeys(by_id[i]["unit_id"] for i in chunk))[:4]

            def messages(chunk=chunk, unit_ids=unit_ids):
                reports = "\n\n".join(f"Report {n}:\n{card(by_id[i])}" for n, i in enumerate(chunk, 1))
                code = "\n\n".join(find.unit_text(run["repo_path"], units[u])[0] for u in unit_ids)
                return (prompts.fill(system_t, {"schema": json.dumps(schema.PARTITION, indent=1)}),
                        prompts.fill(user_t, {"reports": reports, "code": code}))
            for role, seat in seats.items():
                if seat is None:
                    continue
                item["specs"][role] = CallSpec(
                    role=role, seat=seat, prompt_version=version, messages=messages,
                    subject_hash=hashlib.sha256("|".join(chunk).encode()).hexdigest(),
                    unit_id=by_id[chunk[0]]["unit_id"], lens=by_id[chunk[0]]["lens"],
                    effort=seat.get("effort"), schema=schema.PARTITION, schema_name="merge_partition")
            work.append(item)
    specs = [s for item in work for s in item["specs"].values()]
    s = calls.run_calls(conn, cfg, run_dir, specs, retry_errors=bool(args.get("retry_errors")))
    if s["by_status"].get("error") and not s["pending"]:
        again = calls.run_calls(conn, cfg, run_dir, specs, retry_errors=True)
        s["executed"] += again["executed"]
        s["by_status"] = {"ok_after_retry": again["by_status"].get("ok", 0),
                          **{k: v for k, v in s["by_status"].items() if k != "error"},
                          "error": again["by_status"].get("error", 0)}
        s.update({k: again[k] for k in ("pending", "state", "budget")})
    rows, edges = [], []
    if s["pending"]:
        return s, rows, edges

    def together(item, role) -> set | None:
        """Pairs this seat put in one group; None when it gave no usable answer."""
        res = calls.result_of(conn, item["specs"][role], cfg) if role in item["specs"] else None
        if not res or res["parsed"] is None:
            return None
        out, seen = set(), set()
        for g in res["parsed"]["groups"]:
            members = [n for n in dict.fromkeys(g["reports"]) if 1 <= n <= len(item["ids"]) and n not in seen]
            seen.update(members)
            out.update(itertools.combinations(sorted(item["ids"][n - 1] for n in members), 2))
        return out
    for item in work:
        m, a = together(item, "merger"), together(item, "merge_auditor")
        for pair in sorted((m or set()) | (a or set())):
            mv = "unavailable" if m is None else "yes" if pair in m else "no"
            av = "unavailable" if a is None else "yes" if pair in a else "no"
            merged = mv == "yes" and av == "yes"
            rows.append((pair[0], pair[1], mv, av, int(merged)))
            if merged:
                edges.append(pair)
    return s, rows, edges


def run(conn, cfg, run_dir, args: dict) -> dict:
    run = ledger.run(conn)
    cands = conn.execute(
        "SELECT * FROM candidates WHERE candidate_id NOT IN (SELECT candidate_id FROM cluster_members)"
        " ORDER BY candidate_id").fetchall()
    by_id = {c["candidate_id"]: c for c in cands}
    groups = pregroup(cands, int(cfg.get("merge.adjacent_lines", 2)), bool(cfg.get("merge.across_lenses", False)))
    units = {u["unit_id"]: u for u in conn.execute("SELECT * FROM units")}
    seats = {role: (cfg.seats(role) or [None])[0] for role in ("merger", "merge_auditor")}
    partition = cfg.get("merge.mode") == "partition"
    if partition:
        s, rows, edges = run_partition(conn, cfg, run_dir, args, run, cands, by_id, groups, units, seats)
        groups = []
    pairs, specs = [], []
    for group in groups:
        for a, b in itertools.combinations(sorted(group), 2):
            pair = {"a": a, "b": b, "specs": {}}
            for role, seat in seats.items():
                if seat is None:
                    continue
                system_t, user_t, version = prompts.load(f"{role}.md")

                def messages(a=a, b=b, system_t=system_t, user_t=user_t):
                    code = "\n\n".join(find.unit_text(run["repo_path"], units[u])[0]
                                       for u in dict.fromkeys([by_id[a]["unit_id"], by_id[b]["unit_id"]]))
                    return (prompts.fill(system_t, {"schema": json.dumps(schema.MERGE, indent=1)}),
                            prompts.fill(user_t, {"a": card(by_id[a]), "b": card(by_id[b]), "code": code}))

                spec = CallSpec(role=role, seat=seat, prompt_version=version, messages=messages,
                                subject_hash=hashlib.sha256(f"{a}|{b}".encode()).hexdigest(),
                                unit_id=by_id[a]["unit_id"], lens=by_id[a]["lens"],
                                effort=seat.get("effort"), schema=schema.MERGE, schema_name="merge_verdict")
                pair["specs"][role] = spec
                specs.append(spec)
            pairs.append(pair)
    if not partition:
        s = calls.run_calls(conn, cfg, run_dir, specs, retry_errors=bool(args.get("retry_errors")))
    if not partition and s["by_status"].get("error") and not s["pending"]:
        # a failed comparison leaves duplicates apart, which costs critic and prover calls: try once more
        again = calls.run_calls(conn, cfg, run_dir, specs, retry_errors=True)
        s["executed"] += again["executed"]
        s["by_status"] = {"ok_after_retry": again["by_status"].get("ok", 0),
                          **{k: v for k, v in s["by_status"].items() if k != "error"},
                          "error": again["by_status"].get("error", 0)}
        s.update({k: again[k] for k in ("pending", "state", "budget")})
    out = {"calls": s, "new_candidates": len(cands), "pairs": len(pairs), "merged_pairs": 0,
           "clusters_created": 0, "known": 0}
    if s["pending"]:
        return out

    def verdict(pair, role):
        res = calls.result_of(conn, pair["specs"][role], cfg) if role in pair["specs"] else None
        if not res or res["parsed"] is None:
            return "unavailable"
        return "yes" if res["parsed"]["same_root_cause"] else "no"

    if not partition:
        edges = []
    with conn:
        if partition:
            out["pairs"] = len(rows)
            for a, b, mv, av, merged in rows:
                conn.execute("INSERT INTO merges VALUES (?,?,?,?,?,?)", (ledger.new_id(), a, b, mv, av, merged))
        for pair in pairs:
            mv, av = verdict(pair, "merger"), verdict(pair, "merge_auditor")
            merged = mv == "yes" and av == "yes"
            conn.execute("INSERT INTO merges VALUES (?,?,?,?,?,?)",
                         (ledger.new_id(), pair["a"], pair["b"], mv, av, int(merged)))
            if merged:
                edges.append((pair["a"], pair["b"]))
        out["merged_pairs"] = len(edges)
        for members in _components(list(by_id), edges):
            rep = max(members, key=lambda i: (by_id[i]["confidence"] or 0, i))
            cluster_id = "c" + hashlib.sha1("|".join(sorted(members)).encode()).hexdigest()[:10]
            locations = [l for i in members for l in json.loads(by_id[i]["locations_json"])]
            known = preflight.baseline_match(conn, locations)
            conn.execute(
                "INSERT INTO clusters (cluster_id, state, lens, class, representative_candidate_id, known_ref)"
                " VALUES (?,?,?,?,?,?)",
                (cluster_id, "known" if known else "merged", by_id[rep]["lens"], by_id[rep]["class"], rep, known))
            for i in members:
                conn.execute("INSERT INTO cluster_members VALUES (?,?)", (cluster_id, i))
            if known:
                conn.execute("INSERT INTO outcomes (cluster_id, outcome, detail) VALUES (?,?,?)",
                             (cluster_id, "known", f"matches baseline entry {known}"))
                out["known"] += 1
            out["clusters_created"] += 1
        ledger.mark_stage(conn, "merge")
    return out


def lines(s: dict) -> list[str]:
    return [f"merge: {s['new_candidates']} new candidates, {s['pairs']} pairs compared, "
            f"{s['merged_pairs']} merged", f"clusters created: {s['clusters_created']} "
            f"({s['known']} match the known-findings baseline)"]
