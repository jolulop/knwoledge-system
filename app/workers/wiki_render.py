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

from app.backend import taxonomy

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
# ADR-0059: all knowledge items live in ONE flat directory regardless of item_type — the
# path never encodes classification, so a retype is a metadata flip, never a page move.
NODE_DIR = {
    "source": "Sources", "item": "Items", "claim": "Claims",
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


def display_link_label(title: str) -> str:
    """The rendered link label (ADR-0060 decision 2a): wikilink-safe display text for
    `[[target|label]]` position — de-linked, no `[]|`, collapsed, capped at 78 chars.

    The single family-wide seam (promoted from the claim-specific `_claim_alias`): every
    generated link alias flows through here, and the `display_alias_rot` lint compares
    against THIS rendering of the target's current label — never the raw frontmatter
    `title:`, which stays full-length for search (the two-layer label contract)."""
    safe = _delink(_WS.sub(" ", str(title))).replace("[", "").replace("]", "").replace("|", " ")
    safe = _WS.sub(" ", safe).strip()
    return (safe[:77].rstrip() + "…") if len(safe) > 78 else safe


def _wl(target: str, labels: dict[str, str] | None) -> str:
    """A wikilink carrying its display alias when the target's label is known (ADR-0060).

    `labels` maps link targets ("Sources/src_x", "Claims/clm_x", "Items/<slug>") to display
    labels, resolved worker-side (labels.display_labels) so renderers stay IO-free. No label
    -> bare link, legal under validate_link_aliases only when the target has none resolvable."""
    label = (labels or {}).get(target, "")
    rendered = display_link_label(label) if str(label).strip() else ""
    return f"[[{target}|{rendered}]]" if rendered else f"[[{target}]]"


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
        alias = display_link_label(it.get("title")) if it.get("title") else ""
        lines.append(f"- [[{target}|{alias}]]" if alias else f"- [[{target}]]")
    return "\n".join(lines)


def _mention_slugs(items: list[dict[str, Any]] | None) -> list[str]:
    """Slugs from projected mention items (for the machine-readable frontmatter arrays)."""
    return [it["target"].rsplit("/", 1)[-1] for it in (items or [])]


def _items_block(items: list[dict[str, Any]] | None) -> str:
    """Source-page Items section: mention links grouped by `item_type` (ADR-0059).

    Groups render in the taxonomy GROUP_ORDER with display-name sub-headers; the sentinel's
    group renders last under its QA label ("Unclassified (review required)") — never as a
    taxonomy category. Empty/None -> the deterministic placeholder (byte-stable with the
    Phase-3 backbone). Items whose type is unknown (drifted page) sort into the QA bucket
    rather than being dropped — the projection must stay exact.
    """
    if not items:
        return _PENDING
    groups: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        itype = it.get("item_type")
        key = itype if itype in taxonomy.ITEM_TYPES else taxonomy.UNCLASSIFIED
        groups.setdefault(key, []).append(it)
    blocks = []
    for itype in taxonomy.GROUP_ORDER:
        members = groups.get(itype)
        if members:
            blocks.append(f"### {taxonomy.display_name(itype)}\n\n{_link_list(members)}")
    return "\n\n".join(blocks)


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
    items: list[dict[str, Any]] | None = None,
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
        "items_block": _items_block(items),
        # The frontmatter array mirrors the projected body links as an **advisory** slug
        # projection — NOT relationship authority. The id-keyed graph is the source of truth
        # for mentions (ADR-0029/0030); on rename/re-slug these re-project from the graph.
        # validate_projection enforces frontmatter == body so they never drift.
        "items_fm": _render_tag_list(_mention_slugs(items)),
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


def render_claim_page(claim: dict[str, Any], *, review_status: str | None = None,
                      labels: dict[str, str] | None = None) -> str:
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
    # `hidden` (ADR-0048) is a governance status with precedence over the evidence-derived lifecycle: a
    # hidden claim keeps its evidence/citations rendered but is suppressed from default discovery via the
    # status frontmatter. The executor only hides an active (evidenced) claim.
    hidden = bool(claim.get("hidden"))
    deprecated = bool(claim.get("deprecated"))
    if hidden:
        status, derived_review_status = "hidden", "approved"
    elif not active:
        status, derived_review_status = "deprecated_candidate", "pending"
    elif deprecated:
        status, derived_review_status = "deprecated_candidate", "approved"
    else:
        status, derived_review_status = "active", "none"
    rs = _resolve_review_status(review_status, derived_review_status)

    # ADR-0060: display-only projection — `title:` is derived from claim_text (never authored),
    # and `aliases:` carries exactly that one entry so Obsidian's quick switcher matches the
    # id-named file by readable text. claim_text stays the wording authority.
    title = _claim_title(claim_text)
    fm_lines = [
        "---",
        "type: claim",
        f'claim_id: "{claim_id}"',
        f'title: "{_fm_quote(title)}"',
        f"status: {status}",
        f"review_status: {rs}",
        "generation_status: enriched",
        f"confidence: {confidence}",
        f"aliases: {_render_tag_list([title])}",
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
            src_link = _wl(f'Sources/{c["source_id"]}', labels)
            evidence_rows.append(
                f'| {src_link} | {c["char_start"]}–{c["char_end"]} | '
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

    if hidden:
        # ADR-0048: a hidden claim is governance-suppressed regardless of evidence. With evidence it shows
        # its Evidence table; without it (evidence lost while hidden) it renders an explicit no-evidence
        # note — NOT the "approved deprecation" prose (it isn't deprecated). hidden + no citations is
        # validator-legal (scripts/validate_citations.py).
        label = "Claim hidden — suppressed from default discovery"
        evidence_section = (["| Source | Char range | Quote |", "|---|---|---|", *evidence_rows] if active
                            else ["_This claim is hidden (suppressed from default discovery) and currently "
                                  "has no active source evidence._"])
    elif active:
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
            *(f"- {_wl(f'Claims/{cid}', labels)}" for cid in contradicts),
            "",
        ]
    body += [
        "## Notes",
        "",
    ]
    draft = "\n".join(fm_lines + body) + "\n"
    fingerprint = _fingerprint(_FP_LINE.sub("", draft))
    return draft.replace('input_fingerprint: ""', f'input_fingerprint: "{fingerprint}"', 1)


def render_item_page(node: dict[str, Any], *, review_status: str | None = None,
                     labels: dict[str, str] | None = None) -> str:
    """Render a deterministic knowledge-item stub page (ADR-0059; mechanics from ADR-0017/0018).

    No LLM-authored prose: frontmatter (item_id, item_type, title, aliases, status, confidence)
    plus a deterministic summary and a Mentioned-by section projected from the node's active
    `mentions` sources (passed in — no IO). `status` is supplied by the caller (it is
    page-authoritative, ADR-0030): `candidate` until promoted to `active` by recurrence,
    or `deprecated_candidate` when no active mention remains (a tombstone, kept
    page-backed and never hard-deleted). `item_type` is governed classification metadata
    (taxonomy.py) — page-authoritative, mirrored to the graph nodes index, changed only by
    the `change_item_type` executor (a metadata flip, never an identity change).

    `review_status` is normally *derived* from `status`/active-mentions; an explicit, validated override
    is the deterministic render-path input the Phase-6 deprecation executor uses to mark an approved
    deprecation (ADR-0035 A5) — the derivation would otherwise yield `pending` for a no-mention node.
    """
    title = _WS.sub(" ", str(node["title"])).strip()
    item_type = node["item_type"]
    aliases = node.get("aliases") or []
    confidence = node.get("confidence", "low")
    source_ids = node.get("source_ids") or []
    active_mentions = bool(source_ids)
    status = node.get("status") or ("candidate" if active_mentions else "deprecated_candidate")
    if status == "merged":
        # ADR-0050 merge tombstone: the absorbed id is kept at its old path (old links resolve here) with
        # the FULL frontmatter schema preserved + `merged_into`; only the body collapses to a redirect note.
        merged_into = node.get("merged_into", "")
        link = node.get("merged_into_link")            # "Items/<survivor-slug>" of the active survivor
        target = _wl(link, labels) if link else merged_into
        fm_lines = [
            "---",
            "type: item",
            f'item_id: "{node["node_id"]}"',
            f"item_type: {item_type}",
            f'title: "{_fm_quote(title)}"',
            "status: merged",
            "review_status: approved",
            "generation_status: deterministic",
            f"confidence: {confidence}",
            f"aliases: {_render_tag_list(aliases)}",
            f'merged_into: "{merged_into}"',
            f'merged_at: "{node.get("merged_at", "")}"',
            f'merge_review_id: "{node.get("merge_review_id", "")}"',
            'input_fingerprint: ""',
            "---",
        ]
        body = [
            "",
            f"# {_delink(title)}",
            "",
            "> [!summary] Merged item",
            f"> This item was merged into {target} — no longer a live identity.",
            "",
            f"Merged into {target}.",
            "",
        ]
        draft = "\n".join(fm_lines + body) + "\n"
        fingerprint = _fingerprint(_FP_LINE.sub("", draft))
        return draft.replace('input_fingerprint: ""', f'input_fingerprint: "{fingerprint}"', 1)
    derived_review_status = "none" if status == "active" else ("none" if active_mentions else "pending")
    rs = _resolve_review_status(review_status, derived_review_status)

    fm_lines = [
        "---",
        "type: item",
        f'item_id: "{node["node_id"]}"',
        f"item_type: {item_type}",
        f'title: "{_fm_quote(title)}"',
        f"status: {status}",
        f"review_status: {rs}",
        "generation_status: deterministic",
        f"confidence: {confidence}",
        f"aliases: {_render_tag_list(aliases)}",
        # ADR-0052: page-preserved split lineage on a spin-off — optional, so a non-split node emits
        # nothing (byte-stable, no fingerprint churn) and any re-render must thread these through.
        *([f'split_from: "{node["split_from"]}"'] if node.get("split_from") else []),
        *([f'split_review_id: "{node["split_review_id"]}"'] if node.get("split_review_id") else []),
        # ADR-0058: page-owned human description (approve-with-amendments) — optional and
        # page-preserved like split lineage; every re-render must thread it through.
        *([f'description: "{_fm_quote(node["description"])}"'] if node.get("description") else []),
        'input_fingerprint: ""',
        "---",
    ]
    label = status.replace("_", " ")
    type_label = str(item_type).replace("_", " ")
    if active_mentions:
        summary = f"{label.capitalize()} item ({type_label}) mentioned by {len(source_ids)} source(s)."
        mentioned = [f"- {_wl(f'Sources/{s}', labels)}" for s in source_ids]
    else:
        # Callout prose tracks the resolved review_status (ADR-0035 A5); default `pending` byte-stable.
        review_note = "approved for deprecation" if rs == "approved" else "pending review"
        summary = f"Item ({type_label}) with no active mentions — {review_note}."
        mentioned = [f"_No active source mentions; {review_note}._"]
    alias_lines = [f"- {_delink(a)}" for a in aliases] if aliases else ["_None._"]
    description = node.get("description")
    body = [
        "",
        f"# {_delink(title)}",
        "",
        f"> [!summary] {label.capitalize()} item — {type_label}",
        f"> {summary}",
        "",
        *(["## Description", "", _delink(str(description)), ""] if description else []),
        "## Aliases",
        "",
        *alias_lines,
        "",
        "## Mentioned By",
        "",
        *mentioned,
        "",
    ]
    # Body-only `## Duplicates` projection (ADR-0041): rendered ONLY when the node has ≥1 active
    # `duplicates` edge; omitted otherwise (no placeholder), like the Claim Contradicting-Claims section.
    # Follows the active edge regardless of either node's lifecycle status; human-navigation only.
    duplicates = node.get("duplicates") or []
    if duplicates:
        body += [
            "## Duplicates",
            "",
            *(f"- {_wl(NODE_DIR[d['node_type']] + '/' + d['slug'], labels)}" for d in duplicates),
            "",
        ]
    body += [
        "## Notes",
        "",
    ]
    draft = "\n".join(fm_lines + body) + "\n"
    fingerprint = _fingerprint(_FP_LINE.sub("", draft))
    return draft.replace('input_fingerprint: ""', f'input_fingerprint: "{fingerprint}"', 1)


def render_synthesis_page(node: dict[str, Any], *, labels: dict[str, str] | None = None) -> str:
    """Render a deterministic cross-source Synthesis page (Phase 3.5c slice 2, ADR-0031).

    The LLM supplies only the `summary`/`synthesis` prose; the renderer composes it with the
    graph-backed grounding — a Supporting Evidence section of `[[Claims/…]]` links matching the
    synthesis's active `derived_from` edges **whose claim endpoint is not hidden** (ADR-0049: hidden
    claims are suppressed from this default-discovery surface; the edge stays in the graph), and a
    Disagreements section listing the active
    `contradicts` pairs among those claims. Born `status: candidate` (review-gated, no recurrence
    auto-promote — ADR-0031); `active` once a `propose_synthesis` review is approved, or
    `deprecated_candidate` on rejection / when the topic is no longer eligible. No wall-clock —
    freshness lives in `input_fingerprint` (ADR-0023), so a cache-replay re-run is byte-stable."""
    syn_id = node["synthesis_id"]
    title = _WS.sub(" ", str(node["title"])).strip()
    status = node["status"]
    # Route through the shared gate (ADR-0022) like claim/concept so a synthesis page can never emit a
    # value outside the page set {none,pending,approved,rejected}; a stored `deferred` (ledger-only) or
    # bogus value raises rather than silently rendering. No node value -> derived default `pending`.
    review_status = _resolve_review_status(node.get("review_status"), "pending")
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
        # ADR-0060: full-title alias (never truncated here — the search surface) so the
        # quick switcher matches the id-named file; link-position labels are capped separately.
        f"aliases: {_render_tag_list([title])}",
        f'topic_node: "{topic_node}"',
    ]
    if claim_ids:
        fm_lines.append("derived_from:")
        fm_lines.extend(f"  - {cid}" for cid in claim_ids)
    else:
        fm_lines.append("derived_from: []")
    fm_lines += ['input_fingerprint: ""', "---"]

    # ADR-0049: `hidden` is a governance visibility-suppression status. A hidden synthesis keeps its
    # Supporting Evidence + Disagreements sections rendered (the page is the durable inspection record;
    # graph is SoT) under a prominent suppression banner — hide suppresses *discovery*, not the record.
    label = {"candidate": "Candidate synthesis", "active": "Synthesis",
             "deprecated_candidate": "Synthesis deprecated — pending review",
             "hidden": "Synthesis hidden — suppressed from default discovery",
             # ADR-0049: auto-suppressed because a supporting claim is hidden (not an operator hide).
             "evidence_hidden": "Synthesis suppressed — supporting evidence hidden"}.get(status, "Synthesis")
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
        *([f"- {_wl(f'Claims/{cid}', labels)}" for cid in claim_ids] or ["_No supporting claims._"]),
        "",
    ]
    if disagreements:
        body += [
            "## Disagreements or Contradictions",
            "",
            *(f"- {_wl(f'Claims/{a}', labels)} contradicts {_wl(f'Claims/{b}', labels)}"
              for a, b in disagreements),
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
    items: list[dict[str, Any]] | None = None,
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
        enrichment=enrichment, claims=claims, items=items,
    )
    draft = render_template(template, {**values, "input_fingerprint": ""})
    fingerprint = _fingerprint(_FP_LINE.sub("", draft))
    return draft.replace('input_fingerprint: ""', f'input_fingerprint: "{fingerprint}"', 1)


def render_query_page(query: dict[str, Any], *, labels: dict[str, str] | None = None) -> str:
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
        # ADR-0060: full-title alias — question-shaped titles must stay quick-switcher-matchable.
        f"aliases: {_render_tag_list([title])}",
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
            src_link = _wl(f'Sources/{c["source_id"]}', labels)
            evidence_rows.append(
                f'| {src_link} | {_delink(_quote_cell(str(loc)))} | '
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
