#!/usr/bin/env python3
"""Validate that catalogued raw files have not mutated since intake (ADR-0002/0024).

`source_id` is the raw content hash, so a raw file whose bytes change after intake makes
the manifest — and everything derived from it — describe content that no longer exists at
that path. This check compares each manifest's recorded checksum to the file on disk and
hard-fails on a confirmed mismatch.

It is affordable at scale: it pre-filters on the size and modified_at already recorded in
the manifest and only re-hashes a file whose size or mtime has drifted. A referenced file
that is simply missing is reported but is not a failure here (deletion/retention is a
separate concern). Manifests are gitignored local state: no manifests means nothing to
validate (a pass).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend.manifests import sha256_file


def _iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")


def _occurrences(manifest: dict) -> list[dict]:
    occ = manifest.get("occurrences")
    if occ:
        return occ
    # Fall back to the canonical record if occurrences are absent.
    return [{
        "relative_path": manifest.get("relative_raw_path", ""),
        "size_bytes": manifest.get("size_bytes"),
        "modified_at": manifest.get("modified_at"),
    }]


def main(argv: list[str]) -> int:
    root = Path(argv[0]).resolve() if argv else Path.cwd()
    manifests_dir = root / "raw" / "manifests"
    if not manifests_dir.exists():
        print("Raw integrity validation passed (no manifests).")
        return 0

    mismatches: list[str] = []
    missing: list[str] = []
    checked = hashed = 0

    for mpath in sorted(manifests_dir.glob("*.json")):
        try:
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        expected_sha = manifest.get("sha256")
        for occ in _occurrences(manifest):
            rel = occ.get("relative_path")
            if not rel:
                continue
            path = root / rel
            checked += 1
            if not path.is_file():
                missing.append(rel)
                continue
            # Cheap pre-filter: only re-hash when size or mtime drifted from intake.
            try:
                size_ok = path.stat().st_size == occ.get("size_bytes")
                mtime_ok = _iso_mtime(path) == occ.get("modified_at")
            except OSError as exc:
                mismatches.append(f"{rel}: cannot stat ({exc})")
                continue
            if size_ok and mtime_ok:
                continue
            hashed += 1
            if sha256_file(path) != expected_sha:
                mismatches.append(f"{rel}: sha256 differs from manifest (raw file mutated)")

    if missing:
        print(f"Note: {len(missing)} catalogued raw file(s) missing from disk:")
        for rel in missing:
            print(f"- {rel}")
    if mismatches:
        print("Raw integrity validation failed:")
        for err in mismatches:
            print(f"- {err}")
        print("Re-scan the inbox to mint a correct content-keyed manifest, then re-derive.")
        return 1
    print(f"Raw integrity validation passed ({checked} file(s) checked, {hashed} re-hashed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
