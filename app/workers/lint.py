#!/usr/bin/env python3
"""Phase 7 slice 7-1: the `/jobs/lint` maintenance pass (ADR-0036).

A deterministic, job-recorded health pass that **detects and proposes** — it never acts on anything
semantic/destructive. It runs the structural validators as a health report, runs a few semantic checks
over the graph + manifests, files governance review items for the actionable findings, appends
`wiki/log.md`, and returns a typed report.

**Lint health is an outcome, not an abort** (ADR-0036 decision 3): the pass always completes and records
its job; `status: "failing"` means problems were found, not that the run errored. This is distinct from
the deterministic `validate_*` checks, which may hard-fail on a true integrity violation.

Finding classes:
- **Structural (report-only):** the `validate_*` suite (frontmatter, wikilinks, citations, summary
  callouts, …) — defects fixed by regeneration, not governance.
- **Governance (→ review items):** a catalogued **raw file gone missing** → `missing_raw_source`
  (record-only, high severity); an **under-supported active concept** (<2 distinct mentioning sources)
  → `deprecate_wiki_page` proposal. An **uncited active claim** is reported (a backstop; the claim worker
  already tombstones evidence-less claims).

Idempotent: review items key on `(type, subject={source_id|node_id})`, so re-running files no duplicates.
"""
from __future__ import annotations

import subprocess
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from app.backend import db, graph, search
from app.backend.manifests import iso_now, list_manifests
from app.workers import reviews
from app.workers.wiki_render import NODE_DIR

# Concept/entity family — the promotable node types a source "mentions" (ADR-0017/0018).
_CONCEPT_FAMILY = ("concept", "entity", "person", "organization", "project")


def run_validators(root: Path) -> list[dict[str, Any]]:
    """Run every `scripts/validate_*.py` once (the structural integrity bar), capturing results.

    Discovered exactly as `scripts/validate_all.py` does; output tails are sanitized of the absolute
    root path (no path leak). Shared by `/jobs/lint` and `POST /reviews/apply`.
    """
    root = Path(root)
    scripts_dir = root / "scripts"
    root_str = str(root)
    results: list[dict[str, Any]] = []
    for script in sorted(scripts_dir.glob("validate_*.py")):
        if script.name == "validate_all.py":
            continue
        proc = subprocess.run(
            [sys.executable, str(script), str(root)], capture_output=True, text=True)
        results.append({
            "name": script.name, "returncode": proc.returncode,
            "stdout_tail": proc.stdout.replace(root_str, "<root>")[-800:],
            "stderr_tail": proc.stderr.replace(root_str, "<root>")[-800:]})
    return results


def _open_graph_ro(graph_db: Path) -> Any:
    """Open the graph read-only, or None if absent/schema-mismatched. Never creates the DB."""
    graph_db = Path(graph_db)
    if not graph_db.exists():
        return None
    conn = graph.connect(graph_db)
    if graph.schema_version(conn) != graph.SCHEMA_VERSION:
        conn.close()
        return None
    return conn


def _append_log(wiki_dir: Path, message: str) -> None:
    log = Path(wiki_dir) / "log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"- {message}\n")


def _safe_raw_rel(root: Path, raw_root: Path, rel: str) -> Path | None:
    """Resolve a manifest occurrence path under ``root/raw``, or ``None`` if unsafe.

    Mirrors the extraction boundary guard (``extract.py``, ADR-0009): rejects absolute paths and any
    ``..`` segment, and requires the resolved path to stay under ``raw/`` — so a hand-edited/malformed
    manifest path can never make lint probe (or leak) a file outside the raw repository.
    """
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        return None
    resolved = (root / p).resolve()
    try:
        resolved.relative_to(raw_root)
    except ValueError:
        return None
    return resolved


def _item_exists(reviews_dir: Path, rid: str) -> bool:
    """True if a review item already exists in pending/approved/rejected (so it won't be re-created)."""
    return any((Path(reviews_dir) / s / f"{rid}.json").exists()
               for s in ("pending", "approved", "rejected"))


