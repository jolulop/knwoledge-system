# ADR-0044 — Supersede-via-UI: recording the contradiction winner as an approve sub-outcome

**Status:** Accepted. Design-locked **and implemented** 2026-06-27. `ReviewDecisionRequest.winner` +
the decision-time validation (`_validate_supersede_winner` 400s + `_require_active_claims` 409,
graph-free) in `app/backend/main.py`; `resolve_review_item` gained a `winner` param persisting it to the
approved item **and** the audit entry; `review_html._decision_section` renders the five-button set +
the `/ui/reviews/{id}/decide` handler translates `supersede_a/b → approve{winner}`; the
`preview_resolve_contradiction` projector wording. The supersede executor is **unchanged**. Covered by
`tests/test_supersede_via_ui.py`.
Closes the awkward/manual winner-selection step in the existing contradiction-supersede workflow: a human
reviewing a `resolve_contradiction` item can pick the **winning** claim (the loser is superseded) directly
in the review UI, recorded deterministically and applied by the **existing** executor.
**Extends:** ADR-0031 (contradiction detection + supersede effects), ADR-0035 (Phase 6 review UI;
decide/apply decoupling, terminal-decision immutability, the A1 projector), ADR-0040 (apply dry-run
preview), ADR-0009 (loopback-only, no-auth/no-CSRF posture), ADR-0030 (graph is SoT for edges). Read
`app/workers/contradictions.py` (`apply_resolved_contradictions`, `_execute_supersede`, the
`resolve_contradiction` producer), `app/backend/main.py` (`_record_decision`, the `/ui/reviews/{id}/decide`
handler), `app/backend/models.py` (`ReviewDecisionRequest`), `app/workers/reviews.py`
(`resolve_review_item`), `app/backend/review_html.py` (`_decision_section`).

## Context

Contradictions are detected and filed as `resolve_contradiction` review items
(`subject = {claim_a, claim_b}`, `proposal = {outcomes: [acknowledge, supersede, reject], confidence,
explanation, sides: [ctx_a, ctx_b]}`). The supersede executor **already exists**:
`apply_resolved_contradictions` keys off a top-level **`item["winner"]`** (∈ {claim_a, claim_b}) — when
present, an approved item executes `_execute_supersede` (an active `supersedes` edge winner→loser + the
loser deprecated to `deprecated_candidate`, the `contradicts` edge left active for the historical record).
The only gap: **nothing records `winner`**. The decide endpoints (`/reviews/{id}/approve|reject|defer`)
accept only `{note}`, and `resolve_review_item` never writes `winner`, so picking a winner today means
hand-editing the item JSON. This ADR adds the missing recording + UI affordance, reusing the executor
verbatim.

## Decisions

**1. Record the winner as an optional field on the *approve* decision (no new verb, no new type).**
The decision verbs map to review **status** (`approve→approved`, `reject→rejected`, `defer`). "Supersede"
is a **sub-outcome of an approved** `resolve_contradiction`, not its own status. So: extend the decision
body (`ReviewDecisionRequest`) with an optional **`winner`** claim_id; for a `resolve_contradiction`,
approving **with** `winner` writes `item["winner"]` (the **supersede** outcome) and approving **without**
it is **acknowledge** (the `contradicts` edge flips active, both claims stand); `reject` is unchanged. The
proposal's `outcomes` map exactly: `acknowledge = approve∅`, `supersede = approve+winner`,
`reject = reject`. `resolve_review_item` (gaining an optional `winner` param) persists `winner` **both onto the approved
item** (the executor reads it) **and into the terminal `audit_log/<id>-approved.json` entry** — included
**only when present**, so ordinary approvals/rejections are unchanged but a supersede's semantic sub-outcome
(acknowledge vs supersede-A vs supersede-B) is in the **immutable audit trail**, not merely inferable from
the item. `apply_resolved_contradictions` consumes `item["winner"]` **verbatim** — no executor change. (Rejected: a dedicated
`supersede` verb — a verb that isn't a status, breaking the verb=status model; and a separate review type
— it duplicates `resolve_contradiction` and splits one human decision across two ledger items.)

**2. Validate at decision time; the executor is unchanged; the dry-run is the backstop.**
The approve endpoint validates (fail-fast 4xx) and writes `winner` only when **all** hold; otherwise it is
an ordinary acknowledge or an error:
- the item type is `resolve_contradiction` and it is still **pending/deferred** (terminal items 409, as
  today);
- the decision is **approve** — `reject`/`defer` carrying a `winner` is a **400** (winner is meaningless
  for them);
- **canonical-shape gate FIRST (untrusted ledger):** both subject claim ids `claim_a`/`claim_b` **and**
  `winner` must match the canonical claim id (`claims.is_claim_id`, `^clm_[0-9a-f]{16}$`) — a non-canonical
  id is a **400 before any page read**, so a tampered subject/winner can never be recorded or handed to the
  executor/filesystem (a non-canonical id passing pair-membership would otherwise slip a bad id through);
- `winner` ∈ the subject pair `{claim_a, claim_b}` (else **400** — *not* a silent acknowledge);
- **both claims pass the page-frontmatter active check** — `wiki/Claims/<claim_id>.md` must **exist** for
  both claims and each frontmatter `status` must be **`active`**, else **409** ("claim no longer active /
  missing — can't supersede"). The Claim **page frontmatter is the node-status authority** (graph nodes are
  a derived index, ADR-0022/0030), so the decision endpoint reads **no graph**, stays record-only, and
  **never 503s on graph absence**; graph drift between decide and apply is handled by the dry-run/apply via
  the existing executor (not a second decision gate).

