#!/usr/bin/env python3
"""Persistent response cache for non-deterministic enrichment (ADR-0027).

LLM output is not byte-reproducible, so a rebuild or forced re-render replays the stored
response rather than re-sampling — reproducible and free until an input changes. The cache
key is `hash(messages + model_ref + schema + schema_version + prompt_version [+ strategy_ref])`,
so it carries the provider and model id (via `model_ref`), the schema and its version, the
prompt template version, the source content (embedded in `messages`), and — for tier-2
callers (ADR-0056) — the extraction-strategy ref (appended only when present, so non-tier-2
keys are unchanged). A swap of any of these is a clean miss-and-refresh, never a silent
collision.

The cache lives under `db/` and is covered by backup by default (`policies/retention.yaml`,
`scripts/backup.py`). Dependency-free (stdlib sqlite3).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def cache_retention_report(db_path: Path, *, ttl_days: int, cap_mb: int,
                           now: datetime) -> dict[str, Any]:
    """Read-only retention stats for the LLM response cache (Phase 7, ADR-0036). Never mutates.

    Returns live stats: `{cache_present, cache_readable, entries, entries_over_ttl, total_mb, cap_mb,
    oldest_age_days, over_bounds}`. A **missing** DB -> `{cache_present: False}` (no finding); a
    **corrupt/unreadable** DB -> `{cache_present: True, cache_readable: False}` (a degraded report, not an
    error). Carries only counts/sizes/ages — never `response_json`, prompts, model outputs, or keys.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return {"cache_present": False}
    try:
        cutoff = (now - timedelta(days=ttl_days)).isoformat()
        conn = sqlite3.connect(db_path)
        try:
            entries, oldest = conn.execute(
                "SELECT COUNT(*), MIN(created_at) FROM response_cache").fetchone()
            over_ttl = conn.execute(
                "SELECT COUNT(*) FROM response_cache WHERE created_at < ?", (cutoff,)).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return {"cache_present": True, "cache_readable": False}
    total_mb = round(db_path.stat().st_size / (1024 * 1024), 3)
    oldest_age_days = None
    if oldest:
        try:
            oldest_age_days = (now - datetime.fromisoformat(oldest)).days
        except (ValueError, TypeError):
            oldest_age_days = None
    return {"cache_present": True, "cache_readable": True, "entries": int(entries or 0),
            "entries_over_ttl": int(over_ttl or 0), "total_mb": total_mb, "cap_mb": cap_mb,
            "oldest_age_days": oldest_age_days,
            "over_bounds": bool((over_ttl or 0) > 0 or total_mb > cap_mb)}

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


def _version_bytes(value: Any) -> bytes:
    return b"" if value is None else str(value).encode("utf-8")


def cache_key(
    messages: list[dict[str, Any]],
    model_ref: str,
    schema: dict[str, Any],
    *,
    schema_version: Any = None,
    prompt_version: Any = None,
    strategy_ref: Any = None,
) -> str:
    """Stable key over everything that should force a fresh model call.

    `strategy_ref` is the explicit extraction-strategy identity component (ADR-0056) — a
    coverage change (window budget, input cap) must force fresh calls even when prompt text
    and schema are unchanged. Composed as its own component, never folded into
    prompt/schema version strings; `None` (every non-tier-2 caller) hashes identically to the
    pre-ADR-0056 key, so existing cache entries stay valid.
    """
    parts = [
        _canonical(messages),
        model_ref.encode("utf-8"),
        _canonical(schema),
        _version_bytes(schema_version),
        _version_bytes(prompt_version),
    ]
    if strategy_ref is not None:
        # Appended only when present so a None ref hashes byte-identically to the
        # pre-ADR-0056 five-component key.
        parts.append(_version_bytes(strategy_ref))
    h = hashlib.sha256()
    for part in parts:
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
