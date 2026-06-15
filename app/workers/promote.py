#!/usr/bin/env python3
"""Phase 3.5b promotion lifecycle (slice 5, ADR-0018): candidate -> active by recurrence.

A deterministic, rerunnable maintenance pass over the graph + manifests + review state (no
LLM). A candidate concept/entity promotes to `active` once **≥2 mutually-independent**
sources mention it. Independence is judged from manifest provenance
(`author/publisher/report_family/canonical_url`, ADR-0018): two sources are independent only
if there is at least one *comparable* key (known on both) whose values differ and no
comparable key is equal — non-comparable/unknown keys never prove independence. Manifests
carry no provenance by default, so the gate is conservative: nothing auto-promotes until
provenance is populated; everything else stays `candidate` with its `promote_candidate_node`
review item pending (early promotion is a human decision).

On promotion: the page frontmatter is updated to `active` (status authority, ADR-0030), the
`nodes.status` index is mirrored, and the `promote_candidate_node` review item is approved
(pending -> approved + audit_log). Idempotent: a rerun skips already-active nodes and writes
no duplicate audit entries.
"""
from __future__ import annotations

import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from app.backend import db, graph
from app.backend.manifests import PROVENANCE_FIELDS, get_provenance, iso_now, list_manifests
from app.workers import reviews
from app.workers.wiki_render import NODE_DIR, parse_frontmatter, render_concept_page

PROMOTABLE = ("concept", "entity", "person", "organization", "project")
_ID_FIELD = {"concept": "concept_id", "entity": "entity_id", "person": "person_id",
             "organization": "organization_id", "project": "project_id"}
_TITLE_RE = re.compile(r'(?m)^title:\s*"(.*)"\s*$')
_WS = re.compile(r"\s+")


def _canon(key: str, value: Any) -> Any:
    """Canonicalize a provenance value before comparison so trivial variants don't read as
    independent (a conservative gate). Text: casefold + whitespace-collapse; canonical_url
    also drops a #fragment and trailing slashes."""
    if not isinstance(value, str):
        return value
    v = _WS.sub(" ", value).strip().casefold()
    if key == "canonical_url":
        v = v.split("#", 1)[0].rstrip("/")
    return v


def _independent(p1: dict[str, Any], p2: dict[str, Any]) -> bool:
    """Two sources independent iff >=1 comparable key differs and none is equal (ADR-0018)."""
    comparable = [k for k in PROVENANCE_FIELDS if p1.get(k) and p2.get(k)]
    return bool(comparable) and all(_canon(k, p1[k]) != _canon(k, p2[k]) for k in comparable)


def _has_independent_pair(sources: list[str], prov: dict[str, dict[str, Any]]) -> bool:
    provs = [prov.get(s, {}) for s in sources]
    return any(_independent(provs[i], provs[j])
               for i in range(len(provs)) for j in range(i + 1, len(provs)))


def _read_meta(page_path: Path) -> dict[str, Any] | None:
    if not page_path.exists():
        return None
    text = page_path.read_text(encoding="utf-8", errors="replace")
    m = _TITLE_RE.search(text)
    if not m:
        return None
    aliases = parse_frontmatter(text).get("aliases")
    return {"title": re.sub(r"\\(.)", r"\1", m.group(1)),
            "aliases": aliases if isinstance(aliases, list) else []}


def _rebuild_index(root: Path) -> bool:
    script = root / "scripts" / "rebuild_index.py"
    if not script.exists():
        return False
    return subprocess.run([sys.executable, str(script), str(root)]).returncode == 0


