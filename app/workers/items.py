#!/usr/bin/env python3
"""Tier-2 knowledge-item extraction worker (ADR-0059; mechanics from ADR-0017/0018/0021/0030).

Tier-2 LLM pass: for each extracted/partial source, identify the knowledge **items** it is
about — one structural node family (`node_type: item`, type-neutral `itm_` id) classified by
the governed 15-type `item_type` taxonomy (`app/backend/taxonomy.py`) plus the QA-only
`unclassified_review_required` sentinel. Each becomes a node created `candidate` (ADR-0018),
with an `active` `mentions` edge (source → node) recording provenance. Items are interpretive
labels, **not verbatim-grounded** like claims (an optional evidence anchor is stored only
when the name is mechanically locatable).

ADR-0066: an extracted item is a **suggestion** — a tag link the user keeps or drops on the
document's review screen (`tag_links`); nothing here files a promotion. Names on the document's
drop records or on the blocked-tag list (`policies/blocked_tags.txt`) are filtered BEFORE any
write, so a dropped/blocked suggestion never comes back by re-extraction. Kept links are
`asserted_by: human` edges, which re-extraction (superseding only `llm` mentions) never touches.

Pages are deterministic stubs rendered from the graph (Mentioned-by = active incoming
mentions) in ONE flat `wiki/Items/` directory; the page frontmatter is the durable authority
for the node's title/aliases/item_type. A later extraction that classifies an existing name
under a different type routes its mention to the existing node and files a
`change_item_type` review — nothing auto-retypes (ADR-0059 decision 3). On re-extraction a
source's prior mentions are superseded **only once the run can produce the replacement**
(after a successful parse, or deterministically for an empty source — the ADR-0055
rollout-safety ordering), and affected node pages are recomposed from surviving `active`
mentions (tombstoned if none).
Each source commits under the write lock as one unit (ADR-0068 decision 5): the graph, its tag
pages, then its artifact last, then its Source page is published; the run ends with one refresh of
the other Source pages that list the tags it touched, `index.md` and the keyword index. Same
operational shape as the claim worker.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

from app.backend import db, graph, taxonomy
from app.backend import producer_journal as journal
from app.backend.manifests import get_status, iso_now, load_manifest, valid_manifests
from app.llm import prompts
from app.llm.client import CacheMiss, LLMClient, ParseError
from app.workers import citations, publication, reconcile, reviews, titles
from app.workers import enrichment_artifact as art
from app.workers import labels
from app.workers.wiki_render import NODE_DIR, parse_frontmatter, render_item_page

_ENRICHABLE_STATUSES = {"extracted", "partial"}
_PRODUCER = "extract_items"                  # its run guard and journal name
_WS = re.compile(r"\s+")
_TITLE_RE = re.compile(r'(?m)^title:\s*"(.*)"\s*$')

# Defensive bounds against adversarial/oversized LLM output (a source is untrusted data).
_MAX_NAME, _MAX_ALIAS, _MAX_ALIASES, _MAX_ITEMS = 200, 120, 16, 200

# ADR-0066 P1: the default per-document suggestion cap (the `ENRICH_ITEMS_CAP` setting).
DEFAULT_SUGGESTION_CAP = 15
_DOMAINS_FIRST = 3                                        # P2: domain suggestions moved to the front
_RESOLVABLE = frozenset({"active", "unused"})             # P3: tags a suggestion can resolve to
# Part-2 review: statuses a human decided. Extraction never changes them — a suggestion naming such
# a tag is filtered (`hidden` / `retired`) or follows a merge to its survivor. A `deprecated_candidate`
# counts only when the deprecation was approved (the automatic no-mentions tombstone may revive).
_RETIRED = frozenset({"archive_candidate", "archived", "delete_candidate", "deleted"})
_MAX_MERGE_HOPS = 8

# ADR-0066 P4: the reference cut. The extracted text has no reliable headings, so the cut is the
# LAST capitalised standalone marker in the second half of the text that a year or a bracketed
# number follows within ~200 characters (skips e.g. JSTOR's lower-case "This article references").
_REFERENCE_MARKER = re.compile(
    r"(?<![\w-])(?:References|REFERENCES|Bibliography|BIBLIOGRAPHY|Works Cited|WORKS CITED"
    r"|Literature Cited|LITERATURE CITED)(?![\w-])")
_REFERENCE_ENTRY = re.compile(r"\b(?:19|20)\d\d\b|\[\d{1,4}\]")
_REFERENCE_LOOKAHEAD = 200


def _normalize_name(name: str) -> str:
    return _WS.sub(" ", name).strip().lower()


def _name_hash(name: str) -> str:
    return hashlib.sha256(_normalize_name(name).encode("utf-8")).hexdigest()[:16]


def node_id(name: str) -> str:
    """The type-neutral item id (ADR-0059): classification is metadata, never identity.

    Minted from the creation-time normalized canonical name and frozen thereafter (renames
    never rehash, ADR-0021); same-name-different-referent is resolved by `split_item`, never
    by type."""
    return f"itm_{_name_hash(name)}"


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", _normalize_name(name)).strip("-")
    return s or "unnamed"


def _clean_aliases(aliases: Any) -> list[str]:
    out: list[str] = []
    if isinstance(aliases, list):
        for a in aliases:
            if isinstance(a, str):
                cleaned = _WS.sub(" ", a).strip()[:_MAX_ALIAS]
                if cleaned and cleaned not in out:
                    out.append(cleaned)
    return out[:_MAX_ALIASES]


def reference_cut(text: str) -> int | None:
    """The character offset where the document's reference list starts (ADR-0066 P4), or None —
    then the whole text counts as body."""
    cut = None
    for m in _REFERENCE_MARKER.finditer(text, len(text) // 2):
        if _REFERENCE_ENTRY.search(text, m.end(), m.end() + _REFERENCE_LOOKAHEAD):
            cut = m.start()
    return cut


def _body_aliases(aliases: list[str], folded_body: str) -> tuple[list[str], list[str]]:
    """ADR-0066 decision 8(b): an alias is kept only if it occurs in the non-reference text; the
    rest are returned separately for the artifact (`aliases_filtered`)."""
    from app.workers import merge_candidates             # lazy: merge_candidates imports this module
    kept = [a for a in aliases if merge_candidates.occurs_in(a, folded_body)]
    return kept, [a for a in aliases if a not in kept]


def _page_fm(wiki_dir: Path, node: dict[str, Any]) -> dict[str, Any]:
    if not graph.is_safe_slug(node.get("slug")):
        return {}
    path = Path(wiki_dir) / NODE_DIR["item"] / f"{node['slug']}.md"
    try:
        return parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}


def decided_status(wiki_dir: Path, node: dict[str, Any]) -> str | None:
    """`hidden` / `merged` / `retired` when a human decided this tag's lifecycle (hide, merge,
    approved deprecation, archive/delete), else None. The page's review_status tells an approved
    deprecation from the automatic no-mentions tombstone."""
    status = node.get("status")
    if status == "hidden":
        return "hidden"
    if status == "merged":
        return "merged"
    if status in _RETIRED:
        return "retired"
    if status == "deprecated_candidate" and _page_fm(wiki_dir, node).get("review_status") == "approved":
        return "retired"
    return None


def _decided_effect(gconn, wiki_dir: Path, node: dict[str, Any]) -> tuple[str, str | None] | None:
    """What a suggestion naming a decided tag becomes: `("redirected", survivor)` for a merged tag
    whose (final) survivor is a live tag, else `("hidden", None)` / `("retired", None)`; None when the
    tag is not decided. The merge redirect follows `merged_into`, never the survivor's aliases."""
    decision = decided_status(wiki_dir, node)
    if decision is None:
        return None
    for _ in range(_MAX_MERGE_HOPS):
        if decision != "merged":
            return (decision, None)
        target = _page_fm(wiki_dir, node).get("merged_into")
        node = graph.get_node(gconn, target) if isinstance(target, str) else None
        if node is None:
            return ("retired", None)
        decision = decided_status(wiki_dir, node)
        if decision is None:
            return ("redirected", node["node_id"]) if node["status"] in _RESOLVABLE else ("retired", None)
    return ("retired", None)                              # a merge chain too long (or a cycle)


