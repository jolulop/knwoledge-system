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
import math
import struct
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlparse


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


def client_from_settings(settings: Any, *, post: PostFn | None = None) -> EmbeddingClient | None:
    """Build a configured EmbeddingClient, or ``None`` if no embedder is configured."""
    config = EmbeddingConfig.from_settings(settings)
    return None if config is None else EmbeddingClient(config, post=post)
