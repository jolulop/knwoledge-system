"""ADR-0074 decision 8, slice 5b: consistent consumer reads of the normalized layer.

A consumer captures a document's install-journal state and the normalized bytes it needs under the
write lock (model calls stay outside it), and re-checks at its commit. A document whose journal is
`installing`, `unrecoverable` or damaged is skipped with `extraction_install_incomplete` by every
consumer — enrich, claims, items, synthesis inputs, keyword and vector reindex — and is excluded
from search and `/query` evidence; the document screen names it. Reads racing an install wait for
it and see one complete generation; a commit racing a crashed install writes nothing.
"""
from __future__ import annotations

import json
import sys
import threading
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tests.test_contradictions as con_t  # noqa: E402
import tests.test_synthesis as syn_t  # noqa: E402
from app.backend import durable_write, graph, keyword_index, manifests, review_read, search  # noqa: E402
from app.backend import extraction_install as xi  # noqa: E402
from app.backend import main as main_module  # noqa: E402
from app.backend import review_html  # noqa: E402
from app.backend.config import get_settings  # noqa: E402
from app.backend.policy import RetrievalPolicy  # noqa: E402
from app.llm.cache import ResponseCache  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.workers import claims, contradiction_check, contradictions, enrich, extract, intake  # noqa: E402
from app.workers import items, query  # noqa: E402
from app.workers.chunking import Element  # noqa: E402
from app.workers.extractors import Extraction  # noqa: E402
from tests import fixtures  # noqa: E402

GEN_A = {"text": "Generation A has one paragraph of prose text.", "tables": [("0.csv", "a\n1\n")]}
GEN_B = {"text": "Generation B replaces it with a different, longer paragraph.",
         "tables": [("0.csv", "a\n2\n")]}
STATES = ("installing", "unrecoverable", "corrupt")
MODEL = "anthropic:claude-sonnet-4-6"


class Crash(BaseException):
    """A process death mid-install (never caught as an ordinary error)."""


@pytest.fixture
def vault(tmp_path, monkeypatch):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.copy_templates(tmp_path)
    fixtures.write_markdown(inbox / "doc.md")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    gen = {"current": GEN_A}
    monkeypatch.setattr(extract, "_parse", lambda path, sid, **kw: Extraction(
        elements=[Element("prose", gen["current"]["text"])], tool="fake", tool_version="1",
        tables=list(gen["current"]["tables"])))
    _run(tmp_path)
    return tmp_path, gen


def _run(root: Path, **kw) -> dict:
    return extract.extract_sources(root, jobs_db=root / "db" / "jobs.sqlite", **kw)


def _sid(root: Path) -> str:
    return manifests.scan_manifests(root / "raw" / "manifests").valid[0]["source_id"]


def _layout(root: Path) -> xi.Layout:
    return xi.Layout(root / "normalized")


def _crash_mid_install(root: Path, gen: dict) -> None:
    """Re-extract to generation B and die right after the Markdown moved: the journal stays
    `installing`, the Markdown is B's and the chunks are still A's — a mixed generation."""
    real = durable_write.move

    def move(src, dst):
        real(src, dst)
        if Path(dst).suffix == ".md":
            raise Crash("after the markdown rename")
    with pytest.MonkeyPatch.context() as m:
        m.setattr(durable_write, "move", move)
        gen["current"] = GEN_B
        with pytest.raises(Crash):
            _run(root, force=True)


def _block(root: Path, gen: dict, state: str) -> str:
    sid = _sid(root)
    _crash_mid_install(root, gen)
    layout = _layout(root)
    read = xi.read_journal(layout, sid)
    assert read.state == xi.INSTALLING
    if state == "unrecoverable":
        xi.mark_unrecoverable(layout, read.entry, "staged_file_missing", manifests.iso_now())
    elif state == "corrupt":
        layout.journal(sid).write_bytes(b"{not json")
    assert xi.read_journal(layout, sid).state == state
    out = layout.outputs(sid)
    assert "Generation B" in out["markdown"].read_text() and "Generation A" in out["chunks"].read_text()
    return sid


@pytest.fixture(params=STATES)
def blocked(vault, request):
    root, gen = vault
    return root, gen, _block(root, gen, request.param), request.param


