#!/usr/bin/env python3
"""Phase 4d embedding seam (ADR-0033 decision 1).

`EmbeddingClient.embed(texts) -> vectors` over a local OpenAI-compatible `/embeddings` server, using
**stdlib `urllib`** — no provider SDK, no new HTTP dependency. The local default is loopback/LAN-only
(`local_http`); a cloud provider is reachable only through an explicit **three-leg gate**
(`provider=cloud_*` + `EMBEDDING_ALLOW_CLOUD=true` + a dedicated `embedding_api_key`) and must use
HTTPS. The indexer (4d-2) and the `/search` vector channel (4d-3) depend only on the small `Embedder`
surface (`embed` + `dimension`), so tests inject a deterministic fake embedder and never call a model.

Trust-boundary guarantees:
- **No off-host redirects.** The HTTP opener refuses 3xx redirects, so a gated host cannot bounce the
  source-text payload to another host.
- **Scheme guard.** `local_http` allows `http`/`https`; cloud requires `https`.
- **Host guard (lexical, operator-trust).** `local_http` `base_url` must be loopback / private-IP /
  `localhost` / single-label / `.local|.lan|.internal|.home` (ADR-0033). NOTE: this is a *lexical*
  check, not DNS resolution — a hostname that resolves to a public IP is not caught. Operators pin a
  trusted local URL; the redirect+scheme guards close the active exfil vectors.
- **Output validation every call:** response order is restored from a validated `index` permutation;
  dimension is checked; values must be finite numbers; the server `model` is cross-checked (hard
  error unless `EMBEDDING_ALLOW_MODEL_MISMATCH`); base64 vectors are decoded.
"""
from __future__ import annotations

import base64
import json
import ipaddress
import logging
import math
import struct
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    """An embedding call failed, was misconfigured, or returned invalid output."""


class TransientEmbeddingError(EmbeddingError):
    """A retryable transport failure (network error / 5xx)."""


class Embedder(Protocol):
    """The minimal surface the indexer and query path depend on."""

    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


ALLOWED_METRICS = frozenset({"cosine", "l2", "dot"})
_LOCAL_HOST_SUFFIXES = (".local", ".lan", ".internal", ".home")

# The opt-in in-process FlagEmbedding backend (ADR-0053). Selecting it routes the factory to
# BgeM3FlagEmbeddingProvider; every other provider keeps the HTTP EmbeddingClient (ADR-0033).
FLAGEMBEDDING_PROVIDER = "flagembedding_bge_m3"


def _flagembedding_ref(model_id: str, use_fp16: bool) -> str:
    """The FlagEmbedding staleness identity: model id + embedding-affecting **precision** (ADR-0053 dec.4).

    Precision (fp16 vs fp32) is a *systematic* vector difference, so it is part of the identity; device
    and batch_size are execution placement / no semantic effect and are excluded, and the model id
    floats (operators force-reindex on an upstream revision change). The single formula both
    ``resolve_model_ref`` (torch-free, for the index meta) and ``FlagEmbeddingConfig.model_ref`` use, so
    they can never drift.
    """
    return f"{FLAGEMBEDDING_PROVIDER}:{model_id}:{'fp16' if use_fp16 else 'fp32'}"


def is_local_host(host: str) -> bool:
    """Loopback / private / LAN host? (the ADR-0033 `local_http` URL guard — lexical, not resolved).

    IP literals are checked against loopback/private/link-local ranges; hostnames allow ``localhost``,
    single-label names (assumed LAN), and private suffixes — everything else (public IPs, public
    domains) is rejected.
    """
    if not host:
        return False
    host = host.strip("[]")  # ipv6 brackets
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        pass
    h = host.lower()
    if h == "localhost" or "." not in h:
        return True
    return h.endswith(_LOCAL_HOST_SUFFIXES)


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str
    base_url: str
    model_ref: str
    dimension: int
    distance_metric: str = "cosine"
    api_key: str | None = None
    allow_cloud: bool = False
    allow_model_mismatch: bool = False
    timeout_s: float = 30.0
    max_retries: int = 2

    @property
    def is_cloud(self) -> bool:
        return self.provider.startswith("cloud")

    @classmethod
    def from_settings(cls, settings: Any) -> EmbeddingConfig | None:
        """Build from app Settings, ``None`` if unconfigured, or raise on a *partial* config.

        Fully blank (no base_url and no model_ref) → ``None`` (vector cleanly off). A partial config
        (one of the two set, the other missing) is a hard error so a half-finished setup gives
        actionable feedback rather than silently disabling vector search.
        """
        base, ref = settings.embedding_base_url, settings.embedding_model_ref
        if not base and not ref:
            return None
        if not base or not ref:
            raise EmbeddingError(
                "partial embedding config: set BOTH EMBEDDING_BASE_URL and EMBEDDING_MODEL_REF "
                "(or neither to disable vector search)"
            )
        return cls(
            provider=settings.embedding_provider,
            base_url=base,
            model_ref=ref,
            dimension=settings.embedding_dimension,
            distance_metric=settings.embedding_distance_metric,
            api_key=settings.embedding_api_key,
            allow_cloud=settings.embedding_allow_cloud,
            allow_model_mismatch=settings.embedding_allow_model_mismatch,
        )


