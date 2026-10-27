"""ADR-0066 part 2 review: the user's decisions outrank re-extraction and repair.

- After the first keep the aliases are the user's: extraction neither adds aliases nor proposes any,
  and a deleted alias stays deleted (and stops matching).
- Hidden tags, approved deprecations and merge tombstones survive extraction and repair; a suggestion
  naming a hidden/retired tag is filtered, one naming a merged tag follows the merge to its survivor.
- An interrupted "use X" stays visibly pending until the user retries (which finishes it without
  duplicating anything) or decides differently (which cancels it; a keep of X that ran stays).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pytest
import rebuild_index  # noqa: E402
from fastapi.testclient import TestClient

from app.backend import graph
from app.backend import main as main_module
from app.backend.config import get_settings
from app.workers import deprecations, items, merges, tag_links
from app.workers.wiki_render import parse_frontmatter, render_item_page
from tests.test_tag_links import TEMPLATES, _approve_merge, _build, _g, _keep, _sids, _view
from tests.test_tag_suggestions import _it, _page_fm, _run
import tagging  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    settings.manifests_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


def _node(tmp_path, nid):
    gconn = _g(tmp_path)
    try:
        return graph.get_node(gconn, nid)
    finally:
        gconn.close()


def _repair(tmp_path, apply=False):
    gconn = _g(tmp_path)
    try:
        out = tag_links.repair(gconn, reviews_dir=tmp_path / "reviews", wiki_dir=tmp_path / "wiki",
                               policies_dir=tmp_path / "policies", apply=apply)
        errors = tag_links.consistency_errors(gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki",
                                              policies_dir=tmp_path / "policies")
    finally:
        gconn.close()
    if apply:
        tag_links.finish(tmp_path, out["affected_sources"], wiki_dir=tmp_path / "wiki",
                         pages_changed=out["pages_changed"], templates_dir=TEMPLATES)
    return out, errors


def _audits(tmp_path, event):
    d = tmp_path / "reviews" / "audit_log"
    return [x for x in (json.loads(p.read_text()) for p in sorted(d.glob("*.json")))
            if x["event"] == event]


# --- aliases belong to the user after the first keep ---------------------------------------------


def test_after_the_first_keep_extraction_never_changes_the_aliases(client, tmp_path):
    body = "about continual learning (CL), also lifelong learning or cont learning"
    _build(tmp_path, {"a.md": "alpha " + body, "b.md": "beta " + body, "c.md": "gamma " + body,
                      "d.md": "delta " + body})
    sids = _sids(tmp_path)
    a, b, c, d = sids["a.md"], sids["b.md"], sids["c.md"], sids["d.md"]
    suggestion = _it("Continual Learning", "ai_topic_area", ["CL", "Lifelong Learning"])
    _run(tmp_path, {"alpha": [suggestion]}, source_ids=[a])
    nid = items.node_id("Continual Learning")
    # The first keep confirms the list — the user deletes "Lifelong Learning".
    client.post(f"/ui/reviews/sources/{a}/decide",
                data={f"link_{nid}": "keep", f"amend_aliases_{nid}": "CL"})
    assert _page_fm(tmp_path, "continual-learning")["aliases"] == ["CL"]
    pending_before = sorted(p.name for p in (tmp_path / "reviews" / "pending").glob("*.json"))
    more = _it("Continual Learning", "ai_topic_area", ["CL", "Lifelong Learning", "Cont Learning"])
    _run(tmp_path, {"beta": [more]}, source_ids=[b])
    assert _page_fm(tmp_path, "continual-learning")["aliases"] == ["CL"]     # nothing added back
    # ...and nothing proposed: no new review item of any kind.
    assert sorted(p.name for p in (tmp_path / "reviews" / "pending").glob("*.json")) == pending_before
    # The same holds once the tag is unused (its last keep dropped).
    client.post(f"/ui/reviews/sources/{a}/decide", data={f"link_{nid}": "drop"})
    assert _node(tmp_path, nid)["status"] == "unused"
    _run(tmp_path, {"gamma": [more]}, source_ids=[c])
    assert _page_fm(tmp_path, "continual-learning")["aliases"] == ["CL"]
    # A deleted alias no longer matches: "Lifelong Learning" is its own suggestion.
    out = _run(tmp_path, {"delta": [_it("Lifelong Learning", "ai_topic_area")]}, source_ids=[d])
    assert out["suggestions_resolved"] == 0
    assert [r["node_id"] for r in _view(tmp_path, d)["suggestions"]] == [
        items.node_id("Lifelong Learning")]


def _old_new(tmp_path):
    body = "about topic x, also called old name or new name"
    _build(tmp_path, {"a.md": "alpha " + body, "b.md": "beta " + body, "c.md": "gamma " + body})
    sids = _sids(tmp_path)
    _run(tmp_path, {"alpha": [_it("Topic X", aliases=["Old Name"])]}, source_ids=[sids["a.md"]])
    return sids, items.node_id("Topic X")


def test_a_keep_made_while_extraction_buffers_aliases_keeps_the_confirmed_list(tmp_path):
    sids, nid = _old_new(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    real = tag_links.kept_anywhere
    kept: list[str] = []

    def keep_first(reviews_dir):
        # The last moment a human keep can land: after this document's suggestions were buffered,
        # just before the final page writes check ownership (both serialize on the ledger lock).
        gconn2 = _g(tmp_path)
        try:
            kept.append(tag_links.keep(gconn2, source_id=a, node_id=nid, wiki_dir=tmp_path / "wiki",
                                       reviews_dir=tmp_path / "reviews")["effect"])
        finally:
            gconn2.close()
        return real(reviews_dir)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tag_links, "kept_anywhere", keep_first)
        _run(tmp_path, {"beta": [_it("Topic X", aliases=["Old Name", "New Name"])]}, source_ids=[b])
    assert kept == ["activated"]
    assert _node(tmp_path, nid)["status"] == "active"
    assert _page_fm(tmp_path, "topic-x")["aliases"] == ["Old Name"]


def test_an_interrupted_first_keep_owns_the_aliases_before_repair_finishes_it(tmp_path):
    from app.workers import promote
    sids, nid = _old_new(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    real = promote.activate_node

    def stop_activation(*args, dry_run=False, **kwargs):
        if not dry_run:
            raise RuntimeError("power cut")
        return real(*args, dry_run=dry_run, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(promote, "activate_node", stop_activation)
        with pytest.raises(RuntimeError):
            _keep(tmp_path, a, "Topic X")
    assert _node(tmp_path, nid)["status"] == "candidate"            # the ledger keeps it already
    _run(tmp_path, {"beta": [_it("Topic X", aliases=["Old Name", "New Name"])]}, source_ids=[b])
    assert _page_fm(tmp_path, "topic-x")["aliases"] == ["Old Name"]
    plan, _errors = _repair(tmp_path, apply=True)
    assert plan["interrupted_activations"] == 1
    assert _node(tmp_path, nid)["status"] == "active"
    assert _page_fm(tmp_path, "topic-x")["aliases"] == ["Old Name"]


def test_a_damaged_ledger_withholds_new_aliases_and_says_so(tmp_path):
    sids, nid = _old_new(tmp_path)
    (tmp_path / "reviews" / "tag_links").mkdir(parents=True, exist_ok=True)
    (tmp_path / "reviews" / "tag_links" / f"{sids['c.md']}.json").write_text("{not json",
                                                                         encoding="utf-8")
    out = _run(tmp_path, {"beta": [_it("Topic X", aliases=["Old Name", "New Name"]),
                                   _it("Fresh Topic", aliases=["New Name"])]},
               source_ids=[sids["b.md"]])
    assert out["aliases_withheld_ledger_damaged"] == 2
    assert _page_fm(tmp_path, "topic-x")["aliases"] == ["Old Name"]
    assert _page_fm(tmp_path, "fresh-topic")["aliases"] == []


# --- lifecycle decisions survive extraction and repair -------------------------------------------


def _hide(tmp_path, nid, slug):
    appr = tmp_path / "reviews" / "approved"
    appr.mkdir(parents=True, exist_ok=True)
    (appr / f"rev_h_{slug}.json").write_text(json.dumps({
        "review_id": f"rev_h_{slug}", "type": "hide_semantic_page", "status": "approved",
        "subject": {"node_id": nid, "page": f"Items/{slug}.md"},
        "proposal": {"to_status": "hidden"}, "context": {"node_type": "item"}}), encoding="utf-8")
    gconn = _g(tmp_path)
    try:
        assert deprecations.apply_hidden_semantic_pages(
            gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")["applied"] == 1
    finally:
        gconn.close()


def test_a_hidden_tag_stays_hidden_through_extraction_and_repair(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _run(tmp_path, {"alpha": [_it("Agentic Workflow", "ai_topic_area")]}, source_ids=[a])
    _keep(tmp_path, a, "Agentic Workflow")
    nid = items.node_id("Agentic Workflow")
    _hide(tmp_path, nid, "agentic-workflow")
    # Re-extraction of the keeping document, a new document by name and by a plural variant.
    routes = {"alpha": [_it("Agentic Workflow", "ai_topic_area")],
              "beta": [_it("Agentic Workflow", "ai_topic_area"), _it("Agentic Workflows", "ai_topic_area")]}
    out = _run(tmp_path, routes, force=True)
    assert out["suggestions_filtered_hidden"] == 3
    assert _node(tmp_path, nid)["status"] == "hidden"
    assert _page_fm(tmp_path, "agentic-workflow")["status"] == "hidden"
    assert _view(tmp_path, b)["suggestions"] == [] and _node(tmp_path, items.node_id("Agentic Workflows")) is None
    # Repair neither "finishes" a keep nor flags the kept-while-hidden link; the tag stays hidden.
    plan, errors = _repair(tmp_path)
    assert plan["interrupted_activations"] == 0 and plan["invalid_keeps"] == 0 and errors == []
    _repair(tmp_path, apply=True)
    assert _node(tmp_path, nid)["status"] == "hidden"
    assert list(tag_links.load_ledger(tmp_path / "reviews", a)["kept"]) == [nid]   # keep preserved


def test_an_approved_deprecation_survives_extraction(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _run(tmp_path, {"alpha": [_it("Old Tag")]}, source_ids=[a])
    nid = items.node_id("Old Tag")
    appr = tmp_path / "reviews" / "approved"
    appr.mkdir(parents=True, exist_ok=True)
    (appr / "rev_d.json").write_text(json.dumps({
        "review_id": "rev_d", "type": "deprecate_wiki_page", "status": "approved",
        "subject": {"node_id": nid, "page": "Items/old-tag.md"},
        "proposal": {"to_status": "deprecated_candidate"}, "context": {"node_type": "item"}}),
        encoding="utf-8")
    gconn = _g(tmp_path)
    try:
        assert deprecations.apply_approved_deprecations(
            gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki",
            claims_dir=tmp_path / "wiki" / "Claims",
            markdown_dir=tmp_path / "normalized" / "markdown")["applied"] == 1
    finally:
        gconn.close()
    out = _run(tmp_path, {"alpha": [_it("Old Tag")], "beta": [_it("Old Tag")]}, force=True)
    assert out["suggestions_filtered_retired"] == 2
    fm = _page_fm(tmp_path, "old-tag")
    assert (fm["status"], fm["review_status"]) == ("deprecated_candidate", "approved")
    assert _node(tmp_path, nid)["status"] == "deprecated_candidate"
    assert _view(tmp_path, b)["suggestions"] == []


def test_a_merged_tag_follows_the_merge_without_the_survivor_alias(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body", "c.md": "gamma body",
                      "d.md": "delta body"})
    sids = _sids(tmp_path)
    a, b, c, d = sids["a.md"], sids["b.md"], sids["c.md"], sids["d.md"]
    _run(tmp_path, {"alpha": [_it("Survivor")], "beta": [_it("Absorbed")]}, source_ids=[a, b])
    _keep(tmp_path, a, "Survivor")
    _keep(tmp_path, b, "Absorbed")
    surv, absd = items.node_id("Survivor"), items.node_id("Absorbed")
    _approve_merge(tmp_path, surv, absd)
    gconn = _g(tmp_path)
    try:
        assert tagging.close_journals(tmp_path / "reviews", merges.apply_merges(
            gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki"))["applied"] == 1
    finally:
        gconn.close()
    tag_links.finish(tmp_path, [a, b], wiki_dir=tmp_path / "wiki", templates_dir=TEMPLATES)
    # The user removes the absorbed name from the survivor's aliases: ordinary matching stops,
    # the merge redirect does not.
    page = tmp_path / "wiki" / "Items" / "survivor.md"
    page.write_text(re.sub(r"(?m)^aliases: .*$", "aliases: []", page.read_text()), encoding="utf-8")
    out = _run(tmp_path, {"gamma": [_it("Absorbed")]}, source_ids=[c])
    assert out["suggestions_redirected"] == 1
    assert [r["node_id"] for r in _view(tmp_path, c)["kept"] + _view(tmp_path, c)["suggestions"]] \
        == [surv]
    assert _node(tmp_path, absd)["status"] == "merged"
    assert _page_fm(tmp_path, "absorbed")["merged_into"] == surv
    # A hidden survivor filters the suggestion instead.
    _hide(tmp_path, surv, "survivor")
    out = _run(tmp_path, {"delta": [_it("Absorbed")]}, source_ids=[d])
    assert out["suggestions_filtered_hidden"] == 1 and _view(tmp_path, d)["suggestions"] == []
    assert _node(tmp_path, absd)["status"] == "merged"


# --- an interrupted "use X" ----------------------------------------------------------------------


def _bv(tmp_path):
    _build(tmp_path, {"a.md": "alpha body on bv metrics", "b.md": "beta body on bv"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _run(tmp_path, {"alpha": [_it("Bias Metric", aliases=["BV"]),
                              _it("Bayes Value", "ai_topic_area", ["BV"])]}, source_ids=[a])
    _keep(tmp_path, a, "Bias Metric")
    _keep(tmp_path, a, "Bayes Value")
    _run(tmp_path, {"beta": [_it("BV")]}, source_ids=[b])
    return b, items.node_id("BV"), items.node_id("Bias Metric")


def _crash_use_x(client, tmp_path, b, bv, chosen, step):
    """Run "use X" through the form and stop it right after `step` (a durable write)."""
    def boom(*args, **kwargs):
        raise RuntimeError("power cut")

    real_keep, real_drop, real_audit = tag_links.keep, tag_links.drop, tag_links._audit
    with pytest.MonkeyPatch.context() as mp:
        if step == "record":
            mp.setattr(tag_links, "keep", boom)
        elif step == "keep":
            mp.setattr(tag_links, "drop", boom)
        elif step == "drop":
            mp.setattr(tag_links, "_audit", lambda rd, sub, event, *a, **k: boom() if
                       event == "tag-resolved" else real_audit(rd, sub, event, *a, **k))
        else:                                             # "audit": the record is never removed
            mp.setattr(tag_links, "_end_resolving", boom)
        with pytest.raises(RuntimeError):
            client.post(f"/ui/reviews/sources/{b}/decide", data={f"link_{bv}": f"use:{chosen}"})
    assert (tag_links.keep, tag_links.drop, tag_links._audit) == (real_keep, real_drop, real_audit)


@pytest.mark.parametrize("step", ["record", "keep", "drop", "audit"])
def test_an_interrupted_use_x_stays_pending_until_retried(client, tmp_path, step):
    b, bv, chosen = _bv(tmp_path)
    _crash_use_x(client, tmp_path, b, bv, chosen, step)
    view = _view(tmp_path, b)
    row = next((r for r in view["suggestions"] if r["node_id"] == bv), None)
    pending = row["pending_use"] if row else view["unfinished"][0]
    assert pending["chosen"] == chosen and pending["chosen_title"] == "Bias Metric"
    assert ("Unfinished choices" in client.get(f"/ui/reviews/sources/{b}").text) is (row is None)
    # Repair reports it and never resumes it.
    for apply in (False, True):
        plan, _errors = _repair(tmp_path, apply=apply)
        assert plan["pending_resolutions"] == 1
    assert len(tag_links.pending_resolutions(tmp_path / "reviews", b)) == 1
    assert len(_audits(tmp_path, "tag-resolved")) == (1 if step == "audit" else 0)
    # The explicit retry finishes it; nothing is decided or audited twice.
    resp = client.post(f"/ui/reviews/sources/{b}/decide", data={f"link_{bv}": f"use:{chosen}"})
    assert "1 recorded, 0 skipped" in resp.text
    view = _view(tmp_path, b)
    assert [r["node_id"] for r in view["kept"]] == [chosen]
    assert view["suggestions"] == [] and view["unfinished"] == []
    assert [d["name"] for d in view["dropped"]] == ["BV"]
    assert len(_audits(tmp_path, "tag-resolved")) == 1
    assert len([x for x in _audits(tmp_path, "tag-kept") if x["source_id"] == b]) == 1
    assert len([x for x in _audits(tmp_path, "tag-dropped") if x["source_id"] == b]) == 1
    assert tag_links.pending_resolutions(tmp_path / "reviews") == []
    assert _repair(tmp_path)[1] == []


def test_a_different_decision_cancels_the_unfinished_use_x_and_keeps_x(client, tmp_path):
    b, bv, chosen = _bv(tmp_path)
    _crash_use_x(client, tmp_path, b, bv, chosen, "keep")         # X kept, suggestion not dropped
    resp = client.post(f"/ui/reviews/sources/{b}/decide", data={f"link_{bv}": "drop"})
    assert "1 recorded, 0 skipped" in resp.text
    assert "Bias Metric stays kept on this document" in resp.text
    view = _view(tmp_path, b)
    assert [r["node_id"] for r in view["kept"]] == [chosen] and view["unfinished"] == []
    cancelled = _audits(tmp_path, "tag-resolve-cancelled")
    assert len(cancelled) == 1 and cancelled[0]["retained_keep"] is True
    assert cancelled[0]["reason"] == "drop" and _audits(tmp_path, "tag-resolved") == []
    assert tag_links.pending_resolutions(tmp_path / "reviews") == []


def test_an_unfinished_use_x_can_be_cancelled_after_its_suggestion_is_gone(client, tmp_path):
    b, bv, chosen = _bv(tmp_path)
    _crash_use_x(client, tmp_path, b, bv, chosen, "drop")          # the suggestion was deleted
    assert _view(tmp_path, b)["unfinished"][0]["node_id"] == bv
    assert client.post(f"/reviews/sources/{b}/links",
                       json={"node_id": bv, "action": "keep"}).status_code == 409
    resp = client.post(f"/reviews/sources/{b}/links", json={"node_id": bv, "action": "cancel"})
    assert resp.status_code == 200 and resp.json()["cancelled"][0]["retained_keep"] is True
    assert _view(tmp_path, b)["unfinished"] == []
    assert client.post(f"/reviews/sources/{b}/links",
                       json={"node_id": bv, "action": "cancel"}).status_code == 409


# --- frontmatter lists: a comma inside an alias round-trips ----------------------------------------


def test_a_comma_inside_an_alias_round_trips_through_both_parsers():
    aliases = ["Bias, Variance", "Trade-off", "B/V (2020)"]
    text = render_item_page({"node_id": "itm_0123456789abcdef", "item_type": "method_technique",
                             "title": "Bias-Variance", "aliases": aliases, "confidence": "low",
                             "source_ids": [], "status": "active"})
    assert parse_frontmatter(text)["aliases"] == aliases
    assert rebuild_index.parse_frontmatter(text)["aliases"] == aliases
