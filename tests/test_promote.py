from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import validate_projection  # noqa: E402

from app.backend import db, graph, manifests
from app.llm.cache import ResponseCache
from app.llm.client import LLMClient
from app.workers import concepts, extract, intake, promote, reviews
from app.workers.reviews import review_id
from app.workers.wiki_render import parse_frontmatter

MODEL = "anthropic:claude-sonnet-4-6"
NAME = "Shared Concept"
SLUG = "shared-concept"


class FakeAdapter:
    name = "anthropic"
    supports_batch = False

    def available(self):
        return True

    def parse(self, messages, schema, model_id, *, max_tokens):
        return {"concepts": [{"name": NAME, "aliases": []}], "entities": []}


def _setup(tmp_path, contents):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    for fname, content in contents.items():
        (inbox / fname).write_text(content, encoding="utf-8")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    extract.extract_sources(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    client = LLMClient({"anthropic": FakeAdapter()}, cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite"))
    concepts.extract_concepts(tmp_path, client=client, model_ref=MODEL,
                              jobs_db=tmp_path / "db" / "jobs.sqlite", rebuild_index=False)
    return {m["original_filename"]: m["source_id"] for m in manifests.list_manifests(tmp_path / "raw" / "manifests")}


def _node_status(tmp_path):
    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        row = conn.execute("SELECT status FROM nodes WHERE node_id = ?",
                            (concepts.node_id("concept", NAME),)).fetchone()
        return row["status"] if row else None
    finally:
        conn.close()


def _promote(tmp_path):
    return promote.promote_candidates(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite", rebuild_index=False)


_TWO = {"a.md": "# A\n\nAlpha body sentence.\n", "b.md": "# B\n\nBeta body sentence.\n"}


def test_no_provenance_no_promotion(tmp_path):
    _setup(tmp_path, _TWO)
    summary = _promote(tmp_path)
    assert summary["candidates_considered"] == 1 and summary["promoted"] == 0
    assert _node_status(tmp_path) == "candidate"  # conservative gate: null provenance


def test_single_source_not_promoted(tmp_path):
    sids = _setup(tmp_path, {"a.md": "# A\n\nAlpha body.\n"})
    manifests.set_provenance(tmp_path / "raw" / "manifests", sids["a.md"], author="Alice")
    assert _promote(tmp_path)["promoted"] == 0
    assert _node_status(tmp_path) == "candidate"


def test_shared_author_not_independent(tmp_path):
    sids = _setup(tmp_path, _TWO)
    md = tmp_path / "raw" / "manifests"
    manifests.set_provenance(md, sids["a.md"], author="Alice")
    manifests.set_provenance(md, sids["b.md"], author="Alice")  # same author -> not independent
    assert _promote(tmp_path)["promoted"] == 0
    assert _node_status(tmp_path) == "candidate"


def test_non_overlapping_keys_not_independent(tmp_path):
    sids = _setup(tmp_path, _TWO)
    md = tmp_path / "raw" / "manifests"
    manifests.set_provenance(md, sids["a.md"], author="Alice")     # only author
    manifests.set_provenance(md, sids["b.md"], publisher="Acme")   # only publisher -> no comparable key
    assert _promote(tmp_path)["promoted"] == 0
    assert _node_status(tmp_path) == "candidate"


def test_two_independent_sources_promote_and_resolve_review(tmp_path):
    sids = _setup(tmp_path, _TWO)
    md = tmp_path / "raw" / "manifests"
    manifests.set_provenance(md, sids["a.md"], author="Alice")
    manifests.set_provenance(md, sids["b.md"], author="Bob")  # comparable + distinct -> independent
    summary = _promote(tmp_path)
    assert summary["promoted"] == 1 and _node_status(tmp_path) == "active"

    fm = parse_frontmatter((tmp_path / "wiki" / "Concepts" / f"{SLUG}.md").read_text(encoding="utf-8"))
    assert fm["status"] == "active"  # page is the status authority

    rid = review_id("promote_candidate_node", {"node_id": concepts.node_id("concept", NAME)})
    assert (tmp_path / "reviews" / "approved" / f"{rid}.json").exists()
    assert not (tmp_path / "reviews" / "pending" / f"{rid}.json").exists()
    assert list((tmp_path / "reviews" / "audit_log").glob("*.json"))


def test_promotion_is_idempotent(tmp_path):
    sids = _setup(tmp_path, _TWO)
    md = tmp_path / "raw" / "manifests"
    manifests.set_provenance(md, sids["a.md"], author="Alice")
    manifests.set_provenance(md, sids["b.md"], author="Bob")
    assert _promote(tmp_path)["promoted"] == 1
    audit_after_first = list((tmp_path / "reviews" / "audit_log").glob("*.json"))

    second = _promote(tmp_path)
    assert second["promoted"] == 0 and second["candidates_considered"] == 0  # already active
    assert _node_status(tmp_path) == "active"
    assert list((tmp_path / "reviews" / "audit_log").glob("*.json")) == audit_after_first  # no dup audit


def test_independence_canonicalization():
    # Whitespace/case variants of the same author are NOT independent (conservative gate).
    assert promote._independent({"author": "Alice"}, {"author": "alice "}) is False
    # URL trailing-slash / case variants collapse too.
    assert promote._independent({"canonical_url": "http://X.com/a/"},
                                {"canonical_url": "http://x.com/a"}) is False
    # Genuinely distinct values are independent.
    assert promote._independent({"author": "Alice"}, {"author": "Bob"}) is True


def test_canonicalization_blocks_false_promotion(tmp_path):
    sids = _setup(tmp_path, _TWO)
    md = tmp_path / "raw" / "manifests"
    manifests.set_provenance(md, sids["a.md"], author="Alice")
    manifests.set_provenance(md, sids["b.md"], author="  alice")  # variant of the same author
    assert _promote(tmp_path)["promoted"] == 0
    assert _node_status(tmp_path) == "candidate"


def test_recurrence_closes_loop_even_if_pending_item_missing(tmp_path):
    sids = _setup(tmp_path, _TWO)
    md = tmp_path / "raw" / "manifests"
    manifests.set_provenance(md, sids["a.md"], author="Alice")
    manifests.set_provenance(md, sids["b.md"], author="Bob")
    rid = review_id("promote_candidate_node", {"node_id": concepts.node_id("concept", NAME)})
    (tmp_path / "reviews" / "pending" / f"{rid}.json").unlink()  # pending item went missing

    assert _promote(tmp_path)["promoted"] == 1 and _node_status(tmp_path) == "active"
    assert (tmp_path / "reviews" / "approved" / f"{rid}.json").exists()  # loop still closed
    assert list((tmp_path / "reviews" / "audit_log").glob("*.json"))


def test_approved_review_promotes_single_source(tmp_path):
    # Human early promotion (ADR-0018): a single-source candidate whose review item is
    # already approved promotes, with no recurrence.
    _setup(tmp_path, {"a.md": "# A\n\nAlpha body.\n"})  # one source, no provenance
    rid = review_id("promote_candidate_node", {"node_id": concepts.node_id("concept", NAME)})
    reviews.resolve_review_item(tmp_path / "reviews", rid, decision="approved", decided_by="human")

    summary = _promote(tmp_path)
    assert summary["promoted"] == 1 and summary["promoted_by_review"] == 1
    assert _node_status(tmp_path) == "active"


def test_clearing_provenance_blocks_promotion(tmp_path):
    sids = _setup(tmp_path, _TWO)
    md = tmp_path / "raw" / "manifests"
    manifests.set_provenance(md, sids["a.md"], author="Alice")
    manifests.set_provenance(md, sids["b.md"], author="Bob")
    manifests.set_provenance(md, sids["b.md"], author=None)  # correct away the bad provenance
    assert _promote(tmp_path)["promoted"] == 0
    assert _node_status(tmp_path) == "candidate"


def test_promote_job_failed_on_exception(tmp_path, monkeypatch):
    sids = _setup(tmp_path, _TWO)
    manifests.set_provenance(tmp_path / "raw" / "manifests", sids["a.md"], author="Alice")

    def boom(*a, **k):
        raise RuntimeError("graph blew up")
    monkeypatch.setattr(promote.graph, "sources_for_node", boom)
    with pytest.raises(RuntimeError):
        _promote(tmp_path)
    conn = db.connect(tmp_path / "db" / "jobs.sqlite")
    try:
        jobs = [j for j in db.list_jobs(conn) if j["job_type"] == "promote"]
    finally:
        conn.close()
    assert jobs and jobs[0]["status"] == "failed"


def test_status_mirror_validator_catches_drift(tmp_path):
    sids = _setup(tmp_path, _TWO)
    md = tmp_path / "raw" / "manifests"
    manifests.set_provenance(md, sids["a.md"], author="Alice")
    manifests.set_provenance(md, sids["b.md"], author="Bob")
    assert validate_projection.main([str(tmp_path)]) == 0  # consistent before tampering
    # Flip the page status to active while the graph node stays candidate -> drift.
    page = tmp_path / "wiki" / "Concepts" / f"{SLUG}.md"
    page.write_text(page.read_text(encoding="utf-8").replace("status: candidate", "status: active"),
                    encoding="utf-8")
    assert validate_projection.main([str(tmp_path)]) == 1


def test_promoted_status_survives_reextraction(tmp_path):
    sids = _setup(tmp_path, _TWO)
    md = tmp_path / "raw" / "manifests"
    manifests.set_provenance(md, sids["a.md"], author="Alice")
    manifests.set_provenance(md, sids["b.md"], author="Bob")
    _promote(tmp_path)
    assert _node_status(tmp_path) == "active"
    # Re-extract concepts (force): the promoted status must be preserved, not reset to candidate.
    client = LLMClient({"anthropic": FakeAdapter()}, cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite"))
    concepts.extract_concepts(tmp_path, client=client, model_ref=MODEL,
                              jobs_db=tmp_path / "db" / "jobs.sqlite", force=True, rebuild_index=False)
    assert _node_status(tmp_path) == "active"
    fm = parse_frontmatter((tmp_path / "wiki" / "Concepts" / f"{SLUG}.md").read_text(encoding="utf-8"))
    assert fm["status"] == "active"
