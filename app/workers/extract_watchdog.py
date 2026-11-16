#!/usr/bin/env python3
"""The extraction watchdog (ADR-0074 decision 7.2): one minimal process per extraction child.

A parser stuck in C code, or holding the GIL, can stop any monitor thread or signal handler inside
the child, so the child's deadline and the parent's death are enforced from outside it. Started by
`app/workers/extract_runner.py` as ``python -I -S <this file> <fd> <pgid> <budget_s> <grace_s>``:
standard library only, no site packages, nothing inherited but the read end of the control pipe
(``<fd>``), whose only writer is the parent.

- ``x`` on the pipe: the parent stands the watchdog down (it terminates and reaps the child itself).
- EOF on the pipe: the parent is gone — the child's group is terminated (SIGTERM, ``grace_s``,
  SIGKILL).
- ``budget_s`` elapsed: the deadline backstop — the group is terminated the same way.

It never signals its own process group, and ignores SIGINT / SIGHUP (a terminal's Ctrl-C or hang-up
reaches the parent, which cleans up); a SIGTERM to the watchdog terminates the child group first.
"""
from __future__ import annotations

import os
import select
import signal
import sys
import time

STAND_DOWN = b"x"
READY = b"R"


def _alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate(pgid: int, grace: float) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    end = time.monotonic() + grace
    while time.monotonic() < end and _alive(pgid):
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def main(argv: list[str]) -> int:
    fd, pgid, budget, grace = int(argv[1]), int(argv[2]), float(argv[3]), float(argv[4])
    if pgid <= 1 or pgid == os.getpgrp():
        return 2                                   # never the watchdog's (the parent's) own group
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    def _on_term(signum, frame):
        terminate(pgid, grace)
        os._exit(0)

    signal.signal(signal.SIGTERM, _on_term)
    os.write(1, READY)
    deadline = time.monotonic() + budget
    poller = select.poll()                         # not select(): the fd may be numbered > 1024
    poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            terminate(pgid, grace)
            return 0
        if poller.poll(max(1, int(remaining * 1000))):
            data = os.read(fd, 1)
            if data == STAND_DOWN:
                return 0
            if not data:
                terminate(pgid, grace)
                return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
