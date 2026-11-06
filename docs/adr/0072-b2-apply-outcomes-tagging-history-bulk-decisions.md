# ADR-0072 — B2: per-review Apply outcomes, tagging-history tracking, bulk reject/defer

- **Status:** accepted — design-locked by grill-phase 2026-11-04 (Q1–Q6). Not implemented. Refines
  ADR-0070 B2 (§4–§6), which stays the base design; where the two differ, this ADR wins.
- **Date:** 2026-11-04
- **Drivers:** ADR-0070 B2 was design-locked before B1b. Building it raised gaps the ADR left open:
  - the Apply result page and the B1b refusals / similar-tag advisories had no home;
  - the sandbox result gives **counts**, not per-review outcomes, so "already in effect" could only
    be guessed;
  - readable names for subjects whose pages a run deletes or moves;
  - when and how `first_tag_decision_at` is backfilled, and what a missing ledger proves;
  - how much friction bulk reject/defer needs.
- **Related:** ADR-0040 (Apply dry run + preview token), ADR-0043 (hide reindex gate), ADR-0045
  (reopen, idempotent decisions), ADR-0064 (merge filing, pair rejection), ADR-0066 (tag ledgers),
  ADR-0067 (origin guard), ADR-0068 (write lock, preview freshness), ADR-0070 (Slice B),
  ADR-0071 (B1b refusals and advisories).

## Decision 1 — three sub-slices: B2a → B2c → B2b (grill Q1)

Each is implemented, externally reviewed and committed on its own:

1. **B2a — readable Apply:** ADR-0070 §4 (the preview), **plus the Apply result page**, plus plain
   wording of the B1b refusals and the similar-tag advisories on both pages.
2. **B2c — tagging progress:** ADR-0070 §6 (not yet tagged, undecided rows, Next navigation), with
   decisions 4–5 below.
3. **B2b — flat queue:** ADR-0070 §5 (readable subject, grouping, local time, detail heading, bulk
   reject/defer), with decision 6 below.

### B2a boundaries

- **Preview sentences come from the sandbox run's outcomes; result-page sentences from the real
  run's outcomes.** No second prediction path.
- **Distinct groups:** changes, already in effect, an **attention needed** section, and
  **advisories**. An advisory never looks like a refusal: its own group, its own wording, never
  counted as blocked.
- **Attention needed** holds three visibly distinct, explicitly labelled kinds: **blocked** (refused,
  nothing changed), **incomplete** (some state **did** change; it resumes on the next Apply) and
  **outcome not reported** (the result is **uncertain**; see the technical details).
- **A recorded change is not a finished publication.** When an executor reports `applied` but the
  run's publication stays pending (`saved_publication_pending`), the result page shows the change
  as recorded **and** the publication as unfinished (with Retry publishing), never one for the
  other. Test: an `applied` outcome with publication pending.
- **Reuse `review_html.refusal_text`** for refusal sentences; the structured reason and ids stay in
  the folded technical details.
- **Unchanged:** preview tokens, freshness checks, authorization and every executor's behaviour.

### Local time (all B2 pages)

Timestamps are shown in the **runtime's detected local zone** (no new setting), each with its own
offset or abbreviation, so a timestamp on either side of a daylight-saving change shows the offset
that applied to it (e.g. `CET` / `CEST`).

## Decision 2 — explicit per-review outcomes (grill Q2)

Every Apply executor reports, besides its existing counts, one record per review it handles, in one
shared shape:

```
{review_id, outcome, reason?, details}
outcome ∈ applied | already_in_effect | skipped | incomplete
```

- **`applied`:** the executor changed state and finished.
- **`already_in_effect`:** only after the executor **verified its postcondition** (not "nothing
  happened").
- **`skipped`:** refused before any change; `reason` is the typed code (e.g. `name_taken`), with
  the structured problem in `details`.
- **`incomplete`:** execution changed some state but did not finish (e.g. a merge or split left
  in flight). Never shown as an ordinary skip; the sentence says it started and how it resumes.
- **Exactly one terminal outcome per review** the executor handles. **Aggregate counts stay
  consistent with the outcomes**, while one review may still change many pages.
- **Reporting gaps:** an approved appliable review with **no** outcome, or with **more than one**,
  is shown as **"outcome not reported — see technical details"**, never guessed into a group; the
  technical details keep everything known about it.
- **Advisories stay separate** (the ADR-0071 `advisories` lists), never an outcome.
- **Diff attribution** (`items[].effects`) stays optional supporting detail, e.g. "links on 3
  documents move" only when the preview establishes it.
- **Tests:** the reporting contract is tested alongside each executor's existing behaviour (one
  outcome per handled review, counts match outcomes, postcondition before `already_in_effect`, a
  partial run reported `incomplete`). Execution rules are never changed to simplify reporting.

