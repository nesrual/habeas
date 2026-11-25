"""A line-based index of Ruby classes, modules and methods.

Good enough to follow superclass chains, included concerns and method bodies
in conventionally indented code. It is not a Ruby parser: anything it cannot
resolve is left out, never guessed.
"""

from __future__ import annotations

import re

CONST = r"(?:::)?[A-Z]\w*(?:::[A-Z]\w*)*"
CLASS_RE = re.compile(rf"^(\s*)(class|module)\s+({CONST})(?:\s*<\s*({CONST}))?")
INCLUDE_RE = re.compile(rf"^\s*(?:include|extend|prepend)\s+({CONST}(?:\s*,\s*{CONST})*)")
DEF_RE = re.compile(r"^(\s*)def\s+(self\.)?(\w+[?!=]?)")
END_RE = re.compile(r"^(\s*)end\b")
VIS_RE = re.compile(r"^(\s*)(private|protected|public)\s*$")


def parse(text: str) -> list[dict]:
    """Class and module definitions with qualified names."""
    defs, stack = [], []
    for n, line in enumerate(text.splitlines(), 1):
        m = CLASS_RE.match(line)
        if m:
            name = m.group(3).lstrip(":")
            parents = [d["name"] for _, d in stack[-1:]]
            d = {"name": "::".join(parents + [name]), "kind": m.group(2),
                 "superclass": m.group(4), "includes": [], "line": n, "end_line": n,
                 "namespace": parents[0] if parents else ""}
            defs.append(d)
            if not re.search(r";\s*end\s*$", line):
                stack.append((len(m.group(1)), d))
            continue
        m = INCLUDE_RE.match(line)
        if m and stack:
            stack[-1][1]["includes"] += [c.strip() for c in m.group(1).split(",")]
            continue
        m = END_RE.match(line)
        if m and stack and len(m.group(1)) == stack[-1][0]:
            stack.pop()[1]["end_line"] = n
    return defs


def methods(text: str) -> list[dict]:
    """Instance and class methods with their line span, body and visibility."""
    out, lines = [], text.splitlines()
    visibility: dict[int, str] = {}      # indent of the class body -> current visibility
    i = 0
    while i < len(lines):
        line = lines[i]
        m = VIS_RE.match(line)
        if m:
            visibility[len(m.group(1))] = m.group(2)
        if CLASS_RE.match(line):
            indent = len(line) - len(line.lstrip())
            visibility = {k: v for k, v in visibility.items() if k <= indent}
        m = DEF_RE.match(line)
        if m:
            indent = len(m.group(1))
            end = i
            if not re.search(r"(;\s*end\s*$)|(\)\s*=\s)|(^\s*def\s+\S+\s*=\s)", line):
                for j in range(i + 1, len(lines)):
                    e = END_RE.match(lines[j])
                    if e and len(e.group(1)) == indent:
                        end = j
                        break
            out.append({"name": m.group(3), "class_method": bool(m.group(2)), "line": i + 1,
                        "end_line": end + 1, "indent": indent,
                        "visibility": visibility.get(indent, "public"),
                        "body": "\n".join(lines[i + 1:end])})
            i = end
        i += 1
    return out


def underscore(name: str) -> str:
    short = name.split("::")[-1]
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", short).lower()


class Index:
    def __init__(self):
        self.defs: dict[str, list[tuple[str, dict]]] = {}
        self.by_path: dict[str, list[dict]] = {}
        self.text: dict[str, str] = {}

    def add(self, path: str, text: str) -> None:
        self.text[path] = text
        self.by_path[path] = parse(text)
        for d in self.by_path[path]:
            self.defs.setdefault(d["name"], []).append((path, d))

    def resolve(self, name: str, namespace: str = "") -> tuple[str, dict] | None:
        """(path, definition) of a constant as seen from a namespace, or None."""
        name = name.lstrip(":")
        parts = namespace.split("::") if namespace else []
        for k in range(len(parts), -1, -1):
            hit = self.defs.get("::".join(parts[:k] + [name]))
            if hit:
                return hit[0]
        suffix = [v[0] for q, v in self.defs.items() if q.endswith("::" + name)]
        return suffix[0] if len(suffix) == 1 else None

    def parents(self, d: dict) -> list[tuple[str, dict]]:
        """Resolved superclass and included modules of one definition."""
        out = []
        for name in ([d["superclass"]] if d["superclass"] else []) + d["includes"]:
            hit = self.resolve(name, d["name"].rsplit("::", 1)[0] if "::" in d["name"] else "")
            if hit and hit[1] is not d:
                out.append(hit)
        return out

    def ancestors(self, d: dict, depth: int = 5) -> list[tuple[str, dict]]:
        """Breadth first: direct parents come first."""
        out, seen, level = [], {id(d)}, [d]
        for _ in range(depth):
            nxt = []
            for cur in level:
                for path, parent in self.parents(cur):
                    if id(parent) not in seen:
                        seen.add(id(parent))
                        out.append((path, parent))
                        nxt.append(parent)
            level = nxt
        return out

    def descends_from(self, d: dict, base: str) -> bool:
        base = base.lstrip(":").split("::")[-1]
        cur, seen = d, set()
        while cur and id(cur) not in seen:
            seen.add(id(cur))
            sup = cur.get("superclass")
            if not sup:
                return False
            if sup.lstrip(":").split("::")[-1] == base:
                return True
            hit = self.resolve(sup, cur["name"].rsplit("::", 1)[0] if "::" in cur["name"] else "")
            cur = hit[1] if hit else None
        return False
