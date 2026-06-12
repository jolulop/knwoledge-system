# Phase 2 Plan
## Extraction and Normalization

**Status:** Planned
**Depends on:** Phase 1 (File Intake and Raw Repository) complete
**Repository root:** `~/code/knowledge-system`
**Goal:** Turn catalogued raw sources into deterministic, content-keyed normalized
artifacts (Markdown + chunks + tables + extraction logs) with mechanically-derived
citation anchors, and record extraction state on the manifest.

> **Decisions finalized in [ADR-0010](adr/0010-phase-2-extraction-scope-and-boundary.md),
> [ADR-0011](adr/0011-normalized-artifacts-are-content-keyed-and-manifest-tracked.md),
> and [ADR-0012](adr/0012-chunk-schema-and-mechanical-citation-anchors.md).** This plan
> translates those ADRs into concrete schemas, files, thresholds, and acceptance
> criteria. Read the ADRs first; where this plan and an ADR disagree, the ADR wins.

---

## 1. Phase 2 Objective

By the end of Phase 2, for every extractable source already catalogued in
`raw/manifests/`, the system must be able to:

1. Extract embedded text from PDF, DOCX, HTML, and Markdown.
2. Extract tabular data from XLSX and CSV.
3. Normalize each source to deterministic Markdown under `normalized/markdown/`.
4. Split each normalized document into heading-aware chunks with citation anchors.
5. Track per-page spans for PDFs so chunks carry verifiable page numbers.
6. Write a per-source extraction log.
7. Update the manifest with extraction status and the raw→normalized linkage.
8. Record an `extract` job per run.
9. Re-extract idempotently (skip unchanged, overwrite per source on force).
10. Do all of the above deterministically, offline, with no API keys.

Phase 2 does **not** call an LLM, generate wiki pages, or build search/graph indexes.

Manifest JSON files are local runtime state in Phase 2. They are authoritative for
the running workspace, but generated `raw/manifests/*.json` files are not committed
to git. Phase 2 should preserve this by updating manifests in place locally and by
keeping portable references repository-relative in normalized artifacts and API
responses.

---

## 2. Scope

### In Scope

- Native text extraction: PDF (pypdf), DOCX (python-docx), HTML (beautifulsoup4),
  Markdown (passthrough/normalize).
- Tabular extraction: XLSX (openpyxl/pandas), CSV (pandas).
- Deterministic Markdown normalization.
- Heading-aware chunking with citation anchors.
- PDF per-page span tracking → chunk page numbers.
- Per-source extraction logs.
- Manifest extraction fields + `ingestion_status` evolution.
- `extract` job type.
- Idempotent, content-keyed normalized artifacts.
- Untrusted-input safety caps (size, timeout, no network, inert text).
- A normalized-layer validator.
- Unit tests for extraction, chunking, and anchors.

### Out of Scope (deferred)

- OCR for scanned/image-only PDFs and image captioning.
- LLM summaries, tags, entities, candidate claims.
- Wiki page generation (Phase 3).
- Keyword/vector/graph indexing (Phase 4).
- Structured table extraction from PDF/DOCX (kept inline in Markdown; see §8).
- Near-duplicate / semantic duplicate detection.
- Retention reclassification (`retention_class` stays `unknown`).
- Deleting or moving raw files.

---

## 3. Directory Targets

```text
normalized/
├─ markdown/         <source_id>.md            normalized Markdown, one per source
├─ chunks/           <source_id>.jsonl         heading-aware chunks, one line each
├─ tables/           <source_id>/<n>.csv       extracted tables (XLSX/CSV sources)
├─ images/           (unused in Phase 2)
└─ extraction_logs/  <source_id>.json          per-source extraction diagnostics
```

The pre-existing monolithic `normalized/chunks/chunks.jsonl` is a wiki keyword-index
artifact (Phase 4). Phase 2 does not read or write it; it will be regenerated/removed
when Phase 4 reindexing lands. Phase 2 writes only per-`<source_id>` files.

