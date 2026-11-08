#!/usr/bin/env python3
"""Inspect an unfinished supersede, and withdraw it as a human choice (ADR-0073 decisions 11, 18–23).

Usage:
    uv run python scripts/repair_supersede.py inspect <review_id> [--root ROOT] [--json]
    uv run python scripts/repair_supersede.py rollback-edge <review_id> --expected-state <digest>
        --reason "<why>" [--root ROOT]

`inspect` is read-only: it takes no lock, changes no revision and creates nothing. It prints what it
found, the state fingerprint, and a write command only when that command's preconditions hold;
otherwise "No safe repair determined", with what is uncertain. It exits 0 whenever it produced its
report.

`rollback-edge` withdraws only this review's own `supersedes` link and consumes this approval, as
the same rollback Apply runs. It needs an open, readable journal bound to the current approval, an
active loser, exactly one owned active link, and no completion evidence of any approval generation.
It never publishes and never closes the operation: the next Apply finishes the recovery and closes
it once publication completes, and reopen is available after that. There is no "complete" command,
and nothing is written for a supersede with no journal.

Exit codes:
  0  report produced / rollback recorded
  1  a refused write (a safety check, no safe repair) or an operational failure
  2  usage
  3  Apply, claim extraction, contradiction detection, republish or review reconciliation is running
  4  the publication queue is unreadable (`scripts/republish.py --repair`)
  6  state changed since it was inspected: inspect again
  9  the vault revision can't be advanced
Never touches raw/.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.workers import supersede_repair as sr  # noqa: E402

_RID = re.compile(r"rev_[0-9a-f]{16}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="repair_supersede.py",
                                description="Inspect or withdraw an unfinished supersede.")
    sub = p.add_subparsers(dest="command", required=True)
    ins = sub.add_parser("inspect", help="read-only diagnostic report")
    ins.add_argument("review_id")
    ins.add_argument("--root", default=".")
    ins.add_argument("--json", action="store_true")
    rb = sub.add_parser("rollback-edge", help="withdraw this review's own supersede link")
    rb.add_argument("review_id")
    rb.add_argument("--expected-state", required=True)
    rb.add_argument("--reason", required=True)
    rb.add_argument("--root", default=".")
    return p


def main(argv: list[str]) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:                       # argparse: --help is 0, a usage error 2
        return int(exc.code or 0)
    if not _RID.fullmatch(args.review_id):
        print("Usage error: the review id must look like rev_<16 hex>.", file=sys.stderr)
        return sr.EXIT_USAGE
    root = Path(args.root).resolve()
    if args.command == "inspect":
        report = sr.inspect(root, args.review_id)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
            return sr.EXIT_OK
        print(f"Supersede review {report['review_id']}")
        for key, value in report["facts"].items():
            print(f"  {key}: {value}")
        print()
        for line in report["findings"]:
            print(line)
        print(f"\nState fingerprint: {report['fingerprint']}")
        if report["command"]:
            print(f"Offered (replace <why>):\n  {report['command']}")
        return sr.EXIT_OK
    if not _DIGEST.fullmatch(args.expected_state):
        print("Usage error: --expected-state must be the 64-hex fingerprint `inspect` printed.",
              file=sys.stderr)
        return sr.EXIT_USAGE
    out = sr.rollback_edge(root, args.review_id, expected_state=args.expected_state,
                           reason=args.reason)
    print(out["message"], file=sys.stdout if out["exit"] == sr.EXIT_OK else sys.stderr)
    return out["exit"]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
