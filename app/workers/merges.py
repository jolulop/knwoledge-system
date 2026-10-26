#!/usr/bin/env python3
"""Phase 6 identity-surgery executor: merge_items (ADR-0050, restated over ADR-0059's item family).

Collapses two knowledge items — absorbed **B** into survivor **A**: re-points A's live
(active) edges with normalization, tombstones B (`status: merged`, `merged_into: A`), unions B's
title+aliases into A, withdraws unresolved (pending/deferred) B-subjects, and writes a reconstructable
audit entry. **FORWARD-ONLY** (auditable, not live-reversible). Two-pass: a dry plan that detects the
pre-write BLOCK gates (never partial-apply), then the apply. Graph-REQUIRED, key-free, deterministic.

Invariant (decision 3): the live graph after merge ≡ "replace B with A, then normalize — canonicalize
`{contradicts,duplicates}`, collapse FULL-identity duplicates (evidence anchors included → distinct-evidence
edges coexist), remove self-edges". Inactive-target collisions (the `uq_edges_assertion` index is
status-agnostic): resurrect on proposed/superseded (rewriting the row's review_id to the merge id +
withdrawing the stale proposal), BLOCK on rejected. Block gates: rejected_target_collision,
invalid_repoint_endpoint, approved_unapplied_references_absorbed.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from app.backend import graph, taxonomy
from app.backend.manifests import iso_now
from app.workers import items, labels, reviews
from app.workers.wiki_render import NODE_DIR, parse_frontmatter, render_item_page

# ADR-0059: one item family, one merge type. Same-structural-type by construction (both
# nodes are `item`); the survivor KEEPS ITS OWN item_type regardless of the absorbed one —
# the old cross-type-merge deferral dissolves with the typed-prefix identity model.
_MERGE_TYPES = frozenset({"merge_items"})
_SAFE_ID = re.compile(r"[A-Za-z0-9_]+")
# Symmetric edges stored canonically (src_id < dst_id), per validate_graph. `supersedes` is DIRECTED.
_CANONICAL_EDGES = frozenset({"contradicts", "duplicates"})
_MERGEABLE_STATUSES = frozenset({"active", "unused"})   # ADR-0066: unused tags stay mergeable
# effect-statuses that mean an approved item's effect is NOT realized -> it blocks the merge (decision 6).
_UNAPPLIED_EFFECT = frozenset({"pending_apply", "unknown", "apply_deferred"})


def _is_safe_id(x: Any) -> bool:
    return isinstance(x, str) and bool(_SAFE_ID.fullmatch(x))


def _approved_merges(reviews_dir: Path) -> list[dict[str, Any]]:
    d = Path(reviews_dir) / "approved"
    out: list[dict[str, Any]] = []
    for path in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(item, dict) and item.get("type") in _MERGE_TYPES and item.get("status") == "approved":
            out.append(item)
    return out


def _merge_pair(item: dict[str, Any]) -> tuple[str, str] | None:
    subj = item.get("subject") if isinstance(item, dict) else None
    if not isinstance(subj, dict):
        return None
    a, b = subj.get("survivor_node_id"), subj.get("absorbed_node_id")
    return (a, b) if _is_safe_id(a) and _is_safe_id(b) and a != b else None


def merges_conflict(m1: tuple[str, str], m2: tuple[str, str]) -> bool:
    """ADR-0064 review round: two merges conflict iff they share the ABSORBED node, or one's absorbed
    node is the other's survivor (a chain). A shared survivor is compatible (a star)."""
    (a1, b1), (a2, b2) = m1, m2
    return b1 == b2 or b1 == a2 or a1 == b2


