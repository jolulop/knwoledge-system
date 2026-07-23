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
page.

ADR-0056 (document-complete coverage): extraction runs per **claim window** — greedy runs of
consecutive normalized chunks (`chunk-greedy-v1`) — locating each quote *inside its window
text* and translating to full-document offsets. The run **stages before replacing**: all of a
source's window calls must parse before its prior `derived_from` edges are superseded and the
replacement set emitted. A run that cannot produce the complete replacement (no key, any
window ParseError) leaves the existing claim layer untouched — stale-but-visible (validators
fail loudly if the Markdown changed underneath) is preferred over silently thinning the
factual layer. Empty Markdown is a deterministic complete replacement and may supersede to an
empty set. (This supersedes the earlier retract-first ordering.) A claim left with no
`active` edges becomes a **tombstone** page (`deprecated_candidate`, pending review); it is
never hard-deleted (CLAUDE.md rule 9) and its node stays page-backed (ADR-0030).

Source pages are assumed to already exist (run `generate_wiki` first). Supervised and
synchronous, the 3.5a shape: fingerprint-idempotent per source; no API key → the run is a
`skipped` job (stale sources still retracted).
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from app.backend import db, graph
from app.backend.manifests import get_status, iso_now, valid_manifests
from app.llm import prompts
from app.llm.client import LLMClient, ParseError
from app.workers import citations, reviews
from app.workers import enrichment_artifact as art
from app.workers import labels
from app.workers.wiki_render import render_claim_page, title_from_filename

_ENRICHABLE_STATUSES = {"extracted", "partial"}
_WS = re.compile(r"\s+")
_CLAIM_TEXT_RE = re.compile(r'(?m)^claim_text:\s*"(.*)"\s*$')
_STATUS_RE = re.compile(r"(?m)^status:\s*(\S+)\s*$")


def _read_status(page_path: Path) -> str | None:
    """Read the lifecycle `status` from an existing Claim page (the status authority, ADR-0022)."""
    if not page_path.exists():
        return None
    m = _STATUS_RE.search(page_path.read_text(encoding="utf-8", errors="replace"))
    return m.group(1) if m else None


CLAIM_ID_RE = re.compile(r"clm_[0-9a-f]{16}")


def is_claim_id(value: Any) -> bool:
    """True iff `value` is a canonical `clm_<16 hex>` claim id (the shape `claim_id` produces).

    The shape gate for untrusted ledger inputs (e.g. ADR-0044 supersede subject/winner) — claim ids
    flow into filesystem paths + the graph, so a non-canonical id must be rejected, not consumed.
    Uses `fullmatch` (NOT `match` + `^…$`, which accepts a trailing newline because `$` matches before
    it) so the whole string must be exactly the id.
    """
    return isinstance(value, str) and bool(CLAIM_ID_RE.fullmatch(value))


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


# --- claim windows (ADR-0056, `chunk-greedy-v1`) ----------------------------

CLAIM_WINDOW_STRATEGY = "chunk-greedy-v1"


def _chunk_records(chunk_path: Path) -> list[dict[str, Any]]:
    """Anchored chunk records from ``normalized/chunks/<sid>.jsonl``, ordered by ordinal.

    Records missing the citation anchor fields are skipped (not citable evidence — same
    posture as the keyword indexer's parser)."""
    if not chunk_path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in chunk_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("char_start") is None or rec.get("char_end") is None:
            continue
        records.append(rec)
    records.sort(key=lambda r: (r.get("ordinal") if isinstance(r.get("ordinal"), int) else 0,
                                r["char_start"]))
    return records


def _section_context(rec: dict[str, Any]) -> str | None:
    """Local heading context for a window's prompt, from its first chunk's metadata."""
    heading_path = rec.get("heading_path")
    if isinstance(heading_path, list) and heading_path:
        return " > ".join(str(h) for h in heading_path)
    section = rec.get("section")
    return str(section) if section else None


