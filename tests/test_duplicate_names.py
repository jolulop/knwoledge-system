"""ADR-0071 slice B1b — test group 2: DUPLICATE-NAME decisions (decision 3).

Level 1 (exact name under the id normalization) blocks a new name on all three paths — Add tag, a
title amendment at keep, a split spin-off — naming the tag; level 2 (same `merge_key`) only advises,
with Use existing / Add separately on Add tag, rechecked when the action executes and keeping the
screen's draft.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph, review_html  # noqa: E402
from app.workers import human_add, item_names, items, splits, tag_links  # noqa: E402
from tests.test_details import _add, _form, _view, client, vault  # noqa: E402,F401
from tests.test_tag_links import _build, _extract, _g, _item, _keep, _sids  # noqa: E402

OLD, NEW = "Old Tag", "Renamed Tag"


def _add_tag(root, sid, title, **kw):
    gconn = _g(root)
    try:
        return human_add.add_candidate(gconn, root=root, source_id=sid,
                                       item_type=kw.pop("item_type", "method_technique"),
                                       title=title, wiki_dir=root / "wiki",
                                       reviews_dir=root / "reviews", **kw)
    finally:
        gconn.close()


def _conflict(root, name, node_id=None):
    gconn = _g(root)
    try:
        return item_names.exact_conflict(gconn, root / "wiki", name, node_id)
    finally:
        gconn.close()


def _set_status(root, nid, status):
    gconn = _g(root)
    try:
        gconn.execute("UPDATE nodes SET status = ? WHERE node_id = ?", (status, nid))
        gconn.commit()
    finally:
        gconn.close()


def _audits(root):
    return sorted(p.name for p in (root / "reviews" / "audit_log").glob("*.json"))


@pytest.fixture
def renamed(tmp_path):
    """`Old Tag` kept on a with the title amended to `Renamed Tag`: its id no longer hashes to its
    title, so an add or rename of `Renamed Tag` would mint a second item with the same name."""
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    _extract(tmp_path, {"alpha": [_item(OLD), _item("Self Tag")], "beta": [_item("Second")]})
    assert _keep(tmp_path, sids["a.md"], OLD, title=NEW)["outcome"] == "kept"
    return tmp_path, sids["a.md"], sids["b.md"]


# --- level 1: exact name -------------------------------------------------------------------------


def test_add_tag_of_a_renamed_items_title_is_refused_naming_it(renamed):
    root, a, b = renamed
    before = _audits(root)
    out = _add_tag(root, b, "renamed  TAG")                 # equal under the id normalization
    assert out["outcome"] == "blocked" and out["reason"] == item_names.NAME_TAKEN
    assert out["conflict_node_id"] == items.node_id(OLD) and out["conflict_title"] == NEW
    gconn = _g(root)
    try:
        assert graph.get_node(gconn, items.node_id(NEW)) is None    # nothing written
    finally:
        gconn.close()
    assert _audits(root) == before and tag_links.load_ledger(root / "reviews", b)["kept"] == {}
    text = review_html.refusal_text(out["reason"], out)
    assert "“Renamed Tag” already exists" in text and "name_taken" not in text


def test_a_title_amendment_onto_another_items_title_is_refused(renamed):
    root, a, b = renamed
    out = _keep(root, b, "Second", title=NEW)
    assert out["outcome"] == "blocked" and out["reason"] == item_names.NAME_TAKEN
    assert out["problem"]["conflict_node_id"] == items.node_id(OLD)
    assert _view_status(root, "Second") == "candidate"


def _view_status(root, name):
    gconn = _g(root)
    try:
        return graph.get_node(gconn, items.node_id(name))["status"]
    finally:
        gconn.close()


def test_a_self_rename_never_conflicts_with_itself(renamed):
    root, a, b = renamed
    assert _keep(root, a, "Self Tag", title="SELF   tag")["outcome"] == "kept"
    assert _conflict(root, NEW, items.node_id(OLD)) is None        # its own title, excluded


@pytest.mark.parametrize("status", ["candidate", "active", "unused", "hidden",
                                    "deprecated_candidate", "stale_candidate", "archived"])
def test_every_lifecycle_status_counts(renamed, status):
    root, a, b = renamed
    _set_status(root, items.node_id(OLD), status)
    conflict = _conflict(root, NEW)
    assert conflict["reason"] == item_names.NAME_TAKEN
    assert conflict["conflict_status"] == status


def test_a_deleted_item_does_not_count(renamed):
    root, a, b = renamed
    _set_status(root, items.node_id(OLD), "deleted")
    assert _conflict(root, NEW) is None


def _tombstone(root, absorbed, survivor):
    """Make `absorbed` a merged tombstone pointing at `survivor` (page + graph mirror)."""
    gconn = _g(root)
    try:
        node = graph.get_node(gconn, absorbed)
        page = root / "wiki" / "Items" / f"{node['slug']}.md"
        text = page.read_text(encoding="utf-8")
        page.write_text(text.replace("\nstatus: ", f'\nmerged_into: "{survivor}"\nstatus: ', 1)
                        .replace("status: active", "status: merged", 1), encoding="utf-8")
        gconn.execute("UPDATE nodes SET status = 'merged' WHERE node_id = ?", (absorbed,))
        gconn.commit()
    finally:
        gconn.close()


def test_a_merged_tombstone_resolves_to_its_survivor_first(renamed):
    root, a, b = renamed
    _keep(root, b, "Second")
    survivor = items.node_id("Second")
    _tombstone(root, items.node_id(OLD), survivor)          # "Renamed Tag" merged into Second
    conflict = _conflict(root, NEW)
    assert conflict["conflict_node_id"] == survivor and conflict["conflict_title"] == "Second"
    assert conflict["via_tombstone"] == items.node_id(OLD)
    # The survivor taking its absorbed tag's name is no conflict.
    assert _conflict(root, NEW, survivor) is None
    # A chain ending at a deleted survivor is no live conflict.
    _set_status(root, survivor, "deleted")
    assert _conflict(root, NEW) is None


@pytest.mark.parametrize("damage", ["missing", "no_title", "foreign"])
def test_missing_title_authority_is_an_explicit_refusal(renamed, damage):
    root, a, b = renamed
    page = root / "wiki" / "Items" / f"{items._slug('Second')}.md"
    if damage == "missing":
        page.unlink()
    elif damage == "no_title":
        page.write_text(page.read_text(encoding="utf-8").replace('title: "Second"', "title: Second"),
                        encoding="utf-8")
    else:
        page.write_text(page.read_text(encoding="utf-8").replace(
            items.node_id("Second"), items.node_id("Someone Else")), encoding="utf-8")
    out = _add_tag(root, b, "Brand New")
    assert out["outcome"] == "blocked" and out["reason"] == item_names.TITLE_UNREADABLE
    assert out["node_id"] == items.node_id("Second")
    assert "can't be read" in review_html.refusal_text(out["reason"], out)


def test_a_split_spin_off_onto_a_taken_name_is_refused_before_any_write(tmp_path):
    """The case the filename guard can't see: two lossy names get different id suffixes
    (`renamed-tag--<old id>` vs `renamed-tag--<spin-off id>`), so only the name check refuses."""
    root = tmp_path
    _build(root, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(root)
    a, b = sids["a.md"], sids["b.md"]
    old, new = "Old-Tag", "Renamed-Tag"
    _extract(root, {"alpha": [_item(old), _item("Primary")], "beta": [_item("Primary")]})
    assert _keep(root, a, old, title=new)["outcome"] == "kept"
    assert items.page_slug(new, items.node_id(old)) != items._slug(new)    # different files
    _keep(root, a, "Primary")
    _keep(root, b, "Primary")
    appr = root / "reviews" / "approved"
    appr.mkdir(parents=True, exist_ok=True)
    (appr / "rev_s.json").write_text(json.dumps({
        "review_id": "rev_s", "type": "split_item", "status": "approved",
        "subject": {"node_id": items.node_id("Primary"), "spinoff_node_id": items.node_id(new)},
        "proposal": {"spinoff_name": new, "spinoff_sources": [b], "spinoff_aliases": []}}),
        encoding="utf-8")
    gconn = _g(root)
    try:
        sp = splits.apply_splits(gconn, root / "reviews", wiki_dir=root / "wiki")
        assert sp["applied"] == 0
        (skip,) = sp["skipped"]
        assert skip["reason"] == item_names.NAME_TAKEN
        assert skip["problem"]["conflict_node_id"] == items.node_id(old)
        assert graph.get_node(gconn, items.node_id(new)) is None
    finally:
        gconn.close()
    assert not (root / "reviews" / "in_flight").exists() or not any(
        (root / "reviews" / "in_flight").iterdir())


def test_two_lossy_renames_onto_one_name_are_refused(tmp_path):
    """Q1 qualification: `Data-Mesh` reached twice by rename would get two files; the second
    title amendment is refused by the name check."""
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item("First-One"), _item("Second-One")]})
    assert _keep(tmp_path, a, "First-One", title="Data-Mesh")["outcome"] == "kept"
    out = _keep(tmp_path, a, "Second-One", title="Data-Mesh")
    assert out["outcome"] == "blocked" and out["reason"] == item_names.NAME_TAKEN
    assert out["problem"]["conflict_node_id"] == items.node_id("First-One")


# --- level 2: same merge_key (advisory) ----------------------------------------------------------


@pytest.fixture
def mesh(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    _extract(tmp_path, {"alpha": [_item("Data-Mesh"), _item("C"), _item("Mesh Architecture")]})
    for name in ("Data-Mesh", "C"):
        _keep(tmp_path, sids["a.md"], name)
    _keep(tmp_path, sids["a.md"], "Mesh Architecture", aliases=["data mesh"])
    return tmp_path, sids["a.md"], sids["b.md"]


def test_a_similar_name_asks_and_writes_nothing(mesh):
    root, a, b = mesh
    before = _audits(root)
    out = _add_tag(root, b, "Data Mesh")
    assert out["outcome"] == "needs_choice"
    got = {m["node_id"]: m["matched"] for m in out["matches"]}
    assert got == {items.node_id("Data-Mesh"): "Data-Mesh",
                   items.node_id("Mesh Architecture"): "data mesh"}     # the alias that matched
    gconn = _g(root)
    try:
        assert graph.get_node(gconn, items.node_id("Data Mesh")) is None
    finally:
        gconn.close()
    assert _audits(root) == before


def test_add_separately_creates_a_distinct_tag(mesh):
    root, a, b = mesh
    ids = [items.node_id("Data-Mesh"), items.node_id("Mesh Architecture")]
    out = _add_tag(root, b, "Data Mesh", acknowledged=ids)
    assert out["outcome"] == "created" and out["node_id"] == items.node_id("Data Mesh")
    assert (root / "wiki" / "Items" / "data-mesh.md").exists()
    assert (root / "wiki" / "Items" / f"{items._slug('Data-Mesh')}.md").exists()


def test_use_existing_keeps_the_chosen_tag_as_it_is(mesh):
    root, a, b = mesh
    chosen = items.node_id("Data-Mesh")
    out = _add_tag(root, b, "Data Mesh", item_type="domain", existing_node_id=chosen)
    assert out["outcome"] == "mention_added_active" and out["node_id"] == chosen
    assert out["title"] == "Data-Mesh" and out["item_type"] == "method_technique"   # no retype
    assert chosen in tag_links.load_ledger(root / "reviews", b)["kept"]
    assert not list((root / "reviews" / "pending").glob("*.json"))
    gconn = _g(root)
    try:
        assert graph.get_node(gconn, items.node_id("Data Mesh")) is None
    finally:
        gconn.close()


def test_distinct_pairs_like_c_plus_plus_stay_addable(mesh):
    root, a, b = mesh
    first = _add_tag(root, b, "C++")
    assert first["outcome"] == "needs_choice"
    assert [m["node_id"] for m in first["matches"]] == [items.node_id("C")]
    out = _add_tag(root, b, "C++", acknowledged=[items.node_id("C")])
    assert out["outcome"] == "created"
    assert (root / "wiki" / "Items" / "c.md").exists()
    assert (root / "wiki" / "Items" / f"c--{items.node_id('C++')[4:12]}.md").exists()


def test_a_match_that_appeared_since_asks_again(mesh):
    root, a, b = mesh
    seen = [items.node_id("Data-Mesh")]                      # the form showed only this one
    out = _add_tag(root, b, "Data Mesh", acknowledged=seen)
    assert out["outcome"] == "needs_choice"                  # Mesh Architecture's alias is new to it
    assert items.node_id("Mesh Architecture") in {m["node_id"] for m in out["matches"]}


def test_candidates_are_not_advised(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item("Data-Mesh")]})      # a suggestion only, never kept
    assert _add_tag(tmp_path, a, "Data Mesh")["outcome"] == "created"


# --- the screen: Use existing / Add separately keep the draft --------------------------------------


def _kept_suggestion(http, root, sid):
    """Keep the screen's first suggestion; return its id and title."""
    row = _view(root, sid)["suggestions"][0]
    nid = str(row["node_id"])
    assert http.post(f"/ui/reviews/sources/{sid}/decide",
                     data={f"link_{nid}": "keep"}).status_code == 200
    assert nid in tag_links.load_ledger(root / "reviews", sid)["kept"]
    return nid, row.get("title") or row.get("name")


