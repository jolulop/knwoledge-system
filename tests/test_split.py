"""ADR-0052 identity surgery: entity split (split_entity), forward-only — the inverse of merge.

Split divides one entity-family node's evidence into a surviving primary A (keeps id+name) and a
freshly-minted spin-off B (candidate). The human partitions A's mentions (spinoff_sources, MOVE) + aliases;
B is minted, A keeps the rest. Nothing is retired (no tombstone, no withdrawal). Virgin-target + identity
gates; never partial-apply.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from app.backend import graph, review_read
from app.backend import main as main_module
from app.backend.config import get_settings
from app.workers import concepts, merges, reviews, splits, wiki
from app.workers.wiki_render import NODE_DIR, parse_frontmatter, render_concept_page

A = concepts.node_id("entity", "Washington")
SPINOFF = "George Washington"
B = concepts.node_id("entity", SPINOFF)
S1, S2, S3 = "src_1111111111111111", "src_2222222222222222", "src_3333333333333333"
_OLD = "2020-01-01T00:00:00+00:00"


def _graph(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    return graph.connect(gdb)


def _entity(tmp_path, conn, nid, title, *, status="active", aliases=(), slug=None):
    slug = slug or concepts._slug(title)
    graph.upsert_node(conn, node_id=nid, node_type="entity", slug=slug, status=status)
    page = tmp_path / "wiki" / "Entities" / f"{slug}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(render_concept_page({
        "node_type": "entity", "node_id": nid, "id_field": "entity_id", "title": title,
        "aliases": list(aliases), "confidence": "low",
        "source_ids": graph.sources_for_node(conn, nid), "status": status,
        "duplicates": graph.active_duplicates(conn, nid),
    }), encoding="utf-8")
    conn.commit()
    return slug


def _mention(conn, sid, nid, *, span=None):
    graph.upsert_node(conn, node_id=sid, node_type="source", slug=sid, status="active")
    anchor = {"evidence_source_id": sid, "evidence_char_start": span[0],
              "evidence_char_end": span[1]} if span else {}
    eid = graph.upsert_assertion(conn, src_id=sid, dst_id=nid, edge_type="mentions", asserted_by="llm",
                                 status="active", **anchor)
    conn.commit()
    return eid


def _approve(tmp_path, *, a=A, spinoff_name=SPINOFF, sources=(S3,), aliases=(), rid="rev_s",
             spinoff_id=None):
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    b = spinoff_id if spinoff_id is not None else concepts.node_id("entity", spinoff_name)
    (d / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": "split_entity", "status": "approved",
        "subject": {"node_id": a, "spinoff_node_id": b},
        "proposal": {"spinoff_name": spinoff_name, "spinoff_sources": list(sources),
                     "spinoff_aliases": list(aliases)}}), encoding="utf-8")
    return rid


def _approve_raw(tmp_path, *, subject, proposal, rid="rev_s"):
    """Write an approved split item with an ARBITRARY (possibly malformed) subject/proposal — bypasses
    `_approve`'s `list(...)` coercion so a non-list/unhashable/non-string field reaches the executor verbatim."""
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": "split_entity", "status": "approved",
        "subject": subject, "proposal": proposal}), encoding="utf-8")
    return rid


