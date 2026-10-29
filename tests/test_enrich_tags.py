"""ADR-0066 part 3 slice 2: Enrich's tags join the tag suggestions.

Covers the merge before resolution (slice 2 Q2: alias matching, consumption, every contributing key
against drops and blocks, origins), plan freshness kept apart from model freshness (Q1: the stored
raw list, re-plans with no model call and no cache, legacy artifacts, merge-input changes during a
run) and the Source page's `keywords:` for search (Q3).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.backend import graph, keyword_index, manifests  # noqa: E402
from app.backend import main as main_module  # noqa: E402
from app.backend.config import get_settings  # noqa: E402
from app.llm import prompts  # noqa: E402
from app.llm.cache import ResponseCache  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.workers import enrich, extract, intake, items, tag_links, wiki  # noqa: E402
from app.workers.wiki_render import parse_frontmatter  # noqa: E402
from tests import fixtures  # noqa: E402

MODEL_REF = "anthropic:claude-haiku-4-5"
TEMPLATES = ROOT / "templates"
BODY = ("# Notes on language models\n\nA large language model (LLM) is trained on text. "
        "Knowledge graphs (KG) store facts. Retrieval augments generation.\n")
EMPTY_VOCAB = {"keys": {}, "titles": {}, "decided": {}, "decided_keys": {}}


def _it(name, item_type="method_technique", aliases=()):
    return {"name": name, "item_type": item_type, "aliases": list(aliases)}


def _tag(name, item_type="domain"):
    return {"name": name, "item_type": item_type}


@pytest.fixture
def gconn(tmp_path):
    path = tmp_path / "db" / "graph.sqlite"
    graph.init_db(path)
    conn = graph.connect(path)
    yield conn
    conn.close()


def _plan(gconn, raw, tags, *, dropped=(set(), set()), blocked=frozenset(), cap=15, md=BODY):
    return items.plan_suggestions(raw, md=md, gconn=gconn, vocab=EMPTY_VOCAB, rejected_pairs=set(),
                                  dropped=dropped, blocked_keys=set(blocked), kept_ids=set(),
                                  cap=cap, enrich_tags=tags)


def _names(plan):
    return [(e["name"], e["origin"]) for e in plan["emit"]]


# --- the merge (slice 2 Q2) ----------------------------------------------------------------------


def test_an_enrich_acronym_merges_into_the_items_name_through_its_alias(gconn):
    plan = _plan(gconn, [_it("Retrieval"), _it("Large Language Model", aliases=["LLM"])],
                 [_tag("LLM")])
    assert _names(plan) == [("Large Language Model", "both"), ("Retrieval", "items")]
    assert plan["emit"][0]["aliases"] == ["LLM"]
    assert plan["emit"][0]["item_type"] == "method_technique"   # the items type


def test_a_rejected_alias_never_matches(gconn):
    # "Generative AI" never occurs in the body, so the alias check removes it before the merge.
    plan = _plan(gconn, [_it("Large Language Model", aliases=["Generative AI"])],
                 [_tag("Generative AI")])
    assert _names(plan) == [("Generative AI", "enrich"), ("Large Language Model", "items")]


def test_several_enrich_forms_matching_one_item_give_one_entry_at_the_earliest(gconn):
    plan = _plan(gconn, [_it("Retrieval"), _it("Large Language Model", aliases=["LLM"])],
                 [_tag("Retrieval augmentation"), _tag("LLM"), _tag("large language models")])
    assert _names(plan) == [("Retrieval augmentation", "enrich"), ("Large Language Model", "both"),
                            ("Retrieval", "items")]


def test_an_enrich_tag_matching_several_items_merges_with_the_first(gconn):
    plan = _plan(gconn, [_it("Knowledge Graph", aliases=["KG"]), _it("KG")], [_tag("KG")])
    assert _names(plan) == [("Knowledge Graph", "both"), ("KG", "items")]


@pytest.mark.parametrize("decision", ["dropped", "blocked"])
def test_a_decision_on_the_enrich_form_holds_after_the_merge(gconn, decision):
    key = tag_links.tag_key("LLM")
    kw = ({"dropped": ({key}, set())} if decision == "dropped" else {"blocked": {key}})
    plan = _plan(gconn, [_it("Large Language Model", aliases=["LLM"])], [_tag("LLM")], **kw)
    assert plan["emit"] == []
    assert [f["reason"] for f in plan["filtered"]] == [decision]


def test_enrich_keeps_its_order_and_domains_first_moves_only_the_items_tail(gconn):
    plan = _plan(gconn, [_it("Retrieval"), _it("Software", "domain"), _it("Testing")],
                 [_tag("Reporting", "use_case"), _tag("Banking")])
    assert [e["name"] for e in plan["emit"]] == ["Reporting", "Banking", "Software", "Retrieval",
                                                  "Testing"]


def test_the_raw_cap_applies_to_the_items_list_before_the_merge(gconn):
    raw = [_it(f"Topic {i}") for i in range(items._MAX_ITEMS + 5)]
    plan = _plan(gconn, raw, [_tag(f"Enrich {i}") for i in range(7)], cap=1000)
    assert len(plan["emit"]) == 7 + items._MAX_ITEMS


def test_the_combined_cap_cuts_the_tail(gconn):
    plan = _plan(gconn, [_it(f"Topic {i}") for i in range(20)],
                 [_tag(f"Enrich {i}") for i in range(7)])
    assert len(plan["emit"]) == 15 and plan["emit"][6]["name"] == "Enrich 6"
    assert plan["counts"]["over_cap"] == 12


def test_origins_union_when_entries_collapse_onto_one_existing_tag(gconn, tmp_path):
    graph.upsert_node(gconn, node_id="itm_aaaaaaaaaaaaaaaa", node_type="item", slug="kg",
                      status="active", item_type="data_ontology_asset")
    vocab = {"keys": {tag_links.tag_key("Knowledge Graph"): ["itm_aaaaaaaaaaaaaaaa"],
                      tag_links.tag_key("KG"): ["itm_aaaaaaaaaaaaaaaa"]},
             "titles": {"itm_aaaaaaaaaaaaaaaa": "Knowledge Graph"}, "decided": {},
             "decided_keys": {}}
    plan = items.plan_suggestions([_it("Knowledge Graph")], md=BODY, gconn=gconn, vocab=vocab,
                                  rejected_pairs=set(), dropped=(set(), set()), blocked_keys=set(),
                                  kept_ids=set(), cap=15, enrich_tags=[_tag("KG")])
    assert [(e["node_id"], e["origin"]) for e in plan["emit"]] == [("itm_aaaaaaaaaaaaaaaa", "both")]


# --- end to end: plan freshness (slice 2 Q1) ------------------------------------------------------


class Model:
    """The Enrich and items passes; `items_calls` counts model calls for the items pass."""
    name = "anthropic"
    supports_batch = False

    def __init__(self, tags=None, raw=None, *, fail_items=False):
        self.tags = tags if tags is not None else [_tag("Banking"), _tag("LLM", "model")]
        self.raw = raw if raw is not None else [_it("Large Language Model", aliases=["LLM"]),
                                                _it("Retrieval")]
        self.fail_items = fail_items
        self.items_calls = 0

    def available(self):
        return True

    def parse(self, messages, schema, model_id, *, max_tokens):
        if schema is prompts.SUMMARY_TAGS_SCHEMA:
            return {"title": "Notes on Language Models", "authors": [], "publisher": "",
                    "summary": "A short summary.", "tags": [dict(t) for t in self.tags]}
        if self.fail_items:
            raise AssertionError("the items model must not be called for a re-plan")
        self.items_calls += 1
        return {"items": [dict(i) for i in self.raw]}


def _vault(tmp_path):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.copy_templates(tmp_path)
    (inbox / "notes.md").write_text(BODY, encoding="utf-8")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    extract.extract_sources(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    (sid,) = [m["source_id"] for m in manifests.list_manifests(tmp_path / "raw" / "manifests")]
    return sid


def _client(tmp_path, model, cached=True):
    return LLMClient({"anthropic": model},
                     cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite") if cached else None)


def _enrich(tmp_path, model, **kw):
    return enrich.enrich_sources(tmp_path, client=_client(tmp_path, model, cached=False),
                                 model_ref=MODEL_REF, jobs_db=tmp_path / "db" / "jobs.sqlite",
                                 force=True, **kw)


def _items(tmp_path, model, cached=True, **kw):
    return items.extract_items(tmp_path, client=_client(tmp_path, model, cached=cached),
                               model_ref=MODEL_REF, jobs_db=tmp_path / "db" / "jobs.sqlite", **kw)


def _artifact(tmp_path, sid):
    return json.loads((tmp_path / "normalized" / "enrichment" / f"{sid}.items.json").read_text())


def _suggested(tmp_path, sid):
    """{name: origin} of the document's current suggestions and keeps."""
    return {n["name"]: n["origin"] for n in _artifact(tmp_path, sid)["nodes"]}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "settings", get_settings(tmp_path))
    return TestClient(main_module.app)