def promote_candidates(
    root: Path,
    *,
    manifests_dir: Path | None = None,
    graph_db: Path | None = None,
    wiki_dir: Path | None = None,
    reviews_dir: Path | None = None,
    jobs_db: Path | None = None,
    rebuild_index: bool = True,
    record_job: bool = True,
) -> dict[str, Any]:
    root = Path(root).resolve()
    manifests_dir = Path(manifests_dir) if manifests_dir else root / "raw" / "manifests"
    graph_db = Path(graph_db) if graph_db else root / "db" / "graph.sqlite"
    wiki_dir = Path(wiki_dir) if wiki_dir else root / "wiki"
    reviews_dir = Path(reviews_dir) if reviews_dir else root / "reviews"
    jobs_db = Path(jobs_db) if jobs_db else root / "db" / "jobs.sqlite"

    now = iso_now()
    job_id = f"job_{uuid.uuid4().hex[:16]}"
    conn = None
    if record_job:
        db.init_db(jobs_db)
        conn = db.connect(jobs_db)
        db.insert_job(conn, job_id=job_id, job_type="promote", status="running",
                      created_at=now, started_at=now)

    try:
        prov = {m["source_id"]: get_provenance(m) for m in list_manifests(manifests_dir)}
        considered = promoted_recurrence = promoted_review = 0
        if graph_db.exists():
            graph.init_db(graph_db)
            gconn = graph.connect(graph_db)
            try:
                candidates = [
                    dict(r) for r in gconn.execute(
                        "SELECT node_id, node_type, slug FROM nodes WHERE status = 'candidate'")
                ]
                for node in candidates:
                    if node["node_type"] not in PROMOTABLE:
                        continue
                    considered += 1
                    nid = node["node_id"]
                    rid = reviews.review_id("promote_candidate_node", {"node_id": nid})
                    pre_approved = (reviews_dir / "approved" / f"{rid}.json").exists()
                    sources = graph.sources_for_node(gconn, nid)
                    independent = _has_independent_pair(sources, prov)
                    if not (pre_approved or independent):
                        continue  # stays candidate; its promote_candidate_node item stays pending
                    meta = _read_meta(wiki_dir / NODE_DIR[node["node_type"]] / f"{node['slug']}.md")
                    if meta is None:
                        continue
                    (wiki_dir / NODE_DIR[node["node_type"]] / f"{node['slug']}.md").write_text(
                        render_concept_page({
                            "node_type": node["node_type"], "node_id": nid,
                            "id_field": _ID_FIELD[node["node_type"]], "title": meta["title"],
                            "aliases": meta["aliases"], "confidence": "low",
                            "source_ids": sources, "status": "active",
                        }), encoding="utf-8")
                    graph.upsert_node(gconn, node_id=nid, node_type=node["node_type"],
                                      slug=node["slug"], status="active", now=now)
                    if pre_approved:
                        promoted_review += 1  # human-approved early promotion; loop already closed
                    else:
                        # Recurrence: close the loop deterministically — ensure an item exists
                        # (legacy/missing), then approve it (pending -> approved + audit_log).
                        reviews.create_review_item(
                            reviews_dir, review_type="promote_candidate_node",
                            subject={"node_id": nid},
                            proposal={"to_status": "active", "node_type": node["node_type"]},
                            now=now)
                        reviews.resolve_review_item(
                            reviews_dir, rid, decision="approved", decided_by="recurrence",
                            note="promoted on >=2 independent sources", now=now)
                        promoted_recurrence += 1
            finally:
                gconn.close()

        promoted = promoted_recurrence + promoted_review
        index_rebuilt = _rebuild_index(root) if (rebuild_index and promoted) else False
        summary = {
            "job_id": job_id, "candidates_considered": considered, "promoted": promoted,
            "promoted_by_recurrence": promoted_recurrence, "promoted_by_review": promoted_review,
            "index_rebuilt": index_rebuilt, "promoted_at": now,
        }
        if conn is not None:
            db.update_job(conn, job_id, status="succeeded", finished_at=iso_now(), metadata=summary)
        return summary
    except Exception as exc:
        if conn is not None:
            db.update_job(conn, job_id, status="failed", finished_at=iso_now(), error_message=str(exc))
        raise
    finally:
        if conn is not None:
            conn.close()
