"""ADR-0049 synthesis visibility: hide_synthesis / unhide_synthesis.

Synthesis differs from claims/items: it has a promotion lifecycle (candidate -> active via
propose_synthesis), an active synthesis carries `review_status: approved` (not `none`), and the
synthesis-specific crux is PRESERVATION — a `hidden` synthesis must survive the three generate_syntheses
clobber sites (retraction loop / apply_resolved_syntheses / normal-regen gate), keyed on the AUTHORITATIVE
page status (not just the graph mirror). There is no fan-out when hiding a synthesis ITSELF (nothing renders
[[Synthesis/...]]); a CLAIM hide does fan out to citing synthesis pages (ADR-0049 decision 9).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from fastapi.testclient import TestClient

from app.backend import graph
from app.backend import main as main_module
from app.backend import review_read
from app.backend.config import get_settings
from app.workers import claims, deprecations, synthesis
from app.workers.wiki_render import parse_frontmatter

TOPIC = "itm_aaaaaaaaaaaaaaaa"
SYN = synthesis.synthesis_id(TOPIC)
SID = "src_0123456789abcdef"
CX = "clm_aaaaaaaaaaaaaaaa"


def _graph(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    return graph.connect(gdb)


def _build_claim(tmp_path, conn, cid, *, text):
    """An active, evidenced claim page (so the synthesis's Supporting Evidence [[Claims/...]] resolves)."""
    md = tmp_path / "normalized" / "markdown" / f"{SID}.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(text, encoding="utf-8")
    graph.upsert_node(conn, node_id=cid, node_type="claim", slug=cid, status="active")
    graph.upsert_node(conn, node_id=SID, node_type="source", slug=SID, status="active")
    graph.upsert_assertion(conn, src_id=cid, dst_id=SID, edge_type="derived_from", asserted_by="llm",
                           status="active", evidence_source_id=SID, evidence_char_start=0,
                           evidence_char_end=len(text))
    conn.commit()
    claims.recompose_claim(conn, cid=cid, claims_dir=tmp_path / "wiki" / "Claims",
                           reviews_dir=tmp_path / "reviews",
                           markdown_dir=tmp_path / "normalized" / "markdown", now="t", text_hint=text)
    conn.commit()


def _active_synthesis(tmp_path, conn, *, claim_ids=(CX,), status="active"):
    """Build a synthesis (graph node + page + artifact + derived_from edges) at the given status. An
    `active` synthesis is the promoted convention: review_status approved."""
    enrich = tmp_path / "normalized" / "enrichment"
    enrich.mkdir(parents=True, exist_ok=True)
    syn_dir = tmp_path / "wiki" / "Synthesis"
    for cid in claim_ids:
        _build_claim(tmp_path, conn, cid, text="The sky is blue today.")
    graph.upsert_node(conn, node_id=TOPIC, node_type="item", slug=TOPIC, status="active",
                      item_type="method_technique")
    graph.upsert_node(conn, node_id=SYN, node_type="synthesis", slug=SYN, status=status)
    for cid in claim_ids:
        graph.upsert_assertion(conn, src_id=SYN, dst_id=cid, edge_type="derived_from",
                               asserted_by="llm", status="active")
    graph.upsert_assertion(conn, src_id=SYN, dst_id=TOPIC, edge_type="related_to",
                           asserted_by="llm", status="active")
    conn.commit()
    (enrich / f"{TOPIC}.synthesis.json").write_text(json.dumps({
        "node_id": SYN, "topic_node_id": TOPIC, "title": "Solar trends",
        "summary": "Solar adoption is rising.", "synthesis": "Across sources solar grows.",
        "confidence": 0.7, "input_fingerprint": "fp"}), encoding="utf-8")
    synthesis._render_page(
        conn, syn_id=SYN, topic_node=TOPIC, title="Solar trends", summary="Solar adoption is rising.",
        synthesis_text="Across sources solar grows.", confidence=0.7, status=status,
        review_status="approved" if status == "active" else "pending", synthesis_dir=syn_dir, now="t")
    conn.commit()


def _syn_fm(tmp_path):
    return parse_frontmatter((tmp_path / "wiki" / "Synthesis" / f"{SYN}.md").read_text())


def _approve(tmp_path, rtype, *, to_status, rid="rev_s"):
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": rtype, "status": "approved",
        "subject": {"node_id": SYN, "page": f"Synthesis/{SYN}.md"},
        "proposal": {"to_status": to_status}, "context": {"node_type": "synthesis"}}), encoding="utf-8")


def _apply_hide(tmp_path, conn):
    return synthesis.apply_hidden_syntheses(
        conn, tmp_path / "reviews", synthesis_dir=tmp_path / "wiki" / "Synthesis",
        enrichment_dir=tmp_path / "normalized" / "enrichment")


def _apply_unhide(tmp_path, conn):
    return synthesis.apply_unhidden_syntheses(
        conn, tmp_path / "reviews", synthesis_dir=tmp_path / "wiki" / "Synthesis",
        enrichment_dir=tmp_path / "normalized" / "enrichment")


class _SpyClient:
    """Records parse() calls; parse MUST NOT run for a hidden synthesis (the regen guard)."""
    def __init__(self, *, has_key):
        self._has_key = has_key
        self.parsed = 0

    def provider_available(self, model_ref):
        return self._has_key

    def parse(self, *a, **k):
        self.parsed += 1
        raise AssertionError("parse() must not run for a hidden synthesis")


# --- executor: hide / unhide -----------------------------------------------


def test_hide_active_synthesis_flips_page_and_graph(tmp_path):
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn)
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    res = _apply_hide(tmp_path, conn)
    conn.commit()
    assert res["applied"] == 1 and res["changed_pages"] == [f"Synthesis/{SYN}.md"]
    fm = _syn_fm(tmp_path)
    assert fm["status"] == "hidden" and fm["review_status"] == "approved"
    assert graph.get_node(conn, SYN)["status"] == "hidden"
    conn.close()


