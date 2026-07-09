from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph, keyword_index, search
from app.backend.policy import RetrievalPolicy


# --------------------------------------------------------------------------- safe FTS builder


def test_safe_fts_query_quotes_and_neutralizes_operators():
    # FTS5 operators in user input become quoted phrases, never operators. Terms lowercase; the
    # stopword "a" (from "M&A") is dropped by topic extraction.
    assert search.safe_fts_query('AI "M&A" NEAR()', max_chars=512, max_terms=32) == '"ai" "m" "near"'
    assert search.safe_fts_query('  ', max_chars=512, max_terms=32) is None
    assert search.safe_fts_query('integrate:the*plan', max_chars=512, max_terms=32) == '"integrate" "plan"'


def test_extract_terms_drops_stopwords_and_trigger_words():
    # The §8.2 trigger phrase is stripped, leaving the topic.
    assert search.extract_terms("what do I know about synergy", max_chars=512, max_terms=32) == ["synergy"]
    assert search.extract_terms("which sources disagree about returns", max_chars=512, max_terms=32) == ["returns"]
    assert search.extract_terms("how are mergers and synergies related", max_chars=512, max_terms=32) == ["mergers", "synergies"]
    # All-stopword query (no topic) yields no terms -> no FTS match.
    assert search.extract_terms("which sources disagree", max_chars=512, max_terms=32) == []
    assert search.safe_fts_query("which sources disagree", max_chars=512, max_terms=32) is None


def test_extract_terms_dedupes_and_bounds():
    assert search.extract_terms("synergy synergy alpha beta", max_chars=512, max_terms=2) == ["synergy", "alpha"]


def test_embedded_quote_is_doubled():
    assert search.safe_fts_query('say "hi"', max_chars=512, max_terms=8) == '"say" "hi"'


# --------------------------------------------------------------------------- classifier / router


@pytest.mark.parametrize("query,shape", [
    ("how are mergers and synergies related", "relationship"),
    ("which sources disagree about returns", "disagreement"),
    ("which documents mention BCG", "mention"),  # mention wins before exact (acronym)
    ("what do I know about carve-outs", "discovery"),
    ('find the exact phrase "day one readiness"', "exact"),
    ("report.pdf", "exact"),
    ("revenue grew 15%", "exact"),
    ("integration playbook strategy", "default"),
])
def test_classify_shape(query, shape):
    assert search.classify_shape(query) == shape


def test_route_auto_uses_policy_and_drops_vector():
    policy = RetrievalPolicy(
        router_rules={"exact": ["keyword", "vector"]}, default_mode_set=["keyword"], caps={}
    )
    modes, shape = search.route("auto", 'the "exact" 2026', policy)
    assert shape == "exact"
    assert modes == ["keyword"]  # vector dropped (Phase 4d)


def test_route_explicit_mode_forces_single_channel():
    modes, shape = search.route("graph", "anything", RetrievalPolicy())
    assert modes == ["graph"] and shape is None


# --------------------------------------------------------------------------- integration fixture


def _chunk(sid, ordinal, text, start):
    return {
        "chunk_id": f"{sid}::{ordinal:04d}", "source_id": sid, "ordinal": ordinal, "kind": "prose",
        "heading_path": [], "section": None, "text": text, "char_start": start,
        "char_end": start + len(text), "page": 1, "page_end": 1,
        "table_reference": None, "sheet_reference": None,
    }


def _write_chunks(root, sid, chunks):
    p = root / "normalized" / "chunks" / f"{sid}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(c) for c in chunks) + "\n", encoding="utf-8")


def _write_page(root, rel, fm, summary):
    lines = "\n".join(f"{k}: {json.dumps(v) if isinstance(v, list) else v}" for k, v in fm.items())
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{lines}\n---\n\n# {fm.get('title','P')}\n\n> [!summary]\n> {summary}\n", encoding="utf-8")


SRC_OK = "src_aaaaaaaaaaaaaaaa"
SRC_ARCH = "src_bbbbbbbbbbbbbbbb"
ITM = "itm_synergyxxxxxxxx"


