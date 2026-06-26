# ADR-0041 — Governance executor risk classification + mark_semantic_duplicate (first non-rekeying executor)

**Status:** Accepted. Design-locked 2026-06-26 via a grill gate — **design only, not yet implemented**.
This ADR locks (A) the durable risk taxonomy that gates which governance executors are buildable now vs.
deferred, and (B) the full contract for the first one, `mark_semantic_duplicate`. The identity-surgery
executors (`merge_entities`, `merge_concepts`, `split_entity`) are **deferred to their own ADR**.
**Extends:** ADR-0035 (Phase 6 review UI; decide/apply decoupling, executor-backed vs record-only, the A1
per-type `ReviewPreview` projector registry), ADR-0029/0030 (graph is the SoT for edges; the `duplicates`
edge type), ADR-0021 (stable node-id permanence; slug/title may change, id never), ADR-0040 (apply
dry-run preview), ADR-0036 (Phase 7 maintenance executors). Read `app/workers/reviews.py` (`REVIEW_TYPES`),
`app/backend/review_read.py` (`EXECUTOR_BY_TYPE`, projectors), `app/backend/graph.py`
(`EDGE_TYPES`/`SAME_TYPE_EDGES`), `app/backend/graph_read.py` (`SYMMETRIC_EDGE_TYPES`),
`app/workers/{retention,deprecations,contradictions}.py` (executor precedent).

## Context

Every governance review type already exists in `REVIEW_TYPES` (the ledger is decide-complete), but the
identity-changing and visibility-changing ones are **record-only** — no executor realizes them. We want to
start realizing them, but the highest-risk class (entity/concept merge, entity split) rewrites stable node
ids and must redirect every backlink, citation, review subject, and provenance edge — the hardest
invariants in the system. This ADR sets the bright line for what is safe to build incrementally and locks
the first executor, deliberately chosen to be the lowest-risk one that still exercises the new ADR-0040
dry-run path end to end.

## Decisions

**A. Risk bright line = stable-id rekeying.**
The durable axis that gates buildability is **whether the executor changes, collapses, spawns, or redirects
stable node ids** (`concept_id`/`entity_id`/… and thus backlinks/citations/derived edges/review subjects).
- **Non-rekeying → buildable now, one small slice each:** `mark_semantic_duplicate`, `hide_content`,
  and `change_entity_subtype` **only if** it preserves the same id and is strictly a type/status/projection
  change. (Flag: if an entity subtype change would alter the id prefix — e.g. `per_`/`org_`/`prj_` — that is
  rekeying and is **deferred**, not part of the non-rekeying bucket.)
- **Rekeying → deferred to a dedicated identity-surgery ADR:** `merge_entities`, `merge_concepts`,
  `split_entity`. These collapse/spawn ids and must rewrite every reference; they get their own ADR with a
  careful redirect/backlink/citation-rewrite design.
- **Reversibility and retrieval-visibility are secondary axes, not the gate:** some reversible changes are
  still graph-wide and dangerous, and the system already has status/eligibility mechanisms for visibility;
  rekeying cuts deeper because it changes *what ids mean*.

**B. `mark_semantic_duplicate` effect = pure annotation (no rekeying, no suppression, no redirect).**
Approve → upsert an **active** `duplicates` assertion between the two same-type nodes; **preserve both node
ids, both wiki pages, both node statuses, and retrieval eligibility**. It records reviewed semantic
knowledge and nothing else. Suppressing the non-canonical copy is a *separate* policy decision closer to
`hide_content` and is **out of scope**; redirecting backlinks would be a soft merge and belongs in the
deferred identity-surgery ADR. The page re-render (decision C) **rewrites only the `## Duplicates` body
section** and **preserves every page-owned metadata field** — `title`, `aliases`, `status`, `confidence`,
`review_status`, `generation_status`: the graph is authority for the *edge/backlink*, **never** for page
metadata (ADR-0017/0021/0022).

**C. Projection = graph edge + a narrow `## Duplicates` wiki section on both pages.**
The graph edge is the durable assertion, but the wiki is the human review surface, so the executor also
**re-renders both affected same-type pages** with a symmetric `## Duplicates` backlink section (active
edges only; omitted when empty; each page links to the other by its existing slug/path), mirroring the
established `contradicts` projection precedent. The section is generic across the semantic node families
that can be duplicates (`concept`/`entity`/`person`/`organization`/`project`). It is **body-only** — no
`duplicates:` frontmatter key (like the Claim "Contradicting Claims" backlinks), so the page schema is
unchanged and there is one projection surface to validate. It renders for **every active `duplicates`
edge touching the page's node, regardless of either endpoint's lifecycle status** (candidate /
deprecated_candidate included — projection follows the *edge*, as all graph projections do; node status
governs authority/retrieval eligibility separately, and seeing that a deprecated/candidate page is a
duplicate is exactly what a later governance decision needs). The section is **human-navigation only and
confers no answer-eligibility** — a candidate node stays answer-ineligible; retrieval eligibility remains
governed by node status, never by the duplicates section. **No canonical-winner language**, no status
change, no retrieval-eligibility change. The `duplicates` type is already a
`SAME_TYPE_EDGES` / `SYMMETRIC_EDGE_TYPES` member, so `GET /graph/node` surfaces it too.

**`validate_projection` gains `duplicates` coverage** on the semantic page types, **both directions** — the
same wiki↔graph gate it already enforces for `mentions`/`derived_from`/`contradicts`: an **active**
`duplicates` edge **missing** from either page's `## Duplicates` section is an error, and a `## Duplicates`
link with **no active** `duplicates` edge is an error. Without this the new section would be the only
graph-backed projection not enforced against the graph.