def test_hide_non_active_synthesis_is_typed_skip(tmp_path):
    # A candidate synthesis is governed by its propose_synthesis review, not hide -> typed skip.
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, status="candidate")
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    res = _apply_hide(tmp_path, conn)
    assert res["applied"] == 0
    assert res["skipped"] == [{"review_id": "rev_s", "reason": "synthesis_not_active"}]
    assert _syn_fm(tmp_path)["status"] == "candidate"   # untouched
    conn.close()


def test_unhide_restores_active_with_review_status_approved(tmp_path):
    # ADR-0049: unhide restores `review_status: approved` (the synthesis convention), NOT `none`.
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn)
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    _apply_hide(tmp_path, conn)
    conn.commit()
    _approve(tmp_path, "unhide_synthesis", to_status="active", rid="rev_u")
    res = _apply_unhide(tmp_path, conn)
    conn.commit()
    assert res["applied"] == 1
    fm = _syn_fm(tmp_path)
    assert fm["status"] == "active" and fm["review_status"] == "approved"
    assert graph.get_node(conn, SYN)["status"] == "active"
    conn.close()


def test_unhide_non_hidden_is_idempotent_no_op(tmp_path):
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn)                    # active, not hidden
    _approve(tmp_path, "unhide_synthesis", to_status="active", rid="rev_u")
    res = _apply_unhide(tmp_path, conn)
    assert res["applied"] == 0 and res["normalized"] == 0 and res["skipped"] == []
    conn.close()


def test_hidden_render_keeps_sections_with_banner(tmp_path):
    # The hidden page keeps Supporting Evidence + the [[Claims/...]] links under a suppression banner.
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=(CX,))
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    _apply_hide(tmp_path, conn)
    conn.commit()
    txt = (tmp_path / "wiki" / "Synthesis" / f"{SYN}.md").read_text()
    assert "Synthesis hidden — suppressed from default discovery" in txt
    assert "## Supporting Evidence" in txt and f"[[Claims/{CX}|" in txt
    conn.close()


def test_tampered_topic_node_is_typed_skip_not_misrendered(tmp_path):
    # SECURITY (untrusted derived-page boundary): a tampered topic_node pointing at ANOTHER existing
    # artifact must NOT re-render this synthesis with mismatched prose — synthesis_id(topic_node) != nid.
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn)
    other_topic = "itm_bbbbbbbbbbbbbbbb"
    (tmp_path / "normalized" / "enrichment" / f"{other_topic}.synthesis.json").write_text(json.dumps({
        "node_id": synthesis.synthesis_id(other_topic), "topic_node_id": other_topic,
        "title": "EVIL", "summary": "evil summary", "synthesis": "evil prose", "confidence": 0.1}),
        encoding="utf-8")
    p = tmp_path / "wiki" / "Synthesis" / f"{SYN}.md"
    p.write_text(p.read_text().replace(f'topic_node: "{TOPIC}"', f'topic_node: "{other_topic}"', 1),
                 encoding="utf-8")
    conn.commit()
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    res = _apply_hide(tmp_path, conn)
    assert res["applied"] == 0
    assert res["skipped"] == [{"review_id": "rev_s", "reason": "synthesis_topic_mismatch"}]
    assert "EVIL" not in (tmp_path / "wiki" / "Synthesis" / f"{SYN}.md").read_text()  # not misrendered
    conn.close()


def test_artifact_node_id_mismatch_is_typed_skip(tmp_path):
    # The artifact's own node_id must match (defence-in-depth beyond synthesis_id(topic_node) == nid).
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn)
    apath = tmp_path / "normalized" / "enrichment" / f"{TOPIC}.synthesis.json"
    a = json.loads(apath.read_text())
    a["node_id"] = "syn_ffffffffffffffff"
    apath.write_text(json.dumps(a), encoding="utf-8")
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    res = _apply_hide(tmp_path, conn)
    assert res["applied"] == 0
    assert res["skipped"] == [{"review_id": "rev_s", "reason": "synthesis_artifact_mismatch"}]
    assert _syn_fm(tmp_path)["status"] == "active"      # not re-rendered
    conn.close()


def test_partial_page_hidden_survives_generate_resolved_and_retraction(tmp_path):
    # ADR-0049: page-hidden / graph-active partial state. The PAGE is authoritative, so neither a rejected
    # proposal (apply_resolved_syntheses) nor the retraction loop may clobber the hidden page. Skip-only:
    # the graph mirror is left as-is (drift repair belongs to the executor/validator, not the generator).
    (tmp_path / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=())      # graph active; topic ineligible (no claims)
    p = tmp_path / "wiki" / "Synthesis" / f"{SYN}.md"
    p.write_text(p.read_text().replace("status: active", "status: hidden", 1), encoding="utf-8")  # page only
    conn.commit()
    conn.close()
    (tmp_path / "reviews" / "rejected").mkdir(parents=True, exist_ok=True)
    (tmp_path / "reviews" / "rejected" / "rev_p.json").write_text(json.dumps({
        "review_id": "rev_p", "type": "propose_synthesis", "status": "rejected",
        "subject": {"topic_node_id": TOPIC, "fingerprint": "fp"}}), encoding="utf-8")
    summary = synthesis.generate_syntheses(
        tmp_path, client=_SpyClient(has_key=False), model_ref="m", rebuild_index=False, record_job=False)
    assert summary["rejected"] == 0 and summary["retracted"] == 0   # neither clobbered the hidden page
    assert _syn_fm(tmp_path)["status"] == "hidden"       # authoritative page preserved
    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert graph.get_node(conn, SYN)["status"] == "active"   # graph mirror untouched (skip-only, no repair)
    conn.close()


