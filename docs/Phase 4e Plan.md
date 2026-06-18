# Phase 4e Plan — RRF hybrid fusion + retrieval eval harness (final Phase 4 slice)

**Status:** ✅ **Complete** (design-locked 2026-06-18 via grill gate). 4e-1 RRF fuser + shape; 4e-2
`mode=auto` conceptual-default + escalation blend + graceful degradation; 4e-3 eval harness
(`evals/golden_retrieval.yaml` + `tests/test_retrieval_evals.py`, 8 categories). **Phase 4 complete.**
**Governing ADR:** [ADR-0032](adr/0032-phase-4-retrieval-architecture.md) decision 6 + **addenda 5–8**.
**Predecessors:** 4a (keyword/nav index), 4b (graph read), 4c (router + `/search`), 4d (vector channel).
Phase 4e is the **last** Phase 4 slice; it adds **no new dependencies**.

> [!summary]
> Phase 4e fuses keyword + vector chunk evidence into one ranked `evidence[]` via **Reciprocal Rank
> Fusion** (`k=60`, policy-tunable), and finally blends vector into `mode=auto` using a
> **conceptual-default + escalation** rule. Fused hits keep per-channel detail in an additive
> **`channels`** field; `auto` **degrades to keyword-only** (a top-level `notes`, never 503) when the
> embedder/index is unavailable. A **pytest + fake-embedder** retrieval-eval harness
> (`evals/golden_retrieval.yaml` + `tests/test_retrieval_evals.py`) gates the 8 behavior categories
> deterministically and key-free. Graph + navigation stay separate groups; weighted fusion + graph
> boosts remain deferred.

---

## 1. Scope
**In scope:** RRF fusion of the keyword + vector chunk-evidence channels into one ranked `evidence[]`;
`mode=auto` conceptual-default + escalation blend with graceful degradation; the additive `channels`
per-hit field + a top-level `notes` field; `rrf_k` in `retrieval.yaml`; the
`evals/golden_retrieval.yaml` cases + the `tests/test_retrieval_evals.py` harness.

**Out of scope (deferred):** weighted/score-normalized fusion, graph relevance boosts, a standalone
CLI eval runner, real-model semantic-relevance evals. **Phase 5:** `POST /query`, LLM answer
synthesis, the `"No source found in vault."` answer text.

**Invariants:** RRF fuses **chunk evidence only** — graph + navigation remain separate response
groups. Citation authority stays `(source_id, char_start, char_end)`. The deterministic stack and the
whole suite stay key-free (fake embedder).

---

## 2. RRF fusion (ADR-0032 addendum 7)
- **Inputs:** the per-channel pre-fusion hit lists — keyword (BM25, 4c) and vector (distance, 4d) —
  each already retention-filtered and capped to `per_channel_prefusion_limit`, each in its own rank
  order.
- **Score:** for each chunk, `rrf = Σ_channels 1/(k + rank_c)` over the channels that returned it;
  `k = rrf_k` (`retrieval.yaml`, default 60). `rank_c` is the chunk's 1-based position in channel
  `c`'s ordered list.
- **Dedup key = `(source_id, char_start, char_end)`** (`chunk_id` advisory). A chunk returned by both
  channels collapses to one fused evidence object.
- **Output order:** `rrf` descending, final deterministic tie-break `(source_id, ordinal, char_start)`;
  truncated to `max_evidence_hits`.
- **Per-hit shape (additive — base citation unchanged):**
  - top-level `score` = the RRF fused score; `retrieval_path` = contributing channels;
  - `channels`: `{"keyword": {"rank": N, "score": <bm25>}, "vector": {"rank": M, "score": <distance>}}`
    — present for single-channel hits too (just one entry). `channels.*.rank` is 1-based per-channel;
    `channels.vector.score` is distance (lower = better).
- **Models:** add `channels: dict[str, ChannelRank]` to `EvidenceHit`; `ChannelRank{rank:int, score:float}`.

---

## 3. `mode=auto` blend + degradation (ADR-0032 addenda 5–6)
- **Activation by shape** (the router decides; embed the query **only** when vector will run):
  - `default` / broad conceptual → `[keyword, vector]`, fused via RRF.
  - `exact`, `mention` → keyword/graph as today; **add vector only on escalation** — keyword evidence
    count `< escalation_primary_below_k`.
  - `discovery` / `relationship` / `disagreement` → navigation/graph primary; escalate the
    evidence-producing path only (graph-only shapes defer vector).
