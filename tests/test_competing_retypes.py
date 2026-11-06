"""ADR-0070 B4 — competing `change_item_type` proposals for one tag: one review per target type, the
competitors shown wherever a retype is, the approval guard, the Apply recheck (never resolved by
processing order), exact withdrawals in the preview, and reject/defer never withdrawing."""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

from app.backend import apply_readable  # noqa: E402
from app.backend import main as main_module  # noqa: E402
from app.backend.models import ReviewDecisionRequest  # noqa: E402
from app.workers import retypes  # noqa: E402
from tests.test_retype import ITEM, _fm, _graph, _node  # noqa: E402
from tests.test_review_html import client  # noqa: E402,F401

SID = "src_0123456789abcdef"


@pytest.fixture
def clock(monkeypatch):
    """A clock that moves on a minute per timestamp, so decisions and executions are strictly
    ordered (the strict-after rule blocks same-second approvals by design)."""
    from app.workers import reviews as reviews_mod
    state = {"t": datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc), "frozen": False}

    def now():
        if not state["frozen"]:
            state["t"] += timedelta(minutes=1)
        return state["t"].isoformat()
    monkeypatch.setattr(reviews_mod, "iso_now", now)
    monkeypatch.setattr(retypes, "iso_now", now)
    return state


def _retype(root, rid, to_type, *, state="pending", status=None, decided_at=None):
    d = root / "reviews" / state
    d.mkdir(parents=True, exist_ok=True)
    item = {"review_id": rid, "type": "change_item_type", "status": status or state,
            "priority": "low", "created_at": "2026-11-01T10:00:00+00:00",
            "subject": {"node_id": ITEM, "to_item_type": to_type},
            "proposal": {"to_item_type": to_type},
            "context": {"source_id": SID, "from_item_type": "method_technique",
                        "name": "Kubernetes"}}
    if decided_at:
        item["decided_at"] = decided_at
    (d / f"{rid}.json").write_text(json.dumps(item), encoding="utf-8")


def _tag(root):
    conn = _graph(root)
    slug = _node(root, conn, ITEM, "Kubernetes", "method_technique", status="active")
    conn.close()
    return slug


def _apply(root):
    conn = _graph(root)
    try:
        out = retypes.apply_retypes(conn, root / "reviews", wiki_dir=root / "wiki")
        conn.commit()
        return out
    finally:
        conn.close()


def _approve(http, rid):
    return http.post(f"/reviews/{rid}/approve", json={})


def _outcome(res, rid):
    (o,) = [o for o in res["outcomes"] if o["review_id"] == rid]
    return o


# --- the approval guard --------------------------------------------------------------------------


def test_a_second_approval_is_refused_on_every_path_and_links_the_one_to_reopen(client, tmp_path):  # noqa: F811
    _tag(tmp_path)
    _retype(tmp_path, "rev_a", "domain")
    _retype(tmp_path, "rev_b", "model")
    form = client.get("/ui/reviews/rev_b").text              # a form opened before A's approval
    assert "value='approve'" in form
    assert _approve(client, "rev_a").status_code == 200
    api = _approve(client, "rev_b")                          # the JSON/API path
    assert api.status_code == 409 and "rev_a" in api.json()["detail"]
    ui = client.post("/ui/reviews/rev_b/decide", data={"action": "approve"})   # the stale form
    assert ui.status_code == 409
    assert "<a href='/ui/reviews/rev_a'>Open rev_a to reopen it</a>" in ui.text
    assert (tmp_path / "reviews" / "pending" / "rev_b.json").exists()


def test_concurrent_approvals_let_exactly_one_through(client, tmp_path):  # noqa: F811
    _tag(tmp_path)
    _retype(tmp_path, "rev_a", "domain")
    _retype(tmp_path, "rev_b", "model")

    def approve(rid):
        try:
            return main_module._record_decision(rid, "approved", ReviewDecisionRequest())["status"]
        except HTTPException as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(map(str, pool.map(approve, ["rev_a", "rev_b"])))
    assert results == ["409", "approved"]


