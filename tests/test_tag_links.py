"""ADR-0066 per-document tag links: keep/drop durability, drop records, unused tags, the blocked
list, identity operations and the ledger as authority."""
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
from app.workers import extract, human_add, intake, items, merges, tag_links, wiki
from app.workers.wiki_render import parse_frontmatter
import tagging  # noqa: E402

MODEL_REF = "anthropic:claude-sonnet-4-6"
TEMPLATES = ROOT / "templates"


class RoutingAdapter:
    """Routes the payload by a marker string found in the prompt (per-source payloads)."""
    name = "anthropic"
    supports_batch = False

    def __init__(self, routes: dict[str, list[dict]]):
        self._routes = routes

    def available(self):
        return True

    def parse(self, messages, schema, model_id, *, max_tokens):
        text = json.dumps(messages)
        for marker, payload in self._routes.items():
            if marker in text:
                return {"items": [dict(i) for i in payload]}
        return {"items": []}


def _item(name, item_type="method_technique"):
    return {"name": name, "item_type": item_type, "aliases": []}


def _build(tmp_path, files: dict[str, str]):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATES, tmp_path / "templates", dirs_exist_ok=True)
    for name, body in files.items():
        (inbox / name).write_text(f"# Title\n\n{body}\n", encoding="utf-8")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    extract.extract_sources(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    wiki.generate_wiki(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite",
                       templates_dir=TEMPLATES, rebuild_index=False)


def _extract(tmp_path, routes, *, force=False, source_ids=None):
    client = LLMClient({"anthropic": RoutingAdapter(routes)},
                       cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite"))
    out = items.extract_items(tmp_path, client=client, model_ref=MODEL_REF, force=force,
                              source_ids=source_ids, jobs_db=tmp_path / "db" / "jobs.sqlite")
    wiki.generate_wiki(tmp_path, templates_dir=TEMPLATES, rebuild_index=False, record_job=False)
    return out


def _sids(tmp_path):
    return {m["original_filename"]: m["source_id"]
            for m in manifests.list_manifests(tmp_path / "raw" / "manifests")}


def _rewrite_md(tmp_path, sid, body):
    (tmp_path / "normalized" / "markdown" / f"{sid}.md").write_text(f"# T\n\n{body}\n",
                                                                    encoding="utf-8")


def _g(tmp_path):
    return graph.connect(tmp_path / "db" / "graph.sqlite")


def _call(tmp_path, fn, **kw):
    gconn = _g(tmp_path)
    try:
        out = fn(gconn, wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews", **kw)
    finally:
        gconn.close()
    tag_links.finish(tmp_path, out.get("affected_sources") or [], wiki_dir=tmp_path / "wiki",
                     pages_changed=True, templates_dir=TEMPLATES)
    return out


def _keep(tmp_path, sid, name, **amendments):
    return _call(tmp_path, tag_links.keep, source_id=sid, node_id=items.node_id(name),
                 amendments=amendments or None)


def _drop(tmp_path, sid, name):
    return _call(tmp_path, tag_links.drop, source_id=sid, node_id=items.node_id(name))


def _block(tmp_path, name):
    return _call(tmp_path, tag_links.block, name=name, policies_dir=tmp_path / "policies")


def _node(tmp_path, name):
    gconn = _g(tmp_path)
    try:
        return graph.get_node(gconn, items.node_id(name))
    finally:
        gconn.close()


def _view(tmp_path, sid):
    return review_read.source_review_view(
        tmp_path / "reviews", sid, graph_db=tmp_path / "db" / "graph.sqlite",
        wiki_dir=tmp_path / "wiki", manifests_dir=tmp_path / "raw" / "manifests")


def _titles(rows):
    return sorted(r["title"] for r in rows)


def _validators_green(tmp_path):
    root = str(tmp_path)
    assert validate_frontmatter.main([root]) == 0
    assert validate_graph.main([root]) == 0
    assert validate_projection.main([root]) == 0
    assert validate_wikilinks.main([root]) == 0


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    settings.manifests_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


# --- decision 1: a keep lasts until dropped ----------------------------------------------------


def test_keep_survives_reextraction_that_omits_the_tag(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item("Kept Tag"), _item("Other")]})
    assert _keep(tmp_path, a, "Kept Tag")["effect"] == "activated"

    # Re-extraction (changed text, the model no longer suggests the tag) never removes a keep.
    _rewrite_md(tmp_path, a, "alpha rewritten body")
    _extract(tmp_path, {"alpha": [_item("Other")]})
    assert _node(tmp_path, "Kept Tag")["status"] == "active"
    view = _view(tmp_path, a)
    assert _titles(view["kept"]) == ["Kept Tag"] and _titles(view["suggestions"]) == ["Other"]
    _validators_green(tmp_path)


def test_keep_survives_changed_evidence_span_and_stays_one_row(tmp_path):
    _build(tmp_path, {"a.md": "Spanned tag here"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"Spanned": [_item("Spanned tag")]})
    _keep(tmp_path, a, "Spanned tag")
    _rewrite_md(tmp_path, a, "Some prefix moves it. Spanned tag later")
    _extract(tmp_path, {"Spanned": [_item("Spanned tag")]})
    view = _view(tmp_path, a)
    assert _titles(view["kept"]) == ["Spanned tag"] and view["suggestions"] == []
    _validators_green(tmp_path)


def test_changed_file_inherits_no_decisions(tmp_path):
    _build(tmp_path, {"a.md": "alpha v1"})
    _extract(tmp_path, {"alpha": [_item("Tag")]})
    a = _sids(tmp_path)["a.md"]
    _keep(tmp_path, a, "Tag")
    # A changed file is new content -> a new source_id, reviewed from scratch.
    (tmp_path / "raw" / "inbox" / "a2.md").write_text("# Title\n\nalpha v2\n", encoding="utf-8")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    extract.extract_sources(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    wiki.generate_wiki(tmp_path, templates_dir=TEMPLATES, rebuild_index=False, record_job=False)
    _extract(tmp_path, {"alpha": [_item("Tag")]})
    a2 = _sids(tmp_path)["a2.md"]
    assert a2 != a
    assert _titles(_view(tmp_path, a2)["suggestions"]) == ["Tag"]
    assert tag_links.load_ledger(tmp_path / "reviews", a2)["kept"] == {}


# --- decision 3: drops are per document, remembered, and delete never-kept tags ---------------


def test_drop_is_isolated_per_document_and_filters_reextraction(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _extract(tmp_path, {"alpha": [_item("Shared")], "beta": [_item("Shared")]})
    out = _drop(tmp_path, a, "Shared")
    assert out["effect"] == "unlinked"                     # b still suggests it: not deleted
    assert _titles(_view(tmp_path, b)["suggestions"]) == ["Shared"]
    assert [d["name"] for d in _view(tmp_path, a)["dropped"]] == ["Shared"]

    # Re-extracting a with the same suggestion (forced, cache-replayed) does not bring it back.
    summary = _extract(tmp_path, {"alpha": [_item("Shared")], "beta": [_item("Shared")]},
                       force=True)
    assert summary["suggestions_filtered_dropped"] == 1
    assert _view(tmp_path, a)["suggestions"] == []
    assert _titles(_view(tmp_path, b)["suggestions"]) == ["Shared"]
    _validators_green(tmp_path)


def test_dropping_a_never_kept_tag_everywhere_deletes_it(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item("Noise"), _item("Fine")]})
    out = _drop(tmp_path, a, "Noise")
    assert out["effect"] == "deleted"
    assert _node(tmp_path, "Noise") is None
    assert not (tmp_path / "wiki" / "Items" / "noise.md").exists()
    assert tag_links.drop_filter(tmp_path / "reviews", a)[0] == {"noise"}   # the drop record survives
    audits = list((tmp_path / "reviews" / "audit_log").glob("*-tag-deleted-*.json"))
    assert len(audits) == 1 and json.loads(audits[0].read_text())["reason"] == "dropped_never_kept"
    _validators_green(tmp_path)


def test_undo_drop_restores_the_suggestion(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a = sids["a.md"]
    _extract(tmp_path, {"alpha": [_item("Shared")], "beta": [_item("Shared")]})
    _drop(tmp_path, a, "Shared")
    out = _call(tmp_path, tag_links.undo_drop, source_id=a, key="shared")
    assert out["outcome"] == "undone" and out["restored"] is True
    view = _view(tmp_path, a)
    assert _titles(view["suggestions"]) == ["Shared"] and view["dropped"] == []
    _validators_green(tmp_path)


# --- decision 4: unused tags ---------------------------------------------------------------------


def test_last_keep_dropped_makes_unused_then_revive_then_delete_unused(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _extract(tmp_path, {"alpha": [_item("Topic")], "beta": [_item("Topic")]})
    _keep(tmp_path, a, "Topic", description="my words")
    assert _drop(tmp_path, a, "Topic")["effect"] == "unused"
    fm = parse_frontmatter((tmp_path / "wiki" / "Items" / "topic.md").read_text(encoding="utf-8"))
    assert fm["status"] == "unused" and fm["description"] == "my words"    # edits intact
    _validators_green(tmp_path)
    gconn = _g(tmp_path)
    assert [r["title"] for r in tag_links.unused_tags(gconn, tmp_path / "wiki")] == ["Topic"]
    gconn.close()

    # Keeping it on ANY document revives it with its edits.
    assert _keep(tmp_path, b, "Topic")["effect"] == "revived"
    fm = parse_frontmatter((tmp_path / "wiki" / "Items" / "topic.md").read_text(encoding="utf-8"))
    assert fm["status"] == "active" and fm["description"] == "my words"

    assert _drop(tmp_path, b, "Topic")["effect"] == "unused"
    out = _call(tmp_path, lambda gconn, **kw: tag_links.delete_unused(gconn, **kw))
    assert out["deleted"] == [items.node_id("Topic")] and out["skipped"] == []
    assert _node(tmp_path, "Topic") is None
    _validators_green(tmp_path)


def test_delete_unused_skips_merge_redirect_targets(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item("Survivor"), _item("Absorbed")]})
    _keep(tmp_path, a, "Survivor")
    _keep(tmp_path, a, "Absorbed")
    surv, absd = items.node_id("Survivor"), items.node_id("Absorbed")
    appr = tmp_path / "reviews" / "approved"
    appr.mkdir(parents=True, exist_ok=True)
    (appr / "rev_m.json").write_text(json.dumps({
        "review_id": "rev_m", "type": "merge_items", "status": "approved",
        "subject": {"survivor_node_id": surv, "absorbed_node_id": absd},
        "proposal": {"to_status": "merged"}}), encoding="utf-8")
    gconn = _g(tmp_path)
    assert tagging.close_journals(tmp_path / "reviews", merges.apply_merges(
        gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki"))["applied"] == 1
    gconn.commit()
    gconn.close()
    tag_links.finish(tmp_path, [a], wiki_dir=tmp_path / "wiki", templates_dir=TEMPLATES)
    assert list(tag_links.load_ledger(tmp_path / "reviews", a)["kept"]) == [surv]   # keep moved

    assert _drop(tmp_path, a, "Survivor")["effect"] == "unused"
    out = _call(tmp_path, lambda gconn, **kw: tag_links.delete_unused(gconn, **kw))
    assert out["deleted"] == []
    assert out["skipped"] == [{"node_id": surv, "reason": "merge_redirect_target"}]
    assert _node(tmp_path, "Survivor")["status"] == "unused"
    _validators_green(tmp_path)       # an unused survivor is a valid merge redirect target


# --- decision 5: blocked-tag list ----------------------------------------------------------------


def test_block_withdraws_suggestions_but_never_kept_links(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _extract(tmp_path, {"alpha": [_item("Generic")], "beta": [_item("Generic")]})
    _keep(tmp_path, a, "Generic")
    out = _block(tmp_path, "generic")                      # matched case-insensitively
    assert out["withdrawn"] == 1 and out["deleted"] == []
    assert _titles(_view(tmp_path, a)["kept"]) == ["Generic"]          # kept link untouched
    assert _view(tmp_path, b)["suggestions"] == []                     # b's suggestion withdrawn
    assert tag_links.load_ledger(tmp_path / "reviews", b)["drops"] == []   # no drop record
    assert _node(tmp_path, "Generic")["status"] == "active"

    # Extraction filters the blocked name; an explicit human-add is still allowed.
    summary = _extract(tmp_path, {"alpha": [_item("Generic")], "beta": [_item("Generic")]},
                       force=True)
    assert summary["suggestions_filtered_blocked"] == 2
    gconn = _g(tmp_path)
    try:
        added = human_add.add_candidate(gconn, root=tmp_path, source_id=b,
                                        item_type="method_technique", title="Generic",
                                        wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews")
    finally:
        gconn.close()
    assert added["outcome"] == "mention_added_active"
    assert _titles(_view(tmp_path, b)["kept"]) == ["Generic"]
    _validators_green(tmp_path)


def test_unblock_offers_resuggest_and_reextraction_restores(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item("Hype Word"), _item("Fine")]})
    out = _block(tmp_path, "Hype Words")                  # plural folds to the same key
    assert out["deleted"] == [items.node_id("Hype Word")]  # never kept, no links left
    res = tag_links.unblock(key=out["key"], policies_dir=tmp_path / "policies",
                            enrichment_dir=tmp_path / "normalized" / "enrichment",
                            reviews_dir=tmp_path / "reviews")
    assert res["outcome"] == "unblocked" and res["resuggest_sources"] == [a]
    assert tag_links.load_blocked(tmp_path / "policies") == {}
    _extract(tmp_path, {"alpha": [_item("Hype Word"), _item("Fine")]}, force=True,
             source_ids=res["resuggest_sources"])
    assert _titles(_view(tmp_path, a)["suggestions"]) == ["Fine", "Hype Word"]


def test_blocklist_file_is_hand_editable(tmp_path):
    pol = tmp_path / "policies"
    pol.mkdir()
    (pol / "blocked_tags.txt").write_text("# comment\nFoo Bars  # trailing comment\n\nBaz\n",
                                          encoding="utf-8")
    assert tag_links.load_blocked(pol) == {"foo bar": "Foo Bars", "baz": "Baz"}
    assert tag_links._add_blocked(pol, "foo bar") is False        # already blocked (folded)
    assert tag_links._add_blocked(pol, "Qux") is True
    assert "# comment" in (pol / "blocked_tags.txt").read_text(encoding="utf-8")


# --- the sentinel gate and the ledger as authority ---------------------------------------------


def test_unclassified_suggestion_needs_a_real_type_to_keep(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item("Vague", item_type="unclassified_review_required")]})
    out = _keep(tmp_path, a, "Vague")
    assert out["outcome"] == "blocked" and out["reason"] == "missing_required_item_type"
    assert _node(tmp_path, "Vague")["status"] == "candidate"
    assert tag_links.load_ledger(tmp_path / "reviews", a)["kept"] == {}
    out = _keep(tmp_path, a, "Vague", item_type="use_case")
    assert out["effect"] == "activated"
    node = _node(tmp_path, "Vague")
    assert node["status"] == "active" and node["item_type"] == "use_case"
    _validators_green(tmp_path)


def test_ledger_rebuilds_the_kept_edges(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item("Durable")]})
    _keep(tmp_path, a, "Durable")
    gconn = _g(tmp_path)
    gconn.execute("DELETE FROM edges WHERE asserted_by = 'human'")   # a graph rebuild loses them
    gconn.commit()
    assert graph.kept_sources_for_node(gconn, items.node_id("Durable")) == []
    assert tag_links.consistency_errors(gconn, tmp_path / "reviews")       # the validator sees it
    dry = tag_links.repair(gconn, reviews_dir=tmp_path / "reviews", wiki_dir=tmp_path / "wiki")
    assert dry["applied"] is False and dry["missing_keeps"] == 1
    assert graph.kept_sources_for_node(gconn, items.node_id("Durable")) == []   # dry run wrote nothing
    out = tag_links.repair(gconn, reviews_dir=tmp_path / "reviews", wiki_dir=tmp_path / "wiki",
                           apply=True)
    assert out["applied"] is True and out["affected_sources"] == [a]
    assert graph.kept_sources_for_node(gconn, items.node_id("Durable")) == [a]
    assert tag_links.consistency_errors(gconn, tmp_path / "reviews") == []
    gconn.close()


