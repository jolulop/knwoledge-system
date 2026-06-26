# ADR-0039 — Backup/restore durability contract: raw opt-in, guarded restore, checksum-verified

**Status:** Accepted. Design-locked **and implemented** 2026-06-26 (this slice). `scripts/backup.py`
gained `BACKUP_INCLUDE_RAW` (manifest-driven opt-in raw inclusion via `_catalogued_raw_index` +
sha256 verification + embedded `BACKUP_MANIFEST.json` sidecar), the `restore_backup` guarded-in-place
path (`--restore` / `--force` / `--dry-run`, zip-slip guard, PARTIAL-labelled report, post-restore raw
re-verification), collision-safe archive naming, `.agents/` in the durable set, and a
`BackupReport`/`RestoreReport` with the domain/flag summary. Covered by `tests/test_backup.py`.
**Extends:** ADR-0024 (raw integrity / sha256 manifests), ADR-0009/0026 (manifests are untrusted on-disk
data; raw-root containment), ADR-0027 (response cache under `db/`), ADR-0029/0030 (graph in
`db/graph.sqlite` is the source of truth for edges). Read `scripts/backup.py`, `policies/retention.yaml`,
`app/backend/paths.py` (`safe_under`).
**Scope boundary:** durability only. This ADR does **not** change retention status and does **not** move
raw bytes to physical archive — that governance stays separate from backup/restore per ADR-0036 decision 4
(retention transitions status, never bytes). Backup/restore is a copy-out/copy-in durability mechanism for
gitignored runtime state; it is orthogonal to the stale→archive two-gate policy.

## Context

`scripts/backup.py` writes a timestamped ZIP of durable runtime state under `backups/`, but three gaps
remain from the Build Spec's backup requirement (line 60): (1) raw bytes are excluded with a docstring TODO
("set `include_raw` later once a storage policy is decided"); (2) there is **no restore path** at all; and
(3) integrity rests entirely on ZIP CRC32, with no cross-check against the ADR-0024 raw sha256 manifests.
This ADR design-locks the full backup/restore contract: what is durable vs regenerable, how raw bytes opt
in, how restore guards non-regenerable human/graph state, and how integrity is verified end to end.

## Decisions

**1. Raw bytes: default-exclude, env opt-in (`BACKUP_INCLUDE_RAW=1`), manifest-driven.**
Raw bytes are excluded by default (size + privacy: `normalized/` and raw can contain source text, so
default-excluding both keeps backups metadata-shaped unless a human opts in). Opt-in via
`BACKUP_INCLUDE_RAW=1`. Policy mirror: `policies/retention.yaml → raw_files.backup: false`.
- **Inclusion is manifest-driven, not a subdir allowlist.** Intake never moves files out of
  `raw/inbox/` (ADR-0007: "no raw file is ever modified, moved, or deleted") — catalogued sources live
  wherever they were first observed, very often in `raw/inbox/`. So on opt-in we back up **exactly the
  raw paths the manifests catalogue**: `relative_raw_path` **plus every `occurrences[].relative_path`**,
  wherever they sit under `raw/` (including `raw/inbox/`), each resolved through
  `safe_under(root, root/"raw", rel)`. An earlier draft used a `raw/{permanent,ephemeral,assets,
  transcripts}/` allowlist; that was wrong for this system (intake does not promote files into those
  dirs) and would have backed up almost no real source bytes.
- **Excluded even on opt-in:** un-manifested files under `raw/` (e.g. staging not yet ingested) — they
  are not catalogued sources.
- **Always included regardless** (it is the source catalog, small, non-regenerable): `raw/manifests/`.
- **Hard-fail, never silently omit:** when raw is requested, a catalogued raw path that is missing on
  disk, escapes `raw/`, or whose bytes disagree with the manifest sha256 aborts the backup. The backup
  must not ship a "raw included" archive that silently drops or misrepresents a catalogued source.
- The backup **report must state whether raw bytes were included.** When excluded, it warns that restore
  recovers metadata/wiki/db state but **not** the source bytes themselves.

**2. Domain set — durable (always) vs opt-in vs excluded-regenerable.**
The LLM-derived layer (claims/synthesis/enrichment **relationships**) lives in `db/graph.sqlite`, which is
always included, so the durable semantic state is covered by `db/`. The set:
- **Durable, always included:** `db/` (graph SoT incl. LLM-derived edges), `reviews/` (human decisions),
  `raw/manifests/` (source catalog), `policies/`, `templates/`, `evals/`, `scripts/`, `.claude/`,
  **`.agents/`** (durable agent/skill config — `grill-with-docs` skill lives here, parallel to `.claude/`),
  and the top-level config files (`CLAUDE.md`, `AGENTS.md`, `README.md`, `pyproject.toml`,
  `docker-compose.yml`).
