from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import validate_raw_integrity as v  # noqa: E402
from app.workers import intake  # noqa: E402


def _intake(tmp_path: Path) -> Path:
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "doc.md").write_text("original immutable content\n", encoding="utf-8")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    return tmp_path


def test_unchanged_raw_passes(tmp_path):
    _intake(tmp_path)
    assert v.main([str(tmp_path)]) == 0


def test_mutated_raw_file_hard_fails(tmp_path):
    _intake(tmp_path)
    doc = tmp_path / "raw" / "inbox" / "doc.md"
    # Mutate bytes after intake (changes size + mtime → pre-filter trips → re-hash).
    time.sleep(0.01)
    doc.write_text("TAMPERED content with a different length entirely\n", encoding="utf-8")
    assert v.main([str(tmp_path)]) == 1


def test_no_manifests_passes(tmp_path):
    assert v.main([str(tmp_path)]) == 0