Additionally, a `winner` is **only ever valid on an `approve` of a `resolve_contradiction`**: a `winner`
on any **other review type**, or on a `reject`/`defer`, is a **400** (rejected, **never silently
ignored**).

The shared `apply_resolved_contradictions` / `_execute_supersede` stays **as-is** (idempotent: a no-op
once the `supersedes` edge + loser deprecation exist; permissive about drifted state). If state drifts
between decide and apply, **no second decision gate is invented** — the ADR-0040 dry-run is the
authoritative preview of what apply *would* do, and apply reports its outcome via the existing executor.
(Hardening the executor with an active-claim guard is explicitly **out of scope** for this reuse-only
slice; revisit only if the dry-run reveals unsafe effects from drifted state.)

**3. UI — richer action buttons translated by the decide handler (no JS, loopback-only).**
On `/ui/reviews/{id}` for a `resolve_contradiction`, render the two **sides** (`proposal.sides` +
`explanation`) and an atomic button set: **`[Acknowledge (both stand)] [Supersede: A wins]
[Supersede: B wins] [Reject] [Defer]`**. Because an HTML submit carries one name/value, the **UI** action
vocabulary is richer than the API; `POST /ui/reviews/{id}/decide` **translates**:
`acknowledge → approve∅`, `supersede_a → approve{winner=claim_a}`, `supersede_b → approve{winner=claim_b}`,
`reject → reject`, `defer → defer`. One click = one exact, atomic outcome (no radio + Approve, which is
stateful and easy to submit with no/stale winner). Non-`resolve_contradiction` types keep the plain
approve/reject/defer form. The decide handler validates `winner` only for `resolve_contradiction`
approvals and rejects an invalid winner. CSRF stays deferred under the loopback-only no-auth posture
(ADR-0009) — like every other `/ui/*` mutating form.

**4. The winner is terminal once approved (ADR-0035 invariant); a misclick is a documented v1 limit.**
Recording the winner is part of an **approve**, which is **terminal/immutable**: re-sending approve is an
idempotent no-op (it does **not** change `winner`), and flipping to reject is a 409. So a misclicked winner
**cannot be changed in v1** — the same posture as any wrong approval. Mitigations: the human reviews both
sides + the explanation before the single-click decision, and the **aggregate dry-run surfaces the effect
before the deliberate, separate Apply step**. An **un-approve / re-decide** path is a **fast-follow**, not
this slice. *(This was taken by recommendation in the grill; revisit if a "change winner while
approved-but-not-applied" exception is wanted.)*

**5. Dry-run preview is free via the ADR-0040 differ.** The dry-run runs `apply_resolved_contradictions`
on the sandbox, so an approved item with `winner` set previews the full supersede as semantic diff: a
`graph.edges_added` `supersedes` (winner→loser), the loser's `nodes_status_changed`
(`active → deprecated_candidate`), the `contradicts` edge staying active, and the re-rendered Claim-page
`wiki` diffs — live byte-identical until apply. No differ change needed.

## Consequences

The contradiction-resolution workflow closes cleanly: a reviewer picks the winner with one click, the
choice is recorded as an `approved` sub-outcome the **existing** executor already consumes, and the effect
is previewable via the dry-run before a deliberate apply — all without rebuilding the supersede executor,
adding a decision verb/type, or touching the graph-identity model. Costs: an optional `winner` on
`ReviewDecisionRequest` + its decision-time validation, `resolve_review_item` persisting it, the UI button
set + decide-handler translation, and the A1 projector/preview wording for the supersede outcome. Deferred:
the **un-approve / re-decide** path (so a misclicked winner is unfixable in v1), any executor active-claim
hardening, new contradiction *detection*, and all identity-surgery (merge/split).

## Tests (design intent; written at implementation)

- `approve {winner=claim_a}` on a `resolve_contradiction` writes `item["winner"]` **and the
  `audit_log/<id>-approved.json` `winner` field**; apply executes the supersede (active `supersedes` edge
  winner→loser, loser `deprecated_candidate`, `contradicts` active); `approve {}` is acknowledge (edge
  active, both claims stand, no `supersedes`, **no `winner` in item or audit**).
- Decision-time guards: `winner` ∉ pair → 400; non-canonical → 400; `reject`/`defer` + `winner` → 400;
  `winner` on a **non-`resolve_contradiction`** type → **400** (rejected, not ignored); a claim whose
  **Claim page is missing or `status != active`** → **409**, with **no ledger mutation**; the decision
  endpoint does **not** read the graph (no 503 when the graph is absent).
- Re-approve with a **different** winner is a no-op (terminal — `winner` unchanged); flip to reject → 409.
- `/ui/reviews/{id}/decide` translates `supersede_a/supersede_b/acknowledge` → the right approve+winner;
  the detail page renders both sides + the five buttons for a `resolve_contradiction`.
- Dry-run preview of an approved-with-winner item shows the `supersedes` edge add + loser
  `active→deprecated_candidate` + Claim-page diffs, and leaves live state unchanged.
- Executor unchanged: existing contradiction acknowledge/reject/supersede tests still pass.
