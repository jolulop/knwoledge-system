"""ADR-0070 B1 review: two tags whose names give one page name (`Café` and `Caf` → `Items/caf.md`) must
never overwrite or delete each other's page — the page holds the tag's title, aliases, description
and status. Every writer refuses before changing authority; extraction skips and reports."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for p in (ROOT, SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import validate_graph  # noqa: E402
import validate_projection  # noqa: E402

from app.backend import graph  # noqa: E402
from app.backend.manifests import iso_now  # noqa: E402
from app.workers import human_add, item_pages, items, merges, retypes, splits, tag_links  # noqa: E402
from app.workers.wiki_render import parse_frontmatter  # noqa: E402
from tests.test_tag_links import (  # noqa: E402
    _block, _build, _call, _drop, _extract, _g, _item, _keep, _sids)

CPP, C = items.node_id("Café"), items.node_id("Caf")   # one page name: caf


def _page(root, slug="caf"):
    return root / "wiki" / "Items" / f"{slug}.md"


def _title(root, slug="caf"):
    return parse_frontmatter(_page(root, slug).read_text(encoding="utf-8")).get("title")


def _ledger(root, sid):
    return tag_links.load_ledger(root / "reviews", sid)


@pytest.fixture
def vault(tmp_path):
    """`Café` suggested on a and kept there; `Caf` (another key, same page name) comes later."""
    _build(tmp_path, {"a.md": "alpha Café body", "b.md": "beta Caf body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _extract(tmp_path, {"alpha": [_item("Café", "technology_capability")]}, source_ids=[a])
    _keep(tmp_path, a, "Café")
    return tmp_path, a, b


def _index_source(root, sid):
    gconn = _g(root)
    try:
        graph.upsert_node(gconn, node_id=sid, node_type="source", slug=sid, status="active",
                          now=iso_now())
        gconn.commit()
    finally:
        gconn.close()


def _legacy_collision(root, b, status="candidate"):
    """A collision made before the guard existed: `Caf` recorded at slug `caf` with a suggestion on b,
    while the page file belongs to `Café`."""
    _index_source(root, b)
    gconn = _g(root)
    try:
        now = iso_now()
        graph.upsert_node(gconn, node_id=C, node_type="item", slug="caf", status=status,
                          item_type="technology_capability", now=now)
        graph.upsert_assertion(gconn, src_id=b, dst_id=C, edge_type="mentions", asserted_by="llm",
                               status="active", now=now)
        gconn.commit()
    finally:
        gconn.close()


# --- the rule ---------------------------------------------------------------------------------------


def test_page_state_and_problem(vault):
    root, a, b = vault
    gconn = _g(root)
    try:
        wiki = root / "wiki"
        assert item_pages.page_state(wiki, "caf", CPP) == ("own", CPP)
        assert item_pages.page_state(wiki, "caf", C) == ("other", CPP)
        assert item_pages.page_problem(gconn, wiki, CPP, "caf") is None
        assert item_pages.page_problem(gconn, wiki, C, "caf")["reason"] == item_pages.OWNED_BY_OTHER
        assert item_pages.page_state(wiki, "nothing-here", C) == ("absent", None)
        assert item_pages.page_problem(gconn, wiki, C, "nothing-here") is None
        assert item_pages.page_state(wiki, "../x", C)[0] == "unknown"
    finally:
        gconn.close()


@pytest.mark.parametrize("frontmatter", ["", "---\ntitle: \"Café\"\n---\n", '---\nitem_id: "bogus"\n---\n',
                                         '---\nitem_id: 7\n---\n'])
def test_an_unestablished_owner_fails_closed(vault, frontmatter):
    root, a, b = vault
    _page(root).write_text(frontmatter + "\n# Café\n", encoding="utf-8")
    gconn = _g(root)
    try:
        assert item_pages.page_problem(gconn, root / "wiki", CPP, "caf")["reason"] == item_pages.OWNER_UNKNOWN
        assert item_pages.delete_problem(root / "wiki", CPP, "caf")["reason"] == item_pages.OWNER_UNKNOWN
    finally:
        gconn.close()
    out = _drop(root, a, "Café")                     # even the tag that should own it can't touch it
    assert out["outcome"] == "blocked" and out["reason"] == item_pages.OWNER_UNKNOWN
    assert CPP in _ledger(root, a)["kept"]           # nothing implied success
    assert _page(root).read_text(encoding="utf-8").startswith(frontmatter)


def test_an_unreadable_page_fails_closed(vault):
    root, a, b = vault
    _page(root).write_bytes(b"\xff\xfe not utf-8")
    gconn = _g(root)
    try:
        assert item_pages.page_problem(gconn, root / "wiki", CPP, "caf")["reason"] == item_pages.OWNER_UNKNOWN
    finally:
        gconn.close()


# --- creation: extraction skips and reports ----------------------------------------------------------


def test_extraction_skips_a_suggestion_whose_page_name_is_taken(vault):
    root, a, b = vault
    summary = _extract(root, {"alpha": [_item("Café", "technology_capability")],
                              "beta": [_item("Caf", "technology_capability")]}, source_ids=[b])
    assert summary["status"] == "partial"
    assert summary["suggestions_skipped_slug_taken"] == 1
    detail = summary["slug_taken_details"][0]
    assert detail["source_id"] == b and detail["name"] == "Caf" and detail["slug"] == "caf"
    assert detail["reason"] == item_pages.OWNED_BY_OTHER and detail["owner"] == CPP
    gconn = _g(root)
    try:
        assert graph.get_node(gconn, C) is None                       # nothing left behind
        assert not [r for r in graph.source_tag_links(gconn, b) if r["dst_id"] == C]
    finally:
        gconn.close()
    assert _title(root) == "Café" and CPP in _ledger(root, a)["kept"]  # the other tag intact


@pytest.mark.parametrize("status", ["candidate", "unused", "hidden", "merged", "deprecated_candidate"])
def test_a_page_name_held_by_an_item_in_any_status_blocks_creation(vault, status):
    root, a, b = vault
    gconn = _g(root)
    try:
        graph.upsert_node(gconn, node_id=CPP, node_type="item", slug="caf", status=status,
                          item_type="technology_capability", now=iso_now())
        gconn.commit()
    finally:
        gconn.close()
    _page(root).unlink()                                  # even with the file gone, the slug is held
    summary = _extract(root, {"alpha": [], "beta": [_item("Caf", "technology_capability")]},
                       source_ids=[b])
    detail = summary["slug_taken_details"][0]
    assert detail["reason"] == item_pages.SLUG_TAKEN and detail["holders"] == [CPP]
    assert not _page(root).exists()


def test_an_unowned_file_blocks_creation(tmp_path):
    _build(tmp_path, {"b.md": "beta Caf body"})
    _page(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _page(tmp_path).write_text("# a hand-made note with no item_id\n", encoding="utf-8")
    summary = _extract(tmp_path, {"beta": [_item("Caf", "technology_capability")]})
    assert summary["slug_taken_details"][0]["reason"] == item_pages.OWNER_UNKNOWN
    assert _page(tmp_path).read_text(encoding="utf-8") == "# a hand-made note with no item_id\n"


def test_human_add_refuses_a_taken_page_name(vault):
    root, a, b = vault
    _index_source(root, b)
    gconn = _g(root)
    try:
        out = human_add.add_candidate(gconn, root=root, source_id=b, item_type="technology_capability",
                                      title="Caf", wiki_dir=root / "wiki", reviews_dir=root / "reviews")
    finally:
        gconn.close()
    assert out["outcome"] == "blocked" and out["reason"] == "slug_collision"
    assert out["existing_node_id"] == CPP
    assert _title(root) == "Café" and C not in _ledger(root, b)["kept"]


# --- an existing collision: every action refuses before its first write ------------------------------


def test_keep_drop_block_and_undo_refuse_on_a_shared_page(vault):
    root, a, b = vault
    _legacy_collision(root, b)
    before = _page(root).read_text(encoding="utf-8")
    kept = _keep(root, b, "Caf")
    assert kept["outcome"] == "blocked" and kept["reason"] == item_pages.OWNED_BY_OTHER
    assert C not in _ledger(root, b)["kept"]
    gconn = _g(root)
    try:
        assert not [r for r in graph.source_tag_links(gconn, b)
                    if r["dst_id"] == C and r["asserted_by"] == "human"]
    finally:
        gconn.close()
    dropped = _drop(root, b, "Caf")
    assert dropped["outcome"] == "blocked" and not _ledger(root, b)["drops"]
    # A tag's names come from its page; Caf's recorded page is Café's, so the block matches no tag
    # and only records the name (it neither withdraws nor deletes through a borrowed page).
    blocked = _block(root, "Caf")
    assert blocked["outcome"] == "blocked_tag" and blocked["deleted"] == []
    gconn = _g(root)
    try:
        assert graph.get_node(gconn, C) is not None
    finally:
        gconn.close()
    assert _page(root).read_text(encoding="utf-8") == before   # Café's authority untouched
    assert validate_graph._check(root / "db" / "graph.sqlite")  # the shared page name is reported


def test_delete_tag_and_delete_unused_never_remove_another_items_page(vault):
    root, a, b = vault
    _legacy_collision(root, b, status="unused")
    gconn = _g(root)
    try:
        out = tag_links.delete_tag(gconn, node_id=C, wiki_dir=root / "wiki",
                                   reviews_dir=root / "reviews", reason="test")
        assert out["outcome"] == "blocked" and out["reason"] == item_pages.OWNED_BY_OTHER
        assert graph.get_node(gconn, C) is not None
        assert not tag_links.deletion_intents(root / "reviews")
    finally:
        gconn.close()
    res = _call(root, tag_links.delete_unused)
    assert C not in res["deleted"]
    assert {"node_id": C, "reason": item_pages.OWNED_BY_OTHER} in res["skipped"]
    assert _title(root) == "Café"


def test_retype_merge_and_split_refuse_a_page_they_do_not_own(vault):
    root, a, b = vault
    _legacy_collision(root, b)
    appr = root / "reviews" / "approved"
    appr.mkdir(parents=True, exist_ok=True)
    (appr / "rev_rt.json").write_text(json.dumps({
        "review_id": "rev_rt", "type": "change_item_type", "status": "approved",
        "subject": {"node_id": C, "to_item_type": "domain"},
        "proposal": {"to_item_type": "domain"}}), encoding="utf-8")
    gconn = _g(root)
    try:
        rt = retypes.apply_retypes(gconn, root / "reviews", wiki_dir=root / "wiki")
        assert rt["skipped"] == [{"review_id": "rev_rt", "reason": item_pages.OWNED_BY_OTHER}]
        (appr / "rev_rt.json").unlink()
        graph.upsert_node(gconn, node_id=C, node_type="item", slug="caf", status="unused",
                          item_type="technology_capability", now=iso_now())   # mergeable status
        (appr / "rev_m.json").write_text(json.dumps({
            "review_id": "rev_m", "type": "merge_items", "status": "approved",
            "subject": {"survivor_node_id": CPP, "absorbed_node_id": C},
            "proposal": {"to_status": "merged"}}), encoding="utf-8")
        mg = merges.apply_merges(gconn, root / "reviews", wiki_dir=root / "wiki")
        assert mg["applied"] == 0
        assert mg["skipped"] == [{"review_id": "rev_m", "reason": item_pages.OWNED_BY_OTHER}]
        (appr / "rev_m.json").unlink()
        # A split whose spin-off would take a page name another item records.
        (appr / "rev_s.json").write_text(json.dumps({
            "review_id": "rev_s", "type": "split_item", "status": "approved",
            "subject": {"node_id": CPP, "spinoff_node_id": C},
            "proposal": {"spinoff_name": "Caf", "spinoff_sources": [a], "spinoff_aliases": []}}),
            encoding="utf-8")
        sp = splits.apply_splits(gconn, root / "reviews", wiki_dir=root / "wiki")
        assert sp["applied"] == 0
        assert sp["skipped"] == [{"review_id": "rev_s", "reason": "target_spinoff_id_exists"}]
        gconn.commit()
    finally:
        gconn.close()
    assert _title(root) == "Café"
    assert not (root / "reviews" / "in_flight").exists() or not any(
        (root / "reviews" / "in_flight").iterdir())


def test_a_split_spinoff_onto_a_held_page_name_is_refused_before_any_write(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _extract(tmp_path, {"alpha": [_item("Primary"), _item("Café", "technology_capability")],
                        "beta": [_item("Primary")]})
    _keep(tmp_path, a, "Primary")
    _keep(tmp_path, b, "Primary")
    _keep(tmp_path, a, "Café")
    appr = tmp_path / "reviews" / "approved"
    appr.mkdir(parents=True, exist_ok=True)
    (appr / "rev_s.json").write_text(json.dumps({
        "review_id": "rev_s", "type": "split_item", "status": "approved",
        "subject": {"node_id": items.node_id("Primary"), "spinoff_node_id": C},
        "proposal": {"spinoff_name": "Caf", "spinoff_sources": [b], "spinoff_aliases": []}}),
        encoding="utf-8")
    gconn = _g(tmp_path)
    try:
        sp = splits.apply_splits(gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")
        assert sp["applied"] == 0
        assert sp["skipped"] == [{"review_id": "rev_s", "reason": "target_spinoff_page_exists"}]
        assert graph.get_node(gconn, C) is None
        assert items.node_id("Primary") in {r["dst_id"] for r in graph.source_tag_links(gconn, b)}
    finally:
        gconn.close()
    assert _title(tmp_path) == "Café"
    assert items.node_id("Primary") in _ledger(tmp_path, b)["kept"]   # the ledger did not move


def test_validators_report_shared_page_names_and_foreign_pages(vault):
    root, a, b = vault
    _legacy_collision(root, b)
    errors = validate_graph._check(root / "db" / "graph.sqlite")
    assert any("share the page name Items/caf.md" in e for e in errors)
    proj = validate_projection._check(root, root / "db" / "graph.sqlite")
    assert any("is not the graph item recording this page name" in e for e in proj)


def test_a_split_spinoff_onto_a_page_name_held_only_in_the_graph_is_refused(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _extract(tmp_path, {"alpha": [_item("Primary")], "beta": [_item("Primary")]})
    _keep(tmp_path, a, "Primary")
    _keep(tmp_path, b, "Primary")
    gconn = _g(tmp_path)
    try:                                       # Café records `caf` (any status) but has no file
        graph.upsert_node(gconn, node_id=CPP, node_type="item", slug="caf", status="hidden",
                          item_type="technology_capability", now=iso_now())
        gconn.commit()
        appr = tmp_path / "reviews" / "approved"
        appr.mkdir(parents=True, exist_ok=True)
        (appr / "rev_s.json").write_text(json.dumps({
            "review_id": "rev_s", "type": "split_item", "status": "approved",
            "subject": {"node_id": items.node_id("Primary"), "spinoff_node_id": C},
            "proposal": {"spinoff_name": "Caf", "spinoff_sources": [b], "spinoff_aliases": []}}),
            encoding="utf-8")
        sp = splits.apply_splits(gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")
        assert sp["skipped"] == [{"review_id": "rev_s", "reason": item_pages.SLUG_TAKEN}]
        assert graph.get_node(gconn, C) is None and not _page(tmp_path).exists()
    finally:
        gconn.close()
    assert items.node_id("Primary") in _ledger(tmp_path, b)["kept"]
    assert not (tmp_path / "reviews" / "in_flight").exists() or not any(
        (tmp_path / "reviews" / "in_flight").iterdir())


# --- review round 3: "use X" preflights both sides' duplicate partners ------------------------------


def _duplicate(root, x, y):
    gconn = _g(root)
    try:
        lo, hi = sorted((x, y))
        graph.upsert_assertion(gconn, src_id=lo, dst_id=hi, edge_type="duplicates",
                               asserted_by="human", status="active", now=iso_now())
        gconn.commit()
    finally:
        gconn.close()


def _state(root, sid):
    gconn = _g(root)
    try:
        links = sorted((r["dst_id"], r["asserted_by"], r["status"])
                       for r in graph.source_tag_links(gconn, sid))
        nodes = sorted(tuple(r) for r in gconn.execute(
            "SELECT node_id, slug, status FROM nodes WHERE node_type = 'item'"))
    finally:
        gconn.close()
    return _ledger(root, sid), links, nodes


@pytest.mark.parametrize("side", ["suggestion", "chosen"])
@pytest.mark.parametrize("earlier_resolution", [False, True])
def test_use_x_refuses_before_any_write_when_a_duplicate_partner_page_is_foreign(
        vault, side, earlier_resolution):
    root, a, b = vault
    # b suggests "Beta"; the user resolves it to an existing tag, "Alpha", human-added on a.
    _extract(root, {"beta": [_item("Beta")]}, source_ids=[b])
    gconn = _g(root)
    try:
        added = human_add.add_candidate(gconn, root=root, source_id=a, item_type="method_technique",
                                        title="Alpha", wiki_dir=root / "wiki",
                                        reviews_dir=root / "reviews")
    finally:
        gconn.close()
    assert added["outcome"] != "blocked", added
    thing, other = items.node_id("Beta"), items.node_id("Alpha")
    _legacy_collision(root, b)                  # Caf records `caf`, whose page belongs to Café
    _duplicate(root, thing if side == "suggestion" else other, C)
    if earlier_resolution:                      # an unfinished "use Café" for the same suggestion
        tag_links._write_resolving(root / "reviews", {
            "source_id": b, "node_id": thing, "name": "Beta", "chosen": CPP,
            "chosen_title": "Café", "chosen_was_kept": False, "op_id": "op_earlier", "at": iso_now()})
    before_pending = tag_links.pending_resolutions(root / "reviews", b)
    before = _state(root, b)
    out = _call(root, tag_links.use_existing, source_id=b, node_id=thing, chosen=other)
    assert out["outcome"] == "blocked" and out["reason"] == item_pages.OWNED_BY_OTHER
    assert "pending" not in out
    assert _state(root, b) == before                                  # ledger and graph unchanged
    assert other not in _ledger(root, b)["kept"]
    assert tag_links.pending_resolutions(root / "reviews", b) == before_pending
    assert _title(root) == "Café"
