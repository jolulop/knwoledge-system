#!/usr/bin/env python3
"""Build the deterministic keyword index over chunk evidence + wiki navigation (Phase 4a).

Thin CLI over ``app.backend.keyword_index``. Writes the derived FTS5 index to
``indexes/keyword/keyword.sqlite`` (ADR-0032 §7) — the evidence index over
``normalized/chunks/<source_id>.jsonl`` and the navigation index over typed ``wiki/**/*.md``.
This **retires** the Phase-0 scaffold that wrote a whole-file ``documents_fts`` into
``db/metadata.sqlite`` (it predates the chunk/anchor model and could not cite).

Usage:
    reindex_keyword.py [ROOT] [--force]

``--force`` rebuilds from scratch; otherwise only changed/added/removed sources and pages are
touched (fingerprinted incremental rebuild). The index is derived and gitignored: a missing index
is simply rebuilt.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import keyword_index


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    force = "--force" in argv
    positional = [a for a in argv if not a.startswith("-")]
    root = Path(positional[0]).resolve() if positional else Path.cwd()

    stats = keyword_index.reindex(root, force=force)
    db_path = root / keyword_index.DB_RELPATH
    mode = "full rebuild" if stats.full_rebuild else "incremental"
    print(
        f"keyword index ({mode}) -> {db_path}\n"
        f"  evidence: {stats.evidence_sources_indexed} sources indexed "
        f"({stats.evidence_chunks} chunks), {stats.evidence_sources_removed} removed\n"
        f"  navigation: {stats.navigation_pages_indexed} pages indexed, "
        f"{stats.navigation_pages_removed} removed\n"
        f"  skipped (unchanged): {stats.skipped}"
    )
    for warning in stats.warnings:
        print(f"  warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
