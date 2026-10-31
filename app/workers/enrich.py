#!/usr/bin/env python3
"""Phase 3.5a enrichment worker: per-source LLM summary + tags (ADR-0025/0026/0027/0028).

For every extracted/partial source, build an untrusted-data prompt from its normalized
Markdown, ask the LLM for a schema-valid summary + tags through `LLMClient.parse`, and write
the result to the per-source enrichment artifact `normalized/enrichment/<source_id>.json`.
The deterministic wiki worker composes that artifact into the Source page: each document's commit
publishes its Source page under the write lock, and the run ends with one refresh of `index.md` and
the keyword index (ADR-0068 decision 5, `app/workers/publication.py`).

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

from app.backend import db, taxonomy, write_lock
from app.backend.manifests import iso_now, valid_manifests
from app.llm import prompts
from app.llm.client import LLMClient, ParseError
from app.workers import enrichment_artifact as art
from app.workers import publication, titles
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
    logs_dir = markdown_dir.parent / "extraction_logs"   # the PDF metadata hint (ADR-0066 part 3)
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

    run = publication.Run(root, "enrich")
    try:
        manifests, skipped_invalid = valid_manifests(manifests_dir)
        if source_ids is not None:
            wanted = set(source_ids)
            manifests = [m for m in manifests if m.get("source_id") in wanted]

        considered = 0
        enriched = 0
        skipped_fresh = 0
        skipped_not_extracted = 0
        skipped_empty = 0
        skipped_no_key = 0
        input_changed = 0
        errors: list[dict[str, str]] = []

        # ADR-0063: the tier value is an ordered model chain; resolve once per run to the first
        # available concrete model_ref (availability-only, fixed for the run). No available member
        # -> skip, sources stay stubs (ADR-0025); keep the first-preference ref so any fingerprint/
        # record stays a valid concrete ref. `chain_refs` (pre-resolution) drives sticky freshness.
        chain_refs = [c.strip() for c in model_ref.split(",") if c.strip()]
        model_ref, has_key = client.resolve_run_model(model_ref)

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

            # ADR-0066 part 3: the file title and the extraction log's PDF hint are prompt inputs.
            # Captured once: the messages and the fingerprint are built from the same inputs, and
            # the commit checks them again under the lock.
            file_title = title_from_filename(manifest.get("original_filename", source_id))
            hint = titles.read_metadata_hint(logs_dir, source_id)
            prompt_input = art.enrich_prompt_input(file_title, hint)
            artifact_path = art.artifact_path(enrichment_dir, source_id)
            if not force and artifact_path.exists():
                try:
                    existing = json.loads(artifact_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    existing = {}
                # ADR-0063 sticky-to-chain: recompute the fingerprint under the artifact's OWN recorded
                # model; an availability flip alone (recorded model still in chain) never restales.
                if art.chain_fresh(existing, chain_refs,
                                   lambda m: art.artifact_fingerprint(
                                       normalized_markdown, m, prompt_input=prompt_input)):
                    skipped_fresh += 1
                    continue
            fingerprint = art.artifact_fingerprint(normalized_markdown, model_ref,
                                                   prompt_input=prompt_input)

            messages = prompts.build_messages(file_title, normalized_markdown, metadata=hint)
            try:
                result = client.parse(
                    messages, prompts.SUMMARY_TAGS_SCHEMA, model_ref,
                    schema_version=art.SCHEMA_VERSION, prompt_version=art.PROMPT_VERSION,
                    operation=source_id,
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
                "prompt_input": prompt_input,
                **_suggestions(result),
            }
            # ADR-0068: the model call above ran outside the write lock; the commit takes it, and
            # writes nothing if a re-extraction installed new text meanwhile (the next run enriches it).
            # Then it publishes the document's Source page before releasing the lock (decision 5).
            with write_lock.hold(root / "reviews"):
                # The result is valid only for the inputs the model saw: the text, and (ADR-0066
                # part 3) the file title and the PDF hint (a re-extraction may record a new hint).
                if ((md_path.read_text(encoding="utf-8") if md_path.exists() else "")
                        != normalized_markdown
                        or art.enrich_prompt_input_for(manifest, logs_dir) != prompt_input):
                    input_changed += 1
                    continue
                intent = run.intent(sources=[source_id])
                enrichment_dir.mkdir(parents=True, exist_ok=True)
                artifact_path.write_text(
                    json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                run.publish(intent, sources=[source_id])
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
            "skipped_input_changed": input_changed,
            "manifests_skipped_invalid": len(skipped_invalid),
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
        pub = run.finish()                    # index.md + the keyword index, once (decision 5)
        summary["index_rebuilt"] = bool(pub and pub["index_rebuilt"])
        summary["keyword_reindexed"] = bool(pub and pub["keyword_reindexed"])
        summary["publication"] = publication.summary(pub)
        status = publication.job_status(status, pub)

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


def _suggestions(result: dict[str, Any]) -> dict[str, Any]:
    """The validated Enrich output stored in the artifact (ADR-0066 part 3): the summary, the
    suggested title / ordered authors / publisher (provenance suggestions; only a human
    confirmation copies them into the manifest), and at most ENRICH_TAGS_PROMPT_MAX typed tags,
    cleaned and de-duplicated, the single canonical tag list. Unknown values are None / []."""
    tags: list[dict[str, str]] = []
    seen: set[str] = set()
    for tag in result.get("tags") or []:
        if not isinstance(tag, dict):
            continue
        name = titles.clean_tag_name(tag.get("name"))
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        item_type = tag.get("item_type")
        tags.append({"name": name, "item_type": item_type if taxonomy.is_item_type(item_type)
                     else taxonomy.UNCLASSIFIED})
        if len(tags) >= prompts.ENRICH_TAGS_PROMPT_MAX:
            break
    return {
        "summary": result["summary"],
        "title": titles.clean_text(result.get("title")) or None,
        "authors": titles.clean_names(result.get("authors")),
        "publisher": titles.clean_text(result.get("publisher"), max_chars=titles.MAX_NAME_CHARS)
        or None,
        "tags": tags,
    }
