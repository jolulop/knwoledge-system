#!/usr/bin/env python3
"""Phase 1 configuration: project root, repository paths, and app settings.

Dependency-free. Reads an optional .env from the project root, falling back to the
process environment and then sensible defaults. KNOWLEDGE_SYSTEM_HOME overrides the
project root; otherwise the root is derived from this file's location in the tree.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# app/backend/config.py -> repo root is two levels up from this file's parent.
_DERIVED_ROOT = Path(__file__).resolve().parents[2]


def _load_env_file(root: Path) -> dict[str, str]:
    """Parse a minimal KEY=VALUE .env file. Comments and blank lines are ignored."""
    env: dict[str, str] = {}
    env_path = root / ".env"
    if not env_path.exists():
        return env
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def _resolve_root() -> Path:
    """Environment wins, then .env at the derived root, then the derived root."""
    env_root = os.environ.get("KNOWLEDGE_SYSTEM_HOME")
    if env_root:
        return Path(env_root).resolve()
    file_env = _load_env_file(_DERIVED_ROOT)
    if file_env.get("KNOWLEDGE_SYSTEM_HOME"):
        return Path(file_env["KNOWLEDGE_SYSTEM_HOME"]).resolve()
    return _DERIVED_ROOT


@dataclass(frozen=True)
class Settings:
    root: Path
    inbox_dir: Path
    manifests_dir: Path
    db_dir: Path
    jobs_db_path: Path
    # Authoritative semantic graph (ADR-0030); read-only projection in Phase 4b.
    graph_db_path: Path
    # Phase 4 retrieval: derived keyword index (4a) + retrieval policy (4c).
    keyword_index_path: Path
    retrieval_policy_path: Path
    # Phase 2 normalized layer (ADR-0011): one set of files per source id.
    normalized_dir: Path
    markdown_dir: Path
    chunks_dir: Path
    tables_dir: Path
    extraction_logs_dir: Path
    # Phase 2 extraction safety + chunking limits (ADR-0010 / Phase 2 Plan §13).
    extract_max_file_mb: int
    extract_timeout_s: int
    chunk_target_chars: int
    chunk_max_chars: int
    # Phase 3 wiki layer (ADR-0013..0022). Mutable local data, not committed (ADR-0014).
    wiki_dir: Path
    sources_dir: Path
    templates_dir: Path
    wiki_summary_max_chars: int
    wiki_summary_min_chars: int
    # Phase 3.5 enrichment (ADR-0025/0027). Model defaults are config examples, not
    # normative — the contract is the tier -> model_ref indirection (provider:model_id).
    enrich_model_light: str
    enrich_model_standard: str
    enrich_model_heavy: str
    enrich_max_tokens: int
    enrich_local_base_url: str | None
    anthropic_api_key: str | None
    openai_api_key: str | None
    openai_base_url: str | None
    # Phase 4d vector embeddings (ADR-0033). Default local_http, loopback/LAN-only; cloud is an
    # explicit three-leg gate. embedding_model_ref is the staleness identity.
    embedding_provider: str
    embedding_base_url: str | None
    embedding_model_ref: str | None
    embedding_api_key: str | None
    embedding_allow_cloud: bool
    embedding_allow_model_mismatch: bool
    embedding_dimension: int
    embedding_distance_metric: str
    response_cache_path: Path
    app_host: str
    app_port: int
    app_name: str = "knowledge-system"
    app_version: str = "0.1.0"


def get_settings(root: Path | None = None) -> Settings:
    resolved = Path(root).resolve() if root else _resolve_root()
    file_env = _load_env_file(resolved)

    def cfg(key: str, default: str) -> str:
        return os.environ.get(key) or file_env.get(key) or default

    normalized = resolved / "normalized"
    return Settings(
        root=resolved,
        inbox_dir=resolved / "raw" / "inbox",
        manifests_dir=resolved / "raw" / "manifests",
        db_dir=resolved / "db",
        jobs_db_path=resolved / "db" / "jobs.sqlite",
        graph_db_path=resolved / "db" / "graph.sqlite",
        keyword_index_path=resolved / "indexes" / "keyword" / "keyword.sqlite",
        retrieval_policy_path=resolved / "policies" / "retrieval.yaml",
        normalized_dir=normalized,
        markdown_dir=normalized / "markdown",
        chunks_dir=normalized / "chunks",
        tables_dir=normalized / "tables",
        extraction_logs_dir=normalized / "extraction_logs",
        extract_max_file_mb=int(cfg("EXTRACT_MAX_FILE_MB", "50")),
        extract_timeout_s=int(cfg("EXTRACT_TIMEOUT_S", "120")),
        chunk_target_chars=int(cfg("EXTRACT_CHUNK_TARGET_CHARS", "1000")),
        chunk_max_chars=int(cfg("EXTRACT_CHUNK_MAX_CHARS", "2000")),
        wiki_dir=resolved / "wiki",
        sources_dir=resolved / "wiki" / "Sources",
        templates_dir=resolved / "templates",
        wiki_summary_max_chars=int(cfg("WIKI_SUMMARY_MAX_CHARS", "320")),
        wiki_summary_min_chars=int(cfg("WIKI_SUMMARY_MIN_CHARS", "40")),
        enrich_model_light=cfg("ENRICH_MODEL_LIGHT", "anthropic:claude-haiku-4-5"),
        enrich_model_standard=cfg("ENRICH_MODEL_STANDARD", "anthropic:claude-sonnet-4-6"),
        enrich_model_heavy=cfg("ENRICH_MODEL_HEAVY", "anthropic:claude-opus-4-8"),
        enrich_max_tokens=int(cfg("ENRICH_MAX_TOKENS", "1024")),
        enrich_local_base_url=(cfg("ENRICH_LOCAL_BASE_URL", "") or None),
        anthropic_api_key=(cfg("ANTHROPIC_API_KEY", "") or None),
        openai_api_key=(cfg("OPENAI_API_KEY", "") or None),
        openai_base_url=(cfg("OPENAI_BASE_URL", "") or None),
        embedding_provider=cfg("EMBEDDING_PROVIDER", "local_http"),
        embedding_base_url=(cfg("EMBEDDING_BASE_URL", "") or None),
        embedding_model_ref=(cfg("EMBEDDING_MODEL_REF", "") or None),
        embedding_api_key=(cfg("EMBEDDING_API_KEY", "") or None),
        embedding_allow_cloud=cfg("EMBEDDING_ALLOW_CLOUD", "").lower() in {"1", "true", "yes", "on"},
        embedding_allow_model_mismatch=cfg("EMBEDDING_ALLOW_MODEL_MISMATCH", "").lower()
        in {"1", "true", "yes", "on"},
        embedding_dimension=int(cfg("EMBEDDING_DIMENSION", "1024")),
        embedding_distance_metric=cfg("EMBEDDING_DISTANCE_METRIC", "cosine"),
        response_cache_path=resolved / "db" / "llm_cache.sqlite",
        app_host=cfg("APP_HOST", "127.0.0.1"),
        app_port=int(cfg("APP_PORT", "18000")),
    )
