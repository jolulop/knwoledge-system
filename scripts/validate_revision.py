#!/usr/bin/env python3
"""Report recovered Apply-preview state (ADR-0068 decision 6). Never fails.

`db/revision.json` and `db/apply_authorizations.json` repair themselves: damaged contents are kept as
`db/revision.damaged-*.json` / `db/apply_authorizations.damaged-*.json` and replaced, which only makes
outstanding preview tokens stale or unknown. This lists those kept copies as warnings, so the event
stays visible, but always exits 0: a kept copy must never block Apply or normal work. Delete the
copies once inspected. Read-only (it never repairs anything itself).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import apply_authorization, revision


def main(argv: list[str]) -> int:
    root = Path(argv[0]).resolve() if argv else Path.cwd()
    kept = [*revision.damaged_copies(root), *apply_authorization.damaged_copies(root)]
    if not kept:
        print("Revision validation passed.")
        return 0
    print("Revision validation passed with warnings (outstanding preview tokens were refused):")
    for p in kept:
        print(f"  - warning: recovered from a damaged file; its bytes are kept in db/{p.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
