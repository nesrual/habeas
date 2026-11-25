"""The run ledger: one SQLite file per run (plan 7). All state lives here."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Plan 7, plus the columns the plan lists under "Also needed" and a few the
# engine needs to resume: runs.stage*, runs.limit_*, calls.reason,
# calls.rejected_candidates.
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, mode TEXT NOT NULL,
  repo_commit TEXT NOT NULL, config_hash TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT,
  state TEXT NOT NULL DEFAULT 'running',   -- running | paused_budget | stopped | finished
  repo_path TEXT, stage TEXT, stage_args_json TEXT,
  limit_usd REAL, limit_tokens INTEGER,
  stages_json TEXT NOT NULL DEFAULT '{}'    -- stage name -> time it last completed
);
CREATE TABLE IF NOT EXISTS call_results (     -- parsed answers of non-finder calls
  idempotency_key TEXT PRIMARY KEY, call_id TEXT NOT NULL, result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS baseline (
  ref TEXT NOT NULL, source TEXT NOT NULL, path TEXT NOT NULL,
  start_line INTEGER, end_line INTEGER, description TEXT
);
CREATE TABLE IF NOT EXISTS units (
  unit_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
  paths_json TEXT NOT NULL, sha256 TEXT NOT NULL, tokens INTEGER NOT NULL,
  package TEXT, sensitive INTEGER NOT NULL DEFAULT 1,
  scanner_hits_json TEXT, order_rank INTEGER, is_canary INTEGER NOT NULL DEFAULT 0,
  blocked_reason TEXT, context_json TEXT
);
CREATE TABLE IF NOT EXISTS calls (
  call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, unit_id TEXT,
  seat_id TEXT NOT NULL, role TEXT NOT NULL, lens TEXT, run_index INTEGER,
  prompt_variant TEXT, prompt_version TEXT NOT NULL,
  requested_model TEXT NOT NULL, served_model TEXT, served_provider TEXT,
  status TEXT NOT NULL,          -- ok | refused | downgraded | error | skipped
  finish_reason TEXT, input_tokens INTEGER, output_tokens INTEGER,
  cost_usd REAL, latency_ms INTEGER, raw_path TEXT, created_at TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  reason TEXT, rejected_candidates INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS candidates (
  candidate_id TEXT PRIMARY KEY, call_id TEXT NOT NULL, unit_id TEXT NOT NULL,
  lens TEXT NOT NULL, class TEXT NOT NULL,
  title TEXT NOT NULL, invariant TEXT NOT NULL, violation TEXT NOT NULL,
  locations_json TEXT NOT NULL, trigger TEXT, test_sketch TEXT, confidence REAL
);
CREATE TABLE IF NOT EXISTS clusters (
  cluster_id TEXT PRIMARY KEY, state TEXT NOT NULL,
  lens TEXT NOT NULL, class TEXT NOT NULL, representative_candidate_id TEXT NOT NULL,
  severity TEXT, known_ref TEXT, severity_note TEXT
);
-- A person's reading of a cluster after checking it by hand: overrides the models' severity.
CREATE TABLE IF NOT EXISTS reviews (
  cluster_id TEXT PRIMARY KEY, severity TEXT NOT NULL, note TEXT NOT NULL,
  reviewer TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cluster_members (
  cluster_id TEXT NOT NULL, candidate_id TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS merges (
  merge_id TEXT PRIMARY KEY, candidate_a TEXT NOT NULL, candidate_b TEXT NOT NULL,
  merger_verdict TEXT NOT NULL, auditor_verdict TEXT NOT NULL, merged INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS verdicts (
  cluster_id TEXT NOT NULL, critic_seat TEXT NOT NULL, call_id TEXT NOT NULL,
  verdict TEXT NOT NULL,         -- refute | uphold | unsure | unavailable
  reason TEXT, PRIMARY KEY (cluster_id, critic_seat)
);
CREATE TABLE IF NOT EXISTS proofs (
  proof_id TEXT PRIMARY KEY, cluster_id TEXT NOT NULL, prover_seat TEXT NOT NULL,
  status TEXT NOT NULL,          -- valid | invalid | refused | timeout | error
  test_path TEXT, fix_patch_path TEXT, red_log TEXT, green_log TEXT, regress_log TEXT,
  checker_seat TEXT, checker_verdict TEXT, checker_reason TEXT,
  attempt INTEGER, mechanical TEXT,          -- pass | fail | not_run
  reason TEXT, checker_severity TEXT, dir TEXT, created_at TEXT, duration_s REAL,
  prover_model TEXT,             -- the model that wrote the submission (the seat's or its fallback)
  test_destination TEXT          -- where the test was placed in the clone
);
CREATE TABLE IF NOT EXISTS outcomes (
  cluster_id TEXT PRIMARY KEY,
  outcome TEXT NOT NULL,         -- proven | quarantined | refuted | unproven_dropped | known
  detail TEXT, issue_path TEXT,
  triage_state TEXT, triage_by TEXT, fixed_since TEXT
);
CREATE TABLE IF NOT EXISTS canary_truth (
  bug_id TEXT PRIMARY KEY, unit_id TEXT NOT NULL, lens TEXT NOT NULL,
  description TEXT NOT NULL, locations_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS budget_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
  kind TEXT NOT NULL,            -- warning | pause | raise
  unit TEXT NOT NULL,            -- usd | tokens
  old_limit REAL, new_limit REAL, spent_usd REAL, spent_tokens INTEGER,
  approved_by TEXT, created_at TEXT NOT NULL
);
"""


