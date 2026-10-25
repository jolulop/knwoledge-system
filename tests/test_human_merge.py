"""ADR-0064 decisions 9–10: the human-filed merge — a `merge_items` item filed PRE-APPROVED.

Worker `human_merge.file_merge` + `POST /reviews/merge` + the `/ui/reviews` form. Blocked outcomes
write nothing; collision routing never creates a parallel subject or bypasses a rejection.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.backend import graph
from app.backend import main as main_module
from app.backend.config import get_settings
from app.workers import human_merge, items, merges, reviews
import tagging  # noqa: E402
from app.workers.wiki_render import render_item_page

ITEM_TYPE = "method_technique"


def _graph(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    return graph.connect(gdb)


def _item(tmp_path, conn, name, *, status="active"):
    nid = items.node_id(name)
    graph.upsert_node(conn, node_id=nid, node_type="item", slug=items._slug(name), status=status,
                      item_type=ITEM_TYPE)
    page = tmp_path / "wiki" / "Items" / f"{items._slug(name)}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(render_item_page({
        "node_id": nid, "item_type": ITEM_TYPE, "title": name, "aliases": [],
        "confidence": "low", "source_ids": [], "status": status, "duplicates": [],
    }), encoding="utf-8")
    conn.commit()
    return nid


def _rid(survivor, absorbed):
    return reviews.review_id("merge_items", {"survivor_node_id": survivor,
                                             "absorbed_node_id": absorbed})


def _file(tmp_path, conn, survivor, absorbed):
    return human_merge.file_merge(conn, survivor=survivor, absorbed=absorbed,
                                  reviews_dir=tmp_path / "reviews")


def _ledger(tmp_path):
    d = tmp_path / "reviews"
    return sorted(str(p.relative_to(d)) for p in d.rglob("*.json")) if d.exists() else []


@pytest.fixture
def pair(tmp_path):
    conn = _graph(tmp_path)
    a = _item(tmp_path, conn, "Agentic AI")
    b = _item(tmp_path, conn, "AI Agents")
    yield conn, a, b
    conn.close()


# --- worker ------------------------------------------------------------------


def test_files_pre_approved_by_slug_with_audit(tmp_path, pair):
    conn, a, b = pair
    out = _file(tmp_path, conn, "agentic-ai", "Items/ai-agents.md")
    rid = _rid(a, b)
    assert out == {"outcome": "filed", "review_id": rid, "withdrawn": []}
    item = json.loads((tmp_path / "reviews" / "approved" / f"{rid}.json").read_text())
    assert item["status"] == "approved" and item["decided_by"] == "human"
    assert item["subject"] == {"survivor_node_id": a, "absorbed_node_id": b}
    assert item["proposal"] == {"to_status": "merged", "reason_code": "human_filed"}
    audits = list((tmp_path / "reviews" / "audit_log").glob(f"{rid}-human-filed-merge-*.json"))
    assert len(audits) == 1 and json.loads(audits[0].read_text())["actor"] == "human"


def test_files_by_item_id_and_applies(tmp_path, pair):
    conn, a, b = pair
    assert _file(tmp_path, conn, a, b)["outcome"] == "filed"
    res = tagging.close_journals(tmp_path / "reviews", merges.apply_merges(
        conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki"))
    assert res["applied"] == 1 and graph.get_node(conn, b)["status"] == "merged"


@pytest.mark.parametrize("survivor,absorbed,reason", [
    ("", "ai-agents", "invalid_ref"),
    ("nope", "ai-agents", "unknown_item"),
    ("itm_0000000000000000", "ai-agents", "unknown_item"),
    ("agentic-ai", "agentic-ai", "self_merge"),
])
def test_guards_block_and_write_nothing(tmp_path, pair, survivor, absorbed, reason):
    conn, _, _ = pair
    out = _file(tmp_path, conn, survivor, absorbed)
    assert out["outcome"] == "blocked" and out["reason"] == reason
    assert _ledger(tmp_path) == []


def test_non_item_id_blocks(tmp_path, pair):
    conn, _, b = pair
    src = "src_0000000000000001"
    graph.upsert_node(conn, node_id=src, node_type="source", slug=src, status="active")
    conn.commit()
    # A source id is not an itm_ id, so it is read as a slug and not found among items.
    assert _file(tmp_path, conn, src, b)["reason"] == "unknown_item"


@pytest.mark.parametrize("status,side,reason", [
    ("candidate", "absorbed", "absorbed_not_active"),
    ("candidate", "survivor", "survivor_not_active"),
    ("merged", "survivor", "survivor_already_merged"),
])
def test_lifecycle_guards(tmp_path, pair, status, side, reason):
    conn, a, b = pair
    graph.upsert_node(conn, node_id=a if side == "survivor" else b, node_type="item",
                      slug="agentic-ai" if side == "survivor" else "ai-agents", status=status,
                      item_type=ITEM_TYPE)
    conn.commit()
    out = _file(tmp_path, conn, a, b)
    assert out["outcome"] == "blocked" and out["reason"] == reason
    assert _ledger(tmp_path) == []


def test_same_direction_pending_is_approved_in_place(tmp_path, pair):
    conn, a, b = pair
    rid = reviews.create_review_item(
        tmp_path / "reviews", review_type="merge_items",
        subject={"survivor_node_id": a, "absorbed_node_id": b},
        proposal={"to_status": "merged", "reason_code": "alias_cross_match"})
    out = _file(tmp_path, conn, a, b)
    assert out == {"outcome": "approved_existing", "review_id": rid, "withdrawn": []}
    item = json.loads((tmp_path / "reviews" / "approved" / f"{rid}.json").read_text())
    assert item["proposal"]["reason_code"] == "alias_cross_match"     # the original item, approved
    assert not (tmp_path / "reviews" / "pending" / f"{rid}.json").exists()


def test_reverse_pending_is_withdrawn_then_human_direction_filed(tmp_path, pair):
    conn, a, b = pair
    rev = reviews.create_review_item(
        tmp_path / "reviews", review_type="merge_items",
        subject={"survivor_node_id": b, "absorbed_node_id": a},
        proposal={"to_status": "merged", "reason_code": "alias_cross_match"})
    out = _file(tmp_path, conn, a, b)
    assert out["outcome"] == "filed" and out["withdrawn"] == [rev]
    assert not (tmp_path / "reviews" / "pending" / f"{rev}.json").exists()
    withdrawn = list((tmp_path / "reviews" / "audit_log").glob(f"{rev}-withdrawn-*.json"))
    assert json.loads(withdrawn[0].read_text())["note"] == "superseded_by_human_merge"
    assert (tmp_path / "reviews" / "approved" / f"{_rid(a, b)}.json").exists()


def test_same_direction_rejected_blocks(tmp_path, pair):
    conn, a, b = pair
    rid = reviews.create_review_item(
        tmp_path / "reviews", review_type="merge_items",
        subject={"survivor_node_id": a, "absorbed_node_id": b}, proposal={"to_status": "merged"})
    reviews.resolve_review_item(tmp_path / "reviews", rid, decision="rejected", decided_by="human")
    before = _ledger(tmp_path)
    out = _file(tmp_path, conn, a, b)
    assert out["reason"] == "merge_previously_rejected" and out["decided_by"] == "human"
    assert _ledger(tmp_path) == before


def test_reverse_rejected_blocks_until_reopened(tmp_path, pair):
    # A rejection covers the PAIR: filing the reverse is blocked. The "wrong survivor" path is
    # explicit — reopen the rejection (audited, back to pending), then file the other direction,
    # which withdraws the reopened item (audited) and files the human's.
    conn, a, b = pair
    rev = reviews.create_review_item(
        tmp_path / "reviews", review_type="merge_items",
        subject={"survivor_node_id": b, "absorbed_node_id": a}, proposal={"to_status": "merged"})
    reviews.resolve_review_item(tmp_path / "reviews", rev, decision="rejected", decided_by="human")
    before = _ledger(tmp_path)
    out = _file(tmp_path, conn, a, b)
    assert out["reason"] == "merge_previously_rejected" and out["reversed"] is True
    assert out["review_id"] == rev and _ledger(tmp_path) == before
    assert reviews.reopen_review_item(tmp_path / "reviews", rev, reason="wrong survivor")
    out = _file(tmp_path, conn, a, b)
    assert out["outcome"] == "filed" and out["withdrawn"] == [rev]
    audit = tmp_path / "reviews" / "audit_log"
    assert list(audit.glob(f"{rev}-reopened*.json")) and list(audit.glob(f"{rev}-withdrawn-*.json"))
    assert (tmp_path / "reviews" / "approved" / f"{_rid(a, b)}.json").exists()


def test_reverse_approved_blocks(tmp_path, pair):
    conn, a, b = pair
    assert _file(tmp_path, conn, b, a)["outcome"] == "filed"
    before = _ledger(tmp_path)
    out = _file(tmp_path, conn, a, b)
    assert out["reason"] == "conflicting_merge_approved" and out["review_id"] == _rid(b, a)
    assert _ledger(tmp_path) == before


@pytest.fixture
def trio(tmp_path):
    conn = _graph(tmp_path)
    ids = [_item(tmp_path, conn, n) for n in ("Alpha", "Beta", "Gamma")]
    yield conn, *ids
    conn.close()


def test_shared_absorbed_with_approved_merge_blocks(tmp_path, trio):
    conn, a, b, c = trio
    assert _file(tmp_path, conn, a, b)["outcome"] == "filed"           # A <- B approved
    before = _ledger(tmp_path)
    for survivor, absorbed in ((c, b), (b, c)):                       # shared absorbed / chain
        out = _file(tmp_path, conn, survivor, absorbed)
        assert out["reason"] == "conflicting_merge_approved" and out["review_id"] == _rid(a, b)
    assert _ledger(tmp_path) == before
    # A shared SURVIVOR is a compatible star.
    assert _file(tmp_path, conn, a, c)["outcome"] == "filed"
    res = tagging.close_journals(tmp_path / "reviews", merges.apply_merges(
        conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki"))
    assert res["applied"] == 2 and res["skipped"] == []


def test_conflicting_pending_merges_are_withdrawn(tmp_path, trio):
    conn, a, b, c = trio
    shared = reviews.create_review_item(
        tmp_path / "reviews", review_type="merge_items",
        subject={"survivor_node_id": a, "absorbed_node_id": b},
        proposal={"to_status": "merged", "reason_code": "alias_cross_match"})
    chain = reviews.create_review_item(
        tmp_path / "reviews", review_type="merge_items",
        subject={"survivor_node_id": b, "absorbed_node_id": a},
        proposal={"to_status": "merged", "reason_code": "alias_cross_match"})
    star = reviews.create_review_item(
        tmp_path / "reviews", review_type="merge_items",
        subject={"survivor_node_id": c, "absorbed_node_id": a},
        proposal={"to_status": "merged", "reason_code": "alias_cross_match"})
    out = _file(tmp_path, conn, c, b)                                  # C <- B
    # A<-B shares absorbed B, and B<-A is a chain (B is its survivor) — both conflict and are
    # withdrawn; C<-A shares only the survivor C -> a compatible star, kept.
    assert sorted(out["withdrawn"]) == sorted([shared, chain])
    assert (tmp_path / "reviews" / "pending" / f"{star}.json").exists()


def test_approve_endpoint_refuses_conflicting_merge(client, tmp_path):
    conn = _graph(tmp_path)
    a, b, c = (_item(tmp_path, conn, n) for n in ("Alpha", "Beta", "Gamma"))
    conn.close()
    first = reviews.create_review_item(
        tmp_path / "reviews", review_type="merge_items",
        subject={"survivor_node_id": a, "absorbed_node_id": b}, proposal={"to_status": "merged"})
    second = reviews.create_review_item(
        tmp_path / "reviews", review_type="merge_items",
        subject={"survivor_node_id": c, "absorbed_node_id": b}, proposal={"to_status": "merged"})
    assert client.post(f"/reviews/{first}/approve").status_code == 200
    resp = client.post(f"/reviews/{second}/approve")
    assert resp.status_code == 409 and first in resp.json()["detail"]
    assert (tmp_path / "reviews" / "pending" / f"{second}.json").exists()
    # Rejecting / deferring a conflicting item stays allowed.
    assert client.post(f"/reviews/{second}/reject").status_code == 200


def test_executor_reports_absorbed_merged_elsewhere(tmp_path, trio):
    conn, a, b, c = trio
    _file(tmp_path, conn, a, b)
    assert tagging.close_journals(tmp_path / "reviews", merges.apply_merges(
        conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki"))["applied"] == 1
    # A conflicting approval forged straight onto disk (bypassing every guard) is a LOUD skip.
    (tmp_path / "reviews" / "approved" / "rev_forged.json").write_text(json.dumps({
        "review_id": "rev_forged", "type": "merge_items", "status": "approved",
        "subject": {"survivor_node_id": c, "absorbed_node_id": b},
        "proposal": {"to_status": "merged"}}), encoding="utf-8")
    res = tagging.close_journals(tmp_path / "reviews", merges.apply_merges(
        conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki"))
    assert res["applied"] == 0
    assert res["skipped"] == [{"review_id": "rev_forged", "reason": "absorbed_merged_elsewhere"}]


def test_same_direction_approved_is_noop(tmp_path, pair):
    conn, a, b = pair
    _file(tmp_path, conn, a, b)
    before = _ledger(tmp_path)
    assert _file(tmp_path, conn, a, b)["outcome"] == "already_approved"
    assert _ledger(tmp_path) == before


# --- API + UI ------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


def test_api_files_and_maps_blocks(client, tmp_path, pair):
    _, a, b = pair
    resp = client.post("/reviews/merge", json={"survivor": "agentic-ai", "absorbed": "ai-agents"})
    assert resp.status_code == 200
    assert resp.json() == {"review_id": _rid(a, b), "outcome": "filed", "withdrawn": []}
    assert client.post("/reviews/merge", json={"survivor": "", "absorbed": "x"}).status_code == 400
    assert client.post("/reviews/merge",
                       json={"survivor": "nope", "absorbed": "ai-agents"}).status_code == 404
    resp = client.post("/reviews/merge", json={"survivor": "ai-agents", "absorbed": "agentic-ai"})
    assert resp.status_code == 409 and "reopen" in resp.json()["detail"]


def test_api_rejected_merge_names_reopen_path(client, tmp_path, pair):
    _, a, b = pair
    rid = reviews.create_review_item(
        tmp_path / "reviews", review_type="merge_items",
        subject={"survivor_node_id": a, "absorbed_node_id": b}, proposal={"to_status": "merged"})
    reviews.resolve_review_item(tmp_path / "reviews", rid, decision="rejected", decided_by="human")
    resp = client.post("/reviews/merge", json={"survivor": a, "absorbed": b})
    assert resp.status_code == 409 and "ADR-0045" in resp.json()["detail"]


def test_api_graph_unavailable_is_503(client, tmp_path):
    resp = client.post("/reviews/merge", json={"survivor": "a", "absorbed": "b"})
    assert resp.status_code == 503


def test_ui_preview_writes_nothing_then_confirm_files(client, tmp_path, pair):
    conn, a, b = pair
    page = client.get("/ui/reviews")
    assert page.status_code == 200 and "action='/ui/reviews/merge/preview'" in page.text
    graph.upsert_node(conn, node_id="src_0000000000000001", node_type="source",
                      slug="src_0000000000000001", status="active")
    graph.upsert_assertion(conn, src_id="src_0000000000000001", dst_id=b, edge_type="mentions",
                           asserted_by="llm", status="active")
    conn.commit()
    pending = reviews.create_review_item(       # a conflicting machine proposal: B <- A
        tmp_path / "reviews", review_type="merge_items",
        subject={"survivor_node_id": b, "absorbed_node_id": a},
        proposal={"to_status": "merged", "reason_code": "alias_cross_match"})
    before = _ledger(tmp_path)
    prev = client.get("/ui/reviews/merge/preview",
                      params={"survivor": "agentic-ai", "absorbed": "ai-agents"})
    assert prev.status_code == 200
    assert "Confirm and approve merge" in prev.text and "1</strong> active edge" in prev.text
    assert pending in prev.text and "superseded_by_human_merge" in prev.text
    assert "AI Agents" in prev.text                       # alias union preview
    assert _ledger(tmp_path) == before                    # preview wrote nothing
    resp = client.post("/ui/reviews/merge", data={"survivor": a, "absorbed": b},
                       follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == f"/ui/reviews/{_rid(a, b)}"
    detail = client.get(resp.headers["location"])
    assert detail.status_code == 200 and "merge_items" in detail.text
    assert not (tmp_path / "reviews" / "pending" / f"{pending}.json").exists()


def test_ui_preview_block_and_already_approved(client, tmp_path, pair):
    _, a, b = pair
    resp = client.get("/ui/reviews/merge/preview", params={"survivor": "agentic-ai",
                                                           "absorbed": "nope"})
    assert resp.status_code == 404 and "unknown_item" in resp.text
    client.post("/reviews/merge", json={"survivor": a, "absorbed": b})
    resp = client.get("/ui/reviews/merge/preview", params={"survivor": a, "absorbed": b})
    assert resp.status_code == 200 and "already approved" in resp.text
    assert "Confirm and approve" not in resp.text


def test_ui_preview_graph_unavailable_is_503(client, tmp_path):
    resp = client.get("/ui/reviews/merge/preview", params={"survivor": "a", "absorbed": "b"})
    assert resp.status_code == 503


def test_ui_form_block_renders_error(client, tmp_path, pair):
    resp = client.post("/ui/reviews/merge", data={"survivor": "agentic-ai", "absorbed": "nope"})
    assert resp.status_code == 404 and "unknown_item" in resp.text
