#!/usr/bin/env python3
"""Build a local SQLite FTS5 keyword index over wiki and normalized Markdown files."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

INCLUDE_DIRS = ["wiki", "normalized/markdown"]


def iter_markdown(root: Path):
    for rel in INCLUDE_DIRS:
        folder = root / rel
        if folder.exists():
            yield from sorted(folder.rglob("*.md"))


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    db_dir = root / "db"
    db_dir.mkdir(exist_ok=True)
    db_path = db_dir / "metadata.sqlite"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS documents (path TEXT PRIMARY KEY, title TEXT, body TEXT)")
    try:
        cur.execute("CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(path, title, body)")
    except sqlite3.OperationalError as exc:
        print(f"warning: SQLite FTS5 unavailable: {exc}", file=sys.stderr)
        cur.execute("CREATE TABLE IF NOT EXISTS documents_fts (path TEXT PRIMARY KEY, title TEXT, body TEXT)")

    cur.execute("DELETE FROM documents")
    cur.execute("DELETE FROM documents_fts")
    count = 0
    for path in iter_markdown(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        title = path.stem.replace("-", " ").title()
        for line in text.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        rel = path.relative_to(root).as_posix()
        cur.execute("INSERT OR REPLACE INTO documents(path, title, body) VALUES (?, ?, ?)", (rel, title, text))
        cur.execute("INSERT INTO documents_fts(path, title, body) VALUES (?, ?, ?)", (rel, title, text))
        count += 1
    conn.commit()
    conn.close()
    print(f"indexed {count} markdown documents into {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
