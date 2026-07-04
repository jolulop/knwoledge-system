#!/usr/bin/env python3
"""Phase 2 extraction worker: normalize raw sources into the normalized layer.

For every extractable source catalogued in ``raw/manifests/`` this worker extracts
text/tables, writes deterministic per-source artifacts under ``normalized/`` (Markdown,
chunks, tables, an extraction log), and records extraction state back on the manifest
(ADR-0011). It runs offline with no API keys, treats every input as untrusted data
(size/timeout/zip-bomb caps, ADR-0010), and is idempotent: an already-extracted,
unchanged source is skipped unless ``force`` is given. One ``extract`` job records the
run summary; per-source detail lives on the manifest and in the extraction log.

No raw file is ever modified; writes go only to ``normalized/``, ``raw/manifests/``,
and ``db/jobs.sqlite``.
"""
from __future__ import annotations

import json
import shutil
import importlib
import signal
import threading
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.backend import db
from app.backend.manifests import (
    apply_extraction_state,
    iso_now,
    save_manifest,
    sha256_file,
    valid_manifests,
)
from app.backend.paths import safe_under
from app.workers.chunking import assemble
# Only the lightweight Extraction dataclass is imported eagerly. The per-format
# extractor modules (pypdf/python-docx/beautifulsoup4/pandas) are imported lazily in
# _dispatch so the base API and the intake/query workflows boot without the optional
# extraction extras installed.
from app.workers.extractors import Extraction  # noqa: F401  (re-exported for callers)

# Extractor implementation provenance, recorded in every extraction log (ADR-0054 decision 4).
# Observability only — nothing gates, lints, or auto-reextracts on it; old logs are never mutated,
# and a missing field means "pre-marker / older extractor". Increment on any behavior change to
# extraction output (v1 = PDF line-break de-hyphenation + soft-hyphen strip).
EXTRACT_CODE_VERSION = 1

# Extension -> extractor module name under app.workers.extractors (loaded lazily).
_TEXT_FORMATS = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".html": "html",
    ".htm": "html",
    ".md": "markdown",
    ".markdown": "markdown",
}
_TABLE_EXTENSIONS = {".csv", ".xlsx"}
_SUPPORTED = set(_TEXT_FORMATS) | _TABLE_EXTENSIONS
_ZIP_FORMATS = {".docx", ".xlsx"}

# A non-paginated source (HTML/DOCX/Markdown/CSV) yielding less than this much text is
# essentially empty: reported partial with needs_ocr rather than a clean extraction
# (Phase 2 Plan §10). The paginated (PDF) floor lives in extractors/pdf.py.
_NON_PAGINATED_MIN_CHARS = 16

# Decompression-bomb bounds for zipped formats (DOCX/XLSX).
_MAX_ZIP_ENTRIES = 10_000
_MAX_ZIP_UNCOMPRESSED = 512 * 1024 * 1024  # 512 MiB
_MAX_ZIP_RATIO = 200


class ExtractionError(Exception):
    """Recoverable per-source failure carrying a machine-readable skip reason."""

    def __init__(self, message: str, skip_reason: str) -> None:
        super().__init__(message)
        self.skip_reason = skip_reason