def _resolution_index(gconn, wiki_dir: Path) -> dict[str, Any]:
    """The tag vocabulary suggestions are matched against, read from the item pages (the title/alias
    authority):
    - `keys` — `{merge_key: [node_id…]}` over the titles and aliases of ACTIVE and UNUSED tags (never
      candidates — one unreviewed suggestion must not attract others), and `titles` — `{node_id: title}`
      (ADR-0066 P3);
    - `decided` — `{node_id: (effect, target)}` for every tag a human hid, merged or retired, and
      `decided_keys` — `{merge_key: [node_id…]}` over their titles and aliases (part-2 review)."""
    from app.workers import merge_candidates
    index: dict[str, list[str]] = {}
    titles: dict[str, str] = {}
    decided: dict[str, tuple[str, str | None]] = {}
    decided_keys: dict[str, list[str]] = {}
    for node in graph.nodes_of_type(gconn, "item"):
        if node["status"] in _RESOLVABLE:
            target = index
        elif (effect := _decided_effect(gconn, wiki_dir, node)) is not None:
            decided[node["node_id"]] = effect
            target = decided_keys
        else:
            continue
        rec = merge_candidates._item_record(wiki_dir, node)
        if rec is None:
            continue
        if target is index:
            titles[rec["node_id"]] = rec["title"]
        for key in rec["keys"]:
            target.setdefault(key, []).append(rec["node_id"])
    return {"keys": {k: sorted(set(v)) for k, v in index.items()}, "titles": titles,
            "decided": decided, "decided_keys": {k: sorted(set(v)) for k, v in decided_keys.items()}}


def _rejected_merge_pairs(reviews_dir: Path) -> set[frozenset[str]]:
    """Pairs the user decided are distinct (a rejected `merge_items`, ADR-0064): never auto-resolved
    to each other (ADR-0066 decision 8)."""
    from app.workers import merge_candidates
    out: set[frozenset[str]] = set()
    for state, item in merge_candidates._merge_ledger(reviews_dir):
        pair = merge_candidates._pair(item) if state == "rejected" else None
        if pair is not None:
            out.add(frozenset(pair))
    return out


def _combined_effect(effects: list[tuple[str, Any]]) -> tuple[str, Any] | None:
    """One outcome for a name several decided tags answer to: hidden wins (the user hid that name),
    then a single merge survivor, several survivors are ambiguous, else retired."""
    if not effects:
        return None
    if any(kind == "hidden" for kind, _ in effects):
        return ("hidden", None)
    targets = sorted({t for kind, t in effects if kind == "redirected"})
    if len(targets) == 1:
        return ("redirected", targets[0])
    return ("ambiguous", targets) if targets else ("retired", None)


