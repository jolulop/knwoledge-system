#!/usr/bin/env python3
"""The guarded supersede (ADR-0073 decisions 9, 10, 13–17).

An approved `resolve_contradiction` that names a `winner` writes an active `supersedes` edge
winner→loser and deprecates the loser. This module decides — from the authoritative Claim pages,
the graph mirror, the durable identity journal and the audit log — whether to execute, resume,
complete, roll back, report as information, or block. It never guesses: absence counts only when it
is verified, and unreadable evidence blocks.

Records it owns:
- the journal `reviews/in_flight/<review_id>.json` (`op: "supersede"`, `nodes`, `winner`, `loser`,
  `approval_id`, `stage` ∈ `started` · `edge_written` · `loser_deprecated` · `rolling_back`);
- the completion marker on the loser's Claim page (:mod:`claim_marker`);
- the completion audit `reviews/audit_log/<rid>-superseded-<approval_id>.json`
  (`event: supersede_completed`) — the only audit that is completion evidence (decision 13);
- the rollback audit `reviews/audit_log/<rid>-rolled-back-<approval_id>.json`
  (`event: supersede_rolled_back`) — it consumes that approval generation (decision 17).

Write order (decision 10): `begin` → `supersedes` edge, graph committed → stage `edge_written` →
loser page with the marker (fsynced) → graph mirror committed → stage `loser_deprecated` → completion
audit. The Apply caller ends the journal after publication.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.backend import graph
from app.backend.manifests import iso_now
from app.workers import apply_outcomes as ao
from app.workers import claim_marker, claims, identity_journal, reviews
from app.workers.wiki_render import parse_frontmatter

OP = "supersede"
STARTED, EDGE_WRITTEN, LOSER_DEPRECATED, ROLLING_BACK = (
    "started", "edge_written", "loser_deprecated", "rolling_back")
STAGES = frozenset({STARTED, EDGE_WRITTEN, LOSER_DEPRECATED, ROLLING_BACK})

COMPLETED_EVENT = "supersede_completed"
ROLLED_BACK_EVENT = "supersede_rolled_back"

_RID = re.compile(r"rev_[0-9a-f]{16}")
_APPROVAL = re.compile(r"[0-9a-f]{32}")
_CLAIM = re.compile(r"clm_[0-9a-f]{16}")
_CLAIM_STATUSES = frozenset({"active", "deprecated_candidate", "hidden"})

# Effect states (the one effect check shared by Apply, the preview and reopen — decision 9).
COMPLETE = "complete"          # this review's effects hold (already_in_effect; not reopenable)
ABSENT = "absent"              # verified: nothing of this review's effects exists (reopenable)
WITHDRAWN = "withdrawn"        # verified absent after a rollback consumed this approval (reopenable)
PARTIAL = "partial"            # some owned effect exists, not all (blocked)
UNVERIFIABLE = "unverifiable"  # evidence can't be read (blocked)
IN_FLIGHT = "in_flight"        # a journal is open for this review (blocked until Apply finishes)


class Blocked(Exception):
    """A refusal: nothing (more) is written. `reason` is the outcome code."""

    def __init__(self, reason: str, problem: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.problem = problem or {}


# --- identities ------------------------------------------------------------------------------


def binding(item: dict[str, Any]) -> dict[str, Any] | None:
    """`{review_id, approval_id, winner, loser, a, b}` of an approved supersede, or None when it isn't
    one. `approval_id` may be None (a legacy approval) or malformed; callers check it."""
    subj = item.get("subject") or {}
    a, b = subj.get("claim_a"), subj.get("claim_b")
    winner = item.get("winner")
    if not (isinstance(a, str) and isinstance(b, str) and winner in (a, b) and a != b):
        return None
    return {"review_id": str(item.get("review_id") or ""), "approval_id": item.get("approval_id"),
            "winner": winner, "loser": b if winner == a else a, "a": a, "b": b}


def _canonical(bind: dict[str, Any]) -> bool:
    return (bool(_RID.fullmatch(bind["review_id"])) and bool(_CLAIM.fullmatch(bind["winner"]))
            and bool(_CLAIM.fullmatch(bind["loser"])))


def _approval_ok(bind: dict[str, Any]) -> bool:
    return isinstance(bind.get("approval_id"), str) and bool(_APPROVAL.fullmatch(bind["approval_id"]))


def completion_audit_path(reviews_dir: Path, review_id: str, approval_id: str) -> Path:
    return Path(reviews_dir) / "audit_log" / f"{review_id}-superseded-{approval_id}.json"


def rollback_audit_path(reviews_dir: Path, review_id: str, approval_id: str) -> Path:
    return Path(reviews_dir) / "audit_log" / f"{review_id}-rolled-back-{approval_id}.json"


# --- audits ----------------------------------------------------------------------------------


def read_audit(path: Path, *, event: str, bind: dict[str, Any],
               edge_id: str | None = None) -> tuple[str, dict[str, Any] | None]:
    """`("absent", None)`, `("valid", record)` or `("invalid", None)` — unreadable, or a record whose
    identity (`review_id`, `approval_id`) or payload (`event`, `winner`, `loser`, edge id) disagree."""
    path = Path(path)
    if not path.exists():
        return "absent", None
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "invalid", None
    ok = (isinstance(rec, dict) and rec.get("event") == event
          and rec.get("review_id") == bind["review_id"]
          and rec.get("approval_id") == bind["approval_id"]
          and rec.get("winner") == bind["winner"] and rec.get("loser") == bind["loser"]
          and isinstance(rec.get("edge_id"), str) and rec.get("edge_id")
          and (edge_id is None or rec.get("edge_id") == edge_id))
    return ("valid", rec) if ok else ("invalid", None)


def _ensure_audit(path: Path, *, event: str, time_key: str, bind: dict[str, Any], edge_id: str,
                  now: str) -> None:
    """Write the audit durably, or reuse (and flush again) a valid one. An unreadable or conflicting
    entry blocks and is never overwritten; the original time is kept (decisions 13 and 17)."""
    state, _rec = read_audit(path, event=event, bind=bind, edge_id=edge_id)
    if state == "valid":
        reviews.sync_durably(path)
        return
    if state == "invalid":
        raise Blocked("completion_audit_conflict" if event == COMPLETED_EVENT
                      else "rollback_audit_conflict", {"audit": path.name})
    path.parent.mkdir(parents=True, exist_ok=True)
    reviews.write_json_durably(path, {
        "event": event, "review_id": bind["review_id"], "approval_id": bind["approval_id"],
        "winner": bind["winner"], "loser": bind["loser"], "edge_id": edge_id, time_key: now})


# --- authority reads -------------------------------------------------------------------------


def claim_page(claims_dir: Path, cid: str) -> dict[str, Any]:
    """The authoritative Claim page: `{state: ok|missing|unreadable, status, marker_state, marker}`.
    Unreadable covers an unreadable file, a page naming another claim, and a status that isn't a
    supported claim status."""
    path = Path(claims_dir) / f"{cid}.md"
    if not path.exists():
        return {"state": "missing", "status": None, "marker_state": claim_marker.NONE,
                "marker": None}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"state": "unreadable", "status": None, "marker_state": None, "marker": None}
    mstate, marker = claim_marker.parse(text)
    fm = parse_frontmatter(text)
    status = fm.get("status")
    if (mstate == claim_marker.UNREADABLE or str(fm.get("claim_id") or "").strip('"') != cid
            or status not in _CLAIM_STATUSES):
        return {"state": "unreadable", "status": status, "marker_state": mstate, "marker": None}
    return {"state": "ok", "status": status, "marker_state": mstate, "marker": marker}


def _node_status(gconn, cid: str) -> str | None:
    return (graph.get_node(gconn, cid) or {}).get("status")


def _supersedes_rows(gconn, winner: str, loser: str) -> list[dict[str, Any]]:
    return [dict(r) for r in gconn.execute(
        "SELECT * FROM edges WHERE edge_type = 'supersedes' AND src_id = ? AND dst_id = ? "
        "ORDER BY edge_id", (winner, loser))]


def owned_active_edge(gconn, bind: dict[str, Any]) -> dict[str, Any] | None:
    """This review's active `supersedes` edge winner→loser (owned = written with its review id)."""
    for row in _supersedes_rows(gconn, bind["winner"], bind["loser"]):
        if row["status"] == "active" and row.get("review_id") == bind["review_id"]:
            return row
    return None


