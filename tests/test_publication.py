"""ADR-0068 slice 2: the one publication function and the publication pending queue.

Covers acceptance tests 3 (concurrent tag changes cannot publish stale pages or search), 4 (a
successful screen action exposes updated pages and search at once) and 7 (a crash between the
authority write and publication keeps the decision and recovers), plus the queue's fail-closed
rules: a damaged queue refuses changes, never reads as empty, and is repaired without loss."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import republish
import validate_publication

from fastapi.testclient import TestClient

from app.backend import graph, keyword_index, publication_queue, write_lock
from app.backend import main as main_module
from app.backend.config import get_settings
from app.workers import items, publication, tag_links, wiki
from tests.test_tag_links import _build, _extract, _item, _sids, _titles, _view


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    settings.manifests_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


def _setup(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    a = _sids(tmp_path)["a.md"]
    _extract(tmp_path, {"alpha": [_item("Tag")]})
    return a, items.node_id("Tag")


def _source_page_lists_tag(tmp_path, sid) -> bool:
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        return not tag_links._source_page_drift(tmp_path / "wiki", gconn, sid)
    finally:
        gconn.close()


def _queue(tmp_path):
    return publication_queue.load(tmp_path)


def _failing_render(monkeypatch, fail: set[str]):
    """Make Source pages in `fail` fail to render (reported, as generate_wiki does)."""
    real = wiki.generate_wiki

    def render(root, *, source_ids=None, **kw):
        ok = [s for s in source_ids or [] if s not in fail]
        out = real(root, source_ids=ok, **kw) if ok else {"error_details": []}
        out["error_details"] = list(out.get("error_details") or []) + [
            {"source_id": s} for s in source_ids or [] if s in fail]
        return out
    monkeypatch.setattr(wiki, "generate_wiki", render)


# --- the queue file ----------------------------------------------------------------------------


@pytest.mark.parametrize("content", ["{not json", json.dumps({"schema_version": 99, "entries": []}),
                                     json.dumps({"schema_version": 1, "entries": [{"id": "x"}]})])
def test_a_damaged_queue_is_an_error_never_an_empty_queue(tmp_path, content):
    assert publication_queue.load(tmp_path) == []                # no file: nothing pending
    (tmp_path / "db").mkdir()
    publication_queue.queue_path(tmp_path).write_text(content, encoding="utf-8")
    with pytest.raises(publication_queue.QueueCorrupt):
        publication_queue.load(tmp_path)
    assert validate_publication.main([str(tmp_path)]) == 1
    assert publication_queue.status(tmp_path)["state"] == "corrupt"


def test_intent_is_recorded_only_under_the_write_lock(tmp_path):
    with pytest.raises(RuntimeError):
        publication_queue.record(tmp_path, origin="tag_action")
    with write_lock.hold(tmp_path / "reviews"):
        rid = publication_queue.record(tmp_path, origin="tag_action")
    assert [e["id"] for e in _queue(tmp_path)] == [rid]
    assert validate_publication.main([str(tmp_path)]) == 1       # pending fails validation


# --- acceptance 4: a screen action publishes pages and search at once --------------------------


def test_a_keep_publishes_the_source_page_and_search_before_it_returns(client, tmp_path):
    a, nid = _setup(tmp_path)
    keyword_index.reindex(tmp_path)
    resp = client.post(f"/reviews/sources/{a}/links", json={"node_id": nid, "action": "keep"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["publication"]["status"] == "published"
    assert _source_page_lists_tag(tmp_path, a)
    conn = keyword_index.connect_readonly(tmp_path / keyword_index.DB_RELPATH)
    try:
        assert keyword_index.consistency_errors(tmp_path, conn) == []   # search is current
    finally:
        conn.close()
    assert _queue(tmp_path) == []
    assert validate_publication.main([str(tmp_path)]) == 0


# --- a failed refresh stays queued; retry publishes it --------------------------------------------


def test_a_failed_refresh_is_saved_publication_pending_and_retry_clears_it(client, tmp_path,
                                                                            monkeypatch):
    a, nid = _setup(tmp_path)
    real = keyword_index.reindex

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(keyword_index, "reindex", broken)
    resp = client.post(f"/reviews/sources/{a}/links", json={"node_id": nid, "action": "keep"})
    body = resp.json()
    assert resp.status_code == 200
    assert body["publication"]["status"] == publication.PENDING
    assert body["warnings"] == ["keyword_reindex_failed:OSError", "publication_pending"]
    assert _titles(_view(tmp_path, a)["kept"]) == ["Tag"]          # the decision stands
    (entry,) = _queue(tmp_path)
    assert entry["keyword"] and not entry["sources"] and entry["attempts"] == 1
    assert validate_publication.main([str(tmp_path)]) == 1
    page = client.get("/ui/reviews").text
    assert "Saved; publication incomplete" in page and "/ui/publication/retry" in page

    monkeypatch.setattr(keyword_index, "reindex", real)
    resp = client.post("/ui/publication/retry")
    assert resp.status_code == 200 and "Publication complete" in resp.text
    assert _queue(tmp_path) == []
    assert validate_publication.main([str(tmp_path)]) == 0
    assert "Saved; publication incomplete" not in client.get("/ui/reviews").text


def test_the_next_action_also_publishes_pending_work(client, tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _extract(tmp_path, {"alpha": [_item("Tag")], "beta": [_item("Other")]})
    with pytest.MonkeyPatch.context() as mp:     # scoped: keep the client fixture's patches
        _failing_render(mp, {a})
        client.post(f"/reviews/sources/{a}/links",
                    json={"node_id": items.node_id("Tag"), "action": "keep"})
    assert [e["sources"] for e in _queue(tmp_path)] == [[a]]
    assert not _source_page_lists_tag(tmp_path, a)
    # An action on another document publishes its own work AND the pending entry, in one refresh.
    resp = client.post(f"/reviews/sources/{b}/links",
                       json={"node_id": items.node_id("Other"), "action": "keep"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["publication"]["status"] == "published"
    assert _queue(tmp_path) == []
    assert _source_page_lists_tag(tmp_path, a) and _source_page_lists_tag(tmp_path, b)


def _listed_items(tmp_path, sid):
    from app.workers.wiki_render import parse_frontmatter
    page = tmp_path / "wiki" / "Sources" / f"{sid}.md"
    return parse_frontmatter(page.read_text(encoding="utf-8")).get("items") or []


def test_a_retry_publishes_current_authority(client, tmp_path, monkeypatch):
    a, nid = _setup(tmp_path)
    client.post(f"/reviews/sources/{a}/links", json={"node_id": nid, "action": "keep"})
    assert _listed_items(tmp_path, a)                            # published: the page lists it
    _failing_render(monkeypatch, {a})
    client.post(f"/reviews/sources/{a}/links", json={"node_id": nid, "action": "drop"})
    (entry,) = _queue(tmp_path)
    # Entries name ids and steps only — never rendered content an old entry could restore.
    assert set(entry) == {"id", "origin", "created_at", "claims", "syntheses", "items", "sources",
                          "index", "keyword", "full", "provisional", "attempts"}
    assert _listed_items(tmp_path, a)                            # stale: still lists the tag
    monkeypatch.undo()
    assert republish.main([str(tmp_path)]) == 0 and _queue(tmp_path) == []
    assert _listed_items(tmp_path, a) == []                      # the drop, from authority


# --- acceptance 7: a crash between the authority write and publication -------------------------


def test_a_crash_after_the_commit_keeps_the_decision_and_republish_recovers(client, tmp_path,
                                                                            monkeypatch):
    a, nid = _setup(tmp_path)

    def crash(*args, **kwargs):
        raise RuntimeError("killed after the commit")

    monkeypatch.setattr(publication, "publish", crash)
    with pytest.raises(RuntimeError):
        client.post(f"/reviews/sources/{a}/links", json={"node_id": nid, "action": "keep"})
    monkeypatch.undo()
    assert _titles(_view(tmp_path, a)["kept"]) == ["Tag"]          # the decision is preserved
    (entry,) = _queue(tmp_path)                                    # the intent recorded first
    assert entry["provisional"] and entry["sources"] == [a] and entry["items"] == [nid]
    assert not _source_page_lists_tag(tmp_path, a)                # not published yet
    assert validate_publication.main([str(tmp_path)]) == 1

    assert republish.main([str(tmp_path)]) == 0
    assert _queue(tmp_path) == [] and _source_page_lists_tag(tmp_path, a)


def test_a_crash_inside_apply_republishes_everything_from_authority(tmp_path, monkeypatch):
    a, nid = _setup(tmp_path)
    monkeypatch.setattr(main_module, "_run_apply",
                        lambda st, intent=None: (_ for _ in ()).throw(RuntimeError("killed")))
    with pytest.raises(RuntimeError):
        main_module.run_apply(get_settings(tmp_path))
    (entry,) = _queue(tmp_path)
    assert entry["origin"] == "apply" and entry["full"]
    monkeypatch.undo()
    out = publication.publish(tmp_path)
    assert out["status"] == publication.PUBLISHED and a in out["sources_rendered"]
    assert _queue(tmp_path) == []


# --- acceptance 3: concurrent tag changes cannot publish stale pages --------------------------


def test_concurrent_keep_and_drop_publish_the_final_state(client, tmp_path, monkeypatch):
    a, nid = _setup(tmp_path)
    real = wiki.generate_wiki
    started = threading.Event()

    def slow(*args, **kwargs):
        started.set()
        time.sleep(0.3)                                  # the drop arrives during this render
        return real(*args, **kwargs)

    monkeypatch.setattr(wiki, "generate_wiki", slow)
    results: dict = {}
    t1 = threading.Thread(target=lambda: results.setdefault("keep", client.post(
        f"/reviews/sources/{a}/links", json={"node_id": nid, "action": "keep"})))
    t1.start()
    assert started.wait(10)
    t2 = threading.Thread(target=lambda: results.setdefault("drop", client.post(
        f"/reviews/sources/{a}/links", json={"node_id": nid, "action": "drop"})))
    t2.start()
    t1.join(30)
    t2.join(30)
    assert results["keep"].status_code == 200 and results["drop"].status_code == 200
    assert _titles(_view(tmp_path, a)["kept"]) == []
    assert _source_page_lists_tag(tmp_path, a)           # the page matches the final drop
    assert _queue(tmp_path) == []


# --- a damaged queue: fail-closed, then repaired without loss ---------------------------------


def test_a_damaged_queue_refuses_changes_keeps_reads_and_backup_and_repairs(client, tmp_path):
    a, nid = _setup(tmp_path)
    path = publication_queue.queue_path(tmp_path)
    path.write_text("{damaged", encoding="utf-8")

    resp = client.post(f"/reviews/sources/{a}/links", json={"node_id": nid, "action": "keep"})
    assert resp.status_code == 503 and "publication_queue_corrupt" in resp.json()["detail"]
    resp = client.post(f"/ui/reviews/sources/{a}/decide", data={f"link_{nid}": "keep"})
    assert resp.status_code == 503 and "republish.py --repair" in resp.text
    assert _titles(_view(tmp_path, a)["kept"]) == []             # nothing was written
    page = client.get(f"/ui/reviews/sources/{a}")                # reading still works
    assert page.status_code == 200 and "publication queue is unreadable" in page.text
    with write_lock.hold(tmp_path / "reviews", vault=False, authority=False):
        pass                                                     # backup's hold still works
    assert republish.main([str(tmp_path)]) == 4                  # retry refuses: repair first

    resp = client.post("/ui/publication/repair")
    assert resp.status_code == 200 and "damaged queue was kept" in resp.text
    damaged = list((tmp_path / "db").glob("publication_pending.damaged-*.json"))
    assert len(damaged) == 1 and damaged[0].read_text(encoding="utf-8") == "{damaged"
    assert _queue(tmp_path) == []
    resp = client.post(f"/reviews/sources/{a}/links", json={"node_id": nid, "action": "keep"})
    assert resp.status_code == 200 and _source_page_lists_tag(tmp_path, a)


def test_a_producer_script_refuses_on_a_damaged_queue(tmp_path):
    a, _nid = _setup(tmp_path)
    publication_queue.queue_path(tmp_path).write_text("{damaged", encoding="utf-8")
    from app.backend import run_guard
    code = run_guard.run_cli(tmp_path / "db", "generate_wiki", lambda: (
        wiki.generate_wiki(tmp_path, source_ids=[a], force=True, record_job=False,
                           rebuild_index=False), 0)[1])
    assert code == run_guard.EXIT_QUEUE_CORRUPT


def test_a_provisional_entry_widens_to_every_document_linking_its_tag(tmp_path):
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    _extract(tmp_path, {"alpha": [_item("Tag")], "beta": [_item("Tag")]})
    nid = items.node_id("Tag")
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        entry = {"sources": [sids["a.md"]], "items": [nid], "claims": [], "syntheses": [],
                 "full": False, "provisional": True}
        extracted = publication._documents(tmp_path)[0]
        # A crash before the exact scope was known: a title change would reach b's page too.
        assert publication._scope(entry, gconn, tmp_path / "wiki", extracted)["sources"] == {
            sids["a.md"], sids["b.md"]}
        exact = dict(entry, provisional=False)
        assert publication._scope(exact, gconn, tmp_path / "wiki", extracted)["sources"] == {
            sids["a.md"]}
    finally:
        gconn.close()


def test_an_apply_refused_before_its_first_write_leaves_no_intent(client, tmp_path):
    approved = tmp_path / "reviews" / "approved"
    approved.mkdir(parents=True)
    (approved / "rev_0000000000000001.json").write_text(
        json.dumps({"review_id": "rev_0000000000000001", "type": "merge_items"}), encoding="utf-8")
    assert client.post("/reviews/apply").status_code == 503          # graph-required, no graph
    assert _queue(tmp_path) == []


# --- review round: dependencies, full recovery, repair crash window, durable fields ------------

from app.workers import claims, identity_journal, synthesis  # noqa: E402
from app.workers.wiki_render import parse_frontmatter, render_item_page  # noqa: E402

_DURABLE = ("title", "aliases", "item_type", "status", "review_status", "confidence", "description",
            "split_from", "split_review_id")


def _search_current(tmp_path) -> bool:
    conn = keyword_index.connect_readonly(tmp_path / keyword_index.DB_RELPATH)
    try:
        return keyword_index.consistency_errors(tmp_path, conn) == []
    finally:
        conn.close()


def _claim(tmp_path, gconn, sid, cid, *, start=0):
    """An evidenced claim on `sid` (its normalized text), with its page."""
    md = (tmp_path / "normalized" / "markdown" / f"{sid}.md").read_text(encoding="utf-8")
    graph.upsert_node(gconn, node_id=cid, node_type="claim", slug=cid, status="active")
    graph.upsert_assertion(gconn, src_id=cid, dst_id=sid, edge_type="derived_from",
                           asserted_by="llm", status="active", evidence_source_id=sid,
                           evidence_char_start=start, evidence_char_end=len(md))
    claims.recompose_claim(gconn, cid=cid, claims_dir=tmp_path / "wiki" / "Claims",
                           reviews_dir=tmp_path / "reviews",
                           markdown_dir=tmp_path / "normalized" / "markdown", now="t",
                           text_hint=f"Claim {cid[-4:]} holds.")
    gconn.commit()


CX, CY = "clm_aaaaaaaaaaaaaaaa", "clm_bbbbbbbbbbbbbbbb"


def _claims_setup(tmp_path):
    """Source a, tag Tag, claims CX/CY on a (contradicting each other), a synthesis on Tag citing
    both."""
    a, nid = _setup(tmp_path)
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        _claim(tmp_path, gconn, a, CX)
        _claim(tmp_path, gconn, a, CY, start=2)
        graph.upsert_assertion(gconn, src_id=CX, dst_id=CY, edge_type="contradicts",
                               asserted_by="llm", status="active")
        syn = synthesis.synthesis_id(nid)
        graph.upsert_node(gconn, node_id=syn, node_type="synthesis", slug=syn, status="active")
        for cid in (CX, CY):
            graph.upsert_assertion(gconn, src_id=syn, dst_id=cid, edge_type="derived_from",
                                   asserted_by="llm", status="active")
        enrich = tmp_path / "normalized" / "enrichment"
        enrich.mkdir(parents=True, exist_ok=True)
        (enrich / f"{nid}.synthesis.json").write_text(json.dumps({
            "node_id": syn, "topic_node_id": nid, "title": "Tag overview", "summary": "About tags.",
            "synthesis": "Sources agree.", "confidence": 0.7, "input_fingerprint": "fp"}),
            encoding="utf-8")
        synthesis._render_page(gconn, syn_id=syn, topic_node=nid, title="Tag overview",
                               summary="About tags.", synthesis_text="Sources agree.",
                               confidence=0.7, status="active", review_status="approved",
                               synthesis_dir=tmp_path / "wiki" / "Synthesis", now="t")
        for cid in (CX, CY):                              # partner sections now resolve
            claims.recompose_claim(gconn, cid=cid, claims_dir=tmp_path / "wiki" / "Claims",
                                   reviews_dir=tmp_path / "reviews",
                                   markdown_dir=tmp_path / "normalized" / "markdown", now="t")
        gconn.commit()
    finally:
        gconn.close()
    wiki.generate_wiki(tmp_path, record_job=False, rebuild_index=False)
    keyword_index.reindex(tmp_path)
    return a, nid, syn


def _page(tmp_path, rel):
    return (tmp_path / "wiki" / rel).read_text(encoding="utf-8")


def test_a_failed_source_page_keeps_index_and_search_pending_until_its_retry(client, tmp_path,
                                                                            monkeypatch):
    a, nid = _setup(tmp_path)
    keyword_index.reindex(tmp_path)
    _failing_render(monkeypatch, {a})
    client.post(f"/reviews/sources/{a}/links", json={"node_id": nid, "action": "keep"})
    (entry,) = _queue(tmp_path)
    # Search ran against the stale page: it is not complete until the page is.
    assert entry["sources"] == [a] and entry["index"] and entry["keyword"]
    monkeypatch.undo()
    monkeypatch.setattr(main_module, "settings", get_settings(tmp_path))
    out = publication.publish(tmp_path)
    assert out["status"] == publication.PUBLISHED and out["keyword_reindexed"]
    assert _queue(tmp_path) == []
    assert _source_page_lists_tag(tmp_path, a) and _search_current(tmp_path)


def test_a_failed_claim_holds_dependent_work_of_other_entries(tmp_path, monkeypatch):
    a, _nid, syn = _claims_setup(tmp_path)
    with write_lock.hold(tmp_path / "reviews"):
        own = publication_queue.record(tmp_path, origin="apply", claims=[CX], index=False,
                                       keyword=False, provisional=False)
        other = publication_queue.record(tmp_path, origin="tag_action", sources=[a],
                                         provisional=False)
    real = claims.recompose_claim

    def broken(*args, **kwargs):
        if kwargs.get("project_only") and kwargs.get("cid") == CX:
            raise OSError("disk error")
        return real(*args, **kwargs)

    monkeypatch.setattr(claims, "recompose_claim", broken)
    out = publication.publish(tmp_path)
    assert out["status"] == publication.PENDING
    assert out["failed"]["claims"] == {CX: "render_failed:OSError"}
    entries = {e["id"]: e for e in _queue(tmp_path)}
    # The other entry's Source page and search ran against the stale claim: still pending.
    assert entries[other]["sources"] == [a] and entries[other]["index"] and entries[other]["keyword"]
    # The failure's owner keeps everything rendered from that claim, though it never asked for it.
    assert entries[own]["claims"] == [CX, CY] and entries[own]["syntheses"] == [syn]
    assert entries[own]["sources"] == [a] and entries[own]["keyword"]
    monkeypatch.undo()
    assert publication.publish(tmp_path)["status"] == publication.PUBLISHED
    assert _queue(tmp_path) == []


def test_a_crash_inside_apply_after_hiding_a_claim_is_fully_recovered(client, tmp_path, monkeypatch):
    a, _nid, syn = _claims_setup(tmp_path)
    # CY sorts after its partner: its saved visibility must reach CX before CX renders.
    assert f"Claims/{CY}" in _page(tmp_path, f"Claims/{CX}.md")
    approved = tmp_path / "reviews" / "approved"
    approved.mkdir(parents=True, exist_ok=True)
    (approved / "hc.json").write_text(json.dumps({
        "review_id": "hc", "type": "hide_claim", "status": "approved",
        "subject": {"node_id": CY, "page": f"Claims/{CY}.md"},
        "proposal": {"to_status": "hidden"}, "context": {"node_type": "claim"}}), encoding="utf-8")
    real = claims.recompose_claim

    def crash_after_the_hide(*args, **kwargs):
        if not kwargs.get("hide"):
            raise RuntimeError("killed before the fan-out")   # the partner refresh
        return real(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(claims, "recompose_claim", crash_after_the_hide)
        with pytest.raises(RuntimeError):
            client.post("/reviews/apply")
    assert parse_frontmatter(_page(tmp_path, f"Claims/{CY}.md"))["status"] == "hidden"
    assert f"Claims/{CY}" in _page(tmp_path, f"Claims/{CX}.md")      # partner not refreshed
    (entry,) = _queue(tmp_path)
    assert entry["origin"] == "apply" and entry["full"]
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:                                     # the graph mirror rolled back; the page is authority
        graph.upsert_node(gconn, node_id=CY, node_type="claim", slug=CY, status="active")
        gconn.commit()
    finally:
        gconn.close()

    assert republish.main([str(tmp_path)]) == 0 and _queue(tmp_path) == []
    assert f"Claims/{CY}" not in _page(tmp_path, f"Claims/{CX}.md")  # partner section
    assert parse_frontmatter(_page(tmp_path, f"Synthesis/{syn}.md"))["status"] == "evidence_hidden"
    assert f"Claims/{CY}" not in _page(tmp_path, f"Sources/{a}.md")  # Source Claims section
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        assert graph.get_node(gconn, CY)["status"] == "hidden"       # mirror from the page
    finally:
        gconn.close()
    assert _search_current(tmp_path)


def test_publication_files_no_review_and_retracts_nothing(tmp_path):
    """Projection only: a tombstoned claim re-renders without superseding its contradiction or
    filing a deprecation; a claim whose evidence vanished stays pending for its producer."""
    _a, _nid, _syn = _claims_setup(tmp_path)

    def without_evidence(cid, *, deprecate=False):
        gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
        try:
            if deprecate:                                # a human-approved deprecation, saved
                claims.recompose_claim(gconn, cid=cid, claims_dir=tmp_path / "wiki" / "Claims",
                                       reviews_dir=tmp_path / "reviews", now="t", deprecate=True,
                                       markdown_dir=tmp_path / "normalized" / "markdown")
            gconn.execute("UPDATE edges SET status = 'superseded' WHERE src_id = ? AND "
                          "edge_type = 'derived_from'", (cid,))
            gconn.commit()
        finally:
            gconn.close()

    def contradiction_kept():
        gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
        try:
            return graph.active_contradictions_for_claim(gconn, CX) == [CY]
        finally:
            gconn.close()

    without_evidence(CY, deprecate=True)
    reviews_before = sorted((tmp_path / "reviews").rglob("*.json"))
    assert publication.republish_all(tmp_path)["failed"]["claims"] == {}   # the tombstone renders
    assert sorted((tmp_path / "reviews").rglob("*.json")) == reviews_before
    assert contradiction_kept()
    without_evidence(CX)                                 # evidence gone, page still active
    assert publication.republish_all(tmp_path)["failed"]["claims"] == {CX: "evidence_gone"}
    assert sorted((tmp_path / "reviews").rglob("*.json")) == reviews_before
    assert contradiction_kept()


# --- full recovery: missing or unrenderable pages stay pending -----------------------------------


def test_a_missing_tag_page_stays_pending_until_restored(tmp_path):
    a, nid = _setup(tmp_path)
    page = tmp_path / "wiki" / "Items" / "tag.md"
    saved = page.read_text(encoding="utf-8")
    page.unlink()
    out = publication.republish_all(tmp_path)
    assert out["status"] == publication.PENDING
    assert out["failed"]["items"] == {nid: "page_missing"}
    from app.backend import review_html
    assert "restore the page from backup" in review_html.render_publication_result(out)
    (entry,) = _queue(tmp_path)
    assert entry["items"] == [nid] and a in entry["sources"]     # its Source page waits too
    assert not page.exists()                                     # metadata is never fabricated
    assert validate_publication.main([str(tmp_path)]) == 1
    page.write_text(saved, encoding="utf-8")                     # restored from backup
    assert publication.publish(tmp_path)["status"] == publication.PUBLISHED


def test_full_republish_finds_a_tag_page_whose_node_is_gone(tmp_path):
    a, nid = _setup(tmp_path)
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        graph.delete_node(gconn, nid)                  # the page stays; nothing records a deletion
        gconn.commit()
    finally:
        gconn.close()
    out = publication.republish_all(tmp_path)
    assert out["status"] == publication.PENDING
    assert out["failed"]["items"] == {nid: "node_missing"}
    assert (tmp_path / "wiki" / "Items" / "tag.md").exists()     # never inferred as a deletion


def test_queue_repair_finds_a_source_page_whose_manifest_is_invalid(tmp_path):
    a, _nid = _setup(tmp_path)
    (tmp_path / "raw" / "manifests" / f"{a}.json").write_text("{invalid", encoding="utf-8")
    publication_queue.queue_path(tmp_path).write_text("{damaged", encoding="utf-8")
    out = publication.repair(tmp_path)
    assert out["status"] == publication.PENDING
    assert out["failed"]["sources"] == {a: "manifest_unreadable"}
    assert _queue(tmp_path)[0]["sources"] == [a]


def test_a_missing_node_needs_evidence_of_deletion(tmp_path):
    _setup(tmp_path)
    ghost = "itm_00000000000000ff"
    with write_lock.hold(tmp_path / "reviews"):
        publication_queue.record(tmp_path, origin="tag_action", items=[ghost], provisional=False)
    assert publication.publish(tmp_path)["failed"]["items"] == {ghost: "node_missing"}
    audit = tmp_path / "reviews" / "audit_log"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / f"{ghost}-tag-deleted-0000abcd.json").write_text("{}", encoding="utf-8")
    assert publication.publish(tmp_path)["status"] == publication.PUBLISHED


def test_a_merged_redirect_page_is_preserved(tmp_path):
    _a, nid = _setup(tmp_path)
    page = tmp_path / "wiki" / "Items" / "tag.md"
    redirect = render_item_page({"node_id": nid, "item_type": "method_technique", "title": "Tag",
                                 "aliases": [], "status": "merged",
                                 "merged_into": "itm_0000000000000001"})
    page.write_text(redirect, encoding="utf-8")
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        graph.upsert_node(gconn, node_id=nid, node_type="item", slug="tag", status="merged",
                          item_type="method_technique")
        gconn.commit()
    finally:
        gconn.close()
    assert publication.republish_all(tmp_path)["failed"]["items"] == {}
    assert page.read_text(encoding="utf-8") == redirect


def test_an_unavailable_graph_keeps_everything_pending(tmp_path, monkeypatch):
    a, nid = _setup(tmp_path)
    with write_lock.hold(tmp_path / "reviews"):
        publication_queue.record(tmp_path, origin="tag_action", sources=[a], items=[nid])
    monkeypatch.setattr(publication, "_open_graph", lambda root: (None, True))
    out = publication.publish(tmp_path)
    assert out["status"] == publication.PENDING and "graph_unavailable" in out["warnings"]
    (entry,) = _queue(tmp_path)
    assert entry["items"] == [nid] and entry["sources"] == [a] and entry["provisional"]


def test_without_a_graph_tag_pages_stay_pending(tmp_path):
    with write_lock.hold(tmp_path / "reviews"):
        publication_queue.record(tmp_path, origin="tag_action", items=[items.node_id("Tag")])
    out = publication.publish(tmp_path)
    assert out["failed"]["items"] == {items.node_id("Tag"): "graph_missing"}
    assert out["status"] == publication.PENDING


# --- B: publication may move a tag between active and unused only ---------------------------


def _set_status(tmp_path, nid, status, **fields):
    """Put a tag's page and graph mirror at `status` with the given saved fields (a crash-left state)."""
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        node = graph.get_node(gconn, nid)
        meta = items._read_node_meta(tmp_path / "wiki" / "Items" / f"{node['slug']}.md")
        kept, suggested = graph.tag_projection(gconn, nid, status)
        review_status = fields.pop("review_status", None)
        (tmp_path / "wiki" / "Items" / f"{node['slug']}.md").write_text(render_item_page(
            {"node_id": nid, "item_type": meta["item_type"], "title": meta["title"],
             "aliases": meta["aliases"], "confidence": meta["confidence"], "source_ids": kept,
             "suggested_ids": suggested, "status": status, **fields},
            review_status=review_status), encoding="utf-8")
        graph.upsert_node(gconn, node_id=nid, node_type="item", slug=node["slug"], status=status,
                          item_type=meta["item_type"])
        gconn.commit()
    finally:
        gconn.close()


