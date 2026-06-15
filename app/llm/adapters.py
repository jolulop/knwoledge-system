#!/usr/bin/env python3
"""Per-provider LLM adapters behind one uniform `parse` (ADR-0025).

Each adapter turns the provider-agnostic call `parse(messages, schema, model_id)` into a
native, schema-constrained request and returns a plain dict (or raises `AdapterError`).
Provider SDKs are imported lazily so the seam — and the test suite — never depend on them.
Provider-specific accelerators (Batch) are advertised by `supports_batch`, never assumed by
the caller. Anthropic is the first concrete adapter (Phase 3.5a); OpenAI and a local
OpenAI-compatible adapter share the same contract for later activation.
"""
from __future__ import annotations

import json
from typing import Any


class AdapterError(RuntimeError):
    """A provider call failed or returned unparseable output (transient or terminal)."""


def _split_system(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Pull leading system message(s) into a single system string; return (system, rest)."""
    system_parts: list[str] = []
    rest: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "system":
            system_parts.append(str(message.get("content", "")))
        else:
            rest.append(message)
    return "\n\n".join(system_parts), rest


class AnthropicAdapter:
    """Native `anthropic` SDK adapter with schema-constrained structured output."""

    name = "anthropic"
    supports_batch = True

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key

    def available(self) -> bool:
        return bool(self._api_key)

    def parse(
        self, messages: list[dict[str, Any]], schema: dict[str, Any], model_id: str,
        *, max_tokens: int,
    ) -> dict[str, Any]:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - exercised only with the SDK absent
            raise AdapterError("anthropic SDK not installed (pip install 'anthropic')") from exc

        client = anthropic.Anthropic(api_key=self._api_key)
        system, chat = _split_system(messages)
        try:
            response = client.messages.create(
                model=model_id,
                max_tokens=max_tokens,
                system=system or anthropic.NOT_GIVEN,
                messages=chat,
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except Exception as exc:  # provider/network error -> retryable upstream
            raise AdapterError(f"anthropic request failed: {exc}") from exc

        # Check stop_reason before reading content: a refusal has empty/partial content,
        # and a max_tokens truncation yields invalid (cut-off) JSON. Either is a failed
        # attempt the client retries, then drops — never surfaced as data.
        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            raise AdapterError("anthropic declined the request (stop_reason=refusal)")
        if stop == "max_tokens":
            raise AdapterError(
                "anthropic response truncated (stop_reason=max_tokens); raise ENRICH_MAX_TOKENS"
            )

        text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"anthropic returned non-JSON output: {exc}") from exc


class _OpenAICompatibleAdapter:
    """Shared logic for the native OpenAI SDK and local OpenAI-compatible servers."""

    name = "openai"
    supports_batch = False

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self._api_key = api_key
        self._base_url = base_url

    def available(self) -> bool:
        return bool(self._api_key)

    def parse(
        self, messages: list[dict[str, Any]], schema: dict[str, Any], model_id: str,
        *, max_tokens: int,
    ) -> dict[str, Any]:
        try:
            import openai
        except ImportError as exc:  # pragma: no cover
            raise AdapterError("openai SDK not installed (pip install 'openai')") from exc

        client = openai.OpenAI(api_key=self._api_key or "not-needed", base_url=self._base_url)
        try:
            response = client.chat.completions.create(
                model=model_id,
                max_tokens=max_tokens,
                messages=messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "enrichment", "schema": schema, "strict": True},
                },
            )
        except Exception as exc:
            raise AdapterError(f"{self.name} request failed: {exc}") from exc

        text = response.choices[0].message.content or ""
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"{self.name} returned non-JSON output: {exc}") from exc


class OpenAIAdapter(_OpenAICompatibleAdapter):
    name = "openai"


class LocalAdapter(_OpenAICompatibleAdapter):
    """Local model over the OpenAI-compatible HTTP wire (Ollama, vLLM, LM Studio).

    Availability is gated on a configured base URL, not an API key — a local server
    typically needs none. Concrete deployment is deferred (ADR-0025); the seam is fixed.
    """

    name = "local"
    supports_batch = False

    def available(self) -> bool:
        return bool(self._base_url)
