#!/usr/bin/env python3
"""Report vector-index health (Phase 4d-2, ADR-0033).

The vector index (``indexes/vector/``) is optional, derived, and refreshed **explicitly**, so it is
expected to drift between reindexes. This check therefore:

- **passes** when there is no vector index (merely missing is fine);
- **fails** on **incoherent** metadata when an index *does* exist (unreadable/old-schema ``_meta.json``,
  or metadata present but the LanceDB table missing) — that index is unsafe to query;
- **reports (non-fatal)** chunk-level staleness (chunks changed/added since embed, or removed) so the
  operator knows to rerun ``scripts/reindex_vector.py`` — it does not auto-fix or fail on drift.

Chunk-level staleness can only be inspected when the ``vector`` extra (LanceDB) is installed; without
it, metadata coherence is still checked.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import vector_index
from app.backend.config import get_settings


def _expected_meta(root: Path) -> vector_index.VectorMeta | None:
    """The intended index identity from the embedding config, or None if no embedder is configured.

    When configured, the validator compares the index's stored model_ref/dimension/distance_metric
    against this (a mismatch is a hard failure — the index is unsafe to query).
    """
    s = get_settings(root)
    if not s.embedding_model_ref:
        return None
    return vector_index.VectorMeta(
        embedding_model_ref=s.embedding_model_ref,
        embedding_code_version=vector_index.EMBED_CODE_VERSION,
        distance_metric=s.embedding_distance_metric,
        dimension=s.embedding_dimension,
        index_schema_version=vector_index.INDEX_SCHEMA_VERSION,
    )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0]).resolve() if argv else Path.cwd()
    st = vector_index.status(root, expected=_expected_meta(root))

    if not st.present:
        print("Vector index validation passed (no vector index present).")
        return 0

    if not st.coherent:
        print("Vector index validation failed (incoherent — index is unsafe to query):")
        for issue in st.issues:
            print(f"- {issue}")
        print("Rebuild the vector index: scripts/reindex_vector.py [ROOT] --force")
        return 1

    # Coherent: surface notes + chunk staleness as warnings, but pass (drift is by design).
    for note in st.notes:
        print(f"note: {note}")
    if st.inspected and (st.stale_or_missing_chunks or st.removed_chunks):
        print(
            f"warning: vector index is stale — {st.stale_or_missing_chunks} chunk(s) changed/missing, "
            f"{st.removed_chunks} removed; rerun scripts/reindex_vector.py to refresh."
        )
    print("Vector index validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
