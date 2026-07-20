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

TEMPLATES = ROOT / "templates"
CLAIM_MODEL = "anthropic:claude-sonnet-4-6"
HEAVY = "anthropic:claude-opus-4-8"

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


class ContradictionAdapter:
    """Fake tier-3 adapter: returns a fixed verdict; counts provider calls."""
    name = "anthropic"
    supports_batch = False

    def __init__(self, verdict=True, *, confidence=0.9, available=True):
        self._verdict = verdict
        self._confidence = confidence
        self._available = available
        self.calls = 0

    def available(self):
        return self._available

    def parse(self, messages, schema, model_id, *, max_tokens):
        self.calls += 1
        return {"contradicts": bool(self._verdict), "confidence": self._confidence,
                "explanation": "Opposite revenue direction for the same quarter."}


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


def _detect(tmp_path, adapter, jobs, *, rebuild_index=False):
    return contradictions.detect_contradictions(
        tmp_path, client=_client(tmp_path, adapter), model_ref=HEAVY, jobs_db=jobs,
        rebuild_index=rebuild_index)


def _gconn(tmp_path):
    return graph.connect(tmp_path / "db" / "graph.sqlite")


# --- candidate-pair blocking (deterministic core) --------------------------


def test_candidate_pairs_blocks_on_shared_item_and_independence(tmp_path):
    _build_graph(tmp_path)
    conn = _gconn(tmp_path)
    try:
        prov = {m["source_id"]: manifests.get_provenance(m)
                for m in manifests.list_manifests(tmp_path / "raw" / "manifests")}
        pairs = contradictions.candidate_pairs(conn, prov)
    finally:
        conn.close()
    assert len(pairs) == 1
    p = pairs[0]
    assert p["claim_a"] < p["claim_b"]          # canonical ordering
    assert p["shared_nodes"]                      # blocked on the shared co-mentioned item node


def test_no_pairs_when_sources_not_independent(tmp_path):
    # Same author on both sources -> not independent -> no candidate pair.
    _build_graph(tmp_path, independent=False)
    ms = tmp_path / "raw" / "manifests"
    manifests.set_provenance(ms, _sid(tmp_path, "a.md"), author="Same Co")
    manifests.set_provenance(ms, _sid(tmp_path, "b.md"), author="Same Co")
    conn = _gconn(tmp_path)
    try:
        prov = {m["source_id"]: manifests.get_provenance(m) for m in manifests.list_manifests(ms)}
        pairs = contradictions.candidate_pairs(conn, prov)
    finally:
        conn.close()
    assert pairs == []


def test_no_pairs_without_shared_item(tmp_path):
    # Each source mentions a different item -> no co-mention -> no candidate pair.
    class SplitItems:
        name = "anthropic"
        supports_batch = False

        def available(self):
            return True

        def parse(self, messages, schema, model_id, *, max_tokens):
            body = messages[-1]["content"]
            name = "alpha topic" if "Report A" in body else "beta topic"
            return {"items": [{"name": name, "item_type": "method_technique", "aliases": []}]}

    _build_graph(tmp_path, item_adapter=SplitItems())
    conn = _gconn(tmp_path)
    try:
        prov = {m["source_id"]: manifests.get_provenance(m)
                for m in manifests.list_manifests(tmp_path / "raw" / "manifests")}
        pairs = contradictions.candidate_pairs(conn, prov)
    finally:
        conn.close()
    assert pairs == []


# --- detection worker ------------------------------------------------------


def test_proposes_one_sorted_assertion_and_review(tmp_path):
    jobs = _build_graph(tmp_path)
    summary = _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)

    assert summary["candidate_pairs"] == 1
    assert summary["contradictions_proposed"] == 1
    assert summary["status"] == "succeeded"

    conn = _gconn(tmp_path)
    try:
        rows = conn.execute(
            "SELECT * FROM edges WHERE edge_type = 'contradicts'").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "proposed" and row["asserted_by"] == "llm"
    assert row["src_id"] < row["dst_id"]                 # canonical ordering
    assert row["evidence_source_id"] and row["evidence_char_start"] is not None  # advisory anchor
    assert abs(row["confidence"] - 0.9) < 1e-9

    # One review item, carrying both sides.
    pend = list((tmp_path / "reviews" / "pending").glob("*.json"))
    items = [p for p in pend]
    rc = [json.loads(p.read_text()) for p in items
          if json.loads(p.read_text())["type"] == "resolve_contradiction"]
    assert len(rc) == 1
    sides = rc[0]["proposal"]["sides"]
    assert {s["claim_id"] for s in sides} == {row["src_id"], row["dst_id"]}
    assert all(s["citations"] for s in sides)

    assert validate_graph.main([str(tmp_path)]) == 0