def test_reopening_the_approved_one_lets_the_other_through(client, tmp_path):  # noqa: F811
    slug = _tag(tmp_path)
    _retype(tmp_path, "rev_a", "domain")
    _retype(tmp_path, "rev_b", "model")
    assert _approve(client, "rev_a").status_code == 200
    reopened = client.post("/reviews/rev_a/reopen", json={"reason": "chose model instead"})
    assert reopened.status_code == 200
    assert _approve(client, "rev_b").status_code == 200
    res = _apply(tmp_path)
    assert _outcome(res, "rev_b")["outcome"] == "applied" and _fm(tmp_path, slug)["item_type"] == "model"
    assert not (tmp_path / "reviews" / "pending" / "rev_a.json").exists()     # withdrawn by B


def test_an_applied_approval_never_applies_again_after_a_later_one(client, tmp_path, clock):  # noqa: F811
    slug = _tag(tmp_path)
    _retype(tmp_path, "rev_a", "domain")
    _retype(tmp_path, "rev_b", "model")
    assert _approve(client, "rev_a").status_code == 200
    assert _outcome(_apply(tmp_path), "rev_a")["outcome"] == "applied"
    # B was withdrawn by A's apply; a later proposal of the same target is filed again
    _retype(tmp_path, "rev_c", "model")
    assert _approve(client, "rev_c").status_code == 200       # A is carried out: not waiting
    second = _apply(tmp_path)
    assert _outcome(second, "rev_c")["outcome"] == "applied"
    assert _fm(tmp_path, slug)["item_type"] == "model"
    third = _apply(tmp_path)                                   # never flips back to A's type
    assert _outcome(third, "rev_a")["reason"] == "superseded_by_retype"
    assert _outcome(third, "rev_c")["outcome"] == "already_in_effect"
    assert _fm(tmp_path, slug)["item_type"] == "model"


def test_a_restored_database_with_two_waiting_approvals_blocks_both(tmp_path):
    slug = _tag(tmp_path)
    _retype(tmp_path, "rev_a", "domain", state="approved", decided_at="2026-11-02T10:00:00+00:00")
    _retype(tmp_path, "rev_b", "model", state="approved", decided_at="2026-11-02T10:00:00+00:00")
    res = _apply(tmp_path)
    for rid in ("rev_a", "rev_b"):
        o = _outcome(res, rid)
        assert (o["outcome"], o["reason"]) == ("skipped", "competing_retypes_approved")
    assert _fm(tmp_path, slug)["item_type"] == "method_technique"   # nothing applied by order
    html = apply_readable.render(apply_readable.model(
        [{"review_id": r, "type": "change_item_type"} for r in ("rev_a", "rev_b")],
        res["outcomes"]), preview=True)
    blocked = html.split("Attention needed")[1]
    assert blocked.count("<strong>Blocked</strong>") == 2 and "no order decides" in blocked
    assert "competing_retypes_approved" not in blocked.split("<details>")[0]


# --- withdrawals ---------------------------------------------------------------------------------


def test_the_preview_lists_the_exact_proposals_the_chosen_type_withdraws(tmp_path):
    _tag(tmp_path)
    _retype(tmp_path, "rev_a", "domain", state="approved", decided_at="2026-11-02T10:00:00+00:00")
    _retype(tmp_path, "rev_b", "model")
    _retype(tmp_path, "rev_c", "tool_platform", status="deferred")
    res = _apply(tmp_path)
    o = _outcome(res, "rev_a")
    assert {w["review_id"] for w in o["details"]["withdraws"]} == {"rev_b", "rev_c"}
    sentence = apply_readable.sentence(o, set())
    assert "withdraws the other proposals" in sentence
    assert "rev_b" in sentence and "rev_c" in sentence and "tool_platform" in sentence


def test_rejecting_or_deferring_a_retype_never_withdraws_its_competitors(client, tmp_path):  # noqa: F811
    _tag(tmp_path)
    _retype(tmp_path, "rev_a", "domain")
    _retype(tmp_path, "rev_b", "model")
    assert client.post("/reviews/rev_a/reject", json={}).status_code == 200
    assert client.post("/reviews/rev_b/defer", json={}).status_code == 200
    assert (tmp_path / "reviews" / "pending" / "rev_b.json").exists()
    _retype(tmp_path, "rev_d", "concept")
    assert client.post("/reviews/rev_d/defer", json={}).status_code == 200
    assert (tmp_path / "reviews" / "pending" / "rev_b.json").exists()


