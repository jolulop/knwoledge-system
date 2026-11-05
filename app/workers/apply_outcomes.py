#!/usr/bin/env python3
"""Per-review Apply outcomes (ADR-0072 decision 2) — the one shared reporting shape.

Every Apply executor reports, beside its existing counts, ONE record per approved review of its
types that it handles:

    {"review_id": <id>, "type": <review type>, "outcome": <outcome>, "reason": <code>?, "details": {}}

- ``applied``: the executor changed state and finished.
- ``already_in_effect``: only after the executor VERIFIED its postcondition (not "nothing happened").
- ``skipped``: refused before any change; ``reason`` is the typed code, the structured problem goes in
  ``details["problem"]``.
- ``incomplete``: some state changed but the review did not finish (e.g. an identity operation left
  in flight); never reported as an ordinary skip.

**Verified end state only** (B2a review round 1): an outcome reports what was verified after the
run, not what was requested. A repair that wrote anything is ``applied`` (``normalized: True`` where
the legacy counter calls it normalized), never ``already_in_effect``, which is reserved for a verified
no-op. A postcondition that reads back differently is :data:`HIDDEN_TAKES_PRECEDENCE` (verified hidden)
or :data:`POSTCONDITION_NOT_MET`; one that can't be read is :data:`VERIFICATION_UNAVAILABLE`
(:func:`unverified`). Reporting reads go through :func:`guarded`, so they never raise.

``details`` carries the display names the executor read **before** mutating (decision 3), as
``{"id", "name"}`` pairs (:func:`named`), plus explicit before/after values where relevant. The keys
the readable Apply pages use, per type:

- every type: ``subject`` (the reviewed page/tag/document/claim/synthesis);
- ``merge_items``: ``survivor``, ``absorbed``;
- ``change_item_type``: ``before.item_type``, ``after.item_type``;
- ``promote_candidate_node``: ``before.title`` / ``after.title`` when renamed, ``after.item_type``;
- ``split_item``: ``spinoff``, ``documents`` (the moved documents' count);
- ``mark_semantic_duplicate``: ``duplicate_of``;
- ``resolve_contradiction``: ``partner``, ``resolution``, ``winner`` when superseded.

Advisories (ADR-0071) are NOT outcomes; they stay in the executors' ``advisories`` lists. The
renderer never reads the vault: names come only from here.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

APPLIED = "applied"
ALREADY_IN_EFFECT = "already_in_effect"
SKIPPED = "skipped"
INCOMPLETE = "incomplete"
OUTCOMES = frozenset({APPLIED, ALREADY_IN_EFFECT, SKIPPED, INCOMPLETE})

# Postcondition reasons (B2a review round 1). An outcome states the VERIFIED end state, never the
# requested one: `skipped` when nothing changed, `incomplete` when this run changed state.
VERIFICATION_UNAVAILABLE = "verification_unavailable"   # a read needed to verify failed
POSTCONDITION_NOT_MET = "postcondition_not_met"         # read fine, not the requested state
HIDDEN_TAKES_PRECEDENCE = "hidden_takes_precedence"     # verified hidden, which wins over the request


class _Unavailable:
    """The result of a reporting-only read that failed: never a value, never a success."""
    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "UNAVAILABLE"


UNAVAILABLE = _Unavailable()


def guarded(fn, *args: Any, **kwargs: Any) -> Any:
    """Run a READ used only for reporting or verification; any exception gives :data:`UNAVAILABLE`.
    A reporting read must never turn the executor's refusal or result into an exception, and an
    unavailable read must never justify ``already_in_effect`` (callers report
    :data:`VERIFICATION_UNAVAILABLE` instead)."""
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - reporting only; the executor's own outcome stands
        return UNAVAILABLE


def unverified(review_id: str, review_type: str, *, changed: bool, reason: str = "",
               **details: Any) -> dict[str, Any]:
    """The honest outcome when the requested end state couldn't be confirmed: ``incomplete`` if this
    run changed state, else ``skipped``; `reason` defaults to :data:`VERIFICATION_UNAVAILABLE`."""
    return outcome(review_id, review_type, INCOMPLETE if changed else SKIPPED,
                   reason=reason or VERIFICATION_UNAVAILABLE, **details)


# Reporting gaps (decision 2): an approved appliable review with no outcome, or with more than one.
NOT_REPORTED = "not_reported"
REPORTED_TWICE = "reported_more_than_once"


def outcome(review_id: str, review_type: str, kind: str, *, reason: str | None = None,
            **details: Any) -> dict[str, Any]:
    """One outcome record. Raises ValueError on an unknown outcome or a skip without a reason."""
    if kind not in OUTCOMES:
        raise ValueError(f"unknown outcome {kind!r}")
    if kind in (SKIPPED, INCOMPLETE) and not reason:
        raise ValueError(f"a {kind} outcome needs a reason")
    rec: dict[str, Any] = {"review_id": review_id, "type": review_type, "outcome": kind,
                           "details": details}
    if reason:
        rec["reason"] = reason
    return rec


def named(node_id: Any, name: Any) -> dict[str, str]:
    """A display pair: the name as read, or the id itself when the name couldn't be read (never a
    blank; decision 3)."""
    ident = str(node_id or "")
    text = " ".join(str(name).split()) if isinstance(name, str) else ""
    return {"id": ident, "name": text or ident}


def page_name(wiki_dir: Path, rel: str | None, fallback_id: Any) -> dict[str, str]:
    """`named(fallback_id, <the page's title>)` for a page path relative to `wiki_dir` (e.g.
    ``Items/data-mesh.md``, ``Sources/src_….md``); the id when the page or its title can't be read.
    Read BEFORE the executor mutates or deletes the page."""
    from app.workers import labels
    if not rel:
        return named(fallback_id, None)
    rel = rel[:-3] if rel.endswith(".md") else rel
    label = guarded(labels._page_label, Path(wiki_dir) / f"{rel}.md", rel)
    return named(fallback_id, label if isinstance(label, str) else None)


def item_name(wiki_dir: Path, node: dict[str, Any] | None, fallback_id: Any = None) -> dict[str, str]:
    """A tag's display pair from its OWN page (`Items/<slug>.md` whose `item_id` is this tag's);
    the id when the page is missing, unreadable or another tag's."""
    if not node:
        return named(fallback_id, None)
    from app.workers import item_names
    nid = node.get("node_id") or fallback_id
    title = guarded(item_names.title_of, Path(wiki_dir), {"node_id": nid, "slug": node.get("slug")})
    # another tag's page at that path never names this one; a failed read is the id
    return named(nid, title if isinstance(title, str) else None)


