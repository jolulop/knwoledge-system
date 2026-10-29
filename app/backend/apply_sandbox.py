#!/usr/bin/env python3
"""ADR-0040 dry-run sandbox: a fully self-contained copy of the vault for previewing apply.

The dry-run runs the **same** `run_apply()` orchestration against a throwaway copy of the vault, then
`diff_states()` produces the semantic mutation plan by comparing a pre-run snapshot to the post-run
state. No live path is reachable from the sandbox (copies only — never symlinks), so the preview
cannot touch live state by construction (ADR-0040 decisions 1-2).

Copied: the writable domains the executors mutate (`db/` minus the LLM cache, `reviews/`, `wiki/`,
`raw/manifests/`), the read-only inputs + code the in-process executors and the subprocess
validators/`rebuild_index.py` need (`scripts/`, `templates/`, `policies/`, `normalized/`, `app/`),
and the **manifest-referenced** raw bytes only (`relative_raw_path` + every
`occurrences[].relative_path`; un-manifested `raw/inbox/` staging is never copied). A catalogued raw
file absent live is simply not copied, so `validate_raw_integrity` in the sandbox reports the same
condition live apply would (the dry-run predicts live; it is not its own integrity bar). Not copied:
`indexes/` (regenerated in-sandbox, excluded from the diff), the vector DB, `llm_cache`, the Apply
authorizations.

ADR-0068 decision 6: the vault state is copied under the write lock (SQLite through the online
backup), the code and the raw bytes after it, from the copied manifests.
"""
from __future__ import annotations

import difflib
import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.backend import graph
from app.backend.config import Settings, get_settings
from app.backend.paths import safe_under

# Writable domains (executors mutate these) + read-only inputs/code the orchestration needs. `app/`
# and `scripts/` are required because the subprocess validators + rebuild_index.py import `app` and
# self-bootstrap from their own directory (sys.path[0] = sandbox), so they must resolve from the copy.
# ADR-0068 decision 6: what a writer can change is copied under the write lock; code after it.
_COPY_STATE = ("reviews", "wiki", "raw/manifests", "normalized", "policies")
_COPY_CODE = ("app", "scripts", "templates")
_DB_NEVER_COPIED = {"llm_cache.sqlite", "apply_authorizations.json"}
_SQLITE_SIDE = ("-wal", "-shm", "-journal")
# wiki/index.md is wholesale-regenerated navigation (derived churn) — excluded from the page diff.
_WIKI_DIFF_EXCLUDE = {"wiki/index.md"}


def _catalogued_raw_rels(manifests_dir: Path) -> set[str]:
    """Every catalogued raw path (relative_raw_path + occurrences[].relative_path), like ADR-0039."""
    rels: set[str] = set()
    if not manifests_dir.exists():
        return rels
    for mpath in sorted(manifests_dir.rglob("*.json")):
        try:
            data = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):  # untrusted on-disk state — carried into sandbox for validators
            continue
        rel = data.get("relative_raw_path")
        if isinstance(rel, str) and rel:
            rels.add(rel)
        occ = data.get("occurrences")
        for o in occ if isinstance(occ, list) else []:
            r = o.get("relative_path") if isinstance(o, dict) else None
            if isinstance(r, str) and r:
                rels.add(r)
    return rels


def build_sandbox(settings: Settings) -> tuple[Path, Settings]:
    """Build a throwaway sandbox copy and return (tmp_root, sandbox_settings).

    The vault state is copied under the write lock (:func:`copy_state`), the code and raw bytes
    after it (:func:`copy_code_and_raw`); the preview (`app/backend/apply_gate.py`) runs the two
    steps itself, so it can read the vault revision in the same hold."""
    from app.backend import write_lock
    with write_lock.hold(settings.reviews_dir, authority=False, changes=False):
        tmp_root = copy_state(settings)
    try:
        copy_code_and_raw(settings, tmp_root)
    except Exception:
        cleanup_sandbox(tmp_root)
        raise
    return tmp_root, get_settings(tmp_root)


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _copy_sqlite(src: Path, dst: Path) -> None:
    """A consistent copy through SQLite's online backup (ADR-0068 decision 6), as backups take."""
    source = sqlite3.connect(src)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def copy_state(settings: Settings) -> Path:
    """Copy what a writer can change into a new temp dir and return it. The caller holds the write
    lock (``changes=False``), so the copy is one coherent state: `db/` (SQLite files through the
    online backup; their `-wal`/`-shm`/`-journal` side files, lock files, the LLM cache and the Apply
    authorizations never), `reviews/`, `wiki/`, `raw/manifests/`, `normalized/` and `policies/`.

    Owns cleanup of its own partial temp dir on failure."""
    root = settings.root
    tmp_root = Path(tempfile.mkdtemp(prefix="ks-dryrun-"))
    try:
        db_src = root / "db"
        if db_src.exists():
            (tmp_root / "db").mkdir(parents=True, exist_ok=True)
            for p in db_src.iterdir():
                if (not p.is_file() or p.name in _DB_NEVER_COPIED or p.name.endswith(_SQLITE_SIDE)
                        or p.name.endswith(".lock")):
                    continue
                if _is_sqlite(p):
                    _copy_sqlite(p, tmp_root / "db" / p.name)
                else:
                    shutil.copy2(p, tmp_root / "db" / p.name)
        for rel in _COPY_STATE:
            src = root / rel
            if src.exists():
                shutil.copytree(src, tmp_root / rel, dirs_exist_ok=True)
        return tmp_root
    except Exception:
        cleanup_sandbox(tmp_root)
        raise


