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

import validate_frontmatter  # noqa: E402
import validate_graph  # noqa: E402
import validate_projection  # noqa: E402
import validate_wikilinks  # noqa: E402

from app.backend import graph, manifests
from app.llm.cache import ResponseCache
from app.llm.client import LLMClient
from app.workers import concepts, extract, intake, wiki
from app.workers.wiki_render import parse_frontmatter
from tests import fixtures

TEMPLATES = ROOT / "templates"
MODEL_REF = "anthropic:claude-sonnet-4-6"

_DEFAULT = {
    "concepts": [{"name": "Post-Merger Integration", "aliases": ["PMI"]}],
    "entities": [
        {"name": "Acme Corp", "entity_type": "organization", "aliases": ["Acme"]},
        {"name": "Jane Doe", "entity_type": "person", "aliases": []},
    ],
}


class FakeAdapter:
    name = "anthropic"
    supports_batch = False

    def __init__(self, payload=None, *, available=True):
        self.calls = 0
        self._payload = payload if payload is not None else _DEFAULT
        self._available = available

    def available(self):
        return self._available

    def parse(self, messages, schema, model_id, *, max_tokens):
        self.calls += 1
        return {"concepts": [dict(c) for c in self._payload["concepts"]],
                "entities": [dict(e) for e in self._payload["entities"]]}


