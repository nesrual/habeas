#!/usr/bin/env python3
"""Mechanical helpers behind prove.sh (plan 7.6 / 9). Stdlib only.

Subcommands:
  check <log> [--pattern <regex>] [--green]   gate A/B verdict for one run log
  compare <log1> <log2>                       determinism check across two runs
  finding-info <finding.json> --fix-out <p> [--shell]
                                              extract proof fields; write fix
                                              patch when fix_sketch is a diff
  run --cwd D --log F --timeout S --cmd C
      [--scrub --allowlist A,B] [--set K=V]... [--unshare auto|off]
                                              execute one proof run sandboxed

All subcommands print JSON (finding-info with --shell prints shell
assignments instead). prove.sh is the only intended caller, but every
subcommand is independently testable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import uuid
import sys
from pathlib import Path

SUMMARY_RE = re.compile(
    r"^(?P<ex>\d+) examples?, (?P<fail>\d+) failures?"
    r"(?:, (?P<pend>\d+) pending)?"
    r"(?:, (?P<err>\d+) errors? occurred outside of examples)?\s*$",
    re.MULTILINE)

DIFF_RE = re.compile(r"^--- ", re.MULTILINE)
DIFF_RE2 = re.compile(r"^\+\+\+ ", re.MULTILINE)
DIFF_RE3 = re.compile(r"^@@ ", re.MULTILINE)


def _parse_rspec(text: str) -> dict:
    summary = None
    for summary in SUMMARY_RE.finditer(text):
        pass  # the last summary line wins (some suites print several)
    first_failure = ""
    fm = re.search(r"^Failures:\s*$(.*?)(?=^Finished in|\Z)", text, re.M | re.S)
    if fm:
        f1 = re.search(r"^\s*1\)(.*?)(?=^\s*2\)|\Z)", fm.group(1), re.M | re.S)
        if f1:
            first_failure = f1.group(1).strip()
    return {
        "summary_found": summary is not None,
        "examples": int(summary.group("ex")) if summary else 0,
        "failures": int(summary.group("fail")) if summary else 0,
        "errors_outside": int(summary.group("err") or 0) if summary else 0,
        "first_failure": first_failure,
    }


PYTEST_SUMMARY_RE = re.compile(r"^=+ (.+?) in [\d.]+s.*=+\s*$", re.M)


def _parse_pytest(text: str) -> dict:
    summary = None
    for summary in PYTEST_SUMMARY_RE.finditer(text):
        pass
    counts = {}
    if summary:
        for n, kind in re.findall(r"(\d+) (failed|passed|error|errors|skipped|"
                                  r"xfailed|xpassed|deselected)", summary.group(1)):
            counts[kind.rstrip("s") if kind == "errors" else kind] = int(n)
    failures = counts.get("failed", 0)
    examples = failures + counts.get("passed", 0) + counts.get("xfailed", 0) \
        + counts.get("xpassed", 0)
    first_failure = ""
    fm = re.search(r"^=+ FAILURES =+\s*$(.*?)(?=^=+ [\w ]+ =+\s*$|\Z)", text, re.M | re.S)
    if fm:
        f1 = re.search(r"^_+ .+? _+\s*$(.*?)(?=^_+ .+? _+\s*$|\Z)",
                       fm.group(0), re.M | re.S)
        if f1:
            first_failure = f1.group(0).strip()
    return {
        "summary_found": summary is not None,
        "examples": examples,
        "failures": failures,
        "errors_outside": counts.get("error", 0),
        "first_failure": first_failure,
    }


JEST_TESTS_RE = re.compile(r"^Tests:\s+(.*?)\s*$", re.M)


def _parse_jest(text: str) -> dict:
    summary = None
    for summary in JEST_TESTS_RE.finditer(text):
        pass
    counts = {}
    if summary:
        for n, kind in re.findall(r"(\d+) (failed|passed|skipped|todo|total)", summary.group(1)):
            counts[kind] = int(n)
    suite_error = bool(re.search(r"^\s*● Test suite failed to run", text, re.M))
    first_failure = ""
    blocks = re.split(r"^  ● ", text, flags=re.M)
    for b in blocks[1:]:
        if b.startswith("Test suite failed to run"):
            continue
        b = re.split(r"^(?:Test Suites:|Tests:|Snapshots:|Time:)", b, maxsplit=1, flags=re.M)[0]
        first_failure = b.strip()
        break
    return {
        "summary_found": summary is not None,
        "examples": counts.get("total", 0),
        "failures": counts.get("failed", 0),
        "errors_outside": 1 if suite_error else 0,
        "first_failure": first_failure,
    }


PARSERS = {"rspec": _parse_rspec, "pytest": _parse_pytest, "jest": _parse_jest}


def parse_log(text: str, framework: str = "rspec") -> dict:
    parser = PARSERS.get(framework)
    if parser is None:
        raise ValueError(f"unknown proof.framework: {framework} "
                         f"(known: {', '.join(sorted(PARSERS))})")
    return parser(text)


def gate_check(parsed: dict, pattern: str | None, green: bool) -> dict:
    out = dict(parsed)
    if not parsed["summary_found"]:
        gate_a, reason = False, "no_summary"
    elif parsed["errors_outside"] > 0:
        gate_a, reason = False, "errors_outside_examples"
    elif parsed["examples"] == 0:
        gate_a, reason = False, "zero_examples"
    else:
        gate_a, reason = True, None
    out["gate_a"] = gate_a

    if green:
        ok = gate_a and parsed["failures"] == 0
        out["green"] = ok
        out["reason"] = reason if not gate_a else (None if ok else "still_failing")
        out["ok"] = ok
        return out

    gate_b = False
    if gate_a:
        if parsed["failures"] < 1:
            reason = "zero_failures"
        elif pattern and not re.search(pattern, parsed["first_failure"], re.IGNORECASE):
            reason = "pattern_mismatch"
        else:
            gate_b = True
    out["gate_b"] = gate_b
    out["reason"] = reason
    out["ok"] = gate_a and gate_b
    return out


def normalize(text: str) -> str:
    text = re.sub(r"0x[0-9a-fA-F]+", "0xX", text)
    text = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
                  r"(?:Z|[+-]\d{2}:?\d{2})?", "TS", text)
    text = re.sub(r"\b\d{2}:\d{2}:\d{2}\b", "TS", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:seconds?|ms)\b", "DUR", text)
    text = re.sub(r"\bin [\d.]+s\b", "in DUR", text)
    text = re.sub(r"^Finished in .*$", "", text, flags=re.M)
    text = re.sub(r"Randomized with seed \d+", "SEED", text)
    return text


def cmd_check(args) -> dict:
    parsed = parse_log(Path(args.log).read_text(encoding="utf-8", errors="replace"),
                       args.framework)
    return gate_check(parsed, args.pattern, args.green)


def cmd_compare(args) -> dict:
    p1 = parse_log(Path(args.log1).read_text(encoding="utf-8", errors="replace"),
                   args.framework)
    p2 = parse_log(Path(args.log2).read_text(encoding="utf-8", errors="replace"),
                   args.framework)
    if p1["failures"] != p2["failures"]:
        return {"deterministic": False,
                "reason": f"failure counts differ: {p1['failures']} vs {p2['failures']}"}
    if normalize(p1["first_failure"]) != normalize(p2["first_failure"]):
        return {"deterministic": False, "reason": "first failure message differs between runs"}
    return {"deterministic": True, "reason": None}


def cmd_finding_info(args):
    record = json.loads(Path(args.finding).read_text(encoding="utf-8"))
    if "record" in record and isinstance(record["record"], dict):
        record = record["record"]  # accept a full state entry too
    fingerprint = record.get("fingerprint")
    category = record.get("category")
    sketch = record.get("proof_sketch") or {}
    ptype = sketch.get("type", "rspec")
    pattern = sketch.get("expected_failure_pattern", "")
    if not fingerprint or not category:
        raise ValueError("finding record needs fingerprint and category")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", fingerprint):
        raise ValueError("invalid fingerprint")
    if ptype not in ("rspec", "pytest", "jest", "http"):
        raise ValueError("unknown proof type")
    if ptype in ("rspec", "jest", "pytest") and not pattern:
        raise ValueError(f"{ptype} proof requires proof_sketch.expected_failure_pattern")
    if not isinstance(pattern, str):
        raise ValueError("expected_failure_pattern must be a string")
    try:
        re.compile(pattern)
    except re.error as err:
        raise ValueError("invalid expected_failure_pattern") from err
    fix = record.get("fix_sketch") or ""
    has_fix = bool(isinstance(fix, str) and DIFF_RE.search(fix)
                   and DIFF_RE2.search(fix) and DIFF_RE3.search(fix))
    if has_fix and args.fix_out:
        Path(args.fix_out).write_text(fix if fix.endswith("\n") else fix + "\n",
                                      encoding="utf-8")
    info = {"fingerprint": fingerprint, "category": category, "type": ptype,
            "pattern": pattern, "has_fix": has_fix, "file": record.get("file") or ""}
    if args.shell:
        for key, var in [("fingerprint", "FP"), ("category", "CATEGORY"),
                         ("type", "PTYPE"), ("pattern", "PATTERN"), ("file", "FLAGGED")]:
            print(f"{var}={shlex.quote(str(info[key]))}")
        print(f"HAS_FIX={'1' if has_fix else '0'}")
        return None
    return info


def check_fix(args):
    """Only allow content edits to the tracked subject of this finding."""
    from paths import source_path
    cwd = Path(args.cwd).resolve()
    target = source_path(cwd, args.file)
    if not args.file or not target.is_file() or target.relative_to(cwd).as_posix() != args.file:
        raise ValueError("fix must modify the flagged file")
    git = ["git", "--literal-pathspecs", "-C", str(cwd)]
    tracked = subprocess.run([*git, "ls-files", "--error-unmatch", "--", args.file],
                             capture_output=True)
    if tracked.returncode:
        raise ValueError("fix must modify a tracked source file")
    stats = subprocess.check_output([*git, "apply", "--numstat", "-z", "--", args.patch])
    counts = stats.removesuffix(b"\0").split(b"\t", 2)
    if len(counts) != 3 or counts[2] != os.fsencode(args.file):
        raise ValueError("fix must touch only the flagged file")
    if subprocess.check_output([*git, "apply", "--summary", "--", args.patch]):
        raise ValueError("fix may only edit file content")


def cmd_run(args) -> dict:
    if args.scrub:
        allow = [a for a in args.allowlist.split(",") if a]
        env = {k: os.environ[k] for k in allow if k in os.environ}
    else:
        env = dict(os.environ)
    for kv in args.set or []:
        k, _, v = kv.partition("=")
        env[k] = v
    cmd = ["bash", "-c", args.cmd]
    isolated = False
    container = None
    if args.container_image:
        container = "habeas-proof-" + uuid.uuid4().hex
        cmd = ["docker", "run", "--name", container, "--rm", "--pull=never", "--network", "none",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "-v", f"{Path(args.cwd).resolve()}:/source:ro", "-w", "/work",
               "--entrypoint", "bash", "--", args.container_image, "-c",
               'cp -R /source/. /work/ && exec bash -c "$1"', "habeas", args.cmd]
        isolated = True
    elif args.unshare == "auto" and sys.platform.startswith("linux") and shutil.which("unshare"):
        cmd = ["unshare", "-n", "--", *cmd]
        isolated = True
    with open(args.log, "wb") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, cwd=args.cwd, env=env,
                                start_new_session=True)
        try:
            rc = proc.wait(timeout=float(args.timeout))
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            if container:
                # Killing the Docker client does not terminate the container.
                subprocess.run(["docker", "rm", "--force", container], timeout=15,
                               stdout=log, stderr=subprocess.STDOUT)
            rc = 124
            log.write(f"\nprove.sh: hard timeout ({args.timeout}s) expired\n".encode())
    return {"exit": rc, "network_isolated": isolated}


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(prog="prove_gates.py")
    sub = ap.add_subparsers(dest="subcmd", required=True)

    p = sub.add_parser("check")
    p.add_argument("log")
    p.add_argument("--pattern", default=None)
    p.add_argument("--green", action="store_true")
    p.add_argument("--framework", default="rspec")

    p = sub.add_parser("compare")
    p.add_argument("log1")
    p.add_argument("log2")
    p.add_argument("--framework", default="rspec")

    p = sub.add_parser("finding-info")
    p.add_argument("finding")
    p.add_argument("--fix-out", default=None)
    p.add_argument("--shell", action="store_true")

    p = sub.add_parser("check-fix")
    p.add_argument("--cwd", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--patch", required=True)

    p = sub.add_parser("run")
    p.add_argument("--cwd", required=True)
    p.add_argument("--log", required=True)
    p.add_argument("--timeout", required=True)
    p.add_argument("--cmd", required=True)
    p.add_argument("--container-image")
    p.add_argument("--scrub", action="store_true")
    p.add_argument("--allowlist", default="")
    p.add_argument("--set", action="append", default=[])
    p.add_argument("--unshare", choices=["auto", "off"], default="off")

    args = ap.parse_args(argv[1:])
    try:
        result = {"check": cmd_check, "compare": cmd_compare,
                  "finding-info": cmd_finding_info, "check-fix": check_fix, "run": cmd_run}[args.subcmd](args)
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        print(f"prove_gates: {e}", file=sys.stderr)
        return 1
    if result is not None:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
