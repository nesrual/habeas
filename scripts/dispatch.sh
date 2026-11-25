#!/usr/bin/env bash
# dispatch.sh -- run one model role headlessly in a fresh session (plan 7.2).
#
# usage: dispatch.sh <role> <prompt_file> <input_file>
#                    [--cwd <dir>] [--repo <dir>] [--run-dir <dir>]
#
#   role       scout | expert[-*] | prover | critic | proof_review
#   --cwd      where the CLI runs (default: --repo). critic and proof_review
#              MUST point at a linked git worktree: isolation is enforced
#              structurally, not by prompt politeness. issues/ and
#              quarantine.md are deleted inside the worktree first (plan 7.3).
#   --repo     target repo root for config resolution (default: --cwd, else cwd)
#   --run-dir  where raw model output is kept
#              (default: <repo>/.audit/dispatch)
#
# stdout: the extracted JSON value (compact), or on double parse failure
#         {"error":"unparseable","raw_path":"..."} -- caller decides.
# exit:   0 JSON printed (including the unparseable error object)
#         1 usage / config / no usable provider
#         4 isolation violation (critic/proof_review outside a linked worktree)
#
# Decisions noted (plan 14):
# - expert-* and prover roles resolve to providers.expert; provider fallback
#   chain is: configured provider -> scout's provider -> first of
#   claude/codex/grok/pi/generic whose binary is on PATH.
# - models.<role> / effort.<role> pin the model and effort per role (expert-*
#   share models.expert; the prover has models.prover). Templates receive them
#   as {model_args} / {effort_args} (e.g. "--model claude-opus-5-5"), empty
#   when unset. A role with a pinned model never falls back to another
#   provider: model ids are provider-specific, so a fallback would silently
#   run a different model. It fails instead.
# - all roles run read-only (see adapters.conf); model roles only return JSON.
# - hard timeout via python3 (macOS ships no coreutils `timeout`), default
#   600s, override with HABEAS_DISPATCH_TIMEOUT.
# - cold worktrees must share the target git directory and its worktree registration.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"

die() { echo "dispatch.sh: $*" >&2; exit 1; }

[ $# -ge 3 ] || die "usage: dispatch.sh <role> <prompt_file> <input_file> [--cwd d] [--repo d] [--run-dir d]"
role=$1 prompt_file=$2 input_file=$3
shift 3
case "$role" in
  scout|expert|expert-backend|expert-db|expert-security|expert-tests|prover|critic|proof_review) ;;
  *) die "unknown role: $role" ;;
esac

cwd="" repo="" run_dir=""
while [ $# -gt 0 ]; do
  case "$1" in
    --cwd) cwd=$2; shift 2 ;;
    --repo) repo=$2; shift 2 ;;
    --run-dir) run_dir=$2; shift 2 ;;
    *) die "unknown option: $1" ;;
  esac
done
[ -n "$repo" ] || repo="${cwd:-$(pwd)}"
[ -n "$cwd" ] || cwd="$repo"
[ -f "$prompt_file" ] || die "prompt file not found: $prompt_file"
[ -f "$input_file" ] || die "input file not found: $input_file"
[ -d "$cwd" ] || die "cwd not found: $cwd"
[ -n "$run_dir" ] || run_dir="$repo/.audit/dispatch"
python3 "$SCRIPT_DIR/paths.py" audit "$repo" || die "unsafe audit workspace"
mkdir -p "$run_dir"

timeout_s="${HABEAS_DISPATCH_TIMEOUT:-600}"

# --- isolation enforcement for cold roles (plan 7.3) -----------------------
case "$role" in
  critic|proof_review)
    if ! python3 "$SCRIPT_DIR/paths.py" worktree "$repo" "$cwd"; then
      echo "dispatch.sh: role '$role' must run in a linked git worktree (isolation is non-negotiable); got: $cwd" >&2
      exit 4
    fi
    rm -rf "$cwd/issues" "$cwd/quarantine.md"
    ;;
esac

# --- provider resolution ----------------------------------------------------
case "$role" in
  expert*|prover) provider_key="expert" ;;
  *) provider_key="$role" ;;
esac

cfg=$(python3 "$SCRIPT_DIR/state.py" --repo "$repo" config)
config_str() {
  python3 -I -c 'import json,sys
group, key = sys.argv[1].split(".")
v = json.load(sys.stdin)[group].get(key)
if v is not None and not isinstance(v, str):
    sys.exit("expected a string for " + sys.argv[1])
print(v or "")' "$1" <<<"$cfg"
}
config_provider() { config_str "providers.$1"; }

# shellcheck disable=SC1090
source "$SKILL_DIR/adapters.conf"

template_of() {
  case "$1" in
    claude|codex|grok|pi|generic|claude_ro|codex_ro|grok_ro|pi_ro|generic_ro) printf '%s' "${!1-}" ;;
  esac
}

