#!/usr/bin/env python3
"""Promotion lifecycle (ADR-0018, restated by ADR-0066): candidate -> active.

ADR-0066: a knowledge item (tag) becomes active when ONE document link to it is kept — the keep is
the promotion, performed immediately by `tag_links.keep` through :func:`activate_node`. There is
**no recurrence auto-promotion** any more: an AI suggestion never becomes active without a human
keep, however many sources mention it. The ≥2-independent-sources threshold survives only as a
synthesis-eligibility input.

This pass remains the executor for **approved `promote_candidate_node` items** still in the
ledger (the flat queue / pre-ADR-0066 items): it activates each through the same
:func:`activate_node` (approve-with-amendments, ADR-0058; the ADR-0059 sentinel gate). On
promotion the page frontmatter is updated to `active` (status authority, ADR-0030) and the
`nodes.status` index is mirrored. Idempotent: a rerun skips already-active nodes.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from app.backend import db, graph, taxonomy
from app.backend.manifests import iso_now, valid_manifests
from app.workers import items, publication, reviews
from app.workers.wiki_render import NODE_DIR, parse_frontmatter, render_item_page

PROMOTABLE = ("item",)
_TITLE_RE = re.compile(r'(?m)^title:\s*"(.*)"\s*$')

AMENDMENT_FIELDS = frozenset({"title", "aliases", "description", "item_type"})
AMEND_MAX_TITLE, AMEND_MAX_ALIAS, AMEND_MAX_ALIASES, AMEND_MAX_DESCRIPTION = 200, 120, 16, 2000


class AmendmentError(ValueError):
    """An amendments payload violates the ADR-0058 shape/limits (the message names the field)."""


def normalize_amendments(amendments: Any) -> dict[str, Any] | None:
    """Normalize + validate an ADR-0058 amendments payload — the ONE check shared by the review
    route, the per-source form, the JSON tag-link route and `tag_links.keep`.

    Allowed fields exactly title/aliases/description/item_type (ADR-0059: item_type must be one of
    the 15 production types — the sentinel is never an amendment target); a blank title is treated
    as not-provided (a title can be corrected, never erased); a blank description explicitly clears
    the page field. Returns the normalized payload, or None when nothing effective was provided.
    Raises :class:`AmendmentError`."""
    if amendments is None:
        return None
    if not isinstance(amendments, dict):
        raise AmendmentError("amendments must be an object")
    unknown = sorted(set(amendments) - AMENDMENT_FIELDS)
    if unknown:
        raise AmendmentError(f"unknown amendment field(s): {unknown}")
    out: dict[str, Any] = {}
    title = amendments.get("title")
    if title is not None:
        if not isinstance(title, str):
            raise AmendmentError("amendment title must be a string")
        title = re.sub(r"\s+", " ", title).strip()[:AMEND_MAX_TITLE]
        if title:
            out["title"] = title
    aliases = amendments.get("aliases")
    if aliases is not None:
        if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
            raise AmendmentError("amendment aliases must be a list of strings")
        out["aliases"] = [a.strip()[:AMEND_MAX_ALIAS] for a in aliases if a.strip()][:AMEND_MAX_ALIASES]
    description = amendments.get("description")
    if description is not None:
        if not isinstance(description, str):
            raise AmendmentError("amendment description must be a string")
        # Canonical single-line prose (review round): whitespace collapsed at the boundary; an
        # explicitly-blank description clears the page field.
        out["description"] = re.sub(r"\s+", " ", description).strip()[:AMEND_MAX_DESCRIPTION]
    item_type = amendments.get("item_type")
    if item_type is not None:
        if not taxonomy.is_production_item_type(item_type):
            raise AmendmentError("amendment item_type must be one of the production taxonomy values")
        out["item_type"] = item_type
    return out or None


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
            # ADR-0059: the page owns the governed classification.
            "item_type": fm.get("item_type"),
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
    """Resolve the effective title/aliases/description/item_type (ADR-0058 approve-with-amendments,
    extended with `item_type` by ADR-0059 — retype is a metadata flip under the type-neutral id,
    and it is REQUIRED to clear the unclassified sentinel).

    Node id stays frozen regardless; only display identity moves. The old title is auto-added
    to the alias list when the title changes (page-authoritative aliases, normalized/deduped)."""
    title = meta["title"]
    aliases = list(meta["aliases"])
    description = meta.get("description")
    item_type = meta.get("item_type")
    if amendments:
        new_title = str(amendments.get("title") or "").strip()
        if isinstance(amendments.get("aliases"), list):
            aliases = [str(a).strip() for a in amendments["aliases"] if str(a).strip()]
        if new_title and new_title != title:
            aliases = items._union(aliases, [title])  # old title stays findable
            title = new_title
        if amendments.get("description") is not None:
            description = str(amendments["description"]).strip() or None
        if taxonomy.is_production_item_type(amendments.get("item_type")):
            item_type = amendments["item_type"]
    return {"title": title, "aliases": items._union(aliases, []), "description": description,
            "item_type": item_type}


def _with_page(wiki_dir: Path, sources) -> list[str]:
    """The given sources that have a Source page (only those are re-rendered)."""
    return [s for s in sorted(sources) if (Path(wiki_dir) / "Sources" / f"{s}.md").exists()]


def activate_node(gconn, *, node: dict[str, Any], wiki_dir: Path, reviews_dir: Path,
                  amendments: dict[str, Any] | None = None, now: str | None = None,
                  dry_run: bool = False) -> dict[str, Any]:
    """Flip one candidate item to `active` — the single promotion writer (ADR-0018/0066).

    Applies approve-with-amendments (title/aliases/description/item_type, ADR-0058/0059): the node
    id stays frozen; only the slug (display path) may move, and this function owns the move. The
    ADR-0059 sentinel gate holds: without a real production `item_type` the node never activates.
    Returns `{outcome: "activated"|"skipped", reason?, slug, amended, affected_sources}`; a skip
    writes NOTHING. `dry_run=True` runs every gate and returns `"would_activate"` without writing
    (the keep path's preflight, so a blocked keep never half-writes)."""
    now = now or iso_now()
    nid, node_type = node["node_id"], node["node_type"]
    page_dir = Path(wiki_dir) / NODE_DIR[node_type]
    old_page = page_dir / f"{node['slug']}.md"
    meta = _read_meta(old_page)
    if meta is None:
        return {"outcome": "skipped", "reason": "page_missing"}
    eff = _apply_amendments(meta, amendments)
    if not taxonomy.is_production_item_type(eff.get("item_type")):
        return {"outcome": "skipped", "reason": "missing_required_item_type"}
    slug = items._slug(eff["title"]) if amendments else node["slug"]
    new_page = page_dir / f"{slug}.md"
    if slug != node["slug"] and new_page.exists():
        # Another page already owns the amended slug — scope-guard skip, no writes.
        return {"outcome": "skipped", "reason": "amended_slug_collision"}
    if dry_run:
        return {"outcome": "would_activate", "slug": slug, "amended": bool(amendments),
                "affected_sources": []}
    sources, suggested = graph.tag_projection(gconn, nid, "active")
    duplicates = graph.active_duplicates(gconn, nid)
    new_page.write_text(
        render_item_page({
            "node_id": nid, "item_type": eff["item_type"], "title": eff["title"],
            "aliases": eff["aliases"], "confidence": "low",
            "source_ids": sources, "suggested_ids": suggested, "status": "active",
            "duplicates": duplicates,
            "split_from": meta.get("split_from"),        # ADR-0052: preserve spin-off lineage
            "split_review_id": meta.get("split_review_id"),
            "description": eff["description"],           # ADR-0058: page-owned description
        }, labels=items._link_labels(wiki_dir, sources + suggested, duplicates)),
        encoding="utf-8")
    graph.upsert_node(gconn, node_id=nid, node_type=node_type, slug=slug, status="active",
                      item_type=eff["item_type"], now=now)
    # Every Source page that lists the tag changes on activation (its projection follows the status;
    # suggestion lists show the title too), not only on a slug move: the caller publishes them all.
    affected = list(sources) + list(suggested)
    if slug != node["slug"]:
        old_page.unlink(missing_ok=True)
        # Inbound links hold the old slug: the mentioning Source pages are in `affected`; duplicate
        # partners re-render here, AFTER the slug mirror update their ## Duplicates projection reads.
        for dup in graph.active_duplicates(gconn, nid):
            items._recompose_node(gconn, node_id=dup["node_id"], wiki_dir=wiki_dir,
                                  reviews_dir=reviews_dir, now=now)
    return {"outcome": "activated", "slug": slug, "amended": bool(amendments),
            "affected_sources": affected}


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
    """Activate candidates whose `promote_candidate_node` item is approved (see the module docstring).
    ADR-0066 review round 10: it rewrites item pages, so the whole run is serialized with tag actions
    (the ledger lock — re-entrant, so a no-op inside run_apply, which already holds it)."""
    from app.workers import tag_links                    # lazy: tag_links imports this module
    lock_dir = Path(reviews_dir) if reviews_dir else Path(root).resolve() / "reviews"
    with tag_links.ledger_lock(lock_dir):
        return _promote_candidates(root, manifests_dir=manifests_dir, graph_db=graph_db,
                                   wiki_dir=wiki_dir, reviews_dir=reviews_dir, jobs_db=jobs_db,
                                   rebuild_index=rebuild_index, record_job=record_job)


def _promote_candidates(
    root: Path,
    *,
    manifests_dir: Path | None,
    graph_db: Path | None,
    wiki_dir: Path | None,
    reviews_dir: Path | None,
    jobs_db: Path | None,
    rebuild_index: bool,
    record_job: bool,
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

    # ADR-0068 decision 5: a standalone run publishes each activation (its tag's Source pages) in
    # its commit and ends with one refresh; inside Apply (`rebuild_index=False`) it is a step, and
    # Apply's own intent and fan-out cover it.
    run = publication.Run(root, "promote", wiki_dir=wiki_dir) if rebuild_index else None
    try:
        _valid, _skipped_invalid = valid_manifests(manifests_dir)
        considered = promoted_review = amended = 0
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
                    if not (reviews_dir / "approved" / f"{rid}.json").exists():
                        continue  # stays candidate (ADR-0066: only a keep or an approval promotes)
                    # ADR-0058 approve-with-amendments: the human's title/aliases/description ride the
                    # approved item; the executor applies them at flip-to-active.
                    intent = run.intent(items=[nid], refresh=False) if run else None
                    out = activate_node(gconn, node=node, wiki_dir=wiki_dir, reviews_dir=reviews_dir,
                                        amendments=_read_amendments(reviews_dir, rid), now=now)
                    if out["outcome"] == "skipped":
                        if run:
                            run.publish(intent)           # nothing changed: clears the intent
                        if out["reason"] != "page_missing":
                            skipped.append({"review_id": rid, "reason": out["reason"]})
                        continue
                    amended += bool(out["amended"])
                    affected_sources.update(out["affected_sources"])
                    if run:                       # every Source page that lists the tag
                        run.publish(intent, sources=_with_page(wiki_dir, out["affected_sources"]),
                                    refresh=True)
                    promoted_review += 1  # human-approved promotion; the ledger loop already closed
            finally:
                gconn.close()

        promoted = promoted_review
        # A run published each activation's Source pages above; as Apply's step it renders none and
        # reports them (`affected_sources`, existing pages only) for Apply's one publication.
        pub = run.finish() if run is not None else None  # index.md + keyword, once (decision 5)
        affected_sources = set(_with_page(wiki_dir, affected_sources))
        summary = {
            "job_id": job_id, "candidates_considered": considered, "promoted": promoted,
            "promoted_by_review": promoted_review,
            "amended": amended, "skipped": skipped,
            "affected_sources": sorted(affected_sources),
            "manifests_skipped_invalid": len(_skipped_invalid),
            "index_rebuilt": bool(pub and pub["index_rebuilt"]),
            "keyword_reindexed": bool(pub and pub["keyword_reindexed"]),
            "publication": publication.summary(pub), "promoted_at": now,
        }
        status = publication.job_status("succeeded", pub)
        summary["status"] = status
        if conn is not None:
            db.update_job(conn, job_id, status=status, finished_at=iso_now(), metadata=summary)
        return summary
    except Exception as exc:
        if conn is not None:
            db.update_job(conn, job_id, status="failed", finished_at=iso_now(), error_message=str(exc))
        raise
    finally:
        if conn is not None:
            conn.close()
