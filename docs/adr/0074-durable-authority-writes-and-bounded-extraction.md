# ADR-0074 — Durable authority writes and bounded extraction

- **Status:** accepted — design-locked by grill-phase 2026-11-11 (Q1–Q9). **Slice 1 (decision 1)
  implemented 2026-11-11** — `app/backend/durable_write.py`, guard `tests/write_guard.py` +
  `tests/test_authority_write_guard.py`. **Slice 2 (decision 2) implemented 2026-11-12** — typed
  manifest read states in `app/backend/manifests.py`. **Slice 3a (decision 4: Item / Claim /
  Synthesis pages) implemented 2026-11-12** — shared reader `app/backend/authority_read.py`.
  **Slice 3b (review records) implemented 2026-11-12** (review round 2, 2026-11-14: durable removal,
  decision transitions, per-decision audit entries, typed review read model, symlinks refused).
  **Slice 3c (policies, journals, `db/*.json`) implemented 2026-11-14.** **Slice 4 (decision 3:
  `scripts/repair_manifest.py`) implemented 2026-11-15**, with Details history auditing (user
  decision 2026-11-15). **Slice 5a (decision 8 install journal and recovery, 7.4 staging
  cleanup) implemented 2026-11-15** — `app/backend/extraction_install.py`. **Slice 5b (consistent
  consumer reads) implemented 2026-11-15** — `extraction_install.consistent_read` and every
  consumer. **Slice 6a (decisions 5, 7.1–7.2: the extraction child) implemented 2026-11-16** —
  `app/workers/extract_runner.py`, `extract_child.py`, `extract_watchdog.py`, `extract_parse.py`.
  **Slice 6b (decision 6: attempt records and retry classification) implemented 2026-11-16** —
  `app/workers/extract_attempts.py`. **Slice 6c (decisions 7.3, 7.5: stale extraction jobs and
  cancellation) implemented 2026-11-16** — `db.reconcile_interrupted`, `extract.cancel`; slice 6
  complete. **Change 7a grilled 2026-11-16** (see "Change 7a" in Part C); **7a-1 (Q1, Q2, Q6:
  retrieval gating, graph Source nodes, silent exclusion) implemented 2026-11-16**; **7a-2 (Q3–Q5:
  the final model-call check, the guarded save, the warning rename) implemented 2026-11-16** —
  change 7a complete. Changes 7b–7c not yet.
- **Operational acceptance** (user decision 2026-11-14): ADR-0074 was **not operationally accepted
  until change 7a landed**; with 7a-2 it is. Visibility now fails closed for `/search` and `/query`
  retrieval, for the model call (from the final check on) and for saving an answer, despite a
  failed publication. **Retained limitation:** Query pages saved *before* a hide keep their
  quotations discoverable (deferred, see the table); a request already sent to the provider cannot
  be withdrawn.
- **Date:** 2026-11-11
- **Drivers:** external review of `06f3d56` (2026-11-11), blocking findings 1 and 3. Finding 2
  (item descriptions corrupted across re-renders) was fixed separately in `dfda610`.
  - **Finding 1 — authority can be lost in an interrupted write.** `save_manifest`
    (`app/backend/manifests.py`) truncates the manifest in place. `load_manifest` returns `None`
    for an absent *and* for an unparseable file, so intake (`app/workers/intake.py`) re-creates a
    corrupt manifest as a new, default-`active` source: a hidden or archived document comes back and
    its confirmed Details are lost. Item, non-durable Claim and Synthesis page writes, and in-place
    review-record rewrites, truncate the same way.
  - **Finding 3 — the extraction timeout does not apply through the API.** `_time_limit`
    (`app/workers/extract.py`) uses `SIGALRM`, which works only on the main thread; `/jobs/extract`
    is a synchronous FastAPI route, run on a worker thread, so a malformed document can occupy
    extraction (and the producer lock) indefinitely, against ADR-0010's untrusted-input boundary.
- **Related:** ADR-0008 (manifests are authoritative), ADR-0010 (extraction safety limits),
  ADR-0068 (vault consistency contract: durable authority vs projections, write lock, producer
  journal, publication recovery), ADR-0073 decision 10 (durable journal and page writes).

The two findings share one grill but are decided separately: decisions 1–4 cover authority writes
(finding 1), decisions 5–8 cover extraction (finding 3), decision 9 orders the work.

## Part A — durable authority writes (finding 1)

### Decision 1 — scope of the durable-write rule (grill Q1)

**Every write of file-backed authority goes through one shared durable-write helper** — replacing
an existing file *and* creating a new one, so no reader ever sees a truncated or half-written
authority file. The helper writes a temporary file in the destination's directory, flushes and
fsyncs it, `os.replace`s it over the destination, then fsyncs the directory.

**Covered** (ADR-0068 §1 "where authority lives", file-backed part):

- `raw/manifests/*.json`;
- Item, Claim and Synthesis pages — **wholly**, because they are mixed pages: re-rendering a
  projection section rewrites the file, and with it the durable fields it carries;
- everything written under `reviews/` — pending, decided and in-place-rewritten review records
  (defer, reopen, amendments), tag ledgers, **audit records** and **recovery journals**
  (`reviews/in_flight/`, identity journal);
- `policies/`;
- the `db/*.json` authority records (publication queue, revision record, producer journal, Apply
  authorizations, contradiction estimates, tag labels).

**Not covered:**

- **SQLite** (`db/graph.sqlite`, `db/jobs.sqlite`, `db/llm_cache.sqlite`) keeps its own transaction
  and durability mechanisms.
