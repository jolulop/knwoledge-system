# Manifest files are the authoritative source listing in Phase 1

In Phase 1, the JSON manifests under `raw/manifests/` are the single source of truth for the `/sources` and `/sources/{source_id}` endpoints and for the intake CLI. There is no separate `sources` table. The existing `db/metadata.sqlite` remains dedicated to the FTS5 keyword index (`documents`, `documents_fts`), and ingestion job state is persisted separately in `db/jobs.sqlite`.

Consequences: source listing reads and parses manifest files directly, which keeps the on-disk repository authoritative and avoids a synchronization problem between manifests and a database mirror. A database-backed `sources` table (as a rebuildable read cache) may be introduced in a later phase if listing performance over the expected ~600+ manifests requires it. Because manifests remain authoritative, that future change would be additive and low-risk.
