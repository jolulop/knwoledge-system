# ADR-0056 — Tier-2 document-complete extraction coverage

- **Status:** design-locked (not implemented)
- **Date:** 2026-07-06
- **Drivers:** UAT run 2 finding F3 (12k-char head bias); live-vault ADR-0055 v2 rollout
  (2026-07-06) leaving 10 of 23 substantive sources concept-starved
- **Related:** ADR-0012 (chunk anchor contract), ADR-0025 (LLM adapter), ADR-0026 (untrusted
  input), ADR-0027 (fingerprint/cache), ADR-0030 (graph schema), ADR-0033 (config-ref staleness
  precedent), ADR-0054 (rollout posture), ADR-0055 (tier-2 extraction contract, replacement-only
  supersede)

## Context

Every enrichment prompt builder truncates its input at `max_chars=12000`
(`app/llm/prompts.py`), so the model only ever sees a document's head — cover matter, TOC,
executive-summary boilerplate. The ADR-0055 v2 prompt rollout on the live vault (23 sources)
proved the consequence: the `concept_starvation` guard fired on **10 sources, all substantive**
(14–137KB of normalized Markdown: a global banking annual review, a quantum technology monitor,
M&A playbooks). For the 137KB worst case, 12k chars is ~9% of the document. Claims share the
bias: statements beyond the first 12k chars are never candidates, so the factual layer feeding
contradictions, synthesis, and `/query` is silently incomplete even though every extracted
citation grounds correctly.

## Decisions

### 1. Scope: tier-2 only, "document-complete" defined

This slice makes tier-2 extraction coverage **document-complete** for claims and
concepts/entities. *Document-complete* means every extractable body region gets an opportunity
to be seen by the model — not necessarily that the entire document is sent in one prompt.
Tier-1 summary/tags remain head-biased by known, documented limitation (navigational layer,
cheap tier; changing it churns every summary fingerprint and the `summary_rot` contract) and
are deferred to a possible future slice.

### 2. Differentiated strategy per pass

The two passes have opposite output profiles, so they get different mechanisms:

- **Concepts/entities — one full-document call.** The concepts side is bounded by the
  ADR-0055 band (3–10 per *document*, regardless of length). The entity side is **not**
  bounded by ADR-0055 — its boundary is a salience filter with no count contract, and
  full-document input makes more entities *legitimately* substantive, risking
  `ENRICH_MAX_TOKENS` truncation and review-queue re-flooding (review round 1, blocking).
  This slice therefore adds an **entity soft band** to the prompt contract (not schema
  validation): *typically up to ~25 central entities per document; include more only when they
  are substantively central, not merely mentioned* — most-central first, fewer always
  acceptable, never pad. The contract change rides `CONCEPT_PROMPT_VERSION` →
  `enrich-concepts-prompt-v3`, pinned by a prompt-contract test. With both bands, 10 concepts
  + ~25 entities with aliases sit comfortably inside `ENRICH_MAX_TOKENS=4096`. The input
  ceiling rises from 12k to `ENRICH_CONCEPT_INPUT_MAX_CHARS` (decision 5). Windowing concepts
  is rejected: a per-window "3–10 most-central" union would either overproduce review noise
  (re-creating the ADR-0055 failure) or require an LLM consolidation pass — more architecture
  than this failure needs.
- **Claims — windowed map + deterministic merge.** Claim output scales with document length; a
  single full-document call would pile every claim into one response and hit
  `ENRICH_MAX_TOKENS` (the F2 `max_tokens` failure mode, unfixable by raising the knob).
  Windows bound both input and output per call. Quotes are located **inside the window text**,
  then translated to full-document offsets by adding the window's `char_start` — never by
  first-match search over the whole Markdown, which can anchor a repeated phrase to the wrong
  occurrence. Merge is deterministic: claim identity is the existing text hash, the dedup key
  stays `(claim_id, char_start, char_end)`, and the same claim supported from two windows
  yields one claim node with multiple `derived_from` citations (`uq_edges_assertion` includes
  anchors).

### 3. Claims stage before replacing (supersedes the retract-first block)

