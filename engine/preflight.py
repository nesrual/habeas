"""S0 preflight: commit, units, secret scan, sensitivity, scanner hits,
packages, context and the known-findings baseline (plan 9, S0)."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import ledger, rubyindex, units, yamlish


class PreflightError(Exception):
    pass


# ------------------------------------------------------------------- helpers

def glob_re(pattern: str) -> re.Pattern:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out)


def matches(path: str, patterns) -> bool:
    return any(glob_re(p).fullmatch(path) for p in patterns or [])


def git(repo, *args) -> str:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                              text=True).stdout
    except (subprocess.CalledProcessError, OSError) as e:
        detail = getattr(e, "stderr", "") or str(e)
        raise PreflightError(f"git {' '.join(args)} failed in {repo}: {detail.strip()}")


def est_tokens(text: str) -> int:
    """An estimate (about 3.5 characters per token for code); there is no
    tokenizer in the standard library. Used for sizing only."""
    return math.ceil(len(text) / 3.5)


def package_of(path: str, spec: str | None) -> str | None:
    for alt in (spec or "").split("|"):
        if alt:
            m = re.match(re.escape(alt).replace(r"\{package\}", "([^/]+)"), path)
            if m:
                return m.group(1)
    return None


SECRET_RULES = [
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("stripe_live_key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{16,}")),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{24,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("assigned_secret", re.compile(
        r"""(?i)\b\w*(?:secret|passw(?:or)?d|api_?key|access_?key|auth_?token|private_?key)\w*\s*(?:=>|[:=])\s*["'][A-Za-z0-9+/=_\-]{20,}["']""")),
]


def scan_secrets(text: str) -> list[dict]:
    """Built-in secret scan. Reports the rule and line, never the value."""
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for rule, rx in SECRET_RULES:
            if rx.search(line):
                hits.append({"rule": rule, "line": n})
                break
    return hits


def split_file(path: str, lines: list[str], max_tokens: int) -> list[dict]:
    """Segments of at most max_tokens, cut at class or method starts where possible."""
    boundary = [bool(re.match(r"\s*(?:def|class|module)\s", l)) for l in lines]
    segs, start = [], 0
    while start < len(lines):
        size, cut, i = 0, None, start
        while i < len(lines):
            size += est_tokens(lines[i] + "\n")
            if size > max_tokens and i > start:
                break
            i += 1
            if i < len(lines) and boundary[i]:
                cut = i
        end = len(lines) if i >= len(lines) else (cut if cut and cut > start else i)
        segs.append({"path": path, "start_line": start + 1, "end_line": end})
        start = end
    return segs


def build_units(repo: Path, files: list[str], ucfg: dict) -> list[dict]:
    """Split large files, pack small files of one directory. A directory is one
    engine and one layer, so packs never cross either."""
    max_tokens = int(ucfg.get("max_tokens") or 6000)
    target = int(ucfg.get("pack_target_tokens") or 4000)
    pack = bool(ucfg.get("pack_small_files", True))
    approved = ucfg.get("approved_secret_paths") or []
    out: list[dict] = []
    current: dict | None = None

    def flush():
        nonlocal current
        if current:
            out.append(current)
        current = None

    for path in sorted(files):
        text = (repo / path).read_text(encoding="utf-8", errors="replace")
        tokens = est_tokens(text)
        secrets = [] if matches(path, approved) else scan_secrets(text)
        directory = path.rsplit("/", 1)[0] if "/" in path else ""
        if secrets:
            flush()
            out.append({"segments": [path], "tokens": tokens, "blocked": "secret_found",
                        "secrets": [{"path": path, **s} for s in secrets]})
        elif tokens > max_tokens:
            flush()
            lines = text.splitlines()
            for seg in split_file(path, lines, max_tokens):
                chunk = "\n".join(lines[seg["start_line"] - 1:seg["end_line"]])
                out.append({"segments": [seg], "tokens": est_tokens(chunk)})
        elif pack and tokens < target:
            if current and (current["dir"] != directory or current["tokens"] + tokens > target):
                flush()
            if current is None:
                current = {"segments": [], "tokens": 0, "dir": directory}
            current["segments"].append(path)
            current["tokens"] += tokens
        else:
            flush()
            out.append({"segments": [path], "tokens": tokens})
    flush()
    return out


