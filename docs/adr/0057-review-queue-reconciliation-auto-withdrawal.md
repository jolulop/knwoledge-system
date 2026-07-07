# ADR-0057 — Review-queue reconciliation: symmetric auto-withdrawal of extraction-stale items

- **Status:** design-locked
- **Date:** 2026-07-07
- **Drivers:** W1 grill (pending queue ~1380 items after the ADR-0056 rollout — the semantic-layer
  bottleneck), the ADR-0055 named deferral ("auto-withdrawal of pending promotes for
  retraction-tombstoned nodes"), one external design-review round (4 blockers, all resolved)
- **Related:** ADR-0018 (promotion lifecycle), ADR-0035 (review ledger), ADR-0037 (governance
  decision vs maintenance task), ADR-0041 (scope guards), ADR-0050 (withdraw-on-merge precedent),
  ADR-0055 §5 (the deferral this closes), ADR-0056 (replacement-only supersede), ADR-0058
  (per-source review flow — built after this slice)

## Context

Tier-2 re-extraction (ADR-0055/0056) supersedes a source's `mentions` edges and recomposes the
affected nodes: a node left with zero active mentions tombstones to `deprecated_candidate` and files
a `deprecate_wiki_page` item — but its pending `promote_candidate_node` item is **left alone**
(ADR-0055 §5 deferred this deliberately). The live rollout made the cost concrete: 733 pending
promotes + 635 pending deprecates, many stale. Executors scope-guard-skip stale promotes silently
(`promote_candidates` selects only `status='candidate'` nodes), so the projector reports
`pending_apply` forever and the queue never shrinks.

Staleness is **bidirectional**: a later re-extraction can also *resurrect* a tombstoned node
(mentions return → `candidate` again), stranding its pending `deprecate_wiki_page` item in the
opposite direction.

The withdrawal primitive already exists and has two precedents: `reviews.withdraw_review_item`
(removes a still-pending item's file + writes `audit_log/<id>-withdrawn-<hex>.json`,
`decided_by: system`; "withdrawn" is an audited operation, NOT a new item status — ADR-0050) is
called today by `claims.py` when a claim tombstones (withdraws paired `resolve_contradiction`
items, "endpoint claim retracted") and by merge/rekey apply (`superseded_by_merge`). The
concept/entity tombstone path (`concepts._recompose_node`) has no equivalent call — that missing
call, made symmetric, is this slice.

## Decisions

### 1. Symmetric reconciliation, one shared function

A single reconciliation function (one interpretation, two call sites — never a second
implementation) reconciles a node's unresolved extraction-caused review items with its current
graph/wiki state:

- **Node tombstoned** (zero active mentions → `deprecated_candidate`): withdraw its unresolved
  `promote_candidate_node` item.
- **Node resurrected** (active mentions again → `candidate`/`active`): withdraw its unresolved
  recompose-provenance `deprecate_wiki_page` item (provenance gate in decision 2).

Call sites: (a) `concepts._recompose_node`, going forward, at the moment it flips the node's
status; (b) the catch-up sweep (decision 4) over the whole queue.

Scope is **extraction-created semantic review noise only**: `promote_candidate_node`, and
`deprecate_wiki_page` for concept/entity-family pages (`context.node_type ∈ {concept, entity,
person, organization, project}`). Claim/synthesis deprecates are owned by their producers (claims
already reconcile their contradiction items; synthesis withdraws its own stale pendings).
Contradiction-loser deprecates are filed pre-approved and are unreachable by construction.

**Unresolved = `pending` OR `deferred`** (both live in `pending/`; matches the merge/rekey
withdrawal boundary). Approved/rejected items are immutable audit and are **never touched**.
Every withdrawal is audited with one of four stable reasons:

- `node_tombstoned_no_active_mentions` — promote withdrawn, node is a retraction tombstone.
- `node_resurrected_active_mentions` — deprecate withdrawn, mentions returned.
- `node_already_active` — promote withdrawn, node reached `active` by another path.
- `node_missing_or_rekeyed` — subject node absent, `merged`, or `rekeyed` (identity surgery owns
  its own withdrawal at apply; this reason covers residue found by the sweep).

### 2. Provenance keying: `reason_code`, never prose, never state alone

Withdrawal of deprecates keys on a new stable field the recompose path writes going forward:
`proposal.reason_code: "no_active_mentions"` (the human prose `reason` stays alongside).

**State-keying alone is wrong**, not merely brittle: `lint.py` files `deprecate_wiki_page` for
**under-supported ACTIVE concepts** (`under_supported_concept`, <2 mentioning sources) with the
same `context.node_type` family — those nodes *always* have active mentions, so a resurrection
rule keyed on "node has active mentions" would wrongly withdraw every one of them. The item's
stored provenance, not the node's state, decides ownership.

The one-time catch-up sweep ALSO accepts the exact legacy machine constant
`"no active source mentions remain"` (written only by `concepts._recompose_node`; the lint,
claims, synthesis, and contradiction variants all differ) as a **documented migration shim** for
the pre-`reason_code` backlog. Prose matching is never generalized beyond this single constant,
and the going-forward contract is `reason_code` only.

### 3. Same-subject ownership rule

`review_id = hash(type|subject)`, and `create_review_item` is idempotent — so a lint-filed and a
recompose-filed deprecate for the same node/page **collide into one item**, and the first filer
owns the stored reason. The rule (reviewer-required): **reconciliation only owns items whose
stored `reason_code` or legacy constant is `no_active_mentions`; all other same-subject
deprecations remain human/flat-queue decisions and are never rewritten or withdrawn by the
sweep.** Consequence accepted: if lint filed first and the node later tombstones, the pending item
keeps the lint reason and reconciliation leaves it — the conservative outcome (a human decides).

### 4. Catch-up sweep: key-free deterministic script, one-time rollout

`scripts/reconcile_reviews.py` (CLAUDE.md rule 10: small deterministic script) runs the shared
reconciliation function over the whole `pending/` set against current graph/wiki state. Key-free
(no LLM), idempotent (a second run withdraws nothing new), output is **counts only** per the
output-discipline rule (withdrawn by reason code, skipped-not-owned, untouched), every withdrawal
individually audited. Rollout: run **once on the live vault before ADR-0058 is implemented**, so
the per-source flow is built and tested against the real post-cleanup queue.

### 5. Boundary: this is machine retraction of machine proposals, not governance

Withdrawal here retracts **machine-proposed, still-unresolved** items whose premise the machine
itself has since invalidated — the established audited-withdrawal class (ADR-0050), not a human
governance decision (ADR-0037 boundary). No human decision is ever created, altered, or removed;
withdrawn subjects may legitimately re-file later (`create_review_item` idempotence unaffected —
a re-filed id is a fresh pending item over a live premise).

## Tests (implementation slice)

- Withdrawal matrix: `pending` and `deferred` items withdrawn; `approved`/`rejected` untouched;
  each of the four reason codes exercised; audit entry written per withdrawal.
- Provenance gate: recompose-provenance deprecate withdrawn on resurrection; lint-filed
  under-supported deprecate for an active node **not** withdrawn; same-subject foreign-reason item
  untouched when the node tombstones (decision 3).
- Legacy shim: item carrying the exact legacy constant withdrawn by the sweep; near-miss prose not
  matched; `reason_code` present on newly recompose-filed deprecates.
- Idempotence: second sweep run over the same state withdraws zero.
- Hook: `_recompose_node` tombstone path withdraws the pending promote in the same pass;
  resurrection path withdraws the recompose-provenance deprecate.

## Deferred (named)

- `reason_code` adoption by the other `deprecate_wiki_page` producers (lint, claims, synthesis,
  contradictions) — valuable uniformity, separate small slice; nothing in this ADR depends on it.
- Generalizing reconciliation to other review types (e.g. stale `change_entity_subtype` after a
  merge) — identity surgery already withdraws at apply; revisit only with evidence of residue.
- A report-only lint check counting reconciliation-eligible items (drift signal between sweeps).
