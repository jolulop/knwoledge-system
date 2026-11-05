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
from app.workers import apply_outcomes, items
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


def _node_name(wiki_dir: Path, node: dict[str, Any] | None, fallback_id: Any) -> dict[str, str]:
    """A node's display pair from its own page (ADR-0072 decision 3), read before any mutation; the id
    when the node/page/title can't be read. Works for any `NODE_DIR` type (a cross-type pair's skip
    still names both ends)."""
    if not node:
        return apply_outcomes.named(fallback_id, None)
    nt, slug = node.get("node_type"), node.get("slug")
    rel = f"{NODE_DIR[nt]}/{slug}" if nt in NODE_DIR and isinstance(slug, str) and slug else None
    return apply_outcomes.page_name(wiki_dir, rel, fallback_id)


def _projection_holds(gconn, a: str, b: str, page_a: Path, page_b: Path, slug_a: str, slug_b: str,
                      node_type: str) -> bool:
    """The postcondition READ-BACK (B2a review round 1): the canonical edge is active AND both pages
    project the partner. Reporting only — always called through :func:`apply_outcomes.guarded`."""
    return (_edge_active(gconn, a, b)
            and _projects(page_a.read_text(encoding="utf-8"), slug_b, node_type)
            and _projects(page_b.read_text(encoding="utf-8"), slug_a, node_type))


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
    and `gconn.commit()` (ADR-0035).

    Also `outcomes` (ADR-0072 decision 2), one per approved review read, `details` = `subject` +
    `duplicate_of` (the canonical min/max pair), stating the VERIFIED end state (B2a review round 1):
    `already_in_effect` only for a verified no-op (edge active + both pages project, nothing written;
    uncounted); a run that wrote the edge or a page is `applied` once the read-back confirms edge +
    both projections (`details.normalized` for a projection repair, counted `normalized`); `skipped`
    mirrors the typed skips. A page re-render that FAILS after the guards (e.g. `page_conflict`), or a
    read-back that doesn't confirm the postcondition (`postcondition_not_met`) or can't be read
    (`verification_unavailable`), is reported honestly though the legacy counts still count it:
    `incomplete` when the edge or a page was written on this run, else `skipped` (nothing changed)."""
    from app.workers import tag_links                    # lazy: tag_links imports items
    # ADR-0066 review round 10: item-page writers are serialized with tag actions (the ledger lock,
    # re-entrant — a no-op inside run_apply, which already holds it).
    with tag_links.ledger_lock(reviews_dir):
        return _apply_marked_duplicates(gconn, reviews_dir, wiki_dir=wiki_dir, now=now)


def _apply_marked_duplicates(gconn, reviews_dir: Path, *, wiki_dir: Path,
                             now: str | None = None) -> dict[str, Any]:
    """The body of :func:`apply_marked_duplicates` (called under the ledger lock)."""
    now = now or iso_now()
    applied = normalized = 0
    skipped: list[dict[str, str]] = []
    changed_pages: list[str] = []
    outcomes: list[dict[str, Any]] = []
    rtype = "mark_semantic_duplicate"

    def _skip(rid: str, reason: str, **details: Any) -> None:
        skipped.append({"review_id": rid, "reason": reason})
        outcomes.append(apply_outcomes.outcome(rid, rtype, apply_outcomes.SKIPPED, reason=reason,
                                               **details))

    for item in _approved_marked_duplicates(reviews_dir):
        rid = str(item.get("review_id", ""))
        subject = item.get("subject") or {}
        ids = subject.get("node_ids") if isinstance(subject, dict) else None
        # malformed_subject: must be exactly two non-empty string ids
        if not (isinstance(ids, list) and len(ids) == 2
                and all(isinstance(x, str) and x for x in ids)):
            raw = [x for x in ids if isinstance(x, str) and x] if isinstance(ids, list) else []
            _skip(rid, "malformed_subject", subject=apply_outcomes.named(raw[0] if raw else "", None),
                  **({"duplicate_of": apply_outcomes.named(raw[1], None)} if len(raw) > 1 else {}))
            continue
        if not all(_is_safe_id(x) for x in ids):
            # never resolve an unsafe id against the graph or the vault: the ids stand in as names
            _skip(rid, "invalid_node_id", subject=apply_outcomes.named(ids[0], None),
                  duplicate_of=apply_outcomes.named(ids[1], None))
            continue
        a, b = sorted(ids)  # canonical: src_id < dst_id
        if a == b:
            node = apply_outcomes.guarded(graph.get_node, gconn, a)       # reporting-only read
            name = _node_name(wiki_dir, node if isinstance(node, dict) else None, a)
            _skip(rid, "self_duplicate", subject=name, duplicate_of=dict(name))
            continue
        na, nb = graph.get_node(gconn, a), graph.get_node(gconn, b)
        # ADR-0072 d3: both display names, read before any edge/page write
        names = {"subject": _node_name(wiki_dir, na, a), "duplicate_of": _node_name(wiki_dir, nb, b)}
        if na is None or nb is None:
            _skip(rid, "node_missing", **names)
            continue
        if na["node_type"] != nb["node_type"]:
            _skip(rid, "type_mismatch", **names)
            continue
        # Same type, but a type with no `## Duplicates` page projection (source/query/synthesis/...):
        # refuse BEFORE any edge write so an unprojectable edge can never land (ADR-0041).
        if na["node_type"] != "item":
            _skip(rid, "unsupported_node_type", **names)
            continue
        # Both pages must exist before we write the edge (atomic: never an edge with no projection).
        nt = na["node_type"]
        page_a = wiki_dir / NODE_DIR[nt] / f"{na['slug']}.md"
        page_b = wiki_dir / NODE_DIR[nt] / f"{nb['slug']}.md"
        meta_a, meta_b = items._read_node_meta(page_a), items._read_node_meta(page_b)
        if meta_a is None or meta_b is None:
            _skip(rid, "page_missing", **names)
            continue

        # Classify steady-state (ADR-0041): applied (new edge) / normalized (edge active, projection
        # stale) / no-op (edge active + both pages already project -> no write, no rebuild).
        already_active = _edge_active(gconn, a, b)
        proj_a = _projects(page_a.read_text(encoding="utf-8"), nb["slug"], nt)
        proj_b = _projects(page_b.read_text(encoding="utf-8"), na["slug"], nt)
        if already_active and proj_a and proj_b:
            # true no-op (uncounted); postcondition verified: edge active + both pages project
            outcomes.append(apply_outcomes.outcome(rid, rtype, apply_outcomes.ALREADY_IN_EFFECT,
                                                   **names))
            continue
        if not already_active:
            graph.upsert_assertion(gconn, src_id=a, dst_id=b, edge_type="duplicates",
                                   asserted_by="human", status="active", review_id=rid, now=now)
        # Re-render both pages, preserving each page's current status + review_status (page-owned);
        # recompose writes only when content differs and returns "written" only then, so changed_pages
        # stays honest (an already-correct page returns "unchanged" and is not counted).
        renders: list[str] = []
        for nid, n, meta in ((a, na, meta_a), (b, nb, meta_b)):
            render = items.recompose_semantic_node_page(
                gconn, node_id=nid, wiki_dir=wiki_dir,
                status=meta["status"], review_status=meta["review_status"], now=now)
            renders.append(render)
            if render == "written":
                changed_pages.append(f"{NODE_DIR[nt]}/{n['slug']}.md")
        if already_active:
            normalized += 1  # edge was active; only the page projection was repaired
        else:
            applied += 1
        # Reporting only (ADR-0072 d2): a failed re-render means the projection did not land.
        changed = not already_active or "written" in renders     # this run wrote the edge or a page
        failed = [r for r in renders if r not in ("written", "unchanged")]
        if failed:
            if changed:
                outcomes.append(apply_outcomes.outcome(
                    rid, rtype, apply_outcomes.INCOMPLETE, reason=failed[0], **names,
                    edge_written=not already_active))
            else:
                outcomes.append(apply_outcomes.outcome(
                    rid, rtype, apply_outcomes.SKIPPED, reason=failed[0], **names))
            continue
        # Verified end state (B2a review round 1): read back edge + both projections (guarded).
        holds = apply_outcomes.guarded(_projection_holds, gconn, a, b, page_a, page_b,
                                       na["slug"], nb["slug"], nt)
        if holds is apply_outcomes.UNAVAILABLE:
            outcomes.append(apply_outcomes.unverified(rid, rtype, changed=changed, **names))
        elif not holds:
            outcomes.append(apply_outcomes.unverified(
                rid, rtype, changed=changed, reason=apply_outcomes.POSTCONDITION_NOT_MET, **names))
        elif not changed:
            # nothing written on this run and the postcondition reads back: a verified no-op
            outcomes.append(apply_outcomes.outcome(rid, rtype, apply_outcomes.ALREADY_IN_EFFECT,
                                                   **names))
        else:
            outcomes.append(apply_outcomes.outcome(
                rid, rtype, apply_outcomes.APPLIED, **names,
                **({"normalized": True} if already_active else {})))

    return {"applied": applied, "normalized": normalized, "skipped": skipped,
            "changed_pages": sorted(set(changed_pages)), "graph_changed": bool(applied),
            "outcomes": outcomes}