def test_the_screen_offers_both_choices_and_keeps_the_draft(client, vault):  # noqa: F811 - the imported fixture
    root, sid = vault
    nid, title = _kept_suggestion(client, root, sid)
    form = _form(root, sid, title="Edited Title", tick=True) | _add(add_title=f"{title}s")
    resp = client.post(f"/ui/reviews/sources/{sid}/add", data=form)
    assert resp.status_code == 409
    html = resp.text
    assert "A similar tag already exists" in html and "value='Edited Title'" in html
    assert f"name='add_use' value='{nid}'" in html and "name='add_separately'" in html
    assert f"value='{title}s'" in html                       # the add fields are kept to choose
    assert f"{title}s" not in {r["title"] for r in _view(root, sid)["kept"]}

    sep = client.post(f"/ui/reviews/sources/{sid}/add", data=form | {"add_separately": nid})
    assert sep.status_code == 200 and f"Tag “{title}s” added" in sep.text
    assert "value='Edited Title'" in sep.text                # still unsaved, still shown


def test_the_screen_use_existing_keeps_the_chosen_tag(client, vault):  # noqa: F811 - the imported fixture
    root, sid = vault
    nid, title = _kept_suggestion(client, root, sid)
    form = _form(root, sid) | _add(add_title=f"{title}s")
    resp = client.post(f"/ui/reviews/sources/{sid}/add", data=form | {"add_use": nid})
    assert resp.status_code == 200 and f"Tag “{title}” added" in resp.text
    assert f"{title}s" not in {r["title"] for r in _view(root, sid)["kept"]}


