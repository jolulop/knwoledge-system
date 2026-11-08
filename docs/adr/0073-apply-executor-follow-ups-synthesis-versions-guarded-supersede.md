# ADR-0073 — Apply executor follow-ups: synthesis decisions bound to versions, guarded supersede

- **Status:** accepted — design-locked by grill-phase 2026-11-06 (Q1–Q11). **Slice 1 (decisions
  2–8, 12) implemented 2026-11-06**. Slice 2 grilled 2026-11-08 (decisions 13–17); **slice 2
  (decisions 9–10, 13–17) implemented 2026-11-08**. Slice 3 grilled 2026-11-08 (decisions 18–23,
  amending decision 11); slice 3 not implemented.
- **Date:** 2026-11-06
- **Drivers:** ADR-0072's B2a outcomes exposed pre-existing executor behaviour ("Existing behaviour
  the outcomes made visible", items 1–8; item 1 was fixed in B2a review round 1). This ADR decides
  what to do with items 2–8.
- **Related:** ADR-0022 (page is the status authority), ADR-0031 (synthesis, contradiction,
  supersede), ADR-0045 (reopen), ADR-0048/0049 (claim and synthesis visibility), ADR-0050/0052 +
  ADR-0066 review rounds 3–4 (identity journal), ADR-0058 (approve with amendments), ADR-0066
  (tagging replaces promotion), ADR-0068 (write lock, producer journal, publication recovery),
  ADR-0069 (contradiction maintenance), ADR-0070 B4 (`approval_id`), ADR-0072 (Apply outcomes,
  bulk decisions and their stale-selection fingerprint).

## Decision 1 — triage (grill Q1)

Fix items **4** (opposite synthesis decisions), **5** (supersede without a standing contradiction)
and **8** (ineffective test fixture). Items **2, 3, 6, 7** are **dormant / deferred**: they are not
resolved, and the executors are not repaired. No current filer reaches them, which only lowers their
priority. Restoring older data or adding a filer can expose them again. Their outcomes already report
them honestly (`incomplete`, `skipped` or `verification_unavailable`).

| # | Affected path | Behaviour left in place | Reopening trigger |
|---|---|---|---|
| 2 | `app/workers/duplicates.py` `_apply_marked_duplicates` | A duplicate whose page re-render fails is still counted applied/normalized by the legacy counts (its outcome is `incomplete`/`skipped`) | Any filer for `mark_semantic_duplicate` (manual filing is an ADR-0064 deferral), or restored data holding approved duplicate items |
| 3 | `app/workers/deprecations.py` `_apply_approved_deprecations` → `claims.recompose_claim` (hidden precedence) | Deprecating a hidden claim re-renders it still hidden, so the deprecation re-applies on every Apply | Any filer for `hide_claim` (no button or producer today — `main.py` "there is no button for this yet"), or restored data with hidden claims under approved deprecations |
| 6 | `app/workers/promote.py` (candidate loop + read-only verification pass) | An active tag whose approved promotion amendments never applied is never amended (reported `postcondition_not_met` / `verification_unavailable`) | Any vault holding `promote_candidate_node` decisions (pre-ADR-0066 data, a restore from backup), or a new promotion filer. The decision-10 clean restart removes them **from that vault only** |
| 7 | `app/workers/promote.py:104` `_read_amendments` | An unreadable approved promotion file is executed as "no amendments" | Same as 6, plus a damaged review file |

## Decision 2 — three identities for a synthesis (grill Q2–Q3)

Kept apart, each with one job:

- **Input identity — `input_fingerprint`** (`synthesis._fingerprint`, unchanged: **16 hex**). The
  freshness and re-nag key. The `propose_synthesis` review id stays keyed on
  `{topic_node_id, fingerprint}`, so a rejected set of inputs is never re-filed.
- **Content version — `version_digest`** (new): **64-hex SHA-256** of deterministic canonical JSON
  (sorted keys, fixed separators) of `input_fingerprint` and every artifact field the page renders:
  `title`, `summary`, `synthesis`, `confidence`. The input fingerprint does not cover the generated
  text, so regenerating under unchanged inputs can produce different prose under the same review id.
- **Recorded human decision**: the digest of the version the human was shown, stored on the
  decision.

Timestamps never choose between decisions.

## Decision 3 — artifact validation and legacy artifacts (grill Q5)

Before computing a digest, the artifact's required fields are validated, with **no defaults**:
`input_fingerprint` a 16-hex string; `title`, `summary`, `synthesis` strings not blank after
`strip()`; `confidence` a finite number in [0, 1] (not a bool, NaN or infinity). The digest is
always **recomputed** from the validated fields.

- A stored `version_digest` must equal the recomputed one. A mismatch is malformed: a refusal.
- **Legacy-format artifact:** a valid artifact without the `version_digest` field (an explicit
  null is malformed). The recomputed digest is its version digest. It is displayable and decidable as is. **No migration and no artifact
  write** is needed. The generator writes the field on its next write.
- Missing, unreadable or malformed artifact → no mutation anywhere (Apply, decision paths).

## Decision 4 — what the human sees, and every decision path carries the digest (grill Q3)

- The `propose_synthesis` review page shows the version itself: title, summary, confidence and the
  **full synthesis text**, safely rendered (escaped). A link to the Synthesis page is navigation
  only, never the only place the version is visible.
- The digest travels through **every** decision path: the detail form, the JSON API and the B2b
  bulk reject/defer. Bulk records the digest of each item as protected by its stale-selection
  fingerprint (ADR-0072 decision 6). An API decision without a digest is refused.
- **The authority is the artifact.** Under the write lock, a decision is recorded only when the shown
  digest equals the digest recomputed from the current, valid artifact **and** the artifact's
  `input_fingerprint` equals the review's `subject.fingerprint`. The pending item's
  `proposal.version_digest` is a consistency check: when present it must agree. A disagreement
  means an interrupted regeneration, and the decision is refused until recovery ran. A legacy
  pending item with no stored digest is checked against the artifact alone. A refusal writes nothing
  ("changed — review again").

## Decision 5 — regeneration under unchanged inputs (grill Q3)

`generate_synthesis --force` on a **pending** candidate can write new prose under the same review
id. The generator then refreshes **every version-specific proposal field** of that pending item
(`version_digest`, `title`, `summary`, `claim_ids`, `synthesis_id`) in the same commit as the
artifact, with an audit entry. Inputs already decided are never regenerated (the existing guard
before the commit stays).

Holding the write lock prevents concurrent writes. It does not make the artifact and the review file
crash-atomic. The commit stays inside the **producer journal** (ADR-0068, `journal.begin` →
artifact written last with `commit_attempt` → `journal.finish`). Recovery treats an unfinished commit
as before and also verifies that the pending item's digest and the artifact's agree.

## Decision 6 — Apply executes a synthesis decision only on its own version (grill Q2–Q3)

`apply_resolved_syntheses` executes an approved or rejected `propose_synthesis` decision only when:

1. the artifact is present, readable and valid (decision 3);
2. its `input_fingerprint` equals the decision's `subject.fingerprint`;
3. the decision's recorded digest equals the digest recomputed from the artifact.

Otherwise it changes nothing. The existing visibility safeguards stay (`hidden`/`evidence_hidden`
are never flipped). With deterministic review ids, at most one decision per topic matches, so the
flip-flop disappears by construction. This also closes the `--force` hole (an old approval
publishing new, unreviewed prose). Outcomes:

