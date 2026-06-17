from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph, graph_read

SRC_A = "src_aaaaaaaaaaaaaaaa"
SRC_B = "src_bbbbbbbbbbbbbbbb"
CPT_X = "cpt_xxxxxxxxxxxxxxxx"  # active
CPT_Y = "cpt_yyyyyyyyyyyyyyyy"  # candidate
CLM_1 = "clm_1111111111111111"  # active
CLM_2 = "clm_2222222222222222"  # active


@pytest.fixture
def conn(tmp_path):
    """A small graph: two sources, two concepts (one candidate), two claims.

        src_a --mentions--> cpt_x <--mentions-- src_b
        src_a --mentions--> cpt_y           cpt_x --related_to--> cpt_y
        clm_1 --derived_from--> src_a       clm_2 --derived_from--> src_b
        clm_1 --contradicts--> clm_2 (symmetric, stored sorted)
        (plus one PROPOSED mention src_a->cpt_x, hidden by default)
    """
    db_path = tmp_path / "db" / "graph.sqlite"
    graph.init_db(db_path)
    c = graph.connect(db_path)
    graph.reindex_nodes(
        c,
        source_ids=[SRC_A, SRC_B],
        page_nodes=[
            {"node_id": CPT_X, "node_type": "concept", "slug": "x", "status": "active"},
            {"node_id": CPT_Y, "node_type": "concept", "slug": "y", "status": "candidate"},
            {"node_id": CLM_1, "node_type": "claim", "slug": None, "status": "active"},
            {"node_id": CLM_2, "node_type": "claim", "slug": None, "status": "active"},
        ],
        now="t0",
    )
    A = dict(asserted_by="llm", status="active")
    graph.upsert_assertion(c, src_id=SRC_A, dst_id=CPT_X, edge_type="mentions", **A)
    graph.upsert_assertion(c, src_id=SRC_B, dst_id=CPT_X, edge_type="mentions", **A)
    graph.upsert_assertion(c, src_id=SRC_A, dst_id=CPT_Y, edge_type="mentions", **A)
    graph.upsert_assertion(c, src_id=CLM_1, dst_id=SRC_A, edge_type="derived_from", **A)
    graph.upsert_assertion(c, src_id=CLM_2, dst_id=SRC_B, edge_type="derived_from", **A)
    graph.upsert_assertion(c, src_id=CLM_1, dst_id=CLM_2, edge_type="contradicts", **A)  # sorted
    graph.upsert_assertion(c, src_id=CPT_X, dst_id=CPT_Y, edge_type="related_to", **A)  # sorted
    # A proposed mention from a different asserter: hidden unless include_status asks for it.
    graph.upsert_assertion(c, src_id=SRC_A, dst_id=CPT_X, edge_type="mentions",
                           asserted_by="human", status="proposed")
    return c


# --------------------------------------------------------------------------- node_view


def test_node_view_groups_incoming_outgoing(conn):
    view = graph_read.node_view(conn, CPT_X)
    assert view["node"]["answer_eligible"] is True  # active concept
    # cpt_x is dst of two active mentions, src of one related_to.
    assert view["counts"] == {"outgoing": 1, "incoming": 2}
    assert set(view["incoming"]["mentions"][i]["other_node_id"] for i in range(2)) == {SRC_A, SRC_B}
    related = view["outgoing"]["related_to"][0]
    assert related["other_node_id"] == CPT_Y
    assert related["symmetric"] is True
    assert related["other"]["answer_eligible"] is False  # candidate concept


def test_node_view_evidence_is_advisory(conn):
    view = graph_read.node_view(conn, CPT_X)
    assertion = view["incoming"]["mentions"][0]
    assert assertion["evidence"]["advisory"] is True


def test_node_view_hides_proposed_by_default(conn):
    default = graph_read.node_view(conn, CPT_X)
    assert default["counts"]["incoming"] == 2  # proposed mention hidden
    widened = graph_read.node_view(conn, CPT_X, include_status=("active", "proposed"))
    assert widened["counts"]["incoming"] == 3  # proposed now surfaced