## Decision 3 — names captured by the executors (grill Q3)

- Each outcome's `details` carries the **display names the executor read while acting**, captured
  **before mutation or deletion**, with explicit **before/after values** where relevant (old → new
  title, old → new type, survivor and absorbed, document titles). The sandbox preview and the real
  result use the same details; **the renderer never reads the vault**.
- **Unreadable name → the id**, never a blank; the sentence then shows the id.
- Names are escaped and display-only; structured ids stay in `details` and in the folded section
  beside each sentence.
- **Inline ids on collisions (amends ADR-0070 §4):** when two subjects on one page show the same
  display name for **different ids**, each such sentence also shows its id inline. Comparison is
  case-insensitive, after the normal display sanitization, across **all groups** on the page, for
  every named subject (both ends of a merge included). Repeated references to the **same** id are
  not a collision.
- **Tests:** deleted pages, renamed promotions, merge tombstones, unreadable names, titles that
  need escaping, identical names for different ids.

## Decision 4 — the `first_tag_decision_at` migration (grill Q4)

Refines ADR-0070 §6 "Durable evidence" and "Migration".

- **Stored per-ledger state** (in `reviews/tag_links/<source_id>.json`):
  `tagging: {state: "tagged" | "unknown", first_decision_at: <ts> | null,
  earliest_known_decision_at: <ts> | null}`.
  - *Tagged, time known:* `tagged` + `first_decision_at`.
  - *Tagged, first time unknown:* `tagged`, `first_decision_at: null`, and
    `earliest_known_decision_at` when evidence gives one.
  - *History unknown:* `unknown`.
  - *Known never tagged* is **never stored**; it is derived (decision 5).
- **Explicit repair migration:** `scripts/rebuild_tag_links.py` backfills `tagging` from the
  evidence rules of ADR-0070 §6 (kept links, drop records, attributable audit entries). Dry run by
  default; it writes only with `--apply`, under the write lock. Re-running it changes nothing.
- **Read-only display:** no screen or list ever writes a ledger to classify it.
- **A decision on an unmigrated ledger** computes the same backfill (same evidence logic as the
  repair) under the lock and persists it **inside that decision's commit**. If the decision is
  refused, neither the backfill nor the marker is written.
- **Unknown history never becomes a precise first time.** If an older ledger lacks the evidence,
  the new decision establishes that the document is tagged **now**, not that this was its first
  decision: `first_decision_at` stays `null`; the action may be recorded as
  `earliest_known_decision_at`.
- **Validation:** `validate_tag_links` reports a ledger without `tagging` as **advisory only**.
  Malformed ledgers and ledger/graph disagreement keep their existing validation behaviour.
- **Tests:** dry-run immutability, repeated migration, refused actions, a missing ledger with
  attributable audit evidence, unknown timestamps, backup and restore.

## Decision 5 — tracking completeness: `reviews/tag_links/_tracking.json` (grill Q5)

A missing ledger alone proves nothing for an older document. One vault-level **provenance record**
says from when tracking is complete: `{since: <ts>, from_start: true | false}`.

- **`from_start: true` is written only explicitly:** by fresh-vault initialization or the governed
  clean restart (ADR-0066 decision 10), through an explicit command run on purpose. **It is never
  inferred** from an absence of ledgers or audit entries: an older or partially restored vault can
  look empty too. The command refuses when a ledger or tag audit entry already exists (a safety
  check, not a certification). **A fresh code clone alone establishes nothing.**
- **The migration writes `from_start: false`**, `since` = the migration time, **only when
  `_tracking.json` is absent.** An existing record is preserved: repeated repair never advances
  `since` and never downgrades `from_start: true`, so established certainty is never lost.