def test_refusals_read_as_plain_text():
    for reason in ("slug_collision", "name_taken", "title_authority_unreadable", "page_owner_unknown",
                   "page_owned_by_other_item", "amended_slug_collision", "slug_taken"):
        text = review_html.refusal_text(reason, {"slug": "x", "conflict_title": "X",
                                                 "existing_title": "X", "node_id": "itm_0"})
        assert reason not in text, (reason, text)


# --- review round 1: the Merge action on a rename refusal (decision 3) ----------------------------


def _merge_action(root, monkeypatch, out, renamed_id, renamed_title):
    from app.backend import main as main_module
    from app.backend.config import get_settings
    monkeypatch.setattr(main_module, "settings", get_settings(root))
    gconn = _g(root)
    try:
        return main_module._rename_merge_action(gconn, out, renamed_id, renamed_title)
    finally:
        gconn.close()


def test_a_rename_refusal_of_a_suggestion_explains_what_a_merge_needs(renamed, monkeypatch):
    """Title amendments only happen on suggestions, so the renamed tag is not in use and plan_merge
    blocks: the refusal says what the merge needs first instead of offering it."""
    root, a, b = renamed
    out = _keep(root, b, "Second", title=NEW)
    action = _merge_action(root, monkeypatch, out, items.node_id("Second"), "Second")
    assert "merge_preview" not in action
    assert "not in use yet" in action["merge_advice"] and "'Second'" in action["merge_advice"]
    html = review_html._merge_action(action)
    assert "A merge isn't possible yet" in html and "href" not in html


