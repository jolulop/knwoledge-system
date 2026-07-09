#!/usr/bin/env python3
"""Validate that rendered wiki backlinks and the graph's active edges match (Phase 3.5b).

The Source/Claim/Item pages are a deterministic projection of the graph's `active`
edges (ADR-0029/0030/0059). This checks the projection is a *bidirectional* match — neither a
silent missing link nor an invented one:

- Source page: every active `derived_from` claim and `mentions` item is linked, and every
  `[[Claims/…]]` / `[[Items/…]]` link has a corresponding active edge.
- Claim page: its `[[Sources/…]]` links match its active `derived_from` edges.
- Item page: its `[[Sources/…]]` (Mentioned-by) links match its active incoming `mentions`
  edges, and its frontmatter `item_type` matches the graph nodes mirror (ADR-0059).

No graph database yet -> nothing to validate (a pass).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph
from app.workers.wiki_render import NODE_DIR, parse_frontmatter

_LINK = re.compile(r"\[\[([^\]]+)\]\]")
_DIR_TYPE = {NODE_DIR["item"]: "item"}
# Source frontmatter array <-> body section <-> link-target directory (advisory projection
# mirror; the id-keyed graph remains the relationship authority — ADR-0030). One flat Items
# section (ADR-0059); the renderer's item_type sub-headers live INSIDE the section.
_FM_SECTIONS = [
    ("items", "Items Mentioned", "Items"),
]


def _targets(text: str) -> set[str]:
    body = text.split("\n---\n", 1)[-1]
    return {m.group(1).split("|", 1)[0].split("#", 1)[0].strip() for m in _LINK.finditer(body)}


def _section_link_slugs(text: str, section: str, prefix: str) -> set[str]:
    """Slugs of `[[<prefix>/<slug>...]]` links within a `## <section>` body block."""
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == f"## {section}")
    except StopIteration:
        return set()
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    out: set[str] = set()
    for ln in lines[start + 1:end]:
        for m in _LINK.finditer(ln):
            target = m.group(1).split("|", 1)[0].split("#", 1)[0].strip()
            if target.startswith(prefix + "/"):
                out.add(target.split("/", 1)[1])
    return out


def _sources_links(targets: set[str]) -> set[str]:
    return {t.split("/", 1)[1] for t in targets if t.startswith("Sources/")}


def _fm_list(text: str, key: str) -> set[str]:
    """Parse a YAML block-list frontmatter field (`key:` then `  - item` lines) into a set."""
    fm = text.split("\n---\n", 1)[0]
    lines = fm.splitlines()
    out: set[str] = set()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == f"{key}:")
    except StopIteration:
        return out
    for ln in lines[start + 1:]:
        if ln.startswith((" ", "\t")) and ln.strip().startswith("- "):
            out.add(ln.strip()[2:].strip().strip('"\''))
        elif ln.strip() and not ln.startswith((" ", "\t")):
            break
    return out


def _check_status_mirror(errors: list[str], node_id: str, text: str, node_status: dict[str, str]) -> None:
    """Page frontmatter is the status authority; the graph nodes index must mirror it."""
    page_status = parse_frontmatter(text).get("status")
    graph_status = node_status.get(node_id)
    if page_status != graph_status:
        errors.append(f"{node_id}: page status {page_status!r} != graph node status {graph_status!r}")


def _check(root: Path, db_path: Path) -> list[str]:
    errors: list[str] = []
    conn = graph.connect(db_path)
    try:
        # Schema gate (ADR-0059): a pre-v2 database is stale by design after the clean restart.
        found = graph.schema_version(conn)
        if found != graph.SCHEMA_VERSION or not graph._nodes_table_has_item_type(conn):
            # The structural check is independent of the stamp (review round: B1) — a
            # version that LIES about a pre-v2 table still refuses cleanly, never crashes.
            return [f"graph schema version mismatch: found v{found}, expected "
                    f"v{graph.SCHEMA_VERSION} (pre-ADR-0059 database; the clean-repository "
                    f"restart rebuilds it)"]
        slug_to_node = {
            (r["node_type"], r["slug"]): r["node_id"]
            for r in conn.execute("SELECT node_id, node_type, slug FROM nodes")
        }
        node_status = {r["node_id"]: r["status"]
                       for r in conn.execute("SELECT node_id, status FROM nodes")}
        node_type_of = {r["node_id"]: r["node_type"]
                        for r in conn.execute("SELECT node_id, node_type FROM nodes")}
        node_item_type = {r["node_id"]: r["item_type"]
                          for r in conn.execute("SELECT node_id, item_type FROM nodes")}

        # --- Source pages: claims + mentions both directions ---
        sources_dir = root / "wiki" / "Sources"
        for page in sorted(sources_dir.glob("*.md")) if sources_dir.exists() else []:
            sid = page.stem
            text = page.read_text(encoding="utf-8", errors="replace")
            targets = _targets(text)
            # Frontmatter arrays must mirror their body section exactly (absent == empty set).
            fm = parse_frontmatter(text)
            for key, section, prefix in _FM_SECTIONS:
                fm_slugs = set(fm.get(key) or [])
                body_slugs = _section_link_slugs(text, section, prefix)
                if fm_slugs != body_slugs:
                    errors.append(f"{sid}: frontmatter {key}={sorted(fm_slugs)} != body "
                                  f"{section} links {sorted(body_slugs)}")
            # ADR-0048: the rendered Source-page Claims section omits hidden claims (wiki.py); the
            # derived_from edge stays active, so the expected projection is active NON-hidden claims.
            active_claims = {cid for cid in graph.claims_for_source(conn, sid)
                             if node_status.get(cid) != "hidden"}
            active_mentions = {(m["node_type"], m["slug"]) for m in graph.mentions_for_source(conn, sid)}

            for cid in active_claims:
                if f"Claims/{cid}" not in targets:
                    errors.append(f"{sid}: active claim {cid} not projected on Source page")
            for nt, slug in active_mentions:
                if f"{NODE_DIR[nt]}/{slug}" not in targets:
                    errors.append(f"{sid}: active mention {NODE_DIR[nt]}/{slug} not projected")
            for t in targets:
                head, _, rest = t.partition("/")
                if head == "Claims" and rest not in active_claims:
                    errors.append(f"{sid}: projected claim link [[{t}]] has no active edge")
                elif head in _DIR_TYPE and (_DIR_TYPE[head], rest) not in active_mentions:
                    errors.append(f"{sid}: projected mention link [[{t}]] has no active edge")

        # --- Claim pages: Sources links match active derived_from ---
        claims_dir = root / "wiki" / "Claims"
        for page in sorted(claims_dir.glob("*.md")) if claims_dir.exists() else []:
            cid = page.stem
            text = page.read_text(encoding="utf-8", errors="replace")
            _check_status_mirror(errors, cid, text, node_status)
            linked = _sources_links(_targets(text))
            active = {e["dst_id"] for e in graph.outgoing_active(conn, cid) if e["edge_type"] == "derived_from"}
            for sid in active - linked:
                errors.append(f"{cid}: active derived_from source {sid} not linked on Claim page")
            for sid in linked - active:
                errors.append(f"{cid}: linked source {sid} has no active derived_from edge")

            # Contradicting-claims projection matches active `contradicts` edges (ADR-0031).
            linked_contra = _section_link_slugs(text, "Contradicting Claims", "Claims")
            # ADR-0048: the rendered Contradicting Claims section omits hidden partner claims (claims.py);
            # the contradicts edge stays active, so the expected projection is active NON-hidden partners.
            active_contra = {o for o in graph.active_contradictions_for_claim(conn, cid)
                             if node_status.get(o) != "hidden"}
            for other in active_contra - linked_contra:
                errors.append(f"{cid}: active contradiction with {other} not projected on Claim page")
            for other in linked_contra - active_contra:
                errors.append(f"{cid}: projected contradiction link [[Claims/{other}]] has no active edge")

        # --- Synthesis pages: Supporting-Evidence links AND the derived_from frontmatter list
        # both match active derived_from edges (the frontmatter is the machine-readable record) ---
        synthesis_dir = root / "wiki" / "Synthesis"
        for page in sorted(synthesis_dir.glob("*.md")) if synthesis_dir.exists() else []:
            text = page.read_text(encoding="utf-8", errors="replace")
            syn_id = slug_to_node.get(("synthesis", page.stem))
            if syn_id is None:
                errors.append(f"Synthesis/{page.stem}: page has no matching graph node")
                continue
            _check_status_mirror(errors, syn_id, text, node_status)
            linked = {t.split("/", 1)[1] for t in _targets(text) if t.startswith("Claims/")}
            fm_list = _fm_list(text, "derived_from")
            # ADR-0049: a hidden claim is suppressed from the rendered Supporting Evidence + derived_from
            # frontmatter (the synthesis page is a default-discovery surface). The edge stays active in the
            # graph, so the expected projection is active derived_from edges to NON-hidden claim nodes.
            active = {e["dst_id"] for e in graph.outgoing_active(conn, syn_id)
                      if e["edge_type"] == "derived_from" and node_status.get(e["dst_id"]) != "hidden"}
            for cid in active - linked:
                errors.append(f"{syn_id}: active derived_from claim {cid} not linked on Synthesis page")
            for cid in linked - active:
                errors.append(f"{syn_id}: linked claim {cid} has no active derived_from edge")
            if fm_list != active:
                errors.append(f"{syn_id}: derived_from frontmatter {sorted(fm_list)} != active "
                              f"derived_from edges {sorted(active)}")

        # --- Item pages: Mentioned-by links match active mentions ---
        for node_type, subdir in ((t, NODE_DIR[t]) for t in _DIR_TYPE.values()):
            folder = root / "wiki" / subdir
            for page in sorted(folder.glob("*.md")) if folder.exists() else []:
                node_id = slug_to_node.get((node_type, page.stem))
                if node_id is None:
                    errors.append(f"{subdir}/{page.stem}: page has no matching graph node")
                    continue
                text = page.read_text(encoding="utf-8", errors="replace")
                _check_status_mirror(errors, node_id, text, node_status)
                # ADR-0050: a `merged` tombstone must point merged_into at an ACTIVE, SAME-TYPE, non-self
                # survivor; it has no Mentioned-by/Duplicates projection to check.
                fm = parse_frontmatter(text)
                if fm.get("status") == "merged":
                    surv = fm.get("merged_into")
                    if not surv:
                        errors.append(f"{node_id}: merged page is missing merged_into")
                    elif surv == node_id:
                        errors.append(f"{node_id}: merged page points merged_into at itself")
                    elif surv not in node_status:
                        errors.append(f"{node_id}: merged_into {surv} is not an indexed node")
                    elif node_status.get(surv) != "active":
                        errors.append(f"{node_id}: merged_into survivor {surv} is not active "
                                      f"(status {node_status.get(surv)!r})")
                    elif node_type_of.get(surv) != node_type:
                        errors.append(f"{node_id}: merged_into survivor {surv} is a different node_type "
                                      f"({node_type_of.get(surv)} != {node_type})")
                    continue
                # ADR-0059: the governed classification must round-trip page <-> graph mirror.
                page_item_type = fm.get("item_type")
                if page_item_type != node_item_type.get(node_id):
                    errors.append(f"{node_id}: page item_type {page_item_type!r} != graph "
                                  f"item_type {node_item_type.get(node_id)!r}")
                linked = _sources_links(_targets(text))
                active = set(graph.sources_for_node(conn, node_id))
                for sid in active - linked:
                    errors.append(f"{node_id}: active mention from {sid} not linked on page")
                for sid in linked - active:
                    errors.append(f"{node_id}: linked source {sid} has no active mention edge")

                # Duplicates projection (ADR-0041): the body `## Duplicates` links match active
                # `duplicates` edges both directions. `duplicates` is SAME_TYPE, so partners share
                # this page's subdir. (No frontmatter key — body-only, one surface to validate.)
                linked_dup = _section_link_slugs(text, "Duplicates", subdir)
                active_dup = {p["slug"] for p in graph.active_duplicates(conn, node_id)}
                for slug in active_dup - linked_dup:
                    errors.append(f"{node_id}: active duplicate {slug} not projected on page")
                for slug in linked_dup - active_dup:
                    errors.append(f"{node_id}: projected duplicate [[{subdir}/{slug}]] has no active edge")
    finally:
        conn.close()
    return errors


def main(argv: list[str]) -> int:
    root = Path(argv[0]).resolve() if argv else Path.cwd()
    db_path = root / "db" / "graph.sqlite"
    if not db_path.exists():
        print("Projection validation passed (no graph database yet).")
        return 0
    errors = _check(root, db_path)
    if errors:
        print("Projection validation failed:")
        for err in errors:
            print(f"- {err}")
        return 1
    print("Projection validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
