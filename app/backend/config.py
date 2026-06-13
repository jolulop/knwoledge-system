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
        normalized_dir=normalized,
        markdown_dir=normalized / "markdown",
        chunks_dir=normalized / "chunks",
        tables_dir=normalized / "tables",
        extraction_logs_dir=normalized / "extraction_logs",
        extract_max_file_mb=int(cfg("EXTRACT_MAX_FILE_MB", "50")),
        extract_timeout_s=int(cfg("EXTRACT_TIMEOUT_S", "120")),
        chunk_target_chars=int(cfg("EXTRACT_CHUNK_TARGET_CHARS", "1000")),
        chunk_max_chars=int(cfg("EXTRACT_CHUNK_MAX_CHARS", "2000")),
        app_host=cfg("APP_HOST", "127.0.0.1"),
        app_port=int(cfg("APP_PORT", "18000")),
    )
