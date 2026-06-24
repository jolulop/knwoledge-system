from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import manifests
from app.workers import intake


def test_source_id_derivation():
    sha = "a" * 64
    assert manifests.source_id_for(sha) == "src_" + "a" * 16


def test_sha256_file(tmp_path):
    f = tmp_path / "x.txt"
    f.write_bytes(b"hello world")
    assert manifests.sha256_file(f) == hashlib.sha256(b"hello world").hexdigest()


def test_manifest_has_required_fields(tmp_path):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "doc.md").write_text("content one\n", encoding="utf-8")

    summary = intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    sid = summary["source_ids"][0]
    manifest = manifests.load_manifest(tmp_path / "raw" / "manifests", sid)

    assert manifest is not None
    assert manifest["source_id"] == sid
    assert sid == "src_" + manifest["sha256"][:16]
    assert manifest["retention_class"] == "unknown"
    assert manifest["ingestion_status"] == "new"
    assert manifest["original_filename"] == "doc.md"
    assert manifest["relative_raw_path"] == "raw/inbox/doc.md"
    assert len(manifest["occurrences"]) == 1
    occ = manifest["occurrences"][0]
    assert occ["filename"] == "doc.md"
    assert occ["first_seen_at"] == occ["last_seen_at"]


def test_empty_file_is_flagged(tmp_path):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "empty.md").write_text("", encoding="utf-8")

    summary = intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    sid = summary["source_ids"][0]
    manifest = manifests.load_manifest(tmp_path / "raw" / "manifests", sid)

    assert "empty_file" in manifest["notes"]
    assert any(w["warning"] == "empty_file" for w in summary["warnings"])


# --- canonical source-id validation / path-traversal safety (ADR-0009) -----

import pytest  # noqa: E402

_BAD_IDS = ["../../etc/passwd", "src_../../x", "src_short", "src_ZZZZ012345678901",
            "/abs/path", "src_0123456789abcdef/extra", "", "nope"]


def test_is_source_id():
    assert manifests.is_source_id("src_0123456789abcdef")
    assert not any(manifests.is_source_id(b) for b in _BAD_IDS)
    assert not manifests.is_source_id(None)


def test_load_manifest_invalid_id_returns_none_no_traversal(tmp_path):
    md = tmp_path / "raw" / "manifests"
    md.mkdir(parents=True)
    # plant a file one level up that a traversal id would resolve to
    (tmp_path / "raw" / "secret.json").write_text('{"x":1}', encoding="utf-8")
    for bad in _BAD_IDS:
        assert manifests.load_manifest(md, bad) is None  # read -> None, never escapes


def test_write_paths_raise_on_invalid_id(tmp_path):
    md = tmp_path / "raw" / "manifests"
    md.mkdir(parents=True)
    outside = tmp_path / "pwned.json"
    for bad in _BAD_IDS:
        with pytest.raises(ValueError):
            manifests.set_status(md, bad, "archived")
        with pytest.raises(ValueError):
            manifests.set_provenance(md, bad, extracted_at="2026-01-01T00:00:00+00:00")
        with pytest.raises(ValueError):
            manifests.save_manifest(md, {"source_id": bad})
    assert not outside.exists()                       # nothing written outside the manifests dir
    assert list(md.glob("**/*.json")) == []           # ... and nothing inside either


def test_valid_manifests_partitions(tmp_path):
    md = tmp_path / "raw" / "manifests"
    md.mkdir(parents=True)
    good = "src_0123456789abcdef"
    (md / f"{good}.json").write_text(f'{{"source_id": "{good}"}}', encoding="utf-8")  # valid
    (md / "wrongname.json").write_text(
        '{"source_id": "src_00000000000000ab"}', encoding="utf-8")                    # filename_mismatch
    (md / "x.json").write_text('{"source_id": "../../etc/x"}', encoding="utf-8")      # non_canonical
    dup = "src_00000000000000cc"                                                      # duplicate across files
    (md / f"{dup}.json").write_text(f'{{"source_id": "{dup}"}}', encoding="utf-8")
    (md / "copy.json").write_text(f'{{"source_id": "{dup}"}}', encoding="utf-8")
    valid, skipped = manifests.valid_manifests(md)
    assert [r["source_id"] for r in valid] == [good]  # only the clean, correctly-named, unique record
    assert sorted(skipped) == ["duplicate_source_id", "duplicate_source_id",
                               "filename_mismatch", "non_canonical_id"]
    # categorical reasons only — never the malformed id text
    assert all(s in ("non_canonical_id", "filename_mismatch", "duplicate_source_id") for s in skipped)
    assert not any("etc" in s for s in skipped)