def plan_windows(chunks: list[dict[str, Any]], window_chars: int) -> list[dict[str, Any]]:
    """Greedy chunk-run window plan (ADR-0056 decision 4, ``chunk-greedy-v1``).

    Windows are greedy runs of consecutive chunks, ordered by ordinal, bounded by the actual
    full-Markdown span ``last.char_end - first.char_start`` (inter-chunk headings/blank lines
    count — they are inside the window text). A chunk is never split: a single chunk whose own
    span exceeds the budget becomes a counted singleton ``over_budget`` window. Deterministic
    for a fixed chunk table."""
    windows: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for rec in chunks:
        start, end = rec["char_start"], rec["char_end"]
        if current is not None and end - current["char_start"] <= window_chars:
            current["char_end"] = end
            continue
        if current is not None:
            windows.append(current)
        current = {
            "char_start": start,
            "char_end": end,
            "over_budget": (end - start) > window_chars,
            "section": _section_context(rec),
        }
    if current is not None:
        windows.append(current)
    return windows


def _write_source_artifact(apath, sid, fingerprint, source_claims, model_ref, now,
                           strategy_ref=None):
    apath.parent.mkdir(parents=True, exist_ok=True)
    apath.write_text(json.dumps({
        "source_id": sid, "schema_version": art.CLAIM_SCHEMA_VERSION,
        "prompt_version": art.CLAIM_PROMPT_VERSION, "model_ref": model_ref,
        "strategy_ref": strategy_ref,
        "input_fingerprint": fingerprint, "generation_status": "enriched",
        "generated_at": now, "claims": source_claims,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def recompose_claim(gconn, *, cid, claims_dir, reviews_dir, now, markdown_dir, text_hint=None,
                    contradiction_affected=None, deprecate=False, review_status=None,
                    hide=False, unhide=False) -> str:
    """Render a Claim page from its `active` derived_from edges (tombstone if none).

    The single, shared claim renderer (used by the claim worker and the contradiction worker):
    it composes the page's citations and active `contradicts` backlinks from the graph. When a
    claim tombstones (loses all active evidence), it also retracts the relationships that need an
    active endpoint — superseding `contradicts` assertions touching it and withdrawing their
    pending reviews — and records the surviving endpoints in `contradiction_affected` so the
    caller re-renders their pages to drop the dead backlink (ADR-0031). This keeps the endpoint
    invariant local, so the claim CLI stays valid without a separate contradiction pass.

    `review_status` overrides the renderer's derived value — the Phase-6 deprecation executor passes
    `"approved"` so an approved tombstone deprecation isn't rendered as `pending` (ADR-0035 A5).

    ADR-0048: `hide` sets a `hidden` governance status (precedence over evidence-derivation), `unhide`
    clears it and re-derives, and by default the page's current `hidden` is **preserved** across re-render
    (exactly like `deprecated_candidate`) — a later evidence-driven recompose never silently un-hides. A
    hidden **partner** claim is omitted from the rendered Contradicting Claims section (discovery surface;
    the edge stays active in the graph)."""
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
    current_status = _read_status(page_path)
    # ADR-0048 hidden governance status: precedence over evidence; set by hide, cleared by unhide, else
    # preserved from the page (the status authority, ADR-0022) so re-render never silently un-hides.
    hidden = True if hide else (False if unhide else current_status == "hidden")
    # Active contradiction backlinks (ADR-0031) project onto the Claim page; the graph holds the
    # relationship authority, so the single claim renderer reads them here for any caller.
    contradicts = graph.active_contradictions_for_claim(gconn, cid) if cites else []
    # ADR-0048: omit hidden partner claims from the rendered section (discovery surface only — the
    # contradicts edge stays active in the graph for raw /graph/* inspection).
    if contradicts:
        contradicts = [p for p in contradicts
                       if (graph.get_node(gconn, p) or {}).get("status") != "hidden"]
    # A claim with evidence is deprecated only by a human supersede decision (slice 1b): set on
    # request, and *preserved* across re-extraction since the page is the status authority
    # (ADR-0022) — a re-extraction must not silently resurrect a deprecated loser to `active`. A hidden
    # claim is never also deprecated (hide is active-only; hidden has precedence).
    deprecated = bool(cites) and not hidden and (deprecate or current_status == "deprecated_candidate")
    claims_dir.mkdir(parents=True, exist_ok=True)
    # ADR-0060: resolve display labels worker-side (page-local) for the evidence-table source
    # links and contradicting-claim backlinks; the renderer stays IO-free.
    link_labels = labels.display_labels(
        claims_dir.parent,
        [f"Sources/{c['source_id']}" for c in cites] + [f"Claims/{p}" for p in contradicts])
    page_path.write_text(
        render_claim_page({"claim_id": cid, "claim_text": claim_text, "confidence": "low",
                           "citations": cites, "contradicts": contradicts, "deprecated": deprecated,
                           "hidden": hidden},
                          review_status=review_status, labels=link_labels),
        encoding="utf-8",
    )
    # Mirror the page's status into the derived node index (hidden > deprecated/tombstone > active).
    node_status = "hidden" if hidden else ("deprecated_candidate" if (deprecated or not cites) else "active")
    graph.upsert_node(gconn, node_id=cid, node_type="claim", slug=cid, status=node_status, now=now)
    if not cites and not hidden:
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
        # Tombstone -> review-gated deprecation, same as knowledge-item tombstones (B1).
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
    chunks_dir: Path | None = None,
    window_chars: int = 12000,
    rebuild_index: bool = True,
    record_job: bool = True,
) -> dict[str, Any]:
    """Extract grounded claims for pending (or selected) sources; return a run summary.

    ADR-0056: extraction is windowed (`chunk-greedy-v1` over ``chunks_dir``; ``window_chars``
    is the span budget and part of the strategy ref) and staged — see the module docstring."""
    root = Path(root).resolve()
    manifests_dir = Path(manifests_dir) if manifests_dir else root / "raw" / "manifests"
    jobs_db = Path(jobs_db) if jobs_db else root / "db" / "jobs.sqlite"
    graph_db = Path(graph_db) if graph_db else root / "db" / "graph.sqlite"
    markdown_dir = Path(markdown_dir) if markdown_dir else root / "normalized" / "markdown"
    enrichment_dir = Path(enrichment_dir) if enrichment_dir else root / "normalized" / "enrichment"
    reviews_dir = Path(reviews_dir) if reviews_dir else root / "reviews"
    chunks_dir = Path(chunks_dir) if chunks_dir else root / "normalized" / "chunks"
    claims_dir = (Path(wiki_dir) if wiki_dir else root / "wiki") / "Claims"
    strategy_ref = art.claims_strategy_ref(window_chars)

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
    # ADR-0063: resolve the tier's ordered chain once per run to the first available concrete
    # model_ref (availability-only); keep the first-preference ref when none is available so the
    # no-key/stub fingerprint stays a valid concrete ref.
    model_ref, has_key = client.resolve_run_model(model_ref)

    considered = sources_with_claims = claims_written = claims_dropped_ungrounded = 0
    skipped_fresh = skipped_not_extracted = skipped_empty = skipped_no_key = 0
    claim_windows = claim_window_over_budget = 0
    replacement_not_applied = stale_claim_layer_preserved = 0
    errors: list[dict[str, Any]] = []
    texts: dict[str, str] = {}     # cid -> claim_text for claims (re)asserted this run
    touched: set[str] = set()
    affected: set[str] = set()     # claims whose edges were superseded this run

    try:
        manifests, skipped_invalid = valid_manifests(manifests_dir)
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
            fingerprint = art.claims_fingerprint(md, model_ref, strategy_ref)
            apath = art.claims_artifact_path(enrichment_dir, sid)
            if not force and apath.exists():
                try:
                    fresh = json.loads(apath.read_text(encoding="utf-8")).get("input_fingerprint") == fingerprint
                except (OSError, json.JSONDecodeError):
                    fresh = False
                if fresh:
                    skipped_fresh += 1
                    continue

            # ADR-0056 decision 3 (stage before replacing): a run that cannot produce the
            # complete replacement never supersedes existing evidence, so the no-key gate
            # comes first and the supersede moves below, after every window has parsed.
            # Missing key is in the "cannot produce a complete replacement" class, so it
            # counts as replacement_not_applied (review round 2).
            if not has_key:
                skipped_no_key += 1
                replacement_not_applied += 1
                if art.stored_claim_count(enrichment_dir, sid) > 0:
                    stale_claim_layer_preserved += 1
                continue
            if not md.strip():
                # Empty Markdown IS a deterministic complete replacement (ADR-0056).
                affected.update(graph.supersede_source_edges(gconn, sid, now=now))
                skipped_empty += 1
                _write_source_artifact(apath, sid, fingerprint, [], model_ref, now, strategy_ref)
                continue

            # Window plan (`chunk-greedy-v1`): greedy runs of consecutive chunks. Non-empty
            # Markdown with no anchored chunk records is normalized drift (the ADR-0012
            # md<->chunks invariant broken on disk) — FAIL CLOSED (review round 2): no model
            # call, no supersede; the old layer stays visible and validators surface the
            # drift. Never paper over it with an unwindowed whole-document call.
            windows = plan_windows(_chunk_records(chunks_dir / f"{sid}.jsonl"), window_chars)
            if not windows:
                errors.append({"source_id": sid, "error":
                               "window_planning_failed: no anchored chunk records for "
                               "non-empty markdown (normalized drift?)"})
                replacement_not_applied += 1
                if art.stored_claim_count(enrichment_dir, sid) > 0:
                    stale_claim_layer_preserved += 1
                continue
            claim_window_over_budget += sum(1 for w in windows if w["over_budget"])

            title = title_from_filename(manifest.get("original_filename", sid))
            # STAGE: every window must parse before any graph/wiki mutation for this source.
            staged: list[tuple[dict[str, Any], dict[str, Any]]] = []
            failed: dict[str, Any] | None = None
            for i, w in enumerate(windows, 1):
                window_text = md[w["char_start"]:w["char_end"]]
                claim_windows += 1
                try:
                    result = client.parse(
                        prompts.build_claim_messages(
                            title, window_text, segment_index=i, segment_count=len(windows),
                            section_context=w["section"]),
                        prompts.CLAIMS_SCHEMA, model_ref,
                        schema_version=art.CLAIM_SCHEMA_VERSION,
                        prompt_version=art.CLAIM_PROMPT_VERSION,
                        strategy_ref=strategy_ref,
                    )
                except ParseError as exc:
                    failed = {"source_id": sid, "window": i, "windows": len(windows),
                              "error": str(exc)}
                    break
                staged.append((w, result))
            if failed is not None:
                errors.append(failed)
                replacement_not_applied += 1
                if art.stored_claim_count(enrichment_dir, sid) > 0:
                    stale_claim_layer_preserved += 1
                continue

            # Replacement staged in full: NOW retire the prior evidence and emit it.
            affected.update(graph.supersede_source_edges(gconn, sid, now=now))
            graph.upsert_node(
                gconn, node_id=sid, node_type="source", slug=sid,
                status=get_status(manifest), now=now)
            source_claims: list[dict[str, Any]] = []
            seen: set[tuple[str, int, int]] = set()
            for w, result in staged:
                window_start = w["char_start"]
                window_text = md[window_start:w["char_end"]]
                for item in result["claims"]:
                    text = str(item.get("claim", "")).strip()
                    # The window body is entity-escaped in the prompt (ADR-0061), so the model
                    # may return the escaped form (`AT&amp;T`, `a &lt; b`); unescape exactly once
                    # here so grounding runs against — and stores — the source-faithful quote.
                    quote = html.unescape(str(item.get("quote", "")))
                    if not text:
                        continue
                    # Locate inside the WINDOW text, then translate to full-document offsets
                    # (ADR-0056: full-doc first-match can anchor a repeated phrase to the
                    # wrong occurrence).
                    span = citations.locate_quote(window_text, quote)
                    if span is None:
                        claims_dropped_ungrounded += 1
                        continue
                    start, end = span[0] + window_start, span[1] + window_start
                    citation = {"source_id": sid, "char_start": start, "char_end": end,
                                "quote": md[start:end]}
                    if citations.ground_citation(citation, md, require_quote=True):
                        claims_dropped_ungrounded += 1
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

            _write_source_artifact(apath, sid, fingerprint, source_claims, model_ref, now, strategy_ref)
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
            "claim_pages_tombstoned": pages_tombstoned,
            "claims_dropped_ungrounded": claims_dropped_ungrounded,
            # ADR-0056 coverage/staging observability: window calls made, over-budget
            # singleton windows, and the staging outcomes ("nothing changed because staging
            # failed" is distinct from "replacement applied with zero claims").
            "claim_window_strategy": CLAIM_WINDOW_STRATEGY,
            "claim_windows": claim_windows,
            "claim_window_over_budget": claim_window_over_budget,
            "replacement_not_applied": replacement_not_applied,
            "stale_claim_layer_preserved": stale_claim_layer_preserved,
            "skipped_fresh": skipped_fresh, "skipped_not_extracted": skipped_not_extracted,
            "skipped_empty": skipped_empty, "skipped_no_key": skipped_no_key,
            "manifests_skipped_invalid": len(skipped_invalid),
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
