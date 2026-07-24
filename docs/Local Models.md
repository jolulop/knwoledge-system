# Local Models — running enrichment and query on a local LLM

> [!summary]
> How to point the pipeline's LLM passes at a **local, OpenAI-compatible model server**
> (Ollama / vLLM / LM Studio) with automatic hosted fallback, per [ADR-0063](adr/0063-local-first-tier-fallback-routing.md).
> The short version: run a local server, set `ENRICH_LOCAL_BASE_URL`, and add a `local:<model>`
> entry to a tier's model chain. Everything else (fallback, freshness, provenance) is automatic.

## How routing works

Each LLM tier is configured as an **ordered chain** of `provider:model_id` refs
(`ENRICH_MODEL_LIGHT`, `ENRICH_MODEL_STANDARD`, `ENRICH_MODEL_HEAVY`, and `QUERY_MODEL`). At the
start of each run the pipeline **resolves the chain once** to the **first member whose adapter is
available**, and fixes that model for the whole run:

- **`local` is "available" when `ENRICH_LOCAL_BASE_URL` is set** — a cheap static check, *not* a
  network probe. No API key is needed for a local server.
- **`anthropic` / `openai` are available when their API key is set.**

So a chain like `local:qwen2.5:7b,anthropic:claude-haiku-4-5` means *"use the local model; fall
through to Haiku only when no local server is configured."* A single value (no comma) is just a
length-1 chain — the pre-local behavior.

## Prerequisites

1. A local server exposing an **OpenAI-compatible `/chat/completions` API**. Examples:
   - **Ollama** → base URL `http://localhost:11434/v1` (`ollama serve`; `ollama pull qwen2.5:7b`)
   - **vLLM** → `http://localhost:8000/v1`
   - **LM Studio** → `http://localhost:1234/v1`
2. The model pulled/served on that server.
3. The `openai` Python SDK installed (the local adapter uses it; `uv add openai` if missing).
4. **Structured-output support.** The adapter requests `response_format: {type: json_schema,
   strict: true}`. Recent Ollama, vLLM, and LM Studio support this; a server that doesn't will fail
   the schema gate. Prefer a capable instruction-tuned model — weak models produce invalid JSON and
   get dropped.

## Configure

Edit your **`.env`** (not `.env.example`). The `.env` loader keeps everything after `=`, so
**never put an inline comment after a value** — comments go on their own line.

```dotenv
# Point the local adapter at your OpenAI-compatible server:
ENRICH_LOCAL_BASE_URL=http://localhost:11434/v1

# Start with the LIGHT tier only (summaries/tags — mechanical, low quality-risk):
ENRICH_MODEL_LIGHT=local:qwen2.5:7b,anthropic:claude-haiku-4-5
```

Notes:
- **Model ids may contain a colon** (Ollama tags like `qwen2.5:7b`). A ref is split on the **first**
  colon only, so `local:qwen2.5:7b` parses as provider `local`, model `qwen2.5:7b`. ✓
- **Every provider named must be known** (`anthropic`, `openai`, `local`). A typo like `bogus:x`
  fails fast with a `ConfigError` at resolve time — even if an earlier chain member is available.
- **Start local on tier-1, expand deliberately.** Standard (claims + knowledge-item extraction) and
  heavy (synthesis/contradiction) *mutate or govern* the semantic layer, so they default
  **hosted-first**. Opt them into local only after validating your local model's quality on your
  corpus, by **appending** a local member:

  ```dotenv
  ENRICH_MODEL_STANDARD=anthropic:claude-sonnet-4-6,local:qwen2.5:32b
  ENRICH_MODEL_HEAVY=anthropic:claude-opus-4-8,local:qwen2.5:72b
  # QUERY_MODEL inherits ENRICH_MODEL_STANDARD when unset; set it explicitly to keep query
  # answers hosted-first while standard is local-first:
  QUERY_MODEL=anthropic:claude-sonnet-4-6
  ```

## Verify

**1. See how each tier resolves under your current config** (no LLM call, no cost):

```bash
uv run python3 - <<'PY'
from app.backend.config import get_settings
from app.llm.client import build_client
s = get_settings(); c = build_client(s)
print("local base url:", s.enrich_local_base_url or "(unset -> local unavailable)")
for tier, chain in [("light", s.enrich_model_light), ("standard", s.enrich_model_standard),
                    ("heavy", s.enrich_model_heavy), ("query", s.query_model)]:
    ref, runnable = c.resolve_run_model(chain)
    print(f"{tier:9} {chain!r:55} -> {ref!r} (runnable={runnable})")
PY
```

With the light chain above and the server configured, the `light` line should resolve
`'local:qwen2.5:7b'`.

**2. Run enrichment and confirm the model that actually produced each artifact** — the resolved
`model_ref` is recorded in every artifact:

```bash
uv run python scripts/enrich.py .
grep model_ref normalized/enrichment/*.json   # -> "local:qwen2.5:7b" for locally-produced summaries
```

## Behavior & gotchas

- **"Available" = configured, not reachable.** If the server is configured but **down**, the run
  still *selects* local, the call fails, and that source is **skipped/errored** (visible in the run
  counts) — it does **not** silently fall back to hosted. Start the server and rerun.
- **No mid-run failover.** A model that is up but returns bad/invalid output is likewise a skip,
  never an automatic hosted retry — so a weak local model surfaces as a fixable skip, never surprise
  hosted-API spend.
- **Sticky-to-chain freshness.** Once a source is enriched by a model that is still in the chain,
  flipping local up/down between runs won't needlessly re-run it. Force a re-derive with the
  currently-resolved model via the producer's `--force` (e.g. `scripts/enrich.py . --force`).
- **No prompt-version churn.** Switching to local is routing/config only — it does not restale the
  vault or change prompt versions.
- **Provenance & reproducibility.** The response cache and each artifact's fingerprint key on the
  exact `model_ref`, so a local and a hosted run of the "same" logical model never collide.

## Environment keys (reference)

| Key | Purpose |
|---|---|
| `ENRICH_LOCAL_BASE_URL` | Base URL of the local OpenAI-compatible server. **Presence gates `local` availability.** |
| `ENRICH_MODEL_LIGHT` | Light-tier chain (summaries/tags). Local-first recipe lives here. |
| `ENRICH_MODEL_STANDARD` | Standard-tier chain (claims + items). Hosted-first by default. |
| `ENRICH_MODEL_HEAVY` | Heavy-tier chain (synthesis/contradiction). Hosted-first by default. |
| `QUERY_MODEL` | Cited-answer chain (`POST /query`). Inherits `ENRICH_MODEL_STANDARD` when unset. |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Gate `anthropic` / `openai` availability (the hosted fallback members). |

See [ADR-0063](adr/0063-local-first-tier-fallback-routing.md) for the full design (per-tier chains,
availability-only resolution, sticky-to-chain freshness, no intra-chain failover) and
[ADR-0025](adr/0025-phase-3-5-enrichment-architecture.md) for the provider-agnostic adapter seam.