def _item_fm(tmp_path):
    return parse_frontmatter((tmp_path / "wiki" / "Items" / "tag.md").read_text(encoding="utf-8"))


@pytest.mark.parametrize("status", ["active", "candidate", "hidden"])
def test_republishing_preserves_every_declared_item_field(client, tmp_path, status):
    a, nid = _setup(tmp_path)
    if status != "candidate":
        client.post(f"/reviews/sources/{a}/links", json={"node_id": nid, "action": "keep"})
    _set_status(tmp_path, nid, status, review_status="approved", confidence="high",
                aliases=["Tags"], description="A hand-written description.",
                split_from="itm_0000000000000001", split_review_id="rev_0000000000000002")
    before = {k: _item_fm(tmp_path).get(k) for k in _DURABLE}
    reviews_before = sorted((tmp_path / "reviews").rglob("*.json"))
    assert publication.republish_all(tmp_path)["failed"]["items"] == {}
    assert {k: _item_fm(tmp_path).get(k) for k in _DURABLE} == before
    assert sorted((tmp_path / "reviews").rglob("*.json")) == reviews_before   # nothing filed


def test_republishing_moves_a_kept_tag_from_unused_to_active(client, tmp_path):
    a, nid = _setup(tmp_path)
    client.post(f"/reviews/sources/{a}/links", json={"node_id": nid, "action": "keep"})
    _set_status(tmp_path, nid, "unused", review_status="approved")   # a crash before the page
    assert publication.republish_all(tmp_path)["status"] == publication.PUBLISHED
    fm = _item_fm(tmp_path)
    assert fm["status"] == "active" and fm["review_status"] == "approved"


