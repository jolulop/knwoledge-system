#!/usr/bin/env python3
"""Deterministic normalized-Markdown assembly and heading-aware chunking (Phase 2).

Extractors emit a flat list of :class:`Element` blocks (headings, prose paragraphs,
and tables). :func:`assemble` serializes them into the canonical
``normalized/markdown/<source_id>.md`` text and, in the same pass, produces the
heading-aware chunks for ``normalized/chunks/<source_id>.jsonl``.

Every citation anchor is mechanically derived (ADR-0012): ``char_start``/``char_end``
are the exact offsets of the chunk text inside the serialized Markdown (so
``markdown[char_start:char_end] == chunk.text`` always holds), and ``page``/``page_end``
come only from page numbers the extractor attached to elements — never estimated. A
non-paginated format leaves ``page`` null; the section/offset anchor stands in.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Sentence terminators used only to avoid mid-sentence splits of oversized paragraphs.
_SENTENCE_ENDINGS = (". ", "! ", "? ", ".\n", "!\n", "?\n")


@dataclass
class Element:
    """One structural block produced by an extractor.

    ``kind`` is ``"heading"``, ``"prose"`` or ``"table"``. ``level`` applies to
    headings (1-6). ``page`` is the 1-based source page this block came from for
    paginated formats (PDF), else ``None``. Table blocks carry ``table_reference``
    (a repository-relative CSV path) and, for spreadsheets, ``sheet_reference``.
    """

    kind: str
    text: str
    level: int = 0
    page: int | None = None
    table_reference: str | None = None
    sheet_reference: str | None = None


@dataclass
class Chunk:
    chunk_id: str
    source_id: str
    ordinal: int
    kind: str
    heading_path: list[str]
    section: str | None
    text: str
    char_start: int
    char_end: int
    page: int | None = None
    page_end: int | None = None
    table_reference: str | None = None
    sheet_reference: str | None = None

    def to_dict(self) -> dict[str, Any]:
        # Stable key order so chunk files are byte-diffable across runs.
        return {
            "chunk_id": self.chunk_id,
            "source_id": self.source_id,
            "ordinal": self.ordinal,
            "kind": self.kind,
            "heading_path": self.heading_path,
            "section": self.section,
            "text": self.text,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "page": self.page,
            "page_end": self.page_end,
            "table_reference": self.table_reference,
            "sheet_reference": self.sheet_reference,
        }


@dataclass
class _Para:
    """A serialized prose paragraph and its offsets/page in the Markdown."""

    char_start: int
    char_end: int
    page: int | None


def chunk_id(source_id: str, ordinal: int) -> str:
    """`<source_id>::<zero-padded ordinal>` (Phase 2 Plan §5)."""
    return f"{source_id}::{ordinal:04d}"


def _page_range(pages: list[int | None]) -> tuple[int | None, int | None]:
    present = [p for p in pages if p is not None]
    if not present:
        return None, None
    return min(present), max(present)


def _split_oversized(start: int, end: int, markdown: str, max_chars: int) -> list[tuple[int, int]]:
    """Split one over-max paragraph into [start,end) sub-ranges on sentence boundaries.

    ``start``/``end`` are absolute offsets into ``markdown``. Falls back to the nearest
    whitespace (then a hard cut as a last resort) only when a single sentence exceeds
    ``max_chars``, so normal prose is never cut mid-sentence.
    """
    spans: list[tuple[int, int]] = []
    cursor = start
    while end - cursor > max_chars:
        window_end = cursor + max_chars
        # Prefer the last sentence ending within the window.
        cut = -1
        for ending in _SENTENCE_ENDINGS:
            idx = markdown.rfind(ending, cursor, window_end)
            if idx != -1:
                cut = max(cut, idx + len(ending))
        if cut <= cursor:
            # No sentence boundary: break at the last whitespace in the window.
            ws = markdown.rfind(" ", cursor, window_end)
            cut = ws + 1 if ws != -1 and ws + 1 > cursor else window_end
        spans.append((cursor, cut))
        cursor = cut
    if cursor < end:
        spans.append((cursor, end))
    return spans


def assemble(
    source_id: str,
    elements: list[Element],
    *,
    target_chars: int,
    max_chars: int,
) -> tuple[str, list[Chunk]]:
    """Serialize ``elements`` to Markdown and produce heading-aware chunks.

    Returns ``(markdown, chunks)``. ``markdown`` is the exact text chunk offsets index
    into. Chunking is deterministic: identical elements yield identical output.
    """
    parts: list[str] = []
    pos = 0
    chunks: list[Chunk] = []
    heading_stack: list[tuple[int, str]] = []  # (level, title)
    pending: list[_Para] = []
    ordinal = 0

    def emit(text: str) -> tuple[int, int]:
        nonlocal pos
        start = pos
        parts.append(text)
        pos += len(text)
        return start, pos

    def heading_path() -> list[str]:
        return [title for _, title in heading_stack]

    def add_chunk(
        kind: str,
        char_start: int,
        char_end: int,
        pages: list[int | None],
        *,
        table_reference: str | None = None,
        sheet_reference: str | None = None,
    ) -> None:
        nonlocal ordinal
        path = heading_path()
        page, page_end = _page_range(pages)
        chunks.append(
            Chunk(
                chunk_id=chunk_id(source_id, ordinal),
                source_id=source_id,
                ordinal=ordinal,
                kind=kind,
                heading_path=path,
                section=path[-1] if path else None,
                text="".join(parts)[char_start:char_end],
                char_start=char_start,
                char_end=char_end,
                page=page,
                page_end=page_end,
                table_reference=table_reference,
                sheet_reference=sheet_reference,
            )
        )
        ordinal += 1

    def flush_prose() -> None:
        """Pack accumulated paragraphs of the current section into prose chunks."""
        i = 0
        n = len(pending)
        while i < n:
            first = pending[i]
            single_len = first.char_end - first.char_start
            if single_len > max_chars:
                # Oversized lone paragraph: split on sentence boundaries.
                full_md = "".join(parts)
                for s, e in _split_oversized(
                    first.char_start, first.char_end, full_md, max_chars
                ):
                    add_chunk("prose", s, e, [first.page])
                i += 1
                continue
            # Greedily extend while the span stays within the soft target and stays
            # on the same source page, so a chunk never straddles a page boundary
            # (keeps PDF page anchors precise: page == page_end).
            j = i
            while (
                j + 1 < n
                and pending[j + 1].page == first.page
                and pending[j + 1].char_end - first.char_start <= target_chars
            ):
                j += 1
            char_start = first.char_start
            char_end = pending[j].char_end
            pages = [pending[k].page for k in range(i, j + 1)]
            add_chunk("prose", char_start, char_end, pages)
            i = j + 1
        pending.clear()

    for el in elements:
        text = el.text.strip()
        if not text:
            continue
        if el.kind == "heading":
            flush_prose()
            level = max(1, min(6, el.level or 1))
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, text))
            emit("#" * level + " " + text)
            emit("\n\n")
        elif el.kind == "table":
            flush_prose()
            start, end = emit(text)
            emit("\n\n")
            add_chunk(
                "table",
                start,
                end,
                [el.page],
                table_reference=el.table_reference,
                sheet_reference=el.sheet_reference,
            )
        else:  # prose
            start, end = emit(text)
            emit("\n\n")
            pending.append(_Para(char_start=start, char_end=end, page=el.page))

    flush_prose()

    markdown = "".join(parts).rstrip() + "\n" if parts else ""
    return markdown, chunks
