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
  (record-only, high severity); an **under-supported active item** (<2 distinct mentioning sources)
  → `deprecate_wiki_page` proposal. An **uncited active claim** is reported (a backstop; the claim worker
  already tombstones evidence-less claims).

Idempotent: review items key on `(type, subject={source_id|node_id})`, so re-running files no duplicates.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from app.backend import db, graph, search, taxonomy
from app.backend.manifests import get_provenance, is_source_id, iso_now, valid_manifests
from app.backend.paths import safe_child, safe_under
from app.workers import citations, enrichment_artifact, reviews, synthesis
from app.workers.enrichment_artifact import artifact_fingerprint
from app.workers.wiki_render import NODE_DIR, parse_frontmatter

# Knowledge-item family — the promotable node type a source "mentions" (ADR-0059).
_ITEM_FAMILY = ("item",)


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
    """Resolve a manifest occurrence path under ``root/raw``, or ``None`` if unsafe (ADR-0009)."""
    return safe_under(root, raw_root, rel)


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
    # valid_manifests quarantines non-canonical ids; validate_raw_integrity (run via run_validators)
    # is the loud signal that surfaces such tampering as a lint failure.
    for m in valid_manifests(manifests_dir)[0]:
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
    """Under-supported active items (→ deprecate proposal) + uncited active claims (report-only)."""
    findings: list[dict[str, Any]] = []
    for node_type in _ITEM_FAMILY:
        for node in graph.nodes_of_type(gconn, node_type):
            if node["status"] != "active":
                continue
            # Live support follows default retrieval visibility: archived/deleted sources still exist
            # as evidence, but they no longer keep an item active by themselves.
            n = graph.count_independent_sources(
                gconn, node["node_id"], source_statuses=search.RETENTION_DEFAULT_STATUSES)
            if n >= 2:
                continue
            findings.append({
                "check": "under_supported_item", "severity": "high" if n == 0 else "medium",
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


def _check_summary_rot(enrichment_dir: Path, markdown_dir: Path, wiki_dir: Path,
                       summary_model_ref: str | None) -> tuple[list[dict[str, Any]], bool]:
    """Enriched Source summaries whose artifact fingerprint no longer matches the current inputs (ADR-0037).

    `summary_rot` = `normalized/enrichment/<sid>.json.input_fingerprint` != `artifact_fingerprint(current
    normalized md, current configured summary model_ref)` — "the current enrich pass would regenerate it".
    Graph-independent, key-free. Stub/missing artifact is not rot. Coverage-`degraded` only on an
    expectation mismatch: an enriched artifact whose normalized md is gone, or a Source page marked
    `summary_status: enriched` whose artifact is missing/unreadable (page reads are coverage-only)."""
    findings: list[dict[str, Any]] = []
    degraded = False
    if enrichment_dir.exists() and summary_model_ref:
        for apath in sorted(enrichment_dir.glob("*.json")):
            # Positive-only allowlist (ADR-0037): exactly `src_<16 hex>.json`. `.claims`/`.items`/
            # `.synthesis` artifacts have non-canonical stems and are rejected here — no blocklist.
            sid = apath.stem
            if not is_source_id(sid):
                continue
            try:
                art = json.loads(apath.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if art.get("source_id") != sid:  # internal id must match the filename (no spoofing)
                continue
            if art.get("generation_status") != "enriched":
                continue
            md_path = markdown_dir / f"{sid}.md"  # sid is canonical-validated
            if not md_path.exists():  # can't recompute the fingerprint -> unverifiable, not rot
                findings.append({"check": "summary_unverifiable", "severity": "low", "subject": sid,
                                 "detail": "enriched summary artifact but normalized markdown missing",
                                 "data": {"source_id": sid}})
                degraded = True
                continue
            current = artifact_fingerprint(md_path.read_text(encoding="utf-8"), summary_model_ref)
            if art.get("input_fingerprint") != current:
                findings.append({"check": "summary_rot", "severity": "low", "subject": sid,
                                 "detail": "enriched summary stale vs current normalized markdown / model",
                                 "data": {"source_id": sid, "remediation": "rerun_enrich"}})
    # Coverage probe (page reads only): a page claiming enrichment whose durable artifact is gone.
    sources_dir = wiki_dir / "Sources"
    if sources_dir.exists():
        for page in sorted(sources_dir.glob("*.md")):
            sid = page.stem
            if not is_source_id(sid):  # never build a path from a non-canonical page stem
                continue
            try:
                fm = parse_frontmatter(page.read_text(encoding="utf-8"))
            except OSError:
                continue
            if fm.get("summary_status") != "enriched":
                continue
            apath = enrichment_dir / f"{sid}.json"
            ok = apath.exists()
            if ok:
                try:
                    json.loads(apath.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    ok = False
            if not ok:
                findings.append({"check": "summary_unverifiable", "severity": "low", "subject": sid,
                                 "detail": "Source page marked enriched but artifact missing/unreadable",
                                 "data": {"source_id": sid}})
                degraded = True
    return findings, degraded


def _check_topic_starvation(enrichment_dir: Path) -> list[dict[str, Any]]:
    """Substantive sources whose items artifact extracted no thematic topic layer (ADR-0059). Report-only.

    The starvation pattern: `thematic == 0 AND (named >= threshold OR claims >= 1)` per source,
    read from the durable `<sid>.items.json` / `<sid>.claims.json` artifacts via the shared
    `enrichment_artifact.topic_starved` predicate. Artifact/claim state ONLY — never raw text
    length or normalized text shape (that would reopen the "substantive document" classifier
    problem). Graph-independent and key-free; severity medium (it suppresses the source's entire
    topic layer); remediation `rerun_extract_items`. Unreadable/spoofed artifacts are skipped
    (validators own artifact integrity); a missing claims artifact counts as zero claims.
    """
    findings: list[dict[str, Any]] = []
    if not enrichment_dir.exists():
        return findings
    for apath in sorted(enrichment_dir.glob("*.items.json")):
        sid = apath.name[:-len(".items.json")]
        if not is_source_id(sid):  # positive-only allowlist, as in _check_summary_rot
            continue
        try:
            artifact = json.loads(apath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if artifact.get("source_id") != sid:  # internal id must match the filename (no spoofing)
            continue
        nodes = artifact.get("nodes")
        if not isinstance(nodes, list):
            continue
        claim_count = enrichment_artifact.stored_claim_count(enrichment_dir, sid)
        if enrichment_artifact.topic_starved(nodes, claim_count):
            named = sum(1 for n in nodes if n.get("item_type") in taxonomy.NAMED_TYPES)
            findings.append({
                "check": "topic_starvation", "severity": "medium", "subject": sid,
                "detail": "substantive source extracted no thematic topic layer (named items/claims present)",
                "data": {"source_id": sid, "thematic_item_count": 0, "named_item_count": named,
                         "claim_count": claim_count, "remediation": "rerun_extract_items"}})
    return findings


def _check_stale_claims(gconn, enrichment_dir: Path,
                        markdown_dir: Path) -> tuple[list[dict[str, Any]], bool]:
    """Claim citations whose stored anchor no longer supports the stored quote (ADR-0037). Graph-gated.

    Stale detection is artifact-driven: for each durable `.claims.json` citation that exactly matches an
    **active `derived_from` edge** on `(claim_id, source_id, char_start, char_end)`, re-ground the
    **stored** quote against the current normalized markdown (`ground_citation(require_quote=True)`); a
    non-empty problem list -> `stale_claim_citation`. Active-node-only matching is too loose (a claim can
    stay active via another source while this edge is superseded). Coverage is graph-driven: a source the
    graph shows has active claim evidence but whose `.claims.json` / markdown can't be read -> `degraded`."""
    findings: list[dict[str, Any]] = []
    degraded = False
    cite_cache: dict[str, dict[tuple, Any] | None] = {}  # sid -> {(cid,sid,start,end): quote} or None
    md_cache: dict[str, str | None] = {}

    def _citations(sid: str) -> dict[tuple, Any] | None:
        if sid not in cite_cache:
            apath = enrichment_dir / f"{sid}.claims.json"
            if not apath.exists():
                cite_cache[sid] = None
            else:
                try:
                    data = json.loads(apath.read_text(encoding="utf-8"))
                    lut: dict[tuple, Any] = {}
                    for item in data.get("claims", []):
                        c = item.get("citation") or {}
                        lut[(item.get("claim_id"), c.get("source_id"),
                             c.get("char_start"), c.get("char_end"))] = c.get("quote")
                    cite_cache[sid] = lut
                except (OSError, json.JSONDecodeError):
                    cite_cache[sid] = None
        return cite_cache[sid]

    def _md(sid: str) -> str | None:
        if sid not in md_cache:
            mp = markdown_dir / f"{sid}.md"
            md_cache[sid] = mp.read_text(encoding="utf-8") if mp.exists() else None
        return md_cache[sid]

    def _unverifiable(cid, detail, data):
        nonlocal degraded
        findings.append({"check": "claim_evidence_unverifiable", "severity": "low",
                         "subject": cid, "detail": detail, "data": data})
        degraded = True

    # Edge-driven: every active derived_from edge MUST be backed by an exact durable citation we can
    # re-ground; anything missing/unsafe is unverifiable -> degraded, never a silent healthy (ADR-0037).
    for cid in graph.claims_with_active_evidence(gconn):
        for e in graph.outgoing_active(gconn, cid):
            if e["edge_type"] != "derived_from":
                continue
            sid, start, end = e["dst_id"], e["evidence_char_start"], e["evidence_char_end"]
            if not is_source_id(sid):  # never build a path from a non-canonical edge source; no id leak
                _unverifiable(cid, "active claim evidence has a non-canonical source id", {"claim_id": cid})
                continue
            lut = _citations(sid)
            if lut is None:
                _unverifiable(cid, "active claim evidence but claims artifact missing/unreadable",
                              {"claim_id": cid, "source_id": sid})
                continue
            key = (cid, sid, start, end)
            if key not in lut:  # exact citation absent/mismatched (e.g. wrong span) -> cannot verify
                _unverifiable(cid, "active edge citation absent from durable claims artifact",
                              {"claim_id": cid, "source_id": sid, "char_start": start, "char_end": end})
                continue
            md = _md(sid)
            if md is None:
                _unverifiable(cid, "active claim evidence but source markdown missing",
                              {"claim_id": cid, "source_id": sid})
                continue
            problems = citations.ground_citation(
                {"source_id": sid, "char_start": start, "char_end": end, "quote": lut[key]},
                md, require_quote=True)
            if problems:
                findings.append({
                    "check": "stale_claim_citation", "severity": "medium", "subject": cid,
                    "detail": "stored citation quote no longer grounds against current markdown",
                    "data": {"claim_id": cid, "source_id": sid, "char_start": start,
                             "char_end": end, "remediation": "rerun_extract_claims"}})
    return findings, degraded


def _check_synthesis_rot(gconn, manifests_dir: Path, claims_dir: Path, markdown_dir: Path,
                         enrichment_dir: Path,
                         model_ref: str | None) -> tuple[list[dict[str, Any]], bool]:
    """Active syntheses whose evidence drifted since approval (ADR-0037 decision 6). Graph-gated.

    `synthesis_rot` = stored `<topic_id>.synthesis.json.input_fingerprint` != the producer's
    `synthesis._fingerprint(current topic, enrich_model_heavy)` — surfaced key-free via the producer's own
    `eligible_topics` (so lint matches `stale_active` exactly). Topic-driven: an **evidence-gone** topic is
    absent from `eligible_topics` and never visited (that lifecycle condition is the producer's deprecation
    concern, not rot). An active synthesis whose artifact is missing/unreadable -> `synthesis_unverifiable`
    (degraded) — the only unverifiable trigger. Skipped when `model_ref` is None (can't recompute)."""
    findings: list[dict[str, Any]] = []
    degraded = False
    if not model_ref:
        return findings, degraded
    prov = {m["source_id"]: get_provenance(m) for m in valid_manifests(manifests_dir)[0]}
    for topic in synthesis.eligible_topics(
            gconn, prov, claims_dir=claims_dir, markdown_dir=markdown_dir):
        tid = topic["node_id"]
        syn_id = synthesis.synthesis_id(tid)
        node = graph.get_node(gconn, syn_id)
        if not node or node["status"] != "active":
            continue  # only an active synthesis can be stale; candidate/none -> nothing
        apath = safe_child(enrichment_dir, f"{tid}.synthesis.json")  # tid untrusted (basename only)
        if apath is None:
            continue  # path-like topic id -> skip read; validate_graph fails hard on it
        stored = None
        if apath.exists():
            try:
                stored = json.loads(apath.read_text(encoding="utf-8")).get("input_fingerprint")
            except (OSError, json.JSONDecodeError):
                stored = None
        if stored is None:  # active synthesis but artifact missing/unreadable -> can't verify
            findings.append({"check": "synthesis_unverifiable", "severity": "low", "subject": syn_id,
                             "detail": "active synthesis but artifact missing/unreadable",
                             "data": {"synthesis_id": syn_id, "topic_node_id": tid,
                                      "remediation": "rerun_synthesis"}})
            degraded = True
            continue
        if stored != synthesis._fingerprint(topic, model_ref):
            findings.append({"check": "synthesis_rot", "severity": "low", "subject": syn_id,
                             "detail": "active synthesis stale vs current topic evidence / model",
                             "data": {"synthesis_id": syn_id, "topic_node_id": tid,
                                      "remediation": "rerun_synthesis"}})
    return findings, degraded


def run_lint(
    root: Path,
    *,
    manifests_dir: Path | None = None,
    graph_db: Path | None = None,
    wiki_dir: Path | None = None,
    reviews_dir: Path | None = None,
    enrichment_dir: Path | None = None,
    markdown_dir: Path | None = None,
    summary_model_ref: str | None = None,
    synthesis_model_ref: str | None = None,
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

    `summary_model_ref` / `synthesis_model_ref` are the current configured summary-tier / synthesis-tier
    model_refs (the API passes `settings.enrich_model_light` / `settings.enrich_model_heavy`); the
    ADR-0037 `summary_rot` / `synthesis_rot` checks are each **skipped when their model_ref is None**, so a
    direct caller that wants rot detection must pass them.
    """
    root = Path(root).resolve()
    manifests_dir = Path(manifests_dir) if manifests_dir else root / "raw" / "manifests"
    graph_db = Path(graph_db) if graph_db else root / "db" / "graph.sqlite"
    wiki_dir = Path(wiki_dir) if wiki_dir else root / "wiki"
    reviews_dir = Path(reviews_dir) if reviews_dir else root / "reviews"
    enrichment_dir = Path(enrichment_dir) if enrichment_dir else root / "normalized" / "enrichment"
    markdown_dir = Path(markdown_dir) if markdown_dir else root / "normalized" / "markdown"
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

        # summary_rot is graph-independent (artifact + normalized md only) — runs always (ADR-0037).
        rot_findings, coverage_degraded = _check_summary_rot(
            enrichment_dir, markdown_dir, wiki_dir, summary_model_ref)
        findings += rot_findings

        # topic_starvation is artifact-driven and graph-independent — runs always (ADR-0059).
        findings += _check_topic_starvation(enrichment_dir)

        gconn = _open_graph_ro(graph_db)
        graph_available = gconn is not None
        if gconn is not None:
            try:
                findings += _check_graph(
                    gconn, wiki_dir, reviews_dir, file_items=file_review_items, now=now,
                    filed=filed, existing=existing)
                stale_findings, stale_degraded = _check_stale_claims(  # graph-gated (ADR-0037)
                    gconn, enrichment_dir, markdown_dir)
                findings += stale_findings
                synth_findings, synth_degraded = _check_synthesis_rot(  # graph-gated (ADR-0037 dec. 6)
                    gconn, manifests_dir, wiki_dir / "Claims", markdown_dir, enrichment_dir,
                    synthesis_model_ref)
                findings += synth_findings
                coverage_degraded = coverage_degraded or stale_degraded or synth_degraded
            finally:
                gconn.close()
        else:
            findings.append({"check": "graph_unavailable", "severity": "medium", "subject": None,
                             "detail": "graph absent or schema-mismatched; semantic checks skipped"})

        high = any(f["severity"] == "high" for f in findings)
        if not validators_ok or high:
            status = "failing"
        elif not graph_available or coverage_degraded:  # incomplete/unverifiable coverage
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
