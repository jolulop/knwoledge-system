# Phase 3.5 is delivered as three ordered, independently-shippable sub-phases

Phase 3.5 spans a lot — summaries, tags, concepts, entities, claims, synthesis, and
bidirectional backlinks — and it introduces the system's first non-deterministic,
API-key-dependent, prompt-injection-exposed surface. Landing all of it at once would put
the largest, riskiest change into one step. Instead it ships as three ordered sub-phases,
each tested and committed before the next, with risk rising across them.

- **3.5a — per-source enrichment, no cross-source graph.** Produce an LLM summary and
  tags into the per-source **enrichment artifact** (ADR-0025); the deterministic
  Source-page renderer composes that artifact into the page, flipping
  `summary_status: stub → enriched` by artifact presence. Crucially, enrichment does
  **not** mutate `wiki/Sources/<id>.md` in place, so a later deterministic `generate_wiki`
  rerun re-composes the enrichment rather than reverting the page to a stub — the
  Phase-3 "Source page is a pure projection of its inputs" contract (ADR-0016) is
  preserved by adding the artifact to those inputs, not by exempting the page from
  rewrite. This is the lowest-risk slice and exercises the entire LLM harness end to
  end — the tiered model routing (ADR-0025), the untrusted-input/pure-function stance
  (ADR-0026), the fingerprint + response cache (ADR-0027), and the enrichment-artifact +
  single-writer composition seam (ADR-0025) — on a single, contained pass before any
  graph work.

- **3.5b — semantic nodes and grounding.** Extract claims with mechanical citation
  verification, candidate concepts and entities, apply the ≥2-source promotion lifecycle
  (ADR-0018), and build the bidirectional backlink engine the Build Spec requires
  (§3.5) — the deferred dependency the Phase 3 backbone explicitly recorded as "no
  semantic backlinks yet" (ADR-0016). The graph (SQLite under `db/`) is the source of
  truth and the backlinks are a derived projection of it (ADR-0029). This slice **must
  replace the Phase-3 scaffold `scripts/validate_citations.py`** (which only checks for a
  `sources` frontmatter key and an Evidence section) with a real structured-citation
  validator that enforces the ADR-0019/0020 contract: every claim's
  `(source_id, char_start, char_end)` resolves to an existing normalized source, the
  range is in bounds, the evidence quote occurs verbatim (whitespace-normalized) at that
  range, `chunk_id` is treated as advisory only, and a missing normalized source fails.
  No claim is written to the wiki until it passes this gate (ADR-0026). This is the
  largest slice and depends on the identity, citation, and frontmatter contracts already
  fixed in ADR-0017/0019/0020/0021/0022.

- **3.5c — cross-source synthesis and contradiction detection.** Tier-3 reasoning that
  spans multiple sources/claims, proposing syntheses and contradictions as
  human-reviewed items (ADR-0018, `policies/review.yaml`). Highest-risk and most
  reasoning-dependent, built last on a proven graph.

Each sub-phase carries an acceptance/test contract that must pass before it is committed:

- **3.5a** — (1) *Enrichment preservation*: enrich a Source page, rerun deterministic
  `generate_wiki`, assert the page retains the enriched summary/tags (re-composed from the
  artifact) and does not revert to `summary_status: stub`. (2) *Summary provenance*: the
  enriched summary carries the machine-checkable generated/unverified label and source-level
  provenance the linter enforces (ADR-0026). (3) *Response cache*: the cache key includes
  provider, model id, schema version, prompt/template version, and the source fingerprint,
  and a cache replay performs no provider call (ADR-0027). (4) *Cache backup/retention*:
  backup inclusion (or opt-out exclusion) of the cache matches the stated policy (ADR-0027).
- **3.5b** — (5) *Structured-citation validator*: invalid `source_id`, out-of-bounds
  `char_start`/`char_end`, quote mismatch, advisory-only `chunk_id`, and a missing
  normalized source each fail (ADR-0019/0020). (6) *Graph/backlink consistency*:
  Source↔Concept links, tombstone/redirect links, and graph edges round-trip between the
  SQLite graph and the rendered backlink projection without divergence (ADR-0029).

Consequences: value lands early (browsable, summarized sources after 3.5a) and each
slice is independently validatable, so a regression or a cost/quality surprise is
contained to one sub-phase. The trade is more milestones and the need to keep the
enrichment harness stable as 3.5b and 3.5c build on it; the ordering is a delivery
sequence, not three separate architectures — they share the worker, cache, and contracts
established in 3.5a.
