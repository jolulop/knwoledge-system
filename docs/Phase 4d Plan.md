# Phase 4d Plan — Vector retrieval (local-embedding semantic channel)

**Status:** Planned (design-locked 2026-06-18 via grill gate). No code yet.
**Governing ADR:** [ADR-0033](adr/0033-phase-4d-vector-retrieval.md). Read it first — this plan is
the operational breakdown of its decisions.
**Predecessors:** 4a (keyword + navigation index), 4b (graph read API), 4c (router + `GET /search`).
Phase 4d adds the vector channel; **4e** adds RRF fusion + the retrieval eval harness.

> [!summary]
> Phase 4d adds the semantic retrieval channel: an `EmbeddingClient` that calls an OpenAI-compatible
> **local `/embeddings` HTTP server** (no Torch/GPU in the repo), a **LanceDB** index over the same
> per-source chunks carrying the identical structured citation, a complete **config-ref staleness
> key** with `--force`-gated full rebuild, and an **explicit** `scripts/reindex_vector.py`. It wires
> `mode=vector` in `GET /search` to return vector evidence **standalone** (same `evidence[]` shape,
> `retrieval_path: ["vector"]`) — RRF fusion and the `auto` blend stay 4e. First slice with new
> dependencies (LanceDB); tests use a deterministic fake embedder, key-free.

---

## 1. Scope

**In scope:** the `EmbeddingClient` HTTP adapter seam (local default, cloud opt-in); the LanceDB
vector index over `normalized/chunks/<source_id>.jsonl`; the staleness key + incremental/`--force`
rebuild; `scripts/reindex_vector.py` (replacing the 4a-retired scaffold); the `mode=vector` channel
in `GET /search` (explicit-only, standalone); vector-staleness reporting in the validators; the
`vector` optional-dependency group; security docs for the cloud opt-in.

**Out of scope → Phase 4e:** RRF fusion of keyword + vector, `mode=auto` including vector, and the
`evals/golden_retrieval.yaml` harness. **Out of scope → Phase 5:** any LLM/answer synthesis.

**Invariant:** the deterministic 4a–4c stack must remain fully functional with **no embedder
configured** — vector is additive. The default install and the whole test suite stay key-free and
embedding-runtime-free (a deterministic fake embedder stands in).

---

## 2. Embedding seam (ADR-0033 decision 1)

- **`EmbeddingClient.embed(texts: list[str]) -> list[list[float]]`** — the only surface used by the
  indexer and the query path; mirrors the ADR-0025 `LLMClient.parse` seam. Implemented over **stdlib
  `urllib`** (timeout + bounded retry), so the seam (4d-1) adds **no** HTTP dependency to the core.
- **Local adapter** speaks the OpenAI `/embeddings` wire to `embedding_base_url`. Owns: bounded
  timeout + retry; **response-order preservation** (output vector *i* belongs to input text *i*);
  **dimension validation** (every response vector length == `embedding_dimension`, else hard error);
  **model cross-check** (response `model` vs `embedding_model_ref` → hard error unless
  `EMBEDDING_ALLOW_MODEL_MISMATCH=true`).
- **`local_http` URL guard:** `embedding_base_url` must be loopback or private/LAN (127.0.0.0/8, ::1,
  `localhost`, RFC-1918, link-local, `.local`); a **public** URL under `local_http` is rejected
  (mirrors `assert_safe_bind` in `main.py`). Off-network embedding requires the explicit cloud gate.
- **Cloud adapter** (opt-in): same wire, gated by **all three** of `embedding_provider=cloud_*`,
  `EMBEDDING_ALLOW_CLOUD=true`, and a non-empty **dedicated** `embedding_api_key` (never the implicit
  `OPENAI_API_KEY`). Any missing leg → refusal with a clear error. Exports source text → ships with a
  security note (ADR-0026, `policies/security.yaml`); never default.
- **Config keys** (env/`.env`, dependency-free layer): `embedding_provider` (`local_http` default),
  `embedding_base_url`, `embedding_model_ref`, `embedding_api_key` (cloud only), `EMBEDDING_ALLOW_CLOUD`,
  `EMBEDDING_ALLOW_MODEL_MISMATCH`, `embedding_dimension`, `embedding_distance_metric` (default
  cosine). Add to `.env.example` and `config.py` Settings.

---

## 3. Vector index (ADR-0033 decisions 2, 3)

