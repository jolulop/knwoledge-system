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


def _corpus_text(fn: str) -> str:
    return (ROOT / "evals" / "corpus" / fn).read_text(encoding="utf-8")


def _assert_chunk_case_coherent(case: dict) -> None:
    """ADR-0038 M5 coherence we CAN check key-free (no extraction): every `contains:` phrase is literally
    in its named corpus doc, and `chunk`/`near_miss` use distinct phrases. The stronger guarantee — each
    phrase resolves to EXACTLY ONE chunk with DISTINCT citation keys — needs extraction and is the
    runner's eval-time skip-or-report check, not a CI invariant."""
    cid = case.get("id")
    chunk, near_miss = case.get("chunk"), case.get("near_miss")
    assert near_miss, f"{cid}: chunk case must carry a near_miss distractor"
    for label, spec in (("chunk", chunk), ("near_miss", near_miss)):
        assert spec.get("source") in _CORPUS, f"{cid}: {label}.source unknown {spec.get('source')!r}"
        phrase = spec.get("contains")
        assert phrase, f"{cid}: {label} missing a contains phrase"
        assert phrase in _corpus_text(spec["source"]), \
            f"{cid}: {label} phrase {phrase!r} is not a substring of {spec['source']}"
    assert chunk["contains"] != near_miss["contains"], f"{cid}: chunk and near_miss share a phrase"


def test_corpus_and_golden_are_coherent():
    assert _CASES, "golden file has no cases"
    assert len(_CORPUS) >= 6, "corpus should have >= 6 docs (ADR-0038)"
    seen_categories = set()
    for case in _CASES:
        seen_categories.add(case.get("category"))
        if case.get("chunk"):  # chunk-level case (ADR-0038 M2): no `relevant:`; relevant source derived
            _assert_chunk_case_coherent(case)
            continue
        assert case.get("relevant"), f"{case.get('id')}: missing relevant"
        for key in ("relevant", "irrelevant"):
            for fn in case.get(key) or []:
                assert fn in _CORPUS, f"{case.get('id')}: {key} references unknown corpus file {fn!r}"
    # the four source-level categories must all be present; chunk_disambiguation is additive (M2)
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
        # Deterministic discrimination FAILURE: "revenue" is in alpha (the distractor), not beta — alpha is
        # found by BOTH channels (keyword+vector) and outranks the vector-only beta, so it must trip the
        # Channel Diagnostics section.
        {"id": "c_fail", "category": "disambiguation", "mode": "auto", "query": "revenue",
         "relevant": ["beta.md"], "irrelevant": ["alpha.md"]},
        {"id": "c_skip", "category": "conceptual", "mode": "auto", "query": "x",
         "relevant": ["nonexistent.md"]},
        # An unresolved IRRELEVANT filename must skip the whole case (never silently drop the distractor).
        {"id": "c_bad_irr", "category": "disambiguation", "mode": "auto", "query": "revenue",
         "relevant": ["alpha.md"], "irrelevant": ["ghost.md"]},
        # #3 regression: a PARTIALLY-unresolved relevant list (one good + one typo) must skip, not score
        # against the smaller {alpha} oracle (which would inflate recall/MRR).
        {"id": "c_partial_rel", "category": "multi_source", "mode": "auto", "query": "revenue",
         "relevant": ["alpha.md", "ghost2.md"]},
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

    assert len(result["rows"]) == 3                  # 3 scored
    assert result["chunk_rows"] == []                # no chunk cases here
    assert len(result["skipped"]) == 3               # unresolved relevant, unresolved irrelevant, partial
    assert any("c_bad_irr" in s and "ghost.md" in s for s in result["skipped"])  # not silently dropped
    assert any("c_partial_rel" in s and "ghost2.md" in s for s in result["skipped"])  # #3: partial skips
    assert result["graph_present"] is False

    # The build path MUST generate Source wiki pages, else the retention filter drops every evidence hit.
    assert list((work / "wiki" / "Sources").glob("*.md")), "no Source pages generated"
    by_id = {r["id"]: r for r in result["rows"]}
    # Regression guard: with Source pages, the exact-anchor keyword case actually retrieves its source.
    assert by_id["c_anchor"]["hit@5"] == 1.0 or by_id["c_anchor"]["recall@5"] > 0.0, "exact-anchor zero"
    # The failure carries a DEFINITE channel-diagnostic label (keyword fired -> never the catch-all),
    # and that label is what the report prints.
    fail = by_id["c_fail"]
    assert fail["discriminated"] == 0.0 and "diag" in fail
    assert fail["diag"]["label"] in set(er._CHANNEL_LABELS.values())

    report = er.render_report(result, settings=settings, ks=[5], source_label="test")
    for needle in ("## Aggregate", "graph_present: false", "graph_boosts: none", "neg@5",
                   "## Discrimination", "relevant_wins", "negative cases: 2", "## Channel Diagnostics",
                   fail["diag"]["label"]):
        assert needle in report, needle