# ------------------------------------------------------------------- context

POLICY_RE = re.compile(r"\b((?:[A-Z]\w*::)*[A-Z]\w*Policy)\b")


def build_context(index: rubyindex.Index, unit_paths: list[str], budget: int) -> list[str]:
    """Superclass chain and included concerns of every class in the unit and,
    for a controller, the Pundit policy it names. Read-only context."""
    wanted: list[str] = []

    def want(path):
        if path not in unit_paths and path not in wanted:
            wanted.append(path)

    for path in unit_paths:
        for d in index.by_path.get(path, []):
            for apath, _ in index.ancestors(d):
                want(apath)
            names = set(POLICY_RE.findall(index.text[path]))
            short = d["name"].split("::")[-1]
            if short.endswith("Controller"):
                stem = short[:-len("Controller")]
                names |= {stem + "Policy", re.sub(r"s$", "", stem) + "Policy"}
            for name in sorted(names):
                hit = index.resolve(name, d["name"].rsplit("::", 1)[0] if "::" in d["name"] else "")
                if hit:
                    want(hit[0])
    out, used = [], 0
    for path in wanted:
        lines = index.text[path].splitlines()
        body = "\n".join(f"{i:>5}| {l}" for i, l in enumerate(lines, 1))
        block = f"=== CONTEXT {path} ===\n{body}"
        cost = units_tokens(block)
        if used + cost > budget:
            room = int((budget - used) * 3.5)
            if room < 200:
                break
            block = block[:room].rsplit("\n", 1)[0] + "\n  ... (context truncated)"
            cost = units_tokens(block)
        out.append(block)
        used += cost
    return out


def units_tokens(text: str) -> int:
    return est_tokens(text)


# ------------------------------------------------------- scanners and baseline

def _brakeman_items(doc) -> list[dict]:
    """Warnings from Brakeman JSON output or a brakeman.ignore file."""
    items = []
    for key in ("warnings", "ignored_warnings"):
        for w in (doc.get(key) or []) if isinstance(doc, dict) else []:
            if isinstance(w, dict) and w.get("file"):
                items.append({"path": w["file"], "line": w.get("line"),
                              "rule": w.get("warning_type") or w.get("check_name") or "warning",
                              "message": str(w.get("message") or w.get("note") or "")[:300],
                              "fingerprint": w.get("fingerprint")})
    return items


def run_scanners(repo: Path, cfg, run_dir: Path) -> tuple[list[dict], dict]:
    """Run the configured scanners. Returns (hits, status per scanner)."""
    hits, status = [], {}
    for name in cfg.get("scanners") or []:
        if name != "brakeman":
            status[name] = "not run: this version runs brakeman only; import its report via baseline.scanner_reports"
            continue
        exe = shutil.which("brakeman")
        if not exe:
            status[name] = "not run: brakeman is not on PATH"
            continue
        out = run_dir / "scanner_brakeman.json"
        try:
            # flags checked against `brakeman --help` (8.0.6)
            subprocess.run([exe, "-q", "-f", "json", "-o", str(out), "-p", str(repo)],
                           capture_output=True, timeout=1800)
            items = _brakeman_items(json.loads(out.read_text()))
            hits += [{**i, "scanner": "brakeman"} for i in items]
            status[name] = f"ok: {len(items)} warnings"
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            status[name] = f"failed: {type(e).__name__}"
    return hits, status