- **Lock files** (`db/.run-*.lock`, `db/.vault.lock`) and **temporary staging files** (extraction's
  stage directory, the helper's own temp files).
- **Projections** (Source pages, `index.md`, `log.md`, `wiki/Authors/`, `normalized/`, vector-index
  metadata, eval reports): rebuildable by definition (ADR-0068 §1); their repair is ADR-0068's.

**Consolidation.** The existing hand-rolled durable writers — `identity_journal._replace`,
`reviews._write_json_durably` and its re-sync barrier, `claims._write_page_durably` and its barrier,
`authors._write_atomically`, the publication queue, the producer journal, `tag_links` and
`tag_history` — are folded into the one helper (neutral module, e.g. `app/backend/durable_write.py`).

**Boundaries — what atomic replacement does not do:**

- It does **not** replace the write lock, multi-file journals (producer journal, identity journal,
  supersede journal, publication queue) or operation-specific creation and ownership checks (the
  page-identity guards, `create`-only semantics where a caller needs them). Those stay where they
  are; the helper only makes each single-file write all-or-nothing and durable.
- **A directory-fsync failure is an error**, propagated to the caller **even though the replacement
  has already happened**. The new content may or may not survive a power loss, so the caller must
  treat the write as *uncertain completion* and handle it safely (its journal / retry / re-sync
  barrier), never as success and never as "nothing happened".

**Structural guard.** A deterministic check fails when an authority destination is written outside
the helper. It recognises the known writer entry points **and** the authority destinations,
including aliases and wrappers (a path variable bound to an authority directory, a local helper that
calls `write_text` / `json.dump` / `open(..., "w")`), not merely literal `write_text` / `json.dump`
calls. Allowed exceptions (the helper itself, lock files, staging) are listed explicitly.

**As implemented (slice 1).** `durable_write.write_bytes / write_text / write_json / write_stream`
(replace, or `create_only=True`: the complete temp file is hard-linked into place, so an existing
destination — a dangling symlink included — is never replaced), `sync_existing` (a file's re-sync
barrier) and `sync_dir`; `UncertainCompletion(OSError)` for a directory-sync failure after the
replacement. Temporary files are `.<name>.tmp-<hex>` beside the destination. Existing semantics are
kept: writes that overwrote still overwrite; only the quarantine copies of damaged journals
(`*.damaged-*.json`) became create-only. Claim pages lost their `durable=` switch (every page write is
durable now). Backup restore writes each file through the helper. The guard has two halves: a static
**deny-by-default** AST scan of `app/` and `scripts/` (every raw write primitive, called or
referenced, under any import alias, must be the helper or an allowlisted non-authority write with a
reason; stale entries fail) plus a registry of each family's known writer entry points; and a
**runtime audit hook** active for the whole test suite (`tests/conftest.py`) that fails any test in
which project code writes an authority-shaped path outside the helper.

Review round 1 added three guarantees:

- **Directories are durable too.** `durable_write.make_dirs` creates missing directories top-down,
  one level at a time, and fsyncs each new entry into its parent before creating the next level. If
  a parent can't be synced, the directory just created is removed and the error is raised, so a
  retry never trusts an entry that was never synced. If it can't be removed either, that is
  `UncertainCompletion`.
  - Review round 2: an existing directory is never trusted just because it is visible. Its creator
    may have died between `mkdir` and the parent sync, or may not have synced yet. Before a write
    first relies on an existing directory, the helper **establishes** it: it fsyncs the parent of
    each directory in the chain, top-down, and caches the result for the process, keyed by
    `(st_dev, st_ino)`.
    - The walk stops at a directory this process has already established, or at one the current
      user doesn't own, since the system never creates those.
    - The same applies with `mkdir=False` and in `sync_existing`, so the next writer, or a retry,
      repairs an interrupted creation.
    - Steady state costs one `stat` per write.
    - Known limit: if another process deletes and recreates an established directory and its inode
      number is reused, the cache would trust the new directory.
  - The helper's own `mkdir=True` uses `make_dirs`, and so does every caller that creates an
    authority directory or a vault-level ancestor of one: `raw/`, `raw/manifests/`, `wiki/`, the
    page directories, `reviews/**`, `policies/**` and `db/`. That includes the SQLite and lock-file
    openers and the `wiki/log.md` appenders that create `db/` or `wiki/` as a side effect.
  - Both halves of the guard check directory creation: `.mkdir`, `os.mkdir` and `os.makedirs` in
    the static scan, and the `os.mkdir` audit event at runtime.
- **A replacement never widens access.** The temp file gets the existing destination's permission
  bits (`fchmod`) before any content is written. If those bits can't be read, the write is not
  attempted. Ownership is not carried over; the vault is single-user.
- **Cleanup can't skip the barrier.** For a create-only write, installing the file (`os.link`) and
  removing its temporary name are separate steps. The directory sync runs whenever the destination
  is installed, even if removing the temp name fails, and only that sync confirms durability. If
  the sync fails, the result is `UncertainCompletion`. If it succeeds, the leftover hidden link is
  logged as a warning and the write counts as complete.

**Tests.** One implementation, many tests: a **focused fault-injection suite** for the helper
(failure at each step — temp write, file fsync, replace, directory fsync — leaves the old content
intact or reports uncertain completion; no temp file left behind on a clean failure), plus
**integration coverage per authority family** (manifests, Item/Claim/Synthesis pages, review records
and audit records, journals, `policies/`, `db/*.json`).

### Decision 2 — manifest read states and what each caller does (grill Q2)

Today `load_manifest` returns `None` for an absent, an unreadable and an unparseable manifest alike;
`list_manifests` and `valid_manifests` (every runtime worker) **silently drop** an unparseable file —
it is not even a `skipped` reason — and `validate_raw_integrity` skips it too, so no surface reports a
corrupt manifest. Decided:

**One typed reader** returns a state with the record:

| State | Meaning |
|---|---|
| `absent` | **Verified** absence: no entry at `raw/manifests/<source_id>.json`. A dangling symlink, an unreadable directory, or a failed inventory scan (the directory cannot be listed) **never** establishes absence. |
| `unreadable` | The entry exists (or its existence cannot be confirmed) but cannot be read: permission or I/O error, a directory or other non-regular file, a dangling symlink. Possibly transient. |
| `corrupt` | The bytes were read but are not a manifest object: invalid UTF-8, invalid JSON, JSON that is not an object. |
| `ok` | A **parsed object** — not yet a validated manifest. |

- **Identity validation stays separate.** `ok` is followed by the existing checks (canonical id,
  filename equals id, no duplicate id) with their existing quarantine reasons (`non_canonical_id`,
  `filename_mismatch`, `duplicate_source_id`). **No caller uses an `ok` record before those checks
  pass.**
- **`unreadable` and `corrupt` stay distinct**: retrying an I/O failure and recovering damaged bytes
  are different operator actions.

**Callers:**

- **Intake.** **Only `absent` permits creation.** For `unreadable` or `corrupt` it **preserves the
  file** (no write, no occurrence update, no new canonical path), reports a per-document error and
  continues with the other documents.
- **Runtime workers.** `unreadable` and `corrupt` become two more categorical `skipped` reasons
  (never the file's contents), counted in every job result, and the job reports **incomplete
  coverage**. **An incomplete manifest inventory blocks any pruning, deletion or reconciliation whose
  correctness depends on proving a document is absent** (for example retiring a source's derived
  artifacts, graph nodes or review items because no manifest lists it).
- **Retrieval.** An affected document is **excluded before its evidence reaches the model** (fail
  closed). Part C's search decision builds on this.
- **Validators.** `validate_raw_integrity` **fails** on any unreadable or corrupt manifest and on a
  failure to enumerate `raw/manifests/`, naming the file by a safely rendered repository-relative
  path, never echoing its contents.
- **Recovery.** **No automatic recreation, overwrite or repair** — decision 3 decides the operator's
  path.

**Caller audit.** Every caller of `load_manifest`, `list_manifests` and `valid_manifests` (about 25
modules, `app/` and `scripts/`) is moved to the typed states; **no compatibility wrapper** may keep
collapsing a failure into `None` or an empty inventory. A caller that only needs "the record if it is
usable" must still receive and propagate the failure state (count it, report it, block absence-based
actions), not drop it.

**As implemented (slice 2).** `manifests.read_manifest_file` / `read_manifest` return a
`ManifestRead(state, path, record, error)`, where `error` is a categorical tag, never contents.

- **How each state is decided:**
  - `absent` needs ENOENT on the file *and* a confirmable, non-dangling parent directory.
  - The file is opened non-blocking (`O_NONBLOCK`), so a FIFO can't hang the read. A directory,
    FIFO or other non-regular file, a dangling symlink, a permission error and an I/O error are all
    `unreadable`.
  - Invalid UTF-8, invalid JSON (including nesting too deep to parse), and JSON that is not an
    object are `corrupt`.
  - A record whose own `source_id` differs from its filename is `corrupt` (`id_mismatch`) for that
    document.
- **Single-document reads:** `get_manifest` returns the record, `None` *only* for a verified
  absence, and raises `ManifestUnavailable` for a damaged manifest. Every read refuses a
  non-canonical id with `ValueError`; the old lenient read path returned `None` for it.
- **Writers:** `set_status` and `set_provenance` raise on a damaged manifest and never overwrite
  it.
- **Inventory:** `scan_manifests` returns a `ManifestInventory`.
  - `valid`: the records that pass the unchanged identity checks.
  - `skipped`: one categorical reason per skipped file, which now includes `unreadable` and
    `corrupt`.
  - `damaged`: the safely rendered paths of damaged files. `damaged_ids` maps the canonical-named
    ones to their state.
  - `quarantined_ids`: every canonical id an identity-quarantined file names (its filename stem
    or the `source_id` it claims), mapped to the unchanged reason (`duplicate_source_id`,
    `filename_mismatch`, `non_canonical_id`).
  - `enumeration_error`: set when `os.listdir` fails, so an unlistable directory is never an empty
    inventory.
  - `complete`, `coverage` (`complete` / `incomplete` / `unavailable`), `report()` (the job-result
    counts) and `require_complete(action)`, which raises `IncompleteManifestInventory`.
  - Identity quarantine counts as excluded authority: the inventory is not `complete` while a
    damaged **or** quarantined file exists, so neither can establish an absence (review round 1).
- **Revalidation:** `manifests.recheck(dir, sid, inventory)` is the under-lock re-read before a
  mutation or recovery: the fresh typed read, with an id the inventory quarantined kept excluded
  (`read_reason` names it `manifest_<identity reason>`). A duplicate created after the scan is not
  detected by the single-file re-read; the next scan quarantines it.
- **Old readers removed:** `load_manifest`, `list_manifests` and `valid_manifests` are gone, with
  no compatibility wrapper. All 50 call sites in 24 modules moved:
  - **Intake** reports `manifest_<state>` and preserves the file.
  - **Extract, Claims, Items and generate_wiki** re-read under the write lock before committing, and
    stop the document on any state other than `ok`. They never fall back to the scan's copy.
    Claims and Items do so on **every** mutating branch — the empty-input replacement and the
    producer-journal recovery included. Recovery finishes an unfinished commit from authority only
    for a verified absence or a usable manifest whose status no longer extracts; a damaged or
    quarantined one keeps its record queued and its evidence / mentions standing
    (`journal_preserved`).
  - **Every worker's job result** carries `manifests_unreadable`, `manifests_corrupt` and
    `manifest_coverage`.
  - **Absence-dependent actions** are blocked on an incomplete inventory: synthesis retraction
    (`retraction_blocked`), Authors-page pruning (`InventoryError`), and contradiction maintenance
    (`manifest_inventory_incomplete` is unverifiable, never "not independent").
  - **Producer-journal repair** keeps damaged and quarantined documents for redo.
  - **Contradiction checks** refuse a damaged or quarantined document as `manifest_unavailable`.
  - **Publication** reports the typed state.
  - **Lint** files a `manifest_<state>` finding instead of "missing raw".
- **Retrieval:** `search.run_search` requires `manifests_dir`. A per-search gate admits evidence
  and Source navigation hits only for ids in the inventory's identity-validated `valid` set, so a
  damaged, quarantined or absent manifest (a removed manifests folder included) excludes the
  document, with a note. The inventory is scanned once per search, only when a hit needs it.
  `/query` uses the same search; a test drives `/query` with a fake model and checks the excluded
  document never reaches its prompt.
- **Validators:** `validate_raw_integrity` fails on damage and on an enumeration failure, by a safe
  path. `validate_projection`, `validate_wiki` and `validate_normalized`
  report a damaged manifest's documents as uncheckable, never as orphans.
- **API:** every per-document endpoint — `GET /sources/{id}`, `/raw/{id}`,
  `/sources/{id}/chunks` and `/normalized` — resolves through one inventory-aware lookup,
  `manifests.lookup`, so it agrees with `GET /sources` and search. A damaged or quarantined
  manifest (a duplicate elsewhere included) is 409 `manifest_<state>` / `manifest_<identity
  reason>`. An inventory that can't be listed is 409 `manifest_inventory_incomplete` for any id,
  since an enumeration failure never establishes an absence. Only a verified absence (or a
  non-canonical id) is a 404. Damage or quarantine of other documents never blocks a validated
  one. `GET /sources` returns the counts and the coverage (review round 2).
- **Not in this slice:** recovery tooling (slice 4) and visibility suppression of *valid* manifests
  (change 7a).

### Decision 3 — operator recovery of a damaged manifest (grill Q3)

Today `scripts/backup.py --restore` restores **whole archives** only (it skips pre-existing files, or
overwrites everything with `--force`); there is no single-file restore and no manifest repair tool.
Decided: a dedicated, inspect-first script in the `repair_supersede.py` pattern (ADR-0073 decisions
18–23).

**`scripts/repair_manifest.py inspect <source_id>`** — read-only. It reports:

- the state (`unreadable` / `corrupt`) and its error category;
- the damaged file's size and sha256, or **explicitly "unavailable"** when they cannot be read; never
  its contents;
- the raw file's location and integrity **only from trustworthy evidence** — a corrupt manifest may
  provide no trustworthy raw path, which is then reported as unavailable, not guessed;
- the backup archives holding a usable manifest for this id, with each copy's `status` and
  **confirmed Details only** (never source excerpts or arbitrary manifest fields);
- the governance and Details decisions in the audit log that bear on this document, and whether each
  backup copy is **compatible** with them;
- a state fingerprint (input to `--expected-state`).

Exit codes: **0** for a completed diagnostic report, including one that finds damage; **nonzero** for
an invalid invocation or a failure of the inspection itself.

**`scripts/repair_manifest.py restore <source_id> --archive <zip> --expected-state <digest>
--reason "<why>"`** — the only write path.

- **Lock order** is the established one: **vault lock → write lock**. The fingerprint and **every
  precondition are re-checked inside that boundary**.
- **The backup copy is validated**: the archive member is read **alone** (no extraction of arbitrary
  paths; **duplicate or oversized members reject the archive**), it parses, passes the identity checks
  (canonical id, filename equals id), its **full stored SHA-256** is well-formed and agrees with the
  canonical id — and, where the raw file is present and trustworthy, with the raw bytes. Matching the
  16-hex source-id prefix alone proves nothing about raw-byte integrity.
- **Compatibility with later human decisions.** If the audit log holds a governance change (hide,
  archive, unhide, deprecation, …) or a Details change **newer than the backup copy**, restore
  **refuses** and requires a newer compatible backup. There is **no override** that temporarily
  resurrects visibility; explicit reconciliation of an old backup with later decisions is a separate,
  future recovery decision.
- **Unknown is not compatible.** An unreadable audit record or an incomplete audit scan, where
  compatibility cannot be established, **blocks** restore.
- **Order of writes** — no absence window, intent before effect:
  1. **copy** (never move) the damaged bytes durably to `raw/manifests/quarantine/<source_id>-<timestamp>.json`
     — kept, never deleted, ignored by every manifest inventory;
  2. **record recovery intent**: queue publication and persist a small recovery journal (recording
     what is about to be installed, the quarantine copy, the reason);
  3. **atomically replace** the live manifest with the validated backup copy (durable helper);
  4. finish publication and write the audit record, then close the journal.

  Moving the damaged file away first is rejected: the gap would read as `absent` and authorize intake
  to re-create the manifest. A crash after step 3 leaves the journal with enough to finish publication
  and the audit **idempotently**.
- **`unreadable` has no write path**: the operator fixes the I/O or permission problem and re-runs;
  `inspect` says so.

**Deferred — rebuild without a backup.** Reconstructing a manifest from the raw file plus the audit
log is lossy (occurrences, modification times, unconfirmed fields) and is not built now. **Reopening
trigger:** a damaged manifest with **no compatible backup** — including the case where every backup
is too old to preserve later human decisions.

**Details history (user decision 2026-11-15, found implementing slice 4).** Confirming or clearing
Details used to write only the manifest — no audit record — so a Details change newer than a backup
was undetectable. Decided: **audit from now, strict.**

- Every write of a Details field (`manifests.set_provenance`: confirm, clear, any other Details
  write) records `reviews/audit_log/<source_id>-details-<confirmed|cleared|changed>-<id>.json`
  (`event`, `at`, `details_history`) **durably, create-only, before the manifest changes**, under the
  caller's write lock. A damaged manifest writes no record (the change is refused first).
- Backups made by this code carry **`details_history: 1`** in their sidecar
  (`BACKUP_MANIFEST.json`). The marker, not the timestamp, proves that no later Details change
  could go unrecorded.
- Restore **refuses** an unmarked backup (Details history unknown) and an incomplete or unreadable
  audit history.
- An interrupted audit-first attempt (record written, manifest change never made) is treated
  conservatively: it may block a restore.

**As implemented (slice 4 — `app/workers/manifest_repair.py`, `scripts/repair_manifest.py`).**

- **One snapshot** feeds the report, the offered command and the fingerprint
  (`manifest-recovery-v1`): the live manifest's bytes and typed state, other manifest files claiming
  the id, an open recovery journal, the audit records bearing on the document, its published graph
  status, every archive's copy in `<root>/backups` (`--backups DIR` otherwise), and the raw hashes.
  `inspect` takes no lock and writes nothing; it reports byte size and sha256 (never contents), the
  raw path only when a copy's sha256 is verified against the raw bytes, each copy's status,
  confirmed Details and compatibility verdict.
- **Archive copy validation:** exactly one `raw/manifests/<id>.json` member (and sidecar), at most
  4 MiB, unencrypted, read alone; parses with the identity checks; full 64-hex `sha256` agreeing
  with the id and, when the raw file is a readable regular file, with its bytes; a `status` outside
  the lifecycle statuses (which would read as `active`) and malformed provenance are rejected. Raw
  bytes and archives are read non-blocking and never through a link (`authority_read.hash_file`).
- **Compatibility** — any reason refuses: no `details_history` marker; unknown creation time; an
  incomplete audit history (log unlistable; a relevant record unreadable, unparseable, untimed, or
  naming the document in an unknown form); a Details record **at or after** the backup's time; an
  archive / hide / unhide decision (approved, decided again, reopened — not rejected, withdrawn,
  deferred or refreshed) at or after it; **the copy's status disagreeing with the published graph
  status**, or that status unreadable. The last check goes beyond the decision's text: a governance
  approval made before a backup but applied after it leaves no apply-time record, and restoring the
  pre-apply status would resurrect visibility until the next Apply. **Availability tradeoff
  (review round 1, kept):** an approval that was never applied, followed by damage, also makes every
  copy disagree with nothing to reconcile it — and Apply may itself refuse the damaged manifest, so
  "apply the approval first" is no remedy. Restore then stays blocked until a compatible backup
  exists, or a separately designed reconciliation procedure (not part of this slice) settles the
  status. Damaged audit records named for other subjects (tags, claims, other documents) don't
  block.
- **Write path** (`restore`, a corrupt manifest only, no stray claimant, no open journal): run
  guards (apply, intake, extract, republish), then `write_lock.hold(changes=False)` (vault lock →
  write lock), a fresh snapshot whose fingerprint must equal `--expected-state` (exit 6
  otherwise), the archive must be one of the offered compatible copies; then the revision advances
  and: (1) quarantine copy `raw/manifests/quarantine/<id>-<stamp>.json`, create-only;
  (2) publication intent, then `db/manifest_recovery/<id>.json`; (3) atomic replacement with the
  copy's exact bytes; (4) publication, the audit record
  `<id>-manifest-restored-<recovery_id>.json` (create-only, reused on retry), the journal removed.
- **The recovery journal is validated and bound** (review round 1): exactly the fields restore
  writes; `source_id` equal to the requested document (the journal's file name); `recovery_id`
  recomputed from document, archive and expected state; a bare `.zip` archive name; well-formed and
  distinct hashes; the quarantine name `<id>-<stamp>.json`, and that file read through the typed
  reader holding exactly `damaged_sha256`; the `pub_` intent id, actor, a reason, parseable times.
  Anything else is `invalid (invalid_field:<name>)` / `invalid (quarantine_unverified:…)` with no
  write path. Completion takes the requested id, never the journal's. An existing recovery audit
  record is reused only when every field equals the expected record except `at`, which must still
  be a valid timestamp.
- **`finish`** rolls a valid journal forward. **Copy already live:** it completes. **Copy not
  installed yet:** `inspect` offers `finish` only when the identity checks pass and the journal's
  archive copy is still present, the same bytes and **compatible** under the rules above, against
  the current snapshot — so a human decision recorded after the journal blocks `finish` exactly as
  it blocks `restore`; `finish` re-evaluates under the locks. Every refusal (invalid journal,
  incompatible or missing copy) precedes the revision advance and every write. Before publishing it
  **repeats the durability barriers** (`durable_write.sync_existing`) of the journal, the
  quarantine copy and — when the copy is already live — the installed manifest, since the earlier
  run may have failed after the rename, during its directory sync.
- **Uncertain writes** (`UncertainCompletion` or another `OSError` part-way): exit 1 with a message
  naming only what that stage established — no journal yet (quarantine / intent), a journal that
  may or may not be durable, a recorded journal with an uncertain replacement — and "inspect
  again", which offers `finish` only when that path is safe.
- **Quarantine copies are ignored by every manifest inventory**: the manifest scans list only the
  top level of `raw/manifests/`, and the two raw catalogues that used `rglob` (backup's raw index,
  the Apply sandbox) now do too.
- **Tests:** `tests/test_manifest_repair.py` — Details audit order and interruption, the backup
  marker, every rejection and incompatibility, unknown audit history, crash before and after the
  replacement finished by `finish`, refusals that change nothing (no revision advance), FIFOs;
  round 1: later decisions / strays / a vanished archive blocking a pending `finish`, a journal
  naming another document (that document's journal untouched), every invalid journal field and
  quarantine mismatch, recovery-audit binding, stage-specific uncertain-write messages, and a
  directory-sync failure after the replacement whose `finish` completes every barrier before
  publication and the journal's removal (call order asserted).

### Decision 4 — the read discipline covers all file-backed authority (grill Q4)

The same damage-read-as-absence mechanism exists beyond manifests: `items._read_node_meta` returns
`None` for a page without a readable title and the item writer then renders from defaults
(`items.py`, `(existing or {})`), losing title, aliases, description and decided status;
`reviews.py` replaces an unreadable review record with `{"review_id": …}` and continues retiring it.
`review_read._load_dir` already counts parse and schema errors, and the ADR-0068/0073 journals
already fail closed. Decided — **for every file-backed authority family of decision 1**:

1. **Absence permits creation only when the domain allows it.** Verified absence is necessary, not
   sufficient: a missing page for an established item (a graph node, ledger entries or review records
   refer to it) may be **lost authority**, and producers must not re-create its human-owned fields
   from defaults. Each family states when absence legitimately means "new".
2. **`unreadable` and `corrupt` are never absence**: never overwritten with defaults, never rebuilt by
   a producer, reported by category and counted.
3. **A writer that cannot read the durable fields it must carry forward refuses** (for pages: a
   missing or unparseable frontmatter block, or missing required durable fields), since re-rendering
   would silently reset them.
4. **Failure scope follows dependencies.** Skip one subject only when the remaining work is
   independent and safe. An unreadable **shared** input — a policy, a journal, an inventory — may
   require refusing the whole operation. Absence-based actions (pruning, retiring, "no page, so
   create one") are blocked while that family's inventory is incomplete.
5. **Explicit recovery contracts are preserved.** ADR-0068's revision-record recovery is an existing,
   deliberate exception: damaged revision state is replaced with a new epoch and every outstanding
   preview is invalidated. It keeps that behaviour; this decision does not impose blanket refusal on
   it. Any other family with a written recovery contract is listed as conforming, not rewritten.
6. **Shared mechanics, family-specific validation.** Byte reading, the error categories (`absent` /
   `unreadable` / `corrupt` / `ok`) and durable writing are shared. Parsing and required-field
   validation stay **per family** (manifests, Item/Claim/Synthesis pages, policies, review and audit
   records, journals). **Optional or legitimately absent legacy fields remain distinguishable from
   damage** — a pre-ADR page without `description` or `split_from` is not corrupt.
7. **Proof is behavioural.** The structural guard (decision 1) also flags known unsafe fallbacks
   (defaults substituted after a failed read), but pattern matching is not the proof: **behavioural
   tests per family** show that a read failure cannot reset authority.

**Recovery for the other families** is restore from backup by the operator — sufficient for this
slice **provided it follows the rules already agreed**: preserve the damaged bytes by copy before
replacement, the vault lock → write lock order with preconditions re-checked inside it, publication
recorded before replacement, and refusal when later human decisions cannot be preserved. Dedicated
per-family tooling is deferred; **reopening triggers:** the first damage observed in a family, or a
backup recovery that cannot safely preserve later human decisions.

**As implemented (slice 3a — pages).** Slice 3 is split per decision 9: **3a** the Item, Claim and
Synthesis pages; **3b** review and audit records; **3c** policies, journals and the `db/*.json`
records.

- **Shared mechanics:** `app/backend/authority_read.py` (`read_file`, `read_text`; states `absent`
  / `unreadable` / `corrupt` / `ok`; strict UTF-8, never a lossy decode). The manifest reader now
  reads through it. `wiki_render.read_page` adds the page rule: a page without a frontmatter block
  is `corrupt` (`no_frontmatter`).
- **Item pages** (`items.read_item_page`):
  - Required: `title` and `status` (a known node status). `aliases`, when present, must be a list.
  - Legacy-optional, never damage: `item_type` (the graph mirror backs it), `review_status`,
    `confidence`, `split_from` / `split_review_id`, `description`.
  - `_recompose_node` returns `page_unreadable` / `page_corrupt` instead of rendering from
    defaults; `recompose_semantic_node_page` and the executors (split, merge, duplicate, retype,
    promote, tag edits) skip with the precise reason (`items.page_skip_reason`).
  - **Absence means "new"** only when the tag carries no human authority elsewhere
    (`items.page_may_be_created`): its mirror is the producer's `candidate` and no human or
    authored assertion touches it — or the caller created the node in this commit (`human_add`).
    Otherwise the page is `lost_authority` and is never re-created.
  - A damaged tombstone page reads as a decided retirement (`decided_status`), so extraction never
    resurrects it.
  - `extract_items` reports `pages_refused` and finishes `partial`.
- **Claim pages** (`claims.page_authority`):
  - Adds `page_unreadable` / `page_corrupt` (unreadable, invalid UTF-8, no frontmatter) to the
    ADR-0073 states; a dangling link is never absence.
  - `recompose_claim` also refuses a page whose `status` is missing or unsupported (re-rendering
    would undo a hide or a supersede).
  - **Absence means "new"** only when the claim has no node or its mirror is `active`
    (`claims.page_may_be_created`); a `hidden` / `deprecated_candidate` mirror makes the missing
    page `lost_authority`.
  - `extract_claims` reports `claim_pages_refused` and finishes `partial`; publication excludes the
    page like any other failed authority check.
- **Synthesis pages** (`synthesis.read_synthesis_page`):
  - Required: `status` and a `topic_node` that binds to the synthesis id.
  - A damaged page is held like a suppressed one: never regenerated, never retracted, never
    promoted over (the approval path skips it with the reason).
  - **Absence means "new"** only for a synthesis with no node or a `candidate` one
    (`synthesis.page_may_be_created`); an approved, retracted or suppressed one whose page is gone
    is `lost_authority`.
  - `generate_syntheses` reports `synthesis_pages_refused` and finishes `partial`.
  - **Every Synthesis writer** enforces the rule (review round 1): generation, retraction, approval
    (`apply_resolved_syntheses` — a missing page whose node is retracted, approved or suppressed
    is `lost_authority`, the decision stays), the hide / unhide executor, and re-rendering
    (`rerender_synthesis_page`, used by the claim-visibility fan-out and by publication). Each checks
    `synthesis.page_refusal` before it changes anything and reports the reason, leaving page bytes
    and the graph mirror unchanged. The shared renderer `_render_page` checks again and raises
    `SynthesisPageRefused` as the backstop. Re-rendering recomputes only the evidence-derived pair
    (`active` / `evidence_hidden`); every other valid status is preserved, never reset.
- **Split recovery** (review round 1): an open identity journal proves the split owns the
  spin-off's slot, not that the spin-off's page is readable. A spin-off page that exists but can't
  be used stops the split before any link moves or page is written (`page_unreadable` /
  `page_corrupt`), and the journal stays open for the next apply. Merge recovery already validates
  both pages before acting.
- **Tests:** `tests/test_page_read_states.py` — every damage class per family, lost authority vs
  legitimate creation, legacy pages, run-level reporting. Each refusal reverted fails its tests.
- **Not in 3a:** review and audit records (3b); policies, journals and the `db/*.json` records
  (3c). Read-only display paths (e.g. `review_read._family_page_meta`) keep their tolerant reads.

**As implemented (slice 3b — review and audit records).**

- **Typed record read** (`reviews.read_review_record`, on `authority_read`): a record is usable only
  as a JSON object whose `review_id` equals its file name, with a string `type` and an object
  `subject`; anything else is `corrupt` (`invalid_utf8`, `invalid_json`, `not_an_object`,
  `missing_review_id`, `id_mismatch`, `missing_type`, `missing_subject`). An **unsupported type is
  not damage**: it stays a readable record no executor handles. `reviews.scan_review_dir` lists a
  directory with damaged files by safely rendered relative path and category, never their contents,
  and an unlistable directory is an error, never an empty listing.
- **Single-record actions:** resolve, defer, reopen and decide-again refuse a damaged record (or a
  damaged terminal record with the same id) with `ReviewRecordUnavailable` → HTTP 409
  `review_<state>`; decision routes no longer answer 404 for damage. `withdraw_review_item` never
  withdraws (deletes) a damaged record — it used to substitute `{"review_id": …}` and delete it;
  `refresh_pending_proposal` never rewrites one; `create_review_item` treats any entry that is not a
  verified absence (a damaged file, a dangling link) as present and never re-files over it.
- **Apply refuses as a whole** (user decision 2026-11-12): a damaged approved record's subject is
  unknown, so no other approval can be shown independent of it (decision 4.4). One shared
  preflight, `reviews.approved_preflight`, runs under the write lock before anything is copied,
  authorized or executed: the dry run returns a `blocked` preview (`review_records_unavailable`,
  with `problems`) and no token; live Apply raises `ApplyRefused("review_records_unavailable")`
  (409) before the token is consumed; `run_apply` checks again as the backstop before its first
  write. `approved/` that can't be listed refuses the same way. The UI and `scripts/apply_reviews.py`
  list the damaged files (exit code 10). Every other path that executes approved decisions shares
  the preflight: `promote_candidates` refuses; synthesis generation skips its approval step only
  (`review_records_unavailable` in its result, status `partial`) and still generates proposals.
  Read-only diagnostics and backup stay available.
- **Schema** (review round 1): beyond identity, a record needs a non-empty `status`, `proposal` /
  `context` objects when present, and — for a supported type — its required subject fields as
  non-empty strings (`reviews.SUBJECT_FIELDS`; a semantic duplicate's two `node_ids`). A bad shape
  is `corrupt` (`invalid_proposal`, `invalid_context`, `invalid_subject`, `missing_status`), so it
  is refused by the preflight instead of crashing an executor after the publication intent.
- **Audit records** (review round 1): append-only evidence. A retry that would rely on an existing
  keyed entry (`withdraw_review_item` with `audit_key`, `refresh_pending_proposal`) validates it
  first (`reviews.read_audit_record`: a readable object for this review id and this operation);
  a damaged or foreign entry is preserved and the operation refused (`ReviewRecordUnavailable`),
  nothing deleted. `decide_again` refuses instead of overwriting an existing entry that isn't
  exactly this replacement's (with atomic writes, slice 1, an invalid entry is damage, not a torn
  write). Reopen entries are created create-only, never over an existing one. The other readers
  (promote's executor-record verification, supersede's rollback evidence) already failed closed.
- **Reopen checks its destination** (review round 1): a pending slot that holds a damaged record,
  or a different item under the same id, refuses the reopen before anything moves; only an
  interrupted reopen of the same decision (its pending copy written, the terminal file not yet
  removed) is finished.
- **Decision routes read typed** (review round 1): approve / reject / defer / decide-again and the UI
  supersede actions look the review up with `reviews.lookup_review` — every slot read non-blocking
  and typed, any damaged slot (a dangling link, a FIFO, a directory) 409 with its category, never a
  404.
- **Review round 2** (2026-11-14, external review of `90c48e0` + the uncommitted slice):
  - **Durable removal.** `durable_write.remove` / `remove_dir` unlink (or rmdir), then fsync the
    directory; a failed sync after the removal is `UncertainCompletion`. Every authority removal
    uses them (review transitions, identity journals, Item pages, tag-link resolving and deletion
    intents); the write guard covers removals too (`os.remove` / `os.rmdir` / `shutil.rmtree` at
    run time, `unlink` / `rmdir` / `rmtree` statically).
  - **Decision transitions.** Approve / reject and reopen move a record in one order: (1) the
    audit entry, create-only; (2) the destination record; (3) the source removed durably. So no
    change is without its audit. An interruption after (2) leaves the id in `pending/` and a
    terminal directory: a reopen's pending copy names the decision it reopened (`reopened_from`:
    status + generation), which tells an interrupted reopen (the decision is removed — a revoked
    approval never executes) from an interrupted decision (the pending copy is removed).
    `reviews.finish_interrupted` settles it on the next human action on that review (decide,
    defer, reopen, decide again, withdraw); until then **Apply's preflight refuses** (`interrupted`
    in its problems, with the advice to repeat the action — not to restore from backup). An
    interruption between (1) and (2) leaves an audit entry for an attempt that changed no record;
    it stays as evidence (its generation matches no record).
  - **Per-decision audit entries.** Every terminal decision carries a `decision_id` (an approval's
    is also its `approval_id`); its audit entry is `<rid>-<decision>-<decision_id>.json`, created
    create-only, so approve → reopen → approve keeps every entry. A decision's *generation* is its
    `decision_id`, else its `approval_id`, else a digest of the record (older decisions). Reopen
    entries stay sequence-numbered (ADR-0045), carry `prior_generation`, and a retry reuses the
    entry for its generation. Existing `<rid>-approved.json` / `-rejected.json` entries are kept.
  - **One review read model.** `review_read` reads every record through `reviews.read_review_record`
    (non-blocking, one schema): the queue counts damage as `parse_errors` / `schema_errors`;
    `GET /reviews/{id}`, `/ui/reviews/{id}` and reopen answer a damaged record with a categorical
    409 (never 404); bulk reject / defer skip it with its category.
  - **Symbolic links refused.** `authority_read.read_file` opens with `O_NOFOLLOW`: an authority
    file that is a link is `unreadable` (`symlink` / `dangling_symlink`) in every family, whatever
    its target. Linked directories on the way are still followed.
- **Tests:** `tests/test_review_record_states.py`.

**As implemented (slice 3c — policies, journals and the `db/*.json` records).** Every remaining
family reads through `authority_read` (non-blocking — a FIFO can't hang a reader holding the write
lock — and never through a link at the file itself). The gaps closed were the same in every family:
`path.exists()` took a dangling link for absence, a blocking `read_text` could hang on a FIFO, and
`is_dir()` + `glob` turned a directory that can't be listed into an empty inventory.

- **Policies** (`app/backend/policy.py`: `read_policy`, `read_policy_text`,
  `PolicyUnavailable`):
  - **Absence means defaults** (the domain allows it).
  - A policy that exists but is unreadable, invalid UTF-8, unparseable or not a mapping raises
    `PolicyUnavailable`, and the operation that depends on it refuses (decision 4.4: a policy is a
    shared input). The API answers 409 `policy_<state>` with the file name, never its contents.
  - **Review round 1: strict parsing and field validation.** Policies parse with
    `load_yaml(..., strict=True)`, which refuses (`corrupt` / `invalid_yaml`) instead of guessing:
    a tab in the indentation, a line deeper than its block or dedented to no open block, a mapping
    line without `key:`, an empty or duplicate key, a sequence item in a mapping (or the reverse),
    a flow value (`[...]`, `{...}`), an unterminated quote, or content after the top-level block.
    The golden eval files keep the lenient parse. Each family then validates the **present**
    fields it consumes (`check_retrieval`, `check_retention`, `check_review`, `check_citation`;
    `corrupt` / `invalid_field:<dotted.name>`): integers are never booleans, thresholds are ≥ 0
    (`rrf_k` ≥ 1, `merge_candidates.max_items_per_key` ≥ 2), retention's switches are booleans,
    route rules are non-empty lists of known modes, the abstention text is a non-empty string. A
    missing field keeps its default and unknown keys are kept. This **reverses** the earlier
    silent fallbacks: an unknown route mode is no longer filtered out, and a negative cap, `rrf_k`
    < 1 or a negative `item_type_boost` no longer becomes the default. Clamping `item_type_boost`
    to its RRF-derived ceiling (ADR-0062) is unchanged — it bounds a valid value.
  - Covered:
    - `retrieval.yaml`: `/search` and `/query` refuse;
    - `retention.yaml`: the stale check refuses before recording a job;
    - `review.yaml`: lint skips merge candidates with a high `policy_<state>` finding;
    - `citation.yaml`: the abstention text;
    - the blocked-tag list `blocked_tags.txt`: block and unblock refuse before their first write;
      extraction skips each document with `policy_<state>`; lint and tag-link repair or
      validation refuse with the category.
  - Invalid UTF-8 in the blocked-tag list is now `corrupt`. It used to be decoded with
    replacement characters, and the next block rewrote the list from those.
- **Identity journal** (`reviews/in_flight/`):
  - `read` and `is_open` treat only a verified absence as closed: a dangling link or an entry
    that can't be confirmed is an open, unreadable operation (`JournalUnreadable` now carries
    `state` / `error`).
  - A journal directory that can't be listed is one unknown operation, `UNLISTED`
    (`"reviews/in_flight"`), touching every node, so every competing tag action and every
    extraction of a linked document is refused. No path is ever built from it.
- **Producer journal and publication queue** (`db/producer_journal.json`,
  `db/publication_pending.json`):
  - An entry that can't be read is `JournalCorrupt` / `QueueCorrupt` with reason
    `unreadable:<category>`, never an empty journal or queue.
  - Repair can't copy what it can't read. It keeps the entry itself under the `*.damaged-*` name
    as a hard link (`durable_write.preserve_entry`: create-only, directory fsynced, the original
    left in place until the replacement), so a link stays a link and an unreadable file keeps
    its bytes.
- **Explicit recovery contracts (decision 4.5), conforming.** Each keeps its written behaviour for
  bad contents, but none replaces an entry it can't read:
  - the vault revision: malformed contents get a new epoch; an entry that can't be read raises
    `RevisionUnavailable`;
  - Apply authorizations: malformed contents are preserved and emptied; an entry that can't be
    read raises `StoreUnavailable`;
  - contradiction estimates: losing an estimate only refuses a run;
  - the published tag-label snapshot `db/tag_labels.json`: unknown means every Source page is
    refreshed;
  - tagging-history provenance `_tracking.json`: unknown only lowers certainty. The fresh-vault
    initialization and the migration now write it only over a verified absence, never over a
    damaged entry.
- **Tag ledgers** (`reviews/tag_links/`):
  - A ledger that can't be read is `LedgerCorrupt` (`unreadable (<category>)`).
  - A ledger directory that can't be listed is reported as one damaged ledger, `UNLISTED_LEDGERS`:
    - `kept_anywhere` answers "unknown", so no tag counts as deletable;
    - merge preflights refuse;
    - `ledger_keeps` raises, so publication leaves every tag page pending (`ledger_corrupt`);
    - tag-link validation reports it, and repair refuses.
  - Unfinished "use X" records (`resolving/`) and deletion intents (`deleting/`) whose directory
    can't be listed raise `LedgerCorrupt` — including (review round 1) a file in place of a
    document's `resolving/<source_id>/` folder, in the whole-vault and the per-document scan.
    Entries not named by a canonical id are not ours and are ignored. A damaged record still
    counts (`chosen: None` / `reason: None`).
  - **Tag-link repair checks before it writes** (review round 1, `tag_links.repair_preflight`):
    the blocked-tag list, the ledger, resolving and deletion folders and the audit log are checked
    under the **same** write-lock hold as the tagging migration and the repair. The hold is
    `changes=False`, and the revision advances only after the check passes, so a refusal writes
    nothing — no tracking record, no migrated ledger, no publication intent — and doesn't advance
    the revision. `--init-fresh-tracking` advances the revision only right before its write.
- **Audit scans** that prove a step done or a deletion recorded (`tag_links._audited`,
  `_deletion_audited`, and supersede's `read_audit`, `scan_review_audits` and repair audit)
  read non-blocking:
  - a log that can't be listed raises, so a step is never redone or taken as done on a log no one
    could see;
  - a damaged entry is present, never absent: a repair audit is never written over it.
  - Tagging-history evidence stays lenient — it only lowers certainty — and (review round 1)
    carries **whether the scan was complete** (`tag_history.AuditEvidence`). A verified absent
    log is complete (a fresh vault has none). A log that can't be listed, or a tag audit entry that
    can't be read or isn't a record, makes the evidence incomplete, and that reaches
    `classify`, `backfill` and `mark_decision`: no document is shown "never tagged", and no
    first-decision time is inferred, from a scan that missed events.
- **Not changed:** SQLite and lock files (decision 1 "not covered"); projections (vector-index
  metadata, eval reports). `security.yaml` is not read by code.
- **Tests:** `tests/test_authority_family_states.py`:
  - every damage class per family, the recovery contracts, and the unlistable directories;
  - repair keeping an unreadable entry;
  - review round 1: text outside the subset and wrong-typed fields per family, a file in place of
    a resolving folder, repair and fresh-tracking refusals that leave authority and the revision
    unchanged, and incomplete audit evidence under `from_start: true`;
  - a FIFO probe in a subprocess with a deadline, covering every reader.

## Part B — bounded extraction (finding 3)

Today `_extract_one` (`app/workers/extract.py`) runs the untrusted parse (`_dispatch`: the
pypdf / docx / bs4 / pandas extractors and the ZIP checks) under `_time_limit`, which uses `SIGALRM`
and silently does nothing off the main thread. Assembly, staging (`normalized/.staging/<id>/`,
outside the lock) and installation (`os.replace` under the write lock) follow in the same process;
`prior_success` already keeps a prior successful extraction when a re-extraction fails.

### Decision 5 — the process boundary and the deadline (grill Q5)

- **Parse-only child.** Each document's parse runs in **one child process**. The child receives the
  path of that one raw file (already confined, size-checked and sha-verified by the parent) — it
  necessarily reads a vault file — but **no configured vault root, no output paths and no write
  responsibilities**. It returns the parse result; nothing else. A process boundary alone does **not**
  enforce read-only filesystem access; that is not claimed.
- **Inside the bounded child work:** ZIP inspection (`_check_zip_bomb`) and every parser-related
  preflight, so none of them can bypass the deadline.
- **In the parent:** assembly, staging, installation, the manifest update, the extraction log and
  last-good preservation — unchanged in place. A killed child cannot leave partial vault state.
- **`spawn`, never `fork`** (the API process holds threads and CUDA). **Minimal child bootstrap:** the
  child entry point is a small dedicated module that imports only the extractor modules; `spawn`
  re-importing the launching module must not initialise the API, CUDA / embedding models, databases,
  locks or jobs.
- **The deadline is `EXTRACT_TIMEOUT_S` of wall-clock time measured by the parent from launch
  through complete result receipt** — start-up, imports, parsing and serialization all count. The
  termination grace is a **separate, bounded cleanup allowance**, not part of the parse budget.
- **Process group before parsing.** The child establishes its own process group and confirms it in a
  **start-up handshake** before any parsing begins, so a cancellation cannot race group creation and
  the parent can never signal its own group. Termination signals the child's group: SIGTERM, the
  grace, then SIGKILL, then reap.
- **IPC.** The parent **drains the result channel while the child runs** (waiting for exit before
  reading can deadlock on a full pipe) and **enforces the byte limit during receipt**. The format is
  **schema-validated** (e.g. JSON with a versioned schema), **never pickle**.
- **Cleanup on every exit path** — success, timeout, invalid output, parent exception, cancellation:
  remaining helpers in the group are terminated, IPC is closed, the child is reaped. **A successful
  result does not leave descendants running.**
- **One path for CLI and API.** `SIGALRM` / `_time_limit` is removed; behaviour no longer depends on
  which thread runs the job.
- **Error categories.** Existing reasons are **preserved** as the child reports them — missing
  optional dependency (`MissingExtractionDependency`), malformed document, `decompression_bomb`,
  `invalid_archive`, `unsupported`, … New categories are **reserved for process and protocol
  failures**: `timeout` (deadline passed), `extractor_crashed` (child exited or was killed without a
  result), `extractor_output_invalid` (over the byte limit, or failing the schema), and
  `extractor_unavailable` (the child could not be started or failed its handshake).

### Decision 6 — failed attempts, retry classification and last-good (grill Q6)

Today a failed re-extraction keeps the prior outputs, manifest and log (`preserved_prior`); a
first-time failure sets `ingestion_status: "error"`, its reason lives only in the extraction log, and
`_already_extracted` is False for `error`, so the document is re-extracted on **every** run. Decided:

- **Last-good preservation is unchanged.** Any failure on a re-extraction — process and protocol
  failures included — keeps the prior manifest, outputs and extraction log; staging is discarded.
- **Failed attempts are stored separately from last-good state**: a per-document **attempt record**
  (the latest failed attempt: reason, category, time, fingerprint) beside — never inside — the
  successful extraction log, so a failure after a success neither disappears nor overwrites the good
  log. (Location is an implementation detail; it is derived state — losing it causes a retry, the safe
  direction.)
- **Retry classification:**
  - **Always retried** — environmental or ambiguous failures, whose outcome can change without the
    fingerprint changing (CPU contention, memory pressure, machine load): `timeout`,
    `extractor_crashed` (crash or signal, e.g. an OOM kill), `extractor_unavailable` (start-up or
    handshake failure), missing optional dependency, I/O errors, and **incomplete IPC** (truncated
    transport, unexplained invalid output).
  - **Cached** (skipped as `skipped_known_failure` while the fingerprint is unchanged) — only
    **explicit deterministic rejections**: `oversize`, decompression-limit rejection
    (`decompression_bomb`), `invalid_archive`, clearly classified malformed-document errors, and an
    output that **exceeds a fixed output cap** (a complete, well-formed result that is too large).
- **The fingerprint covers every relevant bound**: raw sha256, the file-size limit, the ZIP limits
  (entries, ratio), the output and protocol limits, the extraction configuration (chunking targets,
  timeout), and the implementation and dependency versions (`EXTRACT_CODE_VERSION`, extractor tool
  and library versions). Timeout plus tool version alone is insufficient. **Missing or unreadable
  fingerprint information means retry.**
- **`force` is an unconditional retry**, still subject to the safety checks (confinement, size,
  checksum).
- **Visible, and the two failure situations are distinguished.** The document screen and lint show
  either **"usable prior extraction retained; latest attempt failed: <reason>"** or **"no usable
  extraction: <reason>"**. Only a cached deterministic failure says it **awaits changed inputs or
  force**; every other failure says it **retries on the next run**.
- **No new lifecycle status.** If repeated timeouts become operationally costly, a **bounded
  backoff** is decided separately — they are never treated as permanent known failures.

### Decision 7 — locks, orphaned children, stale jobs and cancellation (grill Q7; points 4–5 completed with decision 8)

Today the extract producer guard (`db/.run-extract.lock`) and the vault lock (`db/.vault.lock`,
shared, held by every `running` job) are `flock`s the OS releases when the holder dies; `ad6391a`
records a Ctrl-C / `SystemExit` as `failed`, but a **hard kill** (SIGKILL, OOM, a reboot) leaves the
job row `running` forever.

1. **Descriptor isolation.** The child inherits **no** vault-lock, producer-guard or database
   descriptors (standard streams and the multiprocessing runtime's own descriptors may be needed).
   Otherwise an orphaned child keeps `db/.vault.lock` shared — backup and restore refuse until it
   dies — or keeps the extract guard. **Proved by a subprocess test** that the locks are released and
   re-acquirable while a child is alive, not assumed from `spawn` defaults.
2. **Orphan termination independent of parsing.** A parser stuck in C code or holding the GIL can
   stop any Python-level monitor thread or signal handler in the child, so EOF monitoring of a control
   pipe and the child's own deadline must be enforced by **an independently enforceable watchdog** —
   a mechanism that does not depend on the parsing interpreter making progress (for example a separate
   minimal watchdog process per child that holds the control pipe's read end and kills the child's
   group on EOF or on deadline). **The control pipe's writer end is never inherited** by the child or
   its helpers (it would keep the pipe open and prevent EOF). **Test:** kill the parent during a
   stalled parse; the child group is gone within the deadline + grace.
3. **Stale `running` jobs — extraction only in this slice.** After acquiring the **outermost** extract
   guard and **before creating the new job**, every extract job row still `running` is marked
   `failed` with reason **`interrupted`** — recording lost execution, **not** a successful rollback —
   and the new run's result reports them. The reconciliation helper is **reusable**, but other
   producers are not switched to it here: they need a separate audit of guard ownership and job-type
   mappings first.
4. **Staging cleanup** — decided in decision 8: at run start, after roll-forward recovery, a staging
   directory is removed **only** when a complete, readable inventory of install journals proves that
   none refers to it.
5. **Shutdown and cancellation.** Cooperative cleanup on shutdown or cancel; lifespan cleanup alone is
   insufficient for an active synchronous worker, so **children stay registered until their cleanup
   completes** and **the run guard is not released while that worker can still install outputs**.
   Cancellation is honoured **before an install starts or between documents**; once an install has
   begun it finishes or leaves journal-recoverable state, still holding the guard. `interrupted` is
   recorded when persistence remains possible; a hard death is reconciled on the next run (point 3)
   and recovered through the install journal (decision 8).

**As implemented (slice 6a — decisions 5, 7.1 and 7.2).** Slice 6 is split: **6a** the process
boundary (this), **6b** attempt records and retry classification (decision 6), **6c** extraction-only
stale-job reconciliation and cancellation (decisions 7.3 and 7.5).

- **Modules.** `extract_parse.py` holds the parse (format dispatch, the ZIP checks, the lazily
  imported extractors), moved out of `extract.py` unchanged; `extract_child.py` is the child's
  entry point; `extract_watchdog.py` the watchdog; `extract_runner.parse` the parent side, the one
  parse path for the CLI and the API (`extract._parse`). `_time_limit` / `SIGALRM` are gone.
- **Spawn.** The child is a fresh interpreter (`python -I extract_child.py`, an exec through
  `subprocess`, not `multiprocessing` — no re-import of the launching module at all) with
  `process_group=0`, `close_fds`, working directory `/` and an environment reduced to `PATH`,
  locale, `TZ` and `TMPDIR` (no API keys, no vault configuration). Its bootstrap imports the
  standard library, `extract_parse`, `chunking` and the one extractor the format needs (proved by a
  test listing the child's modules). The venv's `site` start-up still runs (it imports NVIDIA's
  `cuda` namespace shim, which initialises nothing).
- **Handshake and request.** The child moves fd 1 to a private result descriptor and points fd 1
  at stderr (a parser's print can't corrupt the channel), refuses to run unless it leads its own
  group, and sends `hello {pid, pgid}`. The parent requires `pgid == pid` and never its own group,
  and until then signals only the child's pid. Then the watchdog is armed (and confirms with one
  byte), and only then is the request (path, source id, output cap) sent: a child whose parent died
  earlier reads EOF and exits without parsing.
- **Watchdog.** `python -I -S extract_watchdog.py <fd> <pgid> <budget> <grace>`, standard library
  only, inheriting only the control pipe's read end (`pass_fds`; the parent holds the only writer,
  which is non-inheritable). EOF → it terminates the child's group (SIGTERM, grace, SIGKILL); its
  budget (the deadline remaining + the grace) is the deadline backstop; `x` stands it down. It
  refuses its own group, ignores SIGINT / SIGHUP, and a SIGTERM to it terminates the child's group
  first.
- **Receipt.** `poll` (not `select`: descriptors may exceed 1024), draining while the child runs,
  bounded to the cap + 64 KiB. One newline-terminated ASCII JSON message, protocol version 1,
  schema-checked field by field (element kinds, levels, pages, table names by the install
  journal's rule, status `extracted` / `partial`, metadata strings); a child-reported reason must
  be one the child may report. **The fixed output cap is 256 MiB**, enforced exactly by the child
  (`output_too_large`, a complete result) and with the slack by the parent
  (`extractor_output_invalid`).
- **Reasons.** `timeout` (the deadline — `EXTRACT_TIMEOUT_S`, now required positive — passed,
  start-up included); `extractor_crashed` (exit or signal with no or a truncated result);
  `extractor_output_invalid` (not JSON — nesting too deep to decode included — more than one
  message, off-schema, an unknown or non-string reason, over the receipt bound);
  `extractor_unavailable` (could not start, no or a failed handshake — an undecodable hello
  included — the watchdog failed to start). Every child-controlled value is type-checked before
  use, so no malformed message escapes as an unclassified failure. The child's own `missing_dependency`, `decompression_bomb`,
  `invalid_archive`, `unsupported` are preserved, and an unclassified parse failure keeps its
  "Type: message" with no reason (slice 6b retries it as ambiguous). Every failure goes through
  the existing paths: last-good kept on a re-extraction, staging discarded.
- **Cleanup**, on every path (a parent exception included): the group gets SIGTERM, the leader
  `grace` (5 s) to exit (checked with `waitid(WNOWAIT)`, not reaping), then SIGKILL — on success
  too, so no helper survives; then the watchdog is stood down and reaped; the child is reaped last,
  so its pid (the group id) can't be reused while the watchdog could still signal it.
- **Not claimed:** a helper that leaves the group (`setsid`) escapes the group kill; a process
  boundary is not a read-only file system.
- **Tests** (`tests/test_extract_child.py`, `tests/extract_child_stub.py` as the misbehaving
  child): the real child equal to the in-process parse for every format; the child's modules,
  environment, working directory, group and descriptors (no lock, guard or database, with all three
  held by the parent); the deadline off the main thread, start-up counting, escalation to SIGKILL by
  the parent alone; crash, truncation, invalid output (an unhashable reason and JSON nested past
  the decoder's limit included, in the result and in the hello; a decoder sweep putting every JSON
  type in every field), the receipt bound, the child's cap, a result
  larger than the pipe, handshake failures, a missing interpreter; the child's reasons preserved;
  no descendant after success or a parent exception; the watchdog's EOF, deadline, stand-down,
  interrupt and SIGTERM handling and own-group refusal; **a SIGKILLed parent during a stalled parse
  whose child ignores SIGTERM: the vault lock and the extract guard re-acquirable while the child is
  still alive, then the child gone within the grace**; the API route timing out a stalled parse on
  its worker thread; process failures on re-extraction keeping the last-good generation.

**As implemented (slice 6c — decisions 7.3 and 7.5).**

- **The guard.** `extract_sources` now takes the extract run guard itself for the whole run
  (`run_guard` is re-entrant per thread, so the API route and the CLI, which already hold it,
  keep it; a direct caller — an eval script, a test — now gets it too). A second run refuses with
  `ProducerBusy` before touching any job row.
- **Stale jobs (7.3).** Under the guard and before inserting its own job, the run calls
  `db.reconcile_interrupted(conn, "extract", …)`: every extract job still `running` becomes
  `failed` with `error_message` "interrupted (the run ended without recording a result: …)",
  `finished_at` the run's start and `metadata.reconciled = {reason: "interrupted", by_job, at}` —
  lost execution, never a rollback (decision 8's journal recovery completes any install it left).
  A job this process still has running (a nested run on this thread, known by its held vault lock)
  is never touched. The run's summary lists them (`interrupted_jobs`), and the CLI prints them. The
  helper is reusable; no other producer calls it (deferred, see the table).
- **Cancellation (7.5).** Each run registers a `CancelToken` (an event plus a non-inheritable
  self-pipe) under its job id. `extract.cancel(job_id=None, reason=…)` sets it — from any thread, or
  a signal handler (the registry lock is re-entrant). Honoured: **between documents** (a document
  not reached, even one that would be skipped, is counted in `not_attempted`); **during a parse**
  (the self-pipe is polled with the child's descriptors, so the wait wakes at once and raises
  `Cancelled` after the usual cleanup — the child and its group are gone; nothing is recorded for
  the document: no log, no attempt record, no manifest change); **before an install starts** —
  the install boundary is the **write lock's acquisition**: `_commit` checks the token again once
  it holds the lock, before any publication intent, journal or document write, so a cancel that
  arrives while the run waits for another writer's lock still installs nothing (the staged
  generation is discarded, no document failure recorded). **Never inside an install**: one past
  that boundary (recovery at run start included) finishes, and the run stops afterwards.
- **The settle point (review round 1).** Whether a run was cancelled is decided once, at its
  **settle point** — after the end publication refresh (which runs even when cancelled: it
  completes what the run installed), just before the outcome is recorded — atomically with
  `cancel` under the registry lock: any cancel acknowledged up to there, including one during the
  final document's install or the end refresh, makes the run cancelled (what it installed stays;
  `not_attempted` is then 0); from there on `cancel` refuses the run (it returns no job id) though
  it is still active and waited for. A cancelled run records its job `failed` with `error_message`
  "interrupted (<reason>)", and returns `cancelled: true`, `cancel_reason`, `not_attempted`.
- **Registration and the guard.** A run stays registered until its `finally` — after its child's
  cleanup and its last install — and only then does its own `with` release the guard; nothing else
  releases it. `extract.wait_idle(timeout)` waits for no run to be active.
- **Entry points.** `POST /jobs/extract/cancel` (not a run: no guard) returns
  `{cancelled: [job ids], status: "cancelling" | "not_running"}`; the running request then answers
  with its cancelled summary. The CLI's first Ctrl-C or SIGTERM cancels ("Stopping after the
  current step …"; exit code 130); one while the run is past its settle point only says "The run
  is finishing …"; a second, or one with no run registered, raises `KeyboardInterrupt` at once (an install it cuts is completed by the next run's recovery).
  **Server shutdown:** the lifespan chains the server's SIGINT / SIGTERM handlers (main thread
  only) so a shutdown signal first cancels every run — the in-flight request reaches a safe point
  instead of being drained for the whole run or cut off — and the lifespan's exit cancels again
  and waits — for every active run, settled ones included — up to `SHUTDOWN_WAIT_S` (30 s) for each run to finish its cleanup, warning if one is
  still active.
- **An interrupted cleanup (a 6a fix).** An interrupt landing *during* the termination grace (the
  second Ctrl-C) used to stand the watchdog down while a child ignoring SIGTERM still lived. Now
  the runner kills the group at once and closes the control pipe **without** the stand-down byte —
  its EOF makes the watchdog finish the kill even if the process exits — and the interrupt
  propagates without waiting.
- **Tests** (`tests/test_extraction_cancel.py`; one in `tests/test_extract_child.py`): a stale row
  reconciled once, other producers' rows and terminal rows untouched, a live nested job never
  reconciled, nothing reconciled without the guard; a SIGKILLed run reconciled by the next one, its
  orphaned child terminated by the watchdog and its document extracted; cancelling a parse in
  progress (child gone, nothing recorded, guard released, a later run extracts both documents); the
  guard and the registration held through a SIGTERM-ignoring child's grace; a cancel before an
  install (nothing installed, staging empty); an install that has begun finishing and published
  while the next document waits; a cancel during start-up recovery reaching no document; the API
  endpoint; a server shutdown cancelling and waiting; the chained signal handler; the CLI's first
  Ctrl-C (exit 130, job `interrupted (interrupted by SIGINT)`) and a second one aborting at once
  with the child still cleaned up; an interrupt during termination never standing the watchdog
  down.

**As implemented (slice 6b — decision 6).** `app/workers/extract_attempts.py`.

- **The attempt record** is `normalized/extraction_attempts/<source_id>.json` (durably written;
  derived state — beside, never inside, the extraction log): the latest failed attempt's reason,
  message, retry category, time, attempt id, whether the prior outputs were retained and whether
  they were a *usable* prior extraction (their install journal allowing reads), the generation and
  the manifest's `extracted_at` it was taken against, and the fingerprint with its inputs. Written
  after a failed attempt (the commit, for a first-time failure, having succeeded); removed after a
  success. **Binding instead of ordering:** every extraction log now carries the `attempt_id`
  that wrote it — a unique id per generation — and a record is current only while that id (the
  retained prior's, or the failure log this attempt wrote), the manifest's `extracted_at` and the
  retained-prior flag all still match. A later success writes a new log with a new id, so it
  retires the record even within the same second (timestamps have second resolution) and even if
  the removal failed; a restored manifest retires it too. A log written before 6b has no id (the
  binding is then None, retired by the first 6b success). An
  absent, unreadable, corrupt (bad JSON, off-schema, a `cached` category with a non-cached reason
  or no fingerprint) or superseded record reads as no record: a retry. A record that can't be
  written is a run warning (`attempt_record_unwritten`), and the failure is reported as retried.
- **Classification.** Cached: `oversize`, `decompression_bomb`, `invalid_archive`,
  `output_too_large` — and only with a complete fingerprint. No extractor raises a classified
  malformed-document reason today, so nothing else is cached; an unclassified parse failure
  (no reason) is ambiguous and retried, as are `timeout`, `extractor_crashed`,
  `extractor_unavailable`, `extractor_output_invalid`, `missing_dependency`, `unsupported`,
  `checksum_mismatch`, `missing_raw_file`, `path_escape` and I/O errors. Commit-stage failures
  (a manifest damaged since the scan, an install left to its journal) are not attempt failures:
  the journal and the run's errors report them, and no record is written.
- **The fingerprint** (sha256 of canonical JSON, taken **before** the attempt): the manifest's
  sha256 with the raw file's current size and mtime (the unchanged skip's cheap drift gate — no
  re-hash per run; it relies on raw immutability and is **not** a fresh checksum verification,
  which every actual attempt still performs), its suffix; the file-size limit; the ZIP entry / size / ratio limits; the
  output cap, the receipt envelope and the protocol version; the chunk targets and the timeout;
  `EXTRACT_CODE_VERSION`, the interpreter version and the versions of the format's extractor
  distributions (`pypdf`, `python-docx`, `beautifulsoup4`, `pandas` / `openpyxl`). Any of it
  undeterminable (no confined raw file, no stat, no recorded checksum, a distribution not
  installed) → no fingerprint → retried.
- **The run consults the current failure before the unchanged shortcut** (review round 1): a
  failed re-extraction keeps the prior outputs, which would otherwise read as unchanged and never
  retry. With a current record the document is attempted — unless it is skipped as
  `skipped_known_failure` (with `known_failures` details): not forced, its install journal usable
  (an unrecoverable install is always retried, decision 8), the record cached and the fingerprint
  computed now equal to the record's. Without one, the unchanged shortcut applies as before.
  A skipped known failure is not counted as an error of the run. `force` ignores the cache. Each
  failure in `error_details` carries its category (`retry`: `cached` / `retry`), and the CLI says
  which failures await changed inputs or `--force`.
- **Visible.** The document screen shows a banner — "No usable extraction" or "Latest extraction
  attempt failed" — with the shared wording "no usable extraction: <reason> — …", "no usable
  extraction (earlier outputs kept, but their install is <state>); latest attempt failed: <reason>
  — …" (retained bytes behind an unrecoverable or damaged install journal, read live: kept, not
  usable) or "usable prior extraction retained; latest attempt failed: <reason> — …", ending in
  "awaits changed inputs or a forced re-extraction" (cached) or "retries on the next run". Lint
  reports the same text, report-only: `extraction_failed` (medium, no usable extraction — with
  `data.situation` `no_usable` or `retained_unusable`) and `extraction_attempt_failed` (low, a
  usable prior retained). A failed document without a current record (failed before 6b, or its
  record lost) is described from its extraction log and retries. The wording reflects the
  category the attempt was recorded with; a bound changed since then retries on the next run.
