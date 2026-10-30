"""ADR-0066 part 3 slice 3: confirmed Details in the manifest's provenance.

Covers the storage shape and its strict validation (slice-3 grill Q2), legacy reading and key
precedence, Clear keeping unrelated provenance, set-based author independence and the shared
normalization behind the reconciliation digest.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import manifests  # noqa: E402
from app.workers import intake  # noqa: E402
from tests import fixtures  # noqa: E402


def _a(*names, show=False):
    return [{"name": n, "show_page": show} for n in names]


@pytest.fixture
def sid(tmp_path):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.write_markdown(inbox / "doc.md")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    (m,) = manifests.list_manifests(tmp_path / "raw" / "manifests")
    return m["source_id"]


def _prov(tmp_path, sid):
    return manifests.load_manifest(tmp_path / "raw" / "manifests", sid)["provenance"]


# --- independence (Q2) ---------------------------------------------------------------------------


@pytest.mark.parametrize("a,b,independent", [
    (_a("Alice", "Bob"), _a("Alice", "Carol"), False),           # one shared author
    (_a("Alice", "Bob"), _a("Bob", "Alice"), False),             # reordered: the same set
    (_a("Alice"), _a("Carol"), True),                            # disjoint
    (_a("José García"), _a("josé  garcía"), False),              # a spelling variant of one name
    (_a("Robin Woods"), _a("Robin Wood"), True),                 # no singularization
])
def test_authors_compare_as_identity_sets(a, b, independent):
    assert manifests.independent_sources({"authors": a}, {"authors": b}) is independent


def test_any_shared_comparable_field_blocks_independence():
    assert manifests.independent_sources(
        {"authors": _a("Alice"), "publisher": {"name": "Acme", "show_page": True}},
        {"authors": _a("Carol"), "publisher": "acme"}) is False      # mixed publisher shapes
    assert manifests.independent_sources({"authors": _a("Alice")}, {"publisher": "Acme"}) is False
    assert manifests.independent_sources({"authors": []}, {"authors": []}) is False


def test_authors_wins_over_the_legacy_author_even_when_empty():
    assert manifests.confirmed_details({"authors": [], "author": "Alice"})["authors"] == []
    assert manifests.confirmed_details({"author": "Alice"})["authors"] == _a("Alice")
    assert manifests.independent_sources({"authors": [], "author": "Alice"},
                                         {"author": "Alice"}) is False   # nothing comparable
    assert manifests.independent_sources({"authors": _a("Bob"), "author": "Alice"},
                                         {"author": "Alice"}) is True


def test_the_digest_ignores_order_variants_show_page_title_and_audit_fields():
    base = {"authors": _a("Alice", "Bob"), "publisher": "Acme", "title": "One"}
    same = {"authors": [{"name": "bob", "show_page": True}, {"name": "ALICE", "show_page": False}],
            "publisher": {"name": "acme", "show_page": True}, "title": "Two",
            "confirmed_at": "2026-10-30T00:00:00Z", "confirmed_baseline": "abc"}
    assert manifests.independence_digest(base) == manifests.independence_digest(same)
    assert manifests.independence_digest(base) != manifests.independence_digest(
        {**base, "authors": _a("Alice")})
    assert manifests.independence_digest({}) == manifests.independence_digest({"title": "x"})


# --- storage, validation and Clear (Q2) ----------------------------------------------------------


def test_confirm_writes_the_new_keys_and_removes_the_legacy_author(tmp_path, sid):
    md = tmp_path / "raw" / "manifests"
    manifests.set_provenance(md, sid, author="Old Name", report_family="r1",
                             canonical_url="http://x.org/a")
    manifests.confirm_details(md, sid, title="  A Title ", authors=_a("Ada Lovelace"),
                              publisher={"name": "Example Press", "show_page": True},
                              baseline="b" * 16, now="2026-10-30T00:00:00Z")
    prov = _prov(tmp_path, sid)
    assert "author" not in prov and prov["title"] == "A Title"
    assert prov["authors"] == _a("Ada Lovelace")
    assert prov["publisher"] == {"name": "Example Press", "show_page": True}
    assert prov["report_family"] == "r1" and prov["confirmed_baseline"] == "b" * 16


@pytest.mark.parametrize("field,value", [
    ("title", "   "),
    ("authors", [{"name": " ", "show_page": False}]),
    ("authors", _a("Ada", "ada")),                               # duplicate normalized author
    ("authors", [{"name": "Ada", "show_page": "yes"}]),           # not a boolean
    ("authors", [{"name": "Ada"}]),                              # missing show_page
    ("authors", "Ada"),
    ("publisher", {"name": "", "show_page": False}),
    ("publisher", {"name": "Acme", "show_page": 1}),
])
def test_malformed_details_are_rejected_and_write_nothing(tmp_path, sid, field, value):
    md = tmp_path / "raw" / "manifests"
    before = manifests.load_manifest(md, sid)
    with pytest.raises(ValueError):
        manifests.set_provenance(md, sid, **{field: value})
    assert manifests.load_manifest(md, sid) == before


def test_clear_removes_details_and_keeps_unrelated_provenance(tmp_path, sid):
    md = tmp_path / "raw" / "manifests"
    manifests.set_provenance(md, sid, author="Legacy", report_family="r1",
                             canonical_url="http://x.org/a")
    manifests.confirm_details(md, sid, title="T", authors=_a("Ada"), publisher=None,
                              baseline="c" * 16, now="2026-10-30T00:00:00Z")
    manifests.set_provenance(md, sid, author="Legacy again")       # a stray legacy value
    manifests.clear_details(md, sid)
    assert _prov(tmp_path, sid) == {"report_family": "r1", "canonical_url": "http://x.org/a"}
