#!/usr/bin/env python3
"""Dedupe fingerprints for habeas findings (plan section 6.4).

fingerprint = sha256(relpath + "\\n" + category + "\\n" + normalize(snippet))[:16]

normalize: per line, strip leading/trailing whitespace, collapse internal
whitespace runs to one space, drop comment-only lines ("#" prefix after
strip). Decision (noted per plan section 14): blank lines are dropped too --
blank-line churn is reformatting and must not change a finding's identity.

The snippet passed in should be the flagged lines plus one line of context on
each side (the scout's evidence_snippet contract). The fingerprint survives
line drift and reformatting; it does not survive a rewrite of the flagged
code, which is acceptable.

CLI: fingerprint.py <relpath> <category> [snippet-file]
Snippet is read from snippet-file, or stdin when omitted. Prints the 16-hex
fingerprint.
"""

from __future__ import annotations

import hashlib
import re
import sys


def normalize(snippet: str) -> str:
    lines = []
    for line in snippet.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(re.sub(r"\s+", " ", stripped))
    return "\n".join(lines)


def compute(relpath: str, category: str, snippet: str) -> str:
    material = f"{relpath}\n{category}\n{normalize(snippet)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print("usage: fingerprint.py <relpath> <category> [snippet-file]", file=sys.stderr)
        return 2
    if len(argv) == 4:
        with open(argv[3], encoding="utf-8") as f:
            snippet = f.read()
    else:
        snippet = sys.stdin.read()
    print(compute(argv[1], argv[2], snippet))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
