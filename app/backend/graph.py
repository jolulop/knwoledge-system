#!/usr/bin/env python3
"""Phase 3.5b semantic graph store (`db/graph.sqlite`, ADR-0029/0030).

The graph is authoritative for **relationships**; node metadata stays owned by wiki
frontmatter (and, for sources, by manifests). Each `edges` row is one *assertion* of a
relationship — distinct evidence spans and coexisting LLM/human assertions live as separate
rows — and a relationship exists/projects iff it has an `active` assertion. The `nodes`
table is a derived index rebuilt from frontmatter + manifests, never a second authority.

Edge vocabulary is Build Spec §6.2 **minus `needs_review`** (review is a `status`, not a
relationship). Dependency-free (stdlib sqlite3), same shape as `db.py`.
"""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

from app.backend import taxonomy
from app.backend.manifests import iso_now

# Build Spec §6.1 node types under the ADR-0059 overlay: the old concept/entity/person/
# organization/project family collapsed into the single structural type `item` (type-neutral
# `itm_` id); classification lives in `nodes.item_type` (taxonomy.py), mutable and governed.
NODE_TYPES = frozenset(
    {"source", "item", "claim", "tag", "query", "synthesis"}
)
# Build Spec §6.2 minus needs_review (ADR-0030: review is a status, not an edge type).
EDGE_TYPES = frozenset(
    {"mentions", "supports", "contradicts", "supersedes", "duplicates",
     "derived_from", "related_to"}
)
EDGE_STATUSES = frozenset({"proposed", "active", "rejected", "superseded"})
ASSERTED_BY = frozenset({"deterministic", "llm", "human", "authored_wikilink"})
# Node lifecycle statuses (ADR-0022 / policies/retention.yaml); the derived nodes index
# mirrors the page/manifest status and is validated against this set.
NODE_STATUSES = frozenset(
    {"active", "candidate", "stale_candidate", "deprecated_candidate",
     "archive_candidate", "archived", "delete_candidate", "deleted",
     "hidden",            # ADR-0043: governance visibility-suppression status (active -> hidden)
     "evidence_hidden",   # ADR-0049: synthesis auto-suppressed because a supporting claim is hidden
     "merged"}            # ADR-0050: absorbed identity tombstone (merged_into a same-type survivor)
    # ADR-0051's `rekeyed` tombstone is retired by ADR-0059: a type change is a metadata flip
    # on the type-neutral id, never an identity rekey, so the status can no longer arise.
)
# Endpoint-type contract per edge type (ADR-0030). `None` = unconstrained on that side;
# SAME_TYPE_EDGES require src and dst to share a node_type. Enforced by validate_graph (not
# at write time, to leave producer ordering free); extendable only by ADR.
EDGE_ENDPOINTS: dict[str, tuple[frozenset[str] | None, frozenset[str] | None]] = {
    "mentions": (None, None),
    "derived_from": (frozenset({"claim", "synthesis", "item"}),
                     frozenset({"source", "claim", "synthesis"})),
    "supports": (frozenset({"claim", "synthesis"}), frozenset({"claim", "synthesis"})),
    "contradicts": (frozenset({"claim", "synthesis"}), frozenset({"claim", "synthesis"})),
    "related_to": (None, None),
}
SAME_TYPE_EDGES = frozenset({"supersedes", "duplicates"})

# Bump when the graph schema changes; recorded via PRAGMA user_version for migration.
# v2 (ADR-0059): `nodes.item_type` — the governed classification of `item` nodes (NULL for
# every other node_type). No in-place migration path: v2 ships with the clean-repository
# restart, so a v1 database is simply stale (the ADR-0057 sweep preflight refuses it).
SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id    TEXT PRIMARY KEY,
    node_type  TEXT NOT NULL,
    item_type  TEXT,
    slug       TEXT,
    status     TEXT,
    indexed_at TEXT
);
CREATE TABLE IF NOT EXISTS edges (
    edge_id             TEXT PRIMARY KEY,
    src_id              TEXT NOT NULL,
    dst_id              TEXT NOT NULL,
    edge_type           TEXT NOT NULL,
    status              TEXT NOT NULL,
    asserted_by         TEXT NOT NULL,
    confidence          REAL,
    evidence_source_id  TEXT,
    evidence_char_start INTEGER,
    evidence_char_end   INTEGER,
    review_id           TEXT,
    job_id              TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id, status);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id, status);
