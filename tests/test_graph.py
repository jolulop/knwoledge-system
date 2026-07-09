from __future__ import annotations

import sqlite3
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

from app.backend import graph, taxonomy

SRC = "src_0123456789abcdef"
SRC2 = "src_fedcba9876543210"
ITM = "itm_0123456789abcdef"


def _db(tmp_path):
    db_path = tmp_path / "db" / "graph.sqlite"
    graph.init_db(db_path)
    conn = graph.connect(db_path)
    graph.reindex_nodes(
        conn,
        source_ids=[SRC, SRC2],
        page_nodes=[{"node_id": ITM, "node_type": "item", "item_type": "method_technique",
                     "slug": "post-merger", "status": "candidate"}],
        now="t0",
    )
    return db_path, conn


# --- vocabulary (ADR-0059) ---------------------------------------------------


def test_node_type_vocabulary_is_the_item_family():
    # The concept/entity/person/organization/project family collapsed into `item`.
    assert graph.NODE_TYPES == {"source", "item", "claim", "tag", "query", "synthesis"}


def test_schema_version_is_2_for_item_type_column():
    assert graph.SCHEMA_VERSION == 2


def test_rekeyed_status_is_retired_merged_stays():
    # ADR-0059: retype is a metadata flip on the type-neutral id — the ADR-0051 `rekeyed`
    # tombstone can no longer arise; the ADR-0050 `merged` tombstone stays.
    assert "rekeyed" not in graph.NODE_STATUSES
    assert "merged" in graph.NODE_STATUSES


def test_derived_from_src_endpoint_set_includes_item():
    assert graph.EDGE_ENDPOINTS["derived_from"][0] == {"claim", "synthesis", "item"}


def test_find_node_by_candidate_ids_removed():
    # Single-id probe now (one type-neutral id per name) — the multi-candidate helper is gone.
    assert not hasattr(graph, "find_node_by_candidate_ids")


# --- assertions -------------------------------------------------------------


def test_upsert_is_idempotent(tmp_path):
    _, conn = _db(tmp_path)
    a = graph.upsert_assertion(conn, src_id=SRC, dst_id=ITM, edge_type="mentions",
                               asserted_by="llm", status="proposed", now="t1")
    b = graph.upsert_assertion(conn, src_id=SRC, dst_id=ITM, edge_type="mentions",
                               asserted_by="llm", status="active", now="t2")
    assert a == b  # same assertion identity -> same row
    rows = conn.execute("SELECT status FROM edges").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "active"  # updated in place


def test_distinct_spans_and_asserters_coexist(tmp_path):
    _, conn = _db(tmp_path)
    e1 = graph.upsert_assertion(conn, src_id=SRC, dst_id=ITM, edge_type="mentions",
                                asserted_by="llm", evidence_source_id=SRC,
                                evidence_char_start=0, evidence_char_end=10)
    e2 = graph.upsert_assertion(conn, src_id=SRC, dst_id=ITM, edge_type="mentions",
                                asserted_by="llm", evidence_source_id=SRC,
                                evidence_char_start=50, evidence_char_end=60)
    e3 = graph.upsert_assertion(conn, src_id=SRC, dst_id=ITM, edge_type="mentions",
                                asserted_by="human")  # different asserter, no evidence
    assert len({e1, e2, e3}) == 3  # three distinct assertions of the same relationship
    assert conn.execute("SELECT COUNT(*) AS n FROM edges").fetchone()["n"] == 3


def test_only_active_assertions_project(tmp_path):
    _, conn = _db(tmp_path)
    proposed = graph.upsert_assertion(conn, src_id=SRC, dst_id=ITM, edge_type="mentions",
                                      asserted_by="llm", status="proposed")
    assert graph.outgoing_active(conn, SRC) == []  # proposed not projected
    assert graph.incoming_active(conn, ITM) == []

    graph.set_status(conn, proposed, "active")
    assert [e["dst_id"] for e in graph.outgoing_active(conn, SRC)] == [ITM]
    assert [e["src_id"] for e in graph.incoming_active(conn, ITM)] == [SRC]

    graph.set_status(conn, proposed, "rejected")
    assert graph.outgoing_active(conn, SRC) == []  # rejected not projected


