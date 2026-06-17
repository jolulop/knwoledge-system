from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import backup  # noqa: E402


def _seed(tmp_path: Path) -> None:
    for rel, content in {
        "wiki/log.md": "log",
        "wiki/index.md": "index",
        "wiki/Sources/src_x.md": "page",
        "raw/manifests/src_x.json": "{}",
        "db/jobs.sqlite": "jobs",
        "db/graph.sqlite": "graph",          # authoritative graph -> backed up (lives in db/)
        "db/llm_cache.sqlite": "cache",
        "indexes/keyword/keyword.sqlite": "k",  # derived -> NOT backed up (cheap rebuild)
        "indexes/vector/data.lance": "v",       # derived -> opt-in only
        "policies/citation.yaml": "c",
    }.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_backup_includes_durable_state_and_graph(tmp_path):
    _seed(tmp_path)
    out = backup.create_backup(tmp_path)
    names = set(zipfile.ZipFile(out).namelist())

    for required in (
        "wiki/log.md", "wiki/index.md", "wiki/Sources/src_x.md",
        "raw/manifests/src_x.json", "db/jobs.sqlite", "db/graph.sqlite",
        "db/llm_cache.sqlite", "policies/citation.yaml",
    ):
        assert required in names, f"backup missing {required}"

    # The backup must not recurse the backups/ dir into itself.
    assert not any(n.startswith("backups/") for n in names)


def test_backup_excludes_derived_indexes_by_default(tmp_path):
    _seed(tmp_path)
    out = backup.create_backup(tmp_path)
    names = set(zipfile.ZipFile(out).namelist())

    # ADR-0032 §7: keyword index never backed up; vector index opt-in only.
    assert "indexes/keyword/keyword.sqlite" not in names
    assert "indexes/vector/data.lance" not in names
    assert not any(n.startswith("indexes/") for n in names)


def test_backup_includes_vector_index_on_opt_in(tmp_path, monkeypatch):
    _seed(tmp_path)
    monkeypatch.setenv("BACKUP_INCLUDE_VECTOR_INDEX", "1")
    out = backup.create_backup(tmp_path)
    names = set(zipfile.ZipFile(out).namelist())

    assert "indexes/vector/data.lance" in names
    # Opting into the vector index must not also pull in the keyword index.
    assert "indexes/keyword/keyword.sqlite" not in names


def test_backup_env_flags_compose(tmp_path, monkeypatch):
    # Vector opt-in and cache opt-out are independent env flags that must compose.
    _seed(tmp_path)
    monkeypatch.setenv("BACKUP_INCLUDE_VECTOR_INDEX", "1")
    monkeypatch.setenv("BACKUP_EXCLUDE_LLM_CACHE", "1")
    out = backup.create_backup(tmp_path)
    names = set(zipfile.ZipFile(out).namelist())

    assert "indexes/vector/data.lance" in names       # vector included
    assert "db/llm_cache.sqlite" not in names          # cache excluded
    assert "db/graph.sqlite" in names                  # graph still backed up
    assert "indexes/keyword/keyword.sqlite" not in names