class MissingExtractionDependency(ExtractionError):
    """The optional extraction extras are not installed for the requested format."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            f"{detail}; extraction extras not installed — run "
            "`uv sync --extra extraction`",
            "missing_dependency",
        )


def _load_extractor(module_name: str):
    """Import an extractor module lazily, mapping a missing dep to a clear error."""
    try:
        return importlib.import_module(f"app.workers.extractors.{module_name}")
    except ImportError as exc:  # heavy extra (pypdf/docx/bs4/pandas) not installed
        raise MissingExtractionDependency(str(exc)) from exc


@contextmanager
def _time_limit(seconds: int) -> Iterator[None]:
    """Best-effort per-file timeout via SIGALRM (main thread, Unix only)."""
    if seconds <= 0 or threading.current_thread() is not threading.main_thread():
        yield
        return

    def _raise(signum: int, frame: Any) -> None:
        raise ExtractionError(f"extraction exceeded {seconds}s", "timeout")

    previous = signal.signal(signal.SIGALRM, _raise)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _check_zip_bomb(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise ExtractionError(f"invalid archive: {exc}", "invalid_archive") from exc
    if len(infos) > _MAX_ZIP_ENTRIES:
        raise ExtractionError("too many zip entries", "decompression_bomb")
    uncompressed = sum(i.file_size for i in infos)
    compressed = sum(i.compress_size for i in infos)
    if uncompressed > _MAX_ZIP_UNCOMPRESSED:
        raise ExtractionError("uncompressed size exceeds cap", "decompression_bomb")
    if compressed > 0 and uncompressed / compressed > _MAX_ZIP_RATIO:
        raise ExtractionError("suspicious compression ratio", "decompression_bomb")


def _dispatch(path: Path, source_id: str):
    suffix = path.suffix.lower()
    if suffix in _ZIP_FORMATS:
        _check_zip_bomb(path)
    if suffix in _TEXT_FORMATS:
        return _load_extractor(_TEXT_FORMATS[suffix]).extract(path)
    if suffix in _TABLE_EXTENSIONS:
        return _load_extractor("tables").extract(path, source_id)
    raise ExtractionError(f"unsupported extension: {suffix}", "unsupported")


def is_supported(extension: str) -> bool:
    return extension.lower() in _SUPPORTED


# --- output writing ---------------------------------------------------------


def _write_outputs(
    dirs: dict[str, Path], source_id: str, markdown_text: str, chunks: list[Any],
    table_files: list[tuple[str, str]],
) -> None:
    (dirs["markdown"] / f"{source_id}.md").write_text(markdown_text, encoding="utf-8")

    chunk_lines = [json.dumps(c.to_dict(), ensure_ascii=False) for c in chunks]
    body = "\n".join(chunk_lines) + ("\n" if chunk_lines else "")
    (dirs["chunks"] / f"{source_id}.jsonl").write_text(body, encoding="utf-8")

    # Per-source tables dir is always present (even empty); rewrite it cleanly.
    table_dir = dirs["tables"] / source_id
    if table_dir.exists():
        shutil.rmtree(table_dir)
    table_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in table_files:
        (table_dir / filename).write_text(content, encoding="utf-8")


def _write_log(dirs: dict[str, Path], source_id: str, log: dict[str, Any]) -> None:
    path = dirs["extraction_logs"] / f"{source_id}.json"
    path.write_text(json.dumps(log, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


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
) -> tuple[dict[str, Any], bool]:
    """Extract one source.

    Returns ``(log, persisted)``. ``persisted`` is False when a failed re-extraction
    left a prior successful result untouched (last-good preserved), so the caller must
    not rewrite the manifest. ``log['status']`` drives the run summary either way.
    """
    source_id = manifest["source_id"]
    started = iso_now()
    raw_file = _safe_raw_file(root, manifest)
    # A prior successful extraction we must not destroy if this run fails.
    prior_success = (
        manifest.get("ingestion_status") in {"extracted", "partial"}
        and (dirs["markdown"] / f"{source_id}.md").exists()
    )

    log: dict[str, Any] = {
        "source_id": source_id,
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

        with _time_limit(timeout_s):
            extraction = _dispatch(raw_file, source_id)

        markdown_text, chunks = assemble(
            source_id, extraction.elements, target_chars=target_chars, max_chars=max_chars
        )
        _write_outputs(dirs, source_id, markdown_text, chunks, extraction.tables)

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
        _write_log(dirs, source_id, log)
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
        return log, True

    # Failure. If a prior successful extraction exists, keep its artifacts, manifest,
    # and log untouched — the failure is surfaced in the run/job summary, not by
    # destroying usable evidence (last-good wins). Only flip to error when there is no
    # good prior state to protect.
    if prior_success:
        log["preserved_prior"] = True
        return log, False

    _write_log(dirs, source_id, log)
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
    return log, True


# --- run --------------------------------------------------------------------


def _iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")


def _already_extracted(manifest: dict[str, Any], root: Path, markdown_dir: Path) -> bool:
    """True only when a prior extraction is on disk AND the raw file has not drifted.

    The skip stays cheap (no re-hash) but is gated on the raw file's size/mtime still
    matching the manifest. A drifted (or missing) raw file is NOT skipped, so
    _extract_one re-hashes it and surfaces a checksum_mismatch instead of silently
    generating from stale normalized evidence (ADR-0024).
    """
    if manifest.get("ingestion_status") not in {"extracted", "partial"}:
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
        path.mkdir(parents=True, exist_ok=True)

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

    try:
        manifests, skipped_invalid = valid_manifests(manifests_dir)
        if source_ids is not None:
            wanted = set(source_ids)
            manifests = [m for m in manifests if m.get("source_id") in wanted]

        counts = {
            "extracted": 0, "partial": 0, "errors": 0,
            "skipped_unchanged": 0, "skipped_unsupported": 0,
        }
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        considered = 0

        for manifest in manifests:
            source_id = manifest["source_id"]
            if not is_supported(manifest.get("file_extension", "")):
                counts["skipped_unsupported"] += 1
                continue
            considered += 1
            if not force and _already_extracted(manifest, root, dirs["markdown"]):
                counts["skipped_unchanged"] += 1
                continue

            log, persisted = _extract_one(
                manifest, root, dirs,
                max_file_mb=max_file_mb, timeout_s=timeout_s,
                target_chars=target_chars, max_chars=max_chars,
            )
            if persisted:
                save_manifest(manifests_dir, manifest)

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
            "manifests_skipped_invalid": len(skipped_invalid),
            "error_details": errors,
            "warnings": warnings,
            "forced": force,
            "extracted_at": now,
        }

        if conn is not None:
            db.update_job(
                conn, job_id,
                status="succeeded" if not errors else "partial",
                finished_at=iso_now(), metadata=summary, warnings=warnings,
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
