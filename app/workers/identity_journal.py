#!/usr/bin/env python3
"""In-flight journal for identity operations (ADR-0066 review round 3).

A merge or split changes the tag-decision ledgers, graph links and pages in several steps. Before
its first write the executor records `reviews/in_flight/<review_id>.json` (`{review_id, op, nodes,
stage}`); the executor marks it `graph_done` when its own work is done, and the APPLY CALLER removes
it only after the Source pages, `wiki/index.md` and the keyword index refreshed (review round 4).
While the file exists:
- the next `/reviews/apply` RESUMES the operation (the executor adopts its own partial state —
  a tombstoned absorbed tag, a spin-off slot without its page — and the caller refreshes again);
- every competing human action on the operation's tags (keep, drop, undo-drop, human-add, block,
  Delete unused) is refused with `identity_operation_in_flight`, and extraction skips documents
  linked to them — a decision made against a half-finished screen is never silently reconciled;
- `tag_links.repair` leaves the operation's nodes alone (repairing a half-done merge from the
  ledgers would partly undo it), and the validator says to run apply first.

File-based and backed up with `reviews/`, like the rest of the ledger.

ADR-0073 decision 10: every write is durable (the temp file fsynced, renamed, the directory fsynced),
`set_stage` raises :class:`JournalUnreadable` instead of silently returning on a damaged entry, and a
caller records a stage only AFTER the page and graph changes it certifies are durable. A supersede
entry (`op: "supersede"`) also carries its binding: `winner`, `loser` and `approval_id`.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from app.backend.manifests import iso_now
from app.backend.publication_queue import _sync_dir, _write_durably

JOURNAL_DIR = "in_flight"
_RID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _path(reviews_dir: Path, review_id: str) -> Path:
    if not _RID_RE.fullmatch(str(review_id)):
        raise ValueError("non-canonical review id")       # never build a path from a forged id
    return Path(reviews_dir) / JOURNAL_DIR / f"{review_id}.json"


class JournalUnreadable(Exception):
    """An in-flight entry exists but can't be read or parsed: its operation's state is unknown, so the
    caller stops writing (ADR-0073 decision 10)."""

    def __init__(self, review_id: str) -> None:
        super().__init__(f"in-flight journal for {review_id} is unreadable")
        self.review_id = review_id


def _write(path: Path, data: dict[str, Any]) -> None:
    """Replace `path` durably: temp file written and fsynced, renamed, then the directory fsynced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{uuid.uuid4().hex[:8]}")
    _write_durably(tmp, (json.dumps(data, indent=2) + "\n").encode("utf-8"))
    os.replace(tmp, path)
    _sync_dir(path.parent)


def begin(reviews_dir: Path, review_id: str, *, op: str, nodes: list[str],
          now: str | None = None, binding: dict[str, Any] | None = None) -> None:
    """Open the entry at stage `started`, durably. `binding` adds operation-specific identity fields
    (a supersede's `winner`, `loser`, `approval_id`); it can't override the shared fields."""
    data: dict[str, Any] = dict(binding or {})
    data.update({"review_id": review_id, "op": op, "nodes": sorted(set(nodes)),
                 "stage": "started", "started_at": now or iso_now()})
    _write(_path(reviews_dir, review_id), data)


def read(reviews_dir: Path, review_id: str) -> dict[str, Any] | None:
    """The entry, None when there is none; raises :class:`JournalUnreadable` when it exists but can't
    be read or isn't a JSON object."""
    path = _path(reviews_dir, review_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JournalUnreadable(review_id) from exc
    if not isinstance(data, dict):
        raise JournalUnreadable(review_id)
    return data


def sync(reviews_dir: Path, review_id: str) -> None:
    """Repeat an existing entry's durability barrier (fsync the file, then the directory). Recovery
    calls it after validating a recovered entry and BEFORE acting on it: the run that wrote it may
    have failed after its rename, during the flush (ADR-0073 slice 2 review)."""
    path = _path(reviews_dir, review_id)
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    _sync_dir(path.parent)


def set_stage(reviews_dir: Path, review_id: str, stage: str) -> None:
    """Record how far the operation got, durably (`graph_done`: the executor finished; the caller's
    refreshes are still pending). The entry stays open. Raises :class:`JournalUnreadable` when the
    entry is missing or damaged — a stage is never recorded over an unknown state."""
    data = read(reviews_dir, review_id)
    if data is None:
        raise JournalUnreadable(review_id)
    data["stage"] = stage
    _write(_path(reviews_dir, review_id), data)


def is_open(reviews_dir: Path, review_id: str) -> bool:
    try:
        return _path(reviews_dir, review_id).exists()
    except ValueError:
        return False


def end(reviews_dir: Path, review_id: str) -> None:
    path = _path(reviews_dir, review_id)
    if path.exists():
        path.unlink()
        _sync_dir(path.parent)


def open_operations(reviews_dir: Path) -> list[dict[str, Any]]:
    """Every in-flight operation; an unreadable entry still counts (its nodes unknown → `nodes: None`,
    which callers treat as "everything is in flight")."""
    d = Path(reviews_dir) / JOURNAL_DIR
    out: list[dict[str, Any]] = []
    for path in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            nodes = data.get("nodes") if isinstance(data, dict) else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            data, nodes = {}, None
        out.append({"review_id": path.stem, "op": (data or {}).get("op"),
                    "nodes": nodes if isinstance(nodes, list) else None})
    return out


def operation_for(reviews_dir: Path, node_ids: Any) -> str | None:
    """The review id of an in-flight operation touching any of `node_ids` (an unreadable entry
    touches everything), else None."""
    wanted = {n for n in node_ids if isinstance(n, str)}
    for op in open_operations(reviews_dir):
        if op["nodes"] is None or wanted & set(op["nodes"]):
            return op["review_id"]
    return None