def _build(tmp_path, extra=None):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.write_markdown(inbox / "doc.md")
    if extra:
        for name, content in extra.items():
            (inbox / name).write_text(content, encoding="utf-8")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    extract.extract_sources(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    return tmp_path


def _gen_wiki(tmp_path):
    return wiki.generate_wiki(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite",
                              templates_dir=TEMPLATES, rebuild_index=False)


def _extract(tmp_path, adapter, **kw):
    client = LLMClient({"anthropic": adapter}, cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite"))
    return concepts.extract_concepts(tmp_path, client=client, model_ref=MODEL_REF,
                                     jobs_db=tmp_path / "db" / "jobs.sqlite", **kw)


def _sids(tmp_path):
    return {m["original_filename"]: m["source_id"]
            for m in manifests.list_manifests(tmp_path / "raw" / "manifests")}


# --- id / slug -------------------------------------------------------------


def test_node_id_and_slug():
    assert concepts.node_id("concept", "Post-Merger Integration").startswith("cpt_")
    assert concepts.node_id("person", "Jane Doe").startswith("per_")
    assert concepts.node_id("organization", "Acme") .startswith("org_")
    assert concepts.node_id("project", "Apollo").startswith("prj_")
    # Source-agnostic + case/space-insensitive identity.
    assert concepts.node_id("concept", "post-merger  integration") == concepts.node_id("concept", "Post-Merger Integration")
    assert concepts._slug("Post-Merger Integration") == "post-merger-integration"


# --- worker ----------------------------------------------------------------


def test_extracts_typed_nodes_routes_pages_and_writes_mentions(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)
    summary = _extract(tmp_path, FakeAdapter())
    sid = _sids(tmp_path)["doc.md"]

    assert summary["nodes_written"] == 3 and summary["mentions_written"] == 3
    assert summary["status"] == "succeeded"

    # Routed by type to the right directory, each a candidate.
    cpt = tmp_path / "wiki" / "Concepts" / "post-merger-integration.md"
    org = tmp_path / "wiki" / "Organizations" / "acme-corp.md"
    per = tmp_path / "wiki" / "People" / "jane-doe.md"
    assert cpt.exists() and org.exists() and per.exists()
    fm = parse_frontmatter(cpt.read_text(encoding="utf-8"))
    assert fm["type"] == "concept" and fm["status"] == "candidate" and fm["concept_id"].startswith("cpt_")
    assert parse_frontmatter(org.read_text(encoding="utf-8"))["organization_id"].startswith("org_")
    assert "PMI" in cpt.read_text(encoding="utf-8")  # alias rendered
    assert f"[[Sources/{sid}]]" in cpt.read_text(encoding="utf-8")  # mentioned-by

    # mentions edges are active provenance.
    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        rows = conn.execute("SELECT edge_type, status FROM edges WHERE asserted_by='llm'").fetchall()
    finally:
        conn.close()
    assert len(rows) == 3 and all(r["edge_type"] == "mentions" and r["status"] == "active" for r in rows)

    assert validate_frontmatter.main([str(tmp_path)]) == 0
    assert validate_graph.main([str(tmp_path)]) == 0
    assert validate_wikilinks.main([str(tmp_path)]) == 0


def test_concept_need_not_appear_verbatim(tmp_path):
    # "Post-Merger Integration" is not in the source text, yet it is still extracted (no
    # verbatim grounding for concepts/entities — recurrence is the gate, ADR-0026/0018).
    _build(tmp_path)
    summary = _extract(tmp_path, FakeAdapter(payload={"concepts": [{"name": "Post-Merger Integration", "aliases": []}], "entities": []}))
    assert summary["nodes_written"] == 1
    assert (tmp_path / "wiki" / "Concepts" / "post-merger-integration.md").exists()


def test_generic_entity_routes_to_entities(tmp_path):
    # entity_type is enum-constrained at parse time; a generic `entity` routes to Entities/.
    _build(tmp_path)
    _extract(tmp_path, FakeAdapter(payload={"concepts": [], "entities": [
        {"name": "Mystery Thing", "entity_type": "entity", "aliases": []}]}))
    assert (tmp_path / "wiki" / "Entities" / "mystery-thing.md").exists()
    assert concepts.node_id("entity", "Mystery Thing").startswith("ent_")


def test_source_page_projects_concept_and_entity_mentions(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)
    _extract(tmp_path, FakeAdapter())
    _gen_wiki(tmp_path)  # refresh Source page with mention links
    sid = _sids(tmp_path)["doc.md"]
    text = (tmp_path / "wiki" / "Sources" / f"{sid}.md").read_text(encoding="utf-8")
    assert "[[Concepts/post-merger-integration|" in text       # Concepts Mentioned
    assert "[[Organizations/acme-corp|" in text                # entity family
    assert "[[People/jane-doe|" in text
    assert validate_wikilinks.main([str(tmp_path)]) == 0


def test_no_api_key_skips(tmp_path):
    _build(tmp_path)
    fake = FakeAdapter(available=False)
    summary = _extract(tmp_path, fake)
    assert summary["status"] == "skipped" and summary["nodes_written"] == 0 and fake.calls == 0


def test_idempotent_and_force(tmp_path):
    _build(tmp_path)
    assert _extract(tmp_path, FakeAdapter())["nodes_written"] == 3
    second = _extract(tmp_path, FakeAdapter())
    assert second["nodes_written"] == 0 and second["skipped_fresh"] == 1
    assert _extract(tmp_path, FakeAdapter(), force=True)["nodes_written"] == 3


def test_reextraction_supersedes_and_tombstones(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)
    _extract(tmp_path, FakeAdapter(payload={"concepts": [{"name": "Old Concept", "aliases": []}], "entities": []}))
    old = tmp_path / "wiki" / "Concepts" / "old-concept.md"
    assert old.exists() and parse_frontmatter(old.read_text(encoding="utf-8"))["status"] == "candidate"

    # Change the source text so re-extraction is a cache miss (new prompt -> new concepts).
    sid = _sids(tmp_path)["doc.md"]
    (tmp_path / "normalized" / "markdown" / f"{sid}.md").write_text(
        "# T\n\nAn entirely replacement body sentence.\n", encoding="utf-8")
    summary = _extract(tmp_path, FakeAdapter(payload={"concepts": [{"name": "New Concept", "aliases": []}], "entities": []}))
    assert summary["node_pages_tombstoned"] >= 1
    assert parse_frontmatter(old.read_text(encoding="utf-8"))["status"] == "deprecated_candidate"  # tombstoned, not deleted
    assert (tmp_path / "wiki" / "Concepts" / "new-concept.md").exists()


def _pending_reviews(tmp_path):
    d = tmp_path / "reviews" / "pending"
    return [json.loads(p.read_text(encoding="utf-8")) for p in d.glob("*.json")] if d.exists() else []


def _two_sources(tmp_path):
    _build(tmp_path, extra={"doc2.md": "# Other\n\nA distinct second document body here.\n"})
    s = _sids(tmp_path)
    return s["doc.md"], s["doc2.md"]


def test_alias_union_across_sources_preserves_both(tmp_path):
    a, b = _two_sources(tmp_path)
    _extract(tmp_path, FakeAdapter(payload={"concepts": [{"name": "Shared", "aliases": ["AliasA"]}], "entities": []}), source_ids=[a])
    _extract(tmp_path, FakeAdapter(payload={"concepts": [{"name": "Shared", "aliases": ["AliasB"]}], "entities": []}), source_ids=[b])
    page = (tmp_path / "wiki" / "Concepts" / "shared.md").read_text(encoding="utf-8")
    assert "AliasA" in page and "AliasB" in page  # union — the first source's alias is not lost


def test_subtype_conflict_keeps_existing_node_and_files_review(tmp_path):
    a, b = _two_sources(tmp_path)
    _extract(tmp_path, FakeAdapter(payload={"concepts": [], "entities": [
        {"name": "Acme", "entity_type": "organization", "aliases": []}]}), source_ids=[a])
    assert (tmp_path / "wiki" / "Organizations" / "acme.md").exists()

    _extract(tmp_path, FakeAdapter(payload={"concepts": [], "entities": [
        {"name": "Acme", "entity_type": "person", "aliases": []}]}), source_ids=[b])
    # No second (person) node minted; the existing organization node is kept.
    assert not (tmp_path / "wiki" / "People" / "acme.md").exists()
    assert (tmp_path / "wiki" / "Organizations" / "acme.md").exists()
    # ADR-0051 producer contract: subject {node_id, to_type} + proposal {to_type} (both entity-family).
    revs = [r for r in _pending_reviews(tmp_path) if r["type"] == "change_entity_subtype"]
    assert len(revs) == 1
    org_id = concepts.node_id("organization", "Acme")
    assert revs[0]["subject"] == {"node_id": org_id, "to_type": "person"}
    assert revs[0]["proposal"] == {"to_type": "person"}
    assert revs[0]["context"]["from_type"] == "organization"


def test_concept_entity_conflict_withholds_rekey_review(tmp_path):
    # ADR-0051: a concept<->entity conflict is a cross-family type change (a future change_node_type), so the
    # producer WITHHOLDS a change_entity_subtype review (it could only ever skip out_of_scope).
    a, b = _two_sources(tmp_path)
    _extract(tmp_path, FakeAdapter(payload={"concepts": [{"name": "Acme", "aliases": []}], "entities": []}),
             source_ids=[a])
    assert (tmp_path / "wiki" / "Concepts" / "acme.md").exists()
    _extract(tmp_path, FakeAdapter(payload={"concepts": [], "entities": [
        {"name": "Acme", "entity_type": "organization", "aliases": []}]}), source_ids=[b])
    assert not any(r["type"] == "change_entity_subtype" for r in _pending_reviews(tmp_path))


def test_candidate_promotion_review_items_are_filed(tmp_path):
    _build(tmp_path)
    _extract(tmp_path, FakeAdapter())
    reviews = _pending_reviews(tmp_path)
    promote = [r for r in reviews if r["type"] == "promote_candidate_node"]
    assert len(promote) == 3 and all(r["status"] == "pending" for r in promote)


def test_tombstone_files_deprecation_review(tmp_path):
    _build(tmp_path)
    _extract(tmp_path, FakeAdapter(payload={"concepts": [{"name": "Old", "aliases": []}], "entities": []}))
    sid = _sids(tmp_path)["doc.md"]
    (tmp_path / "normalized" / "markdown" / f"{sid}.md").write_text("# T\n\nReplacement body.\n", encoding="utf-8")
    _extract(tmp_path, FakeAdapter(payload={"concepts": [{"name": "New", "aliases": []}], "entities": []}))
    assert any(r["type"] == "deprecate_wiki_page" for r in _pending_reviews(tmp_path))


def test_oversized_llm_output_is_bounded(tmp_path):
    _build(tmp_path)
    big_name = "A" * 500
    many = [f"alias-{i}-" + "x" * 200 for i in range(40)]
    _extract(tmp_path, FakeAdapter(payload={"concepts": [{"name": big_name, "aliases": many}], "entities": []}))
    pages = list((tmp_path / "wiki" / "Concepts").glob("*.md"))
    assert len(pages) == 1
    fm = parse_frontmatter(pages[0].read_text(encoding="utf-8"))
    assert isinstance(fm["aliases"], list) and len(fm["aliases"]) <= 16  # alias count bounded
    assert len(pages[0].stem) <= 200  # slug/name length bounded


def _section(text, name):
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == f"## {name}")
    except StopIteration:
        return ""
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start + 1:end])


