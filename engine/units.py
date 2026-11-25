"""Reading a unit's code from the audit clone.

A unit's `paths_json` is a list of segments: a path string for a whole file,
or {"path", "start_line", "end_line"} for part of a split file. The unit hash
covers exactly the text the models are shown.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


class UnitError(Exception):
    pass


def segments(paths_json: str) -> list[dict]:
    out = []
    for seg in json.loads(paths_json):
        out.append({"path": seg} if isinstance(seg, str) else dict(seg))
    return out


def _lines(repo: Path, seg: dict) -> tuple[int, list[str]]:
    root = repo.resolve()
    path = (root / seg["path"]).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise UnitError(f"not a file inside the audit clone: {seg['path']}")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = int(seg.get("start_line") or 1)
    end = int(seg.get("end_line") or len(lines))
    return start, lines[start - 1:end]


def read(repo, paths_json: str) -> tuple[str, str]:
    """(text with real line numbers, sha256 of the raw text)."""
    digest = hashlib.sha256()
    parts = []
    for seg in segments(paths_json):
        start, lines = _lines(Path(repo), seg)
        digest.update(json.dumps([seg["path"], start, lines]).encode())
        end = start + len(lines) - 1
        body = "\n".join(f"{start + i:>5}| {line}" for i, line in enumerate(lines))
        parts.append(f"=== {seg['path']} (lines {start}-{end}) ===\n{body}")
    return "\n\n".join(parts), digest.hexdigest()


def covers(paths_json: str, location: dict) -> bool:
    """Is this candidate location inside the unit itself (not its context)?"""
    for seg in segments(paths_json):
        if seg["path"] != location.get("path"):
            continue
        lo = int(seg.get("start_line") or 1)
        hi = seg.get("end_line")
        if location["start_line"] >= lo and (hi is None or location["start_line"] <= int(hi)):
            return True
    return False


def subject_hash(text_sha: str, context: list[str]) -> str:
    """What a unit's calls are keyed on: its own text plus the context shown with it."""
    return hashlib.sha256(json.dumps([text_sha, context]).encode()).hexdigest()
