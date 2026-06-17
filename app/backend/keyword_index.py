#!/usr/bin/env python3
"""Phase 4a deterministic keyword index (`indexes/keyword/keyword.sqlite`, ADR-0032).

Two FTS5 indexes are built from durable derived state, with separate schemas and
separate ranking (ADR-0032 decision 2):

- **evidence** — one row per chunk in ``normalized/chunks/<source_id>.jsonl``. This is the
  *citable* corpus: a hit carries the structured citation, and the authoritative anchor stays
  ``(source_id, char_start, char_end)`` plus optional page/section/table (ADR-0019/0020);
  ``chunk_id`` is advisory. Only the chunk ``text`` is tokenized; every citation field is an
  ``UNINDEXED`` column so it round-trips on a hit. Wiki node prose is **never** indexed here —
  generated prose is not a source.
- **navigation** — one row per *typed* wiki page (``wiki/**/*.md`` with a recognized ``type``
  frontmatter). Title, aliases, tags and the ``> [!summary]`` callout are tokenized; status and
  identity fields are ``UNINDEXED``. ``answer_eligible`` is true only for an ``active`` page whose
  type is node prose that may back an answer (concept/entity/person/organization/project/
  synthesis/claim) — never for a source/query/tag page, and never for a non-``active`` page
  (ADR-0032 retrieval-eligibility invariant). Status is stored, never filtered here; retention
  filtering is a query-time (`/search`) concern.

The index is **derived and gitignored** (ADR-0014): a full rebuild from chunks + wiki is cheap,
so it is never backed up. Rebuilds are **incremental and fingerprinted** (ADR-0023/0027): only a
changed/added/removed source or page is touched (delete + reinsert its rows); ``--force`` forces a
full rebuild, as does an index whose ``PRAGMA user_version`` no longer matches ``INDEX_VERSION``.
Dependency-free (stdlib ``sqlite3``); requires SQLite built with FTS5.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Bump when the index schema or row shape changes; recorded via PRAGMA user_version so a stale
# on-disk index is detected and fully rebuilt rather than silently mixed (ADR-0032 §7).
INDEX_VERSION = 1

# Path of the derived keyword index, relative to the project root (ADR-0032 §7).
DB_RELPATH = Path("indexes") / "keyword" / "keyword.sqlite"

# Wiki page types whose *active* node prose is eligible to back an answer (ADR-0032 decision 2).
# Source/query/tag pages are navigation aids only and are never answer_eligible; their evidence
# (for sources) flows through the chunk evidence index, not the page prose.
ANSWER_ELIGIBLE_TYPES = frozenset(
    {"concept", "entity", "person", "organization", "project", "synthesis", "claim"}
)

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS evidence USING fts5(
    source_id UNINDEXED,
    chunk_id UNINDEXED,
    ordinal UNINDEXED,
    kind UNINDEXED,
    heading_path UNINDEXED,
    section UNINDEXED,
    char_start UNINDEXED,
    char_end UNINDEXED,
    page UNINDEXED,
    page_end UNINDEXED,
    table_reference UNINDEXED,
    sheet_reference UNINDEXED,
    text,
    tokenize = 'unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS navigation USING fts5(
    path UNINDEXED,
    page_type UNINDEXED,
    node_id UNINDEXED,
    status UNINDEXED,
    review_status UNINDEXED,
    language UNINDEXED,
    answer_eligible UNINDEXED,
    title,
    aliases,
    tags,
    summary,
    tokenize = 'unicode61'
);
CREATE TABLE IF NOT EXISTS source_fingerprints (
    source_id   TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nav_fingerprints (
    path        TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL
);
"""

_ALL_TABLES = ("evidence", "navigation", "source_fingerprints", "nav_fingerprints")


@dataclass
class ReindexStats:
    """What a reindex pass did, for the CLI/maintenance summary (no page bodies)."""

    full_rebuild: bool = False
    evidence_sources_indexed: int = 0
    evidence_sources_removed: int = 0
    evidence_chunks: int = 0
    navigation_pages_indexed: int = 0
    navigation_pages_removed: int = 0
    skipped: int = 0  # unchanged sources + pages left untouched
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- parsing helpers
# Frontmatter/summary parsing matches the project subset used by rebuild_index.py: scalar
# `key: value` lines and simple bracket lists. Kept self-contained so the index does not depend
# on a script module.


def parse_value(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip("\"'") for item in inner.split(",") if item.strip()]
    return raw.strip("\"'")


def parse_frontmatter(text: str) -> dict[str, Any]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    data: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = parse_value(value)
    return data


def extract_summary(text: str) -> str:
    """Return the joined body of the first ``> [!summary]`` callout, or ``""`` if none."""
    in_callout = False
    parts: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("> [!summary]"):
            in_callout = True
            # A title may follow the marker on the same line (e.g. "> [!summary] Title").
            tail = stripped[len("> [!summary]"):].strip()
            if tail:
                parts.append(tail)
            continue
        if in_callout:
            if stripped.startswith(">"):
                body = stripped.lstrip(">").strip()
                if body:
                    parts.append(body)
            else:
                break
    return " ".join(parts)