def test_partial_page_hidden_survives_regen_gate(tmp_path, monkeypatch):
    # ADR-0049: the regen gate honors the authoritative page-hidden status even when the graph is active.
    (tmp_path / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=())
    p = tmp_path / "wiki" / "Synthesis" / f"{SYN}.md"
    p.write_text(p.read_text().replace("status: active", "status: hidden", 1), encoding="utf-8")
    conn.commit()
    conn.close()
    fake_topic = {"node_id": TOPIC, "node_type": "item", "slug": TOPIC, "title": "Solar trends",
                  "claims": [{"claim_id": CX, "claim_text": "x",
                              "citations": [{"source_id": SID, "char_start": 0, "char_end": 1}],
                              "sources": [SID]}],
                  "disagreements": []}
    monkeypatch.setattr(synthesis, "eligible_topics", lambda *a, **k: [fake_topic])
    client = _SpyClient(has_key=True)
    synthesis.generate_syntheses(
        tmp_path, client=client, model_ref="m", rebuild_index=False, record_job=False)
    assert client.parsed == 0                            # regen gate honored page-hidden authority
    assert _syn_fm(tmp_path)["status"] == "hidden"


def test_artifact_missing_is_typed_skip(tmp_path):
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn)
    (tmp_path / "normalized" / "enrichment" / f"{TOPIC}.synthesis.json").unlink()
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    res = _apply_hide(tmp_path, conn)
    assert res["applied"] == 0
    assert res["skipped"] == [{"review_id": "rev_s", "reason": "synthesis_artifact_missing"}]
    assert _syn_fm(tmp_path)["status"] == "active"      # untouched
    conn.close()


# --- partial-state typed skips ---------------------------------------------


def test_hide_partial_state_is_typed_skip_not_silent(tmp_path):
    # graph hidden but page active (drift) -> typed partial_hide_state skip.
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn)
    graph.upsert_node(conn, node_id=SYN, node_type="synthesis", slug=SYN, status="hidden")  # graph only
    conn.commit()
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    res = _apply_hide(tmp_path, conn)
    assert res["skipped"] == [{"review_id": "rev_s", "reason": "partial_hide_state"}]
    conn.close()


def test_unhide_partial_state_is_typed_skip_not_silent(tmp_path):
    # page hidden but graph active (drift) -> typed partial_unhide_state skip.
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn)
    p = tmp_path / "wiki" / "Synthesis" / f"{SYN}.md"
    p.write_text(p.read_text().replace("status: active", "status: hidden", 1), encoding="utf-8")
    conn.commit()                                        # graph stays active
    _approve(tmp_path, "unhide_synthesis", to_status="active", rid="rev_u")
    res = _apply_unhide(tmp_path, conn)
    assert res["skipped"] == [{"review_id": "rev_u", "reason": "partial_unhide_state"}]
    conn.close()


# --- preservation: the three generate_syntheses clobber sites --------------


def test_preservation_retraction_loop_skips_hidden(tmp_path):
    # Site 1: an ineligible hidden synthesis is NOT retracted by the generate pass (hide wins; a tombstone
    # would re-expose it). Keyless run: retraction runs, regen does not.
    (tmp_path / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=())      # topic has no claims -> ineligible
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    _apply_hide(tmp_path, conn)
    conn.commit()
    conn.close()
    summary = synthesis.generate_syntheses(
        tmp_path, client=_SpyClient(has_key=False), model_ref="m", rebuild_index=False, record_job=False)
    assert summary["retracted"] == 0
    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert graph.get_node(conn, SYN)["status"] == "hidden"     # survived the retraction loop
    conn.close()
    assert _syn_fm(tmp_path)["status"] == "hidden"


def test_preservation_apply_resolved_cannot_flip_hidden(tmp_path):
    # Site 2: a lingering APPROVED propose_synthesis must not promote a hidden synthesis to active.
    (tmp_path / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=())
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    _apply_hide(tmp_path, conn)
    conn.commit()
    conn.close()
    (tmp_path / "reviews" / "approved" / "rev_p.json").write_text(json.dumps({
        "review_id": "rev_p", "type": "propose_synthesis", "status": "approved",
        "subject": {"topic_node_id": TOPIC, "fingerprint": "fp"},
        "proposal": {"to_status": "active"}}), encoding="utf-8")
    summary = synthesis.generate_syntheses(
        tmp_path, client=_SpyClient(has_key=False), model_ref="m", rebuild_index=False, record_job=False)
    assert summary["promoted"] == 0
    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert graph.get_node(conn, SYN)["status"] == "hidden"     # not flipped active
    conn.close()


def test_preservation_regen_gate_skips_hidden(tmp_path, monkeypatch):
    # Site 3: the normal-regen gate never regenerates a hidden synthesis -> no LLM call, no node reset.
    (tmp_path / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=())
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    _apply_hide(tmp_path, conn)
    conn.commit()
    conn.close()
    # Surface the hidden synthesis's topic as eligible so the regen loop reaches it; the guard must skip.
    fake_topic = {"node_id": TOPIC, "node_type": "item", "slug": TOPIC, "title": "Solar trends",
                  "claims": [{"claim_id": CX, "claim_text": "x",
                              "citations": [{"source_id": SID, "char_start": 0, "char_end": 1}],
                              "sources": [SID]}],
                  "disagreements": []}
    monkeypatch.setattr(synthesis, "eligible_topics", lambda *a, **k: [fake_topic])
    client = _SpyClient(has_key=True)
    summary = synthesis.generate_syntheses(
        tmp_path, client=client, model_ref="m", rebuild_index=False, record_job=False)
    assert client.parsed == 0                            # no LLM call on a hidden synthesis
    assert summary["syntheses_written"] == 0
    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert graph.get_node(conn, SYN)["status"] == "hidden"     # not reset to candidate
    conn.close()


