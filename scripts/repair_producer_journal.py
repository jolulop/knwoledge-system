#!/usr/bin/env python3
"""Repair an unreadable producer journal (ADR-0068 slice 3).

Usage:
    uv run python scripts/repair_producer_journal.py [ROOT]

The journal (`db/producer_journal.json`) records claims, items and synthesis commits that have not
finished. When it cannot be read, those three producers refuse to run, because an empty journal
would let an interrupted document look fresh. This keeps a copy of the damaged file beside it, then
replaces it with a record for every subject that may hold an unfinished commit: every document with
a claims or items artifact (its current claims or tags, plus any its superseded relationships left
without evidence) and every synthesis topic with an artifact. The next runs redo those documents —
from the response cache where the text is unchanged — and verify those topics. A readable journal
is left as it is. Never touches raw/.

Exit codes: 0 repaired (or nothing to repair), 3 a claims, items or synthesis run is in progress.
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import producer_journal, run_guard, write_lock


def main(argv: list[str]) -> int:
    positional = [a for a in argv if not a.startswith("-")]
    root = Path(positional[0]).resolve() if positional else Path.cwd()
    with write_lock.hold(root / "reviews", authority=False):
        try:
            records = producer_journal.load(root)
        except producer_journal.JournalCorrupt:
            kept = producer_journal.preserve_damaged(root)
            records = producer_journal.rebuild(root)
            print(f"Damaged journal kept as db/{kept.name}." if kept else "Damaged journal replaced.")
            for producer, subjects in producer_journal.describe(records).items():
                print(f"  {producer}: {len(subjects)} subject(s) to redo")
            print("Next: run claim, tag and synthesis extraction "
                  "(their next runs redo or verify these subjects).")
            return 0
    print(f"Producer journal is readable ({len(records)} unfinished commit(s)); nothing to repair.")
    return 0


def _guarded_main(argv: list[str]) -> int:
    """Hold the three producers' run guards, so none is running while its journal is replaced."""
    positional = [a for a in argv if not a.startswith("-")]
    root = Path(positional[0]).resolve() if positional else Path.cwd()
    try:
        with contextlib.ExitStack() as stack:
            for producer in producer_journal.PRODUCERS:
                stack.enter_context(run_guard.guard(root / "db", producer))
            return main(argv)
    except run_guard.ProducerBusy as exc:
        print(f"Refused: {exc}. Wait for it to finish, then run again.", file=sys.stderr)
        return run_guard.EXIT_BUSY


if __name__ == "__main__":
    raise SystemExit(_guarded_main(sys.argv[1:]))
