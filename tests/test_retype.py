"""ADR-0059 governed classification flip: change_item_type (apply_retypes), NON-rekeying.

A retype is a metadata flip — page frontmatter `item_type` + graph nodes mirror + audit — with
no id change, no page move, no edge re-point, no tombstone (the ADR-0041 axis: nothing an id
means changes). The sentinel is never a valid target; competing pending retypes of the same
node are withdrawn when one applies.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph, review_read, taxonomy
from app.workers import items, retypes
from app.workers.wiki_render import NODE_DIR, parse_frontmatter, render_item_page

ITEM = items.node_id("Kubernetes")
SID = "src_0123456789abcdef"
SID2 = "src_fedcba9876543210"


def _graph(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    return graph.connect(gdb)


def _node(tmp_path, conn, nid, title, item_type, *, status="candidate", aliases=(),
          description=None, split_from=None, split_review_id=None):
    slug = items._slug(title)
    graph.upsert_node(conn, node_id=nid, node_type="item", slug=slug, status=status,
                      item_type=item_type)
    page = tmp_path / "wiki" / NODE_DIR["item"] / f"{slug}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(render_item_page({
        "node_id": nid, "item_type": item_type, "title": title,
        "aliases": list(aliases), "confidence": "low",
        "source_ids": graph.sources_for_node(conn, nid), "status": status,
        "duplicates": graph.active_duplicates(conn, nid),
        "description": description, "split_from": split_from,
        "split_review_id": split_review_id,
    }), encoding="utf-8")
    conn.commit()
    return slug


def _mention(conn, sid, nid, *, span=(0, 4)):
    graph.upsert_node(conn, node_id=sid, node_type="source", slug=sid, status="active")
    eid = graph.upsert_assertion(
        conn, src_id=sid, dst_id=nid, edge_type="mentions", asserted_by="llm", status="active",
        evidence_source_id=sid, evidence_char_start=span[0], evidence_char_end=span[1])
    conn.commit()
    return eid


def _approve(tmp_path, *, node_id=ITEM, to_type="infrastructure_hardware", rid="rev_rt",
             proposal_to=None):
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": "change_item_type", "status": "approved",
        "subject": {"node_id": node_id, "to_item_type": to_type},
        "proposal": {"to_item_type": proposal_to if proposal_to is not None else to_type}}),
        encoding="utf-8")
    return rid


def _pending(tmp_path, rid, rtype, subject, proposal=None):
    d = tmp_path / "reviews" / "pending"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": rtype, "status": "pending",
        "subject": subject, "proposal": proposal or {}}), encoding="utf-8")


def _apply(tmp_path, conn):
    return retypes.apply_retypes(conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")


def _fm(tmp_path, slug):
    page = tmp_path / "wiki" / NODE_DIR["item"] / f"{slug}.md"
    return parse_frontmatter(page.read_text())


# --- core: the metadata flip ------------------------------------------------


def test_retype_flips_page_and_mirror_only(tmp_path):
    conn = _graph(tmp_path)
    slug = _node(tmp_path, conn, ITEM, "Kubernetes", "product_tool_platform",
                 aliases=["k8s"], description="Container orchestration.")
    eid = _mention(conn, SID, ITEM)
    _approve(tmp_path)
    res = _apply(tmp_path, conn)
    assert res["applied"] == 1 and res["skipped"] == []
    # the page stays at the SAME path with the SAME id; only item_type changed
    fm = _fm(tmp_path, slug)
    assert fm["item_type"] == "infrastructure_hardware"
    assert fm["item_id"] == ITEM and fm["status"] == "candidate"
    # page-owned fields survive the flip
    assert fm["description"] == "Container orchestration."
    assert "k8s" in fm["aliases"]
    # graph mirror flipped in place — same node id, same slug, same status
    node = graph.get_node(conn, ITEM)
    assert node["item_type"] == "infrastructure_hardware"
    assert node["slug"] == slug and node["status"] == "candidate"
    # NO edge surgery: the mention edge is untouched and still active on the same id
    edges = graph.incoming_active(conn, ITEM)
    assert [e["edge_id"] for e in edges] == [eid]
    # the mentioning Source page is the caller's fan-out (its Items section groups by type)
    assert res["affected_sources"] == [SID]
    assert res["changed_pages"] == [f"Items/{slug}.md"]
    # purpose-named audit entry with from/to
    audits = list((tmp_path / "reviews" / "audit_log").glob("rev_rt-retyped-*.json"))
    assert len(audits) == 1
    record = json.loads(audits[0].read_text())
    assert record["from_item_type"] == "product_tool_platform"
    assert record["to_item_type"] == "infrastructure_hardware"


def test_retype_preserves_active_status(tmp_path):
    conn = _graph(tmp_path)
    slug = _node(tmp_path, conn, ITEM, "Kubernetes", "product_tool_platform", status="active")
    _mention(conn, SID, ITEM)
    _approve(tmp_path)
    res = _apply(tmp_path, conn)
    assert res["applied"] == 1
    assert _fm(tmp_path, slug)["status"] == "active"
    assert graph.get_node(conn, ITEM)["status"] == "active"


def test_retype_clears_the_unclassified_sentinel(tmp_path):
    conn = _graph(tmp_path)
    slug = _node(tmp_path, conn, ITEM, "Kubernetes", taxonomy.UNCLASSIFIED)
    _approve(tmp_path, to_type="infrastructure_hardware")
    res = _apply(tmp_path, conn)
    assert res["applied"] == 1
    assert _fm(tmp_path, slug)["item_type"] == "infrastructure_hardware"
    assert graph.get_node(conn, ITEM)["item_type"] == "infrastructure_hardware"


def test_retype_already_in_target_state_is_silent_noop(tmp_path):
    conn = _graph(tmp_path)
    _node(tmp_path, conn, ITEM, "Kubernetes", "infrastructure_hardware")
    _approve(tmp_path, to_type="infrastructure_hardware")
    res = _apply(tmp_path, conn)
    # idempotent: a re-apply (or a no-op proposal) mutates nothing and is not an error
    assert res["applied"] == 0 and res["skipped"] == []
    assert not list((tmp_path / "reviews" / "audit_log").glob("*-retyped-*.json")) \
        if (tmp_path / "reviews" / "audit_log").exists() else True


# --- guards (typed skips, never partial) ------------------------------------


def test_sentinel_is_never_a_valid_target(tmp_path):
    conn = _graph(tmp_path)
    _node(tmp_path, conn, ITEM, "Kubernetes", "product_tool_platform")
    _approve(tmp_path, to_type=taxonomy.UNCLASSIFIED)
    res = _apply(tmp_path, conn)
    assert res["applied"] == 0
    assert res["skipped"] == [{"review_id": "rev_rt", "reason": "invalid_to_item_type"}]
    assert _fm(tmp_path, "kubernetes")["item_type"] == "product_tool_platform"


def test_proposal_subject_mismatch_skips(tmp_path):
    conn = _graph(tmp_path)
    _node(tmp_path, conn, ITEM, "Kubernetes", "product_tool_platform")
    _approve(tmp_path, to_type="infrastructure_hardware", proposal_to="model")
    res = _apply(tmp_path, conn)
    assert res["skipped"] == [{"review_id": "rev_rt", "reason": "to_item_type_mismatch"}]


def test_noncanonical_node_id_skips_before_any_path(tmp_path):
    conn = _graph(tmp_path)
    for bad in ("ent_0123456789abcdef", "../escape", "itm_SHOUTY", "itm_short"):
        _approve(tmp_path, node_id=bad, rid=f"rev_{abs(hash(bad)) % 10**8}")
    res = _apply(tmp_path, conn)
    assert res["applied"] == 0
    assert {s["reason"] for s in res["skipped"]} == {"noncanonical_node_id"}


def test_missing_node_and_missing_page_skip(tmp_path):
    conn = _graph(tmp_path)
    _approve(tmp_path, rid="rev_a")                        # node never indexed
    res = _apply(tmp_path, conn)
    assert res["skipped"] == [{"review_id": "rev_a", "reason": "node_missing"}]
    # node indexed but page absent (wiki/graph drift)
    graph.upsert_node(conn, node_id=ITEM, node_type="item", slug="kubernetes",
                      status="candidate", item_type="product_tool_platform")
    res = _apply(tmp_path, conn)
    assert res["skipped"] == [{"review_id": "rev_a", "reason": "page_missing"}]


def test_non_item_node_is_out_of_scope(tmp_path):
    conn = _graph(tmp_path)
    fake = "itm_0123456789abcdef"                          # canonical shape, wrong structural type
    graph.upsert_node(conn, node_id=fake, node_type="claim", slug="some-claim", status="active")
    _approve(tmp_path, node_id=fake, rid="rev_c")
    res = _apply(tmp_path, conn)
    assert res["skipped"] == [{"review_id": "rev_c", "reason": "out_of_scope"}]


def test_non_live_statuses_are_not_retypable(tmp_path):
    conn = _graph(tmp_path)
    _node(tmp_path, conn, ITEM, "Kubernetes", "product_tool_platform",
          status="deprecated_candidate")
    _approve(tmp_path)
    res = _apply(tmp_path, conn)
    assert res["skipped"] == [{"review_id": "rev_rt", "reason": "node_not_retypable"}]


# --- competing-proposal withdrawal ------------------------------------------


def test_applying_one_retype_withdraws_competing_pending_retypes(tmp_path):
    conn = _graph(tmp_path)
    _node(tmp_path, conn, ITEM, "Kubernetes", "product_tool_platform")
    _approve(tmp_path, to_type="infrastructure_hardware")
    _pending(tmp_path, "rev_other", "change_item_type",
             {"node_id": ITEM, "to_item_type": "technology_capability"},
             {"to_item_type": "technology_capability"})
    _pending(tmp_path, "rev_promote", "promote_candidate_node", {"node_id": ITEM},
             {"to_status": "active"})
    res = _apply(tmp_path, conn)
    assert res["applied"] == 1
    # the competing retype is withdrawn (audited), the promote item is untouched
    assert not (tmp_path / "reviews" / "pending" / "rev_other.json").exists()
    assert (tmp_path / "reviews" / "pending" / "rev_promote.json").exists()
    withdrawn = list((tmp_path / "reviews" / "audit_log").glob("rev_other-withdrawn-*.json"))
    assert len(withdrawn) == 1
    assert json.loads(withdrawn[0].read_text())["note"] == "superseded_by_retype"


# --- effect projector (review_read) ------------------------------------------


def _project(tmp_path, conn, item):
    return review_read.project_review(item, gconn=conn, wiki_dir=tmp_path / "wiki")


def _approved_item(node_id=ITEM, to_type="infrastructure_hardware", rid="rev_rt"):
    return {"review_id": rid, "type": "change_item_type", "status": "approved",
            "subject": {"node_id": node_id, "to_item_type": to_type},
            "proposal": {"to_item_type": to_type}}


def test_effect_projector_pending_then_effected(tmp_path):
    conn = _graph(tmp_path)
    _node(tmp_path, conn, ITEM, "Kubernetes", "product_tool_platform")
    item = _approved_item()
    assert _project(tmp_path, conn, item)["apply"]["effect_status"] == "pending_apply"
    _approve(tmp_path)
    _apply(tmp_path, conn)
    assert _project(tmp_path, conn, item)["apply"]["effect_status"] == "effected"


def test_effect_projector_partial_state_is_unknown(tmp_path):
    conn = _graph(tmp_path)
    slug = _node(tmp_path, conn, ITEM, "Kubernetes", "product_tool_platform")
    # simulate a half-applied flip: page flipped, graph mirror not (crash between writes)
    page = tmp_path / "wiki" / NODE_DIR["item"] / f"{slug}.md"
    page.write_text(render_item_page({
        "node_id": ITEM, "item_type": "infrastructure_hardware", "title": "Kubernetes",
        "aliases": [], "confidence": "low", "source_ids": [], "status": "candidate",
        "duplicates": [],
    }), encoding="utf-8")
    preview = _project(tmp_path, conn, _approved_item())
    assert preview["apply"]["effect_status"] == "unknown"
    assert "partial_retype_state" in preview["apply"]["warnings"]


def test_effect_projector_rejected_owes_nothing_and_preview_shape(tmp_path):
    conn = _graph(tmp_path)
    _node(tmp_path, conn, ITEM, "Kubernetes", "product_tool_platform")
    rejected = _approved_item() | {"status": "rejected"}
    preview = _project(tmp_path, conn, rejected)
    assert preview["apply"]["effect_status"] == "no_effect_required"
    pending = _approved_item() | {"status": "pending"}
    preview = _project(tmp_path, conn, pending)
    assert preview["proposed_status"] is None              # a retype never changes lifecycle status
    assert "reclassify" in preview["proposed_action"]
    assert preview["apply"]["executor"] == "apply_retypes"