---

## 4. Extended Manifest Schema

Extraction extends the Phase 1 manifest (ADR-0011). New/changed fields:

```json
{
  "ingestion_status": "new|extracted|partial|error",
  "normalized": {
    "markdown_path": "normalized/markdown/<source_id>.md",
    "chunks_path": "normalized/chunks/<source_id>.jsonl",
    "tables_dir": "normalized/tables/<source_id>",
    "extraction_log_path": "normalized/extraction_logs/<source_id>.json"
  },
  "extracted_at": "datetime|null",
  "extraction_tool": "string|null",
  "extraction_tool_version": "string|null",
  "text_char_count": 0,
  "chunk_count": 0,
  "page_count": "int|null"
}
```

Field notes:

- `ingestion_status` transitions `new → extracted | partial | error`. `partial` covers
  recoverable cases (e.g. `needs_ocr`, `truncated` if ever enabled).
- `normalized.*` paths are repository-relative. `tables_dir` is present even when empty.
- Existing absolute `raw_path` values may remain in local manifests for runtime file
  resolution, but portable outputs must use `relative_raw_path` or `occurrences[]`
  repository-relative paths.
- `extracted_at`, `extraction_tool[_version]` are refreshed on each successful run.
- `page_count` is the source page count for paginated formats, else `null`.
- All Phase 1 fields (occurrences, sha256, retention_class, etc.) are preserved
  unchanged. `retention_class` remains `unknown` in Phase 2.

---

## 5. Chunk Schema

One JSON object per line in `normalized/chunks/<source_id>.jsonl` (ADR-0012):

```json
{
  "chunk_id": "<source_id>::0007",
  "source_id": "src_xxxxxxxxxxxxxxxx",
  "ordinal": 7,
  "kind": "prose|table",
  "heading_path": ["Top Heading", "Subsection"],
  "section": "Subsection",
  "text": "string",
  "char_start": 0,
  "char_end": 0,
  "page": "int|null",
  "page_end": "int|null",
  "table_reference": "string|null",
  "sheet_reference": "string|null"
}
```

Rules:

- `chunk_id` is `<source_id>::<zero-padded ordinal>`; ordinals are contiguous from 0.
- `char_start`/`char_end` index into `normalized/markdown/<source_id>.md` and must
  satisfy `0 <= char_start < char_end <= len(markdown)`.
- `page`/`page_end` are set only from tracked per-page spans (PDF); `null` otherwise.
  Estimating pages is forbidden.
- `kind: "table"` chunks set `table_reference` (and `sheet_reference` for XLSX) and
  point at a file in `tables_dir`.
- Anchors must come from `policies/citation.yaml` `accepted_citation_anchors`.

### Chunking strategy

- Heading-aware: start a new chunk at each Markdown heading.
- Size cap: target ~1000 characters, hard max 2000; oversized sections split on
  paragraph boundaries, never mid-sentence.
- Tiny trailing fragments may merge into the previous chunk of the same section.
- Deterministic: identical normalized Markdown yields identical chunks.

---

## 6. Extraction Log Schema

`normalized/extraction_logs/<source_id>.json`:

```json
{
  "source_id": "src_xxxxxxxxxxxxxxxx",
  "status": "extracted|partial|error",
  "tool": "pypdf|python-docx|beautifulsoup4|markdown|pandas",
  "tool_version": "string",
  "started_at": "datetime",
  "finished_at": "datetime",
  "input_size_bytes": 0,
  "page_count": "int|null",
  "text_char_count": 0,
  "chunk_count": 0,
  "table_count": 0,
  "warnings": ["needs_ocr"],
  "error": "string|null",
  "skip_reason": "string|null"
}
```

---

## 7. Normalized Markdown Conventions

Deterministic, dependency-light output:

