#!/usr/bin/env python3
"""Validate required frontmatter and summary callouts for wiki pages."""
from __future__ import annotations

import re
import sys
from pathlib import Path

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
WIKI_SUBDIRS = ["Sources", "Concepts", "Claims", "Entities", "People", "Organizations", "Projects", "Tags", "Synthesis", "Queries"]
VALID_TYPES = {"source", "concept", "claim", "entity", "person", "organization", "project", "tag", "synthesis", "query"}
# Field contracts per page type (ADR-0016/0020/0021/0022). Source pages use
# relative_raw_path (never absolute raw_path) and the shared lifecycle fields.
REQUIRED_BY_TYPE = {
    "source": [
        "type", "source_id", "title", "relative_raw_path", "normalized_path",
        "sha256", "file_type", "status", "ingestion_status", "summary_status",
        "generation_status", "input_fingerprint",
    ],
    "concept": ["type", "concept_id", "title", "status", "confidence"],
    "claim": ["type", "claim_id", "status", "confidence"],
    "entity": ["type", "entity_id", "title", "status", "confidence"],
    "person": ["type", "person_id", "title", "status", "confidence"],
    "organization": ["type", "organization_id", "title", "status", "confidence"],
    "project": ["type", "project_id", "title", "status", "confidence"],
    "synthesis": ["type", "synthesis_id", "title", "status"],
    # No wall-clock fields: a saved Query page is a deterministic derived artifact (ADR-0023/0034),
    # like claim/synthesis pages — byte-stable, so no `created`/`last_compiled_at`.
    "query": ["type", "query_id", "title", "question", "status"],
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


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0]).resolve() if argv else Path.cwd()
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
