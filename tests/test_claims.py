from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import validate_citations  # noqa: E402
import validate_frontmatter  # noqa: E402
import validate_graph  # noqa: E402
import validate_wikilinks  # noqa: E402

from app.backend import graph, manifests
from app.llm.cache import ResponseCache
from app.llm.client import LLMClient
from app.workers import claims, extract, intake, wiki
from app.workers.citations import locate_quote
from app.workers.wiki_render import parse_frontmatter
from tests import fixtures

TEMPLATES = ROOT / "templates"
MODEL_REF = "anthropic:claude-sonnet-4-6"
Q1 = "First paragraph of the document."
Q2 = "Second paragraph under a section."


class FakeAdapter:
    name = "anthropic"
    supports_batch = False

    def __init__(self, claims_payload=None, *, available=True):
        self.calls = 0
        self._claims = claims_payload if claims_payload is not None else [
            {"claim": "The document has a first paragraph.", "quote": Q1},
            {"claim": "The document has a section with a second paragraph.", "quote": Q2},
        ]
        self._available = available

    def available(self):
        return self._available

    def parse(self, messages, schema, model_id, *, max_tokens):
        self.calls += 1
        return {"claims": [dict(c) for c in self._claims]}


def _build(tmp_path: Path) -> Path:
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.write_markdown(inbox / "doc.md")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    extract.extract_sources(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    return tmp_path


def _gen_wiki(tmp_path):
    return wiki.generate_wiki(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite",
                              templates_dir=TEMPLATES, rebuild_index=False)


def _client(tmp_path, adapter):
    return LLMClient({"anthropic": adapter}, cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite"))


def _extract(tmp_path, adapter, **kw):
    return claims.extract_claims(
        tmp_path, client=_client(tmp_path, adapter), model_ref=MODEL_REF,
        jobs_db=tmp_path / "db" / "jobs.sqlite", **kw,
    )


def _sids(tmp_path):
    return {m["original_filename"]: m["source_id"]
            for m in manifests.list_manifests(tmp_path / "raw" / "manifests")}


# --- claim_id + locate_quote -----------------------------------------------


def test_claim_id_is_source_agnostic_and_stable():
    assert claims.claim_id("The   sky is  blue.") == claims.claim_id("The sky is blue.")
    assert claims.claim_id("x").startswith("clm_")


def test_locate_quote_whitespace_flexible():
    md = "alpha   beta\ngamma delta"
    assert locate_quote(md, "beta gamma") == (md.index("beta"), md.index("gamma") + len("gamma"))
    assert locate_quote(md, "not present") is None


# --- worker ----------------------------------------------------------------


def test_extracts_grounds_writes_pages_and_edges(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)
    summary = _extract(tmp_path, FakeAdapter())
    sid = _sids(tmp_path)["doc.md"]

    assert summary["claims_written"] == 2 and summary["claim_pages_written"] == 2
    assert summary["status"] == "succeeded"

    pages = sorted((tmp_path / "wiki" / "Claims").glob("*.md"))
    assert len(pages) == 2
    fm = parse_frontmatter(pages[0].read_text(encoding="utf-8"))
    assert fm["type"] == "claim" and fm["review_status"] == "none"

    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        edges = conn.execute("SELECT edge_type, status, dst_id FROM edges").fetchall()
    finally:
        conn.close()
    assert len(edges) == 2
    assert all(e["edge_type"] == "derived_from" and e["status"] == "active"
               and e["dst_id"] == sid for e in edges)

    assert validate_frontmatter.main([str(tmp_path)]) == 0
    assert validate_citations.main([str(tmp_path)]) == 0
    assert validate_graph.main([str(tmp_path)]) == 0
    assert validate_wikilinks.main([str(tmp_path)]) == 0


def test_source_node_status_follows_manifest(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)
    sid = _sids(tmp_path)["doc.md"]
    manifests.set_status(tmp_path / "raw" / "manifests", sid, "deprecated_candidate")

    _extract(tmp_path, FakeAdapter())

    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        assert graph.get_node(conn, sid)["status"] == "deprecated_candidate"
    finally:
        conn.close()


def test_special_chars_quote_round_trips_and_validates(tmp_path):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "sp.md").write_text('# Doc\n\nAlpha "beta" | gamma [[delta]] omega end.\n', encoding="utf-8")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    extract.extract_sources(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    _gen_wiki(tmp_path)

    quote = 'Alpha "beta" | gamma [[delta]] omega end.'
    summary = _extract(tmp_path, FakeAdapter(claims_payload=[
        {"claim": "A claim with tricky punctuation.", "quote": quote}]))
    assert summary["claims_written"] == 1

    # The faithful quote grounds, and no literal [[ leaks into the file for wikilink checks.
    assert validate_citations.main([str(tmp_path)]) == 0
    assert validate_wikilinks.main([str(tmp_path)]) == 0
    page = next((tmp_path / "wiki" / "Claims").glob("*.md")).read_text(encoding="utf-8")
    assert "[[delta]]" not in page  # neutralised everywhere it would render/resolve


def test_unlocatable_quote_is_dropped(tmp_path):
    _build(tmp_path)
    summary = _extract(tmp_path, FakeAdapter(claims_payload=[
        {"claim": "Real claim.", "quote": Q1},
        {"claim": "Hallucinated.", "quote": "this text is nowhere in the source"}]))
    assert summary["claims_written"] == 1 and summary["claims_dropped_ungrounded"] == 1


def test_no_api_key_skips_and_writes_nothing(tmp_path):
    _build(tmp_path)
    fake = FakeAdapter(available=False)
    summary = _extract(tmp_path, fake)
    assert summary["status"] == "skipped" and summary["claims_written"] == 0 and fake.calls == 0
    assert not (tmp_path / "wiki" / "Claims").exists() or not list((tmp_path / "wiki" / "Claims").glob("*.md"))


def test_idempotent_skip_and_force(tmp_path):
    _build(tmp_path)
    assert _extract(tmp_path, FakeAdapter())["claims_written"] == 2
    second = _extract(tmp_path, FakeAdapter())
    assert second["claims_written"] == 0 and second["skipped_fresh"] == 1
    assert _extract(tmp_path, FakeAdapter(), force=True)["claims_written"] == 2


def test_claim_pages_are_idempotent_byte_stable(tmp_path):
    _build(tmp_path)
    _extract(tmp_path, FakeAdapter())
    page = sorted((tmp_path / "wiki" / "Claims").glob("*.md"))[0]
    first = page.read_text(encoding="utf-8")
    _extract(tmp_path, FakeAdapter(), force=True)
    assert page.read_text(encoding="utf-8") == first  # no wall-clock churn


def _set_md(tmp_path, sid, text):
    # Markdown and chunks are always written together by extraction (ADR-0012 anchor contract,
    # validate_normalized-enforced), and the ADR-0056 window planner trusts that invariant —
    # so this shortcut must maintain it: one whole-document chunk matching the new text.
    (tmp_path / "normalized" / "markdown" / f"{sid}.md").write_text(text, encoding="utf-8")
    chunk = {"chunk_id": f"{sid}::0000", "source_id": sid, "ordinal": 0, "kind": "prose",
             "heading_path": [], "section": None, "text": text, "char_start": 0,
             "char_end": len(text), "page": None, "page_end": None,
             "table_reference": None, "sheet_reference": None}
    (tmp_path / "normalized" / "chunks" / f"{sid}.jsonl").write_text(
        json.dumps(chunk, ensure_ascii=False) + "\n", encoding="utf-8")


def _edge_statuses(tmp_path):
    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        return {r["src_id"]: r["status"] for r in conn.execute("SELECT src_id, status FROM edges")}
    finally:
        conn.close()


def test_reextraction_tombstones_orphan_and_supersedes_edges(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)
    _extract(tmp_path, FakeAdapter(claims_payload=[{"claim": "Old claim about paragraphs.", "quote": Q1}]))
    old_id = claims.claim_id("Old claim about paragraphs.")
    sid = _sids(tmp_path)["doc.md"]

    _set_md(tmp_path, sid, "# T\n\nA brand new sentence about widgets.\n")
    summary = _extract(tmp_path, FakeAdapter(claims_payload=[
        {"claim": "New claim about widgets.", "quote": "A brand new sentence about widgets."}]))
    new_id = claims.claim_id("New claim about widgets.")

    # Orphan claim is tombstoned (not deleted — rule 9): page kept, status deprecated.
    assert summary["claim_pages_tombstoned"] >= 1
    old_page = tmp_path / "wiki" / "Claims" / f"{old_id}.md"
    assert old_page.exists()
    assert parse_frontmatter(old_page.read_text(encoding="utf-8"))["status"] == "deprecated_candidate"
    assert (tmp_path / "wiki" / "Claims" / f"{new_id}.md").exists()

    statuses = _edge_statuses(tmp_path)
    assert statuses[old_id] == "superseded" and statuses[new_id] == "active"

    # Tombstoning a Claim page files a deprecate_wiki_page review item (B1, like items).
    pending = tmp_path / "reviews" / "pending"
    items = [json.loads(p.read_text(encoding="utf-8")) for p in pending.glob("*.json")]
    assert any(r["type"] == "deprecate_wiki_page" and r["subject"]["node_id"] == old_id for r in items)
    assert validate_citations.main([str(tmp_path)]) == 0  # tombstone exempt from citation req
    assert validate_frontmatter.main([str(tmp_path)]) == 0


def test_reextraction_without_key_preserves_claim_layer(tmp_path):
    # ADR-0056 decision 3 (reverses the earlier retract-first ordering): a run that cannot
    # produce the replacement never supersedes existing evidence — stale-but-VISIBLE.
    _build(tmp_path)
    _gen_wiki(tmp_path)
    _extract(tmp_path, FakeAdapter(claims_payload=[{"claim": "Claim X.", "quote": Q1}]))
    cid = claims.claim_id("Claim X.")
    sid = _sids(tmp_path)["doc.md"]

    _set_md(tmp_path, sid, "# T\n\nUnrelated replacement text here now.\n")
    summary = _extract(tmp_path, FakeAdapter(available=False))
    assert summary["skipped_no_key"] == 1
    # Missing key is in the "cannot produce a complete replacement" class (review round 2).
    assert summary["replacement_not_applied"] == 1
    assert summary["stale_claim_layer_preserved"] == 1
    assert _edge_statuses(tmp_path)[cid] == "active"  # preserved, not silently thinned
    page = tmp_path / "wiki" / "Claims" / f"{cid}.md"
    assert parse_frontmatter(page.read_text(encoding="utf-8"))["status"] == "active"
    # The changed Markdown makes the preserved anchor stale — validators fail LOUDLY until
    # extract_claims succeeds (the honest degraded state the ADR prefers over silent absence).
    assert validate_citations.main([str(tmp_path)]) != 0


def test_reextraction_parse_failure_preserves_claim_layer(tmp_path):
    # ADR-0056 staging: one bad window must not wipe a source's existing claim layer, and the
    # summary distinguishes "nothing changed because staging failed" from "zero claims".
    _build(tmp_path)
    _gen_wiki(tmp_path)
    _extract(tmp_path, FakeAdapter(claims_payload=[{"claim": "Claim Y.", "quote": Q1}]))
    cid = claims.claim_id("Claim Y.")
    sid = _sids(tmp_path)["doc.md"]

    class BrokenAdapter(FakeAdapter):
        def parse(self, messages, schema, model_id, *, max_tokens):
            return {"wrong": "shape"}  # fails the schema -> ParseError after retries

    _set_md(tmp_path, sid, "# T\n\nReplacement body that no longer supports Claim Y.\n")
    summary = _extract(tmp_path, BrokenAdapter())
    assert summary["errors"] == 1
    assert summary["replacement_not_applied"] == 1
    assert summary["stale_claim_layer_preserved"] == 1
    assert _edge_statuses(tmp_path)[cid] == "active"  # untouched: no replacement was staged


def test_claim_text_authority_is_the_page(tmp_path):
    _build(tmp_path)
    _extract(tmp_path, FakeAdapter(claims_payload=[{"claim": "A durable claim.", "quote": Q1}]))
    cid = claims.claim_id("A durable claim.")
    page = tmp_path / "wiki" / "Claims" / f"{cid}.md"
    # Page frontmatter is the authority for the wording; reconstruct it from the page alone.
    assert claims._read_claim_text(page) == "A durable claim."


def _src_page_text(tmp_path):
    sid = _sids(tmp_path)["doc.md"]
    return (tmp_path / "wiki" / "Sources" / f"{sid}.md").read_text(encoding="utf-8")


def test_source_page_projects_active_claims(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)  # before any claims: Claims section is the placeholder
    assert "_Pending semantic enrichment._" in _src_page_text(tmp_path)

    _extract(tmp_path, FakeAdapter(claims_payload=[{"claim": "The doc has a first paragraph.", "quote": Q1}]))
    _gen_wiki(tmp_path)  # refresh: project the source's active claims
    cid = claims.claim_id("The doc has a first paragraph.")
    text = _src_page_text(tmp_path)
    assert f"[[Claims/{cid}|" in text  # linked with a short title
    assert "The doc has a first paragraph" in text  # the resolved label
    assert validate_wikilinks.main([str(tmp_path)]) == 0  # source<->claim links resolve


def test_source_claims_drop_when_superseded(tmp_path):
    _build(tmp_path)
    _gen_wiki(tmp_path)
    _extract(tmp_path, FakeAdapter(claims_payload=[{"claim": "Old Z.", "quote": Q1}]))
    _gen_wiki(tmp_path)
    old = claims.claim_id("Old Z.")
    assert f"[[Claims/{old}" in _src_page_text(tmp_path)

    sid = _sids(tmp_path)["doc.md"]
    _set_md(tmp_path, sid, "# T\n\nA replacement sentence entirely.\n")
    _extract(tmp_path, FakeAdapter(claims_payload=[{"claim": "New W.", "quote": "A replacement sentence entirely."}]))
    _gen_wiki(tmp_path)
    new = claims.claim_id("New W.")
    text = _src_page_text(tmp_path)
    assert f"[[Claims/{new}" in text          # the active claim is projected
    assert f"[[Claims/{old}" not in text      # the superseded one is no longer listed


def test_source_page_byte_stable_when_claims_unchanged(tmp_path):
    _build(tmp_path)
    _extract(tmp_path, FakeAdapter(claims_payload=[{"claim": "Stable claim.", "quote": Q1}]))
    _gen_wiki(tmp_path)
    first = _src_page_text(tmp_path)
    _gen_wiki(tmp_path)  # re-run with no changes
    assert _src_page_text(tmp_path) == first  # projection is deterministic, no churn


def test_validate_citations_requires_a_manifest(tmp_path):
    # A claim page citing a source whose manifest is absent must fail (ADR-0020).
    (tmp_path / "normalized" / "markdown").mkdir(parents=True)
    (tmp_path / "normalized" / "markdown" / "src_0123456789abcdef.md").write_text(
        "Alpha beta gamma.", encoding="utf-8")
    cp = tmp_path / "wiki" / "Claims" / "clm_x.md"
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(
        '---\ntype: claim\nclaim_id: clm_x\nstatus: active\ncitations:\n'
        '  - source_id: "src_0123456789abcdef"\n    char_start: 0\n    char_end: 5\n'
        '    quote: "Alpha"\n---\n\n## Evidence\n', encoding="utf-8")
    assert validate_citations.main([str(tmp_path)]) == 1  # no manifest for that source


def test_cross_run_aggregation_into_one_claim_page(tmp_path):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.write_markdown(inbox / "doc.md")  # contains Q1
    (inbox / "doc2.md").write_text(f"# Other\n\n{Q1}\n\nA different tail sentence.\n", encoding="utf-8")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    extract.extract_sources(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    sids = _sids(tmp_path)
    a, b = sids["doc.md"], sids["doc2.md"]
    assert a != b  # genuinely distinct sources

    payload = [{"claim": "A shared statement.", "quote": Q1}]
    _extract(tmp_path, FakeAdapter(claims_payload=payload), source_ids=[a])
    cid = claims.claim_id("A shared statement.")
    page = tmp_path / "wiki" / "Claims" / f"{cid}.md"
    assert page.read_text(encoding="utf-8").count("[[Sources/") == 1  # one source so far

    _extract(tmp_path, FakeAdapter(claims_payload=payload), source_ids=[b])
    # Rendered from the graph -> both sources' citations now on the one page.
    text = page.read_text(encoding="utf-8")
    assert f"[[Sources/{a}]]" in text and f"[[Sources/{b}]]" in text
    assert validate_citations.main([str(tmp_path)]) == 0


def test_enrichment_artifacts_are_gitignored(tmp_path):
    # git check-ignore against the real repo paths (the rule lives in the committed .gitignore).
    rels = ["normalized/enrichment/src_x.json", "normalized/enrichment/src_x.claims.json",
            "normalized/enrichment/claims/clm_x.json"]
    result = subprocess.run(["git", "check-ignore", *rels], cwd=str(ROOT), capture_output=True, text=True)
    assert result.returncode == 0
    assert set(result.stdout.split()) == set(rels)
