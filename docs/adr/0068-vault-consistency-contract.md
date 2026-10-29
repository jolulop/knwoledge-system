# ADR-0068 — Vault consistency contract: one write lock, local publication, consistent previews

- **Status:** accepted — design-locked by grill-phase 2026-10-28. **Slice 1 (decisions 2–3: write
  lock, run guards, commit phases) implemented 2026-10-28** (see "Implementation — slice 1").
  **Slice 2 (decision 4: the publication function and pending queue, for screen actions and
  Apply) implemented 2026-10-29** (see "Implementation — slice 2"). **Slice 3 (decision 5:
  producers publish per document and refresh search once per run) implemented 2026-10-29** (see
  "Implementation — slice 3"). **Slice 4 (decision 6: previews + token, design-locked by its own
  grill, Q1–Q8) implemented 2026-10-29** (see "Implementation — slice 4").
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
  `db/publication_pending.json`, the revision record (decisions 4 and 5) and the producer commit
  journal `db/producer_journal.json` (slice 3).
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

### 6. Consistent previews and a preview token

- **The snapshot:** `build_sandbox` copies the vault **under the write lock**, with SQLite's online
  backup method for every SQLite file (as backups do) and plain copies for the rest. The sandbox
  Apply then runs on the copy, outside the lock. What is copied under the lock and what after it
  is set out below ("What the snapshot copies, and when").
- **The revision:** `(epoch, counter)` in `db/`, backed up.
  - **Conservative invalidation, not change detection** (slice-4 grill, Q1). The lock cannot see
    what a holder wrote, so `counter` tracks *potential* authority changes (graph, ledgers,
    reviews, manifests, policies, durable page fields). Every write-lock hold is mutating by
    default and invalidates outstanding tokens; a holder opts out explicitly with
    `changes=False`. A missed opt-out only costs an unnecessary preview refresh, never a stale
    preview accepted.
  - **Invalidate before writing, not on release.** The increment is persisted (atomic, durable
    write) *before* the lock yields to a mutating holder. A process that changes authority and
    dies before its `finally` runs therefore cannot leave the old token valid. One increment per
    outermost hold is enough: no snapshot can be taken while the lock is held.
  - **Nested mutating holds invalidate too.** A mutating hold nested inside an outer
    `changes=False` hold increments on entry (once per outermost hold).
  - **`changes=False` is reserved for paths that cannot change any preview input:** the
    snapshot build, backup, job records, cache writes, the publication-queue file itself and
    derived indexes. **Publication keeps the default** (it can sync claim statuses and move a tag
    between in-use and unused); any other opt-out must show it touches no preview input.
  - **Failure to persist the increment refuses the mutation:** the hold raises before yielding,
    and nothing is written.
  - **Every restore durably establishes a new `epoch`** before any later preview or write, so two
    histories that diverge after a restore can never share a token.
  - **Self-healing, not fail-closed** (slice-4 grill, Q2). Unlike the queue and the journal, the
    revision records no recovery work: replacing its epoch only makes every outstanding token
    stale, which can never accept a wrong Apply. The epoch is a random 32-hex id, never a
    timestamp. All initialization and recovery runs under the exclusive write lock, so
    concurrent processes observe the same replacement epoch.
    - **Missing file** (a new vault, a restore of a pre-slice-4 backup): durably initialize a new
      epoch with `counter = 0` before returning a preview or permitting writes.
    - **Malformed contents:** copy the damaged bytes to `revision.damaged-<ts>.json` and flush
      them, then atomically and durably replace the active file with a new epoch and
      `counter = 0`. Copy-then-replace, not move-away-first, so the evidence survives a crash at
      any point.
    - **I/O failure refuses:** if reading, preserving or replacing fails (permissions, storage),
      the hold raises and nothing is written. Only unparseable or schema-invalid contents count
      as malformed.
    - `counter = 0` is only the initialization value; a mutating hold then increments before
      writing, per the rules above.
    - **Apply after recovery** still compares the submitted token, now against the new epoch, and
      returns `stale_preview`. Recovery never bypasses the comparison.
    - **Visible, not blocking:** a preserved damaged copy is reported as a warning (revision
      status and validator output) that never fails validation, so it cannot block Apply or
      normal work; the operator deletes it when done.
  - **Required tests:** concurrent initialization yields one epoch; corrupt-file recovery
    rejects an old token; a crash at each step of the replacement leaves either the old or the
    new file plus the evidence; a persistence failure prevents authority writes.
- **The token** (slice-4 grill, Q3): an **opaque random preview id**, not the revision itself. A
  bare `<epoch>.<counter>` identifies a state but proves no successful preview happened: a caller
  could construct it, or reuse it with other Apply options.
  - **Only an `ok` preview authorizes Apply.** That preview creates a server-side authorization
    record bound to the revision it snapshotted **and** the exact Apply inputs and options that
    affect execution (none today beyond the vault state; the binding keeps any future option
    from diverging between preview and Apply). The rule lives on the server and is shared by
    the UI, the JSON route and scripts.
  - A non-clean preview (`blocked`, `failed`, `validation_failed`) returns `preview_token: null`
    and creates no record.
  - **Apply, under the write lock and before its first mutation:** look up the record (missing or
    unrecognized token → refused), check it matches the submitted options, compare its revision
    with the current one (mismatch → `stale_preview`), then durably invalidate, then write. Every
    refusal writes nothing.
  - **Single-use:** once Apply passes these checks and advances the revision, the token is spent,
    even if Apply then fails. Of two concurrent submissions of one token, at most one proceeds:
    the lock serializes them, and the second sees the advanced revision.