def test_republishing_never_promotes_a_kept_candidate(client, tmp_path):
    a, nid = _setup(tmp_path)
    client.post(f"/reviews/sources/{a}/links", json={"node_id": nid, "action": "keep"})
    _set_status(tmp_path, nid, "candidate")          # an interrupted first keep
    publication.republish_all(tmp_path)
    assert _item_fm(tmp_path)["status"] == "candidate"     # activation repair's job, not ours


def test_an_unfinished_identity_operation_keeps_its_tag_pending(client, tmp_path):
    a, nid = _setup(tmp_path)
    client.post(f"/reviews/sources/{a}/links", json={"node_id": nid, "action": "keep"})
    identity_journal.begin(tmp_path / "reviews", "rev_00000000000000aa", op="merge", nodes=[nid])
    out = publication.republish_all(tmp_path)
    assert out["failed"]["items"] == {nid: "identity_operation_in_flight"}
    assert nid in _queue(tmp_path)[0]["items"]


def test_a_damaged_ledger_or_a_disagreeing_graph_keeps_the_tag_pending(client, tmp_path):
    a, nid = _setup(tmp_path)
    client.post(f"/reviews/sources/{a}/links", json={"node_id": nid, "action": "keep"})
    ledger = tag_links.ledger_path(tmp_path / "reviews", a)
    saved = ledger.read_text(encoding="utf-8")
    ledger.write_text("{damaged", encoding="utf-8")
    assert publication.republish_all(tmp_path)["failed"]["items"] == {nid: "ledger_corrupt"}
    ledger.write_text(json.dumps({**json.loads(saved), "kept": {}}), encoding="utf-8")
    assert publication.publish(tmp_path)["failed"]["items"] == {nid: "tag_links_repair_needed"}


