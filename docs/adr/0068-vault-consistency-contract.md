# ADR-0068 — Vault consistency contract: one write lock, local publication, consistent previews

- **Status:** accepted — design-locked by grill-phase 2026-10-28. **Slice 1 (decisions 2–3: write
  lock, run guards, commit phases) implemented 2026-10-28** (see "Implementation — slice 1");
  slices 2–4 (publication + queue, producer publication + keyword refresh, previews + token) not
  yet.
- **Date:** 2026-10-28
- **Drivers:** an external architecture review after ADR-0066 part 3's grill. It found three
  blocking issues and four non-blocking ones; the user answered four framing questions and five
  grill questions. Amends **Build Specification §3.2** ("the wiki is derived, reviewable and
  regenerable"), **ADR-0029** (the wiki as "a disposable view"), **ADR-0039** (the vault lock's
  role) and **ADR-0040** (what a dry-run guarantees).
- **Related:** ADR-0022/0030/0048/0049/0052/0058 (page-owned fields), ADR-0039 amendment and
  `app/backend/vault_lock.py` (writer lock for backups), ADR-0066 review rounds 6–10 (tag-ledger
  lock, Apply serialized with tagging, journals and repair), ADR-0067 (unsafe requests take the
  vault lock).

## Context

What the review found in the code (verified 2026-10-28):
- **Writers are not serialized with each other.** `db/.vault.lock` is held *shared* by every job and
  unsafe request, so it keeps backups out but lets writers overlap. Only the tag-ledger lock
  (`reviews/tag_links/.lock`: exclusive, cross-process `flock`, re-entrant per thread) serializes
  anything. Its users are tag actions, items extraction's writes, merges, splits, retypes,
  deprecations, duplicates, promotion and, since review round 10, Apply. Enrich, claims,
  `generate_wiki`, contradictions, synthesis, lint, stale-check and reconcile commit without it.
  Nothing stops two runs of the same producer.
- **Success does not guarantee publication.** Tag actions and Apply refresh the affected Source
  pages, `index.md` and the keyword index (`tag_links.finish`), but a failure only becomes a
  warning in that one response (`source_page_refresh_failed`, `keyword_reindex_failed`), and
  nothing records it. Extraction scripts don't refresh the keyword index at all. On demo day
  `validate_index_consistency` failed until a manual reindex.
- **The dry-run is neither consistent nor binding.** `apply_sandbox.build_sandbox` copies the vault
  with `shutil.copy2`/`copytree`, including `graph.sqlite`, and takes no lock. Backups already use
  SQLite's online backup method; the preview does not. The Apply button sends nothing about the
  preview, so Apply runs whatever `approved/` holds at that moment against the current state.
- **The wiki is not disposable, whatever the docs say.** Item, claim and synthesis pages are the
  authority for fields that are read back from the page on every re-render. The graph's `nodes`
  table stores no title or aliases.

## Decisions

### 1. Durable authority vs projections, field by field

A page can hold both durable fields and generated sections. Authority is declared per field:

| Page | Durable fields (authority, read back on re-render) | Everything else |
|---|---|---|
| Item (`wiki/Items/`) | `title`, `aliases`, `item_type`, `status` (incl. decided statuses), `review_status`, `confidence`, `description`, `split_from`, `split_review_id`, `merged_into` (tombstone) | projection |
| Claim (`wiki/Claims/`) | `status` (`hidden`, `deprecated_candidate`) | projection |
| Synthesis (`wiki/Synthesis/`) | `status` (`hidden`) | projection |
| Source pages, `index.md`, `wiki/Authors/` (ADR-0066 part 3), backlink and list sections everywhere, summary callouts, `generation_status`, `input_fingerprint` | — | **pure projection**, rebuildable at any time |

- **Where else authority lives** is unchanged: `raw/` and `raw/manifests/` (including `status` and
  `provenance`), `reviews/` (review items, tag ledgers, audit log, journals), `policies/`,
  `db/graph.sqlite` (relationships and their status) and `db/llm_cache.sqlite`. This ADR adds
  `db/publication_pending.json` and the revision record (decisions 4 and 5).
- **Backup** already covers all of it (`wiki/`, `reviews/`, `policies/`, `raw/manifests/` and all
  of `db/`).
- **Moving the durable fields out of the pages** (making the whole wiki disposable) was considered
  and not chosen. It would rewrite every "the page is the authority" reader for no current need.

### 2. One exclusive write lock for every commit phase