- Headings preserved as ATX (`#`, `##`, …) using the source's heading levels.
- Paragraphs separated by a single blank line; runs of whitespace collapsed.
- Lists preserved as `-` / `1.`.
- Tables emitted as GitHub-flavored Markdown (see §8).
- No front matter is added in Phase 2 (that is a Phase 3 wiki concern).
- HTML: strip `<script>`, `<style>`, `<nav>`, `<header>`, `<footer>`, comments;
  extract main textual content; **no remote fetch** of any referenced resource.

---

## 8. Table Extraction

- **XLSX/CSV (structured):** each sheet → `normalized/tables/<source_id>/<n>.csv`
  via pandas/openpyxl, plus a `kind:"table"` chunk per sheet with `sheet_reference`.
- **PDF/DOCX (best-effort):** tables are rendered inline in the normalized Markdown as
  GFM tables; structured per-table files are **not** produced in Phase 2 (pypdf and
  python-docx lack reliable table geometry). Structured PDF table extraction is deferred.

---

## 9. Job Schema Additions

Reuse the Phase 1 `jobs` table (no migration). Add to the allowed job types:

```text
extract
```

A per-run `extract` job records, in `metadata_json`, the run summary:
`sources_considered, extracted, partial, errors, skipped_unchanged, skipped_unsupported`.
Per-source status remains on the manifest and in the extraction log.

---

## 10. Safety and Untrusted-Input Limits (ADR-0010)

Defaults (configurable via env, see §13):

- `EXTRACT_MAX_FILE_MB = 50` — larger files are skipped with `status=error`,
  `skip_reason="oversize"`.
- `EXTRACT_TIMEOUT_S = 120` — per-file extraction timeout; on exceed → `error`,
  `skip_reason="timeout"`.
- Decompression-bomb guard for zipped formats (DOCX/XLSX): reject if uncompressed
  size or entry count exceeds sane bounds → `error`, `skip_reason="decompression_bomb"`.
- **No network I/O** during extraction.
- Extracted text is inert: stored and carried forward only as quoted evidence, never
  interpreted as instructions.
- Any single file's failure is logged and the run continues.

`needs_ocr` heuristic: a paginated source whose extracted text averages fewer than ~16
characters per page (or total text below a small floor for non-paginated formats) is
marked `partial` with a `needs_ocr` warning — not an error.

---

## 11. Idempotency and Re-extraction

- A source already `extracted` whose manifest `sha256` is unchanged is **skipped**
  unless `--force` is given.
- Re-extraction overwrites only that source's files under `normalized/`.
- Deterministic input → identical Markdown, chunks, and anchors across runs.

---

## 12. Proposed Files to Implement

```text
app/workers/extract.py             orchestration: scan manifests, dispatch, write outputs
app/workers/extractors/__init__.py
app/workers/extractors/pdf.py      pypdf text + per-page spans
app/workers/extractors/docx.py     python-docx text + tables
app/workers/extractors/html.py     beautifulsoup4 text
app/workers/extractors/markdown.py passthrough/normalize
app/workers/extractors/tables.py   xlsx/csv via pandas/openpyxl
app/workers/chunking.py            heading-aware chunker + anchor assembly
app/backend/manifests.py           shared manifest read/update helpers (extracted from intake)
scripts/extract_sources.py         CLI
scripts/validate_normalized.py     normalized-layer validator
tests/test_extract.py
tests/test_chunking.py
tests/test_citation_anchors.py
```

Update existing files: `app/backend/models.py` (Source extraction fields + Chunk
model), `app/backend/main.py` (new endpoints), `app/backend/db.py` (allow `extract`
job type), `pyproject.toml` (move `[extraction]` deps into runtime or document
`uv sync --extra extraction`).

---

## 13. Configuration

`.env` additions (with defaults applied when absent):

```env
EXTRACT_MAX_FILE_MB=50
EXTRACT_TIMEOUT_S=120
EXTRACT_CHUNK_TARGET_CHARS=1000
EXTRACT_CHUNK_MAX_CHARS=2000
```

