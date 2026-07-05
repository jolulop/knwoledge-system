# ADR-0055 — Tier-2 extraction contract: concept elicitation, entity-noise boundary, concept-starvation guard

- **Status:** implemented
- **Date:** 2026-07-05
- **Drivers:** UAT run 2 (2026-07-05, two real PDFs), user findings F1/F2
- **Related:** ADR-0017 (concept/entity identity), ADR-0018 (promotion), ADR-0025 (LLM adapter),
  ADR-0026 (untrusted input), ADR-0027 (fingerprint/cache), ADR-0037 (lint quality heuristics)

## Context

The Phase 3.5b tier-2 pass (`extract_concepts`, `app/llm/prompts.py::_CONCEPTS_SYSTEM` +
`CONCEPTS_SCHEMA`) is the sole producer of the vault's topic layer. UAT with two real documents
(an arXiv paper and a McKinsey report — both concept-rich) produced:

- **Zero concepts.** The raw cached responses (`db/llm_cache.sqlite`) prove the model
  (`anthropic:claude-sonnet-4-6`) returned `concepts: []` for **both** sources while filling
  `entities` with 48 and 20 items. Not a worker bug: the emit loop consumes `result["concepts"]`
  verbatim. Consequence: no Concept pages, no topics, and downstream no per-concept synthesis and
  no concept-based contradiction blocking.
- **A person flood.** 27 of the paper's 48 nodes were persons — its own author byline plus
  cited-work authors from the references section — each becoming a `candidate` node and a
  `promote_candidate_node` review item (63 pending items from two documents).
- **Claim-extraction truncation (F2).** The default `ENRICH_MAX_TOKENS=1024` truncated claim
  extraction on the dense paper three times (`stop_reason=max_tokens`, three billed calls, zero
  output) before the operator raised it to 4096, which succeeded.

A contributing factor — every enrichment prompt builder truncates input at `max_chars=12000`,
which over-represents title/byline/TOC material — is **out of scope** here (a future
chunked-extraction slice, cost-bearing, own grill).

## Decisions

### 1. Concept contract: expectation band, no schema minimum

`_CONCEPTS_SYSTEM` is reworked to state an explicit contract for `concepts`:

- For substantive prose: return the document's central recurring ideas, frameworks, themes,
  processes, methods, problems, or trade-offs — **typically 3–10, most-central first**, in
  canonical form.
- An **empty list is legitimate but rare**: acceptable only when the source genuinely has no
  durable conceptual content (receipts, OCR noise, raw table dumps, very short administrative
  records).
- **Never invent a concept to satisfy a count.**
- **No named people/organizations/projects/products in `concepts`** — those belong in `entities`.
- Concepts may be abstractions over the text and need not appear verbatim, but must be supported
  by the document's content.

`minItems` on the schema is **rejected**: it would encode a content-quality judgment into schema
validation and create pressure to hallucinate on degenerate inputs. `CONCEPTS_SCHEMA` is
unchanged; `CONCEPT_SCHEMA_VERSION` stays `enrich-concepts-v1`.

### 2. Entity-noise boundary: provenance is not content

The same prompt gains an inclusion boundary for `entities` (domain rationale: provenance authors
and content entities are separate surfaces — manifest provenance fields are the promotion
independence authority; the entity graph models what a document is *about*):

- **Exclude** names that appear only in references, citations, bibliographies, footnotes,
  bylines, author lists, affiliations, acknowledgments, or publisher metadata.
- **Include** persons/organizations/projects/products only when they are discussed in the body,
  perform an action, are affected by one, are compared, evaluated, quoted, or are central to the
  document's claims.
- A document's **own authors become entities only if the substantive text discusses them**, not
  merely because they wrote it.

"Keep own authors" was **rejected** (they are the review-noise majority and already live in
manifest provenance); "salience wording only" was **rejected** (relies on the same unconstrained
model judgment that produced the failure).

### 3. Versioning and cache: prompt-version bump is the rollout switch

`CONCEPT_PROMPT_VERSION`: `enrich-concepts-prompt-v1` → `enrich-concepts-prompt-v2`.
`concepts_fingerprint` covers the prompt version, so every source's artifact goes stale and the
next plain `extract_concepts.py` run re-extracts it; the new prompt text also changes the
response-cache key, so no cache bust is needed. No `--force` machinery.

### 4. Concept-starvation guard: job counts + report-only lint heuristic

The F1 failure mode gets a deterministic, key-free, artifact-driven guard at two layers
(no real-model call in either):

- **Predicate** (per source): `concept_count == 0 AND (entity_family_count >= 5 OR
  claim_count >= 1)`, computed from the on-disk artifacts (`<sid>.concepts.json` node types;
  `<sid>.claims.json` claims). A single claim proves semantic substance; many entities without
  concepts is exactly the F1 pattern. A degenerate document (no entities, no claims) is
  correctly not flagged. The threshold (5) is a module constant, not config (a quality
  heuristic; configurability would add noise before there is operational evidence).
  **Guardrail:** the predicate reads existing artifact/claim state ONLY — it must not infer
  substance from raw text length or normalized text shape, which would reopen the
  "substantive document" classifier problem this design deliberately avoids.
