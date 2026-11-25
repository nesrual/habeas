"""The proof gate (plan 11): mechanical red-green verification in the audit
clone, then the proof check by a lab that did not write the proof.

The only things executed here are the configured test command and `git`.
Nothing a model proposes is run as a command; a model's test file and patch
are data until the test command runs them in the audit clone.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import calls, find, ledger, prompts, schema
from .calls import CallSpec
from .merge import card

SUMMARY_RE = re.compile(r"^(\d+) examples?, (\d+) failures?(?:, \d+ pending)?"
                        r"(?:, (\d+) errors? occurred outside of examples)?\s*$", re.M)
EXCEPTION_LINE_RE = re.compile(r"^\s+(?:[A-Z]\w*::)*[A-Z]\w*:\s*$", re.M)
FAILED_EXAMPLE_RE = re.compile(r"^rspec (\S+?)(?::\d+|\[[\d:]+\]) # (.*)$", re.M)
ENV_KEEP = re.compile(r"^(PATH|HOME|USER|LOGNAME|SHELL|LANG|LC_\w+|TMPDIR|TZ|GEM_\w+|BUNDLE_\w+|"
                      r"RUBY\w*|MY_RUBY_HOME|IRBRC|rvm_\w+|RBENV_\w+|ASDF_\w+|DOCKER_HOST)$")


class VerifyRefused(Exception):
    """A precondition failed. No attempt is used up."""


# ----------------------------------------------------------------- log parsing

def _raised(block: str) -> bool:
    """True when the failure block reports an exception rather than an expectation that did
    not hold. RSpec prints the exception class on a line of its own at the indentation of
    `Failure/Error:`; deeper lines such as `Diff:` belong to an expectation message."""
    head = re.search(r"^(\s+)Failure/Error:", block, re.M)
    for m in EXCEPTION_LINE_RE.finditer(block):
        line = m.group(0).strip("\n")
        if line.strip() == "Diff:":
            continue
        if head is None or len(line) - len(line.lstrip()) == len(head.group(1).strip("\n")):
            return True
    return False


def parse_rspec(text: str) -> dict:
    summary = None
    for summary in SUMMARY_RE.finditer(text):
        pass
    # Failure blocks start with "  1) description". The default formatter prints them under a
    # "Failures:" header; custom progress formatters print them inline, so do not rely on it.
    body = re.split(r"^Finished in ", text, flags=re.M)[0]
    section = re.search(r"^Failures:\s*$(.*)", body, re.M | re.S)
    blocks = re.split(r"^\s+\d+\) ", section.group(1) if section else body, flags=re.M)[1:]
    return {
        "summary": summary is not None,
        "examples": int(summary.group(1)) if summary else 0,
        "failures": int(summary.group(2)) if summary else 0,
        "errors_outside": int(summary.group(3) or 0) if summary else 0,
        "assertion_failures": sum(1 for b in blocks if not _raised(b)),
        "blocks": len(blocks),
        "failed_examples": {f"{m.group(1)} # {m.group(2)}" for m in FAILED_EXAMPLE_RE.finditer(text)},
    }


def classify(exit_code, text: str) -> str:
    """red | green | error. Red means: the suite ran and every failure is an
    assertion that did not hold. A load error, a syntax error or a missing
    constant is `error`, never red."""
    if exit_code == "timeout":
        return "timeout"
    p = parse_rspec(text)
    if not p["summary"] or p["errors_outside"] or p["examples"] < 1:
        return "error"
    if p["failures"] == 0:
        return "green" if exit_code == 0 else "error"
    if exit_code == 0 or p["blocks"] != p["failures"] or p["assertion_failures"] != p["failures"]:
        return "error"
    return "red"


# -------------------------------------------------------------------- helpers

def git(repo, *args, check=True, input=None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True,
                          text=True, input=input)


def clone_dirty(repo) -> str:
    return git(repo, "status", "--porcelain", "--untracked-files=all").stdout.strip()


def package_root(path: str) -> str:
    m = re.match(r"((?:engines|gems)/[^/]+)/", path)
    return m.group(1) if m else ""


PACKAGE_RE = re.compile(r"(?:engines|gems)/[A-Za-z0-9_-]+")


def test_destination(cluster_id: str, unit_path: str, package: str | None = None) -> str:
    """Where the proof test goes: the claimed code's package, or `package` when the prover
    names the package that owns the code to fix."""
    root = package.strip().strip("/") if package and package.strip() else package_root(unit_path)
    return f"{root + '/' if root else ''}spec/habeas_proofs/{cluster_id}_spec.rb"


def test_argv(cfg, paths: list[str]) -> list[str]:
    root = package_root(paths[0])
    if root.startswith("engines/"):
        template = cfg.get("target.engine_test_command")
        values = {"engine": root.split("/")[1], "paths": "\0"}
    else:
        template = cfg.get("target.root_test_command")
        values = {"paths": "\0"}
    if not template:
        raise VerifyRefused("no test command configured for " + paths[0])
    argv = []
    for part in shlex.split(template):
        if "{paths}" in part:
            argv += paths
        else:
            argv.append(part.format(**{k: v for k, v in values.items() if k != "paths"}))
    return argv


def test_env(cfg) -> dict:
    """A reduced environment: no API keys or other credentials reach the tests."""
    extra = set(cfg.get("target.proof_environment.env_allowlist") or [])
    env = {k: v for k, v in os.environ.items() if ENV_KEEP.match(k) or k in extra}
    env.update(RAILS_ENV="test", RACK_ENV="test")
    return env


def run_tests(repo, argv, timeout: float, log: Path, env: dict):
    """Run the test command. Returns (exit code or "timeout", output)."""
    with open(log, "wb") as out:
        proc = subprocess.Popen(argv, cwd=repo, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, start_new_session=True)
        try:
            code = proc.wait(timeout=max(timeout, 1))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
            code = "timeout"
    return code, log.read_text(encoding="utf-8", errors="replace")


def patch_info(text: str) -> dict:
    files, changed = [], 0
    for line in text.splitlines():
        m = re.match(r"^(?:\+\+\+|---) (?:[ab]/)?(\S+)", line)
        if m:
            if m.group(1) != "/dev/null" and m.group(1) not in files:
                files.append(m.group(1))
        elif line[:1] in "+-" and not line.startswith(("+++", "---")):
            changed += 1
    return {"files": files, "changed_lines": changed}


def related_specs(repo: Path, files: list[str]) -> list[str]:
    """Existing specs for the files a fix touches, in the same engine."""
    out = []
    for f in files:
        for a, b in (("/app/", "/spec/"), ("/lib/", "/spec/lib/")):
            spec = re.sub(r"\.rb$", "_spec.rb", ("/" + f).replace(a, b, 1).lstrip("/"))
            if spec != f and (repo / spec).is_file() and spec not in out:
                out.append(spec)
    return out


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ----------------------------------------------------------- mechanical check

def preconditions(conn, cfg, repo: Path) -> None:
    run = ledger.run(conn)
    if run["state"] != "running":
        raise VerifyRefused(f"run is {run['state']}")
    env = cfg.get("target.proof_environment") or {}
    if env.get("run_tests_in") != "dev_container" and os.environ.get("HABEAS_ALLOW_HOST_PROOFS") != "1":
        raise VerifyRefused("proofs run model-written tests with your user's permissions; "
                            "set HABEAS_ALLOW_HOST_PROOFS=1 to allow it for this command")
    check = env.get("network_check_command")
    if check:
        if subprocess.run(shlex.split(check), cwd=repo, capture_output=True, env=test_env(cfg)).returncode != 0:
            raise VerifyRefused("network_check_command failed: the test setup does not block outbound HTTP")
    elif not env.get("outbound_http_blocked") or "TODO" in str(env.get("outbound_http_blocked")):
        raise VerifyRefused("confirm that the test setup blocks unrecorded outbound HTTP: set "
                            "target.proof_environment.network_check_command or outbound_http_blocked")
    if git(repo, "rev-parse", "HEAD").stdout.strip() != run["repo_commit"]:
        raise VerifyRefused("the audit clone is not at the audited commit")
    dirty = clone_dirty(repo)
    if dirty:
        raise VerifyRefused(f"the audit clone is not clean:\n{dirty[:500]}")


def mechanical(cfg, repo: Path, lens: str, dest: str, test_src: Path, patch_src: Path,
               out_dir: Path) -> dict:
    """Red-green on a clean clone. Always restores the clone. Returns
    {"result": "pass" | "fail" | "timeout", "reason", logs...}."""
    pc = cfg.run.get("provers") or {}
    deadline = time.monotonic() + float(pc.get("timeout_minutes") or 30) * 60
    patch_text = patch_src.read_text(encoding="utf-8", errors="replace")
    info = patch_info(patch_text)
    res = {"result": "fail", "reason": None, "red_log": None, "green_log": None, "regress_log": None,
           "fix_files": info["files"], "fix_lines": info["changed_lines"]}

    def fail(reason, result="fail"):
        res.update(result=result, reason=reason)
        return res

    for f in info["files"]:
        parts = Path(f).parts
        if Path(f).is_absolute() or ".." in parts or ".git" in parts:
            return fail(f"fix_path_not_allowed: {f}")
        if f == dest or "habeas_proofs" in parts:
            return fail("fix_touches_test")
    if not info["files"]:
        return fail("fix_is_not_a_patch")
    if len(info["files"]) > int(pc.get("max_fix_files") or 3) or \
            info["changed_lines"] > int(pc.get("max_fix_lines") or 40):
        return fail(f"oversized_fix: {info['changed_lines']} lines in {len(info['files'])} files")
    if git(repo, "apply", "--check", str(patch_src.resolve()), check=False).returncode != 0:
        return fail("fix_does_not_apply")

    concurrency = lens == "concurrency"
    repeat = int(pc.get("concurrency_repeat") or 10) if concurrency else int(pc.get("repeat") or 1)
    env = test_env(cfg)
    target = repo / dest
    created_dirs = [d for d in [target.parent] if not d.exists()]
    applied = False

    def run(argv, name):
        code, text = run_tests(repo, argv, deadline - time.monotonic(), out_dir / name, env)
        return classify(code, text), text

    try:
        specs = related_specs(repo, info["files"])
        before = set()
        if specs:
            kind, text = run(test_argv(cfg, specs), "regress_before.log")
            if kind == "timeout":
                return fail("timeout", "timeout")
            before = parse_rspec(text)["failed_examples"]

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(test_src, target)
        test_hash = _sha(target)
        argv = test_argv(cfg, [dest])
        reds = []
        for i in range(repeat):
            kind, _ = run(argv, f"red_{i + 1}.log")
            reds.append(kind)
            if kind in ("error", "timeout"):
                break                    # no point repeating a test that crashes or hangs
        res["red_log"] = str(out_dir / "red_1.log")
        res["red_runs"] = reds
        if "timeout" in reds:
            return fail("timeout", "timeout")
        if "error" in reds:
            return fail("red_is_not_an_assertion_failure")
        if concurrency:
            if reds.count("red") < int(pc.get("concurrency_min_red") or 3):
                return fail(f"red_too_rare: failed {reds.count('red')} of {repeat} runs")
        elif reds.count("red") == 0:
            return fail("test_passes_on_current_code")
        elif reds.count("red") != repeat:
            return fail(f"red_flaky: failed {reds.count('red')} of {repeat} runs")

        if git(repo, "apply", str(patch_src.resolve()), check=False).returncode != 0:
            return fail("fix_does_not_apply")
        applied = True
        if not target.is_file() or _sha(target) != test_hash:
            return fail("test_changed_between_red_and_green")
        greens = []
        for i in range(repeat):
            kind, _ = run(argv, f"green_{i + 1}.log")
            greens.append(kind)
            if kind in ("error", "timeout"):
                break
        res["green_log"] = str(out_dir / "green_1.log")
        res["green_runs"] = greens
        if "timeout" in greens:
            return fail("timeout", "timeout")
        if greens.count("green") != repeat:
            return fail(f"test_still_fails_after_fix: passed {greens.count('green')} of {repeat} runs")

        if specs:
            kind, text = run(test_argv(cfg, specs), "regress_after.log")
            res["regress_log"] = str(out_dir / "regress_after.log")
            if kind == "timeout":
                return fail("timeout", "timeout")
            new = parse_rspec(text)["failed_examples"] - before
            if new or kind == "error":
                return fail(f"regression: {len(new)} new failures in related specs" if new
                            else "regression: related specs no longer run")
        res.update(result="pass", reason=None, related_specs=specs)
        return res
    finally:
        restore(repo, patch_src, applied, target, created_dirs, info["files"])


def restore(repo: Path, patch_src: Path, applied: bool, target: Path, created_dirs, fix_files) -> None:
    """Put the audit clone back: discard the fix, remove the proof test."""
    if applied:
        git(repo, "apply", "-R", str(patch_src.resolve()), check=False)
    target.unlink(missing_ok=True)
    for d in created_dirs:
        try:
            d.rmdir()
        except OSError:
            pass
    git(repo, "checkout", "--", ".", check=False)
    for line in clone_dirty(repo).splitlines():
        path = line[3:]
        if line.startswith("??") and path in fix_files:      # a file the patch created
            (repo / path).unlink(missing_ok=True)


# ---------------------------------------------------------------- proof check

def _tail(path, limit=6000) -> str:
    if not path or not Path(path).exists():
        return "(none)"
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return text if len(text) <= limit else "...\n" + text[-limit:]


def proof_check(conn, cfg, run_dir, cluster, claim: str, proof: dict, prover_lab) -> dict | None:
    """Ask a checker from another lab. On a refused or failed call, try the
    next checker. None when no checker gave an answer."""
    system_t, user_t, version = prompts.load("proof_checker.md")
    test = Path(proof["test_path"]).read_text(encoding="utf-8", errors="replace")
    patch = Path(proof["fix_patch_path"]).read_text(encoding="utf-8", errors="replace")
    subject = hashlib.sha256(f"{cluster['cluster_id']}|{test}|{patch}".encode()).hexdigest()
    for seat in cfg.seats("proof_checker"):
        if seat.get("lab") in (prover_lab if isinstance(prover_lab, (set, frozenset)) else {prover_lab}):
            continue

        def messages():
            return (prompts.fill(system_t, {"schema": json.dumps(schema.PROOF_CHECK, indent=1),
                                            "rubric": json.dumps(cfg.severity.get("levels") or {}, indent=1)}),
                    prompts.fill(user_t, {"claim": claim, "test": test, "fix": patch,
                                          "red_log": _tail(proof.get("red_log")),
                                          "green_log": _tail(proof.get("green_log"))}))
        spec = CallSpec(role="proof_checker", seat=seat, prompt_version=version, messages=messages,
                        subject_hash=subject, lens=cluster["lens"], effort=seat.get("effort"),
                        schema=schema.PROOF_CHECK, schema_name="proof_check")
        calls.run_calls(conn, cfg, run_dir, [spec])
        res = calls.result_of(conn, spec, cfg)
        if res and res["parsed"]:
            return {"seat": seat["id"], **res["parsed"]}
    return None


# -------------------------------------------------------------------- driver

def cluster_row(conn, cluster_id: str):
    row = conn.execute(
        "SELECT k.*, c.unit_id, c.title, c.invariant, c.violation, c.locations_json, c.trigger,"
        " c.test_sketch, c.class AS cclass FROM clusters k"
        " JOIN candidates c ON c.candidate_id = k.representative_candidate_id"
        " WHERE k.cluster_id = ?", (cluster_id,)).fetchone()
    if row is None:
        raise VerifyRefused(f"no cluster {cluster_id}")
    return row


def exhausted(conn, cfg, cluster_id: str, seat_id: str) -> bool:
    rows = conn.execute("SELECT status FROM proofs WHERE cluster_id = ? AND prover_seat = ?",
                        (cluster_id, seat_id)).fetchall()
    limit = int(cfg.get("provers.max_attempts_per_prover") or 2)
    return any(r["status"] == "refused" for r in rows) or len(rows) >= limit


def settle(conn, cfg, cluster_id: str) -> None:
    """`unproven` only after every enabled prover has used its attempts."""
    state = conn.execute("SELECT state FROM clusters WHERE cluster_id = ?", (cluster_id,)).fetchone()[0]
    if state == "upheld" and all(exhausted(conn, cfg, cluster_id, s["id"]) for s in cfg.seats("prover")):
        conn.execute("UPDATE clusters SET state = 'unproven' WHERE cluster_id = ?", (cluster_id,))


def package_problem(repo: Path, package: str | None, fix_files: list[str]) -> str | None:
    """Why a prover's choice of test package is not allowed, or None."""
    if not package or not package.strip():
        return None
    package = package.strip().strip("/")
    if not PACKAGE_RE.fullmatch(package) or not (Path(repo) / package).is_dir():
        return f"test_package_unknown: {package}"
    if not any(f.startswith(package + "/") for f in fix_files):
        return f"test_package_without_fix: the fix changes no file in {package}"
    return None


