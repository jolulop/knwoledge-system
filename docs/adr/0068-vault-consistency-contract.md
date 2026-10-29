# ADR-0068 — Vault consistency contract: one write lock, local publication, consistent previews

- **Status:** accepted — design-locked by grill-phase 2026-10-28. **Slice 1 (decisions 2–3: write
  lock, run guards, commit phases) implemented 2026-10-28** (see "Implementation — slice 1").
  **Slice 2 (decision 4: the publication function and pending queue, for screen actions and
  Apply) implemented 2026-10-29** (see "Implementation — slice 2"). Slices 3–4 (producer
  publication + keyword refresh, previews + token) not yet.
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

- **One exception, for publication only** (slice 2 review round, user 2026-10-29): publication
  copies every saved item field, `status` included, except that it may move a tag between `active`
  and `unused` per its saved keep/drop decisions (the ADR-0066 rule). Those decisions are the
  authority for that one choice. Every other status (`candidate`, `hidden`, `merged`, deprecated and
  retired) is copied as saved.

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

## Implementation — slice 2 (2026-10-29)

- **`app/backend/publication_queue.py`:** the queue file, plain IO. `load` (no file = empty; an
  unparseable file, a wrong `schema_version` or an invalid entry raises `QueueCorrupt`), `save`
  (atomic replace, flushed to disk before and after the rename), `record` (refuses outside the
  write lock), `discard`, `status` (for screens, never raises), `preserve_damaged`,
  `replace_with_recovery`. An entry: `{id, origin, created_at, claims, syntheses, items, sources,
  index, keyword, full, provisional, attempts}`: ids and step flags only, never content.
- **`app/workers/publication.py`:** the one publication function, `publish(root, intent=…,
  claims, syntheses, items, sources, index, keyword, full)`. Under the write lock it loads every
  pending entry, replaces the caller's provisional entry with its exact work, and widens what
  remains provisional from current authority:
  - a **provisional** entry adds every Source page that links its tags;
  - a **full** entry means every page of every kind. That is each existing page (claim, synthesis
    and Source pages by name, tag pages by the `item_id` in their frontmatter, canonical ids only),
    plus each id the graph and the manifests name, so missing Source pages are recreated. A page
    whose node or manifest is gone is still checked and stays pending (`node_missing`,
    `manifest_missing`, `manifest_unreadable`), never overlooked or taken as a deletion. Saved Query pages are
    answers and are never regenerated. Authors pages (ADR-0066 part 3) join when implemented.

  Then it runs each step once, in dependency order:
  1. claim pages, after copying each page's saved status into the graph mirror;
  2. synthesis pages (`rerender_synthesis_page`);
  3. tag pages;
  4. Source pages (`generate_wiki`);
  5. `index.md`;
  6. the keyword index.

  Also here: `republish_all` and `repair`.
- **Projection only** (slice 2 review round). Publication renders from saved authority. It never
  retracts a relationship, changes a governed status, or files or reconciles a review.
  - Claims render with `recompose_claim(project_only=True)`, which keeps the saved status and
    review status. A claim whose evidence is gone while its page is not a tombstone stays pending
    (`evidence_gone`); tombstoning is producer work.
  - Tag pages render with `tag_links.project_page`, which copies every saved field (decision 1's
    exception aside) and never tombstones or promotes. An interrupted first keep is the tag-link
    repair's to finish.
- **What counts as published.** A page is acknowledged only when it was rendered, or when it is
  legitimately no longer needed:
  - a tag whose deletion is audited;
  - a merged redirect page, preserved as it is;
  - a document with a valid manifest that isn't extracted yet.

  Everything else stays pending, with a reason the result screen and `republish.py` explain:
  - `page_missing`: restore the page from backup; its metadata is never fabricated;
  - `node_missing`: a missing graph node alone is not proof of deletion;
  - `deletion_unfinished`, `identity_operation_in_flight`, `ledger_corrupt`,
    `tag_links_repair_needed` (ledgers and graph disagree), `status_disagrees`;
  - `evidence_gone`, `page_or_artifact_missing`, `manifest_missing`, `manifest_unreadable`;
  - `graph_missing`, and an unopenable graph (`graph_unavailable`), which leaves every entry
    untouched.
- **Downstream closure, across entries.** A page rendered from a failed upstream page is not
  complete:
  - a failed claim holds its contradiction partners, the syntheses citing it and its Source pages;
  - a failed tag page holds the Source pages that list it;
  - any pending page holds `index.md` and search.

  This applies whichever entry asked for the downstream page, and the failure's owner also keeps
  its dependents. Entries whose steps all succeeded are cleared. The rest keep only their
  outstanding steps with `attempts + 1`, and the result is `saved_publication_pending`. The
  existing warning names stay, and new ones are added: `claim_page_refresh_failed:<id>`,
  `synthesis_page_refresh_failed:<id>`, `graph_unavailable`, `publication_pending`.
