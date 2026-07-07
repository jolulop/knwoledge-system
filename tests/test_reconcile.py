"""ADR-0057 review-queue reconciliation: decision matrix, ownership gate, preflight, hook, sweep."""
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

import reconcile_reviews  # noqa: E402

from app.backend import graph, manifests
from app.llm.cache import ResponseCache
from app.llm.client import LLMClient
from app.workers import concepts, extract, intake, reconcile, reviews
from app.workers.wiki_render import NODE_DIR
from tests import fixtures

MODEL_REF = "anthropic:claude-sonnet-4-6"


class FakeAdapter:
    name = "anthropic"
    supports_batch = False

    def __init__(self, payload):
        self._payload = payload

    def available(self):
        return True

    def parse(self, messages, schema, model_id, *, max_tokens):
        return {"concepts": [dict(c) for c in self._payload.get("concepts", [])],
                "entities": [dict(e) for e in self._payload.get("entities", [])]}


def _build(tmp_path):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.write_markdown(inbox / "doc.md")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    extract.extract_sources(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    return tmp_path


def _extract(tmp_path, concept_names):
    adapter = FakeAdapter({"concepts": [{"name": n, "aliases": []} for n in concept_names]})
    client = LLMClient({"anthropic": adapter},
                       cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite"))
    return concepts.extract_concepts(tmp_path, client=client, model_ref=MODEL_REF,
                                     jobs_db=tmp_path / "db" / "jobs.sqlite")


def _sid(tmp_path):
    return next(m["source_id"] for m in manifests.list_manifests(tmp_path / "raw" / "manifests")
                if m["original_filename"] == "doc.md")


def _rewrite_md(tmp_path, body):
    (tmp_path / "normalized" / "markdown" / f"{_sid(tmp_path)}.md").write_text(
        f"# T\n\n{body}\n", encoding="utf-8")


def _pending_path(tmp_path, rid):
    return tmp_path / "reviews" / "pending" / f"{rid}.json"


def _withdrawn_audits(tmp_path, rid):
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted((tmp_path / "reviews" / "audit_log").glob(f"{rid}-withdrawn-*.json"))]


def _promote_rid(name):
    return reviews.review_id("promote_candidate_node",
                             {"node_id": concepts.node_id("concept", name)})


def _deprecate_rid(name):
    return reviews.review_id("deprecate_wiki_page",
                             {"node_id": concepts.node_id("concept", name),
                              "page": f"Concepts/{concepts._slug(name)}.md"})


def _write_concept_page(tmp_path, slug, node_id, status):
    page_dir = tmp_path / "wiki" / "Concepts"
    page_dir.mkdir(parents=True, exist_ok=True)
    (page_dir / f"{slug}.md").write_text(
        f'---\ntype: concept\nconcept_id: {node_id}\ntitle: "{slug}"\nstatus: {status}\n---\n',
        encoding="utf-8")


def _sweep(tmp_path, gconn):
    return reconcile.sweep(tmp_path / "reviews", gconn, wiki_dir=tmp_path / "wiki")


# --- reconciliation_decision matrix (pure) ----------------------------------


def _promote_item(node="cpt_x", status="pending"):
    return {"type": "promote_candidate_node", "review_id": "rev_x", "status": status,
            "subject": {"node_id": node}, "proposal": {"to_status": "active"}, "context": {}}


def _deprecate_item(reason=None, reason_code=None, node_type="concept", status="pending"):
    proposal = {"to_status": "deprecated_candidate"}
    if reason is not None:
        proposal["reason"] = reason
    if reason_code is not None:
        proposal["reason_code"] = reason_code
    return {"type": "deprecate_wiki_page", "review_id": "rev_y", "status": status,
            "subject": {"node_id": "cpt_x", "page": "Concepts/x.md"},
            "proposal": proposal, "context": {"node_type": node_type}}


def test_decision_promote_matrix():
    d = reconcile.reconciliation_decision
    assert d(_promote_item(), graph_status="candidate", page_status="candidate",
             active_source_count=1) is None
    assert d(_promote_item(), graph_status="deprecated_candidate",
             page_status="deprecated_candidate",
             active_source_count=0) == reconcile.REASON_TOMBSTONED
    assert d(_promote_item(), graph_status="active", page_status="active",
             active_source_count=2) == reconcile.REASON_ALREADY_ACTIVE
    assert d(_promote_item(), graph_status=None, page_status=None,
             active_source_count=0) == reconcile.REASON_MISSING_OR_REKEYED
    for gone in ("merged", "rekeyed"):
        assert d(_promote_item(), graph_status=gone, page_status=gone,
                 active_source_count=0) == reconcile.REASON_MISSING_OR_REKEYED
    # Conservative: statuses outside the decided set are left alone.
    assert d(_promote_item(), graph_status="stale_candidate", page_status="stale_candidate",
             active_source_count=0) is None


def test_decision_requires_surface_corroboration():
    d = reconcile.reconciliation_decision
    # Page frontmatter is the status authority; any graph/page disagreement never withdraws.
    assert d(_promote_item(), graph_status="active", page_status="candidate",
             active_source_count=1) is None
    assert d(_promote_item(), graph_status="deprecated_candidate", page_status="candidate",
             active_source_count=0) is None
    # Graph-missing alone (live page) never withdraws; nor does a mirror without a page.
    assert d(_promote_item(), graph_status=None, page_status="candidate",
             active_source_count=0) is None
    assert d(_promote_item(), graph_status="active", page_status=None,
             active_source_count=0) is None
    # Tombstone withdrawal additionally requires zero active mentions.
    assert d(_promote_item(), graph_status="deprecated_candidate",
             page_status="deprecated_candidate", active_source_count=1) is None


def test_decision_terminal_status_never_decided():
    d = reconcile.reconciliation_decision
    for status in ("approved", "rejected"):
        assert d(_promote_item(status=status), graph_status=None, page_status=None,
                 active_source_count=0) is None
    # Deferred is unresolved: still decided.
    assert d(_promote_item(status="deferred"), graph_status=None, page_status=None,
             active_source_count=0) == reconcile.REASON_MISSING_OR_REKEYED


def test_decision_deprecate_ownership_gate():
    owned = _deprecate_item(reason_code=reconcile.REASON_CODE_NO_ACTIVE_MENTIONS)
    legacy = _deprecate_item(reason=reconcile.LEGACY_NO_ACTIVE_MENTIONS_REASON)
    near_miss = _deprecate_item(reason="no active source mentions remain.")
    lint_style = _deprecate_item(reason="active with 1 mentioning source(s) (<2)")
    claim_typed = _deprecate_item(reason_code=reconcile.REASON_CODE_NO_ACTIVE_MENTIONS,
                                  node_type="claim")
    assert reconcile.owns_deprecation(owned)
    assert not reconcile.owns_deprecation(legacy)                             # shim is opt-in
    assert reconcile.owns_deprecation(legacy, allow_legacy_reason=True)       # sweep only
    assert not reconcile.owns_deprecation(near_miss, allow_legacy_reason=True)
    assert not reconcile.owns_deprecation(lint_style, allow_legacy_reason=True)
    assert not reconcile.owns_deprecation(claim_typed, allow_legacy_reason=True)


def test_decision_deprecate_matrix():
    d = reconcile.reconciliation_decision
    owned = _deprecate_item(reason_code=reconcile.REASON_CODE_NO_ACTIVE_MENTIONS)
    # Edges are graph-SoT (ADR-0029): active mentions alone prove resurrection.
    assert d(owned, graph_status="candidate", page_status="candidate",
             active_source_count=1) == reconcile.REASON_RESURRECTED
    assert d(owned, graph_status="deprecated_candidate", page_status="deprecated_candidate",
             active_source_count=0) is None  # human gate
    assert d(owned, graph_status=None, page_status=None,
             active_source_count=0) == reconcile.REASON_MISSING_OR_REKEYED
    assert d(owned, graph_status=None, page_status="candidate",
             active_source_count=0) is None  # graph-missing alone never withdraws
    foreign = _deprecate_item(reason="active with 1 mentioning source(s) (<2)")
    assert d(foreign, graph_status="candidate", page_status="candidate",
             active_source_count=1) is None
    other = {"type": "resolve_contradiction", "review_id": "rev_z", "status": "pending",
             "subject": {}, "proposal": {}, "context": {}}
    assert d(other, graph_status=None, page_status=None, active_source_count=0) is None


def test_dir_id_field_parity():
    # The sweep's local dir->id-field map must track concepts.ID_FIELD / wiki_render.NODE_DIR
    # (kept local to avoid the concepts->reconcile import cycle).
    assert reconcile._DIR_ID_FIELD == {NODE_DIR[t]: f for t, f in concepts.ID_FIELD.items()}


# --- hook: _recompose_node reconciles both directions ------------------------


def test_hook_tombstone_withdraws_promote_and_files_reason_code(tmp_path):
    _build(tmp_path)
    _extract(tmp_path, ["Old", "Keeper"])
    rid = _promote_rid("Old")
    assert _pending_path(tmp_path, rid).exists()

    _rewrite_md(tmp_path, "second body")
    _extract(tmp_path, ["New", "Keeper"])

    # The stale promote is withdrawn with the tombstone reason, audited.
    assert not _pending_path(tmp_path, rid).exists()
    audits = _withdrawn_audits(tmp_path, rid)
    assert len(audits) == 1 and audits[0]["note"] == reconcile.REASON_TOMBSTONED
    assert audits[0]["decided_by"] == "system"
    # The recompose-filed deprecation carries the stable reason_code (and the legacy prose).
    dep = json.loads(_pending_path(tmp_path, _deprecate_rid("Old")).read_text(encoding="utf-8"))
    assert dep["proposal"]["reason_code"] == reconcile.REASON_CODE_NO_ACTIVE_MENTIONS
    assert dep["proposal"]["reason"] == reconcile.LEGACY_NO_ACTIVE_MENTIONS_REASON
    # The live concept's promote is untouched.
    assert _pending_path(tmp_path, _promote_rid("Keeper")).exists()


def test_hook_resurrection_withdraws_recompose_deprecate(tmp_path):
    _build(tmp_path)
    _extract(tmp_path, ["Old", "Keeper"])
    _rewrite_md(tmp_path, "second body")
    _extract(tmp_path, ["New", "Keeper"])
    dep_rid = _deprecate_rid("Old")
    assert _pending_path(tmp_path, dep_rid).exists()

    _rewrite_md(tmp_path, "third body")
    _extract(tmp_path, ["Old", "Keeper"])

    assert not _pending_path(tmp_path, dep_rid).exists()
    audits = _withdrawn_audits(tmp_path, dep_rid)
    assert len(audits) == 1 and audits[0]["note"] == reconcile.REASON_RESURRECTED
    # The resurrected candidate re-enters the promotion ledger.
    assert _pending_path(tmp_path, _promote_rid("Old")).exists()


def test_hook_leaves_foreign_reason_deprecate_on_tombstone(tmp_path):
    _build(tmp_path)
    _extract(tmp_path, ["Old", "Keeper"])
    # A lint-style deprecation occupies the same-subject slot BEFORE the node tombstones.
    lint_reason = "active with 1 mentioning source(s) (<2)"
    dep_rid = reviews.create_review_item(
        tmp_path / "reviews", review_type="deprecate_wiki_page",
        subject={"node_id": concepts.node_id("concept", "Old"), "page": "Concepts/old.md"},
        proposal={"to_status": "deprecated_candidate", "reason": lint_reason},
        context={"node_type": "concept"})

    _rewrite_md(tmp_path, "second body")
    _extract(tmp_path, ["New", "Keeper"])

    # First filer owns the stored reason (create is idempotent) and reconciliation never
    # rewrites or withdraws a foreign-reason item — but the promote IS withdrawn.
    item = json.loads(_pending_path(tmp_path, dep_rid).read_text(encoding="utf-8"))
    assert item["proposal"]["reason"] == lint_reason and "reason_code" not in item["proposal"]
    assert not _pending_path(tmp_path, _promote_rid("Old")).exists()


# --- sweep: preflight fail-closed --------------------------------------------


def _eligible_promote(tmp_path, node_id="cpt_feedfeedfeedfeed"):
    return reviews.create_review_item(
        tmp_path / "reviews", review_type="promote_candidate_node",
        subject={"node_id": node_id}, proposal={"to_status": "active", "node_type": "concept"})


def test_sweep_refuses_empty_graph(tmp_path):
    rid = _eligible_promote(tmp_path)
    graph.init_db(tmp_path / "db" / "graph.sqlite")
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    counts = _sweep(tmp_path, gconn)
    gconn.close()
    assert counts["refused"] == ["graph_has_no_nodes"]
    assert counts["withdrawn"] == 0 and _pending_path(tmp_path, rid).exists()


def test_sweep_refuses_schema_version_mismatch(tmp_path):
    rid = _eligible_promote(tmp_path)
    graph.init_db(tmp_path / "db" / "graph.sqlite")
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    graph.upsert_node(gconn, node_id="cpt_aaaaaaaaaaaaaaaa", node_type="concept",
                      slug="some-node", status="candidate")
    gconn.execute("PRAGMA user_version = 99")
    counts = _sweep(tmp_path, gconn)
    gconn.close()
    assert counts["refused"] and "graph_schema_version_mismatch" in counts["refused"][0]
    assert counts["withdrawn"] == 0 and _pending_path(tmp_path, rid).exists()


def test_sweep_refuses_on_graph_wiki_drift(tmp_path):
    # Three drift shapes over reviewed nodes: mirror-without-page, page-without-mirror,
    # and a status disagreement. Any one refuses the whole sweep; nothing is withdrawn.
    graph.init_db(tmp_path / "db" / "graph.sqlite")
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    graph.upsert_node(gconn, node_id="cpt_aaaaaaaaaaaaaaaa", node_type="concept",
                      slug="mirror-only", status="active")                    # no page
    _write_concept_page(tmp_path, "page-only", "cpt_bbbbbbbbbbbbbbbb", "candidate")  # no mirror
    graph.upsert_node(gconn, node_id="cpt_cccccccccccccccc", node_type="concept",
                      slug="disagrees", status="active")
    _write_concept_page(tmp_path, "disagrees", "cpt_cccccccccccccccc", "candidate")
    rids = [_eligible_promote(tmp_path, node_id=nid)
            for nid in ("cpt_aaaaaaaaaaaaaaaa", "cpt_bbbbbbbbbbbbbbbb", "cpt_cccccccccccccccc")]
    counts = _sweep(tmp_path, gconn)
    gconn.close()
    assert len(counts["refused"]) == 1 and "graph_wiki_projection_invalid" in counts["refused"][0]
    assert "over 3 reviewed node(s)" in counts["refused"][0]
    assert counts["withdrawn"] == 0
    assert all(_pending_path(tmp_path, rid).exists() for rid in rids)


def test_sweep_skips_terminal_and_malformed_pending_files(tmp_path):
    graph.init_db(tmp_path / "db" / "graph.sqlite")
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    graph.upsert_node(gconn, node_id="cpt_aaaaaaaaaaaaaaaa", node_type="concept",
                      slug="unrelated", status="candidate")
    _write_concept_page(tmp_path, "unrelated", "cpt_aaaaaaaaaaaaaaaa", "candidate")
    ghost_rid = _eligible_promote(tmp_path)  # node absent from BOTH surfaces -> withdrawable
    pending = tmp_path / "reviews" / "pending"
    # A terminal-status file sitting in pending/ is broken ledger state: skipped, never withdrawn.
    terminal = dict(_promote_item(node="cpt_9999999999999999", status="approved"),
                    review_id="rev_feedfacefeedface")
    (pending / "rev_feedfacefeedface.json").write_text(
        json.dumps(terminal, indent=2) + "\n", encoding="utf-8")
    (pending / "rev_notjson.json").write_text("{not json", encoding="utf-8")
    (pending / "rev_notadict.json").write_text("[1, 2]", encoding="utf-8")

    counts = _sweep(tmp_path, gconn)
    gconn.close()
    assert counts["refused"] == []
    assert counts["terminal_in_pending"] == 1 and (pending / "rev_feedfacefeedface.json").exists()
    assert counts["parse_errors"] == 1 and counts["schema_errors"] == 1
    assert counts["withdrawn"] == 1  # the sweep continued past the bad files
    assert counts["withdrawn_by_reason"] == {reconcile.REASON_MISSING_OR_REKEYED: 1}
    assert not _pending_path(tmp_path, ghost_rid).exists()


def test_cli_fails_closed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KNOWLEDGE_SYSTEM_HOME", str(tmp_path))
    # Missing graph DB: refuse before connecting.
    assert reconcile_reviews.main([]) == 1
    assert "graph database not found" in capsys.readouterr().out
    # Empty graph DB with an eligible item: preflight refusal, exit non-zero, nothing withdrawn.
    rid = _eligible_promote(tmp_path)
    graph.init_db(tmp_path / "db" / "graph.sqlite")
    assert reconcile_reviews.main([]) == 1
    assert "graph_has_no_nodes" in capsys.readouterr().out
    assert _pending_path(tmp_path, rid).exists()


# --- sweep: catch-up over pre-ADR-0057 backlog -------------------------------


def test_sweep_matrix_legacy_shim_and_idempotence(tmp_path):
    _build(tmp_path)
    _extract(tmp_path, ["Old", "Keeper"])
    _rewrite_md(tmp_path, "second body")
    _extract(tmp_path, ["New", "Keeper"])

    # Simulate a pre-ADR-0057 recompose deprecation: strip reason_code, keep legacy prose.
    dep_path = _pending_path(tmp_path, _deprecate_rid("Old"))
    item = json.loads(dep_path.read_text(encoding="utf-8"))
    del item["proposal"]["reason_code"]
    dep_path.write_text(json.dumps(item, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    _rewrite_md(tmp_path, "third body")
    _extract(tmp_path, ["Old", "Keeper"])
    # The hook must NOT act on the legacy item (shim is sweep-only)...
    assert dep_path.exists()

    # Backlog fabrication: a stale promote for the now-tombstoned New (re-filed after the hook
    # withdrew it — a withdrawn id may legitimately re-file), one for a missing node, one for a
    # directly-activated node, and a deferred stale promote (deferred = still unresolved).
    reviews_dir = tmp_path / "reviews"
    stale_new = reviews.create_review_item(
        reviews_dir, review_type="promote_candidate_node",
        subject={"node_id": concepts.node_id("concept", "New")},
        proposal={"to_status": "active", "node_type": "concept"})
    ghost = reviews.create_review_item(
        reviews_dir, review_type="promote_candidate_node",
        subject={"node_id": "cpt_0000000000000000"},
        proposal={"to_status": "active", "node_type": "concept"})
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    graph.upsert_node(gconn, node_id="cpt_1111111111111111", node_type="concept",
                      slug="already-active", status="active")
    _write_concept_page(tmp_path, "already-active", "cpt_1111111111111111", "active")
    active_rid = reviews.create_review_item(
        reviews_dir, review_type="promote_candidate_node",
        subject={"node_id": "cpt_1111111111111111"},
        proposal={"to_status": "active", "node_type": "concept"})
    reviews.defer_review_item(reviews_dir, stale_new, note="pinned for later")
    # A lint-style foreign-reason deprecation on the live Keeper: never owned.
    lint_rid = reviews.create_review_item(
        reviews_dir, review_type="deprecate_wiki_page",
        subject={"node_id": concepts.node_id("concept", "Keeper"), "page": "Concepts/keeper.md"},
        proposal={"to_status": "deprecated_candidate",
                  "reason": "active with 1 mentioning source(s) (<2)"},
        context={"node_type": "concept"})
    # A human-approved promote: terminal records are immune (not in pending/, never audited).
    approved_rid = reviews.create_review_item(
        reviews_dir, review_type="promote_candidate_node",
        subject={"node_id": "cpt_2222222222222222"},
        proposal={"to_status": "active", "node_type": "concept"})
    reviews.resolve_review_item(reviews_dir, approved_rid, decision="approved", decided_by="human")

    counts = _sweep(tmp_path, gconn)

    assert counts["refused"] == []
    by_reason = counts["withdrawn_by_reason"]
    assert not dep_path.exists()  # legacy shim withdrew it (Old has active mentions again)
    assert by_reason[reconcile.REASON_RESURRECTED] == 1
    assert not _pending_path(tmp_path, stale_new).exists()  # deferred still unresolved -> swept
    assert by_reason[reconcile.REASON_TOMBSTONED] == 1
    assert not _pending_path(tmp_path, ghost).exists()
    assert by_reason[reconcile.REASON_MISSING_OR_REKEYED] == 1
    assert not _pending_path(tmp_path, active_rid).exists()
    assert by_reason[reconcile.REASON_ALREADY_ACTIVE] == 1
    assert counts["withdrawn"] == 4
    # Foreign-reason deprecation untouched; approved record intact, no withdrawal audit.
    assert _pending_path(tmp_path, lint_rid).exists() and counts["not_owned"] == 1
    assert (reviews_dir / "approved" / f"{approved_rid}.json").exists()
    assert _withdrawn_audits(tmp_path, approved_rid) == []
    # Live promotes (Old, Keeper) stay pending.
    assert _pending_path(tmp_path, _promote_rid("Old")).exists()
    assert _pending_path(tmp_path, _promote_rid("Keeper")).exists()

    # Idempotent: a second sweep over the same state withdraws nothing.
    again = _sweep(tmp_path, gconn)
    assert again["withdrawn"] == 0 and again["not_owned"] == 1
    gconn.close()
