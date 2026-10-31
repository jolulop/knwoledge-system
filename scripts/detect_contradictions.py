#!/usr/bin/env python3
"""CLI: contradiction checks — whole-vault judging removed (ADR-0069 slice 0).

Usage:
    uv run python scripts/detect_contradictions.py          # prints this usage; runs nothing

The old no-argument run judged every candidate claim pair across the whole vault with one tier-3
call per pair; under ADR-0066's broad tags that no longer bounded the work (the part-3 UAT built
10,500 pairs). It is removed, with no legacy flag. Contradiction checks return as an explicit,
user-selected document scope in ADR-0069 slice 2:

    detect_contradictions.py maintain                      # slice 2(a): model-free maintenance
    detect_contradictions.py estimate <sid> <sid> ...      # slice 2(b): free estimate + token
    detect_contradictions.py run --token <id> [--allow-paid]

Until then this script makes no model call and changes nothing. Existing `resolve_contradiction`
review items are still resolved by Apply (`/reviews/apply`, `scripts/apply_reviews.py`).
Exit status: 0 for `-h`/`--help`, 2 otherwise (so automation that still calls it fails visibly).
"""
from __future__ import annotations

import sys

USAGE = """\
usage: detect_contradictions.py [-h]

Whole-vault contradiction judging was removed (ADR-0069 slice 0); this command runs nothing.
Scoped contradiction checks on documents you select (estimate -> token -> run) and model-free
contradiction maintenance arrive in ADR-0069 slice 2. Existing contradiction review items are
still resolved by Apply (/reviews/apply or scripts/apply_reviews.py).
"""


def main(argv: list[str]) -> int:
    help_asked = any(a in ("-h", "--help") for a in argv)
    print(USAGE, end="", file=sys.stdout if help_asked else sys.stderr)
    return 0 if help_asked else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
