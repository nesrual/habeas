#!/usr/bin/env bash
# run.sh -- scripted-mode orchestrator: one full pipeline run (plan 4 / 8).
#
# usage: run.sh --unit <dir-or-file> [--unit <dir-or-file> ...]
#               [--repo <dir>] [--from scout|critic] [--sweeps N]
#        run.sh --report [--repo <dir>] [--json]
#
#   --unit         repeatable: several paths scan as ONE unit (one scout pass,
#                  one run) — use for clusters like a state machine plus its
#                  job and interaction.
#   --from scout   (default) full pipeline: scout -> expert -> critic ->
#                  prove -> emit -> close. Opens a new run.
#   --from critic  back half only, for inline-mode handoff: operates on the
#                  currently open run's expert_validated findings (the host
#                  already ran run-start, scout, and expert). Does not
#                  record-hunks (the host scanned, the host records).
#   --report       print the tuning report (delegates to scripts/report.py).
#
# Every model role goes through dispatch.sh (fresh headless session); critic
# and proof_review run in a throwaway worktree created here and removed on
# exit. Emission order: proved, then critic_advisory, then quarantine.
# Appends one metrics line to .audit/metrics.jsonl (plan 11).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
die() { echo "run.sh: $*" >&2; exit 1; }
log() { echo "run.sh: $*" >&2; }

if [ "${1:-}" = "--report" ]; then
  shift
  exec python3 "$SCRIPT_DIR/report.py" "$@"
fi

units=() repo="" from="scout" sweeps=""
while [ $# -gt 0 ]; do
  case "$1" in
    --unit) units+=("$2"); shift 2 ;;
    --repo) repo=$2; shift 2 ;;
    --from) from=$2; shift 2 ;;
    --sweeps) sweeps=$2; shift 2 ;;
    *) die "unknown option: $1" ;;
  esac
done
[ ${#units[@]} -gt 0 ] || die "--unit is required (repeatable)"
unit=$(IFS=,; echo "${units[*]}")
units_json=$(python3 -I -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${units[@]}")
[ -n "$repo" ] || repo=$(pwd)
repo=$(cd "$repo" && pwd -P)
case "$from" in scout|critic) ;; *) die "--from must be scout or critic" ;; esac

STATE() { python3 "$SCRIPT_DIR/state.py" --repo "$repo" "$@"; }
jval() { # jval <dotted.key>  (stdin: json) -> raw string / compact json
  python3 -I -c 'import json,sys
v = json.load(sys.stdin)
for k in sys.argv[1].split("."):
    v = v.get(k) if isinstance(v, dict) else None
print("" if v is None else (v if isinstance(v, str) else json.dumps(v)))' "$1"
}
by_status() {
  python3 -I -c 'import json,sys
st = json.load(open(sys.argv[1]))
for fp, e in st["findings"].items():
    if e["status"] == sys.argv[2]:
        print(fp)' "$repo/.audit/state.json" "$1"
}

CFG=$(STATE config)
[ -n "$sweeps" ] || sweeps=$(jval scan.scout_sweeps <<<"$CFG")
[ -n "$sweeps" ] || sweeps=1
MAX_CANDIDATES=$(jval gates.max_candidates_per_unit <<<"$CFG")
UNIT_MAX_LINES=$(jval scan.unit_max_lines <<<"$CFG")
ATTEMPTS_MAX=$(jval proof.attempts_max <<<"$CFG")
REPAIR_FIXES=$(jval proof.repair_fixes <<<"$CFG")
[ -n "$REPAIR_FIXES" ] || REPAIR_FIXES=security_only
PARALLEL=$(jval scan.parallel <<<"$CFG")
case "$PARALLEL" in ''|*[!0-9]*|0) PARALLEL=4 ;; esac
EXCLUDES=$(jval scope.exclude <<<"$CFG")

STATE init > /dev/null
if [ "$from" = "scout" ]; then
  RUN_ID=$(STATE run-start --unit "$unit" | jval id)
