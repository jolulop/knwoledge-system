# ADR-0062 — item_type retrieval faceting

Status: **implemented** (grill + design-lock `3cfb398`; implementation 2026-07-22)

Fulfills the **retrieval-side `item_type` faceting** deferral named in
[ADR-0059](0059-knowledge-item-taxonomy-and-type-neutral-identity.md) ("the taxonomy's payoff
slice"). Extends the retrieval architecture of [ADR-0032](0032-phase-4-retrieval-architecture.md) (hybrid keyword+vector + RRF)
and touches [ADR-0034](0034-phase-5-query-and-cited-answering.md) (query & cited answering);
eval-gated under [ADR-0038](0038-retrieval-relevance-eval.md). Supersedes nothing.

## Context

ADR-0059 replaced the old ontology with a 15-type **knowledge-item taxonomy**: `item_type` is
mutable, governed metadata on graph **item** nodes (`node_type: item`). UAT rounds 1–3 exercised
the taxonomy end-to-end; retrieval is where it should now pay off — letting a caller say "the
`method_technique` items about X" or "answer using `model` evidence."

The obstacle is that `item_type` lives on **item nodes**, but the citable retrieval layer is
**source-chunk-keyed** (ADR-0032): the evidence/keyword/vector channels return chunks with
citation anchors, and `item_type` is not a chunk property. The only bridge from a chunk to an
`item_type` is coarse and source-level — chunk → its source → `mentions` edges → item nodes →
their `item_type` — and a source mentions items of many types. Faceting must therefore be
**precise where `item_type` is native** (item pages / graph item nodes) and **advisory where it
is only bridged** (evidence chunks), or it would corrupt the citable-evidence contract.

## Decisions

### 1. Precise on native channels, advisory on evidence

> `item_type` faceting is **precise** for item / navigation / graph channels and **advisory**
> for evidence chunks. Evidence chunks may receive a bounded boost when their source is linked
> to active items of the requested type, but they are **not excluded** solely for lacking that
> bridged type.

- **navigation** (item-page discovery) and **graph** (node hits): native `item_type` → hard
  filter.
- **evidence** (chunks; feeds `/query`): bridged, coarse → bounded **boost only**, never a
  filter. This protects the ADR-0032/0034 eligibility invariant (citations come only from
  citable chunks; `item_type` is a topic lens, not a citability property).

Rejected: denormalizing `item_type` onto chunks (couples the chunk index to mutable graph
state → restale pressure; a hard filter on the coarse source join would drop citable evidence);
item-seeded retrieval (facet selects items → their sources scope evidence) — kept as a **future
retrieval mode**, revisited only if evals show the advisory boost is insufficient ("answer
within this item class" workflows).

### 2. Facet is a set-valued type predicate, not a layer filter

- **Multi-value.** The facet accepts a **set** of item types (repeatable `?item_type=` on
  `/search`; a list field on `/query`). Empty/absent = no faceting (today's behavior).
- **Validated vocabulary.** Values are validated against `taxonomy.ITEM_TYPES` (the **15 real
  types**). Unknown values **and** the `unclassified_review_required` sentinel are **rejected
  with 400** — never silently ignored. The sentinel is a QA escape hatch, active+sentinel is
  validator-forbidden, and the bridge is active-only, so faceting on it is meaningless.
- **Type predicate only.** An active facet means "**where a result has an `item_type`, it must
  match one of the requested**." It does **not** mean "return only items": Source / Claim /
  Synthesis / Query results (which carry no `item_type`) pass through untouched. Item-only
  discovery remains the existing `page_type=item` / `node_type=item` levers, stackable with the
  facet.

### 3. Evidence boost: batched active-only bridge, additive, bounded, weaker than relevance

- **Bridge (batched, active-only).** For the fused evidence candidate set: collect distinct
  `source_id`s; one graph lookup maps each `source_id` → the set of `item_type`s of its
  **active** items reachable by **active** `mentions` edges; a chunk is **on-type** when that
  set intersects the requested facet. One lookup per search, never per chunk. A deprecated /
  candidate item or a superseded mention does **not** bridge.
- **Boost form.** Additive, applied **post-fusion, pre-cap**: on-type chunks get `+=
  item_type_boost` on their RRF score, then re-sort, then apply the evidence cap. Policy knob
  `item_type_boost` in `policies/retrieval.yaml`; **`0` disables** the boost.

> The boost must be weaker than primary relevance. It may break ties or move an on-type chunk a
> few positions, but it must **not** turn `item_type` into a hidden evidence filter.

The knob's default is finalized during the structural eval so the anti-hidden-filter case
(below) passes. The boost is surfaced in `notes`/debug metadata.

### 4. Navigation index carries `item_type` (page metadata, not chunk denormalization)

- `item_type` becomes an `UNINDEXED` column in the **navigation** FTS index, populated **only
  for Item pages** (NULL otherwise) — it is item-page frontmatter, flowing in exactly as
  `page_type` / `status` already do. The nav facet is a self-contained `AND item_type IN (…)`,
  applied only where the row is an Item page. **No graph join** (keeps the "page index filters
  page metadata" contract; avoids coupling nav to graph availability).
- The **graph** channel needs no index change — `graph_read` already returns `item_type` on
  nodes; the facet is a predicate on the subgraph's item nodes (claim/synthesis nodes retained).
- **Schema version bump: `keyword_index.INDEX_VERSION` 1 → 2.** An old (v1) or missing nav index
  is handled by the **existing** paths: `reindex` does a full rebuild when
  `user_version != INDEX_VERSION`, and `validate_index_consistency` returns the "stale schema →
  reindex required" error until it is rebuilt. Nav `item_type` inherits the existing per-page
  fingerprint freshness contract, so a retype-without-reindex is caught (the nav fingerprint
  must cover `item_type`).

### 5. Response `notes` — distinct wording per endpoint

`/search` has nav/graph result surfaces; `/query` does not (it is evidence-only synthesis), so
the notes differ. **Implementation note:** the note keys on the **channels that actually ran**,
not the endpoint name — which is stricter than "per endpoint" and can't misdescribe the result.
`/query` never runs nav/graph, so it always gets the evidence-only note; a keyword-only `/search`
(the common `auto` default shape) does too, and a `/search` that actually ran nav/graph gets the
fuller note:

- **nav/graph channels ran:** `item_type facet applied to item page/graph results; non-item
  results retained; evidence received advisory boost only`
- **evidence-only (all `/query`, keyword-only `/search`):** `item_type facet used as advisory
  evidence boost only; off-type evidence retained`

## Eval (ADR-0038) — structural lane only

Faceting is deterministic, so it is proven in the **structural** lane
(`evals/golden_retrieval.yaml` + `tests/test_retrieval_evals.py`, fixture vault + FakeEmbedder,
CI-runnable) — no real embedder. Extend the golden schema with an `item_type` facet field and
add fixture item nodes of varied types + active `mention` edges to exercise the bridge. **No
real-corpus faceted relevance case** this slice; **no reference-baseline re-record** (case 6
guarantees unfaceted default ranking is unchanged, so the baseline is untouched per ADR-0038's
"refresh only on explicit reset" policy).

Falsifiable acceptance cases:

1. **Nav/graph typed filter** — `item_type=[method_technique]` returns exactly the
   `method_technique` Item pages/nodes; other-type Item results excluded.
2. **Non-item retention** — under the same facet, Source/Claim/Synthesis results still appear.
3. **Evidence tie-break boost** — a chunk whose source bridges to a requested-type active item
   ranks above an equally-relevant off-type chunk.
4. **Anti-hidden-filter** — a highly-relevant off-type chunk stays **above** a weakly-relevant
   on-type chunk (relevance dominates the bounded boost).
5. **Unknown/sentinel rejected** — an unknown value or `unclassified_review_required` → 400.
6. **No-facet byte-identical regression** — with the facet absent, results are identical to
   pre-slice behavior (proves opt-in; justifies leaving the relevance baseline untouched).

## Tests

- **Structural evals** — the six acceptance cases above, in `golden_retrieval.yaml`.
- **Bridge unit** — batched `source_id → active-item item_type set` uses only active items +
  active mention edges; a deprecated/candidate item or a superseded mention does not bridge.
- **Nav index** — `item_type` column populated for Item pages, NULL for others; `reindex_keyword`
  refreshes it after a retype; `validate_index_consistency` flags a stale/mismatched nav
  `item_type`; **an old (v1) / missing nav index is rebuilt cleanly on reindex, or rejected via
  the existing reindex-required path** (`INDEX_VERSION` 1→2).
- **Endpoint validation** — multi-value parsing; unknown + sentinel → 400; empty/absent = no
  faceting; distinct `/search` vs `/query` notes.
- **/query boost** — the facet boosts on-type evidence in the pack without excluding off-type;
  notes surface the advisory behavior.

## Rollout

Nav-index schema bump (`INDEX_VERSION` 1→2) → `reindex_keyword` required (free on the empty UAT
vault); **no producer / LLM re-run**; the ADR-0038 relevance baseline is **not** re-recorded.
Faceting is opt-in per request — absent facet changes nothing.

## Review round 1 (post-implementation, 2026-07-22)

An external review found three blocking gaps + two non-blocking, all fixed:

- **B1 — saved-query identity ignored the facet.** `query_id` is the answer-affecting request
  scope, and the facet's boost is pre-cap (it changes pack membership → the answer), so two
  faceted answers could overwrite the same `wiki/Queries/<id>.md`. Fix: `item_type` (order-
  insensitive set) is now in `query_id` and recorded as `item_type_facet` in the saved page
  frontmatter.
- **B2 — the boost was "bounded" only by convention.** A config `item_type_boost: 1` would swamp
  RRF scores and become a hidden filter. Fix: `load_retrieval_policy` clamps to an architectural
  cap `min(0.005, 1/(rrf_k+1) − 1/(rrf_k+prefusion))` — the gap between a rank-1 and a tail
  single-channel hit, so a tail on-type hit can never rise above a rank-1 off-type hit; config can
  only lower it (or set 0), never raise it. (At the default k=60/prefusion=50 the cap is exactly
  the shipped 0.005.)
- **B3 — `/search` didn't gate on index schema.** `search_navigation` unconditionally selects
  `item_type`, so a pre-bump (v1) index crashed *any* navigation query with an `OperationalError`.
  Fix: `keyword_index.schema_usable()` (cheap `PRAGMA user_version` + required-table/column probe)
  is checked in `_run_search`; a stale/mismatched index degrades keyword+navigation to
  **unavailable** with a reindex-required note, never a 500. Full fingerprint freshness stays
  offline in `validate_index_consistency`.
- **NB1 — notes could overclaim the boost.** When `item_type_boost=0` or the graph is unavailable,
  the note now says the boost was disabled/unavailable instead of "received an advisory boost."
- **NB2 — a malformed Item page missing `item_type`** was stored as `''` and passed a facet as if
  non-item. Fix: an Item page with no `item_type` is marked with the QA sentinel in the nav index,
  so a production-type facet excludes it (validators still reject the missing frontmatter).

Tests added per the review: saved-query facet scope + frontmatter; loaded-policy clamp + the
anti-hidden-filter through `load_retrieval_policy`; the `schema_usable` gate; disabled/unavailable
boost notes; malformed-item facet exclusion; the valid `/query` facet path.
