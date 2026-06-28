#!/usr/bin/env python3
"""Phase 6 slice 6-1: deterministic, read-only review-ledger read model (ADR-0035 A1-A3).

Projects ``reviews/<status>/`` JSON into the list + detail shapes that ``GET /reviews`` and
``GET /reviews/{id}`` serve. Two entry points:

- :func:`list_reviews` — explicit-``status``-field filtering (``pending``/``deferred`` both scan
  ``reviews/pending/`` then filter the item's own ``status``; ``approved``/``rejected`` scan their
  own dirs); ``pending`` default (deferred excluded). ``count`` and ``by_type`` are computed over
  the **full filtered set** (status+type+priority) *before* ``limit``/``offset``; ``items[]`` is the
  deterministically-sorted window after pagination. Sort: **priority desc -> ``created_at`` asc (when
  present) -> ``review_id``**. Unusable files are skipped and counted, never crashing the queue:
  ``parse_errors`` (unreadable / invalid / non-object JSON) vs ``schema_errors`` (a JSON object that
  is not a usable ReviewItem shape — kept separate so a misbehaving producer is distinguishable from a
  corrupt file on disk).
- :func:`get_review` — the full stored item plus a **preview** built by a per-type projector registry
  (ADR-0035 A1; record-only types reuse :func:`record_only_preview`). Each preview carries a
  best-effort, read-only ``apply`` block whose ``effect_status`` is derived from the actual wiki/graph
  state (ADR-0035 A2).

**Strictly read-only (ADR-0035 A2).** Nothing here initializes a DB, creates a directory, repairs a
page, or calls any producer/apply code. The graph is opened only if it already exists with a matching
schema; absent or inconsistent state yields ``effect_status: "unknown"`` + warnings, never a side
effect or a guess. This module is the read half of the decoupled decide/apply ledger; the decision
endpoints (6-2) and the apply executors (6-3) live elsewhere.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from app.backend import graph, manifests
from app.workers.reviews import PRIORITIES, REVIEW_STATUSES
from app.workers.wiki_render import NODE_DIR, parse_frontmatter

# --- effect-status vocabulary (ADR-0035 A2) --------------------------------
PENDING_APPLY = "pending_apply"   # supported, but the effect is not (yet) in the world
EFFECTED = "effected"             # the decision's effect is present in wiki/graph
APPLY_DEFERRED = "apply_deferred"  # no Phase-6 executor for this type (record-only)
UNKNOWN = "unknown"               # state absent/inconsistent — never a guess
# A decided item whose decision owes no world change at all (a rejected promotion / rejected in-scope
# deprecation leaves the world untouched). Distinct from EFFECTED ("the world matches an applied
# effect") so the UI never shows a misleading "effected" badge on a do-nothing rejection.
NO_EFFECT_REQUIRED = "no_effect_required"
# A review subject that can never apply because it is malformed/tampered (e.g. a non-canonical
# source_id). Surfaced as a tamper signal; apply is unsupported and the UI disables approval.
INVALID_SUBJECT = "invalid_subject"

# ADR-0045 reopen gate: a terminal decision may be reopened (moved back to pending to re-decide) ONLY
# when the projector *proves* there is no live effect to orphan — PENDING_APPLY (effect verified absent
# in graph/wiki) or NO_EFFECT_REQUIRED (the decision owes no world change). Everything else blocks:
# EFFECTED (live effect — that's the out-of-scope applied-undo), UNKNOWN (can't confirm — repair the
# read model first), INVALID_SUBJECT (tampered), and APPLY_DEFERRED (a no-executor record-only type means
# "not applied *by this system*", NOT "no effect": manual-effect types like delete_raw_file /
# purge_response_cache run by hand outside /reviews/apply, ADR-0036, and the projector can't detect that).
REOPENABLE_EFFECT_STATUSES = frozenset({PENDING_APPLY, NO_EFFECT_REQUIRED})
_REOPEN_BLOCK_REASON = {
    EFFECTED: "already_applied",
    UNKNOWN: "effect_unknown_repair_read_model",
    INVALID_SUBJECT: "invalid_subject",
    APPLY_DEFERRED: "manual_effect_unknown",
}


def reopen_block_reason(effect_status: str | None) -> str | None:
    """``None`` if an item with this ``effect_status`` may be reopened (ADR-0045); else a 409 reason code."""
    if effect_status in REOPENABLE_EFFECT_STATUSES:
        return None
    return _REOPEN_BLOCK_REASON.get(effect_status, "not_reopenable")


# Review types that an explicit POST /reviews/apply executor backs (decide is type-complete; apply
# is not — ADR-0035 decisions 3-5). Maps type -> the executor name surfaced in the preview.
EXECUTOR_BY_TYPE = {
    "promote_candidate_node": "promote_candidates",
    # synthesis has no extracted wrapper — /reviews/apply calls apply_resolved_syntheses directly
    # (it renders its own pages); the 6-3 design dropped apply_synthesis_decisions (ADR-0035 A4).
    "propose_synthesis": "apply_resolved_syntheses",
    "resolve_contradiction": "apply_contradiction_decisions",
    "deprecate_wiki_page": "apply_approved_deprecations",
    # Phase 7: reversible source archive (active -> archive_candidate), ADR-0036 decision 13.
    "archive_source": "apply_archive_sources",
    # First non-rekeying governance executor (ADR-0041): active symmetric `duplicates` annotation.
    "mark_semantic_duplicate": "apply_marked_duplicates",
    # Governance source hide (ADR-0043): active -> hidden status transition (excluded from default
    # retrieval + nav), reusing the archive source-status machinery.
    "hide_content": "apply_hidden_sources",
    # ADR-0046: governance semantic-page hide (active -> hidden) via the deprecation render seam.
    "hide_semantic_page": "apply_hidden_semantic_pages",
}

# Wiki subdirs the scoped deprecation executor may touch in v1 (ADR-0035 A5). A deprecate item whose
# page lives elsewhere is *not* executor-backed here: Synthesis/ is owned by the synthesis apply
# orchestrator; Sources/Queries and any raw-touching deprecation stay record-only.
DEPRECATION_SCOPE_DIRS = frozenset(
    {"Claims", "Concepts", "Entities", "People", "Organizations", "Projects"})
# ADR-0046 v1: semantic-page hide covers the concept/entity family only (the single
# recompose_semantic_node_page seam) — Claims (recompose_claim) + Synthesis are deferred fast-follows.
HIDE_SEMANTIC_SCOPE_DIRS = frozenset(
    {"Concepts", "Entities", "People", "Organizations", "Projects"})

# Executor-backed types whose *rejection* still carries a deterministic reject-effect to apply (node
# -> deprecated_candidate / edge -> rejected). For promote/deprecate a rejection owes no world change.
_REJECT_HAS_EFFECT = frozenset({"propose_synthesis", "resolve_contradiction"})

_PRIORITY_RANK = {"high": 3, "medium": 2, "low": 1}
# The four list-filter statuses are exactly the review lifecycle statuses (reviews.REVIEW_STATUSES):
# pending/approved/rejected/deferred (ADR-0018). deferred lives in pending/ but is filtered by field.


def decision_apply_required(review_type: str, decision: str) -> bool:
    """Whether ``POST /reviews/apply`` is relevant to a recorded decision (Phase 6 slice 6-2).

    True only for decisions a deterministic executor will realize: an approval of any executor-backed
    type, and a rejection of ``propose_synthesis``/``resolve_contradiction`` (which carry a reject-
    effect). Record-only types, deferrals, and the no-effect rejections (promote/deprecate) are False.
    Type-level hint only — the apply step still reports per-item skips (e.g. out-of-scope deprecation).
    """
    if decision not in ("approved", "rejected") or review_type not in EXECUTOR_BY_TYPE:
        return False
    return decision == "approved" or review_type in _REJECT_HAS_EFFECT


def _synthesis_id(topic_node_id: str) -> str:
    """Deterministic one-per-topic synthesis id (mirror of ``synthesis.synthesis_id``, ADR-0021).

    Replicated here so the read model stays decoupled from the synthesis worker for a pure hash.
    """
    return "syn_" + hashlib.sha256(topic_node_id.encode("utf-8")).hexdigest()[:16]


# --- low-level item loading (malformed-robust) -----------------------------


_REQUIRED_STR_FIELDS = ("review_id", "type", "status")
_DICT_FIELDS = ("subject", "proposal", "context")


def _load_item(path: Path) -> dict[str, Any] | None:
    """Parse one review JSON, returning ``None`` on any read/parse error (a *parse* error)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _is_valid_item(item: dict[str, Any]) -> bool:
    """A parseable JSON object that is actually a usable ReviewItem shape (a *schema* check).

    Required: non-empty string ``review_id``/``type``/``status``. ``subject``/``proposal``/``context``
    must be objects when present. A valid-JSON-but-wrong-shape record would otherwise blow up the
    response model (a 500); here it is skipped + counted as a ``schema_error`` instead.
    """
    if not all(isinstance(item.get(f), str) and item.get(f) for f in _REQUIRED_STR_FIELDS):
        return False
    return all(isinstance(item[f], dict) for f in _DICT_FIELDS if f in item)


