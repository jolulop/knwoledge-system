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


def normalized_paths(source_id: str) -> dict[str, str]:
    """Repository-relative normalized-layer paths for a source (ADR-0011)."""
    return {
        "markdown_path": f"normalized/markdown/{source_id}.md",
        "chunks_path": f"normalized/chunks/{source_id}.jsonl",
        "tables_dir": f"normalized/tables/{source_id}",
        "extraction_log_path": f"normalized/extraction_logs/{source_id}.json",
    }


def apply_extraction_state(
    manifest: dict[str, Any],
    *,
    ingestion_status: str,
    extracted_at: str | None,
    extraction_tool: str | None,
    extraction_tool_version: str | None,
    text_char_count: int,
    chunk_count: int,
    page_count: int | None,
) -> None:
    """Set Phase 2 extraction fields on a manifest in place (Phase 2 Plan §4).

    Phase 1 fields (occurrences, sha256, retention_class, …) are left untouched;
    ``retention_class`` deliberately stays whatever intake set (``unknown`` in Phase 2).
    """
    manifest["ingestion_status"] = ingestion_status
    manifest["normalized"] = normalized_paths(manifest["source_id"])
    manifest["extracted_at"] = extracted_at
    manifest["extraction_tool"] = extraction_tool
    manifest["extraction_tool_version"] = extraction_tool_version
    manifest["text_char_count"] = text_char_count
    manifest["chunk_count"] = chunk_count
    manifest["page_count"] = page_count


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


# Optional source-provenance fields (manifest-owned) that the promotion lifecycle uses to
# judge source independence (ADR-0018, Phase 3.5b slice 5). All null/absent by default; not
# auto-derived — populated manually or by a future extractor. The graph never owns these.
PROVENANCE_FIELDS = ("author", "publisher", "report_family", "canonical_url")


def get_provenance(manifest: dict[str, Any]) -> dict[str, Any]:
    """Read a manifest's provenance sub-dict (missing keys -> absent)."""
    prov = manifest.get("provenance") or {}
    return {k: prov.get(k) for k in PROVENANCE_FIELDS if prov.get(k) is not None}


def _canon_provenance(key: str, value: Any) -> Any:
    """Canonicalize a provenance value before comparison so trivial variants don't read as
    independent — a conservative gate (ADR-0018). Text: casefold + whitespace-collapse;
    `canonical_url` also drops a #fragment and trailing slashes."""
    if not isinstance(value, str):
        return value
    v = " ".join(value.split()).strip().casefold()
    if key == "canonical_url":
        v = v.split("#", 1)[0].rstrip("/")
    return v


def independent_sources(p1: dict[str, Any], p2: dict[str, Any]) -> bool:
    """Two sources are independent iff >=1 *comparable* provenance key (known on both) differs
    and no comparable key is equal (ADR-0018). Non-comparable/unknown keys never prove
    independence, so the gate stays conservative until provenance is populated. The single home
    for the independence rule, shared by the promotion lifecycle and contradiction detection."""
    comparable = [k for k in PROVENANCE_FIELDS if p1.get(k) and p2.get(k)]
    return bool(comparable) and all(
        _canon_provenance(k, p1[k]) != _canon_provenance(k, p2[k]) for k in comparable
    )


def set_provenance(manifests_dir: Path, source_id: str, **fields: Any) -> dict[str, Any] | None:
    """Set provenance fields on a manifest (the single write path); returns it or None."""
    unknown = set(fields) - set(PROVENANCE_FIELDS)
    if unknown:
        raise ValueError(f"unknown provenance field(s) {sorted(unknown)}; allowed: {PROVENANCE_FIELDS}")
    manifest = load_manifest(manifests_dir, source_id)
    if manifest is None:
        return None
    prov = dict(manifest.get("provenance") or {})
    for key, value in fields.items():
        if value is None:
            prov.pop(key, None)   # explicit None clears the field back to unknown
        else:
            prov[key] = value
    manifest["provenance"] = prov
    save_manifest(manifests_dir, manifest)
    return manifest


# Source lifecycle status — the durable retrieval-visibility authority (ADR-0036 decision 13). The
# manifest carries it (default `active`); the Source page reads it and stays a pure projection, so a
# wiki regen preserves it. Distinct from `retention_class` (policy category). Matches graph NODE_STATUSES.
SOURCE_STATUSES = ("active", "stale_candidate", "deprecated_candidate", "archive_candidate",
                   "archived", "delete_candidate", "deleted")


def get_status(manifest: dict[str, Any]) -> str:
    """A manifest's source lifecycle status (default ``active`` when unset)."""
    status = manifest.get("status")
    return status if status in SOURCE_STATUSES else "active"


def set_status(manifests_dir: Path, source_id: str, status: str) -> dict[str, Any] | None:
    """Set a manifest's source lifecycle status (the single write path); return it or None.

    The only writer of `manifest["status"]` (ADR-0036). Raw bytes are never touched. Reversible:
    setting `active` un-archives. Validated against SOURCE_STATUSES.
    """
    if status not in SOURCE_STATUSES:
        raise ValueError(f"unknown source status {status!r}; allowed: {list(SOURCE_STATUSES)}")
    manifest = load_manifest(manifests_dir, source_id)
    if manifest is None:
        return None
    manifest["status"] = status
    save_manifest(manifests_dir, manifest)
    return manifest
