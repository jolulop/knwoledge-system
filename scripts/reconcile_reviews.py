#!/usr/bin/env python3
"""CLI: catch-up sweep withdrawing extraction-stale review items (ADR-0057).

Usage:
    uv run python scripts/reconcile_reviews.py

Deterministic maintenance (no LLM, no API key): runs the shared reconciliation over the whole
`reviews/pending/` set against current graph state — a pending `promote_candidate_node` whose
node tombstoned/vanished is withdrawn, and a recompose-provenance `deprecate_wiki_page` whose
node has active mentions again is withdrawn. The sweep is the ONLY caller that also accepts
the pre-`reason_code` legacy prose constant (migration shim). Idempotent — a second run
withdraws nothing new. Approved/rejected items are never touched. Output is counts only;
every withdrawal writes its own `reviews/audit_log/` entry.

Fails closed BEFORE any mutation (exit non-zero, nothing withdrawn) when: the graph database
is absent; its schema version mismatches; it has no nodes; or the graph↔wiki lifecycle
projection is already invalid for any reviewed concept/entity-family node (page frontmatter
is the status authority, the graph a mirror — a drifted or wrong-root graph must never drain
the queue).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph  # noqa: E402
from app.backend.config import get_settings  # noqa: E402
from app.workers import reconcile  # noqa: E402


def main(argv: list[str]) -> int:
    settings = get_settings()
    if not settings.graph_db_path.exists():
        print(f"ERROR: graph database not found at {settings.graph_db_path} — "
              "refusing to sweep (every node would read as missing).")
        return 1
    gconn = graph.connect(settings.graph_db_path)
    try:
        counts = reconcile.sweep(settings.reviews_dir, gconn, wiki_dir=settings.wiki_dir)
    finally:
        gconn.close()
    if counts["refused"]:
        print("Review reconciliation sweep REFUSED (preflight failed, nothing withdrawn):")
        for failure in counts["refused"]:
            print(f"  {failure}")
        return 1
    print("Review reconciliation sweep complete.")
    print(f"Pending items scanned: {counts['scanned']}")
    print(f"Eligible (owned) items: {counts['eligible']}")
    print(f"Withdrawn: {counts['withdrawn']}")
    for reason, n in sorted(counts["withdrawn_by_reason"].items()):
        print(f"  {reason}: {n}")
    print(f"Not owned (foreign-reason deprecations): {counts['not_owned']}")
    print(f"Left unresolved (premise still valid): {counts['left_unresolved']}")
    print(f"Terminal-status files found in pending/ (skipped): {counts['terminal_in_pending']}")
    print(f"Parse errors (skipped): {counts['parse_errors']}")
    print(f"Schema errors (skipped): {counts['schema_errors']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
