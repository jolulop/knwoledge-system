"""ADR-0058 per-source review flow, restated by ADR-0066 (per-document keep/drop tag links):
attribution, batch decide, amendments, human-add, XSS."""
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

from app.backend import graph, manifests, review_read, taxonomy
from app.backend import main as main_module
from app.backend.config import get_settings
from app.llm.cache import ResponseCache
from app.llm.client import LLMClient
from app.workers import extract, human_add, intake, items, reviews, tag_links, wiki
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


def _keep(tmp_path, sid, name, **amendments):
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        return tag_links.keep(gconn, source_id=sid, node_id=items.node_id(name),
                              wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews",
                              amendments=amendments or None)
    finally:
        gconn.close()


def test_index_and_per_document_tag_rows(tmp_path):
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
    # ADR-0066: a tag link is decided PER DOCUMENT — the shared tag is two rows (4, not 3).
    assert idx["totals"]["remaining_overall"] == 4
    assert idx["totals"]["first_remaining_source"] in (a, b)

    for sid, other in ((a, "Only A"), (b, "Only B")):
        view = _view(tmp_path, sid)
        assert {r["title"] for r in view["suggestions"]} == {"Shared", other}
        assert view["kept"] == [] and view["dropped"] == []

    # Keeping Shared on a decides ONLY a's link: b still has it as a suggestion (now active).
    assert _keep(tmp_path, a, "Shared")["effect"] == "activated"
    view_a, view_b = _view(tmp_path, a), _view(tmp_path, b)
    assert [r["title"] for r in view_a["kept"]] == ["Shared"]
    assert [r["title"] for r in view_a["suggestions"]] == ["Only A"]
    shared_b = next(r for r in view_b["suggestions"] if r["title"] == "Shared")
    assert shared_b["kept_elsewhere"] == 1 and shared_b["node_status"] == "active"
    assert _index(tmp_path)["totals"]["remaining_overall"] == 3
    wiki.generate_wiki(tmp_path, templates_dir=TEMPLATES, rebuild_index=False)   # Source projections
    _validators_green(tmp_path)


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


