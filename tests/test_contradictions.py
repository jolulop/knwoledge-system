from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import validate_graph  # noqa: E402
import validate_projection  # noqa: E402

from app.backend import graph, manifests  # noqa: E402
from app.llm.cache import ResponseCache  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.workers import claims, contradictions, extract, intake, items, reviews, wiki  # noqa: E402
from app.workers.wiki_render import render_claim_page  # noqa: E402
from tests import fixtures  # noqa: E402

TEMPLATES = ROOT / "templates"
CLAIM_MODEL = "anthropic:claude-sonnet-4-6"

# Two independent sources that both make a checkable claim about the same shared knowledge item.
DOC_A = "# Report A\n\nThe Q3 revenue increased by ten percent. Margins improved.\n"
DOC_B = "# Report B\n\nThe Q3 revenue decreased by five percent. Costs rose.\n"
CLAIM_A = "Q3 revenue increased by ten percent."
QUOTE_A = "The Q3 revenue increased by ten percent."
CLAIM_B = "Q3 revenue decreased by five percent."
QUOTE_B = "The Q3 revenue decreased by five percent."
ITEM = "Q3 revenue"


class ClaimAdapter:
    """Fake tier-2 adapter: returns the one claim whose verbatim quote is in the given source."""
    name = "anthropic"
    supports_batch = False

    def __init__(self, *, available=True):
        self._available = available

    def available(self):
        return self._available

    def parse(self, messages, schema, model_id, *, max_tokens):
        body = messages[-1]["content"]
        out = []
        if QUOTE_A in body:
            out.append({"claim": CLAIM_A, "quote": QUOTE_A})
        if QUOTE_B in body:
            out.append({"claim": CLAIM_B, "quote": QUOTE_B})
        return {"claims": out}


class ItemAdapter:
    """Fake tier-2 adapter: both sources mention the same shared knowledge item."""
    name = "anthropic"
    supports_batch = False

    def __init__(self, *, available=True):
        self._available = available

    def available(self):
        return self._available

    def parse(self, messages, schema, model_id, *, max_tokens):
        return {"items": [{"name": ITEM, "item_type": "method_technique", "aliases": []}]}


def _claim_page_contradictions(tmp_path, claim_id):
    """The `[[Claims/...]]` ids in a Claim page's Contradicting Claims section."""
    text = (tmp_path / "wiki" / "Claims" / f"{claim_id}.md").read_text(encoding="utf-8")
    return validate_projection._section_link_slugs(text, "Contradicting Claims", "Claims")


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


def _build_graph(tmp_path, *, independent=True, claim_adapter=None, item_adapter=None):
    """Ingest two sources, extract claims + items, optionally mark them independent."""
    jobs = tmp_path / "db" / "jobs.sqlite"
    fixtures.copy_templates(tmp_path)         # extraction publishes Source pages (ADR-0068)
    _ingest(tmp_path, "a.md", DOC_A)
    _ingest(tmp_path, "b.md", DOC_B)
    intake.scan_inbox(tmp_path, jobs_db=jobs)
    extract.extract_sources(tmp_path, jobs_db=jobs)
    wiki.generate_wiki(tmp_path, jobs_db=jobs, templates_dir=TEMPLATES, rebuild_index=False)
    if independent:
        manifests.set_provenance(tmp_path / "raw" / "manifests", _sid(tmp_path, "a.md"), author="Alice")
        manifests.set_provenance(tmp_path / "raw" / "manifests", _sid(tmp_path, "b.md"), author="Bob")
    claims.extract_claims(tmp_path, client=_client(tmp_path, claim_adapter or ClaimAdapter()),
                          model_ref=CLAIM_MODEL, jobs_db=jobs, rebuild_index=False)
    items.extract_items(tmp_path, client=_client(tmp_path, item_adapter or ItemAdapter()),
                        model_ref=CLAIM_MODEL, jobs_db=jobs, rebuild_index=False)
    # Refresh Source pages so their Claims/Items sections project the new edges (the CLI does
    # this after extraction); keeps validate_projection green on the Source side.
    wiki.generate_wiki(tmp_path, jobs_db=jobs, templates_dir=TEMPLATES, rebuild_index=False)
    return jobs


