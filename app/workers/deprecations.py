#!/usr/bin/env python3
"""Phase 6 slice 6-3: the scoped, key-free deprecation apply executor (ADR-0035 A5).

`apply_approved_deprecations` realises **approved** `deprecate_wiki_page` decisions deterministically:
it re-renders the target page as `deprecated_candidate` + `review_status: approved` via the explicit
render-path seam (no frontmatter string surgery) and mirrors the graph node status. It is the one
genuinely new executor Phase 6 ships, because `deprecate_wiki_page` is the dominant pending type and the
existing producers don't apply it.

Scope (the dominant review type would otherwise be un-actionable, but identity/raw types stay deferred):
- **In scope:** Claim pages and the concept/entity family (`concept/entity/person/organization/project`).
- **Out of scope (skipped with a typed reason):** `Synthesis/` (owned by the synthesis apply path),
  `Sources/`/`Queries/`/`Tags/`, and any raw-delete/archive/hide type.

The node type is derived from the page directory via a canonical **reverse of `NODE_DIR`** and must match
the graph node's `node_type`; `context.node_type` is an advisory cross-check only (not required), so
legacy auto-approved contradiction-supersede deprecations (filed without it) are absorbed as idempotent
no-ops. **Key-free, deterministic, idempotent, never touches `raw/`, no index rebuild** (the caller owns
the single rebuild). A true no-op (page + review_status + graph mirror already match) is uncounted; a
page already `deprecated_candidate` in page+graph but with the wrong `review_status` is a **normalization
apply** (counted `normalized`); otherwise a full **apply**.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.backend import graph
from app.backend.manifests import iso_now
from app.workers import claims, concepts
from app.workers.wiki_render import NODE_DIR, parse_frontmatter

# A well-formed wiki page is exactly `<Dir>/<file>.md` — one directory segment, one filename, no path
# separators in the name. This rejects traversal (`Claims/../../raw/...`), absolute paths, and nesting
# before the page is ever read (ADR-0035 A5 / CLAUDE.md rule 1: apply never escapes wiki/ into raw/).
# `fullmatch` + a newline-excluding char class: `^…$` + `.match()` accepts a trailing newline (`$`
# matches before it), and `[^/\\]` alone admits embedded newlines — both undesirable for a path gate.
_WIKI_PAGE_RE = re.compile(r"[A-Za-z]+/[^/\\\n]+\.md")

# Page directory -> node type (canonical reverse of NODE_DIR; no ad-hoc singularization).
_DIR_TO_NODE_TYPE = {dir_name: node_type for node_type, dir_name in NODE_DIR.items()}
# Node types the deprecation executor may apply in v1 (claim + concept/entity family).
_IN_SCOPE_TYPES = frozenset({"claim", "concept", "entity", "person", "organization", "project"})
# recompose_claim success outcomes (an evidenced deprecation writes "written"; a no-evidence claim
# renders its tombstone and returns "tombstoned" — both leave the page deprecated_candidate).
_CLAIM_SUCCESS = frozenset({"written", "tombstoned"})


def _approved_items_of_type(reviews_dir: Path, review_type: str) -> list[dict[str, Any]]:
    """Approved items of a given review type, malformed-robust (a corrupt file is skipped)."""
    out: list[dict[str, Any]] = []
    d = reviews_dir / "approved"
    if not d.exists():
        return out
    for path in sorted(d.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(item, dict) and item.get("type") == review_type:
            out.append(item)
    return out


def _deprecate_page(gconn, node_type: str, node_id: str, *, wiki_dir: Path, claims_dir: Path,
                    reviews_dir: Path, markdown_dir: Path, now: str) -> str:
    """Re-render one page as an approved deprecation via the explicit render-path seam (ADR-0035 A5)."""
    if node_type == "claim":
        return claims.recompose_claim(
            gconn, cid=node_id, claims_dir=claims_dir, reviews_dir=reviews_dir,
            markdown_dir=markdown_dir, now=now, deprecate=True, review_status="approved")
    return concepts.recompose_semantic_node_page(
        gconn, node_id=node_id, wiki_dir=wiki_dir, status="deprecated_candidate",
        review_status="approved", now=now)


def apply_approved_deprecations(
    gconn, reviews_dir: Path, *, wiki_dir: Path, claims_dir: Path, markdown_dir: Path,
    now: str | None = None,
) -> dict[str, Any]:
    """Apply every approved, in-scope `deprecate_wiki_page` decision (ADR-0035 A5).

    Returns `{applied, normalized, skipped:[{review_id, reason}], changed_pages, graph_changed}`. Skips
    are honest and typed; a fully-effected item is a silent no-op. No index rebuild (caller-owned)."""
    now = now or iso_now()
    applied = normalized = 0
    skipped: list[dict[str, str]] = []
    changed_pages: list[str] = []

    for item in _approved_items_of_type(reviews_dir, "deprecate_wiki_page"):
        rid = str(item.get("review_id", ""))
        subj = item.get("subject") or {}
        proposal = item.get("proposal") or {}
        page, nid = subj.get("page"), subj.get("node_id")
        if proposal.get("to_status") != "deprecated_candidate":
            skipped.append({"review_id": rid, "reason": "unexpected_to_status"})
            continue
        if not page or not nid:
            skipped.append({"review_id": rid, "reason": "missing_subject"})
            continue
        # Path safety BEFORE any read: reject traversal/absolute/nested paths outright (ADR-0035 A5).
        if not _WIKI_PAGE_RE.fullmatch(page):
            skipped.append({"review_id": rid, "reason": "invalid_page_path"})
            continue
        top_dir = page.split("/", 1)[0]
        if top_dir == "Synthesis":
            skipped.append({"review_id": rid, "reason": "handled_by_synthesis_executor"})
            continue
        if _DIR_TO_NODE_TYPE.get(top_dir) not in _IN_SCOPE_TYPES:
            skipped.append({"review_id": rid, "reason": "out_of_scope"})
            continue
        node = graph.get_node(gconn, nid)
        if node is None:
            skipped.append({"review_id": rid, "reason": "node_missing"})
            continue
        # The graph node is authoritative for the page path: require subject.page to be *exactly* the
        # node's canonical page (no traversal possible, no page/node mismatch). All reads/writes use
        # the canonical page, never the raw subject.page. context.node_type is an advisory cross-check.
        node_type = node["node_type"]
        canonical_page = f"{NODE_DIR[node_type]}/{node['slug']}.md"
        ctx_type = (item.get("context") or {}).get("node_type")
        if page != canonical_page or (ctx_type and ctx_type != node_type):
            skipped.append({"review_id": rid, "reason": "page_node_mismatch"})
            continue

        # Pre-state: distinguish true no-op / normalization / full apply (ADR-0035 A5).
        page_path = wiki_dir / canonical_page
        fm = parse_frontmatter(page_path.read_text(encoding="utf-8")) if page_path.exists() else {}
        page_deprecated = fm.get("status") == "deprecated_candidate"
        page_approved = fm.get("review_status") == "approved"
        graph_deprecated = node["status"] == "deprecated_candidate"
        if page_deprecated and page_approved and graph_deprecated:
            continue  # fully effected already (e.g. a legacy supersede deprecation) — silent no-op

        outcome = _deprecate_page(gconn, node_type, nid, wiki_dir=wiki_dir, claims_dir=claims_dir,
                                  reviews_dir=reviews_dir, markdown_dir=markdown_dir, now=now)
        # A semantic (concept/entity) recompose returns "unchanged" when the page was already in
        # canonical form but the graph node-status mirror was stale (ADR-0041): still a SUCCESS — the
        # mirror ran — it just wrote no page. Treat it as ok; only a real "written" counts a changed page.
        ok = (outcome in _CLAIM_SUCCESS if node_type == "claim"
              else outcome in ("written", "unchanged"))
        if not ok:
            skipped.append({"review_id": rid, "reason": outcome})
            continue
        if outcome != "unchanged":
            changed_pages.append(canonical_page)
        if page_deprecated and graph_deprecated:
            normalized += 1  # page+graph were already deprecated; only review_status needed fixing
        else:
            applied += 1

    return {"applied": applied, "normalized": normalized, "skipped": skipped,
            "changed_pages": changed_pages, "graph_changed": bool(applied or normalized)}


# v1 semantic-hide scope: the concept/entity family only (the single recompose_semantic_node_page seam).
# claim (recompose_claim) + synthesis (separate executor) are deferred fast-follows (ADR-0046 decision 1).
_HIDE_SEMANTIC_SCOPE_TYPES = frozenset({"concept", "entity", "person", "organization", "project"})


def apply_hidden_semantic_pages(
    gconn, reviews_dir: Path, *, wiki_dir: Path, now: str | None = None,
) -> dict[str, Any]:
    """Apply approved `hide_semantic_page` decisions: an **active** concept/entity-family node -> `hidden`
    (ADR-0046). Mirrors `apply_approved_deprecations`' subject/page/scope/canonical-page guards and the
    `recompose_semantic_node_page` render seam, but renders at `status='hidden'` + `review_status='approved'`,
    is **active-only** (a non-active node is a typed `node_not_active` skip), and covers the concept/entity
    family only (claim/synthesis are deferred fast-follows).

    Graph-REQUIRED (the caller gates a missing graph to 503). Key-free, deterministic, idempotent, never
    touches `raw/`, no index rebuild (caller-owned). A true no-op (page `hidden`+`approved` and graph node
    `hidden` already match) is uncounted; a partially-hidden page/graph normalized to consistent is counted
    `normalized`; an active node hidden is a full `applied`. Returns `{applied, normalized, skipped,
    changed_pages, graph_changed}`."""
    now = now or iso_now()
    applied = normalized = 0
    skipped: list[dict[str, str]] = []
    changed_pages: list[str] = []

    for item in _approved_items_of_type(reviews_dir, "hide_semantic_page"):
        rid = str(item.get("review_id", ""))
        subj = item.get("subject") or {}
        proposal = item.get("proposal") or {}
        page, nid = subj.get("page"), subj.get("node_id")
        if proposal.get("to_status") != "hidden":
            skipped.append({"review_id": rid, "reason": "unexpected_to_status"})
            continue
        if not page or not nid:
            skipped.append({"review_id": rid, "reason": "missing_subject"})
            continue
        # Path safety BEFORE any read (ADR-0035 A5 / CLAUDE.md rule 1) — reuses the deprecation guard.
        if not _WIKI_PAGE_RE.fullmatch(page):
            skipped.append({"review_id": rid, "reason": "invalid_page_path"})
            continue
        if _DIR_TO_NODE_TYPE.get(page.split("/", 1)[0]) not in _HIDE_SEMANTIC_SCOPE_TYPES:
            skipped.append({"review_id": rid, "reason": "out_of_scope"})
            continue
        node = graph.get_node(gconn, nid)
        if node is None:
            skipped.append({"review_id": rid, "reason": "node_missing"})
            continue
        node_type = node["node_type"]
        if node_type not in _HIDE_SEMANTIC_SCOPE_TYPES:
            skipped.append({"review_id": rid, "reason": "out_of_scope"})
            continue
        # The graph node is authoritative for the page path: subject.page must be EXACTLY the node's
        # canonical page (no traversal, no page/node mismatch). context.node_type is an advisory cross-check.
        canonical_page = f"{NODE_DIR[node_type]}/{node['slug']}.md"
        ctx_type = (item.get("context") or {}).get("node_type")
        if page != canonical_page or (ctx_type and ctx_type != node_type):
            skipped.append({"review_id": rid, "reason": "page_node_mismatch"})
            continue

        page_path = wiki_dir / canonical_page
        fm = parse_frontmatter(page_path.read_text(encoding="utf-8")) if page_path.exists() else {}
        page_hidden = fm.get("status") == "hidden"
        page_approved = fm.get("review_status") == "approved"
        graph_status = node["status"]
        # ADR-0046 active-only, EXPLICIT paths (no non-active node's page is ever mutated):
        #   - fully effected (page hidden+approved AND graph hidden) -> silent no-op;
        #   - graph ACTIVE -> apply (active -> hidden; also completes a page-hidden/graph-active drift);
        #   - graph hidden + page hidden (review_status not yet approved) -> normalize review_status;
        #   - anything else (graph not active: graph-hidden/page-active drift, deprecated, candidate, ...)
        #     -> node_not_active skip — never recompose a non-active node's page to hidden.
        if page_hidden and page_approved and graph_status == "hidden":
            continue
        if graph_status == "active":
            counts_as = "applied"
        elif graph_status == "hidden" and page_hidden:
            counts_as = "normalized"
        else:
            skipped.append({"review_id": rid, "reason": "node_not_active"})
            continue

        outcome = concepts.recompose_semantic_node_page(
            gconn, node_id=nid, wiki_dir=wiki_dir, status="hidden", review_status="approved", now=now)
        # "written" = page changed; "unchanged" = page already matched but the graph-node mirror still
        # ran (both are SUCCESS — the mirror is the point); anything else is a typed skip reason.
        if outcome not in ("written", "unchanged"):
            skipped.append({"review_id": rid, "reason": outcome})
            continue
        if outcome != "unchanged":
            changed_pages.append(canonical_page)
        if counts_as == "applied":
            applied += 1      # a real active -> hidden transition
        else:
            normalized += 1   # page+graph already hidden; only review_status needed fixing

    return {"applied": applied, "normalized": normalized, "skipped": skipped,
            "changed_pages": changed_pages, "graph_changed": bool(applied or normalized)}
