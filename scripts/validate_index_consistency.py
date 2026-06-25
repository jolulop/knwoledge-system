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


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0]).resolve() if argv else Path.cwd()
    db_path = root / keyword_index.DB_RELPATH

    if not db_path.exists():
        print("Index consistency validation passed (no keyword index present).")
        return 0

    # Single source of truth: keyword_index.consistency_errors (also the retrieval eval's --vault gate).
    conn = keyword_index.connect(db_path)
    try:
        errors = keyword_index.consistency_errors(root, conn)
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
