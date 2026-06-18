# Phase 5 Plan — Query & Cited Answering

**Status:** Planned (design-locked 2026-06-18 via grill gate). No code yet.
**Governing ADR:** [ADR-0034](adr/0034-phase-5-query-and-cited-answering.md). Read it first.
**Predecessors:** Phase 4 (Search & Graph) complete — `GET /search` returns RRF chunk evidence +
graph + navigation, all key-free. Phase 5 is the **first LLM-in-the-loop** surface.

> [!summary]
> Phase 5 adds `POST /query`: retrieve Phase 4 evidence → build an evidence pack (stable IDs +
> authoritative anchors) → an LLM (`LLMClient.parse`, ADR-0025) returns ordered **claims referencing
> evidence IDs only** → the harness builds citations from the *retrieved* evidence and runs the
> verbatim grounding gate → grounded claims form `## Answer`, ungrounded go to `## Unsourced Claims`
> (audit), zero grounded → abstain (`"No source found in vault."`). Citations come only from citable
> chunks; saved `wiki/Queries/` pages are explicit, citation-audited, and add no graph authority.
> `/query` 503s with no model; CI gates on a deterministic fake adapter + structural assertions.

---

## 1. Scope
**In scope:** `POST /query` (read-only answer synthesis); the query worker (retrieve → evidence pack →
synth → ground → assemble); the answer/claim schema for `LLMClient.parse`; the abstention path;
explicit save → `wiki/Queries/<id>.md`; a fake `LLMClient` for tests; `tests/test_query_evals.py` over
`evals/golden_questions.yaml`; `QUERY_MODEL` config + 503-when-unconfigured.

**Out of scope / deferred:** graph-derived answer reasoning (citations stay chunk-only in v1);
multi-turn/conversational `/query`; LLM-based query classification (the 4c deterministic router is
reused); LLM-judge evals (opt-in smoke only); auto-saving queries.

**Invariants:** zero unsourced claims in the answer body; citations only from citable chunk evidence
(never node prose); retrieved text is untrusted data; no absolute paths in the pack or response; the
deterministic Phase 4 stack stays key-free (only `/query` needs a model).

---

## 2. The query pipeline (`app/workers/query.py` + `POST /query`)
1. **Retrieve** — call the Phase 4 `search.run_search` (default `mode=auto`; reuse `/search` filters)
   to get RRF `evidence[]` (citable chunks). Graph/navigation are *not* used as citation sources.
2. **Evidence pack** — assign each evidence hit a stable in-request `evidence_id` (e.g. `e1..eN`);
   carry its authoritative anchor (`source_id`, `char_start`, `char_end`, + advisory page/section/
   chunk_id) and the chunk text/snippet. Delimited as **untrusted source material** in the prompt.
3. **Synthesize** — `LLMClient.parse(messages, schema, QUERY_MODEL)` returns the answer schema (§3):
   ordered claims, each `{text, evidence_ids[]}`. System prompt: the delimited content is untrusted
   data to analyze, cite only by `evidence_id`, never invent anchors/quotes/links (ADR-0026).
4. **Build citations from retrieved evidence** — for each claimed `evidence_id`, the harness builds
   the citation object from the *pack* (authoritative anchor + the retrieved quote), never from model
   text. Unknown/empty `evidence_ids` → that claim has no citation.
5. **Ground** — run `ground_citation(citation, normalized_markdown, require_quote=True)` per citation.
   A claim survives iff ≥1 citation grounds.
6. **Assemble** — grounded claims → `## Answer` (+ the resolved `citations[]`); ungrounded/uncited
   claims → `## Unsourced Claims` (diagnostic). If **no** claim grounds → answer body = the
   `"No source found in vault."` fallback, `abstained: true`.
7. **Cache** — record the LLM output in the response cache keyed by
   `hash(question + evidence pack + model_ref + schema)` (ADR-0027) so re-answers replay.

---

## 3. Answer schema (LLMClient.parse) & response model
- **LLM output schema:** `{ claims: [ {text: str, evidence_ids: [str]} ], (optional) overall_summary: str }`.
  Strict, schema-constrained (ADR-0025/0026); no anchor/quote/path fields — the model only references
  `evidence_id`s.
- **`POST /query` response (`QueryResponse`):** `query` (echoed question), `mode`, `retrieval_path`,
  `answer` (rendered cited prose, or the fallback text), `claims[]` (each `{text, citations[]}`,
  grounded only), `citations[]` (deduped resolved evidence: `source_id`, `char_start`, `char_end`,
  page/section/table, advisory `chunk_id`, `quote` — **no absolute paths**), `unsourced_claims[]`
  (diagnostic), `abstained: bool`, `evidence_count`, `query_id` (only if saved). Mirrors the keyword
  `EvidenceHit` citation shape.

