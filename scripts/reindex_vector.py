#!/usr/bin/env python3
"""Prepare heading-aware chunks for future vector indexing.

This scaffold does not call an embedding API. It writes JSONL chunks to
`normalized/chunks/chunks.jsonl` so a later implementation can embed them with
OpenAI, Anthropic-compatible tooling, bge-m3, LanceDB, ChromaDB, or Qdrant.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$")
INCLUDE_DIRS = ["wiki", "normalized/markdown"]


def split_heading_chunks(path: Path, text: str):
    current_heading = "Document"
    current_lines: list[str] = []
    for line in text.splitlines():
        match = HEADING_RE.match(line)
        if match and current_lines:
            yield current_heading, "\n".join(current_lines).strip()
            current_heading = match.group(2).strip()
            current_lines = [line]
        else:
            if match:
                current_heading = match.group(2).strip()
            current_lines.append(line)
    if current_lines:
        yield current_heading, "\n".join(current_lines).strip()


def iter_markdown(root: Path):
    for rel in INCLUDE_DIRS:
        folder = root / rel
        if folder.exists():
            yield from sorted(folder.rglob("*.md"))


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    out_dir = root / "normalized" / "chunks"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "chunks.jsonl"
    count = 0
    with out_path.open("w", encoding="utf-8") as f:
        for path in iter_markdown(root):
            rel = path.relative_to(root).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            for idx, (heading, body) in enumerate(split_heading_chunks(path, text)):
                if not body:
                    continue
                record = {
                    "chunk_id": f"{rel}::chunk-{idx}",
                    "path": rel,
                    "heading": heading,
                    "text": body,
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
    print(f"wrote {count} heading-aware chunks to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