def _act(api, sid, name, action):
    resp = api.post(f"/reviews/sources/{sid}/links",
                    json={"node_id": items.node_id(name), "action": action})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_the_artifact_keeps_the_raw_list_and_the_merge_input(tmp_path):
    sid = _vault(tmp_path)
    _enrich(tmp_path, Model())
    _items(tmp_path, Model())
    a = _artifact(tmp_path, sid)
    assert a["raw_items"] == Model().raw and a["merge_version"] == items.MERGE_VERSION
    assert a["merge_input"] == items.merge_input([_tag("Banking"), _tag("LLM", "model")])
    assert _suggested(tmp_path, sid) == {"Banking": "enrich", "Large Language Model": "both",
                                         "Retrieval": "items"}


def test_a_tag_only_enrich_change_re_plans_with_no_model_and_no_cache(tmp_path, api):
    sid = _vault(tmp_path)
    _enrich(tmp_path, Model())
    _items(tmp_path, Model())
    _act(api, sid, "Retrieval", "keep")
    _act(api, sid, "Banking", "drop")
    (tmp_path / "db" / "llm_cache.sqlite").unlink()                # the cache is gone
    _enrich(tmp_path, Model(tags=[_tag("Banking"), _tag("Reporting", "use_case")]))
    out = _items(tmp_path, Model(fail_items=True), cached=False)
    assert out["replanned_sources"] == [sid] and out["status"] == "succeeded"
    names = _suggested(tmp_path, sid)
    assert "Reporting" in names and "Banking" not in names          # the drop survives
    assert "Large Language Model" in names                          # from the stored raw list
    ledger = tag_links.load_ledger(tmp_path / "reviews", sid)
    assert items.node_id("Retrieval") in ledger["kept"]             # the keep survives
    assert _items(tmp_path, Model(fail_items=True), cached=False)["skipped_fresh"] == 1