- **Classification of a document:**

  | Case | Shown as |
  |---|---|
  | A ledger with `tagging` | its stored state |
  | No ledger, attributable audit evidence of a decision | tagged; `earliest_known_decision_at` from the evidence, and `first_decision_at` **only** when tracking completeness establishes that event was the first |
  | No ledger, no evidence, and `from_start: true` | **known never tagged** |
  | No ledger, no evidence, discovered **reliably after** `since` | **known never tagged** |
  | Anything else (no record, an older document, or a discovery time too imprecise to place against `since`) | **tagging history unknown** |

- **Provenance only:** a missing or unreadable `_tracking.json` never prevents ordinary tagging;
  it only reduces historical certainty (more documents show "unknown"). A malformed record is
  reported by `validate_tag_links` like a malformed ledger.
- It travels with the ledgers through backup and restore.

## Decision 6 — bulk reject / defer: two steps (grill Q6)

Refines ADR-0070 §5 "Bulk reject / defer only".

1. **Select, then confirm.** Rows are ticked on the queue (none by default, current page only);
   **Reject selected…** / **Defer selected…** opens a confirmation page. **Opening it writes
   nothing.** A separate, origin-guarded POST (ADR-0067) records the decisions. No server-side
   selection store and no Apply-style preview token.
2. **Stale-selection fingerprint.** The confirmation form carries, per review, its id **and a
   fingerprint of its decision-relevant content** (subject, proposal, evidence, and the whole
   `context`, where producers keep the actual evidence — B2b review round 1). At confirmation,
   under the write lock, a review whose fingerprint changed is skipped as **"changed — review
   again"**: an id alone can survive withdrawal and re-proposal.
3. **The confirmation page** lists each review by its readable subject and type, and explains the
   **lasting effect per review type from the repository's actual rejection rules** (e.g. a rejected
   merge blocks re-filing the pair until reopened, ADR-0064). One optional note applies to every
   decision.
4. **Eligibility** is the single-item decide's, with no type excluded, rechecked under the lock: not
   pending, already carrying that decision, retiring (ADR-0069) or changed → skipped with a reason.
   **Ids are deduplicated before processing.**
5. **Not atomic, safely retried:** completed decisions stay recorded if the batch is interrupted; a
   retry skips them without duplicating audit records (the decision primitives are idempotent,
   ADR-0045).
6. **The result page** lists recorded and skipped reviews, each linked to its review page; subjects
   and notes are escaped. No bulk action runs Apply. A mistaken reject is undone with the existing
   **Reopen**.

## Consequences

- Every Apply executor gains a reporting duty; a new executor without one shows as "outcome not
  reported" rather than mis-grouped.
- Older vaults show more "tagging history unknown" until migrated or rebuilt; the clean restart
  (with its explicit `from_start: true`) gives full certainty.
- The UAT Guide and the decision-10 restart procedure gain the explicit tracking-initialization
  step (B2c).

## Implementation — B2a (2026-11-04)

- **`app/workers/apply_outcomes.py`** (new): the shared record (`outcome`), display pairs
  (`named`, `page_name`, `item_name` — a tag is named only by its **own** page), `reconcile`
  (one outcome per handled review; missing or duplicate reports become gaps, every report kept) and
  `counts`.
- **Every Apply executor reports `outcomes`** beside its unchanged counts: `synthesis` (propose,
  hide, unhide), `contradictions`, `deprecations` (deprecate, semantic hide/unhide, claim
  hide/unhide), `duplicates`, `merges`, `retypes`, `splits`, `promote` (a new read-only pass gives
  approvals the candidate loop never visits — e.g. already active — their outcome) and `retention`
  (archive, hide, unhide documents). Each executor's docstring states where a legacy count differs
  from its outcomes (e.g. a silent no-op whose postcondition no longer holds is now a `skipped`
  outcome but stays out of the legacy `skipped` list; a hide's `normalized` is `applied` with
  `normalized: true` for syntheses, `already_in_effect` with `normalized: true` elsewhere).
  Outcome-only reason codes were added where the old code had none (e.g. `merge_unverified`,
  `graph_item_type_mismatch`, `node_not_candidate`, `page_not_active`, `edge_missing`,
  `edge_status_unexpected`, `page_status_mismatch`).
