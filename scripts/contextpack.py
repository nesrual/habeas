#!/usr/bin/env python3
"""Assemble an expert context pack for one candidate (plan 7.4).

usage: contextpack.py <finding-json-file> --out <pack.md> [--repo <dir>]

The finding JSON is a candidate record (needs `file`, `lines`, `category`;
the rest is carried into the pack as metadata). `-` reads it from stdin.

Recipes: candidate metadata + flagged file (full, or a 400-line window for
big files) + callers of the defining symbols via `git grep -n --untracked`,
up to 5 sites per symbol with +/-5 lines context. Per-domain extras (v2):
expert-db gets schema sections (db/schema.rb, or db/structure.sql — a
decide-and-note extension) for tables named by the flagged file plus the 3
most recent migrations touching them; expert-security gets the auth files
(config `packs.security_globs`); expert-tests gets the specs mirroring the
flagged file. Budget drop order: caller sites, then migrations, then schema,
then auth/spec extras — never the flagged window.

Prints a JSON summary: {"pack", "route", "lines", "symbols", "caller_sites",
"truncated_sites"}. `route` implements the plan 7.4 routing table; v1
orchestration ignores it and uses expert-backend.

Decisions noted (plan 14):
- symbol extraction: definitions inside the flagged range; when none, the
  innermost enclosing definition above the range.
- "10 lines of context" is rendered as 5 before + 5 after each call site.
- callers skip definition lines, comment-only lines, and hits inside the
  flagged range itself.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state  # noqa: E402
from paths import source_path

WINDOW = 400
MAX_SITES_PER_SYMBOL = 5
MAX_SYMBOLS = 5
PACK_LINE_CAP = 1500
CONTEXT = 5

DEF_RE = re.compile(
    r"^\s*(?:def\s+(?:self\.)?([a-zA-Z_]\w*[?!=]?)|class\s+([A-Z]\w*)|module\s+([A-Z]\w*))")

SQL_RE = re.compile(r"\bselect\b.+\bfrom\b|\binsert\s+into\b|\bupdate\s+\w+\s+set\b",
                    re.IGNORECASE | re.DOTALL)


def route(record: dict) -> str:
    category = record.get("category", "")
    if category == "security":
        return "expert-security"
    if category == "test-gap":
        return "expert-tests"
    if category == "bug":
        path = record.get("file", "")
        if re.search(r"(^|/)app/models/|(^|/)db/|repository", path) \
           or SQL_RE.search(record.get("evidence_snippet", "")):
            return "expert-db"
    return "expert-backend"


def _first_group(m: re.Match) -> str:
    return next(g for g in m.groups() if g)


def extract_symbols(lines: list, lo: int, hi: int) -> list:
    """Definitions inside [lo, hi] (1-based); else innermost enclosing one."""
    symbols = []
    for i in range(lo - 1, min(hi, len(lines))):
        m = DEF_RE.match(lines[i])
        if m:
            symbols.append(_first_group(m))
    if not symbols:
        for i in range(min(lo - 1, len(lines)) - 1, -1, -1):
            m = DEF_RE.match(lines[i])
            if m:
                symbols.append(_first_group(m))
                break
    seen, out = set(), []
    for s in symbols:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out[:MAX_SYMBOLS]


def numbered(lines: list, start: int, end: int) -> str:
    """Render lines[start-1:end] with 1-based line numbers."""
    width = len(str(end))
    return "\n".join(f"{i:>{width}} | {lines[i - 1]}" for i in range(start, end + 1))


def flagged_window(lines: list, lo: int, hi: int) -> tuple:
    n = len(lines)
    if n <= WINDOW:
        return 1, n
    span = hi - lo + 1
    if span >= WINDOW:
        return lo, hi
    pad = (WINDOW - span) // 2
    start = max(1, lo - pad)
    end = min(n, start + WINDOW - 1)
    start = max(1, end - WINDOW + 1)
    return start, end


def find_callers(repo: str, symbol: str, flagged_rel: str, lo: int, hi: int,
                 excludes: list) -> list:
    """[(path, line_no)] of up to MAX_SITES_PER_SYMBOL call sites."""
    args = ["git", "-C", repo, "grep", "-n", "--untracked"]
    if re.fullmatch(r"\w+", symbol):
        args.append("-w")
    args += ["-e", symbol, "--", "."]
    args += [f":(exclude){e}" for e in excludes]
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode > 1:
        raise RuntimeError(f"git grep failed: {proc.stderr.strip()}")
    sites = []
    for hit in proc.stdout.splitlines():
        path, _, rest = hit.partition(":")
        line_s, _, content = rest.partition(":")
        if not line_s.isdigit():
            continue
        line_no = int(line_s)
        stripped = content.strip()
        if stripped.startswith("#"):
            continue
        if DEF_RE.match(content):
            continue
        if path == flagged_rel and lo <= line_no <= hi:
            continue
        sites.append((path, line_no))
        if len(sites) >= MAX_SITES_PER_SYMBOL:
            break
    return sites


SECURITY_GLOB_DEFAULTS = ["app/controllers/application_controller.rb",
                          "app/policies/**", "config/initializers/*auth*"]
MAX_EXTRA_FILES = 8


def tableize(model: str) -> str:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", model).lower()
    if snake.endswith("y") and snake[-2:-1] not in ("a", "e", "i", "o", "u"):
        return snake[:-1] + "ies"
    if snake.endswith(("s", "x", "ch", "sh")):
        return snake + "es"
    return snake + "s"


def table_names(rel: str, file_text: str) -> list:
    names = []
    m = re.match(r"app/models/(.+)\.rb$", rel)
    if m:
        names.append(tableize(m.group(1).replace("/", "_")))
    for cm in re.finditer(r"^\s*class\s+([A-Z]\w*)\b", file_text, re.M):
        names.append(tableize(cm.group(1)))
    for tm in re.finditer(r"self\.table_name\s*=\s*[\"'](\w+)[\"']", file_text):
        names.append(tm.group(1))
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def schema_blocks(repo: str, tables: list) -> list:
    """[(title, text)] — create_table / CREATE TABLE sections per table."""
    blocks = []
    schema_rb = source_path(repo, "db/schema.rb")
    structure = source_path(repo, "db/structure.sql")
    if schema_rb.exists():
        text = schema_rb.read_text(encoding="utf-8", errors="replace")
        for t in tables:
            m = re.search(rf'^( *)create_table "{re.escape(t)}"[^\n]*\n.*?^\1end$', text, re.M | re.S)
            if m:
                blocks.append((f"Schema: {t} (db/schema.rb)", m.group(0)))
    elif structure.exists():
        text = structure.read_text(encoding="utf-8", errors="replace")
        for t in tables:
            m = re.search(rf'^CREATE TABLE (?:\w+\.)?{re.escape(t)} \(.*?\);', text, re.M | re.S)
            if m:
                blocks.append((f"Schema: {t} (db/structure.sql)", m.group(0)))
    return blocks


def migration_blocks(repo: str, tables: list, limit: int = 3) -> list:
    mig_dir = source_path(repo, "db/migrate")
    if not mig_dir.is_dir():
        return []
    blocks = []
    for p in sorted(mig_dir.glob("*.rb"), reverse=True):
        text = source_path(repo, p).read_text(encoding="utf-8", errors="replace")
        if any(t in text for t in tables):
            blocks.append((f"Migration: db/migrate/{p.name}", text))
            if len(blocks) >= limit:
                break
    return blocks


def security_blocks(repo: str, cfg: dict) -> list:
    patterns = cfg.get("packs", {}).get("security_globs", SECURITY_GLOB_DEFAULTS)
    files = []
    for pattern in patterns:
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ValueError("security glob must be repository-relative")
        if pattern.endswith("**"):
            pattern += "/*"
        for p in sorted(Path(repo).glob(pattern)):
            if p.is_file():
                files.append(source_path(repo, p))
    blocks = []
    for p in files[:MAX_EXTRA_FILES]:
        rel = os.path.relpath(p, Path(repo).resolve())
        blocks.append((f"Auth: {rel}", p.read_text(encoding="utf-8", errors="replace")))
    return blocks


def mirror_spec_blocks(repo: str, rel: str) -> list:
    stem = re.sub(r"\.rb$", "", rel)
    candidates = []
    if stem.startswith("app/"):
        candidates.append(f"spec/{stem[4:]}_spec.rb")
    elif stem.startswith("lib/"):
        candidates.append(f"spec/lib/{stem[4:]}_spec.rb")
        candidates.append(f"spec/{stem[4:]}_spec.rb")
    else:
        candidates.append(f"spec/{stem}_spec.rb")
    blocks = []
    for c in candidates:
        p = source_path(repo, c)
        if p.exists():
            blocks.append((f"Mirror spec: {c}", p.read_text(encoding="utf-8", errors="replace")))
    return blocks


def domain_extras(repo: str, record: dict, rel: str, file_text: str, cfg: dict) -> list:
    """[(drop_rank, title, text)] in presentation order. Lower rank drops
    first when over budget: migrations (1), schema (2), auth/specs (3)."""
    r = route(record)
    extras = []
    if r == "expert-db":
        tables = table_names(rel, file_text)
        extras += [(2, t, x) for t, x in schema_blocks(repo, tables)]
        extras += [(1, t, x) for t, x in migration_blocks(repo, tables)]
    elif r == "expert-security":
        extras += [(3, t, x) for t, x in security_blocks(repo, cfg)]
    elif r == "expert-tests":
        extras += [(3, t, x) for t, x in mirror_spec_blocks(repo, rel)]
    return extras


def render_site(repo: str, path: str, line_no: int) -> str:
    lines = source_path(repo, path).read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, line_no - CONTEXT)
    end = min(len(lines), line_no + CONTEXT)
    return f"### {path}:{line_no}\n\n```\n{numbered(lines, start, end)}\n```"


def build_pack(repo: str, record: dict) -> tuple:
    rel = record["file"]
    lo, hi = record["lines"]
    abs_p = source_path(repo, rel)
    if not abs_p.exists():
        raise FileNotFoundError(f"flagged file not found: {abs_p}")
    lines = abs_p.read_text(encoding="utf-8", errors="replace").splitlines()
    lo = max(1, min(lo, len(lines)))
    hi = max(lo, min(hi, len(lines)))

    excludes = state.load_config(repo).get("scope", {}).get("exclude", [])
    symbols = extract_symbols(lines, lo, hi)

    head = [f"# Context pack: {record.get('id', rel)}", "",
            "## Candidate", "", "```json",
            json.dumps(record, indent=2), "```", ""]
    start, end = flagged_window(lines, lo, hi)
    scope_note = "full file" if (start, end) == (1, len(lines)) \
        else f"window {start}-{end} of {len(lines)} lines"
    head += [f"## Flagged file: {rel} (lines {lo}-{hi} flagged; {scope_note})", "",
             "```", numbered(lines, start, end), "```", ""]

    site_blocks = []
    for symbol in symbols:
        for path, line_no in find_callers(repo, symbol, rel, lo, hi, excludes):
            site_blocks.append((symbol, render_site(repo, path, line_no)))

    cfg = state.load_config(repo)
    file_text = "\n".join(lines)
    extras = domain_extras(repo, record, rel, file_text, cfg)

    def assemble(n_sites, kept_extras):
        parts = list(head)
        current = None
        for symbol, block in site_blocks[:n_sites]:
            if symbol != current:
                parts += [f"## Callers of `{symbol}`", ""]
                current = symbol
            parts += [block, ""]
        for _rank, title, text in kept_extras:
            parts += [f"## {title}", "", "```", text.rstrip(), "```", ""]
        dropped = (len(site_blocks) - n_sites) + (len(extras) - len(kept_extras))
        if dropped:
            parts += [f"(truncated: {dropped} block(s) dropped to fit the "
                      f"{PACK_LINE_CAP}-line budget)", ""]
        return "\n".join(parts)

    kept_sites = len(site_blocks)
    kept_extras = list(extras)
    text = assemble(kept_sites, kept_extras)
    while text.count("\n") + 1 > PACK_LINE_CAP and (kept_sites > 0 or kept_extras):
        if kept_sites > 0:
            kept_sites -= 1  # callers drop first (plan 7.4)
        else:
            lowest = min(e[0] for e in kept_extras)
            for i in range(len(kept_extras) - 1, -1, -1):
                if kept_extras[i][0] == lowest:
                    del kept_extras[i]
                    break
        text = assemble(kept_sites, kept_extras)
    return text, {"route": route(record), "symbols": symbols,
                  "caller_sites": kept_sites,
                  "truncated_sites": len(site_blocks) - kept_sites,
                  "extras": [t for _r, t, _x in kept_extras],
                  "extras_dropped": len(extras) - len(kept_extras)}


def main(argv: list) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("finding")
    ap.add_argument("--out", required=True)
    ap.add_argument("--repo", default=".")
    args = ap.parse_args(argv[1:])
    try:
        raw = sys.stdin.read() if args.finding == "-" else Path(args.finding).read_text()
        record = json.loads(raw)
        if "record" in record and isinstance(record["record"], dict):
            record = record["record"]  # accept a full `state.py show` entry too
        for field in ("file", "lines", "category"):
            if field not in record:
                raise ValueError(f"candidate record missing field: {field}")
        text, meta = build_pack(args.repo, record)
        Path(args.out).write_text(text, encoding="utf-8")
    except (OSError, ValueError, RuntimeError) as e:
        print(f"contextpack: {e}", file=sys.stderr)
        return 1
    print(json.dumps({"pack": args.out, "lines": text.count("\n") + 1, **meta}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
