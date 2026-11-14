"""ADR-0074 decision 4, slice 3b: the read discipline for review records.

A review record is usable only as a JSON object whose `review_id` is its file name, with a `type` and
a `subject`. A damaged record is never withdrawn, re-filed over or decided on a guess; and because a
damaged *approved* record's subject is unknown, Apply (and its dry run, and every other path that
executes approved decisions) refuses as a whole until it is restored."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import apply_reviews  # noqa: E402

from app.backend import apply_authorization, apply_gate, authority_read, durable_write, manifests  # noqa: E402
from app.backend import publication_queue  # noqa: E402
from app.backend import main as main_module  # noqa: E402
from app.backend.config import get_settings  # noqa: E402
from app.workers import reviews  # noqa: E402
from tests.test_apply_dry_run import (  # noqa: E402
    _approve_archive, _graph_source_node, _rendered_source,
)

SID = "src_0000000000000a11"
NOW = "2026-11-12T00:00:00Z"


def _pending(rdir: Path, *, rtype="deprecate_wiki_page", subject=None) -> str:
    return reviews.create_review_item(rdir, review_type=rtype,
                                      subject=subject or {"node_id": "x", "page": "Items/x.md"},
                                      proposal={}, now=NOW)


# --- the typed record read ------------------------------------------------------------------------


@pytest.mark.parametrize("body, error", [
    (b"\xff", "invalid_utf8"), (b"{", "invalid_json"), (b"[]", "not_an_object"),
    (b'{"type": "x", "subject": {}}', "missing_review_id"),
    (b'{"review_id": "other", "type": "x", "subject": {}}', "id_mismatch"),
    (b'{"review_id": "rev_a", "subject": {}}', "missing_type"),
    (b'{"review_id": "rev_a", "type": "x", "status": "approved"}', "missing_subject"),
])
def test_a_record_without_usable_identity_is_corrupt(tmp_path, body, error):
    path = tmp_path / "rev_a.json"
    path.write_bytes(body)
    read = reviews.read_review_record(path)
    assert read.state == authority_read.CORRUPT and read.error == error and read.record is None


def test_an_unsupported_type_is_a_readable_record_not_damage(tmp_path):
    path = tmp_path / "rev_a.json"
    path.write_text(json.dumps({"review_id": "rev_a", "type": "from_the_future",
                                "status": "approved", "subject": {}}))
    assert reviews.read_review_record(path).state == authority_read.OK
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True)
    path.rename(d / "rev_a.json")
    assert reviews.approved_preflight(tmp_path / "reviews").records[0]["type"] == "from_the_future"


def test_a_scan_lists_damage_safely_and_never_reads_an_unlistable_dir_as_empty(tmp_path):
    rdir = tmp_path / "reviews"
    (rdir / "approved").mkdir(parents=True)
    (rdir / "approved" / "x\x1b[2J.json").write_text("{secret contents")
    (rdir / "approved" / "rev_b.json").mkdir()                        # not a regular file
    scan = reviews.scan_review_dir(rdir, "approved")
    assert not scan.complete
    assert {(d["path"], d["state"]) for d in scan.damaged} == {
        ("reviews/approved/rev_b.json", "unreadable"),
        ("reviews/approved/x\\x1b[2J.json", "corrupt")}
    assert "secret" not in json.dumps(scan.problems()) and "\x1b" not in json.dumps(scan.problems())
    (rdir / "pending").write_text("not a directory")
    assert reviews.scan_review_dir(rdir, "pending").enumeration_error == "NotADirectoryError"


# --- single-record actions never act on a guess ---------------------------------------------------


def test_a_damaged_pending_record_is_never_withdrawn_or_refiled(tmp_path):
    rdir = tmp_path / "reviews"
    rid = _pending(rdir)
    path = rdir / "pending" / f"{rid}.json"
    path.write_text("{truncated")
    before = path.read_bytes()
    assert reviews.withdraw_review_item(rdir, rid, reason="test", now=NOW) is False
    assert path.read_bytes() == before                                 # never deleted
    assert not list((rdir / "audit_log").glob("*.json")) if (rdir / "audit_log").exists() else True
    assert _pending(rdir) == rid and path.read_bytes() == before        # never re-filed over
    path.unlink()
    path.symlink_to(tmp_path / "gone")                                  # a dangling link
    assert _pending(rdir) == rid and path.is_symlink()                  # present, not absent


@pytest.mark.parametrize("where", ["pending", "approved"])
def test_human_actions_refuse_a_damaged_record(tmp_path, where):
    rdir = tmp_path / "reviews"
    rid = _pending(rdir)
    if where == "approved":
        reviews.resolve_review_item(rdir, rid, decision="approved", decided_by="human", now=NOW)
    path = rdir / where / f"{rid}.json"
    path.write_text("[]")
    before = path.read_bytes()
    actions = ([lambda: reviews.resolve_review_item(rdir, rid, decision="approved",
                                                    decided_by="human", now=NOW),
                lambda: reviews.defer_review_item(rdir, rid, now=NOW)] if where == "pending" else
               [lambda: reviews.reopen_review_item(rdir, rid, reason="test", now=NOW),
                lambda: reviews.resolve_review_item(rdir, rid, decision="rejected",
                                                    decided_by="human", now=NOW),
                lambda: reviews.decide_again(rdir, rid, decision="rejected", version_digest="d" * 64,
                                             decided_by="human", now=NOW)])
    for act in actions:
        with pytest.raises(reviews.ReviewRecordUnavailable):
            act()
    assert path.read_bytes() == before


def test_the_api_answers_409_for_a_decision_on_a_damaged_record(tmp_path, monkeypatch):
    st = get_settings(tmp_path)
    rid = _pending(st.reviews_dir)
    (st.reviews_dir / "pending" / f"{rid}.json").write_text("{")
    monkeypatch.setattr(main_module, "settings", st)
    res = TestClient(main_module.app).post(f"/reviews/{rid}/approve", json={})
    assert res.status_code == 409 and res.json()["detail"]["reason"] == "review_corrupt"


# --- Apply refuses as a whole ---------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A vault whose Apply preview is clean (one approved archive), plus a second approval."""
    st = get_settings(tmp_path)
    st.reviews_dir.mkdir(parents=True, exist_ok=True)
    _rendered_source(tmp_path, SID)
    _graph_source_node(tmp_path, SID)
    _approve_archive(tmp_path, SID)
    monkeypatch.setattr(main_module, "settings", st)
    return st


