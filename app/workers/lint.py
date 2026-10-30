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
  (record-only, high severity). An **uncited active claim** is reported (a backstop; the claim worker
  already tombstones evidence-less claims). A **merge candidate** (two active items whose title/alias
  merge keys intersect) → `merge_items` proposal (ADR-0064, star around one survivor; a match with a
  candidate side is report-only `merge_candidate_unpromoted`).
- **Unused tags (report-only, ADR-0066 decision 9):** an `unused` item (no document keeps it) is
  reported for the Unused tags list; no review item. The former single-source deprecation rule
  (`under_supported_item`) is retired — a tag kept on one document is valid curation — and its
  still-pending `deprecate_wiki_page` items are withdrawn with reason `rule_retired`.

Idempotent: review items key on `(type, subject={source_id|node_id})`, so re-running files no duplicates.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from app.backend import db, graph, taxonomy, write_lock
from app.backend.manifests import get_provenance, is_source_id, iso_now, valid_manifests
from app.backend.paths import safe_child, safe_under
from app.backend.policy import load_yaml
from app.llm.client import ConfigError, parse_chain
from app.workers import citations, enrichment_artifact, labels, merge_candidates, reviews, synthesis, tag_links
from app.workers.enrichment_artifact import artifact_fingerprint
from app.workers.wiki_render import display_link_label, parse_frontmatter

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


# The proposal reason the retired `under_supported_item` rule wrote (ADR-0066 decision 9).
_RETIRED_UNDER_SUPPORTED_REASON = re.compile(r"active with \d+ mentioning source\(s\) \(<2\)")


def _withdraw_retired_rule_items(reviews_dir: Path, now: str) -> list[str]:
    """Withdraw the still-pending `deprecate_wiki_page` items the retired single-source rule filed
    (reason `rule_retired`, audited). Approved/rejected items are human records — never touched.
    Idempotent: once withdrawn, nothing re-files them."""
    withdrawn: list[str] = []
    d = Path(reviews_dir) / "pending"
    for path in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(item, dict) or item.get("type") != "deprecate_wiki_page":
            continue
        proposal = item.get("proposal") or {}
        if (proposal.get("reason_code") is None
                and proposal.get("to_status") == "deprecated_candidate"
                and _RETIRED_UNDER_SUPPORTED_REASON.fullmatch(str(proposal.get("reason") or ""))
                and reviews.withdraw_review_item(reviews_dir, str(item.get("review_id") or path.stem),
                                                 reason="rule_retired", now=now)):
            withdrawn.append(str(item.get("review_id") or path.stem))
    return withdrawn


def _check_graph(gconn, wiki_dir: Path, reviews_dir: Path, *, file_items: bool, now: str,
                 filed: list[str], existing: list[str]) -> list[dict[str, Any]]:
    """Unused tags (report-only, ADR-0066 decision 9) + uncited active claims (report-only)."""
    findings: list[dict[str, Any]] = []
    for node_type in _ITEM_FAMILY:
        for node in graph.nodes_of_type(gconn, node_type):
            if node["status"] == "unused":
                findings.append({
                    "check": "unused_tag", "severity": "low", "subject": node["node_id"],
                    "detail": "no document keeps this tag (see /ui/reviews/tags/unused)"})
    for claim in graph.nodes_of_type(gconn, "claim"):
        if claim["status"] == "active" and not graph.sources_for_claim(gconn, claim["node_id"]):
            findings.append({"check": "uncited_claim", "severity": "medium",
                             "subject": claim["node_id"], "detail": "active claim with no active source"})
    return findings


def _merge_key_budget(policy_path: Path) -> int:
    """`merge_candidates.max_items_per_key` from policies/review.yaml (ADR-0064 review round); the
    code default when the file/key is absent or not an integer ≥ 2."""
    try:
        policy = load_yaml(policy_path.read_text(encoding="utf-8")) if policy_path.exists() else {}
    except (OSError, ValueError):
        policy = {}
    section = policy.get("merge_candidates") if isinstance(policy, dict) else None
    value = section.get("max_items_per_key") if isinstance(section, dict) else None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 2:
        return value
    return merge_candidates.DEFAULT_MAX_ITEMS_PER_KEY


