# Phase 3.5c Plan

## Cross-Source Synthesis and Contradiction Detection

Phase 3.5c is the last and highest-risk enrichment slice (ADR-0028): tier-3 reasoning that
spans multiple sources/claims and proposes **contradictions** and **syntheses** as
human-reviewed items. It is built entirely on the proven 3.5b graph — it adds **no new graph
authority**, only two new producers over it. It is delivered as two ordered,
independently-shippable sub-slices, each tested and committed before the next, with risk and
design surface rising across them.

This plan decomposes the slices and records the decisions taken in the 3.5c grilling pass
(ADR-0031). It is a planning document only — no production code follows from it until each
slice is implemented in turn.

---

## 1. Objective

On top of the 3.5b graph (grounded claims, candidate concepts/entities, promotion,
backlinks), surface **where independent sources disagree** (contradiction edges) and
**what they collectively say** about a recurring concept (synthesis pages) — without
weakening any Phase 3/3.5a/3.5b invariant (deterministic backbone, untrusted-input boundary,
citation grounding, graph-is-source-of-truth, human-reviewed semantic/destructive change).

---

## 2. Scope

In scope:
- Contradiction detection: graph-blocked candidate pairs → tier-3 verdict → `proposed`
  `contradicts` edges → `resolve_contradiction` reviews with a three-outcome vocabulary.
- The thin `supersede` resolution executor (`supersedes` edge + `deprecated_candidate`).
- Cross-source synthesis: per active concept/entity (≥2 independent sources) → grounded
  synthesis pages, born `candidate`, review-gated for promotion via a new `propose_synthesis`
  review type.

Out of scope (later phases): retrieval/ranking/cited answering (Phase 4/5); autonomous
scheduling (Phase 7); vector-similarity blocking (recorded escape hatch, not v1); concrete
local-model deployment (only the adapter seam is fixed, ADR-0025).

---

## 3. Sub-slices and sequencing

| # | Slice | Depends on | Status |
|---|-------|-----------|--------|
| 1 | **Contradiction detection** (graph-neighborhood blocking; sorted-pair `contradicts` assertions; `resolve_contradiction` three-outcome reviews; `acknowledge`/`reject` activation; Claim-page projection) | 3.5b graph, ADR-0018/0030/0031 | **DONE** (`app/workers/contradictions.py`, `scripts/detect_contradictions.py`; deterministic `candidate_pairs` blocking; sorted-pair `proposed` `contradicts` edges; per-pair cache-replayed verdicts with **full anchors + shared node ids embedded in the prompt** for a faithful fingerprint; confidence clamped to [0,1]; `apply_resolved_contradictions` acknowledge/reject **+ Claim-page backlink re-projection** via `render_claim_page`/`validate_projection`; **endpoint-gone supersession** enforced in the **claim lifecycle** (shared public `recompose_claim` + `graph.supersede_contradictions_for_claim` — `extract_claims` stays valid on its own; detect keeps it as a backstop) vs provenance-survival; two-sided-evidence guard before any verdict; `rebuild_index` contract (rebuild iff pages re-projected); stale supersession + `reviews.withdraw_review_item` with per-event audit; independence rule moved to `manifests.independent_sources`; `validate_graph` canonical-ordering check) |
| 1b | **`supersede` resolution executor** (thin reviews-worker action: `supersedes` edge + `deprecated_candidate`) | 1 | not started (approved `supersede` decisions surface as `supersede_pending_1b`, never silently applied) |
| 2 | **Cross-source synthesis** (per active concept/entity; grounded synthesis pages; `propose_synthesis` review type; review-only promotion) | 1, ADR-0031 | not started |

Rationale for ordering: 3.5c-1 reuses existing schema and review vocab and is the contained
surface on which the tier-3 pairing/cost question is first exercised; 3.5c-2 reuses 3.5c-1's
blocking/independence/caching machinery and is the larger producer/page surface, so it lands
last on a proven base.

---

## 4. Contradiction detection (slice 3.5c-1)

