# Phase 3 Plan
## Filing-Cabinet Wiki Layer — Deterministic Source-Page Backbone

**Status:** Implemented (commits 8579c8a, f999554, 3d83e3f; refinements per ADR-0023/0024)
**Depends on:** Phase 2 (Extraction and Normalization) complete
**Repository root:** `~/code/knowledge-system`
**Goal:** Generate the wiki Source-page layer and its supporting machinery
(index rebuild, log append, validators) deterministically from the Phase 2 normalized
layer and the manifests — offline, no LLM, no API keys.

> **Decisions finalized in [ADR-0013](adr/0013-phase-3-deterministic-wiki-backbone.md)
> through [ADR-0022](adr/0022-wiki-page-frontmatter-lifecycle-contract.md).** This plan
> translates those ADRs into concrete schemas, files, and acceptance criteria. Read the
> ADRs first; where this plan and an ADR disagree, the ADR wins.

---

## 1. Phase 3 Objective

For every source already catalogued and extracted (manifest `ingestion_status` of
`extracted` or `partial`), the system must:

1. Render a deterministic Source page at `wiki/Sources/<source_id>.md` from
   `templates/source.md`, the manifest, and the normalized layer.
2. Fill only mechanically-derived fields; render LLM-only sections as
   `_Pending semantic enrichment_` placeholders (no wikilinks) — ADR-0016.
3. Write a labelled extractive summary stub into the `> [!summary]` callout — ADR-0016.
4. Carry the full lifecycle frontmatter contract, with page `status` separate from the
   manifest-mirrored `ingestion_status` — ADR-0022.
5. Rebuild `wiki/index.md` (reusing `scripts/rebuild_index.py`).
6. Append a generation entry to `wiki/log.md`.
7. Record a `generate_wiki` job per run.
8. Regenerate idempotently (byte-stable output; skip unchanged, overwrite on `--force`).
9. Do all of the above deterministically, offline, with no API keys.

Phase 3 does **not** call an LLM and does not create concept/entity/claim/synthesis
pages, backlinks, tags, or search/graph indexes.

The wiki layer is mutable local runtime data, not code: it is gitignored and made
durable by the backup mechanism, not git (ADR-0014).

---

## 2. Scope

### In Scope
- Deterministic Source-page generation for `extracted` and `partial` sources.
- Extractive summary stub with structural fallback for sparse text.
- Full lifecycle frontmatter (ADR-0022), `relative_raw_path` only (ADR-0009/0016).
- `index.md` rebuild (reuse `scripts/rebuild_index.py`) and `log.md` append.
- `generate_wiki` job type; idempotent regeneration with `--force`.
- Untracking the `wiki/` layer from git and gitignoring it (ADR-0014).
- A wiki-layer validator; unit/integration tests.
- API endpoints for listing/reading wiki pages and triggering generation.

### Out of Scope (deferred)
- Any LLM use: real summaries, tags, key points, concepts, entities, people,
  organizations, projects, claims, synthesis (Phase 3.5+).
- Bidirectional semantic backlinks (Phase 3.5; ADR-0016 invariant: none yet).
- Keyword/vector/graph indexing (Phase 4).
- Query/cited answering (Phase 5); the `query.md` contract is defined, not exercised.
- Concept promotion, entity merge/split, contradiction handling (Phase 3.5+/review).
- Retention reclassification (`status` stays `active`; `retention_class` untouched).

---

## 3. Directory Targets

```text
wiki/
├─ Sources/        <source_id>.md      one deterministic Source page per extracted source
├─ index.md        regenerated navigation index (reuses scripts/rebuild_index.py)
└─ log.md          append-only generation/maintenance history
```

`wiki/Concepts`, `Claims`, `Entities`, `People`, `Organizations`, `Projects`, `Tags`,
`Synthesis`, `Queries` remain empty `.gitkeep`-only scaffolding in Phase 3.

---

## 4. Which Sources Get a Page

- `ingestion_status: extracted` → full Source page.
- `ingestion_status: partial` (e.g. `needs_ocr`) → Source page; the extractive summary
  falls back to the structural line when normalized text is too sparse (§6).
- `ingestion_status: error` or `new` → **no page** until the source is (re-)extracted.
- Unsupported sources (no extraction) → no page.

Coverage target: every `extracted`/`partial` source has exactly one Source page
(Build Spec §16: ≥95% of parsed sources receive Source pages).

---

## 5. Source Page Schema

Rendered from `templates/source.md`. Frontmatter (ADR-0015/0016/0022):