For tier-2 claims, extraction **stages the full replacement claim set before mutating
graph/wiki state**. All window calls for a source are collected and validated first; only then
does the worker supersede the source's prior `derived_from` edges and emit the replacement set.
If the source is non-empty and the run cannot produce a complete replacement (missing key,
any window `ParseError`), existing claim evidence remains visible and **nothing is retired**.
If normalized Markdown changed underneath those anchors, validators fail loudly
(`validate_citations`) until `extract_claims` succeeds — **stale-but-visible is preferred over
silent absence**. Empty Markdown is a deterministic complete replacement and may supersede to
an empty claim set (the ADR-0055 branch).

This deliberately reverses the retract-first ordering in `app/workers/claims.py` (the
"retract its prior evidence FIRST" comment, which cites ADR-0030 for the supersede *mechanism*,
not the ordering). With ~5× more calls per source, partial-failure probability rises; one bad
window must not wipe a source's claim layer. The run summary distinguishes
`replacement_not_applied` / `stale_claim_layer_preserved` from ordinary parse errors, so an
operator can tell "nothing changed because staging failed" from "replacement applied with zero
claims".

### 4. Claim windows: greedy runs of persisted chunks (`chunk-greedy-v1`)

Claim windows are **greedy runs of consecutive normalized chunks, ordered by ordinal**, bounded
by the actual full-Markdown span `last.char_end - first.char_start` (headings/blank lines
between chunk spans count toward the budget, because they are included in the window text).
Window text is `markdown[first.char_start:last.char_end]`. Chunks already carry the ADR-0012
mechanical anchor contract (`markdown[start:end] == chunk.text`, enforced by
`validate_normalized`), so paragraph safety and offset exactness are inherited, not
re-implemented.

- **Never split a chunk.** A single chunk exceeding the window budget becomes a singleton
  over-budget window, counted and reported (`claim_window_over_budget`).
- **No overlap (v1).** Calls stay predictable and seam content is never double-extracted. A
  seam-straddling claim is usually **missed** (the model never sees both sides in one window) —
  recorded as an accepted **recall limitation** of no-overlap windows, distinct from
  `claims_dropped_ungrounded` (which counts only quotes the model emitted that fail to ground).
- **Prompt framing:** each window call states it is seeing "segment *i* of *N*" of the titled
  document and includes local section context from chunk metadata. This is a real prompt-text
  change: `CLAIM_PROMPT_VERSION` → `enrich-claims-prompt-v2`.
- Run metadata: `claim_windows`, `claim_window_over_budget`, `claims_dropped_ungrounded`,
  `claim_window_strategy: chunk-greedy-v1`, plus the staging outcomes of decision 3.

### 5. Config knobs, strategy refs, composed extraction identity

Two knobs, both documented as **cost-bearing semantic knobs** (they change extraction coverage
and intentionally invalidate existing tier-2 artifacts — not performance tuning):

| Knob | Default | Governs |
|---|---|---|
| `ENRICH_CLAIM_WINDOW_CHARS` | `12000` | claim window span budget (full-span) |
| `ENRICH_CONCEPT_INPUT_MAX_CHARS` | `300000` | concepts single-call input ceiling |

Defaults: 12k preserves today's per-call input scale (known-good with `ENRICH_MAX_TOKENS=4096`);
300k (~75–100k tokens) covers the current 137KB worst case >2× over while bounding pathological
inputs inside the model context.

Strict strategy refs carry the values:

- `claims_strategy_ref = "chunk-greedy-v1:{ENRICH_CLAIM_WINDOW_CHARS}"`
- `concepts_strategy_ref = "full-doc-v1:{ENRICH_CONCEPT_INPUT_MAX_CHARS}"`

Extraction identity is **composed** — schema version ∥ prompt version ∥ strategy ref ∥ model
ref ∥ normalized Markdown — and the strategy ref enters both the artifact freshness fingerprint
and the response-cache key. Implementation **extends the LLM seam explicitly**:
`LLMClient.parse()` gains an optional `strategy_ref: str | None = None` and `cache_key()` a
third version component — the ref is never hidden inside prompt/schema version strings
(review round 1). Prompt wording can change without pretending the chunking strategy changed,
and strategy tuning restales without editing prompt constants. Changing either knob restales that pass vault-wide; replaying
old artifacts after a coverage change would be misleading, so this is correct (ADR-0033
config-ref precedent).

