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
from app.workers import items, reviews
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


def apply_retypes(gconn, reviews_dir: Path, *, wiki_dir: Path, now: str | None = None) -> dict[str, Any]:
    """Apply approved `change_item_type` decisions (ADR-0059). Returns
    `{applied, skipped:[{review_id, reason}], changed_pages, graph_changed, affected_sources}`.
    Graph-REQUIRED; the caller owns the final commit + the Source-page re-render of
    `affected_sources` (their Items sections group by item_type)."""
    now = now or iso_now()
    applied = 0
    skipped: list[dict[str, str]] = []
    changed_pages: list[str] = []
    affected_sources: set[str] = set()

    for item in _approved_retypes(reviews_dir):
        rid = str(item.get("review_id", ""))
        subj = item.get("subject") or {}
        proposal = item.get("proposal") or {}
        nid = subj.get("node_id")
        to_type = subj.get("to_item_type")

        def _skip(reason: str) -> None:
            skipped.append({"review_id": rid, "reason": reason})

        # --- subject guards; each appends a typed reason, never partial-applies ---
        if not taxonomy.is_production_item_type(to_type):   # sentinel is never a valid target
            _skip("invalid_to_item_type")
            continue
        if proposal.get("to_item_type") != to_type:          # proposal must agree with the subject
            _skip("to_item_type_mismatch")
            continue
        if not _is_canonical_item_id(nid):                   # canonical id before any page path (security)
            _skip("noncanonical_node_id")
            continue
        node = graph.get_node(gconn, nid)
        if node is None:
            _skip("node_missing")
            continue
        if node["node_type"] != "item":
            _skip("out_of_scope")
            continue
        if node["status"] not in _RETYPABLE_STATUSES:
            _skip("node_not_retypable")
            continue
        page_rel = f"{NODE_DIR['item']}/{node['slug']}.md"
        page_path = wiki_dir / page_rel
        meta = items._read_node_meta(page_path)
        if meta is None:
            _skip("page_missing")
            continue
        from_type = meta.get("item_type") or node.get("item_type")
        if from_type == to_type:
            continue        # already in the target state — idempotent no-op (re-apply safe)

        # --- APPLY: the metadata flip — re-render the page with the new item_type (status,
        # review_status, and every page-owned field preserved), mirror the graph row, done. ---
        rendered = render_item_page({
            "node_id": nid, "item_type": to_type,
            "title": meta["title"], "aliases": meta["aliases"],
            "confidence": meta.get("confidence", "low"),
            "source_ids": graph.sources_for_node(gconn, nid), "status": node["status"],
            "duplicates": graph.active_duplicates(gconn, nid),
            "split_from": meta.get("split_from"),
            "split_review_id": meta.get("split_review_id"),
            "description": meta.get("description"),
        })
        page_path.write_text(rendered, encoding="utf-8")
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

    return {"applied": applied, "skipped": skipped, "changed_pages": sorted(set(changed_pages)),
            "graph_changed": applied > 0, "affected_sources": sorted(affected_sources)}