def _page_title(path: Path, fm: dict[str, Any], text: str) -> str:
    if fm.get("title"):
        return str(fm["title"])
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem.replace("-", " ").title()


def _as_terms(value: Any) -> str:
    """Flatten a frontmatter list/scalar into a single space-joined searchable string."""
    if isinstance(value, list):
        return " ".join(str(v) for v in value if str(v).strip())
    return str(value).strip()


def _fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


# --------------------------------------------------------------------------- row builders


def _evidence_rows(chunk_path: Path) -> tuple[list[tuple], int, list[str]]:
    """Parse one ``normalized/chunks/<source_id>.jsonl`` into evidence insert tuples.

    Returns ``(rows, chunk_count, warnings)``. Records missing the citation anchor fields
    (``source_id`` / ``char_start`` / ``char_end``) are skipped — they are not citable evidence
    (this also excludes the retired path-keyed ``chunks.jsonl`` shape).
    """
    rows: list[tuple] = []
    warnings: list[str] = []
    for lineno, line in enumerate(chunk_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            warnings.append(f"{chunk_path.name}:{lineno}: malformed JSON, skipped")
            continue
        if rec.get("source_id") is None or rec.get("char_start") is None or rec.get("char_end") is None:
            warnings.append(f"{chunk_path.name}:{lineno}: missing citation anchor, skipped")
            continue
        rows.append(
            (
                rec["source_id"],
                rec.get("chunk_id", ""),
                rec.get("ordinal"),
                rec.get("kind", ""),
                json.dumps(rec.get("heading_path") or [], ensure_ascii=False),
                rec.get("section"),
                rec["char_start"],
                rec["char_end"],
                rec.get("page"),
                rec.get("page_end"),
                rec.get("table_reference"),
                rec.get("sheet_reference"),
                rec.get("text", ""),
            )
        )
    # Deterministic insert order: by ordinal then char_start.
    rows.sort(key=lambda r: (r[2] if r[2] is not None else 0, r[6]))
    return rows, len(rows), warnings


def navigation_row(path: Path, root: Path) -> tuple | None:
    """Build a navigation insert tuple for one wiki page, or ``None`` for an untyped page.

    Untyped pages (``wiki/index.md``, ``wiki/log.md`` — no ``type`` frontmatter) are skipped:
    they are navigation surfaces themselves, not discoverable node/source pages.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    fm = parse_frontmatter(text)
    page_type = fm.get("type")
    if not page_type or not isinstance(page_type, str):
        return None
    node_id = (
        fm.get(f"{page_type}_id")
        or fm.get("node_id")
        or fm.get("source_id")
        or fm.get("query_id")
        or ""
    )
    status = str(fm.get("status") or "unknown")
    answer_eligible = status == "active" and page_type in ANSWER_ELIGIBLE_TYPES
    return (
        path.relative_to(root).as_posix(),
        page_type,
        str(node_id),
        status,
        str(fm.get("review_status") or "none"),
        str(fm.get("language") or "unknown"),
        "1" if answer_eligible else "0",
        _page_title(path, fm, text),
        _as_terms(fm.get("aliases", [])),
        _as_terms(fm.get("tags", [])),
        extract_summary(text),
    )


# --------------------------------------------------------------------------- connection / schema


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        ).fetchall()
    }


def _ensure_schema(conn: sqlite3.Connection) -> None:
    try:
        conn.executescript(_SCHEMA)
    except sqlite3.OperationalError as exc:  # pragma: no cover - environment without FTS5
        raise RuntimeError(
            "SQLite FTS5 is required to build the keyword index but is unavailable: " f"{exc}"
        ) from exc
    conn.execute(f"PRAGMA user_version = {INDEX_VERSION}")


def _drop_all(conn: sqlite3.Connection) -> None:
    for table in _ALL_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def index_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def indexed_source_ids(conn: sqlite3.Connection) -> set[str]:
    if "evidence" not in _tables(conn):
        return set()
    return {r[0] for r in conn.execute("SELECT DISTINCT source_id FROM evidence").fetchall()}


def indexed_navigation_paths(conn: sqlite3.Connection) -> set[str]:
    if "navigation" not in _tables(conn):
        return set()
    return {r[0] for r in conn.execute("SELECT DISTINCT path FROM navigation").fetchall()}


def stored_source_fingerprints(conn: sqlite3.Connection) -> dict[str, str]:
    if "source_fingerprints" not in _tables(conn):
        return {}
    return {r[0]: r[1] for r in conn.execute("SELECT source_id, fingerprint FROM source_fingerprints")}


def stored_nav_fingerprints(conn: sqlite3.Connection) -> dict[str, str]:
    if "nav_fingerprints" not in _tables(conn):
        return {}
    return {r[0]: r[1] for r in conn.execute("SELECT path, fingerprint FROM nav_fingerprints")}


def file_fingerprint(path: Path) -> str:
    """Current fingerprint of a file on disk (matches what reindex stores)."""
    return _fingerprint(Path(path).read_bytes())


# --------------------------------------------------------------------------- live disk sets


def chunk_files(root: Path) -> dict[str, Path]:
    """Map ``source_id`` -> chunk file for the per-source chunk JSONL files on disk.

    Only ``src_*.jsonl`` files are considered; the retired path-keyed ``chunks.jsonl`` (ADR-0032)
    is excluded by the naming convention. This is the indexer's boundary: it indexes the
    normalized chunks present on disk and does **not** consult manifests/ingestion_status —
    ``validate_normalized.py`` (run as part of ``validate_all.py``) is the gate that catches
    orphan/stale normalized outputs before they are exposed to search.
    """
    chunk_dir = root / "normalized" / "chunks"
    if not chunk_dir.exists():
        return {}
    return {p.stem: p for p in sorted(chunk_dir.glob("src_*.jsonl"))}


def _wiki_pages(root: Path) -> list[Path]:
    wiki = root / "wiki"
    if not wiki.exists():
        return []
    return sorted(wiki.rglob("*.md"))


def navigation_pages(root: Path) -> dict[str, Path]:
    """Map ``wiki-relative path`` -> page for the *typed* wiki pages on disk.

    Untyped pages (``wiki/index.md``, ``wiki/log.md``) are excluded, matching what the navigation
    index stores. This is the live set the index-consistency validator compares fingerprints
    against in both directions.
    """
    root = Path(root).resolve()
    out: dict[str, Path] = {}
    for path in _wiki_pages(root):
        if navigation_row(path, root) is not None:
            out[path.relative_to(root).as_posix()] = path
    return out


# --------------------------------------------------------------------------- reindex


def reindex(root: Path, *, force: bool = False) -> ReindexStats:
    """Incrementally (re)build the keyword index under ``<root>/indexes/keyword/``."""
    root = Path(root).resolve()
    conn = connect(root / DB_RELPATH)
    stats = ReindexStats()
    try:
        existing_tables = _tables(conn)
        schema_present = {"evidence", "navigation"} <= existing_tables
        full = force or not schema_present or index_version(conn) != INDEX_VERSION
        if full:
            _drop_all(conn)
        _ensure_schema(conn)
        stats.full_rebuild = full

        # --- evidence index (per-source chunks) ---
        source_files = chunk_files(root)
        stored_sources = {
            r[0]: r[1] for r in conn.execute("SELECT source_id, fingerprint FROM source_fingerprints")
        }
        for source_id in sorted(set(stored_sources) - set(source_files)):
            conn.execute("DELETE FROM evidence WHERE source_id = ?", (source_id,))
            conn.execute("DELETE FROM source_fingerprints WHERE source_id = ?", (source_id,))
            stats.evidence_sources_removed += 1
        for source_id, chunk_path in source_files.items():
            fp = _fingerprint(chunk_path.read_bytes())
            if stored_sources.get(source_id) == fp:
                stats.skipped += 1
                continue
            rows, count, warnings = _evidence_rows(chunk_path)
            stats.warnings.extend(warnings)
            conn.execute("DELETE FROM evidence WHERE source_id = ?", (source_id,))
            conn.executemany(
                "INSERT INTO evidence(source_id, chunk_id, ordinal, kind, heading_path, section, "
                "char_start, char_end, page, page_end, table_reference, sheet_reference, text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.execute(
                "INSERT INTO source_fingerprints(source_id, fingerprint) VALUES (?, ?) "
                "ON CONFLICT(source_id) DO UPDATE SET fingerprint = excluded.fingerprint",
                (source_id, fp),
            )
            stats.evidence_sources_indexed += 1
            stats.evidence_chunks += count

        # --- navigation index (typed wiki pages) ---
        pages = {p.relative_to(root).as_posix(): p for p in _wiki_pages(root)}
        stored_pages = {
            r[0]: r[1] for r in conn.execute("SELECT path, fingerprint FROM nav_fingerprints")
        }
        live_paths: set[str] = set()
        for rel, page_path in pages.items():
            fp = _fingerprint(page_path.read_bytes())
            row = navigation_row(page_path, root)
            if row is None:
                continue  # untyped page (index.md/log.md): not a navigation node
            live_paths.add(rel)
            if stored_pages.get(rel) == fp:
                stats.skipped += 1
                continue
            conn.execute("DELETE FROM navigation WHERE path = ?", (rel,))
            conn.execute(
                "INSERT INTO navigation(path, page_type, node_id, status, review_status, language, "
                "answer_eligible, title, aliases, tags, summary) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            conn.execute(
                "INSERT INTO nav_fingerprints(path, fingerprint) VALUES (?, ?) "
                "ON CONFLICT(path) DO UPDATE SET fingerprint = excluded.fingerprint",
                (rel, fp),
            )
            stats.navigation_pages_indexed += 1
        for rel in sorted(set(stored_pages) - live_paths):
            conn.execute("DELETE FROM navigation WHERE path = ?", (rel,))
            conn.execute("DELETE FROM nav_fingerprints WHERE path = ?", (rel,))
            stats.navigation_pages_removed += 1

        conn.commit()
    finally:
        conn.close()
    return stats