def _owned_edge_any_status(gconn, bind: dict[str, Any]) -> dict[str, Any] | None:
    for row in _supersedes_rows(gconn, bind["winner"], bind["loser"]):
        if row.get("review_id") == bind["review_id"]:
            return row
    return None


def _foreign_active_edge(gconn, bind: dict[str, Any]) -> dict[str, Any] | None:
    for row in _supersedes_rows(gconn, bind["winner"], bind["loser"]):
        if row["status"] == "active" and row.get("review_id") != bind["review_id"]:
            return row
    return None


def _marker_for_review(page: dict[str, Any], bind: dict[str, Any]) -> bool:
    """The loser page carries a marker of THIS review (any approval generation)."""
    marker = page.get("marker")
    return bool(marker) and marker.get("review_id") == bind["review_id"]


def _matching_marker(page: dict[str, Any], bind: dict[str, Any]) -> bool:
    return claim_marker.matches(page.get("marker"), review_id=bind["review_id"],
                                approval_id=bind["approval_id"], winner=bind["winner"])


# --- the one effect check (decision 9) --------------------------------------------------------


def effect_state(gconn, reviews_dir: Path, claims_dir: Path, item: dict[str, Any]) -> dict[str, Any]:
    """Where this approved supersede's effects stand, for Apply, the preview and reopen. Read-only;
    never raises on bad data. `{state, reason?}`."""
    bind = binding(item)
    if bind is None or not _canonical(bind):
        return {"state": UNVERIFIABLE, "reason": "invalid_subject"}
    try:
        if identity_journal.read(reviews_dir, bind["review_id"]) is not None:
            return {"state": IN_FLIGHT, "reason": "supersede_in_flight"}
    except identity_journal.JournalUnreadable:
        return {"state": UNVERIFIABLE, "reason": "supersede_journal_unreadable"}
    page = claim_page(claims_dir, bind["loser"])
    if page["state"] == "unreadable" or claim_marker.is_unreadable(page["marker_state"]):
        return {"state": UNVERIFIABLE, "reason": "loser_page_unreadable"}
    edge = owned_active_edge(gconn, bind)
    deprecated = (page["state"] == "ok" and page["status"] == "deprecated_candidate"
                  and _node_status(gconn, bind["loser"]) == "deprecated_candidate")
    if edge is not None:
        marker_ok = (page["marker_state"] == claim_marker.NONE      # a legacy completed effect
                     or _matching_marker(page, bind))
        if deprecated and marker_ok:
            return {"state": COMPLETE, "edge_id": edge["edge_id"]}
        return {"state": PARTIAL, "reason": "supersede_state_unverified"}
    if _marker_for_review(page, bind):
        return {"state": PARTIAL, "reason": "supersede_state_unverified"}
    if _approval_ok(bind):
        state, _rec = read_audit(rollback_audit_path(reviews_dir, bind["review_id"],
                                                     bind["approval_id"]),
                                 event=ROLLED_BACK_EVENT, bind=bind)
        if state == "invalid":
            return {"state": UNVERIFIABLE, "reason": "rollback_audit_conflict"}
        if state == "valid":
            return {"state": WITHDRAWN, "reason": "supersede_withdrawn"}
        cstate, _rec = read_audit(completion_audit_path(reviews_dir, bind["review_id"],
                                                        bind["approval_id"]),
                                  event=COMPLETED_EVENT, bind=bind)
        if cstate != "absent":                   # a completion record without its effects
            return {"state": UNVERIFIABLE, "reason": "completion_audit_conflict"}
    return {"state": ABSENT}


