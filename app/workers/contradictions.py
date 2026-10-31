#!/usr/bin/env python3
"""Contradiction resolution: applying human decisions on `contradicts` assertions (ADR-0030/0031).

What remains here after ADR-0069 slice 0 is the deterministic, model-free half: Apply's executor
for approved/rejected `resolve_contradiction` items (`apply_contradiction_decisions`, composing
`apply_resolved_contradictions` with the Claim-page re-projection) and the `supersede` effects
(ADR-0044). An assertion is one sorted-pair `contradicts` edge (`src_id < dst_id`), born `proposed`,
invisible until a human approves it (ADR-0030); the LLM proposes, the human disposes (CLAUDE.md
rule 9).

The whole-vault judging sweep (graph-neighbourhood blocking over every claim pair, one tier-3 call
per pair) was removed by ADR-0069 slice 0: under ADR-0066 items are broad user tags, so it no longer
bounded the work (the part-3 UAT built 10,500 pairs). Contradiction checks return as an explicit,
user-selected document scope (estimate → token → run, windowed batch judging) in slice 2(b), and
model-free contradiction maintenance in slice 2(a). Nothing in this module calls a model.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.backend import graph
from app.backend.manifests import iso_now
from app.workers import claims, reviews

_CLAIM_TEXT_RE = re.compile(r'(?m)^claim_text:\s*"(.*)"\s*$')


# --- claim context (durable wording + reconstructed citations) -------------


def _read_claim_text(page_path: Path) -> str | None:
    """Read the durable claim_text from a Claim page's frontmatter (page is the authority)."""
    if not page_path.exists():
        return None
    m = _CLAIM_TEXT_RE.search(page_path.read_text(encoding="utf-8", errors="replace"))
    return re.sub(r"\\(.)", r"\1", m.group(1)) if m else None


def _claim_context(gconn, cid: str, *, claims_dir: Path, markdown_dir: Path) -> dict[str, Any] | None:
    """A claim's review-facing context: durable text + citations reconstructed from its active
    `derived_from` edges (quotes read back from the source spans). None if it has no wording."""
    claim_text = _read_claim_text(claims_dir / f"{cid}.md")
    if claim_text is None:
        return None
    cites: list[dict[str, Any]] = []
    for e in graph.outgoing_active(gconn, cid):
        if e["edge_type"] != "derived_from":
            continue
        src, start, end = e["dst_id"], e["evidence_char_start"], e["evidence_char_end"]
        md_path = markdown_dir / f"{src}.md"
        md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
        quote = md[start:end] if start is not None and end is not None and end <= len(md) else ""
        cites.append({"source_id": src, "char_start": start, "char_end": end, "quote": quote})
    cites.sort(key=lambda c: (c["source_id"], c["char_start"] if c["char_start"] is not None else -1))
    return {"claim_id": cid, "claim_text": claim_text, "citations": cites}


# --- resolution application (deterministic effect of human decisions) ------


