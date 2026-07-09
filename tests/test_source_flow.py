"""ADR-0058 per-source review flow: attribution, batch decide, amendments, human-add, XSS."""
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

import validate_frontmatter  # noqa: E402
import validate_graph  # noqa: E402
import validate_projection  # noqa: E402
import validate_wikilinks  # noqa: E402

from fastapi.testclient import TestClient

from app.backend import graph, manifests, review_read
from app.backend import main as main_module
from app.backend.config import get_settings
from app.llm.cache import ResponseCache
from app.llm.client import LLMClient
from app.workers import extract, human_add, intake, items, promote, reviews, wiki
from app.workers.wiki_render import parse_frontmatter


MODEL_REF = "anthropic:claude-sonnet-4-6"
TEMPLATES = ROOT / "templates"


class RoutingAdapter:
    """Routes the payload by a marker string found in the prompt (per-source payloads)."""
    name = "anthropic"
    supports_batch = False

    def __init__(self, routes: dict[str, dict], default=None):
        self._routes = routes
        self._default = default or {"items": []}

    def available(self):
        return True

    def parse(self, messages, schema, model_id, *, max_tokens):
        text = json.dumps(messages)
        for marker, payload in self._routes.items():
            if marker in text:
                return {"items": [dict(i) for i in payload.get("items", [])]}
        return dict(self._default)