def _propose(tmp_path, *, confidence=0.9):
    """Seed one proposed contradiction between the two extracted claims, filed exactly as the
    removed judging sweep did (sorted pair, advisory anchor, both sides in the review item). Judging
    itself returns as ADR-0069's scoped check; these tests cover what happens after a proposal."""
    conn = _gconn(tmp_path)
    try:
        a, b = sorted(graph.active_node_ids_of_type(conn, "claim"))
        dirs = {"claims_dir": tmp_path / "wiki" / "Claims",
                "markdown_dir": tmp_path / "normalized" / "markdown"}
        ctx_a = contradictions._claim_context(conn, a, **dirs)
        ctx_b = contradictions._claim_context(conn, b, **dirs)
        anchor = ctx_a["citations"][0]
        rid = reviews.review_id("resolve_contradiction", {"claim_a": a, "claim_b": b})
        graph.upsert_assertion(
            conn, src_id=a, dst_id=b, edge_type="contradicts", asserted_by="llm",
            status="proposed", confidence=confidence, evidence_source_id=anchor["source_id"],
            evidence_char_start=anchor["char_start"], evidence_char_end=anchor["char_end"],
            review_id=rid)
        conn.commit()
    finally:
        conn.close()
    reviews.create_review_item(
        tmp_path / "reviews", review_type="resolve_contradiction",
        subject={"claim_a": a, "claim_b": b},
        proposal={"outcomes": ["acknowledge", "supersede", "reject"], "confidence": confidence,
                  "explanation": "Opposite revenue direction for the same quarter.",
                  "sides": [ctx_a, ctx_b]},
        context={"asserted_by": "llm"}, priority="medium")
    return a, b


def _apply(tmp_path):
    """Apply's contradiction executor (`/reviews/apply` runs the same function)."""
    conn = _gconn(tmp_path)
    try:
        out = contradictions.apply_contradiction_decisions(
            conn, tmp_path / "reviews", claims_dir=tmp_path / "wiki" / "Claims",
            markdown_dir=tmp_path / "normalized" / "markdown")
        conn.commit()
    finally:
        conn.close()
    return out


def _pending_contradiction_rid(tmp_path):
    return next(json.loads(p.read_text())["review_id"]
                for p in (tmp_path / "reviews" / "pending").glob("*.json")
                if json.loads(p.read_text())["type"] == "resolve_contradiction")


def _gconn(tmp_path):
    return graph.connect(tmp_path / "db" / "graph.sqlite")


# --- ADR-0069 slice 0: the whole-vault judging sweep is gone ----------------


def test_the_judging_sweep_is_gone_from_the_worker_and_the_cli(tmp_path, monkeypatch, capsys):
    import detect_contradictions as cli

    from app.llm import client as llm_client
    assert not hasattr(contradictions, "detect_contradictions")
    assert not hasattr(contradictions, "candidate_pairs")
    # The CLI never builds a model client: no arguments prints usage and fails visibly (exit 2),
    # -h/--help prints it and succeeds, and anything else is refused the same way.
    monkeypatch.setattr(llm_client, "build_client",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("model client built")))
    assert cli.main([]) == 2
    assert "removed (ADR-0069 slice 0)" in capsys.readouterr().err
    assert cli.main(["--help"]) == 0
    assert "maintain" in capsys.readouterr().out          # the one command that runs (no model)
    assert cli.main(["estimate", "src_a", "src_b"]) == 2
    assert "app.llm" not in Path(cli.__file__).read_text(encoding="utf-8")


def test_no_jobs_route_is_named_for_contradictions():
    # A route-name guard only; the stronger guarantee is that the worker has no judging function
    # for any handler to call (asserted in the test above).
    from app.backend import main as main_module
    paths = [getattr(r, "path", "") for r in main_module.app.routes]
    assert not [p for p in paths if p.startswith("/jobs") and "contradict" in p]


