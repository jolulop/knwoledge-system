# ADR-0038 — Retrieval relevance eval corpus

**Status:** Accepted (design-locked 2026-06-24 via grill gate). Design only — no code yet.
**Extends/relates:** ADR-0032 (Phase 4 retrieval; addendum 8 = the fake-embedder *structural* gate,
addendum 9 = weighted-RRF/graph-boosts deferred *until a real relevance corpus exists* — **this is that
corpus**), ADR-0033 (vector retrieval + the `local_http` embedder seam), ADR-0036 decisions 9/14 (the
answer-quality eval is deferred/manual — kept separate from this). Read `app/backend/search.py`
(`run_search`/`fuse_evidence`), `tests/test_retrieval_evals.py`, `evals/golden_retrieval.yaml`.

## Context

Weighted RRF + graph boosts are **eval-gated** (ADR-0032 addendum 9): RRF is weight-free by design, and the
only retrieval oracle today is the **fake-embedder structural gate** (`test_retrieval_evals`), which
cannot measure semantic relevance. Tuning fusion without a relevance oracle would be unfalsifiable. This
ADR design-locks that missing oracle — a **retrieval-specific** relevance eval: does `run_search` surface
the right citable chunks/sources for a query, measured against human-curated expected evidence. It is
**not** answer-quality grading (no LLM judge; that stays ADR-0036's deferred `/evals/run`).

## Decisions

**1. Scope: retrieval relevance only.** Measure `run_search`'s `evidence[]` (citable chunks) against
human judgments. No answer synthesis, no LLM grading. The goal is to make weighted RRF / graph boosts
*measurable engineering* instead of configuration scaffolding.

**2. Committed curated corpus.** `evals/corpus/` holds **~6–12 short, original (license-clean) prose
docs** across a couple of topics with deliberate overlap/ambiguity. Reproducible + shareable; the runner
can optionally target a real operator vault instead (`--vault`), but the committed corpus is the baseline.

**3. Golden file, referenced by filename.** `evals/golden_retrieval_relevance.yaml` (separate from the
structural `golden_retrieval.yaml`), ~20–30 cases across **four required categories**: `exact_anchor`,
`conceptual` (paraphrase), `multi_source`, `disambiguation` (negative). **Paraphrase + negative are the
fusion differentiators.** Cases reference corpus docs by **stable filename** (the runner maps
`filename → source_id` via the manifest `original_filename`) — content-hash ids are brittle. Minimal-YAML
subset (block lists, no inline `{...}`):

```yaml
version: 1
cases:
  - id: paraphrase_revenue_growth
    category: conceptual            # exact_anchor | conceptual | multi_source | disambiguation
    mode: auto
    query: how did the company's earnings improve last quarter?
    relevant:                       # corpus filenames -> source_id
      - q3_report.md
    irrelevant:                     # optional, disambiguation/negative cases
      - q2_outlook.md
    chunk:                          # optional, exact_anchor span cases only (block form)
      source: q3_report.md
      char_start: 0
      char_end: 40
```

**4. Source-level binary relevance; recall@k + MRR + success@k.** Map each evidence chunk → its
`source_id`; a query's judgment is a binary set of relevant sources (optional chunk-span only where the
span itself is the behavior under test). Metrics: **recall@k** (fraction of expected sources in top-k),
**MRR** (reciprocal rank of the first relevant source), **success@k/hit@k** (any relevant in top-k —
readable when most queries have one expected source). No graded/nDCG in v1. Primary `mode=auto` (the fused
keyword+vector path weighted RRF would tune).

**5. Opt-in real-embedder runner, key-free except the embedder.** `scripts/eval_retrieval.py` builds a
fresh vault from the corpus through the **real** pipeline (intake → extract → keyword index → **real**
vector index via the configured `local_http` embedder) with an **empty graph**, runs `run_search`, scores
`evidence[]`, and emits a report. It needs **no `ANTHROPIC_API_KEY`** (relevance scoring touches no LLM) —
only the embedder. Flags: `--vault <path>`, `-k` (default 5 and 10), `--out`. **Fails clear** when no
embedder is configured/reachable. Report = per-query (relevant-found, first-rank) + aggregate
recall@k/MRR/success@k + per-category, with a reproducibility header: `embedding_model_ref`,
index/extractor version, `rrf_k`, and **`graph_mode=empty` / `graph_present=false`** (so a future
graph-backed variant can't muddy comparisons). Generated reports live under a gitignored `evals/reports/`.

**6. Not a CI gate; the structural eval stays the gate.** The real embedder isn't available in key-free
CI, so this eval is **opt-in/manual**, never in CI (mirrors ADR-0036 decision 14 for answers). The
fake-embedder **structural** eval (`test_retrieval_evals`, ADR-0032 add. 8) remains the green key-free
CI gate, unchanged. **No committed numeric baselines** (embedder-dependent, CI-irreproducible): the
weighted-RRF workflow is *run baseline config → run candidate config → diff the two reports locally*. An
optional committed reference report is acceptable only if clearly labeled (date / embedder /
`embedding_model_ref` / index version) and treated as informational.

## Scope (v1) / out

**In:** the corpus, the golden relevance file, the opt-in runner, the three metrics, the report header.

**Out:** answer-quality grading (ADR-0036's `/evals/run`); a **graph-backed** variant (graph stays empty
until graph boosts exist — then a graph-mode variant is added, distinguished by the header); **recency /
lifecycle** query categories (those belong to a separate *retention-aware ranking* policy decision, kept
out of fusion-weighting per ADR-0032 addendum 9); chunk-primary / graded / nDCG judgments; making this a
CI gate.

## Consequences

- Weighted RRF + graph boosts become **measurable** — a candidate fusion config is judged by diffing two
  local reports over a fixed corpus + pinned embedder, not by intuition.
- Key-free CI is untouched; the new eval is an opt-in operator/maintainer workflow.
- The corpus + golden file double as a **regression guard** an operator can run after an
  extractor/embedder/policy change to catch relevance drift.