# --- visibility ----------------------------------------------------------------------------------


def test_the_review_page_and_the_queue_name_the_competitors(client, tmp_path):  # noqa: F811
    _tag(tmp_path)
    _retype(tmp_path, "rev_a", "domain")
    _retype(tmp_path, "rev_b", "model")
    assert _approve(client, "rev_a").status_code == 200
    page = client.get("/ui/reviews/rev_b").text
    assert "<h2>Competing type changes</h2>" in page
    assert "<code>domain</code></a> (approved, not yet applied, from “src_0123456789abcdef”)" in page
    queue = client.get("/ui/reviews").text
    assert "Also proposed for this tag" in queue and "<code>domain</code>" in queue


def test_the_document_screen_row_names_the_competitors(tmp_path):
    from app.backend import review_html, review_read
    from app.workers import items as items_mod
    from tests.test_tag_links import _build, _extract, _item, _sids
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    _extract(tmp_path, {"alpha": [_item("Kubernetes")], "beta": [_item("Kubernetes")]})
    s = _sids(tmp_path)
    nid = items_mod.node_id("Kubernetes")
    for rid, to_type, sid in (("rev_a", "domain", s["a.md"]), ("rev_b", "model", s["b.md"])):
        d = tmp_path / "reviews" / "pending"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{rid}.json").write_text(json.dumps({
            "review_id": rid, "type": "change_item_type", "status": "pending", "priority": "low",
            "subject": {"node_id": nid, "to_item_type": to_type},
            "proposal": {"to_item_type": to_type},
            "context": {"source_id": sid, "from_item_type": "method_technique",
                        "name": "Kubernetes"}}), encoding="utf-8")
    view = review_read.source_review_view(
        tmp_path / "reviews", s["a.md"], graph_db=tmp_path / "db" / "graph.sqlite",
        wiki_dir=tmp_path / "wiki", manifests_dir=tmp_path / "raw" / "manifests",
        enrichment_dir=tmp_path / "normalized" / "enrichment")
    (row,) = view["retype_items"]
    assert [c["review_id"] for c in row["competitors"]] == ["rev_b"]
    html = review_html.render_source_screen(view)
    assert "Also proposed for this tag" in html and "<code>model</code>" in html
    assert "(pending, from “" in html


# --- B4 review round 1 ---------------------------------------------------------------------------


def test_a_newer_choice_is_never_reversed_by_file_order(client, tmp_path, clock):  # noqa: F811
    """rev_z_old (applied → domain), then rev_a_new (→ model) approved: whatever order the files are
    processed in, the tag stays on the newer choice across repeated Apply runs."""
    slug = _tag(tmp_path)
    _retype(tmp_path, "rev_z_old", "domain")
    assert _approve(client, "rev_z_old").status_code == 200
    assert _outcome(_apply(tmp_path), "rev_z_old")["outcome"] == "applied"
    _retype(tmp_path, "rev_a_new", "model")                  # sorts BEFORE rev_z_old
    assert _approve(client, "rev_a_new").status_code == 200
    first = _apply(tmp_path)
    assert _outcome(first, "rev_a_new")["outcome"] == "applied"
    assert _outcome(first, "rev_z_old")["reason"] == "superseded_by_retype"
    assert _fm(tmp_path, slug)["item_type"] == "model"
    for _run in range(2):
        again = _apply(tmp_path)
        assert _outcome(again, "rev_a_new")["outcome"] == "already_in_effect"
        assert _outcome(again, "rev_z_old")["reason"] == "superseded_by_retype"
        assert _fm(tmp_path, slug)["item_type"] == "model"


