#!/usr/bin/env python3
"""Phase 3.5a enrichment worker: per-source LLM summary + tags (ADR-0025/0026/0027/0028).

For every extracted/partial source, build an untrusted-data prompt from its normalized
Markdown, ask the LLM for a schema-valid summary + tags through `LLMClient.parse`, and write
the result to the per-source enrichment artifact `normalized/enrichment/<source_id>.json`.
The deterministic wiki worker composes that artifact into the Source page later — this worker
never edits `wiki/`.

Supervised and synchronous, the same shape as the extract/wiki workers. Idempotent via the
artifact's `input_fingerprint`. With no API key for the configured provider, enrichment is
skipped and sources stay `summary_status: stub`. A source whose parse fails the schema gate
after retries is dropped (logged), and likewise stays a stub.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from app.backend import db
from app.backend.manifests import iso_now, list_manifests
from app.llm import prompts
from app.llm.client import LLMClient, ParseError
from app.workers import enrichment_artifact as art
from app.workers.wiki_render import title_from_filename

_ENRICHABLE_STATUSES = {"extracted", "partial"}


def enrich_sources(
    root: Path,
    *,
    client: LLMClient,
    model_ref: str,
    source_ids: list[str] | None = None,
    force: bool = False,
    manifests_dir: Path | None = None,
    jobs_db: Path | None = None,
    markdown_dir: Path | None = None,
    enrichment_dir: Path | None = None,
    record_job: bool = True,
) -> dict[str, Any]:
    """Enrich pending (or selected) sources with a summary + tags; return a run summary."""
    root = Path(root).resolve()
    manifests_dir = Path(manifests_dir) if manifests_dir else root / "raw" / "manifests"
    jobs_db = Path(jobs_db) if jobs_db else root / "db" / "jobs.sqlite"
    markdown_dir = Path(markdown_dir) if markdown_dir else root / "normalized" / "markdown"
    enrichment_dir = (
        Path(enrichment_dir) if enrichment_dir else root / "normalized" / "enrichment"
    )

    now = iso_now()
    job_id = f"job_{uuid.uuid4().hex[:16]}"
    conn = None
    if record_job:
        db.init_db(jobs_db)
        conn = db.connect(jobs_db)
        db.insert_job(
            conn, job_id=job_id, job_type="enrich", status="running",
            created_at=now, started_at=now,
        )

    try:
        manifests = list_manifests(manifests_dir)
        if source_ids is not None:
            wanted = set(source_ids)
            manifests = [m for m in manifests if m.get("source_id") in wanted]

        considered = 0
        enriched = 0
        skipped_fresh = 0
        skipped_not_extracted = 0
        skipped_empty = 0
        skipped_no_key = 0
        errors: list[dict[str, str]] = []

        # No credential for the configured provider -> skip; sources stay stubs (ADR-0025).
        has_key = client.provider_available(model_ref)

        for manifest in manifests:
            source_id = manifest["source_id"]
            if manifest.get("ingestion_status") not in _ENRICHABLE_STATUSES:
                skipped_not_extracted += 1
                continue
            considered += 1

            if not has_key:
                skipped_no_key += 1
                continue

            md_path = markdown_dir / f"{source_id}.md"
            normalized_markdown = (
                md_path.read_text(encoding="utf-8") if md_path.exists() else ""
            )
            if not normalized_markdown.strip():
                skipped_empty += 1
                continue

            fingerprint = art.artifact_fingerprint(normalized_markdown, model_ref)
            artifact_path = art.artifact_path(enrichment_dir, source_id)
            if not force and artifact_path.exists():
                try:
                    existing = json.loads(artifact_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    existing = {}
                if existing.get("input_fingerprint") == fingerprint:
                    skipped_fresh += 1
                    continue

            title = title_from_filename(manifest.get("original_filename", source_id))
            messages = prompts.build_messages(title, normalized_markdown)
            try:
                result = client.parse(
                    messages, prompts.SUMMARY_TAGS_SCHEMA, model_ref,
                    schema_version=art.SCHEMA_VERSION, prompt_version=art.PROMPT_VERSION,
                )
            except ParseError as exc:
                errors.append({"source_id": source_id, "error": str(exc)})
                continue

            artifact = {
                "source_id": source_id,
                "schema_version": art.SCHEMA_VERSION,
                "prompt_version": art.PROMPT_VERSION,
                "model_ref": model_ref,
                "input_fingerprint": fingerprint,
                "generation_status": "enriched",
                "generated_at": now,
                "summary": result["summary"],
                "tags": result["tags"],
            }
            enrichment_dir.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text(
                json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            enriched += 1

        summary: dict[str, Any] = {
            "job_id": job_id,
            "model_ref": model_ref,
            "sources_considered": considered,
            "enriched": enriched,
            "skipped_fresh": skipped_fresh,
            "skipped_not_extracted": skipped_not_extracted,
            "skipped_empty": skipped_empty,
            "skipped_no_key": skipped_no_key,
            "errors": len(errors),
            "error_details": errors,
            "enriched_at": now,
        }

        if errors:
            status = "partial"
        elif not has_key and considered > 0:
            # Whole run gated on a missing provider credential — a config gap an operator
            # should see at a glance, distinct from a clean "nothing to do" run (ADR-0025).
            status = "skipped"
        else:
            status = "succeeded"

        if conn is not None:
            db.update_job(
                conn, job_id, status=status, finished_at=iso_now(), metadata=summary,
            )
        summary["status"] = status
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