def _archived(root) -> bool:
    m = json.loads((Path(root) / "raw" / "manifests" / f"{SID}.json").read_text())
    return m.get("status") == "archive_candidate"


def _damage_one(st, how):
    approved = st.reviews_dir / "approved"
    if how == "corrupt":
        (approved / "rev_dead.json").write_text('{"review_id": "rev_dead", "secret": ')
    elif how == "missing_subject":
        (approved / "rev_dead.json").write_text(json.dumps({"review_id": "rev_dead",
                                                            "type": "hide_content",
                                                            "status": "approved"}))
    elif how == "unreadable":
        (approved / "rev_dead.json").mkdir()
    else:                                                     # approved/ can't be listed
        moved = approved.with_name("approved.moved")
        approved.rename(moved)
        approved.write_text("not a directory")


@pytest.mark.parametrize("how", ["corrupt", "missing_subject", "unreadable", "unlistable"])
def test_a_damaged_approval_blocks_the_dry_run_with_no_token(vault, how):
    st = vault
    assert apply_gate.preview(st, evaluate=main_module._evaluate_preview)["status"] == "ok"
    _damage_one(st, how)
    out = apply_gate.preview(st, evaluate=main_module._evaluate_preview)
    assert out["status"] == "blocked" and out["reason"] == "review_records_unavailable"
    assert out["preview_token"] is None
    problems = out["problems"]
    if how == "unlistable":
        assert problems["enumeration_error"] == "NotADirectoryError"
    else:
        assert problems["damaged"][0]["path"] == "reviews/approved/rev_dead.json"
    assert "secret" not in json.dumps(out)                              # never record contents
    page = TestClient(main_module.app).post("/ui/reviews/apply/preview")
    assert page.status_code == 200 and "Apply is refused" in page.text and "Apply now" not in page.text


