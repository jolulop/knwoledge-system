#!/usr/bin/env python3
"""Who may write or delete an item page (ADR-0070 B1 review: fail safe on shared page names).

An item's page is `wiki/Items/<slug>.md`, and the slug is derived from the name, so two different
tags can derive the same one (`C++` and `C` → `c`). The page is the authority for the tag's title,
aliases, description and status (ADR-0030), so writing or deleting a page another item owns destroys
that item's authority. Until a permanent filename policy is chosen (a separate design), every writer
asks :func:`page_problem` **before changing any authority** and refuses when it returns a problem:

- the item's **own** page (frontmatter `item_id` equal to the item): writable;
- **absent**: writable only when no OTHER item node (any status: candidate, active, unused, hidden,
  merged, …) records the same slug;
- a page that belongs to **another** item, or whose owner **can't be established** (unreadable, no
  frontmatter, missing or malformed `item_id`): refused — fail closed.

Deleting is allowed only for the item's own page (or an absent one). :func:`write_page` and
:func:`remove_page` enforce the same rule at the point of IO, raising :class:`PageOwnershipError`, as a
backstop behind the pre-checks.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.backend import graph
from app.workers.wiki_render import NODE_DIR, parse_frontmatter

_ITEM_ID = re.compile(r"itm_[0-9a-f]{16}")

# The refusal reasons (typed outcomes shown on screens and in run summaries).
OWNED_BY_OTHER = "page_owned_by_other_item"
OWNER_UNKNOWN = "page_owner_unknown"
SLUG_TAKEN = "slug_taken"


class PageOwnershipError(RuntimeError):
    """A write or delete would touch a page this item does not own (see :func:`page_problem`)."""

    def __init__(self, problem: dict[str, Any]) -> None:
        super().__init__(f"{problem['reason']}: Items/{problem.get('slug')}.md "
                         f"(item {problem.get('node_id')}, owner {problem.get('owner')!r}, "
                         f"other items with this page name {problem.get('holders') or []})")
        self.problem = problem


def page_path(wiki_dir: Path, slug: str) -> Path:
    return Path(wiki_dir) / NODE_DIR["item"] / f"{slug}.md"


def page_state(wiki_dir: Path, slug: Any, node_id: str) -> tuple[str, str | None]:
    """`(state, owner)` of `Items/<slug>.md` relative to `node_id`: `absent`, `own`, `other` (owner =
    the other item's id) or `unknown` (unsafe slug, unreadable, no valid `item_id`)."""
    if not graph.is_safe_slug(slug):
        return "unknown", None
    path = page_path(wiki_dir, slug)
    if not path.exists():
        return "absent", None
    try:
        owner = parse_frontmatter(path.read_text(encoding="utf-8")).get("item_id")
    except (OSError, UnicodeDecodeError):
        return "unknown", None
    if not isinstance(owner, str) or not _ITEM_ID.fullmatch(owner):
        return "unknown", None
    return ("own" if owner == node_id else "other"), owner


def slug_holders(gconn, slug: Any, node_id: str) -> list[str]:
    """Other item nodes, in ANY status, that record this slug."""
    return [r["node_id"] for r in gconn.execute(
        "SELECT node_id FROM nodes WHERE node_type = 'item' AND slug = ? AND node_id != ? "
        "ORDER BY node_id", (slug, node_id))]


def page_problem(gconn, wiki_dir: Path, node_id: str, slug: Any) -> dict[str, Any] | None:
    """Why `node_id` may not write (create or rewrite) its page at `slug`, or None when it may."""
    state, owner = page_state(wiki_dir, slug, node_id)
    if state == "own":
        return None
    base = {"node_id": node_id, "slug": slug, "owner": owner}
    if state == "other":
        return {"reason": OWNED_BY_OTHER, **base}
    if state == "unknown":
        return {"reason": OWNER_UNKNOWN, **base}
    holders = slug_holders(gconn, slug, node_id)
    return {"reason": SLUG_TAKEN, **base, "holders": holders} if holders else None


def node_problem(gconn, wiki_dir: Path, node_id: str) -> dict[str, Any] | None:
    """:func:`page_problem` for an existing item at its recorded slug (None for a missing node)."""
    node = graph.get_node(gconn, node_id)
    if node is None or node.get("node_type") != "item":
        return None
    return page_problem(gconn, wiki_dir, node_id, node["slug"])


def first_problem(gconn, wiki_dir: Path, node_ids) -> dict[str, Any] | None:
    """The first problem among several items (multi-item operations check every page first)."""
    for nid in node_ids:
        if (problem := node_problem(gconn, wiki_dir, nid)) is not None:
            return problem
    return None


def delete_problem(wiki_dir: Path, node_id: str, slug: Any) -> dict[str, Any] | None:
    """Why `node_id` may not delete the page at `slug` (only its own, or an absent one, may go)."""
    state, owner = page_state(wiki_dir, slug, node_id)
    if state in ("own", "absent"):
        return None
    return {"reason": OWNED_BY_OTHER if state == "other" else OWNER_UNKNOWN,
            "node_id": node_id, "slug": slug, "owner": owner}


def write_page(gconn, wiki_dir: Path, node_id: str, slug: str, text: str) -> None:
    """Write the item's page, or raise :class:`PageOwnershipError` without writing."""
    if (problem := page_problem(gconn, wiki_dir, node_id, slug)) is not None:
        raise PageOwnershipError(problem)
    path = page_path(wiki_dir, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def remove_page(wiki_dir: Path, node_id: str, slug: str) -> None:
    """Delete the item's own page (absent is fine), or raise without deleting."""
    if (problem := delete_problem(wiki_dir, node_id, slug)) is not None:
        raise PageOwnershipError(problem)
    page_path(wiki_dir, slug).unlink(missing_ok=True)
