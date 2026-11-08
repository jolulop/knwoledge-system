"""ADR-0072 decisions 2-3: per-review Apply outcomes from the synthesis and contradiction executors.

`apply_resolved_syntheses` (propose_synthesis, approved AND rejected), `apply_hidden_syntheses` /
`apply_unhidden_syntheses` (hide_synthesis / unhide_synthesis) and `apply_contradiction_decisions`
(resolve_contradiction, approved AND rejected) each report exactly one outcome per decided review they
read, counts consistent with the outcomes, `already_in_effect` only after a verified postcondition,
and display names read before the executor changes anything (the id when a name can't be read).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph
from app.workers import apply_outcomes, contradictions, reviews, synthesis
from tests import test_contradictions as tc
from tests import test_synthesis_visibility as sv

SYN, TOPIC = sv.SYN, sv.TOPIC


def _write_review(tmp_path, state, item):
    d = tmp_path / "reviews" / state
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{item['review_id']}.json").write_text(json.dumps(item), encoding="utf-8")


def _by_id(res):
    out = {}
    for rec in res["outcomes"]:
        assert rec["review_id"] not in out, f"two outcomes for {rec['review_id']}"
        out[rec["review_id"]] = rec
    return out


def _set_artifact_title(tmp_path, title):
    p = tmp_path / "normalized" / "enrichment" / f"{TOPIC}.synthesis.json"
    a = json.loads(p.read_text(encoding="utf-8"))
    p.write_text(json.dumps(a | {"title": title}), encoding="utf-8")


def _page_text(tmp_path):
    return (tmp_path / "wiki" / "Synthesis" / f"{SYN}.md").read_text(encoding="utf-8")


def _set_page_status(tmp_path, status):
    p = tmp_path / "wiki" / "Synthesis" / f"{SYN}.md"
    text = p.read_text(encoding="utf-8")
    old = f"status: {sv._syn_fm(tmp_path)['status']}\n"
    assert old in text
    p.write_text(text.replace(old, f"status: {status}\n", 1), encoding="utf-8")


# --- propose_synthesis ------------------------------------------------------


def _propose(tmp_path, state, rid, topic=TOPIC):
    """A decision bound to the fixture artifact's current version (ADR-0073), when there is one."""
    item = {"review_id": rid, "type": "propose_synthesis", "status": state,
            "subject": {"topic_node_id": topic, "fingerprint": sv.FP} if topic else {}}
    from app.workers import synthesis_version
    version = synthesis_version.read(tmp_path / "normalized" / "enrichment", topic)
    if version.get("ok"):
        item["decided_version_digest"] = version["digest"]
    _write_review(tmp_path, state, item)


def _resolve(tmp_path, conn):
    res = synthesis.apply_resolved_syntheses(
        conn, tmp_path / "reviews", synthesis_dir=tmp_path / "wiki" / "Synthesis",
        enrichment_dir=tmp_path / "normalized" / "enrichment", now="t2")
    conn.commit()
    return res


def test_propose_approved_applied_then_already_in_effect_with_name_before_rerender(tmp_path):
    conn = sv._graph(tmp_path)
    sv._active_synthesis(tmp_path, conn, status="candidate")
    _set_artifact_title(tmp_path, "Solar trends v2")    # the approval re-renders with a new title
    _propose(tmp_path, "approved", "rev_p")             # ... the version it was decided for

    res = _resolve(tmp_path, conn)
    rec = _by_id(res)["rev_p"]
    assert rec["outcome"] == "applied" and rec["type"] == "propose_synthesis"
    assert rec["details"]["subject"] == {"id": SYN, "name": "Solar trends"}   # read before the render
    assert "Solar trends v2" in _page_text(tmp_path)
    assert rec["details"]["before"] == {"status": "candidate"}
    assert rec["details"]["after"] == {"status": "active"} and rec["details"]["decision"] == "approved"
    assert res["promoted"] + res["rejected"] == apply_outcomes.counts(res["outcomes"])["applied"] == 1

    again = _resolve(tmp_path, conn)                    # graph AND page active: verified
    assert [r["outcome"] for r in again["outcomes"]] == ["already_in_effect"]
    assert again["promoted"] == 0


