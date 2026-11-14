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

import functools
import hashlib
import json
import logging
import os
import re
import uuid
import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.backend import authority_read, durable_write, write_lock
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

# ADR-0073: the digest of the synthesis version a decision was recorded for (synthesis_version.DECIDED).
DECIDED_VERSION = "decided_version_digest"


logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- typed record reads
# ADR-0074 decision 4 — the review-record family. A record is usable only when it is a JSON object
# whose `review_id` equals its file name, with a string `type` and an object `subject`; anything else
# is `corrupt` (bad UTF-8 / JSON, not an object, missing identity, type or subject). An unsupported
# `type` is NOT damage: it is a readable record no executor handles (reported as such).


@dataclass(frozen=True)
class ReviewRead:
    state: str
    path: Path
    record: dict[str, Any] | None = None
    error: str | None = None


def read_review_record(path: Path) -> ReviewRead:
    """The typed read of one review record (never raises for the file's state)."""
    read = authority_read.read_file(path)
    if read.state != authority_read.OK:
        return ReviewRead(read.state, read.path, error=read.error)
    try:
        record = json.loads(read.data.decode("utf-8"))
    except UnicodeDecodeError:
        return ReviewRead(authority_read.CORRUPT, read.path, error="invalid_utf8")
    except (ValueError, RecursionError):
        return ReviewRead(authority_read.CORRUPT, read.path, error="invalid_json")
    if not isinstance(record, dict):
        return ReviewRead(authority_read.CORRUPT, read.path, error="not_an_object")
    if record.get("review_id") != read.path.stem:
        return ReviewRead(authority_read.CORRUPT, read.path,
                          error="missing_review_id" if not record.get("review_id") else "id_mismatch")
    if not isinstance(record.get("type"), str) or not record["type"]:
        return ReviewRead(authority_read.CORRUPT, read.path, error="missing_type")
    if (problem := record_schema_problem(record)) is not None:
        return ReviewRead(authority_read.CORRUPT, read.path, error=problem)
    return ReviewRead(authority_read.OK, read.path, record=record)


# The subject fields every supported review type is filed with — and its executor needs. A record of
# a supported type without them can't be executed or attributed; an unsupported type is checked only
# for its containers (a readable record no executor handles).
SUBJECT_FIELDS: dict[str, tuple[str, ...]] = {
    "delete_raw_file": ("source_id",), "archive_source": ("source_id",),
    "missing_raw_source": ("source_id",),
    "hide_content": ("source_id",), "unhide_content": ("source_id",),
    "deprecate_wiki_page": ("node_id", "page"),
    "hide_semantic_page": ("node_id", "page"), "unhide_semantic_page": ("node_id", "page"),
    "hide_claim": ("node_id", "page"), "unhide_claim": ("node_id", "page"),
    "hide_synthesis": ("node_id", "page"), "unhide_synthesis": ("node_id", "page"),
    "resolve_contradiction": ("claim_a", "claim_b"),
    "merge_items": ("survivor_node_id", "absorbed_node_id"),
    "split_item": ("node_id",),
    "promote_candidate_node": ("node_id",),
    "change_item_type": ("node_id", "to_item_type"),
    "propose_synthesis": ("topic_node_id", "fingerprint"),
    "purge_response_cache": ("scope",),
}


def record_schema_problem(record: dict[str, Any]) -> str | None:
    """The first shape problem of a review record, or None (ADR-0074 slice 3b): `status` a
    non-empty string, `subject` an object, `proposal` / `context` objects when present, and — for a
    supported type — every required subject field a non-empty string (`node_ids` of a semantic
    duplicate: two ids)."""
    if not isinstance(record.get("status"), str) or not record["status"]:
        return "missing_status"
    subject = record.get("subject")
    if not isinstance(subject, dict):
        return "missing_subject"
    for name in ("proposal", "context"):
        if name in record and not isinstance(record[name], dict):
            return f"invalid_{name}"
    rtype = record["type"]
    if rtype == "mark_semantic_duplicate":
        ids = subject.get("node_ids")
        if not (isinstance(ids, list) and len(ids) == 2
                and all(isinstance(i, str) and i for i in ids)):
            return "invalid_subject"
    for name in SUBJECT_FIELDS.get(rtype, ()):
        if not isinstance(subject.get(name), str) or not subject[name]:
            return "invalid_subject"
    return None


