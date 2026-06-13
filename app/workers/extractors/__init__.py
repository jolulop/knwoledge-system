#!/usr/bin/env python3
"""Per-format extractors (Phase 2).

Each extractor turns one raw file into an :class:`Extraction`: a flat list of
:class:`~app.workers.chunking.Element` blocks plus the metadata the orchestrator
records (tool/version, page count, warnings) and any structured table files to write.
Extractors are deterministic and perform no network I/O. They raise on unparseable
input; the orchestrator turns that into an ``error`` status and continues the run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version

from app.workers.chunking import Element

# Any run of whitespace (incl. newlines and nbsp) collapses to one space for inline
# text; paragraph separation is handled separately via blank lines.
_WS = re.compile(r"\s+")
_BLANKS = re.compile(r"\n[ \t]*\n+")


@dataclass
class Extraction:
    elements: list[Element]
    tool: str
    tool_version: str
    page_count: int | None = None
    warnings: list[str] = field(default_factory=list)
    # (filename, csv_text) pairs written under normalized/tables/<source_id>/.
    tables: list[tuple[str, str]] = field(default_factory=list)
    # "extracted" or "partial"; orchestrator owns the "error" path.
    status: str = "extracted"


def pkg_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:  # pragma: no cover - deps are declared
        return "unknown"


def collapse_ws(text: str) -> str:
    """Collapse runs of inline whitespace to a single space; strip the ends."""
    return _WS.sub(" ", text).strip()


def paragraphs_from_text(text: str) -> list[str]:
    """Split free text into paragraphs on blank lines, collapsing inline whitespace.

    Single newlines inside a paragraph become spaces, so reflowed PDF lines join into
    readable prose. Empty paragraphs are dropped.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paras: list[str] = []
    for block in _BLANKS.split(text):
        joined = collapse_ws(block.replace("\n", " "))
        if joined:
            paras.append(joined)
    return paras


def _escape_cell(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def gfm_table(header: list[object], rows: list[list[object]]) -> str:
    """Render a header + rows as a GitHub-flavored Markdown table (deterministic)."""
    cols = [_escape_cell(h) for h in header]
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for row in rows:
        cells = [_escape_cell(c) for c in row]
        # Pad/truncate ragged rows to the header width.
        cells = (cells + [""] * len(cols))[: len(cols)]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)
