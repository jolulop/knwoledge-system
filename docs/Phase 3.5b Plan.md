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
| 2 | **SQLite graph schema + backlink projector** (SoT, id-keyed edges; deterministic projection into pages) | ADR-0029/0021 | planned |
| 3 | **LLM claim-extraction pass** (tier-2; gated by slice 1; Claim pages; compose into Source pages) | 1, 2, ADR-0021/0022 | planned |
| 4 | **Candidate concepts & entities** (pages, ids, slugs, aliases; edges into the graph) | 2, ADR-0017 | planned |
| 5 | **Promotion lifecycle** (≥2 independent sources; review-gated early promotion) | 2, 4, ADR-0018 | planned |

Rationale for ordering: slice 1 (deterministic, done) is the grounding foundation. The
**graph (slice 2) moves up front** because claims, concepts, promotion, and backlinks all
read/write it — building it first avoids reworking each producer. Producers (3, 4) then
write nodes/edges; promotion (5) is a graph computation over them.

---

## 4. The SQLite graph (slice 2) — source of truth

Per ADR-0029 the graph in `db/` is authoritative; wiki backlinks are a derived projection.
Edges key on the stable typed ids (ADR-0021), never slugs.

Proposed location: `db/graph.sqlite` (separate from `jobs.sqlite` and `llm_cache.sqlite`;
covered by backup, ADR-0014). Proposed tables (to be fixed in the slice-2 ADR):

```text
nodes(node_id PK, node_type, slug, title, status, created_at)
      node_type ∈ source|concept|entity|claim|synthesis
edges(edge_id PK, src_id, dst_id, edge_type, confidence, source_id, created_at)
      edge_type ∈ mentions|evidences|about|supports|contradicts|alias_of|...
```

- **Edges are id-keyed**, so rename/merge is an id-level redirect, not graph surgery.
- The **backlink projector** is a deterministic script that, for each page, renders the
  inbound/outbound edges from the graph into the page's link sections — backlinks are
  synchronized by construction (CLAUDE.md rules 6, 10), never hand-maintained.
- **Authored wikilinks** found in prose are *validated edge candidates* absorbed under
  review, not trusted as edges (ADR-0029).
- Idempotency: edge upserts keyed on `(src_id, dst_id, edge_type)`; the projector is a
  pure function of the graph + page set (no wall-clock).

---

## 5. Claim extraction (slice 3)

- Tier-2 (`ENRICH_MODEL_STANDARD`, default `anthropic:claude-sonnet-4-6`) via the 3.5a
  `LLMClient`. Untrusted-data framing as in 3.5a (ADR-0026).
- The model proposes candidate claims, each with a structured citation. **Every citation
  is run through `ground_citation` (slice 1); a claim with no resolvable, quoted citation
  is dropped and logged** — never written.
- Surviving claims are written as Claim pages (`templates/claim.md`) with a stable
  `claim_id` (ADR-0021), `generation_status: enriched`, `confidence`, `review_status`.
- A `evidences` edge (claim → source) is written to the graph; the Source page's `Claims`
  placeholder is recomposed to list the claims (deterministic projection, like 3.5a).
- Like 3.5a, claim output lands first as a per-source artifact / cache entry; the page is
  the composed view, so re-runs are idempotent and a deterministic rebuild does not churn.

---

## 6. Concepts & entities (slice 4)

- Candidate concept/entity extraction (tier-2). Pages are slug-keyed by canonical name
  with a stable `concept_id`/`entity_id` in frontmatter and an `aliases` list (ADR-0017).
- Created as `candidate`/`stub` (low confidence), kept out of promoted navigation/synthesis
  until promotion (ADR-0018).
- `mentions`/`about` edges (source ↔ concept/entity) written to the graph.

---

## 7. Promotion lifecycle (slice 5)

- A candidate concept promotes to `active` once **≥2 independent sources** evidence it
  (ADR-0018), computed from the graph's `mentions` edges.
- **Independence**: exact (SHA256) duplicates share one `source_id` and count once;
  same-author/publication/report-family sources are flagged for review rather than
  auto-promoting; promotion also weighs confidence, not raw count.
- Early promotion and all entity merge/split, contradiction resolution, and deprecation
  are **human-reviewed** (ADR-0018, `policies/review.yaml`) — the LLM proposes, never
  executes.

---

## 8. Open decisions to resolve per slice

These are framed but not fully pinned by existing ADRs; each gets an ADR or plan update
when its slice starts:

- **Graph schema** (slice 2): concrete `nodes`/`edges` columns, edge-type vocabulary,
  upsert keys, and whether synthesis/claim nodes live in the same tables. *(new ADR)*
- **`claim_id` generation + dedup** (slice 3): hash inputs for the creation-time id
  (ADR-0021), and how re-runs dedup claims (same text + same citation → same claim?).
- **Source-page composition** (slices 3/4): claims/concepts rendered from artifacts +
  graph, preserving the 3.5a single-writer / fingerprint-idempotent property.
- **Independence heuristic** (slice 5): how "same author/publication/report family" is
  detected from manifest metadata, and the confidence weighting.

---

## 9. Validators (extend the lint suite)

- `validate_citations.py` — **done** (slice 1): structured grounding of claim citations.
- New `validate_graph.py` (slice 2): every edge references existing node ids; backlink
  projection in pages matches the graph (round-trips without divergence); no slug-keyed
  edges.
- `validate_wiki.py` extensions (slices 3/4): claim/concept/entity pages carry required
  frontmatter and stable ids; candidate concepts stay out of promoted navigation.

---

## 10. Testing plan (per slice, before commit)

Mirrors the ADR-0028 acceptance contract; all deterministic pieces are tested offline,
LLM passes are tested with the fake-adapter `LLMClient` (as in 3.5a).

- Slice 2: edge upsert idempotency; projector round-trips (Source↔Concept, tombstone/
  redirect) without divergence; rename = id-level redirect preserves edges.
- Slice 3: a claim whose citation fails grounding is dropped; a grounded claim writes a
  page + `evidences` edge; re-run is idempotent and the Source page does not churn.
- Slice 4: concept/entity pages get stable ids + aliases; `mentions` edges written.
- Slice 5: 1 source → `candidate`; 2nd independent source → `active`; same-family 2nd
  source → review item, not auto-promotion.

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
