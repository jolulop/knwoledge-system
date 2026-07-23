# Local-first tier fallback routing: per-tier model chains, availability-only resolution

**Status:** design-locked (grill, docs-only) — 2026-07-23, on tip `b74bd37`. Extends ADR-0025
(provider-agnostic adapter + tiered routing) and ADR-0027 (non-deterministic fingerprint +
response cache). Supersedes nothing.

## Context

ADR-0025 already abstracts every enrichment model behind `LLMClient.parse(messages, schema,
model_ref)` and maps each tier to exactly one configurable `model_ref` of the form
`provider:model_id` (`anthropic`, `openai`, or `local` over the OpenAI-compatible wire). What it
does **not** do is let a tier *prefer* one model and fall back to another. The "local-first" goal
(W3) is precisely that routing policy: run the free/private local model when it is available, and
reach for a hosted model only when local cannot serve — without weakening the reproducibility
contract or silently degrading the semantic layer's quality.

The tension the grill resolved is that ADR-0027 bakes the **resolved** `model_ref` into both the
response-cache key and the artifact `input_fingerprint`. A naive fallback makes the effective model
per-item and environment-dependent, which would (a) let two rebuilds silently disagree on which
model produced an artifact, and (b) let a flapping local server churn real provider calls. The
decisions below keep routing dynamic while keeping *each produced artifact* a knowable, stable
function of its inputs.

## Decisions

### 1. Per-tier model chain, availability-only, resolved once per run

Each tier's config value widens from a single `model_ref` to an **ordered, comma-separated chain**
of `model_ref`s. At the start of a run the resolver parses the chain, validates every entry, and
selects the **first candidate whose adapter is `available()`** — then **fixes that `model_ref` for
the whole run**. Availability (credential present, local base URL reachable/configured) is the only
input to selection; a model that is up but produces poor or invalid output is **not** a fallback
trigger. A single-value config is a length-1 chain, so existing configs are unchanged.

Rejected: per-call failure fallback (call local, auto-recall hosted on error). It makes the
effective model per-item and nondeterministic, spends hosted budget on local hiccups, and inverts
the cost intent of local-first. See decision 4 for the failure path we chose instead.

### 2. Only the light tier defaults local-first

The chain mechanism ships for **all** tiers, but only the default *ordering* differs by tier:

| Tier | Default chain order | Rationale |
|---|---|---|
| light (`ENRICH_MODEL_LIGHT`) | `local → hosted` | Summaries/tags are mechanical, high-volume, low quality-risk — the biggest cost win, smallest blast radius. |
| standard (`ENRICH_MODEL_STANDARD`) | `hosted → local` | Claims + the ADR-0059 15-type item extraction *mutate the semantic layer*; a merely-running local model must not silently become their producer. |
| heavy (`ENRICH_MODEL_HEAVY`) | `hosted → local` | Synthesis/contradiction *govern* the semantic layer; same reasoning. |
| query (`QUERY_MODEL`, decision 6) | `hosted → local` | User-facing cited answers; grounding proves citations, not prose quality. |

> W3 ships the provider-chain mechanism for all enrichment tiers, but only the light tier defaults
> local-first. Standard and heavy remain hosted-first because their outputs mutate or govern the
> semantic layer; local use there is explicit operator opt-in until quality is validated.

Because selection is availability-only (decision 1), a local-first default is *operationally* safe:
with no local server configured, `available()` is false and the tier resolves straight to hosted.
The only real risk is *quality* when a local server **is** present but weak — which is why
tier-2/tier-3 stay hosted-first. Operators opt those tiers into local by editing the chain once
their local model is proven on their corpus.

### 3. Sticky-to-chain artifact freshness (availability changes do not restale)

Availability is not a semantic-quality signal, so a change in *which* chain member is currently
runnable must **not** restale existing artifacts. The freshness rule becomes:

> An enrichment artifact is fresh when its recorded `model_ref` is still a member of the configured
> tier chain **and** its recorded-model fingerprint still matches the current normalized input,
> prompt/schema versions, and strategy ref. Availability changes alone do not restale artifacts.
> `--force` bypasses stickiness and re-derives with the currently resolved model.

Concretely, the producer freshness / skip decision (and the lint rot checks, decision 5) recompute
the fingerprint using the **artifact's own recorded `model_ref`**, not the newly-resolved one, and
additionally check chain membership. An artifact re-derives only when its normalized text /
prompt version / schema / strategy ref changed, or its recorded model dropped out of the chain.
This preserves ADR-0027 exactly at the artifact grain — cache keys and `input_fingerprint` stay
keyed on the exact producing `model_ref` — while adding one chain-membership test on top of the
skip path. Without this, a flapping local server would trigger real replacement runs for
claims/items/synthesis even though no source text or prompt contract changed.

### 4. No intra-chain failover on failure

Once the run's model is selected, **any** failure on an item — schema-invalid, refusal, timeout, or
the local server dying mid-run — follows the **existing** path: the bounded in-adapter retry on the
same model, then `ParseError` → the item is dropped / the pass skips (stays `summary_status:
stub`), to be re-run later. The resolver never switches to the next chain member mid-run. A dead
local server surfaces as **visible skipped/error counts** for the operator to fix and rerun — more
honest than producing half-local, half-hosted artifacts in one supervised pass, and it keeps
existing tier-2 safety intact (a failed claims/items run never supersedes the prior semantic layer).

