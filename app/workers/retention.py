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

import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.backend import db, graph, keyword_index, manifests
from app.backend.manifests import iso_now, list_manifests, load_manifest
from app.backend.policy import load_yaml
from app.llm import cache as llm_cache
from app.workers import reviews, wiki

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

        for m in list_manifests(manifests_dir):
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
    except Exception as exc:
        if conn is not None:
            db.update_job(conn, job_id, status="failed", finished_at=iso_now(), error_message=str(exc))
        raise
    finally:
        if conn is not None:
            conn.close()


def _approved_archive_items(reviews_dir: Path) -> list[dict[str, Any]]:
    import json
    out: list[dict[str, Any]] = []
    d = reviews_dir / "approved"
    for path in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(item, dict) and item.get("type") == "archive_source":
            out.append(item)
    return out


def apply_archive_sources(
    root: Path,
    *,
    manifests_dir: Path,
    reviews_dir: Path,
    wiki_dir: Path,
    graph_db: Path,
    now: str | None = None,
) -> dict[str, Any]:
    """Apply approved `archive_source` decisions: flip active → archive_candidate (ADR-0036 decision 13).

    Sets the status on the **manifest** (the authority), re-renders the Source page (a projection), and
    mirrors the graph source node. Reversible, idempotent (only an `active` source transitions), and
    **never touches raw bytes**. No reindex here — the caller (`/reviews/apply`) owns the single reindex.
    Returns `{applied, skipped:[{review_id, reason}], changed_pages, graph_changed}`.
    """
    now = now or iso_now()
    applied = 0
    skipped: list[dict[str, str]] = []
    changed_pages: list[str] = []
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
        for item in _approved_archive_items(reviews_dir):
            rid = str(item.get("review_id", ""))
            # Scope guard (ADR-0036): only act on a well-formed, approved archive_source item. A
            # corrupt/unexpected approved file is skipped, never mutates lifecycle state.
            if item.get("status") != "approved":
                skipped.append({"review_id": rid, "reason": "not_approved"})
                continue
            if (item.get("proposal") or {}).get("to_status") != "archive_candidate":
                skipped.append({"review_id": rid, "reason": "unexpected_to_status"})
                continue
            sid = (item.get("subject") or {}).get("source_id")
            if not sid:
                skipped.append({"review_id": rid, "reason": "missing_subject"})
                continue
            m = load_manifest(manifests_dir, sid)
            if m is None:
                skipped.append({"review_id": rid, "reason": "source_missing"})
                continue
            if manifests.get_status(m) != "active":
                continue  # idempotent no-op: already archived (or another lifecycle state)
            manifests.set_status(manifests_dir, sid, "archive_candidate")
            # Re-render the one Source page from the (now-updated) manifest; pure projection.
            # (generate_wiki opens its own read connection — keep our write conn idle/committed here.)
            wiki.generate_wiki(root, source_ids=[sid], rebuild_index=False, record_job=False)
            if gconn is not None and graph.get_node(gconn, sid) is not None:
                graph.upsert_node(gconn, node_id=sid, node_type="source", slug=sid,
                                  status="archive_candidate", now=now)
                gconn.commit()
            applied += 1
            changed_pages.append(f"Sources/{sid}.md")
    finally:
        if gconn is not None:
            gconn.close()
    return {"applied": applied, "skipped": skipped, "changed_pages": changed_pages,
            "graph_changed": bool(applied)}


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
    A genuine sub-step failure (script present and non-zero, or a keyword reindex error) yields
    `status: "failed"` with a warning — not a silent success.
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
        script = root / "scripts" / "rebuild_index.py"
        if script.exists():
            index_rebuilt = subprocess.run(
                [sys.executable, str(script), str(root)]).returncode == 0
            if not index_rebuilt:
                warnings.append("index_rebuild_failed")  # script present but non-zero
        else:
            index_rebuilt = False  # missing script (degraded/test env) is not a failure
        try:
            keyword_index.reindex(root)
            keyword_reindexed = True
        except Exception as exc:  # noqa: BLE001 - report, don't crash the maintenance pass
            keyword_reindexed = False
            warnings.append(f"keyword_reindex_failed:{type(exc).__name__}")

        status = "failed" if warnings else "succeeded"
        _append_log(wiki_dir, f"reindex: {status} (index_rebuilt={index_rebuilt}, "
                              f"keyword={keyword_reindexed}) [{job_id}]")
        if conn is not None:
            db.update_job(conn, job_id, status=status, finished_at=iso_now(), warnings=warnings,
                          metadata={"index_rebuilt": index_rebuilt,
                                    "keyword_reindexed": keyword_reindexed, "warnings": warnings})
        return {"job_id": job_id, "status": status, "index_rebuilt": index_rebuilt,
                "keyword_reindexed": keyword_reindexed, "warnings": warnings}
    except Exception as exc:
        if conn is not None:
            db.update_job(conn, job_id, status="failed", finished_at=iso_now(), error_message=str(exc))
        raise
    finally:
        if conn is not None:
            conn.close()