# --- the completion-evidence guard (decision 10, shared with repair_supersede.py) -----------


def completion_evidence(reviews_dir: Path, claims_dir: Path, bind: dict[str, Any],
                        journal: dict[str, Any] | None) -> tuple[str, str] | None:
    """Why a rollback is forbidden, or None when it may run: `("completion_evidence_found", kind)`
    for a matching marker (whatever the page status), a completed stage or this operation's completion
    audit; `("rollback_evidence_unreadable", what)` when any of that evidence can't be read. A
    rollback or repair audit never counts."""
    page = claim_page(claims_dir, bind["loser"])
    if page["state"] == "unreadable" or claim_marker.is_unreadable(page["marker_state"]):
        return "rollback_evidence_unreadable", "loser_page"
    if _matching_marker(page, bind):
        return "completion_evidence_found", "marker"
    if journal is not None and journal.get("stage") == LOSER_DEPRECATED:
        return "completion_evidence_found", "stage"
    state, _rec = read_audit(completion_audit_path(reviews_dir, bind["review_id"],
                                                   bind["approval_id"]),
                             event=COMPLETED_EVENT, bind=bind)
    if state == "valid":
        return "completion_evidence_found", "audit"
    if state == "invalid":
        return "rollback_evidence_unreadable", "completion_audit"
    return None