```yaml
type: source
source_id: src_xxxxxxxxxxxxxxxx
title: "<derived title>"
aliases: ["<title>"]
relative_raw_path: raw/inbox/.../file.pdf      # repository-relative ONLY (ADR-0009)
normalized_path: normalized/markdown/<source_id>.md
sha256: <hex>
file_type: .pdf
language: <iso code or "unknown">              # deterministic if known, else "unknown"
page_count: <int|null>
chunk_count: <int>
status: active                                 # wiki lifecycle (ADR-0022)
ingestion_status: extracted|partial            # read-only mirror of the manifest
summary_status: stub                           # stub | enriched (ADR-0016)
generation_status: deterministic               # ADR-0022
created: <iso>
ingested: <iso>
last_compiled_at: <iso>
tags: []                                       # empty until Phase 3.5
concepts: []
entities: []
people: []
organizations: []
projects: []
```

Body: labelled summary callout (§6) · Source Details (deterministic facts) ·
Key Points / Claims / Concepts Mentioned / Entities Mentioned, each rendered as
`_Pending semantic enrichment._` with **no wikilinks** (ADR-0016) · Notes.

Title derivation (deterministic, in order): manifest `original_filename` without
extension, normalized to a readable title; never an LLM guess.

---

## 6. Summary Stub Rules (ADR-0016)

The mandatory `> [!summary]` callout is filled without an LLM and labelled so it is
never mistaken for a vetted summary:

```markdown
> [!summary] Extractive excerpt (auto-generated, unverified)
> <first meaningful paragraph of the normalized Markdown, ~1–2 sentences>
```

- **Extractive path:** take the first prose paragraph of
  `normalized/markdown/<source_id>.md` (skip headings/tables), collapse whitespace, and
  truncate to `WIKI_SUMMARY_MAX_CHARS` on a sentence boundary where possible.
- **Structural fallback** (when extractable text < `WIKI_SUMMARY_MIN_CHARS`, e.g.
  `partial`/`needs_ocr`): `Source: <title>. <page_count> pages, <chunk_count> chunks.`
- Frontmatter `summary_status: stub`. The Phase 3.5 LLM phase replaces the text and sets
  `summary_status: enriched`; the linter treats `stub` as expected, not summary rot.
- Extracted text is displayed data only, never instructions (untrusted-input contract).

---

## 7. Git Untracking (ADR-0014)

Implementation step (one-time): stop tracking the wiki layer and gitignore it.

```bash
git rm --cached wiki/index.md wiki/log.md
```

Add to `.gitignore` (keeping the `.gitkeep` scaffolding):

```gitignore
wiki/index.md
wiki/log.md
wiki/Sources/*.md
wiki/Concepts/*.md
wiki/Claims/*.md
wiki/Entities/*.md
wiki/People/*.md
wiki/Organizations/*.md
wiki/Projects/*.md
wiki/Tags/*.md
wiki/Synthesis/*.md
wiki/Queries/*.md
```

Durability of `log.md` (not regenerable) now depends on the backup mechanism — a Phase 3
dependency to confirm `scripts/backup.py` covers `wiki/`.

---

## 8. Job Schema Additions

Reuse the Phase 1 `jobs` table. Add to allowed job types (`app/backend/db.py`):

```text
generate_wiki
```

A per-run `generate_wiki` job records in `metadata_json`:
`sources_considered, generated, skipped_unchanged, skipped_not_extracted, errors`.

---

## 9. Idempotency and Regeneration

- Source pages are machine-owned, deterministic projections: identical inputs yield
  byte-identical pages (clean diffs, idempotent overwrite).
- A source whose page exists and whose manifest `sha256` + `ingestion_status` are
  unchanged is **skipped**; `--force` rewrites every page.
- Re-generation overwrites only that source's page (human edits to Source pages are not
  preserved — Source pages are pure projections; ADR-0014).

---

## 10. Configuration

`.env` additions (defaults applied when absent):

```env
WIKI_SUMMARY_MAX_CHARS=320
WIKI_SUMMARY_MIN_CHARS=40
```

No new runtime dependencies (the renderer is dependency-free token substitution).

---

## 11. Proposed Files to Implement

```text
app/workers/wiki.py            orchestration: scan manifests, render pages, index, log, job
app/workers/wiki_render.py     deterministic template token substitution + summary stub
scripts/generate_wiki.py       CLI
scripts/validate_wiki.py       wiki-layer validator (Phase 3 contract)
tests/test_wiki.py             end-to-end generation, idempotency/force, coverage
tests/test_wiki_render.py      template fill, summary extractive + structural fallback
tests/test_validate_wiki.py    validator pass/fail cases
```

Update existing files: `app/backend/db.py` (`generate_wiki` job type),
`app/backend/models.py` (`WikiPage`, `WikiPagesResponse`), `app/backend/main.py`
(endpoints), `app/backend/config.py` (wiki settings), `.gitignore` (untrack wiki).
Reuse `scripts/rebuild_index.py` unchanged for `index.md`.