def _apply(tmp_path, conn):
    return splits.apply_splits(conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")


def _fm(tmp_path, slug, node_type="entity"):
    return parse_frontmatter((tmp_path / "wiki" / NODE_DIR[node_type] / f"{slug}.md").read_text())


def _full(tmp_path, conn):
    _entity(tmp_path, conn, A, "Washington", aliases=["George Washington", "DC"])
    for s in (S1, S2, S3):
        _mention(conn, s, A)


# --- core -------------------------------------------------------------------


def test_clean_split(tmp_path):
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    _approve(tmp_path, sources=[S3])
    res = _apply(tmp_path, conn)
    assert res["applied"] == 1 and res["skipped"] == []
    nb = graph.get_node(conn, B)
    assert nb and nb["status"] == "candidate"                       # spin-off born candidate
    assert graph.sources_for_node(conn, B) == [S3]                  # S3 moved to B
    assert set(graph.sources_for_node(conn, A)) == {S1, S2}         # A keeps the rest
    assert graph.get_node(conn, A)["status"] == "active"            # A status unchanged
    fm_b = _fm(tmp_path, concepts._slug(SPINOFF))
    assert fm_b["split_from"] == A and fm_b["split_review_id"] == "rev_s"
    # spin-off name auto-moved off A's aliases (it was an A alias); the other alias stays
    fm_a = _fm(tmp_path, "washington")
    assert "George Washington" not in fm_a["aliases"] and "DC" in fm_a["aliases"]
    # B's promote review filed (enters the ledger)
    b_promo = reviews.review_id("promote_candidate_node", {"node_id": B})
    assert (tmp_path / "reviews" / "pending" / f"{b_promo}.json").exists()
    assert res["affected_sources"] == [S3]
    audits = list((tmp_path / "reviews" / "audit_log").glob("rev_s-split-*.json"))
    assert audits and json.loads(audits[0].read_text())["moved_sources"] == [S3]
    conn.close()


def test_candidate_split_preserves_candidate(tmp_path):
    conn = _graph(tmp_path)
    _entity(tmp_path, conn, A, "Washington", status="candidate")
    _mention(conn, S1, A)
    _mention(conn, S2, A)
    _approve(tmp_path, sources=[S2])
    _apply(tmp_path, conn)
    assert graph.get_node(conn, A)["status"] == "candidate"        # primary preserved
    assert graph.get_node(conn, B)["status"] == "candidate"        # spin-off candidate
    conn.close()


def test_moved_aliases_partition(tmp_path):
    conn = _graph(tmp_path)
    _entity(tmp_path, conn, A, "Washington", aliases=["GW", "DC", "Potomac"])
    _mention(conn, S1, A)
    _mention(conn, S2, A)
    _approve(tmp_path, sources=[S2], aliases=["GW", "Potomac"])
    _apply(tmp_path, conn)
    assert set(_fm(tmp_path, "washington")["aliases"]) == {"DC"}   # moved aliases removed from A
    assert set(_fm(tmp_path, concepts._slug(SPINOFF))["aliases"]) == {"GW", "Potomac"}  # present on B
    conn.close()


def test_duplicate_sources_deduped_not_rejected(tmp_path):
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    _approve(tmp_path, sources=[S3, S3, S3])                        # duplicate entries
    res = _apply(tmp_path, conn)
    assert res["applied"] == 1                                     # normalized, not an error
    assert graph.sources_for_node(conn, B) == [S3]
    conn.close()


def test_idempotent_reapply_is_silent_noop(tmp_path):
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    _approve(tmp_path, sources=[S3])
    _apply(tmp_path, conn)
    res2 = _apply(tmp_path, conn)                                  # B exists w/ our lineage -> true no-op
    assert res2["applied"] == 0 and res2["skipped"] == [] and res2["changed_pages"] == []
    conn.close()


def test_repair_completes_half_applied_split_missing_promote(tmp_path):
    # A "completed-looking" half state: B node + B page lineage exist and the partition moved, but the promote
    # item was never filed (crash before step 5). Re-apply must REPAIR (re-file promote, re-fan the source),
    # NOT silently no-op on the lineage marker alone (ADR-0052 review round 2 — full-EFFECTED = no-op).
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    _approve(tmp_path, sources=[S3])
    _apply(tmp_path, conn)                                         # clean first apply
    b_promo = reviews.review_id("promote_candidate_node", {"node_id": B})
    (tmp_path / "reviews" / "pending" / f"{b_promo}.json").unlink()   # simulate the un-filed promote half-state
    res = _apply(tmp_path, conn)                                   # re-apply
    assert res["applied"] == 1 and res["skipped"] == []           # repaired, NOT a silent no-op
    assert res["affected_sources"] == [S3]                        # whole partition re-included for the fan-out
    assert (tmp_path / "reviews" / "pending" / f"{b_promo}.json").exists()   # promote re-filed
    assert graph.sources_for_node(conn, B) == [S3]                # graph unchanged (S3 already on B)
    assert set(graph.sources_for_node(conn, A)) == {S1, S2}
    conn.close()


def test_repair_refuses_ambiguous_source_on_both_a_and_b(tmp_path):
    # A listed source that mentions BOTH A and B (a half-moved edge) is ambiguous — the repair must refuse to
    # invent state and emit partial_split_state, mirroring the projector rather than silently no-op'ing.
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    _approve(tmp_path, sources=[S3])
    _apply(tmp_path, conn)                                         # clean apply: S3 -> B
    _mention(conn, S3, A)                                          # re-attach S3 -> A (now on BOTH A and B)
    res = _apply(tmp_path, conn)
    assert res["applied"] == 0
    assert res["skipped"] == [{"review_id": "rev_s", "reason": "partial_split_state"}]
    conn.close()


def test_target_spinoff_id_exists_other_identity(tmp_path):
    # the spin-off id is occupied by an UNRELATED node (no matching split lineage) -> virgin gate blocks.
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    graph.upsert_node(conn, node_id=B, node_type="entity", slug=concepts._slug(SPINOFF), status="active")
    conn.commit()
    _approve(tmp_path, sources=[S3])
    assert _apply(tmp_path, conn)["skipped"] == [{"review_id": "rev_s", "reason": "target_spinoff_id_exists"}]
    conn.close()


# --- subject / partition guards --------------------------------------------


def test_invalid_proposal_missing_name(tmp_path):
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    _approve(tmp_path, spinoff_name="", sources=[S3])
    assert _apply(tmp_path, conn)["skipped"] == [{"review_id": "rev_s", "reason": "invalid_proposal"}]
    conn.close()


def test_spinoff_id_mismatch(tmp_path):
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    _approve(tmp_path, sources=[S3], spinoff_id="ent_deadbeefdeadbeef")   # wrong precomputed id
    assert _apply(tmp_path, conn)["skipped"] == [{"review_id": "rev_s", "reason": "spinoff_id_mismatch"}]
    conn.close()


def test_spinoff_equals_primary(tmp_path):
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    _approve(tmp_path, spinoff_name="Washington", sources=[S3])    # same name -> same id as A
    assert _apply(tmp_path, conn)["skipped"] == [{"review_id": "rev_s", "reason": "spinoff_equals_primary"}]
    conn.close()


@pytest.mark.parametrize("status", ["deprecated_candidate", "hidden", "archived", "merged", "rekeyed"])
def test_node_not_splittable(tmp_path, status):
    conn = _graph(tmp_path)
    graph.upsert_node(conn, node_id=A, node_type="entity", slug="washington", status=status)
    conn.commit()
    _approve(tmp_path, sources=[S3])
    assert _apply(tmp_path, conn)["skipped"] == [{"review_id": "rev_s", "reason": "node_not_splittable"}]
    conn.close()


def test_out_of_scope_concept_subject(tmp_path):
    conn = _graph(tmp_path)
    C = concepts.node_id("concept", "Idea")
    graph.upsert_node(conn, node_id=C, node_type="concept", slug="idea", status="active")
    conn.commit()
    _approve(tmp_path, a=C, sources=[S3])
    assert _apply(tmp_path, conn)["skipped"] == [{"review_id": "rev_s", "reason": "out_of_scope"}]
    conn.close()


def test_empty_partition(tmp_path):
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    _approve(tmp_path, sources=[])
    assert _apply(tmp_path, conn)["skipped"] == [{"review_id": "rev_s", "reason": "empty_partition"}]
    conn.close()


def test_noncanonical_source_id(tmp_path):
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    _approve(tmp_path, sources=["not-a-src-id"])
    assert _apply(tmp_path, conn)["skipped"] == [{"review_id": "rev_s", "reason": "noncanonical_source_id"}]
    conn.close()


def test_source_not_mentioned(tmp_path):
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    _approve(tmp_path, sources=["src_9999999999999999"])           # canonical but not a mention of A
    assert _apply(tmp_path, conn)["skipped"] == [{"review_id": "rev_s", "reason": "source_not_mentioned"}]
    conn.close()


def test_full_partition_is_rename(tmp_path):
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    _approve(tmp_path, sources=[S1, S2, S3])                       # all sources -> rename, not split
    assert _apply(tmp_path, conn)["skipped"] == [{"review_id": "rev_s", "reason": "full_partition_is_rename"}]
    conn.close()


def test_alias_not_on_primary(tmp_path):
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    _approve(tmp_path, sources=[S3], aliases=["NotAnAlias"])
    assert _apply(tmp_path, conn)["skipped"] == [{"review_id": "rev_s", "reason": "alias_not_on_primary"}]
    conn.close()


@pytest.mark.parametrize("proposal, reason", [
    ({"spinoff_name": SPINOFF, "spinoff_sources": S3, "spinoff_aliases": []}, "invalid_proposal"),       # non-list sources
    ({"spinoff_name": SPINOFF, "spinoff_sources": [{}], "spinoff_aliases": []}, "noncanonical_source_id"),  # unhashable source entry
    ({"spinoff_name": SPINOFF, "spinoff_sources": [S3], "spinoff_aliases": [123]}, "invalid_proposal"),   # non-string alias
    ({"spinoff_name": "   ", "spinoff_sources": [S3], "spinoff_aliases": []}, "invalid_proposal"),        # whitespace-only name
])
def test_malformed_proposal_is_typed_skip_not_raise(tmp_path, proposal, reason):
    # A malformed approved artifact (machine-produced from untrusted proposals) must never raise out of apply
    # and abort the whole run_apply batch — it becomes a typed skip (ADR-0052 review round 2, blocking #2).
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    _approve_raw(tmp_path, subject={"node_id": A, "spinoff_node_id": B}, proposal=proposal)
    res = _apply(tmp_path, conn)                                   # must NOT raise
    assert res["applied"] == 0
    assert res["skipped"] == [{"review_id": "rev_s", "reason": reason}]
    conn.close()


# --- block gates ------------------------------------------------------------


def test_target_spinoff_page_exists(tmp_path):
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    orphan = tmp_path / "wiki" / "Entities" / f"{concepts._slug(SPINOFF)}.md"
    orphan.write_text("orphan page\n", encoding="utf-8")           # page but no graph node
    _approve(tmp_path, sources=[S3])
    assert _apply(tmp_path, conn)["skipped"] == [{"review_id": "rev_s", "reason": "target_spinoff_page_exists"}]
    conn.close()


def test_spinoff_promote_slot_taken(tmp_path):
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    # a stale TERMINAL promote record for computed B (approved/) blocks
    b_promo = reviews.review_id("promote_candidate_node", {"node_id": B})
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{b_promo}.json").write_text(json.dumps({
        "review_id": b_promo, "type": "promote_candidate_node", "status": "approved",
        "subject": {"node_id": B}}), encoding="utf-8")
    _approve(tmp_path, sources=[S3])
    assert _apply(tmp_path, conn)["skipped"] == [{"review_id": "rev_s", "reason": "spinoff_promote_slot_taken"}]
    conn.close()


