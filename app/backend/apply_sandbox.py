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
`indexes/` (regenerated in-sandbox, excluded from the diff), the vector DB, `llm_cache`.
"""
from __future__ import annotations

import difflib
import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.backend import graph
from app.backend.config import Settings, get_settings
from app.backend.paths import safe_under

_CACHE_FILENAME = "llm_cache.sqlite"
# Writable domains (executors mutate these) + read-only inputs/code the orchestration needs. `app/`
# and `scripts/` are required because the subprocess validators + rebuild_index.py import `app` and
# self-bootstrap from their own directory (sys.path[0] = sandbox), so they must resolve from the copy.
_COPY_WRITABLE = ("reviews", "wiki", "raw/manifests")
_COPY_READONLY = ("scripts", "templates", "policies", "normalized", "app")
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

    Owns cleanup of its own partial temp dir on failure (callers get a usable sandbox or an exception,
    never a half-built tree to clean up). Untrusted manifest raw paths are containment-checked with
    ``safe_under`` (ADR-0009) before any stat/copy, so a tampered absolute/`..` path can neither probe
    outside ``raw/`` nor write outside the sandbox.
    """
    root = settings.root
    tmp_root = Path(tempfile.mkdtemp(prefix="ks-dryrun-"))
    try:
        # db/ minus the LLM cache (cost-saver, not correctness; never needed to predict apply).
        db_src = root / "db"
        if db_src.exists():
            (tmp_root / "db").mkdir(parents=True, exist_ok=True)
            for p in db_src.iterdir():
                if p.is_file() and p.name != _CACHE_FILENAME:
                    shutil.copy2(p, tmp_root / "db" / p.name)

        for rel in (*_COPY_WRITABLE, *_COPY_READONLY):
            src = root / rel
            if src.exists():
                shutil.copytree(src, tmp_root / rel, dirs_exist_ok=True)

        # Manifest-referenced raw bytes only. safe_under rejects absolute/`..`/escaping paths (untrusted
        # manifest); a catalogued file absent live is simply skipped (reflect live, never invent).
        raw_base = root / "raw"
        for rel in _catalogued_raw_rels(settings.manifests_dir):
            src = safe_under(root, raw_base, rel)
            if src is None or not src.is_file():
                continue
            dest = tmp_root / rel  # rel passed safe_under (no abs/.. ) so this stays under tmp_root
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)

        return tmp_root, get_settings(tmp_root)
    except Exception:
        cleanup_sandbox(tmp_root)
        raise


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
    """Semantic, deliberately-small diff: graph node lifecycle + active-edge deltas (stable ids), wiki
    unified text diffs (path-scoped), review workflow moves, manifest field-level status changes."""
    nodes_status_changed = []
    nodes_added = []
    for nid, info in sorted(after.nodes.items()):
        if nid not in before.nodes:
            nodes_added.append({"id": nid, "type": info["type"], "status": info["status"]})
        elif before.nodes[nid]["status"] != info["status"]:
            nodes_status_changed.append({"id": nid, "type": info["type"],
                                         "from": before.nodes[nid]["status"], "to": info["status"]})
    edges_added, edges_removed, edges_status_changed = [], [], []
    for eid in sorted(set(before.edges) | set(after.edges)):
        b, a = before.edges.get(eid), after.edges.get(eid)
        if b is None and a is not None:
            edges_added.append({"src": a["src"], "rel": a["rel"], "dst": a["dst"],
                                "status": a["status"], "review_id": a["review_id"] or None})
        elif a is None and b is not None:
            edges_removed.append({"src": b["src"], "rel": b["rel"], "dst": b["dst"],
                                  "status": b["status"]})
        elif b is not None and a is not None and b["status"] != a["status"]:
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
                  "edges_status_changed": edges_status_changed,
                  "nodes_status_changed": nodes_status_changed, "nodes_added": nodes_added},
        "wiki": wiki, "reviews": reviews, "manifests": manifests,
    }


def empty_graph_diff() -> dict[str, list]:
    return {"edges_added": [], "edges_removed": [], "edges_status_changed": [],
            "nodes_status_changed": [], "nodes_added": []}


def diff_is_empty(diff: dict[str, Any]) -> bool:
    g = diff["graph"]
    return not (g["edges_added"] or g["edges_removed"] or g["edges_status_changed"]
                or g["nodes_status_changed"] or g["nodes_added"]
                or diff["wiki"] or diff["reviews"] or diff["manifests"])