def test_a_same_second_reapproval_is_blocked_and_a_later_one_applies(client, tmp_path, clock):  # noqa: F811
    """B4 review round 2: a reopened review re-approved within the same second as the tag's latest
    execution is not provably the later choice — blocked; approved again afterwards, it applies."""
    slug = _tag(tmp_path)
    _retype(tmp_path, "rev_a", "domain")
    assert _approve(client, "rev_a").status_code == 200
    _apply(tmp_path)                                          # domain
    _retype(tmp_path, "rev_b", "model")
    assert _approve(client, "rev_b").status_code == 200
    _apply(tmp_path)                                          # model, at L
    clock["frozen"] = True                                    # everything below happens at L
    assert client.post("/reviews/rev_a/reopen", json={"reason": "back to domain"}).status_code == 200
    assert _approve(client, "rev_a").status_code == 200       # a new approval, but at L
    res = _apply(tmp_path)
    o = _outcome(res, "rev_a")
    assert (o["outcome"], o["reason"]) == ("skipped", "approval_not_after_latest_retype")
    assert _outcome(res, "rev_b")["outcome"] == "already_in_effect"
    assert _fm(tmp_path, slug)["item_type"] == "model"
    clock["frozen"] = False                                   # time moves on
    assert client.post("/reviews/rev_a/reopen", json={"reason": "again"}).status_code == 200
    assert _approve(client, "rev_a").status_code == 200       # provably after L
    res = _apply(tmp_path)
    assert _outcome(res, "rev_a")["outcome"] == "applied"
    assert _outcome(res, "rev_b")["reason"] == "superseded_by_retype"
    assert _fm(tmp_path, slug)["item_type"] == "domain"
    assert _outcome(_apply(tmp_path), "rev_b")["reason"] == "superseded_by_retype"
    assert _fm(tmp_path, slug)["item_type"] == "domain"


def _malformed(root):
    for state, rid, subject, context in (("pending", "rev_bad1", ["bad"], {"source_id": SID}),
                                         ("pending", "rev_bad2", {"node_id": ITEM,
                                                                  "to_item_type": "model"}, "x"),
                                         ("approved", "rev_bad3", ["bad"], {})):
        d = root / "reviews" / state
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{rid}.json").write_text(json.dumps({
            "review_id": rid, "type": "change_item_type", "status": state, "priority": "low",
            "subject": subject, "proposal": {"to_item_type": "model"}, "context": context}),
            encoding="utf-8")


def test_malformed_reviews_never_break_the_screens_or_apply(client, tmp_path):  # noqa: F811
    _tag(tmp_path)
    _retype(tmp_path, "rev_ok", "domain")
    _malformed(tmp_path)
    assert retypes.competitors(tmp_path / "reviews", ITEM, exclude="rev_ok") == []
    queue = client.get("/ui/reviews")
    assert queue.status_code == 200 and "rev_ok" in queue.text
    assert client.get("/ui/reviews/rev_ok").status_code == 200
    res = _apply(tmp_path)
    assert _outcome(res, "rev_bad3")["reason"] == "invalid_subject"


def test_the_document_screen_survives_a_malformed_retype(tmp_path):
    from app.backend import review_html, review_read
    from tests.test_tag_links import _build, _sids
    _build(tmp_path, {"a.md": "alpha body"})
    sid = _sids(tmp_path)["a.md"]
    _malformed(tmp_path)
    view = review_read.source_review_view(
        tmp_path / "reviews", sid, graph_db=tmp_path / "db" / "graph.sqlite",
        wiki_dir=tmp_path / "wiki", manifests_dir=tmp_path / "raw" / "manifests",
        enrichment_dir=tmp_path / "normalized" / "enrichment")
    assert view is not None and "<h1>" in review_html.render_source_screen(view)


# --- B4 review round 2: "waiting" is not "newer"; Apply fails closed on unreadable history -------


def _executed_model(client, tmp_path):  # noqa: F811
    """model carried out at L (via a real approval and Apply); returns (slug, L)."""
    slug = _tag(tmp_path)
    _retype(tmp_path, "rev_m", "model")
    assert _approve(client, "rev_m").status_code == 200
    _apply(tmp_path)
    (rec,) = (tmp_path / "reviews" / "audit_log").glob("rev_m-retyped-*.json")
    return slug, json.loads(rec.read_text())["decided_at"]


def _restored_domain(tmp_path, decided_at):
    """An approval restored from elsewhere: approved, no execution evidence, its own decided_at."""
    _retype(tmp_path, "rev_d", "domain", state="approved", decided_at=decided_at)


