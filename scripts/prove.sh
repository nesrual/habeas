#!/usr/bin/env bash
# prove.sh -- execute one proof attempt under the mechanical gate (plan 7.6/9).
#
# usage: prove.sh <finding-json> <proof-file> --worktree <dir>
#                 [--repo <dir>] [--log-dir <dir>]
#
#   finding-json  the finding record (or full state entry); needs fingerprint,
#                 category, proof_sketch.expected_failure_pattern, optionally
#                 fix_sketch (unified diff)
#   proof-file    the materialized RSpec proof spec
#   --worktree    throwaway git worktree to run in (REQUIRED)
#   --repo        main target repo, for config + logs (default: cwd)
#   --log-dir     where run logs land (default: <repo>/.audit/runs/adhoc-<ts>)
#
# stdout: one JSON object:
#   {result: red|failed|quarantine, reason, runs, failure_message,
#    fail_then_pass, network_isolated, log_path[, advisory_note]}
# exit 0 whenever a result JSON is printed (failed proofs are results, not
# errors); nonzero only for usage/config errors.
#
# Gates (references/proof-gate.md is the normative text):
#   A ran at all; B red for the claimed reason; determinism double run;
#   fail-then-pass mandatory for security. All checks live in prove_gates.py.
#
# Decisions noted (plan 14):
# - v1 executes rspec proofs only; proof_sketch.type http always returns
#   {result: quarantine, reason: harness_missing} (execution is v3).
# - nondeterminism is reported as result failed + advisory_note
#   "flaky-behavior"; filing that note as a finding is the orchestrator's job.
# - the scrubbed env only forwards allowlist vars that are actually set
#   (an empty BUNDLE_PATH="" breaks bundler).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
die() { echo "prove.sh: $*" >&2; exit 1; }

[ $# -ge 2 ] || die "usage: prove.sh <finding-json> <proof-file> --worktree <dir> [--repo d] [--log-dir d]"
finding=$1 proof=$2
shift 2
worktree="" repo="" log_dir=""
while [ $# -gt 0 ]; do
  case "$1" in
    --worktree) worktree=$2; shift 2 ;;
    --repo) repo=$2; shift 2 ;;
    --log-dir) log_dir=$2; shift 2 ;;
    *) die "unknown option: $1" ;;
  esac
done
[ -n "$worktree" ] || die "--worktree is required (proofs never run in the main checkout)"
[ -d "$worktree" ] || die "worktree not found: $worktree"
[ -f "$finding" ] || die "finding json not found: $finding"
[ -f "$proof" ] || die "proof file not found: $proof"
[ -n "$repo" ] || repo=$(pwd)
python3 "$SCRIPT_DIR/paths.py" audit "$repo" || die "unsafe audit workspace"
python3 "$SCRIPT_DIR/paths.py" worktree "$repo" "$worktree" \
  || die "proofs require a distinct linked git worktree of the target"
[ -n "$log_dir" ] || log_dir="$repo/.audit/runs/adhoc-$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$log_dir"

