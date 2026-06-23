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
# The abstention marker (policies/citation.yaml when_no_source_found); validate_citations greps for it.
NO_SOURCE = "No source found in vault."
_FP_LINE = re.compile(r"(?m)^input_fingerprint:.*\n?")
_WIKILINK_SUB = re.compile(r"\[\[([^\]]+)\]\]")

# Bump to force a global Source-page rebuild even when rendered bytes are unchanged.
SOURCE_SCHEMA_VERSION = "wiki-source-v1"

# Frontmatter review_status vocabulary (ADR-0022). An explicit render override (the Phase-6 deprecation
# render seam, ADR-0035 A5) must be one of these; None preserves the renderer's derived value.
REVIEW_STATUSES = frozenset({"none", "pending", "approved", "rejected"})


def _resolve_review_status(override: str | None, derived: str) -> str:
    """Return an explicit review_status override (validated) or the renderer's derived value."""
    if override is None:
        return derived
    if override not in REVIEW_STATUSES:
        raise ValueError(f"unknown review_status {override!r}; allowed: {sorted(REVIEW_STATUSES)}")
    return override

# Node type -> wiki subdirectory (page routing / link targets), Build Spec §6.1.
NODE_DIR = {
    "source": "Sources", "concept": "Concepts", "entity": "Entities", "person": "People",
    "organization": "Organizations", "project": "Projects", "claim": "Claims",
    "synthesis": "Synthesis", "query": "Queries", "tag": "Tags",
}


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


_PENDING = "_Pending semantic enrichment._"


def _claim_alias(title: str) -> str:
    """Wikilink-safe display alias for a claim link: de-linked, no `[]|`, collapsed, short."""
    safe = _delink(_WS.sub(" ", str(title))).replace("[", "").replace("]", "").replace("|", " ")
    safe = _WS.sub(" ", safe).strip()
    return (safe[:77].rstrip() + "…") if len(safe) > 78 else safe


def _link_list(items: list[dict[str, Any]] | None) -> str:
    """Render a Source-page section as graph-backed wikilinks from passed-in data (no IO).

    Each item is {target, title|None}: a linked short title when a label is available, else
    a bare link. Empty/None -> the deterministic placeholder, byte-identical to the Phase-3
    backbone so unenriched Source pages do not churn.
    """
    if not items:
        return _PENDING
    lines = []
    for it in items:
        target = it["target"]
        alias = _claim_alias(it.get("title")) if it.get("title") else ""
        lines.append(f"- [[{target}|{alias}]]" if alias else f"- [[{target}]]")
    return "\n".join(lines)


def _mention_slugs(items: list[dict[str, Any]] | None) -> list[str]:
    """Slugs from projected mention items (for the machine-readable frontmatter arrays)."""
    return [it["target"].rsplit("/", 1)[-1] for it in (items or [])]


def _claims_block(claims: list[dict[str, Any]] | None) -> str:
    """Source page Claims section from claim data ({claim_id, title|None})."""
    items = None if claims is None else [
        {"target": f"Claims/{c['claim_id']}", "title": c.get("title")} for c in claims
    ]
    return _link_list(items)