def test_live_apply_refuses_before_any_change_and_keeps_its_token(vault):
    st = vault
    token = apply_gate.preview(st, evaluate=main_module._evaluate_preview)["preview_token"]
    _damage_one(st, "corrupt")
    queue_before = publication_queue.load(st.root)
    with pytest.raises(apply_gate.ApplyRefused) as exc:
        apply_gate.apply(st, token, run_apply=main_module.run_apply)
    assert exc.value.reason == "review_records_unavailable" and exc.value.status_code == 409
    assert exc.value.detail()["problems"]["damaged"][0]["state"] == "corrupt"
    assert not _archived(st.root)                                       # the readable one waits too
    assert apply_authorization.find(st.root, token) is not None         # token not consumed
    assert publication_queue.load(st.root) == queue_before
    res = TestClient(main_module.app).post("/reviews/apply", json={"preview_token": token})
    assert res.status_code == 409 and res.json()["detail"]["reason"] == "review_records_unavailable"
    # Restored: the same flow applies.
    (st.reviews_dir / "approved" / "rev_dead.json").unlink()
    token = apply_gate.preview(st, evaluate=main_module._evaluate_preview)["preview_token"]
    assert apply_gate.apply(st, token, run_apply=main_module.run_apply)["status"] == "applied"
    assert _archived(st.root)


def test_run_apply_refuses_again_as_the_backstop(vault):
    st = vault
    _damage_one(st, "corrupt")
    with pytest.raises(reviews.ReviewRecordsUnavailable):
        main_module.run_apply(st)
    assert not _archived(st.root) and publication_queue.load(st.root) == []


def test_the_apply_cli_reports_the_refusal(vault, capsys, monkeypatch):
    st = vault
    _damage_one(st, "corrupt")
    monkeypatch.chdir(st.root)
    assert apply_reviews.main(["preview", str(st.root)]) == 1
    out = capsys.readouterr().out
    assert "review_records_unavailable" in out and "reviews/approved/rev_dead.json" in out
    assert "secret" not in out


def test_other_paths_that_execute_approvals_share_the_preflight(vault):
    from app.workers import promote
    st = vault
    _damage_one(st, "corrupt")
    with pytest.raises(reviews.ReviewRecordsUnavailable):
        promote.promote_candidates(st.root, record_job=False, rebuild_index=False)


def test_synthesis_generation_skips_its_approval_step_and_reports_it(tmp_path):
    from tests.test_synthesis import SynthAdapter, _build, _gen
    jobs = _build(tmp_path)
    approved = tmp_path / "reviews" / "approved"
    approved.mkdir(parents=True, exist_ok=True)
    (approved / "rev_dead.json").write_text("{")
    out = _gen(tmp_path, SynthAdapter(), jobs)
    assert out["status"] == "partial" and out["promoted"] == 0
    assert out["review_records_unavailable"]["damaged"][0]["path"] == "reviews/approved/rev_dead.json"
    assert out["syntheses_written"] == 1                      # new proposals still generated


# --- review round 1 (3b) ------------------------------------------------------------------------


@pytest.mark.parametrize("body", ["{", json.dumps({"review_id": "other", "decision": "withdrawn"}),
                                  json.dumps({"review_id": "@", "decision": "rejected"})])
def test_a_keyed_withdrawal_never_relies_on_a_damaged_audit_entry(tmp_path, body):
    rdir = tmp_path / "reviews"
    rid = _pending(rdir)
    body = body.replace('"@"', json.dumps(rid))
    record = rdir / "pending" / f"{rid}.json"
    entry = rdir / "audit_log" / f"{rid}-withdrawn-mkey.json"
    entry.parent.mkdir(parents=True)
    entry.write_text(body)
    before = (record.read_bytes(), entry.read_bytes())
    with pytest.raises(reviews.ReviewRecordUnavailable):
        reviews.withdraw_review_item(rdir, rid, audit_key="mkey", reason="t", now=NOW)
    assert (record.read_bytes(), entry.read_bytes()) == before        # nothing deleted or rewritten


def test_a_valid_keyed_audit_entry_is_reused_on_a_retry(tmp_path):
    rdir = tmp_path / "reviews"
    rid = _pending(rdir)
    entry = rdir / "audit_log" / f"{rid}-withdrawn-mkey.json"
    entry.parent.mkdir(parents=True)
    entry.write_text(json.dumps({"review_id": rid, "decision": "withdrawn", "note": "first"}))
    assert reviews.withdraw_review_item(rdir, rid, audit_key="mkey", reason="t", now=NOW) is True
    assert json.loads(entry.read_text())["note"] == "first"            # kept, not replaced
    assert not (rdir / "pending" / f"{rid}.json").exists()


