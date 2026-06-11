#!/usr/bin/env python3
"""Create a lightweight zip backup of critical project state.

Raw source files are not included by default because they may be large. Raw
manifests are included. Adjust include_raw=True in this script only after deciding
backup storage policy.
"""
from __future__ import annotations

import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

INCLUDE_DIRS = ["wiki", "policies", "evals", "scripts", "templates", "raw/manifests", "reviews", ".claude"]
INCLUDE_FILES = ["CLAUDE.md", "AGENTS.md", "README.md", "pyproject.toml", "docker-compose.yml"]


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
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
            if folder.exists():
                for path in folder.rglob("*"):
                    if path.is_file():
                        zf.write(path, path.relative_to(root))
    print(f"created backup {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
