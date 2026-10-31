#!/usr/bin/env python3
"""Contradiction resolution: applying human decisions on `contradicts` assertions (ADR-0030/0031).

What remains here after ADR-0069 slice 0 is the deterministic, model-free half: Apply's executor
for approved/rejected `resolve_contradiction` items (`apply_contradiction_decisions`, composing
`apply_resolved_contradictions` with the Claim-page re-projection) and the `supersede` effects
(ADR-0044). An assertion is one sorted-pair `contradicts` edge (`src_id < dst_id`), born `proposed`,
invisible until a human approves it (ADR-0030); the LLM proposes, the human disposes (CLAUDE.md
rule 9).

The whole-vault judging sweep (graph-neighbourhood blocking over every claim pair, one tier-3 call
per pair) was removed by ADR-0069 slice 0: under ADR-0066 items are broad user tags, so it no longer
bounded the work (the part-3 UAT built 10,500 pairs). Contradiction checks return as an explicit,
user-selected document scope (estimate → token → run, windowed batch judging) in slice 2(b).

**Contradiction maintenance** (ADR-0069 slice 2(a), decision 8) is model-free: it retires a
**proposed** assertion whose evidence no longer stands as judged — a claim without evidence, a claim
whose wording or evidence changed since the verdict (even under the same claim id), or two documents
no longer independent. It never touches an `active` (human-approved) assertion, nor a proposal a
human already decided that awaits Apply; leaving a scope, dropping a tag or new window boundaries
retire nothing. Retiring supersedes the edge and withdraws the pending review (audited). It changes
no page: pages render only `active` contradictions (ADR-0030). It runs targeted after a Details save
or clear and after claim re-extraction, and in full via `detect_contradictions.py maintain`; lint and
the Source screen report what the read-only check finds, so a failed or skipped maintenance stays
visible. Maintenance being current says nothing about whether documents were *checked* for
contradictions. Nothing in this module calls a model.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

from app.backend import db, graph, write_lock
from app.backend.manifests import get_provenance, independent_sources, iso_now, valid_manifests
from app.workers import claims, reviews

_CLAIM_TEXT_RE = re.compile(r'(?m)^claim_text:\s*"(.*)"\s*$')


# --- claim context (durable wording + reconstructed citations) -------------


def _read_claim_text(page_path: Path) -> str | None:
    """Read the durable claim_text from a Claim page's frontmatter (page is the authority)."""
    if not page_path.exists():
        return None
    m = _CLAIM_TEXT_RE.search(page_path.read_text(encoding="utf-8", errors="replace"))
    return re.sub(r"\\(.)", r"\1", m.group(1)) if m else None


def _claim_context(gconn, cid: str, *, claims_dir: Path, markdown_dir: Path) -> dict[str, Any] | None:
    """A claim's review-facing context: durable text + citations reconstructed from its active
    `derived_from` edges (quotes read back from the source spans). None if it has no wording."""
    claim_text = _read_claim_text(claims_dir / f"{cid}.md")
    if claim_text is None:
        return None
    cites: list[dict[str, Any]] = []
    for e in graph.outgoing_active(gconn, cid):
        if e["edge_type"] != "derived_from":
            continue
        src, start, end = e["dst_id"], e["evidence_char_start"], e["evidence_char_end"]
        md_path = markdown_dir / f"{src}.md"
        md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
        quote = md[start:end] if start is not None and end is not None and end <= len(md) else ""
        cites.append({"source_id": src, "char_start": start, "char_end": end, "quote": quote})
    cites.sort(key=lambda c: (c["source_id"], c["char_start"] if c["char_start"] is not None else -1))
    return {"claim_id": cid, "claim_text": claim_text, "citations": cites}


# --- resolution application (deterministic effect of human decisions) ------


