#!/usr/bin/env python3
"""Phase 1 API schemas.

These mirror the on-disk manifest (ADR-0007) and jobs schema. Source and Job allow
extra fields so the authoritative on-disk records are never silently truncated.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


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
    model_config = ConfigDict(extra="allow")

    source_id: str
    original_filename: str
    sha256: str
    size_bytes: int
    ingestion_status: str
    retention_class: str
    occurrences: list[Occurrence] = []


class Job(BaseModel):
    model_config = ConfigDict(extra="allow")

    job_id: str
    job_type: str
    status: str
    created_at: str
    metadata: dict[str, Any] = {}
    warnings: list[Any] = []