available() { # provider name -> 0 if its template's binary is on PATH
  local tmpl
  tmpl=$(template_of "$1")
  [ -n "$tmpl" ] || return 1
  command -v "${tmpl%% *}" >/dev/null 2>&1
}

case "$role" in
  expert*) model_key="expert" ;;
  *) model_key="$role" ;;
esac
model=$(config_str "models.$model_key")
effort=$(config_str "effort.$model_key")
configured=$(config_provider "$provider_key")
scout_provider=$(config_provider "scout")
provider=""
for p in "$configured" "$scout_provider" claude codex grok pi generic; do
  [ -n "$p" ] || continue
  if available "$p"; then provider=$p; break; fi
done
[ -n "$provider" ] || die "no usable provider CLI found (tried: ${configured:-<none>}, ${scout_provider:-<none>}, claude, codex, grok, pi, generic)"
if [ -n "$configured" ] && [ "$provider" != "$configured" ] && [ -n "$model" ]; then
  die "provider '$configured' for role '$role' is unavailable and models.$model_key is pinned to '$model'; refusing to fall back to '$provider' (it would run a different model). Install '$configured' or set models.$model_key: null"
fi
if [ -n "$configured" ] && [ "$provider" != "$configured" ]; then
  echo "dispatch.sh: provider '$configured' unavailable, falling back to '$provider'" >&2
fi

template=$(template_of "$provider")
case "$role" in
  critic|proof_review)
    ro=$(template_of "${provider}_ro")
    [ -n "$ro" ] && template=$ro
    ;;
esac

# --- build the combined prompt ----------------------------------------------
echo "dispatch.sh: role=$role provider=$provider model=${model:-<provider default>} effort=${effort:-<default>}" >&2
stamp="$(date +%Y%m%d-%H%M%S)-$$"
combined="$run_dir/$role-$stamp.prompt.md"
{
  cat "$prompt_file"
  printf '\n\n---INPUT---\n'
  cat "$input_file"
} > "$combined"

# --- run with hard timeout ---------------------------------------------------
run_cli() { # $1: raw-output file; runs $cmd in $cwd; returns CLI exit code
  local out=$1
  python3 -I - "$template" "$out" "$cwd" "$timeout_s" "$combined" "$input_file" "$model" "$effort" <<'PYTHON'
import os, re, signal, subprocess, sys
from pathlib import Path
import json
template, out_path, cwd, timeout, prompt, data, model, effort = sys.argv[1:]
# Only the operator's adapter template is shell code. Expand placeholders to
# quoted positional parameters, never to text from configuration or paths.
slots = {"prompt_file": '"${1}"', "input_file": '"${2}"',
         "model_args": '--model "${3}"' if model else "",
         "effort_args": '--effort "${4}"' if effort else ""}
slots["codex_effort_args"] = '-c "${5}"' if effort else ""
slots["daybreak_args"] = "-c daybreak=true" if os.getenv("HABEAS_DAYBREAK") == "1" else ""
cmd = re.sub(r"\{(\w+)\}",
             lambda m: slots.get(m[1], m[0]), template)
with open(out_path, "wb") as out, open(out_path + ".err", "wb") as err:
    proc = subprocess.Popen(["bash", "-c", cmd, "habeas", str(Path(prompt).resolve()),
                             str(Path(data).resolve()), model, effort,
                             "model_reasoning_effort=" + json.dumps(effort)],
                            stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                            cwd=cwd, start_new_session=True)
    try:
        sys.exit(proc.wait(timeout=float(timeout)))
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        err.write(b"\ndispatch.sh: hard timeout expired\n")
        sys.exit(124)
PYTHON
}

attempt=1
raw=""
while [ $attempt -le 2 ]; do
  raw="$run_dir/$role-$stamp.attempt$attempt.txt"
  rc=0
  run_cli "$raw" || rc=$?
  if [ "$rc" -eq 0 ] && extracted=$(python3 "$SCRIPT_DIR/json_extract.py" --strict "$raw" 2>/dev/null); then
    printf '%s\n' "$extracted"
    exit 0
  fi
  if [ "$rc" != 0 ] && [ "$rc" != 124 ]; then
    echo "dispatch.sh: $provider exited $rc; diagnostics: $raw.err" >&2
    exit 1
  fi
  if [ $attempt -eq 1 ]; then
    printf '\n\nOutput only the JSON object, no prose, no code fences.\n' >> "$combined"
    [ $rc -eq 124 ] && echo "dispatch.sh: attempt 1 hit the ${timeout_s}s timeout; retrying" >&2
  fi
  attempt=$((attempt + 1))
done

python3 -I -c 'import json,sys; print(json.dumps({"error":"unparseable","raw_path":sys.argv[1]}))' "$raw"
exit 0
