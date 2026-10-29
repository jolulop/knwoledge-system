"""ADR-0066 part 3, slice 1: the effective title, the PDF metadata hint and Enrich's new outputs.

Covers the deterministic PDF-title filter, the title chains (effective and prompt), the hint in the
extraction log, Enrich's suggestions in its artifact (implementation grill 2 Q1), the Source page and
review screens showing the effective title, and claims/items prompted and restaled by the Enrich
title but never by a confirmed (display-only) one.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pypdf import PdfReader, PdfWriter  # noqa: E402

from app.backend import manifests, taxonomy  # noqa: E402
from app.llm import prompts  # noqa: E402
from app.llm.cache import ResponseCache  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.workers import claims, enrich, extract, intake, items, titles, wiki  # noqa: E402
from app.workers.wiki_render import parse_frontmatter  # noqa: E402
from tests import fixtures  # noqa: E402
from tests.test_claims import Q1  # noqa: E402

MODEL_REF = "anthropic:claude-haiku-4-5"
TEMPLATES = ROOT / "templates"


class Router:
    """Answers the Enrich, claims and items passes; records every user prompt per pass."""
    name = "anthropic"
    supports_batch = False

    def __init__(self, enrich_result=None):
        self.prompts: dict[str, list[str]] = {"enrich": [], "claims": [], "items": []}
        self.enrich_result = enrich_result or _enrich_result()

    def available(self):
        return True

    def parse(self, messages, schema, model_id, *, max_tokens):
        user = messages[-1]["content"]
        if schema is prompts.SUMMARY_TAGS_SCHEMA:
            self.prompts["enrich"].append(user)
            return json.loads(json.dumps(self.enrich_result))
        if schema is prompts.CLAIMS_SCHEMA:
            self.prompts["claims"].append(user)
            return {"claims": [{"claim": "The document has a first paragraph.", "quote": Q1}]}
        self.prompts["items"].append(user)
        return {"items": [{"name": "Testing", "item_type": "domain", "aliases": []}]}


def _enrich_result(**over):
    out = {"title": "The Real Title of the Document", "authors": ["Ada Lovelace", "Alan Turing"],
           "publisher": "Example Press", "summary": "A faithful summary.",
           "tags": [{"name": "planning-taxonomy", "item_type": "method_technique"}]}
    out.update(over)
    return out


def _vault(tmp_path, *, pdf_meta=None):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.copy_templates(tmp_path)
    if pdf_meta is None:
        fixtures.write_markdown(inbox / "1_s2.0_S0306437926000529_main.md")
    else:
        _pdf(inbox / "Day_1_v3.pdf", pdf_meta)
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    extract.extract_sources(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    (sid,) = [m["source_id"] for m in manifests.list_manifests(tmp_path / "raw" / "manifests")]
    return sid


def _pdf(path: Path, meta: dict[str, str]) -> None:
    path.write_bytes(fixtures.make_pdf_bytes(["A first page with enough real text to extract.",
                                              "A second page continues the argument."]))
    writer = PdfWriter(clone_from=PdfReader(str(path)))
    writer.add_metadata(meta)
    with path.open("wb") as fh:
        writer.write(fh)


def _client(tmp_path, adapter):
    return LLMClient({"anthropic": adapter}, cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite"))


def _run(tmp_path, adapter, *stages, cached=True, **kw):
    """Run the given stages. `cached=False` uses a client without the response cache, so a
    forced re-run asks the model again instead of replaying the earlier answer."""
    out = {}
    for stage in stages:
        fn = {"enrich": enrich.enrich_sources, "claims": claims.extract_claims,
              "items": items.extract_items}[stage]
        client = _client(tmp_path, adapter) if cached else LLMClient({"anthropic": adapter})
        out[stage] = fn(tmp_path, client=client, model_ref=MODEL_REF,
                        jobs_db=tmp_path / "db" / "jobs.sqlite", **kw)
    return out


def _gen(tmp_path):
    return wiki.generate_wiki(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite",
                              templates_dir=TEMPLATES, rebuild_index=False, force=True)


def _artifact(tmp_path, sid, suffix=""):
    return json.loads((tmp_path / "normalized" / "enrichment" / f"{sid}{suffix}.json")
                      .read_text(encoding="utf-8"))


def _set_confirmed_title(tmp_path, sid, title):
    m = manifests.load_manifest(tmp_path / "raw" / "manifests", sid)
    m["provenance"] = {**(m.get("provenance") or {}), "title": title}
    manifests.save_manifest(tmp_path / "raw" / "manifests", m)


# --- the deterministic PDF-title filter ----------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("Day_1_v3.pdf", None),                                    # a filename
    ("Day_1_v3", None),                                        # filename-like, no spaces
    ("(6) Planning Under Uncertainty", "Planning Under Uncertainty"),   # browser-tab prefix
    ("Microsoft Word - notes.docx", None),
    ("1 s2.0 S0306437926000529 main", None),                   # only repeats the filename
    ("Untitled", None),
    ("", None),
    ("  Knowledge Graphs:\n a  Survey ", "Knowledge Graphs: a Survey"),
])
def test_the_pdf_title_filter(raw, expected):
    assert titles.filter_pdf_title(raw, "1_s2.0-S0306437926000529-main.pdf") == expected


def test_the_title_chains():
    m = {"source_id": "src_0000000000000001", "original_filename": "file_name.pdf"}
    hint = {"title": "A Good Embedded Title"}
    assert titles.effective_title(m, enrichment=None, hint={}) == "file name"
    assert titles.effective_title(m, enrichment=None, hint=hint) == "A Good Embedded Title"
    assert titles.effective_title(m, enrichment={"title": "Enrich Title"}, hint=hint) == "Enrich Title"
    confirmed = {**m, "provenance": {"title": "Confirmed Title"}}
    assert titles.effective_title(confirmed, enrichment={"title": "Enrich Title"},
                                  hint=hint) == "Confirmed Title"
    # The prompt chain never takes the confirmed title (part 3 Q2).
    assert titles.prompt_title(confirmed, enrichment={"title": "Enrich Title"},
                               hint=hint) == "Enrich Title"
    assert titles.prompt_title(confirmed, enrichment=None, hint={}) == "file name"


def test_name_identity_keeps_plurals_accents_and_scripts():
    assert titles.name_identity("Robin  Woods") != titles.name_identity("Robin Wood")
    assert titles.name_identity("JOSÉ García") == titles.name_identity("josé garcía")
    assert titles.name_identity("José") != titles.name_identity("Jose")
    assert titles.name_identity("山田 太郎") == "山田 太郎"


# --- extraction records the hint -----------------------------------------------------------------


def test_extraction_records_the_pdf_metadata_hint(tmp_path):
    sid = _vault(tmp_path, pdf_meta={"/Title": "(3) Planning Under Uncertainty",
                                     "/Author": "Ada Lovelace"})
    log = json.loads((tmp_path / "normalized" / "extraction_logs" / f"{sid}.json").read_text())
    assert log["metadata"] == {"title": "(3) Planning Under Uncertainty", "author": "Ada Lovelace"}
    assert log["extract_code_version"] == extract.EXTRACT_CODE_VERSION == 2
    assert titles.read_metadata_hint(tmp_path / "normalized" / "extraction_logs", sid) == \
        log["metadata"]


def test_a_pdf_without_metadata_records_an_empty_hint(tmp_path):
    sid = _vault(tmp_path, pdf_meta={})
    log = json.loads((tmp_path / "normalized" / "extraction_logs" / f"{sid}.json").read_text())
    assert log["metadata"] == {}


# --- Enrich: the hint as input, suggestions in the artifact (grill 2 Q1) --------------------------


def test_enrich_sees_the_hint_and_stores_cleaned_suggestions(tmp_path):
    sid = _vault(tmp_path, pdf_meta={"/Title": "Planning Under Uncertainty", "/Author": "A. L."})
    router = Router(_enrich_result(
        authors=["Ada Lovelace", " ada  lovelace ", "", "Alan Turing"],
        tags=[{"name": "planning-taxonomy", "item_type": "method_technique"},
              {"name": "Planning taxonomy", "item_type": "domain"}]
        + [{"name": f"tag {i}", "item_type": "domain"} for i in range(10)]))
    _run(tmp_path, router, "enrich")
    (prompt,) = router.prompts["enrich"]
    assert "Embedded title: Planning Under Uncertainty" in prompt and "Embedded author: A. L." in prompt
    a = _artifact(tmp_path, sid)
    assert a["title"] == "The Real Title of the Document"
    assert a["authors"] == ["Ada Lovelace", "Alan Turing"]          # ordered, de-duplicated
    assert a["publisher"] == "Example Press"
    assert a["tags"][0] == {"name": "planning taxonomy", "item_type": "method_technique"}
    assert len(a["tags"]) == prompts.ENRICH_TAGS_PROMPT_MAX == 7    # capped, duplicates folded
    assert "provenance_suggested" not in json.dumps(
        manifests.load_manifest(tmp_path / "raw" / "manifests", sid))


def test_unknown_values_and_bad_types_are_stored_safely(tmp_path):
    sid = _vault(tmp_path)
    _run(tmp_path, Router(_enrich_result(title="", authors=[], publisher="",
                                         tags=[{"name": "x y", "item_type": "domain"}])), "enrich")
    a = _artifact(tmp_path, sid)
    assert a["title"] is None and a["authors"] == [] and a["publisher"] is None
    stored = enrich._suggestions({"summary": "s", "title": "t", "authors": [], "publisher": "",
                                  "tags": [{"name": "z", "item_type": "nonsense"}]})
    assert stored["tags"] == [{"name": "z", "item_type": taxonomy.UNCLASSIFIED}]


def test_re_enrichment_never_touches_confirmed_provenance(tmp_path):
    sid = _vault(tmp_path)
    _run(tmp_path, Router(), "enrich")
    m = manifests.load_manifest(tmp_path / "raw" / "manifests", sid)
    m["provenance"] = {"title": "Mine", "author": ["Grace Hopper"], "publisher": "Navy"}
    manifests.save_manifest(tmp_path / "raw" / "manifests", m)
    _run(tmp_path, Router(_enrich_result(title="Other", authors=["Someone Else"])), "enrich",
         force=True, cached=False)
    after = manifests.load_manifest(tmp_path / "raw" / "manifests", sid)
    assert after["provenance"] == {"title": "Mine", "author": ["Grace Hopper"], "publisher": "Navy"}
    assert _artifact(tmp_path, sid)["title"] == "Other"


def test_a_failed_enrich_keeps_the_previous_artifact(tmp_path):
    sid = _vault(tmp_path)
    _run(tmp_path, Router(), "enrich")
    before = _artifact(tmp_path, sid)
    bad = Router({"summary": "missing the new fields"})
    out = _run(tmp_path, bad, "enrich", force=True, cached=False)["enrich"]
    assert out["errors"] == 1 and _artifact(tmp_path, sid) == before


def test_a_new_pdf_hint_restales_enrich(tmp_path):
    sid = _vault(tmp_path, pdf_meta={})
    router = Router()
    _run(tmp_path, router, "enrich")
    _run(tmp_path, router, "enrich")
    assert len(router.prompts["enrich"]) == 1                      # fresh: skipped
    log_path = tmp_path / "normalized" / "extraction_logs" / f"{sid}.json"
    log = json.loads(log_path.read_text())
    log["metadata"] = {"title": "Planning Under Uncertainty"}      # a re-extraction found one
    log_path.write_text(json.dumps(log))
    _run(tmp_path, router, "enrich")
    assert len(router.prompts["enrich"]) == 2


# --- the effective title on every surface --------------------------------------------------------


def test_the_source_page_and_review_screen_show_the_effective_title(tmp_path, monkeypatch):
    sid = _vault(tmp_path)
    _run(tmp_path, Router(), "enrich")
    _gen(tmp_path)
    page = (tmp_path / "wiki" / "Sources" / f"{sid}.md").read_text(encoding="utf-8")
    assert parse_frontmatter(page)["title"] == "The Real Title of the Document"
    assert "# The Real Title of the Document" in page
    _set_confirmed_title(tmp_path, sid, "My Corrected Title")
    _gen(tmp_path)
    page = (tmp_path / "wiki" / "Sources" / f"{sid}.md").read_text(encoding="utf-8")
    assert parse_frontmatter(page)["title"] == "My Corrected Title"

    from fastapi.testclient import TestClient

    from app.backend import graph
    from app.backend import main as main_module
    from app.backend.config import get_settings
    graph.init_db(tmp_path / "db" / "graph.sqlite")              # the per-source flow needs it
    monkeypatch.setattr(main_module, "settings", get_settings(tmp_path))
    client = TestClient(main_module.app)
    assert "My Corrected Title" in client.get("/ui/reviews/sources").text
    assert "<h1>My Corrected Title</h1>" in client.get(f"/ui/reviews/sources/{sid}").text


# --- claims and items take the Enrich title (part 3 Q2) ------------------------------------------


def test_claims_and_items_are_prompted_with_the_enrich_title(tmp_path):
    sid = _vault(tmp_path)
    router = Router()
    _run(tmp_path, router, "enrich", "claims", "items")
    assert all("Title: The Real Title of the Document" in p
               for p in router.prompts["claims"] + router.prompts["items"])
    assert _artifact(tmp_path, sid, ".claims")["prompt_input"] == "The Real Title of the Document"
    assert _artifact(tmp_path, sid, ".items")["prompt_input"] == "The Real Title of the Document"


def test_without_enrich_the_prompt_title_is_the_filtered_fallback(tmp_path):
    _vault(tmp_path)
    router = Router()
    _run(tmp_path, router, "claims")
    assert "Title: 1 s2.0 S0306437926000529 main" in router.prompts["claims"][0]


def test_a_confirmed_title_never_restales_claims_or_items(tmp_path):
    sid = _vault(tmp_path)
    router = Router()
    _run(tmp_path, router, "enrich", "claims", "items")
    calls = {k: len(v) for k, v in router.prompts.items()}
    _set_confirmed_title(tmp_path, sid, "A Display Correction")
    out = _run(tmp_path, router, "claims", "items")
    assert {k: len(v) for k, v in router.prompts.items()} == calls          # no new model calls
    assert out["claims"]["skipped_fresh"] == 1 and out["items"]["skipped_fresh"] == 1


def test_an_enrich_title_change_restales_claims_and_items(tmp_path):
    sid = _vault(tmp_path)
    _run(tmp_path, Router(), "enrich", "claims", "items")
    router = Router(_enrich_result(title="A Different Real Title"))
    _run(tmp_path, router, "enrich", force=True, cached=False)
    _run(tmp_path, router, "claims", "items")
    assert router.prompts["claims"] and router.prompts["items"]
    assert "Title: A Different Real Title" in router.prompts["items"][0]
    assert _artifact(tmp_path, sid, ".claims")["prompt_input"] == "A Different Real Title"


def test_lint_summary_rot_agrees_with_enrich_on_the_hint(tmp_path):
    from app.workers import lint
    sid = _vault(tmp_path, pdf_meta={"/Title": "Planning Under Uncertainty"})
    _run(tmp_path, Router(), "enrich")

    def rot():
        findings, _ = lint._check_summary_rot(
            tmp_path / "normalized" / "enrichment", tmp_path / "normalized" / "markdown",
            tmp_path / "wiki", MODEL_REF, tmp_path / "raw" / "manifests")
        return [f["subject"] for f in findings if f["check"] == "summary_rot"]
    assert rot() == []                                         # fresh, hint included
    log_path = tmp_path / "normalized" / "extraction_logs" / f"{sid}.json"
    log = json.loads(log_path.read_text())
    log["metadata"] = {"title": "Another Title"}
    log_path.write_text(json.dumps(log))
    assert rot() == [sid]                                      # Enrich would regenerate it


def test_a_junk_pdf_title_falls_through_to_the_filename():
    m = {"source_id": "src_0000000000000001", "original_filename": "Day_1_v3.pdf"}
    for junk in ("Day_1_v3.pdf", "Microsoft Word - x.docx", "Untitled"):
        assert titles.prompt_title(m, enrichment=None, hint={"title": junk}) == "Day 1 v3"
        assert titles.effective_title(m, enrichment=None, hint={"title": junk}) == "Day 1 v3"


# --- review round 1 on slice 1 -------------------------------------------------------------------


def _set_hint(tmp_path, sid, hint):
    log_path = tmp_path / "normalized" / "extraction_logs" / f"{sid}.json"
    log = json.loads(log_path.read_text())
    log["metadata"] = hint
    log_path.write_text(json.dumps(log))


class MidCall(Router):
    """Changes a prompt input (never the text) while one pass's model call runs."""

    def __init__(self, during, change, **kw):
        super().__init__(**kw)
        self.during, self.change, self.done = during, change, False

    def parse(self, messages, schema, model_id, *, max_tokens):
        out = super().parse(messages, schema, model_id, max_tokens=max_tokens)
        stage = {id(prompts.SUMMARY_TAGS_SCHEMA): "enrich",
                 id(prompts.CLAIMS_SCHEMA): "claims"}.get(id(schema), "items")
        if stage == self.during and not self.done:
            self.done = True
            self.change()
        return out


