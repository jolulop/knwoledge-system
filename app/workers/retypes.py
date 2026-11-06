#!/usr/bin/env python3
"""Phase 6 governance executor: change_item_type (ADR-0059).

The **non-rekeying** replacement for ADR-0051's `change_entity_subtype`: under the
type-neutral `itm_` id, a classification change is a **metadata flip** — page frontmatter
`item_type` + graph nodes-table mirror + audit — with **no id change, no page move, no edge
re-point, no tombstone**. Merge (ADR-0050) and split (ADR-0052) remain the only identity
surgery. Applying one retype withdraws competing pending/deferred retypes of the same node
(ADR-0051's rule carried over); everything else referencing the node stays valid because the
id never changes.

The sentinel `unclassified_review_required` is **never a valid target** (a retype clears
uncertainty, it never introduces it); clearing a sentinel-typed candidate is this executor's
main job besides ordinary misclassification fixes. Mentioning Source pages re-render via the
caller fan-out (`affected_sources`) because their Items section groups by item_type.
Graph-REQUIRED, key-free, deterministic; typed scope-skips, never a partial apply.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.backend import graph, taxonomy
from app.backend.manifests import iso_now
from app.workers import apply_outcomes as oc
from app.workers import item_pages, items, reviews
from app.workers.wiki_render import NODE_DIR, render_item_page

_RETYPABLE_STATUSES = frozenset({"active", "candidate"})   # live statuses only (ADR-0051 decision D)
# Canonical item id (ADR-0059/0021): the type-neutral prefix + 16-hex frozen name-hash. A
# malformed/tampered id must never reach a page path.
_CANONICAL_ITEM_ID = re.compile(r"itm_[0-9a-f]{16}")


def _is_canonical_item_id(x: Any) -> bool:
    return isinstance(x, str) and bool(_CANONICAL_ITEM_ID.fullmatch(x))


def _subject(item: dict[str, Any]) -> dict[str, Any]:
    """The review's subject when it is an object, else {} (a malformed review never raises)."""
    subj = item.get("subject")
    return subj if isinstance(subj, dict) else {}


def _context(item: dict[str, Any]) -> dict[str, Any]:
    ctx = item.get("context")
    return ctx if isinstance(ctx, dict) else {}


def _approved_retypes(reviews_dir: Path) -> list[dict[str, Any]]:
    d = Path(reviews_dir) / "approved"
    out: list[dict[str, Any]] = []
    for path in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):            # unreadable, invalid UTF-8 or JSON
            continue
        if (isinstance(item, dict) and item.get("type") == "change_item_type"
                and item.get("status") == "approved"):
            out.append(item)
    return out


# --- ADR-0070 B4: competing proposals for one tag ---------------------------------------------


EXECUTED, VERIFIED = "retyped", "retype-verified"   # the two settlement record kinds (file names)


def _settlement_files(reviews_dir: Path, rid: str = "*") -> list[tuple[Path, str]]:
    """`[(path, kind)]` of the type-change settlement records (`<rid>-retyped-*` executions and
    `<rid>-retype-verified-*` verifications), sorted."""
    audit = Path(reviews_dir) / "audit_log"
    if not audit.is_dir():
        return []
    out = [(p, EXECUTED) for p in audit.glob(f"{rid}-{EXECUTED}-*.json")]
    out += [(p, VERIFIED) for p in audit.glob(f"{rid}-{VERIFIED}-*.json")]
    return sorted(out)


def _settled(reviews_dir: Path, item: dict[str, Any]) -> dict[str, str | None]:
    """This approval's own settlement: `{"executed": <latest time> | None, "verified": <time> |
    None}` from the records established as its own (:func:`_owns`). Unreadable files are skipped here
    (screens); Apply's history fails closed on them separately."""
    rid = str(item.get("review_id") or "")
    found: dict[str, list[str]] = {EXECUTED: [], VERIFIED: []}
    for path, kind in _settlement_files(reviews_dir, rid) if rid else []:
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if _owns(item, rid, rec):
            found[kind].append(rec["decided_at"])
    return {k: (max(v, key=lambda t: _time(t)) if v else None)
            for k, v in (("executed", found[EXECUTED]), ("verified", found[VERIFIED]))}


