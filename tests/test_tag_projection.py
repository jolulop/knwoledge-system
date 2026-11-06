"""ADR-0070 B1: kept tags as nested Obsidian tags on the Source page (`tags:`).

Unit: the encoding rule and the collision fixed point. Integration: the projection through real
keep/drop/block actions and publication, including a collision that rewrites another document's
page, an interrupted publication, visibility and drift."""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for p in (ROOT, SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import validate_projection  # noqa: E402

from app.backend import manifests  # noqa: E402
from app.workers import items, publication, tag_projection, wiki  # noqa: E402
from app.workers.tag_projection import TagMappingError, build_mapping, encode_label  # noqa: E402
from app.workers.wiki_render import parse_frontmatter  # noqa: E402
from tests.test_tag_links import (  # noqa: E402
    TEMPLATES, _block, _build, _call, _drop, _extract, _item, _keep, _sids)


def _id(hexpart: str) -> str:
    return "itm_" + hexpart.ljust(16, "0")


ID = _id("0123456789abcdef")


# --- encoding -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("name, label", [
    ("Graph Rewriting", "Graph_Rewriting"),
    ("  spaced   out  ", "spaced_out"),
    ("Research&Development", "Research_and_Development"),
    ("Research & Development", "Research_and_Development"),
    ("R&D", "R_and_D"),
    ("Children's Health", "Childrens_Health"),
    ("Children’s Health", "Childrens_Health"),
    ("Children‘s Health", "Childrens_Health"),
    ("Childrenʼs Health", "Childrens_Health"),
    ("Children`s Health", "Childrens_Health"),
    ("Children´s Health", "Childrens_Health"),
    ("Café Society", "Café_Society"),                       # accents and case kept
    ("C++", "C"),
    ("GPT-4o (mini)", "GPT_4o_mini"),
    ("2024", "2024"),                                       # digits only: no prefix
    ("ﬁne-tuning", "fine_tuning"),                          # NFKC
    ("AI / ML", "AI_ML"),
])
def test_encoding(name, label):
    assert encode_label(name, ID) == label


@pytest.mark.parametrize("name", ["++", "…", "   ", "''", "—", ""])
def test_punctuation_only_names_fall_back_to_the_item_id(name):
    assert encode_label(name, ID) == ID


def test_names_that_normalize_alike_share_a_base_label():
    assert encode_label("Research & Development", ID) == encode_label("Research&Development", ID)
    assert encode_label("C++", ID) == encode_label("C#", ID) == "C"


# --- collisions ---------------------------------------------------------------------------------------


def test_no_collision_no_suffix():
    m = build_mapping([(_id("a1"), "domain", "Neuroscience"), (_id("b2"), "domain", "Biology")])
    assert m == {_id("a1"): "domain/Neuroscience", _id("b2"): "domain/Biology"}


def test_same_label_under_different_types_does_not_clash():
    m = build_mapping([(_id("a1"), "domain", "Agents"), (_id("b2"), "ai_topic_area", "Agents")])
    assert set(m.values()) == {"domain/Agents", "ai_topic_area/Agents"}


def test_case_only_clash_suffixes_every_member():
    a, b = _id("aaaaaa11"), _id("bbbbbb22")
    m = build_mapping([(a, "domain", "ai"), (b, "domain", "AI")])
    assert m == {a: "domain/ai_aaaaaa", b: "domain/AI_bbbbbb"}


def test_collision_mapping_does_not_depend_on_order():
    rows = [(_id("aaaaaa11"), "domain", "C++"), (_id("bbbbbb22"), "domain", "C"),
            (_id("cccccc33"), "domain", "c#"), (_id("dddddd44"), "domain", "Other")]
    results = {json.dumps(build_mapping(p), sort_keys=True) for p in itertools.permutations(rows)}
    assert len(results) == 1


def test_shared_six_hex_prefix_extends_to_eight():
    a, b = _id("abcdef01"), _id("abcdef02")
    m = build_mapping([(a, "domain", "C"), (b, "domain", "c")])
    assert m == {a: "domain/C_abcdef01", b: "domain/c_abcdef02"}


