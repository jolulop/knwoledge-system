#!/usr/bin/env python3
"""Validate that no producer commit is unfinished (ADR-0068 slice 3, the producer journal).

Fails while `db/producer_journal.json` holds a record: a claims, items or synthesis commit stopped
before its last write, so its document or topic is redone by the next run of that producer (a
human decision may keep a synthesis topic blocked; the run's summary says so). Also fails when the
journal cannot be read (a damaged journal is never an empty one). Read-only; no file is a pass.
Publication retry (`scripts/republish.py`) never clears these records.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import producer_journal

_RERUN = {"extract_claims": "scripts/extract_claims.py", "extract_items": "scripts/extract_items.py",
          "generate_synthesis": "scripts/generate_synthesis.py"}


def _check(root: Path) -> list[str]:
    state = producer_journal.status(root)
    if state["state"] == "corrupt":
        return [f"the producer journal is unreadable ({state['reason']}) — repair it with "
                "`uv run python scripts/repair_producer_journal.py`"]
    return [f"{len(subjects)} unfinished {producer} commit(s) ({', '.join(subjects[:5])}"
            f"{', …' if len(subjects) > 5 else ''}) — rerun `uv run python {_RERUN[producer]}`"
            for producer, subjects in state.get("unfinished", {}).items()]


def main(argv: list[str]) -> int:
    root = Path(argv[0]).resolve() if argv else Path.cwd()
    errors = _check(root)
    if errors:
        print("Producer journal validation failed:")
        for err in errors:
            print(f"- {err}")
        return 1
    print("Producer journal validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