def _settled_at(reviews_dir: Path, item: dict[str, Any]) -> str | None:
    """When this approval was settled — carried out, or verified already in effect — else None."""
    times = [t for t in _settled(reviews_dir, item).values() if t]
    return max(times, key=lambda t: _time(t)) if times else None


def approval_fingerprint(item: dict[str, Any]) -> str:
    """The identity of one approval without an `approval_id`: sha256 of the canonical review id,
    type, subject, proposal and `decided_at` (sorted keys, fixed separators)."""
    import hashlib
    payload = {"review_id": item.get("review_id"), "type": item.get("type"),
               "subject": item.get("subject"), "proposal": item.get("proposal"),
               "decided_at": item.get("decided_at")}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _owns(item: dict[str, Any], rid: str, rec: dict[str, Any]) -> bool:
    """Whether an executor record is ESTABLISHED as this approval's own (B4 review round 3):
    - an approval with an `approval_id`: a record carrying the same id;
    - one without: a record carrying its `approval_fingerprint` (written since B4);
    - a record from before B4 (neither): the same review, at or after the approval's `decided_at`,
      both parsed as instants (offsets included).
    Anything else — an unparsable time, a missing `decided_at` on an old record — is never
    established ownership, so it can't authorize a replay."""
    if not isinstance(rec, dict) or str(rec.get("review_id") or rid) != rid:
        return False
    at = _time(rec.get("decided_at"))
    if at is None:
        return False
    approval_id = item.get("approval_id")
    if approval_id:
        return rec.get("approval_id") == approval_id
    if rec.get("approval_id"):
        return False
    if rec.get("approval_fingerprint"):
        return rec["approval_fingerprint"] == approval_fingerprint(item)
    decided = _time(item.get("decided_at"))
    return decided is not None and at >= decided


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def _review_node(reviews_dir: Path, rid: str) -> str | None:
    """The tag a review is about, read from its file in any state (None when it can't be read)."""
    for state in ("approved", "pending", "rejected"):
        path = Path(reviews_dir) / state / f"{rid}.json"
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(item, dict):
            nid = _subject(item).get("node_id")
            if _is_canonical_item_id(nid):
                return nid
    return None


def execution_history(reviews_dir: Path) -> dict[str, Any]:
    """ADR-0070 B4 review rounds 2–3 — every tag's type-change execution history, from EVERY executor
    record (`audit_log/*-retyped-*`), whichever review or approval generation wrote it (reopened
    ones included): `{"latest": {node_id: <latest time>}, "records": {node_id: [record]},
    "unknown": {node_id, ...}, "unknown_all": bool}`. Fails closed: a record that can't be read (or has no usable time) makes
    its tag's history unknown — mapped through the review id in its file name; if even that fails,
    every tag's history is unknown (`unknown_all`)."""
    latest: dict[str, datetime] = {}
    latest_text: dict[str, str] = {}
    records: dict[str, list[dict[str, Any]]] = {}
    unknown: set[str] = set()
    unknown_all = False
    for path, kind in _settlement_files(reviews_dir):
        rid = path.name.rsplit(f"-{kind}-", 1)[0]
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rec = None
        nid = rec.get("node_id") if isinstance(rec, dict) else None
        at = _time(rec.get("decided_at")) if isinstance(rec, dict) else None
        if _is_canonical_item_id(nid) and at is not None:
            if nid not in latest or at > latest[nid]:
                latest[nid], latest_text[nid] = at, rec["decided_at"]
            records.setdefault(nid, []).append({**rec, "review_id": rec.get("review_id") or rid,
                                                "kind": kind})
            continue
        owner = nid if _is_canonical_item_id(nid) else _review_node(reviews_dir, rid)
        if owner is None:
            unknown_all = True
        else:
            unknown.add(owner)
    return {"latest": latest_text, "records": records, "unknown": unknown,
            "unknown_all": unknown_all}