def load_baseline(repo: Path, cfg) -> tuple[list[dict], list[str]]:
    """Known findings: the Brakeman ignore file, scanner reports and the owner's list."""
    entries, notes = [], []
    sources = []
    ignore = cfg.get("baseline.brakeman_ignore")
    if ignore:
        sources.append(("brakeman_ignore", repo / ignore, True))
    for p in cfg.get("baseline.scanner_reports") or []:
        sources.append(("scanner_report", Path(p) if Path(p).is_absolute() else repo / p, False))
    for source, path, optional in sources:
        if not path.exists():
            if not optional:
                notes.append(f"baseline file not found: {path}")
            continue
        try:
            items = _brakeman_items(json.loads(path.read_text()))
        except ValueError:
            notes.append(f"baseline file is not Brakeman JSON, skipped: {path}")
            continue
        for i in items:
            entries.append({"ref": f"{source}:{i['fingerprint'] or i['rule']}", "source": source,
                            "path": i["path"], "start_line": i["line"], "end_line": i["line"],
                            "description": f"{i['rule']}: {i['message']}"})
    for n, item in enumerate(cfg.get("baseline.known_issues") or []):
        if isinstance(item, str):
            p = Path(item) if Path(item).is_absolute() else repo / item
            try:
                loaded = yamlish.load(p)
            except (OSError, yamlish.YamlError) as e:
                notes.append(f"known_issues file unreadable: {p} ({e})")
                continue
            items = loaded if isinstance(loaded, list) else []
        else:
            items = [item]
        for k, it in enumerate(items):
            if isinstance(it, dict) and it.get("path"):
                entries.append({"ref": str(it.get("ref") or it.get("id") or f"known-{n}-{k}"),
                                "source": "known_issues", "path": it["path"],
                                "start_line": it.get("start_line"),
                                "end_line": it.get("end_line") or it.get("start_line"),
                                "description": str(it.get("description") or "")})
    return entries, notes


def baseline_match(conn, locations: list[dict]) -> str | None:
    """The ref of a baseline entry on the same file with overlapping lines."""
    for loc in locations:
        for row in conn.execute("SELECT * FROM baseline WHERE path = ?", (loc["path"],)):
            if row["start_line"] is None:
                continue
            if row["start_line"] <= loc["end_line"] and (row["end_line"] or row["start_line"]) >= loc["start_line"]:
                return row["ref"]
    return None


# ---------------------------------------------------------------------- main