# --- after a proposal: projection and Apply's resolution ---------------------


def test_proposed_edge_does_not_project_as_backlink(tmp_path):
    _build_graph(tmp_path)
    a, _b = _propose(tmp_path)
    conn = _gconn(tmp_path)
    try:
        active = graph.outgoing_active(conn, a)
    finally:
        conn.close()
    assert all(e["edge_type"] != "contradicts" for e in active)  # proposed != projected
    assert validate_graph.main([str(tmp_path)]) == 0


def test_acknowledge_activates_edge(tmp_path):
    _build_graph(tmp_path)
    a, b = _propose(tmp_path)
    reviews.resolve_review_item(tmp_path / "reviews", _pending_contradiction_rid(tmp_path),
                                decision="approved", decided_by="human")
    out = _apply(tmp_path)
    assert out["resolution"]["acknowledged"] == 1
    assert set(out["changed_pages"]) == {a, b}            # both claim pages re-rendered
    conn = _gconn(tmp_path)
    try:
        row = conn.execute("SELECT * FROM edges WHERE edge_type='contradicts'").fetchone()
    finally:
        conn.close()
    assert row["status"] == "active"
    # The backlink is rendered on BOTH Claim pages (symmetric projection), and the projection
    # validator agrees the rendered links match the active graph edges.
    assert _claim_page_contradictions(tmp_path, a) == {b}
    assert _claim_page_contradictions(tmp_path, b) == {a}
    assert validate_projection.main([str(tmp_path)]) == 0
    assert validate_graph.main([str(tmp_path)]) == 0


def test_tombstoned_claim_retracts_contradiction_in_claim_worker(tmp_path):
    # Blocking-fix invariant (ADR-0031): when an endpoint claim is tombstoned by the CLAIM
    # worker, the contradiction is superseded and the surviving page's backlink dropped
    # *immediately* — extract_claims stays valid without a separate contradiction pass.
    jobs = _build_graph(tmp_path)
    a, b = _propose(tmp_path)
    reviews.resolve_review_item(tmp_path / "reviews", _pending_contradiction_rid(tmp_path),
                                decision="approved", decided_by="human")
    _apply(tmp_path)                                     # -> active, both pages linked

    # Tombstone source B's claim via the CLAIM worker (fresh cache so the empty adapter runs).
    class NoClaims:
        name = "anthropic"
        supports_batch = False

        def available(self):
            return True

        def parse(self, messages, schema, model_id, *, max_tokens):
            return {"claims": []}

    fresh = LLMClient({"anthropic": NoClaims()}, cache=ResponseCache(tmp_path / "db" / "fresh_cache.sqlite"))
    claims.extract_claims(tmp_path, client=fresh, model_ref=CLAIM_MODEL,
                          jobs_db=jobs, source_ids=[_sid(tmp_path, "b.md")], force=True, rebuild_index=False)
    wiki.generate_wiki(tmp_path, jobs_db=jobs, templates_dir=TEMPLATES, rebuild_index=False)  # drop tombstoned claim from Source page

    conn = _gconn(tmp_path)
    try:
        status = conn.execute("SELECT status FROM edges WHERE edge_type='contradicts'").fetchone()["status"]
        surviving = a if conn.execute(
            "SELECT status FROM nodes WHERE node_id=?", (a,)).fetchone()["status"] == "active" else b
    finally:
        conn.close()
    assert status == "superseded"                                   # retracted by the claim worker
    assert _claim_page_contradictions(tmp_path, surviving) == set()  # backlink dropped immediately
    assert validate_projection.main([str(tmp_path)]) == 0           # valid without a contradiction pass
    assert validate_graph.main([str(tmp_path)]) == 0
    assert not [json.loads(p.read_text()) for p in (tmp_path / "reviews" / "pending").glob("*.json")
                if json.loads(p.read_text())["type"] == "resolve_contradiction"]


