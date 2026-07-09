#!/usr/bin/env python3
"""Human-review items on disk (`reviews/pending/`, ADR-0018, policies/review.yaml).

Semantic and destructive changes are proposed, never executed (CLAUDE.md rule 9): a worker
files a pending review item; a human later approves/rejects it. Items are JSON, one file per
item at `reviews/pending/<review_id>.json`, with `review_id` derived from `(type, subject)`
so re-runs are idempotent (an item already filed — pending, approved, or rejected — is not
re-created, so a rejected proposal does not keep reappearing). A *deferred* decision keeps
the item in `pending/` with `status: deferred` (the review dirs are
pending/approved/rejected/audit_log — there is no `deferred/`), so the same `pending/` check
makes deferred items idempotent too. Dependency-free.

The allowed `type`s mirror `policies/review.yaml` `requires_human_approval` and must stay in
sync with it.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from app.backend.manifests import iso_now

REVIEW_TYPES = frozenset({
    "delete_raw_file", "archive_source", "deprecate_wiki_page", "resolve_contradiction",
    "merge_items", "split_item", "mark_semantic_duplicate",
    "hide_content", "hide_semantic_page", "unhide_content", "unhide_semantic_page",
    "hide_claim", "unhide_claim",
    "hide_synthesis", "unhide_synthesis",
    "promote_candidate_node", "change_item_type", "propose_synthesis",
    # Phase 7 maintenance (ADR-0036): a catalogued raw file gone missing — record-only governance
    # of a broken source record (high-severity lint finding; never auto-remediated).
    "missing_raw_source",
    # Phase 7: one aggregate, record-only proposal to manually purge the LLM response cache when it
    # exceeds policy bounds — NO executor (a bulk purge forfeits reproducibility, ADR-0027).
    "purge_response_cache",
})
REVIEW_STATUSES = frozenset({"pending", "approved", "rejected", "deferred"})
PRIORITIES = frozenset({"low", "medium", "high"})

_STATE_DIRS = ("pending", "approved", "rejected")


def review_id(review_type: str, subject: dict[str, Any]) -> str:
    key = review_type + "|" + json.dumps(subject, sort_keys=True, ensure_ascii=False)
    return "rev_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def create_review_item(
    reviews_dir: Path,
    *,
    review_type: str,
    subject: dict[str, Any],
    proposal: dict[str, Any],
    priority: str = "low",
    context: dict[str, Any] | None = None,
    now: str | None = None,
) -> str:
    """File a pending review item (idempotent); return its review_id."""
    if review_type not in REVIEW_TYPES:
        raise ValueError(f"unknown review type {review_type!r}; allowed: {sorted(REVIEW_TYPES)}")
    if priority not in PRIORITIES:
        raise ValueError(f"unknown priority {priority!r}; allowed: {sorted(PRIORITIES)}")
    reviews_dir = Path(reviews_dir)
    rid = review_id(review_type, subject)
    # Idempotent: don't re-file an item already pending/approved/rejected.
    for state in _STATE_DIRS:
        if (reviews_dir / state / f"{rid}.json").exists():
            return rid
    pending = reviews_dir / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (pending / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": review_type, "status": "pending", "priority": priority,
        "created_at": now or iso_now(), "subject": subject, "proposal": proposal,
        "context": context or {},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return rid


def withdraw_review_item(
    reviews_dir: Path, review_id: str, *, reason: str = "", now: str | None = None
) -> bool:
    """Withdraw a still-**pending** machine-proposed item (it never reached a human).

    Removes the pending file and writes an audit entry, so a later re-detection can re-file the
    same `review_id` (unlike a rejected item, which stays and blocks re-filing). Only touches
    `pending/` — an already-decided item (approved/rejected) is a human record and is left
    intact. Used when a proposed contradiction stops being a candidate or is no longer judged a
    contradiction (Phase 3.5c). Returns True if it withdrew this call, False otherwise.
    """
    reviews_dir = Path(reviews_dir)
    src = reviews_dir / "pending" / f"{review_id}.json"
    if not src.exists():
        return False
    now = now or iso_now()
    try:
        item = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        item = {"review_id": review_id}
    src.unlink()
    audit = reviews_dir / "audit_log"
    audit.mkdir(parents=True, exist_ok=True)
    # Unique suffix: a withdraw/re-file/withdraw cycle for the same review_id must not overwrite
    # earlier audit entries (unlike a terminal approve/reject, a withdrawal can recur).
    (audit / f"{review_id}-withdrawn-{uuid.uuid4().hex[:8]}.json").write_text(json.dumps({
        "review_id": review_id, "type": item.get("type"), "decision": "withdrawn",
        "decided_by": "system", "decided_at": now, "subject": item.get("subject"),
        "note": reason,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def resolve_review_item(
    reviews_dir: Path,
    review_id: str,
    *,
    decision: str,
    decided_by: str = "auto",
    note: str = "",
    winner: str | None = None,
    amendments: dict[str, Any] | None = None,
    now: str | None = None,
) -> bool:
    """Resolve a pending review item to approved/rejected and append an audit-log entry.

    Idempotent: if the item is already in `approved/`/`rejected/` (e.g. a promotion rerun),
    it is a no-op and no duplicate audit entry is written. Returns True if it resolved this
    call, False if it was already resolved or not pending.

    `winner` (ADR-0044): a contradiction supersede sub-outcome. When present it is persisted **both**
    onto the approved item (`item["winner"]`, which `apply_resolved_contradictions` consumes) and into
    the terminal audit entry — conditionally, so ordinary approvals/rejections are unchanged. The caller
    validates that `winner` is valid (approve of a resolve_contradiction, in the pair, claims active).

    `amendments` (ADR-0058): the approve-with-amendments payload (`title`/`aliases`/`description`),
    persisted immutably onto the approved item (the promote executor consumes it at flip-to-active) and
    into the audit entry — same conditional shape as `winner`. The caller validates it (approve of a
    promote_candidate_node only, allowed fields only). Any mutable `draft_amendments` on the pending
    file is dropped here: frozen into `amendments` on approve, discarded on reject.
    """
    if decision not in ("approved", "rejected"):
        raise ValueError(f"decision must be approved|rejected, got {decision!r}")
    if winner is not None and decision != "approved":  # defensive: winner is an approve-only sub-outcome
        raise ValueError(f"winner is only valid for an approved decision, got {decision!r}")
    if amendments is not None and decision != "approved":  # defensive: amendments ride approvals only
        raise ValueError(f"amendments are only valid for an approved decision, got {decision!r}")
    reviews_dir = Path(reviews_dir)
    # Already resolved -> idempotent no-op (no duplicate audit).
    if (reviews_dir / "approved" / f"{review_id}.json").exists() or \
       (reviews_dir / "rejected" / f"{review_id}.json").exists():
        return False
    src = reviews_dir / "pending" / f"{review_id}.json"
    if not src.exists():
        return False
    now = now or iso_now()
    item = json.loads(src.read_text(encoding="utf-8"))
    item.pop("draft_amendments", None)  # ADR-0058: drafts never survive a terminal decision
    item.update(status=decision, decided_by=decided_by, decided_at=now, decision_note=note)
    if winner is not None:
        item["winner"] = winner
    if amendments is not None:
        item["amendments"] = amendments
    dest_dir = reviews_dir / decision
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / f"{review_id}.json").write_text(
        json.dumps(item, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    src.unlink()
    audit = reviews_dir / "audit_log"
    audit.mkdir(parents=True, exist_ok=True)
    audit_entry = {
        "review_id": review_id, "type": item.get("type"), "decision": decision,
        "decided_by": decided_by, "decided_at": now, "subject": item.get("subject"),
        "note": note,
    }
    if winner is not None:
        audit_entry["winner"] = winner
    if amendments is not None:
        audit_entry["amendments"] = amendments
    (audit / f"{review_id}-{decision}.json").write_text(
        json.dumps(audit_entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def reopen_review_item(
    reviews_dir: Path,
    review_id: str,
    *,
    reason: str,
    now: str | None = None,
) -> bool:
    """Reopen a terminal item: move `approved/`|`rejected/` -> `pending/`, clear the decision fields + the
    ADR-0044 `winner`, and append a sequence-suffixed reopened audit entry capturing the prior decision
    (ADR-0045). Returns True iff it reopened this call (False if the item is not in a terminal dir).

    The CALLER gates on the projector `effect_status` (only PENDING_APPLY / NO_EFFECT_REQUIRED reach here)
    and requires a non-empty `reason`; this primitive just performs the ledger transition + the audit.
    """
    reviews_dir = Path(reviews_dir)
    src: Path | None = None
    prior_status: str | None = None
    for decision in ("approved", "rejected"):
        candidate = reviews_dir / decision / f"{review_id}.json"
        if candidate.exists():
            src, prior_status = candidate, decision
            break
    if src is None:
        return False
    now = now or iso_now()
    item = json.loads(src.read_text(encoding="utf-8"))
    prior: dict[str, Any] = {
        "prior_status": prior_status,
        "prior_decided_by": item.get("decided_by"),
        "prior_decided_at": item.get("decided_at"),
    }
    if item.get("winner") is not None:
        prior["prior_winner"] = item["winner"]
    # Clear terminal fields + the supersede winner; back to pending (re-decidable).
    for field in ("decided_by", "decided_at", "decision_note", "winner"):
        item.pop(field, None)
    item["status"] = "pending"
    dest = reviews_dir / "pending" / f"{review_id}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(item, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    src.unlink()
    # Sequence-suffixed so repeated decide->reopen cycles never clobber prior reopened audit (ADR-0045).
    audit = reviews_dir / "audit_log"
    audit.mkdir(parents=True, exist_ok=True)
    seq = 1 + len(list(audit.glob(f"{review_id}-reopened-*.json")))
    (audit / f"{review_id}-reopened-{seq}.json").write_text(json.dumps({
        "review_id": review_id, "type": item.get("type"), "event": "reopened",
        "reopened_at": now, "reason": reason, **prior,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def defer_review_item(
    reviews_dir: Path,
    review_id: str,
    *,
    decided_by: str = "human",
    note: str = "",
    draft_amendments: dict[str, Any] | None = None,
    now: str | None = None,
) -> bool:
    """Defer a still-pending item: keep it in `pending/` with `status: deferred` + an audit entry.

    A deferral is explicitly *not* terminal — there is no `deferred/` dir. The item stays in
    `pending/` so it can still be approved/rejected later (`resolve_review_item` finds it there); the
    explicit `status` field is what marks it deferred, so `GET /reviews?status=pending` excludes it
    while `?status=deferred` surfaces it (ADR-0035 A3). Idempotent: a no-op (no duplicate audit) when
    the item is already deferred, already resolved (approved/rejected), or not pending. Returns True
    iff it deferred this call. `resolve_review_item` only does approved|rejected, so defer is its own
    review-service primitive (Phase 6 slice 6-2).

    `draft_amendments` (ADR-0058): typed-but-undecided amendments preserved on the pending file so the
    reviewer returns to the same edited form. MUTABLE (unlike everything else on the ledger): a re-defer
    with a new draft updates it in place even when already deferred (that update alone returns False —
    no status transition, no duplicate audit). Excluded from identity by construction (`review_id`
    hashes only type|subject), never read by executors; `resolve_review_item` drops it on any terminal
    decision.
    """
    reviews_dir = Path(reviews_dir)
    # An already-resolved item is a terminal human record — not deferrable.
    if (reviews_dir / "approved" / f"{review_id}.json").exists() or \
       (reviews_dir / "rejected" / f"{review_id}.json").exists():
        return False
    src = reviews_dir / "pending" / f"{review_id}.json"
    if not src.exists():
        return False
    item = json.loads(src.read_text(encoding="utf-8"))
    if item.get("status") == "deferred":
        if draft_amendments is not None and item.get("draft_amendments") != draft_amendments:
            item["draft_amendments"] = draft_amendments
            src.write_text(json.dumps(item, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return False  # idempotent: already deferred, no duplicate audit
    now = now or iso_now()
    if draft_amendments is not None:
        item["draft_amendments"] = draft_amendments
    item.update(status="deferred", decided_by=decided_by, decided_at=now, decision_note=note)
    src.write_text(json.dumps(item, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit = reviews_dir / "audit_log"
    audit.mkdir(parents=True, exist_ok=True)
    # A deferral is non-terminal (the item may later be re-opened to a real decision), so use a
    # unique suffix rather than overwriting — mirrors `withdraw_review_item`.
    (audit / f"{review_id}-deferred-{uuid.uuid4().hex[:8]}.json").write_text(json.dumps({
        "review_id": review_id, "type": item.get("type"), "decision": "deferred",
        "decided_by": decided_by, "decided_at": now, "subject": item.get("subject"),
        "note": note,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True
