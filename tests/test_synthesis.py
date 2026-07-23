from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import validate_frontmatter  # noqa: E402
import validate_graph  # noqa: E402
import validate_projection  # noqa: E402
import validate_wikilinks  # noqa: E402

from app.backend import graph, manifests  # noqa: E402
from app.llm.cache import ResponseCache  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.workers import claims, extract, intake, items, promote, reviews, synthesis, wiki  # noqa: E402

TEMPLATES = ROOT / "templates"
TIER2 = "anthropic:claude-sonnet-4-6"
HEAVY = "anthropic:claude-opus-4-8"

DOC_A = "# Report A\n\nThe Q3 revenue rose ten percent on strong demand.\n"
DOC_B = "# Report B\n\nThe Q3 revenue rose again, helped by new customers.\n"
QUOTE_A = "The Q3 revenue rose ten percent on strong demand."
QUOTE_B = "The Q3 revenue rose again, helped by new customers."
ITEM = "Q3 revenue"


class ClaimAdapter:
    name = "anthropic"
    supports_batch = False

    def available(self):
        return True

    def parse(self, messages, schema, model_id, *, max_tokens):
        body = messages[-1]["content"]
        out = []
        if QUOTE_A in body:
            out.append({"claim": "Q3 revenue rose ten percent.", "quote": QUOTE_A})
        if QUOTE_B in body:
            out.append({"claim": "Q3 revenue rose, helped by new customers.", "quote": QUOTE_B})
        return {"claims": out}


class ItemAdapter:
    name = "anthropic"
    supports_batch = False

    def available(self):
        return True

    def parse(self, messages, schema, model_id, *, max_tokens):
        return {"items": [{"name": ITEM, "item_type": "method_technique", "aliases": []}]}


class SynthAdapter:
    name = "anthropic"
    supports_batch = False

    def __init__(self, *, confidence=0.8, available=True):
        self._c = confidence
        self._available = available
        self.calls = 0

    def available(self):
        return self._available

    def parse(self, messages, schema, model_id, *, max_tokens):
        self.calls += 1
        return {"summary": "Both sources agree Q3 revenue rose.",
                "synthesis": "Across two independent reports, Q3 revenue increased.",
                "confidence": self._c}


def _client(tmp_path, adapter):
    return LLMClient({"anthropic": adapter}, cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite"))


def _ingest(tmp_path, name, text):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / name).write_text(text, encoding="utf-8")


def _sid(tmp_path, name):
    for m in manifests.list_manifests(tmp_path / "raw" / "manifests"):
        if m.get("original_filename") == name:
            return m["source_id"]
    raise KeyError(name)


def _build(tmp_path, *, second=True):
    """Two independent sources sharing a knowledge item; claims + items extracted; item promoted
    active (so it is an eligible synthesis topic)."""
    jobs = tmp_path / "db" / "jobs.sqlite"
    _ingest(tmp_path, "a.md", DOC_A)
    if second:
        _ingest(tmp_path, "b.md", DOC_B)
    intake.scan_inbox(tmp_path, jobs_db=jobs)
    extract.extract_sources(tmp_path, jobs_db=jobs)
    wiki.generate_wiki(tmp_path, jobs_db=jobs, templates_dir=TEMPLATES, rebuild_index=False)
    manifests.set_provenance(tmp_path / "raw" / "manifests", _sid(tmp_path, "a.md"), author="Alice")
    if second:
        manifests.set_provenance(tmp_path / "raw" / "manifests", _sid(tmp_path, "b.md"), author="Bob")
    claims.extract_claims(tmp_path, client=_client(tmp_path, ClaimAdapter()), model_ref=TIER2,
                          jobs_db=jobs, rebuild_index=False)
    items.extract_items(tmp_path, client=_client(tmp_path, ItemAdapter()), model_ref=TIER2,
                        jobs_db=jobs, rebuild_index=False)
    promote.promote_candidates(tmp_path, jobs_db=jobs, rebuild_index=False)  # item -> active
    wiki.generate_wiki(tmp_path, jobs_db=jobs, templates_dir=TEMPLATES, rebuild_index=False)
    return jobs