@pytest.fixture
def vault(tmp_path):
    # Evidence: an active source and an archived source, both mentioning "synergy".
    _write_chunks(tmp_path, SRC_OK, [_chunk(SRC_OK, 0, "Synergy capture drives post-merger value.", 0)])
    _write_chunks(tmp_path, SRC_ARCH, [_chunk(SRC_ARCH, 0, "Old synergy estimates from a retired deck.", 0)])
    _write_page(tmp_path, f"wiki/Sources/{SRC_OK}.md",
                {"type": "source", "source_id": SRC_OK, "title": "Active deck", "status": "active",
                 "language": "en", "aliases": [], "tags": []}, "synergy capture in M&A")
    _write_page(tmp_path, f"wiki/Sources/{SRC_ARCH}.md",
                {"type": "source", "source_id": SRC_ARCH, "title": "Archived deck", "status": "archived",
                 "language": "en", "aliases": [], "tags": []}, "old synergy notes")
    # Navigation + graph seed: an active item page matching "synergy".
    _write_page(tmp_path, f"wiki/Items/{ITM}.md",
                {"type": "item", "item_id": ITM, "item_type": "method_technique",
                 "title": "Synergy capture", "status": "active",
                 "review_status": "none", "aliases": ["synergies"], "tags": []},
                "How synergy is captured after a merger.")
    keyword_index.reindex(tmp_path, force=True)
    kconn = keyword_index.connect(tmp_path / keyword_index.DB_RELPATH)

    # Graph: source mentions the item (active edge).
    gdb = tmp_path / "db" / "graph.sqlite"
    graph.init_db(gdb)
    gconn = graph.connect(gdb)
    graph.reindex_nodes(gconn, source_ids=[SRC_OK, SRC_ARCH],
                        page_nodes=[{"node_id": ITM, "node_type": "item",
                                     "item_type": "method_technique", "slug": "synergy",
                                     "status": "active"}],
                        now="t0")
    graph.upsert_assertion(gconn, src_id=SRC_OK, dst_id=ITM, edge_type="mentions",
                           asserted_by="llm", status="active")
    return kconn, gconn


def _run(vault, q, mode="auto", **kw):
    kconn, gconn = vault
    return search.run_search(q=q, mode=mode, keyword_conn=kconn, graph_conn=gconn,
                             policy=RetrievalPolicy(), **kw)


def test_evidence_retention_excludes_archived_source_by_default(vault):
    res = _run(vault, "synergy", mode="keyword")
    sids = {h["source_id"] for h in res["evidence"]}
    assert SRC_OK in sids
    assert SRC_ARCH not in sids  # archived source filtered by default
    # Explicitly including archived surfaces it.
    res2 = _run(vault, "synergy", mode="keyword", source_statuses=("active", "archived"))
    assert SRC_ARCH in {h["source_id"] for h in res2["evidence"]}


def test_evidence_hit_carries_citation_and_path(vault):
    res = _run(vault, "synergy", mode="keyword")
    hit = next(h for h in res["evidence"] if h["source_id"] == SRC_OK)
    assert hit["char_start"] == 0 and hit["char_end"] == len("Synergy capture drives post-merger value.")
    assert hit["retrieval_path"] == ["keyword"]
    assert hit["source_status"] == "active"
    assert res["retrieval_path"] == ["keyword"]


def test_navigation_mode_returns_status_aware_pages(vault):
    res = _run(vault, "synergy", mode="navigation")
    paths = {h["path"]: h for h in res["navigation"]}
    item = paths[f"wiki/Items/{ITM}.md"]
    assert item["answer_eligible"] is True
    assert item["page_type"] == "item"


def test_graph_mode_is_flat_subgraph_seeded_from_navigation(vault):
    res = _run(vault, "synergy", mode="graph")
    assert res["retrieval_path"] == ["graph"]
    g = res["graph"]
    assert ITM in g["seeds"]  # the item page seeds the graph
    ids = {n["node_id"] for n in g["nodes"]}
    assert {ITM, SRC_OK} <= ids  # BFS reached the mentioning source
    assert any(e["edge_type"] == "mentions" and e["src_id"] == SRC_OK and e["dst_id"] == ITM
               for e in g["edges"])
    # Flat edges are canonical-only — no other_node_id (ADR-0032 addendum 1).
    assert all("other_node_id" not in e for e in g["edges"])