- **Degradation:** if vector is selected but unavailable, `auto` **runs keyword-only** (never 503),
  `retrieval_path` reflecting the channels that actually ran. A `notes` entry is added **only for a
  genuine degradation** — an embedder *is* configured but vector can't serve (index missing/stale/
  incoherent, embedder down). A **keyword-only deployment** (no embedder / `vector` extra absent)
  degrades **silently** (no note). The strict 503 posture (ADR-0033) stays for **explicit**
  `mode=vector`.
- **Capability laziness:** the endpoint inspects vector state only when the request *could* run vector
  (`search.may_use_vector`) — graph-only auto shapes skip the index-status check entirely; the query
  is embedded only when vector actually runs. A vector-backend failure surfaces as a typed
  `VectorUnavailable` (mapping/impl bugs are not swallowed).
- **Single-channel auto** (keyword-only result) returns plain keyword hits — `channels` still present
  (just `{"keyword": …}`) so the shape is uniform.

---

## 4. Response model additions
- `EvidenceHit`: + `channels: dict[str, {rank, score}]`.
- `SearchResponse`: + `notes: list[str] = []` (degradation/diagnostic messages; empty in the normal
  case). `retrieval_path` already lists channels run; for fused evidence it includes both.

---

## 5. Policy (`retrieval.yaml`)
- Add `rrf_k: 60` under `caps:` (or a `fusion:` block). Document: canonical RRF constant; larger `k`
  flattens rank influence. `escalation_primary_below_k` already present (used by the auto blend).
- Reword the router comment (done in 4d) to "vector joins `auto` via RRF in 4e" — now realized.

---

## 6. Eval harness (ADR-0032 addendum 8)
- **`evals/golden_retrieval.yaml`** — cases: `{id, mode, query, filters?, expect: {...}, category}`,
  kept separate from the answer-shaped `evals/golden_questions.yaml` (Phase 5).
- **`tests/test_retrieval_evals.py`** — builds a small **programmatic fixture vault** (chunks + wiki
  pages with varied status + a graph with nodes/edges incl. a `contradicts` pair), builds the keyword
  + vector indexes with the deterministic **`FakeEmbedder`**, loads the YAML, and runs each case
  through **`run_search()` directly** (not the HTTP server). Key-free, CI-gating.
- **Categories (Plan §6.2) + pass-criteria:**
  1. Exact-anchor retrieval — keyword hit returns the right `(source_id, char_start, char_end)`.
  2. Status-aware navigation — candidate pages `answer_eligible:false`, active `true`.
  3. Graph depth/caps — neighborhood respects default 1 / max 2 + caps; `truncated` correct.
  4. Router taxonomy — §8.2 shapes classify to the expected mode-set (incl. conceptual→vector).
  5. FTS-safe malformed queries — quotes/operators/parens never crash; structural-empty/sane.
  6. Vector citation carry-through — a vector hit carries the full citation shape (incl. `kind`).
  7. **RRF shape/order determinism** — identical query + index ⇒ identical ranked `evidence[]`; a
     keyword∩vector hit merges with `retrieval_path:["keyword","vector"]` + both `channels` entries.
     Asserts ordering/shape, **not** semantic relevance (fake embedder).
  8. Retention filtering — `deprecated_candidate` searchable by default; archived/deleted excluded
     unless requested.
- **Determinism:** in CI, the fake embedder makes vector ordering reproducible. Real-model determinism
  (index version + `embedding_model_ref`) is a documented smoke concern, not the gate.

---

## 7. Sub-slices
| Slice | Deliverable |
|---|---|
| **4e-1** | RRF fuser + `channels`/`notes` model fields; `rrf_k` in `retrieval.yaml`; explicit `mode=vector`/`mode=keyword` unchanged in output except `channels` now present. |
| **4e-2** | `mode=auto` conceptual-default + escalation blend + graceful degradation (`notes`). |
| **4e-3** | `evals/golden_retrieval.yaml` + `tests/test_retrieval_evals.py` (8 categories). |

(May land as one commit; listed for ordering.)

---

## 8. Success criteria (Phase 4e + Phase 4 done when)
- RRF fusion is deterministic; fused hits carry the RRF `score`, `retrieval_path`, and `channels`.
- `mode=auto` blends vector per the conceptual-default + escalation rule and **degrades to
  keyword-only** (with a `notes` entry) when vector is unavailable — never 503.
- `evals/golden_retrieval.yaml` runs green across all 8 categories (key-free, fake embedder).
- Full suite green, ruff clean, all validators green. **Phase 4 (Search & Graph) complete** →
  Phase 5 (Query & Cited Answering) is next.

---

## 9. Deferred (not Phase 4e)
- Weighted/score-normalized fusion + graph relevance boosts (revisit with real-model eval evidence).
- Standalone CLI eval runner + real-model semantic-relevance evals.
- Adaptive/multi-step router escalation beyond the single `< k → vector` rule.
