# Phase 4: deterministic hybrid retrieval (keyword + graph + vector) over citable chunk evidence

Phase 4 ("Search and Graph", Build Spec §8, §15) builds the retrieval layer that the wiki and
graph have been compiling toward. It is the last fully **deterministic, offline, API-key-free**
surface before Phase 5 introduces LLM answer synthesis — and, like the Phase 3/3.5 split
(ADR-0013, ADR-0028), all deterministic retrieval lands and is tested before the next LLM
surface. This ADR fixes the load-bearing decisions; the slicing, endpoint shapes, and eval
schema live in `docs/Phase 4 Plan.md`.

## The load-bearing decisions

**1. Phase 4 returns ranked, cited *evidence*; it does not generate answers.** The Build Spec
splits Phase 4 (Search and Graph) from Phase 5 (Query and Cited Answering). The seam: Phase 4
implements the keyword index, graph traversal API, a deterministic retrieval router, hybrid
fusion, and `GET /search` + `GET /graph/node/{id}` + `GET /graph/neighborhood/{id}`, all
returning **ranked, citation-anchored evidence and graph neighborhoods with no LLM in the loop**.
Deferred to Phase 5: `POST /query` (LLM synthesis over retrieved evidence), saved `Queries/`
pages, LLM-based query classification, and the `"No source found in vault."` *answer text*
(`retrieval.yaml` fallback). This keeps Phase 4 reproducible and key-free, so it can be regression-
tested deterministically.

**2. Two index targets, not one: citable chunk evidence vs status-aware wiki navigation.** The
index serves two distinct retrieval needs that do **not** share a relevance scale or an
eligibility rule, so they are separate schemas with separate ranking:

- **Evidence index — citable chunks only.** Source: the Phase 2 per-source chunks
  `normalized/chunks/<source_id>.jsonl`, one FTS5 row per chunk. A hit returns a structured
  evidence object (`source_id`, advisory `chunk_id`, `ordinal`, `char_start`, `char_end`, `page`,
  `page_end`, `section`, `heading_path`, `table_reference`, `sheet_reference`, snippet, BM25
  score). The **authoritative citation stays `(source_id, char_start, char_end)`** plus optional
  page/section/table fields (ADR-0019/0020); `chunk_id` remains advisory. This layer **never
  indexes wiki concept/entity/synthesis prose as evidence** — generated prose is not a source.
- **Navigation index — wiki page discovery.** Source: generated `wiki/**/*.md` (frontmatter +
  title + summary callout + aliases; not full body in v1). A hit returns a page object (`path`,
  `page_type`, `node_id`/`source_id`/`query_id`, `title`, `summary`, `status`, `review_status`,
  aliases/tags) with **`answer_eligible: false` unless the node is `active` and node-type-allowed**.
  This serves "what do I know about X?", not direct evidence.

Whole-file normalized Markdown is **not** indexed as a third citable corpus — it duplicates chunk
search while losing ready citation anchors. "Which documents mention X?" is answered by
aggregating evidence chunk hits by `source_id`. The Phase-0 scaffolds (`documents_fts` over whole
files in `db/metadata.sqlite`, and the path-keyed `normalized/chunks/chunks.jsonl` from the vector
scaffold) are **retired** — they predate the chunk/anchor model and cannot cite.

**The retrieval-eligibility invariant (corrected):** *answer evidence may come from source chunks
and `active`/eligible graph-backed evidence paths, but never from `candidate` (or
`deprecated_candidate`) wiki-node prose. Navigation may surface candidates; evidence may not cite
them.* Chunks are **source evidence, not semantic nodes** — so node `status` gates *node-prose
eligibility* (`answer_eligible`), not chunk evidence. The terms are kept distinct: **`citable`** is
reserved for structured source evidence (chunks); **`answer_eligible`** is the node-level flag for
routing/synthesis eligibility. An `active` concept/entity is `answer_eligible` for routing, but a
final answer still needs source/claim-backed citations.

**3. Vector search is in Phase 4, sequenced last, as an additive channel over the same chunk
corpus.** Build Spec §3.6 makes hybrid (incl. semantic) retrieval a v0.1 requirement, so vector is
not deferred out of the phase — but it is the **last sub-slice**, after the deterministic
keyword/graph/router stack is stable (ADR-0028 discipline). Commitments:

- Embed the **same per-source chunks** as the evidence layer (not a separate path-keyed or
  wiki-page corpus); a vector hit returns the identical structured-citation object. Each vector
  row stores citation metadata (`source_id`, advisory `chunk_id`, `char_start`, `char_end`, `page`,
  `section`, …).
- **Local embeddings by default, GPU-accelerated** (e.g. `bge-m3` on the RTX 5090). An
  **embedding-provider seam** is defined, but **cloud embedding providers are non-default,
  explicit opt-in only, with security documentation** — sending normalized chunks to a cloud
  embedding API exports source text and crosses the local-first trust boundary.
- **Store: LanceDB** (embedded, serverless, file-based — fits local-first; Build Spec allows
  "LanceDB or ChromaDB").
- **Vector is reproducible-enough only if the staleness key is complete:** embedding model
  identity, embedding code version, distance metric, dimension, and chunk fingerprint are all
  stored. A model/version bump **invalidates the whole vector index**. (This is *why* the vector
  index is not an ADR-0027-style durable record: embeddings derive from deterministic local inputs,
  unlike non-reproducible LLM sampling.) **Refined by ADR-0033:** "embedding model identity" is the
  operator-pinned `embedding_model_ref` — there is **no separate model-version/hash field**; the
  operator bumps the ref on any model/quantization/pooling/normalization/version change.

**4. The retrieval router is deterministic and policy-driven; `/search` exposes explicit `mode`
plus `auto`.** Phase 4 has no LLM, so classification is a small deterministic, testable function
reading `policies/retrieval.yaml` (routing taxonomy + budgets) rather than hardcoding. `/search`
takes `mode = keyword | vector | graph | navigation | auto` (default `auto`); explicit mode lets a
caller (and Phase 5) force a path. The `auto` classifier maps to the Build Spec §8.2 taxonomy
(quoted strings / numbers / filenames / acronyms → keyword; "how are X and Y related" → graph;
broad topic → navigation + graph; "which sources disagree" → claim/contradiction edges; default
conceptual → keyword + vector). **Escalation is minimal in v1** (run the classified mode-set;
optionally "if the primary path returns `< k`, also run vector"); adaptive escalation is deferred
as marginal-value nondeterminism. Recency / "what changed" stays out of the router (a `log.md` +
metadata concern). `max_graph_depth_default: 2` in `retrieval.yaml` is the **router's** depth
budget, distinct from the endpoint default (decision 5).

**5. Graph endpoints are a thin, active-by-default, depth-bounded read projection — no new graph
authority.** They project the existing `app/backend/graph.py` primitives, consistent with the
graph being the edge SoT and backlinks being derived (ADR-0029/0030):

- `GET /graph/node/{id}` → node metadata (id, type, title/slug, `status`) + adjacent **active**
  assertions grouped by `edge_type`, both incoming and outgoing, with minimal adjacent-node
  metadata inline (no N+1).
- `GET /graph/neighborhood/{id}` → a **flat graph-shaped payload** `{root_id, depth, nodes[],
  edges[], truncated, cap}` (not recursively nested). **Endpoint default `depth=1`, hard max
  `depth=2`**, with result-size caps and `edge_types=`/`node_types=` filters.
- **Filtering is by edge-status `active` by default, not node-status:** a `candidate` node can
  still appear via an `active` `mentions`/`derived_from` edge, but is flagged **not
  `answer_eligible`**. `proposed`/`rejected`/`superseded` edges are **hidden by default** and
  reachable only via an explicit review-oriented `include_status=` param.
- **Symmetric edges** (`contradicts`, `related_to`, `duplicates`) always keep their stored canonical
  `src_id`/`dst_id` (sorted, ADR-0031) and carry `symmetric: true`. **In the node-adjacency view
  (`/graph/node/{id}`)** they also expose a client-friendly `other_node_id` (the endpoint that is
  not the queried node). **In the flat `/graph/neighborhood` (and `/search` graph) edge lists,
  `other_node_id` is omitted** — there is no single reference node — see addendum 1.
- Edge **evidence anchors are labelled advisory**; for `contradicts` especially, the
  authoritative evidence remains the two Claim pages' structured citations, not the edge row.

