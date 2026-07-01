"""ADR-0051 identity surgery: entity subtype rekey (change_entity_subtype), forward-only.

A subtype rekey is a single-node 1:1 relabel: mint the new-subtype node at the prefix-substituted id
(same frozen hash), re-point the old node's active edges, tombstone the old id (`rekeyed` + rekeyed_to).
Virgin-target-only (three block gates); re-point BEFORE render; not a merge (no 2->1 collapse).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pytest
from fastapi.testclient import TestClient

from app.backend import graph, review_read
from app.backend import main as main_module
from app.backend.config import get_settings
from app.workers import concepts, rekeys
from app.workers.wiki_render import NODE_DIR, parse_frontmatter, render_concept_page

E = concepts.node_id("entity", "Acme")            # ent_<hash("acme")>
ORG = concepts.node_id("organization", "Acme")    # org_<same hash>
SID = "src_0123456789abcdef"
SID2 = "src_fedcba9876543210"


def _graph(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    return graph.connect(gdb)


def _node(tmp_path, conn, nid, title, node_type, *, status="active", aliases=(), slug=None):
    slug = slug or concepts._slug(title)
    graph.upsert_node(conn, node_id=nid, node_type=node_type, slug=slug, status=status)
    page = tmp_path / "wiki" / NODE_DIR[node_type] / f"{slug}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(render_concept_page({
        "node_type": node_type, "node_id": nid, "id_field": concepts.ID_FIELD[node_type], "title": title,
        "aliases": list(aliases), "confidence": "low",
        "source_ids": graph.sources_for_node(conn, nid), "status": status,
        "duplicates": graph.active_duplicates(conn, nid),
    }), encoding="utf-8")
    conn.commit()
    return slug


def _mention(conn, sid, nid, *, status="active", span=None):
    graph.upsert_node(conn, node_id=sid, node_type="source", slug=sid, status="active")
    anchor = {"evidence_source_id": sid, "evidence_char_start": span[0],
              "evidence_char_end": span[1]} if span else {}
    eid = graph.upsert_assertion(conn, src_id=sid, dst_id=nid, edge_type="mentions", asserted_by="llm",
                                 status=status, **anchor)
    conn.commit()
    return eid


def _approve(tmp_path, *, node_id=E, to_type="organization", rid="rev_r", proposal_to=None):
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": "change_entity_subtype", "status": "approved",
        "subject": {"node_id": node_id, "to_type": to_type},
        "proposal": {"to_type": proposal_to if proposal_to is not None else to_type}}), encoding="utf-8")
    return rid


def _pending(tmp_path, rid, rtype, subject, proposal=None):
    d = tmp_path / "reviews" / "pending"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": rtype, "status": "pending",
        "subject": subject, "proposal": proposal or {}}), encoding="utf-8")


def _apply(tmp_path, conn):
    return rekeys.apply_rekeys(conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")


def _page(tmp_path, node_type, slug):
    return tmp_path / "wiki" / NODE_DIR[node_type] / f"{slug}.md"


def _fm(tmp_path, node_type, slug):
    return parse_frontmatter(_page(tmp_path, node_type, slug).read_text())


# --- core: mint + tombstone + edge re-point --------------------------------


def test_rekey_mints_new_and_tombstones_old(tmp_path):
    conn = _graph(tmp_path)
    _node(tmp_path, conn, E, "Acme", "entity", aliases=["ACME Inc"])
    _mention(conn, SID, E, span=(0, 4))
    _approve(tmp_path)
    res = _apply(tmp_path, conn)
    assert res["applied"] == 1 and res["skipped"] == []
    # new-subtype node minted at the prefix-substituted id (SAME frozen hash), active, with the mention
    assert ORG == rekeys._new_id(E, "organization") == "org_" + E.split("_", 1)[1]
    n_new = graph.get_node(conn, ORG)
    assert n_new and n_new["status"] == "active"
    assert graph.sources_for_node(conn, ORG) == [SID]        # mention re-pointed to the new id
    assert graph.sources_for_node(conn, E) == []             # old id has no active mention left
    # old id tombstoned: rekeyed + rekeyed_to, page kept at the OLD dir, title/aliases copied
    n_old = graph.get_node(conn, E)
    assert n_old["status"] == "rekeyed"
    fm_old = _fm(tmp_path, "entity", "acme")
    assert fm_old["status"] == "rekeyed" and fm_old["rekeyed_to"] == ORG
    assert fm_old["rekey_review_id"] == "rev_r" and fm_old.get("rekeyed_at")
    fm_new = _fm(tmp_path, "organization", "acme")
    assert fm_new["status"] == "active" and fm_new["title"] == "Acme"
    assert "ACME Inc" in fm_new["aliases"]
    assert res["affected_sources"] == [SID]                  # Source page re-render fan-out
    conn.close()


def test_rekey_preserves_candidate_and_withdraws_promotion(tmp_path):
    conn = _graph(tmp_path)
    _node(tmp_path, conn, E, "Acme", "entity", status="candidate")
    _mention(conn, SID, E)
    _pending(tmp_path, "rev_promo", "promote_candidate_node", {"node_id": E})
    _approve(tmp_path)
    res = _apply(tmp_path, conn)
    assert res["applied"] == 1
    assert graph.get_node(conn, ORG)["status"] == "candidate"     # status PRESERVED (not forced active)
    # the pending promotion of the OLD id is withdrawn (not promoted)
    assert not (tmp_path / "reviews" / "pending" / "rev_promo.json").exists()
    audits = list((tmp_path / "reviews" / "audit_log").glob("rev_promo-withdrawn-*.json"))
    assert audits and json.loads(audits[0].read_text())["note"] == "superseded_by_rekey"  # ADR-0051 reason
    conn.close()


def test_rekey_idempotent_on_reapply(tmp_path):
    conn = _graph(tmp_path)
    _node(tmp_path, conn, E, "Acme", "entity")
    _approve(tmp_path)
    _apply(tmp_path, conn)
    res2 = _apply(tmp_path, conn)                                 # old already rekeyed -> no-op
    assert res2["applied"] == 0 and res2["changed_pages"] == []
    conn.close()


def test_rekey_summary_callout_on_tombstone(tmp_path):
    conn = _graph(tmp_path)
    _node(tmp_path, conn, E, "Acme", "entity")
    _approve(tmp_path)
    _apply(tmp_path, conn)
    body = _page(tmp_path, "entity", "acme").read_text()
    assert "> [!summary] Retyped entity" in body and "Retyped into" in body   # required callout kept
    conn.close()


# --- Crux A: three virgin-target block gates -------------------------------


@pytest.mark.parametrize("occ_status", ["active", "candidate", "stale_candidate", "deprecated_candidate",
                                        "archive_candidate", "archived", "delete_candidate", "deleted",
                                        "hidden", "evidence_hidden", "merged", "rekeyed"])
def test_target_subtype_id_exists_blocks_for_any_status(tmp_path, occ_status):
    # the target slot is occupied by a node of ANY lifecycle status -> block, status-agnostic (Crux A).
    conn = _graph(tmp_path)
    _node(tmp_path, conn, E, "Acme", "entity")
    graph.upsert_node(conn, node_id=ORG, node_type="organization", slug="acme", status=occ_status)
    conn.commit()
    _approve(tmp_path)
    res = _apply(tmp_path, conn)
    assert res["skipped"] == [{"review_id": "rev_r", "reason": "target_subtype_id_exists"}]
    assert graph.get_node(conn, E)["status"] == "active"         # no mutation
    conn.close()


def test_target_subtype_page_exists_blocks(tmp_path):
    # orphan target page on disk with NO graph node (wiki/graph drift) -> block, no silent overwrite.
    conn = _graph(tmp_path)
    _node(tmp_path, conn, E, "Acme", "entity")
    orphan = _page(tmp_path, "organization", "acme")
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("orphan page — not overwritten\n", encoding="utf-8")
    _approve(tmp_path)
    res = _apply(tmp_path, conn)
    assert res["skipped"] == [{"review_id": "rev_r", "reason": "target_subtype_page_exists"}]
    assert orphan.read_text() == "orphan page — not overwritten\n"
    conn.close()


def test_target_assertion_exists_blocks(tmp_path):
    # A DANGLING target assertion (drift/tamper the write-guards can't otherwise produce): forge an edge at
    # the identity E's re-pointed mention would produce, then drop the target node so no node/page occupies
    # the slot -> the dry plan finds the collision and BLOCKs before minting (never collapse/resurrect).
    conn = _graph(tmp_path)
    _node(tmp_path, conn, E, "Acme", "entity")
    _mention(conn, SID2, E)                                       # (SID2 -> E) mentions, no anchor
    graph.upsert_node(conn, node_id=ORG, node_type="organization", slug="acme", status="active")
    graph.upsert_assertion(conn, src_id=SID2, dst_id=ORG, edge_type="mentions", asserted_by="llm",
                           status="superseded")
    conn.execute("DELETE FROM nodes WHERE node_id = ?", (ORG,))   # leave the edge dangling
    conn.commit()
    _approve(tmp_path)
    res = _apply(tmp_path, conn)
    assert res["skipped"] == [{"review_id": "rev_r", "reason": "target_assertion_exists"}]
    assert graph.get_node(conn, ORG) is None                     # never (re-)minted
    conn.close()


# --- derivation / subject guards -------------------------------------------


def test_noncanonical_node_id_blocks(tmp_path):
    conn = _graph(tmp_path)
    _approve(tmp_path, node_id="ent_notahex", rid="rev_bad")
    res = _apply(tmp_path, conn)
    assert res["skipped"] == [{"review_id": "rev_bad", "reason": "noncanonical_node_id"}]
    conn.close()


def test_to_type_mismatch_blocks(tmp_path):
    conn = _graph(tmp_path)
    _node(tmp_path, conn, E, "Acme", "entity")
    _approve(tmp_path, to_type="organization", proposal_to="person")   # proposal disagrees with subject
    res = _apply(tmp_path, conn)
    assert res["skipped"] == [{"review_id": "rev_r", "reason": "to_type_mismatch"}]
    conn.close()


def test_noop_same_type_is_typed_skip(tmp_path):
    conn = _graph(tmp_path)
    _node(tmp_path, conn, E, "Acme", "entity")
    _approve(tmp_path, to_type="entity")                         # same type
    res = _apply(tmp_path, conn)
    assert res["skipped"] == [{"review_id": "rev_r", "reason": "noop_same_type"}]
    assert graph.get_node(conn, E)["status"] == "active"         # mutates nothing
    conn.close()


def test_invalid_to_type_and_out_of_scope(tmp_path):
    conn = _graph(tmp_path)
    _node(tmp_path, conn, E, "Acme", "entity")
    _approve(tmp_path, to_type="concept", rid="rev_c")           # concept target -> out of family
    assert _apply(tmp_path, conn)["skipped"] == [{"review_id": "rev_c", "reason": "invalid_to_type"}]
    # concept SUBJECT (old node is a concept) -> out_of_scope
    C = concepts.node_id("concept", "Idea")
    _node(tmp_path, conn, C, "Idea", "concept")
    _approve(tmp_path, node_id=C, to_type="organization", rid="rev_c2")
    assert {"review_id": "rev_c2", "reason": "out_of_scope"} in _apply(tmp_path, conn)["skipped"]
    conn.close()


@pytest.mark.parametrize("status", ["stale_candidate", "deprecated_candidate", "archive_candidate",
                                    "archived", "delete_candidate", "deleted", "hidden", "evidence_hidden",
                                    "merged"])
def test_node_not_retypable_for_every_excluded_status(tmp_path, status):
    # only active/candidate are retypable; every other lifecycle status skips node_not_retypable (a `rekeyed`
    # node is instead the idempotent no-op, covered separately). Node-status check precedes the page read.
    conn = _graph(tmp_path)
    graph.upsert_node(conn, node_id=E, node_type="entity", slug="acme", status=status)
    conn.commit()
    _approve(tmp_path)
    assert _apply(tmp_path, conn)["skipped"] == [{"review_id": "rev_r", "reason": "node_not_retypable"}]
    conn.close()


def test_candidate_rekey_passes_validators(tmp_path):
    # B1 regression: a candidate rekey mints a `candidate` target; validate_projection must accept a
    # rekeyed_to pointing at an active-OR-candidate node.
    import validate_projection
    conn = _graph(tmp_path)
    _node(tmp_path, conn, E, "Acme", "entity", status="candidate")
    _mention(conn, SID, E)
    _approve(tmp_path)
    _apply(tmp_path, conn)
    assert graph.get_node(conn, ORG)["status"] == "candidate"
    conn.close()
    assert validate_projection.main([str(tmp_path)]) == 0        # candidate rekeyed_to accepted


def test_invalid_repoint_endpoint_blocks_on_duplicates(tmp_path):
    # E `duplicates` a same-type entity partner; retyping E to org breaks the SAME_TYPE_EDGES contract.
    conn = _graph(tmp_path)
    P = concepts.node_id("entity", "Acme Corp")
    _node(tmp_path, conn, E, "Acme", "entity")
    _node(tmp_path, conn, P, "Acme Corp", "entity")
    lo, hi = sorted((E, P))
    graph.upsert_assertion(conn, src_id=lo, dst_id=hi, edge_type="duplicates", asserted_by="human",
                           status="active")
    conn.commit()
    _approve(tmp_path)
    res = _apply(tmp_path, conn)
    assert res["skipped"] == [{"review_id": "rev_r", "reason": "invalid_repoint_endpoint"}]
    assert graph.get_node(conn, ORG) is None                     # blocked before any mint
    conn.close()


def test_rekey_uses_frozen_hash_not_current_title(tmp_path):
    # a renamed node: page title differs from the name that seeded the id. The new id must carry the FROZEN
    # hash (prefix swap), not re-hash the current title; the new page copies the current title.
    conn = _graph(tmp_path)
    _node(tmp_path, conn, E, "Acme Renamed", "entity", slug="acme-renamed")  # id from "Acme", title changed
    _mention(conn, SID, E)
    _approve(tmp_path)
    _apply(tmp_path, conn)
    assert graph.get_node(conn, ORG) is not None                 # org_<hash("acme")>, frozen
    assert ORG != concepts.node_id("organization", "Acme Renamed")
    assert _fm(tmp_path, "organization", "acme-renamed")["title"] == "Acme Renamed"  # title copied
    conn.close()


# --- projector (reopen safety) + review_id distinctness --------------------


def test_projector_effect_states(tmp_path):
    conn = _graph(tmp_path)
    _node(tmp_path, conn, E, "Acme", "entity")
    rid = _approve(tmp_path)
    item = json.loads((tmp_path / "reviews" / "approved" / f"{rid}.json").read_text())
    # before apply: fully live at the old id -> PENDING_APPLY (reopenable)
    st, _ = review_read._effect_rekey(item, conn, tmp_path / "wiki")
    assert st == review_read.PENDING_APPLY
    _apply(tmp_path, conn)
    st2, _ = review_read._effect_rekey(item, conn, tmp_path / "wiki")
    assert st2 == review_read.EFFECTED                           # cleanly applied
    conn.close()


def test_projector_partial_rekey_state_is_unknown(tmp_path):
    # graph node rekeyed but the page is NOT a tombstone -> partial -> UNKNOWN (not reopenable).
    conn = _graph(tmp_path)
    slug = _node(tmp_path, conn, E, "Acme", "entity")
    graph.upsert_node(conn, node_id=E, node_type="entity", slug=slug, status="rekeyed")
    conn.commit()
    rid = _approve(tmp_path)
    item = json.loads((tmp_path / "reviews" / "approved" / f"{rid}.json").read_text())
    st, warns = review_read._effect_rekey(item, conn, tmp_path / "wiki")
    assert st == review_read.UNKNOWN and "partial_rekey_state" in warns
    conn.close()


def _clean_applied_item(tmp_path, conn, *, old_status="active"):
    """Apply a rekey and return its approved item dict (for projector tampering tests)."""
    _node(tmp_path, conn, E, "Acme", "entity", status=old_status)
    _mention(conn, SID, E)
    rid = _approve(tmp_path)
    _apply(tmp_path, conn)
    return json.loads((tmp_path / "reviews" / "approved" / f"{rid}.json").read_text())


def test_effect_rekey_target_missing_is_unknown(tmp_path):
    conn = _graph(tmp_path)
    item = _clean_applied_item(tmp_path, conn)
    conn.execute("DELETE FROM nodes WHERE node_id = ?", (ORG,))     # target vanished after apply
    conn.commit()
    st, warns = review_read._effect_rekey(item, conn, tmp_path / "wiki")
    assert st == review_read.UNKNOWN and "partial_rekey_state" in warns
    conn.close()


def test_effect_rekey_wrong_rekeyed_to_is_unknown(tmp_path):
    conn = _graph(tmp_path)
    item = _clean_applied_item(tmp_path, conn)
    page = _page(tmp_path, "entity", "acme")                       # tamper the tombstone's target pointer
    page.write_text(page.read_text().replace(f'rekeyed_to: "{ORG}"', 'rekeyed_to: "org_deadbeefdeadbeef"'),
                    encoding="utf-8")
    st, warns = review_read._effect_rekey(item, conn, tmp_path / "wiki")
    assert st == review_read.UNKNOWN and "partial_rekey_state" in warns
    conn.close()


def test_effect_rekey_target_inactive_is_unknown(tmp_path):
    conn = _graph(tmp_path)
    item = _clean_applied_item(tmp_path, conn)
    graph.upsert_node(conn, node_id=ORG, node_type="organization", slug="acme",
                      status="deprecated_candidate")               # target fell out of active/candidate
    conn.commit()
    st, warns = review_read._effect_rekey(item, conn, tmp_path / "wiki")
    assert st == review_read.UNKNOWN and "partial_rekey_state" in warns
    conn.close()


def test_effect_rekey_candidate_target_is_effected(tmp_path):
    conn = _graph(tmp_path)
    item = _clean_applied_item(tmp_path, conn, old_status="candidate")
    assert graph.get_node(conn, ORG)["status"] == "candidate"
    assert review_read._effect_rekey(item, conn, tmp_path / "wiki")[0] == review_read.EFFECTED
    conn.close()


def _half_mint_item(tmp_path, conn, *, mint_page):
    """Simulate a crash between the target mint and the old-id tombstone: old id still live, target already
    created (graph node, and optionally its page)."""
    slug = _node(tmp_path, conn, E, "Acme", "entity")            # old id untouched (active)
    if mint_page:
        graph.upsert_node(conn, node_id=ORG, node_type="organization", slug=slug, status="active")
        _page(tmp_path, "organization", slug).parent.mkdir(parents=True, exist_ok=True)
        _page(tmp_path, "organization", slug).write_text(render_concept_page({
            "node_type": "organization", "node_id": ORG, "id_field": "organization_id", "title": "Acme",
            "aliases": [], "confidence": "low", "source_ids": [], "status": "active", "duplicates": []}),
            encoding="utf-8")
    else:
        graph.upsert_node(conn, node_id=ORG, node_type="organization", slug=slug, status="active")
    conn.commit()
    rid = _approve(tmp_path)
    return json.loads((tmp_path / "reviews" / "approved" / f"{rid}.json").read_text())


def test_effect_rekey_half_mint_target_node_is_unknown(tmp_path):
    # old id live, target graph node exists but its page NOT yet written (crash right after the node mint) ->
    # the node alone drives UNKNOWN, NOT PENDING_APPLY.
    conn = _graph(tmp_path)
    item = _half_mint_item(tmp_path, conn, mint_page=False)
    assert not _page(tmp_path, "organization", "acme").exists()  # node-only: no target page
    st, warns = review_read._effect_rekey(item, conn, tmp_path / "wiki")
    assert st == review_read.UNKNOWN and "partial_rekey_state" in warns
    conn.close()


def test_effect_rekey_half_mint_target_page_only_is_unknown(tmp_path):
    # old id live, target graph node absent but a target PAGE exists on disk (drift) -> UNKNOWN.
    conn = _graph(tmp_path)
    slug = _node(tmp_path, conn, E, "Acme", "entity")
    _page(tmp_path, "organization", slug).parent.mkdir(parents=True, exist_ok=True)
    _page(tmp_path, "organization", slug).write_text(render_concept_page({
        "node_type": "organization", "node_id": ORG, "id_field": "organization_id", "title": "Acme",
        "aliases": [], "confidence": "low", "source_ids": [], "status": "active", "duplicates": []}),
        encoding="utf-8")
    conn.commit()
    rid = _approve(tmp_path)
    item = json.loads((tmp_path / "reviews" / "approved" / f"{rid}.json").read_text())
    assert graph.get_node(conn, ORG) is None                     # no target node, only the page
    st, warns = review_read._effect_rekey(item, conn, tmp_path / "wiki")
    assert st == review_read.UNKNOWN and "partial_rekey_state" in warns
    conn.close()


def test_reopen_refused_for_half_mint_state(client, tmp_path):
    # a half-minted rekey must NOT be reopenable (that would strand the orphan target); the endpoint 409s.
    conn = _graph(tmp_path)
    _half_mint_item(tmp_path, conn, mint_page=True)
    conn.close()
    r = client.post("/reviews/rev_r/reopen", json={"reason": "changed my mind"})
    assert r.status_code == 409


def test_rejected_target_does_not_lock_other_target(tmp_path):
    # subject {node_id, to_type}: distinct target types -> distinct review_ids (a rejected org retype does
    # not block a person retype of the same node).
    from app.workers import reviews
    id_org = reviews.review_id("change_entity_subtype", {"node_id": E, "to_type": "organization"})
    id_per = reviews.review_id("change_entity_subtype", {"node_id": E, "to_type": "person"})
    assert id_org != id_per


# --- validators -------------------------------------------------------------


def test_validate_projection_accepts_clean_rekey_and_rejects_tamper(tmp_path):
    import validate_projection
    conn = _graph(tmp_path)
    _node(tmp_path, conn, E, "Acme", "entity")
    _mention(conn, SID, E)
    _approve(tmp_path)
    _apply(tmp_path, conn)
    conn.close()
    assert validate_projection.main([str(tmp_path)]) == 0        # clean rekey passes
    # tamper: point rekeyed_to at a SAME-name-hash but wrong shape — an un-indexed target
    page = _page(tmp_path, "entity", "acme")
    page.write_text(page.read_text().replace(f'rekeyed_to: "{ORG}"', 'rekeyed_to: "org_deadbeefdeadbeef"'),
                    encoding="utf-8")
    assert validate_projection.main([str(tmp_path)]) != 0


def test_validate_projection_rejects_same_hash_violation(tmp_path):
    # rekeyed_to must share the old id's name-hash; a different-hash target is rejected even if active.
    import validate_projection
    conn = _graph(tmp_path)
    _node(tmp_path, conn, E, "Acme", "entity")
    other = concepts.node_id("organization", "Other")           # active, different-type, DIFFERENT hash
    _node(tmp_path, conn, other, "Other", "organization")
    _approve(tmp_path)
    _apply(tmp_path, conn)
    conn.close()
    page = _page(tmp_path, "entity", "acme")
    page.write_text(page.read_text().replace(f'rekeyed_to: "{ORG}"', f'rekeyed_to: "{other}"'),
                    encoding="utf-8")
    assert validate_projection.main([str(tmp_path)]) != 0        # same-hash invariant violated


def test_validate_graph_rejects_active_edge_to_rekeyed_endpoint(tmp_path):
    import validate_graph
    conn = _graph(tmp_path)
    _node(tmp_path, conn, E, "Acme", "entity")
    _approve(tmp_path)
    _apply(tmp_path, conn)
    # forge an ACTIVE edge back onto the rekeyed (old) id -> the re-point invariant is violated
    graph.upsert_node(conn, node_id=SID, node_type="source", slug=SID, status="active")
    graph.upsert_assertion(conn, src_id=SID, dst_id=E, edge_type="mentions", asserted_by="llm",
                           status="active")
    conn.commit()
    conn.close()
    assert validate_graph.main([str(tmp_path)]) != 0


# --- end-to-end via /reviews/apply + /reviews/apply/dry-run ----------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


def test_api_apply_rekey_with_summary(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module.retention, "reindex_keyword", lambda root: None)
    conn = _graph(tmp_path)
    _node(tmp_path, conn, E, "Acme", "entity")
    conn.close()
    _approve(tmp_path)
    body = client.post("/reviews/apply").json()
    assert body["status"] == "applied" and body["summary"]["rekeyed"]["applied"] == 1
    conn2 = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert conn2.execute("SELECT status FROM nodes WHERE node_id=?", (E,)).fetchone()["status"] == "rekeyed"
    assert conn2.execute("SELECT status FROM nodes WHERE node_id=?", (ORG,)).fetchone()["status"] == "active"


def test_api_rekey_graph_required_503(client, tmp_path):
    _approve(tmp_path)
    assert not (tmp_path / "db" / "graph.sqlite").exists()
    assert client.post("/reviews/apply").status_code == 503        # change_entity_subtype is graph-required


def test_api_rekey_reindex_failure_is_non_clean(client, tmp_path, monkeypatch):
    def boom(root):
        raise RuntimeError("reindex blew up")
    monkeypatch.setattr(main_module.retention, "reindex_keyword", boom)
    conn = _graph(tmp_path)
    _node(tmp_path, conn, E, "Acme", "entity")
    conn.close()
    _approve(tmp_path)
    body = client.post("/reviews/apply").json()
    assert body["status"] == "validation_failed"
    assert "rekey_discovery_reindex_not_guaranteed" in body["warnings"]


def test_dry_run_rekey_shows_mint_repoint_and_tombstone(client, tmp_path):
    conn = _graph(tmp_path)
    _node(tmp_path, conn, E, "Acme", "entity")
    _mention(conn, SID, E, span=(0, 4))
    conn.close()
    _approve(tmp_path)
    dry = client.post("/reviews/apply/dry-run").json()
    g = dry["diff"]["graph"]
    assert any(e["rel"] == "mentions" and e["from_dst"] == E and e["to_dst"] == ORG
               for e in g["edges_repointed"])                       # mention re-pointed old -> new
    assert any(n["id"] == ORG for n in g["nodes_added"])            # new-subtype node minted
    assert any(n["id"] == E and n["to"] == "rekeyed" for n in g["nodes_status_changed"])  # old tombstoned
    # live graph untouched by the dry-run
    live = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert live.execute("SELECT status FROM nodes WHERE node_id=?", (E,)).fetchone()["status"] == "active"
    assert live.execute("SELECT COUNT(*) c FROM nodes WHERE node_id=?", (ORG,)).fetchone()["c"] == 0