def _check_merge_candidates(gconn, wiki_dir: Path, reviews_dir: Path, *, policy_path: Path,
                            file_items: bool, now: str, filed: list[str],
                            existing: list[str]) -> list[dict[str, Any]]:
    """Merge candidates (ADR-0064) → `merge_items` proposals; severity low (never `failing`).

    New star proposals are filed; still-unresolved machine proposals are re-reported as findings and
    counted as existing (the in-flight freeze keeps them out of the recomputation)."""
    found = merge_candidates.find_merge_candidates(
        gconn, wiki_dir=wiki_dir, reviews_dir=reviews_dir,
        max_items_per_key=_merge_key_budget(policy_path),
        blocked_keys=frozenset(tag_links.load_blocked(policy_path.parent)))
    findings: list[dict[str, Any]] = []
    for prop in found["proposals"]:
        subj = prop["subject"]
        findings.append({
            "check": "merge_candidate", "severity": "low", "subject": subj["absorbed_node_id"],
            "detail": f"merge into {subj['survivor_node_id']} "
                      f"(keys: {', '.join(prop['proposal']['matched_keys'])})",
            "data": {**subj, "matched_keys": prop["proposal"]["matched_keys"],
                     "remediation": "decide_merge"}})
        if file_items:
            _file_review(reviews_dir, review_type="merge_items", subject=subj,
                         proposal=prop["proposal"], context=prop["context"], priority="low",
                         now=now, filed=filed, existing=existing)
    for item in found["pending"]:
        subj = item["subject"]
        findings.append({
            "check": "merge_candidate", "severity": "low", "subject": subj.get("absorbed_node_id"),
            "detail": f"merge into {subj.get('survivor_node_id')} (pending review)",
            "data": {"survivor_node_id": subj.get("survivor_node_id"),
                     "absorbed_node_id": subj.get("absorbed_node_id"),
                     "review_id": item.get("review_id"), "remediation": "decide_merge"}})
        if file_items and item.get("review_id"):
            existing.append(item["review_id"])
    for common in found["too_common"]:
        findings.append({
            "check": "merge_key_too_common", "severity": "low", "subject": None,
            "detail": f"merge key {common['key']!r} is carried by {common['active_items']} active "
                      "items — too generic to signal identity; ignored for merge proposals",
            "data": dict(common)})
    for match in found["unpromoted"]:
        findings.append({
            "check": "merge_candidate_unpromoted", "severity": "low",
            "subject": match["node_ids"][0],
            "detail": f"matches {match['node_ids'][1]} but a side is a candidate "
                      f"(keys: {', '.join(match['matched_keys'])}); not filed",
            "data": {"node_ids": match["node_ids"], "matched_keys": match["matched_keys"]}})
    return findings


def _chain_refs(model_ref_chain: str | None) -> list[str]:
    """Parse a tier model chain into its member refs for the sticky-to-chain rot checks (ADR-0063).

    The lint receives the configured chain (e.g. `settings.enrich_model_light`). A malformed value
    degrades to a single-member best-effort list rather than crashing the whole lint pass."""
    if not model_ref_chain:
        return []
    try:
        return parse_chain(model_ref_chain)
    except ConfigError:
        return [model_ref_chain.strip()]