def _file_review(reviews_dir: Path, *, review_type: str, subject: dict[str, Any],
                 proposal: dict[str, Any], context: dict[str, Any], priority: str, now: str,
                 filed: list[str], existing: list[str]) -> None:
    """Create a review item, recording it as newly *filed* vs already *existing* (Q2). Idempotent."""
    rid = reviews.review_id(review_type, subject)
    if _item_exists(reviews_dir, rid):
        existing.append(rid)
        return
    reviews.create_review_item(reviews_dir, review_type=review_type, subject=subject,
                               proposal=proposal, context=context, priority=priority, now=now)
    filed.append(rid)


def _check_missing_raw(manifests_dir: Path, root: Path, reviews_dir: Path, *, file_items: bool,
                       now: str, filed: list[str], existing: list[str]) -> list[dict[str, Any]]:
    """A catalogued source with no on-disk raw file (or only unsafe paths) → high finding + review item.

    Path-confined: occurrence paths are resolved under ``root/raw`` and must be a real **file**
    (``is_file()``, not ``exists()`` — a directory does not count). An absolute/escaping path is an
    explicit ``invalid_raw_path`` finding; the review payload carries only safe repo-relative paths +
    a count of invalid ones, never an absolute/escaping path (no leak).
    """
    findings: list[dict[str, Any]] = []
    raw_root = (root / "raw").resolve()
    for m in list_manifests(manifests_dir):
        sid = m.get("source_id")
        if not sid:
            continue
        # Distinct paths only — relative_raw_path usually duplicates occurrences[0] (same file).
        rels = list(dict.fromkeys(
            r for r in [m.get("relative_raw_path"),
                        *(o.get("relative_path") for o in (m.get("occurrences") or []))] if r))
        safe_rels: list[str] = []
        invalid = 0
        present = False
        for r in rels:
            resolved = _safe_raw_rel(root, raw_root, r)
            if resolved is None:
                invalid += 1
                continue
            safe_rels.append(r)
            if resolved.is_file():
                present = True
        if present:
            continue
        if invalid and not safe_rels:
            check, detail = "invalid_raw_path", f"all {invalid} catalogued raw path(s) escape raw/"
        elif invalid:
            check, detail = "missing_raw", f"no raw file on disk ({invalid} path(s) also invalid)"
        else:
            check, detail = "missing_raw", "no catalogued raw file found on disk"
        findings.append({"check": check, "severity": "high", "subject": sid, "detail": detail})
        if file_items:
            _file_review(reviews_dir, review_type="missing_raw_source", subject={"source_id": sid},
                         proposal={"reason": detail, "safe_occurrences": safe_rels,
                                   "invalid_path_count": invalid},
                         context={"original_filename": m.get("original_filename")},
                         priority="high", now=now, filed=filed, existing=existing)
    return findings


def _check_graph(gconn, wiki_dir: Path, reviews_dir: Path, *, file_items: bool, now: str,
                 filed: list[str], existing: list[str]) -> list[dict[str, Any]]:
    """Under-supported active concepts (→ deprecate proposal) + uncited active claims (report-only)."""
    findings: list[dict[str, Any]] = []
    for node_type in _CONCEPT_FAMILY:
        for node in graph.nodes_of_type(gconn, node_type):
            if node["status"] != "active":
                continue
            # Live support follows default retrieval visibility: archived/deleted sources still exist
            # as evidence, but they no longer keep a concept active by themselves.
            n = graph.count_independent_sources(
                gconn, node["node_id"], source_statuses=search.RETENTION_DEFAULT_STATUSES)
            if n >= 2:
                continue
            findings.append({
                "check": "under_supported_concept", "severity": "high" if n == 0 else "medium",
                "subject": node["node_id"], "detail": f"active {node_type} with {n} mentioning source(s)"})
            if file_items:
                _file_review(
                    reviews_dir, review_type="deprecate_wiki_page",
                    subject={"node_id": node["node_id"],
                             "page": f"{NODE_DIR[node_type]}/{node['slug']}.md"},
                    proposal={"to_status": "deprecated_candidate",
                              "reason": f"active with {n} mentioning source(s) (<2)"},
                    context={"node_type": node_type}, priority="low", now=now,
                    filed=filed, existing=existing)
    for claim in graph.nodes_of_type(gconn, "claim"):
        if claim["status"] == "active" and not graph.sources_for_claim(gconn, claim["node_id"]):
            findings.append({"check": "uncited_claim", "severity": "medium",
                             "subject": claim["node_id"], "detail": "active claim with no active source"})
    return findings