def reconcile(handled: list[dict[str, Any]], reported: list[dict[str, Any]]) -> dict[str, Any]:
    """Match the approved appliable reviews (`handled`: `{review_id, type}`) to the outcomes the
    executors reported. Returns `{"outcomes": [...one per review that reported exactly once...],
    "gaps": [{review_id, type, gap, reports}]}`; a gap is a review with no report (`not_reported`) or
    with several (`reported_more_than_once`, every report kept for the technical details). Reports
    for reviews outside `handled` are kept as outcomes (an executor may resolve an item the
    approved/ scan didn't list, e.g. one filed during the run)."""
    by_id: dict[str, list[dict[str, Any]]] = {}
    for rec in reported:
        by_id.setdefault(str(rec.get("review_id")), []).append(rec)
    outcomes, gaps = [], []
    seen: set[str] = set()
    for item in handled:
        rid = str(item.get("review_id"))
        if rid in seen:
            continue
        seen.add(rid)
        recs = by_id.get(rid, [])
        if len(recs) == 1:
            outcomes.append(recs[0])
        else:
            gaps.append({"review_id": rid, "type": item.get("type"),
                         "gap": NOT_REPORTED if not recs else REPORTED_TWICE, "reports": recs})
    for rid, recs in by_id.items():
        if rid in seen:
            continue
        if len(recs) == 1:
            outcomes.append(recs[0])
        else:
            gaps.append({"review_id": rid, "type": recs[0].get("type"), "gap": REPORTED_TWICE,
                         "reports": recs})
    return {"outcomes": outcomes, "gaps": gaps}


def counts(reported: list[dict[str, Any]]) -> Counter:
    """Outcome counts, for checking an executor's aggregate counts against its outcomes."""
    return Counter(rec["outcome"] for rec in reported)