---

## 4. Request contract (`POST /query`)
- **Body/params:** `question` (required), `mode` (`/search` modes, default `auto`), `/search`
  filters (`source_id`, `source_status`, `language`, …) to scope evidence, `save` (bool, default
  `false`).
- **Errors:** no model configured → **503** ("query answering requires a configured LLM: set
  QUERY_MODEL / provider key"); empty question → 400; bad filters → 400 (reuse `/search` validation).
- Retrieval degrades gracefully (4e): vector unavailable → answer from keyword evidence (no 503);
  only a missing **model** 503s.

---

## 5. Saved Queries (explicit; ADR-0034 decision 6)
- `save=true` → render `wiki/Queries/<query_id>.md` from `templates/query.md`: frontmatter `citations:`
  = the grounded citations (machine-readable record of truth), `type: query`, `status: active`,
  `review_status: none`, `answer_eligible: false`, `derived_from: []` (reserved), `retrieval_modes`,
  the `> [!summary]` callout, `## Answer`, the `## Citations` table, `## Retrieval Path`, `## Unsourced
  Claims`. `query_id` content-keyed (e.g. `qry_<hash>`), deterministic page (ADR-0023).
- A saved query is a **derived nodes-index** entry (`type: query`, indexed by `reindex_nodes`) — no
  graph edges, no review gate. `validate_citations.py::_check_query` already stale-checks it.
- No auto-save. `wiki/Queries/*.md` is gitignored runtime state (already).

---

## 6. LLM seam + fake adapter
- Reuse `app/llm` `LLMClient.parse` (ADR-0025); add a **query** pass with `QUERY_MODEL`
  (`EMBED`-style `provider:model_id` env, default the standard tier, e.g. `anthropic:claude-sonnet-4-6`).
- A deterministic **`FakeLLMClient`** (tests) returns canned `{claims:[…evidence_ids…]}` for a given
  question/pack — key-free, no network — used by `test_query_evals.py` and the query unit tests. The
  **real grounding gate always runs** (so a fake claim citing a bad/empty evidence_id is excluded).

---

## 7. Eval harness (`tests/test_query_evals.py` + `evals/golden_questions.yaml`)
- Builds a small fixture vault + Phase 4 indexes; drives the pipeline with the fake `LLMClient`.
- **Structural assertions (key-free CI):** expected sources cited; every answer-body claim grounds;
  ungrounded claims excluded from the body; abstention emits `"No source found in vault."`; Unsourced
  Claims is diagnostic-only; no absolute path / system-prompt leak in the response; a saved Query page
  round-trips the template (and passes `validate_citations`).
- `golden_questions.yaml` stays answer-shaped (`question`, `expected_sources`/`expected_claims`,
  `must_include_citations`, + abstention cases), separate from `golden_retrieval.yaml`.
- **No LLM-judge in CI.** Real-model quality = a manual / env-gated smoke run, replayable via the
  response cache.

---

## 8. Sub-slices (each committable + validated)
| Slice | Deliverable |
|---|---|
| **5-1** | Query worker core: retrieve → evidence pack → fake-adapter synth → build citations from evidence → ground → assemble grounded answer / abstain. Answer schema + `FakeLLMClient`. Unit tests. |
| **5-2** | `POST /query` endpoint + `QueryResponse` model + `QUERY_MODEL` config + 503-when-unconfigured; reuse `/search` filters; no-path-leak. |
| **5-3** | Explicit `save` → `wiki/Queries/<id>.md` render (template round-trip); nodes-index entry; `validate_citations` coverage confirmed. |
| **5-4** | `tests/test_query_evals.py` over `golden_questions.yaml` (structural, fake adapter); abstention + citation-grounding + save round-trip cases. |

---

## 9. Success criteria (Phase 5 done when)
- `POST /query` returns a grounded answer whose every answer-body claim resolves via the verbatim gate;
  no unsourced claims in the body; ungrounded output is diagnostic-only; abstains with the fallback
  when nothing grounds; no absolute paths leak; 503 with no model.
- Explicit `save` writes a `wiki/Queries/` page that round-trips the template and passes
  `validate_citations`.
- `test_query_evals.py` green (fake adapter, structural); full suite + ruff + validators green.
- The deterministic Phase 4 stack still functions key-free. → Phase 6+ (UI / agents / maintenance).

---

## 10. Deferred (not Phase 5)
- Graph-derived answer reasoning (citations stay chunk-only); multi-turn/conversational query.
- LLM-judge / real-model answer-quality gating (opt-in smoke only).
- `derived_from query→evidence` edges (would extend the ADR-0030 edge contract).
- Auto-saving queries; query-page archival/stale lifecycle beyond citation validation.