else
  RUN_ID=$(python3 -I -c 'import json,sys
st = json.load(open(sys.argv[1]))
for r in reversed(st["runs"]):
    if "ended" not in r and not r.get("abandoned"):
        print(r["id"]); break' "$repo/.audit/state.json")
  [ -n "$RUN_ID" ] || die "--from critic needs an open run (run-start first)"
fi
RUN_DIR="$repo/.audit/runs/$RUN_ID"
mkdir -p "$RUN_DIR"
log "run $RUN_ID unit=$unit from=$from"

WT=""
cleanup() { [ -n "$WT" ] && git -C "$repo" worktree remove --force "$WT" >/dev/null 2>&1 || true; }
trap cleanup EXIT

declare -i d_scout=0 d_expert=0 d_critic=0 d_prover=0 d_review=0 proof_attempts=0
t_stage=$SECONDS
s_scout=0 s_expert=0 s_critic=0 s_prove=0 s_emit=0
stage_done() { printf -v "s_$1" %s "$((SECONDS - t_stage))"; t_stage=$SECONDS; }

assemble() { # assemble <prompt-name> <categories>
  python3 "$SCRIPT_DIR/assemble_prompt.py" "$SKILL_DIR/prompts/$1.md" --categories "$2"
}

# ------------------------------------------------------------------ scout
UNIT_FILES="[]"
if [ "$from" = "scout" ]; then
  files_json=$(python3 -I - "$repo" "$units_json" "$EXCLUDES" "$UNIT_MAX_LINES" "$SCRIPT_DIR" <<'PY'
import json, os, sys
repo, units, excludes, cap = sys.argv[1], json.loads(sys.argv[2]), json.loads(sys.argv[3]), int(sys.argv[4])
sys.path.insert(0, sys.argv[5])
from paths import source_path
paths = []
for unit in units:
    base = source_path(repo, unit)
    if os.path.isfile(base):
        paths.append(os.path.relpath(base, repo))
    else:
        for root, dirs, names in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d.casefold() not in {".git", ".audit"})
            for n in sorted(names):
                paths.append(os.path.relpath(os.path.join(root, n), repo))
paths = list(dict.fromkeys(paths))
def excluded(rel):
    return any(rel == e or rel.startswith(e.rstrip("/") + "/") or rel.startswith(e)
               for e in excludes)
sel, deferred, total = [], [], 0
for rel in sorted(p for p in paths if not excluded(p)):
    try:
        path = source_path(repo, rel)
        if not path.is_file():
            continue
        n = len(open(path, encoding="utf-8").read().splitlines())
    except (ValueError, OSError, RuntimeError):
        continue  # binary or unreadable
    if sel and total + n > cap:
        deferred.append(rel)
        continue
    sel.append(rel)
    total += n
print(json.dumps({"files": sel, "deferred": deferred, "lines": total}))
PY
)
  UNIT_FILES=$(jval files <<<"$files_json")
  [ "$UNIT_FILES" != "[]" ] || die "unit has no scannable files: $unit"
  log "scout: $(jval lines <<<"$files_json") lines in $unit (deferred: $(jval deferred <<<"$files_json"))"

  printf '%s\n' "$UNIT_FILES" > "$RUN_DIR/unit-files.json"
  scout_in="$RUN_DIR/scout-input.txt"
  {
    echo "max_candidates: $MAX_CANDIDATES"
    echo
    python3 -I -c 'import json,sys
for f in json.loads(sys.argv[2]):
    print(f"=== FILE: {f} ===")
    print(open(f"{sys.argv[1]}/{f}", encoding="utf-8").read())' "$repo" "$UNIT_FILES"
  } > "$scout_in"
  assemble scout "bug,security,improvement,test-gap" > "$RUN_DIR/scout-prompt.md"
  # ensemble: N independent scout passes; the fingerprint dedupe in
  # add-candidates unions them (identical snippets collapse), so extra
  # sweeps buy recall — the same unit yields different candidate sets
  # across passes.
  total_added=0 total_dropped=0
  for sweep in $(seq 1 "$sweeps"); do
    scout_out=$("$SCRIPT_DIR/dispatch.sh" scout "$RUN_DIR/scout-prompt.md" "$scout_in" \
                --repo "$repo" --run-dir "$RUN_DIR") ; d_scout+=1
    printf '%s\n' "$scout_out" > "$RUN_DIR/scout-sweep$sweep.json"
    candidates=$(python3 -I -c 'import json,sys
v = json.load(sys.stdin)
cap = int(sys.argv[1])
if not isinstance(v, list):
    sys.exit("scout returned no candidate array; scan incomplete")
print(json.dumps(v[:cap]))' "$MAX_CANDIDATES" <<<"$scout_out")
    added=$(STATE add-candidates --files-from "$RUN_DIR/unit-files.json" <<<"$candidates")
    n_added=$(jval added <<<"$added" | python3 -I -c 'import json,sys;print(len(json.load(sys.stdin)))')
    n_dropped=$(jval dropped <<<"$added" | python3 -I -c 'import json,sys;print(len(json.load(sys.stdin)))')
    total_added=$((total_added + n_added)); total_dropped=$((total_dropped + n_dropped))
  done
  log "scout: sweeps=$sweeps added=$total_added dropped=$total_dropped"
fi
stage_done scout

# ------------------------------------------------------------------ expert
if [ "$from" = "scout" ]; then
  for pass in 1 2; do
    open_cands=$(by_status candidate)
    [ -n "$open_cands" ] || break
    # phase 1: prep + dispatch in parallel chunks (reads only; every state
    # WRITE happens in the serial phase 2 — state.json has a single writer)
    n_running=0
    for fp in $open_cands; do
      (
        STATE show "$fp" | jval record > "$RUN_DIR/$fp.record.json"
        category=$(jval category < "$RUN_DIR/$fp.record.json")
        pack="$RUN_DIR/$fp.pack.md"
        pack_summary=$(python3 "$SCRIPT_DIR/contextpack.py" "$RUN_DIR/$fp.record.json" \
          --out "$pack" --repo "$repo")
        expert_role=$(jval route <<<"$pack_summary")
        [ -f "$SKILL_DIR/prompts/$expert_role.md" ] || expert_role=expert-backend
        assemble "$expert_role" "$category" > "$RUN_DIR/$fp.expert-prompt.md"
        "$SCRIPT_DIR/dispatch.sh" "$expert_role" "$RUN_DIR/$fp.expert-prompt.md" "$pack" \
          --repo "$repo" --run-dir "$RUN_DIR" > "$RUN_DIR/$fp.expert-out.json" \
          || printf '{"error":"dispatch failed"}\n' > "$RUN_DIR/$fp.expert-out.json"
      ) &
      n_running=$((n_running + 1))
      if [ "$n_running" -ge "$PARALLEL" ]; then
        wait || true
        n_running=0
      fi
    done
    wait || true
    # phase 2: apply verdicts serially
    for fp in $open_cands; do
      out=$(cat "$RUN_DIR/$fp.expert-out.json") ; d_expert+=1
      [ "$(jval error <<<"$out")" != "dispatch failed" ] || die "expert provider failed for $fp"
      verdict=$(jval verdict <<<"$out")
      case "$verdict" in
        proceed)
          patch=$(python3 -I -c 'import json,re,sys
v = json.load(sys.stdin)
if v.get("single_concern") is not True or v.get("severity") not in ("low","medium","high","critical"):
    sys.exit(1)
if not isinstance(v.get("failure_scenario"), str) or not v["failure_scenario"].strip():
    sys.exit(1)
if any(v.get(k) is not None and not isinstance(v[k], str) for k in ("title", "fix_sketch")):
    sys.exit(1)
sketch = v.get("proof_sketch")
if sketch is not None:
    if not isinstance(sketch, dict) or sketch.get("type") not in ("rspec","pytest","jest","http"):
        sys.exit(1)
    if sketch["type"] != "http":
        pattern = sketch.get("expected_failure_pattern")
        if not isinstance(pattern, str) or not pattern:
            sys.exit(1)
        re.compile(pattern)
keys = ("title","failure_scenario","proof_sketch","fix_sketch","severity","single_concern")
print(json.dumps({k: v[k] for k in keys if v.get(k) is not None}))' <<<"$out") || {
            STATE transition "$fp" killed --actor expert --note "invalid expert record" > /dev/null
            continue
          }
          STATE transition "$fp" expert_validated --actor expert --record "$patch" > /dev/null ;;
        split)
          STATE transition "$fp" killed --actor expert --note "split into narrower candidates" > /dev/null
          if [ "$pass" = 1 ]; then
            jval split <<<"$out" | python3 -I -c 'import json,sys