def _scan_dir(reviews_dir: Path, dir_name: str) -> tuple[list[dict[str, Any]], int, int]:
    """Load every ``*.json`` under ``reviews/<dir_name>/``; return (items, parse_errors, schema_errors).

    ``parse_errors`` = unreadable / invalid / non-object JSON; ``schema_errors`` = a JSON object that
    is not a usable ReviewItem. Both are skipped from ``items``, never crashing the queue.
    """
    d = Path(reviews_dir) / dir_name
    if not d.exists():
        return [], 0, 0
    items: list[dict[str, Any]] = []
    parse_errors = schema_errors = 0
    for path in sorted(d.glob("*.json")):
        item = _load_item(path)
        if item is None:
            parse_errors += 1
            continue
        if not _is_valid_item(item):
            schema_errors += 1
            continue
        items.append(item)
    return items, parse_errors, schema_errors


def _sort_key(item: dict[str, Any]) -> tuple[int, int, str, str]:
    """Priority desc -> created_at asc (present before missing) -> review_id (ADR-0035 A3)."""
    rank = -_PRIORITY_RANK.get(item.get("priority"), 0)
    created = item.get("created_at")
    has_created = 0 if isinstance(created, str) and created else 1
    return (rank, has_created, created if has_created == 0 else "", str(item.get("review_id", "")))


# --- list ------------------------------------------------------------------


