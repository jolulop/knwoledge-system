#!/usr/bin/env python3
"""Phase 7 slice 7-2: stale/retention producer + the reversible `archive_source` executor (ADR-0036).

Two halves, both deterministic and key-free:

- **`run_stale_check`** — a detect-and-propose pass: a `status == active` source whose **content age**
  (`modified_at`, fallback `discovered_at`) reaches `older_than_years_archive_candidate` gets one
  **`archive_source`** review item proposing `active → archive_candidate`; an `ephemeral` source whose
  **time-in-system** (`discovered_at`) exceeds `delete_candidate_after_days` gets a **`delete_raw_file`**
  candidate (record-only forever). It changes no status itself.

- **`apply_archive_sources`** — the executor `/reviews/apply` runs over approved `archive_source` items.
  It flips **`active → archive_candidate`** on the **manifest** (the durable source lifecycle authority,
  ADR-0036 decision 13), re-renders the Source page (a pure projection of the manifest), and mirrors the
  graph source node. **Reversible, idempotent, raw bytes untouched.** `archive_candidate` is the honest v1
  terminal status — excluded from default retrieval (`search.RETENTION_DEFAULT_STATUSES`) but found via an
  explicit status filter that includes `archive_candidate`, and never physically moved.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.backend import db, graph, keyword_index, manifests, write_lock
from app.backend.manifests import iso_now, load_manifest, valid_manifests
from app.backend.policy import load_yaml
from app.llm import cache as llm_cache
from app.workers import apply_outcomes, reviews, wiki
from app.workers.wiki_render import parse_frontmatter

_DAYS_PER_YEAR = 365


def _age_days(date_str: str | None, now: datetime) -> int | None:
    """Whole days between an ISO timestamp and `now`, or None if unparseable/absent."""
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None or now.tzinfo is None:  # compare only tz-aware vs tz-aware
        return None
    return (now - dt).days


def _file_tracked(reviews_dir: Path, *, review_type: str, subject: dict[str, Any],
                  proposal: dict[str, Any], priority: str, now: str,
                  filed: list[str], existing: list[str]) -> None:
    """Create a review item, recording newly *filed* vs already *existing* (idempotent)."""
    rid = reviews.review_id(review_type, subject)
    if any((reviews_dir / s / f"{rid}.json").exists() for s in ("pending", "approved", "rejected")):
        existing.append(rid)
        return
    reviews.create_review_item(reviews_dir, review_type=review_type, subject=subject,
                               proposal=proposal, context={}, priority=priority, now=now)
    filed.append(rid)


def _append_log(wiki_dir: Path, message: str) -> None:
    log = Path(wiki_dir) / "log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"- {message}\n")


def run_stale_check(
    root: Path,
    *,
    manifests_dir: Path | None = None,
    reviews_dir: Path | None = None,
    wiki_dir: Path | None = None,
    cache_db: Path | None = None,
    policy_path: Path | None = None,
    jobs_db: Path | None = None,
    record_job: bool = True,
    file_review_items: bool = True,
    now: str | None = None,
) -> dict[str, Any]:
    """Detect stale/ephemeral sources + LLM-cache purge candidates; propose, never act (ADR-0036).

    Always *detects* + counts candidates (source archive/delete and cache purge); `file_review_items=False`
    only suppresses the filing. Reports **live** cache stats every run (even when the purge item already
    exists). Records a job and appends `wiki/log.md` on every run (maintenance passes are auditable).
    """
    root = Path(root).resolve()
    manifests_dir = Path(manifests_dir) if manifests_dir else root / "raw" / "manifests"
    reviews_dir = Path(reviews_dir) if reviews_dir else root / "reviews"
    wiki_dir = Path(wiki_dir) if wiki_dir else root / "wiki"
    cache_db = Path(cache_db) if cache_db else root / "db" / "llm_cache.sqlite"
    policy_path = Path(policy_path) if policy_path else root / "policies" / "retention.yaml"
    jobs_db = Path(jobs_db) if jobs_db else root / "db" / "jobs.sqlite"
    now = now or iso_now()
    now_dt = datetime.fromisoformat(now)
    job_id = f"job_{uuid.uuid4().hex[:16]}"

    policy = load_yaml(policy_path.read_text(encoding="utf-8")) if policy_path.exists() else {}
    raw_pol = policy.get("raw_files") or {}
    eph_pol = policy.get("ephemeral") or {}
    cache_pol = policy.get("response_cache") or {}
    archive_after_days = int(raw_pol.get("older_than_years_archive_candidate", 3)) * _DAYS_PER_YEAR
    ephemeral_enabled = bool(eph_pol.get("enabled", False))
    ephemeral_after_days = int(eph_pol.get("delete_candidate_after_days", 90))
    cache_enabled = bool(cache_pol.get("enabled", True))
    cache_ttl_days = int(cache_pol.get("cache_ttl_days", 365))
    cache_cap_mb = int(cache_pol.get("cache_max_mb", 2048))

    conn = None
    if record_job:
        db.init_db(jobs_db)
        conn = db.connect(jobs_db)
        db.insert_job(conn, job_id=job_id, job_type="stale_check", status="running",
                      created_at=now, started_at=now)
    try:
        archive_filed: list[str] = []
        archive_existing: list[str] = []
        delete_filed: list[str] = []
        delete_existing: list[str] = []
        archive_detected = delete_detected = considered = 0

        # ADR-0068: each source's status check and its filing are one commit phase (an archive
        # applied meanwhile is seen before anything is filed).
        with write_lock.hold(reviews_dir):
            valid, manifests_skipped_invalid = valid_manifests(manifests_dir)
            for m in valid:
                sid = m.get("source_id")
                if not sid:
                    continue
                considered += 1
                # archive candidate: active + content age (modified_at, fallback discovered_at) past threshold
                if manifests.get_status(m) == "active":
                    ref = m.get("modified_at") or m.get("discovered_at")
                    age = _age_days(ref, now_dt)
                    if age is not None and age >= archive_after_days:
                        archive_detected += 1                       # detection is unconditional
                        if file_review_items:
                            _file_tracked(
                                reviews_dir, review_type="archive_source", subject={"source_id": sid},
                                proposal={"to_status": "archive_candidate",
                                          "reason": f"content age {age}d ≥ {archive_after_days}d threshold",
                                          "age_days": age, "reference_date": ref},
                                priority="medium", now=now, filed=archive_filed, existing=archive_existing)
                # ephemeral delete candidate (record-only forever): time-in-system past the window
                if ephemeral_enabled and m.get("retention_class") == "ephemeral":
                    age = _age_days(m.get("discovered_at"), now_dt)
                    if age is not None and age > ephemeral_after_days:
                        delete_detected += 1
                        if file_review_items:
                            _file_tracked(
                                reviews_dir, review_type="delete_raw_file", subject={"source_id": sid},
                                proposal={"reason": f"ephemeral, in system {age}d > {ephemeral_after_days}d",
                                          "age_days": age, "record_only": True},
                                priority="medium", now=now, filed=delete_filed, existing=delete_existing)

        # LLM-cache retention (only when policy-enabled): live stats every run; one aggregate record-only
        # purge candidate when over bounds. Never mutates the cache; payload carries only counts/sizes/ages.
        warnings: list[str] = []
        cache_purge_filed: list[str] = []
        cache_purge_existing: list[str] = []
        if not cache_enabled:
            cache: dict[str, Any] = {"enabled": False}
        else:
            cache = llm_cache.cache_retention_report(
                cache_db, ttl_days=cache_ttl_days, cap_mb=cache_cap_mb, now=now_dt)
        if cache.get("cache_present") and cache.get("cache_readable") is False:
            warnings.append("cache_unreadable")  # degraded report, never aborts source retention
        elif cache.get("over_bounds"):
            cache["purge_candidate"] = True
            if file_review_items:
                _file_tracked(
                    reviews_dir, review_type="purge_response_cache", subject={"scope": "response_cache"},
                    proposal={"reason": "LLM response cache exceeds retention bounds",
                              "entries": cache.get("entries"),
                              "entries_over_ttl": cache.get("entries_over_ttl"),
                              "total_mb": cache.get("total_mb"), "cap_mb": cache.get("cap_mb"),
                              "oldest_age_days": cache.get("oldest_age_days"),
                              "ttl_days": cache_ttl_days, "record_only": True},
                    priority="low", now=now, filed=cache_purge_filed, existing=cache_purge_existing)

        summary = {"considered": considered,
                   "manifests_skipped_invalid": len(manifests_skipped_invalid),
                   "archive_candidates": archive_detected,
                   "archive_candidates_filed": len(archive_filed),
                   "archive_candidates_existing": len(archive_existing),
                   "delete_candidates": delete_detected,
                   "delete_candidates_filed": len(delete_filed),
                   "delete_candidates_existing": len(delete_existing),
                   "cache": cache,
                   "cache_purge_filed": len(cache_purge_filed),
                   "cache_purge_existing": len(cache_purge_existing),
                   "warnings": warnings}
        _append_log(wiki_dir, f"stale-check: {archive_detected} archive / {delete_detected} delete / "
                              f"{1 if cache.get('over_bounds') else 0} cache purge candidate(s); "
                              f"{len(archive_filed) + len(delete_filed) + len(cache_purge_filed)} filed "
                              f"[{job_id}]")
        if conn is not None:
            db.update_job(conn, job_id, status="succeeded", finished_at=iso_now(),
                          metadata=summary, warnings=warnings)
        return {"job_id": job_id, **summary,
                "archive_review_items_filed": sorted(set(archive_filed)),
                "delete_review_items_filed": sorted(set(delete_filed)),
                "cache_purge_review_items_filed": sorted(set(cache_purge_filed))}
    except BaseException as exc:  # incl. Ctrl-C (UAT part 3 #3)
        if conn is not None:
            db.update_job(conn, job_id, status="failed", finished_at=iso_now(), error_message=db.failure_message(exc))
        raise
    finally:
        if conn is not None:
            conn.close()


def _approved_items_of_type(reviews_dir: Path, review_type: str) -> list[dict[str, Any]]:
    import json
    out: list[dict[str, Any]] = []
    d = reviews_dir / "approved"
    for path in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(item, dict) and item.get("type") == review_type:
            out.append(item)
    return out


def _source_page_drift(wiki_dir: Path, sid: str, manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Read-only postcondition check (ADR-0072 decision 2): the Source page mirrors the manifest's
    lifecycle status — the `validate_wiki.py` page-status check (default `active` when unset). An
    extracted document must have its page; a not-yet-extracted one has no page to mirror (its
    extraction publishes it), so only a page that exists is compared. None when consistent, else the
    structured problem."""
    expected = manifest.get("status") or "active"
    page = Path(wiki_dir) / "Sources" / f"{sid}.md"
    try:
        text = page.read_text(encoding="utf-8")
    except OSError:
        if manifest.get("ingestion_status") not in wiki._GENERATED_STATUSES:
            return None
        return {"page": f"Sources/{sid}.md", "page_status": None, "manifest_status": expected}
    page_status = parse_frontmatter(text).get("status")
    if page_status != expected:
        return {"page": f"Sources/{sid}.md", "page_status": page_status, "manifest_status": expected}
    return None