# --- prerequisites (decision 9 guard rows 2–4) -----------------------------------------------


def prerequisite_problem(gconn, claims_dir: Path, bind: dict[str, Any]) -> tuple[str, dict] | None:
    """Why the supersede can't execute now (`(reason, problem)`), or None: the pair's `contradicts`
    edge stands (`proposed`/`active`), and winner and loser are active in page AND graph."""
    rows = graph.contradiction_between(gconn, bind["a"], bind["b"])
    statuses = sorted({r["status"] for r in rows})
    if not rows:
        return "edge_missing", {}
    if not set(statuses) & {"proposed", "active"}:
        if "superseded" in statuses:
            return "contradiction_retired", {"edge_statuses": statuses}
        return "edge_status_unexpected", {"edge_statuses": statuses,
                                          "expected": ["proposed", "active"]}
    for role in ("winner", "loser"):
        cid = bind[role]
        page = claim_page(claims_dir, cid)
        node = _node_status(gconn, cid)
        if page["state"] != "ok" or claim_marker.is_unreadable(page["marker_state"]):
            return "claim_page_unreadable", {"claim": cid}
        if page["status"] == "active" and node == "active":
            continue
        problem = {"claim": cid, "page_status": page["status"], "node_status": node}
        if role == "winner":
            return "winner_not_active", problem
        if "hidden" in (page["status"], node):
            return ao.HIDDEN_TAKES_PRECEDENCE, problem
        return "loser_not_active", problem
    return None


# --- the executor ----------------------------------------------------------------------------


def _journal_binding_ok(journal: dict[str, Any], bind: dict[str, Any]) -> bool:
    return (journal.get("op") == OP and journal.get("stage") in STAGES
            and journal.get("review_id") == bind["review_id"]
            and journal.get("approval_id") == bind["approval_id"]
            and journal.get("winner") == bind["winner"] and journal.get("loser") == bind["loser"]
            and journal.get("nodes") == sorted([bind["winner"], bind["loser"]]))


