#!/usr/bin/env python3
"""The publication pending queue, `db/publication_pending.json` (ADR-0068 decision 4).

Publication (the affected claim, synthesis, tag and Source pages, `index.md` and the keyword
index) runs after a commit, inside the write lock, but it is not atomic with it. So a writer records its **intent**
first: under the write lock, before it changes authority, it adds an entry naming the affected ids
and the steps required. Then it commits, then publishes (`app/workers/publication.py`), which clears
the entries whose steps all succeeded. A crash at any point leaves an entry to retry, and an
unnecessary refresh is harmless.

An entry holds ids and step flags, never rendered content:
`{"id", "origin", "created_at", "claims", "syntheses", "items", "sources", "index", "keyword",
"full", "provisional", "attempts"}`. `provisional` means the writer recorded only what it knew before
committing (the document and the tag it acted on); a retry widens it from current authority (every
Source page that links the tag). `full` means every page of every kind, the scope of an interrupted
Apply or a repair.

The file is durable coordination state: schema-versioned, written by atomic replacement, backed up
with `db/`. **A corrupt or unreadable queue is an error, never an empty queue**: :func:`load` raises
:class:`QueueCorrupt`, and the write lock refuses authority changes until `scripts/republish.py
--repair` preserves a copy of the damaged file and replaces it with a full-recovery entry. This
module is plain IO, so the write lock can
check it without importing the renderers.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.backend import manifests

QUEUE_NAME = "publication_pending.json"
SCHEMA_VERSION = 1

_ENTRY_ID = re.compile(r"pub_[0-9a-f]{12}")
_ITEM_ID = re.compile(r"itm_[0-9a-f]{16}")
_CLAIM_ID = re.compile(r"clm_[0-9a-f]{16}")
_SYNTHESIS_ID = re.compile(r"syn_[0-9a-f]{16}")
_ID_LISTS = (("claims", _CLAIM_ID), ("syntheses", _SYNTHESIS_ID), ("items", _ITEM_ID))
_ORIGIN = re.compile(r"[a-z][a-z0-9_]{0,40}")
_FLAGS = ("index", "keyword", "full", "provisional")


class QueueCorrupt(RuntimeError):
    """The queue file exists but cannot be read as a valid queue. `reason` is categorical."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"publication queue unreadable ({reason}): repair it with "
                         "`uv run python scripts/republish.py --repair`")
        self.reason = reason


def queue_path(root: Path) -> Path:
    return Path(root) / "db" / QUEUE_NAME


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_entry_id() -> str:
    return f"pub_{uuid.uuid4().hex[:12]}"


def _entry_problem(e: Any) -> str | None:
    if not isinstance(e, dict):
        return "entry_not_object"
    if not isinstance(e.get("id"), str) or not _ENTRY_ID.fullmatch(e["id"]):
        return "bad_entry_id"
    if not isinstance(e.get("origin"), str) or not _ORIGIN.fullmatch(e["origin"]):
        return "bad_origin"
    sources = e.get("sources")
    if not isinstance(sources, list) or not all(manifests.is_source_id(s) for s in sources):
        return "bad_sources"
    for key, pattern in _ID_LISTS:
        ids = e.get(key)
        if not isinstance(ids, list) or not all(isinstance(i, str) and pattern.fullmatch(i)
                                                for i in ids):
            return f"bad_{key}"
    if not all(isinstance(e.get(f), bool) for f in _FLAGS):
        return "bad_flags"
    if not isinstance(e.get("attempts"), int) or e["attempts"] < 0:
        return "bad_attempts"
    return None


def load(root: Path) -> list[dict[str, Any]]:
    """The pending entries, oldest first. No file means an empty queue; a file that is unreadable,
    not this schema or holds an invalid entry raises :class:`QueueCorrupt`."""
    path = queue_path(root)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise QueueCorrupt("unparseable") from None
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise QueueCorrupt("schema_version")
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise QueueCorrupt("entries_not_list")
    for e in entries:
        problem = _entry_problem(e)
        if problem:
            raise QueueCorrupt(problem)
    return [dict(e) for e in entries]


