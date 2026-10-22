# Knowledge System

Local-first, agent-maintained information management system based on the LLM Wiki pattern.

**Status as of 2026-09-08: Phases 1-7 complete** (the Build Spec's planned scope), with
post-Phase-7 hardening through ADR-0063 implemented. The pipeline runs end-to-end:
immutable `raw/` intake → extract/normalize → generated wiki → LLM semantic layer (knowledge items/
claims/synthesis, grounded; ADR-0059 taxonomy) → keyword+vector+graph retrieval → cited `POST /query` → human review UI →
autonomous detect-and-propose maintenance (lint / retention / reindex, no daemon). The API is
**loopback-only with no auth** (ADR-0009). New-session orientation: read `REANCHOR.md`, then
`docs/Operations.md` to run it, `CLAUDE.md` for the rules, `CONTEXT.md` for the glossary.

Implemented surface:

- Repository layout, `CLAUDE.md` / `AGENTS.md`, wiki templates, policy files, evaluation seeds
- Claude Code skills + deterministic hooks/validators (`scripts/validate_*.py`)
- FastAPI backend: intake/extract/wiki jobs, `/search`, `/query`, `/reviews` + `/ui/reviews`,
  `/jobs/{intake-scan,extract,generate-wiki,lint,reindex,stale-check}`, graph, raw-source,
  source, and answer-eval endpoints
- Server-rendered review UI, including queue-wide dry-run apply, per-source review, human-added
  candidates, and source-original viewing
- ADR-0063 model chains for hosted/local LLM routing, plus ADR-0053 vector embeddings via
  BGE-M3/FlagEmbedding or the `local_http` fallback

## Target runtime

Recommended deployment:

```text
Windows 11 = human interface
WSL2 Ubuntu = system of record and automation runtime
```

Keep the canonical repo in WSL2:

```bash
cd ~/code/knowledge-system
uv sync --all-extras
```

## First local checks

```bash
uv run python scripts/rebuild_index.py .
uv run python scripts/validate_all.py
```

Vector search (Phase 4d) is opt-in and refreshed explicitly — install the `vector` extra + the GPU
embedding stack and run the reindexer deliberately (it is **not** wired into the per-file hook).
The recommended GPU configuration is **in-process FlagEmbedding + PyTorch CUDA** (BAAI/bge-m3,
`EMBEDDING_PROVIDER=flagembedding_bge_m3`, ADR-0053); the code default remains `local_http`
when no `.env` selects a provider, so lightweight ingest/review/test workflows stay Torch-free.
GPU stack setup + smoke: *docs/Environment Setup v0.1.md* §14.1.

```bash
uv sync --all-extras                          # includes LanceDB; GPU stack is a separate overlay
uv run python scripts/check_embedding.py       # validate torch/CUDA + BGE-M3 -> dense_vecs shape: (3, 1024)
uv run python scripts/reindex_vector.py . --force
uv run python scripts/validate_vector_index.py .
```

The repository no longer relies on committed sample wiki pages. A fresh checkout has tracked
directory skeletons only; generated vault state is local runtime data.

## Day-to-day workflow

```bash
# deterministic ingest/build
uv run python scripts/scan_inbox.py
uv run python scripts/extract_sources.py
uv run python scripts/generate_wiki.py
uv run python scripts/reindex_keyword.py
uv run python scripts/rebuild_index.py
uv run python scripts/validate_all.py

# LLM-backed semantic layer, if a hosted key or local model chain is configured
uv run python scripts/enrich.py
uv run python scripts/extract_claims.py
uv run python scripts/extract_items.py
uv run python scripts/promote.py
uv run python scripts/detect_contradictions.py
uv run python scripts/generate_synthesis.py
# the semantic producers rebuild wiki/index.md but NOT the keyword index — refresh it so new
# Item/Claim/Synthesis pages reach /search navigation, then re-validate
uv run python scripts/reindex_keyword.py
uv run python scripts/rebuild_index.py
uv run python scripts/validate_all.py

# serve loopback UI/API
uv run python -m app.backend
# decide review items at /ui/reviews, then apply. Apply refreshes wiki/index.md + the keyword index
# itself when it changed anything; re-run reindex_keyword.py only if it warns keyword_reindex_failed
uv run python scripts/validate_all.py
```

## Repository principles

- Raw files are source of truth and must not be modified by agents.
- Wiki pages are derived, reviewable, and regenerable.
- Every major wiki page must include a `> [!summary]` callout.
- Claims require citations or must be marked unsourced.
- Deletion, contradiction resolution, item merging, and deprecation require human approval.