- a decision on a **replaced version** → reported as information, **once per Apply result** (no
  persistent acknowledgement state), not under Attention needed;
- a legacy decision without a recorded digest on the current inputs → `skipped` /
  `decision_unbound` under Attention needed, linking to "Decide again for the version shown";
- missing/unreadable/malformed artifact → `skipped` with a reason-specific code.

## Decision 7 — "Decide again for the version shown" (grill Q4)

The existing reopen rule (refuse when the effect is live) is kept unchanged. A narrow, audited
action is added for **legacy unbound** `propose_synthesis` decisions:

- offered on the review page (detail form) and the JSON API only, never in bulk;
- under the write lock it requires: the review is still terminal and unbound, and for the current
  artifact's inputs; the artifact is valid and its recomputed digest equals the digest the human saw;
- it records the new decision on the same review id. The previous decision is preserved in a
  **uniquely identified** audit entry (old decision, new decision, digest). Retrying the same
  submission creates neither a second decision nor a second audit entry;
- recording it does **not** change the page. Apply executes it like any bound decision (the same
  decision verifies as already in effect; the opposite one executes), with its visibility safeguards.

A legacy decision on current inputs whose effect is **not** live uses the existing reopen → decide
path.

## Decision 8 — synthesis validator findings (grill Q5)

The validator (`validate_projection` or the synthesis validator, settled at implementation)
distinguishes three cases:
- **advisory:** a valid legacy artifact without a stored digest;
- **error:** invalid required fields, or a stored-digest mismatch;
- **interrupted-generation finding:** the pending proposal's digest disagrees with the artifact. Run
  the producer recovery (re-run `generate_synthesis`).

## Decision 9 — an approved supersede runs only on a standing contradiction (grill Q6)

**The real trigger.** Contradiction maintenance leaves decisions awaiting Apply alone
(`contradictions.check_assertions`), so a Details edit does not retire an approved proposal. The
claim lifecycle does: when a re-run of `extract_claims` drops a claim from its document (new document
text, a prompt-version bump such as the decision-10 restart), `claims.recompose_claim` tombstones it.
`graph.supersede_contradictions_for_claim` then supersedes every proposed **or active** `contradicts`
edge touching it, and `reviews.withdraw_review_item` withdraws only *pending* reviews. An approved
supersede awaiting Apply survives with its edge retired. Today Apply still writes `supersedes` and
deprecates the loser. If the winner was the dropped claim, a claim without evidence deprecates a
live one.

**The guard runs before every write**, including the activation of the `contradicts` edge (today it
happens before `_execute_supersede`). Endpoint state is read from the authoritative **Claim page** and
the graph mirror. A hidden page with a stale active node never passes. In order:

1. this review's complete effects already hold (decision 10) → `already_in_effect`, whatever the
   `contradicts` edge's state now;
2. the pair's `contradicts` edge exists and is `proposed` or `active` — else `contradiction_retired`
   (superseded), `edge_status_unexpected` (rejected) or `edge_missing`;
3. the winner is active in page and graph — else `winner_not_active`;
4. the loser is active in page and graph — hidden → `hidden_takes_precedence`, otherwise
   `loser_not_active`.

A failed guard writes nothing. **One effect check** serves Apply, the preview and reopen:
- complete effects belonging to this review → not reopenable;
- verified absence of effects → reopenable;
- partial or unverifiable effects → blocked, never described as "no live effect".

The missing-edge case must no longer read as UNKNOWN, and a retired edge must not hide an applied
supersede.

**No automatic undo.** A supersede that applied stays applied when its winner later loses evidence.
Reversing it is a governed decision. Lint reports it: `supersede_winner_retracted`, report-only.

Recovery wording is **reason-specific** and never assumes a cause. A missing or retired edge doesn't
prove that evidence loss caused it.

## Decision 10 — the supersede journal and the completion marker (grill Q7–Q10)

**Durable journal.** `app/workers/identity_journal.py` is reused (`reviews/in_flight/<review_id>.json`)
and made durable for every operation that uses it:
- writes are fsync'd (file, then directory), like `reviews.py`'s atomic writer;
- `set_stage` raises on a read or parse failure instead of returning, and Apply stops writing;
- a stage becomes durable only **after** the page and graph changes it certifies.

Merges and splits gain the same durability. The **publication-recovery contract is kept**: the
executor stops at its last stage, and the Apply caller calls `end` only after the Claim pages,
`wiki/index.md` and search refreshed.

**Binding.** The journal holds `{review_id, op: "supersede", nodes: [winner, loser], winner, loser,
approval_id, stage}`. Before resuming, Apply validates `op`, the stage against the allowed set
(`started`, `edge_written`, `loser_deprecated`, and `rolling_back` from decision 17; an unknown stage blocks), that `nodes` equals
`[winner, loser]`, and that the approved item's `approval_id`, winner and derived loser equal the
journal's.

**`nodes` is kept.** It is the shared field `open_operations()` and `operation_for()` read, so a
supersede journal scopes the in-flight check to its own two claims. Claim ids never overlap tag ids,
so unrelated item actions (keep, drop, human-add, block, Delete unused, extraction, `tag_links`
repair) proceed. The existing **fail-closed rule for unreadable journals is kept and documented**: an
unreadable entry of any op reports `nodes: None`, which every caller treats as "everything is in
flight", so all tag and identity actions are refused until the journal is restored or repaired.
An unreadable journal means the vault's in-flight state is unknown; that is not special-cased. A
**legacy approval without `approval_id`** starts no journal bound to a missing value: it must be
reopened and re-approved before new execution.

**Completion marker.** The loser's deprecation writes, **in the same page write**, a frontmatter
marker naming both `review_id` and `approval_id`. Every Claim rendering preserves it: hide/unhide,
re-extraction, supersede and deprecation re-renders. A deprecated loser is completion evidence
only when its marker matches the journal (both identifiers). A deprecated loser without that marker
proves nothing, since claim retraction can deprecate it independently.

**Write order:** `begin` → `supersedes` edge written and graph committed → `set_stage(edge_written)` →
loser page written with the marker (fsync) → graph mirror committed → `set_stage(loser_deprecated)` →
completion audit (decision 13) → executor done (caller `end`s after publication).

**Precedence, first match wins:**

1. journal unreadable, or its binding/op/stage invalid → **block**;
2. stage `loser_deprecated` → **audit-only completion**; the loser is not touched, even if hidden later;
3. loser page readable, `deprecated_candidate`, identifies the expected loser claim and carries the
   marker matching the journal, graph mirror still `active` → **mirror repair**: update only that
   claim's graph status, durably advance to `loser_deprecated`, finish the audit. This precedes the
   execution prerequisites, so a later-retracted winner cannot prevent completing an established
   deprecation;
4. loser page/graph unreadable, hidden or otherwise inconsistent → **block**;
5. loser deprecated in page and graph **with** the matching marker → advance the stage, finish the audit;
6. loser deprecated **without** the matching marker → **block**, report the uncertainty;
7. loser active and consistent → prerequisites hold (decision 9) → **resume** (edge if missing,
   deprecation, audit); prerequisites fail → the **completion-evidence guard** (below) runs; if it
   passes → **compensating rollback**: the `supersedes` edge this review owns (matched by
   `review_id`) is set to `superseded`, audited, and the journal ended. Outcome `incomplete` /
   `partial_supersede_withdrawn`. After a verified rollback reopen becomes available. If the guard
   refuses → **block**, report the evidence found or unreadable.

