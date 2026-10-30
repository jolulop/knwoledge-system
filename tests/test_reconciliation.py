"""ADR-0066 part 3 slice 3: "awaiting reconciliation" after a provenance change.

Covers the digests each producer acknowledges (slice-3 grill Q3): captured from what the run decided
with, verified under the lock, acknowledged only by a completed run and per producer; Clear and
Clear-to-empty; display-only edits; the document-screen notice and the lint finding.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tests.test_contradictions as con_t  # noqa: E402
from app.backend import manifests  # noqa: E402
from app.workers import contradictions, lint, reconciliation  # noqa: E402

MD = "raw/manifests"


def _vault(tmp_path):
    con_t._build_graph(tmp_path)                       # a.md by Alice, b.md by Bob
    return con_t._sid(tmp_path, "a.md"), con_t._sid(tmp_path, "b.md")


def _detect(tmp_path, adapter=None, *, cached=True):
    from app.llm.client import LLMClient
    adapter = adapter or con_t.ContradictionAdapter()
    client = con_t._client(tmp_path, adapter) if cached else LLMClient({"anthropic": adapter})
    return contradictions.detect_contradictions(
        tmp_path, client=client, model_ref=con_t.HEAVY, jobs_db=tmp_path / "db" / "jobs.sqlite",
        rebuild_index=False)


def _awaiting(tmp_path, sid=None):
    return reconciliation.awaiting(tmp_path / MD, tmp_path / "db" / "jobs.sqlite", source_id=sid)


def _a(*names, show=False):
    return [{"name": n, "show_page": show} for n in names]


def _confirm(tmp_path, sid, authors, **kw):
    manifests.confirm_details(tmp_path / MD, sid, title=kw.get("title", "T"),
                              authors=authors, publisher=kw.get("publisher"),
                              baseline="b" * 16, now="2026-10-30T00:00:00Z")


def test_documents_with_provenance_await_until_a_completed_run(tmp_path):
    a, b = _vault(tmp_path)
    assert set(_awaiting(tmp_path)) == {"generate_synthesis", "detect_contradictions"}
    out = _detect(tmp_path)
    assert out["status"] == "succeeded" and set(out["independence_acknowledged"]) >= {a, b}
    waiting = _awaiting(tmp_path)
    assert "detect_contradictions" not in waiting                  # each producer on its own
    assert set(waiting["generate_synthesis"]) == {a, b}


def test_a_document_without_provenance_or_history_gets_no_notice(tmp_path):
    con_t._build_graph(tmp_path, independent=False)
    assert _awaiting(tmp_path) == {}


def test_a_change_after_the_run_awaits_and_display_only_edits_do_not(tmp_path):
    a, _b = _vault(tmp_path)
    _detect(tmp_path)
    # Display-only: the title, show_page, order and a spelling variant leave the digest alone.
    _confirm(tmp_path, a, _a("alice", show=True), title="Another Title")
    assert "detect_contradictions" not in _awaiting(tmp_path, a)
    _confirm(tmp_path, a, _a("Alice", "Carol"))
    assert _awaiting(tmp_path, a)["detect_contradictions"] == [a]


def test_a_provenance_change_during_the_run_stays_awaiting(tmp_path):
    a, b = _vault(tmp_path)

    class Meanwhile(con_t.ContradictionAdapter):
        def parse(self, *args, **kwargs):
            _confirm(tmp_path, a, _a("Alice", "Dave"))           # lands during the model call
            return super().parse(*args, **kwargs)
    out = _detect(tmp_path, Meanwhile(), cached=False)
    assert a not in out["independence_acknowledged"] and b in out["independence_acknowledged"]
    assert _awaiting(tmp_path, a)["detect_contradictions"] == [a]


def test_a_partial_or_skipped_run_acknowledges_nothing(tmp_path):
    a, _b = _vault(tmp_path)
    _detect(tmp_path)
    _confirm(tmp_path, a, _a("Zed"))
    out = _detect(tmp_path, con_t.ContradictionAdapter(available=False))      # no provider
    assert out["status"] == "skipped" and out["independence_acknowledged"] == {}
    assert _awaiting(tmp_path, a)["detect_contradictions"] == [a]
    assert reconciliation.verify(tmp_path / MD, {a: "x"}, "partial") == {}


def test_clear_and_clear_to_empty_are_changes(tmp_path):
    a, _b = _vault(tmp_path)
    manifests.set_provenance(tmp_path / MD, a, report_family="fam")
    _detect(tmp_path)
    manifests.clear_details(tmp_path / MD, a)                    # report_family remains
    assert _awaiting(tmp_path, a)["detect_contradictions"] == [a]
    _detect(tmp_path)
    assert "detect_contradictions" not in _awaiting(tmp_path, a)
    manifests.set_provenance(tmp_path / MD, a, report_family=None)   # the last field removed
    assert manifests.independence_digest(manifests.get_provenance(
        manifests.load_manifest(tmp_path / MD, a))) == manifests.independence_digest({})
    assert _awaiting(tmp_path, a)["detect_contradictions"] == [a]    # Clear-to-empty still waits


def test_lint_reports_without_failing(tmp_path):
    a, _b = _vault(tmp_path)
    findings = lint._check_awaiting_reconciliation(tmp_path / MD, tmp_path / "db" / "jobs.sqlite")
    assert {(f["subject"], f["data"]["producer"]) for f in findings} >= {
        (a, "generate_synthesis"), (a, "detect_contradictions")}
    assert all(f["severity"] == "low" for f in findings)


def test_the_document_screen_names_the_pending_producer(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.backend import main as main_module
    from app.backend.config import get_settings
    a, _b = _vault(tmp_path)
    _detect(tmp_path)
    _confirm(tmp_path, a, _a("Alice", "Carol"))
    monkeypatch.setattr(main_module, "settings", get_settings(tmp_path))
    html = TestClient(main_module.app).get(f"/ui/reviews/sources/{a}").text
    assert "Contradiction detection hasn't run since these details changed" in html
    assert "Synthesis hasn't run since these details changed" in html


def test_the_newest_acknowledgement_wins_within_the_same_second(tmp_path):
    from app.backend import db
    jobs = tmp_path / "db" / "jobs.sqlite"
    db.init_db(jobs)
    conn = db.connect(jobs)
    for job_id, digest in (("job_ffff", "old"), ("job_0000", "new")):    # ids sort the other way
        db.insert_job(conn, job_id=job_id, job_type="detect_contradictions", status="running",
                      created_at="2026-10-30T00:00:00Z", started_at="2026-10-30T00:00:00Z")
        db.update_job(conn, job_id, status="succeeded", finished_at="2026-10-30T00:00:00Z",
                      metadata={"independence_acknowledged": {"src_x": digest}})
    conn.close()
    assert reconciliation._acknowledged(jobs, "detect_contradictions") == {"src_x": "new"}


def test_synthesis_acknowledges_on_its_own(tmp_path):
    import tests.test_synthesis as syn_t
    from app.workers import synthesis
    a, b = _vault(tmp_path)
    out = synthesis.generate_syntheses(
        tmp_path, client=syn_t._client(tmp_path, syn_t.SynthAdapter()), model_ref=syn_t.HEAVY,
        jobs_db=tmp_path / "db" / "jobs.sqlite", rebuild_index=False)
    assert out["status"] == "succeeded" and {a, b} <= set(out["independence_acknowledged"])
    waiting = _awaiting(tmp_path)
    assert "generate_synthesis" not in waiting and set(waiting["detect_contradictions"]) == {a, b}


def test_a_newer_run_omitting_a_document_never_erases_its_history(tmp_path):
    a, b = _vault(tmp_path)
    manifests.set_provenance(tmp_path / MD, a, report_family="fam")
    _detect(tmp_path)                                              # acknowledges a and b

    class ClearsMeanwhile(con_t.ContradictionAdapter):
        def parse(self, *args, **kwargs):
            manifests.clear_details(tmp_path / MD, a)              # Clear-to-empty during the run
            manifests.set_provenance(tmp_path / MD, a, report_family=None)
            return super().parse(*args, **kwargs)
    _confirm(tmp_path, b, _a("Bob", "Eve"))                        # make the second run evaluate
    out = _detect(tmp_path, ClearsMeanwhile(), cached=False)      # the model is really asked
    assert a not in out["independence_acknowledged"]               # the newer run left a out
    assert _awaiting(tmp_path, a)["detect_contradictions"] == [a]  # its earlier ack still counts