# --- fake models -----------------------------------------------------------------------------


class _Adapter:
    name = "anthropic"
    supports_batch = False

    def __init__(self, response, during=None):
        self.calls = 0
        self._response = response
        self._during = during           # runs inside the model call (outside the write lock)

    def available(self):
        return True

    def parse(self, messages, schema, model_id, *, max_tokens):
        self.calls += 1
        if self._during is not None:
            self._during()
        return json.loads(json.dumps(self._response))


ENRICH = {"title": "", "authors": [], "publisher": "", "summary": "A faithful summary.",
          "tags": [{"name": "alpha", "item_type": "domain"}]}
CLAIMS = {"claims": [{"claim": "Generation A has prose.", "quote": GEN_A["text"]}]}
ITEMS = {"items": [{"name": "Prose", "item_type": "method_technique", "aliases": []}]}


def _client(root: Path, adapter: _Adapter) -> LLMClient:
    return LLMClient({"anthropic": adapter}, cache=ResponseCache(root / "db" / "llm_cache.sqlite"))


def _enrich(root, adapter):
    return enrich.enrich_sources(root, client=_client(root, adapter), model_ref=MODEL,
                                 jobs_db=root / "db" / "jobs.sqlite")


def _claims(root, adapter):
    return claims.extract_claims(root, client=_client(root, adapter), model_ref=MODEL,
                                 jobs_db=root / "db" / "jobs.sqlite", rebuild_index=False)


def _items(root, adapter):
    return items.extract_items(root, client=_client(root, adapter), model_ref=MODEL,
                               jobs_db=root / "db" / "jobs.sqlite", rebuild_index=False)


_PRODUCERS = {"enrich": (_enrich, ENRICH), "claims": (_claims, CLAIMS), "items": (_items, ITEMS)}


def _artifact(root: Path, producer: str, sid: str) -> Path:
    suffix = {"enrich": "", "claims": ".claims", "items": ".items"}[producer]
    return root / "normalized" / "enrichment" / f"{sid}{suffix}.json"


# --- every consumer skips a document with no usable extraction ----------------------------------


@pytest.mark.parametrize("producer", sorted(_PRODUCERS))
def test_a_producer_skips_a_blocked_document_without_a_model_call(blocked, producer):
    root, _gen, sid, _state = blocked
    run, response = _PRODUCERS[producer]
    adapter = _Adapter(response)
    summary = run(root, adapter)
    assert adapter.calls == 0
    assert summary["install_incomplete"] == [sid] and summary["skipped_install_incomplete"] == 1
    assert summary["status"] == "partial"
    assert not _artifact(root, producer, sid).exists()


@pytest.mark.parametrize("producer", sorted(_PRODUCERS))
def test_once_extraction_completes_the_document_the_producer_reads_it_again(vault, producer):
    root, gen = vault
    sid = _block(root, gen, "installing")
    _run(root)                                      # recovery rolls the install forward
    assert xi.read_journal(_layout(root), sid).state == xi.ABSENT
    run, response = _PRODUCERS[producer]
    adapter = _Adapter(response)
    summary = run(root, adapter)
    assert adapter.calls == 1 and summary["install_incomplete"] == []
    assert _artifact(root, producer, sid).exists()


def _rows(root: Path, sid: str) -> list[str]:
    conn = keyword_index.connect(root / keyword_index.DB_RELPATH)
    try:
        return [r[0] for r in conn.execute(
            "SELECT text FROM evidence WHERE source_id = ? ORDER BY ordinal", (sid,))]
    finally:
        conn.close()


def test_the_keyword_reindex_keeps_a_blocked_documents_rows_and_reports_it(blocked):
    root, _gen, sid, _state = blocked
    before = _rows(root, sid)
    assert before and all("Generation A" in t for t in before)
    stats = keyword_index.reindex(root)
    assert stats.install_incomplete == [sid] and stats.evidence_sources_indexed == 0
    assert _rows(root, sid) == before
    (_layout(root).outputs(sid)["chunks"]).unlink()         # not even a vanished file removes them
    assert keyword_index.reindex(root).evidence_sources_removed == 0
    assert _rows(root, sid) == before
    conn = keyword_index.connect(root / keyword_index.DB_RELPATH)
    try:
        errors = keyword_index.consistency_errors(root, conn)
    finally:
        conn.close()
    assert errors == [f"evidence {sid}: extraction install incomplete (run extraction)"]


