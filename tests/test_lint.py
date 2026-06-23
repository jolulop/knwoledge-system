"""Phase 7 slice 7-1: the /jobs/lint maintenance pass (ADR-0036).

Key-free tests over app.workers.lint.run_lint + POST /jobs/lint — detect-and-propose health:
missing-raw → high-severity finding + missing_raw_source review item; under-supported active concept →
deprecate_wiki_page proposal; idempotent reruns; job recorded; log.md appended; lint-health-as-outcome
(failing is a 200 report, not an error); no path leak.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.backend import db, graph, review_read
from app.backend import main as main_module
from app.backend.config import get_settings
from app.workers import lint


def _manifest(tmp_path, sid, *, rel_path, exists, filename="doc.pdf"):
    """Write a manifest; create the raw file too when exists=True."""
    md = tmp_path / "raw" / "manifests"
    md.mkdir(parents=True, exist_ok=True)
    (md / f"{sid}.json").write_text(json.dumps({
        "source_id": sid, "relative_raw_path": rel_path, "original_filename": filename,
        "occurrences": [{"relative_path": rel_path, "filename": filename}],
    }), encoding="utf-8")
    if exists:
        p = tmp_path / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("raw bytes", encoding="utf-8")


def _graph(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    return gdb, graph.connect(gdb)


def _run(tmp_path, **kw):
    return lint.run_lint(tmp_path, **kw)


def _pending(tmp_path, rtype=None):
    items = list((tmp_path / "reviews" / "pending").glob("*.json"))
    if rtype:
        items = [p for p in items if json.loads(p.read_text())["type"] == rtype]
    return items


# --- missing raw -----------------------------------------------------------


def test_present_raw_is_clean(tmp_path):
    _manifest(tmp_path, "src_ok", rel_path="raw/inbox/doc.pdf", exists=True)
    res = _run(tmp_path)
    assert not any(f["check"] == "missing_raw" for f in res["findings"])


def test_missing_raw_is_high_finding_and_review_item(tmp_path):
    _manifest(tmp_path, "src_gone", rel_path="raw/inbox/gone.pdf", exists=False)
    res = _run(tmp_path)
    mr = [f for f in res["findings"] if f["check"] == "missing_raw"]
    assert mr and mr[0]["severity"] == "high" and mr[0]["subject"] == "src_gone"
    assert res["status"] == "failing"            # high-severity finding -> failing health
    items = _pending(tmp_path, "missing_raw_source")
    assert len(items) == 1
    assert json.loads(items[0].read_text())["subject"] == {"source_id": "src_gone"}


def test_missing_raw_idempotent_no_duplicate_items(tmp_path):
    _manifest(tmp_path, "src_gone", rel_path="raw/inbox/gone.pdf", exists=False)
    _run(tmp_path)
    _run(tmp_path)  # rerun
    assert len(_pending(tmp_path, "missing_raw_source")) == 1


def test_file_review_items_false_reports_without_filing(tmp_path):
    _manifest(tmp_path, "src_gone", rel_path="raw/inbox/gone.pdf", exists=False)
    res = _run(tmp_path, file_review_items=False)
    assert any(f["check"] == "missing_raw" for f in res["findings"])
    assert res["review_items_filed"] == []
    assert _pending(tmp_path) == []


# --- under-supported concepts ----------------------------------------------


def test_under_supported_active_concept_proposes_deprecation(tmp_path):
    gdb, conn = _graph(tmp_path)
    # active concept with one mentioning source -> under-supported (<2)
    graph.upsert_node(conn, node_id="src_1", node_type="source", slug="src_1", status="active")
    graph.upsert_node(conn, node_id="cpt_x", node_type="concept", slug="thing", status="active")
    graph.upsert_assertion(conn, src_id="src_1", dst_id="cpt_x", edge_type="mentions",
                           asserted_by="llm", status="active")
    conn.close()
    res = _run(tmp_path, graph_db=gdb)
    us = [f for f in res["findings"] if f["check"] == "under_supported_concept"]
    assert us and us[0]["subject"] == "cpt_x" and us[0]["severity"] == "medium"
    items = _pending(tmp_path, "deprecate_wiki_page")
    assert len(items) == 1
    subj = json.loads(items[0].read_text())["subject"]
    assert subj == {"node_id": "cpt_x", "page": "Concepts/thing.md"}


def test_orphan_concept_zero_sources_is_high(tmp_path):
    gdb, conn = _graph(tmp_path)
    graph.upsert_node(conn, node_id="cpt_orphan", node_type="concept", slug="orphan", status="active")
    conn.close()
    res = _run(tmp_path, graph_db=gdb)
    us = [f for f in res["findings"] if f["check"] == "under_supported_concept"]
    assert us[0]["severity"] == "high" and res["status"] == "failing"


def test_well_supported_concept_is_not_flagged(tmp_path):
    gdb, conn = _graph(tmp_path)
    for s in ("src_1", "src_2"):
        graph.upsert_node(conn, node_id=s, node_type="source", slug=s, status="active")
    graph.upsert_node(conn, node_id="cpt_x", node_type="concept", slug="thing", status="active")
    for s in ("src_1", "src_2"):
        graph.upsert_assertion(conn, src_id=s, dst_id="cpt_x", edge_type="mentions",
                               asserted_by="llm", status="active")
    conn.close()
    res = _run(tmp_path, graph_db=gdb)
    assert not any(f["check"] == "under_supported_concept" for f in res["findings"])


def test_graph_absent_skips_semantic_checks(tmp_path):
    res = _run(tmp_path)  # no graph db created
    assert res["graph_available"] is False
    assert any(f["check"] == "graph_unavailable" for f in res["findings"])


# --- job record / log / health ---------------------------------------------


def test_records_job_and_appends_log(tmp_path):
    _manifest(tmp_path, "src_ok", rel_path="raw/inbox/doc.pdf", exists=True)
    res = _run(tmp_path)
    conn = db.connect(tmp_path / "db" / "jobs.sqlite")
    job = db.get_job(conn, res["job_id"])
    conn.close()
    assert job["job_type"] == "lint" and job["status"] == "succeeded"
    assert "lint:" in (tmp_path / "wiki" / "log.md").read_text(encoding="utf-8")


def test_clean_vault_is_healthy(tmp_path):
    _manifest(tmp_path, "src_ok", rel_path="raw/inbox/doc.pdf", exists=True)
    gdb, conn = _graph(tmp_path)  # empty graph, no nodes
    conn.close()
    res = _run(tmp_path, graph_db=gdb)
    assert res["status"] == "healthy" and res["validators_ok"] is True


# --- API --------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


def test_api_lint_failing_is_200_not_error(client, tmp_path):
    _manifest(tmp_path, "src_gone", rel_path="raw/inbox/gone.pdf", exists=False)
    resp = client.post("/jobs/lint")
    assert resp.status_code == 200                # failing health is a 200 report, not an error
    body = resp.json()
    assert body["status"] == "failing"
    assert body["by_check"].get("missing_raw") == 1
    assert len(body["review_items_filed"]) == 1
    assert str(tmp_path) not in resp.text         # no server path leak


def test_api_lint_validator_failure_marks_failing(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module.lint, "run_validators", lambda root: [
        {"name": "validate_projection.py", "returncode": 1, "stdout_tail": "boom", "stderr_tail": ""}])
    body = client.post("/jobs/lint").json()
    assert body["validators_ok"] is False and body["status"] == "failing"


# --- path safety (the blocking fix) ----------------------------------------


def _manifest_raw(tmp_path, sid, rel_path):
    """Write a manifest whose only raw path is `rel_path` (verbatim, may be hostile)."""
    md = tmp_path / "raw" / "manifests"
    md.mkdir(parents=True, exist_ok=True)
    (md / f"{sid}.json").write_text(json.dumps({
        "source_id": sid, "relative_raw_path": rel_path, "original_filename": "doc.pdf",
        "occurrences": [{"relative_path": rel_path}]}), encoding="utf-8")


def test_absolute_path_is_not_probed_and_not_leaked(tmp_path):
    # an absolute manifest path pointing at a real file OUTSIDE raw/ must not count as present
    secret = tmp_path / "outside.txt"
    secret.write_text("not a raw source", encoding="utf-8")
    _manifest_raw(tmp_path, "src_abs", str(secret))
    res = _run(tmp_path)
    inv = [f for f in res["findings"] if f["check"] == "invalid_raw_path"]
    assert inv and inv[0]["subject"] == "src_abs" and res["status"] == "failing"
    item = _pending(tmp_path, "missing_raw_source")[0].read_text()
    assert str(secret) not in item                 # absolute path never leaks into the payload
    assert json.loads(item)["proposal"]["invalid_path_count"] == 1


def test_dotdot_escape_is_invalid(tmp_path):
    _manifest_raw(tmp_path, "src_esc", "raw/../../etc/passwd")
    res = _run(tmp_path)
    assert any(f["check"] == "invalid_raw_path" for f in res["findings"])
    assert "etc/passwd" not in json.dumps(res["findings"])  # malformed path not echoed in findings


def test_directory_at_raw_path_is_not_a_file(tmp_path):
    # a directory where a raw file should be must NOT count as present (is_file, not exists)
    (tmp_path / "raw" / "inbox" / "doc.pdf").mkdir(parents=True)
    _manifest_raw(tmp_path, "src_dir", "raw/inbox/doc.pdf")
    res = _run(tmp_path)
    assert any(f["check"] == "missing_raw" for f in res["findings"])


def test_safe_present_file_under_raw_is_clean(tmp_path):
    _manifest(tmp_path, "src_ok", rel_path="raw/inbox/doc.pdf", exists=True)
    res = _run(tmp_path)
    assert not any(f["check"] in ("missing_raw", "invalid_raw_path") for f in res["findings"])


# --- rerun semantics / degraded --------------------------------------------


def test_rerun_reports_existing_not_newly_filed(tmp_path):
    _manifest(tmp_path, "src_gone", rel_path="raw/inbox/gone.pdf", exists=False)
    first = _run(tmp_path)
    assert len(first["review_items_filed"]) == 1 and first["review_items_existing"] == []
    second = _run(tmp_path)
    assert second["review_items_filed"] == [] and len(second["review_items_existing"]) == 1


def test_degraded_when_graph_absent_and_otherwise_clean(tmp_path):
    _manifest(tmp_path, "src_ok", rel_path="raw/inbox/doc.pdf", exists=True)
    res = _run(tmp_path)  # no graph db -> semantic coverage incomplete, nothing failing
    assert res["graph_available"] is False and res["status"] == "degraded"


def test_missing_raw_source_preview_is_record_only(tmp_path):
    _manifest(tmp_path, "src_gone", rel_path="raw/inbox/gone.pdf", exists=False)
    res = _run(tmp_path)
    rid = res["review_items_filed"][0]
    prev = review_read.get_review(tmp_path / "reviews", rid)["preview"]
    assert prev["apply"]["supported"] is False
    assert prev["apply"]["effect_status"] == review_read.APPLY_DEFERRED
    assert review_read.decision_apply_required("missing_raw_source", "approved") is False