def _check_summary_rot(enrichment_dir: Path, markdown_dir: Path, wiki_dir: Path,
                       summary_model_ref: str | None,
                       manifests_dir: Path | None = None) -> tuple[list[dict[str, Any]], bool]:
    """Enriched Source summaries whose artifact fingerprint no longer matches the current inputs (ADR-0037).

    `summary_rot` = `normalized/enrichment/<sid>.json.input_fingerprint` != `artifact_fingerprint(current
    normalized md, current configured summary model_ref)` — "the current enrich pass would regenerate it".
    Graph-independent, key-free. Stub/missing artifact is not rot. Coverage-`degraded` only on an
    expectation mismatch: an enriched artifact whose normalized md is gone, or a Source page marked
    `summary_status: enriched` whose artifact is missing/unreadable (page reads are coverage-only)."""
    findings: list[dict[str, Any]] = []
    degraded = False
    chain_refs = _chain_refs(summary_model_ref)
    # ADR-0066 part 3: Enrich's freshness includes its file title and PDF hint (from the manifest
    # and the extraction log); a source without a readable manifest falls back to the text alone.
    by_sid = {m["source_id"]: m for m in valid_manifests(manifests_dir)[0]} if manifests_dir else {}
    logs_dir = markdown_dir.parent / "extraction_logs"
    if enrichment_dir.exists() and chain_refs:
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
            md_text = md_path.read_text(encoding="utf-8")
            # ADR-0063 sticky-to-chain: rot only if the recorded model left the chain OR its own-model
            # fingerprint drifted; an availability flip (recorded model still in the chain) is not rot.
            prompt_input = (enrichment_artifact.enrich_prompt_input_for(by_sid[sid], logs_dir)
                            if sid in by_sid else art.get("prompt_input"))
            if not enrichment_artifact.chain_fresh(
                    art, chain_refs,
                    lambda m: artifact_fingerprint(md_text, m, prompt_input=prompt_input)):
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


def _check_awaiting_reconciliation(manifests_dir: Path | None,
                                   jobs_db: Path | None) -> list[dict[str, Any]]:
    """`awaiting_reconciliation` findings: one per (producer, document) whose confirmed independence
    inputs changed since that producer last acknowledged them (or have never been acknowledged)."""
    from app.workers import reconciliation
    if manifests_dir is None or jobs_db is None:
        return []
    out = []
    for producer, sids in reconciliation.awaiting(manifests_dir, jobs_db).items():
        for sid in sids:
            out.append({"check": "awaiting_reconciliation", "severity": "low", "subject": sid,
                        "detail": f"{reconciliation.PRODUCERS[producer]} hasn't run since this "
                                  "document's provenance changed",
                        "data": {"source_id": sid, "producer": producer,
                                 "remediation": f"run {producer}"}})
    return out


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
    chain_refs = _chain_refs(model_ref)
    if not chain_refs:
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
        meta: dict[str, Any] = {}
        if apath.exists():
            try:
                meta = json.loads(apath.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
        if meta.get("input_fingerprint") is None:  # active synthesis but artifact missing/unreadable
            findings.append({"check": "synthesis_unverifiable", "severity": "low", "subject": syn_id,
                             "detail": "active synthesis but artifact missing/unreadable",
                             "data": {"synthesis_id": syn_id, "topic_node_id": tid,
                                      "remediation": "rerun_synthesis"}})
            degraded = True
            continue
        # ADR-0063 sticky-to-chain: rot only if the recorded model left the chain OR its own-model
        # fingerprint drifted; an availability flip (recorded model still in the chain) is not rot.
        if not enrichment_artifact.chain_fresh(
                meta, chain_refs, lambda m, topic=topic: synthesis._fingerprint(topic, m)):
            findings.append({"check": "synthesis_rot", "severity": "low", "subject": syn_id,
                             "detail": "active synthesis stale vs current topic evidence / model",
                             "data": {"synthesis_id": syn_id, "topic_node_id": tid,
                                      "remediation": "rerun_synthesis"}})
    return findings, degraded


_ALIAS_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_ALIAS_FM_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)
_ALIAS_FENCE_RE = re.compile(r"(?ms)^```.*?^```[ \t]*$")