def test_the_keyword_reindex_indexes_the_document_once_extraction_completes_it(vault):
    root, gen = vault
    sid = _block(root, gen, "installing")
    _run(root)
    keyword_index.reindex(root)
    assert _rows(root, sid) and all("Generation B" in t for t in _rows(root, sid))


def test_search_excludes_a_blocked_document(blocked):
    root, _gen, sid, _state = blocked
    kconn = keyword_index.connect(root / keyword_index.DB_RELPATH)
    try:
        res = search.run_search(q="Generation", mode="keyword", manifests_dir=root / "raw" / "manifests",
                                keyword_conn=kconn, graph_conn=None, policy=RetrievalPolicy(),
                                source_statuses=("active", "unknown"))
    finally:
        kconn.close()
    assert res["evidence"] == []
    assert any("extraction install incomplete" in n for n in res["notes"])


def test_search_admits_the_document_while_its_journal_is_usable(vault):
    root, _gen = vault
    kconn = keyword_index.connect(root / keyword_index.DB_RELPATH)
    try:
        res = search.run_search(q="Generation", mode="keyword", manifests_dir=root / "raw" / "manifests",
                                keyword_conn=kconn, graph_conn=None, policy=RetrievalPolicy(),
                                source_statuses=("active", "unknown"))
    finally:
        kconn.close()
    assert {h["source_id"] for h in res["evidence"]} == {_sid(root)}


def test_a_query_never_cites_a_blocked_document(blocked):
    root, _gen, sid, _state = blocked
    md = _layout(root).outputs(sid)["markdown"].read_text()
    hit = {"source_id": sid, "char_start": 0, "char_end": 10}
    assert query.build_evidence_pack([hit], root / "normalized" / "markdown", {}) == []
    (root / "normalized" / ".install" / f"{sid}.json").unlink()       # usable again
    assert [e["quote"] for e in query.build_evidence_pack(
        [hit], root / "normalized" / "markdown", {})] == [md[:10]]


def test_the_api_refuses_to_serve_a_blocked_documents_outputs(blocked, monkeypatch):
    root, _gen, sid, state = blocked
    monkeypatch.setattr(main_module, "settings", get_settings(root))
    client = TestClient(main_module.app)
    for kind in ("normalized", "chunks"):
        res = client.get(f"/sources/{sid}/{kind}")
        assert res.status_code == 409
        assert res.json()["detail"]["reason"] == xi.INCOMPLETE
        assert res.json()["detail"]["journal_state"] == state


def test_the_document_screen_names_an_incomplete_install(blocked):
    root, _gen, sid, state = blocked
    install = review_read.extraction_install_state(root / "reviews", sid)
    assert install["state"] == state
    banner = review_html._install_banner(install)
    assert "No usable extraction (interrupted install)" in banner
    assert ("Restore the journal" in banner) == (state == "corrupt")
    assert review_read.extraction_install_state(root / "reviews", "src_0000000000000000") is None


def test_the_details_form_derives_nothing_from_a_blocked_extraction(vault):
    root, _gen = vault
    sid = _sid(root)
    _enrich(root, _Adapter({**ENRICH, "title": "An Enrich Title"}))

    def shown():
        return review_read.details_section(
            root / "reviews", sid, manifests_dir=root / "raw" / "manifests",
            enrichment_dir=root / "normalized" / "enrichment")["details"]
    assert shown()["suggested"]["title"] == "An Enrich Title"       # fresh for the same bytes
    _corrupt_journal(root, sid)
    assert shown()["suggested"] is None and shown()["source"] == "filename"


# --- reads racing an install --------------------------------------------------------------------


