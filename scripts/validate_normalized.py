#!/usr/bin/env python3
"""Validate the Phase 2 normalized layer against its manifests.

For every source whose manifest reports an extracted/partial ``ingestion_status`` this
checks that the normalized artifacts exist and that every citation anchor is mechanical
and resolvable (ADR-0012): chunk char offsets are in bounds and slice back to the
chunk's own text, chunk ids and ordinals are well-formed and contiguous, table chunks
reference real CSV files, and non-paginated sources carry no page numbers (no estimated
pages). Manifests are local runtime state (gitignored): if none claim extraction there
is simply nothing to validate, which is a pass, not an error.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_EXTRACTED = {"extracted", "partial"}


def _load_chunks(path: Path) -> list[dict] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            return None
    return out


def _check_source(root: Path, manifest: dict) -> list[str]:
    sid = manifest.get("source_id", "<unknown>")
    errors: list[str] = []
    normalized = manifest.get("normalized") or {}
    md_rel = normalized.get("markdown_path")
    chunks_rel = normalized.get("chunks_path")
    log_rel = normalized.get("extraction_log_path")
    tables_rel = normalized.get("tables_dir")
    if not (md_rel and chunks_rel and log_rel and tables_rel):
        return [f"{sid}: extracted manifest missing normalized.* paths"]

    md_path = root / md_rel
    chunks_path = root / chunks_rel
    if not (root / log_rel).exists():
        errors.append(f"{sid}: missing extraction log {log_rel}")
    if not (root / tables_rel).is_dir():
        errors.append(f"{sid}: missing tables dir {tables_rel}")
    if not md_path.exists():
        errors.append(f"{sid}: missing normalized markdown {md_rel}")
    if not chunks_path.exists():
        errors.append(f"{sid}: missing chunks file {chunks_rel}")
    if not md_path.exists() or not chunks_path.exists():
        return errors

    markdown = md_path.read_text(encoding="utf-8")
    chunks = _load_chunks(chunks_path)
    if chunks is None:
        return [f"{sid}: chunks file is not valid JSONL"]

    declared = manifest.get("chunk_count")
    if declared is not None and declared != len(chunks):
        errors.append(f"{sid}: manifest chunk_count {declared} != {len(chunks)} chunks on disk")

    paginated = manifest.get("page_count") is not None
    seen_ids: set[str] = set()
    for i, chunk in enumerate(chunks):
        cid = chunk.get("chunk_id", f"<{i}>")
        if chunk.get("source_id") != sid:
            errors.append(f"{cid}: source_id mismatch")
        if chunk.get("ordinal") != i:
            errors.append(f"{cid}: ordinal {chunk.get('ordinal')} != position {i}")
        if cid != f"{sid}::{i:04d}":
            errors.append(f"{cid}: malformed chunk_id (expected {sid}::{i:04d})")
        if cid in seen_ids:
            errors.append(f"{cid}: duplicate chunk_id")
        seen_ids.add(cid)

        start, end = chunk.get("char_start"), chunk.get("char_end")
        if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(markdown)):
            errors.append(f"{cid}: char anchor [{start}, {end}] out of bounds (len {len(markdown)})")
        elif markdown[start:end] != chunk.get("text"):
            errors.append(f"{cid}: text does not match markdown[{start}:{end}]")

        if chunk.get("kind") == "table":
            ref = chunk.get("table_reference")
            if not ref:
                errors.append(f"{cid}: table chunk missing table_reference")
            elif not (root / ref).exists():
                errors.append(f"{cid}: table_reference points at missing file {ref}")
        elif chunk.get("table_reference") is not None:
            errors.append(f"{cid}: non-table chunk has a table_reference")

        page, page_end = chunk.get("page"), chunk.get("page_end")
        if paginated:
            page_count = manifest["page_count"]
            for label, value in (("page", page), ("page_end", page_end)):
                if value is not None and not (1 <= value <= page_count):
                    errors.append(f"{cid}: {label} {value} out of [1, {page_count}]")
            if page is not None and page_end is not None and page > page_end:
                errors.append(f"{cid}: page {page} > page_end {page_end}")
        elif page is not None or page_end is not None:
            errors.append(f"{cid}: non-paginated source carries a page number (estimated?)")
    return errors


def _check_orphans(root: Path, status_by_id: dict[str, str]) -> list[str]:
    """Flag normalized outputs with no manifest, or for a non-extracted manifest.

    These are stale leftovers (e.g. a manifest reset to ``new``/``error``, or files
    left behind when a source was removed) that would otherwise let retrieval cite
    evidence the manifest no longer vouches for.
    """
    errors: list[str] = []
    targets = [
        (root / "normalized" / "markdown", "*.md", "markdown"),
        (root / "normalized" / "chunks", "src_*.jsonl", "chunks"),
    ]
    for directory, pattern, label in targets:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob(pattern)):
            sid = path.stem
            status = status_by_id.get(sid)
            if status is None:
                errors.append(f"{sid}: orphan normalized {label} with no manifest ({path.name})")
            elif status not in _EXTRACTED:
                errors.append(
                    f"{sid}: stale normalized {label} for {status!r} manifest ({path.name})"
                )
    return errors


def main(argv: list[str]) -> int:
    root = Path(argv[0]).resolve() if argv else Path.cwd()
    manifests_dir = root / "raw" / "manifests"
    if not manifests_dir.exists():
        print("Normalized validation passed (no manifests).")
        return 0

    errors: list[str] = []
    checked = 0
    status_by_id: dict[str, str] = {}
    for path in sorted(manifests_dir.glob("*.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sid = manifest.get("source_id")
        if sid:
            status_by_id[sid] = manifest.get("ingestion_status")
        if manifest.get("ingestion_status") not in _EXTRACTED:
            continue
        checked += 1
        errors.extend(_check_source(root, manifest))

    errors.extend(_check_orphans(root, status_by_id))

    if errors:
        print("Normalized validation failed:")
        for err in errors:
            print(f"- {err}")
        return 1
    print(f"Normalized validation passed ({checked} extracted source(s) checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