**The compensating rollback is an explicit, narrow recovery authorization.** It changes a semantic
edge, but only one this operation wrote while the journal proves its deprecation never completed. The
human decision is untouched and nothing else changes. It is not a general licence to undo resolutions.

**Completion-evidence guard, shared by both rollback paths.** Apply's compensating rollback (row 7)
and `repair_supersede.py rollback-edge` (decision 11) call one shared check before writing. Rollback
is **forbidden** when any completion evidence exists:
- a marker on the loser's Claim page matching the journal's `review_id` **and** `approval_id`
  (whatever the page status; a crash right after the marked page write leaves only this);
- a completed journal stage (`loser_deprecated`);
- this operation's completion audit (decision 13); a rollback or repair audit never counts.

Evidence that **cannot be read** (loser page, journal, audit log) **blocks** rollback too: absence
counts only when it is verified. A missing marker alone never authorizes rollback. One test suite
exercises the guard through both callers.

**No journal:**
- this review's correctly directed edge plus a loser deprecated **consistently in page and graph**,
  with the marker or as a legacy completed effect without one → `already_in_effect`, read-only (the
  exception authorizes no repair or rollback);
- nothing owned → normal guarded execution;
- an owned edge with an active loser → **blocked**, plus the report-only lint
  `supersede_state_unverified`. Its wording states the uncertainty without assigning a cause: an
  incomplete restore, lost metadata or a pre-fix partial all look the same.

Every other page/graph disagreement stays blocked and goes to the manual recovery (decision 11).

## Decision 11 — `scripts/repair_supersede.py` (grill Q11)

> **Amended by the slice 3 grill (2026-11-08):** `rollback-edge` initiates the shared rollback
> recovery and never ends the journal; Apply owns completion and journal cleanup (decision 18). It
> requires an open journal bound to the current approval; with no journal nothing is written
> (decision 19). `resync-mirror` is dropped in favour of hardened publication (decision 20). When it
> is offered, the fingerprint and the interface: decisions 21–23.

An argparse script (so `--help` is safe), read-only by default. No "complete" command exists:
asserting that an unmarked deprecation belongs to an operation would invent ownership.

- **`inspect <review_id>`** prints the journal, this review's edge, the loser's page status and
  marker, the graph mirror, the approval's `approval_id` and a **state fingerprint**. It offers a
  write command only when that command's prerequisites are established. Otherwise it says "no safe
  repair determined", with the precise uncertainty.
- **`rollback-edge <review_id> --expected-state <digest> --reason "…"`**: the compensating
  rollback, as a human choice. It sets this review's own edge to `superseded` and ends the journal.
  It runs the **completion-evidence guard** shared with Apply (decision 10): it is refused when a
  matching marker, a completed journal stage or this operation's audit exists, and when any of that
  evidence is unreadable. A completed stage or audit refuses even if the marker later disappeared.
  A legacy completed effect without a marker is never treated as an unfinished operation either:
  uncertain completed effects need governed reversal.
- **`resync-mirror <review_id> --expected-state <digest> --reason "…"`** sets only the loser's
  graph status from its readable page. It validates the page's identity and that the status is a
  supported claim status, and never copies an arbitrary frontmatter value into the graph. It
  **retains the supersede journal**: it repairs a projection only, writes no operation stage and
  no completion audit (only its own repair audit), and leaves recovery to the next Apply, which
  evaluates the precedence again (with a marked page, row 3 or 5).

Every write command:
- takes the vault write lock and refuses (exit 3) while Apply or a producer runs;
- recomputes the state fingerprint under the lock and writes nothing if it differs from
  `--expected-state` ("state changed — inspect again");
- uses a stable repair-operation id, with durable audit and recovery writes, so a retry after an
  interruption neither loses nor duplicates the audit; `rollback-edge` removes the journal last,
  `resync-mirror` never removes it;
- queues the affected derived views (Claim pages, index, search) **before** mutating, and reports
  unfinished publication honestly.

The Apply reason text for each blocked state names the inspect command. `scripts/validate_all.py`
diagnoses problems but repairs nothing. `scripts/backup.py` restore stays the last resort for an
unreadable page or journal.

## Decision 12 — item 8, the ineffective fixture

`tests/test_review_html._approved_concept_deprecation` gets a canonical item id. Its tests
**assert the intended state change** (the deprecation was executed: page and graph at
`deprecated_candidate`, outcome `applied`), so a canonical id alone can't leave another ineffective
test.

## Decision 13 — the supersede completion audit (slice 2 grill Q1, 2026-11-08)

**Why.** The supersede's only audit today is a synthetic `deprecate_wiki_page` item, filed and
approved at once. Its id is `review_id("deprecate_wiki_page", {node_id: loser, page})`: the same id as a
human's deprecation proposal for that claim, a claim tombstone's deprecation proposal
(`claims.recompose_claim`), or an earlier supersede of the same loser. `create_review_item` returns an
existing id without writing, so a second supersede would "audit" itself with another record. As
completion evidence it is ambiguous, and as an approved review it is a second persistent instruction
for the general deprecation executor to replay.

**Decision.** One dedicated completion audit per approved operation, and **no more synthetic
`deprecate_wiki_page` filing for new supersedes**. The contradiction approval authorizes the
deprecation; the completion audit records its execution. Historical synthetic items stay untouched.

- **Location and identity:** `reviews/audit_log/<contradiction_review_id>-superseded-<approval_id>.json`.
- **Body:** `event: "supersede_completed"`, `review_id`, `approval_id`, `winner`, `loser`, the
  `supersedes` edge id and `completed_at`.
- **When:** written durably (`write_json_durably`) only after the marked deprecation and the graph
  mirror are established, in the decision-10 write order and precedence.
- **Reuse:** an existing entry is reusable only when its identity (`review_id`, `approval_id`) and its
  completion payload (`event`, `winner`, `loser`, edge id) validate; recovery flushes it again
  (`sync_durably`). An unreadable or conflicting entry **blocks**; it is never overwritten.
- **Idempotent:** repeated recovery keeps the original `completed_at` and leaves exactly one entry.
- **Evidence:** only a valid `supersede_completed` entry for this review and approval counts as "this
  operation's completion audit" in the precedence rows and the completion-evidence guard. Rollback
  and repair audits never satisfy that check.

**Tests:** an unrelated human deprecation of the loser; an earlier supersede of the same loser; a
different approval generation (reopened and re-approved); malformed and conflicting completion
audits; an interruption immediately before and immediately after the audit write.

## Decision 14 — claim governance waits on an open supersede (slice 2 grill Q2, 2026-11-08)

**Why.** No claim action reads the identity journal today, and Apply runs contradictions before the
claim hide/unhide and deprecation executors. A supersede journal stays open across Applies when
publication fails, when a blocked precedence row leaves it, or after a crash. Meanwhile a hide of the
loser turns a resumable state into row 4 (manual repair), and a hide of the winner turns resume into
rollback: executor order would decide recovery semantics.

**Decision.** Competing governance **execution** waits; evidence freshness does not.

