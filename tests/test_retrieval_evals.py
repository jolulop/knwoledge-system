"""Phase 4e-3 retrieval eval harness (ADR-0032 addendum 8).

Loads the golden cases from ``evals/golden_retrieval.yaml`` and runs each through ``run_search()``
against a small **programmatic** fixture vault (chunks + status-varied wiki pages + a graph with a
contradiction pair), with keyword + vector indexes built by the deterministic ``FakeEmbedder``.
Key-free and CI-gating: the assertions are structural (anchors, status-awareness, router taxonomy,
RRF shape/order determinism, retention) — not real semantic relevance, which a fake embedder can't
provide. Real-model determinism is pinned by index version + ``embedding_model_ref`` and is a
smoke/eval concern, not this gate.

LanceDB-gated (the vector index needs the ``vector`` extra); skipped on a bare install.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("lancedb")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph, keyword_index, search, vector_index  # noqa: E402
from app.backend.policy import RetrievalPolicy, load_yaml  # noqa: E402

EVALS_FILE = ROOT / "evals" / "golden_retrieval.yaml"

# Fixture ids (kept stable so the YAML cases can reference them).
SRC_A = "src_aaaaaaaaaaaaaaaa"          # active source
SRC_ARCH = "src_bbbbbbbbbbbbbbbb"       # archived source (retention)
CPT_ACT = "cpt_activexxxxxxxxx"         # active concept  -> answer_eligible true
CPT_DEP = "cpt_deprecatedxxxx"          # deprecated_candidate concept -> searchable but eligible false
CLM_1 = "clm_1111111111111111"
CLM_2 = "clm_2222222222222222"
CHUNK0 = "Synergy capture drives post-merger value."


class FakeEmbedder:
    dimension = 8

    def embed(self, texts):
        return [
            [hashlib.sha256(t.encode("utf-8")).digest()[i % 32] / 255.0 for i in range(8)]
            for t in texts
        ]


def _chunk(sid, ordinal, text, start, *, page=1):
    return {"chunk_id": f"{sid}::{ordinal:04d}", "source_id": sid, "ordinal": ordinal, "kind": "prose",
            "heading_path": ["Top"], "section": "Top", "text": text, "char_start": start,
            "char_end": start + len(text), "page": page, "page_end": page,
            "table_reference": None, "sheet_reference": None}


def _write_chunks(root, sid, chunks):
    p = root / "normalized" / "chunks" / f"{sid}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(c) for c in chunks) + "\n", encoding="utf-8")


def _write_page(root, rel, fm, summary):
    lines = "\n".join(f"{k}: {v}" for k, v in fm.items())
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{lines}\n---\n\n# {fm.get('title', 'P')}\n\n> [!summary]\n> {summary}\n", encoding="utf-8")


def _build_vault(root: Path):
    _write_chunks(root, SRC_A, [
        _chunk(SRC_A, 0, CHUNK0, 0),
        _chunk(SRC_A, 1, "Day one readiness and TSA exits.", len(CHUNK0) + 2),
    ])
    _write_chunks(root, SRC_ARCH, [_chunk(SRC_ARCH, 0, "Old synergy notes from a retired deck.", 0)])

    _write_page(root, f"wiki/Sources/{SRC_A}.md",
                {"type": "source", "source_id": SRC_A, "title": "Active deck", "status": "active",
                 "language": "en"}, "synergy capture in M&A")
    _write_page(root, f"wiki/Sources/{SRC_ARCH}.md",
                {"type": "source", "source_id": SRC_ARCH, "title": "Archived deck", "status": "archived",
                 "language": "en"}, "old synergy notes")
    _write_page(root, f"wiki/Concepts/{CPT_ACT}.md",
                {"type": "concept", "concept_id": CPT_ACT, "title": "Synergy capture", "status": "active",
                 "review_status": "none"}, "How synergy is captured after a merger.")
    _write_page(root, f"wiki/Concepts/{CPT_DEP}.md",
                {"type": "concept", "concept_id": CPT_DEP, "title": "Synergy deprecated",
                 "status": "deprecated_candidate", "review_status": "rejected"}, "A deprecated synergy concept.")

    keyword_index.reindex(root, force=True)
    kconn = keyword_index.connect(root / keyword_index.DB_RELPATH)

    gdb = root / "db" / "graph.sqlite"
    graph.init_db(gdb)
    gconn = graph.connect(gdb)
    graph.reindex_nodes(gconn, source_ids=[SRC_A, SRC_ARCH], page_nodes=[
        {"node_id": CPT_ACT, "node_type": "concept", "slug": "syn", "status": "active"},
        {"node_id": CPT_DEP, "node_type": "concept", "slug": "dep", "status": "deprecated_candidate"},
        {"node_id": CLM_1, "node_type": "claim", "slug": None, "status": "active"},
        {"node_id": CLM_2, "node_type": "claim", "slug": None, "status": "active"},
    ], now="t0")
    graph.upsert_assertion(gconn, src_id=SRC_A, dst_id=CPT_ACT, edge_type="mentions",
                           asserted_by="llm", status="active")
    graph.upsert_assertion(gconn, src_id=CLM_1, dst_id=CLM_2, edge_type="contradicts",
                           asserted_by="llm", status="active")

    vector_index.reindex(root, FakeEmbedder(), embedding_model_ref="fake", distance_metric="cosine",
                         force=True)
    return kconn, gconn, FakeEmbedder()


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    root = tmp_path_factory.mktemp("retrieval_evals")
    kconn, gconn, fake = _build_vault(root)
    return root, kconn, gconn, fake


def _run_case(harness, case):
    root, kconn, gconn, fake = harness
    q = case["query"]

    def vector_search(*, limit):
        return vector_index.search(root, fake.embed([q])[0], limit=limit, metric="cosine")

    return search.run_search(q=q, mode=case.get("mode", "auto"), keyword_conn=kconn, graph_conn=gconn,
                             policy=RetrievalPolicy(), vector_search=vector_search)


# Map each `expect` predicate key to an assertion over the /search result.
def _assert_expect(res, expect, harness, case):
    ev = res["evidence"]
    ev_sources = {h["source_id"] for h in ev}
    gnodes = {n["node_id"] for n in res["graph"]["nodes"]}

    if "shape" in expect:
        assert res["shape"] == expect["shape"]
    if "retrieval_path" in expect:
        assert res["retrieval_path"] == expect["retrieval_path"]
    if "no_results" in expect:
        assert res["no_results"] is expect["no_results"]
    if "notes_empty" in expect:
        assert (res["notes"] == []) is expect["notes_empty"]
    if "min_evidence" in expect:
        assert len(ev) >= expect["min_evidence"]
    if "top_source_id" in expect:
        assert ev[0]["source_id"] == expect["top_source_id"]
    if "top_char_start" in expect:
        assert ev[0]["char_start"] == expect["top_char_start"]
    if "top_channels" in expect:
        assert sorted(ev[0]["channels"]) == sorted(expect["top_channels"])
    if "top_retrieval_path" in expect:
        assert ev[0]["retrieval_path"] == expect["top_retrieval_path"]
    if "top_has_fields" in expect:
        assert all(f in ev[0] and ev[0][f] is not None for f in expect["top_has_fields"])
    if "evidence_excludes_sources" in expect:
        assert ev_sources.isdisjoint(expect["evidence_excludes_sources"])
    if "evidence_includes_sources" in expect:
        assert set(expect["evidence_includes_sources"]) <= ev_sources
    if "nav_eligible" in expect:
        by_path = {h["path"]: h for h in res["navigation"]}
        for rel, eligible in expect["nav_eligible"].items():
            assert by_path[rel]["answer_eligible"] is eligible
    if "graph_node_ids_include" in expect:
        assert set(expect["graph_node_ids_include"]) <= gnodes
    if "graph_max_distance" in expect:
        assert max((n["distance"] for n in res["graph"]["nodes"]), default=0) <= expect["graph_max_distance"]
    if "graph_truncated" in expect:
        assert res["graph"]["truncated"] is expect["graph_truncated"]
    if expect.get("deterministic"):
        assert _run_case(harness, case) == res  # identical query + index -> identical output


def _load_cases():
    return load_yaml(EVALS_FILE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_golden_retrieval(case, harness):
    res = _run_case(harness, case)
    _assert_expect(res, case.get("expect", {}), harness, case)


def test_eval_file_covers_all_eight_categories():
    cats = {c["category"] for c in _load_cases()}
    assert cats == {
        "exact_anchor", "status_nav", "graph_caps", "router_taxonomy",
        "fts_safe", "vector_carry", "rrf_determinism", "retention",
    }
