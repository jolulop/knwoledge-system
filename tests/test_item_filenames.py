"""ADR-0071 slice B1b — test group 1: filename ALLOCATION.

The rule (decision 1) and the readable base (decision 2); recorded paths preserved by extraction,
publication and activation (decision 4); the 8-hex suffix collision still refused by the B1 guards;
the report-only `item_filename_nonconforming` lint.
"""
from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph  # noqa: E402
from app.workers import human_add, item_pages, items, lint, page_names, publication  # noqa: E402
from app.workers.wiki_render import parse_frontmatter  # noqa: E402
from tests.fixtures import legacy_item_slug  # noqa: E402
from tests.test_tag_links import _build, _extract, _g, _item, _keep, _sids  # noqa: E402


def _hex(name: str) -> str:
    return items.node_id(name)[4:12]


# --- the shared helper (decision 2) -------------------------------------------------------------


@pytest.mark.parametrize("name,base", [
    ("café", "cafe"),
    (unicodedata.normalize("NFD", "café"), "cafe"),      # composed and decomposed: one base
    ("straße", "strasse"), ("ẞ", "ss"),                   # the map lists both cases explicitly
    ("æsir", "aesir"), ("Æsir", "aesir"),
    ("øl", "ol"), ("Øl", "ol"),
    ("i̇stanbul", "istanbul"),                            # "İstanbul".lower(): i + combining dot
    ("ﬁle", "file"),                                      # NFKD compatibility forms
    ("山田 太郎", ""), ("!!!", ""), ("—", ""),             # non-Latin and punctuation-only: empty
    ("data-mesh", "data-mesh"), ("c++", "c"),
])
def test_base_slug(name, base):
    assert page_names.base_slug(name) == base


def test_losslessness_is_judged_on_the_original_name():
    assert page_names.is_lossless("data mesh") and page_names.is_lossless("cafe")
    for lossy in ("café", "data-mesh", "data  mesh", " data", "c++", "", "İstanbul".lower()):
        assert not page_names.is_lossless(lossy), lossy


# --- the item rule (decision 1) -----------------------------------------------------------------


@pytest.mark.parametrize("name,expected", [
    ("Data Mesh", "data-mesh"),
    ("  data   MESH ", "data-mesh"),                    # the id normalization: case + spacing
    ("AI", "ai"),
    ("2024", "2024"),
])
def test_a_lossless_name_keeps_its_plain_slug(name, expected):
    assert items._slug(name) == expected


@pytest.mark.parametrize("name,base", [
    ("Data-Mesh", "data-mesh"), ("Café", "cafe"), ("Straße", "strasse"), ("İstanbul", "istanbul"),
    ("Regulation (EU) 575/2013", "regulation-eu-575-2013"), ("C++", "c"),
    ("山田", "item"), ("!!!", "item"),
])
def test_a_lossy_name_gets_its_base_and_8_hex_of_its_id(name, base):
    assert items._slug(name) == f"{base}--{_hex(name)}"


def test_pairs_that_used_to_share_a_page_now_get_distinct_names():
    for a, b in (("Data-Mesh", "Data Mesh"), ("Café", "Cafe"), ("Café", "Caf"),
                 ("Straße", "Strasse"), ("C++", "C"), ("山田", "太郎"), ("!!!", "???")):
        assert items._slug(a) != items._slug(b), (a, b)
    # A plain slug never contains "--", so it can never equal a suffixed one.
    assert "--" not in items._slug("Data Mesh") and "--" in items._slug("Data-Mesh")


def test_the_suffix_comes_from_the_frozen_id_not_the_current_name():
    nid = items.node_id("Old-Name")
    assert items.page_slug("New-Name", nid) == f"new-name--{nid[4:12]}"
    assert items.page_slug("New Name", nid) == "new-name"


# --- minting and preserved paths (decision 4) ---------------------------------------------------


