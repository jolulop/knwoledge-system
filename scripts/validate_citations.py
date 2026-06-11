#!/usr/bin/env python3
"""Basic citation checks for claim and query pages.

This scaffold check intentionally stays simple: claim pages need at least one
source in frontmatter or an explicit "No source found in vault." marker.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
NO_SOURCE = "No source found in vault."


def parse_frontmatter(text: str) -> dict[str, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    data: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip()
    return data


def list_has_values(value: str | None) -> bool:
    if value is None:
        return False
    cleaned = value.strip()
    return cleaned not in {"", "[]", "null", "None"}


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    claims_dir = root / "wiki" / "Claims"
    queries_dir = root / "wiki" / "Queries"
    errors: list[str] = []

    for path in sorted(claims_dir.rglob("*.md")) if claims_dir.exists() else []:
        text = path.read_text(encoding="utf-8", errors="replace")
        fm = parse_frontmatter(text)
        if not list_has_values(fm.get("sources")) and NO_SOURCE not in text:
            errors.append(f"{path.relative_to(root)}: claim has no sources and no explicit no-source marker")
        if "## Evidence" not in text:
            errors.append(f"{path.relative_to(root)}: missing Evidence section")

    for path in sorted(queries_dir.rglob("*.md")) if queries_dir.exists() else []:
        text = path.read_text(encoding="utf-8", errors="replace")
        if "## Citations" not in text and NO_SOURCE not in text:
            errors.append(f"{path.relative_to(root)}: query answer has no Citations section and no no-source marker")

    if errors:
        print("Citation validation failed:")
        for err in errors:
            print(f"- {err}")
        return 1
    print("Citation validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
