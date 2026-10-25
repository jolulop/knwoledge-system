#!/usr/bin/env python3
"""Restore the graph's tag links from the ADR-0066 ledgers (review round 2 recovery path).

Usage:
    uv run python scripts/rebuild_tag_links.py [ROOT]            # dry run: report only
    uv run python scripts/rebuild_tag_links.py [ROOT] --apply    # write the repair

The ledgers (`reviews/tag_links/<source_id>.json`) are the authority. The repair re-asserts every
keep missing from the graph (e.g. after a graph rebuild or restore), finishes an interrupted keep
(activation with the recorded amendments, or the revival of an unused tag), rejects active links
of tags a document dropped, and rejects human links with no keep in the ledger (an interrupted
drop). It re-renders the touched tag pages and Source pages and refreshes `wiki/index.md` + the
keyword index. Never touched: a document whose ledger is damaged (restore it from backup) and the
tags of an in-flight merge/split (`reviews/in_flight/`; run `/reviews/apply` to finish it first).
Never touches raw/.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph
from app.workers import tag_links


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    positional = [a for a in argv if not a.startswith("-")]
    root = Path(positional[0]).resolve() if positional else Path.cwd()
    db_path = root / "db" / "graph.sqlite"
    if not db_path.exists():
        print("No graph database — nothing to repair.")
        return 0
    conn = graph.connect(db_path)
    try:
        out = tag_links.repair(conn, reviews_dir=root / "reviews", wiki_dir=root / "wiki",
                               apply=apply)
        conn.commit()
    finally:
        conn.close()
    print(f"Tag-link repair ({'applied' if apply else 'dry run'}):")
    print(f"  keeps re-asserted: {out['missing_keeps']}")
    print(f"  interrupted keeps finished (activation): {out['interrupted_activations']}")
    print(f"  interrupted revivals finished: {out['interrupted_revivals']}")
    print(f"  dropped links rejected: {out['undropped']}")
    print(f"  human links without a keep rejected: {out['orphan_keeps']}")
    print(f"  keeps naming a gone/unlinkable tag (not repairable): {out['invalid_keeps']}")
    for sid in out["skipped_damaged"]:
        print(f"  SKIPPED {sid}: damaged ledger — its links are left untouched (restore from backup)")
    for rid in out["deferred"]:
        print(f"  DEFERRED merge/split {rid} is in flight — run /reviews/apply to finish it first")
    for b in out["activation_blocked"]:
        print(f"  activation of {b['node_id']} still blocked: {b['reason']}")
    if apply and out["affected_sources"]:
        refresh = tag_links.finish(root, out["affected_sources"], wiki_dir=root / "wiki")
        for w in refresh["warnings"]:
            print(f"  warning: {w}")
    elif not apply and any(out[k] for k in tag_links._PLAN_KEYS if k != "invalid_keeps"):
        print("Re-run with --apply to write the repair.")
    return 1 if (out["skipped_damaged"] or out["deferred"] or out["invalid_keeps"]
                 or out["activation_blocked"]) else 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
