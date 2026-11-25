"""Prompt files under prompts/deep/. Each has a system part and a user part,
split by a line holding ---INPUT---. The version is a content hash, so an edit
to a prompt changes the idempotency key of every call that uses it."""

from __future__ import annotations

import hashlib

from .config import SKILL_ROOT

SPLIT = "\n---INPUT---\n"


def load(name: str) -> tuple[str, str, str]:
    text = (SKILL_ROOT / "prompts" / "deep" / name).read_text(encoding="utf-8")
    system, user = text.split(SPLIT, 1)
    return system, user, hashlib.sha256(text.encode()).hexdigest()[:12]


def fill(template: str, values: dict) -> str:
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template
