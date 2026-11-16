#!/usr/bin/env python3
"""The untrusted parse of one raw file (ADR-0010; ADR-0074 decision 5): format dispatch, the ZIP
checks and the per-format extractors. It runs only inside the extraction child
(`app/workers/extract_child.py`), so it must stay light: standard library, the extractor modules
(imported lazily, per format) and `app.workers.chunking` — never the API, databases, locks, jobs or
embedding models. The parent (`app/workers/extract.py`) keeps confinement, the size and checksum
checks, assembly, staging and installation.
"""
from __future__ import annotations

import importlib
import zipfile
from pathlib import Path

# Extension -> extractor module name under app.workers.extractors (loaded lazily).
TEXT_FORMATS = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".html": "html",
    ".htm": "html",
    ".md": "markdown",
    ".markdown": "markdown",
}
TABLE_EXTENSIONS = {".csv", ".xlsx"}
SUPPORTED = set(TEXT_FORMATS) | TABLE_EXTENSIONS
ZIP_FORMATS = {".docx", ".xlsx"}

# Decompression-bomb bounds for zipped formats (DOCX/XLSX).
MAX_ZIP_ENTRIES = 10_000
MAX_ZIP_UNCOMPRESSED = 512 * 1024 * 1024  # 512 MiB
MAX_ZIP_RATIO = 200


class ExtractionError(Exception):
    """Recoverable per-source failure carrying a machine-readable skip reason (None: an
    unclassified parse failure, reported by its exception type and message)."""

    def __init__(self, message: str, skip_reason: str | None) -> None:
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


def _check_zip_bomb(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise ExtractionError(f"invalid archive: {exc}", "invalid_archive") from exc
    if len(infos) > MAX_ZIP_ENTRIES:
        raise ExtractionError("too many zip entries", "decompression_bomb")
    uncompressed = sum(i.file_size for i in infos)
    compressed = sum(i.compress_size for i in infos)
    if uncompressed > MAX_ZIP_UNCOMPRESSED:
        raise ExtractionError("uncompressed size exceeds cap", "decompression_bomb")
    if compressed > 0 and uncompressed / compressed > MAX_ZIP_RATIO:
        raise ExtractionError("suspicious compression ratio", "decompression_bomb")


def dispatch(path: Path, source_id: str):
    """Parse one file into an `Extraction` (the ZIP checks first, inside the same bounded work)."""
    suffix = path.suffix.lower()
    if suffix in ZIP_FORMATS:
        _check_zip_bomb(path)
    if suffix in TEXT_FORMATS:
        return _load_extractor(TEXT_FORMATS[suffix]).extract(path)
    if suffix in TABLE_EXTENSIONS:
        return _load_extractor("tables").extract(path, source_id)
    raise ExtractionError(f"unsupported extension: {suffix}", "unsupported")
