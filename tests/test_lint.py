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
    _manifest(tmp_path, "src_00000000000000c0", rel_path="raw/inbox/doc.pdf", exists=True)
    res = _run(tmp_path)
    assert not any(f["check"] == "missing_raw" for f in res["findings"])


def test_missing_raw_is_high_finding_and_review_item(tmp_path):
    _manifest(tmp_path, "src_000000000000609e", rel_path="raw/inbox/gone.pdf", exists=False)
    res = _run(tmp_path)
    mr = [f for f in res["findings"] if f["check"] == "missing_raw"]
    assert mr and mr[0]["severity"] == "high" and mr[0]["subject"] == "src_000000000000609e"
    assert res["status"] == "failing"            # high-severity finding -> failing health
    items = _pending(tmp_path, "missing_raw_source")
    assert len(items) == 1
    assert json.loads(items[0].read_text())["subject"] == {"source_id": "src_000000000000609e"}


def test_missing_raw_idempotent_no_duplicate_items(tmp_path):
    _manifest(tmp_path, "src_000000000000609e", rel_path="raw/inbox/gone.pdf", exists=False)
    _run(tmp_path)
    _run(tmp_path)  # rerun
    assert len(_pending(tmp_path, "missing_raw_source")) == 1


def test_file_review_items_false_reports_without_filing(tmp_path):
    _manifest(tmp_path, "src_000000000000609e", rel_path="raw/inbox/gone.pdf", exists=False)
    res = _run(tmp_path, file_review_items=False)
    assert any(f["check"] == "missing_raw" for f in res["findings"])
    assert res["review_items_filed"] == []
    assert _pending(tmp_path) == []


# --- under-supported concepts ----------------------------------------------


