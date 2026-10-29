#!/usr/bin/env python3
"""Per-document tag links (ADR-0066): keep / drop, drop records, unused tags, the blocked-tag list.

In the user's model a knowledge item **is a tag**, and tagging is per document. On a document's
review screen each suggested tag is a **tag link** (the document → item `mentions` edge), decided:

- **keep** — the document is tagged. The authority is the per-document **tag-decision ledger**
  (`reviews/tag_links/<source_id>.json`, file-based like the rest of the review ledger, so it
  survives graph rebuilds and backup/restore), mirrored as an `asserted_by: human` anchorless
  `mentions` edge. Re-extraction supersedes only `llm` mentions, so **a keep lasts until the user
  drops it**. A candidate tag becomes `active` on its first keep (the keep IS the promotion, via
  `promote.activate_node`); an `unused` tag revives.
- **drop** — this document is not tagged: the link's edges become `rejected` and a **drop record**
  `(document, merge_key(name))` is written, which silently filters the same suggestion when the
  document is re-extracted. Only this document is affected. A never-kept tag whose links are all
  gone is **deleted** (node, page, edges); an active tag that lost its last kept link becomes
  **`unused`** (hidden, edits intact, revived by any keep, bulk-deleted only by an explicit human
  "Delete unused" that skips merge-redirect targets).
- **block** ("never suggest") — the name goes on `policies/blocked_tags.txt` (one name per line,
  `#` comments, matched by `merge_key`). Extraction filters blocked names; pending suggestions of a
  blocked tag are WITHDRAWN (edge superseded, **no drop record** — a drop record means "the user
  dropped this here"). Kept links are never touched; an explicit human-add of a blocked name is
  allowed. Unblocking affects future extractions; the affected documents can be re-suggested with
  a cache-only `scripts/extract_items.py --force --cache-only <source_id>…` (zero provider calls).
- **use X** — an ambiguous suggestion (its name matches several existing tags, ADR-0066 P3) is
  resolved to the tag the user chose: keep X here, drop the suggestion, audit `tag-resolved`. A
  pending record makes an interrupted one visible until the user retries or cancels it.

Every operation here writes immediately (a producer-class act on the document's own links, like
the ADR-0058 human-add) and returns `affected_sources`; the caller re-renders those Source pages and
rebuilds `wiki/index.md` + the keyword index once per batch (:func:`finish`). Blocked outcomes write
NOTHING.

Integrity (ADR-0066 review round 2): every read-modify-write of a ledger or the blocked list runs
under :func:`ledger_lock`, the vault write lock (`app/backend/write_lock.py`, ADR-0068); the ledger is written BEFORE the graph mirror; a ledger that exists but does not
parse raises :class:`LedgerCorrupt` and is never overwritten (actions return `ledger_corrupt`).
:func:`consistency_errors` / :func:`repair` check and restore ledger ↔ graph agreement
(`scripts/validate_tag_links.py`, `scripts/rebuild_tag_links.py`).
"""
from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from app.backend import graph, taxonomy, write_lock
from app.backend.manifests import is_source_id, iso_now
from app.workers import identity_journal, items, merge_candidates, merges, promote
from app.workers.wiki_render import NODE_DIR, REVIEW_STATUSES, parse_frontmatter

LEDGER_DIR = "tag_links"
LOCK_NAME = ".lock"
BLOCKLIST_NAME = "blocked_tags.txt"
_LINKABLE_STATUSES = frozenset({"candidate", "active", "unused"})
_BLOCKLIST_HEADER = (
    "# Blocked tags (ADR-0066): names never suggested by extraction, matched case-, punctuation-\n"
    "# and plural-insensitively. One name per line; '#' starts a comment. Kept links are never\n"
    "# affected; an explicit human-add of a blocked name is allowed.\n")


def tag_key(name: Any) -> str:
    """The comparison key for drop records and the blocked list (the ADR-0064 `merge_key` fold)."""
    return merge_candidates.merge_key(name)


def _blocked(reason: str, **extra: Any) -> dict[str, Any]:
    return {"outcome": "blocked", "reason": reason, **extra}


def _in_flight(reviews_dir: Path, node_ids: Any) -> dict[str, Any] | None:
    """The typed refusal for an action on a tag an unfinished merge/split owns (review round 4)."""
    rid = identity_journal.operation_for(reviews_dir, node_ids)
    return None if rid is None else _blocked("identity_operation_in_flight", review_id=rid,
                                             hint="run /reviews/apply to finish it first")


def source_in_flight(gconn, reviews_dir: Path, source_id: str, node_ids: Any = ()) -> str | None:
    """The review id of an in-flight operation that owns a tag `source_id` links to (in any status)
    or one of `node_ids`, else None — extraction skips such a document until apply finishes."""
    ops = identity_journal.open_operations(reviews_dir)
    if not ops:
        return None
    if any(o["nodes"] is None for o in ops):
        return ops[0]["review_id"]
    linked = {r["dst_id"] for r in gconn.execute(
        "SELECT DISTINCT dst_id FROM edges WHERE src_id = ? AND edge_type = 'mentions'", (source_id,))}
    return identity_journal.operation_for(reviews_dir, linked | set(node_ids))


def ledger_action(fn):
    """Run a ledger/blocked-list action under :func:`ledger_lock`; a damaged ledger is a typed
    `ledger_corrupt` block (every action loads its ledger before its first write)."""
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        with ledger_lock(kwargs["reviews_dir"]):
            try:
                return fn(*args, **kwargs)
            except LedgerCorrupt as exc:
                return _blocked("ledger_corrupt", source_id=exc.source_id, problem=exc.problem)
    return wrapper


# --- the per-document ledger ---------------------------------------------------


class LedgerCorrupt(ValueError):
    """A tag-decision ledger exists but does not parse as one — never treated as empty, never
    overwritten (the human decisions in it would be lost)."""

    def __init__(self, source_id: str, problem: str) -> None:
        super().__init__(f"tag ledger {source_id}: {problem}")
        self.source_id, self.problem = source_id, problem


# Ledger and blocked-list updates serialize on the vault write lock (ADR-0068 decision 2), which
# grew out of this module's ledger lock and keeps its file, `reviews/tag_links/.lock`.
ledger_lock = write_lock.hold
_held = write_lock._held


def ledger_path(reviews_dir: Path, source_id: str) -> Path:
    if not is_source_id(source_id):
        raise ValueError("non-canonical source id")       # never build a path from a forged id
    return Path(reviews_dir) / LEDGER_DIR / f"{source_id}.json"


def _ledger_problem(data: Any, source_id: str) -> str | None:
    """Why an EXISTING file is not a ledger (None if it is). Every field `_save_ledger` writes is
    required — a damaged record is never completed with defaults."""
    if not isinstance(data, dict):
        return "not a JSON object"
    missing = [k for k in ("source_id", "kept", "drops") if k not in data]
    if missing:
        return f"missing required field(s) {missing}"
    if data["source_id"] != source_id:
        return "source_id does not match the file name"
    kept = data["kept"]
    if not isinstance(kept, dict) or any(
            not isinstance(k, str) or not isinstance(v, dict) or not isinstance(v.get("at"), str)
            or not isinstance(v.get("amendments", {}), dict) for k, v in kept.items()):
        return "`kept` is not a {node_id: {at, by[, amendments]}} map"
    drops = data["drops"]
    if not isinstance(drops, list) or any(
            not isinstance(d, dict) or not isinstance(d.get("key"), str) or not d["key"]
            or not isinstance(d.get("keys", []), list) for d in drops):
        return "`drops` is not a list of drop records"
    return None


