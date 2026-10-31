#!/usr/bin/env python3
"""CLI: extract grounded claims from sources into Claim pages + graph edges (Phase 3.5b).

Usage:
    uv run python scripts/extract_claims.py                # pending sources
    uv run python scripts/extract_claims.py --force        # re-extract all
    uv run python scripts/extract_claims.py <source_id>    # one source (repeatable)

Claim pages link to Source pages (`[[Sources/...]]`); extraction publishes each Source page
(ADR-0068), so they exist once a document is extracted.

Uses the tier-2 (standard) model_ref and provider credentials from config/.env. With no API
key, extraction is skipped (recorded as a 'skipped' job) and no claims are written. Writes
Claim pages under wiki/Claims/, claim artifacts under normalized/enrichment/, and
derived_from edges into db/graph.sqlite; publishes each source's Source page in its commit and
ends with one refresh of wiki/index.md and the keyword index (ADR-0068 decision 5), then runs the
citation and graph validators (AGENTS / Build Spec §16). Exit 1 on errors, a failed validator or an
incomplete publication. Never touches raw/.
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

import validate_citations  # noqa: E402
import validate_frontmatter  # noqa: E402
import validate_graph  # noqa: E402
import validate_projection  # noqa: E402
import validate_wikilinks  # noqa: E402

from app.backend.config import get_settings
from app.llm.cache import ResponseCache
from app.llm.client import build_client
from app.llm.usage import report_lines
from app.workers import claims, publication


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
        chunks_dir=settings.chunks_dir,
        window_chars=settings.enrich_claim_window_chars,
    )

    print("Claim extraction complete.")
    print(f"Model: {summary['model_ref']}  (job status: {summary['status']})")
    print(f"Sources considered: {summary['sources_considered']}")
    print(f"Sources with claims: {summary['sources_with_claims']}")
    print(f"Claims written: {summary['claims_written']} "
          f"(pages: {summary['claim_pages_written']} active / "
          f"{summary['claim_pages_tombstoned']} tombstoned)")
    print(f"Claims dropped (ungroundable quote): {summary['claims_dropped_ungrounded']}")
    print(f"Claim windows: {summary['claim_windows']} ({summary['claim_window_strategy']}; "
          f"over-budget singletons: {summary['claim_window_over_budget']})")
    print(f"Staging: replacement_not_applied={summary['replacement_not_applied']} "
          f"stale_claim_layer_preserved={summary['stale_claim_layer_preserved']}")
    print(f"Skipped (fresh): {summary['skipped_fresh']}")
    print(f"Skipped (no API key): {summary['skipped_no_key']}")
    print(f"Errors: {summary['errors']}")
    for err in summary["error_details"]:
        print(f"  - {err['source_id']}: {err['error']}")
    print(f"Index rebuilt: {summary['index_rebuilt']}")
    # ADR-0068: each source's commit published its Source page (its Claims section); the run ended
    # with one refresh of index.md and search.
    for line in publication.report(summary["publication"]):
        print(line)
    if summary["unfinished_commits"]:
        print(f"Unfinished commits: {len(summary['unfinished_commits'])} "
              f"({', '.join(summary['unfinished_commits'])}) — the next run redoes them.")
    print(f"Job: {summary['job_id']}")
    for line in report_lines(settings.jobs_db_path, summary["job_id"]):  # ADR-0069
        print(line)

    # Wiki content changed -> run the citation/graph/wikilink validators (AGENTS / Build Spec).
    root = str(settings.root)
    print("\nValidators:")
    rcs = [
        validate_frontmatter.main([root]),
        validate_citations.main([root]),
        validate_graph.main([root]),
        validate_wikilinks.main([root]),
        validate_projection.main([root]),
    ]
    validators_ok = all(rc == 0 for rc in rcs)

    published = (summary["publication"] or {}).get("status") != publication.PENDING
    published = published and not summary["unfinished_commits"]
    return 0 if (not summary["errors"] and validators_ok and published) else 1


if __name__ == "__main__":
    from app.backend import run_guard  # noqa: E402 - one run at a time (ADR-0068)
    raise SystemExit(run_guard.run_cli(get_settings().db_dir, "extract_claims", lambda: main(sys.argv[1:])))
