"""Guard against operational-surface drift: every scripts/*.py referenced by a tracked hook,
skill, or the README must exist on disk.

This is the regression test for the Slice 4a miss where ``scripts/reindex_vector.py`` was deleted
but still invoked by the reindex hook and both vault skills (ADR-0032 §7 "change together").
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Tracked surfaces that instruct a human or the harness to run scripts.
OPERATIONAL_FILES = [
    ROOT / ".claude" / "hooks" / "reindex_changed_file.sh",
    ROOT / ".claude" / "skills" / "vault-ingest" / "SKILL.md",
    ROOT / ".claude" / "skills" / "vault-maintenance" / "SKILL.md",
    ROOT / "README.md",
]

SCRIPT_REF_RE = re.compile(r"scripts/([A-Za-z0-9_./-]+\.py)")


def test_operational_files_reference_only_existing_scripts():
    missing: list[str] = []
    checked = 0
    for surface in OPERATIONAL_FILES:
        if not surface.exists():
            continue
        for match in SCRIPT_REF_RE.findall(surface.read_text(encoding="utf-8")):
            checked += 1
            if not (ROOT / "scripts" / match).exists():
                missing.append(f"{surface.relative_to(ROOT)} references missing scripts/{match}")
    assert checked > 0, "expected to find script references to validate"
    assert not missing, "operational surfaces reference deleted scripts:\n" + "\n".join(missing)


def test_per_file_hook_does_not_run_vector_reindex():
    # ADR-0033 §5: vector re-embedding is GPU/latency-heavy and explicit-only — it must never be
    # wired into the per-file change hook (which would make editing depend on the embedding server).
    hook = ROOT / ".claude" / "hooks" / "reindex_changed_file.sh"
    assert "reindex_vector" not in hook.read_text(encoding="utf-8")


def test_env_example_documents_query_model():
    # The POST /query 503 tells operators to set QUERY_MODEL — it must be documented in .env.example
    # so that guidance isn't a dead end (ADR-0034 operational drift).
    assert "QUERY_MODEL=" in (ROOT / ".env.example").read_text(encoding="utf-8")
