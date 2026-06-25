from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import validate_index_consistency  # noqa: E402

from app.backend import keyword_index  # noqa: E402


# --------------------------------------------------------------------------- fixtures


def _chunk(source_id: str, ordinal: int, text: str, start: int, *, page: int | None = None) -> dict:
    return {
        "chunk_id": f"{source_id}::{ordinal:04d}",
        "source_id": source_id,
        "ordinal": ordinal,
        "kind": "prose",
        "heading_path": [],
        "section": None,
        "text": text,
        "char_start": start,
        "char_end": start + len(text),
        "page": page,
        "page_end": page,
        "table_reference": None,
        "sheet_reference": None,
    }


def _write_chunks(root: Path, source_id: str, chunks: list[dict]) -> None:
    path = root / "normalized" / "chunks" / f"{source_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in chunks) + "\n", encoding="utf-8"
    )


def _write_page(root: Path, rel: str, frontmatter: dict, summary: str, body: str = "") -> Path:
    fm_lines = "\n".join(f"{k}: {json.dumps(v) if isinstance(v, list) else v}" for k, v in frontmatter.items())
    text = f"---\n{fm_lines}\n---\n\n# {frontmatter.get('title', 'Page')}\n\n> [!summary]\n> {summary}\n\n{body}\n"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _seed(root: Path) -> None:
    # Two sources, citation anchors arranged so markdown[start:end] == text holds by construction.
    a0 = "AI reduces total execution effort in technology by fifteen percent."
    a1 = "Synergies cascade across finance and operations when systems falter."
    _write_chunks(root, "src_aaaaaaaaaaaaaaaa", [
        _chunk("src_aaaaaaaaaaaaaaaa", 0, a0, 0, page=1),
        _chunk("src_aaaaaaaaaaaaaaaa", 1, a1, len(a0) + 2, page=2),
    ])
    b0 = "Carve-outs depend on day one readiness and clean data reconciliation."
    _write_chunks(root, "src_bbbbbbbbbbbbbbbb", [_chunk("src_bbbbbbbbbbbbbbbb", 0, b0, 0, page=1)])

    # Typed wiki pages: an active concept (answer_eligible), a candidate concept (not), a source.
    _write_page(root, "wiki/Concepts/cpt_merger.md", {
        "type": "concept", "concept_id": "cpt_merger", "title": "Post-merger integration",
        "status": "active", "review_status": "none", "confidence": "high",
        "aliases": ["PMI", "post-merger integration"], "tags": ["mergers"],
    }, "Integrating two companies after a merger to capture synergies.")
    _write_page(root, "wiki/Concepts/cpt_carveout.md", {
        "type": "concept", "concept_id": "cpt_carveout", "title": "Carve-out",
        "status": "candidate", "review_status": "pending", "confidence": "low",
        "aliases": [], "tags": [],
    }, "Separating a business unit from its parent.")
    _write_page(root, "wiki/Sources/src_aaaaaaaaaaaaaaaa.md", {
        "type": "source", "source_id": "src_aaaaaaaaaaaaaaaa", "title": "AI in M&A",
        "status": "active", "language": "en", "aliases": [], "tags": [],
    }, "Extractive excerpt about AI in M&A technology workstreams.")
    # Untyped pages must be skipped by the navigation index.
    (root / "wiki" / "index.md").write_text("# Index\n\nNo frontmatter here.\n", encoding="utf-8")
    (root / "wiki" / "log.md").write_text("# Log\n\nAppend-only.\n", encoding="utf-8")


def _conn(root: Path) -> sqlite3.Connection:
    return keyword_index.connect(root / keyword_index.DB_RELPATH)


# --------------------------------------------------------------------------- build / shape


def test_full_build_indexes_chunks_and_typed_pages(tmp_path):
    _seed(tmp_path)
    stats = keyword_index.reindex(tmp_path, force=True)

    assert stats.full_rebuild is True
    assert stats.evidence_sources_indexed == 2
    assert stats.evidence_chunks == 3
    # Two concepts + one source; index.md and log.md (untyped) are skipped.
    assert stats.navigation_pages_indexed == 3

    conn = _conn(tmp_path)
    try:
        assert keyword_index.index_version(conn) == keyword_index.INDEX_VERSION
        assert conn.execute("SELECT count(*) FROM evidence").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM navigation").fetchone()[0] == 3
    finally:
        conn.close()


def test_evidence_hit_carries_citation_anchor(tmp_path):
    _seed(tmp_path)
    keyword_index.reindex(tmp_path, force=True)
    conn = _conn(tmp_path)
    try:
        rows = conn.execute(
            "SELECT source_id, chunk_id, ordinal, char_start, char_end, page, text "
            "FROM evidence WHERE evidence MATCH 'synergies'"
        ).fetchall()
        assert len(rows) == 1
        hit = rows[0]
        assert hit["source_id"] == "src_aaaaaaaaaaaaaaaa"
        assert hit["ordinal"] == 1
        # Authoritative citation anchor round-trips and bounds match the chunk text length.
        assert hit["char_end"] - hit["char_start"] == len(hit["text"])
        assert hit["chunk_id"] == "src_aaaaaaaaaaaaaaaa::0001"
        assert hit["page"] == 2
    finally:
        conn.close()


