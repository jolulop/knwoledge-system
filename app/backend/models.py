#!/usr/bin/env python3
"""Phase 1 API schemas.

These mirror the on-disk manifest (ADR-0007) and jobs schema and are used as
FastAPI ``response_model``s so schema drift is caught at the API boundary. The
absolute ``raw_path`` field is intentionally omitted from :class:`Source`: the API
exposes only repository-relative paths, never absolute filesystem locations.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    app: str
    version: str


class Occurrence(BaseModel):
    relative_path: str
    filename: str
    size_bytes: int
    modified_at: str
    first_seen_at: str
    last_seen_at: str


class Source(BaseModel):
    # No raw_path: absolute filesystem paths are never returned over the API.
    source_id: str
    original_filename: str
    relative_raw_path: str
    sha256: str
    size_bytes: int
    file_extension: str
    detected_mime_type: str | None = None
    created_at: str
    modified_at: str
    discovered_at: str
    last_seen_at: str
    last_scanned_at: str
    ingestion_status: str
    retention_class: str
    occurrences: list[Occurrence] = []
    notes: list[str] = []


class SourcesResponse(BaseModel):
    count: int
    sources: list[Source]


class Job(BaseModel):
    job_id: str
    job_type: str
    status: str
    source_id: str | None = None
    input_path: str | None = None
    output_path: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error_message: str | None = None
    warnings: list[Any] = []
    metadata: dict[str, Any] = {}


class JobsResponse(BaseModel):
    count: int
    jobs: list[Job]
