"""Tests for the in-process FlagEmbedding backend (ADR-0053).

Two layers:
- **Torch-free unit tests** (always run): identity/config/factory/health logic that never loads a model
  or imports Torch — so the default key-free CI gate stays light.
- **Real-model tests** (opt-in): the CPU integration test (`@pytest.mark.model`) and the CUDA smoke
  (`@pytest.mark.gpu`) load BAAI/bge-m3. They are skipped unless ``KS_RUN_EMBED_TESTS=1`` (they download
  ~2 GB and are slow); the GPU one additionally needs CUDA. Run them explicitly, e.g.
  ``KS_RUN_EMBED_TESTS=1 uv run pytest -m gpu``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import embeddings
from app.backend.config import get_settings
from app.backend.embeddings import (
    BgeM3FlagEmbeddingProvider,
    EmbeddingError,
    FlagEmbeddingConfig,
    _dense_to_vectors,
)

_RUN_REAL = os.environ.get("KS_RUN_EMBED_TESTS") == "1"
SMOKE_TEXTS = ["hello world", "hola mundo", "semantic search over enterprise documents"]


def _torch_importable() -> bool:
    try:
        import torch  # noqa: F401
    except ImportError:
        return False
    return True


def _flagembedding_importable() -> bool:
    try:
        import FlagEmbedding  # noqa: F401
        import torch  # noqa: F401
    except ImportError:
        return False
    return True


def _cuda_available() -> bool:
    if not _torch_importable():
        return False
    import torch

    return bool(torch.cuda.is_available())


def _flag_settings(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "flagembedding_bge_m3")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return get_settings(tmp_path)


# --------------------------------------------------------------------------- staleness identity (torch-free)


def test_resolve_model_ref_flagembedding(tmp_path, monkeypatch):
    s = _flag_settings(tmp_path, monkeypatch)  # default model id, fp16
    assert embeddings.resolve_model_ref(s) == "flagembedding_bge_m3:BAAI/bge-m3:fp16"
    s2 = _flag_settings(tmp_path, monkeypatch, EMBEDDING_MODEL_ID="BAAI/bge-large")
    assert embeddings.resolve_model_ref(s2) == "flagembedding_bge_m3:BAAI/bge-large:fp16"


def test_resolve_model_ref_folds_precision(tmp_path, monkeypatch):
    # Precision is embedding-affecting → part of the staleness identity (ADR-0053 dec.4).
    fp16 = embeddings.resolve_model_ref(_flag_settings(tmp_path, monkeypatch, EMBEDDING_USE_FP16="true"))
    fp32 = embeddings.resolve_model_ref(_flag_settings(tmp_path, monkeypatch, EMBEDDING_USE_FP16="false"))
    assert fp16 == "flagembedding_bge_m3:BAAI/bge-m3:fp16"
    assert fp32 == "flagembedding_bge_m3:BAAI/bge-m3:fp32"
    assert fp16 != fp32  # switching precision forces a --force rebuild


def test_resolve_model_ref_local_http_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBEDDING_MODEL_REF", "bge-m3")
    s = get_settings(tmp_path)  # provider defaults to local_http
    assert embeddings.resolve_model_ref(s) == "bge-m3"


def test_resolve_model_ref_none_when_unconfigured(tmp_path):
    s = get_settings(tmp_path)  # local_http, no ref
    assert embeddings.resolve_model_ref(s) is None


# --------------------------------------------------------------------------- configured gate (torch-free)


def test_provider_configured_flagembedding_true_without_base_url(tmp_path, monkeypatch):
    s = _flag_settings(tmp_path, monkeypatch)
    assert embeddings.provider_configured(s) is True  # on by selection; no base_url needed


def test_provider_configured_local_http_needs_base_and_ref(tmp_path, monkeypatch):
    assert embeddings.provider_configured(get_settings(tmp_path)) is False
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://127.0.0.1:8080/v1")
    monkeypatch.setenv("EMBEDDING_MODEL_REF", "bge-m3")
    assert embeddings.provider_configured(get_settings(tmp_path)) is True


# --------------------------------------------------------------------------- factory selection (no model load)


def test_client_from_settings_returns_flagembedding_provider(tmp_path, monkeypatch):
    embeddings._reset_flagembedding_cache()
    s = _flag_settings(tmp_path, monkeypatch)
    emb = embeddings.client_from_settings(s)
    assert isinstance(emb, BgeM3FlagEmbeddingProvider)
    assert emb.dimension == 1024
    assert emb.model_ref == "flagembedding_bge_m3:BAAI/bge-m3:fp16"
    # Constructing the provider must NOT load the (~2 GB) model.
    assert emb._cache_key() not in embeddings._MODEL_CACHE


def test_client_from_settings_local_http_none_when_unconfigured(tmp_path):
    assert embeddings.client_from_settings(get_settings(tmp_path)) is None


# --------------------------------------------------------------------------- config parse + validation


def test_flagembedding_config_from_settings(tmp_path, monkeypatch):
    s = _flag_settings(
        tmp_path, monkeypatch, EMBEDDING_DEVICE="cpu", EMBEDDING_USE_FP16="false",
        EMBEDDING_BATCH_SIZE="8", EMBEDDING_MAX_LENGTH="512",
    )
    c = FlagEmbeddingConfig.from_settings(s)
    assert (c.model_id, c.device, c.use_fp16, c.batch_size, c.max_length, c.dimension) == (
        "BAAI/bge-m3", "cpu", False, 8, 512, 1024)
    assert c.model_ref == "flagembedding_bge_m3:BAAI/bge-m3:fp32"  # use_fp16=false


@pytest.mark.parametrize("env", [
    {"EMBEDDING_DEVICE": "tpu"},
    {"EMBEDDING_DIMENSION": "0"},
    {"EMBEDDING_BATCH_SIZE": "0"},
    {"EMBEDDING_MAX_LENGTH": "0"},
    {"EMBEDDING_DISTANCE_METRIC": "hamming"},
])
def test_config_bounds_rejected(tmp_path, monkeypatch, env):
    s = _flag_settings(tmp_path, monkeypatch, **env)
    with pytest.raises(EmbeddingError):
        BgeM3FlagEmbeddingProvider(FlagEmbeddingConfig.from_settings(s))


# --------------------------------------------------------------------------- health (torch-optional, no load)


def test_health_shape_without_loading_model(tmp_path, monkeypatch):
    embeddings._reset_flagembedding_cache()
    s = _flag_settings(tmp_path, monkeypatch, EMBEDDING_DEVICE="cpu")
    info = BgeM3FlagEmbeddingProvider(FlagEmbeddingConfig.from_settings(s)).health()
    for key in ("provider", "model_id", "model_ref", "device", "dimension", "model_loaded",
                "cuda_available"):
        assert key in info
    assert info["provider"] == "flagembedding_bge_m3"
    assert info["model_loaded"] is False  # health never loads the model


# --------------------------------------------------------------------------- dense_vecs validation (numpy-optional)


def test_dense_to_vectors_ok():
    out = _dense_to_vectors([[1.0, 2.0], [3.0, 4.0]], expected_n=2, dimension=2)
    assert out == [[1.0, 2.0], [3.0, 4.0]]


def test_dense_to_vectors_count_mismatch():
    with pytest.raises(EmbeddingError):
        _dense_to_vectors([[1.0, 2.0]], expected_n=2, dimension=2)


def test_dense_to_vectors_dim_mismatch():
    with pytest.raises(EmbeddingError):
        _dense_to_vectors([[1.0, 2.0, 3.0]], expected_n=1, dimension=2)


# --------------------------------------------------------------------------- CUDA fail-fast (needs torch)


@pytest.mark.skipif(not _torch_importable(), reason="torch not installed")
def test_cuda_requested_but_unavailable_fails_fast(tmp_path, monkeypatch):
    import torch

    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    s = _flag_settings(tmp_path, monkeypatch, EMBEDDING_DEVICE="cuda")
    provider = BgeM3FlagEmbeddingProvider(FlagEmbeddingConfig.from_settings(s))
    with pytest.raises(EmbeddingError, match="cuda"):
        provider.validate_startup()  # raises at the CUDA assert, before any model load


@pytest.mark.skipif(not _torch_importable(), reason="torch not installed")
def test_cpu_device_skips_cuda_assert(tmp_path, monkeypatch):
    import torch

    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    s = _flag_settings(tmp_path, monkeypatch, EMBEDDING_DEVICE="cpu")
    # _assert_device_available is a no-op for cpu even when CUDA is unavailable.
    BgeM3FlagEmbeddingProvider(FlagEmbeddingConfig.from_settings(s))._assert_device_available()


# --------------------------------------------------------------------------- real model (opt-in, slow)


@pytest.mark.model
@pytest.mark.skipif(
    not (_RUN_REAL and _flagembedding_importable()),
    reason="set KS_RUN_EMBED_TESTS=1 with FlagEmbedding installed (downloads BGE-M3)",
)
def test_real_bge_m3_cpu_smoke(tmp_path, monkeypatch):
    embeddings._reset_flagembedding_cache()
    s = _flag_settings(tmp_path, monkeypatch, EMBEDDING_DEVICE="cpu", EMBEDDING_USE_FP16="false")
    provider = BgeM3FlagEmbeddingProvider(FlagEmbeddingConfig.from_settings(s))
    vecs = provider.embed_texts(SMOKE_TEXTS)
    assert len(vecs) == 3 and all(len(v) == 1024 for v in vecs)
    assert provider.embed_query("single") and len(provider.embed_query("single")) == 1024


@pytest.mark.gpu
@pytest.mark.skipif(
    not (_RUN_REAL and _cuda_available()),
    reason="set KS_RUN_EMBED_TESTS=1 on a CUDA box (real BGE-M3 GPU smoke)",
)
def test_real_bge_m3_cuda_smoke(tmp_path, monkeypatch):
    embeddings._reset_flagembedding_cache()
    s = _flag_settings(tmp_path, monkeypatch, EMBEDDING_DEVICE="cuda")
    provider = BgeM3FlagEmbeddingProvider(FlagEmbeddingConfig.from_settings(s))
    info = provider.validate_startup()
    assert info["cuda_available"] is True and info["model_loaded"] is True
    vecs = provider.embed_texts(SMOKE_TEXTS)
    assert len(vecs) == 3
    assert all(v is not None and len(v) == 1024 for v in vecs)
    # Singleton: a second provider reuses the cached model (no reload).
    assert BgeM3FlagEmbeddingProvider(FlagEmbeddingConfig.from_settings(s))._cache_key() in \
        embeddings._MODEL_CACHE
