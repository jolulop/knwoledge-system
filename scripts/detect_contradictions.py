#!/usr/bin/env python3
"""CLI: detect contradictions across independent sources (Phase 3.5c slice 1).

Usage:
    uv run python scripts/detect_contradictions.py        # detect over the current graph

Preconditions: run scripts/extract_claims.py and scripts/extract_concepts.py first — detection
blocks candidate claim pairs on the graph's `derived_from` (claim→source) and `mentions`
(source→concept) edges, so claims and concepts/entities must already be in db/graph.sqlite.

Uses the tier-3 (heavy) model_ref and provider credentials from config/.env. With no API key,
detection is skipped (recorded as a 'skipped' job) — but human review decisions are still
applied to the graph and stale contradiction assertions are still superseded. Proposes
`contradicts` edges (status=proposed) and files `resolve_contradiction` review items; it never
activates a contradiction without human approval. Never touches raw/.
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

import validate_graph  # noqa: E402
import validate_projection  # noqa: E402
import validate_wikilinks  # noqa: E402

from app.backend.config import get_settings
from app.llm.cache import ResponseCache
from app.llm.client import build_client
from app.workers import contradictions


def main(argv: list[str]) -> int:
    settings = get_settings()
    client = build_client(settings, cache=ResponseCache(settings.response_cache_path))
    summary = contradictions.detect_contradictions(
        settings.root,
        client=client,
        model_ref=settings.enrich_model_heavy,
        manifests_dir=settings.manifests_dir,
        jobs_db=settings.jobs_db_path,
        markdown_dir=settings.markdown_dir,
    )

    print("Contradiction detection complete.")
    print(f"Model: {summary['model_ref']}  (job status: {summary['status']})")
    print(f"Candidate pairs: {summary['candidate_pairs']}  "
          f"(evaluated: {summary['pairs_evaluated']}, human-decided skipped: "
          f"{summary['skipped_human_decided']})")
    print(f"Contradictions proposed: {summary['contradictions_proposed']}  "
          f"(not a contradiction: {summary['not_contradiction']})")
    print(f"Stale assertions superseded: {summary['superseded_stale']}")
    print(f"Resolutions applied: {summary['resolutions_acknowledged']} acknowledged, "
          f"{summary['resolutions_rejected']} rejected, {summary['supersede_executed']} superseded "
          f"(claim pages re-projected: {summary['claim_pages_reprojected']})")
    print(f"Index rebuilt: {summary['index_rebuilt']}")
    print(f"Errors: {summary['errors']}")
    for err in summary["error_details"]:
        print(f"  - {err['pair']}: {err['error']}")
    print(f"Job: {summary['job_id']}")

    print("\nValidators:")
    root = str(settings.root)
    rcs = [
        validate_graph.main([root]),
        validate_wikilinks.main([root]),
        validate_projection.main([root]),
    ]
    return 0 if (not summary["errors"] and all(rc == 0 for rc in rcs)) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