### 3.1 Store & corpus
- **Store:** LanceDB under `indexes/vector/` (derived, gitignored, regenerable — ADR-0032 §7).
- **Corpus:** the same per-source chunks as the evidence index (`normalized/chunks/src_*.jsonl`);
  the retired path-keyed `chunks.jsonl` is never indexed.

### 3.2 Row schema (mirrors evidence citation + per-row staleness)
- **Vector:** the embedding (`embedding_dimension`-length float vector).
- **Citation (identical to a keyword evidence hit — the full `EvidenceHit` field set):** `source_id`,
  `chunk_id` (advisory), `ordinal`, **`kind`**, `char_start`, `char_end`, `page`, `page_end`,
  `section`, `heading_path`, `table_reference`, `sheet_reference`. Authoritative anchor stays
  `(source_id, char_start, char_end)`. (`kind` was missing from the earlier draft — a vector hit must
  carry every field a keyword hit does so the shape is truly identical.)
- **Per-row staleness:** `source_id`, `chunk_id`, `chunk_fingerprint`, `embedding_model_ref`.

### 3.4 Score & ordering semantics
- A vector hit's `score` is the **`embedding_distance_metric` value** (cosine distance by default —
  lower = closer), labelled consistently; it is **not** comparable to a keyword BM25 `score` (which is
  why 4e fuses by *rank* via RRF, not by raw score).
- **Deterministic ordering:** sort by `score` ascending (closest first), **tie-break by
  `(source_id, ordinal)`** — identical query + index ⇒ identical ranked `evidence[]`.

### 3.3 Index-level manifest (staleness key)
- A manifest beside the index (e.g. `indexes/vector/_meta.json`): `embedding_model_ref`,
  `embedding_code_version`, `distance_metric`, `dimension`, `index_schema_version`.
- **Rebuild rules:** any index-level field mismatch → **refuse incremental, require `--force`**; when
  only chunk fingerprints differ → **re-embed only changed chunks** (delete+reinsert that chunk's
  rows). `--force` always rebuilds from scratch.

---

## 4. `scripts/reindex_vector.py` (ADR-0033 decision 5)

- CLI `reindex_vector.py [ROOT] [--force]`, mirroring `reindex_keyword.py`; thin wrapper over an
  `app/backend/vector_index.py` core (so the validator + tests import it).
- Incremental by default (chunk-fingerprint diff vs the per-row staleness); `--force` full rebuild.
- **Never wired into the per-file change hook** (`.claude/hooks/reindex_changed_file.sh` keeps doing
  the cheap keyword reindex only). Run deliberately after ingest batches / before retrieval evals.
- Requires the embedding server; absent/unreachable → clear non-zero exit with a remediation message
  (does not corrupt or partially write the index).

---

## 5. `GET /search` vector channel (ADR-0033 decision 4)

- **`mode=vector` (explicit only):** embed the **raw NL query** (length-bounded; bypasses the 4c FTS
  tokenizer), ANN-search LanceDB by `distance_metric`, apply the same **retention** filter
  (`source_status` via the navigation join) and per-group `evidence_limit`/caps as keyword evidence,
  and return **standalone** `evidence[]` with `retrieval_path: ["vector"]`. Response shape unchanged.
- **`mode=auto` unchanged in 4d** — keyword-only evidence; vector joins `auto` via RRF in 4e.
- **Unavailable/stale handling:** explicit `mode=vector` returns **503 / clear unavailable** when the
  embedder is unconfigured or down, or the vector index is missing/stale (controlled, never a silent
  empty). `mode=auto` is unaffected.
- Reuses the 4c retention/cap machinery; the vector row's citation fields make the hit identical in
  shape to a keyword hit.

---

## 6. Validators, backup, dependencies