# --- config ------------------------------------------------------------------
config_vars=$(python3 "$SCRIPT_DIR/state.py" --repo "$repo" config | python3 -I -c '
import json, shlex, sys
c = json.load(sys.stdin)
def g(path, default=None):
    v = c
    for k in path.split("."):
        v = v.get(k) if isinstance(v, dict) else None
    return default if v is None else v
print("TEST_CMD=%s" % shlex.quote(str(g("proof.test_cmd", "bundle exec rspec {file}"))))
print("SPEC_DIR=%s" % shlex.quote(str(g("proof.spec_dir", "spec/audit"))))
print("TIMEOUT_S=%s" % shlex.quote(str(g("proof.timeout_s", 120))))
print("SBX_MODE=%s" % shlex.quote(str(g("sandbox.mode", "env"))))
print("DB_URL=%s" % shlex.quote(str(g("sandbox.database_url", ""))))
print("IMAGE=%s" % shlex.quote(str(g("sandbox.container_image") or "")))
print("ALLOWLIST=%s" % shlex.quote(",".join(map(str, g("sandbox.env_allowlist", ["PATH","HOME","LANG"])))))
print("FRAMEWORK=%s" % shlex.quote(str(g("proof.framework", "rspec"))))
print("TEST_CMD_JEST=%s" % shlex.quote(str(g("proof.test_cmd_jest", ""))))
print("JEST_DIR=%s" % shlex.quote(str(g("proof.jest_dir", "app/javascript/__audit__"))))
') || die "could not read config"
eval "$config_vars"

# --- finding fields ----------------------------------------------------------
finding_vars=$(python3 "$SCRIPT_DIR/prove_gates.py" finding-info "$finding" \
        --fix-out "$log_dir/fix.patch" --shell) \
  || die "could not read finding record"
eval "$finding_vars"

emit() { # emit <result> <reason|-> <runs-json> <fail_then_pass-json> [advisory]
  P_RESULT=$1 P_REASON=$2 P_RUNS=$3 P_FTP=$4 P_ADVISORY=${5:-} \
  P_LOGDIR=$log_dir P_NET=${NET_ISOLATED:-false} P_FAILMSG_FILE=$log_dir/first_failure.txt \
  python3 -I - <<'PY'
import json, os
msg = ""
try:
    msg = open(os.environ["P_FAILMSG_FILE"], encoding="utf-8").read()
except OSError:
    pass
out = {
    "result": os.environ["P_RESULT"],
    "reason": None if os.environ["P_REASON"] == "-" else os.environ["P_REASON"],
    "runs": json.loads(os.environ["P_RUNS"]),
    "failure_message": msg,
    "fail_then_pass": json.loads(os.environ["P_FTP"]),
    "network_isolated": json.loads(os.environ["P_NET"]),
    "log_path": os.environ["P_LOGDIR"],
}
if os.environ.get("P_ADVISORY"):
    out["advisory_note"] = os.environ["P_ADVISORY"]
print(json.dumps(out))
open(os.path.join(os.environ["P_LOGDIR"], "prove.json"), "w").write(json.dumps(out, indent=2))
PY
  exit 0
}

# --- v1 stub: http proofs quarantine without executing (plan 7.6 step 8) -----
if [ "$PTYPE" = "http" ]; then
  : > "$log_dir/first_failure.txt"
  emit quarantine harness_missing '[]' null
fi

# --- per-finding framework: jest proofs (frontend) ---------------------------
if [ "$PTYPE" = "jest" ]; then
  if [ -z "$TEST_CMD_JEST" ]; then
    : > "$log_dir/first_failure.txt"
    emit quarantine harness_missing '[]' null
  fi
  FRAMEWORK=jest TEST_CMD=$TEST_CMD_JEST SPEC_DIR=$JEST_DIR
fi

# --- sandbox policy (plan 10) -------------------------------------------------
case "$SBX_MODE" in
  env)
    if [ "${HABEAS_ALLOW_HOST_PROOFS:-}" != 1 ]; then
      emit quarantine host_execution_disabled '[]' null
    fi ;;
  container) [ -n "$IMAGE" ] || die "sandbox.mode container requires sandbox.container_image" ;;
  none)
    [ "${HABEAS_I_UNDERSTAND:-}" = "1" ] \
      || die "sandbox.mode none requires HABEAS_I_UNDERSTAND=1 in the environment"
    echo "prove.sh: WARNING: running proof with NO sandbox (mode: none)" >&2 ;;
  *) die "unknown sandbox.mode: $SBX_MODE" ;;
esac

# --- place the spec -----------------------------------------------------------
case "$FRAMEWORK" in
  pytest) spec_rel="$SPEC_DIR/test_audit_${FP}.py" ;;
  jest) spec_rel="$SPEC_DIR/audit_${FP}.test.tsx" ;;
  *) spec_rel="$SPEC_DIR/audit_${FP}_spec.rb" ;;
esac
python3 "$SCRIPT_DIR/paths.py" source "$worktree" "$spec_rel" > /dev/null \
  || die "invalid proof destination"
[ ! -L "$worktree/$spec_rel" ] || die "proof destination is a symlink"
mkdir -p "$worktree/$SPEC_DIR"
cp "$proof" "$worktree/$spec_rel"
# Keep the data path a single shell argument in the operator's command.
printf -v quoted_spec '%q' "$spec_rel"
cmd=${TEST_CMD//\{file\}/$quoted_spec}

run_proof() { # $1: log file -> prints {"exit":N,"network_isolated":bool}
  case "$SBX_MODE" in
    env)
      python3 "$SCRIPT_DIR/prove_gates.py" run --cwd "$worktree" --log "$1" \
        --timeout "$TIMEOUT_S" --scrub --allowlist "$ALLOWLIST" \
        --set "DATABASE_URL=$DB_URL" --set "RAILS_ENV=test" \
        --unshare auto --cmd "$cmd" ;;
    container)
      python3 "$SCRIPT_DIR/prove_gates.py" run --cwd "$worktree" --log "$1" \
        --timeout "$TIMEOUT_S" \
        --container-image "$IMAGE" --cmd "$cmd" ;;
    none)
      python3 "$SCRIPT_DIR/prove_gates.py" run --cwd "$worktree" --log "$1" \
        --timeout "$TIMEOUT_S" --set "RAILS_ENV=test" --cmd "$cmd" ;;
  esac
}