- **Tests** (`tests/test_extraction_attempts.py`): every reason's category; the fingerprint
  changing with each bound and missing with each undeterminable input; cached rejections skipped
  without launching a child until `force`, a changed limit, configuration, raw file or code
  version; every other reason retried on every run; oversize cached until the limit is raised,
  then extracted with the record cleared; a real-child invalid archive and ZIP bomb cached end to
  end; a failure after a success recorded beside the untouched good log and outputs; superseded
  and damaged records retried and replaced; an unwritable record a warning; an unrecoverable
  journal never skipped; the screen and lint wording for both situations and both futures; the
  log-only fallback; lint not failing on them. Review round 1: a successful extraction, then a
  forced attempt timing out, then ordinary runs retrying it (never `skipped_unchanged`) until it
  succeeds; a cached failed re-extraction skipped while its fingerprint matches and retried when a
  limit, the configuration or the code version changes; a same-second success with the record's
  removal failing still retiring it (the generation binding); kept outputs behind an
  unrecoverable install reported as no usable extraction, their bytes and log untouched, and
  retried.

### Decision 8 — roll-forward install journal for extraction (grill Q8)

The ADR-0068 producer journal covers only `extract_claims`, `extract_items` and `generate_synthesis`
(`app/backend/producer_journal.py`); it does **not** cover extraction. `_install_outputs`
(`app/workers/extract.py`) performs several separate renames (Markdown, chunks, old tables into
staging, new tables), then the extraction log is truncate-written, then `save_manifest`, then
publication. A parent death between any two leaves mixed generations, or the only copy of the old
tables inside staging. Decided: **roll forward** (the old Markdown and chunks are overwritten by
`os.replace`, so there is nothing to roll back to). Rollback with a full copy of the old generation,
and per-document generation directories behind one pointer switch, were considered and not chosen.

