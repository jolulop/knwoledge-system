#!/usr/bin/env python3
"""Validate required frontmatter and summary callouts for wiki pages."""
from __future__ import annotations

import re
import sys
from pathlib import Path

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
WIKI_SUBDIRS = ["Sources", "Concepts", "Claims", "Entities", "People", "Organizations", "Projects", "Tags", "Synthesis", "Queries"]
VALID_TYPES = {"source", "concept", "claim", "entity", "person", "organization", "project", "tag", "synthesis", "query"}
REQUIRED_BY_TYPE = {
    "source": ["type", "source_id", "title", "raw_path", "sha256", "file_type", "language", "status"],
    "concept": ["type", "title", "status", "confidence", "sources"],
    "claim": ["type", "claim_id", "status", "confidence", "sources"],
    "entity": ["type", "title", "status", "confidence", "sources"],
    "synthesis": ["type", "title", "status", "sources", "claims"],
    "query": ["type", "title", "question", "created"],
}


def parse_frontmatter(text: str) -> dict[str, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    data: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"\'')
    return data


def has_summary(text: str) -> bool:
    return any(line.strip().startswith("> [!summary]") for line in text.splitlines())


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    wiki = root / "wiki"
    errors: list[str] = []
    for subdir in WIKI_SUBDIRS:
        folder = wiki / subdir
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            fm = parse_frontmatter(text)
            rel = path.relative_to(root)
            if not fm:
                errors.append(f"{rel}: missing frontmatter")
                continue
            page_type = fm.get("type")
            if page_type not in VALID_TYPES:
                errors.append(f"{rel}: invalid or missing type: {page_type!r}")
            required = REQUIRED_BY_TYPE.get(page_type or "", ["type", "title"])
            for key in required:
                if key not in fm or fm[key] == "":
                    errors.append(f"{rel}: missing required frontmatter field `{key}`")
            if not has_summary(text):
                errors.append(f"{rel}: missing > [!summary] callout")

    if errors:
        print("Frontmatter validation failed:")
        for err in errors:
            print(f"- {err}")
        return 1
    print("Frontmatter validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
