"""ADR-0068 slice 3: producers publish through the one publication function (decision 5).

Each bulk job commits per document under the write lock, publishes that document's pages before
releasing it, and refreshes `wiki/index.md` and the keyword index once at the end; it reports
success only after that refresh. Covers the demo-day gap (extraction left search stale), acceptance
tests 3 and 5 during a run (a keep made during a model call is published and survives the run's
later commits), and acceptance test 7 for a run (a crash anywhere leaves the work queued)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import republish
import validate_publication

from fastapi.testclient import TestClient

from app.backend import db, graph, keyword_index, publication_queue, write_lock
from app.backend import main as main_module
from app.backend.config import get_settings
from app.llm.cache import ResponseCache
from app.llm.client import LLMClient
from app.workers import claims, extract, intake, items, publication, tag_links, wiki, wiki_render
from tests import fixtures
from tests.test_claims import FakeAdapter as ClaimsAdapter
from tests.test_tag_links import MODEL_REF, RoutingAdapter, _item, _sids


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    settings.manifests_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


def _ingest(tmp_path, files: dict[str, str]):
    """A vault with templates (as every real vault has) and the files manifested, not extracted."""
    fixtures.copy_templates(tmp_path)
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (inbox / name).write_text(f"# Title\n\n{body}\n", encoding="utf-8")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")


def _extract_sources(tmp_path, **kw):
    return extract.extract_sources(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite", **kw)


def _search_current(tmp_path) -> bool:
    path = tmp_path / keyword_index.DB_RELPATH
    if not path.exists():
        return False
    conn = keyword_index.connect_readonly(path)
    try:
        return keyword_index.consistency_errors(tmp_path, conn) == []
    finally:
        conn.close()


def _hits(tmp_path, term: str) -> set[str]:
    conn = keyword_index.connect_readonly(tmp_path / keyword_index.DB_RELPATH)
    try:
        return {r[0] for r in conn.execute(
            "SELECT source_id FROM evidence WHERE evidence MATCH ?", (term,))}
    finally:
        conn.close()


def _lists_tags(tmp_path, sid) -> bool:
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        return not tag_links._source_page_drift(tmp_path / "wiki", gconn, sid)
    finally:
        gconn.close()


class _HookAdapter(RoutingAdapter):
    """Routes item payloads like `RoutingAdapter`, and runs `hook()` during the Nth model call
    (outside the write lock, as a real model call is)."""

    def __init__(self, routes, *, on_call: int, hook):
        super().__init__(routes)
        self.calls, self._on_call, self._hook = 0, on_call, hook

    def parse(self, messages, schema, model_id, *, max_tokens):
        self.calls += 1
        if self.calls == self._on_call:
            assert not write_lock.held()                   # the model call runs outside the lock
            self._hook()
        return super().parse(messages, schema, model_id, max_tokens=max_tokens)


def _extract_items(tmp_path, adapter, **kw):
    client = LLMClient({"anthropic": adapter}, cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite"))
    return items.extract_items(tmp_path, client=client, model_ref=MODEL_REF,
                               jobs_db=tmp_path / "db" / "jobs.sqlite", **kw)


# --- extraction: the demo-day gap -----------------------------------------------------------------


def test_extraction_publishes_source_pages_and_search_before_it_reports_success(tmp_path):
    _ingest(tmp_path, {"a.md": "alpha zebrafish body", "b.md": "bravo body"})
    summary = _extract_sources(tmp_path)
    a = _sids(tmp_path)["a.md"]
    assert summary["status"] == "succeeded" and summary["publication"]["status"] == "published"
    assert summary["keyword_reindexed"] is True
    assert (tmp_path / "wiki" / "Sources" / f"{a}.md").exists()     # no generate_wiki needed
    assert _search_current(tmp_path) and _hits(tmp_path, "zebrafish") == {a}
    assert publication_queue.load(tmp_path) == []
    assert validate_publication.main([str(tmp_path)]) == 0

    # A later run that adds searchable text refreshes search too (it left search stale on demo
    # day), and a forced re-extraction of unchanged text leaves it current.
    (tmp_path / "raw" / "inbox" / "c.md").write_text("# Title\n\ncharlie quokka body\n",
                                                    encoding="utf-8")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    _extract_sources(tmp_path)
    assert _search_current(tmp_path) and _hits(tmp_path, "quokka") == {_sids(tmp_path)["c.md"]}
    _extract_sources(tmp_path, force=True)
    assert _search_current(tmp_path) and _hits(tmp_path, "zebrafish") == {a}


def test_a_run_refreshes_the_index_and_search_once(tmp_path, monkeypatch):
    _ingest(tmp_path, {f"{n}.md": f"{n} has a full sentence of prose for extraction."
                       for n in ("alpha", "bravo", "charlie")})
    calls = {"index": 0, "keyword": 0}
    real = keyword_index.reindex

    def count_keyword(*args, **kwargs):
        calls["keyword"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(keyword_index, "reindex", count_keyword)
    monkeypatch.setattr(publication, "_rebuild_index_status",
                        lambda root: calls.__setitem__("index", calls["index"] + 1) or "rebuilt")
    summary = _extract_sources(tmp_path)
    assert summary["extracted"] == 3 and summary["status"] == "succeeded"
    assert calls == {"index": 1, "keyword": 1}                     # not once per document


def test_a_failed_end_refresh_is_saved_publication_pending_and_republish_clears_it(tmp_path,
                                                                                   monkeypatch):
    _ingest(tmp_path, {"a.md": "alpha"})
    real = keyword_index.reindex

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(keyword_index, "reindex", broken)
    summary = _extract_sources(tmp_path)
    a = _sids(tmp_path)["a.md"]
    assert summary["status"] == publication.PENDING                 # never "succeeded"
    assert (tmp_path / "wiki" / "Sources" / f"{a}.md").exists()     # the document is saved
    (entry,) = publication_queue.load(tmp_path)
    assert entry["origin"] == "run_extract" and entry["keyword"]
    conn = db.connect(tmp_path / "db" / "jobs.sqlite")
    try:
        assert db.get_job(conn, summary["job_id"])["status"] == publication.PENDING
    finally:
        conn.close()
    monkeypatch.setattr(keyword_index, "reindex", real)
    assert republish.main([str(tmp_path)]) == 0
    assert publication_queue.load(tmp_path) == [] and _search_current(tmp_path)


def test_a_crash_mid_run_leaves_the_end_refresh_queued(tmp_path, monkeypatch):
    _ingest(tmp_path, {"a.md": "alpha", "b.md": "bravo"})
    real = extract._extract_one
    seen = []

    def crash_on_second(manifest, *args, **kwargs):
        seen.append(manifest["source_id"])
        if len(seen) == 2:
            raise KeyboardInterrupt                               # the process stops here
        return real(manifest, *args, **kwargs)

    monkeypatch.setattr(extract, "_extract_one", crash_on_second)
    with pytest.raises(KeyboardInterrupt):
        _extract_sources(tmp_path)
    first = seen[0]
    assert (tmp_path / "wiki" / "Sources" / f"{first}.md").exists()   # published in its commit
    (entry,) = publication_queue.load(tmp_path)                       # only the end refresh is left
    assert entry["origin"] == "run_extract" and entry["index"] and entry["keyword"]
    assert not _search_current(tmp_path)
    assert validate_publication.main([str(tmp_path)]) == 1
    assert republish.main([str(tmp_path)]) == 0
    assert _search_current(tmp_path) and _hits(tmp_path, "alpha" if first == _sids(tmp_path)["a.md"]
                                                else "bravo") == {first}


# --- items: per-document publication during a run -------------------------------------------------


def test_items_extraction_publishes_each_document_before_the_next_model_call(client, tmp_path):
    _ingest(tmp_path, {"a.md": "alpha body", "b.md": "bravo body"})
    _extract_sources(tmp_path)
    keyword_index.reindex(tmp_path)
    observed = {}

    def during_second_call():
        names = {v: k for k, v in _sids(tmp_path).items()}
        first = next(s for s in names
                     if (tmp_path / "normalized" / "enrichment" / f"{s}.items.json").exists())
        page = (tmp_path / "wiki" / "Sources" / f"{first}.md").read_text(encoding="utf-8")
        observed["first_listed"] = ("Alpha Tag" if names[first] == "a.md" else "Bravo Tag") in page
        observed["queue"] = publication_queue.load(tmp_path)
        observed["status"] = publication_queue.status(tmp_path)
        observed["validate"] = validate_publication.main([str(tmp_path)])
        observed["banner"] = client.get("/ui/reviews").text

    adapter = _HookAdapter({"alpha": [_item("Alpha Tag")], "bravo": [_item("Bravo Tag")]},
                           on_call=2, hook=during_second_call)
    summary = _extract_items(tmp_path, adapter)
    assert summary["status"] == "succeeded" and summary["publication"]["status"] == "published"
    # During the run: the first document's Source page already lists its suggestion, and only the
    # run's own end refresh is pending — shown as "search refresh pending", not as a failure.
    assert observed["first_listed"] is True
    (entry,) = observed["queue"]
    assert entry["origin"] == "run_extract_items" and entry["index"] and entry["keyword"]
    assert observed["status"]["only_runs"] and observed["status"]["runs"] == ["extract_items"]
    assert observed["validate"] == 1
    assert "Search refresh pending" in observed["banner"]
    assert "Saved; publication incomplete" not in observed["banner"]
    # After it: every page and search are current, with no generate_wiki or reindex step.
    sids = _sids(tmp_path)
    assert all(_lists_tags(tmp_path, s) for s in sids.values())
    assert _search_current(tmp_path) and publication_queue.load(tmp_path) == []
    assert "Search refresh pending" not in client.get("/ui/reviews").text


def test_a_keep_during_a_runs_model_call_is_published_and_survives_the_run(client, tmp_path):
    _ingest(tmp_path, {"a.md": "alpha body", "b.md": "bravo body"})
    _extract_sources(tmp_path)
    a, b = _sids(tmp_path)["a.md"], _sids(tmp_path)["b.md"]
    shared = items.node_id("Shared Tag")
    routes = {"alpha": [_item("Shared Tag")], "bravo": [_item("Shared Tag")]}
    _extract_items(tmp_path, RoutingAdapter(routes), source_ids=[a])
    kept = {}

    def keep_on_a():
        # Acceptance 5: tagging is available during the run's model call (no lock held), and
        # acceptance 4: the keep publishes a's Source page before it returns.
        resp = client.post(f"/reviews/sources/{a}/links", json={"node_id": shared, "action": "keep"})
        kept["status"] = resp.status_code
        kept["publication"] = resp.json()["publication"]["status"]

    _extract_items(tmp_path, _HookAdapter(routes, on_call=1, hook=keep_on_a), source_ids=[b],
                   force=True)
    assert kept == {"status": 200, "publication": "published"}
    # Acceptance 3: b's later commit and the run's end refresh render from current authority, so
    # the keep is neither lost nor shown stale anywhere.
    assert list(tag_links.load_ledger(tmp_path / "reviews", a)["kept"]) == [shared]
    page = wiki_render.parse_frontmatter(
        (tmp_path / "wiki" / "Items" / "shared-tag.md").read_text(encoding="utf-8"))
    assert page["status"] == "active"
    assert _lists_tags(tmp_path, a) and _lists_tags(tmp_path, b)
    assert _search_current(tmp_path) and publication_queue.load(tmp_path) == []


# --- claims: a crash inside a document's commit ----------------------------------------------------


def _claims_vault(tmp_path):
    fixtures.copy_templates(tmp_path)
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.write_markdown(inbox / "doc.md")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    _extract_sources(tmp_path)
    return _sids(tmp_path)["doc.md"]


def _extract_claims(tmp_path, adapter):
    client = LLMClient({"anthropic": adapter}, cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite"))
    return claims.extract_claims(tmp_path, client=client, model_ref=MODEL_REF,
                                 jobs_db=tmp_path / "db" / "jobs.sqlite")


def test_claims_publish_per_document_and_a_crash_in_the_commit_is_recovered(tmp_path, monkeypatch):
    sid = _claims_vault(tmp_path)
    real = claims._write_source_artifact

    def crash(*args, **kwargs):
        raise KeyboardInterrupt                                   # after the pages, before the artifact

    monkeypatch.setattr(claims, "_write_source_artifact", crash)
    with pytest.raises(KeyboardInterrupt):
        _extract_claims(tmp_path, ClaimsAdapter())
    entries = publication_queue.load(tmp_path)
    assert {e["origin"] for e in entries} == {"run_extract_claims"}
    (doc,) = [e for e in entries if e["sources"]]                    # the document's own intent
    assert doc["sources"] == [sid] and doc["provisional"]
    assert republish.main([str(tmp_path)]) == 0                       # widened to its claims
    assert publication_queue.load(tmp_path) == [] and _search_current(tmp_path)
    source_page = (tmp_path / "wiki" / "Sources" / f"{sid}.md").read_text(encoding="utf-8")
    assert "[[Claims/clm_" in source_page

    # The artifact was never written, so the next run redoes the document (not "fresh").
    monkeypatch.setattr(claims, "_write_source_artifact", real)
    summary = _extract_claims(tmp_path, ClaimsAdapter())
    assert summary["skipped_fresh"] == 0 and summary["claims_written"] == 2
    assert summary["status"] == "succeeded" and summary["publication"]["status"] == "published"


def test_a_provisional_document_intent_widens_to_its_tags(tmp_path):
    # A crash recovery publishes what the document names, so a damaged tag page is reported, never
    # skipped: the intent knew only the document, not the tags its commit wrote.
    _ingest(tmp_path, {"a.md": "alpha body"})
    _extract_sources(tmp_path)
    a = _sids(tmp_path)["a.md"]
    _extract_items(tmp_path, RoutingAdapter({"alpha": [_item("Alpha Tag")]}))
    nid = items.node_id("Alpha Tag")
    (tmp_path / "wiki" / "Items" / "alpha-tag.md").unlink()
    with write_lock.hold(tmp_path / "reviews"):
        publication_queue.record(tmp_path, origin="run_extract_items", sources=[a])
    out = publication.publish(tmp_path)
    assert out["status"] == publication.PENDING
    assert out["failed"]["items"] == {nid: "page_missing"}
    assert a in out["failed"]["sources"] or a in publication_queue.outstanding(
        publication_queue.load(tmp_path))["sources"]               # held by the missing tag page


# --- generate_wiki and the reindex job ---------------------------------------------------------------


def test_generate_wiki_retries_a_failed_page_once_and_leaves_it_pending(tmp_path, monkeypatch):
    _ingest(tmp_path, {"a.md": "alpha", "b.md": "bravo"})
    _extract_sources(tmp_path)
    a = _sids(tmp_path)["a.md"]
    real = wiki_render.render_source_page
    tries = []

    def fail_a(template, manifest, *args, **kwargs):
        if manifest["source_id"] == a:
            tries.append(a)
            raise ValueError("bad page")
        return real(template, manifest, *args, **kwargs)

    monkeypatch.setattr(wiki_render, "render_source_page", fail_a)
    summary = wiki.generate_wiki(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite", force=True)
    assert len(tries) == 2                                         # the run, then its one retry
    assert summary["status"] == "partial" and summary["errors"] == 1     # not a success
    assert summary["publication"]["pending"]["sources"] == [a]
    assert any(a in line for line in summary["publication"]["still_pending"])
    monkeypatch.setattr(wiki_render, "render_source_page", real)
    assert republish.main([str(tmp_path)]) == 0
    assert publication_queue.load(tmp_path) == []


def test_job_summaries_are_json_and_name_the_publication(tmp_path):
    _ingest(tmp_path, {"a.md": "alpha"})
    summary = _extract_sources(tmp_path)
    json.dumps(summary)                                           # stored as job metadata
    conn = db.connect(tmp_path / "db" / "jobs.sqlite")
    try:
        meta = db.get_job(conn, summary["job_id"])["metadata"]
    finally:
        conn.close()
    assert meta["publication"]["status"] == "published"


# --- contradictions, synthesis and promotion runs --------------------------------------------------


def test_contradiction_synthesis_and_promotion_runs_end_with_current_pages_and_search(tmp_path):
    import tests.test_contradictions as con_t
    import tests.test_synthesis as syn_t
    from app.workers import contradictions, reviews, synthesis

    fixtures.copy_templates(tmp_path)
    jobs = syn_t._build(tmp_path)
    run = contradictions.detect_contradictions(
        tmp_path, client=con_t._client(tmp_path, con_t.ContradictionAdapter(verdict=True)),
        model_ref=con_t.HEAVY, jobs_db=jobs)
    assert run["publication"]["status"] == "published"
    rid = next(json.loads(p.read_text())["review_id"]
               for p in (tmp_path / "reviews" / "pending").glob("*.json")
               if json.loads(p.read_text())["type"] == "resolve_contradiction")
    reviews.resolve_review_item(tmp_path / "reviews", rid, decision="approved", decided_by="human")
    run = contradictions.detect_contradictions(
        tmp_path, client=con_t._client(tmp_path, con_t.ContradictionAdapter(verdict=True)),
        model_ref=con_t.HEAVY, jobs_db=jobs)
    assert run["claim_pages_reprojected"] == 2 and run["keyword_reindexed"] is True
    assert run["status"] == "succeeded" and publication_queue.load(tmp_path) == []

    run = synthesis.generate_syntheses(
        tmp_path, client=syn_t._client(tmp_path, syn_t.SynthAdapter()), model_ref=syn_t.HEAVY,
        jobs_db=jobs)
    assert run["syntheses_written"] == 1 and run["keyword_reindexed"] is True
    assert run["status"] == "succeeded" and publication_queue.load(tmp_path) == []
    assert _search_current(tmp_path)


def test_a_standalone_promotion_run_publishes_and_inside_apply_it_is_a_step(tmp_path, monkeypatch):
    import tests.test_promote as pro_t
    from app.workers import promote

    fixtures.copy_templates(tmp_path)
    sids = pro_t._setup(tmp_path, pro_t._TWO)
    keyword_index.reindex(tmp_path)
    pro_t._approve(tmp_path)
    run = promote.promote_candidates(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    assert run["promoted"] == 1 and run["status"] == "succeeded"
    assert run["publication"]["status"] == "published" and run["keyword_reindexed"] is True
    assert publication_queue.load(tmp_path) == [] and _search_current(tmp_path)
    assert all(_lists_tags(tmp_path, s) for s in sids.values())

    # As Apply's step (rebuild_index=False) it records and publishes nothing of its own.
    recorded = []
    monkeypatch.setattr(publication_queue, "record", lambda *a, **k: recorded.append(k) or "pub_x")
    assert promote.promote_candidates(tmp_path, rebuild_index=False,
                                      record_job=False)["publication"] is None
    assert recorded == []


# --- alias accumulation on never-kept candidates ----------------------------------------------------

_ALIAS_DOCS = {"a.md": "alpha body about a large language model and a foundation model.",
               "b.md": "bravo body about a Large Language Model and a language model."}
_ALIAS_ROUTES = {
    "alpha": [{"name": "LLM", "item_type": "method_technique",
               "aliases": ["large language model", "foundation model"]}],
    "bravo": [{"name": "LLM", "item_type": "method_technique",
               "aliases": ["Large Language Model", "language model"]}]}


def _llm_aliases(root) -> list[str]:
    return wiki_render.parse_frontmatter(
        (root / "wiki" / "Items" / "llm.md").read_text(encoding="utf-8"))["aliases"]


def _order(root) -> list[str]:
    from app.backend.manifests import valid_manifests
    return [m["source_id"] for m in valid_manifests(root / "raw" / "manifests")[0]]


def test_batch_and_per_document_runs_accumulate_the_same_aliases(tmp_path):
    batch, single = tmp_path / "batch", tmp_path / "single"
    for root in (batch, single):
        _ingest(root, _ALIAS_DOCS)
        _extract_sources(root)
    _extract_items(batch, RoutingAdapter(_ALIAS_ROUTES))
    for sid in _order(single):                     # the same document order, one run each
        _extract_items(single, RoutingAdapter(_ALIAS_ROUTES), source_ids=[sid])
    assert _llm_aliases(batch) == _llm_aliases(single)
    # Every document's aliases accumulate (the old end-of-run pass kept only the last document's).
    first = "alpha" if _order(batch)[0] == _sids(batch)["a.md"] else "bravo"
    assert _llm_aliases(batch)[:2] == _ALIAS_ROUTES[first][0]["aliases"]


def test_case_variants_of_an_alias_take_one_slot_with_the_stored_spelling(tmp_path):
    _ingest(tmp_path, _ALIAS_DOCS)
    _extract_sources(tmp_path)
    a, b = _sids(tmp_path)["a.md"], _sids(tmp_path)["b.md"]
    _extract_items(tmp_path, RoutingAdapter(_ALIAS_ROUTES), source_ids=[a])
    _extract_items(tmp_path, RoutingAdapter(_ALIAS_ROUTES), source_ids=[b])
    assert _llm_aliases(tmp_path) == ["large language model", "foundation model", "language model"]
    # The limit is unchanged, and a full list takes nothing new.
    full = [f"alias {i}" for i in range(items._MAX_ALIASES)]
    assert items._accumulate(full, ["another"]) == full
    assert items._accumulate(["A b"], ["a B", "c"]) == ["A b", "c"]
    # Human alias edits keep the exact comparison (and their stale-form check with it).
    assert items._union(["a b"], ["A B"]) == ["a b", "A B"]


def test_re_extraction_leaves_a_kept_tags_aliases_unchanged(tmp_path):
    from tests import tagging
    _ingest(tmp_path, _ALIAS_DOCS)
    _extract_sources(tmp_path)
    a = _sids(tmp_path)["a.md"]
    _extract_items(tmp_path, RoutingAdapter(_ALIAS_ROUTES), source_ids=[a])
    tagging.keep_all(tmp_path, source_ids={a})
    before = _llm_aliases(tmp_path)
    _extract_items(tmp_path, RoutingAdapter(_ALIAS_ROUTES), force=True)   # both documents again
    assert _llm_aliases(tmp_path) == before == ["large language model", "foundation model"]


# --- Apply: an approved promotion publishes every Source page listing the tag ------------------------


def test_apply_promotion_without_a_slug_change_publishes_listing_pages_and_retries(client, tmp_path):
    import tests.test_promote as pro_t
    (tmp_path / "raw" / "inbox").rmdir()                 # the fixture made it; _setup makes it again
    fixtures.copy_templates(tmp_path)
    sids = pro_t._setup(tmp_path, pro_t._TWO)
    keyword_index.reindex(tmp_path)
    pro_t._approve(tmp_path)                             # no amendments: the slug stays
    page = tmp_path / "wiki" / "Items" / f"{items._slug(pro_t.NAME)}.md"
    assert page.exists()
    broken = {sids["a.md"]}
    real = wiki.generate_wiki

    def render(root, *, source_ids=None, **kw):          # a's Source page fails while `broken`
        ok = [s for s in source_ids or [] if s not in broken]
        out = real(root, source_ids=ok, **kw) if ok else {"error_details": []}
        out["error_details"] = list(out.get("error_details") or []) + [
            {"source_id": s} for s in source_ids or [] if s in broken]
        return out

    import unittest.mock
    with unittest.mock.patch.object(wiki, "generate_wiki", render):
        body = fixtures.apply(client).json()
    assert body["summary"]["promotions"]["promoted"] == 1 and page.exists()
    assert body["status"] == publication.PENDING                # never "applied" with stale pages
    assert body["publication"]["pending"]["sources"] == [sids["a.md"]]
    assert not _lists_tags(tmp_path, sids["a.md"]) and _lists_tags(tmp_path, sids["b.md"])
    resp = client.post("/publication/retry")
    assert resp.status_code == 200, resp.text
    assert all(_lists_tags(tmp_path, s) for s in sids.values())
    assert publication_queue.load(tmp_path) == [] and _search_current(tmp_path)


def test_an_interrupted_first_keep_still_owns_the_aliases(tmp_path):
    # A keep that stopped after its ledger write leaves the tag a candidate in the graph; the ledger
    # keep already makes the alias list the user's, so re-extraction adds nothing to it.
    from tests import tagging
    _ingest(tmp_path, _ALIAS_DOCS)
    _extract_sources(tmp_path)
    a = _sids(tmp_path)["a.md"]
    _extract_items(tmp_path, RoutingAdapter(_ALIAS_ROUTES), source_ids=[a])
    tagging.keep_all(tmp_path, source_ids={a})
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        gconn.execute("UPDATE nodes SET status = 'candidate' WHERE node_id = ?",
                      (items.node_id("LLM"),))
        gconn.commit()
    finally:
        gconn.close()
    before = _llm_aliases(tmp_path)
    _extract_items(tmp_path, RoutingAdapter(_ALIAS_ROUTES), source_ids=[_sids(tmp_path)["b.md"]])
    assert _llm_aliases(tmp_path) == before
