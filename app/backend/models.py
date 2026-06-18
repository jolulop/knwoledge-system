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


class WikiPage(BaseModel):
    # No absolute paths (ADR-0009); wiki_path is repository-relative.
    source_id: str
    title: str
    status: str
    ingestion_status: str | None = None
    summary_status: str | None = None
    summary: str
    wiki_path: str


class WikiPagesResponse(BaseModel):
    count: int
    pages: list[WikiPage]


class WikiPageDetail(BaseModel):
    source_id: str
    wiki_path: str
    frontmatter: dict[str, Any]
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


# --- Phase 4b graph read projection (ADR-0032 decision 5) --------------------


class GraphNodeMeta(BaseModel):
    node_id: str
    node_type: str
    slug: str | None = None
    status: str | None = None
    answer_eligible: bool


class GraphEvidence(BaseModel):
    # The edge's evidence anchor is advisory (ADR-0032): the authoritative evidence is the
    # endpoint pages' structured citations, not the edge row.
    advisory: bool = True
    source_id: str | None = None
    char_start: int | None = None
    char_end: int | None = None


class GraphAssertion(BaseModel):
    edge_id: str
    edge_type: str
    status: str
    asserted_by: str
    confidence: float | None = None
    symmetric: bool
    src_id: str
    dst_id: str
    other_node_id: str
    other: GraphNodeMeta
    evidence: GraphEvidence


class GraphNodeResponse(BaseModel):
    node: GraphNodeMeta
    outgoing: dict[str, list[GraphAssertion]]
    incoming: dict[str, list[GraphAssertion]]
    counts: dict[str, int]


class GraphEdge(BaseModel):
    edge_id: str
    src_id: str
    dst_id: str
    edge_type: str
    status: str
    asserted_by: str
    confidence: float | None = None
    symmetric: bool
    evidence: GraphEvidence


class GraphNeighborhoodNode(GraphNodeMeta):
    distance: int


class GraphNeighborhoodResponse(BaseModel):
    root_id: str
    depth: int
    nodes: list[GraphNeighborhoodNode]
    edges: list[GraphEdge]
    truncated: bool
    cap: dict[str, int]


# --- Phase 4c GET /search grouped response (ADR-0032 decisions 4, 8) ---------


class ChannelRank(BaseModel):
    # Per-channel debug detail on a fused hit (ADR-0032 addendum 7). `rank` is 1-based within the
    # channel; `score` is the channel-native score (keyword=BM25; vector=distance, lower is better).
    rank: int
    score: float


class EvidenceHit(BaseModel):
    # Authoritative citation is (source_id, char_start, char_end) + optional page/section/table
    # (ADR-0019/0020); chunk_id is advisory.
    source_id: str
    chunk_id: str | None = None
    ordinal: int | None = None
    kind: str | None = None
    section: str | None = None
    heading_path: list[str] = []
    char_start: int
    char_end: int
    page: int | None = None
    page_end: int | None = None
    table_reference: str | None = None
    sheet_reference: str | None = None
    source_status: str | None = None
    snippet: str
    # `score` is the RRF fused score (Phase 4e). `channels` carries each contributing channel's
    # 1-based rank + native score; present for single-channel hits too (one entry).
    score: float
    retrieval_path: list[str]
    channels: dict[str, ChannelRank] = {}


class NavigationHit(BaseModel):
    path: str
    page_type: str
    node_id: str | None = None
    title: str
    summary: str
    status: str
    review_status: str | None = None
    language: str | None = None
    answer_eligible: bool
    score: float


class SearchGraph(BaseModel):
    # Flat subgraph (ADR-0032 addendum: /search graph is a multi-seed BFS at the policy depth
    # budget). `seeds` are the matched node ids (BM25-ranked); nodes/edges are the induced subgraph.
    seeds: list[str]
    nodes: list[GraphNeighborhoodNode]
    edges: list[GraphEdge]
    depth: int
    truncated: bool


class SearchResponse(BaseModel):
    query: str
    mode: str
    shape: str | None = None
    retrieval_path: list[str]
    evidence: list[EvidenceHit]
    navigation: list[NavigationHit]
    graph: SearchGraph
    counts: dict[str, int]
    truncated: bool
    no_results: bool
    # Diagnostic/degradation messages (Phase 4e) — e.g. auto degraded to keyword-only because the
    # embedder was unavailable. Empty in the normal case.
    notes: list[str] = []