- While a supersede journal names a claim, these executions skip it with `skipped` /
  `identity_operation_in_flight`, naming the blocking review: `hide_claim`, `unhide_claim`, claim
  `deprecate_wiki_page`, and **any other supersede** involving either endpoint (it could overwrite the
  completion marker or create competing partial effects).
- The operation that owns the journal still resumes itself.
- Recording those decisions stays available; only their execution waits.
- An unreadable journal (`nodes: None`) blocks these claim-governance executions globally, as it
  already blocks tag and identity actions (fail-closed).
- The guard holds until the journal **ends**, including the publication-pending stages; deferred
  executions then run on the next Apply.
- **Not blocked:** extraction and contradiction maintenance follow the evidence, and extraction never
  skips documents for a supersede. Extraction preserves any completion marker. Maintenance never alters
  this operation's owned `supersedes` edge (`graph.supersede_contradictions_for_claim` and
  `check_assertions` touch `contradicts` assertions only; keep it that way and test it).
  Winner or loser retraction is then resolved by the decision-10 precedence.

**Tests:** hide, unhide, deprecation and a second supersede each wait on either endpoint; recording
their decisions still works; the owning operation resumes; unrelated claims stay actionable;
unreadable journals fail closed; winner and loser retraction follow the precedence with markers
preserved; deferred executions proceed once publication succeeds and the journal closes.

## Decision 15 — a decision on a retired contradiction is information (slice 2 grill Q3, 2026-11-08)

**Why.** A claim retraction supersedes the pair's `contradicts` edge, and decision 9 then refuses an
approved supersede with `contradiction_retired`. The approved item is never closed, so every Apply
reports it under *Attention needed*. Reopening and rejecting doesn't help: the rejection executor only
flips `proposed`/`active` rows, so a `superseded` edge reads as `edge_status_unexpected`, forever.

**Decision.** When the pair's contradiction is retired (`superseded`), **no journal is open, and the
absence of owned effects is verified**, an approved supersede **and** a rejection are both reported as
**information**: `skipped` / `contradiction_retired`, flagged `information`, in the preview's
*Information* group. No mutation; the decision file stays as history (no new withdrawn state).
Wording, without assigning a cause: "Nothing to do — this contradiction no longer stands (it was
retired), so the decision has nothing to act on. It is kept as a record."

- **Exceptions follow decision 10:** completed effects are checked first (`already_in_effect`); an open
  journal, partial owned effects or unverifiable state follow the precedence (resume, rollback or
  block) and are never shown as information. Unreadable recovery evidence blocks.
- The other refusals stay under *Attention needed* with reason-specific wording: `edge_missing`,
  `edge_status_unexpected`, `winner_not_active`, `loser_not_active`, `hidden_takes_precedence`.

**Re-proposal is not automatic (documented behavior).** `contradiction_check._file` keys a
`resolve_contradiction` review solely on the claim pair and skips a pair with a terminal decision
(`state == "decided"`) or an `active`/`rejected` assertion. So after a retired contradiction, a later
check of the same pair files nothing: the retained terminal decision stands and nothing is
reactivated. Any future re-proposal mechanism must ensure that the retained approval can never
authorize a new assertion.

**Tests:** repeated Apply with no writes; the approved and the rejected retired case; the journal and
owned-effect exceptions; unreadable evidence blocks; a later scoped check of the pair keeps the
terminal decision and reactivates nothing.

## Decision 16 — the completion marker's shape and rendering (slice 2 grill Q4, 2026-11-08)

**Why.** `render_claim_page` builds Claim frontmatter from scratch (no passthrough), at least one
frontmatter parser (`keyword_index.parse_frontmatter`) reads flat `key: value` lines only, and
`validate_frontmatter` has a fixed claim key list.

**Shape: three flat scalar fields**, written in the same page write as the loser's deprecation:
`superseded_by_review: "rev_<16 hex>"`, `superseded_by_approval: "<32 hex>"`,
`superseded_by_claim: "clm_<16 hex>"` (the winner). No nested map, no parser upgrade.

- **All or nothing.** Any marker field present requires all three, in canonical form. A partial or
  malformed marker is **unreadable evidence**.
- **A historical record, separate from current status.** A valid marker records that the operation
  completed. `recompose_claim` reads it from the existing page and the renderer writes it back
  unchanged through **every** render (re-extraction, a tombstone of the loser, contradiction
  re-projection, deprecation re-render, hide and unhide). Rendering never removes it; only a future
  governed reversal could.
- **Ownership** is checked against the journal: `superseded_by_review` and `superseded_by_approval`
  match the journal's `review_id` and `approval_id`, and `superseded_by_claim` matches the journal's
  winner. A wrong winner is not a match.
