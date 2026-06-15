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