jget() { # jget <json> <key> -> value as compact json
  python3 -I -c 'import json,sys; print(json.dumps(json.load(sys.stdin)[sys.argv[1]]))' "$2" <<<"$1"
}

# --- run 1 --------------------------------------------------------------------
r1=$(run_proof "$log_dir/run1.log")
NET_ISOLATED=$(jget "$r1" network_isolated)
# container mode is network-isolated by construction (--network none); the
# run-level flag only reflects unshare availability (env mode, Linux)
[ "$SBX_MODE" = "container" ] && NET_ISOLATED=true
c1=$(python3 "$SCRIPT_DIR/prove_gates.py" check "$log_dir/run1.log" --pattern "$PATTERN" --framework "$FRAMEWORK")
printf '%s\n' "$c1" > "$log_dir/check1.json"
python3 -I -c 'import json,sys; print(json.load(sys.stdin)["first_failure"], end="")' \
  <<<"$c1" > "$log_dir/first_failure.txt"
if [ "$(jget "$c1" ok)" != "true" ]; then
  reason=$(jget "$c1" reason | tr -d '"')
  if [ "$reason" = zero_failures ] && [ "$(jget "$r1" exit)" != 0 ]; then reason=unexpected_exit; fi
  emit failed "$reason" '["failed"]' null
fi

if [ "$(jget "$r1" exit)" != 1 ]; then
  emit failed unexpected_exit '["failed"]' null
fi

# --- run 2: determinism -------------------------------------------------------
r2=$(run_proof "$log_dir/run2.log")
c2=$(python3 "$SCRIPT_DIR/prove_gates.py" check "$log_dir/run2.log" --pattern "$PATTERN" --framework "$FRAMEWORK")
printf '%s\n' "$c2" > "$log_dir/check2.json"
if [ "$(jget "$c2" ok)" != "true" ]; then
  emit failed nondeterministic '["red","failed"]' null flaky-behavior
fi
if [ "$(jget "$r2" exit)" != 1 ]; then
  emit failed unexpected_exit '["red","failed"]' null
fi
cmp_out=$(python3 "$SCRIPT_DIR/prove_gates.py" compare "$log_dir/run1.log" "$log_dir/run2.log" --framework "$FRAMEWORK")
printf '%s\n' "$cmp_out" > "$log_dir/compare.json"
if [ "$(jget "$cmp_out" deterministic)" != "true" ]; then
  emit failed nondeterministic '["red","red"]' null flaky-behavior
fi

# --- fail-then-pass (mandatory for security) ----------------------------------
FTP=null
if [ "$HAS_FIX" = "1" ]; then
  if python3 "$SCRIPT_DIR/prove_gates.py" check-fix --cwd "$worktree" --file="$FLAGGED" \
      --patch "$log_dir/fix.patch" 2> "$log_dir/apply.err" \
      && git -C "$worktree" apply --whitespace=nowarn "$log_dir/fix.patch" 2> "$log_dir/apply.err"; then
    rfix=$(run_proof "$log_dir/runfix.log")
    cfix=$(python3 "$SCRIPT_DIR/prove_gates.py" check "$log_dir/runfix.log" --green --framework "$FRAMEWORK")
    printf '%s\n' "$cfix" > "$log_dir/checkfix.json"
    if [ "$(jget "$cfix" green)" = "true" ] && [ "$(jget "$rfix" exit)" = 0 ]; then FTP=true; else FTP=false; fi
    git --literal-pathspecs -C "$worktree" restore -- "$FLAGGED"
  else
    FTP='"apply_failed"'
  fi
fi
if [ "$CATEGORY" = "security" ] && [ "$FTP" != "true" ]; then
  emit failed security_requires_fail_then_pass '["red","red"]' "$FTP"
fi

emit red - '["red","red"]' "$FTP"
