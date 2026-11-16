#!/usr/bin/env python3
"""Phase 2 extraction worker: normalize raw sources into the normalized layer.

For every extractable source catalogued in ``raw/manifests/`` this worker extracts
text/tables, writes deterministic per-source artifacts under ``normalized/`` (Markdown,
chunks, tables, an extraction log), and records extraction state back on the manifest
(ADR-0011). It runs offline with no API keys, treats every input as untrusted data
(size/timeout/zip-bomb caps, ADR-0010) — the parse runs in a bounded child process
(`app/workers/extract_runner.py`, ADR-0074 decision 5) — and is idempotent: an already-extracted,
unchanged source is skipped unless ``force`` is given, and so is a source whose latest attempt was
an explicit deterministic rejection while nothing that bounded it changed (an attempt record,
`app/workers/extract_attempts.py`, ADR-0074 decision 6). One ``extract`` job records the run
summary; per-source detail lives on the manifest, in the extraction log and the attempt record.

No raw file is ever modified; writes go only to ``normalized/``, ``raw/manifests/``,
and ``db/jobs.sqlite``, plus publication (ADR-0068 decision 5): each document's install publishes
its Source page under the write lock, and the run ends with one refresh of ``wiki/index.md`` and
the keyword index (`app/workers/publication.py`).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.backend import db, durable_write, write_lock
from app.backend import extraction_install as xi
from app.backend.manifests import (
    EXTRACTION_FIELDS,
    apply_extraction_state,
    OK,
    iso_now,
    read_manifest,
    save_manifest,
    scan_manifests,
    sha256_file,
)
from app.backend.paths import safe_under
from app.workers import extract_attempts as attempts
from app.workers import extract_runner, publication
from app.workers.chunking import assemble
# The parse itself (`extract_parse`) runs only in the extraction child; the per-format extractor
# modules (pypdf/python-docx/beautifulsoup4/pandas) are imported there lazily, so the base API and
# the intake/query workflows boot without the optional extraction extras installed.
from app.workers.extract_parse import (  # noqa: F401  (re-exported for callers)
    SUPPORTED,
    ExtractionError,
    MissingExtractionDependency,
)
from app.workers.extractors import Extraction  # noqa: F401  (re-exported for callers)

# Extractor implementation provenance, recorded in every extraction log (ADR-0054 decision 4).
# Observability only — nothing gates, lints, or auto-reextracts on it; old logs are never mutated,
# and a missing field means "pre-marker / older extractor". Increment on any behavior change to
# extraction output (v1 = PDF line-break de-hyphenation + soft-hyphen strip; v2 = the PDF metadata
# title/author hint in the log, ADR-0066 part 3).
EXTRACT_CODE_VERSION = 2

# A non-paginated source (HTML/DOCX/Markdown/CSV) yielding less than this much text is
# essentially empty: reported partial with needs_ocr rather than a clean extraction
# (Phase 2 Plan §10). The paginated (PDF) floor lives in extractors/pdf.py.
_NON_PAGINATED_MIN_CHARS = 16

# The untrusted parse runs in an extraction child under a wall-clock deadline (ADR-0074 decision 5;
# `app/workers/extract_runner.py`), the one path for the CLI and the API. A seam for tests.
_parse = extract_runner.parse


def is_supported(extension: str) -> bool:
    return extension.lower() in SUPPORTED


# --- output writing ---------------------------------------------------------


def _stage_outputs(
    dirs: dict[str, Path], source_id: str, attempt_id: str, markdown_text: str, chunks: list[Any],
    table_files: list[tuple[str, str]],
) -> dict[str, Any]:
    """Write one attempt's complete generation durably to its own staging directory, outside the
    write lock (ADR-0068); return its output inventory for the install journal (ADR-0074
    decision 8). The per-source tables directory is always installed, even empty."""
    chunk_lines = [json.dumps(c.to_dict(), ensure_ascii=False) for c in chunks]
    body = "\n".join(chunk_lines) + ("\n" if chunk_lines else "")
    return xi.stage_generation(
        dirs["layout"], source_id, attempt_id, markdown=markdown_text.encode("utf-8"),
        chunks=body.encode("utf-8"),
        tables=[(name, content.encode("utf-8")) for name, content in table_files])


def _write_log(dirs: dict[str, Path], source_id: str, log: dict[str, Any]) -> None:
    """The extraction log of an attempt that installs no outputs (a first-time failure)."""
    durable_write.write_bytes(dirs["extraction_logs"] / f"{source_id}.json", xi.log_bytes(log))


# --- single source ----------------------------------------------------------


def _safe_raw_file(root: Path, manifest: dict[str, Any]) -> Path | None:
    """Resolve a manifest's raw file under ``root/raw``, or ``None`` if the (untrusted) ``relative_raw_path``
    is absolute, contains ``..``, or escapes ``raw/`` (ADR-0009). Both the extract and idempotent-skip
    paths route through this so the raw-containment contract is enforced identically — matching
    ``app.workers.lint`` and ``scripts/validate_raw_integrity.py``, which resolve the same field via
    ``safe_under``. ``relative_raw_path`` must be repository-relative (CONTEXT.md); an absolute path,
    even one pointing inside ``raw/``, is rejected."""
    return safe_under(root, root / "raw", manifest.get("relative_raw_path", ""))


def _extract_one(
    manifest: dict[str, Any],
    root: Path,
    dirs: dict[str, Path],
    *,
    max_file_mb: int,
    timeout_s: int,
    target_chars: int,
    max_chars: int,
    attempt_id: str,
) -> tuple[dict[str, Any], bool, dict[str, Any] | None]:
    """Extract one source, outside the write lock: nothing it produces is visible yet.

    Returns ``(log, persisted, staged)``. ``persisted`` is False when a failed re-extraction
    left a prior successful result untouched (last-good preserved), so the caller must
    not rewrite the manifest or the log. ``staged`` is the output inventory of the generation
    waiting in the attempt's staging directory for the journaled install, or None.
    ``log['status']`` drives the run summary either way.
    """
    source_id = manifest["source_id"]
    started = iso_now()
    raw_file = _safe_raw_file(root, manifest)
    # A prior successful extraction we must not destroy if this run fails.
    prior_success = (
        manifest.get("ingestion_status") in {"extracted", "partial"}
        and (dirs["markdown"] / f"{source_id}.md").exists()
    )

    staged: dict[str, Any] | None = None
    log: dict[str, Any] = {
        "source_id": source_id,
        # The attempt that wrote this log: a unique id per generation (ADR-0074 decision 6 binds a
        # failed attempt's record to it — timestamps have second resolution).
        "attempt_id": attempt_id,
        "status": "error",
        "tool": None,
        "tool_version": None,
        "extract_code_version": EXTRACT_CODE_VERSION,
        "started_at": started,
        "finished_at": started,
        "input_size_bytes": 0,
        "page_count": None,
        "text_char_count": 0,
        "chunk_count": 0,
        "table_count": 0,
        "warnings": [],
        "error": None,
        "skip_reason": None,
        "metadata": {},
    }

    try:
        # Manifests are untrusted on-disk data; re-check path confinement before reading so a
        # hand-edited absolute/relative path can never escape the raw repository (ADR-0009). `None`
        # means absolute / `..` / outside raw/ — a hard path_escape (the shared `_safe_raw_file` guard).
        if raw_file is None:
            raise ExtractionError(
                "raw path is absolute, contains parent traversal, or escapes the raw repository",
                "path_escape")
        if not raw_file.is_file():
            raise ExtractionError("raw file not found", "missing_raw_file")
        size = raw_file.stat().st_size
        log["input_size_bytes"] = size
        if size > max_file_mb * 1024 * 1024:
            raise ExtractionError(f"file exceeds {max_file_mb} MB", "oversize")
        # Verify the bytes still match the manifest before producing evidence under this
        # content-derived source_id; a changed file must be re-scanned, not extracted.
        if sha256_file(raw_file) != manifest.get("sha256"):
            raise ExtractionError(
                "raw file checksum does not match manifest", "checksum_mismatch"
            )

        # The parse, its ZIP checks included, in a child under the deadline (decision 5).
        extraction = _parse(raw_file, source_id, timeout_s=timeout_s)

        markdown_text, chunks = assemble(
            source_id, extraction.elements, target_chars=target_chars, max_chars=max_chars
        )
        staged = _stage_outputs(dirs, source_id, attempt_id, markdown_text, chunks,
                                extraction.tables)

        log.update(
            status=extraction.status,
            tool=extraction.tool,
            tool_version=extraction.tool_version,
            finished_at=iso_now(),
            page_count=extraction.page_count,
            text_char_count=len(markdown_text),
            chunk_count=len(chunks),
            table_count=len(extraction.tables),
            warnings=extraction.warnings,
            metadata=dict(extraction.metadata),
        )
    except ExtractionError as exc:
        log.update(status="error", finished_at=iso_now(), error=str(exc), skip_reason=exc.skip_reason)
    except Exception as exc:  # any extractor/parse failure: log and continue the run
        log.update(status="error", finished_at=iso_now(), error=f"{type(exc).__name__}: {exc}")

    # Non-paginated source with (almost) no extractable text → partial/needs_ocr.
    if (
        log["status"] == "extracted"
        and log["page_count"] is None
        and log["text_char_count"] < _NON_PAGINATED_MIN_CHARS
    ):
        log["status"] = "partial"
        if "needs_ocr" not in log["warnings"]:
            log["warnings"] = [*log["warnings"], "needs_ocr"]

    if log["status"] != "error":
        apply_extraction_state(
            manifest,
            ingestion_status=log["status"],
            extracted_at=log["finished_at"],
            extraction_tool=log["tool"],
            extraction_tool_version=log["tool_version"],
            text_char_count=log["text_char_count"],
            chunk_count=log["chunk_count"],
            page_count=log["page_count"],
        )
        return log, True, staged

    # Failure. If a prior successful extraction exists, keep its artifacts, manifest,
    # and log untouched — the failure is surfaced in the run/job summary, not by
    # destroying usable evidence (last-good wins). Only flip to error when there is no
    # good prior state to protect.
    if staged is not None:                                # failed after staging: discard it
        xi.discard_stage(dirs["layout"], source_id, attempt_id)
    if prior_success:
        log["preserved_prior"] = True
        return log, False, None

    apply_extraction_state(
        manifest,
        ingestion_status="error",
        extracted_at=None,
        extraction_tool=log["tool"],
        extraction_tool_version=log["tool_version"],
        text_char_count=log["text_char_count"],
        chunk_count=log["chunk_count"],
        page_count=log["page_count"],
    )
    return log, True, None


# --- run --------------------------------------------------------------------


def _iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")


def _already_extracted(manifest: dict[str, Any], root: Path, markdown_dir: Path,
                       journal: xi.JournalRead | None = None) -> bool:
    """True only when a prior extraction is on disk AND the raw file has not drifted, and no install
    journal blocks the outputs (an unrecoverable install is retried: decision 8).

    The skip stays cheap (no re-hash) but is gated on the raw file's size/mtime still
    matching the manifest. A drifted (or missing) raw file is NOT skipped, so
    _extract_one re-hashes it and surfaces a checksum_mismatch instead of silently
    generating from stale normalized evidence (ADR-0024).
    """
    if manifest.get("ingestion_status") not in {"extracted", "partial"}:
        return False
    if journal is not None and not journal.usable:
        return False
    if not (markdown_dir / f"{manifest['source_id']}.md").exists():
        return False
    # An untrusted relative_raw_path that escapes raw/ must NOT be stat'd or treated as a valid skip
    # (symmetric with _extract_one): fall through so _extract_one rejects it as path_escape (ADR-0009).
    raw_file = _safe_raw_file(root, manifest)
    if raw_file is None:
        return False
    if not raw_file.is_file():
        return False
    try:
        if raw_file.stat().st_size != manifest.get("size_bytes"):
            return False
        if _iso_mtime(raw_file) != manifest.get("modified_at"):
            return False
    except OSError:
        return False
    return True


def _note_attempt(normalized: Path, manifest: dict[str, Any], log: dict[str, Any],
                  attempt_id: str, digest: str | None, bounds: dict[str, Any] | None,
                  journal_usable: bool, warnings: list[dict[str, Any]]) -> str | None:
    """Record a failed attempt beside the extraction log, or clear the record after a success
    (ADR-0074 decision 6); return the failure's retry category (None after a success). The record
    is derived state: a write that fails is a warning, and the next run simply retries."""
    source_id = manifest["source_id"]
    try:
        if log["status"] != "error":
            attempts.clear(normalized, source_id)
            return None
        retained = bool(log.get("preserved_prior"))
        record = attempts.new_record(manifest, log, attempt_id=attempt_id,
                                     prior_retained=retained,
                                     prior_usable=retained and journal_usable,
                                     generation=attempts.generation(normalized, source_id),
                                     fingerprint=digest, inputs=bounds)
        attempts.write(normalized, record)
        return record["category"]
    except OSError as exc:
        warnings.append({"source_id": source_id,
                         "warning": f"attempt_record_unwritten: {type(exc).__name__}"})
        return None if log["status"] != "error" else attempts.RETRY


def _published(run: publication.Run, source_id: str, intent: str | None) -> bool:
    """Publish one document's Source page from its install's intent (under the write lock) and say
    whether it is published, so its install journal may close."""
    out = run.publish(intent, sources=[source_id], refresh=True) if intent else None
    if out is None:
        out = publication.publish(run.root, sources=[source_id], origin=run.origin)
    return out["status"] == publication.PUBLISHED


def _commit(root: Path, run: publication.Run, dirs: dict[str, Any], manifests_dir: Path,
            manifest: dict[str, Any], log: dict[str, Any], staged: dict[str, Any] | None,
            attempt_id: str, config: dict[str, int]) -> str | None:
    """Commit one attempt under the write lock; None, or the error reason (nothing installed, or an
    install left to recovery). With outputs: intent -> journal -> verified renames, log, manifest
    fields -> `installed` -> publication -> journal closed (ADR-0074 decision 8)."""
    layout: xi.Layout = dirs["layout"]
    source_id = manifest["source_id"]
    with write_lock.hold(root / "reviews"):
        now_read = read_manifest(manifests_dir, source_id)
        if now_read.state != OK:
            # ADR-0074 decision 2: gone or damaged since the scan — never recreated or overwritten
            # from the scan's copy; nothing is installed.
            if staged is not None:
                xi.discard_stage(layout, source_id, attempt_id)
            return f"manifest_{now_read.state}"
        previous = xi.read_journal(layout, source_id)
        if staged is None:
            if not previous.usable:
                return xi.INCOMPLETE if previous.state == xi.INSTALLING else None
            intent = run.intent(sources=[source_id])
            _write_log(dirs, source_id, log)
            current = now_read.record
            for key in EXTRACTION_FIELDS:
                current[key] = manifest.get(key)
            save_manifest(manifests_dir, current)
            run.publish(intent, sources=[source_id])
            return None
        intent = run.intent(sources=[source_id])
        entry = xi.new_journal(
            source_id, attempt_id, source_sha256=now_read.record["sha256"], config=config,
            intent=intent, outputs=staged,
            obsolete_tables=xi.obsolete_tables(layout, source_id, staged["tables"]), log=log,
            manifest_fields={k: manifest.get(k) for k in EXTRACTION_FIELDS},
            supersedes=xi.supersedes_for(previous), now=iso_now())
        try:
            xi.begin(layout, entry, previous)
        except xi.InstallBlocked as exc:
            xi.discard_stage(layout, source_id, attempt_id)
            return exc.reason
        try:
            entry = xi.install(layout, manifests_dir, entry, now=iso_now())
        except xi.InstallBlocked as exc:
            return exc.reason
        except OSError as exc:                    # the journal stays `installing` for recovery
            return f"{xi.INCOMPLETE}:{type(exc).__name__}"
        if entry["state"] != xi.INSTALLED:
            return f"{xi.INCOMPLETE}:{entry['reason']}"
        out = run.publish(intent, sources=[source_id])
        if out is not None and out["status"] == publication.PUBLISHED:
            xi.close(layout, source_id)
    return None


def extract_sources(
    root: Path,
    *,
    source_ids: list[str] | None = None,
    force: bool = False,
    manifests_dir: Path | None = None,
    jobs_db: Path | None = None,
    normalized_dir: Path | None = None,
    max_file_mb: int = 50,
    timeout_s: int = 120,
    target_chars: int = 1000,
    max_chars: int = 2000,
    record_job: bool = True,
) -> dict[str, Any]:
    """Extract pending (or selected) sources; return a run summary (Phase 2 Plan §9)."""
    if not timeout_s > 0:          # the deadline always applies (ADR-0074 decision 5)
        raise ValueError(f"timeout_s must be positive, not {timeout_s!r}")
    root = Path(root).resolve()
    manifests_dir = Path(manifests_dir) if manifests_dir else root / "raw" / "manifests"
    jobs_db = Path(jobs_db) if jobs_db else root / "db" / "jobs.sqlite"
    normalized = Path(normalized_dir) if normalized_dir else root / "normalized"
    dirs = {
        "markdown": normalized / "markdown",
        "chunks": normalized / "chunks",
        "tables": normalized / "tables",
        "extraction_logs": normalized / "extraction_logs",
    }
    for path in dirs.values():
        durable_write.make_dirs(path)
    layout = xi.Layout(normalized)
    dirs["layout"] = layout                               # staging, holding and install journals
    config = {"extract_code_version": EXTRACT_CODE_VERSION, "target_chars": target_chars,
              "max_chars": max_chars}

    now = iso_now()
    job_id = f"job_{uuid.uuid4().hex[:16]}"
    conn = None
    if record_job:
        db.init_db(jobs_db)
        conn = db.connect(jobs_db)
        db.insert_job(
            conn, job_id=job_id, job_type="extract", status="running",
            created_at=now, started_at=now,
        )

    run = publication.Run(root, "extract")
    try:
        # ADR-0074 decision 8 / 7.4: complete every open install journal, then remove only the
        # staging no journal refers to — before any new attempt, under vault lock -> write lock.
        with write_lock.hold(root / "reviews"):
            recovery = xi.recover(layout, manifests_dir, config, now=now,
                                  publish=lambda sid, intent: _published(run, sid, intent))
            cleanup = xi.cleanup_staging(layout)
        inventory = scan_manifests(manifests_dir)
        manifests = inventory.valid
        if source_ids is not None:
            wanted = set(source_ids)
            manifests = [m for m in manifests if m.get("source_id") in wanted]

        counts = {
            "extracted": 0, "partial": 0, "errors": 0,
            "skipped_unchanged": 0, "skipped_unsupported": 0, "skipped_known_failure": 0,
        }
        errors: list[dict[str, Any]] = []
        known: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        considered = 0

        for manifest in manifests:
            source_id = manifest["source_id"]
            if not is_supported(manifest.get("file_extension", "")):
                counts["skipped_unsupported"] += 1
                continue
            considered += 1
            journal = xi.read_journal(layout, source_id)
            if journal.state in (xi.UNREADABLE, xi.CORRUPT, xi.INSTALLING):
                # A damaged journal is unknown recovery state; an install recovery could not
                # complete stays as it is. Never extracted over (decision 8).
                reason = (xi.INCOMPLETE if journal.state == xi.INSTALLING
                          else f"install_journal_{journal.state}")
                counts["errors"] += 1
                errors.append({"source_id": source_id, "error": reason, "skip_reason": reason,
                               "preserved_prior": True})
                continue
            # Decision 6: everything that bounds this attempt, taken before it runs (a raw file
            # changing during the attempt then differs next time: a retry, the safe direction).
            digest, bounds = attempts.fingerprint(
                manifest, _safe_raw_file(root, manifest), max_file_mb=max_file_mb,
                timeout_s=timeout_s, target_chars=target_chars, max_chars=max_chars,
                code_version=EXTRACT_CODE_VERSION)
            if not force:
                # The latest failure is consulted BEFORE the unchanged shortcut: a failed
                # re-extraction keeps the prior outputs (which would read as unchanged), yet it
                # retries — or, a cached rejection, is skipped only while its fingerprint matches.
                record = attempts.current(normalized, manifest)
                if record is not None:
                    if journal.usable and attempts.known_failure(record, digest):
                        counts["skipped_known_failure"] += 1
                        known.append({"source_id": source_id, "skip_reason": record["reason"],
                                      "error": record["error"],
                                      "attempted_at": record["attempted_at"],
                                      "preserved_prior": record["prior_retained"]})
                        continue
                elif _already_extracted(manifest, root, dirs["markdown"], journal):
                    counts["skipped_unchanged"] += 1
                    continue

            attempt_id = xi.new_attempt_id()
            log, persisted, staged = _extract_one(
                manifest, root, dirs,
                max_file_mb=max_file_mb, timeout_s=timeout_s,
                target_chars=target_chars, max_chars=max_chars, attempt_id=attempt_id,
            )
            if persisted:
                # ADR-0068: install the outputs, the log and the manifest fields together under the
                # write lock, onto the manifest as it is now (a status changed meanwhile is kept),
                # then publish the document's Source page before releasing it (decision 5).
                failure = _commit(root, run, dirs, manifests_dir, manifest, log, staged,
                                  attempt_id, config)
                if failure is not None:
                    counts["errors"] += 1
                    errors.append({"source_id": source_id, "error": failure,
                                   "skip_reason": failure, "preserved_prior": True})
                    continue
            retry = _note_attempt(normalized, manifest, log, attempt_id, digest, bounds,
                                  journal.usable, warnings)

            status = log["status"]
            if status == "extracted":
                counts["extracted"] += 1
            elif status == "partial":
                counts["partial"] += 1
            else:
                counts["errors"] += 1
                errors.append({
                    "source_id": source_id,
                    "error": log["error"] or "unknown",
                    "skip_reason": log["skip_reason"],
                    "preserved_prior": log.get("preserved_prior", False),
                    "retry": retry,
                })
            for warn in log["warnings"]:
                warnings.append({"source_id": source_id, "warning": warn})

        summary: dict[str, Any] = {
            "job_id": job_id,
            "sources_considered": considered,
            "extracted": counts["extracted"],
            "partial": counts["partial"],
            "errors": counts["errors"],
            "skipped_unchanged": counts["skipped_unchanged"],
            "skipped_unsupported": counts["skipped_unsupported"],
            "skipped_known_failure": counts["skipped_known_failure"],
            **inventory.report(),
            "install_recovery": recovery,
            "staging_cleanup": cleanup,
            "error_details": errors,
            "known_failures": known,
            "warnings": warnings,
            "forced": force,
            "extracted_at": now,
        }
        # The end refresh (ADR-0068 decision 5): extraction changed searchable text, so the keyword
        # index (and index.md) refresh once here; the job succeeds only after it.
        pub = run.finish()
        summary["index_rebuilt"] = bool(pub and pub["index_rebuilt"])
        summary["keyword_reindexed"] = bool(pub and pub["keyword_reindexed"])
        summary["publication"] = publication.summary(pub)
        summary["status"] = publication.job_status("succeeded" if not errors else "partial", pub)

        if conn is not None:
            db.update_job(
                conn, job_id, status=summary["status"],
                finished_at=iso_now(), metadata=summary, warnings=warnings,
            )
        return summary
    except BaseException as exc:  # incl. Ctrl-C (UAT part 3 #3)
        if conn is not None:
            db.update_job(
                conn, job_id, status="failed", finished_at=iso_now(), error_message=db.failure_message(exc)
            )
        raise
    finally:
        if conn is not None:
            conn.close()
