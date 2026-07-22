from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import mimetypes
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

logger = logging.getLogger(__name__)

# Ensure the repo root is importable when launched as `uvicorn app.backend.main:app`.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.backend import (
    apply_sandbox, db, embeddings, graph, graph_read, keyword_index, manifests, review_html,
    review_read, search, taxonomy, vector_index,
)
from app.backend.config import get_settings
from app.backend.paths import safe_under
from app.backend.models import (
    ChunksResponse,
    GraphNeighborhoodResponse,
    GraphNodeResponse,
    HealthResponse,
    Job,
    JobsResponse,
    LintResponse,
    NormalizedResponse,
    QueryRequest,
    ReindexResponse,
    StaleCheckResponse,
    QueryResponse,
    EvalRunRequest,
    EvalRunResponse,
    EvalResultsResponse,
    ReviewApplyResponse,
    ReviewDryRunResponse,
    ReviewDecisionRequest,
    ReviewDecisionResponse,
    ReviewReopenRequest,
    ReviewReopenResponse,
    ReviewDetailResponse,
    ReviewListResponse,
    SearchResponse,
    Source,
    SourcesResponse,
    WikiPageDetail,
    WikiPagesResponse,
)
from app.backend.policy import load_retrieval_policy, load_yaml
from app.llm.adapters import AdapterError
from app.llm.cache import ResponseCache
from app.llm.client import ConfigError, ParseError, build_client
from app.workers import (
    claims, contradictions, deprecations, duplicates, eval_answers, extract, human_add, intake,
    lint, merges, promote, query, retention, retypes, reviews, splits, synthesis, wiki,
)
from app.workers import labels
from app.workers.wiki_render import parse_frontmatter, render_query_page

# Hosts on which serving the unauthenticated API is acceptable (loopback only).
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", ""}


def assert_safe_bind(host: str, allow_insecure: bool) -> None:
    """Refuse to expose the unauthenticated API on a non-loopback interface.

    The app has **no application-level authentication or CSRF** (loopback-only posture, ADR-0009;
    auth+CSRF is Phase-8-class future work). Binding to a LAN/public interface would expose the mutating
    *and* browser-UI endpoints unauthenticated. Loopback is always allowed.

    ``KS_ALLOW_INSECURE_BIND=1`` is a narrow **internal-transport escape hatch** — for a trusted private
    network or a container sidecar where an external layer (e.g. a TLS/auth reverse proxy) fronts the app.
    It does NOT add auth or CSRF, and a fronting proxy is NOT equivalent to app-level CSRF protection for
    the browser UI routes. Never use it to reach untrusted networks. Using it logs a loud warning.
    """
    if host in _LOOPBACK_HOSTS:
        return
    if allow_insecure:
        logging.getLogger(__name__).warning(
            "KS_ALLOW_INSECURE_BIND=1: serving the UNAUTHENTICATED API on non-loopback APP_HOST=%r. "
            "Internal-transport escape hatch (trusted private network / container sidecar behind a "
            "TLS/auth proxy) ONLY — this provides no auth and no CSRF (ADR-0009). Do not expose to "
            "untrusted networks.", host)
        print(
            f"WARNING: KS_ALLOW_INSECURE_BIND=1 — unauthenticated API on non-loopback {host!r}; "
            "escape hatch for a trusted private network / sidecar only (no auth, no CSRF).",
            file=sys.stderr)
        return
    raise RuntimeError(
        f"Refusing to start: APP_HOST={host!r} is not loopback and the API has no authentication. "
        "Bind to 127.0.0.1, or set KS_ALLOW_INSECURE_BIND=1 ONLY behind a trusted proxy/sidecar "
        "(internal-transport escape hatch; not a substitute for auth+CSRF — see policies/security.yaml)."
    )