def test_empty_query_is_structural_no_results(vault):
    res = _run(vault, "   ", mode="auto")
    assert res["no_results"] is True
    assert res["evidence"] == [] and res["counts"]["evidence"] == 0


def test_auto_routes_exact_shape_to_keyword(vault):
    res = _run(vault, '"synergy"', mode="auto")
    assert res["shape"] == "exact"
    assert res["retrieval_path"] == ["keyword"]


def test_no_graph_conn_yields_empty_graph_group(vault):
    kconn, _ = vault
    res = search.run_search(q="synergy", mode="graph", keyword_conn=kconn, graph_conn=None,
                            policy=RetrievalPolicy())
    assert res["graph"]["nodes"] == [] and res["no_results"] is True


# --- mode=vector channel (4d): standalone evidence, retention-filtered, deterministic ---


def _vector_rows():
    # LanceDB-like rows: full citation fields + _distance. SRC_OK is active, SRC_ARCH archived.
    def row(sid, dist, text):
        return {"_distance": dist, "source_id": sid, "chunk_id": f"{sid}::0000", "ordinal": 0,
                "kind": "prose", "section": None, "heading_path": json.dumps([]), "char_start": 0,
                "char_end": len(text), "page": 1, "page_end": 1, "table_reference": None,
                "sheet_reference": None, "text": text}
    return [row(SRC_OK, 0.10, "synergy capture drives value"), row(SRC_ARCH, 0.20, "old synergy")]


def _vrun(vault, *, source_statuses=search.RETENTION_DEFAULT_STATUSES, rows=None, searcher=None):
    kconn, _ = vault
    fn = searcher if searcher is not None else (lambda *, limit: rows if rows is not None else _vector_rows())
    return search.run_search(q="synergy", mode="vector", keyword_conn=kconn, graph_conn=None,
                             policy=RetrievalPolicy(), source_statuses=source_statuses, vector_search=fn)


def test_vector_mode_returns_standalone_evidence(vault):
    res = _vrun(vault)
    assert res["retrieval_path"] == ["vector"]
    hit = res["evidence"][0]
    assert hit["retrieval_path"] == ["vector"]
    assert hit["source_id"] == SRC_OK and hit["source_status"] == "active"
    # Identical EvidenceHit shape (kind, anchors, snippet).
    assert hit["kind"] == "prose" and hit["char_start"] == 0 and hit["snippet"]
    # Single-channel hit still carries `channels` (native distance) + an RRF top-level score.
    assert hit["channels"]["vector"] == {"rank": 1, "score": 0.10}
    assert hit["score"] == pytest.approx(1.0 / (60 + 1))


def test_vector_mode_retention_excludes_archived(vault):
    sids = {h["source_id"] for h in _vrun(vault)["evidence"]}
    assert SRC_OK in sids and SRC_ARCH not in sids  # archived excluded by default
    sids2 = {h["source_id"] for h in _vrun(vault, source_statuses=("active", "archived"))["evidence"]}
    assert SRC_ARCH in sids2


def test_vector_mode_closest_first_order(vault):
    rows = list(reversed(_vector_rows()))  # hand it unsorted (farthest first)
    ev = _vrun(vault, source_statuses=("active", "archived"), rows=rows)["evidence"]
    dists = [h["channels"]["vector"]["score"] for h in ev]
    assert dists == sorted(dists)  # closest (smallest distance) first
    assert [h["channels"]["vector"]["rank"] for h in ev] == [1, 2]  # rank reflects channel order


def test_vector_mode_no_searcher_is_empty(vault):
    res = search.run_search(q="synergy", mode="vector", keyword_conn=vault[0], graph_conn=None,
                            policy=RetrievalPolicy(), vector_search=None)
    assert res["no_results"] is True and res["evidence"] == []


def test_vector_mode_honors_source_id_filter(vault):
    res = search.run_search(
        q="synergy", mode="vector", keyword_conn=vault[0], graph_conn=None, policy=RetrievalPolicy(),
        source_id=SRC_OK, source_statuses=("active", "archived"),
        vector_search=lambda *, limit: _vector_rows(),
    )
    assert {h["source_id"] for h in res["evidence"]} == {SRC_OK}  # SRC_ARCH excluded by source_id