class LedgerError(Exception):
    pass


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def connect(run_dir) -> sqlite3.Connection:
    path = Path(run_dir) / "ledger.sqlite"
    if not path.exists():
        raise LedgerError(f"no ledger at {path}")
    return _open(path)


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    _upgrade(conn)
    return conn


ADDED_COLUMNS = {"proofs": {"prover_model": "TEXT", "test_destination": "TEXT"}}


def _upgrade(conn) -> None:
    """Add columns and tables that ledgers written by an earlier version lack."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'clusters'").fetchone():
        conn.execute("CREATE TABLE IF NOT EXISTS reviews (cluster_id TEXT PRIMARY KEY, severity TEXT NOT NULL,"
                     " note TEXT NOT NULL, reviewer TEXT NOT NULL, created_at TEXT NOT NULL)")
    for table, columns in ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue
        for name, kind in columns.items():
            if name not in have:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
                except sqlite3.OperationalError:
                    pass        # another process added it first


def create_run(run_dir, cfg, mode: str = "audit", repo_commit: str = "unknown",
               repo_path: str | None = None) -> sqlite3.Connection:
    run_dir = Path(run_dir)
    for sub in ("raw", "proofs", "issues", "quarantine"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    run_dir.chmod(0o700)
    path = run_dir / "ledger.sqlite"
    if path.exists():
        raise LedgerError(f"ledger already exists at {path}")
    conn = _open(path)
    conn.executescript(SCHEMA)
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, mode, repo_commit, config_hash, started_at,"
            " repo_path, limit_usd, limit_tokens) VALUES (?,?,?,?,?,?,?,?)",
            (run_dir.name, mode, repo_commit, cfg.hash, now(),
             repo_path or cfg.get("target.repo_path"),
             cfg.get("budget.limit_usd"), cfg.get("budget.limit_tokens")))
    return conn


def run(conn) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM runs").fetchone()
    if row is None:
        raise LedgerError("ledger has no run row")
    return row


def mark_stage(conn, stage: str) -> None:
    stages = json.loads(run(conn)["stages_json"])
    stages[stage] = now()
    conn.execute("UPDATE runs SET stages_json = ?", (json.dumps(stages),))


def stages(conn) -> dict:
    return json.loads(run(conn)["stages_json"])


def set_state(conn, state: str) -> None:
    conn.execute("UPDATE runs SET state = ?", (state,))


def spend(conn) -> tuple[float, int, int]:
    """(usd, tokens in plus out, calls that reached a model)."""
    r = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0), COALESCE(SUM(COALESCE(input_tokens, 0)"
        " + COALESCE(output_tokens, 0)), 0), COUNT(*) FROM calls WHERE status != 'skipped' AND role != 'static'"
    ).fetchone()
    return float(r[0]), int(r[1]), int(r[2])


def budget_event(conn, kind: str, unit: str, old=None, new=None, approved_by=None) -> None:
    usd, tokens, _ = spend(conn)
    conn.execute(
        "INSERT INTO budget_events (run_id, kind, unit, old_limit, new_limit, spent_usd,"
        " spent_tokens, approved_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (run(conn)["run_id"], kind, unit, old, new, usd, tokens, approved_by, now()))
