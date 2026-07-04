# ADR-0053 — In-process FlagEmbedding backend (BGE-M3 on CUDA), superseding the HTTP-only embedding seam for GPU

**Status:** Accepted. **Design-locked 2026-07-04** (this session, docs-only); **implemented in this slice.**
Both design forks were confirmed by the user 2026-07-04: (6) startup = *warmup-in-lifespan when
`flagembedding_bge_m3` is the selected provider* + a CLI smoke/health command (**not** always-GPU-required —
non-vector app roles stay GPU-independent); (paper trail) = a **new ADR-0053 within this implementation slice**
(explicitly not a grill-first round and not a mere ADR-0033 addendum, because moving BGE-M3 in-process changes
the trust boundary, dependency surface, startup behavior, memory ownership, failure modes, and deployment story).

**Supersedes:** **ADR-0033 decision 1** ("Embeddings run behind an OpenAI-compatible local HTTP `/embeddings`
adapter; the repo owns no GPU runtime and pulls no Torch into the core environment", which *explicitly rejected*
in-process `sentence-transformers`/Torch/CUDA). This ADR reverses that decision **for the GPU path only**: the
default GPU backend becomes an **in-process** PyTorch + FlagEmbedding (`BGEM3FlagModel`) provider. **ADR-0033
decisions 2–5 stand unchanged** (LanceDB same-citation index; `embedding_model_ref` staleness identity + the
`--force`-gated full rebuild; explicit-only `mode=vector`; explicit non-hooked reindex). The HTTP `local_http`
seam is **retained as an optional CPU fallback**, not the default.

**Extends/claims:** ADR-0033 (the vector-retrieval architecture this modifies; decisions 2–5 preserved),
ADR-0025 (the tier→`model_ref` = `provider:model_id` indirection — the embedding provider seam mirrors the LLM
adapter seam), ADR-0009 (loopback-only no-auth bind — the in-process model needs no new network surface),
ADR-0032 §7 (derived `indexes/vector/` is gitignored/regenerable; opt-in backup).

## Context — why reverse ADR-0033 decision 1

ADR-0033 chose the HTTP seam for one load-bearing reason: **keep Torch/CUDA out of the core environment and the
default test suite** (key-free, dependency-light CI). The operator would run TEI/vLLM/Ollama on the GPU and the
repo would speak the OpenAI `/embeddings` wire to it. That assumption has now broken on the target hardware:

1. **TEI/Candle falls back to CPU on the RTX 5090.** The Docker/WSL/NVIDIA stack is working and TEI images see
   the GPU through `nvidia-smi`, but the TEI Candle backend does not execute BGE-M3 on this GPU — it silently
   runs on CPU, defeating the entire point of the GPU box (throughput for reindex + query-time embedding).
2. **In-process PyTorch + FlagEmbedding runs BGE-M3 on CUDA here, today.** The project venv already has
   `torch 2.11.0+cu128` (CUDA 12.8), `FlagEmbedding 1.4.0`, `sentence-transformers`, `transformers`,
   `accelerate`, `lancedb`, `pyarrow`, `numpy`; `torch.cuda.is_available()` is `True` on the
   "NVIDIA GeForce RTX 5090 Laptop GPU"; `torchvision`/`torchaudio` are **absent** (they previously caused a
   `torchvision::nms` import error and are not needed). The environment provisioning (ADR-req install commands)
   is effectively already done.

So the operational reality forces an in-process backend for GPU. The design question is **how to reverse
ADR-0033 decision 1 without discarding its intent** (a light, key-free, Torch-free default install + CI). The
answer (decision 1 below): Torch/FlagEmbedding are a **local accelerator overlay installed out-of-lock** (not a
locked dependency group — decision 8), are **lazily imported** inside the provider (exactly as LanceDB is
isolated to `vector_index.py`), and are reached **only** when the operator opts into
`EMBEDDING_PROVIDER=flagembedding_bge_m3`. The default (`local_http`, unconfigured, or the fake-embedder test
path) never imports Torch.

## Decisions

### 1. In-process FlagEmbedding is the default GPU backend; `local_http`/TEI is retained as CPU fallback only

- New provider **`BgeM3FlagEmbeddingProvider`** in `app/backend/embeddings.py`, constructed from
  `FlagEmbedding.BGEM3FlagModel("BAAI/bge-m3", use_fp16=True, device="cuda")` (config-driven). It runs the model
  **in-process** — no HTTP, no operator-run server, no TEI.