- **Authorization records** (slice-4 grill, Q4) live in **`db/apply_authorizations.json`**: durable,
  so they survive an API restart and are shared by the API and scripts.
  - **Recorded only after the sandbox preview succeeds**, never at snapshot time:
    1. under the lock with `changes=False`, capture the snapshot and its revision;
    2. run the sandbox outside the lock;
    3. if the result is `ok`, take the lock again (`changes=False`), check that revision is still
       current, and durably record the authorization bound to the exact Apply inputs;
    4. return the token only after that write succeeds. If the revision moved meanwhile, return
       no token and ask for another preview.
  - **At Apply,** under the lock: validate the record, the inputs and the revision, then durably
    consume the record and advance the revision, then write authority. A crash between consumption
    and execution only requires another preview, which is safe.
  - Each write prunes records whose revision is no longer current. Apply rejects a stale record
    on its own, whether or not it was pruned yet.
  - **Malformed file:** keep a copy of the damaged bytes, then replace it with an empty set, so
    outstanding tokens are refused as unrecognized. **I/O failure refuses:** a preview that
    cannot save its record returns no token; an Apply that cannot read or consume one refuses.
  - **Not backed up:** a restore must never bring back old records (its new epoch would refuse
    them anyway).
  - **Required tests:** restart survival; a revision change during the sandbox run yields no
    token; an authorization-write failure yields no token; a crash after consumption but before
    execution leaves the token spent and Apply needing a new preview.
- **What the snapshot copies, and when** (slice-4 grill, Q5). The lock is held only for what a
  writer can change:
  - **Under the lock:** `db/*.sqlite` through SQLite's online backup (their `-wal`/`-shm` and lock
    files are never copied); the other `db/` files (queue, journal, revision); `reviews/`, `wiki/`,
    `raw/manifests/`, `normalized/` and `policies/`.
  - **Never copied:** `db/apply_authorizations.json` and the LLM cache.
  - **After the lock:** the code (`app/`, `scripts/`, `templates/`) and the raw bytes named by the
    **copied** manifests, never the live ones. Raw files are never edited, but an approved
    deletion or a restore can remove or replace one; Q4's revision check before authorization
    catches any such operation in between. A failed copy never yields an authorized preview.
  - **Bound inputs beyond the revision:**
    - a **code fingerprint**: a hash over the relative paths and contents of the sandbox's copied
      `app/`, `scripts/` and `templates/`, so added and deleted files count; generated files
      (`__pycache__/`, `*.pyc`) are excluded. It is compared with the live code before the
      authorization is issued and again before Apply; a mismatch is `stale_preview`;
    - a **settings fingerprint**: the effective settings that affect execution and are not
      already covered by `policies/` or the other bound inputs (API keys and other secrets
      excluded).
  - **Code changes require an API restart.** A disk fingerprint cannot prove that already-imported
    modules match the files, so the API records the fingerprint of the code it started with and
    refuses previews and Apply with `restart_required` while the disk differs. Installing code
    (`git pull`, `uv sync`) stays outside any running preview or Apply; the write lock does not
    serialize it, so this is an operating rule (`docs/Operations.md`).
  - **Required tests:** a deletion or restore during the raw copy yields no token; a code change
    during sandbox construction yields no token; an added or a deleted code file changes the
    fingerprint; a code change between authorization and Apply refuses Apply.
- **Required tests (slice 4):** process termination after an authority write leaves the old token
  stale; a nested mutating hold inside a `changes=False` hold invalidates; a stale Apply is
  refused before any write; a read-only snapshot leaves the revision unchanged; a direct JSON
  Apply without a successful preview, with changed options, or with a non-clean preview's
  (absent) token is refused; concurrent reuse of one token lets at most one Apply proceed.
- **Accepted:** previews taken during an ingest go stale quickly, because each per-document commit
  invalidates.