- **The write lock:** one exclusive, cross-process, re-entrant lock, a widening of today's
  tag-ledger lock. Every **commit phase** takes it: the final validation plus the writes to the
  graph, ledgers, reviews, manifests, policies or durable page fields. That covers:
  - screen and API actions: tag actions, review decisions (approve, reject, defer, reopen),
    human-add and human-filed merges, hide and unhide, provenance confirmation (part 3);
  - Apply and every executor;
  - every producer's commit: Enrich, claims, items, `generate_wiki`, promotion, contradictions,
    synthesis;
  - the review filers (lint, stale-check, reconcile) and the repair scripts (`rebuild_tag_links`,
    `reconcile_reviews`).
- **Model calls stay outside the lock.** A producer calls the model, then takes the lock,
  **re-validates** under it (for example, a source archived or a claim hidden during the call is
  respected, and decided statuses are never overwritten), commits and releases.
- **The vault lock keeps one job:** coordinating writers with backup and restore (shared for
  writers, exclusive for backup and restore). It does not serialize writers with each other.
- **Lock order:** vault lock → write lock → SQLite transaction. Nothing takes the vault lock while
  holding the write lock.
- **Not atomic:** the lock prevents races, but filesystem and SQLite writes inside it still are not
  one atomic transaction. The existing crash journals (`reviews/in_flight/`, deletion and resolution
  records, repair) stay, and decision 4's pending queue covers the publication gap.

### 3. One run at a time per producer

- Each producer (Enrich, claims, items, `generate_wiki`, contradictions, synthesis, promotion, lint,
  stale-check, the reindexers) has a **cross-process run guard**: a non-blocking lock held for the
  whole run. A second instance of the same producer refuses at once with "already running" and
  writes nothing.
- Different producers may run at the same time; their commits serialize on the write lock.
- **Tagging stays available during a run:** the write lock is held only per commit, never during
  model calls.

### 4. Success means local publication; a pending queue covers failures

- **Publication** means the affected Source, item and claim pages, their backlinks and any page
  removals, `index.md`, and the keyword index all reflect the committed change. It runs **inside the
  write-lock boundary**, after the commit.
- **One publication function**, used by every action, every job's end-refresh,
  `scripts/republish.py` and the **Retry publishing** button.
- **Publication intent is recorded first.** Under the write lock, before changing authority, the
  writer adds an entry to **`db/publication_pending.json`** by atomic file replacement. Each entry
  holds the affected ids and the publication steps required (pages to render, pages to remove,
  index, keyword), never rendered content. Then it commits authority, then publishes. A crash at
  any point leaves an entry to retry; an unnecessary refresh after an interrupted action is
  harmless.
- **Retries publish current authority.** Overlapping entries coalesce, and each step renders the
  latest state, so an older entry can never restore an earlier revision.
- **One automatic retry per publication batch.** Each publication combines the pending entries with
  the current action's work and refreshes once. Anything still failing stays queued and is
  reported. There are no loops, and a failed publication never blocks unrelated authority changes.
- **A distinct result, `saved_publication_pending`:** "Saved; publication incomplete", listing the
  outstanding work, plus a retry banner on every review screen while the queue isn't empty. Never a
  generic failure: that would invite the user to repeat a decision already committed.
- **Entries are cleared** only after every required step succeeded.
- **`validate_all` stays read-only** and fails while the queue isn't empty. A separate validation
  failure stays a validation failure; publication runs before validation.
- **The queue file** is schema-versioned, backed up and treated as durable coordination state.
  **A corrupt or unreadable queue is a visible error, never an empty queue.** Authority changes
  refuse until it is repaired, because pending publication work cannot be preserved if it cannot
  be read (fail-closed, the same rule as a damaged tag ledger). Boundaries (user, 2026-10-28):
  - **Still available:** read-only access, diagnostics, backup, and explicit queue repair.
  - **Repair keeps the damaged file** (set aside, never deleted) and **reconciles the outstanding
    work**: it re-derives what still needs publishing from current authority versus the published
    pages and indexes. Replacing the file with an empty queue is not a repair.
- **Outside the queue:** the vector index, which stays explicitly eventual. Job results and
  validators report it as stale; `reindex_vector` refreshes it.

### 5. Bulk jobs commit per document

- For each document: take the write lock, commit its authority changes, publish **that document's**
  pages (Source, item and claim pages, backlinks, removals), release. The model calls for the next
  document happen outside the lock.
- At the end of the run the job refreshes `index.md` and the keyword index **once**. That covers
  extraction runs that changed searchable content, which today leave search stale. The job reports
  success only after this refresh. While the run is going, its status says that search refresh is
  pending.
- A keep during a run waits at most for one document's commit, and the job never overwrites it with
  an older page: both render from current authority under the same lock.
- If the end-refresh fails, the job's result is `saved_publication_pending`, with the work left in
  the queue.