def test_source_sections_route_by_type(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)
    _extract(tmp_path, FakeAdapter())
    _gen_wiki(tmp_path)
    text = (tmp_path / "wiki" / "Sources" / f"{_sids(tmp_path)['doc.md']}.md").read_text(encoding="utf-8")
    assert "[[Concepts/post-merger-integration" in _section(text, "Concepts Mentioned")
    assert "[[Organizations/acme-corp" in _section(text, "Organizations Mentioned")
    assert "[[People/jane-doe" in _section(text, "People Mentioned")
    # Subtyped nodes are not collapsed into Entities Mentioned.
    assert "acme-corp" not in _section(text, "Entities Mentioned")


def test_source_frontmatter_arrays_mirror_body(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)
    _extract(tmp_path, FakeAdapter())
    _gen_wiki(tmp_path)
    fm = parse_frontmatter((tmp_path / "wiki" / "Sources" / f"{_sids(tmp_path)['doc.md']}.md").read_text(encoding="utf-8"))
    assert fm["concepts"] == ["post-merger-integration"]
    assert fm["organizations"] == ["acme-corp"] and fm["people"] == ["jane-doe"]


def test_projection_validator_passes_on_consistent_projection(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)
    _extract(tmp_path, FakeAdapter())
    _gen_wiki(tmp_path)
    assert validate_projection.main([str(tmp_path)]) == 0


