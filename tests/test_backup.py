from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import backup  # noqa: E402


def _write_raw(root: Path, rel: str, content: bytes, *, with_manifest: bool = True,
               sha_override: str | None = None) -> None:
    """Write a raw byte file at <root>/<rel> and (optionally) its ADR-0024 manifest."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if with_manifest:
        sha = sha_override or hashlib.sha256(content).hexdigest()
        mname = rel.replace("/", "_") + ".json"
        mpath = root / "raw" / "manifests" / mname
        mpath.parent.mkdir(parents=True, exist_ok=True)
        mpath.write_text(json.dumps({"relative_raw_path": rel, "sha256": sha}), encoding="utf-8")


def _sidecar(out: Path) -> dict:
    return json.loads(zipfile.ZipFile(out).read("BACKUP_MANIFEST.json"))


def _seed(tmp_path: Path) -> None:
    for rel, content in {
        "wiki/log.md": "log",
        "wiki/index.md": "index",
        "wiki/Sources/src_x.md": "page",
        "raw/manifests/src_x.json": "{}",
        "db/jobs.sqlite": "jobs",
        "db/graph.sqlite": "graph",          # authoritative graph -> backed up (lives in db/)
        "db/llm_cache.sqlite": "cache",
        "indexes/keyword/keyword.sqlite": "k",  # derived -> NOT backed up (cheap rebuild)
        "indexes/vector/data.lance": "v",       # derived -> opt-in only
        "policies/citation.yaml": "c",
    }.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_backup_includes_durable_state_and_graph(tmp_path):
    _seed(tmp_path)
    out = backup.create_backup(tmp_path)
    names = set(zipfile.ZipFile(out).namelist())

    for required in (
        "wiki/log.md", "wiki/index.md", "wiki/Sources/src_x.md",
        "raw/manifests/src_x.json", "db/jobs.sqlite", "db/graph.sqlite",
        "db/llm_cache.sqlite", "policies/citation.yaml",
    ):
        assert required in names, f"backup missing {required}"

    # The backup must not recurse the backups/ dir into itself.
    assert not any(n.startswith("backups/") for n in names)


def test_backup_excludes_derived_indexes_by_default(tmp_path):
    _seed(tmp_path)
    out = backup.create_backup(tmp_path)
    names = set(zipfile.ZipFile(out).namelist())

    # ADR-0032 §7: keyword index never backed up; vector index opt-in only.
    assert "indexes/keyword/keyword.sqlite" not in names
    assert "indexes/vector/data.lance" not in names
    assert not any(n.startswith("indexes/") for n in names)


def test_backup_includes_vector_index_on_opt_in(tmp_path, monkeypatch):
    _seed(tmp_path)
    monkeypatch.setenv("BACKUP_INCLUDE_VECTOR_INDEX", "1")
    out = backup.create_backup(tmp_path)
    names = set(zipfile.ZipFile(out).namelist())

    assert "indexes/vector/data.lance" in names
    # Opting into the vector index must not also pull in the keyword index.
    assert "indexes/keyword/keyword.sqlite" not in names


def test_backup_env_flags_compose(tmp_path, monkeypatch):
    # Vector opt-in and cache opt-out are independent env flags that must compose.
    _seed(tmp_path)
    monkeypatch.setenv("BACKUP_INCLUDE_VECTOR_INDEX", "1")
    monkeypatch.setenv("BACKUP_EXCLUDE_LLM_CACHE", "1")
    out = backup.create_backup(tmp_path)
    names = set(zipfile.ZipFile(out).namelist())

    assert "indexes/vector/data.lance" in names       # vector included
    assert "db/llm_cache.sqlite" not in names          # cache excluded
    assert "db/graph.sqlite" in names                  # graph still backed up
    assert "indexes/keyword/keyword.sqlite" not in names


# --- ADR-0039: raw opt-in, integrity verification, guarded restore --------------------


def test_raw_bytes_excluded_by_default(tmp_path):
    _seed(tmp_path)
    _write_raw(tmp_path, "raw/permanent/doc.pdf", b"raw-bytes")
    report = backup.create_backup_report(tmp_path)
    names = set(zipfile.ZipFile(report.archive).namelist())
    assert "raw/permanent/doc.pdf" not in names
    assert not report.raw_included
    assert _sidecar(report.archive)["raw_included"] is False
    # The report warns that restore won't recover source bytes.
    assert "WARNING" in report.render() and "source bytes" in report.render()


def test_raw_opt_in_is_manifest_driven_including_inbox(tmp_path, monkeypatch):
    # Intake never moves files out of raw/inbox/ (ADR-0007), so a catalogued inbox source IS the real
    # source and must be backed up; un-manifested inbox staging must NOT be (ADR-0039 decision 1).
    _seed(tmp_path)
    _write_raw(tmp_path, "raw/inbox/doc.md", b"ingested-source")     # catalogued -> included
    _write_raw(tmp_path, "raw/permanent/p.pdf", b"perm")            # catalogued -> included
    _write_raw(tmp_path, "raw/inbox/staging.pdf", b"new", with_manifest=False)  # un-ingested -> out
    monkeypatch.setenv("BACKUP_INCLUDE_RAW", "1")
    report = backup.create_backup_report(tmp_path)
    names = set(zipfile.ZipFile(report.archive).namelist())

    assert "raw/inbox/doc.md" in names, "catalogued inbox source must be backed up"
    assert "raw/permanent/p.pdf" in names
    assert "raw/inbox/staging.pdf" not in names, "un-manifested staging must be excluded"
    assert any(n.startswith("raw/manifests/") for n in names)
    assert report.raw_included and report.raw_file_count == 2
    # The non-default _seed manifest (src_x.json = "{}") has no sha -> contributes no raw entry.
    assert all(e["manifest_verified"] for e in _sidecar(report.archive)["raw_files"].values())


def test_occurrence_duplicate_is_backed_up_and_verified(tmp_path, monkeypatch):
    # A second observed copy lives only in occurrences[]; it must be included and checksum-verified
    # (manifest-complete coverage, ADR-0039 decision 4 / blocking #3).
    content = b"same-content"
    sha = hashlib.sha256(content).hexdigest()
    (tmp_path / "raw/permanent").mkdir(parents=True)
    (tmp_path / "raw/inbox").mkdir(parents=True)
    (tmp_path / "raw/permanent/primary.pdf").write_bytes(content)
    (tmp_path / "raw/inbox/dupe.pdf").write_bytes(content)
    (tmp_path / "raw/manifests").mkdir(parents=True)
    (tmp_path / "raw/manifests/m.json").write_text(json.dumps({
        "relative_raw_path": "raw/permanent/primary.pdf", "sha256": sha,
        "occurrences": [{"relative_path": "raw/inbox/dupe.pdf"}],
    }), encoding="utf-8")
    monkeypatch.setenv("BACKUP_INCLUDE_RAW", "1")
    report = backup.create_backup_report(tmp_path)
    names = set(zipfile.ZipFile(report.archive).namelist())
    assert {"raw/permanent/primary.pdf", "raw/inbox/dupe.pdf"} <= names
    assert report.raw_file_count == 2


def test_missing_catalogued_raw_hard_fails(tmp_path, monkeypatch):
    # A manifest references a raw byte that is not on disk -> abort, do NOT ship a "raw included"
    # archive that silently omits the source (review blocking #1/#3).
    _seed(tmp_path)
    sha = hashlib.sha256(b"gone").hexdigest()
    (tmp_path / "raw/manifests/missing.json").write_text(
        json.dumps({"relative_raw_path": "raw/permanent/gone.pdf", "sha256": sha}), encoding="utf-8")
    monkeypatch.setenv("BACKUP_INCLUDE_RAW", "1")
    with pytest.raises(ValueError, match="catalogued raw file missing"):
        backup.create_backup_report(tmp_path)
    assert not list((tmp_path / "backups").glob("*.zip"))


def test_occurrence_mutated_bytes_fail_checksum(tmp_path, monkeypatch):
    content = b"canonical"
    sha = hashlib.sha256(content).hexdigest()
    (tmp_path / "raw/permanent").mkdir(parents=True)
    (tmp_path / "raw/inbox").mkdir(parents=True)
    (tmp_path / "raw/permanent/primary.pdf").write_bytes(content)
    (tmp_path / "raw/inbox/dupe.pdf").write_bytes(b"TAMPERED")  # mutated duplicate occurrence
    (tmp_path / "raw/manifests").mkdir(parents=True)
    (tmp_path / "raw/manifests/m.json").write_text(json.dumps({
        "relative_raw_path": "raw/permanent/primary.pdf", "sha256": sha,
        "occurrences": [{"relative_path": "raw/inbox/dupe.pdf"}],
    }), encoding="utf-8")
    monkeypatch.setenv("BACKUP_INCLUDE_RAW", "1")
    with pytest.raises(ValueError, match="raw checksum mismatch"):
        backup.create_backup_report(tmp_path)


def test_same_timestamp_does_not_overwrite(tmp_path, monkeypatch):
    # Two backups in the same UTC second must not clobber each other (ADR-0039 decision 5, blocking #2).
    _seed(tmp_path)

    class _Frozen:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 6, 26, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(backup, "datetime", _Frozen)
    a = backup.create_backup(tmp_path)
    b = backup.create_backup(tmp_path)
    assert a != b, "same-second backups must get distinct archive paths"
    assert a.exists() and b.exists()
    assert b.name.endswith("-1.zip")


def test_raw_checksum_mismatch_aborts_backup(tmp_path, monkeypatch):
    _seed(tmp_path)
    # Manifest sha intentionally disagrees with the file bytes -> ADR-0024 integrity violation.
    _write_raw(tmp_path, "raw/permanent/drifted.pdf", b"actual-bytes", sha_override="0" * 64)
    monkeypatch.setenv("BACKUP_INCLUDE_RAW", "1")
    before = set((tmp_path / "backups").glob("*.zip")) if (tmp_path / "backups").exists() else set()
    with pytest.raises(ValueError, match="raw checksum mismatch"):
        backup.create_backup_report(tmp_path)
    # No partial/corrupt archive is left behind.
    after = set((tmp_path / "backups").glob("*.zip")) if (tmp_path / "backups").exists() else set()
    assert after == before


def test_restore_refuses_overwrite_without_force(tmp_path):
    _seed(tmp_path)
    out = backup.create_backup(tmp_path)
    # Tamper durable state on disk; default restore must NOT clobber it.
    (tmp_path / "db" / "graph.sqlite").write_text("LOCAL-EDITED", encoding="utf-8")
    (tmp_path / "policies" / "citation.yaml").write_text("LOCAL-POLICY", encoding="utf-8")
    report = backup.restore_backup(out, tmp_path)
    assert (tmp_path / "db" / "graph.sqlite").read_text() == "LOCAL-EDITED"  # untouched
    assert "db/graph.sqlite" in report.skipped_conflicts
    # Durable-state conflicts are surfaced explicitly.
    assert "db/graph.sqlite" in report.durable_conflicts
    assert "policies/citation.yaml" in report.durable_conflicts


def test_restore_force_overwrites(tmp_path):
    _seed(tmp_path)
    out = backup.create_backup(tmp_path)
    (tmp_path / "db" / "graph.sqlite").write_text("LOCAL-EDITED", encoding="utf-8")
    report = backup.restore_backup(out, tmp_path, force=True)
    assert (tmp_path / "db" / "graph.sqlite").read_text() == "graph"  # restored from archive
    assert "db/graph.sqlite" in report.overwritten


def test_restore_dry_run_writes_nothing(tmp_path):
    _seed(tmp_path)
    out = backup.create_backup(tmp_path)
    (tmp_path / "db" / "graph.sqlite").write_text("LOCAL-EDITED", encoding="utf-8")
    report = backup.restore_backup(out, tmp_path, force=True, dry_run=True)
    assert (tmp_path / "db" / "graph.sqlite").read_text() == "LOCAL-EDITED"  # not written
    assert report.dry_run and "db/graph.sqlite" in report.overwritten  # planned, not applied


def test_restore_verifies_raw_bytes_roundtrip(tmp_path, monkeypatch, tmp_path_factory):
    _seed(tmp_path)
    _write_raw(tmp_path, "raw/permanent/p.pdf", b"perm-content")
    monkeypatch.setenv("BACKUP_INCLUDE_RAW", "1")
    out = backup.create_backup(tmp_path)

    target = tmp_path_factory.mktemp("restore_target")
    report = backup.restore_backup(out, target)
    assert (target / "raw/permanent/p.pdf").read_bytes() == b"perm-content"
    assert report.raw_included and report.raw_verified == 1


def test_restore_skipped_raw_not_counted_as_verified(tmp_path, monkeypatch):
    # A pre-existing raw file skipped on a non-force restore must be reported as skipped, never as a
    # verified/restored byte (review recommended test).
    _seed(tmp_path)
    _write_raw(tmp_path, "raw/permanent/p.pdf", b"perm-content")
    monkeypatch.setenv("BACKUP_INCLUDE_RAW", "1")
    out = backup.create_backup(tmp_path)
    # Restore back into the SAME populated tree (no --force): the raw file already exists -> skipped.
    report = backup.restore_backup(out, tmp_path)
    assert report.partial
    assert "raw/permanent/p.pdf" in report.skipped_conflicts
    assert report.raw_verified == 0 and report.raw_skipped == 1
    assert "PARTIAL" in report.render()


def test_restore_rejects_zip_slip(tmp_path):
    _seed(tmp_path)
    out = backup.create_backup(tmp_path)
    # Append a malicious entry escaping the root.
    with zipfile.ZipFile(out, "a") as zf:
        zf.writestr("../escape.txt", "evil")
    with pytest.raises(ValueError, match="zip-slip"):
        backup.restore_backup(out, tmp_path, force=True)
