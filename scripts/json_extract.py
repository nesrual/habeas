#!/usr/bin/env python3
"""Pull the first valid JSON value out of model output (plan section 7.2).

Scans the text for the first "{" or "[" from which json can decode a complete
value; prose, code-fence markers, and broken JSON before it are skipped. Only
objects and arrays are considered (a bare scalar in prose is never the
payload).

CLI: json_extract.py [file]   (stdin when omitted)
Prints the extracted value re-serialized as compact JSON. Exit 1 when no JSON
value is found.
"""

from __future__ import annotations

import json
import sys


def extract(text: str):
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch not in "{[":
            continue
        try:
            value, _end = decoder.raw_decode(text, i)
        except ValueError:
            continue
        return value
    raise ValueError("no JSON object or array found in input")


def main(argv: list[str]) -> int:
    strict = argv[1:2] == ["--strict"]
    if strict:
        argv = [argv[0], *argv[2:]]
    if len(argv) > 2:
        print("usage: json_extract.py [file]", file=sys.stderr)
        return 2
    if len(argv) == 2:
        with open(argv[1], encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    try:
        value = json.loads(text) if strict else extract(text)
        if not isinstance(value, (dict, list)):
            raise ValueError("expected one JSON object or array")
    except ValueError as e:
        print(f"json_extract: {e}", file=sys.stderr)
        return 1
    print(json.dumps(value, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
