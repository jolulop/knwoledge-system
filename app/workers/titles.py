#!/usr/bin/env python3
"""Document titles (ADR-0066 part 3, decision 6).

A filename such as "1 s2.0 S0306437926000529 main" carries no information, so every surface
shows, and every prompt receives, a real title when one can be found:

- **The effective title** (shown on the Source page, the review screens and in Obsidian):
  confirmed title > Enrich title > PDF metadata title (only if :func:`filter_pdf_title` passes
  it) > filename.
- **The prompt title** (claims and items prompts): the same chain **without** the confirmed
  title, so a human correction is display-only and never restales or re-bills them (part 3 Q2).
  An Enrich run that changes the title does restale them: their freshness includes it.

The PDF metadata comes from the extraction log (`metadata`, written by extraction): untrusted
data, used only as a hint. Its title is used only when a deterministic filter passes it.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from app.workers.wiki_render import title_from_filename

MAX_TITLE_CHARS = 300
_WS = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TAB_PREFIX = re.compile(r"^\(\d+\)\s+")                       # a browser tab's "(6) " counter
_APP_PREFIX = re.compile(r"^microsoft (word|powerpoint|excel) - ", re.IGNORECASE)
_FILE_EXT = re.compile(r"\.(pdf|docx?|pptx?|xlsx?|odt|rtf|txt|md|html?|tex|dvi|ps|indd|pages|key)$",
                       re.IGNORECASE)


def clean_text(value: Any, *, max_chars: int = MAX_TITLE_CHARS) -> str:
    """NFKC, control characters out, whitespace collapsed, length capped. Untrusted input."""
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = _WS.sub(" ", _CONTROL.sub(" ", text)).strip()
    return text[:max_chars].strip()


def _fold(text: str) -> str:
    return _WS.sub(" ", re.sub(r"[-_.]+", " ", text)).strip().casefold()


def filter_pdf_title(title: Any, original_filename: str) -> str | None:
    """A PDF metadata title, or None when it carries no information: empty, filename-like
    ("Day_1_v3.pdf", an editor's "Microsoft Word - x.docx"), or only repeating the filename.
    A browser tab's "(n) " prefix is stripped."""
    text = clean_text(title)
    text = _APP_PREFIX.sub("", _TAB_PREFIX.sub("", text)).strip()
    if len(text) < 3 or text.casefold() in {"untitled", "untitled document", "title"}:
        return None
    if _FILE_EXT.search(text) or (" " not in text and "_" in text):
        return None
    stem = original_filename.rsplit(".", 1)[0] if original_filename else ""
    if stem and _fold(text) == _fold(stem):
        return None
    return text


def read_metadata_hint(extraction_logs_dir: Path, source_id: str) -> dict[str, str]:
    """The `metadata` hint recorded by extraction (`{title, author}`, either may be missing)."""
    path = Path(extraction_logs_dir) / f"{source_id}.json"
    try:
        log = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    meta = log.get("metadata") if isinstance(log, dict) else None
    if not isinstance(meta, dict):
        return {}
    return {k: v for k in ("title", "author") if (v := clean_text(meta.get(k)))}


def prompt_title(manifest: dict[str, Any], *, enrichment: dict[str, Any] | None,
                 hint: dict[str, str]) -> str:
    """The title the claims and items prompts receive: Enrich title > filtered PDF title >
    filename. Never the confirmed title (part 3 Q2)."""
    filename = manifest.get("original_filename") or manifest.get("source_id", "")
    enrich_title = clean_text((enrichment or {}).get("title"))
    if enrich_title:
        return enrich_title
    return filter_pdf_title(hint.get("title"), filename) or title_from_filename(filename)


def confirmed_title(manifest: dict[str, Any]) -> str:
    prov = manifest.get("provenance")
    return clean_text(prov.get("title")) if isinstance(prov, dict) else ""


def effective_title(manifest: dict[str, Any], *, enrichment: dict[str, Any] | None,
                    hint: dict[str, str]) -> str:
    """The title a document is shown with: confirmed > :func:`prompt_title`."""
    return confirmed_title(manifest) or prompt_title(manifest, enrichment=enrichment, hint=hint)


MAX_NAME_CHARS = 200
MAX_AUTHORS = 30
_TAG_JOINERS = re.compile(r"(?<=\w)[-_]+(?=\w)")


def name_identity(name: Any) -> str:
    """The normalized identity of a provenance name (ADR-0066 part 3, implementation grill 2 Q3):
    NFKC, casefold and whitespace only. Not `merge_key`, which singularizes ("Robin Woods" would
    become "Robin Wood"); accents and non-Latin characters stay."""
    return clean_text(name, max_chars=MAX_NAME_CHARS).casefold()


def clean_names(names: Any) -> list[str]:
    """Ordered, de-duplicated (by :func:`name_identity`) display names."""
    out: list[str] = []
    seen: set[str] = set()
    for name in names if isinstance(names, list) else []:
        text = clean_text(name, max_chars=MAX_NAME_CHARS)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out[:MAX_AUTHORS]


def clean_tag_name(name: Any) -> str:
    """A tag name cleaned like other names: "planning-taxonomy" -> "planning taxonomy"."""
    return clean_text(_TAG_JOINERS.sub(" ", name) if isinstance(name, str) else "",
                      max_chars=MAX_NAME_CHARS)


def prompt_title_for(manifest: dict[str, Any], normalized_markdown: str, *,
                     enrichment_dir: Path, logs_dir: Path) -> str:
    """:func:`prompt_title` from disk: the Enrich artifact only when fresh for the current text,
    and the extraction log's hint. Used by claims and items for both their prompt and their
    freshness, so an Enrich title change restales them (part 3 Q2)."""
    from app.workers import enrichment_artifact as art
    sid = manifest["source_id"]
    enrichment = art.load_fresh(Path(enrichment_dir), sid, normalized_markdown,
                                prompt_input=art.enrich_prompt_input_for(manifest, Path(logs_dir)))
    return prompt_title(manifest, enrichment=enrichment,
                        hint=read_metadata_hint(Path(logs_dir), sid))
