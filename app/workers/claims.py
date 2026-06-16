#!/usr/bin/env python3
"""Phase 3.5b claim-extraction worker (slice 3a, ADR-0019/0020/0026/0030).

Tier-2 LLM pass: for each extracted/partial source, ask for atomic factual claims each with
a verbatim evidence quote, **locate** the quote in the normalized Markdown to derive its
char span, **ground** the citation (drop the claim if the quote cannot be located), then
write `derived_from` (claim → source) edges into the graph as `active` (grounded provenance,
not a semantic judgment — ADR-0030).

Claim pages are **rendered from the graph**: a page's citations are the claim's `active`
derived_from edges (quotes reconstructed from the source spans), so the same statement from
several sources aggregates onto one page across runs. The Claim page frontmatter is the
durable authority for the claim's wording (`claim_text`); recompose reads it back from the
page. When a source is (re)processed its prior assertions are **superseded first** — before
the can-we-extract gates — so a changed source whose re-extraction can't complete
(no key / empty / parse error) still retracts its stale evidence. A claim left with no
`active` edges becomes a **tombstone** page (`deprecated_candidate`, pending review); it is
never hard-deleted (CLAUDE.md rule 9) and its node stays page-backed (ADR-0030).

Source pages are assumed to already exist (run `generate_wiki` first). Supervised and
synchronous, the 3.5a shape: fingerprint-idempotent per source; no API key → the run is a
`skipped` job (stale sources still retracted).
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
from app.workers import citations, reviews
from app.workers import enrichment_artifact as art
from app.workers.wiki_render import render_claim_page, title_from_filename

_ENRICHABLE_STATUSES = {"extracted", "partial"}
_WS = re.compile(r"\s+")
_CLAIM_TEXT_RE = re.compile(r'(?m)^claim_text:\s*"(.*)"\s*$')


def claim_id(claim_text: str) -> str:
    """Content-derived, source-agnostic claim id frozen at creation (ADR-0021)."""
    norm = _WS.sub(" ", claim_text).strip()
    return f"clm_{hashlib.sha256(norm.encode('utf-8')).hexdigest()[:16]}"


def _read_claim_text(page_path: Path) -> str | None:
    """Read the durable claim_text from an existing Claim page's frontmatter (unescaped)."""
    if not page_path.exists():
        return None
    m = _CLAIM_TEXT_RE.search(page_path.read_text(encoding="utf-8", errors="replace"))
    return re.sub(r"\\(.)", r"\1", m.group(1)) if m else None


def _rebuild_index(root: Path) -> bool:
    script = root / "scripts" / "rebuild_index.py"
    if not script.exists():
        return False
    return subprocess.run([sys.executable, str(script), str(root)]).returncode == 0


