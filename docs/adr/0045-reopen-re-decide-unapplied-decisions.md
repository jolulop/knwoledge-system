# ADR-0045 — Reopen / re-decide: reverting a recorded-but-not-yet-applied review decision

**Status:** Accepted. Design-locked **and implemented** 2026-06-27. `review_read.reopen_block_reason`
(the gate, `REOPENABLE_EFFECT_STATUSES = {PENDING_APPLY, NO_EFFECT_REQUIRED}`); `reviews.reopen_review_item`
(terminal→`pending`, clears `decided_*`/`decision_note`/`winner`, seq-suffixed reopened audit);
`main._reopen_decision` + `POST /reviews/{id}/reopen` (graph-aware projector gate) + `POST
/ui/reviews/{id}/reopen`; `review_html._reopen_section` (conditional button + required reason);
`ReviewReopen{Request,Response}`. Covered by `tests/test_reopen.py`.
Carves a controlled, audited exception to ADR-0035's terminal-decision immutability: a human can
**reopen** a terminal review item (move it back to `pending/` to be re-decided) **only when its decision's
effects are not yet live** — closing the "misclicked ADR-0044 supersede winner / wrong approve-reject is
unfixable" gap **without** building an applied-effect undo.
**Extends:** ADR-0035 (Phase 6 review UI — decide/apply decoupling, terminal-decision immutability, the A2
`effect_status` projector vocabulary), ADR-0044 (the supersede `winner` sub-outcome), ADR-0040 (apply
dry-run), ADR-0009 (loopback-only, no-auth/no-CSRF posture). Read `app/backend/review_read.py` (the
`effect_status` constants + `_effect_*` projectors + `project_review`), `app/workers/reviews.py`
(`resolve_review_item` / `defer_review_item` — the ledger primitives + audit_log writer), and
`app/backend/main.py` (`_record_decision`, the decision endpoints, the `/ui/reviews/{id}/decide` handler).

## Context

ADR-0035 makes a recorded decision **terminal/immutable**: re-sending the same decision is an idempotent
no-op, and flipping it is a 409. ADR-0044 inherits this — the supersede `winner` is locked at approve time,
so a misclick is currently unfixable, and the same applies to any wrong approve/reject. The
decide→apply split is deliberate (a decision is recorded; `POST /reviews/apply` effects it later,
idempotently), and **the ledger does not track per-item applied-ness** — instead the A2 projectors already
re-derive a per-item **`effect_status`** from the *actual* graph/wiki/manifest state:
`PENDING_APPLY` (supported, effect not yet in the world), `EFFECTED` (effect present), `NO_EFFECT_REQUIRED`
(decided but owes no world change, e.g. a rejected promotion), `APPLY_DEFERRED` (record-only type, no
Phase-6 executor), `UNKNOWN` (state absent/inconsistent — never a guess), `INVALID_SUBJECT` (malformed /
tampered). That projector **is** the "is it applied?" detector — so reopen needs no new bookkeeping.

## Decisions

**1. Reopen is a ledger workflow transition (terminal → `pending`), gated on the projector — not a
decision verb, not a new applied-flag.** Reverting is safe **iff there is no live effect to orphan**, and
the projector already reports that. Reopen reuses the *exact* `effect_status` the detail view computes; it
adds **no** durable `applied` bit (which would be a second source of truth that can drift from the world).
Consequence: unlike the graph-free approve/reject (ADR-0044), **reopen reads the graph + wiki** to project
the item — inherent, because you cannot safely revert without confirming the effect is absent.

**2. Reopenable set = the statuses that *prove* no live effect; everything else is refused.**
- **Allowed** (terminal item → `pending`): `PENDING_APPLY` (the projector verified the effect is **not** in
  the graph/wiki) and `NO_EFFECT_REQUIRED` (the decision owes **no** world change by construction, e.g. a
  rejected promotion).
- **Blocked → 409 with reason:** `EFFECTED` (the effect is live — that is the **out-of-scope** applied-undo,
  which needs real effect reversal); `UNKNOWN` (graph absent/inconsistent — **cannot confirm** not-applied,
  so the operator must restore/repair the read model first, never guess); `INVALID_SUBJECT` (tampered — do
  not offer normal operations on it); and **`APPLY_DEFERRED`** (reason `manual_effect_unknown`).

**Why `APPLY_DEFERRED` is *not* reopenable** (corrects the initial grill): it means "not applied **by this
system**," **not** "no effect exists." It is the fallback for *every* no-executor record-only type, which
bundles deliberately **manual-effect** governance actions — `delete_raw_file` and `purge_response_cache`
are executed **by hand outside `/reviews/apply`** (ADR-0036: raw deletion is manual; cache purge is manual
because it affects reproducibility) — alongside genuinely inert ones (`missing_raw_source`, the deferred
merge/split types). The projector **cannot tell** whether a manual effect already happened, so
`APPLY_DEFERRED` does not universally mean "no live effect to orphan." v1 therefore blocks the whole bucket;
a **future ADR may add a per-type allowlist** for a *truly inert* record-only type after review.