settings = get_settings()
assert_safe_bind(settings.app_host, os.environ.get("KS_ALLOW_INSECURE_BIND") == "1")


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Startup warmup (ADR-0053 decision 6): load the in-process embedding backend once and fail fast
    ONLY when ``EMBEDDING_PROVIDER=flagembedding_bge_m3`` is selected. Every other provider (and the
    light install) skips this entirely — no Torch import — so ingest/review/lint stay GPU-independent.
    A warmup failure (CUDA requested-but-unavailable, or model load) aborts startup by design."""
    info = embeddings.warmup_provider(settings)
    if info is not None:
        logger.info(
            "embedding backend ready: %s",
            {k: info.get(k) for k in ("model_ref", "device", "cuda_device_name", "model_loaded")},
        )
    yield


app = FastAPI(title="Knowledge System", version=settings.app_version, lifespan=_lifespan)


@app.get("/health", response_model=HealthResponse)
def health() -> dict[str, Any]:
    return {"status": "ok", "app": settings.app_name, "version": settings.app_version}


@app.get("/sources", response_model=SourcesResponse)
def list_sources() -> dict[str, Any]:
    # Quarantine tampered/misnamed/duplicate manifests like the workers do; expose only counts for
    # skipped records (never their filenames/ids) so the API can't echo attacker-controlled input.
    sources, skipped = manifests.valid_manifests(settings.manifests_dir)
    return {"count": len(sources), "manifests_skipped_invalid": len(skipped), "sources": sources}


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


@app.post("/jobs/generate-wiki")
def run_generate_wiki(force: bool = False) -> dict[str, Any]:
    # Phase 3 generation runs synchronously and offline (no API keys).
    return wiki.generate_wiki(
        settings.root,
        force=force,
        manifests_dir=settings.manifests_dir,
        jobs_db=settings.jobs_db_path,
        wiki_dir=settings.wiki_dir,
        templates_dir=settings.templates_dir,
        markdown_dir=settings.markdown_dir,
        summary_max=settings.wiki_summary_max_chars,
        summary_min=settings.wiki_summary_min_chars,
    )


def _summary_text(text: str) -> str:
    """First body line(s) of the > [!summary] callout, label stripped."""
    parts: list[str] = []
    in_callout = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("> [!summary]"):
            in_callout = True
            continue
        if in_callout:
            if stripped.startswith(">"):
                body = stripped.lstrip(">").strip()
                if body:
                    parts.append(body)
            else:
                break
    return " ".join(parts)


@app.get("/wiki/pages", response_model=WikiPagesResponse)
def list_wiki_pages() -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    if settings.sources_dir.exists():
        for path in sorted(settings.sources_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            fm = parse_frontmatter(text)
            pages.append({
                "source_id": fm.get("source_id", path.stem),
                "title": fm.get("title", path.stem),
                "status": fm.get("status", "unknown"),
                "ingestion_status": fm.get("ingestion_status"),
                "summary_status": fm.get("summary_status"),
                "summary": _summary_text(text),
                "wiki_path": f"wiki/Sources/{path.name}",
            })
    return {"count": len(pages), "pages": pages}


@app.get("/wiki/pages/{source_id}", response_model=WikiPageDetail)
def get_wiki_page(source_id: str) -> dict[str, Any]:
    path = settings.sources_dir / f"{source_id}.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"wiki page not found: {source_id}")
    text = path.read_text(encoding="utf-8")
    return {
        "source_id": source_id,
        "wiki_path": f"wiki/Sources/{source_id}.md",
        "frontmatter": parse_frontmatter(text),
        "content": text,
    }


def _open_graph() -> Any:
    """Open the authoritative graph for read, or ``None`` if it does not exist yet.

    A missing ``db/graph.sqlite`` (no semantic graph built yet) is not an error — the graph
    endpoints simply report the node as not found. A present-but-wrong-schema database is a
    controlled 503 (rebuild required) rather than an uncontrolled 500 deeper in a query.
    """
    if not settings.graph_db_path.exists():
        return None
    conn = graph.connect(settings.graph_db_path)
    version = graph.schema_version(conn)
    if version != graph.SCHEMA_VERSION:
        conn.close()
        raise HTTPException(
            status_code=503,
            detail=(
                f"graph index unavailable: schema version {version} != expected "
                f"{graph.SCHEMA_VERSION}; rebuild db/graph.sqlite"
            ),
        )
    return conn


@app.get("/graph/node/{node_id}", response_model=GraphNodeResponse)
def get_graph_node(node_id: str, include_status: str | None = None) -> dict[str, Any]:
    conn = _open_graph()
    if conn is None:
        raise HTTPException(status_code=404, detail=f"node not found: {node_id}")
    try:
        try:
            statuses = graph_read.parse_edge_statuses(include_status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        view = graph_read.node_view(conn, node_id, include_status=statuses)
    finally:
        conn.close()
    if view is None:
        raise HTTPException(status_code=404, detail=f"node not found: {node_id}")
    return view


@app.get("/graph/neighborhood/{node_id}", response_model=GraphNeighborhoodResponse)
def get_graph_neighborhood(
    node_id: str,
    depth: int = Query(graph_read.DEFAULT_DEPTH, ge=0, le=graph_read.MAX_DEPTH),
    edge_types: str | None = None,
    node_types: str | None = None,
    include_status: str | None = None,
    node_limit: int = Query(graph_read.DEFAULT_MAX_NODES, ge=1, le=graph_read.HARD_MAX_NODES),
    edge_limit: int = Query(graph_read.DEFAULT_MAX_EDGES, ge=1, le=graph_read.HARD_MAX_EDGES),
) -> dict[str, Any]:
    conn = _open_graph()
    if conn is None:
        raise HTTPException(status_code=404, detail=f"node not found: {node_id}")
    try:
        try:
            statuses = graph_read.parse_edge_statuses(include_status)
            et = graph_read.parse_edge_types(edge_types)
            nt = graph_read.parse_node_types(node_types)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = graph_read.neighborhood(
            conn, node_id, depth=depth, edge_types=et, node_types=nt,
            include_status=statuses, node_cap=node_limit, edge_cap=edge_limit,
        )
    finally:
        conn.close()
    if result is None:
        raise HTTPException(status_code=404, detail=f"node not found: {node_id}")
    return result


@app.get("/reviews", response_model=ReviewListResponse)
def list_reviews(
    status: str = "pending",
    type: str | None = None,  # noqa: A002 - the public ?type= filter
    priority: str | None = None,
    limit: int | None = Query(None, ge=1),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Phase 6 review ledger (ADR-0035): deterministic, malformed-robust list of review items.

    Filters on the item's explicit ``status`` field (``pending``/``deferred`` share ``reviews/
    pending/``); ``count``/``by_type`` cover the full filtered set before pagination. Read-only.
    """
    try:
        return review_read.list_reviews(
            settings.reviews_dir, status=status, type=type, priority=priority,
            limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/reviews/{review_id}", response_model=ReviewDetailResponse)
def get_review(review_id: str) -> dict[str, Any]:
    """One review item plus its mandatory normalized preview projection (ADR-0035 A1, decision 6).

    The preview's ``apply`` block carries a read-only, best-effort ``effect_status`` derived from the
    actual wiki/graph state. A corrupt review file is a 404 (the queue never crashes on bad JSON).
    """
    result = review_read.get_review(
        settings.reviews_dir, review_id, graph_db=settings.graph_db_path,
        wiki_dir=settings.wiki_dir, manifests_dir=settings.manifests_dir)
    # A missing, corrupt, or schema-invalid review file is a 404 (the read model never 500s on bad
    # queue state; the parse_error/schema_error markers are diagnostic only).
    if result is None or result.get("parse_error") or result.get("schema_error"):
        raise HTTPException(status_code=404, detail=f"review not found: {review_id}")
    return result


def _validate_supersede_winner(decision: str, rtype: str, item: dict[str, Any], winner: str) -> None:
    """Request-shape validation for a contradiction supersede winner (ADR-0044). 400 on any misuse;
    never silently ignored. Does NOT read the graph (claims-active is a separate 409 check)."""
    if decision != "approved":
        raise HTTPException(status_code=400,
                            detail="`winner` is only valid on an approve decision")
    if rtype != "resolve_contradiction":
        raise HTTPException(status_code=400,
                            detail="`winner` is only valid for a resolve_contradiction item")
    subj = item.get("subject") or {}
    a, b = subj.get("claim_a"), subj.get("claim_b")
    # Canonical-shape gate FIRST (untrusted ledger): a tampered subject/winner must be 400 before any
    # page read — a non-canonical id must never be recorded or handed to the executor/filesystem.
    if not (claims.is_claim_id(a) and claims.is_claim_id(b) and claims.is_claim_id(winner)):
        raise HTTPException(
            status_code=400,
            detail="contradiction claim ids and `winner` must be canonical (clm_<16 hex>)")
    if winner not in (a, b):
        raise HTTPException(
            status_code=400,
            detail="`winner` must be one of the two contradicting claims (subject.claim_a/claim_b)")


def _require_active_claims(item: dict[str, Any]) -> None:
    """Both contradicting Claim pages must exist with frontmatter status `active` to supersede (ADR-0044).
    The Claim PAGE frontmatter is the node-status authority (ADR-0022/0030) — graph-free, so this never
    503s; graph drift after the decision is the dry-run/apply's job. 409 if a claim is missing/non-active."""
    subj = item.get("subject") or {}
    for cid in (subj.get("claim_a"), subj.get("claim_b")):
        fm = review_read._page_frontmatter(settings.wiki_dir, f"Claims/{cid}.md")
        if fm is None or fm.get("status") != "active":
            raise HTTPException(
                status_code=409,
                detail=f"claim {cid} is no longer active or its page is missing — cannot supersede")


_AMENDMENT_FIELDS = frozenset({"title", "aliases", "description", "item_type"})
_AMEND_MAX_TITLE, _AMEND_MAX_ALIAS, _AMEND_MAX_ALIASES, _AMEND_MAX_DESCRIPTION = 200, 120, 16, 2000


def _validate_amendments(decision: str, rtype: str,
                         amendments: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize + validate the ADR-0058 amendments payload; 400 on any shape violation.

    Valid ONLY for a promote_candidate_node on approve (frozen into the ledger) or defer
    (preserved as a mutable draft). Allowed fields exactly title/aliases/description/item_type
    (ADR-0059: item_type must be one of the 15 production types — the sentinel is never an
    amendment target — and it is REQUIRED before an unclassified candidate's approval can
    apply); a blank title is treated as not-provided (a title can be corrected, never erased);
    a blank description explicitly clears the page field. Returns the normalized payload, or
    None when nothing effective was provided.
    """
    if rtype != "promote_candidate_node":
        raise HTTPException(
            status_code=400,
            detail=f"amendments are only valid for promote_candidate_node, not {rtype}")
    if decision not in ("approved", "deferred"):
        raise HTTPException(
            status_code=400,
            detail="amendments are only valid on approve (recorded) or defer (draft)")
    unknown = sorted(set(amendments) - _AMENDMENT_FIELDS)
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown amendment field(s): {unknown}")
    out: dict[str, Any] = {}
    title = amendments.get("title")
    if title is not None:
        if not isinstance(title, str):
            raise HTTPException(status_code=400, detail="amendment title must be a string")
        title = re.sub(r"\s+", " ", title).strip()[:_AMEND_MAX_TITLE]
        if title:
            out["title"] = title
    aliases = amendments.get("aliases")
    if aliases is not None:
        if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
            raise HTTPException(
                status_code=400, detail="amendment aliases must be a list of strings")
        out["aliases"] = [a.strip()[:_AMEND_MAX_ALIAS]
                          for a in aliases if a.strip()][:_AMEND_MAX_ALIASES]
    description = amendments.get("description")
    if description is not None:
        if not isinstance(description, str):
            raise HTTPException(status_code=400, detail="amendment description must be a string")
        # Canonical single-line prose (review round): whitespace collapsed at the boundary; an
        # explicitly-blank description clears the page field.
        out["description"] = re.sub(r"\s+", " ", description).strip()[:_AMEND_MAX_DESCRIPTION]
    item_type = amendments.get("item_type")
    if item_type is not None:
        if not taxonomy.is_production_item_type(item_type):
            raise HTTPException(
                status_code=400,
                detail="amendment item_type must be one of the production taxonomy values")
        out["item_type"] = item_type
    return out or None


def _record_decision(
    review_id: str, decision: str, body: ReviewDecisionRequest | None
) -> dict[str, Any]:
    """Record a human decision (record-only; ADR-0035 decision 3). No effect is applied here.

    A recorded terminal decision (approved/rejected) is immutable: re-sending the same decision is an
    idempotent no-op (``decision_recorded: false``); trying to flip it is a 409. A pending or deferred
    item can be approved/rejected/deferred. Missing/corrupt item -> 404.
    """
    note = body.note if body else ""
    winner = (body.winner if body else None) or None
    amendments = (body.amendments if body else None) or None
    item, error = review_read.find_review(settings.reviews_dir, review_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"review not found: {review_id}")
    current = item.get("status")
    rtype = str(item.get("type"))
    # ADR-0044: a `winner` is ONLY valid on an approve of a resolve_contradiction, naming one of the two
    # contradicting claims. Request-shape violations are 400 (never silently ignored). The claims-active
    # check (page-frontmatter authority, graph-free) is a 409 and runs only when actually resolving.
    if winner is not None:
        _validate_supersede_winner(decision, rtype, item, winner)
    # ADR-0058: amendments are ONLY valid on an approve (frozen) or defer (draft) of a
    # promote_candidate_node — shape violations are 400, never silently ignored.
    if amendments is not None:
        amendments = _validate_amendments(decision, rtype, amendments)
    if current in ("approved", "rejected"):
        if current != decision:
            raise HTTPException(
                status_code=409,
                detail=f"review {review_id} already decided as {current}; decisions are immutable")
        recorded, final = False, current  # idempotent: same terminal decision re-sent (winner unchanged)
    elif decision == "deferred":
        recorded = reviews.defer_review_item(settings.reviews_dir, review_id, note=note,
                                             draft_amendments=amendments)
        final = "deferred"
    else:
        if winner is not None:
            _require_active_claims(item)  # 409 if either Claim page is missing / not active
        recorded = reviews.resolve_review_item(
            settings.reviews_dir, review_id, decision=decision, decided_by="human", note=note,
            winner=winner, amendments=amendments if decision == "approved" else None)
        final = decision
    return {
        "review_id": review_id,
        "decision_recorded": recorded,
        "status": final,
        "apply_required": review_read.decision_apply_required(rtype, final),
    }


@app.post("/reviews/{review_id}/approve", response_model=ReviewDecisionResponse)
def approve_review(
    review_id: str, body: ReviewDecisionRequest | None = None
) -> dict[str, Any]:
    """Record an approval (record-only). The effect is applied later by POST /reviews/apply."""
    return _record_decision(review_id, "approved", body)


@app.post("/reviews/{review_id}/reject", response_model=ReviewDecisionResponse)
def reject_review(
    review_id: str, body: ReviewDecisionRequest | None = None
) -> dict[str, Any]:
    """Record a rejection (record-only)."""
    return _record_decision(review_id, "rejected", body)


@app.post("/reviews/{review_id}/defer", response_model=ReviewDecisionResponse)
def defer_review(
    review_id: str, body: ReviewDecisionRequest | None = None
) -> dict[str, Any]:
    """Defer a decision: keep the item in pending/ with status: deferred (record-only)."""
    return _record_decision(review_id, "deferred", body)


def _reopen_decision(review_id: str, reason: str) -> dict[str, Any]:
    """Reopen a terminal review item back to pending so it can be re-decided (ADR-0045). Shared by the
    JSON endpoint + the UI route. Graph-aware (unlike the graph-free approve): it projects the item to
    read the live effect_status and reopens ONLY when that proves no live effect.

    404 missing; 400 blank reason; 409 if the item is not terminal, or its effect is live / unconfirmable
    (EFFECTED / UNKNOWN / INVALID_SUBJECT / APPLY_DEFERRED -> reason code). No ledger mutation on refusal.
    """
    reason = (reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reopen requires a non-empty reason")
    result = review_read.get_review(
        settings.reviews_dir, review_id, graph_db=settings.graph_db_path,
        wiki_dir=settings.wiki_dir, manifests_dir=settings.manifests_dir)
    if result is None or result.get("parse_error") or result.get("schema_error"):
        raise HTTPException(status_code=404, detail=f"review not found: {review_id}")
    status = result["item"].get("status")
    if status not in ("approved", "rejected"):
        raise HTTPException(
            status_code=409,
            detail=f"review {review_id} is not a terminal decision (status: {status}); nothing to reopen")
    effect_status = (result["preview"].get("apply") or {}).get("effect_status")
    block = review_read.reopen_block_reason(effect_status)
    if block is not None:
        raise HTTPException(
            status_code=409,
            detail=f"cannot reopen {review_id}: {block} (effect_status={effect_status})")
    reopened = reviews.reopen_review_item(settings.reviews_dir, review_id, reason=reason)
    return {"review_id": review_id, "reopened": reopened, "status": "pending"}


@app.post("/reviews/{review_id}/reopen", response_model=ReviewReopenResponse)
def reopen_review(review_id: str, body: ReviewReopenRequest) -> dict[str, Any]:
    """Reopen a not-yet-applied terminal decision back to pending to be re-decided (ADR-0045)."""
    return _reopen_decision(review_id, body.reason)


# Review types POST /reviews/apply has a deterministic executor for (ADR-0035 A4/A5). Any other
# approved type is reported honestly as `unapplied` (record-only / raw-touching).
_APPLY_TYPES = frozenset({
    "propose_synthesis", "resolve_contradiction", "promote_candidate_node", "deprecate_wiki_page",
    "archive_source", "mark_semantic_duplicate", "hide_content", "hide_semantic_page",
    "unhide_content", "unhide_semantic_page", "hide_claim", "unhide_claim",
    "hide_synthesis", "unhide_synthesis", "merge_items",
    "change_item_type", "split_item"})
# Types whose application *requires* the graph (so a missing graph with such approved items -> 503).
# archive_source + hide_content + unhide_content are executor-backed but NOT graph-required — their core
# effect is the manifest status + Source page; the graph source-node mirror is best-effort (skipped when
# graph absent). The semantic hide/unhide ARE graph-required (page + graph via recompose).
_GRAPH_REQUIRED_TYPES = _APPLY_TYPES - {"archive_source", "hide_content", "unhide_content"}


def _rebuild_index_status(root: Path) -> str:
    """Rebuild wiki/index.md (caller owns the single rebuild). "rebuilt"|"failed"|"missing".

    `missing` (no script — a degraded/test env) is distinct from `failed` (script ran, non-zero) so
    only a genuine failure after real changes is surfaced as a warning (ADR-0035 review round)."""
    script = root / "scripts" / "rebuild_index.py"
    if not script.exists():
        return "missing"
    return "rebuilt" if subprocess.run(
        [sys.executable, str(script), str(root)]).returncode == 0 else "failed"


def _run_all_validators(root: Path) -> list[dict[str, Any]]:
    """Run the structural validator suite (sanitized), shared with /jobs/lint. Module-level so tests
    can monkeypatch it; the implementation lives in the lint worker (single source, no drift)."""
    return lint.run_validators(root)


def _approved_graph_backed_count(reviews_dir: Path) -> int:
    """Count approved items whose application *requires* the graph (archive_source is excluded)."""
    n = 0
    d = reviews_dir / "approved"
    for path in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(item, dict) and item.get("type") in _GRAPH_REQUIRED_TYPES:
            n += 1
    return n


def _unapplied_by_type(reviews_dir: Path) -> list[dict[str, Any]]:
    """Approved items whose type has no Phase-6 executor — reported honestly, not hidden (ADR-0035)."""
    counts: dict[str, int] = {}
    d = reviews_dir / "approved"
    for path in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rtype = item.get("type") if isinstance(item, dict) else None
        if rtype and rtype not in _APPLY_TYPES:
            counts[rtype] = counts.get(rtype, 0) + 1
    return [{"type": t, "count": c, "reason": "no_executor_in_phase_6"}
            for t, c in sorted(counts.items())]


class GraphUnavailable(Exception):
    """Raised inside ``run_apply`` when approved graph-required items wait but the graph is unavailable.

    Lives inside the shared orchestration so live apply and dry-run refuse on **exactly** the same
    condition (ADR-0040 decision 6); each endpoint maps it to its own surface (HTTP 503 vs a structured
    `blocked` preview).
    """


def _open_graph_safe_at(graph_db: Path) -> Any:
    """Open the graph at an explicit path, or None if absent/schema-mismatched (never creates it)."""
    if not Path(graph_db).exists():
        return None
    conn = graph.connect(graph_db)
    if graph.schema_version(conn) != graph.SCHEMA_VERSION:
        conn.close()
        return None
    return conn


def run_apply(st: Any) -> dict[str, Any]:
    """The shared apply orchestration (ADR-0035 A4/A6, extracted per ADR-0040). Key-free, raw/-free.

    Composes the existing key-free executors — `apply_resolved_syntheses`,
    `apply_contradiction_decisions`, the scoped `apply_approved_deprecations`, and
    `promote_candidates(rebuild_index=False)` — then rebuilds `wiki/index.md` **once** and runs the full
    validator suite **once**. Non-transactional: effects are written before validation, so a validator
    failure is reported as `status: "validation_failed"` (never a roll back). Rooted entirely at the
    given settings `st` (no globals / `cwd`) so the dry-run can run it against a sandbox copy; the
    graph-availability gate lives here (raises `GraphUnavailable`) so both paths refuse identically.
    """
    root = st.root
    reviews_dir = st.reviews_dir
    wiki_dir = st.wiki_dir
    claims_dir = wiki_dir / "Claims"
    synthesis_dir = wiki_dir / "Synthesis"
    enrichment_dir = st.normalized_dir / "enrichment"
    markdown_dir = st.markdown_dir
    now = manifests.iso_now()

    syntheses = {"promoted": 0, "rejected": 0}
    contra: dict[str, Any] = {"resolution": {"acknowledged": 0, "rejected": 0,
                                             "superseded_executed": 0}, "changed_pages": []}
    deprec: dict[str, Any] = {"applied": 0, "normalized": 0, "skipped": [], "changed_pages": []}
    dups: dict[str, Any] = {"applied": 0, "normalized": 0, "skipped": [], "changed_pages": []}
    sem_hidden: dict[str, Any] = {"applied": 0, "normalized": 0, "skipped": [], "changed_pages": []}
    sem_unhidden: dict[str, Any] = {"applied": 0, "normalized": 0, "skipped": [], "changed_pages": []}
    claims_hidden: dict[str, Any] = {"applied": 0, "normalized": 0, "skipped": [], "changed_pages": []}
    claims_unhidden: dict[str, Any] = {"applied": 0, "normalized": 0, "skipped": [], "changed_pages": []}
    syn_hidden: dict[str, Any] = {"applied": 0, "normalized": 0, "skipped": [], "changed_pages": []}
    syn_unhidden: dict[str, Any] = {"applied": 0, "normalized": 0, "skipped": [], "changed_pages": []}
    claim_syn_pages: list[str] = []   # ADR-0049: synthesis pages re-rendered by a claim hide/unhide fan-out
    syn_evidence_suppressed = syn_evidence_restored = 0   # active <-> evidence_hidden transitions (audit)
    syn_fanout_unreconciled = 0   # affected syntheses the fan-out couldn't re-render (missing/unbindable)
    synthesis_fanout_work = 0     # affected syntheses the fan-out re-rendered/repaired (page or graph)
    merged: dict[str, Any] = {"applied": 0, "skipped": [], "changed_pages": [], "affected_sources": []}
    rekeyed: dict[str, Any] = {"applied": 0, "skipped": [], "changed_pages": [], "affected_sources": []}
    split_res: dict[str, Any] = {"applied": 0, "skipped": [], "changed_pages": [], "affected_sources": []}
    promo = {"promoted": 0}

    # Safe open: None on absent OR schema-mismatch (archive doesn't need the graph, so an unrelated graph
    # problem must not block an archive-only apply). The graph executors + promote run only when the graph
    # is available; 503 only when graph-*required* items are waiting (archive_source is not graph-required).
    gconn = _open_graph_safe_at(st.graph_db_path)
    graph_available = gconn is not None
    if gconn is not None:
        try:
            syntheses = synthesis.apply_resolved_syntheses(
                gconn, reviews_dir, synthesis_dir=synthesis_dir, enrichment_dir=enrichment_dir, now=now)
            contra = contradictions.apply_contradiction_decisions(
                gconn, reviews_dir, claims_dir=claims_dir, markdown_dir=markdown_dir, now=now)
            deprec = deprecations.apply_approved_deprecations(
                gconn, reviews_dir, wiki_dir=wiki_dir, claims_dir=claims_dir,
                markdown_dir=markdown_dir, now=now)
            # ADR-0046: governance semantic-page hide (active -> hidden) via the deprecation render
            # seam (recompose_semantic_node_page), graph-REQUIRED — so it runs inside the graph block.
            sem_hidden = deprecations.apply_hidden_semantic_pages(
                gconn, reviews_dir, wiki_dir=wiki_dir, now=now)
            # ADR-0047: the governed inverse — semantic unhide (hidden -> active), same render seam,
            # also graph-REQUIRED.
            sem_unhidden = deprecations.apply_unhidden_semantic_pages(
                gconn, reviews_dir, wiki_dir=wiki_dir, now=now)
            # ADR-0048: claim visibility (active <-> hidden) via recompose_claim + partner re-render;
            # graph-REQUIRED.
            claims_hidden = deprecations.apply_hidden_claims(
                gconn, reviews_dir, wiki_dir=wiki_dir, markdown_dir=markdown_dir, now=now)
            claims_unhidden = deprecations.apply_unhidden_claims(
                gconn, reviews_dir, wiki_dir=wiki_dir, markdown_dir=markdown_dir, now=now)
            # ADR-0049: explicit synthesis visibility (active <-> hidden) via the synthesis.py / _render_page
            # seam (artifact-sourced); graph-REQUIRED. Runs AFTER the claim status flips but BEFORE the claim
            # fan-out, so an operator hide_synthesis applies to a still-`active` synthesis and the final
            # fan-out then PRESERVES operator `hidden` (decision 10 precedence: operator hidden wins).
            syn_hidden = synthesis.apply_hidden_syntheses(
                gconn, reviews_dir, synthesis_dir=synthesis_dir, enrichment_dir=enrichment_dir, now=now)
            syn_unhidden = synthesis.apply_unhidden_syntheses(
                gconn, reviews_dir, synthesis_dir=synthesis_dir, enrichment_dir=enrichment_dir, now=now)
            # ADR-0049 fan-out (FINAL reconciliation): re-render every synthesis citing a hidden/unhidden
            # claim so its Supporting Evidence drops/restores that claim and its status reconciles to the
            # current evidence (active <-> evidence_hidden), while PRESERVING operator `hidden`. The edges
            # stay active in the graph (SoT); only the rendered discovery surface + status change.
            claim_affected_syn = sorted(set(claims_hidden.get("affected_syntheses", []))
                                        | set(claims_unhidden.get("affected_syntheses", [])))
            for _sid in claim_affected_syn:
                _before = (graph.get_node(gconn, _sid) or {}).get("status")
                _result = synthesis.rerender_synthesis_page(gconn, _sid, synthesis_dir=synthesis_dir,
                                                            enrichment_dir=enrichment_dir, now=now)
                if _result is None:
                    syn_fanout_unreconciled += 1   # page missing/unbindable/artifact gone -> can't suppress
                    continue
                _after, _changed = _result
                if _changed:                       # page OR graph-mirror changed -> reindex; no churn if not
                    claim_syn_pages.append(f"Synthesis/{_sid}.md")
                    synthesis_fanout_work += 1
                    # ADR-0049 decision 10: audit the active <-> evidence_hidden transitions in the summary.
                    if _after == "evidence_hidden" and _before != "evidence_hidden":
                        syn_evidence_suppressed += 1
                    elif _after == "active" and _before == "evidence_hidden":
                        syn_evidence_restored += 1
            dups = duplicates.apply_marked_duplicates(
                gconn, reviews_dir, wiki_dir=wiki_dir, now=now)
            # ADR-0050/0059: identity-surgery merge (knowledge items), forward-only; re-points active edges +
            # tombstones the absorbed id + unions aliases. GRAPH-REQUIRED. Source pages whose mentions were
            # re-pointed are re-rendered after the commit (affected_sources, like the claim fan-out).
            merged = merges.apply_merges(gconn, reviews_dir, wiki_dir=wiki_dir, now=now)
            # ADR-0059: governed classification flip (NON-rekeying) — page item_type + graph mirror; no
            # id change, no page move, no edge re-point. GRAPH-REQUIRED. Mentioning Source pages re-render
            # after the commit (affected_sources — their Items sections group by item_type).
            rekeyed = retypes.apply_retypes(gconn, reviews_dir, wiki_dir=wiki_dir, now=now)
            # ADR-0052/0059: identity-surgery item split (inverse of merge), forward-only; mints the spin-off +
            # re-points the human-partitioned mentions + re-renders the primary. GRAPH-REQUIRED. The moved
            # sources' Source pages re-render after the commit (affected_sources). Runs before promote_
            # candidates (below) so a spin-off with >=2 independent sources can promote in the same apply.
            split_res = splits.apply_splits(gconn, reviews_dir, wiki_dir=wiki_dir, now=now)
            gconn.commit()
        finally:
            gconn.close()
    elif _approved_graph_backed_count(reviews_dir):
        # The graph is unavailable (absent or schema-mismatched) but approved graph-required decisions
        # are waiting — refuse loudly rather than report a silent "applied" (and BEFORE promote_candidates
        # would init an empty graph). archive_source is NOT graph-required, so it doesn't trigger this.
        # The gate lives here so live apply (-> 503) and dry-run (-> blocked preview) refuse identically.
        raise GraphUnavailable()

    if graph_available:
        promo = promote.promote_candidates(root, rebuild_index=False, record_job=False)

    # Phase 7: reversible source archive (active -> archive_candidate; ADR-0036). Own graph conn +
    # per-source page re-render; raw bytes untouched. NOT graph-required.
    archive = retention.apply_archive_sources(
        root, manifests_dir=st.manifests_dir, reviews_dir=reviews_dir, wiki_dir=wiki_dir,
        graph_db=st.graph_db_path, now=now)
    # ADR-0043: governance source hide (active -> hidden). Same reversible status-transition machinery
    # as archive (manifest authority + page re-render + best-effort graph mirror); NOT graph-required.
    hidden = retention.apply_hidden_sources(
        root, manifests_dir=st.manifests_dir, reviews_dir=reviews_dir, wiki_dir=wiki_dir,
        graph_db=st.graph_db_path, now=now)
    # ADR-0047: governance source unhide (hidden -> active) — the inverse of hide_content, same shared
    # source-status machinery (manifest authority + page re-render + best-effort graph mirror); NOT
    # graph-required.
    unhidden = retention.apply_unhidden_sources(
        root, manifests_dir=st.manifests_dir, reviews_dir=reviews_dir, wiki_dir=wiki_dir,
        graph_db=st.graph_db_path, now=now)

    # ADR-0048: re-render the Source pages whose Claims section is affected by a claim hide/unhide so the
    # now-hidden claim drops (or a re-derived-active claim restores). Runs AFTER the graph block committed,
    # so generate_wiki (own read conn) sees the new claim node status; reads the hidden-aware projection.
    # (ADR-0050: a merge re-points mentions(Src->B) to ...(Src->A); those Source pages re-render here too.)
    claim_source_ids = sorted(set(claims_hidden.get("affected_sources", []))
                              | set(claims_unhidden.get("affected_sources", []))
                              | set(merged.get("affected_sources", []))
                              | set(rekeyed.get("affected_sources", []))
                              | set(split_res.get("affected_sources", [])))
    # Only re-render sources that actually have a Source page (nothing to suppress otherwise) — keeps a
    # minimal/degraded vault from failing the apply on a missing manifest/template.
    existing_sources = [sid for sid in claim_source_ids
                        if (wiki_dir / "Sources" / f"{sid}.md").exists()]
    claim_source_pages: list[str] = []
    if existing_sources and graph_available:
        wiki.generate_wiki(root, source_ids=existing_sources, rebuild_index=False, record_job=False)
        claim_source_pages = [f"Sources/{sid}.md" for sid in existing_sources]

    # pages_changed counts every page write: contradiction re-projections, deprecations, the synthesis
    # pages apply_resolved_syntheses re-rendered, the item pages promotion rewrote, archives.
    pages_changed = (len(contra["changed_pages"]) + len(deprec["changed_pages"])
                     + len(archive["changed_pages"]) + len(hidden["changed_pages"])
                     + len(unhidden["changed_pages"])
                     + len(sem_hidden["changed_pages"]) + len(sem_unhidden["changed_pages"])
                     + len(claims_hidden["changed_pages"]) + len(claims_unhidden["changed_pages"])
                     + len(syn_hidden["changed_pages"]) + len(syn_unhidden["changed_pages"])
                     + len(claim_syn_pages) + len(merged["changed_pages"]) + len(rekeyed["changed_pages"])
                     + len(split_res["changed_pages"])
                     + len(claim_source_pages) + len(dups["changed_pages"])
                     + syntheses["promoted"] + syntheses["rejected"] + promo["promoted"])
    # Reindex/index-refresh eligibility: page writes AND status transitions that may NOT re-render a page
    # but still affect retrieval — notably a semantic hide/unhide that only flips the graph node when the
    # page was already at the target status (ADR-0046/0047: applied/normalized with empty changed_pages).
    # Without this, reindex would be skipped and a stale nav index could keep surfacing a hidden page (or
    # keep hiding an unhidden one), with no warning. A dedicated trigger, not overloading pages_changed.
    semantic_hide_work = sem_hidden["applied"] + sem_hidden["normalized"]
    semantic_unhide_work = sem_unhidden["applied"] + sem_unhidden["normalized"]
    claim_hide_work = claims_hidden["applied"] + claims_hidden["normalized"]
    claim_unhide_work = claims_unhidden["applied"] + claims_unhidden["normalized"]
    synthesis_hide_work = syn_hidden["applied"] + syn_hidden["normalized"]
    synthesis_unhide_work = syn_unhidden["applied"] + syn_unhidden["normalized"]
    merge_work = merged["applied"]
    rekey_work = rekeyed["applied"]
    split_work = split_res["applied"]
    changed = bool(pages_changed or contra["resolution"]["acknowledged"]
                   or contra["resolution"]["rejected"] or contra["resolution"]["superseded_executed"]
                   or semantic_hide_work or semantic_unhide_work
                   or claim_hide_work or claim_unhide_work
                   or synthesis_hide_work or synthesis_unhide_work or merge_work or rekey_work
                   or split_work)

    warnings: list[str] = []
    index_status = _rebuild_index_status(root) if changed else "skipped"
    if index_status == "failed":  # script present but non-zero after real changes — surface it
        warnings.append("index_rebuild_failed")
    # Refresh the keyword/navigation index so page status changes (archive, deprecation) reach the
    # retrieval filter (an archived source must drop out of default retrieval). Caller-owned reindex.
    reindex_failed = False
    if changed:
        try:
            retention.reindex_keyword(root)
        except Exception:  # noqa: BLE001 - a reindex failure is a warning (except for hide, below)
            warnings.append("keyword_reindex_failed")
            reindex_failed = True

    # ADR-0043 (stricter than archive): a `hide_content` whose retrieval/nav index did NOT refresh is
    # NOT a clean apply — the manifest/page are hidden (authority), but the hidden source may still
    # surface via the stale keyword/navigation index until reindex succeeds. Surface it as non-clean so
    # an operator hiding sensitive content can't read "applied" and assume suppression took effect.
    hide_retrieval_stale = reindex_failed and hidden["applied"] > 0
    if hide_retrieval_stale:
        warnings.append("hide_retrieval_suppression_not_guaranteed")

    # ADR-0046 (mirrors source hide): a semantic-page hide that applied/normalized while the keyword/nav
    # index did NOT refresh is non-clean — page + graph node are hidden (authority), but a stale index
    # can still surface the page until reindex succeeds.
    semantic_hide_stale = reindex_failed and (sem_hidden["applied"] + sem_hidden["normalized"]) > 0
    if semantic_hide_stale:
        warnings.append("semantic_hide_retrieval_suppression_not_guaranteed")

    # ADR-0047 (inverse risk): an unhide (source or semantic) that applied while reindex failed is non-clean
    # too — the page/source IS active on disk (authority correct), but a STALE index can keep HIDING it from
    # default discovery until reindex succeeds, so the operator shouldn't read "applied" as "discoverable".
    unhide_discovery_stale = reindex_failed and (
        unhidden["applied"] + sem_unhidden["applied"] + sem_unhidden["normalized"]) > 0
    if unhide_discovery_stale:
        warnings.append("unhide_discovery_restoration_not_guaranteed")

    # ADR-0048: claim hide/unhide carry the same stale-index risk (claims are an answer-eligible discovery
    # surface), with claim-specific warnings.
    claim_hide_stale = reindex_failed and claim_hide_work > 0
    if claim_hide_stale:
        warnings.append("claim_hide_retrieval_suppression_not_guaranteed")
    claim_unhide_stale = reindex_failed and claim_unhide_work > 0
    if claim_unhide_stale:
        warnings.append("claim_unhide_discovery_restoration_not_guaranteed")

    # ADR-0049: synthesis hide/unhide carry the same stale-index risk (synthesis is an answer-eligible
    # discovery surface), with per-surface warnings — the operator knows which executor caused non-clean.
    synthesis_hide_stale = reindex_failed and synthesis_hide_work > 0
    if synthesis_hide_stale:
        warnings.append("synthesis_hide_discovery_suppression_not_guaranteed")
    synthesis_unhide_stale = reindex_failed and synthesis_unhide_work > 0
    if synthesis_unhide_stale:
        warnings.append("synthesis_unhide_discovery_restoration_not_guaranteed")

    # ADR-0049 decision 10: a claim hide whose affected synthesis suppression isn't guaranteed is NOT a clean
    # apply. Two causes: (a) the fan-out couldn't re-render the synthesis (page missing/unbindable, artifact
    # gone) -> it may still surface the hidden claim's content as ordinary discoverable evidence; (b) the
    # fan-out changed a synthesis (page or graph-mirror repair, e.g. active -> evidence_hidden) but the
    # keyword/nav reindex failed -> the stale index can keep the synthesis discoverable.
    synthesis_evidence_stale = (syn_fanout_unreconciled > 0
                                or (reindex_failed and synthesis_fanout_work > 0))
    if synthesis_evidence_stale:
        warnings.append("synthesis_evidence_suppression_not_guaranteed")

    # ADR-0050: a merge drops the absorbed id from discovery (status: merged) + re-points its backlinks; a
    # failed reindex leaves a stale nav index that can still surface the absorbed identity, so it's non-clean.
    merge_stale = reindex_failed and merge_work > 0
    if merge_stale:
        warnings.append("merge_discovery_reindex_not_guaranteed")

    # ADR-0059: a retype changes what navigation/grouping surfaces say about a node (item_type on the
    # nav row + the Source-page/index groupings); a failed reindex leaves a stale nav index that still
    # advertises the old classification, so it's non-clean.
    rekey_stale = reindex_failed and rekey_work > 0
    if rekey_stale:
        warnings.append("retype_discovery_reindex_not_guaranteed")

    # ADR-0052: a split re-points the primary's mentions + re-renders the moved sources' Source pages; a
    # failed reindex leaves a stale nav/keyword index that may still surface the pre-split projection.
    split_stale = reindex_failed and split_work > 0
    if split_stale:
        warnings.append("split_discovery_reindex_not_guaranteed")

    validators = _run_all_validators(root)
    failed = [v for v in validators if v["returncode"] != 0]
    validators_ok = not failed
    clean = (validators_ok and not hide_retrieval_stale and not semantic_hide_stale
             and not unhide_discovery_stale and not claim_hide_stale and not claim_unhide_stale
             and not synthesis_hide_stale and not synthesis_unhide_stale
             and not synthesis_evidence_stale and not merge_stale and not rekey_stale and not split_stale)

    return {
        "status": "applied" if clean else "validation_failed",
        "applied": True,
        "validators_ok": validators_ok,
        "failed_validators": failed,
        "warnings": warnings,
        "summary": {
            "syntheses": {"promoted": syntheses["promoted"], "rejected": syntheses["rejected"]},
            "promotions": {"promoted": promo["promoted"]},
            "contradictions": {
                "acknowledged": contra["resolution"]["acknowledged"],
                "rejected": contra["resolution"]["rejected"],
                "superseded": contra["resolution"]["superseded_executed"],
            },
            "deprecations": {"applied": deprec["applied"], "normalized": deprec["normalized"],
                             "skipped": deprec["skipped"]},
            "duplicates": {"applied": dups["applied"], "normalized": dups["normalized"],
                           "skipped": dups["skipped"]},
            "archives": {"applied": archive["applied"], "skipped": archive["skipped"]},
            "hidden": {"applied": hidden["applied"], "skipped": hidden["skipped"]},
            "unhidden": {"applied": unhidden["applied"], "skipped": unhidden["skipped"]},
            "semantic_hidden": {"applied": sem_hidden["applied"], "normalized": sem_hidden["normalized"],
                                "skipped": sem_hidden["skipped"]},
            "semantic_unhidden": {"applied": sem_unhidden["applied"],
                                  "normalized": sem_unhidden["normalized"],
                                  "skipped": sem_unhidden["skipped"]},
            "claims_hidden": {"applied": claims_hidden["applied"],
                              "normalized": claims_hidden["normalized"],
                              "skipped": claims_hidden["skipped"]},
            "claims_unhidden": {"applied": claims_unhidden["applied"],
                                "normalized": claims_unhidden["normalized"],
                                "skipped": claims_unhidden["skipped"]},
            "synthesis_hidden": {"applied": syn_hidden["applied"],
                                 "normalized": syn_hidden["normalized"],
                                 "skipped": syn_hidden["skipped"]},
            "synthesis_unhidden": {"applied": syn_unhidden["applied"],
                                   "normalized": syn_unhidden["normalized"],
                                   "skipped": syn_unhidden["skipped"]},
            "synthesis_evidence": {"suppressed": syn_evidence_suppressed,
                                   "restored": syn_evidence_restored,
                                   "unreconciled": syn_fanout_unreconciled},
            "merged": {"applied": merged["applied"], "skipped": merged["skipped"]},
            "retyped": {"applied": rekeyed["applied"], "skipped": rekeyed["skipped"]},
            "split": {"applied": split_res["applied"], "skipped": split_res["skipped"]},
            "pages_changed": pages_changed,
            "index_rebuilt": index_status == "rebuilt",
            "unapplied": _unapplied_by_type(reviews_dir),
        },
    }


@app.post("/reviews/apply", response_model=ReviewApplyResponse)
def apply_reviews() -> dict[str, Any]:
    """Apply approved review decisions on the live vault (ADR-0035). Thin wrapper over the shared
    `run_apply` orchestration; maps the in-orchestration graph gate to HTTP 503."""
    try:
        return run_apply(settings)
    except GraphUnavailable:
        raise HTTPException(
            status_code=503,
            detail="graph index unavailable; rebuild db/graph.sqlite before applying reviews")


def _approved_appliable_items(reviews_dir: Path) -> list[dict[str, Any]]:
    """Provenance scaffold (ADR-0040): each approved item's id/type/targets + appliable flag, read from
    `approved/` *before* run_apply moves files. `items[]` is provenance, not the authoritative diff."""
    out: list[dict[str, Any]] = []
    d = reviews_dir / "approved"
    for path in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(item, dict):
            continue
        subject = item.get("subject") if isinstance(item.get("subject"), dict) else {}
        targets = [v for k, v in subject.items() if k.endswith("_id") and isinstance(v, str)]
        out.append({"review_id": item.get("review_id", path.stem), "type": item.get("type"),
                    "targets": targets, "appliable": item.get("type") in _APPLY_TYPES})
    return out


def _attribute_effects(item: dict[str, Any], diff: dict[str, Any]) -> list[str]:
    """Best-effort domains touched by one review item (provenance only — never authoritative;
    the durable diff is the authority). Attributes by review_id (reviews/graph) and target id."""
    rid, targets = item["review_id"], set(item["targets"])
    g = diff["graph"]
    effects: set[str] = set()
    if any(m["review_id"] == rid for m in diff["reviews"]):
        effects.add("reviews")
    repointed = g.get("edges_repointed", [])
    if (any(e.get("review_id") == rid for e in (*g["edges_added"], *g["edges_status_changed"], *repointed))
            # ADR-0050 merge: a re-pointed edge keeps its ORIGINAL provenance (not the merge rid), and the
            # absorbed-node tombstone is a node-status change — attribute the graph effect by target id too.
            or any(e["from_src"] in targets or e["from_dst"] in targets for e in repointed)
            or any(n["id"] in targets for n in (*g["nodes_status_changed"], *g["nodes_added"]))):
        effects.add("graph")
    if any(m["source_id"] in targets for m in diff["manifests"]):
        effects.add("manifests")
    return sorted(effects)


@app.post("/reviews/apply/dry-run", response_model=ReviewDryRunResponse)
def dry_run_apply() -> dict[str, Any]:
    """Preview what `POST /reviews/apply` would change, with **no** live writes (ADR-0040).

    Builds a fully self-contained sandbox copy, runs the **same** `run_apply` against it, diffs
    sandbox-vs-live into a semantic mutation plan, and discards the sandbox. The graph gate inside
    `run_apply` makes this refuse on exactly the conditions live apply 503s on — surfaced here as a
    structured `blocked` preview. A sandbox executor failure is a structured `failed` preview, never a
    500; the UI offers Apply only when `status == "ok"`.
    """
    try:
        tmp_root, sandbox = apply_sandbox.build_sandbox(settings)
    except Exception as exc:  # noqa: BLE001 - inability to produce a preview, not a 500 (ADR-0040 #6)
        return {
            "status": "failed", "reason": "sandbox_build_error",
            "error": f"{type(exc).__name__}: {exc}", "diff": None, "items": [], "not_appliable": [],
            "validators": {"passed": False, "failures": []}, "warnings": ["sandbox_build_error"],
        }
    try:
        approved = _approved_appliable_items(sandbox.reviews_dir)
        not_appliable = [{"review_id": it["review_id"], "type": it["type"],
                          "reason": "no_executor_in_phase_6"}
                         for it in approved if not it["appliable"]]
        before = apply_sandbox.snapshot_state(sandbox)
        try:
            result = run_apply(sandbox)
        except GraphUnavailable:
            blocked = [{"review_id": it["review_id"], "type": it["type"], "reason": "graph_unavailable"}
                       for it in approved if it["appliable"] and it["type"] in _GRAPH_REQUIRED_TYPES]
            return {
                "status": "blocked", "reason": "graph_unavailable",
                "diff": {"graph": apply_sandbox.empty_graph_diff(), "wiki": [], "reviews": [],
                         "manifests": []},
                "items": [], "not_appliable": not_appliable + blocked,
                "validators": {"passed": False, "failures": []},
                "warnings": ["graph_unavailable"],
            }
        except Exception as exc:  # noqa: BLE001 - a sandbox failure is a failed preview, not a 500
            return {
                "status": "failed", "reason": "executor_error", "error": f"{type(exc).__name__}: {exc}",
                "diff": None, "items": [], "not_appliable": not_appliable,
                "validators": {"passed": False, "failures": []}, "warnings": ["executor_error"],
            }
        after = apply_sandbox.snapshot_state(sandbox)
        diff = apply_sandbox.diff_states(before, after)
        items = [{"review_id": it["review_id"], "type": it["type"], "targets": it["targets"],
                  "effects": _attribute_effects(it, diff)} for it in approved if it["appliable"]]
        # Overall cleanliness uses run_apply's status (validators AND the ADR-0043 hide-reindex-stale
        # gate), not just validators_ok — so a sandbox reindex failure on a hide previews as non-clean.
        clean = result["status"] == "applied"
        return {
            "status": "ok" if clean else "validation_failed",
            "diff": diff, "items": items, "not_appliable": not_appliable,
            "validators": {"passed": result["validators_ok"], "failures": result["failed_validators"]},
            "warnings": result["warnings"],
            "summary": result["summary"],
        }
    finally:
        apply_sandbox.cleanup_sandbox(tmp_root)


# --- Phase 7 maintenance passes (detect-and-propose; ADR-0036) -------------


@app.post("/jobs/lint", response_model=LintResponse)
def run_lint_job() -> dict[str, Any]:
    """Run the lint maintenance pass: structural health report + semantic checks that file governance
    review items (ADR-0036). Detect-and-propose: it never acts on semantic/destructive issues. Lint
    health is an outcome (`status: "failing"` ≠ HTTP error) — the pass always completes + records a job.
    """
    return lint.run_lint(
        settings.root, manifests_dir=settings.manifests_dir, graph_db=settings.graph_db_path,
        wiki_dir=settings.wiki_dir, reviews_dir=settings.reviews_dir,
        enrichment_dir=settings.normalized_dir / "enrichment", markdown_dir=settings.markdown_dir,
        summary_model_ref=settings.enrich_model_light, synthesis_model_ref=settings.enrich_model_heavy,
        jobs_db=settings.jobs_db_path)


@app.post("/jobs/stale-check", response_model=StaleCheckResponse)
def run_stale_check_job() -> dict[str, Any]:
    """Detect stale/ephemeral sources and propose archive/delete candidates (ADR-0036). Acts on nothing;
    the reversible archive is applied later via POST /reviews/apply."""
    return retention.run_stale_check(
        settings.root, manifests_dir=settings.manifests_dir, reviews_dir=settings.reviews_dir,
        wiki_dir=settings.wiki_dir, cache_db=settings.response_cache_path, jobs_db=settings.jobs_db_path)


@app.post("/jobs/reindex", response_model=ReindexResponse)
def run_reindex_job() -> dict[str, Any]:
    """Cheap deterministic reindex: rebuild wiki/index.md + refresh the keyword index (ADR-0036).
    Index + keyword only — never the vector index (explicit reindex_vector.py only)."""
    return retention.run_reindex(
        settings.root, jobs_db=settings.jobs_db_path, wiki_dir=settings.wiki_dir)


# --- Human Review UI (server-rendered HTML; ADR-0035 decision 1 + A8) -------
# The HTML layer is never authority: each /ui route calls the same read-model / _record_decision /
# apply_reviews primitives the JSON API uses, then renders via review_html (every value escaped).
# `/ui/reviews/apply` is declared before `/ui/reviews/{review_id}` so "apply" is not read as an id.
# Mutating routes are POST-only under the loopback-only assert_safe_bind; CSRF stays deferred while
# loopback-only — any move to a LAN/public bind or added auth MUST revisit form safety first.

_UI_DECISIONS = {"approve": "approved", "reject": "rejected", "defer": "deferred"}


def _html_error(exc: HTTPException) -> HTMLResponse:
    return HTMLResponse(review_html.render_error(exc.status_code, str(exc.detail)),
                        status_code=exc.status_code)


@app.get("/ui/reviews", response_class=HTMLResponse)
def ui_review_queue(
    status: str = "pending",
    type: str | None = None,  # noqa: A002 - the public ?type= filter
    priority: str | None = None,
    limit: int | None = Query(None, ge=1),
    offset: int = Query(0, ge=0),
) -> HTMLResponse:
    try:
        data = review_read.list_reviews(
            settings.reviews_dir, status=status, type=type, priority=priority,
            limit=limit, offset=offset)
    except ValueError as exc:
        return HTMLResponse(review_html.render_error(400, str(exc)), status_code=400)
    return HTMLResponse(review_html.render_queue(data, status=status))


@app.get("/ui/reviews/apply", response_class=HTMLResponse)
def ui_apply_confirm() -> HTMLResponse:
    """Two-step apply, step 1: the dry-run mutation preview (ADR-0040). Renders the semantic diff and
    offers Apply only when the preview is clean (`status == "ok"`)."""
    scope = review_read.apply_scope_counts(settings.reviews_dir)
    dry = dry_run_apply()
    return HTMLResponse(review_html.render_apply_dry_run(scope, dry))


@app.post("/ui/reviews/apply", response_class=HTMLResponse)
def ui_apply() -> HTMLResponse:
    """Two-step apply, step 2: execute via the same logic as POST /reviews/apply, render the summary."""
    try:
        result = apply_reviews()
    except HTTPException as exc:
        return _html_error(exc)
    return HTMLResponse(review_html.render_apply_result(result))


# --- per-source review flow (ADR-0058) — declared before /ui/reviews/{review_id} so
# "sources" is never read as a review id. A high-volume LENS over extraction-caused items;
# the flat queue above stays canonical. Graph-REQUIRED (attribution = mentions edges).

_SOURCE_RID_RE = re.compile(r"^rev_[0-9a-f]{16}$")


def _source_flow_unavailable() -> HTMLResponse:
    return HTMLResponse(review_html.render_error(
        503, "graph unavailable — the per-source flow needs the graph; use the flat queue"),
        status_code=503)


@app.get("/ui/reviews/sources", response_class=HTMLResponse)
def ui_review_sources() -> HTMLResponse:
    """The source index: every reviewable source in manifest ingest order with counts."""
    data = review_read.source_review_index(
        settings.reviews_dir, graph_db=settings.graph_db_path,
        wiki_dir=settings.wiki_dir, manifests_dir=settings.manifests_dir)
    if not data["graph_available"]:
        return _source_flow_unavailable()
    return HTMLResponse(review_html.render_sources_index(data))


@app.get("/ui/reviews/sources/{source_id}", response_class=HTMLResponse)
def ui_review_source_screen(source_id: str, preselect: str | None = None) -> HTMLResponse:
    """One source's screen: its candidates + type changes + retired section, one batch form.

    `?preselect=approve` (the only honored value; anything else is ignored) re-renders the
    SAME form with approve pre-checked on every PENDING row — the explicit two-click bulk
    approve (UAT round): one click of intent, one click of commit. Deferred rows were parked
    deliberately and stay unchecked; recording still runs the per-item primitives + scope
    guard unchanged."""
    if not manifests.is_source_id(source_id):
        return HTMLResponse(
            review_html.render_error(404, f"unknown source: {source_id}"), status_code=404)
    data = review_read.source_review_view(
        settings.reviews_dir, source_id, graph_db=settings.graph_db_path,
        wiki_dir=settings.wiki_dir, manifests_dir=settings.manifests_dir)
    if data is None:
        return HTMLResponse(
            review_html.render_error(404, f"unknown source: {source_id}"), status_code=404)
    if not data["graph_available"]:
        return _source_flow_unavailable()
    data["preselect"] = "approve" if preselect == "approve" else None
    return HTMLResponse(review_html.render_source_screen(data))


# MIME types a browser can render inline WITHOUT executing active content. HTML, SVG, and
# XML are deliberately excluded: raw sources are UNTRUSTED (CLAUDE.md rule 2), and inline
# same-origin HTML/SVG would let a hostile document script against the unauthenticated
# loopback API (review round: blocking). Everything off-list downloads as an attachment.
_RAW_INLINE_TYPES = frozenset({
    "application/pdf", "text/plain",
    "image/png", "image/jpeg", "image/gif", "image/webp",
})


@app.get("/raw/{source_id}")
def get_raw_original(source_id: str) -> FileResponse:
    """Serve a source's ORIGINAL raw bytes for operator review (UAT round).

    Read-only view seam for the review flow ("view original"). Trust posture:
    - The source resolves through `valid_manifests` — the SAME quarantine the rest of the
      system uses (canonical id, filename↔id match, duplicate rejection); a quarantined
      manifest is a plain 404.
    - The path comes ONLY from the manifest's `relative_raw_path`, containment-checked under
      `raw/` (`safe_under`, ADR-0009) — a tampered/escaping path is a 404, never a traversal.
    - Only passive media renders inline (`_RAW_INLINE_TYPES`; markdown re-served as
      text/plain); anything else — HTML/SVG/XML/unknown — is `attachment`. Every response is
      `nosniff`, and inline responses carry `Content-Security-Policy: sandbox` (opaque
      origin, no scripts) as defense in depth.
    - Deliberately STATUS-AGNOSTIC (review round): hide/archive govern default retrieval +
      navigation, and this is a loopback-only governance surface — a human deciding about a
      hidden/archived source must be able to inspect it. A physically absent file is a 404.
    """
    valid, _quarantined = manifests.valid_manifests(settings.manifests_dir)
    manifest = next((m for m in valid if m.get("source_id") == source_id), None)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"unknown source: {source_id}")
    rel = manifest.get("relative_raw_path")
    if not isinstance(rel, str) or not rel:
        raise HTTPException(status_code=404, detail="source has no catalogued raw path")
    resolved = safe_under(settings.root, settings.root / "raw", rel)
    if resolved is None or not resolved.is_file():
        raise HTTPException(status_code=404, detail="raw file not found")
    filename = manifest.get("original_filename") or resolved.name
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if media_type == "text/markdown":
        media_type = "text/plain"       # browsers don't render markdown; show it as text
    headers = {"X-Content-Type-Options": "nosniff"}
    if media_type in _RAW_INLINE_TYPES:
        headers["Content-Security-Policy"] = "sandbox"
        disposition = "inline"
    else:
        disposition = "attachment"
    return FileResponse(resolved, media_type=media_type, filename=filename,
                        content_disposition_type=disposition, headers=headers)


@app.post("/ui/reviews/sources/{source_id}/decide", response_class=HTMLResponse)
async def ui_review_source_decide(source_id: str, request: Request) -> HTMLResponse:
    """Batch decide (ADR-0058): one submit loops the EXISTING single-item primitives — no new
    ledger primitive. Untouched rows stay pending; an already-decided/invalid row skips with a
    per-item reason (never a 409 abort of the batch); results render per item.

    Scope guard (review round, B1): the source view is recomputed SERVER-side and only row ids
    actually visible on this source's screen are decidable here — a forged form naming a
    global/cross-source item skips with `not_attributable_to_source` before any ledger call
    (the per-source lens can never launder a flat-queue decision). Decided visible rows stay
    permitted so a stale-form race resolves honestly via `_record_decision` (idempotent no-op
    or 409 skip), not a misleading attribution error."""
    if not manifests.is_source_id(source_id):
        return HTMLResponse(
            review_html.render_error(404, f"unknown source: {source_id}"), status_code=404)
    view = review_read.source_review_view(
        settings.reviews_dir, source_id, graph_db=settings.graph_db_path,
        wiki_dir=settings.wiki_dir, manifests_dir=settings.manifests_dir)
    if view is None:
        return HTMLResponse(
            review_html.render_error(404, f"unknown source: {source_id}"), status_code=404)
    if not view["graph_available"]:
        return _source_flow_unavailable()
    visible = {str(r["review_id"])
               for r in (view["candidates"] + view["retype_items"] + view["retired"])}
    form = await request.form()
    note = str(form.get("note") or "").strip() or f"per-source flow: {source_id}"
    results: list[dict[str, Any]] = []
    for key in sorted(k for k in form.keys() if str(k).startswith("decision_")):
        rid = str(key)[len("decision_"):]
        action = str(form.get(key) or "")
        if not action:
            continue  # untouched = stays pending (ADR-0058 decision: no default decision)
        if not _SOURCE_RID_RE.fullmatch(rid):
            results.append({"review_id": rid, "action": action, "recorded": False,
                            "skip_reason": "invalid review id"})
            continue
        if rid not in visible:
            results.append({"review_id": rid, "action": action, "recorded": False,
                            "skip_reason": "not_attributable_to_source"})
            continue
        decision = _UI_DECISIONS.get(action)
        if decision is None:
            results.append({"review_id": rid, "action": action, "recorded": False,
                            "skip_reason": f"unknown action: {action}"})
            continue
        amendments: dict[str, Any] = {}
        title = str(form.get(f"amend_title_{rid}") or "").strip()
        aliases = str(form.get(f"amend_aliases_{rid}") or "").strip()
        description = str(form.get(f"amend_description_{rid}") or "").strip()
        item_type = str(form.get(f"amend_item_type_{rid}") or "").strip()
        if title:
            amendments["title"] = title
        if aliases:
            amendments["aliases"] = [a.strip() for a in aliases.split(",") if a.strip()]
        if description:
            amendments["description"] = description
        if item_type:
            amendments["item_type"] = item_type
        # Amendments ride approvals (frozen) and defers (draft) only; a reject discards them.
        send = amendments if (amendments and decision in ("approved", "deferred")) else None
        try:
            out = _record_decision(rid, decision,
                                   ReviewDecisionRequest(note=note, amendments=send))
            results.append({"review_id": rid, "action": action, "recorded": out["decision_recorded"],
                            "status": out["status"], "amended": bool(send)})
        except HTTPException as exc:
            results.append({"review_id": rid, "action": action, "recorded": False,
                            "skip_reason": f"{exc.status_code}: {exc.detail}"})
    return HTMLResponse(review_html.render_source_decide_result(source_id, results))


@app.post("/ui/reviews/sources/{source_id}/add", response_model=None)
def ui_review_source_add(
    source_id: str, title: str = Form(""), item_type: str = Form(""),
    aliases: str = Form(""), description: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    """Human-add (ADR-0058): a PRODUCER-side act — candidate node + anchorless human mention +
    page + a promote item recorded approved in one operation (the add IS the approval; the
    normal executor promotes at apply). Blocked outcomes write nothing."""
    if not manifests.is_source_id(source_id):
        return HTMLResponse(
            review_html.render_error(404, f"unknown source: {source_id}"), status_code=404)
    gconn = _open_graph_safe()
    if gconn is None:
        return _source_flow_unavailable()
    try:
        result = human_add.add_candidate(
            gconn, root=settings.root, source_id=source_id, item_type=item_type.strip(),
            title=title, aliases=[a.strip() for a in aliases.split(",") if a.strip()],
            description=description, wiki_dir=settings.wiki_dir,
            reviews_dir=settings.reviews_dir)
    finally:
        gconn.close()
    if result["outcome"] == "blocked":
        reason = result["reason"]
        if reason == "promotion_previously_rejected":
            message = (f"this candidate's promotion was rejected by "
                       f"{result.get('decided_by')} at {result.get('decided_at')} "
                       f"(review {result.get('review_id')}). A rejection is a human governance "
                       "record — reopen it explicitly from the item detail (ADR-0045) if you've "
                       "changed your mind; it is never silently reused.")
            return HTMLResponse(review_html.render_error(409, message), status_code=409)
        status = 400 if reason.startswith("invalid") else \
            404 if reason == "unknown_source" else 409
        return HTMLResponse(
            review_html.render_error(status, f"add blocked: {reason}"), status_code=status)
    return RedirectResponse(f"/ui/reviews/sources/{source_id}", status_code=303)


@app.get("/ui/reviews/{review_id}", response_class=HTMLResponse)
def ui_review_detail(review_id: str) -> HTMLResponse:
    result = review_read.get_review(
        settings.reviews_dir, review_id, graph_db=settings.graph_db_path,
        wiki_dir=settings.wiki_dir, manifests_dir=settings.manifests_dir)
    if result is None or result.get("parse_error") or result.get("schema_error"):
        return HTMLResponse(
            review_html.render_error(404, f"review not found: {review_id}"), status_code=404)
    return HTMLResponse(review_html.render_detail(result, review_id=review_id))


@app.post("/ui/reviews/{review_id}/decide", response_model=None)
def ui_review_decide(
    review_id: str, action: str = Form(...), note: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    """Record a decision from the detail form, then PRG-redirect to the item detail (ADR-0035 A8).

    ADR-0044: a resolve_contradiction's `acknowledge`/`supersede_a`/`supersede_b` actions translate to an
    approve (+ the winning claim_id for supersede); the handler reads the item to resolve A/B -> claim_id.
    """
    winner: str | None = None
    if action in ("supersede_a", "supersede_b"):
        item, _err = review_read.find_review(settings.reviews_dir, review_id)
        if item is None:  # route consistency: a missing review is 404, like the other decision paths
            return HTMLResponse(
                review_html.render_error(404, f"review not found: {review_id}"), status_code=404)
        subj = item.get("subject") or {}
        winner = subj.get("claim_a") if action == "supersede_a" else subj.get("claim_b")
        if winner is None:
            return HTMLResponse(
                review_html.render_error(400, "no claim to supersede"), status_code=400)
        decision = "approved"
    else:
        decision = _UI_DECISIONS.get("approve" if action == "acknowledge" else action)
        if decision is None:
            return HTMLResponse(
                review_html.render_error(400, f"unknown action: {action}"), status_code=400)
    try:
        _record_decision(review_id, decision, ReviewDecisionRequest(note=note, winner=winner))
    except HTTPException as exc:
        return _html_error(exc)
    return RedirectResponse(f"/ui/reviews/{review_id}", status_code=303)


@app.post("/ui/reviews/{review_id}/reopen", response_model=None)
def ui_review_reopen(
    review_id: str, reason: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    """Reopen a terminal item from the detail form, then PRG-redirect to the item detail (ADR-0045)."""
    try:
        _reopen_decision(review_id, reason)
    except HTTPException as exc:
        return _html_error(exc)
    return RedirectResponse(f"/ui/reviews/{review_id}", status_code=303)


def _open_graph_safe() -> Any:
    """Open the graph for /search, or ``None`` if absent or schema-mismatched.

    Unlike :func:`_open_graph`, a missing or wrong-schema graph is *not* fatal here — /search is a
    multi-channel surface, so a degraded graph just yields an empty graph group, never a 5xx.
    """
    if not settings.graph_db_path.exists():
        return None
    conn = graph.connect(settings.graph_db_path)
    if graph.schema_version(conn) != graph.SCHEMA_VERSION:
        conn.close()
        return None
    return conn


def _vector_capability(
    q: str, policy: Any, source_id: str | None
) -> tuple[search.VectorSearchFn | None, str | None, bool]:
    """Decide whether the vector channel can serve, returning ``(searcher, reason, note_worthy)``:
    ``(searcher, None, _)`` when ready, else ``(None, reason, note_worthy)``. The query is embedded
    **lazily** inside the searcher, so the cost is paid only if the channel actually runs.

    ``note_worthy`` is ``True`` only when an embedder **is configured** but vector still can't serve —
    i.e. a genuine *degradation* (mode=auto surfaces it as a note). A keyword-only deployment (no
    embedder / extra not installed) is **not** a degradation, so auto stays quietly keyword-only.
    The full ``reason`` is always returned for the explicit ``mode=vector`` 503 message.

    Strict serving checks (ADR-0033 decision 4): the optional dependency, the embedder config, and a
    missing/incoherent/**stale** index all make vector unavailable — explicit vector never serves a
    stale citation or a silent empty."""
    if not vector_index.lancedb_available():
        return None, "the 'vector' extra (LanceDB) is not installed", False
    try:
        embedder = embeddings.client_from_settings(settings)
    except embeddings.EmbeddingError as exc:
        return None, f"embedding config error: {exc}", True
    if embedder is None:
        return None, ("no embedder configured (set EMBEDDING_BASE_URL + EMBEDDING_MODEL_REF, or "
                      "EMBEDDING_PROVIDER=flagembedding_bge_m3)"), False
    expected = vector_index.VectorMeta(
        embedding_model_ref=embeddings.resolve_model_ref(settings),
        embedding_code_version=vector_index.EMBED_CODE_VERSION,
        distance_metric=settings.embedding_distance_metric,
        dimension=settings.embedding_dimension,
        index_schema_version=vector_index.INDEX_SCHEMA_VERSION,
    )
    st = vector_index.status(settings.root, expected=expected)
    if not st.present:
        return None, "no vector index built (run scripts/reindex_vector.py)", True
    if not st.coherent:
        return None, "index stale/incoherent (" + "; ".join(st.issues) + "); rerun reindex_vector.py --force", True
    if st.stale_or_missing_chunks or st.removed_chunks:
        return None, (f"index is stale ({st.stale_or_missing_chunks} chunk(s) changed/missing, "
                      f"{st.removed_chunks} removed); rerun scripts/reindex_vector.py"), True

    qtext = (q or "")[:policy.cap("max_query_chars")]
    metric = settings.embedding_distance_metric
    cache: dict[str, list[float]] = {}

    def searcher(*, limit: int) -> list[dict[str, Any]]:
        if not qtext.strip():
            return []
        try:
            if "vec" not in cache:
                cache["vec"] = embedder.embed([qtext])[0]  # lazy: embed only when vector actually runs
            return vector_index.search(settings.root, cache["vec"], limit=limit, metric=metric,
                                       source_id=source_id)
        except Exception as exc:  # backend failure (embed/index) -> narrow, typed unavailability
            raise search.VectorUnavailable(str(exc)) from exc

    return searcher, None, False


def _parse_item_type_facet(values: list[str] | None) -> frozenset[str] | None:
    """Validate an item_type facet (ADR-0062): each value must be one of the 15 production types.
    Unknown values AND the unclassified_review_required sentinel are rejected (400) — the facet is
    never silently ignored. Empty/absent → None (no faceting)."""
    if not values:
        return None
    bad = [v for v in values if not taxonomy.is_production_item_type(v)]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=(f"unknown item_type {bad!r}; allowed: {sorted(taxonomy.ITEM_TYPES)} "
                    "(the unclassified_review_required sentinel is not a facet value)"),
        )
    return frozenset(values)


@app.get("/search", response_model=SearchResponse)
def run_search_endpoint(
    q: str,
    mode: str = "auto",
    source_id: str | None = None,
    page_type: str | None = None,
    node_type: str | None = None,
    item_type: list[str] | None = Query(None),
    language: str | None = None,
    source_status: str | None = None,
    node_status: str | None = None,
    edge_status: str | None = None,
    evidence_limit: int | None = Query(None, ge=1, le=200),
    navigation_limit: int | None = Query(None, ge=1, le=200),
    graph_limit: int | None = Query(None, ge=1, le=200),
) -> dict[str, Any]:
    if mode not in search.VALID_MODES:
        raise HTTPException(status_code=400, detail=f"unknown mode {mode!r}; allowed: {sorted(search.VALID_MODES)}")
    for label, value in (("page_type", page_type), ("node_type", node_type)):
        if value is not None and value not in graph.NODE_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"unknown {label} {value!r}; allowed: {sorted(graph.NODE_TYPES)}",
            )
    item_types = _parse_item_type_facet(item_type)
    if language is not None and language not in {"en", "es", "unknown"}:
        raise HTTPException(
            status_code=400,
            detail=f"unknown language {language!r}; allowed: ['en', 'es', 'unknown']",
        )
    try:
        source_statuses = search.parse_statuses(source_status, graph.NODE_STATUSES, search.RETENTION_DEFAULT_STATUSES)
        node_statuses = search.parse_statuses(node_status, graph.NODE_STATUSES, search.RETENTION_DEFAULT_STATUSES)
        edge_statuses = search.parse_statuses(edge_status, graph.EDGE_STATUSES, graph_read.DEFAULT_EDGE_STATUSES)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _run_search(
        q, mode=mode, source_id=source_id, page_type=page_type, node_type=node_type,
        item_types=item_types, language=language,
        source_statuses=source_statuses, node_statuses=node_statuses, edge_statuses=edge_statuses,
        evidence_limit=evidence_limit, navigation_limit=navigation_limit, graph_limit=graph_limit,
    )


def _run_search(
    q: str, *, mode: str, source_id: str | None, page_type: str | None, node_type: str | None,
    item_types: frozenset[str] | None = None,
    language: str | None, source_statuses, node_statuses, edge_statuses,
    evidence_limit: int | None = None, navigation_limit: int | None = None,
    graph_limit: int | None = None,
) -> dict[str, Any]:
    """Run the Phase 4 retrieval stack and return the grouped result. Shared by GET /search and
    POST /query so the channel wiring (policy, retention, vector capability, graceful degradation)
    can't drift between the two surfaces."""
    policy = load_retrieval_policy(settings.retrieval_policy_path)
    keyword_conn = (
        keyword_index.connect(settings.keyword_index_path)
        if settings.keyword_index_path.exists() else None
    )
    # ADR-0062 review round 1 (Blocking 3): the served /search opens whatever index exists, so an
    # index built before a schema bump (e.g. a v1 navigation table with no item_type column) would
    # crash a query. Gate on the cheap structural usability check: if stale/mismatched, treat the
    # keyword+navigation channels as UNAVAILABLE (degrade + reindex-required note) rather than 500.
    # Full fingerprint freshness stays offline in validate_index_consistency.
    stale_index_note: str | None = None
    if keyword_conn is not None and not keyword_index.schema_usable(keyword_conn):
        keyword_conn.close()
        keyword_conn = None
        stale_index_note = ("keyword/navigation index schema is stale — run "
                            "scripts/reindex_keyword.py (keyword/navigation/vector degraded)")
    graph_conn = _open_graph_safe()
    try:
        # Build the vector capability for explicit mode=vector and mode=auto (it may blend vector).
        # Serving vector evidence needs the navigation index to verify source-status retention.
        vector_search: search.VectorSearchFn | None = None
        vector_reason: str | None = None  # the auto degradation note (None = degrade silently)
        # Only inspect vector state for requests that could actually run vector — graph-only auto
        # shapes (discovery/relationship/disagreement) skip the capability/index-status check entirely.
        if search.may_use_vector(mode, q, policy):
            if keyword_conn is None:
                reason = ("the keyword/navigation index is required for source-status retention; "
                          "run scripts/reindex_keyword.py")
                note_worthy = False  # missing keyword index is a degenerate state, not a vector degradation
            else:
                vector_search, reason, note_worthy = _vector_capability(q, policy, source_id)
            if mode == "vector" and vector_search is None:
                raise HTTPException(status_code=503, detail=f"vector search unavailable: {reason}")
            if mode == "auto" and vector_search is None and note_worthy:
                vector_reason = reason

        result = search.run_search(
            q=q, mode=mode, keyword_conn=keyword_conn, graph_conn=graph_conn, policy=policy,
            source_id=source_id, page_type=page_type, node_type=node_type, item_types=item_types,
            language=language,
            source_statuses=source_statuses, node_statuses=node_statuses, edge_statuses=edge_statuses,
            evidence_limit=evidence_limit, navigation_limit=navigation_limit, graph_limit=graph_limit,
            vector_search=vector_search, vector_unavailable_reason=vector_reason,
        )
        if stale_index_note is not None:
            result["notes"].append(stale_index_note)
    except search.VectorChannelError as exc:  # explicit mode=vector failed at query time
        raise HTTPException(status_code=503, detail=f"vector search unavailable: {exc}") from exc
    finally:
        if keyword_conn is not None:
            keyword_conn.close()
        if graph_conn is not None:
            graph_conn.close()
    return result


def _query_client():
    """Build the LLM client for query synthesis (ADR-0025). Indirected so tests can inject a fake."""
    return build_client(settings, cache=ResponseCache(settings.response_cache_path))


def _append_wiki_log(message: str) -> None:
    """Append a one-line audit entry to wiki/log.md (the wiki-write log; CLAUDE.md ingest workflow).
    Deterministic — no wall-clock — so it doesn't perturb byte-stable artifacts."""
    log = settings.wiki_dir / "log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"- {message}\n")


def _no_source_text() -> str:
    """The abstention text, sourced from policies/citation.yaml so it can't drift (ADR-0034)."""
    path = settings.root / "policies" / "citation.yaml"
    if path.exists():
        loaded = load_yaml(path.read_text(encoding="utf-8"))
        text = loaded.get("when_no_source_found")
        if isinstance(text, str) and text.strip():
            return text
    return query.NO_SOURCE_FOUND


# /query answers from citable chunk evidence; graph/navigation are discovery surfaces that can't cite.
_QUERY_MODES = {"auto", "keyword", "vector"}


@app.post("/query", response_model=QueryResponse)
def run_query_endpoint(req: QueryRequest) -> dict[str, Any]:
    """Synthesize a cited answer over retrieved Phase 4 chunk evidence (ADR-0034). The first
    key-requiring surface: an unconfigured/failed model maps to a controlled 503 (no detail leakage),
    while GET /search stays key-free. ``include_unsourced`` is a local/debug affordance only."""
    q = req.question
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="question must not be empty")
    if req.mode not in _QUERY_MODES:
        raise HTTPException(
            status_code=400,
            detail=(f"unsupported mode {req.mode!r} for /query; allowed: {sorted(_QUERY_MODES)} "
                    "(graph/navigation are discovery surfaces, not answer-citation sources)"),
        )
    if req.language is not None and req.language not in {"en", "es", "unknown"}:
        raise HTTPException(status_code=400, detail=f"unknown language {req.language!r}; allowed: ['en', 'es', 'unknown']")
    try:
        source_statuses = search.parse_statuses(req.source_status, graph.NODE_STATUSES, search.RETENTION_DEFAULT_STATUSES)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    item_types = _parse_item_type_facet(req.item_type)

    try:
        client = _query_client()
        if not client.provider_available(settings.query_model):
            raise HTTPException(
                status_code=503,
                detail=("query answering requires a configured LLM; set QUERY_MODEL and the provider "
                        "credential (the deterministic GET /search stays available without one)"),
            )
        # Retrieve citable chunk evidence (graph/navigation are not answer-citation sources, ADR-0034).
        result = _run_search(
            q, mode=req.mode, source_id=req.source_id, page_type=None, node_type=None,
            item_types=item_types, language=req.language,
            source_statuses=source_statuses,
            node_statuses=search.parse_statuses(None, graph.NODE_STATUSES, search.RETENTION_DEFAULT_STATUSES),
            edge_statuses=graph_read.DEFAULT_EDGE_STATUSES,
        )
        answer = query.answer_query(
            question=q, evidence_hits=result["evidence"], client=client,
            model_ref=settings.query_model, markdown_dir=settings.markdown_dir,
            fallback_text=_no_source_text(),
        )
    except HTTPException:
        raise  # already-controlled 4xx/503 (e.g. provider unavailable, explicit-vector unavailable)
    except ConfigError as exc:
        logger.warning("POST /query: LLM misconfigured: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="query answering is misconfigured (check QUERY_MODEL and the provider credential)",
        ) from exc
    except (ParseError, AdapterError) as exc:  # parse wraps adapter errors; catch adapter too as belt-and-suspenders
        logger.warning("POST /query: synthesis failed: %s", exc)
        raise HTTPException(status_code=503, detail="query answering is temporarily unavailable") from exc

    saved_id: str | None = None
    navigation_stale = False
    if req.save:  # explicit save -> deterministic wiki/Queries/<id>.md (no graph edges, no review)
        saved_id = query.query_id(q, mode=req.mode, source_id=req.source_id,
                                  source_status=req.source_status, language=req.language,
                                  item_type=req.item_type)
        # ADR-0060: page-local display labels for the citation-table source links.
        link_labels = labels.display_labels(
            settings.wiki_dir, [f"Sources/{c['source_id']}" for c in answer.citations])
        page = render_query_page({
            "query_id": saved_id, "question": q, "answer": answer.answer,
            "citations": answer.citations, "retrieval_modes": result["retrieval_path"],
            "item_type": req.item_type,  # ADR-0062: record the facet in the saved page
            "unsourced_claims": answer.unsourced_claims,
            "security_rejected_count": answer.security_rejected_count,
        }, labels=link_labels)
        qpath = settings.wiki_dir / "Queries" / f"{saved_id}.md"
        qpath.parent.mkdir(parents=True, exist_ok=True)
        qpath.write_text(page, encoding="utf-8")
        # Audit only (ADR-0034 Q3): the nav/index are NOT synchronously rebuilt — the saved query is
        # discoverable after the next reindex. Surfaced via navigation_stale so the API stays honest.
        _append_wiki_log(f"query saved: Queries/{saved_id}.md")
        navigation_stale = True

    return {
        "query": answer.question,
        "mode": req.mode,
        "retrieval_path": result["retrieval_path"],
        "answer": answer.answer,
        "abstained": answer.abstained,
        "claims": answer.claims,
        "citations": answer.citations,
        "evidence_count": answer.evidence_count,
        "unsourced_count": len(answer.unsourced_claims),
        "security_rejected_count": answer.security_rejected_count,
        # Q2: full ungrounded text only under explicit local/debug review; default exposes counts only.
        "unsourced_claims": answer.unsourced_claims if req.include_unsourced else [],
        "notes": result.get("notes", []),
        "query_id": saved_id,
        "navigation_stale": navigation_stale,
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


# --- Real-vault answer-quality eval (ADR-0042) ------------------------------
# Read-only over vault SoT (raw/normalized/wiki/reviews/manifests/graph); it scores POST /query's
# cited answers deterministically. It MAY write its own eval artifacts + populate the LLM cache on a
# miss; it always queries with save:false. Loopback-only + key-required (503) for the run; GET stays
# key-free. Same no-auth posture as every other route here (ADR-0009): unsafe on a non-loopback bind.


class _CountingCache(ResponseCache):
    """A ResponseCache that counts get hits/misses so the eval can record real cache_hits/cache_misses
    (ADR-0042 decision 4). A `get` returning a row = a replay (hit); None = a miss (the client then
    generates + `put`s). Behaviour is otherwise identical to ResponseCache. Note: counting is at the raw
    `get`, so a (rare) corrupt cached row the client later rejects in schema validation still counts as a
    hit — acceptable for v1; tighten to post-validation counting only if the metric must mean
    'provider replay provably avoided'."""

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> dict[str, Any] | None:
        row = super().get(key)
        if row is None:
            self.misses += 1
        else:
            self.hits += 1
        return row


def _eval_query_fn(client: Any, cache: _CountingCache | None):
    """A per-case /query runner for the eval (ADR-0042): reuses the shared retrieval + answer building
    blocks (so it can't drift from the operator path) with an INJECTED client, save:false, default
    retention visibility. Returns scoreable signals ONLY (ids/flags/counts) — never prose/prompt/evidence.
    When a counting cache is present, reports per-case `cache_hit` (None when the case made no LLM call,
    e.g. an abstention with no evidence)."""
    statuses = search.parse_statuses(None, graph.NODE_STATUSES, search.RETENTION_DEFAULT_STATUSES)

    def run(case: Any) -> dict[str, Any]:
        h0, m0 = (cache.hits, cache.misses) if cache is not None else (0, 0)
        result = _run_search(
            case.question, mode=case.mode, source_id=None, page_type=None, node_type=None, language=None,
            source_statuses=statuses, node_statuses=statuses,
            edge_statuses=graph_read.DEFAULT_EDGE_STATUSES)
        ans = query.answer_query(
            question=case.question, evidence_hits=result["evidence"], client=client,
            model_ref=settings.query_model, markdown_dir=settings.markdown_dir,
            fallback_text=_no_source_text())
        cache_hit: bool | None = None
        if cache is not None:
            if cache.hits > h0:
                cache_hit = True
            elif cache.misses > m0:
                cache_hit = False
        return {
            "abstained": ans.abstained,
            "cited_source_ids": [c.get("source_id") for c in ans.citations],
            "unsourced_count": len(ans.unsourced_claims),
            "security_rejected_count": ans.security_rejected_count,
            "cache_hit": cache_hit,
        }

    return run


def _vault_fingerprint() -> str:
    """A stable, non-path-leaking label for the vault root (never an absolute path in a durable artifact)."""
    return hashlib.sha256(str(settings.root).encode("utf-8")).hexdigest()[:16]


def _eval_client(fresh: bool) -> tuple[Any, _CountingCache | None]:
    """Build the (client, counting-cache) the eval run uses. Indirected so tests can inject a fake.
    `fresh` -> a cacheless client (bypasses cache lookup + write); else a client over a counting cache."""
    if fresh:
        return build_client(settings), None
    cache = _CountingCache(settings.response_cache_path)
    return build_client(settings, cache=cache), cache


@app.post("/evals/run", response_model=EvalRunResponse)
def run_eval_endpoint(req: EvalRunRequest) -> dict[str, Any]:
    """Run the real-vault answer-quality eval (ADR-0042). `dry_run` validates the corpus with NO LLM
    call; otherwise `confirm_cost` is required (cost-bearing), an unconfigured LLM is a controlled 503,
    and `limit` is clamped to the config hard cap. Writes a privacy-safe stamped report."""
    corpus_path = settings.eval_corpus_path
    if not corpus_path.exists():
        raise HTTPException(
            status_code=404,
            detail=(f"no eval corpus at {corpus_path.relative_to(settings.root)} — copy "
                    f"{settings.eval_corpus_example_path.relative_to(settings.root)} and curate it"))
    if req.limit is not None and req.limit < 0:
        raise HTTPException(status_code=400, detail="limit must be >= 0")
    cases = eval_answers.load_corpus(corpus_path.read_text(encoding="utf-8"))
    known = {m["source_id"] for m in manifests.valid_manifests(settings.manifests_dir)[0]
             if isinstance(m.get("source_id"), str)}
    requested = req.limit if req.limit is not None else settings.eval_max_questions_default
    limit = max(0, min(requested, settings.eval_max_questions_hard_cap))

    if req.dry_run:
        valid, skipped = [], []
        for c in cases:
            errs = eval_answers.validate_case(c, known)
            (skipped.append({"id": c.id, "reasons": errs}) if errs else valid.append(c))
        return {"status": "dry_run", "dry_run": {
            "n_corpus": len(cases), "n_valid": len(valid), "would_run": min(len(valid), limit),
            "limit": limit, "skipped": skipped}}

    if not req.confirm_cost:
        raise HTTPException(
            status_code=400,
            detail=("POST /evals/run makes real, cost-bearing LLM calls; set confirm_cost=true to "
                    "proceed (or dry_run=true to validate the corpus with no LLM call)"))

    # Controlled error posture, like /query (ADR-0042): client setup (a malformed QUERY_MODEL / unknown
    # provider is a ConfigError) AND every per-case LLM/synthesis/search failure are mapped to a
    # controlled 503 (no raw detail); a partial snapshot is never written.
    try:
        client, cache = _eval_client(req.fresh)  # fresh -> cacheless; else a counting cache
        if not client.provider_available(settings.query_model):
            raise HTTPException(
                status_code=503,
                detail=("answer-quality eval requires a configured LLM; set QUERY_MODEL and the "
                        "provider credential (GET /evals/results stays available without one)"))
        report = eval_answers.run_eval(
            cases, _eval_query_fn(client, cache), limit=limit, known_source_ids=known,
            cache_mode=("fresh" if req.fresh else "cached"))
    except HTTPException:
        raise  # already-controlled (provider unavailable / search-vector 4xx/503) — no raw detail
    except (ConfigError, ParseError, AdapterError) as exc:
        logger.warning("POST /evals/run: config/synthesis failed: %s", exc)
        raise HTTPException(
            status_code=503, detail="answer-quality eval is temporarily unavailable") from exc

    stamp = manifests.iso_now()
    # Collision-safe, append-only run id: two runs in the same second get distinct snapshot files.
    settings.eval_reports_dir.mkdir(parents=True, exist_ok=True)
    base = "run-" + ("".join(ch for ch in stamp if ch.isdigit())[:14] or "0")
    run_id, _n = base, 1
    while (settings.eval_reports_dir / f"{run_id}.json").exists():
        run_id, _n = f"{base}-{_n}", _n + 1
    meta = {
        "run_id": run_id, "created_at": stamp, "scoring_version": eval_answers.SCORING_VERSION,
        "model_ref": settings.query_model, "model_provider": settings.query_model.split(":", 1)[0],
        "graph_schema_version": graph.SCHEMA_VERSION, "vault_fingerprint": _vault_fingerprint(),
        "n_requested": requested, "n_run": report["n_run"], "n_skipped": report["n_skipped"],
    }
    report["meta"] = meta
    json_path = settings.eval_reports_dir / f"{run_id}.json"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    (settings.eval_reports_dir / f"{run_id}.md").write_text(
        eval_answers.render_markdown(report), encoding="utf-8")
    return {
        "status": "completed", "report_path": str(json_path.relative_to(settings.root)), "meta": meta,
        "summary": {k: report[k] for k in (
            "n_corpus", "n_valid", "n_run", "n_skipped", "n_passed", "n_failed",
            "predicate_pass_rates", "cache_mode", "cache_hits", "cache_misses")},
    }


@app.get("/evals/results", response_model=EvalResultsResponse)
def read_eval_results(run_id: str | None = None) -> dict[str, Any]:
    """List stored eval runs, or read one run's stored report (ADR-0042). Key-free; the stored artifact
    holds only ids/flags/scores/metadata (no source text). Loopback-only no-auth posture."""
    d = settings.eval_reports_dir
    if run_id is not None:
        path = safe_under(d, d, f"{run_id}.json")  # untrusted query param: no traversal
        if path is None:
            raise HTTPException(status_code=400, detail="invalid run_id")
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"no eval run {run_id!r}")
        return {"report": json.loads(path.read_text(encoding="utf-8"))}
    runs: list[dict[str, Any]] = []
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            rep = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        m = rep.get("meta", {})
        runs.append({"run_id": m.get("run_id", p.stem), "created_at": m.get("created_at"),
                     "model_ref": m.get("model_ref"), "n_run": rep.get("n_run"),
                     "n_passed": rep.get("n_passed"), "cache_mode": rep.get("cache_mode")})
    return {"runs": runs}