Rejected: the type-discriminated hybrid (re-resolve once on a hard adapter outage). It reintroduces
intra-run churn and a two-model run for a marginal resilience gain the supervised-rerun model
already covers.

### 5. `validate_tiers` and lint rot go chain-aware

- **`validate_tiers` / config validation:** every chain entry must parse (`ConfigError`, fail-fast,
  on a malformed `model_ref` or unknown provider). A tier is *runnable* iff **≥1** chain member is
  `available()`; a missing credential on a non-terminal candidate is not an error, it is just
  skipped. The ADR-0025 posture holds: non-strict default = a tier with zero available members is a
  graceful skip (sources stay `stub`), and the opt-in strict check fails on a zero-available tier.
  `LocalAdapter.available()` returns false (never raises) when its base URL/model is unconfigured.
- **`provider_available`** generalizes to a chain check (≥1 member available).
- **Lint rot (`lint._check_summary_rot` and the synthesis rot check, ADR-0037):** these recompute
  `artifact_fingerprint(current md, model_ref)` and today are handed a single tier `model_ref`. They
  must be handed the tier **chain** and apply the decision-3 rule — rot iff the artifact's recorded
  model is out of the chain, or the fingerprint recomputed under *that* recorded model no longer
  matches — otherwise lint nags on every availability flip.

### 6. `QUERY_MODEL` joins the same resolver, hosted-first

`QUERY_MODEL` (Phase 5 cited answers, `/query`, and the `eval_answers` path that reuses it) is the
same kind of value and resolves through the same seam. It becomes a chain with a length-1
hosted-first default (`anthropic:claude-sonnet-4-6`); an operator adds a local candidate when
proven. It is **never** local-first by default — grounding guarantees citations, not that the prose
is complete, well-prioritized, or correctly synthesized. Eval inherits the resolved query model and
records the actual `model_ref` used, as today.

## Config contract

Ordered comma-chain in the existing per-tier vars (no new schema, backward compatible):

```
ENRICH_MODEL_LIGHT=local:qwen2.5-7b,anthropic:claude-haiku-4-5
ENRICH_MODEL_STANDARD=anthropic:claude-sonnet-4-6,local:qwen2.5-32b
ENRICH_MODEL_HEAVY=anthropic:claude-opus-4-8,local:qwen2.5-72b
QUERY_MODEL=anthropic:claude-sonnet-4-6
ENRICH_LOCAL_BASE_URL=http://localhost:11434/v1
```

**Hard constraint:** the repo's `.env` loader (`config._load_env_file`) strips `#` **only at
line start** — an inline `KEY=val # note` keeps `# note` inside the value. So `.env.example` must
place every explanatory comment on its **own line** above the value, never inline after a chain.
Shipped `.env.example` carries the tier-1 local-first line as a ready-to-uncomment recipe (with a
placeholder local model id the operator replaces) and tier-2/3 with `local` appended.

Routing changes nothing the model sees, so **no `*_PROMPT_VERSION` bumps** (unlike ADR-0061): the
prompt, schema, and strategy ref are untouched, and an artifact produced by a given `model_ref`
keeps its exact fingerprint and cache entry.

## Consequences

- One resolver model across enrichment and query; adding a local model is config-only, no code and
  no migration. Existing single-value configs and existing artifacts stay valid (a prior model is
  chain member 0 → still fresh under decision 3).
- The backfill Batch fast-path (ADR-0025) applies only when the *resolved* adapter advertises
  `supports_batch`; when a tier resolves to `local`, that tier degrades to synchronous calls
  (already the ADR-0025 contract, now reachable by default on tier-1).
- Observability: the resolver logs which `model_ref` each tier resolved to and whether it was the
  preferred candidate or a fallback; skipped/error counts stay visible (decision 4). The artifact
  continues to record the exact producing `model_ref`.
- The cost/quality trade is deliberately asymmetric: tier-1 captures the local cost win by default;
  the semantic-governing tiers stay conservative until an operator validates local quality.

## Tests (for the implementation slice, not this gate)

- Resolver: first-available selection over a chain; single-value chain = today; malformed entry →
  `ConfigError`; all-unavailable tier → graceful skip (non-strict) and strict-mode failure.
- Tier-1 default resolves local when available, hosted when not; tier-2/3/query resolve hosted when
  available even if local is up (default order honored).
- Sticky-to-chain: an artifact whose recorded model is still a chain member does **not** re-derive
  on an availability flip; recompute uses the recorded model; it **does** re-derive on text/prompt/
  schema/strategy change or when the recorded model leaves the chain; `--force` re-derives with the
  resolved model.
- No intra-chain failover: a schema/adapter failure on the selected model yields skip/error counts,
  never a switch to the next member; no half-local/half-hosted run.
- Lint rot is chain-aware: an availability flip produces **no** `summary_rot`/synthesis-rot finding
  while the recorded model stays in the chain; a genuine input change still flags.
- `.env.example` has no inline comment after any chain value (guard test).