- **Every Apply requires a preview token**, from every entry point: the UI form, the JSON route and
  scripts. There is no unpreviewed Apply.
  - **Scripts** request a preview, inspect its result, then submit its token. That entry point is
    **`scripts/apply_reviews.py`** with two separate commands (slice-4 grill, Q6):
    - `preview` prints the result (diff summary, validators, warnings) and a token only for an
      `ok` preview, otherwise why none was issued;
    - `apply --token <id>` submits it and **never previews again by itself**;
    - **refusals** (`stale_preview`, `restart_required`, an unrecognized token), **partial results
      and `saved_publication_pending` exit non-zero** with the reason printed.

    The script is thin: it **runs the shared implementation directly** (preview, authorization
    store, input binding, revision checks and the Apply run guard are one module used by both the
    API and the script), so a fresh script process always runs current code and never sees
    `restart_required` for itself. Tokens are interchangeable between the two entry points.
    Preview holds neither the write lock nor the Apply run guard while the sandbox runs. Required
    tests: an API-issued token applied through the CLI and a CLI-issued one through the API;
    concurrent Apply attempts; a stale token refused.
  - **The JSON contract** (slice-4 grill, Q8):
    - `POST /reviews/apply/dry-run` returns `preview_token`: a string only for `ok`, else `null`.
      When an otherwise successful sandbox result gets no token, `token_withheld` says why:
      `revision_changed` or `authorization_write_failed`. A failed copy is a **failed preview**
      (`status: failed`, `reason: copy_failed`, `preview_token: null`), never `ok`.
    - `POST /reviews/apply` requires `{"preview_token": "<id>"}` (a breaking change; today's
      JSON callers are tests and `curl`). Refusals:

      | Case | HTTP | `detail.reason` |
      |---|---|---|
      | body or token missing | 422 | `preview_required` |
      | no record: unknown, consumed, or pruned as stale | 409 | `unknown_preview` |
      | record present, a bound value differs (revision, code, settings, options) | 409 | `stale_preview` |
      | the store cannot be read, or the record cannot be durably consumed | 503 | `authorization_store_unavailable` |
      | the API's code differs from disk (also on the dry-run route) | 503 | `restart_required` |

    - Each refusal carries `{"reason", "action"}` with the specific action: "preview again";
      "restart the API, then preview again" for `restart_required`; "retry; check `db/`" for a
      store failure. A storage failure never looks like an unknown token.
    - **"Writes nothing" on refusal** means no authority change and no authorization consumed.
      Housekeeping under the rules above (revision initialization or recovery, store repair, the
      pruning of stale records) may still write.
    - **`scripts/apply_reviews.py` exit codes** are numeric, print the same reason strings, and
      the mapping is documented in `docs/Operations.md`. They extend `run_guard`'s 3/4/5:
      `0` applied; `1` partial, `validation_failed` or `saved_publication_pending`; `2` usage; `3`
      busy; `4` queue corrupt; `5` journal corrupt; `6` `preview_required`/`unknown_preview`/
      `stale_preview`; `7` `restart_required` (reserved: the script runs current code itself, so only an API caller sees it); `8`
      `authorization_store_unavailable`; for `preview`, `1` when no token was issued.
  - **The UI preview is a `POST`** (slice-4 grill, Q7). A preview writes the authorization store
    (and may initialize the revision), so it must not run on a safe method or outside ADR-0067's
    origin guard. `GET /ui/reviews/apply` becomes read-only: the scope counts and a **Preview**
    button, so the existing links still land there; it never runs the sandbox nor writes the
    revision or authorization files. The button posts to `POST /ui/reviews/apply/preview`,
    which renders the diff and, for an `ok` preview, the Apply form with the token in a hidden
    field.
    - **Distinct refusal messages:** `stale_preview` → "changed since the preview, preview
      again"; `restart_required` → "restart the API, then preview"; an unrecognized token →
      "preview again". Each carries the action it needs, not a generic "changed".
    - Responses carrying a token send `Cache-Control: no-store`. Reloading a preview only ever
      re-previews; resubmitting a spent token is refused as stale, so nothing applies twice.
    - Required tests: `GET` runs no sandbox and writes neither file; a cross-origin preview
      `POST` is refused before any work; a clean preview `POST` renders the token-bound form.
  - **On a mismatch** the operation is refused with no writes. A script must **not** silently
    preview again and apply: changed effects deserve review.
  - **Scope:** the token applies to Apply only. Immediate keep/drop and the other screen actions
    keep their own validation under the write lock.
- **Accepted trade-off:** any authority change between preview and Apply invalidates the token,
  even an unrelated one such as a keep on another document. The user re-previews, which takes
  seconds. A hash of only the relevant state was rejected: it must know every executor's inputs,
  and a missed input would accept a stale preview.

### 7. Bounded graph traversal (separate, non-blocking; design-locked 2026-10-29, Q1–Q6)

Output caps do not bound the assertions fetched while expanding the frontier. Graph traversal gets a
limit on intermediate work per expansion, plus a bounded-traversal test. This is a small separate
change and does not block ADR-0066 part 3.

