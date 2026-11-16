#!/usr/bin/env python3
"""Failed extraction attempts and their retry classification (ADR-0074 decision 6).

A per-document **attempt record**, `normalized/extraction_attempts/<source_id>.json`, holds the
latest failed attempt: its reason and message, its retry category, when it ran, whether a usable
prior extraction was retained, and the fingerprint of everything that bounded it. It lives beside —
never inside — the extraction log, so a failure after a success neither disappears nor overwrites
the good log. It is derived state: losing it (or finding it unreadable) only causes a retry, the
safe direction.

- **Retry classification.** Only explicit deterministic rejections are **cached** (`CACHED`):
  skipped as `skipped_known_failure` while the fingerprint is unchanged. Everything else —
  `timeout`, `extractor_crashed`, `extractor_unavailable`, `extractor_output_invalid`,
  `missing_dependency`, I/O errors, checksum and path problems, an unclassified parse failure —
  is environmental or ambiguous and **always retried**.
- **The fingerprint** covers the raw file (manifest sha256 plus its current size and mtime, the
  same cheap drift gate as the unchanged skip), the file-size, ZIP, output and protocol limits,
  the extraction configuration (chunk targets, timeout), and the implementation and dependency
  versions (`EXTRACT_CODE_VERSION`, the interpreter, the format's extractor distributions). Any
  piece that can't be determined makes it None: the attempt is recorded as retried.
- **Binding.** A record also stores the **generation** it was taken against — the `attempt_id`
  in the extraction log then on disk (unique per attempt; the retained prior's, or for a first-time
  failure the failure log this attempt wrote) — with the manifest's `extracted_at` and whether a
  prior was retained. It is current only while all three still match, so a later success (a new
  log, a new id — even within the same second) or a restored manifest retires it without any
  ordering between the writes.
- **Retained is not usable.** A failed re-extraction keeps the prior bytes, manifest and log; they
  are a *usable* prior extraction only while the install journal allows reading them (decision
  8). The record notes both, and the screen and lint read the journal's state live.
- **The run consults the current failure before the unchanged shortcut**, so a failed
  re-extraction (whose kept outputs look unchanged) retries unless it is a cached rejection with
  an unchanged fingerprint.
- **`force`** ignores the cache (an unconditional retry, the safety checks still apply).
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from app.backend import authority_read as ar
from app.backend import durable_write
from app.backend import extraction_install as xi
from app.backend.manifests import is_source_id

RECORD_VERSION = 1
FINGERPRINT_VERSION = 1
CACHED = "cached"
RETRY = "retry"
# Explicit deterministic rejections: the same inputs, limits and code give the same rejection.
CACHED_REASONS = frozenset({"oversize", "decompression_bomb", "invalid_archive",
                            "output_too_large"})
MESSAGE_MAX_CHARS = 2000
DIRNAME = "extraction_attempts"
_EXTRACTED = ("extracted", "partial")

# The extractor distributions each format's parse depends on (their versions enter the fingerprint).
_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    ".pdf": ("pypdf",), ".docx": ("python-docx",), ".html": ("beautifulsoup4",),
    ".htm": ("beautifulsoup4",), ".md": (), ".markdown": (), ".csv": ("pandas",),
    ".xlsx": ("pandas", "openpyxl"),
}
_KEYS = {"record_version", "source_id", "attempt_id", "attempted_at", "reason", "error",
         "category", "prior_retained", "prior_usable", "generation", "extracted_at",
         "fingerprint", "fingerprint_inputs"}


def category(reason: str | None, fingerprint: str | None) -> str:
    """`cached` only for an explicit deterministic rejection with a complete fingerprint."""
    return CACHED if reason in CACHED_REASONS and fingerprint is not None else RETRY


def _dist_version(name: str) -> str | None:
    try:
        return version(name)
    except (PackageNotFoundError, ValueError, OSError):
        return None


def fingerprint(manifest: dict[str, Any], raw_file: Path | None, *, max_file_mb: int,
                timeout_s: int, target_chars: int, max_chars: int,
                code_version: int) -> tuple[str | None, dict[str, Any] | None]:
    """`(digest, inputs)` of everything that bounds an attempt on this raw file, or `(None, None)`
    when any of it can't be determined (missing or unreadable information means retry)."""
    from app.workers import extract_child, extract_parse, extract_runner
    sha = manifest.get("sha256")
    if raw_file is None or not isinstance(sha, str) or not sha:
        return None, None
    suffix = raw_file.suffix.lower()
    if suffix not in _DEPENDENCIES:
        return None, None
    deps = {name: _dist_version(name) for name in _DEPENDENCIES[suffix]}
    if any(v is None for v in deps.values()):
        return None, None
    try:
        st = raw_file.stat()
    except OSError:
        return None, None
    inputs = {
        "fingerprint_version": FINGERPRINT_VERSION,
        "raw": {"sha256": sha, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                "suffix": suffix},
        "limits": {"max_file_bytes": max_file_mb * 1024 * 1024,
                   "zip_entries": extract_parse.MAX_ZIP_ENTRIES,
                   "zip_uncompressed": extract_parse.MAX_ZIP_UNCOMPRESSED,
                   "zip_ratio": extract_parse.MAX_ZIP_RATIO,
                   "max_result_bytes": extract_runner.MAX_RESULT_BYTES,
                   "envelope_bytes": extract_runner.ENVELOPE_BYTES,
                   "protocol_version": extract_child.PROTOCOL_VERSION},
        "config": {"target_chars": target_chars, "max_chars": max_chars, "timeout_s": timeout_s},
        "code": {"extract_code_version": code_version,
                 "python": f"{sys.implementation.name}-{sys.version.split()[0]}",
                 "dependencies": deps},
    }
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest(), inputs