**The journal.** Before the first rename, under the write lock, a per-document **install journal**
(durable helper; e.g. `normalized/.install/<source_id>.json`) records: the attempt id; the
**source checksum and the relevant extraction configuration** it is bound to; the **exact output
inventory** — each Markdown, chunk and table file with size and sha256, including an empty table set
and the obsolete table files to remove; the extraction log to write; and the **extraction-owned**
manifest fields to apply. The staged generation is complete and durable (files and directories
fsynced) before the journal is written; publication intent is queued first, as today (ADR-0068).

**Stages: install completion is separate from publication.**

1. `installing` — renames in progress. The document's normalized outputs are **not readable** by
   consumers.
2. `installed` — persisted **only after** every output, the extraction log and the manifest
   extraction fields are **verified and durable**. Readers **may** use this complete generation.
   Publication failures are covered by the publication queue as for any other change.
3. closed — the journal is removed **after publication succeeds**.

(An "every open journal blocks readers" rule was rejected: publication itself must read the document
while its journal is open, which would be a cycle.)

**Each step verifies bytes, not presence.** Before a rename, the staged file's size and sha256 must
match the journal; for an already-installed file, the installed bytes must match. Tables follow the
exact inventory — an empty set is a valid generation, and obsolete files are removed. Old tables move
to a **journal-named holding directory** (not loose in staging), deleted only once the generation is
`installed`. **Every affected directory is fsynced after its renames**, including moves into holding
storage.

