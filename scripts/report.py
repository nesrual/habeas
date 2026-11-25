#!/usr/bin/env python3
"""The tuning report (plan 11), served by `run.sh --report`.

usage: report.py [--repo <dir>] [--json] [--kills]

Reads .audit/metrics.jsonl and .audit/state.json and prints the two numbers
that drive tuning: the useful rate of emissions (target >= 70% by end of v2;
below 50% means tighten the critic and negative examples before any feature
work) and the quarantine composition (real bugs landing there means the
recall hole is too big).

--kills prints the kill-review digest instead: every killed finding with its
actor and rationale, newest first. Review it periodically — recurring GOOD
kills become bullets in references/negative-examples.md; recurring BAD kills
(real defects killed) become prompt-calibration fixes. This is the other
half of the tuning loop.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load(repo: str) -> tuple:
    audit = Path(repo) / ".audit"
    metrics_path = audit / "metrics.jsonl"
    state_path = audit / "state.json"
    if not state_path.exists():
        sys.exit(f"report: no state at {state_path}; nothing to report")
    state = json.loads(state_path.read_text())
    metrics = []
    if metrics_path.exists():
        metrics = [json.loads(l) for l in metrics_path.read_text().splitlines() if l.strip()]
    return metrics, state


def build(repo: str) -> dict:
    metrics, state = load(repo)
    findings = state["findings"]

    funnel = {k: 0 for k in ("candidates", "expert_validated", "critic_killed",
                             "critic_advisory", "promoted", "proved",
                             "quarantined", "emitted")}
    for m in metrics:
        for k in funnel:
            funnel[k] += m.get("counts", {}).get(k, 0)

    kills = {"expert": 0, "critic": 0, "other": 0}
    emitted = {"provable": 0, "advisory": 0}
    quarantine = []
    for fp, e in findings.items():
        if e["status"] == "killed":
            actor = next((h["actor"] for h in reversed(e["history"])
                          if h["to"] == "killed"), "other")
            kills[actor if actor in kills else "other"] += 1
        elif e["status"] == "emitted":
            track = e["record"].get("track", "provable")
            emitted[track if track in emitted else "provable"] += 1
        elif e["status"] == "quarantined":
            proof = e["record"].get("proof") or {}
            quarantine.append({
                "fingerprint": fp,
                "reason": proof.get("reason") or proof.get("result") or "unknown",
                "category": e["record"].get("category"),
                "file": e["record"].get("file"),
                "hypothesis": (e["record"].get("hypothesis") or "")[:100],
            })

    verdicts = {"useful": 0, "noise": 0}
    for m in metrics:
        for v in m.get("human_verdicts", []):
            if v.get("verdict") in verdicts:
                verdicts[v["verdict"]] += 1
    judged = verdicts["useful"] + verdicts["noise"]
    useful_rate = (verdicts["useful"] / judged) if judged else None

    per_run = [{
        "run": m.get("run"), "unit": m.get("unit"),
        "candidates": m.get("counts", {}).get("candidates", 0),
        "emitted": m.get("counts", {}).get("emitted", 0),
        "proof_attempts": m.get("proof_attempts", 0),
        "wallclock_s": sum(m.get("stage_s", {}).values()),
    } for m in metrics]

    return {
        "repo": str(Path(repo).resolve()),
        "runs": len(metrics),
        "funnel": funnel,
        "kills": kills,
        "emitted_by_track": emitted,
        "verdicts": {**verdicts, "unjudged": funnel["emitted"] - judged},
        "useful_rate": useful_rate,
        "quarantine": quarantine,
        "per_run": per_run,
    }


def render(r: dict) -> str:
    lines = [f"habeas report — {r['repo']}", ""]
    f = r["funnel"]
    lines.append(f"runs: {r['runs']}   candidates {f['candidates']} -> validated "
                 f"{f['expert_validated']} -> promoted {f['promoted']} + advisory "
                 f"{f['critic_advisory']} -> proved {f['proved']} -> emitted {f['emitted']}"
                 f" (provable {r['emitted_by_track']['provable']},"
                 f" advisory {r['emitted_by_track']['advisory']})")
    k = r["kills"]
    lines.append(f"kills: expert {k['expert']}, critic {k['critic']}, other {k['other']}"
                 f"   quarantined: {f['quarantined']}")
    v = r["verdicts"]
    lines.append("")
    if r["useful_rate"] is None:
        lines.append(f"USEFUL RATE: n/a — 0 of {f['emitted']} emissions judged."
                     " Judge with: scripts/verdict.sh <issue-id> useful|noise")
    else:
        pct = round(r["useful_rate"] * 100)
        note = "on target" if r["useful_rate"] >= 0.7 else \
               ("below target" if r["useful_rate"] >= 0.5 else
                "BELOW 50% — tighten critic and negative examples before feature work")
        lines.append(f"USEFUL RATE: {pct}% ({v['useful']} useful / {v['noise']} noise,"
                     f" {v['unjudged']} unjudged) — {note} (target >= 70%)")
    lines.append("QUARANTINE COMPOSITION:" if r["quarantine"] else
                 "QUARANTINE COMPOSITION: empty")
    for q in r["quarantine"]:
        lines.append(f"  {q['reason']:36} {q['category']:9} {q['file']}"
                     f" — {q['hypothesis']}")
    if r["quarantine"]:
        lines.append("  (real bugs above mean the recall hole is too big —"
                     " consider pulling the HTTP harness / fix-diff work forward)")
    if r["per_run"]:
        lines.append("")
        lines.append(f"{'run':16} {'unit':44} {'cand':>4} {'emit':>4} {'proofs':>6} {'time':>7}")
        for p in r["per_run"]:
            mins = round(p["wallclock_s"] / 60, 1)
            lines.append(f"{p['run']:16} {(p['unit'] or '')[:44]:44} {p['candidates']:>4}"
                         f" {p['emitted']:>4} {p['proof_attempts']:>6} {mins:>6}m")
    return "\n".join(lines)


def build_kills(repo: str) -> list:
    _metrics, state = load(repo)
    kills = []
    for fp, e in state["findings"].items():
        if e["status"] != "killed":
            continue
        kill = next((h for h in reversed(e["history"]) if h["to"] == "killed"), {})
        r = e["record"]
        kills.append({
            "ts": kill.get("ts", ""), "actor": kill.get("actor", "?"),
            "fingerprint": fp, "category": r.get("category"),
            "file": r.get("file"), "lines": r.get("lines"),
            "hypothesis": (r.get("hypothesis") or "")[:140],
            "rationale": (kill.get("note") or "")[:400],
        })
    kills.sort(key=lambda k: k["ts"], reverse=True)
    return kills


def render_kills(repo: str, kills: list) -> str:
    lines = [f"habeas kill digest — {Path(repo).resolve()}", ""]
    if not kills:
        return lines[0] + "\n\nno killed findings recorded"
    by_actor: dict = {}
    for k in kills:
        by_actor[k["actor"]] = by_actor.get(k["actor"], 0) + 1
    lines.append("kills by actor: "
                 + ", ".join(f"{a}: {n}" for a, n in sorted(by_actor.items())))
    lines.append("")
    for k in kills:
        lines.append(f"[{k['ts'][:10]}] {k['actor']:6} {k['category']:12}"
                     f" {k['file']}:{k.get('lines')}")
        lines.append(f"  claim:     {k['hypothesis']}")
        lines.append(f"  rationale: {k['rationale']}")
        lines.append("")
    lines.append("Recurring good kills -> add to references/negative-examples.md.")
    lines.append("Bad kills (real defects) -> calibrate prompts; do not weaken the mandate.")
    return "\n".join(lines)


def main(argv: list) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--kills", action="store_true")
    args = ap.parse_args(argv[1:])
    if args.kills:
        kills = build_kills(args.repo)
        print(json.dumps(kills, indent=2) if args.json else render_kills(args.repo, kills))
        return 0
    report = build(args.repo)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
