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

from app.backend import graph, manifests, taxonomy
from app.llm.cache import ResponseCache
from app.llm.client import LLMClient
from app.workers import extract, intake, items, wiki
from app.workers.wiki_render import parse_frontmatter
from tests import fixtures

TEMPLATES = ROOT / "templates"
MODEL_REF = "anthropic:claude-sonnet-4-6"

_DEFAULT = {
    "items": [
        {"name": "Post-Merger Integration", "item_type": "method_technique", "aliases": ["PMI"]},
        {"name": "Acme Corp", "item_type": "provider_institution", "aliases": ["Acme"]},
        {"name": "AlphaFold", "item_type": "model", "aliases": []},
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
        return {"items": [dict(i) for i in self._payload["items"]]}


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
    return items.extract_items(tmp_path, client=client, model_ref=MODEL_REF,
                               jobs_db=tmp_path / "db" / "jobs.sqlite", **kw)


def _sids(tmp_path):
    return {m["original_filename"]: m["source_id"]
            for m in manifests.list_manifests(tmp_path / "raw" / "manifests")}


def _payload(*entries):
    return {"items": [dict(e) for e in entries]}


def _item(name, item_type="method_technique", aliases=()):
    return {"name": name, "item_type": item_type, "aliases": list(aliases)}


# --- id / slug -------------------------------------------------------------


def test_node_id_and_slug():
    # ADR-0059: ONE type-neutral id family — classification is metadata, never identity.
    assert items.node_id("Post-Merger Integration").startswith("itm_")
    assert len(items.node_id("Post-Merger Integration")) == len("itm_") + 16
    # Source-agnostic + case/space-insensitive identity; type never enters the hash.
    assert items.node_id("post-merger  integration") == items.node_id("Post-Merger Integration")
    assert items.node_id("Acme") != items.node_id("AlphaFold")
    assert items._slug("Post-Merger Integration") == "post-merger-integration"


# --- worker ----------------------------------------------------------------


def test_extracts_items_routes_pages_and_writes_mentions(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)
    summary = _extract(tmp_path, FakeAdapter())
    sid = _sids(tmp_path)["doc.md"]

    assert summary["nodes_written"] == 3 and summary["mentions_written"] == 3
    assert summary["status"] == "succeeded"

    # ADR-0059: ALL items live flat in wiki/Items/ regardless of item_type.
    pmi = tmp_path / "wiki" / "Items" / "post-merger-integration.md"
    acme = tmp_path / "wiki" / "Items" / "acme-corp.md"
    model = tmp_path / "wiki" / "Items" / "alphafold.md"
    assert pmi.exists() and acme.exists() and model.exists()
    fm = parse_frontmatter(pmi.read_text(encoding="utf-8"))
    assert fm["type"] == "item" and fm["status"] == "candidate" and fm["item_id"].startswith("itm_")
    assert fm["item_type"] == "method_technique"
    assert parse_frontmatter(acme.read_text(encoding="utf-8"))["item_type"] == "provider_institution"
    assert parse_frontmatter(model.read_text(encoding="utf-8"))["item_type"] == "model"
    assert "PMI" in pmi.read_text(encoding="utf-8")  # alias rendered
    assert f"[[Sources/{sid}]]" in pmi.read_text(encoding="utf-8")  # mentioned-by

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


def test_item_need_not_appear_verbatim(tmp_path):
    # "Post-Merger Integration" is not in the source text, yet it is still extracted (no
    # verbatim grounding for items — recurrence is the gate, ADR-0026/0018/0059).
    _build(tmp_path)
    summary = _extract(tmp_path, FakeAdapter(payload=_payload(_item("Post-Merger Integration"))))
    assert summary["nodes_written"] == 1
    assert (tmp_path / "wiki" / "Items" / "post-merger-integration.md").exists()


def test_unknown_item_type_coerced_to_sentinel(tmp_path):
    # ADR-0059: model output is untrusted — an item_type outside the taxonomy must land in
    # the QA sentinel, never mint a new classification. The real client's schema enum would
    # reject it upstream; the worker's own coercion is the defense-in-depth pin, so drive it
    # with a raw stub client that skips schema validation.
    class RawClient:
        def provider_available(self, model_ref):
            return True

        def parse(self, messages, schema, model_ref, **kw):
            return _payload(_item("Mystery Thing", item_type="entity"))

    _build(tmp_path)
    summary = items.extract_items(tmp_path, client=RawClient(), model_ref=MODEL_REF,
                                  jobs_db=tmp_path / "db" / "jobs.sqlite")
    assert summary["unclassified_items"] == 1
    page = tmp_path / "wiki" / "Items" / "mystery-thing.md"
    assert page.exists()
    assert parse_frontmatter(page.read_text(encoding="utf-8"))["item_type"] == taxonomy.UNCLASSIFIED


def test_sentinel_item_type_accepted_and_counted(tmp_path):
    # The sentinel is schema-legal model output (ITEM_TYPES_ALL): stored on the candidate
    # and surfaced via the NEW unclassified_items summary key.
    _build(tmp_path)
    summary = _extract(tmp_path, FakeAdapter(payload=_payload(
        _item("Fuzzy Notion", item_type=taxonomy.UNCLASSIFIED))))
    assert summary["unclassified_items"] == 1 and summary["nodes_written"] == 1
    fm = parse_frontmatter((tmp_path / "wiki" / "Items" / "fuzzy-notion.md").read_text(encoding="utf-8"))
    assert fm["item_type"] == taxonomy.UNCLASSIFIED and fm["status"] == "candidate"


def test_source_page_projects_item_mentions(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)
    _extract(tmp_path, FakeAdapter())
    _gen_wiki(tmp_path)  # refresh Source page with mention links
    sid = _sids(tmp_path)["doc.md"]
    text = (tmp_path / "wiki" / "Sources" / f"{sid}.md").read_text(encoding="utf-8")
    assert "[[Items/post-merger-integration|" in text
    assert "[[Items/acme-corp|" in text
    assert "[[Items/alphafold|" in text
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
    _extract(tmp_path, FakeAdapter(payload=_payload(_item("Old Topic"))))
    old = tmp_path / "wiki" / "Items" / "old-topic.md"
    assert old.exists() and parse_frontmatter(old.read_text(encoding="utf-8"))["status"] == "candidate"

    # Change the source text so re-extraction is a cache miss (new prompt -> new items).
    sid = _sids(tmp_path)["doc.md"]
    (tmp_path / "normalized" / "markdown" / f"{sid}.md").write_text(
        "# T\n\nAn entirely replacement body sentence.\n", encoding="utf-8")
    summary = _extract(tmp_path, FakeAdapter(payload=_payload(_item("New Topic"))))
    assert summary["node_pages_tombstoned"] >= 1
    assert parse_frontmatter(old.read_text(encoding="utf-8"))["status"] == "deprecated_candidate"  # tombstoned, not deleted
    assert (tmp_path / "wiki" / "Items" / "new-topic.md").exists()


def _pending_reviews(tmp_path):
    d = tmp_path / "reviews" / "pending"
    return [json.loads(p.read_text(encoding="utf-8")) for p in d.glob("*.json")] if d.exists() else []


def _two_sources(tmp_path):
    _build(tmp_path, extra={"doc2.md": "# Other\n\nA distinct second document body here.\n"})
    s = _sids(tmp_path)
    return s["doc.md"], s["doc2.md"]


def test_alias_union_across_sources_preserves_both(tmp_path):
    a, b = _two_sources(tmp_path)
    _extract(tmp_path, FakeAdapter(payload=_payload(_item("Shared", aliases=["AliasA"]))), source_ids=[a])
    _extract(tmp_path, FakeAdapter(payload=_payload(_item("Shared", aliases=["AliasB"]))), source_ids=[b])
    page = (tmp_path / "wiki" / "Items" / "shared.md").read_text(encoding="utf-8")
    assert "AliasA" in page and "AliasB" in page  # union — the first source's alias is not lost


def test_item_type_preserved_across_recompose(tmp_path):
    # ADR-0059: the page owns the classification. A re-extraction (force, same payload)
    # recomposes the page and must carry the item_type through unchanged.
    _build(tmp_path)
    _extract(tmp_path, FakeAdapter(payload=_payload(_item("Durable", item_type="architecture_pattern"))))
    page = tmp_path / "wiki" / "Items" / "durable.md"
    assert parse_frontmatter(page.read_text(encoding="utf-8"))["item_type"] == "architecture_pattern"
    _extract(tmp_path, FakeAdapter(payload=_payload(_item("Durable", item_type="architecture_pattern"))),
             force=True)
    fm = parse_frontmatter(page.read_text(encoding="utf-8"))
    assert fm["item_type"] == "architecture_pattern" and fm["status"] == "candidate"
    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        node = graph.get_node(conn, items.node_id("Durable"))
    finally:
        conn.close()
    assert node["item_type"] == "architecture_pattern"  # graph mirror matches the page


def test_type_conflict_keeps_existing_node_and_files_review(tmp_path):
    a, b = _two_sources(tmp_path)
    _extract(tmp_path, FakeAdapter(payload=_payload(
        _item("Acme", item_type="provider_institution"))), source_ids=[a])
    page = tmp_path / "wiki" / "Items" / "acme.md"
    assert page.exists()

    _extract(tmp_path, FakeAdapter(payload=_payload(
        _item("Acme", item_type="product_tool_platform"))), source_ids=[b])
    # No second node minted; the mention routes to the existing node whose page keeps its type.
    fm = parse_frontmatter(page.read_text(encoding="utf-8"))
    assert fm["item_type"] == "provider_institution"
    assert "mentioned by 2 source(s)" in page.read_text(encoding="utf-8")
    # ADR-0059 decision 3 producer contract: subject keys on {node_id, to_item_type} so a
    # rejected retype to one type does not lock out retyping to another.
    revs = [r for r in _pending_reviews(tmp_path) if r["type"] == "change_item_type"]
    assert len(revs) == 1
    nid = items.node_id("Acme")
    assert revs[0]["subject"] == {"node_id": nid, "to_item_type": "product_tool_platform"}
    assert revs[0]["proposal"] == {"to_item_type": "product_tool_platform"}
    assert revs[0]["context"] == {"source_id": b, "name": "Acme",
                                  "from_item_type": "provider_institution"}


def test_sentinel_classification_never_files_type_conflict(tmp_path):
    # ADR-0059: an unsure classification is not a correction — a sentinel-classified mention
    # of an existing typed name routes to the node without proposing a retype.
    a, b = _two_sources(tmp_path)
    _extract(tmp_path, FakeAdapter(payload=_payload(
        _item("Acme", item_type="provider_institution"))), source_ids=[a])
    summary = _extract(tmp_path, FakeAdapter(payload=_payload(
        _item("Acme", item_type=taxonomy.UNCLASSIFIED))), source_ids=[b])
    assert not any(r["type"] == "change_item_type" for r in _pending_reviews(tmp_path))
    # The existing node's type stands; the routed mention is not counted unclassified.
    fm = parse_frontmatter((tmp_path / "wiki" / "Items" / "acme.md").read_text(encoding="utf-8"))
    assert fm["item_type"] == "provider_institution"
    assert summary["unclassified_items"] == 0


def test_candidate_promotion_review_items_are_filed(tmp_path):
    _build(tmp_path)
    _extract(tmp_path, FakeAdapter())
    reviews = _pending_reviews(tmp_path)
    promote = [r for r in reviews if r["type"] == "promote_candidate_node"]
    assert len(promote) == 3 and all(r["status"] == "pending" for r in promote)
    # ADR-0059: the promote proposal carries the used classification under `item_type`.
    by_name = {r["proposal"]["name"]: r["proposal"] for r in promote}
    assert by_name["Post-Merger Integration"] == {
        "to_status": "active", "name": "Post-Merger Integration", "item_type": "method_technique"}
    assert all("node_type" not in p for p in by_name.values())


def test_tombstone_files_deprecation_review(tmp_path):
    _build(tmp_path)
    _extract(tmp_path, FakeAdapter(payload=_payload(_item("Old"))))
    sid = _sids(tmp_path)["doc.md"]
    (tmp_path / "normalized" / "markdown" / f"{sid}.md").write_text("# T\n\nReplacement body.\n", encoding="utf-8")
    _extract(tmp_path, FakeAdapter(payload=_payload(_item("New"))))
    assert any(r["type"] == "deprecate_wiki_page" for r in _pending_reviews(tmp_path))


def test_oversized_llm_output_is_bounded(tmp_path):
    _build(tmp_path)
    big_name = "A" * 500
    many = [f"alias-{i}-" + "x" * 200 for i in range(40)]
    _extract(tmp_path, FakeAdapter(payload=_payload(_item(big_name, aliases=many))))
    pages = list((tmp_path / "wiki" / "Items").glob("*.md"))
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


def test_source_items_section_groups_by_type_in_priority_order(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)
    _extract(tmp_path, FakeAdapter())
    _gen_wiki(tmp_path)
    text = (tmp_path / "wiki" / "Sources" / f"{_sids(tmp_path)['doc.md']}.md").read_text(encoding="utf-8")
    section = _section(text, "Items Mentioned")
    # ONE section; the item_type groups render INSIDE it as ### headers in GROUP_ORDER
    # (model before method_technique before provider_institution in PRIORITY_ORDER).
    assert text.count("## Items Mentioned") == 1
    assert "## Concepts Mentioned" not in text and "## Entities Mentioned" not in text
    order = [section.index(h) for h in
             ("### Model", "### Method Technique", "### Provider Institution")]
    assert order == sorted(order)
    assert "[[Items/alphafold" in section.split("### Method Technique")[0]
    assert "[[Items/post-merger-integration" in section.split("### Method Technique")[1]
    assert "[[Items/acme-corp" in section.split("### Provider Institution")[1]


def test_source_frontmatter_items_array_mirrors_body(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)
    _extract(tmp_path, FakeAdapter())
    _gen_wiki(tmp_path)
    text = (tmp_path / "wiki" / "Sources" / f"{_sids(tmp_path)['doc.md']}.md").read_text(encoding="utf-8")
    fm = parse_frontmatter(text)
    # ONE items array (replaces the five old per-family arrays), slug-ordered.
    assert fm["items"] == ["acme-corp", "alphafold", "post-merger-integration"]
    for gone in ("concepts", "entities", "people", "organizations", "projects"):
        assert gone not in fm


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
    page.write_text(page.read_text(encoding="utf-8").replace("[[Items/post-merger-integration", "Items/x"),
                    encoding="utf-8")
    assert validate_projection.main([str(tmp_path)]) == 1


def test_projection_validator_fails_on_extra_link(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)
    _extract(tmp_path, FakeAdapter())
    _gen_wiki(tmp_path)
    page = tmp_path / "wiki" / "Sources" / f"{_sids(tmp_path)['doc.md']}.md"
    # Inject a projected mention link with no active edge -> reverse check fails.
    page.write_text(page.read_text(encoding="utf-8") + "\n- [[Items/ghost-item|Ghost]]\n", encoding="utf-8")
    assert validate_projection.main([str(tmp_path)]) == 1


def test_projection_validator_fails_on_frontmatter_drift(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)
    _extract(tmp_path, FakeAdapter())
    _gen_wiki(tmp_path)
    page = tmp_path / "wiki" / "Sources" / f"{_sids(tmp_path)['doc.md']}.md"
    # Add a slug to the frontmatter items array that the body does not link -> drift.
    page.write_text(
        page.read_text(encoding="utf-8").replace("items: [", 'items: ["ghost-item", ', 1),
        encoding="utf-8")
    assert validate_projection.main([str(tmp_path)]) == 1


def test_item_aggregates_mentions_across_sources(tmp_path):
    _build(tmp_path, extra={"doc2.md": "# Other\n\nA distinct second document body.\n"})
    sids = _sids(tmp_path)
    a, b = sids["doc.md"], sids["doc2.md"]
    payload = _payload(_item("Shared Topic"))
    _extract(tmp_path, FakeAdapter(payload=payload), source_ids=[a])
    _extract(tmp_path, FakeAdapter(payload=payload), source_ids=[b])
    page = (tmp_path / "wiki" / "Items" / "shared-topic.md").read_text(encoding="utf-8")
    assert f"[[Sources/{a}]]" in page and f"[[Sources/{b}]]" in page  # one page, both sources
    assert "mentioned by 2 source(s)" in page


# --- ADR-0059: items extraction contract + topic starvation ------------------


def test_items_prompt_contract_pinned():
    # The ADR-0059 contract markers must survive prompt edits, and any wording change must
    # ride a version bump (the fingerprint + cache key both hash ITEMS_PROMPT_VERSION).
    from app.llm import prompts
    from app.workers import enrichment_artifact as art

    assert art.ITEMS_PROMPT_VERSION == "enrich-items-prompt-v1"
    assert art.ITEMS_SCHEMA_VERSION == "enrich-items-v1"
    text = prompts._ITEMS_SYSTEM
    assert "UNTRUSTED" in text                                   # untrusted-data framing kept
    # 15-step priority order: every production type appears, numbered 1..15.
    for i, itype in enumerate(taxonomy.PRIORITY_ORDER, start=1):
        assert f"{i}. {itype}" in text
    # Band guidance by group (thematic 3-10; named up to ~25, salience-worded, never padded).
    assert "typically 3-10 central items" in text
    assert "up to ~25" in text
    assert "substantively central" in text
    assert "Never invent an item or pad to satisfy a count" in text
    # People / publication / bibliography / host-document exclusions.
    assert "never return a person" in text
    assert "books are never items" in text
    for marker in ("references", "bibliographies", "bylines", "author lists", "acknowledgments"):
        assert marker in text
    assert "never an item itself" in text                        # the host document
    # Substrate carve-out + sentinel guidance.
    assert "runtime substrate" in text
    assert taxonomy.UNCLASSIFIED in text
    assert "never use it to avoid choosing" in text


def test_items_schema_is_single_array_with_type_enum():
    from app.llm import prompts

    schema = prompts.ITEMS_SCHEMA
    assert set(schema["properties"]) == {"items"}                # the two-array shape is gone
    entry = schema["properties"]["items"]["items"]
    assert set(entry["properties"]) == {"name", "item_type", "aliases"}
    assert entry["additionalProperties"] is False
    assert set(entry["properties"]["item_type"]["enum"]) == set(taxonomy.ITEM_TYPES_ALL)


def test_above_cap_document_marked_coverage_truncated(tmp_path):
    # ADR-0056 (carried over): the honesty marker lives in the ARTIFACT and job metadata.
    from app.workers import enrichment_artifact as art

    _build(tmp_path)
    _gen_wiki(tmp_path)
    summary = _extract(tmp_path, FakeAdapter(), input_max_chars=50)
    sid = _sids(tmp_path)["doc.md"]
    assert summary["coverage_truncated"] == 1
    assert summary["coverage_truncated_sources"] == [sid]
    artifact = json.loads(
        art.items_artifact_path(tmp_path / "normalized" / "enrichment", sid).read_text())
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
        art.items_artifact_path(tmp_path / "normalized" / "enrichment", sid).read_text())
    assert artifact["coverage"] == "full"
    assert artifact["strategy_ref"] == "full-doc-v1:300000"
    # Artifact node entries are {node_id, item_type, name, aliases} — no node_type.
    node = artifact["nodes"][0]
    assert set(node) == {"node_id", "item_type", "name", "aliases"}


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


def test_topic_starved_predicate_matrix():
    from app.workers import enrichment_artifact as art

    named = [{"item_type": "provider_institution"}] * 5
    assert art.topic_starved(named, 0)                                     # 5 named, 0 claims
    assert not art.topic_starved(named[:4], 0)                             # below threshold
    assert art.topic_starved([{"item_type": "model"}], 1)                  # one claim suffices
    assert not art.topic_starved([], 0)                                    # degenerate doc
    assert not art.topic_starved([{"item_type": "domain"}] + named, 1)     # thematic present
    # The sentinel counts toward NEITHER group.
    sentinels = [{"item_type": taxonomy.UNCLASSIFIED}] * 5
    assert not art.topic_starved(sentinels, 0)
    assert art.topic_starved(sentinels, 1)                                 # ...but a claim still proves substance


def test_starved_extraction_reported_in_job_summary(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)
    payload = _payload(*[_item(f"Org {i}", item_type="provider_institution") for i in range(5)])
    summary = _extract(tmp_path, FakeAdapter(payload=payload))
    sid = _sids(tmp_path)["doc.md"]
    assert summary["topic_starved"] == 1
    assert summary["topic_starved_sources"] == [sid]


def test_starvation_via_stored_claims(tmp_path):
    # Below the named-item threshold, but a durable claims artifact proves substance -> starved.
    _build(tmp_path)
    _gen_wiki(tmp_path)
    sid = _sids(tmp_path)["doc.md"]
    ed = tmp_path / "normalized" / "enrichment"
    ed.mkdir(parents=True, exist_ok=True)
    (ed / f"{sid}.claims.json").write_text(
        json.dumps({"source_id": sid, "claims": [{"claim_id": "clm_0000000000000001"}]}),
        encoding="utf-8")
    summary = _extract(tmp_path, FakeAdapter(payload=_payload(
        _item("Solo Org", item_type="provider_institution"))))
    assert summary["topic_starved_sources"] == [sid]


def test_sparse_and_healthy_extractions_not_starved(tmp_path):
    # 4 named items + no claims (below threshold) and the default thematic-bearing payload: no flag.
    _build(tmp_path, extra={"doc2.md": "# Other\n\nA distinct second document body.\n"})
    sids = _sids(tmp_path)
    sparse = _payload(*[_item(f"Tool {i}", item_type="product_tool_platform") for i in range(4)])
    s1 = _extract(tmp_path, FakeAdapter(payload=sparse), source_ids=[sids["doc.md"]])
    s2 = _extract(tmp_path, FakeAdapter(), source_ids=[sids["doc2.md"]])
    assert s1["topic_starved"] == 0 and s2["topic_starved"] == 0


# --- rollout safety: no supersede without a replacement extraction -----------


class WrongShapeAdapter(FakeAdapter):
    def parse(self, messages, schema, model_id, *, max_tokens):
        self.calls += 1
        return {"wrong": "shape"}  # fails the schema -> ParseError after retries


def _topic_layer_intact(tmp_path, sid):
    from app.backend import graph as g
    page = tmp_path / "wiki" / "Items" / "post-merger-integration.md"
    assert parse_frontmatter(page.read_text(encoding="utf-8"))["status"] == "candidate"
    gconn = g.connect(tmp_path / "db" / "graph.sqlite")
    try:
        nid = items.node_id("Post-Merger Integration")
        assert any(e["dst_id"] == nid and e["edge_type"] == "mentions"
                   for e in g.outgoing_active(gconn, sid))
    finally:
        gconn.close()
    dep = [p for p in (tmp_path / "reviews" / "pending").glob("*.json")
           if json.loads(p.read_text(encoding="utf-8"))["type"] == "deprecate_wiki_page"]
    assert not dep


def test_no_key_run_never_supersedes_existing_mentions(tmp_path):
    # A prompt/schema bump makes every artifact stale at once; a key-less run over that state
    # must not touch the graph (supersede-then-skip would tombstone the whole topic layer).
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
    summary = items.extract_items(tmp_path, client=client, model_ref=MODEL_REF,
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