items = json.load(sys.stdin); print(json.dumps(items[:int(sys.argv[1])] if isinstance(items,list) else []))' "$MAX_CANDIDATES" \
              | STATE add-candidates --files-from "$RUN_DIR/unit-files.json" > /dev/null || true
          fi ;;
        reject)
          STATE transition "$fp" killed --actor expert \
            --note "expert reject: $(jval reason <<<"$out" | head -c 200)" > /dev/null ;;
        *)
          STATE transition "$fp" killed --actor expert --note "unparseable expert output" > /dev/null ;;
      esac
    done
  done
fi
stage_done expert

# ------------------------------------------------------------------ critic
WT=$(mktemp -d)/wt
git -c core.hooksPath=/dev/null -C "$repo" worktree add --detach "$WT" HEAD > /dev/null 2>&1
claim_card() { # claim_card <fp> -> file path
  local f="$RUN_DIR/$1.claim.json"
  STATE show "$1" | python3 -I -c 'import json,sys
r = json.load(sys.stdin)["record"]
keys = ("file","lines","category","severity","failure_scenario","evidence_snippet")
print(json.dumps({k: r.get(k) for k in keys}))' > "$f"
  echo "$f"
}
validated=$(by_status expert_validated)
n_running=0
for fp in $validated; do
  (
    claim=$(claim_card "$fp")
    category=$(jval category < "$claim")
    assemble critic "$category" > "$RUN_DIR/$fp.critic-prompt.md"
    "$SCRIPT_DIR/dispatch.sh" critic "$RUN_DIR/$fp.critic-prompt.md" "$claim" \
      --cwd "$WT" --repo "$repo" --run-dir "$RUN_DIR" > "$RUN_DIR/$fp.critic-out.json" \
      || printf '{"error":"dispatch failed"}\n' > "$RUN_DIR/$fp.critic-out.json"
  ) &
  n_running=$((n_running + 1))
  if [ "$n_running" -ge "$PARALLEL" ]; then
    wait || true
    n_running=0
  fi
