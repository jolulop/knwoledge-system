#!/usr/bin/env python3
"""One run at a time per producer (ADR-0068 decision 3).

Each producer run holds a non-blocking, cross-process `flock` on `db/.run-<producer>.lock` for its
whole duration. A second instance of the same producer refuses at once with :class:`ProducerBusy`
and writes nothing. Different producers may run at the same time; their commits serialize on the
write lock (`app/backend/write_lock.py`), which is never held during model calls, so tagging stays
available during a run.

The guard is taken at a run's **entry point**: the producer's script `__main__` or its API job
route. A producer called in-process by another (a tag action re-rendering Source pages through
`generate_wiki`) is a step of that caller, not a run, and takes no guard. Re-entrant per thread, so
an entry point that calls another entry point of the same producer does not refuse itself.
"""
from __future__ import annotations

import contextlib
import re
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

try:
    import fcntl
except ImportError:                                       # non-POSIX: no cross-process exclusion
    fcntl = None  # type: ignore[assignment]

_NAME = re.compile(r"[a-z][a-z0-9_]*")
_held = threading.local()
EXIT_BUSY = 3                                             # a script's exit code when refused
EXIT_QUEUE_CORRUPT = 4                                    # ... when the publication queue is damaged


class ProducerBusy(RuntimeError):
    """Another run of the same producer holds its guard."""

    def __init__(self, producer: str) -> None:
        super().__init__(f"{producer} is already running")
        self.producer = producer


def lock_path(db_dir: Path, producer: str) -> Path:
    if not _NAME.fullmatch(producer):
        raise ValueError(f"bad producer name {producer!r}")
    return Path(db_dir) / f".run-{producer}.lock"


@contextlib.contextmanager
def guard(db_dir: Path, producer: str) -> Iterator[None]:
    """Hold `producer`'s run guard, or raise :class:`ProducerBusy` without waiting."""
    path = lock_path(db_dir, producer)
    names = getattr(_held, "names", None)
    if names is None:
        names = _held.names = {}
    if names.get(producer):
        names[producer] += 1
        try:
            yield
        finally:
            names[producer] -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        if fcntl is not None:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ProducerBusy(producer) from None
        names[producer] = 1
        try:
            yield
        finally:
            names[producer] = 0
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_UN)


def run_cli(db_dir: Path, producer: str, main: Callable[[], int]) -> int:
    """A producer script's `__main__`: run `main()` under the guard; refuse with exit code
    :data:`EXIT_BUSY` and a one-line message when another run holds it, and with
    :data:`EXIT_QUEUE_CORRUPT` when a commit was refused because the publication queue is damaged
    (ADR-0068 decision 4)."""
    from app.backend import publication_queue
    try:
        with guard(db_dir, producer):
            return main()
    except ProducerBusy as exc:
        print(f"Refused: {exc}. Wait for it to finish, then run again.", file=sys.stderr)
        return EXIT_BUSY
    except publication_queue.QueueCorrupt as exc:
        print(f"Refused: {exc}.", file=sys.stderr)
        return EXIT_QUEUE_CORRUPT
