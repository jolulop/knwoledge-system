# Normalized artifacts are content-keyed and manifest-tracked

Phase 2 writes one set of normalized files per unique source, named by its Source ID,
extending the content-keyed model of ADR-0007/0008 from manifests to the normalized
layer: `normalized/markdown/<source_id>.md`, `normalized/chunks/<source_id>.jsonl`,
`normalized/tables/<source_id>/*.csv`, and `normalized/extraction_logs/<source_id>.json`.
Re-extracting a source overwrites only that source's files, so extraction is
idempotent and the on-disk normalized layer — not a database — remains authoritative.
The pre-existing monolithic `normalized/chunks/chunks.jsonl` is a wiki keyword-index
artifact owned by Phase 4 reindexing, not part of the Phase 2 per-source chunk store.

Extraction state is recorded on the manifest, which stays the single authoritative
per-source local runtime record that `/sources` reads (ADR-0008). Generated manifest
JSON is not committed to git, even though it is authoritative for the current
workspace. The manifest's `ingestion_status` evolves `new → extracted | partial |
error`, and the manifest gains `normalized_path`, `extracted_at`, `extraction_tool`
(with version), and `text_char_count`. Detailed per-run diagnostics live in the
extraction log, not the manifest. A new `extract` job type tracks each run.
Re-extraction is skipped for a source that is already `extracted` and whose content
is unchanged, unless explicitly forced.

Consequences: a source's complete state — discovery, checksum, occurrences, and
extraction outcome — is readable from one manifest file, and the normalized outputs
are trivially regenerable and diffable per source. Evolving the Phase 1 manifest is a
schema change, but an additive and backward-compatible one: Phase 1 manifests simply
carry `ingestion_status: new` and lack the extraction fields until first extracted.
Validators and the `Source` API model must be updated to accept the new fields.