def test_batch_keep_drop_block_partial_skip(client, tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    _extract(tmp_path, RoutingAdapter({
        "alpha": {"items": [_item("Keep Me"), _item("Drop Me"), _item("Block Me"),
                            _item("Untouched")]},
        "beta": {"items": [_item("Block Me")]},
    }))
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    nid = {n: items.node_id(n) for n in ("Keep Me", "Drop Me", "Block Me", "Untouched")}
    form = {
        f"link_{nid['Keep Me']}": "keep",
        f"amend_title_{nid['Keep Me']}": "Kept Name",
        f"amend_aliases_{nid['Keep Me']}": "Alias One\nAlias, Two",   # one per line (ADR-0066 P5)
        f"link_{nid['Drop Me']}": "drop",
        f"link_{nid['Block Me']}": "block",
        f"link_{nid['Untouched']}": "",                         # untouched = records nothing
        "link_itm_notvalid": "keep",                            # invalid id -> per-row skip
        "note": "",
    }
    resp = client.post(f"/ui/reviews/sources/{a}/decide", data=form)
    assert resp.status_code == 200
    assert "invalid item id" in resp.text

    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        kept = graph.get_node(gconn, nid["Keep Me"])
        assert kept["status"] == "active" and kept["slug"] == "kept-name"   # amended at activation
        assert graph.get_node(gconn, nid["Drop Me"]) is None                # never kept -> deleted
        assert graph.get_node(gconn, nid["Block Me"]) is None               # withdrawn on a AND b
        assert graph.get_node(gconn, nid["Untouched"])["status"] == "candidate"
    finally:
        gconn.close()
    fm = parse_frontmatter((tmp_path / "wiki" / "Items" / "kept-name.md").read_text(encoding="utf-8"))
    assert fm["aliases"] == ["Alias One", "Alias, Two", "Keep Me"] and fm["item_id"] == nid["Keep Me"]
    ledger = tag_links.load_ledger(tmp_path / "reviews", a)
    assert list(ledger["kept"]) == [nid["Keep Me"]]
    assert [d["key"] for d in ledger["drops"]] == ["drop me"]               # only the explicit drop
    assert tag_links.load_ledger(tmp_path / "reviews", b)["drops"] == []    # a block writes none
    assert "Block Me" in tag_links.load_blocked(tmp_path / "policies").values()
    assert _view(tmp_path, b)["suggestions"] == []
    # The kept row now offers only drop; "keep" on it is a per-row skip, never an abort.
    resp = client.post(f"/ui/reviews/sources/{a}/decide", data={f"link_{nid['Keep Me']}": "keep"})
    assert "unknown action: keep" in resp.text
    wiki.generate_wiki(tmp_path, templates_dir=TEMPLATES, rebuild_index=False)   # Source projections
    _validators_green(tmp_path)


def test_json_approve_rejects_bad_amendments(client, tmp_path):
    # The flat-queue promote path still validates amendments (pre-ADR-0066 / hand-filed items).
    _build(tmp_path, {"a.md": "alpha body"})
    _extract(tmp_path, RoutingAdapter({"alpha": {"items": [_item("Solo")]}}))
    rid = reviews.create_review_item(
        tmp_path / "reviews", review_type="promote_candidate_node",
        subject={"node_id": items.node_id("Solo")}, proposal={"to_status": "active"})
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


# --- amendments applied when a keep activates a candidate ---------------------


def test_keep_amendments_frozen_id_slug_move_and_description(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    _extract(tmp_path, RoutingAdapter({"alpha": {"items": [_item("Old Name")]}}))
    a = _sids(tmp_path)["a.md"]
    nid = items.node_id("Old Name")
    out = _keep(tmp_path, a, "Old Name", title="New Name", aliases=["Extra"],
                description="human prose")
    assert out["outcome"] == "kept" and out["effect"] == "activated"
    tag_links.finish(tmp_path, out["affected_sources"], wiki_dir=tmp_path / "wiki",
                     templates_dir=TEMPLATES)

    old_page = tmp_path / "wiki" / "Items" / "old-name.md"
    new_page = tmp_path / "wiki" / "Items" / "new-name.md"
    assert new_page.exists() and not old_page.exists()             # activation owns the move
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

    # The description AND the keep survive a later re-extraction (recompose threads them through).
    _rewrite_md(tmp_path, a, "alpha second body")
    _extract(tmp_path, RoutingAdapter({"alpha": {"items": [_item("Old Name")]}}))
    fm2 = parse_frontmatter(new_page.read_text(encoding="utf-8"))
    assert fm2["description"] == "human prose" and fm2["status"] == "active"


def test_keep_amended_slug_collision_blocks_before_any_write(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    _extract(tmp_path, RoutingAdapter({"alpha": {"items": [_item("First"), _item("Second")]}}))
    a = _sids(tmp_path)["a.md"]
    out = _keep(tmp_path, a, "Second", title="First")
    assert out["outcome"] == "blocked" and out["reason"] == "amended_slug_collision"
    # Nothing written: Second stays a candidate suggestion at its own slug, no ledger keep.
    fm = parse_frontmatter(
        (tmp_path / "wiki" / "Items" / "second.md").read_text(encoding="utf-8"))
    assert fm["status"] == "candidate"
    assert tag_links.load_ledger(tmp_path / "reviews", a)["kept"] == {}
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert graph.kept_sources_for_node(gconn, items.node_id("Second")) == []
    gconn.close()


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

    # (1) brand-new tag: created + kept (ADR-0066: the add IS a keep -> active) + audit.
    out = _human_add(tmp_path, source_id=a, item_type="method_technique", title="Brand New",
                     aliases=["BN"], description="added by hand")
    assert out["outcome"] == "created" and out["keep_effect"] == "activated"
    nid = out["node_id"]
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert graph.get_node(gconn, nid)["status"] == "active"
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
    assert nid in tag_links.load_ledger(rdir, a)["kept"]
    assert not list((rdir / "approved").glob("*.json"))             # no promotion review at all
    audits = list((rdir / "audit_log").glob(f"{out['review_id']}-human-added-*.json"))
    assert len(audits) == 1
    entry = json.loads(audits[0].read_text(encoding="utf-8"))
    assert entry["source_id"] == a and entry["node_id"] == nid and entry["node_created"]
    assert entry["actor"] == "human" and entry["title"] == "Brand New"

    # (2) existing candidate suggestion: kept on this document -> active; no second node.
    out2 = _human_add(tmp_path, source_id=a, item_type="method_technique", title="Existing")
    assert out2["outcome"] == "mention_added" and out2["keep_effect"] == "activated"
    assert out2["node_id"] == items.node_id("Existing")

    # (3) type conflict: kept on the existing node + change_item_type filed
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

    # (5) ADR-0066: a drop is per document and never blocks an explicit add — adding a tag
    # dropped earlier keeps it again and clears the drop record.
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    tag_links.drop(gconn, source_id=a, node_id=items.node_id("Existing"),
                   wiki_dir=tmp_path / "wiki", reviews_dir=rdir)
    gconn.close()
    assert tag_links.drop_filter(rdir, a)[0] == {"existing"}
    out5 = _human_add(tmp_path, source_id=a, item_type="method_technique", title="Existing")
    assert out5["outcome"] == "mention_added_active" and out5["keep_effect"] == "revived"
    assert tag_links.drop_filter(rdir, a) == (set(), set())

    # (6) unknown source: blocked.
    assert _human_add(tmp_path, source_id="src_0000000000000000", item_type="method_technique",
                      title="X")["reason"] == "unknown_source"

    # Anchorless human mentions + all adds keep every validator green.
    _validators_green(tmp_path)


def test_human_add_already_active_tag_on_another_document(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    _extract(tmp_path, RoutingAdapter({"alpha": {"items": [_item("Hot Topic")]}}))
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _keep(tmp_path, a, "Hot Topic")
    out = _human_add(tmp_path, source_id=b, item_type="method_technique", title="Hot Topic")
    assert out["outcome"] == "mention_added_active" and out["keep_effect"] == "kept"
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert sorted(graph.sources_for_node(gconn, out["node_id"])) == sorted([a, b])
    assert sorted(graph.kept_sources_for_node(gconn, out["node_id"])) == sorted([a, b])
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
    assert "Via Form" in {r["title"] for r in _view(tmp_path, a)["kept"]}
    assert client.post(f"/ui/reviews/sources/{a}/add",
                       data={"title": "x", "item_type": "nope"}).status_code == 400
    assert client.post("/ui/reviews/sources/src_not_canonical!!/add",
                       data={"title": "x", "item_type": "method_technique"}).status_code == 404


# --- batch scope guard (review round, B1) --------------------------------------


def test_batch_rejects_non_visible_and_global_rows(client, tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    _extract(tmp_path, RoutingAdapter({
        "alpha": {"items": [_item("Mine")]},
        "beta": {"items": [_item("Foreign")]},
    }))
    sids = _sids(tmp_path)
    a = sids["a.md"]
    foreign = items.node_id("Foreign")               # valid id, a link only on source b
    global_rid = reviews.create_review_item(         # a global type: never source-attributable
        tmp_path / "reviews", review_type="merge_items",
        subject={"survivor_node_id": "itm_1111111111111111",
                 "absorbed_node_id": "itm_2222222222222222"},
        proposal={"to_status": "merged"})

    resp = client.post(f"/ui/reviews/sources/{a}/decide", data={
        f"link_{foreign}": "drop",
        f"decision_{global_rid}": "approve",
        f"link_{items.node_id('Mine')}": "keep",
    })
    assert resp.status_code == 200
    assert resp.text.count("not_attributable_to_source") == 2
    # No mutation for the forged rows: Foreign still a suggestion on b, the merge still pending.
    assert [r["title"] for r in _view(tmp_path, sids["b.md"])["suggestions"]] == ["Foreign"]
    assert (tmp_path / "reviews" / "pending" / f"{global_rid}.json").exists()
    assert list((tmp_path / "reviews" / "audit_log").glob(f"{global_rid}-*.json")) == []
    # The visible row was decided.
    assert [r["title"] for r in _view(tmp_path, a)["kept"]] == ["Mine"]


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
    # Zero partial writes: no node, no mention, no ledger keep, no audit; page keeps its owner.
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert graph.get_node(gconn, new_id) is None
    gconn.close()
    assert tag_links.load_ledger(rdir, a)["kept"] == {}
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


# --- UAT round: preselect link, alphabetical selects, view-original ------------


def test_preselect_keep_checks_suggestions_only(client, tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    _extract(tmp_path, RoutingAdapter({"alpha": {"items": [_item("Fresh"), _item("Kept")]}}))
    a = _sids(tmp_path)["a.md"]
    _keep(tmp_path, a, "Kept")
    fresh, kept = items.node_id("Fresh"), items.node_id("Kept")

    plain = client.get(f"/ui/reviews/sources/{a}").text
    assert f"name='link_{fresh}' value='' checked" in plain         # default = leave undecided
    assert "— leave undecided" in plain
    assert "value='keep' checked" not in plain
    # The explicit pre-select control is a GET-form button targeting the same URL.
    assert f"<form method='get' action='/ui/reviews/sources/{a}'>" in plain
    assert "<input type='hidden' name='preselect' value='keep'>" in plain
    assert "<button type='submit'>Pre-select keep for all suggestions</button>" in plain

    for value in ("keep", "approve"):                                # approve = legacy synonym
        pre = client.get(f"/ui/reviews/sources/{a}?preselect={value}").text
        assert f"name='link_{fresh}' value='keep' checked" in pre    # suggestion pre-checked
        assert f"name='link_{kept}' value='' checked" in pre         # kept row: leave kept
        assert "Clear pre-selection" in pre

    # Any other value is ignored.
    bogus = client.get(f"/ui/reviews/sources/{a}?preselect=drop").text
    assert "value='keep' checked" not in bogus
    assert "value='drop' checked" not in bogus


def test_type_selects_are_alphabetical(client, tmp_path):
    import re as _re
    _build(tmp_path, {"a.md": "alpha body"})
    _extract(tmp_path, RoutingAdapter({"alpha": {"items": [_item("Solo")]}}))
    a = _sids(tmp_path)["a.md"]
    page = client.get(f"/ui/reviews/sources/{a}").text
    selects = dict(_re.findall(r"<select name='([^']+)'>(.*?)</select>", page, _re.S))
    # Human-add select: exactly the 15 production values, alphabetical.
    add_options = _re.findall(r"<option value='([^']*)'", selects["item_type"])
    assert add_options == sorted(taxonomy.ITEM_TYPES)
    # Amendment select: the blank keep-type option, then the same alphabetical 15.
    amend_name = next(k for k in selects if k.startswith("amend_item_type_"))
    amend_options = _re.findall(r"<option value='([^']*)'", selects[amend_name])
    assert amend_options == [""] + sorted(taxonomy.ITEM_TYPES)


def test_view_original_raw_endpoint(client, tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    _extract(tmp_path, RoutingAdapter({"alpha": {"items": [_item("Solo")]}}))
    a = _sids(tmp_path)["a.md"]
    # Linked from both flow surfaces.
    assert f"/raw/{a}" in client.get(f"/ui/reviews/sources/{a}").text
    assert f"/raw/{a}" in client.get("/ui/reviews/sources").text
    # Serves the original bytes inline with a guessed MIME type.
    resp = client.get(f"/raw/{a}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/")
    assert "alpha body" in resp.text
    assert "inline" in resp.headers.get("content-disposition", "")
    # Unknown / non-canonical ids are plain 404s.
    assert client.get("/raw/src_0000000000000000").status_code == 404
    assert client.get("/raw/not_a_source_id").status_code == 404
    # A tampered manifest path can never traverse out of raw/ (safe_under containment).
    (tmp_path / "secret.txt").write_text("outside raw", encoding="utf-8")
    mpath = tmp_path / "raw" / "manifests" / f"{a}.json"
    m = json.loads(mpath.read_text(encoding="utf-8"))
    m["relative_raw_path"] = "../secret.txt"
    mpath.write_text(json.dumps(m), encoding="utf-8")
    assert client.get(f"/raw/{a}").status_code == 404



def test_raw_endpoint_never_renders_active_content_inline(client, tmp_path):
    _build(tmp_path, {"a.md": "alpha body",
                      "page.html": "<script>document.write('x')</script>",
                      "pic.svg": "<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'/>"})
    _extract(tmp_path, RoutingAdapter({"alpha": {"items": [_item("Solo")]}}))
    sids = _sids(tmp_path)
    # Untrusted active-content types NEVER render inline same-origin: attachment + nosniff.
    for fname in ("page.html", "pic.svg"):
        resp = client.get(f"/raw/{sids[fname]}")
        assert resp.status_code == 200
        assert resp.headers["content-disposition"].startswith("attachment")
        assert resp.headers["x-content-type-options"] == "nosniff"
    # Passive text renders inline — but sandboxed (opaque origin, no scripts) + nosniff.
    resp = client.get(f"/raw/{sids['a.md']}")
    assert resp.headers["content-disposition"].startswith("inline")
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["content-security-policy"] == "sandbox"
    assert resp.headers["x-content-type-options"] == "nosniff"
    # A tampered original_filename cannot smuggle an inline-HTML render for the .md bytes.
    a = sids["a.md"]
    mpath = tmp_path / "raw" / "manifests" / f"{a}.json"
    m = json.loads(mpath.read_text(encoding="utf-8"))
    m["original_filename"] = "evil.html"
    mpath.write_text(json.dumps(m), encoding="utf-8")
    resp = client.get(f"/raw/{a}")
    assert resp.headers["content-disposition"].startswith("attachment")


def test_raw_endpoint_uses_the_manifest_quarantine(client, tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    # An internal source_id mismatch quarantines the manifest (valid_manifests) -> plain 404.
    mpath = tmp_path / "raw" / "manifests" / f"{a}.json"
    m = json.loads(mpath.read_text(encoding="utf-8"))
    m["source_id"] = "src_00000000000000ff"
    mpath.write_text(json.dumps(m), encoding="utf-8")
    assert client.get(f"/raw/{a}").status_code == 404
    assert client.get("/raw/src_00000000000000ff").status_code == 404


def test_raw_endpoint_is_lifecycle_status_agnostic(client, tmp_path):
    # Operator-review access is explicitly allowed (review round): hide/archive govern
    # default retrieval + navigation, not this loopback governance surface.
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    manifests.set_status(tmp_path / "raw" / "manifests", a, "hidden")
    assert client.get(f"/raw/{a}").status_code == 200
    manifests.set_status(tmp_path / "raw" / "manifests", a, "archive_candidate")
    assert client.get(f"/raw/{a}").status_code == 200