def list_reviews(
    reviews_dir: Path,
    *,
    status: str = "pending",
    type: str | None = None,  # noqa: A002 - mirrors the public ?type= query param
    priority: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """List review items for a status, filtered + deterministically sorted + paginated.

    ``status`` filters on the item's **explicit ``status`` field**, not just the directory:
    ``pending`` and ``deferred`` both read ``reviews/pending/`` and then keep only items whose own
    ``status`` matches (a deferred item lives in ``pending/`` but is not semantically pending).
    ``count``/``by_type`` cover the full filtered set; ``items`` is the post-``offset``/``limit``
    window. Raises ``ValueError`` for an unknown ``status``/``priority`` (the endpoint maps to 400).
    """
    if status not in REVIEW_STATUSES:
        raise ValueError(f"unknown status {status!r}; allowed: {sorted(REVIEW_STATUSES)}")
    if priority is not None and priority not in PRIORITIES:
        raise ValueError(f"unknown priority {priority!r}; allowed: {sorted(PRIORITIES)}")

    # pending/deferred share the pending/ dir; approved/rejected have their own.
    dir_name = "pending" if status in ("pending", "deferred") else status
    raw, parse_errors, schema_errors = _scan_dir(reviews_dir, dir_name)

    filtered = [
        it for it in raw
        if it.get("status") == status
        and (type is None or it.get("type") == type)
        and (priority is None or it.get("priority") == priority)
    ]
    by_type = Counter(str(it.get("type")) for it in filtered)
    filtered.sort(key=_sort_key)

    window = filtered[offset:] if limit is None else filtered[offset:offset + limit]
    return {
        "count": len(filtered),
        "by_type": dict(sorted(by_type.items())),
        "parse_errors": parse_errors,
        "schema_errors": schema_errors,
        "items": window,
    }


def apply_scope_counts(reviews_dir: Path) -> dict[str, Any]:
    """Read-only classification of the `approved/` queue for the apply confirm page (ADR-0035 A8).

    Groups approved items into executor-backed types (an apply will *process* them — some may be
    idempotent no-ops) vs record-only types (no Phase-6 executor — the `unapplied` set), and counts
    malformed files. **No graph/page reads and no executor calls** — this is scope, *not* a dry-run
    prediction of effects. JSON apply and the HTML confirm page share this one classification.
    """
    items, parse_errors, schema_errors = _scan_dir(reviews_dir, "approved")
    executor_backed: dict[str, int] = {}
    record_only: dict[str, int] = {}
    for it in items:
        rtype = str(it.get("type"))
        bucket = executor_backed if rtype in EXECUTOR_BY_TYPE else record_only
        bucket[rtype] = bucket.get(rtype, 0) + 1
    return {
        "executor_backed": dict(sorted(executor_backed.items())),
        "record_only": dict(sorted(record_only.items())),
        "parse_errors": parse_errors,
        "schema_errors": schema_errors,
    }


# --- detail + per-type preview projection ----------------------------------


def _open_graph_readonly(graph_db: Path | None) -> Any:
    """Open the graph read-only, or ``None`` if absent/schema-mismatched. Never creates the DB."""
    if graph_db is None:
        return None
    graph_db = Path(graph_db)
    if not graph_db.exists():
        return None
    conn = graph.connect(graph_db)
    if graph.schema_version(conn) != graph.SCHEMA_VERSION:
        conn.close()
        return None
    return conn


def _safe_wiki_subpath(wiki_dir: Path, page: str) -> Path | None:
    """Resolve ``page`` under ``wiki_dir`` only if it stays contained — else ``None``.

    A containment guard (no graph-derived matching — the preview may be inspecting malformed ledger
    state). Rejects absolute paths, any ``..`` segment, and anything that resolves outside ``wiki_dir``,
    so a hostile ``subject.page`` can never make the read model read outside the wiki (CLAUDE.md rule 1).
    """
    p = Path(page)
    if p.is_absolute() or ".." in p.parts:
        return None
    resolved = (wiki_dir / p).resolve()
    try:
        resolved.relative_to(wiki_dir.resolve())
    except ValueError:
        return None
    return resolved


def _manifest_status(manifests_dir: Path | None, source_id: str | None) -> str | None:
    """A source's lifecycle status from the manifest (the authority, ADR-0036), or None if unreadable."""
    if manifests_dir is None or not manifests.is_source_id(source_id):
        return None  # never hand an untrusted/non-canonical id to the path layer
    m = manifests.load_manifest(Path(manifests_dir), source_id)
    return manifests.get_status(m) if m is not None else None


def _page_frontmatter(wiki_dir: Path | None, page: str | None) -> dict[str, Any] | None:
    """Read a wiki page's frontmatter, or ``None`` if the path is unsafe/absent/unreadable (read-only)."""
    if wiki_dir is None or not page:
        return None
    page_path = _safe_wiki_subpath(Path(wiki_dir), page)
    if page_path is None:
        return None
    try:
        text = page_path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_frontmatter(text)


def _scaffold(item: dict[str, Any]) -> dict[str, Any]:
    """The common normalized-preview skeleton every projector fills in."""
    return {
        "review_id": item.get("review_id"),
        "type": item.get("type"),
        "status": item.get("status"),
        "summary": "",
        "affected_paths": [],
        "node_ids": [],
        "current_status": None,
        "proposed_status": None,
        "proposed_action": None,
        "warnings": [],
        "apply": {},
        "details": {},
    }


def _apply_record_only(extra_warnings: list[str] | None = None) -> dict[str, Any]:
    """Apply block for a type with no Phase-6 executor (never implies a *failed* apply)."""
    return {
        "supported": False,
        "executor": None,
        "effect_status": APPLY_DEFERRED,
        "effected": None,
        "warnings": extra_warnings or ["executor_missing"],
    }


def _apply_supported(executor: str, effect_status: str, warnings: list[str]) -> dict[str, Any]:
    return {
        "supported": True,
        "executor": executor,
        "effect_status": effect_status,
        "effected": effect_status == EFFECTED,
        "warnings": warnings,
    }


def record_only_preview(item: dict[str, Any], *, gconn: Any, wiki_dir: Path | None) -> dict[str, Any]:
    """Fallback projection for record-only types and unknown/unhandled types (ADR-0035 A1)."""
    subj = item.get("subject") or {}
    proposal = item.get("proposal") or {}
    out = _scaffold(item)
    out["node_ids"] = [v for k, v in subj.items() if k.endswith("node_id") or k == "node_id"]
    if subj.get("page"):
        out["affected_paths"] = [subj["page"]]
    out["proposed_status"] = proposal.get("to_status")
    out["proposed_action"] = item.get("type")
    out["summary"] = f"{item.get('type')} — record-only in Phase 6 (decide here; apply deferred)."
    out["warnings"] = ["apply_deferred"]
    out["apply"] = _apply_record_only()
    out["details"] = {"subject": subj, "proposal": proposal, "context": item.get("context") or {}}
    return out


def _effect_promote(item: dict[str, Any], gconn: Any, wiki_dir: Path | None) -> tuple[str, list[str]]:
    status = item.get("status")
    if status not in ("approved", "rejected"):
        return PENDING_APPLY, []
    if status == "rejected":
        return NO_EFFECT_REQUIRED, []  # rejection promotes nothing; no world mutation is owed
    nid = (item.get("subject") or {}).get("node_id")
    if gconn is None:
        return UNKNOWN, ["graph_unavailable"]
    node = graph.get_node(gconn, nid) if nid else None
    if node is None:
        return UNKNOWN, ["node_missing"]
    return (EFFECTED if node["status"] == "active" else PENDING_APPLY), []


def preview_promote_candidate_node(
    item: dict[str, Any], *, gconn: Any, wiki_dir: Path | None
) -> dict[str, Any]:
    subj = item.get("subject") or {}
    proposal = item.get("proposal") or {}
    out = _scaffold(item)
    out["node_ids"] = [subj["node_id"]] if subj.get("node_id") else []
    out["proposed_status"] = proposal.get("to_status", "active")
    out["proposed_action"] = "promote candidate -> active"
    name = proposal.get("name") or subj.get("node_id")
    out["summary"] = f"Promote candidate {proposal.get('node_type', 'node')} {name} to active."
    if gconn is not None and subj.get("node_id"):
        node = graph.get_node(gconn, subj["node_id"])
        out["current_status"] = node["status"] if node else None
    effect_status, warnings = _effect_promote(item, gconn, wiki_dir)
    out["apply"] = _apply_supported(EXECUTOR_BY_TYPE["promote_candidate_node"], effect_status, warnings)
    out["details"] = {"node_type": proposal.get("node_type"), "name": proposal.get("name")}
    return out


def _effect_synthesis(item: dict[str, Any], gconn: Any, wiki_dir: Path | None) -> tuple[str, list[str]]:
    status = item.get("status")
    if status not in ("approved", "rejected"):
        return PENDING_APPLY, []
    topic = (item.get("subject") or {}).get("topic_node_id")
    if gconn is None:
        return UNKNOWN, ["graph_unavailable"]
    if not topic:
        return UNKNOWN, ["missing_topic_node_id"]
    syn_id = _synthesis_id(topic)
    node = graph.get_node(gconn, syn_id)
    if node is None:
        return UNKNOWN, ["synthesis_node_missing"]
    # ADR-0035 A2: effected requires BOTH the graph node and the Synthesis page in the target state
    # (apply_synthesis_decisions renders the page + mirrors the node together). approve -> active /
    # review_status approved; reject -> deprecated_candidate / review_status rejected.
    node_target = "active" if status == "approved" else "deprecated_candidate"
    page_review_target = "approved" if status == "approved" else "rejected"
    fm = _page_frontmatter(wiki_dir, f"Synthesis/{syn_id}.md")
    if fm is None:
        return UNKNOWN, ["synthesis_page_unreadable"]
    node_ok = node["status"] == node_target
    page_ok = fm.get("status") == node_target and fm.get("review_status") == page_review_target
    return (EFFECTED if (node_ok and page_ok) else PENDING_APPLY), []


def preview_propose_synthesis(
    item: dict[str, Any], *, gconn: Any, wiki_dir: Path | None
) -> dict[str, Any]:
    subj = item.get("subject") or {}
    topic = subj.get("topic_node_id")
    out = _scaffold(item)
    if topic:
        syn_id = _synthesis_id(topic)
        out["node_ids"] = [syn_id, topic]
        out["affected_paths"] = [f"Synthesis/{syn_id}.md"]
        if gconn is not None:
            node = graph.get_node(gconn, syn_id)
            out["current_status"] = node["status"] if node else None
    out["proposed_status"] = "active"
    out["proposed_action"] = "approve synthesis -> active (reject -> deprecated_candidate)"
    out["summary"] = f"Cross-source synthesis over topic {topic}."
    effect_status, warnings = _effect_synthesis(item, gconn, wiki_dir)
    out["apply"] = _apply_supported(EXECUTOR_BY_TYPE["propose_synthesis"], effect_status, warnings)
    out["details"] = {"topic_node_id": topic}
    return out


def _effect_contradiction(
    item: dict[str, Any], gconn: Any, wiki_dir: Path | None
) -> tuple[str, list[str]]:
    status = item.get("status")
    if status not in ("approved", "rejected"):
        return PENDING_APPLY, []
    subj = item.get("subject") or {}
    a, b = subj.get("claim_a"), subj.get("claim_b")
    if gconn is None:
        return UNKNOWN, ["graph_unavailable"]
    if not (a and b):
        return UNKNOWN, ["missing_claim_ids"]
    rows = graph.contradiction_between(gconn, a, b)
    if not rows:
        return UNKNOWN, ["contradiction_edge_missing"]
    statuses = {r["status"] for r in rows}
    if status == "rejected":
        return (EFFECTED if "rejected" in statuses else PENDING_APPLY), []
    # Approve leaves the edge active for both acknowledge and supersede. A supersede (the approved
    # item names a `winner`, ADR-0031) owes more: an active `supersedes` edge winner->loser AND the
    # loser deprecated_candidate. Checking only the edge would hide a not-yet-applied supersede.
    if "active" not in statuses:
        return PENDING_APPLY, []
    winner = item.get("winner")
    if winner not in (a, b):
        return EFFECTED, []  # acknowledge: an active contradicts edge is the whole effect
    loser = b if winner == a else a
    has_supersedes = any(
        e["dst_id"] == loser and e["edge_type"] == "supersedes"
        for e in graph.outgoing_active(gconn, winner))
    loser_node = graph.get_node(gconn, loser)
    loser_deprecated = bool(loser_node) and loser_node["status"] == "deprecated_candidate"
    return (EFFECTED if (has_supersedes and loser_deprecated) else PENDING_APPLY), []


def preview_resolve_contradiction(
    item: dict[str, Any], *, gconn: Any, wiki_dir: Path | None
) -> dict[str, Any]:
    subj = item.get("subject") or {}
    proposal = item.get("proposal") or {}
    a, b = subj.get("claim_a"), subj.get("claim_b")
    out = _scaffold(item)
    out["node_ids"] = [x for x in (a, b) if x]
    out["affected_paths"] = [f"Claims/{x}.md" for x in (a, b) if x]
    out["proposed_action"] = ("resolve contradiction — approve to acknowledge (both stand), "
                              "approve with a winner to supersede (loser deprecated), or reject (ADR-0044)")
    out["summary"] = f"Contradiction between claims {a} and {b}."
    out["details"] = {
        "outcomes": proposal.get("outcomes"),
        "confidence": proposal.get("confidence"),
        "explanation": proposal.get("explanation"),
        "shared_nodes": (item.get("context") or {}).get("shared_nodes"),
        "winner": item.get("winner"),  # ADR-0044: the recorded supersede sub-outcome (None until chosen)
    }
    effect_status, warnings = _effect_contradiction(item, gconn, wiki_dir)
    out["apply"] = _apply_supported(
        EXECUTOR_BY_TYPE["resolve_contradiction"], effect_status, warnings)
    return out


def _effect_deprecate(item: dict[str, Any], gconn: Any, wiki_dir: Path | None) -> tuple[str, list[str]]:
    status = item.get("status")
    if status not in ("approved", "rejected"):
        return PENDING_APPLY, []
    if status == "rejected":
        return NO_EFFECT_REQUIRED, []  # a rejected deprecation leaves the page as-is; nothing to apply
    subj = item.get("subject") or {}
    fm = _page_frontmatter(wiki_dir, subj.get("page"))
    if fm is None:
        return UNKNOWN, ["page_unreadable"]
    page_ok = fm.get("status") == "deprecated_candidate" and fm.get("review_status") == "approved"
    # ADR-0035 A2/A5: the executor mirrors the graph node status, so effected REQUIRES verifying that
    # mirror. An unreadable graph or missing node can't be confirmed -> unknown, never a guess.
    nid = subj.get("node_id")
    if gconn is None:
        return UNKNOWN, ["graph_unavailable"]
    node = graph.get_node(gconn, nid) if nid else None
    if node is None:
        return UNKNOWN, ["node_missing"]
    graph_ok = node["status"] == "deprecated_candidate"
    return (EFFECTED if (page_ok and graph_ok) else PENDING_APPLY), []


def preview_deprecate_wiki_page(
    item: dict[str, Any], *, gconn: Any, wiki_dir: Path | None
) -> dict[str, Any]:
    subj = item.get("subject") or {}
    proposal = item.get("proposal") or {}
    page = subj.get("page")
    top_dir = page.split("/", 1)[0] if page else None
    out = _scaffold(item)
    out["node_ids"] = [subj["node_id"]] if subj.get("node_id") else []
    out["affected_paths"] = [page] if page else []
    out["proposed_status"] = proposal.get("to_status", "deprecated_candidate")
    out["proposed_action"] = "deprecate wiki page -> deprecated_candidate"
    out["summary"] = f"Deprecate {page} ({proposal.get('reason', 'no reason given')})."
    fm = _page_frontmatter(wiki_dir, page)
    out["current_status"] = fm.get("status") if fm else None
    out["details"] = {"node_type": (item.get("context") or {}).get("node_type"),
                      "reason": proposal.get("reason")}
    # In-scope dirs are executor-backed; Synthesis/ is owned by the synthesis apply orchestrator;
    # everything else stays record-only (ADR-0035 A5).
    if top_dir in DEPRECATION_SCOPE_DIRS:
        effect_status, warnings = _effect_deprecate(item, gconn, wiki_dir)
        out["apply"] = _apply_supported(
            EXECUTOR_BY_TYPE["deprecate_wiki_page"], effect_status, warnings)
    elif top_dir == "Synthesis":
        out["apply"] = _apply_record_only(["handled_by_synthesis_executor"])
        out["warnings"] = ["apply_deferred"]
    else:
        out["apply"] = _apply_record_only(["out_of_scope_for_deprecation_executor"])
        out["warnings"] = ["apply_deferred"]
    return out


def _effect_hide_semantic(item: dict[str, Any], gconn: Any, wiki_dir: Path | None) -> tuple[str, list[str]]:
    # ADR-0046 (reopen-safe vs ADR-0045): EFFECTED requires page hidden + review_status approved + graph
    # node hidden. A PARTIAL live hide (page XOR graph hidden, or both hidden but review_status lagging)
    # must NOT be plain PENDING_APPLY — reopen treats PENDING_APPLY as "no live effect" and would orphan
    # the already-hidden page/node — so it is UNKNOWN ("partial_hide_state"), which the reopen gate blocks.
    # Only when NEITHER page nor graph is hidden is the hide cleanly unapplied (reopenable).
    status = item.get("status")
    if status not in ("approved", "rejected"):
        return PENDING_APPLY, []
    if status == "rejected":
        return NO_EFFECT_REQUIRED, []  # a rejected hide leaves the page as-is; nothing to apply
    subj = item.get("subject") or {}
    fm = _page_frontmatter(wiki_dir, subj.get("page"))
    if fm is None:
        return UNKNOWN, ["page_unreadable"]
    nid = subj.get("node_id")
    if gconn is None:
        return UNKNOWN, ["graph_unavailable"]
    node = graph.get_node(gconn, nid) if nid else None
    if node is None:
        return UNKNOWN, ["node_missing"]
    page_hidden = fm.get("status") == "hidden"
    graph_hidden = node["status"] == "hidden"
    if page_hidden and fm.get("review_status") == "approved" and graph_hidden:
        return EFFECTED, []                          # fully live + approved
    if page_hidden or graph_hidden:
        return UNKNOWN, ["partial_hide_state"]       # part of the hide is live -> NOT reopen-safe
    # Neither hidden -> cleanly unapplied (reopenable). A non-active node still warns (the executor will
    # skip it), but reopen stays safe because no hide effect is live.
    warnings = [] if node["status"] == "active" else ["node_not_active"]
    return PENDING_APPLY, warnings


def preview_hide_semantic_page(
    item: dict[str, Any], *, gconn: Any, wiki_dir: Path | None
) -> dict[str, Any]:
    subj = item.get("subject") or {}
    proposal = item.get("proposal") or {}
    page = subj.get("page")
    top_dir = page.split("/", 1)[0] if page else None
    out = _scaffold(item)
    out["node_ids"] = [subj["node_id"]] if subj.get("node_id") else []
    out["affected_paths"] = [page] if page else []
    out["proposed_status"] = proposal.get("to_status", "hidden")
    out["proposed_action"] = "hide semantic page (active -> hidden)"
    out["summary"] = f"Hide semantic page {page}."
    fm = _page_frontmatter(wiki_dir, page)
    out["current_status"] = fm.get("status") if fm else None
    out["details"] = {"node_type": (item.get("context") or {}).get("node_type")}
    if top_dir in HIDE_SEMANTIC_SCOPE_DIRS:
        effect_status, warnings = _effect_hide_semantic(item, gconn, wiki_dir)
        out["apply"] = _apply_supported(EXECUTOR_BY_TYPE["hide_semantic_page"], effect_status, warnings)
    else:
        out["apply"] = _apply_record_only(["out_of_scope_for_hide_semantic_executor"])
        out["warnings"] = ["apply_deferred"]
    return out


# Per-type projection registry (ADR-0035 A1). Every executor-backed type has a dedicated projector;
# all other (record-only) types fall through to record_only_preview, so the ledger is type-complete.
def _effect_archive_source(item: dict[str, Any], wiki_dir: Path | None,
                           manifests_dir: Path | None) -> tuple[str, list[str]]:
    status = item.get("status")
    if status not in ("approved", "rejected"):
        return PENDING_APPLY, []
    if status == "rejected":
        return NO_EFFECT_REQUIRED, []  # a rejected archive leaves the source active; nothing to apply
    sid = (item.get("subject") or {}).get("source_id")
    # The manifest is the lifecycle-status authority (ADR-0036 decision 13); the page is a mirror.
    ms = _manifest_status(manifests_dir, sid)
    if ms is None:
        return UNKNOWN, ["manifest_unreadable"]
    warnings: list[str] = []
    fm = _page_frontmatter(wiki_dir, f"Sources/{sid}.md") if sid else None
    if fm is not None and fm.get("status") != ms:
        warnings.append("page_manifest_drift")  # the page mirror disagrees with the authority
    return (EFFECTED if ms == "archive_candidate" else PENDING_APPLY), warnings


def preview_archive_source(item: dict[str, Any], *, gconn: Any, wiki_dir: Path | None,
                           manifests_dir: Path | None = None) -> dict[str, Any]:
    subj = item.get("subject") or {}
    proposal = item.get("proposal") or {}
    sid = subj.get("source_id")
    out = _scaffold(item)
    if not manifests.is_source_id(sid):
        # Tampered/malformed subject — emit no fake affected path, mark unappliable. The UI disables
        # approval; the apply executor independently skips it (defense-in-depth).
        out["node_ids"] = []
        out["affected_paths"] = []
        out["proposed_status"] = "archive_candidate"
        out["proposed_action"] = "archive source (active -> archive_candidate)"
        out["summary"] = "Invalid review subject: source_id is not canonical (src_<16 hex>) — cannot apply."
        out["current_status"] = None
        out["invalid_subject"] = True
        out["apply"] = {"supported": False, "executor": EXECUTOR_BY_TYPE["archive_source"],
                        "effect_status": INVALID_SUBJECT, "effected": False,
                        "warnings": ["invalid_source_id"]}
        return out
    out["node_ids"] = [sid] if sid else []
    out["affected_paths"] = [f"Sources/{sid}.md"] if sid else []
    out["proposed_status"] = "archive_candidate"
    out["proposed_action"] = "archive source (active -> archive_candidate; excluded from default retrieval)"
    out["summary"] = f"Archive source {sid} ({proposal.get('reason', 'stale')})."
    out["current_status"] = _manifest_status(manifests_dir, sid)  # authority, not the page
    effect_status, warnings = _effect_archive_source(item, wiki_dir, manifests_dir)
    out["apply"] = _apply_supported(EXECUTOR_BY_TYPE["archive_source"], effect_status, warnings)
    out["details"] = {"reason": proposal.get("reason"), "age_days": proposal.get("age_days")}
    return out


# Node families with a `## Duplicates` page projection (ADR-0041) + a conservative id-safety check
# (unsafe/path-like/empty), matching the executor's guards (app/workers/duplicates.py).
_DUP_NODE_TYPES = frozenset({"concept", "entity", "person", "organization", "project"})
_SAFE_NODE_ID = re.compile(r"[A-Za-z0-9_]+")


def preview_mark_semantic_duplicate(
    item: dict[str, Any], *, gconn: Any, wiki_dir: Path | None
) -> dict[str, Any]:
    """Lightweight read-only per-item hint for mark_semantic_duplicate (ADR-0041) — NOT a mutation
    predictor (the full diff is the ADR-0040 dry-run's job). Resolves node_ids -> both page paths when
    possible and surfaces the same read-only warnings the executor guards on (malformed_subject/
    invalid_node_id/self_duplicate/node_missing/type_mismatch/unsupported_node_type/already_duplicated/
    graph_unavailable)."""
    out = _scaffold(item)
    subject = item.get("subject") or {}
    ids = subject.get("node_ids") if isinstance(subject, dict) else None
    warnings: list[str] = []
    out["proposed_action"] = "mark duplicates"
    if not (isinstance(ids, list) and len(ids) == 2 and all(isinstance(x, str) and x for x in ids)):
        warnings.append("malformed_subject")
        out["node_ids"] = [x for x in ids if isinstance(x, str)] if isinstance(ids, list) else []
    elif not all(_SAFE_NODE_ID.fullmatch(x) for x in ids):
        warnings.append("invalid_node_id")
        out["node_ids"] = list(ids)
    else:
        a, b = sorted(ids)
        out["node_ids"] = [a, b]
        out["proposed_action"] = f"mark duplicates({a},{b})"
        out["summary"] = f"Mark {a} and {b} as duplicates."
        if a == b:
            warnings.append("self_duplicate")
        elif gconn is None:
            warnings.append("graph_unavailable")
        else:
            na, nb = graph.get_node(gconn, a), graph.get_node(gconn, b)
            if na is None or nb is None:
                warnings.append("node_missing")
            elif na["node_type"] != nb["node_type"]:
                warnings.append("type_mismatch")
            elif na["node_type"] not in _DUP_NODE_TYPES:
                warnings.append("unsupported_node_type")
            else:
                out["affected_paths"] = [f"{NODE_DIR[na['node_type']]}/{na['slug']}.md",
                                         f"{NODE_DIR[nb['node_type']]}/{nb['slug']}.md"]
                if any(p["node_id"] == b for p in graph.active_duplicates(gconn, a)):
                    warnings.append("already_duplicated")
    effect_status = EFFECTED if "already_duplicated" in warnings else PENDING_APPLY
    out["apply"] = _apply_supported(
        EXECUTOR_BY_TYPE["mark_semantic_duplicate"], effect_status, warnings)
    out["warnings"] = warnings
    return out


def _effect_hide_content(item: dict[str, Any], wiki_dir: Path | None,
                         manifests_dir: Path | None) -> tuple[str, list[str]]:
    status = item.get("status")
    if status not in ("approved", "rejected"):
        return PENDING_APPLY, []
    if status == "rejected":
        return NO_EFFECT_REQUIRED, []  # a rejected hide leaves the source active; nothing to apply
    sid = (item.get("subject") or {}).get("source_id")
    ms = _manifest_status(manifests_dir, sid)  # manifest is the source-status authority (ADR-0043)
    if ms is None:
        return UNKNOWN, ["manifest_unreadable"]
    warnings: list[str] = []
    fm = _page_frontmatter(wiki_dir, f"Sources/{sid}.md") if sid else None
    if fm is not None and fm.get("status") != ms:
        warnings.append("page_manifest_drift")
    # The executor is active-only (ADR-0043): an approved hide of an already non-active source (e.g.
    # archive_candidate / deprecated_candidate) is a no-op skip — flag it so the preview doesn't overpromise.
    if ms not in ("active", "hidden"):
        warnings.append("source_not_active")
    return (EFFECTED if ms == "hidden" else PENDING_APPLY), warnings


def preview_hide_content(item: dict[str, Any], *, gconn: Any, wiki_dir: Path | None,
                         manifests_dir: Path | None = None) -> dict[str, Any]:
    """Per-item preview for hide_content (ADR-0043): governance source hide (active -> hidden). Mirrors
    preview_archive_source (manifest is the status authority)."""
    subj = item.get("subject") or {}
    proposal = item.get("proposal") or {}
    sid = subj.get("source_id")
    out = _scaffold(item)
    if not manifests.is_source_id(sid):
        out["node_ids"] = []
        out["affected_paths"] = []
        out["proposed_status"] = "hidden"
        out["proposed_action"] = "hide source (active -> hidden)"
        out["summary"] = "Invalid review subject: source_id is not canonical (src_<16 hex>) — cannot apply."
        out["current_status"] = None
        out["invalid_subject"] = True
        out["apply"] = {"supported": False, "executor": EXECUTOR_BY_TYPE["hide_content"],
                        "effect_status": INVALID_SUBJECT, "effected": False,
                        "warnings": ["invalid_source_id"]}
        return out
    out["node_ids"] = [sid] if sid else []
    out["affected_paths"] = [f"Sources/{sid}.md"] if sid else []
    out["proposed_status"] = "hidden"
    out["proposed_action"] = "hide source (active -> hidden; excluded from default retrieval + navigation)"
    out["summary"] = f"Hide source {sid} ({proposal.get('reason', 'governance')})."
    out["current_status"] = _manifest_status(manifests_dir, sid)  # authority, not the page
    effect_status, warnings = _effect_hide_content(item, wiki_dir, manifests_dir)
    out["apply"] = _apply_supported(EXECUTOR_BY_TYPE["hide_content"], effect_status, warnings)
    out["details"] = {"reason": proposal.get("reason")}
    return out


_PROJECTORS = {
    "promote_candidate_node": preview_promote_candidate_node,
    "propose_synthesis": preview_propose_synthesis,
    "resolve_contradiction": preview_resolve_contradiction,
    "deprecate_wiki_page": preview_deprecate_wiki_page,
    "archive_source": preview_archive_source,
    "mark_semantic_duplicate": preview_mark_semantic_duplicate,
    "hide_content": preview_hide_content,
    "hide_semantic_page": preview_hide_semantic_page,
}


def project_review(item: dict[str, Any], *, gconn: Any, wiki_dir: Path | None,
                   manifests_dir: Path | None = None) -> dict[str, Any]:
    """Build the normalized preview for one item via the per-type registry (record-only fallback).

    Only the archive + hide_content projectors need the manifest (its status authority); the others
    read graph/page.
    """
    rtype = str(item.get("type"))
    projector = _PROJECTORS.get(rtype, record_only_preview)
    if rtype in ("archive_source", "hide_content"):  # both read the manifest status authority
        return projector(item, gconn=gconn, wiki_dir=wiki_dir, manifests_dir=manifests_dir)
    return projector(item, gconn=gconn, wiki_dir=wiki_dir)


def find_review(reviews_dir: Path, review_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Locate one item across pending/approved/rejected. Returns (item, error_kind).

    ``(item, None)`` -> found and usable; ``(None, None)`` -> not found; ``(None, "parse")`` -> the
    file exists but is corrupt JSON; ``(None, "schema")`` -> valid JSON but not a usable ReviewItem.
    """
    reviews_dir = Path(reviews_dir)
    error: str | None = None
    for dir_name in ("pending", "approved", "rejected"):
        path = reviews_dir / dir_name / f"{review_id}.json"
        if not path.exists():
            continue
        item = _load_item(path)
        if item is None:
            error = "parse"
            continue
        if not _is_valid_item(item):
            error = "schema"
            continue
        return item, None
    return None, error


def get_review(
    reviews_dir: Path,
    review_id: str,
    *,
    graph_db: Path | None = None,
    wiki_dir: Path | None = None,
    manifests_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Return ``{item, preview}`` for one review id, or ``None`` if not found.

    Read-only: opens the graph only if it already exists with a matching schema, and reads wiki
    pages without mutating them. A corrupt or schema-invalid review file is reported via a
    ``parse_error`` / ``schema_error`` marker (the endpoint maps both to 404) rather than crashing.
    """
    item, error = find_review(reviews_dir, review_id)
    if item is None:
        if error == "parse":
            return {"item": None, "preview": None, "parse_error": True, "review_id": review_id}
        if error == "schema":
            return {"item": None, "preview": None, "schema_error": True, "review_id": review_id}
        return None
    conn = _open_graph_readonly(graph_db)
    try:
        preview = project_review(item, gconn=conn, wiki_dir=wiki_dir, manifests_dir=manifests_dir)
    finally:
        if conn is not None:
            conn.close()
    return {"item": item, "preview": preview}