def _approve_with_winner(tmp_path, winner):
    """Approve the pending resolve_contradiction review and name a supersede winner."""
    pending = tmp_path / "reviews" / "pending"
    path = next(p for p in pending.glob("*.json")
                if json.loads(p.read_text())["type"] == "resolve_contradiction")
    item = json.loads(path.read_text())
    item["winner"] = winner
    path.write_text(json.dumps(item), encoding="utf-8")
    reviews.resolve_review_item(tmp_path / "reviews", item["review_id"],
                                decision="approved", decided_by="human")
    return item["review_id"]


def test_supersede_executes_winner_loser_effects(tmp_path):
    # Slice 1b: an approved supersede writes an active supersedes edge winner->loser, deprecates
    # the loser (with evidence retained), keeps the contradicts edge active, and audits the cause.
    _build_graph(tmp_path)
    winner, loser = _propose(tmp_path)
    _approve_with_winner(tmp_path, winner)
    out = _apply(tmp_path)
    assert out["resolution"]["superseded_executed"] == 1

    conn = _gconn(tmp_path)
    try:
        sup = conn.execute("SELECT * FROM edges WHERE edge_type='supersedes'").fetchall()
        contra = conn.execute("SELECT status FROM edges WHERE edge_type='contradicts'").fetchone()
        winner_status = conn.execute("SELECT status FROM nodes WHERE node_id=?", (winner,)).fetchone()["status"]
        loser_status = conn.execute("SELECT status FROM nodes WHERE node_id=?", (loser,)).fetchone()["status"]
    finally:
        conn.close()
    assert len(sup) == 1
    assert sup[0]["src_id"] == winner and sup[0]["dst_id"] == loser
    assert sup[0]["status"] == "active" and sup[0]["asserted_by"] == "human"
    assert contra["status"] == "active"             # historical conflict stays recorded
    assert winner_status == "active"                # winner unaffected
    assert loser_status == "deprecated_candidate"   # loser deprecated, but still evidenced

    # Loser page: deprecated, yet keeps its evidence row and the contradiction backlink.
    loser_page = (tmp_path / "wiki" / "Claims" / f"{loser}.md").read_text(encoding="utf-8")
    assert "status: deprecated_candidate" in loser_page
    assert "## Evidence" in loser_page and "[[Sources/" in loser_page
    assert _claim_page_contradictions(tmp_path, loser) == {winner}
    # Audit trail: a deprecate_wiki_page item was approved naming the contradiction resolution.
    audit = [json.loads(p.read_text()) for p in (tmp_path / "reviews" / "audit_log").glob("*.json")]
    assert any(e["type"] == "deprecate_wiki_page" and e["decision"] == "approved"
               and e["decided_by"] == "contradiction_resolution" for e in audit)
    assert validate_projection.main([str(tmp_path)]) == 0
    assert validate_graph.main([str(tmp_path)]) == 0