# An attempt that ended because the platform failed the call (rate limit, credits, gateway
# error, transport), on the seat's own model or on its fallback. The model never answered.
LOST_ATTEMPT = "status = 'error' AND reason LIKE '%prover call error:%http\\_%' ESCAPE '\\'"


def lost_attempts(conn) -> list[dict]:
    """Proof attempts lost to a failed call on clusters that still wait for a proof."""
    return [dict(r) for r in conn.execute(
        f"SELECT cluster_id, prover_seat, COUNT(*) n FROM proofs WHERE {LOST_ATTEMPT} AND cluster_id IN"
        " (SELECT cluster_id FROM clusters WHERE state IN ('upheld', 'unproven')) AND cluster_id NOT IN"
        " (SELECT cluster_id FROM outcomes) GROUP BY 1, 2 ORDER BY 1, 2")]


class clone_read:
    """Hold the clone for reading. A proof holds it exclusively while its fix is applied, so a
    prover in another process never reads a file with someone else's patch in it."""

    def __init__(self, run_dir):
        self.path = Path(run_dir) / "clone.lock"

    def __enter__(self):
        self.f = open(self.path, "a")
        fcntl.flock(self.f, fcntl.LOCK_SH)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()
        return False


def record(conn, cfg, cluster_id, seat_id, status, **f) -> str:
    proof_id = ledger.new_id()
    attempt = conn.execute("SELECT COALESCE(MAX(attempt), 0) FROM proofs WHERE cluster_id = ? AND prover_seat = ?",
                           (cluster_id, seat_id)).fetchone()[0] + 1
    with conn:
        conn.execute(
            "INSERT INTO proofs (proof_id, cluster_id, prover_seat, status, test_path, fix_patch_path,"
            " red_log, green_log, regress_log, checker_seat, checker_verdict, checker_reason, attempt,"
            " mechanical, reason, checker_severity, dir, created_at, duration_s, prover_model,"
            " test_destination) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (proof_id, cluster_id, seat_id, status, f.get("test_path"), f.get("fix_patch_path"),
             f.get("red_log"), f.get("green_log"), f.get("regress_log"), f.get("checker_seat"),
             f.get("checker_verdict"), f.get("checker_reason"), attempt, f.get("mechanical", "not_run"),
             f.get("reason"), f.get("checker_severity"), f.get("dir"), ledger.now(), f.get("duration_s"),
             f.get("prover_model"), f.get("test_destination")))
        if status == "valid":
            conn.execute("UPDATE clusters SET state = 'proven', severity = ? WHERE cluster_id = ?",
                         (f.get("checker_severity"), cluster_id))
        settle(conn, cfg, cluster_id)
    return proof_id


