#!/usr/bin/env python3
"""Phase 4b graph read projection (ADR-0032 decision 5).

A thin, **read-only** view over the authoritative graph store (`app/backend/graph.py`,
`db/graph.sqlite`). It introduces no new graph authority — it only projects existing nodes and
edge assertions for retrieval:

- :func:`node_view` — a node's metadata plus its adjacent assertions grouped by ``edge_type``,
  split into ``outgoing`` (node is ``src_id``) and ``incoming`` (node is ``dst_id``), each with the
  adjacent node's metadata inline (resolved in one batched query, no N+1).
- :func:`neighborhood` — a flat ``{root_id, depth, nodes[], edges[], truncated, cap}`` payload: a
  bounded BFS out to ``depth`` (default 1, hard max 2) returning the induced subgraph over the
  reachable nodes.

Invariants honored:
- **Filtering is by edge status, not node status** (ADR-0032 decision 5). A ``candidate`` (or
  ``archived``/``deleted``) node can still appear if an ``active`` edge reaches it — always carrying
  its real ``status`` and ``answer_eligible: false``; ``proposed``/``rejected``/``superseded`` edges
  are hidden unless an explicit ``include_status`` asks for them. Node ``answer_eligible`` is
  surfaced but never used to drop a node. Node-status / retention (archived/deleted) filtering is a
  ``/search`` (Phase 4c) concern, deliberately not applied to the raw graph projection.
- **Symmetric edges** (``contradicts``/``related_to``/``duplicates``) keep their stored canonical
  ``src_id``/``dst_id`` (sorted, ADR-0031) and carry ``symmetric: true``. ``other_node_id`` (the
  endpoint that is *not* the queried node) is a :func:`node_view` field — it is well-defined only
  relative to a queried node. In the **flat** :func:`neighborhood` payload there is no single
  reference node (a depth-2 edge can join two non-root nodes), so neighborhood edges are
  **canonical-only** (``src_id``/``dst_id`` + ``symmetric``) and carry no ``other_node_id``.
- **Edge evidence anchors are advisory** (ADR-0032 decision 5): the authoritative evidence for a
  ``contradicts`` edge is the two Claim pages' structured citations, not the edge row.
- **Node metadata is ``id``/``type``/``slug``/``status`` (+ ``answer_eligible``).** The graph store
  has no ``title`` (it lives in wiki frontmatter); title resolution is deferred to the navigation /
  search layer rather than coupling this projection to the wiki.

The depth/cap budgets are endpoint constants here; the deterministic router (Phase 4c) is what
reads ``policies/retrieval.yaml`` and may override them per the routing taxonomy.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from app.backend import graph
from app.backend.eligibility import is_answer_eligible

# Endpoint depth contract (ADR-0032 decision 5): default 1, hard max 2.
DEFAULT_DEPTH = 1
MAX_DEPTH = 2
# Result-size caps (constants in 4b; policy-driven via retrieval.yaml in 4c).
DEFAULT_MAX_NODES = 100
DEFAULT_MAX_EDGES = 300
# Upper bound on a caller-supplied override, so a single request can't pull the whole graph.
HARD_MAX_NODES = 1000
HARD_MAX_EDGES = 2000

# Symmetric relations (ADR-0030/0031): stored once with src_id < dst_id, semantically undirected.
SYMMETRIC_EDGE_TYPES = frozenset({"contradicts", "related_to", "duplicates"})

DEFAULT_EDGE_STATUSES = ("active",)


# --------------------------------------------------------------------------- param parsing


def _parse_csv(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def parse_edge_statuses(raw: str | None) -> tuple[str, ...]:
    """Parse the ``include_status`` override; default ``active`` only. Raises on unknown status."""
    items = _parse_csv(raw)
    if not items:
        return DEFAULT_EDGE_STATUSES
    bad = sorted(set(items) - graph.EDGE_STATUSES)
    if bad:
        raise ValueError(f"unknown edge status(es) {bad}; allowed: {sorted(graph.EDGE_STATUSES)}")
    # De-dupe while staying deterministic.
    return tuple(sorted(set(items)))


def parse_edge_types(raw: str | None) -> tuple[str, ...] | None:
    items = _parse_csv(raw)
    if not items:
        return None
    bad = sorted(set(items) - graph.EDGE_TYPES)
    if bad:
        raise ValueError(f"unknown edge_type(s) {bad}; allowed: {sorted(graph.EDGE_TYPES)}")
    return tuple(sorted(set(items)))


def parse_node_types(raw: str | None) -> frozenset[str] | None:
    items = _parse_csv(raw)
    if not items:
        return None
    bad = sorted(set(items) - graph.NODE_TYPES)
    if bad:
        raise ValueError(f"unknown node_type(s) {bad}; allowed: {sorted(graph.NODE_TYPES)}")
    return frozenset(items)


# --------------------------------------------------------------------------- shaping helpers


def _node_meta(row: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
    node_type = row["node_type"]
    status = row["status"]
    keys = row.keys() if hasattr(row, "keys") else row
    return {
        "node_id": row["node_id"],
        "node_type": node_type,
        # ADR-0059: surface the governed classification on item metadata.
        "item_type": row["item_type"] if "item_type" in keys else None,
        "slug": row["slug"],
        "status": status,
        "answer_eligible": is_answer_eligible(node_type, status),
    }


def _unknown_meta(node_id: str) -> dict[str, Any]:
    # Defensive: edges require indexed endpoints (graph.upsert_assertion), so this should not
    # happen, but a hand-written graph row must not crash the projection.
    return {"node_id": node_id, "node_type": "unknown", "item_type": None, "slug": None,
            "status": None, "answer_eligible": False}


def _evidence(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "advisory": True,
        "source_id": row["evidence_source_id"],
        "char_start": row["evidence_char_start"],
        "char_end": row["evidence_char_end"],
    }


def _edge_obj(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "edge_id": row["edge_id"],
        "src_id": row["src_id"],
        "dst_id": row["dst_id"],
        "edge_type": row["edge_type"],
        "status": row["status"],
        "asserted_by": row["asserted_by"],
        "confidence": row["confidence"],
        "symmetric": row["edge_type"] in SYMMETRIC_EDGE_TYPES,
        "evidence": _evidence(row),
    }


def _assertion_obj(row: sqlite3.Row, node_id: str, meta: dict[str, dict]) -> dict[str, Any]:
    other_id = row["dst_id"] if row["src_id"] == node_id else row["src_id"]
    return {
        "edge_id": row["edge_id"],
        "edge_type": row["edge_type"],
        "status": row["status"],
        "asserted_by": row["asserted_by"],
        "confidence": row["confidence"],
        "symmetric": row["edge_type"] in SYMMETRIC_EDGE_TYPES,
        "src_id": row["src_id"],
        "dst_id": row["dst_id"],
        "other_node_id": other_id,
        "other": meta.get(other_id) or _unknown_meta(other_id),
        "evidence": _evidence(row),
    }


# --------------------------------------------------------------------------- queries


def _node_meta_map(conn: sqlite3.Connection, node_ids: set[str]) -> dict[str, dict[str, Any]]:
    if not node_ids:
        return {}
    ids = sorted(node_ids)
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT node_id, node_type, item_type, slug, status FROM nodes WHERE node_id IN ({placeholders})",
        ids,
    ).fetchall()
    return {r["node_id"]: _node_meta(r) for r in rows}


def _edges_touching(
    conn: sqlite3.Connection,
    node_ids: list[str],
    statuses: tuple[str, ...],
    edge_types: tuple[str, ...] | None,
) -> list[sqlite3.Row]:
    """Active-by-status edges with at least one endpoint in ``node_ids`` (frontier expansion)."""
    if not node_ids:
        return []
    node_ph = ",".join("?" for _ in node_ids)
    status_ph = ",".join("?" for _ in statuses)
    sql = (
        f"SELECT * FROM edges WHERE (src_id IN ({node_ph}) OR dst_id IN ({node_ph})) "
        f"AND status IN ({status_ph})"
    )
    params: list[Any] = [*node_ids, *node_ids, *statuses]
    if edge_types:
        sql += f" AND edge_type IN ({','.join('?' for _ in edge_types)})"
        params += list(edge_types)
    sql += " ORDER BY edge_type, src_id, dst_id, edge_id"
    return conn.execute(sql, params).fetchall()


def _edges_among(
    conn: sqlite3.Connection,
    node_ids: set[str],
    statuses: tuple[str, ...],
    edge_types: tuple[str, ...] | None,
    *,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Edges whose *both* endpoints are in ``node_ids`` — the induced subgraph (deterministic).

    ``limit`` (when given) is pushed down as a SQL ``LIMIT`` so the result set never grows
    unbounded: the caller passes ``edge_cap + 1`` and treats an over-limit fetch as truncation.
    """
    if not node_ids:
        return []
    ids = sorted(node_ids)
    node_ph = ",".join("?" for _ in ids)
    status_ph = ",".join("?" for _ in statuses)
    sql = (
        f"SELECT * FROM edges WHERE src_id IN ({node_ph}) AND dst_id IN ({node_ph}) "
        f"AND status IN ({status_ph})"
    )
    params: list[Any] = [*ids, *ids, *statuses]
    if edge_types:
        sql += f" AND edge_type IN ({','.join('?' for _ in edge_types)})"
        params += list(edge_types)
    sql += " ORDER BY edge_type, src_id, dst_id, edge_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