Grill-phase 2026-10-29 (decision-7 grill). Today each BFS step (`graph_read.neighborhood` behind
`GET /graph/neighborhood`, `graph_read.search_subgraph` behind `/search` and `/query`) fetches every
active edge touching the frontier with no limit, then the metadata of every endpoint, and the
caps only trim the output. A topic-less disagreement search seeds from every endpoint of every
active `contradicts` edge.
- **Shape: a fan-out cap per frontier node** (Q1). Each frontier node contributes at most *k*
  edges per step. **Database work is bounded, not just returned rows:** `ROW_NUMBER()` over all
  incident edges would still scan and sort them, so the fetch is an indexed per-node query with
  `LIMIT k + 1` (the extra row detects truncation without counting), and its query plan is
  checked (no full scan, no temp B-tree over a node's edges).
- **Seed fairness is kept at admission:** candidates are admitted in ranked frontier order (seeds
  in BM25 order, each node's edges in its fan-out order), not sorted by id as today; round-robin
  across frontier nodes, so each gets an opportunity before one consumes the remaining
  `node_cap`.
- **Smaller bounds:** metadata is fetched only for retained candidates; the topic-less
  disagreement seed list is bounded **before** its metadata lookup, and clipping it sets
  `truncated`; an edge selected through both of its frontier endpoints is counted once.
- **Selection order and indexes** (Q2): a node keeps its first *k* edges in the stable order
  `(edge_type, other endpoint, edge_id)`. Ids are content-derived, so the selection is the same
  after a graph rebuild or restore. Two composite indexes serve it without sorting:
  `(src_id, status, edge_type, dst_id, edge_id)` and `(dst_id, status, edge_type, src_id,
  edge_id)`, under **new names** (`CREATE INDEX IF NOT EXISTS` would not widen an existing
  `idx_edges_src`/`idx_edges_dst`). A physical index change: no `SCHEMA_VERSION` bump.
  - **Merge rule:** each (direction, status, edge type) query fetches at most `k + 1` rows in
    index order; the streams are merged by `(edge_type, other endpoint, edge_id)`, deduplicated by
    `edge_id`, and the first *k* distinct edges are kept per frontier node. `truncated` is set
    only when more than *k* distinct eligible edges exist, so a self-loop seen in both
    directions never truncates falsely.
  - **The work bound counts the query streams too:** about `(k + 1) × frontier × directions ×
    statuses × types` rows, so each of those dimensions stays bounded.
  - **Existing vaults must get the indexes before a read-only traversal relies on them**; a fresh
    database alone is not enough.
  - **Query plans are verified** for filtered and unfiltered requests, both directions and several
    statuses: indexed searches, no temporary B-tree for `ORDER BY`. The topic-less disagreement
    seed query (not endpoint-first) gets its own supporting plan.
- **Installing the indexes** (Q3): `graph.ensure_indexes` creates every index the bounded queries
  need (the two adjacency indexes and one for the topic-less disagreement seed query, e.g.
  `(edge_type, status, src_id, dst_id)`), **creates the replacements before dropping**
  `idx_edges_src`/`idx_edges_dst`, and **validates each definition** (columns and order), not only
  its name. It is physical, not authority: it runs under the write lock with `changes=False` (lock
  order vault → write → SQLite), so outstanding preview tokens stay valid. It runs:
  - in `graph.init_db` (every writer);
  - at API startup;
  - at the end of a **successful** restore, while restore's exclusive lock still holds. A failure
    to build is reported explicitly (in the restore report, with a non-zero exit).

  Startup and restore first check that `db/graph.sqlite` exists and has a compatible schema: they
  never create an empty graph nor upgrade an incompatible one (that stays `init_db`'s hard
  failure).
- **A traversal never builds indexes.** Installation guarantees their definitions, so a read checks
  the expected names only (one `sqlite_master` lookup) and **fails closed when any is missing,
  never falling back to the unbounded fetch**: `GET /graph/neighborhood` → 503
  `graph_index_missing` ("restart the API to install graph indexes"); `/search` and `/query`
  return an empty graph group with the warning `graph_index_missing` while their other channels
  still answer.
  - Required tests: an existing vault without the indexes gets them at startup; restoring an
    older `graph.sqlite` installs them; installing twice is a no-op; a missing index degrades as
    above; installing leaves the revision unchanged and a preview token valid.
- **Limits** (Q4), initial defaults:

  | Limit | `/search`, `/query` (`policies/retrieval.yaml`) | `GET /graph/neighborhood` |
  |---|---|---|
  | fan-out *k* per node | `max_graph_fanout: 25` | `fanout_limit` parameter, default 50 |
  | disagreement seeds | `max_disagreement_seeds: 50` | — |
  | query budget per traversal | `max_graph_queries: 500` | internal, 1000 |

  **Hard ceilings apply to the policy values too** (fan-out 200, query budget 4000, the
  neighborhood maxima as shared ceilings), so a policy edit cannot remove the resource bound. The
  query budget is internal: callers tune fan-out within its ceiling, nothing else.
  - **A node is expanded only when the budget covers all of its streams** (every direction,
    status and type it needs). A budget that ran out halfway through a node's streams would
    change which edges count as its top *k*; if not enough is left, expansion stops and
    `truncated` is set.
  - Expansion follows ranked frontier order, with round-robin candidate admission across the
    expanded nodes when `node_cap` binds.
  - `budget × (k + 1)` bounds the **adjacency rows fetched**, not total database work. Seed
    selection, metadata and the output-edge query each need their own bounded, indexed path.
  - Required tests: exact budget exhaustion; too little budget for the next node; several statuses
    and edge types; `truncated` propagated to every surface. The ordinary-search eval decides
    whether these defaults keep useful coverage; the values alone cannot guarantee it.
- **Output edges: induced within fan-out** (Q5; amends ADR-0032, addendum 10). *Output edges are
  the deduplicated union of each fetched retained node's top-k incident edges, restricted to
  retained endpoints, then limited by `edge_cap`* — an approximation of the induced subgraph. A
  node's final degree can exceed *k*, because its neighbors may select further edges incident to
  it. A cross-edge outside both endpoints' top *k* is omitted (each such node's `k + 1` row has
  already set `truncated`).
  - Expanded nodes' fetched adjacency lists are reused, never queried again.
  - Outer-ring nodes (retained, not expanded) get one fan-out fetch that shares the query budget
    and reserves its complete streams per node. It **adds edges only**: it never discovers nodes
    nor deepens the traversal.
  - If the budget prevents any retained node's adjacency fetch, `truncated` is set, even when the
    node and edge counts are below their caps.
  - A deterministic final edge order (`edge_type, src_id, dst_id, edge_id`) applies before
    `edge_cap`.
  - Required tests: a retained outer-ring cross-edge is returned; one outside both endpoints' top
    *k* is omitted (with `truncated`); an edge reached from both endpoints appears once; budget
    exhaustion during outer-ring collection sets `truncated`; depth 0 (the root's own edges
    among retained endpoints only).
- **`GET /graph/node/{id}` is bounded too** (Q6). `node_view` fetched every incident edge and every
  adjacent node's metadata, so a hub tag's view had the same gap. Each (direction, edge type,
  status) group now fetches at most `limit + 1` rows through the shared indexes and fetch helper
  and returns at most `limit` (`edge_limit` parameter, default 50, maximum 200); metadata is
  loaded only for the returned endpoints. The number of groups is bounded by the supported
  direction, edge-type and status sets. A self-loop is listed once, under `outgoing`.
  - **Response-contract change:** `counts` (which presented the full degree, true only while the
    fetch was unbounded) is replaced by `returned: {outgoing, incoming}` plus
    `truncated_groups: [{direction, edge_type, status}]` and an overall `truncated` flag. No exact
    totals: counting a hub's edges is itself an unbounded scan. Its only consumers today are
    tests and `GraphNodeResponse`; `docs/Operations.md` and the API docs note the change.
  - Missing indexes: 503 `graph_index_missing`, as for the neighborhood.
  - Required tests: a large hub; a group at exactly `limit` (not truncated) vs `limit + 1`
    (truncated); incoming and outgoing edges and a self-loop; metadata loaded only for returned
    endpoints; missing indexes.
- **`truncated` is set whenever** a fan-out, seed, node or edge limit omits anything.
- **Required tests:** a large hub beside a smaller ranked seed (the smaller seed's neighbors are
  still admitted); deterministic selection; a shared edge between two frontier nodes counted
  once; disagreement-seed overflow sets `truncated`; database work is verified separately from
  response size (query plans, and rows fetched per step).

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
- **Producers** joined in slice 3 (below). Apply's claim and synthesis re-renders stay in its
  executors; only an interrupted Apply's full entry publishes those pages.
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

## Implementation — slice 3 (2026-10-29)

- **`publication.Run`**, one bulk job's publication (decision 5). For each document, under the
  write lock: `intent()` before the first write, the commit, then `publish()`, which publishes
  **that document's** pages from its own entry only (`publish(own_only=True)` leaves every other
  entry alone). Model calls for the next document run outside the lock. `finish()` is the end
  refresh, once per run: everything pending, the Source pages of the tags the run touched
  (`later_items`, a provisional widening), `index.md` and the keyword index.
  - **The run's own entry** (origin `run_<producer>`, `publication_queue.extend`) is queued before the
    first commit and kept queued before each later one. Another publication (a keep made during the
    run, a Retry) may publish and clear it; the next commit queues it again. So a crash anywhere
    leaves the end refresh to retry.
  - **While a run is going** that entry is pending: screens show **Search refresh pending** (not the
    "publication incomplete" banner) while only run entries are queued, and `validate_publication`
    fails, naming the run.
  - **A commit that may change no page** (`intent(refresh=False)`; contradictions and synthesis
    steps 1–3, a promotion that skips) asks for the end refresh only if its `publish(refresh=…)`
    says pages changed, so a run with nothing to re-project rebuilds nothing.
  - `search=False` (`rebuild_index=False`) is a producer called as a step of a caller that owns the
    refresh (tests, Apply's promotion step): it records and publishes each document but queues no run
    entry and `finish()` does nothing.
- **Each producer**:
  - **extraction** installs the outputs, log and manifest, then publishes the document's Source page
    in the same commit; the end refresh covers the keyword evidence. That closes the demo-day gap:
    an extraction no longer leaves search stale.
  - **`generate_wiki`** (as a run) records each Source page before writing it, which also queues its
    end refresh again (another publication between documents may have cleared it), and
    acknowledges the page once written (`Run.done`, so it is not rendered twice). A failed page stays
    queued, and the end refresh tries it once more. Called by publication itself or Apply it is a
    step and renders pages only.
  - **enrich** publishes the document's Source page in the artifact's commit. `scripts/enrich.py
    --no-compose` is gone: a saved artifact is always published.
  - **claims** and **items** now write each document's pages **inside its commit**: the graph, then
    its claim or tag pages (claims also re-render the partners of superseded contradictions), then the
    artifact **last**, then the Source page. The producer commit journal (below) makes an interrupted
    commit's document not fresh, so the next run redoes it, and its intent names the document's
    earlier claims or tags too, so a retry publishes them. The one
    end-of-run page pass and the scripts' all-sources `generate_wiki` call are removed. Items'
    empty-document branch now commits under the lock too (it wrote outside it before).
  - **contradictions**: steps 1–4 (apply decisions, recompute pairs, supersede stale assertions,
    re-project claim pages) are one commit with a `full` intent (its claims are not known up front).
    The verdicts after the model calls only file proposals, which no page shows.
  - **synthesis**: steps 1–3 are one commit with a `full` intent; each written topic records an intent
    naming its synthesis (its page re-renders from the artifact on retry). A topic's commit now writes
    the graph, the page, the proposal and the artifact **last**, inside a journal record.
  - **promotion**: `activate_node` now reports **every** Source page that lists the tag (activation
    changes their projection), not only on a slug move. Before, a plain approved promotion left those
    pages stale, standalone and inside Apply alike. A standalone run records an intent per
    activation and publishes those pages in its commit. As Apply's step it renders none and reports
    them, and Apply adds them to its one publication call. A failure there is
    `saved_publication_pending`, and Retry publishes it.
  - **the reindex job** is now a publication (`publish(index, keyword)`), so it also retries pending
    work. A failed step is `saved_publication_pending`, not `failed`.
- **The producer commit journal** (`app/backend/producer_journal.py`, `db/producer_journal.json`;
  review round, user decisions 2026-10-29). The review found three crash gaps. Superseding a
  document's relationships removed the links recovery and the rerun followed to its old claims and
  tags. A forced re-extraction kept its previous, still-matching artifact until the end. A synthesis
  wrote its artifact before its proposal, so a crash in between looked fresh for good. Publication
  can't close them: retiring a claim or tag that lost its evidence is producer work.
  - **Begin before the first write, finish after the last.** Under the write lock, before
    superseding, claims and items record the document with its current claims or tags; synthesis
    records the topic with the evidence fingerprint it commits. The record is removed only after
    the graph, page, proposal and artifact writes, with the rest of publication already queued.
    Publication retry never reads or clears it.
  - **Scope survives repeated failures.** A resumed commit unions the saved ids with the document's
    current ones (`begin`) and re-renders all of them, so a claim or tag the interrupted commit
    orphaned is retired. A second interruption never shrinks the set to what the first left.
  - **Never fresh while recorded.** The freshness shortcut skips a recorded subject. A synthesis
    regeneration can come from the response cache, so it need not call the provider again.
  - **Visible.** Run summaries carry `unfinished_commits` (synthesis also `unfinished_blocked` and
    `journal_verified`; claims and items `journal_reconciled`). A run that attempted a recorded
    subject and left it unfinished is `partial`. The scripts print the subjects and exit 1.
    `scripts/validate_producer_journal.py` fails while any record exists.
  - **Human decisions win.** A record bypasses freshness only. Replays still apply drops, blocks,
    hides and deprecations, as every extraction does, and `--force` still never regenerates a hidden
    synthesis.
  - **Verified before cleared.** A synthesis record is first checked for completion, with no model
    call. Each `begin` gets a new attempt id, and the commit stamps it on the artifact it writes
    last (`commit_attempt`). A matching stamp means this attempt finished. Files from an earlier
    complete commit of the same evidence never pass for it (second review round: a forced
    regeneration interrupted after superseding its edges). Without a match (an older attempt, or a
    record rebuilt by repair), the whole expected state must hold:
    - the artifact's fingerprint;
    - the node, with exactly the topic's active edges;
    - a page that is exactly the artifact's rendering at the node's status (summary and body
      both; third review round: a regeneration that kept the summary but rewrote the body left
      its own page);
    - the proposal, filed or decided.

    If the commit isn't complete and an approval, a rejection or a hide rules out regenerating it,
    the topic is **blocked**: it is reported and the record is kept.
  - **Subjects that can't be redone.** A document that is deleted or no longer extractable is finished
    from authority: its saved claims or tags are re-rendered (those without evidence retire). The
    record is cleared only once none of them still stands without evidence and every claim or tag
    it still has has a page. A claim whose wording was never written keeps it. A topic no longer
    eligible is done once it never reached the graph or a page, or once step 3 retracted its
    synthesis (node and page both `deprecated_candidate`).
  - **Fail closed.** The file is schema-validated, replaced atomically and durably, and backed up
    with `db/`. An unreadable journal raises `JournalCorrupt`: the three producers refuse (exit 5;
    503 in the API), and the validator fails. `scripts/repair_producer_journal.py` (holding the three
    run guards) keeps a copy of the damaged bytes, then rebuilds the records. The lost records can't
    be told apart, and an interrupted first commit has no artifact yet. So the inventory is
    conservative and draws on every trace a commit leaves (second review round):
    - claims and items: every extracted document (manifest), every document with an artifact, and
      every document the graph links claims or tags to. Each record holds the document's current
      ids plus those its superseded relationships left without evidence (`graph.orphans`).
    - synthesis: every active tag, plus every topic with an artifact, a synthesis page or a
      synthesis node's topic link. Fingerprint and attempt are unknown, so the next run verifies
      each against the whole expected state, and clears the topics that never reached the graph.
- **Provisional widening** grows: a provisional entry's documents add the tags they mention and the
  claims derived from them (then those tags' Source pages). A crash recovery therefore checks what the
  interrupted commit may have written, and a page it never wrote is reported (`page_missing`, whose
  hint now also says "rerun the extraction that creates it"), never skipped.
- **Job results:** every run's summary carries `publication: {status, pending, still_pending}`,
  `index_rebuilt` and `keyword_reindexed`. `publication.job_status` never reports `succeeded` while
  publication is pending (`saved_publication_pending`, a new `jobs.sqlite` status). A `partial` run
  stays `partial`. The scripts print the outcome and the pending pages, and exit 1 while anything is
  pending.
- **Docs:** Operations "Pipeline runs publish too"; Workflow, README and the UAT Guide drop the
  `generate_wiki` / `reindex_keyword` / `rebuild_index` steps after the producers (they stay as manual
  repairs).
- **Alias accumulation (user decision, 2026-10-29).** Composing each document's tag pages inside
  its commit made extraction union **every** document's body-checked aliases into a never-kept
  candidate. The old end-of-run pass kept only the last document's, so a batch and per-document
  runs disagreed. The union stays, now compared case-insensitively (`items._accumulate`, used only
  where extraction's hints are merged):
  - aliases are compared with `casefold()`; the spelling already stored is kept, and only genuinely
    new aliases are appended, up to the same limit;
  - kept tags and human-owned aliases are unchanged. A keep (even one that stopped after its ledger
    write) still makes extraction add nothing.
  - the shared `_union` keeps its exact comparison for human alias edits and their stale-form check.
- **Tests:** `tests/test_producer_publication.py` (17) covers:
  - extraction publishes Source pages and search before it reports success; a later run's new text
    is searchable at once;
  - a run refreshes `index.md` and search once, not per document;
  - a failed end refresh is `saved_publication_pending` (job row too) and `republish.py` clears it;
  - a crash mid-run leaves only the end refresh queued, and republish recovers;
  - items: each document's Source page is published before the next model call, while only the
    run entry is pending ("Search refresh pending");
  - acceptance 3 and 5 during a run: a keep made during the model call publishes at once and
    survives the run's later commit and end refresh;
  - claims: a crash after the pages and before the artifact is recovered by republish, and the next
    run redoes the document;
  - a provisional document intent widens to its tags (a missing page is reported);
  - `generate_wiki` tries a failed page once more and leaves it pending;
  - contradiction and synthesis runs end with current pages and search; a standalone promotion
    publishes every Source page that lists the tag, and inside Apply it publishes nothing itself;
  - Apply: an approved promotion without a slug change publishes every Source page listing the tag;
    a failed page is `saved_publication_pending`, and Retry publishes it;
  - aliases: a batch and per-document runs accumulate the same aliases; `large language model` and
    `Large Language Model` take one slot; re-extraction leaves a kept tag's aliases unchanged, and an
    interrupted first keep's too.

  `tests/test_producer_journal.py` (28) covers the journal:
  - a forced re-extraction with fewer claims, killed right after superseding (its matching artifact
    intact): retry reports the claims and keeps the record; the next ordinary run redoes the document
    and retires the lost claim. The same for tags;
  - two interruptions of the same document keep its original ids;
  - a run that attempted a recorded document and left it unfinished (its text changed during the
    model call) is `partial`;
  - a document no longer extractable finishes its commit from authority;
  - a drop between the interruption and the replay is respected;
  - synthesis: a crash after the page and before the proposal is refiled by the next run without
    `--force`, from the response cache; a fresh artifact with a record (the pre-fix layout) is
    redone; a commit that finished before clearing its record is verified, with no model call; an
    approval between the interruption and the replay is never overwritten; a decision that rules out
    the replay reports the topic blocked;
  - `generate_wiki`: another publication clears its marker between documents, the next page is
    written and the process dies: recovery republishes it and search; each page is rendered once;
  - second review round: a forced regeneration of the same evidence, interrupted after superseding
    its edges, is never verified by the previous commit's files; an earlier attempt's stamp never
    verifies a later attempt; a page with unreviewed new prose is never verified; `--force` with a
    record still leaves a hidden synthesis alone; repair keeps interrupted first commits that have
    no artifact (claims before any relationship, items, a synthesis after its page) and their next
    runs finish them; repair finds a document only the graph still points at, and its claims are
    never cleared while their pages are missing;
  - a damaged journal refuses the producers until repaired, and the repair over-approximates;
    invalid files are corrupt, never empty; writes need the lock; `db/` is backed up; the API's 503
    names the repair;
  - third review round: a forced regeneration that keeps the summary but rewrites the body, killed
    after re-filing the proposal and before the artifact, is never verified; a repaired record of a
    complete synthesis (no stamp) is verified by its exact page, with no model call.

  Existing tests moved to the new contract. Five test builders that extract now copy `templates/`
  (`tests/fixtures.copy_templates`), as a real vault has, because extraction publishes Source pages.
  Six tests changed an assertion:
  - the reindex failure is `saved_publication_pending` (two tests);
  - a tag page's Source links now carry their titles (the Source pages exist);
  - the contradiction rebuild count observes the shared rebuild;
  - `generate-wiki` finds the extraction's page unchanged;
  - a render-order test sees extraction's own publication render.

  47 mutation checks were run, 27 of them on the journal (the substring check the third round
  replaced fails the rewritten-body test). Two of the earlier checks needed a sharper
  test before they were caught. Four journal mutants survive by design:
  - a fresh attempt id per `begin` (verification always clears a matching record before the next
    `begin`, so reusing the id has no effect today);
  - two of the overlapping synthesis repair sources (each topic is also found by another);
  - the "page without node" case of a retired topic (the node is written before the page). Only
  extraction's first alias guard (`_emit` gives a live tag no hint aliases) survives on its own:
  the final guard at the page write re-checks the same cases, so it is defence in depth.

## Implementation — slice 4 (2026-10-29)

Decision 6 as design-locked by the slice-4 grill (Q1–Q8).
- **The revision** (`app/backend/revision.py`, `db/revision.json`, backed up): `current` initializes
  a missing file and recovers a malformed one (copy the bytes to `revision.damaged-*.json`, flush,
  then atomic replace with a new random epoch); `advance`; `new_epoch`. Every write is atomic and
  durable. An I/O failure raises `RevisionUnavailable` (API 503 `revision_unavailable`; script exit
  9).
- **The write lock** (`write_lock.hold(..., changes=True)`): the outermost hold advances the revision
  after taking the lock and **before yielding**, once per outermost hold; a mutating hold nested in
  a `changes=False` hold advances it on entry; `write_lock.invalidate()` lets Apply do it after its
  token check. Opted out: the preview's two holds and backup. Restore starts a new epoch before its
  first file and again after its last (the archive may carry an older `revision.json`), under the
  exclusive vault lock. Publication, journal repair and every other holder keep the default.
- **Authorizations** (`app/backend/apply_authorization.py`, `db/apply_authorizations.json`, never
  backed up): `issue` (prunes records of other revisions), `find`, `consume`; malformed contents are
  kept as `apply_authorizations.damaged-*.json` and replaced with an empty set; an I/O failure
  raises `StoreUnavailable`. Tokens are `apa_` + 32 hex.
- **The gate** (`app/backend/apply_gate.py`), shared by the API and `scripts/apply_reviews.py`:
  `preview` (state copy + revision under the lock, code and raw after it, the sandbox run outside
  it, then the code-fingerprint check and, under the lock again, the revision check and the record)
  and `apply` (record, revision, code, settings and options checked under the lock; consume;
  invalidate; `run_apply`). `ApplyRefused` carries the reason, HTTP status and action of Q8.
  - The code fingerprint hashes relative paths and contents of `app/`, `scripts/`, `templates/`,
    without `__pycache__/` and compiled files. The API computes the fingerprint of its own code at
    import (`main.LOADED_CODE`); `restart_required` while the disk differs.
  - **The settings fingerprint leaves out settings Apply never reads** (model, embedding, eval and
    extraction settings, and secrets): Apply is key-free and deterministic. It keeps the paths
    (relative to the vault root), the rendering knobs and **`APP_HOST`/`APP_PORT`**, which Source
    pages read for their raw-view links (review round 1 below). A test pins this.
  - A recovered revision or store adds the preview warning `revision_recovered` /
    `authorization_store_recovered`; `scripts/validate_revision.py` lists the kept copies and always
    exits 0.
- **The sandbox** (`apply_sandbox.copy_state` under the lock, `copy_code_and_raw` after it, from the
  copied manifests): SQLite files through the online backup; `-wal`/`-shm`/`-journal`, lock files,
  the LLM cache and the authorizations are never copied; code is copied without `__pycache__`.
  `build_sandbox` keeps its signature (the two steps under a `changes=False` hold). A failed copy is
  `status: failed, reason: copy_failed` (was `sandbox_build_error`).
- **Routes:** `POST /reviews/apply` takes `{"preview_token"}` and holds the new `apply` run guard;
  `POST /reviews/apply/dry-run` returns `preview_token`/`token_withheld` with `Cache-Control:
  no-store`; `GET /ui/reviews/apply` is the read-only scope page with a **Preview** button;
  `POST /ui/reviews/apply/preview` renders the diff and the token-bound form (`no-store`); a refusal
  renders its reason and action, with a Preview button except for `restart_required`.
- **`scripts/apply_reviews.py`** `preview [--json]` / `apply --token <id>`, exit codes 0–9
  (`docs/Operations.md`, *Apply tokens*).
- **Tests:** `tests/test_apply_token.py` (38 at first; 44 after review round 1) covers every test the grill required. Existing tests
  that exercise Apply's executors take a token from `tests/fixtures.authorize_apply` (recorded
  directly, so Apply still runs where the preview would not be clean); 16 files changed only that
  way. Three assertions changed: the dry-run's live-unchanged hash ignores the authorization
  record (the revision stays hashed); the sandbox-failure test expects `copy_failed` for both copy
  steps; the UI preview test posts to the new route.
- **Verification:** 1700 passed + 2 skipped; ruff and `git diff --check` clean; 15 validators pass
  on the repo. 28 mutation checks were run, all caught (two needed a sharper test first: a store
  that exists before the sandbox copy, and a backup that must leave the revision alone).
- **Cost:** a mutating hold now takes about 3.3 ms (two fsyncs: the file and its directory), against
  0.03 ms for a read-only one. Negligible per commit; the test suite, which takes thousands of
  holds, went from about 3 to about 4 minutes.
- **Review round 1 on slice 4 (2 P1s, fixed):**
  1. *An interrupted restore could revive an old token:* extraction overwrote the new epoch with the
     archived `db/revision.json`, and a kill after that skipped the final reset, while earlier
     authorizations survived. Now restore **never writes `db/revision.json` or
     `db/apply_authorizations.json` from any archive** (older ones included), and its first step
     drops every authorization and starts the new epoch, which then holds throughout. Tests: a
     restore subprocess killed at the first member after the archived revision leaves a new epoch
     and refuses the earlier token; a restore of an archive carrying both files writes neither.
  2. *The sandbox ran with other settings than the live vault:* it read no `.env`, so its Source
     pages linked port 18000 while live used 19001, and the fingerprint excluded `app_*` anyway.
     Now the preview **carries the live `.env` into the sandbox** (secrets and absolute `*_DIR` /
     `*_PATH` keys dropped; every vault path is root-derived), and refuses (`status: failed,
     reason: settings_not_carried`) when the copy's settings still differ. The bound fingerprint is
     `effective_settings_fingerprint`: the caller's settings **and** those workers load afresh from
     the vault root (`.env` and the environment), with `APP_HOST`/`APP_PORT` included, so an API and
     a shell that would render different links never share a token. Tests: a preview with a `.env`
     port of 19001 shows and applies 19001 links; changing that port before Apply refuses the
     token (a key change alone does not); a sandbox that could not take the `.env` is a failed
     preview; the carried `.env` holds no secrets or absolute paths.
  7 mutation checks on these fixes, all caught. `tests/test_apply_token.py` now has 44 tests.
- **Review round 2 on slice 4 (1 P1, 1 P2, fixed):**
  1. *Settings could change during the preview and still be authorized:* the record stored a
     fingerprint computed after the sandbox run. Now the preview **captures the settings with the
     snapshot**, under the first lock; the sandbox must reproduce exactly the captured settings
     (else `settings_not_carried`), the authorization lock checks them again (else
     `token_withheld: settings_changed`), and the record stores the captured value. Tests: `.env`
     edited during the run withholds the token; a key rotated during the run still authorizes; an
     edit made after the capture and undone before the check (A-B-A) fails the preview.
  2. *The restore crash test died during the CRC check* (`testzip` opens every member too). It now
     kills the process only once extraction has begun, at a member after `db/revision.json`, and
     asserts that a page edited after the backup was already restored before the kill.
  4 mutation checks: 3 caught; storing the fresh fingerprint instead of the captured one survives
  as an equivalent mutant (the check just before guarantees they are equal). 47 tests.
- **Seen in practice:** editing code while the suite ran made every later API Apply in that run
  refuse with `restart_required` — the rule working as intended; install code only while nothing
  previews or applies.

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
