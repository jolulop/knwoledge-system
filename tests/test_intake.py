from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import db, manifests
from app.workers import intake


def _setup_inbox(tmp_path: Path) -> Path:
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "one.md").write_text("test document one\n", encoding="utf-8")
    (inbox / "one-copy.md").write_text("test document one\n", encoding="utf-8")  # dup
    (inbox / "two.md").write_text("a different document\n", encoding="utf-8")
    return inbox


def test_scan_detects_exact_duplicate(tmp_path):
    _setup_inbox(tmp_path)
    summary = intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")

    assert summary["files_found"] == 3
    assert summary["unique_contents"] == 2
    assert summary["new_manifests"] == 2
    assert summary["duplicates"] == 1
    assert summary["errors"] == 0

    records = manifests.list_manifests(tmp_path / "raw" / "manifests")
    assert len(records) == 2

    dup = [m for m in records if len(m["occurrences"]) == 2]
    assert len(dup) == 1
    rels = {o["relative_path"] for o in dup[0]["occurrences"]}
    assert any(r.endswith("one.md") for r in rels)
    assert any(r.endswith("one-copy.md") for r in rels)


def test_scan_is_idempotent(tmp_path):
    _setup_inbox(tmp_path)
    jobs_db = tmp_path / "db" / "jobs.sqlite"
    manifests_dir = tmp_path / "raw" / "manifests"

    first = intake.scan_inbox(tmp_path, jobs_db=jobs_db)
    discovered = {
        sid: manifests.load_manifest(manifests_dir, sid)["discovered_at"]
        for sid in first["source_ids"]
    }

    second = intake.scan_inbox(tmp_path, jobs_db=jobs_db)

    assert first["new_manifests"] == 2
    assert second["new_manifests"] == 0
    assert second["updated_manifests"] == 2
    assert second["unique_contents"] == 2
    assert len(manifests.list_manifests(manifests_dir)) == 2

    # discovered_at is set once at first intake and must survive every rescan.
    for sid in second["source_ids"]:
        assert manifests.load_manifest(manifests_dir, sid)["discovered_at"] == discovered[sid]


def test_symlink_escape_is_skipped(tmp_path):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "real.md").write_text("real inbox content\n", encoding="utf-8")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("secret living outside the raw repository\n", encoding="utf-8")
    (inbox / "escape.md").symlink_to(outside)

    summary = intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")

    assert summary["files_found"] == 1  # only the real file is hashed
    assert summary["skipped"] == 1
    assert any(w["warning"] == "skipped_symlink" for w in summary["warnings"])
    # The escaped file's path must never leak into a manifest.
    blob = json.dumps(manifests.list_manifests(tmp_path / "raw" / "manifests"))
    assert "outside-secret" not in blob


def test_manifest_sha_mismatch_is_rejected(tmp_path):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "doc.md").write_text("authentic content\n", encoding="utf-8")
    jobs_db = tmp_path / "db" / "jobs.sqlite"
    manifests_dir = tmp_path / "raw" / "manifests"

    first = intake.scan_inbox(tmp_path, jobs_db=jobs_db)
    manifest_path = manifests_dir / f"{first['source_ids'][0]}.json"

    # Corrupt the stored checksum so it no longer matches the file content.
    corrupt = json.loads(manifest_path.read_text())
    corrupt["sha256"] = "0" * 64
    occ_before = len(corrupt["occurrences"])
    manifest_path.write_text(json.dumps(corrupt))

    second = intake.scan_inbox(tmp_path, jobs_db=jobs_db)

    assert second["errors"] == 1
    assert second["new_manifests"] == 0
    assert second["updated_manifests"] == 0
    # The mismatched manifest is left exactly as found, never merged into.
    after = json.loads(manifest_path.read_text())
    assert after["sha256"] == "0" * 64
    assert len(after["occurrences"]) == occ_before


def test_saved_page_assets_are_skipped(tmp_path):
    inbox = tmp_path / "raw" / "inbox"
    assets = inbox / "Article_files"
    assets.mkdir(parents=True)
    (inbox / "Article.html").write_text(
        "<html><body><p>real page</p></body></html>", encoding="utf-8"
    )
    (assets / "app.js").write_text("console.log(1)\n", encoding="utf-8")
    (assets / "style.css").write_text("body{}\n", encoding="utf-8")
    (assets / "fragment.htm").write_text("<html><body></body></html>", encoding="utf-8")

    summary = intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")

    # Only the page itself is manifested; the _files/ assets are skipped as noise.
    records = manifests.list_manifests(tmp_path / "raw" / "manifests")
    assert len(records) == 1
    assert records[0]["original_filename"] == "Article.html"
    assert summary["skipped_assets"] == 3
    assert summary["skipped"] == 3
    assert {w["warning"] for w in summary["warnings"]} == {"saved_page_asset"}


def test_orphan_files_dir_without_page_is_not_skipped(tmp_path):
    # A *_files dir with no sibling HTML page is ordinary content, not a saved page.
    inbox = tmp_path / "raw" / "inbox"
    loose = inbox / "data_files"
    loose.mkdir(parents=True)
    (loose / "report.md").write_text("# real content\n", encoding="utf-8")

    summary = intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    assert summary["files_found"] == 1
    assert summary["skipped_assets"] == 0


def test_scan_does_not_modify_raw_files(tmp_path):
    inbox = _setup_inbox(tmp_path)
    before = {p.name: p.read_bytes() for p in inbox.iterdir()}
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    after = {p.name: p.read_bytes() for p in inbox.iterdir()}
    assert before == after  # nothing added, removed, or changed under raw/inbox


def test_intake_job_is_recorded(tmp_path):
    _setup_inbox(tmp_path)
    jobs_db = tmp_path / "db" / "jobs.sqlite"
    summary = intake.scan_inbox(tmp_path, jobs_db=jobs_db)

    conn = db.connect(jobs_db)
    try:
        job = db.get_job(conn, summary["job_id"])
        all_jobs = db.list_jobs(conn)
    finally:
        conn.close()

    assert job is not None
    assert job["job_type"] == "intake_scan"
    assert job["status"] == "succeeded"
    assert job["metadata"]["files_found"] == 3
    assert len(all_jobs) == 1
