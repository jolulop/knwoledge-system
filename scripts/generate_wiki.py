#!/usr/bin/env python3
"""CLI: generate deterministic wiki Source pages from the normalized layer (Phase 3).

Usage:
    uv run python scripts/generate_wiki.py             # generate pages for pending sources
    uv run python scripts/generate_wiki.py --force     # regenerate all
    uv run python scripts/generate_wiki.py <source_id> # generate one source (repeatable)

Offline and deterministic; no API keys. Writes only under wiki/ and db/jobs.sqlite.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend.config import get_settings
from app.workers import wiki


def main(argv: list[str]) -> int:
    force = "--force" in argv
    source_ids = [a for a in argv if not a.startswith("--")]

    settings = get_settings()
    summary = wiki.generate_wiki(
        settings.root,
        source_ids=source_ids or None,
        force=force,
        manifests_dir=settings.manifests_dir,
        jobs_db=settings.jobs_db_path,
        wiki_dir=settings.wiki_dir,
        templates_dir=settings.templates_dir,
        markdown_dir=settings.markdown_dir,
        summary_max=settings.wiki_summary_max_chars,
        summary_min=settings.wiki_summary_min_chars,
    )

    print("Wiki generation complete.")
    print(f"Sources considered: {summary['sources_considered']}")
    print(f"Generated: {summary['generated']}")
    print(f"Skipped (unchanged): {summary['skipped_unchanged']}")
    print(f"Skipped (not extracted): {summary['skipped_not_extracted']}")
    print(f"Errors: {summary['errors']}")
    if summary["error_details"]:
        for err in summary["error_details"]:
            print(f"  - {err['source_id']}: {err['error']}")
    print(f"Job: {summary['job_id']}")
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
