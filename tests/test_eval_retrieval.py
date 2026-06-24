"""Key-free coherence + scoring-helper tests for the retrieval relevance eval (ADR-0038).

This does NOT run the relevance eval itself (that needs the real embedder and is opt-in, never CI). It
guards the parts that must stay correct regardless: the pure scoring math, and that every golden case
references real corpus files — so a typo'd filename is caught in CI, not at eval time.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import eval_retrieval as er  # noqa: E402
from app.backend.policy import load_yaml  # noqa: E402

_CASES = (load_yaml((ROOT / "evals" / "golden_retrieval_relevance.yaml").read_text(encoding="utf-8"))
          or {}).get("cases") or []
_CORPUS = {p.name for p in (ROOT / "evals" / "corpus").glob("*.md")}
_CATEGORIES = {"exact_anchor", "conceptual", "multi_source", "disambiguation"}


def test_evidence_sources_unique_in_order():
    res = {"evidence": [{"source_id": "src_a"}, {"source_id": "src_b"}, {"source_id": "src_a"}]}
    assert er.evidence_sources(res) == ["src_a", "src_b"]


def test_score_case_metrics():
    ranked = ["src_x", "src_rel", "src_y"]
    s = er.score_case(ranked, relevant={"src_rel"}, irrelevant={"src_y"}, ks=[1, 3])
    assert s["first_rank"] == 2 and abs(s["rr"] - 0.5) < 1e-9
    assert s["recall@1"] == 0.0 and s["hit@1"] == 0.0      # not in top-1
    assert s["recall@3"] == 1.0 and s["hit@3"] == 1.0      # in top-3
    assert s["neg@1"] == 0.0 and s["neg@3"] == 1.0          # the irrelevant is in top-3


def test_score_case_no_relevant_found():
    s = er.score_case(["src_x", "src_y"], relevant={"src_z"}, irrelevant=set(), ks=[5])
    assert s["first_rank"] is None and s["rr"] == 0.0 and s["hit@5"] == 0.0


def test_corpus_and_golden_are_coherent():
    assert _CASES, "golden file has no cases"
    assert len(_CORPUS) >= 6, "corpus should have >= 6 docs (ADR-0038)"
    seen_categories = set()
    for case in _CASES:
        assert case.get("relevant"), f"{case.get('id')}: missing relevant"
        seen_categories.add(case.get("category"))
        for key in ("relevant", "irrelevant"):
            for fn in case.get(key) or []:
                assert fn in _CORPUS, f"{case.get('id')}: {key} references unknown corpus file {fn!r}"
        chunk = case.get("chunk")
        if chunk:
            assert chunk.get("source") in _CORPUS
    assert _CATEGORIES <= seen_categories, f"missing categories: {_CATEGORIES - seen_categories}"


class _FakeEmbedder:
    dimension = 8

    def embed(self, texts):
        return [[hashlib.sha256((t + str(i)).encode()).digest()[i] / 255.0 for i in range(8)]
                for t in texts]


def test_runner_plumbing_with_fake_embedder(tmp_path):
    # Key-free structural test of the runner path (build -> run -> score -> report), distinct from the
    # opt-in real-embedder relevance run. Asserts plumbing + report shape, NOT relevance numbers.
    pytest.importorskip("lancedb")
    from app.backend.config import get_settings
    settings = get_settings(ROOT)

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "alpha.md").write_text("# Alpha\n\nThe alpha report covers revenue and growth.\n",
                                     encoding="utf-8")
    (corpus / "beta.md").write_text("# Beta\n\nThe beta outlook covers guidance and fuel costs.\n",
                                    encoding="utf-8")
    cases = [
        {"id": "c_anchor", "category": "exact_anchor", "mode": "auto", "query": "revenue",
         "relevant": ["alpha.md"]},
        {"id": "c_disambig", "category": "disambiguation", "mode": "auto", "query": "guidance",
         "relevant": ["beta.md"], "irrelevant": ["alpha.md"]},
        {"id": "c_skip", "category": "conceptual", "mode": "auto", "query": "x",
         "relevant": ["nonexistent.md"]},
    ]

    work = tmp_path / "work"
    work.mkdir()
    emb = _FakeEmbedder()
    er._build_corpus_vault(corpus, work, emb, settings)
    fmap = er._filename_to_source(work / "raw" / "manifests")
    assert set(fmap) == {"alpha.md", "beta.md"}

    gconn, present, gtmp = er._open_graph(work, is_vault=False)
    assert present is False and gtmp is None        # corpus graph is empty, in-vault (temp work dir)
    try:
        result = er.run(work, settings, emb, cases, ks=[5], gconn=gconn, graph_present=present)
    finally:
        gconn.close()

    assert len(result["rows"]) == 2                  # 2 scored
    assert len(result["skipped"]) == 1               # the unresolved-filename case is skipped, not scored
    assert result["graph_present"] is False

    # The build path MUST generate Source wiki pages, else the retention filter drops every evidence hit.
    assert list((work / "wiki" / "Sources").glob("*.md")), "no Source pages generated"
    # Regression guard: with Source pages, the exact-anchor keyword case actually retrieves its source.
    by_id = {r["id"]: r for r in result["rows"]}
    anchor = by_id["c_anchor"]
    assert anchor["hit@5"] == 1.0 or anchor["recall@5"] > 0.0, "exact-anchor case scored zero"

    report = er.render_report(result, settings=settings, ks=[5], source_label="test")
    for needle in ("## Aggregate", "graph_present: false", "graph_boosts: none", "neg@5",
                   "## Discrimination", "relevant_wins", "negative cases: 1"):
        assert needle in report, needle