- **Rollback:** a matching marker forbids compensating rollback **whatever the page's current
  status** (decision 10's completion-evidence guard).
- **Completion and mirror repair** still require the exact status conditions of the decision-10
  precedence rows; a valid historical marker alone never authorizes changing a page.
- **Validator:** `validate_frontmatter` accepts the complete marker on Claim pages in every
  supported lifecycle state (`active`, `deprecated_candidate`, `hidden`), canonical and complete;
  anything else is an error.
- **A damaged marker:** `recompose_claim` leaves that page untouched, reports `marker_unreadable`, and
  continues with unrelated claims. The affected extraction or publication work is reported
  **incomplete**: never a silent success, and the graph is not updated as though that page rendered.

**Tests:** marker preservation through hide and unhide; partial markers; malformed values; a
wrong-winner binding; continued processing of unrelated claims next to a damaged marker.

## Decision 17 — a rollback consumes its approval generation (slice 2 grill Q5, 2026-11-08)

**Why.** After row 7's compensating rollback the journal ends and the approved file keeps the same
`approval_id`. With no journal, "nothing owned → normal guarded execution" would let the same approval
run again by itself once its prerequisites held again. That is reachable through the `edge_missing`
and `edge_status_unexpected` rollbacks, or through endpoint changes while no journal is open, and the
result would depend on timing.

**Decision.** A rollback **consumes** its approval generation. Restored prerequisites never silently
authorize another attempt; a new attempt requires reopen and re-approval (a new `approval_id`).

**Rollback protocol** (Apply's row 7 and `repair_supersede.py rollback-edge` alike), after the
completion-evidence guard passed:

1. durably record the journal stage `rolling_back`, bound to the `review_id` and `approval_id`;
2. supersede **only** the owned `supersedes` edge and commit the graph;
3. write (or verify and flush) the deterministic audit
   `reviews/audit_log/<review_id>-rolled-back-<approval_id>.json`
   (`event: "supersede_rolled_back"`, `review_id`, `approval_id`, `winner`, `loser`, edge id,
   `rolled_back_at`), with decision 13's reuse rules: an existing entry is reused only when its
   identity and payload validate, flushed again on recovery, never overwritten; one entry, the
   original time kept;
4. finish publication recovery, then close the journal (the caller `end`s it, as for every stage).

- **Precedence:** a journal at `rolling_back` sits directly after row 1 (an unreadable or invalid
  journal still blocks first). Every rollback recovery **runs the completion-evidence guard again
  before any further mutation**: a matching completion marker, a valid completion audit or a
  completed stage forbids it, and unreadable evidence blocks it. Only when the guard passes are steps
  2–4 resumed. Restored execution prerequisites never restart the supersede at this stage: a
  `rolling_back` journal ends in rollback or in a block, never in the supersede's execution. They
  never bypass the guard either.
- **Consumed approval:** before any execution, a valid `supersede_rolled_back` entry for this
  `review_id` and `approval_id` with no open journal gives `skipped` / `supersede_withdrawn` under
  *Attention needed*, with no writes: "This approval was withdrawn after an incomplete supersede;
  reopen it to decide again." An unfinished rollback or its publication recovery still runs first; the
  no-write outcome applies once that recovery is complete.
- **Unreadable or conflicting** rollback entries block.
- **Separate from completion evidence:** a rollback entry is never completion evidence (decision 13),
  and completion evidence never comes from it. Its only effect is to stop the same approval running
  again.

**Tests:** an interruption at every boundary of the protocol; prerequisites returning during recovery
(the rollback still finishes); repeated Apply after the rollback (no writes); unreadable and
conflicting rollback entries; a fresh approval generation executing normally; recovery at
`rolling_back` that meets matching completion evidence (marker or completion audit) makes no
mutation; completion evidence that became unreadable before recovery makes no mutation.

## Decision 18 — `rollback-edge` initiates recovery; Apply completes it (slice 3 grill Q1, 2026-11-08)

**Amends decision 11.** `repair_supersede.py rollback-edge` no longer "ends the journal" or
"removes the journal last". The script **initiates** the decision-17 rollback recovery; **Apply owns
its completion and the journal's cleanup**. The script never publishes and never closes a journal
on its own.

**Why.** A second rollback implementation would bypass what slice 2 established. Decision 17's
rollback audit is what consumes the approval generation; a script record of its own would let the
next Apply see "approved, no journal, nothing owned active" and execute the supersede again,
undoing the human's choice. Slice 2 review round 1 also made Apply the only closer of a journal,
and only once publication is `published`.

**Protocol.** One shared rollback entry point in `app/workers/supersede.py` serves Apply's row 7,
Apply's `rolling_back` recovery and the script; `_rollback`'s logic is not duplicated. Under the
write lock, after the run guards and the `--expected-state` fingerprint check:

1. the journal binding checks (decision 10 row 1) and the completion-evidence guard;
2. **queue the affected derived views** (both endpoint Claim pages, `wiki/index.md`, search)
   **before the first mutation**, the `rolling_back` transition included;
3. durably record the intent: the journal stage `rolling_back`, persisting with it the repair id,
   `actor: "repair_supersede"`, the `--reason` and the expected-state fingerprint;
4. the shared rollback: the guard again, only the owned edge set to `superseded`, the decision-17
   rollback audit (consumes the approval generation);
5. the separate repair audit (who, why, expected state, repair id), written from the values saved
   with the intent. It is never completion evidence;
6. stop. The script reports that publication is pending and that the next Apply finishes the
   recovery and closes the journal.

**Recovery uses the saved intent.** Any later Apply resuming a `rolling_back` journal that carries
repair metadata finishes steps 4–5 idempotently from the persisted values, **even if the script
never runs again**. Reuse rules as decision 13 (an existing entry is reused only when it validates,
is flushed again, never overwritten). Every recovery keeps the journal binding checks and runs the
completion-evidence guard again before any further mutation; restored prerequisites never restart
the supersede (decision 17).

**Reopen.** Writing the consumed-approval audit prevents re-execution, but while the journal is
open the effect is still in flight (`UNKNOWN`, reopen blocked). **Reopen becomes available only
after Apply closes the journal**, which it does once publication is `published`.

**Tests:** interruption before and after the intent, after the edge update, after each audit and
around publication; repeated script invocation; prerequisites restored during recovery (the
rollback still finishes); a publication failure retaining the journal; the eventual Apply closing
the journal, followed by a successful reopen.

## Decision 19 — `rollback-edge` is strictly compensating; any-generation completion guard (slice 3 grill Q2, 2026-11-08)

**No journal, no rollback write.** The state "owned active `supersedes` edge, active loser, no
journal" is **unexpected**, not impossible. Since slice 2 Apply begins the journal before the edge
and ends it only after the deprecation or the rollback, so the executor does not leave this state
on its own. But a clean restart is a planned operational boundary, not a guarantee, and a consistent
backup can still preserve an already-interrupted or historical state. Locking prevents concurrent
writes; it does not prove every recorded operation completed. Lost metadata (`reviews/in_flight/`
removed) reaches it too. In that state an edge carries only `review_id`, which survives reapproval,
so nothing binds it to a generation. `rollback-edge` therefore offers **no write**, even when the
current approval is canonical. `inspect` reports the uncertainty (an owned edge with no journal;
the generation that wrote it is unknown; "no safe repair determined") **without inferring a cause**,
and names the last resorts: `scripts/backup.py` restore or a governed reversal. Decision 11's rule
"uncertain completed effects need governed reversal" stands unamended.

**`rollback-edge` requires** (in addition to the decision-18 protocol):
- an open, readable journal whose binding matches the **current** approval's `approval_id`,
  winner and derived loser (decision 10 row 1);
- the loser **active in both page and graph**, page readable and consistent (a hidden loser stays
  blocked);
- **exactly one** owned `supersedes` edge for the pair, and it is active;
- the `--expected-state` check under the lock, the durable intent (decision 18), and Apply-owned
  closure.

**Any-generation completion guard.** The shared completion-evidence guard (decision 10) is widened,
for Apply's rollback and the script alike:
- a completion marker on the loser naming **this `review_id` under any `approval_id`**, or a valid
  completion audit (`<review_id>-superseded-<any approval_id>.json`) from **any generation**,
  **forbids** rollback;
- an unreadable or conflicting candidate audit, or an audit directory that **cannot be fully
  scanned**, **blocks** rollback: an incomplete scan never establishes absence;
- the existing rules stay (a completed journal stage forbids; unreadable page or journal blocks;
  a missing marker alone never authorizes).

*Why:* an earlier generation can complete, the loser later become active again (its marker kept,
decision 16), and the review be reapproved. A guard matching only the current `approval_id` would
then let a rollback withdraw an edge a completed generation wrote.

**Deferred — operator-governed withdrawal.** An explicit command to withdraw an uncertain
review-owned edge and consume the current approval, whose audit records that the operator withdrew
it without asserting which generation wrote it. **Trigger:** the no-journal state is actually
observed in a vault.

**Tests:** no journal with a canonical approval → no write offered or performed; a historical
marker from an earlier generation forbids rollback (the reapproval example above); a completion
audit from an earlier generation forbids; an unreadable or conflicting candidate audit blocks; an
unscannable audit directory blocks; a hidden loser blocks; two owned edges block.

## Decision 20 — `resync-mirror` is dropped; ordinary publication is hardened (slice 3 grill Q3, 2026-11-08)

**Amends decision 11.** `repair_supersede.py` keeps read-only `inspect` plus **one** governed write
command, `rollback-edge`. The `resync-mirror` command is removed: publication already mirrors a
Claim page's saved status into the graph (`claims.mirror_page_status`, run by
`publication._render_claims` on every claim it publishes, ADR-0022), so a second repair command for
the same projection is unnecessary. Ordinary projection repair writes no separate repair audit.

**One shared page-authority check.** Publication's mirror and render both go through a single check
of the Claim page before using it:
- **Identity:** the page's frontmatter `claim_id` must equal the claim being published. A
  wrong-owner page is reported `claim_identity_mismatch` and **excluded from both mirroring and
  rendering**, so neither the page nor the graph changes. (Returning `False` from the mirror alone
  is insufficient: `recompose_claim` would still render over it.)
- **Readable marker and supported status**, as today: an unreadable or damaged marker
  (decision 16) or an unsupported status mirrors nothing, and the render reports the page.
- **Durability barrier for marked pages:** before a page carrying a supersede completion marker is
  mirrored into the graph, it is flushed again (`claims.sync_page_durably`), exactly as slice 2's
  recovery does, so publication repairing a lagging mirror never bypasses that barrier. A flush
  failure leaves the graph unchanged and the page reported, and is retried on the next run.

**Journals.** Publication never touches a supersede journal. It stays open until Apply completes its
existing recovery (with a marked page, decision 10 row 3 or 5) and the published-only close
(slice 2 review round 1).

**Two publication routes, named precisely by `inspect`:**
- the affected claim is **already queued** in `db/publication_pending.json` → **Retry publishing**
  (or `scripts/republish.py`), then Apply;
- otherwise → `uv run python scripts/republish.py --all`, then Apply.

**Tests:** a foreign `claim_id` (mirror and render both excluded, page and graph unchanged,
`claim_identity_mismatch` reported); supported and unsupported statuses; damaged and unreadable
markers; repeated page-flush failure on a marked page (graph unchanged each time, repaired once the
flush succeeds); publication repairing a lagging mirror while the supersede journal stays open, then
Apply completing and closing it.

## Decision 21 — when `rollback-edge` is offered (slice 3 grill Q4, 2026-11-08)

**Offered whenever the strict preconditions hold** (decision 19, the journal at `started` or
`edge_written`, and the any-generation completion guard passing), **not only when Apply is
blocked.** The human is explicitly withdrawing an unfinished operation: completion must still be
unproven and the rollback safe. Apply's row 7 already rolls back by itself when the prerequisites
fail; the gap this covers is an operation whose prerequisites hold but which cannot, or should not,
finish. The concrete case is `marker_conflict`: the loser carries **another review's** completion
marker (an earlier supersede completed, the loser later became active again, decision 16 kept the
marker), so every Apply blocks at the recompose and never rolls back.

**What `inspect` says**, from the inspected state and the shared checks, never from the last Apply
outcome (that is a stale view):
- **a deterministic blocker** → named with its evidence, e.g. `marker_conflict` and the conflicting
  review id;
- **prerequisites currently hold** → "Apply can attempt to complete this supersede. Rolling back
  withdraws this approval; another attempt requires reopening and deciding again." It never promises
  completion: rendering or I/O may still fail;
- **already `rolling_back`** → no new write offered; the operator is directed to Apply;
- **unreadable evidence** → a refusal, reported as such. It is never described as something the
  completion-evidence guard tolerates.

**Under the lock** the write command repeats **every** check (run guards, `--expected-state`,
journal binding, preconditions, the guard) against the fingerprint it was given; any difference
writes nothing.

**Tests:** voluntary rollback of a completable operation; rollback after a foreign-marker
conflict; stale inspection (the state changed after `inspect` → nothing written); refusal after
completion evidence appears between inspect and write. Both allowed cases consume the approval
(decision-17 rollback audit) and retain the journal for Apply-owned closure (decision 18).

## Decision 22 — the `--expected-state` fingerprint (slice 3 grill Q5, 2026-11-08)

**Content-based, not a vault revision.** The vault revision (`app/backend/revision.py`) is too broad
(any tag action invalidates it) and misses edits made outside the write lock, such as a Claim page
edited by hand in Obsidian. No vault-wide revision is used.

**Shape.** A full SHA-256 hex digest (the `apply_gate.code_fingerprint` convention) over a canonical
JSON payload, beginning with a version tag `"supersede-state-v1"`. The tag changes whenever the
fingerprint's contents **or** the recovery-policy semantics change, so a fingerprint printed under an
older policy never authorizes a write under a newer one.

| Part | Content |
|---|---|
| target | the resolved vault root, the review id and both endpoint claim ids |
| review | **every** ledger location (`reviews/pending/`, `approved/`, `rejected/`), each with presence, readability and byte hash. An interrupted move can leave several copies; **ambiguous copies refuse the write** |
| journal | byte hash, or `absent` / `unreadable` |
| supersedes edges | every `supersedes` edge between the pair, both directions, owned and foreign: sorted (edge_id, src, dst, status, review_id, asserted_by) |
| prerequisites | both endpoints' graph statuses; the pair's `contradicts` edges (edge_id, status) |
| pages | byte hash of both endpoint Claim pages, or `absent` / `unreadable` |
| audits | sorted (name, byte hash) of every `reviews/audit_log/<review_id>-*` entry (completion, rollback, repair; any generation), or `unscannable` |

**One captured snapshot.** The parsed preconditions, `inspect`'s warning text and the fingerprint
are all derived from **the same captured bytes and graph rows**; files are never reread separately
for hashing and for interpretation. The locked execution check captures a **fresh** snapshot and
compares its fingerprint with `--expected-state`. `inspect` prints the fingerprint inside the exact
command line it offers.

**A matching fingerprint is necessary, never sufficient.** A fingerprint that records unreadable or
unscannable evidence can match, and the safety checks still refuse the write. Strictness is
deliberate: a re-render or any byte change means "state changed — inspect again"; a false mismatch
costs a second `inspect`, a false match could authorize a write against evidence the operator never
saw.

**Run guards stay out of it.** They are independent lock-time checks; a run starting and ending must
not invalidate an otherwise unchanged inspected state.

**Tests:** duplicate ledger copies (the write refused); a cross-vault mismatch (same review id, other
root); hand edits to an endpoint page; audit entries added or removed; a foreign edge changing; an
unrelated tag action leaving the fingerprint unchanged.

## Decision 23 — the script's interface and how Apply points to it (slice 3 grill Q6, 2026-11-08)

**Run guards.** `rollback-edge` takes, **non-blocking and in a fixed order, before the vault write
lock**, the guards of the runs whose multi-commit work can change what the fingerprint reads:
`apply`, `extract_claims`, `detect_contradictions`, `republish`, `reconcile_reviews` (the existing
entry-point names). If any acquisition fails it **releases every guard already acquired** and exits
3. Other runs (tagging, vector reindex, synthesis, Source pages) are not guarded. They can still
change these inputs indirectly, for example by draining pending publication and so refreshing Claim
pages, but their commits stay serialized by the write lock and are caught by the fresh fingerprint
check under the lock (decision 22).

**`inspect` is read-only:** no write lock with changes, no run guards, it never advances the vault
revision and never initializes a missing authority store (a missing `reviews/`, graph or audit
directory is reported as such).

**Exit codes** (aligned with `scripts/apply_reviews.py` where the meaning overlaps):

| Code | Meaning |
|---|---|
| 0 | `inspect` produced its diagnostic report (**even when it offers no safe repair**); `rollback-edge` recorded the intent and finished its steps |
| 1 | a refused write (a safety check, no safe repair) or an operational failure |
| 2 | usage (argparse; `--help` is safe) |
| 3 | a guarded run is in progress |
| 4 | the publication queue is unreadable (`scripts/republish.py --repair`) |
| 6 | state changed: inspect again (`--expected-state` mismatch) |
| 9 | `revision_unavailable` (the write lock could not advance the vault revision) |

`inspect --json` prints the snapshot facts, the offered command and the fingerprint.

**Failure after a mutation** reports honestly whether the repair intent (decision 18) was durably
recorded and what Apply must finish. It is **never** labelled "nothing changed", and its message is
distinct from a pre-write refusal; no extra exit code is needed (it exits 1).

**Apply names the inspect command** — `uv run python scripts/repair_supersede.py inspect
<review_id>` — in the next-step text for the reasons that block without a recovery of their own:
`supersede_state_unverified`, `loser_deprecated_unmarked`, `supersede_journal_invalid`,
`supersede_journal_unreadable`, `completion_evidence_found`, `rollback_evidence_unreadable`,
`completion_audit_conflict`, `rollback_audit_conflict`, `marker_conflict`,
`supersedes_edge_foreign`; and `marker_unreadable` keeps its `validate_all.py` text with the
inspect command added. The lint `supersede_state_unverified` message names it too. Not named:
`supersede_in_flight` (Apply resumes it) and the prerequisite reasons (the Information and
Attention groups explain them).

**Rollback wording.** While its journal is still open, `partial_supersede_withdrawn` says: "Finish
recovery with Apply, then reopen if another attempt is wanted." (Reopen is unavailable until Apply
closes the journal, decision 18.) `supersede_withdrawn`, which applies after closure, keeps the
direct reopen instruction.