def _gen(tmp_path, adapter, jobs, **kw):
    return synthesis.generate_syntheses(
        tmp_path, client=_client(tmp_path, adapter), model_ref=HEAVY, jobs_db=jobs,
        rebuild_index=False, **kw)


def _gconn(tmp_path):
    return graph.connect(tmp_path / "db" / "graph.sqlite")


def _syn_page(tmp_path):
    return next((tmp_path / "wiki" / "Synthesis").glob("*.md"))


# --- eligibility -----------------------------------------------------------


def test_single_source_item_is_not_eligible(tmp_path):
    jobs = _build(tmp_path, second=False)
    summary = _gen(tmp_path, SynthAdapter(), jobs)
    assert summary["eligible_topics"] == 0 and summary["syntheses_written"] == 0
    assert not list((tmp_path / "wiki" / "Synthesis").glob("*.md"))


# --- generation ------------------------------------------------------------


def test_generates_candidate_synthesis_grounded_on_claims(tmp_path):
    jobs = _build(tmp_path)
    summary = _gen(tmp_path, SynthAdapter(), jobs)
    assert summary["eligible_topics"] == 1 and summary["syntheses_written"] == 1
    assert summary["status"] == "succeeded"

    page = _syn_page(tmp_path).read_text(encoding="utf-8")
    fm = validate_frontmatter.parse_frontmatter(page)
    assert fm["type"] == "synthesis" and fm["status"] == "candidate"
    assert fm["synthesis_id"].startswith("syn_")
    assert "[[Claims/" in page  # grounded on the contributing claim nodes

    conn = _gconn(tmp_path)
    try:
        syn = conn.execute("SELECT * FROM nodes WHERE node_type='synthesis'").fetchone()
        df = conn.execute("SELECT COUNT(*) AS n FROM edges WHERE src_id=? AND edge_type='derived_from' "
                          "AND status='active'", (syn["node_id"],)).fetchone()["n"]
        rel = conn.execute("SELECT COUNT(*) AS n FROM edges WHERE src_id=? AND edge_type='related_to' "
                           "AND status='active'", (syn["node_id"],)).fetchone()["n"]
    finally:
        conn.close()
    assert syn["status"] == "candidate"
    assert df == 2 and rel == 1  # two contributing claims + the topic link

    # One propose_synthesis review filed.
    pend = [json.loads(p.read_text()) for p in (tmp_path / "reviews" / "pending").glob("*.json")]
    assert sum(1 for i in pend if i["type"] == "propose_synthesis") == 1

    assert validate_frontmatter.main([str(tmp_path)]) == 0
    assert validate_graph.main([str(tmp_path)]) == 0
    assert validate_wikilinks.main([str(tmp_path)]) == 0
    assert validate_projection.main([str(tmp_path)]) == 0


def test_confidence_clamped(tmp_path):
    jobs = _build(tmp_path)
    _gen(tmp_path, SynthAdapter(confidence=9.0), jobs)
    page = _syn_page(tmp_path).read_text(encoding="utf-8")
    assert "confidence: 1.0" in page


def test_unchanged_topic_skips_on_rerun(tmp_path):
    jobs = _build(tmp_path)
    a1 = SynthAdapter()
    _gen(tmp_path, a1, jobs)
    assert a1.calls == 1
    a2 = SynthAdapter()
    summary = _gen(tmp_path, a2, jobs)
    assert a2.calls == 0 and summary["skipped_fresh"] == 1 and summary["syntheses_written"] == 0


def test_no_api_key_skips(tmp_path):
    jobs = _build(tmp_path)
    adapter = SynthAdapter(available=False)
    summary = _gen(tmp_path, adapter, jobs)
    assert summary["status"] == "skipped"
    assert summary["eligible_topics"] == 1 and summary["syntheses_written"] == 0
    assert adapter.calls == 0


# --- review-only promotion (no recurrence) ---------------------------------


