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

import hashlib
import re
from typing import Any

_TOKEN = re.compile(r"\{\{(\w+)\}\}")
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_WS = re.compile(r"\s+")
_SENTENCE = re.compile(r"[.!?]")
_FP_LINE = re.compile(r"(?m)^input_fingerprint:.*\n?")
_WIKILINK_SUB = re.compile(r"\[\[([^\]]+)\]\]")

# Bump to force a global Source-page rebuild even when rendered bytes are unchanged.
SOURCE_SCHEMA_VERSION = "wiki-source-v1"


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


def _delink(text: str) -> str:
    """Neutralise any `[[wikilink]]` to plain text (ADR-0016/0029, CLAUDE.md rule 4).

    3.5a generated content may not assert links: the SQLite graph and its backlink
    projector do not exist until 3.5b, so a model-emitted wikilink would be either a
    dangling link or an unreviewed phantom edge. Render `[[a|b]]` as `b`, `[[a]]` as `a`.
    """
    return _WIKILINK_SUB.sub(
        lambda m: m.group(1).split("|", 1)[-1].strip(), text
    )


def _render_tag_list(tags: list[Any]) -> str:
    """Render a tag list as an inline YAML array, sanitised and deduplicated."""
    seen: list[str] = []
    for tag in tags:
        cleaned = str(tag).replace('"', "").replace("[", "").replace("]", "").replace("\n", " ")
        cleaned = _WS.sub(" ", cleaned).strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    if not seen:
        return "[]"
    return "[" + ", ".join(f'"{t}"' for t in seen) + "]"


def build_source_values(
    manifest: dict[str, Any], normalized_markdown: str, *,
    summary_max: int, summary_min: int,
    enrichment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sid = manifest["source_id"]
    title = title_from_filename(manifest.get("original_filename", sid))
    page_count = manifest.get("page_count")
    chunk_count = int(manifest.get("chunk_count") or 0)
    normalized = manifest.get("normalized") or {}

    if enrichment:
        # Composed LLM summary/tags: model-authored, labelled generated/unverified, with
        # page-level provenance to this one source (ADR-0026). Span-grounded claims are 3.5b.
        summary_status = "enriched"
        generation_status = "enriched"
        summary_label = "Generated summary (unverified)"
        summary_text = _delink(_WS.sub(" ", str(enrichment.get("summary", "")).strip())) or "(no summary)"
        tags = _render_tag_list(enrichment.get("tags") or [])
    else:
        summary_status = "stub"
        generation_status = "deterministic"
        summary_label = "Extractive excerpt (auto-generated, unverified)"
        # De-link too: source text in the excerpt may contain [[..]] that would otherwise
        # render as a dangling wikilink on the Source page (ADR-0016, CLAUDE.md rule 4).
        summary_text = _delink(summary_excerpt(
            normalized_markdown, title, page_count, chunk_count,
            max_chars=summary_max, min_chars=summary_min,
        ))
        tags = "[]"

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
        "summary_status": summary_status,
        "generation_status": generation_status,
        "summary_label": summary_label,
        "summary_text": summary_text,
        "tags": tags,
        "notes": "",
    }


def _fingerprint(page_without_fp: str) -> str:
    h = hashlib.sha256()
    h.update(SOURCE_SCHEMA_VERSION.encode("utf-8"))
    h.update(b"\0")
    h.update(page_without_fp.encode("utf-8"))
    return h.hexdigest()[:16]


def _claim_title(claim_text: str) -> str:
    """A short, single-line title from the claim text (no invented content)."""
    flat = _WS.sub(" ", claim_text).strip()
    head = _SENTENCE.split(flat, 1)[0].strip() if _SENTENCE.search(flat) else flat
    head = head if head else flat
    return (head[:77].rstrip() + "…") if len(head) > 78 else head


def _quote_cell(text: str) -> str:
    """Display rendering of an evidence quote for a Markdown table cell (a view, not the
    record): collapse whitespace and neutralise table pipes. Callers also de-link it."""
    return _WS.sub(" ", str(text)).strip().replace("|", "\\|")


def _fm_quote(text: str) -> str:
    """Faithful, round-trippable rendering of an evidence quote for the frontmatter
    `citations[].quote` (the machine-readable record validate_citations grounds).

    Whitespace is collapsed (grounding normalises whitespace on both sides, so this is
    span-faithful), then `\\`, `"`, and `[`/`]` are backslash-escaped — so the value parses
    back to the exact span (citations._parse_scalar unescapes) and contains no literal
    `[[` for validate_wikilinks to flag.
    """
    collapsed = _WS.sub(" ", str(text)).strip()
    return (collapsed.replace("\\", "\\\\").replace('"', '\\"')
            .replace("[", "\\[").replace("]", "\\]"))