- **The `local_http` HTTP `EmbeddingClient` stays** — it is a valid **CPU fallback** and the retrieval-relevance
  eval's historical baseline path. It is **no longer the default GPU backend**. There is **no TEI service in
  `docker-compose.yml`** to delete (compose only has `app` + optional `qdrant`); "disable TEI for GPU" is
  realized as the default-posture flip (below) + docs reframing TEI/Candle as CPU-fallback-only.
- **ADR-0033's light-CI invariant is preserved by construction:** Torch + FlagEmbedding are **installed
  out-of-lock** (not in core `dependencies` and not a locked extra — decision 8), are **imported lazily inside
  provider methods** (module import stays Torch-free), and are reached only under the opt-in provider. The
  default test suite keeps using the
  deterministic `FakeEmbedder` and never imports Torch (decision 7).

### 2. Provider abstraction — extend the existing `Embedder` seam; add the richer provider API

The tiny `Embedder` Protocol (`dimension: int`, `embed(texts) -> list[list[float]]`) already isolates the
indexer + query path from the backend. This ADR extends it minimally and adds the requested provider API:

- **Protocol gains `model_ref: str`** (the staleness identity, decision 4) so every call site reads it from the
  provider/config uniformly instead of hard-wiring `settings.embedding_model_ref`.
- **`BgeM3FlagEmbeddingProvider` implements both surfaces:** the protocol `embed(texts)` **and** the requested
  API — `embed_texts(texts) -> list[list[float]]` (alias of `embed`), `embed_query(text) -> list[float]`
  (single-text convenience), `health() -> dict` (provider/model/device/dtype/dimension + `torch.__version__`,
  `torch.version.cuda`, device name), and `validate_startup()` (decision 6).
- **Factory branches on `EMBEDDING_PROVIDER`.** `client_from_settings(settings)` (kept as the single entry the
  4 call sites use) returns: `local_http`/`cloud_*` → the existing `EmbeddingClient`; `flagembedding_bge_m3` →
  `BgeM3FlagEmbeddingProvider`; unconfigured → `None` (vector cleanly off). A tiny **Torch-free**
  `resolve_model_ref(settings) -> str | None` and `provider_configured(settings) -> bool` back the factory and
  the offline validator (decision 4/7), so nothing that only needs the *identity* must load the model.
- The `EmbeddingClient` (HTTP) gains a `model_ref` property (returns `config.model_ref`) so both impls satisfy
  the extended protocol.

### 3. Dense-only v1; sparse + ColBERT kept optional for later hybrid/rerank

The initial production call is **dense-only**:

```python
out = model.encode(texts, batch_size=<EMBEDDING_BATCH_SIZE>, max_length=<EMBEDDING_MAX_LENGTH>,
                   return_dense=True, return_sparse=False, return_colbert_vecs=False)
```

