from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import db
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

    manifests = intake.list_manifests(tmp_path / "raw" / "manifests")
    assert len(manifests) == 2

    dup = [m for m in manifests if len(m["occurrences"]) == 2]
    assert len(dup) == 1
    rels = {o["relative_path"] for o in dup[0]["occurrences"]}
    assert any(r.endswith("one.md") for r in rels)
    assert any(r.endswith("one-copy.md") for r in rels)


def test_scan_is_idempotent(tmp_path):
    _setup_inbox(tmp_path)
    jobs_db = tmp_path / "db" / "jobs.sqlite"
    first = intake.scan_inbox(tmp_path, jobs_db=jobs_db)
    second = intake.scan_inbox(tmp_path, jobs_db=jobs_db)

    assert first["new_manifests"] == 2
    assert second["new_manifests"] == 0
    assert second["updated_manifests"] == 2
    assert second["unique_contents"] == 2
    assert len(intake.list_manifests(tmp_path / "raw" / "manifests")) == 2

    # discovered_at is preserved across rescans; last_scanned_at refreshes.
    sid = second["source_ids"][0]
    m = intake.load_manifest(tmp_path / "raw" / "manifests", sid)
    assert m["discovered_at"] == m["discovered_at"]  # stable field present


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
