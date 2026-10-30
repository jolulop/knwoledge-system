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
_SOURCE_ID_RE = re.compile(r"src_[0-9a-f]{16}")


def is_source_id(value: Any) -> bool:
    """True iff `value` is a canonical `src_<16 hex>` source id.

    Uses `fullmatch` (NOT `match` + `^…$`, which accepts a trailing newline because `$` matches before
    it) — this gates untrusted ids before filesystem/graph use, so the whole string must be exact."""
    return isinstance(value, str) and bool(_SOURCE_ID_RE.fullmatch(value))


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


# The fields :func:`apply_extraction_state` owns. Extraction writes back only these, onto the
# manifest as it is on disk when it commits (ADR-0068: a status changed during the run survives).
EXTRACTION_FIELDS = ("ingestion_status", "normalized", "extracted_at", "extraction_tool",
                     "extraction_tool_version", "text_char_count", "chunk_count", "page_count")


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
# auto-derived — a human sets them (ADR-0066 part 3: the document screen's Details confirmation).
# The graph never owns these. ADR-0066 part 3 slice 3 adds the confirmed `title`, `authors` (an
# ordered list of {name, show_page}), `publisher` as {name, show_page} (a legacy string still reads),
# and the audit fields `confirmed_at` / `confirmed_baseline`; the legacy string `author` is read only
# when `authors` is absent.
PROVENANCE_FIELDS = ("title", "authors", "author", "publisher", "report_family", "canonical_url",
                     "confirmed_at", "confirmed_baseline")
# The Details a confirmation sets and a Clear removes (report_family / canonical_url are kept).
DETAILS_FIELDS = ("title", "authors", "author", "publisher", "confirmed_at", "confirmed_baseline")
_MAX_NAME = 200
_WS = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _clean(value: Any, max_chars: int = _MAX_NAME) -> str:
    if not isinstance(value, str):
        return ""
    import unicodedata
    text = _WS.sub(" ", _CONTROL.sub(" ", unicodedata.normalize("NFKC", value))).strip()
    return text[:max_chars].strip()


def name_identity(name: Any) -> str:
    """The normalized identity of a provenance name (ADR-0066 part 3 grill 2 Q3): NFKC, control
    characters and whitespace normalized, casefold. Not `merge_key` (no singularization); accents
    and non-Latin characters stay. Shared by independence, its digest and the Authors pages."""
    return _clean(name).casefold()


def get_provenance(manifest: dict[str, Any]) -> dict[str, Any]:
    """Read a manifest's provenance sub-dict (missing keys -> absent)."""
    prov = manifest.get("provenance") or {}
    return {k: prov.get(k) for k in PROVENANCE_FIELDS if prov.get(k) is not None}


def confirmed_details(prov: dict[str, Any]) -> dict[str, Any]:
    """The confirmed Details in their current shape, legacy values read (never rewritten):
    `{title, authors: [{name, show_page}], publisher: {name, show_page} | None, confirmed_at}`.
    `authors` wins whenever present, even empty; a legacy string `author` reads as one name."""
    if "authors" in prov:
        authors = [{"name": a["name"], "show_page": bool(a.get("show_page"))}
                   for a in prov.get("authors") or [] if isinstance(a, dict) and a.get("name")]
    elif isinstance(prov.get("author"), str) and prov["author"].strip():
        authors = [{"name": prov["author"].strip(), "show_page": False}]
    else:
        authors = []
    pub = prov.get("publisher")
    if isinstance(pub, dict) and pub.get("name"):
        publisher = {"name": pub["name"], "show_page": bool(pub.get("show_page"))}
    elif isinstance(pub, str) and pub.strip():
        publisher = {"name": pub.strip(), "show_page": False}
    else:
        publisher = None
    return {"title": prov.get("title") if isinstance(prov.get("title"), str) else None,
            "authors": authors, "publisher": publisher, "confirmed_at": prov.get("confirmed_at")}


def _canon_url(value: str) -> str:
    return " ".join(value.split()).strip().casefold().split("#", 1)[0].rstrip("/")


def independence_inputs(prov: dict[str, Any]) -> dict[str, Any]:
    """The one normalization of a source's independence inputs (ADR-0018; ADR-0066 part 3 slice 3):
    the author identity set, the publisher identity, `report_family` and `canonical_url`. Order,
    spelling variants covered by `name_identity`, `show_page`, the title and the confirmation audit
    fields never change it. Empty values are omitted."""
    details = confirmed_details(prov)
    out: dict[str, Any] = {}
    authors = sorted({name_identity(a["name"]) for a in details["authors"]} - {""})
    if authors:
        out["authors"] = authors
    if details["publisher"] and name_identity(details["publisher"]["name"]):
        out["publisher"] = name_identity(details["publisher"]["name"])
    family = _clean(prov.get("report_family")).casefold()
    if family:
        out["report_family"] = family
    url = _canon_url(prov["canonical_url"]) if isinstance(prov.get("canonical_url"), str) else ""
    if url:
        out["canonical_url"] = url
    return out


