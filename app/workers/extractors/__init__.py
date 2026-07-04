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


# Line-break hyphenation repair (ADR-0054). Word-char = Unicode alphanumeric ([^\W_], underscore
# excluded per the contract); the hyphen class includes U+00AD so a discretionary soft hyphen at a
# line break repairs like a hard one. The single \n keeps the repair paragraph-bounded by
# construction: a blank line (\n\n) can never match. Group 2 is a lookahead capture so consecutive
# hyphenated lines (in-\nter-\nnal) repair in one pass without overlapping matches.
_SOFT_HYPHEN = "\u00ad"
_LINEBREAK_HYPHEN = re.compile(r"([^\W_])[-\u00ad][ \t]*\n[ \t]*(?=([^\W_]))")


def dehyphenate(text: str) -> str:
    """Repair PDF line-break hyphenation splits before paragraph reflow (ADR-0054).

    Must run while ``-\\n`` is still visible — ``paragraphs_from_text`` destroys the signal when it
    collapses newlines to spaces. Two total, deterministic branches: both boundary chars lowercase
    letters → typographic hyphenation, drop hyphen and break (``con-\\ntributions`` →
    ``contributions``); otherwise → keep the hyphen, drop only the break (``COVID-\\n19`` →
    ``COVID-19``). Accepted error class: a lowercase compound split at its real hyphen loses it
    (``best-\\nknown`` → ``bestknown``) — avoiding that needs dictionary segmentation, excluded by
    design. Remaining soft hyphens (U+00AD) are stripped everywhere afterward.
    """

    def _join(match: re.Match[str]) -> str:
        before, after = match.group(1), match.group(2)
        if before.islower() and after.islower():
            return before
        return before + "-"

    return _LINEBREAK_HYPHEN.sub(_join, text).replace(_SOFT_HYPHEN, "")


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
