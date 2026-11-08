"""ADR-0073 slice 1: synthesis decisions are bound to the version the human was shown.

Covers decisions 2–8: the version digest (validated artifact, recomputed, legacy format), the digest
through the form, the JSON API and bulk, Apply executing a decision only on its own version, "Decide
again for the version shown", regeneration under unchanged inputs (refresh + interrupted
generation), and the synthesis validator's three cases.
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.backend import graph
from app.backend import main as main_module
from app.backend.config import get_settings
from app.workers import reviews, synthesis
from app.workers import synthesis_version as sv
from scripts import validate_synthesis
from tests import fixtures
from tests import test_synthesis_visibility as vis

TOPIC, SYN, FP = vis.TOPIC, vis.SYN, vis.FP
OTHER_FP = "1e1e1e1e1e1e1e1e"


# --- fixtures ----------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(main_module.publication.keyword_index, "reindex", lambda root: None)
    return TestClient(main_module.app)


def _candidate(tmp_path, status="candidate"):
    conn = vis._graph(tmp_path)
    vis._active_synthesis(tmp_path, conn, status=status)
    conn.commit()
    return conn


def _artifact_path(tmp_path):
    return tmp_path / "normalized" / "enrichment" / f"{TOPIC}.synthesis.json"


def _artifact(tmp_path):
    return json.loads(_artifact_path(tmp_path).read_text(encoding="utf-8"))


def _set_artifact(tmp_path, **fields):
    _artifact_path(tmp_path).write_text(json.dumps(_artifact(tmp_path) | fields),
                                        encoding="utf-8")


def _digest(tmp_path):
    return vis.bound_digest(tmp_path)


def _review(tmp_path, state, rid, *, fingerprint=FP, bound=True, digest=None, proposal=None):
    """A propose_synthesis review in `state`; a terminal one bound to the current version unless
    `bound=False` (a legacy decision) or `digest` names another."""
    item = {"review_id": rid, "type": "propose_synthesis",
            "status": "pending" if state == "pending" else state, "priority": "medium",
            "subject": {"topic_node_id": TOPIC, "fingerprint": fingerprint},
            "proposal": proposal if proposal is not None else {"to_status": "active",
                                                               "synthesis_id": SYN},
            "context": {"node_type": "item"}}
    if state in ("approved", "rejected"):
        item["decided_by"], item["decided_at"] = "human", "2026-11-01T00:00:00Z"
        if state == "approved":
            item["approval_id"] = "a" * 32
        if bound:
            item[sv.DECIDED] = digest or _digest(tmp_path)
    d = tmp_path / "reviews" / state
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps(item), encoding="utf-8")
    return item


def _apply(tmp_path, conn):
    res = synthesis.apply_resolved_syntheses(
        conn, tmp_path / "reviews", synthesis_dir=tmp_path / "wiki" / "Synthesis",
        enrichment_dir=tmp_path / "normalized" / "enrichment", now="t2")
    conn.commit()
    return {r["review_id"]: r for r in res["outcomes"]}, res


def _node_status(conn):
    return graph.get_node(conn, SYN)["status"]


def _page(tmp_path):
    return (tmp_path / "wiki" / "Synthesis" / f"{SYN}.md").read_text(encoding="utf-8")


def _location(tmp_path, rid):
    return [d for d in ("pending", "approved", "rejected")
            if (tmp_path / "reviews" / d / f"{rid}.json").exists()]


def _item(tmp_path, rid):
    (d,) = _location(tmp_path, rid)
    return json.loads((tmp_path / "reviews" / d / f"{rid}.json").read_text(encoding="utf-8"))


# --- decision 2–3: the digest -------------------------------------------------------------------


def test_the_digest_covers_the_inputs_and_every_rendered_field(tmp_path):
    base = {"input_fingerprint": FP, "title": "T", "summary": "S", "synthesis": "X",
            "confidence": 0.5}
    d = sv.compute_digest(base)
    assert re.fullmatch(r"[0-9a-f]{64}", d)
    for key, value in (("input_fingerprint", OTHER_FP), ("title", "T2"), ("summary", "S2"),
                       ("synthesis", "X2"), ("confidence", 0.6)):
        assert sv.compute_digest(base | {key: value}) != d, key
    assert sv.compute_digest(base | {"model_ref": "other", "generated_at": "t9"}) == d
    assert sv.compute_digest(base | {"confidence": 1}) == sv.compute_digest(base | {"confidence": 1.0})


@pytest.mark.parametrize("field, value", [
    ("input_fingerprint", "fp"), ("input_fingerprint", "0" * 64), ("input_fingerprint", None),
    ("title", ""), ("title", "   "), ("summary", None), ("synthesis", "\n"),
    ("confidence", math.nan), ("confidence", math.inf), ("confidence", True), ("confidence", 1.5),
    ("confidence", -0.1), ("confidence", "0.7"), ("confidence", 10**400), ("confidence", 2),
    ("version_digest", "short"), ("version_digest", None),
])
def test_invalid_fields_are_malformed_with_no_defaults(field, value):
    a = {"input_fingerprint": FP, "title": "T", "summary": "S", "synthesis": "X", "confidence": 0.5}
    out = sv.check(a | {field: value})
    assert out == {"ok": False, "reason": sv.INVALID, "field": field}


def test_a_legacy_artifact_is_usable_and_a_stored_mismatch_is_not(tmp_path):
    _candidate(tmp_path)
    legacy = sv.read(tmp_path / "normalized" / "enrichment", TOPIC)
    assert legacy["ok"] and legacy["legacy_format"]
    _set_artifact(tmp_path, version_digest=legacy["digest"])
    stamped = sv.read(tmp_path / "normalized" / "enrichment", TOPIC)
    assert stamped["ok"] and not stamped["legacy_format"] and stamped["digest"] == legacy["digest"]
    _set_artifact(tmp_path, version_digest="f" * 64)
    assert sv.read(tmp_path / "normalized" / "enrichment", TOPIC)["reason"] == sv.DIGEST_MISMATCH


# --- decision 6: Apply executes a decision only on its own version ----------------------------


def test_an_old_rejection_cannot_override_the_approval_of_the_current_version(tmp_path):
    conn = _candidate(tmp_path)
    _review(tmp_path, "rejected", "rev_old", fingerprint=OTHER_FP, digest="e" * 64)
    _review(tmp_path, "approved", "rev_new")
    for _ in range(2):                                    # no flip-flop across Applies
        recs, _res = _apply(tmp_path, conn)
        assert recs["rev_old"]["outcome"] == "skipped"
        assert recs["rev_old"]["reason"] == sv.VERSION_REPLACED
        assert recs["rev_old"]["details"]["information"] is True
        assert recs["rev_new"]["outcome"] in ("applied", "already_in_effect")
        assert _node_status(conn) == "active" and "status: active" in _page(tmp_path)


def test_an_old_approval_cannot_approve_a_replacement_version(tmp_path):
    conn = _candidate(tmp_path)
    _review(tmp_path, "approved", "rev_p")               # bound to the current text
    before = _page(tmp_path)
    _set_artifact(tmp_path, summary="New, unreviewed prose.")   # same inputs, new text
    recs, res = _apply(tmp_path, conn)
    assert recs["rev_p"]["reason"] == sv.VERSION_REPLACED and res["promoted"] == 0
    assert _page(tmp_path) == before and _node_status(conn) == "candidate"


def test_a_legacy_decision_on_current_inputs_is_unbound_and_changes_nothing(tmp_path):
    conn = _candidate(tmp_path)
    _review(tmp_path, "approved", "rev_p", bound=False)
    before = _page(tmp_path)
    recs, _res = _apply(tmp_path, conn)
    assert recs["rev_p"]["reason"] == sv.DECISION_UNBOUND
    assert "information" not in recs["rev_p"]["details"]  # under Attention needed
    assert _page(tmp_path) == before and _node_status(conn) == "candidate"


_BAD_ARTIFACTS = {
    "missing": (lambda p: p.unlink(), sv.MISSING),
    "unreadable": (lambda p: p.write_text("{not json", encoding="utf-8"), sv.UNREADABLE),
    "fingerprint": (lambda p: _rewrite(p, input_fingerprint="fp"), sv.INVALID),
    "title": (lambda p: _rewrite(p, title=" "), sv.INVALID),
    "summary": (lambda p: _rewrite(p, summary=""), sv.INVALID),
    "synthesis": (lambda p: _rewrite(p, synthesis=None), sv.INVALID),
    "nan": (lambda p: _rewrite(p, confidence=math.nan), sv.INVALID),
    "inf": (lambda p: _rewrite(p, confidence=math.inf), sv.INVALID),
    "bool": (lambda p: _rewrite(p, confidence=True), sv.INVALID),
    "mismatch": (lambda p: _rewrite(p, version_digest="f" * 64), sv.DIGEST_MISMATCH),
    # ADR-0073 slice 1 review: an oversized integer is malformed (never an OverflowError), and
    # legacy format is the field's absence — an explicit null digest is malformed.
    "huge_int": (lambda p: p.write_text(
        p.read_text(encoding="utf-8").replace('"confidence": 0.7', '"confidence": 1' + "0" * 400),
        encoding="utf-8"), sv.INVALID),
    "null_digest": (lambda p: _rewrite(p, version_digest=None), sv.INVALID),
}


def _rewrite(path, **fields):
    path.write_text(json.dumps(json.loads(path.read_text(encoding="utf-8")) | fields),
                    encoding="utf-8")


@pytest.mark.parametrize("case", sorted(_BAD_ARTIFACTS))
def test_a_bad_artifact_causes_no_mutation_in_apply(tmp_path, case):
    conn = _candidate(tmp_path)
    _review(tmp_path, "approved", "rev_p")
    before = _page(tmp_path)
    mutate, reason = _BAD_ARTIFACTS[case]
    mutate(_artifact_path(tmp_path))
    recs, res = _apply(tmp_path, conn)
    assert recs["rev_p"]["outcome"] == "skipped" and recs["rev_p"]["reason"] == reason
    assert _page(tmp_path) == before and _node_status(conn) == "candidate"
    assert res["promoted"] == res["rejected"] == 0


@pytest.mark.parametrize("case", sorted(_BAD_ARTIFACTS))
def test_a_bad_artifact_refuses_every_decision_path(client, tmp_path, case):
    _candidate(tmp_path).close()
    _review(tmp_path, "pending", "rev_p")
    shown = _digest(tmp_path)
    mutate, reason = _BAD_ARTIFACTS[case]
    mutate(_artifact_path(tmp_path))
    for verb in ("approve", "reject", "defer"):
        resp = client.post(f"/reviews/rev_p/{verb}", json={"version_digest": shown})
        assert resp.status_code == 409 and reason in resp.json()["detail"], verb
    assert _location(tmp_path, "rev_p") == ["pending"]
    assert _item(tmp_path, "rev_p")["status"] == "pending"


# --- decision 4: the digest through the form, the JSON API and bulk --------------------------


def test_the_api_requires_the_digest_and_records_it(client, tmp_path):
    _candidate(tmp_path).close()
    _review(tmp_path, "pending", "rev_p")
    assert client.post("/reviews/rev_p/approve", json={}).status_code == 400
    assert client.post("/reviews/rev_p/approve", json={"version_digest": "abc"}).status_code == 400
    shown = client.get("/reviews/rev_p").json()["preview"]["synthesis_version"]
    assert shown["ok"] and shown["synthesis"] == "Across sources solar grows."
    assert shown["legacy_format"] is True                # decidable as is, no migration
    resp = client.post("/reviews/rev_p/approve", json={"version_digest": shown["version_digest"]})
    assert resp.status_code == 200 and resp.json()["decision_recorded"] is True
    item = _item(tmp_path, "rev_p")
    assert item["status"] == "approved" and item[sv.DECIDED] == shown["version_digest"]
    audit = json.loads((tmp_path / "reviews" / "audit_log" / "rev_p-approved.json").read_text())
    assert audit[sv.DECIDED] == shown["version_digest"]
    assert client.get("/reviews/rev_p").json()["item"][sv.DECIDED] == shown["version_digest"]
    assert "version_digest" not in _artifact(tmp_path)    # displaying/deciding wrote nothing


def test_a_digest_on_another_type_is_refused(client, tmp_path):
    d = tmp_path / "reviews" / "pending"
    d.mkdir(parents=True, exist_ok=True)
    (d / "rev_o.json").write_text(json.dumps({
        "review_id": "rev_o", "type": "deprecate_wiki_page", "status": "pending",
        "subject": {"node_id": "itm_0123456789abcdef", "page": "Items/x.md"},
        "proposal": {"to_status": "deprecated_candidate"}, "context": {}}), encoding="utf-8")
    assert client.post("/reviews/rev_o/reject", json={"version_digest": "a" * 64}).status_code == 400


def test_same_inputs_with_different_text_need_a_new_decision(client, tmp_path):
    _candidate(tmp_path).close()
    _review(tmp_path, "pending", "rev_p")
    first = client.get("/reviews/rev_p").json()["preview"]["synthesis_version"]["version_digest"]
    _set_artifact(tmp_path, synthesis="Regenerated text under the same evidence.")
    resp = client.post("/reviews/rev_p/approve", json={"version_digest": first})
    assert resp.status_code == 409 and sv.VERSION_CHANGED in resp.json()["detail"]
    assert _location(tmp_path, "rev_p") == ["pending"]
    second = client.get("/reviews/rev_p").json()["preview"]["synthesis_version"]["version_digest"]
    assert second != first
    assert client.post("/reviews/rev_p/approve",
                       json={"version_digest": second}).json()["decision_recorded"] is True
    assert _item(tmp_path, "rev_p")[sv.DECIDED] == second


def test_regenerated_from_other_evidence_is_refused(client, tmp_path):
    _candidate(tmp_path).close()
    _review(tmp_path, "pending", "rev_p")
    _set_artifact(tmp_path, input_fingerprint=OTHER_FP)
    shown = _digest(tmp_path)
    resp = client.post("/reviews/rev_p/approve", json={"version_digest": shown})
    assert resp.status_code == 409 and sv.INPUTS_CHANGED in resp.json()["detail"]


def test_the_review_page_shows_the_full_version_escaped_and_the_form_carries_it(client, tmp_path):
    _candidate(tmp_path).close()
    _set_artifact(tmp_path, synthesis="Line one <script>x</script>\nLine two",
                  title="Solar <b>trends</b>")
    _review(tmp_path, "pending", "rev_p")
    shown = _digest(tmp_path)
    html = client.get("/ui/reviews/rev_p").text
    assert "Line one &lt;script&gt;x&lt;/script&gt;<br>Line two" in html
    assert "<script>x" not in html and "Solar &lt;b&gt;trends&lt;/b&gt;" in html
    assert f"name='version_digest' value='{shown}'" in html
    stale = client.post("/ui/reviews/rev_p/decide", data={"action": "approve", "version_digest": "b" * 64},
                        follow_redirects=False)
    assert stale.status_code == 409 and "changed — review again" in stale.text
    ok = client.post("/ui/reviews/rev_p/decide", data={"action": "approve", "version_digest": shown},
                     follow_redirects=False)
    assert ok.status_code == 303 and _item(tmp_path, "rev_p")[sv.DECIDED] == shown


def test_an_unusable_version_offers_no_decision(client, tmp_path):
    _candidate(tmp_path).close()
    _review(tmp_path, "pending", "rev_p")
    _set_artifact(tmp_path, confidence=True)
    html = client.get("/ui/reviews/rev_p").text
    assert "The synthesis text is malformed (field: confidence)" in html
    assert "/ui/reviews/rev_p/decide'" not in html


def _bulk_form(html):
    ids = re.findall(r"name='rid' value='([^']+)'", html)
    fps = dict(re.findall(r"name='fp_([^']+)' value='([0-9a-f]{64})'", html))
    vds = dict(re.findall(r"name='vd_([^']+)' value='([0-9a-f]{64})'", html))
    return {"rid": ids, **{f"fp_{k}": v for k, v in fps.items()},
            **{f"vd_{k}": v for k, v in vds.items()}}


def test_bulk_reject_records_the_digest_shown_and_skips_a_changed_version(client, tmp_path):
    _candidate(tmp_path).close()
    _review(tmp_path, "pending", "rev_p")
    shown = _digest(tmp_path)
    page = client.post("/ui/reviews/bulk/confirm", data={"bulk_action": "reject", "rid": ["rev_p"]})
    form = _bulk_form(page.text)
    assert form["vd_rev_p"] == shown
    # The text changes after the confirmation page was shown: skipped, nothing recorded.
    _set_artifact(tmp_path, summary="Changed meanwhile.")
    out = client.post("/ui/reviews/bulk", data={"bulk_action": "reject", **form}).text
    assert "0 rejected, 1 skipped" in out and "changed — review again" in out
    assert _location(tmp_path, "rev_p") == ["pending"]
    # Shown again for the current text: recorded with that digest.
    form = _bulk_form(client.post("/ui/reviews/bulk/confirm",
                                  data={"bulk_action": "reject", "rid": ["rev_p"]}).text)
    out = client.post("/ui/reviews/bulk", data={"bulk_action": "reject", **form}).text
    assert "1 rejected, 0 skipped" in out
    assert _item(tmp_path, "rev_p")[sv.DECIDED] == _digest(tmp_path)


# --- decision 7: Decide again for the version shown --------------------------------------------


def _audits(tmp_path, rid):
    return sorted((tmp_path / "reviews" / "audit_log").glob(f"{rid}-decided-again-*.json"))


def test_decide_again_rebinds_a_live_legacy_decision_and_apply_executes_it(client, tmp_path):
    conn = _candidate(tmp_path, status="active")          # a legacy approval, effect live
    conn.close()
    _review(tmp_path, "approved", "rev_p", bound=False)
    shown = _digest(tmp_path)
    html = client.get("/ui/reviews/rev_p").text
    assert "Decide again for the version shown" in html and "can’t be reopened" in html
    before = _page(tmp_path)
    resp = client.post("/reviews/rev_p/decide-again",
                       json={"decision": "rejected", "version_digest": shown, "note": "changed mind"})
    assert resp.status_code == 200 and resp.json()["decision_recorded"] is True
    assert _location(tmp_path, "rev_p") == ["rejected"]
    item = _item(tmp_path, "rev_p")
    assert item[sv.DECIDED] == shown and "approval_id" not in item
    (entry,) = _audits(tmp_path, "rev_p")
    audit = json.loads(entry.read_text(encoding="utf-8"))
    assert audit["prior_status"] == "approved" and audit["decision"] == "rejected"
    assert audit["prior_approval_id"] == "a" * 32 and audit[sv.DECIDED] == shown
    assert _page(tmp_path) == before                       # nothing changes until Apply
    # Retrying the same submission: no second decision, no second audit entry.
    again = client.post("/reviews/rev_p/decide-again",
                        json={"decision": "rejected", "version_digest": shown})
    assert again.status_code == 200 and again.json()["decision_recorded"] is False
    assert len(_audits(tmp_path, "rev_p")) == 1
    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    recs, _res = _apply(tmp_path, conn)
    assert recs["rev_p"]["outcome"] == "applied"
    assert _node_status(conn) == "deprecated_candidate"
    conn.close()


def test_decide_again_the_same_decision_verifies_as_already_in_effect(client, tmp_path):
    _candidate(tmp_path, status="active").close()
    _review(tmp_path, "approved", "rev_p", bound=False)
    resp = client.post("/ui/reviews/rev_p/decide-again",
                       data={"action": "approve", "version_digest": _digest(tmp_path)},
                       follow_redirects=False)
    assert resp.status_code == 303
    item = _item(tmp_path, "rev_p")
    assert item["status"] == "approved" and item["approval_id"] != "a" * 32
    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    recs, _res = _apply(tmp_path, conn)
    assert recs["rev_p"]["outcome"] == "already_in_effect"
    conn.close()


def test_decide_again_lock_checks_refuse_and_write_nothing(client, tmp_path):
    _candidate(tmp_path, status="active").close()
    shown = _digest(tmp_path)
    _review(tmp_path, "pending", "rev_pending")
    _review(tmp_path, "approved", "rev_bound")
    _review(tmp_path, "approved", "rev_old", bound=False, fingerprint=OTHER_FP)
    _review(tmp_path, "approved", "rev_legacy", bound=False)

    def post(rid, digest=shown, decision="rejected"):
        return client.post(f"/reviews/{rid}/decide-again",
                           json={"decision": decision, "version_digest": digest})

    assert post("rev_pending").status_code == 409                  # not terminal
    assert post("rev_bound").status_code == 409                    # already bound
    assert sv.INPUTS_CHANGED in post("rev_old").json()["detail"]   # not the current inputs
    assert sv.VERSION_CHANGED in post("rev_legacy", digest="c" * 64).json()["detail"]
    assert post("rev_legacy", decision="deferred").status_code == 400
    _set_artifact(tmp_path, title="")
    assert sv.INVALID in post("rev_legacy").json()["detail"]      # artifact invalid
    assert _location(tmp_path, "rev_legacy") == ["approved"]
    assert sv.DECIDED not in _item(tmp_path, "rev_legacy")
    assert not list((tmp_path / "reviews").rglob("*decided-again*"))


def test_decide_again_keeps_hidden_states(client, tmp_path):
    _candidate(tmp_path, status="hidden").close()
    _review(tmp_path, "approved", "rev_p", bound=False)
    assert client.post("/reviews/rev_p/decide-again",
                       json={"decision": "approved",
                             "version_digest": _digest(tmp_path)}).status_code == 200
    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    recs, _res = _apply(tmp_path, conn)
    assert recs["rev_p"]["reason"] == "visibility_suppressed"
    assert _node_status(conn) == "hidden" and "status: hidden" in _page(tmp_path)
    conn.close()


def test_decide_again_finishes_an_interrupted_attempt(tmp_path, monkeypatch):
    """The audit entry is written first; a crash before the move leaves it for the retry, which
    keeps it (one entry) and finishes the move."""
    _candidate(tmp_path, status="active").close()
    _review(tmp_path, "approved", "rev_p", bound=False)
    shown = _digest(tmp_path)
    real = reviews.write_json_durably
    calls = []

    def crash_on_item(path, obj):
        calls.append(path)
        if len(calls) == 2:                               # the decision file, after the audit
            raise KeyboardInterrupt
        return real(path, obj)

    monkeypatch.setattr(reviews, "write_json_durably", crash_on_item)
    with pytest.raises(KeyboardInterrupt):
        reviews.decide_again(tmp_path / "reviews", "rev_p", decision="rejected",
                             version_digest=shown)
    monkeypatch.setattr(reviews, "write_json_durably", real)
    assert len(_audits(tmp_path, "rev_p")) == 1 and _location(tmp_path, "rev_p") == ["approved"]
    assert reviews.decide_again(tmp_path / "reviews", "rev_p", decision="rejected",
                                version_digest=shown) is True
    assert len(_audits(tmp_path, "rev_p")) == 1 and _location(tmp_path, "rev_p") == ["rejected"]


def test_reopen_clears_the_bound_version(tmp_path):
    _candidate(tmp_path).close()
    _review(tmp_path, "approved", "rev_p")
    assert reviews.reopen_review_item(tmp_path / "reviews", "rev_p", reason="again") is True
    assert sv.DECIDED not in _item(tmp_path, "rev_p")


# --- Apply pages: information vs attention -----------------------------------------------------


def test_the_apply_preview_lists_a_replaced_version_as_information(client, tmp_path):
    _candidate(tmp_path).close()
    _review(tmp_path, "approved", "rev_old", fingerprint=OTHER_FP, digest="e" * 64)
    _review(tmp_path, "approved", "rev_legacy", bound=False)
    html = client.post("/ui/reviews/apply/preview").text
    info = html.split("<h2>Information</h2>")[1].split("<h2>")[0]
    attention = html.split("<h2>Attention needed</h2>")[1].split("</ul>")[0]
    assert "/ui/reviews/rev_old'" in info and "rev_old" not in attention
    assert "earlier version of the synthesis" in info
    assert "/ui/reviews/rev_legacy'" in attention
    assert "Decide again for the version shown" in attention
    assert html.count("/ui/reviews/rev_old'") == 1        # reported once per Apply result


# --- decision 5: regeneration under unchanged inputs -----------------------------------------


class _Regen:
    name = "anthropic"
    supports_batch = False
    calls = 0

    def available(self):
        return True

    def parse(self, messages, schema, model_id, *, max_tokens):
        self.calls += 1
        return {"summary": "A regenerated summary of the Q3 revenue sources.",
                "synthesis": "Rewritten: both independent reports show Q3 revenue increased.",
                "confidence": 0.6}


def _regen(tmp_path, jobs, **kw):
    import tests.test_synthesis as syn_t
    from app.llm.cache import ResponseCache
    from app.llm.client import LLMClient
    client = LLMClient({"anthropic": _Regen()}, cache=ResponseCache(tmp_path / "db" / "regen.sqlite"))
    return synthesis.generate_syntheses(tmp_path, client=client, model_ref=syn_t.HEAVY,
                                        jobs_db=jobs, rebuild_index=False, **kw)


def _pending_synthesis(tmp_path):
    (p,) = [p for p in (tmp_path / "reviews" / "pending").glob("*.json")
            if json.loads(p.read_text())["type"] == "propose_synthesis"]
    return json.loads(p.read_text(encoding="utf-8"))


def _topic_artifact(tmp_path, item):
    tid = item["subject"]["topic_node_id"]
    return json.loads((tmp_path / "normalized" / "enrichment" / f"{tid}.synthesis.json").read_text())


def test_a_forced_regeneration_refreshes_the_pending_proposal(tmp_path):
    import tests.test_synthesis as syn_t
    fixtures.copy_templates(tmp_path)
    jobs = syn_t._build(tmp_path)
    syn_t._gen(tmp_path, syn_t.SynthAdapter(), jobs)
    first = _pending_synthesis(tmp_path)
    art1 = _topic_artifact(tmp_path, first)
    assert first["proposal"][sv.FIELD] == art1[sv.FIELD] == sv.compute_digest(art1)
    _regen(tmp_path, jobs, force=True)
    second = _pending_synthesis(tmp_path)
    art2 = _topic_artifact(tmp_path, second)
    assert second["review_id"] == first["review_id"]      # same inputs, same review id
    assert second["proposal"][sv.FIELD] == art2[sv.FIELD] != art1[sv.FIELD]
    assert second["proposal"]["summary"] == art2["summary"] == \
        "A regenerated summary of the Q3 revenue sources."
    (entry,) = (tmp_path / "reviews" / "audit_log").glob(f"{first['review_id']}-proposal-refreshed-*.json")
    audit = json.loads(entry.read_text(encoding="utf-8"))
    assert audit["prior_proposal"][sv.FIELD] == art1[sv.FIELD]
    assert audit["proposal"][sv.FIELD] == art2[sv.FIELD]
    assert validate_synthesis.main([str(tmp_path)]) == 0


def test_an_interrupted_regeneration_is_refused_reported_and_recovered(client, tmp_path, monkeypatch):
    import tests.test_synthesis as syn_t
    fixtures.copy_templates(tmp_path)
    jobs = syn_t._build(tmp_path)
    syn_t._gen(tmp_path, syn_t.SynthAdapter(), jobs)
    rid = _pending_synthesis(tmp_path)["review_id"]
    real = reviews.refresh_pending_proposal

    def refresh_then_crash(*a, **kw):
        real(*a, **kw)
        raise KeyboardInterrupt                          # before the artifact is written

    monkeypatch.setattr(reviews, "refresh_pending_proposal", refresh_then_crash)
    with pytest.raises(KeyboardInterrupt):
        _regen(tmp_path, jobs, force=True)
    monkeypatch.setattr(reviews, "refresh_pending_proposal", real)   # (undo() would drop the host env)
    item = _pending_synthesis(tmp_path)
    old_art = _topic_artifact(tmp_path, item)
    assert item["proposal"][sv.FIELD] != sv.compute_digest(old_art)
    # Refused at every decision path while they disagree; the validator says what to run.
    got = client.get(f"/reviews/{rid}")
    assert got.status_code == 200, got.text
    shown = got.json()["preview"]["synthesis_version"]
    assert shown["proposal_agrees"] is False
    resp = client.post(f"/reviews/{rid}/approve", json={"version_digest": shown["version_digest"]})
    assert resp.status_code == 409 and sv.GENERATION_INTERRUPTED in resp.json()["detail"]
    assert "interrupted regeneration" in client.get(f"/ui/reviews/{rid}").text
    assert validate_synthesis.main([str(tmp_path)]) == 1
    # The producer recovery redoes the commit; proposal and artifact agree again.
    run = _regen(tmp_path, jobs)
    assert run["syntheses_written"] == 1
    item = _pending_synthesis(tmp_path)
    assert item["proposal"][sv.FIELD] == _topic_artifact(tmp_path, item)[sv.FIELD]
    assert validate_synthesis.main([str(tmp_path)]) == 0
    digest = client.get(f"/reviews/{rid}").json()["preview"]["synthesis_version"]["version_digest"]
    assert client.post(f"/reviews/{rid}/approve",
                       json={"version_digest": digest}).json()["decision_recorded"] is True


# --- decision 8: the validator -----------------------------------------------------------------


def test_the_validator_distinguishes_advisory_error_and_interrupted(tmp_path, capsys):
    _candidate(tmp_path).close()
    assert validate_synthesis.main([str(tmp_path)]) == 0
    assert "advisory" in capsys.readouterr().out          # legacy format: passes
    _set_artifact(tmp_path, version_digest=_digest(tmp_path))
    assert validate_synthesis.main([str(tmp_path)]) == 0
    assert "advisory" not in capsys.readouterr().out
    _review(tmp_path, "pending", "rev_p", proposal={"to_status": "active", sv.FIELD: "d" * 64})
    assert validate_synthesis.main([str(tmp_path)]) == 1
    assert "interrupted generation" in capsys.readouterr().out
    _set_artifact(tmp_path, confidence=math.inf)
    assert validate_synthesis.main([str(tmp_path)]) == 1
    assert "invalid field `confidence`" in capsys.readouterr().out
    _set_artifact(tmp_path, confidence=10**400)
    assert validate_synthesis.main([str(tmp_path)]) == 1
    assert "invalid field `confidence`" in capsys.readouterr().out
    _set_artifact(tmp_path, confidence=0.7, version_digest=None)
    assert validate_synthesis.main([str(tmp_path)]) == 1
    assert "invalid field `version_digest`" in capsys.readouterr().out


# --- slice 1 review: verified publication, retry durability, bulk shows what it binds -----------


def _republish_case(tmp_path):
    """A live legacy approval whose page shows the OLD text, re-decided for a newer version."""
    conn = _candidate(tmp_path, status="active")          # page + graph active, old text
    _set_artifact(tmp_path, summary="Newer reviewed summary.", synthesis="Newer reviewed text.")
    _review(tmp_path, "approved", "rev_p", bound=False)
    assert reviews.decide_again(tmp_path / "reviews", "rev_p", decision="approved",
                                version_digest=_digest(tmp_path)) is True
    return conn


def test_already_in_effect_requires_the_decided_text_and_republishes_it(tmp_path):
    conn = _republish_case(tmp_path)
    assert "Newer reviewed text." not in _page(tmp_path)
    recs, res = _apply(tmp_path, conn)
    assert recs["rev_p"]["outcome"] == "applied" and res["promoted"] == 1
    page = _page(tmp_path)
    assert "Newer reviewed text." in page and "Newer reviewed summary." in page
    assert "Across sources solar grows." not in page and "status: active" in page
    # The repaired page is the exact rendering: the next Apply writes nothing.
    path = tmp_path / "wiki" / "Synthesis" / f"{SYN}.md"
    stamp = path.stat().st_mtime_ns
    recs, res = _apply(tmp_path, conn)
    assert recs["rev_p"]["outcome"] == "already_in_effect" and res["promoted"] == 0
    assert path.read_text(encoding="utf-8") == page and path.stat().st_mtime_ns == stamp
    conn.close()


def test_a_matching_page_is_a_true_no_op(tmp_path):
    conn = _candidate(tmp_path, status="active")
    _review(tmp_path, "approved", "rev_p")
    path = tmp_path / "wiki" / "Synthesis" / f"{SYN}.md"
    before, stamp = path.read_text(encoding="utf-8"), path.stat().st_mtime_ns
    recs, res = _apply(tmp_path, conn)
    assert recs["rev_p"]["outcome"] == "already_in_effect" and res["promoted"] == 0
    assert path.read_text(encoding="utf-8") == before and path.stat().st_mtime_ns == stamp
    conn.close()


def test_republished_content_is_verified_not_assumed(tmp_path, monkeypatch):
    """A render that lands the right status but not the decided text is never `applied`."""
    conn = _republish_case(tmp_path)
    real = synthesis._page_content

    def stale(gconn, **kw):                               # the write publishes the old text
        return real(gconn, **{**kw, "synthesis_text": "Across sources solar grows."})

    monkeypatch.setattr(synthesis, "_render_page", lambda gconn, **kw: (
        (tmp_path / "wiki" / "Synthesis" / f"{SYN}.md").write_text(
            stale(gconn, **{k: v for k, v in kw.items() if k != "now"}), encoding="utf-8")
        or True))
    recs, _res = _apply(tmp_path, conn)
    assert recs["rev_p"]["outcome"] == "incomplete"
    assert recs["rev_p"]["reason"] == "postcondition_not_met"
    assert recs["rev_p"]["details"]["problem"]["content"]
    conn.close()


def test_an_unreadable_page_comparison_is_unverified(tmp_path, monkeypatch):
    conn = _republish_case(tmp_path)
    monkeypatch.setattr(synthesis, "_rendering_matches",
                        lambda *a, **k: synthesis.apply_outcomes.UNAVAILABLE)
    recs, _res = _apply(tmp_path, conn)
    assert recs["rev_p"]["outcome"] == "skipped"
    assert recs["rev_p"]["reason"] == "verification_unavailable"
    assert "Newer reviewed text." not in _page(tmp_path)
    conn.close()


def _failing_dir_sync(monkeypatch, tmp_path, *, name="rejected"):
    real = reviews._sync_dir
    target = tmp_path / "reviews" / name

    def flaky(path):
        if Path(path) == target:
            raise OSError("fsync failed")
        return real(path)

    monkeypatch.setattr(reviews, "_sync_dir", flaky)
    return real


def test_a_retry_never_deletes_the_predecessor_before_the_replacement_is_durable(
        tmp_path, monkeypatch):
    _candidate(tmp_path, status="active").close()
    _review(tmp_path, "approved", "rev_p", bound=False)
    shown = _digest(tmp_path)
    real = _failing_dir_sync(monkeypatch, tmp_path)
    for _ in range(3):                                    # the first attempt, then repeated retries
        with pytest.raises(OSError):
            reviews.decide_again(tmp_path / "reviews", "rev_p", decision="rejected",
                                 version_digest=shown)
        assert (tmp_path / "reviews" / "approved" / "rev_p.json").exists()
        assert (tmp_path / "reviews" / "rejected" / "rev_p.json").exists()
        assert len(_audits(tmp_path, "rev_p")) == 1
    monkeypatch.setattr(reviews, "_sync_dir", real)
    synced = []
    monkeypatch.setattr(reviews, "sync_durably", lambda p: synced.append(Path(p)))
    monkeypatch.setattr(reviews, "_sync_dir", lambda p: synced.append(("dir", Path(p))))
    assert reviews.decide_again(tmp_path / "reviews", "rev_p", decision="rejected",
                                version_digest=shown) is False
    (audit,) = _audits(tmp_path, "rev_p")
    # audit flush → replacement file+directory flush → predecessor deletion → both terminal
    # directories flushed
    assert synced == [audit, tmp_path / "reviews" / "rejected" / "rev_p.json",
                      ("dir", tmp_path / "reviews" / "approved"),
                      ("dir", tmp_path / "reviews" / "rejected")]
    assert _location(tmp_path, "rev_p") == ["rejected"]


@pytest.mark.parametrize("damage", ["missing", "unreadable"])
def test_a_retry_without_its_audit_entry_keeps_the_predecessor(tmp_path, monkeypatch, damage):
    _candidate(tmp_path, status="active").close()
    _review(tmp_path, "approved", "rev_p", bound=False)
    shown = _digest(tmp_path)
    real = _failing_dir_sync(monkeypatch, tmp_path)
    with pytest.raises(OSError):
        reviews.decide_again(tmp_path / "reviews", "rev_p", decision="rejected",
                             version_digest=shown)
    monkeypatch.setattr(reviews, "_sync_dir", real)
    (audit,) = _audits(tmp_path, "rev_p")
    if damage == "missing":
        audit.unlink()
    else:
        audit.write_text("{torn", encoding="utf-8")
    with pytest.raises(reviews.DecideAgainRefused):
        reviews.decide_again(tmp_path / "reviews", "rev_p", decision="rejected",
                             version_digest=shown)
    assert sorted(_location(tmp_path, "rev_p")) == ["approved", "rejected"]


def test_a_torn_audit_entry_is_rewritten_before_the_first_move(tmp_path):
    _candidate(tmp_path, status="active").close()
    _review(tmp_path, "approved", "rev_p", bound=False)
    shown = _digest(tmp_path)
    prior = _item(tmp_path, "rev_p")
    key = reviews._decide_again_key("rev_p", prior, "rejected", shown)
    entry = tmp_path / "reviews" / "audit_log" / f"rev_p-decided-again-{key}.json"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("{torn", encoding="utf-8")
    assert reviews.decide_again(tmp_path / "reviews", "rev_p", decision="rejected",
                                version_digest=shown) is True
    rec = json.loads(entry.read_text(encoding="utf-8"))
    assert rec["event"] == "decided_again" and rec["prior_status"] == "approved"


def test_bulk_confirmation_shows_the_version_it_binds(client, tmp_path):
    _candidate(tmp_path).close()
    _review(tmp_path, "pending", "rev_p")
    # Regenerated (same inputs) after the human last looked at the review page.
    _set_artifact(tmp_path, title="Regenerated <title>", summary="Regenerated summary.",
                  synthesis="Regenerated full text.")
    html = client.post("/ui/reviews/bulk/confirm",
                       data={"bulk_action": "reject", "rid": ["rev_p"]}).text
    assert "Regenerated &lt;title&gt;" in html and "Regenerated summary." in html
    assert "Regenerated full text." in html and "<details>" in html
    shown = _digest(tmp_path)
    assert shown[:12] in html and _bulk_form(html)["vd_rev_p"] == shown


@pytest.mark.parametrize("break_it", ["invalid", "other_inputs", "interrupted"])
def test_bulk_confirmation_leaves_out_an_undecidable_version(client, tmp_path, break_it):
    _candidate(tmp_path).close()
    proposal = None
    if break_it == "interrupted":
        proposal = {"to_status": "active", "synthesis_id": SYN, sv.FIELD: "d" * 64}
    _review(tmp_path, "pending", "rev_p", proposal=proposal,
            fingerprint=OTHER_FP if break_it == "other_inputs" else FP)
    if break_it == "invalid":
        _set_artifact(tmp_path, title="")
    html = client.post("/ui/reviews/bulk/confirm",
                       data={"bulk_action": "reject", "rid": ["rev_p"]}).text
    assert "Can’t be decided now" in html or "Can't be decided now" in html
    form = _bulk_form(html)
    assert form["rid"] == [] and "vd_rev_p" not in form


def _recording_dir_sync(monkeypatch, fail):
    """Record every directory flush; raise OSError where `fail(path)` says so."""
    real = reviews._sync_dir
    calls = []

    def sync(path):
        calls.append(Path(path))
        if fail(Path(path)):
            raise OSError("fsync failed")
        return real(path)

    monkeypatch.setattr(reviews, "_sync_dir", sync)
    return calls


@pytest.mark.parametrize("prior,decision", [("approved", "rejected"), ("rejected", "approved")])
def test_a_failed_flush_after_deleting_the_predecessor_is_repeated_on_retry(
        tmp_path, monkeypatch, prior, decision):
    """The predecessor was deleted but its directory flush failed: its absence proves nothing
    durable, so every retry flushes that directory again and never reports completion first."""
    _candidate(tmp_path, status="active").close()
    _review(tmp_path, prior, "rev_p", bound=False)
    shown = _digest(tmp_path)
    prior_dir = tmp_path / "reviews" / prior
    failing = [True]
    calls = _recording_dir_sync(
        monkeypatch, lambda p: failing[0] and p == prior_dir
        and not (prior_dir / "rev_p.json").exists())
    with pytest.raises(OSError):                          # first attempt: deleted, flush failed
        reviews.decide_again(tmp_path / "reviews", "rev_p", decision=decision,
                             version_digest=shown)
    assert _location(tmp_path, "rev_p") == [decision]
    for _ in range(3):                                    # repeated retries, flush still failing
        calls.clear()
        with pytest.raises(OSError):
            reviews.decide_again(tmp_path / "reviews", "rev_p", decision=decision,
                                 version_digest=shown)
        assert prior_dir in calls
    failing[0] = False
    calls.clear()
    assert reviews.decide_again(tmp_path / "reviews", "rev_p", decision=decision,
                                version_digest=shown) is False
    assert prior_dir in calls and (tmp_path / "reviews" / decision) in calls
    assert _location(tmp_path, "rev_p") == [decision]
    assert _item(tmp_path, "rev_p")[sv.DECIDED] == shown and len(_audits(tmp_path, "rev_p")) == 1


def test_a_same_status_retry_tolerates_the_absent_opposite_directory(tmp_path, monkeypatch):
    """Re-deciding an approval as an approval never creates rejected/: on retry its absence is
    not a failure, while the replacement's own directory flush is still repeated."""
    _candidate(tmp_path, status="active").close()
    _review(tmp_path, "approved", "rev_p", bound=False)
    shown = _digest(tmp_path)
    approved_dir, rejected_dir = tmp_path / "reviews" / "approved", tmp_path / "reviews" / "rejected"
    assert not rejected_dir.exists()
    failing = [True]
    calls = _recording_dir_sync(monkeypatch, lambda p: failing[0] and p == approved_dir)
    for _ in range(2):                                    # the first attempt, then a retry
        with pytest.raises(OSError):
            reviews.decide_again(tmp_path / "reviews", "rev_p", decision="approved",
                                 version_digest=shown)
    failing[0] = False
    calls.clear()
    assert reviews.decide_again(tmp_path / "reviews", "rev_p", decision="approved",
                                version_digest=shown) is False
    assert calls[-2:] == [approved_dir, rejected_dir] and not rejected_dir.exists()
    item = _item(tmp_path, "rev_p")
    assert item[sv.DECIDED] == shown and item["approval_id"] != "a" * 32


def test_an_unflushable_existing_terminal_directory_is_not_mistaken_for_absence(
        tmp_path, monkeypatch):
    _candidate(tmp_path, status="active").close()
    _review(tmp_path, "approved", "rev_p", bound=False)
    shown = _digest(tmp_path)
    assert reviews.decide_again(tmp_path / "reviews", "rev_p", decision="approved",
                                version_digest=shown) is True
    rejected_dir = tmp_path / "reviews" / "rejected"
    rejected_dir.mkdir()
    real = reviews._sync_dir

    def denied(path):
        if Path(path) == rejected_dir:
            raise PermissionError("denied")
        return real(path)

    monkeypatch.setattr(reviews, "_sync_dir", denied)
    with pytest.raises(PermissionError):
        reviews.decide_again(tmp_path / "reviews", "rev_p", decision="approved",
                             version_digest=shown)

    def vanished_but_present(path):                       # FileNotFoundError, yet it exists
        if Path(path) == rejected_dir:
            raise FileNotFoundError("racy")
        return real(path)

    monkeypatch.setattr(reviews, "_sync_dir", vanished_but_present)
    with pytest.raises(FileNotFoundError):
        reviews.decide_again(tmp_path / "reviews", "rev_p", decision="approved",
                             version_digest=shown)
