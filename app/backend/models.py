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


class NormalizedPaths(BaseModel):
    # All repository-relative (ADR-0009: never expose absolute paths).
    markdown_path: str
    chunks_path: str
    tables_dir: str
    extraction_log_path: str


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
    # Phase 2 extraction state (ADR-0011). Absent on not-yet-extracted Phase 1
    # manifests, so every field is optional with a Phase 1-compatible default.
    normalized: NormalizedPaths | None = None
    extracted_at: str | None = None
    extraction_tool: str | None = None
    extraction_tool_version: str | None = None
    text_char_count: int = 0
    chunk_count: int = 0
    page_count: int | None = None


class SourcesResponse(BaseModel):
    count: int
    sources: list[Source]


class Chunk(BaseModel):
    chunk_id: str
    source_id: str
    ordinal: int
    kind: str
    heading_path: list[str] = []
    section: str | None = None
    text: str
    char_start: int
    char_end: int
    page: int | None = None
    page_end: int | None = None
    table_reference: str | None = None
    sheet_reference: str | None = None


class ChunksResponse(BaseModel):
    source_id: str
    count: int
    chunks: list[Chunk]


class NormalizedResponse(BaseModel):
    source_id: str
    markdown_path: str
    content: str


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