- **Run-time**: the `extract_concepts` job summary reports starved sources
  (`concept_starved` count + source ids in job metadata) so the operator sees the condition
  during the run that produced it.
- **Maintenance**: a new `/jobs/lint` check `concept_starvation` (ADR-0037 family: report-only,
  key-free, never flips `failing`; **not graph-gated** — artifacts only). Severity **medium**
  (it suppresses a source's entire topic layer, more consequential than `summary_rot`'s low).
  `LintFinding.data` carries `source_id` and remediation code **`rerun_extract_concepts`**
  (new code alongside `rerun_enrich`/`rerun_extract_claims`/`rerun_synthesis`).
- **Layer boundary (review round 1)**: the job summary reports only what *this run* wrote — a
  fresh-skipped artifact that is already starved does not appear in the job counts; the lint
  check is the durable layer that flags it regardless of when it was written. Evaluating
  fresh-skipped artifacts at job time would turn the run summary into a second lint.
  Both consumers of the claims artifact validate its internal `source_id` against the filename
  (no spoofing), matching the concepts-artifact posture.

A key-required, opt-in **enrichment-quality eval lane** (real tier-2 pass over committed fixture
docs) is **rejected for this slice** — valuable later, but a new eval surface must not gate a
regression fix; revisit if prompt regressions recur.

### 5. Rollout: opt-in re-run, manual review cleanup

Per the ADR-0054 §3 posture — the operator chooses when to spend tier-2 calls:

```bash
uv run python scripts/extract_concepts.py
uv run python scripts/promote.py
uv run python scripts/reindex_keyword.py
uv run python scripts/rebuild_index.py
uv run python scripts/validate_all.py
```

Re-extraction supersedes each source's prior mentions and tombstones nodes left without active
mentions (existing worker behavior), retiring the noise candidates.

**Rollout safety (review round 1 — blocking fix):** the worker previously superseded a source's
mentions *before* checking provider availability or calling the model. With the v2 bump making
every artifact stale, a key-less (or parse-failing) run would have superseded all mentions,
skipped extraction, and then recomposed the affected nodes to tombstones — wiping the topic
layer while reporting "skipped". The contract is now: **a run that cannot produce the
replacement extraction never supersedes existing mentions.** Supersede happens only (a) after a
successful parse, immediately before emitting the replacement mentions (it retires *all* active
mentions for the source, so it must precede `_emit`), or (b) deterministically for an
empty-markdown source, where "no nodes" is the correct replacement state and the artifact
records it. No-key and ParseError paths leave graph and pages untouched; regression tests pin
both. Pending
`promote_candidate_node` items whose node was tombstoned are **left alone**: executors
scope-guard-skip them; the operator rejects or ignores. Auto-withdrawal on retraction is
**deferred** to a separate small governance slice if queue noise becomes painful (it changes
review-ledger behavior, which identity-surgery treats carefully).

### 6. F2 rides along: `ENRICH_MAX_TOKENS` default 1024 → 4096

`config.py` default and the `.env.example` line change to 4096. UAT-proven default bug, not
speculative tuning: the cap is a ceiling, not a spend (providers bill actual output tokens), and
1024 burns three billed retries on dense documents to produce nothing. **No CLI hinting** — the
adapter's truncation error already names the knob; parsing provider stop reasons in the CLI grows
the diff without fixing the default.

## Tests (implementation slice)

- Prompt-contract pin (structural, key-free): `_CONCEPTS_SYSTEM` carries the band, the
  never-invent clause, the no-named-things-in-concepts clause, and the exclusion boundary;
  `CONCEPT_PROMPT_VERSION == "enrich-concepts-prompt-v2"`.
- Fake-adapter worker test: a response with 0 concepts + ≥5 entities yields `concept_starved`
  job metadata; a response with concepts does not.
- Lint heuristic: starved source flagged with `rerun_extract_concepts`; boundary cases (4
  entities + 0 claims → not flagged; 0 entities + 1 claim → flagged; degenerate 0/0 → not
  flagged; missing artifacts → not flagged); severity/`by_check` counts; report-only (never
  flips `failing`).
- Config: `enrich_max_tokens` default is 4096; `.env.example` documents it (existing doc-drift
  guard style).
- Fingerprint: bumping the prompt version makes a v1 artifact stale (existing fingerprint tests
  extended if not already covering).

## Deferred (named)

- Chunked/multi-call extraction over the full document (removes the 12k-char head bias — the
  other half of the person-flood cause); own grill, cost-bearing.
- Opt-in real-model enrichment-quality eval lane.
- Auto-withdrawal of pending promotes for retraction-tombstoned nodes.
- Any `CONCEPTS_SCHEMA` change (salience/description fields).
