#!/usr/bin/env python3
"""CLI: extract and normalize catalogued raw sources (Phase 2).

Usage:
    uv run python scripts/extract_sources.py             # extract all pending sources
    uv run python scripts/extract_sources.py --force     # re-extract everything
    uv run python scripts/extract_sources.py <source_id> # extract one source (repeatable)

Requires the optional extraction dependencies: ``uv sync --extra extraction``. No raw
file is modified; writes go only to normalized/, raw/manifests/, and db/jobs.sqlite.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend.config import get_settings
from app.workers import extract


def main(argv: list[str]) -> int:
    force = "--force" in argv
    source_ids = [a for a in argv if not a.startswith("--")]

    settings = get_settings()
    summary = extract.extract_sources(
        settings.root,
        source_ids=source_ids or None,
        force=force,
        manifests_dir=settings.manifests_dir,
        jobs_db=settings.jobs_db_path,
        normalized_dir=settings.normalized_dir,
        max_file_mb=settings.extract_max_file_mb,
        timeout_s=settings.extract_timeout_s,
        target_chars=settings.chunk_target_chars,
        max_chars=settings.chunk_max_chars,
    )

    print("Extraction complete.")
    print(f"Sources considered: {summary['sources_considered']}")
    print(f"Extracted: {summary['extracted']}")
    print(f"Partial (needs_ocr): {summary['partial']}")
    print(f"Errors: {summary['errors']}")
    print(f"Skipped (unchanged): {summary['skipped_unchanged']}")
    print(f"Skipped (unsupported): {summary['skipped_unsupported']}")
    if summary["error_details"]:
        for err in summary["error_details"]:
            print(f"  - {err['source_id']}: {err['error']}")
    print(f"Job: {summary['job_id']}")
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
