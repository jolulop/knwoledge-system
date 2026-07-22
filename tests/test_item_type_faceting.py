"""ADR-0062 item_type retrieval faceting: the active-only chunk↔item bridge, the advisory evidence
boost (tie-break + anti-hidden-filter), the navigation index item_type column + schema bump, and the
endpoint validation/notes. Structural + deterministic — no real embedder."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph, keyword_index, search  # noqa: E402
from app.backend import graph_read  # noqa: E402

SRC_M = "src_" + "a" * 16   # bridges to a method_technique item
SRC_D = "src_" + "b" * 16   # bridges to a deprecated method_technique item (must NOT bridge)
ITM_ACT = "itm_" + "1" * 16
ITM_DEP = "itm_" + "2" * 16


# --- bridge: active items + active mentions only ----------------------------


def _graph(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    graph.init_db(gdb)
    gconn = graph.connect(gdb)
    graph.reindex_nodes(gconn, source_ids=[SRC_M, SRC_D], page_nodes=[
        {"node_id": ITM_ACT, "node_type": "item", "item_type": "method_technique",
         "slug": "a", "status": "active"},
        {"node_id": ITM_DEP, "node_type": "item", "item_type": "method_technique",
         "slug": "d", "status": "deprecated_candidate"},
    ], now="t0")
    return gconn


def test_bridge_maps_source_to_active_item_types(tmp_path):
    gconn = _graph(tmp_path)
    graph.upsert_assertion(gconn, src_id=SRC_M, dst_id=ITM_ACT, edge_type="mentions",
                           asserted_by="llm", status="active")
    got = graph_read.source_item_types(gconn, {SRC_M})
    assert got == {SRC_M: frozenset({"method_technique"})}


def test_bridge_ignores_deprecated_item(tmp_path):
    gconn = _graph(tmp_path)
    graph.upsert_assertion(gconn, src_id=SRC_D, dst_id=ITM_DEP, edge_type="mentions",
                           asserted_by="llm", status="active")
    # The mention edge is active but the item is deprecated_candidate — no bridge.
    assert graph_read.source_item_types(gconn, {SRC_D}) == {}


def test_bridge_ignores_superseded_mention(tmp_path):
    gconn = _graph(tmp_path)
    graph.upsert_assertion(gconn, src_id=SRC_M, dst_id=ITM_ACT, edge_type="mentions",
                           asserted_by="llm", status="superseded")
    # The item is active but the mention is superseded — no bridge.
    assert graph_read.source_item_types(gconn, {SRC_M}) == {}


def test_bridge_batches_empty_input(tmp_path):
    gconn = _graph(tmp_path)
    assert graph_read.source_item_types(gconn, set()) == {}


# --- evidence boost: additive, tie-break, anti-hidden-filter -----------------


def _hit(sid, score, *, ordinal=0, cs=0):
    return {"source_id": sid, "char_start": cs, "char_end": cs + 5, "ordinal": ordinal, "score": score}


REQ = frozenset({"method_technique"})
BRIDGE = {SRC_M: frozenset({"method_technique"})}  # SRC_M on-type; SRC_D off-type


def test_boost_breaks_ties_promoting_on_type():
    # Two chunks with EQUAL relevance; the on-type one wins after the boost.
    pool = [_hit(SRC_D, 0.0164, cs=0), _hit(SRC_M, 0.0164, cs=10)]
    out = search.apply_item_type_boost(pool, source_types=BRIDGE, requested=REQ, boost=0.005)
    assert out[0]["source_id"] == SRC_M
    assert out[0]["item_type_boosted"] is True
    assert "item_type_boosted" not in out[1]  # off-type untouched


def test_boost_does_not_outrank_much_more_relevant_off_type():
    # Anti-hidden-filter: a strongly-relevant off-type chunk (multi-channel RRF ~0.033) stays above
    # a weak on-type chunk (single-channel ~0.015) even after the bounded boost.
    strong_off = _hit(SRC_D, 0.0328, cs=0)
    weak_on = _hit(SRC_M, 0.0154, cs=10)
    out = search.apply_item_type_boost([strong_off, weak_on], source_types=BRIDGE,
                                       requested=REQ, boost=0.005)
    assert out[0]["source_id"] == SRC_D          # relevance dominates the bounded boost
    assert out[1]["item_type_boosted"] is True   # the on-type chunk WAS boosted, just not enough


def test_boost_never_drops_off_type_evidence():
    pool = [_hit(SRC_D, 0.02, cs=0), _hit(SRC_M, 0.01, cs=10)]
    out = search.apply_item_type_boost(pool, source_types=BRIDGE, requested=REQ, boost=0.005)
    assert {h["source_id"] for h in out} == {SRC_D, SRC_M}  # boost re-ranks, never filters


# --- navigation index: item_type column + schema version --------------------


def _write_page(root, rel, fm):
    lines = "\n".join(f"{k}: {v}" for k, v in fm.items())
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{lines}\n---\n\n# {fm.get('title', 'P')}\n\n> [!summary]\n> s\n", encoding="utf-8")


def _nav_rows(root):
    conn = keyword_index.connect(root / keyword_index.DB_RELPATH)
    try:
        return {r["path"]: r["item_type"] for r in conn.execute(
            "SELECT path, item_type FROM navigation")}
    finally:
        conn.close()


def test_nav_index_item_type_populated_for_items_null_for_others(tmp_path):
    _write_page(tmp_path, "wiki/Items/itm_x.md",
                {"type": "item", "item_id": "itm_x", "item_type": "model", "title": "X", "status": "active"})
    _write_page(tmp_path, "wiki/Sources/src_y.md",
                {"type": "source", "source_id": "src_y", "title": "Y", "status": "active"})
    keyword_index.reindex(tmp_path, force=True)
    rows = _nav_rows(tmp_path)
    assert rows["wiki/Items/itm_x.md"] == "model"
    assert rows["wiki/Sources/src_y.md"] == ""   # non-item pages carry no item_type


def test_nav_index_refreshes_item_type_after_retype(tmp_path):
    page = "wiki/Items/itm_x.md"
    _write_page(tmp_path, page,
                {"type": "item", "item_id": "itm_x", "item_type": "model", "title": "X", "status": "active"})
    keyword_index.reindex(tmp_path, force=True)
    assert _nav_rows(tmp_path)[page] == "model"
    # Simulate a retype re-rendering the page frontmatter; an incremental reindex must refresh it.
    _write_page(tmp_path, page,
                {"type": "item", "item_id": "itm_x", "item_type": "method_technique", "title": "X", "status": "active"})
    keyword_index.reindex(tmp_path)  # not force: fingerprint change alone must re-pick it up
    assert _nav_rows(tmp_path)[page] == "method_technique"


def test_index_version_bumped_to_two():
    assert keyword_index.INDEX_VERSION == 2


def test_stale_v1_nav_index_is_rebuilt_or_flagged(tmp_path):
    _write_page(tmp_path, "wiki/Items/itm_x.md",
                {"type": "item", "item_id": "itm_x", "item_type": "model", "title": "X", "status": "active"})
    keyword_index.reindex(tmp_path, force=True)
    # Stamp the index back to the old version → the consistency check must flag it stale-schema.
    conn = keyword_index.connect(tmp_path / keyword_index.DB_RELPATH)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    errs = keyword_index.consistency_errors(tmp_path, conn)
    conn.close()
    assert any("schema version" in e for e in errs)
    # A full reindex rebuilds cleanly to the current version → consistency restored.
    keyword_index.reindex(tmp_path, force=True)
    conn = keyword_index.connect(tmp_path / keyword_index.DB_RELPATH)
    assert keyword_index.index_version(conn) == keyword_index.INDEX_VERSION
    assert keyword_index.consistency_errors(tmp_path, conn) == []
    conn.close()
