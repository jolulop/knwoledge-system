"""ADR-0047 unhide: governed effect-reversal of a live hide (hidden -> active).

The inverse of ADR-0043/0046 hides, reusing the same executors/projector machinery:
- source unhide (unhide_content): manifest authority, NOT graph-required, EFFECTED iff manifest active;
- semantic unhide (unhide_semantic_page): page + graph via recompose, graph-required, EFFECTED iff page
  AND graph active, partial live unhide -> UNKNOWN partial_unhide_state (NOT reopen-safe).
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.backend import graph
from app.backend import main as main_module
from app.backend import manifests
from app.backend import review_read
from app.backend.config import get_settings
from app.workers import deprecations, retention, wiki
from app.workers.wiki_render import parse_frontmatter, render_concept_page

SID = "src_0123456789abcdef"
OLD = "2000-01-01T00:00:00+00:00"
NID = "cpt_aaaaaaaaaaaaaaaa"
SLUG = "thing"
PAGE = f"Concepts/{SLUG}.md"


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


# --- source unhide ---------------------------------------------------------


def _hidden_source(tmp_path, sid=SID):
    shutil.copytree(ROOT / "templates", tmp_path / "templates", dirs_exist_ok=True)
    md = tmp_path / "raw" / "manifests"
    md.mkdir(parents=True, exist_ok=True)
    rel = f"raw/inbox/{sid}.md"
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text("raw bytes", encoding="utf-8")
    (md / f"{sid}.json").write_text(json.dumps({
        "source_id": sid, "sha256": sid + "0" * 8, "relative_raw_path": rel, "file_extension": ".md",
        "chunk_count": 1, "ingestion_status": "extracted", "status": "hidden",
        "normalized": {"markdown_path": f"normalized/markdown/{sid}.md"},
        "created_at": OLD, "discovered_at": OLD, "modified_at": OLD,
        "retention_class": "permanent", "occurrences": [{"relative_path": rel}]}), encoding="utf-8")
    norm = tmp_path / "normalized" / "markdown" / f"{sid}.md"
    norm.parent.mkdir(parents=True, exist_ok=True)
    norm.write_text(f"# {sid}\n\nProse.\n", encoding="utf-8")
    wiki.generate_wiki(tmp_path, source_ids=[sid], rebuild_index=False, record_job=False)
    return md


def _approve_unhide_source(tmp_path, sid=SID, rid="rev_u"):
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": "unhide_content", "status": "approved",
        "subject": {"source_id": sid}, "proposal": {"to_status": "active", "reason": "ok to surface"},
        "context": {}}), encoding="utf-8")


def _apply_source(tmp_path):
    return retention.apply_unhidden_sources(
        tmp_path, manifests_dir=tmp_path / "raw" / "manifests", reviews_dir=tmp_path / "reviews",
        wiki_dir=tmp_path / "wiki", graph_db=tmp_path / "db" / "graph.sqlite")


def test_source_unhide_flips_hidden_to_active(tmp_path):
    md = _hidden_source(tmp_path)
    _approve_unhide_source(tmp_path)
    res = _apply_source(tmp_path)
    assert res["applied"] == 1
    assert manifests.get_status(manifests.load_manifest(md, SID)) == "active"        # manifest authority
    assert parse_frontmatter((tmp_path / "wiki" / "Sources" / f"{SID}.md").read_text())["status"] \
        == "active"                                                                  # page mirror


def test_source_unhide_already_active_is_silent_noop(tmp_path):
    md = _hidden_source(tmp_path)
    manifests.set_status(md, SID, "active")          # already at target -> idempotent
    _approve_unhide_source(tmp_path)
    res = _apply_source(tmp_path)
    assert res["applied"] == 0 and res["skipped"] == []   # silent no-op, NOT a skip


def test_source_unhide_third_state_is_source_not_hidden_skip(tmp_path):
    # a non-hidden, non-active source (archive_candidate) can't unhide -> TYPED operator-visible skip
    # (ADR-0047), not a silent continue.
    md = _hidden_source(tmp_path)
    manifests.set_status(md, SID, "archive_candidate")
    _approve_unhide_source(tmp_path)
    res = _apply_source(tmp_path)
    assert res["applied"] == 0
    assert res["skipped"] == [{"review_id": "rev_u", "reason": "source_not_hidden"}]
    assert manifests.get_status(manifests.load_manifest(md, SID)) == "archive_candidate"  # untouched


def test_source_unhide_not_graph_required(tmp_path):
    # No graph db -> source unhide still applies (manifest authority; graph mirror best-effort).
    _hidden_source(tmp_path)
    _approve_unhide_source(tmp_path)
    assert not (tmp_path / "db" / "graph.sqlite").exists()
    assert _apply_source(tmp_path)["applied"] == 1


def _effect_src(tmp_path, status="approved"):
    item = {"type": "unhide_content", "status": status, "subject": {"source_id": SID}}
    return review_read._effect_unhide_content(item, tmp_path / "wiki", tmp_path / "raw" / "manifests")


def test_source_unhide_projector(tmp_path):
    md = _hidden_source(tmp_path)
    assert _effect_src(tmp_path)[0] == review_read.PENDING_APPLY      # manifest hidden -> not yet unhidden
    manifests.set_status(md, SID, "active")
    assert _effect_src(tmp_path)[0] == review_read.EFFECTED           # manifest active -> unhidden
    manifests.set_status(md, SID, "archive_candidate")
    status, warnings = _effect_src(tmp_path)
    assert status == review_read.PENDING_APPLY and "source_not_hidden" in warnings  # non-hidden -> flagged
    assert _effect_src(tmp_path, status="rejected")[0] == review_read.NO_EFFECT_REQUIRED


# --- semantic unhide -------------------------------------------------------


def _graph(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    return graph.connect(gdb)


def _write_concept(tmp_path, conn, *, node_status, review_status):
    page = tmp_path / "wiki" / "Concepts" / f"{SLUG}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(render_concept_page({
        "node_type": "concept", "node_id": NID, "id_field": "concept_id", "title": "Thing",
        "aliases": ["TH"], "confidence": "low", "source_ids": [], "status": node_status,
    }, review_status=review_status), encoding="utf-8")
    graph.upsert_node(conn, node_id=NID, node_type="concept", slug=SLUG, status=node_status)


def _approve_unhide_semantic(tmp_path, rid="rev_u"):
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": "unhide_semantic_page", "status": "approved",
        "subject": {"node_id": NID, "page": PAGE}, "proposal": {"to_status": "active"},
        "context": {"node_type": "concept"}}), encoding="utf-8")


def _apply_semantic(tmp_path, conn):
    return deprecations.apply_unhidden_semantic_pages(conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")


def _page_status(tmp_path):
    fm = parse_frontmatter((tmp_path / "wiki" / "Concepts" / f"{SLUG}.md").read_text())
    return fm.get("status"), fm.get("review_status")


def test_semantic_unhide_flips_hidden_to_active_default(tmp_path):
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn, node_status="hidden", review_status="approved")
    _approve_unhide_semantic(tmp_path)
    res = _apply_semantic(tmp_path, conn)
    conn.commit()
    assert res["applied"] == 1
    assert _page_status(tmp_path) == ("active", "none")              # clean default active state
    assert graph.get_node(conn, NID)["status"] == "active"           # graph mirror
    conn.close()


def test_semantic_unhide_idempotent_when_already_active(tmp_path):
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn, node_status="active", review_status="none")
    _approve_unhide_semantic(tmp_path)
    assert _apply_semantic(tmp_path, conn)["applied"] == 0           # already active -> no-op
    conn.close()


def test_semantic_unhide_completes_page_hidden_graph_hidden(tmp_path):
    # the normal case: page+graph hidden -> both flip to active
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn, node_status="hidden", review_status="approved")
    _approve_unhide_semantic(tmp_path)
    assert _apply_semantic(tmp_path, conn)["applied"] == 1
    conn.close()


def test_semantic_unhide_graph_active_page_hidden_skips_node_not_hidden(tmp_path):
    # drift: graph already active but page hidden -> graph is not in from-status (hidden) -> skip, never
    # mutate (mirrors hide's node_not_active for the inverse drift).
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn, node_status="hidden", review_status="approved")  # page hidden
    graph.upsert_node(conn, node_id=NID, node_type="concept", slug=SLUG, status="active")  # graph active
    conn.commit()
    _approve_unhide_semantic(tmp_path)
    res = _apply_semantic(tmp_path, conn)
    assert res["applied"] == 0 and res["skipped"] == [{"review_id": "rev_u", "reason": "node_not_hidden"}]
    conn.close()


def _effect_sem(tmp_path, conn, status="approved"):
    item = {"type": "unhide_semantic_page", "status": status,
            "subject": {"node_id": NID, "page": PAGE}, "context": {"node_type": "concept"}}
    return review_read._effect_unhide_semantic(item, conn, tmp_path / "wiki")


def test_semantic_unhide_projector_effected_pending_and_partial(tmp_path):
    conn = _graph(tmp_path)
    # both active -> EFFECTED
    _write_concept(tmp_path, conn, node_status="active", review_status="none")
    assert _effect_sem(tmp_path, conn)[0] == review_read.EFFECTED
    # both hidden -> PENDING_APPLY (still hidden; reopenable)
    _write_concept(tmp_path, conn, node_status="hidden", review_status="approved")
    assert _effect_sem(tmp_path, conn)[0] == review_read.PENDING_APPLY
    conn.close()


@pytest.mark.parametrize("page_status,graph_status", [("active", "hidden"), ("hidden", "active")])
def test_semantic_unhide_projector_partial_is_unknown(tmp_path, page_status, graph_status):
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn, node_status=page_status, review_status="none")
    graph.upsert_node(conn, node_id=NID, node_type="concept", slug=SLUG, status=graph_status)
    conn.commit()
    status, warnings = _effect_sem(tmp_path, conn)
    assert status == review_read.UNKNOWN and warnings == ["partial_unhide_state"]
    assert review_read.reopen_block_reason(status) is not None      # not reopenable
    conn.close()


def test_semantic_unhide_projector_rejected_and_graph_unavailable(tmp_path):
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn, node_status="hidden", review_status="approved")
    assert _effect_sem(tmp_path, conn, status="rejected")[0] == review_read.NO_EFFECT_REQUIRED
    conn.close()
    assert _effect_sem(tmp_path, None)[0] == review_read.UNKNOWN     # graph unavailable


# --- reopen safety (inverse of partial_hide_state) -------------------------


def _setup_sem_state(tmp_path, *, page_status, graph_status, review_status="none", rid="rev_u"):
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn, node_status=page_status, review_status=review_status)
    graph.upsert_node(conn, node_id=NID, node_type="concept", slug=SLUG, status=graph_status)
    conn.commit()
    conn.close()
    _approve_unhide_semantic(tmp_path, rid=rid)


@pytest.mark.parametrize("page_status,graph_status", [("active", "hidden"), ("hidden", "active")])
def test_reopen_blocked_for_partial_unhide(client, tmp_path, page_status, graph_status):
    _setup_sem_state(tmp_path, page_status=page_status, graph_status=graph_status)
    r = client.post("/reviews/rev_u/reopen", json={"reason": "undo"})
    assert r.status_code == 409 and "effect_unknown_repair_read_model" in r.json()["detail"]
    assert (tmp_path / "reviews" / "approved" / "rev_u.json").exists()   # no mutation


def test_reopen_allowed_for_fully_hidden_unhide(client, tmp_path):
    # still fully hidden -> PENDING_APPLY -> reopenable (no restoration effect live; the prior hide stays)
    _setup_sem_state(tmp_path, page_status="hidden", graph_status="hidden", review_status="approved")
    r = client.post("/reviews/rev_u/reopen", json={"reason": "changed my mind"})
    assert r.status_code == 200 and r.json()["status"] == "pending"


# --- API: apply + summary + graph-required + reindex posture ---------------


def test_api_apply_unhides_source_and_semantic_with_summary(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module.retention, "reindex_keyword", lambda root: None)
    _hidden_source(tmp_path)
    _approve_unhide_source(tmp_path, rid="rev_us")
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn, node_status="hidden", review_status="approved")
    conn.commit()
    conn.close()
    _approve_unhide_semantic(tmp_path, rid="rev_usem")
    body = client.post("/reviews/apply").json()
    assert body["status"] == "applied"
    assert body["summary"]["unhidden"]["applied"] == 1
    assert body["summary"]["semantic_unhidden"]["applied"] == 1


def test_graph_only_unhide_completion_triggers_reindex(client, tmp_path, monkeypatch):
    # Inverse of the hide graph-only-completion case: a page-active/graph-hidden state completes by flipping
    # ONLY the graph node (page render unchanged -> empty changed_pages). Reindex must STILL run, else a
    # stale nav index keeps hiding the now-active node. Proven by the reindex spy being called.
    called = []
    monkeypatch.setattr(main_module.retention, "reindex_keyword", lambda root: called.append(root))
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn, node_status="active", review_status="none")   # page already active
    graph.upsert_node(conn, node_id=NID, node_type="concept", slug=SLUG, status="hidden")  # graph hidden
    conn.commit()
    conn.close()
    _approve_unhide_semantic(tmp_path)
    body = client.post("/reviews/apply").json()
    assert body["summary"]["semantic_unhidden"]["applied"] == 1   # graph-only completion (page already active)
    assert called                                                 # reindex attempted despite no page write
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert graph.get_node(gconn, NID)["status"] == "active"
    gconn.close()


def test_api_semantic_unhide_graph_required_503(client, tmp_path):
    _approve_unhide_semantic(tmp_path)
    assert not (tmp_path / "db" / "graph.sqlite").exists()
    assert client.post("/reviews/apply").status_code == 503


def test_api_unhide_reindex_failure_is_non_clean(client, tmp_path, monkeypatch):
    def boom(root):
        raise RuntimeError("reindex blew up")
    monkeypatch.setattr(main_module.retention, "reindex_keyword", boom)
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn, node_status="hidden", review_status="approved")
    conn.commit()
    conn.close()
    _approve_unhide_semantic(tmp_path)
    body = client.post("/reviews/apply").json()
    assert body["status"] == "validation_failed"
    assert "unhide_discovery_restoration_not_guaranteed" in body["warnings"]


def test_unhidden_semantic_page_reenters_search_navigation(client, tmp_path):
    from app.backend import keyword_index
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn, node_status="hidden", review_status="approved")
    conn.commit()
    conn.close()
    keyword_index.reindex(tmp_path, force=True)
    nav = lambda r: {n.get("node_id") for n in r.json()["navigation"]}  # noqa: E731
    q = {"q": "Thing", "mode": "navigation"}
    assert NID not in nav(client.get("/search", params=q))            # baseline: hidden, excluded
    _approve_unhide_semantic(tmp_path)
    assert client.post("/reviews/apply").json()["status"] == "applied"
    assert NID in nav(client.get("/search", params=q))                # unhidden -> back in default nav


def test_unhidden_semantic_node_reenters_search_graph_channel(client, tmp_path):
    from app.backend import graph_read, search
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn, node_status="hidden", review_status="approved")
    seed = "cpt_ssssssssssssssss"
    graph.upsert_node(conn, node_id=seed, node_type="concept", slug="seed", status="active")
    graph.upsert_assertion(conn, src_id=seed, dst_id=NID, edge_type="related_to",
                           asserted_by="deterministic", status="active")
    conn.commit()
    conn.close()

    def adjacent():
        g = graph.connect(tmp_path / "db" / "graph.sqlite")
        try:
            sub = graph_read.search_subgraph(g, [seed], depth=1,
                                             node_statuses=search.RETENTION_DEFAULT_STATUSES,
                                             node_cap=50, edge_cap=50)
            return {n["node_id"] for n in sub["nodes"]}
        finally:
            g.close()

    assert NID not in adjacent()                                      # hidden, excluded from graph channel
    _approve_unhide_semantic(tmp_path)
    assert client.post("/reviews/apply").json()["status"] == "applied"
    assert NID in adjacent()                                          # unhidden -> re-enters graph channel


def test_unhidden_source_reenters_search_evidence(client, tmp_path):
    from app.backend import keyword_index
    _hidden_source(tmp_path)                                          # full manifest (hidden) + Source page
    text = "alpha bravo charlie discovery prose"                     # add a searchable evidence chunk
    (tmp_path / "normalized" / "chunks").mkdir(parents=True, exist_ok=True)
    (tmp_path / "normalized" / "chunks" / f"{SID}.jsonl").write_text(json.dumps({
        "chunk_id": f"{SID}::0000", "source_id": SID, "ordinal": 0, "kind": "prose",
        "heading_path": [], "section": None, "text": text, "char_start": 0, "char_end": len(text),
        "page": 1, "page_end": 1, "table_reference": None, "sheet_reference": None}) + "\n",
        encoding="utf-8")
    (tmp_path / "normalized" / "markdown" / f"{SID}.md").write_text(text, encoding="utf-8")
    keyword_index.reindex(tmp_path, force=True)
    cited = lambda r: {e["source_id"] for e in r.json()["evidence"]}  # noqa: E731
    q = {"q": "alpha bravo discovery"}
    assert SID not in cited(client.get("/search", params=q))          # hidden source -> no evidence
    _approve_unhide_source(tmp_path)
    assert client.post("/reviews/apply").json()["status"] == "applied"
    assert SID in cited(client.get("/search", params=q))              # unhidden -> evidence re-enters


def test_dry_run_unhide_reindex_failure_is_non_clean(client, tmp_path, monkeypatch):
    def boom(root):
        raise RuntimeError("reindex blew up")
    monkeypatch.setattr(main_module.retention, "reindex_keyword", boom)
    conn = _graph(tmp_path)
    _write_concept(tmp_path, conn, node_status="hidden", review_status="approved")
    conn.commit()
    conn.close()
    _approve_unhide_semantic(tmp_path)
    dry = client.post("/reviews/apply/dry-run").json()
    assert dry["status"] == "validation_failed"
    assert "unhide_discovery_restoration_not_guaranteed" in dry["warnings"]
    assert _page_status(tmp_path) == ("hidden", "approved")           # live unchanged by the dry-run