def _paused_install(root: Path, gen: dict, monkeypatch):
    """Start a re-extraction to generation B in a thread that pauses, holding the write lock,
    right after its Markdown rename. Returns (installer thread, paused, release, errors)."""
    paused, release, errors = threading.Event(), threading.Event(), []
    real = durable_write.move

    def move(src, dst):
        real(src, dst)
        if Path(dst).suffix == ".md" and threading.current_thread().name == "installer":
            paused.set()
            assert release.wait(30)
    monkeypatch.setattr(durable_write, "move", move)
    gen["current"] = GEN_B

    def target():
        try:
            _run(root, force=True)
        except BaseException as exc:  # noqa: BLE001 - reported by the test
            errors.append(exc)
    installer = threading.Thread(target=target, name="installer")
    installer.start()
    assert paused.wait(30)
    return installer, release, errors


def _race(root, gen, monkeypatch, read):
    installer, release, errors = _paused_install(root, gen, monkeypatch)
    out: dict = {}
    reader = threading.Thread(target=lambda: out.update(read()), name="reader")
    reader.start()
    reader.join(0.5)
    held_off = reader.is_alive()
    release.set()
    installer.join(60)
    reader.join(60)
    assert not errors and not reader.is_alive()
    assert held_off, "the read ran while the install was in progress"
    return out


def test_a_consistent_read_waits_for_an_install_in_progress_and_sees_one_generation(vault, monkeypatch):
    root, gen = vault
    sid = _sid(root)
    out_paths = _layout(root).outputs(sid)

    def read():
        with xi.consistent_read(root / "normalized", sid) as journal:
            return {"state": journal.state, "markdown": out_paths["markdown"].read_text(),
                    "chunks": out_paths["chunks"].read_text()}
    seen = _race(root, gen, monkeypatch, read)
    assert seen["state"] in xi.USABLE
    assert "Generation B" in seen["markdown"]
    assert "Generation B" in seen["chunks"] and "Generation A" not in seen["chunks"]


def test_a_keyword_reindex_racing_an_install_indexes_one_generation(vault, monkeypatch):
    root, gen = vault
    sid = _sid(root)
    captured: dict = {}
    real = keyword_index._capture_chunks

    def spy(*a, **kw):
        got = real(*a, **kw)
        if threading.current_thread().name == "reader":
            captured.update({s: data for s, (_fp, data) in got[0].items()})
        return got
    monkeypatch.setattr(keyword_index, "_capture_chunks", spy)
    _race(root, gen, monkeypatch, lambda: {"stats": keyword_index.reindex(root)})
    assert b"Generation B" in captured[sid] and b"Generation A" not in captured[sid]


@pytest.mark.parametrize("producer", sorted(_PRODUCERS))
def test_a_commit_racing_a_crashed_install_writes_nothing(vault, producer):
    root, gen = vault
    sid = _sid(root)
    run, response = _PRODUCERS[producer]
    adapter = _Adapter(response, during=lambda: _crash_mid_install(root, gen))
    summary = run(root, adapter)
    assert adapter.calls == 1
    assert xi.read_journal(_layout(root), sid).state == xi.INSTALLING
    assert summary["install_incomplete"] == [sid] and summary["status"] == "partial"
    assert not _artifact(root, producer, sid).exists()


# --- synthesis inputs -------------------------------------------------------------------------


def _corrupt_journal(root: Path, sid: str) -> Path:
    path = _layout(root).journal(sid)
    durable_write.make_dirs(path.parent)
    path.write_bytes(b"{")
    return path


def test_synthesis_skips_a_topic_whose_evidence_has_no_usable_extraction(tmp_path):
    jobs = syn_t._build(tmp_path)
    sa = syn_t._sid(tmp_path, "a.md")
    journal = _corrupt_journal(tmp_path, sa)
    adapter = syn_t.SynthAdapter()
    summary = syn_t._gen(tmp_path, adapter, jobs)
    assert adapter.calls == 0 and summary["status"] == "partial"
    assert [t["sources"] for t in summary["install_incomplete"]] == [[sa]]
    assert not (tmp_path / "wiki" / "Synthesis").exists() or not list(
        (tmp_path / "wiki" / "Synthesis").glob("*.md"))
    journal.unlink()
    summary = syn_t._gen(tmp_path, adapter, jobs)
    assert adapter.calls == 1 and summary["install_incomplete"] == []


