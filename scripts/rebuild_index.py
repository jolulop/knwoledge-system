#!/usr/bin/env python3
"""Rebuild wiki/index.md from frontmatter and > [!summary] callouts.

Dependency-free by design. The parser intentionally supports only the frontmatter
subset used by this project: scalar `key: value` lines and simple bracket lists.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

WIKI_DIRS = [
    "Sources",
    "Concepts",
    "Claims",
    "Entities",
    "People",
    "Organizations",
    "Projects",
    "Tags",
    "Synthesis",
    "Queries",
]

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_value(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip('"\'') for item in inner.split(",")]
    return raw.strip('"\'')


def parse_frontmatter(text: str) -> dict[str, Any]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    data: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = parse_value(value)
    return data


def extract_summary(text: str) -> str:
    """Extract full Obsidian summary callout text."""
    in_callout = False
    parts: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("> [!summary]"):
            in_callout = True
            continue
        if in_callout:
            if stripped.startswith(">"):
                part = stripped.lstrip(">").strip()
                if part:
                    parts.append(part)
            else:
                break
    return " ".join(parts) if parts else "(no summary)"


def page_title(path: Path, frontmatter: dict[str, Any], text: str) -> str:
    if frontmatter.get("title"):
        return str(frontmatter["title"])
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem.replace("-", " ").title()


def wiki_link(path: Path, wiki_root: Path) -> str:
    rel = path.relative_to(wiki_root).with_suffix("")
    return f"[[{rel.as_posix()}]]"


def collect_pages(wiki_root: Path) -> dict[str, list[dict[str, Any]]]:
    sections: dict[str, list[dict[str, Any]]] = {name: [] for name in WIKI_DIRS}
    for section in WIKI_DIRS:
        section_dir = wiki_root / section
        if not section_dir.exists():
            continue
        for path in sorted(section_dir.rglob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            fm = parse_frontmatter(text)
            sections[section].append(
                {
                    "path": path,
                    "link": wiki_link(path, wiki_root),
                    "title": page_title(path, fm, text),
                    "type": fm.get("type", section.lower().rstrip("s")),
                    "status": fm.get("status", "unknown"),
                    "confidence": fm.get("confidence", ""),
                    "summary": extract_summary(text),
                    "updated": fm.get("updated", fm.get("ingested", fm.get("created", ""))),
                }
            )
    return sections


def render_index(sections: dict[str, list[dict[str, Any]]]) -> str:
    # Deterministic: a pure function of the wiki pages, no wall-clock (ADR-0023).
    lines: list[str] = [
        "# Index",
        "",
        "> [!summary]",
        "> Auto-generated navigation index for the wiki. Do not edit manually; update source pages and run `scripts/rebuild_index.py` instead.",
        "",
        "## Navigation Counts",
        "",
    ]
    for section, pages in sections.items():
        lines.append(f"- {section}: {len(pages)}")
    lines.append("")

    for section, pages in sections.items():
        lines.append(f"## {section} ({len(pages)})")
        lines.append("")
        if not pages:
            lines.append("_No pages yet._")
            lines.append("")
            continue
        for page in pages:
            meta = page["status"]
            if page["confidence"]:
                meta = f"{meta}, {page['confidence']} confidence"
            if page["updated"]:
                meta = f"{meta}, updated {page['updated']}"
            lines.append(f"- {page['link']} · {meta}")
            lines.append(f"  {page['summary']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    project_root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    wiki_root = project_root / "wiki"
    if not wiki_root.exists():
        print(f"error: wiki directory not found at {wiki_root}", file=sys.stderr)
        return 2
    sections = collect_pages(wiki_root)
    output = render_index(sections)
    index_path = wiki_root / "index.md"
    index_path.write_text(output, encoding="utf-8")
    print(f"rebuilt {index_path} with {sum(len(v) for v in sections.values())} pages", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