def test_suffixed_label_equal_to_another_plain_label_is_resolved():
    a, b = _id("aaaaaa11"), _id("bbbbbb22")
    c = _id("cccccc33")                          # literally named what a's suffixed label would be
    m = build_mapping([(a, "domain", "C"), (b, "domain", "C"), (c, "domain", "C_aaaaaa")])
    assert len({v.casefold() for v in m.values()}) == 3
    assert m[c] != "domain/C_aaaaaa" or m[a] != "domain/C_aaaaaa"


def test_suffixes_extend_to_the_full_id():
    a, b = _id("0123456789abcd01"), _id("0123456789abcd02")   # share 14 hex
    m = build_mapping([(a, "domain", "Same"), (b, "domain", "same")])
    assert m == {a: "domain/Same_0123456789abcd01", b: "domain/same_0123456789abcd02"}


def test_the_id_fallback_collides_like_any_label():
    a = _id("aaaaaa11")
    named_like_the_fallback = _id("bbbbbb22")
    m = build_mapping([(a, "domain", "++"), (named_like_the_fallback, "domain", a)])
    assert len({v.casefold() for v in m.values()}) == 2
    assert m[a].startswith(f"domain/{a}_") and m[named_like_the_fallback].startswith(f"domain/{a}_")


def test_duplicate_or_malformed_ids_raise():
    with pytest.raises(TagMappingError):
        build_mapping([(_id("a1"), "domain", "X"), (_id("a1"), "domain", "Y")])
    with pytest.raises(TagMappingError):
        build_mapping([("itm_short", "domain", "X")])


def test_retype_changes_the_prefix():
    a = _id("a1")
    assert build_mapping([(a, "domain", "Agents")])[a] == "domain/Agents"
    assert build_mapping([(a, "architecture_pattern", "Agents")])[a] == "architecture_pattern/Agents"


def test_changed_ids():
    assert tag_projection.changed_ids(None, {"x": "a"}) is None
    assert tag_projection.changed_ids({"x": "a", "y": "b"}, {"x": "a2", "z": "c"}) == {"x", "y", "z"}
    assert tag_projection.changed_ids({"x": "a"}, {"x": "a"}) == set()


# --- integration ----------------------------------------------------------------------------------------


def _tags(tmp_path, sid):
    page = tmp_path / "wiki" / "Sources" / f"{sid}.md"
    return parse_frontmatter(page.read_text(encoding="utf-8")).get("tags")


def _projection_errors(tmp_path):
    return [e for e in validate_projection._check(tmp_path, tmp_path / "db" / "graph.sqlite")
            if "tags=" in e or "tag mapping" in e]


@pytest.fixture
def vault(tmp_path):
    _build(tmp_path, {"a.md": "alpha Straße Graph Rewriting body", "b.md": "beta STRASSE body"})
    sids = _sids(tmp_path)
    # Different ids and slugs (strasse--<hex> / strasse), but the same tag under casefold(): a collision.
    _extract(tmp_path, {"alpha": [_item("Straße", "domain"), _item("Graph Rewriting")],
                        "beta": [_item("STRASSE", "domain")]})
    return tmp_path, sids["a.md"], sids["b.md"]


def test_kept_tags_project_into_tags_and_suggestions_never_do(vault):
    root, a, b = vault
    assert _tags(root, a) == [] and _tags(root, b) == []           # suggestions only
    _keep(root, a, "Graph Rewriting")
    assert _tags(root, a) == ["method_technique/Graph_Rewriting"]
    fm = parse_frontmatter((root / "wiki" / "Sources" / f"{a}.md").read_text(encoding="utf-8"))
    assert "Graph Rewriting" in fm["keywords"] and "Straße" in fm["keywords"]   # keywords independent
    assert _projection_errors(root) == []


