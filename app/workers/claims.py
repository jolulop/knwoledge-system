#!/usr/bin/env python3
"""Phase 3.5b claim-extraction worker (slice 3a, ADR-0019/0020/0026/0030).

Tier-2 LLM pass: for each extracted/partial source, ask for atomic factual claims each with
a verbatim evidence quote, **locate** the quote in the normalized Markdown to derive its
char span, **ground** the citation (drop the claim if the quote cannot be located), then
write `derived_from` (claim → source) edges into the graph as `active` (grounded provenance,
not a semantic judgment — ADR-0030).

Claim pages are **rendered from the graph**: a page's citations are the claim's `active`
derived_from edges (quotes reconstructed from the source spans), so the same statement from
several sources aggregates onto one page across runs. On re-extraction a source's prior
assertions are **superseded** first, then current ones written, and every affected claim
page is recomposed from its surviving `active` edges — pages with none are deleted. Source
pages are assumed to already exist (run `generate_wiki` first); this worker does not create
them.

Supervised and synchronous, the 3.5a shape: fingerprint-idempotent per source, no API key →
the whole run is recorded as a `skipped` job and no claims are written.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from app.backend import db, graph
from app.backend.manifests import iso_now, list_manifests
from app.llm import prompts
from app.llm.client import LLMClient, ParseError
from app.workers import citations
from app.workers import enrichment_artifact as art
from app.workers.wiki_render import render_claim_page, title_from_filename

_ENRICHABLE_STATUSES = {"extracted", "partial"}
_WS = re.compile(r"\s+")


def claim_id(claim_text: str) -> str:
    """Content-derived, source-agnostic claim id frozen at creation (ADR-0021)."""
    norm = _WS.sub(" ", claim_text).strip()
    return f"clm_{hashlib.sha256(norm.encode('utf-8')).hexdigest()[:16]}"


def _record_path(enrichment_dir: Path, cid: str) -> Path:
    """Per-claim text record (the one fact not in the graph): claim_text by claim_id."""
    return enrichment_dir / "claims" / f"{cid}.json"


def _rebuild_index(root: Path) -> bool:
    script = root / "scripts" / "rebuild_index.py"
    if not script.exists():
        return False
    return subprocess.run([sys.executable, str(script), str(root)]).returncode == 0


def _recompose_claim(
    gconn, *, cid: str, enrichment_dir: Path, claims_dir: Path, markdown_dir: Path
) -> str:
    """Render one Claim page from its `active` derived_from edges, or delete it if none."""
    edges = [e for e in graph.outgoing_active(gconn, cid) if e["edge_type"] == "derived_from"]
    page_path = claims_dir / f"{cid}.md"
    record_path = _record_path(enrichment_dir, cid)
    if not edges:
        page_path.unlink(missing_ok=True)
        record_path.unlink(missing_ok=True)
        return "deleted"
    try:
        claim_text = json.loads(record_path.read_text(encoding="utf-8"))["claim_text"]
    except (OSError, json.JSONDecodeError, KeyError):
        return "skipped"  # no text record (shouldn't happen) — leave the page as-is
    cites = []
    for e in edges:
        src = e["dst_id"]
        start, end = e["evidence_char_start"], e["evidence_char_end"]
        md_path = markdown_dir / f"{src}.md"
        md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
        quote = md[start:end] if start is not None and end is not None and end <= len(md) else ""
        cites.append({"source_id": src, "char_start": start, "char_end": end, "quote": quote})
    cites.sort(key=lambda c: (c["source_id"], c["char_start"]))
    claims_dir.mkdir(parents=True, exist_ok=True)
    page_path.write_text(
        render_claim_page({"claim_id": cid, "claim_text": claim_text, "confidence": "low",
                           "citations": cites}),
        encoding="utf-8",
    )
    return "written"


def extract_claims(
    root: Path,
    *,
    client: LLMClient,
    model_ref: str,
    source_ids: list[str] | None = None,
    force: bool = False,
    manifests_dir: Path | None = None,
    jobs_db: Path | None = None,
    graph_db: Path | None = None,
    markdown_dir: Path | None = None,
    enrichment_dir: Path | None = None,
    wiki_dir: Path | None = None,
    rebuild_index: bool = True,
    record_job: bool = True,
) -> dict[str, Any]:
    """Extract grounded claims for pending (or selected) sources; return a run summary."""
    root = Path(root).resolve()
    manifests_dir = Path(manifests_dir) if manifests_dir else root / "raw" / "manifests"
    jobs_db = Path(jobs_db) if jobs_db else root / "db" / "jobs.sqlite"
    graph_db = Path(graph_db) if graph_db else root / "db" / "graph.sqlite"
    markdown_dir = Path(markdown_dir) if markdown_dir else root / "normalized" / "markdown"
    enrichment_dir = Path(enrichment_dir) if enrichment_dir else root / "normalized" / "enrichment"
    claims_dir = (Path(wiki_dir) if wiki_dir else root / "wiki") / "Claims"

    now = iso_now()
    job_id = f"job_{uuid.uuid4().hex[:16]}"
    conn = None
    if record_job:
        db.init_db(jobs_db)
        conn = db.connect(jobs_db)
        db.insert_job(conn, job_id=job_id, job_type="extract_claims", status="running",
                      created_at=now, started_at=now)

    graph.init_db(graph_db)
    gconn = graph.connect(graph_db)
    has_key = client.provider_available(model_ref)

    considered = sources_with_claims = claims_written = claims_dropped = 0
    skipped_fresh = skipped_not_extracted = skipped_empty = skipped_no_key = 0
    errors: list[dict[str, str]] = []
    touched: set[str] = set()      # claims (re)asserted this run
    affected: set[str] = set()     # claims whose edges were superseded this run

    try:
        manifests = list_manifests(manifests_dir)
        if source_ids is not None:
            wanted = set(source_ids)
            manifests = [m for m in manifests if m.get("source_id") in wanted]

        for manifest in manifests:
            sid = manifest["source_id"]
            if manifest.get("ingestion_status") not in _ENRICHABLE_STATUSES:
                skipped_not_extracted += 1
                continue
            considered += 1
            if not has_key:
                skipped_no_key += 1
                continue

            md_path = markdown_dir / f"{sid}.md"
            md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
            if not md.strip():
                skipped_empty += 1
                continue

            fingerprint = art.claims_fingerprint(md, model_ref)
            apath = art.claims_artifact_path(enrichment_dir, sid)
            if not force and apath.exists():
                try:
                    existing = json.loads(apath.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    existing = {}
                if existing.get("input_fingerprint") == fingerprint:
                    skipped_fresh += 1
                    continue

            title = title_from_filename(manifest.get("original_filename", sid))
            try:
                result = client.parse(
                    prompts.build_claim_messages(title, md), prompts.CLAIMS_SCHEMA, model_ref,
                    schema_version=art.CLAIM_SCHEMA_VERSION, prompt_version=art.CLAIM_PROMPT_VERSION,
                )
            except ParseError as exc:
                errors.append({"source_id": sid, "error": str(exc)})
                continue

            # Re-extraction: supersede this source's prior assertions before writing current
            # ones, and recompose every claim they touched (ADR-0030).
            affected.update(graph.supersede_source_edges(gconn, sid, now=now))
            graph.upsert_node(gconn, node_id=sid, node_type="source", slug=sid, status="active", now=now)

            source_claims: list[dict[str, Any]] = []
            seen: set[tuple[str, int, int]] = set()
            for item in result["claims"]:
                text = str(item.get("claim", "")).strip()
                quote = str(item.get("quote", ""))
                if not text:
                    continue
                span = citations.locate_quote(md, quote)
                if span is None:
                    claims_dropped += 1  # quote not locatable -> drop (ADR-0026)
                    continue
                start, end = span
                citation = {"source_id": sid, "char_start": start, "char_end": end, "quote": md[start:end]}
                if citations.ground_citation(citation, md, require_quote=True):
                    claims_dropped += 1
                    continue
                cid = claim_id(text)
                if (cid, start, end) in seen:
                    continue
                seen.add((cid, start, end))
                source_claims.append({"claim_id": cid, "claim_text": text, "citation": citation})
                touched.add(cid)
                graph.upsert_node(gconn, node_id=cid, node_type="claim", slug=cid, status="active", now=now)
                graph.upsert_assertion(
                    gconn, src_id=cid, dst_id=sid, edge_type="derived_from", asserted_by="llm",
                    status="active", evidence_source_id=sid, evidence_char_start=start,
                    evidence_char_end=end, job_id=job_id, now=now,
                )
                rec_path = _record_path(enrichment_dir, cid)
                rec_path.parent.mkdir(parents=True, exist_ok=True)
                rec_path.write_text(json.dumps({"claim_id": cid, "claim_text": text},
                                               ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            enrichment_dir.mkdir(parents=True, exist_ok=True)
            apath.write_text(json.dumps({
                "source_id": sid, "schema_version": art.CLAIM_SCHEMA_VERSION,
                "prompt_version": art.CLAIM_PROMPT_VERSION, "model_ref": model_ref,
                "input_fingerprint": fingerprint, "generation_status": "enriched",
                "generated_at": now, "claims": source_claims,
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            claims_written += len(source_claims)
            if source_claims:
                sources_with_claims += 1

        pages_written = pages_deleted = 0
        for cid in touched | affected:
            outcome = _recompose_claim(gconn, cid=cid, enrichment_dir=enrichment_dir,
                                       claims_dir=claims_dir, markdown_dir=markdown_dir)
            pages_written += outcome == "written"
            pages_deleted += outcome == "deleted"

        index_rebuilt = _rebuild_index(root) if (rebuild_index and (touched or affected)) else False

        if errors:
            status = "partial"
        elif not has_key and considered > 0:
            status = "skipped"
        else:
            status = "succeeded"

        summary: dict[str, Any] = {
            "job_id": job_id, "model_ref": model_ref, "status": status,
            "sources_considered": considered, "sources_with_claims": sources_with_claims,
            "claims_written": claims_written, "claim_pages_written": pages_written,
            "claim_pages_deleted": pages_deleted, "claims_dropped": claims_dropped,
            "skipped_fresh": skipped_fresh, "skipped_not_extracted": skipped_not_extracted,
            "skipped_empty": skipped_empty, "skipped_no_key": skipped_no_key,
            "errors": len(errors), "error_details": errors,
            "index_rebuilt": index_rebuilt, "extracted_at": now,
        }
        if conn is not None:
            db.update_job(conn, job_id, status=status, finished_at=iso_now(), metadata=summary)
        return summary
    except Exception as exc:
        if conn is not None:
            db.update_job(conn, job_id, status="failed", finished_at=iso_now(), error_message=str(exc))
        raise
    finally:
        gconn.close()
        if conn is not None:
            conn.close()