def test_ledger_rejects_forged_source_ids(tmp_path):
    with pytest.raises(ValueError):
        tag_links.ledger_path(tmp_path / "reviews", "../../etc/passwd")


# --- HTTP surfaces --------------------------------------------------------------------------------


def test_json_links_endpoint(client, tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _extract(tmp_path, {"alpha": [_item("Via Json")], "beta": [_item("Elsewhere")]})
    nid = items.node_id("Via Json")
    resp = client.post(f"/reviews/sources/{a}/links", json={"node_id": nid, "action": "keep"})
    assert resp.status_code == 200 and resp.json()["effect"] == "activated"
    assert client.post(f"/reviews/sources/{a}/links",
                       json={"node_id": nid, "action": "keep"}).status_code == 409   # already kept
    assert client.post(f"/reviews/sources/{a}/links",
                       json={"node_id": items.node_id("Elsewhere"), "action": "drop"}
                       ).status_code == 409                                         # b's link
    assert client.post(f"/reviews/sources/{a}/links",
                       json={"node_id": nid, "action": "approve"}).status_code == 400
    resp = client.post(f"/reviews/sources/{a}/links", json={"node_id": nid, "action": "drop"})
    assert resp.status_code == 200 and resp.json()["effect"] == "unused"
    assert _titles(_view(tmp_path, b)["suggestions"]) == ["Elsewhere"]


def test_tag_pages_escape_untrusted_names_and_unblock(client, tmp_path):
    hostile = "<script>alert(1)</script>"
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item(f"Evil {hostile}")]})
    _keep(tmp_path, a, f"Evil {hostile}")
    _drop(tmp_path, a, f"Evil {hostile}")
    _block(tmp_path, f"Bad {hostile}")
    for url in ("/ui/reviews/tags/unused", "/ui/reviews/tags/blocked", f"/ui/reviews/sources/{a}"):
        body = client.get(url).text
        assert hostile not in body and "&lt;script&gt;" in body
    key = next(iter(tag_links.load_blocked(tmp_path / "policies")))
    resp = client.post("/ui/reviews/tags/blocked/unblock", data={"key": key})
    assert resp.status_code == 200 and "Unblocked" in resp.text and hostile not in resp.text
    assert client.post("/ui/reviews/tags/blocked/unblock", data={"key": "nope"}).status_code == 404
    resp = client.post("/ui/reviews/tags/unused/delete")
    assert resp.status_code == 200 and "Deleted 1" in resp.text
    resp = client.post(f"/ui/reviews/sources/{a}/undrop",
                       data={"key": tag_links.tag_key(f"Evil {hostile}")}, follow_redirects=False)
    assert resp.status_code == 303 and tag_links.load_ledger(tmp_path / "reviews", a)["drops"] == []
    assert client.post(f"/ui/reviews/sources/{a}/undrop", data={"key": "nope"}).status_code == 404


