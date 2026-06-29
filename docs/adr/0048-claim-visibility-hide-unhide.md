# ADR-0048 — Claim visibility: hide_claim / unhide_claim without graph surgery

**Status:** Accepted. Design-locked **and implemented** 2026-06-28. `recompose_claim` gains the `hidden`
governance status (precedence + preservation) + the partner-status filter on the contradiction projection;
`render_claim_page` renders `hidden`; `deprecations._apply_claim_visibility_transition` (+
`apply_hidden_claims`/`apply_unhidden_claims`, the partner re-render fan-out); `review_read`
`_effect_hide_claim`/`_effect_unhide_claim` + previews (keyed on hidden-ness); `hide_claim`/`unhide_claim`
vocab + `run_apply` wiring + the claim-specific reindex warnings. Covered by `tests/test_claim_visibility.py`.
Extends the ADR-0046/0047 hide/unhide pattern to **claim pages** (the distinct `recompose_claim` seam):
`hide_claim` (active → hidden) and `unhide_claim` (hidden → evidence-derived) land **together**.
Visibility-only — **no edge deletion**; contradiction detection is **status-filtered, not edge-destructive**
(a hidden claim falls out of `active_node_ids_of_type("claim")` candidate generation; existing edges stay).
The claim-specific
work is the evidence-derived status mechanism + the rendered contradiction/supersede backlink sections on
**partner** claim pages.
**Extends:** ADR-0046 (semantic hide, the inspection-vs-discovery split, `partial_hide_state`), ADR-0047
(unhide, `from_status`, `partial_unhide_state`), ADR-0031 (claim contradiction projection +
`contradiction_affected` re-render), ADR-0022/0030 (claim **page** is the node-status authority; status
preserved across re-render), ADR-0034 (`/query` cites source chunks, not pages), ADR-0045 (reopen safety).
Read `app/workers/claims.py` (`recompose_claim` — evidence-derived status + the `deprecated_candidate`
preservation + `contradiction_affected`), `app/backend/graph.py` (`active_contradictions_for_claim`),
`app/backend/eligibility.py` (`ANSWER_ELIGIBLE_TYPES` includes `claim`).

## Context — why claims aren't concepts

ADR-0046 hid concept/entity pages via `recompose_semantic_node_page`, which takes an **explicit** `status`.
Claims are different: `recompose_claim` **derives** status from evidence (active if it has active
`derived_from` cites; **tombstone → `deprecated_candidate`** if none) and **preserves**
`deprecated_candidate` by reading the page (ADR-0022: the page is the authority; re-extraction must not
resurrect a deprecated loser). And a claim Y's page renders a **"Contradicting Claims" / supersedes backlink
section** from `active_contradictions_for_claim`, which returns the partner of every **active** `contradicts`
edge **regardless of the partner's node status** — so a hidden partner would *still* be listed today. These
two facts make claim hide more than a clone.

## Decisions

**1. Scope: `hide_claim` + `unhide_claim` together, claim pages only.** Separate review types (distinct
seam + evidence semantics from `hide_semantic_page`), with their own `apply_hidden_claims` /
`apply_unhidden_claims` executors built on `recompose_claim`. **Synthesis waits** (its own executor +
promotion semantics).

**2. Authority + status mechanism (the claim-specific core).** Claim **page** status is authoritative, graph
node mirrored, via `recompose_claim`; **GRAPH-REQUIRED** (like semantic pages). `recompose_claim` is
extended with a **`hidden` governance status** that has **precedence over evidence-derivation** and is
**preserved across re-render** — exactly the mechanism that already preserves `deprecated_candidate` (read
the page; a hidden claim stays hidden through any later evidence-driven recompose — contradiction
re-projection, re-extraction — so it never silently un-hides).
- **Hide** renders `status: hidden` + `review_status: approved` (active-only — a non-active claim is a typed
  `claim_not_active` skip).
- **Unhide** *clears* the hidden override and **re-derives through normal claim logic**: `active` +
  `review_status: none` if the claim still has active evidence, or `deprecated_candidate` (tombstone) if it
  lost all evidence while hidden. **Not** a blind force-to-active — that would create an active claim with
  zero citations, violating the "an active claim has evidence" invariant (and citation validators).
  `review_status` returns to the derived default, not blindly `none`.

