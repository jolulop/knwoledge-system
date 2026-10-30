#!/usr/bin/env python3
"""Rebuild wiki/index.md from frontmatter and > [!summary] callouts.

Dependency-free by design. The parser intentionally supports only the frontmatter
subset used by this project: scalar `key: value` lines and simple bracket lists. One exception
(ADR-0066 part 3 slice 4): the "Authors & publishers" section comes from
`app.workers.authors.inventory`, the same read-only builder publication renders the Authors pages
from, so the two never disagree. Run standalone it takes the write lock itself (`changes=False`);
publication, which already holds it, passes an internal flag instead.
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
QUOTED_ITEMS_RE = re.compile(r'^\s*"[^"]*"\s*(?:,\s*"[^"]*"\s*)*$')


def parse_value(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        if QUOTED_ITEMS_RE.match(inner):                   # a comma inside a quoted item stays in it
            return re.findall(r'"([^"]*)"', inner)
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


def authors_section(people: list[dict[str, Any]]) -> list[str]:
    """`## Authors & publishers` (ADR-0066 part 3 slice 4): one line per page, alphabetical by
    title, with its role counts; nothing when the inventory is empty."""
    if not people:
        return []
    lines = [f"## Authors & publishers ({len(people)})", ""]
    for p in people:
        counts = [f"{role} of {len(p['roles'][role])}" for role in ("author", "publisher")
                  if p["roles"][role]]
        label = display_link_label(p["title"]) or p["filename"]
        lines.append(f"- [[Authors/{p['filename']}|{label}]] — {', '.join(counts)}")
    return lines + [""]


def render_index(sections: dict[str, list[dict[str, Any]]],
                 people: list[dict[str, Any]] | None = None) -> str:
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
    lines.extend(authors_section(people or []))
    return "\n".join(lines).rstrip() + "\n"


_PARENT_HOLDS_LOCK = "--parent-holds-write-lock"   # internal: passed only by publication


def _people(project_root: Path) -> list[dict[str, Any]]:
    """The Authors inventory. Never silently empty: without the app package, or with an unreadable
    manifest, the rebuild fails, and publication keeps the index work pending."""
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from app.workers import authors
    return authors.inventory(project_root)


def _build(project_root: Path, wiki_root: Path) -> int:
    sections = collect_pages(wiki_root)
    output = render_index(sections, _people(project_root))
    index_path = wiki_root / "index.md"
    index_path.write_text(output, encoding="utf-8")
    print(f"rebuilt {index_path} with {sum(len(v) for v in sections.values())} pages", file=sys.stderr)
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:] if a != _PARENT_HOLDS_LOCK]
    project_root = Path(args[0]).resolve() if args else Path.cwd()
    wiki_root = project_root / "wiki"
    if not wiki_root.exists():
        print(f"error: wiki directory not found at {wiki_root}", file=sys.stderr)
        return 2
    if _PARENT_HOLDS_LOCK in sys.argv[1:]:
        return _build(project_root, wiki_root)
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from app.backend import write_lock
    with write_lock.hold(project_root / "reviews", authority=False, changes=False):
        return _build(project_root, wiki_root)


if __name__ == "__main__":
    raise SystemExit(main())
