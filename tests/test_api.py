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


def test_search_auto_stays_keyword_only_with_vector_configured(client, tmp_path, monkeypatch):
    _configure_vector(tmp_path, client, monkeypatch)
    body = client.get("/search?q=synergy%20capture&mode=auto").json()
    # 4d: auto never runs vector (RRF/auto-blend is 4e).
    assert "vector" not in body["retrieval_path"]


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