def test_a_proposal_refresh_never_relies_on_a_damaged_audit_entry(tmp_path):
    rdir = tmp_path / "reviews"
    rid = _pending(rdir)
    record = rdir / "pending" / f"{rid}.json"
    entry = rdir / "audit_log" / f"{rid}-proposal-refreshed-akey.json"
    entry.parent.mkdir(parents=True)
    entry.write_text("[]")
    before = (record.read_bytes(), entry.read_bytes())
    with pytest.raises(reviews.ReviewRecordUnavailable):
        reviews.refresh_pending_proposal(rdir, rid, proposal={"x": 1}, audit_key="akey", now=NOW)
    assert (record.read_bytes(), entry.read_bytes()) == before


def test_reopen_never_replaces_a_damaged_or_foreign_pending_record(tmp_path):
    rdir = tmp_path / "reviews"
    rid = _pending(rdir)
    reviews.resolve_review_item(rdir, rid, decision="approved", decided_by="human", now=NOW)
    approved, pending = rdir / "approved" / f"{rid}.json", rdir / "pending" / f"{rid}.json"
    for body in ("{corrupt", json.dumps({"review_id": rid, "type": "deprecate_wiki_page",
                                         "status": "pending",
                                         "subject": {"node_id": "other", "page": "Items/o.md"}})):
        pending.write_text(body)
        before = (approved.read_bytes(), pending.read_bytes())
        with pytest.raises(reviews.ReviewRecordUnavailable):
            reviews.reopen_review_item(rdir, rid, reason="t", now=NOW)
        assert (approved.read_bytes(), pending.read_bytes()) == before
        assert not list((rdir / "audit_log").glob(f"{rid}-reopened-*.json"))


def test_an_interrupted_reopen_is_finished(tmp_path):
    rdir = tmp_path / "reviews"
    rid = _pending(rdir)
    reviews.resolve_review_item(rdir, rid, decision="approved", decided_by="human", now=NOW)
    approved, pending = rdir / "approved" / f"{rid}.json", rdir / "pending" / f"{rid}.json"
    copy = json.loads(approved.read_text())
    generation = reviews.decision_generation(copy)
    for f in ("decided_by", "decided_at", "decision_note", "approval_id", "decision_id"):
        copy.pop(f, None)
    # The reopen's pending copy was written (naming the decision it reopens), then the crash.
    pending.write_text(json.dumps({**copy, "status": "pending", reviews.REOPENED_FROM: {
        "status": "approved", "generation": generation}}))
    assert reviews.finish_interrupted(rdir, rid) == "reopen"
    assert not approved.exists() and json.loads(pending.read_text())["status"] == "pending"
    assert reviews.reopen_review_item(rdir, rid, reason="t", now=NOW) is False    # nothing left


@pytest.mark.parametrize("record, error", [
    ({"proposal": ["invalid"]}, "invalid_proposal"),
    ({"context": "x"}, "invalid_context"),
    ({"subject": {}}, "invalid_subject"),                     # archive_source needs its source_id
    ({"subject": {"source_id": ""}}, "invalid_subject"),
    ({"status": None}, "missing_status"),
])
def test_a_supported_type_with_a_bad_shape_is_corrupt(tmp_path, record, error):
    path = tmp_path / "rev_a.json"
    base = {"review_id": "rev_a", "type": "archive_source", "status": "approved",
            "subject": {"source_id": SID}, "proposal": {"to_status": "archive_candidate"}}
    path.write_text(json.dumps({**base, **record}))
    assert reviews.read_review_record(path).error == error


def test_a_bad_shape_blocks_apply_before_any_write_and_keeps_the_token(vault):
    st = vault
    token = apply_gate.preview(st, evaluate=main_module._evaluate_preview)["preview_token"]
    path = st.reviews_dir / "approved" / "rev_arch.json"
    rec = json.loads(path.read_text())
    path.write_text(json.dumps({**rec, "proposal": ["invalid"]}))
    before, queue = path.read_bytes(), publication_queue.load(st.root)
    assert apply_gate.preview(st, evaluate=main_module._evaluate_preview)["reason"] == \
        "review_records_unavailable"
    with pytest.raises(apply_gate.ApplyRefused):
        apply_gate.apply(st, token, run_apply=main_module.run_apply)
    assert path.read_bytes() == before and publication_queue.load(st.root) == queue
    assert apply_authorization.find(st.root, token) is not None and not _archived(st.root)


