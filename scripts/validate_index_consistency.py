#!/usr/bin/env python3
"""Validate that generated retrieval indexes only reference paths that exist on disk.

The keyword (FTS) index in ``db/metadata.sqlite`` and the chunk index in
``normalized/chunks/chunks.jsonl`` are generated artifacts rebuilt from wiki and
normalized content. When source pages are deleted but an index is not regenerated,
the index points at evidence that no longer exists, which makes retrieval unsafe.
This check fails when any indexed path is missing from the working tree.

Both indexes are optional local runtime state (gitignored). A missing index file is
not an error: there is simply nothing to validate. The check only fails on a stale
index that references deleted files.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def chunk_index_paths(root: Path) -> set[str]:
    path = root / "normalized" / "chunks" / "chunks.jsonl"
    if not path.exists():
        return set()
    paths: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        ref = record.get("path")
        if ref:
            paths.add(ref)
    return paths


def keyword_index_paths(root: Path) -> set[str]:
    db_path = root / "db" / "metadata.sqlite"
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT path FROM documents").fetchall()
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()
    return {row[0] for row in rows if row[0]}


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    errors: list[str] = []

    for label, paths in (
        ("chunk index (normalized/chunks/chunks.jsonl)", chunk_index_paths(root)),
        ("keyword index (db/metadata.sqlite)", keyword_index_paths(root)),
    ):
        for rel in sorted(paths):
            if not (root / rel).exists():
                errors.append(f"{label}: references missing file {rel}")

    if errors:
        print("Index consistency validation failed:")
        for err in errors:
            print(f"- {err}")
        print("Rebuild the affected index (reindex_keyword.py / reindex_vector.py).")
        return 1
    print("Index consistency validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
