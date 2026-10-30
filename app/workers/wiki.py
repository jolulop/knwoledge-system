#!/usr/bin/env python3
"""Phase 3 wiki worker: generate deterministic Source pages from the normalized layer.

For every source whose manifest reports `extracted`/`partial`, render
`wiki/Sources/<source_id>.md` from `templates/source.md` (ADR-0015/0016), publish the run's end
refresh (`wiki/index.md` via scripts/rebuild_index.py and the keyword index, ADR-0068 decision 5),
append `wiki/log.md`, and record a `generate_wiki` job. Deterministic and offline: identical inputs yield byte-identical
pages. Idempotent — a page whose recomputed input_fingerprint matches the stored one is
skipped unless `force` is given (ADR-0023).

The wiki layer is mutable local data (ADR-0014); this worker writes only under wiki/,
db/jobs.sqlite and, through publication, the keyword index and db/publication_pending.json, never
under raw/ or normalized/.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from app.backend import db, graph, write_lock
from app.backend.config import get_settings
from app.backend.manifests import iso_now, load_manifest, valid_manifests
from app.workers import authors, enrichment_artifact, labels, titles, wiki_render


def _claim_label(claims_dir: Path, claim_id: str) -> str | None:
    """Resolve a claim's display label from its durable Claim page.

    Worker-side IO so the renderer stays pure; returns None (-> bare link) if the page or
    field is unavailable, never the gitignored enrichment record. ADR-0060: the label is the
    page's `title:` (falling back to deriving from `claim_text` for pre-0060 pages) so every
    claim alias agrees with what `display_alias_rot` recomputes."""
    label = labels._page_label(claims_dir / f"{claim_id}.md", f"Claims/{claim_id}")
    return label or None


_TITLE_RE = re.compile(r'(?m)^title:\s*"(.*)"\s*$')


def _node_title(wiki_dir: Path, node_type: str, slug: str) -> str | None:
    """Resolve an item node's display title from its page (worker-side IO)."""
    page = wiki_dir / wiki_render.NODE_DIR[node_type] / f"{slug}.md"
    if not page.exists():
        return None
    match = _TITLE_RE.search(page.read_text(encoding="utf-8", errors="replace"))
    return re.sub(r"\\(.)", r"\1", match.group(1)) if match else None

def _keywords(enrichment_dir: Path, source_id: str, curated: list[dict[str, Any]],
              suggested: list[dict[str, Any]], wiki_dir: Path) -> list[str]:
    """A Source page's search terms (ADR-0066 part 3 slice 2 Q3): the tags it keeps, then its current
    suggestions in the order its suggestion plan recorded (the items artifact), from the same
    projection the page and the document screen list. Dropped, blocked and over-cap suggestions
    have no active mention, so they never appear; a hidden tag is left out. De-duplicated by name."""
    order: dict[str, int] = {}
    try:
        nodes = json.loads(enrichment_artifact.items_artifact_path(enrichment_dir, source_id)
                           .read_text(encoding="utf-8")).get("nodes") or []
        order = {n["node_id"]: i for i, n in enumerate(nodes) if isinstance(n, dict)
                 and isinstance(n.get("node_id"), str)}
    except (OSError, ValueError, AttributeError):
        pass
    rows = [m for m in curated if m["node_type"] == "item"] + sorted(
        suggested, key=lambda m: (order.get(m["dst_id"], len(order)), m["slug"] or ""))
    out: list[str] = []
    seen: set[str] = set()
    for m in rows:
        if m.get("status") == "hidden":
            continue
        name = _node_title(wiki_dir, "item", m["slug"]) or m["slug"]
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    return out


_GENERATED_STATUSES = {"extracted", "partial"}
# A wildcard bind address is not something a browser can open; links use loopback instead.
_WILDCARD_HOSTS = {"", "0.0.0.0", "::"}


def raw_view_base(root: Path) -> str:
    """The base URL of the API's raw view (`GET /raw/<source_id>`) for Source-page "open original"
    links, from APP_HOST/APP_PORT. It goes into the rendered page, so changing the port re-renders
    every Source page on the next run."""
    settings = get_settings(root)
    host = "127.0.0.1" if settings.app_host in _WILDCARD_HOSTS else settings.app_host
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{settings.app_port}/raw/"


def _append_log(wiki_dir: Path, now: str, summary: dict[str, Any]) -> None:
    log_path = wiki_dir / "log.md"
    if not log_path.exists():
        log_path.write_text(
            "# Log\n\nAppend-only semantic history of ingests, queries, lint passes, "
            "reviews, and maintenance.\n",
            encoding="utf-8",
        )
    entry = (
        f"\n## [{now[:10]}] generate_wiki | Generated {summary['generated']} source page(s)\n\n"
        f"Job {summary['job_id']}: generated {summary['generated']}, "
        f"skipped_unchanged {summary['skipped_unchanged']}, "
        f"skipped_not_extracted {summary['skipped_not_extracted']}, "
        f"errors {summary['errors']}.\n"
    )
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(entry)