def test_deferred_review_leaves_assertion_proposed_and_invisible(tmp_path):
    _, conn = _db(tmp_path)
    # A deferred review item makes no status change: the assertion stays proposed, so it is
    # neither projected nor deleted (ADR-0030).
    edge_id = graph.upsert_assertion(conn, src_id=SRC, dst_id=ITM, edge_type="mentions",
                                     asserted_by="authored_wikilink", status="proposed",
                                     review_id="rev_x")
    assert graph.incoming_active(conn, ITM) == []
    row = conn.execute("SELECT status FROM edges WHERE edge_id = ?", (edge_id,)).fetchone()
    assert row["status"] == "proposed"  # still present, still invisible


def test_count_independent_sources(tmp_path):
    _, conn = _db(tmp_path)
    graph.upsert_assertion(conn, src_id=SRC, dst_id=ITM, edge_type="mentions",
                           asserted_by="llm", status="active")
    assert graph.count_independent_sources(conn, ITM) == 1
    graph.upsert_assertion(conn, src_id=SRC2, dst_id=ITM, edge_type="mentions",
                           asserted_by="llm", status="active")
    assert graph.count_independent_sources(conn, ITM) == 2
    # A second active assertion from the SAME source does not add to the count.
    graph.upsert_assertion(conn, src_id=SRC, dst_id=ITM, edge_type="mentions",
                           asserted_by="human", status="active")
    assert graph.count_independent_sources(conn, ITM) == 2


def test_claims_for_source_returns_only_active(tmp_path):
    _, conn = _db(tmp_path)
    clm = "clm_0123456789abcdef"
    clm2 = "clm_fedcba9876543210"
    graph.upsert_node(conn, node_id=clm, node_type="claim", status="active")
    graph.upsert_node(conn, node_id=clm2, node_type="claim", status="active")
    graph.upsert_assertion(conn, src_id=clm, dst_id=SRC, edge_type="derived_from",
                           asserted_by="llm", status="active")
    proposed = graph.upsert_assertion(conn, src_id=clm2, dst_id=SRC, edge_type="derived_from",
                                      asserted_by="llm", status="proposed")
    assert graph.claims_for_source(conn, SRC) == [clm]  # only the active claim
    graph.set_status(conn, proposed, "active")
    assert graph.claims_for_source(conn, SRC) == sorted([clm, clm2])


def test_item_ids_for_source_returns_active_item_mentions(tmp_path):
    _, conn = _db(tmp_path)
    itm2 = "itm_fedcba9876543210"
    clm = "clm_0123456789abcdef"
    graph.upsert_node(conn, node_id=itm2, node_type="item", slug="other",
                      status="candidate", item_type="model")
    graph.upsert_node(conn, node_id=clm, node_type="claim", status="active")
    graph.upsert_assertion(conn, src_id=SRC, dst_id=ITM, edge_type="mentions",
                           asserted_by="llm", status="active")
    superseded = graph.upsert_assertion(conn, src_id=SRC, dst_id=itm2, edge_type="mentions",
                                        asserted_by="llm", status="active")
    # A mentioned non-item never counts toward the item neighborhood.
    graph.upsert_assertion(conn, src_id=SRC, dst_id=clm, edge_type="mentions",
                           asserted_by="llm", status="active")
    assert graph.item_ids_for_source(conn, SRC) == {ITM, itm2}
    graph.set_status(conn, superseded, "superseded")
    assert graph.item_ids_for_source(conn, SRC) == {ITM}


def test_vocabulary_guards(tmp_path):
    _, conn = _db(tmp_path)
    with pytest.raises(ValueError):
        graph.upsert_assertion(conn, src_id=SRC, dst_id=ITM, edge_type="needs_review", asserted_by="llm")
    with pytest.raises(ValueError):
        graph.upsert_assertion(conn, src_id=SRC, dst_id=ITM, edge_type="mentions", asserted_by="llm", status="bogus")
    with pytest.raises(ValueError):
        graph.upsert_assertion(conn, src_id=SRC, dst_id=ITM, edge_type="mentions", asserted_by="alien")


# --- item_type column (ADR-0059) ---------------------------------------------


def test_upsert_new_item_requires_valid_item_type(tmp_path):
    _, conn = _db(tmp_path)
    new = "itm_00000000000000aa"
    with pytest.raises(ValueError, match="item_type"):
        graph.upsert_node(conn, node_id=new, node_type="item", slug="x", status="candidate")
    with pytest.raises(ValueError, match="item_type"):
        graph.upsert_node(conn, node_id=new, node_type="item", slug="x", status="candidate",
                          item_type="organization")  # pre-0059 vocabulary is invalid
    # Every taxonomy value (production + sentinel) is accepted on a candidate.
    graph.upsert_node(conn, node_id=new, node_type="item", slug="x", status="candidate",
                      item_type=taxonomy.UNCLASSIFIED)
    assert graph.get_node(conn, new)["item_type"] == taxonomy.UNCLASSIFIED