def verify(conn, cfg, run_dir, cluster_id: str, test_src, fix_src, seat_id: str,
           test_package: str | None = None, model: str | None = None, lab: str | None = None) -> dict:
    """Mechanical red-green, then the proof check. Only this result counts.

    `test_package` places the test in another package of the repository (the one that owns
    the code to fix). `model` and `lab` name the model that wrote the submission when it is
    not the seat's own (a fallback): the checker must come from neither lab."""
    run_dir = Path(run_dir)
    run = ledger.run(conn)
    repo = Path(run["repo_path"])
    seat = next((s for s in cfg.seats("prover") if s["id"] == seat_id), None)
    if seat is None:
        raise VerifyRefused(f"{seat_id} is not an enabled prover seat")
    k = cluster_row(conn, cluster_id)
    if k["state"] != "upheld":
        raise VerifyRefused(f"cluster {cluster_id} is {k['state']}; only upheld clusters take proofs")
    if exhausted(conn, cfg, cluster_id, seat_id):
        raise VerifyRefused(f"{seat_id} has used its attempts on cluster {cluster_id}")
    for p in (test_src, fix_src):
        if not Path(p).is_file():
            raise VerifyRefused(f"no such file: {p}")

    attempt = conn.execute("SELECT COALESCE(MAX(attempt), 0) FROM proofs WHERE cluster_id = ? AND prover_seat = ?",
                           (cluster_id, seat_id)).fetchone()[0] + 1
    out_dir = run_dir / "proofs" / cluster_id / seat_id / f"attempt_{attempt}"
    out_dir.mkdir(parents=True, exist_ok=True)
    test_copy, patch_copy = out_dir / "proof_spec.rb", out_dir / "fix.patch"
    if Path(test_src).resolve() != test_copy.resolve():
        shutil.copyfile(test_src, test_copy)
    if Path(fix_src).resolve() != patch_copy.resolve():
        shutil.copyfile(fix_src, patch_copy)
    unit_path = json.loads(k["locations_json"])[0]["path"]
    dest = test_destination(cluster_id, unit_path, test_package)
    written_by = model or seat.get("model") or seat_id
    problem = package_problem(repo, test_package, patch_info(patch_copy.read_text(encoding="utf-8", errors="replace"))["files"])
    if problem:
        record(conn, cfg, cluster_id, seat_id, "invalid", mechanical="fail", reason=problem, dir=str(out_dir),
               test_path=str(test_copy), fix_patch_path=str(patch_copy), prover_model=written_by,
               test_destination=dest)
        return {"cluster_id": cluster_id, "prover": seat_id, "attempt": attempt, "dir": str(out_dir),
                "test_destination": dest, "mechanical": "fail", "reason": problem, "clone_clean": True,
                "status": "invalid"}

    started = time.monotonic()
    # one proof at a time in the audit clone: shards of `prove run` share it
    with open(run_dir / "clone.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # another prover may have settled the cluster while this one waited for the clone
        if cluster_row(conn, cluster_id)["state"] != "upheld":
            raise VerifyRefused(f"cluster {cluster_id} was settled by another prover in the meantime")
        preconditions(conn, cfg, repo)
        m = mechanical(cfg, repo, k["lens"], dest, test_copy, patch_copy, out_dir)
        dirty = clone_dirty(repo)
    fields = {"duration_s": round(time.monotonic() - started, 1), "test_path": str(test_copy), "fix_patch_path": str(patch_copy), "red_log": m["red_log"],
              "green_log": m["green_log"], "regress_log": m["regress_log"], "dir": str(out_dir),
              "prover_model": written_by, "test_destination": dest}
    out = {"cluster_id": cluster_id, "prover": seat_id, "attempt": attempt, "dir": str(out_dir),
           "test_destination": dest, "mechanical": m["result"], "reason": m["reason"],
           "clone_clean": not dirty}
    if dirty:
        out.update(status="error", reason=f"audit clone left dirty after the attempt: {dirty[:300]}")
        record(conn, cfg, cluster_id, seat_id, "error", mechanical=m["result"], reason=out["reason"], **fields)
        return out
    if m["result"] != "pass":
        out["status"] = "timeout" if m["result"] == "timeout" else "invalid"
        record(conn, cfg, cluster_id, seat_id, out["status"], mechanical="fail", reason=m["reason"], **fields)
        return out

    claim = card({**dict(k), "class": k["cclass"]})
    check = proof_check(conn, cfg, run_dir, k, claim, fields, {seat.get("lab"), lab} - {None})
    if check is None:
        out.update(status="error", reason="no proof checker gave an answer; the proof is not accepted")
        record(conn, cfg, cluster_id, seat_id, "error", mechanical="pass", reason=out["reason"], **fields)
        return out
    accepted = check["verdict"] == "accept"
    out.update(status="valid" if accepted else "invalid", checker=check["seat"],
               checker_verdict=check["verdict"], checker_reason=check["reason"],
               reason=None if accepted else "proof checker rejected the proof")
    record(conn, cfg, cluster_id, seat_id, out["status"], mechanical="pass", reason=out["reason"],
           checker_seat=check["seat"], checker_verdict=check["verdict"], checker_reason=check["reason"],
           checker_severity=check["severity"], **fields)
    return out


def recheck(conn, cfg, run_dir, reason_like: str, max_proofs: int | None = None) -> dict:
    """Run stored proofs through the gate again, after a gate fault was fixed.

    Takes attempts that failed the mechanical step with a reason matching `reason_like`
    (SQL LIKE) on clusters still waiting for a proof. A row is changed only after its new
    result exists, so an interrupted re-check loses nothing."""
    run_dir = Path(run_dir)
    repo = Path(ledger.run(conn)["repo_path"])
    rows = conn.execute(
        "SELECT p.* FROM proofs p JOIN clusters k ON k.cluster_id = p.cluster_id"
        " WHERE p.status = 'invalid' AND p.mechanical = 'fail' AND p.reason LIKE ?"
        " AND k.state IN ('upheld', 'unproven') ORDER BY p.created_at", (reason_like,)).fetchall()
    out = {"considered": len(rows), "rechecked": 0, "now_valid": 0, "now_rejected": 0, "unchanged": 0, "skipped": 0}
    for p in rows[:max_proofs] if max_proofs else rows:
        k = cluster_row(conn, p["cluster_id"])
        if k["state"] not in ("upheld", "unproven") or not p["dir"] or \
                not all(Path(x or "").is_file() for x in (p["test_path"], p["fix_patch_path"])):
            out["skipped"] += 1
            continue
        seat = next((s for s in cfg.seats("prover") if s["id"] == p["prover_seat"]), {})
        dest = p["test_destination"] or test_destination(p["cluster_id"], json.loads(k["locations_json"])[0]["path"])
        again = Path(p["dir"]) / "recheck"
        again.mkdir(parents=True, exist_ok=True)
        with open(run_dir / "clone.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            preconditions(conn, cfg, repo)
            m = mechanical(cfg, repo, k["lens"], dest, Path(p["test_path"]), Path(p["fix_patch_path"]), again)
            dirty = clone_dirty(repo)
        if dirty:
            raise VerifyRefused(f"the audit clone was left dirty by a re-check:\n{dirty[:300]}")
        out["rechecked"] += 1
        if m["result"] != "pass":
            out["unchanged"] += 1
            continue
        fields = {"test_path": p["test_path"], "fix_patch_path": p["fix_patch_path"], "red_log": m["red_log"],
                  "green_log": m["green_log"], "regress_log": m["regress_log"]}
        labs = {seat.get("lab")} - {None}
        check = proof_check(conn, cfg, run_dir, k, card({**dict(k), "class": k["cclass"]}), fields, labs)
        if check is None:
            out["unchanged"] += 1
            continue
        accepted = check["verdict"] == "accept"
        with conn:
            conn.execute(
                "UPDATE proofs SET status = ?, mechanical = 'pass', reason = ?, red_log = ?, green_log = ?,"
                " regress_log = ?, checker_seat = ?, checker_verdict = ?, checker_reason = ?,"
                " checker_severity = ? WHERE proof_id = ?",
                ("valid" if accepted else "invalid",
                 f"re-checked (was: {p['reason']})" if accepted else "proof checker rejected the proof",
                 m["red_log"], m["green_log"], m["regress_log"], check["seat"], check["verdict"],
                 check["reason"], check["severity"], p["proof_id"]))
            if accepted:
                conn.execute("UPDATE clusters SET state = 'proven', severity = ? WHERE cluster_id = ?",
                             (check["severity"], p["cluster_id"]))
        out["now_valid" if accepted else "now_rejected"] += 1
    return out
