#!/usr/bin/env python3
"""Phase 1 intake worker: scan raw/inbox and write content-keyed manifests.

Implements the content-keyed manifest model (ADR-0007): exactly one manifest per
unique content at raw/manifests/<source_id>.json, with every observed path recorded
as an entry in occurrences[]. Exact (SHA256) duplicates are merged into the existing
manifest and counted in the run summary. No raw file is ever modified, moved, or
deleted; the worker writes only under raw/manifests/ and db/jobs.sqlite.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.backend import db

_CHUNK = 1 << 20  # 1 MiB streaming read for checksums
_SKIP_SUFFIXES = ("~", ".tmp", ".swp", ".part", ".crdownload")


# --- primitives -------------------------------------------------------------


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso_mtime(stat_result: Any) -> str:
    return datetime.fromtimestamp(stat_result.st_mtime, timezone.utc).isoformat(
        timespec="seconds"
    )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def source_id_for(sha256: str) -> str:
    """Deterministic content-derived source id: src_<first 16 hex chars>."""
    return f"src_{sha256[:16]}"


def _is_ignorable(path: Path) -> bool:
    name = path.name
    if name.startswith("."):
        return True
    if "Zone.Identifier" in name:
        return True
    return name.endswith(_SKIP_SUFFIXES)


def iter_inbox_files(inbox: Path) -> list[Path]:
    """Recursively list real files in the inbox, skipping hidden/temp artifacts."""
    if not inbox.exists():
        return []
    files = [p for p in inbox.rglob("*") if p.is_file() and not _is_ignorable(p)]
    return sorted(files, key=lambda p: str(p).lower())


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


# --- manifests --------------------------------------------------------------


def _file_meta(path: Path, root: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "relative_path": _rel(path, root),
        "filename": path.name,
        "size_bytes": stat.st_size,
        "modified_at": _iso_mtime(stat),
    }


def _new_manifest(
    source_id: str, sha256: str, canonical_meta: dict[str, Any],
    canonical_path: Path, now: str,
) -> dict[str, Any]:
    mime, _ = mimetypes.guess_type(str(canonical_path))
    return {
        "source_id": source_id,
        "original_filename": canonical_meta["filename"],
        "raw_path": str(canonical_path),
        "relative_raw_path": canonical_meta["relative_path"],
        "sha256": sha256,
        "size_bytes": canonical_meta["size_bytes"],
        "file_extension": canonical_path.suffix,
        "detected_mime_type": mime,
        # created_at/discovered_at are set once and preserved across rescans.
        "created_at": canonical_meta["modified_at"],
        "modified_at": canonical_meta["modified_at"],
        "discovered_at": now,
        "last_seen_at": now,
        "last_scanned_at": now,
        "ingestion_status": "new",
        "retention_class": "unknown",
        "occurrences": [],
        "notes": [],
    }


def _merge_occurrences(
    manifest: dict[str, Any], observed: list[dict[str, Any]], now: str
) -> None:
    by_rel = {o["relative_path"]: o for o in manifest.get("occurrences", [])}
    for meta in observed:
        rel = meta["relative_path"]
        if rel in by_rel:
            existing = by_rel[rel]
            existing["size_bytes"] = meta["size_bytes"]
            existing["modified_at"] = meta["modified_at"]
            existing["last_seen_at"] = now
        else:
            by_rel[rel] = {
                "relative_path": rel,
                "filename": meta["filename"],
                "size_bytes": meta["size_bytes"],
                "modified_at": meta["modified_at"],
                "first_seen_at": now,
                "last_seen_at": now,
            }
    manifest["occurrences"] = [by_rel[k] for k in sorted(by_rel)]


def list_manifests(manifests_dir: Path) -> list[dict[str, Any]]:
    manifests_dir = Path(manifests_dir)
    if not manifests_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(manifests_dir.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def load_manifest(manifests_dir: Path, source_id: str) -> dict[str, Any] | None:
    path = Path(manifests_dir) / f"{source_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# --- scan -------------------------------------------------------------------


def scan_inbox(
    root: Path,
    *,
    inbox: Path | None = None,
    manifests_dir: Path | None = None,
    jobs_db: Path | None = None,
    record_job: bool = True,
) -> dict[str, Any]:
    """Scan the inbox, write/merge manifests, optionally record a job, return a summary."""
    root = Path(root).resolve()
    inbox = Path(inbox) if inbox else root / "raw" / "inbox"
    manifests_dir = Path(manifests_dir) if manifests_dir else root / "raw" / "manifests"
    jobs_db = Path(jobs_db) if jobs_db else root / "db" / "jobs.sqlite"
    manifests_dir.mkdir(parents=True, exist_ok=True)

    now = iso_now()
    job_id = f"job_{uuid.uuid4().hex[:16]}"
    conn = None
    if record_job:
        db.init_db(jobs_db)
        conn = db.connect(jobs_db)
        db.insert_job(
            conn, job_id=job_id, job_type="intake_scan", status="running",
            created_at=now, started_at=now, input_path=_rel(inbox, root),
        )

    try:
        files = iter_inbox_files(inbox)
        errors: list[dict[str, str]] = []
        by_content: dict[str, list[dict[str, Any]]] = {}
        for path in files:
            try:
                sha = sha256_file(path)
                meta = _file_meta(path, root)
            except OSError as exc:
                errors.append({"path": _rel(path, root), "error": str(exc)})
                continue
            by_content.setdefault(sha, []).append(meta)

        new_manifests = 0
        updated_manifests = 0
        warnings: list[dict[str, str]] = []
        source_ids: list[str] = []

        for sha, observed in by_content.items():
            observed.sort(key=lambda m: m["relative_path"].lower())
            canonical_meta = observed[0]
            canonical_path = (root / canonical_meta["relative_path"]).resolve()
            source_id = source_id_for(sha)
            manifest_path = manifests_dir / f"{source_id}.json"

            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                created = False
            else:
                manifest = _new_manifest(
                    source_id, sha, canonical_meta, canonical_path, now
                )
                created = True

            _merge_occurrences(manifest, observed, now)
            manifest["last_seen_at"] = now
            manifest["last_scanned_at"] = now

            if canonical_meta["size_bytes"] == 0 and "empty_file" not in manifest.get(
                "notes", []
            ):
                manifest.setdefault("notes", []).append("empty_file")
                warnings.append({"source_id": source_id, "warning": "empty_file"})

            manifest_path.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            source_ids.append(source_id)
            new_manifests += int(created)
            updated_manifests += int(not created)

        files_found = len(files)
        unique_contents = len(by_content)
        summary: dict[str, Any] = {
            "job_id": job_id,
            "inbox": _rel(inbox, root),
            "files_found": files_found,
            "unique_contents": unique_contents,
            "new_manifests": new_manifests,
            "updated_manifests": updated_manifests,
            "duplicates": files_found - unique_contents,
            "errors": len(errors),
            "error_details": errors,
            "warnings": warnings,
            "source_ids": sorted(set(source_ids)),
            "scanned_at": now,
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
                conn, job_id, status="failed", finished_at=iso_now(),
                error_message=str(exc),
            )
        raise
    finally:
        if conn is not None:
            conn.close()
