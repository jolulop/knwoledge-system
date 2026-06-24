"""Phase 7 slice 7-2: stale/retention producer + reversible archive_source executor (ADR-0036).

Key-free tests: manifest is the durable source-status authority; the Source page reads it; stale-check
proposes archive/delete candidates without acting; the executor flips active -> archive_candidate on the
manifest + page + graph node, reversibly, idempotently, and never touches raw bytes.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.backend import graph, keyword_index, manifests, review_read, search
from app.backend import main as main_module
from app.backend.config import get_settings
from app.workers import retention, wiki
from app.workers.wiki_render import parse_frontmatter, render_source_page

NOW = "2026-06-23T00:00:00+00:00"
OLD = "2022-01-01T00:00:00+00:00"        # ~4.5 years before NOW -> archive candidate
RECENT = "2026-06-01T00:00:00+00:00"     # weeks before NOW -> not stale
_TEMPLATE = (ROOT / "templates" / "source.md").read_text(encoding="utf-8")


def _write_manifest(tmp_path, sid, *, status="active", modified_at=OLD, discovered_at=OLD,
                    retention_class="permanent", extracted=True):
    md = tmp_path / "raw" / "manifests"
    md.mkdir(parents=True, exist_ok=True)
    rel = f"raw/inbox/{sid}.md"
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text("raw", encoding="utf-8")
    manifest = {
        "source_id": sid, "sha256": sid + "0" * 8, "relative_raw_path": rel,
        "file_extension": ".md", "language": "en", "page_count": None, "chunk_count": 1,
        "ingestion_status": "extracted" if extracted else "scanned",
        "normalized": {"markdown_path": f"normalized/markdown/{sid}.md"},
        "created_at": discovered_at, "discovered_at": discovered_at, "modified_at": modified_at,
        "retention_class": retention_class, "occurrences": [{"relative_path": rel}],
    }
    if status != "active":
        manifest["status"] = status
    (md / f"{sid}.json").write_text(json.dumps(manifest), encoding="utf-8")
    norm = tmp_path / "normalized" / "markdown" / f"{sid}.md"
    norm.parent.mkdir(parents=True, exist_ok=True)
    norm.write_text(f"# {sid}\n\nSome real prose body text for the source.\n", encoding="utf-8")
    return manifest


def _setup_rendered_source(tmp_path, sid, **kw):
    """Manifest + normalized markdown + templates + an initial generated Source page."""
    shutil.copytree(ROOT / "templates", tmp_path / "templates", dirs_exist_ok=True)
    _write_manifest(tmp_path, sid, **kw)
    wiki.generate_wiki(tmp_path, source_ids=[sid], rebuild_index=False, record_job=False)
    return tmp_path / "wiki" / "Sources" / f"{sid}.md"


def _approve_archive(tmp_path, sid, rid="rev_arch"):
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": "archive_source", "status": "approved",
        "subject": {"source_id": sid}, "proposal": {"to_status": "archive_candidate"},
        "context": {}}), encoding="utf-8")


# --- manifest status authority ---------------------------------------------


def test_set_get_status_roundtrip(tmp_path):
    _write_manifest(tmp_path, "src_00000000000000ab")
    md = tmp_path / "raw" / "manifests"
    assert manifests.get_status(manifests.load_manifest(md, "src_00000000000000ab")) == "active"  # default
    manifests.set_status(md, "src_00000000000000ab", "archive_candidate")
    assert manifests.get_status(manifests.load_manifest(md, "src_00000000000000ab")) == "archive_candidate"
    manifests.set_status(md, "src_00000000000000ab", "active")  # reversible
    assert manifests.get_status(manifests.load_manifest(md, "src_00000000000000ab")) == "active"


def test_set_status_validates(tmp_path):
    _write_manifest(tmp_path, "src_00000000000000ab")
    with pytest.raises(ValueError):
        manifests.set_status(tmp_path / "raw" / "manifests", "src_00000000000000ab", "bogus")
    assert manifests.set_status(tmp_path / "raw" / "manifests", "src_0000000000000015", "archived") is None


def test_source_page_renders_manifest_status(tmp_path):
    m = _write_manifest(tmp_path, "src_00000000000000ab", status="archive_candidate")
    page = render_source_page(_TEMPLATE, m, "# t\n\nbody.\n", summary_max=320, summary_min=40)
    assert parse_frontmatter(page)["status"] == "archive_candidate"
    m2 = _write_manifest(tmp_path, "src_00000000000000cd")  # unset -> default active
    assert parse_frontmatter(render_source_page(
        _TEMPLATE, m2, "# t\n\nbody.\n", summary_max=320, summary_min=40))["status"] == "active"


# --- stale-check producer (detect-and-propose) -----------------------------


def _stale(tmp_path):
    # the real retention policy drives the thresholds (ephemeral.enabled etc.)
    (tmp_path / "policies").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "policies" / "retention.yaml", tmp_path / "policies" / "retention.yaml")
    return retention.run_stale_check(tmp_path, record_job=False, now=NOW)


def _pending(tmp_path, rtype):
    d = tmp_path / "reviews" / "pending"
    return [p for p in (d.glob("*.json") if d.exists() else [])
            if json.loads(p.read_text())["type"] == rtype]


def test_stale_check_proposes_archive_for_old_active_source(tmp_path):
    _write_manifest(tmp_path, "src_000000000000001d", modified_at=OLD)
    res = _stale(tmp_path)
    assert res["archive_candidates_filed"] == 1
    item = json.loads(_pending(tmp_path, "archive_source")[0].read_text())
    assert item["subject"] == {"source_id": "src_000000000000001d"}
    assert item["proposal"]["to_status"] == "archive_candidate" and item["proposal"]["age_days"] > 1000


def test_stale_check_skips_recent_source(tmp_path):
    _write_manifest(tmp_path, "src_000000000000002e", modified_at=RECENT)
    res = _stale(tmp_path)
    assert res["archive_candidates_filed"] == 0 and _pending(tmp_path, "archive_source") == []


def test_stale_check_skips_already_archived(tmp_path):
    _write_manifest(tmp_path, "src_00000000000000c0", status="archive_candidate", modified_at=OLD)
    assert _stale(tmp_path)["archive_candidates_filed"] == 0


def test_stale_check_skips_deprecated_candidate_source(tmp_path):
    _write_manifest(tmp_path, "src_00000000000000de", status="deprecated_candidate", modified_at=OLD)
    res = _stale(tmp_path)
    assert res["archive_candidates"] == 0 and _pending(tmp_path, "archive_source") == []


def test_stale_check_ephemeral_proposes_delete_candidate(tmp_path):
    _write_manifest(tmp_path, "src_00000000000000ef", retention_class="ephemeral", discovered_at=OLD)
    res = _stale(tmp_path)
    assert res["delete_candidates_filed"] == 1
    assert json.loads(_pending(tmp_path, "delete_raw_file")[0].read_text())["proposal"]["record_only"]


def test_stale_check_idempotent(tmp_path):
    _write_manifest(tmp_path, "src_000000000000001d", modified_at=OLD)
    _stale(tmp_path)
    res2 = _stale(tmp_path)
    assert res2["archive_candidates_filed"] == 0 and res2["archive_candidates_existing"] == 1
    assert len(_pending(tmp_path, "archive_source")) == 1


# --- archive executor (reversible status only) -----------------------------


def _apply(tmp_path):
    return retention.apply_archive_sources(
        tmp_path, manifests_dir=tmp_path / "raw" / "manifests", reviews_dir=tmp_path / "reviews",
        wiki_dir=tmp_path / "wiki", graph_db=tmp_path / "db" / "graph.sqlite", now=NOW)


def test_apply_archive_flips_manifest_page_and_graph(tmp_path):
    page = _setup_rendered_source(tmp_path, "src_000000000000000a")
    # seed a graph source node to mirror
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    conn = graph.connect(gdb)
    graph.upsert_node(conn, node_id="src_000000000000000a", node_type="source", slug="src_000000000000000a", status="active")
    conn.close()
    _approve_archive(tmp_path, "src_000000000000000a")
    raw_before = (tmp_path / "raw" / "inbox" / "src_000000000000000a.md").read_bytes()

    res = _apply(tmp_path)
    assert res["applied"] == 1 and res["changed_pages"] == ["Sources/src_000000000000000a.md"]
    md = tmp_path / "raw" / "manifests"
    assert manifests.get_status(manifests.load_manifest(md, "src_000000000000000a")) == "archive_candidate"
    assert parse_frontmatter(page.read_text())["status"] == "archive_candidate"
    conn = graph.connect(gdb)
    assert graph.get_node(conn, "src_000000000000000a")["status"] == "archive_candidate"
    conn.close()
    # raw bytes untouched (the load-bearing invariant)
    assert (tmp_path / "raw" / "inbox" / "src_000000000000000a.md").read_bytes() == raw_before


def test_apply_archive_idempotent_noop_when_already_archived(tmp_path):
    _setup_rendered_source(tmp_path, "src_000000000000000a")
    _approve_archive(tmp_path, "src_000000000000000a")
    _apply(tmp_path)
    res2 = _apply(tmp_path)            # already archive_candidate -> no-op
    assert res2["applied"] == 0 and res2["changed_pages"] == []


def test_apply_archive_source_missing_skipped(tmp_path):
    (tmp_path / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    _approve_archive(tmp_path, "src_0000000000000404")
    res = _apply(tmp_path)
    assert res["applied"] == 0
    assert res["skipped"] == [{"review_id": "rev_arch", "reason": "source_missing"}]


def test_archive_candidate_excluded_from_default_retrieval_vocabulary():
    # the retrieval filter excludes archive_candidate by default but an explicit ask can include it
    assert "archive_candidate" not in search.RETENTION_DEFAULT_STATUSES
    assert "active" in search.RETENTION_DEFAULT_STATUSES


# --- API --------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


def test_api_stale_check(client, tmp_path):
    _write_manifest(tmp_path, "src_000000000000001d", modified_at=OLD)
    body = client.post("/jobs/stale-check").json()
    assert body["considered"] == 1 and body["archive_candidates_filed"] == 1
    assert len(body["archive_review_items_filed"]) == 1


def test_api_apply_archives_source(client, tmp_path):
    _setup_rendered_source(tmp_path, "src_000000000000000a")
    _approve_archive(tmp_path, "src_000000000000000a")
    raw_before = (tmp_path / "raw" / "inbox" / "src_000000000000000a.md").read_bytes()
    body = client.post("/reviews/apply").json()
    assert body["summary"]["archives"]["applied"] == 1
    page = tmp_path / "wiki" / "Sources" / "src_000000000000000a.md"
    assert parse_frontmatter(page.read_text())["status"] == "archive_candidate"
    assert (tmp_path / "raw" / "inbox" / "src_000000000000000a.md").read_bytes() == raw_before  # raw untouched


# --- review-round fixes ----------------------------------------------------


def _approve_raw(tmp_path, rid, item):
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps(item), encoding="utf-8")


def test_preview_uses_manifest_authority_over_page_drift(tmp_path):
    # manifest says active, but the page mirror drifted to archive_candidate
    _write_manifest(tmp_path, "src_000000000000000a", status="active")
    page = tmp_path / "wiki" / "Sources" / "src_000000000000000a.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text('---\ntype: source\nsource_id: "src_000000000000000a"\nstatus: archive_candidate\n---\n',
                    encoding="utf-8")
    _approve_archive(tmp_path, "src_000000000000000a")
    res = review_read.get_review(tmp_path / "reviews", "rev_arch", wiki_dir=tmp_path / "wiki",
                                 manifests_dir=tmp_path / "raw" / "manifests")
    ap = res["preview"]["apply"]
    assert ap["effect_status"] == "pending_apply"          # manifest active -> NOT effected
    assert "page_manifest_drift" in ap["warnings"]
    assert res["preview"]["current_status"] == "active"    # authority is the manifest


def test_executor_skips_unexpected_to_status(tmp_path):
    _setup_rendered_source(tmp_path, "src_000000000000000a")
    _approve_raw(tmp_path, "rev_bad", {
        "review_id": "rev_bad", "type": "archive_source", "status": "approved",
        "subject": {"source_id": "src_000000000000000a"}, "proposal": {"to_status": "deleted"}})
    res = _apply(tmp_path)
    assert res["applied"] == 0
    assert res["skipped"] == [{"review_id": "rev_bad", "reason": "unexpected_to_status"}]
    md = tmp_path / "raw" / "manifests"
    assert manifests.get_status(manifests.load_manifest(md, "src_000000000000000a")) == "active"  # untouched


def test_executor_skips_non_approved_and_missing_subject(tmp_path):
    _setup_rendered_source(tmp_path, "src_000000000000000a")
    _approve_raw(tmp_path, "rev_rej", {
        "review_id": "rev_rej", "type": "archive_source", "status": "rejected",
        "subject": {"source_id": "src_000000000000000a"}, "proposal": {"to_status": "archive_candidate"}})
    _approve_raw(tmp_path, "rev_nosub", {
        "review_id": "rev_nosub", "type": "archive_source", "status": "approved",
        "subject": {}, "proposal": {"to_status": "archive_candidate"}})
    res = _apply(tmp_path)
    reasons = {s["review_id"]: s["reason"] for s in res["skipped"]}
    assert reasons["rev_rej"] == "not_approved" and reasons["rev_nosub"] == "missing_subject"
    assert res["applied"] == 0


def test_stale_check_file_review_items_false_detects_but_does_not_file(tmp_path):
    _write_manifest(tmp_path, "src_000000000000001d", modified_at=OLD)
    res = retention.run_stale_check(tmp_path, record_job=False, now=NOW, file_review_items=False)
    assert res["archive_candidates"] == 1 and res["archive_candidates_filed"] == 0
    assert not (tmp_path / "reviews" / "pending").exists() or _pending(tmp_path, "archive_source") == []


def test_source_status_vocabulary_round_trips(tmp_path):
    # every manifest source status renders a Source page the validator accepts
    import importlib
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "scripts"))
    validate_wiki = importlib.import_module("validate_wiki")
    for status in manifests.SOURCE_STATUSES:
        m = _write_manifest(tmp_path, "src_0000000000000ec0", status=status)
        page = render_source_page(_TEMPLATE, m, "# t\n\nbody.\n", summary_max=320, summary_min=40)
        assert parse_frontmatter(page)["status"] in validate_wiki._VALID_STATUS, status
    assert manifests.get_status({"status": "deprecated_candidate"}) == "deprecated_candidate"


# --- API: schema-drift behavior + log --------------------------------------


def _drift_graph(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    conn = graph.connect(gdb)
    conn.execute("PRAGMA user_version = 999")  # force a schema mismatch
    conn.commit()
    conn.close()


def test_api_archive_only_proceeds_on_schema_drift(client, tmp_path):
    _setup_rendered_source(tmp_path, "src_000000000000000a")
    _approve_archive(tmp_path, "src_000000000000000a")
    _drift_graph(tmp_path)
    body = client.post("/reviews/apply").json()
    assert body["summary"]["archives"]["applied"] == 1   # archive proceeds despite graph drift
    assert parse_frontmatter((tmp_path / "wiki" / "Sources" / "src_000000000000000a.md").read_text())["status"] \
        == "archive_candidate"
    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        assert graph.schema_version(conn) == 999  # apply must not reinitialize a mismatched graph
    finally:
        conn.close()


def test_api_graph_required_still_503_on_schema_drift(client, tmp_path):
    _drift_graph(tmp_path)
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / "rev_p.json").write_text(json.dumps({
        "review_id": "rev_p", "type": "promote_candidate_node", "status": "approved",
        "subject": {"node_id": "cpt_1"}, "proposal": {"to_status": "active"}, "context": {}}),
        encoding="utf-8")
    assert client.post("/reviews/apply").status_code == 503


def test_api_stale_check_appends_log(client, tmp_path):
    _write_manifest(tmp_path, "src_000000000000001d", modified_at=OLD)
    client.post("/jobs/stale-check")
    assert "stale-check:" in (tmp_path / "wiki" / "log.md").read_text(encoding="utf-8")


# --- full retrieval integration --------------------------------------------


def _write_chunk(tmp_path, sid, text):
    p = tmp_path / "normalized" / "chunks" / f"{sid}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "chunk_id": f"{sid}::0000", "source_id": sid, "ordinal": 0, "kind": "prose",
        "heading_path": [], "section": None, "text": text, "char_start": 0, "char_end": len(text),
        "page": 1, "page_end": 1, "table_reference": None, "sheet_reference": None}) + "\n",
        encoding="utf-8")


def test_archive_excluded_from_default_search_but_found_with_explicit_status(tmp_path):
    sid = "src_0000000000000a11"
    _setup_rendered_source(tmp_path, sid, modified_at=OLD)
    _write_chunk(tmp_path, sid, "Quantum revenue strategy for the enterprise.")
    keyword_index.reindex(tmp_path, force=True)
    kpath = tmp_path / "indexes" / "keyword" / "keyword.sqlite"

    def _hits(statuses):
        conn = keyword_index.connect(kpath)
        try:
            return search.search_evidence(conn, "quantum", source_id=None,
                                          source_statuses=statuses, prefusion_limit=10, limit=10)
        finally:
            conn.close()

    assert any(h["source_id"] == sid for h in _hits(("active", "deprecated_candidate")))  # visible

    _approve_archive(tmp_path, sid)
    _apply(tmp_path)                              # archive -> archive_candidate
    keyword_index.reindex(tmp_path, force=True)   # refresh nav status
    assert not any(h["source_id"] == sid for h in _hits(("active", "deprecated_candidate")))  # excluded
    assert any(h["source_id"] == sid for h in _hits(("archive_candidate",)))  # explicit ask finds it


# --- 7-3: reindex / cache-purge / no-daemon --------------------------------

import sqlite3  # noqa: E402
import threading  # noqa: E402


def _seed_cache(tmp_path, *, rows, created_at):
    """Write a minimal response_cache db with `rows` entries at `created_at`."""
    cdb = tmp_path / "db" / "llm_cache.sqlite"
    cdb.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cdb)
    conn.execute("CREATE TABLE response_cache (cache_key TEXT PRIMARY KEY, provider TEXT, model_id TEXT, "
                 "schema_version TEXT, prompt_version TEXT, response_json TEXT NOT NULL, created_at TEXT)")
    for i in range(rows):
        conn.execute("INSERT INTO response_cache VALUES (?,?,?,?,?,?,?)",
                     (f"k{i}", "anthropic", "m", "v", "p", '{"big":"secret-payload"}', created_at))
    conn.commit()
    conn.close()
    return cdb


def test_reindex_records_job_and_logs_no_vector(tmp_path):
    res = retention.run_reindex(tmp_path)  # no scripts/ in tmp_path -> index_rebuilt False, no warning
    assert res["status"] == "succeeded" and res["keyword_reindexed"] is True
    assert res["warnings"] == []
    assert "reindex:" in (tmp_path / "wiki" / "log.md").read_text(encoding="utf-8")
    conn = main_module.db.connect(tmp_path / "db" / "jobs.sqlite")
    assert main_module.db.get_job(conn, res["job_id"])["job_type"] == "reindex"
    conn.close()
    # never builds a vector index
    assert not (tmp_path / "indexes" / "vector").exists()


def test_reindex_failure_records_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(retention.keyword_index, "reindex",
                        lambda root, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    res = retention.run_reindex(tmp_path)
    assert res["status"] == "failed" and res["keyword_reindexed"] is False
    assert any("keyword_reindex_failed" in w for w in res["warnings"])


def test_api_reindex(client, tmp_path):
    body = client.post("/jobs/reindex").json()
    assert body["status"] in ("succeeded", "failed") and "keyword_reindexed" in body


def test_cache_purge_candidate_over_ttl(tmp_path):
    _seed_cache(tmp_path, rows=3, created_at="2020-01-01T00:00:00+00:00")  # ancient -> over TTL
    res = retention.run_stale_check(tmp_path, record_job=False, now=NOW)
    cache = res["cache"]
    assert cache["cache_present"] and cache["entries"] == 3 and cache["over_bounds"] is True
    assert res["cache_purge_filed"] == 1
    item = json.loads((tmp_path / "reviews" / "pending" / next(
        p.name for p in (tmp_path / "reviews" / "pending").glob("*.json")
        if json.loads(p.read_text())["type"] == "purge_response_cache")).read_text())
    assert item["subject"] == {"scope": "response_cache"}
    # NEVER leaks cached responses / keys
    blob = json.dumps(item)
    assert "secret-payload" not in blob and "response_json" not in blob and "cache_key" not in blob


def test_cache_purge_idempotent_and_no_deletion(tmp_path):
    cdb = _seed_cache(tmp_path, rows=2, created_at="2020-01-01T00:00:00+00:00")
    retention.run_stale_check(tmp_path, record_job=False, now=NOW)
    res2 = retention.run_stale_check(tmp_path, record_job=False, now=NOW)
    assert res2["cache_purge_filed"] == 0 and res2["cache_purge_existing"] == 1
    # detection never deletes/mutates the cache
    conn = sqlite3.connect(cdb)
    assert conn.execute("SELECT COUNT(*) FROM response_cache").fetchone()[0] == 2
    conn.close()


def test_cache_within_bounds_no_candidate(tmp_path):
    _seed_cache(tmp_path, rows=1, created_at=NOW)  # fresh, tiny -> within bounds
    res = retention.run_stale_check(tmp_path, record_job=False, now=NOW)
    assert res["cache"]["over_bounds"] is False and res["cache_purge_filed"] == 0


def test_cache_missing_is_no_finding(tmp_path):
    res = retention.run_stale_check(tmp_path, record_job=False, now=NOW)  # no cache db
    assert res["cache"] == {"cache_present": False} and res["cache_purge_filed"] == 0


def test_cache_corrupt_is_warning_not_abort(tmp_path):
    cdb = tmp_path / "db" / "llm_cache.sqlite"
    cdb.parent.mkdir(parents=True, exist_ok=True)
    cdb.write_text("not a sqlite database", encoding="utf-8")
    _write_manifest(tmp_path, "src_000000000000001d", modified_at=OLD)  # a source candidate still detected
    res = retention.run_stale_check(tmp_path, record_job=False, now=NOW)
    assert res["cache"]["cache_readable"] is False
    assert "cache_unreadable" in res["warnings"]
    assert res["archive_candidates"] == 1  # source retention not aborted by the bad cache


def test_purge_response_cache_unapplied_by_reviews_apply(client, tmp_path):
    _seed_cache(tmp_path, rows=2, created_at="2020-01-01T00:00:00+00:00")
    client.post("/jobs/stale-check")
    # move the purge item to approved/ and apply
    pend = tmp_path / "reviews" / "pending"
    purge = next(p for p in pend.glob("*.json")
                 if json.loads(p.read_text())["type"] == "purge_response_cache")
    appr = tmp_path / "reviews" / "approved"
    appr.mkdir(parents=True, exist_ok=True)
    data = json.loads(purge.read_text())
    data["status"] = "approved"
    (appr / purge.name).write_text(json.dumps(data), encoding="utf-8")
    purge.unlink()
    body = client.post("/reviews/apply").json()
    # purge_response_cache has no executor -> reported as unapplied, never actioned
    assert {"type": "purge_response_cache", "count": 1, "reason": "no_executor_in_phase_6"} \
        in body["summary"]["unapplied"]
    conn = sqlite3.connect(tmp_path / "db" / "llm_cache.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM response_cache").fetchone()[0] == 2  # cache untouched
    conn.close()


def test_serving_app_starts_no_scheduler_or_daemon():
    # importing + serving the app must spin up no scheduler/cron worker (ADR-0036 no-daemon contract)
    with TestClient(main_module.app) as c:
        c.get("/health")
    suspicious = [t.name for t in threading.enumerate()
                  if any(k in t.name.lower() for k in ("schedul", "cron", "apschedul"))]
    assert suspicious == [], suspicious
    assert not any(hasattr(main_module, a)
                   for a in ("scheduler", "_scheduler", "background_scheduler", "cron"))


def test_cache_detection_skipped_when_policy_disabled(tmp_path):
    _seed_cache(tmp_path, rows=3, created_at="2020-01-01T00:00:00+00:00")  # would be over TTL
    pol = tmp_path / "policies" / "retention.yaml"
    pol.parent.mkdir(parents=True, exist_ok=True)
    pol.write_text("response_cache:\n  enabled: false\n  cache_ttl_days: 365\n  cache_max_mb: 2048\n",
                   encoding="utf-8")
    res = retention.run_stale_check(tmp_path, record_job=False, now=NOW, policy_path=pol)
    assert res["cache"] == {"enabled": False} and res["cache_purge_filed"] == 0


def test_reindex_warnings_persisted_to_job_row(tmp_path, monkeypatch):
    monkeypatch.setattr(retention.keyword_index, "reindex",
                        lambda root, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    res = retention.run_reindex(tmp_path)  # record_job=True by default
    conn = main_module.db.connect(tmp_path / "db" / "jobs.sqlite")
    job = main_module.db.get_job(conn, res["job_id"])
    conn.close()
    assert job["status"] == "failed"
    assert any("keyword_reindex_failed" in w for w in job["warnings"])


def test_stale_check_cache_warning_persisted_to_job_row(tmp_path):
    cdb = tmp_path / "db" / "llm_cache.sqlite"
    cdb.parent.mkdir(parents=True, exist_ok=True)
    cdb.write_text("not a sqlite database", encoding="utf-8")
    res = retention.run_stale_check(tmp_path, now=NOW)  # record_job=True
    conn = main_module.db.connect(tmp_path / "db" / "jobs.sqlite")
    job = main_module.db.get_job(conn, res["job_id"])
    conn.close()
    assert "cache_unreadable" in job["warnings"]


def test_apply_archive_rejects_invalid_subject_source_id(tmp_path):
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / "rev_bad.json").write_text(json.dumps({
        "review_id": "rev_bad", "type": "archive_source", "status": "approved",
        "subject": {"source_id": "../../etc/passwd"},
        "proposal": {"to_status": "archive_candidate"}, "context": {}}), encoding="utf-8")
    res = _apply(tmp_path)
    assert {"review_id": "rev_bad", "reason": "invalid_source_id"} in res["skipped"]
    md = tmp_path / "raw" / "manifests"
    assert (not md.exists()) or list(md.glob("**/*.json")) == []  # no traversal write
    assert not (tmp_path / "etc").exists()


def test_stale_check_reports_skipped_invalid_manifests(tmp_path):
    md = tmp_path / "raw" / "manifests"
    md.mkdir(parents=True)
    (md / "bad.json").write_text(json.dumps(
        {"source_id": "../../x", "modified_at": OLD}), encoding="utf-8")
    res = retention.run_stale_check(tmp_path, record_job=False, now=NOW)
    assert res["manifests_skipped_invalid"] == 1   # observable, not silently dropped
    assert res["archive_candidates"] == 0          # the tampered manifest drove nothing


def test_preview_archive_invalid_subject_is_tamper_signal(tmp_path):
    item = {"review_id": "r1", "type": "archive_source", "status": "pending",
            "subject": {"source_id": "../../etc/passwd"}, "proposal": {"reason": "x"}}
    out = review_read.preview_archive_source(
        item, gconn=None, wiki_dir=tmp_path / "wiki",
        manifests_dir=tmp_path / "raw" / "manifests")
    assert out["invalid_subject"] is True
    assert out["affected_paths"] == [] and out["node_ids"] == []  # no fake path
    assert out["apply"]["supported"] is False
