#!/usr/bin/env python3
"""Create a lightweight zip backup of critical project state.

The backup is the durability mechanism for the gitignored runtime state (manifests,
databases, wiki, indexes) as well as the versioned config. Per the Build Spec backup
agent it covers raw manifests, the databases, the wiki, policies, and the indexes; raw
source files are excluded by default because they may be large (set include_raw later
once a storage policy is decided).
"""
from __future__ import annotations

import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# Gitignored runtime state whose only durability is this backup (ADR-0014): raw
# manifests, the SQLite databases, the wiki layer, and the search/graph indexes.
INCLUDE_DIRS = [
    "wiki", "policies", "evals", "scripts", "templates", "raw/manifests",
    "reviews", ".claude", "db", "indexes",
]
INCLUDE_FILES = ["CLAUDE.md", "AGENTS.md", "README.md", "pyproject.toml", "docker-compose.yml"]


def create_backup(root: Path) -> Path:
    """Write a timestamped zip of critical state under <root>/backups and return it."""
    root = Path(root).resolve()
    backup_dir = root / "backups"
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = backup_dir / f"knowledge-system-backup-{stamp}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in INCLUDE_FILES:
            path = root / rel
            if path.exists():
                zf.write(path, path.relative_to(root))
        for rel in INCLUDE_DIRS:
            folder = root / rel
            if not folder.exists():
                continue
            for path in folder.rglob("*"):
                # Don't recurse the backups dir into itself.
                if path.is_file() and backup_dir not in path.parents:
                    zf.write(path, path.relative_to(root))
    return out


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    out = create_backup(root)
    print(f"created backup {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