@pytest.mark.parametrize("kind", ["dangling", "fifo", "directory"])
def test_decision_routes_report_damage_by_category_never_404(tmp_path, monkeypatch, kind):
    import os
    st = get_settings(tmp_path)
    rid = _pending(st.reviews_dir)
    path = st.reviews_dir / "pending" / f"{rid}.json"
    path.unlink()
    if kind == "dangling":
        path.symlink_to(tmp_path / "gone")
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    monkeypatch.setattr(main_module, "settings", st)
    client = TestClient(main_module.app)
    for route in ("approve", "reject", "defer"):
        res = client.post(f"/reviews/{rid}/{route}", json={})
        assert res.status_code == 409, (route, res.text)
        assert res.json()["detail"]["reason"] == "review_unreadable"
    assert client.post("/reviews/rev_missing/approve", json={}).status_code == 404


# --- review round 2: decision transitions, audit generations, non-blocking reads, links ----------


class _Crash(Exception):
    """A simulated power cut at one step of a transition: nothing after it runs."""


def _crash_at(monkeypatch, step: str):
    """Stop the next transition at `step` — `audit` (its audit entry), `record` (its destination
    record), `remove` (before the source is unlinked) or `remove_sync` (the unlink happens, its
    directory sync fails and the power cut loses the unlink: the entry is back). Returns `restore`."""
    real_write, real_remove = durable_write.write_json, durable_write.remove

    def write_json(path, obj=None, **kw):
        is_audit = Path(path).parent.name == "audit_log"
        if (step == "audit" and is_audit) or (step == "record" and not is_audit):
            raise _Crash(step)
        return real_write(path, obj, **kw)

    def remove(path, **kw):
        if step == "remove":
            raise _Crash(step)
        if step == "remove_sync":
            data = Path(path).read_bytes()
            real_remove(path, **kw)
            Path(path).write_bytes(data)                  # the unlink never reached the disk
            raise durable_write.UncertainCompletion(Path(path), OSError(5, "EIO"), "removed")
        return real_remove(path, **kw)

    monkeypatch.setattr(durable_write, "write_json", write_json)
    monkeypatch.setattr(durable_write, "remove", remove)

    def restore():
        monkeypatch.setattr(durable_write, "write_json", real_write)
        monkeypatch.setattr(durable_write, "remove", real_remove)
    return restore


def _slots(rdir: Path, rid: str):
    return {d: rdir / d / f"{rid}.json" for d in ("pending", "approved", "rejected")}


def _entries(rdir: Path, rid: str, event: str) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((rdir / "audit_log").glob(f"{rid}-{event}-*"))]


@pytest.mark.parametrize("step", ["audit", "record", "remove", "remove_sync"])
def test_an_interrupted_reopen_never_lets_the_revoked_approval_execute(tmp_path, monkeypatch, step):
    rdir = tmp_path / "reviews"
    rid = _pending(rdir)
    reviews.resolve_review_item(rdir, rid, decision="approved", decided_by="human", now=NOW)
    slot = _slots(rdir, rid)
    approval = json.loads(slot["approved"].read_text())
    restore = _crash_at(monkeypatch, step)
    with pytest.raises((_Crash, OSError)):
        reviews.reopen_review_item(rdir, rid, reason="misclick", now=NOW)
    restore()
    if step == "audit":                                   # nothing happened: the approval stands
        assert slot["approved"].exists() and not slot["pending"].exists()
        assert _entries(rdir, rid, "reopened") == []
        reviews.approved_preflight(rdir)
    elif step == "record":                                # the attempt is audited, nothing moved
        assert slot["approved"].exists() and not slot["pending"].exists()
        assert len(_entries(rdir, rid, "reopened")) == 1
    else:                                                 # in two folders: Apply refuses
        assert slot["approved"].exists() and slot["pending"].exists()
        with pytest.raises(reviews.ReviewRecordsUnavailable) as exc:
            reviews.approved_preflight(rdir)
        assert exc.value.scan.interrupted == [{"path": f"reviews/approved/{rid}.json",
                                               "also": f"reviews/pending/{rid}.json"}]
        assert "interrupted" in str(exc.value) and "backup" not in str(exc.value)
    # Recovery: the operator repeats the reopen. The decision is gone, once, with one audit entry.
    reviews.reopen_review_item(rdir, rid, reason="misclick", now=NOW)
    assert not slot["approved"].exists() and slot["pending"].exists()
    reopened = json.loads(slot["pending"].read_text())
    assert reopened["status"] == "pending" and "approval_id" not in reopened
    (entry,) = _entries(rdir, rid, "reopened")
    assert entry["prior_generation"] == approval["decision_id"] == approval["approval_id"]
    assert reviews.approved_preflight(rdir).records == []          # nothing left to execute


