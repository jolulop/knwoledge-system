"""ADR-0041: mark_semantic_duplicate executor + projection + validators.

The first non-rekeying governance executor: an approved decision upserts ONE active canonical
symmetric `duplicates(min,max)` edge and re-renders a body-only `## Duplicates` section on both
same-type pages, with no id/status/retrieval change. Covers the executor + scope guards, idempotency,
the A1 preview projector, the two validator extensions, and end-to-end apply via the API.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.backend import graph, review_read
from app.backend import main as main_module
from app.backend.config import get_settings
from app.workers import concepts, duplicates
from app.workers.wiki_render import parse_frontmatter, render_concept_page


# --- helpers ---------------------------------------------------------------


def _seed_node(tmp_path: Path, gconn, *, node_id: str, slug: str, node_type: str = "entity",
               title: str | None = None, status: str = "active") -> Path:
    """A graph node + its rendered page (so the executor can re-render it)."""
    graph.upsert_node(gconn, node_id=node_id, node_type=node_type, slug=slug, status=status)
    page = tmp_path / "wiki" / {"entity": "Entities", "concept": "Concepts"}[node_type] / f"{slug}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(render_concept_page({
        "node_type": node_type, "node_id": node_id, "id_field": f"{node_type}_id",
        "title": title or slug, "aliases": [], "confidence": "low",
        "source_ids": [], "status": status,
    }), encoding="utf-8")
    return page


def _approve(tmp_path: Path, item: dict) -> None:
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{item['review_id']}.json").write_text(json.dumps(item), encoding="utf-8")


def _dup_item(a: str, b: str, rid: str = "rev_dup") -> dict:
    return {"review_id": rid, "type": "mark_semantic_duplicate", "status": "approved",
            "subject": {"node_ids": sorted([a, b])}, "proposal": {}, "context": {}}


def _edges(gconn, edge_type: str = "duplicates") -> list[dict]:
    return [dict(r) for r in gconn.execute(
        "SELECT src_id, dst_id, status, asserted_by, review_id FROM edges WHERE edge_type = ?",
        (edge_type,))]


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


# --- executor: happy path + idempotency ------------------------------------


def test_approve_creates_canonical_active_edge_and_projects_both_pages(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    gconn = graph.connect(gdb)
    # ids chosen so ent_a < ent_b lexically
    pa = _seed_node(tmp_path, gconn, node_id="ent_aaaaaaaaaaaaaaaa", slug="alpha", title="Alpha")
    pb = _seed_node(tmp_path, gconn, node_id="ent_bbbbbbbbbbbbbbbb", slug="bravo", title="Bravo")
    gconn.commit()
    _approve(tmp_path, _dup_item("ent_aaaaaaaaaaaaaaaa", "ent_bbbbbbbbbbbbbbbb"))

    res = duplicates.apply_marked_duplicates(gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")
    gconn.commit()
    assert res["applied"] == 1 and res["skipped"] == []
    edges = _edges(gconn)
    assert len(edges) == 1
    e = edges[0]
    assert (e["src_id"], e["dst_id"]) == ("ent_aaaaaaaaaaaaaaaa", "ent_bbbbbbbbbbbbbbbb")  # canonical
    assert e["status"] == "active" and e["asserted_by"] == "human" and e["review_id"] == "rev_dup"
    # both pages gained a body-only ## Duplicates section linking the other
    assert "## Duplicates" in pa.read_text() and "[[Entities/bravo]]" in pa.read_text()
    assert "## Duplicates" in pb.read_text() and "[[Entities/alpha]]" in pb.read_text()
    # no frontmatter key, status/metadata preserved
    assert "duplicates:" not in pa.read_text().split("\n---\n", 1)[0]
    assert parse_frontmatter(pa.read_text())["status"] == "active"


def test_steady_state_reapply_is_a_true_noop(tmp_path):
    # Approved files persist in approved/, so apply re-runs every time. A steady-state re-apply (edge
    # active + both pages already project) must be a TRUE no-op: no edge/page write, no changed_pages,
    # so it never forces a rebuild/reindex (ADR-0041).
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    gconn = graph.connect(gdb)
    pa = _seed_node(tmp_path, gconn, node_id="ent_aaaaaaaaaaaaaaaa", slug="alpha")
    _seed_node(tmp_path, gconn, node_id="ent_bbbbbbbbbbbbbbbb", slug="bravo")
    gconn.commit()
    _approve(tmp_path, _dup_item("ent_aaaaaaaaaaaaaaaa", "ent_bbbbbbbbbbbbbbbb"))
    wiki = tmp_path / "wiki"
    r1 = duplicates.apply_marked_duplicates(gconn, tmp_path / "reviews", wiki_dir=wiki)
    gconn.commit()
    assert r1["applied"] == 1 and r1["changed_pages"]
    after_first = pa.read_text()
    r2 = duplicates.apply_marked_duplicates(gconn, tmp_path / "reviews", wiki_dir=wiki)
    gconn.commit()
    assert r2 == {"applied": 0, "normalized": 0, "skipped": [], "changed_pages": [],
                  "graph_changed": False}
    assert len(_edges(gconn)) == 1            # no duplicate row
    assert pa.read_text() == after_first      # page byte-stable


def test_stale_projection_is_normalized_not_applied(tmp_path):
    # Edge already active but a page lost its ## Duplicates section -> `normalized` repairs the page
    # (re-render), no new graph edge, distinct from a first `applied`.
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    gconn = graph.connect(gdb)
    pa = _seed_node(tmp_path, gconn, node_id="ent_aaaaaaaaaaaaaaaa", slug="alpha")
    _seed_node(tmp_path, gconn, node_id="ent_bbbbbbbbbbbbbbbb", slug="bravo")
    graph.upsert_assertion(gconn, src_id="ent_aaaaaaaaaaaaaaaa", dst_id="ent_bbbbbbbbbbbbbbbb",
                           edge_type="duplicates", asserted_by="human", status="active")
    gconn.commit()
    # page A has no ## Duplicates section yet (edge predates projection)
    assert "## Duplicates" not in pa.read_text()
    _approve(tmp_path, _dup_item("ent_aaaaaaaaaaaaaaaa", "ent_bbbbbbbbbbbbbbbb"))
    res = duplicates.apply_marked_duplicates(gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")
    gconn.commit()
    assert res["applied"] == 0 and res["normalized"] == 1 and res["graph_changed"] is False
    assert "## Duplicates" in pa.read_text() and "[[Entities/bravo]]" in pa.read_text()
    assert len(_edges(gconn)) == 1            # still one edge, no new write


def test_partner_link_outside_section_does_not_count_as_projected(tmp_path):
    # An active edge + a partner wikilink OUTSIDE ## Duplicates (e.g. in Notes) but no ## Duplicates
    # section must NORMALIZE (re-render the real section), not be misread as a true no-op (blocking #1).
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    gconn = graph.connect(gdb)
    pa = _seed_node(tmp_path, gconn, node_id="ent_aaaaaaaaaaaaaaaa", slug="alpha")
    _seed_node(tmp_path, gconn, node_id="ent_bbbbbbbbbbbbbbbb", slug="bravo")
    graph.upsert_assertion(gconn, src_id="ent_aaaaaaaaaaaaaaaa", dst_id="ent_bbbbbbbbbbbbbbbb",
                           edge_type="duplicates", asserted_by="human", status="active")
    gconn.commit()
    # decoy: partner link in Notes, but NO ## Duplicates section
    pa.write_text(pa.read_text().replace("## Notes\n", "## Notes\n\n- aside [[Entities/bravo]]\n"),
                  encoding="utf-8")
    assert "## Duplicates" not in pa.read_text()
    _approve(tmp_path, _dup_item("ent_aaaaaaaaaaaaaaaa", "ent_bbbbbbbbbbbbbbbb"))
    res = duplicates.apply_marked_duplicates(gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")
    gconn.commit()
    assert res["normalized"] == 1            # normalized, NOT a no-op
    assert "## Duplicates" in pa.read_text()  # real section now present


def test_recompose_returns_unchanged_when_no_write(tmp_path):
    # The shared seam returns "unchanged" (not "written") when the render is byte-identical.
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    gconn = graph.connect(gdb)
    page = _seed_node(tmp_path, gconn, node_id="ent_aaaaaaaaaaaaaaaa", slug="alpha")
    gconn.commit()
    # first recompose normalizes to canonical form
    concepts.recompose_semantic_node_page(gconn, node_id="ent_aaaaaaaaaaaaaaaa",
                                          wiki_dir=tmp_path / "wiki", status="active", review_status="none")
    before = page.read_text()
    res = concepts.recompose_semantic_node_page(gconn, node_id="ent_aaaaaaaaaaaaaaaa",
                                                wiki_dir=tmp_path / "wiki", status="active",
                                                review_status="none")
    assert res == "unchanged" and page.read_text() == before


def test_unsupported_same_type_skips_before_edge(tmp_path):
    # Two same-type but NON-semantic nodes (source) have no ## Duplicates page projection -> skip with
    # unsupported_node_type BEFORE any edge is written (no orphan edge).
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    gconn = graph.connect(gdb)
    graph.upsert_node(gconn, node_id="src_aaaaaaaaaaaaaaaa", node_type="source",
                      slug="src_aaaaaaaaaaaaaaaa", status="active")
    graph.upsert_node(gconn, node_id="src_bbbbbbbbbbbbbbbb", node_type="source",
                      slug="src_bbbbbbbbbbbbbbbb", status="active")
    gconn.commit()
    _approve(tmp_path, _dup_item("src_aaaaaaaaaaaaaaaa", "src_bbbbbbbbbbbbbbbb"))
    res = duplicates.apply_marked_duplicates(gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")
    gconn.commit()
    assert res["applied"] == 0
    assert res["skipped"] == [{"review_id": "rev_dup", "reason": "unsupported_node_type"}]
    assert _edges(gconn) == []                # no orphan edge


def test_confidence_preserved_across_duplicate_render(tmp_path):
    # Page-owned confidence (e.g. medium) must survive a duplicate-mark re-render (ADR-0041).
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    gconn = graph.connect(gdb)
    pa = _seed_node(tmp_path, gconn, node_id="ent_aaaaaaaaaaaaaaaa", slug="alpha")
    _seed_node(tmp_path, gconn, node_id="ent_bbbbbbbbbbbbbbbb", slug="bravo")
    # bump page A's confidence to medium (page is the authority)
    pa.write_text(pa.read_text().replace("confidence: low", "confidence: medium"), encoding="utf-8")
    gconn.commit()
    _approve(tmp_path, _dup_item("ent_aaaaaaaaaaaaaaaa", "ent_bbbbbbbbbbbbbbbb"))
    duplicates.apply_marked_duplicates(gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")
    gconn.commit()
    assert parse_frontmatter(pa.read_text())["confidence"] == "medium"   # not reset to low


# --- scope guards ----------------------------------------------------------


@pytest.mark.parametrize("subject,reason", [
    ({"node_ids": ["ent_aaaaaaaaaaaaaaaa"]}, "malformed_subject"),         # wrong length
    ({"node_ids": "ent_aaaaaaaaaaaaaaaa"}, "malformed_subject"),           # not a list
    ({}, "malformed_subject"),                                             # missing
    ({"node_ids": ["ent_aaaaaaaaaaaaaaaa", "ent_aaaaaaaaaaaaaaaa"]}, "self_duplicate"),
    ({"node_ids": ["ent_aaaaaaaaaaaaaaaa", "../escape"]}, "invalid_node_id"),
])
def test_scope_guard_skips(tmp_path, subject, reason):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    gconn = graph.connect(gdb)
    _seed_node(tmp_path, gconn, node_id="ent_aaaaaaaaaaaaaaaa", slug="alpha")
    gconn.commit()
    _approve(tmp_path, {"review_id": "rev_x", "type": "mark_semantic_duplicate",
                        "status": "approved", "subject": subject, "proposal": {}, "context": {}})
    res = duplicates.apply_marked_duplicates(gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")
    gconn.commit()
    assert res["applied"] == 0
    assert res["skipped"] == [{"review_id": "rev_x", "reason": reason}]
    assert _edges(gconn) == []                # never partial-applies


def test_node_missing_and_type_mismatch_skips(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    gconn = graph.connect(gdb)
    _seed_node(tmp_path, gconn, node_id="ent_aaaaaaaaaaaaaaaa", slug="alpha", node_type="entity")
    _seed_node(tmp_path, gconn, node_id="cpt_cccccccccccccccc", slug="gamma", node_type="concept")
    gconn.commit()
    # node_missing: ent_b not in graph
    _approve(tmp_path, _dup_item("ent_aaaaaaaaaaaaaaaa", "ent_dddddddddddddddd", rid="rev_nm"))
    # type_mismatch: entity vs concept
    _approve(tmp_path, _dup_item("ent_aaaaaaaaaaaaaaaa", "cpt_cccccccccccccccc", rid="rev_tm"))
    res = duplicates.apply_marked_duplicates(gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")
    gconn.commit()
    reasons = {s["review_id"]: s["reason"] for s in res["skipped"]}
    assert reasons == {"rev_nm": "node_missing", "rev_tm": "type_mismatch"}
    assert _edges(gconn) == []


# --- A1 preview projector --------------------------------------------------


def test_preview_projector_resolves_paths_and_supported(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    gconn = graph.connect(gdb)
    _seed_node(tmp_path, gconn, node_id="ent_aaaaaaaaaaaaaaaa", slug="alpha")
    _seed_node(tmp_path, gconn, node_id="ent_bbbbbbbbbbbbbbbb", slug="bravo")
    gconn.commit()
    item = _dup_item("ent_aaaaaaaaaaaaaaaa", "ent_bbbbbbbbbbbbbbbb")
    out = review_read.preview_mark_semantic_duplicate(item, gconn=gconn, wiki_dir=tmp_path / "wiki")
    assert out["apply"]["supported"] is True
    assert out["node_ids"] == ["ent_aaaaaaaaaaaaaaaa", "ent_bbbbbbbbbbbbbbbb"]
    assert set(out["affected_paths"]) == {"Entities/alpha.md", "Entities/bravo.md"}
    assert out["warnings"] == []


def test_preview_projector_warns_already_duplicated_and_type_mismatch(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    gconn = graph.connect(gdb)
    _seed_node(tmp_path, gconn, node_id="ent_aaaaaaaaaaaaaaaa", slug="alpha")
    _seed_node(tmp_path, gconn, node_id="ent_bbbbbbbbbbbbbbbb", slug="bravo")
    _seed_node(tmp_path, gconn, node_id="cpt_cccccccccccccccc", slug="gamma", node_type="concept")
    graph.upsert_assertion(gconn, src_id="ent_aaaaaaaaaaaaaaaa", dst_id="ent_bbbbbbbbbbbbbbbb",
                           edge_type="duplicates", asserted_by="human", status="active")
    gconn.commit()
    dup = review_read.preview_mark_semantic_duplicate(
        _dup_item("ent_aaaaaaaaaaaaaaaa", "ent_bbbbbbbbbbbbbbbb"), gconn=gconn, wiki_dir=tmp_path / "wiki")
    assert "already_duplicated" in dup["warnings"] and dup["apply"]["effect_status"] == "effected"
    tm = review_read.preview_mark_semantic_duplicate(
        _dup_item("cpt_cccccccccccccccc", "ent_aaaaaaaaaaaaaaaa"), gconn=gconn, wiki_dir=tmp_path / "wiki")
    assert "type_mismatch" in tm["warnings"]


def test_preview_projector_invalid_and_malformed(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    gconn = graph.connect(gdb)
    # unsafe (path-like) id -> invalid_node_id, NOT node_missing
    inv = review_read.preview_mark_semantic_duplicate(
        {"review_id": "r", "type": "mark_semantic_duplicate", "status": "approved",
         "subject": {"node_ids": ["ent_aaaaaaaaaaaaaaaa", "../escape"]}},
        gconn=gconn, wiki_dir=tmp_path / "wiki")
    assert "invalid_node_id" in inv["warnings"] and "node_missing" not in inv["warnings"]
    # a string (not a list) must NOT render as a per-character node_ids list
    mal = review_read.preview_mark_semantic_duplicate(
        {"review_id": "r", "type": "mark_semantic_duplicate", "status": "approved",
         "subject": {"node_ids": "ent_aaaaaaaaaaaaaaaa"}},
        gconn=gconn, wiki_dir=tmp_path / "wiki")
    assert "malformed_subject" in mal["warnings"] and mal["node_ids"] == []


# --- validators ------------------------------------------------------------


def _run_validator(script: str, root: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "scripts" / script), str(root)],
                          capture_output=True, text=True)


def test_validate_graph_rejects_reversed_duplicates(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    gconn = graph.connect(gdb)
    graph.upsert_node(gconn, node_id="ent_aaaaaaaaaaaaaaaa", node_type="entity", slug="a", status="active")
    graph.upsert_node(gconn, node_id="ent_bbbbbbbbbbbbbbbb", node_type="entity", slug="b", status="active")
    # reversed (src > dst) inserted via raw SQL, bypassing the canonicalizing write API
    gconn.execute("INSERT INTO edges (edge_id, src_id, dst_id, edge_type, status, asserted_by, "
                  "created_at) VALUES ('e1','ent_bbbbbbbbbbbbbbbb','ent_aaaaaaaaaaaaaaaa',"
                  "'duplicates','active','human','2026-01-01T00:00:00+00:00')")
    gconn.commit()
    gconn.close()
    proc = _run_validator("validate_graph.py", tmp_path)
    assert proc.returncode == 1 and "duplicates must be canonically ordered" in proc.stdout


def test_validate_projection_both_directions(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    gconn = graph.connect(gdb)
    pa = _seed_node(tmp_path, gconn, node_id="ent_aaaaaaaaaaaaaaaa", slug="alpha")
    _seed_node(tmp_path, gconn, node_id="ent_bbbbbbbbbbbbbbbb", slug="bravo")
    graph.upsert_assertion(gconn, src_id="ent_aaaaaaaaaaaaaaaa", dst_id="ent_bbbbbbbbbbbbbbbb",
                           edge_type="duplicates", asserted_by="human", status="active")
    gconn.commit()
    gconn.close()
    # active edge but NO ## Duplicates section on either page -> missing-link error both ways
    assert _run_validator("validate_projection.py", tmp_path).returncode == 1

    # now project correctly on both pages -> passes
    gconn = graph.connect(gdb)
    for nid in ("ent_aaaaaaaaaaaaaaaa", "ent_bbbbbbbbbbbbbbbb"):
        concepts.recompose_semantic_node_page(gconn, node_id=nid, wiki_dir=tmp_path / "wiki",
                                               status="active", review_status="none")
    gconn.commit()
    gconn.close()
    assert _run_validator("validate_projection.py", tmp_path).returncode == 0

    # tamper the existing section to link a node with no active edge -> invented-link error (and the
    # real active partner now missing) -> both directions fire
    pa.write_text(pa.read_text().replace("[[Entities/bravo]]", "[[Entities/ghost]]", 1),
                  encoding="utf-8")
    assert _run_validator("validate_projection.py", tmp_path).returncode == 1


# --- end-to-end via the API (also exercises the ADR-0040 dry-run) ----------


def test_apply_and_dry_run_end_to_end(client, tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    gconn = graph.connect(gdb)
    _seed_node(tmp_path, gconn, node_id="ent_aaaaaaaaaaaaaaaa", slug="alpha")
    _seed_node(tmp_path, gconn, node_id="ent_bbbbbbbbbbbbbbbb", slug="bravo")
    gconn.commit()
    gconn.close()
    _approve(tmp_path, _dup_item("ent_aaaaaaaaaaaaaaaa", "ent_bbbbbbbbbbbbbbbb"))

    # dry-run previews the edge add + two page diffs, live unchanged
    dry = client.post("/reviews/apply/dry-run").json()
    assert dry["status"] == "ok"
    assert any(e["rel"] == "duplicates" for e in dry["diff"]["graph"]["edges_added"])
    assert len(dry["diff"]["wiki"]) == 2
    assert "## Duplicates" not in (tmp_path / "wiki/Entities/alpha.md").read_text()  # not yet applied

    # real apply lands it
    applied = client.post("/reviews/apply").json()
    assert applied["summary"]["duplicates"]["applied"] == 1
    assert "## Duplicates" in (tmp_path / "wiki/Entities/alpha.md").read_text()
    # the HTML apply result surfaces the duplicates summary (review_html)
    assert "duplicates" in client.post("/ui/reviews/apply").text