**3. Effect.** A hidden claim drops from default `/search` **navigation** + the `/search` **graph channel**
(as adjacent), and loses **answer-eligibility** (`answer_eligible` needs `status == active`; claims *are*
answer-eligible). It is **not** in the `/search` **evidence** channel at all — that channel is **source
chunks** keyed by `source_id` (ADR-0034), not claim pages — and `/query` likewise cites source-chunk
evidence. So hiding a claim changes default `/search` **navigation + graph-channel discovery + the
`answer_eligible` flag**, *not* chunk evidence or `/query` citations. **Preserved:** the graph node + all
edges stay (graph is SoT); raw `/graph/*` still returns the claim and its `contradicts`/`derived_from` edges
with `status: hidden`.

**4. Crux — the rendered "Contradicting Claims" backlink section omits hidden partners + re-renders the
partners.** (Scope note: a claim page renders a **`contradicts`** partner section, via
`active_contradictions_for_claim`; it does **not** render a supersedes *partner* section — "superseded" is
only a status *label* on the loser's own deprecated page — so this decision is about the **contradiction**
backlink section. Supersede *rendering* is therefore out of scope here.) A hidden claim X is **omitted by
default** from a partner claim Y's rendered "Contradicting Claims" section (a **discovery surface**, not the
durable record). Concretely:
- the rendered projection **filters partner claims by default-visible status** (a hidden partner is dropped
  from the rendered section);
- **hiding X re-renders every affected partner claim Y** (those with an active `contradicts` edge to X) so
  their sections drop X; **unhiding X re-renders them** so X reappears — reusing the existing
  `contradiction_affected` re-render fan-out;
- the **edge stays active** (graph is SoT — no surgery), so raw `/graph/*` inspection still shows the X↔Y
  conflict with `status: hidden`. **Raw graph APIs are unchanged.**

The **Source-page "Claims" section** is the same kind of rendered discovery surface: a hidden claim X is
**omitted by default** from the Claims section of every Source page that cites it (the `derived_from` edge
stays active for raw inspection), and a claim hide/unhide **re-renders X's cited Source pages** (its active
`derived_from` destinations) so they drop/restore X. On unhide, X reappears only if it re-derives `active`
(still evidenced); a re-derived **tombstone** has no active `derived_from`, so it isn't on any Source page.

> **Extended by ADR-0049:** the same fan-out reaches **Synthesis** pages — a hidden claim is omitted from
> the Supporting Evidence of every synthesis that cites it, and a claim hide/unhide re-renders those
> syntheses (`affected_syntheses` → `rerender_synthesis_page`). The claim executor was deferred from
> syntheses in v1; ADR-0049 closes that gap so the claim-hide discovery suppression is complete across all
> rendered surfaces (Source pages, contradiction partners, **and** syntheses).

**5. No edge surgery; detection is status-filtered, not edge-destructive.** Hiding does **not** mutate or
delete any existing `contradicts`/`derived_from` edge — they remain as durable graph history and raw
`/graph/*` still shows them. But because a hidden claim is **no longer active**, it is **excluded from future
contradiction candidate generation** by the **existing `active_node_ids_of_type("claim")` gate** in
`candidate_pairs` — so no new tier-3 model calls are spent on deliberately-hidden material, and the detector
needs **no change** (this falls out of the current active-only filter). Unhiding restores the claim to
`active`, so it re-enters future detection. (A separate "hidden-but-evidenced detection" path is explicitly
**not** added — it would contradict hide-as-suppression and spend model calls on hidden material.)

**6. Reopen/projector — partial-state safety keyed on hidden-ness.** Mirrors ADR-0046/0047, with the claim
refinement that the *un-hidden* state may be active **or** tombstone, so the projector keys on
**`status == hidden`**, not "active":
- **hide:** `EFFECTED` = page **and** graph `hidden` (+ `review_status: approved`); `PENDING_APPLY` = neither
  hidden; **partial** (page XOR graph hidden) = `UNKNOWN partial_hide_state`; `claim_not_active` warning on a
  non-active target.