def _ev(sid, kw=None, vec=None):
    ch = {}
    if kw is not None:
        ch["keyword"] = {"rank": kw, "score": 1.0}
    if vec is not None:
        ch["vector"] = {"rank": vec, "score": 1.0}
    return {"source_id": sid, "channels": ch}


def test_channel_diagnostics_labels():
    # keyword ranks relevant first, vector ranks irrelevant first -> fusion-balance (RRF might help)
    d = er.channel_diagnostics([_ev("rel", kw=1, vec=3), _ev("irr", kw=2, vec=1)], {"rel"}, {"irr"})
    assert (d["keyword_relevant_rank"], d["vector_irrelevant_rank"]) == (1, 1)
    assert d["label"] == "keyword_prefers_relevant_vector_prefers_irrelevant"
    # both channels rank the distractor first -> semantic ambiguity (RRF can't help)
    assert er.channel_diagnostics(
        [_ev("rel", kw=2, vec=2), _ev("irr", kw=1, vec=1)], {"rel"}, {"irr"}
    )["label"] == "both_prefer_irrelevant"
    # vector prefers relevant, keyword prefers irrelevant
    assert er.channel_diagnostics(
        [_ev("rel", kw=3, vec=1), _ev("irr", kw=1, vec=2)], {"rel"}, {"irr"}
    )["label"] == "vector_prefers_relevant_keyword_prefers_irrelevant"
    # neither channel surfaced the relevant or the distractor at all
    assert er.channel_diagnostics([_ev("other", kw=1, vec=1)], {"rel"}, {"irr"})["label"] \
        == "no_channel_signal"
    # both channels prefer the relevant (distractor absent from both)
    assert er.channel_diagnostics([_ev("rel", kw=1, vec=1)], {"rel"}, {"irr"})["label"] \
        == "both_prefer_relevant"


def test_channel_diagnostics_single_channel_failures():
    # THE common real failure: keyword silent, vector alone ranks the distractor first -> RRF can't help.
    assert er.channel_diagnostics(
        [_ev("rel", vec=3), _ev("irr", vec=1)], {"rel"}, {"irr"}
    )["label"] == "vector_prefers_irrelevant_keyword_silent"
    # mirror: keyword silent, vector ranks the relevant first (a single-channel success).
    assert er.channel_diagnostics(
        [_ev("rel", vec=1), _ev("irr", vec=3)], {"rel"}, {"irr"}
    )["label"] == "vector_prefers_relevant_keyword_silent"
    # vector silent, keyword prefers the distractor.
    assert er.channel_diagnostics(
        [_ev("rel", kw=2), _ev("irr", kw=1)], {"rel"}, {"irr"}
    )["label"] == "keyword_prefers_irrelevant_vector_silent"


# --- chunk-level helpers (ADR-0038 M1-M4) ----------------------------------------------------------

def test_citation_key_and_evidence_chunks_dedup():
    assert er._citation_key("src_a", 0, 10) == "src_a:0:10"
    res = {"evidence": [
        {"source_id": "src_a", "char_start": 0, "char_end": 10},
        {"source_id": "src_a", "char_start": 20, "char_end": 30},
        {"source_id": "src_a", "char_start": 0, "char_end": 10},   # duplicate citation key
    ]}
    assert er.evidence_chunks(res) == ["src_a:0:10", "src_a:20:30"]


def test_channel_diagnostics_chunk_granularity_keys_on_citation_key():
    # Intra-doc near-miss: relevant + distractor chunk share a SOURCE but differ by span. Source-level
    # keying conflates them; citation-key keying (M4) separates them so the label is meaningful.
    rel = {"source_id": "src_a", "char_start": 0, "char_end": 10,
           "channels": {"keyword": {"rank": 1}, "vector": {"rank": 3}}}
    nm = {"source_id": "src_a", "char_start": 20, "char_end": 30,
          "channels": {"keyword": {"rank": 2}, "vector": {"rank": 1}}}
    rel_key, nm_key = er._evidence_citation_key(rel), er._evidence_citation_key(nm)
    assert rel_key != nm_key
    d = er.channel_diagnostics([rel, nm], {rel_key}, {nm_key}, key_of=er._evidence_citation_key)
    # keyword ranks the relevant chunk first, vector ranks the distractor first -> fusion-balance signal
    assert d["label"] == "keyword_prefers_relevant_vector_prefers_irrelevant"
    # sanity: with the default SOURCE key both hits collapse to one source and can't be told apart
    d2 = er.channel_diagnostics([rel, nm], {"src_a"}, {"src_a"}, key_of=er._source_key)
    assert d2["keyword_relevant_rank"] == d2["keyword_irrelevant_rank"] == 1   # conflated