def test_upsert_none_item_type_preserves_stored_value(tmp_path):
    # A status-only mirror update (item_type=None) must not null the classification.
    _, conn = _db(tmp_path)
    graph.upsert_node(conn, node_id=ITM, node_type="item", slug="post-merger", status="active")
    node = graph.get_node(conn, ITM)
    assert node["status"] == "active" and node["item_type"] == "method_technique"


def test_upsert_item_type_on_non_item_raises(tmp_path):
    _, conn = _db(tmp_path)
    with pytest.raises(ValueError, match="only valid on item nodes"):
        graph.upsert_node(conn, node_id="clm_0123456789abcdef", node_type="claim",
                          status="active", item_type="model")


def test_get_node_and_mentions_for_source_carry_item_type(tmp_path):
    _, conn = _db(tmp_path)
    assert graph.get_node(conn, ITM)["item_type"] == "method_technique"
    assert graph.get_node(conn, SRC)["item_type"] is None  # non-items carry none
    graph.upsert_assertion(conn, src_id=SRC, dst_id=ITM, edge_type="mentions",
                           asserted_by="llm", status="active")
    rows = graph.mentions_for_source(conn, SRC)
    assert [(r["dst_id"], r["item_type"]) for r in rows] == [(ITM, "method_technique")]


def test_reindex_item_page_nodes_require_valid_item_type(tmp_path):
    db_path = tmp_path / "db" / "graph.sqlite"
    graph.init_db(db_path)
    conn = graph.connect(db_path)
    with pytest.raises(ValueError, match="item_type"):
        graph.reindex_nodes(conn, source_ids=[], page_nodes=[
            {"node_id": ITM, "node_type": "item", "slug": "x", "status": "candidate"}])
    with pytest.raises(ValueError, match="item_type"):
        graph.reindex_nodes(conn, source_ids=[], page_nodes=[
            {"node_id": ITM, "node_type": "item", "item_type": "concept",
             "slug": "x", "status": "candidate"}])
    with pytest.raises(ValueError, match="only valid on item nodes"):
        graph.reindex_nodes(conn, source_ids=[], page_nodes=[
            {"node_id": "clm_0123456789abcdef", "node_type": "claim",
             "item_type": "model", "status": "active"}])
    graph.reindex_nodes(conn, source_ids=[], page_nodes=[
        {"node_id": ITM, "node_type": "item", "item_type": "domain",
         "slug": "x", "status": "candidate"}])
    assert graph.get_node(conn, ITM)["item_type"] == "domain"


# --- ADR-0009 path-containment at the graph boundary (identity-surgery hardening) ------------------


def test_is_safe_slug_predicate():
    # a legitimate _slug() value is [a-z0-9-]+; a bare-.. filename with no separator is still one component.
    for good in ("post-merger", "acme", "a", "acme-corp-2", "..foo"):
        assert graph.is_safe_slug(good)
    for bad in ("", ".", "..", "a/b", "..\\b", "/etc/passwd", "a\\b", None, 123):
        assert not graph.is_safe_slug(bad)


def test_upsert_node_rejects_unsafe_slug(tmp_path):
    _, conn = _db(tmp_path)
    graph.upsert_node(conn, node_id=ITM, node_type="item", slug="safe-slug", status="active")   # accepted
    graph.upsert_node(conn, node_id=ITM, node_type="item", slug=None, status="active")          # None ok
    for bad in ("../escape", "a/b", "..", ".", "sub\\dir", ""):
        with pytest.raises(ValueError):
            graph.upsert_node(conn, node_id=ITM, node_type="item", slug=bad, status="active")


def test_validate_graph_rejects_tampered_unsafe_slug(tmp_path):
    db_path, conn = _db(tmp_path)
    conn.commit()
    conn.close()
    assert validate_graph.main([str(tmp_path)]) == 0                    # clean graph passes
    conn = graph.connect(db_path)
    conn.execute("UPDATE nodes SET slug=? WHERE node_id=?", ("../evil", ITM))  # raw SQL bypasses upsert guard
    conn.commit()
    conn.close()
    assert validate_graph.main([str(tmp_path)]) != 0                    # the backstop catches it


