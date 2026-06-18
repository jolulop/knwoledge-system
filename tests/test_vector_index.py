from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

# LanceDB-backed: skipped unless the `vector` extra is installed (the full suite runs under
# `.[dev,vector]`). The embedding seam tests (test_embeddings.py) stay dependency-free.
pytest.importorskip("lancedb")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import validate_vector_index  # noqa: E402

from app.backend import vector_index as vi  # noqa: E402
from app.backend.vector_index import VectorIndexError  # noqa: E402


class FakeEmbedder:
    """Deterministic, order-preserving, key-free stand-in (same pattern as test_embeddings)."""

    def __init__(self, dimension: int = 8) -> None:
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [
            [hashlib.sha256(t.encode("utf-8")).digest()[i % 32] / 255.0 for i in range(self.dimension)]
            for t in texts
        ]


def _chunk(sid, ordinal, text, start, *, kind="prose", page=1):
    return {
        "chunk_id": f"{sid}::{ordinal:04d}", "source_id": sid, "ordinal": ordinal, "kind": kind,
        "heading_path": ["A", "B"], "section": "B", "text": text, "char_start": start,
        "char_end": start + len(text), "page": page, "page_end": page,
        "table_reference": None, "sheet_reference": None,
    }


def _write(root, sid, chunks):
    p = root / "normalized" / "chunks" / f"{sid}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(c) for c in chunks) + "\n", encoding="utf-8")


SRC_A = "src_aaaaaaaaaaaaaaaa"
SRC_B = "src_bbbbbbbbbbbbbbbb"


@pytest.fixture
def vault(tmp_path):
    _write(tmp_path, SRC_A, [_chunk(SRC_A, 0, "synergy capture", 0), _chunk(SRC_A, 1, "day one readiness", 20)])
    _write(tmp_path, SRC_B, [_chunk(SRC_B, 0, "carve out separation", 0)])
    return tmp_path


def _reindex(root, embedder=None, *, model="bge-m3", metric="cosine", force=False):
    return vi.reindex(root, embedder or FakeEmbedder(), embedding_model_ref=model,
                      distance_metric=metric, force=force)


# --------------------------------------------------------------------------- build / search


def test_full_build_indexes_all_chunks(vault):
    stats = _reindex(vault, force=True)
    assert stats.full_rebuild is True
    assert stats.chunks_embedded == 3 and stats.sources_indexed == 2
    assert vi.table_exists(vault)
    assert vi.load_meta(vault) == vi.VectorMeta("bge-m3", vi.EMBED_CODE_VERSION, "cosine", 8, vi.INDEX_SCHEMA_VERSION)


def test_search_returns_full_evidence_citation(vault):
    fe = FakeEmbedder()
    _reindex(vault, fe, force=True)
    hits = vi.search(vault, fe.embed(["synergy capture"])[0], limit=3, metric="cosine")
    top = hits[0]
    assert top["source_id"] == SRC_A and top["chunk_id"] == f"{SRC_A}::0000"
    # The full EvidenceHit field set is present (incl. kind + text for snippets).
    for field in ("kind", "ordinal", "section", "heading_path", "char_start", "char_end",
                  "page", "page_end", "table_reference", "sheet_reference", "text"):
        assert field in top
    assert json.loads(top["heading_path"]) == ["A", "B"]
    assert top["text"] == "synergy capture"
    assert "_distance" in top


def test_wiki_prose_is_not_embedded(vault):
    # Only per-source src_*.jsonl chunks are indexed; a stray legacy chunks.jsonl is ignored.
    (vault / "normalized" / "chunks" / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": "wiki/index.md::chunk-0", "path": "wiki/index.md", "text": "x"}) + "\n",
        encoding="utf-8",
    )
    _reindex(vault, force=True)
    assert set(vi.indexed_chunk_fingerprints(vault)) == {
        f"{SRC_A}::0000", f"{SRC_A}::0001", f"{SRC_B}::0000"
    }


