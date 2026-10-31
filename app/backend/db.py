#!/usr/bin/env python3
"""Phase 1 jobs database (db/jobs.sqlite).

Per ADR-0008, ingestion job state lives in a dedicated jobs database, separate from the
other durable databases under db/ (the LLM cache and the authoritative graph). The keyword
index is a derived product under indexes/ (ADR-0032 §7). Dependency-free (stdlib sqlite3).

A job recorded `running` holds the vault writer lock (`app/backend/vault_lock.py`, shared) from
before its row is inserted until `update_job` records a terminal status, so a backup never runs
while a producer writes (ADR-0039 amendment).

The same database holds the **LLM usage ledger** (ADR-0069 decision 6): `llm_usage`, one row per
provider attempt, and `llm_cache_hits`, avoided calls counted per job and model. A job recorded
`running` is also this context's **current job** (`current_job`), which the ledger uses to attribute
attempts without each producer passing its job id; `bound_invocation` does the same for work that
is not a job (a `/query` request, an eval run). Attribution is a per-context stack: a nested job is
current until it ends, then its parent is again; concurrent threads and tasks each see their own.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from app.backend import vault_lock

JOB_TYPES = frozenset(
    {"intake_scan", "manifest_create", "duplicate_check", "extract", "generate_wiki",
     "enrich", "extract_claims", "extract_items", "promote", "detect_contradictions",
     "generate_synthesis",
     # Phase 7 autonomous-maintenance passes (ADR-0036). No `eval` job ships in v1 — the golden set is
     # a fake CI fixture, so a real-vault eval is deferred (decision 14).
     "lint", "reindex", "stale_check"}
)
# `saved_publication_pending` (ADR-0068 decision 5): the run's work is saved but its publication
# (pages, index.md, keyword search) is incomplete and queued in db/publication_pending.json.
JOB_STATUSES = frozenset(
    {"pending", "running", "succeeded", "failed", "partial", "skipped", "saved_publication_pending"}
)
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "partial", "skipped",
                                "saved_publication_pending"})
_JOB_LOCKS: dict[str, Any] = {}                           # job_id -> vault writer-lock handle
_JOB_LOCKS_GUARD = threading.Lock()
# Stack of (job or invocation id, producer) in this context; the top one is the job the context's
# LLM attempts are attributed to (ADR-0069).
_JOB_STACK: ContextVar[tuple[tuple[str, str], ...]] = ContextVar("ks_job_stack", default=())


def _db_dir(conn: sqlite3.Connection) -> Path | None:
    """The directory of the connection's database file (None for an in-memory database)."""
    row = conn.execute("PRAGMA database_list").fetchone()
    return Path(row[2]).parent if row and row[2] else None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    job_type      TEXT NOT NULL,
    status        TEXT NOT NULL,
    source_id     TEXT,
    input_path    TEXT,
    output_path   TEXT,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    error_message TEXT,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

-- ADR-0069 decision 6: one row per provider attempt, recorded `started` before sending and
-- finalized after. Token fields are each nullable (NULL = not reported) and keep the provider's own
-- categories: for `usage_family` 'anthropic' `input_tokens` excludes the cache read/write tokens;
-- for 'openai_compatible' `input_tokens` is `prompt_tokens`, which INCLUDES `cache_read_tokens`.
-- Metadata and counts only: never prompts, responses, error text or credentials.
CREATE TABLE IF NOT EXISTS llm_usage (
    attempt_id         TEXT PRIMARY KEY,
    job_id             TEXT,
    producer           TEXT,
    operation          TEXT,
    retry              INTEGER NOT NULL,
    model_ref          TEXT NOT NULL,
    provider           TEXT NOT NULL,
    status             TEXT NOT NULL,
    input_chars        INTEGER NOT NULL,
    max_output_tokens  INTEGER NOT NULL,
    usage_family       TEXT,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    cache_read_tokens  INTEGER,
    cache_write_tokens INTEGER,
    started_at         TEXT NOT NULL,
    finished_at        TEXT
);
CREATE INDEX IF NOT EXISTS llm_usage_job ON llm_usage (job_id);

-- Cache replays are avoided calls, counted apart from attempts (job_id '' = unattributed).
CREATE TABLE IF NOT EXISTS llm_cache_hits (
    job_id     TEXT NOT NULL,
    producer   TEXT,
    model_ref  TEXT NOT NULL,
    hits       INTEGER NOT NULL,
    PRIMARY KEY (job_id, model_ref)
);

