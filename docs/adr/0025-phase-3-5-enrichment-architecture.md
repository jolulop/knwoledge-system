# Phase 3.5 LLM enrichment: provider-agnostic adapter, tiered routing, supervised execution

Phase 3.5 is the first LLM-dependent stage. It enriches the deterministic backbone
(ADR-0013) with summaries, tags, and the semantic node types — concepts, entities,
claims, synthesis. The Build Spec is explicitly provider-neutral (§2.5: "Cloud models
are allowed. OpenAI and Anthropic are acceptable. Local-only AI is not required, but the
architecture must be security-ready"), and `.env.example` has carried both
`OPENAI_API_KEY` and `ANTHROPIC_API_KEY` since Phase 1. Enrichment therefore does **not**
fix a single provider; it abstracts the provider behind a uniform call so any model —
Anthropic, OpenAI, or a local server — can serve an enrichment pass.

**A thin internal `LLMClient` adapter is the uniform call.** One method —
`parse(messages, schema, model_ref) -> validated object` — is the only surface the
enrichment passes use. Per-provider adapters implement it: the native `anthropic` SDK
(which lets that adapter offer prompt caching and the Batch API), the native `openai`
SDK (native structured outputs), and a local adapter speaking the OpenAI-compatible HTTP
wire (Ollama, vLLM, LM Studio). The structured-output contract is uniform and strict —
`parse` returns a schema-valid object or raises a typed error, with native
schema-constrained decoding plus a bounded in-adapter retry (ADR-0026). Provider-specific
accelerators (prompt caching, Batch) are **optional adapter capabilities**, advertised by
flags like `supports_batch`, never load-bearing guarantees; an adapter that lacks them
still satisfies the same `parse` contract. We hand-roll this rather than adopt a unifying
dependency (LiteLLM, etc.), matching the project's dependency-light, local-first ethos
and keeping the untrusted-input boundary (ADR-0026) under our own control.

**Tiered model routing stays, now provider-aware.** Three tiers, each a configurable
`model_ref` of the form `provider:model_id`, match cost to task difficulty; pointing all
three at one `model_ref` collapses the layer to a single model:

- **Tier 1 — light** (`ENRICH_MODEL_LIGHT`, default `anthropic:claude-haiku-4-5`):
  high-volume mechanical passes — summary enrichment, tag suggestion.
- **Tier 2 — standard** (`ENRICH_MODEL_STANDARD`, default `anthropic:claude-sonnet-4-6`):
  per-source extraction — candidate concepts, entities, claims.
- **Tier 3 — heavy** (`ENRICH_MODEL_HEAVY`, default `anthropic:claude-opus-4-8`):
  cross-source reasoning — synthesis and contradiction detection.

Each pass declares its tier in code; only the tier→`model_ref` mapping is configuration.
Per-provider connection settings (base URL, API-key env var) resolve from the provider
half of the `model_ref` through the existing dependency-free env/`.env` config layer; a
local tier additionally needs a base URL (e.g. `ENRICH_LOCAL_BASE_URL`). Citation
*verification* is mechanical (no LLM, ADR-0026) and is not a tier.

The concrete default model ids named above are **config examples, not normative
architecture** — model catalogs churn faster than ADRs. The contract is the `model_ref`
shape and the tier→`model_ref` indirection, not any particular id. So the adapter
**validates at startup** rather than trusting docs: it resolves each configured
`model_ref`, fails fast on an unknown provider or a missing API key/base URL for the
selected provider, and checks that any capability a pass relies on (e.g. `supports_batch`
for the backfill fast-path) is actually advertised — degrading or erroring explicitly
instead of discovering the mismatch mid-run.

**Execution is a supervised backend worker, synchronous by default.** Calls run from
`app/workers/enrich.py`, the same supervised, synchronous shape as the extract and wiki
workers; autonomous scheduling is a Phase 7 concern. The uniform path is a synchronous
`parse`. The ~600-document initial backfill *may* use a provider's Batch API as an
opt-in fast-path **only when the active adapter advertises `supports_batch`** (Anthropic
now, OpenAI later), degrading to synchronous calls otherwise. Prompt caching, where a
provider supports it, is handled transparently inside the adapter. With no API key for
the configured provider, enrichment is skipped and sources remain `summary_status: stub`.
The concrete deployment of local models (running an inference server) is deferred; the
adapter seam is what Phase 3.5 fixes now.

**Enriched output lands in a separate artifact; the Source page stays a single-writer
derived view.** Enrichment passes never write `wiki/Sources/<source_id>.md` directly. Each
pass writes its validated, schema-conformant output to a per-source, content-keyed
**enrichment artifact** (e.g. `normalized/enrichment/<source_id>.json`), which is the
artifact of record (ADR-0027). The deterministic Source-page renderer remains the **sole
writer** of the `.md`: it composes the enrichment artifact into the page, filling the
`_Pending semantic enrichment._` placeholders when an artifact exists and falling back to
the placeholder when it does not. This preserves the Phase-3 invariant that a Source page
is a pure function of its inputs — the generator does a full rewrite with no section-level
merge, so a two-writers-one-file model would clobber LLM content — now with the enrichment
artifact folded into the page's `input_fingerprint`. The artifact carries its own
fingerprint (normalized Markdown + prompt/template version + `model_ref` + schema, ADR-0027)
that drives re-enrichment, so a deterministic re-extraction and an LLM re-enrichment each
invalidate the correct layer without overwriting the other.

Consequences: any model can serve any tier without touching pass code, and a small eval
can re-point a tier — or swap providers — by editing config. The backbone stays offline
and deterministic; only this layer needs a key. The trade is that we own the adapters and
their capability differences (structured output, caching, batch all vary by provider),
keep three `model_ref`s current as catalogs evolve, and absorb a new external dependency
and failure surface (rate limits, refusals, retries) per each provider SDK's behavior.
The previously "fixed, load-bearing" Anthropic mechanisms (`output_config.format`,
prompt caching, Batch) are now portable concepts realized per adapter, not commitments.
