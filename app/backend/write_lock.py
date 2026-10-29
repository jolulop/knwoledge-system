#!/usr/bin/env python3
"""The vault write lock (ADR-0068 decision 2).

One exclusive lock that every **commit phase** takes: the final validation plus the writes to the
graph, the tag ledgers, `reviews/`, the manifests, `policies/` or durable page fields. That is every
screen/API action, Apply and its executors, each producer's per-document commit, the review filers
and the repair scripts. Model calls stay outside it: a producer calls the model, then takes the
lock, re-validates (a source archived or a claim hidden during the call is respected), commits and
releases.

It widens the ADR-0066 tag-ledger lock and keeps its file, `reviews/tag_links/.lock`:
- a process lock covers the API's worker threads, and an exclusive `flock` covers other
  processes;
- it is **re-entrant per thread**, so a commit phase may call helpers that take it again.

It is not the vault lock (`app/backend/vault_lock.py`), which only keeps writers and backup/restore
apart. **Lock order: vault lock → write lock → SQLite transaction**, enforced here: the outermost
:func:`hold` takes the vault lock *shared* (at `<vault root>/db`, the reviews dir's sibling) before
the write lock, so a writer can never hold the write lock while waiting for the vault lock — the
promotion-vs-backup deadlock of ADR-0068 review round 1. A shared take nested in another writer's
shared hold never waits. The vault lock is also taken before the process lock, so the order holds
for threads of one process too. Backup and restore, which already hold the vault lock exclusively,
take the write lock with ``vault=False``.

**A corrupt publication queue refuses authority changes** (ADR-0068 decision 4, fail-closed): the
outermost hold checks that `db/publication_pending.json` is readable and raises
:class:`~app.backend.publication_queue.QueueCorrupt` if not. Holders that change no authority (backup,
restore, the queue repair itself) pass ``authority=False``.

**Every hold may change authority unless it says ``changes=False``** (ADR-0068 decision 6,
conservative invalidation): before yielding, the lock durably advances the vault revision
(`app/backend/revision.py`), once per outermost hold, so any Apply preview taken earlier is stale
even if the holder dies mid-write. A mutating hold nested in a ``changes=False`` hold advances it on
entry. If the increment cannot be persisted the hold raises before yielding, so nothing is written.
``changes=False`` is only for holders that cannot change any preview input: the preview snapshot
and its authorization, and backup.
"""
from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from pathlib import Path

from app.backend import publication_queue, revision, vault_lock

try:
    import fcntl
except ImportError:                                       # non-POSIX: the process lock still serializes
    fcntl = None  # type: ignore[assignment]

LOCK_DIR = "tag_links"
LOCK_NAME = ".lock"

_PROCESS_LOCK = threading.RLock()
_held = threading.local()


def lock_path(reviews_dir: Path) -> Path:
    return Path(reviews_dir) / LOCK_DIR / LOCK_NAME


@contextlib.contextmanager
def hold(reviews_dir: Path, *, vault: bool = True, authority: bool = True,
         changes: bool = True) -> Iterator[None]:
    """Hold the write lock for one commit phase. Re-entrant per thread.

    The outermost hold takes, in this order, the vault lock shared (``vault=False`` only for a
    holder of the exclusive vault lock), the process lock, then the `flock`. Taking the vault lock
    before the process lock means a writer never holds the process lock while it waits for a backup,
    so a backup running in a thread of the same process can still take the write lock. With
    ``authority`` (the default) it then refuses if the publication queue is unreadable. With
    ``changes`` (the default) it advances the vault revision before yielding."""
    if getattr(_held, "depth", 0):                        # thread-local: this thread holds it all
        if changes:
            invalidate()
        _held.depth += 1
        try:
            yield
        finally:
            _held.depth -= 1
        return
    path = lock_path(reviews_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    vault_handle = vault_lock.acquire_shared(Path(reviews_dir).parent / "db") if vault else None
    try:
        with _PROCESS_LOCK, open(path, "a", encoding="utf-8") as fh:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_EX)
            if authority:
                publication_queue.check_readable(Path(reviews_dir).parent)
            _held.depth = 1
            _held.root = Path(reviews_dir).parent
            _held.invalidated = False
            try:
                if changes:
                    invalidate()
                yield
            finally:
                _held.depth = 0
                if fcntl is not None:
                    fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        vault_lock.release(vault_handle)


def held() -> bool:
    """Whether this thread holds the write lock."""
    return bool(getattr(_held, "depth", 0))


def invalidate() -> None:
    """Durably advance the vault revision once for this outermost hold (the caller holds the lock).
    Apply calls it itself, after checking its preview token, under a ``changes=False`` hold."""
    if not held():
        raise RuntimeError("write_lock.invalidate() needs the write lock")
    if not _held.invalidated:
        revision.advance(_held.root)
        _held.invalidated = True
