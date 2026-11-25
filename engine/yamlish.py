"""YAML subset parser for the engine config (PyYAML is not stdlib).

Supported: nested block maps and lists (including lists of maps), one-line
flow maps and lists (`{a: 1, b: [x, y]}`), plain and quoted scalars, null,
booleans, integers, floats, comments. JSON on one line is valid input.

Not supported, and rejected rather than misread: tabs for indentation,
anchors and aliases, tags, multi-line strings, multi-line flow collections.
"""

from __future__ import annotations

import json
import re


class YamlError(ValueError):
    pass


_KEY = re.compile(r'''^("(?:[^"\\]|\\.)*"|'[^']*'|[^\s:\[\]{}"'#,&*!|>][^:]*?)\s*:(?:\s+(.*)|)$''')
_INT = re.compile(r"^-?\d+$")
_FLOAT = re.compile(r"^-?\d+\.\d+$")


def _strip_comment(line: str) -> str:
    quote = None
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 1
            elif c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
        i += 1
    return line


def _unquote(s: str, n: int) -> str:
    if s.startswith('"'):
        try:
            v = json.loads(s)
        except ValueError:
            raise YamlError(f"line {n}: bad double-quoted string: {s}")
        if not isinstance(v, str):
            raise YamlError(f"line {n}: bad string: {s}")
        return v
    if not (len(s) >= 2 and s.endswith("'")):
        raise YamlError(f"line {n}: bad single-quoted string: {s}")
    return s[1:-1].replace("''", "'")


def _scalar(s: str, n: int):
    s = s.strip()
    if s[:1] in ('"', "'"):
        return _unquote(s, n)
    if s in ("", "null", "Null", "NULL", "~"):
        return None
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if _INT.match(s):
        return int(s)
    if _FLOAT.match(s):
        return float(s)
    return s


class _Flow:
    def __init__(self, text: str, n: int):
        self.t, self.n, self.p = text, n, 0

    def err(self, what):
        raise YamlError(f"line {self.n}: {what} at column {self.p + 1}: {self.t}")

    def ws(self):
        while self.p < len(self.t) and self.t[self.p] == " ":
            self.p += 1

    def peek(self):
        return self.t[self.p] if self.p < len(self.t) else ""

    def quoted(self):
        q, start = self.t[self.p], self.p
        self.p += 1
        while self.p < len(self.t):
            c = self.t[self.p]
            if c == "\\" and q == '"':
                self.p += 1
            elif c == q:
                self.p += 1
                return _unquote(self.t[start:self.p], self.n)
            self.p += 1
        self.err("unterminated string")

    def value(self, stops: str):
        self.ws()
        c = self.peek()
        if c == "{":
            return self.map()
        if c == "[":
            return self.seq()
        if c and c in "\"'":
            return self.quoted()
        start = self.p
        while self.p < len(self.t) and self.t[self.p] not in stops:
            self.p += 1
        return _scalar(self.t[start:self.p], self.n)

    def map(self):
        self.p += 1
        out = {}
        while True:
            self.ws()
            if self.peek() == "}":
                self.p += 1
                return out
            if self.peek() and self.peek() in "\"'":
                key = self.quoted()
            else:
                start = self.p
                while self.p < len(self.t) and self.t[self.p] not in ":,}":
                    self.p += 1
                key = self.t[start:self.p].strip()
            self.ws()
            if self.peek() != ":" or not key:
                self.err("expected 'key: value'")
            self.p += 1
            if key in out:
                self.err(f"duplicate key {key!r}")
            out[key] = self.value(",}")
            self.ws()
            if self.peek() == ",":
                self.p += 1
            elif self.peek() != "}":
                self.err("expected ',' or '}'")

    def seq(self):
        self.p += 1
        out = []
        while True:
            self.ws()
            if self.peek() == "]":
                self.p += 1
                return out
            out.append(self.value(",]"))
            self.ws()
            if self.peek() == ",":
                self.p += 1
            elif self.peek() != "]":
                self.err("expected ',' or ']'")


def _value(s: str, n: int):
    s = s.strip()
    if s[:1] in ("{", "["):
        f = _Flow(s, n)
        v = f.value("")
        f.ws()
        if f.p != len(s):
            f.err("trailing text")
        return v
    if s[:1] in ("|", ">", "&", "*", "!"):
        raise YamlError(f"line {n}: unsupported YAML feature: {s}")
    return _scalar(s, n)


def _is_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


class _Parser:
    def __init__(self, lines):
        self.lines, self.i = lines, 0

    def block(self, indent: int):
        ind, text, n = self.lines[self.i]
        if ind != indent:
            raise YamlError(f"line {n}: unexpected indentation")
        if _is_item(text):
            return self.seq(indent)
        if text[0] in "{[":
            self.i += 1
            return _value(text, n)
        return self.map(indent)

    def _nested(self, indent: int, allow_same_indent_seq: bool):
        if self.i < len(self.lines):
            nind, ntext, _ = self.lines[self.i]
            if nind > indent:
                return self.block(nind)
            if allow_same_indent_seq and nind == indent and _is_item(ntext):
                return self.seq(indent)
        return None

    def map(self, indent: int):
        out = {}
        while self.i < len(self.lines):
            ind, text, n = self.lines[self.i]
            if ind < indent or (ind == indent and _is_item(text)):
                break
            if ind > indent:
                raise YamlError(f"line {n}: unexpected indentation")
            m = _KEY.match(text)
            if not m:
                raise YamlError(f"line {n}: expected 'key: value': {text}")
            key = m.group(1)
            key = _unquote(key, n) if key[0] in "\"'" else key.strip()
            if key in out:
                raise YamlError(f"line {n}: duplicate key {key!r}")
            self.i += 1
            rest = m.group(2)
            out[key] = _value(rest, n) if rest else self._nested(indent, True)
        return out

    def seq(self, indent: int):
        out = []
        while self.i < len(self.lines):
            ind, text, n = self.lines[self.i]
            if ind > indent:
                raise YamlError(f"line {n}: unexpected indentation")
            if ind < indent or not _is_item(text):
                break
            rest = text[1:].lstrip(" ")
            if not rest:
                self.i += 1
                out.append(self._nested(indent, False))
            elif rest[0] not in "{[\"'" and _KEY.match(rest):
                col = indent + len(text) - len(rest)
                self.lines[self.i] = [col, rest, n]
                out.append(self.map(col))
            else:
                self.i += 1
                out.append(_value(rest, n))
        return out


def loads(text: str):
    lines = []
    for n, raw in enumerate(text.splitlines(), 1):
        lead = raw[:len(raw) - len(raw.lstrip())]
        if "\t" in lead:
            raise YamlError(f"line {n}: tab in indentation")
        s = _strip_comment(raw).rstrip()
        if not s.strip() or s.strip() == "---":
            continue
        lines.append([len(s) - len(s.lstrip(" ")), s.strip(), n])
    if not lines:
        return None
    p = _Parser(lines)
    value = p.block(lines[0][0])
    if p.i < len(lines):
        raise YamlError(f"line {lines[p.i][2]}: unexpected content: {lines[p.i][1]}")
    return value


def load(path):
    with open(path, encoding="utf-8") as f:
        return loads(f.read())
