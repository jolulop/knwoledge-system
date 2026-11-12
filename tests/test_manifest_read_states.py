"""ADR-0074 decision 2: typed manifest read states and the manifest inventory.

`absent` is a *verified* absence (only it permits creation); `unreadable` (an entry that can't be
read — permission, I/O, not a regular file, a dangling link) and `corrupt` (bad UTF-8, bad JSON, not
an object, or a record naming another document) stay distinct; `ok` is a parsed object, not yet a
validated manifest. An inventory never drops a damaged file: it is counted, reported by a safely
rendered path, and makes the inventory incomplete — which blocks absence-based actions."""
from __future__ import annotations

import json
import os

import pytest

from app.backend import manifests as m

SID = "src_0123456789abcdef"
OTHER = "src_00000000000000ab"


def _md(tmp_path):
    d = tmp_path / "raw" / "manifests"
    d.mkdir(parents=True)
    return d


def _good(sid=SID):
    return json.dumps({"source_id": sid, "sha256": "0" * 64})


# --- one read ----------------------------------------------------------------------------------


def test_absent_is_verified_absence(tmp_path):
    md = _md(tmp_path)
    r = m.read_manifest(md, SID)
    assert r.state == m.ABSENT and r.record is None and not r.damaged
    assert m.get_manifest(md, SID) is None


def test_a_missing_manifests_directory_is_a_verified_absence_too(tmp_path):
    assert m.read_manifest(tmp_path / "raw" / "manifests", SID).state == m.ABSENT


def test_ok_is_a_parsed_object(tmp_path):
    md = _md(tmp_path)
    (md / f"{SID}.json").write_text(_good(), encoding="utf-8")
    r = m.read_manifest(md, SID)
    assert r.state == m.OK and r.record["source_id"] == SID
    assert m.get_manifest(md, SID) == r.record


@pytest.mark.parametrize("content,error", [
    (b"\xff\xfe not utf-8", "invalid_utf8"),
    (b"{truncated", "invalid_json"),
    (b"", "invalid_json"),
    (b"[1, 2]", "not_an_object"),
    (b'"a string"', "not_an_object"),
    (b"[" * 100_000, "invalid_json"),                       # nesting too deep: still corrupt
])
def test_corrupt_bytes(tmp_path, content, error):
    md = _md(tmp_path)
    (md / f"{SID}.json").write_bytes(content)
    r = m.read_manifest(md, SID)
    assert (r.state, r.error) == (m.CORRUPT, error)
    with pytest.raises(m.ManifestUnavailable) as exc:
        m.get_manifest(md, SID)
    assert exc.value.state == m.CORRUPT and "truncated" not in str(exc.value)   # no contents


def test_a_record_naming_another_document_is_corrupt_for_this_one(tmp_path):
    md = _md(tmp_path)
    (md / f"{SID}.json").write_text(_good(OTHER), encoding="utf-8")
    r = m.read_manifest(md, SID)
    assert (r.state, r.error) == (m.CORRUPT, "id_mismatch")


def test_a_directory_in_place_of_the_file_is_unreadable(tmp_path):
    md = _md(tmp_path)
    (md / f"{SID}.json").mkdir()
    r = m.read_manifest(md, SID)
    assert (r.state, r.error) == (m.UNREADABLE, "not_a_regular_file")


def test_a_fifo_is_unreadable_and_never_blocks(tmp_path):
    md = _md(tmp_path)
    os.mkfifo(md / f"{SID}.json")
    assert m.read_manifest(md, SID).state == m.UNREADABLE


def test_a_dangling_symlink_never_establishes_absence(tmp_path):
    md = _md(tmp_path)
    (md / f"{SID}.json").symlink_to(tmp_path / "nowhere.json")
    r = m.read_manifest(md, SID)
    assert (r.state, r.error) == (m.UNREADABLE, "dangling_symlink")


def test_a_dangling_manifests_directory_never_establishes_absence(tmp_path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "manifests").symlink_to(tmp_path / "gone")
    assert m.read_manifest(tmp_path / "raw" / "manifests", SID).state == m.UNREADABLE


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
def test_a_permission_error_is_unreadable(tmp_path):
    md = _md(tmp_path)
    path = md / f"{SID}.json"
    path.write_text(_good(), encoding="utf-8")
    path.chmod(0)
    try:
        r = m.read_manifest(md, SID)
        assert (r.state, r.error) == (m.UNREADABLE, "PermissionError")
    finally:
        path.chmod(0o644)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
def test_an_unsearchable_directory_never_establishes_absence(tmp_path):
    md = _md(tmp_path)
    md.chmod(0o600)                                            # no search permission
    try:
        assert m.read_manifest(md, SID).state == m.UNREADABLE
    finally:
        md.chmod(0o755)


def test_an_io_error_while_reading_is_unreadable(tmp_path, monkeypatch):
    md = _md(tmp_path)
    (md / f"{SID}.json").write_text(_good(), encoding="utf-8")
    monkeypatch.setattr(m.os, "read", lambda fd, n: (_ for _ in ()).throw(OSError(5, "EIO")))
    r = m.read_manifest(md, SID)
    monkeypatch.undo()
    assert (r.state, r.error) == (m.UNREADABLE, "OSError")


# --- the inventory ----------------------------------------------------------------------------