def test_propose_graph_in_target_but_page_not_is_skipped(tmp_path):
    conn = sv._graph(tmp_path)
    sv._active_synthesis(tmp_path, conn, status="active")
    _set_page_status(tmp_path, "candidate")             # graph active, page candidate: not verified
    before = _page_text(tmp_path)
    _propose(tmp_path, "approved", "rev_p")

    res = _resolve(tmp_path, conn)
    (rec,) = res["outcomes"]
    assert rec["outcome"] == "skipped" and rec["reason"] == "page_status_mismatch"
    assert rec["details"]["problem"] == {"node_status": "active", "page_status": "candidate"}
    assert _page_text(tmp_path) == before and res["promoted"] == 0      # behaviour unchanged


def test_propose_rejected_and_approved_one_outcome_each_and_skip_reasons(tmp_path):
    conn = sv._graph(tmp_path)
    sv._active_synthesis(tmp_path, conn, status="candidate")
    _propose(tmp_path, "rejected", "rev_r")
    _propose(tmp_path, "approved", "rev_missing", topic=None)
    _propose(tmp_path, "approved", "rev_nonode", topic="itm_bbbbbbbbbbbbbbbb")

    res = _resolve(tmp_path, conn)
    recs = _by_id(res)
    assert set(recs) == {"rev_r", "rev_missing", "rev_nonode"}
    assert recs["rev_r"]["outcome"] == "applied" and recs["rev_r"]["details"]["decision"] == "rejected"
    assert recs["rev_r"]["details"]["after"] == {"status": "deprecated_candidate"}
    assert recs["rev_missing"]["reason"] == "missing_subject"
    assert recs["rev_missing"]["details"]["subject"] == {"id": "rev_missing", "name": "rev_missing"}
    nonode = recs["rev_nonode"]
    assert nonode["reason"] == "synthesis_artifact_missing"   # ADR-0073: bound to a version first
    other = synthesis.synthesis_id("itm_bbbbbbbbbbbbbbbb")
    assert nonode["details"]["subject"] == {"id": other, "name": other}    # no page: the id
    assert res["rejected"] == apply_outcomes.counts(res["outcomes"])["applied"] == 1


def test_propose_hidden_and_artifact_missing_are_skipped(tmp_path):
    conn = sv._graph(tmp_path)
    sv._active_synthesis(tmp_path, conn, status="candidate")
    _propose(tmp_path, "approved", "rev_p")
    (tmp_path / "normalized" / "enrichment" / f"{TOPIC}.synthesis.json").unlink()
    res = _resolve(tmp_path, conn)
    assert [(r["outcome"], r["reason"]) for r in res["outcomes"]] == [
        ("skipped", "synthesis_artifact_missing")]

    conn2 = sv._graph(tmp_path / "h")
    sv._active_synthesis(tmp_path / "h", conn2, status="hidden")
    _propose(tmp_path / "h", "approved", "rev_p")
    res = _resolve(tmp_path / "h", conn2)
    (rec,) = res["outcomes"]
    assert (rec["outcome"], rec["reason"]) == ("skipped", "visibility_suppressed")
    assert rec["details"]["problem"]["page_status"] == "hidden" and res["promoted"] == 0


# --- hide_synthesis / unhide_synthesis ---------------------------------------


def test_hide_applied_then_already_in_effect_names_before_rerender(tmp_path):
    conn = sv._graph(tmp_path)
    sv._active_synthesis(tmp_path, conn, status="active")
    sv._approve(tmp_path, "hide_synthesis", to_status="hidden", rid="rev_h")
    _set_artifact_title(tmp_path, "Renamed <b>title</b>")

    res = sv._apply_hide(tmp_path, conn)
    (rec,) = res["outcomes"]
    assert rec["review_id"] == "rev_h" and rec["type"] == "hide_synthesis"
    assert rec["outcome"] == "applied" and rec["details"]["subject"]["name"] == "Solar trends"
    assert rec["details"]["before"] == {"status": "active"} and rec["details"]["after"] == {"status": "hidden"}
    assert "Renamed <b>title</b>" in _page_text(tmp_path)
    assert res["applied"] + res["normalized"] == apply_outcomes.counts(res["outcomes"])["applied"] == 1

    again = sv._apply_hide(tmp_path, conn)              # page AND graph hidden, review approved
    assert [r["outcome"] for r in again["outcomes"]] == ["already_in_effect"]
    assert again["applied"] == again["normalized"] == 0 and again["skipped"] == []