def run_lint(
    root: Path,
    *,
    manifests_dir: Path | None = None,
    graph_db: Path | None = None,
    wiki_dir: Path | None = None,
    reviews_dir: Path | None = None,
    jobs_db: Path | None = None,
    record_job: bool = True,
    file_review_items: bool = True,
    now: str | None = None,
) -> dict[str, Any]:
    """Run the lint maintenance pass; return a typed health report (ADR-0036).

    Always completes (lint health is an outcome, not an abort). Three-state `status`:
    `"failing"` (a validator failed or a high-severity finding), `"degraded"` (completed but coverage was
    incomplete — e.g. graph absent/schema-mismatched so semantic checks were skipped — with nothing
    failing), else `"healthy"`. Files governance review items idempotently — `review_items_filed` are
    newly created this run, `review_items_existing` were already in the ledger — and appends `wiki/log.md`.
    """
    root = Path(root).resolve()
    manifests_dir = Path(manifests_dir) if manifests_dir else root / "raw" / "manifests"
    graph_db = Path(graph_db) if graph_db else root / "db" / "graph.sqlite"
    wiki_dir = Path(wiki_dir) if wiki_dir else root / "wiki"
    reviews_dir = Path(reviews_dir) if reviews_dir else root / "reviews"
    jobs_db = Path(jobs_db) if jobs_db else root / "db" / "jobs.sqlite"
    now = now or iso_now()
    job_id = f"job_{uuid.uuid4().hex[:16]}"

    conn = None
    if record_job:
        db.init_db(jobs_db)
        conn = db.connect(jobs_db)
        db.insert_job(conn, job_id=job_id, job_type="lint", status="running",
                      created_at=now, started_at=now)
    try:
        validators = run_validators(root)
        validators_ok = all(v["returncode"] == 0 for v in validators)

        findings: list[dict[str, Any]] = []
        filed: list[str] = []
        existing: list[str] = []

        findings += _check_missing_raw(
            manifests_dir, root, reviews_dir, file_items=file_review_items, now=now,
            filed=filed, existing=existing)

        gconn = _open_graph_ro(graph_db)
        graph_available = gconn is not None
        if gconn is not None:
            try:
                findings += _check_graph(
                    gconn, wiki_dir, reviews_dir, file_items=file_review_items, now=now,
                    filed=filed, existing=existing)
            finally:
                gconn.close()
        else:
            findings.append({"check": "graph_unavailable", "severity": "medium", "subject": None,
                             "detail": "graph absent or schema-mismatched; semantic checks skipped"})

        high = any(f["severity"] == "high" for f in findings)
        if not validators_ok or high:
            status = "failing"
        elif not graph_available:  # completed but semantic-check coverage was incomplete
            status = "degraded"
        else:
            status = "healthy"
        new_items = sorted(set(filed))
        existing_items = sorted(set(existing))
        by_check = dict(sorted(Counter(f["check"] for f in findings).items()))

        _append_log(wiki_dir, f"lint: {status} — {len(findings)} finding(s), "
                              f"{len(new_items)} review item(s) filed, {len(existing_items)} existing "
                              f"[{job_id}]")

        summary = {"status": status, "validators_ok": validators_ok, "findings": len(findings),
                   "by_check": by_check, "review_items_filed": len(new_items),
                   "review_items_existing": len(existing_items), "graph_available": graph_available}
        if conn is not None:
            db.update_job(conn, job_id, status="succeeded", finished_at=iso_now(), metadata=summary)

        return {"job_id": job_id, "status": status, "validators_ok": validators_ok,
                "validators": validators, "findings": findings, "by_check": by_check,
                "review_items_filed": new_items, "review_items_existing": existing_items,
                "graph_available": graph_available}
    except Exception as exc:
        if conn is not None:
            db.update_job(conn, job_id, status="failed", finished_at=iso_now(), error_message=str(exc))
        raise
    finally:
        if conn is not None:
            conn.close()
