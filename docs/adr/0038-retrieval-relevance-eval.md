# ADR-0038 — Retrieval relevance eval corpus

**Status:** Accepted. v1 (decisions 1–6) **implemented** + per-channel failure diagnostics added (commits
through `2a0be5e`); the real-embedder baseline confirmed both current failures are
`vector_prefers_irrelevant_keyword_silent` (semantic ambiguity, not fusion). The **Multi-chunk extension**
(below) is design-locked 2026-06-25 via a follow-up grill and **implemented** the same day (3 net-new
`larkfield_*.md` fixtures + 8 `chunk_disambiguation` cases). The post-add baseline (`BAAI/bge-m3`, 2026-06-25)
left the source-level headline **unchanged** (M7) and scored chunk discrimination **perfect** — no chunk
failure surfaced channel *disagreement*, so **weighted RRF stays deferred**. (Exact numbers live in the
committed labeled reference report `evals/reports/reference_baseline.md`, not in this ADR — decision 6.)
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

**2. Committed curated corpus.** `evals/corpus/` holds **short, original (license-clean) prose docs**
across a few topics with deliberate overlap/ambiguity (started at ~6–12; now **15** = 12 source-level +
the 3 multi-chunk `larkfield_*.md` fixtures). Reproducible + shareable; the runner
can optionally target a real operator vault instead (`--vault`), but the committed corpus is the baseline.

**3. Golden file, referenced by filename.** `evals/golden_retrieval_relevance.yaml` (separate from the
structural `golden_retrieval.yaml`), across **four required source-level categories**: `exact_anchor`,
`conceptual` (paraphrase), `multi_source`, `disambiguation` (negative). **Paraphrase + negative are the
fusion differentiators.** (Originally ~20–30 cases; the committed file has since grown to **52 source-level
cases** + the 8 chunk-level cases from the Multi-chunk extension.) Cases reference corpus docs by **stable
filename** (the runner maps `filename → source_id` via the manifest `original_filename`) — content-hash ids
are brittle. Minimal-YAML subset (block lists, no inline `{...}`):

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
# (chunk-level cases use the `chunk:`+`near_miss:` PHRASE form from the Multi-chunk extension below, not a
#  char-span block — the v1 char-span stub was never used and is removed.)
```

**4. Source-level binary relevance; recall@k + MRR + success@k.** Map each evidence chunk → its
`source_id`; a query's judgment is a binary set of relevant sources. (Chunk-level behavior is *not* a
char-span on a source-level case — it is its own `chunk_disambiguation` case type with phrase locators, see
the Multi-chunk extension below; the original char-span stub was never used and is removed.) Metrics:
**recall@k** (fraction of expected sources in top-k),
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

## Multi-chunk extension (design-locked 2026-06-25 via follow-up grill)

The v1 corpus docs are tiny (one chunk each), so the benchmark only tests **source-level** semantic
similarity — not the **chunk-level** ranking/fusion that weighted RRF (ADR-0032 add. 9) would tune. This
extension adds chunk-level cases. No retrieval-logic change; deterministic; key-free CI stays green.

**M1. Chunk locator = a `contains:` phrase resolved to the citation key.** A chunk-level case names its
chunk by a distinctive substring, *not* char spans (brittle) or `chunk_id` (advisory only, ADR-0029/0032).
The runner maps `source` filename → `source_id`, reads `normalized/chunks/<source_id>.jsonl`, finds the
**exactly one** chunk whose text contains the phrase, and resolves it to the **authoritative citation key
`(source_id, char_start, char_end)`** — the same identity fusion/dedup and citations use. `chunk_id` may
appear in diagnostics only. The brittle char-span `chunk:` stub from v1 is **removed**.

**M2. Schema: `chunk:` + `near_miss:`; a case is chunk-level iff it has `chunk:`.** (Example uses the real
`larkfield_warehouse.md` fixture — two adjacent `##` sections, one chunk each.)
```yaml
- id: chunk_cold_zone_picker_rotation
  category: chunk_disambiguation        # new category; the 4 source-level categories are unchanged
  mode: auto
  query: how often do pickers in the cold storage zone rotate out?
  chunk:                                # the RELEVANT chunk (also fixes the relevant source)
    source: larkfield_warehouse.md
    contains: "rotate out every forty minutes"
  near_miss:                            # the distractor chunk (intra-doc — the headline test)
    source: larkfield_warehouse.md
    contains: "standard two-hour shifts with no rotation"
```
The relevant *source* is derived from `chunk.source` (no duplicate `relevant:`). Existing source-level
cases (`relevant`/`irrelevant` filename lists) are untouched and scored exactly as before.

