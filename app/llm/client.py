#!/usr/bin/env python3
"""The provider-agnostic LLM client and tier routing (ADR-0025/0026/0027).

`LLMClient.parse(messages, schema, model_ref)` is the single surface enrichment passes use.
It resolves the provider from `model_ref` (`provider:model_id`), replays a cached response if
present (no provider call), otherwise calls the adapter with native schema-constrained
decoding, re-validates the result, and on a bounded number of failures raises `ParseError`
so the caller can drop the item. Startup validation (`validate_tiers`) fails fast on an
unknown provider or a missing credential rather than discovering it mid-run.

With a usage ledger (ADR-0069 decision 6; `build_client` always wires one), every provider attempt
is recorded `started` before it is sent and finalized after with its outcome and the
provider-reported usage, including failed and retried attempts; a cache replay is counted as an
avoided call instead.
"""
from __future__ import annotations

from typing import Any

from app.backend.manifests import iso_now
from app.llm import schema as schema_mod
from app.llm.adapters import (
    AdapterError,
    AnthropicAdapter,
    LocalAdapter,
    OpenAIAdapter,
)
from app.llm.cache import ResponseCache, cache_key
from app.llm.usage import UsageLedger, input_chars

# Default tier -> model_ref mapping (config examples, not normative — ADR-0025).
DEFAULT_TIERS = {
    "light": "anthropic:claude-haiku-4-5",
    "standard": "anthropic:claude-sonnet-4-6",
    "heavy": "anthropic:claude-opus-4-8",
}


class ConfigError(ValueError):
    """A model_ref is malformed, names an unknown provider, or lacks a credential."""


class ParseError(RuntimeError):
    """The model did not return schema-valid output within the retry budget."""


class CacheMiss(ParseError):
    """A cache-only call found no replayable response — raised before any provider call."""


def parse_model_ref(model_ref: str) -> tuple[str, str]:
    """Split `provider:model_id`; raise ConfigError if malformed."""
    if not model_ref or ":" not in model_ref:
        raise ConfigError(f"model_ref must be 'provider:model_id', got {model_ref!r}")
    provider, model_id = model_ref.split(":", 1)
    provider, model_id = provider.strip(), model_id.strip()
    if not provider or not model_id:
        raise ConfigError(f"model_ref has an empty half: {model_ref!r}")
    return provider, model_id


def parse_chain(chain: str) -> list[str]:
    """Split an ordered `provider:model_id,provider:model_id,...` model chain into validated refs.

    A single value is a length-1 chain, so pre-ADR-0063 configs are unchanged. Each entry must parse;
    a malformed entry or an empty chain raises ConfigError (fail fast), never a silent drop.
    """
    refs = [r.strip() for r in chain.split(",") if r.strip()]
    if not refs:
        raise ConfigError(f"empty model chain: {chain!r}")
    for ref in refs:
        parse_model_ref(ref)  # raises ConfigError on a malformed / empty-half entry
    return refs


