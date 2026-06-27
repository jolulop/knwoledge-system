"""ADR-0045 reopen / re-decide: revert a not-yet-applied terminal decision back to pending.

The safety oracle is the existing A2 projector effect_status (no new applied-flag): reopen is allowed
only for PENDING_APPLY / NO_EFFECT_REQUIRED, and refused (409) for EFFECTED / UNKNOWN / INVALID_SUBJECT /
APPLY_DEFERRED. These tests construct each effect_status and assert the gate + the ledger transition.
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
from app.backend import review_read
from app.backend.config import get_settings

CA = "clm_aaaaaaaaaaaaaaaa"
CB = "clm_bbbbbbbbbbbbbbbb"


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


def _write(tmp_path: Path, where: str, rid: str, item: dict) -> None:
    d = tmp_path / "reviews" / where
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({"review_id": rid, **item}), encoding="utf-8")


def _approved_record_only(tmp_path: Path, rtype: str, rid: str = "rev_x") -> None:
    _write(tmp_path, "approved", rid, {
        "type": rtype, "status": "approved", "decided_by": "human", "decided_at": "t0",
        "subject": {"source_id": "src_0123456789abcdef"}, "proposal": {}, "context": {}})


def _graph(tmp_path: Path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    return graph.connect(gdb)


def _claim_nodes(conn, *, loser_deprecated: bool = False) -> None:
    graph.upsert_node(conn, node_id=CA, node_type="claim", slug=CA, status="active")
    graph.upsert_node(conn, node_id=CB, node_type="claim", slug=CB,
                      status="deprecated_candidate" if loser_deprecated else "active")


def _approved_supersede(tmp_path: Path, rid: str = "rev_c") -> None:
    _write(tmp_path, "approved", rid, {
        "type": "resolve_contradiction", "status": "approved", "decided_by": "human",
        "decided_at": "t0", "winner": CA, "subject": {"claim_a": CA, "claim_b": CB},
        "proposal": {}, "context": {}})


def _reopened_audits(tmp_path: Path, rid: str) -> list[Path]:
    d = tmp_path / "reviews" / "audit_log"
    return sorted(d.glob(f"{rid}-reopened-*.json")) if d.exists() else []


# --- the reopenable gate (unit) --------------------------------------------


def test_reopen_block_reason_mapping():
    assert review_read.reopen_block_reason(review_read.PENDING_APPLY) is None
    assert review_read.reopen_block_reason(review_read.NO_EFFECT_REQUIRED) is None
    assert review_read.reopen_block_reason(review_read.EFFECTED) == "already_applied"
    assert review_read.reopen_block_reason(review_read.UNKNOWN) == "effect_unknown_repair_read_model"
    assert review_read.reopen_block_reason(review_read.INVALID_SUBJECT) == "invalid_subject"
    assert review_read.reopen_block_reason(review_read.APPLY_DEFERRED) == "manual_effect_unknown"
    assert review_read.reopen_block_reason(None) == "not_reopenable"


# --- APPLY_DEFERRED blocks (the whole bucket, incl. manual-effect types) ----


@pytest.mark.parametrize("rtype", ["delete_raw_file", "purge_response_cache", "missing_raw_source"])
def test_apply_deferred_types_block_manual_effect_unknown(client, tmp_path, rtype):
    _approved_record_only(tmp_path, rtype)
    r = client.post("/reviews/rev_x/reopen", json={"reason": "misclick"})
    assert r.status_code == 409 and "manual_effect_unknown" in r.json()["detail"]
    # no ledger mutation: still terminal in approved/, not moved to pending, no reopened audit
    assert (tmp_path / "reviews" / "approved" / "rev_x.json").exists()
    assert not (tmp_path / "reviews" / "pending" / "rev_x.json").exists()
    assert _reopened_audits(tmp_path, "rev_x") == []


# --- reopenable cases -------------------------------------------------------


def test_pending_apply_supersede_reopens_and_clears_winner(client, tmp_path):
    conn = _graph(tmp_path)
    _claim_nodes(conn)
    graph.upsert_assertion(conn, src_id=CA, dst_id=CB, edge_type="contradicts",  # proposed = not applied
                           asserted_by="llm", status="proposed", review_id="rev_c")
    conn.commit()
    conn.close()
    _approved_supersede(tmp_path)
    r = client.post("/reviews/rev_c/reopen", json={"reason": "picked the wrong winner"})
    assert r.status_code == 200 and r.json() == {"review_id": "rev_c", "reopened": True,
                                                 "status": "pending"}
    item = json.loads((tmp_path / "reviews" / "pending" / "rev_c.json").read_text())
    assert item["status"] == "pending"
    for cleared in ("winner", "decided_by", "decided_at", "decision_note"):
        assert cleared not in item                       # terminal fields + winner cleared
    assert not (tmp_path / "reviews" / "approved" / "rev_c.json").exists()
    audits = _reopened_audits(tmp_path, "rev_c")
    assert len(audits) == 1 and audits[0].name == "rev_c-reopened-1.json"
    entry = json.loads(audits[0].read_text())
    assert entry["reason"] == "picked the wrong winner" and entry["prior_status"] == "approved"
    assert entry["prior_winner"] == CA                   # the undone winner is recorded


def test_no_effect_required_rejected_promotion_reopens(client, tmp_path):
    _write(tmp_path, "rejected", "rev_p", {
        "type": "promote_candidate_node", "status": "rejected", "decided_by": "human",
        "decided_at": "t0", "subject": {"node_id": "cpt_x"},
        "proposal": {"node_type": "concept", "name": "X", "to_status": "active"}, "context": {}})
    r = client.post("/reviews/rev_p/reopen", json={"reason": "rejected by mistake"})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert json.loads((tmp_path / "reviews" / "pending" / "rev_p.json").read_text())["status"] \
        == "pending"


# --- blocked cases ----------------------------------------------------------


def test_unknown_graph_unavailable_blocks(client, tmp_path):
    # approved promotion + NO graph -> projector UNKNOWN -> 409 (repair read model first)
    _write(tmp_path, "approved", "rev_p", {
        "type": "promote_candidate_node", "status": "approved", "decided_by": "human",
        "decided_at": "t0", "subject": {"node_id": "cpt_x"},
        "proposal": {"node_type": "concept", "name": "X", "to_status": "active"}, "context": {}})
    assert not (tmp_path / "db" / "graph.sqlite").exists()
    r = client.post("/reviews/rev_p/reopen", json={"reason": "x"})
    assert r.status_code == 409 and "effect_unknown_repair_read_model" in r.json()["detail"]
    assert (tmp_path / "reviews" / "approved" / "rev_p.json").exists()  # no mutation


def test_effected_supersede_blocks_already_applied(client, tmp_path):
    conn = _graph(tmp_path)
    _claim_nodes(conn, loser_deprecated=True)
    graph.upsert_assertion(conn, src_id=CA, dst_id=CB, edge_type="contradicts",
                           asserted_by="llm", status="active", review_id="rev_c")
    graph.upsert_assertion(conn, src_id=CA, dst_id=CB, edge_type="supersedes",
                           asserted_by="human", status="active", review_id="rev_c")
    conn.commit()
    conn.close()
    _approved_supersede(tmp_path)
    r = client.post("/reviews/rev_c/reopen", json={"reason": "x"})
    assert r.status_code == 409 and "already_applied" in r.json()["detail"]
    assert (tmp_path / "reviews" / "approved" / "rev_c.json").exists()  # no mutation


def test_blank_reason_is_400(client, tmp_path):
    _write(tmp_path, "rejected", "rev_p", {
        "type": "promote_candidate_node", "status": "rejected", "subject": {"node_id": "cpt_x"},
        "proposal": {"node_type": "concept", "name": "X"}, "context": {}})
    assert client.post("/reviews/rev_p/reopen", json={"reason": "   "}).status_code == 400
    assert (tmp_path / "reviews" / "rejected" / "rev_p.json").exists()  # no mutation


def test_non_terminal_item_is_409(client, tmp_path):
    _write(tmp_path, "pending", "rev_q", {
        "type": "promote_candidate_node", "status": "pending", "subject": {"node_id": "cpt_x"},
        "proposal": {}, "context": {}})
    assert client.post("/reviews/rev_q/reopen", json={"reason": "x"}).status_code == 409


def test_missing_review_is_404(client, tmp_path):
    assert client.post("/reviews/nope/reopen", json={"reason": "x"}).status_code == 404


# --- audit sequence never clobbers ------------------------------------------


def test_reopened_audit_seq_does_not_clobber(client, tmp_path):
    # reject -> reopen (reopened-1) -> reject again -> reopen (reopened-2): both audits coexist.
    item = {"type": "promote_candidate_node", "status": "rejected", "decided_by": "human",
            "decided_at": "t0", "subject": {"node_id": "cpt_x"},
            "proposal": {"node_type": "concept", "name": "X"}, "context": {}}
    _write(tmp_path, "rejected", "rev_p", item)
    assert client.post("/reviews/rev_p/reopen", json={"reason": "first"}).status_code == 200
    assert client.post("/reviews/rev_p/reject", json={}).status_code == 200      # re-decide
    assert client.post("/reviews/rev_p/reopen", json={"reason": "second"}).status_code == 200
    names = [p.name for p in _reopened_audits(tmp_path, "rev_p")]
    assert names == ["rev_p-reopened-1.json", "rev_p-reopened-2.json"]           # no clobber
    reasons = {json.loads(p.read_text())["reason"] for p in _reopened_audits(tmp_path, "rev_p")}
    assert reasons == {"first", "second"}


# --- UI ---------------------------------------------------------------------


def test_ui_shows_reopen_form_when_reopenable(client, tmp_path):
    _write(tmp_path, "rejected", "rev_p", {
        "type": "promote_candidate_node", "status": "rejected", "subject": {"node_id": "cpt_x"},
        "proposal": {"node_type": "concept", "name": "X"}, "context": {}})
    body = client.get("/ui/reviews/rev_p").text
    assert "/ui/reviews/rev_p/reopen" in body and "name='reason'" in body and "Reopen" in body


def test_ui_shows_block_reason_when_not_reopenable(client, tmp_path):
    _approved_record_only(tmp_path, "purge_response_cache", rid="rev_x")
    body = client.get("/ui/reviews/rev_x").text
    assert "manual_effect_unknown" in body and "can’t be reopened" in body


def test_ui_reopen_round_trips_to_pending(client, tmp_path):
    _write(tmp_path, "rejected", "rev_p", {
        "type": "promote_candidate_node", "status": "rejected", "subject": {"node_id": "cpt_x"},
        "proposal": {"node_type": "concept", "name": "X"}, "context": {}})
    r = client.post("/ui/reviews/rev_p/reopen", data={"reason": "fix"}, follow_redirects=False)
    assert r.status_code == 303
    assert json.loads((tmp_path / "reviews" / "pending" / "rev_p.json").read_text())["status"] \
        == "pending"
