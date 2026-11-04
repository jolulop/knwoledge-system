#!/usr/bin/env python3
"""Duplicate-name checks on a NEW item name (ADR-0071 decision 3).

Three paths give an item a name it did not have: a keep with a title amendment (promote), a split
spin-off name, and Add tag (human-add). Ids are frozen at creation, so a renamed item's title no
longer hashes to its id and a later rename or add of that title would mint a second item with the
same name. The filename guard can't see that (different ids, different suffixes), so these checks
run separately, under the caller's write/ledger lock, before any change:

- :func:`exact_conflict` (level 1, **blocks**): another item whose **title** is equal under the id
  normalization (`items._normalize_name`, called directly). The item being renamed is excluded, a
  merged tombstone resolves to its survivor first (no conflict when the survivor is the item
  itself), `deleted` items don't count, every other status does. A title whose authority can't be
  read (no page, no title, a page that belongs to another item) is an explicit refusal — never a
  silent pass.
- :func:`similar_tags` (level 2, **advisory**): active and unused tags whose title or an alias has
  the same `merge_key` (ADR-0064 fold). Never blocks: `C++` and `C` share a key and are different.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.backend import graph
from app.workers import item_pages, items, merge_candidates
from app.workers.wiki_render import parse_frontmatter

NAME_TAKEN = "name_taken"
SIMILAR_TAGS = "similar_tags"
TITLE_UNREADABLE = "title_authority_unreadable"
_ADVISORY_STATUSES = frozenset({"active", "unused"})
_MAX_MERGE_HOPS = 8


def _item_rows(gconn) -> dict[str, dict[str, Any]]:
    return {r["node_id"]: dict(r) for r in gconn.execute(
        "SELECT node_id, slug, status, item_type FROM nodes WHERE node_type = 'item' "
        "ORDER BY node_id")}


def _page_meta(wiki_dir: Path, node: dict[str, Any]) -> dict[str, Any] | None:
    """The item's title authority from its own page, or None when it can't be read: an unsafe slug,
    no page, unreadable bytes, no title line, or a page whose `item_id` is another item's."""
    slug = node.get("slug")
    if not graph.is_safe_slug(slug):
        return None
    try:
        text = item_pages.page_path(wiki_dir, slug).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = items._TITLE_RE.search(text)
    fm = parse_frontmatter(text)
    if match is None or fm.get("item_id") != node["node_id"]:
        return None
    aliases = fm.get("aliases")
    return {"title": re.sub(r"\\(.)", r"\1", match.group(1)),
            "aliases": [a for a in aliases if isinstance(a, str)] if isinstance(aliases, list) else [],
            "merged_into": fm.get("merged_into")}


def title_of(wiki_dir: Path, node: dict[str, Any]) -> str | None:
    """The item's title from its own page, or None when it can't be read."""
    meta = _page_meta(Path(wiki_dir), node)
    return meta["title"] if meta else None


def _unreadable(node: dict[str, Any]) -> dict[str, Any]:
    return {"reason": TITLE_UNREADABLE, "node_id": node["node_id"], "slug": node.get("slug"),
            "status": node.get("status")}


def _survivor(rows: dict[str, dict[str, Any]], wiki_dir: Path, start: dict[str, Any],
              meta: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Follow a merged tombstone's `merged_into` to its live survivor: `(survivor row, its meta)`,
    `(None, None)` when the chain ends at a deleted or absent item, or `(row, None)` when a link in
    the chain can't be read (the caller refuses)."""
    node, seen = start, {start["node_id"]}
    for _ in range(_MAX_MERGE_HOPS):
        target = meta.get("merged_into")
        nxt = rows.get(target) if isinstance(target, str) else None
        if nxt is None or nxt["status"] == "deleted":
            return None, None
        if nxt["node_id"] in seen:
            return nxt, None
        seen.add(nxt["node_id"])
        meta = _page_meta(wiki_dir, nxt)
        if meta is None:
            return nxt, None
        node = nxt
        if node["status"] != "merged":
            return node, meta
    return node, None


def exact_conflict(gconn, wiki_dir: Path, name: str, node_id: str | None) -> dict[str, Any] | None:
    """Level 1: the refusal when another live item already has `name` as its title (see module doc),
    else None. `node_id` is the item being named (excluded; None for a name no item holds yet)."""
    wiki_dir = Path(wiki_dir)
    key = items._normalize_name(name)
    rows = _item_rows(gconn)
    for nid, node in rows.items():
        if nid == node_id or node["status"] == "deleted":
            continue
        meta = _page_meta(wiki_dir, node)
        if meta is None:
            return _unreadable(node)
        if items._normalize_name(meta["title"]) != key:
            continue
        live, live_meta, via = node, meta, None
        if node["status"] == "merged":
            live, live_meta = _survivor(rows, wiki_dir, node, meta)
            via = nid
            if live is None:
                continue                                  # the tombstone's chain ends at no live item
            if live_meta is None:
                return _unreadable(live)
            if live["node_id"] == node_id:
                continue                                  # renaming the survivor to its absorbed name
        return {"reason": NAME_TAKEN, "name": name, "conflict_node_id": live["node_id"],
                "conflict_title": live_meta["title"], "conflict_status": live["status"],
                "conflict_item_type": live.get("item_type"), "via_tombstone": via}
    return None


def similar_tags(gconn, wiki_dir: Path, name: str, node_id: str | None) -> list[dict[str, Any]]:
    """Level 2: active/unused tags (other than `node_id`) whose title or an alias has `name`'s
    `merge_key`, each with the title or alias that matched. Unreadable pages are skipped here (level
    1 refuses them)."""
    wiki_dir = Path(wiki_dir)
    key = merge_candidates.merge_key(name)
    if not key:
        return []
    out = []
    for nid, node in _item_rows(gconn).items():
        if nid == node_id or node["status"] not in _ADVISORY_STATUSES:
            continue
        meta = _page_meta(wiki_dir, node)
        if meta is None:
            continue
        matched = next((n for n in [meta["title"], *meta["aliases"]]
                        if merge_candidates.merge_key(n) == key), None)
        if matched is not None:
            out.append({"node_id": nid, "title": meta["title"], "matched": matched,
                        "item_type": node.get("item_type"), "status": node["status"]})
    return out


def similar_advisory(gconn, wiki_dir: Path, name: str, node_id: str | None) -> dict[str, Any] | None:
    """Level 2 for the paths that offer no choice (a title amendment, a split spin-off; review round
    1): `{kind: "similar_tags", name, node_id, matches}` reported alongside the outcome, or None.
    Advisory only — it is computed after every gate has passed and a failure to compute it is no
    advisory, so it can never change whether the action runs."""
    try:
        matches = similar_tags(gconn, wiki_dir, name, node_id)
    except Exception:  # noqa: BLE001 - an advisory never blocks or fails the action
        return None
    if not matches:
        return None
    return {"kind": SIMILAR_TAGS, "name": name, "node_id": node_id, "matches": matches}
