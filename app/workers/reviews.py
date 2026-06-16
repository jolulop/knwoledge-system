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
    "delete_raw_file", "archive_raw_file", "deprecate_wiki_page", "resolve_contradiction",
    "merge_entities", "split_entity", "merge_concepts", "mark_semantic_duplicate",
    "promote_single_source_claim_to_concept", "hide_content",
    "promote_candidate_node", "change_entity_subtype",
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
    now: str | None = None,
) -> bool:
    """Resolve a pending review item to approved/rejected and append an audit-log entry.

    Idempotent: if the item is already in `approved/`/`rejected/` (e.g. a promotion rerun),
    it is a no-op and no duplicate audit entry is written. Returns True if it resolved this
    call, False if it was already resolved or not pending.
    """
    if decision not in ("approved", "rejected"):
        raise ValueError(f"decision must be approved|rejected, got {decision!r}")
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
    item.update(status=decision, decided_by=decided_by, decided_at=now, decision_note=note)
    dest_dir = reviews_dir / decision
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / f"{review_id}.json").write_text(
        json.dumps(item, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    src.unlink()
    audit = reviews_dir / "audit_log"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / f"{review_id}-{decision}.json").write_text(json.dumps({
        "review_id": review_id, "type": item.get("type"), "decision": decision,
        "decided_by": decided_by, "decided_at": now, "subject": item.get("subject"),
        "note": note,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True
