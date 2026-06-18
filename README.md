# Knowledge System Scaffold v0.1

Local-first, agent-maintained information management system based on the LLM Wiki pattern.

This scaffold implements the first build deliverable:

- Repository layout
- `CLAUDE.md` and `AGENTS.md`
- Wiki templates
- Policy files
- Evaluation seeds
- Claude Code skill stubs
- Deterministic hooks
- Functional index rebuild and validation scripts
- Minimal FastAPI backend skeleton

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