def _write_durably(path: Path, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save(root: Path, entries: list[dict[str, Any]]) -> None:
    """Replace the queue atomically and durably (flushed before and after the rename). The caller
    holds the write lock."""
    path = queue_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{uuid.uuid4().hex[:8]}")
    _write_durably(tmp, (json.dumps({"schema_version": SCHEMA_VERSION, "entries": entries},
                                    ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    os.replace(tmp, path)
    _sync_dir(path.parent)


def check_readable(root: Path) -> None:
    """Raise :class:`QueueCorrupt` when the queue cannot be read (the write lock's gate)."""
    load(root)


def make_entry(*, origin: str, sources: Any = (), items: Any = (), claims: Any = (),
               syntheses: Any = (), index: bool = True, keyword: bool = True, full: bool = False,
               provisional: bool = True, now: str | None = None) -> dict[str, Any]:
    """A new, validated entry (ids that are not canonical are dropped)."""
    entry = {"id": new_entry_id(), "origin": origin, "created_at": now or _iso_now(),
             "claims": sorted({c for c in claims if isinstance(c, str) and _CLAIM_ID.fullmatch(c)}),
             "syntheses": sorted({s for s in syntheses
                                  if isinstance(s, str) and _SYNTHESIS_ID.fullmatch(s)}),
             "items": sorted({i for i in items if isinstance(i, str) and _ITEM_ID.fullmatch(i)}),
             "sources": sorted({s for s in sources if manifests.is_source_id(s)}),
             "index": bool(index), "keyword": bool(keyword), "full": bool(full),
             "provisional": bool(provisional), "attempts": 0}
    problem = _entry_problem(entry)
    if problem:
        raise ValueError(f"invalid publication entry: {problem}")
    return entry


def _require_lock() -> None:
    from app.backend import write_lock
    if not write_lock.held():
        raise RuntimeError("publication intent is recorded under the write lock")


def record(root: Path, *, origin: str, sources: Any = (), items: Any = (), claims: Any = (),
           syntheses: Any = (), index: bool = True, keyword: bool = True, full: bool = False,
           provisional: bool = True, now: str | None = None) -> str:
    """Add one intent entry and return its id. Called under the write lock, before the commit."""
    _require_lock()
    entry = make_entry(origin=origin, sources=sources, items=items, claims=claims,
                       syntheses=syntheses, index=index, keyword=keyword, full=full,
                       provisional=provisional, now=now)
    entries = load(root)
    entries.append(entry)
    save(root, entries)
    return entry["id"]


def discard(root: Path, entry_id: str) -> None:
    """Remove one entry whose writer changed nothing (it refused before its first write)."""
    entries = load(root)
    kept = [e for e in entries if e["id"] != entry_id]
    if len(kept) != len(entries):
        save(root, kept)


def outstanding(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """What the entries still require, for results and the retry banner (counts and ids only)."""
    return {"entries": len(entries),
            "full": any(e["full"] for e in entries),
            "claims": sorted({c for e in entries for c in e["claims"]}),
            "syntheses": sorted({s for e in entries for s in e["syntheses"]}),
            "items": sorted({i for e in entries for i in e["items"]}),
            "sources": sorted({s for e in entries for s in e["sources"]}),
            "index": any(e["index"] for e in entries),
            "keyword": any(e["keyword"] for e in entries)}


def status(root: Path) -> dict[str, Any]:
    """The queue's state for a screen, never raising: `{"state": "empty"|"pending"|"corrupt", ...}`."""
    try:
        entries = load(root)
    except QueueCorrupt as exc:
        return {"state": "corrupt", "reason": exc.reason}
    if not entries:
        return {"state": "empty"}
    return {"state": "pending", **outstanding(entries)}


def preserve_damaged(root: Path, now: str | None = None) -> Path | None:
    """Copy the damaged queue file's bytes beside it, flushed to disk, and return the copy's path.
    The damaged file itself stays in place, so writes stay refused until :func:`replace_with_recovery`
    swaps it: a crash between the two leaves the vault blocked, never unguarded."""
    path = queue_path(root)
    if not path.exists():
        return None
    stamp = (now or _iso_now()).replace(":", "").replace("-", "")
    target = path.with_name(f"publication_pending.damaged-{stamp}-{uuid.uuid4().hex[:6]}.json")
    _write_durably(target, path.read_bytes())
    _sync_dir(path.parent)
    return target


def replace_with_recovery(root: Path, *, origin: str, now: str | None = None) -> str:
    """Replace the queue, whatever it holds, with one full-recovery entry (atomic and durable) and
    return its id. Only queue repair calls this, after :func:`preserve_damaged`."""
    _require_lock()
    entry = make_entry(origin=origin, full=True, provisional=False, now=now)
    save(root, [entry])
    return entry["id"]