Extraction requires the optional dependency group: `uv sync --extra extraction`.

---

## 14. API Endpoints for Phase 2

Additions to the Phase 1 surface:

```text
POST /jobs/extract            run extraction over pending (or all with ?force=true) sources
GET  /sources/{id}/chunks     list chunks for a source (from its chunks.jsonl)
GET  /sources/{id}/normalized return normalized Markdown for a source
```

`GET /sources` / `GET /sources/{id}` now also surface the extraction fields (§4).
Responses remain schema-enforced and never expose absolute paths (ADR-0009).

---

## 15. CLI Workflow

```bash
uv run python scripts/extract_sources.py            # extract all pending sources
uv run python scripts/extract_sources.py --force    # re-extract everything
uv run python scripts/extract_sources.py <source_id># extract one source
```

Expected summary output:

```text
Extraction complete.
Sources considered: 6
Extracted: 5
Partial (needs_ocr): 1
Errors: 0
Skipped (unchanged): 0
Skipped (unsupported): 0
Job: job_xxxxxxxxxxxxxxxx
```

---

## 16. Testing Plan

### Unit / integration

- **Extraction:** small fixture files per format (md, html, a tiny generated PDF, a
  docx, a csv/xlsx) produce expected normalized Markdown and manifest status.
- **Chunking:** heading-aware boundaries; size cap; no mid-sentence split;
  contiguous ordinals; deterministic output across two runs.
- **Citation anchors:** `char_start`/`char_end` resolve to the chunk's text in the
  normalized Markdown; PDF chunks carry correct `page` from tracked spans;
  non-paginated formats have `page=null`; no estimated anchors.
- **Idempotency:** re-extraction of an unchanged source is skipped; `--force`
  overwrites and preserves `discovered_at`/`extracted_at` semantics.
- **Safety:** oversize file → `error`/`oversize`; malformed file → `error`, run
  continues; zero-text PDF → `partial`/`needs_ocr`.
- **Manifest:** extraction fields populated; `retention_class` unchanged; Phase 1
  fields preserved.

### Validators

```bash
uv run python scripts/validate_normalized.py .   # new: manifest<->normalized consistency,
                                                 # anchor bounds, chunk_id uniqueness
uv run python scripts/validate_frontmatter.py .
uv run python scripts/validate_wikilinks.py .
uv run python scripts/validate_citations.py .
```

All existing validators must continue to pass.

---

## 17. Acceptance Criteria

Phase 2 is complete when:

- [ ] PDF/DOCX/HTML/Markdown text extraction works on fixtures and the demo set.
- [ ] XLSX/CSV tables extract to `normalized/tables/<source_id>/`.
- [ ] Normalized Markdown is written per source and is deterministic.
- [ ] Chunks are heading-aware, content-keyed, and within size caps.
- [ ] Citation anchors are mechanically derived; PDF page numbers come from tracked
      spans; no estimated anchors exist.
- [ ] Extraction logs are written per source.
- [ ] Manifest extraction fields and `ingestion_status` are updated correctly.
- [ ] `extract` jobs are recorded.
- [ ] Re-extraction is idempotent; `--force` re-runs.
- [ ] Oversize/malformed/zero-text inputs are handled per ADR-0010 without crashing.
- [ ] No network I/O occurs during extraction.
- [ ] `retention_class` remains `unknown`; raw files are not modified.
- [ ] New `scripts/validate_normalized.py` passes; existing validators still pass.
- [ ] API exposes extraction state and the new endpoints; no absolute paths leak.
- [ ] A Git commit captures Phase 2 completion.

---

## 18. Phase 2 Non-Goals

Do not implement yet: OCR, image captioning, LLM summaries/tags/entities/claims, wiki
page generation, keyword/vector/graph indexing, structured PDF table extraction,
near-duplicate detection, retention reclassification, background scheduler.

---

## 19. Phase 2 Completion Commit

```bash
git add .
git commit -m "Implement Phase 2 extraction and normalization"
```