def retype_index(reviews_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """`{node_id: [review]}` of every pending, deferred or approved `change_item_type` review (one
    scan; a screen showing many retypes passes it to :func:`competitors`)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for state in ("pending", "approved"):
        d = Path(reviews_dir) / state
        for path in sorted(d.glob("*.json")) if d.exists() else []:
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):        # unreadable, invalid UTF-8 or JSON
                continue
            if (isinstance(item, dict) and item.get("type") == "change_item_type"
                    and item.get("status") in ("pending", "deferred", "approved")
                    # the queue's own shape rule: subject/proposal/context are objects when present
                    and all(isinstance(item[f], dict) for f in ("subject", "proposal", "context")
                            if f in item)):
                nid = _subject(item).get("node_id")       # a malformed review is left out
                if _is_canonical_item_id(nid):
                    out.setdefault(nid, []).append(item)
    return out


def competitors(reviews_dir: Path, node_id: Any, *, exclude: Any = None,
                index: dict[str, list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
    """ADR-0070 B4 — the other open or approved type changes proposed for the same tag, for every
    surface that shows a retype: `[{review_id, to_item_type, status, source_id, applied_at}]`
    (`applied_at` only for an approval already carried out)."""
    if not _is_canonical_item_id(node_id):
        return []
    index = index if index is not None else retype_index(reviews_dir)
    out = []
    for item in index.get(node_id, []):
        if item.get("review_id") == exclude:
            continue
        out.append({"review_id": item.get("review_id"),
                    "to_item_type": _subject(item).get("to_item_type"),
                    "status": item.get("status"),
                    "source_id": _context(item).get("source_id"),
                    **({"applied_at": settled["executed"], "verified_at": settled["verified"]}
                       if (settled := _settled(reviews_dir, item)) and item.get("status") == "approved"
                       else {"applied_at": None, "verified_at": None})})
    return out


def approved_unapplied(reviews_dir: Path, node_id: Any, *, exclude: Any = None) -> list[dict[str, Any]]:
    """The tag's approved type changes not yet carried out (no executor record since approval) — the
    approval guard refuses a second one, and Apply blocks the tag while there are several."""
    return [c for c in competitors(reviews_dir, node_id, exclude=exclude)
            if c["status"] == "approved" and c["applied_at"] is None and c["verified_at"] is None]


def _withdraw_competing_retypes(reviews_dir: Path, node_id: str, keep_rid: str, now: str) -> list[str]:
    """Withdraw unresolved (pending/deferred) `change_item_type` items for the same node.

    Targeted, unlike merge/rekey's all-subjects withdrawal: the node id stays live, so its
    other reviews (promotes, deprecates, …) keep their premise — only the competing
    classification proposals are superseded by the applied one."""
    withdrawn: list[str] = []
    pend = Path(reviews_dir) / "pending"
    for path in sorted(pend.glob("*.json")) if pend.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):            # unreadable, invalid UTF-8 or JSON
            continue
        if not isinstance(item, dict) or item.get("type") != "change_item_type":
            continue
        rid = item.get("review_id")
        if rid == keep_rid or not rid:
            continue
        if _subject(item).get("node_id") == node_id:
            if reviews.withdraw_review_item(reviews_dir, rid, reason="superseded_by_retype", now=now):
                withdrawn.append(rid)
    return withdrawn


def _withdrawal_preview(reviews_dir: Path, node_id: str, keep_rid: str) -> list[dict[str, Any]]:
    """The exact pending/deferred proposals applying `keep_rid` withdraws (read before the
    withdrawal, for the readable preview): `[{review_id, to_item_type, status}]`."""
    return [{"review_id": c["review_id"], "to_item_type": c["to_item_type"], "status": c["status"]}
            for c in competitors(reviews_dir, node_id, exclude=keep_rid)
            if c["status"] in ("pending", "deferred")]


def _identity(item: dict[str, Any]) -> dict[str, str]:
    """What binds a settlement record to ONE approval: its `approval_id`, else its fingerprint."""
    if item.get("approval_id"):
        return {"approval_id": item["approval_id"]}
    return {"approval_fingerprint": approval_fingerprint(item)}


def _write_verification(reviews_dir: Path, item: dict[str, Any], nid: str, item_type: Any,
                        now: str) -> None:
    """B4 review round 4: the record that an approval was VERIFIED already in effect (page and graph
    at its type) — `audit_log/<rid>-retype-verified-<hex>.json`. Not an execution: it never matches
    the `*-retyped-*` execution records, so nothing reads it as "carried out"; it settles the
    approval for the guard and for ordering."""
    rid = str(item.get("review_id") or "")
    audit = Path(reviews_dir) / "audit_log"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / f"{rid}-{VERIFIED}-{uuid.uuid4().hex[:8]}.json").write_text(
        json.dumps({"review_id": rid, "decision": "retype_verified_in_effect",
                    "decided_by": "system", "decided_at": now, "node_id": nid,
                    "item_type": item_type, **_identity(item)},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_audit(reviews_dir: Path, rid: str, record: dict[str, Any], now: str) -> None:
    audit = Path(reviews_dir) / "audit_log"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / f"{rid}-retyped-{uuid.uuid4().hex[:8]}.json").write_text(
        json.dumps({"review_id": rid, "decision": "retyped", "decided_by": "system",
                    "decided_at": now, **record}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def retype_check(gconn, item: dict[str, Any], *, wiki_dir: Path) -> tuple[str, Any]:
    """Where one approved `change_item_type` stands, by the executor's own rules (ADR-0066 review
    round 10: shared with the approved queue's "applied" column so the two can't drift):
    `("skip", reason)` — apply would skip it (malformed, node/page missing, not retypable);
    `("done", None)` — the page already carries the target type (apply is a no-op);
    `("todo", ctx)` — apply has work to do (`ctx` = node, page_rel, page_path, meta, from/to type)."""
    if not isinstance(item.get("subject"), dict):           # malformed: never raise, never apply
        return "skip", "invalid_subject"
    subj = item["subject"]
    proposal = item.get("proposal") if isinstance(item.get("proposal"), dict) else {}
    nid = subj.get("node_id")
    to_type = subj.get("to_item_type")
    if not taxonomy.is_production_item_type(to_type):       # sentinel is never a valid target
        return "skip", "invalid_to_item_type"
    if proposal.get("to_item_type") != to_type:              # proposal must agree with the subject
        return "skip", "to_item_type_mismatch"
    if not _is_canonical_item_id(nid):                       # canonical id before any page path (security)
        return "skip", "noncanonical_node_id"
    node = graph.get_node(gconn, nid)
    if node is None:
        return "skip", "node_missing"
    if node["node_type"] != "item":
        return "skip", "out_of_scope"
    if node["status"] not in _RETYPABLE_STATUSES:
        return "skip", "node_not_retypable"
    page_rel = f"{NODE_DIR['item']}/{node['slug']}.md"
    page_path = Path(wiki_dir) / page_rel
    if (problem := item_pages.page_problem(gconn, wiki_dir, nid, node["slug"])) is not None:
        return "skip", problem["reason"]         # ADR-0070 B1 review: never another item's page
    meta = items._read_node_meta(page_path)
    if meta is None:
        return "skip", "page_missing"
    from_type = meta.get("item_type") or node.get("item_type")
    if from_type == to_type:
        return "done", None             # already in the target state — idempotent no-op (re-apply safe)
    return "todo", {"nid": nid, "to_type": to_type, "node": node, "page_rel": page_rel,
                    "page_path": page_path, "meta": meta, "from_type": from_type}


def apply_retypes(gconn, reviews_dir: Path, *, wiki_dir: Path, now: str | None = None) -> dict[str, Any]:
    """Apply approved `change_item_type` decisions (ADR-0059). Returns
    `{applied, skipped:[{review_id, reason}], changed_pages, graph_changed, affected_sources,
    outcomes}` — `outcomes` has ONE ADR-0072 record per approved retype (`applied` = the applied
    outcomes, every `skipped` entry a `skipped` outcome; a page already at the target type whose graph
    mirror disagrees is a `skipped` outcome, `graph_item_type_mismatch`, the legacy list never
    carried). Graph-REQUIRED; the caller owns the final commit + the Source-page re-render of
    `affected_sources` (their Items sections group by item_type)."""
    from app.workers import tag_links                    # lazy: tag_links imports items/promote
    # ADR-0066 review round 10: a retype reads the item page and writes it back, so it is serialized
    # with tag actions (an alias edit in between would be overwritten) — from the read to the write.
    with tag_links.ledger_lock(reviews_dir):
        return _apply_retypes(gconn, reviews_dir, wiki_dir=wiki_dir, now=now)


def _apply_retypes(gconn, reviews_dir: Path, *, wiki_dir: Path, now: str | None) -> dict[str, Any]:
    """The body of :func:`apply_retypes` (called under the ledger lock)."""
    now = now or iso_now()
    applied = 0
    skipped: list[dict[str, str]] = []
    changed_pages: list[str] = []
    affected_sources: set[str] = set()
    outcomes: list[dict[str, Any]] = []

    approvals = _approved_retypes(reviews_dir)
    # settled = carried out, or verified already in effect (B4 review round 4)
    applied_at = {str(it.get("review_id")): _settled_at(reviews_dir, it) for it in approvals}
    # Only approvals the executor could carry out compete; a malformed or refused one reports its
    # own reason and never blocks a valid one. Read-only, and only for a tag with several waiting
    # approvals; a check that can't be read counts as appliable (conservative: it still blocks).
    unapplied: dict[str, list[dict[str, Any]]] = {}
    for it in approvals:
        if applied_at.get(str(it.get("review_id"))) is None:
            unapplied.setdefault(str(_subject(it).get("node_id")), []).append(it)
    appliable: set[str] = set()
    for group in unapplied.values():
        if len(group) < 2:
            appliable.update(str(it.get("review_id")) for it in group)
            continue
        for it in group:
            check = oc.guarded(retype_check, gconn, it, wiki_dir=wiki_dir)
            if not (isinstance(check, tuple) and check[0] == "skip"):
                appliable.add(str(it.get("review_id")))
    by_node: dict[str, list[dict[str, Any]]] = {}
    for it in approvals:
        by_node.setdefault(str(_subject(it).get("node_id")), []).append(it)

    history = execution_history(reviews_dir)
    run_latest: dict[str, str] = {}                       # executions of THIS run, per tag

    def latest_for(node: str) -> str | None:
        times = [t for t in (history["latest"].get(node), run_latest.get(node)) if t]
        return max(times, key=lambda t: _time(t) or datetime.min) if times else None

    def not_after_latest(o: dict[str, Any], node: str) -> str | None:
        """Why a waiting approval may not proceed: the tag's history can't be established, or the
        approval isn't provably decided after the tag's latest type change; None when it may."""
        if history["unknown_all"] or node in history["unknown"]:
            return "execution_history_unreadable"
        latest = latest_for(node)
        if latest is None:
            return None
        decided, last = _time(o.get("decided_at")), _time(latest)
        if decided is None or last is None or not decided > last:
            return "approval_not_after_latest_retype"
        return None

    for item in approvals:
        rid = str(item.get("review_id", ""))
        nid_subject = str(_subject(item).get("node_id"))
        siblings = by_node.get(nid_subject, [])
        # ADR-0070 B4 — never resolved by processing order. (1) An approval already carried out is
        # never applied again once another retype of the same tag was carried out at or after it
        # (executor times have whole-second precision, so a tie counts as "after": no order is
        # guessed), or — order-independent, B4 review round 1 — once another approval of the tag
        # is the one waiting to be carried out (the newer human choice). If the page still carries
        # this approval's type and nothing newer waits, it is simply in effect.
        mine = applied_at.get(rid)
        waiting_all = [o for o in siblings if applied_at.get(str(o.get("review_id"))) is None
                       and str(o.get("review_id")) in appliable]
        # B4 review round 2: a waiting approval is the newer choice only when it is PROVABLY decided
        # after the tag's latest type change, and the only one waiting.
        newer_waits = (mine is not None and len(waiting_all) == 1 and waiting_all[0] is not item
                       and not_after_latest(waiting_all[0], nid_subject) is None)
        # B4 review round 3: whether a carried-out approval may replay (repair drift) is decided on
        # the COMPLETE history — any execution of the tag that is not established as this approval's
        # own, at or after its own (as parsed instants; a tie counts), from any review or
        # generation, approved or not, supersedes it. Unknown history never lets it mutate the tag.
        replaced = mine is not None and any(
            not _owns(item, rid, rec) and _time(rec.get("decided_at")) is not None
            and _time(rec["decided_at"]) >= _time(mine)
            for rec in history["records"].get(nid_subject, []))
        if (mine is not None and (history["unknown_all"] or nid_subject in history["unknown"])
                and retype_check(gconn, item, wiki_dir=wiki_dir)[0] == "todo"):
            details, _pt = _outcome_details(gconn, item, wiki_dir=wiki_dir, ctx=None)
            skipped.append({"review_id": rid, "reason": "execution_history_unreadable"})
            outcomes.append(oc.outcome(rid, "change_item_type", oc.SKIPPED,
                                       reason="execution_history_unreadable", **details))
            continue
        if newer_waits or (replaced and retype_check(gconn, item, wiki_dir=wiki_dir)[0] == "todo"):
            details, _pt = _outcome_details(gconn, item, wiki_dir=wiki_dir, ctx=None)
            skipped.append({"review_id": rid, "reason": "superseded_by_retype"})
            outcomes.append(oc.outcome(rid, "change_item_type", oc.SKIPPED,
                                       reason="superseded_by_retype", **details))
            continue
        # (2) A waiting approval proceeds only when the tag's execution history is readable and it
        # was provably decided after the tag's latest type change (B4 review round 2: a restored or
        # same-second approval never overwrites a later choice). Apply fails closed here; screens
        # merely skip unreadable records.
        if mine is None and rid in appliable:
            why = not_after_latest(item, nid_subject)
            if why is not None:
                details, _pt = _outcome_details(gconn, item, wiki_dir=wiki_dir, ctx=None)
                skipped.append({"review_id": rid, "reason": why})
                outcomes.append(oc.outcome(rid, "change_item_type", oc.SKIPPED, reason=why,
                                           **details, problem={"latest_settled_choice_at":
                                                               latest_for(nid_subject),
                                                               "approved_at": item.get("decided_at")}))
                continue
        # (3) Several approvals of one tag still waiting (e.g. a restored database, or approvals made
        # before the guard): all of them are blocked until a human reopens all but one.
        waiting = [o for o in siblings if applied_at.get(str(o.get("review_id"))) is None
                   and str(o.get("review_id")) in appliable]
        if mine is None and rid in appliable and len(waiting) > 1:
            details, _pt = _outcome_details(gconn, item, wiki_dir=wiki_dir, ctx=None)
            competing = [{"review_id": o.get("review_id"),
                          "to_item_type": (o.get("subject") or {}).get("to_item_type")}
                         for o in waiting if o is not item]
            skipped.append({"review_id": rid, "reason": "competing_retypes_approved"})
            outcomes.append(oc.outcome(rid, "change_item_type", oc.SKIPPED,
                                       reason="competing_retypes_approved", **details,
                                       problem={"competing": competing}))
            continue
        state, ctx = retype_check(gconn, item, wiki_dir=wiki_dir)
        details, page_type = _outcome_details(gconn, item, wiki_dir=wiki_dir,
                                              ctx=ctx if state == "todo" else None)
        if state == "skip":
            skipped.append({"review_id": rid, "reason": ctx})
            problem = oc.guarded(_ownership_problem, gconn, item, wiki_dir=wiki_dir, reason=ctx)
            outcomes.append(oc.outcome(rid, "change_item_type", oc.SKIPPED, reason=ctx, **details,
                                       **({"problem": problem} if problem else {})))
            continue
        if state == "done":
            out = _done_outcome(gconn, item, rid, details, page_type)
            outcomes.append(out)
            # B4 review round 4: an approval verified in effect (page AND graph at its type) settles
            # once, with its own record — distinct from an execution — so it no longer holds the
            # approval guard. A settlement already on file is reused: nothing new, same time.
            if (out["outcome"] == oc.ALREADY_IN_EFFECT and applied_at.get(rid) is None
                    and not history["unknown_all"] and nid_subject not in history["unknown"]):
                _write_verification(reviews_dir, item, nid_subject, details["after"]["item_type"],
                                    now)
                applied_at[rid] = now
                history["records"].setdefault(nid_subject, []).append(
                    {"review_id": rid, "node_id": nid_subject, "decided_at": now, "kind": VERIFIED,
                     **_identity(item)})
            continue
        nid, to_type, node = ctx["nid"], ctx["to_type"], ctx["node"]
        page_rel, meta, from_type = ctx["page_rel"], ctx["meta"], ctx["from_type"]

        # --- APPLY: the metadata flip — re-render the page with the new item_type (status,
        # review_status, and every page-owned field preserved), mirror the graph row, done. ---
        node_sources, suggested = graph.tag_projection(gconn, nid, node["status"])
        duplicates = graph.active_duplicates(gconn, nid)
        rendered = render_item_page({
            "node_id": nid, "item_type": to_type,
            "title": meta["title"], "aliases": meta["aliases"],
            "confidence": meta.get("confidence", "low"),
            "source_ids": node_sources, "suggested_ids": suggested, "status": node["status"],
            "duplicates": duplicates,
            "split_from": meta.get("split_from"),
            "split_review_id": meta.get("split_review_id"),
            "description": meta.get("description"),
        }, labels=items._link_labels(wiki_dir, node_sources + suggested, duplicates))
        item_pages.write_page(gconn, wiki_dir, nid, node["slug"], rendered)
        graph.upsert_node(gconn, node_id=nid, node_type="item", slug=node["slug"],
                          status=node["status"], item_type=to_type, now=now)
        changed_pages.append(page_rel)
        sources = graph.sources_for_node(gconn, nid)
        affected_sources.update(sources)
        withdraws = oc.guarded(_withdrawal_preview, reviews_dir, nid, rid)   # read before
        withdrawn = _withdraw_competing_retypes(reviews_dir, nid, rid, now)
        _write_audit(reviews_dir, rid, {
            **_identity(item),
            "node_id": nid, "slug": node["slug"], "page": page_rel,
            "from_item_type": from_type, "to_item_type": to_type,
            "status": node["status"], "withdrawn_competing_retypes": withdrawn,
            "affected_pages": [page_rel] + [f"Sources/{s}.md" for s in sorted(sources)],
        }, now)
        applied += 1
        applied_at[rid] = now                    # this run's history counts for later approvals
        run_latest[nid] = now
        history["records"].setdefault(nid, []).append(
            {"review_id": rid, "node_id": nid, "decided_at": now, "kind": EXECUTED,
             **_identity(item)})
        outcomes.append(oc.outcome(rid, "change_item_type", oc.APPLIED, **details,
                                   **({"withdraws": withdraws} if isinstance(withdraws, list)
                                      and withdraws else {})))

    return {"applied": applied, "skipped": skipped, "changed_pages": sorted(set(changed_pages)),
            "graph_changed": applied > 0, "affected_sources": sorted(affected_sources),
            "outcomes": outcomes}


def _outcome_details(gconn, item: dict[str, Any], *, wiki_dir: Path,
                     ctx: dict[str, Any] | None) -> tuple[dict[str, Any], Any]:
    """ADR-0072 decision 3: the reviewed tag's name and its before/after type, read BEFORE the
    retype writes (read-only; a malformed id is never turned into a page path). Every read here is
    reporting-only (:func:`apply_outcomes.guarded`): a failed one leaves the name as the id and the
    before-type unavailable (`None`, listed in `details["unavailable"]`), never guessed.

    Returns `(details, page_type)` — `page_type` is the page's own `item_type` as read here (None when
    the page has none, :data:`apply_outcomes.UNAVAILABLE` when it couldn't be read), for verifying
    an already-in-effect retype."""
    subj = item.get("subject") if isinstance(item.get("subject"), dict) else {}
    nid, to_type = subj.get("node_id"), subj.get("to_item_type")
    node = oc.guarded(graph.get_node, gconn, nid) if _is_canonical_item_id(nid) else None
    is_item = isinstance(node, dict) and node.get("node_type") == "item"
    page_type: Any = oc.UNAVAILABLE
    if ctx is not None:
        from_type = ctx["from_type"]                     # the executor's own read
        page_type = ctx["meta"].get("item_type")
    elif is_item:
        meta = oc.guarded(items._read_node_meta, Path(wiki_dir) / NODE_DIR["item"] / f"{node['slug']}.md")
        if isinstance(meta, dict):                       # retype_check's own rule: page, then mirror
            page_type = meta.get("item_type")
            from_type = page_type or node.get("item_type")
        else:                                            # missing or unreadable page: not guessed
            from_type = None
    else:
        from_type = None
    details: dict[str, Any] = {
        "subject": oc.item_name(wiki_dir, node, nid) if is_item else oc.named(nid, None),
        "before": {"item_type": from_type}, "after": {"item_type": to_type}}
    if from_type is None:
        details["unavailable"] = ["before.item_type"]
    return details, page_type


def _done_outcome(gconn, item: dict[str, Any], rid: str, details: dict[str, Any],
                  page_type: Any) -> dict[str, Any]:
    """The outcome of a retype `retype_check` called done: `already_in_effect` only when the page
    AND the graph mirror read back at the target type (both re-read here, guarded); an unreadable
    one is `verification_unavailable`, never in effect. Nothing is written on this path."""
    to_type = details["after"]["item_type"]
    node = oc.guarded(graph.get_node, gconn, (item.get("subject") or {}).get("node_id"))
    if page_type is oc.UNAVAILABLE or node is oc.UNAVAILABLE:
        return oc.unverified(rid, "change_item_type", changed=False, **details)
    if page_type != to_type:                             # the type came from the mirror only
        return oc.outcome(rid, "change_item_type", oc.SKIPPED, reason=oc.POSTCONDITION_NOT_MET,
                          **details, problem={"fields": ["item_type"]})
    if (node or {}).get("item_type") != to_type:
        return oc.outcome(rid, "change_item_type", oc.SKIPPED,
                          reason="graph_item_type_mismatch", **details)
    return oc.outcome(rid, "change_item_type", oc.ALREADY_IN_EFFECT, **details)


def _ownership_problem(gconn, item: dict[str, Any], *, wiki_dir: Path, reason: Any) -> dict | None:
    """The structured ADR-0070/0071 page problem behind an ownership skip (retype_check returns
    only its code), recomputed read-only for the outcome's details."""
    nid = (item.get("subject") or {}).get("node_id")
    node = graph.get_node(gconn, nid) if _is_canonical_item_id(nid) else None
    if node is None or node.get("node_type") != "item":
        return None
    problem = item_pages.page_problem(gconn, wiki_dir, nid, node["slug"])
    return problem if problem and problem.get("reason") == reason else None