def test_navigation_answer_eligibility(tmp_path):
    _seed(tmp_path)
    keyword_index.reindex(tmp_path, force=True)
    conn = _conn(tmp_path)
    try:
        eligible = {
            r["path"]: r["answer_eligible"]
            for r in conn.execute("SELECT path, answer_eligible FROM navigation")
        }
        assert eligible["wiki/Concepts/cpt_merger.md"] == "1"   # active concept -> eligible
        assert eligible["wiki/Concepts/cpt_carveout.md"] == "0"  # candidate -> not eligible
        assert eligible["wiki/Sources/src_aaaaaaaaaaaaaaaa.md"] == "0"  # source prose never eligible
    finally:
        conn.close()


def test_navigation_search_matches_title_summary_aliases(tmp_path):
    _seed(tmp_path)
    keyword_index.reindex(tmp_path, force=True)
    conn = _conn(tmp_path)
    try:
        # alias term
        by_alias = conn.execute("SELECT path FROM navigation WHERE navigation MATCH 'PMI'").fetchall()
        assert [r["path"] for r in by_alias] == ["wiki/Concepts/cpt_merger.md"]
        # summary term
        by_summary = conn.execute(
            "SELECT path FROM navigation WHERE navigation MATCH 'synergies'"
        ).fetchall()
        assert "wiki/Concepts/cpt_merger.md" in [r["path"] for r in by_summary]
    finally:
        conn.close()


# --------------------------------------------------------------------------- incremental


def test_incremental_skips_unchanged(tmp_path):
    _seed(tmp_path)
    keyword_index.reindex(tmp_path, force=True)
    stats = keyword_index.reindex(tmp_path)  # no changes
    assert stats.full_rebuild is False
    assert stats.evidence_sources_indexed == 0
    assert stats.navigation_pages_indexed == 0
    assert stats.evidence_sources_removed == 0
    assert stats.navigation_pages_removed == 0
    assert stats.skipped == 5  # 2 sources + 3 pages untouched


