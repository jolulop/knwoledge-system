#!/usr/bin/env python3
"""Markdown extractor (Phase 2): parse source Markdown into structural Elements.

Headings keep their ATX level; fenced code blocks are preserved verbatim; every other
run of non-blank lines becomes one prose paragraph (list formatting preserved). No
front matter is added — that is a Phase 3 concern. Non-paginated, so ``page`` stays
null on every element.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.workers.chunking import Element
from app.workers.extractors import Extraction

_ATX = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def extract(path: Path) -> Extraction:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    elements: list[Element] = []
    buffer: list[str] = []

    def flush_paragraph() -> None:
        if not buffer:
            return
        block = "\n".join(ln.rstrip() for ln in buffer).strip("\n")
        if block.strip():
            elements.append(Element(kind="prose", text=block))
        buffer.clear()

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if _FENCE.match(line):
            # Capture the whole fenced block verbatim as one prose element.
            flush_paragraph()
            fence = _FENCE.match(line).group(1)
            block_lines = [line]
            i += 1
            while i < n and not lines[i].lstrip().startswith(fence):
                block_lines.append(lines[i])
                i += 1
            if i < n:
                block_lines.append(lines[i])  # closing fence
                i += 1
            elements.append(Element(kind="prose", text="\n".join(block_lines)))
            continue

        heading = _ATX.match(line)
        if heading:
            flush_paragraph()
            elements.append(
                Element(kind="heading", text=heading.group(2).strip(), level=len(heading.group(1)))
            )
        elif line.strip() == "":
            flush_paragraph()
        else:
            buffer.append(line)
        i += 1

    flush_paragraph()
    # Pure-stdlib normalizer: no third-party Markdown library is used.
    return Extraction(elements=elements, tool="markdown", tool_version="builtin")
