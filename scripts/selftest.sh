#!/usr/bin/env bash
# selftest.sh -- v1 definition of done (plan 12). Builds the fixture repo,
# runs the FULL pipeline in scripted mode with real model CLIs, and asserts:
#   >= 3 of 5 planted defects emitted as provable issues with proof folders,
#   0 lookalikes emitted,
#   no silent drops (every finding in a terminal or in-flight status with a
#   complete lifecycle history),
#   a second run over the same unit emits 0 duplicates.
#
# usage: selftest.sh [--keep] [--work <dir>]
#   --keep   do not delete the work dir (prints its path for debugging)
#
# Providers: scout/expert use the first installed CLI of claude|codex|grok|pi;
# critic/proof_review use a different family when available. Skips with a
# clear message when no CLI is installed. Expect several minutes of runtime
# and ~20-30 model calls.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"

keep=0 work=""
while [ $# -gt 0 ]; do
  case "$1" in
    --keep) keep=1; shift ;;
    --work) work=$2; shift 2 ;;
    *) echo "selftest.sh: unknown option $1" >&2; exit 1 ;;
  esac
done

installed=()
for c in claude codex grok pi; do
  command -v "$c" > /dev/null 2>&1 && installed+=("$c")
done
if [ ${#installed[@]} -eq 0 ]; then
  echo "selftest.sh: no model CLI installed (claude/codex/grok/pi); skipping."
  exit 0
fi
primary=${installed[0]}
secondary=$primary
for c in "${installed[@]}"; do
  if [ "$c" != "$primary" ]; then secondary=$c; break; fi
done
if [ -n "${HABEAS_CONFIG:-}" ]; then
  echo "selftest: using operator-selected config $HABEAS_CONFIG"
else
  echo "selftest: scout/expert=$primary critic/proof_review=$secondary"
fi

if [ -n "$work" ]; then
  mkdir -p "$work"
  work=$(mktemp -d "$work/habeas-selftest.XXXXXXXX")
else
  work=$(mktemp -d)
fi
cleanup() {
  if [ "$keep" = 1 ]; then
    echo "selftest: work dir kept at $work"
  else
    rm -rf "$work"
  fi
}
trap cleanup EXIT

target="$work/target"
"$SKILL_DIR/fixtures/build_fixture.sh" "$target" > /dev/null
if [ -z "${HABEAS_CONFIG:-}" ]; then
cat > "$target/config.yaml" <<EOF
providers:
  scout: $primary
  expert: $primary
  critic: $secondary
  proof_review: $secondary
models:
  scout: null
  expert: null
  critic: null
  prover: null
  proof_review: null
EOF
export HABEAS_CONFIG="$target/config.yaml"
fi

echo "selftest: run 1 (full pipeline over lib/audit_target)..."
"$SCRIPT_DIR/run.sh" --unit lib/audit_target --repo "$target" > "$work/run1-counts.json"

python3 -I - "$target" <<'PY'
import json, os, re, sys

target = sys.argv[1]
failures = []

PLANTS = {  # file -> (plant number, allowed categories)
    "lib/audit_target/reporting.rb": (1, {"security", "bug"}),
    "lib/audit_target/pagination.rb": (2, {"bug"}),
    "lib/audit_target/transfer.rb": (3, {"security", "bug"}),
    "lib/audit_target/token_auth.rb": (4, {"security"}),
    "lib/audit_target/fees.rb": (5, {"bug"}),
}
LOOKALIKE_SIGNS = [  # (regex over evidence+claim, forbidden categories, label)
    (r"key_id_matches\?", {"bug", "security"}, "d: == on public key ids"),
    (r"same_account\?", {"bug", "security"}, "d: == on public account ids"),
    (r"force_adjust!", {"bug", "security"}, "b: documented admin bypass"),
    (r"legacy_transfer_rows", {"bug", "security"}, "c: bug in dead code"),
    (r"Logging\.info", {"security"}, "a: interpolation into a log"),
]

issues_dir = os.path.join(target, "issues")
issues = []
if os.path.isdir(issues_dir):
    for name in sorted(os.listdir(issues_dir)):
        path = os.path.join(issues_dir, name, "ISSUE.md")
        if os.path.exists(path):
            issues.append((name, open(path).read(), os.path.join(issues_dir, name)))

def fm(text, key):
    m = re.search(rf"^{key}: (.*)$", text, re.M)
    return json.loads(m.group(1)) if m else None

# 1. >= 3 of 5 plants emitted with valid proofs
plants_hit = set()
for name, text, folder in issues:
    if fm(text, "track") != "provable":
        continue
    files = fm(text, "files") or []
    for f in files:
        if f in PLANTS:
            n, cats = PLANTS[f]
            if fm(text, "category") in cats \
               and os.path.exists(os.path.join(folder, "proof_spec.rb")) \
               and os.path.exists(os.path.join(folder, "proof_run.log")):
                plants_hit.add(n)
print(f"selftest: plants emitted with proofs: {sorted(plants_hit)} "
      f"({len(plants_hit)}/5, need >= 3)")
if len(plants_hit) < 3:
    failures.append(f"only {len(plants_hit)} of 5 plants emitted with valid proofs")

# 2. zero lookalikes emitted — match against the Claim section only: a
# finding's failure scenario legitimately MENTIONS neighboring lookalikes
# when distinguishing itself from them (e.g. "unlike key_id_matches?, this
# compares a secret"), and that reasoning must not trip the check.
def claim_of(text):
    m = re.search(r"^## Claim\s*$(.*?)(?=^## )", text, re.M | re.S)
    return m.group(1) if m else text

for name, text, _ in issues:
    category = fm(text, "category")
    claim = claim_of(text)
    for pattern, forbidden, label in LOOKALIKE_SIGNS:
        if category in forbidden and re.search(pattern, claim):
            failures.append(f"lookalike emitted ({label}) in {name}")

# 3./4. no silent drops; complete lifecycle histories
state = json.load(open(os.path.join(target, ".audit", "state.json")))
IN_FLIGHT = {"candidate", "expert_validated", "critic_survived",
             "critic_advisory", "proved", "proof_failed"}
TERMINAL = {"killed", "quarantined", "emitted"}
for fp, e in state["findings"].items():
    if e["status"] not in IN_FLIGHT | TERMINAL:
        failures.append(f"{fp}: unknown status {e['status']}")
    h = e["history"]
    if not h or h[0]["to"] != "candidate" or h[-1]["to"] != e["status"]:
        failures.append(f"{fp}: incomplete lifecycle history")
    for item in h:
        if not all(k in item for k in ("ts", "from", "to", "actor", "note")):
            failures.append(f"{fp}: malformed history entry {item}")
n_term = sum(1 for e in state["findings"].values() if e["status"] in TERMINAL)
print(f"selftest: findings={len(state['findings'])} terminal={n_term} "
      f"in-flight={len(state['findings']) - n_term}, histories complete")

status_of = {fp: e["status"] for fp, e in state["findings"].items()}
# findings that legally emit on the NEXT run (per-run emission cap carry-over)
carryover = sum(1 for s in status_of.values() if s in ("proved", "critic_advisory"))
json.dump({"issues": len(issues), "statuses": status_of, "carryover": carryover},
          open(os.path.join(target, ".audit", "selftest-run1.json"), "w"))

if failures:
    print("selftest: RUN 1 FAILURES:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
PY

count_issues() { find "$target/issues" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' '; }
issues_before=$(count_issues)

echo "selftest: run 2 (same unit; expecting 0 new emissions)..."
"$SCRIPT_DIR/run.sh" --unit lib/audit_target --repo "$target" > "$work/run2-counts.json"

python3 -I - "$target" "$issues_before" "$work/run2-counts.json" <<'PY'
import json, os, re, sys
target, before, counts_path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
counts = json.load(open(counts_path))
run1 = json.load(open(os.path.join(target, ".audit", "selftest-run1.json")))
carryover = run1.get("carryover", 0)
issues_dir = os.path.join(target, "issues")
names = [d for d in os.listdir(issues_dir)
         if os.path.isdir(os.path.join(issues_dir, d))] if os.path.isdir(issues_dir) else []
failures = []
# the dedupe property: run 2 discovers NOTHING new; it may only emit run 1's
# emission-cap carry-over (plan 8: excess findings emit next run)
if counts["candidates"] != 0:
    failures.append(f"run 2 admitted {counts['candidates']} candidates (expected 0)")
if counts["emitted"] != carryover:
    failures.append(f"run 2 emitted {counts['emitted']} (expected carry-over {carryover})")
if len(names) != before + carryover:
    failures.append(f"issue count: {before} -> {len(names)} (expected +{carryover} carry-over)")
fps = []
for name in names:
    text = open(os.path.join(issues_dir, name, "ISSUE.md")).read()
    m = re.search(r"^fingerprint: (.*)$", text, re.M)
    fps.append(json.loads(m.group(1)) if m else name)
if len(fps) != len(set(fps)):
    failures.append("duplicate fingerprints across issue folders")
if failures:
    print("selftest: RUN 2 FAILURES:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"selftest: run 2 clean (issues {before} -> {len(names)}, "
      f"emitted {counts['emitted']} carry-over, 0 new candidates, no duplicates)")
PY

echo "selftest: PASS"
