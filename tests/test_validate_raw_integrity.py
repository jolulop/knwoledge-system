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


import json  # noqa: E402


def _manifest_with_relpath(tmp_path: Path, rel: str) -> Path:
    md = tmp_path / "raw" / "manifests"
    md.mkdir(parents=True, exist_ok=True)
    (md / "src_0123456789abcdef.json").write_text(json.dumps({
        "source_id": "src_0123456789abcdef", "sha256": "0" * 64,
        "relative_raw_path": rel, "occurrences": [{"relative_path": rel}],
    }), encoding="utf-8")
    return tmp_path


def test_absolute_relpath_rejected_no_outside_read(tmp_path, capsys):
    secret = tmp_path / "outside_secret.txt"
    secret.write_text("top secret\n", encoding="utf-8")
    _manifest_with_relpath(tmp_path, str(secret))     # absolute path escaping raw/
    assert v.main([str(tmp_path)]) == 1               # integrity violation, not silent pass
    out = capsys.readouterr().out
    assert "escapes raw/" in out
    assert str(secret) not in out                     # no absolute-path leak


def test_dotdot_relpath_rejected(tmp_path, capsys):
    _manifest_with_relpath(tmp_path, "raw/../../etc/passwd")
    assert v.main([str(tmp_path)]) == 1
    assert "escapes raw/" in capsys.readouterr().out


def test_noncanonical_source_id_fails_without_leak(tmp_path, capsys):
    md = tmp_path / "raw" / "manifests"
    md.mkdir(parents=True)
    (md / "x.json").write_text(json.dumps({
        "source_id": "../../evil\ninjected", "sha256": "0" * 64,
        "relative_raw_path": "raw/inbox/a.md", "occurrences": [{"relative_path": "raw/inbox/a.md"}],
    }), encoding="utf-8")
    assert v.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "non-canonical source_id" in out
    assert "injected" not in out  # the untrusted id is never echoed
