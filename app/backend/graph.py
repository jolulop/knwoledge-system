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

from app.backend.manifests import iso_now

# Build Spec §6.1 node types.
NODE_TYPES = frozenset(
    {"source", "entity", "concept", "claim", "project", "person",
     "organization", "tag", "query", "synthesis"}
)
# Build Spec §6.2 minus needs_review (ADR-0030: review is a status, not an edge type).
EDGE_TYPES = frozenset(
    {"mentions", "supports", "contradicts", "supersedes", "duplicates",
     "derived_from", "related_to"}
)
EDGE_STATUSES = frozenset({"proposed", "active", "rejected", "superseded"})
ASSERTED_BY = frozenset({"deterministic", "llm", "human", "authored_wikilink"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id    TEXT PRIMARY KEY,
    node_type  TEXT NOT NULL,
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
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


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
    place rather than duplicating. NULL evidence fields are matched null-safely (`IS`), which
    a table-level UNIQUE would not do, so idempotency is enforced here, not by the schema.
    """
    if edge_type not in EDGE_TYPES:
        raise ValueError(f"unknown edge_type {edge_type!r}; allowed: {sorted(EDGE_TYPES)}")
    if status not in EDGE_STATUSES:
        raise ValueError(f"unknown edge status {status!r}; allowed: {sorted(EDGE_STATUSES)}")
    if asserted_by not in ASSERTED_BY:
        raise ValueError(f"unknown asserted_by {asserted_by!r}; allowed: {sorted(ASSERTED_BY)}")
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
    conn.execute(
        "UPDATE edges SET status = ?, updated_at = ? WHERE edge_id = ?",
        (status, now or iso_now(), edge_id),
    )
    conn.commit()


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


def count_independent_sources(conn: sqlite3.Connection, dst_id: str, *, edge_type: str = "mentions") -> int:
    """Distinct source_ids among a node's active assertions of the given type (promotion).

    Exact-duplicate sources already share one source_id (ADR-0007), so they count once here;
    same-family independence (ADR-0018) is a slice-5 concern layered on top of this count.
    """
    row = conn.execute(
        "SELECT COUNT(DISTINCT src_id) AS n FROM edges "
        "WHERE dst_id = ? AND edge_type = ? AND status = 'active'",
        (dst_id, edge_type),
    ).fetchone()
    return int(row["n"])


# --- nodes (derived index) --------------------------------------------------


def reindex_nodes(
    conn: sqlite3.Connection,
    *,
    source_ids: list[str],
    page_nodes: list[dict[str, Any]],
    now: str | None = None,
) -> int:
    """Rebuild the derived `nodes` index from manifests (sources) + page frontmatter.

    Deterministic and edge-safe: it replaces only the `nodes` table and never touches
    `edges`. `page_nodes` are dicts of {node_id, node_type, slug, status} taken from the
    pages' frontmatter; `source_ids` come from the manifests (ADR-0008).
    """
    now = now or iso_now()
    conn.execute("DELETE FROM nodes")
    for sid in source_ids:
        conn.execute(
            "INSERT OR REPLACE INTO nodes (node_id, node_type, slug, status, indexed_at) "
            "VALUES (?, 'source', ?, 'active', ?)",
            (sid, sid, now),
        )
    for node in page_nodes:
        node_type = node.get("node_type")
        if node_type not in NODE_TYPES:
            raise ValueError(f"unknown node_type {node_type!r}; allowed: {sorted(NODE_TYPES)}")
        conn.execute(
            "INSERT OR REPLACE INTO nodes (node_id, node_type, slug, status, indexed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (node["node_id"], node_type, node.get("slug"), node.get("status"), now),
        )
    conn.commit()
    return conn.execute("SELECT COUNT(*) AS n FROM nodes").fetchone()["n"]


def node_ids(conn: sqlite3.Connection) -> set[str]:
    return {r["node_id"] for r in conn.execute("SELECT node_id FROM nodes")}