### 6. Consistent previews and a revision token

- **The snapshot:** `build_sandbox` copies the vault **under the write lock**, with SQLite's online
  backup method for every SQLite file (as backups do) and plain copies for the rest. The sandbox
  Apply then runs on the copy, outside the lock.
- **The revision:** `(epoch, counter)` in `db/`, backed up.
  - The write lock itself increments `counter` when a holder committed a change to authority
    (graph, ledgers, reviews, manifests, policies, durable page fields), so no writer can forget it.
  - Read-only holders (the snapshot build), job records, cache writes, the publication queue and
    derived indexes do not bump it.
  - **Every restore generates a new `epoch`**, so two histories that diverge after a restore can
    never share a token.
- **The token:** the preview returns the revision it snapshotted. Apply checks it **under the write
  lock, before its first mutation**, and refuses a stale one with `stale_preview` ("changed since
  the preview, run it again") and **no mutations**.
- **Every Apply requires a preview token**, from every entry point: the UI form, the JSON route and
  scripts. There is no unpreviewed Apply.
  - **Scripts** request a preview, inspect its result, then submit its token.
  - **On a mismatch** the operation is refused with no writes. A script must **not** silently
    preview again and apply: changed effects deserve review.
  - **Scope:** the token applies to Apply only. Immediate keep/drop and the other screen actions
    keep their own validation under the write lock.
- **Accepted trade-off:** any authority change between preview and Apply invalidates the token,
  even an unrelated one such as a keep on another document. The user re-previews, which takes
  seconds. A hash of only the relevant state was rejected: it must know every executor's inputs,
  and a missed input would accept a stale preview.

### 7. Bounded graph traversal (separate, non-blocking)

Output caps do not bound the assertions fetched while expanding the frontier. Graph traversal gets a
limit on intermediate work per expansion, plus a bounded-traversal test. This is a small separate
change and does not block ADR-0066 part 3.

## Acceptance tests (required)

1. Concurrent approve/reject, conflicting merges, and reopen versus Apply produce one valid
   governance outcome.
2. Hide or archive during an extraction model call survives the extraction commit.
3. Concurrent tag changes cannot publish stale backlinks or keyword results.
4. A successful screen action exposes updated local pages and search immediately.
5. Two instances of the same producer cannot overlap; tagging remains available during its model
   calls.
6. Preview snapshots stay coherent during concurrent activity; a stale token refuses Apply without
   mutations.
7. Crash injection between authority writes and publication preserves decisions and permits
   recovery.
8. Backup/restore preserves every declared durable field; projection rebuilds preserve that
   authority.

## Sequencing

1. Design-lock this ADR (done 2026-10-28).
2. Implement it in tested slices: locks and guards → the publication function and pending queue →
   producer integration and the keyword refresh → consistent previews and token enforcement. Its
   concurrency and crash-recovery tests pass before ADR-0066 part 3 starts.
3. The graph traversal bound (decision 7), here or later.
4. ADR-0066 part 3, with its tests and UAT. Every new writer (Enrich tags, Details confirmation,
   Authors pages) uses the shared commit and publication mechanism.
5. ADR-0066 part 4 (the scorer), verified against the golden cases, with a pre-restart baseline
   captured where practical.
6. ADR-0066 decision 10: the backup-gated, human-approved clean restart, then fresh ingestion, UAT
   and scoring.

## Implementation — slice 1 (2026-10-28)

- **`app/backend/write_lock.py`:** the write lock (`hold`, `held`), moved out of `tag_links`
  (`tag_links.ledger_lock` is now an alias). Same file, `reviews/tag_links/.lock`.
- **`app/backend/run_guard.py`:** `guard(db_dir, producer)` (non-blocking `flock` on
  `db/.run-<producer>.lock`, re-entrant per thread, `ProducerBusy`) and `run_cli` (exit code 3).
  Taken at entry points only: the producer scripts' `__main__` (scan, extract, generate_wiki,
  enrich, claims, items, promote, contradictions, synthesis, reindex_vector, rebuild_tag_links,
  reconcile_reviews) and the `POST /jobs/*` routes (409 `already_running`). `rebuild_index.py` and
  `reindex_keyword.py` take no guard: they are publication steps spawned by other writers (slice 2
  puts them under the write lock).
- **Commit phases under the write lock:**
  - API: decisions, reopen, merge filing, the source-screen routes and the whole of `run_apply`
    (`_commit_phase`; the async source-decide batch now runs in a worker thread).
  - `reviews.py`: every review-item state change.
  - Producers, per document: Enrich's artifact write; claims' commit and page recompose; items
    (already); each Source page render in `generate_wiki`; contradictions' and synthesis' commits;
    lint's and stale-check's detect-and-file; intake's manifest writes; extraction's manifest write;
    the reconcile sweep.