# --------------------------------------------------------------------------- incremental


def test_incremental_no_change_embeds_nothing(vault):
    _reindex(vault, force=True)
    stats = _reindex(vault)
    assert stats.full_rebuild is False and stats.chunks_embedded == 0 and stats.chunks_deleted == 0


def test_incremental_reembeds_changed_and_removes_deleted(vault):
    _reindex(vault, force=True)
    # Edit one chunk of A (drop its 2nd chunk), add C, remove B.
    _write(vault, SRC_A, [_chunk(SRC_A, 0, "synergy capture EDITED", 0)])
    _write(vault, "src_cccccccccccccccc", [_chunk("src_cccccccccccccccc", 0, "new doc", 0)])
    (vault / "normalized" / "chunks" / f"{SRC_B}.jsonl").unlink()
    stats = _reindex(vault)
    assert stats.chunks_embedded == 2          # A::0000 changed + C::0000 new
    assert stats.chunks_deleted == 2           # A::0001 dropped + B::0000 removed
    ids = set(vi.indexed_chunk_fingerprints(vault))
    assert ids == {f"{SRC_A}::0000", "src_cccccccccccccccc::0000"}


def test_force_rebuilds_from_scratch(vault):
    _reindex(vault, force=True)
    _write(vault, SRC_A, [_chunk(SRC_A, 0, "only one now", 0)])
    stats = _reindex(vault, force=True)
    assert stats.full_rebuild is True
    assert set(vi.indexed_chunk_fingerprints(vault)) == {f"{SRC_A}::0000", f"{SRC_B}::0000"}


# --------------------------------------------------------------------------- staleness key


@pytest.mark.parametrize("kw", [{"model": "other-model"}, {"metric": "l2"}])
def test_index_level_mismatch_refuses_incremental(vault, kw):
    _reindex(vault, force=True)
    with pytest.raises(VectorIndexError):
        _reindex(vault, **kw)                  # changed identity without --force
    _reindex(vault, force=True, **kw)          # --force accepts the new identity


def test_dimension_change_refuses_incremental(vault):
    _reindex(vault, FakeEmbedder(8), force=True)
    with pytest.raises(VectorIndexError):
        _reindex(vault, FakeEmbedder(16))      # different embedder dimension


# --------------------------------------------------------------------------- atomic failure


def test_failed_embed_leaves_existing_index_intact(vault):
    _reindex(vault, force=True)
    before = set(vi.indexed_chunk_fingerprints(vault))

    class Boom:
        dimension = 8

        def embed(self, texts):
            raise RuntimeError("embedding server down")

    _write(vault, SRC_A, [_chunk(SRC_A, 0, "would change", 0)])
    with pytest.raises(RuntimeError):
        _reindex(vault, Boom(), force=True)
    # The live index is untouched (embed fails before the temp dir is even created).
    assert set(vi.indexed_chunk_fingerprints(vault)) == before
    assert not (vault / "indexes" / "vector.tmp").exists()  # never created (embed failed first)


# --------------------------------------------------------------------------- validator / status


def test_validator_passes_when_absent(tmp_path):
    assert validate_vector_index.main([str(tmp_path)]) == 0  # merely missing is OK


def test_validator_passes_but_reports_when_stale(vault):
    _reindex(vault, force=True)
    _write(vault, SRC_A, [_chunk(SRC_A, 0, "changed text", 0)])  # drift, no reindex
    st = vi.status(vault)
    assert st.present and st.coherent and st.stale_or_missing_chunks >= 1
    assert validate_vector_index.main([str(vault)]) == 0  # stale -> report, not fail


def test_validator_fails_on_incoherent_metadata(vault):
    _reindex(vault, force=True)
    # Corrupt the index-level schema version -> incoherent -> fail.
    meta = vi.meta_path(vault)
    data = json.loads(meta.read_text())
    data["index_schema_version"] = vi.INDEX_SCHEMA_VERSION + 99
    meta.write_text(json.dumps(data), encoding="utf-8")
    assert validate_vector_index.main([str(vault)]) == 1


