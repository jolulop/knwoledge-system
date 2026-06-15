# Phase 3.5b Plan

## Semantic Nodes and Grounding — Claims, Concepts, Entities, Promotion, Graph

Phase 3.5b is the largest enrichment slice (ADR-0028). On top of the 3.5a per-source
summary/tags seam it adds the semantic layer: grounded **claims**, candidate **concepts**
and **entities**, the **≥2-source promotion** lifecycle, and the **bidirectional backlink
engine** backed by the SQLite graph as source of truth. It is delivered as ordered,
independently-shippable sub-slices, each tested and committed before the next, with risk
and design surface rising across them.

This plan decomposes the slices, fixes the open decisions that block them, and records the
sequencing. It is a planning document only — no production code follows from it until each
slice is implemented in turn.

---

## 1. Objective

Fill the semantic placeholders the Phase 3 backbone left (`Claims`, `Concepts Mentioned`,
`Entities Mentioned`, `Key Points`) with grounded, reviewed content, and stand up the
graph that promotion and backlinks depend on — without weakening any Phase 3/3.5a
invariant (deterministic backbone, untrusted-input boundary, citation grounding,
human-reviewed semantic/destructive change).

---

## 2. Scope

In scope:
- LLM claim extraction with mechanical citation grounding (drop-on-fail).
- Candidate concept and entity extraction with stable ids, slugs, and aliases.
- The ≥2-independent-source promotion lifecycle (candidate → active), human-reviewed.
- The SQLite graph (source of truth) and the deterministic backlink projector.
- Composition of claims/concepts/entities into Source pages, replacing placeholders.

Out of scope (later phases): cross-source synthesis and contradiction detection (3.5c);
retrieval/answering (Phase 4/5); autonomous scheduling (Phase 7).

---

## 3. Sub-slices and sequencing

| # | Slice | Depends on | Status |
|---|-------|-----------|--------|
| 1 | **Citation grounding gate + structured-citation validator** | ADR-0019/0020/0026 | **DONE** (`app/workers/citations.py`, `scripts/validate_citations.py`) |
| 2 | **SQLite graph store + validator** (`db/graph.sqlite`, per-assertion edges, active-edge projection primitives, `validate_graph`) | ADR-0029/0030 | **store + validator DONE** (`app/backend/graph.py`, `scripts/validate_graph.py`); page-level backlink rendering wires in with producers (3/4) |
| 3 | **LLM claim-extraction pass** (tier-2; gated by slice 1; Claim pages; compose into Source pages) | 1, 2, ADR-0021/0022 | planned |
| 4 | **Candidate concepts & entities** (pages, ids, slugs, aliases; edges into the graph) | 2, ADR-0017 | planned |
| 5 | **Promotion lifecycle** (≥2 independent sources; review-gated early promotion) | 2, 4, ADR-0018 | planned |

Rationale for ordering: slice 1 (deterministic, done) is the grounding foundation. The
**graph (slice 2) moves up front** because claims, concepts, promotion, and backlinks all
read/write it — building it first avoids reworking each producer. Producers (3, 4) then
write nodes/edges; promotion (5) is a graph computation over them.

---

## 4. The SQLite graph (slice 2)

**Authority split (decided).** The graph is authoritative for **relationships (edges)**;
the **wiki page frontmatter is authoritative for node metadata** — `id` (ADR-0021),
`title`/`slug`, lifecycle `status` (ADR-0022), and `aliases` (ADR-0017). The graph's
`nodes` table is a **derived index** rebuilt from frontmatter (id/type plus a mirrored
slug/status for edge queries and promotion), not a second authority. So ADR-0029's
"graph is source of truth" means edges; one fact has one owner, and rename/status/promotion
cannot diverge. Promotion computes over edges, *proposes* a status change that is written
to the page (the authority, review-gated for early promotion), then re-indexed.

Location: `db/graph.sqlite` (separate from `jobs.sqlite`/`llm_cache.sqlite`; covered by
backup, ADR-0014). Schema to be finalized in the slice-2 ADR; shape:

Finalized in **ADR-0030**. Shape:

```text
nodes(node_id PK, node_type, slug, status, indexed_at)   -- DERIVED index
      node_type ∈ Build Spec §6.1; concept/entity/claim/synthesis indexed from page
      frontmatter, source nodes from manifests (ADR-0008)
edges(edge_id PK, src_id, dst_id, edge_type,             -- one row per ASSERTION
      status,            -- proposed | active | rejected | superseded
      asserted_by,       -- deterministic | llm | human | authored_wikilink
      confidence, evidence_source_id, evidence_char_start, evidence_char_end,
      review_id, job_id, created_at, updated_at,
      UNIQUE(src_id, dst_id, edge_type, asserted_by,
             evidence_source_id, evidence_char_start, evidence_char_end))
      edge_type ∈ Build Spec §6.2 MINUS needs_review (review = status)
```