def test_a_restored_older_approval_never_overwrites_the_later_choice(client, tmp_path, clock):  # noqa: F811
    slug, latest = _executed_model(client, tmp_path)
    _restored_domain(tmp_path, "2026-11-01T09:00:00+00:00")   # before L
    for _run in range(2):
        res = _apply(tmp_path)
        assert _outcome(res, "rev_d")["reason"] == "approval_not_after_latest_retype"
        assert _outcome(res, "rev_m")["outcome"] == "already_in_effect"   # not superseded
        assert _fm(tmp_path, slug)["item_type"] == "model"
    html = apply_readable.render(apply_readable.model(
        [{"review_id": "rev_d", "type": "change_item_type"}], res["outcomes"]), preview=True)
    assert "not provably approved after the tag" in html


@pytest.mark.parametrize("when", ["same", "garbage"])
def test_an_ambiguously_ordered_approval_is_blocked(client, tmp_path, clock, when):  # noqa: F811
    slug, latest = _executed_model(client, tmp_path)
    _restored_domain(tmp_path, latest if when == "same" else "not a time")
    res = _apply(tmp_path)
    assert _outcome(res, "rev_d")["reason"] == "approval_not_after_latest_retype"
    assert _fm(tmp_path, slug)["item_type"] == "model"


def test_a_provably_newer_approval_applies_and_supersedes(client, tmp_path, clock):  # noqa: F811
    slug, latest = _executed_model(client, tmp_path)
    _restored_domain(tmp_path, "2030-01-01T00:00:00+00:00")   # after L
    res = _apply(tmp_path)
    assert _outcome(res, "rev_d")["outcome"] == "applied"
    assert _outcome(res, "rev_m")["reason"] == "superseded_by_retype"
    assert _fm(tmp_path, slug)["item_type"] == "domain"


def test_the_latest_time_includes_reopened_generations(client, tmp_path, clock):  # noqa: F811
    slug, latest = _executed_model(client, tmp_path)
    # its review is no longer approved (an earlier generation; reopen itself refuses an applied
    # effect, ADR-0045, so the state is set directly as a restore or an older system leaves it)
    approved = tmp_path / "reviews" / "approved" / "rev_m.json"
    item = json.loads(approved.read_text())
    item.update(status="pending")
    for f in ("decided_at", "decided_by", "decision_note", "approval_id"):
        item.pop(f, None)
    (tmp_path / "reviews" / "pending" / "rev_m.json").write_text(json.dumps(item))
    approved.unlink()
    _restored_domain(tmp_path, "2026-11-01T09:00:00+00:00")   # before that generation ran
    res = _apply(tmp_path)
    assert _outcome(res, "rev_d")["reason"] == "approval_not_after_latest_retype"
    assert _fm(tmp_path, slug)["item_type"] == "model"


def test_an_unreadable_latest_record_leaves_the_tag_unchanged(client, tmp_path, clock):  # noqa: F811
    slug, latest = _executed_model(client, tmp_path)
    (rec,) = (tmp_path / "reviews" / "audit_log").glob("rev_m-retyped-*.json")
    rec.write_bytes(b"\xff\xfe not utf-8")                    # the latest execution can't be read
    _restored_domain(tmp_path, "2026-11-01T09:00:00+00:00")
    res = _apply(tmp_path)
    assert _outcome(res, "rev_d")["reason"] == "execution_history_unreadable"
    assert _fm(tmp_path, slug)["item_type"] == "model"


def test_invalid_utf8_files_are_skipped_by_screens_and_never_crash_apply(client, tmp_path, clock):  # noqa: F811
    _tag(tmp_path)
    _retype(tmp_path, "rev_ok", "domain")
    (tmp_path / "reviews" / "pending" / "rev_bin.json").write_bytes(b"\xff\xfe{")
    audit = tmp_path / "reviews" / "audit_log"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / "rev_orphan-retyped-00000000.json").write_bytes(b"\xff\xfe")
    assert retypes.retype_index(tmp_path / "reviews").get(ITEM)          # rev_ok still indexed
    assert client.get("/ui/reviews").status_code == 200
    assert client.get("/ui/reviews/rev_ok").status_code == 200
    assert _approve(client, "rev_ok").status_code == 200
    res = _apply(tmp_path)                                               # no crash; fails closed
    assert _outcome(res, "rev_ok")["reason"] == "execution_history_unreadable"