def build_source_values(
    manifest: dict[str, Any], normalized_markdown: str, *,
    summary_max: int, summary_min: int,
    enrichment: dict[str, Any] | None = None,
    claims: list[dict[str, Any]] | None = None,
    concepts: list[dict[str, Any]] | None = None,
    entities: list[dict[str, Any]] | None = None,
    people: list[dict[str, Any]] | None = None,
    organizations: list[dict[str, Any]] | None = None,
    projects: list[dict[str, Any]] | None = None,
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
        # Source lifecycle status authority is the manifest (default active, ADR-0036 decision 13;
        # validated at the write path manifests.set_status). Templatized so it flows into the page +
        # its input_fingerprint — a status change re-renders the Source page.
        "status": manifest.get("status") or "active",
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
        "claims_block": _claims_block(claims),
        "concepts_block": _link_list(concepts),
        "entities_block": _link_list(entities),
        "people_block": _link_list(people),
        "organizations_block": _link_list(organizations),
        "projects_block": _link_list(projects),
        # Frontmatter arrays mirror the projected body links as an **advisory** slug
        # projection — NOT relationship authority. The id-keyed graph is the source of truth
        # for mentions (ADR-0029/0030); on rename/re-slug these re-project from the graph.
        # validate_projection enforces frontmatter == body so they never drift.
        "concepts_fm": _render_tag_list(_mention_slugs(concepts)),
        "entities_fm": _render_tag_list(_mention_slugs(entities)),
        "people_fm": _render_tag_list(_mention_slugs(people)),
        "organizations_fm": _render_tag_list(_mention_slugs(organizations)),
        "projects_fm": _render_tag_list(_mention_slugs(projects)),
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


def render_claim_page(claim: dict[str, Any], *, review_status: str | None = None) -> str:
    """Render a deterministic Claim page from a grounded claim record (ADR-0019/0020/0022).

    The frontmatter `citations:` list is the machine-readable record (validated by
    scripts/validate_citations.py); the Evidence table is a rendered view of it. Sources are
    linked as `[[Sources/<source_id>]]` (backed by the active derived_from edge). No
    wall-clock value is embedded — enriched but idempotent: freshness lives in the
    input_fingerprint, so a cache-replay re-run is byte-stable. Empty supporting/contradicting
    sections are omitted (no placeholder links, ADR-0016/0029).

    `review_status` is normally *derived* from the claim's evidence/deprecated state; an explicit,
    validated override is the deterministic render-path input the Phase-6 deprecation executor uses to
    mark an approved deprecation (ADR-0035 A5) — a no-evidence tombstone would otherwise derive `pending`.
    """
    claim_id = claim["claim_id"]
    claim_text = _WS.sub(" ", str(claim["claim_text"])).strip()
    confidence = claim.get("confidence", "low")
    citations = claim.get("citations", [])
    active = bool(citations)
    # Three lifecycle outcomes (ADR-0018/0030/0031):
    #  - no evidence -> a tombstone (deprecated_candidate, pending review), kept page-backed for
    #    audit and node authority, never hard-deleted (CLAUDE.md rule 9);
    #  - `deprecated` with evidence -> a human supersede decision: the claim still has evidence
    #    but lost a contradiction review, so it is deprecated_candidate (already decided), with
    #    its evidence and active contradiction backlink still shown;
    #  - otherwise active.
    deprecated = bool(claim.get("deprecated"))
    if not active:
        status, derived_review_status = "deprecated_candidate", "pending"
    elif deprecated:
        status, derived_review_status = "deprecated_candidate", "approved"
    else:
        status, derived_review_status = "active", "none"
    rs = _resolve_review_status(review_status, derived_review_status)

    fm_lines = [
        "---",
        "type: claim",
        f'claim_id: "{claim_id}"',
        f"status: {status}",
        f"review_status: {rs}",
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
    # Active `contradicts` backlinks (ADR-0031): symmetric, projected on both Claim pages once
    # a human acknowledges the contradiction (the graph holds the relationship authority).
    contradicts = sorted(claim.get("contradicts", []) or []) if active else []
    if contradicts:
        fm_lines.append("contradicts:")
        fm_lines.extend(f"  - {cid}" for cid in contradicts)
    else:
        fm_lines.append("contradicts: []")
    fm_lines.append('input_fingerprint: ""')
    fm_lines.append("---")

    if active:
        label = ("Claim deprecated — superseded by contradiction review" if deprecated
                 else "Generated claim (unverified)")
        evidence_section = ["| Source | Char range | Quote |", "|---|---|---|", *evidence_rows]
    else:
        # The callout prose tracks the resolved review_status so an approved deprecation doesn't read
        # "pending review" (ADR-0035 A5). Default (derived `pending`) is byte-identical to before.
        if rs == "approved":
            label = "Claim evidence superseded — approved deprecation"
            review_note = "approved for deprecation (ADR-0018)"
        else:
            label = "Claim evidence superseded — pending review"
            review_note = "pending human review (ADR-0018)"
        evidence_section = [
            f"_Evidence superseded; this claim is retained for audit and is {review_note}. "
            "It has no active source._"
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
    ]
    # Contradicting-claims projection: only active backlinks render, no placeholder link when
    # empty (the same "no dangling/invented links" discipline as the rest, ADR-0016/0029/0031).
    if contradicts:
        body += [
            "## Contradicting Claims",
            "",
            *(f"- [[Claims/{cid}]]" for cid in contradicts),
            "",
        ]
    body += [
        "## Notes",
        "",
    ]
    draft = "\n".join(fm_lines + body) + "\n"
    fingerprint = _fingerprint(_FP_LINE.sub("", draft))
    return draft.replace('input_fingerprint: ""', f'input_fingerprint: "{fingerprint}"', 1)


def render_concept_page(node: dict[str, Any], *, review_status: str | None = None) -> str:
    """Render a deterministic candidate concept/entity stub page (slice 4, ADR-0017/0018).

    No LLM-authored prose: frontmatter (id, type, title, aliases, status, confidence) plus a
    deterministic summary and a Mentioned-by section projected from the node's active
    `mentions` sources (passed in — no IO). `status` is supplied by the caller (it is
    page-authoritative, ADR-0030): `candidate` until promoted to `active` by recurrence
    (slice 5), or `deprecated_candidate` when no active mention remains (a tombstone, kept
    page-backed and never hard-deleted).

    `review_status` is normally *derived* from `status`/active-mentions; an explicit, validated override
    is the deterministic render-path input the Phase-6 deprecation executor uses to mark an approved
    deprecation (ADR-0035 A5) — the derivation would otherwise yield `pending` for a no-mention node.
    """
    node_type = node["node_type"]
    title = _WS.sub(" ", str(node["title"])).strip()
    aliases = node.get("aliases") or []
    confidence = node.get("confidence", "low")
    source_ids = node.get("source_ids") or []
    active_mentions = bool(source_ids)
    status = node.get("status") or ("candidate" if active_mentions else "deprecated_candidate")
    derived_review_status = "none" if status == "active" else ("none" if active_mentions else "pending")
    rs = _resolve_review_status(review_status, derived_review_status)

    fm_lines = [
        "---",
        f"type: {node_type}",
        f'{node["id_field"]}: "{node["node_id"]}"',
        f'title: "{_fm_quote(title)}"',
        f"status: {status}",
        f"review_status: {rs}",
        "generation_status: deterministic",
        f"confidence: {confidence}",
        f"aliases: {_render_tag_list(aliases)}",
        'input_fingerprint: ""',
        "---",
    ]
    label = status.replace("_", " ")
    if active_mentions:
        summary = f"{label.capitalize()} {node_type} mentioned by {len(source_ids)} source(s)."
        mentioned = [f"- [[Sources/{s}]]" for s in source_ids]
    else:
        # Callout prose tracks the resolved review_status (ADR-0035 A5); default `pending` byte-stable.
        review_note = "approved for deprecation" if rs == "approved" else "pending review"
        summary = f"{node_type.capitalize()} with no active mentions — {review_note}."
        mentioned = [f"_No active source mentions; {review_note}._"]
    alias_lines = [f"- {_delink(a)}" for a in aliases] if aliases else ["_None._"]
    body = [
        "",
        f"# {_delink(title)}",
        "",
        f"> [!summary] {label.capitalize()} {node_type}",
        f"> {summary}",
        "",
        "## Aliases",
        "",
        *alias_lines,
        "",
        "## Mentioned By",
        "",
        *mentioned,
        "",
        "## Notes",
        "",
    ]
    draft = "\n".join(fm_lines + body) + "\n"
    fingerprint = _fingerprint(_FP_LINE.sub("", draft))
    return draft.replace('input_fingerprint: ""', f'input_fingerprint: "{fingerprint}"', 1)


def render_synthesis_page(node: dict[str, Any]) -> str:
    """Render a deterministic cross-source Synthesis page (Phase 3.5c slice 2, ADR-0031).

    The LLM supplies only the `summary`/`synthesis` prose; the renderer composes it with the
    graph-backed grounding — a Supporting Evidence section of `[[Claims/…]]` links matching the
    synthesis's active `derived_from` edges, and a Disagreements section listing the active
    `contradicts` pairs among those claims. Born `status: candidate` (review-gated, no recurrence
    auto-promote — ADR-0031); `active` once a `propose_synthesis` review is approved, or
    `deprecated_candidate` on rejection / when the topic is no longer eligible. No wall-clock —
    freshness lives in `input_fingerprint` (ADR-0023), so a cache-replay re-run is byte-stable."""
    syn_id = node["synthesis_id"]
    title = _WS.sub(" ", str(node["title"])).strip()
    status = node["status"]
    review_status = node.get("review_status", "pending")
    confidence = node.get("confidence", "low")
    topic_node = node.get("topic_node", "")
    summary = _WS.sub(" ", str(node.get("summary", ""))).strip() or "Candidate cross-source synthesis."
    synthesis_text = str(node.get("synthesis_text", "")).strip()
    claim_ids = sorted(node.get("claim_ids", []) or [])
    disagreements = node.get("disagreements", []) or []  # list of (claim_a, claim_b) tuples

    fm_lines = [
        "---",
        "type: synthesis",
        f'synthesis_id: "{syn_id}"',
        f'title: "{_fm_quote(title)}"',
        f"status: {status}",
        f"review_status: {review_status}",
        "generation_status: enriched",
        f"confidence: {confidence}",
        f'topic_node: "{topic_node}"',
    ]
    if claim_ids:
        fm_lines.append("derived_from:")
        fm_lines.extend(f"  - {cid}" for cid in claim_ids)
    else:
        fm_lines.append("derived_from: []")
    fm_lines += ['input_fingerprint: ""', "---"]

    label = {"candidate": "Candidate synthesis", "active": "Synthesis",
             "deprecated_candidate": "Synthesis deprecated — pending review"}.get(status, "Synthesis")
    body = [
        "",
        f"# {_delink(title)}",
        "",
        f"> [!summary] {label}",
        f"> {_delink(summary)}",
        "",
        "## Synthesis",
        "",
        _delink(synthesis_text) if synthesis_text else "_No synthesis text._",
        "",
        "## Supporting Evidence",
        "",
        *([f"- [[Claims/{cid}]]" for cid in claim_ids] or ["_No supporting claims._"]),
        "",
    ]
    if disagreements:
        body += [
            "## Disagreements or Contradictions",
            "",
            *(f"- [[Claims/{a}]] contradicts [[Claims/{b}]]" for a, b in disagreements),
            "",
        ]
    body += ["## Review Notes", ""]
    draft = "\n".join(fm_lines + body) + "\n"
    fingerprint = _fingerprint(_FP_LINE.sub("", draft))
    return draft.replace('input_fingerprint: ""', f'input_fingerprint: "{fingerprint}"', 1)


def render_source_page(
    template: str, manifest: dict[str, Any], normalized_markdown: str, *,
    summary_max: int, summary_min: int,
    enrichment: dict[str, Any] | None = None,
    claims: list[dict[str, Any]] | None = None,
    concepts: list[dict[str, Any]] | None = None,
    entities: list[dict[str, Any]] | None = None,
    people: list[dict[str, Any]] | None = None,
    organizations: list[dict[str, Any]] | None = None,
    projects: list[dict[str, Any]] | None = None,
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
        enrichment=enrichment, claims=claims, concepts=concepts, entities=entities,
        people=people, organizations=organizations, projects=projects,
    )
    draft = render_template(template, {**values, "input_fingerprint": ""})
    fingerprint = _fingerprint(_FP_LINE.sub("", draft))
    return draft.replace('input_fingerprint: ""', f'input_fingerprint: "{fingerprint}"', 1)


def render_query_page(query: dict[str, Any]) -> str:
    """Render a deterministic saved Query page (Phase 5, ADR-0034) from a grounded answer.

    The frontmatter `citations:` list is the machine-readable record (grounded by
    scripts/validate_citations.py::_check_query); the Citations table is a rendered view. The page is
    a navigable answer artifact — `type: query`, `answer_eligible: false`, `derived_from: []`
    (reserved) — and adds NO graph edges. No wall-clock is embedded, so re-saving the same
    question+answer is byte-stable. Ungrounded model text is audit-only: ordinary unsourced claims are
    listed; path-leak/security-rejected claims are summarised by count/reason, never verbatim.
    """
    qid = query["query_id"]
    question = _WS.sub(" ", str(query["question"])).strip()
    answer = str(query.get("answer", "")).strip()
    citations = query.get("citations", []) or []
    modes = query.get("retrieval_modes", []) or []
    title = _query_title(question)
    summary = _delink(_WS.sub(" ", answer))[:280] or "No answer."

    fm_lines = [
        "---",
        "type: query",
        f'query_id: "{qid}"',
        f'title: "{_fm_quote(title)}"',
        f'question: "{_fm_quote(question)}"',
        "status: active",
        "review_status: none",
        "generation_status: enriched",
        "confidence: low",
        "answer_eligible: false",  # a query is a navigable answer artifact, never itself citable
        f"retrieval_modes: [{', '.join(modes)}]",
        "derived_from: []",
    ]
    evidence_rows = []
    if citations:
        fm_lines.append("citations:")
        for c in citations:
            fm_lines.extend([
                f'  - source_id: "{c["source_id"]}"',
                f'    char_start: {c["char_start"]}',
                f'    char_end: {c["char_end"]}',
                f'    page: {c.get("page") if c.get("page") is not None else "null"}',
                f'    page_end: {c.get("page_end") if c.get("page_end") is not None else "null"}',
                f'    section: {_fm_opt(c.get("section"))}',
                f'    table_reference: {_fm_opt(c.get("table_reference"))}',
                f'    sheet_reference: {_fm_opt(c.get("sheet_reference"))}',
                f'    chunk_id: {_fm_opt(c.get("chunk_id"))}',
                f'    quote: "{_fm_quote(c.get("quote", ""))}"',
            ])
            loc = c.get("section") or (f"p.{c['page']}" if c.get("page") is not None else "—")
            evidence_rows.append(
                f'| [[Sources/{c["source_id"]}]] | {_delink(_quote_cell(str(loc)))} | '
                f'{c["char_start"]}–{c["char_end"]} | {_delink(_quote_cell(c.get("quote", "")))} |'
            )
    else:
        fm_lines.append("citations: []")
    fm_lines.append("---")

    citations_section = (["| Source | Page / Section | Char range | Quote |", "|---|---|---|---|",
                          *evidence_rows] if citations else [f"_{NO_SOURCE}_"])
    unsourced = [_delink(_WS.sub(" ", u)) for u in (query.get("unsourced_claims") or [])]
    rejected = int(query.get("security_rejected_count", 0) or 0)
    unsourced_section = [f"- {u}" for u in unsourced]
    if rejected:
        # Path-leak rejections are summarised by reason/count only — never the verbatim text.
        unsourced_section.append(f"- _{rejected} claim(s) withheld (absolute_path_leak)._")
    if not unsourced_section:
        unsourced_section = ["None."]

    body = [
        "",
        f"# Query: {_delink(title)}",
        "",
        "> [!summary]",
        f"> {summary}",
        "",
        "## Question",
        "",
        _delink(question),
        "",
        "## Answer",
        "",
        _delink(answer) if answer else f"_{NO_SOURCE}_",
        "",
        "## Citations",
        "",
        *citations_section,
        "",
        "## Retrieval Path",
        "",
        ", ".join(modes) if modes else "—",
        "",
        "## Unsourced Claims",
        "",
        *unsourced_section,
        "",
    ]
    return "\n".join(fm_lines + body)


def _query_title(question: str) -> str:
    one_line = _WS.sub(" ", question).strip()
    return one_line if len(one_line) <= 80 else one_line[:77].rstrip() + "…"


def _fm_opt(value: Any) -> str:
    """Frontmatter scalar for an optional citation field: null, or a quoted escaped string."""
    return "null" if value is None else f'"{_fm_quote(str(value))}"'
