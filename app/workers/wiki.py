#!/usr/bin/env python3
"""Phase 3 wiki worker: generate deterministic Source pages from the normalized layer.

For every source whose manifest reports `extracted`/`partial`, render
`wiki/Sources/<source_id>.md` from `templates/source.md` (ADR-0015/0016), rebuild
`wiki/index.md` (reusing scripts/rebuild_index.py), append `wiki/log.md`, and record a
`generate_wiki` job. Deterministic and offline: identical inputs yield byte-identical
pages. Idempotent — a page whose recomputed input_fingerprint matches the stored one is
skipped unless `force` is given (ADR-0023).

The wiki layer is mutable local data (ADR-0014); this worker writes only under wiki/ and
db/jobs.sqlite, never under raw/ or normalized/.
"""
from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from app.backend import db
from app.backend.manifests import iso_now, list_manifests
from app.workers import enrichment_artifact, wiki_render

_GENERATED_STATUSES = {"extracted", "partial"}


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


def _rebuild_index(root: Path) -> bool:
    """Rebuild wiki/index.md via the existing deterministic script. Best-effort."""
    script = root / "scripts" / "rebuild_index.py"
    if not script.exists():
        return False
    result = subprocess.run([sys.executable, str(script), str(root)])
    return result.returncode == 0


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
    summary_max: int = 320,
    summary_min: int = 40,
    rebuild_index: bool = True,
    record_job: bool = True,
) -> dict[str, Any]:
    """Generate Source pages for pending (or selected) sources; return a run summary."""
    root = Path(root).resolve()
    manifests_dir = Path(manifests_dir) if manifests_dir else root / "raw" / "manifests"
    jobs_db = Path(jobs_db) if jobs_db else root / "db" / "jobs.sqlite"
    wiki_dir = Path(wiki_dir) if wiki_dir else root / "wiki"
    templates_dir = Path(templates_dir) if templates_dir else root / "templates"
    markdown_dir = Path(markdown_dir) if markdown_dir else root / "normalized" / "markdown"
    enrichment_dir = (
        Path(enrichment_dir) if enrichment_dir else root / "normalized" / "enrichment"
    )
    sources_dir = wiki_dir / "Sources"
    sources_dir.mkdir(parents=True, exist_ok=True)

    template = (templates_dir / "source.md").read_text(encoding="utf-8")
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

    try:
        manifests = list_manifests(manifests_dir)
        if source_ids is not None:
            wanted = set(source_ids)
            manifests = [m for m in manifests if m.get("source_id") in wanted]

        considered = 0
        generated = 0
        skipped_unchanged = 0
        skipped_not_extracted = 0
        errors: list[dict[str, str]] = []

        for manifest in manifests:
            source_id = manifest["source_id"]
            if manifest.get("ingestion_status") not in _GENERATED_STATUSES:
                skipped_not_extracted += 1
                continue
            considered += 1
            page_path = sources_dir / f"{source_id}.md"
            try:
                md_path = markdown_dir / f"{source_id}.md"
                normalized_markdown = (
                    md_path.read_text(encoding="utf-8") if md_path.exists() else ""
                )
                # Compose a *fresh* enrichment artifact, if any; stale or absent -> stub.
                enrichment = enrichment_artifact.load_fresh(
                    enrichment_dir, source_id, normalized_markdown
                )
                candidate = wiki_render.render_source_page(
                    template, manifest, normalized_markdown,
                    summary_max=summary_max, summary_min=summary_min,
                    enrichment=enrichment,
                )
            except Exception as exc:  # one page's failure must not abort the run
                errors.append({"source_id": source_id, "error": f"{type(exc).__name__}: {exc}"})
                continue

            # Idempotent: skip when the rendered page is unchanged (input fingerprint).
            if not force and page_path.exists():
                existing_fp = wiki_render.parse_frontmatter(
                    page_path.read_text(encoding="utf-8")
                ).get("input_fingerprint")
                candidate_fp = wiki_render.parse_frontmatter(candidate).get("input_fingerprint")
                if existing_fp and existing_fp == candidate_fp:
                    skipped_unchanged += 1
                    continue
            page_path.write_text(candidate, encoding="utf-8")
            generated += 1

        summary: dict[str, Any] = {
            "job_id": job_id,
            "sources_considered": considered,
            "generated": generated,
            "skipped_unchanged": skipped_unchanged,
            "skipped_not_extracted": skipped_not_extracted,
            "errors": len(errors),
            "error_details": errors,
            "generated_at": now,
        }

        if rebuild_index:
            summary["index_rebuilt"] = _rebuild_index(root)
        _append_log(wiki_dir, now, summary)

        if conn is not None:
            db.update_job(
                conn, job_id,
                status="succeeded" if not errors else "partial",
                finished_at=iso_now(), metadata=summary,
            )
        return summary
    except Exception as exc:
        if conn is not None:
            db.update_job(
                conn, job_id, status="failed", finished_at=iso_now(), error_message=str(exc)
            )
        raise
    finally:
        if conn is not None:
            conn.close()
