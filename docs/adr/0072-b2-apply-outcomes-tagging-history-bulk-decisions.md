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
   fingerprint of its decision-relevant content** (subject, proposal, evidence). At confirmation,
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
