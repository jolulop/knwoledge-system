#!/usr/bin/env python3
"""CLI: extract candidate knowledge items into pages + graph mentions (ADR-0059).

Usage:
    uv run python scripts/extract_items.py                # pending sources
    uv run python scripts/extract_items.py --force        # re-extract all
    uv run python scripts/extract_items.py <source_id>    # one source (repeatable)
    uv run python scripts/extract_items.py --force --cache-only <source_id>…
                                   # ADR-0066 Re-suggest: replay stored responses only — zero
                                   # provider calls; a cache miss leaves the document untouched.
                                   # Also re-cuts at a changed ENRICH_ITEMS_CAP (ADR-0066 P1).

Precondition: run scripts/generate_wiki.py first — item pages link to Source pages
and the Source pages project these mentions, so the Source pages must already exist.

Uses the tier-2 (standard) model_ref and provider credentials from config/.env. With no API
key, extraction is skipped (recorded as a 'skipped' job) and no nodes are written. Writes
candidate item pages under wiki/Items/, items artifacts under normalized/enrichment/, and
active mentions edges into db/graph.sqlite; rebuilds wiki/index.md, refreshes Source pages
via generate_wiki, and runs the validators. Never touches raw/.
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
from app.workers import items, wiki


def main(argv: list[str]) -> int:
    force = "--force" in argv
    cache_only = "--cache-only" in argv
    source_ids = [a for a in argv if not a.startswith("--")]

    settings = get_settings()
    client = build_client(settings, cache=ResponseCache(settings.response_cache_path))
    summary = items.extract_items(
        settings.root,
        client=client,
        model_ref=settings.enrich_model_standard,
        source_ids=source_ids or None,
        force=force,
        manifests_dir=settings.manifests_dir,
        jobs_db=settings.jobs_db_path,
        markdown_dir=settings.markdown_dir,
        input_max_chars=settings.enrich_items_input_max_chars,
        cache_only=cache_only,
        cap=settings.enrich_items_cap,
    )

    print("Knowledge-item extraction complete.")
    print(f"Model: {summary['model_ref']}  (job status: {summary['status']})")
    print(f"Sources considered: {summary['sources_considered']}")
    print(f"Nodes written: {summary['nodes_written']}  mentions: {summary['mentions_written']}")
    print(f"Node pages: {summary['node_pages_written']} active / "
          f"{summary['node_pages_tombstoned']} tombstoned")
    print(f"Skipped (fresh): {summary['skipped_fresh']}")
    print(f"Skipped (no API key): {summary['skipped_no_key']}")
    print(f"Topic-starved sources: {summary['topic_starved']}")  # ADR-0059 starvation signature
    for sid in summary["topic_starved_sources"]:
        print(f"  - {sid}: no thematic topic layer despite named items/claims present")
    print(f"Unclassified items (QA sentinel): {summary['unclassified_items']}")
    # ADR-0066 part 2: the per-document cap cut, key resolution and the alias check.
    print(f"Suggestion cap: {summary['suggestion_cap']} per document "
          f"(cut: {summary['suggestions_over_cap']})")
    print(f"Resolved to an existing tag: {summary['suggestions_resolved']}  "
          f"ambiguous (choose on the document screen): {summary['suggestions_ambiguous']}")
    print(f"Followed a merge to its surviving tag: {summary['suggestions_redirected']}")
    print(f"Filtered: dropped here {summary['suggestions_filtered_dropped']}, "
          f"blocked {summary['suggestions_filtered_blocked']}, "
          f"hidden tag {summary['suggestions_filtered_hidden']}, "
          f"retired tag {summary['suggestions_filtered_retired']}; "
          f"aliases not in the body text: {summary['aliases_filtered']}")
    if summary["aliases_withheld_ledger_damaged"]:
        print(f"Aliases NOT added to {summary['aliases_withheld_ledger_damaged']} suggested tag(s): a "
              "damaged tag ledger may keep them — restore it, then re-run")
    print(f"Documents with no domain suggestion: {summary['no_domain']}")
    for sid in summary["no_domain_sources"]:
        print(f"  - {sid}")
    print(f"Coverage-truncated sources: {summary['coverage_truncated']}")  # ADR-0056 cap marker
    for sid in summary["coverage_truncated_sources"]:
        print(f"  - {sid}: document exceeds ENRICH_ITEMS_INPUT_MAX_CHARS, head-truncated")
    if cache_only:
        print(f"Cache misses (left untouched, no provider call): {summary['cache_misses']}")
        for sid in summary["cache_miss_sources"]:
            print(f"  - {sid}")
    for row in summary["in_flight_sources"]:
        print(f"  skipped {row['source_id']}: merge/split {row['review_id']} is unfinished — "
              "run /reviews/apply, then re-run this")
    print(f"Errors: {summary['errors']}")
    for err in summary["error_details"]:
        print(f"  - {err['source_id']}: {err['error']}")
    print(f"Job: {summary['job_id']}")

    # Refresh Source pages so their Items section reflects the new mentions.
    gen = wiki.generate_wiki(
        settings.root,
        manifests_dir=settings.manifests_dir,
        jobs_db=settings.jobs_db_path,
        wiki_dir=settings.wiki_dir,
        templates_dir=settings.templates_dir,
        markdown_dir=settings.markdown_dir,
        summary_max=settings.wiki_summary_max_chars,
        summary_min=settings.wiki_summary_min_chars,
    )
    print(f"Source pages refreshed: generated {gen['generated']}, "
          f"skipped_unchanged {gen['skipped_unchanged']}.")

    root = str(settings.root)
    print("\nValidators:")
    rcs = [
        validate_frontmatter.main([root]),
        validate_citations.main([root]),
        validate_graph.main([root]),
        validate_wikilinks.main([root]),
        validate_projection.main([root]),
    ]
    return 0 if (not summary["errors"] and not summary["cache_misses"] and not summary["in_flight_sources"]
                 and all(rc == 0 for rc in rcs)) else 1


if __name__ == "__main__":
    from app.backend import run_guard  # noqa: E402 - one run at a time (ADR-0068)
    raise SystemExit(run_guard.run_cli(get_settings().db_dir, "extract_items", lambda: main(sys.argv[1:])))