class LLMClient:
    def __init__(
        self,
        adapters: dict[str, Any],
        *,
        cache: ResponseCache | None = None,
        max_tokens: int = 1024,
        max_retries: int = 2,
        ledger: UsageLedger | None = None,
    ) -> None:
        self._adapters = adapters
        self._cache = cache
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        self._ledger = ledger

    def _adapter(self, provider: str) -> Any:
        adapter = self._adapters.get(provider)
        if adapter is None:
            raise ConfigError(
                f"unknown provider {provider!r}; known: {sorted(self._adapters)}"
            )
        return adapter

    def provider_available(self, model_ref: str) -> bool:
        """True when the provider for this single model_ref has the credentials/config to run."""
        provider, _ = parse_model_ref(model_ref)
        return self._adapter(provider).available()

    def resolve_run_model(self, chain: str) -> tuple[str, bool]:
        """Resolve an ordered chain to `(concrete_model_ref, available)`, fixed for the run (ADR-0063).

        Availability-only selection: return the first chain member whose adapter is `available()`
        (credential present / local base URL configured) with `available=True`. When no member can
        run, return the **first-preference** ref with `available=False` — so callers keep a valid,
        deterministic concrete ref for fingerprints/records without ever making a call. A member that
        cannot run is skipped, never called; quality is not a selection input.
        """
        refs = parse_chain(chain)
        # Validate EVERY member's provider before selecting one (ADR-0063 decision 5): an unknown
        # provider anywhere in the chain is a config error that must fail fast, even if an earlier
        # member is available — else `anthropic:m,bogus:x` would silently "work" whenever Anthropic is
        # up and `validate_tiers` would not actually validate the whole chain. `_adapter` raises
        # ConfigError on an unknown provider.
        candidates = [(ref, self._adapter(parse_model_ref(ref)[0])) for ref in refs]
        for ref, adapter in candidates:
            if adapter.available():
                return ref, True
        return refs[0], False

    def chain_available(self, chain: str) -> bool:
        """True when at least one member of the ordered chain is available (ADR-0063)."""
        return self.resolve_run_model(chain)[1]

    def validate_tiers(self, tiers: dict[str, str]) -> None:
        """Strict startup check (opt-in): every tier chain parses and has >=1 available member.

        A malformed chain raises ConfigError (fail fast); a well-formed chain with no available member
        also raises — the strict posture for deployments that require enrichment to run. The default
        graceful path (ADR-0025) does NOT call this: an unavailable tier simply skips and sources stay
        `stub`.
        """
        for tier, chain in tiers.items():
            _ref, available = self.resolve_run_model(chain)  # parse_chain raises on malformed
            if not available:
                raise ConfigError(
                    f"tier {tier!r} -> {chain}: no available model in the chain "
                    f"(need a credential/base URL for at least one member)"
                )

    def parse(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        model_ref: str,
        *,
        schema_version: str | None = None,
        prompt_version: str | None = None,
        strategy_ref: str | None = None,
        cache_only: bool = False,
        operation: str | None = None,
    ) -> dict[str, Any]:
        """Return a schema-valid object, replaying from cache when possible, or raise.

        `strategy_ref` (ADR-0056) is the explicit extraction-strategy component of cache
        identity — tier-2 callers pass their coverage strategy so a window-budget or
        input-cap change forces fresh calls without pretending the prompt changed.
        `cache_only` (ADR-0066 Re-suggest) replays or raises :class:`CacheMiss` — the provider
        is never called. `operation` names the unit of work in the usage ledger (ADR-0069).
        """
        provider, model_id = parse_model_ref(model_ref)
        adapter = self._adapter(provider)

        key = cache_key(
            messages, model_ref, schema,
            schema_version=schema_version, prompt_version=prompt_version,
            strategy_ref=strategy_ref,
        )
        if self._cache is not None:
            cached = self._cache.get(key)
            if cached is not None:
                # Replay (no provider call, ADR-0027) — but re-validate: the contract is
                # valid-or-raises, so a stale entry from a tightened validator or a corrupt
                # DB must not slip through. An invalid entry is treated as a miss and
                # re-derived below.
                try:
                    schema_mod.validate(cached, schema)
                    if self._ledger is not None:
                        self._ledger.cache_hit(model_ref)
                    return cached
                except schema_mod.SchemaError:
                    pass
        if cache_only:
            raise CacheMiss(f"{model_ref}: no cached response (cache-only replay)")

        last_error: Exception | None = None
        sent_chars = input_chars(messages, schema)
        for retry in range(self._max_retries + 1):
            attempt = None
            if self._ledger is not None:              # recorded before sending, or not sent
                attempt = self._ledger.start(
                    model_ref=model_ref, provider=provider, retry=retry, input_chars=sent_chars,
                    max_output_tokens=self._max_tokens, operation=operation)
            usage: dict[str, Any] | None = None
            try:
                if hasattr(adapter, "parse_with_usage"):
                    raw, usage = adapter.parse_with_usage(messages, schema, model_id,
                                                          max_tokens=self._max_tokens)
                else:                                 # an adapter that reports no usage
                    raw = adapter.parse(messages, schema, model_id, max_tokens=self._max_tokens)
                schema_mod.validate(raw, schema)
            except AdapterError as exc:
                self._finish(attempt, exc.outcome, exc.usage)
                last_error = exc
                continue
            except schema_mod.SchemaError as exc:     # the response still consumed tokens
                self._finish(attempt, "schema_error", usage)
                last_error = exc
                continue
            except BaseException:                     # unexpected failure or interrupt
                self._finish(attempt, "adapter_error", usage)
                raise
            self._finish(attempt, "ok", usage)
            if self._cache is not None:
                self._cache.put(
                    key, provider=provider, model_id=model_id, response=raw,
                    created_at=iso_now(),
                    schema_version=schema_version, prompt_version=prompt_version,
                )
            return raw

        raise ParseError(
            f"{model_ref}: no schema-valid output after {self._max_retries + 1} attempt(s): "
            f"{last_error}"
        ) from last_error

    def _finish(self, attempt: str | None, outcome: str, usage: dict[str, Any] | None) -> None:
        if attempt is not None and self._ledger is not None:
            self._ledger.finish(attempt, outcome=outcome, usage=usage)


def build_client(settings: Any, *, cache: ResponseCache | None = None) -> LLMClient:
    """Construct an LLMClient with all three adapters wired from config (ADR-0025) and the usage
    ledger in the jobs database (ADR-0069), so every producer and `/query` records its attempts."""
    adapters = {
        "anthropic": AnthropicAdapter(api_key=settings.anthropic_api_key),
        "openai": OpenAIAdapter(
            api_key=settings.openai_api_key, base_url=settings.openai_base_url
        ),
        "local": LocalAdapter(base_url=settings.enrich_local_base_url),
    }
    return LLMClient(adapters=adapters, cache=cache, max_tokens=settings.enrich_max_tokens,
                     ledger=UsageLedger(settings.jobs_db_path))
