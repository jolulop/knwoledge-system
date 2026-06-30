"""ADR-0050 identity surgery: entity/concept merge (rekeying), forward-only.

Merge collapses absorbed B into survivor A: re-point active edges (normalize: canonicalize symmetric,
full-identity collision handling, drop self-edges), tombstone B (`merged` + merged_into), union aliases,
withdraw unresolved B-subjects, audit. Pre-write BLOCK gates; never partial-apply.
"""
from __future__ import annotations

import json
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
from app.workers import concepts, merges
from app.workers.wiki_render import parse_frontmatter, render_concept_page

A = concepts.node_id("concept", "Alpha")
B = concepts.node_id("concept", "Beta")
SID = "src_0123456789abcdef"


def _graph(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    return graph.connect(gdb)


def _concept(tmp_path, conn, cid, name, *, status="active", aliases=()):
    slug = concepts._slug(name)
    graph.upsert_node(conn, node_id=cid, node_type="concept", slug=slug, status=status)
    page = tmp_path / "wiki" / "Concepts" / f"{slug}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(render_concept_page({
        "node_type": "concept", "node_id": cid, "id_field": "concept_id", "title": name,
        "aliases": list(aliases), "confidence": "low",
        "source_ids": graph.sources_for_node(conn, cid), "status": status,
        "duplicates": graph.active_duplicates(conn, cid),
    }), encoding="utf-8")
    conn.commit()
    return slug


def _mention(conn, sid, cid, *, status="active", span=None, review_id=None):
    graph.upsert_node(conn, node_id=sid, node_type="source", slug=sid, status="active")
    anchor = {"evidence_source_id": sid, "evidence_char_start": span[0],
              "evidence_char_end": span[1]} if span else {}
    eid = graph.upsert_assertion(conn, src_id=sid, dst_id=cid, edge_type="mentions", asserted_by="llm",
                                 status=status, review_id=review_id, **anchor)
    conn.commit()
    return eid


def _approve(tmp_path, *, survivor=A, absorbed=B, rtype="merge_concepts", rid="rev_m"):
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": rtype, "status": "approved",
        "subject": {"survivor_node_id": survivor, "absorbed_node_id": absorbed},
        "proposal": {"to_status": "merged"}}), encoding="utf-8")