**Docs:** a "Supersede recovery" section in `docs/Operations.md`: `inspect` first; the two
publication routes (decision 20); `rollback-edge` and the approval it consumes; Apply finishing the
recovery and closing the journal, reopen after that; `scripts/backup.py` restore as the last resort.

**Tests:** each busy guard refuses (exit 3) with nothing written; release after a partial
acquisition (no guard left held); `inspect` is read-only (no revision change, no store created);
the exit-code mapping; accurate output after an interrupted repair (intent recorded, Apply to
finish).

## Tests (required)

- **Synthesis:** an old rejection cannot override approval of the current version; an old approval
  cannot approve a replacement version; same inputs with different generated text require a new
  decision; missing, unreadable or malformed artifacts (each required field, NaN/inf/bool
  confidence, stored-digest mismatch) cause no mutation; the digest through form, API and bulk;
  "Decide again" (each lock-time check, idempotent retry, page unchanged until Apply, hidden states
  preserved); interrupted regeneration (proposal/artifact disagreement refused, recovered).
- **Supersede:** the real trigger (approve, re-run `extract_claims` dropping the winner, then
  Apply → no write) and the loser variant; missing and rejected edges; hidden endpoints (page
  hidden, graph active); an already-applied supersede whose winner retracts (stays, linted); every
  interruption boundary of the write order, including after the marked page write, after the mirror
  repair and before the stage advance; repeated recovery (one audit record, page preserved);
  foreign-owned edges; unreadable journal and recovery evidence; legacy approval without
  `approval_id`; marker preserved through hide/unhide and re-extraction; Apply's rollback refused
  by the completion-evidence guard (each kind of evidence, and each unreadable).
