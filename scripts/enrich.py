#!/usr/bin/env python3
"""CLI: enrich Source pages with an LLM summary + tags (Phase 3.5a).

Usage:
    uv run python scripts/enrich.py                # enrich pending sources
    uv run python scripts/enrich.py --force        # re-enrich all (busts the artifact cache)
    uv run python scripts/enrich.py <source_id>    # one source (repeatable)

Reads the tier-1 (light) model_ref and provider credentials from config/.env. With no API
key for the configured provider, enrichment is skipped and sources stay summary_status: stub.
Writes artifacts under normalized/enrichment/ and the response cache under db/; each source's
commit publishes its Source page, and the run ends with one refresh of wiki/index.md and the
keyword index (ADR-0068 decision 5; the old `--no-compose` switch is gone: a saved artifact is
always published). Exit 1 on errors or an incomplete publication. Never touches raw/.
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
from app.workers import enrich, publication


def main(argv: list[str]) -> int:
    force = "--force" in argv
    source_ids = [a for a in argv if not a.startswith("--")]

    settings = get_settings()
    cache = ResponseCache(settings.response_cache_path)
    client = build_client(settings, cache=cache)
    model_ref = settings.enrich_model_light

    summary = enrich.enrich_sources(
        settings.root,
        client=client,
        model_ref=model_ref,
        source_ids=source_ids or None,
        force=force,
        manifests_dir=settings.manifests_dir,
        jobs_db=settings.jobs_db_path,
        markdown_dir=settings.markdown_dir,
    )

    print("Enrichment complete.")
    print(f"Model: {summary['model_ref']}")
    print(f"Sources considered: {summary['sources_considered']}")
    print(f"Enriched: {summary['enriched']}")
    print(f"Skipped (fresh): {summary['skipped_fresh']}")
    print(f"Skipped (no API key): {summary['skipped_no_key']}")
    print(f"Skipped (empty): {summary['skipped_empty']}")
    print(f"Skipped (not extracted): {summary['skipped_not_extracted']}")
    print(f"Errors (dropped): {summary['errors']}")
    for err in summary["error_details"]:
        print(f"  - {err['source_id']}: {err['error']}")
    for line in publication.report(summary["publication"]):
        print(line)
    print(f"Job: {summary['job_id']}")
    published = (summary["publication"] or {}).get("status") != publication.PENDING
    return 1 if summary["errors"] or not published else 0


if __name__ == "__main__":
    from app.backend import run_guard  # noqa: E402 - one run at a time (ADR-0068)
    raise SystemExit(run_guard.run_cli(get_settings().db_dir, "enrich", lambda: main(sys.argv[1:])))