def test_approved_unapplied_references_primary(tmp_path):
    conn = _graph(tmp_path)
    _entity(tmp_path, conn, A, "Washington", status="candidate")
    _mention(conn, S1, A)
    _mention(conn, S2, A)
    # an approved-but-unapplied promote for A (candidate, not yet promoted) -> PENDING_APPLY -> blocks
    a_promo = reviews.review_id("promote_candidate_node", {"node_id": A})
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{a_promo}.json").write_text(json.dumps({
        "review_id": a_promo, "type": "promote_candidate_node", "status": "approved",
        "subject": {"node_id": A}, "proposal": {"to_status": "active", "node_type": "entity"}}),
        encoding="utf-8")
    _approve(tmp_path, sources=[S2])
    assert _apply(tmp_path, conn)["skipped"] == [
        {"review_id": "rev_s", "reason": "approved_unapplied_references_primary"}]
    conn.close()


# --- projector --------------------------------------------------------------


def _item(tmp_path, rid="rev_s"):
    return json.loads((tmp_path / "reviews" / "approved" / f"{rid}.json").read_text())


def test_projector_pending_then_effected(tmp_path):
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    _approve(tmp_path, sources=[S3])
    item = _item(tmp_path)
    assert review_read._effect_split(item, conn, tmp_path / "wiki")[0] == review_read.PENDING_APPLY
    _apply(tmp_path, conn)
    assert review_read._effect_split(item, conn, tmp_path / "wiki")[0] == review_read.EFFECTED
    conn.close()