def validate_gate(c: EmbeddingConfig) -> None:
    """Enforce the ADR-0033 gate + config bounds at construction. Raises EmbeddingError."""
    if c.dimension <= 0:
        raise EmbeddingError(f"embedding_dimension must be > 0, got {c.dimension}")
    if c.distance_metric not in ALLOWED_METRICS:
        raise EmbeddingError(
            f"unknown embedding_distance_metric {c.distance_metric!r}; allowed: {sorted(ALLOWED_METRICS)}"
        )
    scheme = urlparse(c.base_url).scheme
    if c.provider == "local_http":
        if scheme not in {"http", "https"}:
            raise EmbeddingError(f"local_http embedding_base_url must be http(s), got scheme {scheme!r}")
        host = urlparse(c.base_url).hostname or ""
        if not is_local_host(host):
            raise EmbeddingError(
                f"local_http embedding_base_url must be loopback/LAN, got host {host!r}; "
                "to embed off-network, use the explicit cloud gate (ADR-0033)"
            )
    elif c.is_cloud:
        if scheme != "https":
            raise EmbeddingError("cloud embedding requires an https:// base_url (no plaintext key/text)")
        if not c.allow_cloud:
            raise EmbeddingError("cloud embedding requires EMBEDDING_ALLOW_CLOUD=true (ADR-0033)")
        if not c.api_key:
            raise EmbeddingError("cloud embedding requires a dedicated embedding_api_key (ADR-0033)")
    else:
        raise EmbeddingError(f"unknown embedding_provider {c.provider!r}; use local_http or cloud_*")


# (url, payload, headers, timeout_s) -> parsed JSON body. Injectable for tests.
PostFn = Callable[[str, dict, dict, float], dict]


class EmbeddingClient:
    """OpenAI-compatible `/embeddings` adapter. ``post`` is injectable so tests avoid real HTTP."""

    def __init__(self, config: EmbeddingConfig, *, post: PostFn | None = None) -> None:
        validate_gate(config)
        self._config = config
        self._post = post or _urllib_post

    @property
    def dimension(self) -> int:
        return self._config.dimension

    @property
    def model_ref(self) -> str:
        return self._config.model_ref

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        c = self._config
        url = c.base_url.rstrip("/") + "/embeddings"
        payload = {"model": c.model_ref, "input": list(texts), "encoding_format": "float"}
        headers = {"Content-Type": "application/json"}
        if c.api_key:
            headers["Authorization"] = f"Bearer {c.api_key}"

        last: Exception | None = None
        body: dict | None = None
        for _ in range(c.max_retries + 1):
            try:
                body = self._post(url, payload, headers, c.timeout_s)
                break
            except TransientEmbeddingError as exc:
                last = exc
        if body is None:
            raise EmbeddingError(f"embedding server unreachable after {c.max_retries + 1} tries: {last}")
        return self._parse(body, len(texts))

    def _parse(self, body: dict, n: int) -> list[list[float]]:
        c = self._config
        model = body.get("model")
        if model and model != c.model_ref and not c.allow_model_mismatch:
            raise EmbeddingError(
                f"server model {model!r} != embedding_model_ref {c.model_ref!r}; "
                "set EMBEDDING_ALLOW_MODEL_MISMATCH=true to allow"
            )
        data = body.get("data")
        if not isinstance(data, list) or len(data) != n:
            got = len(data) if isinstance(data, list) else "none"
            raise EmbeddingError(f"expected {n} embeddings, got {got}")
        data = _order_by_index(data, n)
        return [_to_vector(row, c.dimension) for row in data]