def test_keyword_problems_missing_index_is_read_only(tmp_path):
    # #1: a missing keyword index is REPORTED and NOT created (keyword_index.connect would otherwise
    # mkdir + create an empty SQLite file in the operator vault). _keyword_problems opens read-only.
    from app.backend import keyword_index
    assert er._keyword_problems(tmp_path) == ["no keyword index"]
    assert not (tmp_path / keyword_index.DB_RELPATH).exists()       # crucially, nothing was created


def test_keyword_problems_full_consistency_gate(tmp_path):
    # #2 / Q1: the --vault gate enforces the FULL "usable index" definition (schema + core tables +
    # fingerprint freshness), not just file existence — reusing keyword_index.consistency_errors so it
    # can't drift from validate_index_consistency. A stale or table-missing index would otherwise crash
    # search (`FROM evidence`) or score a stale ranking under a clean-looking report.
    pytest.importorskip("lancedb")
    import sqlite3

    from app.backend import keyword_index
    from app.backend.config import get_settings
    settings = get_settings(ROOT)

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "gamma.md").write_text("# Gamma\n\n## One\n\nApples and oranges in the morning shipment.\n",
                                     encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    er._build_corpus_vault(corpus, work, _FakeEmbedder(), settings)
    assert er._keyword_problems(work) == []                          # a freshly built index is usable

    # and a real keyword query runs fine under the READ-ONLY connector (the --vault run() path)
    gconn, present, _gtmp = er._open_graph(work, is_vault=False)
    try:
        ro = er.run(work, settings, _FakeEmbedder(),
                    [{"id": "ro_q", "category": "exact_anchor", "mode": "auto", "query": "apples",
                      "relevant": ["gamma.md"]}],
                    ks=[5], gconn=gconn, graph_present=present, keyword_readonly=True)
    finally:
        gconn.close()
    assert len(ro["rows"]) == 1                                      # keyword SELECT works read-only

    # fingerprint drift: a chunk file changed on disk since indexing -> stale -> rejected
    sid = next(iter(er._filename_to_source(work / "raw" / "manifests").values()))
    chunk_file = work / "normalized" / "chunks" / f"{sid}.jsonl"
    chunk_file.write_text(chunk_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert any("stale" in p for p in er._keyword_problems(work))

    # missing core tables: search would crash on `FROM evidence` and the retention filter needs
    # `navigation` (source status) -> both must be rejected, not scored.
    db = sqlite3.connect(work / keyword_index.DB_RELPATH)
    db.execute("DROP TABLE evidence")
    db.execute("DROP TABLE navigation")
    db.commit()
    db.close()
    problems = er._keyword_problems(work)
    assert any("core table" in p and "evidence" in p and "navigation" in p for p in problems)


def test_runner_chunk_plumbing_with_fake_embedder(tmp_path):
    # Key-free structural test of the CHUNK path (ADR-0038 M3/M4): a tiny two-##-section doc exercises
    # chunk scoring, the three chunk report blocks, and the skip paths (no-match + same-chunk). Asserts
    # plumbing + report shape, NOT relevance numbers.
    pytest.importorskip("lancedb")
    from app.backend.config import get_settings
    settings = get_settings(ROOT)

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "gamma.md").write_text(
        "# Gamma\n\n## Section one\n\nThe gamma section one paragraph mentions apples and oranges in "
        "the morning shipment.\n\n## Section two\n\nThe gamma section two paragraph mentions bananas and "
        "grapes in the evening shipment.\n", encoding="utf-8")
    cases = [
        {"id": "ck_ok", "category": "chunk_disambiguation", "mode": "auto", "query": "apples oranges",
         "chunk": {"source": "gamma.md", "contains": "apples and oranges"},
         "near_miss": {"source": "gamma.md", "contains": "bananas and grapes"}},
        # Deterministic chunk discrimination FAILURE: the query matches the NEAR_MISS section by keyword
        # (section two), not the relevant section one, so the distractor chunk outranks the relevant one
        # -> discriminated == 0 -> the failed-CHUNK-diagnostic block (M4's decisive section) must render.
        {"id": "ck_fail", "category": "chunk_disambiguation", "mode": "auto", "query": "bananas grapes",
         "chunk": {"source": "gamma.md", "contains": "apples and oranges"},
         "near_miss": {"source": "gamma.md", "contains": "bananas and grapes"}},
        # phrase matches no chunk -> curation error -> skip + report (not silently scored)
        {"id": "ck_skip_nomatch", "category": "chunk_disambiguation", "mode": "auto", "query": "x",
         "chunk": {"source": "gamma.md", "contains": "no such phrase anywhere in the doc"},
         "near_miss": {"source": "gamma.md", "contains": "bananas and grapes"}},
        # chunk and near_miss resolve to the SAME chunk -> skip + report
        {"id": "ck_skip_same", "category": "chunk_disambiguation", "mode": "auto", "query": "x",
         "chunk": {"source": "gamma.md", "contains": "apples and oranges"},
         "near_miss": {"source": "gamma.md", "contains": "apples and oranges"}},
    ]

    work = tmp_path / "work"
    work.mkdir()
    emb = _FakeEmbedder()
    er._build_corpus_vault(corpus, work, emb, settings)
    gconn, present, _gtmp = er._open_graph(work, is_vault=False)
    try:
        result = er.run(work, settings, emb, cases, ks=[5], gconn=gconn, graph_present=present)
    finally:
        gconn.close()

    assert result["rows"] == []                       # no source-level cases -> source headline empty
    assert len(result["chunk_rows"]) == 2             # ck_ok + ck_fail scored
    assert {s.split()[0] for s in result["skipped"]} == {"ck_skip_nomatch", "ck_skip_same"}
    by_id = {r["id"]: r for r in result["chunk_rows"]}
    for key in ("first_rank", "first_irrel_rank", "discriminated", "diag", "source_found", "source_rank"):
        assert key in by_id["ck_ok"], key
    # the deterministic failure trips the discrimination + carries a definite channel label
    fail = by_id["ck_fail"]
    assert fail["discriminated"] == 0.0
    assert fail["diag"]["label"] in set(er._CHANNEL_LABELS.values())

    report = er.render_report(result, settings=settings, ks=[5], source_label="chunktest")
    for needle in ("## Chunk-Level Aggregate", "chunk_MRR", "## Chunk Source Continuity",
                   "first_near_miss_rank", "cases scored: 0 source-level + 2 chunk-level",
                   "## Channel Diagnostics (failed CHUNK disambiguation", "ck_fail", fail["diag"]["label"]):
        assert needle in report, needle


def test_chunk_fixtures_isolated_from_source_level_cases():
    # ADR-0038 M6: the net-new multi-chunk fixtures must not appear as relevant/irrelevant in ANY
    # source-level case, so the source-level baseline isn't entangled with the chunk corpus.
    chunk_sources = set()
    for case in _CASES:
        chunk = case.get("chunk")
        if chunk:
            chunk_sources.add(chunk.get("source"))
            chunk_sources.add((case.get("near_miss") or {}).get("source"))
    chunk_sources.discard(None)
    assert chunk_sources, "expected chunk fixtures referenced by chunk cases"
    for case in _CASES:
        if case.get("chunk"):
            continue
        referenced = set(case.get("relevant") or []) | set(case.get("irrelevant") or [])
        leaked = referenced & chunk_sources
        assert not leaked, f"{case.get('id')}: source-level case references chunk fixture(s) {leaked}"


def test_chunk_cases_schema_coherent():
    # ADR-0038 M2: a case with `chunk:` is chunk-level -> must be category chunk_disambiguation, must
    # carry `near_miss:`, and must NOT also carry a source-level `relevant:` list.
    chunk_cases = [c for c in _CASES if c.get("chunk")]
    assert chunk_cases, "expected chunk_disambiguation cases in the golden file"
    for case in chunk_cases:
        cid = case.get("id")
        assert case.get("category") == "chunk_disambiguation", f"{cid}: chunk case wrong category"
        assert case.get("near_miss"), f"{cid}: chunk case missing near_miss"
        assert not case.get("relevant"), f"{cid}: chunk case must not also carry a relevant list"


def test_resolve_chunk_rejects_ambiguous_phrase(tmp_path):
    # M1/M5: a phrase that appears in MORE THAN ONE chunk is a curation error -> _resolve_chunk reports
    # it (runner then skips the case), never silently picks one. Covers the >1-match path (the no-match
    # and same-key paths are covered by the chunk plumbing test's skip set).
    pytest.importorskip("lancedb")
    from app.backend.config import get_settings
    settings = get_settings(ROOT)

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "dup.md").write_text(  # "the shared phrase here" appears in BOTH ## sections -> 2 chunks
        "# Dup\n\n## A\n\nAlpha body with the shared phrase here once.\n\n"
        "## B\n\nBeta body with the shared phrase here again.\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    er._build_corpus_vault(corpus, work, _FakeEmbedder(), settings)
    fmap = er._filename_to_source(work / "raw" / "manifests")
    key, sid, err = er._resolve_chunk(
        {"source": "dup.md", "contains": "the shared phrase here"}, fmap, work)
    assert key is None and sid is not None
    assert "matched 2 chunks" in err
