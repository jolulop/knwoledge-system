#!/usr/bin/env python3
"""Phase 3.5b concept/entity extraction worker (slice 4, ADR-0017/0018/0021/0030).

Tier-2 LLM pass: for each extracted/partial source, identify the durable **concepts** it is
about and the named **entities** it mentions (each entity classified `entity | person |
organization | project`). Each becomes a typed node — id and page directory selected by type
(`cpt_/ent_/per_/org_/prj_`, ADR-0021) — created `candidate` (ADR-0018), with an `active`
`mentions` edge (source → node) recording provenance. Concepts/entities are interpretive
labels, **not verbatim-grounded** like claims (an optional evidence anchor is stored only
when the name is mechanically locatable); quality comes from ≥2-source promotion (slice 5).

Pages are deterministic stubs rendered from the graph (Mentioned-by = active incoming
mentions); the page frontmatter is the durable authority for the node's title/aliases. On
re-extraction a source's prior mentions are **superseded first** (before the can-extract
gates), and affected node pages are recomposed from surviving `active` mentions (tombstoned
if none). Source pages are assumed to exist (run `generate_wiki` first). Same operational
shape as the claim worker.
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
from app.backend.manifests import get_status, iso_now, valid_manifests
from app.llm import prompts
from app.llm.client import LLMClient, ParseError
from app.workers import citations, reviews
from app.workers import enrichment_artifact as art
from app.workers.wiki_render import NODE_DIR, parse_frontmatter, render_concept_page, title_from_filename

_ENRICHABLE_STATUSES = {"extracted", "partial"}
_WS = re.compile(r"\s+")
_TITLE_RE = re.compile(r'(?m)^title:\s*"(.*)"\s*$')

# Type -> id prefix and frontmatter id field (ADR-0021); page directory is NODE_DIR.
_TYPE_PREFIX = {"concept": "cpt", "entity": "ent", "person": "per",
                "organization": "org", "project": "prj"}
# The entity family, within which `change_entity_subtype` re-keys (ADR-0051); concept is a separate family.
_ENTITY_FAMILY = frozenset({"entity", "person", "organization", "project"})
ID_FIELD = {"concept": "concept_id", "entity": "entity_id", "person": "person_id",
            "organization": "organization_id", "project": "project_id"}

# Defensive bounds against adversarial/oversized LLM output (a source is untrusted data).
_MAX_NAME, _MAX_ALIAS, _MAX_ALIASES, _MAX_ITEMS = 200, 120, 16, 200


def _normalize_name(name: str) -> str:
    return _WS.sub(" ", name).strip().lower()


def _name_hash(name: str) -> str:
    return hashlib.sha256(_normalize_name(name).encode("utf-8")).hexdigest()[:16]


def node_id(node_type: str, name: str) -> str:
    return f"{_TYPE_PREFIX[node_type]}_{_name_hash(name)}"


def _candidate_ids(name: str) -> list[str]:
    """Every possible node id for this name, across type prefixes (subtype-conflict probe)."""
    h = _name_hash(name)
    return [f"{prefix}_{h}" for prefix in _TYPE_PREFIX.values()]


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
    """Read the durable title + aliases from an existing node page (page is the authority)."""
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
            # ADR-0052: preserve a spin-off's split lineage across re-renders (page-authoritative).
            "split_from": fm.get("split_from"),
            "split_review_id": fm.get("split_review_id")}


def _rebuild_index(root: Path) -> bool:
    script = root / "scripts" / "rebuild_index.py"
    if not script.exists():
        return False
    return subprocess.run([sys.executable, str(script), str(root)]).returncode == 0


def _recompose_node(gconn, *, node_id, wiki_dir, reviews_dir, now, text_hint=None) -> str:
    """Render a node's stub page from its active mentions (tombstone if none).

    The existing page is the authority for the node's title and aliases (ADR-0030): the
    title is preserved across re-extractions, and aliases are an additive union of what's on
    the page and what this run found — so a later source cannot overwrite or drop aliases
    gathered from another (B1/Q4). With no active mentions the page is tombstoned
    (deprecated_candidate) and a `deprecate_wiki_page` review item is filed (B4).
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

    sources = graph.sources_for_node(gconn, node_id)
    # Status is page-authoritative: a promotion to `active` (slice 5) is preserved across
    # re-extraction; otherwise candidate while mentioned, deprecated_candidate when not.
    if not sources:
        status = "deprecated_candidate"
    elif (existing or {}).get("status") == "active":
        status = "active"
    else:
        status = "candidate"
    page_dir.mkdir(parents=True, exist_ok=True)
    page_path.write_text(render_concept_page({
        "node_type": node_type, "node_id": node_id, "id_field": ID_FIELD[node_type],
        "title": title, "aliases": aliases, "confidence": "low", "source_ids": sources,
        "status": status, "duplicates": graph.active_duplicates(gconn, node_id),
        "split_from": (existing or {}).get("split_from"),               # ADR-0052: preserve spin-off lineage
        "split_review_id": (existing or {}).get("split_review_id"),
    }), encoding="utf-8")
    graph.upsert_node(gconn, node_id=node_id, node_type=node_type, slug=slug,
                      status=status, now=now)
    if not sources:
        reviews.create_review_item(
            reviews_dir, review_type="deprecate_wiki_page",
            subject={"node_id": node_id, "page": f"{NODE_DIR[node_type]}/{slug}.md"},
            proposal={"to_status": "deprecated_candidate",
                      "reason": "no active source mentions remain"},
            context={"node_type": node_type}, now=now)
        return "tombstoned"
    return "written"


