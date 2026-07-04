# Phase 4d: vector retrieval over the same chunk evidence (local-embedding, key-free default)

Phase 4d adds the **semantic / vector** retrieval channel that ADR-0032 decision 3 committed to —
the last channel of the Build Spec §3.6 hybrid-retrieval requirement. It embeds the **same
per-source chunks** as the keyword evidence index (ADR-0032 decision 2), stores them in a local
vector store, and serves them through the *existing* `GET /search` contract (ADR-0032 decision 8).
It is the **first Phase 4 slice to introduce new dependencies** (a vector store; an embedding
runtime). Crucially, it preserves the phase invariant: the deterministic keyword/graph/router stack
(4a–4c) **never depends on** the vector channel — vector is additive, optional infrastructure.

RRF fusion of keyword + vector and the `mode=auto` blend stay **Phase 4e**; this ADR fixes the
load-bearing decisions for the vector channel itself. Slicing and column layouts live in
`docs/Phase 4d Plan.md`.

## The load-bearing decisions

> **Superseded for the GPU path by [ADR-0053](0053-in-process-flagembedding-backend.md) (2026-07-04).**
> Decision 1 below (HTTP-only, no in-process Torch) was reversed because **TEI/Candle falls back to CPU
> on the RTX 5090**: the default GPU backend is now **in-process FlagEmbedding + PyTorch CUDA** (BAAI/bge-m3),
> selected via `EMBEDDING_PROVIDER=flagembedding_bge_m3`. ADR-0053 preserves this ADR's light-CI intent —
> Torch lives in an optional extra, is imported lazily, and is reached only under the opt-in provider — so
> the default/`local_http`/fake-embedder path stays Torch-free. **This ADR's decisions 2–5 stand unchanged**
> (LanceDB same-citation index; `embedding_model_ref` staleness + `--force` rebuild; explicit-only
> `mode=vector`; explicit non-hooked reindex). The `local_http` HTTP seam below is retained as an optional
> CPU fallback.

**1. Embeddings run behind an OpenAI-compatible local HTTP `/embeddings` adapter; the repo owns no
GPU runtime and pulls no Torch into the core environment.** This mirrors the ADR-0025 LLM seam (a
thin provider-agnostic adapter, lazy/standard HTTP, dependency-light by hand-rolling rather than
adopting a heavy unifying dependency) rather than embedding in-process via `sentence-transformers` /
Torch / CUDA. A single `EmbeddingClient` call — `embed(texts) -> list[vector]` — is the only surface
the indexer and the query path use; the concrete adapter speaks the OpenAI `/embeddings` wire to a
local server the operator runs (TEI / vLLM / Ollama / LM Studio on the RTX 5090). The adapter owns
**timeout + bounded retry** and **dimension validation**; the model lifecycle and GPU are the
server's responsibility. Configuration (resolved through the existing dependency-free env/`.env`
layer):

- `embedding_provider` — `local_http` (default) or a `cloud_*` value (see the gate below).
- `embedding_base_url` — the server's base URL.
- `embedding_model_ref` — the pinned model identity (see decision 3).
- `embedding_api_key` — a **dedicated** key, **only** for the cloud opt-in; never the implicit
  `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` (those serve enrichment, ADR-0025 — embeddings have their own
  key so a cloud embedding pass can never piggy-back on an LLM key by accident).
- `EMBEDDING_ALLOW_CLOUD` — the explicit cloud acknowledgment flag (see the gate).
- `embedding_dimension` — the expected vector dimension (validated at runtime).
- `embedding_distance_metric` — the metric the index is built for (default cosine, normalized
  vectors).
- `EMBEDDING_ALLOW_MODEL_MISMATCH` — opt-out of the model cross-check (default false; see decision 3).

**The cloud gate is concrete and conjunctive.** Sending normalized chunks to a cloud `/embeddings`
API exports source text across the local-first trust boundary (ADR-0026, `policies/security.yaml`).
The cloud path runs **only** when **all three** hold: `embedding_provider` is a `cloud_*` value
**and** `EMBEDDING_ALLOW_CLOUD=true` **and** a non-empty `embedding_api_key` is set. Any missing leg
is a refusal at startup/first embed with a clear error — there is no implicit cloud path. This
mirrors the existing `assert_safe_bind` / `KS_ALLOW_INSECURE_BIND` acknowledgment pattern.

**`local_http` is loopback/LAN-only, and the HTTP path refuses redirects + non-http(s) schemes.**
Under `local_http`, `embedding_base_url` must be a loopback or private/LAN host (127.0.0.0/8, ::1,
`localhost`, RFC-1918, link-local, single-label, `.local|.lan|.internal|.home`); a **public** URL
under `local_http` is **rejected**, and the embedding HTTP opener **refuses all 3xx redirects** (so a
gated host cannot bounce the source-text payload off-host) and only `http`/`https` schemes are
allowed (cloud requires `https`). The host check is **lexical operator-trust, not DNS resolution** —
a hostname that resolves to a public IP is not caught; operators pin a trusted local URL. Exporting
source text off the local network is exactly what the cloud gate (with its explicit acknowledgment +
security docs) exists to govern. The default install and the test suite stay key-free and
embedding-runtime-free.