def _resolved_items(reviews_dir: Path, state: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    d = reviews_dir / state
    if not d.exists():
        return out
    for path in sorted(d.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if item.get("type") == "resolve_contradiction":
            out.append(item)
    return out


def apply_resolved_contradictions(
    gconn, reviews_dir: Path, *, claims_dir: Path, markdown_dir: Path,
    affected: set[str] | None = None, now: str | None = None,
) -> dict[str, int]:
    """Apply human decisions to the graph (deterministic, key-free; Apply's executor).

    `approved` → the pair's `contradicts` edge flips to `active` (acknowledge — a standing
    disagreement; both claims stay live). `rejected` → the edge flips to `rejected`. A
    `supersede` decision (an approved item naming a `winner`) activates the edge **and** executes
    the winner→loser effects (slice 1b, ADR-0031): an `active` `supersedes` edge plus the loser
    deprecated to `deprecated_candidate` through the `deprecate_wiki_page` audit path (the
    `contradicts` edge stays active so the historical conflict is recorded). Idempotent: only
    transitions a row whose status differs, and the supersede effects are applied once. Both
    endpoint claims of any transition are added to `affected` so the caller re-projects their
    Claim pages (a backlink appears on acknowledge, disappears on reject).
    """
    now = now or iso_now()
    affected = affected if affected is not None else set()
    acknowledged = rejected = superseded_executed = 0

    def _set(a: str, b: str, rows: list[dict[str, Any]], target: str, statuses: tuple[str, ...]) -> int:
        n = 0
        for r in rows:
            if r["status"] in statuses and r["status"] != target:
                graph.set_status(gconn, r["edge_id"], target, now=now)
                affected.update((a, b))
                n += 1
        return n

    for item in _resolved_items(reviews_dir, "approved"):
        subj = item.get("subject") or {}
        a, b = subj.get("claim_a"), subj.get("claim_b")
        if not a or not b:
            continue
        acknowledged += _set(a, b, graph.contradiction_between(gconn, a, b), "active", ("proposed",))
        winner = item.get("winner")
        if winner in (a, b):  # supersede decision: execute winner→loser effects (slice 1b)
            loser = b if winner == a else a
            if _execute_supersede(gconn, reviews_dir, winner=winner, loser=loser,
                                  source_review_id=item.get("review_id", ""), now=now,
                                  claims_dir=claims_dir, markdown_dir=markdown_dir):
                superseded_executed += 1
            affected.update((a, b))
    for item in _resolved_items(reviews_dir, "rejected"):
        subj = item.get("subject") or {}
        a, b = subj.get("claim_a"), subj.get("claim_b")
        if not a or not b:
            continue
        rejected += _set(a, b, graph.contradiction_between(gconn, a, b), "rejected", ("proposed", "active"))

    return {"acknowledged": acknowledged, "rejected": rejected,
            "superseded_executed": superseded_executed}


def _reproject_claim_pages(gconn, cids, *, claims_dir: Path, reviews_dir: Path, markdown_dir: Path,
                           now: str) -> list[str]:
    """Re-render the given Claim pages from the graph; return the cids whose page was (re)written.

    The re-projection unit of `apply_contradiction_decisions` (ADR-0035 A4). Contradiction
    maintenance needs none: it retires only proposals, which no page renders. No index rebuild here
    (the caller owns the single rebuild)."""
    written: list[str] = []
    for cid in sorted(cids):
        if claims.recompose_claim(gconn, cid=cid, claims_dir=claims_dir, reviews_dir=reviews_dir,
                                  markdown_dir=markdown_dir, now=now) == "written":
            written.append(cid)
    return written


def apply_contradiction_decisions(gconn, reviews_dir: Path, *, claims_dir: Path, markdown_dir: Path,
                                  now: str | None = None) -> dict[str, Any]:
    """Key-free Phase-6 apply: record contradiction decisions to the graph + re-project the affected
    Claim pages (ADR-0035 A4). Composes the existing `apply_resolved_contradictions` executor with the
    shared `_reproject_claim_pages` helper. **No index
    rebuild** (the caller — `/reviews/apply` — owns the single final rebuild). Returns
    `{resolution, changed_pages, graph_changed}`."""
    now = now or iso_now()
    affected: set[str] = set()
    resolution = apply_resolved_contradictions(
        gconn, reviews_dir, claims_dir=claims_dir, markdown_dir=markdown_dir, affected=affected, now=now)
    active_claims = set(graph.active_node_ids_of_type(gconn, "claim"))
    changed_pages = _reproject_claim_pages(
        gconn, affected & active_claims, claims_dir=claims_dir, reviews_dir=reviews_dir,
        markdown_dir=markdown_dir, now=now)
    graph_changed = bool(resolution["acknowledged"] or resolution["rejected"]
                         or resolution["superseded_executed"])
    return {"resolution": resolution, "changed_pages": changed_pages, "graph_changed": graph_changed}


def _execute_supersede(
    gconn, reviews_dir: Path, *, winner: str, loser: str, source_review_id: str, now: str,
    claims_dir: Path, markdown_dir: Path,
) -> bool:
    """Execute an approved `supersede` resolution (slice 1b, ADR-0031): an `active` `supersedes`
    edge winner→loser, and the loser deprecated to `deprecated_candidate` via the
    `deprecate_wiki_page` audit path — authorized by the contradiction approval (no second human
    gate), with the cause recorded. The `contradicts` edge is left active by the caller. Returns
    True if it applied effects this call; idempotent (a no-op once already executed)."""
    already_edge = any(e["dst_id"] == loser and e["edge_type"] == "supersedes"
                       for e in graph.outgoing_active(gconn, winner))
    loser_node = graph.get_node(gconn, loser)
    already_dep = bool(loser_node) and loser_node["status"] == "deprecated_candidate"
    if already_edge and already_dep:
        return False
    # supersedes is a same-node-type edge (claim -> claim); both endpoints are indexed.
    graph.upsert_assertion(gconn, src_id=winner, dst_id=loser, edge_type="supersedes",
                           asserted_by="human", status="active", review_id=source_review_id, now=now)
    # Deprecate the loser (page is the status authority); its evidence + contradiction backlink
    # stay rendered, status flips to deprecated_candidate.
    claims.recompose_claim(gconn, cid=loser, claims_dir=claims_dir, reviews_dir=reviews_dir,
                           markdown_dir=markdown_dir, now=now, deprecate=True)
    # Audit trail: the deprecation is authorized by the approved contradiction resolution, so we
    # file a deprecate_wiki_page item and immediately resolve it approved (records the cause).
    dep_rid = reviews.create_review_item(
        reviews_dir, review_type="deprecate_wiki_page",
        subject={"node_id": loser, "page": f"Claims/{loser}.md"},
        proposal={"to_status": "deprecated_candidate",
                  "reason": "superseded via approved contradiction resolution"},
        # node_type lets the Phase-6 deprecation executor recognise this as a claim deprecation
        # (already effected here) without inferring it from the page dir (ADR-0035 A5).
        context={"resolved_by_review": source_review_id, "winner": winner, "node_type": "claim"},
        now=now)
    reviews.resolve_review_item(
        reviews_dir, dep_rid, decision="approved", decided_by="contradiction_resolution",
        note=f"deprecated as loser of approved contradiction {source_review_id}; winner {winner}",
        now=now)
    return True


# --- maintenance (ADR-0069 slice 2(a), model-free) -------------------------

# Why a proposal is retired, in the words a screen, lint and the CLI show.
RETIRE_REASONS = {
    "evidence_gone": "a claim no longer has evidence",
    "not_independent": "its documents are no longer independent",
    "evidence_changed": "a claim's wording or evidence changed since it was judged",
    "retirement_unfinished": "an interrupted retirement is completed",
}
# Why a proposal can't be checked (reported, never retired).
UNVERIFIABLE_REASONS = {
    "no_pending_review": "no pending review item records what was judged",
    "review_unreadable": "its pending review item is unreadable",
    "no_judged_evidence": "its review item doesn't record the evidence that was judged",
    "judged_evidence_incomplete": "its review item doesn't record the judged evidence of both claims",
}
MAINTENANCE_COMMAND = "uv run python scripts/detect_contradictions.py maintain"
NOTICE_NAME = "Contradiction maintenance"
SCREEN_NOTICE = ("Some proposed contradictions involving this document no longer match their claims' "
                 "evidence or their documents' independence. Run `" + MAINTENANCE_COMMAND + "` to "
                 "retire them.")


def evidence_fingerprint(context: dict[str, Any]) -> str:
    """What a verdict judged about one claim: its wording and each citation's document and quote.
    Character offsets are left out — the same quote at a shifted offset is the same evidence."""
    cites = sorted((str(c.get("source_id") or ""), str(c.get("quote") or ""))
                   for c in context.get("citations") or [] if isinstance(c, dict))
    data = json.dumps({"claim_text": context.get("claim_text"), "citations": cites},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def judged_fingerprints(item: dict[str, Any]) -> dict[str, str] | None:
    """`{claim_id: evidence_fingerprint}` of what the verdict behind a review item judged: the
    `context.judged_evidence` a scoped run records (slice 2(b)), else derived from the `sides` an
    older proposal shows. None when the item records neither."""
    judged = (item.get("context") or {}).get("judged_evidence")
    if isinstance(judged, dict) and judged:
        return {str(k): str(v) for k, v in judged.items()}
    sides = (item.get("proposal") or {}).get("sides")
    if isinstance(sides, list) and sides and all(isinstance(x, dict) and x.get("claim_id")
                                                 for x in sides):
        return {str(x["claim_id"]): evidence_fingerprint(x) for x in sides}
    return None


def _review_state(reviews_dir: Path, rid: str) -> tuple[str | None, dict[str, Any] | None]:
    """`("decided", None)` once a human approved or rejected it (Apply executes it), `("pending",
    item)`, `("unreadable", None)`, or `(None, None)` when there is no item."""
    if any((reviews_dir / d / f"{rid}.json").exists() for d in ("approved", "rejected")):
        return "decided", None
    path = reviews_dir / "pending" / f"{rid}.json"
    if not path.exists():
        return None, None
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unreadable", None
    return ("pending", item) if isinstance(item, dict) else ("unreadable", None)


def check_assertions(gconn, *, reviews_dir: Path, claims_dir: Path, markdown_dir: Path,
                     manifests_dir: Path, sources: set[str] | None = None,
                     claims_scope: set[str] | None = None) -> dict[str, Any]:
    """Read-only: which `proposed` contradiction assertions maintenance would retire, and which
    retirements were interrupted.

    Scoped to the assertions touching `claims_scope` or a claim of `sources` when either is given
    (targeted maintenance), else every one. Order per proposal: a human decision awaiting Apply is
    left for it; a pending review already marked `retiring` is an interrupted retirement, to be
    completed; then the judged record must exist and cover both claims — otherwise the proposal is
    **unverifiable** (reported, never retired) whatever else changed; only then evidence loss,
    independence and changed evidence. A marked pending review whose edge is no longer proposed
    (interrupted after the edge was superseded) is an interrupted retirement too. Returns
    `{checked, stale: [...], unverifiable: [...], awaiting_apply}`; each entry is
    `{edge_id, claim_a, claim_b, review_id, reason}` plus `retiring` for an interrupted one."""
    reviews_dir, claims_dir, markdown_dir = Path(reviews_dir), Path(claims_dir), Path(markdown_dir)
    scoped = sources is not None or claims_scope is not None
    sources, claims_scope = set(sources or ()), set(claims_scope or ())

    def in_scope(a: str, b: str, srcs: list[str]) -> bool:
        return not scoped or bool({a, b} & claims_scope or set(srcs) & sources)

    prov = {m["source_id"]: get_provenance(m) for m in valid_manifests(manifests_dir)[0]}
    standing = graph.claims_with_active_evidence(gconn)
    out: dict[str, Any] = {"checked": 0, "stale": [], "unverifiable": [], "awaiting_apply": 0}
    seen: set[str] = set()
    for row in graph.contradiction_assertions(gconn, statuses=("proposed",)):
        a, b = row["src_id"], row["dst_id"]
        srcs_a, srcs_b = graph.sources_for_claim(gconn, a), graph.sources_for_claim(gconn, b)
        if not in_scope(a, b, srcs_a + srcs_b):
            continue
        out["checked"] += 1
        rid = reviews.review_id("resolve_contradiction", {"claim_a": a, "claim_b": b})
        seen.add(rid)
        entry = {"edge_id": row["edge_id"], "claim_a": a, "claim_b": b, "review_id": rid}
        state, item = _review_state(reviews_dir, rid)
        if state == "decided":
            out["awaiting_apply"] += 1                    # a human decision: Apply executes it
            continue
        if state == "pending" and _retiring(item):
            out["stale"].append(entry | {"reason": "retirement_unfinished",
                                         "retiring": _retiring(item)})
            continue
        if state is None or state == "unreadable":
            reason = "no_pending_review" if state is None else "review_unreadable"
            out["unverifiable"].append(entry | {"reason": reason})
            continue
        judged = judged_fingerprints(item or {})
        if judged is None or not {a, b} <= set(judged):
            reason = "no_judged_evidence" if judged is None else "judged_evidence_incomplete"
            out["unverifiable"].append(entry | {"reason": reason})
            continue
        if a not in standing or b not in standing:
            out["stale"].append(entry | {"reason": "evidence_gone"})
            continue
        if not any(sa != sb and independent_sources(prov.get(sa, {}), prov.get(sb, {}))
                   for sa in srcs_a for sb in srcs_b):
            out["stale"].append(entry | {"reason": "not_independent"})
            continue
        for cid in (a, b):
            ctx = _claim_context(gconn, cid, claims_dir=claims_dir, markdown_dir=markdown_dir)
            if ctx is None or judged[cid] != evidence_fingerprint(ctx):
                out["stale"].append(entry | {"reason": "evidence_changed"})
                break
    # Interrupted after the edge was superseded: the marked review is all that is left.
    for item in _marked_pending(reviews_dir):
        rid = str(item.get("review_id") or "")
        subj = item.get("subject") or {}
        a, b = str(subj.get("claim_a") or ""), str(subj.get("claim_b") or "")
        if rid in seen or not in_scope(a, b, graph.sources_for_claim(gconn, a)
                                       + graph.sources_for_claim(gconn, b)):
            continue
        out["checked"] += 1
        out["stale"].append({"edge_id": None, "claim_a": a, "claim_b": b, "review_id": rid,
                             "reason": "retirement_unfinished", "retiring": _retiring(item)})
    return out


def _retiring(item: dict[str, Any] | None) -> dict[str, Any] | None:
    """The retirement a pending review was marked with, if it is a well-formed one."""
    mark = (item or {}).get(reviews.RETIRING)
    if isinstance(mark, dict) and isinstance(mark.get("id"), str) and \
            mark.get("reason") in RETIRE_REASONS:
        return mark
    return None


def _marked_pending(reviews_dir: Path) -> list[dict[str, Any]]:
    out = []
    for path in sorted((reviews_dir / "pending").glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(item, dict) and item.get("type") == "resolve_contradiction" \
                and _retiring(item):
            out.append(item)
    return out


def _mark_retiring(reviews_dir: Path, rid: str, mark: dict[str, Any], *, retry: bool) -> None:
    """Step 0 of a retirement: record it on the pending review, durably (file and directory
    fsynced), before any effect — so a power loss can't keep the edge's supersession and lose it.
    On a retry the marker is already in place, but the run that wrote it may have failed flushing
    it, so it is flushed again (file and directory) before anything else."""
    path = reviews_dir / "pending" / f"{rid}.json"
    if not path.exists():
        return
    if retry:
        reviews.sync_durably(path)
        return
    item = json.loads(path.read_text(encoding="utf-8"))
    item[reviews.RETIRING] = mark
    reviews.write_json_durably(path, item)


def maintain_assertions(gconn, *, reviews_dir: Path, claims_dir: Path, markdown_dir: Path,
                        manifests_dir: Path, sources: set[str] | None = None,
                        claims_scope: set[str] | None = None,
                        now: str | None = None) -> dict[str, Any]:
    """Retire what :func:`check_assertions` finds stale (the caller holds the write lock).

    A retirement is three effects, made recoverable by a marker written first: (0) the pending
    review is marked `retiring` with a fresh id and the reason; (1) the edge is superseded; (2) the
    review is withdrawn — its audit entry written under the marker's id, then the file removed. An
    interruption anywhere leaves the marked review behind, which the next check reports as
    `retirement_unfinished` and this completes with the **original** reason, keeping an audit entry
    already written (the same id). A retry flushes the marker and any existing audit entry again
    (file and directory) before the effect each guards, since the interrupted run's flush may be
    what failed. A re-proposal files a fresh, unmarked review. Returns the
    check's report with `retired` (the entries retired) in place of `stale`."""
    now = now or iso_now()
    reviews_dir = Path(reviews_dir)
    found = check_assertions(gconn, reviews_dir=reviews_dir, claims_dir=claims_dir,
                             markdown_dir=markdown_dir, manifests_dir=manifests_dir,
                             sources=sources, claims_scope=claims_scope)
    retired = []
    for entry in found.pop("stale"):
        mark = entry.get("retiring") or {"id": uuid.uuid4().hex[:12], "reason": entry["reason"],
                                         "at": now}
        _mark_retiring(reviews_dir, entry["review_id"], mark, retry=bool(entry.get("retiring")))
        for row in graph.contradiction_between(gconn, entry["claim_a"], entry["claim_b"]):
            if row["status"] == "proposed":
                graph.set_status(gconn, row["edge_id"], "superseded", now=now)
        reviews.withdraw_review_item(
            reviews_dir, entry["review_id"], audit_key=f"m{mark['id']}",
            reason=f"contradiction maintenance: {RETIRE_REASONS[mark['reason']]}", now=now)
        retired.append(entry | {"original_reason": mark["reason"]})
    return found | {"retired": retired}


def targeted_maintenance(gconn, *, reviews_dir: Path, claims_dir: Path, markdown_dir: Path,
                         manifests_dir: Path, sources: set[str] | None = None,
                         claims_scope: set[str] | None = None,
                         now: str | None = None) -> dict[str, Any]:
    """Maintenance after a Details save/clear or a claim re-extraction, inside that commit. Never
    raises: a failure is returned as `error` (the caller reports it; lint and the Source screen keep
    reporting the stale proposals until `maintain` retires them)."""
    try:
        return maintain_assertions(gconn, reviews_dir=reviews_dir, claims_dir=claims_dir,
                                   markdown_dir=markdown_dir, manifests_dir=manifests_dir,
                                   sources=sources, claims_scope=claims_scope, now=now) | {
                                       "error": None}
    except Exception as exc:                              # reported, never fatal to the save
        return {"checked": 0, "retired": [], "unverifiable": [], "awaiting_apply": 0,
                "error": f"{type(exc).__name__}: {exc}"}


def maintenance_needed(gconn, *, reviews_dir: Path, claims_dir: Path, markdown_dir: Path,
                       manifests_dir: Path, source_id: str) -> int:
    """How many proposed contradictions involving `source_id` maintenance would retire (the Source
    screen's notice)."""
    return len(check_assertions(gconn, reviews_dir=reviews_dir, claims_dir=claims_dir,
                                markdown_dir=markdown_dir, manifests_dir=manifests_dir,
                                sources={source_id})["stale"])


def notice_needed(root: Path, source_id: str, *, manifests_dir: Path | None = None) -> bool:
    """Whether the Source screen shows :data:`SCREEN_NOTICE`: maintenance would retire a proposed
    contradiction of this document (a targeted run failed or was skipped). False without a usable
    graph — the screen reports that on its own."""
    root = Path(root)
    graph_db = root / "db" / "graph.sqlite"
    if not graph_db.exists():
        return False
    gconn = graph.connect(graph_db)
    try:
        if graph.schema_version(gconn) != graph.SCHEMA_VERSION:
            return False
        return maintenance_needed(
            gconn, reviews_dir=root / "reviews", claims_dir=root / "wiki" / "Claims",
            markdown_dir=root / "normalized" / "markdown",
            manifests_dir=Path(manifests_dir) if manifests_dir else root / "raw" / "manifests",
            source_id=source_id) > 0
    finally:
        gconn.close()


def maintain(root: Path, *, manifests_dir: Path | None = None, jobs_db: Path | None = None,
             graph_db: Path | None = None, markdown_dir: Path | None = None,
             wiki_dir: Path | None = None, reviews_dir: Path | None = None,
             record_job: bool = True) -> dict[str, Any]:
    """Full contradiction maintenance over every proposed assertion: one commit phase under the
    write lock, recorded as a `detect_contradictions` job (`mode: maintain`). No model call, no
    judging, no Apply (Apply alone executes human decisions), and no page change."""
    root = Path(root).resolve()
    manifests_dir = Path(manifests_dir) if manifests_dir else root / "raw" / "manifests"
    jobs_db = Path(jobs_db) if jobs_db else root / "db" / "jobs.sqlite"
    graph_db = Path(graph_db) if graph_db else root / "db" / "graph.sqlite"
    markdown_dir = Path(markdown_dir) if markdown_dir else root / "normalized" / "markdown"
    claims_dir = (Path(wiki_dir) if wiki_dir else root / "wiki") / "Claims"
    reviews_dir = Path(reviews_dir) if reviews_dir else root / "reviews"
    now = iso_now()
    job_id = f"job_{uuid.uuid4().hex[:16]}"
    conn = None
    if record_job:
        db.init_db(jobs_db)
        conn = db.connect(jobs_db)
        db.insert_job(conn, job_id=job_id, job_type="detect_contradictions", status="running",
                      created_at=now, started_at=now)
    try:
        graph.init_db(graph_db)
        gconn = graph.connect(graph_db)
        try:
            with write_lock.hold(reviews_dir):
                report = maintain_assertions(
                    gconn, reviews_dir=reviews_dir, claims_dir=claims_dir,
                    markdown_dir=markdown_dir, manifests_dir=manifests_dir, now=now)
        finally:
            gconn.close()
        summary = {"job_id": job_id, "mode": "maintain", "status": "succeeded",
                   "assertions_checked": report["checked"], "retired": report["retired"],
                   "unverifiable": report["unverifiable"],
                   "awaiting_apply": report["awaiting_apply"], "maintained_at": now}
        if conn is not None:
            db.update_job(conn, job_id, status="succeeded", finished_at=iso_now(), metadata=summary)
        return summary
    except Exception as exc:
        if conn is not None:
            db.update_job(conn, job_id, status="failed", finished_at=iso_now(),
                          error_message=str(exc))
        raise
    finally:
        if conn is not None:
            conn.close()