# --- the repair crash window and republish_all's own intent ------------------------------------


def test_a_crash_during_repair_never_unblocks_writes(client, tmp_path, monkeypatch):
    a, nid = _setup(tmp_path)
    publication_queue.queue_path(tmp_path).write_text("{damaged", encoding="utf-8")

    def crash(*args, **kwargs):
        raise RuntimeError("killed after preserving the damaged copy")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(publication_queue, "replace_with_recovery", crash)
        with pytest.raises(RuntimeError):
            publication.repair(tmp_path)
    assert len(list((tmp_path / "db").glob("publication_pending.damaged-*.json"))) == 1
    assert publication_queue.status(tmp_path)["state"] == "corrupt"     # still guarded
    resp = client.post(f"/reviews/sources/{a}/links", json={"node_id": nid, "action": "keep"})
    assert resp.status_code == 503

    with pytest.MonkeyPatch.context() as mp:                  # crash after the recovery intent
        mp.setattr(publication, "publish", crash)
        with pytest.raises(RuntimeError):
            publication.repair(tmp_path)
    (entry,) = _queue(tmp_path)
    assert entry["full"] and entry["origin"] == "queue_repair"
    assert publication.publish(tmp_path)["status"] == publication.PUBLISHED


def test_a_crash_inside_republish_all_leaves_its_full_intent(tmp_path, monkeypatch):
    _setup(tmp_path)

    def crash(*args, **kwargs):
        raise RuntimeError("killed after recording")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(publication, "publish", crash)
        with pytest.raises(RuntimeError):
            publication.republish_all(tmp_path)
    (entry,) = _queue(tmp_path)
    assert entry["full"] and entry["origin"] == "republish_all"
    assert validate_publication.main([str(tmp_path)]) == 1
    assert republish.main([str(tmp_path)]) == 0 and _queue(tmp_path) == []
