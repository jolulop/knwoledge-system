from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query

# Ensure the repo root is importable when launched as `uvicorn app.backend.main:app`.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.backend import db, graph, graph_read, keyword_index, manifests, search
from app.backend.config import get_settings
from app.backend.models import (
    ChunksResponse,
    GraphNeighborhoodResponse,
    GraphNodeResponse,
    HealthResponse,
    Job,
    JobsResponse,
    NormalizedResponse,
    SearchResponse,
    Source,
    SourcesResponse,
    WikiPageDetail,
    WikiPagesResponse,
)
from app.backend.policy import load_retrieval_policy
from app.workers import extract, intake, wiki
from app.workers.wiki_render import parse_frontmatter

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


@app.get("/search", response_model=SearchResponse)
def run_search_endpoint(
    q: str,
    mode: str = "auto",
    source_id: str | None = None,
    page_type: str | None = None,
    node_type: str | None = None,
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
    if mode == "vector":
        raise HTTPException(
            status_code=400,
            detail="vector retrieval arrives in Phase 4d; use keyword/navigation/graph/auto",
        )
    for label, value in (("page_type", page_type), ("node_type", node_type)):
        if value is not None and value not in graph.NODE_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"unknown {label} {value!r}; allowed: {sorted(graph.NODE_TYPES)}",
            )
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

    policy = load_retrieval_policy(settings.retrieval_policy_path)
    keyword_conn = (
        keyword_index.connect(settings.keyword_index_path)
        if settings.keyword_index_path.exists() else None
    )
    graph_conn = _open_graph_safe()
    try:
        result = search.run_search(
            q=q, mode=mode, keyword_conn=keyword_conn, graph_conn=graph_conn, policy=policy,
            source_id=source_id, page_type=page_type, node_type=node_type, language=language,
            source_statuses=source_statuses, node_statuses=node_statuses, edge_statuses=edge_statuses,
            evidence_limit=evidence_limit, navigation_limit=navigation_limit, graph_limit=graph_limit,
        )
    finally:
        if keyword_conn is not None:
            keyword_conn.close()
        if graph_conn is not None:
            graph_conn.close()
    return result


@app.get("/wiki/index")
def read_index() -> dict[str, str]:
    path = settings.root / "wiki" / "index.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="index.md not found")
    return {
        "path": str(path.relative_to(settings.root)),
        "content": path.read_text(encoding="utf-8"),
    }
