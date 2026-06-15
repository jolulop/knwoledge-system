from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import validate_graph  # noqa: E402

from app.backend import graph

SRC = "src_0123456789abcdef"
SRC2 = "src_fedcba9876543210"
CPT = "cpt_0123456789abcdef"


def _db(tmp_path):
    db_path = tmp_path / "db" / "graph.sqlite"
    graph.init_db(db_path)
    conn = graph.connect(db_path)
    graph.reindex_nodes(
        conn,
        source_ids=[SRC, SRC2],
        page_nodes=[{"node_id": CPT, "node_type": "concept", "slug": "post-merger", "status": "candidate"}],
        now="t0",
    )
    return db_path, conn


# --- assertions -------------------------------------------------------------


def test_upsert_is_idempotent(tmp_path):
    _, conn = _db(tmp_path)
    a = graph.upsert_assertion(conn, src_id=SRC, dst_id=CPT, edge_type="mentions",
                               asserted_by="llm", status="proposed", now="t1")
    b = graph.upsert_assertion(conn, src_id=SRC, dst_id=CPT, edge_type="mentions",
                               asserted_by="llm", status="active", now="t2")
    assert a == b  # same assertion identity -> same row
    rows = conn.execute("SELECT status FROM edges").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "active"  # updated in place


def test_distinct_spans_and_asserters_coexist(tmp_path):
    _, conn = _db(tmp_path)
    e1 = graph.upsert_assertion(conn, src_id=SRC, dst_id=CPT, edge_type="mentions",
                                asserted_by="llm", evidence_source_id=SRC,
                                evidence_char_start=0, evidence_char_end=10)
    e2 = graph.upsert_assertion(conn, src_id=SRC, dst_id=CPT, edge_type="mentions",
                                asserted_by="llm", evidence_source_id=SRC,
                                evidence_char_start=50, evidence_char_end=60)
    e3 = graph.upsert_assertion(conn, src_id=SRC, dst_id=CPT, edge_type="mentions",
                                asserted_by="human")  # different asserter, no evidence
    assert len({e1, e2, e3}) == 3  # three distinct assertions of the same relationship
    assert conn.execute("SELECT COUNT(*) AS n FROM edges").fetchone()["n"] == 3


def test_only_active_assertions_project(tmp_path):
    _, conn = _db(tmp_path)
    proposed = graph.upsert_assertion(conn, src_id=SRC, dst_id=CPT, edge_type="mentions",
                                      asserted_by="llm", status="proposed")
    assert graph.outgoing_active(conn, SRC) == []  # proposed not projected
    assert graph.incoming_active(conn, CPT) == []

    graph.set_status(conn, proposed, "active")
    assert [e["dst_id"] for e in graph.outgoing_active(conn, SRC)] == [CPT]
    assert [e["src_id"] for e in graph.incoming_active(conn, CPT)] == [SRC]

    graph.set_status(conn, proposed, "rejected")
    assert graph.outgoing_active(conn, SRC) == []  # rejected not projected


def test_deferred_review_leaves_assertion_proposed_and_invisible(tmp_path):
    _, conn = _db(tmp_path)
    # A deferred review item makes no status change: the assertion stays proposed, so it is
    # neither projected nor deleted (ADR-0030).
    edge_id = graph.upsert_assertion(conn, src_id=SRC, dst_id=CPT, edge_type="mentions",
                                     asserted_by="authored_wikilink", status="proposed",
                                     review_id="rev_x")
    assert graph.incoming_active(conn, CPT) == []
    row = conn.execute("SELECT status FROM edges WHERE edge_id = ?", (edge_id,)).fetchone()
    assert row["status"] == "proposed"  # still present, still invisible


def test_count_independent_sources(tmp_path):
    _, conn = _db(tmp_path)
    graph.upsert_assertion(conn, src_id=SRC, dst_id=CPT, edge_type="mentions",
                           asserted_by="llm", status="active")
    assert graph.count_independent_sources(conn, CPT) == 1
    graph.upsert_assertion(conn, src_id=SRC2, dst_id=CPT, edge_type="mentions",
                           asserted_by="llm", status="active")
    assert graph.count_independent_sources(conn, CPT) == 2
    # A second active assertion from the SAME source does not add to the count.
    graph.upsert_assertion(conn, src_id=SRC, dst_id=CPT, edge_type="mentions",
                           asserted_by="human", status="active")
    assert graph.count_independent_sources(conn, CPT) == 2


def test_vocabulary_guards(tmp_path):
    _, conn = _db(tmp_path)
    with pytest.raises(ValueError):
        graph.upsert_assertion(conn, src_id=SRC, dst_id=CPT, edge_type="needs_review", asserted_by="llm")
    with pytest.raises(ValueError):
        graph.upsert_assertion(conn, src_id=SRC, dst_id=CPT, edge_type="mentions", asserted_by="llm", status="bogus")
    with pytest.raises(ValueError):
        graph.upsert_assertion(conn, src_id=SRC, dst_id=CPT, edge_type="mentions", asserted_by="alien")


def test_reindex_is_deterministic_and_edge_safe(tmp_path):
    _, conn = _db(tmp_path)
    graph.upsert_assertion(conn, src_id=SRC, dst_id=CPT, edge_type="mentions",
                           asserted_by="llm", status="active", now="e1")
    edges_before = conn.execute("SELECT * FROM edges ORDER BY edge_id").fetchall()
    nodes_before = conn.execute("SELECT node_id, node_type, slug, status FROM nodes ORDER BY node_id").fetchall()

    graph.reindex_nodes(
        conn, source_ids=[SRC, SRC2],
        page_nodes=[{"node_id": CPT, "node_type": "concept", "slug": "post-merger", "status": "candidate"}],
        now="t9",
    )
    nodes_after = conn.execute("SELECT node_id, node_type, slug, status FROM nodes ORDER BY node_id").fetchall()
    edges_after = conn.execute("SELECT * FROM edges ORDER BY edge_id").fetchall()

    assert [dict(r) for r in nodes_before] == [dict(r) for r in nodes_after]  # node identity stable
    assert [dict(r) for r in edges_before] == [dict(r) for r in edges_after]  # edges untouched


# --- validate_graph ---------------------------------------------------------


def test_validate_passes_with_no_graph(tmp_path):
    assert validate_graph.main([str(tmp_path)]) == 0  # no db yet


def test_validate_passes_on_valid_graph(tmp_path):
    _, conn = _db(tmp_path)
    graph.upsert_assertion(conn, src_id=SRC, dst_id=CPT, edge_type="mentions",
                           asserted_by="llm", status="active")
    conn.close()
    assert validate_graph.main([str(tmp_path)]) == 0


def test_validate_rejects_needs_review_edge(tmp_path):
    _, conn = _db(tmp_path)
    # Insert a raw needs_review edge (the upsert API would refuse it).
    conn.execute(
        "INSERT INTO edges (edge_id, src_id, dst_id, edge_type, status, asserted_by, created_at) "
        "VALUES ('edg_x', ?, ?, 'needs_review', 'active', 'llm', 't')",
        (SRC, CPT),
    )
    conn.commit()
    conn.close()
    assert validate_graph.main([str(tmp_path)]) == 1


def test_validate_rejects_dangling_edge(tmp_path):
    _, conn = _db(tmp_path)
    graph.upsert_assertion(conn, src_id=SRC, dst_id="cpt_notindexed00000", edge_type="mentions",
                           asserted_by="llm", status="active")
    conn.close()
    assert validate_graph.main([str(tmp_path)]) == 1
