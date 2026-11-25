#!/usr/bin/env bash
# verdict.sh -- record a human judgment on an emitted issue (plan 11).
#
# usage: verdict.sh <issue-id> useful|noise [--repo <dir>]
#
# Finds the run that emitted the issue and appends {issue, verdict, ts} to
# that run's line in .audit/metrics.jsonl (rewritten atomically).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
die() { echo "verdict.sh: $*" >&2; exit 1; }

[ $# -ge 2 ] || die "usage: verdict.sh <issue-id> useful|noise [--repo <dir>]"
issue=$1 verdict=$2
shift 2
repo=$(pwd)
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) repo=$2; shift 2 ;;
    *) die "unknown option: $1" ;;
  esac
done
case "$verdict" in useful|noise) ;; *) die "verdict must be useful or noise" ;; esac

python3 -I - "$repo" "$issue" "$verdict" <<'PY'
import json, os, sys
from datetime import datetime, timezone

repo, issue, verdict = sys.argv[1:4]
state = json.load(open(os.path.join(repo, ".audit", "state.json")))
entry = next((e for e in state["findings"].values() if e.get("issue_id") == issue), None)
if entry is None:
    sys.exit(f"verdict.sh: no finding with issue_id {issue}")
run_id = next((h.get("note") for h in reversed(entry["history"]) if h["to"] == "emitted"), "")

metrics_path = os.path.join(repo, ".audit", "metrics.jsonl")
if not os.path.exists(metrics_path):
    sys.exit("verdict.sh: no .audit/metrics.jsonl yet")
lines = [json.loads(l) for l in open(metrics_path) if l.strip()]
target = next((l for l in reversed(lines) if l.get("run") == run_id), None)
if target is None:
    sys.exit(f"verdict.sh: no metrics line for run {run_id}")
target.setdefault("human_verdicts", []).append(
    {"issue": issue, "verdict": verdict,
     "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
tmp = metrics_path + ".tmp"
with open(tmp, "w") as f:
    for l in lines:
        f.write(json.dumps(l) + "\n")
os.replace(tmp, metrics_path)
print(json.dumps({"issue": issue, "verdict": verdict, "run": run_id}))
PY
