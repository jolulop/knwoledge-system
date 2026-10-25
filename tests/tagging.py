"""Test helper (ADR-0066): keep tag suggestions — the way an item becomes active now."""
from __future__ import annotations

from pathlib import Path

from app.backend import graph
from app.workers import identity_journal, tag_links

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def keep_all(root: Path, *, node_ids: set[str] | None = None, source_ids: set[str] | None = None) -> int:
    """Keep every active suggestion (optionally only `node_ids` / only on `source_ids`); returns
    the number of keeps. Re-renders the affected Source pages like the UI batch does."""
    root = Path(root)
    gconn = graph.connect(root / "db" / "graph.sqlite")
    affected: set[str] = set()
    n = 0
    try:
        sids = [r["node_id"] for r in gconn.execute(
            "SELECT node_id FROM nodes WHERE node_type = 'source' ORDER BY node_id")]
        for sid in sids:
            if source_ids is not None and sid not in source_ids:
                continue
            for link in graph.source_tag_links(gconn, sid):
                nid = link["dst_id"]
                if (link["status"] != "active" or link["asserted_by"] != "llm"
                        or (node_ids is not None and nid not in node_ids)):
                    continue
                out = tag_links.keep(gconn, source_id=sid, node_id=nid, wiki_dir=root / "wiki",
                                     reviews_dir=root / "reviews")
                if out["outcome"] == "kept":
                    n += 1
                    affected.update(out["affected_sources"])
    finally:
        gconn.close()
    tag_links.finish(root, affected, wiki_dir=root / "wiki", templates_dir=TEMPLATES)
    return n


def close_journals(reviews_dir: Path, out: dict) -> dict:
    """Play the apply caller for an executor-level test (ADR-0066 review round 4): the in-flight
    markers an executor leaves are closed once the caller's refreshes succeeded."""
    for rid in out.get("journaled") or []:
        identity_journal.end(Path(reviews_dir), rid)
    return out