def execute(gconn, reviews_dir: Path, item: dict[str, Any], *, claims_dir: Path,
            markdown_dir: Path, now: str | None = None) -> dict[str, Any]:
    """Run one approved supersede through the decision-10 precedence. Returns
    `{outcome, reason, problem, journaled, wrote, executed, information, affected, stage}`:
    `journaled` → the caller ends the journal after publication; `affected` → claims whose pages
    were re-rendered. Commits the graph at each stage boundary (a stage is recorded only after the
    changes it certifies are durable)."""
    now = now or iso_now()
    reviews_dir, claims_dir = Path(reviews_dir), Path(claims_dir)
    bind = binding(item)
    res: dict[str, Any] = {"outcome": ao.SKIPPED, "reason": None, "problem": {}, "journaled": False,
                           "wrote": False, "executed": False, "information": False,
                           "affected": set(), "stage": None, "activated": 0}
    if bind is None or not _canonical(bind):
        res["reason"] = "invalid_subject"
        return res
    res["affected"] = {bind["winner"], bind["loser"]}
    try:
        journal = identity_journal.read(reviews_dir, bind["review_id"])
    except identity_journal.JournalUnreadable:
        res.update(reason="supersede_journal_unreadable", problem={"journal": "unreadable"})
        res["affected"] = set()
        return res
    try:
        if journal is not None:
            return _resume(gconn, reviews_dir, claims_dir, markdown_dir, bind, journal, res, now)
        return _fresh(gconn, reviews_dir, claims_dir, markdown_dir, bind, res, now)
    except Blocked as blk:
        res.update(outcome=ao.INCOMPLETE if res["wrote"] else ao.SKIPPED, reason=blk.reason,
                   problem=blk.problem)
        if not res["wrote"]:
            res["affected"] = set()              # a refusal writes nothing, not even a re-render
        return res


def _fresh(gconn, reviews_dir, claims_dir, markdown_dir, bind, res, now) -> dict[str, Any]:
    """No journal: completed effects first, then the waits and refusals, then a new execution."""
    eff = effect_state(gconn, reviews_dir, claims_dir, {
        "review_id": bind["review_id"], "approval_id": bind["approval_id"], "winner": bind["winner"],
        "subject": {"claim_a": bind["a"], "claim_b": bind["b"]}})
    state = eff["state"]
    if state == COMPLETE:
        res.update(outcome=ao.ALREADY_IN_EFFECT, affected=set())
        return res
    if state == WITHDRAWN:
        res.update(reason="supersede_withdrawn", affected=set())
        return res
    if state in (PARTIAL, UNVERIFIABLE, IN_FLIGHT):
        res.update(reason=eff.get("reason") or "supersede_state_unverified", affected=set())
        return res
    # Verified: nothing of this review's effects exists.
    busy = identity_journal.operation_for(reviews_dir, [bind["winner"], bind["loser"]])
    if busy is not None and busy != bind["review_id"]:
        res.update(reason="identity_operation_in_flight", problem={"blocking_review_id": busy},
                   affected=set())
        return res
    problem = prerequisite_problem(gconn, claims_dir, bind)
    if problem is not None:
        reason, details = problem
        res.update(reason=reason, problem=details, affected=set())
        # decision 15: a retired contradiction with verified-absent effects is information
        res["information"] = reason == "contradiction_retired"
        return res
    if not _approval_ok(bind):
        res.update(reason="approval_unbound", affected=set())
        return res
    if (row := _foreign_active_edge(gconn, bind)) is not None:
        res.update(reason="supersedes_edge_foreign", affected=set(),
                   problem={"edge_id": row["edge_id"], "owner": row.get("review_id")})
        return res
    # --- execute (decision 10 write order) ---
    for r in graph.contradiction_between(gconn, bind["a"], bind["b"]):
        if r["status"] == "proposed":                 # the acknowledgment half of the decision
            graph.set_status(gconn, r["edge_id"], "active", now=now)
            res["activated"] += 1
    gconn.commit()
    identity_journal.begin(reviews_dir, bind["review_id"], op=OP,
                           nodes=[bind["winner"], bind["loser"]], now=now,
                           binding={"winner": bind["winner"], "loser": bind["loser"],
                                    "approval_id": bind["approval_id"]})
    res["wrote"] = True
    journal = identity_journal.read(reviews_dir, bind["review_id"])
    return _run_from(gconn, reviews_dir, claims_dir, markdown_dir, bind, journal, res, now,
                     resumed=False)