# --- B4 review round 3: replays are decided on the complete history ------------------------------


def _a_then_b(client, tmp_path):  # noqa: F811
    """A carried out (domain), then B carried out (model)."""
    slug = _tag(tmp_path)
    _retype(tmp_path, "rev_a", "domain")
    assert _approve(client, "rev_a").status_code == 200
    _apply(tmp_path)
    _retype(tmp_path, "rev_b", "model")
    assert _approve(client, "rev_b").status_code == 200
    _apply(tmp_path)
    assert _fm(tmp_path, slug)["item_type"] == "model"
    return slug


def _move_b(tmp_path, state):
    src = tmp_path / "reviews" / "approved" / "rev_b.json"
    item = json.loads(src.read_text())
    item["status"] = state
    if state == "pending":
        for f in ("decided_at", "decided_by", "decision_note", "approval_id"):
            item.pop(f, None)
    dest = tmp_path / "reviews" / state / "rev_b.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(item))
    src.unlink()
    return dest


@pytest.mark.parametrize("state", ["pending", "rejected"])
def test_an_older_executed_approval_never_replays_over_a_later_execution(client, tmp_path, clock,  # noqa: F811
                                                                         state):
    slug = _a_then_b(client, tmp_path)
    b_file = _move_b(tmp_path, state)
    before = b_file.read_bytes()
    for _run in range(2):
        res = _apply(tmp_path)
        assert _outcome(res, "rev_a")["reason"] == "superseded_by_retype"
        assert _fm(tmp_path, slug)["item_type"] == "model"
        assert b_file.exists() and b_file.read_bytes() == before        # never withdrawn


def test_an_unreadable_later_record_blocks_the_older_replay(client, tmp_path, clock):  # noqa: F811
    slug = _a_then_b(client, tmp_path)
    b_file = _move_b(tmp_path, "pending")
    before = b_file.read_bytes()
    (rec,) = (tmp_path / "reviews" / "audit_log").glob("rev_b-retyped-*.json")
    rec.write_bytes(b"\xff\xfe not utf-8")                    # A's own record stays readable
    res = _apply(tmp_path)
    assert _outcome(res, "rev_a")["reason"] == "execution_history_unreadable"
    assert _fm(tmp_path, slug)["item_type"] == "model"
    assert b_file.read_bytes() == before


def test_the_latest_approval_still_repairs_drift_when_its_history_is_complete(client, tmp_path,  # noqa: F811
                                                                             clock):
    slug = _tag(tmp_path)
    _retype(tmp_path, "rev_a", "domain")
    assert _approve(client, "rev_a").status_code == 200
    _apply(tmp_path)
    page = tmp_path / "wiki" / "Items" / f"{slug}.md"
    page.write_text(page.read_text().replace("item_type: domain", "item_type: concept"))  # drift
    assert _fm(tmp_path, slug)["item_type"] == "concept"
    res = _apply(tmp_path)
    assert _outcome(res, "rev_a")["outcome"] == "applied"
    assert _fm(tmp_path, slug)["item_type"] == "domain"


def test_timestamps_compare_as_instants_with_offsets():
    later = retypes._time("2026-11-06T10:30:00+01:00")      # 09:30 UTC
    earlier = retypes._time("2026-11-06T09:45:00+00:00")
    assert later < earlier                                   # not a string comparison
    item = {"review_id": "rev_x", "decided_at": "2026-11-06T10:30:00+01:00"}
    assert retypes._owns(item, "rev_x", {"review_id": "rev_x", "decided_at": "2026-11-06T09:45:00Z"})
    assert not retypes._owns(item, "rev_x", {"review_id": "rev_x",
                                             "decided_at": "2026-11-06T09:00:00Z"})
    assert not retypes._owns({"review_id": "rev_x"}, "rev_x",           # no decided_at: unproven
                             {"review_id": "rev_x", "decided_at": "2026-11-06T09:45:00Z"})