def test_a_hint_changed_during_the_enrich_call_writes_nothing(tmp_path):
    sid = _vault(tmp_path, pdf_meta={"/Title": "Planning Under Uncertainty"})
    router = MidCall("enrich", lambda: _set_hint(tmp_path, sid, {"title": "A Newer Hint"}))
    out = _run(tmp_path, router, "enrich")["enrich"]
    assert out["enriched"] == 0 and out["skipped_input_changed"] == 1
    assert not (tmp_path / "normalized" / "enrichment" / f"{sid}.json").exists()


@pytest.mark.parametrize("stage", ["claims", "items"])
def test_a_title_changed_during_the_call_writes_nothing(tmp_path, stage):
    sid = _vault(tmp_path, pdf_meta={"/Title": "Planning Under Uncertainty"})
    router = MidCall(stage, lambda: _set_hint(tmp_path, sid, {"title": "Another Real Title"}))
    out = _run(tmp_path, router, stage)[stage]
    assert not (tmp_path / "normalized" / "enrichment" / f"{sid}.{stage}.json").exists()
    if stage == "claims":
        assert out["skipped_input_changed"] == 1
    else:
        assert out["input_changed_sources"] == [sid]


def test_an_enrich_title_changed_during_the_items_call_writes_nothing(tmp_path):
    sid = _vault(tmp_path)
    _run(tmp_path, Router(), "enrich")

    def re_enrich():
        _run(tmp_path, Router(_enrich_result(title="Retitled Meanwhile")), "enrich", force=True,
             cached=False)
    out = _run(tmp_path, MidCall("items", re_enrich), "items")["items"]
    assert out["input_changed_sources"] == [sid]
    assert not (tmp_path / "normalized" / "enrichment" / f"{sid}.items.json").exists()


