#!/usr/bin/env python3
"""Churn x size scan queue (plan 7.8, v2).

usage: hotspots.py [--repo <dir>] [--top 10] [--days 180] [--depth 2]

Score per unit (a directory truncated to --depth path components) =
commits touching the unit in the last --days * (total_lines / file_count).
Line count stands in for complexity; cyclomatic parsing is not worth a
dependency. Excludes scope.exclude paths plus scan.hotspot_exclude (default:
spec/test/doc/asset dirs — churn there is not audit surface; decide-and-note)
and skips units whose files are ALL recorded in scanned_hunks with unchanged
content. Output: a JSON queue, highest score first — feed entries to
`run.sh --unit`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state  # noqa: E402
from paths import source_path


def unit_of(rel: str, depth: int) -> str:
    parts = rel.split("/")[:-1]  # directory components only
    return "/".join(parts[:depth]) if parts else "."


def excluded(rel: str, excludes: list) -> bool:
    return any(rel == e or rel.startswith(e.rstrip("/") + "/") or rel.startswith(e)
               for e in excludes)


def git_lines(repo: str, *args: str) -> list:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"hotspots: git failed: {proc.stderr.strip()}")
    return proc.stdout.splitlines()


HOTSPOT_EXCLUDE_DEFAULTS = ["spec/", "test/", "features/", "public/", "docs/",
                            "doc/", "bin/", "node_modules/", "Gemfile.lock",
                            "yarn.lock", "package-lock.json", "db/structure.sql"]


def build_queue(repo: str, days: int, depth: int, top: int) -> list:
    cfg = state.load_config(repo)
    excludes = list(cfg.get("scope", {}).get("exclude", []))
    excludes += cfg.get("scan", {}).get("hotspot_exclude", HOTSPOT_EXCLUDE_DEFAULTS)

    tracked = [f for f in git_lines(repo, "ls-files") if not excluded(f, excludes)]
    units: dict = {}
    for rel in tracked:
        try:
            p = source_path(repo, rel)
            if not p.is_file():
                continue
            n_lines = len(p.read_text(encoding="utf-8").splitlines())
        except (ValueError, OSError, RuntimeError):
            continue  # binary or gone
        u = units.setdefault(unit_of(rel, depth), {"lines": 0, "files": []})
        u["lines"] += n_lines
        u["files"].append(rel)

    commits: dict = {}
    touched: set = set()
    for line in git_lines(repo, "log", f"--since={days} days ago",
                          "--name-only", "--pretty=format:@@commit@@"):
        if line == "@@commit@@":
            for unit in touched:
                commits[unit] = commits.get(unit, 0) + 1
            touched = set()
        elif line.strip() and not excluded(line, excludes):
            touched.add(unit_of(line, depth))
    for unit in touched:
        commits[unit] = commits.get(unit, 0) + 1

    hunks = {}
    sp = state.state_path(repo)
    if sp.exists():
        hunks = json.loads(sp.read_text())["scanned_hunks"]

    def all_scanned(files: list) -> bool:
        for rel in files:
            try:
                h = hashlib.sha256(source_path(repo, rel).read_bytes()).hexdigest()[:16]
            except (OSError, ValueError):
                return False
            if h not in hunks:
                return False
        return True

    queue = []
    for unit, info in units.items():
        n_commits = commits.get(unit, 0)
        if n_commits == 0 or not info["files"]:
            continue
        if hunks and all_scanned(info["files"]):
            continue
        score = n_commits * (info["lines"] / len(info["files"]))
        queue.append({"unit": unit, "score": round(score, 1), "commits": n_commits,
                      "lines": info["lines"], "files": len(info["files"])})
    queue.sort(key=lambda q: (-q["score"], q["unit"]))
    return queue[:top]


def main(argv: list) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--depth", type=int, default=2)
    args = ap.parse_args(argv[1:])
    print(json.dumps(build_queue(args.repo, args.days, args.depth, args.top), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