def test_under_supported_active_concept_proposes_deprecation(tmp_path):
    gdb, conn = _graph(tmp_path)
    # active concept with one mentioning source -> under-supported (<2)
    graph.upsert_node(conn, node_id="src_0000000000000101", node_type="source", slug="src_0000000000000101", status="active")
    graph.upsert_node(conn, node_id="cpt_x", node_type="concept", slug="thing", status="active")
    graph.upsert_assertion(conn, src_id="src_0000000000000101", dst_id="cpt_x", edge_type="mentions",
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
    for s in ("src_0000000000000101", "src_0000000000000102"):
        graph.upsert_node(conn, node_id=s, node_type="source", slug=s, status="active")
    graph.upsert_node(conn, node_id="cpt_x", node_type="concept", slug="thing", status="active")
    for s in ("src_0000000000000101", "src_0000000000000102"):
        graph.upsert_assertion(conn, src_id=s, dst_id="cpt_x", edge_type="mentions",
                               asserted_by="llm", status="active")
    conn.close()
    res = _run(tmp_path, graph_db=gdb)
    assert not any(f["check"] == "under_supported_concept" for f in res["findings"])


def test_archived_sources_do_not_count_as_live_support(tmp_path):
    gdb, conn = _graph(tmp_path)
    graph.upsert_node(conn, node_id="src_0000000000000101", node_type="source", slug="src_0000000000000101", status="active")
    graph.upsert_node(conn, node_id="src_00000000000000a4", node_type="source", slug="src_00000000000000a4",
                      status="archive_candidate")
    graph.upsert_node(conn, node_id="cpt_x", node_type="concept", slug="thing", status="active")
    for s in ("src_0000000000000101", "src_00000000000000a4"):
        graph.upsert_assertion(conn, src_id=s, dst_id="cpt_x", edge_type="mentions",
                               asserted_by="llm", status="active")
    conn.close()
    res = _run(tmp_path, graph_db=gdb)
    us = [f for f in res["findings"] if f["check"] == "under_supported_concept"]
    assert us and us[0]["subject"] == "cpt_x"
    assert "1 mentioning source" in us[0]["detail"]


def test_graph_absent_skips_semantic_checks(tmp_path):
    res = _run(tmp_path)  # no graph db created
    assert res["graph_available"] is False
    assert any(f["check"] == "graph_unavailable" for f in res["findings"])


# --- job record / log / health ---------------------------------------------


def test_records_job_and_appends_log(tmp_path):
    _manifest(tmp_path, "src_00000000000000c0", rel_path="raw/inbox/doc.pdf", exists=True)
    res = _run(tmp_path)
    conn = db.connect(tmp_path / "db" / "jobs.sqlite")
    job = db.get_job(conn, res["job_id"])
    conn.close()
    assert job["job_type"] == "lint" and job["status"] == "succeeded"
    assert "lint:" in (tmp_path / "wiki" / "log.md").read_text(encoding="utf-8")


def test_clean_vault_is_healthy(tmp_path):
    _manifest(tmp_path, "src_00000000000000c0", rel_path="raw/inbox/doc.pdf", exists=True)
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
    _manifest(tmp_path, "src_000000000000609e", rel_path="raw/inbox/gone.pdf", exists=False)
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
    _manifest_raw(tmp_path, "src_00000000000000ab", str(secret))
    res = _run(tmp_path)
    inv = [f for f in res["findings"] if f["check"] == "invalid_raw_path"]
    assert inv and inv[0]["subject"] == "src_00000000000000ab" and res["status"] == "failing"
    item = _pending(tmp_path, "missing_raw_source")[0].read_text()
    assert str(secret) not in item                 # absolute path never leaks into the payload
    assert json.loads(item)["proposal"]["invalid_path_count"] == 1


def test_dotdot_escape_is_invalid(tmp_path):
    _manifest_raw(tmp_path, "src_00000000000000e5", "raw/../../etc/passwd")
    res = _run(tmp_path)
    assert any(f["check"] == "invalid_raw_path" for f in res["findings"])
    assert "etc/passwd" not in json.dumps(res["findings"])  # malformed path not echoed in findings


def test_directory_at_raw_path_is_not_a_file(tmp_path):
    # a directory where a raw file should be must NOT count as present (is_file, not exists)
    (tmp_path / "raw" / "inbox" / "doc.pdf").mkdir(parents=True)
    _manifest_raw(tmp_path, "src_00000000000000d1", "raw/inbox/doc.pdf")
    res = _run(tmp_path)
    assert any(f["check"] == "missing_raw" for f in res["findings"])


def test_safe_present_file_under_raw_is_clean(tmp_path):
    _manifest(tmp_path, "src_00000000000000c0", rel_path="raw/inbox/doc.pdf", exists=True)
    res = _run(tmp_path)
    assert not any(f["check"] in ("missing_raw", "invalid_raw_path") for f in res["findings"])


# --- rerun semantics / degraded --------------------------------------------


def test_rerun_reports_existing_not_newly_filed(tmp_path):
    _manifest(tmp_path, "src_000000000000609e", rel_path="raw/inbox/gone.pdf", exists=False)
    first = _run(tmp_path)
    assert len(first["review_items_filed"]) == 1 and first["review_items_existing"] == []
    second = _run(tmp_path)
    assert second["review_items_filed"] == [] and len(second["review_items_existing"]) == 1


def test_degraded_when_graph_absent_and_otherwise_clean(tmp_path):
    _manifest(tmp_path, "src_00000000000000c0", rel_path="raw/inbox/doc.pdf", exists=True)
    res = _run(tmp_path)  # no graph db -> semantic coverage incomplete, nothing failing
    assert res["graph_available"] is False and res["status"] == "degraded"


def test_missing_raw_source_preview_is_record_only(tmp_path):
    _manifest(tmp_path, "src_000000000000609e", rel_path="raw/inbox/gone.pdf", exists=False)
    res = _run(tmp_path)
    rid = res["review_items_filed"][0]
    prev = review_read.get_review(tmp_path / "reviews", rid)["preview"]
    assert prev["apply"]["supported"] is False
    assert prev["apply"]["effect_status"] == review_read.APPLY_DEFERRED
    assert review_read.decision_apply_required("missing_raw_source", "approved") is False


# --- ADR-0037: summary_rot / stale_claim_citation quality heuristics --------

from app.workers.enrichment_artifact import artifact_fingerprint  # noqa: E402

QSID = "src_00000000000000fa"
QCID = "clm_0000000000000001"
QMODEL = "anthropic:claude-haiku-4-5"


def _summary_artifact(tmp_path, sid, md_text, model_ref, *, fingerprint=None):
    edir = tmp_path / "normalized" / "enrichment"
    mdir = tmp_path / "normalized" / "markdown"
    edir.mkdir(parents=True, exist_ok=True)
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / f"{sid}.md").write_text(md_text, encoding="utf-8")
    fp = fingerprint if fingerprint is not None else artifact_fingerprint(md_text, model_ref)
    (edir / f"{sid}.json").write_text(json.dumps({
        "source_id": sid, "generation_status": "enriched", "model_ref": model_ref,
        "input_fingerprint": fp, "summary": "a summary.",
    }), encoding="utf-8")


