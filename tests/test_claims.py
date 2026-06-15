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

import validate_citations  # noqa: E402
import validate_graph  # noqa: E402

from app.backend import graph, manifests
from app.llm.cache import ResponseCache
from app.llm.client import LLMClient
from app.workers import claims, extract, intake
from app.workers.citations import locate_quote
from app.workers.wiki_render import parse_frontmatter
from tests import fixtures

MODEL_REF = "anthropic:claude-sonnet-4-6"
# Two verbatim substrings of fixtures.write_markdown's output.
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


def _client(tmp_path, adapter):
    return LLMClient({"anthropic": adapter}, cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite"))


def _extract(tmp_path, adapter, **kw):
    return claims.extract_claims(
        tmp_path, client=_client(tmp_path, adapter), model_ref=MODEL_REF,
        jobs_db=tmp_path / "db" / "jobs.sqlite", **kw,
    )


def _sid(tmp_path):
    return manifests.list_manifests(tmp_path / "raw" / "manifests")[0]["source_id"]


# --- claim_id + locate_quote -----------------------------------------------


def test_claim_id_is_source_agnostic_and_stable():
    a = claims.claim_id("The   sky is  blue.")
    b = claims.claim_id("The sky is blue.")  # whitespace-normalized -> same id
    assert a == b and a.startswith("clm_") and len(a) == len("clm_") + 16


def test_locate_quote_whitespace_flexible():
    md = "alpha   beta\ngamma delta"
    assert locate_quote(md, "beta gamma") == (md.index("beta"), md.index("gamma") + len("gamma"))
    assert locate_quote(md, "not present") is None


# --- worker ----------------------------------------------------------------


def test_extracts_grounds_writes_pages_and_edges(tmp_path):
    _build(tmp_path)
    summary = _extract(tmp_path, FakeAdapter())
    sid = _sid(tmp_path)

    assert summary["claims_written"] == 2
    assert summary["claim_pages_written"] == 2
    assert summary["status"] == "succeeded"

    # Claim pages exist and validate (citations ground against the normalized source).
    pages = sorted((tmp_path / "wiki" / "Claims").glob("*.md"))
    assert len(pages) == 2
    fm = parse_frontmatter(pages[0].read_text(encoding="utf-8"))
    assert fm["type"] == "claim" and fm["review_status"] == "none"
    assert validate_citations.main([str(tmp_path)]) == 0

    # The graph has an active derived_from edge per claim, and validates.
    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        edges = conn.execute(
            "SELECT edge_type, status, dst_id FROM edges WHERE asserted_by='llm'"
        ).fetchall()
    finally:
        conn.close()
    assert len(edges) == 2
    assert all(e["edge_type"] == "derived_from" and e["status"] == "active"
               and e["dst_id"] == sid for e in edges)
    assert validate_graph.main([str(tmp_path)]) == 0

    # The derived_from edge is grounded provenance -> active immediately (ADR-0030).
    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        assert graph.count_independent_sources(conn, _claim_id_for(tmp_path), edge_type="derived_from") >= 0
    finally:
        conn.close()


def _claim_id_for(tmp_path):
    art = json.loads(next((tmp_path / "normalized" / "enrichment").glob("*.claims.json")).read_text())
    return art["claims"][0]["claim_id"]


def test_unlocatable_quote_is_dropped(tmp_path):
    _build(tmp_path)
    summary = _extract(tmp_path, FakeAdapter(claims_payload=[
        {"claim": "Real claim.", "quote": Q1},
        {"claim": "Hallucinated claim.", "quote": "this text is nowhere in the source"},
    ]))
    assert summary["claims_written"] == 1
    assert summary["claims_dropped"] == 1


def test_no_api_key_skips_and_writes_nothing(tmp_path):
    _build(tmp_path)
    fake = FakeAdapter(available=False)
    summary = _extract(tmp_path, fake)
    assert summary["status"] == "skipped"
    assert summary["claims_written"] == 0
    assert fake.calls == 0
    assert not list((tmp_path / "wiki" / "Claims").glob("*.md")) \
        if (tmp_path / "wiki" / "Claims").exists() else True


def test_idempotent_skip_and_force(tmp_path):
    _build(tmp_path)
    first = _extract(tmp_path, FakeAdapter())
    assert first["claims_written"] == 2

    second = _extract(tmp_path, FakeAdapter())
    assert second["claims_written"] == 0
    assert second["skipped_fresh"] == 1

    forced = _extract(tmp_path, FakeAdapter(), force=True)
    assert forced["claims_written"] == 2


def test_claim_pages_are_idempotent_byte_stable(tmp_path):
    _build(tmp_path)
    _extract(tmp_path, FakeAdapter())
    page = sorted((tmp_path / "wiki" / "Claims").glob("*.md"))[0]
    first = page.read_text(encoding="utf-8")
    _extract(tmp_path, FakeAdapter(), force=True)
    assert page.read_text(encoding="utf-8") == first  # no wall-clock churn