def test_projection_validator_fails_on_missing_link(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)
    _extract(tmp_path, FakeAdapter())
    _gen_wiki(tmp_path)
    page = tmp_path / "wiki" / "Sources" / f"{_sids(tmp_path)['doc.md']}.md"
    # Drop a projected mention link the graph still has active -> forward check fails.
    page.write_text(page.read_text(encoding="utf-8").replace("[[Concepts/post-merger-integration", "Concepts/x"),
                    encoding="utf-8")
    assert validate_projection.main([str(tmp_path)]) == 1


def test_projection_validator_fails_on_extra_link(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)
    _extract(tmp_path, FakeAdapter())
    _gen_wiki(tmp_path)
    page = tmp_path / "wiki" / "Sources" / f"{_sids(tmp_path)['doc.md']}.md"
    # Inject a projected mention link with no active edge -> reverse check fails.
    page.write_text(page.read_text(encoding="utf-8") + "\n- [[Concepts/ghost-concept|Ghost]]\n", encoding="utf-8")
    assert validate_projection.main([str(tmp_path)]) == 1


def test_projection_validator_fails_on_frontmatter_drift(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)
    _extract(tmp_path, FakeAdapter())
    _gen_wiki(tmp_path)
    page = tmp_path / "wiki" / "Sources" / f"{_sids(tmp_path)['doc.md']}.md"
    # Add a slug to the frontmatter concepts array that the body does not link -> drift.
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            'concepts: ["post-merger-integration"]',
            'concepts: ["post-merger-integration", "ghost-concept"]'),
        encoding="utf-8")
    assert validate_projection.main([str(tmp_path)]) == 1


def test_concept_aggregates_mentions_across_sources(tmp_path):
    _build(tmp_path, extra={"doc2.md": "# Other\n\nA distinct second document body.\n"})
    sids = _sids(tmp_path)
    a, b = sids["doc.md"], sids["doc2.md"]
    payload = {"concepts": [{"name": "Shared Concept", "aliases": []}], "entities": []}
    _extract(tmp_path, FakeAdapter(payload=payload), source_ids=[a])
    _extract(tmp_path, FakeAdapter(payload=payload), source_ids=[b])
    page = (tmp_path / "wiki" / "Concepts" / "shared-concept.md").read_text(encoding="utf-8")
    assert f"[[Sources/{a}]]" in page and f"[[Sources/{b}]]" in page  # one page, both sources
    assert "mentioned by 2 source(s)" in page


# --- ADR-0055: tier-2 extraction contract + concept starvation --------------