def _drift_md(tmp_path, sid, text):
    (tmp_path / "normalized" / "markdown" / f"{sid}.md").write_text(text, encoding="utf-8")


def _claim_artifact_and_edge(tmp_path, conn, sid, cid, md_text, quote, *, edge_status="active"):
    start = md_text.index(quote)
    end = start + len(quote)
    mdir = tmp_path / "normalized" / "markdown"
    edir = tmp_path / "normalized" / "enrichment"
    mdir.mkdir(parents=True, exist_ok=True)
    edir.mkdir(parents=True, exist_ok=True)
    (mdir / f"{sid}.md").write_text(md_text, encoding="utf-8")
    (edir / f"{sid}.claims.json").write_text(json.dumps({
        "source_id": sid, "generation_status": "enriched", "input_fingerprint": "fp",
        "claims": [{"claim_id": cid, "claim_text": "a claim",
                    "citation": {"source_id": sid, "char_start": start, "char_end": end, "quote": quote}}],
    }), encoding="utf-8")
    graph.upsert_node(conn, node_id=sid, node_type="source", slug=sid, status="active")
    graph.upsert_node(conn, node_id=cid, node_type="claim", slug=cid, status="active")
    graph.upsert_assertion(conn, src_id=cid, dst_id=sid, edge_type="derived_from", asserted_by="llm",
                           status=edge_status, evidence_source_id=sid,
                           evidence_char_start=start, evidence_char_end=end)
    return start, end


# summary_rot ---------------------------------------------------------------

def test_summary_rot_detected_on_content_drift(tmp_path):
    gdb, conn = _graph(tmp_path)  # empty graph -> graph_available, so status reflects only rot
    conn.close()
    _summary_artifact(tmp_path, QSID, "# H\n\nOriginal body.\n", QMODEL)
    _drift_md(tmp_path, QSID, "# H\n\nCompletely different body now.\n")
    res = _run(tmp_path, graph_db=gdb, summary_model_ref=QMODEL)
    rot = [f for f in res["findings"] if f["check"] == "summary_rot"]
    assert rot and rot[0]["subject"] == QSID and rot[0]["severity"] == "low"
    assert rot[0]["data"]["remediation"] == "rerun_enrich"
    assert res["status"] == "healthy"  # low severity never turns the board red


def test_fresh_summary_is_not_rot(tmp_path):
    gdb, conn = _graph(tmp_path)
    conn.close()
    _summary_artifact(tmp_path, QSID, "# H\n\nBody.\n", QMODEL)  # fingerprint matches current md
    res = _run(tmp_path, graph_db=gdb, summary_model_ref=QMODEL)
    assert not any(f["check"] == "summary_rot" for f in res["findings"])
    assert res["status"] == "healthy"


def test_model_bump_is_rot_not_failing(tmp_path):
    gdb, conn = _graph(tmp_path)
    conn.close()
    _summary_artifact(tmp_path, QSID, "# H\n\nBody.\n", QMODEL)
    res = _run(tmp_path, graph_db=gdb, summary_model_ref="anthropic:claude-sonnet-4-6")  # bumped
    assert any(f["check"] == "summary_rot" for f in res["findings"])  # model change -> rot
    assert res["status"] != "failing"


def test_stub_only_vault_is_healthy_no_rot(tmp_path):
    gdb, conn = _graph(tmp_path)  # no enrichment artifacts at all
    conn.close()
    res = _run(tmp_path, graph_db=gdb, summary_model_ref=QMODEL)
    assert not any(f["check"] in ("summary_rot", "summary_unverifiable") for f in res["findings"])
    assert res["status"] == "healthy"