# --- review round 2 (2026-10-25) ------------------------------------------------------------------


class CountingAdapter(RoutingAdapter):
    """RoutingAdapter that counts provider calls (cache-only must make none)."""

    def __init__(self, routes):
        super().__init__(routes)
        self.calls = 0

    def parse(self, messages, schema, model_id, *, max_tokens):
        self.calls += 1
        return super().parse(messages, schema, model_id, max_tokens=max_tokens)


def _ledger_file(tmp_path, sid):
    return tmp_path / "reviews" / "tag_links" / f"{sid}.json"


def _approve_merge(tmp_path, survivor, absorbed, rid="rev_m"):
    appr = tmp_path / "reviews" / "approved"
    appr.mkdir(parents=True, exist_ok=True)
    (appr / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": "merge_items", "status": "approved",
        "subject": {"survivor_node_id": survivor, "absorbed_node_id": absorbed},
        "proposal": {"to_status": "merged"}}), encoding="utf-8")


def _active_links(tmp_path, sid, name):
    gconn = _g(tmp_path)
    try:
        return [r for r in graph.source_tag_links(gconn, sid)
                if r["dst_id"] == items.node_id(name) and r["status"] == "active"]
    finally:
        gconn.close()


def test_concurrent_keeps_on_one_document_are_both_recorded(tmp_path, monkeypatch):
    import threading
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item("One"), _item("Two")]})
    real_load = tag_links.load_ledger
    barrier = threading.Barrier(2)

    def slow_load(*args, **kwargs):   # without the lock both threads read the empty ledger here
        out = real_load(*args, **kwargs)
        try:
            barrier.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass
        return out

    monkeypatch.setattr(tag_links, "load_ledger", slow_load)
    results = []

    def worker(name):
        gconn = _g(tmp_path)
        try:
            results.append(tag_links.keep(gconn, source_id=a, node_id=items.node_id(name),
                                          wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews"))
        finally:
            gconn.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("One", "Two")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    monkeypatch.undo()
    assert sorted(r["outcome"] for r in results) == ["kept", "kept"]
    assert sorted(tag_links.load_ledger(tmp_path / "reviews", a)["kept"]) == sorted(
        [items.node_id("One"), items.node_id("Two")])
    tag_links.finish(tmp_path, [a], wiki_dir=tmp_path / "wiki", templates_dir=TEMPLATES)  # the caller's
    gconn = _g(tmp_path)
    try:
        assert tag_links.consistency_errors(gconn, tmp_path / "reviews") == []
    finally:
        gconn.close()


@pytest.mark.parametrize("damage", ["{not json", '{"kept": ["a list"], "drops": []}',
                                    '{"kept": {}, "drops": [{"name": "no key"}]}',
                                    "{}", '{"kept": {}, "drops": []}',          # required fields missing
                                    '{"source_id": "SID", "kept": {"itm_x": {}}, "drops": []}'])
def test_damaged_ledger_is_refused_never_overwritten(tmp_path, damage):
    import validate_tag_links
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item("Kept"), _item("Other")]})
    _keep(tmp_path, a, "Kept")
    path = _ledger_file(tmp_path, a)
    damage = damage.replace("SID", a)
    path.write_text(damage, encoding="utf-8")

    for out in (_keep(tmp_path, a, "Other"), _drop(tmp_path, a, "Kept")):
        assert out["outcome"] == "blocked" and out["reason"] == "ledger_corrupt"
    gconn = _g(tmp_path)
    try:
        added = human_add.add_candidate(gconn, root=tmp_path, source_id=a,
                                        item_type="method_technique", title="Brand New",
                                        wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews")
    finally:
        gconn.close()
    assert added["outcome"] == "blocked" and added["reason"] == "ledger_corrupt"
    assert _node(tmp_path, "Brand New") is None                    # nothing written
    summary = _extract(tmp_path, {"alpha": [_item("Kept"), _item("Other")]}, force=True)
    assert [e["source_id"] for e in summary["error_details"]] == [a]
    assert summary["error_details"][0]["error"].startswith("ledger_corrupt")
    assert path.read_text(encoding="utf-8") == damage              # never overwritten
    assert _view(tmp_path, a)["ledger_error"]
    assert validate_tag_links.main([str(tmp_path)]) == 1


def test_damaged_ledger_blocks_a_merge(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    _extract(tmp_path, {"alpha": [_item("Survivor"), _item("Absorbed")], "beta": [_item("Noise")]})
    _keep(tmp_path, sids["a.md"], "Survivor")
    _keep(tmp_path, sids["a.md"], "Absorbed")
    _keep(tmp_path, sids["b.md"], "Noise")
    _ledger_file(tmp_path, sids["b.md"]).write_text("{broken", encoding="utf-8")
    _approve_merge(tmp_path, items.node_id("Survivor"), items.node_id("Absorbed"))
    gconn = _g(tmp_path)
    try:
        out = tagging.close_journals(tmp_path / "reviews", merges.apply_merges(
            gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki"))
    finally:
        gconn.close()
    assert out["applied"] == 0 and out["skipped"][0]["reason"] == "tag_ledger_corrupt"
    assert _node(tmp_path, "Absorbed")["status"] == "active"


def test_interrupted_drop_is_detected_and_repaired(tmp_path, monkeypatch):
    import rebuild_tag_links
    import validate_tag_links
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _extract(tmp_path, {"alpha": [_item("Shared")], "beta": [_item("Shared")]})
    _keep(tmp_path, a, "Shared")
    _keep(tmp_path, b, "Shared")

    def crash(*args, **kwargs):
        raise RuntimeError("power cut")

    monkeypatch.setattr(graph, "set_status", crash)
    gconn = _g(tmp_path)
    with pytest.raises(RuntimeError):
        tag_links.drop(gconn, source_id=a, node_id=items.node_id("Shared"),
                       wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews")
    gconn.close()
    monkeypatch.undo()
    # The ledger (written first) says dropped; the graph mirror still says kept.
    assert items.node_id("Shared") not in tag_links.load_ledger(tmp_path / "reviews", a)["kept"]
    assert _active_links(tmp_path, a, "Shared")
    assert validate_tag_links.main([str(tmp_path)]) == 1
    assert rebuild_tag_links.main([str(tmp_path)]) == 0                  # dry run
    assert _active_links(tmp_path, a, "Shared")
    assert rebuild_tag_links.main([str(tmp_path), "--apply"]) == 0
    assert _active_links(tmp_path, a, "Shared") == []
    assert validate_tag_links.main([str(tmp_path)]) == 0
    assert _titles(_view(tmp_path, b)["kept"]) == ["Shared"]             # b untouched
    _validators_green(tmp_path)


@pytest.mark.parametrize("dropped", ["Alpha Tool", "Beta Tool"])
def test_merge_never_restores_a_dropped_suggestion_at_another_span(tmp_path, dropped):
    # D dropped one side and still suggests the other, at a DIFFERENT evidence span (each name is
    # located where it occurs). Merging Beta Tool into Alpha Tool must leave D untagged by the
    # survivor — the per-document drop wins whatever the anchors.
    _build(tmp_path, {"d.md": "delta Alpha Tool and later Beta Tool",
                      "e.md": "epsilon Alpha Tool and Beta Tool"})
    sids = _sids(tmp_path)
    d, e = sids["d.md"], sids["e.md"]
    _extract(tmp_path, {"delta": [_item("Alpha Tool"), _item("Beta Tool")],
                        "epsilon": [_item("Alpha Tool"), _item("Beta Tool")]})
    _keep(tmp_path, e, "Alpha Tool")
    _keep(tmp_path, e, "Beta Tool")
    _drop(tmp_path, d, dropped)
    _approve_merge(tmp_path, items.node_id("Alpha Tool"), items.node_id("Beta Tool"))
    gconn = _g(tmp_path)
    try:
        out = tagging.close_journals(tmp_path / "reviews", merges.apply_merges(
            gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki"))
    finally:
        gconn.close()
    assert out["applied"] == 1
    tag_links.finish(tmp_path, out["affected_sources"], wiki_dir=tmp_path / "wiki",
                     pages_changed=True, templates_dir=TEMPLATES)
    assert _active_links(tmp_path, d, "Alpha Tool") == []
    assert _view(tmp_path, d)["suggestions"] == []
    drops = tag_links.load_ledger(tmp_path / "reviews", d)["drops"]
    assert [x["item_id"] for x in drops] == [items.node_id("Alpha Tool")]
    assert _titles(_view(tmp_path, e)["kept"]) == ["Alpha Tool"]
    gconn = _g(tmp_path)
    try:
        assert tag_links.consistency_errors(gconn, tmp_path / "reviews") == []
    finally:
        gconn.close()
    _validators_green(tmp_path)


def test_renamed_then_dropped_tag_is_not_resuggested_under_its_original_name(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item("Large Language Models")]})
    assert _keep(tmp_path, a, "Large Language Models", title="LLM")["effect"] == "activated"
    assert _drop(tmp_path, a, "Large Language Models")["effect"] == "unused"
    _rewrite_md(tmp_path, a, "alpha body, revised")   # a fresh response (not the cached one)
    summary = _extract(tmp_path, {"alpha": [_item("Large Language Models"), _item("LLM")]},
                       force=True)
    assert summary["suggestions_filtered_dropped"] == 2       # the original wording AND the new title
    assert _view(tmp_path, a)["suggestions"] == []
    artifact = json.loads((tmp_path / "normalized" / "enrichment" / f"{a}.items.json").read_text())
    assert sorted(f["reason"] for f in artifact["filtered"]) == ["dropped", "dropped"]


def test_immediate_actions_keep_the_keyword_index_consistent(tmp_path):
    from app.backend import keyword_index
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _extract(tmp_path, {"alpha": [_item("Old Name"), _item("Gone")], "beta": [_item("Kept Here")]})
    keyword_index.reindex(tmp_path)

    def consistent():
        conn = keyword_index.connect(tmp_path / keyword_index.DB_RELPATH)
        try:
            return keyword_index.consistency_errors(tmp_path, conn)
        finally:
            conn.close()

    assert consistent() == []
    _keep(tmp_path, a, "Old Name", title="New Name")             # amended title
    assert consistent() == []
    _keep(tmp_path, b, "Kept Here")
    assert _drop(tmp_path, b, "Kept Here")["effect"] == "unused"  # status change
    assert consistent() == []
    assert _drop(tmp_path, a, "Gone")["effect"] == "deleted"      # page removed
    assert consistent() == []


def test_failed_index_refresh_is_shown_not_swallowed(client, tmp_path, monkeypatch):
    from app.backend import keyword_index
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item("Tag")]})

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(keyword_index, "reindex", broken)
    resp = client.post(f"/ui/reviews/sources/{a}/decide",
                       data={f"link_{items.node_id('Tag')}": "keep"})
    assert resp.status_code == 200 and "Pages or search index not refreshed" in resp.text
    assert "keyword_reindex_failed:OSError" in resp.text
    assert _titles(_view(tmp_path, a)["kept"]) == ["Tag"]          # the decision itself stands
    resp = client.post(f"/reviews/sources/{a}/links",
                       json={"node_id": items.node_id("Tag"), "action": "drop"})
    assert resp.json()["warnings"] == ["keyword_reindex_failed:OSError"]


def test_unblock_finds_documents_extracted_while_blocked_and_cache_only_replays(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    routes = {"alpha": [_item("Hype Word"), _item("Fine")], "beta": [_item("Hype Word")]}
    _extract(tmp_path, routes, source_ids=[a])
    _block(tmp_path, "Hype Word")                     # withdraws a's suggestion
    _extract(tmp_path, routes, force=True)            # extracted while blocked: filtered, recorded
    artifact = json.loads((tmp_path / "normalized" / "enrichment" / f"{b}.items.json").read_text())
    assert artifact["nodes"] == [] and artifact["filtered"][0]["reason"] == "blocked"
    res = tag_links.unblock(key=tag_links.tag_key("Hype Word"), policies_dir=tmp_path / "policies",
                            enrichment_dir=tmp_path / "normalized" / "enrichment",
                            reviews_dir=tmp_path / "reviews")
    assert res["resuggest_sources"] == [a, b]

    adapter = CountingAdapter(routes)
    client = LLMClient({"anthropic": adapter},
                       cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite"))
    _rewrite_md(tmp_path, b, "beta body changed")     # b's stored response no longer matches
    out = items.extract_items(tmp_path, client=client, model_ref=MODEL_REF, force=True,
                              source_ids=res["resuggest_sources"], cache_only=True,
                              jobs_db=tmp_path / "db" / "jobs.sqlite")
    assert adapter.calls == 0                         # zero provider calls, hit or miss
    assert out["cache_miss_sources"] == [b] and out["status"] == "partial"
    wiki.generate_wiki(tmp_path, templates_dir=TEMPLATES, rebuild_index=False, record_job=False)
    assert _titles(_view(tmp_path, a)["suggestions"]) == ["Fine", "Hype Word"]   # replayed
    assert _view(tmp_path, b)["suggestions"] == []                                 # left untouched


def test_cache_only_parse_never_calls_the_provider(tmp_path):
    from app.llm.client import CacheMiss
    adapter = CountingAdapter({})
    client = LLMClient({"anthropic": adapter}, cache=ResponseCache(tmp_path / "c.sqlite"))
    with pytest.raises(CacheMiss):
        client.parse([{"role": "user", "content": "x"}], {"type": "object"}, MODEL_REF,
                     cache_only=True)
    no_cache = LLMClient({"anthropic": adapter})
    with pytest.raises(CacheMiss):
        no_cache.parse([{"role": "user", "content": "x"}], {"type": "object"}, MODEL_REF,
                       cache_only=True)
    assert adapter.calls == 0


def test_amendments_are_validated_on_both_http_routes(client, tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item("Amend Me"), _item("Form Row")]})
    nid = items.node_id("Amend Me")
    url = f"/reviews/sources/{a}/links"
    for bad in ({"aliases": "a,b"}, {"item_type": "unclassified_review_required"},
                {"item_type": "bogus"}, {"title": 7}, {"description": ["x"]}):
        resp = client.post(url, json={"node_id": nid, "action": "keep", **bad})
        assert resp.status_code == 400, bad
    assert _node(tmp_path, "Amend Me")["status"] == "candidate"            # nothing written
    resp = client.post(url, json={"node_id": nid, "action": "keep", "title": "T" * 300,
                                  "aliases": [" x ", ""]})
    assert resp.status_code == 200
    fm = parse_frontmatter(next((tmp_path / "wiki" / "Items").glob("t*.md")).read_text())
    assert fm["title"] == "T" * 200 and "x" in fm["aliases"] and "" not in fm["aliases"]

    form_nid = items.node_id("Form Row")
    resp = client.post(f"/ui/reviews/sources/{a}/decide",
                       data={f"link_{form_nid}": "keep", f"amend_item_type_{form_nid}": "bogus"})
    assert resp.status_code == 200 and "invalid_amendments" in resp.text
    assert _node(tmp_path, "Form Row")["status"] == "candidate"


# --- review round 3 (2026-10-25) ------------------------------------------------------------------


def _approve_split(tmp_path, primary, spinoff_name, sources, rid="rev_s"):
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": "split_item", "status": "approved",
        "subject": {"node_id": items.node_id(primary), "spinoff_node_id": items.node_id(spinoff_name)},
        "proposal": {"spinoff_name": spinoff_name, "spinoff_sources": list(sources),
                     "spinoff_aliases": []}}), encoding="utf-8")


