"""ADR-0071 slice B1b — test group 3: AUTHORS PUBLICATION RECOVERY.

The decision-2 filename change for Authors pages is a migration, not regeneration: every Source page
that links an Authors page is refreshed to the new name, the replacement pages are written before any
Source page links them, and an old page is removed only once nothing links it. Interrupted at each
step, the retry completes it; a Source page never links a missing Authors page.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import manifests  # noqa: E402
from app.workers import authors, publication, wiki  # noqa: E402
from tests.test_authors import TEMPLATES, _a, _confirm, _page, vault  # noqa: E402,F401

NAME = "José Saramago"
IDENT = manifests.name_identity(NAME)
OLD, NEW = authors.legacy_filename_for(IDENT), authors.filename_for(IDENT)


def _links(root, sid):
    text = (root / "wiki" / "Sources" / f"{sid}.md").read_text(encoding="utf-8")
    return set(re.findall(r"\[\[Authors/([^\]|#]+)", text))


def _no_dangling(root):
    for path in (root / "wiki" / "Sources").glob("*.md"):
        for stem in re.findall(r"\[\[Authors/([^\]|#]+)", path.read_text(encoding="utf-8")):
            assert _page(root, stem).exists(), (path.name, stem)


def _publish(root, **kw):
    return publication.publish(root, index=True, keyword=False, templates_dir=TEMPLATES, **kw)


@pytest.fixture
def legacy(vault, monkeypatch):  # noqa: F811 - the imported fixture
    """A vault whose Authors page and Source links were published under the previous rule."""
    root, sids = vault
    sid = sids["one.md"]
    _confirm(root, sid, _a(NAME))
    assert OLD != NEW and OLD.startswith("jos-saramago--") and NEW.startswith("jose-saramago--")
    with monkeypatch.context() as m:
        m.setattr(authors, "filename_for", authors.legacy_filename_for)
        assert publication.republish_all(root)["status"] == publication.PUBLISHED
    assert _page(root, OLD).exists() and _links(root, sid) == {OLD}
    return root, sid


def test_the_old_page_is_still_recognised_as_generated(legacy):
    root, sid = legacy
    assert authors.ownership(_page(root, OLD)) == "owned"


def test_an_index_publication_migrates_links_then_pages(legacy):
    root, sid = legacy
    assert authors.stale_link_sources(root) == {sid}
    out = _publish(root)                                   # no Source named: the widening finds it
    assert out["status"] == publication.PUBLISHED
    assert _links(root, sid) == {NEW} and _page(root, NEW).exists() and not _page(root, OLD).exists()
    assert out["authors_removed"] == [OLD]
    assert authors.stale_link_sources(root) == set()
    _no_dangling(root)


def test_a_failed_source_refresh_keeps_the_old_page_and_stays_pending(legacy, monkeypatch):
    root, sid = legacy
    real = wiki.generate_wiki

    def broken(*a, **kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr(wiki, "generate_wiki", broken)
    out = _publish(root)
    assert out["status"] == publication.PENDING
    assert _links(root, sid) == {OLD} and _page(root, OLD).exists()      # still linked, so kept
    assert any(w.startswith("authors_still_linked:") for w in out["warnings"])
    _no_dangling(root)
    monkeypatch.setattr(wiki, "generate_wiki", real)
    assert publication.publish(root, templates_dir=TEMPLATES)["status"] == publication.PUBLISHED
    assert _links(root, sid) == {NEW} and not _page(root, OLD).exists()
    _no_dangling(root)


def test_a_crash_before_the_index_step_leaves_no_dangling_link(legacy, monkeypatch):
    """Source pages refreshed, then the index step fails: the new page was written first, so the
    refreshed link resolves; the old page lingers unlinked until the retry prunes it."""
    root, sid = legacy
    real = publication._publish_authors

    def crash(root_, wiki_dir, out):
        out["warnings"].append("authors_publication_failed:crash")
        return False
    monkeypatch.setattr(publication, "_publish_authors", crash)
    out = _publish(root)
    assert out["status"] == publication.PENDING
    assert _links(root, sid) == {NEW} and _page(root, NEW).exists()
    _no_dangling(root)
    monkeypatch.setattr(publication, "_publish_authors", real)
    assert publication.publish(root, templates_dir=TEMPLATES)["status"] == publication.PUBLISHED
    assert not _page(root, OLD).exists()
    _no_dangling(root)


def test_a_still_linked_page_is_never_pruned(legacy):
    """Directly: authors.publish refuses to remove an old page a Source page still links."""
    root, sid = legacy
    from app.backend import write_lock
    with write_lock.hold(root / "reviews"):
        res = authors.publish(root)
    assert res["removed"] == [] and res["still_linked"] == [
        {"file": f"Authors/{OLD}.md", "sources": [sid]}]
    assert _page(root, OLD).exists() and _page(root, NEW).exists()


def test_lossless_names_do_not_move(vault):  # noqa: F811 - the imported fixture
    root, sids = vault
    _confirm(root, sids["one.md"], _a("Ada Lovelace"))
    assert authors.legacy_filename_for(manifests.name_identity("Ada Lovelace")) == "ada-lovelace"
    assert publication.republish_all(root)["status"] == publication.PUBLISHED
    assert _page(root, "ada-lovelace").exists()
    assert authors.stale_link_sources(root) == set()


# --- review round 1: failed or unverifiable replacements, incomplete link scans -------------------


def _source_only(root, sid):
    return publication.publish(root, sources=[sid], templates_dir=TEMPLATES)


def _retry(root):
    return publication.publish(root, templates_dir=TEMPLATES)        # the pending queue only


def _fail_writes_to(monkeypatch, stem):
    real = authors._write_atomically

    def failing(path, content):
        if path.stem == stem:
            raise OSError(28, "No space left on device")
        return real(path, content)
    monkeypatch.setattr(authors, "_write_atomically", failing)
    return real


def test_a_failed_replacement_write_keeps_the_old_links_through_repeated_retries(legacy, monkeypatch):
    root, sid = legacy
    real = _fail_writes_to(monkeypatch, NEW)
    for _attempt in range(2):                                       # repeated failures
        out = _publish(root)
        assert out["status"] == publication.PENDING
        assert out["failed"]["sources"] == {sid: "authors_page_unavailable"}
        assert any(w.startswith(f"authors_write_failed:Authors/{NEW}.md") for w in out["warnings"])
        assert _links(root, sid) == {OLD} and _page(root, OLD).exists()
        assert not _page(root, NEW).exists()
        _no_dangling(root)
    monkeypatch.setattr(authors, "_write_atomically", real)
    assert _retry(root)["status"] == publication.PUBLISHED
    assert _links(root, sid) == {NEW} and not _page(root, OLD).exists()
    _no_dangling(root)


def test_a_replacement_conflict_keeps_the_old_links(legacy):
    root, sid = legacy
    squatter = _page(root, NEW)
    squatter.write_text("# my own notes\n", encoding="utf-8")       # handwritten, at the new name
    out = _publish(root)
    assert out["status"] == publication.PENDING
    assert out["failed"]["sources"] == {sid: "authors_page_unavailable"}
    assert f"authors_conflict:Authors/{NEW}.md:handwritten" in out["warnings"]
    assert _links(root, sid) == {OLD} and _page(root, OLD).exists()
    assert squatter.read_text(encoding="utf-8") == "# my own notes\n"   # never overwritten
    _no_dangling(root)
    squatter.unlink()                                               # the human resolves it
    assert _retry(root)["status"] == publication.PUBLISHED
    assert _links(root, sid) == {NEW} and not _page(root, OLD).exists()
    _no_dangling(root)


def test_an_unreadable_inventory_keeps_new_links_back(legacy):
    root, sid = legacy
    bad = root / "raw" / "manifests" / "src_0000000000000000.json"
    bad.write_text("{not json", encoding="utf-8")
    out = _publish(root)
    assert out["status"] == publication.PENDING
    assert any(w.startswith("authors_publication_failed:") for w in out["warnings"])
    assert _links(root, sid) == {OLD} and _page(root, OLD).exists() and not _page(root, NEW).exists()
    _no_dangling(root)
    bad.unlink()
    assert _retry(root)["status"] == publication.PUBLISHED
    assert _links(root, sid) == {NEW}
    _no_dangling(root)


def test_an_unreadable_source_page_prevents_pruning(legacy):
    root, sid = legacy
    source = root / "wiki" / "Sources" / f"{sid}.md"
    source.chmod(0)                                                 # still links OLD, can't be read
    try:
        out = _publish(root)
        assert out["status"] == publication.PENDING
        assert any(w.startswith("authors_link_scan_failed:") for w in out["warnings"])
        assert _page(root, OLD).exists()                            # the unreadable page's target
        from app.backend import write_lock
        with write_lock.hold(root / "reviews"):
            res = authors.publish(root)
        assert res["removed"] == [] and res["link_scan_failed"]
    finally:
        source.chmod(0o644)
    _no_dangling(root)
    assert _retry(root)["status"] == publication.PUBLISHED
    assert _links(root, sid) == {NEW} and not _page(root, OLD).exists()
    _no_dangling(root)


def test_a_missing_sources_folder_is_empty_but_an_unlistable_one_is_not(tmp_path):
    assert authors.source_links(tmp_path / "wiki") == {}            # a new vault: no Source pages
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "Sources").write_text("not a folder", encoding="utf-8")
    with pytest.raises(authors.LinkScanError):
        authors.source_links(tmp_path / "wiki")
    (tmp_path / "wiki" / "Sources").unlink()
    (tmp_path / "wiki" / "Sources").mkdir()
    (tmp_path / "wiki" / "Sources").chmod(0)
    try:
        with pytest.raises(authors.LinkScanError):
            authors.source_links(tmp_path / "wiki")
    finally:
        (tmp_path / "wiki" / "Sources").chmod(0o755)


def test_a_source_only_refresh_publishes_a_newly_confirmed_author(vault):  # noqa: F811
    root, sids = vault
    sid = sids["one.md"]
    assert publication.republish_all(root)["status"] == publication.PUBLISHED
    _confirm(root, sid, _a("Grace Hopper"))
    out = _source_only(root, sid)                                   # no index step
    assert out["status"] == publication.PUBLISHED, out["warnings"]
    assert _links(root, sid) == {"grace-hopper"} and _page(root, "grace-hopper").exists()
    _no_dangling(root)


def test_an_existing_link_to_an_invalid_page_keeps_the_source_back(vault):  # noqa: F811
    """Every intended link is checked, not only new ones: a page already linked that is no longer a
    valid Authors page (here a handwritten file over it) keeps the Source page pending."""
    root, sids = vault
    sid = sids["one.md"]
    _confirm(root, sid, _a("Ada Lovelace"))
    assert publication.republish_all(root)["status"] == publication.PUBLISHED
    assert _links(root, sid) == {"ada-lovelace"}
    _page(root, "ada-lovelace").write_text("# notes\n", encoding="utf-8")
    _confirm(root, sid, _a("Ada Lovelace"), title="A New Title")
    out = _source_only(root, sid)
    assert out["status"] == publication.PENDING
    assert out["failed"]["sources"] == {sid: "authors_page_unavailable"}
    assert "A New Title" not in (root / "wiki" / "Sources" / f"{sid}.md").read_text(encoding="utf-8")


def test_a_failed_update_of_an_existing_page_stays_pending_on_a_source_only_refresh(
        vault, monkeypatch):  # noqa: F811 - the imported fixture
    """The page exists with the right identity, so the Source link resolves and the Source page is
    published — but the page's own update failed, so the Authors work stays pending."""
    root, sids = vault
    one, two = sids["one.md"], sids["two.md"]
    _confirm(root, one, _a("Ada Lovelace"))
    assert publication.republish_all(root)["status"] == publication.PUBLISHED
    before = _page(root, "ada-lovelace").read_text(encoding="utf-8")
    _confirm(root, two, _a("Ada Lovelace"))                          # the page must list `two` too
    real = _fail_writes_to(monkeypatch, "ada-lovelace")
    out = _source_only(root, two)
    assert out["status"] == publication.PENDING
    assert out["sources_rendered"] == [two] and _links(root, two) == {"ada-lovelace"}
    assert any(w.startswith("authors_write_failed:Authors/ada-lovelace.md") for w in out["warnings"])
    assert _page(root, "ada-lovelace").read_text(encoding="utf-8") == before
    _no_dangling(root)
    again = _retry(root)                                             # still failing: still pending
    assert again["status"] == publication.PENDING
    monkeypatch.setattr(authors, "_write_atomically", real)
    assert _retry(root)["status"] == publication.PUBLISHED
    assert two in _page(root, "ada-lovelace").read_text(encoding="utf-8")
    _no_dangling(root)