def _build(tmp_path, files: dict[str, str]):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    # The promote/human-add Source fan-outs read the root-default templates dir (real-vault layout).
    shutil.copytree(TEMPLATES, tmp_path / "templates", dirs_exist_ok=True)
    for name, body in files.items():
        (inbox / name).write_text(f"# Title\n\n{body}\n", encoding="utf-8")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    extract.extract_sources(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    wiki.generate_wiki(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite",
                       templates_dir=TEMPLATES, rebuild_index=False)


def _extract(tmp_path, adapter):
    client = LLMClient({"anthropic": adapter},
                       cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite"))
    return items.extract_items(tmp_path, client=client, model_ref=MODEL_REF,
                               jobs_db=tmp_path / "db" / "jobs.sqlite")


def _sids(tmp_path):
    return {m["original_filename"]: m["source_id"]
            for m in manifests.list_manifests(tmp_path / "raw" / "manifests")}


def _rewrite_md(tmp_path, sid, body):
    (tmp_path / "normalized" / "markdown" / f"{sid}.md").write_text(
        f"# T\n\n{body}\n", encoding="utf-8")


def _promote_rid(name):
    return reviews.review_id("promote_candidate_node",
                             {"node_id": items.node_id(name)})


def _view(tmp_path, sid):
    return review_read.source_review_view(
        tmp_path / "reviews", sid, graph_db=tmp_path / "db" / "graph.sqlite",
        wiki_dir=tmp_path / "wiki", manifests_dir=tmp_path / "raw" / "manifests")


def _index(tmp_path):
    return review_read.source_review_index(
        tmp_path / "reviews", graph_db=tmp_path / "db" / "graph.sqlite",
        wiki_dir=tmp_path / "wiki", manifests_dir=tmp_path / "raw" / "manifests")


def _validators_green(tmp_path):
    root = str(tmp_path)
    assert validate_frontmatter.main([root]) == 0
    assert validate_graph.main([root]) == 0
    assert validate_projection.main([root]) == 0
    assert validate_wikilinks.main([root]) == 0


def _item(name, aliases=(), item_type="method_technique"):
    return {"name": name, "item_type": item_type, "aliases": list(aliases)}


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    settings.manifests_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


# --- attribution ------------------------------------------------------------


def test_index_and_multi_source_candidate_attribution(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    _extract(tmp_path, RoutingAdapter({
        "alpha": {"items": [_item("Shared"), _item("Only A")]},
        "beta": {"items": [_item("Shared"), _item("Only B")]},
    }))
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]

    idx = _index(tmp_path)
    assert idx["graph_available"]
    ordered = sorted(idx["sources"], key=lambda s: (str(s["discovered_at"]), s["source_id"]))
    assert [s["source_id"] for s in idx["sources"]] == [s["source_id"] for s in ordered]
    assert len(idx["sources"]) == 2
    by_id = {s["source_id"]: s for s in idx["sources"]}
    assert by_id[a]["counts"]["remaining"] == 2 and by_id[b]["counts"]["remaining"] == 2
    # A multi-source candidate counts ONCE overall (3 distinct items, not 4 rows).
    assert idx["totals"]["remaining_overall"] == 3
    assert idx["totals"]["first_remaining_source"] in (a, b)

    # Shared appears on BOTH screens with the other-sources badge.
    for sid, other in ((a, "Only A"), (b, "Only B")):
        view = _view(tmp_path, sid)
        titles = {r["title"]: r for r in view["candidates"]}
        assert set(titles) == {"Shared", other}
        assert titles["Shared"]["other_source_count"] == 1
        assert titles[other]["other_source_count"] == 0
        assert all(r["decidable"] for r in view["candidates"])

    # First decision resolves globally: both screens render it read-only decided.
    reviews.resolve_review_item(tmp_path / "reviews", _promote_rid("Shared"),
                                decision="approved", decided_by="human",
                                note="per-source flow: " + a)
    for sid in (a, b):
        row = next(r for r in _view(tmp_path, sid)["candidates"] if r["title"] == "Shared")
        assert not row["decidable"] and row["status"] == "approved"
        assert row["decided_by"] == "human" and a in (row["decision_note"] or "")
    assert _index(tmp_path)["totals"]["remaining_overall"] == 2


def test_retired_section_h_predicate(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    _extract(tmp_path, RoutingAdapter({
        "alpha": {"items": [_item("Gone"), _item("Both Gone")]},
        "beta": {"items": [_item("Both Gone"), _item("Keeper B")]},
    }))
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    # Re-extract BOTH sources without the old items: Gone (H={a}) and Both Gone (H={a,b}).
    _rewrite_md(tmp_path, a, "alpha second body")
    _rewrite_md(tmp_path, b, "beta second body")
    _extract(tmp_path, RoutingAdapter({
        "alpha": {"items": [_item("New A")]},
        "beta": {"items": [_item("Keeper B")]},
    }))

    view_a, view_b = _view(tmp_path, a), _view(tmp_path, b)
    # Gone: single-source history -> deterministic, shown under a only.
    assert [r["title"] for r in view_a["retired"]] == ["Gone"]
    assert view_b["retired"] == []
    # Both Gone: |H| = 2 -> ambiguous, flat queue only (counted in the global remainder).
    assert "deprecate_wiki_page" in view_a["global_remaining_by_type"]
    both_gone_id = items.node_id("Both Gone")
    assert all(r["node_id"] != both_gone_id for r in view_a["retired"] + view_b["retired"])
    # |H| = 0 (no superseded provenance at all) -> flat only.
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    graph.upsert_node(gconn, node_id="itm_feedfeedfeedfeed", node_type="item",
                      item_type="method_technique",
                      slug="no-history", status="deprecated_candidate")
    gconn.close()
    reviews.create_review_item(
        tmp_path / "reviews", review_type="deprecate_wiki_page",
        subject={"node_id": "itm_feedfeedfeedfeed", "page": "Items/no-history.md"},
        proposal={"to_status": "deprecated_candidate",
                  "reason_code": "no_active_mentions",
                  "reason": "no active source mentions remain"},
        context={"node_type": "item"})
    assert all(r["node_id"] != "itm_feedfeedfeedfeed"
               for s in (_view(tmp_path, a), _view(tmp_path, b)) for r in s["retired"])


# --- batch decide endpoint ----------------------------------------------------


def test_batch_decide_partial_skip_and_drafts(client, tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    _extract(tmp_path, RoutingAdapter({"alpha": {"items": [
        _item("Approve Me"), _item("Reject Me"), _item("Defer Me"),
        _item("Untouched"), _item("Predecided")]}}))
    a = _sids(tmp_path)["a.md"]
    rids = {n: _promote_rid(n)
            for n in ("Approve Me", "Reject Me", "Defer Me", "Untouched", "Predecided")}
    reviews.resolve_review_item(tmp_path / "reviews", rids["Predecided"],
                                decision="rejected", decided_by="human")

    form = {
        f"decision_{rids['Approve Me']}": "approve",
        f"amend_title_{rids['Approve Me']}": "Approved Name",
        f"amend_aliases_{rids['Approve Me']}": "Alias One, Alias Two",
        f"decision_{rids['Reject Me']}": "reject",
        f"amend_title_{rids['Reject Me']}": "typed but discarded",  # dropped on reject, no 400
        f"decision_{rids['Defer Me']}": "defer",
        f"amend_description_{rids['Defer Me']}": "draft description",
        f"decision_{rids['Untouched']}": "",                        # untouched = stays pending
        f"decision_{rids['Predecided']}": "approve",                # flip -> per-item skip, no abort
        "decision_rev_notavalidid!!": "approve",                    # invalid id -> per-item skip
        "note": "",
    }
    resp = client.post(f"/ui/reviews/sources/{a}/decide", data=form)
    assert resp.status_code == 200

    rdir = tmp_path / "reviews"
    approved = json.loads((rdir / "approved" / f"{rids['Approve Me']}.json").read_text())
    assert approved["amendments"] == {"title": "Approved Name",
                                      "aliases": ["Alias One", "Alias Two"]}
    assert f"per-source flow: {a}" in approved["decision_note"]     # which source decided it
    rejected = json.loads((rdir / "rejected" / f"{rids['Reject Me']}.json").read_text())
    assert "amendments" not in rejected
    deferred = json.loads((rdir / "pending" / f"{rids['Defer Me']}.json").read_text())
    assert deferred["status"] == "deferred"
    assert deferred["draft_amendments"] == {"description": "draft description"}
    untouched = json.loads((rdir / "pending" / f"{rids['Untouched']}.json").read_text())
    assert untouched["status"] == "pending"
    # The flip attempt skipped with a reason but did NOT abort the batch (others recorded).
    assert (rdir / "rejected" / f"{rids['Predecided']}.json").exists()
    body = resp.text
    assert "409" in body and "invalid review id" in body
    # Draft round-trip: the screen prefills the deferred row's draft.
    screen = client.get(f"/ui/reviews/sources/{a}").text
    assert "draft description" in screen


def test_json_approve_rejects_bad_amendments(client, tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    _extract(tmp_path, RoutingAdapter({"alpha": {"items": [_item("Solo")]}}))
    rid = _promote_rid("Solo")
    # Unknown field / wrong type / non-promote type all 400 before any ledger write.
    assert client.post(f"/reviews/{rid}/approve",
                       json={"amendments": {"nope": 1}}).status_code == 400
    assert client.post(f"/reviews/{rid}/approve",
                       json={"amendments": {"aliases": "not-a-list"}}).status_code == 400
    assert client.post(f"/reviews/{rid}/reject",
                       json={"amendments": {"title": "x"}}).status_code == 400
    assert (tmp_path / "reviews" / "pending" / f"{rid}.json").exists()
    ok = client.post(f"/reviews/{rid}/approve",
                     json={"amendments": {"title": "Solo Fixed", "description": "d"}})
    assert ok.status_code == 200
    approved = json.loads(
        (tmp_path / "reviews" / "approved" / f"{rid}.json").read_text(encoding="utf-8"))
    assert approved["amendments"] == {"title": "Solo Fixed", "description": "d"}


# --- amendments applied by the promote executor -------------------------------


def test_amendments_apply_frozen_id_slug_move_and_description(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    _extract(tmp_path, RoutingAdapter({"alpha": {"items": [_item("Old Name")]}}))
    a = _sids(tmp_path)["a.md"]
    nid = items.node_id("Old Name")
    reviews.resolve_review_item(
        tmp_path / "reviews", _promote_rid("Old Name"), decision="approved",
        decided_by="human",
        amendments={"title": "New Name", "aliases": ["Extra"], "description": "human prose"})

    summary = promote.promote_candidates(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite",
                                         rebuild_index=False)
    assert summary["promoted"] == 1 and summary["amended"] == 1
    assert summary["affected_sources"] == [a]

    old_page = tmp_path / "wiki" / "Items" / "old-name.md"
    new_page = tmp_path / "wiki" / "Items" / "new-name.md"
    assert new_page.exists() and not old_page.exists()             # executor owns the move
    fm = parse_frontmatter(new_page.read_text(encoding="utf-8"))
    assert fm["item_id"] == nid                                  # frozen id, never re-hashed
    assert fm["status"] == "active"
    assert fm["aliases"] == ["Extra", "Old Name"]                   # old title stays findable
    assert fm["description"] == "human prose"
    assert "## Description" in new_page.read_text(encoding="utf-8")
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert graph.get_node(gconn, nid)["slug"] == "new-name"
    gconn.close()
    # The mentioning Source page re-rendered to the new slug (link fan-out).
    src_page = (tmp_path / "wiki" / "Sources" / f"{a}.md").read_text(encoding="utf-8")
    assert "[[Items/new-name" in src_page and "old-name" not in src_page
    _validators_green(tmp_path)

    # The description survives a later re-extraction (recompose threads it through).
    _rewrite_md(tmp_path, a, "alpha second body")
    _extract(tmp_path, RoutingAdapter({"alpha": {"items": [_item("Old Name")]}}))
    fm2 = parse_frontmatter(new_page.read_text(encoding="utf-8"))
    assert fm2["description"] == "human prose" and fm2["status"] == "active"


def test_amended_slug_collision_skips_with_reason(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    _extract(tmp_path, RoutingAdapter({"alpha": {"items": [_item("First"), _item("Second")]}}))
    reviews.resolve_review_item(
        tmp_path / "reviews", _promote_rid("Second"), decision="approved",
        decided_by="human", amendments={"title": "First"})
    summary = promote.promote_candidates(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite",
                                         rebuild_index=False)
    assert {"review_id": _promote_rid("Second"),
            "reason": "amended_slug_collision"} in summary["skipped"]
    # Nothing moved: Second stays candidate at its own slug.
    fm = parse_frontmatter(
        (tmp_path / "wiki" / "Items" / "second.md").read_text(encoding="utf-8"))
    assert fm["status"] == "candidate"


# --- human-add ----------------------------------------------------------------


def _human_add(tmp_path, **kw):
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        return human_add.add_candidate(
            gconn, root=tmp_path, wiki_dir=tmp_path / "wiki",
            reviews_dir=tmp_path / "reviews", **kw)
    finally:
        gconn.close()


def test_human_add_matrix(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    _extract(tmp_path, RoutingAdapter({"alpha": {
        "items": [_item("Existing"),
                  _item("Jane Tool", item_type="product_tool_platform")]}}))
    a = _sids(tmp_path)["a.md"]
    rdir = tmp_path / "reviews"

    # (1) brand-new candidate: producer writes + pre-approved promote + purpose-named audit.
    out = _human_add(tmp_path, source_id=a, item_type="method_technique", title="Brand New",
                     aliases=["BN"], description="added by hand")
    assert out["outcome"] == "created" and out["promote_resolution"] == "approved"
    nid = out["node_id"]
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert graph.get_node(gconn, nid)["status"] == "candidate"
    edge = gconn.execute(
        "SELECT asserted_by, evidence_source_id, evidence_char_start FROM edges "
        "WHERE src_id=? AND dst_id=? AND edge_type='mentions' AND status='active'",
        (a, nid)).fetchone()
    assert edge["asserted_by"] == "human"
    assert edge["evidence_source_id"] is None and edge["evidence_char_start"] is None
    gconn.close()
    page = tmp_path / "wiki" / "Items" / "brand-new.md"
    fm = parse_frontmatter(page.read_text(encoding="utf-8"))
    assert fm["description"] == "added by hand" and fm["aliases"] == ["BN"]
    assert (rdir / "approved" / f"{out['review_id']}.json").exists()
    audits = list((rdir / "audit_log").glob(f"{out['review_id']}-human-added-*.json"))
    assert len(audits) == 1
    entry = json.loads(audits[0].read_text(encoding="utf-8"))
    assert entry["source_id"] == a and entry["node_id"] == nid and entry["node_created"]
    assert entry["actor"] == "human" and entry["title"] == "Brand New"

    # (2) existing candidate: mention + approve its pending item; no second node.
    out2 = _human_add(tmp_path, source_id=a, item_type="method_technique", title="Existing")
    assert out2["outcome"] == "mention_added"
    assert out2["node_id"] == items.node_id("Existing")
    assert (rdir / "approved" / f"{out2['review_id']}.json").exists()

    # (3) type conflict: mention routed to the existing node + change_item_type filed
    # (the page keeps its classification until the flip applies — nothing auto-retypes).
    out3 = _human_add(tmp_path, source_id=a, item_type="provider_institution", title="Jane Tool")
    assert out3["outcome"] == "routed_retype"
    assert out3["item_type"] == "product_tool_platform"            # existing type stays authoritative
    retype = [json.loads(p.read_text(encoding="utf-8"))
              for p in (rdir / "pending").glob("*.json")]
    retype = [r for r in retype if r["type"] == "change_item_type"
              and r["subject"]["node_id"] == out3["node_id"]]
    assert len(retype) == 1 and retype[0]["context"]["source_id"] == a
    assert retype[0]["subject"]["to_item_type"] == "provider_institution"

    # (4) the sentinel is model-only: a human add must name a REAL type; nothing written.
    before = len(list((rdir / "audit_log").glob("*-human-added-*.json")))
    out4 = _human_add(tmp_path, source_id=a, item_type="unclassified_review_required",
                      title="Whatever")
    assert out4["outcome"] == "blocked" and out4["reason"] == "invalid_item_type"
    assert len(list((rdir / "audit_log").glob("*-human-added-*.json"))) == before

    # (5) rejected slot: a human rejection blocks the add BEFORE any write (ADR-0045 reopen path).
    _human_add(tmp_path, source_id=a, item_type="method_technique", title="Doomed")  # created+approved
    doomed_rid = _promote_rid("Doomed")
    reviews.reopen_review_item(rdir, doomed_rid, reason="test")
    reviews.resolve_review_item(rdir, doomed_rid, decision="rejected", decided_by="human",
                                note="not wanted")
    out5 = _human_add(tmp_path, source_id=a, item_type="method_technique", title="Doomed")
    assert out5["outcome"] == "blocked"
    assert out5["reason"] == "promotion_previously_rejected"
    assert out5["decided_by"] == "human" and out5["review_id"] == doomed_rid

    # (6) unknown source: blocked.
    assert _human_add(tmp_path, source_id="src_0000000000000000", item_type="method_technique",
                      title="X")["reason"] == "unknown_source"

    # Anchorless human mentions + all adds keep every validator green.
    _validators_green(tmp_path)


def test_human_add_already_active_mention_only(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    _extract(tmp_path, RoutingAdapter({"alpha": {"items": [_item("Hot Topic")]}}))
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    reviews.resolve_review_item(tmp_path / "reviews", _promote_rid("Hot Topic"),
                                decision="approved", decided_by="human")
    promote.promote_candidates(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite",
                               rebuild_index=False)
    out = _human_add(tmp_path, source_id=b, item_type="method_technique", title="Hot Topic")
    assert out["outcome"] == "mention_added_active" and out["promote_resolution"] is None
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert sorted(graph.sources_for_node(gconn, out["node_id"])) == sorted([a, b])
    assert graph.get_node(gconn, out["node_id"])["status"] == "active"
    gconn.close()


def test_human_add_endpoint(client, tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    _extract(tmp_path, RoutingAdapter({"alpha": {"items": [_item("Existing")]}}))
    a = _sids(tmp_path)["a.md"]
    resp = client.post(f"/ui/reviews/sources/{a}/add",
                       data={"title": "Via Form", "item_type": "method_technique",
                             "aliases": "VF", "description": "form add"},
                       follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].endswith(f"/sources/{a}")
    assert (tmp_path / "wiki" / "Items" / "via-form.md").exists()
    # Rejected slot via the endpoint: 409 with the reopen pointer.
    rid = _promote_rid("Existing")
    reviews.resolve_review_item(tmp_path / "reviews", rid, decision="rejected",
                                decided_by="human")
    resp = client.post(f"/ui/reviews/sources/{a}/add",
                       data={"title": "Existing", "item_type": "method_technique"})
    assert resp.status_code == 409 and "reopen" in resp.text.lower()
    assert client.post("/ui/reviews/sources/src_not_canonical!!/add",
                       data={"title": "x", "item_type": "method_technique"}).status_code == 404


# --- batch scope guard (review round, B1) --------------------------------------


def test_batch_rejects_non_visible_and_global_rids(client, tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    _extract(tmp_path, RoutingAdapter({
        "alpha": {"items": [_item("Mine")]},
        "beta": {"items": [_item("Foreign")]},
    }))
    sids = _sids(tmp_path)
    a = sids["a.md"]
    foreign_rid = _promote_rid("Foreign")            # valid id, visible only on source b
    global_rid = reviews.create_review_item(         # a global type: never source-attributable
        tmp_path / "reviews", review_type="merge_items",
        subject={"survivor_node_id": "itm_1111111111111111",
                 "absorbed_node_id": "itm_2222222222222222"},
        proposal={"to_status": "merged"})
    mine_rid = _promote_rid("Mine")
    reviews.resolve_review_item(tmp_path / "reviews", mine_rid, decision="approved",
                                decided_by="human")

    resp = client.post(f"/ui/reviews/sources/{a}/decide", data={
        f"decision_{foreign_rid}": "approve",
        f"decision_{global_rid}": "approve",
        f"decision_{mine_rid}": "approve",           # decided visible row -> idempotent no-op
    })
    assert resp.status_code == 200
    assert resp.text.count("not_attributable_to_source") == 2
    # No ledger mutation for the forged rows: both still pending, no audit entries.
    for rid in (foreign_rid, global_rid):
        assert (tmp_path / "reviews" / "pending" / f"{rid}.json").exists()
        assert list((tmp_path / "reviews" / "audit_log").glob(f"{rid}-*.json")) == []
    # The decided visible row was permitted and resolved as an idempotent no-op (not a skip).
    assert (tmp_path / "reviews" / "approved" / f"{mine_rid}.json").exists()


# --- human-add slug collision + index freshness (review round, B2/B3) ----------


def test_human_add_slug_collision_blocks_before_any_write(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    _extract(tmp_path, RoutingAdapter({"alpha": {"items": [_item("Foo Bar")]}}))
    a = _sids(tmp_path)["a.md"]
    # "Foo-Bar" normalizes to a DIFFERENT name-hash than "Foo Bar" but the SAME slug.
    new_id = items.node_id("Foo-Bar")
    assert new_id != items.node_id("Foo Bar")
    assert items._slug("Foo-Bar") == items._slug("Foo Bar") == "foo-bar"
    rdir = tmp_path / "reviews"
    audits_before = len(list((rdir / "audit_log").glob("*-human-added-*.json")))

    out = _human_add(tmp_path, source_id=a, item_type="method_technique", title="Foo-Bar")
    assert out["outcome"] == "blocked" and out["reason"] == "slug_collision"
    assert out["existing_node_id"] == items.node_id("Foo Bar")
    # Zero partial writes: no node, no mention, no promote item, no audit; page keeps its owner.
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert graph.get_node(gconn, new_id) is None
    gconn.close()
    assert not (rdir / "pending" / f"{_promote_rid('Foo-Bar')}.json").exists()
    assert not (rdir / "approved" / f"{_promote_rid('Foo-Bar')}.json").exists()
    assert len(list((rdir / "audit_log").glob("*-human-added-*.json"))) == audits_before
    fm = parse_frontmatter(
        (tmp_path / "wiki" / "Items" / "foo-bar.md").read_text(encoding="utf-8"))
    assert fm["item_id"] == items.node_id("Foo Bar")


def test_human_add_rebuilds_wiki_index(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    _extract(tmp_path, RoutingAdapter({"alpha": {"items": [_item("Existing")]}}))
    a = _sids(tmp_path)["a.md"]
    # The producer index seam runs root/scripts/rebuild_index.py (dependency-free by design).
    (tmp_path / "scripts").mkdir(exist_ok=True)
    shutil.copy(SCRIPTS / "rebuild_index.py", tmp_path / "scripts" / "rebuild_index.py")

    out = _human_add(tmp_path, source_id=a, item_type="method_technique", title="Indexed Fresh")
    assert out["outcome"] == "created" and out["index_rebuilt"] is True
    index_md = (tmp_path / "wiki" / "index.md").read_text(encoding="utf-8")
    assert "indexed-fresh" in index_md


# --- untrusted text in the new pages (ADR-0035 A8 invariant) -------------------


def test_source_screen_escapes_untrusted_text(client, tmp_path):
    hostile = "<script>alert(1)</script>"
    _build(tmp_path, {"a.md": "alpha body"})
    _extract(tmp_path, RoutingAdapter({"alpha": {"items": [
        {"name": f"Evil {hostile}", "item_type": "method_technique",
         "aliases": [f"x' onerror='{hostile}"]}]}}))
    a = _sids(tmp_path)["a.md"]
    _human_add(tmp_path, source_id=a, item_type="method_technique", title="Described",
               description=f"desc {hostile}")
    for url in ("/ui/reviews/sources", f"/ui/reviews/sources/{a}"):
        body = client.get(url).text
        assert hostile not in body
        assert "&lt;script&gt;" in body or "Evil" not in body
    # Batch results page escapes a hostile skip payload too.
    resp = client.post(f"/ui/reviews/sources/{a}/decide",
                       data={f"decision_{hostile}": "approve"})
    assert resp.status_code == 200 and hostile not in resp.text
