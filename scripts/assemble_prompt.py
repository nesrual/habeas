#!/usr/bin/env python3
"""Assemble a role prompt: inline negative-example sections (plan 7.5).

Replaces the `{{NEGATIVE_EXAMPLES}}` marker line in a prompt file with the
`## all` section plus each requested category section from
references/negative-examples.md. Keeps a single source of truth: the negative
list grows without editing prompt files.

usage: assemble_prompt.py <prompt_file> [--categories bug,security]
                          [--refs <negative-examples.md>]

Output on stdout. A prompt without the marker passes through unchanged.
Unknown category names are an error (typo protection).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MARKER = "{{NEGATIVE_EXAMPLES}}"
KNOWN = ["all", "bug", "security", "improvement", "test-gap"]


def sections(refs_text: str) -> dict:
    out: dict[str, list] = {}
    current = None
    for line in refs_text.splitlines():
        m = re.match(r"^## (\S+)\s*$", line)
        if m:
            current = m.group(1)
            out[current] = []
        elif current is not None:
            out[current].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def assemble(prompt_text: str, refs_text: str, categories: list) -> str:
    for c in categories:
        if c not in KNOWN:
            raise ValueError(f"unknown category: {c} (known: {', '.join(KNOWN)})")
    if MARKER not in prompt_text:
        return prompt_text
    secs = sections(refs_text)
    parts = []
    for name in ["all"] + [c for c in categories if c != "all"]:
        body = secs.get(name, "").strip()
        if body:
            parts.append(f"### Not an issue ({name})\n\n{body}")
    return prompt_text.replace(MARKER, "\n\n".join(parts) if parts else "(none)")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt_file")
    ap.add_argument("--categories", default="")
    ap.add_argument("--refs", default=str(Path(__file__).resolve().parent.parent
                                          / "references" / "negative-examples.md"))
    args = ap.parse_args(argv[1:])
    cats = [c.strip() for c in args.categories.split(",") if c.strip()]
    try:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
        refs = Path(args.refs).read_text(encoding="utf-8")
        sys.stdout.write(assemble(prompt, refs, cats))
    except (OSError, ValueError) as e:
        print(f"assemble_prompt: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