- **Repair never leaves the vault unguarded.** It runs in three steps:
  1. copy the damaged bytes to `db/publication_pending.damaged-<stamp>-<id>.json`, flushed to disk.
     The damaged file stays in place, so writes stay refused.
  2. Atomically and durably replace the queue with one full-recovery entry.
  3. Publish it.

  A crash after step 1 leaves writes refused; rerun repair. A crash after step 2 leaves a full
  entry pending.
- **Intent recorded first.** Provisional intent (the document plus the tags it names) is recorded
  before the first write by:
  - every source-screen tag action: the batch's link rows and alias edits, the JSON link route and
    undo-drop;
  - human-add;
  - Delete unused and the tag repair script (`full`, because the scope is unknown up front);
  - Apply (`full`, recorded in `run_apply` before `_run_apply`; discarded if Apply refuses with
    `GraphUnavailable` before its first write).

  Each then publishes through `tag_links.finish`, now a thin adapter over `publish`, or directly in
  Apply. Every publish also retries whatever was already pending.
- **Apply's status:** `validation_failed` only for a validator other than `validate_publication`
  failing, or for a synthesis the fan-out could not re-render. A refresh failure (Source pages,
  index or keyword, including the per-surface `…_not_guaranteed` gates and
  `identity_refresh_pending`) is `saved_publication_pending`. The response carries
  `publication: {status, pending}`. The dry-run preview still maps anything other than `applied` to
  a not-clean preview.
- **Fail-closed queue:** the outermost `write_lock.hold()` checks that the queue is readable, so
  every authority change refuses while the queue is damaged:
  - API routes: 503 `publication_queue_corrupt`, with an HTML page for `/ui/`;
  - scripts: `run_guard.run_cli` exits with code **4**.

  Holders that change no authority pass `authority=False`: backup (and restore, through its writer
  exclusion) and the repair itself. Reads keep working.
- **Retry and repair surfaces:** `GET /publication`, `POST /publication/retry`, `POST
  /publication/repair` (both under the `republish` run guard) and their `/ui/` forms. Every review
  screen's nav shows the retry banner ("Saved; publication incomplete … Retry publishing") while
  entries are pending, and a repair banner when the queue is unreadable. CLI:
  `scripts/republish.py [ROOT] [--all|--repair]` (exit codes: 0 published, 1 still pending,
  3 busy, 4 unreadable queue).
- **`scripts/validate_publication.py`:** read-only, auto-wired into `validate_all`. It fails while
  any entry is pending or the queue is unreadable.
- **Not yet (slice 3):** the producers (`generate_wiki`, extraction, claims, items, enrich, the
  reindex job) still refresh on their own and record no intent. Apply's claim and synthesis
  re-renders stay in its executors. Only an interrupted Apply's full entry publishes those pages.
- **Tests:** `tests/test_publication.py` (15) covers:
  - the queue's fail-closed reads (3 corruptions) and intent under the lock;
  - acceptance test 4: a keep publishes the Source page and leaves search consistent;
  - a failed refresh is `saved_publication_pending`, shows the banner, and Retry clears it;
  - the next action also publishes pending work;
  - a retry publishes current authority;
  - acceptance test 7: a crash after a keep's commit keeps the decision and `republish.py`
    recovers it; a crash inside Apply leaves a full entry that republishes;
  - acceptance test 3: a keep and a drop that overlap publish the final state;
  - a damaged queue refuses route writes and producer commits, keeps reads and the backup hold,
    and repairs without losing the damaged file;
  - provisional widening;
  - a refused Apply leaves no intent.

  The review rounds added 20:
  - a failed Source page keeps index and search pending until its retry;
  - a failed claim holds dependent work of another entry;
  - a crash inside Apply after a real claim hide, before its partner and synthesis refresh, is
    fully recovered. The hidden claim sorts after its partner, and the graph mirror is rolled back.
  - publication files no review and retracts nothing;
  - missing pages stay pending:
    - a missing tag page, until it is restored;
    - a missing node with no deletion evidence;
    - no graph, and an unavailable graph;
  - merged redirect pages are preserved;
  - every declared item field survives for active, candidate and hidden tags;
  - unused moves to active from saved keeps, and a kept candidate is not promoted;
  - an unfinished identity operation, a damaged ledger, or ledgers that disagree with the graph
    each keep the tag pending;
  - repair's crash windows;
  - a crash inside `republish_all`;
  - pages a full republish finds on disk, not in the queue: a tag page whose graph node is gone,
    and, through queue repair, a Source page whose manifest is invalid.

  Fifteen existing tests moved to the new seams or statuses. 30 mutation checks were run, and
  every new rule is caught. A full republish of a copy of the UAT vault (12 documents, 151 tags,
  979 claims) reported `published` and changed no page except `log.md`.

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
