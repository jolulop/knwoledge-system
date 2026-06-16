#!/usr/bin/env python3
"""Phase 3.5c slice 2 cross-source synthesis (ADR-0031).

Tier-3 LLM pass that writes one **candidate** synthesis per `active` concept/entity that is
evidenced by ≥2 `active` claims (each with grounded citations) from ≥2 *independent* sources
(ADR-0018; the `claim → source → mentions → concept` neighborhood). The synthesis is grounded on
the claim **nodes**: the page's Supporting-Evidence links and `derived_from` (synthesis → claim)
edges are the graph authority, the LLM supplies only the prose, and the artifact
(`normalized/enrichment/<topic_node_id>.synthesis.json`) is the record of that prose.

Governance (ADR-0031 §7; review-only promotion, **no recurrence**):
- A synthesis is born `status: candidate` with a `propose_synthesis` review whose id is
  **fingerprint-scoped** (`{topic_node_id, fingerprint}`) so each distinct evidence set is a
  distinct, re-fileable decision.
- The normal pass **never rewrites a reviewed synthesis**: an `active` (approved) synthesis stays
  active (a fingerprint change is surfaced as `stale_active`, regenerated only with `--force`),
  and a synthesis whose *current evidence* was already rejected is left alone (no re-nag). Only
  changed evidence (a new fingerprint), with `--force` for an approved one, re-opens it.
- Topics that drop below the threshold are **retracted** through the audited deprecation path
  (`deprecate_wiki_page` filed, page re-rendered `deprecated_candidate` with a coherent
  `review_status`, pending proposals withdrawn) — the same governance the claim/concept tombstones
  use.

No API key → a `skipped` job, but resolution + retraction still run. v1 scope (ADR-0031 §6
permits both): the prose stands on claim nodes and emits **no direct source quotes** — a guard
rejects output that copies a long verbatim run from a contributing source; the concept→synthesis
backlink is a `related_to` edge, not yet projected on the concept page.
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
from app.backend.manifests import get_provenance, independent_sources, iso_now, list_manifests
from app.llm import prompts
from app.llm.client import LLMClient, ParseError
from app.workers import enrichment_artifact as art
from app.workers import reviews
from app.workers.wiki_render import NODE_DIR, parse_frontmatter, render_synthesis_page

_PROMOTABLE = ("concept", "entity", "person", "organization", "project")
_CLAIM_TEXT_RE = re.compile(r'(?m)^claim_text:\s*"(.*)"\s*$')
_TITLE_RE = re.compile(r'(?m)^title:\s*"(.*)"\s*$')
_WS = re.compile(r"\s+")
_VERBATIM_WINDOW = 12  # consecutive-word run that counts as a copied source quote (guard)


def synthesis_id(topic_node_id: str) -> str:
    """Deterministic, one-per-topic synthesis id frozen at creation (ADR-0021)."""
    return f"syn_{hashlib.sha256(topic_node_id.encode('utf-8')).hexdigest()[:16]}"


def _review_subject(topic_node_id: str, fingerprint: str) -> dict[str, str]:
    """Fingerprint-scoped review identity, so each distinct evidence set is re-fileable."""
    return {"topic_node_id": topic_node_id, "fingerprint": fingerprint}


def _rebuild_index(root: Path) -> bool:
    script = root / "scripts" / "rebuild_index.py"
    if not script.exists():
        return False
    return subprocess.run([sys.executable, str(script), str(root)]).returncode == 0


def _read_fm_title(page_path: Path) -> str | None:
    if not page_path.exists():
        return None
    m = _TITLE_RE.search(page_path.read_text(encoding="utf-8", errors="replace"))
    return re.sub(r"\\(.)", r"\1", m.group(1)) if m else None


def _claim_context(gconn, cid: str, *, claims_dir: Path, markdown_dir: Path) -> dict[str, Any] | None:
    """A contributing claim's durable text + citations reconstructed from its active edges; None
    if its page/wording is missing. A claim with no citations is not usable (caller drops it)."""
    page = claims_dir / f"{cid}.md"
    if not page.exists():
        return None
    m = _CLAIM_TEXT_RE.search(page.read_text(encoding="utf-8", errors="replace"))
    if not m:
        return None
    claim_text = re.sub(r"\\(.)", r"\1", m.group(1))
    cites: list[dict[str, Any]] = []
    for e in graph.outgoing_active(gconn, cid):
        if e["edge_type"] != "derived_from":
            continue
        src, start, end = e["dst_id"], e["evidence_char_start"], e["evidence_char_end"]
        md_path = markdown_dir / f"{src}.md"
        md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
        quote = md[start:end] if start is not None and end is not None and end <= len(md) else ""
        cites.append({"source_id": src, "char_start": start, "char_end": end, "quote": quote})
    cites.sort(key=lambda c: (c["source_id"], c["char_start"] if c["char_start"] is not None else -1))
    return {"claim_id": cid, "claim_text": claim_text, "citations": cites, "sources": [c["source_id"] for c in cites]}


def _disagreement_pairs(gconn, claim_ids: set[str]) -> list[tuple[str, str]]:
    """Active `contradicts` pairs (sorted, deduped) among a set of contributing claims."""
    pairs: set[tuple[str, str]] = set()
    for cid in claim_ids:
        for other in graph.active_contradictions_for_claim(gconn, cid):
            if other in claim_ids:
                pairs.add((min(cid, other), max(cid, other)))
    return sorted(pairs)


def _has_independent_pair(sources: set[str], prov: dict[str, dict[str, Any]]) -> bool:
    s = sorted(sources)
    return any(independent_sources(prov.get(s[i], {}), prov.get(s[j], {}))
               for i in range(len(s)) for j in range(i + 1, len(s)))


def eligible_topics(gconn, prov, *, claims_dir: Path, markdown_dir: Path) -> list[dict[str, Any]]:
    """Active concept/entity topics with **≥2 grounded active claims from ≥2 independent
    sources**, checked over the *surviving* claim contexts — a claim whose page/citations are
    missing is dropped, then the trigger is re-verified (ADR-0031)."""
    topics: list[dict[str, Any]] = []
    for node_type in _PROMOTABLE:
        for nid in graph.active_node_ids_of_type(gconn, node_type):
            node = graph.get_node(gconn, nid)
            if node is None:
                continue
            claim_ids: set[str] = set()
            for s in graph.sources_for_node(gconn, nid):           # sources mentioning the topic
                for clm in graph.claims_for_source(gconn, s):      # their active claims
                    cnode = graph.get_node(gconn, clm)
                    if cnode and cnode["status"] == "active":
                        claim_ids.add(clm)
            # Build contexts and KEEP only grounded ones (have citations), then re-check the
            # trigger over the surviving claims' own sources — not the pre-filter set.
            ctxs = []
            for c in sorted(claim_ids):
                ctx = _claim_context(gconn, c, claims_dir=claims_dir, markdown_dir=markdown_dir)
                if ctx is not None and ctx["citations"]:
                    ctxs.append(ctx)
            surviving_sources = {s for c in ctxs for s in c["sources"]}
            if len(ctxs) < 2 or not _has_independent_pair(surviving_sources, prov):
                continue
            topic_page = claims_dir.parent / NODE_DIR[node_type] / f"{node['slug']}.md"
            topics.append({
                "node_id": nid, "node_type": node_type, "slug": node["slug"],
                "title": _read_fm_title(topic_page) or node["slug"], "claims": ctxs,
                "disagreements": _disagreement_pairs(gconn, {c["claim_id"] for c in ctxs}),
            })
    return topics


def _fingerprint(topic: dict[str, Any], model_ref: str) -> str:
    h = hashlib.sha256()
    parts = [art.SYNTHESIS_SCHEMA_VERSION, art.SYNTHESIS_PROMPT_VERSION, model_ref, topic["node_id"]]
    for c in topic["claims"]:
        parts.append(c["claim_id"])
        parts.append(c["claim_text"])
        for cite in c["citations"]:
            parts.append(f"{cite['source_id']}:{cite['char_start']}:{cite['char_end']}")
    for a, b in topic["disagreements"]:
        parts.append(f"{a}|{b}")
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def _disagreement_notes(topic: dict[str, Any]) -> list[str]:
    by_id = {c["claim_id"]: c["claim_text"] for c in topic["claims"]}
    return [f"{by_id.get(a, a)} <> {by_id.get(b, b)}" for a, b in topic["disagreements"]]


def _contains_verbatim_quote(text: str, source_texts: list[str], window: int = _VERBATIM_WINDOW) -> bool:
    """True if `text` contains a run of `window` consecutive words occurring verbatim
    (whitespace-normalized) in any source — a likely copied quote (direct-quote grounding is
    deferred, so such output is rejected rather than written, ADR-0031 §6)."""
    norm_sources = [_WS.sub(" ", s).lower() for s in source_texts]
    words = _WS.sub(" ", text).lower().split()
    for i in range(len(words) - window + 1):
        run = " ".join(words[i:i + window])
        if any(run in s for s in norm_sources):
            return True
    return False


def _artifact_fp(enrichment_dir: Path, topic_node_id: str) -> str | None:
    apath = art.synthesis_artifact_path(enrichment_dir, topic_node_id)
    if not apath.exists():
        return None
    try:
        return json.loads(apath.read_text(encoding="utf-8")).get("input_fingerprint")
    except (OSError, json.JSONDecodeError):
        return None


def _render_page(gconn, *, syn_id, topic_node, title, summary, synthesis_text, confidence,
                 status, review_status, synthesis_dir: Path, now: str) -> None:
    """Render a synthesis page (filename = syn_id, for cross-type uniqueness): prose from the
    caller, claim links + disagreements from the graph, so the projection matches the graph."""
    claim_ids = [e["dst_id"] for e in graph.outgoing_active(gconn, syn_id)
                 if e["edge_type"] == "derived_from"]
    disagreements = _disagreement_pairs(gconn, set(claim_ids))
    synthesis_dir.mkdir(parents=True, exist_ok=True)
    (synthesis_dir / f"{syn_id}.md").write_text(render_synthesis_page({
        "synthesis_id": syn_id, "title": title, "status": status, "review_status": review_status,
        "confidence": confidence, "topic_node": topic_node, "summary": summary,
        "synthesis_text": synthesis_text, "claim_ids": claim_ids, "disagreements": disagreements,
    }), encoding="utf-8")
    graph.upsert_node(gconn, node_id=syn_id, node_type="synthesis", slug=syn_id, status=status, now=now)


def _withdraw_stale_pending(reviews_dir: Path, topic_node_id: str, keep_fp: str | None, now: str) -> None:
    """Withdraw pending propose_synthesis items for this topic under a *different* fingerprint
    (a regenerated candidate replaces the prior pending one)."""
    pend = reviews_dir / "pending"
    for path in sorted(pend.glob("*.json")) if pend.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        subj = item.get("subject") or {}
        if (item.get("type") == "propose_synthesis" and subj.get("topic_node_id") == topic_node_id
                and subj.get("fingerprint") != keep_fp):
            reviews.withdraw_review_item(reviews_dir, item["review_id"],
                                         reason="superseded by a fresher synthesis", now=now)


def _deprecate_synthesis(gconn, *, syn_id, topic_node, reviews_dir: Path, synthesis_dir: Path,
                         enrichment_dir: Path, reason: str, now: str) -> None:
    """Retract a synthesis through the audited deprecation path (claim/concept tombstone pattern):
    re-render `deprecated_candidate` with a coherent `review_status: pending`, file a
    `deprecate_wiki_page` item, and withdraw any pending proposals — never a bare status rewrite."""
    a = {}
    apath = art.synthesis_artifact_path(enrichment_dir, topic_node)
    if apath.exists():
        try:
            a = json.loads(apath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            a = {}
    _render_page(gconn, syn_id=syn_id, topic_node=topic_node, title=a.get("title", topic_node),
                 summary=a.get("summary", ""), synthesis_text=a.get("synthesis", ""),
                 confidence=a.get("confidence", "low"), status="deprecated_candidate",
                 review_status="pending", synthesis_dir=synthesis_dir, now=now)
    reviews.create_review_item(
        reviews_dir, review_type="deprecate_wiki_page",
        subject={"node_id": syn_id, "page": f"Synthesis/{syn_id}.md"},
        proposal={"to_status": "deprecated_candidate", "reason": reason},
        context={"node_type": "synthesis", "topic_node": topic_node}, now=now)
    _withdraw_stale_pending(reviews_dir, topic_node, keep_fp=None, now=now)


def apply_resolved_syntheses(gconn, reviews_dir: Path, *, synthesis_dir: Path,
                             enrichment_dir: Path, now: str) -> dict[str, int]:
    """Apply human decisions: approve → synthesis `active`, reject → `deprecated_candidate`
    (review-only promotion, no recurrence — ADR-0031). Renders from the synthesis artifact."""
    promoted = rejected = 0
    for state, status, rstatus in (("approved", "active", "approved"),
                                   ("rejected", "deprecated_candidate", "rejected")):
        d = reviews_dir / state
        for path in sorted(d.glob("*.json")) if d.exists() else []:
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if item.get("type") != "propose_synthesis":
                continue
            subj = item.get("subject") or {}
            topic_node = subj.get("topic_node_id")
            if not topic_node:
                continue
            syn_id = synthesis_id(topic_node)
            node = graph.get_node(gconn, syn_id)
            if node is None or node["status"] == status:
                continue  # nothing indexed, or already in the target state (idempotent)
            apath = art.synthesis_artifact_path(enrichment_dir, topic_node)
            if not apath.exists():
                continue
            a = json.loads(apath.read_text(encoding="utf-8"))
            _render_page(gconn, syn_id=syn_id, topic_node=topic_node, title=a["title"],
                         summary=a["summary"], synthesis_text=a["synthesis"], confidence=a["confidence"],
                         status=status, review_status=rstatus, synthesis_dir=synthesis_dir, now=now)
            promoted += state == "approved"
            rejected += state == "rejected"
    return {"promoted": promoted, "rejected": rejected}


def generate_syntheses(
    root: Path,
    *,
    client: LLMClient,
    model_ref: str,
    manifests_dir: Path | None = None,
    jobs_db: Path | None = None,
    graph_db: Path | None = None,
    markdown_dir: Path | None = None,
    enrichment_dir: Path | None = None,
    wiki_dir: Path | None = None,
    reviews_dir: Path | None = None,
    force: bool = False,
    rebuild_index: bool = True,
    record_job: bool = True,
) -> dict[str, Any]:
    """Generate candidate cross-source syntheses for eligible topics; return a run summary."""
    root = Path(root).resolve()
    manifests_dir = Path(manifests_dir) if manifests_dir else root / "raw" / "manifests"
    jobs_db = Path(jobs_db) if jobs_db else root / "db" / "jobs.sqlite"
    graph_db = Path(graph_db) if graph_db else root / "db" / "graph.sqlite"
    markdown_dir = Path(markdown_dir) if markdown_dir else root / "normalized" / "markdown"
    enrichment_dir = Path(enrichment_dir) if enrichment_dir else root / "normalized" / "enrichment"
    wiki_dir = Path(wiki_dir) if wiki_dir else root / "wiki"
    reviews_dir = Path(reviews_dir) if reviews_dir else root / "reviews"
    claims_dir = wiki_dir / "Claims"
    synthesis_dir = wiki_dir / "Synthesis"

    now = iso_now()
    job_id = f"job_{uuid.uuid4().hex[:16]}"
    conn = None
    if record_job:
        db.init_db(jobs_db)
        conn = db.connect(jobs_db)
        db.insert_job(conn, job_id=job_id, job_type="generate_synthesis", status="running",
                      created_at=now, started_at=now)

    graph.init_db(graph_db)
    gconn = graph.connect(graph_db)
    has_key = client.provider_available(model_ref)

    try:
        # 1. Apply human decisions (deterministic; runs without a key).
        resolution = apply_resolved_syntheses(gconn, reviews_dir, synthesis_dir=synthesis_dir,
                                              enrichment_dir=enrichment_dir, now=now)

        # 2. Eligible topics from the current active graph.
        prov = {m["source_id"]: get_provenance(m) for m in list_manifests(manifests_dir)}
        topics = eligible_topics(gconn, prov, claims_dir=claims_dir, markdown_dir=markdown_dir)
        eligible_ids = {t["node_id"] for t in topics}

        # 3. Retract syntheses whose topic is no longer eligible (audited deprecation, keyless).
        retracted = 0
        for syn in graph.nodes_of_type(gconn, "synthesis"):
            if syn["status"] == "deprecated_candidate":
                continue
            page = synthesis_dir / f"{syn['node_id']}.md"
            topic_node = parse_frontmatter(page.read_text(encoding="utf-8")).get("topic_node") \
                if page.exists() else None
            if topic_node not in eligible_ids:
                _deprecate_synthesis(gconn, syn_id=syn["node_id"], topic_node=topic_node or "",
                                     reviews_dir=reviews_dir, synthesis_dir=synthesis_dir,
                                     enrichment_dir=enrichment_dir,
                                     reason="topic no longer an eligible synthesis subject", now=now)
                retracted += 1

        considered = len(topics)
        written = skipped_fresh = skipped_reviewed = stale_active = 0
        errors: list[dict[str, str]] = []

        if has_key:
            for t in topics:
                tid = t["node_id"]
                syn_id = synthesis_id(tid)
                fp = _fingerprint(t, model_ref)
                rid = reviews.review_id("propose_synthesis", _review_subject(tid, fp))
                node = graph.get_node(gconn, syn_id)
                status = node["status"] if node else None
                art_fp = _artifact_fp(enrichment_dir, tid)
                rejected_current = (reviews_dir / "rejected" / f"{rid}.json").exists()

                # Governance gate (Q1/Q2): never rewrite a reviewed synthesis in the normal pass.
                if status == "active":
                    if art_fp == fp:
                        continue                       # approved & current — done
                    stale_active += 1                  # evidence changed since approval
                    if not force:
                        continue                       # stays active (Q1); --force re-opens
                elif rejected_current:
                    skipped_reviewed += 1              # this exact evidence was rejected — no re-nag
                    continue
                elif status == "candidate" and art_fp == fp and not force:
                    skipped_fresh += 1                 # candidate already generated for this evidence
                    continue

                src_texts = []
                for c in t["claims"]:
                    for s in {cite["source_id"] for cite in c["citations"]}:
                        p = markdown_dir / f"{s}.md"
                        if p.exists():
                            src_texts.append(p.read_text(encoding="utf-8"))
                try:
                    result = client.parse(
                        prompts.build_synthesis_messages(t["title"], t["claims"], _disagreement_notes(t)),
                        prompts.SYNTHESIS_SCHEMA, model_ref,
                        schema_version=art.SYNTHESIS_SCHEMA_VERSION,
                        prompt_version=art.SYNTHESIS_PROMPT_VERSION,
                    )
                except ParseError as exc:
                    errors.append({"topic": tid, "error": str(exc)})
                    continue
                if _contains_verbatim_quote(result["summary"] + " " + result["synthesis"], src_texts):
                    errors.append({"topic": tid, "error": "synthesis copied a verbatim source quote (rejected)"})
                    continue
                confidence = round(max(0.0, min(1.0, float(result["confidence"]))), 3)

                # Index the node candidate + (re)write grounded edges: supersede prior synthesis
                # edges, then derived_from -> each contributing claim and related_to -> the topic.
                graph.upsert_node(gconn, node_id=syn_id, node_type="synthesis", slug=syn_id,
                                  status="candidate", now=now)
                for e in graph.outgoing_active(gconn, syn_id):
                    if e["edge_type"] in ("derived_from", "related_to"):
                        graph.set_status(gconn, e["edge_id"], "superseded", now=now)
                for c in t["claims"]:
                    graph.upsert_assertion(gconn, src_id=syn_id, dst_id=c["claim_id"],
                                           edge_type="derived_from", asserted_by="llm",
                                           status="active", job_id=job_id, now=now)
                graph.upsert_assertion(gconn, src_id=syn_id, dst_id=tid, edge_type="related_to",
                                       asserted_by="llm", status="active", job_id=job_id, now=now)

                apath = art.synthesis_artifact_path(enrichment_dir, tid)
                apath.parent.mkdir(parents=True, exist_ok=True)
                apath.write_text(json.dumps({
                    "node_id": syn_id, "topic_node_id": tid, "title": t["title"],
                    "summary": result["summary"], "synthesis": result["synthesis"],
                    "confidence": confidence, "input_fingerprint": fp,
                    "schema_version": art.SYNTHESIS_SCHEMA_VERSION, "model_ref": model_ref,
                    "generation_status": "enriched", "generated_at": now,
                }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

                _render_page(gconn, syn_id=syn_id, topic_node=tid, title=t["title"],
                             summary=result["summary"], synthesis_text=result["synthesis"],
                             confidence=confidence, status="candidate", review_status="pending",
                             synthesis_dir=synthesis_dir, now=now)
                _withdraw_stale_pending(reviews_dir, tid, keep_fp=fp, now=now)
                reviews.create_review_item(
                    reviews_dir, review_type="propose_synthesis",
                    subject=_review_subject(tid, fp),
                    proposal={"to_status": "active", "synthesis_id": syn_id, "title": t["title"],
                              "claim_ids": [c["claim_id"] for c in t["claims"]],
                              "summary": result["summary"]},
                    context={"node_type": t["node_type"]}, priority="medium", now=now)
                written += 1

        changed = written or retracted or resolution["promoted"] or resolution["rejected"]
        index_rebuilt = _rebuild_index(root) if (rebuild_index and changed) else False

        if errors:
            status = "partial"
        elif not has_key and considered > 0:
            status = "skipped"
        else:
            status = "succeeded"

        summary: dict[str, Any] = {
            "job_id": job_id, "model_ref": model_ref, "status": status,
            "eligible_topics": considered, "syntheses_written": written,
            "skipped_fresh": skipped_fresh, "skipped_reviewed": skipped_reviewed,
            "stale_active": stale_active, "retracted": retracted,
            "promoted": resolution["promoted"], "rejected": resolution["rejected"],
            "index_rebuilt": index_rebuilt, "errors": len(errors), "error_details": errors,
            "generated_at": now,
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
