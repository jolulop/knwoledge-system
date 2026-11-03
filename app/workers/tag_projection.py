#!/usr/bin/env python3
"""Kept tags as Obsidian tags: the Source page `tags:` projection (ADR-0070 B1).

The Source page carries `tags:` as a **generated projection of the keep ledger**, never a place where
tags are decided: it lists the tags the document keeps (the same curated rows its "Items Mentioned"
section links), each as `<item_type>/<label>`, so Obsidian's Tags pane browses by type. Identity
stays the item id; the rendered string is only a browsing label.

- :func:`encode_label` is the one deterministic encoding of a canonical name (ADR-0070 "Encoding
  rule"): apostrophes deleted (before and after NFKC), NFKC, `&` → ` and `, every run of non-letter/digit characters → `_`,
  edges trimmed, case and accents kept; an empty result falls back to the item id itself.
- :func:`build_mapping` makes the labels of a set of tags unique by a fixed point: tags whose
  complete `<item_type>/<label>` are equal under ``str.casefold()`` (the chosen comparison policy)
  all move to the next suffix length, 0 → 6 → 8 → … → 16 hex of the item id, until no two are equal.
  Two full-length suffixes end in different ids, so it terminates; if it cannot (duplicate ids, a
  malformed id), :class:`TagMappingError` is raised and nothing ambiguous is published.
- :func:`active_mapping` is the vault's mapping: every **active** tag (only those render), with its
  page title and governed type. It is derived every time; nothing stores identity.
- The **last published mapping** (`db/tag_labels.json`, :func:`load_published` /
  :func:`save_published`) is a disposable snapshot publication compares against, so that a change to
  any tag other than the ones a write acted on refreshes every Source page (ADR-0070 "Publication").
  Deleting it only causes one full Source-page refresh.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

from app.workers.wiki_render import NODE_DIR, parse_frontmatter

SNAPSHOT_NAME = "tag_labels.json"
SNAPSHOT_SCHEMA = 1

# The encoding's own apostrophe list (ADR-0070 review round 1); merge_key is unchanged.
_APOSTROPHES = ("'", "’", "‘", "ʼ", "`", "´")
SUFFIX_LENGTHS = (0, 6, 8, 10, 12, 14, 16)
_ITEM_ID = re.compile(r"itm_([0-9a-f]{16})")
_RUNS = re.compile(r"_+")


class TagMappingError(RuntimeError):
    """The tag labels cannot be made unique (an invariant violation such as duplicate item ids)."""


def _strip_apostrophes(text: str) -> str:
    for mark in _APOSTROPHES:
        text = text.replace(mark, "")
    return text


def encode_label(name: Any, item_id: str) -> str:
    """The tag-safe label of a canonical name; the item id when nothing tag-safe is left."""
    text = _strip_apostrophes(name if isinstance(name, str) else "")
    # NFKC turns `´` into a space plus a combining accent (and fullwidth `＇` into `'`), so the
    # apostrophes are deleted before and after it.
    text = _strip_apostrophes(unicodedata.normalize("NFKC", text))
    text = text.replace("&", " and ")
    label = "".join(ch if ch.isalnum() else "_" for ch in text)
    label = _RUNS.sub("_", label).strip("_")
    return label or item_id


def _suffix(item_id: str, length: int) -> str:
    match = _ITEM_ID.fullmatch(item_id)
    if match is None:
        raise TagMappingError(f"malformed item id {item_id!r}")
    return f"_{match.group(1)[:length]}" if length else ""


def build_mapping(tags: Iterable[tuple[str, str, str]]) -> dict[str, str]:
    """`{item_id: "<item_type>/<label>"}` for `(item_id, item_type, name)` rows, collision-free under
    ``str.casefold()``. Depends only on the set of rows, never on their order."""
    base: dict[str, str] = {}
    for item_id, item_type, name in tags:
        if item_id in base:
            raise TagMappingError(f"duplicate item id {item_id}")
        _suffix(item_id, 0)                                   # validates the id shape
        base[item_id] = f"{item_type}/{encode_label(name, item_id)}"
    level = dict.fromkeys(base, 0)

    def final(item_id: str) -> str:
        return base[item_id] + _suffix(item_id, SUFFIX_LENGTHS[level[item_id]])

    while True:
        groups: dict[str, list[str]] = {}
        for item_id in base:
            groups.setdefault(final(item_id).casefold(), []).append(item_id)
        clashing = [ids for ids in groups.values() if len(ids) > 1]
        if not clashing:
            return {item_id: final(item_id) for item_id in sorted(base)}
        top = len(SUFFIX_LENGTHS) - 1
        for ids in clashing:
            # Saturated members (already the full id) stay as they are; the others extend. Only a
            # group in which no member can extend any further is unresolvable (duplicate ids).
            movable = [i for i in ids if level[i] < top]
            if not movable:
                raise TagMappingError(
                    f"tag labels cannot be made unique: {sorted(ids)} all read "
                    f"{final(ids[0])!r} with full-length suffixes")
            for item_id in movable:
                level[item_id] += 1


def _page_title(wiki_dir: Path, node_id: str, slug: Any) -> str:
    """The tag's canonical name from its own page, or TagMappingError. The page must exist, parse,
    belong to this item (`item_id`) and carry a non-empty title: a page another item owns (two names
    with one slug) or a missing title is never substituted (review round on B1)."""
    if not isinstance(slug, str) or not slug:
        raise TagMappingError(f"{node_id}: the tag has no page name (slug)")
    page = Path(wiki_dir) / NODE_DIR["item"] / f"{slug}.md"
    try:
        fm = parse_frontmatter(page.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise TagMappingError(f"{node_id}: its page Items/{slug}.md can't be read "
                              f"({type(exc).__name__})") from exc
    if fm.get("item_id") != node_id:
        raise TagMappingError(f"{node_id}: its page Items/{slug}.md belongs to "
                              f"{fm.get('item_id')!r}, not to this tag")
    title = fm.get("title")
    if not isinstance(title, str) or not title.strip():
        raise TagMappingError(f"{node_id}: its page Items/{slug}.md has no title")
    return title


def active_mapping(gconn, wiki_dir: Path) -> dict[str, str]:
    """The mapping of every active tag in the vault: title from the tag's own page (the authority),
    type from the graph mirror of the page. Any active tag without a valid page of its own raises
    TagMappingError, so publication stays pending rather than publish a borrowed or invented name."""
    rows = gconn.execute(
        "SELECT node_id, slug, item_type FROM nodes WHERE node_type = 'item' AND status = 'active' "
        "ORDER BY node_id").fetchall()
    tags = []
    for r in rows:
        if not r["item_type"]:
            raise TagMappingError(f"{r['node_id']}: the tag has no item_type")
        tags.append((r["node_id"], r["item_type"], _page_title(wiki_dir, r["node_id"], r["slug"])))
    return build_mapping(tags)


def source_tags(curated: Iterable[dict[str, Any]], mapping: dict[str, str]) -> list[str]:
    """A Source page's `tags:`: the active tags among its curated (kept) rows, sorted."""
    tags = {mapping[m["dst_id"]] for m in curated
            if m.get("node_type") == "item" and m.get("status") == "active"
            and m.get("dst_id") in mapping}
    return sorted(tags, key=lambda t: (t.casefold(), t))


# --- the last published mapping ---------------------------------------------------------------------


def snapshot_path(root: Path) -> Path:
    return Path(root) / "db" / SNAPSHOT_NAME


def load_published(root: Path) -> dict[str, str] | None:
    """The mapping the Source pages were last fully published with, or None when unknown (missing,
    unreadable or malformed: the caller then refreshes every Source page)."""
    try:
        data = json.loads(snapshot_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    labels = data.get("labels") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or data.get("schema_version") != SNAPSHOT_SCHEMA
            or not isinstance(labels, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items())):
        return None
    return labels


def save_published(root: Path, mapping: dict[str, str]) -> None:
    from app.backend.publication_queue import _write_durably
    path = snapshot_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"schema_version": SNAPSHOT_SCHEMA, "labels": dict(sorted(mapping.items()))}
    _write_durably(path, (json.dumps(data, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))


def changed_ids(published: dict[str, str] | None, current: dict[str, str]) -> set[str] | None:
    """The tags whose label differs between the two mappings (added and removed included); None when
    the published mapping is unknown."""
    if published is None:
        return None
    return {i for i in set(published) | set(current) if published.get(i) != current.get(i)}
