#!/usr/bin/env python3
"""CLI: extract grounded claims from sources into Claim pages + graph edges (Phase 3.5b).

Usage:
    uv run python scripts/extract_claims.py                # pending sources
    uv run python scripts/extract_claims.py --force        # re-extract all
    uv run python scripts/extract_claims.py <source_id>    # one source (repeatable)

Uses the tier-2 (standard) model_ref and provider credentials from config/.env. With no API
key, extraction is skipped (recorded as a 'skipped' job) and no claims are written. Writes
Claim pages under wiki/Claims/, claim artifacts under normalized/enrichment/, and
derived_from edges into db/graph.sqlite; never touches raw/.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend.config import get_settings
from app.llm.cache import ResponseCache
from app.llm.client import build_client
from app.workers import claims


def main(argv: list[str]) -> int:
    force = "--force" in argv
    source_ids = [a for a in argv if not a.startswith("--")]

    settings = get_settings()
    client = build_client(settings, cache=ResponseCache(settings.response_cache_path))
    summary = claims.extract_claims(
        settings.root,
        client=client,
        model_ref=settings.enrich_model_standard,
        source_ids=source_ids or None,
        force=force,
        manifests_dir=settings.manifests_dir,
        jobs_db=settings.jobs_db_path,
        markdown_dir=settings.markdown_dir,
    )

    print("Claim extraction complete.")
    print(f"Model: {summary['model_ref']}  (job status: {summary['status']})")
    print(f"Sources considered: {summary['sources_considered']}")
    print(f"Sources with claims: {summary['sources_with_claims']}")
    print(f"Claims written: {summary['claims_written']} "
          f"({summary['claim_pages_written']} pages)")
    print(f"Claims dropped (unlocatable quote): {summary['claims_dropped']}")
    print(f"Skipped (fresh): {summary['skipped_fresh']}")
    print(f"Skipped (no API key): {summary['skipped_no_key']}")
    print(f"Errors: {summary['errors']}")
    for err in summary["error_details"]:
        print(f"  - {err['source_id']}: {err['error']}")
    print(f"Job: {summary['job_id']}")
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