**6. Hybrid fusion is RRF over the two chunk-evidence channels only; graph and navigation stay
separate groups.** Keyword BM25 and vector distance are incomparable scales, and graph/navigation
results are not even the same *unit* as chunk evidence. So:

- **Fuse keyword + vector (both chunk evidence) with Reciprocal Rank Fusion (RRF)** — rank-based,
  scale-free, no weight tuning, deterministic — into one ranked `evidence[]`. Dedup by the current
  chunk row key, keeping citation authority at `(source_id, char_start, char_end)`. Merged hits
  carry `retrieval_path: ["keyword","vector"]` plus per-channel ranks/scores for debugging.
- **Do not blend graph or navigation into the evidence ranking.** `/search` returns three labelled
  groups — `evidence[]` (RRF-ranked citable chunks), `navigation[]` (status-aware page hits, not
  evidence), `graph[]` (active-default, eligibility-aware node/edge hits) — so a wiki page, a graph
  node, and a source chunk are never pretended onto one relevance scale.
- **Weighted score-normalization and graph boosts are deferred** until eval queries justify the
  tuning. Determinism is required: stable vector index version/`model_ref`, fixed top-k, and a
  deterministic tie-break by `source_id` + `ordinal`, so identical query + index ⇒ identical
  output.

**7. Derived indexes live under `indexes/`; durable authority stays in `db/`.** The repo had drifted
(keyword scaffold wrote FTS into `db/metadata.sqlite`); Phase 4 corrects it:

- **`db/` = durable runtime state:** `graph.sqlite` (edge SoT — **backed up**, it holds reviewed
  relationship state and edge assertions), `jobs.sqlite`, `llm_cache.sqlite`, genuinely durable
  metadata.
- **`indexes/` = derived retrieval products:** `indexes/keyword/keyword.sqlite` (FTS5) and
  `indexes/vector/` (LanceDB) — **gitignored and regenerable** (ADR-0014), since the durable source
  of truth is always raw → normalized chunks + graph.
- **`indexes/graph/` is vestigial and must be explicitly documented** as "reserved for a future
  derived graph cache, NOT graph authority" — the authoritative graph stays `db/graph.sqlite`.
- **Backup posture:** keyword index **not backed up** (cheap full rebuild from chunks); vector
  index **backup optional** (recompute-savings only, not a durable record); graph **backed up**.
- **Rebuilds are incremental and fingerprinted** (ADR-0023/0027 discipline): reindex only changed
  sources (keyword = delete+reinsert rows for a changed `source_id`; vector = re-embed only changed
  chunks via the decision-3 staleness key). `--force` always allows a full rebuild.

**8. `/search` returns a grouped, structural response with disambiguated filters and a safe query
builder.** Shape: `{query, mode, retrieval_path, evidence[], navigation[], graph[], truncated,
counts}`. Rules:

- **Empty results are structural** (`evidence: []`, `counts`, `no_results: true`) — the
  `"No source found in vault."` text is a Phase 5 answer-layer concern, not a `/search` output.
- **FTS5 input goes through a deterministic safe-query builder** that tokenizes and quotes terms;
  raw user text is never passed to `MATCH` (it would throw on `"`, `*`, `:`, `NEAR`, parens, etc.).
  This is ordinary input-validation against the FTS5 grammar — **distinct from** the
  document-untrusted-input rule (which is about source *content* as instructions). A power-user raw
  FTS mode is deferred and explicit-opt-in only.
- **Filter names are non-overloaded:** `node_type=`, `page_type=`, `source_id=`, `language=`,
  `source_status=`, `node_status=`, `edge_status=`. `include_status=` override semantics are
  **defined per result group** (so a bare `active` is never ambiguous across source-page status vs
  graph node status vs edge status).
- **Retention-aware defaults** (§12.2 "deprecated content remains searchable unless hidden"):
  status is always surfaced on hits; default returns `active` + `deprecated_candidate`, and
  **excludes `archived`/`deleted`/hidden** unless explicitly included.
- **`language=en|es|unknown` is a filter only** — v1 does not translate queries or rank by
  language.
