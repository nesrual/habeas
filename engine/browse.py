"""Read-only access to the audit clone for scripted provers.

A prover model cannot run anything. It asks for one of three fixed operations
(read files, list a directory, search for a literal string) and the engine
answers from the clone. Only tracked files that match `provers.browse.allow`,
do not match `provers.browse.deny` and pass the secret scan are ever shown.
"""

from __future__ import annotations

import difflib
import subprocess
from pathlib import Path

from . import preflight

ALLOW = ["**/*.rb", "**/*.rake", "**/*.yml", "**/*.yaml", "**/*.md", "**/Gemfile", "bin/*"]
DENY = ["**/fixtures/**", "**/cassettes/**", "**/*.pem", "**/*.key", "**/*.crt", "**/*.p12",
        "**/.env*", "**/credentials*", "**/secrets*", "**/master.key", "db/seeds/**", "db/seeds.rb",
        "docker_config/**", "node_modules/**", "vendor/**", "tmp/**", "log/**"]
MAX_PATHS, MAX_LINES, MAX_LISTED, MAX_HITS = 5, 400, 200, 60

PROTOCOL = """

How to work. You answer in steps, at most {{max_steps}}. In each step return one JSON object
matching the schema below and nothing else.
- `read`: put up to 5 repository-relative file paths in `paths`. You get their contents.
- `list`: put directory paths in `paths`. You get the tracked files under them.
- `search`: put a literal string in `query` (and optionally directories in `paths`). You get
  matching lines.
- `submit`: put the full test file in `test_file` and the fix in `edits`. Each edit names an
  existing file `path`, an exact snippet `old` that occurs once in it, and its replacement `new`.
  Copy `old` from file contents you were shown. Do not write a diff yourself.
  Leave `test_package` empty to have the test saved at the destination named above. If the code
  that must be fixed lives in another package of this repository and the defect can only be
  shown there, put that package's root in `test_package` (for example `engines/billing`):
  the test is then saved under that package's `spec/habeas_proofs/` and run with that package's
  specs, and the fix must change at least one file in that package.
- `decline`: say why in `reason`.
Read the existing specs for the claimed code before you write a test, so yours loads the same
helpers and builds records the same way. Set unused fields to "" or [].

{{schema}}"""


class Browser:
    def __init__(self, repo, cfg):
        self.repo = Path(repo).resolve()
        bc = cfg.get("provers.browse") or {}
        self.allow = bc.get("allow") or ALLOW
        self.deny = DENY + list(bc.get("deny") or [])
        out = subprocess.run(["git", "-C", str(self.repo), "ls-files", "-z"], capture_output=True, text=True)
        self.tracked = [p for p in out.stdout.split("\0") if p]
        self._tracked = set(self.tracked)

    def allowed(self, path: str) -> str | None:
        """None when the file may be shown, else the reason it may not."""
        path = path.strip().lstrip("./")
        if path not in self._tracked:
            return "not a tracked file in the repository"
        if not preflight.matches(path, self.allow) or preflight.matches(path, self.deny):
            return "this kind of file is not available to provers"
        full = (self.repo / path).resolve()
        if not full.is_relative_to(self.repo) or not full.is_file():
            return "not a regular file inside the repository"
        return None

    def text(self, path: str) -> tuple[str | None, str | None]:
        path = path.strip().lstrip("./")
        why = self.allowed(path)
        if why:
            return None, why
        text = (self.repo / path).read_text(encoding="utf-8", errors="replace")
        if preflight.scan_secrets(text):
            return None, "withheld: the secret scan flagged this file"
        return text, None

    def read(self, paths: list[str]) -> str:
        out = []
        for path in paths[:MAX_PATHS]:
            text, why = self.text(path)
            if why:
                out.append(f"=== {path} ===\n({why})")
                continue
            lines = text.splitlines()
            body = "\n".join(f"{i:>5}| {l}" for i, l in enumerate(lines[:MAX_LINES], 1))
            more = f"\n  ... ({len(lines) - MAX_LINES} more lines not shown)" if len(lines) > MAX_LINES else ""
            out.append(f"=== {path} ===\n{body}{more}")
        if len(paths) > MAX_PATHS:
            out.append(f"(only the first {MAX_PATHS} paths were read)")
        return "\n\n".join(out) or "(no paths given)"

    def list(self, paths: list[str]) -> str:
        out = []
        for d in paths[:MAX_PATHS] or [""]:
            prefix = d.strip().strip("/").lstrip("./")
            names = [p for p in self.tracked if (not prefix or p.startswith(prefix + "/"))
                     and self.allowed(p) is None]
            shown = "\n".join(names[:MAX_LISTED]) or "(no files available here)"
            more = f"\n... ({len(names) - MAX_LISTED} more)" if len(names) > MAX_LISTED else ""
            out.append(f"{prefix or '.'}/:\n{shown}{more}")
        return "\n\n".join(out)

    def search(self, query: str, paths: list[str]) -> str:
        if len(query.strip()) < 3:
            return "(the search string must have at least 3 characters)"
        prefixes = [p.strip().strip("/") for p in paths if p.strip()]
        hits = []
        for path in self.tracked:
            if prefixes and not any(path.startswith(p + "/") or path == p for p in prefixes):
                continue
            if not path.endswith((".rb", ".rake")) or self.allowed(path):
                continue
            try:
                text = (self.repo / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if query not in text or preflight.scan_secrets(text):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if query in line:
                    hits.append(f"{path}:{n}: {line.strip()[:200]}")
                    if len(hits) >= MAX_HITS:
                        return "\n".join(hits) + f"\n... (stopped at {MAX_HITS} matches; narrow the search)"
        return "\n".join(hits) or "(no matches)"


def _diff(path: str, old: str, new: str) -> str:
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    out = []
    for line in difflib.unified_diff(a, b, fromfile=f"a/{path}", tofile=f"b/{path}"):
        out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out)


def build_patch(browser: Browser, edits: list[dict], test_dest: str) -> tuple[str | None, str | None]:
    """Turn search-and-replace edits into a unified diff. (patch, None) or (None, problem)."""
    if not edits:
        return None, "no edits given"
    contents: dict[str, str] = {}
    originals: dict[str, str] = {}
    for e in edits:
        path = e["path"].strip().lstrip("./")
        if path == test_dest or "habeas_proofs" in Path(path).parts:
            return None, "the fix must not touch the proof test"
        if path not in contents:
            text, why = browser.text(path)
            if why:
                return None, f"cannot edit {path}: {why}"
            contents[path] = originals[path] = text
        count = contents[path].count(e["old"]) if e["old"] else 0
        if count != 1:
            return None, (f"edit of {path}: `old` must occur exactly once in the file, it occurs {count} "
                          "times. Read the file and copy the snippet exactly")
        contents[path] = contents[path].replace(e["old"], e["new"])
    patch = "".join(_diff(p, originals[p], contents[p]) for p in contents if contents[p] != originals[p])
    return (patch, None) if patch else (None, "the edits change nothing")