def _render_error(gen: Any, sid: str) -> dict[str, str] | None:
    """The page error `generate_wiki` reported for `sid` (its `error_details`), if any."""
    errors = gen.get("error_details") if isinstance(gen, dict) else None
    for err in errors or []:
        if isinstance(err, dict) and err.get("source_id") == sid:
            return {k: str(v) for k, v in err.items() if k in ("source_id", "reason", "error")}
    return None


def _apply_source_status_transition(
    root: Path,
    *,
    review_type: str,
    to_status: str,
    manifests_dir: Path,
    reviews_dir: Path,
    wiki_dir: Path,
    graph_db: Path,
    from_status: str = "active",
    not_in_from_reason: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Shared `<from_status> → <to_status>` source-lifecycle executor for the reversible status governance
    types (`archive_source` active→archive_candidate, ADR-0036; `hide_content` active→hidden, ADR-0043;
    `unhide_content` hidden→active, ADR-0047 — the inverse, via `from_status="hidden"`).

    Sets the status on the **manifest** (the authority), re-renders the Source page (a projection), and
    mirrors the graph source node (best-effort). Reversible, idempotent (only a source currently in
    `from_status` transitions), and **never touches raw bytes**. No reindex here — the caller
    (`/reviews/apply`) owns the single reindex. Returns
    `{applied, skipped:[{review_id, reason}], changed_pages, graph_changed, outcomes}`.

    `outcomes` (ADR-0072 decision 2) holds exactly one record per approved review of `review_type`
    read on this run, named with the document title read before any change (decision 3):
    `applied` (manifest flipped and the page now mirrors it); `already_in_effect` (manifest already
    at the target AND the page mirror verified); `skipped` (nothing changed: every `skipped` entry,
    plus two cases that stay silent in `skipped` — a third state without a typed code,
    `source_not_<from_status>`, and an already-transitioned manifest whose page drifted,
    `page_manifest_drift`); `incomplete` (`source_page_refresh_failed`: the manifest flipped but the
    page did not re-render to match — still counted in `applied`/`changed_pages`, as before; its
    details carry `source_id` for Apply's publication retry). Outcomes state the VERIFIED end state:
    `after` is the manifest status read back after the write — a read-back other than the target is
    `incomplete` `postcondition_not_met` (`hidden_takes_precedence` when it reads `hidden`), and a
    reporting read that fails is `verification_unavailable` (`incomplete` after a write, `skipped` on
    a no-op — never `already_in_effect`). Reporting reads never raise and never change the result.
    """
    now = now or iso_now()
    applied = 0
    graph_writes = 0  # actual graph-mirror writes (honest `graph_changed`; 0 when graph absent/missing)
    skipped: list[dict[str, str]] = []
    outcomes: list[dict[str, Any]] = []   # ADR-0072 decision 2: one per approved review handled
    changed_pages: list[str] = []
    status_changed: list[str] = []
    # Schema-safe graph open: only mirror onto a present, matching-schema graph (a wrong-schema graph
    # is left untouched — the mirror is best-effort; the manifest + page are the load-bearing effect).
    gconn = None
    if Path(graph_db).exists():
        candidate = graph.connect(graph_db)
        if graph.schema_version(candidate) == graph.SCHEMA_VERSION:
            gconn = candidate
        else:
            candidate.close()
    try:
        for item in _approved_items_of_type(reviews_dir, review_type):
            rid = str(item.get("review_id", ""))
            # ADR-0072 decision 3: the document's display name, read BEFORE any change (the id when the
            # page/title can't be read; an untrusted, non-canonical id is never used in a path).
            _subj = item.get("subject")
            _sid = _subj.get("source_id") if isinstance(_subj, dict) else None
            subject = (apply_outcomes.page_name(wiki_dir, f"Sources/{_sid}", _sid)
                       if manifests.is_source_id(_sid) else apply_outcomes.named(_sid, None))

            def details_of(*, before: Any = None, after: Any = None, problem: Any = None,
                           **extra: Any) -> dict[str, Any]:
                details: dict[str, Any] = {"subject": subject, **extra}
                if before is not None:
                    details["before"] = {"status": before}
                if after is not None:
                    details["after"] = {"status": after}
                if problem is not None:
                    details["problem"] = problem
                return details

            def report(kind: str, *, reason: str | None = None, **kw: Any) -> None:
                outcomes.append(apply_outcomes.outcome(rid, review_type, kind, reason=reason,
                                                       **details_of(**kw)))

            def report_unverified(*, changed: bool, reason: str = "", **kw: Any) -> None:
                # the honest outcome when the end state couldn't be confirmed (B2a review round 1)
                outcomes.append(apply_outcomes.unverified(rid, review_type, changed=changed,
                                                          reason=reason, **details_of(**kw)))

            def skip(reason: str, **kw: Any) -> None:
                skipped.append({"review_id": rid, "reason": reason})
                report(apply_outcomes.SKIPPED, reason=reason, **kw)

            # Scope guard: only act on a well-formed, approved item. A corrupt/unexpected approved file
            # is skipped, never mutates lifecycle state.
            if item.get("status") != "approved":
                skip("not_approved")
                continue
            if (item.get("proposal") or {}).get("to_status") != to_status:
                skip("unexpected_to_status",
                     problem={"to_status": (item.get("proposal") or {}).get("to_status"),
                              "expected": to_status})
                continue
            sid = (item.get("subject") or {}).get("source_id")
            if not sid:
                skip("missing_subject")
                continue
            if not manifests.is_source_id(sid):  # untrusted review subject — reject before any path use
                skip("invalid_source_id")
                continue
            m = load_manifest(manifests_dir, sid)
            if m is None:
                skip("source_missing")
                continue
            current = manifests.get_status(m)
            if current == to_status:
                # Idempotent no-op: already at the target lifecycle state (silent in `skipped`, not a
                # skip). Reported `already_in_effect` only once the page mirror is verified too
                # (ADR-0072 decision 2); a drifted/missing page is left as it is (behaviour unchanged)
                # and reported as a skipped outcome — not added to `skipped`, whose counts stay as-is.
                # A failed read never justifies `already_in_effect` (nothing written: `skipped`).
                drift = apply_outcomes.guarded(_source_page_drift, wiki_dir, sid, m)
                if drift is apply_outcomes.UNAVAILABLE:
                    report_unverified(changed=False, before=current, after=current)
                elif drift is None:
                    report(apply_outcomes.ALREADY_IN_EFFECT, before=current, after=current)
                else:
                    report(apply_outcomes.SKIPPED, reason="page_manifest_drift", before=current,
                           after=current, problem=drift)
                continue
            if current != from_status:
                # A third state (neither from nor target): the transition can't apply. Emit a typed skip
                # when the caller names one (ADR-0047: unhide of a non-hidden source -> source_not_hidden),
                # so the guard failure is operator-visible in the apply summary + dry-run, not silent.
                if not_in_from_reason is not None:
                    skip(not_in_from_reason, before=current, after=current,
                         problem={"status": current, "expected": from_status})
                else:
                    # Silent in `skipped` (unchanged); the outcome still names why nothing happened, with
                    # the dry-run's code (review_read: `source_not_active`).
                    report(apply_outcomes.SKIPPED, reason=f"source_not_{from_status}", before=current,
                           after=current, problem={"status": current, "expected": from_status})
                continue
            manifests.set_status(manifests_dir, sid, to_status)
            # B2a review round 2: EXECUTION data, independent of how the outcome is classified — every
            # document whose manifest status this run wrote. Apply's publication re-renders each one,
            # so a failed page refresh is never lost to a reporting failure.
            status_changed.append(sid)
            # Re-render the one Source page from the (now-updated) manifest; pure projection.
            # (generate_wiki opens its own read connection — keep our write conn idle/committed here.)
            gen = wiki.generate_wiki(root, source_ids=[sid], rebuild_index=False, record_job=False)
            if gconn is not None and graph.get_node(gconn, sid) is not None:
                graph.upsert_node(gconn, node_id=sid, node_type="source", slug=sid,
                                  status=to_status, now=now)
                gconn.commit()
                graph_writes += 1
            applied += 1
            changed_pages.append(f"Sources/{sid}.md")
            # The manifest (authority) changed; the review finished only if the VERIFIED end state is
            # the target (B2a review round 1): the manifest read back at `to_status`, then the page
            # mirroring it. Every read here is reporting-only (`guarded`): a failure never changes what
            # was executed, and is `incomplete` (this run wrote) with `verification_unavailable`.
            after_m = apply_outcomes.guarded(load_manifest, manifests_dir, sid)
            after = (apply_outcomes.guarded(manifests.get_status, after_m)
                     if isinstance(after_m, dict) else apply_outcomes.UNAVAILABLE)
            if not isinstance(after, str):           # unreadable (load_manifest folds errors into None)
                report_unverified(changed=True, before=current, source_id=sid)
                continue
            if after != to_status:
                report(apply_outcomes.INCOMPLETE,
                       # hidden wins only over a non-visibility change (archive); an unhide that
                       # reads back hidden is simply not in effect
                       reason=(apply_outcomes.HIDDEN_TAKES_PRECEDENCE
                               if after == "hidden" and review_type == "archive_source"
                               else apply_outcomes.POSTCONDITION_NOT_MET),
                       before=current, after=after, source_id=sid,
                       problem={"manifest_status": after, "expected": to_status})
                continue
            # generate_wiki keeps a page failure in its result (the page keeps its old status): that is
            # `incomplete`, not applied; Apply's publication step retries the page named by
            # `details.source_id`.
            problem = apply_outcomes.guarded(_render_error, gen, sid)
            if problem is None:
                problem = apply_outcomes.guarded(_source_page_drift, wiki_dir, sid, after_m)
            if problem is apply_outcomes.UNAVAILABLE:
                report_unverified(changed=True, before=current, after=after, source_id=sid)
            elif problem is None:
                report(apply_outcomes.APPLIED, before=current, after=after)
            else:
                report(apply_outcomes.INCOMPLETE, reason="source_page_refresh_failed",
                       before=current, after=after, source_id=sid, problem=problem)
    finally:
        if gconn is not None:
            gconn.close()
    # `graph_changed` is honest: True only when a graph mirror was actually written (not merely that an
    # apply happened). Graph absent / schema-mismatched / node missing -> applied > 0, graph_changed False.
    return {"applied": applied, "skipped": skipped, "changed_pages": changed_pages,
            "graph_changed": graph_writes > 0, "outcomes": outcomes,
            "status_changed_sources": status_changed}


def apply_archive_sources(
    root: Path, *, manifests_dir: Path, reviews_dir: Path, wiki_dir: Path, graph_db: Path,
    now: str | None = None,
) -> dict[str, Any]:
    """Apply approved `archive_source` decisions: flip active → archive_candidate (ADR-0036 decision 13).
    Thin wrapper over the shared source-status executor."""
    return _apply_source_status_transition(
        root, review_type="archive_source", to_status="archive_candidate",
        manifests_dir=manifests_dir, reviews_dir=reviews_dir, wiki_dir=wiki_dir, graph_db=graph_db,
        now=now)


def apply_hidden_sources(
    root: Path, *, manifests_dir: Path, reviews_dir: Path, wiki_dir: Path, graph_db: Path,
    now: str | None = None,
) -> dict[str, Any]:
    """Apply approved `hide_content` decisions for sources: flip active → hidden (ADR-0043).
    Governance suppression (distinct from retention archive); thin wrapper over the shared executor."""
    return _apply_source_status_transition(
        root, review_type="hide_content", to_status="hidden",
        manifests_dir=manifests_dir, reviews_dir=reviews_dir, wiki_dir=wiki_dir, graph_db=graph_db,
        now=now)


def apply_unhidden_sources(
    root: Path, *, manifests_dir: Path, reviews_dir: Path, wiki_dir: Path, graph_db: Path,
    now: str | None = None,
) -> dict[str, Any]:
    """Apply approved `unhide_content` decisions for sources: flip hidden → active (ADR-0047) — the
    governed inverse of `hide_content`. Only a currently-hidden source transitions; an already-`active`
    source is a silent idempotent no-op, and any other non-hidden state (e.g. `archive_candidate`) emits a
    typed `source_not_hidden` skip. Thin wrapper over the shared source-status executor; NOT
    graph-required."""
    return _apply_source_status_transition(
        root, review_type="unhide_content", from_status="hidden", to_status="active",
        not_in_from_reason="source_not_hidden",
        manifests_dir=manifests_dir, reviews_dir=reviews_dir, wiki_dir=wiki_dir, graph_db=graph_db,
        now=now)


def reindex_keyword(root: Path) -> bool:
    """Refresh the keyword/navigation index so archived source status reaches the retrieval filter."""
    keyword_index.reindex(Path(root))
    return True


def run_reindex(
    root: Path,
    *,
    jobs_db: Path | None = None,
    wiki_dir: Path | None = None,
    record_job: bool = True,
    now: str | None = None,
) -> dict[str, Any]:
    """Cheap deterministic reindex pass: rebuild `wiki/index.md` + refresh the keyword index (ADR-0036).

    **Index + keyword only — never the vector index** (that stays the explicit `reindex_vector.py`,
    ADR-0033, so maintenance triggers no embedding-server side effect). Job-recorded, appends `wiki/log.md`.
    ADR-0068: it is a publication (`app/workers/publication.py`), so it also retries whatever is
    pending. A genuine sub-step failure (script present and non-zero, or a keyword reindex error)
    stays queued and yields `status: "saved_publication_pending"` with a warning — not a silent
    success.
    """
    root = Path(root).resolve()
    jobs_db = Path(jobs_db) if jobs_db else root / "db" / "jobs.sqlite"
    wiki_dir = Path(wiki_dir) if wiki_dir else root / "wiki"
    now = now or iso_now()
    job_id = f"job_{uuid.uuid4().hex[:16]}"
    warnings: list[str] = []

    conn = None
    if record_job:
        db.init_db(jobs_db)
        conn = db.connect(jobs_db)
        db.insert_job(conn, job_id=job_id, job_type="reindex", status="running",
                      created_at=now, started_at=now)
    try:
        from app.workers import publication              # lazy: publication renders through wiki
        pub = publication.publish(root, index=True, keyword=True, origin="reindex",
                                  wiki_dir=wiki_dir, now=now)
        index_rebuilt, keyword_reindexed = pub["index_rebuilt"], pub["keyword_reindexed"]
        warnings = pub["warnings"]
        status = publication.job_status("succeeded", pub)
        _append_log(wiki_dir, f"reindex: {status} (index_rebuilt={index_rebuilt}, "
                              f"keyword={keyword_reindexed}) [{job_id}]")
        if conn is not None:
            db.update_job(conn, job_id, status=status, finished_at=iso_now(), warnings=warnings,
                          metadata={"index_rebuilt": index_rebuilt,
                                    "keyword_reindexed": keyword_reindexed, "warnings": warnings})
        return {"job_id": job_id, "status": status, "index_rebuilt": index_rebuilt,
                "keyword_reindexed": keyword_reindexed, "warnings": warnings,
                "publication": publication.summary(pub)}
    except BaseException as exc:  # incl. Ctrl-C (UAT part 3 #3)
        if conn is not None:
            db.update_job(conn, job_id, status="failed", finished_at=iso_now(), error_message=db.failure_message(exc))
        raise
    finally:
        if conn is not None:
            conn.close()
