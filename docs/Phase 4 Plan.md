# Phase 4 Plan — Search and Graph (deterministic hybrid retrieval)

**Status:** In progress (design-locked 2026-06-17 via grill gate). **Slice 4a implemented**
(keyword evidence + wiki navigation index, scaffolds retired, §7 coordination done); 4b–4e pending.
**Governing ADR:** [ADR-0032](adr/0032-phase-4-retrieval-architecture.md). Read it first — this
plan is the operational breakdown of its decisions.
**Predecessors:** Phase 3 (deterministic Source-page backbone), Phase 3.5a/b/c (semantic LLM
layer + graph SoT). Phase 4 adds the retrieval layer; Phase 5 (Query and Cited Answering) adds
LLM answer synthesis on top.

> [!summary]
> Phase 4 builds the deterministic, offline, key-free retrieval layer: a citable keyword index
> over chunks, a status-aware navigation index over wiki pages, a thin read-only graph traversal
> API, a deterministic router, and RRF hybrid fusion — exposed as `GET /search`,
> `GET /graph/node/{id}`, `GET /graph/neighborhood/{id}`. It returns ranked cited *evidence*, never
> generated answers (that is Phase 5). Ships as five tested slices 4a–4e.

---

## 1. Scope (what Phase 4 is and is not)

**In scope (deterministic, no API key):** keyword evidence index, wiki navigation index, graph
traversal API (read projection over `graph.py`), deterministic retrieval router, RRF hybrid
fusion, vector index (local-embedding default), and the endpoints `GET /search`,
`GET /graph/node/{id}`, `GET /graph/neighborhood/{id}`.

**Out of scope → Phase 5:** `POST /query` (LLM answer synthesis), saved `Queries/` pages,
LLM-based query classification, and emitting the `"No source found in vault."` answer text.

**Retrieval-eligibility invariant (ADR-0032 §2):** answer evidence may come from source chunks and
`active`/eligible graph-backed paths, but never from `candidate`/`deprecated_candidate` wiki-node
prose. `citable` = structured source evidence (chunks); `answer_eligible` = node-level routing/
synthesis eligibility. Navigation surfaces candidates; evidence does not cite them.

---

## 2. Index data model

### 2.1 Evidence index (citable chunks)
- **Source:** `normalized/chunks/<source_id>.jsonl`, one FTS5 row per chunk.
- **Store:** `indexes/keyword/keyword.sqlite` (FTS5; replaces the `db/metadata.sqlite`
  `documents_fts` scaffold).
- **Hit object:** `source_id`, advisory `chunk_id`, `ordinal`, `char_start`, `char_end`, `page`,
  `page_end`, `section`, `heading_path`, `table_reference`, `sheet_reference`, snippet, BM25 score.
- **Citation authority:** `(source_id, char_start, char_end)` + optional page/section/table
  (ADR-0019/0020); `chunk_id` advisory only.
- Never indexes wiki node prose. Whole-file normalized Markdown is not a citable corpus.

### 2.2 Navigation index (wiki page discovery)
- **Source:** `wiki/**/*.md` — frontmatter + title + summary callout + aliases (not full body in
  v1).
- **Store:** `indexes/keyword/keyword.sqlite` (separate table/schema from evidence).
- **Hit object:** `path`, `page_type`, `node_id`/`source_id`/`query_id`, `title`, `summary`,
  `status`, `review_status`, aliases/tags, `answer_eligible` (false unless `active` and
  node-type-allowed).

### 2.3 Vector index (4d)
- **Source:** the same per-source chunks as 2.1.
- **Store:** `indexes/vector/` (LanceDB). Local GPU embeddings by default; embedding-provider seam
  with cloud opt-in gated + security docs.
- **Row metadata:** citation fields (as 2.1) + the staleness key: `embedding_model_ref`, model
  version/hash, embedding code version, distance metric, `dimension`, chunk fingerprint, index
  version. A model/version bump invalidates the whole index.

