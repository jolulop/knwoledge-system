#!/usr/bin/env python3
"""Phase 6 governance executor: mark_semantic_duplicate (ADR-0041).

The first non-rekeying governance executor. On an approved `mark_semantic_duplicate` decision it
upserts ONE active, canonical, symmetric `duplicates(min_id, max_id)` assertion (`asserted_by=human`)
between two same-type graph nodes and re-renders both pages so each gains a body-only `## Duplicates`
section. **Pure annotation**: it never rewrites a stable id, changes a node status, suppresses
retrieval, or redirects backlinks — both pages keep all page-owned metadata. Key-free, deterministic,
and previewable via the ADR-0040 dry-run. Steady-state aware (applied/normalized/no-op): a re-apply of
an already-active+projected duplicate is a true no-op (no graph/page write, no `changed_pages`).
Scope-guarded: a malformed/invalid/self/cross-type/unsupported/missing pair is SKIPPED with a reason,
never partial-applied.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.backend import graph
from app.backend.manifests import iso_now
from app.workers import items
from app.workers.wiki_render import NODE_DIR

# Conservative id safety (ADR-0041): reject unsafe/path-like/empty ids before any graph lookup. NOT a
# concept/entity-family prefix grammar (that grammar is not validator-fixed; do not invent one here).
_SAFE_ID = re.compile(r"[A-Za-z0-9_]+")


def _is_safe_id(x: Any) -> bool:
    return isinstance(x, str) and bool(_SAFE_ID.fullmatch(x))


def _edge_active(gconn, a: str, b: str) -> bool:
    """Whether an active canonical `duplicates(a,b)` edge already exists (a < b)."""
    return any(p["node_id"] == b for p in graph.active_duplicates(gconn, a))


def _projects(text: str, partner_slug: str, node_type: str) -> bool:
    """Whether a page renders the partner link *inside its `## Duplicates` section* (ADR-0041).

    Section-scoped, matching `validate_projection`: a partner wikilink elsewhere (e.g. in Notes) does
    NOT count as projected, so a stale page can't be misread as a true no-op. Matches the link
    TARGET, alias-insensitive (ADR-0060: projected links carry a display alias when resolvable)."""
    target = f"{NODE_DIR[node_type]}/{partner_slug}"
    forms = (f"[[{target}]]", f"[[{target}|")
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "## Duplicates")
    except StopIteration:
        return False
    for ln in lines[start + 1:]:
        if ln.startswith("## "):
            break
        if any(form in ln for form in forms):
            return True
    return False


def _approved_marked_duplicates(reviews_dir: Path) -> list[dict[str, Any]]:
    d = Path(reviews_dir) / "approved"
    items: list[dict[str, Any]] = []
    for path in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(item, dict) and item.get("type") == "mark_semantic_duplicate" \
                and item.get("status") == "approved":
            items.append(item)
    return items


def apply_marked_duplicates(gconn, reviews_dir: Path, *, wiki_dir: Path,
                            now: str | None = None) -> dict[str, Any]:
    """Apply approved `mark_semantic_duplicate` decisions. Returns
    `{applied, normalized, skipped:[{review_id, reason}], changed_pages, graph_changed}` (mirrors the
    deprecation executor): `applied` = new edge written, `normalized` = edge already active but a page
    projection repaired, true no-ops counted in neither. The caller owns the single index rebuild/reindex
    and `gconn.commit()` (ADR-0035)."""
    now = now or iso_now()
    applied = normalized = 0
    skipped: list[dict[str, str]] = []
    changed_pages: list[str] = []

    for item in _approved_marked_duplicates(reviews_dir):
        rid = str(item.get("review_id", ""))
        subject = item.get("subject") or {}
        ids = subject.get("node_ids") if isinstance(subject, dict) else None
        # malformed_subject: must be exactly two non-empty string ids
        if not (isinstance(ids, list) and len(ids) == 2
                and all(isinstance(x, str) and x for x in ids)):
            skipped.append({"review_id": rid, "reason": "malformed_subject"})
            continue
        if not all(_is_safe_id(x) for x in ids):
            skipped.append({"review_id": rid, "reason": "invalid_node_id"})
            continue
        a, b = sorted(ids)  # canonical: src_id < dst_id
        if a == b:
            skipped.append({"review_id": rid, "reason": "self_duplicate"})
            continue
        na, nb = graph.get_node(gconn, a), graph.get_node(gconn, b)
        if na is None or nb is None:
            skipped.append({"review_id": rid, "reason": "node_missing"})
            continue
        if na["node_type"] != nb["node_type"]:
            skipped.append({"review_id": rid, "reason": "type_mismatch"})
            continue
        # Same type, but a type with no `## Duplicates` page projection (source/query/synthesis/...):
        # refuse BEFORE any edge write so an unprojectable edge can never land (ADR-0041).
        if na["node_type"] != "item":
            skipped.append({"review_id": rid, "reason": "unsupported_node_type"})
            continue
        # Both pages must exist before we write the edge (atomic: never an edge with no projection).
        nt = na["node_type"]
        page_a = wiki_dir / NODE_DIR[nt] / f"{na['slug']}.md"
        page_b = wiki_dir / NODE_DIR[nt] / f"{nb['slug']}.md"
        meta_a, meta_b = items._read_node_meta(page_a), items._read_node_meta(page_b)
        if meta_a is None or meta_b is None:
            skipped.append({"review_id": rid, "reason": "page_missing"})
            continue

        # Classify steady-state (ADR-0041): applied (new edge) / normalized (edge active, projection
        # stale) / no-op (edge active + both pages already project -> no write, no rebuild).
        already_active = _edge_active(gconn, a, b)
        proj_a = _projects(page_a.read_text(encoding="utf-8"), nb["slug"], nt)
        proj_b = _projects(page_b.read_text(encoding="utf-8"), na["slug"], nt)
        if already_active and proj_a and proj_b:
            continue  # true no-op
        if not already_active:
            graph.upsert_assertion(gconn, src_id=a, dst_id=b, edge_type="duplicates",
                                   asserted_by="human", status="active", review_id=rid, now=now)
        # Re-render both pages, preserving each page's current status + review_status (page-owned);
        # recompose writes only when content differs and returns "written" only then, so changed_pages
        # stays honest (an already-correct page returns "unchanged" and is not counted).
        for nid, n, meta in ((a, na, meta_a), (b, nb, meta_b)):
            if items.recompose_semantic_node_page(
                    gconn, node_id=nid, wiki_dir=wiki_dir,
                    status=meta["status"], review_status=meta["review_status"], now=now) == "written":
                changed_pages.append(f"{NODE_DIR[nt]}/{n['slug']}.md")
        if already_active:
            normalized += 1  # edge was active; only the page projection was repaired
        else:
            applied += 1

    return {"applied": applied, "normalized": normalized, "skipped": skipped,
            "changed_pages": sorted(set(changed_pages)), "graph_changed": bool(applied)}
