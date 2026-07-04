from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.backend import graph, keyword_index
from app.backend import main as main_module
from app.backend.config import get_settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient whose app is pointed at an isolated temp project root."""
    settings = get_settings(tmp_path)
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    settings.manifests_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


def _seed(tmp_path: Path, name: str, content: str) -> None:
    (tmp_path / "raw" / "inbox" / name).write_text(content, encoding="utf-8")


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "app": "knowledge-system",
        "version": "0.1.0",
    }


def test_intake_scan_then_sources_are_path_sanitized(client, tmp_path):
    _seed(tmp_path, "probe.md", "hello api\n")

    scan = client.post("/jobs/intake-scan")
    assert scan.status_code == 200
    assert scan.json()["files_found"] == 1

    resp = client.get("/sources")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    source = data["sources"][0]
    # Absolute path is never exposed; relative path is.
    assert "raw_path" not in source
    assert "relative_raw_path" in source
    assert source["relative_raw_path"].startswith("raw/inbox/")

    sid = source["source_id"]
    one = client.get(f"/sources/{sid}")
    assert one.status_code == 200
    assert "raw_path" not in one.json()

    assert client.get("/sources/src_doesnotexist").status_code == 404


def test_jobs_endpoints(client, tmp_path):
    _seed(tmp_path, "probe.md", "hello jobs\n")
    job_id = client.post("/jobs/intake-scan").json()["job_id"]

    listing = client.get("/jobs")
    assert listing.status_code == 200
    data = listing.json()
    assert data["count"] >= 1
    assert any(j["job_id"] == job_id for j in data["jobs"])

    one = client.get(f"/jobs/{job_id}")
    assert one.status_code == 200
    body = one.json()
    assert body["job_type"] == "intake_scan"
    assert body["status"] == "succeeded"

    assert client.get("/jobs/job_missing").status_code == 404


def test_extract_endpoint_then_serve_chunks_and_normalized(client, tmp_path):
    # Markdown needs no extraction extras, so this exercises the full path in core.
    _seed(tmp_path, "doc.md", "# Title\n\nA paragraph of real body text.\n")
    client.post("/jobs/intake-scan")

    extracted = client.post("/jobs/extract")
    assert extracted.status_code == 200
    assert extracted.json()["extracted"] == 1

    sid = client.get("/sources").json()["sources"][0]["source_id"]

    chunks = client.get(f"/sources/{sid}/chunks")
    assert chunks.status_code == 200
    body = chunks.json()
    assert body["source_id"] == sid
    assert body["count"] >= 1

    normalized = client.get(f"/sources/{sid}/normalized")
    assert normalized.status_code == 200
    assert "Title" in normalized.json()["content"]
    assert normalized.json()["markdown_path"] == f"normalized/markdown/{sid}.md"


def test_chunks_and_normalized_404_before_extraction(client, tmp_path):
    _seed(tmp_path, "doc.md", "# T\n\nbody text here.\n")
    client.post("/jobs/intake-scan")
    sid = client.get("/sources").json()["sources"][0]["source_id"]

    # Manifested but not yet extracted → gated on ingestion_status, not file existence.
    assert client.get(f"/sources/{sid}/chunks").status_code == 404
    assert client.get(f"/sources/{sid}/normalized").status_code == 404
    # Unknown source id.
    assert client.get("/sources/src_missing/chunks").status_code == 404


def test_generate_wiki_endpoint_and_pages(client, tmp_path):
    # Templates are code; the temp project root needs them for generation.
    shutil.copytree(ROOT / "templates", tmp_path / "templates")
    _seed(tmp_path, "doc.md", "# Title\n\nA solid opening paragraph of real prose text.\n")
    client.post("/jobs/intake-scan")
    client.post("/jobs/extract")

    gen = client.post("/jobs/generate-wiki")
    assert gen.status_code == 200
    assert gen.json()["generated"] == 1

    listing = client.get("/wiki/pages")
    assert listing.status_code == 200
    body = listing.json()
    assert body["count"] == 1
    page = body["pages"][0]
    sid = page["source_id"]
    assert page["status"] == "active"
    assert page["summary"]
    assert page["wiki_path"] == f"wiki/Sources/{sid}.md"

    detail = client.get(f"/wiki/pages/{sid}")
    assert detail.status_code == 200
    dj = detail.json()
    assert dj["frontmatter"]["source_id"] == sid
    # Title derives from the filename ("doc"); the extractive summary carries the prose.
    assert dj["frontmatter"]["title"] == "doc"
    assert "A solid opening paragraph" in dj["content"]

    assert client.get("/wiki/pages/src_missing").status_code == 404


def _build_graph(tmp_path: Path) -> tuple[str, str]:
    """Seed a minimal graph (a source mentions an active concept) and return their ids."""
    src = "src_aaaaaaaaaaaaaaaa"
    cpt = "cpt_xxxxxxxxxxxxxxxx"
    db_path = tmp_path / "db" / "graph.sqlite"
    graph.init_db(db_path)
    conn = graph.connect(db_path)
    try:
        graph.reindex_nodes(
            conn,
            source_ids=[src],
            page_nodes=[{"node_id": cpt, "node_type": "concept", "slug": "x", "status": "active"}],
            now="t0",
        )
        graph.upsert_assertion(conn, src_id=src, dst_id=cpt, edge_type="mentions",
                               asserted_by="llm", status="active")
    finally:
        conn.close()
    return src, cpt


def test_graph_node_endpoint(client, tmp_path):
    src, cpt = _build_graph(tmp_path)

    resp = client.get(f"/graph/node/{cpt}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["node"]["node_id"] == cpt
    assert body["node"]["answer_eligible"] is True
    assert body["counts"]["incoming"] == 1
    inc = body["incoming"]["mentions"][0]
    assert inc["other_node_id"] == src
    assert inc["evidence"]["advisory"] is True

    # Unknown node and bad include_status.
    assert client.get("/graph/node/cpt_missing").status_code == 404
    assert client.get(f"/graph/node/{cpt}?include_status=bogus").status_code == 400


def test_graph_neighborhood_endpoint(client, tmp_path):
    src, cpt = _build_graph(tmp_path)

    resp = client.get(f"/graph/neighborhood/{cpt}?depth=1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["root_id"] == cpt
    assert {n["node_id"] for n in body["nodes"]} == {cpt, src}
    assert body["truncated"] is False

    # Hard depth cap is enforced by the query schema (le=2).
    assert client.get(f"/graph/neighborhood/{cpt}?depth=3").status_code == 422
    # Bad filters -> 400.
    assert client.get(f"/graph/neighborhood/{cpt}?node_types=widget").status_code == 400
    assert client.get("/graph/neighborhood/cpt_missing").status_code == 404


def test_graph_endpoints_404_without_graph_db(client):
    # No graph built yet (the client fixture lays down no db/graph.sqlite).
    assert client.get("/graph/node/cpt_x").status_code == 404
    assert client.get("/graph/neighborhood/cpt_x").status_code == 404


def _build_graph_with_contradiction(tmp_path: Path) -> tuple[str, str]:
    """Two claims with a symmetric contradicts edge plus a proposed mention for include_status."""
    clm1, clm2, src = "clm_1111111111111111", "clm_2222222222222222", "src_dddddddddddddddd"
    db_path = tmp_path / "db" / "graph.sqlite"
    graph.init_db(db_path)
    conn = graph.connect(db_path)
    try:
        graph.reindex_nodes(
            conn, source_ids=[src],
            page_nodes=[
                {"node_id": clm1, "node_type": "claim", "slug": None, "status": "active"},
                {"node_id": clm2, "node_type": "claim", "slug": None, "status": "active"},
            ],
            now="t0",
        )
        graph.upsert_assertion(conn, src_id=clm1, dst_id=clm2, edge_type="contradicts",
                               asserted_by="llm", status="active")
        graph.upsert_assertion(conn, src_id=clm1, dst_id=src, edge_type="derived_from",
                               asserted_by="human", status="proposed")
    finally:
        conn.close()
    return clm1, clm2


def test_graph_neighborhood_symmetric_edge_is_canonical_only(client, tmp_path):
    clm1, clm2 = _build_graph_with_contradiction(tmp_path)
    body = client.get(f"/graph/neighborhood/{clm1}?depth=1").json()
    edge = next(e for e in body["edges"] if e["edge_type"] == "contradicts")
    assert edge["symmetric"] is True
    assert (edge["src_id"], edge["dst_id"]) == (clm1, clm2)
    assert "other_node_id" not in edge  # canonical-only in the flat list
    # node endpoint, by contrast, exposes other_node_id.
    nb = client.get(f"/graph/node/{clm1}").json()
    assert nb["outgoing"]["contradicts"][0]["other_node_id"] == clm2


def test_graph_include_status_widens_both_endpoints(client, tmp_path):
    clm1, _ = _build_graph_with_contradiction(tmp_path)
    # The proposed derived_from edge is hidden by default, surfaced via include_status.
    default = client.get(f"/graph/node/{clm1}").json()
    assert "derived_from" not in default["outgoing"]
    widened = client.get(f"/graph/node/{clm1}?include_status=active,proposed").json()
    assert "derived_from" in widened["outgoing"]

    nb = client.get(f"/graph/neighborhood/{clm1}?depth=1&include_status=active,proposed").json()
    assert any(e["edge_type"] == "derived_from" for e in nb["edges"])


def test_graph_bad_params_and_limits(client, tmp_path):
    clm1, _ = _build_graph_with_contradiction(tmp_path)
    assert client.get(f"/graph/node/{clm1}?include_status=bogus").status_code == 400
    assert client.get(f"/graph/neighborhood/{clm1}?edge_types=frobnicates").status_code == 400
    assert client.get(f"/graph/neighborhood/{clm1}?include_status=nope").status_code == 400
    # Out-of-range limits are rejected by the query schema (ge=1).
    assert client.get(f"/graph/neighborhood/{clm1}?node_limit=0").status_code == 422
    assert client.get(f"/graph/neighborhood/{clm1}?edge_limit=0").status_code == 422


def test_graph_stale_schema_returns_503(client, tmp_path):
    clm1, _ = _build_graph_with_contradiction(tmp_path)
    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    conn.execute("PRAGMA user_version = 999")
    conn.commit()
    conn.close()
    assert client.get(f"/graph/node/{clm1}").status_code == 503
    assert client.get(f"/graph/neighborhood/{clm1}").status_code == 503


def _build_search_corpus(tmp_path: Path) -> str:
    """Write one chunk + a source page + a matching active concept page, then build the index
    and a graph (source mentions concept). Returns the concept node id."""
    src, cpt = "src_eeeeeeeeeeeeeeee", "cpt_searchxxxxxxxxx"
    chunks = tmp_path / "normalized" / "chunks" / f"{src}.jsonl"
    chunks.parent.mkdir(parents=True, exist_ok=True)
    text = "Synergy capture is central to post-merger integration."
    chunks.write_text(json.dumps({
        "chunk_id": f"{src}::0000", "source_id": src, "ordinal": 0, "kind": "prose",
        "heading_path": [], "section": None, "text": text, "char_start": 0,
        "char_end": len(text), "page": 1, "page_end": 1,
        "table_reference": None, "sheet_reference": None,
    }) + "\n", encoding="utf-8")
    for rel, fm, summ in [
        (f"wiki/Sources/{src}.md",
         {"type": "source", "source_id": src, "title": "Deck", "status": "active", "language": "en"},
         "synergy in M&A"),
        (f"wiki/Concepts/{cpt}.md",
         {"type": "concept", "concept_id": cpt, "title": "Synergy capture", "status": "active",
          "review_status": "none"},
         "How synergy is captured."),
    ]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        fm_lines = "\n".join(f"{k}: {v}" for k, v in fm.items())
        p.write_text(f"---\n{fm_lines}\n---\n\n# {fm['title']}\n\n> [!summary]\n> {summ}\n", encoding="utf-8")
    keyword_index.reindex(tmp_path, force=True)

    gdb = tmp_path / "db" / "graph.sqlite"
    graph.init_db(gdb)
    conn = graph.connect(gdb)
    try:
        graph.reindex_nodes(conn, source_ids=[src],
                            page_nodes=[{"node_id": cpt, "node_type": "concept", "slug": "s", "status": "active"}],
                            now="t0")
        graph.upsert_assertion(conn, src_id=src, dst_id=cpt, edge_type="mentions",
                               asserted_by="llm", status="active")
    finally:
        conn.close()
    return cpt


def test_search_returns_grouped_evidence_and_graph(client, tmp_path):
    cpt = _build_search_corpus(tmp_path)

    body = client.get("/search?q=synergy").json()
    assert body["mode"] == "auto"
    assert body["counts"]["evidence"] >= 1
    assert body["notes"] == []  # 4e response shape: notes present (empty in the normal case)
    ev = body["evidence"][0]
    assert ev["retrieval_path"] == ["keyword"]
    assert ev["char_start"] == 0 and ev["snippet"]
    assert ev["channels"]["keyword"]["rank"] == 1  # single-channel hit still carries `channels`

    g = client.get("/search?q=synergy&mode=graph").json()
    assert g["retrieval_path"] == ["graph"]
    assert cpt in g["graph"]["seeds"]
    assert any(n["node_id"] == cpt for n in g["graph"]["nodes"])


def test_search_errors(client, tmp_path):
    _build_search_corpus(tmp_path)
    # mode=vector is valid now (4d) but unavailable with no embedder configured -> 503.
    assert client.get("/search?q=x&mode=vector").status_code == 503
    assert client.get("/search?q=x&mode=bogus").status_code == 400
    assert client.get("/search?q=x&source_status=nope").status_code == 400
    assert client.get("/search?q=x&edge_status=nope").status_code == 400
    assert client.get("/search?q=x&page_type=widget").status_code == 400
    assert client.get("/search?q=x&node_type=widget").status_code == 400
    assert client.get("/search?q=x&language=fr").status_code == 400
    assert client.get("/search?q=x&evidence_limit=0").status_code == 422


def test_search_without_index_is_structural_empty(client):
    # No keyword index built in this fresh client; /search degrades to a structural empty result.
    body = client.get("/search?q=synergy").json()
    assert body["no_results"] is True
    assert body["counts"] == {"evidence": 0, "navigation": 0, "graph": 0}


class _FakeEmbedder:
    dimension = 8

    def embed(self, texts):
        import hashlib
        return [
            [hashlib.sha256(t.encode("utf-8")).digest()[i % 32] / 255.0 for i in range(8)]
            for t in texts
        ]


def _configure_vector(tmp_path, client, monkeypatch):
    """Build keyword + vector indexes and point settings at a fake embedder. Returns the source id."""
    import dataclasses

    from app.backend import keyword_index, vector_index

    src = "src_ffffffffffffffff"
    text = "synergy capture is central to post-merger integration"
    chunks = tmp_path / "normalized" / "chunks" / f"{src}.jsonl"
    chunks.parent.mkdir(parents=True, exist_ok=True)
    chunks.write_text(json.dumps({
        "chunk_id": f"{src}::0000", "source_id": src, "ordinal": 0, "kind": "prose",
        "heading_path": [], "section": None, "text": text, "char_start": 0, "char_end": len(text),
        "page": 1, "page_end": 1, "table_reference": None, "sheet_reference": None,
    }) + "\n", encoding="utf-8")
    sp = tmp_path / "wiki" / "Sources" / f"{src}.md"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(f"---\ntype: source\nsource_id: {src}\ntitle: Deck\nstatus: active\nlanguage: en\n"
                  f"---\n\n# Deck\n\n> [!summary]\n> s\n", encoding="utf-8")
    keyword_index.reindex(tmp_path, force=True)
    vector_index.reindex(tmp_path, _FakeEmbedder(), embedding_model_ref="bge-m3",
                         distance_metric="cosine", force=True)
    monkeypatch.setattr(main_module, "settings", dataclasses.replace(
        main_module.settings, embedding_base_url="http://127.0.0.1:8080/v1",
        embedding_model_ref="bge-m3", embedding_dimension=8))
    monkeypatch.setattr(main_module.embeddings, "client_from_settings", lambda s, **kw: _FakeEmbedder())
    return src


def test_search_vector_mode_returns_evidence(client, tmp_path, monkeypatch):
    src = _configure_vector(tmp_path, client, monkeypatch)
    body = client.get("/search?q=synergy%20capture&mode=vector").json()
    assert body["retrieval_path"] == ["vector"]
    assert body["counts"]["evidence"] >= 1
    hit = body["evidence"][0]
    assert hit["source_id"] == src and hit["retrieval_path"] == ["vector"]
    assert "kind" in hit and hit["snippet"]
    assert "vector" in hit["channels"] and body["notes"] == []  # 4e shape: channels + notes


def test_search_vector_503_without_index(client, tmp_path, monkeypatch):
    # Embedder configured but no vector index built -> controlled 503.
    import dataclasses
    monkeypatch.setattr(main_module, "settings", dataclasses.replace(
        main_module.settings, embedding_base_url="http://127.0.0.1:8080/v1",
        embedding_model_ref="bge-m3", embedding_dimension=8))
    monkeypatch.setattr(main_module.embeddings, "client_from_settings", lambda s, **kw: _FakeEmbedder())
    assert client.get("/search?q=x&mode=vector").status_code == 503


def test_search_auto_blends_vector_for_conceptual_query(client, tmp_path, monkeypatch):
    _configure_vector(tmp_path, client, monkeypatch)
    # 4e-2: a conceptual (default-shape) query blends keyword + vector via RRF when vector is ready.
    body = client.get("/search?q=synergy%20capture&mode=auto").json()
    assert "vector" in body["retrieval_path"]
    assert body["notes"] == []  # vector available -> no degradation note


def test_search_auto_degrades_silently_without_embedder(client, tmp_path, monkeypatch):
    # Keyword-only deployment (no embedder configured): auto wants vector for a conceptual query but
    # degrades QUIETLY — no 503, no degradation note (it isn't a degradation, it's the deployment).
    _build_search_corpus(tmp_path)  # keyword + graph, no embedder
    body = client.get("/search?q=synergy&mode=auto")
    assert body.status_code == 200
    assert body.json()["notes"] == [] and "vector" not in body.json()["retrieval_path"]


def test_search_auto_notes_degradation_on_stale_index(client, tmp_path, monkeypatch):
    src = _configure_vector(tmp_path, client, monkeypatch)  # embedder configured + fresh index
    # Edit a chunk after indexing -> the vector index is stale (embedder IS configured) -> genuine
    # degradation: auto conceptual query stays 200 keyword-only WITH a note.
    (tmp_path / "normalized" / "chunks" / f"{src}.jsonl").write_text(json.dumps({
        "chunk_id": f"{src}::0000", "source_id": src, "ordinal": 0, "kind": "prose",
        "heading_path": [], "section": None, "text": "EDITED anchors moved", "char_start": 0,
        "char_end": 20, "page": 1, "page_end": 1, "table_reference": None, "sheet_reference": None,
    }) + "\n", encoding="utf-8")
    body = client.get("/search?q=synergy%20capture&mode=auto").json()
    assert "vector" not in body["retrieval_path"]
    assert any("degraded to keyword-only" in n for n in body["notes"])


def test_search_auto_graph_only_skips_vector_capability(client, tmp_path, monkeypatch):
    _configure_vector(tmp_path, client, monkeypatch)
    calls = {"n": 0}
    real = main_module._vector_capability

    def spy(*a, **k):
        calls["n"] += 1
        return real(*a, **k)
    monkeypatch.setattr(main_module, "_vector_capability", spy)
    # A discovery (graph-only) auto query must NOT inspect vector state at all.
    client.get("/search?q=what%20do%20I%20know%20about%20synergy&mode=auto")
    assert calls["n"] == 0
    # A conceptual query does build the capability.
    client.get("/search?q=synergy%20capture&mode=auto")
    assert calls["n"] == 1


def test_search_vector_honors_source_id(client, tmp_path, monkeypatch):
    src = _configure_vector(tmp_path, client, monkeypatch)
    body = client.get(f"/search?q=synergy&mode=vector&source_id={src}").json()
    assert body["evidence"] and all(h["source_id"] == src for h in body["evidence"])
    other = client.get("/search?q=synergy&mode=vector&source_id=src_nonexistent00").json()
    assert other["evidence"] == []  # no rows for a different source


def test_search_vector_503_on_chunk_drift(client, tmp_path, monkeypatch):
    src = _configure_vector(tmp_path, client, monkeypatch)
    # Edit the chunk file after indexing -> the vector rows are stale -> refuse to serve.
    chunks = tmp_path / "normalized" / "chunks" / f"{src}.jsonl"
    chunks.write_text(json.dumps({
        "chunk_id": f"{src}::0000", "source_id": src, "ordinal": 0, "kind": "prose",
        "heading_path": [], "section": None, "text": "EDITED — anchors moved", "char_start": 0,
        "char_end": 22, "page": 1, "page_end": 1, "table_reference": None, "sheet_reference": None,
    }) + "\n", encoding="utf-8")
    assert client.get("/search?q=synergy&mode=vector").status_code == 503


def test_search_vector_503_when_keyword_index_missing(client, tmp_path, monkeypatch):
    import dataclasses

    from app.backend import vector_index
    # A coherent vector index but NO keyword index -> source status unverifiable -> 503.
    src = "src_gggggggggggggggg"
    chunks = tmp_path / "normalized" / "chunks" / f"{src}.jsonl"
    chunks.parent.mkdir(parents=True, exist_ok=True)
    chunks.write_text(json.dumps({
        "chunk_id": f"{src}::0000", "source_id": src, "ordinal": 0, "kind": "prose",
        "heading_path": [], "section": None, "text": "t", "char_start": 0, "char_end": 1,
        "page": 1, "page_end": 1, "table_reference": None, "sheet_reference": None,
    }) + "\n", encoding="utf-8")
    vector_index.reindex(tmp_path, _FakeEmbedder(), embedding_model_ref="bge-m3",
                         distance_metric="cosine", force=True)
    monkeypatch.setattr(main_module, "settings", dataclasses.replace(
        main_module.settings, embedding_base_url="http://127.0.0.1:8080/v1",
        embedding_model_ref="bge-m3", embedding_dimension=8))
    monkeypatch.setattr(main_module.embeddings, "client_from_settings", lambda s, **kw: _FakeEmbedder())
    assert client.get("/search?q=x&mode=vector").status_code == 503


def test_assert_safe_bind():
    from app.backend.main import assert_safe_bind

    # Loopback is always fine; explicit override allows a non-loopback bind.
    assert_safe_bind("127.0.0.1", False)
    assert_safe_bind("localhost", False)
    assert_safe_bind("::1", False)
    assert_safe_bind("0.0.0.0", True)
    # Non-loopback without the override must refuse startup.
    with pytest.raises(RuntimeError):
        assert_safe_bind("0.0.0.0", False)
    with pytest.raises(RuntimeError):
        assert_safe_bind("192.168.1.10", False)


# --------------------------------------------------------------------------- POST /query (5-2)


QSRC = "src_eeeeeeeeeeeeeeee"
QTEXT = "Synergy capture is central to post-merger integration."


def _build_query_corpus(tmp_path):
    """Chunk + matching Markdown (for quote slicing) + an active source page + keyword index."""
    ch = tmp_path / "normalized" / "chunks" / f"{QSRC}.jsonl"
    ch.parent.mkdir(parents=True, exist_ok=True)
    ch.write_text(json.dumps({
        "chunk_id": f"{QSRC}::0000", "source_id": QSRC, "ordinal": 0, "kind": "prose",
        "heading_path": [], "section": None, "text": QTEXT, "char_start": 0, "char_end": len(QTEXT),
        "page": 1, "page_end": 1, "table_reference": None, "sheet_reference": None,
    }) + "\n", encoding="utf-8")
    md = tmp_path / "normalized" / "markdown"
    md.mkdir(parents=True, exist_ok=True)
    (md / f"{QSRC}.md").write_text(QTEXT, encoding="utf-8")  # md[0:len] == chunk text (groundable)
    sp = tmp_path / "wiki" / "Sources" / f"{QSRC}.md"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("---\ntype: source\nsource_id: " + QSRC + "\ntitle: Deck\nstatus: active\n"
                  "language: en\n---\n\n# Deck\n\n> [!summary]\n> synergy\n", encoding="utf-8")
    man = tmp_path / "raw" / "manifests" / f"{QSRC}.json"  # so saved-query citations ground (ADR-0020)
    man.parent.mkdir(parents=True, exist_ok=True)
    man.write_text(json.dumps({"source_id": QSRC}), encoding="utf-8")
    keyword_index.reindex(tmp_path, force=True)


class _FakeQueryClient:
    def __init__(self, response, *, available=True, raises=None):
        self.response = response
        self._available = available
        self._raises = raises

    def provider_available(self, model_ref):
        return self._available

    def parse(self, messages, schema, model_ref, **kwargs):
        if self._raises is not None:
            raise self._raises
        return self.response


def _use_client(monkeypatch, fake):
    monkeypatch.setattr(main_module, "_query_client", lambda: fake)


def _q(question, **extra):
    return {"question": question, **extra}


def test_query_returns_grounded_answer(client, tmp_path, monkeypatch):
    _build_query_corpus(tmp_path)
    _use_client(monkeypatch, _FakeQueryClient(
        {"claims": [{"text": "Synergy capture is central to integration.", "evidence_ids": ["e1"]}]}))
    body = client.post("/query", json=_q("synergy capture")).json()
    assert body["abstained"] is False
    assert len(body["claims"]) == 1 and len(body["citations"]) == 1
    assert body["citations"][0]["source_id"] == QSRC and body["citations"][0]["char_start"] == 0
    assert "[1]" in body["answer"]
    assert body["unsourced_count"] == 0 and body["security_rejected_count"] == 0
    assert "keyword" in body["retrieval_path"]


def test_query_abstains_when_no_evidence(client, tmp_path, monkeypatch):
    _build_query_corpus(tmp_path)
    _use_client(monkeypatch, _FakeQueryClient({"claims": [{"text": "x", "evidence_ids": ["e1"]}]}))
    body = client.post("/query", json=_q("nonexistentterm")).json()
    assert body["abstained"] is True and body["answer"] == "No source found in vault."
    assert body["evidence_count"] == 0 and body["claims"] == []


def test_query_503_without_model(client, tmp_path, monkeypatch):
    _build_query_corpus(tmp_path)
    _use_client(monkeypatch, _FakeQueryClient({"claims": []}, available=False))
    r = client.post("/query", json=_q("synergy"))
    assert r.status_code == 503 and "configured LLM" in r.json()["detail"]


def test_query_503_on_malformed_query_model(client, tmp_path, monkeypatch):
    import dataclasses
    _build_query_corpus(tmp_path)
    # A real client whose provider_available() raises ConfigError on a malformed QUERY_MODEL.
    monkeypatch.setattr(main_module, "settings",
                        dataclasses.replace(main_module.settings, query_model="badref-no-colon"))
    monkeypatch.setattr(main_module, "_query_client",
                        lambda: main_module.build_client(main_module.settings))
    r = client.post("/query", json=_q("synergy"))
    assert r.status_code == 503 and "misconfigured" in r.json()["detail"]
    assert "badref" not in r.text  # no internal/config detail leaks


def test_query_503_when_parse_raises(client, tmp_path, monkeypatch):
    _build_query_corpus(tmp_path)
    _use_client(monkeypatch, _FakeQueryClient(
        None, raises=main_module.ParseError("provider boom: secret-endpoint")))
    r = client.post("/query", json=_q("synergy capture"))
    assert r.status_code == 503 and r.json()["detail"] == "query answering is temporarily unavailable"
    assert "secret-endpoint" not in r.text  # concrete exception stays server-side


def test_query_400_on_empty_question(client, tmp_path, monkeypatch):
    _use_client(monkeypatch, _FakeQueryClient({"claims": []}))
    assert client.post("/query", json=_q("   ")).status_code == 400


def test_query_400_on_non_evidence_mode(client, tmp_path, monkeypatch):
    _build_query_corpus(tmp_path)
    _use_client(monkeypatch, _FakeQueryClient({"claims": []}))
    r = client.post("/query", json=_q("synergy", mode="graph"))
    assert r.status_code == 400 and "discovery surfaces" in r.json()["detail"]


def test_query_unsourced_count_only_by_default(client, tmp_path, monkeypatch):
    _build_query_corpus(tmp_path)
    _use_client(monkeypatch, _FakeQueryClient({"claims": [
        {"text": "Synergy is captured.", "evidence_ids": ["e1"]},
        {"text": "An unsupported aside.", "evidence_ids": ["e404"]},
    ]}))
    body = client.post("/query", json=_q("synergy capture")).json()
    assert body["unsourced_count"] == 1 and body["unsourced_claims"] == []  # text withheld by default
    full = client.post("/query", json=_q("synergy capture", include_unsourced=True)).json()
    assert full["unsourced_claims"] == ["An unsupported aside."]


def test_query_path_leak_rejected_and_not_leaked(client, tmp_path, monkeypatch):
    _build_query_corpus(tmp_path)
    _use_client(monkeypatch, _FakeQueryClient({"claims": [
        {"text": "Synergy is captured.", "evidence_ids": ["e1"]},
        {"text": "Stored at /home/jolulop/secret.txt.", "evidence_ids": ["e1"]},
    ]}))
    r = client.post("/query", json=_q("synergy capture", include_unsourced=True))
    body = r.json()
    assert body["security_rejected_count"] == 1
    assert "/home/jolulop/secret.txt" not in r.text  # never returned verbatim, anywhere


def test_query_source_quote_with_path_is_returned_intact(client, tmp_path, monkeypatch):
    # A source document legitimately contains an absolute path. The verbatim quote MUST survive
    # (grounding requires it); only system/generated paths are withheld (ADR-0034 Q2).
    src, text = "src_ffffffffffffffff", "Logs live at /var/log/app per the runbook."
    ch = tmp_path / "normalized" / "chunks" / f"{src}.jsonl"
    ch.parent.mkdir(parents=True, exist_ok=True)
    ch.write_text(json.dumps({
        "chunk_id": f"{src}::0000", "source_id": src, "ordinal": 0, "kind": "prose",
        "heading_path": [], "section": None, "text": text, "char_start": 0, "char_end": len(text),
        "page": 1, "page_end": 1, "table_reference": None, "sheet_reference": None,
    }) + "\n", encoding="utf-8")
    md = tmp_path / "normalized" / "markdown"
    md.mkdir(parents=True, exist_ok=True)
    (md / f"{src}.md").write_text(text, encoding="utf-8")
    sp = tmp_path / "wiki" / "Sources" / f"{src}.md"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("---\ntype: source\nsource_id: " + src + "\ntitle: Run\nstatus: active\n"
                  "language: en\n---\n\n# Run\n\n> [!summary]\n> logs\n", encoding="utf-8")
    keyword_index.reindex(tmp_path, force=True)
    _use_client(monkeypatch, _FakeQueryClient(
        {"claims": [{"text": "The runbook documents the log location.", "evidence_ids": ["e1"]}]}))
    r = client.post("/query", json=_q("where are logs"))
    body = r.json()
    assert body["citations"][0]["quote"] == text          # source path survives verbatim in the quote
    assert str(tmp_path) not in r.text                     # but no server/generated path leaks


def test_query_response_leaks_no_absolute_paths(client, tmp_path, monkeypatch):
    _build_query_corpus(tmp_path)
    _use_client(monkeypatch, _FakeQueryClient(
        {"claims": [{"text": "Synergy is captured.", "evidence_ids": ["e1"]}]}))
    r = client.post("/query", json=_q("synergy capture"))
    assert str(tmp_path) not in r.text and r.status_code == 200


# --------------------------------------------------------------------------- /query save (5-3)


def _saved_query_path(tmp_path, qid):
    return tmp_path / "wiki" / "Queries" / f"{qid}.md"


def _validator_ok(tmp_path, script):
    import subprocess
    import sys as _sys
    r = subprocess.run([_sys.executable, f"scripts/{script}", str(tmp_path)],
                       capture_output=True, text=True)
    return r.returncode == 0, r.stdout + r.stderr


def _frontmatter_ok_isolated(tmp_path, page_path):
    # Validate ONLY the query page: the minimal fixture Source page isn't a full source artifact, so
    # check the saved page against validate_frontmatter.py in a clean vault containing just it.
    clean = tmp_path / "_fmcheck"
    dest = clean / "wiki" / "Queries"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / page_path.name).write_text(page_path.read_text(encoding="utf-8"), encoding="utf-8")
    return _validator_ok(clean, "validate_frontmatter.py")


def test_query_save_writes_roundtrip_page(client, tmp_path, monkeypatch):
    from app.workers import citations
    from app.workers.wiki_render import parse_frontmatter
    _build_query_corpus(tmp_path)
    _use_client(monkeypatch, _FakeQueryClient(
        {"claims": [{"text": "Synergy capture is central to integration.", "evidence_ids": ["e1"]}]}))
    body = client.post("/query", json=_q("What is synergy capture?", save=True)).json()
    qid = body["query_id"]
    assert qid and qid.startswith("qry_")
    assert body["navigation_stale"] is True  # honest: page saved, nav not yet rebuilt
    page = _saved_query_path(tmp_path, qid).read_text(encoding="utf-8")

    fm = parse_frontmatter(page)
    assert fm["type"] == "query" and fm["status"] == "active" and fm["answer_eligible"] == "false"
    assert "created" not in fm and "last_compiled_at" not in fm  # deterministic, no wall-clock
    fmblock = page.split("---", 2)[1]
    cites = citations.parse_citations(fmblock)
    assert len(cites) == 1 and cites[0]["source_id"] == QSRC
    md = (tmp_path / "normalized" / "markdown" / f"{QSRC}.md").read_text(encoding="utf-8")
    assert citations.ground_citation(cites[0], md, require_quote=True) == []  # frontmatter record grounds
    assert "## Citations" in page and "## Answer" in page

    # The real validators accept the saved page (citations grounded + frontmatter complete).
    ok_c, out_c = _validator_ok(tmp_path, "validate_citations.py")
    assert ok_c, out_c
    ok_f, out_f = _frontmatter_ok_isolated(tmp_path, _saved_query_path(tmp_path, qid))
    assert ok_f, out_f


def test_query_save_defers_navigation_refresh(client, tmp_path, monkeypatch):
    # ADR-0034 Q3: save persists the page + appends wiki/log.md, but does NOT synchronously rebuild
    # wiki/index.md or the nav index — discoverability lags until the next reindex.
    _build_query_corpus(tmp_path)
    _use_client(monkeypatch, _FakeQueryClient(
        {"claims": [{"text": "Synergy is captured.", "evidence_ids": ["e1"]}]}))
    body = client.post("/query", json=_q("synergy capture", save=True)).json()
    assert body["navigation_stale"] is True
    assert not (tmp_path / "wiki" / "index.md").exists()        # index NOT synchronously rebuilt
    log = (tmp_path / "wiki" / "log.md").read_text(encoding="utf-8")
    assert body["query_id"] in log and "query saved" in log     # but the write is logged for audit


def test_query_save_abstained_page_is_valid(client, tmp_path, monkeypatch):
    _build_query_corpus(tmp_path)
    _use_client(monkeypatch, _FakeQueryClient({"claims": [{"text": "x", "evidence_ids": ["e1"]}]}))
    body = client.post("/query", json=_q("nonexistentterm", save=True)).json()
    assert body["abstained"] is True
    page = _saved_query_path(tmp_path, body["query_id"]).read_text(encoding="utf-8")
    assert "No source found in vault." in page  # marker present -> validators accept the no-citation page
    assert _validator_ok(tmp_path, "validate_citations.py")[0]
    assert _frontmatter_ok_isolated(tmp_path, _saved_query_path(tmp_path, body["query_id"]))[0]


def test_query_id_source_status_order_insensitive():
    from app.workers import query as qmod
    a = qmod.query_id("q", source_status="active,deprecated_candidate")
    b = qmod.query_id("q", source_status="deprecated_candidate,active")
    assert a == b  # same scope regardless of filter order
    assert a != qmod.query_id("q", source_status="active")  # but a different status set differs


def test_query_distinct_scope_gets_distinct_ids(client, tmp_path, monkeypatch):
    _build_query_corpus(tmp_path)
    _use_client(monkeypatch, _FakeQueryClient(
        {"claims": [{"text": "Synergy is captured.", "evidence_ids": ["e1"]}]}))
    a = client.post("/query", json=_q("synergy capture", save=True)).json()["query_id"]
    # Same question, different answer-affecting scope (source_id filter) -> different page, no clobber.
    b = client.post("/query", json=_q("synergy capture", save=True, source_id=QSRC)).json()["query_id"]
    assert a != b and len(list((tmp_path / "wiki" / "Queries").glob("*.md"))) == 2


def test_query_no_save_persists_nothing(client, tmp_path, monkeypatch):
    _build_query_corpus(tmp_path)
    _use_client(monkeypatch, _FakeQueryClient(
        {"claims": [{"text": "Synergy is captured.", "evidence_ids": ["e1"]}]}))
    body = client.post("/query", json=_q("synergy capture")).json()  # save defaults to false
    assert body["query_id"] is None and body["navigation_stale"] is False
    assert not (tmp_path / "wiki" / "Queries").exists()


def test_query_save_deterministic_id_overwrites(client, tmp_path, monkeypatch):
    _build_query_corpus(tmp_path)
    _use_client(monkeypatch, _FakeQueryClient(
        {"claims": [{"text": "Synergy is captured.", "evidence_ids": ["e1"]}]}))
    a = client.post("/query", json=_q("What is synergy?", save=True)).json()["query_id"]
    b = client.post("/query", json=_q("  what   IS Synergy?  ", save=True)).json()["query_id"]
    assert a == b  # normalized (whitespace + case) content key -> same page, overwritten
    assert list((tmp_path / "wiki" / "Queries").glob("*.md")) == [_saved_query_path(tmp_path, a)]


def test_query_save_security_rejection_not_in_page(client, tmp_path, monkeypatch):
    _build_query_corpus(tmp_path)
    _use_client(monkeypatch, _FakeQueryClient({"claims": [
        {"text": "Synergy is captured.", "evidence_ids": ["e1"]},
        {"text": "Stored at /home/jolulop/secret.txt.", "evidence_ids": ["e1"]},
    ]}))
    qid = client.post("/query", json=_q("synergy capture", save=True)).json()["query_id"]
    page = _saved_query_path(tmp_path, qid).read_text(encoding="utf-8")
    assert "/home/jolulop/secret.txt" not in page          # never persisted verbatim
    assert "absolute_path_leak" in page                    # summarised by reason


def test_query_validator_rejects_bad_saved_citations(tmp_path):
    # The strengthened _check_query grounds saved-query citations like a claim: each failure mode fails.
    from app.workers.wiki_render import render_query_page
    _build_query_corpus(tmp_path)
    (tmp_path / "raw" / "manifests" / "src_aaaaaaaaaaaaaaaa.json").write_text(
        json.dumps({"source_id": "src_aaaaaaaaaaaaaaaa"}), encoding="utf-8")  # manifest, but no Markdown
    base = {"source_id": QSRC, "char_start": 0, "char_end": len(QTEXT), "page": None, "page_end": None,
            "section": None, "table_reference": None, "sheet_reference": None, "chunk_id": None,
            "quote": QTEXT}
    qdir = tmp_path / "wiki" / "Queries"
    qdir.mkdir(parents=True, exist_ok=True)
    bad = [
        {**base, "quote": "not the source text"},          # quote mismatch
        {**base, "char_end": len(QTEXT) + 500},            # out-of-bounds span
        {**base, "source_id": "src_bbbbbbbbbbbbbbbb"},      # no manifest
        {**base, "source_id": "src_aaaaaaaaaaaaaaaa"},      # manifest but no normalized Markdown
    ]
    for cit in bad:
        page = render_query_page({"query_id": "qry_bad0000000000", "question": "q", "answer": "a [1]",
                                  "citations": [cit], "retrieval_modes": ["keyword"],
                                  "unsourced_claims": [], "security_rejected_count": 0})
        (qdir / "qry_bad0000000000.md").write_text(page, encoding="utf-8")
        assert not _validator_ok(tmp_path, "validate_citations.py")[0], f"expected failure: {cit}"
    # Sanity: the well-formed citation passes.
    good = render_query_page({"query_id": "qry_bad0000000000", "question": "q", "answer": "a [1]",
                              "citations": [base], "retrieval_modes": ["keyword"],
                              "unsourced_claims": [], "security_rejected_count": 0})
    (qdir / "qry_bad0000000000.md").write_text(good, encoding="utf-8")
    assert _validator_ok(tmp_path, "validate_citations.py")[0]


def test_query_save_preserves_source_quote_path(client, tmp_path, monkeypatch):
    src, text = "src_ffffffffffffffff", "Logs live at /var/log/app per the runbook."
    ch = tmp_path / "normalized" / "chunks" / f"{src}.jsonl"
    ch.parent.mkdir(parents=True, exist_ok=True)
    ch.write_text(json.dumps({
        "chunk_id": f"{src}::0000", "source_id": src, "ordinal": 0, "kind": "prose",
        "heading_path": [], "section": None, "text": text, "char_start": 0, "char_end": len(text),
        "page": 1, "page_end": 1, "table_reference": None, "sheet_reference": None,
    }) + "\n", encoding="utf-8")
    (tmp_path / "normalized" / "markdown").mkdir(parents=True, exist_ok=True)
    (tmp_path / "normalized" / "markdown" / f"{src}.md").write_text(text, encoding="utf-8")
    sp = tmp_path / "wiki" / "Sources" / f"{src}.md"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("---\ntype: source\nsource_id: " + src + "\ntitle: Run\nstatus: active\n"
                  "language: en\n---\n\n# Run\n\n> [!summary]\n> logs\n", encoding="utf-8")
    (tmp_path / "raw" / "manifests" / f"{src}.json").write_text(
        json.dumps({"source_id": src}), encoding="utf-8")
    keyword_index.reindex(tmp_path, force=True)
    _use_client(monkeypatch, _FakeQueryClient(
        {"claims": [{"text": "The runbook documents the log path.", "evidence_ids": ["e1"]}]}))
    qid = client.post("/query", json=_q("where are logs", save=True)).json()["query_id"]
    page = _saved_query_path(tmp_path, qid).read_text(encoding="utf-8")
    assert "/var/log/app" in page                # verbatim source quote preserved (grounding needs it)
    assert str(tmp_path) not in page             # but no server/generated path
    assert _validator_ok(tmp_path, "validate_citations.py")[0]


# --- Phase 6 review ledger (ADR-0035 slice 6-1) ----------------------------


def _write_review(tmp_path, state, item):
    d = tmp_path / "reviews" / state
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{item['review_id']}.json").write_text(json.dumps(item), encoding="utf-8")


def test_reviews_list_default_pending_excludes_deferred(client, tmp_path):
    _write_review(tmp_path, "pending", {
        "review_id": "rev_a", "type": "promote_candidate_node", "status": "pending",
        "priority": "high", "created_at": "2026-01-01T00:00:00Z", "subject": {"node_id": "cpt_1"},
        "proposal": {"to_status": "active", "node_type": "concept"}, "context": {}})
    _write_review(tmp_path, "pending", {
        "review_id": "rev_b", "type": "deprecate_wiki_page", "status": "deferred",
        "priority": "low", "subject": {}, "proposal": {}, "context": {}})

    resp = client.get("/reviews")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["by_type"] == {"promote_candidate_node": 1}
    assert [it["review_id"] for it in body["items"]] == ["rev_a"]
    assert body["parse_errors"] == 0


def test_reviews_list_unknown_status_is_400(client, tmp_path):
    assert client.get("/reviews", params={"status": "bogus"}).status_code == 400


def test_reviews_list_malformed_json_counted_not_crashing(client, tmp_path):
    _write_review(tmp_path, "pending", {
        "review_id": "rev_ok", "type": "promote_candidate_node", "status": "pending",
        "subject": {}, "proposal": {}, "context": {}})
    (tmp_path / "reviews" / "pending" / "rev_bad.json").write_text("{nope", encoding="utf-8")
    body = client.get("/reviews").json()
    assert body["parse_errors"] == 1
    assert [it["review_id"] for it in body["items"]] == ["rev_ok"]


def test_reviews_list_schema_invalid_json_does_not_500(client, tmp_path):
    # valid JSON object but not a usable ReviewItem (missing review_id/type) must not crash the queue
    _write_review(tmp_path, "pending", {
        "review_id": "rev_ok", "type": "promote_candidate_node", "status": "pending",
        "subject": {}, "proposal": {}, "context": {}})
    (tmp_path / "reviews" / "pending" / "rev_bad.json").write_text(
        json.dumps({"status": "pending"}), encoding="utf-8")
    resp = client.get("/reviews")
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_errors"] == 1
    assert body["parse_errors"] == 0
    assert [it["review_id"] for it in body["items"]] == ["rev_ok"]


def test_reviews_detail_404_for_schema_invalid(client, tmp_path):
    (tmp_path / "reviews" / "pending").mkdir(parents=True, exist_ok=True)
    (tmp_path / "reviews" / "pending" / "rev_bad.json").write_text(
        json.dumps({"status": "pending"}), encoding="utf-8")
    assert client.get("/reviews/rev_bad").status_code == 404


def test_reviews_detail_returns_item_and_preview(client, tmp_path):
    _write_review(tmp_path, "pending", {
        "review_id": "rev_a", "type": "promote_candidate_node", "status": "pending",
        "priority": "low", "subject": {"node_id": "cpt_1"},
        "proposal": {"to_status": "active", "node_type": "concept", "name": "thing"}, "context": {}})
    body = client.get("/reviews/rev_a").json()
    assert body["item"]["review_id"] == "rev_a"
    prev = body["preview"]
    assert prev["type"] == "promote_candidate_node"
    assert prev["apply"]["supported"] is True
    assert prev["apply"]["effect_status"] == "pending_apply"
    assert prev["node_ids"] == ["cpt_1"]


def test_reviews_detail_404_for_missing(client, tmp_path):
    assert client.get("/reviews/rev_nope").status_code == 404


def test_reviews_detail_404_for_malformed(client, tmp_path):
    (tmp_path / "reviews" / "pending").mkdir(parents=True, exist_ok=True)
    (tmp_path / "reviews" / "pending" / "rev_bad.json").write_text("{broken", encoding="utf-8")
    assert client.get("/reviews/rev_bad").status_code == 404


def test_reviews_detail_no_server_path_leak(client, tmp_path):
    _write_review(tmp_path, "pending", {
        "review_id": "rev_d", "type": "deprecate_wiki_page", "status": "pending",
        "subject": {"node_id": "clm_1", "page": "Claims/clm_1.md"},
        "proposal": {"to_status": "deprecated_candidate", "reason": "x"},
        "context": {"node_type": "claim"}})
    raw = client.get("/reviews/rev_d").text
    assert str(tmp_path) not in raw


# --- Phase 6 slice 6-2: decision endpoints (record-only) -------------------


def _pending_promote(tmp_path, rid="rev_a"):
    _write_review(tmp_path, "pending", {
        "review_id": rid, "type": "promote_candidate_node", "status": "pending",
        "subject": {"node_id": "cpt_1"}, "proposal": {"to_status": "active", "node_type": "concept"},
        "context": {}})


def test_approve_records_decision_and_moves_to_approved(client, tmp_path):
    _pending_promote(tmp_path)
    resp = client.post("/reviews/rev_a/approve", json={"note": "ok"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"review_id": "rev_a", "decision_recorded": True, "status": "approved",
                    "apply_required": True}
    assert (tmp_path / "reviews" / "approved" / "rev_a.json").exists()
    assert not (tmp_path / "reviews" / "pending" / "rev_a.json").exists()
    assert (tmp_path / "reviews" / "audit_log" / "rev_a-approved.json").exists()


def test_approve_without_body_works(client, tmp_path):
    _pending_promote(tmp_path)
    assert client.post("/reviews/rev_a/approve").status_code == 200


def test_reject_records_decision(client, tmp_path):
    _pending_promote(tmp_path)
    body = client.post("/reviews/rev_a/reject").json()
    # rejected promotion owes no apply
    assert body["status"] == "rejected" and body["apply_required"] is False
    assert (tmp_path / "reviews" / "rejected" / "rev_a.json").exists()


def test_defer_keeps_pending_with_deferred_status(client, tmp_path):
    _pending_promote(tmp_path)
    body = client.post("/reviews/rev_a/defer").json()
    assert body == {"review_id": "rev_a", "decision_recorded": True, "status": "deferred",
                    "apply_required": False}
    page = tmp_path / "reviews" / "pending" / "rev_a.json"
    assert page.exists() and json.loads(page.read_text())["status"] == "deferred"
    # deferred is excluded from the default queue but reachable via ?status=deferred
    assert client.get("/reviews").json()["count"] == 0
    assert [it["review_id"] for it in client.get(
        "/reviews", params={"status": "deferred"}).json()["items"]] == ["rev_a"]


def test_deferred_item_can_then_be_approved(client, tmp_path):
    _pending_promote(tmp_path)
    client.post("/reviews/rev_a/defer")
    body = client.post("/reviews/rev_a/approve").json()
    assert body["decision_recorded"] is True and body["status"] == "approved"
    assert (tmp_path / "reviews" / "approved" / "rev_a.json").exists()


def test_same_decision_is_idempotent(client, tmp_path):
    _pending_promote(tmp_path)
    client.post("/reviews/rev_a/approve")
    again = client.post("/reviews/rev_a/approve").json()
    assert again == {"review_id": "rev_a", "decision_recorded": False, "status": "approved",
                     "apply_required": True}


def test_flipping_a_recorded_decision_is_409(client, tmp_path):
    _pending_promote(tmp_path)
    client.post("/reviews/rev_a/approve")
    assert client.post("/reviews/rev_a/reject").status_code == 409
    assert client.post("/reviews/rev_a/defer").status_code == 409


def test_decision_on_missing_review_is_404(client, tmp_path):
    assert client.post("/reviews/rev_nope/approve").status_code == 404


def test_decision_apply_required_true_for_contradiction_reject(client, tmp_path):
    _write_review(tmp_path, "pending", {
        "review_id": "rev_c", "type": "resolve_contradiction", "status": "pending",
        "subject": {"claim_a": "clm_1", "claim_b": "clm_2"}, "proposal": {}, "context": {}})
    body = client.post("/reviews/rev_c/reject").json()
    assert body["status"] == "rejected" and body["apply_required"] is True


# --- Phase 6 slice 6-3: POST /reviews/apply --------------------------------


def _approved_concept_deprecation(tmp_path):
    """A graph concept node + page + an approved deprecate_wiki_page item, at the settings paths."""
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    conn = graph.connect(gdb)
    graph.upsert_node(conn, node_id="cpt_x", node_type="concept", slug="thing", status="active")
    conn.commit()
    conn.close()
    page = tmp_path / "wiki" / "Concepts" / "thing.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text('---\ntype: concept\nconcept_id: "cpt_x"\ntitle: "Thing"\nstatus: active\n'
                    "review_status: none\naliases: []\n---\n\n# Thing\n", encoding="utf-8")
    _write_review(tmp_path, "approved", {
        "review_id": "rev_d", "type": "deprecate_wiki_page", "status": "approved",
        "subject": {"node_id": "cpt_x", "page": "Concepts/thing.md"},
        "proposal": {"to_status": "deprecated_candidate", "reason": "x"},
        "context": {"node_type": "concept"}})
    return page


def test_apply_empty_is_clean(client, tmp_path):
    body = client.post("/reviews/apply").json()
    assert body["status"] == "applied" and body["applied"] is True
    assert body["validators_ok"] is True and body["failed_validators"] == []
    assert body["summary"]["deprecations"] == {"applied": 0, "normalized": 0, "skipped": []}
    assert body["summary"]["unapplied"] == []


def test_apply_runs_deprecation_executor_and_summary(client, tmp_path):
    page = _approved_concept_deprecation(tmp_path)
    body = client.post("/reviews/apply").json()
    assert body["applied"] is True
    assert body["summary"]["deprecations"]["applied"] == 1
    fm = main_module.parse_frontmatter(page.read_text(encoding="utf-8"))
    assert fm["status"] == "deprecated_candidate" and fm["review_status"] == "approved"
    assert graph.connect(tmp_path / "db" / "graph.sqlite").execute(
        "SELECT status FROM nodes WHERE node_id='cpt_x'").fetchone()["status"] == "deprecated_candidate"


def test_apply_is_idempotent(client, tmp_path):
    _approved_concept_deprecation(tmp_path)
    client.post("/reviews/apply")
    again = client.post("/reviews/apply").json()
    assert again["summary"]["deprecations"] == {"applied": 0, "normalized": 0, "skipped": []}


def test_apply_reports_unapplied_record_only_types(client, tmp_path):
    _write_review(tmp_path, "approved", {
        "review_id": "rev_m", "type": "delete_raw_file", "status": "approved",
        "subject": {"source_id": "src_0123456789abcdef"}, "proposal": {}, "context": {}})
    body = client.post("/reviews/apply").json()
    assert {"type": "delete_raw_file", "count": 1, "reason": "no_executor_in_phase_6"} \
        in body["summary"]["unapplied"]


def test_apply_validator_failure_is_200_not_500(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_run_all_validators", lambda root: [
        {"name": "validate_projection.py", "returncode": 1, "stdout_tail": "boom", "stderr_tail": ""}])
    resp = client.post("/reviews/apply")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "validation_failed" and body["validators_ok"] is False
    assert body["failed_validators"][0]["name"] == "validate_projection.py"
    assert body["applied"] is True  # the apply still ran; never a pretend-rollback


def test_apply_no_server_path_leak(client, tmp_path):
    _approved_concept_deprecation(tmp_path)
    assert str(tmp_path) not in client.post("/reviews/apply").text


def test_apply_graph_missing_with_approved_items_is_503(client, tmp_path):
    # no graph db exists, but an approved graph-backed item is waiting -> controlled 503, not a
    # silent "applied" (and before promote_candidates would init an empty graph)
    _write_review(tmp_path, "approved", {
        "review_id": "rev_d", "type": "deprecate_wiki_page", "status": "approved",
        "subject": {"node_id": "cpt_x", "page": "Concepts/thing.md"},
        "proposal": {"to_status": "deprecated_candidate"}, "context": {"node_type": "concept"}})
    assert client.post("/reviews/apply").status_code == 503


def test_apply_index_rebuild_failure_warns(client, tmp_path, monkeypatch):
    _approved_concept_deprecation(tmp_path)
    monkeypatch.setattr(main_module, "_rebuild_index_status", lambda root: "failed")
    body = client.post("/reviews/apply").json()
    assert body["summary"]["deprecations"]["applied"] == 1   # the change happened
    assert "index_rebuild_failed" in body["warnings"]
    assert body["summary"]["index_rebuilt"] is False


def test_run_all_validators_sanitizes_root_path(tmp_path):
    # a validator that echoes its argv (the absolute root) must have the path scrubbed from the tail
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "validate_leak.py").write_text(
        "import sys\nprint('checked', sys.argv[1])\nsys.exit(1)\n", encoding="utf-8")
    results = main_module._run_all_validators(tmp_path)
    leak = next(r for r in results if r["name"] == "validate_leak.py")
    assert str(tmp_path) not in leak["stdout_tail"]
    assert "<root>" in leak["stdout_tail"]


def test_sources_exposes_lifecycle_status(client, tmp_path):
    from app.backend import manifests as _m
    _seed(tmp_path, "doc.md", "hello status\n")
    client.post("/jobs/intake-scan")
    sid = client.get("/sources").json()["sources"][0]["source_id"]
    assert client.get(f"/sources/{sid}").json()["status"] == "active"  # default when unset
    _m.set_status(tmp_path / "raw" / "manifests", sid, "archive_candidate")
    assert client.get(f"/sources/{sid}").json()["status"] == "archive_candidate"


def test_insecure_bind_override_warns(caplog):
    import logging as _logging

    from app.backend.main import assert_safe_bind
    with caplog.at_level(_logging.WARNING):
        assert_safe_bind("0.0.0.0", True)  # allowed, but must warn loudly (not silent)
    assert "KS_ALLOW_INSECURE_BIND" in caplog.text and "no auth" in caplog.text.lower()


def test_serve_entrypoint_binds_settings_host(monkeypatch):
    import app.backend.__main__ as entry
    from app.backend.config import get_settings
    captured = {}
    monkeypatch.setattr(entry.uvicorn, "run",
                        lambda target, host, port, reload=False: captured.update(host=host, port=port))
    entry.main()
    s = get_settings()
    assert captured == {"host": s.app_host, "port": s.app_port}  # bind can't drift from the guard


def test_docker_compose_uses_blessed_entrypoint_and_loopback_port():
    import pathlib
    text = (pathlib.Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    assert "python -m app.backend" in text             # blessed entrypoint
    assert "uvicorn app.backend.main:app" not in text  # never direct uvicorn
    for line in text.splitlines():                     # published API port stays loopback-only
        if "18000:18000" in line:
            assert "127.0.0.1:18000:18000" in line, line


def test_dockerfile_uses_blessed_entrypoint():
    # The image default must also route through the guard: the old CMD ran uvicorn --host 0.0.0.0
    # directly, so a plain `docker run` (no compose override) bound all interfaces while the
    # import-time assert_safe_bind — which checks APP_HOST, default loopback — passed. With the
    # blessed CMD a bare run binds loopback inside the container (fail-closed, unreachable from the
    # host); compose remains the explicit reachability exception (guard above).
    import pathlib
    text = (pathlib.Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    assert 'CMD ["python", "-m", "app.backend"]' in text  # blessed entrypoint
    assert "app.backend.main:app" not in text             # never a direct uvicorn bind


def test_sources_quarantines_invalid_manifests(client, tmp_path):
    md = tmp_path / "raw" / "manifests"
    md.mkdir(parents=True, exist_ok=True)
    # a canonical id in a wrongly-named file is quarantined, never listed (count only, no id echoed)
    (md / "wrongname.json").write_text('{"source_id": "src_0123456789abcdef"}', encoding="utf-8")
    body = client.get("/sources").json()
    assert body["count"] == 0 and body["manifests_skipped_invalid"] == 1


# --------------------------------------------------------------------------- startup warmup (ADR-0053)
# These exercise the FastAPI lifespan directly (context-manager form), overriding the autouse
# conftest no-op with a spy/raiser to prove the wiring the global no-op otherwise hides.


def test_lifespan_invokes_embedding_warmup(monkeypatch):
    calls = []
    monkeypatch.setattr(main_module.embeddings, "warmup_provider", lambda s: calls.append(s) or None)
    with TestClient(main_module.app):
        pass
    assert len(calls) == 1  # lifespan called warmup_provider once at startup


def test_lifespan_failfast_aborts_startup(monkeypatch):
    def boom(_settings):
        raise main_module.embeddings.EmbeddingError("cuda unavailable")

    monkeypatch.setattr(main_module.embeddings, "warmup_provider", boom)
    with pytest.raises(main_module.embeddings.EmbeddingError):
        with TestClient(main_module.app):
            pass  # a warmup failure must abort app startup (fail-fast), not be swallowed