def test_vector_mode_equal_distance_deterministic_by_anchor(vault):
    # Distinct chunks (different char ranges) with equal distance -> ordered by (ordinal, char_start).
    def row(ordinal, start):
        return {"_distance": 0.1, "source_id": SRC_OK, "chunk_id": f"{SRC_OK}::{ordinal:04d}",
                "ordinal": ordinal, "kind": "prose", "section": None, "heading_path": json.dumps([]),
                "char_start": start, "char_end": start + 5, "page": 1, "page_end": 1,
                "table_reference": None, "sheet_reference": None, "text": "t"}
    res = search.run_search(q="x", mode="vector", keyword_conn=vault[0], graph_conn=None,
                            policy=RetrievalPolicy(), vector_search=lambda *, limit: [row(2, 40), row(0, 0)])
    assert [h["ordinal"] for h in res["evidence"]] == [0, 2]


# --- RRF fusion (the fuser directly; the auto blend wiring is 4e-2) ---


def test_rrf_fuses_both_channels_dedup_by_citation():
    def kw(sid, start, bm25):
        return {"source_id": sid, "chunk_id": f"{sid}::x", "ordinal": 0, "kind": "prose",
                "section": None, "heading_path": "[]", "char_start": start, "char_end": start + 5,
                "page": 1, "page_end": 1, "table_reference": None, "sheet_reference": None,
                "source_status": "active", "snippet": "s", "score": bm25, "retrieval_path": ["keyword"]}
    def vec(sid, start, dist):
        h = kw(sid, start, dist)
        h["retrieval_path"] = ["vector"]
        return h
    # SRC_A@0 is found by both channels (same citation key) -> merges; SRC_B@0 keyword-only.
    fused = search.fuse_evidence(
        {"keyword": [kw("src_a", 0, -4.0), kw("src_b", 0, -3.0)], "vector": [vec("src_a", 0, 0.2)]},
        k=60, limit=10,
    )
    by_src = {h["source_id"]: h for h in fused}
    a = by_src["src_a"]
    assert a["retrieval_path"] == ["keyword", "vector"]
    assert a["channels"] == {"keyword": {"rank": 1, "score": -4.0}, "vector": {"rank": 1, "score": 0.2}}
    assert a["score"] == pytest.approx(1 / 61 + 1 / 61)        # found at rank 1 in both
    assert by_src["src_b"]["channels"] == {"keyword": {"rank": 2, "score": -3.0}}
    assert a["score"] > by_src["src_b"]["score"]                # dual-channel hit ranks above
    assert [h["source_id"] for h in fused] == ["src_a", "src_b"]


def test_rrf_is_deterministic():
    ch = {"keyword": [_ehit("src_a", 0, -1.0)]}
    assert search.fuse_evidence(ch, k=60, limit=5) == search.fuse_evidence(ch, k=60, limit=5)


def _ehit(sid, start, score, *, channel="keyword", snippet="s", chunk_id="c", ordinal=0):
    return {"source_id": sid, "chunk_id": chunk_id, "ordinal": ordinal, "kind": "prose",
            "section": None, "heading_path": "[]", "char_start": start, "char_end": start + 5,
            "page": 1, "page_end": 1, "table_reference": None, "sheet_reference": None,
            "source_status": "active", "snippet": snippet, "score": score, "retrieval_path": [channel]}


def test_rrf_dedups_same_channel_best_rank():
    # The same citation key appears twice in ONE channel (corrupt index) -> counted once, best rank.
    fused = search.fuse_evidence({"keyword": [_ehit("src_a", 0, -4.0), _ehit("src_a", 0, -3.0)]},
                                 k=60, limit=10)
    assert len(fused) == 1
    assert fused[0]["channels"]["keyword"] == {"rank": 1, "score": -4.0}     # first/best rank kept
    assert fused[0]["score"] == pytest.approx(1 / 61)                         # counted once, not twice