def test_zero_chunk_full_rebuild(tmp_path):
    (tmp_path / "normalized" / "chunks").mkdir(parents=True)
    stats = _reindex(tmp_path, force=True)
    assert stats.full_rebuild is True and stats.chunks_embedded == 0
    assert vi.table_exists(tmp_path)
    assert vi.indexed_chunk_fingerprints(tmp_path) == {}


# --------------------------------------------------------------------------- Q1: index-level key


def test_status_flags_index_level_identity_mismatch(vault):
    _reindex(vault, force=True)  # built with model bge-m3, dim 8, cosine
    expected = vi.VectorMeta("OTHER-MODEL", vi.EMBED_CODE_VERSION, "cosine", 8, vi.INDEX_SCHEMA_VERSION)
    st = vi.status(vault, expected=expected)
    assert st.coherent is False and st.identity_checked is True
    assert any("embedding_model_ref" in i for i in st.issues)


def test_status_notes_when_embedder_disabled(vault):
    _reindex(vault, force=True)
    st = vi.status(vault)  # expected=None
    assert st.coherent is True and st.identity_checked is False
    assert any("identity not checked" in n for n in st.notes)


def test_validator_fails_on_identity_mismatch_when_configured(vault, monkeypatch):
    _reindex(vault, force=True)  # index identity = bge-m3 / 8 / cosine
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://127.0.0.1:8080/v1")
    monkeypatch.setenv("EMBEDDING_MODEL_REF", "DIFFERENT-model")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "8")
    assert validate_vector_index.main([str(vault)]) == 1


# --------------------------------------------------------------------------- Q2: swap rollback


def test_full_rebuild_swap_failure_rolls_back(vault, monkeypatch):
    _reindex(vault, force=True)
    before = set(vi.indexed_chunk_fingerprints(vault))
    _write(vault, SRC_A, [_chunk(SRC_A, 0, "would change everything", 0)])

    real_replace = vi.os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # fail on the second rename (tmp -> live)
            raise OSError("simulated rename failure")
        return real_replace(src, dst)

    monkeypatch.setattr(vi.os, "replace", flaky)
    with pytest.raises(OSError):
        _reindex(vault, force=True)
    # Old live index restored + usable; the .bak backup is not left as the only copy.
    assert set(vi.indexed_chunk_fingerprints(vault)) == before
    assert not (vault / "indexes" / "vector.bak").exists()


# --------------------------------------------------------------------------- Q3: incremental atomicity


def test_incremental_upsert_failure_leaves_old_rows(vault, monkeypatch):
    _reindex(vault, force=True)
    before = vi.indexed_chunk_fingerprints(vault)
    _write(vault, SRC_A, [_chunk(SRC_A, 0, "CHANGED text", 0), _chunk(SRC_A, 1, "day one readiness", 20)])

    def boom(tbl, rows):
        raise RuntimeError("simulated upsert failure")

    monkeypatch.setattr(vi, "_upsert", boom)
    with pytest.raises(RuntimeError):
        _reindex(vault)
    # The upsert never applied -> the previous rows (old fingerprints) remain.
    assert vi.indexed_chunk_fingerprints(vault) == before


def test_incremental_delete_failure_leaves_validator_detectable_stale(vault, monkeypatch):
    _reindex(vault, force=True)
    (vault / "normalized" / "chunks" / f"{SRC_B}.jsonl").unlink()  # remove a source

    def boom(tbl, ids):
        raise RuntimeError("simulated delete failure")

    monkeypatch.setattr(vi, "_delete", boom)
    with pytest.raises(RuntimeError):
        _reindex(vault)
    # The removed source's rows linger -> the validator reports them as stale-removed.
    st = vi.status(vault)
    assert st.removed_chunks >= 1
    assert validate_vector_index.main([str(vault)]) == 0  # stale -> warn, not fail