def _check_display_alias_rot(wiki_dir: Path) -> list[dict[str, Any]]:
    """Aliased links whose display text drifted from the target's current label (ADR-0060).

    Report-only, severity low, graph-independent (pages only, never a model call). Compares the
    link's alias against `display_link_label(target's current label)` — the RENDERED link label,
    never the raw frontmatter `title:` (the two-layer contract: a capped alias on a long-titled
    target is current, not rot). Bare links are the alias-shape validator's contract, not lint's;
    remediation is the free ADR-0060 §6 re-render chain — no review item is ever filed."""
    findings: list[dict[str, Any]] = []
    label_cache: dict[str, str] = {}
    pages: list[Path] = []
    # ADR-0060 scope exactly: the five generated families + index.md (Tags deliberately absent).
    for subdir in ("Sources", "Items", "Claims", "Synthesis", "Queries"):
        folder = wiki_dir / subdir
        if folder.exists():
            pages.extend(sorted(folder.rglob("*.md")))
    if (wiki_dir / "index.md").exists():
        pages.append(wiki_dir / "index.md")
    for path in pages:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        body = _ALIAS_FENCE_RE.sub("", _ALIAS_FM_RE.sub("", text, count=1))
        for match in _ALIAS_LINK_RE.finditer(body):
            raw = match.group(1)
            target_part, sep, alias = raw.partition("|")
            if not sep or not alias.strip():
                continue  # bare/blank -> validate_link_aliases' contract
            target = target_part.split("#", 1)[0].strip()
            if not target:
                continue
            if target not in label_cache:
                label_cache[target] = labels._page_label(wiki_dir / f"{target}.md", target)
            current = label_cache[target]
            if not current:
                continue  # target missing/unlabelled -> dangling is validate_wikilinks' contract
            expected = display_link_label(current)
            if alias.strip() != expected:
                findings.append({
                    "check": "display_alias_rot", "severity": "low",
                    "subject": path.relative_to(wiki_dir).as_posix(),
                    "detail": "link alias drifted from the target's current display label",
                    "data": {"page": path.relative_to(wiki_dir).as_posix(), "target": target,
                             "alias": alias.strip(), "current_label": expected,
                             "remediation": "rerun_render_chain"}})
    return findings


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

        # ADR-0068: the checks file and withdraw review items as they go, so detection and filing
        # run as one commit phase under the write lock (the validators above only read).
        with write_lock.hold(reviews_dir):
            findings: list[dict[str, Any]] = []
            filed: list[str] = []
            existing: list[str] = []

            findings += _check_missing_raw(
                manifests_dir, root, reviews_dir, file_items=file_review_items, now=now,
                filed=filed, existing=existing)
            withdrawn = _withdraw_retired_rule_items(reviews_dir, now) if file_review_items else []

            # summary_rot is graph-independent (artifact + normalized md only) — runs always (ADR-0037).
            rot_findings, coverage_degraded = _check_summary_rot(
                enrichment_dir, markdown_dir, wiki_dir, summary_model_ref, manifests_dir)
            findings += rot_findings

            # topic_starvation is artifact-driven and graph-independent — runs always (ADR-0059).
            findings += _check_topic_starvation(enrichment_dir)

            # ADR-0066 part 3 slice 3: documents whose independence inputs changed since a producer
            # reconciled them. Report-only (low): never fails lint, nothing is filed.
            findings += _check_awaiting_reconciliation(manifests_dir, jobs_db)

            # display_alias_rot reads wiki pages only — runs always (ADR-0060, report-only).
            findings += _check_display_alias_rot(wiki_dir)

            gconn = _open_graph_ro(graph_db)
            graph_available = gconn is not None
            if gconn is not None:
                try:
                    findings += _check_graph(
                        gconn, wiki_dir, reviews_dir, file_items=file_review_items, now=now,
                        filed=filed, existing=existing)
                    findings += _check_merge_candidates(  # graph-gated (ADR-0064)
                        gconn, wiki_dir, reviews_dir, policy_path=root / "policies" / "review.yaml",
                        file_items=file_review_items, now=now,
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
                   "review_items_existing": len(existing_items),
                   "review_items_withdrawn": len(withdrawn), "graph_available": graph_available}
        if conn is not None:
            db.update_job(conn, job_id, status="succeeded", finished_at=iso_now(), metadata=summary)

        return {"job_id": job_id, "status": status, "validators_ok": validators_ok,
                "validators": validators, "findings": findings, "by_check": by_check,
                "review_items_filed": new_items, "review_items_existing": existing_items,
                "review_items_withdrawn": withdrawn, "graph_available": graph_available}
    except Exception as exc:
        if conn is not None:
            db.update_job(conn, job_id, status="failed", finished_at=iso_now(), error_message=str(exc))
        raise
    finally:
        if conn is not None:
            conn.close()