def test_extraction_mints_by_the_rule(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    _extract(tmp_path, {"alpha": [_item("Data-Mesh"), _item("Data Mesh"), _item("山田")]})
    for name in ("Data-Mesh", "Data Mesh", "山田"):
        page = tmp_path / "wiki" / "Items" / f"{items._slug(name)}.md"
        assert parse_frontmatter(page.read_text(encoding="utf-8"))["item_id"] == items.node_id(name)


def test_recorded_paths_are_never_recomputed(tmp_path):
    """A legacy-named item keeps its page through re-extraction, a full republish and an activation
    that amends only aliases; only an explicit title amendment moves it, by the new rule."""
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _extract(tmp_path, {"alpha": [_item("Data-Mesh")], "beta": [_item("Second-Tag")]})
    nid = items.node_id("Data-Mesh")
    legacy_item_slug(tmp_path, nid, "data-mesh")          # minted by the pre-ADR-0071 rule
    legacy = tmp_path / "wiki" / "Items" / "data-mesh.md"
    _extract(tmp_path, {"alpha": [_item("Data-Mesh")], "beta": [_item("Second-Tag")]}, force=True)
    publication.republish_all(tmp_path)
    assert legacy.exists()
    assert _keep(tmp_path, a, "Data-Mesh", aliases=["DM"])["outcome"] == "kept"
    gconn = _g(tmp_path)
    try:
        assert graph.get_node(gconn, nid)["slug"] == "data-mesh"
    finally:
        gconn.close()
    assert parse_frontmatter(legacy.read_text(encoding="utf-8"))["aliases"] == ["DM"]

    # An explicit title amendment is the one place a path moves (and it follows the new rule).
    sid2 = items.node_id("Second-Tag")
    legacy_item_slug(tmp_path, sid2, "second-tag")
    assert _keep(tmp_path, b, "Second-Tag", title="Second-Tag Renamed")["outcome"] == "kept"
    moved = tmp_path / "wiki" / "Items" / f"second-tag-renamed--{sid2[4:12]}.md"
    assert moved.exists() and not (tmp_path / "wiki" / "Items" / "second-tag.md").exists()


def test_an_8_hex_suffix_collision_is_still_refused(tmp_path, monkeypatch):
    """Collision-resistant, not collision-free: two lossy names whose ids share 8 hex would meet; the
    B1 ownership guard refuses the second before any write."""
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item("Data-Mesh")]})
    taken = items._slug("Data-Mesh")
    monkeypatch.setattr(items, "page_slug", lambda name, node_id: taken)   # force the 8-hex clash
    gconn = _g(tmp_path)
    try:
        out = human_add.add_candidate(gconn, root=tmp_path, source_id=a, item_type="method_technique",
                                      title="Other-Thing", wiki_dir=tmp_path / "wiki",
                                      reviews_dir=tmp_path / "reviews")
        assert out["outcome"] == "blocked" and out["reason"] == "slug_collision"
        assert out["detail"] == item_pages.OWNED_BY_OTHER
        assert out["existing_node_id"] == items.node_id("Data-Mesh")
        assert graph.get_node(gconn, items.node_id("Other-Thing")) is None
    finally:
        gconn.close()


# --- the report-only lint (decision 4) ----------------------------------------------------------


def test_the_lint_reports_actual_and_expected_paths_and_nothing_else(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    _extract(tmp_path, {"alpha": [_item("Data-Mesh"), _item("Plain Name")]})
    gconn = _g(tmp_path)
    try:
        assert lint._check_item_filenames(gconn, tmp_path / "wiki") == []      # a conforming vault
    finally:
        gconn.close()
    nid = items.node_id("Data-Mesh")
    legacy_item_slug(tmp_path, nid, "data-mesh")
    before = sorted(p.name for p in (tmp_path / "reviews").rglob("*.json"))
    gconn = _g(tmp_path)
    try:
        (finding,) = lint._check_item_filenames(gconn, tmp_path / "wiki")
        assert graph.get_node(gconn, nid)["slug"] == "data-mesh"              # nothing renamed
    finally:
        gconn.close()
    assert finding["check"] == "item_filename_nonconforming" and finding["severity"] == "low"
    assert finding["subject"] == nid
    assert finding["data"] == {"title": "Data-Mesh", "actual": "Items/data-mesh.md",
                               "expected": f"Items/data-mesh--{nid[4:12]}.md"}
    assert sorted(p.name for p in (tmp_path / "reviews").rglob("*.json")) == before   # nothing filed
    assert (tmp_path / "wiki" / "Items" / "data-mesh.md").exists()


def test_the_lint_never_fails_a_run(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    _extract(tmp_path, {"alpha": [_item("Data-Mesh")]})
    legacy_item_slug(tmp_path, items.node_id("Data-Mesh"), "data-mesh")
    out = lint.run_lint(tmp_path, record_job=False, file_review_items=True)
    assert out["by_check"].get("item_filename_nonconforming") == 1
    assert not any(f["check"] == "item_filename_nonconforming" and f["severity"] != "low"
                   for f in out["findings"])
    assert out["review_items_filed"] == []