- **Re-validation under the lock:** claims and items mirror the source's status from the manifest
  as it is at commit time; a re-asserted claim keeps a human-set `hidden`/`deprecated_candidate`
  status; extraction writes back only its own fields (`manifests.EXTRACTION_FIELDS`) onto the current
  manifest; contradictions skip a pair a human decided during the model call; synthesis skips a
  topic hidden or approved during it; `generate_wiki` renders from the current manifest.
- **Backup** skips the run-guard files.
- **Tests:** `tests/test_write_lock.py` (17): the lock and guard units, acceptance test 1 (concurrent
  approve/reject → one outcome; a reopen waits for a running Apply and is then refused), test 2
  (archive during a claims/tags model call, hide during a claims call, archive during extraction)
  and test 5 (a second run refuses; the write lock stays free during a model call). Six
  mutation checks, each caught. The contradiction and synthesis re-checks have no dedicated test
  yet.

### Review round 1 on slice 1 (2026-10-28)

Static review found four issues and one test gap; the user chose the structural fix for the first.
- **P1 lock order (promotion vs backup deadlock):** `promote_candidates` took the write lock before
  `insert_job` took the vault lock, while backup takes them the other way round. **Structural fix:**
  the outermost `write_lock.hold()` now takes the vault lock shared first (at the reviews dir's
  sibling `db/`), so no writer can hold the write lock while waiting for the vault lock. Backup,
  already holding the vault lock exclusively, uses `hold(..., vault=False)`. Test: promotion paused
  between the two steps, then a backup subprocess refuses after its timeout (without the fix it
  deadlocks).
- **P1 contradictions:** the commit-time check now also reads the review ledger (an approved or
  rejected item counts at once, before Apply changes the edge) and re-checks that both claims still
  stand, are not hidden and show the wording and evidence the model judged
  (`_still_as_shown`; `skipped_changed_during_call`).
- **P1 synthesis:** under the lock the topic's current eligibility and evidence fingerprint are
  recomputed (`_current_fingerprint`); a changed or vanished topic discards the result
  (`skipped_changed_during_call`), and a rejection recorded during the call wins
  (`skipped_reviewed`).
- **P2 extraction outputs:** `_extract_one` stages Markdown, chunks and tables in
  `normalized/.staging/<source_id>/` outside the lock; the run installs them by rename, writes the
  log and merges the manifest fields in one commit phase. Claims, items and Enrich compare the
  normalized text with what the model read and write nothing if it changed
  (`skipped_input_changed` / `input_changed_sources`).
- **Test gap:** concurrent opposite merges (A ← B and B ← A) now file exactly one.
- **Tests:** `tests/test_write_lock.py` grew to 30; every new check was mutation-tested (undo the
  fix → its test fails).

### Review round 2 on slice 1 (2026-10-28)

- **P1 synthesis approval during a model call:** the commit now checks both terminal states of the
  current proposal. An approval recorded while the model ran keeps the artifact and page the human
  read, because Apply promotes that artifact (`skipped_reviewed`). Test: the model returns different
  prose and both files stay byte-identical.
- **Lock order inside one process:** the outermost `hold()` takes the vault lock before the
  process lock, then the `flock`, so a writer thread never holds the process lock while it waits
  for a backup thread. Test: a backup thread holding the vault lock exclusively and a writer thread
  started in that order both finish.
- **Per-topic re-check:** `eligible_topics` is now a loop over `topic_for(node_id)`, which the commit
  check calls for its one topic instead of rescanning every topic under the write lock.
- `tests/test_write_lock.py`: 32 tests; both new checks mutation-tested.

## Consequences

- One rule for every writer replaces a set of per-family serializations. Round 10's ledger-lock
  pattern becomes the general case.
- Tagging, reviewing and running the pipeline can overlap safely. The cost is short waits on
  per-document commits.
- "Success" becomes a promise about local pages and search, and an incomplete publication is a
  named, recoverable state, not a lost warning.
- Previews mean what they show: a stale preview cannot be applied.
- The wiki's regeneration contract is now precise. Rebuilding projections never touches the durable
  fields.
- Cost: every producer's commit path, the preview builder and the Apply routes change, and the
  required concurrency and crash tests are a substantial suite. No general-purpose job system is
  introduced.

## Deferred / out of scope

- Moving durable page fields into a separate store (decision 1, alternative).
- Making the vector index part of synchronous publication.
- Crash-atomic filesystem plus SQLite commits (journals and the queue cover recovery instead).