def test_rrf_field_precedence_keyword_wins(vault):
    kw = _ehit("src_a", 0, -4.0, snippet="KEYWORD snippet", chunk_id="kw_chunk")
    vec = _ehit("src_a", 0, 0.2, channel="vector", snippet="vector text", chunk_id="vec_chunk")
    fused = search.fuse_evidence({"keyword": [kw], "vector": [vec]}, k=60, limit=5)
    h = fused[0]
    assert h["snippet"] == "KEYWORD snippet" and h["chunk_id"] == "kw_chunk"  # keyword display wins
    assert h["channels"]["keyword"]["score"] == -4.0 and h["channels"]["vector"]["score"] == 0.2


def test_rrf_limit_truncates_after_ordering():
    hits = [_ehit("src_a", 0, -1.0), _ehit("src_b", 0, -2.0), _ehit("src_c", 0, -3.0)]
    fused = search.fuse_evidence({"keyword": hits}, k=60, limit=2)
    assert [h["source_id"] for h in fused] == ["src_a", "src_b"]              # top-2 by RRF rank


def test_rrf_k_clamped_never_divides_by_zero():
    h = _ehit("src_a", 0, -1.0)
    assert search.fuse_evidence({"keyword": [h]}, k=0, limit=5)               # no ZeroDivisionError
    assert search.fuse_evidence({"keyword": [h]}, k=-5, limit=5)


# --- 4e-2: mode=auto conceptual-default + escalation blend + graceful degradation ---


def _auto(vault, q, *, vector_search=None, reason=None, policy=None):
    return search.run_search(q=q, mode="auto", keyword_conn=vault[0], graph_conn=None,
                             policy=policy or RetrievalPolicy(),
                             vector_search=vector_search, vector_unavailable_reason=reason)


def test_auto_default_shape_blends_vector(vault):
    res = _auto(vault, "integration playbook strategy", vector_search=lambda *, limit: _vector_rows())
    assert res["shape"] == "default"
    assert "vector" in res["retrieval_path"]  # conceptual default always blends vector


def test_auto_exact_shape_escalates_when_keyword_sparse(vault):
    calls = {"n": 0}

    def vs(*, limit):
        calls["n"] += 1
        return _vector_rows()
    res = _auto(vault, "revenue grew 999", vector_search=vs)  # exact (number), no keyword match -> sparse
    assert res["shape"] == "exact" and calls["n"] == 1
    assert "vector" in res["retrieval_path"]


def test_auto_exact_shape_skips_vector_when_not_sparse(vault):
    calls = {"n": 0}

    def vs(*, limit):
        calls["n"] += 1
        return _vector_rows()
    # escalation threshold 0 -> exact never escalates; vector is not consulted (lazy: never called).
    res = _auto(vault, 'find "synergy" 2026', vector_search=vs,
                policy=RetrievalPolicy(caps={"escalation_primary_below_k": 0}))
    assert res["shape"] == "exact" and calls["n"] == 0
    assert "vector" not in res["retrieval_path"]


def test_auto_graph_only_shape_defers_vector(vault):
    calls = {"n": 0}

    def vs(*, limit):
        calls["n"] += 1
        return _vector_rows()
    res = _auto(vault, "what do I know about synergy", vector_search=vs)  # discovery -> no keyword channel
    assert res["shape"] == "discovery" and calls["n"] == 0  # vector deferred (never embedded)
    assert "vector" not in res["retrieval_path"]


def test_auto_degrades_with_note_on_genuine_unavailable(vault):
    res = _auto(vault, "integration playbook strategy", vector_search=None, reason="embedder down")
    assert "vector" not in res["retrieval_path"]
    assert any("degraded to keyword-only" in n for n in res["notes"])


def test_auto_degrades_silently_when_not_note_worthy(vault):
    # reason=None means "keyword-only deployment" -> degrade quietly (no note).
    res = _auto(vault, "integration playbook strategy", vector_search=None, reason=None)
    assert res["notes"] == [] and "vector" not in res["retrieval_path"]