def _order_by_index(data: list, n: int) -> list:
    """Restore input order from the response `index` fields, validating a complete 0..n-1 permutation.

    All rows carry `index` → must be exactly a permutation of range(n) (no dup/gap/negative/oob), then
    sort. No row carries `index` → keep response order. A mix is ambiguous → hard error.
    """
    flags = [isinstance(d, dict) and "index" in d for d in data]
    if all(flags):
        indexes = [d["index"] for d in data]
        if sorted(indexes) != list(range(n)):
            raise EmbeddingError(f"response `index` fields are not a 0..{n - 1} permutation: {indexes}")
        return sorted(data, key=lambda d: d["index"])
    if any(flags):
        raise EmbeddingError("response mixes rows with and without an `index` field (ambiguous order)")
    return data


def _to_vector(row: Any, dimension: int) -> list[float]:
    vec = row.get("embedding") if isinstance(row, dict) else None
    if isinstance(vec, str):  # base64-encoded float32 (some servers default to this)
        try:
            raw = base64.b64decode(vec)
            vec = list(struct.unpack(f"<{len(raw) // 4}f", raw))
        except (ValueError, struct.error) as exc:
            raise EmbeddingError(f"could not decode base64 embedding: {exc}") from exc
    if not isinstance(vec, list) or len(vec) != dimension:
        got = len(vec) if isinstance(vec, list) else "n/a"
        raise EmbeddingError(f"embedding dimension {got} != expected {dimension}")
    out: list[float] = []
    for x in vec:
        try:
            f = float(x)
        except (TypeError, ValueError) as exc:
            raise EmbeddingError(f"non-numeric embedding value {x!r}") from exc
        if not math.isfinite(f):
            raise EmbeddingError(f"non-finite embedding value {x!r}")
        out.append(f)
    return out


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse all redirects — a gated embedding host must serve directly, never bounce the payload."""

    def _refuse(self, req, fp, code, msg, headers):  # noqa: ANN001
        loc = headers.get("Location")
        raise EmbeddingError(
            f"refusing redirect (HTTP {code}) to {loc!r}: the embedding host must serve directly "
            "(ADR-0033 local guard)"
        )

    http_error_301 = http_error_302 = http_error_303 = http_error_307 = http_error_308 = _refuse


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _urllib_post(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 (scheme/host/redirect guarded)
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code >= 500:
            raise TransientEmbeddingError(f"HTTP {exc.code} from embedding server") from exc
        raise EmbeddingError(f"HTTP {exc.code} from embedding server") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TransientEmbeddingError(f"embedding request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EmbeddingError(f"invalid JSON from embedding server: {exc}") from exc


def client_from_settings(settings: Any, *, post: PostFn | None = None) -> Embedder | None:
    """Build the configured embedder, or ``None`` if none is configured.

    Branches on ``EMBEDDING_PROVIDER`` (ADR-0053): ``flagembedding_bge_m3`` → the in-process
    ``BgeM3FlagEmbeddingProvider`` (``post`` ignored; model loads lazily / at warmup); any other
    provider → the HTTP ``EmbeddingClient`` (``None`` when unconfigured). Both satisfy the small
    ``Embedder`` surface the indexer and query path depend on.
    """
    if getattr(settings, "embedding_provider", None) == FLAGEMBEDDING_PROVIDER:
        return BgeM3FlagEmbeddingProvider(FlagEmbeddingConfig.from_settings(settings))
    config = EmbeddingConfig.from_settings(settings)
    return None if config is None else EmbeddingClient(config, post=post)


# =========================================================================== in-process FlagEmbedding
# The opt-in GPU backend (ADR-0053). Torch + FlagEmbedding are imported lazily *inside* methods, so
# importing this module — and app startup on any non-flagembedding path — never pulls Torch. Dense-only
# v1: only ``dense_vecs`` (dim 1024) is used; sparse + ColBERT are deferred for later hybrid/rerank.


def resolve_model_ref(settings: Any) -> str | None:
    """The index-level staleness identity (ADR-0053 decision 4), computed without loading a model.

    ``flagembedding_bge_m3`` → ``flagembedding_bge_m3:<EMBEDDING_MODEL_ID>:<fp16|fp32>``; every other provider →
    ``settings.embedding_model_ref``. The single source of truth for the vector-index meta identity,
    used by the indexer, the query path, the eval, and the offline validator.
    """
    if getattr(settings, "embedding_provider", None) == FLAGEMBEDDING_PROVIDER:
        return _flagembedding_ref(settings.embedding_model_id, settings.embedding_use_fp16)
    return settings.embedding_model_ref


def provider_configured(settings: Any) -> bool:
    """Is an embedder configured (vector on)? flagembedding is on by selection; HTTP needs base+ref."""
    if getattr(settings, "embedding_provider", None) == FLAGEMBEDDING_PROVIDER:
        return True
    return bool(settings.embedding_base_url and settings.embedding_model_ref)


@dataclass(frozen=True)
class FlagEmbeddingConfig:
    model_id: str
    device: str
    use_fp16: bool
    batch_size: int
    max_length: int
    dimension: int
    cache_dir: Path | None  # None → FlagEmbedding/HF default cache (~/.cache/huggingface)
    distance_metric: str = "cosine"

    @property
    def model_ref(self) -> str:
        return _flagembedding_ref(self.model_id, self.use_fp16)

    @classmethod
    def from_settings(cls, settings: Any) -> FlagEmbeddingConfig:
        cache = settings.embedding_cache_dir
        return cls(
            model_id=settings.embedding_model_id,
            device=settings.embedding_device,
            use_fp16=settings.embedding_use_fp16,
            batch_size=settings.embedding_batch_size,
            max_length=settings.embedding_max_length,
            dimension=settings.embedding_dimension,
            cache_dir=Path(cache) if cache else None,
            distance_metric=settings.embedding_distance_metric,
        )


def _validate_flagembedding_config(c: FlagEmbeddingConfig) -> None:
    if c.dimension <= 0:
        raise EmbeddingError(f"embedding_dimension must be > 0, got {c.dimension}")
    if c.device not in {"cuda", "cpu"}:
        raise EmbeddingError(f"embedding_device must be 'cuda' or 'cpu', got {c.device!r}")
    if c.distance_metric not in ALLOWED_METRICS:
        raise EmbeddingError(
            f"unknown embedding_distance_metric {c.distance_metric!r}; allowed: {sorted(ALLOWED_METRICS)}"
        )
    if c.batch_size <= 0:
        raise EmbeddingError(f"embedding_batch_size must be > 0, got {c.batch_size}")
    if c.max_length <= 0:
        raise EmbeddingError(f"embedding_max_length must be > 0, got {c.max_length}")
    if not c.model_id:
        raise EmbeddingError("embedding_model_id must be set for flagembedding_bge_m3")


def _dense_to_vectors(dense: Any, *, expected_n: int, dimension: int) -> list[list[float]]:
    """Validate + convert FlagEmbedding ``dense_vecs`` (numpy array, shape (n, dim)) to nested lists."""
    try:
        import numpy as np

        arr = np.asarray(dense, dtype=float)
        if not np.isfinite(arr).all():
            raise EmbeddingError("FlagEmbedding produced a non-finite dense vector value")
        vecs = arr.tolist()
    except ImportError:  # numpy ships with FlagEmbedding, but degrade gracefully
        vecs = [list(row) for row in dense]
    if len(vecs) != expected_n:
        raise EmbeddingError(f"expected {expected_n} embeddings, got {len(vecs)}")
    for v in vecs:
        if len(v) != dimension:
            raise EmbeddingError(f"embedding dimension {len(v)} != expected {dimension}")
    return [[float(x) for x in row] for row in vecs]


# Process-level model cache: BGE-M3 is ~2 GB; load it once per (model_id, device, use_fp16, cache_dir)
# and reuse across every provider instance / request (ADR-0053 decision 6 — never reload per query).
_MODEL_CACHE: dict[tuple, Any] = {}
_MODEL_LOCK = threading.Lock()


def _reset_flagembedding_cache() -> None:
    """Drop the cached model(s). For tests only."""
    with _MODEL_LOCK:
        _MODEL_CACHE.clear()


class BgeM3FlagEmbeddingProvider:
    """In-process BAAI/bge-m3 dense embeddings via ``FlagEmbedding.BGEM3FlagModel`` (ADR-0053)."""

    def __init__(self, config: FlagEmbeddingConfig) -> None:
        _validate_flagembedding_config(config)
        self._config = config

    @property
    def dimension(self) -> int:
        return self._config.dimension

    @property
    def model_ref(self) -> str:
        return self._config.model_ref

    @property
    def device(self) -> str:
        return self._config.device

    # ------------------------------------------------------------------ model lifecycle (lazy, cached)
    def _cache_key(self) -> tuple:
        c = self._config
        return (c.model_id, c.device, c.use_fp16, str(c.cache_dir))

    def _assert_device_available(self) -> None:
        """Fail fast if CUDA is requested but unavailable (ADR-0053 decision 6). CPU always ok."""
        if self._config.device != "cuda":
            return
        try:
            import torch
        except ImportError as exc:
            raise EmbeddingError(
                "torch is not installed; install the CUDA 12.8 build for device=cuda "
                "(`uv pip install torch --index-url https://download.pytorch.org/whl/cu128`; "
                "see docs/Environment Setup §14.1)"
            ) from exc
        if not torch.cuda.is_available():
            raise EmbeddingError(
                "EMBEDDING_DEVICE=cuda but torch.cuda.is_available() is False; "
                "set EMBEDDING_DEVICE=cpu or fix the CUDA runtime"
            )

    def _load_model(self) -> Any:
        key = self._cache_key()
        cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached
        with _MODEL_LOCK:
            cached = _MODEL_CACHE.get(key)
            if cached is not None:
                return cached
            self._assert_device_available()
            try:
                from FlagEmbedding import BGEM3FlagModel
            except ImportError as exc:
                raise EmbeddingError(
                    "FlagEmbedding is not installed; install the GPU embedding stack "
                    "(`uv pip install -U FlagEmbedding sentence-transformers transformers accelerate`; "
                    "see docs/Environment Setup §14.1) to use EMBEDDING_PROVIDER=flagembedding_bge_m3"
                ) from exc
            c = self._config
            cache_dir = None
            if c.cache_dir is not None:
                c.cache_dir.mkdir(parents=True, exist_ok=True)
                cache_dir = str(c.cache_dir)
            try:
                model = BGEM3FlagModel(
                    c.model_id, use_fp16=c.use_fp16, device=c.device, cache_dir=cache_dir
                )
            except Exception as exc:  # noqa: BLE001 — any load/download failure is fail-fast
                raise EmbeddingError(f"failed to load {c.model_id!r} on {c.device!r}: {exc}") from exc
            _MODEL_CACHE[key] = model
            logger.info(
                "loaded FlagEmbedding model %s on %s (fp16=%s)", c.model_id, c.device, c.use_fp16
            )
            return model

    # ------------------------------------------------------------------ embedding API (ADR-0053 dec.2)
    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load_model()
        c = self._config
        out = model.encode(
            list(texts),
            batch_size=c.batch_size,
            max_length=c.max_length,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )
        dense = out.get("dense_vecs") if isinstance(out, dict) else None
        if dense is None:
            raise EmbeddingError("FlagEmbedding returned no dense_vecs (return_dense=True expected)")
        return _dense_to_vectors(dense, expected_n=len(texts), dimension=c.dimension)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def health(self) -> dict[str, Any]:
        c = self._config
        info: dict[str, Any] = {
            "provider": FLAGEMBEDDING_PROVIDER,
            "model_id": c.model_id,
            "model_ref": c.model_ref,
            "device": c.device,
            "use_fp16": c.use_fp16,
            "dimension": c.dimension,
            "max_length": c.max_length,
            "batch_size": c.batch_size,
            "model_loaded": self._cache_key() in _MODEL_CACHE,
        }
        try:
            import torch

            info["torch_version"] = torch.__version__
            info["torch_cuda_version"] = torch.version.cuda
            info["cuda_available"] = bool(torch.cuda.is_available())
            info["cuda_device_name"] = (
                torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
            )
        except ImportError:
            info["torch_version"] = None
            info["torch_cuda_version"] = None
            info["cuda_available"] = False
            info["cuda_device_name"] = None
        return info

    def validate_startup(self) -> dict[str, Any]:
        """Fail-fast startup validation (ADR-0053 decision 6): probe Torch/CUDA, log device metadata,
        load the model once. Raises ``EmbeddingError`` on CUDA-unavailable or model-load failure."""
        info = self.health()
        logger.info(
            "embedding backend %s: torch=%s torch.cuda=%s device=%s (%s)",
            FLAGEMBEDDING_PROVIDER, info.get("torch_version"), info.get("torch_cuda_version"),
            self._config.device, info.get("cuda_device_name"),
        )
        self._load_model()  # fail-fast: asserts CUDA (device=cuda) then loads once
        info["model_loaded"] = True
        return info


def warmup_provider(settings: Any) -> dict[str, Any] | None:
    """App-startup warmup (ADR-0053 decision 6). If the in-process FlagEmbedding backend is selected,
    validate Torch/CUDA + load the model once (fail-fast). No-op (``None``) for every other provider,
    so non-vector app roles (ingest/review/lint) stay GPU-independent."""
    if getattr(settings, "embedding_provider", None) != FLAGEMBEDDING_PROVIDER:
        return None
    provider = BgeM3FlagEmbeddingProvider(FlagEmbeddingConfig.from_settings(settings))
    return provider.validate_startup()