done
wait || true
for fp in $validated; do
  out=$(cat "$RUN_DIR/$fp.critic-out.json") ; d_critic+=1
  [ "$(jval error <<<"$out")" != "dispatch failed" ] || die "critic provider failed for $fp"
  verdict=$(jval verdict <<<"$out")
  critic_patch=$(python3 -I -c 'import json,sys
v = json.load(sys.stdin)
keys = ("verdict","strongest_counterargument","confidence")
print(json.dumps({"critic": {k: v.get(k) for k in keys}}))' <<<"$out" 2>/dev/null \
    || echo '{"critic":{"verdict":"unparseable"}}')
  track=$(STATE show "$fp" | jval record.track)
  case "$verdict" in
    promote)
      if [ "$track" = "advisory" ]; then
        STATE transition "$fp" critic_advisory --actor critic --record "$critic_patch" > /dev/null
      else
        STATE transition "$fp" critic_survived --actor critic --record "$critic_patch" > /dev/null
      fi ;;
    advisory)
      STATE transition "$fp" critic_advisory --actor critic --record "$critic_patch" > /dev/null ;;
    kill)
      STATE transition "$fp" killed --actor critic --record "$critic_patch" \
        --note "critic kill: $(jval strongest_counterargument <<<"$out" | head -c 200)" > /dev/null ;;
    *)
      STATE transition "$fp" killed --actor critic --note "unparseable critic output" > /dev/null ;;
  esac
