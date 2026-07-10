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
    "Items",
    "Claims",
    "Tags",
    "Synthesis",
    "Queries",
]

# ADR-0059: the Items section groups by item_type in the classifier priority order, the
# QA sentinel bucket last — LOCAL constants because this script is dependency-free by
# design (a parity test pins them against app/backend/taxonomy.py).
ITEM_GROUP_ORDER = [
    "domain", "model", "ai_topic_area", "architecture_pattern", "model_family_architecture",
    "method_technique", "technology_capability", "use_case", "problem_risk",
    "product_tool_platform", "standard_protocol_interface", "data_ontology_asset",
    "governance_regulation", "infrastructure_hardware", "provider_institution",
    "unclassified_review_required",
]
_ITEM_DISPLAY = {"ai_topic_area": "AI Topic Area",
                 "unclassified_review_required": "Unclassified (review required)"}


def item_display(item_type: str) -> str:
    return _ITEM_DISPLAY.get(item_type, item_type.replace("_", " ").title())

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


WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def display_link_label(title: str) -> str:
    # ADR-0060 rendered link label — LOCAL copy of wiki_render.display_link_label (this
    # script is dependency-free by design; a parity test pins the behaviour).
    collapsed = re.sub(r"\s+", " ", str(title))
    safe = WIKILINK_RE.sub(lambda m: m.group(1).split("|", 1)[-1].strip(), collapsed)
    safe = safe.replace("[", "").replace("]", "").replace("|", " ")
    safe = re.sub(r"\s+", " ", safe).strip()
    return (safe[:77].rstrip() + "…") if len(safe) > 78 else safe


def wiki_link(path: Path, wiki_root: Path, title: str = "") -> str:
    # ADR-0060: the primary index link carries the display alias (no duplicated title text).
    rel = path.relative_to(wiki_root).with_suffix("")
    label = display_link_label(title) if str(title).strip() else ""
    return f"[[{rel.as_posix()}|{label}]]" if label else f"[[{rel.as_posix()}]]"


def collect_pages(wiki_root: Path) -> dict[str, list[dict[str, Any]]]:
    sections: dict[str, list[dict[str, Any]]] = {name: [] for name in WIKI_DIRS}
    for section in WIKI_DIRS:
        section_dir = wiki_root / section
        if not section_dir.exists():
            continue
        for path in sorted(section_dir.rglob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            fm = parse_frontmatter(text)
            title = page_title(path, fm, text)
            sections[section].append(
                {
                    "path": path,
                    "link": wiki_link(path, wiki_root, title),
                    "title": title,
                    "type": fm.get("type", section.lower().rstrip("s")),
                    "item_type": fm.get("item_type", ""),
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

    def _page_lines(page: dict[str, Any]) -> list[str]:
        meta = page["status"]
        if page["confidence"]:
            meta = f"{meta}, {page['confidence']} confidence"
        if page["updated"]:
            meta = f"{meta}, updated {page['updated']}"
        return [f"- {page['link']} · {meta}", f"  {page['summary']}"]

    for section, pages in sections.items():
        lines.append(f"## {section} ({len(pages)})")
        lines.append("")
        if not pages:
            lines.append("_No pages yet._")
            lines.append("")
            continue
        if section == "Items":
            # ADR-0059: group by item_type (priority order; the sentinel's QA bucket renders
            # last and never as a taxonomy group). Unknown/missing types sort into the QA
            # bucket rather than being dropped — the index is the full page listing.
            groups: dict[str, list[dict[str, Any]]] = {}
            for page in pages:
                itype = page.get("item_type") or ""
                key = itype if itype in ITEM_GROUP_ORDER[:-1] else ITEM_GROUP_ORDER[-1]
                groups.setdefault(key, []).append(page)
            for itype in ITEM_GROUP_ORDER:
                members = groups.get(itype)
                if not members:
                    continue
                lines.append(f"### {item_display(itype)} ({len(members)})")
                lines.append("")
                for page in members:
                    lines.extend(_page_lines(page))
                lines.append("")
            continue
        for page in pages:
            lines.extend(_page_lines(page))
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