def recompose_semantic_node_page(
    gconn, *, node_id: str, wiki_dir: Path, status: str, review_status: str, now: str | None = None,
) -> str:
    """Re-render a concept/entity-family page at an EXPLICIT status + review_status (ADR-0035 A5).

    The Phase-6 deprecation executor's render seam for `concept/entity/person/organization/project`
    (all flow through `render_concept_page`/`NODE_DIR`). Reloads the node's display metadata
    (title/aliases) from the existing page — the page is the authority (ADR-0030) — and its active
    `mentions` sources from the graph, preserving citations/evidence and the summary callout, then
    re-renders with the **explicit** status (never re-derived from mentions, so a still-mentioned node
    cannot resurrect out of a deprecation) and mirrors the graph node status. Idempotent and
    deterministic. Returns "written", else a typed skip reason
    ("node_missing"/"unsupported_node_type"/"page_missing")."""
    node = graph.get_node(gconn, node_id)
    if node is None:
        return "node_missing"
    node_type, slug = node["node_type"], node["slug"]
    if node_type not in ID_FIELD:
        return "unsupported_node_type"
    page_path = wiki_dir / NODE_DIR[node_type] / f"{slug}.md"
    meta = _read_node_meta(page_path)
    if meta is None:
        return "page_missing"
    sources = graph.sources_for_node(gconn, node_id)
    rendered = render_concept_page({
        "node_type": node_type, "node_id": node_id, "id_field": ID_FIELD[node_type],
        "title": meta["title"], "aliases": meta["aliases"], "confidence": meta["confidence"],
        "source_ids": sources, "status": status,
        "duplicates": graph.active_duplicates(gconn, node_id),
        "split_from": meta.get("split_from"),                           # ADR-0052: preserve spin-off lineage
        "split_review_id": meta.get("split_review_id"),
    }, review_status=review_status)
    # Write only when content differs (avoid churn; ADR-0041). confidence is page-owned, preserved.
    # Returns "written" only when the page actually changed, "unchanged" otherwise (the graph node-status
    # mirror always runs, idempotently). Callers that gate on `== "written"` get an honest signal.
    changed = page_path.read_text(encoding="utf-8") != rendered
    if changed:
        page_path.write_text(rendered, encoding="utf-8")
    graph.upsert_node(gconn, node_id=node_id, node_type=node_type, slug=slug, status=status,
                      now=now or iso_now())
    return "written" if changed else "unchanged"