# --- projector + reopen safety ---------------------------------------------


def _effect(tmp_path, conn, rtype, *, status="approved"):
    item = {"type": rtype, "status": status,
            "subject": {"node_id": SYN, "page": f"Synthesis/{SYN}.md"},
            "context": {"node_type": "synthesis"}}
    fn = (review_read._effect_hide_synthesis if rtype == "hide_synthesis"
          else review_read._effect_unhide_synthesis)
    return fn(item, conn, tmp_path / "wiki")


def test_hide_projector_pending_and_synthesis_not_active(tmp_path):
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn)
    assert _effect(tmp_path, conn, "hide_synthesis")[0] == review_read.PENDING_APPLY
    graph.upsert_node(conn, node_id=SYN, node_type="synthesis", slug=SYN, status="candidate")
    conn.commit()
    # page active, graph candidate -> neither hidden -> PENDING_APPLY + synthesis_not_active warning
    assert _effect(tmp_path, conn, "hide_synthesis")[1] == ["synthesis_not_active"]
    conn.close()


def test_hide_projector_both_hidden_pending_is_unknown_not_effected(tmp_path):
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn)
    p = tmp_path / "wiki" / "Synthesis" / f"{SYN}.md"
    p.write_text(p.read_text().replace("status: active", "status: hidden", 1)
                 .replace("review_status: approved", "review_status: pending", 1), encoding="utf-8")
    graph.upsert_node(conn, node_id=SYN, node_type="synthesis", slug=SYN, status="hidden")
    conn.commit()
    status, warnings = _effect(tmp_path, conn, "hide_synthesis")
    assert status == review_read.UNKNOWN and warnings == ["partial_hide_state"]
    conn.close()


def test_unhide_projector_effected_when_not_hidden(tmp_path):
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn)                    # active (not hidden)
    assert _effect(tmp_path, conn, "unhide_synthesis")[0] == review_read.EFFECTED
    conn.close()


# --- API: apply + summary + graph-required + reindex posture + reopen -------


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


def test_api_apply_hides_synthesis_with_summary(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module.retention, "reindex_keyword", lambda root: None)
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn)
    conn.commit()
    conn.close()
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    body = client.post("/reviews/apply").json()
    assert body["status"] == "applied"
    assert body["summary"]["synthesis_hidden"]["applied"] == 1


def test_api_synthesis_hide_graph_required_503(client, tmp_path):
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    assert not (tmp_path / "db" / "graph.sqlite").exists()
    assert client.post("/reviews/apply").status_code == 503


def test_api_synthesis_hide_reindex_failure_is_non_clean(client, tmp_path, monkeypatch):
    def boom(root):
        raise RuntimeError("reindex blew up")
    monkeypatch.setattr(main_module.retention, "reindex_keyword", boom)
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn)
    conn.commit()
    conn.close()
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    body = client.post("/reviews/apply").json()
    assert body["status"] == "validation_failed"
    assert "synthesis_hide_discovery_suppression_not_guaranteed" in body["warnings"]


def test_reopen_blocked_for_partial_synthesis_hide(client, tmp_path):
    # page XOR graph hidden -> UNKNOWN partial_hide_state -> reopen blocked 409.
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn)
    p = tmp_path / "wiki" / "Synthesis" / f"{SYN}.md"
    p.write_text(p.read_text().replace("status: active", "status: hidden", 1), encoding="utf-8")
    conn.commit()                                        # graph stays active
    conn.close()
    _approve(tmp_path, "hide_synthesis", to_status="hidden", rid="rev_s")
    r = client.post("/reviews/rev_s/reopen", json={"reason": "undo"})
    assert r.status_code == 409 and "effect_unknown_repair_read_model" in r.json()["detail"]


def test_api_unhide_synthesis_reindex_failure_is_non_clean(client, tmp_path, monkeypatch):
    def boom(root):
        raise RuntimeError("reindex blew up")
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn)
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    _apply_hide(tmp_path, conn)                          # synthesis now hidden (page+graph)
    conn.commit()
    conn.close()
    monkeypatch.setattr(main_module.retention, "reindex_keyword", boom)
    (tmp_path / "reviews" / "approved" / "rev_s.json").unlink()   # drop the hide review (effected)
    _approve(tmp_path, "unhide_synthesis", to_status="active", rid="rev_u")
    body = client.post("/reviews/apply").json()
    assert body["status"] == "validation_failed"
    assert "synthesis_unhide_discovery_restoration_not_guaranteed" in body["warnings"]
    assert _syn_fm(tmp_path)["status"] == "active"       # mutation still written


# --- ADR-0049 fan-out: hidden claim dropped from a citing synthesis ---------

CY = "clm_bbbbbbbbbbbbbbbb"