# --------------------------------------------------------------------------- projections


def node_view(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    include_status: tuple[str, ...] = DEFAULT_EDGE_STATUSES,
) -> dict[str, Any] | None:
    """Node metadata + adjacent assertions grouped by ``edge_type`` (outgoing/incoming).

    Returns ``None`` if the node does not exist.
    """
    node = graph.get_node(conn, node_id)
    if node is None:
        return None

    rows = _edges_touching(conn, [node_id], tuple(include_status), None)
    adjacent_ids = {r["src_id"] for r in rows} | {r["dst_id"] for r in rows}
    adjacent_ids.discard(node_id)
    meta = _node_meta_map(conn, adjacent_ids)

    outgoing: dict[str, list[dict[str, Any]]] = {}
    incoming: dict[str, list[dict[str, Any]]] = {}
    for row in rows:  # already sorted by (edge_type, src_id, dst_id, edge_id)
        assertion = _assertion_obj(row, node_id, meta)
        bucket = outgoing if row["src_id"] == node_id else incoming
        bucket.setdefault(row["edge_type"], []).append(assertion)

    return {
        "node": _node_meta(node),
        "outgoing": outgoing,
        "incoming": incoming,
        "counts": {
            "outgoing": sum(len(v) for v in outgoing.values()),
            "incoming": sum(len(v) for v in incoming.values()),
        },
    }


