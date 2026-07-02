"""Phase 6 slice 6-1: review read model (ADR-0035 A1-A3).

Unit tests over app.backend.review_read — deterministic list (filter/sort/pagination/by_type),
malformed-JSON robustness, the per-type preview projection registry, and the read-only best-effort
effect_status derivation against a small fixture graph + wiki page. Key-free, no LLM, no TestClient.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph, review_read


def _write_item(reviews_dir: Path, state: str, item: dict) -> Path:
    d = reviews_dir / state
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{item['review_id']}.json"
    path.write_text(json.dumps(item), encoding="utf-8")
    return path


def _item(rid: str, rtype: str, *, status: str = "pending", priority: str = "low",
          created_at: str | None = None, subject: dict | None = None,
          proposal: dict | None = None, context: dict | None = None) -> dict:
    return {
        "review_id": rid, "type": rtype, "status": status, "priority": priority,
        "created_at": created_at, "subject": subject or {}, "proposal": proposal or {},
        "context": context or {},
    }


# --- list: filtering / status semantics ------------------------------------


def test_default_lists_pending_and_excludes_deferred(tmp_path):
    rv = tmp_path / "reviews"
    _write_item(rv, "pending", _item("rev_a", "promote_candidate_node"))
    _write_item(rv, "pending", _item("rev_b", "deprecate_wiki_page", status="deferred"))

    out = review_read.list_reviews(rv)
    ids = [it["review_id"] for it in out["items"]]
    assert ids == ["rev_a"]
    assert out["count"] == 1
    assert out["by_type"] == {"promote_candidate_node": 1}


def test_status_deferred_reads_pending_dir_filters_field(tmp_path):
    rv = tmp_path / "reviews"
    _write_item(rv, "pending", _item("rev_a", "promote_candidate_node"))
    _write_item(rv, "pending", _item("rev_b", "deprecate_wiki_page", status="deferred"))

    out = review_read.list_reviews(rv, status="deferred")
    assert [it["review_id"] for it in out["items"]] == ["rev_b"]
    assert out["count"] == 1


def test_filter_by_type_and_priority(tmp_path):
    rv = tmp_path / "reviews"
    _write_item(rv, "pending", _item("rev_a", "promote_candidate_node", priority="high"))
    _write_item(rv, "pending", _item("rev_b", "promote_candidate_node", priority="low"))
    _write_item(rv, "pending", _item("rev_c", "deprecate_wiki_page", priority="high"))

    by_type = review_read.list_reviews(rv, type="promote_candidate_node")
    assert {it["review_id"] for it in by_type["items"]} == {"rev_a", "rev_b"}
    by_prio = review_read.list_reviews(rv, priority="high")
    assert {it["review_id"] for it in by_prio["items"]} == {"rev_a", "rev_c"}


def test_unknown_status_and_priority_raise(tmp_path):
    rv = tmp_path / "reviews"
    with pytest.raises(ValueError):
        review_read.list_reviews(rv, status="bogus")
    with pytest.raises(ValueError):
        review_read.list_reviews(rv, priority="urgent")


# --- list: sort / pagination / counts --------------------------------------


def test_deterministic_sort_priority_then_created_then_id(tmp_path):
    rv = tmp_path / "reviews"
    _write_item(rv, "pending", _item("rev_z", "promote_candidate_node", priority="low",
                                     created_at="2026-01-01T00:00:00Z"))
    _write_item(rv, "pending", _item("rev_m", "promote_candidate_node", priority="high",
                                     created_at="2026-02-01T00:00:00Z"))
    _write_item(rv, "pending", _item("rev_a", "promote_candidate_node", priority="high",
                                     created_at="2026-01-01T00:00:00Z"))
    # missing created_at sorts after present ones within the same priority, by review_id.
    _write_item(rv, "pending", _item("rev_h", "promote_candidate_node", priority="high",
                                     created_at=None))

    order = [it["review_id"] for it in review_read.list_reviews(rv)["items"]]
    # high priority first: created asc (rev_a 01-01, rev_m 02-01), then missing-created (rev_h); low last.
    assert order == ["rev_a", "rev_m", "rev_h", "rev_z"]


def test_count_and_by_type_cover_full_set_before_pagination(tmp_path):
    rv = tmp_path / "reviews"
    for i in range(5):
        _write_item(rv, "pending", _item(f"rev_{i}", "promote_candidate_node",
                                         created_at=f"2026-01-0{i+1}T00:00:00Z"))
    out = review_read.list_reviews(rv, limit=2, offset=1)
    assert out["count"] == 5  # full filtered set, not the page
    assert out["by_type"] == {"promote_candidate_node": 5}
    assert [it["review_id"] for it in out["items"]] == ["rev_1", "rev_2"]


def test_malformed_json_skipped_and_counted(tmp_path):
    rv = tmp_path / "reviews"
    _write_item(rv, "pending", _item("rev_ok", "promote_candidate_node"))
    (rv / "pending" / "rev_bad.json").write_text("{not json", encoding="utf-8")

    out = review_read.list_reviews(rv)
    assert [it["review_id"] for it in out["items"]] == ["rev_ok"]
    assert out["parse_errors"] == 1


def test_empty_reviews_dir_is_clean(tmp_path):
    out = review_read.list_reviews(tmp_path / "reviews")
    assert out == {"count": 0, "by_type": {}, "parse_errors": 0, "schema_errors": 0, "items": []}


def test_schema_invalid_json_counted_separately_from_parse(tmp_path):
    rv = tmp_path / "reviews"
    _write_item(rv, "pending", _item("rev_ok", "promote_candidate_node"))
    (rv / "pending" / "rev_parse.json").write_text("{not json", encoding="utf-8")
    # valid JSON object, but not a usable ReviewItem (no review_id/type)
    (rv / "pending" / "rev_schema.json").write_text(json.dumps({"status": "pending"}),
                                                    encoding="utf-8")
    # valid required fields but a non-dict subject -> schema error
    (rv / "pending" / "rev_badsubj.json").write_text(json.dumps(
        {"review_id": "rev_badsubj", "type": "promote_candidate_node", "status": "pending",
         "subject": "oops"}), encoding="utf-8")
    out = review_read.list_reviews(rv)
    assert [it["review_id"] for it in out["items"]] == ["rev_ok"]
    assert out["parse_errors"] == 1
    assert out["schema_errors"] == 2


# --- detail: lookup + robustness -------------------------------------------


def test_get_review_not_found_returns_none(tmp_path):
    assert review_read.get_review(tmp_path / "reviews", "rev_missing") is None


def test_get_review_finds_across_states(tmp_path):
    rv = tmp_path / "reviews"
    _write_item(rv, "approved", _item("rev_app", "promote_candidate_node", status="approved"))
    res = review_read.get_review(rv, "rev_app")
    assert res is not None
    assert res["item"]["review_id"] == "rev_app"
    assert res["preview"]["type"] == "promote_candidate_node"


def test_get_review_malformed_marks_parse_error(tmp_path):
    rv = tmp_path / "reviews"
    (rv / "pending").mkdir(parents=True)
    (rv / "pending" / "rev_bad.json").write_text("{broken", encoding="utf-8")
    res = review_read.get_review(rv, "rev_bad")
    assert res is not None and res.get("parse_error") is True


# --- preview: record-only fallback -----------------------------------------


def test_decision_apply_required_classification():
    f = review_read.decision_apply_required
    # approve of an executor-backed type -> apply needed
    for t in ("promote_candidate_node", "propose_synthesis", "resolve_contradiction",
              "deprecate_wiki_page"):
        assert f(t, "approved") is True
    # reject only matters for the types with a reject-effect
    assert f("propose_synthesis", "rejected") is True
    assert f("resolve_contradiction", "rejected") is True
    assert f("promote_candidate_node", "rejected") is False
    assert f("deprecate_wiki_page", "rejected") is False
    # record-only types and deferrals never require apply (delete_raw_file stays record-only — ADR-0036)
    assert f("delete_raw_file", "approved") is False
    assert f("promote_candidate_node", "deferred") is False


def test_record_only_type_is_apply_deferred(tmp_path):
    rv = tmp_path / "reviews"
    _write_item(rv, "pending", _item("rev_m", "delete_raw_file",
                                     subject={"node_id": "ent_1"}))
    prev = review_read.get_review(rv, "rev_m")["preview"]
    assert prev["apply"]["supported"] is False
    assert prev["apply"]["effect_status"] == review_read.APPLY_DEFERRED
    assert prev["apply"]["effected"] is None
    assert prev["node_ids"] == ["ent_1"]


# --- preview: effect_status derivation (read-only) -------------------------


def _graph_with(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    return gdb, graph.connect(gdb)


def test_promote_pending_is_pending_apply(tmp_path):
    rv = tmp_path / "reviews"
    gdb, conn = _graph_with(tmp_path)
    graph.upsert_node(conn, node_id="cpt_1", node_type="concept", slug="x", status="candidate")
    conn.close()
    _write_item(rv, "pending", _item("rev_p", "promote_candidate_node",
                                     subject={"node_id": "cpt_1"},
                                     proposal={"to_status": "active", "node_type": "concept"}))
    prev = review_read.get_review(rv, "rev_p", graph_db=gdb, wiki_dir=tmp_path / "wiki")["preview"]
    assert prev["apply"]["effect_status"] == review_read.PENDING_APPLY
    assert prev["current_status"] == "candidate"


def test_promote_approved_node_active_is_effected(tmp_path):
    rv = tmp_path / "reviews"
    gdb, conn = _graph_with(tmp_path)
    graph.upsert_node(conn, node_id="cpt_1", node_type="concept", slug="x", status="active")
    conn.close()
    _write_item(rv, "approved", _item("rev_p", "promote_candidate_node", status="approved",
                                      subject={"node_id": "cpt_1"},
                                      proposal={"to_status": "active", "node_type": "concept"}))
    prev = review_read.get_review(rv, "rev_p", graph_db=gdb, wiki_dir=tmp_path / "wiki")["preview"]
    assert prev["apply"]["effect_status"] == review_read.EFFECTED
    assert prev["apply"]["effected"] is True


def test_promote_approved_without_graph_is_unknown(tmp_path):
    rv = tmp_path / "reviews"
    _write_item(rv, "approved", _item("rev_p", "promote_candidate_node", status="approved",
                                      subject={"node_id": "cpt_1"}))
    prev = review_read.get_review(rv, "rev_p", graph_db=tmp_path / "db" / "graph.sqlite",
                                  wiki_dir=tmp_path / "wiki")["preview"]
    assert prev["apply"]["effect_status"] == review_read.UNKNOWN
    assert "graph_unavailable" in prev["apply"]["warnings"]


def _write_synthesis_page(tmp_path, syn_id, *, status, review_status):
    page = tmp_path / "wiki" / "Synthesis" / f"{syn_id}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        f"---\ntype: synthesis\nstatus: {status}\nreview_status: {review_status}\n---\n",
        encoding="utf-8")


def test_synthesis_approved_active_node_and_page_is_effected(tmp_path):
    rv = tmp_path / "reviews"
    gdb, conn = _graph_with(tmp_path)
    syn_id = review_read._synthesis_id("cpt_topic")
    graph.upsert_node(conn, node_id=syn_id, node_type="synthesis", slug="s", status="active")
    conn.close()
    _write_synthesis_page(tmp_path, syn_id, status="active", review_status="approved")
    _write_item(rv, "approved", _item("rev_s", "propose_synthesis", status="approved",
                                      subject={"topic_node_id": "cpt_topic"}))
    prev = review_read.get_review(rv, "rev_s", graph_db=gdb, wiki_dir=tmp_path / "wiki")["preview"]
    assert prev["apply"]["effect_status"] == review_read.EFFECTED
    assert f"Synthesis/{syn_id}.md" in prev["affected_paths"]


def test_synthesis_node_active_but_page_missing_is_unknown(tmp_path):
    rv = tmp_path / "reviews"
    gdb, conn = _graph_with(tmp_path)
    syn_id = review_read._synthesis_id("cpt_topic")
    graph.upsert_node(conn, node_id=syn_id, node_type="synthesis", slug="s", status="active")
    conn.close()  # node active, but no Synthesis page written
    _write_item(rv, "approved", _item("rev_s", "propose_synthesis", status="approved",
                                      subject={"topic_node_id": "cpt_topic"}))
    prev = review_read.get_review(rv, "rev_s", graph_db=gdb, wiki_dir=tmp_path / "wiki")["preview"]
    assert prev["apply"]["effect_status"] == review_read.UNKNOWN
    assert "synthesis_page_unreadable" in prev["apply"]["warnings"]


def test_synthesis_node_active_but_page_not_target_is_pending_apply(tmp_path):
    rv = tmp_path / "reviews"
    gdb, conn = _graph_with(tmp_path)
    syn_id = review_read._synthesis_id("cpt_topic")
    graph.upsert_node(conn, node_id=syn_id, node_type="synthesis", slug="s", status="active")
    conn.close()
    _write_synthesis_page(tmp_path, syn_id, status="candidate", review_status="pending")
    _write_item(rv, "approved", _item("rev_s", "propose_synthesis", status="approved",
                                      subject={"topic_node_id": "cpt_topic"}))
    prev = review_read.get_review(rv, "rev_s", graph_db=gdb, wiki_dir=tmp_path / "wiki")["preview"]
    assert prev["apply"]["effect_status"] == review_read.PENDING_APPLY


def test_synthesis_rejected_checks_deprecated_state(tmp_path):
    rv = tmp_path / "reviews"
    gdb, conn = _graph_with(tmp_path)
    syn_id = review_read._synthesis_id("cpt_topic")
    graph.upsert_node(conn, node_id=syn_id, node_type="synthesis", slug="s",
                      status="deprecated_candidate")
    conn.close()
    _write_synthesis_page(tmp_path, syn_id, status="deprecated_candidate", review_status="rejected")
    _write_item(rv, "rejected", _item("rev_s", "propose_synthesis", status="rejected",
                                      subject={"topic_node_id": "cpt_topic"}))
    prev = review_read.get_review(rv, "rev_s", graph_db=gdb, wiki_dir=tmp_path / "wiki")["preview"]
    assert prev["apply"]["effect_status"] == review_read.EFFECTED


def test_contradiction_approved_active_edge_is_effected(tmp_path):
    rv = tmp_path / "reviews"
    gdb, conn = _graph_with(tmp_path)
    a, b = sorted(("clm_1", "clm_2"))
    graph.upsert_node(conn, node_id=a, node_type="claim", slug=a, status="active")
    graph.upsert_node(conn, node_id=b, node_type="claim", slug=b, status="active")
    graph.upsert_assertion(conn, src_id=a, dst_id=b, edge_type="contradicts",
                           asserted_by="llm", status="active")
    conn.close()
    _write_item(rv, "approved", _item("rev_c", "resolve_contradiction", status="approved",
                                      subject={"claim_a": "clm_1", "claim_b": "clm_2"}))
    prev = review_read.get_review(rv, "rev_c", graph_db=gdb, wiki_dir=tmp_path / "wiki")["preview"]
    assert prev["apply"]["effect_status"] == review_read.EFFECTED
    assert prev["affected_paths"] == ["Claims/clm_1.md", "Claims/clm_2.md"]


def _seed_contradiction(tmp_path, *, edge_status="active"):
    gdb, conn = _graph_with(tmp_path)
    a, b = sorted(("clm_1", "clm_2"))
    graph.upsert_node(conn, node_id=a, node_type="claim", slug=a, status="active")
    graph.upsert_node(conn, node_id=b, node_type="claim", slug=b, status="active")
    graph.upsert_assertion(conn, src_id=a, dst_id=b, edge_type="contradicts",
                           asserted_by="llm", status=edge_status)
    return gdb, conn


def test_contradiction_supersede_edge_active_but_effects_missing_is_pending(tmp_path):
    # Approved supersede (winner present) with the contradicts edge active but NO supersedes edge /
    # loser deprecation yet -> must be pending_apply, not effected (ADR-0035 A2 blocking-fix).
    rv = tmp_path / "reviews"
    gdb, conn = _seed_contradiction(tmp_path)
    conn.close()
    item = _item("rev_c", "resolve_contradiction", status="approved",
                 subject={"claim_a": "clm_1", "claim_b": "clm_2"})
    item["winner"] = "clm_1"
    _write_item(rv, "approved", item)
    prev = review_read.get_review(rv, "rev_c", graph_db=gdb, wiki_dir=tmp_path / "wiki")["preview"]
    assert prev["apply"]["effect_status"] == review_read.PENDING_APPLY


def test_contradiction_supersede_fully_applied_is_effected(tmp_path):
    rv = tmp_path / "reviews"
    gdb, conn = _seed_contradiction(tmp_path)
    # full supersede effects: active supersedes edge winner->loser + loser deprecated_candidate
    graph.upsert_assertion(conn, src_id="clm_1", dst_id="clm_2", edge_type="supersedes",
                           asserted_by="human", status="active")
    graph.upsert_node(conn, node_id="clm_2", node_type="claim", slug="clm_2",
                      status="deprecated_candidate")
    conn.close()
    item = _item("rev_c", "resolve_contradiction", status="approved",
                 subject={"claim_a": "clm_1", "claim_b": "clm_2"})
    item["winner"] = "clm_1"
    _write_item(rv, "approved", item)
    prev = review_read.get_review(rv, "rev_c", graph_db=gdb, wiki_dir=tmp_path / "wiki")["preview"]
    assert prev["apply"]["effect_status"] == review_read.EFFECTED


def test_contradiction_rejected_checks_edge_rejected(tmp_path):
    rv = tmp_path / "reviews"
    gdb, conn = _seed_contradiction(tmp_path, edge_status="rejected")
    conn.close()
    _write_item(rv, "rejected", _item("rev_c", "resolve_contradiction", status="rejected",
                                      subject={"claim_a": "clm_1", "claim_b": "clm_2"}))
    prev = review_read.get_review(rv, "rev_c", graph_db=gdb, wiki_dir=tmp_path / "wiki")["preview"]
    assert prev["apply"]["effect_status"] == review_read.EFFECTED


def test_deprecate_in_scope_effected_when_page_and_graph_agree(tmp_path):
    rv = tmp_path / "reviews"
    gdb, conn = _graph_with(tmp_path)
    graph.upsert_node(conn, node_id="clm_1", node_type="claim", slug="clm_1",
                      status="deprecated_candidate")
    conn.close()
    page = tmp_path / "wiki" / "Claims" / "clm_1.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: claim\nstatus: deprecated_candidate\nreview_status: approved\n---\n",
                    encoding="utf-8")
    _write_item(rv, "approved", _item("rev_d", "deprecate_wiki_page", status="approved",
                                      subject={"node_id": "clm_1", "page": "Claims/clm_1.md"},
                                      proposal={"to_status": "deprecated_candidate", "reason": "x"},
                                      context={"node_type": "claim"}))
    prev = review_read.get_review(rv, "rev_d", graph_db=gdb, wiki_dir=tmp_path / "wiki")["preview"]
    assert prev["apply"]["supported"] is True
    assert prev["apply"]["effect_status"] == review_read.EFFECTED
    assert prev["current_status"] == "deprecated_candidate"


def test_deprecate_in_scope_pending_apply_when_page_not_yet_marked(tmp_path):
    rv = tmp_path / "reviews"
    gdb, conn = _graph_with(tmp_path)
    graph.upsert_node(conn, node_id="cpt_1", node_type="concept", slug="thing", status="active")
    conn.close()
    page = tmp_path / "wiki" / "Concepts" / "thing.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: concept\nstatus: active\nreview_status: none\n---\n", encoding="utf-8")
    _write_item(rv, "approved", _item("rev_d", "deprecate_wiki_page", status="approved",
                                      subject={"node_id": "cpt_1", "page": "Concepts/thing.md"},
                                      proposal={"to_status": "deprecated_candidate", "reason": "x"},
                                      context={"node_type": "concept"}))
    prev = review_read.get_review(rv, "rev_d", graph_db=gdb, wiki_dir=tmp_path / "wiki")["preview"]
    assert prev["apply"]["effect_status"] == review_read.PENDING_APPLY


def test_deprecate_synthesis_page_is_handled_elsewhere(tmp_path):
    rv = tmp_path / "reviews"
    _write_item(rv, "approved", _item("rev_d", "deprecate_wiki_page", status="approved",
                                      subject={"node_id": "syn_1", "page": "Synthesis/syn_1.md"},
                                      proposal={"to_status": "deprecated_candidate"}))
    prev = review_read.get_review(rv, "rev_d", wiki_dir=tmp_path / "wiki")["preview"]
    assert prev["apply"]["supported"] is False
    assert "handled_by_synthesis_executor" in prev["apply"]["warnings"]


def test_deprecate_out_of_scope_page_is_record_only(tmp_path):
    rv = tmp_path / "reviews"
    _write_item(rv, "approved", _item("rev_d", "deprecate_wiki_page", status="approved",
                                      subject={"node_id": "src_1", "page": "Sources/src_1.md"},
                                      proposal={"to_status": "deprecated_candidate"}))
    prev = review_read.get_review(rv, "rev_d", wiki_dir=tmp_path / "wiki")["preview"]
    assert prev["apply"]["supported"] is False
    assert "out_of_scope_for_deprecation_executor" in prev["apply"]["warnings"]


def test_deprecate_approved_page_marked_but_graph_absent_is_unknown(tmp_path):
    # Page is fully marked, but the required graph mirror can't be read -> unknown, never effected.
    rv = tmp_path / "reviews"
    page = tmp_path / "wiki" / "Claims" / "clm_1.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: claim\nstatus: deprecated_candidate\nreview_status: approved\n---\n",
                    encoding="utf-8")
    _write_item(rv, "approved", _item("rev_d", "deprecate_wiki_page", status="approved",
                                      subject={"node_id": "clm_1", "page": "Claims/clm_1.md"},
                                      proposal={"to_status": "deprecated_candidate"},
                                      context={"node_type": "claim"}))
    prev = review_read.get_review(rv, "rev_d", graph_db=tmp_path / "db" / "graph.sqlite",
                                  wiki_dir=tmp_path / "wiki")["preview"]
    assert prev["apply"]["effect_status"] == review_read.UNKNOWN
    assert "graph_unavailable" in prev["apply"]["warnings"]


def test_deprecate_approved_node_missing_is_unknown(tmp_path):
    rv = tmp_path / "reviews"
    gdb, conn = _graph_with(tmp_path)
    conn.close()  # graph exists but the node was never indexed
    page = tmp_path / "wiki" / "Claims" / "clm_1.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: claim\nstatus: deprecated_candidate\nreview_status: approved\n---\n",
                    encoding="utf-8")
    _write_item(rv, "approved", _item("rev_d", "deprecate_wiki_page", status="approved",
                                      subject={"node_id": "clm_1", "page": "Claims/clm_1.md"},
                                      proposal={"to_status": "deprecated_candidate"},
                                      context={"node_type": "claim"}))
    prev = review_read.get_review(rv, "rev_d", graph_db=gdb, wiki_dir=tmp_path / "wiki")["preview"]
    assert prev["apply"]["effect_status"] == review_read.UNKNOWN
    assert "node_missing" in prev["apply"]["warnings"]


def test_rejected_promote_is_no_effect_required(tmp_path):
    rv = tmp_path / "reviews"
    _write_item(rv, "rejected", _item("rev_p", "promote_candidate_node", status="rejected",
                                      subject={"node_id": "cpt_1"}))
    prev = review_read.get_review(rv, "rev_p", wiki_dir=tmp_path / "wiki")["preview"]
    assert prev["apply"]["effect_status"] == review_read.NO_EFFECT_REQUIRED
    assert prev["apply"]["effected"] is False


def test_rejected_in_scope_deprecate_is_no_effect_required(tmp_path):
    rv = tmp_path / "reviews"
    _write_item(rv, "rejected", _item("rev_d", "deprecate_wiki_page", status="rejected",
                                      subject={"node_id": "clm_1", "page": "Claims/clm_1.md"},
                                      proposal={"to_status": "deprecated_candidate"},
                                      context={"node_type": "claim"}))
    prev = review_read.get_review(rv, "rev_d", wiki_dir=tmp_path / "wiki")["preview"]
    assert prev["apply"]["effect_status"] == review_read.NO_EFFECT_REQUIRED


def test_stale_schema_graph_reports_unknown_without_repair(tmp_path, monkeypatch):
    # A schema-mismatched graph must be treated as unavailable (unknown), never initialized/repaired.
    rv = tmp_path / "reviews"
    gdb, conn = _graph_with(tmp_path)
    conn.execute("PRAGMA user_version = 999")  # force a schema-version mismatch
    conn.commit()
    conn.close()
    before = gdb.stat().st_mtime_ns
    monkeypatch.setattr(graph, "init_db", lambda *a, **k: pytest.fail("init_db must not be called"))
    _write_item(rv, "approved", _item("rev_p", "promote_candidate_node", status="approved",
                                      subject={"node_id": "cpt_1"}))
    prev = review_read.get_review(rv, "rev_p", graph_db=gdb, wiki_dir=tmp_path / "wiki")["preview"]
    assert prev["apply"]["effect_status"] == review_read.UNKNOWN
    assert "graph_unavailable" in prev["apply"]["warnings"]
    assert gdb.stat().st_mtime_ns == before  # untouched


def test_get_review_schema_invalid_marks_schema_error(tmp_path):
    rv = tmp_path / "reviews"
    (rv / "pending").mkdir(parents=True)
    (rv / "pending" / "rev_s.json").write_text(json.dumps({"status": "pending"}), encoding="utf-8")
    res = review_read.get_review(rv, "rev_s")
    assert res is not None and res.get("schema_error") is True


def test_preview_traversal_page_is_not_read_outside_wiki(tmp_path):
    # a hostile subject.page must not make the preview read outside wiki/ (containment guard)
    secret = tmp_path / "raw" / "permanent" / "x.md"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("---\nstatus: secret\n---\n", encoding="utf-8")
    rv = tmp_path / "reviews"
    _write_item(rv, "approved", _item("rev_d", "deprecate_wiki_page", status="approved",
                                      subject={"node_id": "clm_1",
                                               "page": "Claims/../../raw/permanent/x.md"},
                                      proposal={"to_status": "deprecated_candidate"}))
    prev = review_read.get_review(rv, "rev_d", wiki_dir=tmp_path / "wiki")["preview"]
    # the out-of-wiki frontmatter was never read -> current_status stays None
    assert prev["current_status"] is None


def test_safe_wiki_subpath_rejects_traversal_and_absolute(tmp_path):
    wiki = tmp_path / "wiki"
    assert review_read._safe_wiki_subpath(wiki, "Claims/../../etc/passwd") is None
    assert review_read._safe_wiki_subpath(wiki, "/etc/passwd") is None
    assert review_read._safe_wiki_subpath(wiki, "Claims/clm_1.md") == (wiki / "Claims" / "clm_1.md").resolve()


def test_preview_paths_are_repository_relative(tmp_path):
    rv = tmp_path / "reviews"
    _write_item(rv, "pending", _item("rev_d", "deprecate_wiki_page",
                                     subject={"node_id": "clm_1", "page": "Claims/clm_1.md"},
                                     proposal={"to_status": "deprecated_candidate"}))
    prev = review_read.get_review(rv, "rev_d", wiki_dir=tmp_path / "wiki")["preview"]
    for p in prev["affected_paths"]:
        assert not Path(p).is_absolute()
        assert str(tmp_path) not in p
