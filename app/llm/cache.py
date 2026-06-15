#!/usr/bin/env python3
"""Persistent response cache for non-deterministic enrichment (ADR-0027).

LLM output is not byte-reproducible, so a rebuild or forced re-render replays the stored
response rather than re-sampling — reproducible and free until an input changes. The cache
key is `hash(messages + model_ref + schema + schema_version + prompt_version)`, so it carries
the provider and model id (via `model_ref`), the schema and its version, the prompt template
version, and the source content (embedded in `messages`). A swap of any of these is a clean
miss-and-refresh, never a silent collision.

The cache lives under `db/` and is covered by backup by default (`policies/retention.yaml`,
`scripts/backup.py`). Dependency-free (stdlib sqlite3).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS response_cache (
    cache_key      TEXT PRIMARY KEY,
    provider       TEXT NOT NULL,
    model_id       TEXT NOT NULL,
    schema_version TEXT,
    prompt_version TEXT,
    response_json  TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
"""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")


def cache_key(
    messages: list[dict[str, Any]],
    model_ref: str,
    schema: dict[str, Any],
    *,
    schema_version: str | None = None,
    prompt_version: str | None = None,
) -> str:
    """Stable key over everything that should force a fresh model call."""
    h = hashlib.sha256()
    for part in (
        _canonical(messages),
        model_ref.encode("utf-8"),
        _canonical(schema),
        (schema_version or "").encode("utf-8"),
        (prompt_version or "").encode("utf-8"),
    ):
        h.update(part)
        h.update(b"\0")
    return h.hexdigest()


class ResponseCache:
    """A local SQLite cache of raw, validated model responses under `db/`."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def get(self, key: str) -> dict[str, Any] | None:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT response_json FROM response_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        finally:
            conn.close()
        return json.loads(row[0]) if row else None

    def put(
        self,
        key: str,
        *,
        provider: str,
        model_id: str,
        response: dict[str, Any],
        created_at: str,
        schema_version: str | None = None,
        prompt_version: str | None = None,
    ) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """INSERT OR REPLACE INTO response_cache (
                    cache_key, provider, model_id, schema_version, prompt_version,
                    response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    key, provider, model_id, schema_version, prompt_version,
                    json.dumps(response, ensure_ascii=False), created_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()