def test_hide_normalized_is_one_applied_outcome_flagged(tmp_path):
    conn = sv._graph(tmp_path)
    sv._active_synthesis(tmp_path, conn, status="hidden")   # both hidden, review_status pending
    assert sv._syn_fm(tmp_path)["review_status"] == "pending"
    sv._approve(tmp_path, "hide_synthesis", to_status="hidden", rid="rev_h")
    res = sv._apply_hide(tmp_path, conn)
    (rec,) = res["outcomes"]
    assert rec["outcome"] == "applied" and rec["details"]["normalized"] is True
    assert res["normalized"] == 1 and res["applied"] == 0
    assert res["applied"] + res["normalized"] == apply_outcomes.counts(res["outcomes"])["applied"]


def test_hide_skip_reasons_match_skipped_list(tmp_path):
    conn = sv._graph(tmp_path)
    sv._active_synthesis(tmp_path, conn, status="candidate")
    sv._approve(tmp_path, "hide_synthesis", to_status="hidden", rid="rev_notactive")
    sv._approve(tmp_path, "hide_synthesis", to_status="active", rid="rev_badto")
    d = tmp_path / "reviews" / "approved"
    (d / "rev_badpath.json").write_text(json.dumps({
        "review_id": "rev_badpath", "type": "hide_synthesis",
        "subject": {"node_id": SYN, "page": "../../etc/passwd"},
        "proposal": {"to_status": "hidden"}}), encoding="utf-8")

    res = sv._apply_hide(tmp_path, conn)
    recs = _by_id(res)
    assert {rid: r["reason"] for rid, r in recs.items()} == {
        "rev_notactive": "synthesis_not_active", "rev_badto": "unexpected_to_status",
        "rev_badpath": "invalid_page_path"}
    assert all(r["outcome"] == "skipped" for r in recs.values())
    assert recs["rev_notactive"]["details"]["problem"] == {"page_status": "candidate",
                                                           "node_status": "candidate"}
    assert recs["rev_badpath"]["details"]["subject"] == {"id": SYN, "name": SYN}  # page never read
    assert sorted((s["review_id"], s["reason"]) for s in res["skipped"]) == sorted(
        (rid, r["reason"]) for rid, r in recs.items())


def test_hide_partial_state_and_unreadable_name(tmp_path):
    conn = sv._graph(tmp_path)
    sv._active_synthesis(tmp_path, conn, status="active")
    _set_page_status(tmp_path, "hidden")                # page hidden, graph active
    sv._approve(tmp_path, "hide_synthesis", to_status="hidden", rid="rev_h")
    (rec,) = sv._apply_hide(tmp_path, conn)["outcomes"]
    assert (rec["outcome"], rec["reason"]) == ("skipped", "partial_hide_state")

    (tmp_path / "wiki" / "Synthesis" / f"{SYN}.md").unlink()   # no page: no topic, no name
    res = sv._apply_hide(tmp_path, conn)
    (rec,) = res["outcomes"]
    assert (rec["outcome"], rec["reason"]) == ("skipped", "synthesis_topic_mismatch")
    assert rec["details"]["subject"] == {"id": SYN, "name": SYN}


def test_unhide_applied_already_in_effect_and_page_missing(tmp_path):
    conn = sv._graph(tmp_path)
    sv._active_synthesis(tmp_path, conn, status="active")
    sv._approve(tmp_path, "hide_synthesis", to_status="hidden", rid="rev_h")
    sv._apply_hide(tmp_path, conn)
    sv._approve(tmp_path, "unhide_synthesis", to_status="active", rid="rev_u")

    res = sv._apply_unhide(tmp_path, conn)
    (rec,) = res["outcomes"]
    assert rec["review_id"] == "rev_u" and rec["outcome"] == "applied"
    assert rec["details"]["before"] == {"status": "hidden"} and rec["details"]["after"] == {"status": "active"}
    assert res["applied"] == apply_outcomes.counts(res["outcomes"])["applied"] == 1

    again = sv._apply_unhide(tmp_path, conn)
    assert [r["outcome"] for r in again["outcomes"]] == ["already_in_effect"]

    (tmp_path / "wiki" / "Synthesis" / f"{SYN}.md").unlink()   # "not hidden" no longer verifiable
    gone = sv._apply_unhide(tmp_path, conn)
    (rec,) = gone["outcomes"]
    assert (rec["outcome"], rec["reason"]) == ("skipped", "page_missing")
    assert gone["skipped"] == []                        # the existing list is unchanged