def test_proposed_edge_does_not_project_as_backlink(tmp_path):
    jobs = _build_graph(tmp_path)
    _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)
    conn = _gconn(tmp_path)
    try:
        src = conn.execute("SELECT src_id FROM edges WHERE edge_type='contradicts'").fetchone()["src_id"]
        active = graph.outgoing_active(conn, src)
    finally:
        conn.close()
    assert all(e["edge_type"] != "contradicts" for e in active)  # proposed != projected


def test_verdict_false_writes_no_assertion(tmp_path):
    jobs = _build_graph(tmp_path)
    summary = _detect(tmp_path, ContradictionAdapter(verdict=False), jobs)
    assert summary["candidate_pairs"] == 1 and summary["contradictions_proposed"] == 0
    conn = _gconn(tmp_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM edges WHERE edge_type='contradicts'").fetchone()["n"]
    finally:
        conn.close()
    assert n == 0


def test_unchanged_pair_replays_from_cache(tmp_path):
    jobs = _build_graph(tmp_path)
    adapter = ContradictionAdapter(verdict=True)
    _detect(tmp_path, adapter, jobs)
    assert adapter.calls == 1
    # Second run with a fresh adapter sharing the same cache: no provider call.
    adapter2 = ContradictionAdapter(verdict=True)
    summary = _detect(tmp_path, adapter2, jobs)
    assert adapter2.calls == 0
    assert summary["skipped_human_decided"] == 0  # still proposed, re-evaluated via cache


def test_stale_pair_superseded_and_review_withdrawn(tmp_path):
    jobs = _build_graph(tmp_path)
    _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)
    # The pair leaves the candidate set: make the two sources no longer independent.
    ms = tmp_path / "raw" / "manifests"
    manifests.set_provenance(ms, _sid(tmp_path, "a.md"), author="Same Co")
    manifests.set_provenance(ms, _sid(tmp_path, "b.md"), author="Same Co")
    summary = _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)
    assert summary["candidate_pairs"] == 0 and summary["superseded_stale"] == 1
    conn = _gconn(tmp_path)
    try:
        statuses = [r["status"] for r in conn.execute(
            "SELECT status FROM edges WHERE edge_type='contradicts'")]
    finally:
        conn.close()
    assert statuses == ["superseded"]
    # The pending contradiction review was withdrawn (re-fileable later, not a blocking
    # rejection). Other pending items (e.g. the shared item's promotion) are untouched.
    pending = [json.loads(p.read_text()) for p in (tmp_path / "reviews" / "pending").glob("*.json")]
    assert not [i for i in pending if i["type"] == "resolve_contradiction"]