def test_review_only_promotion(tmp_path):
    jobs = _build(tmp_path)
    _gen(tmp_path, SynthAdapter(), jobs)
    # Re-running does NOT auto-promote (no recurrence path), even though >=2 sources exist.
    _gen(tmp_path, SynthAdapter(), jobs)
    conn = _gconn(tmp_path)
    try:
        assert conn.execute("SELECT status FROM nodes WHERE node_type='synthesis'").fetchone()["status"] == "candidate"
    finally:
        conn.close()

    rid = next(json.loads(p.read_text())["review_id"]
               for p in (tmp_path / "reviews" / "pending").glob("*.json")
               if json.loads(p.read_text())["type"] == "propose_synthesis")
    reviews.resolve_review_item(tmp_path / "reviews", rid, decision="approved", decided_by="human")
    summary = _gen(tmp_path, SynthAdapter(), jobs)
    assert summary["promoted"] == 1
    conn = _gconn(tmp_path)
    try:
        assert conn.execute("SELECT status FROM nodes WHERE node_type='synthesis'").fetchone()["status"] == "active"
    finally:
        conn.close()
    assert "status: active" in _syn_page(tmp_path).read_text(encoding="utf-8")
    assert validate_projection.main([str(tmp_path)]) == 0


def test_rejection_deprecates(tmp_path):
    jobs = _build(tmp_path)
    _gen(tmp_path, SynthAdapter(), jobs)
    rid = next(json.loads(p.read_text())["review_id"]
               for p in (tmp_path / "reviews" / "pending").glob("*.json")
               if json.loads(p.read_text())["type"] == "propose_synthesis")
    reviews.resolve_review_item(tmp_path / "reviews", rid, decision="rejected", decided_by="human")
    _gen(tmp_path, SynthAdapter(), jobs)
    page = _syn_page(tmp_path).read_text(encoding="utf-8")
    assert "status: deprecated_candidate" in page and "review_status: rejected" in page
    conn = _gconn(tmp_path)
    try:
        assert conn.execute("SELECT status FROM nodes WHERE node_type='synthesis'").fetchone()["status"] == "deprecated_candidate"
    finally:
        conn.close()


# --- retraction ------------------------------------------------------------


def test_retracts_when_topic_no_longer_eligible(tmp_path):
    jobs = _build(tmp_path)
    _gen(tmp_path, SynthAdapter(), jobs)
    # Tombstone source B's claim with a fresh-cache empty adapter -> topic drops to 1 claim.
    fresh = LLMClient({"anthropic": _EmptyClaims()}, cache=ResponseCache(tmp_path / "db" / "fresh.sqlite"))
    claims.extract_claims(tmp_path, client=fresh, model_ref=TIER2, jobs_db=jobs,
                          source_ids=[_sid(tmp_path, "b.md")], force=True, rebuild_index=False)
    summary = _gen(tmp_path, SynthAdapter(), jobs)
    assert summary["retracted"] == 1
    conn = _gconn(tmp_path)
    try:
        assert conn.execute("SELECT status FROM nodes WHERE node_type='synthesis'").fetchone()["status"] == "deprecated_candidate"
    finally:
        conn.close()
    assert "status: deprecated_candidate" in _syn_page(tmp_path).read_text(encoding="utf-8")


class _EmptyClaims:
    name = "anthropic"
    supports_batch = False

    def available(self):
        return True

    def parse(self, messages, schema, model_id, *, max_tokens):
        return {"claims": []}


def _propose_rid(tmp_path):
    return next(json.loads(p.read_text())["review_id"]
                for p in (tmp_path / "reviews" / "pending").glob("*.json")
                if json.loads(p.read_text())["type"] == "propose_synthesis")


def _bump_a_claim(tmp_path, jobs):
    """Change source A's claim text so the topic fingerprint changes (fresh cache so it runs)."""
    class Other:
        name = "anthropic"
        supports_batch = False

        def available(self):
            return True

        def parse(self, messages, schema, model_id, *, max_tokens):
            body = messages[-1]["content"]
            if QUOTE_A in body:
                return {"claims": [{"claim": "Q3 revenue grew strongly.", "quote": QUOTE_A}]}
            return {"claims": [{"claim": "Q3 revenue rose, helped by new customers.", "quote": QUOTE_B}]}

    fresh = LLMClient({"anthropic": Other()}, cache=ResponseCache(tmp_path / "db" / "bump.sqlite"))
    claims.extract_claims(tmp_path, client=fresh, model_ref=TIER2, jobs_db=jobs,
                          source_ids=[_sid(tmp_path, "a.md")], force=True, rebuild_index=False)