- **`incomplete`** is reported where state changed but the review didn't finish: a merge/split
  refused while its identity journal is open (`details.stage`), `partial_split_state`, a document
  status flip whose Source page re-render failed (`source_page_refresh_failed`), a duplicate whose
  page re-render failed after the edge was written. An executor exception still aborts the run as
  before (no outcome is invented for it).
- **`_run_apply`** records the handled approvals before any executor runs and returns `handled`,
  `outcomes` and `advisories` (promotions, splits); the dry run passes the sandbox's through; the
  API models gain the three fields.
- **`app/backend/apply_readable.py`** (new): groups and sentences (changes by type; one
  already-in-effect line; Attention needed with Blocked / Incomplete / Outcome not reported each
  labelled; Advisories as information only), inline ids on name collisions, plain wording for every
  executor code (`REASONS`, after `refusal_text`); never reads the vault. The preview and the
  result page lead with it; the full diff, counts and raw outcomes are folded under "Technical
  details"; validators show passed / failed / not run; a pending publication is shown as
  "recorded; publishing unfinished" with Retry publishing.
- **Tests:** `tests/test_apply_outcomes_{synthesis,deprecations,identity,retention}.py` (the
  reporting contract per executor), `tests/test_apply_readable.py` (grouping, gaps, escaping,
  collisions, advisories, publication pending, folded diff), and an end-to-end preview → Apply →
  re-preview in `tests/test_review_html.py`.

### Review round 1 on B2a (2026-11-04)

Four blocking reporting issues (one an execution regression), reproduced by the reviewer:

- **Reporting never raises, and never fabricates success.** Every reporting or verification read
  goes through `apply_outcomes.guarded`; a failed read is `UNAVAILABLE`, never a value. A
  postcondition that can't be read is `verification_unavailable` (`apply_outcomes.unverified`:
  `incomplete` if this run changed state, else `skipped`), never `already_in_effect`. (The retype
  instrumentation turned a `page_owner_unknown` refusal into a `PermissionError`.)
- **A repair is `applied`** (`normalized: true` where the legacy counter says normalized);
  `already_in_effect` is only a verified no-op. The legacy counters are unchanged.
- **Outcomes state the verified end state.** `after` values are read back, not the requested
  target. A read-back mismatch is `hidden_takes_precedence` only when hidden is verified,
  otherwise `postcondition_not_met`. Promote's read-only pass compares the approved amendments with
  the page using the executor's own amendment rules; an active tag whose amendments never applied is
  `skipped` / `postcondition_not_met`, not in effect.
- **Recovery wording by reason** (`apply_readable.RECOVERY`): Apply resumes authority work (a saved
  identity operation); Retry publishing repairs projections (pages, index, search); anything else
  says "see the details", never "the next Apply resumes it".
- **The one executor-behaviour change:** a document whose status changed but whose Source page
  re-render failed inside its executor is now added to Apply's own publication work (not filtered
  through the existing-page check, so a missing page is retried too). If publication renders it, the
  outcome is reconciled to `applied` (`page_refreshed_by_publication`); if not, it stays queued for
  Retry publishing and the outcome stays `incomplete`.
- Non-blocking, done: every change sentence links its review; collision detection also counts
  promotion after-titles and advisory names.

### Review round 2 on B2a (2026-11-04)

- **The Source retry comes from execution, not reporting.** The source executors return
  `status_changed_sources` (every document whose manifest status they wrote this run), and Apply's
  publication always includes them — so a failed refresh whose reporting read also failed
  (`verification_unavailable`, left as is) is still published or stays queued. Tests start from a
  published tag-label snapshot (an unknown mapping forces a full Source refresh and had hidden this).
- **Promotion amendments are verified from evidence bound to the approval.** Activating an approved
  promotion now writes a minimal executor record (`audit_log/<review_id>-promoted-<hex>.json`: the
  title just before execution and the effective title/aliases/description/type it produced),
  under Apply's lock like the merge/retype/split records. With that record, in effect means the
  page equals the recorded result. Without one (older promotions, or a tag activated by a keep):
  a fixed point is in effect; any certain difference is `postcondition_not_met`; the one
  unresolvable case — the approved title is the page's and the page has exactly one extra alias,
  which an executed rename (appending the previous title) and a self-title amendment with an
  unwanted alias both produce — is `verification_unavailable`. No alias is inferred to be the old
  title, by position or by the creation-time name; a record older than the approval is ignored.

