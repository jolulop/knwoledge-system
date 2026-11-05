#!/usr/bin/env python3
"""Phase 6 governance executor: change_item_type (ADR-0059).

The **non-rekeying** replacement for ADR-0051's `change_entity_subtype`: under the
type-neutral `itm_` id, a classification change is a **metadata flip** — page frontmatter
`item_type` + graph nodes-table mirror + audit — with **no id change, no page move, no edge
re-point, no tombstone**. Merge (ADR-0050) and split (ADR-0052) remain the only identity
surgery. Applying one retype withdraws competing pending/deferred retypes of the same node
(ADR-0051's rule carried over); everything else referencing the node stays valid because the
id never changes.

The sentinel `unclassified_review_required` is **never a valid target** (a retype clears
uncertainty, it never introduces it); clearing a sentinel-typed candidate is this executor's
main job besides ordinary misclassification fixes. Mentioning Source pages re-render via the
caller fan-out (`affected_sources`) because their Items section groups by item_type.
Graph-REQUIRED, key-free, deterministic; typed scope-skips, never a partial apply.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from app.backend import graph, taxonomy
from app.backend.manifests import iso_now
from app.workers import apply_outcomes as oc
from app.workers import item_pages, items, reviews
from app.workers.wiki_render import NODE_DIR, render_item_page

_RETYPABLE_STATUSES = frozenset({"active", "candidate"})   # live statuses only (ADR-0051 decision D)
# Canonical item id (ADR-0059/0021): the type-neutral prefix + 16-hex frozen name-hash. A
# malformed/tampered id must never reach a page path.
_CANONICAL_ITEM_ID = re.compile(r"itm_[0-9a-f]{16}")


def _is_canonical_item_id(x: Any) -> bool:
    return isinstance(x, str) and bool(_CANONICAL_ITEM_ID.fullmatch(x))


def _approved_retypes(reviews_dir: Path) -> list[dict[str, Any]]:
    d = Path(reviews_dir) / "approved"
    out: list[dict[str, Any]] = []
    for path in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (isinstance(item, dict) and item.get("type") == "change_item_type"
                and item.get("status") == "approved"):
            out.append(item)
    return out


def _withdraw_competing_retypes(reviews_dir: Path, node_id: str, keep_rid: str, now: str) -> list[str]:
    """Withdraw unresolved (pending/deferred) `change_item_type` items for the same node.

    Targeted, unlike merge/rekey's all-subjects withdrawal: the node id stays live, so its
    other reviews (promotes, deprecates, …) keep their premise — only the competing
    classification proposals are superseded by the applied one."""
    withdrawn: list[str] = []
    pend = Path(reviews_dir) / "pending"
    for path in sorted(pend.glob("*.json")) if pend.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(item, dict) or item.get("type") != "change_item_type":
            continue
        rid = item.get("review_id")
        if rid == keep_rid or not rid:
            continue
        if (item.get("subject") or {}).get("node_id") == node_id:
            if reviews.withdraw_review_item(reviews_dir, rid, reason="superseded_by_retype", now=now):
                withdrawn.append(rid)
    return withdrawn


def _write_audit(reviews_dir: Path, rid: str, record: dict[str, Any], now: str) -> None:
    audit = Path(reviews_dir) / "audit_log"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / f"{rid}-retyped-{uuid.uuid4().hex[:8]}.json").write_text(
        json.dumps({"review_id": rid, "decision": "retyped", "decided_by": "system",
                    "decided_at": now, **record}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def retype_check(gconn, item: dict[str, Any], *, wiki_dir: Path) -> tuple[str, Any]:
    """Where one approved `change_item_type` stands, by the executor's own rules (ADR-0066 review
    round 10: shared with the approved queue's "applied" column so the two can't drift):
    `("skip", reason)` — apply would skip it (malformed, node/page missing, not retypable);
    `("done", None)` — the page already carries the target type (apply is a no-op);
    `("todo", ctx)` — apply has work to do (`ctx` = node, page_rel, page_path, meta, from/to type)."""
    subj = item.get("subject") or {}
    proposal = item.get("proposal") or {}
    nid = subj.get("node_id")
    to_type = subj.get("to_item_type")
    if not taxonomy.is_production_item_type(to_type):       # sentinel is never a valid target
        return "skip", "invalid_to_item_type"
    if proposal.get("to_item_type") != to_type:              # proposal must agree with the subject
        return "skip", "to_item_type_mismatch"
    if not _is_canonical_item_id(nid):                       # canonical id before any page path (security)
        return "skip", "noncanonical_node_id"
    node = graph.get_node(gconn, nid)
    if node is None:
        return "skip", "node_missing"
    if node["node_type"] != "item":
        return "skip", "out_of_scope"
    if node["status"] not in _RETYPABLE_STATUSES:
        return "skip", "node_not_retypable"
    page_rel = f"{NODE_DIR['item']}/{node['slug']}.md"
    page_path = Path(wiki_dir) / page_rel
    if (problem := item_pages.page_problem(gconn, wiki_dir, nid, node["slug"])) is not None:
        return "skip", problem["reason"]         # ADR-0070 B1 review: never another item's page
    meta = items._read_node_meta(page_path)
    if meta is None:
        return "skip", "page_missing"
    from_type = meta.get("item_type") or node.get("item_type")
    if from_type == to_type:
        return "done", None             # already in the target state — idempotent no-op (re-apply safe)
    return "todo", {"nid": nid, "to_type": to_type, "node": node, "page_rel": page_rel,
                    "page_path": page_path, "meta": meta, "from_type": from_type}


def apply_retypes(gconn, reviews_dir: Path, *, wiki_dir: Path, now: str | None = None) -> dict[str, Any]:
    """Apply approved `change_item_type` decisions (ADR-0059). Returns
    `{applied, skipped:[{review_id, reason}], changed_pages, graph_changed, affected_sources,
    outcomes}` — `outcomes` has ONE ADR-0072 record per approved retype (`applied` = the applied
    outcomes, every `skipped` entry a `skipped` outcome; a page already at the target type whose graph
    mirror disagrees is a `skipped` outcome, `graph_item_type_mismatch`, the legacy list never
    carried). Graph-REQUIRED; the caller owns the final commit + the Source-page re-render of
    `affected_sources` (their Items sections group by item_type)."""
    from app.workers import tag_links                    # lazy: tag_links imports items/promote
    # ADR-0066 review round 10: a retype reads the item page and writes it back, so it is serialized
    # with tag actions (an alias edit in between would be overwritten) — from the read to the write.
    with tag_links.ledger_lock(reviews_dir):
        return _apply_retypes(gconn, reviews_dir, wiki_dir=wiki_dir, now=now)


def _apply_retypes(gconn, reviews_dir: Path, *, wiki_dir: Path, now: str | None) -> dict[str, Any]:
    """The body of :func:`apply_retypes` (called under the ledger lock)."""
    now = now or iso_now()
    applied = 0
    skipped: list[dict[str, str]] = []
    changed_pages: list[str] = []
    affected_sources: set[str] = set()
    outcomes: list[dict[str, Any]] = []

    for item in _approved_retypes(reviews_dir):
        rid = str(item.get("review_id", ""))
        state, ctx = retype_check(gconn, item, wiki_dir=wiki_dir)
        details, page_type = _outcome_details(gconn, item, wiki_dir=wiki_dir,
                                              ctx=ctx if state == "todo" else None)
        if state == "skip":
            skipped.append({"review_id": rid, "reason": ctx})
            problem = oc.guarded(_ownership_problem, gconn, item, wiki_dir=wiki_dir, reason=ctx)
            outcomes.append(oc.outcome(rid, "change_item_type", oc.SKIPPED, reason=ctx, **details,
                                       **({"problem": problem} if problem else {})))
            continue
        if state == "done":
            outcomes.append(_done_outcome(gconn, item, rid, details, page_type))
            continue
        nid, to_type, node = ctx["nid"], ctx["to_type"], ctx["node"]
        page_rel, meta, from_type = ctx["page_rel"], ctx["meta"], ctx["from_type"]

        # --- APPLY: the metadata flip — re-render the page with the new item_type (status,
        # review_status, and every page-owned field preserved), mirror the graph row, done. ---
        node_sources, suggested = graph.tag_projection(gconn, nid, node["status"])
        duplicates = graph.active_duplicates(gconn, nid)
        rendered = render_item_page({
            "node_id": nid, "item_type": to_type,
            "title": meta["title"], "aliases": meta["aliases"],
            "confidence": meta.get("confidence", "low"),
            "source_ids": node_sources, "suggested_ids": suggested, "status": node["status"],
            "duplicates": duplicates,
            "split_from": meta.get("split_from"),
            "split_review_id": meta.get("split_review_id"),
            "description": meta.get("description"),
        }, labels=items._link_labels(wiki_dir, node_sources + suggested, duplicates))
        item_pages.write_page(gconn, wiki_dir, nid, node["slug"], rendered)
        graph.upsert_node(gconn, node_id=nid, node_type="item", slug=node["slug"],
                          status=node["status"], item_type=to_type, now=now)
        changed_pages.append(page_rel)
        sources = graph.sources_for_node(gconn, nid)
        affected_sources.update(sources)
        withdrawn = _withdraw_competing_retypes(reviews_dir, nid, rid, now)
        _write_audit(reviews_dir, rid, {
            "node_id": nid, "slug": node["slug"], "page": page_rel,
            "from_item_type": from_type, "to_item_type": to_type,
            "status": node["status"], "withdrawn_competing_retypes": withdrawn,
            "affected_pages": [page_rel] + [f"Sources/{s}.md" for s in sorted(sources)],
        }, now)
        applied += 1
        outcomes.append(oc.outcome(rid, "change_item_type", oc.APPLIED, **details))

    return {"applied": applied, "skipped": skipped, "changed_pages": sorted(set(changed_pages)),
            "graph_changed": applied > 0, "affected_sources": sorted(affected_sources),
            "outcomes": outcomes}


def _outcome_details(gconn, item: dict[str, Any], *, wiki_dir: Path,
                     ctx: dict[str, Any] | None) -> tuple[dict[str, Any], Any]:
    """ADR-0072 decision 3: the reviewed tag's name and its before/after type, read BEFORE the
    retype writes (read-only; a malformed id is never turned into a page path). Every read here is
    reporting-only (:func:`apply_outcomes.guarded`): a failed one leaves the name as the id and the
    before-type unavailable (`None`, listed in `details["unavailable"]`), never guessed.

    Returns `(details, page_type)` — `page_type` is the page's own `item_type` as read here (None when
    the page has none, :data:`apply_outcomes.UNAVAILABLE` when it couldn't be read), for verifying
    an already-in-effect retype."""
    subj = item.get("subject") if isinstance(item.get("subject"), dict) else {}
    nid, to_type = subj.get("node_id"), subj.get("to_item_type")
    node = oc.guarded(graph.get_node, gconn, nid) if _is_canonical_item_id(nid) else None
    is_item = isinstance(node, dict) and node.get("node_type") == "item"
    page_type: Any = oc.UNAVAILABLE
    if ctx is not None:
        from_type = ctx["from_type"]                     # the executor's own read
        page_type = ctx["meta"].get("item_type")
    elif is_item:
        meta = oc.guarded(items._read_node_meta, Path(wiki_dir) / NODE_DIR["item"] / f"{node['slug']}.md")
        if isinstance(meta, dict):                       # retype_check's own rule: page, then mirror
            page_type = meta.get("item_type")
            from_type = page_type or node.get("item_type")
        else:                                            # missing or unreadable page: not guessed
            from_type = None
    else:
        from_type = None
    details: dict[str, Any] = {
        "subject": oc.item_name(wiki_dir, node, nid) if is_item else oc.named(nid, None),
        "before": {"item_type": from_type}, "after": {"item_type": to_type}}
    if from_type is None:
        details["unavailable"] = ["before.item_type"]
    return details, page_type


def _done_outcome(gconn, item: dict[str, Any], rid: str, details: dict[str, Any],
                  page_type: Any) -> dict[str, Any]:
    """The outcome of a retype `retype_check` called done: `already_in_effect` only when the page
    AND the graph mirror read back at the target type (both re-read here, guarded); an unreadable
    one is `verification_unavailable`, never in effect. Nothing is written on this path."""
    to_type = details["after"]["item_type"]
    node = oc.guarded(graph.get_node, gconn, (item.get("subject") or {}).get("node_id"))
    if page_type is oc.UNAVAILABLE or node is oc.UNAVAILABLE:
        return oc.unverified(rid, "change_item_type", changed=False, **details)
    if page_type != to_type:                             # the type came from the mirror only
        return oc.outcome(rid, "change_item_type", oc.SKIPPED, reason=oc.POSTCONDITION_NOT_MET,
                          **details, problem={"fields": ["item_type"]})
    if (node or {}).get("item_type") != to_type:
        return oc.outcome(rid, "change_item_type", oc.SKIPPED,
                          reason="graph_item_type_mismatch", **details)
    return oc.outcome(rid, "change_item_type", oc.ALREADY_IN_EFFECT, **details)


def _ownership_problem(gconn, item: dict[str, Any], *, wiki_dir: Path, reason: Any) -> dict | None:
    """The structured ADR-0070/0071 page problem behind an ownership skip (retype_check returns
    only its code), recomputed read-only for the outcome's details."""
    nid = (item.get("subject") or {}).get("node_id")
    node = graph.get_node(gconn, nid) if _is_canonical_item_id(nid) else None
    if node is None or node.get("node_type") != "item":
        return None
    problem = item_pages.page_problem(gconn, wiki_dir, nid, node["slug"])
    return problem if problem and problem.get("reason") == reason else None