# --- resolve_contradiction ---------------------------------------------------

CA, CB = sorted(("clm_1", "clm_2"))


def _pair(tmp_path, *, edge_status="proposed", pages=True):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    conn = graph.connect(gdb)
    tc._seed_evidenced_claim(conn, tmp_path, CA, "src_a", "Revenue rose ten percent.")
    tc._seed_evidenced_claim(conn, tmp_path, CB, "src_b", "Revenue fell five percent.")
    if not pages:
        for c in (CA, CB):
            (tmp_path / "wiki" / "Claims" / f"{c}.md").unlink()
    if edge_status:
        rid = reviews.review_id("resolve_contradiction", {"claim_a": CA, "claim_b": CB})
        graph.upsert_assertion(conn, src_id=CA, dst_id=CB, edge_type="contradicts", asserted_by="llm",
                               status=edge_status, evidence_source_id="src_a", evidence_char_start=0,
                               evidence_char_end=5, review_id=rid)
    conn.commit()
    return conn


def _decide(tmp_path, state, rid, *, winner=None, subject=None, with_id=True):
    item = {"review_id": rid, "type": "resolve_contradiction", "status": state,
            "subject": subject if subject is not None else {"claim_a": CA, "claim_b": CB},
            "proposal": {}, "context": {}}
    if winner:
        item["winner"] = winner
    d = tmp_path / "reviews" / state
    d.mkdir(parents=True, exist_ok=True)
    if not with_id:
        item.pop("review_id")
    (d / f"{rid}.json").write_text(json.dumps(item), encoding="utf-8")


def _contra(tmp_path, conn):
    res = contradictions.apply_contradiction_decisions(
        conn, tmp_path / "reviews", claims_dir=tmp_path / "wiki" / "Claims",
        markdown_dir=tmp_path / "normalized" / "markdown")
    conn.commit()
    return res


def test_acknowledge_applied_then_already_in_effect_with_names(tmp_path):
    conn = _pair(tmp_path)
    _decide(tmp_path, "approved", "rev_c")
    res = _contra(tmp_path, conn)
    (rec,) = res["outcomes"]
    assert rec["review_id"] == "rev_c" and rec["type"] == "resolve_contradiction"
    assert rec["outcome"] == "applied" and rec["details"]["resolution"] == "acknowledge"
    assert rec["details"]["subject"]["id"] == CA and rec["details"]["partner"]["id"] == CB
    assert "Revenue rose" in rec["details"]["subject"]["name"]
    assert "Revenue fell" in rec["details"]["partner"]["name"]
    assert "winner" not in rec["details"]
    assert res["resolution"]["acknowledged"] == apply_outcomes.counts(res["outcomes"])["applied"] == 1

    again = _contra(tmp_path, conn)                     # the edge verified active
    assert [r["outcome"] for r in again["outcomes"]] == ["already_in_effect"]
    assert again["resolution"]["acknowledged"] == 0


def test_reject_applied_then_already_in_effect(tmp_path):
    conn = _pair(tmp_path, edge_status="active")
    _decide(tmp_path, "rejected", "rev_c")
    res = _contra(tmp_path, conn)
    (rec,) = res["outcomes"]
    assert rec["outcome"] == "applied" and rec["details"]["resolution"] == "reject"
    assert res["resolution"]["rejected"] == apply_outcomes.counts(res["outcomes"])["applied"] == 1
    again = _contra(tmp_path, conn)
    assert [r["outcome"] for r in again["outcomes"]] == ["already_in_effect"]