def test_rendering_rejects_an_enrich_artifact_whose_hint_changed(tmp_path):
    sid = _vault(tmp_path, pdf_meta={"/Title": "Planning Under Uncertainty"})
    _run(tmp_path, Router(), "enrich")
    _gen(tmp_path)
    page = tmp_path / "wiki" / "Sources" / f"{sid}.md"
    assert parse_frontmatter(page.read_text())["title"] == "The Real Title of the Document"
    _set_hint(tmp_path, sid, {"title": "(2) A Newer Embedded Title"})
    _gen(tmp_path)
    fm = parse_frontmatter(page.read_text())
    assert fm["title"] == "A Newer Embedded Title"             # the filtered fallback
    assert fm["summary_status"] == "stub"                      # the stale artifact is not used
    md = (tmp_path / "normalized" / "markdown" / f"{sid}.md").read_text()
    assert titles.prompt_title_for(
        manifests.load_manifest(tmp_path / "raw" / "manifests", sid), md,
        enrichment_dir=tmp_path / "normalized" / "enrichment",
        logs_dir=tmp_path / "normalized" / "extraction_logs") == "A Newer Embedded Title"


@pytest.mark.parametrize("source", ["enrich", "pdf"])
def test_a_title_cannot_assert_a_wikilink(tmp_path, source):
    linked = "See [[itm_0123456789abcdef|A source title]] and [[Items/x]]"
    if source == "enrich":
        sid = _vault(tmp_path)
        _run(tmp_path, Router(_enrich_result(title=linked)), "enrich")
    else:
        sid = _vault(tmp_path, pdf_meta={"/Title": linked})
    _gen(tmp_path)
    text = (tmp_path / "wiki" / "Sources" / f"{sid}.md").read_text(encoding="utf-8")
    assert "[[itm_" not in text and "[[Items/x]]" not in text
    assert parse_frontmatter(text)["title"] == "See A source title and Items/x"
    assert "# See A source title and Items/x" in text