**D. Subject contract + edge identity + scope-guard skips.**
- **Subject:** an unordered, canonicalized pair — `subject = {"node_ids": [a, b]}` with the ids **sorted**
  before `review_id` generation (so re-proposal is idempotent and there is no "winner"). Exactly two ids.
- **Edge:** canonical `duplicates(min_id, max_id)`, `status="active"`, `asserted_by="human"`,
  `review_id` = the approved item's id; idempotent upsert (re-apply writes no duplicate row).
- **Canonical ordering is validator-enforced:** extend `validate_graph`'s existing `contradicts`
  canonical-ordering check to `duplicates` — a non-canonical row (`src_id >= dst_id`) is a hard error — so a
  reversed pair can't enter via a tampered DB / raw SQL and pass validation. (Generalizing the check to all
  symmetric edge types is the natural form.)
- **Scope guards (skip with a reason, never partial-apply or crash — mirrors `apply_archive_sources`):**
  `malformed_subject` (missing / non-list / wrong-length `node_ids`), `invalid_node_id`, `node_missing`
  (a node absent from the graph), `type_mismatch` (cross-type forbidden — `duplicates` is SAME_TYPE),
  `self_duplicate` (a == b). **`invalid_node_id` is defined conservatively** — an unsafe/path-like/empty id
  (the `safe_child` runtime-guard notion, ADR-0009/0037) — **not** a stricter concept/entity-family prefix
  grammar, which is **not** validator-fixed today (`validate_graph` defers that grammar) and must not be
  invented here. Graph existence is the `node_missing` guard.

**E. Lifecycle + classification.**
- **Approve →** upsert the active edge + re-render both pages. **Reject →** **no-op** (ledger/audit only):
  the review item *is* the proposal (no producer pre-creates a `proposed` edge), so there is nothing to
  transition and writing a `rejected` row would pollute the graph to record a negative.
  `decision_apply_required(mark_semantic_duplicate, "approved") == True`,
  `(…, "rejected") == False`; it is **not** in `_REJECT_HAS_EFFECT`.
- **Classification:** executor-backed **and graph-required** — add to `_APPLY_TYPES`,
  `_GRAPH_REQUIRED_TYPES`, and `EXECUTOR_BY_TYPE`; a new key-free executor (`apply_marked_duplicates`)
  runs inside `run_apply`'s graph block, so a graph-down apply with an approved item refuses identically
  (live 503 / dry-run `blocked`, ADR-0040).
- **Per-item preview:** add a dedicated A1 `ReviewPreview` projector, kept **read-only and lightweight — a
  per-item detail hint, NOT a mutation predictor** (the full mutation diff is the ADR-0040 dry-run's job).
  It carries: `proposed_action` ("mark duplicates(a,b)"), `node_ids` `[a, b]`, `affected_paths`
  `[pageA, pageB]` *when the graph nodes resolve to pages*, `apply.supported = true`, and read-only
  `warnings` for `malformed_subject`, `self_duplicate`, `node_missing`, `type_mismatch`, and
  **`already_duplicated`** (an active `duplicates(a,b)` edge already exists). It does not enumerate the
  edge/page writes.
- **Previewable:** the ADR-0040 dry-run shows `diff.graph.edges_added` for `duplicates(a,b)` active, **two
  wiki unified diffs** (one per page), and the review-file move under `diff.reviews`.

**F. No detector in this slice.** Nothing currently *proposes* `mark_semantic_duplicate`; items are
human/externally filed (the decide-complete vocabulary already includes the type). An automatic
duplicate-detection producer is **deferred** — this slice is the executor + projection + preview + dry-run
coverage only.

## Consequences

The project gains its first reviewed governance executor on the safest possible footing: it realizes
approved semantic knowledge as a durable, idempotent, symmetric graph assertion with a human-visible page
projection, while touching **no** stable id, citation, backlink target, node status, or retrieval
eligibility — so it cannot trigger the identity-surgery blast radius. It also validates the ADR-0040
dry-run on a graph-writing + page-rendering executor (edges_added + wiki diffs together). The cost is a
bounded renderer addition (`## Duplicates` across the semantic page types), the executor + its scope
guards, the A1 projector, and the classification wiring. Deferred: `hide_content` (next non-rekeying
slice; retrieval/visibility semantics), `change_entity_subtype` (only if id-preserving), a
duplicate-detection producer, and all rekeying executors (merge/split) to a dedicated identity-surgery ADR.

## Tests (design intent; written at implementation)

- Approve → active `duplicates(min,max)` edge (canonical, `asserted_by=human`), both pages gain a
  `## Duplicates` section; idempotent re-apply writes no duplicate row.
- Reject → no graph/wiki mutation (ledger move only); `decision_apply_required` approve=True/reject=False.
- Each scope guard skips with its reason (`malformed_subject`, `invalid_node_id`, `node_missing`,
  `type_mismatch`, `self_duplicate`) and never partial-applies.
- Graph-down apply with an approved item refuses (live 503 / dry-run `blocked`).
- Dry-run preview shows `edges_added` + two wiki diffs + the review move; live stays byte-identical until
  apply.
- The A1 single-item preview reports `apply.supported = true` with `node_ids` + both `affected_paths`, and
  surfaces `already_duplicated` when an active `duplicates(a,b)` edge already exists (read-only; no diff).
- `validate_graph` passes on the produced canonical SAME_TYPE `duplicates` edge, and **fails on a
  reversed/non-canonical `duplicates` row** (`src_id >= dst_id`).
- `validate_projection` **fails when an active `duplicates` edge is missing from either page**, and **fails
  when a `## Duplicates` link has no active `duplicates` edge** (both directions).