def test_supersede_is_idempotent(tmp_path):
    _build_graph(tmp_path)
    winner, _loser = _propose(tmp_path)
    _approve_with_winner(tmp_path, winner)
    s1, s2 = _apply(tmp_path), _apply(tmp_path)
    assert s1["resolution"]["superseded_executed"] == 1
    assert s2["resolution"]["superseded_executed"] == 0           # applied once
    conn = _gconn(tmp_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM edges WHERE edge_type='supersedes'").fetchone()["n"]
    finally:
        conn.close()
    assert n == 1


def test_supersede_deprecation_persists_across_reextraction(tmp_path):
    # The loser's deprecated_candidate status is page-authoritative and survives a claim
    # re-extraction (its evidence is unchanged) — it is never resurrected to active.
    jobs = _build_graph(tmp_path)
    a, b = _propose(tmp_path)
    _approve_with_winner(tmp_path, a)
    _apply(tmp_path)                                             # b deprecated

    # Re-extract both sources' claims (evidence unchanged) — the loser must stay deprecated, and
    # the contradicts edge must stay active (evidence-based endpoint validity, not status-based).
    claims.extract_claims(tmp_path, client=_client(tmp_path, ClaimAdapter()), model_ref=CLAIM_MODEL,
                          jobs_db=jobs, force=True, rebuild_index=False)
    conn = _gconn(tmp_path)
    try:
        loser_status = conn.execute("SELECT status FROM nodes WHERE node_id=?", (b,)).fetchone()["status"]
        contra = conn.execute("SELECT status FROM edges WHERE edge_type='contradicts'").fetchone()["status"]
    finally:
        conn.close()
    assert loser_status == "deprecated_candidate"  # preserved, not resurrected
    assert contra == "active"                       # endpoint still stands (has evidence)
    assert "status: deprecated_candidate" in (tmp_path / "wiki" / "Claims" / f"{b}.md").read_text()


def test_reject_marks_edge_rejected(tmp_path):
    _build_graph(tmp_path)
    _propose(tmp_path)
    reviews.resolve_review_item(tmp_path / "reviews", _pending_contradiction_rid(tmp_path),
                                decision="rejected", decided_by="human")
    out = _apply(tmp_path)
    assert out["resolution"]["rejected"] == 1
    conn = _gconn(tmp_path)
    try:
        status = conn.execute("SELECT status FROM edges WHERE edge_type='contradicts'").fetchone()["status"]
    finally:
        conn.close()
    assert status == "rejected"


# --- Phase 6 slice 6-3: apply_contradiction_decisions bundle (ADR-0035 A4) ---


def _seed_evidenced_claim(conn, tmp_path, cid, src, text):
    md = tmp_path / "normalized" / "markdown" / f"{src}.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(text, encoding="utf-8")
    graph.upsert_node(conn, node_id=src, node_type="source", slug=src, status="active")
    graph.upsert_node(conn, node_id=cid, node_type="claim", slug=cid, status="active")
    graph.upsert_assertion(conn, src_id=cid, dst_id=src, edge_type="derived_from",
                           asserted_by="llm", status="active", evidence_source_id=src,
                           evidence_char_start=0, evidence_char_end=len(text))
    page = tmp_path / "wiki" / "Claims" / f"{cid}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(render_claim_page({
        "claim_id": cid, "claim_text": text, "confidence": "low",
        "citations": [{"source_id": src, "char_start": 0, "char_end": len(text), "quote": text}],
        "contradicts": [], "deprecated": False}), encoding="utf-8")


def test_apply_contradiction_decisions_acknowledge_flips_edge_and_reprojects(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    conn = graph.connect(gdb)
    a, b = sorted(("clm_1", "clm_2"))
    _seed_evidenced_claim(conn, tmp_path, a, "src_a", "Revenue rose ten percent.")
    _seed_evidenced_claim(conn, tmp_path, b, "src_b", "Revenue fell five percent.")
    rid = reviews.review_id("resolve_contradiction", {"claim_a": a, "claim_b": b})
    graph.upsert_assertion(conn, src_id=a, dst_id=b, edge_type="contradicts", asserted_by="llm",
                           status="proposed", evidence_source_id="src_a", evidence_char_start=0,
                           evidence_char_end=5, review_id=rid)
    appr = tmp_path / "reviews" / "approved"
    appr.mkdir(parents=True, exist_ok=True)
    (appr / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": "resolve_contradiction", "status": "approved",
        "subject": {"claim_a": a, "claim_b": b}, "proposal": {}, "context": {}}), encoding="utf-8")

    res = contradictions.apply_contradiction_decisions(
        conn, tmp_path / "reviews", claims_dir=tmp_path / "wiki" / "Claims",
        markdown_dir=tmp_path / "normalized" / "markdown")
    assert res["resolution"]["acknowledged"] == 1 and res["graph_changed"] is True
    assert graph.contradiction_between(conn, a, b)[0]["status"] == "active"
    # both endpoint claims reprojected so the new contradiction backlink renders
    assert set(res["changed_pages"]) == {a, b}
