"""Phase 6 slice 6-4: server-rendered Human Review UI (ADR-0035 A8).

Key-free TestClient tests over the /ui/* routes: queue/detail/apply render, the decision form
Post/Redirect/Get round-trip, HTML error pages (not JSON), the two-step apply, and — load-bearing —
that untrusted review content (incl. nested dict/list markup) is HTML-escaped, never executable.
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


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


def _write_review(tmp_path, state, item):
    d = tmp_path / "reviews" / state
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{item['review_id']}.json").write_text(json.dumps(item), encoding="utf-8")


def _pending_promote(tmp_path, rid="rev_a"):
    _write_review(tmp_path, "pending", {
        "review_id": rid, "type": "promote_candidate_node", "status": "pending", "priority": "low",
        "subject": {"node_id": "cpt_1"}, "proposal": {"to_status": "active", "node_type": "concept"},
        "context": {}})


# --- queue -----------------------------------------------------------------


def test_queue_renders_html(client, tmp_path):
    _pending_promote(tmp_path)
    resp = client.get("/ui/reviews")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "rev_a" in resp.text and "promote_candidate_node" in resp.text
    assert "href='/ui/reviews/rev_a'" in resp.text


def test_queue_default_excludes_deferred(client, tmp_path):
    _write_review(tmp_path, "pending", {
        "review_id": "rev_d", "type": "deprecate_wiki_page", "status": "deferred",
        "subject": {}, "proposal": {}, "context": {}})
    assert "rev_d" not in client.get("/ui/reviews").text
    assert "rev_d" in client.get("/ui/reviews", params={"status": "deferred"}).text


def test_queue_bad_status_is_html_400(client, tmp_path):
    resp = client.get("/ui/reviews", params={"status": "bogus"})
    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("text/html")
    assert "Error 400" in resp.text


# --- detail ----------------------------------------------------------------


def test_detail_renders_preview_and_forms(client, tmp_path):
    _pending_promote(tmp_path)
    resp = client.get("/ui/reviews/rev_a")
    assert resp.status_code == 200
    body = resp.text
    # proposed_action from the projector ("promote candidate -> active"), with > escaped to &gt;
    assert "promote candidate -&gt; active" in body
    assert "effect_status" in body and "pending_apply" in body
    assert "action='/ui/reviews/rev_a/decide'" in body
    assert "value='approve'" in body and "value='reject'" in body and "value='defer'" in body


def test_detail_missing_is_html_404(client, tmp_path):
    resp = client.get("/ui/reviews/rev_nope")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert "Error 404" in resp.text


# --- decision form: Post/Redirect/Get --------------------------------------


def test_decide_approve_redirects_and_records(client, tmp_path):
    _pending_promote(tmp_path)
    resp = client.post("/ui/reviews/rev_a/decide", data={"action": "approve", "note": "ok"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/reviews/rev_a"
    assert (tmp_path / "reviews" / "approved" / "rev_a.json").exists()
    # the redirected-to detail now shows the approved status
    assert "approved" in client.get("/ui/reviews/rev_a").text


def test_decide_defer_then_detail_shows_deferred(client, tmp_path):
    _pending_promote(tmp_path)
    client.post("/ui/reviews/rev_a/decide", data={"action": "defer"}, follow_redirects=False)
    page = tmp_path / "reviews" / "pending" / "rev_a.json"
    assert json.loads(page.read_text())["status"] == "deferred"


def test_decide_flip_is_html_409(client, tmp_path):
    _pending_promote(tmp_path)
    client.post("/ui/reviews/rev_a/decide", data={"action": "approve"}, follow_redirects=False)
    resp = client.post("/ui/reviews/rev_a/decide", data={"action": "reject"}, follow_redirects=False)
    assert resp.status_code == 409
    assert "Error 409" in resp.text


def test_decide_unknown_action_is_html_400(client, tmp_path):
    _pending_promote(tmp_path)
    resp = client.post("/ui/reviews/rev_a/decide", data={"action": "delete"})
    assert resp.status_code == 400 and "Error 400" in resp.text


# --- two-step apply --------------------------------------------------------


def _approved_concept_deprecation(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    conn = graph.connect(gdb)
    graph.upsert_node(conn, node_id="cpt_x", node_type="concept", slug="thing", status="active")
    conn.commit()
    conn.close()
    page = tmp_path / "wiki" / "Concepts" / "thing.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text('---\ntype: concept\nconcept_id: "cpt_x"\ntitle: "Thing"\nstatus: active\n'
                    "review_status: none\naliases: []\n---\n", encoding="utf-8")
    _write_review(tmp_path, "approved", {
        "review_id": "rev_d", "type": "deprecate_wiki_page", "status": "approved",
        "subject": {"node_id": "cpt_x", "page": "Concepts/thing.md"},
        "proposal": {"to_status": "deprecated_candidate", "reason": "x"},
        "context": {"node_type": "concept"}})


def test_apply_confirm_shows_dry_run_preview(client, tmp_path):
    # ADR-0040: GET /ui/reviews/apply now renders the dry-run mutation preview, not a scope-count page.
    _approved_concept_deprecation(tmp_path)
    _write_review(tmp_path, "approved", {
        "review_id": "rev_m", "type": "split_entity", "status": "approved",
        "subject": {}, "proposal": {}, "context": {}})
    body = client.get("/ui/reviews/apply").text
    assert "deprecate_wiki_page" in body          # executor-backed item / blocked
    assert "split_entity" in body                 # executor-backed; empty subject/proposal -> scope-guard skip -> not appliable
    assert "no live state was changed" in body    # framed as a dry-run preview
    assert "Not appliable" in body


def test_apply_post_renders_summary(client, tmp_path):
    _approved_concept_deprecation(tmp_path)
    resp = client.post("/ui/reviews/apply")
    assert resp.status_code == 200
    assert "Apply result" in resp.text and "deprecations" in resp.text


def test_apply_graph_missing_is_html_503(client, tmp_path):
    _write_review(tmp_path, "approved", {
        "review_id": "rev_d", "type": "deprecate_wiki_page", "status": "approved",
        "subject": {"node_id": "cpt_x", "page": "Concepts/thing.md"},
        "proposal": {"to_status": "deprecated_candidate"}, "context": {"node_type": "concept"}})
    resp = client.post("/ui/reviews/apply")
    assert resp.status_code == 503
    assert "Error 503" in resp.text


# --- escaping (the load-bearing safety invariant) --------------------------


_XSS = "<script>alert('x')</script>"


def test_untrusted_content_is_escaped_not_executable(client, tmp_path):
    _write_review(tmp_path, "pending", {
        "review_id": "rev_x", "type": "merge_entities", "status": "pending",
        "subject": {"node_id": "ent_1", "name": _XSS}, "proposal": {"note": _XSS}, "context": {}})
    body = client.get("/ui/reviews/rev_x").text
    assert _XSS not in body                       # never the raw, executable form
    assert "&lt;script&gt;" in body               # escaped instead


def test_nested_details_escaped_recursively(client, tmp_path):
    # dangerous markup buried in a nested dict/list must still be escaped, not stringified raw
    _write_review(tmp_path, "pending", {
        "review_id": "rev_n", "type": "merge_entities", "status": "pending",
        "subject": {"node_id": "ent_1"},
        "proposal": {"deep": {"list": ["<img src=x onerror=alert(1)>", {"k": _XSS}]}},
        "context": {}})
    body = client.get("/ui/reviews/rev_n").text
    assert "<img src=x onerror=alert(1)>" not in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body
    assert _XSS not in body


def test_detail_renders_full_synthesis_proposal_payload(client, tmp_path):
    _write_review(tmp_path, "pending", {
        "review_id": "rev_s", "type": "propose_synthesis", "status": "pending",
        "subject": {"topic_node_id": "cpt_topic"},
        "proposal": {"to_status": "active", "synthesis_id": "syn_1", "title": "Big Finding",
                     "claim_ids": ["clm_1", "clm_2"], "summary": "It all connects."},
        "context": {"node_type": "concept"}})
    body = client.get("/ui/reviews/rev_s").text
    # the curated projection keeps only topic_node_id; the Stored Proposal section shows the rest
    assert "Stored proposal" in body
    assert "Big Finding" in body and "It all connects." in body
    assert "clm_1" in body and "clm_2" in body
    # corrected executor label (no apply_synthesis_decisions wrapper)
    assert "apply_resolved_syntheses" in body and "apply_synthesis_decisions" not in body


def test_detail_renders_contradiction_sides_and_winner(client, tmp_path):
    _write_review(tmp_path, "approved", {
        "review_id": "rev_c", "type": "resolve_contradiction", "status": "approved",
        "subject": {"claim_a": "clm_1", "claim_b": "clm_2"},
        "proposal": {"outcomes": ["acknowledge", "supersede", "reject"],
                     "sides": ["A says up", "B says down"]},
        "context": {"shared_nodes": ["cpt_rev"]},
        "winner": "clm_1", "decided_by": "human"})
    body = client.get("/ui/reviews/rev_c").text
    assert "A says up" in body and "B says down" in body   # proposal.sides
    assert "cpt_rev" in body                                # context.shared_nodes
    assert "clm_1" in body and "other_fields" in body       # top-level winner surfaced generically


def test_terminal_item_hides_decision_forms(client, tmp_path):
    _write_review(tmp_path, "approved", {
        "review_id": "rev_t", "type": "promote_candidate_node", "status": "approved",
        "subject": {"node_id": "cpt_1"}, "proposal": {}, "context": {}, "decided_by": "human"})
    body = client.get("/ui/reviews/rev_t").text
    assert "Decision (recorded)" in body
    assert "/ui/reviews/rev_t/decide" not in body          # no approve/reject/defer form
    # the backend 409 guard still holds even if a form is forged
    assert client.post("/ui/reviews/rev_t/decide", data={"action": "reject"}).status_code == 409


def test_pending_item_still_shows_forms(client, tmp_path):
    _pending_promote(tmp_path, rid="rev_p")
    assert "/ui/reviews/rev_p/decide" in client.get("/ui/reviews/rev_p").text


def test_xss_in_raw_subject_proposal_context_is_escaped(client, tmp_path):
    _write_review(tmp_path, "pending", {
        "review_id": "rev_x2", "type": "promote_candidate_node", "status": "pending",
        "subject": {"node_id": _XSS}, "proposal": {"name": _XSS}, "context": {"src": _XSS}})
    body = client.get("/ui/reviews/rev_x2").text
    assert _XSS not in body and "&lt;script&gt;" in body


def test_no_server_path_leak_in_ui(client, tmp_path):
    _approved_concept_deprecation(tmp_path)
    for url in ("/ui/reviews", "/ui/reviews/rev_d", "/ui/reviews/apply"):
        assert str(tmp_path) not in client.get(url).text
    assert str(tmp_path) not in client.post("/ui/reviews/apply").text