def test_repeating_the_reopen_through_the_api_finishes_it(tmp_path, monkeypatch):
    st = get_settings(tmp_path)
    rid = _pending(st.reviews_dir)
    reviews.resolve_review_item(st.reviews_dir, rid, decision="approved", decided_by="human",
                                now=NOW)
    restore = _crash_at(monkeypatch, "remove")
    with pytest.raises(_Crash):
        reviews.reopen_review_item(st.reviews_dir, rid, reason="t", now=NOW)
    restore()
    monkeypatch.setattr(main_module, "settings", st)
    res = TestClient(main_module.app).post(f"/reviews/{rid}/reopen", json={"reason": "again"})
    assert res.status_code == 200 and res.json()["reopened"] is True
    assert not _slots(st.reviews_dir, rid)["approved"].exists()


@pytest.mark.parametrize("step", ["audit", "record", "remove", "remove_sync"])
def test_an_interrupted_decision_is_finished_by_repeating_it(tmp_path, monkeypatch, step):
    rdir = tmp_path / "reviews"
    rid = _pending(rdir)
    slot = _slots(rdir, rid)
    restore = _crash_at(monkeypatch, step)
    with pytest.raises((_Crash, OSError)):
        reviews.resolve_review_item(rdir, rid, decision="approved", decided_by="human", now=NOW)
    restore()
    if step in ("audit", "record"):                       # nothing decided yet
        assert slot["pending"].exists() and not slot["approved"].exists()
        assert len(_entries(rdir, rid, "approved")) == (0 if step == "audit" else 1)
    else:
        assert slot["pending"].exists() and slot["approved"].exists()
        with pytest.raises(reviews.ReviewRecordsUnavailable):
            reviews.approved_preflight(rdir)
    reviews.resolve_review_item(rdir, rid, decision="approved", decided_by="human", now=NOW)
    assert slot["approved"].exists() and not slot["pending"].exists()
    record = json.loads(slot["approved"].read_text())
    # Exactly one audit entry is this decision's (an attempt cut before its record stays as evidence
    # under its own generation, matching no record).
    assert [e["decision_id"] for e in _entries(rdir, rid, "approved")].count(
        record["decision_id"]) == 1
    assert [r["review_id"] for r in reviews.approved_preflight(rdir).records] == [rid]


def test_a_re_decision_interrupted_after_a_reopen_keeps_the_new_decision(tmp_path, monkeypatch):
    rdir = tmp_path / "reviews"
    rid = _pending(rdir)
    reviews.resolve_review_item(rdir, rid, decision="approved", decided_by="human", now=NOW)
    reviews.reopen_review_item(rdir, rid, reason="t", now=NOW)
    restore = _crash_at(monkeypatch, "remove")
    with pytest.raises(_Crash):
        reviews.resolve_review_item(rdir, rid, decision="rejected", decided_by="human", now=NOW)
    restore()
    # The pending copy names the FIRST decision; the record in rejected/ is the new one.
    assert reviews.finish_interrupted(rdir, rid) == "decision"
    slot = _slots(rdir, rid)
    assert slot["rejected"].exists() and not slot["pending"].exists()
    assert reviews.REOPENED_FROM not in json.loads(slot["rejected"].read_text())


def test_disagreeing_records_in_two_folders_refuse_and_move_nothing(tmp_path):
    rdir = tmp_path / "reviews"
    rid = _pending(rdir)
    reviews.resolve_review_item(rdir, rid, decision="approved", decided_by="human", now=NOW)
    slot = _slots(rdir, rid)
    slot["pending"].write_text(json.dumps({
        "review_id": rid, "type": "deprecate_wiki_page", "status": "pending",
        "subject": {"node_id": "other", "page": "Items/o.md"}}))
    before = (slot["pending"].read_bytes(), slot["approved"].read_bytes())
    for act in (lambda: reviews.resolve_review_item(rdir, rid, decision="rejected"),
                lambda: reviews.reopen_review_item(rdir, rid, reason="t"),
                lambda: reviews.defer_review_item(rdir, rid),
                lambda: reviews.withdraw_review_item(rdir, rid)):
        with pytest.raises(reviews.ReviewRecordUnavailable):
            act()
    assert (slot["pending"].read_bytes(), slot["approved"].read_bytes()) == before