- **Pagination is per-group** (`evidence_limit=`, `navigation_limit=`, `graph_limit=`), never a
  single global offset across heterogeneous groups; opaque per-group cursors are a later option;
  v1 = a capped first page. Per-group caps (`max_evidence_hits`, `max_navigation_hits`,
  `max_graph_nodes`, `max_graph_edges`, per-channel pre-fusion limits) are added to
  `retrieval.yaml`.

**9. Phase 4 ships as five committable slices, each tested before the next, gated by deterministic
retrieval evals.** Slices `4a–4e` (keyword+navigation index/validator/scaffold-retirement → graph
read API → router + `/search` → vector index → RRF fusion + caps + evals); see the plan for the
breakdown. In 4c, `/search?mode=auto` covers keyword/navigation/graph only, and **vector joins the
same response contract in 4d without changing the shape**. Phase 4 evals are kept **separate** from
the answer-shaped `evals/golden_questions.yaml` — they live in `evals/golden_retrieval.yaml` and
are deterministic given a pinned index version + `embedding_model_ref`. The eval schema,
categories, and pass criteria (plus a small seed set) are defined in the plan now; the concrete
query set is drafted during 4a/4c when the fixture corpus and exact index schema exist, to avoid
writing fake golden cases too early.

## Consequences

Phase 4 is additive infrastructure over the proven Phase 3/3.5 stack: no new graph authority (the
graph endpoints are a read projection), one new derived keyword index, one new derived vector
index with a local-default embedding seam, a deterministic policy-driven router, and an RRF fusion
that respects unit boundaries. It honors the project invariants — retrieval is reproducible and
key-free; evidence is always source-anchored; candidate node prose is navigable but never citable;
the LLM stays out until Phase 5. The standing trades: vector recall depends on a single local
embedding model (cloud is opt-in only); minimal router escalation may under-route some queries
(accepted for v1, revisited with eval evidence); and the storage relayout requires coordinated
updates to `.gitignore`, `scripts/backup.py`, `reindex_keyword.py`, the validators, and the docs
in the same slice. The two-target index, the citable/answer_eligible split, RRF-over-chunks-only,
the grouped `/search` contract, the deterministic router, the thin active-default graph projection,
and the `indexes/` vs `db/` storage split are the load-bearing commitments; the per-channel
ranking constants, exact column layouts, and the embedding model id are tuned during
implementation.

## Addenda (Phase 4b implementation)

Recorded while implementing the graph read endpoints, after a code review against decision 5.
These refine — and do not overturn — the load-bearing commitments above:

1. **`other_node_id` is a node-adjacency field, not a flat-edge field.** Decision 5 says symmetric
   edges expose `src_id`/`dst_id` + `other_node_id` + `symmetric`. That holds for
   `GET /graph/node/{id}`, where every adjacent assertion has a well-defined "other" endpoint
   relative to the queried node. The **flat** `GET /graph/neighborhood/{id}` payload has no single
   reference node (a depth-2 edge can join two non-root nodes), so its edges are **canonical-only**:
   `src_id`/`dst_id` + `symmetric: true`, and **no `other_node_id`**. The canonical sorted direction
   is still never erased.
2. **Graph traversal filters by edge status only; retention/node-status filtering is `/search`'s
   job.** Consistent with "a `candidate` node can still appear via an `active` edge," `/graph/*`
   does **not** hide `archived`/`deleted`/`deprecated_candidate` nodes — they surface if an `active`
   edge reaches them, always carrying their real `status` and `answer_eligible: false`. The
   retention-aware defaults (decision 8) apply at the `/search` layer (Phase 4c), not the raw graph
   projection.
3. **Graph node metadata is `id`/`type`/`slug`/`status` (+ `answer_eligible`); `title` is deferred.**
   The graph store (`db/graph.sqlite`) holds no title — titles live in wiki frontmatter. Resolving
   them would couple the graph projection to the wiki/navigation layer, so title resolution is left
   to the navigation/search layer rather than the thin graph read.
4. **Graph endpoint caps stay code constants in 4b; the router owns the policy.** Depth (default 1,
   hard max 2) and node/edge caps are constants with bounded query overrides. Moving them into
   `policies/retrieval.yaml` happens in 4c, where the deterministic router (the component that reads
   that policy) lands — avoiding a documented-but-unenforced policy file before a loader exists.
