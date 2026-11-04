#!/usr/bin/env python3
"""Human-add (ADR-0058, restated by ADR-0066): a reviewer tags a document with a knowledge item the
extractor missed.

ADR-0066: the add IS a keep. The POST creates the item if it does not exist (candidate node + an
anchorless `asserted_by: human` mention + page — a documented use of the nullable-anchor contract;
no renderer claims quote text for mentions) and then runs `tag_links.keep`, which records the keep
in the document's tag-decision ledger and activates the item immediately (no promotion review).
A purpose-named `audit_log/<review_id>-human-added-<hex>.json` entry ties actor/source/node together
(the per-source `added` counter reads it).

Duplicate/identity routing (identity is name-hashed and frozen, ADR-0021/0059 — never mint a second
node for a known name): existing candidate/tombstone → kept (activated); active/unused → kept
(revived); an `item_type` differing from the existing node's → kept on the existing node + a
`change_item_type` item (nothing auto-retypes) — except an UNCLASSIFIED candidate, whose keep takes
the human's type directly (the sentinel needs a real type to activate). The human must supply a REAL
production type — the sentinel is model-only (ADR-0059 decision 5). The blocked-tag list does not
apply: it filters machine suggestions only. A pre-ADR-0066 rejected promotion no longer blocks (a
rejection is per document now). A create whose slug is already owned by a DIFFERENT id blocks
(`slug_collision`) before any write. After a successful add, `wiki/index.md` is rebuilt.

ADR-0071 decision 3, both checked under the ledger lock before any write, only when the add would
CREATE a tag: a new name equal (id normalization) to another live tag's title blocks (`name_taken`,
naming that tag); a name with the same `merge_key` as an active/unused tag's title or alias returns
`needs_choice` (nothing written) until the caller acknowledges every match (`acknowledged`, "Add
separately") or picks one (`existing_node_id`, "Use existing", which keeps that tag as it is).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from app.backend import graph, publication_queue, taxonomy
from app.backend.manifests import iso_now
from app.workers import identity_journal, item_names, item_pages, items, reviews, tag_links

_MAX_DESCRIPTION = 2000

# Node lifecycle states the add can work with; anything else (merged/hidden/…) blocks.
_ADDABLE_STATUSES = frozenset({"candidate", "active", "unused", "deprecated_candidate",
                               "stale_candidate"})


def _blocked(reason: str, **extra: Any) -> dict[str, Any]:
    return {"outcome": "blocked", "reason": reason, **extra}


@tag_links.ledger_action           # serialized with every other ledger write; damaged ledger → blocked
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
    existing_node_id: str | None = None,
    acknowledged: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Perform one human-add. Returns a typed outcome dict; `blocked` outcomes write NOTHING.

    Outcomes: `created` (new item, kept → active), `mention_added` (existing candidate/tombstone,
    kept → active), `mention_added_active` (active/unused item, kept), `routed_retype` (kept on the
    existing node + a change_item_type item), or `blocked` with a `reason` (`invalid_*`,
    `unknown_source`, `node_not_addable`, `slug_collision`, `ledger_corrupt`,
    `identity_operation_in_flight`, or a keep gate), `name_taken` / `title_authority_unreadable`
    (ADR-0071 level 1), or `needs_choice` (level 2: `matches` to choose from; nothing written).

    `existing_node_id` ("Use existing"): keep that tag on the document as it is (its own title and
    type; no retype is routed). `acknowledged`: the tag ids the human saw and chose to add separately
    from; a match not among them (one that appeared since) asks again.
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
    tag_links.load_ledger(reviews_dir, source_id)      # LedgerCorrupt here — before ANY write

    if existing_node_id is not None:
        # "Use existing" (ADR-0071 decision 3): the human picked a tag; keep it exactly as it is.
        chosen = graph.get_node(gconn, existing_node_id) if isinstance(existing_node_id, str) else None
        if chosen is None or chosen.get("node_type") != "item":
            return _blocked("unknown_item", node_id=existing_node_id)
        if taxonomy.is_production_item_type(chosen.get("item_type")):
            item_type = chosen["item_type"]               # kept as it is: no retype is routed
        nid = existing_node_id
        title = item_names.title_of(wiki_dir, chosen) or title
    else:
        nid = items.node_id(title)
    if (rid_busy := identity_journal.operation_for(reviews_dir, [nid])) is not None:
        return _blocked("identity_operation_in_flight", review_id=rid_busy,
                        hint="run /reviews/apply to finish it first")
    existing = graph.get_node(gconn, nid)
    if existing is None and existing_node_id is None:
        # ADR-0071 decision 3: a NEW tag's name, checked under the ledger lock before any write.
        if (conflict := item_names.exact_conflict(gconn, wiki_dir, title, None)) is not None:
            return _blocked(conflict.pop("reason"), **conflict)
        seen = set(acknowledged or ())
        matches = item_names.similar_tags(gconn, wiki_dir, title, None)
        if any(m["node_id"] not in seen for m in matches):
            return {"outcome": "needs_choice", "reason": "similar_tags", "title": title,
                    "matches": matches}
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
    amendments: dict[str, Any] = {}
    if (node is not None and node_status == "candidate"
            and node.get("item_type") == taxonomy.UNCLASSIFIED):
        amendments["item_type"] = item_type    # the sentinel takes the human's type on the keep
        routed_retype = None

    # --- producer writes (mirror extract_items' _emit, asserted_by=human, anchorless) ---
    node_created = node is None
    slug = items.page_slug(title, nid) if node_created else node["slug"]
    # Slug-collision guard (review round, B2; ADR-0070 B1 review): different titles can hash to
    # different ids yet normalize to the same slug — never write another item's page, nor a page name
    # another item records (any status), nor a page whose owner can't be established; block BEFORE
    # any write (the promote executor's amended_slug_collision mirror).
    problem = item_pages.page_problem(gconn, wiki_dir, nid, slug)
    if problem is None:
        problem = item_pages.first_problem(
            gconn, wiki_dir, [d["node_id"] for d in graph.active_duplicates(gconn, nid)])
    if problem is not None:
        holder = problem.get("owner") or (problem.get("holders") or [None])[0]
        holder_node = graph.get_node(gconn, holder) if holder else None
        return _blocked("slug_collision", slug=problem.get("slug"), existing_node_id=holder,
                        existing_title=(item_names.title_of(wiki_dir, holder_node)
                                        if holder_node else None),
                        detail=problem["reason"])
    # ADR-0068 decision 4: record the publication intent before the first write.
    intent = publication_queue.record(root, origin="tag_action", sources=[source_id], items=[nid],
                                      now=now)
    if node_created:
        graph.upsert_node(gconn, node_id=nid, node_type="item", slug=slug,
                          status="candidate", item_type=used_type, now=now)
        if description:
            amendments["description"] = description
    if node_created or node_status in ("deprecated_candidate", "stale_candidate"):
        # Create / resurrect the page from graph + page authority (a tombstone becomes a candidate;
        # the ADR-0057 hook then withdraws its stale deprecation) so the keep can activate it.
        graph.upsert_assertion(gconn, src_id=source_id, dst_id=nid, edge_type="mentions",
                               asserted_by="human", status="active", now=now)
        items._recompose_node(gconn, node_id=nid, wiki_dir=wiki_dir, reviews_dir=reviews_dir,
                              now=now, text_hint={"title": title, "aliases": clean_aliases,
                                                  "item_type": used_type})

    kept = tag_links.keep(gconn, source_id=source_id, node_id=nid, wiki_dir=wiki_dir,
                          reviews_dir=reviews_dir, amendments=amendments or None, now=now)
    if kept["outcome"] == "blocked":
        gconn.commit()                                    # a created node is published, not lost
        tag_links.finish(Path(root), [source_id], wiki_dir=wiki_dir, intent=intent)
        return kept

    if routed_retype is not None:
        reviews.create_review_item(
            reviews_dir, review_type="change_item_type",
            subject={"node_id": nid, "to_item_type": routed_retype[1]},
            proposal={"to_item_type": routed_retype[1]},
            context={"source_id": source_id, "name": title,
                     "from_item_type": routed_retype[0]}, now=now)

    audit = reviews_dir / "audit_log"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / f"{rid}-human-added-{uuid.uuid4().hex[:8]}.json").write_text(json.dumps({
        "review_id": rid, "event": "human_added", "actor": "human", "at": now,
        "source_id": source_id, "node_id": nid, "item_type": used_type,
        "title": title, "aliases": clean_aliases, "description": description,
        "node_created": node_created, "edge": {"src_id": source_id, "dst_id": nid},
        "keep_effect": kept.get("effect"),
        "routed_retype": (
            {"from_item_type": routed_retype[0], "to_item_type": routed_retype[1]}
            if routed_retype else None),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Producer contract: re-render the Source page (its Items projection) and rebuild the index.
    refresh = tag_links.finish(Path(root), kept["affected_sources"], wiki_dir=wiki_dir,
                               intent=intent)

    outcome = ("routed_retype" if routed_retype
               else "created" if node_created
               else "mention_added_active" if node_status in ("active", "unused")
               else "mention_added")
    return {"outcome": outcome, "node_id": nid, "title": title, "item_type": used_type,
            "review_id": rid,
            "node_created": node_created, "keep_effect": kept.get("effect"),
            "index_rebuilt": refresh["index_rebuilt"],
            "keyword_reindexed": refresh["keyword_reindexed"], "warnings": refresh["warnings"]}