def test_a_block_survives_a_re_plan(tmp_path, api):
    sid = _vault(tmp_path)
    _enrich(tmp_path, Model())
    _items(tmp_path, Model())
    _act(api, sid, "Banking", "block")
    _enrich(tmp_path, Model(tags=[_tag("Banking"), _tag("Reporting", "use_case")]))
    _items(tmp_path, Model(fail_items=True), cached=False)
    assert "Banking" not in _suggested(tmp_path, sid)


def test_enrich_tags_removed_or_reordered_re_plan(tmp_path):
    sid = _vault(tmp_path)
    _enrich(tmp_path, Model(tags=[_tag("Banking"), _tag("Reporting", "use_case")]))
    _items(tmp_path, Model())
    assert list(_suggested(tmp_path, sid))[:2] == ["Banking", "Reporting"]
    _enrich(tmp_path, Model(tags=[_tag("Reporting", "use_case"), _tag("Banking")]))
    assert _items(tmp_path, Model(fail_items=True), cached=False)["replanned_sources"] == [sid]
    assert list(_suggested(tmp_path, sid))[:2] == ["Reporting", "Banking"]
    # A changed hint makes the Enrich artifact stale: an explicit empty merge input.
    log_path = tmp_path / "normalized" / "extraction_logs" / f"{sid}.json"
    log = json.loads(log_path.read_text())
    log["metadata"] = {"title": "A Newer Hint"}
    log_path.write_text(json.dumps(log))
    out = _items(tmp_path, Model(fail_items=False), cached=True)
    assert set(_suggested(tmp_path, sid)) == {"Large Language Model", "Retrieval"}
    assert out["status"] == "succeeded"


