#!/usr/bin/env python3
"""CLI: scan raw/inbox, write content-keyed manifests, record an intake job.

Usage:
    uv run python scripts/scan_inbox.py [PROJECT_ROOT]

PROJECT_ROOT defaults to KNOWLEDGE_SYSTEM_HOME (or the repository root). No raw file
is modified; the scan writes only under raw/manifests/ and db/jobs.sqlite.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend.config import get_settings
from app.workers import intake


def main() -> int:
    root_arg = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    settings = get_settings(root_arg)
    summary = intake.scan_inbox(
        settings.root,
        inbox=settings.inbox_dir,
        manifests_dir=settings.manifests_dir,
        jobs_db=settings.jobs_db_path,
    )

    print("Inbox scan complete.")
    print(f"Files found: {summary['files_found']}")
    print(f"New manifests: {summary['new_manifests']}")
    print(f"Updated manifests: {summary['updated_manifests']}")
    print(f"Duplicates: {summary['duplicates']}")
    if summary["skipped"]:
        print(f"Skipped (non-source files): {summary['skipped']}")
        if summary.get("skipped_assets"):
            print(f"  of which saved-page assets: {summary['skipped_assets']}")
    print(f"Errors: {summary['errors']}")
    if summary["warnings"]:
        print(f"Warnings: {len(summary['warnings'])}")
    print(f"Job: {summary['job_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