### Review round 3 on B2a (2026-11-05)

- **A promotion record verifies only the approval it executed.** Matching by review id and a
  whole-second timestamp let a reopened approval re-approved within the same second borrow the
  previous record. The record now carries `approval_fingerprint`: sha256 of deterministic canonical
  JSON (sorted keys, fixed separators, list order and values preserved) of the approval's review
  id, type, subject, the amendments exactly as executed and its `decided_at`. Verification uses only
  a record with the approval's fingerprint (the latest, if several); a readable record with another
  or no fingerprint is ignored (no evidence); a record that can't be read well enough to check it
  is `verification_unavailable`. An identical re-approval verifies (the same content ran).

### Review round 4 on B2a (2026-11-05)

- **An incomplete matching record never verifies.** A record whose fingerprint matches must carry
  all four recorded fields, well typed: `title` a string that isn't blank after `strip()` (the
  recorded value itself is compared unchanged), `aliases` a list of strings, `description` a string
  or null, `item_type` a string. Anything less is `verification_unavailable`. The page is compared
  with the recorded state as recorded; no field is filled in from the page (an empty `effective`
  had made the page verify against itself).

### Existing behaviour the outcomes made visible (follow-ups, not changed in B2a)

> [!note] Triaged by ADR-0073 (grill 2026-11-06)
> Items 4, 5 and 8 are **to be fixed** (ADR-0073 decisions 2–12). Items 2, 3, 6 and 7 are
> **dormant / deferred**, not resolved: no current filer reaches them. ADR-0073 decision 1 records
> each one's affected path and reopening trigger. Item 5's trigger is the claim lifecycle, not a
> Details edit (ADR-0073 decision 9).

1. ~~Apply's Source-page refresh after a document status change is not retried~~ — **fixed in
   review round 1** (above).
2. **A duplicate whose page re-render fails** is still counted as applied/normalized by the legacy
   counts; its outcome is `incomplete` or `skipped`.
3. **A deprecation of a hidden claim** re-renders it still hidden (hidden wins), so it re-applies on
   every run.
4. **Opposite `propose_synthesis` decisions on one topic** (approved and rejected, different
   fingerprints) re-render the topic twice on every run.
5. **An approved supersede whose contradicts edge is missing or rejected** still runs and reports
   `applied`, leaving no active contradicts edge.
6. **An active tag whose approved promotion amendments never applied** (e.g. approved after a
   keep activated it) is never amended; now reported `postcondition_not_met` (or
   `verification_unavailable` when the evidence can't settle it).
7. **An unreadable approved promotion file** is executed as "no amendments" (`_read_amendments`),
   silently applying none.
8. **A legacy test fixture** (`tests/test_review_html._approved_concept_deprecation`, non-canonical
   `itm_x`) has been refused by the B1 page guard since B1; the old count-only assertions never
   showed it. The readable page does.

## Implementation — B2c (2026-11-05)

- **`app/workers/tag_history.py`** (new): the tracking record (`load_tracking`,
  `tracking_problem`, `init_fresh`, `ensure_migration_record`, `complete_for`), attributable tag
  audit evidence (`audit_evidence`: tag events carrying a `source_id`), the strict `tagging` schema
  (`tagging_problem`), the shared evidence backfill (`backfill`), the decision marker
  (`mark_decision`) and the read-only display state (`classify`: tagged / never_tagged /
  history_unknown).
- **Ledger:** `tagging` is an optional ledger field, strictly validated (malformed → the ledger is
  damaged, like any other field); load and save carry it, so every ledger writer preserves it.
- **What sets it:** `tag_links.keep` (Add tag and "use X" go through it) and `drop` compute the
  marker from the ledger as loaded and persist it with their own ledger write; `block` sets it only
  when called from a document's screen (`source_id`, also recorded in the `tag-blocked` audit
  entry). A refused action writes nothing; `undo_drop` never clears it.
