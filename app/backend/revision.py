#!/usr/bin/env python3
"""The vault revision, `db/revision.json` (ADR-0068 decision 6).

`{"schema_version", "epoch", "counter"}`. It tracks **potential** authority changes, not detected
ones: every write-lock hold is mutating unless it says `changes=False`, and the lock persists the
increment **before** it yields (`app/backend/write_lock.py`), so a process that writes and dies
cannot leave an earlier Apply preview valid. Every restore starts a new random `epoch`, so two
histories that diverge after a restore never share a revision.

The file records no work, only "something may have changed", so it **self-heals** instead of failing
closed: a missing file is initialized with a new epoch; malformed contents are copied aside
(`revision.damaged-<ts>-<hex>.json`, flushed) and the file is atomically replaced with a new epoch.
A new epoch only makes outstanding previews stale. An I/O failure (permissions, storage) is not
malformed contents: it raises :class:`RevisionUnavailable`, and the hold refuses before any write.

Every function here runs under the exclusive write lock (the caller holds it), so concurrent
processes initialize or recover to the same epoch. Plain IO, so the write lock can import it.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.backend.publication_queue import _sync_dir, _write_durably

REVISION_NAME = "revision.json"
SCHEMA_VERSION = 1
_EPOCH = re.compile(r"[0-9a-f]{32}")


class RevisionUnavailable(RuntimeError):
    """The revision file could not be read, preserved or replaced (an I/O failure)."""

    def __init__(self, op: str, exc: OSError) -> None:
        super().__init__(f"vault revision unavailable ({op}: {type(exc).__name__}: {exc}); "
                         "check db/ permissions and free space")
        self.op = op


def revision_path(root: Path) -> Path:
    return Path(root) / "db" / REVISION_NAME


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(data: bytes) -> dict[str, Any] | None:
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (not isinstance(body, dict) or set(body) != {"schema_version", "epoch", "counter"}
            or body["schema_version"] != SCHEMA_VERSION
            or not isinstance(body["epoch"], str) or not _EPOCH.fullmatch(body["epoch"])
            or type(body["counter"]) is not int or body["counter"] < 0):
        return None
    return {"epoch": body["epoch"], "counter": body["counter"]}


def _save(root: Path, rev: dict[str, Any]) -> None:
    path = revision_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp-{uuid.uuid4().hex[:8]}")
        _write_durably(tmp, (json.dumps({"schema_version": SCHEMA_VERSION, **rev}) + "\n")
                       .encode("utf-8"))
        tmp.replace(path)
        _sync_dir(path.parent)
    except OSError as exc:
        raise RevisionUnavailable("replace", exc) from exc


def _fresh() -> dict[str, Any]:
    return {"epoch": uuid.uuid4().hex, "counter": 0}


def current(root: Path) -> dict[str, Any]:
    """The revision `{"epoch", "counter"}`, initializing or recovering the file first if needed."""
    path = revision_path(root)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        rev = _fresh()
        _save(root, rev)
        return rev
    except OSError as exc:
        raise RevisionUnavailable("read", exc) from exc
    rev = _parse(data)
    if rev is not None:
        return rev
    # Malformed: keep the evidence (copied and flushed) before replacing the active file, so a crash
    # at any point leaves the damaged file or its copy, never neither.
    stamp = _iso_now().replace(":", "").replace("-", "")
    try:
        _write_durably(path.with_name(f"revision.damaged-{stamp}-{uuid.uuid4().hex[:6]}.json"), data)
        _sync_dir(path.parent)
    except OSError as exc:
        raise RevisionUnavailable("preserve", exc) from exc
    rev = _fresh()
    _save(root, rev)
    return rev


def advance(root: Path) -> dict[str, Any]:
    """Persist `counter + 1` (durably) and return the new revision."""
    rev = current(root)
    rev = {"epoch": rev["epoch"], "counter": rev["counter"] + 1}
    _save(root, rev)
    return rev


def new_epoch(root: Path) -> dict[str, Any]:
    """Durably start a new random epoch (restore)."""
    rev = _fresh()
    _save(root, rev)
    return rev


def damaged_copies(root: Path) -> list[Path]:
    """Copies of malformed revision files kept by recovery (reported as a warning, never a failure)."""
    db = Path(root) / "db"
    return sorted(db.glob("revision.damaged-*.json")) if db.exists() else []