Only **`out["dense_vecs"]`** is persisted/used; **dense dimension is 1024**. The provider returns
`list[list[float]]` (each length 1024), converting from the returned array (`.tolist()`), asserting no `None`
dense result and the 1024 width per row (hard error otherwise — mirrors the HTTP path's dimension validation).
**Sparse lexical weights (`return_sparse`) and ColBERT vectors (`return_colbert_vecs`) are deferred** — they are
the substrate for a future hybrid-retrieval / reranking slice (BGE-M3 is natively multi-vector), out of scope
here. This keeps the LanceDB row schema unchanged (single `vector` column, dim 1024) so `INDEX_SCHEMA_VERSION`
does **not** bump.

### 4. Staleness identity — `flagembedding_bge_m3:<model_id>:<precision>`, distinct from the TEI ref

ADR-0033 decision 3 makes `embedding_model_ref` the index-level identity that gates re-embed. For the in-process
backend the ref is derived, **Torch-free**, by `resolve_model_ref(settings)` (and identically by the provider's
`model_ref`, via the shared `_flagembedding_ref(model_id, use_fp16)` — one formula, so they never drift):

- `flagembedding_bge_m3` → **`flagembedding_bge_m3:<EMBEDDING_MODEL_ID>:<fp16|fp32>`** (e.g.
  `flagembedding_bge_m3:BAAI/bge-m3:fp16`).
- `local_http`/`cloud_*` → `settings.embedding_model_ref` (unchanged).

Because this differs from the historical TEI ref (`bge-m3`), **switching from TEI to the in-process backend is
an index-level mismatch that forces `reindex_vector.py --force`** — correct, since fp16/pooling/normalization can
differ between backends even for the same nominal model.

**What is (and isn't) part of the identity (updated after the round-1 review):**
- **Precision (`use_fp16`) IS folded in.** fp16-vs-fp32 is a *systematic* difference (larger than the GPU float
  nondeterminism ADR-0033 tolerates), so `fp16`/`fp32` is part of the ref — flipping precision forces a rebuild
  rather than silently mixing precisions in one index.
- **Device and `batch_size` are excluded.** Device (cuda/cpu) is *execution placement*, not intended embedding
  semantics — and the precision component already captures the meaningful cuda-fp16-vs-cpu-fp32 gap; `batch_size`
  has no effect on output.
- **`max_length` is excluded** because the ingestion chunking contract keeps chunks far below the 8192-token
  cap (`EXTRACT_CHUNK_MAX_CHARS` default 2000 chars ≈ ~500 tokens), so truncation never fires and the cap never
  changes a vector. *If that chunk-size contract ever loosens toward 8192 tokens, revisit — either fold
  `max_length` in or assert the chunk bound.*
- **The model id FLOATS (no HF-revision pin in v1).** Following ADR-0033's "operator discipline over the ref"
  contract, `EMBEDDING_MODEL_ID` is not pinned to an immutable HF revision. **Accepted tradeoff:** if the local
  HF cache / upstream `BAAI/bge-m3` revision changes, the operator is responsible for a `reindex_vector.py
  --force`. Revision pinning (an `EMBEDDING_MODEL_REVISION` folded into the ref) is a clean, deferred follow-up —
  added only once the project proves it needs immutable model snapshots.

Per ADR-0033 decision 3, **bit-identical vectors are not required** (cosine ranking tolerates GPU float
nondeterminism); the ref governs *when to re-embed*. `EMBED_CODE_VERSION` and `INDEX_SCHEMA_VERSION` are
unchanged (row mapping + dim 1024 unchanged).

### 5. Configuration surface

New settings (read through the existing dependency-free env/`.env` layer, `EMBEDDING_*`):

| env var | setting | default (code) | notes |
|---|---|---|---|
| `EMBEDDING_PROVIDER` | `embedding_provider` | `local_http` | operator `.env`/docs default = `flagembedding_bge_m3` (see below) |
| `EMBEDDING_MODEL_ID` | `embedding_model_id` | `BAAI/bge-m3` | the FlagEmbedding model id |
| `EMBEDDING_DEVICE` | `embedding_device` | `cuda` | `cuda`\|`cpu`; `cuda` + unavailable → fail-fast (decision 6) |
| `EMBEDDING_USE_FP16` | `embedding_use_fp16` | `true` | fp16 on CUDA |
| `EMBEDDING_BATCH_SIZE` | `embedding_batch_size` | `16` | lower on memory pressure |
| `EMBEDDING_MAX_LENGTH` | `embedding_max_length` | `8192` | BGE-M3 max context |
| `EMBEDDING_DIMENSION` | `embedding_dimension` | `1024` | validated per row (existing key) |
| `EMBEDDING_CACHE_DIR` | `embedding_cache_dir` | *unset* → HF default (`~/.cache/huggingface`) | optional repo-local model cache; unset avoids a repo-local-dir-writability footgun (the repo `.cache/` is root-owned here from a docker run) |

`EMBEDDING_MODEL_REF` is **kept** (used by `local_http`/`cloud_*`; unused by `flagembedding_bge_m3`, which uses
`EMBEDDING_MODEL_ID`). **The code default of `EMBEDDING_PROVIDER` stays `local_http`** so that (a) an unconfigured
install keeps vector cleanly off, (b) the light install + CI never build the in-process provider, and (c) the
existing `test_client_from_settings_none_when_unconfigured` stays green. **The operator `.env` and
`.env.example`'s recommended value becomes `flagembedding_bge_m3`**, and the docs name it the default GPU backend.
This is how "in-process is the default, TEI must not be the default" is honored without forcing Torch onto the
light path.

### 6. Startup: load-once singleton + fail-fast, scoped to the selected backend (confirmed)

Requirement (req #7): load BGE-M3 once at service startup, log `torch.__version__` / `torch.version.cuda` /
`torch.cuda.get_device_name(0)`, and fail fast if CUDA is requested but unavailable. **Req #7 applies to the
selected embedding backend, not unconditionally to every process role** — the whole app must **not** become
GPU-required, or ingest/review/lint become unusable on machines without CUDA (violating ADR-0033's optional-
vector posture). The confirmed startup matrix:

| `EMBEDDING_PROVIDER` | `EMBEDDING_DEVICE` | app startup behavior |
|---|---|---|
| unset / `local_http` / `cloud_*` | — | app boots; vector stays unavailable/degraded exactly as today; **no Torch import** |
| `flagembedding_bge_m3` | `cuda` | validate `torch.cuda.is_available()`, log device metadata, **load BGE-M3 once**, **fail fast** if CUDA or the model load fails |
| `flagembedding_bge_m3` | `cpu` | app boots and loads the **CPU** provider (mainly tests/dev fallback); no CUDA assertion |

Resolution:

- **Process-level singleton model cache** keyed by `(model_id, device, use_fp16, cache_dir)`, so the ~2 GB model
  is loaded **once per process** and every `embed`/`embed_query`/per-request embedder construction reuses it
  (critical — reloading per query would be catastrophic).
- **`validate_startup()`** on the provider: import Torch, log the three version/device lines, and — when
  `device == "cuda"` — **assert `torch.cuda.is_available()`**, raising a typed error (fail-fast) otherwise; then
  load the singleton (warmup).
- **Eager warmup in the FastAPI app lifespan runs ONLY when `EMBEDDING_PROVIDER == flagembedding_bge_m3`** — so
  the light/`local_http`/CI path never imports Torch or loads a model at boot, while the GPU deployment gets the
  literal "load once at startup + fail fast" behavior. (Existing `test_api.py` app-boot tests use the default
  `local_http`, so they are unaffected.)
- **`scripts/check_embedding.py`** — a standalone verification CLI (the req #10 verification, made repeatable):
  prints the torch/CUDA probe and runs the BGE-M3 dense smoke (`["hello world", "hola mundo", "semantic search
  over enterprise documents"]` → shape `(3, 1024)`), with a `--json` mode. This is the operator's fail-fast entry
  independent of the running service.

*Alternatives considered:* **lazy-only** (load on first embed; the CLI is the only fail-fast entry) — simplest,
fastest boot, but doesn't satisfy "load at startup" for the running service; **always-eager** (warm up whenever
any embedder is configured, regardless of provider) — makes app boot depend on the GPU/model even for non-vector
work, the largest behavioral change. The recommended middle path satisfies req #7 literally on the GPU box while
leaving the light path untouched.

### 7. Testing / CI — the fake-embedder gate stays Torch-free; real-model tests are marked and skipped by default

- **The CI gate is unchanged:** default `pytest` uses the deterministic `FakeEmbedder` (gains a `model_ref`
  attribute for protocol conformance) and **never imports Torch**. `resolve_model_ref` / `provider_configured` /
  factory-selection / `health()`-shape are tested **without loading a model**, including the `device=cuda`
  fail-fast path (simulated by monkeypatching `torch.cuda.is_available`, guarded so it skips when Torch is
  absent).
- **`@pytest.mark.model`** — a CPU integration test (`EMBEDDING_DEVICE=cpu`) that loads the real BGE-M3 and
  asserts 3 vectors × 1024, no `None`. **Skipped unless** FlagEmbedding/Torch are importable (req #9: a non-GPU
  test path via `EMBEDDING_DEVICE=cpu`).
- **`@pytest.mark.gpu`** — the CUDA smoke test (req #8): the three inputs → 3 vectors, each length 1024, no
  `None` dense result, **device is CUDA**. **Skipped unless** `torch.cuda.is_available()`.
- Markers are **registered** in a new `[tool.pytest.ini_options] markers = [...]` block in `pyproject.toml`
  (the project currently registers none), so the marked tests emit no "unknown marker" warnings and the GPU/model
  tests stay out of the default key-free run.

### 8. Dependencies + docs

- **The GPU stack is a local accelerator overlay, NOT a locked optional-dependency group** (decided in the
  round-1 review). `uv.lock` is tracked, and `torch` (a transitive dep of FlagEmbedding) can't be locked to the
  **CUDA 12.8** wheel build from PyPI without pinning the whole lock to a cu128 index/source — coupling the
  portable lock to one GPU platform and risking CPU-only dev paths. So **no `embed` extra is added**; core
  `dependencies` and `uv.lock` are **unchanged** (no drift). Instead the install path is the source of truth,
  recorded verbatim in `docs/Environment Setup §14.1`: `uv pip install torch --index-url
  https://download.pytorch.org/whl/cu128`, then `uv pip install -U FlagEmbedding sentence-transformers
  transformers accelerate` (do **not** add `torchvision`/`torchaudio` — `torchvision::nms` import error; not
  needed). Startup validation (decision 6) is what makes a misbuilt env fail loudly. The only `pyproject.toml`
  change is the `[tool.pytest.ini_options]` markers (`gpu`/`model`) — not deps, so no lock impact.
- **Docs updated:** `docs/Environment Setup v0.1.md` (the cu128 torch + FlagEmbedding install, the torch/CUDA
  verification block, the BGE-M3 smoke, and the TEI/Candle CPU-fallback caveat), `docs/Operations.md` (the
  `flagembedding_bge_m3` provider + `reindex_vector.py` under it + `check_embedding.py`), `docs/README.md`,
  `docs/Phase 4d Plan.md` (backend note), `.env`/`.env.example` (the new keys + default flip), a `CONTEXT.md`
  glossary entry, and a pointer added to ADR-0033.

## Consequences

- The GPU is actually used: BGE-M3 dense embeddings run on the RTX 5090 via CUDA, replacing the TEI CPU fallback
  for both reindex throughput and query-time embedding.
- **ADR-0033's light-default posture survives the reversal:** Torch stays out of core deps + the default test
  suite (out-of-lock overlay + lazy import + opt-in provider). A machine without Torch, or one on `local_http`/
  unconfigured, is unaffected.
- Switching backends is a deliberate, safe re-embed: the distinct `model_ref` forces `--force`, and validators
  surface the index-level mismatch rather than silently serving mixed-backend vectors.
- **Standing trades:** the app boot on the GPU deployment now depends on Torch + a resident ~2 GB model (only
  under the opt-in provider); vector recall still reflects the last explicit reindex (ADR-0033 decision 5,
  unchanged); dense-only means no lexical/multi-vector signal until the deferred hybrid slice.
- **Follow-ups (deferred):** sparse (`return_sparse`) + ColBERT (`return_colbert_vecs`) for hybrid retrieval /
  reranking (new LanceDB columns → an `INDEX_SCHEMA_VERSION` bump); folding `use_fp16` into the ref if precision
  drift proves error-prone; a CPU-quant path for machines without CUDA.

## Resolved forks (confirmed 2026-07-04)

1. **Startup-wiring:** *warmup-in-lifespan when `flagembedding_bge_m3` is selected* + `scripts/check_embedding.py`
   CLI, with the device matrix above. Rejected: *always-eager on boot* (would make the whole app GPU-required).
2. **Paper trail:** a **new ADR-0053 within this implementation slice**. Rejected: *grill-first* (the request
   already carries enough direction) and *ADR-0033 addendum* (too small for a trust-boundary/dependency/startup
   change).

## Tests (design intent; written at implementation)

- Torch-free: `resolve_model_ref` returns `flagembedding_bge_m3:BAAI/bge-m3:fp16` (and `:fp32` when
  `use_fp16` is off — precision is folded in) for the new provider and
  `settings.embedding_model_ref` for `local_http`; `provider_configured` true for `flagembedding_bge_m3` even
  with no base_url; factory returns the right impl per `EMBEDDING_PROVIDER`; unconfigured `local_http` → `None`
  (unchanged); `health()` shape without loading a model; `device=cuda` + monkeypatched
  `cuda.is_available()==False` → typed fail-fast (skipped if Torch absent).
- `@pytest.mark.model` (CPU): real BGE-M3 on `EMBEDDING_DEVICE=cpu` → 3×1024, no `None`.
- `@pytest.mark.gpu` (CUDA): the three inputs → 3 vectors × 1024, no `None`, device CUDA; singleton is loaded
  once (second construction reuses the cached model).
- `FakeEmbedder` carries `model_ref` so the extended protocol is satisfied; the existing 4d vector-index tests
  are unaffected.
- `scripts/check_embedding.py` exits 0 and prints `dense_vecs shape: (3, 1024)` on this box; `--json` emits the
  health block.