def test_projector_half_mint_missing_promote_is_unknown(tmp_path):
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    _approve(tmp_path, sources=[S3])
    _apply(tmp_path, conn)
    # simulate the half-mint: B minted + mentions moved, but the promote item never filed
    b_promo = reviews.review_id("promote_candidate_node", {"node_id": B})
    (tmp_path / "reviews" / "pending" / f"{b_promo}.json").unlink()
    st, warns = review_read._effect_split(_item(tmp_path), conn, tmp_path / "wiki")
    assert st == review_read.UNKNOWN and "partial_split_state" in warns
    conn.close()


def test_projector_half_mint_partial_move_is_unknown(tmp_path):
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    _approve(tmp_path, sources=[S3])
    # B minted but the mention NOT moved (crash before repoint) -> partial
    graph.upsert_node(conn, node_id=B, node_type="entity", slug=concepts._slug(SPINOFF), status="candidate")
    conn.commit()
    st, warns = review_read._effect_split(_item(tmp_path), conn, tmp_path / "wiki")
    assert st == review_read.UNKNOWN and "partial_split_state" in warns
    conn.close()


def test_projector_rejected_promote_is_accounted_effected(tmp_path):
    # ADR-0052: a terminal *rejected* promote for B post-split is a deliberate human accounting ("split done,
    # chose not to promote B") — a filled ledger slot, NOT a partial split, so _effect_split stays EFFECTED.
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    _approve(tmp_path, sources=[S3])
    _apply(tmp_path, conn)
    b_promo = reviews.review_id("promote_candidate_node", {"node_id": B})
    dst = tmp_path / "reviews" / "rejected" / f"{b_promo}.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "reviews" / "pending" / f"{b_promo}.json").rename(dst)   # human rejects B's promote
    st, _warns = review_read._effect_split(_item(tmp_path), conn, tmp_path / "wiki")
    assert st == review_read.EFFECTED
    conn.close()