- **Tier-3** (`ENRICH_MODEL_HEAVY`, default `anthropic:claude-opus-4-8`) via the 3.5a
  `LLMClient`. Untrusted-data framing as in 3.5a/b (ADR-0026) — each of the two claims'
  underlying source spans is untrusted input, treated as data, never instructions.
- **Blocking (decided, ADR-0031 §1).** Candidate pairs are generated deterministically from
  the graph: two claims are a candidate pair iff they share ≥1 `active` concept/entity node
  (co-mention via `active` `mentions` edges) **and** come from two *independent* sources
  (ADR-0018 independence test). Zero model calls to compute; bounds the pair set; a source
  cannot contradict itself. Vector blocking is a deferred second channel, not v1.
- **Representation (decided, ADR-0031 §2).** One `contradicts` assertion per pair, with
  `src_id`/`dst_id` = the two claim ids **sorted lexically**, so A-vs-B and B-vs-A collapse to
  one row and one review. The row's single evidence anchor is the `src` claim's primary
  `active` citation, set **advisory only** — the authoritative, two-sided evidence stays the
  two Claim pages' structured citations (a single edge row cannot represent both sides).
  `asserted_by: llm`, `status: proposed`, `confidence` = verdict confidence, carrying
  `review_id`. **Never `active` on creation** — a semantic judgment is invisible until reviewed
  (ADR-0030). No schema change.