**Recovery rolls forward.** At extract run start, after the outermost guard (decision 7.3) and under
vault lock → write lock, every open journal is completed. Recovery **re-reads and validates the
current manifest** (decision 2), **keeps its current governance and provenance**, and applies **only
extraction-owned fields**. A journal whose bound checksum or extraction configuration no longer
matches is **incompatible** and is refused (not applied), and the document follows the unrecoverable
transition below.

**Unrecoverable install — an explicit transition.** When the generation cannot be completed (a staged
file missing and the installed bytes not matching, or an incompatible journal), the journal is marked
**`unrecoverable`**: the journal and every remaining artifact (holding directory, staging) are
**preserved**, consumers **stay blocked** — the mixed files are **never** silently marked usable — and
the document is reported as "no usable extraction (interrupted install)". **A fresh extraction attempt
may supersede it**: a new successful attempt stages, journals and installs a complete new generation
over it, after which the superseded journal and its artifacts are retired. This is what "always
retried" (decision 6) means for this state.

**A damaged journal is unknown recovery state.** An unreadable or corrupt journal (decision 4) is
reported; consumption of the affected document and **all staging cleanup are blocked** — an
incomplete journal inventory never proves that a staging directory is unreferenced. **Every path in a
journal is validated** (confined to the document's own normalized, staging and holding locations)
before any rename or deletion.

**Consistent reads.** A consumer's journal check and its read must not race an install. A consumer
captures the journal state, the manifest and the normalized bytes it needs **under the write lock**,
or through an equivalent consistent snapshot, and only then releases the lock; **model calls stay
outside the lock** (ADR-0068). A document whose journal is `installing`, `unrecoverable` or damaged is
skipped with `extraction_install_incomplete` by every consumer — enrich, claims, items, synthesis
inputs, keyword and vector reindex — and is excluded from search and `/query` evidence (fail closed,
decision 2). `validate_normalized` fails while such a journal exists; lint names the fix ("run
extraction").

**Tests cover every durable transition**, not an assumed fixed step count: a crash or injected
failure after each one, **recovery interrupted again**, **directory-sync failures**, empty and
table-changing generations (tables added, removed, set emptied), incompatible and damaged journals,
the unrecoverable transition superseded by a fresh attempt, and **consumer reads racing an install**.

**Sub-slices.** Slice 5 is split (decision 9): **5a** — the journal, install, recovery, the
unrecoverable transition and its supersession, staging cleanup (7.4), and `validate_normalized` /
lint reporting; **5b** — consistent consumer reads (enrich, claims, items, synthesis inputs, keyword
and vector reindex, search and `/query` exclusion, the document screen's two failure situations)
with the race tests. (Between 5a and 5b a consumer could still read a document whose journal was
open — the pre-slice exposure, visible to `validate_normalized` and lint; 5b closed it.)

**As implemented (slice 5a — `app/backend/extraction_install.py`, `app/workers/extract.py`).**

- **Layout.** Staging per attempt, `normalized/.staging/<id>-<attempt_id>/` (`att_<12 hex>`; a new
  attempt never collides with a preserved one); the journal `normalized/.install/<id>.json`; holding
  `normalized/.install/<id>-<attempt_id>.holding/`. No path is read from a journal: every path
  derives from the validated id, attempt id and table names (`[0-9A-Za-z][0-9A-Za-z._-]*`).
- **Staging** writes every file through `durable_write` (create-only, fsynced, directories
  established) and returns the inventory (size + sha256 per file, the table set possibly empty).
- **Journal** (version 1): attempt id, stage, the unrecoverable reason, `source_sha256` (agreeing
  with the id), the configuration it is bound to (`extract_code_version`, `target_chars`,
  `max_chars`), the publication intent, the inventory, the obsolete tables, the log and its sha256,
  the extraction-owned manifest fields, the superseded attempts, times. Exactly these fields; any
  other content reads as `corrupt` (`invalid_field:<name>`).
- **Install order**, under the write lock after the intent: journal (create-only, or replacing an
  `unrecoverable` / `installed` one it supersedes) → **verify every output first** (the staged
  file's bytes, or the installed bytes when it already moved) so a generation that can't complete is
  refused with nothing moved → table entries outside the inventory into holding → verified renames
  (`durable_write.move`: replace, then fsync both directories) → every affected directory fsynced
  again → the log (written, or its barrier repeated) → the manifest's extraction fields (onto the
  current manifest; governance and provenance kept) → full verification (bytes, the exact table set,
  log, manifest fields) → `installed` → holding and staging (own and superseded) retired →
  publication → journal closed only when the document's publication succeeded (else it stays
  `installed`, usable, closed by the next run's recovery).
- **Recovery** at every extraction run start, under the write lock, before any attempt: `installing`
  journals are completed (another configuration → `unrecoverable: incompatible_configuration`;
  another checksum → `incompatible_source`; a damaged manifest → left as it is, reported
  `manifest_<state>`; an I/O failure → left for the next run), `installed` ones retired, published
  and closed; `unrecoverable` and damaged ones reported. The run summary carries
  `install_recovery` and `staging_cleanup`.
- **Per document** in the run: a damaged or still-`installing` journal is never extracted over (an
  error `install_journal_<state>` / `extraction_install_incomplete`, prior state preserved); an
  `unrecoverable` one is not skipped as unchanged — the fresh attempt supersedes it.
- **Staging cleanup** after recovery removes staging entries and holding directories no journal
  names (the pre-journal `normalized/.staging/<id>/` layout included), and nothing at all while the
  journal inventory is incomplete or any journal damaged.
- **Reporting:** `validate_normalized` fails per blocked document ("extraction install <state> —
  run extraction") and for an unreadable inventory; lint files a high `extraction_install_incomplete`
  finding with that remediation.
- **Write guard:** the runtime guard gained an `extraction` family (`normalized/markdown`, `chunks`,
  `extraction_logs`, `tables/<id>/`, `.install/`, `.staging/`), so every install write is proved to go
  through `durable_write` (new helpers `move` and `remove_tree`).
- **Tests** (`tests/test_extraction_install.py`): a crash before **every** durable write of a
  re-extraction (old generation if the journal was never written, the new one once it was — never
  a mix; nothing left open or staged; `validate_normalized` passes), every second install crash
  with the recovery crashed again at each of its own writes, emptied / added / changed table sets,
  a failed directory sync after the last rename whose recovery repeats the barrier, an `installed`
  journal kept open across failed publications, incompatible configuration and staged-file mismatch
  or loss (`unrecoverable`, every artifact byte-identical), supersession by a fresh attempt
  (superseded artifacts retired), damaged journals blocking the document and all cleanup (never
  re-parsed), cleanup removing only unreferenced entries, a final-verification failure, governance
  kept across recovery, and a damaged manifest blocking recovery until repaired.

**As implemented (slice 5b — `extraction_install.consistent_read` / `read_text` / `usable`).**

- **The read.** `consistent_read(normalized_dir, source_id)` holds the write lock (re-entrant;
  `authority=False, changes=False`: a read neither checks the publication queue nor advances the
  vault revision) and yields the document's journal state; the caller reads the bytes it needs
  inside it, and only when the state is usable (`absent` / `installed`). `read_text` is the
  one-file form (None when blocked, "" when the file is absent). Model calls stay outside.
- **Producers** (enrich, claims, items) capture the Markdown, the chunks and the extraction log's
  PDF hint (the prompt title) in one consistent read; a blocked document is skipped before any
  model call (`install_incomplete` in the run summary, the run `partial`). Each commit re-checks
  the journal under its write lock, next to the existing input re-check, so an install that crashed
  during the model call stops it with nothing written — the empty-Markdown commits included (they
  now also re-check the text).
- **Synthesis and contradiction inputs.** A claim context does not read a blocked document: its
  citation keeps the anchor with no quote, and the context lists the document under
  `install_incomplete`. Eligibility and the synthesis fingerprint (anchors) are unchanged, so no
  synthesis is retracted or reported stale for it; a topic carrying the flag is never generated
  (no model call — also when the document became usable after the topic was built), and the commit
  re-checks the topic under the lock. Contradiction maintenance reports such a proposal
  `unverifiable: extraction_install_incomplete` and never retires it (unknown evidence is not
  changed evidence); a contradiction check refuses a blocked document (`Refused`) and its filing
  revalidation refuses a window whose document became blocked.
- **Indexes.** The keyword reindex captures each document's chunk bytes under the write lock before
  any index write (lock order: write lock, then SQLite) and parses the captured bytes — the
  fingerprint and the rows come from one read; an incremental pass neither indexes nor removes a
  blocked document (its rows stay; search excludes it) and `consistency_errors` reports it once
  ("extraction install incomplete (run extraction)"). **A full rebuild** (`--force`, or a schema
  change) starts from empty tables and can't keep its rows: it leaves the document out — the
  same exception for both indexes, reported in `install_incomplete` (every journal-named document
  is checked, so one whose chunk file is gone is reported too); search keeps excluding it either
  way. The vector reindex reads the same way; `status` doesn't count a blocked document's rows as
  removed and notes it. `reindex_keyword.py` prints the skipped documents.
- **Search and `/query`.** The ADR-0074 decision 2 gate also excludes a document whose journal is
  unusable (a note says so); it reads the journal without the lock — the hits come from the index,
  not the normalized files. The `/query` evidence pack reads each document's Markdown with
  `read_text`, so a hit is cited only from a usable extraction.
- **API and screen.** `GET /sources/{id}/normalized` and `/chunks` answer 409
  `extraction_install_incomplete` (with the journal state). The document screen shows a banner —
  "No usable extraction (interrupted install)" — with the fix per state (run extraction to complete
  or replace it; restore a damaged journal from backup first); the Details form derives no
  suggestion or PDF hint from a blocked extraction.
- **Report-only readers.** Lint's summary-rot check skips a blocked document (coverage degraded)
  and its stale-claim check treats it as unverifiable; the install-journal finding names the fix.
- **Source pages** (review round 1, 2026-11-15). The one Source page renderer
  (`wiki.generate_wiki`, which publication calls) reads the text and the extraction log in a
  consistent read; a blocked document's page is not rendered — it keeps its previous version, the
  render reports `extraction_install_incomplete`, and publication leaves the document pending
  (`sources_failed`, queued). An `installed` journal renders (the publication-cycle rule above: only
  installing, unrecoverable and damaged block).
- **Claim pages** (review round 1). The claim renderer reads each cited document with `read_text`:
  a blocked document's citation keeps its anchor and shows no quote, and the page renders — hide,
  supersede and deprecation proceed without reading blocked text (the synthesis-input approach).
  The quote returns at the claim's next render after extraction completes the document.
- **Tests** (`tests/test_consistent_reads.py`): every consumer skipping a document in each blocked
  state (`installing` with mixed files, `unrecoverable`, corrupt), producers reading it again once
  extraction completed it, a consistent read and a keyword reindex racing a paused install (held
  off, then one complete generation), commits racing a crashed install (enrich, claims, items,
  synthesis, the empty-Markdown commits), a topic built while blocked not generated after it
  unblocks, contradiction maintenance never retiring on unreadable evidence, a contradiction check
  and its revalidation refused, search / `/query` / API / screen / Details, the vector index;
  review round 1: publication keeping a blocked document's Source page and leaving it pending in
  every blocked state (and a direct wiki run reporting it), publication rendering under an
  `installed` journal, hide / deprecation / guarded supersede of a claim citing a blocked document
  (anchor kept, no quote), and a full keyword rebuild leaving a blocked document out, reported.

## Part C — order of work and deferrals

### Decision 9 — slices and order (grill Q9)

One implementation commit per slice where practical, **review fixes folded in before committing**.
A large slice is **not** forced into one review-sized unit to keep that count: slices 3, 5 and 6 may
be split into smaller sub-slices. **Every slice finishes with its targeted failure tests and the
project gate** (tests, ruff, validators). Each slice starts on the user's "implement now".

| Slice | Content | Decisions |
|---|---|---|
| **1** | Durable-write helper; consolidation of the existing durable writers; fault-injection suite; structural guard; integration coverage per authority family. **Consolidation preserves existing creation, ownership and locking semantics** — atomic replacement never turns a create-only operation into an overwrite | 1 |
| **2** | Typed manifest read states: reader, intake "only absent creates", the `load_manifest` / `list_manifests` / `valid_manifests` caller audit, incomplete-coverage blocking, retrieval exclusion of documents with an **unreadable or corrupt** manifest, `validate_raw_integrity`. This **damaged-manifest exclusion is distinct** from general visibility suppression (change 7a), which checks **valid** manifests' current hide/archive status | 2 |
| **3** | Read discipline for the other families (Item / Claim / Synthesis pages, review and audit records, policies, journals) with behavioural tests per family; sub-slices allowed | 4 |
| **4** | `scripts/repair_manifest.py` inspect / restore | 3 |
| **5** | Extraction install journal: stages, roll-forward recovery, consistent consumer reads, staging cleanup, crash-transition tests; sub-slices allowed | 8, 7.4 |
| **6** | Extraction child: spawn bootstrap, handshake and process group, watchdog, IPC limits, error categories, attempt records and retry classification, descriptor isolation, extraction-only stale-job reconciliation, cancellation; sub-slices allowed | 5, 6, 7.1–7.3, 7.5 |

Install recovery (slice 5) deliberately precedes the subprocess (slice 6): the child then only adds a
new way to *fail* in front of an install that is already crash-safe. Prioritising extraction first
was considered and not chosen.

**The accepted review follow-ups are three independently reviewed changes, after slice 6**, each with
its own short grill:

- **7a — manifest visibility enforcement:** default search and `/query` check each candidate's
  **valid** manifest's current hide/archive status (the authority) and **fail closed before evidence
  reaches the model**, so a hide/archive whose reindex failed still suppresses retrieval.
- **7b — documentation reconciliation:** `AGENTS.md` and Build Specification §3.2 adopt ADR-0068's
  durable wiki fields; the stale Build Specification statement that semantic producers do not
  refresh keyword search is corrected.
- **7c — Apply preview benchmark** at the 600-document target: latency, peak temporary storage,
  concurrent tagging — **measured with copies first**; **results precede any storage optimization**,
  and reflinks are preferred over hardlinks if one is needed.

### Change 7a — manifest visibility enforcement (grill 2026-11-16)

Split into **7a-1** (Q1, Q2, Q6) and **7a-2** (Q3–Q5), both implemented 2026-11-16 (see below),
each reviewed and validated independently; operational acceptance waits for both. Closes the
**status-copy failure**: retrieval filters a document by the
status copied into the keyword index's `navigation` table (from its Source page), so a hide or
archive whose reindex failed left the document's evidence reaching the model. The manifest's
`status` is the authority (ADR-0036 decision 13, ADR-0043).

1. **Both must allow it (Q1).** The index status filter stays in SQL as a cheap **pre-filter**;
   `_ManifestGate` additionally requires the **valid** manifest's status to belong to the request's
   **effective** status set — the default retention set or an explicit `source_status=` filter.
   Applies to keyword evidence, vector evidence and Source navigation hits. A **malformed** status
   (present, but not one of `SOURCE_STATUSES`) refuses the document — `get_status` must not turn it
   into `active` here; only a genuinely **absent** legacy status keeps its documented default
   `active`. **Intentional, temporary recall loss:** after an unhide, or for an explicit status
   search, whose reindex failed, the stale index can still exclude a document the manifest allows,
   until a reindex. That errs toward suppression; exact recall waits for reindexing. A hide or
   archive takes effect despite a failed publication.
2. **The graph group's Source nodes too (Q2).** `/search` is a discovery surface: each `source` node
   in the graph group needs both the graph's status filter and the manifest's status to allow it,
   against the effective `node_statuses`; edges incident to a dropped node are removed. Item /
   claim / synthesis nodes (page / graph authority) and the raw `/graph/*` inspection API
   (ADR-0043 decision 1: hidden nodes visible with their status) are unchanged. The gate's
   **status-independent** checks (inventory validity, install journal) stay shared, but the
   allowed status set is **passed explicitly** — evidence uses `source_statuses`, graph nodes
   `node_statuses` — and a decision cached for one set is never reused for the other.
3. **A final check before the model call (Q3).** `answer_query` takes a visibility check and runs it
   **fresh** (a new inventory read, not the search's cached gate) **after the evidence pack is built
   and immediately before the provider request is prepared**, so `/query` and its eval runner behave
   identically. Same effective `source_statuses` as retrieval; a failed or damaged manifest read
   excludes; the prompt pack and its citation lookup are filtered together; if nothing remains, the
   ordinary abstention, with **no model call**; a note counts the evidence items this check removed.
   **Guarantee:** a hide or archive whose manifest write completed before the final check began
   excludes that document from the call. The window between the check and the send is **accepted**
   and has no strict time bound; a request already sent cannot be withdrawn. Closing it would need
   the **exclusive** write lock (or another exclusion) held across the call — the shared vault lock
   does not exclude Apply, whose writers hold it shared too — contrary to the model-call contract.
4. **Saving an answer (Q4).** `save: true` re-checks **every document the final answer cites**
   against the request's effective `source_statuses` under `write_lock.hold(..., changes=False)`,
   with a fresh read — current eligibility, not "status unchanged". All allowed: the lock
   invalidates (advances the revision) immediately before the page and `wiki/log.md` writes. Any
   citation failing, a damaged manifest included: the **whole save is refused** — no page, no log
   line, no revision advance; citations are never trimmed from an otherwise unchanged answer. The
   response is still HTTP 200 with the answer, plus `query_id: null`, `navigation_stale: false` and
   `save_refused: {reason: "citation_visibility_changed", count: N}` beside a readable note.
   **Limitation (deferred, see the table):** 7a suppresses source evidence and prevents newly
   ineligible saves; it does **not** remove quotations in Query pages saved before a hide from
   discovery.
5. **Apply's source-hide warning renamed (Q5).** With read-time enforcement,
   `hide_retrieval_suppression_not_guaranteed` misnames the remaining problem. It becomes
   **`hide_index_refresh_pending`** — "Source retrieval is suppressed by the manifest gate. Index
   refresh is incomplete; retry publication to restore index consistency and discovery recall." The
   strict posture stays (`validation_failed`, the dry-run's non-clean verdict). Archive's
   warning-only posture and the semantic / claim / synthesis hide and unhide warnings are unchanged.
   API vocabulary, UI, tests and the ADR-0043 note change together, within 7a.
6. **Silent exclusion (Q6).** A status exclusion adds **no** count, flag or query-specific
   index-lag note — any would reveal that a hidden document matches the query. Result counts
   describe surviving results only; no pre-filter totals. A valid manifest whose status disallows
   the document stops the gate **before its install journal is checked**, so an "install
   incomplete" note cannot reveal it indirectly. Surviving Source results report the
   **manifest's** status wherever they expose one — evidence, navigation, graph Source nodes;
   semantic-node statuses are untouched. Q3's final-check count and Q4's `save_refused` count stay:
   they explain a change affecting evidence already admitted during that request, and give only a
   count and a reason, never the excluded documents' identities. Damaged-manifest and
   install-journal notes are unchanged for documents the status check admits. **"Silent" means
   no explicit exclusion diagnostic, not indistinguishable responses:** a stale hidden candidate
   still occupies a place in a bounded per-channel candidate pool before the gate drops it, so it
   can still affect ranking, recall or truncation (the cost of keeping the index pre-filter, Q1).

**As implemented (7a-1 — Q1, Q2, Q6).**

- **The gate** (`search._ManifestGate`) checks, in order: usable authority (decision 2, reported as
  `blocked`); the manifest's status in the status set passed **on that call** (silent); the install
  journal (decision 8, reported as `install_blocked`). Only status-independent facts are cached —
  the inventory's `{valid id: status}` map and each journal verdict — so no decision is reused
  across status sets. `manifests.visibility_status` reads the status: absent → `active`; present
  but not one of `SOURCE_STATUSES` (`null`, a wrong type or case) → `None`, refused under any filter.
- **Status sets.** Keyword and vector evidence pass `source_statuses`; Source navigation hits and
  graph Source nodes pass `node_statuses` — the same set the index / graph pre-filter used for that
  channel.
- **Graph.** `graph_read.search_subgraph` takes an `admit_extra` predicate, applied inside the
  bounded traversal like every other admission check: a refused Source node is not returned, not
  expanded (a node reachable only through it is not reached), and no returned edge touches it.
  `neighborhood` / `node_view` and the `/graph/*` routes don't take it.
- **Reported status.** Surviving evidence (`source_status`), Source navigation hits and graph
  Source nodes (`status`) carry the manifest's status; item / claim / synthesis nodes keep theirs.
- **Tests:** `tests/test_manifest_visibility.py`. The silent-exclusion tests compare the
  diagnostic surface (notes, counts, flags, surviving hits) with and without a matching hidden
  document on small vaults; they don't prove full indistinguishability (bounded candidate pools,
  see Q6).

**As implemented (7a-2 — Q3, Q4, Q5).**

- **The fresh check** is `search.visibility_check(manifests_dir, normalized_dir, statuses)`: each
  call builds a new gate (a new inventory read) and returns the ids that pass all three gate checks.
- **Final check (Q3).** `query.answer_query(..., visible=...)` applies it to the pack's documents
  after `build_evidence_pack` and before `_render_pack`; the kept entries are renumbered `e1..eN`
  and the citation lookup is built from them; an empty pack returns the abstention before
  `client.parse`. `QueryAnswer.visibility_removed` counts the removed items; `/query` adds the note
  "N evidence item(s) removed by the final visibility check (ADR-0074): a document's eligibility
  changed during the query" (eligibility, not status: damage or an incomplete install also
  excludes). `/query` passes the request's
  `source_statuses`, the eval runner (`_eval_query_fn`) the default set.
- **Guarded save (Q4).** `/query` renders the page first, then under
  `write_lock.hold(settings.reviews_dir, changes=False)` re-checks the cited documents; all
  eligible → `write_lock.invalidate()`, page, `wiki/log.md` line; otherwise nothing is written and
  the response carries `save_refused: {reason: "citation_visibility_changed", count}` (new
  `QueryResponse` field), `query_id: null`, `navigation_stale: false` and a note. The hold stays
  authority-checked (the default, never `authority=False`), so an unreadable publication queue
  still blocks the write; `/query` catches that `QueueCorrupt` around the guarded save and returns
  HTTP 200 with the answer, `save_refused: {reason: "publication_queue_unavailable"}`,
  `query_id: null`, `navigation_stale: false` and a note that nothing was saved and the queue
  needs repair. No page, log line or revision advance; the damaged queue is left as found
  (review round 1).
- **Rename (Q5).** `run_apply` emits `hide_index_refresh_pending`; `review_html.WARNING_EXPLANATIONS`
  carries its sentence, shown beside the code on the dry-run preview and the Apply result screens.
- **Tests:** `tests/test_query_visibility.py`; the rename in `tests/test_hide_content.py`.

**Tests (written at implementation).** Stale-index combinations (hide / archive with a failed
reindex; unhide with a failed reindex; an explicit status search against a stale index); explicit
filters; malformed statuses; stale graph mirrors, explicit hidden-status graph searches,
incident-edge removal and unchanged raw `/graph/*` inspection; a hide / archive during pack
construction, partial and complete exclusion, damaged manifests, explicit filters and the shared
eval path; hide-before-save, save-before-hide, damaged manifests, mixed allowed / disallowed
citations, a refusal leaving page, log and revision unchanged; responses compared with and without
a matching hidden document — including one with a broken install journal — showing no added
diagnostic signal.

### Deferred items and reopening triggers

| Item | Deferred because | Reopening trigger |
|---|---|---|
| Rebuilding a manifest without a backup, from the raw file and the audit log (decision 3) | Lossy (occurrences, modification times, unconfirmed fields) | A damaged manifest with **no compatible backup**, including when every backup is too old to preserve later human decisions |
| Explicit reconciliation of an older backup with later governance / Details decisions (decision 3) | Restore refuses instead; no override that temporarily resurrects visibility | The same no-compatible-backup case, or a restore refused for newer decisions that the operator cannot satisfy with a newer backup |
| Dedicated repair tooling for pages, review / audit records, policies and journals (decision 4) | Backup recovery under the agreed preservation, locking and publication rules suffices | The **first damage observed** in that family, or a backup recovery that cannot safely preserve later human decisions |
| Stale-`running` job reconciliation for producers other than extraction (decision 7.3) | Needs an audit of guard ownership and job-type mappings first | A hard-killed run of another producer observed left `running`, or the audit being scheduled |
| Bounded backoff for repeatedly timing-out documents (decision 6) | Timeouts are ambiguous and always retried; no evidence of cost yet | Repeated timeouts becoming operationally costly (run time dominated by retried timeouts) |
| Retracting or re-checking Query pages saved before a hide (change 7a, Q4) | 7a suppresses source evidence and refuses newly ineligible saves; earlier saved quotations stay discoverable | A hide that must also retract saved answers |
| Preview storage optimization (change 7c) | Benchmark results come first | The 7c benchmark exceeding the agreed latency / storage limits |
