"""ADR-0074 decision 3, slice 4: operator recovery of a damaged manifest
(`scripts/repair_manifest.py`, `app/workers/manifest_repair.py`), and the Details history it relies
on (every Details change is audited before the manifest changes; backups carry the marker)."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import backup  # noqa: E402
import repair_manifest  # noqa: E402

from app.backend import durable_write, graph, manifests, publication_queue, revision  # noqa: E402
from app.workers import manifest_repair as mr  # noqa: E402

RAW = b"the raw bytes of one document\n"
SHA = hashlib.sha256(RAW).hexdigest()
SID = manifests.source_id_for(SHA)
OLD = "2000-01-01T00:00:00+00:00"
FUTURE = "2999-01-01T00:00:00+00:00"
DAMAGED = b'{"source_id": "' + SID.encode() + b'", "sha256": '


def _vault(tmp_path: Path, **extra) -> Path:
    root = tmp_path / "vault"
    raw = root / "raw" / "inbox" / "doc.txt"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(RAW)
    manifests.save_manifest(root / "raw" / "manifests", {
        "source_id": SID, "sha256": SHA, "relative_raw_path": "raw/inbox/doc.txt",
        "status": "active", **extra})
    (root / "reviews").mkdir()
    return root


def _mdir(root: Path) -> Path:
    return root / "raw" / "manifests"


def _manifest(root: Path) -> Path:
    return manifests.manifest_path(_mdir(root), SID)


def _damage(root: Path, data: bytes = DAMAGED) -> None:
    _manifest(root).write_bytes(data)


def _backup(root: Path) -> Path:
    return backup.create_backup(root)


def _snapshot(root: Path) -> dict[str, bytes | None]:
    skip = {".lock", ".vault.lock"}
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() and not p.is_symlink() else None)
            for p in sorted(root.rglob("*"))
            if p.name not in skip and not p.name.startswith(".run-") and "backups" not in p.parts}


def _governance(root: Path, rtype: str, decision: str, at: str, name: str = "approved") -> None:
    audit = root / "reviews" / "audit_log"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / f"rev_00000000000000a1-{name}-x.json").write_text(json.dumps({
        "review_id": "rev_00000000000000a1", "type": rtype, "decision": decision,
        "decided_at": at, "subject": {"source_id": SID}}))


def _confirm(root: Path, now: str) -> None:
    manifests.confirm_details(_mdir(root), SID, title="A Title", authors=[], publisher=None,
                              baseline="b", now=now)


def _restore(root: Path, report: dict, archive: Path | None = None) -> int:
    archive = archive or Path(next(iter(report["offers"].values())).split("--archive ")[1].split(" ")[0])
    return repair_manifest.main(["restore", SID, "--archive", str(archive), "--expected-state",
                                 report["fingerprint"], "--reason", "test", "--root", str(root)])


# --- Details history ---------------------------------------------------------------------------


def test_confirm_and_clear_write_their_audit_record_before_the_manifest(tmp_path, monkeypatch):
    root = _vault(tmp_path)
    _confirm(root, "2026-11-15T10:00:00+00:00")
    manifests.clear_details(_mdir(root), SID)
    records = sorted((root / "reviews" / "audit_log").glob(f"{SID}-details-*.json"))
    events = sorted(json.loads(p.read_text())["event"] for p in records)
    assert events == ["details_cleared", "details_confirmed"]
    assert all(json.loads(p.read_text())["details_history"] == 1 for p in records)
    # Audit first: a manifest write that fails still leaves the record (conservative).
    monkeypatch.setattr(manifests, "save_manifest", lambda *a, **k: (_ for _ in ()).throw(OSError))
    before = _manifest(root).read_bytes()
    with pytest.raises(OSError):
        _confirm(root, "2026-11-15T11:00:00+00:00")
    assert _manifest(root).read_bytes() == before
    assert len(list((root / "reviews" / "audit_log").glob(f"{SID}-details-*.json"))) == 3


def test_a_damaged_manifest_writes_no_details_record(tmp_path):
    root = _vault(tmp_path)
    _damage(root)
    with pytest.raises(manifests.ManifestUnavailable):
        _confirm(root, OLD)
    assert not (root / "reviews" / "audit_log").exists()


def test_backups_carry_the_details_history_marker(tmp_path):
    root = _vault(tmp_path)
    with zipfile.ZipFile(_backup(root)) as zf:
        assert json.loads(zf.read(backup._BACKUP_MANIFEST_NAME))["details_history"] == 1


# --- inspect -----------------------------------------------------------------------------------


def test_inspect_reports_damage_and_a_compatible_copy_and_writes_nothing(tmp_path, capsys):
    root = _vault(tmp_path)
    _backup(root)
    _damage(root)
    before = _snapshot(root)
    assert repair_manifest.main(["inspect", SID, "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "corrupt (invalid_json)" in out
    assert f"{len(DAMAGED)} bytes, sha256 {hashlib.sha256(DAMAGED).hexdigest()}" in out
    assert "raw/inbox/doc.txt (sha256 verified)" in out
    assert "compatible" in out and "scripts/repair_manifest.py restore" in out
    assert DAMAGED.decode() not in out                         # never the contents
    assert _snapshot(root) == before
    assert not revision.revision_path(root).exists()


def test_inspect_is_deterministic_and_a_new_backup_changes_the_fingerprint(tmp_path):
    root = _vault(tmp_path)
    _backup(root)
    _damage(root)
    first = mr.inspect(root, SID)["fingerprint"]
    assert mr.inspect(root, SID)["fingerprint"] == first
    _backup(root)
    assert mr.inspect(root, SID)["fingerprint"] != first


@pytest.mark.parametrize("damage, finding", [
    ("absent", "absent — nothing to recover"),
    ("dangling", "unreadable — there is no write path"),
    ("ok", "nothing to restore"),
])
def test_only_a_corrupt_manifest_has_a_write_path(tmp_path, damage, finding):
    root = _vault(tmp_path)
    _backup(root)
    if damage == "absent":
        _manifest(root).unlink()
    elif damage == "dangling":
        _manifest(root).unlink()
        os.symlink(root / "nowhere", _manifest(root))
    report = mr.inspect(root, SID)
    assert any(finding in f for f in report["findings"])
    assert report["offers"] == {} and report["finish"] is None


# --- restore -----------------------------------------------------------------------------------


def test_restore_quarantines_records_intent_replaces_publishes_audits_and_closes(tmp_path):
    root = _vault(tmp_path)
    archive = _backup(root)
    good = _manifest(root).read_bytes()
    _damage(root)
    assert _restore(root, mr.inspect(root, SID), archive) == 0
    assert _manifest(root).read_bytes() == good
    quarantined = list(mr.quarantine_dir(root).glob(f"{SID}-*.json"))
    assert [p.read_bytes() for p in quarantined] == [DAMAGED]  # copied, kept
    audit = list((root / "reviews" / "audit_log").glob(f"{SID}-manifest-restored-*.json"))
    rec = json.loads(audit[0].read_text())
    assert rec["event"] == "manifest_restored" and rec["archive"] == archive.name
    assert rec["damaged_sha256"] == hashlib.sha256(DAMAGED).hexdigest()
    assert not mr.journal_path(root, SID).exists()
    inv = manifests.scan_manifests(_mdir(root))                # quarantine/ is never a manifest
    assert [m["source_id"] for m in inv.valid] == [SID] and inv.complete
    assert backup._catalogued_raw_index(root) == {"raw/inbox/doc.txt": SHA}
    assert any(f.startswith("The manifest reads") for f in mr.inspect(root, SID)["findings"])


def test_restore_refuses_a_changed_state_and_changes_nothing(tmp_path):
    root = _vault(tmp_path)
    _backup(root)
    _damage(root)
    report = mr.inspect(root, SID)
    _damage(root, DAMAGED + b"x")                              # damaged differently since
    before = _snapshot(root)
    assert _restore(root, report) == mr.EXIT_STATE_CHANGED
    assert _snapshot(root) == before
    assert not revision.revision_path(root).exists()


def test_restore_refuses_an_archive_outside_the_backups_folder(tmp_path):
    root = _vault(tmp_path)
    archive = _backup(root)
    _damage(root)
    elsewhere = tmp_path / "elsewhere.zip"
    elsewhere.write_bytes(archive.read_bytes())
    before = _snapshot(root)
    assert _restore(root, mr.inspect(root, SID), elsewhere) == mr.EXIT_REFUSED
    assert _snapshot(root) == before


def _hand_made_archive(root: Path, members: list[tuple[str, bytes]], name: str = "hand.zip") -> Path:
    out = root / "backups" / name
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w") as zf:
        for member, data in members:
            zf.writestr(member, data)
    return out


def _sidecar(**extra) -> bytes:
    return json.dumps({"format": "knowledge-system-backup/1", "created_at": "20991231T000000Z",
                       **extra}).encode()


def _copy(**changes) -> bytes:
    return json.dumps({"source_id": SID, "sha256": SHA, "relative_raw_path": "raw/inbox/doc.txt",
                       "status": "active", **changes}).encode()


def test_an_unmarked_backup_is_never_compatible(tmp_path):
    root = _vault(tmp_path)
    _hand_made_archive(root, [(f"raw/manifests/{SID}.json", _copy()),
                              (backup._BACKUP_MANIFEST_NAME, _sidecar())])   # no details_history
    _damage(root)
    report = mr.inspect(root, SID)
    [row] = report["copies"]
    assert any("details history unknown" in r for r in row["incompatible"])
    assert report["offers"] == {}


@pytest.mark.parametrize("members, reason", [
    ([(f"raw/manifests/{SID}.json", _copy()), (f"raw/manifests/{SID}.json", _copy())],
     "duplicate_member"),
    ([(f"raw/manifests/{SID}.json", b" " * (mr.MAX_MEMBER_BYTES + 1))], "oversized_member"),
    ([(f"raw/manifests/{SID}.json", _copy(sha256="0" * 64))], "copy_sha256_disagrees_with_id"),
    ([(f"raw/manifests/{SID}.json", _copy(sha256=SHA[:16]))], "copy_sha256_malformed"),
    ([(f"raw/manifests/{SID}.json", _copy(status="bogus"))], "copy_status_invalid"),
    ([(f"raw/manifests/{SID}.json", _copy(source_id="src_" + "f" * 16))], "copy_id_mismatch"),
    ([(f"raw/manifests/{SID}.json", _copy(relative_raw_path="../../etc/passwd"))],
     "copy_raw_path_escapes_raw"),
])
def test_an_unusable_archive_copy_is_rejected(tmp_path, members, reason):
    import warnings
    root = _vault(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")                        # zipfile warns on a duplicate name
        _hand_made_archive(root, [*members, (backup._BACKUP_MANIFEST_NAME,
                                             _sidecar(details_history=1))])
    _damage(root)
    report = mr.inspect(root, SID)
    assert [c["reason"] for c in report["copies"]] == [reason]
    assert report["offers"] == {}


def test_a_copy_that_disagrees_with_the_raw_bytes_is_rejected(tmp_path):
    root = _vault(tmp_path)
    _backup(root)
    (root / "raw" / "inbox" / "doc.txt").write_bytes(b"different bytes")
    _damage(root)
    assert [c["reason"] for c in mr.inspect(root, SID)["copies"]] == ["copy_disagrees_with_raw_bytes"]


def test_a_missing_raw_file_is_reported_unavailable_not_guessed(tmp_path):
    root = _vault(tmp_path)
    _backup(root)
    (root / "raw" / "inbox" / "doc.txt").unlink()
    _damage(root)
    report = mr.inspect(root, SID)
    assert report["facts"]["raw file"].startswith("unavailable")
    assert report["copies"][0]["raw"]["state"] == "absent" and report["offers"]


# --- compatibility with later human decisions --------------------------------------------------


@pytest.mark.parametrize("later", ["confirm", "clear", "interrupted"])
def test_a_details_change_after_the_backup_blocks_restore(tmp_path, monkeypatch, later):
    root = _vault(tmp_path)
    _backup(root)
    if later == "confirm":
        _confirm(root, FUTURE)
    elif later == "clear":
        manifests.clear_details(_mdir(root), SID)              # at = now, after the backup
    else:                                                      # audit written, change never made
        monkeypatch.setattr(manifests, "save_manifest",
                            lambda *a, **k: (_ for _ in ()).throw(OSError))
        with pytest.raises(OSError):
            _confirm(root, FUTURE)
        monkeypatch.undo()
    _damage(root)
    report = mr.inspect(root, SID)
    assert any("Details change" in r for r in report["copies"][0]["incompatible"])
    assert report["offers"] == {}


def test_a_details_change_before_the_backup_is_in_the_copy(tmp_path):
    root = _vault(tmp_path)
    _confirm(root, OLD)
    _backup(root)
    _damage(root)
    report = mr.inspect(root, SID)
    assert report["copies"][0]["details"]["title"] == "A Title" and report["offers"]


@pytest.mark.parametrize("rtype, decision, at, blocks", [
    ("hide_content", "approved", FUTURE, True),
    ("archive_source", "approved", FUTURE, True),
    ("unhide_content", "decided_again", FUTURE, True),
    ("hide_content", "rejected", FUTURE, False),               # no change of the document
    ("hide_content", "approved", OLD, False),                  # already in the copy
    ("merge_items", "approved", FUTURE, False),                # not a manifest governance type
])
def test_governance_decisions_after_the_backup_block_restore(tmp_path, rtype, decision, at, blocks):
    root = _vault(tmp_path)
    _backup(root)
    _governance(root, rtype, decision, at)
    _damage(root)
    report = mr.inspect(root, SID)
    assert bool(report["copies"][0]["incompatible"]) is blocks
    assert bool(report["offers"]) is not blocks


@pytest.mark.parametrize("damage", ["unlistable", "unreadable_review", "untimed_governance",
                                    "unreadable_details"])
def test_unknown_audit_history_is_not_compatible(tmp_path, damage):
    root = _vault(tmp_path)
    _backup(root)
    audit = root / "reviews" / "audit_log"
    if damage == "unlistable":
        audit.write_text("not a directory")
    elif damage == "unreadable_review":
        audit.mkdir()
        os.symlink(root / "nowhere", audit / "rev_00000000000000b2-approved-x.json")
    elif damage == "untimed_governance":
        _governance(root, "hide_content", "approved", "not a time")
    else:
        audit.mkdir()
        (audit / f"{SID}-details-confirmed-x.json").write_bytes(b"\xff")
    _damage(root)
    report = mr.inspect(root, SID)
    assert any("audit history is incomplete" in r for r in report["copies"][0]["incompatible"])
    assert report["offers"] == {}


def test_damaged_audit_records_of_other_subjects_dont_block(tmp_path):
    root = _vault(tmp_path)
    _backup(root)
    audit = root / "reviews" / "audit_log"
    audit.mkdir()
    os.symlink(root / "nowhere", audit / "itm_00000000000000c4-tag-kept-x.json")
    (audit / f"src_{'e' * 16}-details-confirmed-x.json").write_bytes(b"\xff")
    _damage(root)
    assert mr.inspect(root, SID)["offers"]


def test_a_copy_whose_status_disagrees_with_the_published_status_is_not_compatible(tmp_path):
    root = _vault(tmp_path)
    _backup(root)
    (root / "db").mkdir(exist_ok=True)
    graph.init_db(root / "db" / "graph.sqlite")
    gconn = graph.connect(root / "db" / "graph.sqlite")
    graph.upsert_node(gconn, node_id=SID, node_type="source", slug=SID, status="hidden",
                      now="2026-11-15T00:00:00+00:00")
    gconn.commit()
    gconn.close()
    _damage(root)
    report = mr.inspect(root, SID)
    assert any("disagrees with the published status (hidden)" in r
               for r in report["copies"][0]["incompatible"])


def test_another_file_claiming_the_document_blocks_restore(tmp_path):
    root = _vault(tmp_path)
    _backup(root)
    _damage(root)
    (_mdir(root) / "stray.json").write_bytes(_copy())
    report = mr.inspect(root, SID)
    assert any("claims this document" in f for f in report["findings"]) and not report["offers"]


# --- interrupted recoveries --------------------------------------------------------------------


def test_a_crash_before_the_replacement_is_finished_from_the_journal(tmp_path, monkeypatch):
    root = _vault(tmp_path)
    archive = _backup(root)
    good = _manifest(root).read_bytes()
    _damage(root)
    real = durable_write.write_bytes

    def crash(path, data, **kw):
        if Path(path) == _manifest(root):
            raise OSError("crash")
        return real(path, data, **kw)
    monkeypatch.setattr(durable_write, "write_bytes", crash)
    assert _restore(root, mr.inspect(root, SID), archive) == mr.EXIT_REFUSED
    monkeypatch.undo()
    assert _manifest(root).read_bytes() == DAMAGED and mr.journal_path(root, SID).exists()
    assert publication_queue.load(root)                        # intent recorded before the effect
    report = mr.inspect(root, SID)
    assert report["offers"] == {} and report["finish"]
    assert repair_manifest.main(["finish", SID, "--expected-state", report["fingerprint"],
                                 "--root", str(root)]) == 0
    assert _manifest(root).read_bytes() == good
    assert not mr.journal_path(root, SID).exists()
    assert list((root / "reviews" / "audit_log").glob(f"{SID}-manifest-restored-*.json"))


def test_a_crash_after_the_replacement_finishes_the_audit_idempotently(tmp_path, monkeypatch):
    root = _vault(tmp_path)
    archive = _backup(root)
    good = _manifest(root).read_bytes()
    _damage(root)
    real = durable_write.write_json

    def crash(path, obj, **kw):
        if "manifest-restored" in Path(path).name:
            raise OSError("crash")
        return real(path, obj, **kw)
    monkeypatch.setattr(durable_write, "write_json", crash)
    assert _restore(root, mr.inspect(root, SID), archive) == mr.EXIT_REFUSED
    monkeypatch.undo()
    assert _manifest(root).read_bytes() == good and mr.journal_path(root, SID).exists()
    report = mr.inspect(root, SID)
    assert report["finish"]
    assert mr.finish(root, SID, expected_state=report["fingerprint"])["exit"] == 0
    assert not mr.journal_path(root, SID).exists()
    assert len(list((root / "reviews" / "audit_log").glob(f"{SID}-manifest-restored-*.json"))) == 1


def test_an_open_journal_blocks_a_new_restore(tmp_path):
    root = _vault(tmp_path)
    _backup(root)
    _damage(root)
    mr.journal_path(root, SID).parent.mkdir(parents=True)
    mr.journal_path(root, SID).write_text("{}")
    report = mr.inspect(root, SID)
    assert report["offers"] == {} and report["finish"] is None
    assert any("no safe repair determined" in f for f in report["findings"])


def test_usage_errors(tmp_path):
    assert repair_manifest.main(["inspect", "nope"]) == mr.EXIT_USAGE
    assert repair_manifest.main(["restore", SID, "--archive", "x.zip", "--expected-state", "abc",
                                 "--reason", "r"]) == mr.EXIT_USAGE
    assert repair_manifest.main([]) == mr.EXIT_USAGE


_FIFO_PROBE = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import test_manifest_repair as t
from app.workers import manifest_repair as mr
root = t._vault(Path(sys.argv[1]))
t._backup(root)
t._damage(root)
raw = root / "raw" / "inbox" / "doc.txt"
raw.unlink()
os.mkfifo(raw)                                        # a FIFO where the raw file was
os.mkfifo(root / "backups" / "fifo.zip")             # and one among the archives
os.mkfifo(root / "reviews" / "audit_log" / "rev_00000000000000d5-approved-x.json") \
    if (root / "reviews" / "audit_log").exists() else None
report = mr.inspect(root, t.SID)
print(report["copies"][0]["raw"]["state"], sorted(c["reason"] or "" for c in report["copies"]))
"""


