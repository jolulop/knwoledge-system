"""Phase 6 slice 6-3: the scoped deprecation apply executor (ADR-0035 A5).

Key-free tests over app.workers.deprecations.apply_approved_deprecations — in-scope claim + concept
apply, idempotent no-op vs normalization apply, legacy items missing context.node_type absorbed,
node_type_mismatch + out-of-scope typed skips. No LLM.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph
from app.workers import deprecations
from app.workers.wiki_render import parse_frontmatter, render_claim_page, render_item_page


def _graph(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    return graph.connect(gdb)


def _write_item(tmp_path, conn, *, node_id="itm_x", slug="thing", node_status="active",
                review_status="none"):
    page = tmp_path / "wiki" / "Items" / f"{slug}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(render_item_page({
        "node_id": node_id, "item_type": "method_technique", "title": "Thing",
        "aliases": ["TH"], "confidence": "low", "source_ids": [], "status": node_status,
    }, review_status=review_status), encoding="utf-8")
    graph.upsert_node(conn, node_id=node_id, node_type="item", slug=slug, status=node_status,
                      item_type="method_technique")
    return page


def _write_claim(tmp_path, conn, *, cid="clm_x", node_status="active", review_status="pending"):
    page = tmp_path / "wiki" / "Claims" / f"{cid}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    # no-evidence claim -> renders deprecated_candidate; override the review_status for the fixture
    page.write_text(render_claim_page(
        {"claim_id": cid, "claim_text": "A claim.", "confidence": "low",
         "citations": [], "contradicts": [], "deprecated": False}, review_status=review_status),
        encoding="utf-8")
    graph.upsert_node(conn, node_id=cid, node_type="claim", slug=cid, status=node_status)
    return page


def _approve(tmp_path, *, node_id, page, node_type=None, rid="rev_d", to_status="deprecated_candidate"):
    item = {"review_id": rid, "type": "deprecate_wiki_page", "status": "approved",
            "subject": {"node_id": node_id, "page": page},
            "proposal": {"to_status": to_status, "reason": "x"},
            "context": {"node_type": node_type} if node_type else {}}
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps(item), encoding="utf-8")


def _apply(tmp_path, conn):
    return deprecations.apply_approved_deprecations(
        conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki",
        claims_dir=tmp_path / "wiki" / "Claims", markdown_dir=tmp_path / "normalized" / "markdown")


def test_apply_in_scope_concept(tmp_path):
    conn = _graph(tmp_path)
    page = _write_item(tmp_path, conn)
    _approve(tmp_path, node_id="itm_x", page="Items/thing.md", node_type="item")
    res = _apply(tmp_path, conn)
    assert res["applied"] == 1 and res["normalized"] == 0 and res["skipped"] == []
    fm = parse_frontmatter(page.read_text(encoding="utf-8"))
    assert fm["status"] == "deprecated_candidate" and fm["review_status"] == "approved"
    assert graph.get_node(conn, "itm_x")["status"] == "deprecated_candidate"


def test_apply_in_scope_claim(tmp_path):
    conn = _graph(tmp_path)
    page = _write_claim(tmp_path, conn)  # node active, page tombstone/pending
    _approve(tmp_path, node_id="clm_x", page="Claims/clm_x.md", node_type="claim")
    res = _apply(tmp_path, conn)
    assert res["applied"] == 1
    fm = parse_frontmatter(page.read_text(encoding="utf-8"))
    assert fm["status"] == "deprecated_candidate" and fm["review_status"] == "approved"
    assert graph.get_node(conn, "clm_x")["status"] == "deprecated_candidate"


def test_idempotent_true_no_op(tmp_path):
    conn = _graph(tmp_path)
    _write_item(tmp_path, conn)
    _approve(tmp_path, node_id="itm_x", page="Items/thing.md", node_type="item")
    _apply(tmp_path, conn)            # first apply
    res = _apply(tmp_path, conn)      # second apply -> fully effected, silent no-op
    assert res["applied"] == 0 and res["normalized"] == 0 and res["changed_pages"] == []


def test_normalization_apply_when_only_review_status_off(tmp_path):
    # page + graph already deprecated_candidate, but review_status is wrong -> normalization apply
    conn = _graph(tmp_path)
    _write_item(tmp_path, conn, node_status="deprecated_candidate", review_status="pending")
    _approve(tmp_path, node_id="itm_x", page="Items/thing.md", node_type="item")
    res = _apply(tmp_path, conn)
    assert res["normalized"] == 1 and res["applied"] == 0
    fm = parse_frontmatter((tmp_path / "wiki" / "Items" / "thing.md").read_text(encoding="utf-8"))
    assert fm["review_status"] == "approved"


def test_graph_only_mirror_update_succeeds_without_page_change(tmp_path):
    # Regression (ADR-0041 shared recompose seam): page is already canonical (deprecated_candidate +
    # approved) but the graph node-status mirror is stale (still active). recompose returns "unchanged"
    # (no page write) yet DOES flip the graph -> must succeed, update the graph, report no skip, and add
    # no changed_pages.
    conn = _graph(tmp_path)
    _write_item(tmp_path, conn, node_status="deprecated_candidate", review_status="approved")
    graph.upsert_node(conn, node_id="itm_x", node_type="item", slug="thing", status="active")  # stale
    _approve(tmp_path, node_id="itm_x", page="Items/thing.md", node_type="item")
    res = _apply(tmp_path, conn)
    assert res["skipped"] == []                  # not recorded as a skip
    assert res["changed_pages"] == []            # page already canonical -> no page write
    assert res["applied"] == 1 and res["graph_changed"] is True
    assert graph.get_node(conn, "itm_x")["status"] == "deprecated_candidate"  # mirror updated


def test_legacy_missing_context_node_type_absorbed_not_skipped(tmp_path):
    # an already-effected claim deprecation filed WITHOUT context.node_type (legacy supersede) is
    # absorbed as a no-op via page-dir inference, never skipped with a missing-context reason
    conn = _graph(tmp_path)
    _write_claim(tmp_path, conn, node_status="deprecated_candidate", review_status="approved")
    _approve(tmp_path, node_id="clm_x", page="Claims/clm_x.md", node_type=None)  # no context.node_type
    res = _apply(tmp_path, conn)
    assert res["skipped"] == [] and res["applied"] == 0 and res["normalized"] == 0
    assert graph.get_node(conn, "clm_x")["status"] == "deprecated_candidate"


def test_page_node_mismatch_wrong_dir_is_skipped(tmp_path):
    conn = _graph(tmp_path)
    # page lives under Claims/ but the graph node is an item -> canonical is Items/clm_x.md
    graph.upsert_node(conn, node_id="clm_x", node_type="item", slug="clm_x", status="active", item_type="model")
    _approve(tmp_path, node_id="clm_x", page="Claims/clm_x.md", node_type="claim")
    res = _apply(tmp_path, conn)
    assert res["applied"] == 0
    assert res["skipped"] == [{"review_id": "rev_d", "reason": "page_node_mismatch"}]


def test_page_node_mismatch_wrong_slug_is_skipped(tmp_path):
    # well-formed in-scope path, but it is not the node's canonical page (different slug)
    conn = _graph(tmp_path)
    _write_item(tmp_path, conn)  # node itm_x, slug "thing" -> canonical Items/thing.md
    _approve(tmp_path, node_id="itm_x", page="Items/other.md", node_type="item")
    res = _apply(tmp_path, conn)
    assert res["applied"] == 0
    assert res["skipped"] == [{"review_id": "rev_d", "reason": "page_node_mismatch"}]


def test_trailing_newline_page_is_invalid_page_path(tmp_path):
    # `_WIKI_PAGE_RE` now uses fullmatch (not `^…$`+match), so a trailing newline in subject.page is
    # rejected as invalid_page_path, not silently accepted.
    conn = _graph(tmp_path)
    _write_item(tmp_path, conn)
    _approve(tmp_path, node_id="itm_x", page="Items/thing.md\n", node_type="item")
    res = _apply(tmp_path, conn)
    assert res["skipped"] == [{"review_id": "rev_d", "reason": "invalid_page_path"}]
    assert graph.get_node(conn, "itm_x")["status"] == "active"  # untouched


def test_malicious_traversal_page_is_invalid_and_never_read(tmp_path):
    # a path-traversal subject.page must be rejected before any read (CLAUDE.md rule 1, ADR-0035 A5)
    conn = _graph(tmp_path)
    _write_item(tmp_path, conn)
    # plant a file outside wiki/ that traversal would reach; the executor must never touch it
    secret = tmp_path / "raw" / "permanent" / "x.md"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("---\nstatus: active\n---\n", encoding="utf-8")
    _approve(tmp_path, node_id="itm_x", page="Claims/../../raw/permanent/x.md", node_type="item")
    res = _apply(tmp_path, conn)
    assert res["applied"] == 0 and res["changed_pages"] == []
    assert res["skipped"] == [{"review_id": "rev_d", "reason": "invalid_page_path"}]
    assert graph.get_node(conn, "itm_x")["status"] == "active"  # untouched


def test_item_types_share_the_scope_seam(tmp_path):
    # Every item_type flows through the same renderer/NODE_DIR seam (one flat Items/ dir).
    for item_type, slug in (("provider_institution", "acme"), ("model", "alice-model"),
                            ("product_tool_platform", "globex"), ("use_case", "apollo")):
        conn = _graph(tmp_path / item_type)
        nid = f"itm_{item_type[:12]:x<16}"[:20] if False else f"itm_{abs(hash(item_type)) % 10**16:016d}"
        page = tmp_path / item_type / "wiki" / "Items" / f"{slug}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(render_item_page({
            "node_id": nid, "item_type": item_type, "title": "X",
            "aliases": [], "confidence": "low", "source_ids": [], "status": "active"}),
            encoding="utf-8")
        graph.upsert_node(conn, node_id=nid, node_type="item", slug=slug, status="active",
                          item_type=item_type)
        _approve(tmp_path / item_type, node_id=nid, page=f"Items/{slug}.md", node_type="item")
        res = deprecations.apply_approved_deprecations(
            conn, tmp_path / item_type / "reviews", wiki_dir=tmp_path / item_type / "wiki",
            claims_dir=tmp_path / item_type / "wiki" / "Claims",
            markdown_dir=tmp_path / item_type / "normalized" / "markdown")
        assert res["applied"] == 1, item_type
        assert graph.get_node(conn, nid)["status"] == "deprecated_candidate"


def test_synthesis_page_skipped_handled_elsewhere(tmp_path):
    conn = _graph(tmp_path)
    _approve(tmp_path, node_id="syn_x", page="Synthesis/syn_x.md")
    res = _apply(tmp_path, conn)
    assert res["skipped"] == [{"review_id": "rev_d", "reason": "handled_by_synthesis_executor"}]


def test_out_of_scope_source_page_skipped(tmp_path):
    conn = _graph(tmp_path)
    _approve(tmp_path, node_id="src_x", page="Sources/src_x.md")
    res = _apply(tmp_path, conn)
    assert res["skipped"] == [{"review_id": "rev_d", "reason": "out_of_scope"}]


def test_node_missing_skipped(tmp_path):
    conn = _graph(tmp_path)
    _approve(tmp_path, node_id="itm_gone", page="Items/gone.md", node_type="item")
    res = _apply(tmp_path, conn)
    assert res["skipped"] == [{"review_id": "rev_d", "reason": "node_missing"}]
