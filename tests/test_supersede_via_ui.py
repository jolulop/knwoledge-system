"""ADR-0044 supersede-via-UI: recording the contradiction winner as an approve sub-outcome.

The supersede executor (apply_resolved_contradictions) is reused verbatim; these tests cover the NEW
code — recording + validating `winner` on the approve decision, the audit trail, the UI button
translation — plus a worker-level proof that a recorded winner drives the existing supersede.
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
from app.workers import claims, contradictions

CA = "clm_aaaaaaaaaaaaaaaa"
CB = "clm_bbbbbbbbbbbbbbbb"


# --- helpers ---------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


def _claim_page(tmp_path: Path, cid: str, status: str = "active") -> Path:
    page = tmp_path / "wiki" / "Claims" / f"{cid}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f'---\ntype: claim\nclaim_id: "{cid}"\nstatus: {status}\nreview_status: none\n'
                    "---\n\n## Claim\n\nA claim.\n", encoding="utf-8")
    return page


def _pending_contradiction(tmp_path: Path, a: str = CA, b: str = CB, rid: str = "rev_c") -> None:
    d = tmp_path / "reviews" / "pending"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": "resolve_contradiction", "status": "pending", "priority": "medium",
        "subject": {"claim_a": a, "claim_b": b},
        "proposal": {"outcomes": ["acknowledge", "supersede", "reject"], "confidence": 0.9,
                     "explanation": "Opposite revenue direction.", "sides": ["ctx a", "ctx b"]},
        "context": {"shared_nodes": []}}), encoding="utf-8")


def _approved_item(tmp_path: Path, rid: str = "rev_c") -> dict:
    return json.loads((tmp_path / "reviews" / "approved" / f"{rid}.json").read_text())


def _audit(tmp_path: Path, rid: str = "rev_c") -> dict:
    return json.loads((tmp_path / "reviews" / "audit_log" / f"{rid}-approved.json").read_text())


def test_is_claim_id_shape():
    assert claims.is_claim_id(CA) and claims.is_claim_id("clm_0123456789abcdef")
    for bad in ("not-a-claim", "foo/bar", "clm_xyz", "clm_" + "a" * 17, "cpt_aaaaaaaaaaaaaaaa", "", None,
                # exact-match boundaries: trailing newline/space/slash must NOT pass (fullmatch, not $)
                "clm_aaaaaaaaaaaaaaaa\n", "clm_aaaaaaaaaaaaaaaa ", "clm_aaaaaaaaaaaaaaaa/",
                " clm_aaaaaaaaaaaaaaaa", "clm_AAAAAAAAAAAAAAAA"):
        assert not claims.is_claim_id(bad), bad
    assert claims.is_claim_id(claims.claim_id("any claim text"))  # the generator's output is canonical


# --- recording + audit -----------------------------------------------------


def test_approve_with_winner_persists_to_item_and_audit(client, tmp_path):
    _claim_page(tmp_path, CA)
    _claim_page(tmp_path, CB)
    _pending_contradiction(tmp_path)
    r = client.post(f"/reviews/{'rev_c'}/approve", json={"winner": CA})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert _approved_item(tmp_path)["winner"] == CA          # executor reads this
    assert _audit(tmp_path)["winner"] == CA                  # immutable audit trail


def test_approve_acknowledge_has_no_winner(client, tmp_path):
    _claim_page(tmp_path, CA)
    _claim_page(tmp_path, CB)
    _pending_contradiction(tmp_path)
    client.post("/reviews/rev_c/approve", json={})           # acknowledge
    assert "winner" not in _approved_item(tmp_path)
    assert "winner" not in _audit(tmp_path)


# --- decision-time guards --------------------------------------------------


@pytest.mark.parametrize("winner", ["clm_dddddddddddddddd", "../escape", "not-a-claim"])
def test_winner_outside_pair_or_noncanonical_is_400(client, tmp_path, winner):
    _claim_page(tmp_path, CA)
    _claim_page(tmp_path, CB)
    _pending_contradiction(tmp_path)
    assert client.post("/reviews/rev_c/approve", json={"winner": winner}).status_code == 400


def test_tampered_noncanonical_subject_and_winner_is_400_no_mutation(client, tmp_path):
    # Untrusted ledger: a tampered subject claim_a == winner == "not-a-claim" must be 400 on the
    # canonical-shape gate, EVEN IF a matching active page exists (the shape check is first).
    (tmp_path / "wiki" / "Claims").mkdir(parents=True, exist_ok=True)
    (tmp_path / "wiki" / "Claims" / "not-a-claim.md").write_text(
        '---\ntype: claim\nclaim_id: "not-a-claim"\nstatus: active\nreview_status: none\n---\n',
        encoding="utf-8")
    d = tmp_path / "reviews" / "pending"
    d.mkdir(parents=True, exist_ok=True)
    (d / "rev_c.json").write_text(json.dumps({
        "review_id": "rev_c", "type": "resolve_contradiction", "status": "pending",
        "subject": {"claim_a": "not-a-claim", "claim_b": CB}, "proposal": {}, "context": {}}),
        encoding="utf-8")
    assert client.post("/reviews/rev_c/approve", json={"winner": "not-a-claim"}).status_code == 400
    assert (tmp_path / "reviews" / "pending" / "rev_c.json").exists()        # no ledger mutation
    assert not (tmp_path / "reviews" / "approved" / "rev_c.json").exists()


def test_path_ish_id_is_400_without_page_read(client, tmp_path):
    # A path-ish claim id is rejected by the shape gate before any wiki/Claims read.
    _pending_contradiction(tmp_path, a="foo/bar", b=CB)
    assert client.post("/reviews/rev_c/approve", json={"winner": "foo/bar"}).status_code == 400


def test_malformed_pair_one_noncanonical_is_400(client, tmp_path):
    # The pair itself is malformed (one non-canonical id) even though the winner is canonical -> 400.
    _claim_page(tmp_path, CA)
    _pending_contradiction(tmp_path, a=CA, b="bogus")
    assert client.post("/reviews/rev_c/approve", json={"winner": CA}).status_code == 400


def test_ui_supersede_on_missing_review_is_404(client, tmp_path):
    r = client.post("/ui/reviews/nope/decide", data={"action": "supersede_a"}, follow_redirects=False)
    assert r.status_code == 404


def test_winner_on_reject_or_defer_is_400(client, tmp_path):
    _claim_page(tmp_path, CA)
    _claim_page(tmp_path, CB)
    _pending_contradiction(tmp_path)
    assert client.post("/reviews/rev_c/reject", json={"winner": CA}).status_code == 400
    assert client.post("/reviews/rev_c/defer", json={"winner": CA}).status_code == 400


def test_winner_on_non_contradiction_type_is_400(client, tmp_path):
    d = tmp_path / "reviews" / "pending"
    d.mkdir(parents=True, exist_ok=True)
    (d / "rev_p.json").write_text(json.dumps({
        "review_id": "rev_p", "type": "promote_candidate_node", "status": "pending",
        "subject": {"node_id": "cpt_x"}, "proposal": {}, "context": {}}), encoding="utf-8")
    assert client.post("/reviews/rev_p/approve", json={"winner": CA}).status_code == 400


def test_inactive_or_missing_claim_is_409_no_ledger_mutation(client, tmp_path):
    _claim_page(tmp_path, CA, status="deprecated_candidate")   # not active
    # CB page intentionally absent
    _pending_contradiction(tmp_path)
    r = client.post("/reviews/rev_c/approve", json={"winner": CA})
    assert r.status_code == 409
    # no ledger mutation: still pending, not moved to approved/, no audit
    assert (tmp_path / "reviews" / "pending" / "rev_c.json").exists()
    assert not (tmp_path / "reviews" / "approved" / "rev_c.json").exists()
    assert not (tmp_path / "reviews" / "audit_log").exists()


def test_decision_endpoint_does_not_require_graph(client, tmp_path):
    # No db/graph.sqlite exists; an approve-with-winner must still succeed (page-frontmatter authority).
    _claim_page(tmp_path, CA)
    _claim_page(tmp_path, CB)
    _pending_contradiction(tmp_path)
    assert not (tmp_path / "db" / "graph.sqlite").exists()
    assert client.post("/reviews/rev_c/approve", json={"winner": CA}).status_code == 200


def test_reapprove_with_different_winner_is_noop(client, tmp_path):
    _claim_page(tmp_path, CA)
    _claim_page(tmp_path, CB)
    _pending_contradiction(tmp_path)
    client.post("/reviews/rev_c/approve", json={"winner": CA})
    r2 = client.post("/reviews/rev_c/approve", json={"winner": CB})   # terminal re-send
    assert r2.status_code == 200 and r2.json()["decision_recorded"] is False
    assert _approved_item(tmp_path)["winner"] == CA                   # original winner unchanged


# --- UI --------------------------------------------------------------------


def test_ui_renders_five_contradiction_buttons(client, tmp_path):
    _claim_page(tmp_path, CA)
    _claim_page(tmp_path, CB)
    _pending_contradiction(tmp_path)
    body = client.get("/ui/reviews/rev_c").text
    for value in ("acknowledge", "supersede_a", "supersede_b", "reject", "defer"):
        assert f"value='{value}'" in body
    assert "Supersede is terminal" in body


def test_ui_supersede_action_maps_to_correct_claim(client, tmp_path):
    _claim_page(tmp_path, CA)
    _claim_page(tmp_path, CB)
    _pending_contradiction(tmp_path)
    # PRG redirect (303) on success; the recorded winner = claim_b for supersede_b
    r = client.post("/ui/reviews/rev_c/decide", data={"action": "supersede_b"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert _approved_item(tmp_path)["winner"] == CB


# --- worker: a recorded winner drives the (reused) supersede executor -------


def test_recorded_winner_drives_supersede_execution(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    conn = graph.connect(gdb)
    graph.upsert_node(conn, node_id=CA, node_type="claim", slug=CA, status="active")
    graph.upsert_node(conn, node_id=CB, node_type="claim", slug=CB, status="active")
    graph.upsert_assertion(conn, src_id=CA, dst_id=CB, edge_type="contradicts",  # canonical CA < CB
                           asserted_by="llm", status="proposed", review_id="rev_c")
    conn.commit()
    _claim_page(tmp_path, CA)
    _claim_page(tmp_path, CB)
    # an APPROVED resolve_contradiction with winner=CA (loser CB) — what the UI records
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / "rev_c.json").write_text(json.dumps({
        "review_id": "rev_c", "type": "resolve_contradiction", "status": "approved",
        "subject": {"claim_a": CA, "claim_b": CB}, "winner": CA,
        "proposal": {}, "context": {}}), encoding="utf-8")

    res = contradictions.apply_resolved_contradictions(
        conn, tmp_path / "reviews", claims_dir=tmp_path / "wiki" / "Claims",
        markdown_dir=tmp_path / "normalized" / "markdown")
    conn.commit()
    # The recorded winner drove the (unchanged) supersede executor: a supersedes(winner CA -> loser CB)
    # active edge is written and the contradicts edge stays active for the historical record. (The
    # loser-deprecation effect itself is covered with full claim evidence in test_contradictions.)
    assert res["superseded_executed"] == 1
    sup = conn.execute("SELECT src_id, dst_id, status FROM edges WHERE edge_type='supersedes'").fetchone()
    assert (sup["src_id"], sup["dst_id"], sup["status"]) == (CA, CB, "active")
    contra = conn.execute("SELECT status FROM edges WHERE edge_type='contradicts'").fetchone()["status"]
    assert contra == "active"
    conn.close()
