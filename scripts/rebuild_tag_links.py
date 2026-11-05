#!/usr/bin/env python3
"""Restore the graph's tag links from the ADR-0066 ledgers (review round 2 recovery path).

Usage:
    uv run python scripts/rebuild_tag_links.py [ROOT]            # dry run: report only
    uv run python scripts/rebuild_tag_links.py [ROOT] --apply    # write the repair

The ledgers (`reviews/tag_links/<source_id>.json`) are the authority. The repair re-asserts every
keep missing from the graph (e.g. after a graph rebuild or restore), finishes an interrupted keep
(activation with the recorded amendments, or the revival of an unused tag), rejects active links
of tags a document dropped, and rejects human links with no keep in the ledger (an interrupted
drop). Review round 6: it also recomputes a tag's status from its keeps (a drop stopped before the
status refresh), withdraws suggestions of a blocked tag (a block stopped after the list write),
finishes a deletion stopped halfway, and re-renders tag/Source pages that no longer match the graph.
It re-renders the touched tag pages and Source pages and refreshes `wiki/index.md` + the
keyword index. Never touched: a document whose ledger is damaged (restore it from backup) and the
tags of an in-flight merge/split (`reviews/in_flight/`; run `/reviews/apply` to finish it first).
Never touches raw/.

Tagging history (ADR-0072 decisions 4–5): the same run backfills each ledger's `tagging` marker
from evidence (kept/drop timestamps, attributable tag audit entries) and, with --apply, writes the
vault's `reviews/tag_links/_tracking.json` only if absent (`from_start: false`). A fresh vault —
created from raw/ by the clean restart, before any tag decision — is certified once with:

    uv run python scripts/rebuild_tag_links.py [ROOT] --init-fresh-tracking

which records `from_start: true` and refuses when any ledger or tag audit entry already exists.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph, publication_queue, write_lock
from app.backend.manifests import iso_now
from app.workers import tag_history, tag_links


def _tagging(root: Path, *, apply: bool) -> int:
    """The tagging-history migration (graph-independent; under the write lock when applying)."""
    if apply:
        with write_lock.hold(root / "reviews"):
            out = tag_links.migrate_tagging(root / "reviews", apply=True, now=iso_now())
    else:
        out = tag_links.migrate_tagging(root / "reviews", apply=False)
    print(f"Tagging history ({'applied' if apply else 'dry run'}):")
    print(f"  ledgers {'backfilled' if apply else 'to backfill'}: {len(out['backfilled'])} "
          f"({sum(b['tagging']['state'] == 'tagged' for b in out['backfilled'])} tagged, "
          f"{sum(b['tagging']['state'] == 'unknown' for b in out['backfilled'])} unknown)")
    if out["tracking_written"]:
        print("  tracking record: " + ("written (from_start: false)" if apply
                                       else "would be written (from_start: false)"))
    for d in out["skipped_damaged"]:
        print(f"  SKIPPED {d['source_id']}: damaged ledger ({d['problem']})")
    return 1 if out["skipped_damaged"] else 0


def _init_fresh(root: Path) -> int:
    with write_lock.hold(root / "reviews"):
        out = tag_history.init_fresh(root / "reviews", iso_now())
    if out["outcome"] == "initialized":
        print("Tagging history: this vault is recorded as fresh (from_start: true) — every document "
              "without a decision now shows as not yet tagged.")
        return 0
    print(f"Refused: {out['reason']} — this vault already has tagging history, so it can't be "
          "certified as fresh. Use the migration (run without --init-fresh-tracking).")
    return 1


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    positional = [a for a in argv if not a.startswith("-")]
    root = Path(positional[0]).resolve() if positional else Path.cwd()
    if "--init-fresh-tracking" in argv:
        return _init_fresh(root)
    tagging_rc = _tagging(root, apply=apply)
    if not (root / "db" / "graph.sqlite").exists():
        print("No graph database — nothing to repair.")
        return tagging_rc
    if not apply:
        return _repair(root, apply=False, intent=None) or tagging_rc
    # ADR-0068: one commit phase; its publication intent is recorded first (a crash mid-repair
    # republishes from authority).
    with write_lock.hold(root / "reviews"):
        intent = publication_queue.record(root, origin="tag_repair", full=True)
        return _repair(root, apply=True, intent=intent) or tagging_rc


def _repair(root: Path, *, apply: bool, intent: str | None) -> int:
    conn = graph.connect(root / "db" / "graph.sqlite")
    try:
        out = tag_links.repair(conn, reviews_dir=root / "reviews", wiki_dir=root / "wiki",
                               policies_dir=root / "policies", apply=apply)
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
    print(f"  tag statuses recomputed from keeps (active/unused): {out['stale_status']}")
    print(f"  blocked suggestions withdrawn: {out['unwithdrawn_blocks']}")
    print(f"  interrupted deletions finished (recorded intent only): {out['interrupted_deletions']}")
    print(f"  stale deletion records dropped (tag kept/protected meanwhile): "
          f"{out['cancelled_deletions']}")
    print(f"  tags with a missing page (not repairable — restore from backup): {out['missing_pages']}")
    print(f"  stale tag pages re-rendered: {out['stale_item_pages']}")
    print(f"  stale Source pages re-rendered: {out['stale_source_pages']}")
    print(f"  unfinished 'use X' choices (never resumed here — retry or cancel on the document "
          f"screen): {out['pending_resolutions']}")
    for sid in out["skipped_damaged"]:
        print(f"  SKIPPED {sid}: damaged ledger — its links are left untouched (restore from backup)")
    for rid in out["deferred"]:
        print(f"  DEFERRED merge/split {rid} is in flight — run /reviews/apply to finish it first")
    for b in out["activation_blocked"]:
        print(f"  activation of {b['node_id']} still blocked: {b['reason']}")
    if apply:
        refresh = tag_links.finish(root, out["affected_sources"], wiki_dir=root / "wiki",
                                   pages_changed=out["pages_changed"], intent=intent,
                                   origin="tag_repair")
        for w in refresh["warnings"]:
            print(f"  warning: {w}")
    elif not apply and any(out[k] for k in tag_links._PLAN_KEYS if k not in tag_links._REPORT_ONLY):
        print("Re-run with --apply to write the repair.")
    return 1 if (out["skipped_damaged"] or out["deferred"] or out["invalid_keeps"] or out["missing_pages"]
                 or out["activation_blocked"]) else 0

def _locked_main(argv: list[str]) -> int:
    """Run as a vault writer (ADR-0039 amendment): a backup never captures a half-done repair."""
    from app.backend import vault_lock
    positional = [a for a in argv if not a.startswith("-")]
    root = Path(positional[0]).resolve() if positional else Path.cwd()
    from app.backend import run_guard
    with vault_lock.shared(root / "db"):
        return run_guard.run_cli(root / "db", "rebuild_tag_links", lambda: main(argv))


if __name__ == "__main__":
    raise SystemExit(_locked_main(sys.argv[1:]))
