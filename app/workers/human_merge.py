#!/usr/bin/env python3
"""Human-filed merge (ADR-0064 decisions 9–10): the reviewer names a survivor and an absorbed
knowledge item, and a `merge_items` review item is filed PRE-APPROVED (`decided_by: human` — the
filing IS the approval, the ADR-0058 human-add pattern). Nothing else is touched here: the merge
itself still happens only at the explicit, dry-run-previewed apply (ADR-0040/0050).

Each side is an `itm_` id or an Items slug (optionally `Items/<slug>.md`), resolved against graph
nodes. **Blocked outcomes write NOTHING** (typed `reason`): unresolvable/ambiguous ref, not an item,
self-merge, survivor already merged, survivor/absorbed not active (the executor's own guards, checked
up front so a pre-approved item is never born unexecutable).

Collision routing (never a parallel subject, never a bypassed rejection — ADR-0064 review round):
- a rejection covers the PAIR: a rejected merge in EITHER direction BLOCKS
  (`merge_previously_rejected`) — reopen it explicitly (ADR-0045); to flip a "wrong survivor"
  rejection, reopen it, then file the other direction (which withdraws the reopened item);
- same-direction approved → no-op (`already_approved`); same-direction pending/deferred →
  approved in place;
- a CONFLICTING merge (`merges.merges_conflict`: shared absorbed node, or a chain — the reverse
  direction included) that is approved-unapplied BLOCKS (`conflicting_merge_approved`); one still
  pending/deferred is withdrawn (`superseded_by_human_merge`). A shared survivor never conflicts.
`plan_merge` computes the outcome WITHOUT writing (the UI confirm page renders it); `file_merge`
executes that plan. A purpose-named `audit_log/<review_id>-human-filed-merge-<hex>.json` entry
records the act.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from app.backend import graph
from app.backend.manifests import iso_now
from app.workers import items, merges, reviews
from app.workers.wiki_render import NODE_DIR

REASON_CODE = "human_filed"
_ITEM_ID_RE = re.compile(r"^itm_[0-9a-f]{16}$")


def _blocked(reason: str, **extra: Any) -> dict[str, Any]:
    return {"outcome": "blocked", "reason": reason, **extra}


def _resolve_ref(gconn: Any, ref: Any) -> tuple[dict[str, Any] | None, str | None]:
    """(node, None) or (None, reason). An id resolves by id; anything else by unique item slug."""
    ref = str(ref or "").strip()
    if ref.startswith("Items/"):
        ref = ref[len("Items/"):]
    if ref.endswith(".md"):
        ref = ref[:-3]
    if not ref:
        return None, "invalid_ref"
    if _ITEM_ID_RE.fullmatch(ref):
        node = graph.get_node(gconn, ref)
        if node is None:
            return None, "unknown_item"
        return (node, None) if node.get("node_type") == "item" else (None, "not_an_item")
    matches = [n for n in graph.nodes_of_type(gconn, "item") if n["slug"] == ref]
    if not matches:
        return None, "unknown_item"
    if len(matches) > 1:
        return None, "ambiguous_ref"
    return graph.get_node(gconn, matches[0]["node_id"]), None


def _load(reviews_dir: Path, state: str, rid: str) -> dict[str, Any] | None:
    path = Path(reviews_dir) / state / f"{rid}.json"
    if not path.exists():
        return None
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"review_id": rid}
    return item if isinstance(item, dict) else {"review_id": rid}


def plan_merge(
    gconn: Any,
    *,
    survivor: str,
    absorbed: str,
    reviews_dir: Path,
) -> dict[str, Any]:
    """Resolve + route one human-filed merge WITHOUT writing. Returns `blocked` (typed `reason`) or
    `{outcome: filed|approved_existing|already_approved, review_id, subject, survivor, absorbed,
    withdraw: [review_id…]}` — exactly what `file_merge` would do."""
    reviews_dir = Path(reviews_dir)
    nodes = {}
    for side, ref in (("survivor", survivor), ("absorbed", absorbed)):
        node, reason = _resolve_ref(gconn, ref)
        if node is None:
            return _blocked(reason, side=side, ref=str(ref or "").strip())
        nodes[side] = node
    a, b = nodes["survivor"], nodes["absorbed"]
    if a["node_id"] == b["node_id"]:
        return _blocked("self_merge", node_id=a["node_id"])
    if a["status"] == "merged":
        return _blocked("survivor_already_merged", node_id=a["node_id"])
    for side, node in (("survivor", a), ("absorbed", b)):
        if node["status"] not in ("active", "unused"):   # ADR-0066: an unused tag stays mergeable
            return _blocked(f"{side}_not_active", node_id=node["node_id"],
                            node_status=node["status"])

    subject = {"survivor_node_id": a["node_id"], "absorbed_node_id": b["node_id"]}
    rid = reviews.review_id("merge_items", subject)
    rev_rid = reviews.review_id("merge_items", {"survivor_node_id": b["node_id"],
                                                "absorbed_node_id": a["node_id"]})
    for candidate in (rid, rev_rid):                       # a rejection covers the pair
        rejected = _load(reviews_dir, "rejected", candidate)
        if rejected is not None:
            return _blocked("merge_previously_rejected", review_id=candidate,
                            reversed=candidate == rev_rid,
                            decided_by=rejected.get("decided_by"),
                            decided_at=rejected.get("decided_at"),
                            decision_note=rejected.get("decision_note"))
    plan = {"review_id": rid, "subject": subject, "survivor": a, "absorbed": b, "withdraw": []}
    if _load(reviews_dir, "approved", rid) is not None:
        return {**plan, "outcome": "already_approved"}
    conflicts = merges.conflicting_merges(gconn, reviews_dir, a["node_id"], b["node_id"],
                                          exclude_rid=rid)
    if conflicts["approved_unapplied"]:
        other = conflicts["approved_unapplied"][0]
        return _blocked("conflicting_merge_approved", review_id=other.get("review_id"),
                        conflicting_subject=other.get("subject"))
    plan["withdraw"] = [str(i["review_id"]) for i in conflicts["unresolved"] if i.get("review_id")]
    outcome = "approved_existing" if _load(reviews_dir, "pending", rid) is not None else "filed"
    return {**plan, "outcome": outcome}


def file_merge(
    gconn: Any,
    *,
    survivor: str,
    absorbed: str,
    reviews_dir: Path,
    now: str | None = None,
) -> dict[str, Any]:
    """Perform one human-filed merge (`plan_merge`, then its writes). Returns a typed outcome dict;
    `blocked` writes nothing.

    Outcomes: `filed` (new pre-approved item), `approved_existing` (a same-direction pending item
    approved in place), `already_approved` (no-op), or `blocked` with a `reason`. `withdrawn` lists
    conflicting pending items withdrawn by this call."""
    now = now or iso_now()
    reviews_dir = Path(reviews_dir)
    plan = plan_merge(gconn, survivor=survivor, absorbed=absorbed, reviews_dir=reviews_dir)
    if plan["outcome"] == "blocked":
        return plan
    rid, outcome = plan["review_id"], plan["outcome"]
    if outcome == "already_approved":
        return {"outcome": outcome, "review_id": rid, "withdrawn": []}
    a, b, subject = plan["survivor"], plan["absorbed"], plan["subject"]
    withdrawn = [w for w in plan["withdraw"] if reviews.withdraw_review_item(
        reviews_dir, w, reason="superseded_by_human_merge", now=now)]
    if outcome == "filed":
        reviews.create_review_item(
            reviews_dir, review_type="merge_items", subject=subject,
            proposal={"to_status": "merged", "reason_code": REASON_CODE},
            context={"survivor": {"slug": a["slug"]}, "absorbed": {"slug": b["slug"]}},
            now=now)
    reviews.resolve_review_item(reviews_dir, rid, decision="approved", decided_by="human",
                                note="human-filed merge (ADR-0064)", now=now)

    audit = reviews_dir / "audit_log"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / f"{rid}-human-filed-merge-{uuid.uuid4().hex[:8]}.json").write_text(json.dumps({
        "review_id": rid, "event": "human_filed_merge", "actor": "human", "at": now,
        "subject": subject, "outcome": outcome, "withdrawn": withdrawn,
        "survivor_slug": a["slug"], "absorbed_slug": b["slug"],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"outcome": outcome, "review_id": rid, "withdrawn": withdrawn}


def preview_context(gconn: Any, plan: dict[str, Any], *, wiki_dir: Path) -> dict[str, Any]:
    """Read-only blast-radius summary for the UI confirm step (ADR-0064 review round): what the
    merge WOULD do at apply — the absorbed item's active edges re-pointed, its mentioning sources
    moving to the survivor, and the survivor's aliases after the ADR-0050 union. Writes nothing."""
    wiki_dir = Path(wiki_dir)
    a, b = plan["survivor"], plan["absorbed"]
    meta = {n["node_id"]: items._read_node_meta(wiki_dir / NODE_DIR["item"] / f"{n['slug']}.md")
            or {"title": n["slug"], "aliases": []} for n in (a, b)}
    ma, mb = meta[a["node_id"]], meta[b["node_id"]]
    edges = graph.outgoing_active(gconn, b["node_id"]) + graph.incoming_active(gconn, b["node_id"])
    return {
        "survivor": {"node_id": a["node_id"], "slug": a["slug"], "title": ma["title"],
                     "sources": graph.sources_for_node(gconn, a["node_id"])},
        "absorbed": {"node_id": b["node_id"], "slug": b["slug"], "title": mb["title"],
                     "sources": graph.sources_for_node(gconn, b["node_id"])},
        "absorbed_active_edges": len(edges),
        "aliases_after": merges._alias_union(ma["title"], ma["aliases"], mb["title"], mb["aliases"]),
    }
