# Phase 1 Plan
## File Intake and Raw Repository

**Status:** Planned  
**Depends on:** Environment Setup v0.1 complete  
**Repository root:** `~/code/knowledge-system`  
**Goal:** Turn the scaffold into a reliable raw file intake system with manifests, deduplication, and job tracking.

---

## 1. Phase 1 Objective

Phase 1 builds the foundation for all later ingestion work.

By the end of Phase 1, the system must be able to:

1. Detect files placed in `raw/inbox/`.
2. Create source manifests in `raw/manifests/`.
3. Compute SHA256 checksums.
4. Detect exact duplicates.
5. Register ingestion jobs in a local jobs database.
6. Expose basic API endpoints for sources and jobs.
7. Provide a small command-line workflow for testing.
8. Avoid modifying raw source files.

Phase 1 does not need full extraction, LLM summarization, vector search, or graph building.

---

## 2. Scope

### In Scope

- Raw file discovery.
- Manifest creation.
- SHA256 checksum calculation.
- File metadata capture.
- Exact duplicate detection.
- Job database.
- Basic FastAPI health endpoint.
- Basic source listing endpoint.
- Basic CLI script for intake scan.
- Unit tests for manifest and checksum behavior.

### Out of Scope

- PDF/DOCX/HTML extraction.
- LLM summarization.
- Concept extraction.
- Vector search.
- Graph traversal.
- Review UI.
- Mobile UI.
- Scheduled autonomous jobs.
- Deleting raw files.

---

## 3. Directory Targets

Phase 1 uses these directories:

```text
raw/
├─ inbox/        new files waiting for intake
├─ permanent/    accepted source files, optional later
├─ ephemeral/    low-value temporary captures, optional later
├─ assets/       not used in Phase 1
├─ transcripts/  transcript inputs, treated as raw files
└─ manifests/    JSON/YAML manifests

db/
├─ metadata.sqlite
└─ jobs.sqlite
```

For Phase 1, files may remain in `raw/inbox/`. Moving them to `raw/permanent/` can be deferred until Phase 2 unless it is simple and safe.

---

## 4. Manifest Schema

Create one manifest per **unique content** (not per file).

> **Finalized in [ADR-0007](adr/0007-content-keyed-manifests-with-occurrences.md).** The schema below supersedes the original draft: `duplicate_of` is removed (a content-keyed manifest would only self-reference), every observed file path is recorded under `occurrences[]`, and inbox files default to `retention_class: unknown`.

Recommended path:

```text
raw/manifests/<source_id>.json
```

Manifest fields:

```json
{
  "source_id": "string",
  "original_filename": "string",
  "raw_path": "string",
  "relative_raw_path": "string",
  "sha256": "string",
  "size_bytes": 0,
  "file_extension": "string",
  "detected_mime_type": "string|null",
  "created_at": "datetime",
  "modified_at": "datetime",
  "discovered_at": "datetime",
  "last_seen_at": "datetime",
  "last_scanned_at": "datetime",
  "ingestion_status": "new|queued|error",
  "retention_class": "permanent|ephemeral|unknown",
  "occurrences": [
    {
      "relative_path": "string",
      "filename": "string",
      "size_bytes": 0,
      "modified_at": "datetime",
      "first_seen_at": "datetime",
      "last_seen_at": "datetime"
    }
  ],
  "notes": []
}
```

Field notes:

- `source_id`, `created_at`, and `discovered_at` are set once at first intake and preserved across rescans.
- `last_seen_at` / `last_scanned_at` are refreshed on every scan run.
- `original_filename`, `raw_path`, and `relative_raw_path` describe the canonical (first-seen) file; all paths, including byte-identical copies, also appear in `occurrences[]`.
- `ingestion_status` is normally `new` at the manifest level; the "duplicate" disposition belongs to a scan occurrence and the run summary, not to a manifest (see [ADR-0007](adr/0007-content-keyed-manifests-with-occurrences.md)).
- `retention_class` defaults to `unknown` for files arriving in `raw/inbox/`.
- `notes` carries per-source warnings such as `empty_file`.

---

## 5. Job Schema

Create a simple SQLite jobs table.

Fields:

```text
job_id TEXT PRIMARY KEY
job_type TEXT
status TEXT
source_id TEXT
input_path TEXT
output_path TEXT NULL
created_at TEXT
started_at TEXT NULL
finished_at TEXT NULL
error_message TEXT NULL
warnings_json TEXT
metadata_json TEXT
```

Allowed job statuses:

```text
pending
running
succeeded
failed
partial
skipped
```

Allowed job types for Phase 1:

```text
intake_scan
manifest_create
duplicate_check
```

---

## 6. Source ID Strategy

Use deterministic source IDs where possible.

Recommended:

```text
src_<first_16_chars_of_sha256>
```

Example:

```text
src_a1b2c3d4e5f67890
```

This makes repeated scans idempotent.

---

## 7. Duplicate Detection

Phase 1 only handles exact duplicates.

Rule:

```text
If two files have the same SHA256 hash, they are exact duplicates.
```

Behavior (content-keyed model, see [ADR-0007](adr/0007-content-keyed-manifests-with-occurrences.md)):

