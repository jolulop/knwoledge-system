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

import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from app.backend import db, graph
from app.backend.manifests import get_provenance, independent_sources, iso_now, valid_manifests
from app.workers import concepts, reviews, wiki
from app.workers.wiki_render import NODE_DIR, parse_frontmatter, render_concept_page

PROMOTABLE = ("concept", "entity", "person", "organization", "project")
_ID_FIELD = {"concept": "concept_id", "entity": "entity_id", "person": "person_id",
             "organization": "organization_id", "project": "project_id"}
_TITLE_RE = re.compile(r'(?m)^title:\s*"(.*)"\s*$')


def _has_independent_pair(sources: list[str], prov: dict[str, dict[str, Any]]) -> bool:
    """Any mutually-independent pair among a node's mentioning sources (ADR-0018)."""
    provs = [prov.get(s, {}) for s in sources]
    return any(independent_sources(provs[i], provs[j])
               for i in range(len(provs)) for j in range(i + 1, len(provs)))


def _read_meta(page_path: Path) -> dict[str, Any] | None:
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
            # ADR-0052: preserve a spin-off's split lineage when the promote pass re-renders it active.
            "split_from": fm.get("split_from"), "split_review_id": fm.get("split_review_id"),
            # ADR-0058: preserve the page-owned human description.
            "description": fm.get("description")}


def _read_amendments(reviews_dir: Path, rid: str) -> dict[str, Any] | None:
    """The approve-with-amendments payload stored on the approved item (ADR-0058), if any."""
    path = reviews_dir / "approved" / f"{rid}.json"
    if not path.exists():
        return None
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    amendments = item.get("amendments") if isinstance(item, dict) else None
    return amendments if isinstance(amendments, dict) else None


def _apply_amendments(meta: dict[str, Any], amendments: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve the effective title/aliases/description (ADR-0058 approve-with-amendments).

    Node id stays frozen regardless; only display identity moves. The old title is auto-added
    to the alias list when the title changes (page-authoritative aliases, normalized/deduped)."""
    title = meta["title"]
    aliases = list(meta["aliases"])
    description = meta.get("description")
    if amendments:
        new_title = str(amendments.get("title") or "").strip()
        if isinstance(amendments.get("aliases"), list):
            aliases = [str(a).strip() for a in amendments["aliases"] if str(a).strip()]
        if new_title and new_title != title:
            aliases = concepts._union(aliases, [title])  # old title stays findable
            title = new_title
        if amendments.get("description") is not None:
            description = str(amendments["description"]).strip() or None
    return {"title": title, "aliases": concepts._union(aliases, []), "description": description}


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
        _valid, _skipped_invalid = valid_manifests(manifests_dir)
        prov = {m["source_id"]: get_provenance(m) for m in _valid}
        considered = promoted_recurrence = promoted_review = amended = 0
        skipped: list[dict[str, str]] = []
        affected_sources: set[str] = set()
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
                    page_dir = wiki_dir / NODE_DIR[node["node_type"]]
                    old_page = page_dir / f"{node['slug']}.md"
                    meta = _read_meta(old_page)
                    if meta is None:
                        continue
                    # ADR-0058 approve-with-amendments: the human's title/aliases/description ride the
                    # approved item; the executor applies them at flip-to-active. The node id stays
                    # frozen — only the slug (display path) may move, and this executor owns the move.
                    amendments = _read_amendments(reviews_dir, rid) if pre_approved else None
                    eff = _apply_amendments(meta, amendments)
                    slug = concepts._slug(eff["title"]) if amendments else node["slug"]
                    new_page = page_dir / f"{slug}.md"
                    if slug != node["slug"] and new_page.exists():
                        # Another page already owns the amended slug — scope-guard skip, no writes.
                        skipped.append({"review_id": rid, "reason": "amended_slug_collision"})
                        continue
                    new_page.write_text(
                        render_concept_page({
                            "node_type": node["node_type"], "node_id": nid,
                            "id_field": _ID_FIELD[node["node_type"]], "title": eff["title"],
                            "aliases": eff["aliases"], "confidence": "low",
                            "source_ids": sources, "status": "active",
                            "duplicates": graph.active_duplicates(gconn, nid),
                            "split_from": meta.get("split_from"),        # ADR-0052: preserve spin-off lineage
                            "split_review_id": meta.get("split_review_id"),
                            "description": eff["description"],           # ADR-0058: page-owned description
                        }), encoding="utf-8")
                    if amendments:
                        amended += 1
                    graph.upsert_node(gconn, node_id=nid, node_type=node["node_type"],
                                      slug=slug, status="active", now=now)
                    if slug != node["slug"]:
                        old_page.unlink(missing_ok=True)
                        # Inbound links hold the old slug: mentioning Source pages re-render via the
                        # caller fan-out (affected_sources); duplicate partners re-render here, AFTER
                        # the slug mirror update their ## Duplicates projection reads.
                        affected_sources.update(sources)
                        for dup in graph.active_duplicates(gconn, nid):
                            concepts._recompose_node(gconn, node_id=dup["node_id"], wiki_dir=wiki_dir,
                                                     reviews_dir=reviews_dir, now=now)
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
        # ADR-0058: an amended slug moved a page — re-render the mentioning Source pages so their
        # [[Dir/slug]] links follow (the executor owns the whole move; validators stay green even
        # on a standalone scripts/promote.py run). Only sources with an existing Source page.
        fanout_sources = [s for s in sorted(affected_sources)
                          if (wiki_dir / "Sources" / f"{s}.md").exists()]
        if fanout_sources:
            wiki.generate_wiki(root, source_ids=fanout_sources, rebuild_index=False,
                               record_job=False)
        index_rebuilt = _rebuild_index(root) if (rebuild_index and promoted) else False
        summary = {
            "job_id": job_id, "candidates_considered": considered, "promoted": promoted,
            "promoted_by_recurrence": promoted_recurrence, "promoted_by_review": promoted_review,
            "amended": amended, "skipped": skipped,
            "affected_sources": sorted(affected_sources),
            "manifests_skipped_invalid": len(_skipped_invalid),
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
