#!/usr/bin/env python3
"""Shared filesystem path-containment guard (ADR-0009/0037).

Derived local state — manifests, enrichment/claims artifacts, and graph node ids — is untrusted input
(AGENTS.md): a hand-edited path or a tampered id must never make a worker or validator read/write outside
its intended directory. `safe_under`/`safe_child` are the point-of-use containment checks used wherever a
path is built from such a value (they replaced the duplicated `_safe_raw_rel` / `_safe_under` /
`_safe_under_raw` helpers). A graph node's `slug` is additionally validated at its source — the graph
boundary — by `graph.is_safe_slug` (`upsert_node` + the `validate_graph` backstop), so no downstream
renderer builds an escaping page path from it.
"""
from __future__ import annotations

from pathlib import Path


def safe_under(root: Path, base: Path, rel: str) -> Path | None:
    """Resolve ``root / rel`` and return it only if it stays under ``base``; else ``None``.

    Rejects absolute paths and any ``..`` segment up front, then confirms the resolved path is contained
    in ``base`` (which is normally ``root`` or a subdirectory of it). For a single-component name built
    from an untrusted id, call ``safe_under(dir, dir, f"{id}.ext")``.
    """
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        return None
    resolved = (Path(root) / p).resolve()
    try:
        resolved.relative_to(Path(base).resolve())
    except ValueError:
        return None
    return resolved


def safe_child(base: Path, name: str) -> Path | None:
    """Return ``base / name`` only if ``name`` is a single safe **basename**, else ``None``.

    Stricter than ``safe_under`` (which allows nested segments): for a filename built directly from an
    untrusted id (`f"{id}.ext"`), the id must be a pure basename — reject any path separator, ``.``/``..``,
    or absolute path *before* constructing the path. So a tampered id can drive only an in-directory read,
    never a nested or escaping one (ADR-0009/0037).
    """
    if not name or name in (".", "..") or "/" in name or "\\" in name or Path(name).is_absolute():
        return None
    return (Path(base) / name).resolve()
