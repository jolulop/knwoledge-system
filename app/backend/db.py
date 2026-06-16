#!/usr/bin/env python3
"""Phase 1 jobs database (db/jobs.sqlite).

Per ADR-0008, ingestion job state lives in a dedicated jobs database, separate from
the FTS keyword index in db/metadata.sqlite. Dependency-free (stdlib sqlite3).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

JOB_TYPES = frozenset(
    {"intake_scan", "manifest_create", "duplicate_check", "extract", "generate_wiki",
     "enrich", "extract_claims", "extract_concepts", "promote", "detect_contradictions",
     "generate_synthesis"}
)
JOB_STATUSES = frozenset(
    {"pending", "running", "succeeded", "failed", "partial", "skipped"}
)

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
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


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
    conn.execute(
        f"UPDATE jobs SET {', '.join(assignments)} WHERE job_id = ?", values
    )
    conn.commit()


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
