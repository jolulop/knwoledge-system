# Knowledge System v0.1

Local-first, agent-maintained information management system based on the LLM Wiki pattern.

**Status: Phases 1–7 complete** (the Build Spec's planned scope). The pipeline runs end-to-end —
immutable `raw/` intake → extract/normalize → generated wiki → LLM semantic layer (concepts/entities/
claims/synthesis, grounded) → keyword+vector+graph retrieval → cited `POST /query` → human review UI →
autonomous detect-and-propose maintenance (lint / retention / reindex, no daemon). The API is
**loopback-only with no auth** (ADR-0009). New-session orientation: read `REANCHOR.md`, then
`docs/Operations.md` to run it, `CLAUDE.md` for the rules, `CONTEXT.md` for the glossary.

Implemented surface:

- Repository layout, `CLAUDE.md` / `AGENTS.md`, wiki templates, policy files, evaluation seeds
- Claude Code skills + deterministic hooks/validators (`scripts/validate_*.py`)
- FastAPI backend: intake/extract/wiki jobs, `/search`, `/query`, `/reviews` + `/ui/reviews`,
  `/jobs/{lint,reindex,stale-check}`, graph + sources endpoints

## Target runtime

Recommended deployment:

```text
Windows 11 = human interface
WSL2 Ubuntu = system of record and automation runtime
```

Keep the canonical repo in WSL2:

```bash
cd ~
unzip knowledge-system-scaffold-v0.1.zip
cd knowledge-system-scaffold-v0.1
```

## First local checks

```bash
python3 scripts/rebuild_index.py .
python3 scripts/validate_frontmatter.py .
python3 scripts/validate_wikilinks.py .
python3 scripts/reindex_keyword.py .
python3 scripts/validate_index_consistency.py .
```

Vector search (Phase 4d) is opt-in and refreshed explicitly — install the extra and run the
reindexer deliberately (it needs a local embedding server; it is **not** wired into the per-file
hook):

```bash
uv pip install '.[vector]'
python3 scripts/reindex_vector.py . --force   # needs EMBEDDING_BASE_URL + EMBEDDING_MODEL_REF
python3 scripts/validate_vector_index.py .
```

The scaffold ships with one sample source, concept, claim, and synthesis page so the indexer and validators have something to inspect.

## Suggested next implementation sequence

1. Review and edit `CLAUDE.md` / `AGENTS.md`.
2. Run `scripts/rebuild_index.py .`.
3. Start with one real file in `raw/inbox/`.
4. Implement extraction for PDF, DOCX, HTML, Markdown.
5. Generate Source pages and normalized Markdown.
6. Add keyword search, then vector search.
7. Add review UI and autonomous scheduled jobs.

## Repository principles

- Raw files are source of truth and must not be modified by agents.
- Wiki pages are derived, reviewable, and regenerable.
- Every major wiki page must include a `> [!summary]` callout.
- Claims require citations or must be marked unsourced.
- Deletion, contradiction resolution, entity merging, and deprecation require human approval.
