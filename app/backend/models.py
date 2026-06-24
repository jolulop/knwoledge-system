#!/usr/bin/env python3
"""Phase 1 API schemas.

These mirror the on-disk manifest (ADR-0007) and jobs schema and are used as
FastAPI ``response_model``s so schema drift is caught at the API boundary. The
absolute ``raw_path`` field is intentionally omitted from :class:`Source`: the API
exposes only repository-relative paths, never absolute filesystem locations.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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
    # Source lifecycle status — the manifest is the authority (ADR-0036 decision 13); default `active`
    # when unset. Distinct from `retention_class` (policy category).
    status: str = "active"
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
    # Count of quarantined manifests (non-canonical id / filename mismatch / duplicate). Count only —
    # never the skipped filenames or ids (no echo of untrusted input).
    manifests_skipped_invalid: int = 0
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


class QueryRequest(BaseModel):
    # POST /query body (Phase 5, ADR-0034). Distinct from GET /search?q= on purpose — /query is
    # synthesis (potentially long-form, later save-capable). `mode` is restricted to the
    # evidence-producing modes at the endpoint (graph/navigation can't cite).
    question: str
    mode: str = "auto"
    source_id: str | None = None
    source_status: str | None = None
    language: str | None = None
    include_unsourced: bool = False  # LOCAL/DEBUG only: return ungrounded claim *text* (counts always present)
    save: bool = False  # persist the answer to wiki/Queries/<query_id>.md (explicit; ADR-0034)


class QueryCitation(BaseModel):
    # Resolved answer citation — authoritative (source_id, char_start, char_end) + advisory locators
    # and the verbatim source quote (Phase 5, ADR-0034). No system/generated filesystem paths are ever
    # exposed; a path-shaped substring inside `quote` is verbatim source content (preserved, ADR-0034 Q2).
    source_id: str
    char_start: int
    char_end: int
    page: int | None = None
    page_end: int | None = None
    section: str | None = None
    table_reference: str | None = None
    sheet_reference: str | None = None
    chunk_id: str | None = None
    quote: str


class QueryClaim(BaseModel):
    text: str
    citations: list[QueryCitation]


class QueryResponse(BaseModel):
    # POST /query (Phase 5, ADR-0034). Every claim in `claims`/`answer` is mechanically grounded;
    # `abstained` + the "No source found in vault." answer text mean nothing grounded.
    query: str
    mode: str
    retrieval_path: list[str]
    answer: str
    abstained: bool
    claims: list[QueryClaim] = []
    citations: list[QueryCitation] = []
    evidence_count: int
    # Audit signals only (ADR-0034 Q2): the ordinary-unsourced and security-rejected counts are always
    # present; the unsourced *text* is exposed only under include_unsourced (debug/review). Path-leak
    # rejections are never returned verbatim — count only.
    unsourced_count: int
    security_rejected_count: int
    unsourced_claims: list[str] = []
    notes: list[str] = []
    query_id: str | None = None  # set only when save=true (the saved wiki/Queries/<id>.md)
    # True when a page was saved but the nav/index were NOT synchronously rebuilt — the saved query is
    # discoverable only after the next reindex (ADR-0034 Q3). False/None when nothing was saved.
    navigation_stale: bool = False


# --- Phase 6 review ledger (ADR-0035) --------------------------------------


class ReviewItem(BaseModel):
    # One stored review item (reviews/<status>/<id>.json). subject/proposal/context are per-type
    # free-form payloads (ADR-0018), so they stay permissive dicts; the typed governance shape is the
    # per-type preview projection below, not this raw record.
    review_id: str
    type: str
    status: str
    priority: str = "low"
    created_at: str | None = None
    subject: dict[str, Any] = {}
    proposal: dict[str, Any] = {}
    context: dict[str, Any] = {}
    decided_by: str | None = None
    decided_at: str | None = None
    decision_note: str | None = None


class ReviewListResponse(BaseModel):
    # GET /reviews (ADR-0035 A3). count/by_type cover the full filtered set (status+type+priority)
    # *before* limit/offset; items is the deterministically-sorted post-pagination window. Two skip
    # counters keep the queue crash-proof and diagnosable: parse_errors (unreadable/invalid/non-object
    # JSON) vs schema_errors (valid JSON object that is not a usable ReviewItem shape).
    count: int
    by_type: dict[str, int]
    parse_errors: int
    schema_errors: int
    items: list[ReviewItem]


class ReviewApply(BaseModel):
    # Read-only, best-effort apply-capability + effect state derived from wiki/graph (ADR-0035 A2).
    # effect_status in {pending_apply, effected, apply_deferred, unknown, no_effect_required};
    # no_effect_required = a decided item that owes no world change (rejected promote / rejected
    # in-scope deprecate). `effected` is None for record-only types (never implies a *failed* apply).
    supported: bool
    executor: str | None = None
    effect_status: str
    effected: bool | None = None
    warnings: list[str] = []


class ReviewPreview(BaseModel):
    # The mandatory normalized preview a human sees before approving (ADR-0035 A1, decision 6). Built
    # by a per-type projector; record-only/unknown types use a shared fallback. Not a mutation diff.
    review_id: str | None = None
    type: str | None = None
    status: str | None = None
    summary: str = ""
    affected_paths: list[str] = []
    node_ids: list[str] = []
    current_status: str | None = None
    proposed_status: str | None = None
    proposed_action: str | None = None
    warnings: list[str] = []
    apply: ReviewApply
    details: dict[str, Any] = {}


class ReviewDetailResponse(BaseModel):
    # GET /reviews/{id} — the full stored item plus its preview projection.
    item: ReviewItem
    preview: ReviewPreview


class ReviewDecisionRequest(BaseModel):
    # Optional body for the decision endpoints (Phase 6 slice 6-2). Loopback single-user, so the
    # decider is server-fixed ("human"); only an optional free-text note is accepted.
    note: str = ""


class ReviewDecisionResponse(BaseModel):
    # POST /reviews/{id}/approve|reject|defer — record-only (ADR-0035 decision 3). No effect is applied
    # here; apply_required flags whether a later POST /reviews/apply is relevant to this decision.
    review_id: str
    decision_recorded: bool
    status: str
    apply_required: bool


class FailedValidator(BaseModel):
    # One validate_*.py that exited non-zero during POST /reviews/apply (ADR-0035 A6). Tails only —
    # no absolute paths are surfaced (the validators print repo-relative diagnostics).
    name: str
    returncode: int
    stdout_tail: str = ""
    stderr_tail: str = ""


class ReindexResponse(BaseModel):
    # POST /jobs/reindex (Phase 7, ADR-0036). Index + keyword only (never vector). status "failed" means
    # a sub-step (rebuild_index script non-zero, or keyword reindex error) failed — not a silent success.
    job_id: str
    status: str  # succeeded | failed
    index_rebuilt: bool
    keyword_reindexed: bool
    warnings: list[str] = []


class LintValidator(BaseModel):
    # One structural validator's result in the /jobs/lint report (tails sanitized of the root path).
    name: str
    returncode: int
    stdout_tail: str = ""
    stderr_tail: str = ""


class LintFinding(BaseModel):
    # One lint finding. `check` names the rule; `severity` ∈ {high, medium, low}; `subject` is the
    # source/node id. `data` carries optional machine-actionable fields (e.g. source_id/claim_id/char
    # range + a stable `remediation` code like rerun_enrich / rerun_extract_claims), ADR-0037.
    check: str
    severity: str
    subject: str | None = None
    detail: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class StaleCheckResponse(BaseModel):
    # POST /jobs/stale-check (Phase 7, ADR-0036). Detect-and-propose: files archive_source candidates
    # (stale active sources) + delete_raw_file candidates (ephemeral past window, record-only). Changes
    # no status itself. *_filed = newly created this run; *_existing = already in the ledger.
    job_id: str
    considered: int
    archive_candidates: int          # detected (regardless of filing)
    archive_candidates_filed: int    # newly created this run
    archive_candidates_existing: int  # already in the ledger
    delete_candidates: int
    delete_candidates_filed: int
    delete_candidates_existing: int
    # Live LLM-cache retention stats every run (cache_present/readable/entries/total_mb/over_bounds/…);
    # the aggregate purge_response_cache item is record-only. Carries no cached responses or keys.
    cache: dict[str, Any] = {}
    cache_purge_filed: int = 0
    cache_purge_existing: int = 0
    warnings: list[str] = []
    archive_review_items_filed: list[str] = []
    delete_review_items_filed: list[str] = []
    cache_purge_review_items_filed: list[str] = []


class LintResponse(BaseModel):
    # POST /jobs/lint (Phase 7, ADR-0036). Detect-and-propose health pass; lint health is an outcome,
    # not an abort (none of these are HTTP errors). `status`: "failing" (a validator failed or a
    # high-severity finding), "degraded" (completed but coverage incomplete — graph absent so semantic
    # checks skipped — nothing failing), "healthy". `review_items_filed` are newly created this run;
    # `review_items_existing` were already in the ledger (not re-created).
    job_id: str
    status: str  # healthy | degraded | failing
    validators_ok: bool
    validators: list[LintValidator] = []
    findings: list[LintFinding] = []
    by_check: dict[str, int] = {}
    review_items_filed: list[str] = []
    review_items_existing: list[str] = []
    graph_available: bool


class ReviewApplyResponse(BaseModel):
    # POST /reviews/apply (ADR-0035 A4/A6). Non-transactional: effects are written, then validators run
    # once. status is "applied" (clean) or "validation_failed" (apply ran; the vault now reports an
    # inconsistency to resolve) — a validator failure is HTTP 200, not 500. `summary` carries the typed
    # per-executor counts + honest `unapplied` (approved types with no Phase-6 executor).
    status: str
    applied: bool
    validators_ok: bool
    failed_validators: list[FailedValidator] = []
    warnings: list[str] = []  # non-fatal post-apply issues, e.g. "index_rebuild_failed"
    summary: dict[str, Any]