def plan_suggestions(raw_items: list[Any], *, md: str, gconn, vocab: dict[str, Any],
                     rejected_pairs: set[frozenset[str]], dropped: tuple[set[str], set[str]],
                     blocked_keys: set[str], kept_ids: set[str], cap: int) -> dict[str, Any]:
    """The ADR-0066 P6 suggestion pipeline over the model's ranked list, before any write:

    1. clean each name; keep only aliases that occur in the body text (P4);
    2. resolve: the exact name wins when it is an active/unused tag; otherwise one `merge_key` match
       among active/unused tags resolves to it, several (or a rejected merge pair) are ambiguous
       and stay the suggestion's own candidate with `ambiguous_with` (P3). A suggestion naming a tag a
       human hid or retired is filtered (`hidden` / `retired`); one naming a merged tag follows the
       merge to its survivor (`redirected`) — the suggestion's own id first, then, when no live tag
       answers to the name, the titles and aliases of decided tags (part-2 review);
    3. filter drops and blocks against the suggested name AND the tag it resolved to;
    4. move the first three domain suggestions to the front (P2);
    5. cut at `cap`, counting the tags this document already keeps (they are never cut).

    Returns `{emit, filtered, reference_cut_at, counts}`; `emit` entries carry `node_id` (the target),
    `name`, `item_type` (as suggested), `aliases`, `aliases_filtered`, `resolution` and, when
    ambiguous, `ambiguous_with`."""
    from app.workers import merge_candidates, tag_links
    cut = reference_cut(md)
    folded_body = merge_candidates.fold_text(md if cut is None else md[:cut])
    dropped_keys, dropped_ids = dropped
    counts = {"dropped": 0, "blocked": 0, "hidden": 0, "retired": 0, "over_cap": 0, "resolved": 0,
              "redirected": 0, "ambiguous": 0, "aliases_filtered": 0, "domains": 0}
    filtered: list[dict[str, Any]] = []
    tag_index, tag_titles, decided = vocab["keys"], vocab["titles"], vocab["decided"]

    def _filter(entry: dict[str, Any], reason: str) -> None:
        counts[reason] += 1
        if not any(f["key"] == entry["key"] for f in filtered):
            row = {"name": entry["name"], "key": entry["key"], "reason": reason}
            if entry["resolution"] in ("resolved", "redirected"):
                row["resolved_to"] = entry["node_id"]
                row["resolved_title"] = tag_titles.get(entry["node_id"])
            filtered.append(row)

    ranked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for it in raw_items[:_MAX_ITEMS]:
        if not isinstance(it, dict):
            continue
        name = _WS.sub(" ", str(it.get("name") or "")).strip()[:_MAX_NAME]
        if not name:
            continue
        item_type = it.get("item_type", "")
        if not taxonomy.is_item_type(item_type):
            item_type = taxonomy.UNCLASSIFIED
        counts["domains"] += item_type == "domain"
        aliases, removed = _body_aliases(_clean_aliases(it.get("aliases", [])), folded_body)
        counts["aliases_filtered"] += len(removed)
        own, key = node_id(name), tag_links.tag_key(name)
        entry: dict[str, Any] = {"name": name, "key": key, "item_type": item_type,
                                 "aliases": aliases, "aliases_filtered": removed,
                                 "node_id": own, "resolution": "new"}
        own_node = graph.get_node(gconn, own)
        effect = decided.get(own) if own_node is not None else None
        if own_node is not None and own_node.get("status") in _RESOLVABLE:
            entry["resolution"] = "exact"
        elif effect is None:
            if own_node is not None:
                entry["resolution"] = "existing"
            matches = [m for m in tag_index.get(key, []) if m != own]
            if len(matches) == 1 and frozenset((own, matches[0])) not in rejected_pairs:
                entry.update(node_id=matches[0], resolution="resolved")
            elif matches:
                entry["ambiguous_with"] = matches
            else:
                effect = _combined_effect(
                    [decided[n] for n in vocab["decided_keys"].get(key, []) if n != own and n in decided])
        if effect is not None:
            if effect[0] == "redirected":
                entry.update(node_id=effect[1], resolution="redirected")
            elif effect[0] == "ambiguous":
                entry["ambiguous_with"] = effect[1]
            else:
                _filter(entry, effect[0])
                continue
        target = entry["node_id"]
        target_key = tag_links.tag_key(tag_titles.get(target)) if target != own else ""
        if (key in dropped_keys or own in dropped_ids or target in dropped_ids
                or (target_key and target_key in dropped_keys)):
            _filter(entry, "dropped")
            continue
        if key in blocked_keys or (target_key and target_key in blocked_keys):
            _filter(entry, "blocked")
            continue
        if target in seen:
            continue
        seen.add(target)
        ranked.append(entry)

    domains = [e for e in ranked if e["item_type"] == "domain"][:_DOMAINS_FIRST]
    ranked = domains + [e for e in ranked if all(e is not d for d in domains)]
    emit: list[dict[str, Any]] = []
    slots = max(0, cap - len(kept_ids))
    for entry in ranked:
        if entry["node_id"] in kept_ids:                  # already kept here: counted, never cut
            emit.append(entry)
        elif slots > 0:
            slots -= 1
            emit.append(entry)
        else:
            _filter(entry, "over_cap")
            continue
        counts["resolved"] += entry["resolution"] == "resolved"
        counts["redirected"] += entry["resolution"] == "redirected"
        counts["ambiguous"] += bool(entry.get("ambiguous_with"))
    return {"emit": emit, "filtered": filtered, "reference_cut_at": cut, "counts": counts}


def _accumulate(existing: list[str], new: list[str]) -> list[str]:
    """Extraction's automatic alias accumulation onto a never-kept candidate (ADR-0068 slice 3):
    the stored list exactly as `_union` keeps it, then only the genuinely new aliases, compared with
    `casefold()` (so `large language model` and `Large Language Model` take one slot, with the
    spelling already stored), up to the same limit. Human alias edits and their stale-form check
    keep `_union`'s exact comparison; a kept tag gets no hint aliases at all."""
    out = _union(existing, [])
    seen = {a.casefold() for a in out}
    for x in new:
        if x and x.casefold() not in seen:
            seen.add(x.casefold())
            out.append(x)
    return out[:_MAX_ALIASES]


def _union(existing: list[str], new: list[str]) -> list[str]:
    """Additive alias union (never auto-removes); identity-changing merges are review-gated."""
    out: list[str] = []
    for x in list(existing) + list(new):
        if x and x not in out:
            out.append(x)
    return out[:_MAX_ALIASES]


def _read_node_meta(page_path: Path) -> dict[str, Any] | None:
    """Read the durable title + aliases + item_type from an existing node page (page is the authority)."""
    if not page_path.exists():
        return None
    text = page_path.read_text(encoding="utf-8", errors="replace")
    m = _TITLE_RE.search(text)
    if not m:
        return None
    fm = parse_frontmatter(text)
    aliases = fm.get("aliases")
    return {"title": re.sub(r"\\(.)", r"\1", m.group(1)),
            "aliases": aliases if isinstance(aliases, list) else [],
            "status": fm.get("status"),
            "review_status": fm.get("review_status"),
            "confidence": fm.get("confidence", "low"),
            # ADR-0059: the page owns the governed classification; recompose preserves it.
            "item_type": fm.get("item_type"),
            # ADR-0052: preserve a spin-off's split lineage across re-renders (page-authoritative).
            "split_from": fm.get("split_from"),
            "split_review_id": fm.get("split_review_id"),
            # ADR-0058: preserve the page-owned human description across re-renders.
            "description": fm.get("description")}


def _link_labels(wiki_dir: Path, source_ids, duplicates) -> dict[str, str]:
    """ADR-0060: page-local display labels for an item page's Mentioned-By and Duplicates
    links (worker-side IO; every item-page writer routes through this so the renderer stays pure)."""
    targets = [f"Sources/{s}" for s in source_ids or []]
    targets += [NODE_DIR[d["node_type"]] + "/" + d["slug"] for d in duplicates or []]
    return labels.display_labels(wiki_dir, targets)


