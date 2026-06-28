"""ADR-0046 semantic-page hiding: hide_semantic_page governance executor.

Extends ADR-0043 source hiding to the concept/entity family via the deprecation render seam
(recompose_semantic_node_page at status='hidden' + review_status='approved'), graph-required, active-only.
Tests the executor, the status vocab, the projector (effect_status), the API apply + summary + stricter
reindex posture, and that hidden semantic pages drop from /query, /search nav + graph channel while raw
/graph/* still returns them.
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
from app.workers import deprecations
from app.workers.wiki_render import parse_frontmatter, render_concept_page

NID = "cpt_aaaaaaaaaaaaaaaa"
SLUG = "thing"
PAGE = f"Concepts/{SLUG}.md"


# --- fixtures ---------------------------------------------------------------


def _graph(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    return graph.connect(gdb)


def _write_concept(tmp_path, conn, *, node_status="active", review_status="approved"):
    page = tmp_path / "wiki" / "Concepts" / f"{SLUG}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(render_concept_page({
        "node_type": "concept", "node_id": NID, "id_field": "concept_id", "title": "Thing",
        "aliases": ["TH"], "confidence": "low", "source_ids": [], "status": node_status,
    }, review_status=review_status), encoding="utf-8")
    graph.upsert_node(conn, node_id=NID, node_type="concept", slug=SLUG, status=node_status)
    return page


def _approve_hide(tmp_path, *, node_id=NID, page=PAGE, node_type="concept", rid="rev_h",
                  to_status="hidden"):
    item = {"review_id": rid, "type": "hide_semantic_page", "status": "approved",
            "subject": {"node_id": node_id, "page": page},
            "proposal": {"to_status": to_status}, "context": {"node_type": node_type}}
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps(item), encoding="utf-8")


def _apply(tmp_path, conn):
    return deprecations.apply_hidden_semantic_pages(
        conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")


def _page_status(tmp_path):
    fm = parse_frontmatter((tmp_path / "wiki" / "Concepts" / f"{SLUG}.md").read_text())
    return fm.get("status"), fm.get("review_status")


# --- executor ---------------------------------------------------------------


def test_apply_hides_active_concept_page_and_graph_node(tmp_path):
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn)
    _approve_hide(tmp_path)
    res = _apply(tmp_path, conn)
    conn.commit()
    assert res["applied"] == 1 and res["normalized"] == 0 and res["graph_changed"] is True
    assert _page_status(tmp_path) == ("hidden", "approved")          # page authority
    assert graph.get_node(conn, NID)["status"] == "hidden"           # graph mirror
    conn.close()


def test_apply_idempotent_noop_when_already_hidden(tmp_path):
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn, node_status="hidden", review_status="approved")
    _approve_hide(tmp_path)
    res = _apply(tmp_path, conn)
    assert res["applied"] == 0 and res["normalized"] == 0 and res["changed_pages"] == []
    conn.close()


def test_apply_normalizes_when_page_and_graph_hidden_but_review_pending(tmp_path):
    # page+graph hidden, but review_status not approved -> a normalization apply (review_status fixed).
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn, node_status="hidden", review_status="pending")
    _approve_hide(tmp_path)
    res = _apply(tmp_path, conn)
    conn.commit()
    assert res["normalized"] == 1 and res["applied"] == 0
    assert _page_status(tmp_path) == ("hidden", "approved")
    conn.close()


def test_active_only_non_active_node_skips_node_not_active(tmp_path):
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn, node_status="deprecated_candidate", review_status="approved")
    _approve_hide(tmp_path)
    res = _apply(tmp_path, conn)
    assert res["applied"] == 0 and res["skipped"] == [{"review_id": "rev_h", "reason": "node_not_active"}]
    assert graph.get_node(conn, NID)["status"] == "deprecated_candidate"   # untouched
    conn.close()


def test_partial_graph_hidden_page_active_skips_no_mutation(tmp_path):
    # Drift: graph node hidden but page still active -> node_not_active skip; the page is NEVER mutated
    # (active-only: a real apply requires the graph node to be active).
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn, node_status="active", review_status="approved")  # page active
    graph.upsert_node(conn, node_id=NID, node_type="concept", slug=SLUG, status="hidden")  # graph hidden
    conn.commit()
    _approve_hide(tmp_path)
    res = _apply(tmp_path, conn)
    assert res["applied"] == 0 and res["normalized"] == 0
    assert res["skipped"] == [{"review_id": "rev_h", "reason": "node_not_active"}]
    assert _page_status(tmp_path) == ("active", "approved")   # page untouched
    conn.close()


def test_partial_page_hidden_graph_active_completes_the_hide(tmp_path):
    # Drift: page hidden but graph still active -> apply completes the transition (graph is active, so the
    # approved hide safely finishes; graph node -> hidden).
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn, node_status="hidden", review_status="approved")  # page hidden
    graph.upsert_node(conn, node_id=NID, node_type="concept", slug=SLUG, status="active")  # graph active
    conn.commit()
    _approve_hide(tmp_path)
    res = _apply(tmp_path, conn)
    conn.commit()
    assert res["applied"] == 1
    assert graph.get_node(conn, NID)["status"] == "hidden"
    conn.close()


def test_node_missing_skips(tmp_path):
    conn = _graph(tmp_path)  # no node, no page
    _approve_hide(tmp_path)
    res = _apply(tmp_path, conn)
    assert res["skipped"] == [{"review_id": "rev_h", "reason": "node_missing"}]
    conn.close()


def test_unexpected_to_status_and_page_node_mismatch_skip(tmp_path):
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn)
    _approve_hide(tmp_path, to_status="deprecated_candidate")   # wrong to_status
    assert _apply(tmp_path, conn)["skipped"] == [
        {"review_id": "rev_h", "reason": "unexpected_to_status"}]
    # page != node's canonical page
    _approve_hide(tmp_path, page="Concepts/wrong.md", rid="rev_h")
    assert _apply(tmp_path, conn)["skipped"] == [
        {"review_id": "rev_h", "reason": "page_node_mismatch"}]
    conn.close()


def test_out_of_scope_claim_page_skips(tmp_path):
    # claim is deferred (fast-follow) -> out_of_scope for the v1 semantic-hide executor.
    conn = _graph(tmp_path)
    graph.upsert_node(conn, node_id="clm_x", node_type="claim", slug="clm_x", status="active")
    conn.commit()
    _approve_hide(tmp_path, node_id="clm_x", page="Claims/clm_x.md", node_type="claim")
    assert _apply(tmp_path, conn)["skipped"] == [{"review_id": "rev_h", "reason": "out_of_scope"}]
    conn.close()


def test_traversal_page_is_invalid_path(tmp_path):
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn)
    _approve_hide(tmp_path, page="Concepts/../../raw/x.md")
    assert _apply(tmp_path, conn)["skipped"] == [{"review_id": "rev_h", "reason": "invalid_page_path"}]
    conn.close()


# --- status vocab -----------------------------------------------------------


def test_hide_semantic_page_in_review_vocab():
    from app.workers import reviews
    assert "hide_semantic_page" in reviews.REVIEW_TYPES


# --- projector --------------------------------------------------------------


def _effect(tmp_path, conn, item_status="approved"):
    item = {"type": "hide_semantic_page", "status": item_status,
            "subject": {"node_id": NID, "page": PAGE}, "context": {"node_type": "concept"}}
    return review_read._effect_hide_semantic(item, conn, tmp_path / "wiki")


def test_projector_effected_requires_page_review_status_and_graph(tmp_path):
    conn = _graph(tmp_path)
    # page hidden + approved + graph hidden -> EFFECTED
    _write_concept(tmp_path, conn, node_status="hidden", review_status="approved")
    assert _effect(tmp_path, conn)[0] == review_read.EFFECTED
    # both hidden but review_status pending -> a PARTIAL live hide -> UNKNOWN (NOT reopen-safe), not
    # plain PENDING_APPLY (ADR-0045 reopen safety).
    _write_concept(tmp_path, conn, node_status="hidden", review_status="pending")
    status, warnings = _effect(tmp_path, conn)
    assert status == review_read.UNKNOWN and warnings == ["partial_hide_state"]
    conn.close()


def test_projector_pending_apply_and_node_not_active_warning(tmp_path):
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn, node_status="active", review_status="approved")
    status, warnings = _effect(tmp_path, conn)
    assert status == review_read.PENDING_APPLY and warnings == []      # active, not yet hidden
    # a non-active node -> node_not_active warning so preview doesn't overpromise
    _write_concept(tmp_path, conn, node_status="deprecated_candidate", review_status="approved")
    assert _effect(tmp_path, conn)[1] == ["node_not_active"]
    conn.close()


@pytest.mark.parametrize("page_status,graph_status", [("hidden", "active"), ("active", "hidden")])
def test_projector_partial_live_hide_is_unknown_not_reopenable(tmp_path, page_status, graph_status):
    # A partial live hide (page XOR graph hidden) -> UNKNOWN partial_hide_state (not PENDING_APPLY), so
    # the ADR-0045 reopen gate blocks it (a hide effect is already live).
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn, node_status=page_status, review_status="approved")
    graph.upsert_node(conn, node_id=NID, node_type="concept", slug=SLUG, status=graph_status)
    conn.commit()
    status, warnings = _effect(tmp_path, conn)
    assert status == review_read.UNKNOWN and warnings == ["partial_hide_state"]
    assert review_read.reopen_block_reason(status) is not None     # not reopenable
    conn.close()


def test_projector_rejected_is_no_effect_required(tmp_path):
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn)
    assert _effect(tmp_path, conn, item_status="rejected")[0] == review_read.NO_EFFECT_REQUIRED
    conn.close()


def test_projector_graph_unavailable_is_unknown(tmp_path):
    _write_concept(tmp_path, _graph(tmp_path))
    assert _effect(tmp_path, None)[0] == review_read.UNKNOWN


# --- API: apply + summary + graph-required + reindex posture ----------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


def _live_concept(tmp_path):
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn)
    conn.commit()
    conn.close()


def _setup_state(tmp_path, *, page_status, page_review, graph_status, rid="rev_h"):
    # Write the page at page_status (+ page_review) and the graph node at graph_status (override), then
    # file an APPROVED hide_semantic_page item. Used to construct partial / clean hide states.
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn, node_status=page_status, review_status=page_review)
    graph.upsert_node(conn, node_id=NID, node_type="concept", slug=SLUG, status=graph_status)
    conn.commit()
    conn.close()
    _approve_hide(tmp_path, rid=rid)


@pytest.mark.parametrize("page_status,graph_status", [("hidden", "active"), ("active", "hidden")])
def test_reopen_blocked_for_partial_live_hide_409(client, tmp_path, page_status, graph_status):
    # ADR-0045 reopen safety: a partial live hide (page or graph already hidden) is NOT reopenable —
    # reopening would orphan the hidden page/node. POST /reviews/{id}/reopen -> 409, no ledger mutation.
    _setup_state(tmp_path, page_status=page_status, page_review="approved", graph_status=graph_status)
    r = client.post("/reviews/rev_h/reopen", json={"reason": "undo"})
    assert r.status_code == 409 and "effect_unknown_repair_read_model" in r.json()["detail"]
    assert (tmp_path / "reviews" / "approved" / "rev_h.json").exists()      # no mutation


def test_reopen_blocked_for_both_hidden_review_pending_409(client, tmp_path):
    _setup_state(tmp_path, page_status="hidden", page_review="pending", graph_status="hidden")
    assert client.post("/reviews/rev_h/reopen", json={"reason": "undo"}).status_code == 409


def test_reopen_allowed_for_cleanly_unapplied_hide(client, tmp_path):
    # Neither page nor graph hidden -> PENDING_APPLY -> reopenable (no live effect to orphan).
    _setup_state(tmp_path, page_status="active", page_review="approved", graph_status="active")
    r = client.post("/reviews/rev_h/reopen", json={"reason": "changed my mind"})
    assert r.status_code == 200 and r.json()["status"] == "pending"


def test_api_apply_hides_semantic_page_and_reports_summary(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module.retention, "reindex_keyword", lambda root: None)
    _live_concept(tmp_path)
    _approve_hide(tmp_path)
    body = client.post("/reviews/apply").json()
    assert body["status"] == "applied"
    assert body["summary"]["semantic_hidden"]["applied"] == 1
    assert _page_status(tmp_path) == ("hidden", "approved")


def test_graph_only_hide_completion_triggers_reindex(client, tmp_path, monkeypatch):
    # A page-hidden/graph-active partial state completes by flipping ONLY the graph node (page render
    # unchanged -> empty changed_pages). Reindex must STILL run (else a stale nav index keeps surfacing
    # the now-hidden node). Proven by the reindex spy being called despite no page write.
    called = []
    monkeypatch.setattr(main_module.retention, "reindex_keyword", lambda root: called.append(root))
    _setup_state(tmp_path, page_status="hidden", page_review="approved", graph_status="active")
    body = client.post("/reviews/apply").json()
    assert body["summary"]["semantic_hidden"]["applied"] == 1   # graph-only completion (page already hidden)
    assert called                                                # reindex attempted despite no page write


def test_graph_only_hide_completion_reindex_failure_is_non_clean(client, tmp_path, monkeypatch):
    # Same graph-only completion, but reindex fails -> non-clean + the suppression warning (which can
    # only fire because reindex was ATTEMPTED — the bug was that it was skipped, hiding the staleness).
    def boom(root):
        raise RuntimeError("reindex blew up")
    monkeypatch.setattr(main_module.retention, "reindex_keyword", boom)
    _setup_state(tmp_path, page_status="hidden", page_review="approved", graph_status="active")
    body = client.post("/reviews/apply").json()
    assert body["status"] == "validation_failed"
    assert "semantic_hide_retrieval_suppression_not_guaranteed" in body["warnings"]


def test_api_apply_graph_required_503_when_graph_absent(client, tmp_path):
    # approved hide_semantic_page with NO graph -> graph-required gate -> 503
    _approve_hide(tmp_path)
    assert not (tmp_path / "db" / "graph.sqlite").exists()
    assert client.post("/reviews/apply").status_code == 503


def test_hidden_semantic_page_index_row_is_suppressed(client, tmp_path):
    # After hide, the navigation index row for the concept reads status=hidden + answer_eligible=0 — the
    # data the default RETENTION_DEFAULT node-status filter uses to drop it from /search nav (the
    # end-to-end /search exclusion is asserted separately). (/query cites source chunks, not semantic
    # pages, ADR-0034 — so concepts are not /query citations regardless of status.)
    import sqlite3

    from app.backend import keyword_index
    _live_concept(tmp_path)
    keyword_index.reindex(tmp_path, force=True)
    idx = tmp_path / "indexes" / "keyword" / "keyword.sqlite"

    def nav_row():
        c = sqlite3.connect(idx)
        try:
            return c.execute(
                "SELECT status, answer_eligible FROM navigation WHERE node_id = ?", (NID,)).fetchone()
        finally:
            c.close()

    assert nav_row() == ("active", "1")          # baseline: discoverable + citable
    _approve_hide(tmp_path)                       # apply runs the real reindex_keyword
    assert client.post("/reviews/apply").json()["status"] == "applied"
    assert nav_row() == ("hidden", "0")          # status-filtered out of default retrieval + answer-ineligible
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert graph.get_node(gconn, NID)["status"] == "hidden"   # raw graph inspection still returns it
    gconn.close()


def test_hidden_semantic_page_excluded_from_search_navigation_end_to_end(client, tmp_path):
    # End-to-end /search: a hidden concept's navigation row is excluded by default; an explicit
    # node_status=hidden surfaces it again (mirrors the source-hide round-trip).
    from app.backend import keyword_index
    _live_concept(tmp_path)
    keyword_index.reindex(tmp_path, force=True)
    nav = lambda r: {n.get("node_id") for n in r.json()["navigation"]}  # noqa: E731
    q = {"q": "Thing", "mode": "navigation"}
    assert NID in nav(client.get("/search", params=q))                         # baseline (title "Thing")
    _approve_hide(tmp_path)
    assert client.post("/reviews/apply").json()["status"] == "applied"          # runs the real reindex
    assert NID not in nav(client.get("/search", params=q))                      # default excludes it
    explicit = client.get("/search", params={**q, "node_status": "hidden"})
    assert NID in nav(explicit)                                                 # explicit node_status surfaces it


def test_hidden_semantic_node_excluded_from_search_graph_channel(client, tmp_path):
    # End-to-end /search graph channel: after hide, the concept node is excluded as a graph adjacent by
    # default (search_subgraph node_status filter); explicit include surfaces it; raw inspection sees it.
    from app.backend import graph_read, search
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn)
    seed = "cpt_ssssssssssssssss"
    graph.upsert_node(conn, node_id=seed, node_type="concept", slug="seed", status="active")
    graph.upsert_assertion(conn, src_id=seed, dst_id=NID, edge_type="related_to",
                           asserted_by="deterministic", status="active")
    conn.commit()
    conn.close()
    _approve_hide(tmp_path)
    assert client.post("/reviews/apply").json()["status"] == "applied"          # NID -> hidden
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    default = graph_read.search_subgraph(gconn, [seed], depth=1,
                                         node_statuses=search.RETENTION_DEFAULT_STATUSES,
                                         node_cap=50, edge_cap=50)
    assert NID not in {n["node_id"] for n in default["nodes"]}                  # graph channel excludes hidden
    incl = graph_read.search_subgraph(gconn, [seed], depth=1, node_statuses=("active", "hidden"),
                                      node_cap=50, edge_cap=50)
    assert NID in {n["node_id"] for n in incl["nodes"]}                         # explicit include surfaces it
    assert graph.get_node(gconn, NID)["status"] == "hidden"                     # raw inspection still sees it
    gconn.close()


def test_api_reindex_failure_makes_semantic_hide_non_clean(client, tmp_path, monkeypatch):
    def boom(root):
        raise RuntimeError("reindex blew up")
    monkeypatch.setattr(main_module.retention, "reindex_keyword", boom)
    _live_concept(tmp_path)
    _approve_hide(tmp_path)
    body = client.post("/reviews/apply").json()
    assert body["status"] == "validation_failed"
    assert "semantic_hide_retrieval_suppression_not_guaranteed" in body["warnings"]
    assert _page_status(tmp_path) == ("hidden", "approved")   # mutation still written