def _apply(tmp_path, conn):
    return merges.apply_merges(conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")


def _fm(tmp_path, slug):
    return parse_frontmatter((tmp_path / "wiki" / "Concepts" / f"{slug}.md").read_text())


def _active_mentions(conn, dst):
    return [e for e in graph.incoming_active(conn, dst) if e["edge_type"] == "mentions"]


# --- core: collapse + tombstone + aliases ----------------------------------


def test_merge_tombstones_b_and_keeps_a_active(tmp_path):
    conn = _graph(tmp_path)
    a_slug = _concept(tmp_path, conn, A, "Alpha", aliases=["A1"])
    b_slug = _concept(tmp_path, conn, B, "Beta", aliases=["B1"])
    _approve(tmp_path)
    res = _apply(tmp_path, conn)
    assert res["applied"] == 1 and res["skipped"] == []
    assert graph.get_node(conn, B)["status"] == "merged"          # absorbed -> tombstone
    assert graph.get_node(conn, A)["status"] == "active"          # survivor stays active
    fm_b = _fm(tmp_path, b_slug)
    assert fm_b["status"] == "merged" and fm_b["merged_into"] == A
    assert "Merged into" in (tmp_path / "wiki" / "Concepts" / f"{b_slug}.md").read_text()
    fm_a = _fm(tmp_path, a_slug)
    assert "Beta" in fm_a["aliases"] and "B1" in fm_a["aliases"] and "A1" in fm_a["aliases"]  # union
    assert fm_a["title"] == "Alpha"                               # survivor title unchanged
    conn.close()


def test_merge_is_idempotent_on_reapply(tmp_path):
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    _concept(tmp_path, conn, B, "Beta")
    _approve(tmp_path)
    _apply(tmp_path, conn)
    res2 = _apply(tmp_path, conn)                                 # B already merged -> no-op
    assert res2["applied"] == 0 and res2["changed_pages"] == []
    conn.close()


# --- edge re-point + collision ---------------------------------------------


def test_mention_repoints_b_to_a(tmp_path):
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    _concept(tmp_path, conn, B, "Beta")
    _mention(conn, SID, B, span=(0, 4))
    _approve(tmp_path)
    res = _apply(tmp_path, conn)
    assert SID in res["affected_sources"]                         # Source page must re-render
    ments = _active_mentions(conn, A)
    assert len(ments) == 1 and ments[0]["src_id"] == SID          # re-pointed Src->A
    assert not _active_mentions(conn, B)                          # none left on B
    conn.close()


def test_distinct_evidence_edges_coexist(tmp_path):
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    _concept(tmp_path, conn, B, "Beta")
    _mention(conn, SID, A, span=(0, 4))                           # different evidence spans
    _mention(conn, SID, B, span=(10, 14))
    _approve(tmp_path)
    _apply(tmp_path, conn)
    assert len(_active_mentions(conn, A)) == 2                    # distinct identities -> both survive
    conn.close()


def test_exact_full_identity_collision_collapses(tmp_path):
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    _concept(tmp_path, conn, B, "Beta")
    _mention(conn, SID, A, span=(0, 4))                           # SAME evidence span -> collision
    b_edge = _mention(conn, SID, B, span=(0, 4))
    _approve(tmp_path)
    _apply(tmp_path, conn)
    assert len(_active_mentions(conn, A)) == 1                    # collapsed, no duplicate row
    row = conn.execute("SELECT status FROM edges WHERE edge_id=?", (b_edge,)).fetchone()
    assert row["status"] == "superseded"                         # absorbed row superseded, not deleted
    conn.close()


def test_self_edge_is_superseded(tmp_path):
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    _concept(tmp_path, conn, B, "Beta")
    lo, hi = sorted((A, B))
    dup = graph.upsert_assertion(conn, src_id=lo, dst_id=hi, edge_type="duplicates",
                                 asserted_by="human", status="active")
    conn.commit()
    _approve(tmp_path)
    _apply(tmp_path, conn)
    row = conn.execute("SELECT status FROM edges WHERE edge_id=?", (dup,)).fetchone()
    assert row["status"] == "superseded"                         # duplicates(A,B) -> A<->A -> dropped
    conn.close()


def test_resurrect_proposed_target_collision(tmp_path):
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    _concept(tmp_path, conn, B, "Beta")
    # a PROPOSED mentions(Src->A) with a pending review + an ACTIVE mentions(Src->B), SAME identity
    a_edge = _mention(conn, SID, A, span=(0, 4), status="proposed", review_id="rev_prop")
    (tmp_path / "reviews" / "pending").mkdir(parents=True, exist_ok=True)
    (tmp_path / "reviews" / "pending" / "rev_prop.json").write_text(json.dumps({
        "review_id": "rev_prop", "type": "promote_candidate_node", "status": "pending",
        "subject": {"node_id": "cpt_x"}}), encoding="utf-8")
    _mention(conn, SID, B, span=(0, 4))
    _approve(tmp_path)
    res = _apply(tmp_path, conn)
    assert res["applied"] == 1
    row = conn.execute("SELECT status, review_id FROM edges WHERE edge_id=?", (a_edge,)).fetchone()
    assert row["status"] == "active" and row["review_id"] == "rev_m"   # resurrected + merge authority
    assert not (tmp_path / "reviews" / "pending" / "rev_prop.json").exists()   # stale review withdrawn
    conn.close()


def test_collapse_and_resurrect_still_re_render_the_source(tmp_path):
    # A collapsed/resurrected mentions(Src->B) must still mark the Source page for re-render (it can keep a
    # stale [[Concepts/B]] otherwise) — affected_sources covers EVERY action, not just repoint.
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    _concept(tmp_path, conn, B, "Beta")
    _mention(conn, SID, A, span=(0, 4))                           # collision -> B edge collapses
    _mention(conn, SID, B, span=(0, 4))
    _approve(tmp_path)
    assert SID in _apply(tmp_path, conn)["affected_sources"]
    conn.close()


def test_duplicates_collapse_re_renders_partner_dropping_b(tmp_path):
    C = concepts.node_id("concept", "Gamma")
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    _concept(tmp_path, conn, B, "Beta")
    c_slug = _concept(tmp_path, conn, C, "Gamma")
    for pair in ((A, C), (B, C)):                                # both A,C and B,C are duplicates of C
        lo, hi = sorted(pair)
        graph.upsert_assertion(conn, src_id=lo, dst_id=hi, edge_type="duplicates",
                               asserted_by="human", status="active")
    conn.commit()
    # render C's page with both duplicates so its ## Duplicates section lists Alpha + Beta
    concepts.recompose_semantic_node_page(conn, node_id=C, wiki_dir=tmp_path / "wiki",
                                          status="active", review_status="none")
    assert "beta" in (tmp_path / "wiki" / "Concepts" / f"{c_slug}.md").read_text()
    _approve(tmp_path)
    _apply(tmp_path, conn)
    txt = (tmp_path / "wiki" / "Concepts" / f"{c_slug}.md").read_text()
    assert "[[Concepts/alpha]]" in txt and "[[Concepts/beta]]" not in txt   # C now lists only A
    conn.close()


def test_tombstone_carries_merge_audit_fields(tmp_path):
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    b_slug = _concept(tmp_path, conn, B, "Beta")
    _approve(tmp_path)
    _apply(tmp_path, conn)
    fm = _fm(tmp_path, b_slug)
    assert fm["merged_into"] == A and fm["merge_review_id"] == "rev_m" and fm.get("merged_at")
    conn.close()


@pytest.mark.parametrize("corrupt", ["cpt_dead", B, "_SELF_"])
def test_validator_rejects_bad_merged_into(tmp_path, corrupt):
    # non-indexed survivor, wrong-type survivor (B is a concept; point at an entity), and self pointer.
    import validate_projection
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    b_slug = _concept(tmp_path, conn, B, "Beta")
    _approve(tmp_path)
    _apply(tmp_path, conn)
    if corrupt == B:                                             # wrong-type: an entity survivor
        corrupt = concepts.node_id("entity", "Delta")
        graph.upsert_node(conn, node_id=corrupt, node_type="entity", slug="delta", status="active")
        conn.commit()
    conn.close()
    assert validate_projection.main([str(tmp_path)]) == 0        # clean merge passes
    target = B if corrupt == "_SELF_" else corrupt
    page = tmp_path / "wiki" / "Concepts" / f"{b_slug}.md"
    page.write_text(page.read_text().replace(f'merged_into: "{A}"', f'merged_into: "{target}"'),
                    encoding="utf-8")
    assert validate_projection.main([str(tmp_path)]) != 0        # invariant violated


def test_validator_rejects_inactive_survivor(tmp_path):
    import validate_projection
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    _concept(tmp_path, conn, B, "Beta")
    _approve(tmp_path)
    _apply(tmp_path, conn)
    # survivor A deprecated after the merge -> B's merged_into points at a non-active survivor
    concepts.recompose_semantic_node_page(conn, node_id=A, wiki_dir=tmp_path / "wiki",
                                          status="deprecated_candidate", review_status="approved")
    conn.commit()
    conn.close()
    assert validate_projection.main([str(tmp_path)]) != 0


def test_partial_merge_state_is_unknown(tmp_path):
    # graph node merged but the page is NOT a tombstone -> partial -> UNKNOWN (reopen-safe), not EFFECTED.
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    _concept(tmp_path, conn, B, "Beta")                          # page status active
    graph.upsert_node(conn, node_id=B, node_type="concept", slug="beta", status="merged")  # graph only
    conn.commit()
    item = {"type": "merge_concepts", "status": "approved",
            "subject": {"survivor_node_id": A, "absorbed_node_id": B}}
    es, warns = review_read._effect_merge(item, conn, tmp_path / "wiki")
    assert es == review_read.UNKNOWN and warns == ["partial_merge_state"]
    conn.close()


def test_approved_proposal_references_absorbed_blocks(tmp_path):
    # the gate matches absorbed B in the PROPOSAL, not just the subject (decision 6: subject OR proposal).
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    _concept(tmp_path, conn, B, "Beta")
    _approve(tmp_path)
    (tmp_path / "reviews" / "approved" / "rc.json").write_text(json.dumps({
        "review_id": "rc", "type": "resolve_contradiction", "status": "approved",
        "subject": {"node_ids": ["clm_aaaaaaaaaaaaaaaa", "clm_bbbbbbbbbbbbbbbb"]},
        "proposal": {"winner": "clm_aaaaaaaaaaaaaaaa", "node_id": B}}), encoding="utf-8")
    res = _apply(tmp_path, conn)
    assert {"review_id": "rev_m", "reason": "approved_unapplied_references_absorbed"} in res["skipped"]
    conn.close()


def test_rejected_target_collision_blocks_the_merge(tmp_path):
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    _concept(tmp_path, conn, B, "Beta")
    _mention(conn, SID, A, span=(0, 4), status="rejected")        # human "no" on Src->A
    _mention(conn, SID, B, span=(0, 4))
    _approve(tmp_path)
    res = _apply(tmp_path, conn)
    assert res["applied"] == 0
    assert res["skipped"] == [{"review_id": "rev_m", "reason": "rejected_target_collision"}]
    assert graph.get_node(conn, B)["status"] == "active"         # no writes — B untouched
    conn.close()


# --- subject guards + matcher + gates --------------------------------------


@pytest.mark.parametrize("survivor,absorbed,reason", [
    (A, A, "self_merge"),
    (A, "cpt_ffffffffffffffff", "node_missing"),
])
def test_subject_guards_skip(tmp_path, survivor, absorbed, reason):
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    _approve(tmp_path, survivor=survivor, absorbed=absorbed)
    res = _apply(tmp_path, conn)
    assert res["skipped"] == [{"review_id": "rev_m", "reason": reason}]
    conn.close()


def test_type_mismatch_and_out_of_scope_skip(tmp_path):
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    # absorbed is an entity (different node_type) -> type_mismatch for merge_concepts
    ent = concepts.node_id("entity", "Beta")
    graph.upsert_node(conn, node_id=ent, node_type="entity", slug="beta", status="active")
    conn.commit()
    _approve(tmp_path, absorbed=ent)
    assert _apply(tmp_path, conn)["skipped"] == [{"review_id": "rev_m", "reason": "type_mismatch"}]
    conn.close()


def test_withdraws_pending_and_deferred_b_subjects(tmp_path):
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    b_slug = _concept(tmp_path, conn, B, "Beta")
    pend = tmp_path / "reviews" / "pending"
    pend.mkdir(parents=True, exist_ok=True)
    (pend / "p1.json").write_text(json.dumps({
        "review_id": "p1", "type": "promote_candidate_node", "status": "pending",
        "subject": {"node_id": B}}), encoding="utf-8")
    (pend / "d1.json").write_text(json.dumps({
        "review_id": "d1", "type": "hide_semantic_page", "status": "deferred",
        "subject": {"node_id": B, "page": f"Concepts/{b_slug}.md"}}), encoding="utf-8")
    (pend / "keep.json").write_text(json.dumps({
        "review_id": "keep", "type": "promote_candidate_node", "status": "pending",
        "subject": {"node_id": A}}), encoding="utf-8")
    _approve(tmp_path)
    _apply(tmp_path, conn)
    assert not (pend / "p1.json").exists() and not (pend / "d1.json").exists()   # both withdrawn
    assert (pend / "keep.json").exists()                         # A-subject untouched
    conn.close()


def test_subject_matcher_topic_node_id(tmp_path):
    # a propose_synthesis pending item keyed on topic_node_id == B must be withdrawn.
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    _concept(tmp_path, conn, B, "Beta")
    pend = tmp_path / "reviews" / "pending"
    pend.mkdir(parents=True, exist_ok=True)
    (pend / "syn.json").write_text(json.dumps({
        "review_id": "syn", "type": "propose_synthesis", "status": "pending",
        "subject": {"topic_node_id": B, "fingerprint": "fp"}}), encoding="utf-8")
    _approve(tmp_path)
    _apply(tmp_path, conn)
    assert not (pend / "syn.json").exists()
    conn.close()


def test_approved_unapplied_references_absorbed_blocks(tmp_path):
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    b_slug = _concept(tmp_path, conn, B, "Beta")
    _approve(tmp_path)                                            # creates reviews/approved/
    # an approved hide_semantic_page on B that is NOT yet applied (page still active) -> PENDING_APPLY
    (tmp_path / "reviews" / "approved" / "hb.json").write_text(json.dumps({
        "review_id": "hb", "type": "hide_semantic_page", "status": "approved",
        "subject": {"node_id": B, "page": f"Concepts/{b_slug}.md"},
        "proposal": {"to_status": "hidden"}, "context": {"node_type": "concept"}}), encoding="utf-8")
    res = _apply(tmp_path, conn)
    assert res["applied"] == 0
    assert {"review_id": "rev_m", "reason": "approved_unapplied_references_absorbed"} in res["skipped"]
    conn.close()


# --- projector / reopen / validator / API ----------------------------------


def test_effect_merge_pending_then_effected(tmp_path):
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    _concept(tmp_path, conn, B, "Beta")
    item = {"type": "merge_concepts", "status": "approved",
            "subject": {"survivor_node_id": A, "absorbed_node_id": B}}
    assert review_read._effect_merge(item, conn, tmp_path / "wiki")[0] == review_read.PENDING_APPLY
    _approve(tmp_path)
    _apply(tmp_path, conn)
    assert review_read._effect_merge(item, conn, tmp_path / "wiki")[0] == review_read.EFFECTED
    conn.close()


def test_validator_rejects_active_edge_with_merged_endpoint(tmp_path):
    import validate_graph
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    _concept(tmp_path, conn, B, "Beta")
    _mention(conn, SID, B, span=(0, 4))
    # force the broken state: mark B merged WITHOUT re-pointing its active edge
    graph.upsert_node(conn, node_id=B, node_type="concept", slug="beta", status="merged")
    conn.commit()
    conn.close()
    assert validate_graph.main([str(tmp_path)]) != 0             # active edge -> merged endpoint -> fail


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


def test_api_apply_merges_with_summary(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module.retention, "reindex_keyword", lambda root: None)
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    _concept(tmp_path, conn, B, "Beta")
    conn.commit()
    conn.close()
    _approve(tmp_path)
    body = client.post("/reviews/apply").json()
    assert body["status"] == "applied" and body["summary"]["merged"]["applied"] == 1


def test_api_merge_graph_required_503(client, tmp_path):
    _approve(tmp_path)
    assert not (tmp_path / "db" / "graph.sqlite").exists()
    assert client.post("/reviews/apply").status_code == 503


def test_api_merge_reindex_failure_is_non_clean(client, tmp_path, monkeypatch):
    def boom(root):
        raise RuntimeError("reindex blew up")
    monkeypatch.setattr(main_module.retention, "reindex_keyword", boom)
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    _concept(tmp_path, conn, B, "Beta")
    conn.commit()
    conn.close()
    _approve(tmp_path)
    body = client.post("/reviews/apply").json()
    assert body["status"] == "validation_failed"
    assert "merge_discovery_reindex_not_guaranteed" in body["warnings"]
    assert graph.connect(tmp_path / "db" / "graph.sqlite").execute(
        "SELECT status FROM nodes WHERE node_id=?", (B,)).fetchone()["status"] == "merged"


def test_dry_run_merge_shows_edges_repointed(client, tmp_path):
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    _concept(tmp_path, conn, B, "Beta")
    _mention(conn, SID, B, span=(0, 4))
    conn.commit()
    conn.close()
    _approve(tmp_path)
    dry = client.post("/reviews/apply/dry-run").json()
    rep = dry["diff"]["graph"]["edges_repointed"]
    assert any(e["rel"] == "mentions" and e["from_dst"] == B and e["to_dst"] == A for e in rep)
    # live graph untouched by the dry-run: B still active, the mention still points to B
    live = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert live.execute("SELECT status FROM nodes WHERE node_id=?", (B,)).fetchone()["status"] == "active"
    assert live.execute("SELECT dst_id FROM edges WHERE edge_type='mentions'").fetchone()["dst_id"] == B
    live.close()


def test_merged_excluded_from_search_nav_e2e(client, tmp_path):
    from app.backend import keyword_index
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    _concept(tmp_path, conn, B, "Beta")
    conn.commit()
    conn.close()
    keyword_index.reindex(tmp_path, force=True)
    nav = lambda r: {n.get("node_id") for n in r.json()["navigation"]}  # noqa: E731
    q = {"q": "Beta", "mode": "navigation"}
    assert B in nav(client.get("/search", params=q))                     # baseline
    _approve(tmp_path)
    assert client.post("/reviews/apply").json()["status"] == "applied"
    assert B not in nav(client.get("/search", params=q))                 # merged -> excluded by default
    assert B in nav(client.get("/search", params={**q, "node_status": "merged"}))   # explicit surfaces it


def test_api_reopen_blocked_after_merge(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module.retention, "reindex_keyword", lambda root: None)
    conn = _graph(tmp_path)
    _concept(tmp_path, conn, A, "Alpha")
    _concept(tmp_path, conn, B, "Beta")
    conn.commit()
    conn.close()
    _approve(tmp_path)
    client.post("/reviews/apply")
    r = client.post("/reviews/rev_m/reopen", json={"reason": "undo"})
    assert r.status_code == 409 and "already_applied" in r.json()["detail"]   # forward-only
