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
CPT = "cpt_synergyxxxxxxxx"


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
    # Navigation + graph seed: an active concept page matching "synergy".
    _write_page(tmp_path, f"wiki/Concepts/{CPT}.md",
                {"type": "concept", "concept_id": CPT, "title": "Synergy capture", "status": "active",
                 "review_status": "none", "aliases": ["synergies"], "tags": []},
                "How synergy is captured after a merger.")
    keyword_index.reindex(tmp_path, force=True)
    kconn = keyword_index.connect(tmp_path / keyword_index.DB_RELPATH)

    # Graph: source mentions the concept (active edge).
    gdb = tmp_path / "db" / "graph.sqlite"
    graph.init_db(gdb)
    gconn = graph.connect(gdb)
    graph.reindex_nodes(gconn, source_ids=[SRC_OK, SRC_ARCH],
                        page_nodes=[{"node_id": CPT, "node_type": "concept", "slug": "synergy", "status": "active"}],
                        now="t0")
    graph.upsert_assertion(gconn, src_id=SRC_OK, dst_id=CPT, edge_type="mentions",
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
    concept = paths[f"wiki/Concepts/{CPT}.md"]
    assert concept["answer_eligible"] is True
    assert concept["page_type"] == "concept"


def test_graph_mode_is_flat_subgraph_seeded_from_navigation(vault):
    res = _run(vault, "synergy", mode="graph")
    assert res["retrieval_path"] == ["graph"]
    g = res["graph"]
    assert CPT in g["seeds"]  # the concept page seeds the graph
    ids = {n["node_id"] for n in g["nodes"]}
    assert {CPT, SRC_OK} <= ids  # BFS reached the mentioning source
    assert any(e["edge_type"] == "mentions" and e["src_id"] == SRC_OK and e["dst_id"] == CPT
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


# --- golden Build Spec §8.2 examples (topic extraction makes routed NL queries work) ---


def test_golden_discovery_finds_topic_not_question_words(vault):
    res = _run(vault, "what do I know about synergy", mode="auto")
    assert res["shape"] == "discovery"
    assert res["retrieval_path"] == ["navigation", "graph"]
    assert any(h["node_id"] == CPT for h in res["navigation"])  # topic 'synergy' matched the concept
    assert CPT in res["graph"]["seeds"]


def test_golden_mention_routes_keyword_and_graph(vault):
    res = _run(vault, "which documents mention synergy", mode="auto")
    assert res["shape"] == "mention"
    assert res["retrieval_path"] == ["keyword", "graph"]
    assert res["counts"]["evidence"] >= 1  # keyword evidence on the topic


def test_golden_relationship_routes_to_graph(vault):
    res = _run(vault, "how are synergy and mergers related", mode="auto")
    assert res["shape"] == "relationship"
    assert res["retrieval_path"] == ["graph"]
    assert CPT in res["graph"]["seeds"]


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


def _concept_index(tmp_path, concept_id, title, summary):
    _write_page(tmp_path, f"wiki/Concepts/{concept_id}.md",
                {"type": "concept", "concept_id": concept_id, "title": title, "status": "active",
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
    seed = "cpt_anchorxxxxxxxxx"
    arch, dele, depr = "cpt_archivedxxxxxx", "cpt_deletedxxxxxxx", "cpt_deprecatedxxx"
    kconn = _concept_index(tmp_path, seed, "Anchor", "anchor topic")
    gconn = _graph(tmp_path, [
        {"node_id": seed, "node_type": "concept", "slug": "a", "status": "active"},
        {"node_id": arch, "node_type": "concept", "slug": "b", "status": "archived"},
        {"node_id": dele, "node_type": "concept", "slug": "c", "status": "deleted"},
        {"node_id": depr, "node_type": "concept", "slug": "d", "status": "deprecated_candidate"},
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
    a, b, d = "cpt_aaaaaaaaaaaaaaa", "cpt_bbbbbbbbbbbbbbb", "cpt_ddddddddddddddd"
    kconn = _concept_index(tmp_path, a, "Alpha", "alpha topic")
    gconn = _graph(tmp_path, [
        {"node_id": a, "node_type": "concept", "slug": "a", "status": "active"},
        {"node_id": b, "node_type": "concept", "slug": "b", "status": "active"},
        {"node_id": d, "node_type": "concept", "slug": "d", "status": "active"},
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