def test_incremental_reindexes_only_changed_source(tmp_path):
    _seed(tmp_path)
    keyword_index.reindex(tmp_path, force=True)
    # Change only src_bbb's content.
    new_text = "Updated: TSA exits require careful license and contract review."
    _write_chunks(tmp_path, "src_bbbbbbbbbbbbbbbb", [_chunk("src_bbbbbbbbbbbbbbbb", 0, new_text, 0, page=1)])
    stats = keyword_index.reindex(tmp_path)

    assert stats.evidence_sources_indexed == 1  # only the changed source
    conn = _conn(tmp_path)
    try:
        hit = conn.execute(
            "SELECT text FROM evidence WHERE source_id = 'src_bbbbbbbbbbbbbbbb'"
        ).fetchone()
        assert hit["text"] == new_text
        # The unchanged source's rows are intact.
        assert conn.execute(
            "SELECT count(*) FROM evidence WHERE source_id = 'src_aaaaaaaaaaaaaaaa'"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_incremental_removes_deleted_source_and_page(tmp_path):
    _seed(tmp_path)
    keyword_index.reindex(tmp_path, force=True)
    (tmp_path / "normalized" / "chunks" / "src_bbbbbbbbbbbbbbbb.jsonl").unlink()
    (tmp_path / "wiki" / "Concepts" / "cpt_carveout.md").unlink()
    stats = keyword_index.reindex(tmp_path)

    assert stats.evidence_sources_removed == 1
    assert stats.navigation_pages_removed == 1
    conn = _conn(tmp_path)
    try:
        assert conn.execute(
            "SELECT count(*) FROM evidence WHERE source_id = 'src_bbbbbbbbbbbbbbbb'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM navigation WHERE path = 'wiki/Concepts/cpt_carveout.md'"
        ).fetchone()[0] == 0
        assert "src_bbbbbbbbbbbbbbbb" not in keyword_index.indexed_source_ids(conn)
    finally:
        conn.close()


def test_stale_schema_triggers_full_rebuild(tmp_path):
    _seed(tmp_path)
    keyword_index.reindex(tmp_path, force=True)
    conn = _conn(tmp_path)
    conn.execute("PRAGMA user_version = 999")
    conn.commit()
    conn.close()

    stats = keyword_index.reindex(tmp_path)  # not forced, but version mismatch
    assert stats.full_rebuild is True
    assert stats.evidence_sources_indexed == 2


# --------------------------------------------------------------------------- legacy / robustness


def test_legacy_path_keyed_chunks_jsonl_is_ignored(tmp_path):
    _seed(tmp_path)
    # The retired path-keyed scaffold shape (no source_id) must never be indexed as evidence.
    (tmp_path / "normalized" / "chunks" / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": "wiki/index.md::chunk-0", "path": "wiki/index.md", "text": "x"}) + "\n",
        encoding="utf-8",
    )
    keyword_index.reindex(tmp_path, force=True)
    conn = _conn(tmp_path)
    try:
        # Only the two real per-source files were indexed.
        assert keyword_index.indexed_source_ids(conn) == {
            "src_aaaaaaaaaaaaaaaa", "src_bbbbbbbbbbbbbbbb"
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------- validator


def test_validator_passes_on_fresh_index(tmp_path):
    _seed(tmp_path)
    keyword_index.reindex(tmp_path, force=True)
    assert validate_index_consistency.main([str(tmp_path)]) == 0


def test_validator_passes_when_no_index(tmp_path):
    _seed(tmp_path)  # no reindex -> no index file
    assert validate_index_consistency.main([str(tmp_path)]) == 0


def test_validator_fails_on_stale_evidence_source(tmp_path):
    _seed(tmp_path)
    keyword_index.reindex(tmp_path, force=True)
    # Delete a chunk file without reindexing: the index now references missing evidence.
    (tmp_path / "normalized" / "chunks" / "src_bbbbbbbbbbbbbbbb.jsonl").unlink()
    assert validate_index_consistency.main([str(tmp_path)]) == 1


def test_validator_fails_on_stale_navigation_page(tmp_path):
    _seed(tmp_path)
    keyword_index.reindex(tmp_path, force=True)
    (tmp_path / "wiki" / "Concepts" / "cpt_merger.md").unlink()
    assert validate_index_consistency.main([str(tmp_path)]) == 1


def test_validator_fails_when_new_chunk_file_not_indexed(tmp_path):
    _seed(tmp_path)
    keyword_index.reindex(tmp_path, force=True)
    # A new source appears on disk but the index was not rebuilt: completeness gap.
    _write_chunks(tmp_path, "src_cccccccccccccccc", [_chunk("src_cccccccccccccccc", 0, "New evidence.", 0)])
    assert validate_index_consistency.main([str(tmp_path)]) == 1
    # Reindexing closes the gap.
    keyword_index.reindex(tmp_path)
    assert validate_index_consistency.main([str(tmp_path)]) == 0


def test_validator_fails_on_chunk_fingerprint_drift(tmp_path):
    _seed(tmp_path)
    keyword_index.reindex(tmp_path, force=True)
    # Same source_id, changed content, no reindex: stale index.
    _write_chunks(tmp_path, "src_bbbbbbbbbbbbbbbb", [_chunk("src_bbbbbbbbbbbbbbbb", 0, "Edited content.", 0)])
    assert validate_index_consistency.main([str(tmp_path)]) == 1


def test_validator_fails_when_new_page_not_indexed(tmp_path):
    _seed(tmp_path)
    keyword_index.reindex(tmp_path, force=True)
    _write_page(tmp_path, "wiki/Concepts/cpt_new.md", {
        "type": "concept", "concept_id": "cpt_new", "title": "New concept",
        "status": "active", "aliases": [], "tags": [],
    }, "A freshly authored concept.")
    assert validate_index_consistency.main([str(tmp_path)]) == 1


def test_validator_fails_on_page_fingerprint_drift(tmp_path):
    _seed(tmp_path)
    keyword_index.reindex(tmp_path, force=True)
    page = tmp_path / "wiki" / "Concepts" / "cpt_merger.md"
    page.write_text(page.read_text(encoding="utf-8") + "\nEdited body.\n", encoding="utf-8")
    assert validate_index_consistency.main([str(tmp_path)]) == 1


def test_indexer_indexes_chunks_without_manifests(tmp_path):
    # Boundary decision (ADR-0032, Phase 4a): the indexer indexes on-disk src_*.jsonl and does
    # NOT consult manifests/ingestion_status. validate_normalized.py is the gate for orphan/stale
    # normalized outputs. This test pins that behavior: a manifest-less chunk file is still indexed.
    _write_chunks(tmp_path, "src_orphanaaaaaaaa", [_chunk("src_orphanaaaaaaaa", 0, "Orphan chunk text.", 0)])
    stats = keyword_index.reindex(tmp_path, force=True)
    assert stats.evidence_sources_indexed == 1
    conn = _conn(tmp_path)
    try:
        assert "src_orphanaaaaaaaa" in keyword_index.indexed_source_ids(conn)
    finally:
        conn.close()


def test_connect_readonly_rejects_writes(tmp_path):
    # connect_readonly opens mode=ro: an existing index can be read but never written (the eval's
    # --vault boundary). A write must raise, proving an operator vault is safe under the eval connector.
    path = tmp_path / keyword_index.DB_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    keyword_index.connect(path).close()                 # create a real on-disk DB file first
    ro = keyword_index.connect_readonly(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("CREATE TABLE x (a)")
    finally:
        ro.close()