def extract_concepts(
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
    """Extract candidate concepts/entities for pending (or selected) sources; return a summary."""
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
        db.insert_job(conn, job_id=job_id, job_type="extract_concepts", status="running",
                      created_at=now, started_at=now)

    graph.init_db(graph_db)
    gconn = graph.connect(graph_db)
    has_key = client.provider_available(model_ref)

    considered = nodes_written = mentions_written = 0
    skipped_fresh = skipped_not_extracted = skipped_empty = skipped_no_key = 0
    concept_starved_sources: list[str] = []
    errors: list[dict[str, str]] = []
    texts: dict[str, dict[str, Any]] = {}
    touched: set[str] = set()
    affected: set[str] = set()

    def _emit(sid, md, node_type, name, aliases, source_nodes, seen):
        nonlocal mentions_written
        name = _WS.sub(" ", str(name)).strip()[:_MAX_NAME]
        if not name:
            return
        clean_aliases = _clean_aliases(aliases)

        # Subtype conflict (B2): a node for this name already exists under a different type.
        # Keep the existing node, route the mention to it, and propose the subtype change for
        # review (re-keying the id is gated like a merge — ADR-0021); never mint a 2nd node.
        existing = graph.find_node_by_candidate_ids(gconn, _candidate_ids(name))
        if existing is not None and existing["node_type"] != node_type:
            nid, used_type = existing["node_id"], existing["node_type"]
            subtype_conflict = (used_type, node_type)
        else:
            used_type, nid = node_type, node_id(node_type, name)
            subtype_conflict = None
        if nid in seen:
            return
        seen.add(nid)

        slug = _slug(name)
        graph.upsert_node(gconn, node_id=nid, node_type=used_type, slug=slug, status="candidate", now=now)
        # Optional evidence anchor when the name is mechanically locatable (Q2).
        span = citations.locate_quote(md, name)
        anchor = {"evidence_source_id": sid, "evidence_char_start": span[0],
                  "evidence_char_end": span[1]} if span else {}
        graph.upsert_assertion(gconn, src_id=sid, dst_id=nid, edge_type="mentions",
                               asserted_by="llm", status="active", job_id=job_id, now=now, **anchor)
        texts[nid] = {"title": name, "aliases": clean_aliases, "node_type": used_type}
        touched.add(nid)
        mentions_written += 1
        source_nodes.append({"node_id": nid, "node_type": used_type, "name": name,
                             "aliases": clean_aliases})

        # ADR-0051: file a rekey review ONLY for an ENTITY-FAMILY subtype conflict (both sides in
        # entity/person/organization/project) — its executor contract is subject {node_id, to_type} (so the
        # review_id keys on the proposed identity change, not just the node: a rejected retype to one subtype
        # must not lock out retyping to another) + proposal {to_type}; `from` type + display name are
        # informational context. A concept<->entity conflict is a cross-family *type* change (a future
        # `change_node_type`), so it is WITHHELD — filing an always-skipped review would be misleading.
        if subtype_conflict is not None and set(subtype_conflict) <= _ENTITY_FAMILY:
            reviews.create_review_item(
                reviews_dir, review_type="change_entity_subtype",
                subject={"node_id": nid, "to_type": subtype_conflict[1]},
                proposal={"to_type": subtype_conflict[1]},
                context={"source_id": sid, "name": name, "from_type": subtype_conflict[0]}, now=now)
        # B3: the candidate's promotion is the review-gated semantic act (recurrence may
        # auto-resolve it in slice 5). Idempotent per node id.
        reviews.create_review_item(
            reviews_dir, review_type="promote_candidate_node",
            subject={"node_id": nid},
            proposal={"to_status": "active", "name": name, "node_type": used_type},
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
            fingerprint = art.concepts_fingerprint(md, model_ref)
            apath = art.concepts_artifact_path(enrichment_dir, sid)
            if not force and apath.exists():
                try:
                    fresh = json.loads(apath.read_text(encoding="utf-8")).get("input_fingerprint") == fingerprint
                except (OSError, json.JSONDecodeError):
                    fresh = False
                if fresh:
                    skipped_fresh += 1
                    continue

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
                _write_artifact(apath, sid, fingerprint, [], model_ref, now)
                continue

            title = title_from_filename(manifest.get("original_filename", sid))
            try:
                result = client.parse(
                    prompts.build_concept_messages(title, md), prompts.CONCEPTS_SCHEMA, model_ref,
                    schema_version=art.CONCEPT_SCHEMA_VERSION, prompt_version=art.CONCEPT_PROMPT_VERSION,
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
            for c in result["concepts"][:_MAX_ITEMS]:
                _emit(sid, md, "concept", c.get("name", ""), c.get("aliases", []), source_nodes, seen)
            for e in result["entities"][:_MAX_ITEMS]:
                etype = e.get("entity_type", "entity")
                if etype not in _TYPE_PREFIX:
                    etype = "entity"
                _emit(sid, md, etype, e.get("name", ""), e.get("aliases", []), source_nodes, seen)

            _write_artifact(apath, sid, fingerprint, source_nodes, model_ref, now)
            nodes_written += len(source_nodes)
            # ADR-0055: immediate operator feedback for the F1 failure signature (zero concepts
            # from a substantive source). Artifact/claim state only — never text-shape inference.
            if art.concept_starved(source_nodes, art.stored_claim_count(enrichment_dir, sid)):
                concept_starved_sources.append(sid)

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
            "concept_starved": len(concept_starved_sources),
            "concept_starved_sources": sorted(concept_starved_sources),
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


def _write_artifact(apath, sid, fingerprint, source_nodes, model_ref, now):
    apath.parent.mkdir(parents=True, exist_ok=True)
    apath.write_text(json.dumps({
        "source_id": sid, "schema_version": art.CONCEPT_SCHEMA_VERSION,
        "prompt_version": art.CONCEPT_PROMPT_VERSION, "model_ref": model_ref,
        "input_fingerprint": fingerprint, "generation_status": "enriched",
        "generated_at": now, "nodes": source_nodes,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
