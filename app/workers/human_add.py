#!/usr/bin/env python3
"""Human-add producer path (ADR-0058, restated over ADR-0059): a reviewer adds a knowledge
item the extractor missed.

The POST is PRODUCER-side work — the same mutation class as `extract_items`, which was
never governance-gated: it immediately upserts the candidate node, a `mentions` edge from the
current source with `asserted_by: human` and NO evidence anchor (a documented use of the
nullable-anchor contract — no renderer claims quote text for mentions), renders the candidate
page, and files the `promote_candidate_node` item recorded APPROVED (`decided_by: human` —
the add IS the approval; promotion itself stays apply-gated through the normal promote
executor). A purpose-named `audit_log/<review_id>-human-added-<hex>.json` entry ties
actor/source/node/item together (precedent: the `-withdrawn-`/`-merged-` entries).

Duplicate/identity routing (identity is name-hashed and frozen, ADR-0021/0059 — never mint a
second node for a known name): existing candidate/tombstone -> add the mention and
approve/reuse its promote item; already active -> mention only, no promote item; an
`item_type` differing from the existing node's -> mention routed to the existing node + a
`change_item_type` item (the node's page keeps its classification until the flip applies —
nothing auto-retypes). The human must supply a REAL production type — the sentinel is
model-only (ADR-0059 decision 5). **Terminal rejected slot:** a rejected promotion is a
human governance record — the add is BLOCKED with the prior decision named; it is never
silently reused, reopened as a side effect, or bypassed via a parallel subject (explicit
ADR-0045 reopen is the path back). A create whose slug is already owned by a DIFFERENT id
blocks (`slug_collision`) before any write. After a successful add, `wiki/index.md` is
rebuilt (producer contract); the audit filename keys on the node's promote-slot review id
even for a mention-only add (one deterministic key per node, whether or not an item was
filed — `promote_resolution: null` marks the mention-only case).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from app.backend import graph, taxonomy
from app.backend.manifests import iso_now
from app.workers import items, reviews, wiki
from app.workers.wiki_render import NODE_DIR, parse_frontmatter, render_item_page

_MAX_DESCRIPTION = 2000

# Node lifecycle states the add can work with; anything else (merged/hidden/…) blocks.
_ADDABLE_STATUSES = frozenset({"candidate", "active", "deprecated_candidate", "stale_candidate"})


def _blocked(reason: str, **extra: Any) -> dict[str, Any]:
    return {"outcome": "blocked", "reason": reason, **extra}


def _rejection_record(reviews_dir: Path, rid: str) -> dict[str, Any] | None:
    path = Path(reviews_dir) / "rejected" / f"{rid}.json"
    if not path.exists():
        return None
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"review_id": rid}
    return item if isinstance(item, dict) else {"review_id": rid}


def add_candidate(
    gconn,
    *,
    root: Path,
    source_id: str,
    item_type: str,
    title: str,
    aliases: list[str] | None = None,
    description: str | None = None,
    wiki_dir: Path,
    reviews_dir: Path,
    now: str | None = None,
) -> dict[str, Any]:
    """Perform one human-add. Returns a typed outcome dict; `blocked` outcomes write NOTHING.

    Outcomes: `created` (new candidate + pre-approved promote), `mention_added` (existing
    promotable node; item approved/reused), `mention_added_active` (active node; mention only),
    `routed_retype` (mention to the existing node + a change_item_type item), or
    `blocked` with a `reason` (`invalid_*`, `unknown_source`, `node_not_addable`,
    `promotion_previously_rejected` + the prior decision fields).
    """
    now = now or iso_now()
    wiki_dir, reviews_dir = Path(wiki_dir), Path(reviews_dir)
    if not taxonomy.is_production_item_type(item_type):   # sentinel is model-only (ADR-0059)
        return _blocked("invalid_item_type", allowed=sorted(taxonomy.ITEM_TYPES))
    title = items._WS.sub(" ", str(title or "")).strip()[:items._MAX_NAME]
    if not title:
        return _blocked("invalid_title")
    clean_aliases = items._clean_aliases(aliases or [])
    if description:  # canonical single-line prose (review round: whitespace collapsed at the boundary)
        description = items._WS.sub(" ", str(description)).strip()[:_MAX_DESCRIPTION] or None
    else:
        description = None
    src_node = graph.get_node(gconn, source_id)
    if src_node is None or src_node.get("node_type") != "source":
        return _blocked("unknown_source", source_id=source_id)

    nid = items.node_id(title)
    existing = graph.get_node(gconn, nid)
    routed_retype: tuple[str, str] | None = None
    if (existing is not None and existing.get("item_type")
            and existing["item_type"] != item_type):
        # One node per canonical referent: the mention routes to the existing node (its page
        # keeps its item_type — the authority) and the human's classification becomes a
        # governed change_item_type proposal. Nothing auto-retypes.
        routed_retype = (existing["item_type"], item_type)

    node = existing
    used_type = (existing.get("item_type") if existing is not None else None) or item_type
    node_status = node["status"] if node is not None else None
    if node is not None and node_status not in _ADDABLE_STATUSES:
        return _blocked("node_not_addable", node_id=nid, node_status=node_status)

    rid = reviews.review_id("promote_candidate_node", {"node_id": nid})
    needs_promotion = node_status != "active"
    if needs_promotion:
        rejected = _rejection_record(reviews_dir, rid)
        if rejected is not None:
            # A human rejection is immutable ledger — block BEFORE any write; ADR-0045 reopen
            # is the explicit path back.
            return _blocked("promotion_previously_rejected", node_id=nid, review_id=rid,
                            decided_by=rejected.get("decided_by"),
                            decided_at=rejected.get("decided_at"),
                            decision_note=rejected.get("decision_note"))

    # --- producer writes (mirror extract_items' _emit, asserted_by=human, anchorless) ---
    node_created = node is None
    slug = items._slug(title) if node_created else node["slug"]
    if node_created:
        # Slug-collision guard (review round, B2): different titles can hash to different ids
        # yet normalize to the same slug — never overwrite another node's page; block BEFORE
        # any write (the promote executor's amended_slug_collision mirror).
        page_path = wiki_dir / NODE_DIR["item"] / f"{slug}.md"
        if page_path.exists():
            fm = parse_frontmatter(page_path.read_text(encoding="utf-8", errors="replace"))
            if fm.get("item_id") != nid:
                return _blocked("slug_collision", slug=slug,
                                existing_node_id=fm.get("item_id"))
        graph.upsert_node(gconn, node_id=nid, node_type="item", slug=slug,
                          status="candidate", item_type=used_type, now=now)
    graph.upsert_assertion(gconn, src_id=source_id, dst_id=nid, edge_type="mentions",
                           asserted_by="human", status="active", now=now)
    # Re-render from graph + page authority (resurrects a tombstone; the ADR-0057 hook then
    # withdraws its stale deprecation). The text hint seeds title/aliases/type only on a fresh page.
    items._recompose_node(gconn, node_id=nid, wiki_dir=wiki_dir, reviews_dir=reviews_dir,
                          now=now, text_hint={"title": title, "aliases": clean_aliases,
                                              "item_type": used_type})
    if node_created and description:
        # A brand-new page may carry the human description immediately (page-owned field).
        page_path = wiki_dir / NODE_DIR["item"] / f"{slug}.md"
        page_path.write_text(render_item_page({
            "node_id": nid, "item_type": used_type,
            "title": title, "aliases": clean_aliases, "confidence": "low",
            "source_ids": graph.sources_for_node(gconn, nid), "status": "candidate",
            "duplicates": graph.active_duplicates(gconn, nid), "description": description,
        }), encoding="utf-8")

    # The Source page's Items section is a graph projection — re-render so the human
    # mention shows immediately (and validate_projection stays green).
    if (wiki_dir / "Sources" / f"{source_id}.md").exists():
        wiki.generate_wiki(root, source_ids=[source_id], rebuild_index=False, record_job=False)

    if routed_retype is not None:
        reviews.create_review_item(
            reviews_dir, review_type="change_item_type",
            subject={"node_id": nid, "to_item_type": routed_retype[1]},
            proposal={"to_item_type": routed_retype[1]},
            context={"source_id": source_id, "name": title,
                     "from_item_type": routed_retype[0]}, now=now)

    promote_resolution = None
    if needs_promotion:
        reviews.create_review_item(
            reviews_dir, review_type="promote_candidate_node",
            subject={"node_id": nid},
            proposal={"to_status": "active", "name": title, "item_type": used_type}, now=now)
        resolved = reviews.resolve_review_item(
            reviews_dir, rid, decision="approved", decided_by="human",
            note=f"human-added via per-source review (source {source_id})", now=now)
        promote_resolution = "approved" if resolved else "reused_approved"

    audit = reviews_dir / "audit_log"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / f"{rid}-human-added-{uuid.uuid4().hex[:8]}.json").write_text(json.dumps({
        "review_id": rid, "event": "human_added", "actor": "human", "at": now,
        "source_id": source_id, "node_id": nid, "item_type": used_type,
        "title": title, "aliases": clean_aliases, "description": description,
        "node_created": node_created, "edge": {"src_id": source_id, "dst_id": nid},
        "promote_resolution": promote_resolution,
        "routed_retype": (
            {"from_item_type": routed_retype[0], "to_item_type": routed_retype[1]}
            if routed_retype else None),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Producer contract (review round, B3): a page change rebuilds wiki/index.md (same
    # best-effort script seam every producer uses); KEYWORD freshness deliberately waits for
    # the normal reindex pass (/jobs/reindex, the apply chain) — no producer refreshes it.
    index_rebuilt = items._rebuild_index(Path(root))

    outcome = ("routed_retype" if routed_retype
               else "created" if node_created
               else "mention_added_active" if not needs_promotion
               else "mention_added")
    return {"outcome": outcome, "node_id": nid, "item_type": used_type, "review_id": rid,
            "node_created": node_created, "promote_resolution": promote_resolution,
            "index_rebuilt": index_rebuilt}
