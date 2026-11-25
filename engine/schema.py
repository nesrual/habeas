"""Minimal JSON-schema check for model output (jsonschema is not stdlib).

Supports: type, properties, required, additionalProperties: false, items,
enum, maxLength, minimum, maximum. Enough for the engine's own schemas.
"""

from __future__ import annotations

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "integer": int, "number": (int, float), "null": type(None),
}


def errors(value, schema: dict, path: str = "$") -> list[str]:
    out: list[str] = []
    types = schema.get("type")
    if types:
        types = [types] if isinstance(types, str) else types
        ok = any(isinstance(value, _TYPES[t])
                 and not (t in ("integer", "number") and isinstance(value, bool))
                 for t in types)
        if not ok:
            return [f"{path}: expected {'|'.join(types)}"]
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: not one of {schema['enum']}")
    if isinstance(value, str) and len(value) > schema.get("maxLength", len(value)):
        out.append(f"{path}: longer than {schema['maxLength']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{path}: above {schema['maximum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                out.append(f"{path}: missing {key}")
        for key, sub in value.items():
            if key in props:
                out += errors(sub, props[key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                out.append(f"{path}: unexpected {key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            out += errors(item, schema["items"], f"{path}[{i}]")
    return out


_LOCATION = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "start_line": {"type": "integer", "minimum": 1},
        "end_line": {"type": "integer", "minimum": 1},
    },
    "required": ["path", "start_line", "end_line"],
    "additionalProperties": False,
}

# Plan 8.3. Every property is required and additionalProperties is false, as
# strict structured output demands.
FINDER = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},      # cut to 120 characters when stored
                    "class": {"type": "string", "enum": ["security", "correctness"]},
                    "invariant": {"type": "string"},
                    "violation": {"type": "string"},
                    "locations": {"type": "array", "items": _LOCATION},
                    "trigger": {"type": "string"},
                    "test_sketch": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["title", "class", "invariant", "violation", "locations",
                             "trigger", "test_sketch", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["candidates"],
    "additionalProperties": False,
}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props),
            "additionalProperties": False}


SEVERITIES = ["critical", "high", "medium", "low"]
MERGE = _obj({"same_root_cause": {"type": "boolean"}, "reason": {"type": "string"}})
# One call sorts a whole group of nearby reports: each entry lists the numbers of reports
# that share a root cause. Reports not listed stay alone.
PARTITION = _obj({"groups": {"type": "array", "items": _obj({
    "reports": {"type": "array", "items": {"type": "integer", "minimum": 1}},
    "root_cause": {"type": "string"}})}})
CRITIC = _obj({"verdict": {"type": "string", "enum": ["refute", "uphold", "unsure"]},
               "reason": {"type": "string"}})
PROOF_CHECK = _obj({
    "demonstrates_claim": {"type": "boolean"},
    "fix_minimal_at_root_cause": {"type": "boolean"},
    "verdict": {"type": "string", "enum": ["accept", "reject"]},
    "severity": {"type": "string", "enum": SEVERITIES},
    "reason": {"type": "string"}})
# One step of a scripted prover. It may look at the clone (read, list, search) before it
# submits a test plus edits, or declines. Every field is always present; unused ones are empty.
PROVER = _obj({
    "action": {"type": "string", "enum": ["read", "list", "search", "submit", "decline"]},
    "paths": {"type": "array", "items": {"type": "string"}},
    "query": {"type": "string"},
    "reason": {"type": "string"},
    "test_file": {"type": "string"},
    "test_package": {"type": "string"},
    "edits": {"type": "array", "items": _obj({
        "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}})},
})
WRITER = _obj({"title": {"type": "string"},
               "severity": {"type": "string", "enum": SEVERITIES},
               "how_it_breaks": {"type": "string"}})