-- When this database started recording usage (written once, iso_now's format): a job created
-- earlier made attempts no ledger saw, so its usage is "not recorded", not "none".
CREATE TABLE IF NOT EXISTS llm_ledger_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO llm_ledger_meta (key, value)
    VALUES ('started_at', strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'));
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def current_job() -> tuple[str, str] | None:
    """`(job or invocation id, producer)` this context's LLM attempts belong to, or None."""
    stack = _JOB_STACK.get()
    return stack[-1] if stack else None


@contextmanager
def bound_invocation(invocation_id: str, producer: str) -> Iterator[None]:
    """Attribute LLM attempts made inside the block to work that is not a job (ADR-0069)."""
    token = _JOB_STACK.set(_JOB_STACK.get() + ((invocation_id, producer),))
    try:
        yield
    finally:
        _JOB_STACK.reset(token)


def init_db(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def insert_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    job_type: str,
    status: str,
    created_at: str,
    source_id: str | None = None,
    input_path: str | None = None,
    output_path: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    error_message: str | None = None,
    warnings: list[Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    if job_type not in JOB_TYPES:
        raise ValueError(f"unknown job_type {job_type!r}; allowed: {sorted(JOB_TYPES)}")
    if status not in JOB_STATUSES:
        raise ValueError(f"unknown job status {status!r}; allowed: {sorted(JOB_STATUSES)}")
    if status == "running" and (db_dir := _db_dir(conn)) is not None:
        handle = vault_lock.acquire_shared(db_dir)        # waits while a backup/restore runs
        with _JOB_LOCKS_GUARD:
            vault_lock.release(_JOB_LOCKS.pop(job_id, None))
            _JOB_LOCKS[job_id] = handle
    try:
        _insert_row(conn, job_id, job_type, status, source_id, input_path, output_path, created_at,
                    started_at, finished_at, error_message, warnings, metadata)
    except BaseException:
        _release_job_lock(job_id)
        raise
    if status == "running":                               # the ledger attributes attempts to it
        _JOB_STACK.set(_JOB_STACK.get() + ((job_id, job_type),))


def _release_job_lock(job_id: str) -> None:
    with _JOB_LOCKS_GUARD:
        handle = _JOB_LOCKS.pop(job_id, None)
    vault_lock.release(handle)


def _insert_row(conn, job_id, job_type, status, source_id, input_path, output_path, created_at,
                started_at, finished_at, error_message, warnings, metadata) -> None:
    conn.execute(
        """INSERT INTO jobs (
            job_id, job_type, status, source_id, input_path, output_path,
            created_at, started_at, finished_at, error_message,
            warnings_json, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job_id, job_type, status, source_id, input_path, output_path,
            created_at, started_at, finished_at, error_message,
            json.dumps(warnings or []), json.dumps(metadata or {}),
        ),
    )
    conn.commit()


def update_job(conn: sqlite3.Connection, job_id: str, **fields: Any) -> None:
    """Update a job. The keys `warnings` and `metadata` are JSON-encoded for you."""
    if not fields:
        return
    if "status" in fields and fields["status"] not in JOB_STATUSES:
        raise ValueError(
            f"unknown job status {fields['status']!r}; allowed: {sorted(JOB_STATUSES)}"
        )
    assignments: list[str] = []
    values: list[Any] = []
    for key, value in fields.items():
        if key == "warnings":
            assignments.append("warnings_json = ?")
            values.append(json.dumps(value or []))
        elif key == "metadata":
            assignments.append("metadata_json = ?")
            values.append(json.dumps(value or {}))
        else:
            assignments.append(f"{key} = ?")
            values.append(value)
    values.append(job_id)
    try:
        conn.execute(
            f"UPDATE jobs SET {', '.join(assignments)} WHERE job_id = ?", values
        )
        conn.commit()
    finally:
        if fields.get("status") in _TERMINAL_STATUSES:
            _release_job_lock(job_id)                     # the job's writes are done
            stack = _JOB_STACK.get()                      # a parent job is current again
            if any(entry[0] == job_id for entry in stack):
                _JOB_STACK.set(tuple(entry for entry in stack if entry[0] != job_id))


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    job = dict(row)
    job["warnings"] = json.loads(job.pop("warnings_json") or "[]")
    job["metadata"] = json.loads(job.pop("metadata_json") or "{}")
    return job


def get_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def list_jobs(
    conn: sqlite3.Connection, *, limit: int = 100, status: str | None = None
) -> list[dict[str, Any]]:
    if status:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = ? "
            "ORDER BY created_at DESC, job_id DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC, job_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_job(r) for r in rows]