The gate is **one conservative principle** — reopen iff the projector **proves** there is no live effect —
not a per-type whitelist. For supersede specifically: an approved-with-`winner` `resolve_contradiction` is
reopenable **only** while `PENDING_APPLY` (no `supersedes` edge + loser deprecation yet); once `EFFECTED`,
reopen is refused.

**3. Surface — a dedicated `reopen` endpoint + ledger primitive + a conditional UI button.** Reopen is a
distinct workflow transition, not an overload of approve/reject (a `force` flag would erode the clean
verb=status / terminal-immutable model and make the 409-on-flip contract conditional).
- **API:** `POST /reviews/{id}/reopen` — 404 if missing; **409** if the item is not terminal (already
  `pending`/`deferred`) or its `effect_status` is blocked (with the reason); on success the item returns to
  `pending`.
- **Ledger primitive:** `reviews.reopen_review_item` moves `approved/<id>.json` **or** `rejected/<id>.json`
  → `pending/<id>.json`, sets `status: pending`, and **clears the terminal fields** `decided_by`,
  `decided_at`, `decision_note`, and the ADR-0044 `winner`.
- **UI:** the `/ui/reviews/{id}` terminal panel shows a **Reopen** button (+ a required reason input)
  **only** when the projector reports a reopenable status; otherwise it shows the inline reason it can't be
  reopened ("already applied — effects must be reversed manually" / "graph unavailable — repair the read
  model first"). `/ui/reviews/{id}/reopen` posts to the API verb.

**4. A non-empty reason is required and the prior decision is preserved in the audit.** Reopen weakens a
terminal human governance verdict (even with no live effect), so it must leave a **stronger** trail than an
ordinary decision note: `POST /reviews/{id}/reopen` **requires a non-empty `reason`** (blank/whitespace →
**400**; the UI requires the field). The primitive appends `audit_log/<id>-reopened-<seq>.json` capturing
`{reason, prior_decision, prior_status, prior_decided_by, prior_decided_at, prior_winner?}` — `<seq>` is a
monotonic suffix (count of existing reopened entries for the id, +1) so repeated decide→reopen cycles never
clobber history. The prior terminal decision's details are captured **in the reopened entry at reopen
time**, so a later re-decision overwriting `<id>-<decision>.json` loses nothing — the full causal trail is
the ordered reopened entries plus the current decision. The original `<id>-<decision>.json` decision audit
is left as-is (it is the record that that decision happened); reopen never deletes audit history.

**5. Dry-run / apply interaction is free.** Apply and the ADR-0040 dry-run only process `approved/` items;
a reopened item is back in `pending/`, so it is **naturally excluded** — no special handling. Re-deciding
it (re-approve with the correct winner) puts it back in scope. This is exactly why reopen is restricted to
the not-yet-applied window: apply simply never ran for it.

## Consequences

A misclicked supersede winner or a wrong approve/reject is now **correctable** — reopen → re-decide —
provided apply hasn't effected it yet, with a required-reason audit trail and **zero** new ledger state (the
projector is the safety oracle). ADR-0035's terminal-immutability invariant is preserved for the dangerous
case: a decision whose effects are **live** stays immutable here (its undo is a separate, harder slice).
Costs: the `reopen_review_item` primitive + the seq-suffixed reopened audit, the graph-aware `reopen`
endpoint + its `effect_status` gate, and the conditional UI button + reason input. Deferred: undoing an
**already-applied** decision (real effect reversal), merge/split identity surgery, and Phase 8 auth/CSRF.

## Tests (design intent; written at implementation)

- Reopen an approved-not-applied item (`PENDING_APPLY`) → it returns to `pending`, `winner`/`decided_*`/
  `decision_note` cleared, a `<id>-reopened-1.json` audit with the reason + prior decision/status/winner;
  it can then be re-decided (e.g. approve with the other winner).
- Reopen of an **`EFFECTED`** item → **409** (no ledger mutation); of an `UNKNOWN`/graph-unavailable item →
  **409** ("repair read model first"); of an `INVALID_SUBJECT` item → **409**.
- Reopen allowed for `NO_EFFECT_REQUIRED` (rejected promotion); **the whole `APPLY_DEFERRED` bucket is
  blocked → 409 `manual_effect_unknown`** — explicitly the manual-effect types `delete_raw_file` and
  `purge_response_cache` **and the record-only `missing_raw_source`** (no per-type allowlist in v1) — and
  any `EFFECTED` item.
- Blank/whitespace-only `reason` → **400**; a non-terminal (`pending`/`deferred`) item → **409**; a missing
  item → **404**.
- A decide → reopen → decide → reopen cycle preserves ordered `<id>-reopened-<seq>.json` history (no
  clobber); the prior `winner` is recorded in the reopened entry.
- UI: the terminal panel renders a Reopen button + reason input **only** when reopenable, and the inline
  reason otherwise; `/ui/reviews/{id}/reopen` round-trips to `pending` and PRG-redirects.
- Dry-run/apply after a reopen excludes the now-`pending` item (it is not applied).
