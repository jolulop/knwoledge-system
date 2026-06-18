#!/usr/bin/env python3
"""Phase 4d-2 vector index over citable chunk evidence (LanceDB, ADR-0033).

Embeds the **same per-source chunks** as the keyword evidence index (`normalized/chunks/
<source_id>.jsonl`) into a local **LanceDB** store under `indexes/vector/`, carrying the full
`EvidenceHit` citation field set (+ the chunk `text` for snippets) so a vector hit is the identical
evidence object as a keyword hit (4d-3). Wiki prose is never embedded.

Guardrails (pinned for this slice):
- **LanceDB is isolated here + in `scripts/reindex_vector.py`.** `lancedb`/`pyarrow` are imported
  lazily inside the functions that need them, so importing this module (or app startup / `/search`)
  never requires the optional dependency. `lancedb_available()` lets callers degrade cleanly.
- **Full rebuild is atomic:** built into a temp dir, then swapped — a failed embed/server call never
  leaves a half-valid live index.
- **Incremental** embeds changed chunks *before* deleting old rows; removed chunks/sources are
  deleted. Per-row staleness is `chunk_fingerprint`.
- **`_meta.json` is authoritative for index-level staleness.** Any mismatch in `embedding_model_ref`,
  `embedding_code_version`, `distance_metric`, `dimension`, or `index_schema_version` **refuses
  incremental** and tells the operator to rerun with `--force`.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.backend import keyword_index
from app.backend.embeddings import Embedder

VECTOR_RELDIR = Path("indexes") / "vector"
META_FILENAME = "_meta.json"
TABLE_NAME = "evidence_vectors"
# Bump when the LanceDB row schema changes; part of the index-level staleness key (full rebuild).
INDEX_SCHEMA_VERSION = 1
# Bump when the embedding/index *code* (chunking→row mapping, fingerprint rule) changes.
EMBED_CODE_VERSION = 1
_EMBED_BATCH = 64


class VectorIndexError(RuntimeError):
    """A vector index operation failed (stale index, missing dependency, bad input)."""


def lancedb_available() -> bool:
    try:
        import lancedb  # noqa: F401
        import pyarrow  # noqa: F401
    except ImportError:
        return False
    return True


# --------------------------------------------------------------------------- index-level staleness


@dataclass(frozen=True)
class VectorMeta:
    embedding_model_ref: str
    embedding_code_version: int
    distance_metric: str
    dimension: int
    index_schema_version: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VectorMeta:
        return cls(
            embedding_model_ref=str(d["embedding_model_ref"]),
            embedding_code_version=int(d["embedding_code_version"]),
            distance_metric=str(d["distance_metric"]),
            dimension=int(d["dimension"]),
            index_schema_version=int(d["index_schema_version"]),
        )


def meta_path(root: Path) -> Path:
    return Path(root) / VECTOR_RELDIR / META_FILENAME


def load_meta(root: Path) -> VectorMeta | None:
    path = meta_path(root)
    if not path.exists():
        return None
    try:
        return VectorMeta.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        raise VectorIndexError(f"vector index {META_FILENAME} is unreadable/incoherent: {exc}") from exc


def _save_meta(index_dir: Path, meta: VectorMeta) -> None:
    (index_dir / META_FILENAME).write_text(
        json.dumps(meta.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------- chunk reading


def _chunk_fingerprint(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()[:16]


def disk_chunks(root: Path) -> dict[str, dict[str, Any]]:
    """Map ``chunk_id`` -> {citation fields + text + chunk_fingerprint} for all per-source chunks.

    Records missing the citation anchor (`source_id`/`char_start`/`char_end`) are skipped — they are
    not citable evidence (mirrors the keyword evidence index).
    """
    out: dict[str, dict[str, Any]] = {}
    for source_id, path in keyword_index.chunk_files(Path(root)).items():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("source_id") is None or rec.get("char_start") is None or rec.get("char_end") is None:
                continue
            cid = rec.get("chunk_id") or f"{rec['source_id']}::{rec.get('ordinal')}"
            out[cid] = {
                "source_id": rec["source_id"],
                "chunk_id": cid,
                "ordinal": rec.get("ordinal"),
                "kind": rec.get("kind", ""),
                "section": rec.get("section"),
                "heading_path": json.dumps(rec.get("heading_path") or [], ensure_ascii=False),
                "char_start": rec["char_start"],
                "char_end": rec["char_end"],
                "page": rec.get("page"),
                "page_end": rec.get("page_end"),
                "table_reference": rec.get("table_reference"),
                "sheet_reference": rec.get("sheet_reference"),
                "text": rec.get("text", ""),
                "chunk_fingerprint": _chunk_fingerprint(line),
            }
    return out


def disk_chunk_fingerprints(root: Path) -> dict[str, str]:
    return {cid: rec["chunk_fingerprint"] for cid, rec in disk_chunks(root).items()}


# --------------------------------------------------------------------------- LanceDB plumbing (lazy)


def _pa_schema(dimension: int):
    import pyarrow as pa

    return pa.schema([
        pa.field("vector", pa.list_(pa.float32(), dimension)),
        pa.field("source_id", pa.string()),
        pa.field("chunk_id", pa.string()),
        pa.field("ordinal", pa.int64()),
        pa.field("kind", pa.string()),
        pa.field("section", pa.string()),
        pa.field("heading_path", pa.string()),
        pa.field("char_start", pa.int64()),
        pa.field("char_end", pa.int64()),
        pa.field("page", pa.int64()),
        pa.field("page_end", pa.int64()),
        pa.field("table_reference", pa.string()),
        pa.field("sheet_reference", pa.string()),
        pa.field("text", pa.string()),
        pa.field("chunk_fingerprint", pa.string()),
        pa.field("embedding_model_ref", pa.string()),
    ])


def _row(record: dict[str, Any], vector: list[float], model_ref: str) -> dict[str, Any]:
    return {**record, "vector": vector, "embedding_model_ref": model_ref}


def _embed_texts(embedder: Embedder, texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), _EMBED_BATCH):
        out.extend(embedder.embed(texts[i:i + _EMBED_BATCH]))
    return out


def _in_clause(column: str, values: list[str]) -> str:
    quoted = ",".join("'" + v.replace("'", "''") + "'" for v in values)
    return f"{column} IN ({quoted})"


def _upsert(tbl: Any, rows: list[dict[str, Any]]) -> None:
    """Atomic LanceDB upsert keyed on ``chunk_id`` (a failed upsert leaves the old row intact)."""
    tbl.merge_insert("chunk_id").when_matched_update_all().when_not_matched_insert_all().execute(rows)


def _delete(tbl: Any, chunk_ids: list[str]) -> None:
    tbl.delete(_in_clause("chunk_id", chunk_ids))


def _swap_dir(tmp: Path, live: Path) -> None:
    """Atomically replace ``live`` with ``tmp`` via rename-rename, rolling back on failure.

    If the final ``tmp -> live`` rename fails, the previous index is restored from the backup so the
    live index is never left absent — then the error propagates so the operator knows the rebuild
    failed (decision Q2).
    """
    bak = live.parent / (live.name + ".bak")
    if bak.exists():
        shutil.rmtree(bak)
    if live.exists():
        os.replace(live, bak)
    try:
        os.replace(tmp, live)
    except OSError:
        if bak.exists() and not live.exists():
            os.replace(bak, live)  # rollback: restore the previous index
        raise
    if bak.exists():
        shutil.rmtree(bak)


# --------------------------------------------------------------------------- reindex


@dataclass
class ReindexStats:
    full_rebuild: bool = False
    sources_indexed: int = 0
    chunks_embedded: int = 0
    chunks_deleted: int = 0
    chunks_total: int = 0


def reindex(
    root: Path,
    embedder: Embedder,
    *,
    embedding_model_ref: str,
    distance_metric: str,
    force: bool = False,
) -> ReindexStats:
    """(Re)build the vector index. ``--force`` / first build → atomic full rebuild; otherwise an
    incremental re-embed of changed chunks. Refuses incremental against an index-level mismatch."""
    if not lancedb_available():
        raise VectorIndexError(
            "lancedb is not installed; install the 'vector' extra (uv pip install '.[vector]')"
        )
    root = Path(root).resolve()
    expected = VectorMeta(
        embedding_model_ref=embedding_model_ref,
        embedding_code_version=EMBED_CODE_VERSION,
        distance_metric=distance_metric,
        dimension=embedder.dimension,
        index_schema_version=INDEX_SCHEMA_VERSION,
    )
    existing = load_meta(root)
    if force or existing is None:
        return _build_full(root, embedder, expected)
    if existing != expected:
        raise VectorIndexError(
            f"vector index is stale at the index level (stored {existing.to_dict()} != "
            f"expected {expected.to_dict()}); rerun with --force to fully rebuild"
        )
    return _incremental(root, embedder, expected)


def _build_full(root: Path, embedder: Embedder, meta: VectorMeta) -> ReindexStats:
    import lancedb
    import pyarrow as pa

    chunks = disk_chunks(root)
    ordered = [chunks[cid] for cid in sorted(chunks)]
    vectors = _embed_texts(embedder, [r["text"] for r in ordered])  # embed all BEFORE any write
    rows = [_row(r, v, meta.embedding_model_ref) for r, v in zip(ordered, vectors)]

    tmp = root / "indexes" / "vector.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    db = lancedb.connect(str(tmp))
    table = pa.Table.from_pylist(rows, schema=_pa_schema(meta.dimension))
    db.create_table(TABLE_NAME, data=table, mode="overwrite")
    _save_meta(tmp, meta)
    _swap_dir(tmp, root / VECTOR_RELDIR)
    (root / VECTOR_RELDIR / ".gitkeep").touch()

    return ReindexStats(
        full_rebuild=True,
        sources_indexed=len({r["source_id"] for r in rows}),
        chunks_embedded=len(rows),
        chunks_total=len(rows),
    )


def _incremental(root: Path, embedder: Embedder, meta: VectorMeta) -> ReindexStats:
    import lancedb

    live = root / VECTOR_RELDIR
    db = lancedb.connect(str(live))
    tbl = db.open_table(TABLE_NAME)
    on_disk = disk_chunks(root)
    n = tbl.count_rows()
    indexed = {
        r["chunk_id"]: r["chunk_fingerprint"]
        for r in tbl.search().select(["chunk_id", "chunk_fingerprint"]).limit(max(1, n)).to_list()
    }
    to_upsert = [cid for cid in sorted(on_disk) if indexed.get(cid) != on_disk[cid]["chunk_fingerprint"]]
    to_delete = [cid for cid in sorted(indexed) if cid not in on_disk]

    rows: list[dict[str, Any]] = []
    if to_upsert:  # embed BEFORE deleting old rows
        vectors = _embed_texts(embedder, [on_disk[cid]["text"] for cid in to_upsert])
        rows = [_row(on_disk[cid], v, meta.embedding_model_ref) for cid, v in zip(to_upsert, vectors)]

    if rows:  # atomic upsert keyed on chunk_id (changed/new) — old row survives a failed upsert
        _upsert(tbl, rows)
    if to_delete:  # removed chunks; a failed delete leaves benign stale rows the validator reports
        _delete(tbl, to_delete)
    _save_meta(live, meta)
    (live / ".gitkeep").touch()

    return ReindexStats(
        full_rebuild=False,
        sources_indexed=len({on_disk[cid]["source_id"] for cid in to_upsert}),
        chunks_embedded=len(rows),
        chunks_deleted=len(to_delete),
        chunks_total=tbl.count_rows(),
    )


# --------------------------------------------------------------------------- read (validator / 4d-3)


def indexed_chunk_fingerprints(root: Path) -> dict[str, str]:
    """``chunk_id`` -> stored ``chunk_fingerprint`` from the live table (for staleness reporting)."""
    import lancedb

    live = Path(root) / VECTOR_RELDIR
    db = lancedb.connect(str(live))
    tbl = db.open_table(TABLE_NAME)
    n = tbl.count_rows()
    return {
        r["chunk_id"]: r["chunk_fingerprint"]
        for r in tbl.search().select(["chunk_id", "chunk_fingerprint"]).limit(max(1, n)).to_list()
    }


def table_exists(root: Path) -> bool:
    if not lancedb_available():
        return False
    import lancedb

    live = Path(root) / VECTOR_RELDIR
    if not live.exists():
        return False
    try:
        lancedb.connect(str(live)).open_table(TABLE_NAME)
    except Exception:  # noqa: BLE001 - any open failure means the table is absent/unusable
        return False
    return True


def search(root: Path, query_vector: list[float], *, limit: int, metric: str) -> list[dict[str, Any]]:
    """ANN search the vector index; rows carry the full citation fields + `_distance` (4d-3 maps to
    the evidence shape). Deterministic tie-break is applied by the caller (source_id, ordinal)."""
    import lancedb

    live = Path(root) / VECTOR_RELDIR
    db = lancedb.connect(str(live))
    tbl = db.open_table(TABLE_NAME)
    return tbl.search(query_vector).metric(metric).limit(limit).to_list()


@dataclass
class VectorStatus:
    present: bool
    coherent: bool
    issues: list[str] = field(default_factory=list)   # incoherence/unsafe -> validator FAILS
    notes: list[str] = field(default_factory=list)     # informational -> validator WARNS, passes
    stale_or_missing_chunks: int = 0
    removed_chunks: int = 0
    inspected: bool = False        # chunk-level staleness checkable (needs lancedb)
    identity_checked: bool = False  # index-level model/dim/metric compared (needs embedder config)


def status(root: Path, *, expected: VectorMeta | None = None) -> VectorStatus:
    """Report vector-index health for the validator (Q1 split).

    - Missing index → ``present=False`` (pass).
    - **Incoherent** (`issues`, validator fails): unreadable/missing ``_meta.json`` while files exist;
      ``index_schema_version`` or ``embedding_code_version`` != current; and — when ``expected`` is
      given (an embedder is configured) — ``embedding_model_ref``/``dimension``/``distance_metric``
      mismatch; or metadata present but the LanceDB table missing. Such an index is unsafe to query.
    - **Notes** (`notes`, validator warns/passes): embedder disabled so identity was not compared;
      the ``vector`` extra absent so the table/chunks could not be inspected.
    - **Chunk drift** (`stale_or_missing_chunks`/`removed_chunks`): rebuildable-stale, reported as a
      warning — never a failure (drift is by design; reindex is explicit).
    """
    root = Path(root)
    has_meta = meta_path(root).exists()
    index_dir = root / VECTOR_RELDIR
    has_dir = index_dir.exists() and any(
        p.name not in {".gitkeep", META_FILENAME} for p in index_dir.glob("*")
    )
    if not has_meta and not has_dir:
        return VectorStatus(present=False, coherent=True)

    try:
        meta = load_meta(root)
    except VectorIndexError as exc:
        return VectorStatus(present=True, coherent=False, issues=[str(exc)])
    if meta is None:
        return VectorStatus(
            present=True, coherent=False,
            issues=["vector index files present but _meta.json is missing"],
        )

    issues: list[str] = []
    notes: list[str] = []
    if meta.index_schema_version != INDEX_SCHEMA_VERSION:
        issues.append(
            f"index_schema_version {meta.index_schema_version} != current {INDEX_SCHEMA_VERSION}; "
            "rerun reindex_vector.py --force"
        )
    if meta.embedding_code_version != EMBED_CODE_VERSION:
        issues.append(
            f"embedding_code_version {meta.embedding_code_version} != current {EMBED_CODE_VERSION}; "
            "rerun reindex_vector.py --force"
        )
    if expected is not None:
        if meta.embedding_model_ref != expected.embedding_model_ref:
            issues.append(
                f"embedding_model_ref {meta.embedding_model_ref!r} != configured "
                f"{expected.embedding_model_ref!r}; rerun reindex_vector.py --force"
            )
        if meta.dimension != expected.dimension:
            issues.append(f"dimension {meta.dimension} != configured {expected.dimension}; rerun --force")
        if meta.distance_metric != expected.distance_metric:
            issues.append(
                f"distance_metric {meta.distance_metric!r} != configured "
                f"{expected.distance_metric!r}; rerun reindex_vector.py --force"
            )
    else:
        notes.append("vector identity not checked because the embedder is disabled (no EMBEDDING_MODEL_REF)")

    st = VectorStatus(present=True, coherent=not issues, issues=issues, notes=notes,
                      identity_checked=expected is not None)
    if not lancedb_available():
        st.notes.append("install the 'vector' extra to inspect the LanceDB table + chunk staleness")
        return st
    if not table_exists(root):
        st.coherent = False
        st.issues.append("vector index metadata present but the LanceDB table is missing")
        return st

    st.inspected = True
    on_disk = disk_chunk_fingerprints(root)
    indexed = indexed_chunk_fingerprints(root)
    st.stale_or_missing_chunks = sum(1 for cid, fp in on_disk.items() if indexed.get(cid) != fp)
    st.removed_chunks = sum(1 for cid in indexed if cid not in on_disk)
    return st