def test_explicit_vector_failure_raises_channel_error(vault):
    def boom(*, limit):
        raise search.VectorUnavailable("embed server down")  # what the real searcher raises
    with pytest.raises(search.VectorChannelError):
        search.run_search(q="x", mode="vector", keyword_conn=vault[0], graph_conn=None,
                          policy=RetrievalPolicy(), vector_search=boom)


def test_auto_vector_backend_failure_degrades_with_note(vault):
    def boom(*, limit):
        raise search.VectorUnavailable("embed server down")
    res = search.run_search(q="integration playbook strategy", mode="auto", keyword_conn=vault[0],
                            graph_conn=None, policy=RetrievalPolicy(), vector_search=boom)
    assert "vector" not in res["retrieval_path"]
    assert any("degraded to keyword-only" in n for n in res["notes"])


def test_auto_non_vector_exception_propagates(vault):
    # A non-VectorUnavailable error (e.g. a mapping/impl bug) is NOT swallowed as a fallback.
    def buggy(*, limit):
        raise KeyError("malformed row")
    with pytest.raises(KeyError):
        search.run_search(q="integration playbook strategy", mode="auto", keyword_conn=vault[0],
                          graph_conn=None, policy=RetrievalPolicy(), vector_search=buggy)


def test_auto_exact_no_escalation_with_sufficient_keyword_evidence(tmp_path):
    from app.backend import keyword_index
    sid = "src_alphaaaaaaaaaa"
    _write_chunks(tmp_path, sid, [_chunk(sid, i, f"alpha topic chunk {i}", i * 30) for i in range(3)])
    _write_page(tmp_path, f"wiki/Sources/{sid}.md",
                {"type": "source", "source_id": sid, "title": "A", "status": "active",
                 "language": "en", "aliases": [], "tags": []}, "alpha")
    keyword_index.reindex(tmp_path, force=True)
    kconn = keyword_index.connect(tmp_path / keyword_index.DB_RELPATH)
    calls = {"n": 0}

    def vs(*, limit):
        calls["n"] += 1
        return _vector_rows()
    res = search.run_search(q='"alpha"', mode="auto", keyword_conn=kconn, graph_conn=None,
                            policy=RetrievalPolicy(), vector_search=vs)  # exact, 3 hits, threshold 3
    assert res["shape"] == "exact" and res["counts"]["evidence"] == 3
    assert calls["n"] == 0 and "vector" not in res["retrieval_path"]  # >= threshold -> no escalation


# --- golden Build Spec §8.2 examples (topic extraction makes routed NL queries work) ---


def test_golden_discovery_finds_topic_not_question_words(vault):
    res = _run(vault, "what do I know about synergy", mode="auto")
    assert res["shape"] == "discovery"
    assert res["retrieval_path"] == ["navigation", "graph"]
    assert any(h["node_id"] == ITM for h in res["navigation"])  # topic 'synergy' matched the item
    assert ITM in res["graph"]["seeds"]


def test_golden_mention_routes_keyword_and_graph(vault):
    res = _run(vault, "which documents mention synergy", mode="auto")
    assert res["shape"] == "mention"
    assert res["retrieval_path"] == ["keyword", "graph"]
    assert res["counts"]["evidence"] >= 1  # keyword evidence on the topic


def test_golden_relationship_routes_to_graph(vault):
    res = _run(vault, "how are synergy and mergers related", mode="auto")
    assert res["shape"] == "relationship"
    assert res["retrieval_path"] == ["graph"]
    assert ITM in res["graph"]["seeds"]


# --- graph-native disagreement, retention, and depth (own builders) ---


def _graph(tmp_path, page_nodes, edges, source_ids=()):
    db = tmp_path / "db" / "graph.sqlite"
    graph.init_db(db)
    conn = graph.connect(db)
    graph.reindex_nodes(conn, source_ids=list(source_ids), page_nodes=page_nodes, now="t0")
    for src, dst, etype in edges:
        graph.upsert_assertion(conn, src_id=src, dst_id=dst, edge_type=etype,
                               asserted_by="llm", status="active")
    return conn


def _item_index(tmp_path, item_id, title, summary):
    _write_page(tmp_path, f"wiki/Items/{item_id}.md",
                {"type": "item", "item_id": item_id, "item_type": "method_technique",
                 "title": title, "status": "active",
                 "review_status": "none", "aliases": [], "tags": []}, summary)
    keyword_index.reindex(tmp_path, force=True)
    return keyword_index.connect(tmp_path / keyword_index.DB_RELPATH)


