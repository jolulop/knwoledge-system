"""ADR-0048 claim visibility: hide_claim / unhide_claim (evidence-derived status + backlink re-render).

Claims differ from concepts: status is evidence-derived (recompose_claim), a hidden status must be
preserved across re-render, unhide re-derives (active|tombstone), and a hidden partner is omitted from
the rendered "Contradicting Claims" section (the edge stays active).
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
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from fastapi.testclient import TestClient

from app.backend import graph
from app.backend import main as main_module
from app.backend import review_read
from app.backend.config import get_settings
from app.workers import claims, deprecations, wiki
from app.workers.wiki_render import parse_frontmatter

CX = "clm_aaaaaaaaaaaaaaaa"
CY = "clm_bbbbbbbbbbbbbbbb"
SID = "src_0123456789abcdef"


def _graph(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    return graph.connect(gdb)


def _active_claim(tmp_path, conn, cid, *, text):
    # source markdown for the citation quote + a derived_from edge -> an evidenced (active) claim page.
    md = tmp_path / "normalized" / "markdown" / f"{SID}.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(text, encoding="utf-8")
    graph.upsert_node(conn, node_id=cid, node_type="claim", slug=cid, status="active")
    graph.upsert_node(conn, node_id=SID, node_type="source", slug=SID, status="active")
    graph.upsert_assertion(conn, src_id=cid, dst_id=SID, edge_type="derived_from", asserted_by="llm",
                           status="active", evidence_source_id=SID, evidence_char_start=0,
                           evidence_char_end=len(text))
    conn.commit()
    claims.recompose_claim(conn, cid=cid, claims_dir=tmp_path / "wiki" / "Claims",
                           reviews_dir=tmp_path / "reviews",
                           markdown_dir=tmp_path / "normalized" / "markdown", now="t", text_hint=text)
    conn.commit()


def _claim_fm(tmp_path, cid):
    return parse_frontmatter((tmp_path / "wiki" / "Claims" / f"{cid}.md").read_text())


def _approve(tmp_path, rtype, cid, *, to_status, rid="rev_c"):
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": rtype, "status": "approved",
        "subject": {"node_id": cid, "page": f"Claims/{cid}.md"},
        "proposal": {"to_status": to_status}, "context": {"node_type": "claim"}}), encoding="utf-8")


def _apply_hide(tmp_path, conn):
    return deprecations.apply_hidden_claims(conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki",
                                            markdown_dir=tmp_path / "normalized" / "markdown")


def _apply_unhide(tmp_path, conn):
    return deprecations.apply_unhidden_claims(conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki",
                                              markdown_dir=tmp_path / "normalized" / "markdown")


# --- executor: hide / unhide / preservation --------------------------------


def test_hide_active_claim_flips_page_and_graph(tmp_path):
    conn = _graph(tmp_path)
    _active_claim(tmp_path, conn, CX, text="The sky is blue today.")
    _approve(tmp_path, "hide_claim", CX, to_status="hidden")
    res = _apply_hide(tmp_path, conn)
    conn.commit()
    assert res["applied"] == 1
    fm = _claim_fm(tmp_path, CX)
    assert fm["status"] == "hidden" and fm["review_status"] == "approved"
    assert graph.get_node(conn, CX)["status"] == "hidden"
    # evidence (citations) still rendered + the derived_from edge preserved (raw graph)
    assert "citations:" in (tmp_path / "wiki" / "Claims" / f"{CX}.md").read_text()
    conn.close()


def test_hide_non_active_claim_skips_claim_not_active(tmp_path):
    conn = _graph(tmp_path)
    graph.upsert_node(conn, node_id=CX, node_type="claim", slug=CX, status="deprecated_candidate")
    (tmp_path / "wiki" / "Claims").mkdir(parents=True, exist_ok=True)
    (tmp_path / "wiki" / "Claims" / f"{CX}.md").write_text(
        f'---\ntype: claim\nclaim_id: "{CX}"\nstatus: deprecated_candidate\nreview_status: approved\n'
        'claim_text: "x"\n---\n', encoding="utf-8")
    conn.commit()
    _approve(tmp_path, "hide_claim", CX, to_status="hidden")
    res = _apply_hide(tmp_path, conn)
    assert res["applied"] == 0 and res["skipped"] == [{"review_id": "rev_c", "reason": "claim_not_active"}]
    conn.close()


def test_hidden_status_preserved_across_recompose(tmp_path):
    # ADR-0048: a later evidence-driven recompose of a hidden claim must NOT silently un-hide it.
    conn = _graph(tmp_path)
    _active_claim(tmp_path, conn, CX, text="The sky is blue today.")
    _approve(tmp_path, "hide_claim", CX, to_status="hidden")
    _apply_hide(tmp_path, conn)
    conn.commit()
    # re-render with no hide/unhide flag (as contradiction re-projection / re-extraction would)
    claims.recompose_claim(conn, cid=CX, claims_dir=tmp_path / "wiki" / "Claims",
                           reviews_dir=tmp_path / "reviews",
                           markdown_dir=tmp_path / "normalized" / "markdown", now="t2")
    conn.commit()
    assert _claim_fm(tmp_path, CX)["status"] == "hidden"          # stayed hidden
    assert graph.get_node(conn, CX)["status"] == "hidden"
    conn.close()


def test_unhide_re_derives_active_when_evidence_remains(tmp_path):
    conn = _graph(tmp_path)
    _active_claim(tmp_path, conn, CX, text="The sky is blue today.")
    _approve(tmp_path, "hide_claim", CX, to_status="hidden")
    _apply_hide(tmp_path, conn)
    conn.commit()
    _approve(tmp_path, "unhide_claim", CX, to_status="active", rid="rev_u")
    res = _apply_unhide(tmp_path, conn)
    conn.commit()
    assert res["applied"] == 1
    fm = _claim_fm(tmp_path, CX)
    assert fm["status"] == "active" and fm["review_status"] == "none"   # clean re-derived default
    assert graph.get_node(conn, CX)["status"] == "active"
    conn.close()


def test_unhide_re_derives_tombstone_when_evidence_lost(tmp_path):
    # a claim hidden, then its evidence removed while hidden -> unhide re-derives a tombstone, NOT
    # active-with-zero-citations (preserves the active-claim-has-evidence invariant).
    conn = _graph(tmp_path)
    _active_claim(tmp_path, conn, CX, text="The sky is blue today.")
    _approve(tmp_path, "hide_claim", CX, to_status="hidden")
    _apply_hide(tmp_path, conn)
    conn.commit()
    # drop the evidence edge while hidden (re-extraction removed the source)
    graph.set_status(conn, graph.outgoing_active(conn, CX)[0]["edge_id"], "superseded", now="t2")
    conn.commit()
    _approve(tmp_path, "unhide_claim", CX, to_status="active", rid="rev_u")
    _apply_unhide(tmp_path, conn)
    conn.commit()
    assert _claim_fm(tmp_path, CX)["status"] == "deprecated_candidate"   # tombstone, not active
    conn.close()


# --- backlink omission + re-render -----------------------------------------


def _contradicts(tmp_path, conn):
    _active_claim(tmp_path, conn, CX, text="The sky is blue.")
    _active_claim(tmp_path, conn, CY, text="The sky is green.")
    a, b = sorted((CX, CY))
    graph.upsert_assertion(conn, src_id=a, dst_id=b, edge_type="contradicts", asserted_by="llm",
                           status="active")
    conn.commit()
    # re-render both so each lists the other in its Contradicting Claims section
    for cid in (CX, CY):
        claims.recompose_claim(conn, cid=cid, claims_dir=tmp_path / "wiki" / "Claims",
                               reviews_dir=tmp_path / "reviews",
                               markdown_dir=tmp_path / "normalized" / "markdown", now="t")
    conn.commit()


def _cy_lists_cx(tmp_path):  # the partner cid appears in CY's page (contradicts list + body section)
    return CX in (tmp_path / "wiki" / "Claims" / f"{CY}.md").read_text()


def test_hiding_claim_omits_it_from_partner_section_edge_preserved(tmp_path):
    conn = _graph(tmp_path)
    _contradicts(tmp_path, conn)
    assert _cy_lists_cx(tmp_path)                                    # baseline: Y lists X
    _approve(tmp_path, "hide_claim", CX, to_status="hidden")
    _apply_hide(tmp_path, conn)
    conn.commit()
    assert not _cy_lists_cx(tmp_path)                               # Y's section dropped hidden X
    # the contradicts edge stays active (raw graph inspection)
    row = conn.execute("SELECT status FROM edges WHERE edge_type='contradicts'").fetchone()
    assert row["status"] == "active"
    conn.close()


def test_validate_projection_passes_with_hidden_partner_on_claim_page(tmp_path):
    # ADR-0048 (review fix): the rendered Contradicting Claims section omits a hidden partner, so
    # validate_projection must expect active NON-hidden contradicts — else a real vault validate-fails.
    import validate_projection
    conn = _graph(tmp_path)
    _contradicts(tmp_path, conn)
    _approve(tmp_path, "hide_claim", CX, to_status="hidden")
    _apply_hide(tmp_path, conn)                                      # re-renders CY to drop hidden CX
    conn.commit()
    conn.close()
    assert not _cy_lists_cx(tmp_path)
    assert validate_projection.main([str(tmp_path)]) == 0           # status-aware projection passes


def test_unhiding_claim_restores_it_in_partner_section(tmp_path):
    conn = _graph(tmp_path)
    _contradicts(tmp_path, conn)
    _approve(tmp_path, "hide_claim", CX, to_status="hidden")
    _apply_hide(tmp_path, conn)
    conn.commit()
    assert not _cy_lists_cx(tmp_path)
    _approve(tmp_path, "unhide_claim", CX, to_status="active", rid="rev_u")
    _apply_unhide(tmp_path, conn)
    conn.commit()
    assert _cy_lists_cx(tmp_path)                                   # restored
    conn.close()


# --- Source-page Claims section omits hidden claims + re-renders ------------


def _source_with_claim(tmp_path, conn, text="The sky is blue today."):
    # a full-enough vault (manifest + templates + normalized) so generate_wiki renders the Source page,
    # with an active claim cited from the source (its Claims section should list the claim).
    shutil.copytree(ROOT / "templates", tmp_path / "templates", dirs_exist_ok=True)
    md = tmp_path / "raw" / "manifests"
    md.mkdir(parents=True, exist_ok=True)
    rel = f"raw/inbox/{SID}.md"
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text("raw", encoding="utf-8")
    (md / f"{SID}.json").write_text(json.dumps({
        "source_id": SID, "sha256": SID + "0" * 8, "relative_raw_path": rel, "file_extension": ".md",
        "chunk_count": 1, "ingestion_status": "extracted",
        "normalized": {"markdown_path": f"normalized/markdown/{SID}.md"},
        "created_at": "2000-01-01T00:00:00+00:00", "discovered_at": "2000-01-01T00:00:00+00:00",
        "modified_at": "2000-01-01T00:00:00+00:00", "retention_class": "permanent",
        "occurrences": [{"relative_path": rel}]}), encoding="utf-8")
    (tmp_path / "normalized" / "markdown" / f"{SID}.md").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "normalized" / "markdown" / f"{SID}.md").write_text(text, encoding="utf-8")
    graph.upsert_node(conn, node_id=CX, node_type="claim", slug=CX, status="active")
    graph.upsert_node(conn, node_id=SID, node_type="source", slug=SID, status="active")
    graph.upsert_assertion(conn, src_id=CX, dst_id=SID, edge_type="derived_from", asserted_by="llm",
                           status="active", evidence_source_id=SID, evidence_char_start=0,
                           evidence_char_end=len(text))
    conn.commit()
    claims.recompose_claim(conn, cid=CX, claims_dir=tmp_path / "wiki" / "Claims",
                           reviews_dir=tmp_path / "reviews",
                           markdown_dir=tmp_path / "normalized" / "markdown", now="t", text_hint=text)
    conn.commit()
    wiki.generate_wiki(tmp_path, source_ids=[SID], rebuild_index=False, record_job=False)


def test_validate_projection_passes_with_hidden_claim_on_source_page(tmp_path):
    # ADR-0048 (review fix): the Source-page Claims section omits a hidden claim, so validate_projection
    # must expect active NON-hidden claims — else a real vault with a Source page validate-fails.
    import validate_projection
    conn = _graph(tmp_path)
    _source_with_claim(tmp_path, conn)
    _approve(tmp_path, "hide_claim", CX, to_status="hidden")
    _apply_hide(tmp_path, conn)
    conn.commit()
    conn.close()
    wiki.generate_wiki(tmp_path, source_ids=[SID], rebuild_index=False, record_job=False)  # drop hidden CX
    assert CX not in (tmp_path / "wiki" / "Sources" / f"{SID}.md").read_text()
    assert validate_projection.main([str(tmp_path)]) == 0


def test_source_page_render_omits_hidden_claim(tmp_path):
    conn = _graph(tmp_path)
    _source_with_claim(tmp_path, conn)
    src = tmp_path / "wiki" / "Sources" / f"{SID}.md"
    assert CX in src.read_text()                                   # baseline: Source page lists the claim
    graph.upsert_node(conn, node_id=CX, node_type="claim", slug=CX, status="hidden")
    conn.commit()
    conn.close()
    wiki.generate_wiki(tmp_path, source_ids=[SID], rebuild_index=False, record_job=False)
    assert CX not in src.read_text()                              # hidden claim dropped (render filter)


def test_api_claim_hide_unhide_rerenders_source_page(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module.retention, "reindex_keyword", lambda root: None)
    conn = _graph(tmp_path)
    _source_with_claim(tmp_path, conn)
    conn.commit()
    conn.close()
    src = tmp_path / "wiki" / "Sources" / f"{SID}.md"
    assert CX in src.read_text()
    _approve(tmp_path, "hide_claim", CX, to_status="hidden")
    assert client.post("/reviews/apply").json()["status"] == "applied"
    assert CX not in src.read_text()                              # apply re-rendered the Source page
    _approve(tmp_path, "unhide_claim", CX, to_status="active", rid="rev_u")
    assert client.post("/reviews/apply").json()["status"] == "applied"
    assert CX in src.read_text()                                  # unhide (evidence stands) restores it


def test_unhide_after_evidence_lost_tombstones_not_restored_to_source_page(client, tmp_path, monkeypatch):
    # cross-path: hide -> lose evidence while hidden (recompose preserves hidden + citations []) -> unhide
    # re-derives a TOMBSTONE, and the claim is NOT restored to the Source-page Claims section.
    monkeypatch.setattr(main_module.retention, "reindex_keyword", lambda root: None)
    conn = _graph(tmp_path)
    _source_with_claim(tmp_path, conn)
    conn.commit()
    conn.close()
    src = tmp_path / "wiki" / "Sources" / f"{SID}.md"
    _approve(tmp_path, "hide_claim", CX, to_status="hidden")
    client.post("/reviews/apply")
    assert CX not in src.read_text()
    # evidence lost while hidden + recompose (preservation): stays hidden with citations []
    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    graph.set_status(conn, graph.outgoing_active(conn, CX)[0]["edge_id"], "superseded", now="t2")
    conn.commit()
    claims.recompose_claim(conn, cid=CX, claims_dir=tmp_path / "wiki" / "Claims",
                           reviews_dir=tmp_path / "reviews",
                           markdown_dir=tmp_path / "normalized" / "markdown", now="t3")
    conn.commit()
    conn.close()
    assert _claim_fm(tmp_path, CX)["status"] == "hidden"
    _approve(tmp_path, "unhide_claim", CX, to_status="active", rid="rev_u")
    client.post("/reviews/apply")
    assert _claim_fm(tmp_path, CX)["status"] == "deprecated_candidate"   # re-derived tombstone, not active
    assert CX not in src.read_text()                                    # not restored (no active evidence)


# --- detection: hidden claim excluded from future candidate generation -----


def test_hidden_claim_excluded_from_contradiction_candidates_edge_kept(tmp_path):
    conn = _graph(tmp_path)
    _active_claim(tmp_path, conn, CX, text="The sky is blue.")
    _active_claim(tmp_path, conn, CY, text="The sky is green.")
    a, b = sorted((CX, CY))
    graph.upsert_assertion(conn, src_id=a, dst_id=b, edge_type="contradicts", asserted_by="llm",
                           status="active")            # an existing active contradiction
    conn.commit()
    assert CX in graph.active_node_ids_of_type(conn, "claim")       # baseline: candidate-eligible
    _approve(tmp_path, "hide_claim", CX, to_status="hidden")
    _apply_hide(tmp_path, conn)
    conn.commit()
    assert CX not in graph.active_node_ids_of_type(conn, "claim")   # hidden -> not in candidate set
    row = conn.execute("SELECT status FROM edges WHERE edge_type='contradicts'").fetchone()
    assert row["status"] == "active"                               # existing edge preserved (no surgery)
    conn.close()


def test_hidden_claim_with_no_evidence_passes_validators(tmp_path):
    # ADR-0048 (review fix): a hidden claim that loses all evidence while hidden renders status: hidden +
    # citations: [] and is validator-legal (hidden has governance precedence over evidence-derivation).
    import validate_citations
    conn = _graph(tmp_path)
    _active_claim(tmp_path, conn, CX, text="The sky is blue today.")
    _approve(tmp_path, "hide_claim", CX, to_status="hidden")
    _apply_hide(tmp_path, conn)
    conn.commit()
    graph.set_status(conn, graph.outgoing_active(conn, CX)[0]["edge_id"], "superseded", now="t2")
    conn.commit()
    claims.recompose_claim(conn, cid=CX, claims_dir=tmp_path / "wiki" / "Claims",
                           reviews_dir=tmp_path / "reviews",
                           markdown_dir=tmp_path / "normalized" / "markdown", now="t3")
    conn.commit()
    conn.close()
    txt = (tmp_path / "wiki" / "Claims" / f"{CX}.md").read_text()
    assert "status: hidden" in txt and "citations: []" in txt
    assert validate_citations.main([str(tmp_path)]) == 0           # validator-legal


def test_hide_partial_state_is_typed_skip_not_silent(tmp_path):
    # graph hidden but page active (drift) -> typed partial_hide_state skip, NOT a silent no-op.
    conn = _graph(tmp_path)
    _active_claim(tmp_path, conn, CX, text="The sky is blue today.")
    graph.upsert_node(conn, node_id=CX, node_type="claim", slug=CX, status="hidden")  # graph only
    conn.commit()
    _approve(tmp_path, "hide_claim", CX, to_status="hidden")
    res = _apply_hide(tmp_path, conn)
    assert res["applied"] == 0
    assert res["skipped"] == [{"review_id": "rev_c", "reason": "partial_hide_state"}]
    conn.close()


def test_unhide_partial_state_is_typed_skip_not_silent(tmp_path):
    # page hidden but graph active (drift) -> typed partial_unhide_state skip, NOT a silent no-op.
    conn = _graph(tmp_path)
    _active_claim(tmp_path, conn, CX, text="The sky is blue today.")
    p = tmp_path / "wiki" / "Claims" / f"{CX}.md"
    p.write_text(p.read_text().replace("status: active", "status: hidden", 1), encoding="utf-8")
    conn.commit()                                                  # graph stays active
    _approve(tmp_path, "unhide_claim", CX, to_status="active", rid="rev_u")
    res = _apply_unhide(tmp_path, conn)
    assert res["skipped"] == [{"review_id": "rev_u", "reason": "partial_unhide_state"}]
    conn.close()


# --- projector + reopen safety ---------------------------------------------


def _effect(tmp_path, conn, rtype, *, status="approved"):
    item = {"type": rtype, "status": status, "subject": {"node_id": CX, "page": f"Claims/{CX}.md"},
            "context": {"node_type": "claim"}}
    fn = review_read._effect_hide_claim if rtype == "hide_claim" else review_read._effect_unhide_claim
    return fn(item, conn, tmp_path / "wiki")


def test_hide_projector_effected_pending_and_claim_not_active(tmp_path):
    conn = _graph(tmp_path)
    _active_claim(tmp_path, conn, CX, text="x y z.")
    assert _effect(tmp_path, conn, "hide_claim")[0] == review_read.PENDING_APPLY   # active, not yet hidden
    graph.upsert_node(conn, node_id=CX, node_type="claim", slug=CX, status="deprecated_candidate")
    conn.commit()
    # page active, graph deprecated -> neither hidden -> PENDING_APPLY + claim_not_active
    assert _effect(tmp_path, conn, "hide_claim")[1] == ["claim_not_active"]
    conn.close()


def test_hide_projector_both_hidden_pending_is_unknown_not_effected(tmp_path):
    # ADR-0048 reopen-safety: EFFECTED requires review_status approved; both hidden + pending -> UNKNOWN.
    conn = _graph(tmp_path)
    _active_claim(tmp_path, conn, CX, text="x y z.")
    p = tmp_path / "wiki" / "Claims" / f"{CX}.md"
    p.write_text(p.read_text().replace("status: active", "status: hidden", 1)
                 .replace("review_status: none", "review_status: pending", 1), encoding="utf-8")
    graph.upsert_node(conn, node_id=CX, node_type="claim", slug=CX, status="hidden")
    conn.commit()
    status, warnings = _effect(tmp_path, conn, "hide_claim")
    assert status == review_read.UNKNOWN and warnings == ["partial_hide_state"]
    conn.close()


def test_unhide_projector_effected_when_not_hidden(tmp_path):
    conn = _graph(tmp_path)
    _active_claim(tmp_path, conn, CX, text="x y z.")        # active (not hidden)
    assert _effect(tmp_path, conn, "unhide_claim")[0] == review_read.EFFECTED
    conn.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


def _setup_partial(tmp_path, *, page_status, graph_status):
    conn = _graph(tmp_path)
    _active_claim(tmp_path, conn, CX, text="The sky is blue today.")
    # force a partial state: rewrite the page status + set the graph node status independently
    p = tmp_path / "wiki" / "Claims" / f"{CX}.md"
    p.write_text(p.read_text().replace("status: active", f"status: {page_status}", 1), encoding="utf-8")
    graph.upsert_node(conn, node_id=CX, node_type="claim", slug=CX, status=graph_status)
    conn.commit()
    conn.close()


def test_reopen_blocked_for_partial_claim_hide(client, tmp_path):
    _setup_partial(tmp_path, page_status="hidden", graph_status="active")   # page XOR graph hidden
    _approve(tmp_path, "hide_claim", CX, to_status="hidden", rid="rev_c")
    r = client.post("/reviews/rev_c/reopen", json={"reason": "undo"})
    assert r.status_code == 409 and "effect_unknown_repair_read_model" in r.json()["detail"]


# --- API: apply + summary + graph-required + reindex posture ---------------


def test_api_apply_hides_claim_with_summary(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module.retention, "reindex_keyword", lambda root: None)
    conn = _graph(tmp_path)
    _active_claim(tmp_path, conn, CX, text="The sky is blue today.")
    conn.commit()
    conn.close()
    _approve(tmp_path, "hide_claim", CX, to_status="hidden")
    body = client.post("/reviews/apply").json()
    assert body["status"] == "applied" and body["summary"]["claims_hidden"]["applied"] == 1


def test_api_claim_hide_graph_required_503(client, tmp_path):
    _approve(tmp_path, "hide_claim", CX, to_status="hidden")
    assert not (tmp_path / "db" / "graph.sqlite").exists()
    assert client.post("/reviews/apply").status_code == 503


def test_api_claim_hide_reindex_failure_is_non_clean(client, tmp_path, monkeypatch):
    def boom(root):
        raise RuntimeError("reindex blew up")
    monkeypatch.setattr(main_module.retention, "reindex_keyword", boom)
    conn = _graph(tmp_path)
    _active_claim(tmp_path, conn, CX, text="The sky is blue today.")
    conn.commit()
    conn.close()
    _approve(tmp_path, "hide_claim", CX, to_status="hidden")
    body = client.post("/reviews/apply").json()
    assert body["status"] == "validation_failed"
    assert "claim_hide_retrieval_suppression_not_guaranteed" in body["warnings"]


def test_detection_still_pairs_after_using_active_claims(tmp_path):
    # sanity: candidate_pairs uses active claims only (so a hidden claim is excluded — see above), and
    # the contradicts producer / detection path is unchanged for active claims.
    conn = _graph(tmp_path)
    _active_claim(tmp_path, conn, CX, text="The sky is blue.")
    _active_claim(tmp_path, conn, CY, text="The sky is green.")
    assert set(graph.active_node_ids_of_type(conn, "claim")) == {CX, CY}
    conn.close()