def build_index(repo: Path, tracked: list[str]) -> rubyindex.Index:
    index = rubyindex.Index()
    for path in tracked:
        if path.endswith(".rb") and "/spec/" not in f"/{path}":
            try:
                index.add(path, (repo / path).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    return index


def run(cfg, runs_root: Path, mode: str = "audit") -> tuple[Path, dict]:
    repo = Path(cfg.get("target.repo_path"))
    if not (repo / ".git").exists():
        raise PreflightError(f"target.repo_path is not a git checkout: {repo}")
    commit = git(repo, "rev-parse", cfg.get("target.commit") or "HEAD").strip()
    if git(repo, "rev-parse", "HEAD").strip() != commit:
        raise PreflightError("the audit clone is not checked out at target.commit")
    if git(repo, "status", "--porcelain", "--untracked-files=no").strip():
        raise PreflightError("the audit clone has uncommitted changes to tracked files")

    ucfg = cfg.run.get("units") or {}
    tracked = [p for p in git(repo, "ls-files", "-z").split("\0") if p]
    files = [p for p in tracked if matches(p, ucfg.get("include")) and not matches(p, ucfg.get("exclude"))
             and (repo / p).is_file()]
    engines = cfg.get("target.engines") or []
    pkg_spec = ucfg.get("package_from_path")
    if engines:
        files = [p for p in files if package_of(p, pkg_spec) in engines]
    built = build_units(repo, files, ucfg)
    index = build_index(repo, tracked)

    run_id = datetime.now(timezone.utc).strftime("r-%Y%m%d-%H%M%S")
    run_dir = Path(runs_root) / run_id
    conn = ledger.create_run(run_dir, cfg, mode=mode, repo_commit=commit, repo_path=str(repo))
    scanner_hits, scanner_status = run_scanners(repo, cfg, run_dir)
    baseline, notes = load_baseline(repo, cfg)
    hits_by_path: dict[str, list] = {}
    for h in scanner_hits + [{"path": b["path"], "line": b["start_line"], "rule": b["ref"],
                              "scanner": b["source"]} for b in baseline if b["source"] != "known_issues"]:
        hits_by_path.setdefault(h["path"], []).append(
            {"scanner": h["scanner"], "rule": h["rule"], "line": h.get("line")})

    blocked, total_tokens = [], 0
    with conn:
        for b in baseline:
            conn.execute("INSERT INTO baseline (ref, source, path, start_line, end_line, description)"
                         " VALUES (?,?,?,?,?,?)", (b["ref"], b["source"], b["path"], b["start_line"],
                                                   b["end_line"], b["description"]))
        for u in built:
            paths_json = json.dumps(u["segments"])
            segs = units.segments(paths_json)
            paths = [s["path"] for s in segs]
            unit_id = "u" + hashlib.sha1(paths_json.encode()).hexdigest()[:10]
            context = [] if u.get("blocked") else build_context(
                index, paths, int(ucfg.get("context_tokens") or 2000))
            hits = []
            for s in segs:
                for h in hits_by_path.get(s["path"], []):
                    lo, hi = s.get("start_line") or 1, s.get("end_line") or 10 ** 9
                    if h["line"] is None or lo <= h["line"] <= hi:
                        hits.append({**h, "path": s["path"]})
            total_tokens += u["tokens"]
            if u.get("blocked"):
                blocked.append({"unit_id": unit_id, "reason": u["blocked"], "hits": u["secrets"]})
            conn.execute(
                "INSERT INTO units (unit_id, kind, paths_json, sha256, tokens, package, sensitive,"
                " scanner_hits_json, blocked_reason, context_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (unit_id, "file", paths_json,
                 units.subject_hash(units.read(repo, paths_json)[1], context), u["tokens"],
                 package_of(paths[0], pkg_spec),
                 int(any(matches(p, ucfg.get("sensitive_globs", ["**/*"])) for p in paths)),
                 json.dumps(hits), u.get("blocked"), json.dumps(context)))
        ledger.mark_stage(conn, "preflight")
    (run_dir / "blocked_units.json").write_text(json.dumps(blocked, indent=1))
    Path(runs_root, "current").write_text(run_id + "\n")
    summary = {
        "run_id": run_id, "run_dir": str(run_dir), "commit": commit, "files": len(files),
        "units": len(built), "packed_units": sum(1 for u in built if len(u["segments"]) > 1),
        "split_units": sum(1 for u in built if isinstance(u["segments"][0], dict)),
        "unit_tokens_estimate": total_tokens, "blocked_units": len(blocked),
        "scanners": scanner_status, "scanner_hits": len(scanner_hits),
        "baseline_entries": len(baseline), "notes": notes}
    conn.close()
    return run_dir, summary


def scout(conn) -> dict:
    """S1, first version: order by scanner hit count. Never removes a unit."""
    rows = conn.execute("SELECT unit_id, scanner_hits_json, paths_json FROM units").fetchall()
    ranked = sorted(rows, key=lambda r: (-len(json.loads(r["scanner_hits_json"] or "[]")), r["paths_json"]))
    with conn:
        for rank, r in enumerate(ranked):
            conn.execute("UPDATE units SET order_rank = ? WHERE unit_id = ?", (rank, r["unit_id"]))
        ledger.mark_stage(conn, "scout")
    return {"units": len(ranked),
            "with_scanner_hits": sum(1 for r in rows if json.loads(r["scanner_hits_json"] or "[]"))}