- **Validators (surface, don't fix):** extend the index-consistency check to **report** vector
  staleness — chunks whose `chunk_fingerprint` changed since embed, source/chunk drift, and
  index-level `embedding_model_ref`/`dimension` mismatch vs config. Missing vector index is **not** a
  failure (optional, regenerable); a *stale* or *incoherent* one is reported for a deliberate reindex.
- **Backup:** already wired — vector index is opt-in via `BACKUP_INCLUDE_VECTOR_INDEX` (ADR-0032 §7),
  not backed up by default.
- **Dependencies & test posture (pinned):** new `vector` optional-dependency group in
  `pyproject.toml` = `["lancedb>=…"]`. LanceDB is **not** in core or in `dev`, so a bare `.[dev]`
  install stays light. **LanceDB-backed tests are skipped unless the extra is installed** (a module-
  level `pytest.importorskip("lancedb")`); the **canonical full-suite / CI command installs
  `.[dev,vector]`** so they run for the maintainer and in CI. The embedding-seam tests (fake embedder,
  stdlib `urllib`) run **always** — no LanceDB needed. The embedding adapter adds no HTTP dependency
  (stdlib `urllib`).
- **`indexes/vector/` gitignore** is already in place (4a); confirm it covers the LanceDB layout.

---

## 7. Test strategy (key-free, runtime-free)

- A **deterministic fake embedder** (e.g. a stable hash → fixed-dimension vector) is the default in
  tests — never a real model — so vector indexing, staleness, `--force`, and the `/search` vector
  channel are all tested offline and deterministically.
- **Adapter (fake embedder, always runs):** response **order preserved** (vector *i* ↔ input *i*);
  **dimension mismatch** hard-errors; **model mismatch** hard-errors (and is allowed under
  `EMBEDDING_ALLOW_MODEL_MISMATCH`); timeout/retry is **bounded** (no unbounded loop).
- **Security gate:** `local_http` **rejects** a public/non-local `embedding_base_url`; the cloud path
  **never runs** unless all three legs (`cloud_*` provider + `EMBEDDING_ALLOW_CLOUD=true` +
  `embedding_api_key`) are present; `OPENAI_API_KEY` alone never enables cloud embedding.
- **Vector index (LanceDB, `importorskip`):** index-level mismatch **refuses incremental**; `--force`
  rebuilds; changed chunk fingerprints **re-embed only the changed chunks**; validator **surfaces**
  vector staleness/missing without failing on a merely-absent index.
- **`/search?mode=vector`:** returns the **full `EvidenceHit` citation shape** (incl. `kind`) with
  `retrieval_path:["vector"]`, deterministic ordering (tie-break `source_id`+`ordinal`),
  retention-filtered; **stale/missing index or embedder down → 503**; **`mode=auto` remains unchanged**
  (no vector) in 4d.
- **Operational refs:** the reintroduced `scripts/reindex_vector.py` **exists** (the 4a
  `test_operational_refs` guard passes), **and** the per-file hook still **does not** call it (an
  explicit assertion that `reindex_changed_file.sh` references no vector reindex).
- The **real local server** is exercised only behind an **opt-in integration smoke test** (manual /
  env-gated), never in the default suite.

---

## 8. Sub-slices (each independently committable + validated)

| Slice | Deliverable |
|---|---|
| **4d-1** | `EmbeddingClient` HTTP adapter + config keys + fake embedder for tests (no index yet). |
| **4d-2** | LanceDB `vector_index.py` core + manifest/staleness key + `scripts/reindex_vector.py` (incremental + `--force`); validator vector-staleness reporting. |
| **4d-3** | `GET /search` `mode=vector` channel (standalone, retention/caps, 503-on-unavailable); `.env.example`/docs; `vector` dependency group; cloud opt-in security note. |

Each lands behind the fake embedder; the real server is smoke-tested separately.

---

## 9. Success criteria (Phase 4d done when)
- `reindex_vector.py` builds a LanceDB index over the chunks with full citation + staleness metadata;
  incremental re-embeds only changed chunks; an index-level change is refused without `--force`.
- Runtime dimension validation + model cross-check enforced.
- `GET /search?mode=vector` returns standalone vector evidence in the unchanged `evidence[]` shape
  (`retrieval_path:["vector"]`), retention-filtered; unavailable/stale → controlled 503.
- `mode=auto` is unchanged (no vector) — confirmed by test.
- Validators surface vector staleness; a missing vector index is not a failure.
- The deterministic 4a–4c stack still passes with no embedder configured; tests green (fake embedder),
  ruff clean, validators green.

---

## 10. Deferred (not Phase 4d)
- RRF fusion of keyword + vector and `mode=auto` including vector → **Phase 4e**.
- `evals/golden_retrieval.yaml` retrieval eval harness → **Phase 4e**.
- Probe-fingerprint auto-detection of silent same-dimension model swaps (rejected for v1; operator
  bumps `embedding_model_ref`).
- Wiring vector re-embed into the per-file change hook (explicit reindex is the contract).
- Cloud embedding as anything other than an explicit, security-gated opt-in.