def test_a_synthesis_commit_racing_a_damaged_journal_writes_nothing(tmp_path):
    jobs = syn_t._build(tmp_path)
    sa = syn_t._sid(tmp_path, "a.md")

    class Racing(syn_t.SynthAdapter):
        def parse(self, *a, **kw):
            _corrupt_journal(tmp_path, sa)
            return super().parse(*a, **kw)
    adapter = Racing()
    summary = syn_t._gen(tmp_path, adapter, jobs)
    assert adapter.calls == 1 and summary["syntheses_written"] == 0
    assert [t["sources"] for t in summary["install_incomplete"]] == [[sa]]


# --- contradiction inputs ---------------------------------------------------------------------


def _check(tmp_path):
    conn = con_t._gconn(tmp_path)
    try:
        return contradictions.check_assertions(
            conn, reviews_dir=tmp_path / "reviews", claims_dir=tmp_path / "wiki" / "Claims",
            markdown_dir=tmp_path / "normalized" / "markdown",
            manifests_dir=tmp_path / "raw" / "manifests")
    finally:
        conn.close()


def test_contradiction_maintenance_never_retires_on_evidence_it_cannot_read(tmp_path):
    con_t._build_graph(tmp_path)
    con_t._propose(tmp_path)
    sa = con_t._sid(tmp_path, "a.md")
    md = _layout(tmp_path).outputs(sa)["markdown"]
    md.write_text("A different generation of the text.\n", encoding="utf-8")   # mixed install
    journal = _corrupt_journal(tmp_path, sa)
    found = _check(tmp_path)
    assert found["stale"] == []
    assert [e["reason"] for e in found["unverifiable"]] == [xi.INCOMPLETE]
    assert xi.INCOMPLETE in contradictions.UNVERIFIABLE_REASONS
    assert contradictions.maintain(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")["retired"] == []
    journal.unlink()                     # the same text, now usable, has changed: that one counts
    assert [e["reason"] for e in _check(tmp_path)["stale"]] == ["evidence_changed"]


def test_a_scoped_context_marks_its_blocked_document_and_a_check_window_is_refused(tmp_path):
    con_t._build_graph(tmp_path)
    sa, sb = con_t._sid(tmp_path, "a.md"), con_t._sid(tmp_path, "b.md")
    st = types.SimpleNamespace(manifests_dir=tmp_path / "raw" / "manifests",
                               wiki_dir=tmp_path / "wiki",
                               markdown_dir=tmp_path / "normalized" / "markdown")
    conn = con_t._gconn(tmp_path)
    try:
        ca, cb = graph.claims_for_source(conn, sa)[0], graph.claims_for_source(conn, sb)[0]

        def scoped(cid, sid):
            return contradictions.scoped_claim_context(
                conn, cid, sid, claims_dir=st.wiki_dir / "Claims", markdown_dir=st.markdown_dir)
        prov = {m["source_id"]: manifests.get_provenance(m)
                for m in manifests.scan_manifests(st.manifests_dir).valid}
        plan = {"independence": {s: manifests.independence_digest(prov[s]) for s in (sa, sb)},
                "fingerprints": {sa: {ca: contradictions.evidence_fingerprint(scoped(ca, sa))},
                                 sb: {cb: contradictions.evidence_fingerprint(scoped(cb, sb))}}}
        window = {"a": sa, "b": sb, "claims_a": [ca], "claims_b": [cb]}
        assert contradiction_check._revalidate(conn, st, plan, window) is None
        _corrupt_journal(tmp_path, sa)
        assert scoped(ca, sa)["install_incomplete"] == [sa]
        assert "install_incomplete" not in scoped(cb, sb)
        assert contradiction_check._revalidate(conn, st, plan, window) == (
            f"{sa} has no usable extraction (interrupted install)")
    finally:
        conn.close()


def test_a_contradiction_check_refuses_a_document_with_no_usable_extraction(tmp_path):
    con_t._build_graph(tmp_path)
    sa, sb = con_t._sid(tmp_path, "a.md"), con_t._sid(tmp_path, "b.md")
    _corrupt_journal(tmp_path, sa)
    st = types.SimpleNamespace(manifests_dir=tmp_path / "raw" / "manifests",
                               wiki_dir=tmp_path / "wiki",
                               markdown_dir=tmp_path / "normalized" / "markdown")
    conn = con_t._gconn(tmp_path)
    try:
        with pytest.raises(contradiction_check.Refused) as exc:
            contradiction_check.build_plan(st, conn, [sa, sb], MODEL)
    finally:
        conn.close()
    assert exc.value.code == xi.INCOMPLETE and sa in str(exc.value)


# --- vector index -----------------------------------------------------------------------------


SRC_A, SRC_B = "src_aaaaaaaaaaaaaaaa", "src_bbbbbbbbbbbbbbbb"


def _vector_vault(root: Path) -> None:
    for sid, text in ((SRC_A, "synergy capture"), (SRC_B, "carve out separation")):
        path = root / "normalized" / "chunks" / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"chunk_id": f"{sid}::0000", "source_id": sid, "ordinal": 0,
                                    "text": text, "char_start": 0, "char_end": len(text)}) + "\n")