- Do not delete either file.
- Do not create a second manifest for the duplicate.
- Record the duplicate file's path as an additional entry in the existing manifest's `occurrences[]`.
- Count the redundant copy in the scan run summary (`duplicates = files_found - unique_contents`).
- Exact (SHA256) duplicates do not require human review.

The per-run `intake_scan` job records the duplicate counts in its summary; Phase 1 does not create one job per file (see Section 5 and decision log).

Near-duplicate and semantic duplicate detection are deferred.

---

## 8. Proposed Files to Implement

```text
app/backend/main.py
app/backend/config.py
app/backend/db.py
app/backend/models.py
app/workers/intake.py
scripts/scan_inbox.py
tests/test_manifest.py
tests/test_intake.py
```

If tests are not already configured, add:

```text
tests/
```

and update `pyproject.toml` with test dependencies later.

---

## 9. API Endpoints for Phase 1

Minimum API endpoints:

```text
GET  /health
GET  /sources
GET  /sources/{source_id}
GET  /jobs
GET  /jobs/{job_id}
POST /jobs/intake-scan
```

### 9.1 GET /health

Response:

```json
{
  "status": "ok",
  "app": "knowledge-system",
  "version": "0.1.0"
}
```

### 9.2 GET /sources

Returns manifests from `raw/manifests/` or metadata DB.

### 9.3 POST /jobs/intake-scan

Runs or queues an intake scan over `raw/inbox/`.

For Phase 1, synchronous execution is acceptable.

---

## 10. CLI Workflow

Add:

```bash
uv run python scripts/scan_inbox.py
```

Expected behavior:

1. Scan `raw/inbox/` recursively.
2. Ignore directories and hidden temp files.
3. Compute checksums.
4. Write manifests.
5. Update jobs database.
6. Print summary.

Example output:

```text
Inbox scan complete.
Files found: 3
New manifests: 2
Duplicates: 1
Errors: 0
```

---

## 11. Configuration

Use `.env` values:

```env
KNOWLEDGE_SYSTEM_HOME=/home/jolulop/code/knowledge-system
APP_HOST=127.0.0.1
APP_PORT=18000
```

If `KNOWLEDGE_SYSTEM_HOME` is missing, default to current working directory.

---

## 12. Testing Plan

### 12.1 Manual Test

Create two test files:

```bash
mkdir -p raw/inbox

echo "test document one" > raw/inbox/test-one.md
cp raw/inbox/test-one.md raw/inbox/test-one-copy.md
```

Run:

```bash
uv run python scripts/scan_inbox.py
```

Expected:

- Two files found.
- One source ID for the original hash.
- One duplicate detected.
- Manifests created.
- No raw files modified.

### 12.2 Validation Commands

After intake scan:

```bash
uv run python scripts/rebuild_index.py .
uv run python scripts/validate_frontmatter.py .
uv run python scripts/validate_wikilinks.py .
uv run python scripts/validate_citations.py .
```

These existing validators should continue to pass.

---

## 13. Acceptance Criteria

Phase 1 is complete when:

- [ ] `raw/inbox/` scan works.
- [ ] SHA256 checksums are computed.
- [ ] Manifests are created in `raw/manifests/`.
- [ ] Exact duplicates are detected.
- [ ] Duplicate files are not deleted.
- [ ] Jobs database is created.
- [ ] Intake jobs are recorded.
- [ ] `/health` endpoint works on port `18000`.
- [ ] `/sources` endpoint lists known sources.
- [ ] `/jobs` endpoint lists jobs.
- [ ] CLI scan command works.
- [ ] Existing scaffold validators still pass.
- [ ] A Git commit captures Phase 1 completion.

---

## 14. Suggested Claude Code Prompt for Phase 1

Use this from the repository root:

```text
Read docs/Build Specification v0.1.md, docs/Architecture Overview v0.1.md, docs/Environment Setup v0.1.md, and docs/Phase 1 Plan.md.

Implement Phase 1: File Intake and Raw Repository.

Requirements:
- Keep repository root as /home/jolulop/code/knowledge-system.
- Use Python 3.12 via uv.
- Do not require API keys.
- Use APP_PORT=18000.
- Implement raw/inbox scanning.
- Create raw/manifests/<source_id>.json.
- Compute SHA256.
- Detect exact duplicates.
- Create a simple jobs SQLite database.
- Add a CLI script scripts/scan_inbox.py.
- Add or update FastAPI endpoints: /health, /sources, /sources/{source_id}, /jobs, /jobs/{job_id}, /jobs/intake-scan.
- Do not modify or delete raw files.
- Add tests where practical.
- Run existing validators after changes.
- Show a concise implementation summary and commands to test.
```

---

## 15. Phase 1 Non-Goals

Do not implement yet:

- Full document extraction.
- LLM summaries.
- Claim extraction.
- Concept pages from real documents.
- Vector embeddings.
- Graph visualization.
- Human review UI.
- Retention automation.
- Background scheduler.
- Public network exposure.

---

## 16. Phase 1 Completion Commit

Recommended commit message:

```bash
git add .
git commit -m "Implement Phase 1 file intake and raw repository"
```