def record_path(normalized_dir: Path, source_id: str) -> Path:
    return Path(normalized_dir) / DIRNAME / f"{source_id}.json"


def generation(normalized_dir: Path, source_id: str) -> str | None:
    """The `attempt_id` of the extraction log on disk — the generation a record binds to — or
    None (no log, an unreadable one, or one written before logs carried it)."""
    try:
        log = ar.load_json(Path(normalized_dir) / "extraction_logs" / f"{source_id}.json")
    except (OSError, ValueError, RecursionError):
        return None
    value = log.get("attempt_id") if isinstance(log, dict) else None
    return value if type(value) is str else None


def new_record(manifest: dict[str, Any], log: dict[str, Any], *, attempt_id: str,
               prior_retained: bool, prior_usable: bool, generation: str | None,
               fingerprint: str | None, inputs: dict[str, Any] | None) -> dict[str, Any]:
    """The record of one failed attempt; `manifest` is the manifest as the attempt left it and
    `generation` the extraction log's attempt id after it."""
    reason = log.get("skip_reason")
    return {
        "record_version": RECORD_VERSION,
        "source_id": manifest["source_id"],
        "attempt_id": attempt_id,
        "attempted_at": log.get("finished_at"),
        "reason": reason,
        "error": str(log.get("error") or "")[:MESSAGE_MAX_CHARS],
        "category": category(reason, fingerprint),
        "prior_retained": prior_retained,
        "prior_usable": prior_usable,
        "generation": generation,
        "extracted_at": manifest.get("extracted_at"),
        "fingerprint": fingerprint,
        "fingerprint_inputs": inputs,
    }


def write(normalized_dir: Path, record: dict[str, Any]) -> None:
    durable_write.write_json(record_path(normalized_dir, record["source_id"]), record)


def clear(normalized_dir: Path, source_id: str) -> None:
    """A success supersedes the latest failure (its binding already retires it; this tidies)."""
    durable_write.remove(record_path(normalized_dir, source_id), missing_ok=True)


@dataclass(frozen=True)
class AttemptRead:
    state: str                              # authority_read's absent / unreadable / corrupt / ok
    record: dict[str, Any] | None = None
    error: str | None = None


def _valid(record: Any, source_id: str) -> bool:
    if not isinstance(record, dict) or set(record) != _KEYS:
        return False
    fp = record["fingerprint"]
    return (record["record_version"] == RECORD_VERSION and record["source_id"] == source_id
            and type(record["attempt_id"]) is str and type(record["attempted_at"]) is str
            and (record["reason"] is None or type(record["reason"]) is str)
            and type(record["error"]) is str and record["category"] in (CACHED, RETRY)
            and type(record["prior_retained"]) is bool and type(record["prior_usable"]) is bool
            and (record["prior_retained"] or not record["prior_usable"])
            and (record["generation"] is None or type(record["generation"]) is str)
            and (record["extracted_at"] is None or type(record["extracted_at"]) is str)
            and (fp is None or (type(fp) is str and len(fp) == 64))
            and (record["fingerprint_inputs"] is None
                 or isinstance(record["fingerprint_inputs"], dict))
            and (record["category"] == RETRY
                 or (fp is not None and record["reason"] in CACHED_REASONS)))