- **Journal durability:** merge and split recovery still pass after the durability changes; a
  supersede journal does not block unrelated item actions (its `nodes` scope it); an unreadable
  journal still blocks every tag action (fail-closed, documented).
- **Repair script:** `--expected-state` mismatch writes nothing; rollback refused after an
  established completion; a crash immediately after the marked page write → `rollback-edge` is
  refused even with no completion stage and no audit; unreadable completion evidence → rollback
  writes nothing; retry after interruption. Plus the tests listed under decisions 18–23
  (`resync-mirror` is dropped, decision 20; its projection cases move to publication).

## Recommended implementation order

1. **Slice 1 — synthesis version binding** (decisions 2–8), with item 8 (decision 12).
2. **Slice 2 — guarded supersede** (decisions 9–10, 13–17): durable identity journal, binding, marker,
   precedence, shared effect check, the two report-only lints.
3. **Slice 3 — `repair_supersede.py`** (decision 11 as amended by decisions 18–23): the shared
   rollback entry point and any-generation guard, the hardened publication authority check, the
   script (`inspect`, `rollback-edge`), Apply's inspect wording, and the Operations doc.

## Implementation notes — slice 1 (2026-11-06)

- `app/workers/synthesis_version.py`: field validation, `compute_digest`, `read`, the decision-time
  check (`decision_problem`) and the Apply binding (`apply_problem`). Reason codes:
  `synthesis_artifact_missing` / `_unreadable` / `_invalid` (with the field), `synthesis_digest_mismatch`,
  `synthesis_inputs_changed`, `synthesis_version_changed`, `synthesis_generation_interrupted`
  (decision refusals), `synthesis_version_replaced` (Apply information) and `decision_unbound`.
- The decision field is `decided_version_digest`, on the item and its audit entry; reopen clears it
  (and keeps it in the reopened audit as `prior_decided_version_digest`). The artifact and the
  pending proposal carry `version_digest`.
- The authority check runs in `main._record_pending_decision` (under the write lock) for every path:
  the form (`version_digest` hidden field), the JSON API (`version_digest` in the body; 400 when
  missing or on another type) and bulk (the confirmation page carries `vd_<rid>` beside `fp_<rid>`).
  The version is exposed as `preview.synthesis_version` on `GET /reviews/{id}`.
- Apply: the binding check runs before the visibility and in-effect checks, so a replaced or unbound
  decision is reported as such even when the synthesis is hidden. A replaced version is a `skipped`
  outcome flagged `information`, rendered in its own *Information* group.
- Decide again: `reviews.decide_again` (audit `<rid>-decided-again-<key>.json`, key over the replaced
  decision, the new decision and the digest; audit written durably first). Endpoints
  `POST /reviews/{id}/decide-again` and `POST /ui/reviews/{id}/decide-again`. The review page offers
  it only where reopen is refused; otherwise reopen → decide is the path.
- Regeneration: `reviews.refresh_pending_proposal` (audit `<rid>-proposal-refreshed-<attempt>.json`)
  runs before the artifact write; `_commit_complete` also requires the pending proposal's digest to
  match the artifact. The generator now refuses blank summary or text.
- Validator: `scripts/validate_synthesis.py` (decision 8).
- Tests: `tests/test_synthesis_versions.py`; existing synthesis fixtures now bind their decisions;
  `tests/test_review_html._approved_concept_deprecation` uses a canonical id and its tests assert
  the deprecation executed (decision 12).

### Review round 1 (2026-11-07)

- **In effect means the decided text is published.** When the graph and the page already hold the
  target status, Apply compares the page byte-for-byte with `_page_content` for the bound artifact
  at that status (`synthesis._rendering_matches`; byte-stable, no wall-clock fields). Equal →
  `already_in_effect`; different → republished through `_render_page`, including its generated
  claim links; unreadable → `verification_unavailable`. Every write is verified the same way: a
  page at the right status but with other content is `postcondition_not_met`, never `applied`.
  After a repair the next Apply writes nothing. The other hold branches (node missing, hidden,
  graph and page disagreeing on status) stay skip-only.