# --- review round 2: direct generation (generate_wiki) enforces the same prerequisite --------------


def _generate(root, **kw):
    return wiki.generate_wiki(root, force=True, templates_dir=TEMPLATES, **kw)


def _crash_before_final_refresh(monkeypatch):
    def crash(self):
        raise RuntimeError("interrupted before the end refresh")
    monkeypatch.setattr(publication.Run, "finish", crash)


def test_direct_generation_interrupted_before_its_final_refresh_never_dangles(legacy, monkeypatch):
    """The replacement is written before the Source page links it; the crash leaves only the old,
    now unlinked page, which the queued retry prunes."""
    root, sid = legacy
    _crash_before_final_refresh(monkeypatch)
    with pytest.raises(RuntimeError):
        _generate(root)
    assert _links(root, sid) == {NEW} and _page(root, NEW).exists() and _page(root, OLD).exists()
    _no_dangling(root)
    monkeypatch.undo()
    assert _retry(root)["status"] == publication.PUBLISHED
    assert not _page(root, OLD).exists()
    _no_dangling(root)


def test_direct_generation_with_a_failed_replacement_keeps_the_old_links(legacy, monkeypatch):
    root, sid = legacy
    real = _fail_writes_to(monkeypatch, NEW)
    from app.backend import write_lock
    with write_lock.hold(root / "reviews"):                    # a caller that publishes itself
        step = _generate(root, rebuild_index=False, record_job=False)
    assert {e["source_id"]: e["reason"] for e in step["error_details"]} == {
        sid: authors.UNAVAILABLE}
    assert _links(root, sid) == {OLD} and not _page(root, NEW).exists()
    _no_dangling(root)

    run = _generate(root)                                      # a direct run: still failing
    assert run["status"] == "partial" and run["errors"] == 1
    assert run["publication"]["status"] == publication.PENDING
    assert _links(root, sid) == {OLD} and _page(root, OLD).exists()
    _no_dangling(root)

    _crash_before_final_refresh(monkeypatch)                   # failing AND interrupted
    with pytest.raises(RuntimeError):
        _generate(root)
    assert _links(root, sid) == {OLD}
    _no_dangling(root)

    monkeypatch.undo()                                         # writes work again (and finish)
    monkeypatch.setattr(authors, "_write_atomically", real)
    assert _retry(root)["status"] == publication.PUBLISHED
    assert _links(root, sid) == {NEW} and not _page(root, OLD).exists()
    _no_dangling(root)
