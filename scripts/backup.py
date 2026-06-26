#!/usr/bin/env python3
"""Create and restore a zip backup of critical project state (ADR-0039).

The backup is the durability mechanism for the gitignored runtime state (manifests,
databases, wiki) as well as the versioned config. Per the Build Spec backup agent it covers
raw manifests, the databases (incl. the authoritative graph in ``db/graph.sqlite``), the wiki,
policies, and durable agent config; **raw source bytes are excluded by default** (size + the
raw-privacy posture) and opt in via ``BACKUP_INCLUDE_RAW`` (ADR-0039 decision 1).

Raw inclusion is **manifest-driven** (ADR-0039 decision 1, revised): intake never moves files out
of ``raw/inbox/`` (ADR-0007), so catalogued sources live wherever they were first seen. On
``BACKUP_INCLUDE_RAW`` we back up exactly the raw paths the manifests catalogue
(``relative_raw_path`` + every ``occurrences[].relative_path``), wherever they sit under ``raw/`` —
including ``raw/inbox/``. Un-manifested files (staging not yet ingested) are excluded. A catalogued
raw file that is missing on disk, or whose bytes disagree with the manifest sha256 (ADR-0024), is a
**hard error** — the backup aborts rather than ship an archive that silently omits or misrepresents a
source.

Index backup posture (ADR-0032 §7): the derived retrieval indexes under ``indexes/`` are NOT
backed up by default. The **keyword** index is a cheap full rebuild from chunks + wiki, so it is
never backed up. The **vector** index is recompute-savings only, so it is opt-in via
``BACKUP_INCLUDE_VECTOR_INDEX``. The authoritative **graph** is backed up because it lives in
``db/`` (reviewed relationship state), not under ``indexes/``.

Integrity (ADR-0039 decision 4): ZIP CRC32 catches archive corruption but not raw bytes that
drifted from their manifest *before* the backup ran, so raw is also sha256-verified against the
manifest at backup time and again at restore time. Any mismatch is a hard error, never a warning.

Restore (ADR-0039 decision 3) is guarded in-place: it refuses to overwrite existing files unless
``--force`` is given, never deletes files absent from the archive, and calls out conflicts on
durable, non-regenerable state (``db/graph.sqlite``, ``reviews/``, ``raw/manifests/``, ``policies/``).
A restore that skips any pre-existing file is reported as **PARTIAL**.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.backend.paths import safe_under  # noqa: E402

# Gitignored runtime state whose only durability is this backup (ADR-0014): raw manifests, the
# SQLite databases (jobs, llm_cache, and the authoritative graph in db/graph.sqlite), the wiki
# layer, and durable agent/skill config (.claude/, .agents/). The derived indexes/ tree is
# excluded by default (ADR-0032 §7) — see below. `.codex/` is developer-local, not backed up. Raw
# *bytes* are NOT listed here; they are added manifest-driven on opt-in (see create_backup_report).
INCLUDE_DIRS = [
    "wiki", "policies", "evals", "scripts", "templates", "raw/manifests",
    "reviews", ".claude", ".agents", "db",
]
INCLUDE_FILES = ["CLAUDE.md", "AGENTS.md", "README.md", "pyproject.toml", "docker-compose.yml"]

# The response cache is backed up by default (ADR-0027); set BACKUP_EXCLUDE_LLM_CACHE to a
# truthy value to opt out (trading reproducibility for a smaller backup footprint).
_CACHE_FILENAME = "llm_cache.sqlite"

# The vector index is derived/regenerable; include it only on explicit opt-in (ADR-0032 §7) to
# save re-embedding cost. The keyword index is never backed up (cheap full rebuild from chunks).
_VECTOR_INDEX_DIR = "indexes/vector"
_INCLUDE_VECTOR_ENV = "BACKUP_INCLUDE_VECTOR_INDEX"

# Raw bytes: excluded by default, opt in via BACKUP_INCLUDE_RAW (ADR-0039 decision 1). Inclusion is
# manifest-driven (see _catalogued_raw_index) — never a subdir allowlist, because intake leaves real
# sources in raw/inbox/ permanently (ADR-0007).
_INCLUDE_RAW_ENV = "BACKUP_INCLUDE_RAW"

# Embedded per-archive integrity sidecar (ADR-0039 decision 4). Not a project file: skipped on
# restore extraction.
_BACKUP_MANIFEST_NAME = "BACKUP_MANIFEST.json"

# Durable, non-regenerable state whose overwrite must be called out on restore (ADR-0039 decision 3):
# reviewed graph relationships, human review decisions, the source catalog, and the policies that
# change interpretation. Matched as path prefixes against archive-relative names.
DURABLE_CONFLICT_PREFIXES = ("db/graph.sqlite", "reviews/", "raw/manifests/", "policies/")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _catalogued_raw_index(root: Path) -> dict[str, str]:
    """Manifest-complete map of catalogued raw path -> content sha256 (ADR-0024).

    Covers ``relative_raw_path`` AND every ``occurrences[].relative_path`` (ADR-0007: one manifest
    per unique content, every observed path recorded), so a duplicate copy can't be missed. All
    occurrences of one manifest are the same content, so they share the manifest sha256. Paths that
    escape ``raw/`` (malformed/tampered manifest) are dropped here and surfaced as a hard error by the
    caller when raw backup is requested.
    """
    index: dict[str, str] = {}
    manifests = root / "raw" / "manifests"
    if not manifests.exists():
        return index
    for mpath in sorted(manifests.rglob("*.json")):
        try:
            data = json.loads(mpath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        sha = data.get("sha256")
        if not isinstance(sha, str):
            continue
        rels = [data.get("relative_raw_path")]
        rels.extend(o.get("relative_path") for o in data.get("occurrences", []) if isinstance(o, dict))
        for rel in rels:
            if isinstance(rel, str) and rel:
                index[rel] = sha
    return index


@dataclass
class BackupReport:
    archive: Path
    size_bytes: int
    included_domains: list[str]
    raw_included: bool
    vector_index_included: bool
    llm_cache_included: bool
    raw_file_count: int = 0

    def render(self) -> str:
        lines = [
            f"created backup {self.archive}",
            f"  size:    {self.size_bytes} bytes",
            f"  domains: {', '.join(self.included_domains)}",
            f"  raw bytes:    {'included' if self.raw_included else 'EXCLUDED'}"
            + (f" ({self.raw_file_count} catalogued files, manifest-verified)"
               if self.raw_included else ""),
            f"  vector index: {'included' if self.vector_index_included else 'excluded'}",
            f"  llm_cache:    {'included' if self.llm_cache_included else 'excluded'}",
        ]
        if not self.raw_included:
            lines.append("  WARNING: raw bytes not in this archive — restore recovers "
                         "metadata/wiki/db state but NOT the source bytes themselves.")
        return "\n".join(lines)


def _resolve_archive_path(backup_dir: Path, stamp: str) -> Path:
    """A unique, append-only archive path. Same-second runs get a -N suffix so a snapshot never
    overwrites a prior one (ADR-0039 decision 5)."""
    out = backup_dir / f"knowledge-system-backup-{stamp}.zip"
    n = 1
    while out.exists():
        out = backup_dir / f"knowledge-system-backup-{stamp}-{n}.zip"
        n += 1
    return out


def create_backup(root: Path) -> Path:
    """Write a timestamped zip of critical state under <root>/backups and return its path.

    On ``BACKUP_INCLUDE_RAW`` the manifest-catalogued raw bytes are added and verified against their
    manifest sha256 before the archive is written (ADR-0039). Raises ``ValueError`` if a catalogued
    raw file is missing, escapes ``raw/``, or fails its checksum.
    """
    return create_backup_report(root).archive


def create_backup_report(root: Path) -> BackupReport:
    root = Path(root).resolve()
    backup_dir = root / "backups"
    backup_dir.mkdir(exist_ok=True)
    exclude_cache = bool(os.environ.get("BACKUP_EXCLUDE_LLM_CACHE"))
    include_vector = bool(os.environ.get(_INCLUDE_VECTOR_ENV))
    include_raw = bool(os.environ.get(_INCLUDE_RAW_ENV))

    include_dirs = list(INCLUDE_DIRS)
    if include_vector:
        include_dirs.append(_VECTOR_INDEX_DIR)  # opt-in: save re-embedding cost

    # Validate + collect raw BEFORE opening the archive, so a failure leaves no partial zip behind.
    raw_to_add: list[tuple[Path, str, int, str]] = []  # (src, arcname, size, sha256)
    if include_raw:
        for rel, sha in sorted(_catalogued_raw_index(root).items()):
            dest = safe_under(root, root / "raw", rel)
            if dest is None:
                raise ValueError(f"catalogued raw path escapes raw/: {rel!r} (malformed manifest); "
                                 "aborting backup")
            if not dest.is_file():
                raise ValueError(f"catalogued raw file missing on disk: {rel} (ADR-0007); "
                                 "aborting raw backup")
            digest = _sha256(dest)
            if digest != sha:
                raise ValueError(f"raw checksum mismatch for {rel}: file sha256 {digest} != manifest "
                                 f"sha256 {sha} (ADR-0024); aborting backup")
            raw_to_add.append((dest, rel, dest.stat().st_size, digest))

    raw_entries: dict[str, dict[str, Any]] = {}
    llm_cache_included = False
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = _resolve_archive_path(backup_dir, stamp)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in INCLUDE_FILES:
            path = root / rel
            if path.exists():
                zf.write(path, path.relative_to(root))
        for rel in include_dirs:
            folder = root / rel
            if not folder.exists():
                continue
            for path in folder.rglob("*"):
                # Don't recurse the backups dir into itself.
                if not path.is_file() or backup_dir in path.parents:
                    continue
                if path.name == _CACHE_FILENAME:
                    if exclude_cache:
                        continue  # opt-out: skip the LLM response cache
                    llm_cache_included = True
                zf.write(path, str(path.relative_to(root)))
        for src, arcname, size, digest in raw_to_add:
            zf.write(src, arcname)
            raw_entries[arcname] = {"size": size, "sha256": digest, "manifest_verified": True}

        included_domains = [d for d in include_dirs if (root / d).exists()]
        if include_raw:
            included_domains.append("raw (catalogued bytes)")
        sidecar = {
            "format": "knowledge-system-backup/1",
            "created_at": stamp,
            "raw_included": include_raw,
            "vector_index_included": include_vector and (root / _VECTOR_INDEX_DIR).exists(),
            "llm_cache_included": llm_cache_included,
            "included_domains": included_domains,
            "raw_files": raw_entries,
        }
        zf.writestr(_BACKUP_MANIFEST_NAME, json.dumps(sidecar, indent=2, sort_keys=True))

    return BackupReport(
        archive=out,
        size_bytes=out.stat().st_size,
        included_domains=included_domains,
        raw_included=include_raw,
        vector_index_included=sidecar["vector_index_included"],
        llm_cache_included=llm_cache_included,
        raw_file_count=len(raw_entries),
    )


@dataclass
class RestoreReport:
    archive: Path
    dry_run: bool
    written: list[str] = field(default_factory=list)
    overwritten: list[str] = field(default_factory=list)
    skipped_conflicts: list[str] = field(default_factory=list)
    durable_conflicts: list[str] = field(default_factory=list)
    raw_verified: int = 0       # raw files written/overwritten this run and checksum-verified
    raw_skipped: int = 0        # catalogued raw files left in place (pre-existing, not --force)
    raw_included: bool = False

    @property
    def partial(self) -> bool:
        return bool(self.skipped_conflicts)

    def render(self) -> str:
        if self.dry_run:
            head = "would restore (dry-run)"
        elif self.partial:
            head = "PARTIAL restore (some existing files were left in place)"
        else:
            head = "restored"
        lines = [
            f"{head} from {self.archive}",
            f"  written:     {len(self.written)}",
            f"  overwritten: {len(self.overwritten)}",
            f"  skipped (exists, no --force): {len(self.skipped_conflicts)}",
        ]
        if self.durable_conflicts:
            lines.append("  DURABLE-STATE CONFLICTS (reviewed graph / human decisions / source "
                         "catalog / policies):")
            lines.extend(f"    ! {n}" for n in self.durable_conflicts)
        if self.raw_included:
            lines.append(f"  raw bytes verified: {self.raw_verified}"
                         + (f"  (skipped pre-existing: {self.raw_skipped})"
                            if self.raw_skipped else ""))
        else:
            lines.append("  raw bytes not present; manifest/raw checksum verification skipped.")
        return "\n".join(lines)


def restore_backup(archive: Path, root: Path, *, force: bool = False,
                   dry_run: bool = False) -> RestoreReport:
    """Guarded in-place restore (ADR-0039 decision 3).

    Refuses to overwrite existing files unless ``force``; ``dry_run`` plans without writing; never
    deletes files absent from the archive. A run that skips any pre-existing file is reported as
    PARTIAL. Verifies ZIP CRC and, when raw was included, re-verifies the raw bytes it actually wrote
    against the restored manifest (or the backup's own sidecar for paths absent from the restored
    catalogue). Raises ``ValueError`` on corruption, zip-slip, or any checksum mismatch.
    """
    archive = Path(archive).resolve()
    root = Path(root).resolve()
    report = RestoreReport(archive=archive, dry_run=dry_run)

    with zipfile.ZipFile(archive) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise ValueError(f"archive CRC check failed at {bad}: backup is corrupt")
        try:
            sidecar = json.loads(zf.read(_BACKUP_MANIFEST_NAME))
        except KeyError:
            sidecar = {"raw_files": {}, "raw_included": False}
        report.raw_included = bool(sidecar.get("raw_included"))

        members = [m for m in zf.namelist() if m != _BACKUP_MANIFEST_NAME]
        # Pre-resolve every target with safe_under to block zip-slip BEFORE touching disk.
        targets: dict[str, Path] = {}
        for name in members:
            if name.endswith("/"):
                continue
            dest = safe_under(root, root, name)
            if dest is None:
                raise ValueError(f"archive entry escapes restore root: {name!r} (zip-slip; aborting)")
            targets[name] = dest

        applied: set[str] = set()  # names written or overwritten this run
        for name, dest in targets.items():
            exists = dest.exists()
            if exists and name.startswith(DURABLE_CONFLICT_PREFIXES):
                report.durable_conflicts.append(name)
            if exists and not force:
                report.skipped_conflicts.append(name)
                continue
            if dry_run:
                (report.overwritten if exists else report.written).append(name)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, dest.open("wb") as out_fh:
                out_fh.write(src.read())
            (report.overwritten if exists else report.written).append(name)
            applied.add(name)

    if dry_run or not report.raw_included:
        return report

    # Post-restore raw verification (ADR-0039 decision 4): verify only the raw we actually wrote, vs
    # the restored manifest catalogue (manifest-complete) or the backup's sidecar as fallback. Raw
    # files left in place because of a conflict are reported as skipped, never "verified".
    restored_index = _catalogued_raw_index(root)
    for name, entry in sidecar.get("raw_files", {}).items():
        if name not in applied:
            report.raw_skipped += 1
            continue
        dest = targets.get(name)
        if dest is None or not dest.exists():
            continue
        digest = _sha256(dest)
        expected = restored_index.get(name, entry.get("sha256"))
        if expected is not None and digest != expected:
            raise ValueError(f"restored raw checksum mismatch for {name}: {digest} != {expected}; "
                             "restore integrity check failed")
        report.raw_verified += 1
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or restore a project backup (ADR-0039).")
    parser.add_argument("root", nargs="?", default=".", help="project root (default: cwd)")
    parser.add_argument("--restore", metavar="ARCHIVE",
                        help="restore from ARCHIVE instead of creating a backup")
    parser.add_argument("--force", action="store_true",
                        help="restore: overwrite existing files (default refuses)")
    parser.add_argument("--dry-run", action="store_true",
                        help="restore: list writes/skips/conflicts without touching disk")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    root = Path(args.root).resolve()

    if args.restore:
        report = restore_backup(Path(args.restore), root, force=args.force, dry_run=args.dry_run)
        print(report.render())
        return 0

    report = create_backup_report(root)
    print(report.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
