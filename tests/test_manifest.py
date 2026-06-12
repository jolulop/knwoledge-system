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
