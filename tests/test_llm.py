from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.llm import schema as schema_mod
from app.llm.adapters import AdapterError
from app.llm.cache import ResponseCache, cache_key
from app.llm.client import ConfigError, LLMClient, ParseError, parse_model_ref

SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}},
    "required": ["summary", "tags"],
    "additionalProperties": False,
}
MESSAGES = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]


class FakeAdapter:
    """Records calls; returns a canned (or supplied) object without any network/SDK."""

    name = "anthropic"
    supports_batch = False

    def __init__(self, response=None, *, available=True, error=None):
        self.calls = 0
        self._response = response if response is not None else {"summary": "s", "tags": ["a", "b"]}
        self._available = available
        self._error = error

    def available(self):
        return self._available

    def parse(self, messages, schema, model_id, *, max_tokens):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return dict(self._response) if isinstance(self._response, dict) else self._response


# --- schema validation ------------------------------------------------------


def test_schema_accepts_valid_and_rejects_invalid():
    schema_mod.validate({"summary": "x", "tags": ["t"]}, SCHEMA)
    with pytest.raises(schema_mod.SchemaError):
        schema_mod.validate({"summary": "x"}, SCHEMA)  # missing tags
    with pytest.raises(schema_mod.SchemaError):
        schema_mod.validate({"summary": 1, "tags": []}, SCHEMA)  # wrong type
    with pytest.raises(schema_mod.SchemaError):
        schema_mod.validate({"summary": "x", "tags": [1]}, SCHEMA)  # bad item type
    with pytest.raises(schema_mod.SchemaError):
        schema_mod.validate({"summary": "x", "tags": [], "extra": 1}, SCHEMA)  # additionalProperties


# --- model_ref + cache key --------------------------------------------------


def test_parse_model_ref():
    assert parse_model_ref("anthropic:claude-haiku-4-5") == ("anthropic", "claude-haiku-4-5")
    with pytest.raises(ConfigError):
        parse_model_ref("no-colon")
    with pytest.raises(ConfigError):
        parse_model_ref("anthropic:")


def test_cache_key_includes_every_component():
    base = cache_key(MESSAGES, "anthropic:m1", SCHEMA, schema_version="v1", prompt_version="p1")
    # provider/model (model_ref)
    assert base != cache_key(MESSAGES, "openai:m1", SCHEMA, schema_version="v1", prompt_version="p1")
    assert base != cache_key(MESSAGES, "anthropic:m2", SCHEMA, schema_version="v1", prompt_version="p1")
    # schema
    other = {**SCHEMA, "required": ["summary"]}
    assert base != cache_key(MESSAGES, "anthropic:m1", other, schema_version="v1", prompt_version="p1")
    # schema version / prompt version
    assert base != cache_key(MESSAGES, "anthropic:m1", SCHEMA, schema_version="v2", prompt_version="p1")
    assert base != cache_key(MESSAGES, "anthropic:m1", SCHEMA, schema_version="v1", prompt_version="p2")
    # source fingerprint (embedded in messages)
    msgs2 = [{"role": "system", "content": "S"}, {"role": "user", "content": "different source"}]
    assert base != cache_key(msgs2, "anthropic:m1", SCHEMA, schema_version="v1", prompt_version="p1")


def test_cache_key_accepts_integer_versions():
    assert cache_key(MESSAGES, "anthropic:m1", SCHEMA, schema_version=1, prompt_version=1) == cache_key(
        MESSAGES, "anthropic:m1", SCHEMA, schema_version="1", prompt_version="1"
    )


# --- client behaviour -------------------------------------------------------


def test_parse_returns_validated_object(tmp_path):
    fake = FakeAdapter()
    client = LLMClient({"anthropic": fake})
    out = client.parse(MESSAGES, SCHEMA, "anthropic:m")
    assert out == {"summary": "s", "tags": ["a", "b"]}
    assert fake.calls == 1


def test_cache_replay_makes_no_second_provider_call(tmp_path):
    fake = FakeAdapter()
    cache = ResponseCache(tmp_path / "db" / "llm_cache.sqlite")
    client = LLMClient({"anthropic": fake}, cache=cache)
    a = client.parse(MESSAGES, SCHEMA, "anthropic:m", schema_version="v1", prompt_version="p1")
    b = client.parse(MESSAGES, SCHEMA, "anthropic:m", schema_version="v1", prompt_version="p1")
    assert a == b
    assert fake.calls == 1  # second call replayed from cache, no provider call


def test_cache_replay_revalidates_and_ignores_corrupt_entry(tmp_path):
    fake = FakeAdapter()  # returns valid output
    cache = ResponseCache(tmp_path / "db" / "llm_cache.sqlite")
    # Poison the cache with a schema-invalid entry under the exact key parse() computes.
    key = cache_key(MESSAGES, "anthropic:m", SCHEMA, schema_version="v1", prompt_version="p1")
    cache.put(
        key, provider="anthropic", model_id="m", response={"summary": "no tags"},
        created_at="t", schema_version="v1", prompt_version="p1",
    )
    client = LLMClient({"anthropic": fake}, cache=cache)
    out = client.parse(MESSAGES, SCHEMA, "anthropic:m", schema_version="v1", prompt_version="p1")
    assert out == {"summary": "s", "tags": ["a", "b"]}  # re-derived, not the corrupt entry
    assert fake.calls == 1  # corrupt entry treated as a miss, provider re-called


def test_invalid_output_is_retried_then_dropped():
    fake = FakeAdapter(response={"summary": "x"})  # always missing tags
    client = LLMClient({"anthropic": fake}, max_retries=2)
    with pytest.raises(ParseError):
        client.parse(MESSAGES, SCHEMA, "anthropic:m")
    assert fake.calls == 3  # initial + 2 retries


def test_adapter_error_is_retried_then_dropped():
    fake = FakeAdapter(error=AdapterError("boom"))
    client = LLMClient({"anthropic": fake}, max_retries=1)
    with pytest.raises(ParseError):
        client.parse(MESSAGES, SCHEMA, "anthropic:m")
    assert fake.calls == 2


def test_provider_available_and_tier_validation():
    ok = LLMClient({"anthropic": FakeAdapter(available=True)})
    assert ok.provider_available("anthropic:m") is True
    ok.validate_tiers({"light": "anthropic:m"})

    missing = LLMClient({"anthropic": FakeAdapter(available=False)})
    assert missing.provider_available("anthropic:m") is False
    with pytest.raises(ConfigError):
        missing.validate_tiers({"light": "anthropic:m"})

    unknown = LLMClient({"anthropic": FakeAdapter()})
    with pytest.raises(ConfigError):
        unknown.provider_available("openai:m")