A concepts document exceeding the cap is truncated and marked **`coverage: truncated` in the
artifact and job metadata** (not only stdout) — keeping "document-complete" honest and enabling
later lint/eval consumption.

### 6. Rollout: opt-in, operator-gated, falsifiable acceptance

Shipping ADR-0056 changes no vault state. The first `extract_claims.py` and
`extract_concepts.py` run after ship is intentionally a **full tier-2 re-extraction** because
strategy refs restale artifacts/cache. This is expected, billable, and opt-in (ADR-0054 §3
posture; no automation). Chain: `extract_claims` → `extract_concepts` → `promote` →
**`reindex_keyword.py`** → **`rebuild_index.py`** → `validate_all.py`. The explicit reindex
steps are required, not belt-and-braces: the producers refresh Source pages without
guaranteeing keyword/nav index freshness, and the 2026-07-06 live repair demonstrated the gap
(`validate_index_consistency` failed on stale navigation rows after the producer chain until
`reindex_keyword.py --force` ran). This matches the ADR-0054 §3 chain.

**Acceptance is falsifiable against the live vault:**

- `concept_starved` for the known 10 sources must go to **0**. Any residual starved source is a
  real follow-up finding, not acceptable drift.
- `src_9eb8eb1c85413978` (Spanish, zero nodes of any type) stays **explicitly out of scope** if
  it remains zero-node/Spanish-specific — a named separate investigation.
- Claims acceptance records pre/post claim counts for the large documents, plus
  `claim_windows`, `claim_window_over_budget`, `claims_dropped_ungrounded`, parse errors, and
  whether staging applied.

Review-queue posture unchanged: replacement-only supersede retires noise; stale pending
promotes are scope-guard-skipped, cleaned manually (auto-withdrawal remains its own deferred
governance micro-slice).

**Lint: deferred.** Markers first — the `coverage: truncated` artifact field and the new run
metadata enable a future ADR-0037-family report-only lint check; this slice ships no new lint
vocabulary.

## Tests (contract, when implemented)

- **Window planner** (pure function over the chunk table): greedy ordinal grouping; budget over
  the full span including inter-chunk gaps; never splits a chunk; singleton over-budget window
  counted; deterministic for a fixed chunk table.
- **Window-local grounding:** a document with the same phrase in two windows anchors each claim
  to the occurrence in its own window (pins the first-match-over-full-doc bug the design
  forbids).
- **Staging:** window `ParseError` → prior claim layer untouched, summary carries
  `replacement_not_applied`/`stale_claim_layer_preserved`; no-key run → untouched; empty
  Markdown → supersedes to empty set; unchanged-Markdown failed rerun preserves valid claims.
- **Merge:** same claim text from two windows → one claim node, two anchored citations; dedup
  key unchanged.
- **Identity:** knob change → fingerprint and cache key change (restale); prompt-only change
  and strategy-only change are independently legible.
- **Concepts truncation:** above-cap doc → `coverage: truncated` in artifact + job metadata;
  band contract otherwise unchanged (pin against ADR-0055 tests).
- **Entity soft band (prompt contract):** `_CONCEPTS_SYSTEM` pins the "typically up to ~25
  central entities / substantively central, not merely mentioned" wording and the
  `enrich-concepts-prompt-v3` version constant.
- **Rollout-doc guard** (`tests/test_operational_refs.py` family): the ADR-0056 rollout chain
  names `reindex_keyword.py` before `validate_all.py`.
- **E2E:** a multi-window fixture whose distinctive claim lives beyond the first 12k chars is
  extracted, grounded (`markdown[start:end] == quote`), and survives `validate_all`.

## Out of scope (named deferrals)

- Tier-1 summary/tags coverage (head-biased by documented limitation).
- Seam-overlap or two-pass seam repair for claims recall.
- LLM consolidation pass for windowed concepts.
- `coverage: truncated` / window-metadata lint checks (ADR-0037 family, future).
- `src_9eb8eb1c85413978` zero-node/Spanish-language investigation.
- Real-model enrichment-quality eval lane (already deferred by ADR-0055).