# --- preservation + validators ---------------------------------------------


def test_split_from_preserved_across_recompose(tmp_path):
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    _approve(tmp_path, sources=[S3])
    _apply(tmp_path, conn)
    # a later recompose of B (e.g. deprecation/hide seam) must keep split_from/split_review_id
    concepts.recompose_semantic_node_page(conn, node_id=B, wiki_dir=tmp_path / "wiki",
                                          status="candidate", review_status="none")
    fm_b = _fm(tmp_path, concepts._slug(SPINOFF))
    assert fm_b["split_from"] == A and fm_b["split_review_id"] == "rev_s"
    conn.close()


def test_validate_graph_accepts_clean_split(tmp_path):
    import validate_graph
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    _approve(tmp_path, sources=[S3])
    _apply(tmp_path, conn)
    conn.close()
    assert validate_graph.main([str(tmp_path)]) == 0


def test_rollback_via_merge_after_promotion(tmp_path):
    # ADR-0052: rollback = merge_entities(spin-off -> primary), available once the spin-off is active.
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    _approve(tmp_path, sources=[S3])
    _apply(tmp_path, conn)
    # promote B to active (its page + node) so merge (active-only) can absorb it
    concepts.recompose_semantic_node_page(conn, node_id=B, wiki_dir=tmp_path / "wiki",
                                          status="active", review_status="none")
    conn.commit()
    d = tmp_path / "reviews" / "approved"
    (d / "rev_merge.json").write_text(json.dumps({
        "review_id": "rev_merge", "type": "merge_entities", "status": "approved",
        "subject": {"survivor_node_id": A, "absorbed_node_id": B},
        "proposal": {"to_status": "merged"}}), encoding="utf-8")
    merges.apply_merges(conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")
    assert graph.get_node(conn, B)["status"] == "merged"
    assert set(graph.sources_for_node(conn, A)) == {S1, S2, S3}    # S3 restored to A
    conn.close()


# --- real Source pages + validate_projection (Source-page fan-out exercised for real) --------------


def _real_source(tmp_path, sid):
    """A real, renderable source: valid manifest + normalized markdown so `generate_wiki` can render (and
    re-render) its Source page and project its graph mentions — the input the caller's fan-out drives."""
    md = tmp_path / "raw" / "manifests"
    md.mkdir(parents=True, exist_ok=True)
    rel = f"raw/inbox/{sid}.md"
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text("raw bytes", encoding="utf-8")
    (md / f"{sid}.json").write_text(json.dumps({
        "source_id": sid, "sha256": sid + "0" * 8, "relative_raw_path": rel, "file_extension": ".md",
        "chunk_count": 1, "ingestion_status": "extracted",
        "normalized": {"markdown_path": f"normalized/markdown/{sid}.md"},
        "created_at": _OLD, "discovered_at": _OLD, "modified_at": _OLD,
        "retention_class": "permanent", "occurrences": [{"relative_path": rel}]}), encoding="utf-8")
    norm = tmp_path / "normalized" / "markdown" / f"{sid}.md"
    norm.parent.mkdir(parents=True, exist_ok=True)
    norm.write_text(f"# {sid}\n\nReal prose body.\n", encoding="utf-8")


def test_e2e_source_fanout_and_validate_projection(tmp_path):
    # The Source-page fan-out + validate_projection exercised together over real Source pages: after the split
    # moves S3's mention A->B, the caller re-renders S3's Source page, which must drop A and link the spin-off B,
    # and the full graph<->wiki projection must validate clean.
    import validate_projection
    conn = _graph(tmp_path)
    _full(tmp_path, conn)                                          # entity A (active) + mentions S1,S2,S3 -> A
    shutil.copytree(ROOT / "templates", tmp_path / "templates", dirs_exist_ok=True)
    for s in (S1, S2, S3):
        _real_source(tmp_path, s)
    wiki.generate_wiki(tmp_path, source_ids=[S1, S2, S3], rebuild_index=False, record_job=False)
    assert "[[Entities/washington|" in (tmp_path / "wiki" / "Sources" / f"{S3}.md").read_text()  # initially -> A

    _approve(tmp_path, sources=[S3])
    _apply(tmp_path, conn)                                         # split: S3 -> B; A & B entity pages re-rendered
    conn.commit()
    wiki.generate_wiki(tmp_path, source_ids=[S3], rebuild_index=False, record_job=False)   # caller's fan-out
    conn.close()

    s3_page = (tmp_path / "wiki" / "Sources" / f"{S3}.md").read_text()
    assert "[[Entities/george-washington|" in s3_page             # moved Source page now links the spin-off B
    assert "[[Entities/washington|" not in s3_page                # ...and dropped the primary A
    assert validate_projection.main([str(tmp_path)]) == 0         # graph<->wiki projection consistent


# --- end-to-end via /reviews/apply -----------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


def test_api_apply_split_summary(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module.retention, "reindex_keyword", lambda root: None)
    monkeypatch.setattr(main_module, "_run_all_validators", lambda root: [])   # source pages out of scope here
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    conn.close()
    _approve(tmp_path, sources=[S3])
    body = client.post("/reviews/apply").json()
    assert body["status"] == "applied" and body["summary"]["split"]["applied"] == 1
    conn2 = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert conn2.execute("SELECT status FROM nodes WHERE node_id=?", (B,)).fetchone()["status"] in (
        "candidate", "active")


def test_api_split_graph_required_503(client, tmp_path):
    _approve(tmp_path, sources=[S3])
    assert not (tmp_path / "db" / "graph.sqlite").exists()
    assert client.post("/reviews/apply").status_code == 503


def test_api_split_reindex_failure_is_non_clean(client, tmp_path, monkeypatch):
    def boom(root):
        raise RuntimeError("reindex blew up")
    monkeypatch.setattr(main_module.retention, "reindex_keyword", boom)
    monkeypatch.setattr(main_module, "_run_all_validators", lambda root: [])
    conn = _graph(tmp_path)
    _full(tmp_path, conn)
    conn.close()
    _approve(tmp_path, sources=[S3])
    body = client.post("/reviews/apply").json()
    assert body["status"] == "validation_failed"
    assert "split_discovery_reindex_not_guaranteed" in body["warnings"]