done
stage_done critic

# ------------------------------------------------------------------ prove
for fp in $(by_status critic_survived); do
  STATE show "$fp" | jval record > "$RUN_DIR/$fp.record.json"
  flagged=$(jval file < "$RUN_DIR/$fp.record.json")
  status=""
  for attempt in $(seq 1 "$ATTEMPTS_MAX"); do
    proof_attempts+=1
    adir="$RUN_DIR/$fp-attempt$attempt"
    mkdir -p "$adir"
    prover_in="$adir/prover-input.md"
    {
      echo "## Finding record"
      cat "$RUN_DIR/$fp.record.json"
      echo
      echo "## Flagged file: $flagged"
      cat "$(python3 "$SCRIPT_DIR/paths.py" source "$repo" "$flagged")"
      if [ "$attempt" -gt 1 ]; then
        prev="$RUN_DIR/$fp-attempt$((attempt-1))"
        echo
        echo "## Previous attempt FAILED. Its result:"
        [ -f "$prev/prove.json" ] && cat "$prev/prove.json"
        if [ -f "$prev/apply.err" ] && [ -s "$prev/apply.err" ]; then
          echo
          echo "## The fix diff failed to apply (git apply said):"
          cat "$prev/apply.err"
        fi
        if [ -f "$prev/run1.log" ]; then
          echo
          echo "## Previous run log:"
          cat "$prev/run1.log"
        fi
      fi
    } > "$prover_in"
    out=$("$SCRIPT_DIR/dispatch.sh" prover "$SKILL_DIR/prompts/prover.md" "$prover_in" \
          --repo "$repo" --run-dir "$RUN_DIR") ; d_prover+=1
    spec="$adir/proof_spec.rb"
    python3 -I -c 'import json,sys
v = json.load(sys.stdin)
s = v.get("spec") if isinstance(v, dict) else None
sys.exit(1) if not s else open(sys.argv[1], "w").write(s)' "$spec" <<<"$out" || {
      STATE transition "$fp" proof_failed --actor prover \
        --note "attempt $attempt: prover output unusable" > /dev/null
      continue
    }
    # a prover-supplied fix diff replaces the expert's (fail-then-pass repair)
    python3 -I -c 'import json,sys
