#!/usr/bin/env python3
"""Review-queue reconciliation: symmetric auto-withdrawal of extraction-stale items (ADR-0057).

Tier-2 re-extraction can invalidate the premise of an unresolved review item in BOTH
directions: a node tombstoned by replacement-only supersede (ADR-0055/0056) strands its
pending `promote_candidate_node`; a later run that resurrects the node (active mentions
return) strands the recompose-filed `deprecate_wiki_page`. This module is the ONE
interpretation of that reconciliation (ADR-0057 decision 1): `reconciliation_decision` maps
an unresolved item + current node state to an audited withdrawal reason (or None), and both
call sites — the `concepts._recompose_node` hook and the catch-up `sweep` — go through it.

Authority is per-surface (review round): node **status** authority is page frontmatter
(ADR-0030) with the graph a derived mirror, so every status-based reason requires the two
surfaces to AGREE; **edges** are graph-SoT (ADR-0029), so the active-mentions resurrection
premise reads the graph alone. The `_recompose_node` hook writes page and mirror in the same
pass and passes the status it just wrote — agreement by construction. The sweep reads both
surfaces, and additionally runs a fail-closed **preflight** (schema version, non-empty
graph, graph↔wiki projection validity over the reviewed nodes) before any mutation:
`node_missing_or_rekeyed` requires page-side corroboration (page absent or an identity
tombstone) — graph-missing alone never withdraws.

Ownership is keyed on STORED provenance, never node state (decision 2): only a deprecation
carrying `proposal.reason_code == "no_active_mentions"` (or, for the sweep alone, the exact
legacy prose constant) belongs to reconciliation. Node state alone would mass-misfire: lint
files same-type `deprecate_wiki_page` items for under-supported ACTIVE concepts, whose nodes
always have active mentions. Same-subject collisions (`review_id = hash(type|subject)`) mean
the first filer owns the stored reason — a foreign-reason item is never rewritten or
withdrawn (decision 3).

Withdrawal reuses `reviews.withdraw_review_item`: it only touches `pending/` (status
`pending` OR `deferred` — a terminal-status file found in `pending/` is broken ledger state,
skipped, never withdrawn), so approved/rejected human records are immune by construction,
and a withdrawn subject may legitimately re-file later.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.workers import reviews
from app.workers.wiki_render import parse_frontmatter

# Stable machine-readable provenance recompose-filed deprecations carry going forward.
REASON_CODE_NO_ACTIVE_MENTIONS = "no_active_mentions"
# The exact prose constant `concepts._recompose_node` wrote BEFORE reason_code existed —
# unique to that producer (lint/claims/synthesis/contradiction variants all differ). Accepted
# only where the caller opts in (the one-time catch-up sweep); never generalized.
LEGACY_NO_ACTIVE_MENTIONS_REASON = "no active source mentions remain"

# Concept/entity-family scope; claim/synthesis deprecations are owned by their producers.
_FAMILY_TYPES = frozenset({"concept", "entity", "person", "organization", "project"})

# Audited withdrawal reasons (ADR-0057 decision 1).
REASON_TOMBSTONED = "node_tombstoned_no_active_mentions"
REASON_RESURRECTED = "node_resurrected_active_mentions"
REASON_ALREADY_ACTIVE = "node_already_active"
REASON_MISSING_OR_REKEYED = "node_missing_or_rekeyed"

# Identity-surgery tombstones: the node id no longer exists as itself. Surgery withdraws its
# own subjects at apply (ADR-0050/0051); this reason covers residue found later.
_GONE_STATUSES = frozenset({"merged", "rekeyed"})

_UNRESOLVED = frozenset({"pending", "deferred"})

# Family page dir -> typed frontmatter id field, for the sweep's page-authority scan. Kept
# local to avoid an import cycle with `concepts` (which imports this module); a parity test
# pins it against concepts.ID_FIELD / wiki_render.NODE_DIR.
_DIR_ID_FIELD = {"Concepts": "concept_id", "Entities": "entity_id", "People": "person_id",
                 "Organizations": "organization_id", "Projects": "project_id"}


def owns_deprecation(item: dict[str, Any], *, allow_legacy_reason: bool = False) -> bool:
    """Same-subject ownership rule (decision 3): does this deprecation belong to reconciliation?"""
    if (item.get("context") or {}).get("node_type") not in _FAMILY_TYPES:
        return False
    proposal = item.get("proposal") or {}
    if proposal.get("reason_code") == REASON_CODE_NO_ACTIVE_MENTIONS:
        return True
    return allow_legacy_reason and proposal.get("reason") == LEGACY_NO_ACTIVE_MENTIONS_REASON


def _gone(status: str | None) -> bool:
    return status is None or status in _GONE_STATUSES


def reconciliation_decision(
    item: dict[str, Any],
    *,
    graph_status: str | None,
    page_status: str | None,
    active_source_count: int,
    allow_legacy_reason: bool = False,
) -> str | None:
    """Map an unresolved item + current node state to a withdrawal reason, or None to leave it.

    `None` status means the node is absent from that surface. Status-based reasons require
    graph and page to agree (page is the authority, the graph a mirror — ADR-0030); the
    edge-based resurrection premise reads `active_source_count` alone (edges are graph-SoT,
    ADR-0029). Statuses outside the decided set (e.g. `stale_candidate`, `hidden`) and any
    surface disagreement are deliberately left alone — conservative over clever.
    """
    if item.get("status") not in _UNRESOLVED:
        return None  # terminal-status file in pending/ is broken ledger state, never withdrawn
    item_type = item.get("type")
    if item_type == "promote_candidate_node":
        if _gone(graph_status) and _gone(page_status):
            return REASON_MISSING_OR_REKEYED
        if graph_status == page_status == "deprecated_candidate" and active_source_count == 0:
            return REASON_TOMBSTONED
        if graph_status == page_status == "active":
            return REASON_ALREADY_ACTIVE
        return None
    if item_type == "deprecate_wiki_page":
        if not owns_deprecation(item, allow_legacy_reason=allow_legacy_reason):
            return None
        if active_source_count > 0:
            return REASON_RESURRECTED
        if _gone(graph_status) and _gone(page_status):
            return REASON_MISSING_OR_REKEYED
        return None  # still tombstoned with no active mentions: the legitimate human gate
    return None


def reconcile_pending_item(
    reviews_dir: Path,
    item: dict[str, Any],
    *,
    graph_status: str | None,
    page_status: str | None,
    active_source_count: int,
    now: str | None = None,
    allow_legacy_reason: bool = False,
) -> str | None:
    """Apply `reconciliation_decision` to one unresolved item; withdraw + audit if it decides.

    Returns the withdrawal reason iff a withdrawal happened this call (the underlying
    `withdraw_review_item` only touches `pending/`, so terminal items are no-ops).
    """
    reason = reconciliation_decision(
        item, graph_status=graph_status, page_status=page_status,
        active_source_count=active_source_count, allow_legacy_reason=allow_legacy_reason)
    if reason is None:
        return None
    rid = item.get("review_id")
    if not isinstance(rid, str) or not rid:
        return None
    if reviews.withdraw_review_item(reviews_dir, rid, reason=reason, now=now):
        return reason
    return None


def _load_pending(reviews_dir: Path, review_id: str) -> dict[str, Any] | None:
    path = Path(reviews_dir) / "pending" / f"{review_id}.json"
    if not path.exists():
        return None
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return item if isinstance(item, dict) else None


def reconcile_node_items(
    reviews_dir: Path,
    *,
    node_id: str,
    page: str,
    node_status: str,
    active_source_count: int,
    now: str | None = None,
) -> list[tuple[str, str]]:
    """The `_recompose_node` hook: reconcile the node's two candidate item slots in place.

    The caller has just written BOTH the page and the graph mirror at `node_status`, so that
    one value serves as both surfaces (agreement by construction). Computes the deterministic
    review ids (`promote_candidate_node` keys on `{node_id}`; `deprecate_wiki_page` on
    `{node_id, page}`) and reconciles whichever is unresolved. No legacy-reason shim here —
    pre-`reason_code` items are the catch-up sweep's job. Returns `(review_id, reason)` per
    withdrawal.
    """
    withdrawn: list[tuple[str, str]] = []
    for rid in (reviews.review_id("promote_candidate_node", {"node_id": node_id}),
                reviews.review_id("deprecate_wiki_page", {"node_id": node_id, "page": page})):
        item = _load_pending(reviews_dir, rid)
        if item is None:
            continue
        reason = reconcile_pending_item(
            reviews_dir, item, graph_status=node_status, page_status=node_status,
            active_source_count=active_source_count, now=now)
        if reason is not None:
            withdrawn.append((rid, reason))
    return withdrawn


def page_status_by_id(wiki_dir: Path) -> dict[str, str]:
    """Scan the concept/entity-family page dirs and map typed node id -> frontmatter status.

    Page frontmatter is the node-lifecycle authority (ADR-0030); the sweep corroborates every
    status-based decision against this map.
    """
    statuses: dict[str, str] = {}
    for dir_name, id_field in _DIR_ID_FIELD.items():
        page_dir = Path(wiki_dir) / dir_name
        if not page_dir.is_dir():
            continue
        for path in sorted(page_dir.glob("*.md")):
            try:
                fm = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            nid, status = fm.get(id_field), fm.get("status")
            if isinstance(nid, str) and nid and isinstance(status, str) and status:
                statuses[nid] = status
    return statuses


def _collect_pending(reviews_dir: Path, counts: dict[str, Any],
                     *, allow_legacy_reason: bool) -> list[dict[str, Any]]:
    """Parse `pending/` into eligible items, tallying parse/schema/terminal/ownership skips."""
    pending = Path(reviews_dir) / "pending"
    eligible: list[dict[str, Any]] = []
    for path in sorted(pending.glob("*.json")) if pending.exists() else []:
        counts["scanned"] += 1
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            counts["parse_errors"] += 1
            continue
        if not isinstance(item, dict):
            counts["schema_errors"] += 1
            continue
        if item.get("type") not in ("promote_candidate_node", "deprecate_wiki_page"):
            continue
        rid = item.get("review_id")
        node_id = (item.get("subject") or {}).get("node_id")
        if not (isinstance(rid, str) and rid and isinstance(node_id, str) and node_id):
            counts["schema_errors"] += 1
            continue
        if item.get("status") not in _UNRESOLVED:
            counts["terminal_in_pending"] += 1
            continue
        if item.get("type") == "deprecate_wiki_page" and not owns_deprecation(
                item, allow_legacy_reason=allow_legacy_reason):
            counts["not_owned"] += 1
            continue
        eligible.append(item)
    return eligible


def _preflight(gconn, eligible: list[dict[str, Any]],
               pages: dict[str, str]) -> list[str]:
    """Fail-closed gate before any mutation (review round): schema version, non-empty graph,
    and graph↔wiki projection validity over the reviewed nodes. Any failure refuses the sweep."""
    from app.backend import graph

    failures: list[str] = []
    version = graph.schema_version(gconn)
    if version != graph.SCHEMA_VERSION:
        failures.append(f"graph_schema_version_mismatch (db={version}, expected={graph.SCHEMA_VERSION})")
    if not graph.node_ids(gconn):
        failures.append("graph_has_no_nodes")
    if failures:
        return failures  # don't projection-check a graph already known unusable
    drift: list[str] = []
    for node_id in sorted({i["subject"]["node_id"] for i in eligible}):
        node = graph.get_node(gconn, node_id)
        graph_status = node["status"] if node else None
        page_status = pages.get(node_id)
        if (graph_status is None) != (page_status is None):
            drift.append(f"{node_id} (graph={graph_status}, page={page_status})")
        elif graph_status is not None and graph_status != page_status:
            drift.append(f"{node_id} (graph={graph_status}, page={page_status})")
    if drift:
        shown = ", ".join(drift[:5]) + (f", … +{len(drift) - 5} more" if len(drift) > 5 else "")
        failures.append(f"graph_wiki_projection_invalid over {len(drift)} reviewed node(s): {shown}")
    return failures


def sweep(reviews_dir: Path, gconn, *, wiki_dir: Path, now: str | None = None,
          allow_legacy_reason: bool = True) -> dict[str, Any]:
    """Catch-up sweep over the whole `pending/` set against current graph + page state
    (decision 4). Preflight-gated fail-closed: a non-empty `refused` list means NOTHING was
    withdrawn. Idempotent and key-free; the ONLY caller of the legacy-prose shim (on by
    default here, off everywhere else). Returns counts only — each withdrawal writes its own
    audit entry.
    """
    from app.backend import graph  # local import: keeps decision helpers dependency-light

    counts: dict[str, Any] = {
        "scanned": 0, "eligible": 0, "withdrawn": 0, "withdrawn_by_reason": {},
        "not_owned": 0, "left_unresolved": 0, "parse_errors": 0, "schema_errors": 0,
        "terminal_in_pending": 0, "refused": [],
    }
    eligible = _collect_pending(reviews_dir, counts, allow_legacy_reason=allow_legacy_reason)
    counts["eligible"] = len(eligible)
    pages = page_status_by_id(wiki_dir)
    if eligible:
        counts["refused"] = _preflight(gconn, eligible, pages)
        if counts["refused"]:
            return counts
    for item in eligible:
        node_id = item["subject"]["node_id"]
        node = graph.get_node(gconn, node_id)
        reason = reconcile_pending_item(
            reviews_dir, item,
            graph_status=node["status"] if node else None,
            page_status=pages.get(node_id),
            active_source_count=len(graph.sources_for_node(gconn, node_id)) if node else 0,
            now=now, allow_legacy_reason=allow_legacy_reason)
        if reason is None:
            counts["left_unresolved"] += 1
        else:
            counts["withdrawn"] += 1
            counts["withdrawn_by_reason"][reason] = counts["withdrawn_by_reason"].get(reason, 0) + 1
    return counts