- **Decide-again retry durability.** A retry that finds the replacement already written repeats the
  first attempt's barriers in order: audit flush → replacement file and directory flush →
  predecessor deletion → flush of **both** terminal directories (`approved/`, `rejected/`). Both
  are flushed even when no predecessor file remains, because an earlier run may have deleted it
  and then failed to flush its directory; the retry reports completion only after both flushes
  succeed. A terminal directory that does not exist (a same-status re-decision never creates the
  opposite one) is skipped; a permission or I/O failure, or a `FileNotFoundError` for a directory
  that does exist, propagates. If the audit entry for a predecessor that is still present is
  missing or unreadable, the retry refuses (`DecideAgainRefused`) and keeps it. On the first
  attempt a torn audit entry is written again before anything moves. (Review round 2.)
- **Bulk confirmation shows what it binds.** Each `propose_synthesis` row shows the version (title,
  summary, confidence, short version, full text in a disclosure) and carries that version's digest.
  A synthesis whose version can't be decided (invalid, other inputs, interrupted regeneration) is
  listed with the reason and left out of the form.
- **Validation.** An integer `confidence` is compared exactly, so an oversized one is
  `synthesis_artifact_invalid` instead of an `OverflowError`. Legacy format is the
  `version_digest` field's absence; an explicit `null` is malformed (field `version_digest`).

## Implementation notes — slice 2 (2026-11-08)

- `app/workers/supersede.py` owns the guarded supersede: `binding`, `claim_page` (the authoritative
  page read: unreadable when the file can't be read, names another claim or has an unsupported
  status), `effect_state` (the one effect check: `complete` · `absent` · `withdrawn` · `partial` ·
  `unverifiable` · `in_flight`), `completion_evidence` (the guard `repair_supersede.py` will share),
  `prerequisite_problem` (decision 9 rows 2–4) and `execute` (the decision-10 precedence and write
  order, decision 17's rollback protocol). It commits the graph at each stage boundary.
- Strictness: a supersede needs canonical ids (`rev_<16 hex>`, `clm_<16 hex>`) — anything else is
  `invalid_subject` — and a 32-hex `approval_id` (else `approval_unbound`).
- `app/workers/claim_marker.py` parses and writes the three marker fields (decision 16).
  `claims.recompose_claim` preserves a marker through every render, adds one only via
  `supersede_marker=` (never replacing another: `marker_conflict`), returns `marker_unreadable`
  before any write for a damaged one, and fsyncs the marked page (`durable=True`). The claim worker
  reports damaged-marker pages (`claim_pages_marker_unreadable`) and the run as `partial`;
  publication lists them as failed.
- `app/workers/identity_journal.py`: durable writes (file and directory fsync), `read` and
  `JournalUnreadable`, `set_stage` raises instead of returning, `begin(binding=…)`. Merges and splits
  now commit the graph before recording `graph_done`.
- `contradictions.apply_resolved_contradictions` delegates every approved supersede to
  `supersede.execute`; the synthetic `deprecate_wiki_page` filing and `_execute_supersede` are gone.
  It returns `journaled`, and `main._run_apply` ends those journals only when the index and search
  refreshed. Decision 15's information classification also covers an **acknowledge** on a retired
  contradiction (same reasoning: nothing to act on).
- Decision 14: `deprecations` (claim deprecation, claim hide/unhide) skip a claim named by an open
  journal with `identity_operation_in_flight` and `problem.blocking_review_id`; `supersede._fresh`
  does the same for another supersede. `tag_links` repair no longer defers on a readable supersede
  journal (it names claims only); an unreadable journal still defers everything.
- `review_read._effect_contradiction` uses `effect_state` for an approved supersede before looking
  at the contradiction edge.
- Lints (report-only, `lint._check_supersedes`): `supersede_winner_retracted` (low),
  `supersede_state_unverified` (medium; never fails lint).
- `validate_frontmatter.py` accepts the complete marker on Claim pages in every supported status and
  reports a damaged one.
- The Apply reason text does not name `repair_supersede.py inspect` yet; that comes with slice 3.
- Tests: `tests/test_guarded_supersede.py`; existing contradiction, reopen and review-read
  fixtures moved to canonical ids and an `approval_id`.

### Slice 2 review round 1 (2026-11-08)

- **Recovery repeats durability barriers before acting.** `_resume` validates the recovered
  journal's binding and stage, then repeats its flush (`identity_journal.sync`: file, then
  directory) before any recovery mutation. A marked loser page is likewise validated (readable,
  marker matching review, approval and winner), then flushed again
  (`claims.sync_page_durably`) before rows 3 and 5 repair the mirror or advance the stage. A failed
  flush propagates and nothing advances; the next Apply tries again.
- **Unreadable authority is never "no marker".** `claim_marker.parse` returns `UNREADABLE` for an
  existing page whose frontmatter block can't be parsed, or that has a `superseded_by_*` line
  outside it; `claim_marker.is_unreadable` covers `DAMAGED` and `UNREADABLE`. Every consumer treats
  both as unreadable evidence: `recompose_claim` (returns `marker_unreadable` before any write — a
  Claim page with broken frontmatter is no longer silently re-rendered), `mirror_page_status`
  (mirrors nothing, so publication leaves the graph unchanged and reports the page failed),
  `supersede.claim_page` / `effect_state` / `completion_evidence` / `prerequisite_problem`, and
  `validate_frontmatter.py`.
- **Journals close only after publication completed.** Apply ends a supersede, merge or split
  journal only when the publication status is `published`; any pending work (for example an upstream
  Claim-page failure that holds index and search without setting their flags) keeps it, and the
  waiting claim governance keeps waiting. Cleanup stays with Apply: Retry publishing finishes
  publication, and the next Apply verifies completion and closes the journal, so deferred
  governance runs on a later Apply.

### Slice 2 review round 2 (2026-11-08)

- **Reserved marker keys are detected under any spelling.** `claim_marker.parse` separates key
  detection from canonical-line validation. Detection reads keys the way the project's frontmatter
  parsers (and YAML) do: strip the line, skip blank lines and `#` comments, split on the first
  colon, trim whitespace and surrounding quotes from the key. So leading indentation, whitespace
  before the colon and quoted keys are all found. Validation then accepts a field only on a line in
  the renderer's exact key spelling (`field:` at column 0, quoted canonical value; the existing
  spaces or tabs after the colon and at the end of the line stay permitted). A non-canonically
  keyed field is `DAMAGED`, never absent, so recompose refuses with `marker_unreadable` instead of
  erasing it, and publication leaves the page and its mirror unchanged.
- **Duplicates count across all spellings.** A field named twice, in any mix of spellings, is
  `DAMAGED`, so a conflicting second winner can't hide behind one parser's first-value or another's
  last-value behaviour.
- **Outside the frontmatter** the same broad detection applies, and any reserved field there makes
  the page `UNREADABLE`.
- **CRLF is a line-ending difference, not damage.** A trailing `\r` is removed before validation,
  so a canonical marker in a CRLF page parses `OK` (the page reads normally through Python's
  universal newlines, and a recompose writes it back with LF and the marker kept).

## Consequences

- Legacy synthesis decisions without a digest stop executing until decided again; the decision-10
  clean restart makes this moot for the rebuilt vault.
- Merges and splits inherit a stricter, durable identity journal.
- Some new interruption states (anything not covered by precedence rows 2, 3, 5 and 7) need the
  documented manual recovery instead of an automatic resume.
- Claim page frontmatter gains a durable marker field.