---

## 12. API Endpoints for Phase 3

Additions (Build Spec §15):

```text
POST /jobs/generate-wiki        generate Source pages for pending (or all with ?force=true) sources
GET  /wiki/pages                list Source pages (source_id, title, status, summary)
GET  /wiki/pages/{source_id}    return a Source page (frontmatter + content)
```

`GET /wiki/index` already exists. Responses are schema-enforced and never expose
absolute paths (ADR-0009).

---

## 13. CLI Workflow

```bash
uv run python scripts/generate_wiki.py             # generate pages for pending sources
uv run python scripts/generate_wiki.py --force     # regenerate all
uv run python scripts/generate_wiki.py <source_id> # generate one source
uv run python scripts/rebuild_index.py .           # rebuild wiki/index.md
```

Expected summary output:

```text
Wiki generation complete.
Sources considered: 9
Generated: 9
Skipped (unchanged): 0
Skipped (not extracted): 0
Errors: 0
Job: job_xxxxxxxxxxxxxxxx
```

---

## 14. Validator: `scripts/validate_wiki.py`

Auto-discovered by `scripts/validate_all.py`. Checks (Phase 3 contract):

- **Frontmatter contract:** every `wiki/Sources/*.md` has required fields (`type`,
  `source_id`, `title`, `relative_raw_path`, `normalized_path`, `sha256`, `status`,
  `ingestion_status`, `summary_status`, `generation_status`).
- **Identity:** filename `<source_id>.md` equals frontmatter `source_id` (ADR-0015).
- **Manifest consistency:** a manifest exists for that `source_id` with
  `ingestion_status ∈ {extracted, partial}`; `relative_raw_path`/`sha256` match it.
- **No orphans/stale:** no Source page exists for a missing or non-extracted manifest.
- **Coverage:** every `extracted`/`partial` manifest has a Source page.
- **Summary contract:** a `> [!summary]` callout is present and, when
  `summary_status: stub`, carries the extractive-excerpt label (ADR-0016).
- **No absolute paths / no dangling wikilinks:** page leaks no absolute path; any
  wikilink resolves (placeholders contain none — ADR-0016).

Existing validators (`validate_frontmatter`, `validate_wikilinks`, `validate_citations`,
`validate_normalized`, `validate_index_consistency`) must continue to pass.

---

## 15. Testing Plan

- **Rendering:** template fills deterministically; extractive summary from a known
  normalized doc; structural fallback for an empty/`needs_ocr` source; labelled callout;
  placeholders contain no wikilinks; no absolute paths.
- **Generation (end-to-end):** intake → extract → generate; pages created for
  `extracted`/`partial`, none for `error`/`new`; counts correct; `generate_wiki` job
  recorded; `log.md` appended.
- **Idempotency:** second run skips unchanged; `--force` rewrites; byte-stable output
  across two runs.
- **Index:** `rebuild_index.py` lists the new Source pages with their summaries.
- **Validator:** pass on a clean tree; fail on missing frontmatter, id/filename
  mismatch, orphan/stale page, absolute path, missing summary label.

---

## 16. Acceptance Criteria

Phase 3 is complete when:

- [ ] A deterministic Source page is generated for every `extracted`/`partial` source.
- [ ] Pages use `relative_raw_path` only; no absolute path leaks anywhere.
- [ ] Summary callout is a labelled extractive stub (or structural fallback), with
      `summary_status: stub`.
- [ ] LLM-only sections render as placeholders with no wikilinks; lifecycle frontmatter
      present; page `status` distinct from mirrored `ingestion_status`.
- [ ] `index.md` rebuilds deterministically and lists the Source pages.
- [ ] `log.md` records each generation run.
- [ ] `generate_wiki` jobs are recorded; regeneration is idempotent; `--force` rewrites.
- [ ] The `wiki/` layer is untracked and gitignored; backups cover it.
- [ ] `scripts/validate_wiki.py` passes; all existing validators still pass.
- [ ] API exposes wiki pages and the generation endpoint; no absolute paths leak.
- [ ] Output is byte-stable for byte-stable input; no LLM/API calls occur.
- [ ] A Git commit captures Phase 3 completion.

---

## 17. Phase 3 Non-Goals

Do not implement yet: any LLM summary/tags/key-points/concepts/entities/claims/
synthesis, semantic backlinks, keyword/vector/graph indexing, query/cited answering,
concept promotion or entity merge/split, retention reclassification, background
scheduler.

---

## 18. Phase 3 Completion Commit

```bash
git add .
git commit -m "Implement Phase 3 deterministic wiki Source-page backbone"
```