def copy_code_and_raw(settings: Settings, tmp_root: Path) -> None:
    """After the lock: copy the code (`app/`, `scripts/`, `templates/`) and the raw bytes the
    **copied** manifests name, never the live ones (an approved deletion or a restore may change the
    live catalogue meanwhile; the preview's revision check catches it). Raw files are never edited.

    Untrusted manifest raw paths are containment-checked with ``safe_under`` (ADR-0009) before any
    stat/copy, so a tampered absolute/`..` path can neither probe outside ``raw/`` nor write outside
    the sandbox. A catalogued file absent live is skipped (reflect live, never invent)."""
    root = settings.root
    for rel in _COPY_CODE:
        src = root / rel
        if src.exists():
            shutil.copytree(src, tmp_root / rel, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    raw_base = root / "raw"
    for rel in _catalogued_raw_rels(tmp_root / "raw" / "manifests"):
        src = safe_under(root, raw_base, rel)
        if src is None or not src.is_file():
            continue
        dest = tmp_root / rel  # rel passed safe_under (no abs/.. ) so this stays under tmp_root
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def cleanup_sandbox(tmp_root: Path) -> None:
    shutil.rmtree(tmp_root, ignore_errors=True)


@dataclass
class StateSnapshot:
    nodes: dict[str, dict[str, str]] = field(default_factory=dict)        # node_id -> {type, status}
    # edge_id -> {src, rel, dst, status, review_id}; all governed statuses (ADR-0040 decision 4), so a
    # reject/supersede that never touches the active set still surfaces as a status change.
    edges: dict[str, dict[str, str]] = field(default_factory=dict)
    wiki: dict[str, str] = field(default_factory=dict)                    # "wiki/..md" -> content
    reviews: dict[str, str] = field(default_factory=dict)                 # review_id -> status_dir
    manifests: dict[str, str] = field(default_factory=dict)              # source_id -> status


def _graph_snapshot(graph_db: Path) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    nodes: dict[str, dict[str, str]] = {}
    edges: dict[str, dict[str, str]] = {}
    if not Path(graph_db).exists():
        return nodes, edges
    conn = graph.connect(graph_db)
    try:
        if graph.schema_version(conn) != graph.SCHEMA_VERSION:
            return nodes, edges
        for r in conn.execute("SELECT node_id, node_type, status FROM nodes"):
            nodes[r["node_id"]] = {"type": r["node_type"], "status": r["status"] or ""}
        for r in conn.execute(
                "SELECT edge_id, src_id, dst_id, edge_type, status, review_id FROM edges"):
            edges[r["edge_id"]] = {"src": r["src_id"], "rel": r["edge_type"], "dst": r["dst_id"],
                                   "status": r["status"], "review_id": r["review_id"] or ""}
    finally:
        conn.close()
    return nodes, edges


def snapshot_state(settings: Settings) -> StateSnapshot:
    """Capture the durable state the dry-run diffs: graph nodes/edges, wiki pages, review file
    locations, and manifest statuses."""
    snap = StateSnapshot()
    snap.nodes, snap.edges = _graph_snapshot(settings.graph_db_path)

    wiki_dir = settings.wiki_dir
    if wiki_dir.exists():
        for p in sorted(wiki_dir.rglob("*.md")):
            rel = str(p.relative_to(settings.root))
            if rel in _WIKI_DIFF_EXCLUDE:
                continue
            snap.wiki[rel] = p.read_text(encoding="utf-8", errors="replace")

    for d in ("pending", "approved", "rejected"):
        folder = settings.reviews_dir / d
        if folder.exists():
            for p in folder.glob("*.json"):
                snap.reviews[p.stem] = d

    md = settings.manifests_dir
    if md.exists():
        for p in sorted(md.rglob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                sid = data.get("source_id") or p.stem
                snap.manifests[sid] = data.get("status", "active")
    return snap


def diff_states(before: StateSnapshot, after: StateSnapshot) -> dict[str, Any]:
    """Semantic, deliberately-small diff: graph node lifecycle + all governed edge status deltas
    (added/removed/status-changed across {proposed,active,rejected,superseded}, stable ids), wiki
    unified text diffs (path-scoped), review workflow moves, manifest field-level status changes."""
    nodes_status_changed = []
    nodes_added = []
    for nid, info in sorted(after.nodes.items()):
        if nid not in before.nodes:
            nodes_added.append({"id": nid, "type": info["type"], "status": info["status"]})
        elif before.nodes[nid]["status"] != info["status"]:
            nodes_status_changed.append({"id": nid, "type": info["type"],
                                         "from": before.nodes[nid]["status"], "to": info["status"]})
    edges_added, edges_removed, edges_status_changed, edges_repointed = [], [], [], []
    for eid in sorted(set(before.edges) | set(after.edges)):
        b, a = before.edges.get(eid), after.edges.get(eid)
        if b is None and a is not None:
            edges_added.append({"src": a["src"], "rel": a["rel"], "dst": a["dst"],
                                "status": a["status"], "review_id": a["review_id"] or None})
        elif a is None and b is not None:
            edges_removed.append({"src": b["src"], "rel": b["rel"], "dst": b["dst"],
                                  "status": b["status"]})
        elif b is not None and a is not None:
            # ADR-0050: an in-place endpoint re-point keeps the SAME edge_id (+ usually status), so it is
            # invisible to the status-delta check — surface it explicitly so the dry-run exposes the
            # rekeying itself, not only its tombstone/wiki side effects.
            if b["src"] != a["src"] or b["dst"] != a["dst"]:
                edges_repointed.append({"rel": a["rel"], "from_src": b["src"], "from_dst": b["dst"],
                                        "to_src": a["src"], "to_dst": a["dst"], "status": a["status"],
                                        "review_id": a["review_id"] or None})
            if b["status"] != a["status"]:
                edges_status_changed.append({"src": a["src"], "rel": a["rel"], "dst": a["dst"],
                                             "from": b["status"], "to": a["status"],
                                             "review_id": a["review_id"] or None})

    wiki = []
    for path in sorted(set(before.wiki) | set(after.wiki)):
        b, a = before.wiki.get(path, ""), after.wiki.get(path, "")
        if b != a:
            ud = "".join(difflib.unified_diff(
                b.splitlines(keepends=True), a.splitlines(keepends=True),
                fromfile=path, tofile=path))
            wiki.append({"path": path, "unified_diff": ud})

    reviews = [{"review_id": rid, "from_dir": before.reviews.get(rid), "to_dir": after.reviews.get(rid)}
               for rid in sorted(set(before.reviews) | set(after.reviews))
               if before.reviews.get(rid) != after.reviews.get(rid)]
    manifests = [{"source_id": sid, "field": "status",
                  "from": before.manifests.get(sid), "to": after.manifests.get(sid)}
                 for sid in sorted(set(before.manifests) | set(after.manifests))
                 if before.manifests.get(sid) != after.manifests.get(sid)]

    return {
        "graph": {"edges_added": edges_added, "edges_removed": edges_removed,
                  "edges_status_changed": edges_status_changed, "edges_repointed": edges_repointed,
                  "nodes_status_changed": nodes_status_changed, "nodes_added": nodes_added},
        "wiki": wiki, "reviews": reviews, "manifests": manifests,
    }


def empty_graph_diff() -> dict[str, list]:
    return {"edges_added": [], "edges_removed": [], "edges_status_changed": [], "edges_repointed": [],
            "nodes_status_changed": [], "nodes_added": []}


def diff_is_empty(diff: dict[str, Any]) -> bool:
    g = diff["graph"]
    return not (g["edges_added"] or g["edges_removed"] or g["edges_status_changed"]
                or g.get("edges_repointed") or g["nodes_status_changed"] or g["nodes_added"]
                or diff["wiki"] or diff["reviews"] or diff["manifests"])