def _run(tmp_path, executor):
    gconn = _g(tmp_path)
    try:
        out = executor(gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")
        gconn.commit()
    finally:
        gconn.close()
    tag_links.finish(tmp_path, out["affected_sources"], wiki_dir=tmp_path / "wiki",
                     pages_changed=True, templates_dir=TEMPLATES)
    return tagging.close_journals(tmp_path / "reviews", out)     # the caller's refreshes succeeded


def _consistent(tmp_path):
    gconn = _g(tmp_path)
    try:
        return tag_links.consistency_errors(gconn, tmp_path / "reviews")
    finally:
        gconn.close()


def test_repair_never_touches_a_document_whose_ledger_is_damaged(tmp_path):
    import rebuild_tag_links
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _extract(tmp_path, {"alpha": [_item("Precious")], "beta": [_item("Other")]})
    _keep(tmp_path, a, "Precious")
    _keep(tmp_path, b, "Other")
    gconn = _g(tmp_path)
    gconn.execute("DELETE FROM edges WHERE src_id = ? AND asserted_by = 'human'", (b,))
    gconn.commit()
    gconn.close()
    _ledger_file(tmp_path, a).write_text("{damaged", encoding="utf-8")
    assert rebuild_tag_links.main([str(tmp_path), "--apply"]) == 1      # reports the damage
    assert _active_links(tmp_path, a, "Precious")                       # a's kept link untouched
    assert "human" in [r["asserted_by"] for r in _active_links(tmp_path, b, "Other")]  # b repaired


def test_interrupted_first_keep_is_finished_with_the_chosen_type(tmp_path, monkeypatch):
    import validate_tag_links
    from app.workers import promote
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item("Vague", item_type="unclassified_review_required")]})
    real = promote.activate_node

    def crash_on_write(*args, **kwargs):
        if kwargs.get("dry_run"):
            return real(*args, **kwargs)
        raise RuntimeError("power cut")

    monkeypatch.setattr(promote, "activate_node", crash_on_write)
    gconn = _g(tmp_path)
    with pytest.raises(RuntimeError):
        tag_links.keep(gconn, source_id=a, node_id=items.node_id("Vague"), wiki_dir=tmp_path / "wiki",
                       reviews_dir=tmp_path / "reviews",
                       amendments={"item_type": "use_case", "title": "Vague Idea"})
    gconn.close()
    monkeypatch.undo()
    entry = tag_links.load_ledger(tmp_path / "reviews", a)["kept"][items.node_id("Vague")]
    assert entry["amendments"] == {"item_type": "use_case", "title": "Vague Idea"}   # intent recorded
    assert _node(tmp_path, "Vague")["status"] == "candidate"
    assert any("interrupted keep" in e for e in _consistent(tmp_path))
    assert validate_tag_links.main([str(tmp_path)]) == 1
    gconn = _g(tmp_path)
    try:
        out = tag_links.repair(gconn, reviews_dir=tmp_path / "reviews", wiki_dir=tmp_path / "wiki",
                               apply=True)
    finally:
        gconn.close()
    assert out["interrupted_activations"] == 1 and out["activation_blocked"] == []
    tag_links.finish(tmp_path, out["affected_sources"], wiki_dir=tmp_path / "wiki",
                     templates_dir=TEMPLATES)             # what rebuild_tag_links.py runs next
    node = _node(tmp_path, "Vague")
    assert node["status"] == "active" and node["item_type"] == "use_case"
    assert _titles(_view(tmp_path, a)["kept"]) == ["Vague Idea"]
    assert _consistent(tmp_path) == []