def test_a_collision_rewrites_the_other_documents_page_and_its_end_restores_it(vault):
    root, a, b = vault
    _keep(root, a, "Straße")
    assert _tags(root, a) == ["domain/Straße"]
    _keep(root, b, "STRASSE")                 # acts on b only; a's label must change too
    s1, s2 = items.node_id("Straße"), items.node_id("STRASSE")
    assert _tags(root, a) == [f"domain/Straße_{s1[4:10]}"]
    assert _tags(root, b) == [f"domain/STRASSE_{s2[4:10]}"]
    assert _projection_errors(root) == []
    _drop(root, b, "STRASSE")                 # STRASSE becomes unused: the collision disappears
    assert _tags(root, a) == ["domain/Straße"] and _tags(root, b) == []
    assert _projection_errors(root) == []


def test_an_interrupted_collision_publication_is_completed_by_the_retry(vault, monkeypatch):
    from app.backend import publication_queue
    root, a, b = vault
    _keep(root, a, "Straße")
    real = publication._render_sources

    def fail_all(root_, sids, *, documents, wiki_dir, templates_dir, failed):
        for sid in sids:
            failed[sid] = "render_failed"
        return []

    monkeypatch.setattr(publication, "_render_sources", fail_all)
    _keep(root, b, "STRASSE")
    assert publication_queue.load(root)                             # still pending
    assert _tags(root, a) == ["domain/Straße"]                     # stale: not yet refreshed
    assert _projection_errors(root)                                 # the backstop sees the drift
    monkeypatch.setattr(publication, "_render_sources", real)
    retry = publication.publish(root, templates_dir=TEMPLATES)
    assert retry["status"] == publication.PUBLISHED
    assert _tags(root, a) == [f"domain/Straße_{items.node_id('Straße')[4:10]}"]
    assert _projection_errors(root) == []


def test_a_lost_published_mapping_refreshes_every_source_page(vault):
    root, a, b = vault
    _keep(root, a, "Straße")
    tag_projection.snapshot_path(root).unlink()
    page = root / "wiki" / "Sources" / f"{b}.md"
    before = page.stat().st_mtime_ns
    out = publication.publish(root, sources=[a], templates_dir=TEMPLATES)
    assert set(out["sources_rendered"]) == {a, b}
    assert page.stat().st_mtime_ns == before                         # unchanged pages are not rewritten
    assert tag_projection.load_published(root) == {items.node_id("Straße"): "domain/Straße"}


def test_a_keep_that_changes_no_other_label_refreshes_only_its_documents(vault):
    root, a, b = vault
    _keep(root, a, "Straße")                                         # snapshot now known
    out = publication.publish(root, sources=[a], templates_dir=TEMPLATES)
    assert out["sources_rendered"] == [a]


def test_a_blocked_but_kept_tag_stays_in_tags(vault):
    root, a, b = vault
    _keep(root, a, "Straße")
    _block(root, "Straße")
    assert _tags(root, a) == ["domain/Straße"]
    assert _projection_errors(root) == []


def test_last_keep_dropped_leaves_tags_and_a_revive_restores(vault):
    root, a, b = vault
    _keep(root, a, "Graph Rewriting")
    _drop(root, a, "Graph Rewriting")
    assert _tags(root, a) == []
    from app.workers import merge_candidates, tag_links
    _call(root, tag_links.undo_drop, source_id=a, key=merge_candidates.merge_key("Graph Rewriting"))
    assert _tags(root, a) == []                                      # back to a suggestion only
    _keep(root, a, "Graph Rewriting")                                # revives the unused tag
    assert _tags(root, a) == ["method_technique/Graph_Rewriting"]


def test_title_amendment_on_keep_uses_the_amended_name(vault):
    root, a, b = vault
    _keep(root, a, "Graph Rewriting", title="Graph Rewrite Systems")
    assert _tags(root, a) == ["method_technique/Graph_Rewrite_Systems"]