def _vector_reindex(root, *, force=False):
    from tests.test_vector_index import FakeEmbedder
    from app.backend import vector_index as vi
    return vi.reindex(root, FakeEmbedder(), embedding_model_ref="bge-m3", distance_metric="cosine",
                      force=force)


def test_the_vector_index_skips_a_blocked_document_and_keeps_its_rows(tmp_path):
    pytest.importorskip("lancedb")
    from app.backend import vector_index as vi
    _vector_vault(tmp_path)
    _vector_reindex(tmp_path, force=True)
    before = set(vi.indexed_chunk_fingerprints(tmp_path))
    assert before == {f"{SRC_A}::0000", f"{SRC_B}::0000"}
    fingerprints = vi.indexed_chunk_fingerprints(tmp_path)
    _corrupt_journal(tmp_path, SRC_A)
    path_a = tmp_path / "normalized" / "chunks" / f"{SRC_A}.jsonl"
    path_a.write_text(path_a.read_text().replace("synergy capture", "a mixed generation"))
    stats = _vector_reindex(tmp_path)                    # present but unusable: neither re-embedded
    assert stats.install_incomplete == [SRC_A]           # nor removed
    assert stats.chunks_deleted == 0 and stats.chunks_embedded == 0
    assert vi.indexed_chunk_fingerprints(tmp_path) == fingerprints
    path_a.unlink()
    stats = _vector_reindex(tmp_path)
    assert stats.install_incomplete == [SRC_A] and stats.chunks_deleted == 0
    assert set(vi.indexed_chunk_fingerprints(tmp_path)) == before
    st = vi.status(tmp_path)
    assert st.removed_chunks == 0 and any("extraction install incomplete" in n for n in st.notes)
    full = _vector_reindex(tmp_path, force=True)          # a full rebuild leaves it out, reported
    assert full.install_incomplete == [SRC_A]
    assert set(vi.indexed_chunk_fingerprints(tmp_path)) == {f"{SRC_B}::0000"}


@pytest.mark.parametrize("producer,hook", [("claims", "claims_fingerprint"),
                                           ("items", "items_fingerprint")])
def test_an_empty_text_commit_racing_a_crashed_install_writes_nothing(vault, monkeypatch, producer,
                                                                      hook):
    """Empty Markdown commits with no model call; an install that crashed between its read and its
    commit (here: while the fingerprint is computed, outside the lock) still stops it."""
    root, gen = vault
    gen["current"] = {"text": "", "tables": []}
    _run(root, force=True)
    sid = _sid(root)
    assert _layout(root).outputs(sid)["markdown"].read_text() == ""
    from app.workers import enrichment_artifact as art
    real, fired = getattr(art, hook), []

    def racing(*a, **kw):
        if not fired:
            fired.append(True)
            _crash_mid_install(root, gen)
        return real(*a, **kw)
    monkeypatch.setattr(art, hook, racing)
    run, response = _PRODUCERS[producer]
    adapter = _Adapter(response)
    summary = run(root, adapter)
    assert fired and adapter.calls == 0
    assert summary["install_incomplete"] == [sid] and summary["skipped_empty"] == 0
    assert not _artifact(root, producer, sid).exists()