def render_claim_page(claim: dict[str, Any]) -> str:
    """Render a deterministic Claim page from a grounded claim record (ADR-0019/0020/0022).

    The frontmatter `citations:` list is the machine-readable record (validated by
    scripts/validate_citations.py); the Evidence table is a rendered view of it. Sources are
    linked as `[[Sources/<source_id>]]` (backed by the active derived_from edge). No
    wall-clock value is embedded — enriched but idempotent: freshness lives in the
    input_fingerprint, so a cache-replay re-run is byte-stable. Empty supporting/contradicting
    sections are omitted (no placeholder links, ADR-0016/0029).
    """
    claim_id = claim["claim_id"]
    claim_text = _WS.sub(" ", str(claim["claim_text"])).strip()
    confidence = claim.get("confidence", "low")
    citations = claim.get("citations", [])
    active = bool(citations)
    # No active evidence -> a tombstone: kept page-backed for audit and node authority
    # (ADR-0030), marked deprecated_candidate pending human-reviewed deletion (ADR-0018);
    # never hard-deleted (CLAUDE.md rule 9).
    status = "active" if active else "deprecated_candidate"
    review_status = "none" if active else "pending"

    fm_lines = [
        "---",
        "type: claim",
        f'claim_id: "{claim_id}"',
        f"status: {status}",
        f"review_status: {review_status}",
        "generation_status: enriched",
        f"confidence: {confidence}",
        # claim_text is the durable authority for the claim's wording (ADR-0030 node
        # metadata lives in frontmatter); escaped so it round-trips and carries no [[.
        f'claim_text: "{_fm_quote(claim_text)}"',
    ]
    evidence_rows = []
    if active:
        fm_lines.append("citations:")
        for c in citations:
            fm_lines.extend([
                f'  - source_id: "{c["source_id"]}"',
                f'    char_start: {c["char_start"]}',
                f'    char_end: {c["char_end"]}',
                f'    page: {c.get("page") if c.get("page") is not None else "null"}',
                '    section: null',
                '    chunk_id: null',
                f'    quote: "{_fm_quote(c.get("quote", ""))}"',
            ])
            evidence_rows.append(
                f'| [[Sources/{c["source_id"]}]] | {c["char_start"]}–{c["char_end"]} | '
                f'{_delink(_quote_cell(c.get("quote", "")))} |'
            )
    else:
        fm_lines.append("citations: []")
    fm_lines.append('input_fingerprint: ""')
    fm_lines.append("---")

    if active:
        label = "Generated claim (unverified)"
        evidence_section = ["| Source | Char range | Quote |", "|---|---|---|", *evidence_rows]
    else:
        label = "Claim evidence superseded — pending review"
        evidence_section = [
            "_Evidence superseded; this claim is retained for audit and is pending "
            "human review (ADR-0018). It has no active source._"
        ]

    body = [
        "",
        f"# Claim: {_delink(_claim_title(claim_text))}",
        "",
        f"> [!summary] {label}",
        f"> {_delink(claim_text)}",
        "",
        "## Claim",
        "",
        _delink(claim_text),
        "",
        "## Evidence",
        "",
        *evidence_section,
        "",
        "## Notes",
        "",
    ]
    draft = "\n".join(fm_lines + body) + "\n"
    fingerprint = _fingerprint(_FP_LINE.sub("", draft))
    return draft.replace('input_fingerprint: ""', f'input_fingerprint: "{fingerprint}"', 1)


def render_source_page(
    template: str, manifest: dict[str, Any], normalized_markdown: str, *,
    summary_max: int, summary_min: int,
    enrichment: dict[str, Any] | None = None,
) -> str:
    """Render a Source page, stamping its input_fingerprint.

    The fingerprint hashes the page's own rendered content (with the fingerprint line
    excluded) plus a schema version, so it transitively covers the template, the
    normalized text, the manifest fields, the summary config, and — when present — the
    composed enrichment artifact (summary/tags), every input that determines the bytes
    (ADR-0023/0025). No wall-clock value is embedded; enrichment freshness is the
    artifact's own fingerprint, checked before composition (ADR-0027).
    """
    values = build_source_values(
        manifest, normalized_markdown, summary_max=summary_max, summary_min=summary_min,
        enrichment=enrichment,
    )
    draft = render_template(template, {**values, "input_fingerprint": ""})
    fingerprint = _fingerprint(_FP_LINE.sub("", draft))
    return draft.replace('input_fingerprint: ""', f'input_fingerprint: "{fingerprint}"', 1)