def _write_source_artifact(apath, sid, fingerprint, source_claims, model_ref, now):
    apath.parent.mkdir(parents=True, exist_ok=True)
    apath.write_text(json.dumps({
        "source_id": sid, "schema_version": art.CLAIM_SCHEMA_VERSION,
        "prompt_version": art.CLAIM_PROMPT_VERSION, "model_ref": model_ref,
        "input_fingerprint": fingerprint, "generation_status": "enriched",
        "generated_at": now, "claims": source_claims,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def recompose_claim(gconn, *, cid, claims_dir, reviews_dir, now, markdown_dir, text_hint=None,
                    contradiction_affected=None) -> str:
    """Render a Claim page from its `active` derived_from edges (tombstone if none).

    The single, shared claim renderer (used by the claim worker and the contradiction worker):
    it composes the page's citations and active `contradicts` backlinks from the graph. When a
    claim tombstones (loses all active evidence), it also retracts the relationships that need an
    active endpoint — superseding `contradicts` assertions touching it and withdrawing their
    pending reviews — and records the surviving endpoints in `contradiction_affected` so the
    caller re-renders their pages to drop the dead backlink (ADR-0031). This keeps the endpoint
    invariant local, so the claim CLI stays valid without a separate contradiction pass."""
    edges = [e for e in graph.outgoing_active(gconn, cid) if e["edge_type"] == "derived_from"]
    page_path = claims_dir / f"{cid}.md"
    claim_text = text_hint or _read_claim_text(page_path)
    if claim_text is None:
        return "skipped"  # no durable wording to render from (shouldn't happen)
    cites = []
    for e in edges:
        src, start, end = e["dst_id"], e["evidence_char_start"], e["evidence_char_end"]
        md_path = markdown_dir / f"{src}.md"
        md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
        quote = md[start:end] if start is not None and end is not None and end <= len(md) else ""
        cites.append({"source_id": src, "char_start": start, "char_end": end, "quote": quote})
    cites.sort(key=lambda c: (c["source_id"], c["char_start"] if c["char_start"] is not None else -1))
    # Active contradiction backlinks (ADR-0031) project onto the Claim page; the graph holds the
    # relationship authority, so the single claim renderer reads them here for any caller.
    contradicts = graph.active_contradictions_for_claim(gconn, cid) if cites else []
    claims_dir.mkdir(parents=True, exist_ok=True)
    page_path.write_text(
        render_claim_page({"claim_id": cid, "claim_text": claim_text, "confidence": "low",
                           "citations": cites, "contradicts": contradicts}),
        encoding="utf-8",
    )
    # Mirror the page's status into the derived node index (active vs tombstone).
    graph.upsert_node(gconn, node_id=cid, node_type="claim", slug=cid,
                      status="active" if cites else "deprecated_candidate", now=now)
    if not cites:
        # Endpoint invariant (ADR-0031): a tombstoned claim can no longer anchor a contradiction,
        # so supersede any contradicts assertions touching it (even an acknowledged/active one)
        # and withdraw their pending reviews. Surviving endpoints are recorded so the caller
        # re-renders their pages and drops the now-dead backlink.
        for row in graph.supersede_contradictions_for_claim(gconn, cid, now=now):
            other = row["dst_id"] if row["src_id"] == cid else row["src_id"]
            rid = reviews.review_id(
                "resolve_contradiction", {"claim_a": row["src_id"], "claim_b": row["dst_id"]})
            reviews.withdraw_review_item(reviews_dir, rid, reason="endpoint claim retracted", now=now)
            if contradiction_affected is not None:
                contradiction_affected.add(other)
        # Tombstone -> review-gated deprecation, same as concept/entity tombstones (B1).
        reviews.create_review_item(
            reviews_dir, review_type="deprecate_wiki_page",
            subject={"node_id": cid, "page": f"Claims/{cid}.md"},
            proposal={"to_status": "deprecated_candidate",
                      "reason": "no active source evidence remains"},
            context={"node_type": "claim"}, now=now)
        return "tombstoned"
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
    reviews_dir: Path | None = None,
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
    reviews_dir = Path(reviews_dir) if reviews_dir else root / "reviews"
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
    texts: dict[str, str] = {}     # cid -> claim_text for claims (re)asserted this run
    touched: set[str] = set()
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

            md_path = markdown_dir / f"{sid}.md"
            md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
            fingerprint = art.claims_fingerprint(md, model_ref)
            apath = art.claims_artifact_path(enrichment_dir, sid)
            if not force and apath.exists():
                try:
                    fresh = json.loads(apath.read_text(encoding="utf-8")).get("input_fingerprint") == fingerprint
                except (OSError, json.JSONDecodeError):
                    fresh = False
                if fresh:
                    skipped_fresh += 1
                    continue

            # (Re)processing this source: retract its prior evidence FIRST, so a changed
            # source whose re-extraction can't complete still drops its stale claims (ADR-0030).
            affected.update(graph.supersede_source_edges(gconn, sid, now=now))

            if not has_key:
                skipped_no_key += 1
                continue
            if not md.strip():
                skipped_empty += 1
                _write_source_artifact(apath, sid, fingerprint, [], model_ref, now)
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
                    claims_dropped += 1
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
                texts[cid] = text
                touched.add(cid)
                graph.upsert_node(gconn, node_id=cid, node_type="claim", slug=cid, status="active", now=now)
                graph.upsert_assertion(
                    gconn, src_id=cid, dst_id=sid, edge_type="derived_from", asserted_by="llm",
                    status="active", evidence_source_id=sid, evidence_char_start=start,
                    evidence_char_end=end, job_id=job_id, now=now,
                )

            _write_source_artifact(apath, sid, fingerprint, source_claims, model_ref, now)
            claims_written += len(source_claims)
            if source_claims:
                sources_with_claims += 1

        pages_written = pages_tombstoned = 0
        contradiction_affected: set[str] = set()  # surviving endpoints of superseded contradictions
        for cid in touched | affected:
            outcome = recompose_claim(gconn, cid=cid, claims_dir=claims_dir, reviews_dir=reviews_dir,
                                      markdown_dir=markdown_dir, now=now, text_hint=texts.get(cid),
                                      contradiction_affected=contradiction_affected)
            pages_written += outcome == "written"
            pages_tombstoned += outcome == "tombstoned"

        # Re-render the surviving endpoints of any contradiction superseded above so their pages
        # drop the now-dead backlink (the claim CLI stays valid without a contradiction pass).
        for cid in sorted(contradiction_affected):
            node = graph.get_node(gconn, cid)
            if node and node["status"] == "active":
                recompose_claim(gconn, cid=cid, claims_dir=claims_dir, reviews_dir=reviews_dir,
                                markdown_dir=markdown_dir, now=now)

        index_rebuilt = _rebuild_index(root) if (
            rebuild_index and (touched or affected or contradiction_affected)) else False

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
            "claim_pages_tombstoned": pages_tombstoned, "claims_dropped": claims_dropped,
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
