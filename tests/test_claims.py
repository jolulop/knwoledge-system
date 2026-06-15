from __future__ import annotations

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

    assert validate_citations.main([str(tmp_path)]) == 0
    assert validate_graph.main([str(tmp_path)]) == 0
    assert validate_wikilinks.main([str(tmp_path)]) == 0


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
    assert summary["claims_written"] == 1 and summary["claims_dropped"] == 1


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


def test_reextraction_supersedes_stale_edges_and_removes_orphan_pages(tmp_path):
    _build(tmp_path)
    _extract(tmp_path, FakeAdapter(claims_payload=[{"claim": "Old claim about paragraphs.", "quote": Q1}]))
    old_id = claims.claim_id("Old claim about paragraphs.")
    assert (tmp_path / "wiki" / "Claims" / f"{old_id}.md").exists()

    sid = _sids(tmp_path)["doc.md"]
    # Change the normalized text so the old quote is gone and a new one is present.
    (tmp_path / "normalized" / "markdown" / f"{sid}.md").write_text(
        "# T\n\nA brand new sentence about widgets.\n", encoding="utf-8")
    summary = _extract(tmp_path, FakeAdapter(claims_payload=[
        {"claim": "New claim about widgets.", "quote": "A brand new sentence about widgets."}]))

    new_id = claims.claim_id("New claim about widgets.")
    assert summary["claim_pages_deleted"] >= 1
    assert not (tmp_path / "wiki" / "Claims" / f"{old_id}.md").exists()  # orphan removed
    assert (tmp_path / "wiki" / "Claims" / f"{new_id}.md").exists()

    # The old edge is superseded (audit-preserving), the new one active.
    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        statuses = {r["src_id"]: r["status"] for r in conn.execute("SELECT src_id, status FROM edges")}
    finally:
        conn.close()
    assert statuses[old_id] == "superseded" and statuses[new_id] == "active"


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