out = json.load(sys.stdin)
fix = out.get("fix")
if isinstance(fix, str) and fix.strip():
    rec = json.load(open(sys.argv[1]))
    rec["fix_sketch"] = fix
    json.dump(rec, open(sys.argv[1], "w"))' "$RUN_DIR/$fp.record.json" <<<"$out" || true
    pr=$("$SCRIPT_DIR/prove.sh" "$RUN_DIR/$fp.record.json" "$spec" \
         --worktree "$WT" --repo "$repo" --log-dir "$adir")
    result=$(jval result <<<"$pr")
    proof_patch=$(python3 -I -c 'import json,sys
p = json.load(sys.stdin)
p["spec_path"] = sys.argv[1]
patch = {"proof": p}
rec = json.load(open(sys.argv[2]))
if rec.get("fix_sketch"):
    patch["fix_sketch"] = rec["fix_sketch"]
print(json.dumps(patch))' "$spec" "$RUN_DIR/$fp.record.json" <<<"$pr")
    if [ "$result" = "red" ]; then
      review_in="$adir/review-input.md"
      {
        echo "## Claim card"
        cat "$(claim_card "$fp")"
        echo
        echo "## Proof spec"
        cat "$spec"
        echo
        echo "## Run log (run 1, red on HEAD)"
        cat "$adir/run1.log"
      } > "$review_in"
      rv=$("$SCRIPT_DIR/dispatch.sh" proof_review "$SKILL_DIR/prompts/proof-review.md" \
           "$review_in" --cwd "$WT" --repo "$repo" --run-dir "$RUN_DIR") ; d_review+=1
      if [ "$(jval verdict <<<"$rv")" = "valid" ]; then
        # optional fail-then-pass repair for non-security findings: the proof
        # is red and valid, but the fix diff did not verify — one prover pass
        # to regenerate the diff, then re-prove with the same spec
        category_r=$(jval category < "$RUN_DIR/$fp.record.json")
        if [ "$REPAIR_FIXES" = "always" ] && [ "$category_r" != "security" ] \
           && [ "$(jval fail_then_pass <<<"$pr")" != "true" ]; then
          rdir="$adir-repair"
          mkdir -p "$rdir"
          {
            echo "## Finding record"
            cat "$RUN_DIR/$fp.record.json"
            echo
            echo "## Flagged file: $flagged"
            cat "$(python3 "$SCRIPT_DIR/paths.py" source "$repo" "$flagged")"
            echo
            echo "## The proof spec below is RED and VALID; only the fix diff failed to verify. prove result:"
            printf '%s\n' "$pr"
            if [ -f "$adir/apply.err" ] && [ -s "$adir/apply.err" ]; then
              echo "## git apply said:"
              cat "$adir/apply.err"
            fi
            echo
            echo "## Keep this exact spec in \"spec\" and output a corrected unified diff in \"fix\":"
            cat "$spec"
          } > "$rdir/prover-input.md"
          rout=$("$SCRIPT_DIR/dispatch.sh" prover "$SKILL_DIR/prompts/prover.md" \
                 "$rdir/prover-input.md" --repo "$repo" --run-dir "$RUN_DIR") ; d_prover+=1
          got_fix=$(python3 -I -c 'import json,sys
out = json.load(sys.stdin)
fix = out.get("fix")
if isinstance(fix, str) and fix.strip():
    rec = json.load(open(sys.argv[1]))
    rec["fix_sketch"] = fix
    json.dump(rec, open(sys.argv[1], "w"))
    print("yes")' "$RUN_DIR/$fp.record.json" <<<"$rout" 2>/dev/null || true)
          if [ "$got_fix" = "yes" ]; then
            pr2=$("$SCRIPT_DIR/prove.sh" "$RUN_DIR/$fp.record.json" "$spec" \
                  --worktree "$WT" --repo "$repo" --log-dir "$rdir")
            if [ "$(jval result <<<"$pr2")" = "red" ] \
               && [ "$(jval fail_then_pass <<<"$pr2")" = "true" ]; then
              pr=$pr2
              proof_patch=$(python3 -I -c 'import json,sys
p = json.load(sys.stdin)
p["spec_path"] = sys.argv[1]
patch = {"proof": p}
rec = json.load(open(sys.argv[2]))
if rec.get("fix_sketch"):
    patch["fix_sketch"] = rec["fix_sketch"]
print(json.dumps(patch))' "$spec" "$RUN_DIR/$fp.record.json" <<<"$pr")
              log "prove: fix repaired for $fp (fail-then-pass now true)"
            fi
          fi
        fi
        full_patch=$(python3 -I -c 'import json,sys
pp = json.loads(sys.argv[1]); pp["proof_review"] = json.load(sys.stdin)
print(json.dumps(pp))' "$proof_patch" <<<"$rv")
        STATE transition "$fp" proved --actor prover --record "$full_patch" > /dev/null
        status=proved
        break
      fi
      STATE transition "$fp" proof_failed --actor proof_review --record "$proof_patch" \
        --note "attempt $attempt: proof-review invalid: $(jval reason <<<"$rv" | head -c 200)" > /dev/null
    elif [ "$result" = "quarantine" ]; then
      STATE transition "$fp" proof_failed --actor prover --record "$proof_patch" \
        --note "$(jval reason <<<"$pr")" > /dev/null
      break
    else
      STATE transition "$fp" proof_failed --actor prover --record "$proof_patch" \
        --note "attempt $attempt: $(jval reason <<<"$pr")" > /dev/null
    fi
  done
done
stage_done prove

# ------------------------------------------------------------------ emit
for group in proved critic_advisory proof_failed; do
  for fp in $(by_status "$group"); do
    if ! python3 "$SCRIPT_DIR/emit.py" "$fp" --repo "$repo" > /dev/null 2> "$RUN_DIR/emit.err"; then
      if grep -q "emission cap" "$RUN_DIR/emit.err"; then
        log "emit: cap reached; remaining findings stay for the next run"
        break 2
      fi
      log "emit: $fp refused: $(cat "$RUN_DIR/emit.err")"
    fi
  done
done
stage_done emit

# --------------------------------------------------- tracker (flag-gated)
tracker_kind=$(jval tracker.kind <<<"$CFG")
if [ -n "$tracker_kind" ]; then
  if tracker_out=$(python3 "$SCRIPT_DIR/tracker.py" push --repo "$repo" 2>&1); then
    log "tracker: $(python3 -I -c 'import json,sys
r = json.load(sys.stdin)
print(f"{len(r[\"pushed\"])} ticket(s) created, {len(r[\"failed\"])} failed")' <<<"$tracker_out" 2>/dev/null || echo "pushed")"
    printf '%s\n' "$tracker_out" > "$RUN_DIR/tracker.json"
  else
    log "tracker: push failed (findings remain local): $(printf '%s' "$tracker_out" | head -2)"
  fi
fi

# ------------------------------------------------------------------ close
if [ "$from" = "scout" ] && [ "$UNIT_FILES" != "[]" ]; then
  python3 -I -c 'import json,sys; sys.stdout.buffer.write(b"".join(
    f.encode() + b"\0" for f in json.loads(sys.argv[1])))' "$UNIT_FILES" \
    | xargs -0 python3 "$SCRIPT_DIR/state.py" --repo "$repo" record-hunks -- > /dev/null
fi
counts=$(python3 -I - "$repo/.audit/state.json" "$RUN_ID" <<'PY'
import json, sys
st = json.load(open(sys.argv[1]))
run = next(r for r in st["runs"] if r["id"] == sys.argv[2])
since = run["started"]
c = {k: 0 for k in ("candidates","expert_validated","critic_killed","critic_advisory",
                    "promoted","proved","quarantined","emitted")}
for e in st["findings"].values():
    for h in e["history"]:
        if h["ts"] < since:
            continue
        to, actor = h["to"], h["actor"]
        if to == "candidate": c["candidates"] += 1
        elif to == "expert_validated": c["expert_validated"] += 1
        elif to == "killed" and actor == "critic": c["critic_killed"] += 1
        elif to == "critic_advisory": c["critic_advisory"] += 1
        elif to == "critic_survived": c["promoted"] += 1
        elif to == "proved": c["proved"] += 1
        elif to == "quarantined": c["quarantined"] += 1
        elif to == "emitted": c["emitted"] += 1
print(json.dumps(c))
PY
)
STATE run-end --counts "$counts" > /dev/null
python3 -I - "$repo/.audit/metrics.jsonl" "$RUN_ID" "$unit" "$counts" \
  "$s_scout" "$s_expert" "$s_critic" "$s_prove" "$s_emit" \
  "$d_scout" "$d_expert" "$d_critic" "$d_prover" "$d_review" "$proof_attempts" <<'PYTHON'
import json, sys
from datetime import datetime, timezone
line = {
    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "run": sys.argv[2], "unit": sys.argv[3], "counts": json.loads(sys.argv[4]),
    "stage_s": dict(zip(("scout", "expert", "critic", "prove", "emit"), map(int, sys.argv[5:10]))),
    "dispatch_calls": dict(zip(("scout", "expert", "critic", "prover", "proof_review"),
                               map(int, sys.argv[10:15]))),
    "proof_attempts": int(sys.argv[15]), "human_verdicts": [],
}
open(sys.argv[1], "a").write(json.dumps(line) + "\n")
PYTHON
log "done: $counts"
echo "$counts"
