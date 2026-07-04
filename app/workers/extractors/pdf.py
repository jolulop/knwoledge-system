#!/usr/bin/env python3
"""PDF extractor (Phase 2): pypdf text with per-page provenance.

Each page's embedded text is extracted, reflowed into paragraphs, and emitted as prose
elements carrying that page's 1-based number, so chunk page anchors are mechanically
derived from real page provenance — never estimated (ADR-0012). A paginated source
whose extracted text averages fewer than ~16 characters per page is reported
``partial`` with a ``needs_ocr`` warning rather than an error (ADR-0010): it is
catalogued but awaits OCR, which is out of scope for Phase 2.
"""
from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from app.workers.chunking import Element
from app.workers.extractors import Extraction, dehyphenate, paragraphs_from_text, pkg_version

# Average chars/page below this on a paginated source means "no real embedded text".
_NEEDS_OCR_CHARS_PER_PAGE = 16


def extract(path: Path) -> Extraction:
    reader = PdfReader(str(path))
    pages = reader.pages
    page_count = len(pages)

    elements: list[Element] = []
    total_chars = 0
    for index, page in enumerate(pages, start=1):
        # De-hyphenation must precede the paragraph reflow: the line-break signal (-\n) is the
        # only mechanical marker of a hyphenation split and reflow collapses it (ADR-0054).
        text = dehyphenate(page.extract_text() or "")
        for para in paragraphs_from_text(text):
            total_chars += len(para)
            elements.append(Element(kind="prose", text=para, page=index))

    warnings: list[str] = []
    status = "extracted"
    avg = total_chars / page_count if page_count else 0
    if page_count and avg < _NEEDS_OCR_CHARS_PER_PAGE:
        warnings.append("needs_ocr")
        status = "partial"

    return Extraction(
        elements=elements,
        tool="pypdf",
        tool_version=pkg_version("pypdf"),
        page_count=page_count,
        warnings=warnings,
        status=status,
    )
