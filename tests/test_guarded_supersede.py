"""ADR-0073 slice 2 — the guarded supersede (decisions 9, 10, 13–17).

The supersede runs only on a standing contradiction, journals its progress durably, marks the
loser's page in the same write as its deprecation, records one dedicated completion audit, rolls back
only its own edge when the journal proves the deprecation never completed (and that rollback consumes
the approval), and makes competing claim governance wait while it is unfinished.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph, review_read
from app.workers import (claim_marker, claims, contradiction_check, contradictions, deprecations,
                         identity_journal, lint, reviews, supersede, tag_links, wiki_render)
from scripts import validate_frontmatter
from tests import test_contradictions as tc

A, B, C, D, E = ("clm_" + ch * 16 for ch in "abcde")     # A wins over B by default
APPROVAL = "1" * 32
NOW = "2026-11-08T00:00:00Z"


# --- fixtures ----------------------------------------------------------------------------------


def _rid(a=A, b=B):
    lo, hi = sorted((a, b))
    return reviews.review_id("resolve_contradiction", {"claim_a": lo, "claim_b": hi})


def _graph(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    return graph.connect(gdb)


def _reconnect(tmp_path):
    return graph.connect(tmp_path / "db" / "graph.sqlite")


def _claim(conn, tmp_path, cid, text=None):
    tc._seed_evidenced_claim(conn, tmp_path, cid, f"src_{cid[-4:]}",
                             text or f"Claim {cid[-1]} says something specific.")


def _contradiction(conn, a, b, status="proposed"):
    lo, hi = sorted((a, b))
    graph.upsert_assertion(conn, src_id=lo, dst_id=hi, edge_type="contradicts", asserted_by="llm",
                           status=status, evidence_source_id=f"src_{lo[-4:]}",
                           evidence_char_start=0, evidence_char_end=5, review_id=_rid(lo, hi))


def _approve(tmp_path, a=A, b=B, *, winner=A, approval_id=APPROVAL, state="approved"):
    lo, hi = sorted((a, b))
    rid = _rid(lo, hi)
    item = {"review_id": rid, "type": "resolve_contradiction", "status": state,
            "subject": {"claim_a": lo, "claim_b": hi}, "decided_by": "human",
            "decided_at": "2026-11-01T00:00:00Z", "proposal": {}, "context": {}}
    if winner and state == "approved":
        item["winner"] = winner
    if approval_id and state == "approved":
        item["approval_id"] = approval_id
    d = tmp_path / "reviews" / state
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps(item), encoding="utf-8")
    return rid


def _world(tmp_path, *, edge_status="proposed", extra=()):
    conn = _graph(tmp_path)
    for cid in (A, B, *extra):
        _claim(conn, tmp_path, cid)
    _contradiction(conn, A, B, edge_status)
    conn.commit()
    return conn, _approve(tmp_path)


def _apply(tmp_path, conn):
    res = contradictions.apply_contradiction_decisions(
        conn, tmp_path / "reviews", claims_dir=tmp_path / "wiki" / "Claims",
        markdown_dir=tmp_path / "normalized" / "markdown", now=NOW)
    conn.commit()
    return {r["review_id"]: r for r in res["outcomes"]}, res


def _page(tmp_path, cid):
    return (tmp_path / "wiki" / "Claims" / f"{cid}.md").read_text(encoding="utf-8")


def _status(conn, cid):
    return graph.get_node(conn, cid)["status"]


def _sup_edges(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM edges WHERE edge_type = 'supersedes' ORDER BY edge_id")]


def _journal(tmp_path, rid):
    p = tmp_path / "reviews" / "in_flight" / f"{rid}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _audits(tmp_path, kind="superseded"):
    d = tmp_path / "reviews" / "audit_log"
    return sorted(d.glob(f"*-{kind}-*.json")) if d.exists() else []


def _end(tmp_path, rid):
    identity_journal.end(tmp_path / "reviews", rid)


def _retire_winner(tmp_path, conn, cid=A):
    """The real trigger: a re-extraction drops the claim from its document (it tombstones), which
    supersedes every contradicts edge touching it."""
    conn.execute("UPDATE edges SET status = 'superseded' WHERE src_id = ? "
                 "AND edge_type = 'derived_from'", (cid,))
    assert claims.recompose_claim(conn, cid=cid, claims_dir=tmp_path / "wiki" / "Claims",
                                  reviews_dir=tmp_path / "reviews", now=NOW,
                                  markdown_dir=tmp_path / "normalized" / "markdown") == "tombstoned"
    conn.commit()


def _interrupt_before_loser_page(monkeypatch):
    """Stop after the edge stage, before the marked loser page is written."""
    real = claims.recompose_claim

    def stop(*a, **kw):
        if kw.get("supersede_marker"):
            raise KeyboardInterrupt
        return real(*a, **kw)

    monkeypatch.setattr(claims, "recompose_claim", stop)
    return real


def _to_edge_written(tmp_path, monkeypatch, conn):
    real = _interrupt_before_loser_page(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        _apply(tmp_path, conn)
    monkeypatch.setattr(claims, "recompose_claim", real)
    conn.close()
    conn = _reconnect(tmp_path)
    assert _journal(tmp_path, _rid())["stage"] == supersede.EDGE_WRITTEN
    return conn


# --- the happy path ---------------------------------------------------------------------------


def test_a_supersede_executes_with_marker_and_one_completion_audit(tmp_path):
    conn, rid = _world(tmp_path)
    recs, res = _apply(tmp_path, conn)
    assert recs[rid]["outcome"] == "applied" and res["journaled"] == [rid]
    (edge,) = _sup_edges(conn)
    assert (edge["src_id"], edge["dst_id"], edge["status"], edge["review_id"]) == (A, B, "active", rid)
    page = _page(tmp_path, B)
    assert "status: deprecated_candidate" in page and _status(conn, B) == "deprecated_candidate"
    assert claim_marker.parse(page) == (claim_marker.OK, {"review_id": rid, "approval_id": APPROVAL,
                                                           "winner": A})
    (audit,) = _audits(tmp_path)
    rec = json.loads(audit.read_text())
    assert rec == {"event": "supersede_completed", "review_id": rid, "approval_id": APPROVAL,
                   "winner": A, "loser": B, "edge_id": edge["edge_id"], "completed_at": NOW}
    assert _journal(tmp_path, rid)["stage"] == supersede.LOSER_DEPRECATED
    assert not list((tmp_path / "reviews").rglob("*deprecate*"))   # no synthetic filing (d13)
    # The caller ends the journal after publication; then the next Apply writes nothing.
    _end(tmp_path, rid)
    path = tmp_path / "wiki" / "Claims" / f"{B}.md"
    stamp = path.stat().st_mtime_ns
    recs, res = _apply(tmp_path, conn)
    assert recs[rid]["outcome"] == "already_in_effect" and res["journaled"] == []
    assert path.stat().st_mtime_ns == stamp and len(_audits(tmp_path)) == 1


# --- decision 9: only on a standing contradiction ---------------------------------------------


@pytest.mark.parametrize("dropped", [A, B])
def test_the_real_trigger_writes_nothing(tmp_path, dropped):
    """Approve, then a re-extraction drops the winner (or the loser): Apply writes nothing."""
    conn, rid = _world(tmp_path)
    _retire_winner(tmp_path, conn, dropped)
    other = B if dropped == A else A
    before = _page(tmp_path, other)
    recs, _res = _apply(tmp_path, conn)
    assert (recs[rid]["outcome"], recs[rid]["reason"]) == ("skipped", "contradiction_retired")
    assert recs[rid]["details"]["information"] is True                   # decision 15
    assert _sup_edges(conn) == [] and _page(tmp_path, other) == before
    assert _journal(tmp_path, rid) is None and _audits(tmp_path) == []


@pytest.mark.parametrize("edge,reason", [(None, "edge_missing"),
                                         ("rejected", "edge_status_unexpected")])
def test_missing_and_rejected_edges_are_refused_under_attention(tmp_path, edge, reason):
    conn, rid = _world(tmp_path, edge_status=edge or "proposed")
    if edge is None:
        conn.execute("DELETE FROM edges WHERE edge_type = 'contradicts'")
        conn.commit()
    recs, _res = _apply(tmp_path, conn)
    assert (recs[rid]["outcome"], recs[rid]["reason"]) == ("skipped", reason)
    assert "information" not in recs[rid]["details"] and _sup_edges(conn) == []


@pytest.mark.parametrize("who,reason", [(A, "winner_not_active"), (B, "hidden_takes_precedence")])
def test_a_hidden_endpoint_page_with_a_stale_active_node_never_passes(tmp_path, who, reason):
    conn, rid = _world(tmp_path)
    path = tmp_path / "wiki" / "Claims" / f"{who}.md"
    path.write_text(path.read_text().replace("status: active", "status: hidden"))
    assert _status(conn, who) == "active"                               # the mirror is stale
    recs, _res = _apply(tmp_path, conn)
    assert (recs[rid]["outcome"], recs[rid]["reason"]) == ("skipped", reason)
    assert _sup_edges(conn) == [] and _journal(tmp_path, rid) is None
    (row,) = graph.contradiction_between(conn, A, B)
    assert row["status"] == "proposed"                  # not even the activation was written


def test_an_applied_supersede_stays_when_its_winner_retracts_and_is_linted(tmp_path):
    conn, rid = _world(tmp_path)
    _apply(tmp_path, conn)
    _end(tmp_path, rid)
    _retire_winner(tmp_path, conn, A)
    recs, _res = _apply(tmp_path, conn)
    assert recs[rid]["outcome"] == "already_in_effect"
    assert _status(conn, B) == "deprecated_candidate"   # no automatic undo
    found = lint._check_supersedes(conn, tmp_path / "wiki", tmp_path / "reviews")
    assert [f["check"] for f in found] == ["supersede_winner_retracted"]


def test_an_owned_edge_with_an_active_loser_and_no_journal_is_blocked_and_linted(tmp_path):
    conn, rid = _world(tmp_path)
    graph.upsert_assertion(conn, src_id=A, dst_id=B, edge_type="supersedes", asserted_by="human",
                           status="active", review_id=rid)
    conn.commit()
    recs, _res = _apply(tmp_path, conn)
    assert (recs[rid]["outcome"], recs[rid]["reason"]) == ("skipped", "supersede_state_unverified")
    assert _status(conn, B) == "active"
    found = lint._check_supersedes(conn, tmp_path / "wiki", tmp_path / "reviews")
    assert [f["check"] for f in found] == ["supersede_state_unverified"]


def test_a_foreign_owned_edge_blocks(tmp_path):
    conn, rid = _world(tmp_path)
    graph.upsert_assertion(conn, src_id=A, dst_id=B, edge_type="supersedes", asserted_by="human",
                           status="active", review_id="rev_" + "f" * 16)
    conn.commit()
    recs, _res = _apply(tmp_path, conn)
    assert recs[rid]["reason"] == "supersedes_edge_foreign" and _status(conn, B) == "active"


def test_a_legacy_approval_without_approval_id_starts_no_journal(tmp_path):
    conn = _graph(tmp_path)
    for cid in (A, B):
        _claim(conn, tmp_path, cid)
    _contradiction(conn, A, B)
    conn.commit()
    rid = _approve(tmp_path, approval_id=None)
    recs, _res = _apply(tmp_path, conn)
    assert recs[rid]["reason"] == "approval_unbound"
    assert _journal(tmp_path, rid) is None and _sup_edges(conn) == []


# --- decision 10: interruption boundaries and the precedence -----------------------------------


def test_interrupted_after_the_edge_resumes(tmp_path, monkeypatch):
    conn, rid = _world(tmp_path)
    conn = _to_edge_written(tmp_path, monkeypatch, conn)
    assert _status(conn, B) == "active" and len(_sup_edges(conn)) == 1
    recs, _res = _apply(tmp_path, conn)
    assert recs[rid]["outcome"] == "applied" and recs[rid]["details"]["resumed"] is True
    assert _status(conn, B) == "deprecated_candidate" and len(_audits(tmp_path)) == 1


def test_interrupted_after_the_marked_page_repairs_the_mirror(tmp_path, monkeypatch):
    conn, rid = _world(tmp_path)
    real = claims.recompose_claim

    def page_then_crash(gconn, **kw):
        out = real(gconn, **kw)
        if kw.get("supersede_marker"):
            raise KeyboardInterrupt
        return out

    monkeypatch.setattr(claims, "recompose_claim", page_then_crash)
    with pytest.raises(KeyboardInterrupt):
        _apply(tmp_path, conn)
    monkeypatch.setattr(claims, "recompose_claim", real)
    conn.close()
    conn = _reconnect(tmp_path)
    # the crash came after the fsynced marked page and before the graph mirror was durable
    graph.upsert_node(conn, node_id=B, node_type="claim", slug=B, status="active")
    conn.commit()
    assert "status: deprecated_candidate" in _page(tmp_path, B) and _status(conn, B) == "active"
    page = _page(tmp_path, B)
    recs, _res = _apply(tmp_path, conn)                  # row 3: mirror repair only
    assert recs[rid]["outcome"] == "applied"
    assert _status(conn, B) == "deprecated_candidate" and _page(tmp_path, B) == page
    assert len(_audits(tmp_path)) == 1


@pytest.mark.parametrize("boundary", ["before_stage", "before_audit", "after_audit"])
def test_interrupted_around_the_completion_audit(tmp_path, monkeypatch, boundary):
    conn, rid = _world(tmp_path)
    real_stage, real_audit = identity_journal.set_stage, supersede._ensure_audit

    def stage(reviews_dir, review_id, value):
        if value == supersede.LOSER_DEPRECATED and boundary == "before_stage":
            raise KeyboardInterrupt
        return real_stage(reviews_dir, review_id, value)

    def audit(path, **kw):
        if boundary == "before_audit":
            raise KeyboardInterrupt
        real_audit(path, **kw)
        if boundary == "after_audit":
            raise KeyboardInterrupt

    monkeypatch.setattr(identity_journal, "set_stage", stage)
    monkeypatch.setattr(supersede, "_ensure_audit", audit)
    with pytest.raises(KeyboardInterrupt):
        _apply(tmp_path, conn)
    monkeypatch.setattr(identity_journal, "set_stage", real_stage)
    monkeypatch.setattr(supersede, "_ensure_audit", real_audit)
    conn.close()
    conn = _reconnect(tmp_path)
    first = [json.loads(p.read_text()) for p in _audits(tmp_path)]
    page = _page(tmp_path, B)
    for _ in range(2):                                   # repeated recovery
        recs, _res = _apply(tmp_path, conn)
        assert recs[rid]["outcome"] == "applied"
    (audit_path,) = _audits(tmp_path)
    if first:                                            # the original completion time is kept
        assert json.loads(audit_path.read_text()) == first[0]
    assert _page(tmp_path, B) == page                    # page preserved


def test_a_deprecated_loser_without_the_marker_blocks(tmp_path, monkeypatch):
    conn, rid = _world(tmp_path)
    conn = _to_edge_written(tmp_path, monkeypatch, conn)
    # independently deprecated (e.g. by a retraction), no marker
    claims.recompose_claim(conn, cid=B, claims_dir=tmp_path / "wiki" / "Claims",
                           reviews_dir=tmp_path / "reviews", now=NOW, deprecate=True,
                           markdown_dir=tmp_path / "normalized" / "markdown")
    conn.commit()
    recs, _res = _apply(tmp_path, conn)
    assert (recs[rid]["outcome"], recs[rid]["reason"]) == ("skipped", "loser_deprecated_unmarked")
    assert _audits(tmp_path) == [] and _journal(tmp_path, rid)["stage"] == supersede.EDGE_WRITTEN


def test_a_wrong_winner_marker_is_not_this_operation(tmp_path, monkeypatch):
    conn, rid = _world(tmp_path)
    conn = _to_edge_written(tmp_path, monkeypatch, conn)
    claims.recompose_claim(conn, cid=B, claims_dir=tmp_path / "wiki" / "Claims",
                           reviews_dir=tmp_path / "reviews", now=NOW, deprecate=True,
                           supersede_marker={"review_id": rid, "approval_id": APPROVAL,
                                             "winner": C},
                           markdown_dir=tmp_path / "normalized" / "markdown")
    conn.rollback()                                      # the page only
    recs, _res = _apply(tmp_path, conn)
    assert recs[rid]["reason"] == "loser_deprecated_unmarked" and _audits(tmp_path) == []


@pytest.mark.parametrize("damage", ["unreadable", "other_op", "other_approval", "unknown_stage"])
def test_an_unreadable_or_mismatched_journal_blocks(tmp_path, monkeypatch, damage):
    conn, rid = _world(tmp_path)
    conn = _to_edge_written(tmp_path, monkeypatch, conn)
    path = tmp_path / "reviews" / "in_flight" / f"{rid}.json"
    data = json.loads(path.read_text())
    if damage == "unreadable":
        path.write_text("{torn")
    else:
        data.update({"other_op": {"op": "merge_items"},
                     "other_approval": {"approval_id": "9" * 32},
                     "unknown_stage": {"stage": "mystery"}}[damage])
        path.write_text(json.dumps(data))
    before = (_page(tmp_path, B), _sup_edges(conn))
    recs, _res = _apply(tmp_path, conn)
    assert recs[rid]["reason"] in ("supersede_journal_unreadable", "supersede_journal_invalid")
    assert (_page(tmp_path, B), _sup_edges(conn)) == before


# --- decisions 10 and 17: rollback ------------------------------------------------------------


def _fail_prerequisites(conn):
    """Make the prerequisites fail without retracting anyone: the contradiction now reads rejected."""
    conn.execute("UPDATE edges SET status = 'rejected' WHERE edge_type = 'contradicts'")
    conn.commit()


def _restore_prerequisites(conn):
    conn.execute("UPDATE edges SET status = 'active' WHERE edge_type = 'contradicts'")
    conn.commit()


def test_rollback_consumes_the_approval_generation(tmp_path, monkeypatch):
    conn, rid = _world(tmp_path)
    conn = _to_edge_written(tmp_path, monkeypatch, conn)
    _fail_prerequisites(conn)
    recs, res = _apply(tmp_path, conn)
    rec = recs[rid]
    assert (rec["outcome"], rec["reason"]) == ("incomplete", "partial_supersede_withdrawn")
    assert res["journaled"] == [rid]
    (edge,) = _sup_edges(conn)
    assert edge["status"] == "superseded" and _status(conn, B) == "active"
    (rb,) = _audits(tmp_path, "rolled-back")
    assert json.loads(rb.read_text())["event"] == "supersede_rolled_back"
    assert _audits(tmp_path) == []                       # never completion evidence
    _end(tmp_path, rid)
    _restore_prerequisites(conn)                          # restored prerequisites…
    for _ in range(2):
        recs, _res = _apply(tmp_path, conn)              # …never authorize another attempt
        assert (recs[rid]["outcome"], recs[rid]["reason"]) == ("skipped", "supersede_withdrawn")
    assert _sup_edges(conn)[0]["status"] == "superseded" and _status(conn, B) == "active"
    # verified absence after the rollback: reopenable
    prev = review_read.project_review(json.loads((tmp_path / "reviews" / "approved"
                                                  / f"{rid}.json").read_text()),
                                      gconn=conn, wiki_dir=tmp_path / "wiki")
    assert prev["apply"]["effect_status"] == review_read.PENDING_APPLY


def test_a_fresh_approval_generation_executes_normally(tmp_path, monkeypatch):
    conn, rid = _world(tmp_path)
    conn = _to_edge_written(tmp_path, monkeypatch, conn)
    _fail_prerequisites(conn)
    _apply(tmp_path, conn)
    _end(tmp_path, rid)
    _restore_prerequisites(conn)
    _approve(tmp_path, approval_id="2" * 32)              # reopened and re-approved
    recs, _res = _apply(tmp_path, conn)
    assert recs[rid]["outcome"] == "applied"
    assert _status(conn, B) == "deprecated_candidate"
    assert [json.loads(p.read_text())["approval_id"] for p in _audits(tmp_path)] == ["2" * 32]


@pytest.mark.parametrize("boundary", ["after_stage", "after_edge"])
def test_an_interrupted_rollback_finishes_even_when_prerequisites_return(tmp_path, monkeypatch,
                                                                         boundary):
    conn, rid = _world(tmp_path)
    conn = _to_edge_written(tmp_path, monkeypatch, conn)
    _fail_prerequisites(conn)
    real_status, real_audit = graph.set_status, supersede._ensure_audit

    def stop_edge(c, edge_id, status, **kw):
        if status == "superseded" and boundary == "after_stage":
            raise KeyboardInterrupt
        return real_status(c, edge_id, status, **kw)

    def stop_audit(path, **kw):
        if boundary == "after_edge":
            raise KeyboardInterrupt
        return real_audit(path, **kw)

    monkeypatch.setattr(supersede.graph, "set_status", stop_edge)
    monkeypatch.setattr(supersede, "_ensure_audit", stop_audit)
    with pytest.raises(KeyboardInterrupt):
        _apply(tmp_path, conn)
    monkeypatch.setattr(supersede.graph, "set_status", real_status)
    monkeypatch.setattr(supersede, "_ensure_audit", real_audit)
    conn.close()
    conn = _reconnect(tmp_path)
    assert _journal(tmp_path, rid)["stage"] == supersede.ROLLING_BACK
    _restore_prerequisites(conn)                          # prerequisites return during recovery
    recs, _res = _apply(tmp_path, conn)
    assert recs[rid]["reason"] == "partial_supersede_withdrawn"
    assert _sup_edges(conn)[0]["status"] == "superseded" and _status(conn, B) == "active"
    assert len(_audits(tmp_path, "rolled-back")) == 1


@pytest.mark.parametrize("evidence", ["marker", "audit", "unreadable_audit", "damaged_marker"])
def test_rollback_recovery_meeting_completion_evidence_makes_no_mutation(tmp_path, monkeypatch,
                                                                          evidence):
    conn, rid = _world(tmp_path)
    conn = _to_edge_written(tmp_path, monkeypatch, conn)
    identity_journal.set_stage(tmp_path / "reviews", rid, supersede.ROLLING_BACK)
    page = tmp_path / "wiki" / "Claims" / f"{B}.md"
    bind = {"review_id": rid, "approval_id": APPROVAL, "winner": A, "loser": B}
    if evidence in ("marker", "damaged_marker"):
        lines = (claim_marker.lines({"review_id": rid, "approval_id": APPROVAL, "winner": A})
                 if evidence == "marker" else ['superseded_by_review: "' + rid + '"'])
        text = page.read_text()
        page.write_text(text.replace("status: active", "status: active\n" + "\n".join(lines), 1))
    else:
        path = supersede.completion_audit_path(tmp_path / "reviews", rid, APPROVAL)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{torn" if evidence == "unreadable_audit" else json.dumps({
            "event": "supersede_completed", **bind, "edge_id": _sup_edges(conn)[0]["edge_id"],
            "completed_at": NOW}))
    before = (page.read_text(), _sup_edges(conn))
    recs, _res = _apply(tmp_path, conn)
    expected = ("completion_evidence_found" if evidence in ("marker", "audit")
                else "rollback_evidence_unreadable")
    assert (recs[rid]["outcome"], recs[rid]["reason"]) == ("skipped", expected)
    assert (page.read_text(), _sup_edges(conn)) == before
    assert _audits(tmp_path, "rolled-back") == []


@pytest.mark.parametrize("evidence", ["marker", "audit", "unreadable_audit"])
def test_apply_rollback_is_refused_by_the_completion_evidence_guard(tmp_path, monkeypatch, evidence):
    """Row 7 with failed prerequisites: any completion evidence (or unreadable evidence) forbids the
    compensating rollback, so nothing is written."""
    conn, rid = _world(tmp_path)
    conn = _to_edge_written(tmp_path, monkeypatch, conn)
    _fail_prerequisites(conn)
    page = tmp_path / "wiki" / "Claims" / f"{B}.md"
    if evidence == "marker":                              # an active page with this marker
        lines = claim_marker.lines({"review_id": rid, "approval_id": APPROVAL, "winner": A})
        page.write_text(page.read_text().replace("status: active",
                                                 "status: active\n" + "\n".join(lines), 1))
    else:
        path = supersede.completion_audit_path(tmp_path / "reviews", rid, APPROVAL)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{torn" if evidence == "unreadable_audit" else json.dumps({
            "event": "supersede_completed", "review_id": rid, "approval_id": APPROVAL,
            "winner": A, "loser": B, "edge_id": _sup_edges(conn)[0]["edge_id"],
            "completed_at": NOW}))
    recs, _res = _apply(tmp_path, conn)
    assert recs[rid]["reason"] in ("completion_evidence_found", "rollback_evidence_unreadable")
    assert _sup_edges(conn)[0]["status"] == "active"
    assert _journal(tmp_path, rid)["stage"] == supersede.EDGE_WRITTEN


@pytest.mark.parametrize("damage", ["unreadable", "conflicting"])
def test_an_unreadable_or_conflicting_rollback_record_blocks(tmp_path, monkeypatch, damage):
    conn, rid = _world(tmp_path)
    conn = _to_edge_written(tmp_path, monkeypatch, conn)
    _fail_prerequisites(conn)
    _apply(tmp_path, conn)
    _end(tmp_path, rid)
    (rb,) = _audits(tmp_path, "rolled-back")
    rb.write_text("{torn" if damage == "unreadable"
                  else json.dumps({**json.loads(rb.read_text()), "winner": C}))
    recs, _res = _apply(tmp_path, conn)
    assert recs[rid]["reason"] == "rollback_audit_conflict"


# --- decision 13: the completion audit ---------------------------------------------------------


def test_an_unrelated_human_deprecation_of_the_loser_is_left_alone(tmp_path):
    conn, rid = _world(tmp_path)
    dep = reviews.create_review_item(tmp_path / "reviews", review_type="deprecate_wiki_page",
                                     subject={"node_id": B, "page": f"Claims/{B}.md"},
                                     proposal={"to_status": "deprecated_candidate"},
                                     context={"node_type": "claim"}, now=NOW)
    before = (tmp_path / "reviews" / "pending" / f"{dep}.json").read_text()
    recs, _res = _apply(tmp_path, conn)
    assert recs[rid]["outcome"] == "applied" and len(_audits(tmp_path)) == 1
    assert (tmp_path / "reviews" / "pending" / f"{dep}.json").read_text() == before


def test_an_earlier_supersede_of_the_same_loser_wins(tmp_path):
    conn, rid = _world(tmp_path, extra=(C,))
    _apply(tmp_path, conn)
    _end(tmp_path, rid)
    _contradiction(conn, B, C)
    conn.commit()
    rid2 = _approve(tmp_path, B, C, winner=C, approval_id="3" * 32)
    recs, _res = _apply(tmp_path, conn)
    assert recs[rid2]["reason"] == "loser_not_active"
    assert claim_marker.parse(_page(tmp_path, B))[1]["review_id"] == rid   # marker untouched
    assert len(_audits(tmp_path)) == 1


def test_a_conflicting_completion_audit_blocks_and_is_never_overwritten(tmp_path, monkeypatch):
    conn, rid = _world(tmp_path)
    real = supersede._ensure_audit
    monkeypatch.setattr(supersede, "_ensure_audit", lambda *a, **k: (_ for _ in ()).throw(
        KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        _apply(tmp_path, conn)
    monkeypatch.setattr(supersede, "_ensure_audit", real)
    path = supersede.completion_audit_path(tmp_path / "reviews", rid, APPROVAL)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"event": "supersede_completed", "review_id": rid,
                                "approval_id": APPROVAL, "winner": C, "loser": B,
                                "edge_id": "x", "completed_at": "then"}))
    before = path.read_text()
    conn.close()
    recs, _res = _apply(tmp_path, _reconnect(tmp_path))
    assert (recs[rid]["outcome"], recs[rid]["reason"]) == ("skipped", "completion_audit_conflict")
    assert path.read_text() == before


# --- decision 14: claim governance waits ------------------------------------------------------


def _governance(tmp_path, rtype, cid, to_status):
    rid = reviews.create_review_item(tmp_path / "reviews", review_type=rtype,
                                     subject={"node_id": cid, "page": f"Claims/{cid}.md"},
                                     proposal={"to_status": to_status},
                                     context={"node_type": "claim"}, now=NOW)
    reviews.resolve_review_item(tmp_path / "reviews", rid, decision="approved", now=NOW)
    return rid                                           # recording works while in flight


def _run_governance(tmp_path, conn):
    kw = {"wiki_dir": tmp_path / "wiki", "markdown_dir": tmp_path / "normalized" / "markdown",
          "now": NOW}
    out = {}
    for res in (deprecations.apply_hidden_claims(conn, tmp_path / "reviews", **kw),
                deprecations.apply_unhidden_claims(conn, tmp_path / "reviews", **kw),
                deprecations.apply_approved_deprecations(
                    conn, tmp_path / "reviews", claims_dir=tmp_path / "wiki" / "Claims", **kw)):
        out.update({r["review_id"]: r for r in res["outcomes"]})
    conn.commit()
    return out


def test_claim_governance_waits_on_either_endpoint(tmp_path, monkeypatch):
    conn, rid = _world(tmp_path, extra=(C, D))
    conn = _to_edge_written(tmp_path, monkeypatch, conn)
    hide_a = _governance(tmp_path, "hide_claim", A, "hidden")
    unhide_b = _governance(tmp_path, "unhide_claim", B, "active")
    dep_b = _governance(tmp_path, "deprecate_wiki_page", B, "deprecated_candidate")
    hide_d = _governance(tmp_path, "hide_claim", D, "hidden")            # unrelated claim
    _contradiction(conn, B, C)
    conn.commit()
    other = _approve(tmp_path, B, C, winner=C, approval_id="4" * 32)     # a second supersede
    out = _run_governance(tmp_path, conn)
    for r in (hide_a, unhide_b, dep_b):
        assert out[r]["reason"] == "identity_operation_in_flight"
        assert out[r]["details"]["problem"]["blocking_review_id"] == rid
    assert out[hide_d]["outcome"] == "applied" and _status(conn, D) == "hidden"
    recs, _res = _apply(tmp_path, conn)
    assert recs[other]["reason"] == "identity_operation_in_flight"
    assert recs[rid]["outcome"] == "applied"             # the owner resumes itself
    assert _status(conn, A) == "active"                  # nothing ran on the endpoints meanwhile
    _end(tmp_path, rid)                                  # publication done: deferred work runs
    out = _run_governance(tmp_path, conn)
    assert out[hide_a]["outcome"] == "applied" and _status(conn, A) == "hidden"


def test_an_unreadable_journal_blocks_claim_governance_globally(tmp_path):
    conn, _rid_ = _world(tmp_path, extra=(D,))
    hide_d = _governance(tmp_path, "hide_claim", D, "hidden")
    bad = tmp_path / "reviews" / "in_flight" / ("rev_" + "9" * 16 + ".json")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{torn")
    out = _run_governance(tmp_path, conn)
    assert out[hide_d]["reason"] == "identity_operation_in_flight"
    # …and tag actions too (fail-closed), while a readable supersede journal blocks no tag
    assert identity_journal.operation_for(tmp_path / "reviews", ["itm_x"]) is not None
    bad.unlink()


def test_a_supersede_journal_does_not_block_tag_actions(tmp_path, monkeypatch):
    conn, rid = _world(tmp_path)
    conn = _to_edge_written(tmp_path, monkeypatch, conn)
    assert identity_journal.operation_for(tmp_path / "reviews", ["itm_" + "0" * 16]) is None
    assert identity_journal.operation_for(tmp_path / "reviews", [A]) == rid
    plan = tag_links._plan_repair(conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki") \
        if hasattr(tag_links, "_plan_repair") else None
    if plan is not None:
        assert rid not in plan["deferred"]


# --- decision 15: decisions on a retired contradiction ----------------------------------------


def test_a_rejection_of_a_retired_contradiction_is_information(tmp_path):
    conn = _graph(tmp_path)
    for cid in (A, B):
        _claim(conn, tmp_path, cid)
    _contradiction(conn, A, B, "superseded")
    conn.commit()
    rid = _approve(tmp_path, state="rejected")
    for _ in range(2):                                   # repeated Apply, no writes
        recs, res = _apply(tmp_path, conn)
        assert (recs[rid]["outcome"], recs[rid]["reason"]) == ("skipped", "contradiction_retired")
        assert recs[rid]["details"]["information"] is True and res["graph_changed"] is False
    # an owned supersede effect on the pair is not information
    graph.upsert_assertion(conn, src_id=A, dst_id=B, edge_type="supersedes", asserted_by="human",
                           status="active", review_id=rid)
    conn.commit()
    recs, _res = _apply(tmp_path, conn)
    assert recs[rid]["reason"] == "edge_status_unexpected"


def test_a_retired_contradiction_with_an_open_journal_follows_the_precedence(tmp_path, monkeypatch):
    conn, rid = _world(tmp_path)
    conn = _to_edge_written(tmp_path, monkeypatch, conn)
    _retire_winner(tmp_path, conn, A)
    recs, _res = _apply(tmp_path, conn)
    assert recs[rid]["reason"] == "partial_supersede_withdrawn"     # rollback, not information
    assert "information" not in recs[rid]["details"]


def test_a_later_scoped_check_keeps_the_terminal_decision(tmp_path):
    conn, rid = _world(tmp_path)
    _retire_winner(tmp_path, conn, B)
    _apply(tmp_path, conn)
    settings = SimpleNamespace(reviews_dir=tmp_path / "reviews", wiki_dir=tmp_path / "wiki",
                               markdown_dir=tmp_path / "normalized" / "markdown")
    window = {"a": f"src_{A[-4:]}", "b": f"src_{B[-4:]}", "claims_a": [A], "claims_b": [B],
              "id": "w"}
    counts = contradiction_check._file(conn, settings, {"fingerprints": {}}, window,
                                       {(A, B): {"confidence": 0.9, "explanation": "x"}},
                                       job_id="j", now=NOW)
    assert counts["decided"] == 1 and counts["proposed"] == 0
    assert {r["status"] for r in graph.contradiction_between(conn, A, B)} == {"superseded"}
    assert (tmp_path / "reviews" / "approved" / f"{rid}.json").exists()


# --- decision 16: the marker through every render ---------------------------------------------


def _mark(tmp_path, cid, lines):
    page = tmp_path / "wiki" / "Claims" / f"{cid}.md"
    page.write_text(page.read_text().replace("status: active", "status: active\n"
                                             + "\n".join(lines), 1))
    return page


def test_the_marker_survives_hide_and_unhide(tmp_path):
    conn, _rid_ = _world(tmp_path, extra=(D,))
    marker = {"review_id": "rev_" + "7" * 16, "approval_id": "8" * 32, "winner": A}
    _mark(tmp_path, D, claim_marker.lines(marker))
    _governance(tmp_path, "hide_claim", D, "hidden")
    _run_governance(tmp_path, conn)
    assert "status: hidden" in _page(tmp_path, D)
    assert claim_marker.parse(_page(tmp_path, D)) == (claim_marker.OK, marker)
    _governance(tmp_path, "unhide_claim", D, "active")
    _run_governance(tmp_path, conn)
    assert "status: active" in _page(tmp_path, D)
    assert claim_marker.parse(_page(tmp_path, D)) == (claim_marker.OK, marker)
    assert validate_frontmatter.main([str(tmp_path)]) == 0


@pytest.mark.parametrize("lines", [
    ['superseded_by_review: "rev_' + "7" * 16 + '"'],                       # partial
    ['superseded_by_review: "rev_7"', 'superseded_by_approval: "' + "8" * 32 + '"',
     'superseded_by_claim: "' + A + '"'],                                   # malformed value
    ['superseded_by_review: rev_' + "7" * 16, 'superseded_by_approval: "' + "8" * 32 + '"',
     'superseded_by_claim: "' + A + '"'],                                   # unquoted
])
def test_a_damaged_marker_leaves_the_page_untouched_and_unrelated_claims_continue(tmp_path, lines):
    conn, _rid_ = _world(tmp_path, extra=(D, E))
    page = _mark(tmp_path, D, lines)
    before = page.read_text()
    hide_d = _governance(tmp_path, "hide_claim", D, "hidden")
    hide_e = _governance(tmp_path, "hide_claim", E, "hidden")
    out = _run_governance(tmp_path, conn)
    assert out[hide_d]["reason"] == "marker_unreadable" and page.read_text() == before
    assert _status(conn, D) == "active"                  # the graph not updated either
    assert out[hide_e]["outcome"] == "applied"
    assert validate_frontmatter.main([str(tmp_path)]) == 1


def test_the_validator_accepts_a_complete_marker_in_every_lifecycle_state(tmp_path):
    conn, rid = _world(tmp_path)
    _apply(tmp_path, conn)
    assert "status: deprecated_candidate" in _page(tmp_path, B)
    assert validate_frontmatter.main([str(tmp_path)]) == 0


def test_set_stage_refuses_an_unreadable_journal(tmp_path):
    identity_journal.begin(tmp_path / "reviews", "rev_" + "6" * 16, op="supersede", nodes=[A, B])
    path = tmp_path / "reviews" / "in_flight" / ("rev_" + "6" * 16 + ".json")
    path.write_text("{torn")
    with pytest.raises(identity_journal.JournalUnreadable):
        identity_journal.set_stage(tmp_path / "reviews", "rev_" + "6" * 16, "edge_written")
    assert path.read_text() == "{torn"


def test_the_preview_reports_an_in_flight_supersede_as_not_reopenable(tmp_path, monkeypatch):
    conn, rid = _world(tmp_path)
    conn = _to_edge_written(tmp_path, monkeypatch, conn)
    item = json.loads((tmp_path / "reviews" / "approved" / f"{rid}.json").read_text())
    prev = review_read.project_review(item, gconn=conn, wiki_dir=tmp_path / "wiki")
    assert prev["apply"]["effect_status"] == review_read.UNKNOWN
    assert review_read.reopen_block_reason(prev["apply"]["effect_status"]) is not None


# --- slice 2 review round 1: barriers, unreadable authority, publication-owned cleanup ----------


def _failing_claims_dir_sync(monkeypatch, tmp_path):
    from app.backend import publication_queue as pq
    real = pq._sync_dir
    state = {"fail": True}
    target = tmp_path / "wiki" / "Claims"

    def sync(path):
        if state["fail"] and Path(path) == target:
            raise OSError("fsync failed")
        return real(path)

    monkeypatch.setattr(pq, "_sync_dir", sync)
    return state


def test_a_marked_page_is_certified_only_after_its_flush_succeeds(tmp_path, monkeypatch):
    conn, rid = _world(tmp_path)
    state = _failing_claims_dir_sync(monkeypatch, tmp_path)
    for _ in range(3):                                   # the first write, then repeated recovery
        with pytest.raises(OSError):
            _apply(tmp_path, conn)
        conn.close()
        conn = _reconnect(tmp_path)
        assert claim_marker.parse(_page(tmp_path, B))[0] == claim_marker.OK   # renamed into place
        assert _journal(tmp_path, rid)["stage"] == supersede.EDGE_WRITTEN  # never advanced
        assert _status(conn, B) == "active" and _audits(tmp_path) == []
    state["fail"] = False
    recs, _res = _apply(tmp_path, conn)
    assert recs[rid]["outcome"] == "applied"
    assert _journal(tmp_path, rid)["stage"] == supersede.LOSER_DEPRECATED
    assert _status(conn, B) == "deprecated_candidate" and len(_audits(tmp_path)) == 1


def test_a_recovered_journal_is_flushed_before_any_recovery_mutation(tmp_path, monkeypatch):
    conn, rid = _world(tmp_path)
    conn = _to_edge_written(tmp_path, monkeypatch, conn)
    real = identity_journal._sync_dir
    state = {"fail": True}

    def sync(path):
        if state["fail"] and Path(path) == tmp_path / "reviews" / "in_flight":
            raise OSError("fsync failed")
        return real(path)

    monkeypatch.setattr(identity_journal, "_sync_dir", sync)
    before = _page(tmp_path, B)
    for _ in range(2):
        with pytest.raises(OSError):
            _apply(tmp_path, conn)
        assert _page(tmp_path, B) == before and _status(conn, B) == "active"
    state["fail"] = False
    recs, _res = _apply(tmp_path, conn)
    assert recs[rid]["outcome"] == "applied"


def _break_frontmatter(path):
    text = path.read_text()
    head, sep, body = text.partition("\n---\n")          # drop the closing delimiter
    path.write_text(head + "\n" + body)


def test_broken_frontmatter_on_a_marked_page_is_unreadable_never_no_marker(tmp_path):
    conn, rid = _world(tmp_path)
    _apply(tmp_path, conn)
    _end(tmp_path, rid)
    page = tmp_path / "wiki" / "Claims" / f"{B}.md"
    _break_frontmatter(page)
    before = page.read_text()
    assert claim_marker.parse(before)[0] == claim_marker.UNREADABLE
    out = claims.recompose_claim(conn, cid=B, claims_dir=tmp_path / "wiki" / "Claims",
                                 reviews_dir=tmp_path / "reviews", now=NOW,
                                 markdown_dir=tmp_path / "normalized" / "markdown")
    assert out == "marker_unreadable" and page.read_text() == before
    bind = supersede.binding(json.loads((tmp_path / "reviews" / "approved"
                                         / f"{rid}.json").read_text()))
    assert supersede.completion_evidence(tmp_path / "reviews", tmp_path / "wiki" / "Claims",
                                         bind, None)[0] == "rollback_evidence_unreadable"
    item = json.loads((tmp_path / "reviews" / "approved" / f"{rid}.json").read_text())
    assert supersede.effect_state(conn, tmp_path / "reviews", tmp_path / "wiki" / "Claims",
                                  item)["state"] == supersede.UNVERIFIABLE


def test_a_marker_field_outside_the_frontmatter_is_unreadable(tmp_path):
    conn, _rid_ = _world(tmp_path, extra=(D,))
    page = tmp_path / "wiki" / "Claims" / f"{D}.md"
    page.write_text(page.read_text() + '\nsuperseded_by_review: "rev_' + "7" * 16 + '"\n')
    assert claim_marker.parse(page.read_text())[0] == claim_marker.UNREADABLE
    assert validate_frontmatter.main([str(tmp_path)]) == 1


@pytest.mark.parametrize("damage", ["partial", "broken_frontmatter"])
def test_publication_leaves_a_damaged_page_and_its_lagging_mirror_unchanged(tmp_path, damage):
    from app.workers import publication
    conn, rid = _world(tmp_path, extra=(D,))
    _apply(tmp_path, conn)
    _end(tmp_path, rid)
    page = tmp_path / "wiki" / "Claims" / f"{B}.md"
    if damage == "partial":
        page.write_text("\n".join(line for line in page.read_text().split("\n")
                                  if not line.startswith("superseded_by_claim")))
    else:
        _break_frontmatter(page)
    graph.upsert_node(conn, node_id=B, node_type="claim", slug=B, status="active")   # lagging
    conn.commit()
    before = page.read_text()
    failed: dict[str, str] = {}
    publication._render_claims(conn, [B, D], wiki_dir=tmp_path / "wiki", root=tmp_path, now=NOW,
                               failed=failed)
    assert failed == {B: "marker_unreadable"}            # the unrelated claim D rendered fine
    assert page.read_text() == before and _status(conn, B) == "active"


def _apply_settings(tmp_path, monkeypatch, *, pending):
    """A full Apply whose publication ends pending (or published) as asked."""
    from app.backend import main as main_module
    from app.backend.config import get_settings
    from app.workers import publication
    monkeypatch.setattr(publication.keyword_index, "reindex", lambda root: None)
    real = publication.publish

    def publish(*a, **kw):
        out = real(*a, **kw)
        if pending["on"]:
            out = {**out, "status": publication.PENDING}
        return out

    monkeypatch.setattr(main_module.publication, "publish", publish)
    return main_module, get_settings(tmp_path)


def test_a_supersede_journal_stays_while_publication_is_pending(tmp_path, monkeypatch):
    conn, rid = _world(tmp_path)
    conn.close()
    hide_a = _governance(tmp_path, "hide_claim", A, "hidden")      # the winner stays active
    pending = {"on": True}
    main_module, st = _apply_settings(tmp_path, monkeypatch, pending=pending)

    def outcomes():
        return {r["review_id"]: r for r in main_module.run_apply(st)["outcomes"]}

    by_id = outcomes()
    assert by_id[rid]["outcome"] == "applied"
    assert _journal(tmp_path, rid) is not None                       # kept: publication pending
    assert by_id[hide_a]["reason"] == "identity_operation_in_flight"
    assert outcomes()[hide_a]["reason"] == "identity_operation_in_flight"   # still pending
    assert _journal(tmp_path, rid) is not None
    pending["on"] = False
    by_id = outcomes()                                   # this Apply verifies, then closes it
    assert _journal(tmp_path, rid) is None
    assert by_id[hide_a]["reason"] == "identity_operation_in_flight"   # it ran before the close
    assert outcomes()[hide_a]["outcome"] == "applied"   # the waiting hide runs on the next Apply
    conn = _reconnect(tmp_path)
    assert _status(conn, A) == "hidden"
    conn.close()


@pytest.mark.parametrize("op,module", [("merge_items", "merges"), ("split_item", "splits")])
def test_merge_and_split_journals_stay_while_publication_is_pending(tmp_path, monkeypatch, op,
                                                                   module):
    from app.workers import merges, splits
    rid = "rev_" + "4" * 16
    identity_journal.begin(tmp_path / "reviews", rid, op=op, nodes=["itm_" + "1" * 16])
    target = {"merges": merges, "splits": splits}[module]
    fn = "apply_merges" if module == "merges" else "apply_splits"
    empty = {"applied": 0, "skipped": [], "changed_pages": [], "affected_sources": [],
             "journaled": [rid], "outcomes": []}
    monkeypatch.setattr(target, fn, lambda *a, **k: dict(empty))
    _graph(tmp_path).close()
    pending = {"on": True}
    main_module, st = _apply_settings(tmp_path, monkeypatch, pending=pending)
    main_module.run_apply(st)
    assert _journal(tmp_path, rid) is not None
    pending["on"] = False
    main_module.run_apply(st)
    assert _journal(tmp_path, rid) is None


# --- slice 2 review round 2: reserved marker keys under any spelling ---------------------------


_MR, _MA = "rev_" + "7" * 16, "8" * 32
_MARKER = {"review_id": _MR, "approval_id": _MA, "winner": A}


def _marked_text(lines=None, *, body=""):
    lines = claim_marker.lines(_MARKER) if lines is None else lines
    return "---\ntype: claim\nstatus: deprecated_candidate\n" + "\n".join(lines) + "\n---\n\nx\n" + body


def _spaced(field):
    return lambda t: t.replace(f"{field}:", f"{field} :", 1)


def _indented(field, pad="  "):
    return lambda t: t.replace(f"\n{field}:", f"\n{pad}{field}:", 1)


def _quoted(field, q='"'):
    return lambda t: t.replace(f"\n{field}:", f"\n{q}{field}{q}:", 1)


def _dup_winner(order):
    """A second, conflicting winner in a non-canonical spelling, before or after the real line."""
    def go(t):
        real = next(line for line in t.split("\n") if line.startswith(claim_marker.WINNER + ":"))
        other = f'{claim_marker.WINNER} : "{C}"'
        pair = f"{real}\n{other}" if order == "canonical_first" else f"{other}\n{real}"
        return t.replace(real, pair, 1)
    return go


def _all_spaced(t):
    for field in claim_marker.FIELDS:
        t = _spaced(field)(t)
    return t


_NON_CANONICAL = {
    "space_before_colon_all": _all_spaced,
    "space_before_colon_one": _spaced(claim_marker.APPROVAL),
    "tab_before_colon": lambda t: t.replace(f"{claim_marker.REVIEW}:", f"{claim_marker.REVIEW}\t:"),
    "indented_one": _indented(claim_marker.WINNER),
    "indented_tab": _indented(claim_marker.REVIEW, "\t"),
    "indented_all": lambda t: t.replace("\nsuperseded_by_", "\n  superseded_by_"),
    "double_quoted_key": _quoted(claim_marker.REVIEW),
    "single_quoted_key": _quoted(claim_marker.WINNER, "'"),
    "dup_canonical_first": _dup_winner("canonical_first"),
    "dup_spelled_first": _dup_winner("spelled_first"),
}


@pytest.mark.parametrize("variant", sorted(_NON_CANONICAL))
def test_a_non_canonically_keyed_marker_field_is_damaged_never_absent(variant):
    text = _NON_CANONICAL[variant](_marked_text())
    assert text != _marked_text()
    # the field IS present to some reader: the project's frontmatter parser (spacing, indentation,
    # duplicates) or YAML, as Obsidian reads it (quoted keys)
    fm = wiki_render.parse_frontmatter(text)
    if "quoted" in variant:
        yaml = pytest.importorskip("yaml")
        fm = yaml.safe_load(text.split("---\n")[1])
    assert set(claim_marker.FIELDS) <= set(fm)
    assert claim_marker.parse(text) == (claim_marker.DAMAGED, None)


def test_conflicting_duplicate_winners_are_damaged_in_both_orders():
    first = _NON_CANONICAL["dup_canonical_first"](_marked_text())
    last = _NON_CANONICAL["dup_spelled_first"](_marked_text())
    # the last-value parser reads a different winner in each order; neither order can hide one
    assert {wiki_render.parse_frontmatter(first)[claim_marker.WINNER],
            wiki_render.parse_frontmatter(last)[claim_marker.WINNER]} == {A, C}
    assert claim_marker.parse(first)[0] == claim_marker.parse(last)[0] == claim_marker.DAMAGED


@pytest.mark.parametrize("spelling", [
    f'  {claim_marker.REVIEW}: "{_MR}"', f'{claim_marker.APPROVAL} : "{_MA}"',
    f'"{claim_marker.WINNER}": "{A}"', f"'{claim_marker.REVIEW}' : x", f"\t{claim_marker.WINNER}:",
])
def test_a_reserved_field_outside_the_frontmatter_is_unreadable_under_any_spelling(spelling):
    assert claim_marker.parse(_marked_text(body=spelling + "\n"))[0] == claim_marker.UNREADABLE
    assert claim_marker.parse(_marked_text([], body=spelling + "\n"))[0] == claim_marker.UNREADABLE


@pytest.mark.parametrize("variant", ["crlf", "crlf_trailing_whitespace", "lf_trailing_whitespace"])
def test_line_endings_and_permitted_trailing_whitespace_are_not_damage(variant):
    text = _marked_text()
    if variant != "crlf":
        text = "\n".join(line + " \t" if line.startswith("superseded_by_") else line
                         for line in text.split("\n"))
    if variant.startswith("crlf"):
        text = text.replace("\n", "\r\n")
    assert claim_marker.parse(text) == (claim_marker.OK, _MARKER)


def test_a_commented_out_marker_line_is_not_a_field():
    assert claim_marker.parse(_marked_text([f'# {claim_marker.REVIEW}: "{_MR}"'])) == \
        (claim_marker.NONE, None)


def _completed_loser(tmp_path, extra=()):
    conn, rid = _world(tmp_path, extra=extra)
    _apply(tmp_path, conn)
    _end(tmp_path, rid)
    page = tmp_path / "wiki" / "Claims" / f"{B}.md"
    assert claim_marker.parse(page.read_text())[0] == claim_marker.OK
    return conn, rid, page


_RECOMPOSE_VARIANTS = ["space_before_colon_all", "indented_one", "double_quoted_key",
                       "dup_canonical_first", "dup_spelled_first"]


@pytest.mark.parametrize("variant", _RECOMPOSE_VARIANTS)
def test_recompose_refuses_a_non_canonically_keyed_marker_and_keeps_the_page(tmp_path, variant):
    conn, rid, page = _completed_loser(tmp_path)
    page.write_text(_NON_CANONICAL[variant](page.read_text()))
    before = page.read_text()
    out = claims.recompose_claim(conn, cid=B, claims_dir=tmp_path / "wiki" / "Claims",
                                 reviews_dir=tmp_path / "reviews", now=NOW,
                                 markdown_dir=tmp_path / "normalized" / "markdown")
    assert out == "marker_unreadable" and page.read_text() == before
    item = json.loads((tmp_path / "reviews" / "approved" / f"{rid}.json").read_text())
    assert supersede.effect_state(conn, tmp_path / "reviews", tmp_path / "wiki" / "Claims",
                                  item)["state"] == supersede.UNVERIFIABLE
    assert validate_frontmatter.main([str(tmp_path)]) == 1


@pytest.mark.parametrize("variant", _RECOMPOSE_VARIANTS)
def test_publication_leaves_a_non_canonically_keyed_page_and_its_mirror_unchanged(tmp_path,
                                                                                  variant):
    from app.workers import publication
    conn, _rid_, page = _completed_loser(tmp_path, extra=(D,))
    page.write_text(_NON_CANONICAL[variant](page.read_text()))
    graph.upsert_node(conn, node_id=B, node_type="claim", slug=B, status="active")   # lagging
    conn.commit()
    before = page.read_text()
    failed: dict[str, str] = {}
    publication._render_claims(conn, [B, D], wiki_dir=tmp_path / "wiki", root=tmp_path, now=NOW,
                               failed=failed)
    assert failed == {B: "marker_unreadable"}            # the unrelated claim D rendered fine
    assert page.read_text() == before and _status(conn, B) == "active"


def test_a_crlf_marked_page_recomposes_with_its_marker_kept(tmp_path):
    conn, rid, page = _completed_loser(tmp_path)
    _state, marker = claim_marker.parse(page.read_text())
    page.write_bytes(page.read_text().replace("\n", "\r\n").encode())
    assert claim_marker.parse(page.read_bytes().decode()) == (claim_marker.OK, marker)
    out = claims.recompose_claim(conn, cid=B, claims_dir=tmp_path / "wiki" / "Claims",
                                 reviews_dir=tmp_path / "reviews", now=NOW,
                                 markdown_dir=tmp_path / "normalized" / "markdown")
    assert out not in ("marker_unreadable", "marker_conflict")
    assert claim_marker.parse(page.read_text()) == (claim_marker.OK, marker)
    item = json.loads((tmp_path / "reviews" / "approved" / f"{rid}.json").read_text())
    assert supersede.effect_state(conn, tmp_path / "reviews", tmp_path / "wiki" / "Claims",
                                  item)["state"] == supersede.COMPLETE