def test_a_topic_built_while_a_document_was_blocked_is_not_generated_after_it_unblocks(
        tmp_path, monkeypatch):
    """Its claims' quotes were read while the document had no usable extraction: the prompt would
    lack them even though the document is usable by the time of the model call."""
    from app.workers import synthesis
    jobs = syn_t._build(tmp_path)
    journal = _corrupt_journal(tmp_path, syn_t._sid(tmp_path, "a.md"))
    real = synthesis._page_status

    def unblocking(*a, **kw):                    # runs after the topics were built
        journal.unlink(missing_ok=True)
        return real(*a, **kw)
    monkeypatch.setattr(synthesis, "_page_status", unblocking)
    adapter = syn_t.SynthAdapter()
    summary = syn_t._gen(tmp_path, adapter, jobs)
    assert adapter.calls == 0 and len(summary["install_incomplete"]) == 1


def test_a_non_canonical_id_reads_as_an_absent_output(tmp_path):
    assert xi.read_text(tmp_path / "normalized", "src_aaaa", "markdown") == ""
    assert xi.read_text(tmp_path / "normalized", "../escape", "markdown") == ""


# --- publication: Source pages and Claim pages (review round 1) --------------------------------


def _source_page(root: Path, sid: str) -> Path:
    return root / "wiki" / "Sources" / f"{sid}.md"


def test_publication_keeps_a_blocked_documents_source_page_and_leaves_it_pending(blocked):
    from app.backend import publication_queue
    from app.workers import publication
    root, _gen, sid, _state = blocked
    page = _source_page(root, sid)
    before = page.read_bytes()
    assert b"Generation A" in before
    out = publication.publish(root, sources=[sid])
    assert out["status"] == publication.PENDING
    assert out["failed"]["sources"] == {sid: xi.INCOMPLETE}
    assert out["sources_failed"] == [sid] and sid not in out["sources_rendered"]
    assert page.read_bytes() == before                   # never generation B's mixed text
    assert any(sid in e["sources"] for e in publication_queue.load(root))


def test_a_direct_wiki_run_reports_a_blocked_document_and_keeps_its_page(blocked):
    from app.workers import wiki
    root, _gen, sid, _state = blocked
    before = _source_page(root, sid).read_bytes()
    gen = wiki.generate_wiki(root, jobs_db=root / "db" / "jobs.sqlite", rebuild_index=False,
                             force=True)
    assert [(e["source_id"], e["reason"]) for e in gen["error_details"]] == [(sid, xi.INCOMPLETE)]
    assert _source_page(root, sid).read_bytes() == before


def test_publication_renders_a_document_whose_journal_is_installed(vault, monkeypatch):
    from app.workers import publication
    root, gen = vault
    sid = _sid(root)
    gen["current"] = GEN_B
    with monkeypatch.context() as m:                      # every publication of the run fails
        m.setattr(publication.Run, "publish", lambda self, *a, **k: {"status": publication.PENDING})
        m.setattr(publication.Run, "finish", lambda self, *a, **k: None)
        _run(root, force=True)
    assert xi.read_journal(_layout(root), sid).state == xi.INSTALLED
    assert b"Generation A" in _source_page(root, sid).read_bytes()
    out = publication.publish(root, sources=[sid])
    assert out["status"] == publication.PUBLISHED and sid in out["sources_rendered"]
    assert b"Generation B" in _source_page(root, sid).read_bytes()


MIXED = "A mixed generation nobody may quote."


def _governance_vault(tmp_path):
    """Two documents' claims; document a's install blocked (corrupt journal) over mixed text."""
    con_t._build_graph(tmp_path)
    sa = con_t._sid(tmp_path, "a.md")
    conn = con_t._gconn(tmp_path)
    try:
        ca = graph.claims_for_source(conn, sa)[0]
        cb = graph.claims_for_source(conn, con_t._sid(tmp_path, "b.md"))[0]
    finally:
        conn.close()
    _layout(tmp_path).outputs(sa)["markdown"].write_text(MIXED + "\n" * 400, encoding="utf-8")
    _corrupt_journal(tmp_path, sa)
    return sa, ca, cb


def _claim_page(tmp_path, cid) -> str:
    return (tmp_path / "wiki" / "Claims" / f"{cid}.md").read_text(encoding="utf-8")