-- Assertion identity (ADR-0030). COALESCE makes it null-safe — a plain UNIQUE would treat
-- NULL evidence anchors as distinct and let duplicates through; this rejects them even via
-- raw SQL or a future import path, not just the write API.
CREATE UNIQUE INDEX IF NOT EXISTS uq_edges_assertion ON edges(
    src_id, dst_id, edge_type, asserted_by,
    COALESCE(evidence_source_id, ''),
    COALESCE(evidence_char_start, -1),
    COALESCE(evidence_char_end, -1)
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


class GraphSchemaError(RuntimeError):
    """An existing graph database has a pre-ADR-0059 schema; the clean restart rebuilds it."""


def _nodes_table_has_item_type(conn: sqlite3.Connection) -> bool:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    return "item_type" in cols


def init_db(db_path: Path) -> None:
    """Create (or verify) the graph schema. HARD-FAILS on a pre-v2 database (ADR-0059).

    There is deliberately NO migration path: the schema bump ships with the clean-repository
    restart, so an existing v1 database is stale by design. `CREATE TABLE IF NOT EXISTS`
    would silently keep the old `nodes` shape while the version stamp claimed v2 — the
    structural check refuses BEFORE any write, so producers can never "upgrade" a v1 vault
    into a lying half-state (review round: B1)."""
    conn = connect(db_path)
    try:
        found = schema_version(conn)
        has_nodes = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'nodes'"
        ).fetchone() is not None
        if (has_nodes and not _nodes_table_has_item_type(conn)) or \
                found not in (0, SCHEMA_VERSION):
            raise GraphSchemaError(
                f"graph database at {db_path} is pre-ADR-0059 (schema v{found}); the "
                f"clean-repository restart rebuilds it — refusing to modify")
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _has_node(conn: sqlite3.Connection, node_id: str) -> bool:
    return conn.execute("SELECT 1 FROM nodes WHERE node_id = ?", (node_id,)).fetchone() is not None


def is_safe_slug(slug: Any) -> bool:
    """True iff `slug` is a single safe filename component — a non-empty string with no path separators
    (`/`, `\\`) and not `.`/`..` (ADR-0009 path-containment, at the graph boundary). Downstream renderers
    build `wiki_dir / NODE_DIR[type] / f"{slug}.md"` from `nodes.slug`, so an unsafe slug could escape the
    wiki dir; a legitimate `items._slug()` value (`[a-z0-9-]+`) always passes. Shared by `upsert_node`
    (the single normal write into `nodes`) and `validate_graph` (the tampered-DB / raw-SQL backstop) so both
    enforce the exact same rule. An unsafe slug is structural corruption, never governance business logic."""
    return (isinstance(slug, str) and bool(slug) and slug not in (".", "..")
            and "/" not in slug and "\\" not in slug)