def test_supersede_applied_with_winner_then_already_in_effect(tmp_path):
    conn = _pair(tmp_path)
    _decide(tmp_path, "approved", "rev_c", winner=CB)
    loser_name = apply_outcomes.page_name(tmp_path / "wiki", f"Claims/{CA}", CA)
    res = _contra(tmp_path, conn)
    recs = [r for r in res["outcomes"] if r["review_id"] == "rev_c"]   # the audit item is not read here
    assert len(recs) == 1 and len(res["outcomes"]) == 1
    rec = recs[0]
    assert rec["outcome"] == "applied" and rec["details"]["resolution"] == "supersede"
    assert rec["details"]["winner"] == rec["details"]["partner"] and rec["details"]["winner"]["id"] == CB
    assert rec["details"]["subject"] == loser_name                      # read before the deprecation
    assert graph.get_node(conn, CA)["status"] == "deprecated_candidate"
    assert res["resolution"]["superseded_executed"] == 1 and res["resolution"]["acknowledged"] == 1

    again = _contra(tmp_path, conn)                     # supersede effects + active edge verified
    assert [r["outcome"] for r in again["outcomes"]] == ["already_in_effect"]
    assert again["resolution"] == {"acknowledged": 0, "rejected": 0, "superseded_executed": 0}


def test_contradiction_skip_reasons_and_id_fallbacks(tmp_path):
    conn = _pair(tmp_path, edge_status=None, pages=False)  # no edge, no claim pages
    _decide(tmp_path, "approved", "rev_noedge")
    _decide(tmp_path, "rejected", "rev_noid", subject={"claim_a": CA}, with_id=False)
    _decide(tmp_path, "approved", "rev_trav", subject={"claim_a": "../../x", "claim_b": CB})
    res = _contra(tmp_path, conn)
    recs = _by_id(res)
    assert set(recs) == {"rev_noedge", "rev_noid", "rev_trav"}
    noedge = recs["rev_noedge"]
    assert (noedge["outcome"], noedge["reason"]) == ("skipped", "edge_missing")
    assert noedge["details"]["subject"] == {"id": CA, "name": CA}       # unreadable page: the id
    noid = recs["rev_noid"]                             # review id from the file name
    assert (noid["outcome"], noid["reason"]) == ("skipped", "missing_subject")
    assert noid["details"]["partner"] == {"id": "rev_noid", "name": "rev_noid"}
    assert recs["rev_trav"]["details"]["subject"] == {"id": "../../x", "name": "../../x"}
    assert apply_outcomes.counts(res["outcomes"]) == {"skipped": 3}


def test_contradiction_edge_in_other_status_is_skipped(tmp_path):
    conn = _pair(tmp_path, edge_status="proposed")
    (row,) = graph.contradiction_between(conn, CA, CB)
    graph.set_status(conn, row["edge_id"], "superseded", now="t")
    conn.commit()
    _decide(tmp_path, "approved", "rev_c")
    res = _contra(tmp_path, conn)
    (rec,) = res["outcomes"]
    assert (rec["outcome"], rec["reason"]) == ("skipped", "edge_status_unexpected")
    assert rec["details"]["problem"]["edge_statuses"] == ["superseded"]
    assert res["graph_changed"] is False


# --- verified end state (B2a review round 1) ---------------------------------
# Reporting reads never raise and never justify `already_in_effect`; `after` is read back, not assumed.


def _boom(*a, **kw):
    raise OSError("unreadable")


def _then(monkeypatch, after_render):
    """Run the real `_render_page`, then `after_render()` — a fault that only the reporting
    read-back (or a concurrent writer) sees."""
    real = synthesis._render_page

    def wrapped(*a, **kw):
        changed = real(*a, **kw)
        after_render()
        return changed

    monkeypatch.setattr(synthesis, "_render_page", wrapped)


def test_propose_noop_page_read_failure_is_unavailable_not_already_in_effect(tmp_path, monkeypatch):
    conn = sv._graph(tmp_path)
    sv._active_synthesis(tmp_path, conn, status="active")      # graph AND page already active
    _propose(tmp_path, "approved", "rev_p")
    before = _page_text(tmp_path)
    monkeypatch.setattr(synthesis, "_page_status", _boom)       # the verification read fails
    res = _resolve(tmp_path, conn)                              # no exception
    (rec,) = res["outcomes"]
    assert (rec["outcome"], rec["reason"]) == ("skipped", apply_outcomes.VERIFICATION_UNAVAILABLE)
    assert res["promoted"] == 0 and _page_text(tmp_path) == before

    monkeypatch.undo()                                          # readable again: verified no-op
    assert [r["outcome"] for r in _resolve(tmp_path, conn)["outcomes"]] == ["already_in_effect"]


