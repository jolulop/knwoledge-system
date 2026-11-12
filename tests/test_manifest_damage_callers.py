"""ADR-0074 decision 2, slice 2: what every kind of caller does with a damaged manifest.

Intake preserves it and reports it (only a verified absence creates); a runtime worker counts it and
reports incomplete coverage; an action that depends on proving a document absent is blocked;
retrieval excludes the document before its evidence reaches the model; validators fail; the API
answers 409 (never 404, never a write). This is the damaged-manifest exclusion only — visibility
suppression of *valid* manifests is change 7a."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import validate_raw_integrity  # noqa: E402

from app.backend import keyword_index, manifests, search  # noqa: E402
from app.backend import main as main_module  # noqa: E402
from app.backend.config import get_settings  # noqa: E402
from app.backend.policy import RetrievalPolicy  # noqa: E402
from app.workers import extract, intake, wiki  # noqa: E402
from tests import fixtures  # noqa: E402
from tests.test_search import _chunk, _write_chunks, _write_page  # noqa: E402

JOBS = "db/jobs.sqlite"


def _vault(tmp_path, docs=("one.md", "two.md")):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.copy_templates(tmp_path)
    for name in docs:
        (inbox / name).write_text(f"# {name}\n\nA document called {name} with prose.\n",
                                  encoding="utf-8")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / JOBS)
    md = tmp_path / "raw" / "manifests"
    sids = {m["original_filename"]: m["source_id"] for m in manifests.scan_manifests(md).valid}
    return md, sids


def _damage(md: Path, sid: str, how: str) -> Path:
    path = md / f"{sid}.json"
    if how == "corrupt":
        path.write_text('{"source_id": "' + sid + '", truncated', encoding="utf-8")
    else:                                                  # unreadable: not a regular file
        path.unlink()
        path.mkdir()
    return path


def _snapshot(path: Path):
    return path.read_bytes() if path.is_file() else "dir"


# --- intake: only a verified absence creates ---------------------------------------------------


@pytest.mark.parametrize("how", ["corrupt", "unreadable"])
def test_intake_preserves_a_damaged_manifest_and_continues(tmp_path, how):
    md, sids = _vault(tmp_path)
    damaged = _damage(md, sids["one.md"], how)
    before = _snapshot(damaged)
    (tmp_path / "raw" / "inbox" / "three.md").write_text("a third document\n", encoding="utf-8")
    out = intake.scan_inbox(tmp_path, jobs_db=tmp_path / JOBS)
    assert _snapshot(damaged) == before                    # no write, no occurrence update
    ((err,),) = [[e for e in out["error_details"] if e.get("source_id") == sids["one.md"]]]
    assert err["reason"] == f"manifest_{how}" and "truncated" not in err["error"]
    assert out["new_manifests"] == 1 and out["updated_manifests"] == 1   # three created, two updated
    assert sids["one.md"] not in out["source_ids"]


def test_intake_creates_only_on_verified_absence(tmp_path):
    md, sids = _vault(tmp_path, docs=("one.md",))
    (md / f"{sids['one.md']}.json").unlink()
    out = intake.scan_inbox(tmp_path, jobs_db=tmp_path / JOBS)
    assert out["new_manifests"] == 1 and manifests.read_manifest(md, sids["one.md"]).state == "ok"


# --- runtime workers: counted, coverage reported, never recreated ------------------------------


@pytest.mark.parametrize("how", ["corrupt", "unreadable"])
def test_workers_count_damage_and_report_incomplete_coverage(tmp_path, how):
    md, sids = _vault(tmp_path)
    _damage(md, sids["two.md"], how)
    ext = extract.extract_sources(tmp_path, jobs_db=tmp_path / JOBS)
    gen = wiki.generate_wiki(tmp_path, jobs_db=tmp_path / JOBS, rebuild_index=False,
                             templates_dir=tmp_path / "templates")
    for out in (ext, gen):
        assert out[f"manifests_{how}"] == 1 and out["manifest_coverage"] == "incomplete"
    assert ext["extracted"] == 1                           # the readable document still runs
    assert (tmp_path / "wiki" / "Sources" / f"{sids['one.md']}.md").exists()
    assert not (tmp_path / "wiki" / "Sources" / f"{sids['two.md']}.md").exists()


def test_extract_never_recreates_a_manifest_damaged_during_the_run(tmp_path, monkeypatch):
    md, sids = _vault(tmp_path, docs=("one.md",))
    sid = sids["one.md"]
    real = extract._extract_one

    def damaged_meanwhile(manifest, *a, **k):
        out = real(manifest, *a, **k)
        _damage(md, sid, "corrupt")                         # between the model-free parse and install
        return out
    monkeypatch.setattr(extract, "_extract_one", damaged_meanwhile)
    out = extract.extract_sources(tmp_path, jobs_db=tmp_path / JOBS)
    assert out["errors"] == 1 and out["error_details"][0]["error"] == "manifest_corrupt"
    assert manifests.read_manifest(md, sid).state == "corrupt"      # still the damaged bytes
    assert not (tmp_path / "normalized" / "markdown" / f"{sid}.md").exists()   # nothing installed
    assert not (tmp_path / "normalized" / ".staging" / sid).exists()


# --- absence-dependent actions are blocked ------------------------------------------------------


def test_synthesis_retraction_is_blocked_on_an_incomplete_inventory(tmp_path):
    from app.llm.cache import ResponseCache
    from app.llm.client import LLMClient
    from app.workers import claims
    from tests.test_synthesis import TIER2, SynthAdapter, _build, _EmptyClaims, _gen, _sid
    jobs = _build(tmp_path)
    _gen(tmp_path, SynthAdapter(), jobs)
    fresh = LLMClient({"anthropic": _EmptyClaims()},
                      cache=ResponseCache(tmp_path / "db" / "fresh.sqlite"))
    claims.extract_claims(tmp_path, client=fresh, model_ref=TIER2, jobs_db=jobs,
                          source_ids=[_sid(tmp_path, "b.md")], force=True, rebuild_index=False)
    stray = tmp_path / "raw" / "manifests" / "src_000000000000dead.json"
    stray.write_text("{", encoding="utf-8")                # an unrelated damaged manifest
    blocked = _gen(tmp_path, SynthAdapter(), jobs)
    assert blocked["retracted"] == 0 and blocked["retraction_blocked"] is True
    assert blocked["manifest_coverage"] == "incomplete"
    stray.unlink()
    done = _gen(tmp_path, SynthAdapter(), jobs)
    assert done["retracted"] == 1 and done["retraction_blocked"] is False


def test_contradiction_maintenance_never_retires_on_a_damaged_manifest(tmp_path):
    from tests.test_contradiction_maintenance import _edge_status, _maintain
    from tests.test_contradiction_maintenance import _vault as contradiction_vault
    a, b, _sa, sb = contradiction_vault(tmp_path)
    _damage(tmp_path / "raw" / "manifests", sb, "corrupt")
    out = _maintain(tmp_path)
    assert out["retired"] == []
    assert [e["reason"] for e in out["unverifiable"]] == ["manifest_inventory_incomplete"]
    assert _edge_status(tmp_path, a, b) == ["proposed"]


# --- retrieval: excluded before evidence reaches the model --------------------------------------


@pytest.mark.parametrize("how", ["corrupt", "unreadable"])
def test_search_excludes_a_document_whose_manifest_is_damaged(tmp_path, how):
    md = tmp_path / "raw" / "manifests"
    md.mkdir(parents=True)
    a, b = "src_aaaaaaaaaaaaaaaa", "src_bbbbbbbbbbbbbbbb"
    for sid in (a, b):
        (md / f"{sid}.json").write_text(json.dumps({"source_id": sid}), encoding="utf-8")
        _write_chunks(tmp_path, sid, [_chunk(sid, 0, "alpha evidence text", 0)])
        _write_page(tmp_path, f"wiki/Sources/{sid}.md",
                    {"type": "source", "source_id": sid, "title": sid, "status": "active",
                     "language": "en", "aliases": [], "tags": []}, "alpha summary")
    keyword_index.reindex(tmp_path, force=True)
    kconn = keyword_index.connect(tmp_path / keyword_index.DB_RELPATH)
    try:
        def run():
            return search.run_search(q="alpha", mode="auto", manifests_dir=md, keyword_conn=kconn,
                                     graph_conn=None, policy=RetrievalPolicy())
        assert {h["source_id"] for h in run()["evidence"]} == {a, b}
        _damage(md, b, how)
        res = run()
        assert {h["source_id"] for h in res["evidence"]} == {a}
        assert all(h.get("node_id") != b for h in res["navigation"])
        assert any("manifest unreadable, corrupt, quarantined or missing" in n for n in res["notes"])
        (md / f"{b}.json").rmdir() if how == "unreadable" else (md / f"{b}.json").unlink()
        (md / f"{b}.json").write_text(json.dumps({"source_id": b}), encoding="utf-8")
        assert {h["source_id"] for h in run()["evidence"]} == {a, b}     # recovered: back again
    finally:
        kconn.close()


def test_the_query_evidence_pack_never_sees_a_damaged_document(tmp_path, monkeypatch):
    """/query builds its evidence from the same gated search."""
    md = tmp_path / "raw" / "manifests"
    md.mkdir(parents=True)
    sid = "src_cccccccccccccccc"
    _write_chunks(tmp_path, sid, [_chunk(sid, 0, "gamma evidence text", 0)])
    _write_page(tmp_path, f"wiki/Sources/{sid}.md",
                {"type": "source", "source_id": sid, "title": sid, "status": "active",
                 "language": "en", "aliases": [], "tags": []}, "gamma")
    keyword_index.reindex(tmp_path, force=True)
    (md / f"{sid}.json").write_text("{bad", encoding="utf-8")
    settings = get_settings(tmp_path)
    monkeypatch.setattr(main_module, "settings", settings)
    res = TestClient(main_module.app).get("/search", params={"q": "gamma", "mode": "keyword"})
    assert res.status_code == 200 and res.json()["evidence"] == []


# --- validators fail; the API answers 409 -------------------------------------------------------


@pytest.mark.parametrize("how", ["corrupt", "unreadable"])
def test_validate_raw_integrity_fails_on_a_damaged_manifest(tmp_path, capsys, how):
    md, sids = _vault(tmp_path, docs=("one.md",))
    _damage(md, sids["one.md"], how)
    assert validate_raw_integrity.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert f"raw/manifests/{sids['one.md']}.json: manifest is {how}" in out
    assert "truncated" not in out and str(tmp_path) not in out


def test_validate_raw_integrity_fails_when_the_directory_cannot_be_listed(tmp_path, capsys):
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "manifests").write_text("not a directory")
    assert validate_raw_integrity.main([str(tmp_path)]) == 1
    assert "raw/manifests: could not be listed (NotADirectoryError)" in capsys.readouterr().out


def test_validate_raw_integrity_renders_a_hostile_name_safely(tmp_path, capsys):
    md = tmp_path / "raw" / "manifests"
    md.mkdir(parents=True)
    (md / "x\x1b[2J.json").write_text("{", encoding="utf-8")
    assert validate_raw_integrity.main([str(tmp_path)]) == 1
    assert "\x1b" not in capsys.readouterr().out


def test_the_api_answers_409_for_a_damaged_manifest_and_never_writes(tmp_path, monkeypatch):
    md, sids = _vault(tmp_path)
    path = _damage(md, sids["one.md"], "corrupt")
    before = path.read_bytes()
    monkeypatch.setattr(main_module, "settings", get_settings(tmp_path))
    client = TestClient(main_module.app)
    listing = client.get("/sources").json()
    assert listing["count"] == 1 and listing["manifests_corrupt"] == 1
    assert listing["manifest_coverage"] == "incomplete"
    res = client.get(f"/sources/{sids['one.md']}")
    assert res.status_code == 409 and res.json()["detail"]["reason"] == "manifest_corrupt"
    assert "truncated" not in res.text
    assert client.get("/sources/src_0000000000000000").status_code == 404   # absent stays 404
    assert path.read_bytes() == before


def test_lint_reports_a_damaged_manifest_as_its_own_finding(tmp_path):
    from app.workers import lint
    md, sids = _vault(tmp_path, docs=("one.md",))
    _damage(md, sids["one.md"], "unreadable")
    findings = lint._check_missing_raw(md, tmp_path, tmp_path / "reviews", file_items=False,
                                       now="2026-11-12T00:00:00Z", filed=[], existing=[])
    assert [f["check"] for f in findings] == ["manifest_unreadable"]   # never "missing_raw"
    assert findings[0]["subject"] == f"raw/manifests/{sids['one.md']}.json"


def test_the_wiki_and_normalized_validators_report_damage_instead_of_orphans(tmp_path, capsys):
    import validate_normalized
    import validate_wiki
    md, sids = _vault(tmp_path, docs=("one.md",))
    extract.extract_sources(tmp_path, jobs_db=tmp_path / JOBS)
    wiki.generate_wiki(tmp_path, jobs_db=tmp_path / JOBS, rebuild_index=False,
                       templates_dir=tmp_path / "templates")
    _damage(md, sids["one.md"], "corrupt")
    assert validate_wiki.main([str(tmp_path)]) == 1
    assert validate_normalized.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "manifest is corrupt" in out and "orphan" not in out and "truncated" not in out


# --- review round 1 (slice 2): every mutation / recovery branch revalidates ----------------------


def _claims_setup(tmp_path):
    from app.workers import claims
    from tests.test_claims import FakeAdapter as ClaimsAdapter
    from tests.test_producer_publication import _claims_vault, _extract_claims
    sid = _claims_vault(tmp_path)
    _extract_claims(tmp_path, ClaimsAdapter())
    return sid, claims


def _claims_run(tmp_path, **kw):
    from app.llm.client import LLMClient
    from app.workers import claims
    from tests.test_claims import FakeAdapter as ClaimsAdapter
    from tests.test_tag_links import MODEL_REF
    return claims.extract_claims(tmp_path, client=LLMClient({"anthropic": ClaimsAdapter()}),
                                 model_ref=MODEL_REF, jobs_db=tmp_path / JOBS, **kw)


def _active_claims(tmp_path, sid):
    from app.backend import graph
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        return {c for c in graph.claims_for_source(gconn, sid)
                if (graph.get_node(gconn, c) or {}).get("status") == "active"}
    finally:
        gconn.close()


def _duplicate(md: Path, sid: str) -> Path:
    """A second file claiming `sid` (identity validation quarantines both copies)."""
    dup = md / "src_000000000000dead.json"
    dup.write_bytes((md / f"{sid}.json").read_bytes())
    return dup


@pytest.mark.parametrize("how", ["corrupt", "duplicate"])
def test_claims_journal_redo_is_kept_while_the_manifest_is_unusable(tmp_path, how):
    """Repair queues the document; claims extraction must not clear that redo (nor retire its
    evidence) as "deleted" while its manifest is damaged or quarantined."""
    import repair_producer_journal
    from app.backend import producer_journal
    sid, _claims = _claims_setup(tmp_path)
    md = tmp_path / "raw" / "manifests"
    standing = _active_claims(tmp_path, sid)
    assert standing
    good = (md / f"{sid}.json").read_bytes()
    extra = _damage(md, sid, "corrupt") if how == "corrupt" else _duplicate(md, sid)
    producer_journal.journal_path(tmp_path).write_text("{", encoding="utf-8")       # damaged
    assert repair_producer_journal.main([str(tmp_path)]) == 0
    assert sid in producer_journal.pending(tmp_path, "extract_claims")

    out = _claims_run(tmp_path)
    reason = "manifest_corrupt" if how == "corrupt" else "manifest_duplicate_source_id"
    assert [e["error"] for e in out["error_details"] if e["source_id"] == sid] == [reason]
    assert out["journal_reconciled"] == [] and out["status"] != "succeeded"
    assert sid in producer_journal.pending(tmp_path, "extract_claims")            # still queued
    assert _active_claims(tmp_path, sid) == standing                             # evidence kept

    if how == "corrupt":
        (md / f"{sid}.json").write_bytes(good)
    else:
        extra.unlink()
    assert _claims_run(tmp_path)["status"] == "succeeded"
    assert producer_journal.pending(tmp_path, "extract_claims") == {}


def test_items_recovery_keeps_the_record_and_mentions_of_a_corrupt_manifest(tmp_path, monkeypatch):
    from app.backend import graph, producer_journal
    from app.workers import items
    from tests.test_producer_journal import _TWO_TAGS, _crash_after, _items_now, _status
    from tests.test_producer_publication import _extract_items, _extract_sources, _ingest
    from tests.test_tag_links import RoutingAdapter, _item, _sids
    _ingest(tmp_path, {"a.md": "alpha body"})
    _extract_sources(tmp_path)
    a = _sids(tmp_path)["a.md"]
    _extract_items(tmp_path, RoutingAdapter(_TWO_TAGS))
    beta = items.node_id("Beta Tag")
    real = _crash_after(monkeypatch, graph, "supersede_mentions_for_source")
    with pytest.raises(KeyboardInterrupt):
        _items_now(tmp_path, {"alpha": [_item("Alpha Tag")]}, force=True)
    monkeypatch.setattr(graph, "supersede_mentions_for_source", real)
    md = tmp_path / "raw" / "manifests"
    good = (md / f"{a}.json").read_bytes()
    _damage(md, a, "corrupt")

    out = _items_now(tmp_path, {"alpha": [_item("Alpha Tag")]})
    assert [e["error"] for e in out["error_details"] if e["source_id"] == a] == ["manifest_corrupt"]
    assert out["journal_reconciled"] == [] and a in producer_journal.pending(tmp_path, "extract_items")
    assert _status(tmp_path, beta) == "candidate"                     # never retired as "deleted"

    (md / f"{a}.json").write_bytes(good)
    assert _items_now(tmp_path, {"alpha": [_item("Alpha Tag")]})["status"] == "succeeded"
    assert _status(tmp_path, beta) == "deprecated_candidate"
    assert producer_journal.pending(tmp_path, "extract_items") == {}


def _damage_after_scan(monkeypatch, module, md: Path, sid: str):
    real = module.scan_manifests

    def scan_then_damage(directory):
        inv = real(directory)
        _damage(md, sid, "corrupt")                         # after the scan, before the commit
        return inv
    monkeypatch.setattr(module, "scan_manifests", scan_then_damage)


def test_claims_empty_input_branch_revalidates_before_retiring(tmp_path, monkeypatch):
    sid, claims = _claims_setup(tmp_path)
    md = tmp_path / "raw" / "manifests"
    standing = _active_claims(tmp_path, sid)
    assert standing
    (tmp_path / "normalized" / "markdown" / f"{sid}.md").write_text("", encoding="utf-8")
    _damage_after_scan(monkeypatch, claims, md, sid)
    out = _claims_run(tmp_path)
    assert out["skipped_empty"] == 0 and out["errors"] == 1
    assert out["error_details"][0] == {"source_id": sid, "error": "manifest_corrupt"}
    assert _active_claims(tmp_path, sid) == standing                    # evidence never retired


def test_items_empty_input_branch_revalidates_before_retiring(tmp_path, monkeypatch):
    from app.workers import items
    from tests.test_producer_journal import _TWO_TAGS, _items_now, _status
    from tests.test_producer_publication import _extract_items, _extract_sources, _ingest
    from tests.test_tag_links import RoutingAdapter, _sids
    _ingest(tmp_path, {"a.md": "alpha body"})
    _extract_sources(tmp_path)
    a = _sids(tmp_path)["a.md"]
    _extract_items(tmp_path, RoutingAdapter(_TWO_TAGS))
    alpha = items.node_id("Alpha Tag")
    (tmp_path / "normalized" / "markdown" / f"{a}.md").write_text("", encoding="utf-8")
    _damage_after_scan(monkeypatch, items, tmp_path / "raw" / "manifests", a)
    out = _items_now(tmp_path, {"alpha": []})
    assert out["skipped_empty"] == 0 and out["errors"] == 1
    assert out["error_details"][0] == {"source_id": a, "error": "manifest_corrupt"}
    assert _status(tmp_path, alpha) == "candidate"                       # mentions never retired


# --- review round 1: identity quarantine never establishes an absence ---------------------------


def test_synthesis_retraction_is_blocked_by_a_quarantined_duplicate(tmp_path):
    from app.llm.cache import ResponseCache
    from app.llm.client import LLMClient
    from app.workers import claims
    from tests.test_synthesis import TIER2, SynthAdapter, _build, _EmptyClaims, _gen, _sid
    jobs = _build(tmp_path)
    _gen(tmp_path, SynthAdapter(), jobs)
    fresh = LLMClient({"anthropic": _EmptyClaims()},
                      cache=ResponseCache(tmp_path / "db" / "fresh.sqlite"))
    claims.extract_claims(tmp_path, client=fresh, model_ref=TIER2, jobs_db=jobs,
                          source_ids=[_sid(tmp_path, "b.md")], force=True, rebuild_index=False)
    md = tmp_path / "raw" / "manifests"
    dup = _duplicate(md, _sid(tmp_path, "a.md"))       # the topic's other source, now ambiguous
    blocked = _gen(tmp_path, SynthAdapter(), jobs)
    assert blocked["retracted"] == 0 and blocked["retraction_blocked"] is True
    assert blocked["manifest_coverage"] == "incomplete" and blocked["manifests_skipped_invalid"] == 2
    dup.unlink()
    assert _gen(tmp_path, SynthAdapter(), jobs)["retracted"] == 1


def test_contradiction_maintenance_never_retires_on_a_quarantined_duplicate(tmp_path):
    from tests.test_contradiction_maintenance import _edge_status, _maintain
    from tests.test_contradiction_maintenance import _vault as contradiction_vault
    a, b, _sa, sb = contradiction_vault(tmp_path)
    _duplicate(tmp_path / "raw" / "manifests", sb)
    out = _maintain(tmp_path)
    assert out["retired"] == []
    assert [e["reason"] for e in out["unverifiable"]] == ["manifest_inventory_incomplete"]
    assert _edge_status(tmp_path, a, b) == ["proposed"]


def test_a_contradiction_check_refuses_a_quarantined_document(tmp_path):
    from app.workers import contradiction_check
    md, sids = _vault(tmp_path)
    _duplicate(md, sids["two.md"])
    with pytest.raises(contradiction_check.Refused) as exc:
        contradiction_check._documents(md, [sids["one.md"], sids["two.md"]])
    assert exc.value.code == "manifest_unavailable"
    assert "duplicate_source_id" in str(exc.value)


def test_the_raw_endpoint_answers_409_for_a_quarantined_manifest(tmp_path, monkeypatch):
    md, sids = _vault(tmp_path, docs=("one.md",))
    _duplicate(md, sids["one.md"])
    monkeypatch.setattr(main_module, "settings", get_settings(tmp_path))
    res = TestClient(main_module.app).get(f"/raw/{sids['one.md']}")
    assert res.status_code == 409
    assert res.json()["detail"]["reason"] == "manifest_duplicate_source_id"


# --- review round 1: retrieval admits only usable, identity-validated authority -----------------


def _searchable(tmp_path, sids, text="alpha evidence text"):
    md = tmp_path / "raw" / "manifests"
    md.mkdir(parents=True, exist_ok=True)
    for sid in sids:
        (md / f"{sid}.json").write_text(json.dumps({"source_id": sid}), encoding="utf-8")
        _write_chunks(tmp_path, sid, [_chunk(sid, 0, f"{text} {sid}", 0)])
        _write_page(tmp_path, f"wiki/Sources/{sid}.md",
                    {"type": "source", "source_id": sid, "title": sid, "status": "active",
                     "language": "en", "aliases": [], "tags": []}, "alpha summary")
    keyword_index.reindex(tmp_path, force=True)
    return md


@pytest.mark.parametrize("how", ["missing_folder", "absent", "duplicate"])
def test_search_excludes_unavailable_authority(tmp_path, how):
    import shutil
    a, b = "src_aaaaaaaaaaaaaaaa", "src_bbbbbbbbbbbbbbbb"
    md = _searchable(tmp_path, (a, b))
    kconn = keyword_index.connect(tmp_path / keyword_index.DB_RELPATH)
    try:
        def run():
            return search.run_search(q="alpha", mode="auto", manifests_dir=md, keyword_conn=kconn,
                                     graph_conn=None, policy=RetrievalPolicy())
        assert {h["source_id"] for h in run()["evidence"]} == {a, b}
        if how == "missing_folder":
            shutil.rmtree(md)
            expected = set()
        elif how == "absent":
            (md / f"{b}.json").unlink()
            expected = {a}
        else:
            _duplicate(md, b)
            expected = {a}
        res = run()
        assert {h["source_id"] for h in res["evidence"]} == expected
        assert not {h.get("node_id") for h in res["navigation"]} & ({a, b} - expected)
        assert any("quarantined or missing" in n for n in res["notes"])
    finally:
        kconn.close()


class _RecordingQueryClient:
    """A fake query model that cites every evidence entry and records each prompt it is sent."""

    def __init__(self):
        self.prompts: list[str] = []

    def provider_available(self, model_ref):
        return True

    def resolve_run_model(self, chain):
        return chain.split(",")[0].strip(), True

    def chain_available(self, chain):
        return True

    def parse(self, messages, schema, model_ref, **kwargs):
        self.prompts.append("\n".join(str(m.get("content")) for m in messages))
        pack = json.loads(messages[-1]["content"].split("EVIDENCE:\n", 1)[1])
        return {"claims": [{"text": f"Grounded claim for {e['evidence_id']}.",
                            "evidence_ids": [e["evidence_id"]]} for e in pack]}


@pytest.mark.parametrize("how", ["corrupt", "duplicate", "absent"])
def test_query_never_sends_excluded_evidence_to_the_model(tmp_path, monkeypatch, how):
    from tests.test_hide_content import _write_searchable_source
    good, bad = "src_aaaaaaaaaaaaaaaa", "src_bbbbbbbbbbbbbbbb"
    for sid in (good, bad):
        _write_searchable_source(tmp_path, sid, "summary first navigation reads the index first")
    keyword_index.reindex(tmp_path, force=True)
    md = tmp_path / "raw" / "manifests"
    if how == "corrupt":
        _damage(md, bad, "corrupt")
    elif how == "duplicate":
        _duplicate(md, bad)
    else:
        (md / f"{bad}.json").unlink()
    settings = get_settings(tmp_path)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    fake = _RecordingQueryClient()
    monkeypatch.setattr(main_module, "_query_client", lambda: fake)
    res = TestClient(main_module.app).post(
        "/query", json={"question": "summary navigation index"})
    assert res.status_code == 200, res.text
    assert fake.prompts, res.json()                             # the model was asked
    assert all(bad not in p for p in fake.prompts)              # the excluded document never was
    assert any(good in p for p in fake.prompts)
    assert {c["source_id"] for c in res.json()["citations"]} == {good}


# --- review round 2: one inventory-aware lookup behind every per-document endpoint -------------


def _api(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "settings", get_settings(tmp_path))
    return TestClient(main_module.app)


def test_an_unlistable_inventory_never_reads_as_an_unknown_source(tmp_path, monkeypatch):
    """raw/manifests is not a directory: no per-document endpoint may answer 404 (absence can't be
    established), only the existing 409 inventory error."""
    md, sids = _vault(tmp_path, docs=("one.md",))
    extract.extract_sources(tmp_path, jobs_db=tmp_path / JOBS)
    sid = sids["one.md"]
    md.rename(md.with_name("manifests.moved"))
    md.write_text("not a directory", encoding="utf-8")
    client = _api(tmp_path, monkeypatch)
    listing = client.get("/sources").json()
    assert listing["count"] == 0 and listing["manifest_coverage"] == "unavailable"
    for path in (f"/sources/{sid}", f"/raw/{sid}", f"/sources/{sid}/chunks",
                 f"/sources/{sid}/normalized", "/sources/src_00000000000000ee",
                 "/raw/src_00000000000000ee"):
        res = client.get(path)
        assert res.status_code == 409, path
        assert res.json()["detail"]["reason"] == "manifest_inventory_incomplete", path
        assert "not a directory" not in res.text
    assert client.get("/sources/not-an-id").status_code == 404        # names no manifest at all


def test_duplicated_authority_is_rejected_consistently_and_only_for_that_document(tmp_path,
                                                                                 monkeypatch):
    md, sids = _vault(tmp_path)
    extract.extract_sources(tmp_path, jobs_db=tmp_path / JOBS)
    dup_sid, other = sids["one.md"], sids["two.md"]
    _duplicate(md, dup_sid)
    client = _api(tmp_path, monkeypatch)

    listing = client.get("/sources").json()
    assert [s["source_id"] for s in listing["sources"]] == [other]
    assert listing["manifests_skipped_invalid"] == 2 and listing["manifest_coverage"] == "incomplete"
    for path in (f"/sources/{dup_sid}", f"/raw/{dup_sid}", f"/sources/{dup_sid}/chunks",
                 f"/sources/{dup_sid}/normalized"):
        res = client.get(path)
        assert res.status_code == 409, path
        assert res.json()["detail"]["reason"] == "manifest_duplicate_source_id", path
    # The quarantined copy's own filename id is excluded the same way.
    assert client.get("/sources/src_000000000000dead").json()["detail"]["reason"] == \
        "manifest_duplicate_source_id"
    # An unrelated, validated document is never blocked by another one's quarantine.
    for path in (f"/sources/{other}", f"/raw/{other}", f"/sources/{other}/chunks",
                 f"/sources/{other}/normalized"):
        assert client.get(path).status_code == 200, path
    assert client.get("/sources/src_00000000000000ee").status_code == 404   # absent stays 404


def test_lookup_is_the_shared_inventory_view(tmp_path):
    md, sids = _vault(tmp_path)
    one, two = sids["one.md"], sids["two.md"]
    assert manifests.lookup(md, one)["source_id"] == one
    assert manifests.lookup(md, "src_00000000000000ee") is None
    _damage(md, two, "corrupt")
    assert manifests.lookup(md, one)["source_id"] == one               # unrelated damage: no block
    with pytest.raises(manifests.ManifestUnavailable):
        manifests.lookup(md, two)
    with pytest.raises(ValueError):
        manifests.lookup(md, "../x")
