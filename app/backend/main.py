from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

# Ensure the repo root is importable when launched as `uvicorn app.backend.main:app`.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.backend import db
from app.backend.config import get_settings
from app.workers import intake

settings = get_settings()
app = FastAPI(title="Knowledge System", version=settings.app_version)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "app": settings.app_name, "version": settings.app_version}


@app.get("/sources")
def list_sources() -> dict[str, Any]:
    sources = intake.list_manifests(settings.manifests_dir)
    return {"count": len(sources), "sources": sources}


@app.get("/sources/{source_id}")
def get_source(source_id: str) -> dict[str, Any]:
    manifest = intake.load_manifest(settings.manifests_dir, source_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"source not found: {source_id}")
    return manifest


@app.get("/jobs")
def list_jobs(limit: int = 100, status: str | None = None) -> dict[str, Any]:
    db.init_db(settings.jobs_db_path)
    conn = db.connect(settings.jobs_db_path)
    try:
        jobs = db.list_jobs(conn, limit=limit, status=status)
    finally:
        conn.close()
    return {"count": len(jobs), "jobs": jobs}


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    db.init_db(settings.jobs_db_path)
    conn = db.connect(settings.jobs_db_path)
    try:
        job = db.get_job(conn, job_id)
    finally:
        conn.close()
    if job is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    return job


@app.post("/jobs/intake-scan")
def run_intake_scan() -> dict[str, Any]:
    # Phase 1: synchronous execution is acceptable (see Phase 1 Plan section 9.3).
    return intake.scan_inbox(
        settings.root,
        inbox=settings.inbox_dir,
        manifests_dir=settings.manifests_dir,
        jobs_db=settings.jobs_db_path,
    )


@app.get("/wiki/index")
def read_index() -> dict[str, str]:
    path = settings.root / "wiki" / "index.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="index.md not found")
    return {
        "path": str(path.relative_to(settings.root)),
        "content": path.read_text(encoding="utf-8"),
    }