def _resolved_items(reviews_dir: Path, state: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    d = reviews_dir / state
    if not d.exists():
        return out
    for path in sorted(d.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if item.get("type") == "resolve_contradiction":
            out.append(item)
    return out


def apply_resolved_contradictions(
    gconn, reviews_dir: Path, *, claims_dir: Path, markdown_dir: Path,
    affected: set[str] | None = None, now: str | None = None,
) -> dict[str, int]:
    """Apply human decisions to the graph (deterministic, key-free; Apply's executor).

    `approved` → the pair's `contradicts` edge flips to `active` (acknowledge — a standing
    disagreement; both claims stay live). `rejected` → the edge flips to `rejected`. A
    `supersede` decision (an approved item naming a `winner`) activates the edge **and** executes
    the winner→loser effects (slice 1b, ADR-0031): an `active` `supersedes` edge plus the loser
    deprecated to `deprecated_candidate` through the `deprecate_wiki_page` audit path (the
    `contradicts` edge stays active so the historical conflict is recorded). Idempotent: only
    transitions a row whose status differs, and the supersede effects are applied once. Both
    endpoint claims of any transition are added to `affected` so the caller re-projects their
    Claim pages (a backlink appears on acknowledge, disappears on reject).
    """
    now = now or iso_now()
    affected = affected if affected is not None else set()
    acknowledged = rejected = superseded_executed = 0

    def _set(a: str, b: str, rows: list[dict[str, Any]], target: str, statuses: tuple[str, ...]) -> int:
        n = 0
        for r in rows:
            if r["status"] in statuses and r["status"] != target:
                graph.set_status(gconn, r["edge_id"], target, now=now)
                affected.update((a, b))
                n += 1
        return n

    for item in _resolved_items(reviews_dir, "approved"):
        subj = item.get("subject") or {}
        a, b = subj.get("claim_a"), subj.get("claim_b")
        if not a or not b:
            continue
        acknowledged += _set(a, b, graph.contradiction_between(gconn, a, b), "active", ("proposed",))
        winner = item.get("winner")
        if winner in (a, b):  # supersede decision: execute winner→loser effects (slice 1b)
            loser = b if winner == a else a
            if _execute_supersede(gconn, reviews_dir, winner=winner, loser=loser,
                                  source_review_id=item.get("review_id", ""), now=now,
                                  claims_dir=claims_dir, markdown_dir=markdown_dir):
                superseded_executed += 1
            affected.update((a, b))
    for item in _resolved_items(reviews_dir, "rejected"):
        subj = item.get("subject") or {}
        a, b = subj.get("claim_a"), subj.get("claim_b")
        if not a or not b:
            continue
        rejected += _set(a, b, graph.contradiction_between(gconn, a, b), "rejected", ("proposed", "active"))

    return {"acknowledged": acknowledged, "rejected": rejected,
            "superseded_executed": superseded_executed}


def _reproject_claim_pages(gconn, cids, *, claims_dir: Path, reviews_dir: Path, markdown_dir: Path,
                           now: str) -> list[str]:
    """Re-render the given Claim pages from the graph; return the cids whose page was (re)written.

    The re-projection unit of `apply_contradiction_decisions`, kept separate so later contradiction
    maintenance (ADR-0069 slice 2(a)) reuses the same render logic (ADR-0035 A4). No index rebuild
    here (the caller owns the single rebuild)."""
    written: list[str] = []
    for cid in sorted(cids):
        if claims.recompose_claim(gconn, cid=cid, claims_dir=claims_dir, reviews_dir=reviews_dir,
                                  markdown_dir=markdown_dir, now=now) == "written":
            written.append(cid)
    return written


def apply_contradiction_decisions(gconn, reviews_dir: Path, *, claims_dir: Path, markdown_dir: Path,
                                  now: str | None = None) -> dict[str, Any]:
    """Key-free Phase-6 apply: record contradiction decisions to the graph + re-project the affected
    Claim pages (ADR-0035 A4). Composes the existing `apply_resolved_contradictions` executor with the
    shared `_reproject_claim_pages` helper. **No index
    rebuild** (the caller — `/reviews/apply` — owns the single final rebuild). Returns
    `{resolution, changed_pages, graph_changed}`."""
    now = now or iso_now()
    affected: set[str] = set()
    resolution = apply_resolved_contradictions(
        gconn, reviews_dir, claims_dir=claims_dir, markdown_dir=markdown_dir, affected=affected, now=now)
    active_claims = set(graph.active_node_ids_of_type(gconn, "claim"))
    changed_pages = _reproject_claim_pages(
        gconn, affected & active_claims, claims_dir=claims_dir, reviews_dir=reviews_dir,
        markdown_dir=markdown_dir, now=now)
    graph_changed = bool(resolution["acknowledged"] or resolution["rejected"]
                         or resolution["superseded_executed"])
    return {"resolution": resolution, "changed_pages": changed_pages, "graph_changed": graph_changed}


def _execute_supersede(
    gconn, reviews_dir: Path, *, winner: str, loser: str, source_review_id: str, now: str,
    claims_dir: Path, markdown_dir: Path,
) -> bool:
    """Execute an approved `supersede` resolution (slice 1b, ADR-0031): an `active` `supersedes`
    edge winner→loser, and the loser deprecated to `deprecated_candidate` via the
    `deprecate_wiki_page` audit path — authorized by the contradiction approval (no second human
    gate), with the cause recorded. The `contradicts` edge is left active by the caller. Returns
    True if it applied effects this call; idempotent (a no-op once already executed)."""
    already_edge = any(e["dst_id"] == loser and e["edge_type"] == "supersedes"
                       for e in graph.outgoing_active(gconn, winner))
    loser_node = graph.get_node(gconn, loser)
    already_dep = bool(loser_node) and loser_node["status"] == "deprecated_candidate"
    if already_edge and already_dep:
        return False
    # supersedes is a same-node-type edge (claim -> claim); both endpoints are indexed.
    graph.upsert_assertion(gconn, src_id=winner, dst_id=loser, edge_type="supersedes",
                           asserted_by="human", status="active", review_id=source_review_id, now=now)
    # Deprecate the loser (page is the status authority); its evidence + contradiction backlink
    # stay rendered, status flips to deprecated_candidate.
    claims.recompose_claim(gconn, cid=loser, claims_dir=claims_dir, reviews_dir=reviews_dir,
                           markdown_dir=markdown_dir, now=now, deprecate=True)
    # Audit trail: the deprecation is authorized by the approved contradiction resolution, so we
    # file a deprecate_wiki_page item and immediately resolve it approved (records the cause).
    dep_rid = reviews.create_review_item(
        reviews_dir, review_type="deprecate_wiki_page",
        subject={"node_id": loser, "page": f"Claims/{loser}.md"},
        proposal={"to_status": "deprecated_candidate",
                  "reason": "superseded via approved contradiction resolution"},
        # node_type lets the Phase-6 deprecation executor recognise this as a claim deprecation
        # (already effected here) without inferring it from the page dir (ADR-0035 A5).
        context={"resolved_by_review": source_review_id, "winner": winner, "node_type": "claim"},
        now=now)
    reviews.resolve_review_item(
        reviews_dir, dep_rid, decision="approved", decided_by="contradiction_resolution",
        note=f"deprecated as loser of approved contradiction {source_review_id}; winner {winner}",
        now=now)
    return True