**2. The vector store is LanceDB; vector hits are the *same* structured-citation object as keyword
evidence.** LanceDB (embedded, serverless, file-based) fits the local-first posture (Build Spec
allows "LanceDB or ChromaDB"). The index lives under `indexes/vector/` — derived, gitignored,
regenerable (ADR-0032 §7). Each row stores the **vector** plus the **full `EvidenceHit` citation
field set** mirroring the evidence chunk (`source_id`, advisory `chunk_id`, `ordinal`, `kind`,
`char_start`, `char_end`, `page`, `page_end`, `section`, `heading_path`, `table_reference`,
`sheet_reference`) **and** the per-row staleness fields (decision 3). A vector hit therefore returns the identical evidence object as a
keyword hit — authoritative citation stays `(source_id, char_start, char_end)` — so vector "joins
the same response contract without changing the shape" (ADR-0032 decision 9).

**3. Reproducibility is config-ref identity plus a complete staleness key; the operator owns the
ref.** A local `/embeddings` server does not expose a clean version hash, so **`embedding_model_ref`
*is* the embedding identity**: the operator pins it precisely and bumps it whenever the served model,
quantization, pooling, normalization, or version changes. Two keys are stored:

- **Index-level** (a manifest): `embedding_model_ref`, `embedding_code_version`, `distance_metric`,
  `dimension`, `index_schema_version`.
- **Per-row**: `source_id`, `chunk_id`, `chunk_fingerprint`, `embedding_model_ref`.

Rebuild rules: **any index-level field mismatch refuses an incremental run and requires `--force`** (a
model/metric/dimension/code/schema change invalidates the whole index, per ADR-0032 decision 3); when
only chunk fingerprints differ, **re-embed only the changed chunks**. Runtime guards on every call:
**validate the returned vector dimension** against `embedding_dimension` (hard error on mismatch),
and if the server response carries a `model`, **cross-check it against `embedding_model_ref`** (hard
error unless `EMBEDDING_ALLOW_MODEL_MISMATCH=true` is explicitly set — for the case where the server
reports a cosmetically different string for the same model). This is deliberately *not* an ADR-0027-style durable
record: embeddings derive from deterministic local inputs (unlike non-reproducible LLM sampling), and
**bit-identical vectors are not required** — cosine ranking tolerates the small float nondeterminism
of GPU inference; the staleness key governs *when to re-embed*, not bit-equality. A fixed-probe
fingerprint to auto-detect a silent same-dimension model swap behind an unchanged ref was considered
and **rejected** for v1 (float-tolerance fragility); operator discipline over the ref is the chosen
contract.

**4. `mode=vector` is explicit-only in 4d; fusion and the `auto` blend are Phase 4e.** Because RRF
does not exist yet, 4d does **not** blend channels: an explicit `mode=vector` returns vector evidence
**standalone** in the unchanged `evidence[]` shape with `retrieval_path: ["vector"]`, while `mode=auto`
keeps **keyword-only** evidence until 4e adds RRF (so the §8.2 "conceptual default → keyword + vector"
lands in 4e). The query is embedded from the **raw natural-language text** (length-bounded), *not* the
FTS tokenization/topic-extraction of 4c — embeddings handle natural language natively, so the vector
channel bypasses the keyword query builder. When the vector index or the embedder is **not ready**
(unconfigured, server down, or the index is stale/missing), an explicit `mode=vector` returns a
controlled **503 / clear unavailable response**, never a silent empty that masquerades as "no
matches"; `mode=auto` is unaffected (it isn't running vector in 4d).

**5. Vector reindex is an explicit, deliberate step — never the per-file change hook.** Re-embedding
calls the GPU embedding server, so wiring it into the 4a per-file hook would make ordinary
wiki/chunk editing depend on the embedding server being up and pay embedding latency on every edit.
Instead, `scripts/reindex_vector.py` (root + `--force`, **mirroring `reindex_keyword.py`**, replacing
the scaffold retired in 4a) is run deliberately — after ingest batches or before retrieval evals. The
**keyword index stays the cheap, always-fresh deterministic channel**; the vector index is refreshed
on demand and **will drift between runs by design**. Validators **surface** stale/missing vector state
(changed-since-embed chunks, ref/dimension mismatch) but **never auto-fix** it. Backup posture is
already wired (ADR-0032 §7): the vector index is **opt-in** via `BACKUP_INCLUDE_VECTOR_INDEX`
(recompute-savings only), not backed up by default.

## Consequences

Phase 4d is additive: one new derived vector index, one thin embedding adapter behind the ADR-0025
seam, and an explicit reindex script — with the deterministic 4a–4c stack untouched and still fully
functional without any embedder. It honors the project invariants: retrieval stays key-free by
default (local embeddings; cloud is opt-in and security-gated), evidence stays source-anchored (vector
hits carry the same structured citation), and the LLM/answer layer stays out until Phase 5. The
standing trades: vector recall depends on a **single local embedding model and operator discipline**
bumping `embedding_model_ref` on any model change (a silent same-dimension swap behind an unchanged
ref is undetected by design); the explicit-refresh posture means vector recall reflects the **last
reindex**, with drift surfaced by validators rather than auto-corrected; and `mode=vector` is a
standalone channel until 4e fuses it. The HTTP-local embedding seam, the LanceDB same-citation index,
the config-ref staleness key with `--force`-gated full rebuild, the explicit-only `mode=vector`
contract, and explicit (non-hooked) reindex are the load-bearing commitments; the embedding model id,
the exact LanceDB column types, retry/timeout constants, and the manifest format are tuned during
implementation. New dependency: a `vector` optional-dependency group (LanceDB) — **core install and
the default test suite stay light** (LanceDB-backed tests are skipped unless the `vector` extra is
installed; the maintainer/CI run installs it). The embedding adapter uses **stdlib `urllib`** (timeout
+ bounded retry), not a provider SDK or a new HTTP dependency, so the seam (slice 4d-1) stays
dependency-free.
