#!/usr/bin/env python3
"""Phase 6 identity-surgery executor: change_entity_subtype (ADR-0051).

Single-node 1:1 **subtype rekey**: relabels one entity-family node to a new subtype, changing its
id-prefix + page directory. Mints a NEW active/candidate node at the prefix-substituted id (same
FROZEN name-hash), re-points the old node's active edges to it, tombstones the old id
(`status: rekeyed`, `rekeyed_to: <new>`), withdraws unresolved old-id subjects, and writes a
reconstructable audit. **FORWARD-ONLY** (auditable, not live-reversible). NOT a merge — no 2->1
collapse, no source-set union; the new node inherits the old node's title/aliases/confidence and its
source links come from the re-pointed `mentions` (graph projection, never page frontmatter).

Two-pass: a dry plan that detects the pre-write BLOCK gates (never partial-apply), then the apply.
**Crux A (virgin-target-only):** the new id is COMPUTED, so the target slot must be fully empty — three
block gates `target_subtype_id_exists` / `target_subtype_page_exists` / `target_assertion_exists`. Because
the target is virgin, the edge re-point has NONE of merge's collapse/resurrect matrix; on a drifted/tampered
graph a pre-existing target assertion BLOCKs (never collapse/resurrect — that is the identity collapse this
op forbids). The `invalid_repoint_endpoint` gate blocks a `duplicates`/type-constrained edge whose
`SAME_TYPE_EDGES`/`EDGE_ENDPOINTS` contract breaks under the new type (resolve the duplicate first).
**Ordering (re-point BEFORE render):** mint bare node -> repoint -> render new page (sources now populated)
-> tombstone old -> Source fan-out -> withdraw -> audit. Graph-REQUIRED, key-free, deterministic. Reuses
ADR-0050 merge machinery (`merges._active_edges_touching`/`_approved_unapplied_block`/`_withdraw_b_subjects`,
`graph.repoint_edge`/`find_assertion`).
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from app.backend import graph
from app.backend.manifests import iso_now
from app.workers import concepts, merges
from app.workers.wiki_render import NODE_DIR, render_concept_page

_ENTITY_FAMILY = frozenset({"entity", "person", "organization", "project"})
_RETYPABLE_STATUSES = frozenset({"active", "candidate"})       # ADR-0051 decision D
_CANONICAL_EDGES = frozenset({"contradicts", "duplicates"})    # symmetric, stored src_id < dst_id
# Canonical semantic node id (ADR-0021/0051): a valid concept/entity-family prefix + 16-hex frozen
# name-hash. Tighter than merge's `_is_safe_id` — a malformed/tampered id must never mint a target. A
# canonical concept (`cpt_`) id passes this shape check but is caught downstream by `out_of_scope`.
_CANONICAL_NODE_ID = re.compile(r"(cpt|ent|per|org|prj)_[0-9a-f]{16}")


def _is_canonical_node_id(x: Any) -> bool:
    return isinstance(x, str) and bool(_CANONICAL_NODE_ID.fullmatch(x))


def _new_id(old_id: str, to_type: str) -> str:
    """ADR-0051 decision C: prefix substitution on the FROZEN hash (never re-hash the name)."""
    return f"{concepts._TYPE_PREFIX[to_type]}_{old_id.split('_', 1)[1]}"


def _approved_rekeys(reviews_dir: Path) -> list[dict[str, Any]]:
    d = Path(reviews_dir) / "approved"
    out: list[dict[str, Any]] = []
    for path in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (isinstance(item, dict) and item.get("type") == "change_entity_subtype"
                and item.get("status") == "approved"):
            out.append(item)
    return out


def _effective_type(gconn, endpoint: str, new: str, to_type: str) -> str | None:
    """The endpoint's node_type AFTER the rekey: `to_type` for the (virgin, not-yet-minted) target, else
    the live graph type."""
    if endpoint == new:
        return to_type
    n = graph.get_node(gconn, endpoint)
    return n["node_type"] if n else None


def _plan_edge(gconn, e: dict[str, Any], old: str, new: str, to_type: str) -> tuple:
    """Plan one active edge's re-point old->new (dry — no writes). Returns one of:
    ("repoint", e, new_src, new_dst) | ("self", e) | ("block", reason). Simpler than merge: the virgin
    target can hold no live edge, so there is no collapse/resurrect — a pre-existing full-identity row is a
    drift/tamper artifact and BLOCKs."""
    new_src = new if e["src_id"] == old else e["src_id"]
    new_dst = new if e["dst_id"] == old else e["dst_id"]
    if e["edge_type"] in _CANONICAL_EDGES and new_src > new_dst:
        new_src, new_dst = new_dst, new_src          # re-canonicalize symmetric pairs (src < dst)
    if new_src == new_dst:
        return ("self", e)                            # impossible on a virgin target; defensive only
    src_t = _effective_type(gconn, new_src, new, to_type)
    dst_t = _effective_type(gconn, new_dst, new, to_type)
    # SAME_TYPE_EDGES (`duplicates`/`supersedes`): a subtype change can break the same-type contract
    # (e.g. an entity marked `duplicates` of a same-type entity, now type-mismatched). Block; the human
    # resolves the duplicate first. (Merge never needs this — survivor and absorbed share a type.)
    if e["edge_type"] in graph.SAME_TYPE_EDGES and src_t != dst_t:
        return ("block", "invalid_repoint_endpoint")
    allowed = graph.EDGE_ENDPOINTS.get(e["edge_type"], (None, None))
    if (allowed[0] is not None and src_t not in allowed[0]) or \
       (allowed[1] is not None and dst_t not in allowed[1]):
        return ("block", "invalid_repoint_endpoint")
    existing = graph.find_assertion(
        gconn, src_id=new_src, dst_id=new_dst, edge_type=e["edge_type"], asserted_by=e["asserted_by"],
        evidence_source_id=e["evidence_source_id"], evidence_char_start=e["evidence_char_start"],
        evidence_char_end=e["evidence_char_end"])
    if existing is not None and existing["edge_id"] != e["edge_id"]:
        return ("block", "target_assertion_exists")   # drift/tamper: never collapse/resurrect into target
    return ("repoint", e, new_src, new_dst)


def _write_audit(reviews_dir: Path, rid: str, record: dict[str, Any], now: str) -> None:
    audit = Path(reviews_dir) / "audit_log"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / f"{rid}-rekeyed-{uuid.uuid4().hex[:8]}.json").write_text(
        json.dumps({"review_id": rid, "decision": "rekeyed", "decided_by": "system",
                    "decided_at": now, **record}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def apply_rekeys(gconn, reviews_dir: Path, *, wiki_dir: Path, now: str | None = None) -> dict[str, Any]:
    """Apply approved `change_entity_subtype` decisions (ADR-0051). Returns
    `{applied, skipped:[{review_id, reason}], changed_pages, graph_changed, affected_sources}`. Graph-
    REQUIRED; the caller owns the final commit + the Source-page re-render of `affected_sources`."""
    now = now or iso_now()
    applied = 0
    skipped: list[dict[str, str]] = []
    changed_pages: list[str] = []
    affected_sources: set[str] = set()

    for item in _approved_rekeys(reviews_dir):
        rid = str(item.get("review_id", ""))
        subj = item.get("subject") or {}
        proposal = item.get("proposal") or {}
        old = subj.get("node_id")
        to_type = subj.get("to_type")

        def _skip(reason: str) -> None:
            skipped.append({"review_id": rid, "reason": reason})

        # --- subject/derivation guards (decision E); each appends a typed reason, never partial-applies ---
        if to_type not in _ENTITY_FAMILY:
            _skip("invalid_to_type")
            continue
        if proposal.get("to_type") != to_type:           # proposal must agree with the subject's target
            _skip("to_type_mismatch")
            continue
        if not _is_canonical_node_id(old):               # canonical old id before any derivation (security)
            _skip("noncanonical_node_id")
            continue
        n_old = graph.get_node(gconn, old)
        if n_old is None:
            _skip("node_missing")
            continue
        old_type = n_old["node_type"]
        if old_type not in _ENTITY_FAMILY:
            _skip("out_of_scope")
            continue
        if to_type == old_type:                          # typed no-op (never an error; mutates/blocks nothing)
            _skip("noop_same_type")
            continue
        if n_old["status"] == "rekeyed":                 # idempotent: a completed rekey is a true no-op
            continue
        if n_old["status"] not in _RETYPABLE_STATUSES:
            _skip("node_not_retypable")
            continue
        new = _new_id(old, to_type)
        old_page = f"{NODE_DIR[old_type]}/{n_old['slug']}.md"
        meta_old = concepts._read_node_meta(wiki_dir / old_page)
        if meta_old is None:
            _skip("page_missing")
            continue

        # --- three virgin-target block gates (decision A; dry, before any write) ---
        if graph.get_node(gconn, new) is not None:
            _skip("target_subtype_id_exists")
            continue
        new_page_rel = f"{NODE_DIR[to_type]}/{n_old['slug']}.md"
        if (wiki_dir / new_page_rel).exists():           # orphan page w/ no node (wiki/graph drift)
            _skip("target_subtype_page_exists")
            continue

        # --- plan the edge re-points (dry) + detect block gates (target_assertion / endpoint) ---
        edges = merges._active_edges_touching(gconn, old)
        plan: list[tuple] = []
        blocked = None
        for e in edges:
            action = _plan_edge(gconn, e, old, new, to_type)
            if action[0] == "block":
                blocked = action[1]
                break
            plan.append(action)
        if blocked:
            _skip(blocked)
            continue
        if merges._approved_unapplied_block(gconn, reviews_dir, wiki_dir, old, old_page, rid):
            _skip("approved_unapplied_references_rekeyed")
            continue

        # --- APPLY (re-point BEFORE render so the new page's source links populate; never partial now) ---
        new_status = n_old["status"]                     # active or candidate, PRESERVED on the new node
        slug = n_old["slug"]
        graph.upsert_node(gconn, node_id=new, node_type=to_type, slug=slug, status=new_status, now=now)
        audit_edges: dict[str, list] = {"repointed": [], "self": []}
        item_sources: set[str] = set()
        for action in plan:
            e = action[1]
            if e["edge_type"] == "mentions":             # the only projected fan-out for a rekey
                src = e["src_id"]
                if (graph.get_node(gconn, src) or {}).get("node_type") == "source":
                    affected_sources.add(src)
                    item_sources.add(src)
            if action[0] == "repoint":
                _, _, new_src, new_dst = action
                graph.repoint_edge(gconn, e["edge_id"],
                                   new_src=new_src if new_src != e["src_id"] else None,
                                   new_dst=new_dst if new_dst != e["dst_id"] else None, now=now)
                audit_edges["repointed"].append(e["edge_id"])
            elif action[0] == "self":
                graph.set_status(gconn, e["edge_id"], "superseded", now=now)
                audit_edges["self"].append(e["edge_id"])

        # render the NEW node's page (sources_for_node(new) is now populated by the re-pointed mentions)
        new_page = wiki_dir / new_page_rel
        new_page.parent.mkdir(parents=True, exist_ok=True)
        new_page.write_text(render_concept_page({
            "node_type": to_type, "node_id": new, "id_field": concepts.ID_FIELD[to_type],
            "title": meta_old["title"], "aliases": meta_old["aliases"],
            "confidence": meta_old.get("confidence", "low"),
            "source_ids": graph.sources_for_node(gconn, new), "status": new_status,
            "duplicates": graph.active_duplicates(gconn, new),
        }), encoding="utf-8")
        # tombstone the OLD node (rekeyed + rekeyed_to), mirror the graph node status
        (wiki_dir / old_page).write_text(render_concept_page({
            "node_type": old_type, "node_id": old, "id_field": concepts.ID_FIELD[old_type],
            "title": meta_old["title"], "aliases": meta_old["aliases"],
            "confidence": meta_old.get("confidence", "low"), "status": "rekeyed",
            "rekeyed_to": new, "rekeyed_to_link": f"{NODE_DIR[to_type]}/{slug}",
            "rekeyed_at": now, "rekey_review_id": rid,
        }), encoding="utf-8")
        graph.upsert_node(gconn, node_id=old, node_type=old_type, slug=slug, status="rekeyed", now=now)
        item_pages = [new_page_rel, old_page]
        changed_pages.extend(item_pages)

        # withdraw unresolved (pending/deferred) old-id subjects (incl. a pending promotion + competing
        # retypes); the approved rekey item itself is in approved/, so the pending-only scan never touches it.
        withdrawn = merges._withdraw_b_subjects(reviews_dir, old, old_page, now,
                                                reason="superseded_by_rekey")
        _write_audit(reviews_dir, rid, {
            "old": {"node_id": old, "type": old_type, "slug": slug, "page": old_page,
                    "title": meta_old["title"], "old_status": new_status},
            "new": {"node_id": new, "type": to_type, "slug": slug, "page": new_page_rel},
            "edges": audit_edges, "withdrawn_subjects": withdrawn,
            "affected_pages": item_pages + [f"Sources/{s}.md" for s in sorted(item_sources)],
        }, now)
        applied += 1

    return {"applied": applied, "skipped": skipped, "changed_pages": sorted(set(changed_pages)),
            "graph_changed": applied > 0, "affected_sources": sorted(affected_sources)}
