#!/usr/bin/env python3
"""Phase 4b graph read projection (ADR-0032 decision 5).

A thin, **read-only** view over the authoritative graph store (`app/backend/graph.py`,
`db/graph.sqlite`). It introduces no new graph authority — it only projects existing nodes and
edge assertions for retrieval:

- :func:`node_view` — a node's metadata plus its adjacent assertions grouped by ``edge_type``,
  split into ``outgoing`` (node is ``src_id``) and ``incoming`` (node is ``dst_id``), each with the
  adjacent node's metadata inline (resolved in one batched query, no N+1).
- :func:`neighborhood` — a flat ``{root_id, depth, nodes[], edges[], truncated, cap}`` payload: a
  bounded BFS out to ``depth`` (default 1, hard max 2) returning the subgraph induced within
  fan-out over the reachable nodes (ADR-0032 addendum 10).

Every read is bounded at a hub (ADR-0068 decision 7): per-node indexed queries with ``LIMIT k + 1``
in the stable order ``(edge_type, other endpoint, asserter and evidence anchor)``, a per-traversal query budget, and
``truncated`` whenever a limit omitted anything. A missing traversal index fails closed
(:class:`~app.backend.graph.GraphIndexMissing`), never falling back to an unbounded fetch.

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

# ADR-0068 decision 7: bounded traversal. Each frontier node contributes at most `fanout` edges per
# step, fetched by indexed per-node queries (`LIMIT k + 1`); a traversal runs at most `budget` such
# query streams. Hard ceilings bound every caller and every policy value.
DEFAULT_FANOUT = 50
HARD_MAX_FANOUT = 200
NEIGHBORHOOD_QUERY_BUDGET = 1000
HARD_MAX_QUERY_BUDGET = 4000
DEFAULT_EDGE_LIMIT = 50            # node view: edges per (direction, edge type, status) group
HARD_MAX_EDGE_LIMIT = 200

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


def require_indexes(conn: sqlite3.Connection) -> None:
    """Fail closed when a traversal index is missing (never fall back to an unbounded fetch)."""
    missing = graph.missing_indexes(conn)
    if missing:
        raise graph.GraphIndexMissing(missing)


def _other(row: sqlite3.Row, node_id: str) -> str:
    return row["dst_id"] if row["src_id"] == node_id else row["src_id"]


def _streams(statuses: tuple[str, ...],
             edge_types: tuple[str, ...] | None) -> list[tuple[str, str, str | None]]:
    """The (direction, status, edge type) query streams one node needs."""
    return [(d, st, et) for d in ("out", "in") for st in statuses for et in (edge_types or (None,))]


_KEY_SQL = ", ".join(graph._ASSERTION_KEY)


def _nulls_first(v: Any) -> tuple[bool, Any]:
    return (v is not None, v)


def _assertion_key(row: sqlite3.Row) -> tuple:
    """The logical assertion key (asserter, evidence anchor), ordered like SQLite (NULLs first).
    Unique per (src, dst, edge_type) by `uq_edges_assertion`, and stable across a rebuild."""
    return (row["asserted_by"], _nulls_first(row["evidence_source_id"]),
            _nulls_first(row["evidence_char_start"]), _nulls_first(row["evidence_char_end"]))


def _stream_rows(conn: sqlite3.Connection, node_id: str, direction: str, status: str,
                 edge_type: str | None, limit: int, *, other_range: str | None = None
                 ) -> list[sqlite3.Row]:
    """One indexed stream in the stable order (edge_type, other endpoint, assertion key), at most
    `limit` rows. The index serves the order, so SQLite reads at most `limit` entries (no sort).
    `other_range` ("lt"/"gt") keeps only other endpoints below/above `node_id`: an index range, so
    excluding self-loops never scans them (a residual `src_id <> dst_id` filter would)."""
    end, other, index = (("src_id", "dst_id", "idx_edges_src_walk") if direction == "out"
                         else ("dst_id", "src_id", "idx_edges_dst_walk"))
    sql = f"SELECT * FROM edges INDEXED BY {index} WHERE {end} = ? AND status = ?"
    params: list[Any] = [node_id, status]
    if edge_type is not None:
        sql += " AND edge_type = ?"
        params.append(edge_type)
    if other_range is not None:
        if edge_type is None:
            raise ValueError("an other-endpoint range needs an edge type")
        sql += f" AND {other} {'<' if other_range == 'lt' else '>'} ?"
        params.append(node_id)
    sql += (f" ORDER BY {other}, {_KEY_SQL}" if edge_type is not None
            else f" ORDER BY edge_type, {other}, {_KEY_SQL}")
    return conn.execute(sql + " LIMIT ?", [*params, limit]).fetchall()


def _adjacency(conn: sqlite3.Connection, node_id: str,
               streams: list[tuple[str, str, str | None]], k: int) -> tuple[list[sqlite3.Row], bool]:
    """A node's top-k distinct edges over its streams, and whether more than k exist.

    Each stream fetches at most k + 1 rows in the shared order; merging them and deduplicating by
    edge_id yields the node's global top k (each of those ranks within k in its own stream). More
    than k distinct edges exist exactly when the merge has more than k, so a self-loop seen from
    both directions never truncates falsely."""
    merged: dict[str, sqlite3.Row] = {}
    for direction, status, edge_type in streams:
        for row in _stream_rows(conn, node_id, direction, status, edge_type, k + 1):
            merged.setdefault(row["edge_id"], row)
    ordered = sorted(merged.values(),
                     key=lambda r: (r["edge_type"], _other(r, node_id), _assertion_key(r)))
    return ordered[:k], len(ordered) > k


class _Budget:
    """The query streams one traversal may still run. A node is expanded only if all of its streams
    fit: a partial fetch would change which edges are its top k."""

    def __init__(self, streams: int) -> None:
        self.left = streams

    def reserve(self, n: int) -> bool:
        if n > self.left:
            return False
        self.left -= n
        return True


def _bounded_bfs(conn: sqlite3.Connection, seeds: list[str], *, depth: int,
                 statuses: tuple[str, ...], edge_types: tuple[str, ...] | None,
                 admit, node_cap: int, edge_cap: int, fanout: int, budget: int,
                 ) -> tuple[dict[str, int], list[sqlite3.Row], bool]:
    """The bounded BFS shared by the neighborhood and the search subgraph (ADR-0068 decision 7).

    `seeds` are already admitted, in rank order. Each step expands the frontier in rank order while
    the query budget covers a node's streams, then admits new endpoints round-robin across the
    expanded nodes (each node's candidates in its fan-out order) until `node_cap`. Retained nodes
    never expanded (the outer ring) get one edge-only fetch from the same budget. Output edges: the
    deduplicated union of every fetched retained node's top-k edges among retained endpoints, in
    a deterministic order, then `edge_cap`. Returns (visited -> distance in admission order, edge
    rows, truncated)."""
    fanout = max(1, min(fanout, HARD_MAX_FANOUT))
    left = _Budget(max(0, min(budget, HARD_MAX_QUERY_BUDGET)))
    streams = _streams(statuses, edge_types)
    visited: dict[str, int] = {sid: 0 for sid in seeds}
    adjacency: dict[str, list[sqlite3.Row]] = {}
    truncated = False

    def fetch(nid: str) -> bool:
        nonlocal truncated
        if not left.reserve(len(streams)):
            truncated = True                       # a retained node's adjacency is not fetched
            return False
        rows, more = _adjacency(conn, nid, streams, fanout)
        adjacency[nid] = rows
        truncated = truncated or more
        return True

    frontier = list(seeds)
    for dist in range(1, depth + 1):
        expanded = []
        for nid in frontier:
            if not fetch(nid):
                break
            expanded.append(nid)
        lists = [list(dict.fromkeys(_other(r, nid) for r in adjacency[nid])) for nid in expanded]
        fresh = {c for lst in lists for c in lst if c not in visited}
        meta = _node_meta_map(conn, fresh)         # only this step's retained edges' endpoints
        lists = [[c for c in lst if c in fresh and admit(meta.get(c))] for lst in lists]
        added: list[str] = []
        depth_of = max((len(lst) for lst in lists), default=0)
        for i in range(depth_of):                  # round-robin across the expanded nodes
            for lst in lists:
                if i >= len(lst) or lst[i] in visited:
                    continue
                if len(visited) >= node_cap:
                    truncated = True
                    break
                visited[lst[i]] = dist
                added.append(lst[i])
            else:
                continue
            break
        if not added:
            break
        frontier = added

    for nid in list(visited):                      # the outer ring: edges only, no new nodes
        if nid not in adjacency and not fetch(nid):
            break

    retained = set(visited)
    picked: dict[str, sqlite3.Row] = {}
    for rows in adjacency.values():
        for row in rows:
            if row["src_id"] in retained and row["dst_id"] in retained:
                picked.setdefault(row["edge_id"], row)
    edge_rows = sorted(picked.values(), key=lambda r: (r["edge_type"], r["src_id"], r["dst_id"],
                                                       _assertion_key(r)))
    if len(edge_rows) > edge_cap:
        truncated = True
        edge_rows = edge_rows[:edge_cap]
    return visited, edge_rows, truncated


# --------------------------------------------------------------------------- projections


def node_view(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    include_status: tuple[str, ...] = DEFAULT_EDGE_STATUSES,
    edge_limit: int = DEFAULT_EDGE_LIMIT,
) -> dict[str, Any] | None:
    """Node metadata + adjacent assertions grouped by ``edge_type`` (outgoing/incoming).

    Bounded (ADR-0068 decision 7): each (direction, edge type, status) group fetches at most
    ``edge_limit + 1`` rows through the traversal indexes and returns at most ``edge_limit``;
    metadata is loaded only for the returned endpoints. ``returned`` counts what is listed (never
    the full degree: counting a hub's edges is itself an unbounded scan); ``truncated_groups``
    names the groups that had more, and ``truncated`` is set if any did. A self-loop is listed once,
    under ``outgoing``. Returns ``None`` if the node does not exist."""
    node = graph.get_node(conn, node_id)
    if node is None:
        return None
    require_indexes(conn)
    limit = max(1, min(edge_limit, HARD_MAX_EDGE_LIMIT))
    groups: list[tuple[str, str, str, list[sqlite3.Row]]] = []
    truncated_groups: list[dict[str, str]] = []
    for direction in ("out", "in"):
        for edge_type in sorted(graph.EDGE_TYPES):
            for status in include_status:
                if direction == "out":
                    rows = _stream_rows(conn, node_id, direction, status, edge_type, limit + 1)
                else:                              # self-loops are listed under outgoing
                    rows = [*_stream_rows(conn, node_id, direction, status, edge_type, limit + 1,
                                          other_range="lt"),
                            *_stream_rows(conn, node_id, direction, status, edge_type, limit + 1,
                                          other_range="gt")][:limit + 1]
                if len(rows) > limit:
                    rows = rows[:limit]
                    truncated_groups.append({"direction": "outgoing" if direction == "out"
                                             else "incoming", "edge_type": edge_type,
                                             "status": status})
                if rows:
                    groups.append((direction, edge_type, status, rows))
    meta = _node_meta_map(conn, {_other(r, node_id) for *_, rows in groups for r in rows}
                          - {node_id})
    meta[node_id] = _node_meta(node)               # a self-loop's other end is the node itself

    outgoing: dict[str, list[dict[str, Any]]] = {}
    incoming: dict[str, list[dict[str, Any]]] = {}
    buckets: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for direction, edge_type, _status, rows in groups:
        buckets.setdefault((direction, edge_type), []).extend(rows)
    for (direction, edge_type), rows in buckets.items():
        rows.sort(key=lambda r: (r["src_id"], r["dst_id"], _assertion_key(r), r["status"]))
        (outgoing if direction == "out" else incoming)[edge_type] = [
            _assertion_obj(r, node_id, meta) for r in rows]

    return {
        "node": _node_meta(node),
        "outgoing": outgoing,
        "incoming": incoming,
        "returned": {
            "outgoing": sum(len(v) for v in outgoing.values()),
            "incoming": sum(len(v) for v in incoming.values()),
        },
        "truncated_groups": truncated_groups,
        "truncated": bool(truncated_groups),
        "cap": {"edges_per_group": limit},
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
    fanout: int = DEFAULT_FANOUT,
) -> dict[str, Any] | None:
    """Bounded BFS neighborhood as a flat node/edge payload. ``None`` if the root is absent.

    Nodes are discovered by BFS out to ``depth`` (clamped to ``[0, MAX_DEPTH]``), honoring the
    ``node_types`` filter (the root is always included). Each node contributes at most ``fanout``
    edges per step, and the traversal runs at most :data:`NEIGHBORHOOD_QUERY_BUDGET` query streams
    (ADR-0068 decision 7). Edges are **induced within fan-out** (ADR-0032 addendum 10): every
    retained node's top-k edges among the retained nodes, so outer-ring cross-edges are kept. Both
    sets are capped (``node_cap``/``edge_cap``) and ``truncated`` is set whenever a fan-out, budget,
    node or edge limit omitted anything.

    ``node_types`` filtering is **traversal-time**, not output-only: a node whose type is excluded
    is never entered, so it also blocks discovery of anything reachable *only* through it. This
    keeps the result a connected neighborhood of the requested types rather than a "traverse
    everything, then filter" view.
    """
    root = graph.get_node(conn, root_id)
    if root is None:
        return None
    require_indexes(conn)
    depth = max(0, min(depth, MAX_DEPTH))

    def admit(meta: dict[str, Any] | None) -> bool:
        node_type = meta["node_type"] if meta else None
        return node_types is None or node_type in node_types

    visited, edge_rows, truncated = _bounded_bfs(
        conn, [root_id], depth=depth, statuses=tuple(include_status), edge_types=edge_types,
        admit=admit, node_cap=node_cap, edge_cap=edge_cap, fanout=fanout,
        budget=NEIGHBORHOOD_QUERY_BUDGET)
    meta = _node_meta_map(conn, set(visited))
    nodes = [{**(meta.get(nid) or _unknown_meta(nid)), "distance": visited[nid]}
             for nid in sorted(visited, key=lambda n: (visited[n], n))]
    return {
        "root_id": root_id,
        "depth": depth,
        "nodes": nodes,
        "edges": [_edge_obj(row) for row in edge_rows],
        "truncated": truncated,
        "cap": {"nodes": node_cap, "edges": edge_cap,
                "fanout": max(1, min(fanout, HARD_MAX_FANOUT))},
    }


def active_contradiction_endpoints(conn: sqlite3.Connection, limit: int) -> tuple[list[str], bool]:
    """Up to ``limit`` node ids on either end of an ``active`` ``contradicts`` edge (the topic-less
    disagreement seeds), and whether the limit clipped them (ADR-0068 decision 7).

    Lets a "which sources disagree" query surface contradictions directly from the graph, without
    depending on the literal trigger words matching any page text. Bounded before any metadata
    lookup: at most ``limit + 1`` edges are read, through the ``(edge_type, status, …)`` index in
    a stable order. Reading ``limit + 1`` edges means there may be more, so that counts as clipped.
    """
    limit = max(0, limit)
    rows = conn.execute(
        "SELECT src_id, dst_id FROM edges INDEXED BY idx_edges_type_walk "
        "WHERE edge_type = 'contradicts' AND status = 'active' ORDER BY src_id, dst_id LIMIT ?",
        (limit + 1,)).fetchall()
    ids = list(dict.fromkeys(nid for r in rows for nid in (r["src_id"], r["dst_id"])))
    return ids[:limit], len(rows) > limit or len(ids) > limit


def source_item_types(
    conn: sqlite3.Connection, source_ids: set[str]
) -> dict[str, frozenset[str]]:
    """The active-only chunk↔item_type bridge for ADR-0062 evidence faceting.

    Map each ``source_id`` → the set of ``item_type``s of the **active** item nodes that reach it
    by an **active** ``mentions`` edge (mentions is ``source → item``, so the source is ``src_id``).
    Batched (one query over all candidate sources), never per chunk. A deprecated/candidate item or
    a superseded mention does not bridge — this is a ranking signal, and only live topics steer it.
    Only **kept** links bridge (ADR-0066 review round 2: ``asserted_by = 'human'``) — an undecided
    suggestion is not a tag the user applied.
    """
    out: dict[str, set[str]] = {}
    if not source_ids:
        return {}
    ph = ",".join("?" for _ in source_ids)
    rows = conn.execute(
        f"""SELECT e.src_id AS source_id, n.item_type AS item_type
            FROM edges e JOIN nodes n ON n.node_id = e.dst_id
            WHERE e.edge_type = 'mentions' AND e.status = 'active' AND e.asserted_by = 'human'
              AND n.node_type = 'item' AND n.status = 'active'
              AND n.item_type IS NOT NULL
              AND e.src_id IN ({ph})""",
        tuple(source_ids),
    ).fetchall()
    for r in rows:
        out.setdefault(r["source_id"], set()).add(r["item_type"])
    return {sid: frozenset(types) for sid, types in out.items()}


def search_subgraph(
    conn: sqlite3.Connection,
    seed_ids: list[str],
    *,
    depth: int,
    edge_statuses: tuple[str, ...] = DEFAULT_EDGE_STATUSES,
    node_statuses: tuple[str, ...] | None = None,
    node_types: frozenset[str] | None = None,
    item_types: frozenset[str] | None = None,
    edge_types: tuple[str, ...] | None = None,
    node_cap: int,
    edge_cap: int,
    fanout: int = DEFAULT_FANOUT,
    budget: int = NEIGHBORHOOD_QUERY_BUDGET,
    seeds_truncated: bool = False,
) -> dict[str, Any]:
    """Multi-seed bounded BFS returning a flat ``{seeds, nodes, edges, depth, truncated}`` subgraph.

    Used by ``/search`` (Phase 4c). Unlike :func:`neighborhood` (a single-root ``/graph/*`` view
    that is edge-status-only by contract), this accepts an optional ``node_statuses`` retention
    filter — a node is admitted only if its status is allowed — so the search layer can exclude
    archived/deleted nodes (ADR-0032 addendum 2: retention is ``/search``'s job, not the raw graph
    projection's). Seeds that do not exist or fail the filters are dropped; the rest keep the
    caller's (BM25) order, which the bounded traversal (ADR-0068 decision 7) preserves.
    ``seeds_truncated`` carries a clipped seed list into ``truncated``.

    ``item_types`` (ADR-0062) is a **type predicate, not a layer filter**: an *item* node is
    admitted only if its ``item_type`` is in the set, but non-item nodes (claim/synthesis) always
    pass — the facet narrows the item layer without dropping the rest.
    """
    require_indexes(conn)
    depth = max(0, min(depth, MAX_DEPTH))

    def admit(meta: dict[str, Any] | None) -> bool:
        if meta is None:
            return False
        if node_types is not None and meta["node_type"] not in node_types:
            return False
        if item_types is not None and meta["node_type"] == "item" and meta["item_type"] not in item_types:
            return False
        if node_statuses is not None and meta["status"] not in node_statuses:
            return False
        return True

    truncated = seeds_truncated
    admitted_seeds: list[str] = []
    seed_meta = _node_meta_map(conn, set(seed_ids))
    for sid in seed_ids:  # preserve caller ordering (BM25 rank)
        if sid in admitted_seeds or not admit(seed_meta.get(sid)):
            continue
        if len(admitted_seeds) >= node_cap:
            truncated = True
            break
        admitted_seeds.append(sid)

    visited, edge_rows, clipped = _bounded_bfs(
        conn, admitted_seeds, depth=depth, statuses=tuple(edge_statuses), edge_types=edge_types,
        admit=admit, node_cap=node_cap, edge_cap=edge_cap, fanout=fanout, budget=budget)
    meta = _node_meta_map(conn, set(visited))
    nodes = [
        {**(meta.get(nid) or _unknown_meta(nid)), "distance": visited[nid]}
        for nid in sorted(visited, key=lambda n: (visited[n], n))
    ]
    return {"seeds": admitted_seeds, "nodes": nodes, "edges": [_edge_obj(r) for r in edge_rows],
            "depth": depth, "truncated": truncated or clipped}