def test_a_legacy_artifact_without_the_raw_list_is_reported_never_emptied(tmp_path):
    sid = _vault(tmp_path)
    _enrich(tmp_path, Model())
    _items(tmp_path, Model())
    path = tmp_path / "normalized" / "enrichment" / f"{sid}.items.json"
    legacy = json.loads(path.read_text())
    for key in ("raw_items", "merge_input", "merge_version"):
        legacy.pop(key)
    path.write_text(json.dumps(legacy))
    before = set(_suggested(tmp_path, sid))
    out = _items(tmp_path, Model(fail_items=True), cached=False)
    assert out["replan_unavailable_sources"] == [sid] and out["status"] == "partial"
    assert set(_suggested(tmp_path, sid)) == before                 # nothing withdrawn
    # Regeneration from the response cache recovers it.
    out = _items(tmp_path, Model(fail_items=True), cached=True, cache_only=True, force=True)
    assert _artifact(tmp_path, sid)["raw_items"] == Model().raw


def test_a_malformed_raw_list_is_not_reused(tmp_path):
    sid = _vault(tmp_path)
    _enrich(tmp_path, Model())
    _items(tmp_path, Model())
    path = tmp_path / "normalized" / "enrichment" / f"{sid}.items.json"
    bad = json.loads(path.read_text())
    bad["raw_items"] = [{"name": 3}]
    bad["merge_input"] = "stale"
    path.write_text(json.dumps(bad))
    assert _items(tmp_path, Model(fail_items=True), cached=False)["replan_unavailable_sources"] == [sid]


def test_a_merge_version_change_re_plans_without_a_model_call(tmp_path, monkeypatch):
    sid = _vault(tmp_path)
    _enrich(tmp_path, Model())
    _items(tmp_path, Model())
    monkeypatch.setattr(items, "MERGE_VERSION", "enrich-merge-v999")
    out = _items(tmp_path, Model(fail_items=True), cached=False)
    assert out["replanned_sources"] == [sid]
    assert _artifact(tmp_path, sid)["merge_version"] == "enrich-merge-v999"


def test_a_merge_input_changed_during_the_items_call_writes_nothing(tmp_path):
    sid = _vault(tmp_path)
    _enrich(tmp_path, Model())

    class Meanwhile(Model):
        def parse(self, messages, schema, model_id, *, max_tokens):
            out = super().parse(messages, schema, model_id, max_tokens=max_tokens)
            if schema is prompts.ITEMS_SCHEMA:
                _enrich(tmp_path, Model(tags=[_tag("Reporting", "use_case")]))
            return out
    out = _items(tmp_path, Meanwhile(), cached=False)
    assert out["input_changed_sources"] == [sid]
    assert not (tmp_path / "normalized" / "enrichment" / f"{sid}.items.json").exists()


