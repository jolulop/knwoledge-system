#!/usr/bin/env python3
"""Tier-2 knowledge-item extraction worker (ADR-0059; mechanics from ADR-0017/0018/0021/0030).

Tier-2 LLM pass: for each extracted/partial source, identify the knowledge **items** it is
about — one structural node family (`node_type: item`, type-neutral `itm_` id) classified by
the governed 15-type `item_type` taxonomy (`app/backend/taxonomy.py`) plus the QA-only
`unclassified_review_required` sentinel. Each becomes a node created `candidate` (ADR-0018),
with an `active` `mentions` edge (source → node) recording provenance. Items are interpretive
labels, **not verbatim-grounded** like claims (an optional evidence anchor is stored only
when the name is mechanically locatable); quality comes from ≥2-source promotion.

Pages are deterministic stubs rendered from the graph (Mentioned-by = active incoming
mentions) in ONE flat `wiki/Items/` directory; the page frontmatter is the durable authority
for the node's title/aliases/item_type. A later extraction that classifies an existing name
under a different type routes its mention to the existing node and files a
`change_item_type` review — nothing auto-retypes (ADR-0059 decision 3). On re-extraction a
source's prior mentions are superseded **only once the run can produce the replacement**
(after a successful parse, or deterministically for an empty source — the ADR-0055
rollout-safety ordering), and affected node pages are recomposed from surviving `active`
mentions (tombstoned if none).
Source pages are assumed to exist (run `generate_wiki` first). Same operational shape as the
claim worker.
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

from app.backend import db, graph, taxonomy
from app.backend.manifests import get_status, iso_now, valid_manifests
from app.llm import prompts
from app.llm.client import LLMClient, ParseError
from app.workers import citations, reconcile, reviews
from app.workers import enrichment_artifact as art
from app.workers import labels
from app.workers.wiki_render import NODE_DIR, parse_frontmatter, render_item_page, title_from_filename

_ENRICHABLE_STATUSES = {"extracted", "partial"}
_WS = re.compile(r"\s+")
_TITLE_RE = re.compile(r'(?m)^title:\s*"(.*)"\s*$')

# Defensive bounds against adversarial/oversized LLM output (a source is untrusted data).
_MAX_NAME, _MAX_ALIAS, _MAX_ALIASES, _MAX_ITEMS = 200, 120, 16, 200


def _normalize_name(name: str) -> str:
    return _WS.sub(" ", name).strip().lower()


def _name_hash(name: str) -> str:
    return hashlib.sha256(_normalize_name(name).encode("utf-8")).hexdigest()[:16]


def node_id(name: str) -> str:
    """The type-neutral item id (ADR-0059): classification is metadata, never identity.

    Minted from the creation-time normalized canonical name and frozen thereafter (renames
    never rehash, ADR-0021); same-name-different-referent is resolved by `split_item`, never
    by type."""
    return f"itm_{_name_hash(name)}"


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", _normalize_name(name)).strip("-")
    return s or "unnamed"


def _clean_aliases(aliases: Any) -> list[str]:
    out: list[str] = []
    if isinstance(aliases, list):
        for a in aliases:
            if isinstance(a, str):
                cleaned = _WS.sub(" ", a).strip()[:_MAX_ALIAS]
                if cleaned and cleaned not in out:
                    out.append(cleaned)
    return out[:_MAX_ALIASES]


def _union(existing: list[str], new: list[str]) -> list[str]:
    """Additive alias union (never auto-removes); identity-changing merges are review-gated."""
    out: list[str] = []
    for x in list(existing) + list(new):
        if x and x not in out:
            out.append(x)
    return out[:_MAX_ALIASES]


def _read_node_meta(page_path: Path) -> dict[str, Any] | None:
    """Read the durable title + aliases + item_type from an existing node page (page is the authority)."""
    if not page_path.exists():
        return None
    text = page_path.read_text(encoding="utf-8", errors="replace")
    m = _TITLE_RE.search(text)
    if not m:
        return None
    fm = parse_frontmatter(text)
    aliases = fm.get("aliases")
    return {"title": re.sub(r"\\(.)", r"\1", m.group(1)),
            "aliases": aliases if isinstance(aliases, list) else [],
            "status": fm.get("status"),
            "review_status": fm.get("review_status"),
            "confidence": fm.get("confidence", "low"),
            # ADR-0059: the page owns the governed classification; recompose preserves it.
            "item_type": fm.get("item_type"),
            # ADR-0052: preserve a spin-off's split lineage across re-renders (page-authoritative).
            "split_from": fm.get("split_from"),
            "split_review_id": fm.get("split_review_id"),
            # ADR-0058: preserve the page-owned human description across re-renders.
            "description": fm.get("description")}


def _rebuild_index(root: Path) -> bool:
    script = root / "scripts" / "rebuild_index.py"
    if not script.exists():
        return False
    return subprocess.run([sys.executable, str(script), str(root)]).returncode == 0


def _link_labels(wiki_dir: Path, source_ids, duplicates) -> dict[str, str]:
    """ADR-0060: page-local display labels for an item page's Mentioned-By and Duplicates
    links (worker-side IO; every item-page writer routes through this so the renderer stays pure)."""
    targets = [f"Sources/{s}" for s in source_ids or []]
    targets += [NODE_DIR[d["node_type"]] + "/" + d["slug"] for d in duplicates or []]
    return labels.display_labels(wiki_dir, targets)


def _recompose_node(gconn, *, node_id, wiki_dir, reviews_dir, now, text_hint=None) -> str:
    """Render a node's stub page from its active mentions (tombstone if none).

    The existing page is the authority for the node's title, aliases, and item_type
    (ADR-0030/0059): the title and classification are preserved across re-extractions, and
    aliases are an additive union of what's on the page and what this run found — so a later
    source cannot overwrite or drop aliases gathered from another (B1/Q4). With no active
    mentions the page is tombstoned (deprecated_candidate) and a `deprecate_wiki_page`
    review item is filed (B4). Either status flip also reconciles the node's unresolved
    review items (ADR-0057): tombstoning withdraws a pending promote, resurrection withdraws
    the recompose-filed deprecation.
    """
    node = graph.get_node(gconn, node_id)
    if node is None:
        return "skipped"
    node_type, slug = node["node_type"], node["slug"]
    page_dir = wiki_dir / NODE_DIR[node_type]
    page_path = page_dir / f"{slug}.md"
    existing = _read_node_meta(page_path)
    new_title = text_hint["title"] if text_hint else None
    new_aliases = text_hint["aliases"] if text_hint else []
    title = (existing["title"] if existing else None) or new_title  # page title is authority
    if title is None:
        return "skipped"
    aliases = _union(existing["aliases"] if existing else [], new_aliases)
    # Classification authority: existing page > graph mirror > this run's hint. A node whose
    # page and mirror both lack a type (drifted state) cannot render — skip, never guess.
    item_type = ((existing or {}).get("item_type") or node.get("item_type")
                 or (text_hint or {}).get("item_type"))
    if not taxonomy.is_item_type(item_type):
        return "skipped"

    sources = graph.sources_for_node(gconn, node_id)
    # Status is page-authoritative: a promotion to `active` is preserved across
    # re-extraction; otherwise candidate while mentioned, deprecated_candidate when not.
    if not sources:
        status = "deprecated_candidate"
    elif (existing or {}).get("status") == "active":
        status = "active"
    else:
        status = "candidate"
    page_dir.mkdir(parents=True, exist_ok=True)
    duplicates = graph.active_duplicates(gconn, node_id)
    page_path.write_text(render_item_page({
        "node_id": node_id, "item_type": item_type,
        "title": title, "aliases": aliases, "confidence": "low", "source_ids": sources,
        "status": status, "duplicates": duplicates,
        "split_from": (existing or {}).get("split_from"),               # ADR-0052: preserve spin-off lineage
        "split_review_id": (existing or {}).get("split_review_id"),
        "description": (existing or {}).get("description"),            # ADR-0058: preserve human description
    }, labels=_link_labels(wiki_dir, sources, duplicates)), encoding="utf-8")
    graph.upsert_node(gconn, node_id=node_id, node_type=node_type, slug=slug,
                      status=status, item_type=item_type, now=now)
    page_rel = f"{NODE_DIR[node_type]}/{slug}.md"
    if not sources:
        reviews.create_review_item(
            reviews_dir, review_type="deprecate_wiki_page",
            subject={"node_id": node_id, "page": page_rel},
            proposal={"to_status": "deprecated_candidate",
                      "reason": reconcile.LEGACY_NO_ACTIVE_MENTIONS_REASON,
                      "reason_code": reconcile.REASON_CODE_NO_ACTIVE_MENTIONS},
            context={"node_type": node_type}, now=now)
    # ADR-0057: the status flip may invalidate an unresolved item's premise in either direction
    # (tombstone -> stale pending promote; resurrection -> stale recompose-filed deprecation).
    reconcile.reconcile_node_items(
        reviews_dir, node_id=node_id, page=page_rel,
        node_status=status, active_source_count=len(sources), now=now)
    return "tombstoned" if not sources else "written"


def recompose_semantic_node_page(
    gconn, *, node_id: str, wiki_dir: Path, status: str, review_status: str, now: str | None = None,
) -> str:
    """Re-render an item page at an EXPLICIT status + review_status (ADR-0035 A5).

    The Phase-6 deprecation executor's render seam for knowledge items (all flow through
    `render_item_page`/`NODE_DIR`). Reloads the node's display metadata (title/aliases/
    item_type) from the existing page — the page is the authority (ADR-0030) — and its active
    `mentions` sources from the graph, preserving citations/evidence and the summary callout, then
    re-renders with the **explicit** status (never re-derived from mentions, so a still-mentioned node
    cannot resurrect out of a deprecation) and mirrors the graph node status. Idempotent and
    deterministic. Returns "written", else a typed skip reason
    ("node_missing"/"unsupported_node_type"/"page_missing")."""
    node = graph.get_node(gconn, node_id)
    if node is None:
        return "node_missing"
    node_type, slug = node["node_type"], node["slug"]
    if node_type != "item":
        return "unsupported_node_type"
    page_path = wiki_dir / NODE_DIR[node_type] / f"{slug}.md"
    meta = _read_node_meta(page_path)
    if meta is None:
        return "page_missing"
    item_type = meta.get("item_type") or node.get("item_type")
    if not taxonomy.is_item_type(item_type):
        return "page_missing"
    sources = graph.sources_for_node(gconn, node_id)
    duplicates = graph.active_duplicates(gconn, node_id)
    rendered = render_item_page({
        "node_id": node_id, "item_type": item_type,
        "title": meta["title"], "aliases": meta["aliases"], "confidence": meta["confidence"],
        "source_ids": sources, "status": status,
        "duplicates": duplicates,
        "split_from": meta.get("split_from"),                           # ADR-0052: preserve spin-off lineage
        "split_review_id": meta.get("split_review_id"),
        "description": meta.get("description"),                        # ADR-0058: preserve human description
    }, review_status=review_status, labels=_link_labels(wiki_dir, sources, duplicates))
    # Write only when content differs (avoid churn; ADR-0041). confidence is page-owned, preserved.
    # Returns "written" only when the page actually changed, "unchanged" otherwise (the graph node-status
    # mirror always runs, idempotently). Callers that gate on `== "written"` get an honest signal.
    changed = page_path.read_text(encoding="utf-8") != rendered
    if changed:
        page_path.write_text(rendered, encoding="utf-8")
    graph.upsert_node(gconn, node_id=node_id, node_type=node_type, slug=slug, status=status,
                      item_type=item_type, now=now or iso_now())
    return "written" if changed else "unchanged"


def extract_items(
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
    input_max_chars: int = 300000,
    rebuild_index: bool = True,
    record_job: bool = True,
) -> dict[str, Any]:
    """Extract candidate knowledge items for pending (or selected) sources; return a summary.

    ADR-0056 (carried over): one full-document call per source up to ``input_max_chars``
    (the `full-doc-v1` strategy ref component); an above-cap document is truncated and
    marked ``coverage: truncated`` in its artifact and the job metadata."""
    root = Path(root).resolve()
    manifests_dir = Path(manifests_dir) if manifests_dir else root / "raw" / "manifests"
    jobs_db = Path(jobs_db) if jobs_db else root / "db" / "jobs.sqlite"
    graph_db = Path(graph_db) if graph_db else root / "db" / "graph.sqlite"
    markdown_dir = Path(markdown_dir) if markdown_dir else root / "normalized" / "markdown"
    enrichment_dir = Path(enrichment_dir) if enrichment_dir else root / "normalized" / "enrichment"
    wiki_dir = Path(wiki_dir) if wiki_dir else root / "wiki"
    reviews_dir = Path(reviews_dir) if reviews_dir else root / "reviews"

    now = iso_now()
    job_id = f"job_{uuid.uuid4().hex[:16]}"
    conn = None
    if record_job:
        db.init_db(jobs_db)
        conn = db.connect(jobs_db)
        db.insert_job(conn, job_id=job_id, job_type="extract_items", status="running",
                      created_at=now, started_at=now)

    graph.init_db(graph_db)
    gconn = graph.connect(graph_db)
    # ADR-0063: resolve the tier's ordered chain once per run to the first available concrete
    # model_ref (availability-only); keep the first-preference ref when none is available so the
    # no-key/stub fingerprint stays a valid concrete ref. `chain_refs` drives sticky freshness.
    chain_refs = [c.strip() for c in model_ref.split(",") if c.strip()]
    model_ref, has_key = client.resolve_run_model(model_ref)

    strategy_ref = art.items_strategy_ref(input_max_chars)
    considered = nodes_written = mentions_written = 0
    skipped_fresh = skipped_not_extracted = skipped_empty = skipped_no_key = 0
    topic_starved_sources: list[str] = []
    unclassified_count = 0
    coverage_truncated_sources: list[str] = []
    errors: list[dict[str, str]] = []
    texts: dict[str, dict[str, Any]] = {}
    touched: set[str] = set()
    affected: set[str] = set()

    def _emit(sid, md, item_type, name, aliases, source_nodes, seen):
        nonlocal mentions_written, unclassified_count
        name = _WS.sub(" ", str(name)).strip()[:_MAX_NAME]
        if not name:
            return
        clean_aliases = _clean_aliases(aliases)

        # One node per canonical referent (ADR-0059 decision 3): the type-neutral id makes the
        # probe a single lookup. When the model classifies an existing name under a DIFFERENT
        # type, the mention routes to the existing node (its page keeps its item_type — the
        # authority) and a `change_item_type` review is filed; nothing auto-retypes. The
        # sentinel never proposes a retype (an unsure classification is not a correction).
        nid = node_id(name)
        existing = graph.get_node(gconn, nid)
        used_type = (existing.get("item_type") if existing else None) or item_type
        type_conflict = (existing is not None and existing.get("item_type") is not None
                         and item_type != existing["item_type"]
                         and taxonomy.is_production_item_type(item_type))
        if nid in seen:
            return
        seen.add(nid)
        if used_type == taxonomy.UNCLASSIFIED:
            unclassified_count += 1

        slug = _slug(name)
        graph.upsert_node(gconn, node_id=nid, node_type="item", slug=slug, status="candidate",
                          item_type=used_type, now=now)
        # Optional evidence anchor when the name is mechanically locatable (Q2).
        span = citations.locate_quote(md, name)
        anchor = {"evidence_source_id": sid, "evidence_char_start": span[0],
                  "evidence_char_end": span[1]} if span else {}
        graph.upsert_assertion(gconn, src_id=sid, dst_id=nid, edge_type="mentions",
                               asserted_by="llm", status="active", job_id=job_id, now=now, **anchor)
        texts[nid] = {"title": name, "aliases": clean_aliases, "item_type": used_type}
        touched.add(nid)
        mentions_written += 1
        source_nodes.append({"node_id": nid, "item_type": used_type, "name": name,
                             "aliases": clean_aliases})

        if type_conflict:
            # Subject keys on the proposed classification change, not just the node (ADR-0051
            # precedent): a rejected retype to one type must not lock out retyping to another.
            reviews.create_review_item(
                reviews_dir, review_type="change_item_type",
                subject={"node_id": nid, "to_item_type": item_type},
                proposal={"to_item_type": item_type},
                context={"source_id": sid, "name": name,
                         "from_item_type": existing["item_type"]}, now=now)
        # The candidate's promotion is the review-gated semantic act (recurrence may
        # auto-resolve it — except for the sentinel, promote-side gated). Idempotent per node id.
        reviews.create_review_item(
            reviews_dir, review_type="promote_candidate_node",
            subject={"node_id": nid},
            proposal={"to_status": "active", "name": name, "item_type": used_type},
            now=now)

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
            apath = art.items_artifact_path(enrichment_dir, sid)
            if not force and apath.exists():
                try:
                    existing = json.loads(apath.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    existing = {}
                # ADR-0063 sticky-to-chain: fresh iff the recorded model is still a chain member and
                # its own-model fingerprint matches — an availability flip alone never restales.
                if art.chain_fresh(existing, chain_refs,
                                   lambda m: art.items_fingerprint(md, m, strategy_ref)):
                    skipped_fresh += 1
                    continue
            fingerprint = art.items_fingerprint(md, model_ref, strategy_ref)

            # ADR-0055 rollout safety: supersede a source's prior mentions ONLY once this run can
            # produce the replacement state — after a successful parse (below), or deterministically
            # for an empty source. A no-key or failed-parse run must leave the existing topic layer
            # untouched: supersede-then-skip would recompose affected nodes with zero mentions and
            # tombstone them (the wipe hazard a prompt-version bump arms vault-wide).
            if not has_key:
                skipped_no_key += 1
                continue
            if not md.strip():
                affected.update(graph.supersede_mentions_for_source(gconn, sid, now=now))
                skipped_empty += 1
                _write_artifact(apath, sid, fingerprint, [], model_ref, now, strategy_ref)
                continue

            # ADR-0056: one full-document call up to the input cap; above-cap documents are
            # truncated and marked (coverage honesty — the head-bias returns only for
            # pathological inputs, and visibly).
            coverage = "truncated" if len(md) > input_max_chars else "full"
            if coverage == "truncated":
                coverage_truncated_sources.append(sid)
            title = title_from_filename(manifest.get("original_filename", sid))
            try:
                result = client.parse(
                    prompts.build_items_messages(title, md, max_chars=input_max_chars),
                    prompts.ITEMS_SCHEMA, model_ref,
                    schema_version=art.ITEMS_SCHEMA_VERSION,
                    prompt_version=art.ITEMS_PROMPT_VERSION,
                    strategy_ref=strategy_ref,
                )
            except ParseError as exc:
                errors.append({"source_id": sid, "error": str(exc)})
                continue

            # Parse succeeded: retire the old mentions immediately before emitting replacements
            # (supersede covers ALL active mentions for the source, so it must precede _emit).
            affected.update(graph.supersede_mentions_for_source(gconn, sid, now=now))

            graph.upsert_node(
                gconn, node_id=sid, node_type="source", slug=sid,
                status=get_status(manifest), now=now)
            source_nodes: list[dict[str, Any]] = []
            seen: set[str] = set()
            for it in result["items"][:_MAX_ITEMS]:
                itype = it.get("item_type", "")
                if not taxonomy.is_item_type(itype):
                    itype = taxonomy.UNCLASSIFIED
                _emit(sid, md, itype, it.get("name", ""), it.get("aliases", []), source_nodes, seen)

            _write_artifact(apath, sid, fingerprint, source_nodes, model_ref, now,
                            strategy_ref, coverage)
            nodes_written += len(source_nodes)
            # ADR-0059: immediate operator feedback for the starvation failure signature (no
            # thematic topic layer from a substantive source). Artifact/claim state only —
            # never text-shape inference.
            if art.topic_starved(source_nodes, art.stored_claim_count(enrichment_dir, sid)):
                topic_starved_sources.append(sid)

        pages_written = pages_tombstoned = 0
        for nid in touched | affected:
            outcome = _recompose_node(gconn, node_id=nid, wiki_dir=wiki_dir,
                                      reviews_dir=reviews_dir, now=now, text_hint=texts.get(nid))
            pages_written += outcome == "written"
            pages_tombstoned += outcome == "tombstoned"

        index_rebuilt = _rebuild_index(root) if (rebuild_index and (touched or affected)) else False

        if errors:
            status = "partial"
        elif not has_key and considered > 0:
            status = "skipped"
        else:
            status = "succeeded"

        summary: dict[str, Any] = {
            "job_id": job_id, "model_ref": model_ref, "status": status,
            "sources_considered": considered, "nodes_written": nodes_written,
            "mentions_written": mentions_written, "node_pages_written": pages_written,
            "node_pages_tombstoned": pages_tombstoned, "skipped_fresh": skipped_fresh,
            "skipped_not_extracted": skipped_not_extracted, "skipped_empty": skipped_empty,
            "skipped_no_key": skipped_no_key, "manifests_skipped_invalid": len(skipped_invalid),
            "topic_starved": len(topic_starved_sources),
            "topic_starved_sources": sorted(topic_starved_sources),
            "unclassified_items": unclassified_count,
            "coverage_truncated": len(coverage_truncated_sources),
            "coverage_truncated_sources": sorted(coverage_truncated_sources),
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


def _write_artifact(apath, sid, fingerprint, source_nodes, model_ref, now,
                    strategy_ref=None, coverage="full"):
    apath.parent.mkdir(parents=True, exist_ok=True)
    apath.write_text(json.dumps({
        "source_id": sid, "schema_version": art.ITEMS_SCHEMA_VERSION,
        "prompt_version": art.ITEMS_PROMPT_VERSION, "model_ref": model_ref,
        "strategy_ref": strategy_ref,
        # ADR-0056 honesty marker: "truncated" when the document exceeded the input cap, so
        # "document-complete" stays auditable (a future lint can consume this).
        "coverage": coverage,
        "input_fingerprint": fingerprint, "generation_status": "enriched",
        "generated_at": now, "nodes": source_nodes,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