def neighborhood(
    conn: sqlite3.Connection,
    root_id: str,
    *,
    depth: int = DEFAULT_DEPTH,
    edge_types: tuple[str, ...] | None = None,
    node_types: frozenset[str] | None = None,
    include_status: tuple[str, ...] = DEFAULT_EDGE_STATUSES,
    node_cap: int = DEFAULT_MAX_NODES,
    edge_cap: int = DEFAULT_MAX_EDGES,
) -> dict[str, Any] | None:
    """Bounded BFS neighborhood as a flat node/edge payload. ``None`` if the root is absent.

    Nodes are discovered by BFS out to ``depth`` (clamped to ``[0, MAX_DEPTH]``), honoring the
    ``node_types`` filter (the root is always included). Edges are then the induced subgraph over
    the discovered nodes — so cross-edges within the outer ring are not missed. Both sets are
    capped (``node_cap``/``edge_cap``) and ``truncated`` is set if either cap clipped the result.

    ``node_types`` filtering is **traversal-time**, not output-only: a node whose type is excluded
    is never entered, so it also blocks discovery of anything reachable *only* through it. This
    keeps the result a connected neighborhood of the requested types rather than a "traverse
    everything, then filter" view.
    """
    root = graph.get_node(conn, root_id)
    if root is None:
        return None
    depth = max(0, min(depth, MAX_DEPTH))
    statuses = tuple(include_status)

    visited: dict[str, int] = {root_id: 0}
    frontier = [root_id]
    truncated = False

    for dist in range(1, depth + 1):
        rows = _edges_touching(conn, frontier, statuses, edge_types)
        # Candidate new neighbors = endpoints not yet visited.
        candidates: set[str] = set()
        for row in rows:
            for endpoint in (row["src_id"], row["dst_id"]):
                if endpoint not in visited:
                    candidates.add(endpoint)
        if not candidates:
            break
        cand_meta = _node_meta_map(conn, candidates)
        added: list[str] = []
        for nid in sorted(candidates):
            if node_types is not None:
                meta = cand_meta.get(nid)
                node_type = meta["node_type"] if meta else None
                if node_type not in node_types:
                    continue
            if len(visited) >= node_cap:
                truncated = True
                break
            visited[nid] = dist
            added.append(nid)
        if not added:
            break
        frontier = added

    # Fetch at most edge_cap + 1 (bounded by SQL LIMIT); an over-cap fetch signals truncation.
    edge_rows = _edges_among(conn, set(visited), statuses, edge_types, limit=edge_cap + 1)
    if len(edge_rows) > edge_cap:
        truncated = True
        edge_rows = edge_rows[:edge_cap]
    edges = [_edge_obj(row) for row in edge_rows]

    meta = _node_meta_map(conn, set(visited))
    nodes = []
    for nid in sorted(visited, key=lambda n: (visited[n], n)):
        node_meta = meta.get(nid) or _unknown_meta(nid)
        nodes.append({**node_meta, "distance": visited[nid]})

    return {
        "root_id": root_id,
        "depth": depth,
        "nodes": nodes,
        "edges": edges,
        "truncated": truncated,
        "cap": {"nodes": node_cap, "edges": edge_cap},
    }