# --- governance: reviewed syntheses are never silently rewritten -----------


def test_approved_synthesis_not_demoted_on_evidence_change(tmp_path):
    jobs = _build(tmp_path)
    _gen(tmp_path, SynthAdapter(), jobs)
    reviews.resolve_review_item(tmp_path / "reviews", _propose_rid(tmp_path),
                                decision="approved", decided_by="human")
    _gen(tmp_path, SynthAdapter(), jobs)  # -> active
    _bump_a_claim(tmp_path, jobs)         # contributing evidence changes -> fingerprint changes
    summary = _gen(tmp_path, SynthAdapter(), jobs)
    assert summary["stale_active"] == 1 and summary["syntheses_written"] == 0  # surfaced, not redone
    conn = _gconn(tmp_path)
    try:
        assert conn.execute("SELECT status FROM nodes WHERE node_type='synthesis'").fetchone()["status"] == "active"
    finally:
        conn.close()
    assert "status: active" in _syn_page(tmp_path).read_text(encoding="utf-8")


def test_force_reopens_stale_approved_with_fresh_pending(tmp_path):
    jobs = _build(tmp_path)
    _gen(tmp_path, SynthAdapter(), jobs)
    reviews.resolve_review_item(tmp_path / "reviews", _propose_rid(tmp_path),
                                decision="approved", decided_by="human")
    _gen(tmp_path, SynthAdapter(), jobs)
    _bump_a_claim(tmp_path, jobs)
    summary = _gen(tmp_path, SynthAdapter(), jobs, force=True)
    assert summary["syntheses_written"] == 1
    conn = _gconn(tmp_path)
    try:
        assert conn.execute("SELECT status FROM nodes WHERE node_type='synthesis'").fetchone()["status"] == "candidate"
    finally:
        conn.close()
    # A fresh pending propose_synthesis exists under the new fingerprint (re-fileable).
    pend = [json.loads(p.read_text()) for p in (tmp_path / "reviews" / "pending").glob("*.json")]
    assert sum(1 for i in pend if i["type"] == "propose_synthesis") == 1


def test_rejected_is_refileable_on_evidence_change(tmp_path):
    jobs = _build(tmp_path)
    _gen(tmp_path, SynthAdapter(), jobs)
    reviews.resolve_review_item(tmp_path / "reviews", _propose_rid(tmp_path),
                                decision="rejected", decided_by="human")
    s1 = _gen(tmp_path, SynthAdapter(), jobs)
    assert s1["skipped_reviewed"] == 1 and s1["syntheses_written"] == 0  # same evidence: no re-nag
    _bump_a_claim(tmp_path, jobs)                                        # evidence changes
    s2 = _gen(tmp_path, SynthAdapter(), jobs)
    assert s2["syntheses_written"] == 1                                  # new evidence: re-proposed
    pend = [json.loads(p.read_text()) for p in (tmp_path / "reviews" / "pending").glob("*.json")]
    assert sum(1 for i in pend if i["type"] == "propose_synthesis") == 1