- **Migration and initialization:** `tag_links.migrate_tagging` (via
  `scripts/rebuild_tag_links.py`, dry run by default, `--apply` under the write lock, run
  independently of the graph repair) and `--init-fresh-tracking`. `validate_tag_links` reports a
  malformed `_tracking.json` as an error and unmarked ledgers as an advisory note.
- **Display:** `review_read` classifies each document (an unmigrated ledger shows the backfill the
  migration would store, never written on display) and splits the existing `remaining` rule into
  `undecided: {tags, type_changes, retirements, total}`. `next_source` navigates forward in list
  order, wrapping once, never the current document. The document list shows a *not yet tagged* /
  *tagging history unknown* badge, the counts and **Next untagged document**; the document screen
  shows the undecided count beside Save and **Next document with undecided rows** (replacing "Next
  source"); the Save result page warns (never blocks) with counts recomputed after the save.
- **Docs:** the UAT Guide's deterministic build and ADR-0066 decision 10 gain the explicit
  initialization step.
- **Tests:** `tests/test_tagging_history.py` (marker, tracking, migration, undecided rows,
  navigation, the scripts).

### Review round 1 on B2c (2026-11-05)

- **Evidence existence is tracked apart from timestamp availability.** A kept/drop entry or an
  attributable tag audit event (one naming the `source_id`) is evidence of a decision whether or
  not its timestamp is usable (`audit_evidence` returns `{times, untimed}`). Any evidence means
  tagged: `earliest_known_decision_at` is the earliest usable time (or null); `first_decision_at`
  only when tracking is complete **and** no evidence is untimed (an untimed decision could precede
  the earliest dated one). Untimed evidence without a ledger is never shown as not yet tagged.
- **The decision being recorded is dated evidence:** it supplies `earliest_known_decision_at` when
  every older piece of evidence is untimed; `first_decision_at` stays null.
- The list and screen say "none known to be untagged" when unknown histories remain.

## Implementation — B2b (2026-11-05)

- **Readable subject** (`review_read.readable_subject`): a tag by its page title, "X into Y" for a
  merge, "A vs B" for a contradiction, a document by its effective title, a split as
  "P → split off “S”", a type change as "T → type"; each part falls back to its id. It is the
  queue's "What" column (linking to the review) and the detail page's heading, with the type and
  the id beside it.
- **Grouping and time:** queue rows are grouped by review type (readable type names, with counts);
  times are shown with `review_html.local_time` — the runtime's local zone, each timestamp with the
  zone that applied to it (CET / CEST), non-timestamps unchanged.
- **Bulk reject / defer** (pending and deferred views only; no row ticked by default; the current
  page's rows only): **Reject selected…** / **Defer selected…** post to
  `/ui/reviews/bulk/confirm`, which writes nothing and lists each review's subject, type and
  lasting effect (`review_html.lasting_effect`: a rejection blocks re-filing that proposal; a
  rejected merge also blocks the pair both ways; contradictions and syntheses carry the rejection
  out at the next Apply; a deferral changes nothing), with one optional note. Its form carries each
  review's `review_read.decision_fingerprint` (sha256 of the canonical type, subject, proposal and
  evidence). `/ui/reviews/bulk` (origin-guarded like every POST) records under the write lock,
  through the single-item `_record_decision`: ids deduplicated; a review not found, no longer open,
  already carrying the decision or whose fingerprint changed ("changed — review again") is skipped
  with its reason. Not atomic; a retry skips what was recorded and never duplicates an audit entry.
  The result page links every review and escapes subjects and notes; no bulk action runs Apply.
- **Tests:** `tests/test_flat_queue.py`.

### Review round 1 on B2b (2026-11-05)

- **The fingerprint hashes the whole `context`.** Producers store the real evidence there (a
  merge's `match_evidence`, a contradiction's `judged_evidence` and scope); hashing only a top-level
  `evidence` field let a bulk rejection go through after the evidence changed. Conservative: any
  change inside `context`, nested timestamps included, makes the selection stale ("changed —
  review again"). No producer rewrites a filed review's `context` in place (filing is idempotent),
  so routine confirmations aren't affected. The retirement marker is a top-level field: a review
  being retired is refused by the decision path itself, and the rest of the batch proceeds.
