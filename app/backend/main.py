from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

# Ensure the repo root is importable when launched as `uvicorn app.backend.main:app`.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.backend import db, manifests
from app.backend.config import get_settings
from app.backend.models import (
    ChunksResponse,
    HealthResponse,
    Job,
    JobsResponse,
    NormalizedResponse,
    Source,
    SourcesResponse,
)
from app.workers import extract, intake

# Hosts on which serving the unauthenticated API is acceptable (loopback only).
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", ""}


def assert_safe_bind(host: str, allow_insecure: bool) -> None:
    """Refuse to expose the unauthenticated API on a non-loopback interface.

    The API has no auth yet (ADR-0009 hardening is partial), so binding it to a LAN /
    public interface would expose mutating endpoints (intake/extract) to the network.
    Loopback is always allowed; any other host requires an explicit, acknowledged
    override via ``KS_ALLOW_INSECURE_BIND=1``.
    """
    if host in _LOOPBACK_HOSTS or allow_insecure:
        return
    raise RuntimeError(
        f"Refusing to start: APP_HOST={host!r} is not loopback and the API has no "
        "authentication. Bind to 127.0.0.1, or set KS_ALLOW_INSECURE_BIND=1 to override "
        "(not recommended — see policies/security.yaml)."
    )


settings = get_settings()
assert_safe_bind(settings.app_host, os.environ.get("KS_ALLOW_INSECURE_BIND") == "1")
app = FastAPI(title="Knowledge System", version=settings.app_version)


@app.get("/health", response_model=HealthResponse)
def health() -> dict[str, Any]:
    return {"status": "ok", "app": settings.app_name, "version": settings.app_version}


@app.get("/sources", response_model=SourcesResponse)
def list_sources() -> dict[str, Any]:
    sources = manifests.list_manifests(settings.manifests_dir)
    return {"count": len(sources), "sources": sources}


@app.get("/sources/{source_id}", response_model=Source)
def get_source(source_id: str) -> dict[str, Any]:
    manifest = manifests.load_manifest(settings.manifests_dir, source_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"source not found: {source_id}")
    return manifest


@app.get("/jobs", response_model=JobsResponse)
def list_jobs(limit: int = 100, status: str | None = None) -> dict[str, Any]:
    db.init_db(settings.jobs_db_path)
    conn = db.connect(settings.jobs_db_path)
    try:
        jobs = db.list_jobs(conn, limit=limit, status=status)
    finally:
        conn.close()
    return {"count": len(jobs), "jobs": jobs}


@app.get("/jobs/{job_id}", response_model=Job)
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


@app.post("/jobs/extract")
def run_extract(force: bool = False) -> dict[str, Any]:
    # Phase 2 extraction runs synchronously and offline (no API keys).
    return extract.extract_sources(
        settings.root,
        force=force,
        manifests_dir=settings.manifests_dir,
        jobs_db=settings.jobs_db_path,
        normalized_dir=settings.normalized_dir,
        max_file_mb=settings.extract_max_file_mb,
        timeout_s=settings.extract_timeout_s,
        target_chars=settings.chunk_target_chars,
        max_chars=settings.chunk_max_chars,
    )


_EXTRACTED_STATUSES = {"extracted", "partial"}


def _require_extracted(source_id: str) -> None:
    """404 unless the manifest exists and currently reports a usable extraction.

    Gating on ``ingestion_status`` (not just file existence) ensures an error-state or
    not-yet-extracted source never serves artifacts, and that the served evidence is
    the state the manifest claims (consistency after a preserved-last-good failure).
    """
    manifest = manifests.load_manifest(settings.manifests_dir, source_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"source not found: {source_id}")
    if manifest.get("ingestion_status") not in _EXTRACTED_STATUSES:
        raise HTTPException(status_code=404, detail=f"source not extracted: {source_id}")


@app.get("/sources/{source_id}/chunks", response_model=ChunksResponse)
def get_source_chunks(source_id: str) -> dict[str, Any]:
    _require_extracted(source_id)
    path = settings.chunks_dir / f"{source_id}.jsonl"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"source not extracted: {source_id}")
    chunks: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            chunks.append(json.loads(line))
    return {"source_id": source_id, "count": len(chunks), "chunks": chunks}


@app.get("/sources/{source_id}/normalized", response_model=NormalizedResponse)
def get_source_normalized(source_id: str) -> dict[str, Any]:
    _require_extracted(source_id)
    path = settings.markdown_dir / f"{source_id}.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"source not extracted: {source_id}")
    return {
        "source_id": source_id,
        "markdown_path": f"normalized/markdown/{source_id}.md",
        "content": path.read_text(encoding="utf-8"),
    }


@app.get("/wiki/index")
def read_index() -> dict[str, str]:
    path = settings.root / "wiki" / "index.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="index.md not found")
    return {
        "path": str(path.relative_to(settings.root)),
        "content": path.read_text(encoding="utf-8"),
    }