def test_hide_claim_fans_out_to_citing_synthesis(tmp_path):
    # A hidden claim drops from a citing synthesis's Supporting Evidence; a visible co-claim stays; the
    # derived_from edge remains active in the graph (SoT). The claim executor reports affected_syntheses.
    from app.workers import deprecations
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=(CX, CY))
    txt0 = (tmp_path / "wiki" / "Synthesis" / f"{SYN}.md").read_text()
    assert f"[[Claims/{CX}|" in txt0 and f"[[Claims/{CY}|" in txt0   # baseline: both linked
    (tmp_path / "reviews" / "approved").mkdir(parents=True, exist_ok=True)
    (tmp_path / "reviews" / "approved" / "rev_c.json").write_text(json.dumps({
        "review_id": "rev_c", "type": "hide_claim", "status": "approved",
        "subject": {"node_id": CX, "page": f"Claims/{CX}.md"},
        "proposal": {"to_status": "hidden"}, "context": {"node_type": "claim"}}), encoding="utf-8")
    res = deprecations.apply_hidden_claims(conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki",
                                           markdown_dir=tmp_path / "normalized" / "markdown")
    conn.commit()
    assert res["affected_syntheses"] == [SYN]
    assert synthesis.rerender_synthesis_page(
        conn, SYN, synthesis_dir=tmp_path / "wiki" / "Synthesis",
        enrichment_dir=tmp_path / "normalized" / "enrichment")
    conn.commit()
    txt1 = (tmp_path / "wiki" / "Synthesis" / f"{SYN}.md").read_text()
    assert f"[[Claims/{CX}" not in txt1 and f"[[Claims/{CY}|" in txt1   # hidden dropped, visible kept
    edge = [e for e in graph.outgoing_active(conn, SYN)
            if e["edge_type"] == "derived_from" and e["dst_id"] == CX]
    assert edge and edge[0]["status"] == "active"        # edge preserved (no surgery)
    conn.close()


def test_api_hide_claim_rerenders_citing_synthesis(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module.retention, "reindex_keyword", lambda root: None)
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=(CX,))
    conn.commit()
    conn.close()
    (tmp_path / "reviews" / "approved").mkdir(parents=True, exist_ok=True)
    (tmp_path / "reviews" / "approved" / "rev_c.json").write_text(json.dumps({
        "review_id": "rev_c", "type": "hide_claim", "status": "approved",
        "subject": {"node_id": CX, "page": f"Claims/{CX}.md"},
        "proposal": {"to_status": "hidden"}, "context": {"node_type": "claim"}}), encoding="utf-8")
    body = client.post("/reviews/apply").json()
    assert body["status"] == "applied"                   # validators pass (projection now filters hidden)
    txt = (tmp_path / "wiki" / "Synthesis" / f"{SYN}.md").read_text()
    assert f"[[Claims/{CX}" not in txt                  # fan-out dropped the hidden claim


# --- ADR-0049 decision 10: synthesis evidence-suppression (active <-> evidence_hidden) -----


def _hide_claim_and_fanout(tmp_path, conn, cid):
    (tmp_path / "reviews" / "approved").mkdir(parents=True, exist_ok=True)
    (tmp_path / "reviews" / "approved" / f"hc_{cid}.json").write_text(json.dumps({
        "review_id": f"hc_{cid}", "type": "hide_claim", "status": "approved",
        "subject": {"node_id": cid, "page": f"Claims/{cid}.md"},
        "proposal": {"to_status": "hidden"}, "context": {"node_type": "claim"}}), encoding="utf-8")
    res = deprecations.apply_hidden_claims(conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki",
                                           markdown_dir=tmp_path / "normalized" / "markdown")
    conn.commit()
    for sid in res["affected_syntheses"]:                # run_apply does this re-render after the graph block
        synthesis.rerender_synthesis_page(conn, sid, synthesis_dir=tmp_path / "wiki" / "Synthesis",
                                          enrichment_dir=tmp_path / "normalized" / "enrichment")
    conn.commit()
    return res


def _unhide_claim_and_fanout(tmp_path, conn, cid):
    (tmp_path / "reviews" / "approved").mkdir(parents=True, exist_ok=True)
    (tmp_path / "reviews" / "approved" / f"hc_{cid}.json").unlink(missing_ok=True)
    (tmp_path / "reviews" / "approved" / f"uc_{cid}.json").write_text(json.dumps({
        "review_id": f"uc_{cid}", "type": "unhide_claim", "status": "approved",
        "subject": {"node_id": cid, "page": f"Claims/{cid}.md"},
        "proposal": {"to_status": "active"}, "context": {"node_type": "claim"}}), encoding="utf-8")
    res = deprecations.apply_unhidden_claims(conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki",
                                             markdown_dir=tmp_path / "normalized" / "markdown")
    conn.commit()
    for sid in res["affected_syntheses"]:
        synthesis.rerender_synthesis_page(conn, sid, synthesis_dir=tmp_path / "wiki" / "Synthesis",
                                          enrichment_dir=tmp_path / "normalized" / "enrichment")
    conn.commit()
    return res


def test_claim_hide_suppresses_synthesis_to_evidence_hidden(tmp_path):
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=(CX,))
    assert graph.get_node(conn, SYN)["status"] == "active"
    res = _hide_claim_and_fanout(tmp_path, conn, CX)
    assert res["affected_syntheses"] == [SYN]
    assert graph.get_node(conn, SYN)["status"] == "evidence_hidden"
    txt = (tmp_path / "wiki" / "Synthesis" / f"{SYN}.md").read_text()
    assert "Synthesis suppressed — supporting evidence hidden" in txt
    assert f"[[Claims/{CX}" not in txt                 # hidden claim dropped from Supporting Evidence
    assert any(e["dst_id"] == CX and e["status"] == "active"   # derived_from edge preserved (graph SoT)
               for e in graph.outgoing_active(conn, SYN) if e["edge_type"] == "derived_from")
    conn.close()


def test_claim_unhide_restores_evidence_hidden_synthesis_to_active(tmp_path):
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=(CX,))
    _hide_claim_and_fanout(tmp_path, conn, CX)
    assert graph.get_node(conn, SYN)["status"] == "evidence_hidden"
    _unhide_claim_and_fanout(tmp_path, conn, CX)
    assert graph.get_node(conn, SYN)["status"] == "active"     # evidence visible again -> restored
    assert f"[[Claims/{CX}|" in (tmp_path / "wiki" / "Synthesis" / f"{SYN}.md").read_text()
    conn.close()