def test_node_view_symmetric_edge_direction_preserved(conn):
    # contradicts is stored sorted clm_1 < clm_2; clm_1 sees it outgoing, clm_2 incoming,
    # but both expose other_node_id + symmetric and never lose src/dst.
    v1 = graph_read.node_view(conn, CLM_1)
    out = v1["outgoing"]["contradicts"][0]
    assert (out["src_id"], out["dst_id"]) == (CLM_1, CLM_2)
    assert out["other_node_id"] == CLM_2 and out["symmetric"] is True

    v2 = graph_read.node_view(conn, CLM_2)
    inc = v2["incoming"]["contradicts"][0]
    assert inc["other_node_id"] == CLM_1 and inc["symmetric"] is True


def test_node_view_missing_node(conn):
    assert graph_read.node_view(conn, "cpt_does_not_exist") is None


# --------------------------------------------------------------------------- neighborhood


def test_neighborhood_depth1_induced_subgraph(conn):
    nb = graph_read.neighborhood(conn, CPT_X, depth=1)
    ids = {n["node_id"]: n for n in nb["nodes"]}
    assert set(ids) == {CPT_X, CPT_Y, SRC_A, SRC_B}
    assert ids[CPT_X]["distance"] == 0
    assert ids[SRC_A]["distance"] == 1
    # Candidate node reachable via an active edge appears, flagged not answer_eligible.
    assert ids[CPT_Y]["answer_eligible"] is False
    # Induced edges include src_a->cpt_y even though it is "between" two distance-1 nodes.
    edge_pairs = {(e["src_id"], e["dst_id"], e["edge_type"]) for e in nb["edges"]}
    assert (SRC_A, CPT_Y, "mentions") in edge_pairs
    assert (CPT_X, CPT_Y, "related_to") in edge_pairs
    assert nb["truncated"] is False


def test_neighborhood_depth0_is_root_only(conn):
    nb = graph_read.neighborhood(conn, CPT_X, depth=0)
    assert [n["node_id"] for n in nb["nodes"]] == [CPT_X]
    assert nb["edges"] == []


def test_neighborhood_depth_clamped_to_max(conn):
    nb = graph_read.neighborhood(conn, CLM_1, depth=5)
    assert nb["depth"] == graph_read.MAX_DEPTH  # clamped to 2


def test_neighborhood_depth2_reaches_two_hops(conn):
    nb = graph_read.neighborhood(conn, CLM_1, depth=2)
    dist = {n["node_id"]: n["distance"] for n in nb["nodes"]}
    assert dist[CLM_1] == 0
    assert dist[CLM_2] == 1 and dist[SRC_A] == 1
    assert dist[SRC_B] == 2 and dist[CPT_X] == 2 and dist[CPT_Y] == 2


def test_neighborhood_node_types_filter(conn):
    nb = graph_read.neighborhood(conn, CPT_X, depth=1, node_types=frozenset({"source"}))
    assert {n["node_id"] for n in nb["nodes"]} == {CPT_X, SRC_A, SRC_B}  # cpt_y filtered out
    # No edge touches the filtered-out cpt_y.
    assert all(CPT_Y not in (e["src_id"], e["dst_id"]) for e in nb["edges"])


def test_neighborhood_edge_types_filter(conn):
    nb = graph_read.neighborhood(conn, CPT_X, depth=1, edge_types=("mentions",))
    assert {n["node_id"] for n in nb["nodes"]} == {CPT_X, SRC_A, SRC_B}
    assert all(e["edge_type"] == "mentions" for e in nb["edges"])


def test_neighborhood_node_cap_truncates(conn):
    nb = graph_read.neighborhood(conn, CPT_X, depth=1, node_cap=2)
    assert len(nb["nodes"]) == 2  # root + first sorted candidate
    assert nb["truncated"] is True
    assert nb["cap"]["nodes"] == 2


def test_neighborhood_edge_cap_truncates(conn):
    nb = graph_read.neighborhood(conn, CLM_1, depth=2, edge_cap=2)
    assert len(nb["edges"]) == 2
    assert nb["truncated"] is True