def test_propose_read_back_failure_after_write_is_incomplete_result_stands(tmp_path, monkeypatch):
    conn = sv._graph(tmp_path)
    sv._active_synthesis(tmp_path, conn, status="candidate")
    _propose(tmp_path, "approved", "rev_p")
    _then(monkeypatch, lambda: monkeypatch.setattr(synthesis, "parse_frontmatter", _boom))
    res = _resolve(tmp_path, conn)
    (rec,) = res["outcomes"]
    assert (rec["outcome"], rec["reason"]) == ("incomplete", apply_outcomes.VERIFICATION_UNAVAILABLE)
    assert res["promoted"] == 1                                 # the executor's result stands
    monkeypatch.undo()
    assert sv._syn_fm(tmp_path)["status"] == "active"
    assert graph.get_node(conn, SYN)["status"] == "active"


def test_propose_read_back_mismatch_is_postcondition_not_met(tmp_path, monkeypatch):
    conn = sv._graph(tmp_path)
    sv._active_synthesis(tmp_path, conn, status="candidate")
    _propose(tmp_path, "approved", "rev_p")
    _then(monkeypatch, lambda: _set_page_status(tmp_path, "candidate"))   # a concurrent revert
    res = _resolve(tmp_path, conn)
    (rec,) = res["outcomes"]
    assert (rec["outcome"], rec["reason"]) == ("incomplete", apply_outcomes.POSTCONDITION_NOT_MET)
    assert rec["details"]["after"] == {"status": "candidate"}            # read back, not assumed
    assert rec["details"]["problem"]["expected"] == "active"
    assert res["promoted"] == 1 and sv._syn_fm(tmp_path)["status"] == "candidate"


def test_unhide_read_back_hidden_is_postcondition_not_met(tmp_path, monkeypatch):
    """hidden_takes_precedence is reserved for non-visibility reviews; an unhide that reads back
    hidden is simply not in effect."""
    conn = sv._graph(tmp_path)
    sv._active_synthesis(tmp_path, conn, status="active")
    sv._approve(tmp_path, "hide_synthesis", to_status="hidden", rid="rev_h")
    sv._apply_hide(tmp_path, conn)
    sv._approve(tmp_path, "unhide_synthesis", to_status="active", rid="rev_u")

    def rehide():                                    # page AND graph hidden again after the render
        _set_page_status(tmp_path, "hidden")
        graph.upsert_node(conn, node_id=SYN, node_type="synthesis", slug=SYN, status="hidden", now="t3")

    _then(monkeypatch, rehide)
    res = sv._apply_unhide(tmp_path, conn)
    (rec,) = res["outcomes"]
    assert (rec["outcome"], rec["reason"]) == ("incomplete", apply_outcomes.POSTCONDITION_NOT_MET)
    assert rec["details"]["after"] == {"status": "hidden"}
    assert res["applied"] == 1                                  # legacy counter unchanged
    assert sv._syn_fm(tmp_path)["status"] == "hidden"


def test_hide_read_back_failure_is_incomplete_and_normalized_page_changed(tmp_path, monkeypatch):
    conn = sv._graph(tmp_path)
    sv._active_synthesis(tmp_path, conn, status="hidden")       # both hidden, review pending
    sv._approve(tmp_path, "hide_synthesis", to_status="hidden", rid="rev_h")
    _then(monkeypatch, lambda: monkeypatch.setattr(synthesis.graph, "get_node", _boom))
    res = sv._apply_hide(tmp_path, conn)                        # no exception
    (rec,) = res["outcomes"]
    assert (rec["outcome"], rec["reason"]) == ("incomplete", apply_outcomes.VERIFICATION_UNAVAILABLE)
    assert rec["details"]["normalized"] is True and res["normalized"] == 1
    monkeypatch.undo()
    assert sv._syn_fm(tmp_path)["review_status"] == "approved"  # the write happened


