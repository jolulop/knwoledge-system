#!/usr/bin/env python3
"""The provider-agnostic LLM client and tier routing (ADR-0025/0026/0027).

`LLMClient.parse(messages, schema, model_ref)` is the single surface enrichment passes use.
It resolves the provider from `model_ref` (`provider:model_id`), replays a cached response if
present (no provider call), otherwise calls the adapter with native schema-constrained
decoding, re-validates the result, and on a bounded number of failures raises `ParseError`
so the caller can drop the item. Startup validation (`validate_tiers`) fails fast on an
unknown provider or a missing credential rather than discovering it mid-run.
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


def parse_model_ref(model_ref: str) -> tuple[str, str]:
    """Split `provider:model_id`; raise ConfigError if malformed."""
    if not model_ref or ":" not in model_ref:
        raise ConfigError(f"model_ref must be 'provider:model_id', got {model_ref!r}")
    provider, model_id = model_ref.split(":", 1)
    provider, model_id = provider.strip(), model_id.strip()
    if not provider or not model_id:
        raise ConfigError(f"model_ref has an empty half: {model_ref!r}")
    return provider, model_id


class LLMClient:
    def __init__(
        self,
        adapters: dict[str, Any],
        *,
        cache: ResponseCache | None = None,
        max_tokens: int = 1024,
        max_retries: int = 2,
    ) -> None:
        self._adapters = adapters
        self._cache = cache
        self._max_tokens = max_tokens
        self._max_retries = max_retries

    def _adapter(self, provider: str) -> Any:
        adapter = self._adapters.get(provider)
        if adapter is None:
            raise ConfigError(
                f"unknown provider {provider!r}; known: {sorted(self._adapters)}"
            )
        return adapter

    def provider_available(self, model_ref: str) -> bool:
        """True when the provider for this model_ref has the credentials/config to run."""
        provider, _ = parse_model_ref(model_ref)
        return self._adapter(provider).available()

    def validate_tiers(self, tiers: dict[str, str]) -> None:
        """Startup check: every tier resolves to a known provider with a credential."""
        for tier, model_ref in tiers.items():
            provider, _ = parse_model_ref(model_ref)
            adapter = self._adapter(provider)
            if not adapter.available():
                raise ConfigError(
                    f"tier {tier!r} -> {model_ref}: provider {provider!r} has no credential"
                )

    def parse(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        model_ref: str,
        *,
        schema_version: str | None = None,
        prompt_version: str | None = None,
    ) -> dict[str, Any]:
        """Return a schema-valid object, replaying from cache when possible, or raise."""
        provider, model_id = parse_model_ref(model_ref)
        adapter = self._adapter(provider)

        key = cache_key(
            messages, model_ref, schema,
            schema_version=schema_version, prompt_version=prompt_version,
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
                    return cached
                except schema_mod.SchemaError:
                    pass

        last_error: Exception | None = None
        for _ in range(self._max_retries + 1):
            try:
                raw = adapter.parse(messages, schema, model_id, max_tokens=self._max_tokens)
                schema_mod.validate(raw, schema)
            except (AdapterError, schema_mod.SchemaError) as exc:
                last_error = exc
                continue
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


def build_client(settings: Any, *, cache: ResponseCache | None = None) -> LLMClient:
    """Construct an LLMClient with all three adapters wired from config (ADR-0025)."""
    adapters = {
        "anthropic": AnthropicAdapter(api_key=settings.anthropic_api_key),
        "openai": OpenAIAdapter(
            api_key=settings.openai_api_key, base_url=settings.openai_base_url
        ),
        "local": LocalAdapter(base_url=settings.enrich_local_base_url),
    }
    return LLMClient(adapters=adapters, cache=cache, max_tokens=settings.enrich_max_tokens)