def test_neighborhood_edges_are_canonical_only(conn):
    # Flat neighborhood edges carry src/dst + symmetric but NO other_node_id (ambiguous in a flat
    # list); other_node_id is a node_view-only field. (ADR-0032 addendum 1.)
    nb = graph_read.neighborhood(conn, CLM_1, depth=1)
    contradicts = [e for e in nb["edges"] if e["edge_type"] == "contradicts"]
    assert len(contradicts) == 1
    edge = contradicts[0]
    assert edge["symmetric"] is True
    assert (edge["src_id"], edge["dst_id"]) == (CLM_1, CLM_2)
    assert "other_node_id" not in edge
    # node_view, by contrast, DOES expose other_node_id (well-defined vs the queried node).
    assert "other_node_id" in graph_read.node_view(conn, CLM_1)["outgoing"]["contradicts"][0]


def test_neighborhood_node_types_filter_blocks_deeper_discovery(conn):
    # cpt_x/cpt_y/src_b are reachable from clm_1 only *through* src_a (a source). Excluding
    # sources at traversal time therefore stops discovery at the claims (traversal-time semantics).
    nb = graph_read.neighborhood(conn, CLM_1, depth=2, node_types=frozenset({"claim"}))
    assert {n["node_id"] for n in nb["nodes"]} == {CLM_1, CLM_2}


def test_neighborhood_edge_cap_truncates_deterministically(conn):
    # Full edge order is (edge_type, src_id, dst_id, edge_id); a cap returns the deterministic
    # prefix, bounded by SQL LIMIT (cap + 1), never an unbounded fetch.
    nb = graph_read.neighborhood(conn, CLM_1, depth=2, edge_cap=3)
    assert nb["truncated"] is True
    pairs = [(e["src_id"], e["dst_id"], e["edge_type"]) for e in nb["edges"]]
    assert pairs == [
        (CLM_1, CLM_2, "contradicts"),
        (CLM_1, SRC_A, "derived_from"),
        (CLM_2, SRC_B, "derived_from"),
    ]


def test_graph_surfaces_retired_nodes_via_active_edges(tmp_path):
    # Edge-status-only traversal (ADR-0032 addendum 2): a deleted node reachable by an active edge
    # still appears, carrying its real status and answer_eligible: false. Pins the default.
    src = "src_cccccccccccccccc"
    deleted = "cpt_deadbeefdeadbeef"
    db_path = tmp_path / "db" / "graph.sqlite"
    graph.init_db(db_path)
    c = graph.connect(db_path)
    graph.reindex_nodes(
        c, source_ids=[src],
        page_nodes=[{"node_id": deleted, "node_type": "concept", "slug": "d", "status": "deleted"}],
        now="t0",
    )
    graph.upsert_assertion(c, src_id=src, dst_id=deleted, edge_type="mentions",
                           asserted_by="llm", status="active")
    nb = graph_read.neighborhood(c, src, depth=1)
    node = next(n for n in nb["nodes"] if n["node_id"] == deleted)
    assert node["status"] == "deleted"
    assert node["answer_eligible"] is False


def test_neighborhood_missing_root(conn):
    assert graph_read.neighborhood(conn, "clm_missing") is None


def test_neighborhood_is_deterministic(conn):
    a = graph_read.neighborhood(conn, CLM_1, depth=2)
    b = graph_read.neighborhood(conn, CLM_1, depth=2)
    assert a == b


# --------------------------------------------------------------------------- param parsing


def test_parse_edge_statuses_default_and_validation():
    assert graph_read.parse_edge_statuses(None) == ("active",)
    assert graph_read.parse_edge_statuses("active,proposed") == ("active", "proposed")
    with pytest.raises(ValueError):
        graph_read.parse_edge_statuses("bogus")


def test_parse_edge_and_node_types_validation():
    assert graph_read.parse_edge_types(None) is None
    assert graph_read.parse_node_types("source,concept") == frozenset({"source", "concept"})
    with pytest.raises(ValueError):
        graph_read.parse_edge_types("frobnicates")
    with pytest.raises(ValueError):
        graph_read.parse_node_types("widget")