- **One row per assertion (decided).** A relationship is the *set* of its assertion rows
  and exists/projects iff it has an `active` assertion — so distinct evidence spans and
  coexisting LLM/human assertions never overwrite each other. The `UNIQUE` key is the
  assertion identity, so re-runs upsert idempotently without collapsing distinct spans.
- **Governed vocabulary (decided).** `edge_type` ∈ Build Spec §6.2 **minus `needs_review`**
  (review is `status`, not a relationship); `validate_graph` rejects anything else,
  including a literal `needs_review` edge. Earlier sketch types map on: `about→mentions`,
  `evidences→derived_from`, `alias_of→` frontmatter `aliases`.
- **Review-gated candidates in one table (decided).** Proposed/rejected assertions live in
  `edges` distinguished by `status`; no separate table. The **projector renders only
  `status=active`**, so a model- or prose-authored assertion enters as `proposed` (with
  `asserted_by` + `review_id`) and is invisible until approved (ADR-0018). A *deferred*
  review item leaves the assertion `proposed` (never activates or deletes it).
- **Edges are id-keyed**, so rename/merge is an id-level redirect, not graph surgery.
- The **backlink projector** is a deterministic script that renders each page's `active`
  inbound/outbound edges into its link sections — synchronized by construction (CLAUDE.md
  rules 6, 10), a pure function of the graph + page set (no wall-clock).
- **Authored wikilinks** in prose are validated edge candidates absorbed as `proposed`
  edges under review, never trusted as edges (ADR-0029).
- Idempotency: edge upserts keyed on `(src_id, dst_id, edge_type)`.

---

## 5. Claim extraction (slice 3)

- Tier-2 (`ENRICH_MODEL_STANDARD`, default `anthropic:claude-sonnet-4-6`) via the 3.5a
  `LLMClient`. Untrusted-data framing as in 3.5a (ADR-0026).
- The model proposes candidate claims, each with a structured citation. **Every citation
  is run through `ground_citation` (slice 1); a claim with no resolvable, quoted citation
  is dropped and logged** — never written.
- Surviving claims are written as Claim pages (`templates/claim.md`) with a stable
  `claim_id` (ADR-0021), `generation_status: enriched`, `confidence`, `review_status`.
- A `derived_from` edge (claim → source, Build Spec §6.2) is written to the graph; the
  Source page's `Claims` placeholder is recomposed to list the claims (deterministic
  projection, like 3.5a).
- **Only graph-backed links are rendered.** Claim/Source pages emit links only for
  `active` graph edges; the template's placeholder `[[Claims/{{...}}]]` /
  `[[Sources/{{...}}]]` are omitted when empty (as the Phase 3 backbone did, ADR-0016), so
  no invented-but-valid-looking link can slip past the dangling-link check.
- Like 3.5a, claim output lands first as a per-source artifact / cache entry; the page is
  the composed view, so re-runs are idempotent and a deterministic rebuild does not churn.

---

## 6. Concepts & entities (slice 4)

- Candidate concept/entity extraction (tier-2). Pages are slug-keyed by canonical name
  with a stable `concept_id`/`entity_id` in frontmatter and an `aliases` list (ADR-0017).
- Created as `candidate`/`stub` (low confidence), kept out of promoted navigation/synthesis
  until promotion (ADR-0018).