**M3. Reporting split — chunk cases never contaminate the source headline.** `## Aggregate` stays
**source-level cases only**. New `## Chunk-Level Aggregate` covers chunk cases only. A separate, explicitly
diagnostic `## Chunk Source Continuity` reports whether `chunk.source` was retrieved at all — kept out of
both headlines, so an intra-doc case can't look "source-correct" while failing the chunk behavior.

**M4. Chunk metrics + chunk-granular diagnostic** (keyed on the citation key, mirroring source-level):
`chunk_recall@k`, `chunk_hit@k`, `chunk_MRR`, **`chunk_discrimination`** (the relevant chunk ranks above
`near_miss` — the headline chunk signal), `chunk_neg@k`, per-query `first_chunk_rank` /
`first_near_miss_rank`. The **per-channel diagnostic is recomputed at chunk granularity** from
`evidence[].channels` with the same label taxonomy — *this* is the payoff: a chunk failure labelled
`keyword_prefers_relevant_vector_prefers_irrelevant` is the **fusion-balance** signal that would finally
justify revisiting weighted RRF; `vector_prefers_irrelevant_keyword_silent` etc. means the embedder can't
separate the chunks (RRF can't help). The decisive report section is the **failed chunk-disambiguation
diagnostics**.

**M5. Authoring contract + validation.** Multi-chunk docs use distinct **`##` sections** to force chunk
boundaries (the chunker is heading-aware: a heading flushes the prior section into its own chunk; keep each
section under the chunk target so it's one chunk). **Intra-doc near-miss** (two adjacent sections of one
doc) is the headline benchmark. The 3 net-new fixtures are `larkfield_warehouse.md` /
`larkfield_returns.md` / `larkfield_membership.md` (two `##` sections each, ~340–440 chars/section so each
is one chunk), with 8 `chunk_disambiguation` cases. The runner
validates each phrase resolves to **exactly one** chunk *and* that `chunk` and `near_miss` resolve to
**different citation keys** — 0 matches / >1 matches / same key → curation error → **skip + report** (like
an unresolved filename). The key-free coherence test checks each `contains:` phrase is a substring of its
named corpus doc and `chunk.contains != near_miss.contains`; chunk uniqueness/separation needs extraction
and is the runner's eval-time check. The plumbing test uses a tiny two-`##`-section doc to exercise
chunk scoring/reporting + the skip path with the fake embedder.

**M6. Fixtures are net-new, topically-isolated docs (not restructures).** The ~3 multi-chunk docs are
**brand-new** files on a fresh topic cluster — topically adjacent enough to exercise ambiguity, but
**isolated from the existing source-level cases**: a new doc must never appear as `relevant`/`irrelevant`
in any existing source-level case. Existing corpus docs (e.g. `Agentic_1.md`) are **not** restructured into
`##` sections — that would change one slice's content *and* add chunk behavior at once. The M2/M5 example
names/phrases above are **illustrative**; once the real fixtures exist, repoint those examples at the actual
filenames. (Grill follow-up, 2026-06-25.)

**M7. Source-level baseline integrity — content unchanged ≠ rankings unchanged.** Even with the existing 12
docs byte-identical, adding docs **enlarges the retrieval candidate set**, so existing source-level rankings
*can* shift. The pre-add baseline (numbers in `evals/reports/reference_baseline.md`) is therefore "content
unchanged, low expected risk," **not** mathematically invariant. After adding the new docs the runner's
**source-level headline must be re-run and diffed explicitly** against the recorded pre-add baseline, and any
movement reported — this is a success criterion, not optional. (Grill follow-up, 2026-06-25.)

**Success criteria:** the ~3 docs + 6–10 chunk cases exist; the new docs are absent from every existing
source-level case (M6); the runner resolves/validates and renders the three new report blocks with the source
headline uncontaminated; coherence + plumbing tests green; key-free CI green; the relevance run stays opt-in;
**the post-add source-level headline is diffed against the pre-add baseline and any movement reported (M7)**.
**Weighted RRF stays deferred** until a chunk failure surfaces channel *disagreement* at the chunk level.

**Result (run 2026-06-25, `BAAI/bge-m3`, vector_schema=1/embed_code=1/cosine, `graph_present=false`):** all
criteria met. Post-add source-level headline **unchanged** (no movement vs the pre-add baseline); chunk-level
discrimination **perfect** over the 8 cases, 0 skipped; no failed chunk disambiguation → no channel
disagreement → **weighted RRF stays deferred**. The benchmark layer now exists to detect disagreement under
harder cases or a weaker embedder. **Exact decimals are intentionally NOT committed in this ADR** (decision
6 — embedder-dependent / CI-irreproducible); they live in the one tracked, labeled reference report
`evals/reports/reference_baseline.md` (regenerate via `scripts/eval_retrieval.py --out …` to diff).
**Update policy:** the reference report is refreshed only on an **explicit baseline reset**, not on every
corpus/golden change — it is a dated informational snapshot, and routine commits must not depend on a
running embedder.