### 2.4 Storage & lifecycle (ADR-0032 §7)
- `db/` durable: `graph.sqlite` (backed up), `jobs.sqlite`, `llm_cache.sqlite`.
- `indexes/` derived, gitignored, regenerable: `indexes/keyword/`, `indexes/vector/`.
- `indexes/graph/` vestigial — document as "reserved for a future derived graph cache, not graph
  authority."
- Backup: keyword no, vector optional (recompute savings), graph yes.
- Incremental fingerprinted rebuild; `--force` for full rebuild.

---

## 3. Endpoints

### 3.1 `GET /search`
- **Params:** `q` (required), `mode` (`keyword|vector|graph|navigation|auto`, default `auto`),
  `node_type=`, `page_type=`, `source_id=`, `language=` (`en|es|unknown`, filter only),
  `source_status=`, `node_status=`, `edge_status=`, `include_status=` (override, **defined per
  result group**), per-group limits `evidence_limit=`/`navigation_limit=`/`graph_limit=`.
- **Response:** `{query, mode, retrieval_path, evidence[], navigation[], graph[], truncated,
  counts}`. Empty = structural (`evidence: []`, `counts`, `no_results: true`).
- **Query safety:** deterministic FTS5 safe-query builder (tokenize + quote); raw `MATCH` never
  receives user text; bounded query length. Raw-FTS power mode deferred/opt-in.
- **Retention defaults:** return `active` + `deprecated_candidate`; exclude
  `archived`/`deleted`/hidden unless explicitly included; status always surfaced.

### 3.2 `GET /graph/node/{node_id}`
- Node metadata + adjacent **active** assertions grouped by `edge_type` (incoming + outgoing) with
  minimal adjacent-node metadata inline. `answer_eligible` flag per node.

### 3.3 `GET /graph/neighborhood/{node_id}`
- Flat payload `{root_id, depth, nodes[], edges[], truncated, cap}`. Default `depth=1`, hard max
  `2`. `edge_types=`/`node_types=` filters. Edge-status `active` by default;
  `include_status=proposed,active` for review tooling. Symmetric edges expose stored
  `src_id`/`dst_id` + `other_node_id` + `symmetric: true`. Edge anchors labelled advisory.

---

## 4. Router & fusion

- **Router (4c):** deterministic, reads `policies/retrieval.yaml` (routing taxonomy + budgets).
  `mode=auto` classifies via Build Spec §8.2 signals; explicit `mode` forces a path. Minimal
  escalation only ("primary `< k` → also run vector"). `max_graph_depth_default: 2` is the router's
  depth budget (endpoint default stays 1).
- **Fusion (4e):** RRF over keyword + vector evidence only → single ranked `evidence[]`; dedup by
  chunk row key; `retrieval_path` + per-channel ranks/scores on merged hits. Graph and navigation
  stay separate groups. Deterministic tie-break by `source_id`+`ordinal`. Weighted fusion and graph
  boosts deferred.
- **`retrieval.yaml` additions (caps):** `max_evidence_hits`, `max_navigation_hits`,
  `max_graph_nodes`, `max_graph_edges`, per-channel pre-fusion limits.

---

## 5. Sub-slices (each independently committable + validated)

| Slice | Deliverable | New deps |
|---|---|---|
| **4a** | Keyword evidence index + navigation index + reindex script + index-consistency validator; **retire scaffolds** (`documents_fts`, path-keyed `chunks.jsonl`). | none |
| **4b** | Graph read API (`/graph/node`, `/graph/neighborhood`): active-default, depth-bounded projection over `graph.py`. | none |
| **4c** | Retrieval router + `GET /search` (keyword + navigation + graph groups; safe FTS builder; retention filters). `mode=auto` covers keyword/navigation/graph only. | none |
| **4d** | Vector index: LanceDB + local embeddings + embedding-provider seam (cloud opt-in). Vector joins the **same** `/search` contract without changing response shape. | LanceDB, embedding model |
| **4e** | RRF hybrid fusion over keyword+vector + per-group caps + retrieval eval harness. | none |

