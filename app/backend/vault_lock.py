#!/usr/bin/env python3
"""Vault-wide writer lock (ADR-0039 amendment; ADR-0066 review round 6, blocking issue 3).

`db/.vault.lock` coordinates writers with backup/restore:
- **writers hold it shared** — every producer job for as long as its jobs-database row says
  `running` (taken in `db.insert_job`, released when `db.update_job` records a terminal status),
  every state-changing API request (the ADR-0067 middleware), and the write scripts that record no
  job (`rebuild_tag_links.py`, `reconcile_reviews.py`, `reindex_vector.py`). `rebuild_index.py`
  (dependency-free, writes only the derived `wiki/index.md`) and `reindex_keyword.py` (a derived
  index that is never backed up) are not writers of backed-up authority;
- **backup and restore hold it exclusive** for the whole archive read/write, waiting up to a timeout
  for running writers to finish; a writer that starts meanwhile waits for the backup.

It is an `flock`, so a crashed process releases it automatically — a stale `running` row never
blocks a backup. Shared holders never block each other, so nested writers (a request that runs a
job, a job that spawns `rebuild_index.py`) cannot deadlock. Without `fcntl` (non-POSIX) it is a
no-op, like the tag-ledger lock's cross-process half.
"""
from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

try:
    import fcntl
except ImportError:                                       # non-POSIX: no cross-process exclusion
    fcntl = None  # type: ignore[assignment]

LOCK_NAME = ".vault.lock"
DEFAULT_TIMEOUT = 60.0


class VaultBusy(RuntimeError):
    """The exclusive lock could not be taken in time (writers are still running)."""


def lock_path(db_dir: Path) -> Path:
    return Path(db_dir) / LOCK_NAME


def acquire_shared(db_dir: Path) -> IO[Any] | None:
    """Take the writer (shared) lock and return its handle — pass it to :func:`release`. Blocks while
    a backup/restore holds the lock."""
    if fcntl is None:
        return None
    path = lock_path(db_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a", encoding="utf-8")                # noqa: SIM115 - released by release()
    try:
        fcntl.flock(fh, fcntl.LOCK_SH)
    except BaseException:
        fh.close()
        raise
    return fh


def release(handle: IO[Any] | None) -> None:
    if handle is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


@contextlib.contextmanager
def shared(db_dir: Path) -> Iterator[None]:
    handle = acquire_shared(db_dir)
    try:
        yield
    finally:
        release(handle)


@contextlib.contextmanager
def exclusive(db_dir: Path, *, timeout: float = DEFAULT_TIMEOUT, poll: float = 0.1) -> Iterator[None]:
    """Hold the lock exclusively (backup/restore). Raises :class:`VaultBusy` when writers still hold
    it after `timeout` seconds."""
    if fcntl is None:
        yield
        return
    path = lock_path(db_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise VaultBusy(f"writers still hold {path} after {timeout:g}s") from None
                time.sleep(poll)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
