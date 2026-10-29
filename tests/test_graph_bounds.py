"""ADR-0068 decision 7: bounded graph traversal.

Covers the traversal indexes (installation, definitions, startup and restore hooks, fail-closed
reads), the per-node fan-out with its stable order, ranked round-robin admission, the query budget,
induced-within-fan-out output edges, the bounded node view, the disagreement seeds, and database
work measured apart from response size (query plans, rows fetched).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from fastapi.testclient import TestClient  # noqa: E402

import backup  # noqa: E402
from app.backend import graph, graph_read, revision, search, write_lock  # noqa: E402
from app.backend import main as main_module  # noqa: E402
from app.backend.config import get_settings  # noqa: E402

NOW = "2026-10-29T00:00:00Z"


# --- fixtures ------------------------------------------------------------------------------------


def _graph(root: Path) -> Path:
    path = root / "db" / "graph.sqlite"
    graph.init_db(path)
    return path


def _conn(path: Path) -> sqlite3.Connection:
    return graph.connect(path)


def _node(conn, nid, node_type="item", status="active"):
    conn.execute("INSERT OR IGNORE INTO nodes(node_id, node_type, slug, status) VALUES (?,?,?,?)",
                 (nid, node_type, nid, status))


def _edge(conn, src, dst, edge_type="mentions", status="active", eid=None):
    eid = eid or f"e_{src}_{edge_type}_{dst}_{status}"
    conn.execute("INSERT INTO edges(edge_id, src_id, dst_id, edge_type, status, asserted_by, "
                 "created_at) VALUES (?,?,?,?,?,?,?)",
                 (eid, src, dst, edge_type, status, "human", NOW))
    return eid


def _hub(conn, hub="hub", n=100, prefix="h"):
    _node(conn, hub)
    for i in range(n):
        _node(conn, f"{prefix}{i:03d}", "source")
        _edge(conn, f"{prefix}{i:03d}", hub)


def _legacy(path: Path) -> None:
    """Give a graph the pre-decision-7 index set (an existing vault)."""
    conn = sqlite3.connect(path)
    for name in graph.TRAVERSAL_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.execute("CREATE INDEX idx_edges_src ON edges(src_id, status)")
    conn.execute("CREATE INDEX idx_edges_dst ON edges(dst_id, status)")
    conn.commit()
    conn.close()


def _index_names(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' "
                                           "AND name NOT LIKE 'sqlite_%'")}
    finally:
        conn.close()


def _rev(root):
    with write_lock.hold(Path(root) / "reviews", authority=False, changes=False):
        return revision.current(root)


# --- installing the indexes (Q3) -----------------------------------------------------------------


def test_an_existing_vault_gets_the_indexes_and_loses_the_legacy_ones(tmp_path):
    path = _graph(tmp_path)
    _legacy(path)
    before = _rev(tmp_path)
    assert graph.ensure_indexes(path) is True
    assert _index_names(path) == {*graph.TRAVERSAL_INDEXES, "uq_edges_assertion"}
    assert _rev(tmp_path) == before                            # physical: tokens stay valid
    assert graph.ensure_indexes(path) is True                  # repeated: a no-op
    assert _index_names(path) == {*graph.TRAVERSAL_INDEXES, "uq_edges_assertion"}


def test_installation_replaces_an_index_with_the_right_name_but_wrong_definition(tmp_path):
    path = _graph(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("DROP INDEX idx_edges_src_walk")
    conn.execute("CREATE INDEX idx_edges_src_walk ON edges(src_id)")
    conn.commit()
    conn.close()
    graph.ensure_indexes(path)
    conn = sqlite3.connect(path)
    assert graph._index_columns(conn, "idx_edges_src_walk") == \
        graph.TRAVERSAL_INDEXES["idx_edges_src_walk"]
    conn.close()


def test_installation_never_creates_or_upgrades_a_graph(tmp_path):
    missing = tmp_path / "db" / "graph.sqlite"
    assert graph.ensure_indexes(missing) is False and not missing.exists()
    old = tmp_path / "old.sqlite"
    conn = sqlite3.connect(old)
    conn.execute("CREATE TABLE nodes(node_id TEXT)")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    with pytest.raises(graph.GraphSchemaError):
        graph.ensure_indexes(old)


def test_api_startup_installs_the_indexes(tmp_path, monkeypatch):
    path = _graph(tmp_path)
    _legacy(path)
    monkeypatch.setattr(main_module, "settings", get_settings(tmp_path))
    with TestClient(main_module.app):                           # runs the lifespan
        pass
    assert set(graph.TRAVERSAL_INDEXES) <= _index_names(path)


def test_restoring_an_older_graph_installs_the_indexes(tmp_path):
    path = _graph(tmp_path)
    _legacy(path)
    archive = backup.create_backup(tmp_path)                    # an older graph.sqlite
    graph.ensure_indexes(path)
    report = backup.restore_backup(archive, tmp_path, force=True)
    assert report.graph_index_error is None
    assert set(graph.TRAVERSAL_INDEXES) <= _index_names(path)
    assert not {"idx_edges_src", "idx_edges_dst"} & _index_names(path)


def test_a_restore_reports_an_index_failure_explicitly(tmp_path, monkeypatch):
    path = _graph(tmp_path)
    archive = backup.create_backup(tmp_path)

    def broken(_path):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(graph, "ensure_indexes", broken)
    report = backup.restore_backup(archive, tmp_path, force=True)
    assert "disk I/O error" in report.graph_index_error
    assert "GRAPH INDEXES NOT INSTALLED" in report.render()
    assert path.exists()


# --- missing indexes fail closed (Q3) ------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = _graph(tmp_path)
    conn = _conn(path)
    _hub(conn, n=5)
    conn.commit()
    conn.close()
    monkeypatch.setattr(main_module, "settings", get_settings(tmp_path))
    return TestClient(main_module.app)


def test_missing_indexes_refuse_graph_views_and_degrade_search(client, tmp_path):
    conn = sqlite3.connect(tmp_path / "db" / "graph.sqlite")
    conn.execute("DROP INDEX idx_edges_dst_walk")
    conn.commit()
    conn.close()
    for url in ("/graph/neighborhood/hub", "/graph/node/hub"):
        resp = client.get(url)
        assert resp.status_code == 503
        assert resp.json()["detail"]["reason"] == "graph_index_missing"
        assert resp.json()["detail"]["action"] == "restart the API to install graph indexes"
    gconn = _conn(tmp_path / "db" / "graph.sqlite")
    policy = main_module.load_retrieval_policy(ROOT / "policies" / "retrieval.yaml")
    res = search.run_search(q="which sources disagree", mode="auto", keyword_conn=None,
                            graph_conn=gconn, policy=policy)
    gconn.close()
    assert res["graph"]["nodes"] == [] and any("graph_index_missing" in n for n in res["notes"])


# --- query plans: database work, not response size (Q1/Q2) ---------------------------------------


@pytest.mark.parametrize("direction", ["out", "in"])
@pytest.mark.parametrize("edge_type", [None, "mentions"])
def test_every_stream_is_an_indexed_search_without_a_sort(tmp_path, direction, edge_type):
    conn = _conn(_graph(tmp_path))
    captured = []
    real = conn.execute

    class Spy:
        def execute(self, sql, params=()):
            if sql.lstrip().startswith("SELECT * FROM edges"):
                captured.append([r[3] for r in real("EXPLAIN QUERY PLAN " + sql, params)])
            return real(sql, params)
    for status in ("active", "proposed"):
        graph_read._stream_rows(Spy(), "x", direction, status, edge_type, 26)
    assert captured and all(len(plan) == 1 for plan in captured)
    for plan in captured:
        assert "SEARCH edges USING INDEX idx_edges_" in plan[0] and "TEMP B-TREE" not in plan[0]


def test_the_disagreement_seed_query_has_its_own_indexed_plan(tmp_path):
    conn = _conn(_graph(tmp_path))
    plan = [r[3] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT src_id, dst_id FROM edges INDEXED BY idx_edges_type_walk "
        "WHERE edge_type = 'contradicts' AND status = 'active' ORDER BY src_id, dst_id LIMIT 51")]
    assert plan == ["SEARCH edges USING COVERING INDEX idx_edges_type_walk "
                    "(edge_type=? AND status=?)"]


def test_rows_fetched_stay_within_fanout_whatever_the_hub_degree(tmp_path, monkeypatch):
    conn = _conn(_graph(tmp_path))
    _hub(conn, n=1000)
    conn.commit()
    fetched = []
    real = graph_read._stream_rows

    def counting(*a, **k):
        rows = real(*a, **k)
        fetched.append(len(rows))
        return rows
    monkeypatch.setattr(graph_read, "_stream_rows", counting)
    out = graph_read.neighborhood(conn, "hub", depth=1, fanout=10, node_cap=1000, edge_cap=2000)
    assert out["truncated"] is True and len(out["nodes"]) == 11
    assert max(fetched) <= 11                                   # k + 1 per stream
    assert sum(fetched) <= 11 * len(fetched)


# --- selection and admission (Q1/Q2/Q4) ----------------------------------------------------------


def test_a_large_hub_never_crowds_out_a_smaller_ranked_seed(tmp_path):
    conn = _conn(_graph(tmp_path))
    _hub(conn, "hub", n=100, prefix="h")
    _hub(conn, "small", n=3, prefix="s")
    conn.commit()
    out = graph_read.search_subgraph(conn, ["hub", "small"], depth=1, node_cap=12,
                                     edge_cap=100, fanout=25, budget=500)
    ids = {n["node_id"] for n in out["nodes"]}
    assert {"s000", "s001", "s002"} <= ids                     # round-robin admitted them
    assert out["truncated"] is True and len(ids) == 12


def test_selection_is_deterministic_and_survives_a_rebuild_in_another_order(tmp_path):
    def build(root, order):
        conn = _conn(_graph(root))
        _node(conn, "hub")
        for i in order:
            _node(conn, f"n{i:02d}", "source")
            _edge(conn, f"n{i:02d}", "hub", eid=f"e{i:02d}")
        conn.commit()
        return conn
    a = build(tmp_path / "a", range(30))
    b = build(tmp_path / "b", reversed(range(30)))
    pick = [graph_read.neighborhood(c, "hub", fanout=5)["nodes"] for c in (a, a, b)]
    assert pick[0] == pick[1] == pick[2]
    assert [n["node_id"] for n in pick[0]] == ["hub", "n00", "n01", "n02", "n03", "n04"]


def test_an_edge_between_two_frontier_nodes_is_counted_once(tmp_path):
    conn = _conn(_graph(tmp_path))
    for n in ("a", "b"):
        _node(conn, n)
    _edge(conn, "a", "b", "related_to")
    conn.commit()
    out = graph_read.search_subgraph(conn, ["a", "b"], depth=1, node_cap=10, edge_cap=10)
    assert [e["edge_id"] for e in out["edges"]] == ["e_a_related_to_b_active"]
    assert out["truncated"] is False


def test_a_self_loop_seen_from_both_directions_never_truncates(tmp_path):
    conn = _conn(_graph(tmp_path))
    _node(conn, "a")
    _node(conn, "b")
    _edge(conn, "a", "a", "related_to")
    _edge(conn, "a", "b", "related_to")
    conn.commit()
    out = graph_read.neighborhood(conn, "a", fanout=2)
    assert out["truncated"] is False and len(out["edges"]) == 2


def test_disagreement_seeds_are_bounded_and_clipping_truncates(tmp_path):
    conn = _conn(_graph(tmp_path))
    for i in range(40):
        _node(conn, f"c{i:02d}a", "claim")
        _node(conn, f"c{i:02d}b", "claim")
        _edge(conn, f"c{i:02d}a", f"c{i:02d}b", "contradicts")
    conn.commit()
    ids, clipped = graph_read.active_contradiction_endpoints(conn, 50)
    assert len(ids) == 50 and clipped is True
    ids, clipped = graph_read.active_contradiction_endpoints(conn, 100)
    assert len(ids) == 80 and clipped is False
    out = search.graph_group(conn, [], shape="disagreement", depth=1, edge_statuses=("active",),
                             node_statuses=("active",), node_types=None, node_cap=200,
                             edge_cap=200, max_seeds=10)
    assert len(out["seeds"]) == 10 and out["truncated"] is True


def test_policy_values_cannot_lift_the_hard_ceilings(tmp_path):
    conn = _conn(_graph(tmp_path))
    _hub(conn, n=300)
    conn.commit()
    out = graph_read.search_subgraph(conn, ["hub"], depth=1, node_cap=1000, edge_cap=2000,
                                     fanout=10 ** 6, budget=10 ** 9)
    assert len(out["nodes"]) == 1 + graph_read.HARD_MAX_FANOUT and out["truncated"] is True


# --- the query budget (Q4) -----------------------------------------------------------------------


def _chain(conn):
    for n in ("r", "a", "b", "a1", "b1"):
        _node(conn, n)
    for s, d in (("r", "a"), ("r", "b"), ("a", "a1"), ("b", "b1")):
        _edge(conn, s, d, "related_to")
    conn.commit()


def test_exact_budget_exhaustion_is_not_truncation(tmp_path):
    conn = _conn(_graph(tmp_path))
    _chain(conn)
    # depth 2: r, a, b expanded (3 nodes × 2 streams) + outer ring a1, b1 (2 × 2) = 10 streams.
    out = graph_read.search_subgraph(conn, ["r"], depth=2, node_cap=10, edge_cap=10, budget=10)
    assert out["truncated"] is False and len(out["nodes"]) == 5 and len(out["edges"]) == 4


def test_too_little_budget_for_the_next_node_stops_and_truncates(tmp_path):
    conn = _conn(_graph(tmp_path))
    _chain(conn)
    out = graph_read.search_subgraph(conn, ["r"], depth=2, node_cap=10, edge_cap=10, budget=5)
    ids = {n["node_id"] for n in out["nodes"]}
    assert out["truncated"] is True
    assert "b1" not in ids                  # b could not be expanded: 2 streams, 1 left


def test_streams_multiply_by_statuses_and_types(tmp_path, monkeypatch):
    conn = _conn(_graph(tmp_path))
    _hub(conn, n=3)
    conn.commit()
    calls = []
    real = graph_read._stream_rows
    monkeypatch.setattr(graph_read, "_stream_rows",
                        lambda *a, **k: calls.append(a[2:5]) or real(*a, **k))
    out = graph_read.search_subgraph(conn, ["hub"], depth=0, node_cap=10, edge_cap=10,
                                     edge_statuses=("active", "proposed"),
                                     edge_types=("mentions", "related_to"), budget=8)
    assert len(calls) == 8 and out["truncated"] is False      # 2 directions × 2 × 2
    calls.clear()
    out = graph_read.search_subgraph(conn, ["hub"], depth=0, node_cap=10, edge_cap=10,
                                     edge_statuses=("active", "proposed"),
                                     edge_types=("mentions", "related_to"), budget=7)
    assert calls == [] and out["truncated"] is True             # never a partial node


# --- output edges: induced within fan-out (Q5) ---------------------------------------------------


def test_an_outer_ring_cross_edge_is_kept(tmp_path):
    conn = _conn(_graph(tmp_path))
    for n in ("claim", "src", "tag"):
        _node(conn, n)
    _edge(conn, "claim", "src", "derived_from")
    _edge(conn, "claim", "tag", "related_to")
    _edge(conn, "src", "tag", "mentions")                      # between two outer-ring nodes
    conn.commit()
    out = graph_read.neighborhood(conn, "claim", depth=1)
    assert ("src", "tag") in {(e["src_id"], e["dst_id"]) for e in out["edges"]}
    assert out["truncated"] is False


def test_a_cross_edge_outside_both_endpoints_top_k_is_omitted_with_truncation(tmp_path):
    conn = _conn(_graph(tmp_path))
    for n in ("r", "x", "y"):
        _node(conn, n)
    _edge(conn, "r", "x", "related_to")
    _edge(conn, "r", "y", "related_to")
    for n in ("x", "y"):                                       # each gets edges that sort first
        for i in range(3):
            _node(conn, f"{n}_{i}")
            _edge(conn, n, f"{n}_{i}", "derived_from")
    _edge(conn, "x", "y", "supports")                          # sorts after derived_from/related_to
    conn.commit()
    out = graph_read.neighborhood(conn, "r", depth=1, fanout=3)
    pairs = {(e["src_id"], e["dst_id"]) for e in out["edges"]}
    assert ("x", "y") not in pairs and out["truncated"] is True


def test_budget_exhaustion_during_the_outer_ring_truncates(tmp_path):
    conn = _conn(_graph(tmp_path))
    _chain(conn)
    # depth 1: r expanded (2) + a, b in the outer ring (2 each) = 6; give 4.
    out = graph_read.search_subgraph(conn, ["r"], depth=1, node_cap=10, edge_cap=10, budget=4)
    assert out["truncated"] is True and len(out["nodes"]) == 3


def test_depth_zero_returns_the_root_and_its_own_retained_edges(tmp_path):
    conn = _conn(_graph(tmp_path))
    _node(conn, "a")
    _node(conn, "b")
    _edge(conn, "a", "a", "related_to")
    _edge(conn, "a", "b", "related_to")
    conn.commit()
    out = graph_read.neighborhood(conn, "a", depth=0)
    assert [n["node_id"] for n in out["nodes"]] == ["a"]
    assert [(e["src_id"], e["dst_id"]) for e in out["edges"]] == [("a", "a")]


# --- the node view (Q6) --------------------------------------------------------------------------


def test_the_node_view_of_a_large_hub_is_bounded(tmp_path, monkeypatch):
    conn = _conn(_graph(tmp_path))
    _hub(conn, n=300)
    _edge(conn, "hub", "hub", "related_to")                    # a self-loop
    _node(conn, "out1", "source")
    _edge(conn, "hub", "out1", "mentions")
    conn.commit()
    sizes, fetched = [], []
    real = graph_read._node_meta_map
    monkeypatch.setattr(graph_read, "_node_meta_map",
                        lambda c, ids: sizes.append(len(ids)) or real(c, ids))
    real_rows = graph_read._stream_rows
    monkeypatch.setattr(graph_read, "_stream_rows",
                        lambda *a, **k: fetched.append(len(r := real_rows(*a, **k))) or r)
    view = graph_read.node_view(conn, "hub", edge_limit=50)
    assert max(fetched) == 51                                  # limit + 1 rows, never the degree
    assert len(view["incoming"]["mentions"]) == 50 and view["truncated"] is True
    assert view["truncated_groups"] == [{"direction": "incoming", "edge_type": "mentions",
                                         "status": "active"}]
    assert view["returned"] == {"outgoing": 2, "incoming": 50}
    assert [a["other_node_id"] for a in view["outgoing"]["related_to"]] == ["hub"]  # listed once
    assert "related_to" not in view["incoming"]
    assert sizes == [51]                                       # 50 + out1 (hub itself excluded)


@pytest.mark.parametrize("n,truncated", [(50, False), (51, True)])
def test_a_group_at_exactly_the_limit_is_not_truncated(tmp_path, n, truncated):
    conn = _conn(_graph(tmp_path))
    _hub(conn, n=n)
    conn.commit()
    view = graph_read.node_view(conn, "hub", edge_limit=50)
    assert len(view["incoming"]["mentions"]) == 50 and view["truncated"] is truncated


def test_the_api_node_view_reports_returned_and_truncation(client):
    body = client.get("/graph/node/hub?edge_limit=2").json()
    assert body["returned"] == {"outgoing": 0, "incoming": 2} and body["truncated"] is True
    body = client.get("/graph/neighborhood/hub?fanout_limit=2").json()
    assert body["truncated"] is True and body["cap"]["fanout"] == 2 and len(body["nodes"]) == 3


# --- review round 1 on decision 7 -----------------------------------------------------------------


def _vm_steps(conn, fn) -> int:
    """SQLite virtual-machine work done by `fn` (progress-handler calls, every 100 instructions)."""
    steps = [0]

    def tick():
        steps[0] += 1
        return 0
    conn.set_progress_handler(tick, 100)
    try:
        fn()
    finally:
        conn.set_progress_handler(None, 0)
    return steps[0]


def test_excluding_self_loops_never_scans_them(tmp_path):
    def work(root, loops):
        conn = _conn(_graph(root))
        _node(conn, "hub")
        _node(conn, "src", "source")
        _edge(conn, "src", "hub", "related_to")
        for i in range(loops):
            conn.execute("INSERT INTO edges(edge_id, src_id, dst_id, edge_type, status, asserted_by,"
                         " evidence_source_id, evidence_char_start, evidence_char_end, created_at)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (f"loop{i}", "hub", "hub", "related_to", "active", "human", "s", i, i + 1,
                          NOW))
        conn.commit()
        view = {}
        steps = _vm_steps(conn, lambda: view.update(graph_read.node_view(conn, "hub", edge_limit=5)))
        assert [a["other_node_id"] for a in view["incoming"]["related_to"]] == ["src"]
        return steps
    few, many = work(tmp_path / "few", 10), work(tmp_path / "many", 10_000)
    assert many <= few * 2 + 50                                # not proportional to the self-loops


def test_parallel_assertions_are_picked_the_same_after_a_rebuild_in_either_order(tmp_path):
    anchors = [("s1", 10, 20), ("s1", 30, 40), ("s2", 5, 9)]

    def build(root, order):
        conn = _conn(_graph(root))
        _node(conn, "a")
        _node(conn, "b")
        for i in order:                                        # real random edge ids
            sid, start, end = anchors[i]
            graph.upsert_assertion(conn, src_id="a", dst_id="b", edge_type="mentions",
                                   asserted_by="llm", status="active", evidence_source_id=sid,
                                   evidence_char_start=start, evidence_char_end=end, now=NOW)
        conn.commit()
        return conn

    def picked(conn, **kw):
        out = graph_read.neighborhood(conn, "a", **kw)
        return [(e["evidence"]["source_id"], e["evidence"]["char_start"]) for e in out["edges"]]
    a, b = build(tmp_path / "a", [0, 1, 2]), build(tmp_path / "b", [2, 1, 0])
    assert picked(a, fanout=1) == picked(b, fanout=1) == [("s1", 10)]
    assert picked(a, fanout=3) == picked(b, fanout=3) == [("s1", 10), ("s1", 30), ("s2", 5)]
    view_a, view_b = (graph_read.node_view(c, "a", edge_limit=2) for c in (a, b))
    strip = [(x["evidence"]["source_id"], x["evidence"]["char_start"])
             for x in view_a["outgoing"]["mentions"]]
    assert strip == [(x["evidence"]["source_id"], x["evidence"]["char_start"])
                     for x in view_b["outgoing"]["mentions"]] == [("s1", 10), ("s1", 30)]


def test_a_self_loop_reports_the_node_itself_as_its_other_end(tmp_path):
    conn = _conn(_graph(tmp_path))
    _node(conn, "a")
    _edge(conn, "a", "a", "related_to")
    conn.commit()
    view = graph_read.node_view(conn, "a")
    (loop,) = view["outgoing"]["related_to"]
    assert loop["other_node_id"] == "a" and loop["other"] == view["node"]
    assert loop["other"]["node_type"] == "item" and loop["other"]["answer_eligible"] is True


@pytest.mark.parametrize("other_range", ["lt", "gt"])
def test_the_self_loop_free_ranges_are_indexed_searches_without_a_sort(tmp_path, other_range):
    conn = _conn(_graph(tmp_path))
    sql = ("SELECT * FROM edges INDEXED BY idx_edges_dst_walk WHERE dst_id = ? AND status = ? "
           f"AND edge_type = ? AND src_id {'<' if other_range == 'lt' else '>'} ? ORDER BY src_id, "
           f"{graph_read._KEY_SQL} LIMIT ?")
    plan = [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, ("x", "active", "mentions", "x", 6))]
    assert len(plan) == 1 and "src_id" in plan[0] and "TEMP B-TREE" not in plan[0]


def test_parallel_assertions_in_different_status_streams_merge_by_the_assertion_key(tmp_path):
    def build(root, order):
        conn = _conn(_graph(root))
        _node(conn, "a")
        _node(conn, "b")
        for status, start in order:                            # one per status stream
            graph.upsert_assertion(conn, src_id="a", dst_id="b", edge_type="mentions",
                                   asserted_by="llm", status=status, evidence_source_id="s1",
                                   evidence_char_start=start, evidence_char_end=start + 5, now=NOW)
        conn.commit()
        return conn
    for order in ([("active", 30), ("proposed", 10)], [("proposed", 10), ("active", 30)]):
        for _ in range(3):                                     # fresh random ids each time
            conn = build(tmp_path / f"v{len(list(tmp_path.iterdir()))}", order)
            out = graph_read.neighborhood(conn, "a", fanout=1,
                                          include_status=("active", "proposed"))
            assert [e["evidence"]["char_start"] for e in out["edges"]] == [10]
