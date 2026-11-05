"""ADR-0072 decisions 2-3: per-review Apply outcomes from the identity / lifecycle executors —
merge_items, change_item_type, split_item, promote_candidate_node.

Each executor reports exactly ONE outcome per approved review of its type (re-runs included), only
claims `already_in_effect` after checking the postcondition, reports `incomplete` when an earlier run
already changed state, and carries the display names it read BEFORE mutating.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph  # noqa: E402
from app.workers import (  # noqa: E402
    apply_outcomes,
    identity_journal,
    item_names,
    item_pages,
    items,
    merges,
    promote,
    retypes,
    reviews,
    splits,
    tag_links,
)
from app.workers.wiki_render import NODE_DIR, render_item_page  # noqa: E402
from tests import test_merge as tm  # noqa: E402
from tests import test_retype as tr  # noqa: E402
from tests import test_split as ts  # noqa: E402
from tests.tagging import close_journals  # noqa: E402


def _one_each(res, rids, rtype):
    """Exactly one outcome per review id, all of `rtype`, and counts consistent with them."""
    outs = res["outcomes"]
    assert sorted(o["review_id"] for o in outs) == sorted(rids)
    assert all(o["type"] == rtype and o["outcome"] in apply_outcomes.OUTCOMES for o in outs)
    return {o["review_id"]: o for o in outs}


def _counts_match(res, *, applied_key="applied", legacy_skips_only=True):
    c = apply_outcomes.counts(res["outcomes"])
    assert res[applied_key] == c[apply_outcomes.APPLIED]
    if legacy_skips_only:
        assert len(res["skipped"]) == c[apply_outcomes.SKIPPED]


def _reown(page, nid):
    """Make an item page name another owner (ADR-0070 B1 ownership refusal)."""
    text = page.read_text()
    assert f'item_id: "{nid}"' in text
    page.write_text(text.replace(f'item_id: "{nid}"', f'item_id: "{items.node_id("Z")}"'))


# --- merge_items ----------------------------------------------------------------------------------


def _merges(tmp_path, conn, *, close=True):
    res = merges.apply_merges(conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")
    return close_journals(tmp_path / "reviews", res) if close else res


def test_merge_applied_then_already_in_effect_with_names_captured_before(tmp_path):
    conn = tm._graph(tmp_path)
    tm._item(tmp_path, conn, tm.A, "Alpha")
    b_slug = tm._item(tmp_path, conn, tm.B, "Beta")
    tm._approve(tmp_path)
    first = _merges(tmp_path, conn)
    (o,) = _one_each(first, ["rev_m"], "merge_items").values()
    assert o["outcome"] == "applied" and "reason" not in o
    assert o["details"]["survivor"] == {"id": tm.A, "name": "Alpha"}
    assert o["details"]["absorbed"] == {"id": tm.B, "name": "Beta"}
    assert o["details"]["subject"] == o["details"]["absorbed"]
    _counts_match(first)

    again = _merges(tmp_path, conn)                         # re-run: postcondition verified
    (o2,) = _one_each(again, ["rev_m"], "merge_items").values()
    assert o2["outcome"] == "already_in_effect" and again["applied"] == 0
    assert o2["details"]["absorbed"]["name"] == "Beta"
    _counts_match(again)

    # the absorbed tombstone page disappears: the result still names it from the earlier capture,
    # and a further run can no longer verify the merge -> a skip naming the id (never a blank).
    (tmp_path / "wiki" / "Items" / f"{b_slug}.md").unlink()
    assert o2["details"]["absorbed"]["name"] == "Beta"
    third = _merges(tmp_path, conn)
    (o3,) = _one_each(third, ["rev_m"], "merge_items").values()
    assert o3["outcome"] == "skipped" and o3["reason"] == "merge_unverified"
    assert o3["details"]["absorbed"] == {"id": tm.B, "name": tm.B}
    assert third["skipped"] == []                           # documented: not in the legacy list
    conn.close()


def test_merge_skips_carry_typed_reasons_one_each(tmp_path):
    conn = tm._graph(tmp_path)
    tm._item(tmp_path, conn, tm.A, "Alpha")
    tm._item(tmp_path, conn, tm.B, "Beta")
    tm._approve(tmp_path, rid="rev_self", survivor=tm.A, absorbed=tm.A)
    tm._approve(tmp_path, rid="rev_missing", survivor=tm.A, absorbed=items.node_id("Nope"))
    tm._approve(tmp_path, rid="rev_bad", survivor="../x", absorbed=tm.B)
    res = _merges(tmp_path, conn)
    outs = _one_each(res, ["rev_self", "rev_missing", "rev_bad"], "merge_items")
    assert {k: (v["outcome"], v["reason"]) for k, v in outs.items()} == {
        "rev_self": ("skipped", "self_merge"), "rev_missing": ("skipped", "node_missing"),
        "rev_bad": ("skipped", "invalid_subject")}
    assert outs["rev_missing"]["details"]["survivor"] == {"id": tm.A, "name": "Alpha"}
    assert outs["rev_missing"]["details"]["absorbed"]["name"] == items.node_id("Nope")  # id stands in
    assert sorted(s["reason"] for s in res["skipped"]) == sorted(
        v["reason"] for v in outs.values())
    _counts_match(res)
    conn.close()


def test_merge_ownership_refusal_carries_the_problem(tmp_path):
    conn = tm._graph(tmp_path)
    tm._item(tmp_path, conn, tm.A, "Alpha")
    b_slug = tm._item(tmp_path, conn, tm.B, "Beta")
    page = tmp_path / "wiki" / "Items" / f"{b_slug}.md"        # B's page now names another owner
    _reown(page, tm.B)
    tm._approve(tmp_path)
    res = _merges(tmp_path, conn)
    (o,) = _one_each(res, ["rev_m"], "merge_items").values()
    assert o["outcome"] == "skipped" and o["reason"] == item_pages.OWNED_BY_OTHER
    assert o["details"]["problem"]["reason"] == item_pages.OWNED_BY_OTHER
    assert o["details"]["problem"]["node_id"] == tm.B
    _counts_match(res)
    conn.close()


def test_interrupted_merge_refused_on_resume_is_incomplete_then_applied(tmp_path, monkeypatch):
    conn = tm._graph(tmp_path)
    tm._item(tmp_path, conn, tm.A, "Alpha")
    tm._item(tmp_path, conn, tm.B, "Beta")
    tm._approve(tmp_path)

    def _crash(*a, **k):
        raise RuntimeError("crash after the journal")
    monkeypatch.setattr(tag_links, "move_merge_decisions", _crash)
    with pytest.raises(RuntimeError):
        _merges(tmp_path, conn)
    monkeypatch.undo()
    assert identity_journal.is_open(tmp_path / "reviews", "rev_m")

    conn.execute("UPDATE nodes SET status = 'archived' WHERE node_id = ?", (tm.A,))
    conn.commit()
    res = _merges(tmp_path, conn)
    (o,) = _one_each(res, ["rev_m"], "merge_items").values()
    assert o["outcome"] == "incomplete" and o["reason"] == "survivor_not_active"
    assert o["details"]["stage"] == "started"
    assert res["skipped"] == [{"review_id": "rev_m", "reason": "survivor_not_active"}]
    assert apply_outcomes.counts(res["outcomes"])["skipped"] == 0   # documented difference

    conn.execute("UPDATE nodes SET status = 'active' WHERE node_id = ?", (tm.A,))
    conn.commit()
    done = _merges(tmp_path, conn)
    (o2,) = _one_each(done, ["rev_m"], "merge_items").values()
    assert o2["outcome"] == "applied" and o2["details"]["resumed"] is True
    _counts_match(done)
    conn.close()


# --- change_item_type -----------------------------------------------------------------------------


def _retypes(tmp_path, conn):
    return retypes.apply_retypes(conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")


def test_retype_applied_then_already_in_effect_with_before_after(tmp_path):
    conn = tr._graph(tmp_path)
    tr._node(tmp_path, conn, tr.ITEM, "Kubernetes", "method_technique", status="active")
    tr._approve(tmp_path)
    res = _retypes(tmp_path, conn)
    (o,) = _one_each(res, ["rev_rt"], "change_item_type").values()
    assert o["outcome"] == "applied"
    assert o["details"]["subject"] == {"id": tr.ITEM, "name": "Kubernetes"}
    assert o["details"]["before"] == {"item_type": "method_technique"}
    assert o["details"]["after"] == {"item_type": "infrastructure_hardware"}
    _counts_match(res)

    again = _retypes(tmp_path, conn)
    (o2,) = _one_each(again, ["rev_rt"], "change_item_type").values()
    assert o2["outcome"] == "already_in_effect"
    assert o2["details"]["before"] == o2["details"]["after"] == {"item_type": "infrastructure_hardware"}
    _counts_match(again)

    # page at the target type but the graph mirror drifted: not verified, and apply doesn't act
    conn.execute("UPDATE nodes SET item_type = 'method_technique' WHERE node_id = ?", (tr.ITEM,))
    conn.commit()
    drift = _retypes(tmp_path, conn)
    (o3,) = _one_each(drift, ["rev_rt"], "change_item_type").values()
    assert o3["outcome"] == "skipped" and o3["reason"] == "graph_item_type_mismatch"
    assert drift["skipped"] == []                           # documented: not in the legacy list
    conn.close()


def test_retype_skips_one_each_with_problem(tmp_path):
    conn = tr._graph(tmp_path)
    slug = tr._node(tmp_path, conn, tr.ITEM, "Kubernetes", "method_technique", status="active")
    page = tmp_path / "wiki" / NODE_DIR["item"] / f"{slug}.md"
    _reown(page, tr.ITEM)
    tr._approve(tmp_path, rid="rev_owner")
    tr._approve(tmp_path, rid="rev_sentinel", to_type="unclassified_review_required")
    tr._approve(tmp_path, rid="rev_missing", node_id=items.node_id("Gone"))
    res = _retypes(tmp_path, conn)
    outs = _one_each(res, ["rev_owner", "rev_sentinel", "rev_missing"], "change_item_type")
    assert {k: v["reason"] for k, v in outs.items()} == {
        "rev_owner": item_pages.OWNED_BY_OTHER, "rev_sentinel": "invalid_to_item_type",
        "rev_missing": "node_missing"}
    assert all(v["outcome"] == "skipped" for v in outs.values())
    assert outs["rev_owner"]["details"]["problem"]["reason"] == item_pages.OWNED_BY_OTHER
    assert "problem" not in outs["rev_missing"]["details"]
    assert outs["rev_missing"]["details"]["subject"] == {"id": items.node_id("Gone"),
                                                         "name": items.node_id("Gone")}
    _counts_match(res)
    conn.close()


# --- split_item -----------------------------------------------------------------------------------


def _splits(tmp_path, conn):
    return close_journals(tmp_path / "reviews", splits.apply_splits(
        conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki"))


def test_split_applied_then_already_in_effect(tmp_path):
    conn = ts._graph(tmp_path)
    ts._full(tmp_path, conn)
    ts._approve(tmp_path, sources=[ts.S2, ts.S3])
    res = _splits(tmp_path, conn)
    (o,) = _one_each(res, ["rev_s"], "split_item").values()
    assert o["outcome"] == "applied"
    assert o["details"]["subject"] == {"id": ts.A, "name": "Washington"}
    assert o["details"]["spinoff"] == {"id": ts.B, "name": ts.SPINOFF}
    assert o["details"]["documents"] == 2
    _counts_match(res)

    again = _splits(tmp_path, conn)                         # lineage + partition on B, A keeps S1
    (o2,) = _one_each(again, ["rev_s"], "split_item").values()
    assert o2["outcome"] == "already_in_effect" and again["applied"] == 0
    assert o2["details"]["spinoff"]["name"] == ts.SPINOFF
    _counts_match(again)
    conn.close()


def test_split_ambiguous_half_state_is_incomplete(tmp_path):
    conn = ts._graph(tmp_path)
    ts._full(tmp_path, conn)
    ts._approve(tmp_path, sources=[ts.S3])
    _splits(tmp_path, conn)
    ts._mention(conn, ts.S3, ts.A)                          # S3 now on BOTH A and B
    res = _splits(tmp_path, conn)
    (o,) = _one_each(res, ["rev_s"], "split_item").values()
    assert o["outcome"] == "incomplete" and o["reason"] == "partial_split_state"
    assert res["skipped"] == [{"review_id": "rev_s", "reason": "partial_split_state"}]  # unchanged
    conn.close()


def test_split_name_taken_is_skipped_with_its_problem(tmp_path):
    conn = ts._graph(tmp_path)
    ts._full(tmp_path, conn)
    other = items.node_id("Someone Else")
    ts._entity(tmp_path, conn, other, ts.SPINOFF, slug="someone-else")   # holds the spin-off's title
    ts._approve(tmp_path, sources=[ts.S3])
    ts._approve(tmp_path, rid="rev_bad", a=items.node_id("Elsewhere"), spinoff_name="")
    res = _splits(tmp_path, conn)
    outs = _one_each(res, ["rev_s", "rev_bad"], "split_item")
    assert outs["rev_s"]["outcome"] == "skipped"
    assert outs["rev_s"]["reason"] == item_names.NAME_TAKEN
    assert outs["rev_s"]["details"]["problem"]["conflict_node_id"] == other
    legacy = next(s for s in res["skipped"] if s["review_id"] == "rev_s")
    assert legacy["problem"]["conflict_node_id"] == other  # the legacy entry keeps its problem
    assert outs["rev_bad"]["reason"] == "invalid_proposal"
    assert graph.get_node(conn, ts.B) is None
    _counts_match(res)
    conn.close()


# --- promote_candidate_node -----------------------------------------------------------------------


NID = items.node_id("Old Name")


def _candidate(tmp_path, conn, nid, title, *, status="candidate", item_type="method_technique"):
    slug = items._slug(title)
    graph.upsert_node(conn, node_id=nid, node_type="item", slug=slug, status=status,
                      item_type=item_type)
    page = tmp_path / "wiki" / "Items" / f"{slug}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(render_item_page({
        "node_id": nid, "item_type": item_type, "title": title, "aliases": [],
        "confidence": "low", "source_ids": [], "status": status, "duplicates": []}),
        encoding="utf-8")
    conn.commit()
    return slug


def _approve_promotion(tmp_path, nid, *, amendments=None, rid=None):
    rid = rid or reviews.review_id("promote_candidate_node", {"node_id": nid})
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": "promote_candidate_node", "status": "approved",
        "subject": {"node_id": nid}, "proposal": {"to_status": "active"},
        **({"amendments": amendments} if amendments else {})}), encoding="utf-8")
    return rid


def _promote(tmp_path):
    (tmp_path / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    return promote.promote_candidates(tmp_path, rebuild_index=False, record_job=False)


def _setup_graph(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    return graph.connect(gdb)


def test_renamed_promotion_carries_before_after_then_already_in_effect(tmp_path):
    conn = _setup_graph(tmp_path)
    _candidate(tmp_path, conn, NID, "Old Name")
    conn.close()
    rid = _approve_promotion(tmp_path, NID, amendments={"title": "New <Name>",
                                                        "item_type": "model"})
    res = _promote(tmp_path)
    (o,) = _one_each(res, [rid], "promote_candidate_node").values()
    assert o["outcome"] == "applied"
    assert o["details"]["subject"] == {"id": NID, "name": "Old Name"}      # read before the rename
    assert o["details"]["before"] == {"title": "Old Name"}
    assert o["details"]["after"] == {"title": "New <Name>", "item_type": "model"}
    _counts_match(res, applied_key="promoted")

    again = _promote(tmp_path)                              # node active: the loop never visits it
    (o2,) = _one_each(again, [rid], "promote_candidate_node").values()
    assert o2["outcome"] == "already_in_effect" and again["promoted"] == 0
    assert o2["details"]["subject"] == {"id": NID, "name": "New <Name>"}
    _counts_match(again, applied_key="promoted")


def test_unrenamed_promotion_has_no_title_change(tmp_path):
    conn = _setup_graph(tmp_path)
    _candidate(tmp_path, conn, NID, "Old Name")
    conn.close()
    rid = _approve_promotion(tmp_path, NID)
    (o,) = _one_each(_promote(tmp_path), [rid], "promote_candidate_node").values()
    assert o["outcome"] == "applied" and "before" not in o["details"]
    assert o["details"]["after"] == {"item_type": "method_technique"}


def test_promotion_skips_one_each(tmp_path):
    conn = _setup_graph(tmp_path)
    taken = items.node_id("Taken")
    _candidate(tmp_path, conn, taken, "Taken", status="active")
    _candidate(tmp_path, conn, NID, "Old Name")
    conn.close()
    r_taken = _approve_promotion(tmp_path, NID, amendments={"title": "Taken"})
    r_gone = _approve_promotion(tmp_path, items.node_id("Gone"))
    r_odd = _approve_promotion(tmp_path, NID, rid="rev_not_canonical")   # loop never visits it
    res = _promote(tmp_path)
    outs = _one_each(res, [r_taken, r_gone, r_odd], "promote_candidate_node")
    assert {k: (v["outcome"], v["reason"]) for k, v in outs.items()} == {
        r_taken: ("skipped", item_names.NAME_TAKEN),
        r_gone: ("skipped", "node_missing"),
        r_odd: ("skipped", "review_id_mismatch")}
    assert outs[r_taken]["details"]["problem"]["conflict_node_id"] == taken
    assert outs[r_taken]["details"]["subject"] == {"id": NID, "name": "Old Name"}
    assert outs[r_gone]["details"]["subject"]["name"] == items.node_id("Gone")
    assert res["skipped"] == [{"review_id": r_taken, "reason": item_names.NAME_TAKEN,
                               "problem": outs[r_taken]["details"]["problem"]}]
    _counts_match(res, applied_key="promoted", legacy_skips_only=False)  # unvisited: not legacy


def test_promotion_page_and_status_skips(tmp_path):
    conn = _setup_graph(tmp_path)
    sentinel = items.node_id("Sentinel")
    _candidate(tmp_path, conn, sentinel, "Sentinel", item_type="unclassified_review_required")
    nopage = items.node_id("No Page")
    no_slug = _candidate(tmp_path, conn, nopage, "No Page")
    (tmp_path / "wiki" / "Items" / f"{no_slug}.md").unlink()
    gone_page = items.node_id("Active No Page")
    gp_slug = _candidate(tmp_path, conn, gone_page, "Active No Page", status="active")
    (tmp_path / "wiki" / "Items" / f"{gp_slug}.md").unlink()
    retired = items.node_id("Retired")
    _candidate(tmp_path, conn, retired, "Retired", status="archived")
    conn.close()
    r_sentinel = _approve_promotion(tmp_path, sentinel)
    r_nopage = _approve_promotion(tmp_path, nopage)
    r_gone_page = _approve_promotion(tmp_path, gone_page)
    r_retired = _approve_promotion(tmp_path, retired)
    res = _promote(tmp_path)
    outs = _one_each(res, [r_sentinel, r_nopage, r_gone_page, r_retired], "promote_candidate_node")
    assert {k: (v["outcome"], v["reason"]) for k, v in outs.items()} == {
        r_sentinel: ("skipped", "missing_required_item_type"),
        r_nopage: ("skipped", "page_missing"),           # the candidate loop's silent skip
        r_gone_page: ("skipped", "page_missing"),        # active, but no page to verify
        r_retired: ("skipped", "node_not_candidate")}
    assert outs[r_retired]["details"]["status"] == "archived"
    assert outs[r_nopage]["details"]["subject"] == {"id": nopage, "name": nopage}  # unreadable -> id
    # the legacy list keeps its old shape: page_missing never entered it (documented difference)
    assert res["skipped"] == [{"review_id": r_sentinel, "reason": "missing_required_item_type"}]
    assert res["promoted"] == 0


def test_promotion_without_a_graph_reports_every_approval(tmp_path):
    rid = _approve_promotion(tmp_path, NID)
    res = _promote(tmp_path)
    (o,) = _one_each(res, [rid], "promote_candidate_node").values()
    assert o["outcome"] == "skipped" and o["reason"] == "graph_missing"
    assert o["details"]["subject"] == {"id": NID, "name": NID}


# --- B2a review round 2: reporting never raises; already_in_effect only when verified -------------


def _unreadable(monkeypatch, page):
    """Make every read of `page` fail like a chmod-0 file (deterministic, whoever runs the tests)."""
    real = Path.read_text

    def _read(self, *a, **k):
        if Path(self) == page:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", _read)


def test_retype_with_unreadable_page_is_refused_not_raised(tmp_path, monkeypatch):
    conn = tr._graph(tmp_path)
    slug = tr._node(tmp_path, conn, tr.ITEM, "Kubernetes", "method_technique", status="active")
    tr._approve(tmp_path)
    page = tmp_path / "wiki" / NODE_DIR["item"] / f"{slug}.md"
    before = page.read_bytes()
    _unreadable(monkeypatch, page)
    res = _retypes(tmp_path, conn)                          # used to raise PermissionError
    (o,) = _one_each(res, ["rev_rt"], "change_item_type").values()
    assert o["outcome"] == "skipped" and o["reason"] == item_pages.OWNER_UNKNOWN
    assert o["details"]["subject"] == {"id": tr.ITEM, "name": tr.ITEM}     # the id, never a blank
    assert o["details"]["before"] == {"item_type": None}                    # unavailable, not guessed
    assert o["details"]["unavailable"] == ["before.item_type"]
    assert o["details"]["after"] == {"item_type": "infrastructure_hardware"}
    assert res["skipped"] == [{"review_id": "rev_rt", "reason": item_pages.OWNER_UNKNOWN}]
    assert res["applied"] == 0 and res["changed_pages"] == []
    monkeypatch.undo()
    assert page.read_bytes() == before                      # nothing written
    assert graph.get_node(conn, tr.ITEM)["item_type"] == "method_technique"
    conn.close()


def test_retype_done_with_unreadable_recheck_is_not_in_effect(tmp_path, monkeypatch):
    conn = tr._graph(tmp_path)
    tr._node(tmp_path, conn, tr.ITEM, "Kubernetes", "infrastructure_hardware", status="active")
    tr._approve(tmp_path)                                   # already at the target type
    real = items._read_node_meta
    calls = {"n": 0}

    def _flaky(path):                                       # the executor's read works, the recheck fails
        calls["n"] += 1
        if calls["n"] > 1:
            raise PermissionError("recheck")
        return real(path)
    monkeypatch.setattr(items, "_read_node_meta", _flaky)
    res = _retypes(tmp_path, conn)
    (o,) = _one_each(res, ["rev_rt"], "change_item_type").values()
    assert o["outcome"] == "skipped" and o["reason"] == apply_outcomes.VERIFICATION_UNAVAILABLE
    assert res["skipped"] == [] and res["applied"] == 0     # legacy shape unchanged
    monkeypatch.undo()
    again = _retypes(tmp_path, conn)                        # readable: verified
    assert again["outcomes"][0]["outcome"] == "already_in_effect"
    conn.close()


def _page_meta(tmp_path, nid):
    conn = _setup_graph(tmp_path)
    node = graph.get_node(conn, nid)
    conn.close()
    return promote._read_meta(tmp_path / "wiki" / "Items" / f"{node['slug']}.md")


AMENDED = {"title": "New Name", "aliases": ["  Alpha ", "Alpha", "Beta  ", " "],
           "description": "  A described tag. "}


def test_promote_active_tag_without_its_amendments_is_not_in_effect(tmp_path):
    conn = _setup_graph(tmp_path)
    slug = _candidate(tmp_path, conn, NID, "Old Name", status="active")   # a keep activated it
    conn.close()
    rid = _approve_promotion(tmp_path, NID, amendments=AMENDED)
    page = tmp_path / "wiki" / "Items" / f"{slug}.md"
    before = page.read_bytes()
    res = _promote(tmp_path)
    (o,) = _one_each(res, [rid], "promote_candidate_node").values()
    assert o["outcome"] == "skipped" and o["reason"] == apply_outcomes.POSTCONDITION_NOT_MET
    assert o["details"]["problem"] == {"fields": ["title", "aliases", "description"]}
    assert o["details"]["before"] == {"title": "Old Name", "aliases": [], "description": None}
    assert o["details"]["after"] == {"title": "New Name", "aliases": ["Alpha", "Beta", "Old Name"],
                                     "description": "A described tag."}
    assert o["details"]["subject"] == {"id": NID, "name": "Old Name"}
    assert res["promoted"] == 0 and res["skipped"] == []    # execution and legacy list unchanged
    assert page.read_bytes() == before

    # a field-by-field miss names only the field that differs
    rid2 = _approve_promotion(tmp_path, NID, amendments={"title": "Old Name",
                                                         "description": "Elsewhere"})
    assert rid2 == rid
    (o2,) = _one_each(_promote(tmp_path), [rid], "promote_candidate_node").values()
    assert o2["reason"] == apply_outcomes.POSTCONDITION_NOT_MET
    assert o2["details"]["problem"] == {"fields": ["description"]}


def test_promote_amendments_really_on_the_page_are_in_effect(tmp_path):
    conn = _setup_graph(tmp_path)
    _candidate(tmp_path, conn, NID, "Old Name")
    conn.close()
    rid = _approve_promotion(tmp_path, NID, amendments={**AMENDED, "item_type": "model"})
    first = _promote(tmp_path)                              # the executor applies them
    assert first["promoted"] == 1
    meta = _page_meta(tmp_path, NID)
    assert meta["title"] == "New Name" and meta["item_type"] == "model"
    assert meta["aliases"] == ["Alpha", "Beta", "Old Name"]  # normalized, old title appended
    assert meta["description"] == "A described tag."
    again = _promote(tmp_path)                              # active: the read-only pass verifies
    (o,) = _one_each(again, [rid], "promote_candidate_node").values()
    assert o["outcome"] == "already_in_effect" and again["promoted"] == 0
    assert o["details"]["subject"] == {"id": NID, "name": "New Name"}

    # the same page, verified as a fixed point when no rename alias is involved
    rid_b = _approve_promotion(tmp_path, NID, amendments={"aliases": [" Alpha", "Beta ", "Old Name",
                                                                     "Alpha"]})
    assert rid_b == rid
    (o2,) = _one_each(_promote(tmp_path), [rid], "promote_candidate_node").values()
    assert o2["outcome"] == "already_in_effect"


def test_promote_pass_with_a_failed_read_is_verification_unavailable(tmp_path, monkeypatch):
    conn = _setup_graph(tmp_path)
    slug = _candidate(tmp_path, conn, NID, "Old Name", status="active")
    conn.close()
    rid = _approve_promotion(tmp_path, NID)                 # would verify as in effect

    def _boom(*a, **k):
        raise PermissionError("unreadable")
    monkeypatch.setattr(promote, "_read_meta", _boom)        # the page's amendment read fails
    (o,) = _one_each(_promote(tmp_path), [rid], "promote_candidate_node").values()
    assert o["outcome"] == "skipped" and o["reason"] == apply_outcomes.VERIFICATION_UNAVAILABLE
    monkeypatch.undo()

    monkeypatch.setattr(promote, "_load_amendments", _boom)  # the approved item can't be re-read
    (o,) = _one_each(_promote(tmp_path), [rid], "promote_candidate_node").values()
    assert o["reason"] == apply_outcomes.VERIFICATION_UNAVAILABLE
    monkeypatch.undo()

    _unreadable(monkeypatch, tmp_path / "wiki" / "Items" / f"{slug}.md")   # the page status read
    (o,) = _one_each(_promote(tmp_path), [rid], "promote_candidate_node").values()
    assert o["outcome"] == "skipped" and o["reason"] == apply_outcomes.VERIFICATION_UNAVAILABLE
    assert o["details"]["subject"] == {"id": NID, "name": NID}
    monkeypatch.undo()

    (o,) = _one_each(_promote(tmp_path), [rid], "promote_candidate_node").values()
    assert o["outcome"] == "already_in_effect"              # readable again: verified


def test_identity_refusal_with_an_unreadable_journal_still_reports_the_refusal(tmp_path, monkeypatch):
    conn = tm._graph(tmp_path)
    tm._item(tmp_path, conn, tm.A, "Alpha")
    tm._approve(tmp_path, rid="rev_self", survivor=tm.A, absorbed=tm.A)

    def _boom(*a, **k):
        raise PermissionError("journal")
    monkeypatch.setattr(merges, "journal_stage", _boom)
    res = _merges(tmp_path, conn, close=False)
    (o,) = _one_each(res, ["rev_self"], "merge_items").values()
    assert o["outcome"] == "skipped" and o["reason"] == "self_merge"
    assert o["details"]["in_flight"] == apply_outcomes.VERIFICATION_UNAVAILABLE
    assert res["skipped"] == [{"review_id": "rev_self", "reason": "self_merge"}]
    conn.close()

    conn = ts._graph(tmp_path / "s")
    ts._approve(tmp_path / "s", rid="rev_bad", a=items.node_id("Elsewhere"), spinoff_name="")
    res = splits.apply_splits(conn, tmp_path / "s" / "reviews", wiki_dir=tmp_path / "s" / "wiki")
    (o,) = _one_each(res, ["rev_bad"], "split_item").values()
    assert o["outcome"] == "skipped" and o["reason"] == "invalid_proposal"
    assert o["details"]["in_flight"] == apply_outcomes.VERIFICATION_UNAVAILABLE
    conn.close()


# --- B2a review round 2: evidence-bound amendment verification ------------------------------------


def _tag_page(tmp_path, conn, nid, title, aliases, *, slug, status):
    graph.upsert_node(conn, node_id=nid, node_type="item", slug=slug, status=status,
                      item_type="method_technique")
    page = tmp_path / "wiki" / "Items" / f"{slug}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(render_item_page({
        "node_id": nid, "item_type": "method_technique", "title": title, "aliases": aliases,
        "confidence": "low", "source_ids": [], "status": status, "duplicates": []}),
        encoding="utf-8")
    conn.commit()
    return page


def _records(tmp_path, rid):
    return sorted((tmp_path / "reviews" / "audit_log").glob(f"{rid}-promoted-*.json"))


def _verify(tmp_path, rid):
    (o,) = _one_each(_promote(tmp_path), [rid], "promote_candidate_node").values()
    return o


def test_the_reviewers_case_is_never_verified_without_evidence(tmp_path):
    """Active "Old Name" with aliases [Expected, Unapproved]; the approval asks for title "Old Name"
    and aliases [Expected]. With no executor record, "Unapproved" may be a rename's appended title or
    an unwanted alias — never already_in_effect."""
    conn = _setup_graph(tmp_path)
    _tag_page(tmp_path, conn, NID, "Old Name", ["Expected", "Unapproved"], slug="old-name",
              status="active")
    conn.close()
    rid = _approve_promotion(tmp_path, NID, amendments={"title": "Old Name", "aliases": ["Expected"]})
    o = _verify(tmp_path, rid)
    assert (o["outcome"], o["reason"]) == ("skipped", apply_outcomes.VERIFICATION_UNAVAILABLE)
    assert o["details"]["problem"]["evidence"] == "no_executor_record"


def test_a_self_title_amendment_with_an_unwanted_extra_alias_is_not_in_effect(tmp_path):
    """With the executor record, the self-title amendment's result is known ([Expected]); an alias
    added afterwards is a certain mismatch."""
    conn = _setup_graph(tmp_path)
    page = _tag_page(tmp_path, conn, NID, "Old Name", [], slug="old-name", status="candidate")
    conn.close()
    rid = _approve_promotion(tmp_path, NID, amendments={"title": "Old Name", "aliases": ["Expected"]})
    assert _promote(tmp_path)["promoted"] == 1 and len(_records(tmp_path, rid)) == 1
    assert _verify(tmp_path, rid)["outcome"] == "already_in_effect"
    text = page.read_text(encoding="utf-8").replace('aliases: ["Expected"]',
                                                    'aliases: ["Expected", "Unapproved"]')
    page.write_text(text, encoding="utf-8")
    o = _verify(tmp_path, rid)
    assert (o["outcome"], o["reason"]) == ("skipped", apply_outcomes.POSTCONDITION_NOT_MET)
    assert o["details"]["problem"] == {"fields": ["aliases"]}


def test_a_verified_executed_rename_is_in_effect(tmp_path):
    conn = _setup_graph(tmp_path)
    _tag_page(tmp_path, conn, NID, "Old Name", [], slug="old-name", status="candidate")
    conn.close()
    rid = _approve_promotion(tmp_path, NID, amendments={"title": "New Name", "aliases": ["Expected"]})
    assert _promote(tmp_path)["promoted"] == 1
    (rec_path,) = _records(tmp_path, rid)
    rec = json.loads(rec_path.read_text(encoding="utf-8"))
    assert rec["title_before"] == "Old Name"
    assert rec["effective"]["aliases"] == ["Expected", "Old Name"]   # what execution produced
    assert _verify(tmp_path, rid)["outcome"] == "already_in_effect"


def test_a_second_rename_keeps_the_creation_time_alias_and_verifies_from_the_record(tmp_path):
    """Original → Intermediate (earlier) → Final (this approval): the executor appends Intermediate;
    Original stays among the aliases. Verified from the record, never from the creation-time name."""
    nid = items.node_id("Original")
    conn = _setup_graph(tmp_path)
    _tag_page(tmp_path, conn, nid, "Intermediate", ["Original"], slug="intermediate",
              status="candidate")
    conn.close()
    rid = _approve_promotion(tmp_path, nid, amendments={"title": "Final", "aliases": ["Original"]})
    assert _promote(tmp_path)["promoted"] == 1
    assert _page_meta(tmp_path, nid)["aliases"] == ["Original", "Intermediate"]
    assert _verify(tmp_path, rid)["outcome"] == "already_in_effect"
    for path in _records(tmp_path, rid):                     # the same state with no evidence
        path.unlink()
    o = _verify(tmp_path, rid)
    assert (o["outcome"], o["reason"]) == ("skipped", apply_outcomes.VERIFICATION_UNAVAILABLE)


def test_missing_unreadable_or_earlier_evidence_never_verifies(tmp_path):
    conn = _setup_graph(tmp_path)
    _tag_page(tmp_path, conn, NID, "Old Name", [], slug="old-name", status="candidate")
    conn.close()
    rid = _approve_promotion(tmp_path, NID, amendments={"title": "New Name"})
    assert _promote(tmp_path)["promoted"] == 1
    (rec_path,) = _records(tmp_path, rid)
    rec_path.write_text("{not json", encoding="utf-8")       # unreadable evidence
    o = _verify(tmp_path, rid)
    assert (o["outcome"], o["reason"]) == ("skipped", apply_outcomes.VERIFICATION_UNAVAILABLE)
    # a record older than this approval belongs to an earlier one and is NOT this approval's
    # evidence: its (conflicting) state is ignored; this title-only approval is then judged on the
    # page's title alone, which matches
    rec_path.write_text(json.dumps({"review_id": rid, "decision": "promoted",
                                    "decided_at": "2000-01-01T00:00:00Z",
                                    "effective": {"title": "Something Else", "aliases": [],
                                                  "description": None,
                                                  "item_type": "method_technique"}}),
                        encoding="utf-8")
    approved = tmp_path / "reviews" / "approved" / f"{rid}.json"
    item = json.loads(approved.read_text(encoding="utf-8"))
    approved.write_text(json.dumps({**item, "decided_at": "2026-01-01T00:00:00Z"}), encoding="utf-8")
    assert _verify(tmp_path, rid)["outcome"] == "already_in_effect"
    # ... and with aliases approved too, the one-extra-alias ambiguity has no evidence to settle it
    approved.write_text(json.dumps({**item, "decided_at": "2026-01-01T00:00:00Z",
                                    "amendments": {"title": "New Name", "aliases": []}}),
                        encoding="utf-8")
    o = _verify(tmp_path, rid)
    assert (o["outcome"], o["reason"]) == ("skipped", apply_outcomes.VERIFICATION_UNAVAILABLE)


# --- B2a review round 3: a record verifies only the approval it executed --------------------------

T = "2026-11-04T10:00:00Z"


def _approval_file(tmp_path, rid):
    return tmp_path / "reviews" / "approved" / f"{rid}.json"


def _reapprove(tmp_path, rid, **changes):
    path = _approval_file(tmp_path, rid)
    path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), **changes}),
                    encoding="utf-8")


def _executed(tmp_path):
    """"Old Name" promoted by an approval amending its title to "First Name", decided at T."""
    conn = _setup_graph(tmp_path)
    _tag_page(tmp_path, conn, NID, "Old Name", [], slug="old-name", status="candidate")
    conn.close()
    rid = _approve_promotion(tmp_path, NID, amendments={"title": "First Name"})
    _reapprove(tmp_path, rid, decided_at=T)
    assert _promote(tmp_path)["promoted"] == 1
    (rec,) = _records(tmp_path, rid)
    return rid, rec


def test_a_reapproval_with_other_amendments_in_the_same_second_is_not_verified(tmp_path):
    """The reviewer's reproduction: reopened and re-approved "Never Applied" within the same
    timestamp; the earlier record must not make the new approval look in effect."""
    rid, _rec = _executed(tmp_path)
    _reapprove(tmp_path, rid, amendments={"title": "Never Applied"}, decided_at=T)
    o = _verify(tmp_path, rid)
    assert o["outcome"] != "already_in_effect"
    assert (o["outcome"], o["reason"]) == ("skipped", apply_outcomes.POSTCONDITION_NOT_MET)
    assert _page_meta(tmp_path, NID)["title"] == "First Name"


def test_an_identical_reapproval_in_the_same_second_verifies(tmp_path):
    rid, _rec = _executed(tmp_path)
    _reapprove(tmp_path, rid, decision_note="approved again, same content", decided_at=T)
    assert _verify(tmp_path, rid)["outcome"] == "already_in_effect"


def test_a_record_whose_fingerprint_does_not_match_is_ignored(tmp_path):
    rid, rec = _executed(tmp_path)
    data = json.loads(rec.read_text(encoding="utf-8"))
    wrong = {**data["effective"], "title": "Something Else"}
    rec.write_text(json.dumps({**data, "effective": wrong}), encoding="utf-8")
    o = _verify(tmp_path, rid)                               # the matching record is used ...
    assert (o["outcome"], o["reason"]) == ("skipped", apply_outcomes.POSTCONDITION_NOT_MET)
    rec.write_text(json.dumps({**data, "effective": wrong, "approval_fingerprint": "0" * 64}),
                   encoding="utf-8")
    assert _verify(tmp_path, rid)["outcome"] == "already_in_effect"   # ... a foreign one isn't


def test_a_record_without_a_fingerprint_is_no_evidence(tmp_path):
    rid, rec = _executed(tmp_path)
    data = json.loads(rec.read_text(encoding="utf-8"))
    rec.write_text(json.dumps({k: v for k, v in data.items() if k != "approval_fingerprint"}
                              | {"effective": {**data["effective"], "title": "Something Else"}}),
                   encoding="utf-8")
    assert _verify(tmp_path, rid)["outcome"] == "already_in_effect"   # judged without it
    rec.write_text("{truncated", encoding="utf-8")           # unreadable: can't check the fingerprint
    o = _verify(tmp_path, rid)
    assert (o["outcome"], o["reason"]) == ("skipped", apply_outcomes.VERIFICATION_UNAVAILABLE)


def test_the_fingerprint_is_deterministic_and_keeps_list_order():
    item = {"review_id": "rev_x", "type": "promote_candidate_node", "subject": {"node_id": NID},
            "decided_at": T}
    a = promote.approval_fingerprint(item, {"aliases": ["A", "B"], "title": "X"})
    assert a == promote.approval_fingerprint(dict(reversed(list(item.items()))),
                                             {"title": "X", "aliases": ["A", "B"]})
    assert a != promote.approval_fingerprint(item, {"aliases": ["B", "A"], "title": "X"})
    assert a != promote.approval_fingerprint({**item, "decided_at": "2026-11-04T10:00:01Z"},
                                             {"aliases": ["A", "B"], "title": "X"})


# --- B2a review round 4: an incomplete matching record never verifies -----------------------------

GOOD = {"title": "First Name", "aliases": ["Old Name"], "description": None,
        "item_type": "method_technique"}


def _with_effective(rec, effective):
    data = json.loads(rec.read_text(encoding="utf-8"))
    rec.write_text(json.dumps({**data, "effective": effective}), encoding="utf-8")


def test_an_empty_effective_state_is_not_evidence_of_success(tmp_path):
    """The reviewer's reproduction: the page changed to "Never Approved", the matching record has an
    empty effective state — the page must not verify against itself."""
    rid, rec = _executed(tmp_path)
    page = next((tmp_path / "wiki" / "Items").glob("*.md"))
    page.write_text(page.read_text(encoding="utf-8").replace('title: "First Name"',
                                                             'title: "Never Approved"'),
                    encoding="utf-8")
    _with_effective(rec, {})
    o = _verify(tmp_path, rid)
    assert (o["outcome"], o["reason"]) == ("skipped", apply_outcomes.VERIFICATION_UNAVAILABLE)


@pytest.mark.parametrize("missing", ["title", "aliases", "description", "item_type"])
def test_each_missing_recorded_field_is_insufficient(tmp_path, missing):
    rid, rec = _executed(tmp_path)
    _with_effective(rec, {k: v for k, v in GOOD.items() if k != missing})
    o = _verify(tmp_path, rid)
    assert (o["outcome"], o["reason"]) == ("skipped", apply_outcomes.VERIFICATION_UNAVAILABLE)


@pytest.mark.parametrize("bad", [
    {"title": ["First Name"]}, {"title": "   "}, {"title": None},
    {"aliases": "Old Name"}, {"aliases": ["Old Name", 3]},
    {"description": 7}, {"item_type": None}, {"item_type": 1},
])
def test_malformed_recorded_values_are_insufficient(tmp_path, bad):
    rid, rec = _executed(tmp_path)
    _with_effective(rec, {**GOOD, **bad})
    o = _verify(tmp_path, rid)
    assert (o["outcome"], o["reason"]) == ("skipped", apply_outcomes.VERIFICATION_UNAVAILABLE)


def test_a_complete_record_still_verifies_and_compares_as_recorded(tmp_path):
    rid, rec = _executed(tmp_path)
    assert json.loads(rec.read_text(encoding="utf-8"))["effective"] == GOOD
    assert _verify(tmp_path, rid)["outcome"] == "already_in_effect"
    _with_effective(rec, {**GOOD, "title": " First Name "})     # recorded value compared unchanged
    o = _verify(tmp_path, rid)
    assert (o["outcome"], o["reason"]) == ("skipped", apply_outcomes.POSTCONDITION_NOT_MET)
