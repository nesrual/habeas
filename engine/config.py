"""Load and validate config/models.yaml, run.yaml and lenses.yaml (plan 6)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from . import yamlish

SKILL_ROOT = Path(__file__).resolve().parent.parent
# Reasoning effort values in the OpenRouter chat completions schema
# (checked against the docs on 2026-11-22).
EFFORTS = {"max", "xhigh", "high", "medium", "low", "minimal", "none"}
ADAPTERS = {"openai_compat", "native"}
STATIC_CHECKS = ("controller_authorisation", "state_machine_bypass", "write_to_base_class",
                 "cross_engine_model")
ROLES = ["scout", "finder", "trial_finder", "merger", "merge_auditor", "critic",
         "prover", "proof_checker", "writer"]


class ConfigError(Exception):
    pass


def default_config_dir() -> Path:
    return Path(os.environ.get("HABEAS_CONFIG_DIR") or SKILL_ROOT / "config")


def _todos(obj, path: str) -> list[str]:
    if isinstance(obj, dict):
        return [t for k, v in obj.items() for t in _todos(v, f"{path}.{k}")]
    if isinstance(obj, list):
        return [t for i, v in enumerate(obj) for t in _todos(v, f"{path}[{i}]")]
    return [path] if isinstance(obj, str) and "TODO" in obj else []


def is_alias(model: str) -> bool:
    return model.startswith("~") or model.endswith("latest")


class Config:
    def __init__(self, models: dict, run: dict, lenses: dict, config_dir=None,
                 static: dict | None = None, severity: dict | None = None):
        self.models, self.run, self.lenses = models or {}, run or {}, lenses or {}
        self.static, self.severity = static or {}, severity or {}
        self.dir = Path(config_dir) if config_dir else None
        self.hash = hashlib.sha256(json.dumps(
            [self.models, self.run, self.lenses, self.static, self.severity], sort_keys=True).encode()).hexdigest()
        self.errors = self._validate()
        self.todos = self._find_todos()

    # ------------------------------------------------------------ accessors

    def seats(self, role: str, include_disabled: bool = False) -> list[dict]:
        seats = (self.models.get("seats") or {}).get(role) or []
        return [s for s in seats if isinstance(s, dict)
                and (include_disabled or s.get("enabled", True))]

    def endpoint(self, seat: dict) -> dict:
        if seat.get("endpoint") == "native":
            return {"adapter": "native"}
        return (self.models.get("endpoints") or {}).get(seat.get("endpoint")) or {}

    def is_openrouter(self, seat: dict) -> bool:
        """OpenRouter-only request fields (provider routing) apply here."""
        ep = self.endpoint(seat)
        if "openrouter" in ep:
            return bool(ep["openrouter"])
        host = urlparse(str(ep.get("base_url") or "")).hostname or ""
        return seat.get("endpoint") == "openrouter" or host.endswith("openrouter.ai")

    def provider(self, seat: dict) -> dict | None:
        if not self.is_openrouter(seat):
            return None
        return {**(self.models.get("provider_defaults") or {}), **(seat.get("provider") or {})}

    def get(self, dotted: str, default=None):
        node = self.run
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # ----------------------------------------------------------- validation

    def _find_todos(self) -> list[str]:
        out = _todos(self.run, "run")
        out += _todos(self.models.get("endpoints"), "models.endpoints")
        for role in ROLES:
            for seat in self.seats(role):
                out += _todos(seat, f"models.seats.{role}.{seat.get('id', '?')}")
        return out

    def _validate(self) -> list[str]:
        errs: list[str] = []
        endpoints = self.models.get("endpoints") or {}
        for name, ep in endpoints.items():
            if not isinstance(ep, dict) or ep.get("adapter") not in ADAPTERS:
                errs.append(f"endpoint {name}: adapter must be one of {sorted(ADAPTERS)}")
            elif ep["adapter"] == "openai_compat" and not ep.get("base_url"):
                errs.append(f"endpoint {name}: base_url is required")
        seen: set[str] = set()
        for role in ROLES:
            for seat in self.seats(role, include_disabled=True):
                sid = seat.get("id") or "?"
                where = f"seat {sid} ({role})"
                if sid in seen:
                    errs.append(f"{where}: duplicate seat id")
                seen.add(sid)
                if not seat.get("lab"):
                    errs.append(f"{where}: lab is required")
                ep_name = seat.get("endpoint")
                if ep_name != "native" and ep_name not in endpoints:
                    errs.append(f"{where}: unknown endpoint {ep_name!r}")
                if ep_name != "native" and not seat.get("model"):
                    errs.append(f"{where}: model is required")
                for key in ("model", "alternate_model"):
                    model = seat.get(key)
                    if isinstance(model, str) and is_alias(model):
                        errs.append(f"{where}: {key} {model!r} is an alias; pin a fixed model ID")
                general = (self.models.get("refusal_fallback") or {}).get("model") if hasattr(self, "models") else None
                if isinstance(general, str) and is_alias(general) and "refusal_fallback" not in " ".join(errs):
                    errs.append(f"models.refusal_fallback: model {general!r} is an alias; pin a fixed model ID")
                efforts = list(seat.get("effort_schedule") or [])
                if seat.get("effort"):
                    efforts.append(seat["effort"])
                for e in efforts:
                    if e not in EFFORTS:
                        errs.append(f"{where}: unknown effort {e!r}")
                only = (seat.get("provider") or {}).get("only")
                if seat.get("open_weights") and self.is_openrouter(seat) and not only:
                    errs.append(f"{where}: open_weights on openrouter needs a provider.only list")

        critic_labs = {s.get("lab") for s in self.seats("critic")}
        if len(critic_labs) < 2:
            errs.append("critic: needs seats from at least two labs")
        prover_labs = {s.get("lab") for s in self.seats("prover")}
        if len(prover_labs) < 2:
            errs.append("prover: needs enabled seats from at least two labs")
        checker_labs = {s.get("lab") for s in self.seats("proof_checker")}
        if not checker_labs - prover_labs:
            errs.append("proof_checker: needs a seat from a lab other than the provers' labs")

        for role in ("merger", "merge_auditor"):
            if not self.seats(role):
                errs.append(f"{role}: needs an enabled seat")

        lenses = self.run.get("lenses") or []
        runs = self.run.get("runs_per_finder")
        for lens in lenses:
            spec = self.lenses.get(lens)
            if not isinstance(spec, dict) or not spec.get("invariants"):
                errs.append(f"lens {lens}: not defined in lenses.yaml")
            elif spec.get("class") not in ("security", "correctness"):
                errs.append(f"lens {lens}: class must be security or correctness")
        if not isinstance(runs, int) or isinstance(runs, bool) or runs < 1:
            errs.append("run.runs_per_finder: must be a positive integer")
        else:
            if len(lenses) < runs:
                errs.append(f"run.lenses: run i uses lens i, so {runs} runs need {runs} lenses")
            for seat in self.seats("finder") + self.seats("trial_finder"):
                sched = seat.get("effort_schedule")
                if sched is not None and len(sched) < runs:
                    errs.append(f"seat {seat.get('id')}: effort_schedule shorter than runs_per_finder")
        if self.get("critics.drop_rule") != "unanimous":
            errs.append("run.critics.drop_rule: the only allowed value is unanimous")
        for lens in self.get("quarantine.lenses") or []:
            if lens not in self.lenses:
                errs.append(f"run.quarantine.lenses: unknown lens {lens}")
        for key in ("limit_usd", "limit_tokens"):
            v = self.get(f"budget.{key}")
            ok = v is None or (isinstance(v, str) and "TODO" in v) or (
                isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0)
            if not ok:
                errs.append(f"run.budget.{key}: must be a positive number or null")
        for rule in self.static.get("rules") or []:
            rid = rule.get("id", "?") if isinstance(rule, dict) else "?"
            if not isinstance(rule, dict) or rule.get("check") not in STATIC_CHECKS:
                errs.append(f"static rule {rid}: check must be one of {list(STATIC_CHECKS)}")
            elif rule.get("output") != "tech_debt" and (
                    rule.get("lens") not in self.lenses or not rule.get("invariant")):
                errs.append(f"static rule {rid}: needs a lens from lenses.yaml and an invariant")
        if self.get("budget.limit_usd") is None:
            errs.append("run.budget.limit_usd: a starting limit is required")
        return errs

    def require_ready(self) -> None:
        """Every command except `config check` refuses errors and TODO values."""
        problems = self.errors + [f"TODO value at {t}" for t in self.todos]
        if problems:
            raise ConfigError("config is not ready (run `habeas config check`):\n  "
                              + "\n  ".join(problems[:15]))


def load(config_dir=None) -> Config:
    d = Path(config_dir) if config_dir else default_config_dir()
    docs = []
    for name in ("models.yaml", "run.yaml", "lenses.yaml"):
        path = d / name
        try:
            doc = yamlish.load(path)
        except OSError as e:
            hint = (f" (copy {d / 'run.example.yaml'} to {path} and fill in the TODO values)"
                    if name == "run.yaml" and (d / "run.example.yaml").exists() else "")
            raise ConfigError(f"cannot read {path}: {e.strerror}{hint}")
        except yamlish.YamlError as e:
            raise ConfigError(f"{path}: {e}")
        if not isinstance(doc, dict):
            raise ConfigError(f"{path}: expected a map at the top level")
        docs.append(doc)
    extra = {}
    for key, name in (("static", "static_rules.yaml"), ("severity", "severity.yaml")):
        if (d / name).exists():
            try:
                extra[key] = yamlish.load(d / name)
            except yamlish.YamlError as e:
                raise ConfigError(f"{d / name}: {e}")
    return Config(*docs, config_dir=d, **extra)