def _resume(gconn, reviews_dir, claims_dir, markdown_dir, bind, journal, res, now) -> dict[str, Any]:
    """An open journal for this review: the decision-10 precedence, first match wins."""
    res["stage"] = journal.get("stage")
    res["resumed"] = True
    if not _approval_ok(bind) or not _journal_binding_ok(journal, bind):     # row 1
        raise Blocked("supersede_journal_invalid", {"stage": journal.get("stage")})
    # Validated: repeat the entry's durability barrier before any recovery mutation (an earlier
    # run's flush may have failed after its rename). A failure propagates; nothing advances.
    identity_journal.sync(reviews_dir, bind["review_id"])
    if journal["stage"] == ROLLING_BACK:                                       # decision 17
        return _rollback(gconn, reviews_dir, claims_dir, bind, journal, res, now)
    if journal["stage"] == LOSER_DEPRECATED:                                   # row 2
        return _complete(gconn, reviews_dir, bind, res, now)
    page = claim_page(claims_dir, bind["loser"])
    node = _node_status(gconn, bind["loser"])
    if page["state"] != "ok" or claim_marker.is_unreadable(page["marker_state"]) or node is None:
        raise Blocked("supersede_state_unverified",                           # row 4
                      {"loser_page": page["state"], "node_status": node})
    marked = _matching_marker(page, bind)
    if page["status"] == "deprecated_candidate" and marked:
        # A marked page an earlier run wrote is certified only after its durability barrier is
        # repeated (that run's flush may have failed after the rename); a failure propagates.
        claims.sync_page_durably(claims_dir / f"{bind['loser']}.md")
    if page["status"] == "deprecated_candidate" and marked and node == "active":   # row 3
        graph.upsert_node(gconn, node_id=bind["loser"], node_type="claim", slug=bind["loser"],
                          status="deprecated_candidate", now=now)
        gconn.commit()
        res["wrote"] = True
        identity_journal.set_stage(reviews_dir, bind["review_id"], LOSER_DEPRECATED)
        return _complete(gconn, reviews_dir, bind, res, now)
    if page["status"] == "deprecated_candidate" and node == "deprecated_candidate":
        if marked:                                                             # row 5
            identity_journal.set_stage(reviews_dir, bind["review_id"], LOSER_DEPRECATED)
            res["wrote"] = True
            return _complete(gconn, reviews_dir, bind, res, now)
        raise Blocked("loser_deprecated_unmarked", {})                        # row 6
    if page["status"] == "deprecated_candidate" and not marked:
        raise Blocked("loser_deprecated_unmarked", {"node_status": node})     # row 6
    if page["status"] == "active" and node == "active":                       # row 7
        if prerequisite_problem(gconn, claims_dir, bind) is None:
            return _run_from(gconn, reviews_dir, claims_dir, markdown_dir, bind, journal, res,
                             now, resumed=True)
        refusal = completion_evidence(reviews_dir, claims_dir, bind, journal)
        if refusal is not None:
            raise Blocked(refusal[0], {"evidence": refusal[1]})
        identity_journal.set_stage(reviews_dir, bind["review_id"], ROLLING_BACK)
        res["wrote"] = True
        journal = identity_journal.read(reviews_dir, bind["review_id"])
        return _rollback(gconn, reviews_dir, claims_dir, bind, journal, res, now)
    raise Blocked("supersede_state_unverified",                               # row 4
                  {"page_status": page["status"], "node_status": node})