def test_every_decision_keeps_its_own_audit_entry(tmp_path):
    """Approve → reopen → approve → reopen → reject: every entry survives, each names its
    decision, and each reopen names the generation it reopened."""
    rdir = tmp_path / "reviews"
    rid = _pending(rdir)
    ids = []
    for decision, note in (("approved", "first"), ("approved", "second"), ("rejected", "third")):
        reviews.resolve_review_item(rdir, rid, decision=decision, decided_by="human", note=note,
                                    now=NOW)
        rec = json.loads(_slots(rdir, rid)[decision].read_text())
        ids.append(rec["decision_id"])
        assert rec.get("approval_id") == (rec["decision_id"] if decision == "approved" else None)
        if note != "third":
            reviews.reopen_review_item(rdir, rid, reason=f"after {note}", now=NOW)
    approved = {e["decision_id"]: e["note"] for e in _entries(rdir, rid, "approved")}
    assert approved == {ids[0]: "first", ids[1]: "second"}
    assert {e["decision_id"]: e["note"] for e in _entries(rdir, rid, "rejected")} == {ids[2]: "third"}
    assert [e["approval_id"] for e in _entries(rdir, rid, "approved")] == \
        [e["decision_id"] for e in _entries(rdir, rid, "approved")]
    reopened = sorted((rdir / "audit_log").glob(f"{rid}-reopened-*.json"))
    assert [p.name for p in reopened] == [f"{rid}-reopened-1.json", f"{rid}-reopened-2.json"]
    assert [json.loads(p.read_text())["prior_generation"] for p in reopened] == ids[:2]


def test_a_legacy_decision_reopens_by_its_digest_and_its_old_entry_is_kept(tmp_path):
    rdir = tmp_path / "reviews"
    rid = _pending(rdir)
    pending = _slots(rdir, rid)["pending"]
    legacy = {**json.loads(pending.read_text()), "status": "rejected", "decided_by": "human",
              "decided_at": "2026-01-01T00:00:00Z", "decision_note": ""}
    pending.unlink()
    (rdir / "rejected").mkdir()
    _slots(rdir, rid)["rejected"].write_text(json.dumps(legacy))
    (rdir / "audit_log").mkdir()
    old = rdir / "audit_log" / f"{rid}-rejected.json"
    old.write_text(json.dumps({"review_id": rid, "decision": "rejected"}))
    generation = reviews.decision_generation(legacy)
    assert generation.startswith("legacy-")
    assert reviews.reopen_review_item(rdir, rid, reason="t", now=NOW) is True
    (entry,) = _entries(rdir, rid, "reopened")
    assert entry["prior_generation"] == generation and old.exists()


# A FIFO under the write lock must never hang a writer: these routes run in a child process with a
# deadline, so a regression fails the test instead of hanging the suite.
_FIFO_PROBE = r"""
import json, os, sys
from pathlib import Path
from fastapi.testclient import TestClient
from app.backend import main as m
from app.backend.config import get_settings
from app.workers import reviews
st = get_settings(Path(sys.argv[1]))
m.settings = st
def pending(node):
    return reviews.create_review_item(st.reviews_dir, review_type="deprecate_wiki_page",
        subject={"node_id": node, "page": f"Items/{node}.md"}, proposal={},
        now="2026-11-14T00:00:00Z")
a, b, ok = pending("a"), pending("b"), pending("c")
reviews.resolve_review_item(st.reviews_dir, a, decision="approved", decided_by="human")
for rid, d in ((a, "approved"), (b, "pending")):
    p = st.reviews_dir / d / f"{rid}.json"
    p.unlink()
    os.mkfifo(p)
c = TestClient(m.app)
out = {}
r = c.post(f"/reviews/{a}/reopen", json={"reason": "t"})
out["reopen"] = [r.status_code, r.json()["detail"]["reason"]]
out["get"] = c.get(f"/reviews/{a}").status_code
out["ui_get"] = c.get(f"/ui/reviews/{a}").status_code
r = c.get("/reviews")
out["queue"] = [r.status_code, r.json()["parse_errors"]]
out["ui_queue"] = c.get("/ui/reviews").status_code
r = c.post("/ui/reviews/bulk", data={"bulk_action": "reject", "note": "", "rid": [b]})
out["bulk"] = [r.status_code, "record damaged (unreadable: not_a_regular_file)" in r.text]
out["other_writer"] = c.post(f"/reviews/{ok}/approve", json={}).status_code
print(json.dumps(out))
"""


