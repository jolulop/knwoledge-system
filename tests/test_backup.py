from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import backup  # noqa: E402


def test_backup_includes_all_gitignored_runtime_state(tmp_path):
    # Lay down a sample of each durability-critical area.
    for rel, content in {
        "wiki/log.md": "log",
        "wiki/index.md": "index",
        "wiki/Sources/src_x.md": "page",
        "raw/manifests/src_x.json": "{}",
        "db/jobs.sqlite": "jobs",
        "db/metadata.sqlite": "meta",
        "indexes/keyword/idx.bin": "k",
        "policies/citation.yaml": "c",
    }.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    out = backup.create_backup(tmp_path)
    names = set(zipfile.ZipFile(out).namelist())

    for required in (
        "wiki/log.md", "wiki/index.md", "wiki/Sources/src_x.md",
        "raw/manifests/src_x.json", "db/jobs.sqlite", "db/metadata.sqlite",
        "indexes/keyword/idx.bin", "policies/citation.yaml",
    ):
        assert required in names, f"backup missing {required}"

    # The backup must not recurse the backups/ dir into itself.
    assert not any(n.startswith("backups/") for n in names)