def test_rejected_synthesis_honored_across_availability_flip(tmp_path):
    # ADR-0063 B3: a synthesis rejected while produced by a still-in-chain model must NOT re-nag when the
    # run later resolves to a DIFFERENT (also in-chain) model. The rejected-review lookup keys on the
    # RECORDED model's fingerprint when the artifact is chain-fresh, not the newly-resolved model's.
    jobs = _build(tmp_path)
    cache = ResponseCache(tmp_path / "db" / "llm_cache.sqlite")
    synthesis.generate_syntheses(  # produced by HEAVY (single chain -> recorded model_ref = HEAVY)
        tmp_path, client=LLMClient({"anthropic": SynthAdapter()}, cache=cache),
        model_ref=HEAVY, jobs_db=jobs, rebuild_index=False)
    reviews.resolve_review_item(tmp_path / "reviews", _propose_rid(tmp_path),
                                decision="rejected", decided_by="human")
    # Local now up; chain is local-first but HEAVY (which produced the rejected evidence) is still a
    # member -> sticky-fresh -> the rejection is honored, and the newly-resolved local model is NOT called.
    local = SynthAdapter()
    flip = LLMClient({"local": local, "anthropic": SynthAdapter()}, cache=cache)
    s = synthesis.generate_syntheses(
        tmp_path, client=flip, model_ref=f"local:qwen,{HEAVY}", jobs_db=jobs, rebuild_index=False)
    assert s["skipped_reviewed"] == 1 and s["syntheses_written"] == 0
    assert local.calls == 0   # no re-nag, no model call despite the resolved model changing


# --- slug collision across topic node types --------------------------------


def test_synthesis_pages_are_keyed_by_node_id_not_slug(tmp_path):
    # Two distinct eligible topics get two distinct `syn_<hash>.md` pages (keyed by topic node id,
    # not slug), so syntheses can never collide on a shared slug across topics (blocking 2).
    jobs = tmp_path / "db" / "jobs.sqlite"
    _ingest(tmp_path, "a.md", DOC_A)
    _ingest(tmp_path, "b.md", DOC_B)
    intake.scan_inbox(tmp_path, jobs_db=jobs)
    extract.extract_sources(tmp_path, jobs_db=jobs)
    wiki.generate_wiki(tmp_path, jobs_db=jobs, templates_dir=TEMPLATES, rebuild_index=False)
    manifests.set_provenance(tmp_path / "raw" / "manifests", _sid(tmp_path, "a.md"), author="Alice")
    manifests.set_provenance(tmp_path / "raw" / "manifests", _sid(tmp_path, "b.md"), author="Bob")
    claims.extract_claims(tmp_path, client=_client(tmp_path, ClaimAdapter()), model_ref=TIER2,
                          jobs_db=jobs, rebuild_index=False)

    class TwoItems:
        name = "anthropic"
        supports_batch = False

        def available(self):
            return True

        def parse(self, messages, schema, model_id, *, max_tokens):
            # Both sources mention both items -> each is evidenced by 2 independent claims.
            return {"items": [{"name": "Q3 revenue", "item_type": "method_technique", "aliases": []},
                              {"name": "customer demand", "item_type": "problem_risk", "aliases": []}]}

    items.extract_items(tmp_path, client=_client(tmp_path, TwoItems()), model_ref=TIER2,
                        jobs_db=jobs, rebuild_index=False)
    promote.promote_candidates(tmp_path, jobs_db=jobs, rebuild_index=False)
    wiki.generate_wiki(tmp_path, jobs_db=jobs, templates_dir=TEMPLATES, rebuild_index=False)
    _gen(tmp_path, SynthAdapter(), jobs)

    pages = list((tmp_path / "wiki" / "Synthesis").glob("*.md"))
    assert len(pages) == 2 and all(p.stem.startswith("syn_") for p in pages)  # distinct, node-keyed
    assert validate_projection.main([str(tmp_path)]) == 0


# --- eligibility re-check over surviving contexts --------------------------


def test_uncited_side_fails_eligibility(tmp_path):
    jobs = _build(tmp_path)
    # Remove source B's claim page so its context is missing -> only one grounded claim remains.
    conn = _gconn(tmp_path)
    try:
        sid_b = _sid(tmp_path, "b.md")
        cb = graph.claims_for_source(conn, sid_b)[0]
    finally:
        conn.close()
    (tmp_path / "wiki" / "Claims" / f"{cb}.md").unlink()
    summary = _gen(tmp_path, SynthAdapter(), jobs)
    assert summary["eligible_topics"] == 0 and summary["syntheses_written"] == 0


# --- audited retraction of an approved synthesis ---------------------------