def test_a_document_outside_default_visibility_carries_no_tags(vault):
    root, a, b = vault
    _keep(root, a, "Graph Rewriting")
    manifests.set_status(root / "raw" / "manifests", a, "archived")
    wiki.generate_wiki(root, source_ids=[a], templates_dir=TEMPLATES, rebuild_index=False,
                       record_job=False)
    assert _tags(root, a) == []
    assert _projection_errors(root) == []


def test_hand_edited_tags_change_nothing_and_are_flagged_then_restored(vault):
    root, a, b = vault
    _keep(root, a, "Graph Rewriting")
    page = root / "wiki" / "Sources" / f"{a}.md"
    page.write_text(page.read_text(encoding="utf-8").replace(
        'tags: ["method_technique/Graph_Rewriting"]', 'tags: ["domain/Invented"]'), encoding="utf-8")
    assert _projection_errors(root)                                  # drift is reported
    from app.workers import tag_links
    keeps, _ = tag_links.ledger_keeps(root / "reviews")
    assert items.node_id("Invented") not in keeps
    wiki.generate_wiki(root, source_ids=[a], templates_dir=TEMPLATES, rebuild_index=False,
                       record_job=False, force=True)
    assert _tags(root, a) == ["method_technique/Graph_Rewriting"]
    assert _projection_errors(root) == []


def test_a_mapping_that_cannot_be_built_keeps_every_source_page_pending(vault, monkeypatch):
    root, a, b = vault
    _keep(root, a, "Straße")
    before = (root / "wiki" / "Sources" / f"{a}.md").read_text(encoding="utf-8")

    def broken(gconn, wiki_dir):
        raise TagMappingError("duplicate item id itm_x")

    monkeypatch.setattr(tag_projection, "active_mapping", broken)
    out = publication.publish(root, sources=[a], templates_dir=TEMPLATES)
    assert out["status"] == publication.PENDING
    assert set(out["sources_failed"]) == {a, b}                      # nothing ambiguous is published
    assert (root / "wiki" / "Sources" / f"{a}.md").read_text(encoding="utf-8") == before
    monkeypatch.undo()
    assert publication.publish(root, templates_dir=TEMPLATES)["status"] == publication.PUBLISHED


# --- review round on B1: the resolver keeps saturated members and extends the others ---------------


def test_a_saturated_member_stays_while_the_others_extend():
    a, b = _id("0123456789abcd01"), _id("0123456789abcd02")    # need the full 16 to tell apart
    c = _id("ffffffffffffffff")                                 # named like a's full-suffix label
    m = build_mapping([(a, "domain", "X"), (b, "domain", "X"), (c, "domain", "X_0123456789abcd01")])
    assert m[a] == "domain/X_0123456789abcd01" and m[b] == "domain/X_0123456789abcd02"
    assert m[c] == "domain/X_0123456789abcd01_ffffff"
    assert len({v.casefold() for v in m.values()}) == 3


# --- review round on B1: the mapper takes a title only from the tag's own, valid page ---------------


def _set_page(root, name, text):
    from app.workers import item_pages
    gconn = __import__("tests.test_tag_links", fromlist=["_g"])._g(root)
    try:
        slug = gconn.execute("SELECT slug FROM nodes WHERE node_id = ?",
                             (items.node_id(name),)).fetchone()["slug"]
    finally:
        gconn.close()
    page = item_pages.page_path(root / "wiki", slug)
    if text is None:
        page.unlink()
    else:
        page.write_bytes(text)


@pytest.mark.parametrize("damage", ["missing", "unreadable", "wrong_owner", "no_title"])
def test_the_mapper_refuses_a_tag_without_a_valid_page_of_its_own(vault, damage):
    root, a, b = vault
    _keep(root, a, "Straße")
    page = root / "wiki" / "Items" / f"{items._slug('Straße')}.md"   # ADR-0071: strasse--<hex>
    original = page.read_bytes()
    text = original.decode("utf-8")
    _set_page(root, "Straße", {
        "missing": None,
        "unreadable": b"\xff\xfe\x00 not utf-8",
        "wrong_owner": text.replace(items.node_id("Straße"), items.node_id("Other")).encode(),
        "no_title": text.replace('title: "Straße"', 'title: ""').encode(),
    }[damage])
    gconn = __import__("tests.test_tag_links", fromlist=["_g"])._g(root)
    try:
        with pytest.raises(TagMappingError):
            tag_projection.active_mapping(gconn, root / "wiki")
    finally:
        gconn.close()
    out = publication.publish(root, sources=[a], templates_dir=TEMPLATES)
    assert out["status"] == publication.PENDING                     # nothing borrowed or invented
    assert _tags(root, a) == ["domain/Straße"]                      # the page is left as it was
    page.write_bytes(original)
    assert publication.publish(root, templates_dir=TEMPLATES)["status"] == publication.PUBLISHED