def test_fifo_records_never_hang_reopen_bulk_or_the_queue(tmp_path):
    probe = tmp_path / "probe" / "fifo_probe.py"
    probe.parent.mkdir()
    probe.write_text(_FIFO_PROBE)
    vault = tmp_path / "vault"
    vault.mkdir()
    done = subprocess.run([sys.executable, str(probe), str(vault)], cwd=ROOT, capture_output=True,
                          text=True, timeout=120, env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out == {"reopen": [409, "review_unreadable"], "get": 409, "ui_get": 409,
                   "queue": [200, 1], "ui_queue": 200, "bulk": [200, True], "other_writer": 200}


def test_get_routes_answer_damage_with_409_never_404(tmp_path, monkeypatch):
    st = get_settings(tmp_path)
    rid = _pending(st.reviews_dir)
    (st.reviews_dir / "pending" / f"{rid}.json").write_text("{")
    monkeypatch.setattr(main_module, "settings", st)
    client = TestClient(main_module.app)
    res = client.get(f"/reviews/{rid}")
    assert res.status_code == 409 and res.json()["detail"]["reason"] == "review_corrupt"
    assert client.get(f"/ui/reviews/{rid}").status_code == 409
    assert client.post(f"/reviews/{rid}/reopen", json={"reason": "t"}).status_code == 409
    assert client.get("/reviews/rev_missing").status_code == 404


@pytest.mark.parametrize("where", ["review", "manifest"])
def test_an_authority_file_that_is_a_symlink_is_never_followed(tmp_path, where):
    outside = tmp_path / "outside"
    outside.mkdir()
    if where == "review":
        rdir = tmp_path / "reviews"
        rid = _pending(rdir)
        real = rdir / "pending" / f"{rid}.json"
        target = outside / "evil.json"
        target.write_bytes(real.read_bytes())             # a perfectly valid record, elsewhere
        real.unlink()
        real.symlink_to(target)
        read = reviews.read_review_record(real)
        (rdir / "approved").mkdir()
        (rdir / "approved" / f"{rid}.json").symlink_to(target)
        with pytest.raises(reviews.ReviewRecordsUnavailable):
            reviews.approved_preflight(rdir)
    else:
        mdir = tmp_path / "raw" / "manifests"
        mdir.mkdir(parents=True)
        target = outside / "m.json"
        target.write_text(json.dumps({"source_id": SID, "status": "active"}))
        (mdir / f"{SID}.json").symlink_to(target)
        read = manifests.read_manifest(mdir, SID)
    assert read.state == authority_read.UNREADABLE and read.error == "symlink"
    (outside / "gone").symlink_to(tmp_path / "nowhere")
    assert authority_read.read_file(outside / "gone").error == "dangling_symlink"


def test_a_linked_vault_directory_is_still_followed(tmp_path):
    real = tmp_path / "elsewhere" / "reviews"
    rid = _pending(real)
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "reviews").symlink_to(real)
    read = reviews.read_review_record(tmp_path / "vault" / "reviews" / "pending" / f"{rid}.json")
    assert read.state == authority_read.OK


@pytest.mark.parametrize("step", ["remove", "remove_sync"])
def test_deciding_after_an_interrupted_reopen_finishes_the_reopen_first(tmp_path, monkeypatch, step):
    """The page shows the reopened (pending) record; whatever the operator does next settles the
    reopen as a reopen — the revoked approval goes, and the new decision is recorded."""
    rdir = tmp_path / "reviews"
    rid = _pending(rdir)
    reviews.resolve_review_item(rdir, rid, decision="approved", decided_by="human", now=NOW)
    restore = _crash_at(monkeypatch, step)
    with pytest.raises((_Crash, OSError)):
        reviews.reopen_review_item(rdir, rid, reason="misclick", now=NOW)
    restore()
    assert reviews.resolve_review_item(rdir, rid, decision="rejected", decided_by="human",
                                       now=NOW) is True
    slot = _slots(rdir, rid)
    assert slot["rejected"].exists() and not slot["approved"].exists()
    assert not slot["pending"].exists()