def test_interrupted_revival_of_an_unused_tag_is_finished(tmp_path, monkeypatch):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _extract(tmp_path, {"alpha": [_item("Phoenix")], "beta": [_item("Phoenix")]})
    _keep(tmp_path, a, "Phoenix")
    assert _drop(tmp_path, a, "Phoenix")["effect"] == "unused"

    def crash(*args, **kwargs):
        raise RuntimeError("power cut")

    monkeypatch.setattr(tag_links, "_refresh_page", crash)
    gconn = _g(tmp_path)
    with pytest.raises(RuntimeError):
        tag_links.keep(gconn, source_id=b, node_id=items.node_id("Phoenix"),
                       wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews")
    gconn.close()
    monkeypatch.undo()
    assert _node(tmp_path, "Phoenix")["status"] == "unused"      # the human link is written, no revival
    assert any("interrupted revival" in e for e in _consistent(tmp_path))
    gconn = _g(tmp_path)
    try:
        out = tag_links.repair(gconn, reviews_dir=tmp_path / "reviews", wiki_dir=tmp_path / "wiki",
                               apply=True)
    finally:
        gconn.close()
    assert out["interrupted_revivals"] == 1
    tag_links.finish(tmp_path, out["affected_sources"], wiki_dir=tmp_path / "wiki",
                     templates_dir=TEMPLATES)
    assert _node(tmp_path, "Phoenix")["status"] == "active"
    assert _consistent(tmp_path) == []


def test_a_drop_or_block_during_the_model_call_wins_over_the_extraction(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item("Dropped Now"), _item("Blocked Now"), _item("Fine")]})
    _rewrite_md(tmp_path, a, "alpha body, revised")    # a fresh (uncached) model call follows

    class DecidingDuringCall(RoutingAdapter):
        def parse(self, messages, schema, model_id, *, max_tokens):
            # the user acts while the (slow) model call is in flight — no lock is held here
            _drop(tmp_path, a, "Dropped Now")
            _block(tmp_path, "Blocked Now")
            return super().parse(messages, schema, model_id, max_tokens=max_tokens)

    adapter = DecidingDuringCall({"alpha": [_item("Dropped Now"), _item("Blocked Now"), _item("Fine")]})
    out = items.extract_items(tmp_path, client=LLMClient(
        {"anthropic": adapter}, cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite")),
        model_ref=MODEL_REF, jobs_db=tmp_path / "db" / "jobs.sqlite", force=True)
    assert out["suggestions_filtered_dropped"] == 1 and out["suggestions_filtered_blocked"] == 1
    wiki.generate_wiki(tmp_path, templates_dir=TEMPLATES, rebuild_index=False, record_job=False)
    assert _titles(_view(tmp_path, a)["suggestions"]) == ["Fine"]
    assert _consistent(tmp_path) == []


def _merge_fixture(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _extract(tmp_path, {"alpha": [_item("Survivor"), _item("Absorbed")], "beta": [_item("Absorbed")]})
    _keep(tmp_path, a, "Survivor")
    _keep(tmp_path, b, "Absorbed")
    _approve_merge(tmp_path, items.node_id("Survivor"), items.node_id("Absorbed"))
    return a, b


@pytest.mark.parametrize("fail_at", ["repoint", "survivor_render"])
def test_interrupted_merge_is_deferred_by_repair_and_resumed_by_apply(tmp_path, monkeypatch, fail_at):
    import rebuild_tag_links
    from app.workers import identity_journal
    a, b = _merge_fixture(tmp_path)

    def crash(*args, **kwargs):
        raise RuntimeError("power cut")

    if fail_at == "repoint":
        monkeypatch.setattr(graph, "repoint_edge", crash)
    else:
        monkeypatch.setattr(merges, "_render_survivor", crash)
    gconn = _g(tmp_path)
    with pytest.raises(RuntimeError):
        merges.apply_merges(gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")
    gconn.close()
    monkeypatch.undo()
    # ledger first: b's keep already moved to the survivor; the journal marks the merge in flight
    assert list(tag_links.load_ledger(tmp_path / "reviews", b)["kept"]) == [items.node_id("Survivor")]
    assert identity_journal.is_open(tmp_path / "reviews", "rev_m")
    errors = _consistent(tmp_path)
    assert errors and all(("in flight" in e) for e in errors)        # nodes deferred, not "broken"
    before = _active_links(tmp_path, b, "Absorbed") + _active_links(tmp_path, b, "Survivor")
    assert rebuild_tag_links.main([str(tmp_path), "--apply"]) == 1      # refuses to touch them
    assert _active_links(tmp_path, b, "Absorbed") + _active_links(tmp_path, b, "Survivor") == before

    out = _run(tmp_path, merges.apply_merges)                          # the next apply resumes
    assert out["applied"] == 1
    assert not identity_journal.is_open(tmp_path / "reviews", "rev_m")
    assert _node(tmp_path, "Absorbed")["status"] == "merged"
    assert _titles(_view(tmp_path, b)["kept"]) == ["Survivor"]
    assert _consistent(tmp_path) == []
    assert _run(tmp_path, merges.apply_merges)["applied"] == 0          # then a true no-op
    _validators_green(tmp_path)


def test_interrupted_split_is_resumed_by_apply(tmp_path, monkeypatch):
    from app.workers import identity_journal, splits
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body", "c.md": "gamma body"})
    sids = _sids(tmp_path)
    c = sids["c.md"]
    _extract(tmp_path, {"alpha": [_item("Mercury")], "beta": [_item("Mercury")],
                        "gamma": [_item("Mercury")]})
    for f in ("a.md", "b.md", "c.md"):
        _keep(tmp_path, sids[f], "Mercury")
    _approve_split(tmp_path, "Mercury", "Mercury Planet", [c])
    real = graph.repoint_edge
    calls = {"n": 0}

    def crash_second(*args, **kwargs):          # c has two links (suggestion + keep): move one, die
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("power cut")
        return real(*args, **kwargs)

    monkeypatch.setattr(graph, "repoint_edge", crash_second)
    gconn = _g(tmp_path)
    with pytest.raises(RuntimeError):
        splits.apply_splits(gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")
    gconn.close()
    monkeypatch.undo()
    assert identity_journal.is_open(tmp_path / "reviews", "rev_s")
    out = _run(tmp_path, splits.apply_splits)
    assert out["applied"] == 1 and not identity_journal.is_open(tmp_path / "reviews", "rev_s")
    assert _titles(_view(tmp_path, c)["kept"]) == ["Mercury Planet"]
    assert _node(tmp_path, "Mercury Planet")["status"] == "active"
    assert _node(tmp_path, "Mercury")["status"] == "active"
    assert _consistent(tmp_path) == []
    _validators_green(tmp_path)


def test_a_keep_waits_for_an_in_progress_merge(tmp_path, monkeypatch):
    import threading
    import time
    from app.workers import identity_journal
    a, b = _merge_fixture(tmp_path)
    _extract(tmp_path, {"alpha": [_item("Survivor"), _item("Absorbed")], "beta": [_item("Absorbed")]})
    order: list[str] = []
    started = threading.Event()
    real_repoint, real_stage = graph.repoint_edge, identity_journal.set_stage

    def slow_repoint(*args, **kwargs):
        started.set()
        time.sleep(0.3)
        return real_repoint(*args, **kwargs)

    def recording_stage(*args, **kwargs):
        order.append("merge_finished")
        return real_stage(*args, **kwargs)

    monkeypatch.setattr(graph, "repoint_edge", slow_repoint)
    monkeypatch.setattr(identity_journal, "set_stage", recording_stage)

    def run_merge():
        gconn = _g(tmp_path)
        try:
            merges.apply_merges(gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")
            gconn.commit()
        finally:
            gconn.close()

    t = threading.Thread(target=run_merge)
    t.start()
    assert started.wait(5)
    gconn = _g(tmp_path)
    try:
        out = tag_links.keep(gconn, source_id=a, node_id=items.node_id("Absorbed"),
                             wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews")
    finally:
        gconn.close()
    order.append("keep_returned")
    t.join()
    assert order == ["merge_finished", "keep_returned"]      # the keep ran after the whole merge
    assert out["outcome"] == "blocked" and out["reason"] == "item_not_linkable"   # B is merged now


# --- review round 4 (2026-10-25) ------------------------------------------------------------------


def _interrupt_merge(tmp_path, monkeypatch):
    """The merge fixture, interrupted after the ledger moved b's keep to the survivor."""
    def crash(*args, **kwargs):
        raise RuntimeError("power cut")

    # A scoped patch: `monkeypatch.undo()` would also undo the client fixture's settings patch.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(graph, "repoint_edge", crash)
        gconn = _g(tmp_path)
        with pytest.raises(RuntimeError):
            merges.apply_merges(gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")
        gconn.close()


def test_tag_actions_on_an_unfinished_merge_are_refused(client, tmp_path, monkeypatch):
    a, b = _merge_fixture(tmp_path)
    _interrupt_merge(tmp_path, monkeypatch)
    absorbed, survivor = items.node_id("Absorbed"), items.node_id("Survivor")
    ledger_before = _ledger_file(tmp_path, b).read_text(encoding="utf-8")

    screen = client.get(f"/ui/reviews/sources/{b}").text
    assert "A merge/split touching this document is unfinished" in screen and "rev_m" in screen
    resp = client.post(f"/ui/reviews/sources/{b}/decide", data={f"link_{absorbed}": "drop"})
    assert resp.status_code == 200 and "identity_operation_in_flight" in resp.text
    resp = client.post(f"/reviews/sources/{b}/links", json={"node_id": absorbed, "action": "drop"})
    assert resp.status_code == 409 and resp.json()["detail"] == "identity_operation_in_flight"
    assert _ledger_file(tmp_path, b).read_text(encoding="utf-8") == ledger_before   # nothing written

    gconn = _g(tmp_path)
    try:
        added = human_add.add_candidate(gconn, root=tmp_path, source_id=a,
                                        item_type="method_technique", title="Absorbed",
                                        wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews")
        blocked = tag_links.block(gconn, name="Survivor", policies_dir=tmp_path / "policies",
                                  wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews")
    finally:
        gconn.close()
    assert added["reason"] == "identity_operation_in_flight" and added["review_id"] == "rev_m"
    assert blocked["reason"] == "identity_operation_in_flight"
    assert tag_links.load_blocked(tmp_path / "policies") == {}                   # not even listed

    # The next apply finishes the merge; then the same actions work again.
    out = _run(tmp_path, merges.apply_merges)
    assert out["applied"] == 1
    assert _titles(_view(tmp_path, b)["kept"]) == ["Survivor"]
    assert _drop(tmp_path, b, "Survivor")["outcome"] == "dropped"
    assert _consistent(tmp_path) == []
    assert survivor not in tag_links.load_ledger(tmp_path / "reviews", b)["kept"]


def test_delete_unused_skips_an_unfinished_merge_survivor(tmp_path, monkeypatch):
    a, b = _merge_fixture(tmp_path)
    assert _drop(tmp_path, a, "Survivor")["effect"] == "unused"          # an unused survivor
    _interrupt_merge(tmp_path, monkeypatch)
    out = _call(tmp_path, lambda gconn, **kw: tag_links.delete_unused(gconn, **kw))
    assert out["deleted"] == []
    assert out["skipped"] == [{"node_id": items.node_id("Survivor"),
                               "reason": "identity_operation_in_flight"}]
    assert _node(tmp_path, "Survivor")["status"] == "unused"


def test_extraction_skips_documents_of_an_unfinished_merge(tmp_path, monkeypatch):
    a, b = _merge_fixture(tmp_path)
    _interrupt_merge(tmp_path, monkeypatch)
    adapter = CountingAdapter({"alpha": [_item("Survivor")], "beta": [_item("Absorbed")]})
    _rewrite_md(tmp_path, b, "beta body, revised")
    out = items.extract_items(tmp_path, client=LLMClient(
        {"anthropic": adapter}, cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite")),
        model_ref=MODEL_REF, jobs_db=tmp_path / "db" / "jobs.sqlite", force=True)
    assert sorted(r["source_id"] for r in out["in_flight_sources"]) == sorted([a, b])
    assert adapter.calls == 0                        # skipped before the (billable) model call
    assert out["status"] == "partial"


def test_extraction_rechecks_under_the_lock_after_the_model_call(tmp_path):
    from app.workers import identity_journal
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]

    class OperationStartsDuringCall(RoutingAdapter):
        def parse(self, messages, schema, model_id, *, max_tokens):
            identity_journal.begin(tmp_path / "reviews", "rev_x", op="merge_items",
                                   nodes=[items.node_id("Contested")])
            return super().parse(messages, schema, model_id, max_tokens=max_tokens)

    adapter = OperationStartsDuringCall({"alpha": [_item("Contested"), _item("Fine")]})
    out = items.extract_items(tmp_path, client=LLMClient(
        {"anthropic": adapter}, cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite")),
        model_ref=MODEL_REF, jobs_db=tmp_path / "db" / "jobs.sqlite")
    assert out["in_flight_sources"] == [{"source_id": a, "review_id": "rev_x"}]
    assert _node(tmp_path, "Contested") is None and _node(tmp_path, "Fine") is None   # nothing written
    assert not (tmp_path / "normalized" / "enrichment" / f"{a}.items.json").exists()


def test_actions_on_an_unfinished_split_are_refused(tmp_path, monkeypatch):
    from app.workers import splits
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body", "c.md": "gamma body"})
    sids = _sids(tmp_path)
    for f in ("a.md", "b.md", "c.md"):
        _extract(tmp_path, {"alpha": [_item("Mercury")], "beta": [_item("Mercury")],
                            "gamma": [_item("Mercury")]}, source_ids=[sids[f]])
        _keep(tmp_path, sids[f], "Mercury")
    _approve_split(tmp_path, "Mercury", "Mercury Planet", [sids["c.md"]])

    def crash(*args, **kwargs):
        raise RuntimeError("power cut")

    monkeypatch.setattr(graph, "repoint_edge", crash)
    gconn = _g(tmp_path)
    with pytest.raises(RuntimeError):
        splits.apply_splits(gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")
    gconn.close()
    monkeypatch.undo()
    gconn = _g(tmp_path)
    try:
        added = human_add.add_candidate(gconn, root=tmp_path, source_id=sids["a.md"],
                                        item_type="method_technique", title="Mercury Planet",
                                        wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews")
    finally:
        gconn.close()
    assert added["reason"] == "identity_operation_in_flight" and added["review_id"] == "rev_s"
    assert _drop(tmp_path, sids["a.md"], "Mercury")["reason"] == "identity_operation_in_flight"
    out = _extract(tmp_path, {"alpha": [_item("Mercury")]}, force=True, source_ids=[sids["a.md"]])
    assert out["in_flight_sources"] == [{"source_id": sids["a.md"], "review_id": "rev_s"}]
    assert _run(tmp_path, splits.apply_splits)["applied"] == 1
    assert _titles(_view(tmp_path, sids["c.md"])["kept"]) == ["Mercury Planet"]
    assert _consistent(tmp_path) == []


@pytest.mark.parametrize("fail_at", ["source_pages", "keyword_index"])
def test_apply_keeps_the_marker_until_its_refreshes_succeed(client, tmp_path, monkeypatch, fail_at):
    import validate_projection
    from app.workers import identity_journal
    a, b = _merge_fixture(tmp_path)

    def crash(*args, **kwargs):
        raise RuntimeError("crash during refresh")

    with pytest.MonkeyPatch.context() as mp:     # scoped: keep the client fixture's settings patch
        if fail_at == "source_pages":
            mp.setattr(main_module.wiki, "generate_wiki", crash)
            with pytest.raises(RuntimeError):
                client.post("/reviews/apply")
        else:
            mp.setattr(main_module.retention, "reindex_keyword", crash)
            body = client.post("/reviews/apply").json()
            assert body["status"] == "validation_failed"
            assert "identity_refresh_pending" in body["warnings"]
    # The executor finished (B tombstoned) but the marker survives, so the refresh is still owed.
    assert _node(tmp_path, "Absorbed")["status"] == "merged"
    assert identity_journal.is_open(tmp_path / "reviews", "rev_m")
    assert _drop(tmp_path, b, "Survivor")["reason"] == "identity_operation_in_flight"

    body = client.post("/reviews/apply").json()                       # resumes + refreshes
    assert body["status"] == "applied", body.get("failed_validators")
    assert body["summary"]["merged"]["applied"] == 1
    assert not identity_journal.is_open(tmp_path / "reviews", "rev_m")
    assert validate_projection.main([str(tmp_path)]) == 0            # Source pages re-rendered
    assert _consistent(tmp_path) == []
    assert client.post("/reviews/apply").json()["summary"]["merged"]["applied"] == 0   # done


# --- review round 5 (2026-10-25) ------------------------------------------------------------------


def _fail_rendering(mp, sid):
    """Make ONE Source page's renderer fail inside generate_wiki (which reports, never raises)."""
    from app.workers import wiki_render
    real = wiki_render.render_source_page

    def flaky(template, manifest, *args, **kwargs):
        if manifest.get("source_id") == sid:
            raise RuntimeError("template glitch")
        return real(template, manifest, *args, **kwargs)

    mp.setattr(wiki_render, "render_source_page", flaky)


def test_a_reported_source_page_failure_keeps_the_marker(client, tmp_path):
    import validate_projection
    from app.workers import identity_journal
    a, b = _merge_fixture(tmp_path)
    with pytest.MonkeyPatch.context() as mp:     # scoped: keep the client fixture's settings patch
        _fail_rendering(mp, b)
        body = client.post("/reviews/apply").json()
    assert body["summary"]["merged"]["applied"] == 1
    assert body["status"] == "validation_failed"
    assert {"source_page_refresh_failed", "identity_refresh_pending"} <= set(body["warnings"])
    assert identity_journal.is_open(tmp_path / "reviews", "rev_m")      # the refresh is still owed
    assert validate_projection.main([str(tmp_path)]) == 1                # b's page is stale

    body = client.post("/reviews/apply").json()                         # resumes + refreshes
    assert body["status"] == "applied", body.get("failed_validators")
    assert not identity_journal.is_open(tmp_path / "reviews", "rev_m")
    assert validate_projection.main([str(tmp_path)]) == 0


def test_a_reported_source_page_failure_after_a_keep_is_shown(client, tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item("One"), _item("Two")]})
    with pytest.MonkeyPatch.context() as mp:
        _fail_rendering(mp, a)
        resp = client.post(f"/ui/reviews/sources/{a}/decide",
                           data={f"link_{items.node_id('One')}": "keep"})
        assert resp.status_code == 200 and f"source_page_refresh_failed:{a}" in resp.text
        resp = client.post(f"/reviews/sources/{a}/links",
                           json={"node_id": items.node_id("Two"), "action": "keep"})
        assert resp.json()["warnings"] == [f"source_page_refresh_failed:{a}"]
    assert _titles(_view(tmp_path, a)["kept"]) == ["One", "Two"]          # the decisions stand


# --- review round 6: kept-only projections + recovery of derived state ---------------------------


def _page(tmp_path, rel):
    return (tmp_path / "wiki" / rel).read_text(encoding="utf-8")


def _repair(tmp_path):
    gconn = _g(tmp_path)
    try:
        out = tag_links.repair(gconn, reviews_dir=tmp_path / "reviews", wiki_dir=tmp_path / "wiki",
                               policies_dir=tmp_path / "policies", apply=True)
    finally:
        gconn.close()
    tag_links.finish(tmp_path, out["affected_sources"], wiki_dir=tmp_path / "wiki",
                     pages_changed=True, templates_dir=TEMPLATES)     # what rebuild_tag_links.py runs
    return out


def _crash(*args, **kwargs):
    raise RuntimeError("power cut")


def test_an_unkept_suggestion_never_shows_as_a_document_tag(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _extract(tmp_path, {"alpha": [_item("Topic")], "beta": [_item("Topic")]})
    _keep(tmp_path, a, "Topic")                        # Topic is now active, kept on A only
    slug = _node(tmp_path, "Topic")["slug"]
    page_a, page_b = _page(tmp_path, f"Sources/{a}.md"), _page(tmp_path, f"Sources/{b}.md")
    assert f"[[Items/{slug}|" in page_a and parse_frontmatter(page_a)["items"] == [slug]
    assert f"[[Items/{slug}" not in page_b and parse_frontmatter(page_b)["items"] == []
    assert "### Suggested (not reviewed)" in page_b and "- Topic" in page_b
    item = _page(tmp_path, f"Items/{slug}.md")
    assert f"[[Sources/{a}|" in item and f"[[Sources/{b}" not in item     # backlinks: kept only
    assert "## Suggested On" in item and "mentioned by 1 source(s)" in item
    _validators_green(tmp_path)
    assert _consistent(tmp_path) == []


def test_a_legacy_tag_links_its_documents_until_its_first_keep(tmp_path):
    from app.workers import promote
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _extract(tmp_path, {"alpha": [_item("Topic")], "beta": [_item("Topic")]})
    gconn = _g(tmp_path)
    try:                                               # the flat-queue promotion: no keep anywhere
        promote.activate_node(gconn, node=graph.get_node(gconn, items.node_id("Topic")),
                              wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews")
    finally:
        gconn.close()
    tag_links.finish(tmp_path, [a, b], wiki_dir=tmp_path / "wiki", templates_dir=TEMPLATES)
    slug = _node(tmp_path, "Topic")["slug"]
    for sid in (a, b):
        assert f"[[Items/{slug}|" in _page(tmp_path, f"Sources/{sid}.md")
    _validators_green(tmp_path)
    _keep(tmp_path, a, "Topic")                        # the first keep ends the legacy projection
    assert f"[[Items/{slug}" not in _page(tmp_path, f"Sources/{b}.md")
    assert f"[[Sources/{b}" not in _page(tmp_path, f"Items/{slug}.md")
    _validators_green(tmp_path)
    assert _consistent(tmp_path) == []


def test_an_interrupted_last_drop_is_repaired_to_unused(tmp_path, monkeypatch):
    import validate_tag_links
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item("Topic")]})
    _keep(tmp_path, a, "Topic")
    monkeypatch.setattr(tag_links, "_refresh_page", _crash)
    gconn = _g(tmp_path)
    with pytest.raises(RuntimeError):
        tag_links.drop(gconn, source_id=a, node_id=items.node_id("Topic"),
                       wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews")
    gconn.close()
    monkeypatch.undo()
    assert _node(tmp_path, "Topic")["status"] == "active"           # no keep left, still active
    errors = _consistent(tmp_path)
    assert any("but its keeps make it unused" in e for e in errors)
    assert any("Source page tag list is stale" in e for e in errors)
    assert validate_tag_links.main([str(tmp_path)]) == 1
    out = _repair(tmp_path)
    assert out["stale_status"] == 1
    slug = _node(tmp_path, "Topic")["slug"]
    assert _node(tmp_path, "Topic")["status"] == "unused"
    assert parse_frontmatter(_page(tmp_path, f"Items/{slug}.md"))["status"] == "unused"
    assert f"[[Items/{slug}" not in _page(tmp_path, f"Sources/{a}.md")
    _validators_green(tmp_path)
    assert _consistent(tmp_path) == []


def test_an_interrupted_drop_of_one_of_two_keeps_refreshes_the_pages(tmp_path, monkeypatch):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _extract(tmp_path, {"alpha": [_item("Topic")], "beta": [_item("Topic")]})
    _keep(tmp_path, a, "Topic")
    _keep(tmp_path, b, "Topic")
    monkeypatch.setattr(tag_links, "_refresh_page", _crash)
    gconn = _g(tmp_path)
    with pytest.raises(RuntimeError):
        tag_links.drop(gconn, source_id=a, node_id=items.node_id("Topic"),
                       wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews")
    gconn.close()
    monkeypatch.undo()
    errors = _consistent(tmp_path)
    assert any("Mentioned By does not list exactly" in e for e in errors)
    out = _repair(tmp_path)
    assert out["stale_item_pages"] == 1 and out["stale_source_pages"] == 1
    slug = _node(tmp_path, "Topic")["slug"]
    item = _page(tmp_path, f"Items/{slug}.md")
    assert f"[[Sources/{b}|" in item and f"[[Sources/{a}" not in item
    _validators_green(tmp_path)
    assert _consistent(tmp_path) == []


def test_an_interrupted_block_is_finished_by_repair(tmp_path, monkeypatch):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    _extract(tmp_path, {"alpha": [_item("Junk"), _item("Fine")], "beta": [_item("Junk")]})
    monkeypatch.setattr(graph, "set_status", _crash)               # stop right after the list write
    gconn = _g(tmp_path)
    with pytest.raises(RuntimeError):
        tag_links.block(gconn, name="Junk", policies_dir=tmp_path / "policies",
                        wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews")
    gconn.close()
    monkeypatch.undo()
    assert "Junk" in (tmp_path / "policies" / "blocked_tags.txt").read_text(encoding="utf-8")
    assert sum("interrupted block" in e for e in _consistent(tmp_path)) == 2
    out = _repair(tmp_path)
    assert out["unwithdrawn_blocks"] == 2
    assert _node(tmp_path, "Junk") is None                          # never kept → deleted, as a block does
    assert not (tmp_path / "wiki" / "Items" / "junk.md").exists()
    assert _node(tmp_path, "Fine")["status"] == "candidate"         # other suggestions untouched
    _validators_green(tmp_path)
    assert _consistent(tmp_path) == []


def test_an_interrupted_deletion_is_finished_by_repair(tmp_path, monkeypatch):
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item("Gone"), _item("Stays")]})
    monkeypatch.setattr(graph, "delete_node", _crash)              # the page is unlinked first
    gconn = _g(tmp_path)
    with pytest.raises(RuntimeError):
        tag_links.drop(gconn, source_id=a, node_id=items.node_id("Gone"),
                       wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews")
    gconn.close()
    monkeypatch.undo()
    assert _node(tmp_path, "Gone") is not None and not (tmp_path / "wiki" / "Items" / "gone.md").exists()
    assert (tmp_path / "reviews" / "tag_links" / "deleting" / f"{items.node_id('Gone')}.json").exists()
    assert any("was started and did not finish" in e for e in _consistent(tmp_path))
    out = _repair(tmp_path)
    assert out["interrupted_deletions"] == 1 and out["pages_changed"] is True
    assert tag_links.deletion_intents(tmp_path / "reviews") == {}
    assert _node(tmp_path, "Gone") is None and _node(tmp_path, "Stays") is not None
    _validators_green(tmp_path)
    assert _consistent(tmp_path) == []


def test_the_unused_tags_list_carries_the_item_type(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item("Topic", item_type="use_case")]})
    _keep(tmp_path, a, "Topic")
    _drop(tmp_path, a, "Topic")
    gconn = _g(tmp_path)
    try:
        rows = tag_links.unused_tags(gconn, tmp_path / "wiki")
    finally:
        gconn.close()
    assert [(r["title"], r["item_type"]) for r in rows] == [("Topic", "use_case")]


# --- review round 7: deletion needs a recorded intent; tag-page-only repairs refresh the index ----


def _unused(tmp_path, name):
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item(name)]})
    _keep(tmp_path, a, name)
    _drop(tmp_path, a, name)
    assert _node(tmp_path, name)["status"] == "unused"
    return a


def test_a_missing_page_is_reported_never_treated_as_a_deletion(tmp_path):
    import rebuild_tag_links
    _unused(tmp_path, "Lost")
    (tmp_path / "wiki" / "Items" / "lost.md").unlink()        # corruption / incomplete restore
    errors = _consistent(tmp_path)
    assert any("has no page wiki/Items/lost.md" in e and "restore it from backup" in e for e in errors)
    out = _repair(tmp_path)
    assert out["missing_pages"] == 1 and out["interrupted_deletions"] == 0
    assert _node(tmp_path, "Lost")["status"] == "unused"        # nothing deleted
    assert rebuild_tag_links.main([str(tmp_path), "--apply"]) == 1   # still reported, non-zero


def test_an_interrupted_delete_unused_resumes_from_its_record(tmp_path, monkeypatch):
    _unused(tmp_path, "Old")
    monkeypatch.setattr(graph, "delete_node", _crash)
    gconn = _g(tmp_path)
    with pytest.raises(RuntimeError):
        tag_links.delete_unused(gconn, wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews")
    gconn.close()
    monkeypatch.undo()
    assert _node(tmp_path, "Old") is not None and not (tmp_path / "wiki" / "Items" / "old.md").exists()
    record = tag_links.deletion_intents(tmp_path / "reviews")[items.node_id("Old")]
    assert record["reason"] == "delete_unused"
    out = _repair(tmp_path)
    assert out["interrupted_deletions"] == 1 and out["missing_pages"] == 0
    assert _node(tmp_path, "Old") is None
    _validators_green(tmp_path)
    assert _consistent(tmp_path) == []


def test_a_deletion_record_whose_tag_was_kept_meanwhile_deletes_nothing(tmp_path, monkeypatch):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _extract(tmp_path, {"alpha": [_item("Topic")], "beta": [_item("Topic")]})
    _keep(tmp_path, a, "Topic")
    _drop(tmp_path, a, "Topic")                               # unused; still suggested on B
    monkeypatch.setattr(graph, "delete_node", _crash)
    gconn = _g(tmp_path)
    with pytest.raises(RuntimeError):
        tag_links.delete_unused(gconn, wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews")
    gconn.close()
    monkeypatch.undo()
    gconn = _g(tmp_path)                                      # B keeps it before anyone repairs
    try:
        out = tag_links.keep(gconn, source_id=b, node_id=items.node_id("Topic"),
                             wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews")
    finally:
        gconn.close()
    assert out["outcome"] == "kept"
    assert items.node_id("Topic") in tag_links.load_ledger(tmp_path / "reviews", b)["kept"]
    assert any("no longer applies (kept)" in e for e in _consistent(tmp_path))
    out = _repair(tmp_path)
    assert out["cancelled_deletions"] == 1 and out["interrupted_deletions"] == 0
    assert _node(tmp_path, "Topic") is not None                 # kept → never deleted
    assert tag_links.deletion_intents(tmp_path / "reviews") == {}


def test_a_repair_of_only_an_unused_tag_page_refreshes_the_index(tmp_path, monkeypatch):
    import rebuild_tag_links
    _unused(tmp_path, "Quiet")
    page = tmp_path / "wiki" / "Items" / "quiet.md"
    page.write_text(page.read_text(encoding="utf-8").replace("status: unused", "status: active", 1),
                    encoding="utf-8")                         # a stale page, no document involved
    calls = []
    real_finish = tag_links.finish

    def spy(root, affected, **kw):
        calls.append((sorted(affected), kw.get("pages_changed")))
        return real_finish(root, affected, **kw)

    monkeypatch.setattr(tag_links, "finish", spy)
    assert rebuild_tag_links.main([str(tmp_path), "--apply"]) == 0
    assert calls == [([], True)]                              # index + keyword refresh still ran
    assert parse_frontmatter(page.read_text(encoding="utf-8"))["status"] == "unused"
