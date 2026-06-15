#!/usr/bin/env python3
"""CLI: promote candidate concepts/entities to active by source recurrence (Phase 3.5b).

Usage:
    uv run python scripts/promote.py

Deterministic maintenance (no LLM, no API key): promotes a candidate to `active` once >=2
mutually-independent sources mention it (independence from manifest provenance, ADR-0018),
updates the page + graph, and approves the promote_candidate_node review item. Safely
rerunnable after manual provenance edits or review decisions. Rebuilds wiki/index.md and runs
the frontmatter/graph/projection/wikilink validators afterward.
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
from app.workers import promote


def main(argv: list[str]) -> int:
    settings = get_settings()
    summary = promote.promote_candidates(
        settings.root,
        manifests_dir=settings.manifests_dir,
        wiki_dir=settings.wiki_dir,
        jobs_db=settings.jobs_db_path,
    )
    print("Promotion pass complete.")
    print(f"Candidates considered: {summary['candidates_considered']}")
    print(f"Promoted to active: {summary['promoted']}")
    print(f"Index rebuilt: {summary['index_rebuilt']}")
    print(f"Job: {summary['job_id']}")

    root = str(settings.root)
    print("\nValidators:")
    rcs = [
        validate_frontmatter.main([root]),
        validate_graph.main([root]),
        validate_projection.main([root]),
        validate_wikilinks.main([root]),
    ]
    return 0 if all(rc == 0 for rc in rcs) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
