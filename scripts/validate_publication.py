#!/usr/bin/env python3
"""Validate that local publication is complete (ADR-0068 decision 4).

Fails while `db/publication_pending.json` holds an entry: some committed change has not yet
reached its Source or tag pages, `wiki/index.md` or the keyword index. Also fails when the queue
file cannot be read (a damaged queue is never an empty one). Read-only: it never publishes.
`scripts/republish.py` retries the queue; `--repair` sets a damaged file aside and republishes
from authority. No queue file is a pass.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import publication_queue


def _check(root: Path) -> list[str]:
    try:
        entries = publication_queue.load(root)
    except publication_queue.QueueCorrupt as exc:
        return [f"the publication queue is unreadable ({exc.reason}) — repair it with "
                "`uv run python scripts/republish.py --repair`"]
    if not entries:
        return []
    left = publication_queue.outstanding(entries)
    steps = [f"{len(left['sources'])} Source page(s)"] if left["sources"] else []
    steps += [f"{len(left['items'])} tag page(s)"] if left["items"] else []
    steps += ["every page (full republish)"] if left["full"] else []
    steps += ["wiki/index.md"] if left["index"] else []
    steps += ["the keyword index"] if left["keyword"] else []
    return [f"publication incomplete: {len(entries)} pending entr{'y' if len(entries) == 1 else 'ies'}"
            f" ({', '.join(steps) or 'no steps'}) — retry with "
            "`uv run python scripts/republish.py`"]


def main(argv: list[str]) -> int:
    root = Path(argv[0]).resolve() if argv else Path.cwd()
    errors = _check(root)
    if errors:
        print("Publication validation failed:")
        for err in errors:
            print(f"- {err}")
        return 1
    print("Publication validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