def test_enriched_page_without_artifact_is_unverifiable_degraded(tmp_path):
    gdb, conn = _graph(tmp_path)
    conn.close()
    sdir = tmp_path / "wiki" / "Sources"
    sdir.mkdir(parents=True)
    (sdir / f"{QSID}.md").write_text("---\nsummary_status: enriched\n---\n\nbody\n", encoding="utf-8")
    res = _run(tmp_path, graph_db=gdb, summary_model_ref=QMODEL)
    assert any(f["check"] == "summary_unverifiable" for f in res["findings"])
    assert res["status"] == "degraded"


# stale_claim_citation ------------------------------------------------------

def test_stale_claim_detected_on_span_drift(tmp_path):
    gdb, conn = _graph(tmp_path)
    _claim_artifact_and_edge(tmp_path, conn, QSID, QCID, "# H\n\nThe sky is blue today.\n", "The sky is blue")
    conn.commit()
    conn.close()
    _drift_md(tmp_path, QSID, "# H\n\nXXX everything changed XXX.\n")  # stored quote no longer grounds
    res = _run(tmp_path, graph_db=gdb, summary_model_ref=QMODEL)
    stale = [f for f in res["findings"] if f["check"] == "stale_claim_citation"]
    assert stale and stale[0]["subject"] == QCID and stale[0]["severity"] == "medium"
    assert stale[0]["data"]["source_id"] == QSID
    assert stale[0]["data"]["remediation"] == "rerun_extract_claims"
    assert res["status"] != "failing"  # medium, not failing


def test_no_stale_when_quote_still_grounds(tmp_path):
    gdb, conn = _graph(tmp_path)
    _claim_artifact_and_edge(tmp_path, conn, QSID, QCID, "# H\n\nThe sky is blue today.\n", "The sky is blue")
    conn.commit()
    conn.close()
    res = _run(tmp_path, graph_db=gdb, summary_model_ref=QMODEL)
    assert not any(f["check"] == "stale_claim_citation" for f in res["findings"])
    assert res["status"] == "healthy"


def test_no_stale_when_edge_superseded(tmp_path):
    gdb, conn = _graph(tmp_path)
    _claim_artifact_and_edge(tmp_path, conn, QSID, QCID, "# H\n\nThe sky is blue today.\n",
                             "The sky is blue", edge_status="superseded")
    conn.commit()
    conn.close()
    _drift_md(tmp_path, QSID, "# H\n\nchanged.\n")  # would be stale, but no ACTIVE edge matches
    res = _run(tmp_path, graph_db=gdb, summary_model_ref=QMODEL)
    assert not any(f["check"] == "stale_claim_citation" for f in res["findings"])


def test_active_claim_evidence_without_artifact_is_unverifiable_degraded(tmp_path):
    gdb, conn = _graph(tmp_path)
    md = "# H\n\nThe sky is blue.\n"
    (tmp_path / "normalized" / "markdown").mkdir(parents=True, exist_ok=True)
    (tmp_path / "normalized" / "markdown" / f"{QSID}.md").write_text(md, encoding="utf-8")
    start = md.index("The sky is blue")
    graph.upsert_node(conn, node_id=QSID, node_type="source", slug=QSID, status="active")
    graph.upsert_node(conn, node_id=QCID, node_type="claim", slug=QCID, status="active")
    graph.upsert_assertion(conn, src_id=QCID, dst_id=QSID, edge_type="derived_from", asserted_by="llm",
                           status="active", evidence_source_id=QSID,
                           evidence_char_start=start, evidence_char_end=start + 15)
    conn.commit()
    conn.close()
    res = _run(tmp_path, graph_db=gdb, summary_model_ref=QMODEL)  # NO .claims.json artifact
    assert any(f["check"] == "claim_evidence_unverifiable" for f in res["findings"])
    assert res["status"] == "degraded"


# --- ADR-0037 round 2: positive enumeration, path safety, exact-citation coverage ---