def test_concept_prompt_contract_pinned():
    # The ADR-0055 contract markers must survive prompt edits, and any wording change must ride a
    # version bump (the fingerprint + cache key both hash CONCEPT_PROMPT_VERSION).
    # v3 = the ADR-0056 entity soft band.
    from app.llm import prompts
    from app.workers import enrichment_artifact as art

    assert art.CONCEPT_PROMPT_VERSION == "enrich-concepts-prompt-v3"
    text = prompts._CONCEPTS_SYSTEM
    assert "UNTRUSTED" in text                                  # untrusted-data framing kept
    assert "typically 3-10" in text                             # concept expectation band
    assert "Never invent a concept to satisfy a count" in text  # no-invention posture
    assert "those belong in `entities`" in text  # no named things in concepts
    for marker in ("references", "bibliographies", "bylines", "author lists", "acknowledgments"):
        assert marker in text                                   # entity-noise boundary
    assert "own authors qualify only if" in text
    # ADR-0056 entity soft band: a count expectation, salience-worded, never schema-enforced.
    assert "typically up to ~25 central entities" in text
    assert "substantively central, not merely mentioned" in text
    assert "never pad" in text


def test_above_cap_document_marked_coverage_truncated(tmp_path):
    # ADR-0056: the honesty marker lives in the ARTIFACT and the job metadata, not stdout.
    from app.workers import enrichment_artifact as art

    _build(tmp_path)
    _gen_wiki(tmp_path)
    summary = _extract(tmp_path, FakeAdapter(), input_max_chars=50)
    sid = _sids(tmp_path)["doc.md"]
    assert summary["coverage_truncated"] == 1
    assert summary["coverage_truncated_sources"] == [sid]
    artifact = json.loads(
        art.concepts_artifact_path(tmp_path / "normalized" / "enrichment", sid).read_text())
    assert artifact["coverage"] == "truncated"
    assert artifact["strategy_ref"] == "full-doc-v1:50"


def test_within_cap_document_marked_coverage_full(tmp_path):
    from app.workers import enrichment_artifact as art

    _build(tmp_path)
    _gen_wiki(tmp_path)
    summary = _extract(tmp_path, FakeAdapter())
    sid = _sids(tmp_path)["doc.md"]
    assert summary["coverage_truncated"] == 0
    artifact = json.loads(
        art.concepts_artifact_path(tmp_path / "normalized" / "enrichment", sid).read_text())
    assert artifact["coverage"] == "full"
    assert artifact["strategy_ref"] == "full-doc-v1:300000"


def test_input_cap_change_restales_the_pass(tmp_path):
    # The knob is part of the strategy ref (cost-bearing semantic knob): changing it must
    # re-extract even though markdown, prompt, and model are unchanged.
    _build(tmp_path)
    _gen_wiki(tmp_path)
    _extract(tmp_path, FakeAdapter())
    again = _extract(tmp_path, FakeAdapter())
    assert again["skipped_fresh"] == 1
    rescoped = _extract(tmp_path, FakeAdapter(), input_max_chars=200000)
    assert rescoped["skipped_fresh"] == 0


def test_concept_starved_predicate_matrix():
    from app.workers import enrichment_artifact as art

    ent = [{"node_type": "person"}] * 5
    assert art.concept_starved(ent, 0)                                    # 5 entities, 0 claims
    assert not art.concept_starved(ent[:4], 0)                            # below threshold
    assert art.concept_starved([{"node_type": "entity"}], 1)              # one claim suffices
    assert not art.concept_starved([], 0)                                 # degenerate doc
    assert not art.concept_starved([{"node_type": "concept"}] + ent, 1)   # concepts present


def test_starved_extraction_reported_in_job_summary(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)
    payload = {"concepts": [], "entities": [
        {"name": f"Person {i}", "entity_type": "person", "aliases": []} for i in range(5)]}
    summary = _extract(tmp_path, FakeAdapter(payload=payload))
    sid = _sids(tmp_path)["doc.md"]
    assert summary["concept_starved"] == 1
    assert summary["concept_starved_sources"] == [sid]


def test_starvation_via_stored_claims(tmp_path):
    # Below the entity threshold, but a durable claims artifact proves substance -> starved.
    _build(tmp_path)
    _gen_wiki(tmp_path)
    sid = _sids(tmp_path)["doc.md"]
    ed = tmp_path / "normalized" / "enrichment"
    ed.mkdir(parents=True, exist_ok=True)
    (ed / f"{sid}.claims.json").write_text(
        json.dumps({"source_id": sid, "claims": [{"claim_id": "clm_0000000000000001"}]}),
        encoding="utf-8")
    payload = {"concepts": [], "entities": [{"name": "Solo Org", "entity_type": "organization",
                                             "aliases": []}]}
    summary = _extract(tmp_path, FakeAdapter(payload=payload))
    assert summary["concept_starved_sources"] == [sid]


