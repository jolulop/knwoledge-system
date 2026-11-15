#!/usr/bin/env python3
"""Inspect a damaged manifest, and restore it from a compatible backup (ADR-0074 decision 3).

Usage:
    uv run python scripts/repair_manifest.py inspect <source_id> [--root ROOT] [--backups DIR] [--json]
    uv run python scripts/repair_manifest.py restore <source_id> --archive <zip>
        --expected-state <digest> --reason "<why>" [--root ROOT] [--backups DIR]
    uv run python scripts/repair_manifest.py finish <source_id> --expected-state <digest>
        [--root ROOT] [--backups DIR]

`inspect` is read-only: it takes no lock, changes no revision and creates nothing. It reports the
manifest's state and error category, the damaged file's size and sha256 (or "unavailable"; never its
contents), the raw file only from trustworthy evidence, every backup archive's copy with its status
and confirmed Details, the later human decisions on record and whether each copy is compatible with
them, and the state fingerprint. It prints a write command only when that command's preconditions
hold. It exits 0 whenever it produced its report, damage found or not.

`restore` is the only write path, for a **corrupt** manifest: quarantine copy of the damaged bytes
(`raw/manifests/quarantine/`, kept), recovery intent (publication queued, `db/manifest_recovery/`
journal), atomic replacement with the validated backup copy, then publication, the audit record and
the journal closed. An **unreadable** manifest has no write path. `finish` completes a recovery
whose journal is still open. Backups default to `<root>/backups` (scripts/backup.py).

Exit codes:
  0  report produced / restore or finish completed
  1  a refused write (a safety check, no compatible copy) or an operational failure
  2  usage
  3  Apply, intake, extraction or republish is running
  4  the publication queue is unreadable (`scripts/republish.py --repair`)
  6  state changed since it was inspected: inspect again
  9  the vault revision can't be advanced
Never touches raw/ outside raw/manifests/.
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

from app.backend.manifests import is_source_id  # noqa: E402
from app.workers import manifest_repair as mr  # noqa: E402

_DIGEST = re.compile(r"[0-9a-f]{64}")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="repair_manifest.py",
                                description="Inspect or restore a damaged manifest.")
    sub = p.add_subparsers(dest="command", required=True)
    ins = sub.add_parser("inspect", help="read-only diagnostic report")
    ins.add_argument("source_id")
    ins.add_argument("--json", action="store_true")
    rs = sub.add_parser("restore", help="restore a corrupt manifest from a compatible backup")
    rs.add_argument("source_id")
    rs.add_argument("--archive", required=True)
    fin = sub.add_parser("finish", help="complete a recovery whose journal is still open")
    fin.add_argument("source_id")
    for writer in (rs, fin):
        writer.add_argument("--expected-state", required=True)
    rs.add_argument("--reason", required=True)
    for cmd in (ins, rs, fin):
        cmd.add_argument("--root", default=".")
        cmd.add_argument("--backups", default=None)
    return p


def _print_report(report: dict) -> None:
    print(f"Manifest of {report['source_id']}")
    for key, value in report["facts"].items():
        print(f"  {key}: {value}")
    if report["copies"]:
        print("\nBackup copies (newest first):")
    for c in report["copies"]:
        if c["state"] != "copy":
            print(f"  {c['archive']}: rejected ({c['reason']})")
            continue
        d = c["details"]
        authors = ", ".join(a["name"] for a in d["authors"]) or "—"
        verdict = "compatible" if not c["incompatible"] else "NOT compatible: " + "; ".join(
            c["incompatible"])
        print(f"  {c['archive']} (created {c['created_at'] or 'unknown'}): status {c['status']}; "
              f"confirmed Details: title {d['title'] or '—'}, authors {authors}, publisher "
              f"{(d['publisher'] or {}).get('name') or '—'}; raw {c['raw']['state']}; {verdict}")
    print()
    for line in report["findings"]:
        print(line)
    print(f"\nState fingerprint: {report['fingerprint']}")
    commands = list(report["offers"].values()) + ([report["finish"]] if report["finish"] else [])
    if commands:
        print("Offered (replace <why>):")
        for cmd in commands:
            print(f"  {cmd}")


def main(argv: list[str]) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:                       # argparse: --help is 0, a usage error 2
        return int(exc.code or 0)
    if not is_source_id(args.source_id):
        print("Usage error: the source id must look like src_<16 hex>.", file=sys.stderr)
        return mr.EXIT_USAGE
    root = Path(args.root).resolve()
    backups = Path(args.backups).resolve() if args.backups else None
    if args.command == "inspect":
        report = mr.inspect(root, args.source_id, backups)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True, default=str))
        else:
            _print_report(report)
        return mr.EXIT_OK
    if not _DIGEST.fullmatch(args.expected_state):
        print("Usage error: --expected-state must be the 64-hex fingerprint `inspect` printed.",
              file=sys.stderr)
        return mr.EXIT_USAGE
    if args.command == "restore":
        out = mr.restore(root, args.source_id, archive=Path(args.archive),
                         expected_state=args.expected_state, reason=args.reason,
                         backups_dir=backups)
    else:
        out = mr.finish(root, args.source_id, expected_state=args.expected_state,
                        backups_dir=backups)
    print(out["message"], file=sys.stdout if out["exit"] == mr.EXIT_OK else sys.stderr)
    return out["exit"]


if __name__ == "__main__":
    # restore / finish hold the vault lock (shared) and the write lock while they write (ADR-0039
    # amendment: a backup never captures a half-done restore); inspect takes no lock.
    raise SystemExit(main(sys.argv[1:]))