def test_synthesis_and_concepts_artifacts_are_not_summaries(tmp_path):
    gdb, conn = _graph(tmp_path)
    conn.close()
    edir = tmp_path / "normalized" / "enrichment"
    edir.mkdir(parents=True)
    # both have generation_status: enriched but non-canonical stems -> must be ignored
    (edir / "cpt_thing.synthesis.json").write_text(json.dumps(
        {"generation_status": "enriched", "input_fingerprint": "x", "summary": "s"}), encoding="utf-8")
    (edir / f"{QSID}.concepts.json").write_text(json.dumps(
        {"source_id": QSID, "generation_status": "enriched", "input_fingerprint": "x"}), encoding="utf-8")
    res = _run(tmp_path, graph_db=gdb, summary_model_ref=QMODEL)
    assert not any(f["check"] in ("summary_rot", "summary_unverifiable") for f in res["findings"])
    assert res["status"] == "healthy"


def test_summary_artifact_internal_id_must_match_filename(tmp_path):
    gdb, conn = _graph(tmp_path)
    conn.close()
    edir = tmp_path / "normalized" / "enrichment"
    mdir = tmp_path / "normalized" / "markdown"
    edir.mkdir(parents=True)
    mdir.mkdir(parents=True)
    (mdir / f"{QSID}.md").write_text("# H\n\nbody.\n", encoding="utf-8")
    # filename is canonical, but internal source_id is a path-like spoof -> rejected, no read/leak
    (edir / f"{QSID}.json").write_text(json.dumps(
        {"source_id": "../../etc/x", "generation_status": "enriched", "input_fingerprint": "x"}),
        encoding="utf-8")
    res = _run(tmp_path, graph_db=gdb, summary_model_ref=QMODEL)
    assert not any(f["check"] in ("summary_rot", "summary_unverifiable") for f in res["findings"])
    assert not any("etc" in json.dumps(f.get("data", {})) for f in res["findings"])
    assert res["status"] == "healthy"


def test_noncanonical_edge_source_is_unverifiable_no_path_read(tmp_path):
    gdb, conn = _graph(tmp_path)
    bad = "../../etc/passwd"
    graph.upsert_node(conn, node_id=bad, node_type="source", slug="x", status="active")
    graph.upsert_node(conn, node_id=QCID, node_type="claim", slug=QCID, status="active")
    graph.upsert_assertion(conn, src_id=QCID, dst_id=bad, edge_type="derived_from", asserted_by="llm",
                           status="active", evidence_source_id=bad, evidence_char_start=0, evidence_char_end=5)
    conn.commit()
    conn.close()
    res = _run(tmp_path, graph_db=gdb, summary_model_ref=QMODEL)
    unv = [f for f in res["findings"] if f["check"] == "claim_evidence_unverifiable"]
    assert unv and res["status"] == "degraded"
    assert not any("etc" in json.dumps(f.get("data", {})) for f in res["findings"])  # no id leak


def test_active_edge_citation_absent_from_artifact_is_unverifiable(tmp_path):
    gdb, conn = _graph(tmp_path)
    md = "# H\n\nThe sky is blue today.\n"
    quote = "The sky is blue"
    start, end = md.index(quote), md.index(quote) + len(quote)
    mdir = tmp_path / "normalized" / "markdown"
    edir = tmp_path / "normalized" / "enrichment"
    mdir.mkdir(parents=True)
    edir.mkdir(parents=True)
    (mdir / f"{QSID}.md").write_text(md, encoding="utf-8")
    # artifact present + readable, but the citation has a WRONG span (off by 1) -> no exact match
    (edir / f"{QSID}.claims.json").write_text(json.dumps({
        "source_id": QSID, "claims": [{"claim_id": QCID, "claim_text": "c",
            "citation": {"source_id": QSID, "char_start": start + 1, "char_end": end, "quote": quote}}],
    }), encoding="utf-8")
    graph.upsert_node(conn, node_id=QSID, node_type="source", slug=QSID, status="active")
    graph.upsert_node(conn, node_id=QCID, node_type="claim", slug=QCID, status="active")
    graph.upsert_assertion(conn, src_id=QCID, dst_id=QSID, edge_type="derived_from", asserted_by="llm",
                           status="active", evidence_source_id=QSID,
                           evidence_char_start=start, evidence_char_end=end)
    conn.commit()
    conn.close()
    res = _run(tmp_path, graph_db=gdb, summary_model_ref=QMODEL)
    assert any(f["check"] == "claim_evidence_unverifiable" for f in res["findings"])  # not healthy/stale
    assert not any(f["check"] == "stale_claim_citation" for f in res["findings"])
    assert res["status"] == "degraded"