def test_reindex_is_deterministic_and_edge_safe(tmp_path):
    _, conn = _db(tmp_path)
    graph.upsert_assertion(conn, src_id=SRC, dst_id=ITM, edge_type="mentions",
                           asserted_by="llm", status="active", now="e1")
    edges_before = conn.execute("SELECT * FROM edges ORDER BY edge_id").fetchall()
    nodes_before = conn.execute(
        "SELECT node_id, node_type, item_type, slug, status FROM nodes ORDER BY node_id").fetchall()

    graph.reindex_nodes(
        conn, source_ids=[SRC, SRC2],
        page_nodes=[{"node_id": ITM, "node_type": "item", "item_type": "method_technique",
                     "slug": "post-merger", "status": "candidate"}],
        now="t9",
    )
    nodes_after = conn.execute(
        "SELECT node_id, node_type, item_type, slug, status FROM nodes ORDER BY node_id").fetchall()
    edges_after = conn.execute("SELECT * FROM edges ORDER BY edge_id").fetchall()

    assert [dict(r) for r in nodes_before] == [dict(r) for r in nodes_after]  # node identity stable
    assert [dict(r) for r in edges_before] == [dict(r) for r in edges_after]  # edges untouched


# --- validate_graph ---------------------------------------------------------


def test_validate_passes_with_no_graph(tmp_path):
    assert validate_graph.main([str(tmp_path)]) == 0  # no db yet


def test_validate_passes_on_valid_graph(tmp_path):
    _, conn = _db(tmp_path)
    graph.upsert_assertion(conn, src_id=SRC, dst_id=ITM, edge_type="mentions",
                           asserted_by="llm", status="active")
    conn.close()
    assert validate_graph.main([str(tmp_path)]) == 0


def test_validate_rejects_needs_review_edge(tmp_path):
    _, conn = _db(tmp_path)
    # Insert a raw needs_review edge (the upsert API would refuse it).
    conn.execute(
        "INSERT INTO edges (edge_id, src_id, dst_id, edge_type, status, asserted_by, created_at) "
        "VALUES ('edg_x', ?, ?, 'needs_review', 'active', 'llm', 't')",
        (SRC, ITM),
    )
    conn.commit()
    conn.close()
    assert validate_graph.main([str(tmp_path)]) == 1


def test_validate_rejects_dangling_edge(tmp_path):
    _, conn = _db(tmp_path)
    # The write API refuses this; insert raw to prove validate_graph is the backstop.
    conn.execute(
        "INSERT INTO edges (edge_id, src_id, dst_id, edge_type, status, asserted_by, created_at) "
        "VALUES ('edg_dangle', ?, 'itm_notindexed00000', 'mentions', 'active', 'llm', 't')",
        (SRC,),
    )
    conn.commit()
    conn.close()
    assert validate_graph.main([str(tmp_path)]) == 1


def test_validate_rejects_tampered_item_type(tmp_path):
    # The upsert/reindex APIs refuse these; raw SQL proves validate_graph is the backstop.
    db_path, conn = _db(tmp_path)
    conn.execute("UPDATE nodes SET item_type = NULL WHERE node_id = ?", (ITM,))
    conn.commit()
    conn.close()
    assert validate_graph.main([str(tmp_path)]) == 1                    # item without item_type

    _, conn = _db(tmp_path)
    conn.execute("UPDATE nodes SET item_type = 'model' WHERE node_id = ?", (SRC,))
    conn.commit()
    conn.close()
    assert validate_graph.main([str(tmp_path)]) == 1                    # item_type on a non-item


def test_validate_rejects_active_item_with_sentinel(tmp_path):
    # ADR-0059 decision 5: the sentinel is candidate-only — an ACTIVE item carrying it is an error.
    db_path, conn = _db(tmp_path)
    conn.execute("UPDATE nodes SET item_type = ?, status = 'candidate' WHERE node_id = ?",
                 (taxonomy.UNCLASSIFIED, ITM))
    conn.commit()
    conn.close()
    assert validate_graph.main([str(tmp_path)]) == 0                    # candidate + sentinel is legal
    conn = graph.connect(db_path)
    conn.execute("UPDATE nodes SET status = 'active' WHERE node_id = ?", (ITM,))
    conn.commit()
    conn.close()
    assert validate_graph.main([str(tmp_path)]) == 1                    # active + sentinel is not


def test_upsert_rejects_unknown_node(tmp_path):
    _, conn = _db(tmp_path)
    with pytest.raises(ValueError, match="not an indexed node"):
        graph.upsert_assertion(conn, src_id=SRC, dst_id="itm_missing00000000",
                               edge_type="mentions", asserted_by="llm")
    with pytest.raises(ValueError, match="not an indexed node"):
        graph.upsert_assertion(conn, src_id="src_missing00000000", dst_id=ITM,
                               edge_type="mentions", asserted_by="llm")


