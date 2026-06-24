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
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CHUNK = 1 << 20  # 1 MiB streaming read for checksums

# Canonical content-derived source id (ADR-0007): src_<first 16 hex chars of SHA256>. Manifest JSON is
# durable *untrusted* local input (CLAUDE.md rule 2), so a hostile/hand-edited source_id (absolute path,
# `..`, slashes) must never reach the filesystem — every manifest path is gated on this shape. Mirrors
# `app/workers/citations.py:_SOURCE_ID`.
_SOURCE_ID_RE = re.compile(r"^src_[0-9a-f]{16}$")


def is_source_id(value: Any) -> bool:
    """True iff `value` is a canonical `src_<16 hex>` source id."""
    return isinstance(value, str) and bool(_SOURCE_ID_RE.match(value))


def _require_source_id(source_id: Any) -> str:
    """Raise on a non-canonical source id (the write-path guard)."""
    if not is_source_id(source_id):
        raise ValueError(f"invalid source_id {source_id!r}; expected src_<16 hex>")
    return source_id


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
    """The on-disk path for a manifest. Validated chokepoint: a non-canonical id raises (so nothing can
    build a manifest path that escapes the manifests dir, ADR-0009)."""
    _require_source_id(source_id)
    return Path(manifests_dir) / f"{source_id}.json"


def load_manifest(manifests_dir: Path, source_id: str) -> dict[str, Any] | None:
    # Read path is lenient: an invalid/unknown id is treated as "not found" (None), never a traversal.
    try:
        path = manifest_path(manifests_dir, source_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_manifests(manifests_dir: Path) -> list[dict[str, Any]]:
    """Every parseable manifest record, unfiltered. Validators use this + fail hard on a bad id;
    runtime workers should use `valid_manifests` so a tampered record can't drive a path."""
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


def valid_manifests(manifests_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Partition manifests into (canonical records, skipped reasons) — the runtime-worker entry point.

    A manifest's `source_id` flows into filesystem paths (Source pages, normalized files) and graph node
    ids, and manifest JSON is untrusted local input (AGENTS.md). A record is **valid** only when all hold:
    the `source_id` is canonical `src_<16 hex>`, the **filename stem equals it** (ADR-0007:
    `raw/manifests/<source_id>.json`), and the id has **not already been seen**. This blocks a tampered /
    misnamed / duplicate manifest from driving a worker for some id and clobbering that id's artifacts.

    `skipped` carries **categorical reasons only** — `"non_canonical_id"` | `"filename_mismatch"` |
    `"duplicate_source_id"` — never the malformed id text, so callers surface counts without echoing
    attacker-controlled input. Validators stay on `list_manifests` (all records) + fail hard."""
    manifests_dir = Path(manifests_dir)
    valid: list[dict[str, Any]] = []
    skipped: list[str] = []
    if not manifests_dir.exists():
        return valid, skipped
    records: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(manifests_dir.glob("*.json")):
        try:
            records.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError):
            continue
    # A canonical content source_id appearing in 2+ files is ambiguous authority — quarantine every copy
    # (order-independent), even the correctly-named one: with a tampered duplicate present, none is trusted.
    id_counts: dict[str, int] = {}
    for _p, rec in records:
        sid = rec.get("source_id")
        if is_source_id(sid):
            id_counts[sid] = id_counts.get(sid, 0) + 1
    for path, rec in records:
        sid = rec.get("source_id")
        if not is_source_id(sid):
            skipped.append("non_canonical_id")
        elif id_counts[sid] > 1:
            skipped.append("duplicate_source_id")
        elif path.stem != sid:
            skipped.append("filename_mismatch")
        else:
            valid.append(rec)
    return valid, skipped


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
    _require_source_id(source_id)  # write path: a non-canonical id raises
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
    _require_source_id(source_id)  # write path: a non-canonical id raises, never returns None
    if status not in SOURCE_STATUSES:
        raise ValueError(f"unknown source status {status!r}; allowed: {list(SOURCE_STATUSES)}")
    manifest = load_manifest(manifests_dir, source_id)
    if manifest is None:
        return None
    manifest["status"] = status
    save_manifest(manifests_dir, manifest)
    return manifest
