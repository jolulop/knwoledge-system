#!/usr/bin/env python3
"""Phase 3.5c slice 1 contradiction detection (ADR-0030/0031).

Tier-3 LLM pass that finds where *independent* sources disagree. It never compares all claim
pairs (O(N²) heavy-model calls); a deterministic **graph-neighborhood blocking** step bounds
the work first: two `active` claims are a candidate pair iff their sources co-mention a
concept/entity (`claim → source → concept` via `active` edges) **and** at least one of those
source pairs is mutually independent (ADR-0018 — a source cannot contradict itself, and
same-author/same-family is not real disagreement). `candidate_pairs` is the deterministic,
LLM-free core and is unit-tested offline.

For each candidate pair the tier-3 model returns a verdict. A genuine contradiction is written
as **one `contradicts` assertion** with the two claim ids **sorted** (`src_id < dst_id`) so the
symmetric relation collapses to a single row and a single `resolve_contradiction` review; it is
`status=proposed`, `asserted_by=llm` — a semantic judgment, invisible until a human approves it
(ADR-0030). The row's evidence anchor is the `src` claim's primary citation (advisory only — the
authoritative two-sided evidence is the two Claim pages, carried in the review item). The
verdict's per-pair idempotency is the **response cache**: the prompt embeds both claim texts and
their evidence quotes (reconstructed from the citation anchors) and the shared topic names, so an
unchanged pair replays the cached verdict with no provider call, while a changed claim or anchor
busts the cache and re-evaluates (ADR-0027/0031).

Re-run discipline mirrors the claim worker: human decisions are applied first
(`apply_resolved_contradictions` — approve → `active`, reject → `rejected`), then pairs that have
left the candidate set have their `proposed`/`active` llm assertion **superseded** and their
pending review withdrawn. No API key → a `skipped` job, but the deterministic stale-supersession
and resolution-application still run. The LLM proposes; the human disposes (CLAUDE.md rule 9).
"""
from __future__ import annotations

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
from app.workers import claims, reviews
from app.workers import enrichment_artifact as art

_CLAIM_TEXT_RE = re.compile(r'(?m)^claim_text:\s*"(.*)"\s*$')


def _rebuild_index(root: Path) -> bool:
    script = root / "scripts" / "rebuild_index.py"
    if not script.exists():
        return False
    return subprocess.run([sys.executable, str(script), str(root)]).returncode == 0


# --- claim context (durable wording + reconstructed citations) -------------


def _read_claim_text(page_path: Path) -> str | None:
    """Read the durable claim_text from a Claim page's frontmatter (page is the authority)."""
    if not page_path.exists():
        return None
    m = _CLAIM_TEXT_RE.search(page_path.read_text(encoding="utf-8", errors="replace"))
    return re.sub(r"\\(.)", r"\1", m.group(1)) if m else None


def _claim_context(gconn, cid: str, *, claims_dir: Path, markdown_dir: Path) -> dict[str, Any] | None:
    """A claim's review-facing context: durable text + citations reconstructed from its active
    `derived_from` edges (quotes read back from the source spans). None if it has no wording."""
    claim_text = _read_claim_text(claims_dir / f"{cid}.md")
    if claim_text is None:
        return None
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
    return {"claim_id": cid, "claim_text": claim_text, "citations": cites}


# --- deterministic candidate-pair generation (the blocking step) -----------