def _assert_anchor_without_quote(tmp_path, cid, sid):
    """The re-rendered page keeps the citation's anchor and shows no quote: neither the blocked
    (mixed) text nor the quote of the generation it had before."""
    text = _claim_page(tmp_path, cid)
    assert "nobody may quote" not in text and "The Q3 revenue increased" not in text
    block = text.split("citations:\n", 1)[1].split("contradicts:", 1)[0]
    assert f'source_id: "{sid}"' in block and "char_start: 12" in block and 'quote: ""' in block


def _approve_review(tmp_path, item):
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{item['review_id']}.json").write_text(json.dumps(item), encoding="utf-8")


def test_hiding_a_claim_proceeds_without_reading_a_blocked_quote(tmp_path):
    from app.workers import deprecations
    sa, ca, _cb = _governance_vault(tmp_path)
    _approve_review(tmp_path, {"review_id": "rev_h", "type": "hide_claim", "status": "approved",
                               "subject": {"node_id": ca, "page": f"Claims/{ca}.md"},
                               "proposal": {"to_status": "hidden"},
                               "context": {"node_type": "claim"}})
    conn = con_t._gconn(tmp_path)
    try:
        out = deprecations.apply_hidden_claims(conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki",
                                               markdown_dir=tmp_path / "normalized" / "markdown")
        conn.commit()
        assert out["applied"] == 1 and graph.get_node(conn, ca)["status"] == "hidden"
    finally:
        conn.close()
    assert "status: hidden" in _claim_page(tmp_path, ca)
    _assert_anchor_without_quote(tmp_path, ca, sa)


def test_deprecating_a_claim_proceeds_without_reading_a_blocked_quote(tmp_path):
    from app.workers import deprecations
    sa, ca, _cb = _governance_vault(tmp_path)
    _approve_review(tmp_path, {"review_id": "rev_d", "type": "deprecate_wiki_page",
                               "status": "approved",
                               "subject": {"node_id": ca, "page": f"Claims/{ca}.md"},
                               "proposal": {"to_status": "deprecated_candidate", "reason": "x"},
                               "context": {"node_type": "claim"}})
    conn = con_t._gconn(tmp_path)
    try:
        out = deprecations.apply_approved_deprecations(
            conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki",
            claims_dir=tmp_path / "wiki" / "Claims", markdown_dir=tmp_path / "normalized" / "markdown")
        conn.commit()
        assert out["applied"] == 1
    finally:
        conn.close()
    assert "status: deprecated_candidate" in _claim_page(tmp_path, ca)
    _assert_anchor_without_quote(tmp_path, ca, sa)


def test_superseding_a_claim_proceeds_without_reading_a_blocked_quote(tmp_path):
    sa, ca, cb = _governance_vault(tmp_path)
    con_t._propose(tmp_path)
    lo, hi = sorted((ca, cb))
    from app.workers import reviews
    rid = reviews.review_id("resolve_contradiction", {"claim_a": lo, "claim_b": hi})
    (tmp_path / "reviews" / "pending" / f"{rid}.json").unlink()
    _approve_review(tmp_path, {"review_id": rid, "type": "resolve_contradiction",
                               "status": "approved", "subject": {"claim_a": lo, "claim_b": hi},
                               "decided_by": "human", "decided_at": "2026-11-01T00:00:00Z",
                               "proposal": {}, "context": {}, "winner": cb,
                               "approval_id": "1" * 32})
    conn = con_t._gconn(tmp_path)
    try:
        res = contradictions.apply_contradiction_decisions(
            conn, tmp_path / "reviews", claims_dir=tmp_path / "wiki" / "Claims",
            markdown_dir=tmp_path / "normalized" / "markdown")
        conn.commit()
        assert graph.get_node(conn, ca)["status"] == "deprecated_candidate", res
    finally:
        conn.close()
    _assert_anchor_without_quote(tmp_path, ca, sa)


def test_a_full_keyword_rebuild_leaves_a_blocked_document_out_and_reports_it(blocked):
    root, _gen, sid, _state = blocked
    assert _rows(root, sid)
    stats = keyword_index.reindex(root, force=True)
    assert stats.full_rebuild and stats.install_incomplete == [sid]
    assert _rows(root, sid) == []                         # the documented full-rebuild exception
    (_layout(root).outputs(sid)["chunks"]).unlink()       # reported even with its file gone
    assert keyword_index.reindex(root, force=True).install_incomplete == [sid]