- **`wiki/`: included as a convenience/human-facing projection** (and to preserve any human-edited
  generated pages), though it is regenerable and is **not** the evidence authority — `db/` is.
- **Opt-out:** `db/llm_cache.sqlite` (`BACKUP_EXCLUDE_LLM_CACHE` — cost-saver, not correctness; ADR-0027).
- **Opt-in:** `indexes/vector/` (`BACKUP_INCLUDE_VECTOR_INDEX`), raw bytes (decision 1).
- **Excluded, regenerable:** `normalized/` (derived from raw; can contain source text → excluding by
  default upholds the raw-privacy posture), `indexes/` non-vector (keyword index is a cheap rebuild).
- **Always excluded:** `.env` (secrets).
- `.codex/` is currently empty → documented as developer-local, **not** backed up; if it later holds
  durable config, treat it like `.claude/`/`.agents/`.

**3. Restore: guarded in-place, `--force` required to overwrite, no implicit prune.**
A `--restore <archive>` mode extracts into the repo root with these guards:
- **Refuses to overwrite existing files by default;** `--force` is required for any overwrite.
- **`--dry-run`** lists writes / skips / conflicts without touching disk.
- **Conflicts on durable, non-regenerable state are called out explicitly:** `db/graph.sqlite`,
  `reviews/`, `raw/manifests/`, and `policies/` (policy drift changes interpretation).
- **Partial by design, labelled loudly.** Default (no `--force`) restore *fills gaps* — it writes files
  absent from the target and skips pre-existing ones — which is the disaster-recovery case (restore into a
  fresh or partially-present tree). Any run that skips a pre-existing file is reported as **PARTIAL**
  (never an unqualified "restore succeeded"), with the skipped/durable-conflict counts; raw bytes left in
  place are reported as **skipped**, distinct from (and never counted as) verified/restored bytes.
- **Never deletes** target files that are absent from the archive (no implicit prune; a `--prune` decision
  is deferred, consistent with the no-silent-deletion posture).
- Raw recovery follows archive contents: if raw bytes were excluded, restore states so and skips raw
  checksum verification (decision 4).

**4. Integrity: manifest checksum on raw at both ends; hard fail on mismatch.**
ZIP CRC32 is necessary (catches archive corruption — `zipfile.testzip()` on restore) but **not
sufficient** (it cannot catch raw bytes that drifted from their manifest *before* the backup ran).
- When `BACKUP_INCLUDE_RAW=1`: at **backup**, verify every catalogued raw file's bytes (the
  manifest-complete set of decision 1 — `relative_raw_path` + all `occurrences[].relative_path`) against
  its `raw/manifests/*.json` sha256 (ADR-0024) **before** writing the archive; at **restore**, re-verify
  the raw bytes it wrote against the restored manifest **before** reporting success. Because inclusion is
  manifest-driven, every included raw path is manifest-referenced; the embedded `BACKUP_MANIFEST.json`
  sidecar (path + size + sha256) is the restore-time fallback when a path is absent from the restored
  catalogue (e.g. manifests were skipped on a non-force restore).
- When raw is excluded, restore reports: *"raw bytes not present; manifest/raw checksum verification
  skipped."*
- **Any checksum mismatch is a hard error** that aborts the backup/restore — never a warning. (Raw is
  immutable; a drifted byte is a real ADR-0024 integrity violation, not a cosmetic one.)

**5. Output semantics: timestamped, append-only snapshots; no auto-prune.**
Each run writes a new immutable `backups/knowledge-system-backup-<UTC stamp>.zip`; backup never overwrites
or deletes an older archive. Auto-prune is a destruction policy and stays under explicit human control
(manual, or a future `--prune`/retention decision). No content-addressing/dedup (durable state — `db/`,
`reviews/` — changes nearly every run, so dedup rarely fires and adds storage-engine complexity for no
durability gain). The backup report includes: **archive path, size, included domains, and whether raw /
vector index / llm_cache were included.**

## Consequences

The backup is a boring, append-only, metadata-by-default durability copy with an explicit raw opt-in that
respects the raw-privacy posture; restore cannot silently clobber reviewed graph state or human review
decisions; and integrity is checked against the same sha256 system of record that ADR-0024 already
maintains, so backup/restore catches both archive corruption and pre-backup raw drift. Costs: a hash pass
over raw on opt-in backup/restore, more flags (`BACKUP_INCLUDE_RAW`, `--restore`, `--force`, `--dry-run`),
and an embedded per-archive file manifest. Pruning old archives and a `normalized/` opt-in are deliberately
deferred.
