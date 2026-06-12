from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.backend import main as main_module
from app.backend.config import get_settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient whose app is pointed at an isolated temp project root."""
    settings = get_settings(tmp_path)
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    settings.manifests_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


def _seed(tmp_path: Path, name: str, content: str) -> None:
    (tmp_path / "raw" / "inbox" / name).write_text(content, encoding="utf-8")


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "app": "knowledge-system",
        "version": "0.1.0",
    }


def test_intake_scan_then_sources_are_path_sanitized(client, tmp_path):
    _seed(tmp_path, "probe.md", "hello api\n")

    scan = client.post("/jobs/intake-scan")
    assert scan.status_code == 200
    assert scan.json()["files_found"] == 1

    resp = client.get("/sources")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    source = data["sources"][0]
    # Absolute path is never exposed; relative path is.
    assert "raw_path" not in source
    assert "relative_raw_path" in source
    assert source["relative_raw_path"].startswith("raw/inbox/")

    sid = source["source_id"]
    one = client.get(f"/sources/{sid}")
    assert one.status_code == 200
    assert "raw_path" not in one.json()

    assert client.get("/sources/src_doesnotexist").status_code == 404


def test_jobs_endpoints(client, tmp_path):
    _seed(tmp_path, "probe.md", "hello jobs\n")
    job_id = client.post("/jobs/intake-scan").json()["job_id"]

    listing = client.get("/jobs")
    assert listing.status_code == 200
    data = listing.json()
    assert data["count"] >= 1
    assert any(j["job_id"] == job_id for j in data["jobs"])

    one = client.get(f"/jobs/{job_id}")
    assert one.status_code == 200
    body = one.json()
    assert body["job_type"] == "intake_scan"
    assert body["status"] == "succeeded"

    assert client.get("/jobs/job_missing").status_code == 404
