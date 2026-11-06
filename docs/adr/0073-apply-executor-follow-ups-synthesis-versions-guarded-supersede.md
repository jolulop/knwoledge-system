# ADR-0073 — Apply executor follow-ups: synthesis decisions bound to versions, guarded supersede

- **Status:** accepted — design-locked by grill-phase 2026-11-06 (Q1–Q11). Not implemented.
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
- **Legacy-format artifact:** a valid artifact without a stored `version_digest`. The recomputed
  digest is its version digest. It is displayable and decidable as is. **No migration and no artifact
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
(`started`, `edge_written`, `loser_deprecated`; an unknown stage blocks), that `nodes` equals
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
audit record (deterministic id) → executor done (caller `end`s after publication).

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
- this operation's audit record (its deterministic id).

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
  writes nothing; `resync-mirror` preserves the journal and the authoritative page; resync refuses
  unsupported statuses; retry after interruption.

## Recommended implementation order

1. **Slice 1 — synthesis version binding** (decisions 2–8), with item 8 (decision 12).
2. **Slice 2 — guarded supersede** (decisions 9–10): durable identity journal, binding, marker,
   precedence, shared effect check, the two report-only lints.
3. **Slice 3 — `repair_supersede.py`** (decision 11) + Operations doc.

## Consequences

- Legacy synthesis decisions without a digest stop executing until decided again; the decision-10
  clean restart makes this moot for the rebuilt vault.
- Merges and splits inherit a stricter, durable identity journal.
- Some new interruption states (anything not covered by precedence rows 2, 3, 5 and 7) need the
  documented manual recovery instead of an automatic resume.
- Claim page frontmatter gains a durable marker field.