def load_ledger(reviews_dir: Path, source_id: str) -> dict[str, Any]:
    """`{source_id, kept: {node_id: {at, by[, amendments]}}, drops: [{key, keys, name, item_id, at}]}`
    (empty if absent). Raises :class:`LedgerCorrupt` when the file exists but is not a valid ledger."""
    path = ledger_path(reviews_dir, source_id)
    if not path.exists():
        return {"source_id": source_id, "kept": {}, "drops": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerCorrupt(source_id, f"unreadable ({type(exc).__name__})") from exc
    problem = _ledger_problem(data, source_id)
    if problem:
        raise LedgerCorrupt(source_id, problem)
    return {"source_id": source_id, "kept": dict(data.get("kept") or {}),
            "drops": [dict(d) for d in data.get("drops") or []]}


def _save_ledger(reviews_dir: Path, ledger: dict[str, Any]) -> None:
    path = ledger_path(reviews_dir, ledger["source_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    ledger = {"source_id": ledger["source_id"],
              "kept": {k: ledger["kept"][k] for k in sorted(ledger["kept"])},
              "drops": sorted(ledger["drops"], key=lambda d: (d["key"], d.get("item_id") or ""))}
    tmp = path.with_suffix(f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _drop_record_keys(d: dict[str, Any]) -> set[str]:
    return {d["key"], *(k for k in d.get("keys") or [] if isinstance(k, str) and k)}


def drop_filter(reviews_dir: Path, source_id: str) -> tuple[set[str], set[str]]:
    """What re-extraction must not re-suggest on this document: `(keys, item_ids)` — the folded
    title + alias keys each dropped tag had when dropped, and the dropped tags' ids (a renamed tag
    is still caught by the id its extracted name derives to). Raises :class:`LedgerCorrupt`."""
    keys: set[str] = set()
    ids: set[str] = set()
    for d in load_ledger(reviews_dir, source_id)["drops"]:
        keys |= _drop_record_keys(d)
        if isinstance(d.get("item_id"), str):
            ids.add(d["item_id"])
    return keys, ids


def _ledger_ids(reviews_dir: Path) -> list[str]:
    d = Path(reviews_dir) / LEDGER_DIR
    return [p.stem for p in sorted(d.glob("*.json")) if is_source_id(p.stem)] if d.is_dir() else []


def all_ledgers(reviews_dir: Path) -> list[dict[str, Any]]:
    """Every ledger; raises :class:`LedgerCorrupt` on the first damaged one (callers preflight with
    :func:`corrupt_ledgers`)."""
    return [load_ledger(reviews_dir, sid) for sid in _ledger_ids(reviews_dir)]


def corrupt_ledgers(reviews_dir: Path) -> list[dict[str, str]]:
    """`[{source_id, problem}]` for every ledger file that exists but does not parse."""
    out: list[dict[str, str]] = []
    for sid in _ledger_ids(reviews_dir):
        try:
            load_ledger(reviews_dir, sid)
        except LedgerCorrupt as exc:
            out.append({"source_id": sid, "problem": exc.problem})
    return out


def _audit(reviews_dir: Path, subject_id: str, event: str, record: dict[str, Any], now: str) -> None:
    audit = Path(reviews_dir) / "audit_log"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / f"{subject_id}-{event}-{uuid.uuid4().hex[:8]}.json").write_text(json.dumps(
        {"event": event, "actor": "human", "at": now, **record},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --- the blocked-tag list --------------------------------------------------------


def blocklist_path(policies_dir: Path) -> Path:
    return Path(policies_dir) / BLOCKLIST_NAME


def load_blocked(policies_dir: Path) -> dict[str, str]:
    """`{key: name}` for every blocked name (first spelling wins)."""
    path = blocklist_path(policies_dir)
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        name = line.split("#", 1)[0].strip()
        key = tag_key(name)
        if key and key not in out:
            out[key] = name
    return out


def _write_blocklist(policies_dir: Path, lines: list[str]) -> None:
    path = blocklist_path(policies_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _add_blocked(policies_dir: Path, name: str) -> bool:
    key = tag_key(name)
    if not key or key in load_blocked(policies_dir):
        return False
    path = blocklist_path(policies_dir)
    lines = (path.read_text(encoding="utf-8").splitlines() if path.exists()
             else _BLOCKLIST_HEADER.rstrip("\n").splitlines())
    _write_blocklist(policies_dir, [*lines, items._WS.sub(" ", name).strip()])
    return True


# --- shared helpers --------------------------------------------------------------


def _node_meta(wiki_dir: Path, node: dict[str, Any]) -> dict[str, Any] | None:
    node_dir = NODE_DIR[node.get("node_type") or "item"]
    return items._read_node_meta(Path(wiki_dir) / node_dir / f"{node['slug']}.md")


def _node_keys(wiki_dir: Path, node: dict[str, Any]) -> set[str]:
    meta = _node_meta(wiki_dir, node) or {}
    return {k for k in (tag_key(x) for x in [meta.get("title"), *(meta.get("aliases") or [])]) if k}


def implied_status(gconn, node_id: str, current: str) -> str:
    """The lifecycle status ADR-0066 implies for a live tag after its links changed: an active or
    unused tag is `active` while ≥1 document keeps it and `unused` otherwise; anything else is
    returned unchanged. A pre-ADR-0066 promotion (never kept/dropped through the ledger, so no
    human mention in any status) is not demoted by the recompute."""
    if current not in ("active", "unused"):
        return current
    if graph.kept_sources_for_node(gconn, node_id):
        return "active"
    if graph.is_legacy_tag(gconn, node_id, current):
        return "active"
    return "unused"


def _refresh_page(gconn, *, node_id: str, wiki_dir: Path, reviews_dir: Path, now: str) -> str:
    """Re-render a tag's page after its links changed, at the status ADR-0066 implies:
    active/unused follow the kept count; a candidate re-derives (candidate while mentioned)."""
    node = graph.get_node(gconn, node_id)
    if node is None:
        return "missing"
    if node["status"] in ("active", "unused"):
        status = implied_status(gconn, node_id, node["status"])
        return items.recompose_semantic_node_page(gconn, node_id=node_id, wiki_dir=wiki_dir,
                                                  status=status, review_status="none", now=now)
    return items._recompose_node(gconn, node_id=node_id, wiki_dir=wiki_dir,
                                 reviews_dir=reviews_dir, now=now)


PROJECTED = frozenset({"written", "unchanged", "removed", "merged"})


def ledger_keeps(reviews_dir: Path) -> tuple[dict[str, set[str]], set[str]]:
    """`({node_id: {source ids whose ledger keeps it}}, {source ids whose ledger is damaged})`."""
    keeps: dict[str, set[str]] = {}
    damaged: set[str] = set()
    for sid in _ledger_ids(reviews_dir):
        try:
            ledger = load_ledger(reviews_dir, sid)
        except LedgerCorrupt:
            damaged.add(sid)
            continue
        for nid in ledger["kept"]:
            keeps.setdefault(nid, set()).add(sid)
    return keeps, damaged


def _deletion_audited(reviews_dir: Path, node_id: str) -> bool:
    d = Path(reviews_dir) / "audit_log"
    return d.is_dir() and any(d.glob(f"{node_id}-tag-deleted-*.json"))


def project_page(gconn, *, node_id: str, wiki_dir: Path, reviews_dir: Path,
                 keeps: dict[str, set[str]], damaged: set[str], now: str) -> str:
    """Publication's tag-page refresh (ADR-0068 decision 4): re-render the lists from current
    authority and copy every saved field from the page — status, review_status, confidence,
    description, split lineage, aliases, type. It never files or reconciles a review, never tombstones
    and never promotes a candidate (an interrupted first keep is the tag-link repair's).

    The one exception to copying the saved status: an `active`/`unused` tag is reconciled between those
    two from the saved keep/drop decisions (`keeps`, from :func:`ledger_keeps`), the ADR-0066 rule.

    Returns an outcome in :data:`PROJECTED` (`removed`: a deletion is audited; `merged`: the redirect
    page is preserved as it is), or why the page stays pending: `node_missing` (no evidence the tag was
    removed), `deletion_unfinished`, `page_missing` (restore the page from backup — its title and
    description live only there), `identity_operation_in_flight`, `ledger_corrupt`,
    `tag_links_repair_needed` (the ledgers and the graph disagree) or `status_disagrees`."""
    node = _item_node(gconn, node_id)
    if node is None:
        if _deletion_audited(reviews_dir, node_id):
            return "removed"
        return "deletion_unfinished" if node_id in deletion_intents(reviews_dir) else "node_missing"
    meta = _node_meta(wiki_dir, node)
    if meta is None or not taxonomy.is_item_type(meta.get("item_type") or node.get("item_type")):
        return "page_missing"
    if node["status"] == "merged":
        return "merged"
    if identity_journal.operation_for(reviews_dir, [node_id]) is not None:
        return "identity_operation_in_flight"
    status = node["status"]
    if meta["status"] in ("active", "unused") and status in ("active", "unused"):
        linked = {r["src_id"] for r in gconn.execute(
            "SELECT DISTINCT src_id FROM edges WHERE dst_id = ? AND edge_type = 'mentions'",
            (node_id,))}
        if linked & damaged:
            return "ledger_corrupt"
        if keeps.get(node_id, set()) != set(graph.kept_sources_for_node(gconn, node_id)):
            return "tag_links_repair_needed"
        status = implied_status(gconn, node_id, status)
    elif meta["status"] != status:
        return "status_disagrees"
    review_status = meta.get("review_status")
    return items.recompose_semantic_node_page(
        gconn, node_id=node_id, wiki_dir=Path(wiki_dir), status=status,
        review_status=review_status if review_status in REVIEW_STATUSES else None, now=now)


def _active_mention_edges(gconn, source_id: str, node_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in gconn.execute(
        "SELECT * FROM edges WHERE src_id = ? AND dst_id = ? AND edge_type = 'mentions' "
        "AND status = 'active' ORDER BY edge_id", (source_id, node_id))]


def _source_ok(gconn, source_id: str) -> bool:
    node = graph.get_node(gconn, source_id) if is_source_id(source_id) else None
    return node is not None and node.get("node_type") == "source"


def _item_node(gconn, node_id: str) -> dict[str, Any] | None:
    node = graph.get_node(gconn, node_id) if isinstance(node_id, str) else None
    return node if node is not None and node.get("node_type") == "item" else None


# --- keep / drop -------------------------------------------------------------------


@ledger_action
def keep(gconn, *, source_id: str, node_id: str, wiki_dir: Path, reviews_dir: Path,
         amendments: dict[str, Any] | None = None, now: str | None = None) -> dict[str, Any]:
    """Keep the document → tag link (ADR-0066 decision 1/2).

    `amendments` (title/aliases/description/item_type) apply only when the keep activates a
    CANDIDATE (approve-with-amendments, ADR-0058/0059); an unclassified suggestion needs a real
    `item_type` (the sentinel gate — blocked otherwise). Outcomes: `kept` (+ `activated` /
    `revived`), `already_kept`, or `blocked` (nothing written)."""
    now = now or iso_now()
    wiki_dir, reviews_dir = Path(wiki_dir), Path(reviews_dir)
    if not _source_ok(gconn, source_id):
        return _blocked("unknown_source", source_id=source_id)
    node = _item_node(gconn, node_id)
    if node is None:
        return _blocked("unknown_item", node_id=node_id)
    if node["status"] not in _LINKABLE_STATUSES:
        return _blocked("item_not_linkable", node_id=node_id, node_status=node["status"])
    if (busy := _in_flight(reviews_dir, [node_id])) is not None:
        return busy
    try:
        amendments = promote.normalize_amendments(amendments)     # the shared ADR-0058 rules
    except promote.AmendmentError as exc:
        return _blocked("invalid_amendments", detail=str(exc))
    if node["status"] == "candidate":
        pre = promote.activate_node(gconn, node=node, wiki_dir=wiki_dir, reviews_dir=reviews_dir,
                                    amendments=amendments, now=now, dry_run=True)
        if pre["outcome"] == "skipped":
            return _blocked(pre["reason"], node_id=node_id)
    elif not taxonomy.is_production_item_type(node.get("item_type")):
        return _blocked("missing_required_item_type", node_id=node_id)

    ledger = load_ledger(reviews_dir, source_id)
    was_kept = node_id in ledger["kept"] and bool(
        [e for e in _active_mention_edges(gconn, source_id, node_id) if e["asserted_by"] == "human"])
    # The keep records the activation intent (amendments) so an interrupted first keep can be
    # completed by `repair` with the human's title/type, never re-derived.
    entry: dict[str, Any] = {"at": now, "by": "human"}
    if amendments and node["status"] == "candidate":
        entry["amendments"] = amendments
    ledger["kept"][node_id] = entry
    keys = _node_keys(wiki_dir, node)
    ledger["drops"] = [d for d in ledger["drops"]
                       if d.get("item_id") != node_id and d["key"] not in keys]
    # A legacy tag's active mentions all project as curated links; its first keep ends that, so
    # every document suggesting it re-renders (its link becomes a suggestion) — review round 6.
    was_legacy = graph.is_legacy_tag(gconn, node_id, node["status"])
    _save_ledger(reviews_dir, ledger)
    graph.upsert_assertion(gconn, src_id=source_id, dst_id=node_id, edge_type="mentions",
                           asserted_by="human", status="active", now=now)

    affected = {source_id}
    if was_legacy:
        affected.update(graph.sources_for_node(gconn, node_id))
    effect = "already_kept" if was_kept else "kept"
    if node["status"] == "candidate":
        out = promote.activate_node(gconn, node=node, wiki_dir=wiki_dir, reviews_dir=reviews_dir,
                                    amendments=amendments, now=now)
        affected.update(out.get("affected_sources") or [])
        effect = "activated"
    elif node["status"] == "unused":
        _refresh_page(gconn, node_id=node_id, wiki_dir=wiki_dir, reviews_dir=reviews_dir, now=now)
        effect = "revived"
    else:
        _refresh_page(gconn, node_id=node_id, wiki_dir=wiki_dir, reviews_dir=reviews_dir, now=now)
    if not was_kept:
        _audit(reviews_dir, node_id, "tag-kept", {"source_id": source_id, "node_id": node_id,
                                                   "effect": effect, "amendments": amendments}, now)
    return {"outcome": "kept" if effect != "already_kept" else "already_kept", "effect": effect,
            "node_id": node_id, "affected_sources": sorted(affected)}


@ledger_action
def drop(gconn, *, source_id: str, node_id: str, wiki_dir: Path, reviews_dir: Path,
         now: str | None = None) -> dict[str, Any]:
    """Drop the document → tag link (ADR-0066 decision 1/3/4). Outcomes: `dropped` (with `effect`
    `deleted` / `unused` / `unlinked`) or `blocked`."""
    now = now or iso_now()
    wiki_dir, reviews_dir = Path(wiki_dir), Path(reviews_dir)
    if not _source_ok(gconn, source_id):
        return _blocked("unknown_source", source_id=source_id)
    node = _item_node(gconn, node_id)
    if node is None:
        return _blocked("unknown_item", node_id=node_id)
    if node["status"] not in _LINKABLE_STATUSES:
        return _blocked("item_not_linkable", node_id=node_id, node_status=node["status"])
    if (busy := _in_flight(reviews_dir, [node_id])) is not None:
        return busy
    meta = _node_meta(wiki_dir, node) or {}
    title = meta.get("title") or node["slug"]

    # The ledger (the authority) is written first, then its graph mirror: an interrupted drop
    # leaves a ledger the repair (`rebuild_tag_links.py`) can re-apply to the graph.
    ledger = load_ledger(reviews_dir, source_id)
    ledger["kept"].pop(node_id, None)
    key = tag_key(title) or tag_key(node["slug"])
    # The record carries every name the tag answered to when dropped (title + aliases) and its id,
    # so a later rename or a re-extraction under the original wording is still filtered.
    keys = sorted((_node_keys(wiki_dir, node) | {key}) - {""})
    ledger["drops"] = [d for d in ledger["drops"] if d["key"] != key and d.get("item_id") != node_id]
    ledger["drops"].append({"key": key, "keys": keys, "name": title, "item_id": node_id, "at": now})
    _save_ledger(reviews_dir, ledger)
    for e in _active_mention_edges(gconn, source_id, node_id):
        graph.set_status(gconn, e["edge_id"], "rejected", now=now)

    effect = "unlinked"
    if node["status"] == "candidate" and not graph.sources_for_node(gconn, node_id):
        delete_tag(gconn, node_id=node_id, wiki_dir=wiki_dir, reviews_dir=reviews_dir,
                   reason="dropped_never_kept", now=now)
        effect = "deleted"
    else:
        _refresh_page(gconn, node_id=node_id, wiki_dir=wiki_dir, reviews_dir=reviews_dir, now=now)
        if (graph.get_node(gconn, node_id) or {}).get("status") == "unused":
            effect = "unused"
    _audit(reviews_dir, node_id, "tag-dropped", {"source_id": source_id, "node_id": node_id,
                                                  "name": title, "effect": effect}, now)
    return {"outcome": "dropped", "effect": effect, "node_id": node_id,
            "affected_sources": [source_id]}


@ledger_action
def edit_aliases(gconn, *, source_id: str, node_id: str, aliases: list[str], wiki_dir: Path,
                 reviews_dir: Path, expected: list[str] | None = None,
                 now: str | None = None) -> dict[str, Any]:
    """Replace the aliases of a tag this document keeps (UAT round 8). After the first keep the
    aliases are the human's (re-extraction never adds one), so this is the only way to change them
    short of a merge. Only an in-use tag (active/unused) kept here; the shared amendment limits apply.
    `expected` (review round 9) is the list the editor was shown: if the page's aliases differ from it
    now (another document edited them since), the edit is refused — checked here, under the ledger
    lock, so a stale form can never undo a newer edit. Outcomes: `aliases_updated`, `unchanged`, or
    `blocked` (nothing written)."""
    now = now or iso_now()
    wiki_dir, reviews_dir = Path(wiki_dir), Path(reviews_dir)
    node = _item_node(gconn, node_id)
    if node is None:
        return _blocked("unknown_item", node_id=node_id)
    if node["status"] not in ("active", "unused"):
        return _blocked("item_not_in_use", node_id=node_id, node_status=node["status"])
    if (busy := _in_flight(reviews_dir, [node_id])) is not None:
        return busy
    if node_id not in load_ledger(reviews_dir, source_id)["kept"]:
        return _blocked("not_kept_here", node_id=node_id, source_id=source_id)
    try:
        new = (promote.normalize_amendments({"aliases": list(aliases)}) or {}).get("aliases", [])
    except promote.AmendmentError as exc:
        return _blocked("invalid_amendments", detail=str(exc))
    meta = _node_meta(wiki_dir, node)
    if meta is None:
        return _blocked("page_missing", node_id=node_id)
    old = [str(a) for a in meta.get("aliases") or []]
    if expected is not None and items._union(
            [str(a).strip() for a in expected if str(a).strip()], []) != items._union(old, []):
        return _blocked("aliases_changed_since_shown", node_id=node_id, current=old)
    new = items._union(new, [])
    if new == items._union(old, []):
        return {"outcome": "unchanged", "node_id": node_id, "affected_sources": []}
    out = items.recompose_semantic_node_page(
        gconn, node_id=node_id, wiki_dir=wiki_dir, status=implied_status(gconn, node_id, node["status"]),
        review_status="none", now=now, aliases=new)
    if out not in ("written", "unchanged"):
        return _blocked(out, node_id=node_id)
    _audit(reviews_dir, node_id, "tag-aliases-edited", {"source_id": source_id, "node_id": node_id,
                                                        "from": old, "to": new}, now)
    return {"outcome": "aliases_updated", "effect": "aliases updated", "node_id": node_id,
            "aliases": new, "affected_sources": [source_id]}


@ledger_action
def undo_drop(gconn, *, source_id: str, key: str, wiki_dir: Path, reviews_dir: Path,
              now: str | None = None) -> dict[str, Any]:
    """Remove a drop record. If the tag still exists, its rejected suggestion on this document is
    restored as a suggestion (never as a keep); otherwise the next re-extraction may suggest it."""
    now = now or iso_now()
    reviews_dir = Path(reviews_dir)
    if not is_source_id(source_id):
        return _blocked("unknown_source", source_id=source_id)
    ledger = load_ledger(reviews_dir, source_id)
    record = next((d for d in ledger["drops"] if d["key"] == key), None)
    if record is None:
        return _blocked("no_such_drop", key=key)
    if (busy := _in_flight(reviews_dir, [record.get("item_id")])) is not None:
        return busy
    ledger["drops"] = [d for d in ledger["drops"] if d["key"] != key]
    _save_ledger(reviews_dir, ledger)
    restored = False
    nid = record.get("item_id")
    node = _item_node(gconn, nid) if nid else None
    if node is not None and node["status"] in _LINKABLE_STATUSES:
        for r in gconn.execute(
                "SELECT edge_id FROM edges WHERE src_id = ? AND dst_id = ? AND edge_type = 'mentions' "
                "AND asserted_by = 'llm' AND status = 'rejected'", (source_id, nid)).fetchall():
            graph.set_status(gconn, r["edge_id"], "active", now=now)
            restored = True
        if restored:
            _refresh_page(gconn, node_id=nid, wiki_dir=Path(wiki_dir), reviews_dir=reviews_dir,
                          now=now)
    _audit(reviews_dir, nid or source_id, "tag-drop-undone",
           {"source_id": source_id, "key": key, "name": record.get("name"),
            "suggestion_restored": restored}, now)
    return {"outcome": "undone", "restored": restored, "affected_sources": [source_id]}


RESOLVING_DIR = "resolving"                               # under reviews/tag_links/


def _resolving_path(reviews_dir: Path, source_id: str, node_id: str) -> Path:
    if not is_source_id(source_id) or not _ITEM_ID_RE.fullmatch(str(node_id)):
        raise ValueError("non-canonical id")              # never build a path from a forged id
    return Path(reviews_dir) / LEDGER_DIR / RESOLVING_DIR / source_id / f"{node_id}.json"


def pending_resolutions(reviews_dir: Path, source_id: str | None = None) -> list[dict[str, Any]]:
    """The unfinished "use X" operations (part-2 review): `[{source_id, node_id, name, chosen,
    chosen_title, op_id, at}]`, for one document or all. An unreadable record comes back with
    `chosen: None` (it can only be cancelled). Nothing resumes them — the document screen shows
    them until the user retries or cancels."""
    base = Path(reviews_dir) / LEDGER_DIR / RESOLVING_DIR
    dirs = [base / source_id] if source_id else (sorted(base.iterdir()) if base.is_dir() else [])
    out: list[dict[str, Any]] = []
    for d in dirs:
        if not d.is_dir() or not is_source_id(d.name):
            continue
        for path in sorted(d.glob("*.json")):
            if not _ITEM_ID_RE.fullmatch(path.stem):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                data = None
            ok = isinstance(data, dict) and _ITEM_ID_RE.fullmatch(str(data.get("chosen") or ""))
            out.append({**(data if ok else {}), "source_id": d.name, "node_id": path.stem,
                        **({} if ok else {"chosen": None})})
    return out


def _write_resolving(reviews_dir: Path, record: dict[str, Any]) -> None:
    path = _resolving_path(reviews_dir, record["source_id"], record["node_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _end_resolving(reviews_dir: Path, source_id: str, node_id: str) -> None:
    path = _resolving_path(reviews_dir, source_id, node_id)
    path.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        path.parent.rmdir()                               # only when it was the document's last


def _audited(reviews_dir: Path, subject_id: str, event: str, op_id: str) -> bool:
    d = Path(reviews_dir) / "audit_log"
    for path in sorted(d.glob(f"{subject_id}-{event}-*.json")) if d.is_dir() else []:
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("op_id") == op_id:
                return True
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            continue
    return False


def _kept_here(gconn, reviews_dir: Path, source_id: str, node_id: str) -> bool:
    return node_id in load_ledger(reviews_dir, source_id)["kept"] and any(
        e["asserted_by"] == "human" for e in _active_mention_edges(gconn, source_id, node_id))


@ledger_action
def use_existing(gconn, *, source_id: str, node_id: str, chosen: str, wiki_dir: Path,
                 reviews_dir: Path, now: str | None = None) -> dict[str, Any]:
    """Resolve an ambiguous suggestion to the existing tag the user chose (ADR-0066 P3 "Use X").

    Keeps `chosen` on this document (a ledger keep + human link, like a human-add of an existing
    tag), then drops the suggestion — its drop record carries the suggested name, so re-extraction
    filters it — and writes one `tag-resolved` audit entry. The keep runs first: keep removes drop
    records matching the kept tag's names, which the suggestion's record usually shares.

    Part-2 review: a **pending record** (`reviews/tag_links/resolving/<source>/<suggestion>.json`)
    is written before the first change and removed after the last. An interrupted operation stays
    visible on the document screen until the user retries (this call again with the same `chosen`:
    every step that already happened is skipped, so nothing is decided or audited twice) or changes
    the decision (:func:`cancel_resolutions`). Repair reports it and never resumes it."""
    now = now or iso_now()
    wiki_dir, reviews_dir = Path(wiki_dir), Path(reviews_dir)
    if not _source_ok(gconn, source_id):
        return _blocked("unknown_source", source_id=source_id)
    if not _ITEM_ID_RE.fullmatch(str(node_id)) or not _ITEM_ID_RE.fullmatch(str(chosen)) \
            or chosen == node_id:
        return _blocked("unknown_item", node_id=node_id, chosen=chosen)
    pending = next((r for r in pending_resolutions(reviews_dir, source_id)
                    if r["node_id"] == node_id), None)
    resume = pending is not None and pending.get("chosen") == chosen
    suggestion, target = _item_node(gconn, node_id), _item_node(gconn, chosen)
    if target is None:
        return _blocked("unknown_item", node_id=node_id, chosen=chosen)
    if target["status"] not in ("active", "unused"):
        return _blocked("chosen_not_a_tag", chosen=chosen, node_status=target["status"])
    if not resume:                                        # a new operation needs a live suggestion
        if suggestion is None:
            return _blocked("unknown_item", node_id=node_id, chosen=chosen)
        if suggestion["status"] not in _LINKABLE_STATUSES:
            return _blocked("item_not_linkable", node_id=node_id, node_status=suggestion["status"])
    if (busy := _in_flight(reviews_dir, [node_id, chosen])) is not None:
        return busy
    cancelled: list[dict[str, Any]] = []
    if resume:
        record = pending
    else:
        if pending is not None:                           # a different choice replaces the old one
            cancelled = cancel_resolutions(gconn, source_id=source_id, node_ids=[node_id],
                                           reason="use_other", wiki_dir=wiki_dir,
                                           reviews_dir=reviews_dir, now=now)["cancelled"]
        record = {"source_id": source_id, "node_id": node_id,
                  "name": (_node_meta(wiki_dir, suggestion) or {}).get("title") or suggestion["slug"],
                  "chosen": chosen, "chosen_title": (_node_meta(wiki_dir, target) or {}).get("title"),
                  "chosen_was_kept": _kept_here(gconn, reviews_dir, source_id, chosen),
                  "op_id": uuid.uuid4().hex, "at": now}
        _write_resolving(reviews_dir, record)
    affected: set[str] = {source_id}
    effect = "already_kept"
    if not _kept_here(gconn, reviews_dir, source_id, chosen):
        kept = keep(gconn, source_id=source_id, node_id=chosen, wiki_dir=wiki_dir,
                    reviews_dir=reviews_dir, now=now)
        if kept["outcome"] == "blocked":
            return kept | {"pending": True}
        effect = kept["effect"]
        affected.update(kept["affected_sources"])
    suggestion = _item_node(gconn, node_id)
    dropped_here = any(d.get("item_id") == node_id
                       for d in load_ledger(reviews_dir, source_id)["drops"])
    suggestion_effect = "already_dropped"
    if suggestion is not None and not dropped_here:
        dropped = drop(gconn, source_id=source_id, node_id=node_id, wiki_dir=wiki_dir,
                       reviews_dir=reviews_dir, now=now)
        if dropped["outcome"] == "blocked":
            return dropped | {"pending": True}
        suggestion_effect = dropped["effect"]
        affected.update(dropped["affected_sources"])
    if not _audited(reviews_dir, chosen, "tag-resolved", record["op_id"]):
        _audit(reviews_dir, chosen, "tag-resolved",
               {"source_id": source_id, "suggestion": {"node_id": node_id, "name": record.get("name")},
                "chosen": chosen, "suggestion_effect": suggestion_effect, "op_id": record["op_id"],
                "resumed": resume}, now)
    _end_resolving(reviews_dir, source_id, node_id)
    return {"outcome": "resolved", "effect": effect, "node_id": node_id, "chosen": chosen,
            "resumed": resume, "cancelled": cancelled, "affected_sources": sorted(affected)}


@ledger_action
def cancel_resolutions(gconn, *, source_id: str, node_ids: Any, reason: str, wiki_dir: Path,
                       reviews_dir: Path, now: str | None = None) -> dict[str, Any]:
    """Cancel this document's unfinished "use X" operations that involve any of `node_ids` (as the
    suggestion or as the chosen tag) — the user decided differently. Whatever already happened
    stays: a keep of X that ran is an ordinary kept tag, which the result names (`retained_keep`) so
    the screen can tell the user to drop it if unwanted. Audited `tag-resolve-cancelled`."""
    now = now or iso_now()
    reviews_dir = Path(reviews_dir)
    ids = {str(n) for n in node_ids}
    cancelled: list[dict[str, Any]] = []
    for r in pending_resolutions(reviews_dir, source_id):
        if r["node_id"] not in ids and r.get("chosen") not in ids:
            continue
        chosen = r.get("chosen")
        retained = (bool(chosen) and not r.get("chosen_was_kept")
                    and _kept_here(gconn, reviews_dir, source_id, chosen))
        _audit(reviews_dir, chosen or r["node_id"], "tag-resolve-cancelled",
               {"source_id": source_id, "suggestion": {"node_id": r["node_id"], "name": r.get("name")},
                "chosen": chosen, "op_id": r.get("op_id"), "reason": reason,
                "retained_keep": retained}, now)
        _end_resolving(reviews_dir, source_id, r["node_id"])
        cancelled.append({"node_id": r["node_id"], "name": r.get("name"), "chosen": chosen,
                          "chosen_title": r.get("chosen_title"), "retained_keep": retained})
    return {"outcome": "cancelled", "cancelled": cancelled, "affected_sources": [source_id]}


# --- deletion + the unused-tags list ----------------------------------------------------


def merge_redirect_targets(wiki_dir: Path) -> set[str]:
    """Node ids some `merged` tombstone points at (protected from Delete unused, ADR-0066 decision 4)."""
    out: set[str] = set()
    d = Path(wiki_dir) / NODE_DIR["item"]
    for path in sorted(d.glob("*.md")) if d.is_dir() else []:
        fm = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        if fm.get("status") == "merged" and isinstance(fm.get("merged_into"), str):
            out.add(fm["merged_into"])
    return out


def _has_synthesis(gconn, node_id: str) -> bool:
    from app.backend import review_read                  # lazy: review_read imports workers
    return graph.get_node(gconn, review_read._synthesis_id(node_id)) is not None


DELETION_DIR = "deleting"                                 # under reviews/tag_links/
_ITEM_ID_RE = re.compile(r"^itm_[0-9a-f]{16}$")


def _deletion_path(reviews_dir: Path, node_id: str) -> Path:
    if not _ITEM_ID_RE.fullmatch(str(node_id)):
        raise ValueError("non-canonical item id")          # never build a path from a forged id
    return Path(reviews_dir) / LEDGER_DIR / DELETION_DIR / f"{node_id}.json"


def deletion_intents(reviews_dir: Path) -> dict[str, dict[str, Any]]:
    """The deletion-intent records of deletions that started and did not finish (review round 7):
    `{node_id: {node_id, reason, slug, at}}`. An unreadable record is returned with `reason: None`."""
    d = Path(reviews_dir) / LEDGER_DIR / DELETION_DIR
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(d.glob("*.json")) if d.is_dir() else []:
        if not _ITEM_ID_RE.fullmatch(path.stem):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            data = None
        out[path.stem] = data if isinstance(data, dict) else {"node_id": path.stem, "reason": None}
    return out


def _end_deletion(reviews_dir: Path, node_id: str) -> None:
    _deletion_path(reviews_dir, node_id).unlink(missing_ok=True)


def deletion_blocker(gconn, wiki_dir: Path, reviews_dir: Path, node_id: str) -> str | None:
    """Why a tag may NOT be deleted now (None = deletable): kept by some document, a merge-redirect
    target, owner of a synthesis page, or still live (an active tag, a candidate with suggestions)."""
    node = _item_node(gconn, node_id)
    if node is None:
        return "missing"
    if any(node_id in lg["kept"] for lg in _readable_ledgers(reviews_dir)):
        return "kept"
    if node_id in merge_redirect_targets(wiki_dir):
        return "merge_redirect_target"
    if _has_synthesis(gconn, node_id):
        return "has_synthesis"
    if node["status"] == "unused" or (node["status"] == "candidate"
                                      and not graph.sources_for_node(gconn, node_id)):
        return None
    return f"status_{node['status']}"


def delete_tag(gconn, *, node_id: str, wiki_dir: Path, reviews_dir: Path, reason: str,
               now: str | None = None) -> dict[str, Any]:
    """Hard-delete a tag nobody keeps: page, node, every edge; withdraw its unresolved review items;
    re-render `duplicates` partners; audit. The caller has checked eligibility.

    Review round 7: a durable **deletion-intent record** (`reviews/tag_links/deleting/<id>.json`) is
    written before the first change and removed after the last, so `repair` resumes only a deletion a
    human action started — a page that is merely missing is reported, never treated as approval."""
    now = now or iso_now()
    wiki_dir, reviews_dir = Path(wiki_dir), Path(reviews_dir)
    node = _item_node(gconn, node_id)
    if node is None:
        return _blocked("unknown_item", node_id=node_id)
    page_rel = f"{NODE_DIR['item']}/{node['slug']}.md"
    partners = [d["node_id"] for d in graph.active_duplicates(gconn, node_id)]
    mentioning = sorted({r["src_id"] for r in gconn.execute(
        "SELECT DISTINCT src_id FROM edges WHERE dst_id = ? AND edge_type = 'mentions' "
        "AND status = 'active'", (node_id,))})
    meta = _node_meta(wiki_dir, node) or {}
    intent = _deletion_path(reviews_dir, node_id)
    intent.parent.mkdir(parents=True, exist_ok=True)
    tmp = intent.with_suffix(f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps({"node_id": node_id, "reason": reason, "slug": node["slug"],
                               "title": meta.get("title"), "at": now}, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, intent)
    (wiki_dir / page_rel).unlink(missing_ok=True)
    edges_removed = graph.delete_node(gconn, node_id)
    withdrawn = merges._withdraw_b_subjects(reviews_dir, node_id, page_rel, now, reason="tag_deleted")
    for pid in partners:
        if _item_node(gconn, pid) is not None:
            _refresh_page(gconn, node_id=pid, wiki_dir=wiki_dir, reviews_dir=reviews_dir, now=now)
    _audit(reviews_dir, node_id, "tag-deleted", {
        "node_id": node_id, "title": meta.get("title"), "item_type": node.get("item_type"),
        "status": node["status"], "reason": reason, "page": page_rel,
        "edges_removed": edges_removed, "withdrawn": withdrawn}, now)
    _end_deletion(reviews_dir, node_id)
    return {"outcome": "deleted", "node_id": node_id, "affected_sources": mentioning}


def unused_tags(gconn, wiki_dir: Path) -> list[dict[str, Any]]:
    """The Unused tags list: every `unused` item with its title/type and why it is protected (if)."""
    protected = merge_redirect_targets(wiki_dir)
    out = []
    for node in graph.nodes_of_type(gconn, "item"):
        if node["status"] != "unused":
            continue
        meta = _node_meta(wiki_dir, node) or {}
        reason = ("merge_redirect_target" if node["node_id"] in protected
                  else "has_synthesis" if _has_synthesis(gconn, node["node_id"]) else None)
        out.append({"node_id": node["node_id"], "title": meta.get("title") or node["slug"],
                    "item_type": node.get("item_type"), "slug": node["slug"],
                    "protected": reason})
    return sorted(out, key=lambda r: (str(r["title"]).lower(), r["node_id"]))


@ledger_action
def delete_unused(gconn, *, wiki_dir: Path, reviews_dir: Path,
                  now: str | None = None) -> dict[str, Any]:
    """Bulk **Delete unused** (the explicit human deletion step, ADR-0066 decision 4). Skips
    merge-redirect targets and tags that own a synthesis page."""
    now = now or iso_now()
    deleted: list[str] = []
    skipped: list[dict[str, str]] = []
    affected: set[str] = set()
    for row in unused_tags(gconn, wiki_dir):
        if row["protected"]:
            skipped.append({"node_id": row["node_id"], "reason": row["protected"]})
            continue
        if _in_flight(reviews_dir, [row["node_id"]]) is not None:
            skipped.append({"node_id": row["node_id"], "reason": "identity_operation_in_flight"})
            continue
        out = delete_tag(gconn, node_id=row["node_id"], wiki_dir=wiki_dir, reviews_dir=reviews_dir,
                         reason="delete_unused", now=now)
        deleted.append(row["node_id"])
        affected.update(out.get("affected_sources") or [])
    return {"deleted": deleted, "skipped": skipped, "affected_sources": sorted(affected)}


# --- block / unblock ---------------------------------------------------------------------


@ledger_action
def block(gconn, *, name: str, policies_dir: Path, wiki_dir: Path, reviews_dir: Path,
          now: str | None = None) -> dict[str, Any]:
    """Never suggest `name` (ADR-0066 decision 5): add it to the blocked list and withdraw its
    pending suggestions everywhere (no drop records). Kept links are untouched."""
    now = now or iso_now()
    wiki_dir, reviews_dir = Path(wiki_dir), Path(reviews_dir)
    name = items._WS.sub(" ", str(name or "")).strip()[:items._MAX_NAME]
    key = tag_key(name)
    if not key:
        return _blocked("invalid_name")
    matching = [n["node_id"] for n in graph.nodes_of_type(gconn, "item")
                if n["status"] in _LINKABLE_STATUSES and key in _node_keys(wiki_dir, n)]
    if (busy := _in_flight(reviews_dir, matching)) is not None:
        return busy                                       # nothing written, not even the list entry
    added = _add_blocked(policies_dir, name)
    withdrawn: list[dict[str, str]] = []
    affected: set[str] = set()
    deleted: list[str] = []
    for node in graph.nodes_of_type(gconn, "item"):
        if node["status"] not in _LINKABLE_STATUSES or key not in _node_keys(wiki_dir, node):
            continue
        nid = node["node_id"]
        kept_here = set(graph.kept_sources_for_node(gconn, nid))
        for e in [dict(r) for r in gconn.execute(
                "SELECT * FROM edges WHERE dst_id = ? AND edge_type = 'mentions' AND asserted_by = 'llm' "
                "AND status = 'active' ORDER BY edge_id", (nid,))]:
            if e["src_id"] in kept_here:
                continue                                  # a kept link is never touched by a block
            graph.set_status(gconn, e["edge_id"], "superseded", now=now)
            withdrawn.append({"source_id": e["src_id"], "node_id": nid})
            affected.add(e["src_id"])
        if node["status"] == "candidate" and not graph.sources_for_node(gconn, nid):
            delete_tag(gconn, node_id=nid, wiki_dir=wiki_dir, reviews_dir=reviews_dir,
                       reason="blocked_never_kept", now=now)
            deleted.append(nid)
        elif any(w["node_id"] == nid for w in withdrawn):
            _refresh_page(gconn, node_id=nid, wiki_dir=wiki_dir, reviews_dir=reviews_dir, now=now)
    _audit(reviews_dir, f"blocked-{uuid.uuid4().hex[:8]}", "tag-blocked",
           {"name": name, "key": key, "added": added, "withdrawn": withdrawn, "deleted": deleted}, now)
    return {"outcome": "blocked_tag", "name": name, "key": key, "added": added,
            "withdrawn": len(withdrawn), "deleted": deleted, "affected_sources": sorted(affected)}


@ledger_action
def unblock(*, key: str, policies_dir: Path, enrichment_dir: Path, reviews_dir: Path,
            now: str | None = None) -> dict[str, Any]:
    """Remove a blocked name. Returns the documents whose stored extraction suggested it, for the
    cache-only Re-suggest (`scripts/extract_items.py --force --cache-only <source_id>…`)."""
    now = now or iso_now()
    blocked = load_blocked(policies_dir)
    if key not in blocked:
        return _blocked("not_blocked", key=key)
    path = blocklist_path(policies_dir)
    kept_lines = [line for line in path.read_text(encoding="utf-8").splitlines()
                  if tag_key(line.split("#", 1)[0].strip()) != key]
    _write_blocklist(policies_dir, kept_lines)
    resuggest = suggesting_sources(enrichment_dir, key)
    _audit(reviews_dir, f"unblocked-{uuid.uuid4().hex[:8]}", "tag-unblocked",
           {"name": blocked[key], "key": key, "resuggest_sources": resuggest}, now)
    return {"outcome": "unblocked", "name": blocked[key], "key": key,
            "resuggest_sources": resuggest}


def suggesting_sources(enrichment_dir: Path, key: str) -> list[str]:
    """Sources whose stored items artifact names `key` — as a suggestion, or as a name extraction
    filtered because it was blocked (`filtered[]`, so a document extracted while the tag was blocked
    is still found) — what an unblock can re-suggest."""
    out: list[str] = []
    base = Path(enrichment_dir)
    for path in sorted(base.glob("*.items.json")) if base.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sid = data.get("source_id") if isinstance(data, dict) else None
        if not (is_source_id(sid) and path.name == f"{sid}.items.json"):
            continue
        names = [n.get("name") for n in data.get("nodes") or [] if isinstance(n, dict)]
        blocked_rows = [f for f in data.get("filtered") or []
                        if isinstance(f, dict) and f.get("reason") == "blocked"]
        # A suggestion blocked through the tag it resolved to (ADR-0066 P6) is found by that tag.
        names += [f.get(k) for f in blocked_rows for k in ("name", "resolved_title")]
        if any(tag_key(n) == key for n in names):
            out.append(sid)
    return sorted(set(out))


# --- identity operations: keep the ledger in step (ADR-0066 decision 4) ---------------------------


def move_merge_decisions(reviews_dir: Path, *, survivor: str, absorbed: str) -> list[str]:
    """Ledger half of a merge B→A, run BEFORE any graph write (ledger first): move B's keeps and
    drop records to A per document; a document that keeps A loses any drop record for A (the keep
    wins). Idempotent (a resumed merge re-runs it). Returns the rewritten source ids. Raises
    :class:`LedgerCorrupt` (the merge preflights)."""
    changed: list[str] = []
    with ledger_lock(reviews_dir):
        for ledger in all_ledgers(reviews_dir):
            before = json.dumps(ledger, sort_keys=True)
            if absorbed in ledger["kept"]:
                ledger["kept"].setdefault(survivor, ledger["kept"][absorbed])
                ledger["kept"].pop(absorbed)
            for d in ledger["drops"]:
                if d.get("item_id") == absorbed:
                    d["item_id"] = survivor
            if survivor in ledger["kept"]:
                ledger["drops"] = [d for d in ledger["drops"] if d.get("item_id") != survivor]
            if json.dumps(ledger, sort_keys=True) != before:
                _save_ledger(reviews_dir, ledger)
                changed.append(ledger["source_id"])
    return changed


def enforce_drops(gconn, reviews_dir: Path, *, node_id: str, now: str | None = None) -> list[str]:
    """Graph half of the per-document precedence, independent of evidence anchors: on every document
    that dropped `node_id` and does not keep it, reject each suggestion (`llm` link) now on it — a
    re-pointed suggestion at another span, or the survivor's own where the absorbed tag was dropped.
    Returns the affected source ids."""
    now = now or iso_now()
    rejected: list[str] = []
    with ledger_lock(reviews_dir):
        for ledger in all_ledgers(reviews_dir):
            sid = ledger["source_id"]
            if node_id in ledger["kept"] or not any(d.get("item_id") == node_id
                                                    for d in ledger["drops"]):
                continue
            for e in _active_mention_edges(gconn, sid, node_id):
                if e["asserted_by"] == "llm":
                    graph.set_status(gconn, e["edge_id"], "rejected", now=now)
                    rejected.append(sid)
    return sorted(set(rejected))


def on_split(reviews_dir: Path, *, primary: str, spinoff: str, sources: list[str]) -> list[str]:
    """After `split_item` moved the listed documents' mentions A→B: those documents' keeps of A
    become keeps of B (each moved link keeps its decision). Raises :class:`LedgerCorrupt` (the
    split preflights)."""
    changed: list[str] = []
    with ledger_lock(reviews_dir):
        for sid in sources:
            if not is_source_id(sid):
                continue
            ledger = load_ledger(reviews_dir, sid)
            if primary in ledger["kept"]:
                ledger["kept"].setdefault(spinoff, ledger["kept"].pop(primary))
                _save_ledger(reviews_dir, ledger)
                changed.append(sid)
    return changed


# --- ledger ↔ graph consistency + repair ------------------------------------------------------


def _readable_ledgers(reviews_dir: Path) -> list[dict[str, Any]]:
    out = []
    for sid in _ledger_ids(reviews_dir):
        try:
            out.append(load_ledger(reviews_dir, sid))
        except LedgerCorrupt:
            continue                                      # reported by corrupt_ledgers
    return out


def kept_anywhere(reviews_dir: Path) -> set[str] | None:
    """Every tag some document's ledger keeps — the durable first-keep evidence (a keep is written to
    the ledger before anything else, so an interrupted first keep counts too). None when a ledger is
    damaged: it may hold a keep, so the caller must assume any tag could be kept. Call it under
    :func:`ledger_lock` for an answer no keep can overtake."""
    if corrupt_ledgers(reviews_dir):
        return None
    return {nid for lg in all_ledgers(reviews_dir) for nid in lg["kept"]}


_PLAN_KEYS = ("missing_keeps", "interrupted_activations", "interrupted_revivals", "undropped",
              "orphan_keeps", "invalid_keeps", "stale_status", "unwithdrawn_blocks",
              "interrupted_deletions", "cancelled_deletions", "missing_pages", "stale_item_pages",
              "stale_source_pages", "pending_resolutions")
# Reported, never written by repair: a human (or a restore from backup) resolves them. An unfinished
# "use X" is the user's to retry or cancel on the document screen — not an inconsistency.
_REPORT_ONLY = frozenset({"invalid_keeps", "missing_pages", "pending_resolutions"})
_SOURCE_LINK_RE = re.compile(r"\[\[Sources/(src_[0-9a-f]{16})[|\]]")


def _page_drift(wiki_dir: Path, gconn, node: dict[str, Any]) -> str | None:
    """Why a live tag's page disagrees with the graph (its status, or its Mentioned By links vs the
    documents that keep it), else None. A missing/unreadable page is not drift (see deletions)."""
    path = Path(wiki_dir) / NODE_DIR["item"] / f"{node['slug']}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    status = parse_frontmatter(text).get("status")
    if status != node["status"]:
        return f"page status {status!r} != graph status {node['status']!r}"
    kept = set(graph.tag_projection(gconn, node["node_id"], node["status"])[0])
    if set(_SOURCE_LINK_RE.findall(text)) != kept:
        return "Mentioned By does not list exactly the documents that keep it"
    return None


def _source_page_drift(wiki_dir: Path, gconn, sid: str) -> bool:
    """True when a Source page's `items:` tag list differs from the tags the document keeps."""
    path = Path(wiki_dir) / "Sources" / f"{sid}.md"
    try:
        listed = parse_frontmatter(path.read_text(encoding="utf-8")).get("items")
    except OSError:
        return False
    curated = {m["slug"] for m in graph.source_tag_projection(gconn, sid)[0] if m["node_type"] == "item"}
    return set(listed or []) != curated


def _plan_repair(gconn, reviews_dir: Path, wiki_dir: Path | None = None,
                 policies_dir: Path | None = None) -> dict[str, Any]:
    """What disagrees between the ledgers (the authority) and the graph mirror:
    `missing_keeps` (a keep with no active human link), `interrupted_activations` (a kept tag still a
    candidate — a first keep stopped before activating; resumed with the recorded amendments),
    `interrupted_revivals` (a kept tag still `unused`), `undropped` (an active link of a tag the
    document dropped and does not keep), `orphan_keeps` (an active human link with no keep — e.g. an
    interrupted drop) and `invalid_keeps` (a keep naming a gone/merged tag; not repairable).

    Review round 6 — the derived state every action must leave behind: `stale_status` (an active
    tag no ledger keeps, or an unused one a ledger keeps — e.g. a drop stopped before the status
    refresh), `unwithdrawn_blocks` (a blocked tag still suggested — a block stopped after writing
    the list), `stale_item_pages` / `stale_source_pages` (a page whose status or tag links disagree
    with the graph — a refresh that never ran).

    Review round 7 — deletion needs a recorded human intent, never an inference: an
    `interrupted_deletions` entry is a deletion-intent record (`deletion_intents`) whose tag is still
    deletable (`deletion_blocker`); `cancelled_deletions` is a record whose tag became kept/protected
    meanwhile (repair drops the record, deletes nothing); `missing_pages` is a live tag with no page
    and no record — report-only (restore the page from backup; it holds the title and aliases).

    Never touched: documents whose ledger is damaged (`skipped_damaged` — their links are left
    exactly as they are) and the nodes of an in-flight merge/split (`deferred` — run apply to
    finish it first; every node is deferred when a journal entry is unreadable)."""
    from app.workers import identity_journal              # lazy: keeps the import graph flat
    plan: dict[str, Any] = {k: [] for k in _PLAN_KEYS}
    damaged = {c["source_id"] for c in corrupt_ledgers(reviews_dir)}
    plan["skipped_damaged"] = sorted(damaged)
    ops = identity_journal.open_operations(reviews_dir)
    plan["deferred"] = [o["review_id"] for o in ops]
    plan["pending_resolutions"] = pending_resolutions(reviews_dir)
    all_deferred = any(o["nodes"] is None for o in ops)
    in_flight = {n for o in ops for n in (o["nodes"] or [])}

    def deferred(nid: str) -> bool:
        return all_deferred or nid in in_flight

    ledgers = {lg["source_id"]: lg for lg in _readable_ledgers(reviews_dir)}
    for sid, lg in sorted(ledgers.items()):
        if not _source_ok(gconn, sid):
            continue                                      # the document left the graph
        for nid in sorted(lg["kept"]):
            if deferred(nid):
                continue
            node = _item_node(gconn, nid)
            if node is not None and node["status"] == "hidden":
                continue                                  # a hide keeps its keeps (unhide restores)
            if node is None or node["status"] not in _LINKABLE_STATUSES:
                plan["invalid_keeps"].append({"source_id": sid, "node_id": nid,
                                              "node_status": (node or {}).get("status") or "missing"})
                continue
            if not [e for e in _active_mention_edges(gconn, sid, nid) if e["asserted_by"] == "human"]:
                plan["missing_keeps"].append({"source_id": sid, "node_id": nid})
            if node["status"] == "candidate":
                plan["interrupted_activations"].append(
                    {"source_id": sid, "node_id": nid,
                     "amendments": lg["kept"][nid].get("amendments") or None})
            elif node["status"] == "unused":
                plan["interrupted_revivals"].append({"source_id": sid, "node_id": nid})
        for d in lg["drops"]:
            nid = d.get("item_id")
            if isinstance(nid, str) and nid not in lg["kept"] and not deferred(nid):
                for e in _active_mention_edges(gconn, sid, nid):
                    plan["undropped"].append({"source_id": sid, "node_id": nid,
                                              "edge_id": e["edge_id"]})
    undropped = {x["edge_id"] for x in plan["undropped"]}
    for r in gconn.execute(
            "SELECT e.edge_id, e.src_id, e.dst_id FROM edges e JOIN nodes n ON n.node_id = e.dst_id "
            "WHERE e.edge_type = 'mentions' AND e.asserted_by = 'human' AND e.status = 'active' "
            "AND n.node_type = 'item' ORDER BY e.edge_id"):
        if r["src_id"] in damaged or deferred(r["dst_id"]) or r["edge_id"] in undropped:
            continue
        lg = ledgers.get(r["src_id"])
        if lg is None or r["dst_id"] not in lg["kept"]:
            plan["orphan_keeps"].append({"source_id": r["src_id"], "node_id": r["dst_id"],
                                         "edge_id": r["edge_id"]})
    _plan_derived_state(gconn, plan, ledgers, damaged, deferred,
                        Path(wiki_dir) if wiki_dir else Path(reviews_dir).parent / "wiki",
                        Path(policies_dir) if policies_dir else Path(reviews_dir).parent / "policies",
                        Path(reviews_dir))
    # one activation/revival per tag, however many documents keep it
    for k in ("interrupted_activations", "interrupted_revivals"):
        seen: set[str] = set()
        plan[k] = [x for x in plan[k] if not (x["node_id"] in seen or seen.add(x["node_id"]))]
    return plan


def _plan_derived_state(gconn, plan: dict[str, Any], ledgers: dict[str, Any], damaged: set[str],
                        deferred, wiki_dir: Path, policies_dir: Path, reviews_dir: Path) -> None:
    """Fill the review-round-6 categories (see `_plan_repair`). A tag linked from a document whose
    ledger is damaged is skipped: its keeps are unknown, so its lifecycle cannot be derived."""
    kept_by_ledger = {nid for lg in ledgers.values() for nid in lg["kept"]}
    blocked = set(load_blocked(policies_dir))
    intents = deletion_intents(reviews_dir)
    for nid, record in sorted(intents.items()):
        if deferred(nid) or (damaged and any(r["src_id"] in damaged for r in gconn.execute(
                "SELECT src_id FROM edges WHERE dst_id = ? AND edge_type = 'mentions'", (nid,)))):
            continue                                      # a damaged ledger may hold a keep of it
        why = deletion_blocker(gconn, wiki_dir, reviews_dir, nid)
        if why is None or why == "missing":               # still deletable / only the tail is left
            plan["interrupted_deletions"].append({"node_id": nid, "reason": record.get("reason"),
                                                  "slug": record.get("slug"),
                                                  "node_missing": why == "missing"})
        else:
            plan["cancelled_deletions"].append({"node_id": nid, "reason": why})
    revivals = {x["node_id"] for x in plan["interrupted_revivals"]}
    for node in graph.nodes_of_type(gconn, "item"):
        nid, status = node["node_id"], node["status"]
        if status not in _LINKABLE_STATUSES or deferred(nid):
            continue
        if damaged and any(r["src_id"] in damaged for r in gconn.execute(
                "SELECT src_id FROM edges WHERE dst_id = ? AND edge_type = 'mentions'", (nid,))):
            continue
        if status in ("active", "unused") and nid not in revivals:
            expected = ("active" if nid in kept_by_ledger or graph.is_legacy_tag(gconn, nid, status)
                        else "unused")
            if expected != status:
                plan["stale_status"].append({"node_id": nid, "status": status, "expected": expected})
        if blocked and _node_keys(wiki_dir, node) & blocked:
            kept_here = set(graph.kept_sources_for_node(gconn, nid))
            for r in gconn.execute(
                    "SELECT edge_id, src_id FROM edges WHERE dst_id = ? AND edge_type = 'mentions' "
                    "AND asserted_by = 'llm' AND status = 'active' ORDER BY edge_id", (nid,)):
                if r["src_id"] not in kept_here:
                    plan["unwithdrawn_blocks"].append({"source_id": r["src_id"], "node_id": nid,
                                                       "edge_id": r["edge_id"]})
        page = Path(wiki_dir) / NODE_DIR["item"] / f"{node['slug']}.md"
        if not page.exists():
            if nid not in intents:                        # a missing page is NOT a deletion approval
                plan["missing_pages"].append({"node_id": nid, "status": status, "slug": node["slug"]})
            continue
        if (why := _page_drift(wiki_dir, gconn, node)) is not None:
            plan["stale_item_pages"].append({"node_id": nid, "reason": why})
    for r in gconn.execute("SELECT node_id FROM nodes WHERE node_type = 'source' ORDER BY node_id"):
        sid = r["node_id"]
        if sid in damaged or any(deferred(m["dst_id"]) for m in graph.source_tag_links(gconn, sid)):
            continue                                      # an in-flight merge/split re-renders it
        if _source_page_drift(wiki_dir, gconn, sid):
            plan["stale_source_pages"].append({"source_id": sid})


def consistency_errors(gconn, reviews_dir: Path, *, wiki_dir: Path | None = None,
                       policies_dir: Path | None = None) -> list[str]:
    """Reasons the tag-decision ledgers and the graph disagree (empty == consistent)."""
    errors = [f"{c['source_id']}: tag ledger is damaged ({c['problem']}) — restore it from backup; "
              "its links are left untouched" for c in corrupt_ledgers(reviews_dir)]
    plan = _plan_repair(gconn, reviews_dir, wiki_dir, policies_dir)
    errors += [f"merge/split {rid} is in flight — run /reviews/apply to finish it before repairing"
               for rid in plan["deferred"]]
    hint = "run scripts/rebuild_tag_links.py"
    errors += [f"{x['source_id']}: kept tag {x['node_id']} has no active link in the graph ({hint})"
               for x in plan["missing_keeps"]]
    errors += [f"{x['source_id']}: kept tag {x['node_id']} was never activated — an interrupted "
               f"keep ({hint})" for x in plan["interrupted_activations"]]
    errors += [f"{x['source_id']}: kept tag {x['node_id']} is still unused — an interrupted "
               f"revival ({hint})" for x in plan["interrupted_revivals"]]
    errors += [f"{x['source_id']}: dropped tag {x['node_id']} still has an active link "
               f"{x['edge_id']} ({hint})" for x in plan["undropped"]]
    errors += [f"{x['source_id']}: human link {x['edge_id']} to {x['node_id']} has no keep in the "
               f"ledger ({hint})" for x in plan["orphan_keeps"]]
    errors += [f"{x['source_id']}: kept tag {x['node_id']} is {x['node_status']}"
               for x in plan["invalid_keeps"]]
    errors += [f"tag {x['node_id']} is {x['status']} but its keeps make it {x['expected']} — an "
               f"interrupted keep/drop ({hint})" for x in plan["stale_status"]]
    errors += [f"{x['source_id']}: blocked tag {x['node_id']} is still suggested (link "
               f"{x['edge_id']}) — an interrupted block ({hint})" for x in plan["unwithdrawn_blocks"]]
    errors += [f"tag {x['node_id']}: a deletion ({x['reason']}) was started and did not finish "
               f"({hint})" for x in plan["interrupted_deletions"]]
    errors += [f"tag {x['node_id']}: an unfinished deletion no longer applies ({x['reason']}) — its "
               f"record is stale ({hint})" for x in plan["cancelled_deletions"]]
    errors += [f"tag {x['node_id']} ({x['status']}) has no page wiki/Items/{x['slug']}.md — restore it "
               "from backup (repair never deletes a tag because its page is missing)"
               for x in plan["missing_pages"]]
    errors += [f"tag {x['node_id']}: {x['reason']} ({hint})" for x in plan["stale_item_pages"]]
    errors += [f"{x['source_id']}: Source page tag list is stale ({hint})"
               for x in plan["stale_source_pages"]]
    return errors


def repair(gconn, *, reviews_dir: Path, wiki_dir: Path, policies_dir: Path | None = None,
           apply: bool = False, now: str | None = None) -> dict[str, Any]:
    """Make the graph agree with the ledgers (the authority): re-assert missing keeps, finish
    interrupted activations (with the recorded amendments) and revivals, reject active links of
    dropped tags, and reject human links with no keep. Damaged ledgers' documents and in-flight
    merge/split nodes are never touched. Dry run unless `apply`. Returns the plan counts,
    `skipped_damaged`, `deferred`, `activation_blocked`, `affected_sources` and `pages_changed`
    (a tag page was re-rendered or deleted — the caller refreshes `index.md` + the keyword index
    even when no Source page is affected)."""
    now = now or iso_now()
    wiki_dir, reviews_dir = Path(wiki_dir), Path(reviews_dir)
    with ledger_lock(reviews_dir):
        plan = _plan_repair(gconn, reviews_dir, wiki_dir, policies_dir)
        summary: dict[str, Any] = {k: len(plan[k]) for k in _PLAN_KEYS}
        summary.update({"skipped_damaged": plan["skipped_damaged"], "deferred": plan["deferred"],
                        "activation_blocked": []})
        affected = {x["source_id"] for k in _PLAN_KEYS if k not in _REPORT_ONLY for x in plan[k]
                    if "source_id" in x}
        for k in ("stale_status", "stale_item_pages", "interrupted_deletions"):
            for x in plan[k]:                             # their documents' pages may list them
                affected.update(graph.sources_for_node(gconn, x["node_id"]))
        if not apply:
            return {"applied": False, **summary, "affected_sources": sorted(affected),
                    "pages_changed": False}
        nodes: set[str] = set()
        for x in plan["missing_keeps"]:
            graph.upsert_assertion(gconn, src_id=x["source_id"], dst_id=x["node_id"],
                                   edge_type="mentions", asserted_by="human", status="active", now=now)
            nodes.add(x["node_id"])
        for x in plan["undropped"] + plan["orphan_keeps"]:
            graph.set_status(gconn, x["edge_id"], "rejected", now=now)
            nodes.add(x["node_id"])
        for x in plan["interrupted_activations"]:
            node = _item_node(gconn, x["node_id"])
            out = promote.activate_node(gconn, node=node, wiki_dir=wiki_dir, reviews_dir=reviews_dir,
                                        amendments=x["amendments"], now=now)
            if out.get("outcome") == "skipped":
                summary["activation_blocked"].append({"node_id": x["node_id"],
                                                      "reason": out.get("reason")})
            affected.update(out.get("affected_sources") or [])
        for x in plan["interrupted_revivals"] + plan["stale_status"] + plan["stale_item_pages"]:
            nodes.add(x["node_id"])
        for x in plan["unwithdrawn_blocks"]:              # finish the block: withdraw, no drop record
            graph.set_status(gconn, x["edge_id"], "superseded", now=now)
            nodes.add(x["node_id"])
        deleted_any = False
        for x in plan["interrupted_deletions"]:           # finish the deletion the human started
            nid = x["node_id"]
            if not x["node_missing"]:
                delete_tag(gconn, node_id=nid, wiki_dir=wiki_dir, reviews_dir=reviews_dir,
                           reason=x["reason"] or "resumed_deletion", now=now)
            else:                                         # node already gone: only the tail is left
                page_rel = f"{NODE_DIR['item']}/{x['slug']}.md" if x.get("slug") else None
                withdrawn = merges._withdraw_b_subjects(reviews_dir, nid, page_rel, now,
                                                        reason="tag_deleted") if page_rel else []
                _audit(reviews_dir, nid, "tag-deleted", {"node_id": nid, "reason": x["reason"],
                                                         "page": page_rel, "resumed": True,
                                                         "withdrawn": withdrawn}, now)
                _end_deletion(reviews_dir, nid)
            deleted_any = True
            nodes.discard(nid)
        for x in plan["cancelled_deletions"]:             # kept/protected meanwhile: delete nothing
            _end_deletion(reviews_dir, x["node_id"])
        blocked_nodes = {x["node_id"] for x in plan["unwithdrawn_blocks"]}
        for nid in sorted(nodes):
            node = _item_node(gconn, nid)
            if node is None:
                continue
            if (nid in blocked_nodes and node["status"] == "candidate"
                    and not graph.sources_for_node(gconn, nid)):
                delete_tag(gconn, node_id=nid, wiki_dir=wiki_dir, reviews_dir=reviews_dir,
                           reason="blocked_never_kept", now=now)   # what the block would have done
                deleted_any = True
                continue
            _refresh_page(gconn, node_id=nid, wiki_dir=wiki_dir, reviews_dir=reviews_dir, now=now)
        pages_changed = deleted_any or bool(nodes) or bool(plan["interrupted_activations"])
        if any(plan[k] for k in _PLAN_KEYS if k not in _REPORT_ONLY):
            _audit(reviews_dir, f"repair-{uuid.uuid4().hex[:8]}", "tag-links-repaired",
                   {**summary, "affected_sources": sorted(affected)}, now)
    return {"applied": True, **summary, "affected_sources": sorted(affected),
            "pages_changed": pages_changed}


# --- batch finish ---------------------------------------------------------------------------------


def finish(root: Path, affected_sources: set[str] | list[str], *, wiki_dir: Path,
           pages_changed: bool = False, templates_dir: Path | None = None,
           intent: str | None = None, origin: str = "tag_action") -> dict[str, Any]:
    """Publish a tag action's effects through the one publication function (ADR-0068 decision 4):
    the affected Source pages (their Items projection), then `wiki/index.md` and the keyword index
    once (whenever a source was affected or `pages_changed` says tag pages moved/vanished), together
    with any publication still pending. `intent` is the entry the caller recorded before its commit.
    Returns `{index_rebuilt, keyword_reindexed, warnings, publication}`; a refresh failure is a
    warning the caller shows, and stays queued for retry."""
    from app.workers import publication
    changed = bool(affected_sources) or pages_changed
    out = publication.publish(Path(root), intent=intent, sources=affected_sources, index=changed,
                              keyword=changed, origin=origin, wiki_dir=wiki_dir,
                              templates_dir=templates_dir)
    return {"index_rebuilt": out["index_rebuilt"], "keyword_reindexed": out["keyword_reindexed"],
            "warnings": out["warnings"], "publication": out}