def test_repeated_recovery_failures_stay_queued_until_fixed(vault, monkeypatch):
    from app.backend import publication_queue
    root, a, b = vault
    _keep(root, a, "Straße")

    def broken(gconn, wiki_dir):
        raise TagMappingError("still broken")

    monkeypatch.setattr(tag_projection, "active_mapping", broken)
    publication.publish(root, sources=[a], templates_dir=TEMPLATES)
    first = publication_queue.load(root)
    publication.publish(root, templates_dir=TEMPLATES)
    second = publication_queue.load(root)
    assert first and second
    assert max(e["attempts"] for e in second) > max(e["attempts"] for e in first)
    monkeypatch.undo()
    assert publication.publish(root, templates_dir=TEMPLATES)["status"] == publication.PUBLISHED
    assert publication_queue.load(root) == []


# --- review round on B1: real interruptions --------------------------------------------------------


def test_a_crash_after_the_authority_commit_is_recovered_by_the_next_publication(vault):
    """The UI records the intent, the keep commits, and the process dies before publishing."""
    from app.backend import publication_queue, write_lock
    from app.workers import tag_links
    from tests.test_tag_links import _g
    root, a, b = vault
    _keep(root, a, "Straße")
    with write_lock.hold(root / "reviews"):
        publication_queue.record(root, origin="tag_action", sources=[b],
                                 items=[items.node_id("STRASSE")])
    gconn = _g(root)
    try:
        out = tag_links.keep(gconn, source_id=b, node_id=items.node_id("STRASSE"),
                             wiki_dir=root / "wiki", reviews_dir=root / "reviews")
        gconn.commit()
    finally:
        gconn.close()
    assert out["outcome"] == "kept"
    assert _tags(root, a) == ["domain/Straße"]                      # the crash: nothing published
    result = publication.publish(root, templates_dir=TEMPLATES)     # e.g. Retry publishing
    assert result["status"] == publication.PUBLISHED
    assert _tags(root, a) == [f"domain/Straße_{items.node_id('Straße')[4:10]}"]
    assert _tags(root, b) == [f"domain/STRASSE_{items.node_id('STRASSE')[4:10]}"]
    assert _projection_errors(root) == []


def test_a_partial_source_refresh_keeps_the_snapshot_and_the_retry_finishes(vault, monkeypatch):
    root, a, b = vault
    _keep(root, a, "Straße")
    published = tag_projection.load_published(root)
    real = publication._render_sources

    def fail_a(root_, sids, *, documents, wiki_dir, templates_dir, failed):
        done = real(root_, [s for s in sids if s != a], documents=documents, wiki_dir=wiki_dir,
                    templates_dir=templates_dir, failed=failed)
        if a in sids:
            failed[a] = "render_failed"
        return done

    monkeypatch.setattr(publication, "_render_sources", fail_a)
    out = _keep(root, b, "STRASSE")
    assert out["outcome"] == "kept"
    assert _tags(root, b) == [f"domain/STRASSE_{items.node_id('STRASSE')[4:10]}"]   # b refreshed
    assert _tags(root, a) == ["domain/Straße"]                                      # a not yet
    assert tag_projection.load_published(root) == published          # snapshot not advanced
    monkeypatch.setattr(publication, "_render_sources", real)
    assert publication.publish(root, templates_dir=TEMPLATES)["status"] == publication.PUBLISHED
    assert _tags(root, a) == [f"domain/Straße_{items.node_id('Straße')[4:10]}"]
    assert _projection_errors(root) == []