def test_retract_approved_synthesis_is_coherent_and_audited(tmp_path):
    jobs = _build(tmp_path)
    _gen(tmp_path, SynthAdapter(), jobs)
    reviews.resolve_review_item(tmp_path / "reviews", _propose_rid(tmp_path),
                                decision="approved", decided_by="human")
    _gen(tmp_path, SynthAdapter(), jobs)  # active
    # Drop the topic below threshold by tombstoning source B's claim.
    fresh = LLMClient({"anthropic": _EmptyClaims()}, cache=ResponseCache(tmp_path / "db" / "f2.sqlite"))
    claims.extract_claims(tmp_path, client=fresh, model_ref=TIER2, jobs_db=jobs,
                          source_ids=[_sid(tmp_path, "b.md")], force=True, rebuild_index=False)
    summary = _gen(tmp_path, SynthAdapter(), jobs)
    assert summary["retracted"] == 1

    page = _syn_page(tmp_path).read_text(encoding="utf-8")
    assert "status: deprecated_candidate" in page
    assert "review_status: pending" in page  # coherent, not a stale `approved`
    conn = _gconn(tmp_path)
    try:
        assert conn.execute("SELECT status FROM nodes WHERE node_type='synthesis'").fetchone()["status"] == "deprecated_candidate"
    finally:
        conn.close()
    # A deprecate_wiki_page item records the retraction (audit trail).
    pend = [json.loads(p.read_text()) for p in (tmp_path / "reviews" / "pending").glob("*.json")]
    assert any(i["type"] == "deprecate_wiki_page" and i["context"].get("node_type") == "synthesis"
               for i in pend)


# --- frontmatter projection is machine-checked -----------------------------


def test_tampered_derived_from_frontmatter_fails_projection(tmp_path):
    jobs = _build(tmp_path)
    _gen(tmp_path, SynthAdapter(), jobs)
    page = _syn_page(tmp_path)
    text = page.read_text(encoding="utf-8")
    # Drop one claim id from the frontmatter derived_from list while body links stay correct.
    tampered = re.sub(r"(?m)^  - clm_[0-9a-f]+\n", "", text, count=1)
    page.write_text(tampered, encoding="utf-8")
    assert validate_projection.main([str(tmp_path)]) == 1


def test_lint_synthesis_rot_matches_producer_stale_active(tmp_path):
    # Real eligible_topics (no monkeypatch): lint's synthesis_rot must fire exactly when the producer
    # would report stale_active for an active synthesis whose evidence drifted (ADR-0037 decision 6).
    from app.workers import lint
    jobs = _build(tmp_path)
    _gen(tmp_path, SynthAdapter(), jobs)
    reviews.resolve_review_item(tmp_path / "reviews", _propose_rid(tmp_path),
                                decision="approved", decided_by="human")
    _gen(tmp_path, SynthAdapter(), jobs)   # approved -> active synthesis, artifact fingerprinted
    _bump_a_claim(tmp_path, jobs)          # contributing evidence changes -> topic fingerprint drifts
    res = lint.run_lint(tmp_path, graph_db=tmp_path / "db" / "graph.sqlite",
                        synthesis_model_ref=HEAVY, record_job=False)
    rot = [f for f in res["findings"] if f["check"] == "synthesis_rot"]
    assert len(rot) == 1 and rot[0]["data"]["remediation"] == "rerun_synthesis"
    assert res["status"] != "failing"  # low severity


def test_lint_synthesis_fresh_no_rot(tmp_path):
    from app.workers import lint
    jobs = _build(tmp_path)
    _gen(tmp_path, SynthAdapter(), jobs)
    reviews.resolve_review_item(tmp_path / "reviews", _propose_rid(tmp_path),
                                decision="approved", decided_by="human")
    _gen(tmp_path, SynthAdapter(), jobs)   # active + fresh (no evidence change)
    res = lint.run_lint(tmp_path, graph_db=tmp_path / "db" / "graph.sqlite",
                        synthesis_model_ref=HEAVY, record_job=False)
    assert not any(f["check"] == "synthesis_rot" for f in res["findings"])