- **unhide:** `EFFECTED` = neither page nor graph `hidden` (the override is cleared — the re-derived status,
  active *or* tombstone, is irrelevant to "is it still hidden?"); `PENDING_APPLY` = both `hidden`;
  **partial** = `UNKNOWN partial_unhide_state`. (No `claim_not_hidden` warning is needed — a non-hidden
  target *is* the unhide goal, so it projects `EFFECTED`; the executor treats it as an idempotent no-op.)

Partial live states are **not reopenable** (ADR-0045): part of the visibility change is live, so reopening
would orphan it.

**7. Reindex-failure is non-clean, with claim-specific warnings.** An applied claim hide/unhide whose
keyword/nav reindex failed → apply `validation_failed` (live + dry-run) +
**`claim_hide_retrieval_suppression_not_guaranteed`** / **`claim_unhide_discovery_restoration_not_guaranteed`**
(distinct from the semantic warnings — claims are an answer-eligible evidence surface, so operators want the
specific signal). A graph-only completion still triggers reindex (the ADR-0046 hardening); `wiki/index.md`
keeps the claim listed, annotated `hidden` (or its re-derived status on unhide).

## Consequences

The claim lifecycle gains reversible, audited, reopen-safe visibility governance reusing the review-ledger +
hide/unhide + projector/dry-run/reopen machinery, with three claim-specific additions: the `hidden`
governance status threaded through `recompose_claim` (precedence + preservation, mirroring deprecated); the
partner-status filter + `contradiction_affected`-style re-render fan-out for backlink sections; and the
unhide re-derive. Deferred: **synthesis** visibility, any contradiction/supersede **detection** change, and
identity surgery.

## Tests (design intent; written at implementation)

- Hide an active claim → page+graph `hidden` + `review_status: approved`; it drops from default `/search`
  **navigation** + the **graph channel** + **answer-eligibility** (`answer_eligible` false). **No test
  expects it to leave source-chunk evidence** — claims are not chunk evidence. Raw `/graph/*` still returns
  it + edges (`status: hidden`).
- **Status preservation:** a later `recompose_claim` (e.g. contradiction re-projection / re-extraction) of a
  hidden claim **stays hidden** (never silently re-derived active).
- **Detection:** after hiding one endpoint, `candidate_pairs` no longer generates a NEW candidate involving
  the hidden claim (active-only gate); the **existing** active `contradicts` edge remains visible via raw
  `/graph/*`. Unhiding re-admits it to candidate generation **only if it re-derives `active`** — a
  re-derived tombstone (evidence lost while hidden) stays excluded.
- **Backlink omission + re-render:** hiding X re-renders partner Y so Y's **"Contradicting Claims"** section
  drops X; the edge stays active (raw graph shows it); unhiding X re-renders Y so X reappears.
- **Unhide re-derive:** unhide of a still-evidenced claim → `active` + `review_status: none`; unhide of a
  claim that lost evidence while hidden → tombstone `deprecated_candidate` (never active-with-no-citations).
- Projector/reopen: hide partial / unhide partial → `UNKNOWN` `partial_*_state` → reopen **409**; fully
  effected → reopen blocked; from-state → reopen allowed; `claim_not_active` skip on a non-active hide
  target (a non-hidden unhide target is `EFFECTED`/idempotent — no `claim_not_hidden` warning).
- Graph-required: graph absent / node missing → block + 503; dry-run blocked. Reindex failure → non-clean +
  the claim-specific warning (live + dry-run); graph-only completion still reindexes.
- Detection status-filtered (not edge-destructive): after hiding one endpoint, `candidate_pairs`
  (`active_node_ids_of_type("claim")`) no longer generates a NEW candidate involving the hidden claim,
  while the **existing** active `contradicts` edge remains in the graph (raw `/graph/*`); unhide re-admits
  it only if it re-derives active.
- Validator: a hidden claim that lost all evidence (recomposed while still hidden) renders
  `status: hidden` + `citations: []` and **passes** `validate_citations` (hidden is no-citation-legal).
- Partial states are typed skips, never silent: hide with page-active/graph-hidden (or vice versa) →
  `partial_hide_state` skip; unhide with page-hidden/graph-active → `partial_unhide_state` skip. Both
  hidden + `review_status: pending` → projector `UNKNOWN` (not `EFFECTED`); the executor normalizes it.