def test_damaged_files_are_counted_never_dropped(tmp_path):
    md = _md(tmp_path)
    (md / f"{SID}.json").write_text(_good(), encoding="utf-8")
    (md / f"{OTHER}.json").write_text("{bad", encoding="utf-8")
    (md / "src_00000000000000cd.json").mkdir()
    inv = m.scan_manifests(md)
    assert [r["source_id"] for r in inv.valid] == [SID]
    assert sorted(inv.skipped) == ["corrupt", "unreadable"]
    assert inv.damaged_ids == {OTHER: "corrupt", "src_00000000000000cd": "unreadable"}
    assert not inv.complete and inv.coverage == "incomplete"
    assert inv.report() == {"manifests_skipped_invalid": 0, "manifests_unreadable": 1,
                            "manifests_corrupt": 1, "manifest_coverage": "incomplete"}
    with pytest.raises(m.IncompleteManifestInventory):
        inv.require_complete("pruning")


def test_damaged_files_are_reported_by_a_safe_path_only(tmp_path):
    md = _md(tmp_path)
    (md / "evil\x1b[31m‮.json").write_text("not json: SECRET", encoding="utf-8")
    inv = m.scan_manifests(md)
    ((shown, state),) = inv.damaged
    assert state == "corrupt" and shown.startswith("raw/manifests/evil")
    assert "\x1b" not in shown and "‮" not in shown and "SECRET" not in shown
    assert inv.damaged_ids == {}                               # not named like a document


def test_an_unlistable_directory_is_never_an_empty_inventory(tmp_path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "manifests").write_text("a file, not a directory")
    inv = m.scan_manifests(tmp_path / "raw" / "manifests")
    assert inv.enumeration_error == "NotADirectoryError"
    assert not inv.complete and inv.coverage == "unavailable"
    with pytest.raises(m.IncompleteManifestInventory, match="could not be listed"):
        inv.require_complete("pruning")


def test_a_missing_directory_is_a_complete_empty_inventory(tmp_path):
    inv = m.scan_manifests(tmp_path / "raw" / "manifests")
    assert inv.valid == [] and inv.complete and inv.coverage == "complete"


def test_identity_quarantine_keeps_its_reasons_and_is_not_damage(tmp_path):
    md = _md(tmp_path)
    (md / "wrong.json").write_text(_good(OTHER), encoding="utf-8")
    inv = m.scan_manifests(md)
    assert inv.skipped == ["filename_mismatch"] and not inv.damaged
    report = inv.report()
    assert report["manifests_skipped_invalid"] == 1
    assert report["manifests_corrupt"] == report["manifests_unreadable"] == 0
    assert not inv.complete and report["manifest_coverage"] == "incomplete"   # review round 1


# --- writers never touch a damaged manifest ----------------------------------------------------


@pytest.mark.parametrize("damage", ["corrupt", "unreadable"])
def test_set_status_and_provenance_refuse_a_damaged_manifest(tmp_path, damage):
    md = _md(tmp_path)
    path = md / f"{SID}.json"
    if damage == "corrupt":
        path.write_text("{bad", encoding="utf-8")
    else:
        path.mkdir()
    before = path.read_bytes() if damage == "corrupt" else None
    for call in (lambda: m.set_status(md, SID, "archived"),
                 lambda: m.set_provenance(md, SID, title="T")):
        with pytest.raises(m.ManifestUnavailable):
            call()
    if before is not None:
        assert path.read_bytes() == before                     # never overwritten
    assert m.set_status(md, OTHER, "archived") is None         # absent: nothing to change


# --- review round 1: identity quarantine makes the inventory incomplete ------------------------


@pytest.mark.parametrize("reason", ["duplicate_source_id", "filename_mismatch", "non_canonical_id"])
def test_identity_quarantine_keeps_its_reason_and_makes_coverage_incomplete(tmp_path, reason):
    md = tmp_path / "manifests"
    md.mkdir()
    sid, other = "src_aaaaaaaaaaaaaaaa", "src_bbbbbbbbbbbbbbbb"
    (md / f"{sid}.json").write_text(json.dumps({"source_id": sid}), encoding="utf-8")
    if reason == "duplicate_source_id":
        (md / f"{other}.json").write_text(json.dumps({"source_id": sid}), encoding="utf-8")
    elif reason == "filename_mismatch":
        (md / "renamed.json").write_text(json.dumps({"source_id": other}), encoding="utf-8")
    else:
        (md / f"{other}.json").write_text(json.dumps({"source_id": "nope"}), encoding="utf-8")
    inv = m.scan_manifests(md)
    assert reason in inv.skipped and not inv.damaged
    assert not inv.complete and inv.coverage == "incomplete"
    assert inv.report()["manifests_skipped_invalid"] >= 1
    assert inv.quarantined_ids.get(other) == reason            # the id the stray file names
    with pytest.raises(m.IncompleteManifestInventory, match="quarantined"):
        inv.require_complete("an absence-dependent action")
    if reason == "duplicate_source_id":
        assert inv.valid == [] and inv.excluded_reason(sid) == "manifest_duplicate_source_id"
        read = m.recheck(md, sid, inv)
        assert read.state != m.OK and m.read_reason(read) == \
            "manifest_duplicate_source_id"
    else:
        assert inv.valid_ids == {sid} and inv.excluded_reason(sid) is None
        assert m.recheck(md, sid, inv).state == m.OK
