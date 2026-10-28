#!/usr/bin/env python3
"""Merge-candidate producer (ADR-0064): deterministic `merge_items` proposals over knowledge items.

Detect-only and key-free (no model, no embedder) — the `/jobs/lint` `merge_candidate` check files
what this module computes. Two active items are a **merge candidate** when their merge-key sets
intersect: each item contributes `merge_key(title)` plus `merge_key(alias)` for every alias, read from
the item page frontmatter (title/aliases authority, ADR-0030); node set + status come from the graph.

- **Lifecycle (decision 4):** only `active ↔ active` pairs are filed (the merge executor's own guard);
  a match involving a `candidate` is reported (`unpromoted`) and never filed. Other statuses are ignored.
- **Rejection memory (decision 6):** an unordered pair is *suppressed* when any `merge_items` item for
  it, in EITHER direction, is pending/deferred/approved/rejected — so a rejected pair never comes back
  reversed after support counts shift.
- **In-flight freeze (decision 7):** a node in an unresolved merge, or in an approved merge whose
  absorbed node is still active, takes no part in new proposals.
- **Clusters (decision 8):** suppressed pairs are cut first, then each connected component of size ≥2
  becomes a STAR around one survivor (most live support → most inbound alias pointers → smallest
  item_id). Never a chain — applying `A←B` would withdraw a pending `B←C` and drop C silently.
  **Direct matches only** (review round): a member that shares no key with the survivor is not filed
  this run — once `A←B` applies, the alias union gives A B's names, so the next run proposes `A←C`
  with real A–C evidence instead of a transitive guess.
- **Blocked tags (UAT round 8):** a tag whose title is on the blocked list (ADR-0066 *never suggest*)
  takes no part in proposals, on either side: the user wants it gone, and merging it would hand its
  names to the other tag as aliases (or make it a survivor). A human-filed merge is still allowed.
- **Key budget (review round):** a merge key carried by more than `max_items_per_key` active items
  (default 5, `policies/review.yaml` `merge_candidates.max_items_per_key`) is too generic to signal
  identity — it pairs nothing and is reported once (`too_common`), bounding pair generation.

Byte-stable: identical graph + pages + ledger ⇒ identical proposals in identical order.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from app.backend import graph, search
from app.workers import items
from app.workers.wiki_render import NODE_DIR, parse_frontmatter

REASON_CODE = "alias_cross_match"
_MERGE_TYPE = "merge_items"
_CONSIDERED = frozenset({"active", "candidate"})
_PUNCT = re.compile(r"[^\w\s]|_")
_PLURAL_KEEP = ("ss", "us", "is")
DEFAULT_MAX_ITEMS_PER_KEY = 5


def _tokens(text: str) -> list[str]:
    s = items._normalize_name(unicodedata.normalize("NFKC", text))
    s = s.replace("&", " and ").replace("'", "").replace("’", "")
    return _PUNCT.sub(" ", s).split()


def _singular(token: str) -> str:
    if len(token) >= 4 and token.endswith("s") and not token.endswith(_PLURAL_KEEP):
        return token[:-1]
    return token


def merge_key(text: Any) -> str:
    """The ADR-0064 decision-3 fold: NFKC → `_normalize_name` → `&`→`and`, apostrophes deleted,
    hyphens/other punctuation → space → plural fold on the LAST token only (≥4 chars, not -ss/-us/-is).
    Returns "" for an empty/non-string input (an empty key never matches)."""
    if not isinstance(text, str):
        return ""
    tokens = _tokens(text)
    if tokens:
        tokens[-1] = _singular(tokens[-1])
    return " ".join(tokens)


def fold_text(text: Any) -> str:
    """A running text folded for phrase search (ADR-0066 P4 alias check): the `merge_key` fold with
    EVERY token singularised, space-padded so :func:`occurs_in` matches whole tokens only."""
    if not isinstance(text, str):
        return " "
    return " " + " ".join(_singular(t) for t in _tokens(text)) + " "


def occurs_in(phrase: Any, folded: str) -> bool:
    """True when `phrase` occurs in a :func:`fold_text` result — case-, punctuation- and
    plural-insensitive, on whole tokens. An empty phrase never occurs."""
    if not isinstance(phrase, str):
        return False
    tokens = [_singular(t) for t in _tokens(phrase)]
    return bool(tokens) and f" {' '.join(tokens)} " in folded


def _item_record(wiki_dir: Path, node: dict[str, Any]) -> dict[str, Any] | None:
    """Title/aliases from the page (frontmatter authority); None when the page is missing, unreadable,
    or owned by a different id (projection integrity is validate_projection's job, not ours)."""
    slug = node.get("slug")
    if not graph.is_safe_slug(slug):
        return None
    path = Path(wiki_dir) / NODE_DIR["item"] / f"{slug}.md"
    try:
        fm = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    if fm.get("item_id") != node["node_id"] or not isinstance(fm.get("title"), str):
        return None
    aliases = [a for a in (fm.get("aliases") or []) if isinstance(a, str)] \
        if isinstance(fm.get("aliases"), list) else []
    keys: dict[str, list[str]] = {}
    for field, text in [("title", fm["title"]), *(("alias", a) for a in aliases)]:
        k = merge_key(text)
        if k:
            keys.setdefault(k, [])
            label = "title" if field == "title" else f"alias:{text}"
            if label not in keys[k]:
                keys[k].append(label)
    return {"node_id": node["node_id"], "slug": slug, "status": node["status"],
            "title": fm["title"], "title_key": merge_key(fm["title"]), "keys": keys}


def _merge_ledger(reviews_dir: Path) -> list[tuple[str, dict[str, Any]]]:
    """Every readable merge_items item as (state_dir, item), sorted by path (deterministic)."""
    out: list[tuple[str, dict[str, Any]]] = []
    for state in ("pending", "approved", "rejected"):
        d = Path(reviews_dir) / state
        for path in sorted(d.glob("*.json")) if d.exists() else []:
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(item, dict) and item.get("type") == _MERGE_TYPE \
                    and isinstance(item.get("subject"), dict):
                out.append((state, item))
    return out


def _pair(item: dict[str, Any]) -> tuple[str, str] | None:
    s = item["subject"]
    a, b = s.get("survivor_node_id"), s.get("absorbed_node_id")
    if isinstance(a, str) and isinstance(b, str) and a != b:
        return a, b
    return None


def find_merge_candidates(gconn: Any, *, wiki_dir: Path, reviews_dir: Path,
                          max_items_per_key: int = DEFAULT_MAX_ITEMS_PER_KEY,
                          blocked_keys: Any = frozenset()) -> dict[str, Any]:
    """Compute merge proposals. Returns {"proposals", "unpromoted", "pending", "too_common", "blocked"}:

    - `proposals`: new star proposals to file — {subject, proposal, context};
    - `unpromoted`: report-only matches with a candidate side — {node_ids, matched_keys};
    - `pending`: unresolved machine-filed (`alias_cross_match`) merge items, re-reported each run;
    - `too_common`: keys over the budget, ignored for pairing — {key, active_items};
    - `blocked`: ids of tags left out because their title is in `blocked_keys` (merge_key fold).
    Reads only; files nothing."""
    status = {n["node_id"]: n["status"] for n in graph.nodes_of_type(gconn, "item")}
    records = {}
    blocked: list[str] = []
    for node in graph.nodes_of_type(gconn, "item"):
        if node["status"] in _CONSIDERED:
            rec = _item_record(wiki_dir, node)
            if rec is not None and rec["title_key"] in blocked_keys:
                blocked.append(rec["node_id"])
            elif rec is not None:
                records[rec["node_id"]] = rec

    suppressed: set[frozenset[str]] = set()
    in_flight: set[str] = set()
    pending: list[dict[str, Any]] = []
    for state, item in _merge_ledger(reviews_dir):
        pair = _pair(item)
        if pair is None:
            continue
        suppressed.add(frozenset(pair))
        if state == "pending" and item.get("status") in ("pending", "deferred"):
            in_flight.update(pair)
            if (item.get("proposal") or {}).get("reason_code") == REASON_CODE:
                pending.append(item)
        elif state == "approved" and status.get(pair[1]) == "active":
            in_flight.update(pair)

    by_key: dict[str, list[str]] = {}
    for nid in sorted(records):
        for k in records[nid]["keys"]:
            by_key.setdefault(k, []).append(nid)
    edges: dict[tuple[str, str], list[str]] = {}
    too_common: list[dict[str, Any]] = []
    for k in sorted(by_key):
        ids = by_key[k]
        n_active = sum(1 for n in ids if records[n]["status"] == "active")
        if n_active > max_items_per_key:
            too_common.append({"key": k, "active_items": n_active})
            continue
        for i, x in enumerate(ids):
            for y in ids[i + 1:]:
                edges.setdefault((x, y), []).append(k)

    unpromoted: list[dict[str, Any]] = []
    adjacency: dict[str, set[str]] = {}
    for (x, y), keys in sorted(edges.items()):
        sx, sy = records[x]["status"], records[y]["status"]
        if "candidate" in (sx, sy):
            unpromoted.append({"node_ids": [x, y], "matched_keys": keys})
            continue
        if frozenset((x, y)) in suppressed or x in in_flight or y in in_flight:
            continue
        adjacency.setdefault(x, set()).add(y)
        adjacency.setdefault(y, set()).add(x)

    proposals: list[dict[str, Any]] = []
    seen: set[str] = set()
    for start in sorted(adjacency):
        if start in seen:
            continue
        component, stack = [], [start]
        seen.add(start)
        while stack:
            cur = stack.pop()
            component.append(cur)
            for nxt in sorted(adjacency[cur]):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        component.sort()
        support = {n: graph.count_independent_sources(
            gconn, n, source_statuses=search.RETENTION_DEFAULT_STATUSES) for n in component}
        inbound = {n: sum(1 for m in component if m != n
                          and any(lbl.startswith("alias:")
                                  for lbl in records[m]["keys"].get(records[n]["title_key"], [])))
                   for n in component}
        survivor = min(component, key=lambda n: (-support[n], -inbound[n], n))
        for member in component:
            if member == survivor or member not in adjacency[survivor]:
                continue                     # indirect: waits for the alias union (direct only)
            keys = sorted(edges[tuple(sorted((survivor, member)))])
            evidence = [{"key": k, "survivor_field": records[survivor]["keys"].get(k, []),
                         "absorbed_field": records[member]["keys"].get(k, [])} for k in keys]
            proposals.append({
                "subject": {"survivor_node_id": survivor, "absorbed_node_id": member},
                "proposal": {"to_status": "merged", "reason_code": REASON_CODE,
                             "matched_keys": keys},
                "context": {
                    "survivor": {"title": records[survivor]["title"],
                                 "slug": records[survivor]["slug"], "support": support[survivor]},
                    "absorbed": {"title": records[member]["title"],
                                 "slug": records[member]["slug"], "support": support[member]},
                    "match_evidence": evidence, "component_size": len(component)},
            })
    return {"proposals": proposals, "unpromoted": unpromoted, "pending": pending,
            "too_common": too_common, "blocked": sorted(blocked)}