def test_jobs_lint_serializes_finding_data(client, tmp_path):
    gdb, conn = _graph(tmp_path)
    conn.close()
    _summary_artifact(tmp_path, QSID, "# H\n\nOriginal.\n", QMODEL)
    _drift_md(tmp_path, QSID, "# H\n\nChanged entirely.\n")
    body = client.post("/jobs/lint").json()
    rot = [f for f in body["findings"] if f["check"] == "summary_rot"]
    assert rot and rot[0]["data"]["remediation"] == "rerun_enrich"
    assert rot[0]["data"]["source_id"] == QSID


# --- ADR-0037 decision 6: synthesis_rot ------------------------------------

from app.workers import synthesis as _synth  # noqa: E402

SYN_TOPIC = {
    "node_id": "cpt_0000000000000001", "node_type": "concept", "slug": "thing", "title": "Thing",
    "claims": [
        {"claim_id": "clm_a", "claim_text": "A", "citations": [
            {"source_id": QSID, "char_start": 0, "char_end": 5}]},
        {"claim_id": "clm_b", "claim_text": "B", "citations": [
            {"source_id": "src_00000000000000bb", "char_start": 0, "char_end": 5}]},
    ],
    "disagreements": [],
}


def _synthesis_setup(tmp_path, conn, topic, stored_fp, *, syn_status="active"):
    tid = topic["node_id"]
    syn_id = _synth.synthesis_id(tid)
    graph.upsert_node(conn, node_id=syn_id, node_type="synthesis", slug=topic["slug"], status=syn_status)
    edir = tmp_path / "normalized" / "enrichment"
    edir.mkdir(parents=True, exist_ok=True)
    if stored_fp is not None:
        (edir / f"{tid}.synthesis.json").write_text(json.dumps(
            {"topic_node_id": tid, "generation_status": "enriched", "input_fingerprint": stored_fp}),
            encoding="utf-8")
    return syn_id


def test_synthesis_rot_detected_on_evidence_drift(tmp_path, monkeypatch):
    gdb, conn = _graph(tmp_path)
    monkeypatch.setattr(_synth, "eligible_topics", lambda *a, **k: [SYN_TOPIC])
    syn_id = _synthesis_setup(tmp_path, conn, SYN_TOPIC, "stale-fingerprint")  # stored != recomputed
    conn.commit()
    conn.close()
    res = _run(tmp_path, graph_db=gdb, synthesis_model_ref=QMODEL)
    rot = [f for f in res["findings"] if f["check"] == "synthesis_rot"]
    assert rot and rot[0]["subject"] == syn_id and rot[0]["severity"] == "low"
    assert rot[0]["data"]["topic_node_id"] == SYN_TOPIC["node_id"]
    assert rot[0]["data"]["remediation"] == "rerun_synthesis"
    assert res["status"] == "healthy"  # low, never failing


def test_fresh_synthesis_is_not_rot(tmp_path, monkeypatch):
    gdb, conn = _graph(tmp_path)
    monkeypatch.setattr(_synth, "eligible_topics", lambda *a, **k: [SYN_TOPIC])
    fresh = _synth._fingerprint(SYN_TOPIC, QMODEL)  # matches what lint will recompute
    _synthesis_setup(tmp_path, conn, SYN_TOPIC, fresh)
    conn.commit()
    conn.close()
    res = _run(tmp_path, graph_db=gdb, synthesis_model_ref=QMODEL)
    assert not any(f["check"] == "synthesis_rot" for f in res["findings"])
    assert res["status"] == "healthy"


