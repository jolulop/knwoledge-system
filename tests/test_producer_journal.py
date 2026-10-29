"""ADR-0068 slice 3: the producer commit journal (`db/producer_journal.json`).

A claims, items or synthesis commit records its subject, with the ids it may affect, before its first
write and clears it after its last. These tests interrupt commits at their riskiest points and check
that recovery and the next run finish them: superseding a document's relationships never loses the
claims and tags it retires, a forced re-extraction's matching artifact does not look fresh, a synthesis
proposal is never left unfiled, and publication retry never clears the record.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
import backup  # noqa: E402
import republish  # noqa: E402
import repair_producer_journal  # noqa: E402
import validate_producer_journal  # noqa: E402

from app.backend import graph, producer_journal, publication_queue, write_lock  # noqa: E402
from app.workers import claims, items, publication, reviews, synthesis, tag_links, wiki  # noqa: E402
from app.workers import enrichment_artifact as art  # noqa: E402
from tests import fixtures  # noqa: E402
from tests.test_claims import Q1, Q2  # noqa: E402
from tests.test_claims import FakeAdapter as ClaimsAdapter  # noqa: E402
from tests.test_producer_publication import (_claims_vault, _extract_claims,  # noqa: E402
                                             _extract_items, _extract_sources, _ingest,
                                             _search_current)
from tests.test_tag_links import RoutingAdapter, _item, _sids  # noqa: E402

C1 = {"claim": "The document has a first paragraph.", "quote": Q1}
C2 = {"claim": "The document has a section with a second paragraph.", "quote": Q2}


def _gconn(tmp_path):
    return graph.connect(tmp_path / "db" / "graph.sqlite")


def _status(tmp_path, node_id):
    gconn = _gconn(tmp_path)
    try:
        return (graph.get_node(gconn, node_id) or {}).get("status")
    finally:
        gconn.close()


def _records(tmp_path, producer):
    return producer_journal.pending(tmp_path, producer)


def _crash_after(monkeypatch, module, name, *, times=1):
    """Make `module.name` run, then raise KeyboardInterrupt (a process killed right after it), for
    its next `times` calls."""
    real = getattr(module, name)
    left = [times]

    def wrapped(*args, **kwargs):
        out = real(*args, **kwargs)
        if left[0] > 0:
            left[0] -= 1
            raise KeyboardInterrupt
        return out

    monkeypatch.setattr(module, name, wrapped)
    return real


# --- claims ---------------------------------------------------------------------------------------


def _forced_claims(tmp_path, payload, **kw):
    """A claims run whose model now answers `payload` (no response cache: the first answer is cached)."""
    from app.llm.client import LLMClient
    from tests.test_tag_links import MODEL_REF
    return claims.extract_claims(tmp_path, client=LLMClient({"anthropic": ClaimsAdapter(payload)}),
                                 model_ref=MODEL_REF, jobs_db=tmp_path / "db" / "jobs.sqlite", **kw)


def _items_now(tmp_path, routes, **kw):
    """An items run whose model now answers `routes` (no response cache)."""
    from app.llm.client import LLMClient
    from tests.test_tag_links import MODEL_REF
    return items.extract_items(tmp_path, client=LLMClient({"anthropic": RoutingAdapter(routes)}),
                               model_ref=MODEL_REF, jobs_db=tmp_path / "db" / "jobs.sqlite", **kw)


def test_an_interrupted_forced_re_extraction_with_fewer_claims_retires_the_lost_one(tmp_path,
                                                                                     monkeypatch):
    sid = _claims_vault(tmp_path)
    _extract_claims(tmp_path, ClaimsAdapter())
    c1, c2 = claims.claim_id(C1["claim"]), claims.claim_id(C2["claim"])
    artifact = art.claims_artifact_path(tmp_path / "normalized" / "enrichment", sid)
    before = artifact.read_text(encoding="utf-8")

    # A forced re-extraction that keeps only C1, killed right after superseding the evidence.
    real = _crash_after(monkeypatch, graph, "supersede_source_edges")
    with pytest.raises(KeyboardInterrupt):
        _forced_claims(tmp_path, [C1], force=True)
    monkeypatch.setattr(graph, "supersede_source_edges", real)
    assert set(_records(tmp_path, "extract_claims")[sid]["claims"]) == {c1, c2}
    assert artifact.read_text(encoding="utf-8") == before        # the old, still-matching artifact
    assert _status(tmp_path, c2) == "active"                      # not retired yet

    # Publication retry reports both claims (their evidence is gone) and never clears the record.
    assert republish.main([str(tmp_path)]) == 1
    left = publication_queue.outstanding(publication_queue.load(tmp_path))
    assert {c1, c2} <= set(left["claims"])
    assert sid in _records(tmp_path, "extract_claims")
    assert validate_producer_journal.main([str(tmp_path)]) == 1

    # The next ordinary run is not fooled by the matching artifact: it redoes the document.
    summary = _forced_claims(tmp_path, [C1])
    assert summary["skipped_fresh"] == 0 and summary["claims_written"] == 1
    assert _status(tmp_path, c1) == "active" and _status(tmp_path, c2) == "deprecated_candidate"
    assert summary["status"] == "succeeded" and summary["unfinished_commits"] == []
    assert _records(tmp_path, "extract_claims") == {}
    assert publication_queue.load(tmp_path) == [] and _search_current(tmp_path)
    assert validate_producer_journal.main([str(tmp_path)]) == 0


def test_the_original_recovery_ids_survive_two_interruptions(tmp_path, monkeypatch):
    sid = _claims_vault(tmp_path)
    _extract_claims(tmp_path, ClaimsAdapter())
    c1, c2 = claims.claim_id(C1["claim"]), claims.claim_id(C2["claim"])

    real = _crash_after(monkeypatch, graph, "supersede_source_edges")
    with pytest.raises(KeyboardInterrupt):
        _forced_claims(tmp_path, [C1], force=True)
    monkeypatch.setattr(graph, "supersede_source_edges", real)
    # The second attempt starts with no active claim on the document (the first superseded them) and
    # dies after re-asserting C1, before the artifact: the record must still hold C2.
    real_write = _crash_after(monkeypatch, claims, "_write_source_artifact")
    with pytest.raises(KeyboardInterrupt):
        _forced_claims(tmp_path, [C1])
    monkeypatch.setattr(claims, "_write_source_artifact", real_write)
    rec = _records(tmp_path, "extract_claims")[sid]
    assert set(rec["claims"]) == {c1, c2} and rec["attempts"] == 2

    summary = _forced_claims(tmp_path, [C1])
    assert summary["status"] == "succeeded"
    assert _status(tmp_path, c2) == "deprecated_candidate" and _status(tmp_path, c1) == "active"
    assert _records(tmp_path, "extract_claims") == {} and publication_queue.load(tmp_path) == []


def test_a_run_that_leaves_an_attempted_commit_unfinished_is_not_a_success(tmp_path, monkeypatch):
    sid = _claims_vault(tmp_path)
    _extract_claims(tmp_path, ClaimsAdapter())
    real = _crash_after(monkeypatch, graph, "supersede_source_edges")
    with pytest.raises(KeyboardInterrupt):
        _forced_claims(tmp_path, [C1], force=True)
    monkeypatch.setattr(graph, "supersede_source_edges", real)
    md = tmp_path / "normalized" / "markdown" / f"{sid}.md"

    class Reextracted(ClaimsAdapter):
        """The document is re-extracted while the model runs: the run writes nothing for it."""

        def parse(self, *args, **kwargs):
            md.write_text(md.read_text(encoding="utf-8") + "\nMore.\n", encoding="utf-8")
            return super().parse(*args, **kwargs)

    from app.llm.client import LLMClient
    from tests.test_tag_links import MODEL_REF
    summary = claims.extract_claims(
        tmp_path, client=LLMClient({"anthropic": Reextracted([C1])}), model_ref=MODEL_REF,
        jobs_db=tmp_path / "db" / "jobs.sqlite")
    assert summary["errors"] == 0 and summary["skipped_input_changed"] == 1
    assert summary["status"] == "partial" and summary["unfinished_commits"] == [sid]


def test_a_document_no_longer_extractable_finishes_its_commit_from_authority(tmp_path, monkeypatch):
    sid = _claims_vault(tmp_path)
    _extract_claims(tmp_path, ClaimsAdapter())
    c2 = claims.claim_id(C2["claim"])
    real = _crash_after(monkeypatch, graph, "supersede_source_edges")
    with pytest.raises(KeyboardInterrupt):
        _forced_claims(tmp_path, [C1], force=True)
    monkeypatch.setattr(graph, "supersede_source_edges", real)
    mpath = tmp_path / "raw" / "manifests" / f"{sid}.json"
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    mpath.write_text(json.dumps({**manifest, "ingestion_status": "needs_ocr"}), encoding="utf-8")

    summary = _forced_claims(tmp_path, [C1])
    assert summary["journal_reconciled"] == [sid] and summary["unfinished_commits"] == []
    assert _status(tmp_path, c2) == "deprecated_candidate"        # retired, never left active


# --- items ----------------------------------------------------------------------------------------


_TWO_TAGS = {"alpha": [_item("Alpha Tag"), _item("Beta Tag")]}


def test_an_interrupted_re_extraction_with_fewer_tags_retires_the_lost_one(tmp_path, monkeypatch):
    _ingest(tmp_path, {"a.md": "alpha body"})
    _extract_sources(tmp_path)
    a = _sids(tmp_path)["a.md"]
    _extract_items(tmp_path, RoutingAdapter(_TWO_TAGS))
    alpha, beta = items.node_id("Alpha Tag"), items.node_id("Beta Tag")

    real = _crash_after(monkeypatch, graph, "supersede_mentions_for_source")
    with pytest.raises(KeyboardInterrupt):
        _items_now(tmp_path, {"alpha": [_item("Alpha Tag")]}, force=True)
    monkeypatch.setattr(graph, "supersede_mentions_for_source", real)
    assert set(_records(tmp_path, "extract_items")[a]["items"]) == {alpha, beta}

    # Retry publishes what it can (tag pages from authority) and leaves the record to the producer.
    republish.main([str(tmp_path)])
    assert a in _records(tmp_path, "extract_items")
    assert _status(tmp_path, beta) == "candidate"                 # publication never retires it

    summary = _items_now(tmp_path, {"alpha": [_item("Alpha Tag")]})
    assert summary["skipped_fresh"] == 0 and summary["status"] == "succeeded"
    assert _status(tmp_path, beta) == "deprecated_candidate" and _status(tmp_path, alpha) == "candidate"
    assert _records(tmp_path, "extract_items") == {} and publication_queue.load(tmp_path) == []


def test_a_drop_between_the_interruption_and_the_replay_is_respected(tmp_path, monkeypatch):
    _ingest(tmp_path, {"a.md": "alpha body"})
    _extract_sources(tmp_path)
    a = _sids(tmp_path)["a.md"]
    _extract_items(tmp_path, RoutingAdapter(_TWO_TAGS))
    beta = items.node_id("Beta Tag")
    real = _crash_after(monkeypatch, items, "_write_artifact")
    with pytest.raises(KeyboardInterrupt):
        _extract_items(tmp_path, RoutingAdapter(_TWO_TAGS), force=True)
    monkeypatch.setattr(items, "_write_artifact", real)

    # A human drops Beta from the document before the next run.
    gconn = _gconn(tmp_path)
    try:
        with tag_links.ledger_lock(tmp_path / "reviews"):
            out = tag_links.drop(gconn, source_id=a, node_id=beta, wiki_dir=tmp_path / "wiki",
                                 reviews_dir=tmp_path / "reviews")
    finally:
        gconn.close()
    assert out["outcome"] == "dropped"

    summary = _extract_items(tmp_path, RoutingAdapter(_TWO_TAGS))
    assert summary["status"] == "succeeded" and summary["suggestions_filtered_dropped"] == 1
    gconn = _gconn(tmp_path)
    try:
        assert beta not in graph.item_ids_for_source(gconn, a)    # the replay kept the drop
    finally:
        gconn.close()
    assert _records(tmp_path, "extract_items") == {}


# --- synthesis ------------------------------------------------------------------------------------


def _synthesis_vault(tmp_path):
    import tests.test_synthesis as syn_t
    fixtures.copy_templates(tmp_path)
    return syn_t._build(tmp_path)


def _generate(tmp_path, jobs, adapter=None, **kw):
    import tests.test_synthesis as syn_t
    adapter = adapter or syn_t.SynthAdapter()
    return synthesis.generate_syntheses(tmp_path, client=syn_t._client(tmp_path, adapter),
                                        model_ref=syn_t.HEAVY, jobs_db=jobs, **kw), adapter


def _proposals(tmp_path, state="pending"):
    d = tmp_path / "reviews" / state
    return [json.loads(p.read_text()) for p in d.glob("*.json")
            if json.loads(p.read_text())["type"] == "propose_synthesis"] if d.exists() else []


def test_a_synthesis_crash_before_its_proposal_is_refiled_by_the_next_run(tmp_path, monkeypatch):
    jobs = _synthesis_vault(tmp_path)
    real = synthesis._withdraw_stale_pending                       # right after the page

    def crash(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(synthesis, "_withdraw_stale_pending", crash)
    with pytest.raises(KeyboardInterrupt):
        _generate(tmp_path, jobs)
    monkeypatch.setattr(synthesis, "_withdraw_stale_pending", real)
    (tid,) = _records(tmp_path, "generate_synthesis")
    assert _records(tmp_path, "generate_synthesis")[tid]["fingerprint"]
    assert list((tmp_path / "wiki" / "Synthesis").glob("*.md"))    # the page was written
    assert _proposals(tmp_path) == []
    assert not art.synthesis_artifact_path(tmp_path / "normalized" / "enrichment", tid).exists()

    republish.main([str(tmp_path)])                                # never clears the record
    assert tid in _records(tmp_path, "generate_synthesis")

    run, adapter = _generate(tmp_path, jobs)                       # no --force
    assert run["syntheses_written"] == 1 and run["status"] == "succeeded"
    assert adapter.calls == 0                                      # the response cache answered
    assert len(_proposals(tmp_path)) == 1
    assert _records(tmp_path, "generate_synthesis") == {} and publication_queue.load(tmp_path) == []


def test_a_fresh_synthesis_artifact_with_an_unfinished_record_is_redone(tmp_path):
    # The pre-fix layout (artifact first): a fresh artifact, but its proposal was never filed. A
    # record (here as a journal repair writes it, without a fingerprint) bypasses freshness.
    jobs = _synthesis_vault(tmp_path)
    _generate(tmp_path, jobs)
    (proposal,) = _proposals(tmp_path)
    (tmp_path / "reviews" / "pending" / f"{proposal['review_id']}.json").unlink()
    tid = proposal["subject"]["topic_node_id"]
    with write_lock.hold(tmp_path / "reviews"):
        producer_journal.begin(tmp_path, "generate_synthesis", tid)
    run, adapter = _generate(tmp_path, jobs)
    assert run["skipped_fresh"] == 0 and run["syntheses_written"] == 1 and adapter.calls == 0
    assert len(_proposals(tmp_path)) == 1 and _records(tmp_path, "generate_synthesis") == {}


def test_a_synthesis_commit_that_finished_before_clearing_its_record_is_verified(tmp_path,
                                                                                monkeypatch):
    jobs = _synthesis_vault(tmp_path)
    # Every write landed; the process dies just before clearing the record.
    monkeypatch.setattr(producer_journal, "finish",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        _generate(tmp_path, jobs)
    monkeypatch.undo()
    ((tid, rec),) = _records(tmp_path, "generate_synthesis").items()
    meta = json.loads(art.synthesis_artifact_path(tmp_path / "normalized" / "enrichment", tid)
                      .read_text(encoding="utf-8"))
    assert meta["commit_attempt"] == rec["attempt"]                # this attempt's stamp
    run, adapter = _generate(tmp_path, jobs)
    assert run["journal_verified"] == [tid] and run["skipped_fresh"] == 1 and adapter.calls == 0
    assert run["status"] == "succeeded" and _records(tmp_path, "generate_synthesis") == {}


def test_an_approval_between_the_interruption_and_the_replay_is_never_overwritten(tmp_path,
                                                                                  monkeypatch):
    jobs = _synthesis_vault(tmp_path)
    _generate(tmp_path, jobs)
    (proposal,) = _proposals(tmp_path)
    # A forced regeneration of the same evidence dies after rewriting the page.
    monkeypatch.setattr(synthesis, "_withdraw_stale_pending",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        _generate(tmp_path, jobs, force=True)
    monkeypatch.undo()
    (tid,) = _records(tmp_path, "generate_synthesis")
    # A human approves the proposal before the next run.
    reviews.resolve_review_item(tmp_path / "reviews", proposal["review_id"], decision="approved",
                                decided_by="human")
    run, adapter = _generate(tmp_path, jobs)
    assert run["promoted"] == 1 and adapter.calls == 0             # approved prose, not regenerated
    assert run["journal_verified"] == [tid] and _records(tmp_path, "generate_synthesis") == {}
    assert _status(tmp_path, synthesis.synthesis_id(tid)) == "active"


def test_a_human_decision_that_prevents_the_replay_reports_the_topic_blocked(tmp_path):
    jobs = _synthesis_vault(tmp_path)
    _generate(tmp_path, jobs)
    (proposal,) = _proposals(tmp_path)
    reviews.resolve_review_item(tmp_path / "reviews", proposal["review_id"], decision="approved",
                                decided_by="human")
    _generate(tmp_path, jobs)                                      # promoted: active
    tid = proposal["subject"]["topic_node_id"]
    # An unfinished commit for different evidence than the approved synthesis's.
    with write_lock.hold(tmp_path / "reviews"):
        producer_journal.begin(tmp_path, "generate_synthesis", tid, fingerprint="ab" * 32)
    run, adapter = _generate(tmp_path, jobs)
    assert run["unfinished_blocked"] == [tid] and run["status"] == "partial"
    assert adapter.calls == 0 and _status(tmp_path, synthesis.synthesis_id(tid)) == "active"
    assert tid in _records(tmp_path, "generate_synthesis")


def _active_edges(tmp_path, syn_id):
    gconn = _gconn(tmp_path)
    try:
        return {(e["edge_type"], e["dst_id"]) for e in graph.outgoing_active(gconn, syn_id)}
    finally:
        gconn.close()


def test_a_forced_regeneration_interrupted_after_superseding_is_never_verified_by_old_files(
        tmp_path, monkeypatch):
    jobs = _synthesis_vault(tmp_path)
    _generate(tmp_path, jobs)                                      # a complete candidate
    (proposal,) = _proposals(tmp_path)
    tid = proposal["subject"]["topic_node_id"]
    syn_id = synthesis.synthesis_id(tid)
    complete = _active_edges(tmp_path, syn_id)
    # A forced regeneration of the same evidence dies right after superseding the synthesis edges:
    # the old artifact (same fingerprint), node, page and proposal all still exist.
    monkeypatch.setattr(graph, "upsert_assertion",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        _generate(tmp_path, jobs, force=True)
    monkeypatch.undo()
    assert _active_edges(tmp_path, syn_id) == set()
    assert tid in _records(tmp_path, "generate_synthesis")

    run, _ = _generate(tmp_path, jobs)                             # an ordinary run
    assert run["journal_verified"] == [] and run["skipped_fresh"] == 0
    assert run["syntheses_written"] == 1 and run["status"] == "succeeded"
    assert _active_edges(tmp_path, syn_id) == complete
    assert _records(tmp_path, "generate_synthesis") == {}


def test_a_stamp_from_an_earlier_finished_attempt_never_verifies_a_later_one(tmp_path,
                                                                            monkeypatch):
    jobs = _synthesis_vault(tmp_path)
    # Attempt 1 finishes every write (its artifact carries its stamp) but dies before clearing.
    monkeypatch.setattr(producer_journal, "finish",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        _generate(tmp_path, jobs)
    monkeypatch.undo()
    (tid,) = _records(tmp_path, "generate_synthesis")
    syn_id = synthesis.synthesis_id(tid)
    complete = _active_edges(tmp_path, syn_id)
    # Attempt 2 (forced) dies right after superseding the edges.
    monkeypatch.setattr(graph, "upsert_assertion",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        _generate(tmp_path, jobs, force=True)
    monkeypatch.undo()
    run, _ = _generate(tmp_path, jobs)
    assert run["journal_verified"] == [] and run["syntheses_written"] == 1
    assert _active_edges(tmp_path, syn_id) == complete and _records(tmp_path, "generate_synthesis") == {}


def test_a_page_with_unreviewed_new_prose_is_never_verified(tmp_path, monkeypatch):
    import tests.test_synthesis as syn_t
    from app.llm.client import LLMClient
    jobs = _synthesis_vault(tmp_path)
    _generate(tmp_path, jobs)
    (proposal,) = _proposals(tmp_path)
    tid = proposal["subject"]["topic_node_id"]

    class Rewritten(syn_t.SynthAdapter):
        def parse(self, *args, **kwargs):
            return {**super().parse(*args, **kwargs), "summary": "A rewritten summary."}

    # A forced regeneration (new prose, same evidence) dies after the page, before the proposal.
    monkeypatch.setattr(synthesis, "_withdraw_stale_pending",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        synthesis.generate_syntheses(tmp_path, client=LLMClient({"anthropic": Rewritten()}),
                                     model_ref=syn_t.HEAVY, jobs_db=jobs, force=True)
    monkeypatch.undo()
    page = tmp_path / "wiki" / "Synthesis" / f"{synthesis.synthesis_id(tid)}.md"
    assert "A rewritten summary." in page.read_text(encoding="utf-8")
    run, _ = _generate(tmp_path, jobs)
    assert run["journal_verified"] == [] and run["syntheses_written"] == 1
    assert "A rewritten summary." not in page.read_text(encoding="utf-8")
    assert _records(tmp_path, "generate_synthesis") == {}


def test_a_page_with_a_rewritten_body_under_the_same_summary_is_never_verified(tmp_path,
                                                                                monkeypatch):
    import tests.test_synthesis as syn_t
    from app.llm.client import LLMClient
    jobs = _synthesis_vault(tmp_path)
    _generate(tmp_path, jobs)
    (proposal,) = _proposals(tmp_path)
    tid = proposal["subject"]["topic_node_id"]

    class Rewritten(syn_t.SynthAdapter):
        def parse(self, *args, **kwargs):
            return {**super().parse(*args, **kwargs), "synthesis": "A rewritten synthesis body."}

    # A forced regeneration (same evidence, same summary, new body) dies after re-filing the
    # proposal, before writing the artifact: the old artifact and the proposal still match.
    _crash_after(monkeypatch, reviews, "create_review_item")
    with pytest.raises(KeyboardInterrupt):
        synthesis.generate_syntheses(tmp_path, client=LLMClient({"anthropic": Rewritten()}),
                                     model_ref=syn_t.HEAVY, jobs_db=jobs, force=True)
    monkeypatch.undo()
    page = tmp_path / "wiki" / "Synthesis" / f"{synthesis.synthesis_id(tid)}.md"
    assert "A rewritten synthesis body." in page.read_text(encoding="utf-8")
    assert syn_t.SynthAdapter().parse(None, None, None, max_tokens=0)["summary"] in page.read_text(
        encoding="utf-8")                                          # the summary is unchanged
    run, _ = _generate(tmp_path, jobs)
    assert run["journal_verified"] == [] and run["syntheses_written"] == 1
    assert "A rewritten synthesis body." not in page.read_text(encoding="utf-8")
    assert _records(tmp_path, "generate_synthesis") == {}


def test_a_repaired_record_of_a_complete_synthesis_is_verified_by_its_exact_page(tmp_path):
    jobs = _synthesis_vault(tmp_path)
    _generate(tmp_path, jobs)
    (proposal,) = _proposals(tmp_path)
    tid = proposal["subject"]["topic_node_id"]
    producer_journal.journal_path(tmp_path).write_text("{", encoding="utf-8")    # damaged
    assert repair_producer_journal.main([str(tmp_path)]) == 0
    assert _records(tmp_path, "generate_synthesis")[tid]["attempt"] is None      # no stamp to match
    run, adapter = _generate(tmp_path, jobs)
    assert run["journal_verified"] == [tid] and adapter.calls == 0
    assert run["syntheses_written"] == 0 and _records(tmp_path, "generate_synthesis") == {}


def test_a_forced_run_with_an_unfinished_record_still_respects_a_hide(tmp_path):
    jobs = _synthesis_vault(tmp_path)
    _generate(tmp_path, jobs)
    (proposal,) = _proposals(tmp_path)
    reviews.resolve_review_item(tmp_path / "reviews", proposal["review_id"], decision="approved",
                                decided_by="human")
    _generate(tmp_path, jobs)                                      # promoted: active
    tid = proposal["subject"]["topic_node_id"]
    syn_id = synthesis.synthesis_id(tid)
    approved = tmp_path / "reviews" / "approved"
    (approved / "rev_hide.json").write_text(json.dumps({
        "review_id": "rev_hide", "type": "hide_synthesis", "status": "approved",
        "subject": {"node_id": syn_id, "page": f"Synthesis/{syn_id}.md"},
        "proposal": {"to_status": "hidden"}, "context": {"node_type": "synthesis"}}),
        encoding="utf-8")
    gconn = _gconn(tmp_path)
    try:
        with write_lock.hold(tmp_path / "reviews"):
            synthesis.apply_hidden_syntheses(gconn, tmp_path / "reviews",
                                             synthesis_dir=tmp_path / "wiki" / "Synthesis",
                                             enrichment_dir=tmp_path / "normalized" / "enrichment")
        gconn.commit()
    finally:
        gconn.close()
    page = tmp_path / "wiki" / "Synthesis" / f"{syn_id}.md"
    before = page.read_text(encoding="utf-8")
    with write_lock.hold(tmp_path / "reviews"):
        producer_journal.begin(tmp_path, "generate_synthesis", tid, fingerprint="cd" * 32)
    run, _ = _generate(tmp_path, jobs, force=True)
    assert run["syntheses_written"] == 0 and run["unfinished_blocked"] == [tid]
    assert page.read_text(encoding="utf-8") == before and _status(tmp_path, syn_id) == "hidden"


# --- generate_wiki --------------------------------------------------------------------------------


def test_generate_wiki_re_records_its_refresh_after_another_publication_cleared_it(tmp_path,
                                                                                   monkeypatch):
    _ingest(tmp_path, {"a.md": "alpha", "b.md": "bravo"})
    _extract_sources(tmp_path)
    real_valid = wiki.valid_manifests

    class _Between(list):
        """Another screen's publication runs between the two documents (outside the lock)."""

        def __iter__(self):
            for i, m in enumerate(list.__iter__(self)):
                if i == 1:
                    assert not write_lock.held()
                    assert publication.publish(tmp_path, origin="republish")["status"] == "published"
                    assert publication_queue.load(tmp_path) == []      # the run's marker is gone
                yield m

    monkeypatch.setattr(wiki, "valid_manifests",
                        lambda d: (lambda ms, bad: (_Between(ms), bad))(*real_valid(d)))
    # The second page is written, then the process dies before acknowledging it.
    real_done = publication.Run.done
    calls = []

    def done(self, entry_id):
        calls.append(entry_id)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real_done(self, entry_id)

    monkeypatch.setattr(publication.Run, "done", done)
    with pytest.raises(KeyboardInterrupt):
        wiki.generate_wiki(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite", force=True)
    monkeypatch.undo()
    left = publication_queue.outstanding(publication_queue.load(tmp_path))
    assert left["index"] and left["keyword"] and left["runs"] == ["generate_wiki"]
    assert len(left["sources"]) == 1                               # the page written last
    assert republish.main([str(tmp_path)]) == 0
    assert publication_queue.load(tmp_path) == [] and _search_current(tmp_path)


def test_generate_wiki_renders_each_page_once(tmp_path, monkeypatch):
    from app.workers import wiki_render
    _ingest(tmp_path, {"a.md": "alpha", "b.md": "bravo"})
    _extract_sources(tmp_path)
    real = wiki_render.render_source_page
    renders = []
    monkeypatch.setattr(wiki_render, "render_source_page",
                        lambda t, m, *a, **k: renders.append(m["source_id"]) or real(t, m, *a, **k))
    summary = wiki.generate_wiki(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite", force=True)
    assert summary["generated"] == 2 and sorted(renders) == sorted(_sids(tmp_path).values())
    assert publication_queue.load(tmp_path) == []


# --- the journal file -----------------------------------------------------------------------------


def test_a_damaged_journal_refuses_the_producers_until_repaired(tmp_path):
    sid = _claims_vault(tmp_path)
    _extract_claims(tmp_path, ClaimsAdapter())
    path = producer_journal.journal_path(tmp_path)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(producer_journal.JournalCorrupt):
        _extract_claims(tmp_path, ClaimsAdapter())
    assert validate_producer_journal.main([str(tmp_path)]) == 1

    assert repair_producer_journal.main([str(tmp_path)]) == 0
    assert list(path.parent.glob("producer_journal.damaged-*.json"))   # the damaged bytes kept
    assert sid in _records(tmp_path, "extract_claims")             # over-approximated, never empty
    summary = _extract_claims(tmp_path, ClaimsAdapter())
    assert summary["skipped_fresh"] == 0 and summary["status"] == "succeeded"
    assert _records(tmp_path, "extract_claims") == {}


def test_repair_keeps_interrupted_first_commits_that_have_no_artifact_yet(tmp_path, monkeypatch):
    sid = _claims_vault(tmp_path)
    enrichment = tmp_path / "normalized" / "enrichment"
    # The first claims commit dies before writing any relationship (only the manifest knows it).
    real = _crash_after(monkeypatch, graph, "supersede_source_edges")
    with pytest.raises(KeyboardInterrupt):
        _extract_claims(tmp_path, ClaimsAdapter())
    monkeypatch.setattr(graph, "supersede_source_edges", real)
    monkeypatch.setattr(items, "_write_artifact",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        _items_now(tmp_path, {"First paragraph": [_item("Alpha Tag")]})   # the first items commit
    monkeypatch.undo()
    assert not art.claims_artifact_path(enrichment, sid).exists()
    assert not art.items_artifact_path(enrichment, sid).exists()

    producer_journal.journal_path(tmp_path).write_text("[]", encoding="utf-8")   # damaged
    assert repair_producer_journal.main([str(tmp_path)]) == 0
    assert sid in _records(tmp_path, "extract_claims") and sid in _records(tmp_path, "extract_items")

    assert _extract_claims(tmp_path, ClaimsAdapter())["status"] == "succeeded"
    assert _items_now(tmp_path, {"First paragraph": [_item("Alpha Tag")]})["status"] == "succeeded"
    assert art.claims_artifact_path(enrichment, sid).exists()
    assert art.items_artifact_path(enrichment, sid).exists()
    assert _records(tmp_path, "extract_claims") == {} and _records(tmp_path, "extract_items") == {}


def test_repair_finds_a_document_by_its_relationships_and_keeps_it_until_pages_exist(tmp_path,
                                                                                   monkeypatch):
    sid = _claims_vault(tmp_path)
    # The first claims commit dies after its relationships, before any claim page was written...
    monkeypatch.setattr(claims, "recompose_claim",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        _extract_claims(tmp_path, ClaimsAdapter())
    monkeypatch.undo()
    # ...and the document is no longer extractable, so only the graph still points at it.
    mpath = tmp_path / "raw" / "manifests" / f"{sid}.json"
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    mpath.write_text(json.dumps({**manifest, "ingestion_status": "needs_ocr"}), encoding="utf-8")
    producer_journal.journal_path(tmp_path).write_text("{", encoding="utf-8")    # damaged
    assert repair_producer_journal.main([str(tmp_path)]) == 0
    assert sid in _records(tmp_path, "extract_claims")

    # Its claims can't be rendered (their wording was never written): never cleared as finished.
    summary = _extract_claims(tmp_path, ClaimsAdapter())
    assert summary["journal_reconciled"] == [] and summary["unfinished_commits"] == [sid]


def test_repair_keeps_an_interrupted_first_synthesis_and_the_next_run_files_it(tmp_path,
                                                                              monkeypatch):
    jobs = _synthesis_vault(tmp_path)
    monkeypatch.setattr(synthesis, "_withdraw_stale_pending",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        _generate(tmp_path, jobs)                                  # page written, no artifact
    monkeypatch.undo()
    (tid,) = _records(tmp_path, "generate_synthesis")
    assert not art.synthesis_artifact_path(tmp_path / "normalized" / "enrichment", tid).exists()

    producer_journal.journal_path(tmp_path).write_text("{", encoding="utf-8")    # damaged
    assert repair_producer_journal.main([str(tmp_path)]) == 0
    assert tid in _records(tmp_path, "generate_synthesis")
    run, _ = _generate(tmp_path, jobs)
    assert run["syntheses_written"] == 1 and run["status"] == "succeeded"
    assert len(_proposals(tmp_path)) == 1
    # Every other plausible topic it recorded never reached the graph: verified and cleared.
    assert _records(tmp_path, "generate_synthesis") == {}


@pytest.mark.parametrize("body", [
    {"schema_version": 2, "records": []},
    {"schema_version": 1, "records": [{"producer": "extract_claims", "subject": "../x",
                                       "claims": [], "items": [], "fingerprint": None,
                                       "started_at": "t", "attempts": 1}]},
    {"schema_version": 1, "records": [{"producer": "nope", "subject": "x", "claims": [],
                                       "items": [], "fingerprint": None, "started_at": "t",
                                       "attempts": 1}]},
])
def test_an_invalid_journal_is_corrupt_never_empty(tmp_path, body):
    path = producer_journal.journal_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(producer_journal.JournalCorrupt):
        producer_journal.load(tmp_path)
    assert producer_journal.status(tmp_path)["state"] == "corrupt"


def test_the_journal_is_written_under_the_lock_and_backed_up_with_db():
    with pytest.raises(RuntimeError):
        producer_journal.begin(Path("/nonexistent"), "extract_claims", "src_" + "0" * 16)
    assert "db" in backup.INCLUDE_DIRS


def test_the_api_refuses_with_a_503_that_names_the_repair():
    import asyncio

    from starlette.requests import Request

    from app.backend import main as main_module
    handler = main_module.app.exception_handlers[producer_journal.JournalCorrupt]
    request = Request({"type": "http", "method": "POST", "path": "/reviews/apply", "headers": [],
                       "query_string": b""})
    response = asyncio.run(handler(request, producer_journal.JournalCorrupt("unparseable")))
    assert response.status_code == 503
    assert b"repair_producer_journal.py" in response.body
