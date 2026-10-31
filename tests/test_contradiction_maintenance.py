"""ADR-0069 slice 2(a): model-free contradiction maintenance.

Retires exactly the **proposed** contradictions whose evidence changed (a claim without evidence, or
its wording/evidence differing from what was judged, even under the same claim id) or whose
documents are no longer independent; never an approved (active) one or a decision awaiting Apply.
Runs targeted after a Details save/clear and claim re-extraction, in full via
`detect_contradictions.py maintain`; lint and the Source screen report what is left from the
evidence itself, so a failed targeted run stays visible.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tests.test_contradictions as con_t  # noqa: E402
from app.backend import graph, manifests  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.workers import claims, contradictions, lint, reviews  # noqa: E402

MD = "raw/manifests"
QUOTE_A2 = "The Q3 revenue increased by ten percent. Margins improved."


def _dirs(tmp_path):
    return {"reviews_dir": tmp_path / "reviews", "claims_dir": tmp_path / "wiki" / "Claims",
            "markdown_dir": tmp_path / "normalized" / "markdown",
            "manifests_dir": tmp_path / MD}


def _vault(tmp_path):
    """Two independent documents (Alice, Bob), their two claims, and one proposed contradiction."""
    con_t._build_graph(tmp_path)
    a, b = con_t._propose(tmp_path)
    return a, b, con_t._sid(tmp_path, "a.md"), con_t._sid(tmp_path, "b.md")


def _rid(a, b):
    return reviews.review_id("resolve_contradiction", {"claim_a": a, "claim_b": b})


def _edge_status(tmp_path, a, b):
    conn = con_t._gconn(tmp_path)
    try:
        return sorted(r["status"] for r in graph.contradiction_between(conn, a, b))
    finally:
        conn.close()


def _check(tmp_path, **scope):
    conn = con_t._gconn(tmp_path)
    try:
        return contradictions.check_assertions(conn, **_dirs(tmp_path), **scope)
    finally:
        conn.close()


def _maintain(tmp_path):
    return contradictions.maintain(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")


def _withdrawn_notes(tmp_path, rid):
    return [json.loads(p.read_text())["note"]
            for p in (tmp_path / "reviews" / "audit_log").glob(f"{rid}-withdrawn-*.json")]


def _same_author(tmp_path, sid_b):
    manifests.set_provenance(tmp_path / MD, sid_b, author="Alice")    # b now shares a's author


# --- what is retired, and what never is ---------------------------------------------------------


def test_an_unchanged_proposal_is_kept(tmp_path):
    a, b, _sa, _sb = _vault(tmp_path)
    out = _maintain(tmp_path)
    assert out["assertions_checked"] == 1 and out["retired"] == [] and out["unverifiable"] == []
    assert _edge_status(tmp_path, a, b) == ["proposed"]
    assert (tmp_path / "reviews" / "pending" / f"{_rid(a, b)}.json").exists()


def test_documents_no_longer_independent_retire_the_proposal_with_an_audit(tmp_path):
    a, b, _sa, sb = _vault(tmp_path)
    _same_author(tmp_path, sb)
    out = _maintain(tmp_path)
    assert [(e["claim_a"], e["claim_b"], e["reason"]) for e in out["retired"]] == [
        (a, b, "not_independent")]
    assert _edge_status(tmp_path, a, b) == ["superseded"]
    assert not (tmp_path / "reviews" / "pending" / f"{_rid(a, b)}.json").exists()
    assert _withdrawn_notes(tmp_path, _rid(a, b)) == [
        "contradiction maintenance: its documents are no longer independent"]
    assert _maintain(tmp_path)["retired"] == []                       # idempotent


class NewQuoteAdapter(con_t.ClaimAdapter):
    """Same claim wording (so the same claim id), now grounded in a longer quote."""
    def parse(self, messages, schema, model_id, *, max_tokens):
        out = super().parse(messages, schema, model_id, max_tokens=max_tokens)
        for c in out["claims"]:
            if c["claim"] == con_t.CLAIM_A:
                c["quote"] = QUOTE_A2
        return out


def _re_extract(tmp_path, sid, adapter):
    return claims.extract_claims(
        tmp_path, client=LLMClient({"anthropic": adapter}), model_ref=con_t.CLAIM_MODEL,
        source_ids=[sid], force=True, jobs_db=tmp_path / "db" / "jobs.sqlite", rebuild_index=False)


def test_changed_evidence_under_the_same_claim_id_retires_it_on_re_extraction(tmp_path):
    a, b, sa, _sb = _vault(tmp_path)
    summary = _re_extract(tmp_path, sa, NewQuoteAdapter())
    assert set(graph.active_node_ids_of_type(con_t._gconn(tmp_path), "claim")) >= {a, b}
    assert summary["contradictions_retired"] == 1
    assert summary["contradiction_maintenance_errors"] == []
    assert _edge_status(tmp_path, a, b) == ["superseded"]
    assert _withdrawn_notes(tmp_path, _rid(a, b)) == [
        "contradiction maintenance: a claim's wording or evidence changed since it was judged"]


def test_re_extraction_with_the_same_evidence_keeps_the_proposal(tmp_path):
    a, b, sa, _sb = _vault(tmp_path)
    summary = claims.extract_claims(
        tmp_path, client=con_t._client(tmp_path, con_t.ClaimAdapter()), model_ref=con_t.CLAIM_MODEL,
        source_ids=[sa], force=True, jobs_db=tmp_path / "db" / "jobs.sqlite", rebuild_index=False)
    assert summary["contradictions_retired"] == 0
    assert _edge_status(tmp_path, a, b) == ["proposed"]


def test_the_fingerprint_is_wording_and_quotes_not_offsets():
    side = {"claim_id": "c", "claim_text": "T",
            "citations": [{"source_id": "s", "char_start": 3, "char_end": 9, "quote": "q"}]}
    moved = side | {"citations": [{"source_id": "s", "char_start": 30, "char_end": 36,
                                   "quote": "q"}]}
    assert contradictions.evidence_fingerprint(side) == contradictions.evidence_fingerprint(moved)
    for changed in (side | {"claim_text": "T2"},
                    side | {"citations": [{"source_id": "s", "quote": "q2"}]},
                    side | {"citations": [{"source_id": "s2", "quote": "q"}]}):
        assert contradictions.evidence_fingerprint(changed) != contradictions.evidence_fingerprint(side)


def test_recorded_judged_evidence_wins_over_the_shown_sides():
    item = {"proposal": {"sides": [{"claim_id": "c", "claim_text": "T", "citations": []}]},
            "context": {"judged_evidence": {"c": "abc"}}}
    assert contradictions.judged_fingerprints(item) == {"c": "abc"}
    assert contradictions.judged_fingerprints({"proposal": {}}) is None


def test_a_decision_awaiting_apply_is_left_for_apply(tmp_path):
    a, b, _sa, sb = _vault(tmp_path)
    reviews.resolve_review_item(tmp_path / "reviews", _rid(a, b), decision="approved",
                                decided_by="human")
    _same_author(tmp_path, sb)
    out = _maintain(tmp_path)
    assert out["retired"] == [] and out["awaiting_apply"] == 1
    assert _edge_status(tmp_path, a, b) == ["proposed"]
    con_t._apply(tmp_path)                                          # Apply executes the decision
    assert _edge_status(tmp_path, a, b) == ["active"]


def test_maintenance_never_reverses_an_approved_contradiction(tmp_path):
    a, b, _sa, sb = _vault(tmp_path)
    reviews.resolve_review_item(tmp_path / "reviews", _rid(a, b), decision="approved",
                                decided_by="human")
    con_t._apply(tmp_path)
    _same_author(tmp_path, sb)
    out = _maintain(tmp_path)
    assert out["assertions_checked"] == 0 and out["retired"] == []
    assert _edge_status(tmp_path, a, b) == ["active"]


def test_a_proposal_without_judged_evidence_is_reported_never_retired(tmp_path):
    a, b, _sa, _sb = _vault(tmp_path)
    path = tmp_path / "reviews" / "pending" / f"{_rid(a, b)}.json"
    item = json.loads(path.read_text())
    item["proposal"].pop("sides")
    path.write_text(json.dumps(item))
    out = _maintain(tmp_path)
    assert out["retired"] == []
    assert [e["reason"] for e in out["unverifiable"]] == ["no_judged_evidence"]
    assert _edge_status(tmp_path, a, b) == ["proposed"]


def test_targeted_scope_checks_only_the_named_documents_or_claims(tmp_path):
    a, b, sa, sb = _vault(tmp_path)
    _same_author(tmp_path, sb)
    assert _check(tmp_path, sources={"src_0000000000000000"})["checked"] == 0
    assert _check(tmp_path, claims_scope={"clm_0000000000000000"})["checked"] == 0
    for scope in ({"sources": {sa}}, {"sources": {sb}}, {"claims_scope": {b}}):
        assert [e["reason"] for e in _check(tmp_path, **scope)["stale"]] == ["not_independent"]


# --- targeted maintenance after Details, and its failure reporting ------------------------------


def _api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.backend import main as main_module
    from app.backend.config import get_settings
    monkeypatch.setattr(main_module, "settings", get_settings(tmp_path))
    return TestClient(main_module.app)


def _clear(tmp_path, api, sid):
    from app.backend import review_read
    view = review_read.source_review_view(
        tmp_path / "reviews", sid, graph_db=tmp_path / "db" / "graph.sqlite",
        wiki_dir=tmp_path / "wiki", manifests_dir=tmp_path / MD,
        enrichment_dir=tmp_path / "normalized" / "enrichment")
    return api.post(f"/ui/reviews/sources/{sid}/details/clear",
                    data={"details_baseline": view["details"]["baseline"]})


def test_clearing_details_retires_the_documents_proposals_in_the_same_save(tmp_path, monkeypatch):
    a, b, sa, _sb = _vault(tmp_path)
    resp = _clear(tmp_path, _api(tmp_path, monkeypatch), sa)       # a loses its author
    assert resp.status_code == 200
    assert "1 proposed contradiction(s) retired" in resp.text
    assert _edge_status(tmp_path, a, b) == ["superseded"]
    assert contradictions.SCREEN_NOTICE not in resp.text


def test_a_failed_targeted_run_is_reported_and_stays_visible(tmp_path, monkeypatch):
    a, b, sa, sb = _vault(tmp_path)
    api = _api(tmp_path, monkeypatch)

    def broken(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(contradictions, "maintain_assertions", broken)
    resp = _clear(tmp_path, api, sa)
    assert resp.status_code == 200                                  # the Details are saved
    assert manifests.get_provenance(manifests.load_manifest(tmp_path / MD, sa)).get("author") is None
    assert "OSError: disk full" in resp.text and "detect_contradictions.py maintain" in resp.text
    assert _edge_status(tmp_path, a, b) == ["proposed"]
    # Durable: the screen and lint report it from the evidence until maintenance runs.
    import html
    assert html.escape(contradictions.SCREEN_NOTICE) in resp.text
    screen = api.get(f"/ui/reviews/sources/{sb}").text
    assert html.escape(contradictions.SCREEN_NOTICE) in screen
    conn = con_t._gconn(tmp_path)
    try:
        found = lint._check_contradiction_maintenance(conn, **_dirs(tmp_path))
    finally:
        conn.close()
    assert [(f["check"], f["data"]["reason"], f["severity"]) for f in found] == [
        ("contradiction_maintenance_needed", "not_independent", "low")]
    assert "detect_contradictions.py maintain" in found[0]["data"]["remediation"]
    monkeypatch.undo()
    assert [e["reason"] for e in _maintain(tmp_path)["retired"]] == ["not_independent"]
    conn = con_t._gconn(tmp_path)
    try:
        assert lint._check_contradiction_maintenance(conn, **_dirs(tmp_path)) == []
    finally:
        conn.close()


def test_lint_reports_an_unverifiable_proposal(tmp_path):
    a, b, _sa, _sb = _vault(tmp_path)
    (tmp_path / "reviews" / "pending" / f"{_rid(a, b)}.json").unlink()
    conn = con_t._gconn(tmp_path)
    try:
        found = lint._check_contradiction_maintenance(conn, **_dirs(tmp_path))
    finally:
        conn.close()
    assert [(f["check"], f["data"]["reason"]) for f in found] == [
        ("contradiction_unverifiable", "no_pending_review")]


# --- the CLI ------------------------------------------------------------------------------------


def test_the_cli_maintains_without_a_model_and_records_a_job(tmp_path, monkeypatch, capsys):
    import detect_contradictions as cli

    from app.backend import config, db
    from app.llm import client as llm_client
    a, b, _sa, sb = _vault(tmp_path)
    _same_author(tmp_path, sb)
    real = config.get_settings
    monkeypatch.setattr(config, "get_settings", lambda root=None: real(tmp_path))
    monkeypatch.setattr(llm_client, "build_client",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("model client built")))
    assert cli.main(["maintain"]) == 0
    out = capsys.readouterr().out
    assert "Retired: 1" in out and "no longer independent" in out
    assert "does not check documents for contradictions" in out
    conn = db.connect(tmp_path / "db" / "jobs.sqlite")
    try:
        jobs = [j for j in db.list_jobs(conn, limit=50) if j["job_type"] == "detect_contradictions"]
    finally:
        conn.close()
    assert [(j["status"], j["metadata"]["mode"]) for j in jobs] == [("succeeded", "maintain")]
    assert _edge_status(tmp_path, a, b) == ["superseded"]


def test_the_cli_reports_a_failed_maintenance(tmp_path, monkeypatch, capsys):
    import detect_contradictions as cli

    from app.backend import config
    real = config.get_settings
    monkeypatch.setattr(config, "get_settings", lambda root=None: real(tmp_path))
    monkeypatch.setattr(contradictions, "maintain",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("graph locked")))
    assert cli.main(["maintain"]) == 1
    assert "Contradiction maintenance failed: graph locked" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["maintain", "extra"], ["run"], ["estimate", "src_a"]])
def test_other_arguments_run_nothing(argv, capsys):
    import detect_contradictions as cli
    assert cli.main(argv) == 2
    assert "usage:" in capsys.readouterr().err


def test_no_page_changes(tmp_path):
    a, b, _sa, sb = _vault(tmp_path)
    before = {p: p.read_bytes() for p in (tmp_path / "wiki").rglob("*.md")}
    _same_author(tmp_path, sb)
    assert _maintain(tmp_path)["retired"]
    assert {p: p.read_bytes() for p in (tmp_path / "wiki").rglob("*.md")} == before


def test_a_lint_run_reports_stale_proposals_without_failing(tmp_path):
    a, b, _sa, sb = _vault(tmp_path)
    _same_author(tmp_path, sb)
    report = lint.run_lint(tmp_path, record_job=False, file_review_items=False)
    assert report["by_check"].get("contradiction_maintenance_needed") == 1
    assert not any(f["check"].startswith("contradiction_") and f["severity"] != "low"
                   for f in report["findings"])
    _maintain(tmp_path)
    report = lint.run_lint(tmp_path, record_job=False, file_review_items=False)
    assert "contradiction_maintenance_needed" not in report["by_check"]


def test_saving_details_retires_the_documents_proposals_in_the_same_save(tmp_path, monkeypatch):
    import tests.test_details as det_t
    a, b, _sa, sb = _vault(tmp_path)
    form = det_t._form(tmp_path, sb, title="Report B", authors=[{"name": "Alice"}], tick=True)
    resp = det_t._post(_api(tmp_path, monkeypatch), sb, form)      # b now shares a's author
    assert resp.status_code == 200, resp.text
    assert "confirm details" in resp.text and "1 proposed contradiction(s) retired" in resp.text
    assert _edge_status(tmp_path, a, b) == ["superseded"]



# --- review round 1: interrupted retirement, extraction failure status, unverifiable first --------


def _audits(tmp_path, rid):
    return sorted((tmp_path / "reviews" / "audit_log").glob(f"{rid}-withdrawn-*.json"))


def _pending(tmp_path, rid):
    return (tmp_path / "reviews" / "pending" / f"{rid}.json").exists()


def _fail_once(monkeypatch, owner, name, *, when=lambda *a, **k: True, times=1):
    real = getattr(owner, name)
    state = {"failures": 0}

    def flaky(*args, **kwargs):
        if state["failures"] < times and when(*args, **kwargs):
            state["failures"] += 1
            raise OSError(f"simulated failure in {name}")
        return real(*args, **kwargs)
    monkeypatch.setattr(owner, name, flaky)


def _pending_tmp(self, *a, **k):
    return self.name.endswith(".json.tmp") and self.parent.name == "pending"


def _audit_tmp(self, *a, **k):
    return self.name.endswith(".json.tmp") and self.parent.name == "audit_log"


def _lint_reasons(tmp_path):
    conn = con_t._gconn(tmp_path)
    try:
        return [(f["check"], f["data"]["reason"])
                for f in lint._check_contradiction_maintenance(conn, **_dirs(tmp_path))]
    finally:
        conn.close()


def _completes_on_retry(tmp_path, a, b, sb):
    """The interrupted retirement is reported, then completed with its original reason, even if the
    documents became independent again meanwhile, with exactly one audit entry."""
    rid = _rid(a, b)
    manifests.set_provenance(tmp_path / MD, sb, author="Bob")
    found = _check(tmp_path)
    assert [e["reason"] for e in found["stale"]] == ["retirement_unfinished"]
    assert found["unverifiable"] == []
    assert _lint_reasons(tmp_path) == [("contradiction_maintenance_needed", "retirement_unfinished")]
    retired = _maintain(tmp_path)["retired"]
    assert [(e["reason"], e["original_reason"]) for e in retired] == [
        ("retirement_unfinished", "not_independent")]
    assert _edge_status(tmp_path, a, b) == ["superseded"] and not _pending(tmp_path, rid)
    assert _withdrawn_notes(tmp_path, rid) == [
        "contradiction maintenance: its documents are no longer independent"]
    assert _maintain(tmp_path)["retired"] == [] and _lint_reasons(tmp_path) == []


def test_a_failure_before_any_effect_leaves_the_proposal_to_retry(tmp_path, monkeypatch):
    a, b, _sa, sb = _vault(tmp_path)
    _same_author(tmp_path, sb)
    _fail_once(monkeypatch, Path, "replace", when=_pending_tmp)       # marking the review fails
    with pytest.raises(OSError):
        _maintain(tmp_path)
    assert _edge_status(tmp_path, a, b) == ["proposed"] and _pending(tmp_path, _rid(a, b))
    assert _audits(tmp_path, _rid(a, b)) == []
    assert [e["reason"] for e in _check(tmp_path)["stale"]] == ["not_independent"]
    assert [e["reason"] for e in _maintain(tmp_path)["retired"]] == ["not_independent"]
    assert _edge_status(tmp_path, a, b) == ["superseded"] and len(_audits(tmp_path, _rid(a, b))) == 1


def test_a_failure_superseding_the_edge_is_completed_on_retry(tmp_path, monkeypatch):
    a, b, _sa, sb = _vault(tmp_path)
    _same_author(tmp_path, sb)
    _fail_once(monkeypatch, graph, "set_status")
    with pytest.raises(OSError):
        _maintain(tmp_path)
    assert _edge_status(tmp_path, a, b) == ["proposed"] and _pending(tmp_path, _rid(a, b))
    monkeypatch.undo()
    _completes_on_retry(tmp_path, a, b, sb)


def test_a_failure_after_edge_supersession_is_completed_on_retry(tmp_path, monkeypatch):
    a, b, _sa, sb = _vault(tmp_path)
    _same_author(tmp_path, sb)
    _fail_once(monkeypatch, Path, "replace", when=_audit_tmp)         # the audit write fails
    with pytest.raises(OSError):
        _maintain(tmp_path)
    assert _edge_status(tmp_path, a, b) == ["superseded"]            # no longer a proposal...
    assert _pending(tmp_path, _rid(a, b)) and _audits(tmp_path, _rid(a, b)) == []
    monkeypatch.undo()
    _completes_on_retry(tmp_path, a, b, sb)                          # ...yet still found


def test_a_failure_deleting_the_review_keeps_its_audit_and_is_completed(tmp_path, monkeypatch):
    a, b, _sa, sb = _vault(tmp_path)
    _same_author(tmp_path, sb)
    rid = _rid(a, b)
    _fail_once(monkeypatch, Path, "unlink", when=lambda self, *a, **k: self.name == f"{rid}.json")
    with pytest.raises(OSError):
        _maintain(tmp_path)
    assert _edge_status(tmp_path, a, b) == ["superseded"] and _pending(tmp_path, rid)
    assert len(_audits(tmp_path, rid)) == 1                          # written before the delete
    monkeypatch.undo()
    _completes_on_retry(tmp_path, a, b, sb)                          # the same entry, not a second


def test_a_re_proposal_after_retirement_gets_its_own_audit_entry(tmp_path):
    a, b, _sa, sb = _vault(tmp_path)
    _same_author(tmp_path, sb)
    _maintain(tmp_path)
    manifests.set_provenance(tmp_path / MD, sb, author="Bob")
    con_t._propose(tmp_path)                                       # re-judged and re-filed later
    assert _edge_status(tmp_path, a, b) == ["proposed"]
    assert _maintain(tmp_path)["retired"] == []                    # not mistaken for unfinished
    _same_author(tmp_path, sb)
    assert [e["reason"] for e in _maintain(tmp_path)["retired"]] == ["not_independent"]
    assert len(_audits(tmp_path, _rid(a, b))) == 2


def test_a_maintenance_failure_during_extraction_keeps_claims_but_is_partial(tmp_path, monkeypatch):
    a, b, sa, _sb = _vault(tmp_path)

    def broken(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(contradictions, "maintain_assertions", broken)
    summary = _re_extract(tmp_path, sa, NewQuoteAdapter())
    assert summary["status"] == "partial" and summary["errors"] == 0
    assert summary["contradiction_maintenance_errors"] == ["OSError: disk full"]
    assert summary["claims_written"] == 1                          # the claims are kept
    assert (tmp_path / "wiki" / "Claims" / f"{a}.md").exists()
    assert _edge_status(tmp_path, a, b) == ["proposed"]


def test_the_claims_cli_exits_nonzero_when_maintenance_fails(tmp_path, monkeypatch, capsys):
    import extract_claims as script

    from app.backend.config import get_settings
    a, b, sa, _sb = _vault(tmp_path)
    monkeypatch.setenv("ENRICH_MODEL_STANDARD", con_t.CLAIM_MODEL)
    monkeypatch.setattr(script, "get_settings", lambda: get_settings(tmp_path))
    monkeypatch.setattr(script, "build_client",
                        lambda settings, cache: LLMClient({"anthropic": con_t.ClaimAdapter()}))
    assert script.main(["--force", sa]) == 0                       # control: a clean run passes
    capsys.readouterr()

    def broken(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(contradictions, "maintain_assertions", broken)
    assert script.main(["--force", sa]) == 1
    out = capsys.readouterr().out
    assert "contradiction maintenance failed: OSError: disk full" in out
    assert "detect_contradictions.py maintain" in out


@pytest.mark.parametrize("damage", ["review_missing", "sides_missing", "one_side_judged"])
def test_an_unverifiable_proposal_is_never_retired_even_when_independence_is_lost(
        tmp_path, damage):
    a, b, _sa, sb = _vault(tmp_path)
    path = tmp_path / "reviews" / "pending" / f"{_rid(a, b)}.json"
    item = json.loads(path.read_text())
    if damage == "review_missing":
        path.unlink()
    elif damage == "sides_missing":
        item["proposal"].pop("sides")
        path.write_text(json.dumps(item))
    else:
        item["context"]["judged_evidence"] = {a: "0" * 16}
        path.write_text(json.dumps(item))
    _same_author(tmp_path, sb)
    out = _maintain(tmp_path)
    assert out["retired"] == []
    assert [e["reason"] for e in out["unverifiable"]] == [{
        "review_missing": "no_pending_review", "sides_missing": "no_judged_evidence",
        "one_side_judged": "judged_evidence_incomplete"}[damage]]
    assert _edge_status(tmp_path, a, b) == ["proposed"]


def test_an_unverifiable_proposal_is_never_retired_even_when_evidence_changes(tmp_path):
    a, b, sa, _sb = _vault(tmp_path)
    path = tmp_path / "reviews" / "pending" / f"{_rid(a, b)}.json"
    item = json.loads(path.read_text())
    item["proposal"].pop("sides")
    path.write_text(json.dumps(item))
    summary = _re_extract(tmp_path, sa, NewQuoteAdapter())
    assert summary["contradictions_retired"] == 0
    assert _edge_status(tmp_path, a, b) == ["proposed"]
    assert [e["reason"] for e in _check(tmp_path)["unverifiable"]] == ["no_judged_evidence"]


# --- review round 2: decisions on a retiring review; durable recovery records -------------------


def _interrupt_after_marking(tmp_path, monkeypatch, sb):
    """Leave a retirement interrupted right after its marker (the edge still proposed)."""
    _same_author(tmp_path, sb)
    _fail_once(monkeypatch, graph, "set_status")
    with pytest.raises(OSError):
        _maintain(tmp_path)                                       # _fail_once fails only once


def _interrupt_after_supersession(tmp_path, monkeypatch, sb):
    """Leave a retirement interrupted after the edge was superseded (the review still pending)."""
    _same_author(tmp_path, sb)
    _fail_once(monkeypatch, Path, "replace", when=_audit_tmp)
    with pytest.raises(OSError):
        _maintain(tmp_path)                                       # _fail_once fails only once


def test_the_decision_primitives_refuse_a_retiring_review(tmp_path, monkeypatch):
    a, b, _sa, sb = _vault(tmp_path)
    _interrupt_after_supersession(tmp_path, monkeypatch, sb)
    rid = _rid(a, b)
    for call in (lambda: reviews.resolve_review_item(tmp_path / "reviews", rid,
                                                     decision="approved", decided_by="human"),
                 lambda: reviews.resolve_review_item(tmp_path / "reviews", rid,
                                                     decision="rejected", decided_by="human"),
                 lambda: reviews.defer_review_item(tmp_path / "reviews", rid, note="later")):
        with pytest.raises(reviews.ReviewRetiring):
            call()
    assert _pending(tmp_path, rid)
    assert not (tmp_path / "reviews" / "approved" / f"{rid}.json").exists()


@pytest.mark.parametrize("interrupt", ["after_marking", "after_supersession"])
@pytest.mark.parametrize("route", ["approve", "reject", "defer"])
def test_the_api_refuses_a_decision_on_a_retiring_review(tmp_path, monkeypatch, interrupt, route):
    a, b, _sa, sb = _vault(tmp_path)
    {"after_marking": _interrupt_after_marking,
     "after_supersession": _interrupt_after_supersession}[interrupt](tmp_path, monkeypatch, sb)
    api = _api(tmp_path, monkeypatch)
    rid = _rid(a, b)
    resp = api.post(f"/reviews/{rid}/{route}", json={"note": "n"})
    assert resp.status_code == 409
    assert resp.json()["detail"].startswith("maintenance_required:")
    assert "detect_contradictions.py maintain" in resp.json()["detail"]
    assert _pending(tmp_path, rid) and not list((tmp_path / "reviews").glob(f"*ed/{rid}.json"))
    # Maintenance finishes the retirement; nothing was decided, so Apply has nothing to do.
    assert [e["reason"] for e in _maintain(tmp_path)["retired"]] == ["retirement_unfinished"]
    assert _edge_status(tmp_path, a, b) == ["superseded"]
    assert con_t._apply(tmp_path)["resolution"] == {"acknowledged": 0, "rejected": 0,
                                                   "superseded_executed": 0}


@pytest.mark.parametrize("action", ["acknowledge", "supersede_a", "reject"])
def test_a_form_opened_before_the_retirement_started_is_refused(tmp_path, monkeypatch, action):
    a, b, _sa, sb = _vault(tmp_path)
    rid = _rid(a, b)
    page = _api(tmp_path, monkeypatch).get(f"/ui/reviews/{rid}")    # the form, as first shown
    assert page.status_code == 200 and "decide" in page.text
    _interrupt_after_supersession(tmp_path, monkeypatch, sb)
    resp = _api(tmp_path, monkeypatch).post(f"/ui/reviews/{rid}/decide",
                                           data={"action": action, "note": "n"},
                                           follow_redirects=False)
    assert resp.status_code == 409 and "maintenance_required" in resp.text
    assert "detect_contradictions.py maintain" in resp.text
    assert _pending(tmp_path, rid)
    assert not list((tmp_path / "reviews").glob(f"*ed/{rid}.json"))


def test_an_unmarked_review_can_still_be_decided(tmp_path, monkeypatch):
    a, b, _sa, _sb = _vault(tmp_path)
    resp = _api(tmp_path, monkeypatch).post(f"/reviews/{_rid(a, b)}/approve", json={"note": "n"})
    assert resp.status_code == 200 and resp.json()["decision_recorded"] is True


def _record_persistence(monkeypatch, tmp_path, rid) -> list[str]:
    """Record the file writes, flushes, renames, deletes and edge updates of a retirement, in order
    (from the calls made after this is installed, wrapping whatever is installed now)."""
    reviews_dir = tmp_path / "reviews"
    events: list[str] = []

    def where(path):
        path = Path(path)
        return f"{path.parent.name}/{path.name.replace(rid, 'RID')}"

    real_write, real_sync, real_sync_file = reviews._write_durably, reviews._sync_dir, \
        reviews._sync_file
    real_replace, real_unlink, real_set = Path.replace, Path.unlink, graph.set_status

    def write(path, data):
        real_write(path, data)
        events.append(f"fsync {where(path)}")

    def sync(path):
        real_sync(path)
        events.append(f"dirsync {Path(path).name}")

    def sync_file(path):
        real_sync_file(path)
        events.append(f"fsync existing {where(path)}")

    def replace(self, target):
        out = real_replace(self, target)
        if reviews_dir in Path(target).parents:
            events.append(f"rename {where(target)}")
        return out

    def unlink(self, *args, **kwargs):
        real_unlink(self, *args, **kwargs)
        if reviews_dir in self.parents:
            events.append(f"delete {where(self)}")

    def set_status(conn, edge_id, status, **kwargs):
        real_set(conn, edge_id, status, **kwargs)
        events.append(f"edge {status}")

    monkeypatch.setattr(reviews, "_write_durably", write)
    monkeypatch.setattr(reviews, "_sync_dir", sync)
    monkeypatch.setattr(reviews, "_sync_file", sync_file)
    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(graph, "set_status", set_status)
    return events


def test_recovery_records_are_durable_before_the_effects_they_guard(tmp_path, monkeypatch):
    a, b, _sa, sb = _vault(tmp_path)
    _same_author(tmp_path, sb)
    rid = _rid(a, b)
    events = _record_persistence(monkeypatch, tmp_path, rid)
    _maintain(tmp_path)
    audit = next(e for e in events if e.startswith("fsync audit_log/")).split(" ", 1)[1]
    assert [e for e in events if "jobs" not in e] == [
        "fsync pending/RID.json.tmp", "rename pending/RID.json", "dirsync pending",  # the marker
        "edge superseded",                                                         # then the edge
        f"fsync {audit}", f"rename {audit[:-len('.tmp')]}", "dirsync audit_log",   # the audit
        "delete pending/RID.json", "dirsync pending"]                              # then the delete


@pytest.mark.parametrize("failing_dir", ["pending", "audit_log"])
def test_a_failed_directory_sync_stops_before_the_guarded_effect(tmp_path, monkeypatch,
                                                                 failing_dir):
    a, b, _sa, sb = _vault(tmp_path)
    _same_author(tmp_path, sb)
    rid = _rid(a, b)
    _fail_once(monkeypatch, reviews, "_sync_dir", when=lambda path: Path(path).name == failing_dir)
    with pytest.raises(OSError):
        _maintain(tmp_path)
    if failing_dir == "pending":          # the marker isn't known durable: the edge is untouched
        assert _edge_status(tmp_path, a, b) == ["proposed"]
    else:                                 # the audit isn't known durable: the review isn't deleted
        assert _edge_status(tmp_path, a, b) == ["superseded"]
    assert _pending(tmp_path, rid)
    _completes_on_retry(tmp_path, a, b, sb)


def _mark_id(tmp_path, rid):
    item = json.loads((tmp_path / "reviews" / "pending" / f"{rid}.json").read_text("utf-8"))
    return item["retiring"]["id"]


@pytest.mark.parametrize("failing_dir", ["pending", "audit_log"])
def test_a_retry_repeats_the_failed_durability_barrier_first(tmp_path, monkeypatch, failing_dir):
    """The interrupted run renamed its record into place but couldn't flush the directory; the
    retry flushes that record (file and directory) again before the effect it guards."""
    a, b, _sa, sb = _vault(tmp_path)
    _same_author(tmp_path, sb)
    rid = _rid(a, b)
    _fail_once(monkeypatch, reviews, "_sync_dir", when=lambda path: Path(path).name == failing_dir)
    with pytest.raises(OSError):
        _maintain(tmp_path)
    audit = f"audit_log/RID-withdrawn-m{_mark_id(tmp_path, rid)}.json"
    events = _record_persistence(monkeypatch, tmp_path, rid)
    assert [e["reason"] for e in _maintain(tmp_path)["retired"]] == ["retirement_unfinished"]
    marker = ["fsync existing pending/RID.json", "dirsync pending"]     # the marker, again
    if failing_dir == "pending":
        rest = ["edge superseded", f"fsync {audit}.tmp", f"rename {audit}", "dirsync audit_log"]
    else:                                     # the edge is already superseded; the audit, again
        rest = [f"fsync existing {audit}", "dirsync audit_log"]
    assert [e for e in events if "jobs" not in e] == marker + rest + [
        "delete pending/RID.json", "dirsync pending"]
    assert _edge_status(tmp_path, a, b) == ["superseded"] and not _pending(tmp_path, rid)
    assert [p.name for p in _audits(tmp_path, rid)] == [audit.split("/")[1].replace("RID", rid)]


@pytest.mark.parametrize("failing_dir", ["pending", "audit_log"])
def test_a_repeated_sync_failure_still_stops_before_the_guarded_effect(tmp_path, monkeypatch,
                                                                       failing_dir):
    a, b, _sa, sb = _vault(tmp_path)
    _same_author(tmp_path, sb)
    rid = _rid(a, b)
    _fail_once(monkeypatch, reviews, "_sync_dir", times=2,
               when=lambda path: Path(path).name == failing_dir)
    for _attempt in range(2):                 # the first run, then a retry: both fail to flush
        with pytest.raises(OSError):
            _maintain(tmp_path)
        if failing_dir == "pending":          # the marker still isn't known durable
            assert _edge_status(tmp_path, a, b) == ["proposed"]
        else:                                 # the audit still isn't known durable
            assert _edge_status(tmp_path, a, b) == ["superseded"]
            assert len(_audits(tmp_path, rid)) == 1
        assert _pending(tmp_path, rid)
    _completes_on_retry(tmp_path, a, b, sb)