def test_raw_duplicate_assertion_rejected_by_unique_index(tmp_path):
    _, conn = _db(tmp_path)
    graph.upsert_assertion(conn, src_id=SRC, dst_id=ITM, edge_type="mentions", asserted_by="llm")
    # A second raw insert with the same assertion identity (null evidence) must be rejected
    # by the null-safe unique index, not silently duplicated.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO edges (edge_id, src_id, dst_id, edge_type, status, asserted_by, created_at) "
            "VALUES ('edg_dup', ?, ?, 'mentions', 'proposed', 'llm', 't')",
            (SRC, ITM),
        )


def test_set_status_raises_for_unknown_edge(tmp_path):
    _, conn = _db(tmp_path)
    with pytest.raises(ValueError, match="no edge"):
        graph.set_status(conn, "edg_does_not_exist", "active")


def test_node_status_vocabulary_is_validated(tmp_path):
    db_path = tmp_path / "db" / "graph.sqlite"
    graph.init_db(db_path)
    conn = graph.connect(db_path)
    with pytest.raises(ValueError, match="node status"):
        graph.reindex_nodes(conn, source_ids=[],
                            page_nodes=[{"node_id": ITM, "node_type": "item",
                                         "item_type": "model", "status": "bogus"}])
    with pytest.raises(ValueError, match="node status"):
        graph.upsert_node(conn, node_id=ITM, node_type="item", item_type="model", status="bogus")


def test_endpoint_type_matrix_enforced(tmp_path):
    _, conn = _db(tmp_path)
    clm = "clm_0123456789abcdef"
    graph.upsert_node(conn, node_id=clm, node_type="claim", status="active")
    # Valid: a claim derived_from a source.
    graph.upsert_assertion(conn, src_id=clm, dst_id=SRC, edge_type="derived_from",
                           asserted_by="llm", status="active")
    conn.close()
    assert validate_graph.main([str(tmp_path)]) == 0

    # Invalid: a source as the src of derived_from (must be claim/synthesis/item).
    _, conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO edges (edge_id, src_id, dst_id, edge_type, status, asserted_by, created_at) "
        "VALUES ('edg_bad', ?, ?, 'derived_from', 'active', 'llm', 't')",
        (SRC, SRC2),
    )
    conn.commit()
    conn.close()
    assert validate_graph.main([str(tmp_path)]) == 1


def test_schema_version_recorded(tmp_path):
    db_path = tmp_path / "db" / "graph.sqlite"
    graph.init_db(db_path)
    conn = graph.connect(db_path)
    assert graph.schema_version(conn) == graph.SCHEMA_VERSION


# --- ADR-0059 review round B1: pre-v2 databases hard-fail, never half-upgrade ---


def _make_v1_db(tmp_path):
    """A pre-ADR-0059 graph DB: old `nodes` shape (no item_type), stamped v1."""
    db_path = tmp_path / "db" / "graph.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE nodes (node_id TEXT PRIMARY KEY, node_type TEXT NOT NULL, "
                 "slug TEXT, status TEXT, indexed_at TEXT)")
    conn.execute("INSERT INTO nodes (node_id, node_type, slug, status) "
                 "VALUES ('cpt_0123456789abcdef', 'concept', 'thing', 'active')")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    return db_path


def test_init_db_refuses_v1_database_without_migrating(tmp_path):
    db_path = _make_v1_db(tmp_path)
    with pytest.raises(graph.GraphSchemaError):
        graph.init_db(db_path)
    # NOTHING was touched: still stamped v1, still structurally v1 (no lying half-upgrade).
    conn = sqlite3.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    assert "item_type" not in cols
    conn.close()


def test_validators_refuse_v1_database_with_typed_mismatch(tmp_path, capsys):
    _make_v1_db(tmp_path)
    assert validate_graph.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "graph schema version mismatch" in out and "pre-ADR-0059" in out


def test_validators_refuse_lying_v2_stamp_without_column(tmp_path, capsys):
    # A v2 stamp on a structurally-v1 table (the exact half-state init_db used to create):
    # the STRUCTURAL check refuses cleanly — never an OperationalError crash.
    db_path = _make_v1_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version = {graph.SCHEMA_VERSION}")
    conn.commit()
    conn.close()
    with pytest.raises(graph.GraphSchemaError):
        graph.init_db(db_path)
    assert validate_graph.main([str(tmp_path)]) == 1
    assert "graph schema version mismatch" in capsys.readouterr().out