def test_an_eligible_pair_gets_a_preview_link_in_plan_merge_direction(renamed, monkeypatch):
    """With both tags in use (checked on the helper itself — no new rename path), the link is the
    merge PREVIEW with the existing tag surviving, exactly as plan_merge routes it."""
    root, a, b = renamed
    _keep(root, b, "Second")                                      # in use: mergeable
    problem = {"conflict_node_id": items.node_id(OLD), "conflict_title": NEW}
    action = _merge_action(root, monkeypatch, {"reason": item_names.NAME_TAKEN, "problem": problem},
                           items.node_id("Second"), "Second")
    assert action["merge_preview"] == {"survivor": items.node_id(OLD),
                                       "absorbed": items.node_id("Second"), "survivor_title": NEW}
    html = review_html._merge_action(action)
    assert (f"href='/ui/reviews/merge/preview?survivor={items.node_id(OLD)}&amp;"
            f"absorbed={items.node_id('Second')}'") in html
    assert "Renamed Tag" in html
    assert _merge_action(root, monkeypatch, {"reason": "slug_collision", "problem": problem},
                         items.node_id("Second"), "Second") == {}     # other refusals: nothing


def test_the_merge_link_escapes_titles():
    html = review_html._merge_action({"merge_preview": {
        "survivor": "itm_1", "absorbed": "itm_2", "survivor_title": "<b>x</b>"}})
    assert "<b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html


# --- review round 1: level-2 advisories on title amendments and split spin-offs --------------------