Up through 4c the layer is fully offline/deterministic; 4d is the first slice introducing new
dependencies.

---

## 6. Evaluation & success criteria

### 6.1 Eval harness
- **File:** `evals/golden_retrieval.yaml` — kept **separate** from the answer-shaped
  `evals/golden_questions.yaml` (Phase 5).
- **Determinism:** results pinned by index version + `embedding_model_ref`; vector tie-break by
  `source_id`+`ordinal`.
- **Timing:** schema/categories/pass-criteria + a small seed set defined now (§6.2); the concrete
  query set is drafted during 4a/4c when the fixture corpus and exact index schema exist.

### 6.2 Eval schema (seed)
Each case: `{id, mode, query, filters?, expect: {...}, category}`. Pass-criteria are per category;
ordering assertions allow deterministic tie-breaks.

Categories:
1. **Exact-anchor retrieval** — keyword hit returns the correct `(source_id, char_start,
   char_end)`; `markdown[char_start:char_end] == chunk.text`.
2. **Status-aware navigation** — candidate pages appear with `answer_eligible: false`; active with
   `true`.
3. **Graph depth/caps** — neighborhood respects default `depth=1`, hard max `2`, and node/edge
   caps; `truncated` set correctly.
4. **Router taxonomy** — §8.2 query shapes classify to the expected mode-set.
5. **FTS-safe malformed queries** — quotes/operators/parens/`NEAR`/unbalanced input never crash;
   return structural empty or sane results.
6. **Vector metadata/citation carry-through** — a vector hit carries full citation metadata
   identical in shape to a keyword hit.
7. **RRF deterministic ordering** — identical query + index ⇒ identical ranked `evidence[]`;
   keyword∩vector hits merge with `retrieval_path: ["keyword","vector"]`.
8. **Retention-filtering defaults** — `deprecated_candidate` searchable by default;
   `archived`/`deleted`/hidden excluded unless explicitly included.

### 6.3 Success bar (Phase 4 done when)
- Anchored keyword evidence search works; navigation discovery is status-aware; graph endpoints
  return active-default bounded payloads; router classifies deterministically; RRF fusion is
  deterministic; vector returns relevant chunks carrying citation metadata.
- `validate_index_consistency.py` updated for the new index locations/schema and green.
- Graph/projection validators still green; retrieval evals green; tests green; lint clean.

---

## 7. Implementation coordination caveats (must change together)

The storage relayout (ADR-0032 §7) touches several files that currently disagree with the target
state; a slice that moves the index must update all of them in the same change:
- `scripts/reindex_keyword.py` — write FTS to `indexes/keyword/keyword.sqlite`, not
  `db/metadata.sqlite`; index chunks (+ navigation) not whole files.
- `scripts/reindex_vector.py` — replace the no-embedding scaffold; retire path-keyed
  `chunks.jsonl`.
- `.gitignore` — ensure `indexes/` is gitignored as derived data; reconcile `db/*.sqlite` handling.
- `scripts/backup.py` — currently includes all of `indexes/`; switch to: graph (backed up via
  `db/`), keyword excluded, vector optional.
- `scripts/validate_index_consistency.py` — assert index ↔ chunk/graph coherence for the new
  schema/locations.
- Docs: mark `indexes/graph/` as reserved-for-derived-cache; update Build Spec/Architecture
  retrieval sections if wording drifts.

---

## 8. Open items deferred (not Phase 4 v1)
- Weighted score-normalization fusion and graph-boost signals (revisit with eval evidence).
- Adaptive router escalation beyond the single `< k → vector` rule.
- Raw/power-user FTS query mode.
- Opaque per-group pagination cursors (v1 = capped first page).
- Cloud embedding providers (seam only; explicit opt-in + security docs when enabled).