def read_audit_record(path: Path, review_id: str, **expected: Any) -> ReviewRead:
    """The typed read of an audit entry this operation is about to rely on: a JSON object for
    `review_id` whose fields equal `expected` — else `corrupt` (`audit_mismatch` when readable but
    not this operation's). Audit entries are append-only evidence: a damaged one is preserved and
    the operation that would rely on it refuses (ADR-0074 slice 3b)."""
    read = authority_read.read_file(path)
    if read.state != authority_read.OK:
        return ReviewRead(read.state, read.path, error=read.error)
    try:
        record = json.loads(read.data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return ReviewRead(authority_read.CORRUPT, read.path, error="invalid_json")
    if not isinstance(record, dict):
        return ReviewRead(authority_read.CORRUPT, read.path, error="not_an_object")
    if record.get("review_id") != review_id or any(record.get(k) != v for k, v in expected.items()):
        return ReviewRead(authority_read.CORRUPT, read.path, error="audit_mismatch")
    return ReviewRead(authority_read.OK, read.path, record=record)


def _reuse_audit(entry: Path, review_id: str, **expected: Any) -> bool:
    """A retry's existing keyed audit entry: True (flushed again, safe to rely on) when it is this
    operation's readable entry, False when there is none; a damaged or foreign one raises
    :class:`ReviewRecordUnavailable` and is never overwritten."""
    if not _present(entry):
        return False
    read = read_audit_record(entry, review_id, **expected)
    if read.state != authority_read.OK:
        raise ReviewRecordUnavailable(review_id, read)
    durable_write.sync_existing(entry)
    return True


@dataclass
class ReviewScan:
    """One `reviews/<state>/` directory, read: the usable records, every damaged file (safely
    rendered relative path, state, categorical error), and whether the listing itself failed."""
    state_dir: str
    records: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    damaged: list[dict[str, str]] = dataclasses.field(default_factory=list)
    enumeration_error: str | None = None
    # Review ids also in `pending/`: an interrupted decision or reopen (slice 3b, review round 2).
    interrupted: list[dict[str, str]] = dataclasses.field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.enumeration_error is None and not self.damaged and not self.interrupted

    def problems(self) -> dict[str, Any]:
        return {"directory": f"reviews/{self.state_dir}", "damaged": self.damaged,
                "enumeration_error": self.enumeration_error, "interrupted": self.interrupted}


def scan_review_dir(reviews_dir: Path, state_dir: str) -> ReviewScan:
    scan = ReviewScan(state_dir)
    paths, scan.enumeration_error = authority_read.list_entries(Path(reviews_dir) / state_dir, ".json")
    for path in paths:
        read = read_review_record(path)
        if read.state == authority_read.OK:
            scan.records.append(read.record)
        elif read.state != authority_read.ABSENT:            # gone between listing and read: skip
            scan.damaged.append({"path": f"reviews/{state_dir}/{authority_read.safe_name(path.name)}",
                                 "state": read.state, "error": read.error or read.state})
    return scan


class ReviewRecordsUnavailable(RuntimeError):
    """An operation that reads a whole review directory was refused: a record in it is damaged, or
    the directory can't be listed (ADR-0074 decision 4.4 — its subject is unknown, so no other
    decision can be shown independent of it). The message is categorical."""

    def __init__(self, scan: ReviewScan) -> None:
        self.scan = scan
        if scan.enumeration_error:
            what = f"can't be listed ({scan.enumeration_error}); restore it from backup first"
        elif scan.damaged:
            what = (f"holds {len(scan.damaged)} unreadable or corrupt record(s); restore them from "
                    "backup first")
        else:
            what = (f"holds {len(scan.interrupted)} decision(s) or reopen(s) that were interrupted; "
                    "open each review and repeat the action to finish it")
        super().__init__(f"reviews/{scan.state_dir} {what}")


def approved_preflight(reviews_dir: Path) -> ReviewScan:
    """The Apply preflight (ADR-0074 decision 4, slice 3b): every approved record must be readable
    and well-formed, `approved/` fully listable, and no review id at once in `pending/` and a
    terminal directory (an interrupted decision or reopen), before any executor runs — else
    :class:`ReviewRecordsUnavailable`. Returns the scan."""
    scan = scan_review_dir(reviews_dir, "approved")
    if scan.enumeration_error is None and not scan.damaged:
        # Slice 3b review round 2: an id still in `pending/` is a decision or a reopen that was
        # interrupted — a reopened (revoked) decision must never execute, so Apply waits for it.
        scan.interrupted, err = interrupted_transitions(reviews_dir)
        if err is not None:
            scan.enumeration_error = err
    if not scan.complete:
        raise ReviewRecordsUnavailable(scan)
    return scan


class ReviewRecordUnavailable(RuntimeError):
    """A human action on one review was refused: its record (or a terminal record with the same id)
    is unreadable or corrupt. Nothing was written; restore the record from backup."""

    def __init__(self, review_id: str, read: ReviewRead) -> None:
        super().__init__(f"review {review_id} can't be used ({read.state}: {read.error}); nothing "
                         "was recorded — restore the record from backup")
        self.review_id, self.state, self.error = review_id, read.state, read.error


def _present(path: Path) -> bool:
    """Whether an entry is at `path`, failing closed: anything but a verified absence is present."""
    return authority_read.lexists(path) is not False


def _load(path: Path, review_id: str) -> dict[str, Any]:
    """The usable record at `path`, or :class:`ReviewRecordUnavailable` (never a partial dict)."""
    read = read_review_record(path)
    if read.state != authority_read.OK:
        raise ReviewRecordUnavailable(review_id, read)
    return read.record


def _terminal(reviews_dir: Path, review_id: str) -> bool:
    """Whether the review is already decided (approved or rejected); a damaged terminal record
    raises — a human action never proceeds over a decision it can't read."""
    for d in ("approved", "rejected"):
        path = Path(reviews_dir) / d / f"{review_id}.json"
        if _present(path):
            _load(path, review_id)
            return True
    return False


# --------------------------------------------------------------------------- decision transitions
# ADR-0074 slice 3b, review round 2. A decision (approve / reject) and a reopen each move one record
# between `pending/` and a terminal directory, in one order: (1) the audit entry, create-only and
# named by the decision's generation, so no change is ever without its audit and no earlier entry is
# overwritten; (2) the destination record; (3) the source record, removed durably (unlink + directory
# sync). An interruption after (2) leaves the id in `pending/` AND a terminal directory; the pending
# copy of a reopen names the decision it reopened (`reopened_from`), which tells the two apart, and
# the next human action on the review finishes it — Apply's preflight refuses until then.
# An interruption between (1) and (2) leaves an audit entry for an attempt that changed no record:
# kept as evidence of the attempt (the records are the state; the entry's generation matches none).

REOPENED_FROM = "reopened_from"


def decision_generation(record: dict[str, Any]) -> str:
    """The identity of one terminal decision: its `decision_id` (every decision recorded since
    slice 3b review round 2), else its `approval_id` (approvals since ADR-0070 B4), else a digest
    of the record (older decisions — a terminal record never changes while it stands)."""
    for key in ("decision_id", "approval_id"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    canon = json.dumps(record, sort_keys=True, ensure_ascii=False)
    return "legacy-" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _conflict(review_id: str, path: Path, error: str) -> ReviewRecordUnavailable:
    return ReviewRecordUnavailable(review_id, ReviewRead(authority_read.CORRUPT, path, error=error))


def finish_interrupted(reviews_dir: Path, review_id: str) -> str | None:
    """Finish a decision or a reopen that was interrupted after writing its destination (the id is
    in `pending/` and in a terminal directory). Returns "reopen" / "decision" when it finished one,
    None when nothing was interrupted. A reopen's pending copy names the generation it reopened: the
    terminal record of that generation is removed. Otherwise the decision stands and the pending
    copy is removed. The kept record's durability barrier is repeated first (the earlier run may
    have failed during it). Records that disagree on type or subject, or the id in both terminal
    directories next to a pending copy, refuse (:class:`ReviewRecordUnavailable`); nothing moves.
    The caller holds the write lock."""
    reviews_dir = Path(reviews_dir)
    pending = reviews_dir / "pending" / f"{review_id}.json"
    if not _present(pending):
        return None
    terminals = {d: reviews_dir / d / f"{review_id}.json" for d in ("approved", "rejected")
                 if _present(reviews_dir / d / f"{review_id}.json")}
    if not terminals:
        return None
    if len(terminals) != 1:
        raise _conflict(review_id, pending, "decided_in_two_places")
    (_status, terminal), = terminals.items()
    open_rec, decided = _load(pending, review_id), _load(terminal, review_id)
    if open_rec.get("type") != decided.get("type") or open_rec.get("subject") != decided.get("subject"):
        raise _conflict(review_id, pending, "conflicting_pending_record")
    marker = open_rec.get(REOPENED_FROM)
    if isinstance(marker, dict) and marker.get("generation") == decision_generation(decided):
        durable_write.sync_existing(pending)
        durable_write.remove(terminal)
        logger.warning("review %s: finished an interrupted reopen", review_id)
        return "reopen"
    durable_write.sync_existing(terminal)
    durable_write.remove(pending)
    logger.warning("review %s: finished an interrupted decision", review_id)
    return "decision"


def interrupted_transitions(reviews_dir: Path) -> tuple[list[dict[str, str]], str | None]:
    """Review ids in `pending/` and a terminal directory at once — an interrupted decision or
    reopen that Apply must not execute over (`(entries, enumeration error)`; listing only)."""
    reviews_dir = Path(reviews_dir)
    pending, err = authority_read.list_entries(reviews_dir / "pending", ".json")
    if err is not None:
        return [], f"pending: {err}"
    open_ids = {p.stem for p in pending}
    out: list[dict[str, str]] = []
    for d in ("approved", "rejected"):
        decided, err = authority_read.list_entries(reviews_dir / d, ".json")
        if err is not None:
            return [], f"{d}: {err}"
        for p in decided:
            if p.stem in open_ids:
                out.append({"path": f"reviews/{d}/{authority_read.safe_name(p.name)}",
                            "also": f"reviews/pending/{authority_read.safe_name(p.name)}"})
    return out, None


def _committed(fn):
    """Each review-item state change is one commit phase under the vault write lock (ADR-0068
    decision 2): its check of the current state and its file moves never interleave with another
    decision, a reopen or Apply."""
    @functools.wraps(fn)
    def wrapper(reviews_dir: Path, *args: Any, **kwargs: Any) -> Any:
        with write_lock.hold(Path(reviews_dir)):
            return fn(reviews_dir, *args, **kwargs)
    return wrapper


# A pending review carrying this key is being retired by contradiction maintenance (ADR-0069 slice
# 2(a)): its edge may already be superseded, so no human decision may be recorded on it.
RETIRING = "retiring"


class ReviewRetiring(RuntimeError):
    """A decision was refused: the item is being retired by contradiction maintenance."""

    def __init__(self, review_id: str) -> None:
        super().__init__(f"review {review_id} is being retired by contradiction maintenance; "
                         "no decision was recorded")
        self.review_id = review_id


def is_retiring(item: dict[str, Any]) -> bool:
    return isinstance(item.get(RETIRING), dict)


def review_id(review_type: str, subject: dict[str, Any]) -> str:
    key = review_type + "|" + json.dumps(subject, sort_keys=True, ensure_ascii=False)
    return "rev_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


@_committed
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
        if _present(reviews_dir / state / f"{rid}.json"):   # damaged counts: never re-filed over
            return rid
    durable_write.write_json(reviews_dir / "pending" / f"{rid}.json", {
        "review_id": rid, "type": review_type, "status": "pending", "priority": priority,
        "created_at": now or iso_now(), "subject": subject, "proposal": proposal,
        "context": context or {},
    })
    return rid


@_committed
def withdraw_review_item(
    reviews_dir: Path, review_id: str, *, reason: str = "", now: str | None = None,
    audit_key: str | None = None,
) -> bool:
    """Withdraw a still-**pending** machine-proposed item (it never reached a human).

    Writes an audit entry, then removes the pending file, so a later re-detection can re-file the
    same `review_id` (unlike a rejected item, which stays and blocks re-filing). The audit is
    written first (atomically), so an interruption never leaves a deleted item without its record.
    Only touches `pending/` — an already-decided item (approved/rejected) is a human record and is
    left intact. Used when a proposed contradiction stops being a candidate or is no longer judged a
    contradiction (Phase 3.5c). `audit_key` names the audit entry deterministically (contradiction
    maintenance, ADR-0069: a retried retirement finds and keeps its first entry); otherwise each
    withdrawal gets a fresh one. Returns True if it withdrew this call, False otherwise.
    """
    reviews_dir = Path(reviews_dir)
    finish_interrupted(reviews_dir, review_id)
    src = reviews_dir / "pending" / f"{review_id}.json"
    if not _present(src):
        return False
    now = now or iso_now()
    read = read_review_record(src)
    if read.state != authority_read.OK:
        # ADR-0074 decision 4: a damaged record is never withdrawn (deleted) on a guess; it stays for
        # the operator (the review screens count it) and the caller sees "not withdrawn".
        logger.warning("review %s not withdrawn: its record is %s (%s)", review_id, read.state,
                       read.error)
        return False
    item = read.record
    audit = reviews_dir / "audit_log"
    durable_write.make_dirs(audit)
    # Unique suffix: a withdraw/re-file/withdraw cycle for the same review_id must not overwrite
    # earlier audit entries (unlike a terminal approve/reject, a withdrawal can recur).
    entry = audit / f"{review_id}-withdrawn-{audit_key or uuid.uuid4().hex[:8]}.json"
    # A retried keyed withdrawal keeps its first entry — validated and flushed again before the item
    # is deleted; a damaged one is preserved and the withdrawal refused (ReviewRecordUnavailable).
    if not _reuse_audit(entry, review_id, decision="withdrawn"):
        durable_write.write_json(entry, {   # durable before the item is deleted
            "review_id": review_id, "type": item.get("type"), "decision": "withdrawn",
            "decided_by": "system", "decided_at": now, "subject": item.get("subject"),
            "note": reason,
        })
    durable_write.remove(src)
    return True


@_committed
def resolve_review_item(
    reviews_dir: Path,
    review_id: str,
    *,
    decision: str,
    decided_by: str = "auto",
    note: str = "",
    winner: str | None = None,
    amendments: dict[str, Any] | None = None,
    version_digest: str | None = None,
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

    `version_digest` (ADR-0073 decision 4): for a `propose_synthesis`, the digest of the version the
    human was shown, recorded as `decided_version_digest` on the item and its audit entry (Apply
    executes the decision only on that version). The caller checks it against the artifact.
    """
    if decision not in ("approved", "rejected"):
        raise ValueError(f"decision must be approved|rejected, got {decision!r}")
    if winner is not None and decision != "approved":  # defensive: winner is an approve-only sub-outcome
        raise ValueError(f"winner is only valid for an approved decision, got {decision!r}")
    if amendments is not None and decision != "approved":  # defensive: amendments ride approvals only
        raise ValueError(f"amendments are only valid for an approved decision, got {decision!r}")
    reviews_dir = Path(reviews_dir)
    # A decision or reopen an interruption left half-done is finished first (slice 3b round 2).
    finish_interrupted(reviews_dir, review_id)
    # Already resolved -> idempotent no-op (no duplicate audit).
    if _terminal(reviews_dir, review_id):
        return False
    src = reviews_dir / "pending" / f"{review_id}.json"
    if not _present(src):
        return False
    now = now or iso_now()
    item = _load(src, review_id)
    if is_retiring(item):                 # ADR-0069: its edge may already be superseded
        raise ReviewRetiring(review_id)
    item.pop("draft_amendments", None)  # ADR-0058: drafts never survive a terminal decision
    item.pop(REOPENED_FROM, None)       # the reopen it came from is in the audit, not the decision
    item.update(status=decision, decided_by=decided_by, decided_at=now, decision_note=note)
    # Slice 3b review round 2: a unique identity for THIS decision (a reopened and re-decided review
    # gets a new one, even within the same second) — it names the decision's audit entry. An
    # approval's is also its `approval_id` (ADR-0070 B4: executor records bind to it).
    generation = uuid.uuid4().hex
    item["decision_id"] = generation
    item.pop("approval_id", None)
    if decision == "approved":
        item["approval_id"] = generation
    if winner is not None:
        item["winner"] = winner
    if amendments is not None:
        item["amendments"] = amendments
    item.pop(DECIDED_VERSION, None)
    if version_digest is not None:
        item[DECIDED_VERSION] = version_digest
    audit = reviews_dir / "audit_log"
    durable_write.make_dirs(audit)
    audit_entry = {
        "review_id": review_id, "type": item.get("type"), "decision": decision,
        "decided_by": decided_by, "decided_at": now, "subject": item.get("subject"),
        "note": note, "decision_id": generation,
    }
    if item.get("approval_id"):
        audit_entry["approval_id"] = item["approval_id"]
    if winner is not None:
        audit_entry["winner"] = winner
    if amendments is not None:
        audit_entry["amendments"] = amendments
    if version_digest is not None:
        audit_entry[DECIDED_VERSION] = version_digest
    # The transition order (see "decision transitions"): audit (create-only, one entry per decision —
    # an approve / reopen / approve cycle keeps every entry), the decision, the pending copy removed.
    durable_write.write_json(audit / f"{review_id}-{decision}-{generation}.json", audit_entry,
                             create_only=True)
    durable_write.write_json(reviews_dir / decision / f"{review_id}.json", item)
    durable_write.remove(src)
    return True


@_committed
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
    # An interrupted earlier reopen (or decision) is finished first (slice 3b review round 2): a
    # retried reopen of the same decision then finds nothing terminal and returns False.
    finish_interrupted(reviews_dir, review_id)
    src: Path | None = None
    prior_status: str | None = None
    for decision in ("approved", "rejected"):
        candidate = reviews_dir / decision / f"{review_id}.json"
        if _present(candidate):
            src, prior_status = candidate, decision
            break
    if src is None:
        return False
    now = now or iso_now()
    item = _load(src, review_id)
    generation = decision_generation(item)
    prior: dict[str, Any] = {
        "prior_status": prior_status,
        "prior_decided_by": item.get("decided_by"),
        "prior_decided_at": item.get("decided_at"),
        "prior_generation": generation,
    }
    if item.get("approval_id") is not None:
        prior["prior_approval_id"] = item["approval_id"]
    if item.get("winner") is not None:
        prior["prior_winner"] = item["winner"]
    if item.get(DECIDED_VERSION) is not None:
        prior["prior_" + DECIDED_VERSION] = item[DECIDED_VERSION]
    # Clear terminal fields + the supersede winner; back to pending (re-decidable). The pending copy
    # names the decision it reopens, so an interruption before that decision's removal is finished
    # as a reopen, never read as a fresh decision (see "decision transitions").
    for field in ("decided_by", "decided_at", "decision_note", "winner", "approval_id",
                  "decision_id", DECIDED_VERSION):
        item.pop(field, None)
    item["status"] = "pending"
    item[REOPENED_FROM] = {"status": prior_status, "generation": generation}
    dest = reviews_dir / "pending" / f"{review_id}.json"
    if _present(dest):        # never replaced (finish_interrupted settled a same-item copy above)
        existing = _load(dest, review_id)                 # damaged -> ReviewRecordUnavailable
        raise _conflict(review_id, dest, "conflicting_pending_record"
                        if existing.get("type") != item.get("type")
                        or existing.get("subject") != item.get("subject") else "pending_record_present")
    # 1. The audit entry first, create-only. Sequence-suffixed (ADR-0045) so repeated decide->reopen
    # cycles never clobber an earlier one; a retry after an interruption before step 2 finds the
    # entry for this generation and keeps it.
    audit = reviews_dir / "audit_log"
    durable_write.make_dirs(audit)
    if not _reopen_audit_exists(audit, review_id, generation):
        entries, err = authority_read.list_entries(audit, ".json")
        if err is not None:
            raise _conflict(review_id, audit, f"audit_log_unlistable:{err}")
        prefix = f"{review_id}-reopened-"
        taken = {e.name for e in entries if e.name.startswith(prefix)}
        seq = 1 + len(taken)
        while f"{prefix}{seq}.json" in taken or _present(audit / f"{prefix}{seq}.json"):
            seq += 1                                       # never over an existing entry
        durable_write.write_json(audit / f"{prefix}{seq}.json", create_only=True, obj={
            "review_id": review_id, "type": item.get("type"), "event": "reopened",
            "reopened_at": now, "reason": reason, **prior,
        })
    # 2. The pending copy; 3. the decision, removed durably.
    durable_write.write_json(dest, item)
    durable_write.remove(src)
    return True


def _reopen_audit_exists(audit: Path, review_id: str, generation: str) -> bool:
    """Whether a reopened audit entry for this decision generation exists (flushed again when it
    does). A reopened entry for this review that can't be read refuses the reopen — it might be this
    generation's, and audit evidence is never written around."""
    entries, err = authority_read.list_entries(audit, ".json")
    if err is not None:
        raise _conflict(review_id, audit, f"audit_log_unlistable:{err}")
    for entry in entries:
        if not entry.name.startswith(f"{review_id}-reopened-"):
            continue
        read = read_audit_record(entry, review_id)
        if read.state != authority_read.OK:
            raise ReviewRecordUnavailable(review_id, read)
        if read.record.get("event") == "reopened" and read.record.get("prior_generation") == generation:
            durable_write.sync_existing(entry)
            return True
    return False


@_committed
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
    finish_interrupted(reviews_dir, review_id)       # slice 3b review round 2
    # An already-resolved item is a terminal human record — not deferrable.
    if _terminal(reviews_dir, review_id):
        return False
    src = reviews_dir / "pending" / f"{review_id}.json"
    if not _present(src):
        return False
    item = _load(src, review_id)
    if is_retiring(item):                 # ADR-0069: being withdrawn; a deferral would mislead
        raise ReviewRetiring(review_id)
    if item.get("status") == "deferred":
        if draft_amendments is not None and item.get("draft_amendments") != draft_amendments:
            item["draft_amendments"] = draft_amendments
            durable_write.write_json(src, item)
        return False  # idempotent: already deferred, no duplicate audit
    now = now or iso_now()
    if draft_amendments is not None:
        item["draft_amendments"] = draft_amendments
    item.update(status="deferred", decided_by=decided_by, decided_at=now, decision_note=note)
    durable_write.write_json(src, item)
    audit = reviews_dir / "audit_log"
    durable_write.make_dirs(audit)
    # A deferral is non-terminal (the item may later be re-opened to a real decision), so use a
    # unique suffix rather than overwriting — mirrors `withdraw_review_item`.
    durable_write.write_json(audit / f"{review_id}-deferred-{uuid.uuid4().hex[:8]}.json", {
        "review_id": review_id, "type": item.get("type"), "decision": "deferred",
        "decided_by": decided_by, "decided_at": now, "subject": item.get("subject"),
        "note": note,
    })
    return True


class DecideAgainRefused(RuntimeError):
    """`decide_again` was refused: the review is not a terminal, unbound decision any more."""

    def __init__(self, review_id: str, reason: str) -> None:
        super().__init__(f"review {review_id}: {reason}")
        self.review_id = review_id
        self.reason = reason


def _decide_again_key(review_id: str, prior: dict[str, Any], decision: str, digest: str) -> str:
    """One audit identity per (decision being replaced, new decision, version): a retry of the same
    submission finds its own entry; a later, different re-decision gets another."""
    raw = json.dumps({"review_id": review_id, "prior_status": prior.get("status"),
                      "prior_decided_at": prior.get("decided_at"),
                      "prior_approval_id": prior.get("approval_id"),
                      "decision": decision, "digest": digest}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _sync_terminal_dir(path: Path) -> None:
    """Flush a terminal decision directory. A directory that legitimately does not exist (a
    same-status re-decision never created the opposite one) holds nothing to flush; any other
    failure — permission, I/O — propagates."""
    try:
        durable_write.sync_dir(path)
    except FileNotFoundError:
        if os.path.lexists(path):                # it exists after all: not an absence
            raise


def _audit_records_decide_again(entry: Path, review_id: str, prior_status: str, decision: str,
                                version_digest: str) -> bool:
    """Whether `entry` is a readable decide-again audit entry for exactly this replacement (a typed,
    non-blocking read: a special file or a link is never opened as a stream)."""
    read = read_audit_record(entry, review_id, event="decided_again", prior_status=prior_status,
                             decision=decision, **{DECIDED_VERSION: version_digest})
    return read.state == authority_read.OK


@_committed
def decide_again(
    reviews_dir: Path,
    review_id: str,
    *,
    decision: str,
    version_digest: str,
    note: str = "",
    decided_by: str = "human",
    now: str | None = None,
) -> bool:
    """"Decide again for the version shown" (ADR-0073 decision 7): record a new terminal decision,
    bound to `version_digest`, on a terminal decision that carries no digest (a legacy unbound one).
    The previous decision is kept in a uniquely identified audit entry, written durably FIRST.

    Idempotent: retrying the same submission (same decision, same digest) writes neither a second
    decision nor a second audit entry and returns False; an interrupted earlier attempt is finished.
    Raises :class:`DecideAgainRefused` when the review is not terminal or is already bound to another
    decision or version. The CALLER checks the artifact (valid, current inputs, the digest shown).
    Changes no page: Apply executes the new decision like any bound one."""
    if decision not in ("approved", "rejected"):
        raise ValueError(f"decision must be approved|rejected, got {decision!r}")
    reviews_dir = Path(reviews_dir)
    finish_interrupted(reviews_dir, review_id)       # slice 3b review round 2
    found = {d: reviews_dir / d / f"{review_id}.json" for d in ("approved", "rejected")
             if _present(reviews_dir / d / f"{review_id}.json")}
    if not found:
        raise DecideAgainRefused(review_id, "not a terminal decision")
    items = {d: _load(p, review_id) for d, p in found.items()}
    # Already recorded (a retry, or an attempt interrupted after its write): finish and stop, in the
    # first attempt's barrier order — audit flush, replacement file + directory flush, then delete
    # the predecessor and flush its directory. An earlier run's flush may have failed after its
    # rename, so each barrier is repeated before the deletion it guards; without a readable audit
    # entry for the predecessor it is never deleted.
    done = items.get(decision)
    if done is not None and done.get(DECIDED_VERSION) == version_digest:
        left = {d: p for d, p in found.items() if d != decision}
        audits = []
        for d in left:
            entry = (reviews_dir / "audit_log"
                     / f"{review_id}-decided-again-"
                       f"{_decide_again_key(review_id, items[d], decision, version_digest)}.json")
            if not _audit_records_decide_again(entry, review_id, d, decision, version_digest):
                raise DecideAgainRefused(
                    review_id, "the audit entry for the replaced decision is missing or unreadable; "
                    "the replaced decision is kept")
            audits.append(entry)
        for entry in audits:
            durable_write.sync_existing(entry)
        durable_write.sync_existing(found[decision])
        for p in left.values():                   # the replaced file an interruption left behind
            durable_write.remove(p)
        # Flush BOTH terminal directories, even when no predecessor file remains: an earlier run may
        # have deleted it and then failed to flush its directory, so its absence proves nothing
        # durable. Only after both flushes succeed is the re-decision complete.
        for d in ("approved", "rejected"):
            _sync_terminal_dir(reviews_dir / d)
        return False
    if len(found) != 1:
        raise DecideAgainRefused(review_id, "inconsistent ledger (decided in two places)")
    (prior_status, src), = found.items()
    prior = items[prior_status]
    if prior.get(DECIDED_VERSION) is not None:
        raise DecideAgainRefused(review_id, "already bound to a version; reopen it instead")
    now = now or iso_now()
    key = _decide_again_key(review_id, prior, decision, version_digest)
    audit = reviews_dir / "audit_log"
    durable_write.make_dirs(audit)
    entry = audit / f"{review_id}-decided-again-{key}.json"
    if _audit_records_decide_again(entry, review_id, prior_status, decision, version_digest):
        durable_write.sync_existing(entry)
    elif _present(entry):
        # ADR-0074 slice 3b: an existing entry that isn't exactly this replacement's (torn,
        # unreadable, or foreign) is evidence — preserved, never overwritten; nothing is recorded.
        raise DecideAgainRefused(review_id, "an audit entry for this re-decision exists but can't "
                                 "be read; restore it from backup — the decision is kept")
    else:                                        # absent: write it
        durable_write.write_json(entry, {
            "review_id": review_id, "type": prior.get("type"), "event": "decided_again",
            "decided_by": decided_by, "decided_at": now, "subject": prior.get("subject"),
            "note": note, "prior_status": prior_status,
            "prior_decided_by": prior.get("decided_by"), "prior_decided_at": prior.get("decided_at"),
            "prior_approval_id": prior.get("approval_id"),
            "prior_decision_note": prior.get("decision_note"),
            "decision": decision, DECIDED_VERSION: version_digest,
        })
    item = dict(prior)
    item.update(status=decision, decided_by=decided_by, decided_at=now, decision_note=note)
    item[DECIDED_VERSION] = version_digest
    generation = uuid.uuid4().hex                 # a new decision: a new identity (round 2)
    item["decision_id"] = generation
    item.pop("approval_id", None)
    if decision == "approved":
        item["approval_id"] = generation
    dest = reviews_dir / decision / f"{review_id}.json"
    durable_write.make_dirs(dest.parent)
    durable_write.write_json(dest, item)
    if dest != src:
        durable_write.remove(src)
    return True


@_committed
def refresh_pending_proposal(
    reviews_dir: Path,
    review_id: str,
    *,
    proposal: dict[str, Any],
    audit_key: str,
    now: str | None = None,
) -> bool:
    """Replace the version-specific proposal fields of a still-open item in place (ADR-0073
    decision 5): a forced regeneration under unchanged inputs writes new prose under the same review
    id, so the pending proposal must describe the new version (its `version_digest`, title, summary,
    claim ids). Only an open (`pending`/`deferred`, not retiring) item is touched — a decided one is
    a human record. An audit entry keyed by `audit_key` (the producer's commit attempt) is written
    durably first, so a retry neither loses nor duplicates it. Returns True iff it changed the item."""
    reviews_dir = Path(reviews_dir)
    src = reviews_dir / "pending" / f"{review_id}.json"
    if not _present(src):
        return False
    read = read_review_record(src)
    if read.state != authority_read.OK:   # ADR-0074: a damaged record is never rewritten
        logger.warning("review %s not refreshed: its record is %s (%s)", review_id, read.state,
                       read.error)
        return False
    item = read.record
    if item.get("status") not in ("pending", "deferred") or is_retiring(item):
        return False
    old = item.get("proposal") if isinstance(item.get("proposal"), dict) else {}
    new = {**old, **proposal}
    if new == old:
        return False
    audit = reviews_dir / "audit_log"
    durable_write.make_dirs(audit)
    entry = audit / f"{review_id}-proposal-refreshed-{audit_key}.json"
    if not _reuse_audit(entry, review_id, event="proposal_refreshed"):   # damaged: refused, kept
        durable_write.write_json(entry, {
            "review_id": review_id, "type": item.get("type"), "event": "proposal_refreshed",
            "decided_by": "system", "decided_at": now or iso_now(), "subject": item.get("subject"),
            "prior_proposal": old, "proposal": new,
        })
    item["proposal"] = new
    durable_write.write_json(src, item)
    return True


_REVIEW_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def is_review_id(value: Any) -> bool:
    """A review id that names a file inside a review directory (no separators, no dot-names)."""
    return isinstance(value, str) and bool(_REVIEW_ID.fullmatch(value))


def lookup_review(reviews_dir: Path, review_id: str) -> dict[str, Any] | None:
    """The review record for a mutation route, read typed across `pending/`, `approved/` and
    `rejected/`: the usable record, None only when no slot holds an entry; any damaged slot raises
    :class:`ReviewRecordUnavailable` with its category (a dangling link or a special file included —
    every open is non-blocking), so a decision never proceeds next to damage."""
    found: dict[str, Any] | None = None
    for d in _STATE_DIRS:
        path = Path(reviews_dir) / d / f"{review_id}.json"
        if not _present(path):
            continue
        read = read_review_record(path)
        if read.state != authority_read.OK:
            raise ReviewRecordUnavailable(review_id, read)
        found = found or read.record
    return found