@pytest.fixture
def similar(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    _extract(tmp_path, {"alpha": [_item("Data-Mesh"), _item("Mesh Architecture"), _item("Other"),
                                  _item("Primary")], "beta": [_item("Primary")]})
    _keep(tmp_path, sids["a.md"], "Data-Mesh")
    _keep(tmp_path, sids["a.md"], "Mesh Architecture", aliases=["data mesh"])
    return tmp_path, sids["a.md"], sids["b.md"]


def test_a_title_amendment_reports_similar_tags_as_structured_data(similar):
    root, a, b = similar
    out = _keep(root, a, "Other", title="Data Mesh")             # not equal, same merge_key
    assert out["outcome"] == "kept"
    (adv,) = out["advisories"]
    assert adv["kind"] == item_names.SIMILAR_TAGS and adv["name"] == "Data Mesh"
    assert adv["node_id"] == items.node_id("Other")
    assert {(m["title"], m["matched"]) for m in adv["matches"]} == {
        ("Data-Mesh", "Data-Mesh"), ("Mesh Architecture", "data mesh")}
    assert "reason" not in out                                    # an advisory is not a refusal


def test_an_advisory_never_changes_eligibility(similar, monkeypatch):
    root, a, b = similar

    def broken(*args, **kwargs):
        raise RuntimeError("advisory failure")
    monkeypatch.setattr(item_names, "similar_tags", broken)
    out = _keep(root, a, "Other", title="Data Mesh")
    assert out["outcome"] == "kept" and out["advisories"] == []
    assert _view_status(root, "Other") == "active"


def test_a_split_spin_off_reports_similar_tags(similar):
    root, a, b = similar
    _keep(root, a, "Primary")
    _keep(root, b, "Primary")                                     # b moves; a stays: a real split
    appr = root / "reviews" / "approved"
    appr.mkdir(parents=True, exist_ok=True)
    spin = "Data Mesh"
    (appr / "rev_s.json").write_text(json.dumps({
        "review_id": "rev_s", "type": "split_item", "status": "approved",
        "subject": {"node_id": items.node_id("Primary"), "spinoff_node_id": items.node_id(spin)},
        "proposal": {"spinoff_name": spin, "spinoff_sources": [b], "spinoff_aliases": []}}),
        encoding="utf-8")
    gconn = _g(root)
    try:
        sp = splits.apply_splits(gconn, root / "reviews", wiki_dir=root / "wiki")
    finally:
        gconn.close()
    assert sp["applied"] == 1 and sp["skipped"] == [], sp["skipped"]
    (adv,) = sp["advisories"]
    assert adv["review_id"] == "rev_s" and adv["node_id"] == items.node_id(spin)
    assert {m["title"] for m in adv["matches"]} == {"Data-Mesh", "Mesh Architecture"}


def test_advisories_render_escaped_and_apart_from_refusals():
    rows = [{"title": "Other", "action": "keep", "recorded": True, "status": "activated",
             "advisories": [{"kind": "similar_tags", "name": "x", "node_id": "itm_1", "matches": [
                 {"node_id": "itm_2", "title": "<i>Data-Mesh</i>", "matched": "<i>dm</i>",
                  "status": "active"}]}]}]
    html = review_html.render_source_decide_result("src_0123456789abcdef", rows)
    assert "<i>" not in html and "&lt;i&gt;Data-Mesh&lt;/i&gt;" in html
    assert "a similar tag already exists" in html and "matched its alias" in html


def test_the_screen_shows_the_rename_refusal_and_the_advisory(client, vault):  # noqa: F811 - the imported fixture
    root, sid = vault
    nid, title = _kept_suggestion(client, root, sid)
    other = next(r for r in _view(root, sid)["suggestions"] if str(r["node_id"]) != nid)
    oid = str(other["node_id"])
    refused = client.post(f"/ui/reviews/sources/{sid}/decide",
                          data={f"link_{oid}": "keep", f"amend_title_{oid}": f" {title.upper()} "})
    assert refused.status_code == 200
    assert f"“{title}” already exists" in refused.text and "name_taken" not in refused.text
    assert "A merge isn't possible yet" in refused.text
    assert oid not in tag_links.load_ledger(root / "reviews", sid)["kept"]
    kept = client.post(f"/ui/reviews/sources/{sid}/decide",
                       data={f"link_{oid}": "keep", f"amend_title_{oid}": f"{title}s"})
    assert kept.status_code == 200 and "a similar tag already exists" in kept.text
    assert oid in tag_links.load_ledger(root / "reviews", sid)["kept"]