def conflicting_merges(gconn, reviews_dir: Path, survivor: str, absorbed: str, *,
                       exclude_rid: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Other `merge_items` items conflicting with `survivor ← absorbed`, split into `unresolved`
    (pending/deferred, in `pending/`) and `approved_unapplied` (approved, absorbed node not yet
    `merged`). Rejected and effected merges never conflict. `exclude_rid` skips the item itself."""
    out: dict[str, list[dict[str, Any]]] = {"unresolved": [], "approved_unapplied": []}
    for state, key in (("pending", "unresolved"), ("approved", "approved_unapplied")):
        d = Path(reviews_dir) / state
        for path in sorted(d.glob("*.json")) if d.exists() else []:
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(item, dict) or item.get("type") not in _MERGE_TYPES \
                    or item.get("review_id") == exclude_rid:
                continue
            pair = _merge_pair(item)
            if pair is None or not merges_conflict((survivor, absorbed), pair):
                continue
            if state == "approved":
                nb = graph.get_node(gconn, pair[1]) if gconn is not None else None
                if nb is not None and nb["status"] == "merged":
                    continue                       # effected: history, not a live conflict
            out[key].append(item)
    return out


def _subject_references(subject: Any, node_id: str, page: str | None) -> bool:
    """Exact node-id equality in STRUCTURED subject fields only (ADR-0050 decision 2) — never prose."""
    if not isinstance(subject, dict):
        return False
    if subject.get("node_id") == node_id:
        return True
    nids = subject.get("node_ids")
    if isinstance(nids, list) and node_id in nids:
        return True
    if subject.get("survivor_node_id") == node_id or subject.get("absorbed_node_id") == node_id:
        return True
    if subject.get("topic_node_id") == node_id:          # propose_synthesis keys on the topic node
        return True
    return bool(page and subject.get("page") == page)


def _item_references(item: dict[str, Any], node_id: str, page: str | None) -> bool:
    """Whether a review item references `node_id` in its structured **subject OR proposal** fields
    (ADR-0050 decision 6 — both, never prose). Used by the unresolved-withdrawal AND the approved gate."""
    return (_subject_references(item.get("subject"), node_id, page)
            or _subject_references(item.get("proposal"), node_id, page))


def _alias_union(a_title: str, a_aliases: list[str], b_title: str, b_aliases: list[str]) -> list[str]:
    """Deterministic union (decision 5): A.title unchanged; stable-dedup(A.aliases ++ [B.title] ++
    B.aliases), case-insensitive, dropping any entry equal to A's title."""
    out: list[str] = []
    seen = {a_title.strip().lower()}
    for x in [*a_aliases, b_title, *b_aliases]:
        key = (x or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(x.strip())
    return out[:items._MAX_ALIASES]


def _active_edges_touching(gconn, node_id: str) -> list[dict[str, Any]]:
    """Active edges with the node as src OR dst, deduped by edge_id (sorted for determinism)."""
    by_id: dict[str, dict[str, Any]] = {}
    for e in graph.outgoing_active(gconn, node_id) + graph.incoming_active(gconn, node_id):
        by_id[e["edge_id"]] = e
    return [by_id[k] for k in sorted(by_id)]


def _plan_edge(gconn, e: dict[str, Any], b: str, a: str) -> tuple:
    """Plan one active edge's re-point B->A (dry — no writes). Returns one of:
    ("repoint", e, new_src, new_dst) | ("collapse", e) | ("resurrect", e, target_row) | ("self", e)
    | ("block", reason)."""
    new_src = a if e["src_id"] == b else e["src_id"]
    new_dst = a if e["dst_id"] == b else e["dst_id"]
    if e["edge_type"] in _CANONICAL_EDGES and new_src > new_dst:
        new_src, new_dst = new_dst, new_src          # re-canonicalize symmetric pairs (src < dst)
    if new_src == new_dst:
        return ("self", e)                            # A<->A — a node can't relate to itself
    allowed = graph.EDGE_ENDPOINTS.get(e["edge_type"], (None, None))
    nsrc, ndst = graph.get_node(gconn, new_src), graph.get_node(gconn, new_dst)
    if (allowed[0] is not None and (nsrc or {}).get("node_type") not in allowed[0]) or \
       (allowed[1] is not None and (ndst or {}).get("node_type") not in allowed[1]):
        return ("block", "invalid_repoint_endpoint")
    existing = graph.find_assertion(
        gconn, src_id=new_src, dst_id=new_dst, edge_type=e["edge_type"], asserted_by=e["asserted_by"],
        evidence_source_id=e["evidence_source_id"], evidence_char_start=e["evidence_char_start"],
        evidence_char_end=e["evidence_char_end"])
    if existing is not None and existing["edge_id"] != e["edge_id"]:
        st = existing["status"]
        if st == "active":
            return ("collapse", e)                    # full-identity active collision -> collapse
        if st in ("proposed", "superseded"):
            return ("resurrect", e, existing)         # lifecycle-inactive -> resurrect the target
        if e["edge_type"] == "mentions":
            # ADR-0066: a rejected tag link is a per-document drop. A KEPT link (human) moving onto
            # it wins — the merge is the later human statement that both are the same tag; a mere
            # suggestion (llm) moving onto a dropped link loses (the drop stands).
            return ("resurrect", e, existing) if e["asserted_by"] == "human" else ("collapse", e)
        return ("block", "rejected_target_collision")  # a human "no" — never auto-resurrect
    return ("repoint", e, new_src, new_dst)


def _approved_unapplied_block(gconn, reviews_dir: Path, wiki_dir: Path, b: str, b_page: str,
                              self_rid: str) -> bool:
    """True if any OTHER approved item references absorbed B and is not yet effected (decision 6)."""
    from app.backend import review_read                # lazy import (avoid load-order cycle)
    d = Path(reviews_dir) / "approved"
    manifests_dir = wiki_dir.parent / "raw" / "manifests"
    for path in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(item, dict) or item.get("review_id") == self_rid:
            continue
        if not _item_references(item, b, b_page):
            continue
        preview = review_read.project_review(item, gconn=gconn, wiki_dir=wiki_dir,
                                             manifests_dir=manifests_dir)
        if preview.get("apply", {}).get("effect_status") in _UNAPPLIED_EFFECT:
            return True
    return False


def _render_survivor(gconn, *, a: str, na: dict[str, Any], nt: str, aliases: list[str], title: str,
                     wiki_dir: Path, now: str, status: str = "active") -> str | None:
    """Re-render survivor A with the unioned aliases (page-authoritative). Returns the page path if it
    changed, else None. The survivor keeps its OWN item_type (ADR-0059) and page-owned fields; its
    status is `active`, or ADR-0066 `unused` when no document keeps the merged tag."""
    page = wiki_dir / NODE_DIR[nt] / f"{na['slug']}.md"
    meta = items._read_node_meta(page)
    item_type = (meta or {}).get("item_type") or na.get("item_type")
    node_sources, suggested = graph.tag_projection(gconn, a, status)
    duplicates = graph.active_duplicates(gconn, a)
    rendered = render_item_page({
        "node_id": a, "item_type": item_type, "title": title,
        "aliases": aliases, "confidence": (meta or {}).get("confidence", "low"),
        "source_ids": node_sources, "suggested_ids": suggested, "status": status,
        "duplicates": duplicates,
        "split_from": (meta or {}).get("split_from"),
        "split_review_id": (meta or {}).get("split_review_id"),
        "description": (meta or {}).get("description"),
    }, labels=items._link_labels(wiki_dir, node_sources + suggested, duplicates))
    changed = (not page.exists()) or page.read_text(encoding="utf-8") != rendered
    if changed:
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(rendered, encoding="utf-8")
    graph.upsert_node(gconn, node_id=a, node_type=nt, slug=na["slug"], status=status,
                      item_type=item_type, now=now)
    return f"{NODE_DIR[nt]}/{na['slug']}.md" if changed else None


def _render_tombstone(gconn, *, b: str, nb: dict[str, Any], nt: str, a: str, a_slug: str,
                      b_title: str, b_aliases: list[str], rid: str, wiki_dir: Path, now: str) -> str:
    """Render B as a `merged` tombstone (full schema + merged_into/merged_at/merge_review_id) and mirror
    the graph node status."""
    page = wiki_dir / NODE_DIR[nt] / f"{nb['slug']}.md"
    meta = items._read_node_meta(page)
    item_type = (meta or {}).get("item_type") or nb.get("item_type")
    page.parent.mkdir(parents=True, exist_ok=True)
    survivor_link = f"{NODE_DIR[nt]}/{a_slug}"
    page.write_text(render_item_page({
        "node_id": b, "item_type": item_type, "title": b_title,
        "aliases": b_aliases, "confidence": (meta or {}).get("confidence", "low"), "status": "merged",
        "merged_into": a, "merged_into_link": survivor_link,
        "merged_at": now, "merge_review_id": rid,
    }, labels=labels.display_labels(wiki_dir, [survivor_link])), encoding="utf-8")
    graph.upsert_node(gconn, node_id=b, node_type=nt, slug=nb["slug"], status="merged",
                      item_type=item_type, now=now)
    return f"{NODE_DIR[nt]}/{nb['slug']}.md"


def _write_audit(reviews_dir: Path, rid: str, record: dict[str, Any], now: str) -> None:
    audit = Path(reviews_dir) / "audit_log"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / f"{rid}-merged-{uuid.uuid4().hex[:8]}.json").write_text(
        json.dumps({"review_id": rid, "decision": "merged", "decided_by": "system",
                    "decided_at": now, **record}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def apply_merges(gconn, reviews_dir: Path, *, wiki_dir: Path, now: str | None = None) -> dict[str, Any]:
    """Apply approved `merge_items` decisions (ADR-0050/0059). Returns
    `{applied, skipped:[{review_id, reason}], changed_pages, graph_changed, affected_sources,
    journaled}` — `journaled` are the in-flight markers the caller closes once its refreshes succeed. Graph-
    REQUIRED; the caller owns the final commit + the Source-page re-render of `affected_sources`."""
    now = now or iso_now()
    applied = 0
    skipped: list[dict[str, str]] = []
    changed_pages: list[str] = []
    affected_sources: set[str] = set()
    journaled: list[str] = []                            # in-flight markers the caller must close

    from app.workers import identity_journal, tag_links   # lazy: tag_links imports this module
    # ADR-0066 review round 3: the whole identity operation is serialized with tag actions and
    # extraction writes (the ledger lock), from planning to the last page.
    with tag_links.ledger_lock(reviews_dir):
        for item in _approved_merges(reviews_dir):
            rid = str(item.get("review_id", ""))
            subj = item.get("subject") or {}
            proposal = item.get("proposal") or {}
            a, b = subj.get("survivor_node_id"), subj.get("absorbed_node_id")

            # --- subject guards (decision 5); each appends a typed reason and skips, never partial-applies ---
            def _skip(reason: str) -> None:
                skipped.append({"review_id": rid, "reason": reason})

            if proposal.get("to_status") != "merged":
                _skip("unexpected_to_status")
                continue
            if not _is_safe_id(a) or not _is_safe_id(b):
                _skip("invalid_subject")
                continue
            if a == b:
                _skip("self_merge")
                continue
            na, nb = graph.get_node(gconn, a), graph.get_node(gconn, b)
            if na is None or nb is None:
                _skip("node_missing")
                continue
            nt = na["node_type"]
            if nb["node_type"] != nt:
                _skip("type_mismatch")
                continue
            if nt != "item":
                _skip("out_of_scope")
                continue
            b_page = f"{NODE_DIR[nt]}/{nb['slug']}.md"
            # ADR-0066 review round 3: an open journal entry means THIS merge was interrupted — resume
            # it (adopt the tombstone it may already have written) instead of treating it as done.
            resuming = identity_journal.is_open(reviews_dir, rid)
            tomb = wiki_dir / b_page
            merged_into = parse_frontmatter(tomb.read_text(encoding="utf-8", errors="replace")) \
                .get("merged_into") if tomb.exists() else None
            if nb["status"] == "merged" and not (resuming and merged_into == a):
                # Idempotent: a completed merge INTO THIS survivor is a true no-op; a tombstone pointing
                # elsewhere means a conflicting merge won — loud typed skip, never silent (ADR-0064).
                if merged_into is not None and merged_into != a:
                    _skip("absorbed_merged_elsewhere")
                continue
            if na["status"] not in _MERGEABLE_STATUSES:       # ADR-0066: an unused tag stays mergeable
                _skip("survivor_not_active")
                continue
            if nb["status"] not in _MERGEABLE_STATUSES and nb["status"] != "merged":
                _skip("absorbed_not_active")
                continue
            meta_a = items._read_node_meta(wiki_dir / f"{NODE_DIR[nt]}/{na['slug']}.md")
            meta_b = items._read_node_meta(wiki_dir / b_page)
            if meta_a is None or meta_b is None:
                _skip("page_missing")
                continue
            # ADR-0059: both pages must carry a valid governed classification (drifted state is a
            # typed skip, never a mid-apply raise from the graph write gate).
            if not taxonomy.is_item_type(meta_a.get("item_type") or na.get("item_type")) or \
               not taxonomy.is_item_type(meta_b.get("item_type") or nb.get("item_type")):
                _skip("item_type_missing")
                continue

            # --- plan the edge re-points (dry) + detect block gates (no writes) ---
            edges = _active_edges_touching(gconn, b)
            plan: list[tuple] = []
            blocked = None
            for e in edges:
                action = _plan_edge(gconn, e, b, a)
                if action[0] == "block":
                    blocked = action[1]
                    break
                plan.append(action)
            if blocked:
                _skip(blocked)
                continue
            if not resuming and _approved_unapplied_block(gconn, reviews_dir, wiki_dir, b, b_page, rid):
                _skip("approved_unapplied_references_absorbed")
                continue
            if tag_links.corrupt_ledgers(reviews_dir):
                _skip("tag_ledger_corrupt")                  # decisions must move with the merge, intact
                continue

            # --- LEDGER FIRST (ADR-0066 review round 3): journal the operation, then move B's keeps and
            # drops to A, then touch the graph. Every step below is idempotent, so a re-run resumes. ---
            identity_journal.begin(reviews_dir, rid, op="merge_items", nodes=[a, b], now=now)
            ledger_sources = tag_links.move_merge_decisions(reviews_dir, survivor=a, absorbed=b)

            # --- APPLY (no more blocks; never partial after this point) ---
            audit_edges: dict[str, list] = {"repointed": [], "collapsed": [], "resurrected": [], "self": []}
            partners: set[str] = set()                       # same-type partner pages to re-render (duplicates)
            item_sources: set[str] = set()                   # this merge's affected Source pages (for the audit)
            for action in plan:
                e = action[1]
                # Any action that touches B's edge changes the projection on the OTHER endpoint's page — the
                # Source page of a `mentions` edge, the same-type partner of a `duplicates` edge — so it must
                # re-render to drop the absorbed B (whether the edge is re-pointed, collapsed, resurrected away,
                # or self-dropped). Collected from the ORIGINAL edge regardless of the action (fixes the
                # collapse/resurrect stale-projection gap).
                if e["edge_type"] == "mentions":
                    src = e["src_id"]
                    if (graph.get_node(gconn, src) or {}).get("node_type") == "source":
                        affected_sources.add(src)
                        item_sources.add(src)
                elif e["edge_type"] == "duplicates":
                    partner = e["src_id"] if e["dst_id"] == b else e["dst_id"]
                    if partner not in (a, b):
                        partners.add(partner)
                if action[0] == "repoint":
                    _, _, new_src, new_dst = action
                    graph.repoint_edge(gconn, e["edge_id"],
                                       new_src=new_src if new_src != e["src_id"] else None,
                                       new_dst=new_dst if new_dst != e["dst_id"] else None, now=now)
                    audit_edges["repointed"].append(e["edge_id"])
                elif action[0] == "collapse":
                    graph.set_status(gconn, e["edge_id"], "superseded", now=now)
                    audit_edges["collapsed"].append(e["edge_id"])
                elif action[0] == "resurrect":
                    target = action[2]
                    graph.reactivate_edge(gconn, target["edge_id"], review_id=rid, now=now)
                    if target.get("review_id"):
                        reviews.withdraw_review_item(reviews_dir, target["review_id"],
                                                     reason="superseded_by_merge", now=now)
                    graph.set_status(gconn, e["edge_id"], "superseded", now=now)
                    audit_edges["resurrected"].append(
                        {"target_edge_id": target["edge_id"], "previous_status": target["status"],
                         "previous_target_review_id": target.get("review_id"), "absorbed_edge_id": e["edge_id"]})
                elif action[0] == "self":
                    graph.set_status(gconn, e["edge_id"], "superseded", now=now)
                    audit_edges["self"].append(e["edge_id"])

            if resuming:
                # An interrupted run may already have re-pointed edges whose Source pages were never
                # re-rendered: re-render every document linked to either tag.
                linked = {r["src_id"] for r in gconn.execute(
                    "SELECT DISTINCT e.src_id FROM edges e JOIN nodes n ON n.node_id = e.src_id "
                    "WHERE e.dst_id IN (?, ?) AND e.edge_type = 'mentions' AND n.node_type = 'source'",
                    (a, b))}
                affected_sources.update(linked)
                item_sources.update(linked)

            # tombstone B, re-render survivor A (alias union), partners, withdraw B-subjects, audit.
            absorbed_old_status = nb["status"]               # captured before the tombstone (decision 5 audit)
            a_page = f"{NODE_DIR[nt]}/{na['slug']}.md"
            aliases = _alias_union(meta_a["title"], meta_a["aliases"], meta_b["title"], meta_b["aliases"])
            item_pages: list[str] = [b_page if nb["status"] == "merged" else _render_tombstone(
                gconn, b=b, nb=nb, nt=nt, a=a, a_slug=na["slug"], b_title=meta_b["title"],
                b_aliases=meta_b["aliases"], rid=rid, wiki_dir=wiki_dir, now=now)]
            # ADR-0066: each document's decision wins over any suggestion now on A, whatever its evidence
            # span (before the survivor renders). The survivor is active if it was, or if a document
            # keeps it after the re-point (an unused survivor gaining a kept link revives).
            rejected_by_drop = tag_links.enforce_drops(gconn, reviews_dir, node_id=a, now=now)
            affected_sources.update(rejected_by_drop)
            item_sources.update(rejected_by_drop)
            a_status = ("active" if na["status"] == "active" or graph.kept_sources_for_node(gconn, a)
                        else "unused")
            surv = _render_survivor(gconn, a=a, na=na, nt=nt, aliases=aliases, title=meta_a["title"],
                                    wiki_dir=wiki_dir, now=now, status=a_status)
            if surv:
                item_pages.append(surv)
            for pid in sorted(partners):
                pn = graph.get_node(gconn, pid)
                if pn and pn["node_type"] == "item" and items.recompose_semantic_node_page(
                        gconn, node_id=pid, wiki_dir=wiki_dir, status=pn["status"],
                        review_status=(items._read_node_meta(
                            wiki_dir / NODE_DIR[pn["node_type"]] / f"{pn['slug']}.md") or {}).get(
                                "review_status", "none"), now=now) == "written":
                    item_pages.append(f"{NODE_DIR[pn['node_type']]}/{pn['slug']}.md")
            changed_pages.extend(item_pages)

            withdrawn = _withdraw_b_subjects(reviews_dir, b, b_page, now)
            _write_audit(reviews_dir, rid, {
                "survivor": {"node_id": a, "type": nt, "slug": na["slug"], "page": a_page},
                "absorbed": {"node_id": b, "type": nt, "slug": nb["slug"], "title": meta_b["title"],
                             "old_status": absorbed_old_status, "page": b_page},
                "edges": audit_edges, "aliases": aliases, "withdrawn_subjects": withdrawn,
                "survivor_status": a_status, "tag_ledgers_rewritten": ledger_sources,
                "suggestions_rejected_by_drop": rejected_by_drop, "resumed": resuming,
                # affected pages re-rendered here + the Source pages run_apply re-renders (affected_sources).
                "affected_pages": item_pages + [f"Sources/{s}.md" for s in sorted(item_sources)],
            }, now)
            # The marker stays: the apply caller removes it after the Source pages + indexes refreshed.
            identity_journal.set_stage(reviews_dir, rid, "graph_done")
            journaled.append(rid)
            applied += 1

    return {"applied": applied, "skipped": skipped, "changed_pages": sorted(set(changed_pages)),
            "graph_changed": applied > 0, "affected_sources": sorted(affected_sources),
            "journaled": journaled}


def _withdraw_b_subjects(reviews_dir: Path, b: str, b_page: str, now: str,
                         *, reason: str = "superseded_by_merge") -> list[str]:
    """Withdraw every unresolved item in pending/ (status pending OR deferred) referencing B. `reason` is
    the audited withdrawal cause — the ADR-0051 rekey reuse passes `superseded_by_rekey` for an accurate
    audit trail (merge keeps the default `superseded_by_merge`)."""
    withdrawn: list[str] = []
    pend = Path(reviews_dir) / "pending"
    for path in sorted(pend.glob("*.json")) if pend.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rid = item.get("review_id") if isinstance(item, dict) else None
        if rid and _item_references(item, b, b_page):
            if reviews.withdraw_review_item(reviews_dir, rid, reason=reason, now=now):
                withdrawn.append(rid)
    return withdrawn
