"""ADR-0064 merge-candidate producer: the `/jobs/lint` `merge_candidate` check.

Key-free, deterministic: two ACTIVE items whose title/alias merge keys intersect → one `merge_items`
proposal per absorbed member, starred around one survivor (most live support → most inbound alias
pointers → smallest item_id). Pair-keyed rejection memory (either direction), in-flight freeze,
candidate-side matches report-only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph
from app.workers import items, lint, merge_candidates, merges, reviews
from app.workers.merge_candidates import merge_key
from app.workers.wiki_render import render_item_page

ITEM_TYPE = "method_technique"


def _graph(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    return gdb, graph.connect(gdb)


def _item(tmp_path, conn, name, *, status="active", aliases=(), sources=()):
    nid = items.node_id(name)
    graph.upsert_node(conn, node_id=nid, node_type="item", slug=items._slug(name), status=status,
                      item_type=ITEM_TYPE)
    for sid in sources:
        graph.upsert_node(conn, node_id=sid, node_type="source", slug=sid, status="active")
        graph.upsert_assertion(conn, src_id=sid, dst_id=nid, edge_type="mentions",
                               asserted_by="llm", status="active")
    page = tmp_path / "wiki" / "Items" / f"{items._slug(name)}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(render_item_page({
        "node_id": nid, "item_type": ITEM_TYPE, "title": name, "aliases": list(aliases),
        "confidence": "low", "source_ids": graph.sources_for_node(conn, nid), "status": status,
        "duplicates": [],
    }), encoding="utf-8")
    conn.commit()
    return nid


def _src(n):
    return f"src_{n:016x}"


def _lint(tmp_path, gdb, **kw):
    return lint.run_lint(tmp_path, graph_db=gdb, record_job=False, **kw)


def _merge_items(tmp_path, state="pending"):
    d = tmp_path / "reviews" / state
    out = [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.exists() else []
    return [i for i in out if i["type"] == "merge_items"]


def _pairs(tmp_path, state="pending"):
    return sorted((i["subject"]["survivor_node_id"], i["subject"]["absorbed_node_id"])
                  for i in _merge_items(tmp_path, state))


# --- merge_key fold (decision 3) -------------------------------------------


def test_merge_key_conservative_fold():
    assert merge_key("Large Language Models") == merge_key("large  language model")
    assert merge_key("LLMs") == "llm"
    assert merge_key("McKinsey & Company") == merge_key("mckinsey and company")
    assert merge_key("Multi-agent  Systems") == "multi agent system"
    assert merge_key("Analysis") == "analysis"          # -is kept
    assert merge_key("Business") == "business"          # -ss kept
    assert merge_key("Status") == "status"              # -us kept
    assert merge_key("Bus") == "bus"                    # < 4 chars kept
    assert merge_key("Models of Thought") == "models of thought"   # only the LAST token folds
    assert merge_key("Theory-of-Mind") == "theory of mind"
    assert merge_key("Ｆｕｌｌwidth") == "fullwidth"     # NFKC
    assert merge_key("McKinsey's Report") == "mckinseys report"
    assert merge_key("") == "" and merge_key("!!!") == "" and merge_key(None) == ""


# --- filing (decisions 1, 2, 5) --------------------------------------------


def test_alias_cross_match_files_merge_with_most_supported_survivor(tmp_path):
    gdb, conn = _graph(tmp_path)
    agentic = _item(tmp_path, conn, "Agentic AI", sources=[_src(1), _src(2), _src(3)])
    agents = _item(tmp_path, conn, "AI Agents and Multi-agent Systems", aliases=["agentic AI"],
                   sources=[_src(4)])
    _item(tmp_path, conn, "Unrelated", sources=[_src(1)])
    conn.close()
    res = _lint(tmp_path, gdb)
    assert _pairs(tmp_path) == [(agentic, agents)]
    item = _merge_items(tmp_path)[0]
    assert item["proposal"]["to_status"] == "merged"
    assert item["proposal"]["reason_code"] == "alias_cross_match"
    assert item["proposal"]["matched_keys"] == ["agentic ai"]
    assert item["context"]["survivor"]["support"] == 3
    assert item["context"]["match_evidence"] == [
        {"key": "agentic ai", "survivor_field": ["title"], "absorbed_field": ["alias:agentic AI"]}]
    mc = [f for f in res["findings"] if f["check"] == "merge_candidate"]
    assert len(mc) == 1 and mc[0]["severity"] == "low" and mc[0]["subject"] == agents
    assert mc[0]["data"]["remediation"] == "decide_merge"
    assert item["review_id"] in res["review_items_filed"]


def test_variant_fold_matches_plural_and_ampersand(tmp_path):
    gdb, conn = _graph(tmp_path)
    llm = _item(tmp_path, conn, "Large Language Models", sources=[_src(1), _src(2)])
    llm2 = _item(tmp_path, conn, "LLM", aliases=["large language model"], sources=[_src(3)])
    mck = _item(tmp_path, conn, "McKinsey & Company", sources=[_src(1), _src(2)])
    mck2 = _item(tmp_path, conn, "Consultancy", aliases=["McKinsey and Company"], sources=[_src(3)])
    conn.close()
    _lint(tmp_path, gdb)
    assert _pairs(tmp_path) == sorted([(llm, llm2), (mck, mck2)])


def test_survivor_tie_breaks_on_inbound_alias_pointer_then_id(tmp_path):
    gdb, conn = _graph(tmp_path)
    # Equal support; only "McKinsey & Company" carries the other's title as an alias, so
    # "McKinsey" has 1 inbound pointer and survives.
    mck = _item(tmp_path, conn, "McKinsey", sources=[_src(1)])
    full = _item(tmp_path, conn, "McKinsey & Company", aliases=["McKinsey"], sources=[_src(2)])
    conn.close()
    _lint(tmp_path, gdb)
    assert _pairs(tmp_path) == [(mck, full)]


def test_survivor_tie_both_pointing_falls_back_to_smallest_id(tmp_path):
    gdb, conn = _graph(tmp_path)
    x = _item(tmp_path, conn, "McKinsey", aliases=["McKinsey & Company"], sources=[_src(1)])
    y = _item(tmp_path, conn, "McKinsey & Company", aliases=["McKinsey"], sources=[_src(2)])
    conn.close()
    _lint(tmp_path, gdb)
    assert _pairs(tmp_path) == [(min(x, y), max(x, y))]


def test_cluster_files_star_never_chain(tmp_path):
    gdb, conn = _graph(tmp_path)
    a = _item(tmp_path, conn, "Alpha", aliases=["Beta", "Gamma"], sources=[_src(1), _src(2), _src(3)])
    b = _item(tmp_path, conn, "Beta", aliases=["Gamma"], sources=[_src(4)])
    c = _item(tmp_path, conn, "Gamma", sources=[_src(5)])
    conn.close()
    _lint(tmp_path, gdb)
    assert _pairs(tmp_path) == sorted([(a, b), (a, c)])      # both absorbed into ONE survivor
    assert all(i["context"]["component_size"] == 3 for i in _merge_items(tmp_path))


def test_indirect_member_waits_for_the_alias_union(tmp_path):
    # A~B (alias "Beta") and B~C (alias "Gamma") but A and C share NO key: only the direct A<-B is
    # filed — no transitive identity guess. After A<-B applies, A carries B's aliases, so the next run
    # proposes A<-C with real A–C evidence.
    gdb, conn = _graph(tmp_path)
    a = _item(tmp_path, conn, "Alpha", aliases=["Beta"], sources=[_src(1), _src(2), _src(3)])
    b = _item(tmp_path, conn, "Beta", aliases=["Gamma"], sources=[_src(4)])
    c = _item(tmp_path, conn, "Gamma", sources=[_src(5)])
    _lint(tmp_path, gdb)
    assert _pairs(tmp_path) == [(a, b)]
    assert _merge_items(tmp_path)[0]["proposal"]["matched_keys"] == ["beta"]
    reviews.resolve_review_item(tmp_path / "reviews", _merge_items(tmp_path)[0]["review_id"],
                                decision="approved", decided_by="human")
    assert merges.apply_merges(conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")["applied"] == 1
    conn.close()
    _lint(tmp_path, gdb)
    assert _pairs(tmp_path) == [(a, c)]
    follow_up = _merge_items(tmp_path)[0]
    assert follow_up["proposal"]["matched_keys"] == ["gamma"]
    assert follow_up["context"]["match_evidence"] == [
        {"key": "gamma", "survivor_field": ["alias:Gamma"], "absorbed_field": ["title"]}]


def test_rerun_is_idempotent_and_reports_existing(tmp_path):
    gdb, conn = _graph(tmp_path)
    _item(tmp_path, conn, "Agentic AI", sources=[_src(1), _src(2)])
    _item(tmp_path, conn, "AI Agents", aliases=["agentic AI"], sources=[_src(3)])
    conn.close()
    first = _lint(tmp_path, gdb)
    snapshot = [p.read_bytes() for p in sorted((tmp_path / "reviews" / "pending").glob("*.json"))]
    second = _lint(tmp_path, gdb)
    assert second["review_items_filed"] == []
    assert first["review_items_filed"][0] in second["review_items_existing"]
    assert [p.read_bytes() for p in sorted((tmp_path / "reviews" / "pending").glob("*.json"))] \
        == snapshot
    assert [f["check"] for f in second["findings"]].count("merge_candidate") == 1


def test_proposals_are_byte_stable(tmp_path):
    gdb, conn = _graph(tmp_path)
    _item(tmp_path, conn, "Alpha", aliases=["Beta"], sources=[_src(1)])
    _item(tmp_path, conn, "Beta", aliases=["Gamma"], sources=[_src(2)])
    _item(tmp_path, conn, "Gamma", sources=[_src(3)])
    kw = {"wiki_dir": tmp_path / "wiki", "reviews_dir": tmp_path / "reviews"}
    one = merge_candidates.find_merge_candidates(conn, **kw)
    two = merge_candidates.find_merge_candidates(conn, **kw)
    conn.close()
    assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)


def test_file_review_items_false_reports_without_filing(tmp_path):
    gdb, conn = _graph(tmp_path)
    _item(tmp_path, conn, "Agentic AI", sources=[_src(1), _src(2)])
    _item(tmp_path, conn, "AI Agents", aliases=["agentic AI"], sources=[_src(3)])
    conn.close()
    res = _lint(tmp_path, gdb, file_review_items=False)
    assert any(f["check"] == "merge_candidate" for f in res["findings"])
    assert _merge_items(tmp_path) == []


def test_merge_candidates_never_fail_lint(tmp_path):
    gdb, conn = _graph(tmp_path)
    _item(tmp_path, conn, "Agentic AI", sources=[_src(1), _src(2)])
    _item(tmp_path, conn, "AI Agents", aliases=["agentic AI"], sources=[_src(3), _src(4)])
    conn.close()
    res = _lint(tmp_path, gdb)
    assert res["by_check"].get("merge_candidate") == 1
    assert all(f["severity"] != "high" for f in res["findings"])


# --- lifecycle scope (decision 4) ------------------------------------------


def test_candidate_side_is_report_only(tmp_path):
    gdb, conn = _graph(tmp_path)
    a = _item(tmp_path, conn, "Agentic AI", sources=[_src(1), _src(2)])
    b = _item(tmp_path, conn, "AI Agents", status="candidate", aliases=["agentic AI"],
              sources=[_src(3)])
    conn.close()
    res = _lint(tmp_path, gdb)
    assert _merge_items(tmp_path) == []
    up = [f for f in res["findings"] if f["check"] == "merge_candidate_unpromoted"]
    assert len(up) == 1 and sorted(up[0]["data"]["node_ids"]) == sorted([a, b])
    assert up[0]["severity"] == "low"


def test_merged_and_deprecated_items_are_ignored(tmp_path):
    gdb, conn = _graph(tmp_path)
    _item(tmp_path, conn, "Agentic AI", sources=[_src(1)])
    _item(tmp_path, conn, "AI Agents", status="merged", aliases=["agentic AI"])
    _item(tmp_path, conn, "Agent Systems", status="deprecated_candidate", aliases=["agentic AI"])
    conn.close()
    res = _lint(tmp_path, gdb)
    assert _merge_items(tmp_path) == []
    assert not any(f["check"].startswith("merge_candidate") for f in res["findings"])


def test_item_without_readable_page_is_skipped(tmp_path):
    gdb, conn = _graph(tmp_path)
    _item(tmp_path, conn, "Agentic AI", sources=[_src(1)])
    b = _item(tmp_path, conn, "AI Agents", aliases=["agentic AI"], sources=[_src(2)])
    (tmp_path / "wiki" / "Items" / "ai-agents.md").unlink()
    conn.close()
    _lint(tmp_path, gdb)
    assert _merge_items(tmp_path) == []
    assert graph.get_node(graph.connect(gdb), b)["status"] == "active"


# --- rejection memory + in-flight (decisions 6–8) --------------------------


def test_rejected_pair_is_suppressed_in_both_directions(tmp_path):
    gdb, conn = _graph(tmp_path)
    tom = _item(tmp_path, conn, "Theory of Mind in AI", aliases=["agent phenomenology"],
                sources=[_src(1), _src(2)])
    phen = _item(tmp_path, conn, "Agent Phenomenology", sources=[_src(3)])
    _lint(tmp_path, gdb)
    rid = _merge_items(tmp_path)[0]["review_id"]
    assert _pairs(tmp_path) == [(tom, phen)]
    reviews.resolve_review_item(tmp_path / "reviews", rid, decision="rejected",
                                decided_by="human")
    # Support flips — the survivor rule would now pick the other direction.
    for n in (4, 5, 6):
        graph.upsert_node(conn, node_id=_src(n), node_type="source", slug=_src(n), status="active")
        graph.upsert_assertion(conn, src_id=_src(n), dst_id=phen, edge_type="mentions",
                               asserted_by="llm", status="active")
    conn.commit()
    conn.close()
    res = _lint(tmp_path, gdb)
    assert _merge_items(tmp_path) == [] and res["review_items_filed"] == []
    assert not any(f["check"] == "merge_candidate" for f in res["findings"])


def test_suppressed_pair_is_cut_before_clustering(tmp_path):
    gdb, conn = _graph(tmp_path)
    a = _item(tmp_path, conn, "Alpha", aliases=["Beta"], sources=[_src(1), _src(2), _src(3)])
    b = _item(tmp_path, conn, "Beta", aliases=["Gamma"], sources=[_src(4)])
    c = _item(tmp_path, conn, "Gamma", sources=[_src(5), _src(6)])
    conn.close()
    reviews.create_review_item(tmp_path / "reviews", review_type="merge_items",
                               subject={"survivor_node_id": a, "absorbed_node_id": b},
                               proposal={"to_status": "merged"})
    rid = reviews.review_id("merge_items", {"survivor_node_id": a, "absorbed_node_id": b})
    reviews.resolve_review_item(tmp_path / "reviews", rid, decision="rejected", decided_by="human")
    _lint(tmp_path, gdb)
    # The rejected A~B link no longer bridges A into {B, C}; C (more support) absorbs B.
    assert _pairs(tmp_path) == [(c, b)]


def test_in_flight_nodes_are_frozen_until_decided(tmp_path):
    gdb, conn = _graph(tmp_path)
    a = _item(tmp_path, conn, "Alpha", aliases=["Beta"], sources=[_src(1), _src(2)])
    b = _item(tmp_path, conn, "Beta", sources=[_src(3)])
    _lint(tmp_path, gdb)
    assert _pairs(tmp_path) == [(a, b)]
    _item(tmp_path, conn, "Beta Prime", aliases=["beta"], sources=[_src(4)])
    conn.close()
    res = _lint(tmp_path, gdb)
    assert _pairs(tmp_path) == [(a, b)]        # nothing stacks on the undecided proposal
    merge_rids = {i["review_id"] for i in _merge_items(tmp_path)}
    assert not merge_rids & set(res["review_items_filed"])


def test_human_filed_pending_items_are_not_reported_as_machine_findings(tmp_path):
    gdb, conn = _graph(tmp_path)
    a = _item(tmp_path, conn, "Alpha", sources=[_src(1)])
    b = _item(tmp_path, conn, "Omega", sources=[_src(2)])
    conn.close()
    reviews.create_review_item(tmp_path / "reviews", review_type="merge_items",
                               subject={"survivor_node_id": a, "absorbed_node_id": b},
                               proposal={"to_status": "merged", "reason_code": "human_filed"})
    res = _lint(tmp_path, gdb)
    assert not any(f["check"] == "merge_candidate" for f in res["findings"])


# --- end to end: lint proposal → approve → existing executor ---------------


def test_filed_proposal_applies_through_the_merge_executor(tmp_path):
    gdb, conn = _graph(tmp_path)
    a = _item(tmp_path, conn, "Agentic AI", sources=[_src(1), _src(2)])
    b = _item(tmp_path, conn, "AI Agents", aliases=["agentic AI"], sources=[_src(3)])
    _lint(tmp_path, gdb)
    rid = _merge_items(tmp_path)[0]["review_id"]
    reviews.resolve_review_item(tmp_path / "reviews", rid, decision="approved", decided_by="human")
    res = merges.apply_merges(conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")
    assert res["applied"] == 1 and res["skipped"] == []
    assert graph.get_node(conn, b)["status"] == "merged"
    assert sorted(graph.sources_for_node(conn, a)) == [_src(1), _src(2), _src(3)]
    conn.close()
    # The absorbed node is now a tombstone: nothing is proposed again.
    assert _lint(tmp_path, gdb)["review_items_filed"] == []


# --- key budget (review round) ---------------------------------------------


def test_generic_key_is_ignored_and_reported_once(tmp_path):
    gdb, conn = _graph(tmp_path)
    for n in range(6):                                       # 6 > default budget of 5
        _item(tmp_path, conn, f"Thing {n}", aliases=["AI"], sources=[_src(n + 1)])
    conn.close()
    res = _lint(tmp_path, gdb)
    assert _merge_items(tmp_path) == []
    common = [f for f in res["findings"] if f["check"] == "merge_key_too_common"]
    assert len(common) == 1 and common[0]["data"] == {"key": "ai", "active_items": 6}


def test_key_budget_reads_policy(tmp_path):
    gdb, conn = _graph(tmp_path)
    for n in range(6):
        _item(tmp_path, conn, f"Thing {n}", aliases=["AI"], sources=[_src(n + 1)])
    conn.close()
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "review.yaml").write_text(
        "merge_candidates:\n  max_items_per_key: 6\n", encoding="utf-8")
    _lint(tmp_path, gdb)
    assert len(_merge_items(tmp_path)) == 5                  # one star, now under budget
    assert lint._merge_key_budget(tmp_path / "missing.yaml") == 5
    (tmp_path / "policies" / "review.yaml").write_text(
        "merge_candidates:\n  max_items_per_key: 1\n", encoding="utf-8")
    assert lint._merge_key_budget(tmp_path / "policies" / "review.yaml") == 5   # < 2 -> default


def test_repo_policy_carries_the_budget():
    assert lint._merge_key_budget(ROOT / "policies" / "review.yaml") == 5
    assert "max_items_per_key" in (ROOT / "policies" / "review.yaml").read_text(encoding="utf-8")


def test_benchmark_many_items_sharing_one_alias_is_bounded(tmp_path):
    import time
    gdb, conn = _graph(tmp_path)
    for n in range(1000):
        _item(tmp_path, conn, f"Topic {n}", aliases=["artificial intelligence"])
    t0 = time.perf_counter()
    res = merge_candidates.find_merge_candidates(
        conn, wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews")
    elapsed = time.perf_counter() - t0
    conn.close()
    assert res["proposals"] == [] and res["unpromoted"] == []
    assert res["too_common"] == [{"key": "artificial intelligence", "active_items": 1000}]
    assert elapsed < 10.0