def test_claim_unhide_does_not_restore_while_another_claim_still_hidden(tmp_path):
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=(CX, CY))
    _hide_claim_and_fanout(tmp_path, conn, CX)
    _hide_claim_and_fanout(tmp_path, conn, CY)
    assert graph.get_node(conn, SYN)["status"] == "evidence_hidden"
    _unhide_claim_and_fanout(tmp_path, conn, CX)               # CY still hidden
    assert graph.get_node(conn, SYN)["status"] == "evidence_hidden"   # NOT restored yet
    _unhide_claim_and_fanout(tmp_path, conn, CY)
    assert graph.get_node(conn, SYN)["status"] == "active"     # all supporting evidence visible
    conn.close()


def test_operator_hide_synthesis_wins_over_claim_unhide(tmp_path):
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=(CX,))
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    _apply_hide(tmp_path, conn)
    conn.commit()
    assert graph.get_node(conn, SYN)["status"] == "hidden"
    _hide_claim_and_fanout(tmp_path, conn, CX)
    assert graph.get_node(conn, SYN)["status"] == "hidden"     # operator hide preserved
    _unhide_claim_and_fanout(tmp_path, conn, CX)
    assert graph.get_node(conn, SYN)["status"] == "hidden"     # still operator-hidden, not auto-restored
    conn.close()


def test_operator_unhide_with_hidden_evidence_restores_to_evidence_hidden(tmp_path):
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=(CX,))
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    _apply_hide(tmp_path, conn)
    conn.commit()
    _hide_claim_and_fanout(tmp_path, conn, CX)                 # SYN stays operator-hidden; CX now hidden
    assert graph.get_node(conn, SYN)["status"] == "hidden"
    (tmp_path / "reviews" / "approved" / "rev_s.json").unlink()
    _approve(tmp_path, "unhide_synthesis", to_status="active", rid="rev_u")
    res = _apply_unhide(tmp_path, conn)
    conn.commit()
    assert res["applied"] == 1
    assert graph.get_node(conn, SYN)["status"] == "evidence_hidden"   # evidence still hidden -> not active
    conn.close()


def test_generate_pass_preserves_evidence_hidden(tmp_path):
    (tmp_path / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=(CX,))
    _hide_claim_and_fanout(tmp_path, conn, CX)
    assert graph.get_node(conn, SYN)["status"] == "evidence_hidden"
    conn.close()
    synthesis.generate_syntheses(
        tmp_path, client=_SpyClient(has_key=False), model_ref="m", rebuild_index=False, record_job=False)
    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert graph.get_node(conn, SYN)["status"] == "evidence_hidden"   # not retracted/regenerated
    conn.close()


def test_validate_projection_passes_for_evidence_hidden_synthesis(tmp_path):
    import validate_projection
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=(CX,))
    _hide_claim_and_fanout(tmp_path, conn, CX)
    assert graph.get_node(conn, SYN)["status"] == "evidence_hidden"
    conn.close()
    assert validate_projection.main([str(tmp_path)]) == 0


def test_api_claim_hide_suppresses_synthesis_and_audits_it(client, tmp_path):
    import sqlite3

    from app.backend import keyword_index
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=(CX,))
    conn.commit()
    conn.close()
    keyword_index.reindex(tmp_path, force=True)

    def nav_row():
        c = sqlite3.connect(tmp_path / "indexes" / "keyword" / "keyword.sqlite")
        try:
            return c.execute(
                "SELECT status, answer_eligible FROM navigation WHERE node_id = ?", (SYN,)).fetchone()
        finally:
            c.close()

    assert nav_row() == ("active", "1")                  # baseline: discoverable + answer-eligible
    (tmp_path / "reviews" / "approved").mkdir(parents=True, exist_ok=True)
    (tmp_path / "reviews" / "approved" / "hc.json").write_text(json.dumps({
        "review_id": "hc", "type": "hide_claim", "status": "approved",
        "subject": {"node_id": CX, "page": f"Claims/{CX}.md"},
        "proposal": {"to_status": "hidden"}, "context": {"node_type": "claim"}}), encoding="utf-8")
    body = client.post("/reviews/apply").json()
    assert body["status"] == "applied"
    assert body["summary"]["synthesis_evidence"]["suppressed"] == 1   # audited in the apply summary
    assert nav_row() == ("evidence_hidden", "0")         # not a normal active answer-eligible page anymore


def _approve_hide_claim(tmp_path, cid, *, rid="hc"):
    (tmp_path / "reviews" / "approved").mkdir(parents=True, exist_ok=True)
    (tmp_path / "reviews" / "approved" / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": "hide_claim", "status": "approved",
        "subject": {"node_id": cid, "page": f"Claims/{cid}.md"},
        "proposal": {"to_status": "hidden"}, "context": {"node_type": "claim"}}), encoding="utf-8")


def test_apply_batch_hide_claim_and_hide_synthesis_operator_wins(client, tmp_path, monkeypatch):
    # Apply ORDER: explicit hide_synthesis runs after the claim flip but BEFORE the claim fan-out, so an
    # operator hide lands on the still-active synthesis and the fan-out preserves `hidden` (decision 10).
    monkeypatch.setattr(main_module.retention, "reindex_keyword", lambda root: None)
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=(CX,))
    conn.commit()
    conn.close()
    _approve_hide_claim(tmp_path, CX)
    _approve(tmp_path, "hide_synthesis", to_status="hidden")    # SAME batch
    body = client.post("/reviews/apply").json()
    assert body["status"] == "applied"
    assert body["summary"]["synthesis_hidden"]["applied"] == 1  # operator hide applied
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert graph.get_node(gconn, SYN)["status"] == "hidden"     # operator hidden WINS, not evidence_hidden
    gconn.close()


