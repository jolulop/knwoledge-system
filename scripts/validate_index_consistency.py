#!/usr/bin/env python3
"""Validate that the derived keyword index is coherent with — and fresh against — disk.

The keyword index (``indexes/keyword/keyword.sqlite``, ADR-0032 §7) is a generated artifact
rebuilt from the per-source chunks and the typed wiki pages. It is unsafe to search a *stale*
index, so this check compares the index against the live chunk/page sets **in both directions**
using the index's own fingerprint tables, and fails when:

- the index schema version no longer matches the builder (a stale-schema index must be rebuilt);
- the index is internally inconsistent (FTS rows vs fingerprint tables disagree);
- a live chunk file / typed wiki page is **missing from the index** (added but not reindexed);
- a live chunk file / wiki page **changed** since indexing (fingerprint drift, not reindexed);
- the index references a source/page that **no longer exists** on disk (removed, not reindexed).

The index is optional local runtime state (gitignored, regenerable). A missing index file is not
an error: there is simply nothing to validate. Any failure is fixed by a reindex.

Boundary (ADR-0032, decision recorded for Phase 4a): this validator checks index↔chunk/page
coherence only. Whether a normalized chunk *should* exist (manifest present, ingestion succeeded)
is the job of ``validate_normalized.py``, which runs in the same ``validate_all.py`` pass.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import keyword_index


def _check_freshness(
    label: str,
    live: dict[str, Path],
    stored: dict[str, str],
    indexed: set[str],
    *,
    reindex_hint: str,
) -> list[str]:
    """Compare a live disk set against the index's fingerprint table, both directions."""
    errors: list[str] = []
    if set(indexed) != set(stored):
        errors.append(
            f"{label} index internally inconsistent: FTS rows {sorted(indexed)} != "
            f"fingerprint table {sorted(stored)}"
        )
    for key in sorted(set(stored) - set(live)):
        errors.append(f"{label} index references {key}, which no longer exists on disk ({reindex_hint})")
    for key, path in sorted(live.items()):
        if key not in stored:
            errors.append(f"{label} {key} exists on disk but is not indexed ({reindex_hint})")
        elif stored[key] != keyword_index.file_fingerprint(path):
            errors.append(f"{label} index is stale for {key}: it changed since indexing ({reindex_hint})")
    return errors


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0]).resolve() if argv else Path.cwd()
    db_path = root / keyword_index.DB_RELPATH

    if not db_path.exists():
        print("Index consistency validation passed (no keyword index present).")
        return 0

    errors: list[str] = []
    conn = keyword_index.connect(db_path)
    try:
        version = keyword_index.index_version(conn)
        if version != keyword_index.INDEX_VERSION:
            errors.append(
                f"keyword index schema version {version} != builder version "
                f"{keyword_index.INDEX_VERSION} (stale schema)"
            )
        errors += _check_freshness(
            "evidence",
            keyword_index.chunk_files(root),
            keyword_index.stored_source_fingerprints(conn),
            keyword_index.indexed_source_ids(conn),
            reindex_hint="reindex",
        )
        errors += _check_freshness(
            "navigation",
            keyword_index.navigation_pages(root),
            keyword_index.stored_nav_fingerprints(conn),
            keyword_index.indexed_navigation_paths(conn),
            reindex_hint="reindex",
        )
    finally:
        conn.close()

    if errors:
        print("Index consistency validation failed:")
        for err in errors:
            print(f"- {err}")
        print("Rebuild the index: scripts/reindex_keyword.py [ROOT] --force")
        return 1
    print("Index consistency validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
