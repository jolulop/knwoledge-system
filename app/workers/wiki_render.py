#!/usr/bin/env python3
"""Deterministic rendering of wiki Source pages (Phase 3).

Renders `templates/source.md` from a manifest plus the source's normalized Markdown,
filling only mechanically-derived values (ADR-0016). No LLM, no network, byte-stable for
byte-stable input. The `> [!summary]` callout is a labelled extractive stub: the first
prose paragraph of the normalized Markdown, truncated on a sentence boundary, with a
structural fallback when the source has too little text (e.g. needs_ocr).

Also exposes a tiny frontmatter parser reused by the API and the wiki validator.
"""
from __future__ import annotations

import re
from typing import Any

_TOKEN = re.compile(r"\{\{(\w+)\}\}")
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_WS = re.compile(r"\s+")
_SENTENCE = re.compile(r"[.!?]")


# --- frontmatter (dependency-free, project subset) --------------------------


def _parse_value(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [] if not inner else [i.strip().strip("\"'") for i in inner.split(",")]
    return raw.strip("\"'")


def parse_frontmatter(text: str) -> dict[str, Any]:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}
    data: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        # strip inline comments from scalar values (not inside quotes/brackets)
        data[key.strip()] = _parse_value(value.split("  #", 1)[0])
    return data


# --- rendering --------------------------------------------------------------


def render_template(template: str, values: dict[str, Any]) -> str:
    """Substitute every ``{{token}}`` from ``values`` (strict: unknown token errors)."""
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(f"no value for template token {{{{{key}}}}}")
        val = values[key]
        return "" if val is None else str(val)

    return _TOKEN.sub(repl, template)


def title_from_filename(original_filename: str) -> str:
    """Readable title from a filename: drop extension, separators → spaces."""
    stem = original_filename.rsplit(".", 1)[0]
    title = _WS.sub(" ", stem.replace("-", " ").replace("_", " ")).strip()
    return (title or original_filename).replace('"', "'")


def _first_prose_paragraph(markdown: str) -> str:
    blocks = re.split(r"\n[ \t]*\n", markdown)
    for block in blocks:
        stripped = block.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("|"):
            continue
        return _WS.sub(" ", stripped).strip()
    return ""


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    ends = list(_SENTENCE.finditer(window))
    if ends and ends[-1].end() >= max_chars // 2:
        return window[: ends[-1].end()]
    return window.rstrip() + "…"


def summary_excerpt(
    markdown: str, title: str, page_count: int | None, chunk_count: int,
    *, max_chars: int, min_chars: int,
) -> str:
    """Extractive first-paragraph excerpt, or a structural fallback for sparse text."""
    para = _first_prose_paragraph(markdown)
    if len(para) >= min_chars:
        return _truncate(para, max_chars)
    pages = "unknown" if page_count is None else str(page_count)
    return f"Source: {title}. {pages} pages, {chunk_count} chunks."


def build_source_values(
    manifest: dict[str, Any], normalized_markdown: str, *,
    summary_max: int, summary_min: int, now: str,
) -> dict[str, Any]:
    sid = manifest["source_id"]
    title = title_from_filename(manifest.get("original_filename", sid))
    page_count = manifest.get("page_count")
    chunk_count = int(manifest.get("chunk_count") or 0)
    normalized = manifest.get("normalized") or {}
    return {
        "source_id": sid,
        "title": title,
        "relative_raw_path": manifest.get("relative_raw_path", ""),
        "normalized_path": normalized.get("markdown_path", f"normalized/markdown/{sid}.md"),
        "sha256": manifest.get("sha256", ""),
        "file_type": manifest.get("file_extension", ""),
        "language": manifest.get("language", "unknown"),
        "page_count": "null" if page_count is None else str(page_count),
        "chunk_count": str(chunk_count),
        "ingestion_status": manifest.get("ingestion_status", ""),
        "created_at": manifest.get("created_at", ""),
        "ingested_at": manifest.get("discovered_at", ""),
        "last_compiled_at": now,
        "extractive_excerpt": summary_excerpt(
            normalized_markdown, title, page_count, chunk_count,
            max_chars=summary_max, min_chars=summary_min,
        ),
        "notes": "",
    }


def render_source_page(
    template: str, manifest: dict[str, Any], normalized_markdown: str, *,
    summary_max: int, summary_min: int, now: str,
) -> str:
    values = build_source_values(
        manifest, normalized_markdown,
        summary_max=summary_max, summary_min=summary_min, now=now,
    )
    return render_template(template, values)