def test_sparse_and_healthy_extractions_not_starved(tmp_path):
    # 4 entities + no claims (below threshold) and the default concept-bearing payload: no flag.
    _build(tmp_path, extra={"doc2.md": "# Other\n\nA distinct second document body.\n"})
    sids = _sids(tmp_path)
    sparse = {"concepts": [], "entities": [
        {"name": f"Entity {i}", "entity_type": "entity", "aliases": []} for i in range(4)]}
    s1 = _extract(tmp_path, FakeAdapter(payload=sparse), source_ids=[sids["doc.md"]])
    s2 = _extract(tmp_path, FakeAdapter(), source_ids=[sids["doc2.md"]])
    assert s1["concept_starved"] == 0 and s2["concept_starved"] == 0


# --- ADR-0055 rollout safety: no supersede without a replacement extraction --


class WrongShapeAdapter(FakeAdapter):
    def parse(self, messages, schema, model_id, *, max_tokens):
        self.calls += 1
        return {"wrong": "shape"}  # fails the schema -> ParseError after retries


def _topic_layer_intact(tmp_path, sid):
    from app.backend import graph as g
    page = tmp_path / "wiki" / "Concepts" / "post-merger-integration.md"
    assert parse_frontmatter(page.read_text(encoding="utf-8"))["status"] == "candidate"
    gconn = g.connect(tmp_path / "db" / "graph.sqlite")
    try:
        nid = concepts.node_id("concept", "Post-Merger Integration")
        assert any(e["dst_id"] == nid and e["edge_type"] == "mentions"
                   for e in g.outgoing_active(gconn, sid))
    finally:
        gconn.close()
    dep = [p for p in (tmp_path / "reviews" / "pending").glob("*.json")
           if json.loads(p.read_text(encoding="utf-8"))["type"] == "deprecate_wiki_page"]
    assert not dep


def test_no_key_run_never_supersedes_existing_mentions(tmp_path):
    # The v2 prompt bump makes every artifact stale at once; a key-less run over that state must
    # not touch the graph (supersede-then-skip would tombstone the whole topic layer).
    _build(tmp_path)
    _gen_wiki(tmp_path)
    _extract(tmp_path, FakeAdapter())
    sid = _sids(tmp_path)["doc.md"]

    summary = _extract(tmp_path, FakeAdapter(available=False), force=True)
    assert summary["skipped_no_key"] == 1
    assert summary["node_pages_tombstoned"] == 0
    _topic_layer_intact(tmp_path, sid)


def test_failed_parse_never_supersedes_existing_mentions(tmp_path):
    # A parse failure (e.g. max_tokens truncation) must leave the prior topic layer intact; the
    # artifact stays stale so the next run retries. The failing run gets its own empty response
    # cache — the shared one would (correctly) replay the healthy first-run response.
    _build(tmp_path)
    _gen_wiki(tmp_path)
    _extract(tmp_path, FakeAdapter())
    sid = _sids(tmp_path)["doc.md"]

    client = LLMClient({"anthropic": WrongShapeAdapter()},
                       cache=ResponseCache(tmp_path / "db" / "empty_cache.sqlite"))
    summary = concepts.extract_concepts(tmp_path, client=client, model_ref=MODEL_REF,
                                        jobs_db=tmp_path / "db" / "jobs.sqlite", force=True)
    assert summary["errors"] == 1 and summary["status"] == "partial"
    assert summary["node_pages_tombstoned"] == 0
    _topic_layer_intact(tmp_path, sid)


def test_stored_claim_count_rejects_spoofed_artifact(tmp_path):
    from app.workers import enrichment_artifact as art

    ed = tmp_path / "enrichment"
    ed.mkdir()
    sid = "src_00000000000000aa"
    (ed / f"{sid}.claims.json").write_text(json.dumps(
        {"source_id": "src_00000000000000ff", "claims": [{"claim_id": "clm_1"}]}),
        encoding="utf-8")
    assert art.stored_claim_count(ed, sid) == 0  # internal id must match the filename
    (ed / f"{sid}.claims.json").write_text(json.dumps(
        {"source_id": sid, "claims": [{"claim_id": "clm_1"}]}), encoding="utf-8")
    assert art.stored_claim_count(ed, sid) == 1
