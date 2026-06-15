#!/usr/bin/env python3
"""Structured-citation validation for claim (and query) pages (ADR-0019/0020/0026).

Replaces the Phase-3 scaffold. For every claim page it parses the structured
`citations:` frontmatter list and mechanically grounds each citation against the cited
source's normalized Markdown: the `(source_id, char_start, char_end)` range must be in
bounds and the evidence quote must occur verbatim (whitespace-normalized) at that range
(claims require a quote). A missing normalized source, an unknown source, an out-of-bounds
range, or a quote mismatch all fail; `chunk_id` is advisory and never grounds. A claim may
instead carry the explicit `No source found in vault.` marker.

Query pages keep the lighter Phase-5 check (a Citations section or the no-source marker).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.workers.citations import ground_citation, parse_citations

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
NO_SOURCE = "No source found in vault."


def _frontmatter(text: str) -> str:
    match = FRONTMATTER_RE.match(text)
    return match.group(1) if match else ""


def _check_claim(root: Path, path: Path) -> list[str]:
    sid = path.stem
    errors: list[str] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    citations = parse_citations(_frontmatter(text))

    if not citations:
        if NO_SOURCE not in text:
            errors.append(f"{sid}: claim has no citations and no '{NO_SOURCE}' marker")
        return errors

    markdown_dir = root / "normalized" / "markdown"
    for i, citation in enumerate(citations):
        source_id = citation.get("source_id")
        md_path = markdown_dir / f"{source_id}.md"
        if not isinstance(source_id, str) or not md_path.exists():
            errors.append(f"{sid}: citation[{i}] cites missing normalized source {source_id!r}")
            continue
        normalized = md_path.read_text(encoding="utf-8", errors="replace")
        for problem in ground_citation(citation, normalized, require_quote=True):
            errors.append(f"{sid}: citation[{i}] {problem}")

    if "## Evidence" not in text:
        errors.append(f"{sid}: missing Evidence section")
    return errors


def _check_query(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if "## Citations" not in text and NO_SOURCE not in text:
        return [f"{path.stem}: query answer has no Citations section and no no-source marker"]
    return []


def main(argv: list[str]) -> int:
    root = Path(argv[0]).resolve() if argv else Path.cwd()
    errors: list[str] = []

    claims_dir = root / "wiki" / "Claims"
    for path in sorted(claims_dir.rglob("*.md")) if claims_dir.exists() else []:
        errors.extend(_check_claim(root, path))

    queries_dir = root / "wiki" / "Queries"
    for path in sorted(queries_dir.rglob("*.md")) if queries_dir.exists() else []:
        errors.extend(_check_query(path))

    if errors:
        print("Citation validation failed:")
        for err in errors:
            print(f"- {err}")
        return 1
    print("Citation validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
