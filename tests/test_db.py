from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import db


def _conn(tmp_path: Path):
    path = tmp_path / "jobs.sqlite"
    db.init_db(path)
    return db.connect(path)


def test_insert_job_rejects_unknown_type_and_status(tmp_path):
    conn = _conn(tmp_path)
    try:
        with pytest.raises(ValueError):
            db.insert_job(conn, job_id="j1", job_type="bogus", status="running", created_at="t")
        with pytest.raises(ValueError):
            db.insert_job(conn, job_id="j2", job_type="extract", status="bogus", created_at="t")
        # A valid combination is accepted.
        db.insert_job(conn, job_id="j3", job_type="extract", status="running", created_at="t")
        assert db.get_job(conn, "j3") is not None
    finally:
        conn.close()


def test_update_job_rejects_unknown_status(tmp_path):
    conn = _conn(tmp_path)
    try:
        db.insert_job(conn, job_id="j1", job_type="extract", status="running", created_at="t")
        with pytest.raises(ValueError):
            db.update_job(conn, "j1", status="bogus")
        # A valid status update works.
        db.update_job(conn, "j1", status="succeeded")
        assert db.get_job(conn, "j1")["status"] == "succeeded"
    finally:
        conn.close()