def generate_wiki(
    root: Path,
    *,
    source_ids: list[str] | None = None,
    force: bool = False,
    manifests_dir: Path | None = None,
    jobs_db: Path | None = None,
    wiki_dir: Path | None = None,
    templates_dir: Path | None = None,
    markdown_dir: Path | None = None,
    enrichment_dir: Path | None = None,
    graph_db: Path | None = None,
    summary_max: int = 320,
    summary_min: int = 40,
    rebuild_index: bool = True,
    record_job: bool = True,
) -> dict[str, Any]:
    """Generate Source pages for pending (or selected) sources; return a run summary.

    As a run (`rebuild_index=True`, ADR-0068 decision 5) it records its end refresh first and ends
    with one publication: `index.md`, the keyword index, everything already pending and one more try
    of any page that failed. With `rebuild_index=False` it is a step of a caller that publishes
    (publication itself, Apply), so it renders the pages and nothing else."""
    root = Path(root).resolve()
    manifests_dir = Path(manifests_dir) if manifests_dir else root / "raw" / "manifests"
    jobs_db = Path(jobs_db) if jobs_db else root / "db" / "jobs.sqlite"
    wiki_dir = Path(wiki_dir) if wiki_dir else root / "wiki"
    templates_dir = Path(templates_dir) if templates_dir else root / "templates"
    markdown_dir = Path(markdown_dir) if markdown_dir else root / "normalized" / "markdown"
    logs_dir = root / "normalized" / "extraction_logs"   # the PDF metadata hint (ADR-0066 part 3)
    enrichment_dir = (
        Path(enrichment_dir) if enrichment_dir else root / "normalized" / "enrichment"
    )
    graph_db = Path(graph_db) if graph_db else root / "db" / "graph.sqlite"
    sources_dir = wiki_dir / "Sources"
    claims_dir = wiki_dir / "Claims"
    sources_dir.mkdir(parents=True, exist_ok=True)
    # Read-only graph connection for the Source-page Claims projection (slice 3b). Absent
    # before any claim extraction -> no claim links, just the pending placeholder.
    gconn = graph.connect(graph_db) if graph_db.exists() else None

    template = (templates_dir / "source.md").read_text(encoding="utf-8")
    raw_base = raw_view_base(root)
    now = iso_now()
    job_id = f"job_{uuid.uuid4().hex[:16]}"
    conn = None
    if record_job:
        db.init_db(jobs_db)
        conn = db.connect(jobs_db)
        db.insert_job(
            conn, job_id=job_id, job_type="generate_wiki", status="running",
            created_at=now, started_at=now,
        )

    run = None
    try:
        if rebuild_index:
            from app.workers import publication          # lazy: publication renders through here
            run = publication.Run(root, "generate_wiki", wiki_dir=wiki_dir,
                                  templates_dir=templates_dir)
            with write_lock.hold(root / "reviews"):
                run.intent()                              # the end refresh, queued first
        manifests, skipped_invalid = valid_manifests(manifests_dir)
        if source_ids is not None:
            wanted = set(source_ids)
            manifests = [m for m in manifests if m.get("source_id") in wanted]

        considered = 0
        generated = 0
        skipped_unchanged = 0
        skipped_not_extracted = 0
        errors: list[dict[str, str]] = []

        def _render_one(manifest: dict[str, Any]) -> str | dict[str, str]:
            """Render and write one Source page from current state (ADR-0068: called under
            the write lock, so the manifest, ledger and graph it reads are consistent)."""
            source_id = manifest["source_id"]
            page_path = sources_dir / f"{source_id}.md"
            try:
                md_path = markdown_dir / f"{source_id}.md"
                normalized_markdown = (
                    md_path.read_text(encoding="utf-8") if md_path.exists() else ""
                )
                # Compose a *fresh* enrichment artifact, if any; stale or absent -> stub.
                enrichment = enrichment_artifact.load_fresh(
                    enrichment_dir, source_id, normalized_markdown,
                    prompt_input=enrichment_artifact.enrich_prompt_input_for(manifest, logs_dir),
                )
                # Project the source's active claims (3b) and concept/entity mentions
                # (slice 4) from the graph; resolve labels worker-side, pass plain data to
                # the pure renderer.
                claims = None
                items = None
                suggested_items = None
                keywords: list[str] = []
                if gconn is not None:
                    claims = [
                        {"claim_id": cid, "title": _claim_label(claims_dir, cid)}
                        for cid in graph.claims_for_source(gconn, source_id)
                        # ADR-0048: omit hidden claims from the Source-page Claims section (a rendered
                        # discovery surface, like the partner contradiction sections) — the derived_from
                        # edge stays active in the graph for raw inspection.
                        if (graph.get_node(gconn, cid) or {}).get("status") != "hidden"
                    ]
                    # ADR-0059: project the source's knowledge items; the renderer groups them by
                    # item_type (sentinel under the QA bucket, never a taxonomy group). ADR-0066 review
                    # round 6: only the tags the document keeps are its (linked) tags; unkept
                    # suggestions are listed apart, unlinked.
                    curated, suggested = graph.source_tag_projection(gconn, source_id)
                    items = [
                        {"target": f"Items/{m['slug']}",
                         "title": _node_title(wiki_dir, "item", m["slug"]),
                         "item_type": m.get("item_type")}
                        for m in curated if m["node_type"] == "item"
                    ]
                    suggested_items = [
                        {"target": f"Items/{m['slug']}",
                         "title": _node_title(wiki_dir, "item", m["slug"])}
                        for m in suggested
                    ]
                    keywords = _keywords(enrichment_dir, source_id, curated, suggested, wiki_dir)
                candidate = wiki_render.render_source_page(
                    template, manifest, normalized_markdown,
                    summary_max=summary_max, summary_min=summary_min,
                    enrichment=enrichment, claims=claims, items=items,
                    suggested_items=suggested_items, raw_url=raw_base + source_id,
                    title=titles.effective_title(   # ADR-0066 part 3
                        manifest, enrichment=enrichment,
                        hint=titles.read_metadata_hint(logs_dir, source_id)),
                    keywords=keywords,
                    names=authors.linked_names(manifest),   # ADR-0066 part 3 slice 4
                )
            except Exception as exc:  # one page's failure must not abort the run
                return {"source_id": source_id, "error": f"{type(exc).__name__}: {exc}"}

            # Idempotent: skip when the rendered page is unchanged (input fingerprint).
            if not force and page_path.exists():
                existing_fp = wiki_render.parse_frontmatter(
                    page_path.read_text(encoding="utf-8")
                ).get("input_fingerprint")
                candidate_fp = wiki_render.parse_frontmatter(candidate).get("input_fingerprint")
                if existing_fp and existing_fp == candidate_fp:
                    return "unchanged"
            page_path.write_text(candidate, encoding="utf-8")
            return "generated"

        for manifest in manifests:
            source_id = manifest["source_id"]
            if manifest.get("ingestion_status") not in _GENERATED_STATUSES:
                skipped_not_extracted += 1
                continue
            considered += 1
            with write_lock.hold(root / "reviews"):
                # ADR-0068: record the page (renewing the run's end refresh, which another
                # publication may have cleared meanwhile) before writing it, and acknowledge it once
                # written; a page that failed stays queued for the end refresh to retry.
                entry = run.intent(sources=[source_id]) if run is not None else None
                outcome = _render_one(load_manifest(manifests_dir, source_id) or manifest)
                if run is not None and not isinstance(outcome, dict):
                    run.done(entry)
            if isinstance(outcome, dict):
                errors.append(outcome)
            elif outcome == "unchanged":
                skipped_unchanged += 1
            else:
                generated += 1

        summary: dict[str, Any] = {
            "job_id": job_id,
            "sources_considered": considered,
            "generated": generated,
            "skipped_unchanged": skipped_unchanged,
            "skipped_not_extracted": skipped_not_extracted,
            "manifests_skipped_invalid": len(skipped_invalid),  # quarantined non-canonical ids (tamper)
            "errors": len(errors),
            "error_details": errors,
            "generated_at": now,
        }

        status = "succeeded" if not errors else "partial"
        if run is not None:
            pub = run.finish()
            summary["index_rebuilt"] = pub["index_rebuilt"]
            summary["keyword_reindexed"] = pub["keyword_reindexed"]
            summary["publication"] = publication.summary(pub)
            status = publication.job_status(status, pub)
        summary["status"] = status
        _append_log(wiki_dir, now, summary)

        if conn is not None:
            db.update_job(
                conn, job_id, status=status, finished_at=iso_now(), metadata=summary,
            )
        return summary
    except Exception as exc:
        if conn is not None:
            db.update_job(
                conn, job_id, status="failed", finished_at=iso_now(), error_message=str(exc)
            )
        raise
    finally:
        if gconn is not None:
            gconn.close()
        if conn is not None:
            conn.close()
