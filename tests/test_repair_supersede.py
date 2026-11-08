"""ADR-0073 slice 3 — supersede recovery (decision 11 as amended by decisions 18–23).

`scripts/repair_supersede.py` (`app/workers/supersede_repair.py`): read-only `inspect`, one governed
write `rollback-edge` that initiates the shared rollback, Apply finishing it and closing the journal;
the any-generation completion guard; the hardened publication authority check; the content
fingerprint; the run guards and exit codes; Apply's inspect wording.
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

from app.backend import graph, publication_queue, review_read, revision, run_guard  # noqa: E402
from app.workers import (claim_marker, claims, identity_journal,  # noqa: E402
                         publication, supersede)
from app.workers import supersede_repair as sr  # noqa: E402
from scripts import repair_supersede  # noqa: E402
from tests import test_guarded_supersede as gs  # noqa: E402

A, B, C, D, E = gs.A, gs.B, gs.C, gs.D, gs.E
NOW = gs.NOW
OTHER_RID = "rev_" + "7" * 16
OTHER_APPROVAL = "8" * 32


# --- fixtures ----------------------------------------------------------------------------------


def _edge_written(tmp_path, monkeypatch, *, extra=()):
    """An approved supersede interrupted after its edge: journal at `edge_written`, loser active."""
    conn, rid = gs._world(tmp_path, extra=extra)
    conn = gs._to_edge_written(tmp_path, monkeypatch, conn)
    conn.close()
    return rid


def _conn(tmp_path):
    return gs._reconnect(tmp_path)


def _inspect(tmp_path, rid):
    return sr.inspect(tmp_path, rid)


def _rollback(tmp_path, rid, fp=None, reason="the loser was re-checked by hand"):
    fp = fp or _inspect(tmp_path, rid)["fingerprint"]
    return sr.rollback_edge(tmp_path, rid, expected_state=fp, reason=reason, now=NOW)


def _approved(tmp_path, rid):
    return json.loads((tmp_path / "reviews" / "approved" / f"{rid}.json").read_text())


def _sup(tmp_path):
    conn = _conn(tmp_path)
    try:
        return gs._sup_edges(conn)
    finally:
        conn.close()


def _node(tmp_path, cid):
    conn = _conn(tmp_path)
    try:
        return gs._status(conn, cid)
    finally:
        conn.close()


def _repair_audits(tmp_path):
    return gs._audits(tmp_path, "repair-rollback")


def _queue(tmp_path):
    return publication_queue.load(tmp_path)


def _apply_executor(tmp_path):
    conn = _conn(tmp_path)
    try:
        recs, res = gs._apply(tmp_path, conn)
        return recs, res
    finally:
        conn.close()


def _effect(tmp_path, rid):
    conn = _conn(tmp_path)
    try:
        prev = review_read.project_review(_approved(tmp_path, rid), gconn=conn,
                                          wiki_dir=tmp_path / "wiki")
        return prev["apply"]["effect_status"]
    finally:
        conn.close()


def _completion_audit(tmp_path, rid, approval, *, winner=A, loser=B, body=None):
    d = tmp_path / "reviews" / "audit_log"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{rid}-superseded-{approval}.json"
    path.write_text(body if body is not None else json.dumps({
        "event": supersede.COMPLETED_EVENT, "review_id": rid, "approval_id": approval,
        "winner": winner, "loser": loser, "edge_id": "edg_old", "completed_at": NOW}))
    return path


def _set_page_status(tmp_path, cid, status):
    page = tmp_path / "wiki" / "Claims" / f"{cid}.md"
    text = page.read_text()
    for old in ("active", "deprecated_candidate", "hidden"):
        text = text.replace(f"\nstatus: {old}\n", f"\nstatus: {status}\n", 1)
    page.write_text(text)


def _set_node(tmp_path, cid, status):
    conn = _conn(tmp_path)
    graph.upsert_node(conn, node_id=cid, node_type="claim", slug=cid, status=status, now=NOW)
    conn.commit()
    conn.close()


# --- decision 18: rollback-edge initiates; Apply completes and closes --------------------------


def test_a_voluntary_rollback_records_intent_withdraws_the_edge_and_keeps_the_journal(
        tmp_path, monkeypatch):
    rid = _edge_written(tmp_path, monkeypatch)
    report = _inspect(tmp_path, rid)
    assert report["offer"] and report["command"].startswith(
        f"uv run python scripts/repair_supersede.py rollback-edge {rid} --expected-state "
        f"{report['fingerprint']}")
    assert "Apply can attempt to complete this supersede" in report["note"]
    out = _rollback(tmp_path, rid, report["fingerprint"])
    assert out["exit"] == sr.EXIT_OK and out["intent_recorded"]
    journal = gs._journal(tmp_path, rid)
    assert journal["stage"] == supersede.ROLLING_BACK
    assert journal["repair"]["actor"] == "repair_supersede"
    assert journal["repair"]["expected_state"] == report["fingerprint"]
    assert [e["status"] for e in _sup(tmp_path)] == ["superseded"]
    assert _node(tmp_path, B) == "active"
    (rb,) = gs._audits(tmp_path, "rolled-back")
    assert json.loads(rb.read_text())["approval_id"] == gs.APPROVAL      # the generation consumed
    (rp,) = _repair_audits(tmp_path)
    rec = json.loads(rp.read_text())
    assert rec["reason"] == "the loser was re-checked by hand" and rec["event"] == sr.supersede.REPAIR_EVENT
    assert gs._audits(tmp_path) == []                    # never completion evidence
    assert any(set(e["claims"]) == {A, B} for e in _queue(tmp_path))   # publication queued
    assert _effect(tmp_path, rid) == review_read.UNKNOWN  # in flight: reopen not yet available
    assert review_read.reopen_block_reason(review_read.UNKNOWN) is not None


def test_publication_is_queued_before_the_first_mutation(tmp_path, monkeypatch):
    rid = _edge_written(tmp_path, monkeypatch)
    seen = {}
    real = identity_journal.set_stage

    def spy(reviews_dir, review_id, stage, **kw):
        seen["queued_before_intent"] = any(set(e["claims"]) == {A, B} for e in _queue(tmp_path))
        return real(reviews_dir, review_id, stage, **kw)

    monkeypatch.setattr(identity_journal, "set_stage", spy)
    assert _rollback(tmp_path, rid)["exit"] == sr.EXIT_OK
    assert seen == {"queued_before_intent": True}


def _full_apply(tmp_path, monkeypatch, *, pending):
    main_module, st = gs._apply_settings(tmp_path, monkeypatch, pending=pending)
    return main_module, st, (lambda: {r["review_id"]: r
                                      for r in main_module.run_apply(st)["outcomes"]})


def test_apply_closes_the_journal_after_publication_then_reopen_succeeds(tmp_path, monkeypatch):
    rid = _edge_written(tmp_path, monkeypatch)
    assert _rollback(tmp_path, rid)["exit"] == sr.EXIT_OK
    pending = {"on": True}
    main_module, st, apply = _full_apply(tmp_path, monkeypatch, pending=pending)
    rec = apply()[rid]
    assert rec["reason"] == "partial_supersede_withdrawn"
    assert gs._journal(tmp_path, rid) is not None        # publication failed: journal retained
    assert _effect(tmp_path, rid) == review_read.UNKNOWN
    pending["on"] = False
    apply()
    assert gs._journal(tmp_path, rid) is None             # published: Apply closed it
    assert len(gs._audits(tmp_path, "rolled-back")) == 1 and len(_repair_audits(tmp_path)) == 1
    assert apply()[rid]["reason"] == "supersede_withdrawn"   # the approval never runs again
    assert _effect(tmp_path, rid) == review_read.PENDING_APPLY
    monkeypatch.setattr(main_module, "settings", st)
    out = main_module._reopen_decision(rid, "decide the contradiction again")
    assert out["status"] == "pending"
    assert (tmp_path / "reviews" / "pending" / f"{rid}.json").exists()


@pytest.mark.parametrize("boundary", ["intent", "edge", "rollback_audit", "repair_audit"])
def test_an_interrupted_repair_is_finished_by_apply_from_the_saved_intent(tmp_path, monkeypatch,
                                                                          boundary):
    rid = _edge_written(tmp_path, monkeypatch)
    fp = _inspect(tmp_path, rid)["fingerprint"]
    target = {"intent": (identity_journal, "set_stage"), "edge": (graph, "set_status"),
              "rollback_audit": (supersede, "_ensure_audit"),
              "repair_audit": (supersede, "_ensure_repair_audit")}[boundary]
    real = getattr(*target)

    def boom(*a, **kw):
        raise OSError("disk went away")

    monkeypatch.setattr(*target, boom)
    out = _rollback(tmp_path, rid, fp)
    monkeypatch.setattr(*target, real)
    assert out["exit"] == sr.EXIT_REFUSED
    journal = gs._journal(tmp_path, rid)
    if boundary == "intent":
        assert not out["intent_recorded"] and "before its intent was recorded" in out["message"]
        assert journal["stage"] == supersede.EDGE_WRITTEN and "repair" not in journal
        assert [e["status"] for e in _sup(tmp_path)] == ["active"]
        return
    assert out["intent_recorded"] and "durably recorded" in out["message"]
    assert "Nothing was changed" not in out["message"] and "Apply" in out["message"]
    assert journal["stage"] == supersede.ROLLING_BACK
    for _ in range(2):                                    # repeated recovery: idempotent
        recs, _res = _apply_executor(tmp_path)
        assert recs[rid]["reason"] == "partial_supersede_withdrawn"
    assert [e["status"] for e in _sup(tmp_path)] == ["superseded"]
    assert len(gs._audits(tmp_path, "rolled-back")) == 1
    (rp,) = _repair_audits(tmp_path)
    assert json.loads(rp.read_text())["expected_state"] == fp    # from the saved intent
    assert _node(tmp_path, B) == "active"                 # restored prerequisites never resume it


def test_repeated_invocation_is_stale_then_offers_no_new_write(tmp_path, monkeypatch):
    rid = _edge_written(tmp_path, monkeypatch)
    fp = _inspect(tmp_path, rid)["fingerprint"]
    assert _rollback(tmp_path, rid, fp)["exit"] == sr.EXIT_OK
    again = _rollback(tmp_path, rid, fp)
    assert again["exit"] == sr.EXIT_STATE_CHANGED and not again["wrote"]
    report = _inspect(tmp_path, rid)
    assert not report["offer"] and any("already recorded" in f for f in report["findings"])
    assert _rollback(tmp_path, rid, report["fingerprint"])["exit"] == sr.EXIT_REFUSED
    assert len(gs._audits(tmp_path, "rolled-back")) == 1 and len(_repair_audits(tmp_path)) == 1


def test_a_damaged_saved_intent_blocks_apply(tmp_path, monkeypatch):
    rid = _edge_written(tmp_path, monkeypatch)
    identity_journal.set_stage(tmp_path / "reviews", rid, supersede.ROLLING_BACK,
                               extra={"repair": {"repair_id": "nope"}})
    recs, _res = _apply_executor(tmp_path)
    assert recs[rid]["reason"] == "supersede_journal_invalid"
    assert [e["status"] for e in _sup(tmp_path)] == ["active"]


def test_a_conflicting_repair_audit_blocks_and_is_never_overwritten(tmp_path, monkeypatch):
    rid = _edge_written(tmp_path, monkeypatch)
    fp = _inspect(tmp_path, rid)["fingerprint"]
    monkeypatch.setattr(supersede, "_ensure_repair_audit",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    _rollback(tmp_path, rid, fp)
    monkeypatch.undo()
    repair_id = gs._journal(tmp_path, rid)["repair"]["repair_id"]
    path = supersede.repair_audit_path(tmp_path / "reviews", rid, repair_id)
    path.write_text('{"event": "something else"}')
    recs, _res = _apply_executor(tmp_path)
    assert recs[rid]["reason"] == "repair_audit_conflict"
    assert path.read_text() == '{"event": "something else"}'


# --- decision 19: strictly compensating; the any-generation guard ------------------------------


def test_no_journal_means_no_write_even_with_a_canonical_approval(tmp_path, monkeypatch):
    rid = _edge_written(tmp_path, monkeypatch)
    gs._end(tmp_path, rid)                                # lost metadata: the unexpected state
    report = _inspect(tmp_path, rid)
    assert not report["offer"] and report["command"] is None
    text = " ".join(report["findings"])
    assert "owns an active supersede link" in text and "generation wrote the link is unknown" in text
    assert "No safe repair determined" in text and "backup" in text
    out = _rollback(tmp_path, rid, report["fingerprint"])
    assert out["exit"] == sr.EXIT_REFUSED and not out["wrote"]
    assert [e["status"] for e in _sup(tmp_path)] == ["active"]
    assert gs._audits(tmp_path, "rolled-back") == []


def test_a_historical_marker_from_an_earlier_generation_forbids_rollback(tmp_path):
    conn, rid = gs._world(tmp_path)
    gs._apply(tmp_path, conn)                             # generation 1 completes (marker kept)
    gs._end(tmp_path, rid)
    conn.close()
    _set_page_status(tmp_path, B, "active")               # the loser later became active again
    _set_node(tmp_path, B, "active")
    gs._approve(tmp_path, approval_id="2" * 32)            # reopened and re-approved
    identity_journal.begin(tmp_path / "reviews", rid, op="supersede", nodes=[A, B],
                           binding={"winner": A, "loser": B, "approval_id": "2" * 32})
    identity_journal.set_stage(tmp_path / "reviews", rid, supersede.EDGE_WRITTEN)
    assert claim_marker.parse(gs._page(tmp_path, B))[1]["approval_id"] == gs.APPROVAL
    report = _inspect(tmp_path, rid)
    assert not report["offer"]
    assert any("Completion evidence exists (marker)" in f for f in report["findings"])
    bind = supersede.binding(_approved(tmp_path, rid))
    assert supersede.completion_evidence(tmp_path / "reviews", tmp_path / "wiki" / "Claims", bind,
                                         gs._journal(tmp_path, rid)) == (
        "completion_evidence_found", "marker")


@pytest.mark.parametrize("body,expected", [
    (None, "completion_evidence_found"),                        # a valid earlier-generation audit
    ("{torn", "rollback_evidence_unreadable"),                  # unreadable candidate
    (json.dumps({"event": "supersede_completed", "review_id": OTHER_RID, "approval_id": "9" * 32,
                 "winner": A, "loser": B, "edge_id": "e"}), "rollback_evidence_unreadable"),
])
def test_completion_audits_of_any_generation_are_checked(tmp_path, monkeypatch, body, expected):
    rid = _edge_written(tmp_path, monkeypatch)
    _completion_audit(tmp_path, rid, "9" * 32, body=body)
    assert not _inspect(tmp_path, rid)["offer"]
    bind = supersede.binding(_approved(tmp_path, rid))
    got = supersede.completion_evidence(tmp_path / "reviews", tmp_path / "wiki" / "Claims", bind,
                                        gs._journal(tmp_path, rid))
    assert got[0] == expected
    # the shared guard refuses Apply's own rollback too
    conn = _conn(tmp_path)
    gs._fail_prerequisites(conn)
    recs, _res = gs._apply(tmp_path, conn)
    conn.close()
    assert recs[rid]["reason"] == expected
    assert [e["status"] for e in _sup(tmp_path)] == ["active"]


def test_an_audit_directory_that_cant_be_scanned_blocks(tmp_path, monkeypatch):
    rid = _edge_written(tmp_path, monkeypatch)
    real = supersede.os.listdir

    def listdir(path):
        if Path(path).name == "audit_log":
            raise PermissionError("denied")
        return real(path)

    monkeypatch.setattr(supersede.os, "listdir", listdir)
    report = _inspect(tmp_path, rid)
    assert not report["offer"]
    assert any("can't be read (audit_log)" in f for f in report["findings"])
    assert _rollback(tmp_path, rid, report["fingerprint"])["exit"] == sr.EXIT_REFUSED


def test_a_hidden_loser_blocks(tmp_path, monkeypatch):
    rid = _edge_written(tmp_path, monkeypatch)
    _set_page_status(tmp_path, B, "hidden")
    _set_node(tmp_path, B, "hidden")
    report = _inspect(tmp_path, rid)
    assert not report["offer"] and any("not active in both" in f for f in report["findings"])


def test_two_owned_active_edges_block(tmp_path, monkeypatch):
    rid = _edge_written(tmp_path, monkeypatch)
    conn = _conn(tmp_path)
    graph.upsert_assertion(conn, src_id=B, dst_id=A, edge_type="supersedes", asserted_by="human",
                           status="active", review_id=rid, now=NOW)
    conn.commit()
    conn.close()
    report = _inspect(tmp_path, rid)
    assert not report["offer"] and any("owns 2 active" in f for f in report["findings"])


# --- decision 20: the hardened publication authority check --------------------------------------


def _render(tmp_path, cids):
    conn = _conn(tmp_path)
    failed: dict[str, str] = {}
    try:
        publication._render_claims(conn, cids, wiki_dir=tmp_path / "wiki", root=tmp_path, now=NOW,
                                   failed=failed)
    finally:
        conn.close()
    return failed


def test_a_wrong_owner_page_is_excluded_from_mirroring_and_rendering(tmp_path):
    conn, _rid_ = gs._world(tmp_path, extra=(D, E))
    conn.close()
    page = tmp_path / "wiki" / "Claims" / f"{D}.md"
    page.write_text(page.read_text().replace(f"claim_id: {D}", f"claim_id: {E}")
                    .replace(f'claim_id: "{D}"', f'claim_id: "{E}"')
                    .replace("\nstatus: active\n", "\nstatus: hidden\n"))
    assert claims.page_authority(page, D)["state"] == claims.IDENTITY_MISMATCH
    before = page.read_text()
    failed = _render(tmp_path, [D, E])
    assert failed == {D: "claim_identity_mismatch"}
    assert page.read_text() == before and _node(tmp_path, D) == "active"
    conn = _conn(tmp_path)
    assert claims.recompose_claim(conn, cid=D, claims_dir=tmp_path / "wiki" / "Claims",
                                  reviews_dir=tmp_path / "reviews", now=NOW,
                                  markdown_dir=tmp_path / "normalized" / "markdown") == \
        "claim_identity_mismatch"
    conn.close()
    assert page.read_text() == before


@pytest.mark.parametrize("status,mirrored", [("hidden", True), ("deprecated_candidate", True),
                                             ("bogus", False)])
def test_supported_statuses_are_mirrored_and_unsupported_ones_are_not(tmp_path, status, mirrored):
    conn, _rid_ = gs._world(tmp_path, extra=(D,))
    conn.close()
    _set_page_status(tmp_path, D, status) if status != "bogus" else (
        (tmp_path / "wiki" / "Claims" / f"{D}.md").write_text(
            gs._page(tmp_path, D).replace("\nstatus: active\n", "\nstatus: bogus\n")))
    conn = _conn(tmp_path)
    claims.mirror_page_status(conn, cid=D, claims_dir=tmp_path / "wiki" / "Claims", now=NOW)
    conn.commit()
    conn.close()
    assert _node(tmp_path, D) == (status if mirrored else "active")


@pytest.mark.parametrize("damage", ["partial", "spaced_key"])
def test_a_damaged_marker_mirrors_nothing(tmp_path, damage):
    conn, rid = gs._world(tmp_path)
    gs._apply(tmp_path, conn)
    gs._end(tmp_path, rid)
    conn.close()
    page = tmp_path / "wiki" / "Claims" / f"{B}.md"
    text = page.read_text()
    page.write_text("\n".join(x for x in text.split("\n") if not x.startswith(claim_marker.WINNER))
                    if damage == "partial" else text.replace(f"{claim_marker.REVIEW}:",
                                                             f"{claim_marker.REVIEW} :"))
    _set_node(tmp_path, B, "active")
    assert _render(tmp_path, [B]) == {B: "marker_unreadable"}
    assert _node(tmp_path, B) == "active"


def test_a_marked_page_is_mirrored_only_after_its_flush_succeeds(tmp_path, monkeypatch):
    conn, rid = gs._world(tmp_path)
    gs._apply(tmp_path, conn)                             # journal at loser_deprecated, open
    conn.close()
    _set_node(tmp_path, B, "active")                      # a lagging mirror
    real = claims.sync_page_durably
    monkeypatch.setattr(claims, "sync_page_durably",
                        lambda path: (_ for _ in ()).throw(OSError("fsync failed")))
    for _ in range(2):
        failed = _render(tmp_path, [A, B])
        assert failed[B].startswith("page_flush_failed") and A not in failed
        assert _node(tmp_path, B) == "active"
    monkeypatch.setattr(claims, "sync_page_durably", real)
    assert _render(tmp_path, [A, B]) == {}
    assert _node(tmp_path, B) == "deprecated_candidate"
    assert gs._journal(tmp_path, rid) is not None         # publication never touches the journal


def test_publication_repairs_a_lagging_mirror_and_apply_then_closes_the_journal(tmp_path,
                                                                               monkeypatch):
    conn, rid = gs._world(tmp_path)
    gs._apply(tmp_path, conn)
    conn.close()
    _set_node(tmp_path, B, "active")
    assert _render(tmp_path, [A, B]) == {}
    assert _node(tmp_path, B) == "deprecated_candidate"
    assert gs._journal(tmp_path, rid) is not None
    _main, _st, apply = _full_apply(tmp_path, monkeypatch, pending={"on": False})
    apply()
    assert gs._journal(tmp_path, rid) is None


def test_inspect_names_the_publication_route(tmp_path, monkeypatch):
    rid = _edge_written(tmp_path, monkeypatch)
    _set_node(tmp_path, B, "hidden")                      # graph disagrees with the active page
    text = " ".join(_inspect(tmp_path, rid)["findings"])
    assert "republish.py --all" in text
    from app.backend import write_lock
    with write_lock.hold(tmp_path / "reviews"):
        publication_queue.record(tmp_path, origin="test", claims=[B])
    assert "Retry publishing" in " ".join(_inspect(tmp_path, rid)["findings"])


# --- decision 21: when it is offered -------------------------------------------------------------


def test_rollback_after_a_foreign_marker_conflict(tmp_path, monkeypatch):
    rid = _edge_written(tmp_path, monkeypatch)
    gs._mark(tmp_path, B, claim_marker.lines({"review_id": OTHER_RID,
                                              "approval_id": OTHER_APPROVAL, "winner": C}))
    recs, _res = _apply_executor(tmp_path)
    assert recs[rid]["reason"] == "marker_conflict"       # Apply blocks and never rolls back
    report = _inspect(tmp_path, rid)
    assert report["offer"] and "marker_conflict" in report["note"] and OTHER_RID in report["note"]
    assert _rollback(tmp_path, rid, report["fingerprint"])["exit"] == sr.EXIT_OK
    assert [e["status"] for e in _sup(tmp_path)] == ["superseded"]
    assert len(gs._audits(tmp_path, "rolled-back")) == 1
    assert gs._journal(tmp_path, rid)["stage"] == supersede.ROLLING_BACK


def test_failed_prerequisites_say_apply_rolls_back_by_itself(tmp_path, monkeypatch):
    rid = _edge_written(tmp_path, monkeypatch)
    conn = _conn(tmp_path)
    gs._fail_prerequisites(conn)
    conn.close()
    report = _inspect(tmp_path, rid)
    assert report["offer"] and "rolls it back by itself" in report["note"]


def test_a_stale_inspection_writes_nothing(tmp_path, monkeypatch):
    rid = _edge_written(tmp_path, monkeypatch)
    fp = _inspect(tmp_path, rid)["fingerprint"]
    page = tmp_path / "wiki" / "Claims" / f"{A}.md"
    page.write_text(page.read_text() + "\n")             # a hand edit after inspect
    out = _rollback(tmp_path, rid, fp)
    assert out["exit"] == sr.EXIT_STATE_CHANGED and not out["wrote"]
    assert gs._journal(tmp_path, rid)["stage"] == supersede.EDGE_WRITTEN


def test_completion_evidence_appearing_after_inspect_refuses(tmp_path, monkeypatch):
    rid = _edge_written(tmp_path, monkeypatch)
    fp = _inspect(tmp_path, rid)["fingerprint"]
    _completion_audit(tmp_path, rid, "9" * 32)
    assert _rollback(tmp_path, rid, fp)["exit"] == sr.EXIT_STATE_CHANGED
    fresh = _inspect(tmp_path, rid)
    assert not fresh["offer"]
    assert _rollback(tmp_path, rid, fresh["fingerprint"])["exit"] == sr.EXIT_REFUSED
    # the shared entry point refuses on its own as well (defence in depth)
    conn = _conn(tmp_path)
    with pytest.raises(supersede.Blocked) as blk:
        supersede.initiate_rollback(conn, tmp_path / "reviews", _approved(tmp_path, rid),
                                    claims_dir=tmp_path / "wiki" / "Claims", now=NOW,
                                    repair={"repair_id": "rpr_" + "0" * 16,
                                            "actor": "repair_supersede", "reason": "x",
                                            "expected_state": "0" * 64, "initiated_at": NOW})
    conn.close()
    assert blk.value.reason == "completion_evidence_found"
    assert gs._journal(tmp_path, rid)["stage"] == supersede.EDGE_WRITTEN
    assert [e["status"] for e in _sup(tmp_path)] == ["active"]


# --- decision 22: the fingerprint -----------------------------------------------------------------


def test_duplicate_ledger_copies_refuse_the_write(tmp_path, monkeypatch):
    rid = _edge_written(tmp_path, monkeypatch)
    fp = _inspect(tmp_path, rid)["fingerprint"]
    (tmp_path / "reviews" / "pending").mkdir(parents=True, exist_ok=True)
    shutil.copy(tmp_path / "reviews" / "approved" / f"{rid}.json",
                tmp_path / "reviews" / "pending" / f"{rid}.json")
    report = _inspect(tmp_path, rid)
    assert report["fingerprint"] != fp and not report["offer"]
    assert any("copies in pending, approved" in f for f in report["findings"])
    assert _rollback(tmp_path, rid, report["fingerprint"])["exit"] == sr.EXIT_REFUSED


def test_a_fingerprint_from_another_vault_never_matches(tmp_path, monkeypatch):
    v1 = tmp_path / "v1"
    v1.mkdir()
    rid = _edge_written(v1, monkeypatch)
    v2 = tmp_path / "v2"
    shutil.copytree(v1, v2)
    fp1 = _inspect(v1, rid)["fingerprint"]
    assert _inspect(v2, rid)["fingerprint"] != fp1        # same bytes, other root
    assert _rollback(v2, rid, fp1)["exit"] == sr.EXIT_STATE_CHANGED


def _fp(tmp_path, rid):
    return _inspect(tmp_path, rid)["fingerprint"]


def test_the_fingerprint_covers_hand_edits_audits_and_foreign_edges(tmp_path, monkeypatch):
    rid = _edge_written(tmp_path, monkeypatch)
    fp = _fp(tmp_path, rid)
    page = tmp_path / "wiki" / "Claims" / f"{B}.md"
    page.write_text(page.read_text() + "\n")
    fp2 = _fp(tmp_path, rid)
    assert fp2 != fp
    audit = _completion_audit(tmp_path, rid, "9" * 32, body="{}")
    fp3 = _fp(tmp_path, rid)
    assert fp3 != fp2
    audit.unlink()
    assert _fp(tmp_path, rid) == fp2                      # removal is seen too
    conn = _conn(tmp_path)
    graph.upsert_assertion(conn, src_id=A, dst_id=B, edge_type="supersedes", asserted_by="human",
                           status="proposed", review_id=OTHER_RID, now=NOW)
    conn.commit()
    conn.close()
    assert _fp(tmp_path, rid) != fp2


def test_an_unrelated_action_leaves_the_fingerprint_unchanged(tmp_path, monkeypatch):
    rid = _edge_written(tmp_path, monkeypatch, extra=(D, E))
    fp = _fp(tmp_path, rid)
    conn = _conn(tmp_path)
    graph.upsert_assertion(conn, src_id=D, dst_id=E, edge_type="supersedes", asserted_by="human",
                           status="active", review_id=OTHER_RID, now=NOW)
    conn.commit()
    conn.close()
    (tmp_path / "reviews" / "audit_log").mkdir(parents=True, exist_ok=True)
    (tmp_path / "reviews" / "audit_log" / f"{OTHER_RID}-superseded-{'8' * 32}.json").write_text("{}")
    page = tmp_path / "wiki" / "Claims" / f"{D}.md"
    page.write_text(page.read_text() + "\n")
    from app.backend import write_lock
    with write_lock.hold(tmp_path / "reviews"):          # an unrelated commit advances the revision
        pass
    assert _fp(tmp_path, rid) == fp


# --- decision 23: guards, read-only inspect, exit codes, wording ---------------------------------


def _hold_guard(tmp_path, name):
    import fcntl
    path = run_guard.lock_path(tmp_path / "db", name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a", encoding="utf-8")                # noqa: SIM115 - released by the test
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fh


def _free(tmp_path, name):
    import fcntl
    with open(run_guard.lock_path(tmp_path / "db", name), "a", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fh, fcntl.LOCK_UN)
        return True


@pytest.mark.parametrize("name", sr.GUARDS)
def test_each_busy_guard_refuses_and_releases_the_rest(tmp_path, monkeypatch, name):
    rid = _edge_written(tmp_path, monkeypatch)
    fp = _fp(tmp_path, rid)
    held = _hold_guard(tmp_path, name)
    try:
        out = _rollback(tmp_path, rid, fp)
    finally:
        held.close()
    assert out["exit"] == sr.EXIT_BUSY and not out["wrote"] and "Nothing was changed" in out["message"]
    assert gs._journal(tmp_path, rid)["stage"] == supersede.EDGE_WRITTEN
    assert all(_free(tmp_path, g) for g in sr.GUARDS)    # every guard acquired earlier released
    assert not any((getattr(run_guard._held, "names", None) or {}).get(g) for g in sr.GUARDS)


def test_inspect_is_read_only(tmp_path, monkeypatch):
    rid = _edge_written(tmp_path, monkeypatch)
    shutil.rmtree(tmp_path / "reviews" / "audit_log", ignore_errors=True)
    rev_before = revision.revision_path(tmp_path).read_bytes() \
        if revision.revision_path(tmp_path).exists() else None
    queue_existed = publication_queue.queue_path(tmp_path).exists()
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    _inspect(tmp_path, rid)
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before
    after = revision.revision_path(tmp_path).read_bytes() \
        if revision.revision_path(tmp_path).exists() else None
    assert after == rev_before
    assert publication_queue.queue_path(tmp_path).exists() == queue_existed


def test_inspect_never_creates_a_missing_graph(tmp_path):
    report = _inspect(tmp_path, OTHER_RID)
    assert not (tmp_path / "db" / "graph.sqlite").exists()
    assert not report["offer"]


def test_the_exit_code_mapping(tmp_path, monkeypatch, capsys):
    rid = _edge_written(tmp_path, monkeypatch)
    root = ["--root", str(tmp_path)]
    assert repair_supersede.main(["--help"]) == 0
    assert repair_supersede.main(["inspect"]) == 2
    assert repair_supersede.main(["inspect", "rev_bad", *root]) == 2
    assert repair_supersede.main(["inspect", rid, *root]) == 0
    assert repair_supersede.main(["inspect", OTHER_RID, *root]) == 0     # no safe repair: still 0
    assert repair_supersede.main(["inspect", rid, "--json", *root]) == 0
    capsys.readouterr()
    fp = _fp(tmp_path, rid)
    assert repair_supersede.main(["rollback-edge", rid, "--expected-state", "x", "--reason", "r",
                                  *root]) == 2
    assert repair_supersede.main(["rollback-edge", rid, "--expected-state", "0" * 64,
                                  "--reason", "r", *root]) == 6
    queue = publication_queue.queue_path(tmp_path)
    saved = queue.read_bytes() if queue.exists() else None
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text("{torn")
    assert repair_supersede.main(["rollback-edge", rid, "--expected-state", fp, "--reason", "r",
                                  *root]) == 4
    if saved is None:
        queue.unlink()
    else:
        queue.write_bytes(saved)

    def no_revision(root):
        raise revision.RevisionUnavailable("advance", OSError("read-only disk"))

    monkeypatch.setattr(revision, "advance", no_revision)
    assert repair_supersede.main(["rollback-edge", rid, "--expected-state", fp, "--reason", "r",
                                  *root]) == 9
    monkeypatch.undo()
    gs._end(tmp_path, rid)                                 # no journal: a refused write
    assert repair_supersede.main(["rollback-edge", rid, "--expected-state", _fp(tmp_path, rid),
                                  "--reason", "r", *root]) == 1


def test_apply_names_the_inspect_command_for_blocked_supersedes():
    from app.backend import apply_readable as ar
    from app.workers import apply_outcomes as ao
    rid = "rev_" + "5" * 16
    blocked = ao.outcome(rid, "resolve_contradiction", ao.SKIPPED,
                         reason="supersede_state_unverified")
    groups = {"changes": [], "in_effect": [], "information": [], "blocked": [blocked],
              "incomplete": [], "not_reported": []}
    html = ar.render(groups, preview=False)
    assert f"uv run python scripts/repair_supersede.py inspect {rid}" in html
    partial = ao.outcome(rid, "resolve_contradiction", ao.INCOMPLETE,
                         reason="partial_supersede_withdrawn")
    assert ar.recovery_text(partial) == ("Finish recovery with Apply, then reopen if another "
                                         "attempt is wanted.")
    assert "reopen the review" not in ar.reason_text("partial_supersede_withdrawn", {})
    hide = ao.outcome(rid, "hide_claim", ao.INCOMPLETE, reason="marker_unreadable")
    assert "repair_supersede" not in ar.recovery_text(hide)
    sup = ao.outcome(rid, "resolve_contradiction", ao.INCOMPLETE, reason="marker_unreadable")
    assert "validate_all.py" in ar.recovery_text(sup) and "inspect" in ar.recovery_text(sup)
    in_flight = ao.outcome(rid, "resolve_contradiction", ao.SKIPPED, reason="supersede_in_flight")
    assert ar.inspect_hint(in_flight) == ""


def test_the_lint_names_the_inspect_command(tmp_path, monkeypatch):
    rid = _edge_written(tmp_path, monkeypatch)
    gs._end(tmp_path, rid)
    from app.workers import lint
    conn = _conn(tmp_path)
    try:
        findings = lint._check_supersedes(conn, tmp_path / "wiki", tmp_path / "reviews")
    finally:
        conn.close()
    (f,) = [x for x in findings if x["check"] == "supersede_state_unverified"]
    assert f"repair_supersede.py inspect {rid}" in f["detail"]