def independence_digest(prov: dict[str, Any]) -> str:
    """A digest of :func:`independence_inputs` (reconciliation notices, ADR-0066 part 3 slice 3)."""
    data = json.dumps(independence_inputs(prov), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def independent_sources(p1: dict[str, Any], p2: dict[str, Any]) -> bool:
    """Two sources are independent iff >=1 *comparable* independence input (known on both) differs
    and no comparable input is shared (ADR-0018). Authors compare as identity sets: **any shared
    author** is shared provenance ({Alice, Bob} vs {Alice, Carol} are not independent), and only
    disjoint sets differ. Non-comparable/unknown inputs never prove independence, so the gate stays
    conservative until provenance is populated. The single home for the independence rule, shared
    by the promotion lifecycle, contradiction detection and synthesis."""
    a, b = independence_inputs(p1), independence_inputs(p2)
    comparable = [k for k in a if k in b]
    if not comparable:
        return False
    for key in comparable:
        if key == "authors":
            if set(a[key]) & set(b[key]):
                return False
        elif a[key] == b[key]:
            return False
    return True


def _check_field(key: str, value: Any) -> Any:
    """Validate (and clean) one provenance value; raise ValueError on malformed input."""
    if key == "title":
        text = _clean(value, 300)
        if not text:
            raise ValueError("a confirmed title must not be empty")
        return text
    if key == "authors":
        if not isinstance(value, list):
            raise ValueError("authors must be a list")
        out, seen = [], set()
        for a in value:
            if not isinstance(a, dict) or set(a) != {"name", "show_page"}:
                raise ValueError("each author is {name, show_page}")
            if not isinstance(a["show_page"], bool):
                raise ValueError("show_page must be a boolean")
            name = _clean(a["name"])
            if not name:
                raise ValueError("an author name must not be empty")
            if name_identity(name) in seen:
                raise ValueError(f"duplicate author: {name}")
            seen.add(name_identity(name))
            out.append({"name": name, "show_page": a["show_page"]})
        return out
    if key == "publisher":
        if isinstance(value, str):                 # the legacy shape, still accepted
            text = _clean(value)
            if not text:
                raise ValueError("a publisher name must not be empty")
            return text
        if not isinstance(value, dict) or set(value) != {"name", "show_page"}:
            raise ValueError("publisher is {name, show_page}")
        if not isinstance(value["show_page"], bool):
            raise ValueError("show_page must be a boolean")
        name = _clean(value["name"])
        if not name:
            raise ValueError("a publisher name must not be empty")
        return {"name": name, "show_page": value["show_page"]}
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def set_provenance(manifests_dir: Path, source_id: str, **fields: Any) -> dict[str, Any] | None:
    """Set provenance fields on a manifest (the single write path); returns it or None.

    Values are validated and cleaned (`_check_field`); malformed input raises ValueError and
    writes nothing. An explicit None clears a field back to unknown, which is distinct from
    malformed input. The caller holds the write lock (a screen action's commit phase)."""
    _require_source_id(source_id)  # write path: a non-canonical id raises
    unknown = set(fields) - set(PROVENANCE_FIELDS)
    if unknown:
        raise ValueError(f"unknown provenance field(s) {sorted(unknown)}; allowed: {PROVENANCE_FIELDS}")
    checked = {k: (None if v is None else _check_field(k, v)) for k, v in fields.items()}
    manifest = load_manifest(manifests_dir, source_id)
    if manifest is None:
        return None
    prov = dict(manifest.get("provenance") or {})
    for key, value in checked.items():
        if value is None:
            prov.pop(key, None)   # explicit None clears the field back to unknown
        else:
            prov[key] = value
    manifest["provenance"] = prov
    save_manifest(manifests_dir, manifest)
    return manifest


def confirm_details(manifests_dir: Path, source_id: str, *, title: str, authors: list[dict[str, Any]],
                    publisher: dict[str, Any] | None, baseline: str, now: str) -> dict[str, Any] | None:
    """Record the human-confirmed Details (ADR-0066 part 3 slice 3): writes the new keys and
    removes the legacy `author`; `report_family` / `canonical_url` are untouched."""
    return set_provenance(manifests_dir, source_id, title=title, authors=authors,
                          publisher=publisher, author=None, confirmed_at=now,
                          confirmed_baseline=baseline)


def clear_details(manifests_dir: Path, source_id: str) -> dict[str, Any] | None:
    """Clear confirmed Details: title, authors, publisher, the legacy author and the audit fields.
    `report_family`, `canonical_url` and every other field stay, so independence may remain
    established through them."""
    return set_provenance(manifests_dir, source_id, **{k: None for k in DETAILS_FIELDS})


# Source lifecycle status — the durable retrieval-visibility authority (ADR-0036 decision 13). The
# manifest carries it (default `active`); the Source page reads it and stays a pure projection, so a
# wiki regen preserves it. Distinct from `retention_class` (policy category). Matches graph NODE_STATUSES.
SOURCE_STATUSES = ("active", "stale_candidate", "deprecated_candidate", "archive_candidate",
                   "archived", "delete_candidate", "deleted", "hidden")


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