def test_disagreement_is_graph_native_without_topic(tmp_path):
    # "which sources disagree" has no topic terms, so nav seeds nothing — disagreement falls back to
    # the contradiction endpoints directly from the graph.
    c1, c2 = "clm_1111111111111111", "clm_2222222222222222"
    gconn = _graph(tmp_path, [
        {"node_id": c1, "node_type": "claim", "slug": None, "status": "active"},
        {"node_id": c2, "node_type": "claim", "slug": None, "status": "active"},
    ], [(c1, c2, "contradicts")])
    res = search.run_search(q="which sources disagree", mode="auto", keyword_conn=None,
                            graph_conn=gconn, policy=RetrievalPolicy())
    assert res["shape"] == "disagreement"
    assert res["retrieval_path"] == ["graph"]
    assert set(res["graph"]["seeds"]) == {c1, c2}
    assert any(e["edge_type"] == "contradicts" and (e["src_id"], e["dst_id"]) == (c1, c2)
               for e in res["graph"]["edges"])


def test_graph_retention_excludes_archived_and_deleted_adjacents(tmp_path):
    seed = "itm_anchorxxxxxxxxx"
    arch, dele, depr = "itm_archivedxxxxxx", "itm_deletedxxxxxxx", "itm_deprecatedxxx"
    kconn = _item_index(tmp_path, seed, "Anchor", "anchor topic")
    T = {"item_type": "method_technique"}
    gconn = _graph(tmp_path, [
        {"node_id": seed, "node_type": "item", "slug": "a", "status": "active", **T},
        {"node_id": arch, "node_type": "item", "slug": "b", "status": "archived", **T},
        {"node_id": dele, "node_type": "item", "slug": "c", "status": "deleted", **T},
        {"node_id": depr, "node_type": "item", "slug": "d", "status": "deprecated_candidate", **T},
    ], [(seed, arch, "related_to"), (seed, dele, "related_to"), (seed, depr, "related_to")])
    res = search.run_search(q="anchor", mode="graph", keyword_conn=kconn, graph_conn=gconn,
                            policy=RetrievalPolicy())
    ids = {n["node_id"] for n in res["graph"]["nodes"]}
    assert seed in ids and depr in ids       # active + deprecated_candidate kept
    assert arch not in ids and dele not in ids  # archived + deleted excluded by default
    # Explicitly requesting archived surfaces it.
    res2 = search.run_search(q="anchor", mode="graph", keyword_conn=kconn, graph_conn=gconn,
                             policy=RetrievalPolicy(), node_statuses=("active", "archived"))
    assert arch in {n["node_id"] for n in res2["graph"]["nodes"]}


def test_search_depth_budget_controls_traversal(tmp_path):
    a, b, d = "itm_aaaaaaaaaaaaaaa", "itm_bbbbbbbbbbbbbbb", "itm_ddddddddddddddd"
    kconn = _item_index(tmp_path, a, "Alpha", "alpha topic")
    T = {"item_type": "method_technique"}
    gconn = _graph(tmp_path, [
        {"node_id": a, "node_type": "item", "slug": "a", "status": "active", **T},
        {"node_id": b, "node_type": "item", "slug": "b", "status": "active", **T},
        {"node_id": d, "node_type": "item", "slug": "d", "status": "active", **T},
    ], [(a, b, "related_to"), (b, d, "related_to")])

    def run(depth):
        pol = RetrievalPolicy(caps={"max_graph_depth_default": depth})
        return search.run_search(q="alpha", mode="graph", keyword_conn=kconn, graph_conn=gconn,
                                 policy=pol)

    ids1 = {n["node_id"] for n in run(1)["graph"]["nodes"]}
    assert d not in ids1 and b in ids1            # depth 1 stops before the far node
    ids2 = {n["node_id"] for n in run(2)["graph"]["nodes"]}
    assert d in ids2                              # depth 2 reaches it
    assert run(2)["graph"]["depth"] == 2
