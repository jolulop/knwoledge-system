#!/usr/bin/env python3
"""Retry local publication (ADR-0068 decision 4).

Usage:
    uv run python scripts/republish.py [ROOT]            # publish what the queue holds
    uv run python scripts/republish.py [ROOT] --all      # republish everything from authority
    uv run python scripts/republish.py [ROOT] --repair   # keep a copy of a damaged queue, then --all

Publication brings the claim, synthesis, tag and Source pages, `wiki/index.md` and the keyword index
up to date with committed authority. A failed refresh leaves work in `db/publication_pending.json`; this runs it
once (the same function as the **Retry publishing** button) and prints what is still outstanding.
`--repair` is the only fix for an unreadable queue: a copy of the damaged file is kept beside it,
then the queue is replaced by one full-recovery entry and everything is republished from current
authority. The vector index is outside
publication (`scripts/reindex_vector.py`). Never touches raw/.

Exit codes: 0 published, 1 still pending, 3 another republish is running, 4 the queue is unreadable
(run --repair).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import publication_queue, run_guard
from app.workers import publication


def main(argv: list[str]) -> int:
    positional = [a for a in argv if not a.startswith("-")]
    root = Path(positional[0]).resolve() if positional else Path.cwd()
    try:
        if "--repair" in argv:
            out = publication.repair(root)
            if out["set_aside"]:
                print(f"Damaged queue kept as db/{out['set_aside']}.")
        elif "--all" in argv:
            out = publication.republish_all(root)
        else:
            out = publication.publish(root, origin="republish")
    except publication_queue.QueueCorrupt as exc:
        print(f"Refused: {exc}.", file=sys.stderr)
        return run_guard.EXIT_QUEUE_CORRUPT
    print(f"Publication: {out['status']}")
    print(f"  Source pages rendered: {len(out['sources_rendered'])}")
    print(f"  index.md rebuilt: {out['index_rebuilt']}")
    print(f"  keyword index refreshed: {out['keyword_reindexed']}")
    for w in out["warnings"]:
        print(f"  warning: {w}")
    for line in publication.describe_failures(out):
        print(f"  pending: {line}")
    if out["pending"]:
        left = out["pending"]
        print(f"  still pending: {left['entries']} entr{'y' if left['entries'] == 1 else 'ies'} "
              f"({len(left['claims'])} claim, {len(left['syntheses'])} synthesis, "
              f"{len(left['items'])} tag, {len(left['sources'])} Source pages, "
              f"index={left['index']}, keyword={left['keyword']})")
        return 1
    return 0


def _locked_main(argv: list[str]) -> int:
    positional = [a for a in argv if not a.startswith("-")]
    root = Path(positional[0]).resolve() if positional else Path.cwd()
    return run_guard.run_cli(root / "db", "republish", lambda: main(argv))


if __name__ == "__main__":
    raise SystemExit(_locked_main(sys.argv[1:]))