def test_apply_batch_hide_claim_and_unhide_synthesis_lands_evidence_hidden(client, tmp_path, monkeypatch):
    # Operator unhide of an operator-hidden synthesis whose evidence is being hidden in the SAME batch ->
    # lands on evidence_hidden (the evidence suppression outlives the operator unhide).
    monkeypatch.setattr(main_module.retention, "reindex_keyword", lambda root: None)
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=(CX,))
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    _apply_hide(tmp_path, conn)                                 # pre-state: operator-hidden
    conn.commit()
    conn.close()
    (tmp_path / "reviews" / "approved" / "rev_s.json").unlink()  # the hide is now effected/old
    _approve_hide_claim(tmp_path, CX)
    _approve(tmp_path, "unhide_synthesis", to_status="active", rid="rev_u")
    body = client.post("/reviews/apply").json()
    assert body["status"] == "applied"
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert graph.get_node(gconn, SYN)["status"] == "evidence_hidden"   # unhid operator, evidence still hidden
    gconn.close()


def test_claim_hide_fanout_missing_artifact_is_non_clean_and_stale(client, tmp_path, monkeypatch):
    # The fan-out can't re-render a synthesis whose artifact is gone -> non-clean (suppression not
    # guaranteed) + a structured `unreconciled` audit; the synthesis page is left stale (active + link).
    monkeypatch.setattr(main_module.retention, "reindex_keyword", lambda root: None)
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=(CX,))
    conn.commit()
    conn.close()
    (tmp_path / "normalized" / "enrichment" / f"{TOPIC}.synthesis.json").unlink()  # artifact gone
    _approve_hide_claim(tmp_path, CX)
    body = client.post("/reviews/apply").json()
    assert body["status"] == "validation_failed"
    assert "synthesis_evidence_suppression_not_guaranteed" in body["warnings"]
    assert body["summary"]["synthesis_evidence"]["unreconciled"] == 1
    txt = (tmp_path / "wiki" / "Synthesis" / f"{SYN}.md").read_text()
    assert "status: active" in txt and f"[[Claims/{CX}|" in txt   # stale (un-reconciled)


def test_repair_then_rerun_reconciles_unreconciled_synthesis(client, tmp_path, monkeypatch):
    # repair-then-rerun posture: a fan-out that fails once (missing artifact) is retried on the next apply
    # of the still-approved claim hide (affected_syntheses recomputed even for an effected claim), and a
    # steady-state apply afterwards is clean with NO page churn / reindex (change-detecting rerender).
    reindex_calls = []
    monkeypatch.setattr(main_module.retention, "reindex_keyword",
                        lambda root: reindex_calls.append(root))
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=(CX,))
    conn.commit()
    conn.close()
    art = tmp_path / "normalized" / "enrichment" / f"{TOPIC}.synthesis.json"
    saved = art.read_text()
    art.unlink()                                         # artifact gone -> first fan-out can't reconcile
    _approve_hide_claim(tmp_path, CX)

    b1 = client.post("/reviews/apply").json()            # 1. claim hidden, synthesis unreconcilable
    assert b1["status"] == "validation_failed"
    assert b1["summary"]["synthesis_evidence"]["unreconciled"] == 1
    assert "synthesis_evidence_suppression_not_guaranteed" in b1["warnings"]
    assert "status: active" in (tmp_path / "wiki" / "Synthesis" / f"{SYN}.md").read_text()

    art.write_text(saved, encoding="utf-8")              # 2. repair + rerun (claim hide still approved)
    b2 = client.post("/reviews/apply").json()
    assert b2["status"] == "applied"
    assert b2["summary"]["synthesis_evidence"] == {"suppressed": 1, "restored": 0, "unreconciled": 0}
    assert "synthesis_evidence_suppression_not_guaranteed" not in b2["warnings"]
    txt = (tmp_path / "wiki" / "Synthesis" / f"{SYN}.md").read_text()
    assert "status: evidence_hidden" in txt and f"[[Claims/{CX}" not in txt
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert graph.get_node(gconn, SYN)["status"] == "evidence_hidden"
    gconn.close()

    n = len(reindex_calls)
    b3 = client.post("/reviews/apply").json()            # 3. steady state: clean, no churn
    assert b3["status"] == "applied"
    assert b3["summary"]["synthesis_evidence"] == {"suppressed": 0, "restored": 0, "unreconciled": 0}
    assert len(reindex_calls) == n                       # no reindex on a no-op steady-state apply


def test_fanout_preserves_page_hidden_over_stale_graph_active(tmp_path):
    # page-hidden / graph-active partial state: the fan-out reads the AUTHORITATIVE page status, so an
    # operator-hidden synthesis is NOT downgraded to evidence_hidden — and the graph mirror is repaired.
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=(CX,))
    p = tmp_path / "wiki" / "Synthesis" / f"{SYN}.md"
    p.write_text(p.read_text().replace("status: active", "status: hidden", 1), encoding="utf-8")  # page only
    assert graph.get_node(conn, SYN)["status"] == "active"     # graph mirror still active (drift)
    _hide_claim_and_fanout(tmp_path, conn, CX)
    assert _syn_fm(tmp_path)["status"] == "hidden"             # page authority preserved (operator hidden wins)
    assert graph.get_node(conn, SYN)["status"] == "hidden"     # graph mirror repaired to match the page
    conn.close()


def test_fanout_graph_only_repair_triggers_reindex_and_is_clean(client, tmp_path, monkeypatch):
    # page already evidence_hidden, graph drifted back to active: a rerun repairs the graph mirror AND
    # triggers reindex (a graph-mirror-only change still reindexes), and is clean once the index refreshes.
    reindex_calls = []
    monkeypatch.setattr(main_module.retention, "reindex_keyword", lambda root: reindex_calls.append(root))
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=(CX,))
    _hide_claim_and_fanout(tmp_path, conn, CX)                 # page + graph -> evidence_hidden
    assert _syn_fm(tmp_path)["status"] == "evidence_hidden"
    graph.upsert_node(conn, node_id=SYN, node_type="synthesis", slug=SYN, status="active")  # graph drift
    conn.commit()
    conn.close()
    n = len(reindex_calls)
    body = client.post("/reviews/apply").json()                # hide_claim still approved -> fan-out retries
    assert body["status"] == "applied"
    assert len(reindex_calls) > n                              # graph-mirror repair still triggered reindex
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert graph.get_node(gconn, SYN)["status"] == "evidence_hidden"   # mirror repaired
    gconn.close()


