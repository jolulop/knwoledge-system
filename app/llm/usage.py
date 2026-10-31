#!/usr/bin/env python3
"""The LLM usage ledger (ADR-0069 decision 6).

One row per **provider attempt**, written by `LLMClient.parse` so no producer can leave it out:
`start` records the attempt as `started` **before** the request is sent; `finish` records its
outcome and the provider-reported usage afterwards. A crash or timeout in between leaves a visible
`started` row with unknown usage. Retries are separate attempts (`retry` 0, 1, …). A cache replay is
not an attempt: `cache_hit` counts it per job and model as an **avoided call**.

Attribution comes from `db.current_job()` — the job a producer recorded `running` in this context,
or an invocation bound with `db.bound_invocation` (a `/query` request, an eval run). `operation`
names the unit of work (a source id, a claim window, a synthesis topic).

Token fields are each None when not reported and keep the provider's categories (`usage_family`):
for `anthropic`, `input_tokens` excludes the cache read/write tokens; for `openai_compatible`,
`input_tokens` (the wire's `prompt_tokens`) includes `cache_read_tokens`. Totals are therefore
computed per model (one family each) and never mix the two. The ledger stores metadata and counts
only: no prompts, responses, error messages, credentials or source excerpts, and no prices.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from app.backend import db
from app.backend.manifests import iso_now

log = logging.getLogger(__name__)

OUTCOMES = ("ok", "schema_error", "adapter_error", "output_limit")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
_TIMEOUT_S = 30


class UsageLedgerError(RuntimeError):
    """An attempt could not be recorded before sending, so it was not sent."""


class LedgerUnavailable(RuntimeError):
    """The ledger exists but could not be read. Never reported as "no usage"."""


def input_chars(messages: list[dict[str, Any]], schema: dict[str, Any]) -> int:
    """The characters an attempt sends: every message's content plus the JSON-serialized schema.
    Deterministic, so a budget can be checked before sending (ADR-0069 decision 5)."""
    return (sum(len(str(m.get("content", ""))) for m in messages)
            + len(json.dumps(schema, ensure_ascii=False, sort_keys=True)))


class UsageLedger:
    def __init__(self, jobs_db: Path) -> None:
        self._path = Path(jobs_db)
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        if not self._ready:
            db.init_db(self._path)
            self._ready = True
        return sqlite3.connect(self._path, timeout=_TIMEOUT_S)

    def start(self, *, model_ref: str, provider: str, retry: int, input_chars: int,
              max_output_tokens: int, operation: str | None = None) -> str:
        """Record an attempt as `started`; return its id. Raises UsageLedgerError if it cannot be
        recorded — the caller then does not send it."""
        job_id, producer = db.current_job() or (None, None)
        attempt_id = f"att_{uuid.uuid4().hex[:16]}"
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO llm_usage (attempt_id, job_id, producer, operation, retry, model_ref,"
                    " provider, status, input_chars, max_output_tokens, started_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 'started', ?, ?, ?)",
                    (attempt_id, job_id, producer, operation, retry, model_ref, provider,
                     input_chars, max_output_tokens, iso_now()))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise UsageLedgerError(f"could not record the LLM attempt before sending: {exc}") from exc
        return attempt_id

    def finish(self, attempt_id: str, *, outcome: str, usage: dict[str, Any] | None) -> None:
        """Record the attempt's outcome and reported usage. Never raises: a failure here leaves the
        row `started` (visible as unfinished, usage unknown) rather than losing a paid response."""
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown attempt outcome {outcome!r}")
        u = usage or {}
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE llm_usage SET status = ?, usage_family = ?, input_tokens = ?,"
                    " output_tokens = ?, cache_read_tokens = ?, cache_write_tokens = ?,"
                    " finished_at = ? WHERE attempt_id = ?",
                    (outcome, u.get("family"), *(u.get(f) for f in TOKEN_FIELDS), iso_now(),
                     attempt_id))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:                      # disk/lock failure
            log.warning("LLM usage ledger: could not finalize %s: %s", attempt_id, exc)

    def cache_hit(self, model_ref: str) -> None:
        """Count one avoided call (a cache replay) for the current job and model. Never raises."""
        job_id, producer = db.current_job() or ("", None)
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO llm_cache_hits (job_id, producer, model_ref, hits) VALUES (?, ?, ?, 1)"
                    " ON CONFLICT (job_id, model_ref) DO UPDATE SET hits = hits + 1",
                    (job_id, producer, model_ref))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:                      # pragma: no cover - disk/lock failure
            log.warning("LLM usage ledger: could not count a cache hit: %s", exc)


# --- reading -------------------------------------------------------------------------------------


def summarize(jobs_db: Path, job_id: str) -> dict[str, dict[str, Any]] | None:
    """Per-model totals for one job or invocation: attempts by outcome, unfinished attempts, cache
    hits, and **reported** token sums with, per field, how many attempts left it unknown (an
    unfinished attempt counts as unknown). `{}` when the job made no LLM attempt and had no cache
    hit; None when its usage was **not recorded** (the database has no ledger, or the job predates
    it). Raises LedgerUnavailable when the ledger cannot be read."""
    return summarize_many(jobs_db, [job_id])[job_id]


def summarize_many(jobs_db: Path,
                   job_ids: list[str]) -> dict[str, dict[str, dict[str, Any]] | None]:
    """`{job_id: summarize(...)}` for every requested id, in one read."""
    path = Path(jobs_db)
    ids = sorted(set(job_ids))
    if not path.exists():
        return {i: None for i in ids}
    marks = ",".join("?" * len(ids))
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=_TIMEOUT_S)
        conn.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name IN ('jobs', 'llm_usage', 'llm_cache_hits', 'llm_ledger_meta')")}
            if not {"llm_usage", "llm_cache_hits", "llm_ledger_meta"} <= tables:
                return {i: None for i in ids}             # an older database: no ledger yet
            started = conn.execute(
                "SELECT value FROM llm_ledger_meta WHERE key = 'started_at'").fetchone()
            older = set() if started is None or "jobs" not in tables else {
                r[0] for r in conn.execute(
                    f"SELECT job_id FROM jobs WHERE job_id IN ({marks}) AND created_at < ?",
                    [*ids, started[0]])}
            rows = conn.execute(f"SELECT * FROM llm_usage WHERE job_id IN ({marks})",
                                ids).fetchall()
            hits = conn.execute(
                f"SELECT job_id, model_ref, hits FROM llm_cache_hits WHERE job_id IN ({marks})",
                ids).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise LedgerUnavailable(f"could not read the LLM usage ledger: {exc}") from exc
    out: dict[str, dict[str, dict[str, Any]]] = {}

    def entry(job_id: str, model_ref: str) -> dict[str, Any]:
        return out.setdefault(job_id, {}).setdefault(model_ref, {
            "attempts": 0, "outcomes": {o: 0 for o in OUTCOMES}, "unfinished": 0, "cache_hits": 0,
            "usage_family": None,
            "reported": {f: 0 for f in TOKEN_FIELDS}, "unknown": {f: 0 for f in TOKEN_FIELDS}})

    for r in rows:
        e = entry(r["job_id"], r["model_ref"])
        e["attempts"] += 1
        if r["status"] == "started":
            e["unfinished"] += 1
            for f in TOKEN_FIELDS:
                e["unknown"][f] += 1
            continue
        e["outcomes"][r["status"]] = e["outcomes"].get(r["status"], 0) + 1
        e["usage_family"] = e["usage_family"] or r["usage_family"]
        for f in TOKEN_FIELDS:
            if r[f] is None:
                e["unknown"][f] += 1
            else:
                e["reported"][f] += r[f]
    for h in hits:
        entry(h["job_id"], h["model_ref"])["cache_hits"] += h["hits"]
    return {i: (None if i in older else out.get(i, {})) for i in ids}


def report_lines(jobs_db: Path, job_id: str) -> list[str]:
    """CLI lines: per model, attempts, cache hits and **reported** tokens with unknown counts. A
    missing or unreadable ledger is said so, never reported as no usage."""
    try:
        summary = summarize(jobs_db, job_id)
    except LedgerUnavailable as exc:
        return [f"LLM usage: unknown — {exc}."]
    if summary is None:
        return ["LLM usage: not recorded — this job predates the usage ledger."]
    if not summary:
        return ["LLM usage: no model attempts and no cache hits."]
    lines = ["LLM usage (reported tokens; prices not tracked):"]
    for model_ref, e in sorted(summary.items()):
        failed = {k: v for k, v in e["outcomes"].items() if k != "ok" and v}
        parts = [f"{e['attempts']} attempt(s)"]
        if failed:
            parts.append("failed: " + ", ".join(f"{v} {k}" for k, v in sorted(failed.items())))
        if e["unfinished"]:
            parts.append(f"{e['unfinished']} unfinished")
        parts.append(f"{e['cache_hits']} cache hit(s)")
        lines.append(f"  {model_ref}: " + "; ".join(parts))
        if e["attempts"]:
            tok = ", ".join(f"{f.replace('_tokens', '')} {e['reported'][f]}"
                            + (f" (+{e['unknown'][f]} unknown)" if e["unknown"][f] else "")
                            for f in TOKEN_FIELDS)
            note = (" — input includes cache_read" if e["usage_family"] == "openai_compatible"
                    else " — input excludes cache read/write" if e["usage_family"] == "anthropic"
                    else "")
            lines.append(f"    tokens: {tok}{note}")
    return lines
