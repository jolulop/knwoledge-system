"""ADR-0066 part 2 (decisions 7–8, P1–P7): the suggestion pipeline — body-text alias check, key
resolution to existing tags, ambiguous matches and "Use X", drop/block filtering through the
resolved tag, domains first, the per-document cap, and the prefilled aliases field on keep."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from fastapi.testclient import TestClient

from app.backend import graph
from app.backend import main as main_module
from app.backend.config import get_settings
from app.llm.cache import ResponseCache
from app.llm.client import LLMClient
from app.workers import items, merge_candidates, tag_links, wiki
from app.workers.wiki_render import parse_frontmatter
from tests.test_tag_links import (
    MODEL_REF,
    TEMPLATES,
    CountingAdapter,
    _block,
    _build,
    _drop,
    _g,
    _keep,
    _sids,
    _titles,
    _validators_green,
    _view,
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    settings.manifests_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


def _it(name, item_type="method_technique", aliases=()):
    return {"name": name, "item_type": item_type, "aliases": list(aliases)}


def _run(tmp_path, routes, *, cap=items.DEFAULT_SUGGESTION_CAP, force=False, source_ids=None,
         cache_only=False, adapter=None):
    adapter = adapter or CountingAdapter(routes)
    client_ = LLMClient({"anthropic": adapter},
                        cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite"))
    out = items.extract_items(tmp_path, client=client_, model_ref=MODEL_REF, force=force,
                              source_ids=source_ids, cache_only=cache_only, cap=cap,
                              jobs_db=tmp_path / "db" / "jobs.sqlite")
    wiki.generate_wiki(tmp_path, templates_dir=TEMPLATES, rebuild_index=False, record_job=False)
    return out


def _artifact(tmp_path, sid):
    return json.loads((tmp_path / "normalized" / "enrichment" / f"{sid}.items.json").read_text())


def _page_fm(tmp_path, slug):
    return parse_frontmatter((tmp_path / "wiki" / "Items" / f"{slug}.md").read_text(encoding="utf-8"))


def _llm_tag(tmp_path):
    """An active "LLM" tag (alias "Large Language Model", kept on a.md); returns the sids."""
    _build(tmp_path, {"a.md": "alpha body about the LLM, a large language model.",
                      "b.md": "beta body", "c.md": "gamma body"})
    sids = _sids(tmp_path)
    _run(tmp_path, {"alpha": [_it("LLM", "model_family_architecture",
                                  ["Large Language Model"])]}, source_ids=[sids["a.md"]])
    assert _keep(tmp_path, sids["a.md"], "LLM")["effect"] == "activated"
    return sids


# --- P4: the reference cut + the alias check ---------------------------------------------------


def test_reference_cut_takes_the_last_qualifying_marker():
    body = "Body text. " * 40
    text = (body + "References [1] Smith (2020). A survey.\n" + "More references 2019.\n")
    assert items.reference_cut(text) == len(body)
    # A marker in the first half, a lower-case footer, or one with no year/[n] after it: no cut.
    assert items.reference_cut("References [1] Smith 2020. " + body) is None
    assert items.reference_cut(body + "This article references 2019 work.") is None
    assert items.reference_cut(body + "Bibliography and further thoughts on nothing.") is None
    # The LAST qualifying marker wins (a "References" heading inside the body text comes earlier).
    late = body + "References 2001 aside. " + "x " * 10 + "BIBLIOGRAPHY\n[1] Doe 1999."
    assert items.reference_cut(late) == late.index("BIBLIOGRAPHY")


def test_occurs_in_is_case_punctuation_and_plural_insensitive():
    folded = merge_candidates.fold_text("We study Large-Language Models (LLMs) and agents.")
    assert merge_candidates.occurs_in("large language model", folded)
    assert merge_candidates.occurs_in("LLM", folded) and merge_candidates.occurs_in("Agent", folded)
    assert not merge_candidates.occurs_in("lang", folded)               # whole tokens only
    assert not merge_candidates.occurs_in("", folded)


def test_an_alias_found_only_in_the_references_is_filtered(tmp_path):
    body = "alpha body on continual learning (CL). " * 20
    _build(tmp_path, {"a.md": body + "\n\nReferences\n[1] Smith (2020). Lifelong learning."})
    sid = _sids(tmp_path)["a.md"]
    _run(tmp_path, {"alpha": [_it("Continual Learning", "ai_topic_area",
                                  ["CL", "Lifelong Learning", "Plastic Learning"])]})
    node = _artifact(tmp_path, sid)["nodes"][0]
    assert node["aliases"] == ["CL"]
    assert node["aliases_filtered"] == ["Lifelong Learning", "Plastic Learning"]
    assert _artifact(tmp_path, sid)["reference_cut_at"] is not None
    assert _page_fm(tmp_path, "continual-learning")["aliases"] == ["CL"]


# --- P1/P2: domains first, the cap ---------------------------------------------------------------


def test_domains_move_to_the_front_and_the_cap_cuts_the_rest(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    sid = _sids(tmp_path)["a.md"]
    ranked = [_it(f"Topic {i:02d}") for i in range(17)] + [_it("Philosophy", "domain")] \
        + [_it(f"Topic {i:02d}") for i in range(17, 19)]
    out = _run(tmp_path, {"alpha": ranked}, cap=15)
    names = [n["name"] for n in _artifact(tmp_path, sid)["nodes"]]
    assert len(names) == 15 and names[0] == "Philosophy"            # the domain survives the cut
    assert names[1:] == [f"Topic {i:02d}" for i in range(14)]
    over = [f for f in _artifact(tmp_path, sid)["filtered"] if f["reason"] == "over_cap"]
    assert [f["name"] for f in over] == [f"Topic {i:02d}" for i in range(14, 19)]
    assert out["suggestions_over_cap"] == 5 and out["suggestion_cap"] == 15
    assert out["no_domain"] == 0


def test_only_the_first_three_domains_are_moved(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    sid = _sids(tmp_path)["a.md"]
    ranked = [_it("T1"), _it("D1", "domain"), _it("T2"), _it("D2", "domain"), _it("D3", "domain"),
              _it("D4", "domain")]
    _run(tmp_path, {"alpha": ranked})
    assert [n["name"] for n in _artifact(tmp_path, sid)["nodes"]] == ["D1", "D2", "D3", "T1", "T2",
                                                                      "D4"]


def test_a_document_with_no_domain_suggestion_is_reported(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    out = _run(tmp_path, {"alpha": [_it("Only Topic")], "beta": [_it("AI", "domain")]})
    assert out["no_domain"] == 1 and out["no_domain_sources"] == [sids["a.md"]]


def test_kept_tags_count_toward_the_cap_and_are_never_cut(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    sid = _sids(tmp_path)["a.md"]
    names = [f"Tag {i}" for i in range(8)]
    _run(tmp_path, {"alpha": [_it(n) for n in names]})
    for n in names[4:]:
        _keep(tmp_path, sid, n)
    _run(tmp_path, {"alpha": [_it(n) for n in names]}, cap=5, force=True)
    view = _view(tmp_path, sid)
    assert _titles(view["kept"]) == names[4:]                       # 4 kept: never cut
    assert _titles(view["suggestions"]) == ["Tag 0"]                # 5 - 4 = 1 new suggestion
    _validators_green(tmp_path)


def test_a_cap_change_restales_nothing_and_cache_only_recuts(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    sid = _sids(tmp_path)["a.md"]
    routes = {"alpha": [_it(f"Tag {i}") for i in range(10)]}
    _run(tmp_path, routes, cap=15)
    adapter = CountingAdapter(routes)
    assert _run(tmp_path, routes, cap=5, adapter=adapter)["skipped_fresh"] == 1   # not stale
    out = _run(tmp_path, routes, cap=5, force=True, cache_only=True, adapter=adapter)
    assert adapter.calls == 0 and out["suggestions_over_cap"] == 5
    assert len(_view(tmp_path, sid)["suggestions"]) == 5
    assert _artifact(tmp_path, sid)["cap"] == 5


# --- P3: resolution to existing tags ------------------------------------------------------------


def test_a_suggestion_resolves_to_an_existing_tag_by_title_or_alias(tmp_path):
    sids = _llm_tag(tmp_path)
    b = sids["b.md"]
    out = _run(tmp_path, {"beta": [_it("Large Language Models", "model_family_architecture",
                                      ["LLMs"]),
                                  _it("LLMs", "model_family_architecture")]}, source_ids=[b])
    llm = items.node_id("LLM")
    assert [r["node_id"] for r in _view(tmp_path, b)["suggestions"]] == [llm]   # one link
    gconn = _g(tmp_path)
    try:
        assert graph.get_node(gconn, items.node_id("Large Language Models")) is None   # no new tag
        assert graph.get_node(gconn, llm)["status"] == "active"
    finally:
        gconn.close()
    nodes = _artifact(tmp_path, b)["nodes"]
    assert [(n["node_id"], n["resolution"], n["name"]) for n in nodes] == [
        (llm, "resolved", "Large Language Models")]
    assert out["suggestions_resolved"] == 1
    assert _page_fm(tmp_path, "llm")["aliases"] == ["Large Language Model"]      # nothing added
    _validators_green(tmp_path)


def test_candidates_never_attract_other_suggestions(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    _run(tmp_path, {"alpha": [_it("Graph Store")], "beta": [_it("Graph Stores")]})
    assert [r["node_id"] for r in _view(tmp_path, sids["b.md"])["suggestions"]] == [
        items.node_id("Graph Stores")]


def test_the_exact_name_wins_over_an_alias_match(tmp_path):
    _build(tmp_path, {"a.md": "alpha body on agents", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _run(tmp_path, {"alpha": [_it("Agent"), _it("Autonomous Agent", aliases=["Agents"])]},
         source_ids=[a])
    _keep(tmp_path, a, "Agent")
    _keep(tmp_path, a, "Autonomous Agent")
    _run(tmp_path, {"beta": [_it("Agent")]}, source_ids=[b])
    row = _view(tmp_path, b)["suggestions"][0]
    assert row["node_id"] == items.node_id("Agent") and row["ambiguous_with"] == []
    assert _artifact(tmp_path, b)["nodes"][0]["resolution"] == "exact"


def _two_bv_tags(tmp_path):
    _build(tmp_path, {"a.md": "alpha body on bv metrics", "b.md": "beta body on bv"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _run(tmp_path, {"alpha": [_it("Bias Metric", aliases=["BV"]),
                              _it("Bayes Value", "ai_topic_area", ["BV"])]}, source_ids=[a])
    _keep(tmp_path, a, "Bias Metric")
    _keep(tmp_path, a, "Bayes Value")
    _run(tmp_path, {"beta": [_it("BV")]}, source_ids=[b])
    return a, b


def test_an_ambiguous_match_is_left_to_the_user(client, tmp_path):
    a, b = _two_bv_tags(tmp_path)
    bv = items.node_id("BV")
    row = _view(tmp_path, b)["suggestions"][0]
    assert row["node_id"] == bv and row["node_status"] == "candidate"
    assert {(m["title"], m["item_type"], m["kept_count"]) for m in row["ambiguous_with"]} == {
        ("Bias Metric", "method_technique", 1), ("Bayes Value", "ai_topic_area", 1)}
    assert sorted(_artifact(tmp_path, b)["nodes"][0]["ambiguous_with"]) == sorted(
        [items.node_id("Bias Metric"), items.node_id("Bayes Value")])
    page = client.get(f"/ui/reviews/sources/{b}").text
    assert f"value='use:{items.node_id('Bias Metric')}'" in page
    assert "use Bayes Value (ai_topic_area, kept on 1)" in page and "keep as new tag" in page
    assert "matches several existing tags" in page


def test_use_x_keeps_the_chosen_tag_and_drops_the_suggestion(client, tmp_path):
    a, b = _two_bv_tags(tmp_path)
    bv, chosen = items.node_id("BV"), items.node_id("Bias Metric")
    resp = client.post(f"/ui/reviews/sources/{b}/decide", data={f"link_{bv}": f"use:{chosen}"})
    assert resp.status_code == 200 and "1 recorded, 0 skipped" in resp.text
    view = _view(tmp_path, b)
    assert [r["node_id"] for r in view["kept"]] == [chosen] and view["suggestions"] == []
    assert [d["name"] for d in view["dropped"]] == ["BV"]                # the suggested name
    gconn = _g(tmp_path)
    try:
        assert graph.get_node(gconn, bv) is None                         # never kept: deleted
    finally:
        gconn.close()
    audits = [json.loads(p.read_text()) for p in (tmp_path / "reviews" / "audit_log").glob("*.json")]
    resolved = [x for x in audits if x["event"] == "tag-resolved"]
    assert len(resolved) == 1 and resolved[0]["chosen"] == chosen
    assert resolved[0]["suggestion"] == {"node_id": bv, "name": "BV"}
    # Re-extraction filters the suggestion (the drop record carries its name); the keep stays.
    out = _run(tmp_path, {"beta": [_it("BV")]}, source_ids=[b], force=True)
    assert out["suggestions_filtered_dropped"] == 1
    view = _view(tmp_path, b)
    assert [r["node_id"] for r in view["kept"]] == [chosen] and view["suggestions"] == []
    _validators_green(tmp_path)


def test_use_x_refuses_a_tag_that_is_not_one_of_the_matches(client, tmp_path):
    a, b = _two_bv_tags(tmp_path)
    bv = items.node_id("BV")
    other = items.node_id("Unrelated")
    resp = client.post(f"/ui/reviews/sources/{b}/decide", data={f"link_{bv}": f"use:{other}"})
    assert "not_an_ambiguous_match" in resp.text
    assert client.post(f"/reviews/sources/{b}/links",
                       json={"node_id": bv, "action": "use", "chosen": other}).status_code == 409
    resp = client.post(f"/reviews/sources/{b}/links",
                       json={"node_id": bv, "action": "use", "chosen": items.node_id("Bayes Value")})
    assert resp.status_code == 200 and resp.json()["outcome"] == "resolved"
    assert [r["title"] for r in _view(tmp_path, b)["kept"]] == ["Bayes Value"]


def test_a_rejected_merge_pair_is_never_auto_resolved(tmp_path):
    sids = _llm_tag(tmp_path)
    b = sids["b.md"]
    rej = tmp_path / "reviews" / "rejected"
    rej.mkdir(parents=True, exist_ok=True)
    (rej / "rev_r.json").write_text(json.dumps({
        "review_id": "rev_r", "type": "merge_items", "status": "rejected",
        "subject": {"survivor_node_id": items.node_id("LLM"),
                    "absorbed_node_id": items.node_id("Large Language Model")}}), encoding="utf-8")
    _run(tmp_path, {"beta": [_it("Large Language Model", "model_family_architecture")]},
         source_ids=[b])
    row = _view(tmp_path, b)["suggestions"][0]
    assert row["node_id"] == items.node_id("Large Language Model")
    assert [m["node_id"] for m in row["ambiguous_with"]] == [items.node_id("LLM")]


# --- P6 step 3: drops and blocks through the resolved tag; step 6: type conflicts ---------------


def test_a_drop_of_the_resolved_tag_filters_the_long_form(tmp_path):
    sids = _llm_tag(tmp_path)
    b = sids["b.md"]
    _run(tmp_path, {"beta": [_it("LLM", "model_family_architecture")]}, source_ids=[b])
    _drop(tmp_path, b, "LLM")
    out = _run(tmp_path, {"beta": [_it("Large Language Models", "model_family_architecture")]},
               source_ids=[b], force=True)
    assert out["suggestions_filtered_dropped"] == 1 and _view(tmp_path, b)["suggestions"] == []


def test_a_block_of_the_resolved_tag_blocks_the_long_form_and_unblock_finds_it(tmp_path):
    sids = _llm_tag(tmp_path)
    c = sids["c.md"]
    _block(tmp_path, "LLM")
    out = _run(tmp_path, {"gamma": [_it("Large Language Models", "model_family_architecture")]},
               source_ids=[c])
    assert out["suggestions_filtered_blocked"] == 1 and _view(tmp_path, c)["suggestions"] == []
    row = _artifact(tmp_path, c)["filtered"][0]
    assert row["reason"] == "blocked" and row["resolved_title"] == "LLM"
    res = tag_links.unblock(key=tag_links.tag_key("LLM"), policies_dir=tmp_path / "policies",
                            enrichment_dir=tmp_path / "normalized" / "enrichment",
                            reviews_dir=tmp_path / "reviews")
    assert c in res["resuggest_sources"]


def test_a_resolved_suggestion_of_another_type_files_a_retype_on_the_existing_tag(tmp_path):
    sids = _llm_tag(tmp_path)
    b = sids["b.md"]
    _run(tmp_path, {"beta": [_it("LLMs", "technology_capability")]}, source_ids=[b])
    pending = [json.loads(p.read_text()) for p in (tmp_path / "reviews" / "pending").glob("*.json")]
    retypes = [p for p in pending if p["type"] == "change_item_type"]
    assert [(r["subject"]["node_id"], r["subject"]["to_item_type"]) for r in retypes] == [
        (items.node_id("LLM"), "technology_capability")]
    assert _page_fm(tmp_path, "llm")["item_type"] == "model_family_architecture"   # never auto


# --- P5: the aliases field on keep ------------------------------------------------------------------


def test_the_aliases_field_is_prefilled_and_replaces_the_list(client, tmp_path):
    _build(tmp_path, {"a.md": "alpha body on continual learning, CL or cont learning",
                      "b.md": "beta body on continual learning, CL or cont learning"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    routes = {"alpha": [_it("Continual Learning", aliases=["CL", "Cont Learning"])],
              "beta": [_it("Other Learning", aliases=["CL", "Cont Learning"])]}
    _run(tmp_path, routes)
    nid = items.node_id("Continual Learning")
    page = client.get(f"/ui/reviews/sources/{a}").text
    assert f"name='amend_aliases_{nid}'" in page and ">CL\nCont Learning</textarea>" in page
    # Unchanged field: no aliases amendment is recorded.
    client.post(f"/ui/reviews/sources/{a}/decide",
                data={f"link_{nid}": "keep", f"amend_aliases_{nid}": "CL\r\nCont Learning\r\n"})
    entry = json.loads((tmp_path / "reviews" / "tag_links" / f"{a}.json").read_text())["kept"][nid]
    assert "aliases" not in (entry.get("amendments") or {})
    # Edited field: it replaces the list (a deleted alias is gone from the tag).
    other = items.node_id("Other Learning")
    client.post(f"/ui/reviews/sources/{b}/decide",
                data={f"link_{other}": "keep", f"amend_aliases_{other}": "CL"})
    assert _page_fm(tmp_path, "other-learning")["aliases"] == ["CL"]


def test_reextracting_a_renamed_tag_keeps_its_slug_and_one_page(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    routes = {"alpha": [_it("Keep Me")], "beta": [_it("Keep Me")]}
    _run(tmp_path, routes, source_ids=[sids["a.md"]])
    _keep(tmp_path, sids["a.md"], "Keep Me", title="Kept Name")
    _run(tmp_path, routes, force=True)                  # both documents suggest the original name
    gconn = _g(tmp_path)
    try:
        node = graph.get_node(gconn, items.node_id("Keep Me"))
    finally:
        gconn.close()
    assert node["slug"] == "kept-name" and node["status"] == "active"
    assert not (tmp_path / "wiki" / "Items" / "keep-me.md").exists()
    assert [r["title"] for r in _view(tmp_path, sids["b.md"])["suggestions"]] == ["Kept Name"]
    _validators_green(tmp_path)