# --- review round on B1: real Apply paths ----------------------------------------------------------


@pytest.fixture
def api(vault, monkeypatch):
    from fastapi.testclient import TestClient

    from app.backend import main as main_module
    from app.backend.config import get_settings
    root, a, b = vault
    monkeypatch.setattr(main_module, "settings", get_settings(root))
    return TestClient(main_module.app)


def _approve(root, rid, rtype, subject, proposal, decided_at=None):
    d = root / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({"review_id": rid, "type": rtype, "status": "approved",
                                               "subject": subject, "proposal": proposal,
                                               **({"decided_at": decided_at} if decided_at else {})}),
                                   encoding="utf-8")


def _apply(api):
    from tests import fixtures
    body = fixtures.apply(api).json()
    assert body["status"] == "applied", body
    return body


def test_apply_retype_creates_and_ends_a_collision(vault, api):
    root, a, b = vault
    _keep(root, a, "Straße")
    _keep(root, b, "STRASSE")
    s1, s2 = items.node_id("Straße"), items.node_id("STRASSE")
    _approve(root, "rev_rt1", "change_item_type", {"node_id": s2, "to_item_type": "use_case"},
             {"to_item_type": "use_case"})
    _apply(api)                                     # different types: no collision any more
    assert _tags(root, a) == ["domain/Straße"] and _tags(root, b) == ["use_case/STRASSE"]
    # ADR-0070 B4: a later type change of the same tag must be provably approved after the first
    # one ran (real approvals carry decided_at)
    _approve(root, "rev_rt2", "change_item_type", {"node_id": s2, "to_item_type": "domain"},
             {"to_item_type": "domain"}, decided_at="2099-01-01T00:00:00+00:00")
    _apply(api)                                     # back to one type: both suffixed
    assert _tags(root, a) == [f"domain/Straße_{s1[4:10]}"]
    assert _tags(root, b) == [f"domain/STRASSE_{s2[4:10]}"]
    assert _projection_errors(root) == []


def test_apply_merge_ends_a_collision(vault, api):
    root, a, b = vault
    _keep(root, a, "Straße")
    _keep(root, b, "STRASSE")
    s1, s2 = items.node_id("Straße"), items.node_id("STRASSE")
    assert _tags(root, a) == [f"domain/Straße_{s1[4:10]}"]
    _approve(root, "rev_m", "merge_items", {"survivor_node_id": s1, "absorbed_node_id": s2},
             {"to_status": "merged"})
    _apply(api)
    assert _tags(root, a) == ["domain/Straße"] and _tags(root, b) == ["domain/Straße"]
    assert _projection_errors(root) == []


def test_apply_split_creates_a_collision(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.backend import main as main_module
    from app.backend.config import get_settings
    _build(tmp_path, {"a.md": "alpha Straße body", "b.md": "beta Straße STRASSE body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _extract(tmp_path, {"alpha": [_item("Straße", "domain")], "beta": [_item("Straße", "domain")]})
    _keep(tmp_path, a, "Straße")
    _keep(tmp_path, b, "Straße")
    assert _tags(tmp_path, a) == _tags(tmp_path, b) == ["domain/Straße"]
    s1, s2 = items.node_id("Straße"), items.node_id("STRASSE")
    _approve(tmp_path, "rev_s", "split_item", {"node_id": s1, "spinoff_node_id": s2},
             {"spinoff_name": "STRASSE", "spinoff_sources": [b], "spinoff_aliases": []})
    monkeypatch.setattr(main_module, "settings", get_settings(tmp_path))
    _apply(TestClient(main_module.app))
    assert _tags(tmp_path, a) == [f"domain/Straße_{s1[4:10]}"]       # the untouched document too
    assert _tags(tmp_path, b) == [f"domain/STRASSE_{s2[4:10]}"]
    assert _projection_errors(tmp_path) == []