def read(normalized_dir: Path, source_id: str) -> AttemptRead:
    if not is_source_id(source_id):
        return AttemptRead(ar.CORRUPT, error="source_id")
    got = ar.read_file(record_path(normalized_dir, source_id))
    if got.state != ar.OK:
        return AttemptRead(got.state, error=got.error)
    try:
        record = json.loads(got.data.decode("utf-8"))
    except (ValueError, RecursionError):
        return AttemptRead(ar.CORRUPT, error="json")
    if not _valid(record, source_id):
        return AttemptRead(ar.CORRUPT, error="schema")
    return AttemptRead(ar.OK, record=record)


def current(normalized_dir: Path, manifest: dict[str, Any]) -> dict[str, Any] | None:
    """The document's latest failed attempt while it still describes the current state (the same
    generation — the extraction log's attempt id — and `extracted_at`, and a prior retained exactly
    when the manifest reports one), else None — an absent, damaged or superseded record reads as
    no record (a retry)."""
    sid = manifest.get("source_id", "")
    got = read(normalized_dir, sid)
    if got.record is None:
        return None
    record = got.record
    if record["extracted_at"] != manifest.get("extracted_at"):
        return None
    if record["generation"] != generation(normalized_dir, sid):
        return None
    retained = manifest.get("ingestion_status") in _EXTRACTED
    if record["prior_retained"] != retained or (not retained
                                                and manifest.get("ingestion_status") != "error"):
        return None
    return record


def known_failure(record: dict[str, Any] | None, digest: str | None) -> bool:
    """Whether a run skips the document: a cached rejection whose fingerprint is unchanged."""
    return (record is not None and record["category"] == CACHED and digest is not None
            and record["fingerprint"] == digest)


# --- what the document screen and lint show -----------------------------------------------

PRIOR_RETAINED = "prior_retained"        # a usable prior extraction retained
RETAINED_UNUSABLE = "retained_unusable"  # prior bytes kept, but the install journal blocks them
NO_USABLE = "no_usable"


def _log_failure(normalized_dir: Path, source_id: str) -> tuple[str | None, str]:
    """A failure known only from the extraction log (written before attempt records existed, or
    after the record was lost): its reason and message, or `unknown`."""
    try:
        log = ar.load_json(Path(normalized_dir) / "extraction_logs" / f"{source_id}.json")
    except (OSError, ValueError, RecursionError):
        return None, "unknown (extraction log unreadable)"
    if not isinstance(log, dict) or log.get("status") != "error":
        return None, "unknown"
    reason = log.get("skip_reason")
    error = log.get("error")
    return (reason if type(reason) is str else None,
            str(error)[:MESSAGE_MAX_CHARS] if error else "unknown")


def failure_state(normalized_dir: Path, manifest: dict[str, Any]) -> dict[str, Any] | None:
    """The two failure situations (decision 6): a usable prior extraction retained while the latest
    attempt failed, or no usable extraction — with prior bytes kept but blocked by their install
    journal (`retained_unusable`, read live) told apart from none at all. None when the latest
    attempt did not fail. `cached` says it awaits changed inputs or force; otherwise it retries on
    the next run."""
    sid = manifest.get("source_id", "")
    status = manifest.get("ingestion_status")
    if status not in (*_EXTRACTED, "error") or not is_source_id(sid):
        return None
    record = current(normalized_dir, manifest)
    if record is not None:
        reason, error, at, cached = (record["reason"], record["error"], record["attempted_at"],
                                     record["category"] == CACHED)
    elif status == "error":
        (reason, error), at, cached = _log_failure(normalized_dir, sid), None, False
    else:
        return None
    if status not in _EXTRACTED:
        situation, journal = NO_USABLE, None
    else:
        read = xi.read_journal(xi.Layout(Path(normalized_dir)), sid)
        situation = PRIOR_RETAINED if read.usable else RETAINED_UNUSABLE
        journal = None if read.usable else read.state
    return {"situation": situation, "reason": reason, "error": error, "attempted_at": at,
            "cached": cached, "journal": journal}


def describe(state: dict[str, Any]) -> str:
    """The one-line wording the screen and lint share."""
    shown = state["reason"] or "unclassified parse failure"
    if state["situation"] == PRIOR_RETAINED:
        what = "usable prior extraction retained; latest attempt failed: " + shown
    elif state["situation"] == RETAINED_UNUSABLE:
        what = (f"no usable extraction (earlier outputs kept, but their install is "
                f"{state.get('journal')}); latest attempt failed: {shown}")
    else:
        what = "no usable extraction: " + shown
    then = ("awaits changed inputs or a forced re-extraction" if state["cached"]
            else "retries on the next run")
    return f"{what} — {then}"