def test_no_api_key_skips_evaluation(tmp_path):
    jobs = _build_graph(tmp_path)
    adapter = ContradictionAdapter(verdict=True, available=False)
    summary = _detect(tmp_path, adapter, jobs)
    assert summary["status"] == "skipped"
    assert summary["candidate_pairs"] == 1 and summary["pairs_evaluated"] == 0
    assert adapter.calls == 0
    conn = _gconn(tmp_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM edges WHERE edge_type='contradicts'").fetchone()["n"]
    finally:
        conn.close()
    assert n == 0


# --- resolution application (acknowledge / reject) -------------------------


def test_acknowledge_activates_edge(tmp_path):
    jobs = _build_graph(tmp_path)
    _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)
    rid = next(json.loads(p.read_text())["review_id"]
               for p in (tmp_path / "reviews" / "pending").glob("*.json")
               if json.loads(p.read_text())["type"] == "resolve_contradiction")
    reviews.resolve_review_item(tmp_path / "reviews", rid, decision="approved", decided_by="human")
    # Re-run detect: applies the human decision -> edge active.
    summary = _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)
    assert summary["resolutions_acknowledged"] == 1
    assert summary["skipped_human_decided"] == 1  # now human-decided, not re-evaluated
    assert summary["claim_pages_reprojected"] == 2  # both claim pages re-rendered
    conn = _gconn(tmp_path)
    try:
        row = conn.execute("SELECT * FROM edges WHERE edge_type='contradicts'").fetchone()
        a, b = row["src_id"], row["dst_id"]
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
    _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)
    rid = next(json.loads(p.read_text())["review_id"]
               for p in (tmp_path / "reviews" / "pending").glob("*.json")
               if json.loads(p.read_text())["type"] == "resolve_contradiction")
    reviews.resolve_review_item(tmp_path / "reviews", rid, decision="approved", decided_by="human")
    _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)  # -> active, both pages linked
    conn = _gconn(tmp_path)
    try:
        a, b = (lambda r: (r["src_id"], r["dst_id"]))(
            conn.execute("SELECT src_id, dst_id FROM edges WHERE edge_type='contradicts'").fetchone())
    finally:
        conn.close()

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

    # No contradiction pass has run since the tombstone — the claim worker already retracted it.
    conn = _gconn(tmp_path)
    try:
        status = conn.execute("SELECT status FROM edges WHERE edge_type='contradicts'").fetchone()["status"]
        surviving = a if conn.execute(
            "SELECT status FROM nodes WHERE node_id=?", (a,)).fetchone()["status"] == "active" else b
    finally:
        conn.close()
    assert status == "superseded"                                   # retracted by the claim worker
    assert _claim_page_contradictions(tmp_path, surviving) == set()  # backlink dropped immediately
    assert validate_projection.main([str(tmp_path)]) == 0           # repo valid without a detect pass
    assert validate_graph.main([str(tmp_path)]) == 0
    # The pending review was withdrawn (it had been approved here, so nothing pending lingers).
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
    jobs = _build_graph(tmp_path)
    _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)
    conn = _gconn(tmp_path)
    try:
        a, b = (lambda r: (r["src_id"], r["dst_id"]))(
            conn.execute("SELECT src_id, dst_id FROM edges WHERE edge_type='contradicts'").fetchone())
    finally:
        conn.close()
    winner, loser = a, b
    _approve_with_winner(tmp_path, winner)
    summary = _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)
    assert summary["supersede_executed"] == 1

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
    jobs = _build_graph(tmp_path)
    _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)
    conn = _gconn(tmp_path)
    try:
        winner = conn.execute("SELECT src_id FROM edges WHERE edge_type='contradicts'").fetchone()["src_id"]
    finally:
        conn.close()
    _approve_with_winner(tmp_path, winner)
    s1 = _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)
    s2 = _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)
    assert s1["supersede_executed"] == 1 and s2["supersede_executed"] == 0  # applied once
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
    _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)
    conn = _gconn(tmp_path)
    try:
        a, b = (lambda r: (r["src_id"], r["dst_id"]))(
            conn.execute("SELECT src_id, dst_id FROM edges WHERE edge_type='contradicts'").fetchone())
    finally:
        conn.close()
    _approve_with_winner(tmp_path, a)
    _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)  # b deprecated

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


def test_superseded_contradiction_survives_detect_backstop(tmp_path):
    # The detect backstop must NOT re-supersede the contradicts edge of a supersede-deprecated
    # pair: endpoint validity is evidence-based, and the deprecated loser still has evidence.
    jobs = _build_graph(tmp_path)
    _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)
    conn = _gconn(tmp_path)
    try:
        a = conn.execute("SELECT src_id FROM edges WHERE edge_type='contradicts'").fetchone()["src_id"]
    finally:
        conn.close()
    _approve_with_winner(tmp_path, a)
    _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)
    summary = _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)  # backstop pass
    assert summary["superseded_stale"] == 0
    conn = _gconn(tmp_path)
    try:
        contra = conn.execute("SELECT status FROM edges WHERE edge_type='contradicts'").fetchone()["status"]
    finally:
        conn.close()
    assert contra == "active"


def test_two_sided_evidence_required_before_verdict(tmp_path):
    # A verdict is never requested without grounded evidence on BOTH sides: if claim B's wording
    # is unavailable, the pair is skipped and no model call is made.
    jobs = _build_graph(tmp_path)
    conn = _gconn(tmp_path)
    try:
        b = sorted(graph.active_node_ids_of_type(conn, "claim"))[1]
    finally:
        conn.close()
    (tmp_path / "wiki" / "Claims" / f"{b}.md").unlink()  # remove side B's wording/citations
    adapter = ContradictionAdapter(verdict=True)
    summary = _detect(tmp_path, adapter, jobs)
    assert summary["candidate_pairs"] == 1 and summary["pairs_evaluated"] == 0
    assert adapter.calls == 0  # no model call without two-sided evidence
    assert summary["contradictions_proposed"] == 0


def test_reprojection_rebuilds_index(tmp_path, monkeypatch):
    # The contradiction worker honours the rebuild_index contract (like claims/items/promote):
    # it rebuilds wiki/index.md exactly when it re-projects Claim pages, and not otherwise. The
    # real rebuild script lives in the repo, not tmp_path, so the call itself is monkeypatched.
    calls = []
    monkeypatch.setattr(contradictions, "_rebuild_index", lambda root: bool(calls.append(root)) or True)
    jobs = _build_graph(tmp_path)
    _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)
    rid = next(json.loads(p.read_text())["review_id"]
               for p in (tmp_path / "reviews" / "pending").glob("*.json")
               if json.loads(p.read_text())["type"] == "resolve_contradiction")
    reviews.resolve_review_item(tmp_path / "reviews", rid, decision="approved", decided_by="human")
    summary = _detect(tmp_path, ContradictionAdapter(verdict=True), jobs, rebuild_index=True)
    assert summary["claim_pages_reprojected"] == 2 and summary["index_rebuilt"] is True
    assert len(calls) == 1
    # A run with nothing to re-project does not rebuild.
    summary2 = _detect(tmp_path, ContradictionAdapter(verdict=True), jobs, rebuild_index=True)
    assert summary2["claim_pages_reprojected"] == 0 and summary2["index_rebuilt"] is False
    assert len(calls) == 1  # not rebuilt again


