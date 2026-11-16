"""ADR-0074 change 7a-1 — manifest visibility enforcement in retrieval.

The manifest's lifecycle status is the visibility authority; the keyword index's status copy is a
cheap pre-filter only. Both must allow a document (Q1), for keyword evidence, vector evidence,
Source navigation hits and the graph group's Source nodes (Q2); a malformed status refuses; the
exclusion is silent and happens before the install-journal check (Q6); surviving Source results
report the manifest's status.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.backend import graph, graph_read, keyword_index, manifests, search  # noqa: E402
from app.backend import extraction_install as xi  # noqa: E402
from app.backend import main as main_module  # noqa: E402
from app.backend.config import get_settings  # noqa: E402
from app.backend.policy import RetrievalPolicy  # noqa: E402

VIS = "src_00000000000000a1"      # always visible
HID = "src_00000000000000b2"      # the subject
OTHER = "src_00000000000000c3"
ITEM = "itm_000000000000000d"
ITEM2 = "itm_000000000000000e"
Q = "lighthouse keeper logbook"
DEFAULT = search.RETENTION_DEFAULT_STATUSES


def _doc(root: Path, sid: str, text: str, *, page_status: str = "active") -> None:
    rec = {"chunk_id": f"{sid}::0000", "source_id": sid, "ordinal": 0, "kind": "prose",
           "heading_path": [], "section": None, "text": text, "char_start": 0,
           "char_end": len(text), "page": 1, "page_end": 1, "table_reference": None,
           "sheet_reference": None}
    for sub in ("chunks", "markdown"):
        (root / "normalized" / sub).mkdir(parents=True, exist_ok=True)
    (root / "normalized" / "chunks" / f"{sid}.jsonl").write_text(json.dumps(rec) + "\n",
                                                                encoding="utf-8")
    (root / "normalized" / "markdown" / f"{sid}.md").write_text(text, encoding="utf-8")
    (root / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    (root / "raw" / "manifests" / f"{sid}.json").write_text(json.dumps({"source_id": sid}),
                                                            encoding="utf-8")
    _page(root, sid, page_status)


def _page(root: Path, sid: str, status: str) -> None:
    sp = root / "wiki" / "Sources" / f"{sid}.md"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(f"---\ntype: source\nsource_id: {sid}\ntitle: Lighthouse {sid[-2:]}\n"
                  f"status: {status}\nlanguage: en\n---\n\n# Lighthouse keeper logbook\n\n"
                  "> [!summary]\n> lighthouse keeper logbook\n", encoding="utf-8")


def _manifest_field(root: Path, sid: str, **fields) -> None:
    """Write manifest fields directly (bypassing set_status validation: malformed values)."""
    path = root / "raw" / "manifests" / f"{sid}.json"
    m = json.loads(path.read_text())
    m.update(fields)
    path.write_text(json.dumps(m), encoding="utf-8")


def _vault(root: Path, *sids: str, index_status: str = "active") -> Path:
    for sid in sids:
        _doc(root, sid, f"The {Q} records storms for {sid}.",
             page_status=index_status if sid == HID else "active")
    keyword_index.reindex(root, force=True)
    return root


def _search(root: Path, *, mode: str = "keyword", statuses=DEFAULT, node_statuses=DEFAULT,
            vector=None, graph_conn=None, q: str = Q) -> dict:
    conn = keyword_index.connect(root / "indexes" / "keyword" / "keyword.sqlite")
    try:
        return search.run_search(
            q=q, mode=mode, manifests_dir=root / "raw" / "manifests",
            normalized_dir=root / "normalized", keyword_conn=conn, graph_conn=graph_conn,
            policy=RetrievalPolicy(), source_statuses=statuses, node_statuses=node_statuses,
            vector_search=vector)
    finally:
        conn.close()


def _ev(result: dict) -> set[str]:
    return {e["source_id"] for e in result["evidence"]}


def _nav(result: dict) -> set[str]:
    return {n["node_id"] for n in result["navigation"] if n["page_type"] == "source"}


# --- Q1: both must allow it ------------------------------------------------------------------


@pytest.mark.parametrize("status", ["hidden", "archived", "archive_candidate", "deleted"])
def test_a_hide_or_archive_whose_reindex_failed_still_suppresses(tmp_path, status):
    root = _vault(tmp_path, VIS, HID)
    before = _search(root, mode="navigation")
    assert HID in _nav(before)
    assert _ev(_search(root)) == {VIS, HID}
    manifests.set_status(root / "raw" / "manifests", HID, status)    # no reindex: stale copy
    assert _ev(_search(root)) == {VIS}
    assert _nav(_search(root, mode="navigation", node_statuses=DEFAULT)) == {VIS}


def test_an_unhide_whose_reindex_failed_stays_suppressed_until_a_reindex(tmp_path):
    # The intentional, temporary recall loss: the stale index copy still says hidden.
    root = _vault(tmp_path, VIS, HID, index_status="hidden")
    manifests.set_status(root / "raw" / "manifests", HID, "hidden")
    assert _ev(_search(root)) == {VIS}
    manifests.set_status(root / "raw" / "manifests", HID, "active")  # unhide, reindex failed
    assert _ev(_search(root)) == {VIS}                                # the index pre-filter
    assert _ev(_search(root, statuses=("hidden",))) == set()          # the manifest refuses
    _page(root, HID, "active")
    keyword_index.reindex(root, force=True)
    assert _ev(_search(root)) == {VIS, HID}                           # recall restored


def test_explicit_status_filters_need_both_and_report_the_manifest_status(tmp_path):
    root = _vault(tmp_path, VIS, HID)
    manifests.set_status(root / "raw" / "manifests", HID, "hidden")  # index copy: active
    assert _ev(_search(root, statuses=("hidden",))) == set()          # stale copy: a miss
    both = _search(root, statuses=("active", "hidden"))
    assert _ev(both) == {VIS, HID}
    status = {e["source_id"]: e["source_status"] for e in both["evidence"]}
    assert status == {VIS: "active", HID: "hidden"}                   # the authority, not "active"
    nav = _search(root, mode="navigation", node_statuses=("active", "hidden"))
    assert {n["node_id"]: n["status"] for n in nav["navigation"]
            if n["page_type"] == "source"} == {VIS: "active", HID: "hidden"}


def test_surviving_hits_report_the_manifest_status_when_both_allow(tmp_path):
    root = _vault(tmp_path, VIS, HID)
    manifests.set_status(root / "raw" / "manifests", HID, "deprecated_candidate")
    out = _search(root)
    assert {e["source_id"]: e["source_status"] for e in out["evidence"]} == {
        VIS: "active", HID: "deprecated_candidate"}


@pytest.mark.parametrize("value", ["Hidden", "", None, 5, ["active"], "visible"])
def test_a_malformed_status_refuses_the_document_under_any_filter(tmp_path, value):
    root = _vault(tmp_path, VIS, HID)
    _manifest_field(root, HID, status=value)
    every = tuple(manifests.SOURCE_STATUSES)
    assert _ev(_search(root, statuses=every)) == {VIS}
    assert _nav(_search(root, mode="navigation", node_statuses=every)) == {VIS}
    assert manifests.visibility_status({"source_id": HID, "status": value}) is None


def test_an_absent_status_keeps_its_documented_default(tmp_path):
    root = _vault(tmp_path, HID)
    assert "status" not in json.loads((root / "raw" / "manifests" / f"{HID}.json").read_text())
    assert _ev(_search(root)) == {HID}
    assert _ev(_search(root, statuses=("hidden",))) == set()
    assert manifests.visibility_status({}) == "active"


def _vector_rows():
    def row(sid, dist):
        return {"_distance": dist, "source_id": sid, "chunk_id": f"{sid}::0000", "ordinal": 0,
                "kind": "prose", "heading_path": "[]", "section": None, "char_start": 0,
                "char_end": 10, "page": 1, "page_end": 1, "table_reference": None,
                "sheet_reference": None, "text": f"The {Q}."}
    return [row(VIS, 0.1), row(HID, 0.05)]


def test_vector_evidence_is_gated_by_the_manifest_status(tmp_path):
    root = _vault(tmp_path, VIS, HID)
    vec = lambda *, limit: _vector_rows()  # noqa: E731
    assert _ev(_search(root, mode="vector", vector=vec)) == {VIS, HID}
    manifests.set_status(root / "raw" / "manifests", HID, "hidden")
    assert _ev(_search(root, mode="vector", vector=vec)) == {VIS}
    both = _search(root, mode="vector", vector=vec, statuses=("active", "hidden"))
    assert {e["source_id"]: e["source_status"] for e in both["evidence"]} == {
        VIS: "active", HID: "hidden"}
    auto = _search(root, mode="auto", vector=vec)                     # both channels fused
    assert HID not in _ev(auto) and VIS in _ev(auto)


# --- Q2: the graph group's Source nodes ------------------------------------------------------


def _graph(root: Path, *, mirror: str = "active") -> Path:
    gdb = root / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    conn = graph.connect(gdb)
    graph.upsert_node(conn, node_id=ITEM, node_type="item", item_type="method_technique",
                      slug="keeper", status="active")
    graph.upsert_node(conn, node_id=ITEM2, node_type="item", item_type="method_technique",
                      slug="storms", status="active")
    graph.upsert_node(conn, node_id=HID, node_type="source", slug=HID, status=mirror)
    graph.upsert_node(conn, node_id=VIS, node_type="source", slug=VIS, status="active")
    for src, dst in ((ITEM, HID), (ITEM, VIS), (ITEM2, HID)):      # ITEM2 reachable only via HID
        graph.upsert_assertion(conn, src_id=src, dst_id=dst, edge_type="mentions",
                               asserted_by="deterministic", status="active")
    conn.commit()
    conn.close()
    return gdb


def _graph_search(root: Path, monkeypatch, *, node_statuses=DEFAULT) -> dict:
    monkeypatch.setattr(search, "search_navigation", lambda *a, **kw: [
        {"page_type": "item", "node_id": ITEM, "path": "Items/keeper.md"}])
    conn = graph.connect(root / "db" / "graph.sqlite")
    try:
        policy = RetrievalPolicy()
        return search.run_search(
            q=Q, mode="graph", manifests_dir=root / "raw" / "manifests",
            normalized_dir=root / "normalized",
            keyword_conn=keyword_index.connect(root / "indexes" / "keyword" / "keyword.sqlite"),
            graph_conn=conn, policy=policy, node_statuses=node_statuses)["graph"]
    finally:
        conn.close()


def _ids(g: dict) -> set[str]:
    return {n["node_id"] for n in g["nodes"]}


def test_a_stale_graph_mirror_cannot_surface_a_hidden_source(tmp_path, monkeypatch):
    root = _vault(tmp_path, VIS, HID)
    _graph(root, mirror="active")
    before = _graph_search(root, monkeypatch)
    assert {HID, ITEM2} <= _ids(before)                               # depth 2 reaches ITEM2
    manifests.set_status(root / "raw" / "manifests", HID, "hidden")  # graph mirror stays active
    g = _graph_search(root, monkeypatch)
    assert HID not in _ids(g) and {ITEM, VIS} <= _ids(g)
    assert ITEM2 not in _ids(g)                                       # never traversed through it
    assert all(HID not in (e["src_id"], e["dst_id"]) for e in g["edges"])   # no incident edge
    assert {(e["src_id"], e["dst_id"]) for e in g["edges"]} == {(ITEM, VIS)}
    explicit = _graph_search(root, monkeypatch, node_statuses=("active", "hidden"))
    assert HID in _ids(explicit)
    assert {n["node_id"]: n["status"] for n in explicit["nodes"]}[HID] == "hidden"  # manifest's
    assert HID not in _ids(_graph_search(root, monkeypatch, node_statuses=("hidden",)))  # stale


def test_graph_source_statuses_come_from_the_manifest_and_other_families_are_untouched(
        tmp_path, monkeypatch):
    root = _vault(tmp_path, VIS, HID)
    _graph(root, mirror="active")
    manifests.set_status(root / "raw" / "manifests", HID, "deprecated_candidate")
    g = _graph_search(root, monkeypatch)
    status = {n["node_id"]: n["status"] for n in g["nodes"]}
    assert status[HID] == "deprecated_candidate" and status[ITEM] == "active"


def test_the_raw_graph_inspection_api_is_unchanged(tmp_path, monkeypatch):
    root = _vault(tmp_path, VIS, HID)
    _graph(root, mirror="active")
    manifests.set_status(root / "raw" / "manifests", HID, "hidden")
    conn = graph.connect(root / "db" / "graph.sqlite")
    try:
        assert graph.get_node(conn, HID)["status"] == "active"        # the mirror, as stored
        view = graph_read.node_view(conn, ITEM)
        assert HID in json.dumps(view)                                 # still inspectable
    finally:
        conn.close()
    settings = get_settings(root)
    monkeypatch.setattr(main_module, "settings", settings)
    client = TestClient(main_module.app)
    r = client.get(f"/graph/neighborhood/{ITEM}")
    assert r.status_code == 200 and HID in r.text
    node = client.get(f"/graph/node/{HID}")
    assert node.status_code == 200 and node.json()["node"]["status"] == "active"


def test_the_gate_never_reuses_a_decision_across_status_sets(tmp_path):
    root = _vault(tmp_path, HID)
    manifests.set_status(root / "raw" / "manifests", HID, "hidden")
    gate = search._ManifestGate(root / "raw" / "manifests", root / "normalized")
    assert gate.allows(HID, DEFAULT) is False
    assert gate.allows(HID, ("hidden",)) is True
    assert gate.allows(HID, DEFAULT) is False
    assert gate.blocked == set() and gate.install_blocked == set()


# --- Q6: silent exclusion --------------------------------------------------------------------


def _break_journal(root: Path, sid: str) -> None:
    path = xi.Layout(root / "normalized").journal(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert not xi.usable(root / "normalized", sid)


def _signal(result: dict) -> dict:
    """The diagnostic surface — notes, counts, flags and the surviving hits (not their scores).
    On these small vaults it matches; with bounded candidate pools a stale hidden candidate can
    still shift ranking or truncation, which "silent" (no exclusion diagnostic) does not exclude."""
    return {"evidence": sorted(_ev(result)), "navigation": sorted(_nav(result)),
            "counts": result["counts"], "notes": result["notes"],
            "no_results": result["no_results"], "truncated": result["truncated"],
            "retrieval_path": result["retrieval_path"]}


@pytest.mark.parametrize("broken_journal", [False, True])
@pytest.mark.parametrize("mode", ["keyword", "navigation", "auto"])
def test_a_matching_hidden_document_adds_no_diagnostic_signal(tmp_path, broken_journal, mode):
    without = _vault(tmp_path / "a", VIS)
    with_hidden = _vault(tmp_path / "b", VIS, HID)
    manifests.set_status(with_hidden / "raw" / "manifests", HID, "hidden")   # stale index copy
    if broken_journal:
        _break_journal(with_hidden, HID)
    assert _signal(_search(with_hidden, mode=mode)) == _signal(_search(without, mode=mode))


def test_a_visible_document_with_a_broken_journal_is_still_reported(tmp_path):
    # The contrast: the install note is a real signal — it is the status check that silences it.
    root = _vault(tmp_path, VIS, HID)
    _break_journal(root, HID)
    out = _search(root)
    assert _ev(out) == {VIS}
    assert any("extraction install incomplete" in n for n in out["notes"])


def test_a_hidden_document_with_a_broken_journal_never_reaches_the_journal_check(tmp_path,
                                                                                 monkeypatch):
    root = _vault(tmp_path, VIS, HID)
    manifests.set_status(root / "raw" / "manifests", HID, "hidden")
    checked = []
    real = xi.usable
    monkeypatch.setattr(search.xi, "usable", lambda n, sid: checked.append(sid) or real(n, sid))
    _search(root)
    assert checked == [VIS]


# --- the API surfaces --------------------------------------------------------------------------


class _CiteAll:
    def provider_available(self, model_ref):
        return True

    def resolve_run_model(self, chain):
        return chain.split(",")[0].strip(), True

    def chain_available(self, chain):
        return True

    def parse(self, messages, schema, model_ref, **kwargs):
        pack = json.loads(messages[-1]["content"].split("EVIDENCE:\n", 1)[1])
        return {"claims": [{"text": f"Grounded claim for {e['evidence_id']}.",
                            "evidence_ids": [e["evidence_id"]]} for e in pack]}


def test_search_and_query_suppress_a_hide_whose_reindex_failed(tmp_path, monkeypatch):
    root = _vault(tmp_path, VIS, HID)
    settings = get_settings(root)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(main_module, "_query_client", lambda: _CiteAll())
    client = TestClient(main_module.app)
    cited = lambda r: {c["source_id"] for c in r["citations"]}  # noqa: E731
    assert cited(client.post("/query", json={"question": Q, "mode": "keyword"}).json()) == {
        VIS, HID}
    manifests.set_status(root / "raw" / "manifests", HID, "hidden")
    s = client.get("/search", params={"q": Q, "mode": "keyword"}).json()
    assert {e["source_id"] for e in s["evidence"]} == {VIS}
    assert cited(client.post("/query", json={"question": Q, "mode": "keyword"}).json()) == {VIS}
    explicit = client.post("/query", json={"question": Q, "mode": "keyword",
                                           "source_status": "active,hidden"}).json()
    assert cited(explicit) == {VIS, HID}
