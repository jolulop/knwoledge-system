#!/usr/bin/env python3
"""HTML extractor (Phase 2): beautifulsoup4 main-content text.

Non-content and unsafe nodes (``script``, ``style``, ``nav``, ``header``, ``footer``,
comments) are stripped, then the remaining DOM is walked into headings, prose
paragraphs, lists, and inline GFM tables. No referenced resource is ever fetched —
extraction performs zero network I/O (ADR-0010). HTML is not paginated, so ``page``
stays null.
"""
from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup, Comment

from app.workers.chunking import Element
from app.workers.extractors import Extraction, collapse_ws, gfm_table, pkg_version

_DROP_TAGS = ("script", "style", "nav", "header", "footer", "noscript", "template", "iframe")
_BLOCK_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "table")


def _list_markdown(tag) -> str:
    items = []
    ordered = tag.name == "ol"
    for n, li in enumerate(tag.find_all("li", recursive=False), start=1):
        text = collapse_ws(li.get_text(" "))
        if text:
            items.append(f"{n}. {text}" if ordered else f"- {text}")
    return "\n".join(items)


def _table_markdown(tag) -> str:
    rows = []
    for tr in tag.find_all("tr"):
        cells = [collapse_ws(c.get_text(" ")) for c in tr.find_all(["th", "td"])]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    return gfm_table(rows[0], rows[1:])


def extract(path: Path) -> Extraction:
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(raw, "html.parser")

    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for tag in soup(_DROP_TAGS):
        tag.decompose()

    root = soup.body or soup
    elements: list[Element] = []
    for tag in root.find_all(_BLOCK_TAGS):
        name = tag.name
        if name.startswith("h") and len(name) == 2 and name[1].isdigit():
            text = collapse_ws(tag.get_text(" "))
            if text:
                elements.append(Element(kind="heading", text=text, level=int(name[1])))
        elif name in ("ul", "ol"):
            md = _list_markdown(tag)
            if md:
                elements.append(Element(kind="prose", text=md))
        elif name == "table":
            md = _table_markdown(tag)
            if md:
                elements.append(Element(kind="prose", text=md))
        else:  # p
            text = collapse_ws(tag.get_text(" "))
            if text:
                elements.append(Element(kind="prose", text=text))

    return Extraction(
        elements=elements, tool="beautifulsoup4", tool_version=pkg_version("beautifulsoup4")
    )