def test_fanout_graph_only_repair_reindex_failure_is_non_clean(client, tmp_path, monkeypatch):
    def boom(root):
        raise RuntimeError("reindex blew up")
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn, claim_ids=(CX,))
    _hide_claim_and_fanout(tmp_path, conn, CX)
    graph.upsert_node(conn, node_id=SYN, node_type="synthesis", slug=SYN, status="active")  # graph drift
    conn.commit()
    conn.close()
    monkeypatch.setattr(main_module.retention, "reindex_keyword", boom)
    body = client.post("/reviews/apply").json()
    assert body["status"] == "validation_failed"              # clean only AFTER the index refresh
    assert "synthesis_evidence_suppression_not_guaranteed" in body["warnings"]
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert graph.get_node(gconn, SYN)["status"] == "evidence_hidden"   # mutation written before validation
    gconn.close()


# --- shared status-filter machinery: nav row / /search / graph channel ------


def test_hidden_synthesis_nav_row_suppressed(client, tmp_path):
    import sqlite3

    from app.backend import keyword_index
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn)
    conn.commit()
    conn.close()
    keyword_index.reindex(tmp_path, force=True)
    idx = tmp_path / "indexes" / "keyword" / "keyword.sqlite"

    def nav_row():
        c = sqlite3.connect(idx)
        try:
            return c.execute(
                "SELECT status, answer_eligible FROM navigation WHERE node_id = ?", (SYN,)).fetchone()
        finally:
            c.close()

    assert nav_row() == ("active", "1")                  # baseline: discoverable + answer-eligible
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    assert client.post("/reviews/apply").json()["status"] == "applied"   # runs real reindex
    assert nav_row() == ("hidden", "0")                  # status-filtered + answer-ineligible
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert graph.get_node(gconn, SYN)["status"] == "hidden"   # raw graph still returns it
    gconn.close()


def test_hidden_synthesis_excluded_from_search_navigation_end_to_end(client, tmp_path):
    from app.backend import keyword_index
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn)
    conn.commit()
    conn.close()
    keyword_index.reindex(tmp_path, force=True)
    nav = lambda r: {n.get("node_id") for n in r.json()["navigation"]}  # noqa: E731
    q = {"q": "Solar", "mode": "navigation"}
    assert SYN in nav(client.get("/search", params=q))                  # baseline (title "Solar trends")
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    assert client.post("/reviews/apply").json()["status"] == "applied"
    assert SYN not in nav(client.get("/search", params=q))              # default excludes hidden
    explicit = client.get("/search", params={**q, "node_status": "hidden"})
    assert SYN in nav(explicit)                                          # explicit node_status surfaces it


def test_unhide_synthesis_restores_nav_row_and_answer_eligible(client, tmp_path):
    import sqlite3

    from app.backend import keyword_index
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn)
    conn.commit()
    conn.close()

    def nav_row():
        c = sqlite3.connect(tmp_path / "indexes" / "keyword" / "keyword.sqlite")
        try:
            return c.execute(
                "SELECT status, answer_eligible FROM navigation WHERE node_id = ?", (SYN,)).fetchone()
        finally:
            c.close()

    keyword_index.reindex(tmp_path, force=True)
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    client.post("/reviews/apply")
    (tmp_path / "reviews" / "approved" / "rev_s.json").unlink()
    assert nav_row() == ("hidden", "0")
    _approve(tmp_path, "unhide_synthesis", to_status="active", rid="rev_u")
    assert client.post("/reviews/apply").json()["status"] == "applied"
    assert nav_row() == ("active", "1")                  # discovery + answer-eligibility restored


def test_hidden_synthesis_excluded_from_search_graph_channel(client, tmp_path):
    from app.backend import graph_read, search
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn)                    # SYN --related_to--> TOPIC
    conn.commit()
    conn.close()
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    assert client.post("/reviews/apply").json()["status"] == "applied"   # SYN -> hidden
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    default = graph_read.search_subgraph(gconn, [TOPIC], depth=1,
                                         node_statuses=search.RETENTION_DEFAULT_STATUSES,
                                         node_cap=50, edge_cap=50)
    assert SYN not in {n["node_id"] for n in default["nodes"]}           # graph channel excludes hidden
    incl = graph_read.search_subgraph(gconn, [TOPIC], depth=1, node_statuses=("active", "hidden"),
                                      node_cap=50, edge_cap=50)
    assert SYN in {n["node_id"] for n in incl["nodes"]}                  # explicit include surfaces it
    gconn.close()


def test_dry_run_synthesis_hide_reindex_failure_is_non_clean_and_live_unchanged(
        client, tmp_path, monkeypatch):
    def boom(root):
        raise RuntimeError("reindex blew up")
    monkeypatch.setattr(main_module.retention, "reindex_keyword", boom)
    conn = _graph(tmp_path)
    _active_synthesis(tmp_path, conn)
    conn.commit()
    conn.close()
    _approve(tmp_path, "hide_synthesis", to_status="hidden")
    dry = client.post("/reviews/apply/dry-run").json()
    assert dry["status"] == "validation_failed"
    assert "synthesis_hide_discovery_suppression_not_guaranteed" in dry["warnings"]
    assert _syn_fm(tmp_path)["status"] == "active"       # live vault unchanged by the dry-run