def candidate_pairs(gconn, prov: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Graph-neighborhood blocking: candidate claim pairs to send to the tier-3 verdict.

    Deterministic and LLM-free. Two `active` claims A,B (ids sorted, A < B) qualify iff there
    exist sources sA derived-from A and sB derived-from B with sA ≠ sB, sA/sB mutually
    independent (ADR-0018), and sA, sB co-mentioning ≥1 concept/entity. `shared_nodes` is the
    union of those co-mentioned node ids (the blocking topics). Returns dicts
    `{claim_a, claim_b, shared_nodes}` ordered deterministically.
    """
    claims = graph.active_node_ids_of_type(gconn, "claim")
    claim_sources = {c: graph.sources_for_claim(gconn, c) for c in claims}
    # Concept neighborhood + provenance per source, computed once.
    source_ids = {s for srcs in claim_sources.values() for s in srcs}
    src_concepts = {s: graph.concept_ids_for_source(gconn, s) for s in source_ids}

    pairs: list[dict[str, Any]] = []
    for i in range(len(claims)):
        for j in range(i + 1, len(claims)):
            a, b = claims[i], claims[j]  # already sorted (active_node_ids_of_type orders by id)
            shared: set[str] = set()
            for sa in claim_sources[a]:
                for sb in claim_sources[b]:
                    if sa == sb:
                        continue  # a source cannot contradict itself
                    if not independent_sources(prov.get(sa, {}), prov.get(sb, {})):
                        continue
                    shared |= src_concepts[sa] & src_concepts[sb]
            if shared:
                pairs.append({"claim_a": a, "claim_b": b, "shared_nodes": sorted(shared)})
    return pairs


# --- resolution application (deterministic effect of human decisions) ------


def _resolved_items(reviews_dir: Path, state: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    d = reviews_dir / state
    if not d.exists():
        return out
    for path in sorted(d.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if item.get("type") == "resolve_contradiction":
            out.append(item)
    return out


def apply_resolved_contradictions(
    gconn, reviews_dir: Path, *, claims_dir: Path, markdown_dir: Path,
    affected: set[str] | None = None, now: str | None = None,
) -> dict[str, int]:
    """Apply human decisions to the graph (deterministic; runs every pass, key or not).

    `approved` → the pair's `contradicts` edge flips to `active` (acknowledge — a standing
    disagreement; both claims stay live). `rejected` → the edge flips to `rejected`. A
    `supersede` decision (an approved item naming a `winner`) activates the edge **and** executes
    the winner→loser effects (slice 1b, ADR-0031): an `active` `supersedes` edge plus the loser
    deprecated to `deprecated_candidate` through the `deprecate_wiki_page` audit path (the
    `contradicts` edge stays active so the historical conflict is recorded). Idempotent: only
    transitions a row whose status differs, and the supersede effects are applied once. Both
    endpoint claims of any transition are added to `affected` so the caller re-projects their
    Claim pages (a backlink appears on acknowledge, disappears on reject).
    """
    now = now or iso_now()
    affected = affected if affected is not None else set()
    acknowledged = rejected = superseded_executed = 0

    def _set(a: str, b: str, rows: list[dict[str, Any]], target: str, statuses: tuple[str, ...]) -> int:
        n = 0
        for r in rows:
            if r["status"] in statuses and r["status"] != target:
                graph.set_status(gconn, r["edge_id"], target, now=now)
                affected.update((a, b))
                n += 1
        return n

    for item in _resolved_items(reviews_dir, "approved"):
        subj = item.get("subject") or {}
        a, b = subj.get("claim_a"), subj.get("claim_b")
        if not a or not b:
            continue
        acknowledged += _set(a, b, graph.contradiction_between(gconn, a, b), "active", ("proposed",))
        winner = item.get("winner")
        if winner in (a, b):  # supersede decision: execute winner→loser effects (slice 1b)
            loser = b if winner == a else a
            if _execute_supersede(gconn, reviews_dir, winner=winner, loser=loser,
                                  source_review_id=item.get("review_id", ""), now=now,
                                  claims_dir=claims_dir, markdown_dir=markdown_dir):
                superseded_executed += 1
            affected.update((a, b))
    for item in _resolved_items(reviews_dir, "rejected"):
        subj = item.get("subject") or {}
        a, b = subj.get("claim_a"), subj.get("claim_b")
        if not a or not b:
            continue
        rejected += _set(a, b, graph.contradiction_between(gconn, a, b), "rejected", ("proposed", "active"))

    return {"acknowledged": acknowledged, "rejected": rejected,
            "superseded_executed": superseded_executed}


def _execute_supersede(
    gconn, reviews_dir: Path, *, winner: str, loser: str, source_review_id: str, now: str,
    claims_dir: Path, markdown_dir: Path,
) -> bool:
    """Execute an approved `supersede` resolution (slice 1b, ADR-0031): an `active` `supersedes`
    edge winner→loser, and the loser deprecated to `deprecated_candidate` via the
    `deprecate_wiki_page` audit path — authorized by the contradiction approval (no second human
    gate), with the cause recorded. The `contradicts` edge is left active by the caller. Returns
    True if it applied effects this call; idempotent (a no-op once already executed)."""
    already_edge = any(e["dst_id"] == loser and e["edge_type"] == "supersedes"
                       for e in graph.outgoing_active(gconn, winner))
    loser_node = graph.get_node(gconn, loser)
    already_dep = bool(loser_node) and loser_node["status"] == "deprecated_candidate"
    if already_edge and already_dep:
        return False
    # supersedes is a same-node-type edge (claim -> claim); both endpoints are indexed.
    graph.upsert_assertion(gconn, src_id=winner, dst_id=loser, edge_type="supersedes",
                           asserted_by="human", status="active", review_id=source_review_id, now=now)
    # Deprecate the loser (page is the status authority); its evidence + contradiction backlink
    # stay rendered, status flips to deprecated_candidate.
    claims.recompose_claim(gconn, cid=loser, claims_dir=claims_dir, reviews_dir=reviews_dir,
                           markdown_dir=markdown_dir, now=now, deprecate=True)
    # Audit trail: the deprecation is authorized by the approved contradiction resolution, so we
    # file a deprecate_wiki_page item and immediately resolve it approved (records the cause).
    dep_rid = reviews.create_review_item(
        reviews_dir, review_type="deprecate_wiki_page",
        subject={"node_id": loser, "page": f"Claims/{loser}.md"},
        proposal={"to_status": "deprecated_candidate",
                  "reason": "superseded via approved contradiction resolution"},
        context={"resolved_by_review": source_review_id, "winner": winner}, now=now)
    reviews.resolve_review_item(
        reviews_dir, dep_rid, decision="approved", decided_by="contradiction_resolution",
        note=f"deprecated as loser of approved contradiction {source_review_id}; winner {winner}",
        now=now)
    return True


# --- the worker ------------------------------------------------------------


def detect_contradictions(
    root: Path,
    *,
    client: LLMClient,
    model_ref: str,
    manifests_dir: Path | None = None,
    jobs_db: Path | None = None,
    graph_db: Path | None = None,
    markdown_dir: Path | None = None,
    wiki_dir: Path | None = None,
    reviews_dir: Path | None = None,
    rebuild_index: bool = True,
    record_job: bool = True,
) -> dict[str, Any]:
    """Detect contradictions across independent sources; return a run summary."""
    root = Path(root).resolve()
    manifests_dir = Path(manifests_dir) if manifests_dir else root / "raw" / "manifests"
    jobs_db = Path(jobs_db) if jobs_db else root / "db" / "jobs.sqlite"
    graph_db = Path(graph_db) if graph_db else root / "db" / "graph.sqlite"
    markdown_dir = Path(markdown_dir) if markdown_dir else root / "normalized" / "markdown"
    wiki_dir = Path(wiki_dir) if wiki_dir else root / "wiki"
    reviews_dir = Path(reviews_dir) if reviews_dir else root / "reviews"
    claims_dir = wiki_dir / "Claims"

    now = iso_now()
    job_id = f"job_{uuid.uuid4().hex[:16]}"
    conn = None
    if record_job:
        db.init_db(jobs_db)
        conn = db.connect(jobs_db)
        db.insert_job(conn, job_id=job_id, job_type="detect_contradictions", status="running",
                      created_at=now, started_at=now)

    graph.init_db(graph_db)
    gconn = graph.connect(graph_db)
    has_key = client.provider_available(model_ref)

    try:
        affected: set[str] = set()  # claim ids whose page projection must be re-rendered

        # 1. Apply human decisions made since the last run (deterministic; runs without a key).
        resolution = apply_resolved_contradictions(
            gconn, reviews_dir, claims_dir=claims_dir, markdown_dir=markdown_dir,
            affected=affected, now=now)

        # 2. Recompute candidate pairs from the current active graph.
        prov = {m["source_id"]: get_provenance(m) for m in list_manifests(manifests_dir)}
        pairs = candidate_pairs(gconn, prov)
        pair_keys = {(p["claim_a"], p["claim_b"]) for p in pairs}
        active_claims = set(graph.active_node_ids_of_type(gconn, "claim"))
        standing_claims = graph.claims_with_active_evidence(gconn)  # evidence-based endpoint validity

        # 3. Supersede stale llm assertions and withdraw their pending reviews (runs without a
        #    key — like claim retraction). Two distinct conditions (ADR-0031):
        #    - **endpoint gone** (a claim no longer *stands* — has no `active` evidence, i.e. a
        #      tombstone): supersede whether proposed OR active. Endpoint validity is
        #      **evidence-based, not node-status-based**, so a claim that is `deprecated_candidate`
        #      by a human supersede decision but *keeps* its evidence is NOT "gone" — its
        #      `contradicts` edge stays active (the historical conflict is recorded). The CLAIM
        #      worker is the *primary* enforcer on tombstone; this is a backstop.
        #    - **pair left the candidate set** but both endpoints stand (e.g. a provenance edit
        #      removed independence): supersede only a *proposed* assertion. Independence is the
        #      blocking criterion for *finding* candidates, not a validity condition, so it must
        #      not silently undo a human acknowledgement.
        stale = 0
        for row in graph.contradiction_assertions(gconn, statuses=("proposed", "active")):
            a, b = row["src_id"], row["dst_id"]
            endpoint_gone = a not in standing_claims or b not in standing_claims
            left_candidate = (a, b) not in pair_keys
            if endpoint_gone or (row["status"] == "proposed" and left_candidate):
                graph.set_status(gconn, row["edge_id"], "superseded", now=now)
                rid = reviews.review_id("resolve_contradiction", {"claim_a": a, "claim_b": b})
                reason = "an endpoint claim no longer stands" if endpoint_gone \
                    else "pair no longer a candidate"
                reviews.withdraw_review_item(reviews_dir, rid, reason=reason, now=now)
                affected.update({a, b} & standing_claims)  # re-project the surviving claim(s)
                stale += 1

        considered = len(pairs)
        evaluated = proposed = not_contradiction = skipped_decided = 0
        errors: list[dict[str, str]] = []

        if has_key:
            for p in pairs:
                a, b = p["claim_a"], p["claim_b"]
                existing = graph.contradiction_between(gconn, a, b)
                statuses = {r["status"] for r in existing}
                if "active" in statuses or "rejected" in statuses:
                    skipped_decided += 1  # human already decided this pair; don't re-nag or re-cost
                    continue

                ctx_a = _claim_context(gconn, a, claims_dir=claims_dir, markdown_dir=markdown_dir)
                ctx_b = _claim_context(gconn, b, claims_dir=claims_dir, markdown_dir=markdown_dir)
                if (ctx_a is None or ctx_b is None
                        or not ctx_a["citations"] or not ctx_b["citations"]):
                    continue  # both sides need wording + grounded evidence before a verdict
                try:
                    verdict = client.parse(
                        prompts.build_contradiction_messages(
                            ctx_a["claim_text"], ctx_a["citations"],
                            ctx_b["claim_text"], ctx_b["citations"], p["shared_nodes"]),
                        prompts.CONTRADICTION_SCHEMA, model_ref,
                        schema_version=art.CONTRADICTION_SCHEMA_VERSION,
                        prompt_version=art.CONTRADICTION_PROMPT_VERSION,
                    )
                except ParseError as exc:
                    errors.append({"pair": f"{a}|{b}", "error": str(exc)})
                    continue
                evaluated += 1

                # Drop any prior proposed rows for this pair (e.g. a changed anchor), then write
                # the current verdict. Existing active/rejected are excluded above.
                for r in existing:
                    if r["status"] == "proposed":
                        graph.set_status(gconn, r["edge_id"], "superseded", now=now)

                if not bool(verdict["contradicts"]):
                    rid = reviews.review_id("resolve_contradiction", {"claim_a": a, "claim_b": b})
                    reviews.withdraw_review_item(reviews_dir, rid, reason="no longer judged a contradiction", now=now)
                    not_contradiction += 1
                    continue

                anchor = ctx_a["citations"][0]
                confidence = max(0.0, min(1.0, float(verdict["confidence"])))  # clamp untrusted value
                rid = reviews.review_id("resolve_contradiction", {"claim_a": a, "claim_b": b})
                graph.upsert_assertion(
                    gconn, src_id=a, dst_id=b, edge_type="contradicts", asserted_by="llm",
                    status="proposed", confidence=confidence,
                    evidence_source_id=anchor["source_id"], evidence_char_start=anchor["char_start"],
                    evidence_char_end=anchor["char_end"], review_id=rid, job_id=job_id, now=now,
                )
                reviews.create_review_item(
                    reviews_dir, review_type="resolve_contradiction",
                    subject={"claim_a": a, "claim_b": b},
                    proposal={
                        "outcomes": ["acknowledge", "supersede", "reject"],
                        "confidence": confidence,
                        "explanation": str(verdict["explanation"]),
                        "sides": [ctx_a, ctx_b],
                    },
                    context={"shared_nodes": p["shared_nodes"], "asserted_by": "llm"},
                    priority="medium", now=now)
                proposed += 1

        # 4. Re-project Claim pages whose active contradiction backlinks changed (ADR-0031).
        pages_reprojected = 0
        for cid in sorted(affected & active_claims):
            outcome = claims.recompose_claim(
                gconn, cid=cid, claims_dir=claims_dir, reviews_dir=reviews_dir,
                markdown_dir=markdown_dir, now=now)
            pages_reprojected += outcome == "written"
        index_rebuilt = _rebuild_index(root) if (rebuild_index and pages_reprojected) else False

        if errors:
            status = "partial"
        elif not has_key and considered > 0:
            status = "skipped"
        else:
            status = "succeeded"

        summary: dict[str, Any] = {
            "job_id": job_id, "model_ref": model_ref, "status": status,
            "candidate_pairs": considered, "pairs_evaluated": evaluated,
            "contradictions_proposed": proposed, "not_contradiction": not_contradiction,
            "skipped_human_decided": skipped_decided, "superseded_stale": stale,
            "claim_pages_reprojected": pages_reprojected, "index_rebuilt": index_rebuilt,
            "resolutions_acknowledged": resolution["acknowledged"],
            "resolutions_rejected": resolution["rejected"],
            "supersede_executed": resolution["superseded_executed"],
            "errors": len(errors), "error_details": errors, "detected_at": now,
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