def _recompose_node(gconn, *, node_id, wiki_dir, reviews_dir, now, text_hint=None) -> str:
    """Render a node's stub page from its active mentions (tombstone if none).

    The existing page is the authority for the node's title, aliases, and item_type
    (ADR-0030/0059): the title and classification are preserved across re-extractions, and
    aliases are an additive union of what's on the page and what this run found — so a later
    source cannot overwrite or drop aliases gathered from another (B1/Q4). New hint aliases are
    compared case-insensitively (`_accumulate`); the stored spellings are kept. With no active
    mentions the page is tombstoned (deprecated_candidate) and a `deprecate_wiki_page`
    review item is filed (B4). Either status flip also reconciles the node's unresolved
    review items (ADR-0057): tombstoning withdraws a pending promote, resurrection withdraws
    the recompose-filed deprecation.
    """
    node = graph.get_node(gconn, node_id)
    if node is None:
        return "skipped"
    node_type, slug = node["node_type"], node["slug"]
    page_dir = wiki_dir / NODE_DIR[node_type]
    page_path = page_dir / f"{slug}.md"
    existing = _read_node_meta(page_path)
    # Part-2 review: a decided lifecycle is never re-derived from mentions. A merge tombstone is left
    # as it is; a hidden or retired tag re-renders at its own status (its suggestion list may change).
    decision = decided_status(wiki_dir, node)
    if decision == "merged":
        return "skipped"
    if decision is not None:
        outcome = recompose_semantic_node_page(
            gconn, node_id=node_id, wiki_dir=wiki_dir, status=node["status"],
            review_status=(existing or {}).get("review_status") or "approved", now=now)
        return "written" if outcome == "written" else "skipped"
    new_title = text_hint["title"] if text_hint else None
    new_aliases = text_hint["aliases"] if text_hint else []
    title = (existing["title"] if existing else None) or new_title  # page title is authority
    if title is None:
        return "skipped"
    aliases = _accumulate(existing["aliases"] if existing else [], new_aliases)
    # Classification authority: existing page > graph mirror > this run's hint. A node whose
    # page and mirror both lack a type (drifted state) cannot render — skip, never guess.
    item_type = ((existing or {}).get("item_type") or node.get("item_type")
                 or (text_hint or {}).get("item_type"))
    if not taxonomy.is_item_type(item_type):
        return "skipped"

    sources = graph.sources_for_node(gconn, node_id)
    # Status is page-authoritative: a promotion to `active` is preserved across re-extraction, and
    # so is ADR-0066 `unused` (a tag that lost its last kept link; only a keep/drop changes it,
    # `tag_links`); otherwise candidate while mentioned, deprecated_candidate when not. A kept tag
    # always has an active `human` mention, which re-extraction never supersedes.
    prior = (existing or {}).get("status")
    if prior == "unused":
        status = "unused"
    elif not sources:
        status = "deprecated_candidate"
    elif prior == "active":
        status = "active"
    else:
        status = "candidate"
    page_dir.mkdir(parents=True, exist_ok=True)
    duplicates = graph.active_duplicates(gconn, node_id)
    kept, suggested = graph.tag_projection(gconn, node_id, status)
    page_path.write_text(render_item_page({
        "node_id": node_id, "item_type": item_type,
        "title": title, "aliases": aliases, "confidence": "low", "source_ids": kept,
        "suggested_ids": suggested, "status": status, "duplicates": duplicates,
        "split_from": (existing or {}).get("split_from"),               # ADR-0052: preserve spin-off lineage
        "split_review_id": (existing or {}).get("split_review_id"),
        "description": (existing or {}).get("description"),            # ADR-0058: preserve human description
    }, labels=_link_labels(wiki_dir, kept + suggested, duplicates)), encoding="utf-8")
    graph.upsert_node(gconn, node_id=node_id, node_type=node_type, slug=slug,
                      status=status, item_type=item_type, now=now)
    page_rel = f"{NODE_DIR[node_type]}/{slug}.md"
    if status == "deprecated_candidate":
        reviews.create_review_item(
            reviews_dir, review_type="deprecate_wiki_page",
            subject={"node_id": node_id, "page": page_rel},
            proposal={"to_status": "deprecated_candidate",
                      "reason": reconcile.LEGACY_NO_ACTIVE_MENTIONS_REASON,
                      "reason_code": reconcile.REASON_CODE_NO_ACTIVE_MENTIONS},
            context={"node_type": node_type}, now=now)
    # ADR-0057: the status flip may invalidate an unresolved item's premise in either direction
    # (tombstone -> stale pending promote; resurrection -> stale recompose-filed deprecation).
    reconcile.reconcile_node_items(
        reviews_dir, node_id=node_id, page=page_rel,
        node_status=status, active_source_count=len(sources), now=now)
    return "tombstoned" if status == "deprecated_candidate" else "written"


def recompose_semantic_node_page(
    gconn, *, node_id: str, wiki_dir: Path, status: str, review_status: str, now: str | None = None,
    aliases: list[str] | None = None,
) -> str:
    """Re-render an item page at an EXPLICIT status + review_status (ADR-0035 A5).

    The Phase-6 deprecation executor's render seam for knowledge items (all flow through
    `render_item_page`/`NODE_DIR`). Reloads the node's display metadata (title/aliases/
    item_type) from the existing page — the page is the authority (ADR-0030) — and its active
    `mentions` sources from the graph, preserving citations/evidence and the summary callout, then
    re-renders with the **explicit** status (never re-derived from mentions, so a still-mentioned node
    cannot resurrect out of a deprecation) and mirrors the graph node status. Idempotent and
    deterministic. Returns "written", else a typed skip reason
    ("node_missing"/"unsupported_node_type"/"page_missing"). `aliases`, when given, replaces the
    page's aliases (the human alias edit of an in-use tag, UAT round 8)."""
    node = graph.get_node(gconn, node_id)
    if node is None:
        return "node_missing"
    node_type, slug = node["node_type"], node["slug"]
    if node_type != "item":
        return "unsupported_node_type"
    page_path = wiki_dir / NODE_DIR[node_type] / f"{slug}.md"
    meta = _read_node_meta(page_path)
    if meta is None:
        return "page_missing"
    item_type = meta.get("item_type") or node.get("item_type")
    if not taxonomy.is_item_type(item_type):
        return "page_missing"
    kept, suggested = graph.tag_projection(gconn, node_id, status)
    duplicates = graph.active_duplicates(gconn, node_id)
    rendered = render_item_page({
        "node_id": node_id, "item_type": item_type,
        "title": meta["title"], "aliases": meta["aliases"] if aliases is None else _union(aliases, []),
        "confidence": meta["confidence"],
        "source_ids": kept, "suggested_ids": suggested, "status": status,
        "duplicates": duplicates,
        "split_from": meta.get("split_from"),                           # ADR-0052: preserve spin-off lineage
        "split_review_id": meta.get("split_review_id"),
        "description": meta.get("description"),                        # ADR-0058: preserve human description
    }, review_status=review_status, labels=_link_labels(wiki_dir, kept + suggested, duplicates))
    # Write only when content differs (avoid churn; ADR-0041). confidence is page-owned, preserved.
    # Returns "written" only when the page actually changed, "unchanged" otherwise (the graph node-status
    # mirror always runs, idempotently). Callers that gate on `== "written"` get an honest signal.
    changed = page_path.read_text(encoding="utf-8") != rendered
    if changed:
        page_path.write_text(rendered, encoding="utf-8")
    graph.upsert_node(gconn, node_id=node_id, node_type=node_type, slug=slug, status=status,
                      item_type=item_type, now=now or iso_now())
    return "written" if changed else "unchanged"


