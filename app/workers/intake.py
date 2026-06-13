#!/usr/bin/env python3
"""Phase 1 intake worker: scan raw/inbox and write content-keyed manifests.

Implements the content-keyed manifest model (ADR-0007): exactly one manifest per
unique content at raw/manifests/<source_id>.json, with every observed path recorded
as an entry in occurrences[]. Exact (SHA256) duplicates are merged into the existing
manifest and counted in the run summary. No raw file is ever modified, moved, or
deleted; the worker writes only under raw/manifests/ and db/jobs.sqlite.
"""
from __future__ import annotations

import mimetypes
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.backend import db
from app.backend.manifests import (
    iso_now,
    load_manifest,
    save_manifest,
    sha256_file,
    source_id_for,
)

_SKIP_SUFFIXES = ("~", ".tmp", ".swp", ".part", ".crdownload")


# --- primitives -------------------------------------------------------------


def _iso_mtime(stat_result: Any) -> str:
    return datetime.fromtimestamp(stat_result.st_mtime, timezone.utc).isoformat(
        timespec="seconds"
    )


def _is_ignorable(path: Path) -> bool:
    name = path.name
    if name.startswith("."):
        return True
    if "Zone.Identifier" in name:
        return True
    return name.endswith(_SKIP_SUFFIXES)


def _within(path: Path, root: Path) -> bool:
    """True if path's real location stays inside root (defeats symlink escape)."""
    try:
        path.resolve().relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def _saved_page_asset_dirs(inbox: Path) -> set[Path]:
    """Resolved paths of browser "save page complete" companion dirs (``<name>_files``).

    A directory named ``<stem>_files`` sitting next to a saved ``<stem>.htm(l)`` page
    holds that page's downloaded assets (scripts, styles, images, helper fragments),
    not standalone user sources. Treating each asset as its own source floods the
    manifest store with noise, so files under such a directory are skipped at intake.
    """
    dirs: set[Path] = set()
    for path in inbox.rglob("*"):
        if not path.is_dir() or not path.name.endswith("_files"):
            continue
        stem = path.name[: -len("_files")].lower()
        try:
            siblings = {p.name.lower() for p in path.parent.iterdir() if p.is_file()}
        except OSError:
            continue
        if f"{stem}.htm" in siblings or f"{stem}.html" in siblings:
            dirs.add(path.resolve())
    return dirs


def iter_inbox_files(inbox: Path) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Return (safe_files, skipped) where skipped is a list of (path, reason).

    Skips hidden/temp artifacts. Rejects symlinks and any path whose real location
    escapes the inbox, so a link under raw/inbox can never cause hashing or
    manifesting of files outside the raw repository (untrusted-data contract). Also
    skips the asset files of saved web pages (``<name>_files/``), which are page
    resources rather than sources.
    """
    if not inbox.exists():
        return [], []
    inbox_real = inbox.resolve()
    asset_dirs = _saved_page_asset_dirs(inbox)
    files: list[Path] = []
    skipped: list[tuple[Path, str]] = []
    for path in sorted(inbox.rglob("*"), key=lambda p: str(p).lower()):
        if not path.is_file() or _is_ignorable(path):
            continue
        if path.is_symlink() or not _within(path, inbox_real):
            skipped.append((path, "skipped_symlink"))
            continue
        if asset_dirs and asset_dirs.intersection(path.resolve().parents):
            skipped.append((path, "saved_page_asset"))
            continue
        files.append(path)
    return files, skipped


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
        files, skipped = iter_inbox_files(inbox)
        errors: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        for skipped_path, reason in skipped:
            warnings.append({"path": _rel(skipped_path, root), "warning": reason})
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
        source_ids: list[str] = []

        for sha, observed in by_content.items():
            observed.sort(key=lambda m: m["relative_path"].lower())
            canonical_meta = observed[0]
            canonical_path = (root / canonical_meta["relative_path"]).resolve()
            source_id = source_id_for(sha)
            existing = load_manifest(manifests_dir, source_id)

            if existing is not None:
                manifest = existing
                # Manifests are authoritative (ADR-0008): never merge into a record
                # whose stored checksum disagrees with the scanned content.
                if manifest.get("sha256") != sha:
                    errors.append({
                        "path": canonical_meta["relative_path"],
                        "error": f"manifest sha256 mismatch for {source_id}; skipped",
                    })
                    continue
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

            save_manifest(manifests_dir, manifest)
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
            "skipped": len(skipped),
            "skipped_assets": sum(1 for _, r in skipped if r == "saved_page_asset"),
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
