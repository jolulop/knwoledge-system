#!/usr/bin/env python3
"""Phase 3.5c slice 2 cross-source synthesis (ADR-0031).

Tier-3 LLM pass that writes one **candidate** synthesis per `active` knowledge item that is
evidenced by ≥2 `active` claims (each with grounded citations) from ≥2 *independent* sources
(ADR-0018; the `claim → source → mentions → item` neighborhood). The synthesis is grounded on
the claim **nodes**: the page's Supporting-Evidence links and `derived_from` (synthesis → claim)
edges are the graph authority, the LLM supplies only the prose, and the artifact
(`normalized/enrichment/<topic_node_id>.synthesis.json`) is the record of that prose.

Governance (ADR-0031 §7; review-only promotion, **no recurrence**):
- A synthesis is born `status: candidate` with a `propose_synthesis` review whose id is
  **fingerprint-scoped** (`{topic_node_id, fingerprint}`) so each distinct evidence set is a
  distinct, re-fileable decision.
- The normal pass **never rewrites a reviewed synthesis**: an `active` (approved) synthesis stays
  active (a fingerprint change is surfaced as `stale_active`, regenerated only with `--force`),
  and a synthesis whose *current evidence* was already rejected is left alone (no re-nag). Only
  changed evidence (a new fingerprint), with `--force` for an approved one, re-opens it.
- Topics that drop below the threshold are **retracted** through the audited deprecation path
  (`deprecate_wiki_page` filed, page re-rendered `deprecated_candidate` with a coherent
  `review_status`, pending proposals withdrawn) — the same governance the claim/concept tombstones
  use.

No API key → a `skipped` job, but resolution + retraction still run. v1 scope (ADR-0031 §6
permits both): the prose stands on claim nodes and emits **no direct source quotes** — a guard
rejects output that copies a long verbatim run from a contributing source; the item→synthesis
backlink is a `related_to` edge, not yet projected on the item page.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

from app.backend import db, graph, write_lock
from app.backend import producer_journal as journal
from app.backend.manifests import get_provenance, independent_sources, iso_now, valid_manifests
from app.backend.paths import safe_child
from app.llm import prompts
from app.llm.client import LLMClient, ParseError
from app.workers import enrichment_artifact as art
from app.workers import publication, reconciliation, reviews
from app.workers import labels
from app.workers.wiki_render import NODE_DIR, parse_frontmatter, render_synthesis_page

_PROMOTABLE = ("item",)
_CLAIM_TEXT_RE = re.compile(r'(?m)^claim_text:\s*"(.*)"\s*$')
_TITLE_RE = re.compile(r'(?m)^title:\s*"(.*)"\s*$')
_WS = re.compile(r"\s+")
_VERBATIM_WINDOW = 12  # consecutive-word run that counts as a copied source quote (guard)


def synthesis_id(topic_node_id: str) -> str:
    """Deterministic, one-per-topic synthesis id frozen at creation (ADR-0021)."""
    return f"syn_{hashlib.sha256(topic_node_id.encode('utf-8')).hexdigest()[:16]}"


def _review_subject(topic_node_id: str, fingerprint: str) -> dict[str, str]:
    """Fingerprint-scoped review identity, so each distinct evidence set is re-fileable."""
    return {"topic_node_id": topic_node_id, "fingerprint": fingerprint}


def _read_fm_title(page_path: Path) -> str | None:
    if not page_path.exists():
        return None
    m = _TITLE_RE.search(page_path.read_text(encoding="utf-8", errors="replace"))
    return re.sub(r"\\(.)", r"\1", m.group(1)) if m else None


def _page_status(synthesis_dir: Path, syn_id: str) -> str | None:
    """The synthesis PAGE status — the authority (ADR-0049: page frontmatter is authoritative, graph is the
    mirror). The generate-pass hidden guards check this too, so a page-hidden/graph-active partial state is
    still preserved (skip-only); drift repair belongs to the visibility executor/validator, not the generator."""
    p = synthesis_dir / f"{syn_id}.md"
    return parse_frontmatter(p.read_text(encoding="utf-8")).get("status") if p.exists() else None


# Synthesis statuses the generate pass must PRESERVE (never promote/refresh/retract/regenerate): an operator
# `hidden` (ADR-0049 decision 1) and an evidence-derived `evidence_hidden` (a supporting claim is hidden,
# decision 10). Both are visibility-suppression states the claim fan-out / hide-unhide executors own.
_PRESERVED_GENERATE_STATUSES = ("hidden", "evidence_hidden")
_PRODUCER = "generate_synthesis"            # its run guard and journal name


def _evidence_hidden(gconn, syn_id: str) -> bool:
    """True iff any active `derived_from` claim of this synthesis is hidden (ADR-0049 decision 10): the
    synthesis is materially derived from hidden evidence, so its prose is suppressed from default discovery
    (status `evidence_hidden`) until the evidence is visible again. The edge stays active in the graph."""
    return any((graph.get_node(gconn, e["dst_id"]) or {}).get("status") == "hidden"
               for e in graph.outgoing_active(gconn, syn_id) if e["edge_type"] == "derived_from")


def _claim_context(gconn, cid: str, *, claims_dir: Path, markdown_dir: Path) -> dict[str, Any] | None:
    """A contributing claim's durable text + citations reconstructed from its active edges; None
    if its page/wording is missing. A claim with no citations is not usable (caller drops it)."""
    page = safe_child(claims_dir, f"{cid}.md")  # cid is graph-derived (untrusted), ADR-0009
    if page is None or not page.exists():
        return None
    m = _CLAIM_TEXT_RE.search(page.read_text(encoding="utf-8", errors="replace"))
    if not m:
        return None
    claim_text = re.sub(r"\\(.)", r"\1", m.group(1))
    cites: list[dict[str, Any]] = []
    for e in graph.outgoing_active(gconn, cid):
        if e["edge_type"] != "derived_from":
            continue
        src, start, end = e["dst_id"], e["evidence_char_start"], e["evidence_char_end"]
        md_path = safe_child(markdown_dir, f"{src}.md")
        if md_path is None:
            continue  # non-canonical/path-like source id -> drop this citation, never read outside
        md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
        quote = md[start:end] if start is not None and end is not None and end <= len(md) else ""
        cites.append({"source_id": src, "char_start": start, "char_end": end, "quote": quote})
    cites.sort(key=lambda c: (c["source_id"], c["char_start"] if c["char_start"] is not None else -1))
    return {"claim_id": cid, "claim_text": claim_text, "citations": cites, "sources": [c["source_id"] for c in cites]}


def _disagreement_pairs(gconn, claim_ids: set[str]) -> list[tuple[str, str]]:
    """Active `contradicts` pairs (sorted, deduped) among a set of contributing claims."""
    pairs: set[tuple[str, str]] = set()
    for cid in claim_ids:
        for other in graph.active_contradictions_for_claim(gconn, cid):
            if other in claim_ids:
                pairs.add((min(cid, other), max(cid, other)))
    return sorted(pairs)


def _has_independent_pair(sources: set[str], prov: dict[str, dict[str, Any]]) -> bool:
    s = sorted(sources)
    return any(independent_sources(prov.get(s[i], {}), prov.get(s[j], {}))
               for i in range(len(s)) for j in range(i + 1, len(s)))


def eligible_topics(gconn, prov, *, claims_dir: Path, markdown_dir: Path) -> list[dict[str, Any]]:
    """Active knowledge-item topics with **≥2 grounded active claims from ≥2 independent
    sources**, checked over the *surviving* claim contexts — a claim whose page/citations are
    missing is dropped, then the trigger is re-verified (ADR-0031)."""
    topics: list[dict[str, Any]] = []
    for node_type in _PROMOTABLE:
        for nid in graph.active_node_ids_of_type(gconn, node_type):
            topic = topic_for(gconn, nid, prov, claims_dir=claims_dir, markdown_dir=markdown_dir)
            if topic is not None:
                topics.append(topic)
    return topics


def topic_for(gconn, nid: str, prov, *, claims_dir: Path, markdown_dir: Path) -> dict[str, Any] | None:
    """`nid`'s synthesis topic if it is eligible now (the rule of :func:`eligible_topics`), else None.
    Lets a commit re-check one topic under the write lock without rescanning the vault (ADR-0068)."""
    node = graph.get_node(gconn, nid)
    if node is None or node["status"] != "active" or node["node_type"] not in _PROMOTABLE:
        return None
    node_type = node["node_type"]
    claim_ids: set[str] = set()
    # ADR-0066 review round 2: only documents that KEEP the tag feed its synthesis — an
    # undecided suggestion elsewhere is not evidence the user tagged.
    for s in graph.kept_sources_for_node(gconn, nid):      # sources keeping the topic
        for clm in graph.claims_for_source(gconn, s):      # their active claims
            cnode = graph.get_node(gconn, clm)
            if cnode and cnode["status"] == "active":
                claim_ids.add(clm)
    # Build contexts and KEEP only grounded ones (have citations), then re-check the
    # trigger over the surviving claims' own sources — not the pre-filter set.
    ctxs = []
    for c in sorted(claim_ids):
        ctx = _claim_context(gconn, c, claims_dir=claims_dir, markdown_dir=markdown_dir)
        if ctx is not None and ctx["citations"]:
            ctxs.append(ctx)
    surviving_sources = {s for c in ctxs for s in c["sources"]}
    if len(ctxs) < 2 or not _has_independent_pair(surviving_sources, prov):
        return None
    node_dir = claims_dir.parent / NODE_DIR[node_type]
    topic_page = safe_child(node_dir, f"{node['slug']}.md")  # slug is graph-derived
    return {
        "node_id": nid, "node_type": node_type, "slug": node["slug"],
        "title": (_read_fm_title(topic_page) if topic_page else None) or node["slug"],
        "claims": ctxs,
        "disagreements": _disagreement_pairs(gconn, {c["claim_id"] for c in ctxs}),
    }


def _current_fingerprint(gconn, topic_id: str, model_ref: str, *, manifests_dir: Path,
                         claims_dir: Path, markdown_dir: Path) -> str | None:
    """The evidence fingerprint `topic_id` has now, or None when it is no longer an eligible topic
    (ADR-0068: compared under the write lock with the one the model was given)."""
    prov = {m["source_id"]: get_provenance(m) for m in valid_manifests(manifests_dir)[0]}
    topic = topic_for(gconn, topic_id, prov, claims_dir=claims_dir, markdown_dir=markdown_dir)
    return _fingerprint(topic, model_ref) if topic is not None else None

def _fingerprint(topic: dict[str, Any], model_ref: str) -> str:
    h = hashlib.sha256()
    parts = [art.SYNTHESIS_SCHEMA_VERSION, art.SYNTHESIS_PROMPT_VERSION, model_ref, topic["node_id"]]
    for c in topic["claims"]:
        parts.append(c["claim_id"])
        parts.append(c["claim_text"])
        for cite in c["citations"]:
            parts.append(f"{cite['source_id']}:{cite['char_start']}:{cite['char_end']}")
    for a, b in topic["disagreements"]:
        parts.append(f"{a}|{b}")
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def _disagreement_notes(topic: dict[str, Any]) -> list[str]:
    by_id = {c["claim_id"]: c["claim_text"] for c in topic["claims"]}
    return [f"{by_id.get(a, a)} <> {by_id.get(b, b)}" for a, b in topic["disagreements"]]


def _contains_verbatim_quote(text: str, source_texts: list[str], window: int = _VERBATIM_WINDOW) -> bool:
    """True if `text` contains a run of `window` consecutive words occurring verbatim
    (whitespace-normalized) in any source — a likely copied quote (direct-quote grounding is
    deferred, so such output is rejected rather than written, ADR-0031 §6)."""
    norm_sources = [_WS.sub(" ", s).lower() for s in source_texts]
    words = _WS.sub(" ", text).lower().split()
    for i in range(len(words) - window + 1):
        run = " ".join(words[i:i + window])
        if any(run in s for s in norm_sources):
            return True
    return False


def _artifact_meta(enrichment_dir: Path, topic_node_id: str) -> dict[str, Any]:
    """The stored synthesis artifact dict (for its `input_fingerprint` + recorded `model_ref`), or {}.

    ADR-0063 sticky-to-chain freshness needs the recorded model, not just the fingerprint."""
    apath = safe_child(enrichment_dir, f"{topic_node_id}.synthesis.json")  # tid untrusted
    if apath is None or not apath.exists():
        return {}
    try:
        return json.loads(apath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _page_content(gconn, *, syn_id, topic_node, title, summary, synthesis_text, confidence,
                  status, review_status, synthesis_dir: Path) -> str:
    """The synthesis page for this prose and status: claim links + disagreements from the graph."""
    # ADR-0049: a hidden claim is suppressed from the rendered Supporting Evidence links AND the
    # derived_from frontmatter (both are default-discovery surfaces on a browsable synthesis page, like
    # Source-page Claims / contradiction sections) — uniformly, regardless of the synthesis's own status.
    # The edge stays active in the graph (SoT); raw /graph/* still shows the syn -> hidden-claim edge.
    claim_ids = [e["dst_id"] for e in graph.outgoing_active(gconn, syn_id)
                 if e["edge_type"] == "derived_from"
                 and (graph.get_node(gconn, e["dst_id"]) or {}).get("status") != "hidden"]
    disagreements = _disagreement_pairs(gconn, set(claim_ids))
    # ADR-0060: page-local display labels for the claim links; renderer stays IO-free.
    link_labels = labels.display_labels(
        synthesis_dir.parent, [f"Claims/{cid}" for cid in claim_ids])
    return render_synthesis_page({
        "synthesis_id": syn_id, "title": title, "status": status, "review_status": review_status,
        "confidence": confidence, "topic_node": topic_node, "summary": summary,
        "synthesis_text": synthesis_text, "claim_ids": claim_ids, "disagreements": disagreements,
    }, labels=link_labels)


def _render_page(gconn, *, syn_id, topic_node, title, summary, synthesis_text, confidence,
                 status, review_status, synthesis_dir: Path, now: str) -> bool:
    """Render a synthesis page (filename = syn_id, for cross-type uniqueness): prose from the
    caller, claim links + disagreements from the graph, so the projection matches the graph.

    CHANGE-DETECTING (ADR-0049): the page is byte-stable (no wall-clock; freshness in input_fingerprint),
    so the file is rewritten only when its content differs and the graph node status mirror is upserted only
    when it differs — a re-render of an already-current synthesis is a true no-op (no page churn / reindex,
    so the claim fan-out can re-reconcile on every apply without steady-state churn). Returns True iff
    ANYTHING changed — the page file OR the graph node-status mirror — so the caller still triggers a
    reindex on a graph-mirror-only repair (e.g. a page-correct / graph-stale partial state)."""
    content = _page_content(gconn, syn_id=syn_id, topic_node=topic_node, title=title, summary=summary,
                            synthesis_text=synthesis_text, confidence=confidence, status=status,
                            review_status=review_status, synthesis_dir=synthesis_dir)
    synthesis_dir.mkdir(parents=True, exist_ok=True)
    page_path = synthesis_dir / f"{syn_id}.md"
    page_changed = (not page_path.exists()) or page_path.read_text(encoding="utf-8") != content
    if page_changed:
        page_path.write_text(content, encoding="utf-8")
    node = graph.get_node(gconn, syn_id)
    graph_changed = node is None or node["status"] != status
    if graph_changed:
        graph.upsert_node(gconn, node_id=syn_id, node_type="synthesis", slug=syn_id, status=status, now=now)
    return page_changed or graph_changed


def _commit_complete(gconn, *, rec: dict[str, Any], topic: dict[str, Any], syn_id: str,
                     meta: dict[str, Any], fp: str, synthesis_dir: Path, reviews_dir: Path) -> bool:
    """Whether the commit a journal record names finished (ADR-0068 producer journal).

    The commit writes the artifact last, stamped with the record's attempt id: a matching stamp
    means every write of THIS attempt landed. Files from an earlier, complete commit of the same
    evidence (a forced regeneration interrupted part-way) carry another stamp, so they never pass
    for it. Without a match (an older attempt, or a record rebuilt by journal repair), the whole
    expected state for evidence `fp` must hold: the artifact's fingerprint, the node, exactly the
    topic's active edges, a page that is exactly the artifact's rendering at the node's status,
    and the proposal (filed, possibly decided since)."""
    if rec.get("attempt") and meta.get("commit_attempt") == rec["attempt"]:
        return True
    tid = topic["node_id"]
    node = graph.get_node(gconn, syn_id)
    if meta.get("input_fingerprint") != fp or node is None:
        return False
    edges = {(e["edge_type"], e["dst_id"]) for e in graph.outgoing_active(gconn, syn_id)
             if e["edge_type"] in ("derived_from", "related_to")}
    if edges != {("derived_from", c["claim_id"]) for c in topic["claims"]} | {("related_to", tid)}:
        return False
    page = synthesis_dir / f"{syn_id}.md"
    text = page.read_text(encoding="utf-8") if page.exists() else ""
    fm = parse_frontmatter(text) if text else {}
    if fm.get("status") != node["status"]:
        return False
    # The page must be exactly the artifact's rendering (summary AND body), not merely mention its
    # summary: an interrupted attempt that kept the summary but rewrote the body left its own page.
    try:
        expected = _page_content(gconn, syn_id=syn_id, topic_node=tid, title=meta.get("title", tid),
                                 summary=meta.get("summary", ""), synthesis_text=meta.get("synthesis", ""),
                                 confidence=meta.get("confidence", "low"), status=node["status"],
                                 review_status=fm.get("review_status"), synthesis_dir=synthesis_dir)
    except ValueError:                                   # an unrenderable review_status on the page
        return False
    if text != expected:
        return False
    rid = reviews.review_id("propose_synthesis", _review_subject(tid, fp))
    return any((reviews_dir / d / f"{rid}.json").exists() for d in ("pending", "approved", "rejected"))


def _retired_or_absent(gconn, *, syn_id: str, synthesis_dir: Path) -> bool:
    """Whether an ineligible topic's unfinished commit needs nothing more: it never reached the graph
    or a page, or step 3 retracted its synthesis (node and page both `deprecated_candidate`)."""
    node = graph.get_node(gconn, syn_id)
    page_status = _page_status(synthesis_dir, syn_id)
    if node is None:
        return not (synthesis_dir / f"{syn_id}.md").exists()
    return node["status"] == "deprecated_candidate" and page_status == "deprecated_candidate"


def _withdraw_stale_pending(reviews_dir: Path, topic_node_id: str, keep_fp: str | None, now: str) -> None:
    """Withdraw pending propose_synthesis items for this topic under a *different* fingerprint
    (a regenerated candidate replaces the prior pending one)."""
    pend = reviews_dir / "pending"
    for path in sorted(pend.glob("*.json")) if pend.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        subj = item.get("subject") or {}
        if (item.get("type") == "propose_synthesis" and subj.get("topic_node_id") == topic_node_id
                and subj.get("fingerprint") != keep_fp):
            reviews.withdraw_review_item(reviews_dir, item["review_id"],
                                         reason="superseded by a fresher synthesis", now=now)


def _deprecate_synthesis(gconn, *, syn_id, topic_node, reviews_dir: Path, synthesis_dir: Path,
                         enrichment_dir: Path, reason: str, now: str) -> None:
    """Retract a synthesis through the audited deprecation path (claim/concept tombstone pattern):
    re-render `deprecated_candidate` with a coherent `review_status: pending`, file a
    `deprecate_wiki_page` item, and withdraw any pending proposals — never a bare status rewrite."""
    a = {}
    apath = art.synthesis_artifact_path(enrichment_dir, topic_node)
    if apath.exists():
        try:
            a = json.loads(apath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            a = {}
    _render_page(gconn, syn_id=syn_id, topic_node=topic_node, title=a.get("title", topic_node),
                 summary=a.get("summary", ""), synthesis_text=a.get("synthesis", ""),
                 confidence=a.get("confidence", "low"), status="deprecated_candidate",
                 review_status="pending", synthesis_dir=synthesis_dir, now=now)
    reviews.create_review_item(
        reviews_dir, review_type="deprecate_wiki_page",
        subject={"node_id": syn_id, "page": f"Synthesis/{syn_id}.md"},
        proposal={"to_status": "deprecated_candidate", "reason": reason},
        context={"node_type": "synthesis", "topic_node": topic_node}, now=now)
    _withdraw_stale_pending(reviews_dir, topic_node, keep_fp=None, now=now)


def apply_resolved_syntheses(gconn, reviews_dir: Path, *, synthesis_dir: Path,
                             enrichment_dir: Path, now: str) -> dict[str, int]:
    """Apply human decisions: approve → synthesis `active`, reject → `deprecated_candidate`
    (review-only promotion, no recurrence — ADR-0031). Renders from the synthesis artifact."""
    promoted = rejected = 0
    for state, status, rstatus in (("approved", "active", "approved"),
                                   ("rejected", "deprecated_candidate", "rejected")):
        d = reviews_dir / state
        for path in sorted(d.glob("*.json")) if d.exists() else []:
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if item.get("type") != "propose_synthesis":
                continue
            subj = item.get("subject") or {}
            topic_node = subj.get("topic_node_id")
            if not topic_node:
                continue
            syn_id = synthesis_id(topic_node)
            node = graph.get_node(gconn, syn_id)
            # ADR-0049: a `hidden`/`evidence_hidden` synthesis is a visibility-suppression state — a lingering
            # approved/rejected proposal must not flip it active/deprecated. Check the AUTHORITATIVE page
            # status too, so a page-suppressed/graph-active partial state is also preserved (skip-only; the
            # unhide executor / claim fan-out / validator owns drift repair, not the generator).
            if (node is None or node["status"] == status
                    or node["status"] in _PRESERVED_GENERATE_STATUSES
                    or _page_status(synthesis_dir, syn_id) in _PRESERVED_GENERATE_STATUSES):
                continue  # nothing indexed, already in target, or visibility-suppressed (page or graph)
            apath = art.synthesis_artifact_path(enrichment_dir, topic_node)
            if not apath.exists():
                continue
            a = json.loads(apath.read_text(encoding="utf-8"))
            _render_page(gconn, syn_id=syn_id, topic_node=topic_node, title=a["title"],
                         summary=a["summary"], synthesis_text=a["synthesis"], confidence=a["confidence"],
                         status=status, review_status=rstatus, synthesis_dir=synthesis_dir, now=now)
            promoted += state == "approved"
            rejected += state == "rejected"
    return {"promoted": promoted, "rejected": rejected}


_SYN_PAGE_RE = re.compile(r"Synthesis/syn_[0-9a-f]{16}\.md")  # canonical synthesis page shape (untrusted subject)


def _apply_synthesis_visibility_transition(
    gconn, reviews_dir: Path, *, review_type: str, hide: bool, synthesis_dir: Path,
    enrichment_dir: Path, now: str | None = None,
) -> dict[str, Any]:
    """Shared SYNTHESIS visibility executor (ADR-0049) for `hide_synthesis` (hide=True: active -> hidden)
    and `unhide_synthesis` (hide=False: hidden -> active). Re-renders the page via `_render_page` from the
    synthesis artifact (the prose record) + the graph (Supporting Evidence / Disagreements), writing the
    target `status` + `review_status: approved` (an active synthesis is a promoted/approved one — ADR-0049,
    intentionally unlike concept/claim unhide which restore `none`). Graph-REQUIRED; reads BOTH page + graph
    so a page/graph disagreement is a typed `partial_*_state` skip (never a silent no-op); never deletes
    edges. Idempotent: an already-hidden hide / a non-hidden unhide is a silent no-op; hide of a non-active
    synthesis is a typed `synthesis_not_active` skip. Returns
    `{applied, normalized, skipped, changed_pages, graph_changed}`."""
    now = now or iso_now()
    applied = normalized = 0
    skipped: list[dict[str, str]] = []
    changed_pages: list[str] = []
    expected_to = "hidden" if hide else "active"
    approved = reviews_dir / "approved"

    for path in sorted(approved.glob("*.json")) if approved.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if item.get("type") != review_type:
            continue
        rid = str(item.get("review_id", ""))
        subj = item.get("subject") or {}
        proposal = item.get("proposal") or {}
        page, nid = subj.get("page"), subj.get("node_id")
        if proposal.get("to_status") != expected_to:
            skipped.append({"review_id": rid, "reason": "unexpected_to_status"})
            continue
        if not page or not nid:
            skipped.append({"review_id": rid, "reason": "missing_subject"})
            continue
        if not _SYN_PAGE_RE.fullmatch(page):       # path safety BEFORE any read (untrusted subject)
            skipped.append({"review_id": rid, "reason": "invalid_page_path"})
            continue
        node = graph.get_node(gconn, nid)
        if node is None:
            skipped.append({"review_id": rid, "reason": "node_missing"})
            continue
        if node["node_type"] != "synthesis":
            skipped.append({"review_id": rid, "reason": "out_of_scope"})
            continue
        # The graph node is authoritative for the page path: subject.page must be EXACTLY the node's
        # canonical page (slug == syn_id). All reads/writes use the canonical page, never raw subject.page.
        canonical_page = f"Synthesis/{node['slug']}.md"
        ctx_type = (item.get("context") or {}).get("node_type")
        if page != canonical_page or (ctx_type and ctx_type != "synthesis"):
            skipped.append({"review_id": rid, "reason": "page_node_mismatch"})
            continue

        # Read BOTH authorities (page authoritative, graph node mirrored): a page/graph disagreement is a
        # typed skip, NEVER a silent no-op (ADR-0049 reopen-safety; mirrors the claim/semantic executors).
        page_path = synthesis_dir / f"{node['slug']}.md"
        fm = parse_frontmatter(page_path.read_text(encoding="utf-8")) if page_path.exists() else {}
        page_hidden = fm.get("status") == "hidden"
        graph_hidden = node["status"] == "hidden"
        if page_hidden != graph_hidden:
            skipped.append({"review_id": rid,
                            "reason": "partial_hide_state" if hide else "partial_unhide_state"})
            continue
        if hide:
            if page_hidden:                                  # both hidden
                if fm.get("review_status") == "approved":
                    continue                                 # fully effected -> silent no-op
                counts_as = "normalized"                     # both hidden, review pending -> fix review_status
            elif node["status"] == "active":
                counts_as = "applied"                        # the active -> hidden transition
            else:
                skipped.append({"review_id": rid, "reason": "synthesis_not_active"})  # hide is active-only
                continue
        elif not page_hidden:
            continue                                         # unhide: both not hidden -> already un-hidden
        else:
            counts_as = "applied"                            # unhide: both hidden -> active

        # Re-render from the synthesis artifact (the prose record, keyed by topic_node) — the SAME source
        # apply_resolved_syntheses renders from. topic_node comes from the (UNTRUSTED) page, so BIND it to
        # this synthesis: synthesis_id(topic_node) MUST equal nid (the deterministic one-per-topic id hash,
        # ADR-0021/0049). Without this a tampered page could point topic_node at ANOTHER topic's artifact and
        # re-render THIS page with the wrong title/summary/prose while keeping syn_id + its graph edges. The
        # artifact's own node_id is a second, defence-in-depth match. Both are typed skips (never silent).
        topic_node = fm.get("topic_node")
        if not topic_node or synthesis_id(topic_node) != nid:
            skipped.append({"review_id": rid, "reason": "synthesis_topic_mismatch"})
            continue
        apath = safe_child(enrichment_dir, f"{topic_node}.synthesis.json")
        if apath is None or not apath.exists():
            skipped.append({"review_id": rid, "reason": "synthesis_artifact_missing"})
            continue
        try:
            a = json.loads(apath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            skipped.append({"review_id": rid, "reason": "synthesis_artifact_unreadable"})
            continue
        if a.get("node_id") != nid:
            skipped.append({"review_id": rid, "reason": "synthesis_artifact_mismatch"})
            continue
        # Operator hide -> `hidden`. Operator UNHIDE clears the operator hide, but must not re-expose a
        # synthesis whose evidence is still hidden: restore to `evidence_hidden` if a supporting claim is
        # still hidden, else `active` (ADR-0049 decision 10 — operator hide wins, but evidence suppression
        # outlives it).
        render_status = expected_to if hide else (
            "evidence_hidden" if _evidence_hidden(gconn, nid) else "active")
        _render_page(gconn, syn_id=nid, topic_node=topic_node, title=a.get("title", topic_node),
                     summary=a.get("summary", ""), synthesis_text=a.get("synthesis", ""),
                     confidence=a.get("confidence", "low"), status=render_status,
                     review_status="approved", synthesis_dir=synthesis_dir, now=now)
        changed_pages.append(canonical_page)
        if counts_as == "applied":
            applied += 1
        else:
            normalized += 1

    return {"applied": applied, "normalized": normalized, "skipped": skipped,
            "changed_pages": changed_pages, "graph_changed": applied + normalized > 0}


def rerender_synthesis_page(gconn, syn_id: str, *, synthesis_dir: Path, enrichment_dir: Path,
                            now: str | None = None) -> tuple[str, bool] | None:
    """Re-render an existing synthesis page so its Supporting Evidence + status reflect the current
    claim-visibility (ADR-0049 claim -> synthesis fan-out). The hidden-claim links/frontmatter are dropped
    by `_render_page`; the **status** is recomputed by precedence (decision 10) from the **authoritative PAGE
    status** (NOT the possibly-stale graph mirror — so a page-hidden/graph-active partial state isn't
    downgraded; the graph mirror is then repaired to match):
      - page `hidden` (operator hide_synthesis) -> stays `hidden` (operator hide wins over evidence restore);
      - page `active` / `evidence_hidden` -> `evidence_hidden` if ANY supporting claim is hidden, else
        `active` (auto-suppress / restore — only `active` is default-discoverable, so only it is suppressed);
      - page `candidate` / `deprecated_candidate` -> unchanged (already not default-discoverable; no leak).
    Bound to the node via `synthesis_id(topic_node) == syn_id` + the artifact's own `node_id` (the untrusted
    -page guard). Returns `(new_status, changed)` — `changed` is True iff the page OR the graph mirror was
    written (False when already fully current, so the fan-out re-reconciles every apply without steady-state
    churn; True on a graph-mirror-only repair so the caller still reindexes) — or None if the page is
    missing/unbindable/artifact-gone (left untouched; the caller treats this as an unreconciled fan-out)."""
    now = now or iso_now()
    node = graph.get_node(gconn, syn_id)
    if node is None:
        return None
    page_path = synthesis_dir / f"{syn_id}.md"
    fm = parse_frontmatter(page_path.read_text(encoding="utf-8")) if page_path.exists() else {}
    topic_node = fm.get("topic_node")
    if not topic_node or synthesis_id(topic_node) != syn_id:
        return None                                      # untrusted/unbindable page -> leave it untouched
    apath = safe_child(enrichment_dir, f"{topic_node}.synthesis.json")
    if apath is None or not apath.exists():
        return None
    try:
        a = json.loads(apath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if a.get("node_id") != syn_id:
        return None
    page_status = fm.get("status")                       # PAGE is authoritative (ADR-0049), not the mirror
    if page_status in ("hidden", "candidate", "deprecated_candidate"):
        target = page_status                             # operator-hidden / not-default-discoverable -> preserve
        review_status = fm.get("review_status") or "approved"
    else:                                                # active / evidence_hidden / unknown -> recompute
        target = "evidence_hidden" if _evidence_hidden(gconn, syn_id) else "active"
        review_status = "approved"
    changed = _render_page(gconn, syn_id=syn_id, topic_node=topic_node, title=a.get("title", topic_node),
                           summary=a.get("summary", ""), synthesis_text=a.get("synthesis", ""),
                           confidence=a.get("confidence", "low"), status=target,
                           review_status=review_status, synthesis_dir=synthesis_dir, now=now)
    return (target, changed)


def apply_hidden_syntheses(
    gconn, reviews_dir: Path, *, synthesis_dir: Path, enrichment_dir: Path, now: str | None = None,
) -> dict[str, Any]:
    """Apply approved `hide_synthesis` decisions: an active synthesis -> hidden (ADR-0049). Thin wrapper
    over the shared synthesis visibility executor; graph-REQUIRED."""
    return _apply_synthesis_visibility_transition(
        gconn, reviews_dir, review_type="hide_synthesis", hide=True, synthesis_dir=synthesis_dir,
        enrichment_dir=enrichment_dir, now=now)


def apply_unhidden_syntheses(
    gconn, reviews_dir: Path, *, synthesis_dir: Path, enrichment_dir: Path, now: str | None = None,
) -> dict[str, Any]:
    """Apply approved `unhide_synthesis` decisions: a hidden synthesis -> active (ADR-0049). Thin wrapper;
    graph-REQUIRED."""
    return _apply_synthesis_visibility_transition(
        gconn, reviews_dir, review_type="unhide_synthesis", hide=False, synthesis_dir=synthesis_dir,
        enrichment_dir=enrichment_dir, now=now)


def generate_syntheses(
    root: Path,
    *,
    client: LLMClient,
    model_ref: str,
    manifests_dir: Path | None = None,
    jobs_db: Path | None = None,
    graph_db: Path | None = None,
    markdown_dir: Path | None = None,
    enrichment_dir: Path | None = None,
    wiki_dir: Path | None = None,
    reviews_dir: Path | None = None,
    force: bool = False,
    rebuild_index: bool = True,
    record_job: bool = True,
) -> dict[str, Any]:
    """Generate candidate cross-source syntheses for eligible topics; return a run summary."""
    root = Path(root).resolve()
    manifests_dir = Path(manifests_dir) if manifests_dir else root / "raw" / "manifests"
    jobs_db = Path(jobs_db) if jobs_db else root / "db" / "jobs.sqlite"
    graph_db = Path(graph_db) if graph_db else root / "db" / "graph.sqlite"
    markdown_dir = Path(markdown_dir) if markdown_dir else root / "normalized" / "markdown"
    enrichment_dir = Path(enrichment_dir) if enrichment_dir else root / "normalized" / "enrichment"
    wiki_dir = Path(wiki_dir) if wiki_dir else root / "wiki"
    reviews_dir = Path(reviews_dir) if reviews_dir else root / "reviews"
    claims_dir = wiki_dir / "Claims"
    synthesis_dir = wiki_dir / "Synthesis"

    now = iso_now()
    job_id = f"job_{uuid.uuid4().hex[:16]}"
    conn = None
    if record_job:
        db.init_db(jobs_db)
        conn = db.connect(jobs_db)
        db.insert_job(conn, job_id=job_id, job_type="generate_synthesis", status="running",
                      created_at=now, started_at=now)

    graph.init_db(graph_db)
    gconn = graph.connect(graph_db)

    try:
        # ADR-0063: resolve the tier chain INSIDE the protected block so a malformed-chain ConfigError
        # marks the job failed and closes connections rather than orphaning a "running" job. `chain_refs`
        # (plain split, pre-resolution) drives sticky-to-chain freshness; resolve_run_model validates.
        chain_refs = [c.strip() for c in model_ref.split(",") if c.strip()]
        model_ref, has_key = client.resolve_run_model(model_ref)
        # ADR-0068: steps 1-3 are deterministic and commit under the write lock in one phase, which
        # records its intent first (`full`: the syntheses it changes are not known up front).
        run = publication.Run(root, "generate_synthesis", search=rebuild_index, wiki_dir=wiki_dir)
        retracted = 0
        # ADR-0068: topics whose commit an earlier run left unfinished (fails closed when damaged).
        unfinished = journal.pending(root, _PRODUCER)
        journal_verified: list[str] = []      # unfinished records found complete and cleared
        blocked: list[str] = []               # ... a human decision keeps from being redone
        captured: dict[str, str] = {}         # independence digests this run decides with
        with write_lock.hold(reviews_dir):
            intent = run.intent(full=True, refresh=False)
            # 1. Apply human decisions (deterministic; runs without a key).
            resolution = apply_resolved_syntheses(gconn, reviews_dir, synthesis_dir=synthesis_dir,
                                                  enrichment_dir=enrichment_dir, now=now)

            # 2. Eligible topics from the current active graph.
            _valid, _skipped_invalid = valid_manifests(manifests_dir)
            prov = {m["source_id"]: get_provenance(m) for m in _valid}
            # ADR-0066 part 3 slice 3: the independence inputs this run decides with.
            captured = reconciliation.capture(prov)
            topics = eligible_topics(gconn, prov, claims_dir=claims_dir, markdown_dir=markdown_dir)
            eligible_ids = {t["node_id"] for t in topics}

            # 3. Retract syntheses whose topic is no longer eligible (audited deprecation, keyless).
            for syn in graph.nodes_of_type(gconn, "synthesis"):
                page = synthesis_dir / f"{syn['node_id']}.md"
                page_fm = parse_frontmatter(page.read_text(encoding="utf-8")) if page.exists() else {}
                # ADR-0049: a `hidden`/`evidence_hidden` synthesis (page authority OR graph mirror) is a
                # visibility-suppression state — the generate pass NEVER retracts it (a tombstone would re-expose
                # it: deprecated_candidate is in default retrieval). Skip-only; the executor/fan-out owns drift.
                if (syn["status"] in ("deprecated_candidate", *_PRESERVED_GENERATE_STATUSES)
                        or page_fm.get("status") in _PRESERVED_GENERATE_STATUSES):
                    continue
                topic_node = page_fm.get("topic_node")
                if topic_node not in eligible_ids:
                    _deprecate_synthesis(gconn, syn_id=syn["node_id"], topic_node=topic_node or "",
                                         reviews_dir=reviews_dir, synthesis_dir=synthesis_dir,
                                         enrichment_dir=enrichment_dir,
                                         reason="topic no longer an eligible synthesis subject", now=now)
                    retracted += 1
            # A topic no longer eligible is never regenerated: its record is done once step 3
            # retracted the synthesis (or it never reached the graph); a hidden one stays blocked.
            for tid in sorted(set(unfinished) - eligible_ids):
                if _retired_or_absent(gconn, syn_id=synthesis_id(tid), synthesis_dir=synthesis_dir):
                    journal.finish(root, _PRODUCER, tid)
                    journal_verified.append(tid)
                    del unfinished[tid]
                else:
                    blocked.append(tid)
            run.publish(intent, refresh=bool(retracted or resolution["promoted"]
                                             or resolution["rejected"]))

        considered = len(topics)
        written = skipped_fresh = skipped_reviewed = stale_active = changed_during_call = 0
        errors: list[dict[str, str]] = []

        if has_key:
            for t in topics:
                tid = t["node_id"]
                syn_id = synthesis_id(tid)
                fp = _fingerprint(t, model_ref)   # resolved-model fingerprint (used if we (re)generate)
                node = graph.get_node(gconn, syn_id)
                status = node["status"] if node else None
                meta = _artifact_meta(enrichment_dir, tid)
                # ADR-0068 producer journal: an unfinished commit whose writes all landed (it stopped
                # just before clearing its record) is verified and cleared; otherwise it is redone.
                rec = unfinished.get(tid)
                if rec is not None and _commit_complete(
                        gconn, rec=rec, topic=t, syn_id=syn_id, meta=meta,
                        fp=rec["fingerprint"] or _fingerprint(t, meta.get("model_ref") or model_ref),
                        synthesis_dir=synthesis_dir, reviews_dir=reviews_dir):
                    with write_lock.hold(reviews_dir):
                        journal.finish(root, _PRODUCER, tid)
                    journal_verified.append(tid)
                    rec = None
                # ADR-0049: never regenerate a `hidden`/`evidence_hidden` synthesis (page authority OR graph
                # mirror) — no LLM call, no node reset. Skip-only; the executor/claim fan-out owns drift.
                if (status in _PRESERVED_GENERATE_STATUSES
                        or _page_status(synthesis_dir, syn_id) in _PRESERVED_GENERATE_STATUSES):
                    if rec is not None:
                        blocked.append(tid)            # a hide wins over replaying the commit
                    continue
                # ADR-0063 sticky-to-chain: an existing artifact stays fresh while its recorded model is
                # still a chain member and its own-model fingerprint matches, so an availability flip
                # alone (heavy tier opted into a local chain) never re-nags or restales a reviewed node.
                # An unfinished commit is never fresh: the artifact predates it (written last).
                fresh = rec is None and art.chain_fresh(
                    meta, chain_refs, lambda m, topic=t: _fingerprint(topic, m))
                # The review identity keys on the CURRENT evidence's fingerprint. When chain-fresh, the
                # current evidence is the one the RECORDED model produced, so the rejected/proposal lookup
                # must use the recorded-model fingerprint — else an availability flip mints a new rid and
                # re-nags a previously-rejected synthesis (the resolved model's fingerprint differs).
                current_fp = _fingerprint(t, meta.get("model_ref")) if fresh else fp
                rid = reviews.review_id("propose_synthesis", _review_subject(tid, current_fp))
                rejected_current = (reviews_dir / "rejected" / f"{rid}.json").exists()

                # Governance gate (Q1/Q2): never rewrite a reviewed synthesis in the normal pass.
                if status == "active":
                    if rec is not None and not force:
                        blocked.append(tid)            # approved meanwhile: never overwritten
                        continue
                    if fresh:
                        continue                       # approved & current — done
                    stale_active += 1                  # evidence changed since approval
                    if not force:
                        continue                       # stays active (Q1); --force re-opens
                elif rejected_current:
                    skipped_reviewed += 1              # this exact evidence was rejected — no re-nag
                    if rec is not None:
                        blocked.append(tid)
                    continue
                elif status == "candidate" and fresh and not force:
                    skipped_fresh += 1                 # candidate already generated for this evidence
                    continue

                src_texts = []
                for c in t["claims"]:
                    for s in {cite["source_id"] for cite in c["citations"]}:
                        p = markdown_dir / f"{s}.md"
                        if p.exists():
                            src_texts.append(p.read_text(encoding="utf-8"))
                try:
                    result = client.parse(
                        prompts.build_synthesis_messages(t["title"], t["claims"], _disagreement_notes(t)),
                        prompts.SYNTHESIS_SCHEMA, model_ref,
                        schema_version=art.SYNTHESIS_SCHEMA_VERSION,
                        prompt_version=art.SYNTHESIS_PROMPT_VERSION,
                        operation=tid,
                    )
                except ParseError as exc:
                    errors.append({"topic": tid, "error": str(exc)})
                    continue
                if _contains_verbatim_quote(result["summary"] + " " + result["synthesis"], src_texts):
                    errors.append({"topic": tid, "error": "synthesis copied a verbatim source quote (rejected)"})
                    continue
                confidence = round(max(0.0, min(1.0, float(result["confidence"]))), 3)

                # ADR-0068: the model call ran outside the write lock; a human may have hidden or
                # approved this synthesis meanwhile, so re-read its status under the lock first.
                with write_lock.hold(reviews_dir):
                    now_status = (graph.get_node(gconn, syn_id) or {}).get("status")
                    if (now_status in _PRESERVED_GENERATE_STATUSES
                            or _page_status(synthesis_dir, syn_id) in _PRESERVED_GENERATE_STATUSES
                            or (now_status == "active" and status != "active")):
                        if rec is not None:
                            blocked.append(tid)
                        continue
                    # The result is valid only for the evidence the model saw: the topic must still
                    # be eligible with the same claims, wording and anchors (a tag link dropped, a
                    # claim hidden or the topic hidden/merged meanwhile changes or removes it).
                    if _current_fingerprint(gconn, tid, model_ref, manifests_dir=manifests_dir,
                                            claims_dir=claims_dir, markdown_dir=markdown_dir) != fp:
                        changed_during_call += 1
                        continue
                    # A decision on the current proposal recorded while the model ran wins: an
                    # approval is of the prose the human read (Apply promotes the artifact), so it
                    # must not be overwritten; a rejection must not be re-filed.
                    fresh_rid = reviews.review_id("propose_synthesis", _review_subject(tid, fp))
                    if any((reviews_dir / d / f"{fresh_rid}.json").exists()
                           for d in ("approved", "rejected")):
                        skipped_reviewed += 1
                        if rec is not None:
                            blocked.append(tid)
                        continue
                    # The commit writes the node, its edges, the page, the proposal and the artifact
                    # last. Its journal record (with the evidence fingerprint it commits) keeps the
                    # topic from looking fresh until all of them landed; a crash inside it also leaves
                    # this synthesis queued for publication.
                    topic_intent = run.intent(syntheses=[syn_id])
                    attempt = journal.begin(root, _PRODUCER, tid, fingerprint=fp, now=now)["attempt"]
                    # Index the node candidate + (re)write grounded edges: supersede prior synthesis
                    # edges, then derived_from -> each contributing claim and related_to -> the topic.
                    graph.upsert_node(gconn, node_id=syn_id, node_type="synthesis", slug=syn_id,
                                      status="candidate", now=now)
                    for e in graph.outgoing_active(gconn, syn_id):
                        if e["edge_type"] in ("derived_from", "related_to"):
                            graph.set_status(gconn, e["edge_id"], "superseded", now=now)
                    for c in t["claims"]:
                        graph.upsert_assertion(gconn, src_id=syn_id, dst_id=c["claim_id"],
                                               edge_type="derived_from", asserted_by="llm",
                                               status="active", job_id=job_id, now=now)
                    graph.upsert_assertion(gconn, src_id=syn_id, dst_id=tid, edge_type="related_to",
                                           asserted_by="llm", status="active", job_id=job_id, now=now)

                    _render_page(gconn, syn_id=syn_id, topic_node=tid, title=t["title"],
                                 summary=result["summary"], synthesis_text=result["synthesis"],
                                 confidence=confidence, status="candidate", review_status="pending",
                                 synthesis_dir=synthesis_dir, now=now)
                    _withdraw_stale_pending(reviews_dir, tid, keep_fp=fp, now=now)
                    reviews.create_review_item(
                        reviews_dir, review_type="propose_synthesis",
                        subject=_review_subject(tid, fp),
                        proposal={"to_status": "active", "synthesis_id": syn_id, "title": t["title"],
                                  "claim_ids": [c["claim_id"] for c in t["claims"]],
                                  "summary": result["summary"]},
                        context={"node_type": t["node_type"]}, priority="medium", now=now)
                    apath = art.synthesis_artifact_path(enrichment_dir, tid)
                    apath.parent.mkdir(parents=True, exist_ok=True)
                    apath.write_text(json.dumps({
                        "node_id": syn_id, "topic_node_id": tid, "title": t["title"],
                        "summary": result["summary"], "synthesis": result["synthesis"],
                        "confidence": confidence, "input_fingerprint": fp,
                        "schema_version": art.SYNTHESIS_SCHEMA_VERSION, "model_ref": model_ref,
                        "generation_status": "enriched", "generated_at": now,
                        "commit_attempt": attempt,       # the journal attempt this commit finished
                    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    run.publish(topic_intent)             # the page was rendered above
                    journal.finish(root, _PRODUCER, tid)
                    written += 1

        pub = run.finish()                    # index.md + the keyword index, once (decision 5)
        left = journal.pending(root, _PRODUCER)
        # A topic this run attempted whose commit is still unfinished is never a clean success.
        left_attempted = sorted(set(left) & {t["node_id"] for t in topics}) if has_key else []

        if errors or left_attempted:
            status = "partial"
        elif not has_key and considered > 0:
            status = "skipped"
        else:
            status = "succeeded"
        status = publication.job_status(status, pub)
        # ADR-0066 part 3 slice 3: a completed run acknowledges the documents whose independence
        # inputs are still the ones it decided with (read under the lock, so none lands between).
        with write_lock.hold(reviews_dir, changes=False):
            acknowledged = reconciliation.verify(manifests_dir, captured, status)

        summary: dict[str, Any] = {
            "job_id": job_id, "model_ref": model_ref, "status": status,
            "eligible_topics": considered, "syntheses_written": written,
            "skipped_fresh": skipped_fresh, "skipped_reviewed": skipped_reviewed,
            "skipped_changed_during_call": changed_during_call,
            "stale_active": stale_active, "retracted": retracted,
            "promoted": resolution["promoted"], "rejected": resolution["rejected"],
            "manifests_skipped_invalid": len(_skipped_invalid),
            "index_rebuilt": bool(pub and pub["index_rebuilt"]),
            "keyword_reindexed": bool(pub and pub["keyword_reindexed"]),
            "publication": publication.summary(pub),
            # ADR-0068 producer journal: topics whose commit is still unfinished, those a human
            # decision keeps from being redone, and the ones verified complete and cleared.
            "unfinished_commits": sorted(left), "unfinished_blocked": sorted(set(blocked)),
            "journal_verified": journal_verified,
            "independence_acknowledged": acknowledged,
            "errors": len(errors), "error_details": errors, "generated_at": now,
        }
        if conn is not None:
            db.update_job(conn, job_id, status=status, finished_at=iso_now(), metadata=summary)
        return summary
    except Exception as exc:
        if conn is not None:
            db.update_job(conn, job_id, status="failed", finished_at=iso_now(), error_message=str(exc))
        raise
    finally:
        gconn.close()
        if conn is not None:
            conn.close()