# --- B4 review round 4: a verified no-op approval settles, distinct from an execution ------------


def _records(tmp_path, kind):
    return sorted((tmp_path / "reviews" / "audit_log").glob(f"*-{kind}-*.json"))


def test_an_in_effect_approval_settles_once_and_releases_the_guard(client, tmp_path, clock):  # noqa: F811
    slug = _tag(tmp_path)                                     # already method_technique
    _retype(tmp_path, "rev_same", "method_technique")
    assert _approve(client, "rev_same").status_code == 200
    first = _apply(tmp_path)
    assert _outcome(first, "rev_same")["outcome"] == "already_in_effect"
    (ver,) = _records(tmp_path, "retype-verified")
    assert _records(tmp_path, "retyped") == []                # not an execution
    stamp = ver.read_bytes()
    again = _apply(tmp_path)                                  # reused: no new record, same time
    assert _outcome(again, "rev_same")["outcome"] == "already_in_effect"
    assert _records(tmp_path, "retype-verified") == [ver] and ver.read_bytes() == stamp
    _retype(tmp_path, "rev_new", "model")
    assert _approve(client, "rev_new").status_code == 200     # the guard is released
    res = _apply(tmp_path)
    assert _outcome(res, "rev_new")["outcome"] == "applied"
    assert _fm(tmp_path, slug)["item_type"] == "model"
    later = _apply(tmp_path)                                  # the verified one never replays
    assert _outcome(later, "rev_same")["reason"] == "superseded_by_retype"
    assert _fm(tmp_path, slug)["item_type"] == "model"
    page = client.get("/ui/reviews/rev_new").text
    assert "approved, verified in effect" in page and "approved, applied" not in page.split(
        "Competing type changes")[1].split("</div>")[0]


def test_a_legacy_in_effect_approval_settles_by_fingerprint(client, tmp_path, clock):  # noqa: F811
    slug = _tag(tmp_path)
    _retype(tmp_path, "rev_old", "method_technique", state="approved",
            decided_at="2026-01-01T00:00:00+00:00")             # no approval_id, no execution
    assert _outcome(_apply(tmp_path), "rev_old")["outcome"] == "already_in_effect"
    (ver,) = _records(tmp_path, "retype-verified")
    data = json.loads(ver.read_text())
    assert "approval_fingerprint" in data and "approval_id" not in data
    _retype(tmp_path, "rev_new", "model")
    assert _approve(client, "rev_new").status_code == 200
    assert _outcome(_apply(tmp_path), "rev_new")["outcome"] == "applied"
    assert _fm(tmp_path, slug)["item_type"] == "model"


def test_an_older_restored_approval_cannot_override_a_verified_choice(client, tmp_path, clock):  # noqa: F811
    slug = _tag(tmp_path)
    _retype(tmp_path, "rev_same", "method_technique")
    assert _approve(client, "rev_same").status_code == 200
    _apply(tmp_path)                                          # verified at V
    _retype(tmp_path, "rev_d", "domain", state="approved",
            decided_at="2026-11-01T09:00:00+00:00")             # restored, before V
    res = _apply(tmp_path)
    o = _outcome(res, "rev_d")
    assert o["reason"] == "approval_not_after_latest_retype"
    assert "latest_settled_choice_at" in o["details"]["problem"]
    assert _fm(tmp_path, slug)["item_type"] == "method_technique"


def test_unreadable_verification_history_blocks_changes(client, tmp_path, clock):  # noqa: F811
    slug = _tag(tmp_path)
    _retype(tmp_path, "rev_same", "method_technique")
    assert _approve(client, "rev_same").status_code == 200
    _apply(tmp_path)
    (ver,) = _records(tmp_path, "retype-verified")
    ver.write_bytes(b"\xff\xfe broken")
    _retype(tmp_path, "rev_d", "domain", state="approved",
            decided_at="2030-01-01T00:00:00+00:00")             # newer, but history unreadable
    res = _apply(tmp_path)
    assert _outcome(res, "rev_d")["reason"] == "execution_history_unreadable"
    assert _fm(tmp_path, slug)["item_type"] == "method_technique"
    assert len(_records(tmp_path, "retype-verified")) == 1   # no re-verification on unknown history
