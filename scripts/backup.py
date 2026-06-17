#!/usr/bin/env python3
"""Create a lightweight zip backup of critical project state.

The backup is the durability mechanism for the gitignored runtime state (manifests,
databases, wiki) as well as the versioned config. Per the Build Spec backup agent it covers
raw manifests, the databases (incl. the authoritative graph in ``db/graph.sqlite``), the wiki,
and policies; raw source files are excluded by default because they may be large (set
include_raw later once a storage policy is decided).

Index backup posture (ADR-0032 §7): the derived retrieval indexes under ``indexes/`` are NOT
backed up by default. The **keyword** index is a cheap full rebuild from chunks + wiki, so it is
never backed up. The **vector** index is recompute-savings only (re-embedding is expensive but the
truth is still raw -> chunks), so it is opt-in via ``BACKUP_INCLUDE_VECTOR_INDEX``. The
authoritative **graph** is backed up because it lives in ``db/`` (reviewed relationship state),
not under ``indexes/``.
"""
from __future__ import annotations

import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# Gitignored runtime state whose only durability is this backup (ADR-0014): raw manifests, the
# SQLite databases (jobs, llm_cache, and the authoritative graph in db/graph.sqlite), and the
# wiki layer. The derived indexes/ tree is excluded by default (ADR-0032 §7) — see below.
INCLUDE_DIRS = [
    "wiki", "policies", "evals", "scripts", "templates", "raw/manifests",
    "reviews", ".claude", "db",
]
INCLUDE_FILES = ["CLAUDE.md", "AGENTS.md", "README.md", "pyproject.toml", "docker-compose.yml"]

# The response cache is backed up by default (ADR-0027); set BACKUP_EXCLUDE_LLM_CACHE to a
# truthy value to opt out (trading reproducibility for a smaller backup footprint).
_CACHE_FILENAME = "llm_cache.sqlite"

# The vector index is derived/regenerable; include it only on explicit opt-in (ADR-0032 §7) to
# save re-embedding cost. The keyword index is never backed up (cheap full rebuild from chunks).
_VECTOR_INDEX_DIR = "indexes/vector"
_INCLUDE_VECTOR_ENV = "BACKUP_INCLUDE_VECTOR_INDEX"


def create_backup(root: Path) -> Path:
    """Write a timestamped zip of critical state under <root>/backups and return it."""
    root = Path(root).resolve()
    backup_dir = root / "backups"
    backup_dir.mkdir(exist_ok=True)
    exclude_cache = bool(os.environ.get("BACKUP_EXCLUDE_LLM_CACHE"))
    include_dirs = list(INCLUDE_DIRS)
    if os.environ.get(_INCLUDE_VECTOR_ENV):
        include_dirs.append(_VECTOR_INDEX_DIR)  # opt-in: save re-embedding cost
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = backup_dir / f"knowledge-system-backup-{stamp}.zip"
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
                if exclude_cache and path.name == _CACHE_FILENAME:
                    continue  # opt-out: skip the LLM response cache
                zf.write(path, path.relative_to(root))
    return out


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    out = create_backup(root)
    print(f"created backup {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
