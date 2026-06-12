#!/usr/bin/env python3
"""Shared helpers for content-keyed source manifests.

Manifests at ``raw/manifests/<source_id>.json`` are the authoritative per-source
record (ADR-0008). Intake (Phase 1) creates them from inbox scans; extraction
(Phase 2) updates them with normalization state. This module centralizes the
identity, read, and write logic both stages share, so there is exactly one canonical
manifest writer (``save_manifest``) and one formatting convention.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CHUNK = 1 << 20  # 1 MiB streaming read for checksums


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def source_id_for(sha256: str) -> str:
    """Deterministic content-derived source id: src_<first 16 hex chars>."""
    return f"src_{sha256[:16]}"


def manifest_path(manifests_dir: Path, source_id: str) -> Path:
    return Path(manifests_dir) / f"{source_id}.json"


def load_manifest(manifests_dir: Path, source_id: str) -> dict[str, Any] | None:
    path = manifest_path(manifests_dir, source_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_manifests(manifests_dir: Path) -> list[dict[str, Any]]:
    manifests_dir = Path(manifests_dir)
    if not manifests_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(manifests_dir.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def save_manifest(manifests_dir: Path, manifest: dict[str, Any]) -> Path:
    """Write a manifest with the canonical formatting.

    This is the single place manifests are written: 2-space indent, UTF-8, trailing
    newline. Both intake and extraction route writes through here.
    """
    manifests_dir = Path(manifests_dir)
    manifests_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_path(manifests_dir, manifest["source_id"])
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path