def upsert_node(
    conn: sqlite3.Connection,
    *,
    node_id: str,
    node_type: str,
    slug: str | None = None,
    status: str | None = None,
    item_type: str | None = None,
    now: str | None = None,
) -> None:
    """Index one node (producers call this before asserting edges, ADR-0030).

    ADR-0059: an `item` node carries a validated `item_type` (taxonomy.py). When the caller
    omits it, the existing row's value is preserved (a status-only mirror update must not
    null the classification); a brand-new item row without one is a producer bug and fails.
    Non-item nodes must not carry one."""
    if node_type not in NODE_TYPES:
        raise ValueError(f"unknown node_type {node_type!r}; allowed: {sorted(NODE_TYPES)}")
    if status is not None and status not in NODE_STATUSES:
        raise ValueError(f"unknown node status {status!r}; allowed: {sorted(NODE_STATUSES)}")
    if slug is not None and not is_safe_slug(slug):
        # Structural corruption, not business logic — reject at the graph boundary so no downstream
        # renderer builds an escaping path. Don't echo the (possibly path-like) value into the message.
        raise ValueError("unsafe node slug; must be a single safe filename component (no path separators, "
                         "not '.'/'..')")
    if node_type == "item":
        if item_type is None:
            row = conn.execute(
                "SELECT item_type FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
            item_type = row["item_type"] if row else None
        if not taxonomy.is_item_type(item_type):
            raise ValueError(f"item node requires a valid item_type; got {item_type!r}")
    elif item_type is not None:
        raise ValueError(f"item_type is only valid on item nodes, not {node_type!r}")
    conn.execute(
        "INSERT OR REPLACE INTO nodes (node_id, node_type, item_type, slug, status, indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (node_id, node_type, item_type, slug, status, now or iso_now()),
    )
    conn.commit()


# --- edges (assertions) -----------------------------------------------------


def upsert_assertion(
    conn: sqlite3.Connection,
    *,
    src_id: str,
    dst_id: str,
    edge_type: str,
    asserted_by: str,
    status: str = "proposed",
    confidence: float | None = None,
    evidence_source_id: str | None = None,
    evidence_char_start: int | None = None,
    evidence_char_end: int | None = None,
    review_id: str | None = None,
    job_id: str | None = None,
    now: str | None = None,
) -> str:
    """Insert or update one relationship assertion; return its edge_id (idempotent).

    The assertion identity is (src, dst, edge_type, asserted_by, evidence anchor) — distinct
    spans/asserters are distinct rows, while a re-run of the same assertion updates it in
    place rather than duplicating. Idempotency is enforced both by the null-safe unique index
    (`uq_edges_assertion`, which uses `COALESCE` so NULL anchors don't slip past it) and by
    this upsert's `IS`-based lookup, which updates the existing row in place.
    """
    if edge_type not in EDGE_TYPES:
        raise ValueError(f"unknown edge_type {edge_type!r}; allowed: {sorted(EDGE_TYPES)}")
    if status not in EDGE_STATUSES:
        raise ValueError(f"unknown edge status {status!r}; allowed: {sorted(EDGE_STATUSES)}")
    if asserted_by not in ASSERTED_BY:
        raise ValueError(f"unknown asserted_by {asserted_by!r}; allowed: {sorted(ASSERTED_BY)}")
    # No dangling edges: both endpoints must be indexed nodes first (ADR-0030). Producers
    # call upsert_node before asserting; validate_graph is the backstop for raw SQL.
    if not _has_node(conn, src_id):
        raise ValueError(f"src_id {src_id!r} is not an indexed node; index it first")
    if not _has_node(conn, dst_id):
        raise ValueError(f"dst_id {dst_id!r} is not an indexed node; index it first")
    now = now or iso_now()

    row = conn.execute(
        """SELECT edge_id FROM edges
           WHERE src_id = ? AND dst_id = ? AND edge_type = ? AND asserted_by = ?
             AND evidence_source_id IS ? AND evidence_char_start IS ? AND evidence_char_end IS ?""",
        (src_id, dst_id, edge_type, asserted_by,
         evidence_source_id, evidence_char_start, evidence_char_end),
    ).fetchone()

    if row is not None:
        edge_id = row["edge_id"]
        conn.execute(
            "UPDATE edges SET status = ?, confidence = ?, review_id = ?, job_id = ?, "
            "updated_at = ? WHERE edge_id = ?",
            (status, confidence, review_id, job_id, now, edge_id),
        )
    else:
        edge_id = f"edg_{uuid.uuid4().hex[:16]}"
        conn.execute(
            """INSERT INTO edges (
                edge_id, src_id, dst_id, edge_type, status, asserted_by, confidence,
                evidence_source_id, evidence_char_start, evidence_char_end,
                review_id, job_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (edge_id, src_id, dst_id, edge_type, status, asserted_by, confidence,
             evidence_source_id, evidence_char_start, evidence_char_end,
             review_id, job_id, now, now),
        )
    conn.commit()
    return edge_id


def set_status(conn: sqlite3.Connection, edge_id: str, status: str, *, now: str | None = None) -> None:
    """Transition one assertion's review status (approve/reject/supersede)."""
    if status not in EDGE_STATUSES:
        raise ValueError(f"unknown edge status {status!r}; allowed: {sorted(EDGE_STATUSES)}")
    cur = conn.execute(
        "UPDATE edges SET status = ?, updated_at = ? WHERE edge_id = ?",
        (status, now or iso_now(), edge_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"no edge {edge_id!r}; status transition would be a silent no-op")
    conn.commit()


def find_assertion(
    conn: sqlite3.Connection, *, src_id: str, dst_id: str, edge_type: str, asserted_by: str,
    evidence_source_id: str | None = None, evidence_char_start: int | None = None,
    evidence_char_end: int | None = None,
) -> dict[str, Any] | None:
    """The edge row matching the FULL assertion identity (`uq_edges_assertion`), in ANY status, or None
    (ADR-0050 merge collision check — the unique index is status-agnostic, so a re-point can land on an
    existing inactive row of the same identity)."""
    row = conn.execute(
        """SELECT * FROM edges WHERE src_id = ? AND dst_id = ? AND edge_type = ? AND asserted_by = ?
             AND evidence_source_id IS ? AND evidence_char_start IS ? AND evidence_char_end IS ?""",
        (src_id, dst_id, edge_type, asserted_by,
         evidence_source_id, evidence_char_start, evidence_char_end),
    ).fetchone()
    return dict(row) if row is not None else None


def repoint_edge(
    conn: sqlite3.Connection, edge_id: str, *, new_src: str | None = None, new_dst: str | None = None,
    now: str | None = None,
) -> None:
    """Re-point an existing edge's endpoint(s) IN PLACE (ADR-0050 merge). Preserves the `edge_id` and ALL
    provenance (`asserted_by`/`review_id`/`job_id`/evidence/`status`/`confidence`); only `src_id`/`dst_id`
    (+ `updated_at`) change. The caller must first ensure the resulting full assertion identity is free
    (no `uq_edges_assertion` collision) — otherwise it raises (a collision is the caller's to resolve)."""
    sets: list[str] = []
    params: list[Any] = []
    if new_src is not None:
        sets.append("src_id = ?")
        params.append(new_src)
    if new_dst is not None:
        sets.append("dst_id = ?")
        params.append(new_dst)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(now or iso_now())
    params.append(edge_id)
    conn.execute(f"UPDATE edges SET {', '.join(sets)} WHERE edge_id = ?", params)


def reactivate_edge(conn: sqlite3.Connection, edge_id: str, *, review_id: str,
                    now: str | None = None) -> None:
    """Resurrect an inactive edge to `active` + stamp the authorizing `review_id` (ADR-0050 merge
    `resurrected_target_collision` — the merge, not the row's old proposal, authorizes the active status;
    asserted_by/evidence/job_id provenance is left untouched)."""
    conn.execute("UPDATE edges SET status = 'active', review_id = ?, updated_at = ? WHERE edge_id = ?",
                 (review_id, now or iso_now(), edge_id))


def supersede_source_edges(
    conn: sqlite3.Connection,
    source_id: str,
    *,
    edge_type: str = "derived_from",
    asserted_by: str = "llm",
    now: str | None = None,
) -> list[str]:
    """Mark a source's active assertions `superseded` before it is re-extracted (ADR-0030).

    Used on re-extraction so stale evidence (old char ranges into changed text) stops being
    authoritative without losing the audit trail. Returns the affected `src_id`s (the claim
    nodes) so the caller can recompose their pages from the surviving `active` assertions.
    """
    now = now or iso_now()
    affected = [
        r["src_id"] for r in conn.execute(
            "SELECT DISTINCT src_id FROM edges WHERE dst_id = ? AND edge_type = ? "
            "AND asserted_by = ? AND status = 'active'",
            (source_id, edge_type, asserted_by),
        )
    ]
    conn.execute(
        "UPDATE edges SET status = 'superseded', updated_at = ? WHERE dst_id = ? "
        "AND edge_type = ? AND asserted_by = ? AND status = 'active'",
        (now, source_id, edge_type, asserted_by),
    )
    conn.commit()
    return affected


def supersede_mentions_for_source(
    conn: sqlite3.Connection, source_id: str, *, now: str | None = None
) -> list[str]:
    """Supersede a source's active `mentions` before it is re-extracted; return the affected
    node ids (knowledge items) to recompose. The mirror of `supersede_source_edges`, but
    `mentions` runs source→node (the source is the `src_id`)."""
    now = now or iso_now()
    affected = [
        r["dst_id"] for r in conn.execute(
            "SELECT DISTINCT dst_id FROM edges WHERE src_id = ? AND edge_type = 'mentions' "
            "AND asserted_by = 'llm' AND status = 'active'",
            (source_id,),
        )
    ]
    conn.execute(
        "UPDATE edges SET status = 'superseded', updated_at = ? WHERE src_id = ? "
        "AND edge_type = 'mentions' AND asserted_by = 'llm' AND status = 'active'",
        (now, source_id),
    )
    conn.commit()
    return affected


def mentions_for_source(conn: sqlite3.Connection, source_id: str) -> list[dict[str, Any]]:
    """Active nodes a source mentions, with their type/item_type and slug (Source-page projection)."""
    return _rows(conn.execute(
        "SELECT DISTINCT e.dst_id, n.node_type, n.item_type, n.slug FROM edges e "
        "JOIN nodes n ON n.node_id = e.dst_id "
        "WHERE e.src_id = ? AND e.edge_type = 'mentions' AND e.status = 'active' "
        "ORDER BY n.node_type, n.slug, e.dst_id",
        (source_id,),
    ))


def sources_for_node(conn: sqlite3.Connection, node_id: str) -> list[str]:
    """Active sources that mention a node (the node page's Mentioned-by projection)."""
    return [
        r["src_id"] for r in conn.execute(
            "SELECT DISTINCT src_id FROM edges WHERE dst_id = ? AND edge_type = 'mentions' "
            "AND status = 'active' ORDER BY src_id",
            (node_id,),
        )
    ]


def superseded_mention_sources(conn: sqlite3.Connection, node_id: str) -> list[str]:
    """Distinct sources whose `mentions` of a node were superseded (retirement provenance).

    The ADR-0058 retired-section attribution set `H`: a recompose-tombstoned node is shown
    under source S only when this history is exactly `{S}` — multi-source or empty history
    stays flat-queue-only.
    """
    return [
        r["src_id"] for r in conn.execute(
            "SELECT DISTINCT src_id FROM edges WHERE dst_id = ? AND edge_type = 'mentions' "
            "AND status = 'superseded' ORDER BY src_id",
            (node_id,),
        )
    ]


def get_node(conn: sqlite3.Connection, node_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT node_id, node_type, item_type, slug, status FROM nodes WHERE node_id = ?",
        (node_id,)
    ).fetchone()
    return dict(row) if row else None


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cursor.fetchall()]


def outgoing_active(conn: sqlite3.Connection, src_id: str) -> list[dict[str, Any]]:
    """Active assertions originating at a node (for forward link projection)."""
    return _rows(conn.execute(
        "SELECT * FROM edges WHERE src_id = ? AND status = 'active' "
        "ORDER BY edge_type, dst_id, edge_id",
        (src_id,),
    ))


def incoming_active(conn: sqlite3.Connection, dst_id: str) -> list[dict[str, Any]]:
    """Active assertions pointing at a node (for backlink projection)."""
    return _rows(conn.execute(
        "SELECT * FROM edges WHERE dst_id = ? AND status = 'active' "
        "ORDER BY edge_type, src_id, edge_id",
        (dst_id,),
    ))


def active_duplicates(conn: sqlite3.Connection, node_id: str) -> list[dict[str, Any]]:
    """Partner nodes of active `duplicates` edges touching ``node_id`` (ADR-0041).

    `duplicates` is symmetric + canonical-ordered (src_id < dst_id), so a node's partner can be on
    either side; this returns the *other* endpoint of each active edge as ``{node_id, node_type, slug}``,
    deduped and slug-sorted, for the body-only ``## Duplicates`` page projection.
    """
    partner_ids: list[str] = []
    seen: set[str] = set()
    for e in outgoing_active(conn, node_id):
        if e["edge_type"] == "duplicates" and e["dst_id"] not in seen:
            seen.add(e["dst_id"])
            partner_ids.append(e["dst_id"])
    for e in incoming_active(conn, node_id):
        if e["edge_type"] == "duplicates" and e["src_id"] not in seen:
            seen.add(e["src_id"])
            partner_ids.append(e["src_id"])
    partners: list[dict[str, Any]] = []
    for pid in partner_ids:
        n = get_node(conn, pid)
        if n is not None:
            partners.append({"node_id": pid, "node_type": n["node_type"], "slug": n["slug"]})
    return sorted(partners, key=lambda d: d["slug"])


def claims_for_source(conn: sqlite3.Connection, source_id: str) -> list[str]:
    """Active claim ids derived from a source (for the Source-page Claims projection)."""
    return [
        r["src_id"] for r in conn.execute(
            "SELECT DISTINCT src_id FROM edges WHERE dst_id = ? AND edge_type = 'derived_from' "
            "AND status = 'active' ORDER BY src_id",
            (source_id,),
        )
    ]


def active_node_ids_of_type(conn: sqlite3.Connection, node_type: str) -> list[str]:
    """Active node ids of a given type (e.g. the active claims to compare, Phase 3.5c)."""
    return [
        r["node_id"] for r in conn.execute(
            "SELECT node_id FROM nodes WHERE node_type = ? AND status = 'active' "
            "ORDER BY node_id",
            (node_type,),
        )
    ]


def nodes_of_type(conn: sqlite3.Connection, node_type: str) -> list[dict[str, Any]]:
    """All nodes of a type with id/slug/status, any status (e.g. to enumerate syntheses for
    retraction, Phase 3.5c)."""
    return _rows(conn.execute(
        "SELECT node_id, slug, status FROM nodes WHERE node_type = ? ORDER BY node_id",
        (node_type,),
    ))


def sources_for_claim(conn: sqlite3.Connection, claim_id: str) -> list[str]:
    """Active sources a claim is derived from (claim → source `derived_from`; the inverse of
    `claims_for_source`). Used to find a claim's blocking neighborhood and primary citation."""
    return [
        r["dst_id"] for r in conn.execute(
            "SELECT DISTINCT e.dst_id FROM edges e JOIN nodes n ON n.node_id = e.dst_id "
            "AND n.node_type = 'source' WHERE e.src_id = ? AND e.edge_type = 'derived_from' "
            "AND e.status = 'active' ORDER BY e.dst_id",
            (claim_id,),
        )
    ]


def item_ids_for_source(conn: sqlite3.Connection, source_id: str) -> set[str]:
    """Active knowledge items a source mentions (its blocking neighborhood, ADR-0059)."""
    return {
        r["dst_id"] for r in conn.execute(
            "SELECT DISTINCT e.dst_id FROM edges e JOIN nodes n ON n.node_id = e.dst_id "
            "WHERE e.src_id = ? AND e.edge_type = 'mentions' AND e.status = 'active' "
            "AND n.node_type = 'item'",
            (source_id,),
        )
    }


def contradiction_assertions(
    conn: sqlite3.Connection,
    *,
    statuses: tuple[str, ...] = ("proposed", "active"),
    asserted_by: str = "llm",
) -> list[dict[str, Any]]:
    """`contradicts` assertion rows in the given statuses (Phase 3.5c stale detection)."""
    placeholders = ",".join("?" for _ in statuses)
    return _rows(conn.execute(
        f"SELECT * FROM edges WHERE edge_type = 'contradicts' AND asserted_by = ? "
        f"AND status IN ({placeholders}) ORDER BY src_id, dst_id, edge_id",
        (asserted_by, *statuses),
    ))


def claims_with_active_evidence(conn: sqlite3.Connection) -> set[str]:
    """Claim ids that still *stand* — have ≥1 `active` `derived_from` edge. A contradiction
    endpoint is "gone" (its `contradicts` edges should be superseded) only when a claim stops
    standing, i.e. tombstones — NOT when it is merely deprecated by a human supersede decision
    while keeping its evidence (ADR-0031). So endpoint validity is evidence-based, not
    node-status-based."""
    return {
        r["src_id"] for r in conn.execute(
            "SELECT DISTINCT e.src_id FROM edges e JOIN nodes n ON n.node_id = e.src_id "
            "AND n.node_type = 'claim' WHERE e.edge_type = 'derived_from' AND e.status = 'active'"
        )
    }


def supersede_contradictions_for_claim(
    conn: sqlite3.Connection, claim_id: str, *, now: str | None = None
) -> list[dict[str, Any]]:
    """Supersede every `proposed`/`active` `contradicts` assertion touching a claim that has
    stopped being an active node (tombstone / identity change). Keeps the endpoint invariant
    **local**: a relationship that needs an active claim endpoint stops being active the moment
    the endpoint does — so the claim-lifecycle path leaves the graph valid without waiting for a
    contradiction pass. Returns the affected rows so the caller can withdraw their pending
    reviews and re-render the surviving endpoints (ADR-0031)."""
    now = now or iso_now()
    rows = _rows(conn.execute(
        "SELECT * FROM edges WHERE edge_type = 'contradicts' AND status IN ('proposed', 'active') "
        "AND (src_id = ? OR dst_id = ?)",
        (claim_id, claim_id),
    ))
    conn.execute(
        "UPDATE edges SET status = 'superseded', updated_at = ? WHERE edge_type = 'contradicts' "
        "AND status IN ('proposed', 'active') AND (src_id = ? OR dst_id = ?)",
        (now, claim_id, claim_id),
    )
    conn.commit()
    return rows


def active_contradictions_for_claim(conn: sqlite3.Connection, claim_id: str) -> list[str]:
    """The other claim in each `active` `contradicts` assertion involving this claim.

    `contradicts` is symmetric and stored once with `src_id < dst_id`, so a claim can be on
    either endpoint — this returns the opposite endpoint for the Claim-page projection."""
    out: set[str] = set()
    for r in conn.execute(
        "SELECT src_id, dst_id FROM edges WHERE edge_type = 'contradicts' AND status = 'active' "
        "AND (src_id = ? OR dst_id = ?)",
        (claim_id, claim_id),
    ):
        out.add(r["dst_id"] if r["src_id"] == claim_id else r["src_id"])
    return sorted(out)


def contradiction_between(
    conn: sqlite3.Connection, claim_a: str, claim_b: str, *, asserted_by: str = "llm"
) -> list[dict[str, Any]]:
    """All `contradicts` assertion rows for a (sorted) claim pair, any status."""
    src, dst = sorted((claim_a, claim_b))
    return _rows(conn.execute(
        "SELECT * FROM edges WHERE edge_type = 'contradicts' AND asserted_by = ? "
        "AND src_id = ? AND dst_id = ? ORDER BY edge_id",
        (asserted_by, src, dst),
    ))


def count_independent_sources(
    conn: sqlite3.Connection,
    dst_id: str,
    *,
    edge_type: str = "mentions",
    source_statuses: tuple[str, ...] | None = None,
) -> int:
    """Distinct source_ids among a node's active assertions of the given type (promotion).

    Exact-duplicate sources already share one source_id (ADR-0007), so they count once here;
    same-family independence (ADR-0018) is a slice-5 concern layered on top of this count.
    """
    if source_statuses is not None and not source_statuses:
        return 0
    sql = (
        "SELECT COUNT(DISTINCT e.src_id) AS n FROM edges e "
        "JOIN nodes n ON n.node_id = e.src_id AND n.node_type = 'source' "
        "WHERE e.dst_id = ? AND e.edge_type = ? AND e.status = 'active'"
    )
    params: list[Any] = [dst_id, edge_type]
    if source_statuses is not None:
        sql += f" AND n.status IN ({','.join('?' for _ in source_statuses)})"
        params.extend(source_statuses)
    row = conn.execute(sql, params).fetchone()
    return int(row["n"])


# --- nodes (derived index) --------------------------------------------------


def reindex_nodes(
    conn: sqlite3.Connection,
    *,
    source_ids: list[str],
    page_nodes: list[dict[str, Any]],
    source_statuses: dict[str, str] | None = None,
    now: str | None = None,
) -> int:
    """Rebuild the derived `nodes` index from manifests (sources) + page frontmatter.

    Deterministic and edge-safe: it replaces only the `nodes` table and never touches
    `edges`. `page_nodes` are dicts of {node_id, node_type, item_type?, slug, status} taken
    from the pages' frontmatter; `source_ids` come from the manifests (ADR-0008).
    """
    now = now or iso_now()
    conn.execute("DELETE FROM nodes")
    source_statuses = source_statuses or {}
    for sid in source_ids:
        status = source_statuses.get(sid, "active")
        if status not in NODE_STATUSES:
            raise ValueError(f"unknown source node status {status!r}; allowed: {sorted(NODE_STATUSES)}")
        conn.execute(
            "INSERT OR REPLACE INTO nodes (node_id, node_type, item_type, slug, status, indexed_at) "
            "VALUES (?, 'source', NULL, ?, ?, ?)",
            (sid, sid, status, now),
        )
    for node in page_nodes:
        node_type = node.get("node_type")
        if node_type not in NODE_TYPES:
            raise ValueError(f"unknown node_type {node_type!r}; allowed: {sorted(NODE_TYPES)}")
        status = node.get("status")
        if status is not None and status not in NODE_STATUSES:
            raise ValueError(f"unknown node status {status!r}; allowed: {sorted(NODE_STATUSES)}")
        item_type = node.get("item_type")
        if node_type == "item":
            if not taxonomy.is_item_type(item_type):
                raise ValueError(f"item node requires a valid item_type; got {item_type!r}")
        elif item_type is not None:
            raise ValueError(f"item_type is only valid on item nodes, not {node_type!r}")
        conn.execute(
            "INSERT OR REPLACE INTO nodes (node_id, node_type, item_type, slug, status, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (node["node_id"], node_type, item_type, node.get("slug"), node.get("status"), now),
        )
    conn.commit()
    return conn.execute("SELECT COUNT(*) AS n FROM nodes").fetchone()["n"]


def node_ids(conn: sqlite3.Connection) -> set[str]:
    return {r["node_id"] for r in conn.execute("SELECT node_id FROM nodes")}
