#!/usr/bin/env python3
"""CLI: contradiction checks (ADR-0069).

Usage:
    uv run python scripts/detect_contradictions.py            # prints this usage; runs nothing
    uv run python scripts/detect_contradictions.py maintain   # model-free contradiction maintenance

The old no-argument run judged every candidate claim pair across the whole vault with one tier-3
call per pair; under ADR-0066's broad tags that no longer bounded the work (the part-3 UAT built
10,500 pairs). It was removed in slice 0, with no legacy flag. No path of this command judges the
whole vault.

`maintain` (slice 2(a)) makes no model call: it retires **proposed** contradictions whose claims lost
their evidence, whose wording or evidence changed since they were judged, or whose documents are no
longer independent. It never changes an approved (active) contradiction or a decision awaiting
Apply, and it does not check documents for contradictions. The same maintenance already runs,
targeted, after a Details save or clear and after claim re-extraction; `maintain` is the full repair
path that lint and the Source screen point to.

Scoped contradiction checks on documents you select arrive in slice 2(b):

    detect_contradictions.py estimate <sid> <sid> ...      # free estimate + token
    detect_contradictions.py run --token <id> [--allow-paid]

Existing contradiction review items are resolved by Apply (`/reviews/apply`,
`scripts/apply_reviews.py`). Exit status: 0 for `-h`/`--help` and a successful `maintain`; 1 when
`maintain` fails; 2 for no or unknown arguments (so automation that still calls the old run fails
visibly); 3 when another `detect_contradictions` run holds its guard.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

USAGE = """\
usage: detect_contradictions.py [-h] {maintain}

  maintain   retire proposed contradictions whose evidence or independence changed (no model call)

Whole-vault contradiction judging was removed (ADR-0069 slice 0). Scoped contradiction checks on
documents you select (estimate -> token -> run) arrive in ADR-0069 slice 2(b). Existing
contradiction review items are resolved by Apply (/reviews/apply or scripts/apply_reviews.py).
"""


def _maintain() -> int:
    from app.backend.config import get_settings
    from app.workers import contradictions
    settings = get_settings()
    try:
        summary = contradictions.maintain(settings.root, manifests_dir=settings.manifests_dir,
                                          jobs_db=settings.jobs_db_path,
                                          graph_db=settings.graph_db_path,
                                          wiki_dir=settings.wiki_dir,
                                          reviews_dir=settings.reviews_dir)
    except Exception as exc:
        print(f"Contradiction maintenance failed: {exc}", file=sys.stderr)
        return 1
    print(f"Job: {summary['job_id']} ({summary['status']})")
    print(f"Proposed contradictions checked: {summary['assertions_checked']}")
    print(f"Retired: {len(summary['retired'])}")
    for e in summary["retired"]:
        reason = contradictions.RETIRE_REASONS[e["reason"]]
        print(f"  {e['claim_a']} <-> {e['claim_b']}: {reason} ({e['review_id']} withdrawn)")
    if summary["awaiting_apply"]:
        print(f"Decided, awaiting Apply (left as they are): {summary['awaiting_apply']}")
    if summary["unverifiable"]:
        print(f"Couldn't be checked (left as they are): {len(summary['unverifiable'])}")
        for e in summary["unverifiable"]:
            reason = contradictions.UNVERIFIABLE_REASONS[e["reason"]]
            print(f"  {e['claim_a']} <-> {e['claim_b']}: {reason}")
    print("Maintenance is current. This does not check documents for contradictions.")
    return 0


def main(argv: list[str]) -> int:
    if any(a in ("-h", "--help") for a in argv):
        print(USAGE, end="")
        return 0
    if argv == ["maintain"]:
        return _maintain()
    print(USAGE, end="", file=sys.stderr)
    return 2


if __name__ == "__main__":
    if sys.argv[1:] == ["maintain"]:
        from app.backend import run_guard  # noqa: E402 - one run at a time (ADR-0068)
        from app.backend.config import get_settings  # noqa: E402
        raise SystemExit(run_guard.run_cli(get_settings().db_dir, "detect_contradictions",
                                           lambda: main(sys.argv[1:])))
    raise SystemExit(main(sys.argv[1:]))
