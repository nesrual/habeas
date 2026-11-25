#!/usr/bin/env python3
"""habeas state store: the ONLY writer of <target-repo>/.audit/state.json.

Subcommands (all output JSON on stdout; errors go to stderr with exit 1):

  init                                  create .audit/state.json skeleton
  run-start --unit <path>               open a run, print its id
  run-end --counts <json>               close the current run with stage counts
  add-candidates [file]                 scout output (JSON array; stdin default)
  transition <fp> <to> --actor A        move a finding through the lifecycle
             [--note N] [--record J] [--issue-id I]
  record-hunks <paths...>               record content hashes of scanned files
  is-scanned <path>                     is this file's current content recorded?
  open-fingerprints                     findings not yet killed/emitted/quarantined
  show <fp>                             dump one finding entry
  config                                dump the merged config as JSON
  config-get <dotted.key>               dump one config value as JSON

Global option: --repo <dir> (default: cwd) -- the TARGET repo, not the skill.

Also home of the restricted-YAML config parser (plan section 5; PyYAML is not
stdlib): two-space indent, scalars, inline and block lists, one nesting level
of maps, null, comments. No anchors, no multiline strings, no lists of maps.

Decisions taken here (plan section 14, decide-and-note):
- `transition --record <json>` merges stage output into the finding record;
  the record is built incrementally through the stages (plan 6.3).
- transition to critic_advisory mechanically demotes: record.track=advisory,
  severity clamped to medium. Advisory-track findings are refused entry to
  critic_survived (they have no provable path).
- add-candidates drops candidates whose file's current content hash is
  already in scanned_hunks (the "already-scanned unchanged" rule, plan 8.2).
- a run-start while another run is open marks the older run "abandoned".
- killed-finding revival after 30 days reuses the finding entry: history is
  preserved and a killed->candidate transition is appended.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fingerprint  # noqa: E402
from paths import audit_workspace, source_path, within

ACTORS = {"scout", "expert", "critic", "prover", "proof_review", "emit"}
CATEGORY_TRACKS = {
    "bug": "provable",
    "security": "provable",
    "improvement": "advisory",
    "test-gap": "advisory",
}
SEVERITY_ORDER = ["low", "medium", "high", "critical"]
ADVISORY_SEVERITY_CAP = "medium"
KILL_SUPPRESS_DAYS = 30
TERMINAL_STATUSES = {"killed", "emitted", "quarantined"}

TRANSITIONS = {
    "candidate": {"expert_validated", "killed"},
    "expert_validated": {"critic_survived", "critic_advisory", "killed"},
    "critic_survived": {"proved", "proof_failed", "killed"},
    "proof_failed": {"proved", "proof_failed", "quarantined", "killed"},
    "critic_advisory": {"emitted", "killed"},
    "proved": {"emitted", "killed"},
    "killed": {"candidate"},  # revival after suppression window only
    "quarantined": set(),
    "emitted": set(),
}

EMPTY_STATE = {"version": 1, "scanned_hunks": {}, "findings": {}, "runs": []}


class StateError(Exception):
    pass


# ---------------------------------------------------------------- utilities

def now_iso() -> str:
    # microsecond resolution: run windows are derived by comparing these
    # strings, and whole-second stamps collide across back-to-back runs
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_ts(ts: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise StateError(f"unparseable timestamp: {ts}")


def content_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def state_path(repo: str) -> Path:
    return within(repo, ".audit/state.json")


def load_state(repo: str) -> dict:
    p = state_path(repo)
    if not p.exists():
        raise StateError(f"no state at {p}; run `state.py init` first")
    return json.loads(p.read_text(encoding="utf-8"))


def save_state(repo: str, state: dict) -> None:
    p = state_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=p.parent, encoding="utf-8", delete=False) as tmp:
        json.dump(state, tmp, indent=2)
        tmp.write("\n")
    os.replace(tmp.name, p)


def _current_run(state: dict) -> dict | None:
    for run in reversed(state["runs"]):
        if "ended" not in run and not run.get("abandoned"):
            return run
    return None


def _resolve(repo: str, path: str) -> tuple[Path, str]:
    """Return (absolute path, repo-relative path) for a file argument."""
    abs_p = source_path(repo, path)
    return abs_p, os.path.relpath(abs_p, Path(repo).resolve())


# ---------------------------------------------------------------- state API

def init_repo(repo: str) -> dict:
    audit_workspace(repo)
    p = state_path(repo)
    if p.exists():
        return {"state": str(p), "created": False}
    save_state(repo, json.loads(json.dumps(EMPTY_STATE)))
    return {"state": str(p), "created": True}


def run_start(repo: str, unit: str) -> dict:
    state = load_state(repo)
    abandoned = []
    for run in state["runs"]:
        if "ended" not in run and not run.get("abandoned"):
            run["abandoned"] = True
            abandoned.append(run["id"])
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    n = sum(1 for r in state["runs"] if r["id"].startswith(f"r-{date}-")) + 1
    run = {"id": f"r-{date}-{n}", "unit": unit, "started": now_iso()}
    state["runs"].append(run)
    save_state(repo, state)
    return {"id": run["id"], "unit": unit, "abandoned": abandoned}


def run_end(repo: str, counts: dict) -> dict:
    state = load_state(repo)
    run = _current_run(state)
    if run is None:
        raise StateError("no run in progress")
    run["ended"] = now_iso()
    run["counts"] = counts
    save_state(repo, state)
    return {"id": run["id"], "counts": counts}


def add_candidates(repo: str, candidates: list, files: set | None = None) -> dict:
    if not isinstance(candidates, list):
        raise StateError("add-candidates input must be a JSON array")
    state = load_state(repo)
    run = _current_run(state)
    rid = run["id"] if run else ""
    added, dropped = [], []

    def drop(cand, reason):
        dropped.append({"id": cand.get("id"), "file": cand.get("file"), "reason": reason})

    allowed = {"id", "category", "file", "lines", "hypothesis", "confidence", "evidence_snippet"}
    for cand in candidates:
        if not isinstance(cand, dict):
            drop({}, "invalid_candidate")
            continue
        missing = [f for f in ("file", "category", "evidence_snippet") if not cand.get(f)]
        if missing:
            drop(cand, f"missing_fields:{','.join(missing)}")
            continue
        if any(not isinstance(cand[k], str) for k in ("category", "file", "evidence_snippet")):
            drop(cand, "invalid_fields")
            continue
        lines = cand.get("lines")
        if not (isinstance(lines, list) and len(lines) == 2
                and all(type(i) is int for i in lines) and 1 <= lines[0] <= lines[1]):
            drop(cand, "invalid_lines")
            continue
        if not isinstance(cand.get("hypothesis", ""), str):
            drop(cand, "invalid_hypothesis")
            continue
        category = cand["category"]
        if category not in CATEGORY_TRACKS:
            drop(cand, f"unknown_category:{category}")
            continue
        try:
            abs_p, rel = _resolve(repo, cand["file"])
            if not abs_p.is_file():
                raise ValueError("not a regular file")
        except (OSError, ValueError, RuntimeError):
            drop(cand, "invalid_file")
            continue
        if files is not None and rel not in files:
            drop(cand, "outside_unit")
            continue
        if content_hash(abs_p) in state["scanned_hunks"]:
            drop(cand, "already_scanned_unchanged")
            continue
        fp = fingerprint.compute(rel, category, cand["evidence_snippet"])
        record = {k: v for k, v in cand.items() if k in allowed}
        record["file"] = rel
        record["fingerprint"] = fp
        record["track"] = CATEGORY_TRACKS[category]
        ts = now_iso()
        entry = state["findings"].get(fp)
        if entry is None:
            state["findings"][fp] = {
                "status": "candidate",
                "issue_id": None,
                "record": record,
                "history": [{"ts": ts, "from": None, "to": "candidate", "actor": "scout", "note": rid}],
            }
            added.append(fp)
        elif entry["status"] == "killed":
            killed_ts = entry.get("killed_ts")
            cutoff = datetime.now(timezone.utc) - timedelta(days=KILL_SUPPRESS_DAYS)
            if killed_ts and _parse_ts(killed_ts) > cutoff:
                drop(cand, "killed_recently")
            else:
                entry["status"] = "candidate"
                entry["record"] = record
                entry.pop("killed_ts", None)
                entry["history"].append({
                    "ts": ts, "from": "killed", "to": "candidate", "actor": "scout",
                    "note": f"revived after {KILL_SUPPRESS_DAYS}d suppression ({rid})",
                })
                added.append(fp)
        else:
            drop(cand, f"duplicate:{entry['status']}")
    save_state(repo, state)
    return {"added": added, "dropped": dropped}


def _clamp_advisory_severity(record: dict) -> None:
    sev = record.get("severity")
    cap = SEVERITY_ORDER.index(ADVISORY_SEVERITY_CAP)
    if sev in SEVERITY_ORDER and SEVERITY_ORDER.index(sev) > cap:
        record["severity"] = ADVISORY_SEVERITY_CAP


def transition(repo: str, fp: str, to: str, actor: str, note: str = "",
               record_patch: dict | None = None, issue_id: str | None = None) -> dict:
    state = load_state(repo)
    entry = state["findings"].get(fp)
    if entry is None:
        raise StateError(f"unknown fingerprint: {fp}")
    if actor not in ACTORS:
        raise StateError(f"unknown actor: {actor} (must be one of {sorted(ACTORS)})")
    frm = entry["status"]
    if to not in TRANSITIONS.get(frm, set()):
        raise StateError(f"illegal transition {frm} -> {to} for {fp}")
    if record_patch is not None:
        if not isinstance(record_patch, dict):
            raise StateError("--record must be a JSON object")
        entry["record"].update(record_patch)
    if to == "critic_survived" and entry["record"].get("track") == "advisory":
        raise StateError(f"advisory-track finding {fp} cannot enter the provable path")
    if to == "critic_advisory":
        entry["record"]["track"] = "advisory"
    if entry["record"].get("track") == "advisory":
        _clamp_advisory_severity(entry["record"])
    if to == "killed":
        entry["killed_ts"] = now_iso()
    if issue_id is not None:
        entry["issue_id"] = issue_id
    entry["status"] = to
    entry["history"].append({"ts": now_iso(), "from": frm, "to": to, "actor": actor, "note": note})
    save_state(repo, state)
    return {"fingerprint": fp, "from": frm, "to": to}


def record_hunks(repo: str, paths: list) -> dict:
    state = load_state(repo)
    run = _current_run(state)
    rid = run["id"] if run else ""
    recorded = []
    for path in paths:
        abs_p, rel = _resolve(repo, path)
        h = content_hash(abs_p)
        state["scanned_hunks"][h] = {"path": rel, "run": rid, "ts": now_iso()}
        recorded.append({"path": rel, "hash": h})
    save_state(repo, state)
    return {"recorded": recorded}


def is_scanned(repo: str, path: str) -> dict:
    state = load_state(repo)
    abs_p, rel = _resolve(repo, path)
    if not abs_p.exists():
        return {"path": rel, "exists": False, "scanned": False}
    return {"path": rel, "exists": True, "scanned": content_hash(abs_p) in state["scanned_hunks"]}


def open_fingerprints(repo: str) -> dict:
    state = load_state(repo)
    return {"open": {fp: e["status"] for fp, e in state["findings"].items()
                     if e["status"] not in TERMINAL_STATUSES}}


def show(repo: str, fp: str) -> dict:
    state = load_state(repo)
    entry = state["findings"].get(fp)
    if entry is None:
        raise StateError(f"unknown fingerprint: {fp}")
    return entry


def annotate(repo: str, fp: str, tracker: dict | None = None,
             title: str | None = None) -> dict:
    """Attach presentation metadata to a finding without a lifecycle
    transition (emitted is terminal; a pushed ticket or a title is metadata,
    not a state change). Tracker info refuses overwrite; a title may be set
    or replaced (it exists to be improved)."""
    state = load_state(repo)
    entry = state["findings"].get(fp)
    if entry is None:
        raise StateError(f"unknown fingerprint: {fp}")
    if tracker is None and title is None:
        raise StateError("annotate needs --tracker and/or --title")
    if tracker is not None:
        if entry.get("tracker"):
            raise StateError(f"finding {fp} already has a tracker ticket: "
                             f"{entry['tracker'].get('key')}")
        if not isinstance(tracker, dict):
            raise StateError("tracker annotation must be a JSON object")
        entry["tracker"] = tracker
    if title is not None:
        entry["record"]["title"] = title
    save_state(repo, state)
    return {"fingerprint": fp, "tracker": entry.get("tracker"),
            "title": entry["record"].get("title")}


# ------------------------------------------------- restricted YAML (plan 5)

def _strip_comment(line: str) -> str:
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _split_top(s: str) -> list:
    parts, buf, quote = [], [], None
    for ch in s:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _scalar(tok: str):
    tok = tok.strip()
    if tok in ("null", "~", ""):
        return None
    if tok == "true":
        return True
    if tok == "false":
        return False
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
        return tok[1:-1]
    if tok.startswith("[") and tok.endswith("]"):
        return [_scalar(t) for t in _split_top(tok[1:-1])]
    try:
        return int(tok)
    except ValueError:
        pass
    try:
        return float(tok)
    except ValueError:
        pass
    return tok


def parse_restricted_yaml(text: str) -> dict:
    """~60-line parser for the config subset (plan section 5). Supports:
    scalars, quoted strings, null, inline [a, b] lists, block lists of
    scalars, and ONE nesting level of maps. Two-space indent only."""
    root: dict = {}
    key0 = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        if line.startswith("\t"):
            raise StateError(f"config line {lineno}: tab indentation not supported")
        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()
        if indent == 0:
            key0 = None
            key, sep, rest = content.partition(":")
            if not sep or content.startswith("- "):
                raise StateError(f"config line {lineno}: expected `key:` at top level")
            key, rest = key.strip(), rest.strip()
            if rest == "":
                root[key] = {}
                key0 = key
            else:
                root[key] = _scalar(rest)
        elif indent == 2:
            if key0 is None:
                raise StateError(f"config line {lineno}: indented line without a parent key")
            if content.startswith("- "):
                if root[key0] == {}:
                    root[key0] = []
                if not isinstance(root[key0], list):
                    raise StateError(f"config line {lineno}: mixed map and list under {key0}")
                root[key0].append(_scalar(content[2:]))
            else:
                if not isinstance(root[key0], dict):
                    raise StateError(f"config line {lineno}: mixed list and map under {key0}")
                key, sep, rest = content.partition(":")
                if not sep or rest.strip() == "":
                    raise StateError(
                        f"config line {lineno}: only one nesting level of maps is supported")
                root[key0][key.strip()] = _scalar(rest.strip())
        else:
            raise StateError(f"config line {lineno}: unsupported indent {indent} (two-space, one level)")
    return root


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(repo: str, skill_dir: str | None = None) -> dict:
    skill = Path(skill_dir) if skill_dir else Path(__file__).resolve().parent.parent
    cfg: dict = {}
    example = skill / "config.example.yaml"
    if example.exists():
        cfg = parse_restricted_yaml(example.read_text(encoding="utf-8"))
    selected = os.environ.get("HABEAS_CONFIG")
    if selected:
        path = Path(selected)
        if not path.is_absolute():
            raise StateError("HABEAS_CONFIG must be an absolute path to a reviewed config")
        override = parse_restricted_yaml(path.read_text(encoding="utf-8"))
        # The OpenAI installation profile historically used the name reasoning.
        if "reasoning" in override and "effort" not in override:
            override["effort"] = override["reasoning"]
        return deep_merge(cfg, override)
    for name in ("config.yaml", ".audit/config.yaml"):
        path = within(repo, name)
        if path.is_file():
            override = parse_restricted_yaml(path.read_text(encoding="utf-8"))
            if any(key in override for key in ("proof", "sandbox", "tracker", "providers",
                                               "models", "effort", "reasoning", "scan", "packs")):
                raise StateError(f"review {path} and select it explicitly with HABEAS_CONFIG")
            cfg = deep_merge(cfg, {k: override[k] for k in ("gates", "scope") if k in override})
            break
    return cfg


def config_get(repo: str, dotted: str, skill_dir: str | None = None):
    value = load_config(repo, skill_dir)
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            raise StateError(f"config key not found: {dotted}")
        value = value[part]
    return value


# ---------------------------------------------------------------------- CLI

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="state.py", description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=".", help="target repo root (default: cwd)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    p = sub.add_parser("run-start")
    p.add_argument("--unit", required=True)
    p = sub.add_parser("run-end")
    p.add_argument("--counts", required=True, help="JSON object of stage counts")
    p = sub.add_parser("add-candidates")
    p.add_argument("--files-from", help="JSON array of files scanned in this unit")
    p.add_argument("file", nargs="?", help="JSON array file (stdin when omitted)")
    p = sub.add_parser("transition")
    p.add_argument("fp")
    p.add_argument("to")
    p.add_argument("--actor", required=True)
    p.add_argument("--note", default="")
    p.add_argument("--record", default=None, help="JSON object merged into the finding record")
    p.add_argument("--issue-id", default=None)
    p = sub.add_parser("record-hunks")
    p.add_argument("paths", nargs="+")
    p = sub.add_parser("is-scanned")
    p.add_argument("path")
    sub.add_parser("open-fingerprints")
    p = sub.add_parser("show")
    p.add_argument("fp")
    p = sub.add_parser("annotate")
    p.add_argument("fp")
    p.add_argument("--tracker", default=None, help="JSON object: ticket key/url/ts")
    p.add_argument("--title", default=None, help="short display title for the finding")
    sub.add_parser("config")
    p = sub.add_parser("config-get")
    p.add_argument("key")

    args = parser.parse_args(argv[1:])
    repo = args.repo
    try:
        if args.cmd == "init":
            result = init_repo(repo)
        elif args.cmd == "run-start":
            result = run_start(repo, args.unit)
        elif args.cmd == "run-end":
            result = run_end(repo, json.loads(args.counts))
        elif args.cmd == "add-candidates":
            if args.file:
                with open(args.file, encoding="utf-8") as f:
                    cands = json.load(f)
            else:
                cands = json.load(sys.stdin)
            files = set(json.loads(Path(args.files_from).read_text())) if args.files_from else None
            result = add_candidates(repo, cands, files)
        elif args.cmd == "transition":
            patch = json.loads(args.record) if args.record else None
            result = transition(repo, args.fp, args.to, args.actor,
                                note=args.note, record_patch=patch, issue_id=args.issue_id)
        elif args.cmd == "record-hunks":
            result = record_hunks(repo, args.paths)
        elif args.cmd == "is-scanned":
            result = is_scanned(repo, args.path)
        elif args.cmd == "open-fingerprints":
            result = open_fingerprints(repo)
        elif args.cmd == "show":
            result = show(repo, args.fp)
        elif args.cmd == "annotate":
            result = annotate(repo, args.fp,
                              tracker=json.loads(args.tracker) if args.tracker else None,
                              title=args.title)
        elif args.cmd == "config":
            result = load_config(repo)
        elif args.cmd == "config-get":
            result = config_get(repo, args.key)
        else:  # pragma: no cover
            raise StateError(f"unknown command {args.cmd}")
    except (StateError, ValueError, OSError) as e:
        print(f"state.py: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