def test_synthesis_model_bump_is_rot_not_failing(tmp_path, monkeypatch):
    gdb, conn = _graph(tmp_path)
    monkeypatch.setattr(_synth, "eligible_topics", lambda *a, **k: [SYN_TOPIC])
    stored = _synth._fingerprint(SYN_TOPIC, "anthropic:claude-opus-4-8")
    _synthesis_setup(tmp_path, conn, SYN_TOPIC, stored)
    conn.commit()
    conn.close()
    res = _run(tmp_path, graph_db=gdb, synthesis_model_ref="anthropic:claude-sonnet-4-6")  # bumped
    assert any(f["check"] == "synthesis_rot" for f in res["findings"])
    assert res["status"] != "failing"


def test_evidence_gone_topic_yields_no_synthesis_finding(tmp_path, monkeypatch):
    gdb, conn = _graph(tmp_path)
    monkeypatch.setattr(_synth, "eligible_topics", lambda *a, **k: [])  # topic no longer reconstructs
    # an active synthesis + artifact still on disk, but the topic is absent from eligible_topics
    _synthesis_setup(tmp_path, conn, SYN_TOPIC, "whatever")
    conn.commit()
    conn.close()
    res = _run(tmp_path, graph_db=gdb, synthesis_model_ref=QMODEL)
    assert not any(f["check"] in ("synthesis_rot", "synthesis_unverifiable") for f in res["findings"])
    assert res["status"] == "healthy"  # producer's deprecation flow owns evidence-gone


def test_active_synthesis_missing_artifact_is_unverifiable_degraded(tmp_path, monkeypatch):
    gdb, conn = _graph(tmp_path)
    monkeypatch.setattr(_synth, "eligible_topics", lambda *a, **k: [SYN_TOPIC])
    _synthesis_setup(tmp_path, conn, SYN_TOPIC, None)  # active synthesis node, NO artifact
    conn.commit()
    conn.close()
    res = _run(tmp_path, graph_db=gdb, synthesis_model_ref=QMODEL)
    unv = [f for f in res["findings"] if f["check"] == "synthesis_unverifiable"]
    assert unv and unv[0]["data"]["remediation"] == "rerun_synthesis"
    assert res["status"] == "degraded"


def test_no_active_synthesis_node_yields_nothing(tmp_path, monkeypatch):
    gdb, conn = _graph(tmp_path)
    monkeypatch.setattr(_synth, "eligible_topics", lambda *a, **k: [SYN_TOPIC])
    _synthesis_setup(tmp_path, conn, SYN_TOPIC, "x", syn_status="candidate")  # not active
    conn.commit()
    conn.close()
    res = _run(tmp_path, graph_db=gdb, synthesis_model_ref=QMODEL)
    assert not any(f["check"] in ("synthesis_rot", "synthesis_unverifiable") for f in res["findings"])
    assert res["status"] == "healthy"


def test_synthesis_path_like_topic_id_is_skipped_no_read(tmp_path, monkeypatch):
    # A path-like topic node id from a tampered graph must not drive a synthesis-artifact read.
    gdb, conn = _graph(tmp_path)
    bad_topic = dict(SYN_TOPIC, node_id="../../etc/passwd")
    monkeypatch.setattr(_synth, "eligible_topics", lambda *a, **k: [bad_topic])
    syn_id = _synth.synthesis_id("../../etc/passwd")  # canonical (hashed) -> active node exists
    graph.upsert_node(conn, node_id=syn_id, node_type="synthesis", slug="x", status="active")
    conn.commit()
    conn.close()
    (tmp_path / "evil.synthesis.json").write_text('{"input_fingerprint": "x"}', encoding="utf-8")
    res = _run(tmp_path, graph_db=gdb, synthesis_model_ref=QMODEL)
    assert not any(f["check"] in ("synthesis_rot", "synthesis_unverifiable") for f in res["findings"])
    assert not any("etc" in json.dumps(f.get("data", {})) for f in res["findings"])  # no leak


def test_operations_doc_synthesis_remediation_uses_force():
    text = (Path(__file__).resolve().parents[1] / "docs" / "Operations.md").read_text(encoding="utf-8")
    row = next(ln for ln in text.splitlines() if "`synthesis_rot`" in ln and "rerun_synthesis" in ln)
    assert "--force" in row  # the actionable command must use --force (normal run only reports)
