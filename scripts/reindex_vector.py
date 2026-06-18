#!/usr/bin/env python3
"""Build the vector index over chunk evidence (Phase 4d-2, ADR-0033).

Thin CLI over ``app.backend.vector_index``. Embeds the per-source chunks into the LanceDB store at
``indexes/vector/`` via the configured local embedding server. This is an **explicit** step — it is
NOT wired into the per-file change hook (embedding is GPU/latency-heavy and must not make ordinary
editing depend on the embedding server). Run it deliberately after ingest batches or before
retrieval evals; the keyword index stays the cheap always-fresh channel.

Usage:
    reindex_vector.py [ROOT] [--force]

``--force`` fully rebuilds (atomic temp-dir swap); otherwise only changed/added/removed chunks are
re-embedded. An index-level staleness mismatch (model_ref / code / metric / dimension / schema)
refuses an incremental run and asks for ``--force``. Requires a configured embedder
(EMBEDDING_BASE_URL + EMBEDDING_MODEL_REF) and the ``vector`` extra (LanceDB) installed.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import vector_index
from app.backend.config import get_settings
from app.backend.embeddings import EmbeddingError, client_from_settings


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    force = "--force" in argv
    positional = [a for a in argv if not a.startswith("-")]
    root = Path(positional[0]).resolve() if positional else Path.cwd()

    if not vector_index.lancedb_available():
        print("error: the 'vector' extra is not installed (uv pip install '.[vector]')", file=sys.stderr)
        return 2

    settings = get_settings(root)
    try:
        embedder = client_from_settings(settings)
    except EmbeddingError as exc:
        print(f"error: embedding configuration invalid: {exc}", file=sys.stderr)
        return 2
    if embedder is None:
        print("error: no embedder configured (set EMBEDDING_BASE_URL and EMBEDDING_MODEL_REF)", file=sys.stderr)
        return 2

    try:
        stats = vector_index.reindex(
            root, embedder,
            embedding_model_ref=settings.embedding_model_ref,
            distance_metric=settings.embedding_distance_metric,
            force=force,
        )
    except (vector_index.VectorIndexError, EmbeddingError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    mode = "full rebuild" if stats.full_rebuild else "incremental"
    print(
        f"vector index ({mode}) -> {root / vector_index.VECTOR_RELDIR}\n"
        f"  sources touched: {stats.sources_indexed}, chunks embedded: {stats.chunks_embedded}, "
        f"deleted: {stats.chunks_deleted}, total now: {stats.chunks_total}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