def extract_items(
    root: Path,
    *,
    client: LLMClient,
    model_ref: str,
    source_ids: list[str] | None = None,
    force: bool = False,
    manifests_dir: Path | None = None,
    jobs_db: Path | None = None,
    graph_db: Path | None = None,
    markdown_dir: Path | None = None,
    enrichment_dir: Path | None = None,
    wiki_dir: Path | None = None,
    reviews_dir: Path | None = None,
    policies_dir: Path | None = None,
    input_max_chars: int = 300000,
    rebuild_index: bool = True,
    record_job: bool = True,
    cache_only: bool = False,
    cap: int = DEFAULT_SUGGESTION_CAP,
) -> dict[str, Any]:
    """Extract candidate knowledge items for pending (or selected) sources; return a summary.

    ADR-0056 (carried over): one full-document call per source up to ``input_max_chars``
    (the `full-doc-v1` strategy ref component); an above-cap document is truncated and
    marked ``coverage: truncated`` in its artifact and the job metadata.

    ``cache_only`` (ADR-0066 Re-suggest): replay stored responses only — zero provider calls, no
    API key needed. Each document replays under the chain member its artifact recorded; a miss
    leaves the document untouched and is reported in ``cache_miss_sources``.

    ``cap`` (ADR-0066 P1, the ``ENRICH_ITEMS_CAP`` setting): at most this many suggestions per
    document, counting the tags it already keeps — applied by :func:`plan_suggestions` after
    resolution and filtering. Not in the fingerprint: a cap change restales nothing."""
    root = Path(root).resolve()
    manifests_dir = Path(manifests_dir) if manifests_dir else root / "raw" / "manifests"
    jobs_db = Path(jobs_db) if jobs_db else root / "db" / "jobs.sqlite"
    graph_db = Path(graph_db) if graph_db else root / "db" / "graph.sqlite"
    markdown_dir = Path(markdown_dir) if markdown_dir else root / "normalized" / "markdown"
    enrichment_dir = Path(enrichment_dir) if enrichment_dir else root / "normalized" / "enrichment"
    wiki_dir = Path(wiki_dir) if wiki_dir else root / "wiki"
    reviews_dir = Path(reviews_dir) if reviews_dir else root / "reviews"
    policies_dir = Path(policies_dir) if policies_dir else root / "policies"
    from app.workers import tag_links                    # lazy: tag_links imports this module
    if not isinstance(cap, int) or cap <= 0:
        raise ValueError(f"cap must be a positive integer, got {cap!r}")
    rejected_pairs = _rejected_merge_pairs(reviews_dir)

    now = iso_now()
    job_id = f"job_{uuid.uuid4().hex[:16]}"
    conn = None
    if record_job:
        db.init_db(jobs_db)
        conn = db.connect(jobs_db)
        db.insert_job(conn, job_id=job_id, job_type="extract_items", status="running",
                      created_at=now, started_at=now)

    graph.init_db(graph_db)
    gconn = graph.connect(graph_db)

    strategy_ref = art.items_strategy_ref(input_max_chars)
    considered = nodes_written = mentions_written = 0
    plan_totals = {"dropped": 0, "blocked": 0, "hidden": 0, "retired": 0, "over_cap": 0,
                   "resolved": 0, "redirected": 0, "ambiguous": 0, "aliases_filtered": 0}
    no_domain_sources: list[str] = []
    skipped_fresh = skipped_not_extracted = skipped_empty = skipped_no_key = 0
    topic_starved_sources: list[str] = []
    unclassified_count = 0
    coverage_truncated_sources: list[str] = []
    errors: list[dict[str, str]] = []
    cache_miss_sources: list[str] = []
    in_flight_sources: list[dict[str, str]] = []
    input_changed_sources: list[str] = []
    texts: dict[str, dict[str, Any]] = {}     # one document's page hints, by node id
    touched: set[str] = set()                  # the nodes one document's suggestions emit
    page_outcomes: dict[str, str] = {}         # node id -> its last page outcome this run
    journal_reconciled: list[str] = []         # unfinished commits finished from authority
    aliases_withheld: set[str] = set()
    run = publication.Run(root, "extract_items", search=rebuild_index, wiki_dir=wiki_dir)

    def _compose(nids: set[str]) -> None:
        """Render one document's tag pages inside its commit (statuses read and written under the
        lock). Review round 8 follow-up: alias ownership is decided here, at the final write and
        under the lock every keep takes — a keep made while this run was waiting on the model (or
        one that stopped after its ledger write) must not get the buffered aliases unioned back."""
        owned = tag_links.kept_anywhere(reviews_dir)
        for nid in sorted(nids):
            hint = texts.get(nid)
            if hint and hint["aliases"]:
                node = graph.get_node(gconn, nid) or {}
                if owned is None and node.get("status") == "candidate":
                    aliases_withheld.add(nid)             # a damaged ledger may hold its keep
                if owned is None or nid in owned or node.get("status") != "candidate":
                    hint = {**hint, "aliases": []}
            page_outcomes[nid] = _recompose_node(gconn, node_id=nid, wiki_dir=wiki_dir,
                                                 reviews_dir=reviews_dir, now=now, text_hint=hint)

    def _emit(sid, md, entry, source_nodes):
        """Write one planned suggestion (`plan_suggestions`) as an `llm` mention of its target."""
        nonlocal mentions_written, unclassified_count
        nid, name, item_type = entry["node_id"], entry["name"], entry["item_type"]
        # One node per canonical referent (ADR-0059 decision 3): when the model classifies an
        # existing tag — by exact name or resolved by key (ADR-0066 P6 step 6) — under a DIFFERENT
        # type, the mention routes to the existing node (its page keeps its item_type — the
        # authority) and a `change_item_type` review is filed; nothing auto-retypes. The sentinel
        # never proposes a retype (an unsure classification is not a correction).
        existing = graph.get_node(gconn, nid)
        used_type = (existing.get("item_type") if existing else None) or item_type
        type_conflict = (existing is not None and existing.get("item_type") is not None
                         and item_type != existing["item_type"]
                         and taxonomy.is_production_item_type(item_type))
        if used_type == taxonomy.UNCLASSIFIED:
            unclassified_count += 1
        if entry["resolution"] not in ("resolved", "redirected"):
            # A live tag keeps its slug and status (a renamed tag's slug is not the extracted
            # name's); anything else is (re)created as a candidate. Decided tags never get here.
            keep_status = existing is not None and existing.get("status") in _RESOLVABLE
            graph.upsert_node(gconn, node_id=nid, node_type="item",
                              slug=existing["slug"] if keep_status else _slug(name),
                              status=existing["status"] if keep_status else "candidate",
                              item_type=used_type, now=now)
            # A never-kept suggestion's page unions in this run's (body-checked) aliases. After the
            # first keep the user owns the list (part-2 review): extraction adds nothing to a live
            # tag, and a resolved/redirected suggestion adds nothing to its target (merges still
            # union, ADR-0050).
            texts[nid] = {"title": name, "aliases": [] if keep_status else entry["aliases"],
                          "item_type": used_type}
        # Optional evidence anchor when the name is mechanically locatable (Q2).
        span = citations.locate_quote(md, name)
        anchor = {"evidence_source_id": sid, "evidence_char_start": span[0],
                  "evidence_char_end": span[1]} if span else {}
        graph.upsert_assertion(gconn, src_id=sid, dst_id=nid, edge_type="mentions",
                               asserted_by="llm", status="active", job_id=job_id, now=now, **anchor)
        touched.add(nid)
        mentions_written += 1
        row = {"node_id": nid, "item_type": used_type, "name": name, "aliases": entry["aliases"],
               "resolution": entry["resolution"]}
        if entry["aliases_filtered"]:
            row["aliases_filtered"] = entry["aliases_filtered"]
        if entry.get("ambiguous_with"):
            row["ambiguous_with"] = entry["ambiguous_with"]
        source_nodes.append(row)

        if type_conflict:
            # Subject keys on the proposed classification change, not just the node (ADR-0051
            # precedent): a rejected retype to one type must not lock out retyping to another.
            reviews.create_review_item(
                reviews_dir, review_type="change_item_type",
                subject={"node_id": nid, "to_item_type": item_type},
                proposal={"to_item_type": item_type},
                context={"source_id": sid, "name": name,
                         "from_item_type": existing["item_type"]}, now=now)

    try:
        # ADR-0063: resolve the tier chain INSIDE the protected block so a malformed-chain ConfigError
        # marks the job failed and closes connections rather than orphaning a "running" job. `chain_refs`
        # (plain split, pre-resolution) drives sticky-to-chain freshness; resolve_run_model validates.
        chain_refs = [c.strip() for c in model_ref.split(",") if c.strip()]
        model_ref, has_key = client.resolve_run_model(model_ref)
        manifests, skipped_invalid = valid_manifests(manifests_dir)
        if source_ids is not None:
            wanted = set(source_ids)
            manifests = [m for m in manifests if m.get("source_id") in wanted]
        # ADR-0068: commits an earlier run left unfinished (fails closed on a damaged journal).
        unfinished = journal.pending(root, _PRODUCER)
        eligible = {m["source_id"] for m in manifests
                    if m.get("ingestion_status") in _ENRICHABLE_STATUSES}
        for sid in sorted(unfinished):
            if sid not in eligible and (source_ids is None or sid in source_ids):
                # Deleted or no longer extractable: nothing will re-extract it, so finish the
                # commit from authority — re-render its saved tags (one left unmentioned retires).
                with tag_links.ledger_lock(reviews_dir):
                    rec = journal.pending(root, _PRODUCER).get(sid)
                    if rec is not None:
                        texts, touched = {}, set()
                        intent = run.intent(items=rec["items"])
                        _compose(set(rec["items"]))
                        run.publish(intent, later_items=rec["items"])
                        # Cleared only once verified: none of its saved tags is left standing
                        # without a mention, and every tag it still mentions has its page.
                        ids = set(rec["items"]) | graph.item_ids_for_source(gconn, sid)
                        if not (set(graph.orphans(gconn, edge_type="mentions", source_id=sid))
                                & ids) and all(_has_page(wiki_dir, gconn, n) for n in ids):
                            journal.finish(root, _PRODUCER, sid)
                            journal_reconciled.append(sid)
        attempted: set[str] = set()

        for manifest in manifests:
            sid = manifest["source_id"]
            if manifest.get("ingestion_status") not in _ENRICHABLE_STATUSES:
                skipped_not_extracted += 1
                continue
            considered += 1
            attempted.add(sid)

            md_path = markdown_dir / f"{sid}.md"
            md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
            apath = art.items_artifact_path(enrichment_dir, sid)
            existing: dict[str, Any] = {}
            if apath.exists():
                try:
                    existing = json.loads(apath.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    existing = {}
                if not isinstance(existing, dict):
                    existing = {}
            # ADR-0066 part 3: the prompt title is a prompt input, so it is part of freshness.
            title = titles.prompt_title_for(manifest, md, enrichment_dir=enrichment_dir,
                                            logs_dir=markdown_dir.parent / "extraction_logs")
            # ADR-0063 sticky-to-chain: fresh iff the recorded model is still a chain member and
            # its own-model fingerprint matches — an availability flip alone never restales.
            # An unfinished commit is never fresh: the artifact predates it (written last).
            if not force and existing and sid not in unfinished and art.chain_fresh(
                    existing, chain_refs,
                    lambda m: art.items_fingerprint(md, m, strategy_ref, prompt_input=title)):
                skipped_fresh += 1
                continue
            # A cache-only replay asks for the response the recorded chain member produced.
            run_ref = (existing["model_ref"] if cache_only and existing.get("model_ref") in chain_refs
                       else model_ref)
            fingerprint = art.items_fingerprint(md, run_ref, strategy_ref, prompt_input=title)
            # ADR-0066: a damaged ledger skips the document BEFORE the (billable) model call —
            # filtering against an empty drop list would resurrect drops. Re-read under the lock below.
            try:
                tag_links.drop_filter(reviews_dir, sid)
            except tag_links.LedgerCorrupt as exc:
                errors.append({"source_id": sid, "error": f"ledger_corrupt: {exc.problem}"})
                continue
            # ADR-0066 review round 4: a document linked to a tag an unfinished merge/split owns is
            # skipped (re-checked under the lock below) — re-run after /reviews/apply finishes it.
            if (busy := tag_links.source_in_flight(gconn, reviews_dir, sid)) is not None:
                in_flight_sources.append({"source_id": sid, "review_id": busy})
                continue

            # ADR-0055 rollout safety: supersede a source's prior mentions ONLY once this run can
            # produce the replacement state — after a successful parse (below), or deterministically
            # for an empty source. A no-key or failed-parse run must leave the existing topic layer
            # untouched: supersede-then-skip would recompose affected nodes with zero mentions and
            # tombstone them (the wipe hazard a prompt-version bump arms vault-wide).
            if not has_key and not cache_only:
                skipped_no_key += 1
                continue
            if not md.strip():
                with tag_links.ledger_lock(reviews_dir):
                    rec = journal.begin(root, _PRODUCER, sid,
                                        items=graph.item_ids_for_source(gconn, sid), now=now)
                    intent = run.intent(sources=[sid], items=rec["items"])
                    texts = {}
                    affected = set(rec["items"]) | set(
                        graph.supersede_mentions_for_source(gconn, sid, now=now))
                    _compose(affected)
                    _write_artifact(apath, sid, fingerprint, [], run_ref, now, strategy_ref,
                                    prompt_input=title)
                    run.publish(intent, sources=[sid], later_items=affected)
                    journal.finish(root, _PRODUCER, sid)
                skipped_empty += 1
                continue

            # ADR-0056: one full-document call up to the input cap; above-cap documents are
            # truncated and marked (coverage honesty — the head-bias returns only for
            # pathological inputs, and visibly).
            coverage = "truncated" if len(md) > input_max_chars else "full"
            if coverage == "truncated":
                coverage_truncated_sources.append(sid)
            try:
                result = client.parse(
                    prompts.build_items_messages(title, md, max_chars=input_max_chars),
                    prompts.ITEMS_SCHEMA, run_ref,
                    schema_version=art.ITEMS_SCHEMA_VERSION,
                    prompt_version=art.ITEMS_PROMPT_VERSION,
                    strategy_ref=strategy_ref,
                    cache_only=cache_only,
                )
            except CacheMiss:
                cache_miss_sources.append(sid)            # typed outcome: nothing written
                continue
            except ParseError as exc:
                errors.append({"source_id": sid, "error": str(exc)})
                continue

            # ADR-0066 review round 3: the model call ran OUTSIDE the ledger lock, so a drop or block
            # made meanwhile must win — re-read both under the lock, then write this document's links
            # and artifact before releasing it.
            with tag_links.ledger_lock(reviews_dir):
                # ADR-0068: the suggestions are valid only for the text the model read; a
                # re-extraction installed meanwhile makes them stale — write nothing, the next run
                # extracts the new text.
                # ADR-0066 part 3: likewise the prompt title (an Enrich run may change it meanwhile).
                if ((md_path.read_text(encoding="utf-8") if md_path.exists() else "") != md
                        or titles.prompt_title_for(
                            manifest, md, enrichment_dir=enrichment_dir,
                            logs_dir=markdown_dir.parent / "extraction_logs") != title):
                    input_changed_sources.append(sid)
                    continue
                try:
                    dropped = tag_links.drop_filter(reviews_dir, sid)
                    kept_ids = set(tag_links.load_ledger(reviews_dir, sid)["kept"])
                except tag_links.LedgerCorrupt as exc:
                    errors.append({"source_id": sid, "error": f"ledger_corrupt: {exc.problem}"})
                    continue
                blocked_keys = set(tag_links.load_blocked(policies_dir))
                vocab = _resolution_index(gconn, wiki_dir)
                plan = plan_suggestions(
                    result["items"], md=md, gconn=gconn, vocab=vocab, rejected_pairs=rejected_pairs,
                    dropped=dropped, blocked_keys=blocked_keys, kept_ids=kept_ids, cap=cap)
                busy = tag_links.source_in_flight(
                    gconn, reviews_dir, sid,
                    [n for e in plan["emit"] for n in [e["node_id"], *(e.get("ambiguous_with") or [])]]
                    + [node_id(str(it.get("name") or "")) for it in result["items"][:_MAX_ITEMS]
                       if isinstance(it, dict)])
                if busy is not None:                          # nothing written for this document
                    in_flight_sources.append({"source_id": sid, "review_id": busy})
                    continue
                # ADR-0068 decision 5: this document commits as one unit — the graph, its tag pages,
                # then the artifact last (an interrupted commit leaves it stale, so the next run
                # redoes it) — and publishes its Source page before releasing the lock.
                # The journal record keeps every tag this commit may retire — the document's current
                # ones, unioned with any an interrupted attempt saved — so superseding the mentions
                # below can't lose them (the record goes only after the artifact).
                rec = journal.begin(root, _PRODUCER, sid,
                                    items=graph.item_ids_for_source(gconn, sid), now=now)
                intent = run.intent(sources=[sid], items=rec["items"])
                texts, touched = {}, set()
                # Parse succeeded: retire the old mentions immediately before emitting replacements
                # (supersede covers ALL active mentions for the source, so it must precede _emit).
                affected = set(rec["items"]) | set(
                    graph.supersede_mentions_for_source(gconn, sid, now=now))

                # ADR-0068: the manifest read at the run's start may be stale (an archive applied
                # during the model call) — mirror the status as it is now, under the lock.
                graph.upsert_node(
                    gconn, node_id=sid, node_type="source", slug=sid,
                    status=get_status(load_manifest(manifests_dir, sid) or manifest), now=now)
                source_nodes: list[dict[str, Any]] = []
                for entry in plan["emit"]:
                    _emit(sid, md, entry, source_nodes)
                for k in plan_totals:
                    plan_totals[k] += plan["counts"][k]
                if not plan["counts"]["domains"]:
                    no_domain_sources.append(sid)

                _compose(touched | affected)
                _write_artifact(apath, sid, fingerprint, source_nodes, run_ref, now,
                                strategy_ref, coverage, plan["filtered"],
                                reference_cut_at=plan["reference_cut_at"], cap=cap,
                                prompt_input=title)
                # Other Source pages that list these tags are refreshed once, at the end.
                run.publish(intent, sources=[sid], later_items=touched | affected)
                journal.finish(root, _PRODUCER, sid)
                nodes_written += len(source_nodes)
                # ADR-0059: immediate operator feedback for the starvation failure signature (no
                # thematic topic layer from a substantive source). Artifact/claim state only —
                # never text-shape inference.
                if art.topic_starved(source_nodes, art.stored_claim_count(enrichment_dir, sid)):
                    topic_starved_sources.append(sid)

        pages_written = sum(o == "written" for o in page_outcomes.values())
        pages_tombstoned = sum(o == "tombstoned" for o in page_outcomes.values())
        # The end refresh (decision 5): the Source pages of every tag touched, index.md and the
        # keyword index, once — the job succeeds only after it.
        pub = run.finish()
        left = journal.pending(root, _PRODUCER)
        # A document this run attempted whose commit is still unfinished is never a clean success.
        left_attempted = sorted(set(left) & attempted)

        if errors or cache_miss_sources or in_flight_sources or left_attempted:
            status = "partial"
        elif not has_key and not cache_only and considered > 0:
            status = "skipped"
        else:
            status = "succeeded"
        status = publication.job_status(status, pub)

        summary: dict[str, Any] = {
            "job_id": job_id, "model_ref": model_ref, "status": status,
            "sources_considered": considered, "nodes_written": nodes_written,
            "mentions_written": mentions_written, "node_pages_written": pages_written,
            "node_pages_tombstoned": pages_tombstoned, "skipped_fresh": skipped_fresh,
            "skipped_not_extracted": skipped_not_extracted, "skipped_empty": skipped_empty,
            "skipped_no_key": skipped_no_key, "manifests_skipped_invalid": len(skipped_invalid),
            "topic_starved": len(topic_starved_sources),
            "topic_starved_sources": sorted(topic_starved_sources),
            "unclassified_items": unclassified_count,
            "suggestions_filtered_dropped": plan_totals["dropped"],
            "suggestions_filtered_blocked": plan_totals["blocked"],
            # Part-2 review: names of tags a human hid / retired (filtered) or merged (redirected).
            "suggestions_filtered_hidden": plan_totals["hidden"],
            "suggestions_filtered_retired": plan_totals["retired"],
            "suggestions_redirected": plan_totals["redirected"],
            # ADR-0066 part 2: the cap cut, key resolution, ambiguous matches, the alias check and
            # documents with no domain suggestion (code never invents one).
            "suggestion_cap": cap,
            "suggestions_over_cap": plan_totals["over_cap"],
            "suggestions_resolved": plan_totals["resolved"],
            "suggestions_ambiguous": plan_totals["ambiguous"],
            "aliases_filtered": plan_totals["aliases_filtered"],
            # Candidates whose new aliases were not added because a damaged ledger may keep them.
            "aliases_withheld_ledger_damaged": len(aliases_withheld),
            "no_domain": len(no_domain_sources),
            "no_domain_sources": sorted(no_domain_sources),
            "coverage_truncated": len(coverage_truncated_sources),
            "coverage_truncated_sources": sorted(coverage_truncated_sources),
            "errors": len(errors), "error_details": errors,
            "cache_only": cache_only, "cache_misses": len(cache_miss_sources),
            "cache_miss_sources": sorted(cache_miss_sources),
            "in_flight_sources": in_flight_sources,
            # ADR-0068 producer journal: documents whose commit is still unfinished (redone by the
            # next run that reaches them), and the ones finished here from authority.
            "unfinished_commits": sorted(left), "journal_reconciled": journal_reconciled,
            "input_changed_sources": input_changed_sources,
            "index_rebuilt": bool(pub and pub["index_rebuilt"]),
            "keyword_reindexed": bool(pub and pub["keyword_reindexed"]),
            "publication": publication.summary(pub), "extracted_at": now,
        }
        if conn is not None:
            db.update_job(conn, job_id, status=status, finished_at=iso_now(), metadata=summary)
        return summary
    except Exception as exc:
        if conn is not None:
            db.update_job(conn, job_id, status="failed", finished_at=iso_now(), error_message=str(exc))
        raise
    finally:
        gconn.close()
        if conn is not None:
            conn.close()


def _has_page(wiki_dir: Path, gconn, nid: str) -> bool:
    """Whether a tag has its page (a tag deleted meanwhile needs none)."""
    node = graph.get_node(gconn, nid)
    return node is None or (Path(wiki_dir) / NODE_DIR["item"] / f"{node['slug']}.md").exists()


def _write_artifact(apath, sid, fingerprint, source_nodes, model_ref, now,
                    strategy_ref=None, coverage="full", filtered=None, *, reference_cut_at=None,
                    cap=None, prompt_input=None):
    apath.parent.mkdir(parents=True, exist_ok=True)
    apath.write_text(json.dumps({
        "source_id": sid, "schema_version": art.ITEMS_SCHEMA_VERSION,
        "prompt_version": art.ITEMS_PROMPT_VERSION, "model_ref": model_ref,
        "strategy_ref": strategy_ref,
        "prompt_input": prompt_input,              # the prompt title (ADR-0066 part 3)
        # ADR-0056 honesty marker: "truncated" when the document exceeded the input cap, so
        # "document-complete" stays auditable (a future lint can consume this).
        "coverage": coverage,
        "input_fingerprint": fingerprint, "generation_status": "enriched",
        "generated_at": now, "nodes": source_nodes,
        # ADR-0066: names extracted but not suggested (dropped here / blocked vault-wide / past the
        # cap); `cap` is the setting this run cut at (audit only — not part of the fingerprint).
        "filtered": filtered or [],
        "cap": cap,
        # ADR-0066 P4: where the reference list starts (aliases must occur before it), or null.
        "reference_cut_at": reference_cut_at,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