def test_an_interrupted_re_plan_replays_from_the_raw_list(tmp_path, monkeypatch):
    sid = _vault(tmp_path)
    _enrich(tmp_path, Model())
    _items(tmp_path, Model())
    _enrich(tmp_path, Model(tags=[_tag("Reporting", "use_case")]))
    real = items._write_artifact
    monkeypatch.setattr(items, "_write_artifact",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        _items(tmp_path, Model(fail_items=True), cached=False)
    monkeypatch.setattr(items, "_write_artifact", real)
    out = _items(tmp_path, Model(fail_items=True), cached=False)     # journal record outstanding
    assert out["replanned_sources"] == [sid] and out["unfinished_commits"] == []
    assert "Reporting" in _suggested(tmp_path, sid)


# --- keywords: (slice 2 Q3) ----------------------------------------------------------------------


def _gen(tmp_path):
    wiki.generate_wiki(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite", templates_dir=TEMPLATES,
                       rebuild_index=False, force=True)


def _hits(tmp_path, term):
    from app.backend import search
    conn = keyword_index.connect(tmp_path / keyword_index.DB_RELPATH)
    try:
        rows = search.search_navigation(conn, search.build_fts_match([term], op="OR"),
                                        page_type="source", node_statuses=("active",),
                                        language=None, prefusion_limit=50, limit=50)
    finally:
        conn.close()
    return {r["path"] for r in rows}


def test_keywords_list_kept_and_eligible_suggestions_and_feed_search(tmp_path, api):
    sid = _vault(tmp_path)
    _enrich(tmp_path, Model(tags=[_tag("Banking"), _tag("Securitization", "use_case")]))
    _items(tmp_path, Model())
    _act(api, sid, "Retrieval", "keep")
    _act(api, sid, "Banking", "drop")
    page = tmp_path / "wiki" / "Sources" / f"{sid}.md"
    fm = parse_frontmatter(page.read_text(encoding="utf-8"))
    assert "tags" not in fm
    assert fm["keywords"][0] == "Retrieval"                          # kept first
    assert set(fm["keywords"]) == {"Retrieval", "Securitization", "Large Language Model"}
    keyword_index.reindex(tmp_path)
    source_page = f"wiki/Sources/{sid}.md"
    assert source_page in _hits(tmp_path, "securitization")          # a keyword-only match
    assert source_page not in _hits(tmp_path, "banking")              # a dropped name is gone
    _act(api, sid, "Securitization", "drop")                          # publication reindexes
    assert source_page not in _hits(tmp_path, "securitization")


def test_the_index_prefers_keywords_even_empty_and_falls_back_to_legacy_tags(tmp_path):
    fm_source = {"keywords": [], "tags": ["legacy"]}
    assert keyword_index._search_terms("source", fm_source) == []
    assert keyword_index._search_terms("source", {"tags": ["legacy"]}) == ["legacy"]
    assert keyword_index._search_terms("source", {"keywords": ["new"], "tags": ["old"]}) == ["new"]
    assert keyword_index._search_terms("item", {"keywords": ["x"], "tags": ["t"]}) == ["t"]


# --- review round 1 on slice 2 -------------------------------------------------------------------

LLM, LLM_LONG = items.node_id("LLM"), items.node_id("Large Language Model")


def _governed_plan(gconn, *, decided=None, keys=None, rejected=frozenset(), statuses=None):
    for nid, slug in ((LLM, "llm"), (LLM_LONG, "large-language-model")):
        status = (statuses or {}).get(nid)
        if status:
            graph.upsert_node(gconn, node_id=nid, node_type="item", slug=slug, status=status,
                              item_type="model")
    vocab = {"keys": keys or {}, "titles": {LLM: "LLM", LLM_LONG: "Large Language Model"},
             "decided": decided or {}, "decided_keys": {}}
    return items.plan_suggestions(
        [_it("Large Language Model", aliases=["LLM"])], md=BODY, gconn=gconn, vocab=vocab,
        rejected_pairs=set(rejected), dropped=(set(), set()), blocked_keys=set(), kept_ids=set(),
        cap=15, enrich_tags=[_tag("LLM", "model")])


@pytest.mark.parametrize("effect", ["hidden", "retired"])
def test_a_hidden_or_retired_enrich_form_is_never_merged_into_a_suggestion(gconn, effect):
    plan = _governed_plan(gconn, decided={LLM: (effect, None)},
                          statuses={LLM: "hidden" if effect == "hidden" else "deprecated_candidate"})
    # The governed form is filtered on its own; the items suggestion stands alone, never "both".
    assert [(e["name"], e["origin"]) for e in plan["emit"]] == [("Large Language Model", "items")]
    assert [(f["name"], f["reason"]) for f in plan["filtered"]] == [("LLM", effect)]


def test_a_redirected_enrich_form_follows_its_merge_not_the_items_name(gconn):
    survivor = items.node_id("Language Model")
    graph.upsert_node(gconn, node_id=survivor, node_type="item", slug="language-model",
                      status="active", item_type="model")
    plan = _governed_plan(gconn, decided={LLM: ("redirected", survivor)}, statuses={LLM: "merged"})
    assert [(e["node_id"], e["origin"], e["resolution"]) for e in plan["emit"]] == [
        (survivor, "enrich", "redirected"), (LLM_LONG, "items", "new")]


def test_a_rejected_merge_pair_never_collapses_through_an_alias(gconn):
    plan = _governed_plan(
        gconn, statuses={LLM: "active", LLM_LONG: "active"},
        keys={tag_links.tag_key("LLM"): [LLM], tag_links.tag_key("Large Language Model"): [LLM_LONG]},
        rejected={frozenset((LLM, LLM_LONG))})
    assert [(e["node_id"], e["origin"]) for e in plan["emit"]] == [(LLM, "enrich"),
                                                                   (LLM_LONG, "items")]


class Offline(Model):
    def available(self):
        return False


def test_a_provider_free_re_plan_reports_its_work(tmp_path, monkeypatch):
    sid = _vault(tmp_path)
    _enrich(tmp_path, Model())
    _items(tmp_path, Model())
    (tmp_path / "db" / "llm_cache.sqlite").unlink()
    _enrich(tmp_path, Model(tags=[_tag("Reporting", "use_case")]))
    out = _items(tmp_path, Offline(fail_items=True), cached=False)
    assert out["replanned_sources"] == [sid] and out["status"] == "succeeded"
    assert "Reporting" in _suggested(tmp_path, sid)

    _enrich(tmp_path, Model(tags=[_tag("Securitization", "use_case")]))

    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(keyword_index, "reindex", broken)
    out = _items(tmp_path, Offline(fail_items=True), cached=False)
    assert out["replanned_sources"] == [sid]
    assert out["status"] == "saved_publication_pending"


def test_an_offline_run_with_nothing_to_re_plan_is_skipped(tmp_path):
    _vault(tmp_path)
    out = _items(tmp_path, Offline(fail_items=True), cached=False)
    assert out["status"] == "skipped" and out["replanned_sources"] == []


def test_the_printed_recovery_command_recovers_a_legacy_artifact(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(ROOT / "scripts"))
    import extract_items as script
    sid = _vault(tmp_path)
    _enrich(tmp_path, Model())
    _items(tmp_path, Model())
    path = tmp_path / "normalized" / "enrichment" / f"{sid}.items.json"
    legacy = json.loads(path.read_text())
    for key in ("raw_items", "merge_input", "merge_version"):
        legacy.pop(key)
    path.write_text(json.dumps(legacy))
    monkeypatch.setenv("ENRICH_MODEL_STANDARD", MODEL_REF)          # the model the artifact used
    monkeypatch.setattr(script, "get_settings", lambda: get_settings(tmp_path))
    monkeypatch.setattr(script, "build_client",
                        lambda settings, cache: LLMClient({"anthropic": Offline(fail_items=True)},
                                                          cache=cache))
    script.main([])
    printed = capsys.readouterr().out
    command = next(line.split("recover it with: ", 1)[1] for line in printed.splitlines()
                   if "recover it with: " in line)
    assert command == script.RECOVER_COMMAND.format(sid=sid)
    script.main(command.split("scripts/extract_items.py", 1)[1].split())
    assert _artifact(tmp_path, sid)["raw_items"] == Model().raw     # recovered from the cache


def test_a_rejected_pair_holds_when_the_enrich_form_is_an_unreviewed_candidate(gconn):
    # "LLM" is only a candidate node (so it resolves as `existing`, ungoverned), but a human
    # rejected merging it with the active "Large Language Model": the alias must not merge them.
    plan = _governed_plan(
        gconn, statuses={LLM: "candidate", LLM_LONG: "active"},
        keys={tag_links.tag_key("Large Language Model"): [LLM_LONG]},
        rejected={frozenset((LLM, LLM_LONG))})
    assert [(e["node_id"], e["origin"]) for e in plan["emit"]] == [(LLM, "enrich"),
                                                                   (LLM_LONG, "items")]
