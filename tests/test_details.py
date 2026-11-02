"""ADR-0066 part 3 slice 3: the document screen's Details confirmation.

Covers the form-level refusals and the baseline token (slice-3 grill Q1), the draft-only actions and
the no-JavaScript form rules (Q4), the confirmed provenance it writes (Q2) and Clear.
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

from app.backend import main as main_module  # noqa: E402
from app.backend import manifests, review_read  # noqa: E402
from app.backend.config import get_settings  # noqa: E402
from app.llm import prompts  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.workers import enrich, extract, intake, items, tag_links  # noqa: E402
from app.workers.wiki_render import parse_frontmatter  # noqa: E402
from tests import fixtures  # noqa: E402

MODEL_REF = "anthropic:claude-haiku-4-5"
BODY = "# Planning\n\nA document about planning taxonomy and retrieval.\n"


class Model:
    name = "anthropic"
    supports_batch = False

    def __init__(self, authors=("Ada Lovelace", "Alan Turing"), publisher="Example Press"):
        self.authors, self.publisher = list(authors), publisher

    def available(self):
        return True

    def parse(self, messages, schema, model_id, *, max_tokens):
        if schema is prompts.SUMMARY_TAGS_SCHEMA:
            return {"title": "Planning Under Uncertainty", "authors": self.authors,
                    "publisher": self.publisher, "summary": "A summary.",
                    "tags": [{"name": "Planning", "item_type": "domain"}]}
        return {"items": [{"name": "Retrieval", "item_type": "method_technique", "aliases": []}]}


def _doc(tmp_path, name="doc.md"):
    (tmp_path / "raw" / "inbox" / name).write_text(BODY.replace("Planning", name), encoding="utf-8")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    (tmp_path / "raw" / "inbox").mkdir(parents=True)
    fixtures.copy_templates(tmp_path)
    _doc(tmp_path)
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    extract.extract_sources(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    for fn in (enrich.enrich_sources, items.extract_items):
        fn(tmp_path, client=LLMClient({"anthropic": Model()}), model_ref=MODEL_REF,
           jobs_db=tmp_path / "db" / "jobs.sqlite")
    monkeypatch.setattr(main_module, "settings", get_settings(tmp_path))
    (m,) = manifests.list_manifests(tmp_path / "raw" / "manifests")
    return tmp_path, m["source_id"]


@pytest.fixture
def client(vault):
    return TestClient(main_module.app)


def _details(root, sid):
    return review_read.details_section(root / "reviews", sid, manifests_dir=root / "raw" / "manifests",
                                       enrichment_dir=root / "normalized" / "enrichment")["details"]


def _form(root, sid, *, authors=None, title=None, publisher=..., tick=False, **extra):
    """The form as the screen displayed it, with the given fields changed."""
    d = _details(root, sid)
    shown = d["prefill"]
    form = {"details_title": shown["title"] if title is None else title,
            "details_shown": json.dumps(shown), "details_baseline": d["baseline"]}
    rows = shown["authors"] if authors is None else authors
    for i, a in enumerate(rows):
        form[f"author_row_{i}"] = "1"
        form[f"author_name_{i}"] = a["name"]
        form[f"author_order_{i}"] = str(a.get("order", i))
        if a.get("show_page"):
            form[f"author_show_{i}"] = "1"
    pub = shown["publisher"] if publisher is ... else publisher
    if pub:
        form["publisher_name"] = pub["name"]
        if pub.get("show_page"):
            form["publisher_show"] = "1"
    if tick:
        form["details_confirm"] = "1"
    form.update(extra)
    return form


def _prov(root, sid):
    return manifests.load_manifest(root / "raw" / "manifests", sid).get("provenance") or {}


def _post(client, sid, form):
    return client.post(f"/ui/reviews/sources/{sid}/decide", data=form)


def _a(*names, show=False):
    return [{"name": n, "show_page": show} for n in names]


# --- the section and its prefill -----------------------------------------------------------------


def test_the_section_is_prefilled_from_enrich(client, vault):
    root, sid = vault
    html = client.get(f"/ui/reviews/sources/{sid}").text
    assert "Not confirmed: Suggestions from Enrich." in html
    assert "value='Planning Under Uncertainty'" in html and "value='Ada Lovelace'" in html
    assert "name='details_confirm'" in html and "name='details_clear'" not in html


def test_a_valid_confirmation_writes_provenance_and_the_page_title(client, vault):
    root, sid = vault
    form = _form(root, sid, title="Planning, Revisited", tick=True,
                 authors=_a("Alan Turing", "Ada Lovelace") + [{"name": "", "show_page": False}],
                 publisher={"name": "Example Press", "show_page": True})
    baseline = form["details_baseline"]
    assert _post(client, sid, form).status_code == 200
    prov = _prov(root, sid)
    assert prov["title"] == "Planning, Revisited"
    assert prov["authors"] == _a("Alan Turing", "Ada Lovelace")     # blank row dropped, order kept
    assert prov["publisher"] == {"name": "Example Press", "show_page": True}
    assert prov["confirmed_baseline"] == baseline and prov["confirmed_at"]
    page = (root / "wiki" / "Sources" / f"{sid}.md").read_text(encoding="utf-8")
    assert parse_frontmatter(page)["title"] == "Planning, Revisited"
    html = client.get(f"/ui/reviews/sources/{sid}").text
    assert "Details confirmed on" in html and "name='details_clear'" in html


# --- form-level refusals (Q1) --------------------------------------------------------------------


def test_details_edited_without_the_tick_are_refused_and_nothing_is_written(client, vault):
    root, sid = vault
    retrieval = items.node_id("Retrieval")
    before = (json.dumps(_prov(root, sid)), tag_links.load_ledger(root / "reviews", sid))
    resp = _post(client, sid, _form(root, sid, title="An Edit", **{f"link_{retrieval}": "keep"}))
    assert resp.status_code == 409 and "tick “These details are correct”" in resp.text
    assert "Nothing was saved" in resp.text and "value='An Edit'" in resp.text
    assert f"name='link_{retrieval}' value='keep' checked" in resp.text   # selection preserved
    assert (json.dumps(_prov(root, sid)), tag_links.load_ledger(root / "reviews", sid)) == before


def test_a_tags_only_save_after_a_concurrent_details_change_is_accepted(client, vault):
    root, sid = vault
    form = _form(root, sid, **{f"link_{items.node_id('Retrieval')}": "keep"})   # Details untouched
    manifests.confirm_details(root / "raw" / "manifests", sid, title="Someone Else's",
                              authors=_a("Grace Hopper"), publisher=None, baseline="x" * 16,
                              now="2026-10-30T00:00:00Z")
    assert _post(client, sid, form).status_code == 200
    assert items.node_id("Retrieval") in tag_links.load_ledger(root / "reviews", sid)["kept"]
    assert _prov(root, sid)["title"] == "Someone Else's"          # never overwritten


def test_a_same_fingerprint_enrich_change_makes_a_details_save_stale(client, vault):
    root, sid = vault
    form = _form(root, sid, tick=True)
    path = root / "normalized" / "enrichment" / f"{sid}.json"
    artifact = json.loads(path.read_text())
    artifact["authors"] = ["Someone Different"]                    # same input fingerprint
    path.write_text(json.dumps(artifact))
    resp = _post(client, sid, form)
    assert resp.status_code == 409 and "changed after this page was shown" in resp.text
    assert "authors" not in _prov(root, sid)


def test_invalid_details_are_refused(client, vault):
    root, sid = vault
    for form in (_form(root, sid, title="  ", tick=True),
                 _form(root, sid, authors=_a("Ada Lovelace", "ada  lovelace"), tick=True)):
        resp = _post(client, sid, form)
        assert resp.status_code == 409 and "Nothing was saved" in resp.text
    assert "authors" not in _prov(root, sid)


def test_a_valid_details_save_with_a_skipped_tag_row(client, vault):
    root, sid = vault
    stray = items.node_id("Not On This Document")
    resp = _post(client, sid, _form(root, sid, tick=True, **{f"link_{stray}": "keep"}))
    assert resp.status_code == 200 and "not_attributable_to_source" in resp.text
    assert _prov(root, sid)["authors"] == _a("Ada Lovelace", "Alan Turing")


# --- draft actions and the no-JavaScript rules (Q4) ----------------------------------------------


@pytest.mark.parametrize("action", ["add_rows", "use_suggestion"])
def test_draft_actions_write_nothing_and_keep_tag_selections(client, vault, action):
    root, sid = vault
    retrieval = items.node_id("Retrieval")
    before = (json.dumps(_prov(root, sid)), tag_links.load_ledger(root / "reviews", sid))
    resp = _post(client, sid, _form(root, sid, title="Draft Title", tick=True,
                                    details_action=action, **{f"link_{retrieval}": "keep"}))
    assert resp.status_code == 200
    assert f"name='link_{retrieval}' value='keep' checked" in resp.text
    assert (json.dumps(_prov(root, sid)), tag_links.load_ledger(root / "reviews", sid)) == before
    if action == "add_rows":
        assert "value='Draft Title'" in resp.text and resp.text.count("name='author_row_") == 2 + 6
    else:
        assert "value='Planning Under Uncertainty'" in resp.text
        assert "name='details_confirm' value='1' checked" not in resp.text


def test_use_these_appears_when_confirmed_details_differ_from_enrich(client, vault):
    root, sid = vault
    _post(client, sid, _form(root, sid, authors=_a("Grace Hopper"), tick=True))
    html = client.get(f"/ui/reviews/sources/{sid}").text
    assert "Enrich now suggests" in html and "value='use_suggestion'" in html


def test_ordering_follows_the_order_field_with_row_order_breaking_ties(client, vault):
    root, sid = vault
    rows = [{"name": "Third", "order": 5}, {"name": "First", "order": 1},
            {"name": "Second A", "order": 2}, {"name": "Second B", "order": 2}]
    _post(client, sid, _form(root, sid, authors=rows, tick=True))
    assert [a["name"] for a in _prov(root, sid)["authors"]] == ["First", "Second A", "Second B",
                                                                "Third"]


def test_an_explicit_unchecked_box_beats_an_existing_page(client, vault, tmp_path):
    root, sid = vault
    # Another document gives "Ada Lovelace" a page.
    _doc(root, "other.md")
    intake.scan_inbox(root, jobs_db=root / "db" / "jobs.sqlite")
    other = next(m["source_id"] for m in manifests.list_manifests(root / "raw" / "manifests")
                 if m["source_id"] != sid)
    manifests.confirm_details(root / "raw" / "manifests", other, title="Other",
                              authors=_a("Ada Lovelace", show=True), publisher=None,
                              baseline="y" * 16, now="2026-10-30T00:00:00Z")
    shown = _details(root, sid)["prefill"]["authors"]
    assert shown[0] == {"name": "Ada Lovelace", "show_page": True}  # pre-ticked at prefill
    _post(client, sid, _form(root, sid, authors=_a("Ada Lovelace", "Alan Turing"), tick=True))
    assert _prov(root, sid)["authors"][0] == {"name": "Ada Lovelace", "show_page": False}


# --- Clear ---------------------------------------------------------------------------------------


def test_clear_removes_the_details_and_a_stale_clear_is_refused(client, vault):
    root, sid = vault
    manifests.set_provenance(root / "raw" / "manifests", sid, report_family="fam")
    _post(client, sid, _form(root, sid, tick=True))
    baseline = _details(root, sid)["baseline"]
    resp = client.post(f"/ui/reviews/sources/{sid}/details/clear",
                       data={"details_baseline": "0" * 24})
    assert resp.status_code == 409 and "authors" in _prov(root, sid)
    resp = client.post(f"/ui/reviews/sources/{sid}/details/clear",
                       data={"details_baseline": baseline})
    assert resp.status_code == 200 and _prov(root, sid) == {"report_family": "fam"}


def test_a_changed_enrich_suggestion_makes_a_confirmed_documents_form_stale(client, vault):
    root, sid = vault
    _post(client, sid, _form(root, sid, tick=True))                # confirmed: prefill = confirmed
    form = _form(root, sid, title="A Later Edit", tick=True)
    path = root / "normalized" / "enrichment" / f"{sid}.json"
    artifact = json.loads(path.read_text())
    artifact["publisher"] = "A Different Press"                    # only the suggestion changes
    path.write_text(json.dumps(artifact))
    resp = _post(client, sid, form)
    assert resp.status_code == 409 and "changed after this page was shown" in resp.text
    assert _prov(root, sid)["title"] == "Planning Under Uncertainty"


def test_a_stale_redraw_keeps_the_seen_baseline_for_clear(client, vault):
    import re
    root, sid = vault
    _post(client, sid, _form(root, sid, tick=True))                # confirmed
    form = _form(root, sid, title="My Edit", tick=True)            # the page as I saw it
    manifests.confirm_details(root / "raw" / "manifests", sid, title="A Newer Confirmation",
                              authors=_a("Grace Hopper"), publisher=None, baseline="z" * 16,
                              now="2026-10-30T01:00:00Z")          # someone else, meanwhile
    resp = _post(client, sid, form)
    assert resp.status_code == 409                                 # stale: my draft is redrawn
    seen = re.search(r"name='details_baseline' value='([0-9a-f]+)'", resp.text)
    assert seen and seen.group(1) == form["details_baseline"]
    # Clear is now a pending change of the same form: the seen baseline makes it stale too.
    untouched = {k: v for k, v in form.items() if k != "details_confirm"}
    untouched["details_title"] = json.loads(form["details_shown"])["title"]
    resp = _post(client, sid, untouched | {"details_clear": "1"})
    assert resp.status_code == 409 and "Reload the page before clearing" in resp.text
    assert _prov(root, sid)["title"] == "A Newer Confirmation"     # the unseen one survives


@pytest.mark.parametrize("field", ["author", "publisher"])
def test_an_unnormalizable_name_is_refused_with_its_tag_decision(client, vault, field):
    root, sid = vault
    retrieval = items.node_id("Retrieval")
    kw = ({"authors": _a("Ada Lovelace", "\x01\x02")} if field == "author"
          else {"publisher": {"name": "\x07", "show_page": False}})
    before = (json.dumps(_prov(root, sid)), tag_links.load_ledger(root / "reviews", sid))
    resp = _post(client, sid, _form(root, sid, tick=True, **kw, **{f"link_{retrieval}": "keep"}))
    assert resp.status_code == 409 and "usable text (only control characters)" in resp.text
    assert (json.dumps(_prov(root, sid)), tag_links.load_ledger(root / "reviews", sid)) == before


# --- UAT part 3 #6/#7: screen actions keep unsaved choices ---------------------------------------


def _view(root, sid):
    return review_read.source_review_view(
        root / "reviews", sid, graph_db=root / "db" / "graph.sqlite", wiki_dir=root / "wiki",
        manifests_dir=root / "raw" / "manifests", enrichment_dir=root / "normalized" / "enrichment")


def _suggestion(root, sid):
    return str(_view(root, sid)["suggestions"][0]["node_id"])


def _shown_baseline(html):
    import re
    return re.search(r"name='details_baseline' value='([0-9a-f]+)'", html).group(1)


def _add(**fields):
    return {"_screen": "1", "add_title": "", "add_item_type": "method_technique",
            "add_aliases": "", "add_description": ""} | fields


def test_every_screen_button_belongs_to_the_one_batch_form(client, vault):
    root, sid = vault
    nid = _suggestion(root, sid)
    assert _post(client, sid, {f"link_{nid}": "drop"}).status_code == 200    # an Undo drop row
    html = client.get(f"/ui/reviews/sources/{sid}").text
    assert html.count("<form ") == 1 and "id='ks-source-form'" in html      # no second form
    for button in ("value='preselect_keep'>Pre-select keep", ">Undo drop</button>",
                   ">Add tag</button>"):
        tag = html[html.rindex("<button", 0, html.index(button)):html.index(button)]
        assert "form='ks-source-form'" in tag, button
    assert "formaction='/ui/reviews/sources/" + sid + "/add'" in html
    assert "formaction='/ui/reviews/sources/" + sid + "/undrop'" in html
    assert " required" not in html             # an empty Add-tag title must not block Save


def test_preselect_keep_keeps_the_edits_already_made_and_saves_nothing(client, vault):
    root, sid = vault
    nid = _suggestion(root, sid)
    before = tag_links.load_ledger(root / "reviews", sid)
    form = _form(root, sid, title="Edited Title", tick=True) | {
        "_screen": "1", "screen_action": "preselect_keep", f"link_{nid}": ""}
    html = _post(client, sid, form).text
    assert f"name='link_{nid}' value='keep' checked" in html
    assert "value='Edited Title'" in html and "name='details_confirm' value='1' checked" in html
    assert "Keep pre-selected on all suggestions" in html
    assert tag_links.load_ledger(root / "reviews", sid) == before and not _prov(root, sid).get("title")
    # A choice already made is not overridden.
    html = _post(client, sid, form | {f"link_{nid}": "drop"}).text
    assert f"name='link_{nid}' value='drop' checked" in html


def test_add_tag_keeps_the_unsaved_choices_and_the_save_still_works(client, vault):
    root, sid = vault
    nid = _suggestion(root, sid)
    form = _form(root, sid, title="Edited Title", tick=True) | _add(add_title="Hand Tag") | {
        f"link_{nid}": "drop"}
    resp = client.post(f"/ui/reviews/sources/{sid}/add", data=form)
    assert resp.status_code == 200 and "Tag “Hand Tag” added and kept" in resp.text
    html = resp.text
    assert f"name='link_{nid}' value='drop' checked" in html            # unsaved choice kept
    assert "value='Edited Title'" in html and "name='add_title'" in html
    assert "value='Hand Tag'" not in html                               # the add fields are reset
    view = _view(root, sid)
    assert "Hand Tag" in {r["title"] for r in view["kept"]}
    assert nid in {str(r["node_id"]) for r in view["suggestions"]}      # the drop wasn't saved
    assert not _prov(root, sid).get("title")
    # The page's own change moved the baseline, so its Save goes through.
    assert _shown_baseline(html) == _details(root, sid)["baseline"] != form["details_baseline"]
    save = form | {"details_baseline": _shown_baseline(html)}
    assert _post(client, sid, save).status_code == 200
    assert _prov(root, sid)["title"] == "Edited Title"
    assert nid not in {str(r["node_id"]) for r in _view(root, sid)["suggestions"]}


def test_a_refused_add_redraws_the_screen_with_every_field_kept(client, vault):
    root, sid = vault
    nid = _suggestion(root, sid)
    form = _form(root, sid, title="Edited Title", tick=True) | _add(add_description="why") | {
        f"link_{nid}": "keep"}
    resp = client.post(f"/ui/reviews/sources/{sid}/add", data=form)
    assert resp.status_code == 400 and "The tag wasn&#x27;t added: give the tag a title." in resp.text
    assert f"name='link_{nid}' value='keep' checked" in resp.text and "value='why'" in resp.text
    assert "value='Edited Title'" in resp.text


def test_a_stale_page_stays_stale_after_add(client, vault):
    root, sid = vault
    form = _form(root, sid, title="Edited Title", tick=True) | _add(add_title="Hand Tag") | {
        "details_baseline": "0" * 24}
    html = client.post(f"/ui/reviews/sources/{sid}/add", data=form).text
    assert _shown_baseline(html) == "0" * 24
    assert _post(client, sid, form).status_code == 409 and not _prov(root, sid).get("title")


def test_undo_drop_keeps_the_unsaved_choices(client, vault):
    root, sid = vault
    nid = _suggestion(root, sid)
    assert _post(client, sid, {f"link_{nid}": "drop"}).status_code == 200
    (dropped,) = _view(root, sid)["dropped"]
    form = _form(root, sid, title="Edited Title", tick=True) | _add() | {"key": dropped["key"]}
    resp = client.post(f"/ui/reviews/sources/{sid}/undrop", data=form)
    assert resp.status_code == 200 and f"Drop undone: “{dropped['name']}”" in resp.text
    assert "value='Edited Title'" in resp.text
    assert tag_links.load_ledger(root / "reviews", sid)["drops"] == []
    # Refused on the screen: redrawn with the error and the edits (a bare post keeps its 404 page,
    # tests/test_tag_links.py).
    resp = client.post(f"/ui/reviews/sources/{sid}/undrop", data=form | {"key": "nope"})
    assert resp.status_code == 404 and "The drop wasn&#x27;t undone" in resp.text
    assert "value='Edited Title'" in resp.text


def test_show_page_is_explained_on_the_screen(client, vault):
    root, sid = vault
    html = client.get(f"/ui/reviews/sources/{sid}").text
    assert "<strong>show page</strong>: list this document on the person" in html
    assert "<code>Authors/</code>" in html


# --- review 2026-11-02: Enter never saves; Clear is pending until Save ---------------------------


def test_enter_presses_a_hidden_redraw_that_saves_nothing(client, vault):
    import re
    root, sid = vault
    nid = _suggestion(root, sid)
    html = client.get(f"/ui/reviews/sources/{sid}").text
    first = re.search(r"<button[^>]*>", html).group(0)              # the form's default button
    assert "value='redraw'" in first and "form='ks-source-form'" in first
    assert "Save all changes on this page" in html and "Enter never" in html
    before = tag_links.load_ledger(root / "reviews", sid)
    form = _form(root, sid, title="Edited Title", tick=True) | {
        "_screen": "1", "screen_action": "redraw", f"link_{nid}": "keep"}
    resp = _post(client, sid, form)
    assert resp.status_code == 200 and "Nothing was saved (Enter only redraws the page)" in resp.text
    assert f"name='link_{nid}' value='keep' checked" in resp.text and "value='Edited Title'" in resp.text
    assert tag_links.load_ledger(root / "reviews", sid) == before and not _prov(root, sid).get("title")


def test_clear_is_a_pending_change_saved_with_the_batch(client, vault):
    root, sid = vault
    nid = _suggestion(root, sid)
    manifests.set_provenance(root / "raw" / "manifests", sid, report_family="fam")
    _post(client, sid, _form(root, sid, tick=True))
    html = client.get(f"/ui/reviews/sources/{sid}").text
    assert html.count("<form ") == 1 and "details/clear" not in html   # no separate Clear form
    # Ticking Clear and also confirming/editing is refused, and nothing is written.
    resp = _post(client, sid, _form(root, sid, title="Other", tick=True) | {"details_clear": "1"})
    assert resp.status_code == 409 and "Choose one" in resp.text and "authors" in _prov(root, sid)
    # Clear + a tag decision, one Save.
    resp = _post(client, sid, _form(root, sid) | {"details_clear": "1", f"link_{nid}": "drop"})
    assert resp.status_code == 200 and "clear details" in resp.text
    assert _prov(root, sid) == {"report_family": "fam"}
    assert nid not in {str(r["node_id"]) for r in _view(root, sid)["suggestions"]}


def test_muted_text_meets_the_contrast_minimum_on_its_backgrounds():
    import re

    from app.backend import review_html

    def lum(hex_):
        c = [int(hex_[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    def ratio(a, b):
        hi, lo = sorted((lum(a), lum(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)
    css = review_html._STYLE
    var = dict(re.findall(r"--([a-z-]+):#([0-9A-Fa-f]{6})", css))
    muted = var[re.search(r"\.muted\{color:var\(--([a-z-]+)\)\}", css).group(1)]
    for background in ("FFFFFF", var["suave"]):
        assert ratio(muted, background) >= 4.5, (muted, background)
    assert ratio(var["gris"], "FFFFFF") >= 3            # field borders (non-text, 3:1)


def test_a_stale_page_stays_stale_after_undo_drop(client, vault):
    root, sid = vault
    nid = _suggestion(root, sid)
    assert _post(client, sid, {f"link_{nid}": "drop"}).status_code == 200
    (dropped,) = _view(root, sid)["dropped"]
    form = _form(root, sid, title="Edited Title", tick=True) | _add() | {
        "key": dropped["key"], "details_baseline": "0" * 24}
    html = client.post(f"/ui/reviews/sources/{sid}/undrop", data=form).text
    assert _shown_baseline(html) == "0" * 24
    assert _post(client, sid, form).status_code == 409 and not _prov(root, sid).get("title")


_TYPED = {"add_title": "Half Typed", "add_item_type": "domain", "add_aliases": "HT, Half",
          "add_description": "not added yet"}


def _assert_add_draft_kept(html):
    assert "name='add_title' size='30' form='ks-source-form' value='Half Typed'" in html
    assert "<option value='domain' selected>" in html
    assert "value='HT, Half'" in html and "value='not added yet'" in html


@pytest.mark.parametrize("outcome", ["undone", "refused"])
def test_undo_drop_keeps_an_unsaved_add_tag_draft(client, vault, outcome):
    """Review 2026-11-02 (P2): only a successful Add tag resets the Add tag fields."""
    root, sid = vault
    nid = _suggestion(root, sid)
    assert _post(client, sid, {f"link_{nid}": "drop"}).status_code == 200
    (dropped,) = _view(root, sid)["dropped"]
    key = dropped["key"] if outcome == "undone" else "nope"
    form = _form(root, sid) | _add(**_TYPED) | {"key": key}
    resp = client.post(f"/ui/reviews/sources/{sid}/undrop", data=form)
    assert resp.status_code == (200 if outcome == "undone" else 404)
    _assert_add_draft_kept(resp.text)
    assert "Half Typed" not in {r["title"] for r in _view(root, sid)["kept"]}   # nothing added


def test_only_a_successful_add_resets_the_add_fields(client, vault):
    root, sid = vault
    form = _form(root, sid) | _add(**_TYPED)
    refused = client.post(f"/ui/reviews/sources/{sid}/add", data=form | {"add_item_type": "nope"})
    assert refused.status_code == 400 and "value='Half Typed'" in refused.text
    added = client.post(f"/ui/reviews/sources/{sid}/add", data=form).text
    assert "Tag “Half Typed” added" in added and "value='Half Typed'" not in added
    assert "value='not added yet'" not in added
