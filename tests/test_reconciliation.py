"""ADR-0066 part 3 slice 3: "awaiting reconciliation" after a provenance change.

Covers the digests each producer acknowledges (slice-3 grill Q3): captured from what the run decided
with, verified under the lock, acknowledged only by a completed run and per producer; Clear and
Clear-to-empty; display-only edits; the document-screen notice and the lint finding.

Synthesis is the acknowledging producer here: ADR-0069 slice 0 removed whole-vault contradiction
judging, so nothing acknowledges `detect_contradictions` until contradiction maintenance (slice 2(a));
its notice stays, saying that nothing re-checks them yet.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tests.test_contradictions as con_t  # noqa: E402
import tests.test_synthesis as syn_t  # noqa: E402
from app.backend import manifests  # noqa: E402
from app.workers import lint, reconciliation, synthesis  # noqa: E402

MD = "raw/manifests"


def _vault(tmp_path):
    syn_t._build(tmp_path)                 # a.md by Alice, b.md by Bob; an eligible synthesis topic
    return syn_t._sid(tmp_path, "a.md"), syn_t._sid(tmp_path, "b.md")


def _synth(tmp_path, adapter=None, *, cached=True, force=False):
    from app.llm.client import LLMClient
    adapter = adapter or syn_t.SynthAdapter()
    client = syn_t._client(tmp_path, adapter) if cached else LLMClient({"anthropic": adapter})
    return synthesis.generate_syntheses(
        tmp_path, client=client, model_ref=syn_t.HEAVY, jobs_db=tmp_path / "db" / "jobs.sqlite",
        rebuild_index=False, force=force)


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
    out = _synth(tmp_path)
    assert out["status"] == "succeeded" and set(out["independence_acknowledged"]) >= {a, b}
    waiting = _awaiting(tmp_path)
    assert "generate_synthesis" not in waiting                     # each producer on its own
    # ADR-0069 slice 0: nothing acknowledges contradictions until contradiction maintenance exists.
    assert set(waiting["detect_contradictions"]) == {a, b}


def test_a_document_without_provenance_or_history_gets_no_notice(tmp_path):
    con_t._build_graph(tmp_path, independent=False)
    assert _awaiting(tmp_path) == {}


def test_a_change_after_the_run_awaits_and_display_only_edits_do_not(tmp_path):
    a, _b = _vault(tmp_path)
    _synth(tmp_path)
    # Display-only: the title, show_page, order and a spelling variant leave the digest alone.
    _confirm(tmp_path, a, _a("alice", show=True), title="Another Title")
    assert "generate_synthesis" not in _awaiting(tmp_path, a)
    _confirm(tmp_path, a, _a("Alice", "Carol"))
    assert _awaiting(tmp_path, a)["generate_synthesis"] == [a]


def test_a_provenance_change_during_the_run_stays_awaiting(tmp_path):
    a, b = _vault(tmp_path)

    class Meanwhile(syn_t.SynthAdapter):
        def parse(self, *args, **kwargs):
            _confirm(tmp_path, a, _a("Alice", "Dave"))           # lands during the model call
            return super().parse(*args, **kwargs)
    adapter = Meanwhile()
    out = _synth(tmp_path, adapter, cached=False)
    assert adapter.calls == 1
    assert a not in out["independence_acknowledged"] and b in out["independence_acknowledged"]
    assert _awaiting(tmp_path, a)["generate_synthesis"] == [a]


def test_a_partial_or_skipped_run_acknowledges_nothing(tmp_path):
    a, _b = _vault(tmp_path)
    _synth(tmp_path)
    _confirm(tmp_path, a, _a("Zed"))
    out = _synth(tmp_path, syn_t.SynthAdapter(available=False))      # no provider
    assert out["status"] == "skipped" and out["independence_acknowledged"] == {}
    assert _awaiting(tmp_path, a)["generate_synthesis"] == [a]
    assert reconciliation.verify(tmp_path / MD, {a: "x"}, "partial") == {}


def test_clear_and_clear_to_empty_are_changes(tmp_path):
    a, _b = _vault(tmp_path)
    manifests.set_provenance(tmp_path / MD, a, report_family="fam")
    _synth(tmp_path)
    manifests.clear_details(tmp_path / MD, a)                    # report_family remains
    assert _awaiting(tmp_path, a)["generate_synthesis"] == [a]
    _synth(tmp_path)
    assert "generate_synthesis" not in _awaiting(tmp_path, a)
    manifests.set_provenance(tmp_path / MD, a, report_family=None)   # the last field removed
    assert manifests.independence_digest(manifests.get_provenance(
        manifests.load_manifest(tmp_path / MD, a))) == manifests.independence_digest({})
    assert _awaiting(tmp_path, a)["generate_synthesis"] == [a]       # Clear-to-empty still waits


def test_lint_reports_without_failing(tmp_path):
    a, _b = _vault(tmp_path)
    findings = lint._check_awaiting_reconciliation(tmp_path / MD, tmp_path / "db" / "jobs.sqlite")
    by_producer = {(f["subject"], f["data"]["producer"]): f for f in findings}
    assert set(by_producer) >= {(a, "generate_synthesis"), (a, "detect_contradictions")}
    assert all(f["severity"] == "low" for f in findings)
    assert by_producer[(a, "generate_synthesis")]["data"]["remediation"] == "run generate_synthesis"
    # ADR-0069 slice 0: no command re-checks contradictions yet, and the finding says so.
    contra = by_producer[(a, "detect_contradictions")]
    assert contra["data"]["remediation"].startswith("none yet")
    assert "weren't re-checked" in contra["detail"]


def test_the_document_screen_names_the_pending_producer(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.backend import main as main_module
    from app.backend.config import get_settings
    a, _b = _vault(tmp_path)
    _synth(tmp_path)
    _confirm(tmp_path, a, _a("Alice", "Carol"))
    monkeypatch.setattr(main_module, "settings", get_settings(tmp_path))
    html = TestClient(main_module.app).get(f"/ui/reviews/sources/{a}").text
    assert "Synthesis hasn&#x27;t run since these details changed" in html \
        or "Synthesis hasn't run since these details changed" in html
    assert "Proposed contradictions involving this document weren" in html
    assert "nothing re-checks them yet" in html
    assert "Contradiction detection hasn" not in html               # no "run it" for a removed run


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


def test_a_contradiction_acknowledgement_from_before_slice_0_still_counts(tmp_path):
    # Job history from the removed whole-vault run keeps acknowledging what it reconciled.
    from app.backend import db
    a, b = _vault(tmp_path)
    digests = reconciliation.capture({m["source_id"]: manifests.get_provenance(m)
                                      for m in manifests.valid_manifests(tmp_path / MD)[0]})
    jobs = tmp_path / "db" / "jobs.sqlite"
    conn = db.connect(jobs)
    db.insert_job(conn, job_id="job_legacy", job_type="detect_contradictions", status="running",
                  created_at="2026-10-30T00:00:00Z", started_at="2026-10-30T00:00:00Z")
    db.update_job(conn, "job_legacy", status="succeeded", finished_at="2026-10-30T00:00:00Z",
                  metadata={"independence_acknowledged": digests})
    conn.close()
    assert "detect_contradictions" not in _awaiting(tmp_path)
    _confirm(tmp_path, a, _a("Alice", "Carol"))
    assert _awaiting(tmp_path)["detect_contradictions"] == [a]


def test_a_newer_run_omitting_a_document_never_erases_its_history(tmp_path):
    a, b = _vault(tmp_path)
    manifests.set_provenance(tmp_path / MD, a, report_family="fam")
    _synth(tmp_path)                                               # acknowledges a and b

    class ClearsMeanwhile(syn_t.SynthAdapter):
        def parse(self, *args, **kwargs):
            manifests.clear_details(tmp_path / MD, a)              # Clear-to-empty during the run
            manifests.set_provenance(tmp_path / MD, a, report_family=None)
            return super().parse(*args, **kwargs)
    adapter = ClearsMeanwhile()
    out = _synth(tmp_path, adapter, cached=False, force=True)      # the model is really asked
    assert adapter.calls == 1
    assert a not in out["independence_acknowledged"]               # the newer run left a out
    assert _awaiting(tmp_path, a)["generate_synthesis"] == [a]     # its earlier ack still counts