def test_hide_normalized_applied_reads_back_the_written_page(tmp_path):
    conn = sv._graph(tmp_path)
    sv._active_synthesis(tmp_path, conn, status="hidden")
    sv._approve(tmp_path, "hide_synthesis", to_status="hidden", rid="rev_h")
    (rec,) = sv._apply_hide(tmp_path, conn)["outcomes"]
    assert rec["outcome"] == "applied" and rec["details"]["normalized"] is True   # never already_in_effect
    assert rec["details"]["after"] == {"status": "hidden"}
    assert sv._syn_fm(tmp_path)["review_status"] == "approved"
    assert graph.get_node(conn, SYN)["status"] == "hidden"
    again = sv._apply_hide(tmp_path, conn)                      # now a verified no-op
    assert [r["outcome"] for r in again["outcomes"]] == ["already_in_effect"]


def _fail_second_edge_query(monkeypatch):
    """`contradiction_between` works for the executor (1st call per decision) and fails for the
    reporting re-query (2nd call)."""
    real, calls = graph.contradiction_between, []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) % 2 == 0:
            raise RuntimeError("graph read failed")
        return real(*a, **kw)

    monkeypatch.setattr(contradictions.graph, "contradiction_between", flaky)


def test_contradiction_requery_failure_after_write_is_incomplete(tmp_path, monkeypatch):
    conn = _pair(tmp_path)
    _decide(tmp_path, "approved", "rev_c")
    _fail_second_edge_query(monkeypatch)
    res = _contra(tmp_path, conn)                               # no exception
    (rec,) = res["outcomes"]
    assert (rec["outcome"], rec["reason"]) == ("incomplete", apply_outcomes.VERIFICATION_UNAVAILABLE)
    assert res["resolution"]["acknowledged"] == 1 and res["graph_changed"] is True
    monkeypatch.undo()
    assert [r["status"] for r in graph.contradiction_between(conn, CA, CB)] == ["active"]


def test_contradiction_requery_failure_on_noop_is_not_already_in_effect(tmp_path, monkeypatch):
    conn = _pair(tmp_path, edge_status="active")
    _decide(tmp_path, "approved", "rev_c")
    _fail_second_edge_query(monkeypatch)
    res = _contra(tmp_path, conn)
    (rec,) = res["outcomes"]
    assert (rec["outcome"], rec["reason"]) == ("skipped", apply_outcomes.VERIFICATION_UNAVAILABLE)
    assert res["resolution"]["acknowledged"] == 0
    monkeypatch.undo()
    assert [r["outcome"] for r in _contra(tmp_path, conn)["outcomes"]] == ["already_in_effect"]


def test_contradiction_read_back_mismatch_is_postcondition_not_met(tmp_path, monkeypatch):
    conn = _pair(tmp_path)
    _decide(tmp_path, "approved", "rev_c")
    real = graph.set_status
    monkeypatch.setattr(contradictions.graph, "set_status",
                        lambda c, eid, status, **kw: real(c, eid, "superseded", **kw))
    res = _contra(tmp_path, conn)
    (rec,) = res["outcomes"]
    assert (rec["outcome"], rec["reason"]) == ("incomplete", apply_outcomes.POSTCONDITION_NOT_MET)
    assert rec["details"]["problem"]["edge_statuses"] == ["superseded"]
    assert res["resolution"]["acknowledged"] == 1                # legacy counter unchanged


def test_supersede_effects_read_failure_is_incomplete(tmp_path, monkeypatch):
    conn = _pair(tmp_path)
    _decide(tmp_path, "approved", "rev_c", winner=CB)
    real, real_out = contradictions._execute_supersede, graph.outgoing_active

    def once(*a, **kw):                  # the next read (the reporting check) fails, later ones work
        monkeypatch.setattr(contradictions.graph, "outgoing_active", real_out)
        raise OSError("unreadable")

    def then_fail(*a, **kw):
        done = real(*a, **kw)
        monkeypatch.setattr(contradictions.graph, "outgoing_active", once)
        return done

    monkeypatch.setattr(contradictions, "_execute_supersede", then_fail)
    res = _contra(tmp_path, conn)
    (rec,) = res["outcomes"]
    assert (rec["outcome"], rec["reason"]) == ("incomplete", apply_outcomes.VERIFICATION_UNAVAILABLE)
    assert res["resolution"]["superseded_executed"] == 1
    assert graph.get_node(conn, CA)["status"] == "deprecated_candidate"