def test_acknowledged_edge_survives_provenance_change(tmp_path):
    # Independence is the blocking criterion for *finding* candidates, not a validity condition:
    # once a human acknowledges a contradiction, a later provenance edit must not retract it.
    jobs = _build_graph(tmp_path)
    _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)
    rid = next(json.loads(p.read_text())["review_id"]
               for p in (tmp_path / "reviews" / "pending").glob("*.json")
               if json.loads(p.read_text())["type"] == "resolve_contradiction")
    reviews.resolve_review_item(tmp_path / "reviews", rid, decision="approved", decided_by="human")
    _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)  # applies -> active
    # Now make the sources non-independent; the pair leaves the candidate set.
    ms = tmp_path / "raw" / "manifests"
    manifests.set_provenance(ms, _sid(tmp_path, "a.md"), author="Same Co")
    manifests.set_provenance(ms, _sid(tmp_path, "b.md"), author="Same Co")
    summary = _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)
    assert summary["superseded_stale"] == 0  # active human decision not auto-retracted
    conn = _gconn(tmp_path)
    try:
        status = conn.execute("SELECT status FROM edges WHERE edge_type='contradicts'").fetchone()["status"]
    finally:
        conn.close()
    assert status == "active"


def test_reject_marks_edge_rejected(tmp_path):
    jobs = _build_graph(tmp_path)
    _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)
    rid = next(json.loads(p.read_text())["review_id"]
               for p in (tmp_path / "reviews" / "pending").glob("*.json")
               if json.loads(p.read_text())["type"] == "resolve_contradiction")
    reviews.resolve_review_item(tmp_path / "reviews", rid, decision="rejected", decided_by="human")
    summary = _detect(tmp_path, ContradictionAdapter(verdict=True), jobs)
    assert summary["resolutions_rejected"] == 1
    conn = _gconn(tmp_path)
    try:
        status = conn.execute("SELECT status FROM edges WHERE edge_type='contradicts'").fetchone()["status"]
    finally:
        conn.close()
    assert status == "rejected"


# --- idempotency fingerprint fidelity & defensive handling -----------------


def test_cache_key_busts_on_anchor_or_topic_change():
    # The response cache keys on the messages, so the prompt must embed the full citation
    # anchors + shared node ids for the per-pair fingerprint to be faithful (ADR-0031): identical
    # claim text + quote but a changed source_id, char range, or shared node must miss the cache.
    from app.llm import prompts
    from app.llm.cache import cache_key

    base = [{"source_id": "src_" + "a" * 16, "char_start": 10, "char_end": 40, "quote": "q"}]
    diff_src = [{"source_id": "src_" + "b" * 16, "char_start": 10, "char_end": 40, "quote": "q"}]
    diff_range = [{"source_id": "src_" + "a" * 16, "char_start": 99, "char_end": 129, "quote": "q"}]

    def key(cites_a, shared):
        msgs = prompts.build_contradiction_messages("A", cites_a, "B", base, shared)
        return cache_key(msgs, "anthropic:m", prompts.CONTRADICTION_SCHEMA,
                         schema_version="v", prompt_version="v")

    # Shared node ids must be canonical (ADR-0061 asserts id shape at prompt assembly).
    itm_x, itm_y = "itm_" + "1" * 16, "itm_" + "2" * 16
    k0 = key(base, [itm_x])
    assert key(base, [itm_x]) == k0                       # identical inputs -> hit
    assert key(diff_src, [itm_x]) != k0                   # changed source_id -> miss
    assert key(diff_range, [itm_x]) != k0                 # changed char range -> miss
    assert key(base, [itm_y]) != k0                       # changed shared node -> miss


def test_confidence_is_clamped(tmp_path):
    # An out-of-range confidence from the (untrusted) model is clamped before it reaches the edge.
    jobs = _build_graph(tmp_path)
    _detect(tmp_path, ContradictionAdapter(verdict=True, confidence=5.0), jobs)
    conn = _gconn(tmp_path)
    try:
        conf = conn.execute("SELECT confidence FROM edges WHERE edge_type='contradicts'").fetchone()["confidence"]
    finally:
        conn.close()
    assert conf == 1.0


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
