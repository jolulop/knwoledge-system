"""ADR-0070 B5 — author/publisher name variants: a hint on the Details form and a report-only lint.
Nothing merges; identity (`name_identity`) and independence are unchanged until Details are saved."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.backend import main as main_module  # noqa: E402
from app.backend import manifests  # noqa: E402
from app.backend.config import get_settings  # noqa: E402
from app.workers import lint, name_variants as nv  # noqa: E402
from tests.test_tag_links import _build, _extract, _item, _sids  # noqa: E402

NOW = "2026-11-06T10:00:00+00:00"


@pytest.mark.parametrize("a,b,want", [
    ("McKinsey", "McKinsey & Company", "containment"),
    ("D. C. Dennett", "Daniel C. Dennett", "initials"),
    ("A. Smith", "Adam Smith", "initials"),            # a hint, never evidence
    ("José Saramago", "Jose Saramago", "containment"),  # accents, for the hint only
    ("Smith & Sons", "Smith and Sons", "containment"),  # & / and
    ("O'Reilly Media", "OReilly Media", "containment"),  # apostrophes / punctuation
    ("AI", "AI Lab", None),                             # short token: never through (a)
    ("University", "Oxford University", None),          # generic institutional words
    ("Press", "MIT Press", None),
    ("J. Smith", "Adam Smith", None),                   # different people sharing a surname
    ("Williams", "William", None),                      # no plural folding for names
    ("Dennett", "Daniel Dennett", "containment"),
])
def test_the_matching_rule(a, b, want):
    assert nv.rule(nv.fold(a), nv.fold(b)) == want


def _confirm(root, sid, *, authors=(), publisher=None, title="T"):
    manifests.confirm_details(root / "raw" / "manifests", sid, title=title,
                              authors=[{"name": n, "show_page": False} for n in authors],
                              publisher={"name": publisher, "show_page": False} if publisher
                              else None, baseline="b" * 16, now=NOW)


@pytest.fixture
def vault(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body", "c.md": "gamma body"})
    _extract(tmp_path, {"alpha": [_item("Strategy")]})        # the document screen needs a graph
    s = _sids(tmp_path)
    _confirm(tmp_path, s["b.md"], publisher="McKinsey & Company", authors=["Daniel C. Dennett"],
             title="Report B")
    _confirm(tmp_path, s["c.md"], authors=["McKinsey & Company"], title="Report C")
    return tmp_path, s


def test_hints_name_the_role_and_documents_and_exclude_this_document(vault):
    root, s = vault
    names = nv.confirmed_names(root / "raw" / "manifests")
    (h,) = nv.hints("McKinsey", names, exclude_source=s["a.md"], budget=nv.Budget(100))
    assert h["name"] == "McKinsey & Company"
    assert {(u["role"], u["title"]) for u in h["uses"]} == {("publisher", "Report B"),
                                                            ("author", "Report C")}
    mine = nv.hints("McKinsey", names, exclude_source=s["b.md"], budget=nv.Budget(100))
    assert [u["title"] for u in mine[0]["uses"]] == ["Report C"]       # this document left out
    assert nv.hints("McKinsey & Company", names, exclude_source=None,
                    budget=nv.Budget(100)) == []                         # the same identity


def test_at_most_three_hints_ordered_by_documents_then_name():
    names = {}
    for i, (name, docs) in enumerate([("Acme Widgets", 1), ("Acme Corp Labs", 3),
                                      ("Acme Holdings", 2), ("Acme Partners", 2)]):
        names[manifests.name_identity(name)] = {
            "name": name, "tokens": nv.fold(name),
            "uses": [{"role": "publisher", "source_id": f"src_{i}{j}", "title": "x"}
                     for j in range(docs)]}
    found = nv.hints("Acme", names, exclude_source=None, budget=nv.Budget(100))
    assert [h["name"] for h in found] == ["Acme Corp Labs", "Acme Holdings", "Acme Partners"]


def test_the_comparison_budget_is_deterministic_and_reported():
    names = {manifests.name_identity(f"Smith {i:03d}x"): {
        "name": f"Smith {i:03d}x", "tokens": nv.fold(f"Smith {i:03d}x"),
        "uses": [{"role": "author", "source_id": f"src_{i}", "title": "t"}]} for i in range(60)}
    names[manifests.name_identity("Smith")] = {"name": "Smith", "tokens": nv.fold("Smith"),
                                                "uses": [{"role": "author", "source_id": "src_s",
                                                          "title": "t"}]}
    budget = nv.Budget(10)
    first = nv.hints("Smith", names, exclude_source=None, budget=budget)
    assert budget.truncated and budget.used == 10
    assert nv.hints("Smith", names, exclude_source=None, budget=nv.Budget(10)) == first
    run = nv.variant_pairs(names, budget_limit=25)
    assert run["truncated"] and run["compared"] == 25
    assert nv.variant_pairs(names, budget_limit=25) == run
    full = nv.variant_pairs(names, budget_limit=10_000)
    assert len(full["pairs"]) == 50 and full["more"] == 10 and not full["truncated"]


# --- the form ------------------------------------------------------------------------------------


@pytest.fixture
def client(vault, monkeypatch):
    root, s = vault
    monkeypatch.setattr(main_module, "settings", get_settings(root))
    return TestClient(main_module.app), root, s


def _manifest_bytes(root):
    return {p.name: p.read_bytes() for p in (root / "raw" / "manifests").glob("*.json")}


def test_the_form_hints_and_use_this_spelling_changes_only_that_field(client):
    http, root, s = client
    before = _manifest_bytes(root)
    form = {"screen_action": "redraw", "details_title": "My edited title",
            "author_row_0": "1", "author_order_0": "0", "author_name_0": "D. C. Dennett",
            "publisher_name": "McKinsey"}
    page = http.post(f"/ui/reviews/sources/{s['a.md']}/decide", data=form).text
    assert "Already confirmed elsewhere" in page
    assert "“McKinsey &amp; Company” (publisher of “Report B”, author of “Report C”)" in page
    assert "“Daniel C. Dennett” (author of “Report B”)" in page
    assert "value='use_spelling:publisher_name:McKinsey &amp; Company'" in page
    used = http.post(f"/ui/reviews/sources/{s['a.md']}/decide", data={
        **form, "screen_action": "", "details_action": "use_spelling:publisher_name:McKinsey & Company"}
    ).text
    assert "name='publisher_name' size='40' list='ks-known-names' value='McKinsey &amp; Company'" in used
    assert "value='My edited title'" in used and "value='D. C. Dennett'" in used   # kept
    assert _manifest_bytes(root) == before                    # nothing saved; independence unchanged


def test_a_forged_field_name_is_ignored(client):
    http, root, s = client
    before = _manifest_bytes(root)
    page = http.post(f"/ui/reviews/sources/{s['a.md']}/decide", data={
        "details_title": "T", "details_action": "use_spelling:details_title:Hacked"}).text
    assert "value='Hacked'" not in page
    assert _manifest_bytes(root) == before


# --- the lint ------------------------------------------------------------------------------------


def test_the_lint_reports_variants_and_files_nothing(vault):
    root, s = vault
    _confirm(root, s["a.md"], authors=["D. C. Dennett"], title="Report A")
    out = lint.run_lint(root, record_job=False, file_review_items=True)
    found = [f for f in out["findings"] if f["check"] == "name_variant_possible"]
    pair = next(f for f in found if "Dennett" in f["subject"])
    assert pair["severity"] == "low" and pair["data"]["rule"] == "initials"
    uses = pair["data"]["a"]["uses"] + pair["data"]["b"]["uses"]
    assert {u["screen"] for u in uses} == {f"/ui/reviews/sources/{s['a.md']}",
                                          f"/ui/reviews/sources/{s['b.md']}"}
    assert out["review_items_filed"] == []


@pytest.mark.parametrize("action", ["use_spelling:publisher_name", "use_spelling",
                                    "use_spelling::", "use_spellingX:publisher_name:Y"])
def test_a_malformed_spelling_action_redraws_the_draft_and_writes_nothing(client, action):
    http, root, s = client
    before = _manifest_bytes(root)
    resp = http.post(f"/ui/reviews/sources/{s['a.md']}/decide", data={
        "details_title": "Kept title", "publisher_name": "McKinsey", "details_action": action})
    assert resp.status_code == 200
    assert "value='Kept title'" in resp.text
    assert "list='ks-known-names' value='McKinsey'" in resp.text
    assert _manifest_bytes(root) == before
