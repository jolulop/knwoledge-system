#!/usr/bin/env python3
"""Mechanical citation grounding gate (ADR-0019/0020/0026).

This is the deterministic core of Phase 3.5b's "claims are grounded or dropped" rule: a
claim's structured citation must resolve against the source's normalized Markdown — the
authoritative `(source_id, char_start, char_end)` range must be in bounds and the evidence
quote must occur verbatim (whitespace-normalized) at that range. No LLM is involved; the
LLM claim-extraction pass calls this gate per citation and drops claims that fail it, and
`scripts/validate_citations.py` uses it to validate claim pages already on disk.

`chunk_id` is advisory only (ADR-0019) and is never used for grounding. Also exposes a
small parser for the nested `citations:` frontmatter block, since the project's flat
frontmatter parser does not handle it.
"""
from __future__ import annotations

import re
from typing import Any

_WS = re.compile(r"\s+")
_SOURCE_ID = re.compile(r"^src_[0-9a-f]{16}$")
_INT = re.compile(r"-?\d+$")


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip()


def ground_citation(
    citation: dict[str, Any],
    normalized_markdown: str,
    *,
    page_count: int | None = None,
    require_quote: bool = False,
) -> list[str]:
    """Return a list of grounding problems for one citation; empty means it resolves.

    Checks the authoritative locator and, when present, the evidence quote and page.
    `require_quote=True` (used for claims, ADR-0026) makes a missing quote a problem.
    """
    problems: list[str] = []

    sid = citation.get("source_id")
    if not isinstance(sid, str) or not _SOURCE_ID.match(sid):
        problems.append(f"source_id missing or malformed: {sid!r}")

    start, end = citation.get("char_start"), citation.get("char_end")
    if (
        not isinstance(start, int) or isinstance(start, bool)
        or not isinstance(end, int) or isinstance(end, bool)
    ):
        problems.append("char_start/char_end must be integers")
    else:
        length = len(normalized_markdown)
        if start < 0 or end <= start or end > length:
            problems.append(f"char range [{start}, {end}) out of bounds for length {length}")
        else:
            quote = citation.get("quote")
            if quote in (None, ""):
                if require_quote:
                    problems.append("missing evidence quote")
            elif _norm(str(quote)) != _norm(normalized_markdown[start:end]):
                problems.append("evidence quote does not match the cited range")

    page = citation.get("page")
    if page is not None:
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            problems.append(f"page must be a positive integer: {page!r}")
        elif page_count is not None and page > page_count:
            problems.append(f"page {page} exceeds page_count {page_count}")

    return problems


def is_grounded(citation: dict[str, Any], normalized_markdown: str, **kwargs: Any) -> bool:
    return not ground_citation(citation, normalized_markdown, **kwargs)


def _parse_scalar(raw: str) -> Any:
    raw = raw.split("  #", 1)[0].strip()
    if raw in ("", "null", "None", "~", "[]"):
        return None
    if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
        return raw[1:-1]
    if _INT.match(raw):
        return int(raw)
    return raw


def parse_citations(frontmatter: str) -> list[dict[str, Any]]:
    """Parse the nested `citations:` list out of a page's YAML frontmatter block.

    Handles the regular shape the templates emit (a list of `- key: value` objects);
    a dependency-free, targeted parser, not a general YAML implementation.
    """
    items: list[dict[str, Any]] = []
    in_block = False
    current: dict[str, Any] | None = None
    for line in frontmatter.splitlines():
        if not in_block:
            if re.match(r"^citations:\s*$", line):
                in_block = True
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if line[:1] not in (" ", "\t"):
            break  # dedented to the next top-level key — end of the block
        if stripped.startswith("- "):
            current = {}
            items.append(current)
            stripped = stripped[2:].strip()
            if not stripped:
                continue
        if current is not None and ":" in stripped:
            key, _, value = stripped.partition(":")
            current[key.strip()] = _parse_scalar(value.strip())
    return items
