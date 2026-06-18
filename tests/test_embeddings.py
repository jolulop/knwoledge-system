from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import embeddings
from app.backend.embeddings import EmbeddingClient, EmbeddingConfig, EmbeddingError, TransientEmbeddingError


# --------------------------------------------------------------------------- shared fake embedder


class FakeEmbedder:
    """Deterministic, order-preserving stand-in for a real embedding model (no network, no model).

    A stable hash of each text seeds a fixed-dimension vector, so embeddings are reproducible across
    runs and processes — the default embedder for every 4d test (4d-2/4d-3 import this).
    """

    def __init__(self, dimension: int = 8) -> None:
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            h = hashlib.sha256(t.encode("utf-8")).digest()
            out.append([h[i % len(h)] / 255.0 for i in range(self.dimension)])
        return out


def test_fake_embedder_is_deterministic_and_order_preserving():
    fe = FakeEmbedder(dimension=8)
    a = fe.embed(["alpha", "beta"])
    b = fe.embed(["alpha", "beta"])
    assert a == b
    assert len(a) == 2 and all(len(v) == 8 for v in a)
    assert fe.embed(["beta", "alpha"]) == [a[1], a[0]]  # order tracks input


# --------------------------------------------------------------------------- local URL guard / gate


@pytest.mark.parametrize("host_url,local", [
    ("http://127.0.0.1:8080/v1", True),
    ("http://localhost:8080", True),
    ("http://[::1]:8080", True),
    ("http://192.168.1.50:8080", True),
    ("http://10.0.0.5:8080", True),
    ("http://gpubox:8080", True),          # single-label LAN host
    ("http://rig.local:8080", True),
    ("http://8.8.8.8:8080", False),        # public IP
    ("https://api.openai.com/v1", False),  # public domain
])
def test_is_local_host(host_url, local):
    from urllib.parse import urlparse
    assert embeddings.is_local_host(urlparse(host_url).hostname or "") is local


def _local_cfg(**kw):
    base = dict(provider="local_http", base_url="http://127.0.0.1:8080/v1",
                model_ref="bge-m3", dimension=4)
    base.update(kw)
    return EmbeddingConfig(**base)


def test_local_http_rejects_public_url():
    with pytest.raises(EmbeddingError):
        EmbeddingClient(_local_cfg(base_url="https://api.openai.com/v1"))


def test_cloud_gate_requires_all_three_legs():
    # provider cloud_* but no allow_cloud
    with pytest.raises(EmbeddingError):
        EmbeddingClient(EmbeddingConfig(provider="cloud_openai", base_url="https://api.openai.com/v1",
                                        model_ref="text-embedding-3-small", dimension=4, api_key="k"))
    # allow_cloud but no key
    with pytest.raises(EmbeddingError):
        EmbeddingClient(EmbeddingConfig(provider="cloud_openai", base_url="https://api.openai.com/v1",
                                        model_ref="m", dimension=4, allow_cloud=True))
    # all three present -> constructs
    EmbeddingClient(EmbeddingConfig(provider="cloud_openai", base_url="https://api.openai.com/v1",
                                    model_ref="m", dimension=4, allow_cloud=True, api_key="k"))


def test_unknown_provider_rejected():
    with pytest.raises(EmbeddingError):
        EmbeddingClient(_local_cfg(provider="weird"))


# --------------------------------------------------------------------------- embed() over fake POST


def _resp(vectors, *, model="bge-m3", with_index=True, shuffle=False):
    data = []
    order = list(range(len(vectors)))
    if shuffle:
        order = order[::-1]
    for i in order:
        row = {"embedding": vectors[i]}
        if with_index:
            row["index"] = i
        data.append(row)
    return {"model": model, "data": data}


def test_embed_preserves_order_even_if_server_reorders():
    vecs = [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]
    captured = {}

    def post(url, payload, headers, timeout):
        captured["url"] = url
        captured["input"] = payload["input"]
        return _resp(vecs, shuffle=True)  # server returns reversed, with index fields

    client = EmbeddingClient(_local_cfg(), post=post)
    out = client.embed(["a", "b"])
    assert out == vecs                      # restored to input order via `index`
    assert captured["url"].endswith("/embeddings")
    assert captured["input"] == ["a", "b"]


def test_embed_empty_is_noop():
    client = EmbeddingClient(_local_cfg(), post=lambda *a: pytest.fail("should not POST"))
    assert client.embed([]) == []


def test_embed_dimension_mismatch_hard_errors():
    client = EmbeddingClient(_local_cfg(dimension=4), post=lambda *a: _resp([[1.0, 2.0, 3.0]]))
    with pytest.raises(EmbeddingError):
        client.embed(["x"])


def test_embed_model_mismatch_hard_errors_unless_allowed():
    def post(*a):
        return _resp([[1.0, 2.0, 3.0, 4.0]], model="some-other-model")
    with pytest.raises(EmbeddingError):
        EmbeddingClient(_local_cfg(dimension=4), post=post).embed(["x"])
    # With the override it passes.
    out = EmbeddingClient(_local_cfg(dimension=4, allow_model_mismatch=True), post=post).embed(["x"])
    assert out == [[1.0, 2.0, 3.0, 4.0]]


def test_embed_count_mismatch_hard_errors():
    client = EmbeddingClient(_local_cfg(dimension=4), post=lambda *a: _resp([[1.0, 2.0, 3.0, 4.0]]))
    with pytest.raises(EmbeddingError):
        client.embed(["x", "y"])  # asked for 2, server returned 1