def active_contradiction_endpoints(conn: sqlite3.Connection) -> list[str]:
    """Node ids on either end of an ``active`` ``contradicts`` edge (graph-native disagreement seed).

    Lets a "which sources disagree" query surface contradictions directly from the graph, without
    depending on the literal trigger words matching any page text.
    """
    ids: set[str] = set()
    for r in conn.execute(
        "SELECT src_id, dst_id FROM edges WHERE edge_type = 'contradicts' AND status = 'active'"
    ):
        ids.add(r["src_id"])
        ids.add(r["dst_id"])
    return sorted(ids)


def search_subgraph(
    conn: sqlite3.Connection,
    seed_ids: list[str],
    *,
    depth: int,
    edge_statuses: tuple[str, ...] = DEFAULT_EDGE_STATUSES,
    node_statuses: tuple[str, ...] | None = None,
    node_types: frozenset[str] | None = None,
    edge_types: tuple[str, ...] | None = None,
    node_cap: int,
    edge_cap: int,
) -> dict[str, Any]:
    """Multi-seed bounded BFS returning a flat ``{seeds, nodes, edges, depth, truncated}`` subgraph.

    Used by ``/search`` (Phase 4c). Unlike :func:`neighborhood` (a single-root ``/graph/*`` view
    that is edge-status-only by contract), this accepts an optional ``node_statuses`` retention
    filter — a node is admitted only if its status is allowed — so the search layer can exclude
    archived/deleted nodes (ADR-0032 addendum 2: retention is ``/search``'s job, not the raw graph
    projection's). Seeds that do not exist or fail the filters are dropped.
    """
    depth = max(0, min(depth, MAX_DEPTH))
    statuses = tuple(edge_statuses)

    def admit(meta: dict[str, Any] | None) -> bool:
        if meta is None:
            return False
        if node_types is not None and meta["node_type"] not in node_types:
            return False
        if node_statuses is not None and meta["status"] not in node_statuses:
            return False
        return True

    visited: dict[str, int] = {}
    admitted_seeds: list[str] = []
    truncated = False
    seed_meta = _node_meta_map(conn, set(seed_ids))
    for sid in seed_ids:  # preserve caller ordering (BM25 rank)
        if sid in visited or not admit(seed_meta.get(sid)):
            continue
        if len(visited) >= node_cap:
            truncated = True
            break
        visited[sid] = 0
        admitted_seeds.append(sid)

    frontier = list(admitted_seeds)
    for dist in range(1, depth + 1):
        rows = _edges_touching(conn, frontier, statuses, edge_types)
        candidates = {ep for r in rows for ep in (r["src_id"], r["dst_id"]) if ep not in visited}
        if not candidates:
            break
        cand_meta = _node_meta_map(conn, candidates)
        added: list[str] = []
        for nid in sorted(candidates):
            if not admit(cand_meta.get(nid)):
                continue
            if len(visited) >= node_cap:
                truncated = True
                break
            visited[nid] = dist
            added.append(nid)
        if not added:
            break
        frontier = added

    edge_rows = _edges_among(conn, set(visited), statuses, edge_types, limit=edge_cap + 1)
    if len(edge_rows) > edge_cap:
        truncated = True
        edge_rows = edge_rows[:edge_cap]
    edges = [_edge_obj(row) for row in edge_rows]

    meta = _node_meta_map(conn, set(visited))
    nodes = [
        {**(meta.get(nid) or _unknown_meta(nid)), "distance": visited[nid]}
        for nid in sorted(visited, key=lambda n: (visited[n], n))
    ]
    return {"seeds": admitted_seeds, "nodes": nodes, "edges": edges,
            "depth": depth, "truncated": truncated}