def test_fifos_never_hang_inspect(tmp_path):
    import subprocess
    done = subprocess.run([sys.executable, "-c", _FIFO_PROBE, str(tmp_path), str(ROOT / "tests")],
                          capture_output=True, text=True, timeout=60, cwd=ROOT,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert done.returncode == 0, done.stderr
    assert done.stdout.split()[0] == "untrusted"           # never opened as a stream
    assert "archive_not_a_regular_file" in done.stdout


def test_quarantine_copies_are_ignored_by_every_manifest_inventory(tmp_path):
    from app.backend import apply_sandbox
    root = _vault(tmp_path)
    q = mr.quarantine_dir(root)
    q.mkdir()
    (q / f"{SID}-20261115T000000Z.json").write_bytes(_copy(relative_raw_path="raw/inbox/other.txt"))
    (q / f"{SID}-20261115T000001Z.json").write_text("[1, 2]")      # not an object
    assert backup._catalogued_raw_index(root) == {"raw/inbox/doc.txt": SHA}
    assert apply_sandbox._catalogued_raw_rels(_mdir(root)) == {"raw/inbox/doc.txt"}
    inv = manifests.scan_manifests(_mdir(root))
    assert [m["source_id"] for m in inv.valid] == [SID] and inv.complete


def test_restore_refuses_an_incompatible_archive_even_when_another_is_offered(tmp_path):
    root = _vault(tmp_path)
    _backup(root)                                              # compatible
    unmarked = _hand_made_archive(root, [(f"raw/manifests/{SID}.json", _copy()),
                                         (backup._BACKUP_MANIFEST_NAME, _sidecar())])
    _damage(root)
    report = mr.inspect(root, SID)
    assert report["offers"] and unmarked.name not in report["offers"]
    before = _snapshot(root)
    out = mr.restore(root, SID, archive=unmarked, expected_state=report["fingerprint"],
                     reason="test")
    assert out["exit"] == mr.EXIT_REFUSED and "details history unknown" in out["message"]
    assert _snapshot(root) == before


# --- review round 1: finish re-checks compatibility, the journal is validated, barriers repeat ---


OTHER = f"src_{'0' * 15}1"


def _crash_on(monkeypatch, fn_name: str, match) -> None:
    """Make durable_write.<fn_name> raise OSError("crash") for a path `match(path)` accepts."""
    real = getattr(durable_write, fn_name)

    def crash(path, *a, **kw):
        if match(Path(path)):
            raise OSError("crash")
        return real(path, *a, **kw)
    monkeypatch.setattr(durable_write, fn_name, crash)


def _pending(tmp_path, monkeypatch) -> tuple[Path, bytes]:
    """A restore interrupted after its journal, before the replacement (copy not installed)."""
    root = _vault(tmp_path)
    _backup(root)
    good = _manifest(root).read_bytes()
    _damage(root)
    _crash_on(monkeypatch, "write_bytes", lambda p: p == _manifest(root))
    assert _restore(root, mr.inspect(root, SID)) == mr.EXIT_REFUSED
    monkeypatch.undo()
    assert _manifest(root).read_bytes() == DAMAGED and mr.journal_path(root, SID).exists()
    return root, good


def _installed(tmp_path, monkeypatch) -> tuple[Path, bytes]:
    """A restore interrupted after the replacement, before the audit record (copy live)."""
    root = _vault(tmp_path)
    _backup(root)
    good = _manifest(root).read_bytes()
    _damage(root)
    _crash_on(monkeypatch, "write_json", lambda p: "manifest-restored" in p.name)
    assert _restore(root, mr.inspect(root, SID)) == mr.EXIT_REFUSED
    monkeypatch.undo()
    assert _manifest(root).read_bytes() == good and mr.journal_path(root, SID).exists()
    return root, good


def _finish(root: Path, fp: str) -> dict:
    return mr.finish(root, SID, expected_state=fp)


@pytest.mark.parametrize("later", ["hide_approved", "details_change", "stray", "archive_gone"])
def test_a_pending_install_is_rechecked_and_a_later_decision_blocks_finish(tmp_path, monkeypatch,
                                                                            later):
    root, _ = _pending(tmp_path, monkeypatch)
    assert mr.inspect(root, SID)["finish"]                     # finishable before the decision
    if later == "hide_approved":
        _governance(root, "hide_content", "approved", FUTURE)
    elif later == "details_change":
        (root / "reviews" / "audit_log").mkdir(exist_ok=True)
        (root / "reviews" / "audit_log" / f"{SID}-details-confirmed-x.json").write_text(json.dumps(
            {"source_id": SID, "event": "details_confirmed", "at": FUTURE}))
    elif later == "stray":
        (_mdir(root) / "stray.json").write_bytes(_copy())
    else:
        for z in (root / "backups").glob("*.zip"):
            z.unlink()
    report = mr.inspect(root, SID)
    assert report["finish"] is None and report["offers"] == {}
    assert any("can't be installed now" in f for f in report["findings"])
    before = _snapshot(root)
    out = _finish(root, report["fingerprint"])
    assert out["exit"] == mr.EXIT_REFUSED and "Refused before any write" in out["message"]
    assert _snapshot(root) == before                           # no revision bump, no write
    assert _manifest(root).read_bytes() == DAMAGED


def test_a_journal_naming_another_document_never_finishes_or_touches_that_document(
        tmp_path, monkeypatch):
    root, _ = _installed(tmp_path, monkeypatch)
    jpath = mr.journal_path(root, SID)
    other = mr.journal_path(root, OTHER)
    other.write_bytes(jpath.read_bytes())                      # another document's open journal
    entry = json.loads(jpath.read_text())
    jpath.write_text(json.dumps({**entry, "source_id": OTHER}))
    report = mr.inspect(root, SID)
    assert report["facts"]["recovery journal"] == "invalid (invalid_field:source_id)"
    assert report["finish"] is None
    before = _snapshot(root)
    assert _finish(root, report["fingerprint"])["exit"] == mr.EXIT_REFUSED
    assert _snapshot(root) == before
    assert other.exists() and jpath.exists()


def _set(field, value):
    return lambda e, root: e.__setitem__(field, value)


@pytest.mark.parametrize("change, error", [
    (lambda e, root: e.pop("intent"), "invalid_field:intent"),
    (_set("surprise", 1), "invalid_field:surprise"),
    (_set("installed_sha256", 123), "invalid_field:installed_sha256"),
    (_set("damaged_sha256", "A" * 64), "invalid_field:damaged_sha256"),
    (lambda e, root: e.__setitem__("damaged_sha256", e["installed_sha256"]),
     "invalid_field:installed_sha256"),
    (_set("recovery_id", "mrc_" + "0" * 16), "invalid_field:recovery_id"),
    (_set("archive", "../elsewhere.zip"), "invalid_field:archive"),
    (_set("quarantine", f"{OTHER}-20261115T000000Z.json"), "invalid_field:quarantine"),
    (_set("intent", "pub_nope"), "invalid_field:intent"),
    (_set("actor", "someone"), "invalid_field:actor"),
    (_set("reason", " "), "invalid_field:reason"),
    (_set("started_at", "yesterday"), "invalid_field:started_at"),
    (_set("backup_created_at", None), "invalid_field:backup_created_at"),
    (lambda e, root: (mr.quarantine_dir(root) / e["quarantine"]).write_bytes(b"other bytes"),
     "quarantine_unverified:ok:sha256_mismatch"),
    (lambda e, root: (mr.quarantine_dir(root) / e["quarantine"]).unlink(),
     "quarantine_unverified:absent"),
])
def test_an_invalid_journal_has_no_write_path(tmp_path, monkeypatch, change, error):
    root, _ = _installed(tmp_path, monkeypatch)
    jpath = mr.journal_path(root, SID)
    entry = json.loads(jpath.read_text())
    change(entry, root)
    jpath.write_text(json.dumps(entry))
    report = mr.inspect(root, SID)
    assert report["facts"]["recovery journal"] == f"invalid ({error})"
    assert report["finish"] is None and report["offers"] == {}
    before = _snapshot(root)
    assert _finish(root, report["fingerprint"])["exit"] == mr.EXIT_REFUSED
    assert _snapshot(root) == before


def _audit_written_journal_open(tmp_path, monkeypatch) -> Path:
    root = _vault(tmp_path)
    _backup(root)
    _damage(root)
    _crash_on(monkeypatch, "remove", lambda p: p == mr.journal_path(root, SID))
    assert _restore(root, mr.inspect(root, SID)) == mr.EXIT_REFUSED
    monkeypatch.undo()
    assert mr.journal_path(root, SID).exists()
    return root


@pytest.mark.parametrize("change, ok", [
    ({"at": "2026-11-16T09:00:00+00:00"}, True),               # a retry's time differs: same record
    ({"at": "not a time"}, False),
    ({"reason": "something else"}, False),
    ({"installed_sha256": "0" * 64}, False),
    ({"source_id": OTHER}, False),
    ({"extra": 1}, False),
])
def test_an_existing_recovery_audit_must_match_the_whole_binding(tmp_path, monkeypatch, change, ok):
    root = _audit_written_journal_open(tmp_path, monkeypatch)
    [audit] = (root / "reviews" / "audit_log").glob(f"{SID}-manifest-restored-*.json")
    audit.write_text(json.dumps({**json.loads(audit.read_text()), **change}))
    out = _finish(root, mr.inspect(root, SID)["fingerprint"])
    assert (out["exit"] == mr.EXIT_OK) is ok
    assert mr.journal_path(root, SID).exists() is not ok


def _fail_sync(monkeypatch, match) -> list[Path]:
    """Fail durable_write.sync_dir once for a directory `match(dir)` accepts; returns the hits."""
    real = durable_write.sync_dir
    hits: list[Path] = []

    def sync(directory):
        if not hits and match(Path(directory)):
            hits.append(Path(directory))
            raise OSError(5, "Input/output error")
        return real(directory)
    monkeypatch.setattr(durable_write, "sync_dir", sync)
    return hits


@pytest.mark.parametrize("stage, journal_after, says, never_says", [
    ("quarantine", False, "No recovery journal was created", "journal is recorded"),
    ("journal", None, "may or may not have been recorded durably", "journal is recorded"),
    ("install", True, "manifest replacement may or may not have completed", "stays open"),
])
def test_an_uncertain_write_names_only_what_its_stage_established(tmp_path, monkeypatch, stage,
                                                                  journal_after, says, never_says):
    root = _vault(tmp_path)
    _backup(root)
    _damage(root)
    jpath = mr.journal_path(root, SID)
    match = {"quarantine": lambda d: d == mr.quarantine_dir(root),
             "journal": lambda d: d == jpath.parent,
             "install": lambda d: d == _mdir(root) and jpath.exists()}[stage]
    hits = _fail_sync(monkeypatch, match)
    out = mr.restore(root, SID, archive=next((root / "backups").glob("*.zip")),
                     expected_state=mr.inspect(root, SID)["fingerprint"], reason="test")
    monkeypatch.undo()
    assert hits and out["exit"] == mr.EXIT_REFUSED
    assert says in out["message"] and never_says not in out["message"]
    assert "Inspect again" in out["message"] and "finish` only when" in out["message"]
    if journal_after is not None:
        assert jpath.exists() is journal_after


def test_finish_repeats_every_durability_barrier_before_publication_and_close(tmp_path,
                                                                               monkeypatch):
    from app.workers import publication
    root = _vault(tmp_path)
    _backup(root)
    good = _manifest(root).read_bytes()
    _damage(root)
    jpath = mr.journal_path(root, SID)
    # The replacement lands, but raw/manifests can't be synced after it: durability uncertain.
    hits = _fail_sync(monkeypatch, lambda d: d == _mdir(root) and jpath.exists())
    assert _restore(root, mr.inspect(root, SID)) == mr.EXIT_REFUSED
    monkeypatch.undo()
    assert hits and _manifest(root).read_bytes() == good and jpath.exists()
    report = mr.inspect(root, SID)
    assert report["finish"] and any("the backup copy is live" in f for f in report["findings"])
    quarantine = mr.quarantine_dir(root) / json.loads(jpath.read_text())["quarantine"]

    calls: list[tuple[str, Path | None]] = []
    real_sync, real_publish, real_remove = (durable_write.sync_existing, publication.publish,
                                            durable_write.remove)

    def sync_existing(path):
        real_sync(path)                                       # recorded once it has COMPLETED
        calls.append(("sync", Path(path)))

    def publish(*a, **kw):
        calls.append(("publish", None))
        return real_publish(*a, **kw)

    def remove(path, **kw):
        calls.append(("remove", Path(path)))
        return real_remove(path, **kw)
    monkeypatch.setattr(durable_write, "sync_existing", sync_existing)
    monkeypatch.setattr(publication, "publish", publish)
    monkeypatch.setattr(durable_write, "remove", remove)
    assert _finish(root, report["fingerprint"])["exit"] == mr.EXIT_OK
    first_publish = calls.index(("publish", None))
    for path in (jpath, quarantine, _manifest(root)):
        assert calls.index(("sync", path)) < first_publish
    assert first_publish < calls.index(("remove", jpath))
    assert not jpath.exists()


def test_a_failed_barrier_stops_finish_before_publication(tmp_path, monkeypatch):
    from app.workers import publication
    root, _ = _installed(tmp_path, monkeypatch)
    report = mr.inspect(root, SID)
    published: list[int] = []
    monkeypatch.setattr(publication, "publish", lambda *a, **k: published.append(1) or {})
    _crash_on(monkeypatch, "sync_existing", lambda p: p == _manifest(root))
    out = _finish(root, report["fingerprint"])
    assert out["exit"] == mr.EXIT_REFUSED and "could not be confirmed durable" in out["message"]
    assert not published and mr.journal_path(root, SID).exists()