- `mentions` edges (source → concept/entity, Build Spec §6.2) written to the graph; the
  same graph-backed-links-only rule applies to the Source page's `Concepts/Entities
  Mentioned` sections.

---

## 7. Promotion lifecycle (slice 5)

- A candidate concept promotes to `active` once **≥2 independent sources** evidence it
  (ADR-0018), computed from the graph's `active` `mentions` edges.
- **Independence**: exact (SHA256) duplicates share one `source_id` and count once;
  same-author/publication/report-family sources are flagged for review rather than
  auto-promoting; promotion also weighs confidence, not raw count.
- **Provenance-metadata prerequisite (decided).** Independence detection needs source
  provenance the manifest does not carry today. Slice 5 first models optional manifest
  fields — `author`, `publisher`, `report_family`, `canonical_url` (all null when unknown)
  — and **until they are populated, promotion only *proposes* (human-reviewed), it never
  auto-activates**, so same-family sources can never silently auto-promote. Auto-promotion
  on recurrence turns on only where independence can actually be established.
- Early promotion and all entity merge/split, contradiction resolution, and deprecation
  are **human-reviewed** (ADR-0018, `policies/review.yaml`) — the LLM proposes, never
  executes.

---

## 8. Decisions and remaining open items

**Decided (this review):**
- **Node authority** — graph owns edges; wiki frontmatter owns node metadata (id/title/
  slug/status/aliases); graph `nodes` is a derived index (§4).
- **Edge vocabulary** — Build Spec §6.2 only, enforced by `validate_graph` (§4).
- **Candidate edges** — one `edges` table with `status`+provenance; projector renders only
  `status=active` (§4).
- **Promotion independence** — model optional provenance manifest fields; review-gate
  promotion until populated (§7).
- **Graph-backed links only** — renderers omit placeholder/non-`active`-edge links (§5/§6).

**Still open, resolved when each slice starts:**
- **Graph schema** (slice 2): formalized in **ADR-0030**; only the final column *types*
  remain to tune during implementation.
- **`claim_id` generation + dedup** (slice 3): hash inputs for the creation-time id
  (ADR-0021); how re-runs dedup claims (same text + same citation → same claim?).
- **Source-page composition** (slices 3/4): claims/concepts rendered from artifacts +
  graph, preserving the 3.5a single-writer / fingerprint-idempotent property.
- **Independence heuristic** (slice 5): how `report_family`/`publisher`/`canonical_url`
  are derived and the confidence weighting.

---

## 9. Validators (extend the lint suite)

- `validate_citations.py` — **done** (slice 1): structured grounding of claim citations.
- New `validate_graph.py` (slice 2): every edge references existing node ids; `edge_type`
  is within Build Spec §6.2 and `status` within the allowed set; the backlink projection
  in pages matches the graph's `active` edges (round-trips without divergence); no
  slug-keyed edges; `proposed`/`rejected` edges are never projected.
- `validate_wiki.py` extensions (slices 3/4): claim/concept/entity pages carry required
  frontmatter and stable ids; candidate concepts stay out of promoted navigation.

---

## 10. Testing plan (per slice, before commit)

Mirrors the ADR-0028 acceptance contract; all deterministic pieces are tested offline,
LLM passes are tested with the fake-adapter `LLMClient` (as in 3.5a).

- Slice 2 (graph): assertion upsert idempotency; **multiple evidence spans / asserters**
  for the same `(src, dst, edge_type)` coexist as separate rows and do not overwrite each
  other; rename = id-level redirect preserves assertions; **review-gated** — `proposed`,
  `rejected`, and `superseded` assertions never project, an `active` one does, and a
  **deferred** review item leaves the assertion `proposed` (neither activated nor deleted);
  **edge vocabulary** — an `edge_type` outside §6.2-minus-`needs_review` fails
  `validate_graph` (a literal `needs_review` edge is rejected), each allowed type maps to
  its §6.2 semantics; **node authority** — a rename/status change has exactly one source of
  truth (frontmatter/manifest) and a deterministic projection, and rebuilding the `nodes`
  index does not mutate any edge; **projector round-trips** — a graph `active` assertion
  with no page link fails, and a page link with no `active` assertion fails unless it is a
  `proposed` candidate.
- Slice 3 (claims): a claim whose citation fails grounding is dropped; a grounded claim
  writes a page + `derived_from` edge; re-run is idempotent and the Source page does not
  churn; **no placeholder `[[Claims/{{...}}]]`/`[[Sources/{{...}}]]` survives rendering**.
- Slice 4 (concepts/entities): pages get stable ids + aliases; `mentions` edges written;
  only graph-backed links rendered.
- Slice 5 (promotion): same source, duplicate (same-`source_id`) source, same-family
  source, and two independent sources each behave correctly — 1 source → `candidate`,
  2 independent → `active`, same-family 2nd → review item (never auto-promotion), and
  promotion only proposes while provenance fields are unpopulated.

---

## 11. Non-goals (3.5b)

- Cross-source synthesis and contradiction detection (3.5c).
- Retrieval, ranking, and cited answering (Phase 4/5).
- Autonomous/scheduled enrichment (Phase 7).
- Concrete local-model deployment (only the adapter seam is fixed, ADR-0025).

---

## 12. Completion

3.5b is complete when extracted sources carry grounded claims and candidate
concepts/entities, the graph is the source of truth with backlinks projected from it,
promotion runs on independent-source recurrence with review gating, and the full validator
suite (citations + graph + wiki) passes. Synthesis/contradiction (3.5c) follows on the
proven graph.