- **Review proposal (carries both sides).** Each surviving pair files a `resolve_contradiction`
  item (existing review type) keyed on the sorted pair, so `review_id` is stable/idempotent.
  Because the edge row is one-sided, the item carries **both claim ids plus rendered context
  for both claims** (each side's text + active citations), so the human can judge the
  disagreement from the item alone.
- **Re-run/supersede (decided, ADR-0031 §2; implemented).** Two triggers, by *what* changed:
  **endpoint gone** (a claim no longer `active` — tombstoned or text-changed → new id)
  supersedes the assertion **whether proposed or active** (even a human-acknowledged edge, and
  the surviving claim's page drops the backlink); **pair left the candidate set** with both
  endpoints standing (e.g. a provenance edit) supersedes **proposed only** — independence is
  the blocking criterion, not a validity condition, so it never silently undoes an
  acknowledgement. A superseded pair's pending review is **withdrawn** (re-fileable), not
  rejected. **The endpoint-gone retraction is enforced in the claim lifecycle** (the shared
  `recompose_claim` calls `graph.supersede_contradictions_for_claim` on tombstone, withdraws the
  pending reviews, and re-renders surviving endpoints — no `contradictions` import into
  `claims`), so `extract_claims` stays valid on its own; the contradiction worker keeps the same
  check only as a backstop, and "run detection after extraction" is **not** a validity contract.
- **Resolution outcomes (decided, ADR-0031 §3).** A field on the review item, chosen by the
  human, maps to a deterministic effect: **`acknowledge`** → edge `proposed → active` (both
  claims live; **projected as a `[[Claims/…]]` backlink under each Claim page's "Contradicting
  Claims" section**, re-rendered through the single claim renderer and checked by
  `validate_projection`); **`supersede`** → winner-named
  `active` `supersedes` edge + loser deprecated to `deprecated_candidate` **via the
  `deprecate_wiki_page` audit path** (the `resolve_contradiction` approval authorizes the
  deprecation — no second human gate — but the `audit_log` entry states the status change was
  part of an approved contradiction resolution; never a silent status mutation); the
  `contradicts` edge still activates, recording the historical conflict; **`reject`** → edge
  `rejected`, nothing else. Slice 3.5c-1 builds the proposal (with the full fixed vocabulary)
  + `acknowledge`/`reject` activation; `supersede` *execution* is slice 1b — and a `supersede`
  decision is **never silently recorded without its graph/page effects**: 3.5c-1 either applies
  the small action or explicitly returns "not implemented" until 1b.
- **Idempotency (decided, ADR-0031 §4; implemented).** The canonical per-pair payload is
  realized **in the prompt**, since the response cache keys on the messages: both claim texts,
  the **full** citation anchors (`source_id` + char range + quote) of each claim, and the
  shared blocking node ids are all embedded, with schema/prompt/model already in the cache key.
  So identical text + quote but a changed `source_id`, char range, or shared node misses the
  cache and re-evaluates (`test_cache_key_busts_on_anchor_or_topic_change`). A corpus-level
  fingerprint is rejected (one local change → full-pass cache miss). **No API key → `skipped`
  job**, but stale-pair supersession and human-decision application still run keyless.
- **Visibility default.** Contradictory claims remain visible (Build Spec) — the slice never
  hides or deprecates a claim on its own; only an approved `supersede` review does.

---

## 5. `supersede` resolution executor (slice 1b)

A thin action in the reviews worker, run when a `resolve_contradiction` item is approved with
outcome `supersede` and a named winner: write an `active` `supersedes` edge (winner → loser),
deprecate the losing claim page to `deprecated_candidate` through the **`deprecate_wiki_page`
audit path** — the `resolve_contradiction` approval authorizes it (no second human gate), and
the `audit_log` records that the status change was part of an approved contradiction resolution
(never a silent mutation) — re-index `nodes.status`, and leave the `contradicts` edge `active`.
The page is the status authority (ADR-0022). Reuses existing edge/status/deprecation
primitives — no new schema. The loser becomes a tombstone-style deprecated claim, never
hard-deleted (CLAUDE.md rule 9).

---

## 6. Cross-source synthesis (slice 3.5c-2)

- **Tier-3**, untrusted-data framing (ADR-0026).
- **Trigger (decided, ADR-0031 §5).** One synthesis node/page per **`active`**
  concept/entity, eligible iff: the target node `status` is `active`; **≥2 `active` claims**
  are connected to it through the graph neighborhood (`active` `mentions` walk); and those
  claims are supported by **≥2 independent sources** (ADR-0018). The page aggregates the
  contributing claims + their source citations, surfacing any **`active` `contradicts` edges
  among those claims** under the template's "Disagreements" section. Candidate concepts/entities
  are never synthesized; single-source concepts get no synthesis.
- **Grounding (decided, ADR-0031 §6).** Raw sources are truth → claims are grounded atomic
  evidence → synthesis is prose over grounded claims. **synthesis → claim `derived_from`** edges
  are `active` when the referenced claim is `active` and citation-grounded; **optional
  synthesis → source `derived_from`** edges are `active` only for a *direct source quote* that
  passes the `citations.py` gate (an unlocatable quote is **dropped, or generation is marked
  `partial`** — never written). Frontmatter carries **structured, machine-checkable** refs:
  contributing claim ids + any direct-source citation anchors. A sentence summarizing grounded
  claims needs **no** raw span of its own — the chain through Claim pages suffices. `active`
  `derived_from` edges are **provenance, not approval**: the node stays `candidate`.
- **Visibility + review gate (decided, ADR-0031 §7).** The candidate synthesis page **is
  written to disk** under `wiki/Synthesis/` as `status: candidate` (frontmatter:
  `type: synthesis`, `synthesis_id`, `status: candidate`, `review_status: pending`,
  `generation_status: enriched`, `confidence`, `input_fingerprint`). A reviewer must read the
  prose; the page is **excluded from `index.md` / promoted navigation and unusable as evidence
  for any later synthesis or query answer** until promoted to `active`. Promotion is
  **review-only with no recurrence path** — a new **`propose_synthesis`** review type (added to
  `policies/review.yaml` and `reviews.py` `REVIEW_TYPES`), *not* `promote_candidate_node` (whose
  ≥2-source recurrence auto-promote would make every synthesis's review a no-op, since a
  synthesis is born from ≥2 sources by construction; a node-type exception in the promotion
  worker would be fragile). **Approval** → page `status` `candidate → active`, mirror
  `nodes.status`, `audit_log` entry. **Rejection** → `review_status: rejected` + page `status`
  `deprecated_candidate` (the lifecycle vocabulary, ADR-0022, has **no** `rejected` node status;
  a new one would need an explicit ADR-0022 extension).
- **Idempotency.** Per-**target-node** fingerprint over `{target id + status; each contributing
  claim's id/text/active citation anchors; the active contradiction edge ids/statuses among
  them; model/prompt/schema versions}`; cache-replayed; re-run supersedes a synthesis whose
  evidence or surfaced disagreements changed, mirroring the claim/contradiction passes. **No API
  key → `skipped` job.**
- **Renderer discipline.** Same pure-renderer + worker-reads-graph pattern as 3.5b: the
  synthesis page is composed from the graph + grounded data; only graph-backed links render;
  no wall-clock, byte-stable re-render.

---

## 7. Decisions and remaining open items

**Decided (3.5c grilling, ADR-0031):**
- **Two ordered slices** — contradiction (3.5c-1, +1b executor) then synthesis (3.5c-2).
- **Graph-neighborhood blocking** — shared `active` concept/entity + independent sources;
  vector blocking deferred.
- **Sorted-pair single `contradicts` assertion**, src-claim anchor (**advisory only**;
  authoritative evidence = the two Claim pages), `proposed`/`llm`, no schema change,
  supersede-on-rerun; the **review item carries both sides' context**.
- **Three-outcome resolution** — `acknowledge`/`supersede`/`reject`; `supersede` deprecates the
  loser via the `deprecate_wiki_page` audit path (cause recorded), executes as a thin follow-on
  (slice 1b), and is **never silently recorded without effects**.
- **Per-sorted-pair idempotency** keyed on **both claims' text + active citation anchors + the
  shared blocking node ids** + prompt/model/schema; cache-replayed; corpus-level rejected.
- **Per-active-node synthesis trigger** — active target + ≥2 active claims via neighborhood +
  ≥2 independent sources; fingerprint includes contributing claim citation anchors **and the
  active contradiction edges among them**.
- **Synthesis grounds on claim nodes** + verbatim-gates direct quotes (unlocatable → drop or
  mark generation `partial`); `derived_from` edges `active` = provenance, node stays
  `candidate`; frontmatter carries machine-checkable claim/citation refs.
- **Synthesis born `candidate`, written to `wiki/Synthesis/`, promotion review-only** via a new
  `propose_synthesis` review type (no recurrence auto-promote). Rejection →
  `review_status: rejected` + `status: deprecated_candidate` (no `rejected` node status in the
  ADR-0022 lifecycle vocabulary).

**Still open, resolved when each slice starts:**
- The **LLM verdict schema** for a contradiction pair (boolean + confidence, and whether to
  classify a contradiction type/severity) — implementation detail of slice 3.5c-1.
- The **synthesis output schema** and short-summary/section shaping — slice 3.5c-2.
- Whether the **`resolve_contradiction` outcome field** is recorded as a dedicated decision
  key or inferred from the approval payload — slice 3.5c-1 implementation.
- Tuning of **blocking recall** (whether vector blocking is needed) — observed after 3.5c-1.
- **Known deferred (non-blocking, post-review):** (a) candidate generation is
  O(active_claims²) before blocking — fine at local-first scale, but a large corpus may want
  deterministic pre-indexing by mentioned node; (b) a `resolve_contradiction` item has no
  durable explicit-outcome field beyond approved/rejected + optional `winner` — slice 1b adds
  the `supersede` winner→loser executor and can formalize the outcome field then; (c) an
  acknowledged edge's *advisory* anchor is not refreshed if a claim's span moves while its text
  (and id) is unchanged — acceptable since the anchor is advisory and the authoritative
  evidence is the Claim pages.

---

## 8. Validators (extend the lint suite)

- `validate_graph.py` already enforces the `contradicts` endpoint contract
  ({claim,synthesis}↔{claim,synthesis}) and the `proposed`/`active`/`rejected`/`superseded`
  status model — contradiction assertions are covered by the existing graph validator.
- Slice 3.5c-1 additions (implemented): `validate_graph` enforces `contradicts` canonical
  ordering (`src_id < dst_id`, no mirrored duplicate); `validate_projection` enforces the
  bidirectional Claim-page contradiction projection — every `active` `contradicts` edge is a
  `[[Claims/…]]` link under "Contradicting Claims" on **both** endpoints and every such link
  has an active edge (a `proposed` edge never projects).
- Slice 3.5c-2 additions (`validate_projection`/`validate_wiki`): a synthesis page carries
  required frontmatter + stable `syn_` id; a `candidate` synthesis stays out of `index.md` /
  promoted navigation; synthesis `derived_from` edges resolve to existing claim/source nodes;
  page-frontmatter status == graph `nodes.status` for synthesis nodes.
- `reviews.py` `REVIEW_TYPES` stays in sync with `policies/review.yaml` (new
  `propose_synthesis`).

---

## 9. Testing plan (per slice, before commit)

Mirrors the ADR-0028 acceptance discipline; deterministic pieces tested offline, LLM passes
tested with the fake-adapter `LLMClient` (as in 3.5a/b).

- **Slice 3.5c-1 (contradiction) — implemented in `tests/test_contradictions.py` (17 tests):**
  blocking yields only shared-concept, cross-independent-source pairs (same-source and
  same-family excluded; no shared concept → none); A-vs-B and B-vs-A produce **one** sorted
  assertion + **one** review carrying **both sides' text + citations**; a `proposed` edge never
  projects; `acknowledge` flips it `active` and **both Claim pages render the backlink**
  (`validate_projection` green); `reject` → `rejected`; an unchanged pair replays from cache
  (no provider call); **changed anchors/shared-node bust the cache key**
  (`test_cache_key_busts_on_anchor_or_topic_change`); a pair that leaves the candidate set is
  `superseded` and its review withdrawn; an **acknowledged edge survives a provenance change**
  but is **retracted by the claim worker when an endpoint is tombstoned** (surviving page drops
  the backlink, repo valid without a detect pass); a **verdict is never requested without
  two-sided evidence**; out-of-range **confidence is clamped**; re-projection **rebuilds the
  index** iff pages changed; **no API key → `skipped`** but stale supersession + resolution
  application still run. Withdraw audit history is covered in `tests/test_reviews.py`.
- **Slice 1b (`supersede` executor):** approving `supersede` writes an `active` `supersedes`
  edge (winner → loser), deprecates the loser to `deprecated_candidate` via the
  `deprecate_wiki_page` audit path with an `audit_log` entry naming the contradiction
  resolution as the cause, re-indexes, leaves the `contradicts` edge `active`; idempotent;
  loser never hard-deleted.
- **Slice 3.5c-2 (synthesis):** an active concept with ≥2 active claims over ≥2 independent
  sources gets one candidate synthesis page under `wiki/Synthesis/`; a candidate concept or a
  single-source concept gets none; the page is written to disk but absent from
  `index.md`/promoted nav and unusable as evidence for another synthesis until promoted; a
  direct source quote that fails grounding is dropped (or generation marked `partial`);
  `derived_from` edges are `active` and resolve while the node stays `candidate`; **a synthesis
  never auto-promotes** (no recurrence path) — only an approved `propose_synthesis` flips it
  `active` (mirror + audit); **rejection** sets `review_status: rejected` + `status:
  deprecated_candidate` (never a `rejected` node status); re-run with unchanged evidence **and
  unchanged surfaced contradictions** replays from cache and the page does not churn, but a
  changed contributing-claim citation anchor or a new/removed active contradiction edge
  re-derives it.

---

## 10. Completion

3.5c is complete when, over the 3.5b graph: independent sources' conflicting claims surface as
review-gated `contradicts` assertions with a working three-outcome resolution; recurring active
concepts carry grounded, review-gated synthesis pages that cannot silently auto-promote; and
the full validator suite (citations + graph + projection + wiki) passes. This closes Phase 3.5;
retrieval and cited answering (Phase 4/5) follow on the completed semantic layer.