def test_embed_retries_transient_then_succeeds():
    calls = {"n": 0}

    def post(url, payload, headers, timeout):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise TransientEmbeddingError("boom")
        return _resp([[1.0, 2.0, 3.0, 4.0]])

    client = EmbeddingClient(_local_cfg(dimension=4), post=post)  # max_retries=2 -> 3 tries
    assert client.embed(["x"]) == [[1.0, 2.0, 3.0, 4.0]]
    assert calls["n"] == 3


def test_embed_bounded_retries_then_raises():
    calls = {"n": 0}

    def post(url, payload, headers, timeout):
        calls["n"] += 1
        raise TransientEmbeddingError("always down")

    with pytest.raises(EmbeddingError):
        EmbeddingClient(_local_cfg(dimension=4), post=post).embed(["x"])
    assert calls["n"] == 3  # bounded: max_retries(2) + 1, not unbounded


# --------------------------------------------------------------------------- settings factory


def test_client_from_settings_none_when_unconfigured(tmp_path):
    from app.backend.config import get_settings
    settings = get_settings(tmp_path)  # no EMBEDDING_BASE_URL / EMBEDDING_MODEL_REF
    assert embeddings.client_from_settings(settings) is None
    assert EmbeddingConfig.from_settings(settings) is None


def test_client_from_settings_builds_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://127.0.0.1:8080/v1")
    monkeypatch.setenv("EMBEDDING_MODEL_REF", "bge-m3")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "4")
    from app.backend.config import get_settings
    settings = get_settings(tmp_path)
    client = embeddings.client_from_settings(settings, post=lambda *a: _resp([[1.0, 2.0, 3.0, 4.0]]))
    assert client is not None and client.dimension == 4
    assert client.embed(["x"]) == [[1.0, 2.0, 3.0, 4.0]]


@pytest.mark.parametrize("base_only,ref_only", [(True, False), (False, True)])
def test_partial_config_is_hard_error(tmp_path, monkeypatch, base_only, ref_only):
    if base_only:
        monkeypatch.setenv("EMBEDDING_BASE_URL", "http://127.0.0.1:8080/v1")
    if ref_only:
        monkeypatch.setenv("EMBEDDING_MODEL_REF", "bge-m3")
    from app.backend.config import get_settings
    with pytest.raises(EmbeddingError):
        EmbeddingConfig.from_settings(get_settings(tmp_path))


# --------------------------------------------------------------------------- hardening (review)


def test_scheme_guard_rejects_non_http():
    with pytest.raises(EmbeddingError):
        EmbeddingClient(_local_cfg(base_url="file:///etc/passwd"))
    with pytest.raises(EmbeddingError):
        EmbeddingClient(_local_cfg(base_url="ftp://127.0.0.1/x"))


def test_cloud_requires_https():
    with pytest.raises(EmbeddingError):
        EmbeddingClient(EmbeddingConfig(provider="cloud_openai", base_url="http://api.openai.com/v1",
                                        model_ref="m", dimension=4, allow_cloud=True, api_key="k"))


def test_config_bounds_validated():
    with pytest.raises(EmbeddingError):
        EmbeddingClient(_local_cfg(dimension=0))
    with pytest.raises(EmbeddingError):
        EmbeddingClient(_local_cfg(distance_metric="hamming"))


def test_redirect_is_refused():
    import email.message
    import io
    msg = email.message.Message()
    msg["Location"] = "https://evil.example.com/embeddings"
    handler = embeddings._NoRedirectHandler()
    with pytest.raises(EmbeddingError):
        handler.http_error_302(None, io.BytesIO(b""), 302, "Found", msg)


def test_payload_has_encoding_format_and_no_auth_without_key():
    captured = {}

    def post(url, payload, headers, timeout):
        captured["payload"] = payload
        captured["headers"] = headers
        return _resp([[1.0, 2.0, 3.0, 4.0]])

    EmbeddingClient(_local_cfg(dimension=4), post=post).embed(["x"])
    assert captured["payload"]["encoding_format"] == "float"
    assert "Authorization" not in captured["headers"]  # local_http, no api_key


def test_base64_embedding_is_decoded():
    import base64
    import struct
    vec = [1.0, 2.0, 3.0, 4.0]
    b64 = base64.b64encode(struct.pack("<4f", *vec)).decode()

    def post(*a):
        return {"model": "bge-m3", "data": [{"embedding": b64, "index": 0}]}

    out = EmbeddingClient(_local_cfg(dimension=4), post=post).embed(["x"])
    assert out[0] == pytest.approx(vec)


@pytest.mark.parametrize("data", [
    [{"embedding": [1, 2, 3, 4], "index": 0}, {"embedding": [5, 6, 7, 8], "index": 0}],   # dup
    [{"embedding": [1, 2, 3, 4], "index": 0}, {"embedding": [5, 6, 7, 8], "index": 2}],   # gap
    [{"embedding": [1, 2, 3, 4], "index": -1}, {"embedding": [5, 6, 7, 8], "index": 0}],  # negative
    [{"embedding": [1, 2, 3, 4], "index": 0}, {"embedding": [5, 6, 7, 8]}],               # mixed
])
def test_bad_index_permutations_hard_error(data):
    def post(*a, _d=data):
        return {"model": "bge-m3", "data": _d}

    with pytest.raises(EmbeddingError):
        EmbeddingClient(_local_cfg(dimension=4), post=post).embed(["x", "y"])


@pytest.mark.parametrize("bad", [
    [float("inf"), 1.0, 2.0, 3.0],
    [float("nan"), 1.0, 2.0, 3.0],
    ["not-a-number", 1.0, 2.0, 3.0],
])
def test_non_finite_or_non_numeric_vectors_hard_error(bad):
    def post(*a, _b=bad):
        return _resp([_b])

    with pytest.raises(EmbeddingError):
        EmbeddingClient(_local_cfg(dimension=4), post=post).embed(["x"])