def _run_from(gconn, reviews_dir, claims_dir, markdown_dir, bind, journal, res, now, *,
              resumed: bool) -> dict[str, Any]:
    """The remaining writes from the journal's stage: edge → stage → marked page → mirror → stage →
    completion audit."""
    rid = bind["review_id"]
    if journal.get("stage") == STARTED or owned_active_edge(gconn, bind) is None:
        graph.upsert_assertion(gconn, src_id=bind["winner"], dst_id=bind["loser"],
                               edge_type="supersedes", asserted_by="human", status="active",
                               review_id=rid, now=now)
        gconn.commit()
        res["wrote"] = True
        identity_journal.set_stage(reviews_dir, rid, EDGE_WRITTEN)
    marker = {"review_id": rid, "approval_id": bind["approval_id"], "winner": bind["winner"]}
    outcome = claims.recompose_claim(
        gconn, cid=bind["loser"], claims_dir=claims_dir, reviews_dir=reviews_dir,
        markdown_dir=markdown_dir, now=now, deprecate=True, supersede_marker=marker, durable=True)
    if outcome != "written":
        raise Blocked(outcome if outcome in ("marker_unreadable", "marker_conflict")
                      else ao.POSTCONDITION_NOT_MET, {"loser_render": outcome})
    gconn.commit()                                       # the graph mirror, after the page
    res["wrote"] = True
    page = claim_page(claims_dir, bind["loser"])
    if not (page["state"] == "ok" and page["status"] == "deprecated_candidate"
            and _matching_marker(page, bind)
            and _node_status(gconn, bind["loser"]) == "deprecated_candidate"):
        raise Blocked(ao.POSTCONDITION_NOT_MET, {"loser_status": page["status"]})
    identity_journal.set_stage(reviews_dir, rid, LOSER_DEPRECATED)
    res["executed"] = not resumed
    return _complete(gconn, reviews_dir, bind, res, now)


def _complete(gconn, reviews_dir, bind, res, now) -> dict[str, Any]:
    """Audit-only completion (the loser is not touched)."""
    edge = owned_active_edge(gconn, bind) or _owned_edge_any_status(gconn, bind)
    if edge is None:
        raise Blocked("supersede_state_unverified", {"edge": "missing"})
    _ensure_audit(completion_audit_path(reviews_dir, bind["review_id"], bind["approval_id"]),
                  event=COMPLETED_EVENT, time_key="completed_at", bind=bind,
                  edge_id=edge["edge_id"], now=now)
    res.update(outcome=ao.APPLIED, journaled=True, stage=LOSER_DEPRECATED)
    return res


def _rollback(gconn, reviews_dir, claims_dir, bind, journal, res, now) -> dict[str, Any]:
    """Decision 17 steps 2–3 (step 1, the `rolling_back` stage, is already durable): the guard
    again, then only the owned edge superseded, then the rollback audit. The caller closes the
    journal after publication (step 4)."""
    refusal = completion_evidence(reviews_dir, claims_dir, bind, journal)
    if refusal is not None:
        raise Blocked(refusal[0], {"evidence": refusal[1]})
    edge = _owned_edge_any_status(gconn, bind)
    if edge is not None and edge["status"] == "active":
        graph.set_status(gconn, edge["edge_id"], "superseded", now=now)
        gconn.commit()
        res["wrote"] = True
    edge_id = edge["edge_id"] if edge is not None else "none"
    _ensure_audit(rollback_audit_path(reviews_dir, bind["review_id"], bind["approval_id"]),
                  event=ROLLED_BACK_EVENT, time_key="rolled_back_at", bind=bind, edge_id=edge_id,
                  now=now)
    res.update(outcome=ao.INCOMPLETE, reason="partial_supersede_withdrawn", journaled=True,
               stage=ROLLING_BACK, wrote=True)
    return res


# --- read-only helpers for other executors (decision 14) -------------------------------------


def claim_in_flight(reviews_dir: Path, cid: str) -> str | None:
    """The review id of an open identity operation touching claim `cid` (an unreadable journal
    touches every claim), else None. Claim governance execution waits on it."""
    return identity_journal.operation_for(reviews_dir, [cid])
