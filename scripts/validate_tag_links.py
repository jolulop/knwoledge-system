#!/usr/bin/env python3
"""Validate the ADR-0066 tag-decision ledgers against the graph (review round 2).

The per-document ledgers (`reviews/tag_links/<source_id>.json`) are the authority for keep/drop;
the graph's `asserted_by: human` `mentions` edges are their mirror. This check fails when:
- a ledger file exists but does not parse (restore it from backup — it is never overwritten);
- a keep has no active human link, or names an item that is gone / no longer linkable;
- a document still has an active link to a tag it dropped (and does not keep);
- an active human link has no keep in the ledger (e.g. an interrupted drop);
- (review round 6) a tag's status disagrees with its keeps (active with none, unused with one), a
  blocked tag is still suggested, a deletion stopped halfway (node without page), or a tag/Source
  page no longer matches the graph (an action stopped before its refresh).

`scripts/rebuild_tag_links.py` restores the graph from the ledgers. With no ledgers and no graph
there is nothing to validate — a pass.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph
from app.workers import tag_links


def _check(root: Path) -> list[str]:
    reviews_dir = root / "reviews"
    db_path = root / "db" / "graph.sqlite"
    if not db_path.exists():
        return [f"{c['source_id']}: tag ledger is damaged ({c['problem']}) — restore it from backup"
                for c in tag_links.corrupt_ledgers(reviews_dir)]
    conn = graph.connect(db_path)
    try:
        if graph.schema_version(conn) != graph.SCHEMA_VERSION:
            return []                                    # validate_graph reports the stale schema
        return tag_links.consistency_errors(conn, reviews_dir, wiki_dir=root / "wiki",
                                            policies_dir=root / "policies")
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    root = Path(argv[0]).resolve() if argv else Path.cwd()
    errors = _check(root)
    if errors:
        print("Tag-link validation failed:")
        for err in errors:
            print(f"- {err}")
        return 1
    print("Tag-link validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
