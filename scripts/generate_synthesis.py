#!/usr/bin/env python3
"""CLI: generate candidate cross-source syntheses (Phase 3.5c slice 2).

Usage:
    uv run python scripts/generate_synthesis.py            # synthesize eligible topics
    uv run python scripts/generate_synthesis.py --force    # re-synthesize all eligible topics

Preconditions: run scripts/extract_claims.py and scripts/extract_items.py first — a topic is
eligible only with ≥2 active claims from ≥2 independent sources mentioning an active knowledge
item, all read from db/graph.sqlite.

Uses the tier-3 (heavy) model_ref and provider credentials from config/.env. With no API key,
generation is skipped (recorded as a 'skipped' job) — but human review decisions are still
applied and ineligible topics' syntheses are still retracted. Writes candidate Synthesis pages
under wiki/Synthesis/ (status: candidate) and files `propose_synthesis` review items; a
synthesis is promoted to active only by an approved review (no recurrence). Never touches raw/.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import validate_frontmatter  # noqa: E402
import validate_graph  # noqa: E402
import validate_projection  # noqa: E402
import validate_wikilinks  # noqa: E402

from app.backend.config import get_settings
from app.llm.cache import ResponseCache
from app.llm.client import build_client
from app.workers import synthesis


def main(argv: list[str]) -> int:
    force = "--force" in argv
    settings = get_settings()
    client = build_client(settings, cache=ResponseCache(settings.response_cache_path))
    summary = synthesis.generate_syntheses(
        settings.root,
        client=client,
        model_ref=settings.enrich_model_heavy,
        manifests_dir=settings.manifests_dir,
        jobs_db=settings.jobs_db_path,
        markdown_dir=settings.markdown_dir,
        force=force,
    )

    print("Cross-source synthesis complete.")
    print(f"Model: {summary['model_ref']}  (job status: {summary['status']})")
    print(f"Eligible topics: {summary['eligible_topics']}  "
          f"(written: {summary['syntheses_written']}, skipped fresh: {summary['skipped_fresh']}, "
          f"skipped reviewed: {summary['skipped_reviewed']})")
    print(f"Stale approved syntheses (evidence changed; re-review or --force): {summary['stale_active']}")
    print(f"Retracted (topic no longer eligible): {summary['retracted']}")
    print(f"Reviews applied: {summary['promoted']} promoted, {summary['rejected']} rejected")
    print(f"Index rebuilt: {summary['index_rebuilt']}")
    print(f"Errors: {summary['errors']}")
    for err in summary["error_details"]:
        print(f"  - {err['topic']}: {err['error']}")
    print(f"Job: {summary['job_id']}")

    print("\nValidators:")
    root = str(settings.root)
    rcs = [
        validate_frontmatter.main([root]),
        validate_graph.main([root]),
        validate_wikilinks.main([root]),
        validate_projection.main([root]),
    ]
    return 0 if (not summary["errors"] and all(rc == 0 for rc in rcs)) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
