"""ADR-0066 part 3 slice 4: Authors & publishers pages.

Covers the filename and identity rules (grill 2 Q3), the inventory under source visibility (slice-4
grill Q2), the page schema and ownership (Q1), publication's Authors step with pruning, collisions
and the lock flag (Q3), Source-page links, the index section, the validators and name search.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import rebuild_index  # noqa: E402
import validate_frontmatter  # noqa: E402
import validate_wiki  # noqa: E402

from app.backend import keyword_index, manifests, publication_queue, write_lock  # noqa: E402
from app.workers import authors, extract, intake, publication, wiki  # noqa: E402
from app.workers.wiki_render import parse_frontmatter  # noqa: E402
from tests import fixtures  # noqa: E402

TEMPLATES = ROOT / "templates"
NOW = "2026-10-30T00:00:00Z"


def _a(*names, show=True):
    return [{"name": n, "show_page": show} for n in names]


@pytest.fixture
def vault(tmp_path):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.copy_templates(tmp_path)
    for name in ("one.md", "two.md", "three.md"):
        (inbox / name).write_text(f"# {name}\n\nA document called {name} with some prose.\n",
                                  encoding="utf-8")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    extract.extract_sources(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    sids = {m["original_filename"]: m["source_id"]
            for m in manifests.list_manifests(tmp_path / "raw" / "manifests")}
    return tmp_path, sids


def _confirm(root, sid, authors_=(), publisher=None, title="A Title"):
    manifests.confirm_details(root / "raw" / "manifests", sid, title=title, authors=list(authors_),
                              publisher=publisher, baseline="b" * 16, now=NOW)


def _set_status(root, sid, status):
    m = manifests.load_manifest(root / "raw" / "manifests", sid)
    m["status"] = status
    manifests.save_manifest(root / "raw" / "manifests", m)


def _publish(root):
    with write_lock.hold(root / "reviews"):
        return authors.publish(root)


def _render(root):
    wiki.generate_wiki(root, jobs_db=root / "db" / "jobs.sqlite", templates_dir=TEMPLATES,
                       rebuild_index=False, force=True)


def _page(root, stem):
    return root / "wiki" / "Authors" / f"{stem}.md"


# --- filenames and identity (grill 2 Q3) ---------------------------------------------------------


@pytest.mark.parametrize("name,stem", [
    ("Ada Lovelace", "ada-lovelace"),
    ("  ADA   lovelace ", "ada-lovelace"),
    ("Robin Woods", "robin-woods"),
    ("Robin Wood", "robin-wood"),
])
def test_lossless_names_keep_their_plain_slug(name, stem):
    assert authors.filename_for(manifests.name_identity(name)) == stem


def test_lossy_names_get_a_reserved_hash_suffix_that_never_collides_with_a_plain_slug():
    accented = authors.filename_for(manifests.name_identity("José"))
    non_latin = authors.filename_for(manifests.name_identity("山田 太郎"))
    assert accented.startswith("jos--") and non_latin.startswith("unnamed--")
    lookalike = authors.filename_for(manifests.name_identity("jos " + accented.split("--")[1]))
    assert lookalike != accented and "--" not in lookalike


# --- the inventory (slice-4 grill Q2) ------------------------------------------------------------


def test_an_empty_vault_has_an_empty_inventory(vault):
    root, _ = vault
    assert authors.inventory(root) == []


def test_only_confirmed_ticked_visible_documents_are_listed(vault):
    root, sids = vault
    _confirm(root, sids["one.md"], _a("Ada Lovelace") + _a("Alan Turing", show=False),
             publisher={"name": "Example Press", "show_page": True})
    _confirm(root, sids["two.md"], _a("ADA LOVELACE"))
    manifests.set_provenance(root / "raw" / "manifests", sids["three.md"], author="Ada Lovelace")
    pages = {p["filename"]: p for p in authors.inventory(root)}
    assert set(pages) == {"ada-lovelace", "example-press"}          # unticked/legacy: no page
    ada = pages["ada-lovelace"]
    assert {d["source_id"] for d in ada["roles"]["author"]} == {sids["one.md"], sids["two.md"]}
    # One each: the tie goes to the first spelling in code-point order; aliases sort the same way.
    assert ada["aliases"] == ["ADA LOVELACE", "Ada Lovelace"] and ada["title"] == "ADA LOVELACE"
    assert pages["example-press"]["roles"]["publisher"][0]["source_id"] == sids["one.md"]


def test_hidden_sources_are_never_listed_and_their_spelling_disappears(vault):
    root, sids = vault
    _confirm(root, sids["one.md"], _a("Ada Lovelace"))
    _confirm(root, sids["two.md"], _a("A. Lovelace ada lovelace"[:0] + "ada lovelace"))
    _set_status(root, sids["two.md"], "hidden")
    (page,) = authors.inventory(root)
    assert [d["source_id"] for d in page["roles"]["author"]] == [sids["one.md"]]
    assert page["aliases"] == ["Ada Lovelace"] and page["title"] == "Ada Lovelace"
    _set_status(root, sids["one.md"], "archived")
    assert authors.inventory(root) == []


def test_the_inventory_is_deterministic(vault):
    root, sids = vault
    for sid, name in ((sids["one.md"], "Zed"), (sids["two.md"], "Ada"), (sids["three.md"], "Mia")):
        _confirm(root, sid, _a(name))
    first = authors.inventory(root)
    assert first == authors.inventory(root)
    assert [p["title"] for p in first] == ["Ada", "Mia", "Zed"]


def test_a_malformed_manifest_stops_the_step_before_pruning(vault):
    root, sids = vault
    _confirm(root, sids["one.md"], _a("Ada Lovelace"))
    _publish(root)
    _confirm(root, sids["one.md"], _a("Ada Lovelace", show=False))  # would prune the page
    (root / "raw" / "manifests" / "src_000000000000dead.json").write_text("{", encoding="utf-8")
    with pytest.raises(authors.InventoryError):
        _publish(root)
    assert _page(root, "ada-lovelace").exists()


# --- the page and ownership (Q1) -----------------------------------------------------------------


def test_a_generated_page_has_the_schema_and_safe_names(vault):
    root, sids = vault
    _confirm(root, sids["one.md"], _a('Eve "[[Items/x|evil]]" Smith'),
             publisher={"name": "Eve \"[[Items/x|evil]]\" Smith", "show_page": True})
    _publish(root)
    (path,) = (root / "wiki" / "Authors").glob("*.md")
    text = path.read_text(encoding="utf-8")
    fm = parse_frontmatter(text)
    assert fm["type"] == "author" and fm["author_id"] == path.stem
    assert fm["generated_by"] == "authors-v1" and fm["status"] == "active"
    assert "[[Items/" not in text                                   # no wikilink injected
    assert "author of 1 document and publisher of 1 document" in text
    assert authors.ownership(path) == "owned"
    assert validate_frontmatter.main([str(root)]) == 0


@pytest.mark.parametrize("change,state", [
    (lambda t: t.replace("generated_by: authors-v1", ""), "handwritten"),
    (lambda t: t.replace("type: author", "type: item"), "invalid"),
    (lambda t: t.replace('author_id: "ada-lovelace"', 'author_id: "other"'), "invalid"),
    (lambda t: t.replace('name_identity: "ada lovelace"', 'name_identity: "grace hopper"'),
     "invalid"),
])
def test_each_failed_ownership_condition(vault, change, state):
    root, sids = vault
    _confirm(root, sids["one.md"], _a("Ada Lovelace"))
    _publish(root)
    path = _page(root, "ada-lovelace")
    path.write_text(change(path.read_text(encoding="utf-8")), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    assert authors.ownership(path) == state
    out = _publish(root)
    assert out["conflicts"] == [{"file": "Authors/ada-lovelace.md", "state": state,
                                 "name": "Ada Lovelace"}]
    assert path.read_text(encoding="utf-8") == before               # never overwritten
    _confirm(root, sids["one.md"], _a("Ada Lovelace", show=False))
    _publish(root)
    assert path.exists()                                            # never pruned either


def test_a_hash_collision_with_another_identity_is_refused(vault, monkeypatch):
    root, sids = vault
    _confirm(root, sids["one.md"], _a("José"))
    _publish(root)
    (path,) = (root / "wiki" / "Authors").glob("*.md")
    monkeypatch.setattr(authors, "filename_for", lambda ident: path.stem)   # force a collision
    _confirm(root, sids["two.md"], _a("Josée"))
    out = _publish(root)
    assert {"file": f"Authors/{path.name}", "state": "another_identity", "name": "Josée"} \
        in out["conflicts"]
    assert parse_frontmatter(path.read_text(encoding="utf-8"))["name_identity"] == "josé"


def test_pruning_rename_untick_and_shared_names(vault):
    root, sids = vault
    _confirm(root, sids["one.md"], _a("Ada Lovelace"))
    _confirm(root, sids["two.md"], _a("Ada Lovelace"))
    _publish(root)
    _confirm(root, sids["one.md"], _a("Ada Lovelace", show=False))  # still referenced by two.md
    assert _publish(root)["removed"] == [] and _page(root, "ada-lovelace").exists()
    _confirm(root, sids["two.md"], _a("Ada King"))                  # a correction: renamed
    out = _publish(root)
    assert out["removed"] == ["ada-lovelace"] and _page(root, "ada-king").exists()
    assert _publish(root)["written"] == []                          # unchanged: no writes


# --- Source pages link only what lists them back (Q2) --------------------------------------------


def test_source_page_links_follow_ticks_and_visibility(vault):
    root, sids = vault
    one, two = sids["one.md"], sids["two.md"]
    _confirm(root, one, _a("Ada Lovelace") + _a("Alan Turing", show=False))
    _confirm(root, two, _a("Ada Lovelace"))
    _render(root)
    _publish(root)
    text = (root / "wiki" / "Sources" / f"{one}.md").read_text(encoding="utf-8")
    assert "- Authors: [[Authors/ada-lovelace|Ada Lovelace]], Alan Turing" in text
    _set_status(root, two, "hidden")                                 # one of several references
    _render(root)
    _publish(root)
    hidden = (root / "wiki" / "Sources" / f"{two}.md").read_text(encoding="utf-8")
    assert "[[Authors/" not in hidden and "- Authors: Ada Lovelace" in hidden
    assert validate_wiki.main([str(root)]) == 0
    _set_status(root, one, "hidden")                                 # the last reference
    _render(root)
    _publish(root)
    assert not _page(root, "ada-lovelace").exists()
    _set_status(root, one, "active")                                 # unhide restores both
    _set_status(root, two, "active")
    _render(root)
    _publish(root)
    assert _page(root, "ada-lovelace").exists()
    assert "[[Authors/ada-lovelace|" in (root / "wiki" / "Sources" / f"{two}.md").read_text()
    assert manifests.load_manifest(root / "raw" / "manifests", two)["provenance"]["authors"] == \
        _a("Ada Lovelace")                                          # the saved tick never changed


# --- publication and the index (Q3) --------------------------------------------------------------


def _with_scripts(root):
    """The scripts plus the app package they import (as the Apply sandbox copies them)."""
    for d in ("scripts", "app"):
        shutil.copytree(ROOT / d, root / d, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))


def test_publication_renders_pages_then_the_index_section(vault):
    root, sids = vault
    _with_scripts(root)
    _confirm(root, sids["one.md"], _a("Ada Lovelace"))
    out = publication.publish(root, origin="test", sources=[sids["one.md"]], index=True,
                              keyword=True)
    assert out["status"] != publication.PENDING and out["authors_written"] == ["ada-lovelace"]
    index = (root / "wiki" / "index.md").read_text(encoding="utf-8")
    assert "## Authors & publishers (1)" in index
    assert "- [[Authors/ada-lovelace|Ada Lovelace]] — author of 1" in index


def test_a_collision_keeps_index_and_keyword_work_pending(vault):
    root, sids = vault
    _with_scripts(root)
    _confirm(root, sids["one.md"], _a("Ada Lovelace"))
    (root / "wiki" / "Authors").mkdir(parents=True)
    _page(root, "ada-lovelace").write_text("# my own notes\n", encoding="utf-8")
    out = publication.publish(root, origin="test", sources=[sids["one.md"]], index=True,
                              keyword=True)
    assert out["status"] == publication.PENDING
    assert "authors_conflict:Authors/ada-lovelace.md:handwritten" in out["warnings"]
    (entry,) = publication_queue.load(root)
    assert entry["index"] is True and entry["keyword"] is True
    _page(root, "ada-lovelace").unlink()                             # the person resolves it
    out = publication.publish(root, origin="retry")
    assert out["status"] != publication.PENDING and publication_queue.load(root) == []


def test_the_index_rebuilds_standalone_with_its_own_lock(vault):
    root, sids = vault
    _with_scripts(root)
    _confirm(root, sids["one.md"], _a("Ada Lovelace"))
    _publish(root)
    lock = root / "reviews" / "tag_links" / ".lock"
    lock.unlink()                                                   # only a lock holder recreates it
    proc = subprocess.run([sys.executable, str(root / "scripts" / "rebuild_index.py"), str(root)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "[[Authors/ada-lovelace|Ada Lovelace]]" in (root / "wiki" / "index.md").read_text()
    assert lock.exists()                                            # it took the write lock


def test_the_index_section_is_omitted_when_empty_and_sorted_otherwise():
    assert rebuild_index.authors_section([]) == []
    people = [{"title": "Ada", "filename": "ada", "roles": {"author": [1], "publisher": []}},
              {"title": "Mia", "filename": "mia", "roles": {"author": [1, 2], "publisher": [1]}}]
    lines = rebuild_index.authors_section(people)
    assert lines[2:4] == ["- [[Authors/ada|Ada]] — author of 1",
                          "- [[Authors/mia|Mia]] — author of 2, publisher of 1"]


# --- validators ----------------------------------------------------------------------------------


def _consistent(root, sids):
    _confirm(root, sids["one.md"], _a("Ada Lovelace"))
    _render(root)
    _publish(root)


def test_a_consistent_vault_validates_and_handwritten_files_are_only_noted(vault, capsys):
    root, sids = vault
    _consistent(root, sids)
    (root / "wiki" / "Authors" / "notes.md").write_text("my notes\n", encoding="utf-8")
    assert validate_wiki.main([str(root)]) == 0 and validate_frontmatter.main([str(root)]) == 0
    assert "handwritten file" in capsys.readouterr().out


@pytest.mark.parametrize("breakage", ["count", "role", "missed_prune", "claimed_invalid",
                                      "dangling_backlink"])
def test_validate_wiki_catches_authors_inconsistencies(vault, breakage):
    root, sids = vault
    _consistent(root, sids)
    path = _page(root, "ada-lovelace")
    text = path.read_text(encoding="utf-8")
    if breakage == "count":
        path.write_text(text.replace("author of 1 document", "author of 3 documents"))
    elif breakage == "role":
        path.write_text(text.replace("roles: [author]", "roles: [author, publisher]"))
    elif breakage == "missed_prune":
        _confirm(root, sids["one.md"], _a("Ada Lovelace", show=False))
        _render(root)                       # the Source page no longer links it: only the page is stale
    elif breakage == "claimed_invalid":
        path.write_text(text.replace("type: author", "type: item"))
    else:
        src = root / "wiki" / "Sources" / f"{sids['two.md']}.md"
        src.write_text(src.read_text() + "\n- Authors: [[Authors/ada-lovelace|Ada]]\n")
    assert validate_wiki.main([str(root)]) == 1


def test_validate_frontmatter_checks_author_roles(vault):
    root, sids = vault
    _consistent(root, sids)
    path = _page(root, "ada-lovelace")
    path.write_text(path.read_text().replace("roles: [author]", "roles: [author, author]"))
    assert validate_frontmatter.main([str(root)]) == 1


# --- search ---------------------------------------------------------------------------------------


def test_a_name_search_finds_the_page_and_it_is_never_answer_eligible(vault):
    from app.backend import search
    root, sids = vault
    _consistent(root, sids)
    keyword_index.reindex(root)
    conn = keyword_index.connect(root / keyword_index.DB_RELPATH)
    try:
        rows = search.search_navigation(conn, search.build_fts_match(["lovelace"], op="OR"),
                                        page_type="author", node_statuses=("active",),
                                        language=None, prefusion_limit=10, limit=10)
    finally:
        conn.close()
    (row,) = rows
    assert row["path"] == "wiki/Authors/ada-lovelace.md" and row["answer_eligible"] is False



def test_document_titles_on_an_authors_page_are_link_safe(vault):
    root, sids = vault
    _confirm(root, sids["one.md"], _a("Ada Lovelace"), title="Part A | Part B")
    _render(root)
    _publish(root)
    text = _page(root, "ada-lovelace").read_text(encoding="utf-8")
    assert f"- [[Sources/{sids['one.md']}|Part A Part B]]" in text   # no stray alias separator


# --- review round 1 on slice 4 -------------------------------------------------------------------


def test_awkward_names_round_trip_and_stay_owned(vault):
    yaml = pytest.importorskip("yaml")
    root, sids = vault
    names = ['Eve "Smith"', "O'Connor'", "ACME\\quality", "Bracket [[x]] Co"]
    _confirm(root, sids["one.md"], _a(*names), publisher={"name": "ACME\\quality", "show_page": True})
    first = _publish(root)
    assert first["conflicts"] == []
    for path in sorted((root / "wiki" / "Authors").glob("*.md")):
        assert authors.ownership(path) == "owned"
        text = path.read_text(encoding="utf-8")
        fm = yaml.safe_load(text.split("---\n")[1])                 # valid YAML
        ours = authors.read_frontmatter(path)
        assert {k: fm[k] for k in ("author_id", "title", "aliases", "name_identity")} == \
            {k: ours[k] for k in ("author_id", "title", "aliases", "name_identity")}
        assert fm["title"] in names and fm["aliases"] == [fm["title"]] and "[[x]]" not in text
    again = _publish(root)                                          # a retry: nothing to redo
    assert again["conflicts"] == [] and again["written"] == []


@pytest.mark.parametrize("existing", [False, True])
def test_a_failed_write_never_leaves_a_page_retry_cannot_replace(vault, monkeypatch, existing):
    root, sids = vault
    if existing:
        _confirm(root, sids["one.md"], _a("Ada Lovelace"))
        _publish(root)
        before = _page(root, "ada-lovelace").read_text(encoding="utf-8")
        _confirm(root, sids["two.md"], _a("Ada Lovelace"))          # the page must be replaced
    else:
        _confirm(root, sids["one.md"], _a("Ada Lovelace"))
    real = authors._write_durably

    def dies(path, data):
        real(path, data[: len(data) // 2])                          # half written, then the crash
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(authors, "_write_durably", dies)
    with pytest.raises(OSError):
        _publish(root)
    page = _page(root, "ada-lovelace")
    assert (page.read_text(encoding="utf-8") == before) if existing else not page.exists()
    assert not [p for p in (root / "wiki" / "Authors").iterdir() if ".tmp-" in p.name]
    monkeypatch.setattr(authors, "_write_durably", real)
    out = _publish(root)                                            # retry recovers by itself
    assert out["conflicts"] == [] and authors.ownership(page) == "owned"


def test_malformed_provenance_in_valid_json_stops_the_step(vault):
    root, sids = vault
    _with_scripts(root)
    _confirm(root, sids["one.md"], _a("Ada Lovelace"))
    _publish(root)
    before = _page(root, "ada-lovelace").read_text(encoding="utf-8")
    m = manifests.load_manifest(root / "raw" / "manifests", sids["one.md"])
    m["provenance"]["authors"] = "Ada Lovelace"                     # a string, not a list
    manifests.save_manifest(root / "raw" / "manifests", m)
    with pytest.raises(authors.InventoryError):
        authors.inventory(root)
    out = publication.publish(root, origin="test", sources=[sids["one.md"]], index=True,
                              keyword=True)
    assert out["status"] == publication.PENDING
    assert _page(root, "ada-lovelace").read_text(encoding="utf-8") == before


def test_one_document_votes_once_for_a_spelling_across_both_roles(vault):
    root, sids = vault
    _confirm(root, sids["one.md"], _a("ADA"), publisher={"name": "ADA", "show_page": True})
    _confirm(root, sids["two.md"], _a("Ada"))
    _confirm(root, sids["three.md"], _a("Ada"))
    (page,) = authors.inventory(root)
    assert page["title"] == "Ada"                                   # 2 documents against 1


def test_both_spellings_in_one_document_stay_aliases_with_one_title_vote(vault):
    root, sids = vault
    _confirm(root, sids["one.md"], _a("Ada"), publisher={"name": "ADA", "show_page": True})
    (page,) = authors.inventory(root)
    assert page["aliases"] == ["ADA", "Ada"]                        # both spellings kept
    assert page["title"] == "Ada"         # one vote (the author's); a vote per role would tie → "ADA"
