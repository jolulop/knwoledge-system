from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.llm.adapters import AdapterError, AnthropicAdapter, OpenAIAdapter

SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}},
    "required": ["summary", "tags"],
    "additionalProperties": False,
}
MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


def _fake_anthropic(*, text=None, stop_reason="end_turn", raise_exc=None):
    """A stand-in `anthropic` module: client.messages.create(...) -> response object."""
    mod = types.ModuleType("anthropic")
    mod.NOT_GIVEN = object()

    class _Block:
        def __init__(self, t):
            self.type = "text"
            self.text = t

    class _Resp:
        def __init__(self):
            self.stop_reason = stop_reason
            self.content = [] if text is None else [_Block(text)]

    class _Messages:
        def create(self, **kwargs):
            if raise_exc is not None:
                raise raise_exc
            return _Resp()

    class _Client:
        def __init__(self, api_key=None):
            self.messages = _Messages()

    mod.Anthropic = _Client
    return mod


def _fake_openai(*, content=None, raise_exc=None):
    mod = types.ModuleType("openai")

    class _Msg:
        def __init__(self, c):
            self.content = c

    class _Choice:
        def __init__(self, c):
            self.message = _Msg(c)

    class _Resp:
        def __init__(self):
            self.choices = [_Choice(content)]

    class _Completions:
        def create(self, **kwargs):
            if raise_exc is not None:
                raise raise_exc
            return _Resp()

    class _Chat:
        def __init__(self):
            self.completions = _Completions()

    class _Client:
        def __init__(self, api_key=None, base_url=None):
            self.chat = _Chat()

    mod.OpenAI = _Client
    return mod


# --- Anthropic adapter ------------------------------------------------------


def test_anthropic_parses_valid_json(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(text='{"summary": "x", "tags": ["a"]}'))
    out = AnthropicAdapter(api_key="k").parse(MESSAGES, SCHEMA, "claude-haiku-4-5", max_tokens=256)
    assert out == {"summary": "x", "tags": ["a"]}


def test_anthropic_refusal_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(text=None, stop_reason="refusal"))
    with pytest.raises(AdapterError, match="refusal"):
        AnthropicAdapter(api_key="k").parse(MESSAGES, SCHEMA, "m", max_tokens=256)


def test_anthropic_max_tokens_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(text='{"summary": "x"', stop_reason="max_tokens"))
    with pytest.raises(AdapterError, match="truncated"):
        AnthropicAdapter(api_key="k").parse(MESSAGES, SCHEMA, "m", max_tokens=256)


def test_anthropic_non_json_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(text="not json at all"))
    with pytest.raises(AdapterError, match="non-JSON"):
        AnthropicAdapter(api_key="k").parse(MESSAGES, SCHEMA, "m", max_tokens=256)


def test_anthropic_request_error_wrapped(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(raise_exc=RuntimeError("boom")))
    with pytest.raises(AdapterError, match="request failed"):
        AnthropicAdapter(api_key="k").parse(MESSAGES, SCHEMA, "m", max_tokens=256)


def test_anthropic_returns_schema_invalid_json_unjudged(monkeypatch):
    # The adapter parses JSON; schema-conformance is the client's job (valid JSON here).
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(text='{"summary": "x"}'))
    out = AnthropicAdapter(api_key="k").parse(MESSAGES, SCHEMA, "m", max_tokens=256)
    assert out == {"summary": "x"}  # missing tags -> the LLMClient validation gate drops it


# --- OpenAI adapter ---------------------------------------------------------


def test_openai_parses_valid_json(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", _fake_openai(content='{"summary": "x", "tags": []}'))
    out = OpenAIAdapter(api_key="k").parse(MESSAGES, SCHEMA, "gpt-x", max_tokens=256)
    assert out == {"summary": "x", "tags": []}


def test_openai_non_json_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", _fake_openai(content="oops"))
    with pytest.raises(AdapterError, match="non-JSON"):
        OpenAIAdapter(api_key="k").parse(MESSAGES, SCHEMA, "gpt-x", max_tokens=256)


def test_openai_request_error_wrapped(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", _fake_openai(raise_exc=RuntimeError("boom")))
    with pytest.raises(AdapterError, match="request failed"):
        OpenAIAdapter(api_key="k").parse(MESSAGES, SCHEMA, "gpt-x", max_tokens=256)
