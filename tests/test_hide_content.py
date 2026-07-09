"""ADR-0043 hide_content governance executor: source visibility via the reversible `hidden` status.

Covers the shared source-status executor (active->hidden), scope guards/idempotency/reject, the status
vocabulary acceptance, the A1 projector, end-to-end apply + ADR-0040 dry-run, and the visibility
contract (hidden excluded from default retrieval/search; explicit source_status=hidden surfaces it).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.backend import graph, keyword_index, manifests, review_read, search
from app.backend import main as main_module
from app.backend.config import get_settings
from app.workers import retention, wiki
from app.workers.wiki_render import parse_frontmatter

SID = "src_000000000000000a"
OLD = "2020-01-01T00:00:00+00:00"


# --- helpers ---------------------------------------------------------------


def _write_manifest(tmp_path: Path, sid: str, *, status: str = "active") -> None:
    md = tmp_path / "raw" / "manifests"
    md.mkdir(parents=True, exist_ok=True)
    rel = f"raw/inbox/{sid}.md"
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text("raw bytes", encoding="utf-8")
    m = {"source_id": sid, "sha256": sid + "0" * 8, "relative_raw_path": rel, "file_extension": ".md",
         "chunk_count": 1, "ingestion_status": "extracted",
         "normalized": {"markdown_path": f"normalized/markdown/{sid}.md"},
         "created_at": OLD, "discovered_at": OLD, "modified_at": OLD,
         "retention_class": "permanent", "occurrences": [{"relative_path": rel}]}
    if status != "active":
        m["status"] = status
    (md / f"{sid}.json").write_text(json.dumps(m), encoding="utf-8")
    norm = tmp_path / "normalized" / "markdown" / f"{sid}.md"
    norm.parent.mkdir(parents=True, exist_ok=True)
    norm.write_text(f"# {sid}\n\nReal prose body for the source.\n", encoding="utf-8")


def _rendered_source(tmp_path: Path, sid: str = SID) -> Path:
    shutil.copytree(ROOT / "templates", tmp_path / "templates", dirs_exist_ok=True)
    _write_manifest(tmp_path, sid)
    wiki.generate_wiki(tmp_path, source_ids=[sid], rebuild_index=False, record_job=False)
    return tmp_path / "wiki" / "Sources" / f"{sid}.md"


def _graph_source_node(tmp_path: Path, sid: str = SID, status: str = "active") -> Path:
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    conn = graph.connect(gdb)
    graph.upsert_node(conn, node_id=sid, node_type="source", slug=sid, status=status)
    conn.commit()
    conn.close()
    return gdb


def _approve_hide(tmp_path: Path, sid: str = SID, rid: str = "rev_hide") -> None:
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": "hide_content", "status": "approved",
        "subject": {"source_id": sid}, "proposal": {"to_status": "hidden", "reason": "sensitive"},
        "context": {}}), encoding="utf-8")


def _apply(tmp_path: Path) -> dict:
    return retention.apply_hidden_sources(
        tmp_path, manifests_dir=tmp_path / "raw" / "manifests", reviews_dir=tmp_path / "reviews",
        wiki_dir=tmp_path / "wiki", graph_db=tmp_path / "db" / "graph.sqlite")


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


# --- executor --------------------------------------------------------------


def test_apply_hide_flips_manifest_page_graph_raw_untouched(tmp_path):
    page = _rendered_source(tmp_path)
    _graph_source_node(tmp_path)
    _approve_hide(tmp_path)
    raw_before = (tmp_path / "raw" / "inbox" / f"{SID}.md").read_bytes()

    res = _apply(tmp_path)
    assert res["applied"] == 1 and res["skipped"] == [] and res["changed_pages"] == [f"Sources/{SID}.md"]
    md = tmp_path / "raw" / "manifests"
    assert manifests.get_status(manifests.load_manifest(md, SID)) == "hidden"   # manifest authority
    assert parse_frontmatter(page.read_text())["status"] == "hidden"           # page mirror
    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    assert graph.get_node(conn, SID)["status"] == "hidden"                     # graph mirror
    conn.close()
    assert (tmp_path / "raw" / "inbox" / f"{SID}.md").read_bytes() == raw_before  # raw untouched


def test_apply_hide_idempotent_noop(tmp_path):
    _rendered_source(tmp_path)
    _approve_hide(tmp_path)
    assert _apply(tmp_path)["applied"] == 1
    res2 = _apply(tmp_path)
    assert res2["applied"] == 0 and res2["changed_pages"] == []   # already hidden -> no-op


def test_apply_hide_graph_absent_still_hides(tmp_path):
    _rendered_source(tmp_path)            # no graph db created
    _approve_hide(tmp_path)
    res = _apply(tmp_path)
    # manifest+page are the authority; graph absent -> applied but NO mirror -> graph_changed False (honest)
    assert res["applied"] == 1 and res["graph_changed"] is False
    md = tmp_path / "raw" / "manifests"
    assert manifests.get_status(manifests.load_manifest(md, SID)) == "hidden"


def test_graph_changed_true_only_when_mirror_written(tmp_path):
    # with a graph source node present, the mirror IS written -> graph_changed True
    _rendered_source(tmp_path)
    _graph_source_node(tmp_path)
    _approve_hide(tmp_path)
    assert _apply(tmp_path)["graph_changed"] is True


@pytest.mark.parametrize("setup,reason", [
    ("invalid_id", "invalid_source_id"),
    ("missing", "source_missing"),
    ("bad_to_status", "unexpected_to_status"),
])
def test_apply_hide_scope_guards(tmp_path, setup, reason):
    (tmp_path / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    item = {"review_id": "rev_x", "type": "hide_content", "status": "approved",
            "subject": {"source_id": SID}, "proposal": {"to_status": "hidden"}, "context": {}}
    if setup == "invalid_id":
        item["subject"]["source_id"] = "../escape"
    elif setup == "bad_to_status":
        item["proposal"]["to_status"] = "deleted"
    if setup != "missing":  # "missing" = approved item but no manifest on disk
        if setup == "invalid_id":
            pass
        else:
            _write_manifest(tmp_path, SID)
    (d / "rev_x.json").write_text(json.dumps(item), encoding="utf-8")
    res = _apply(tmp_path)
    assert res["applied"] == 0
    assert res["skipped"] == [{"review_id": "rev_x", "reason": reason}]


def test_already_non_active_source_is_noop_skip(tmp_path):
    # an already archive_candidate source can't be hidden in v1 (active-only) -> silent no-op, not error
    _write_manifest(tmp_path, SID, status="archive_candidate")
    _approve_hide(tmp_path)
    res = _apply(tmp_path)
    assert res["applied"] == 0 and res["skipped"] == []     # not-active -> no-op continue
    md = tmp_path / "raw" / "manifests"
    assert manifests.get_status(manifests.load_manifest(md, SID)) == "archive_candidate"  # unchanged


def test_rejected_hide_is_noop(tmp_path):
    _rendered_source(tmp_path)
    d = tmp_path / "reviews" / "rejected"   # rejected/, not approved/
    d.mkdir(parents=True, exist_ok=True)
    (d / "rev_r.json").write_text(json.dumps({
        "review_id": "rev_r", "type": "hide_content", "status": "rejected",
        "subject": {"source_id": SID}, "proposal": {"to_status": "hidden"}, "context": {}}),
        encoding="utf-8")
    res = _apply(tmp_path)
    assert res["applied"] == 0   # executor only scans approved/
    md = tmp_path / "raw" / "manifests"
    assert manifests.get_status(manifests.load_manifest(md, SID)) == "active"  # stays active


# --- status vocabulary -----------------------------------------------------


def test_set_status_hidden_roundtrip(tmp_path):
    _write_manifest(tmp_path, SID)
    md = tmp_path / "raw" / "manifests"
    manifests.set_status(md, SID, "hidden")
    assert manifests.get_status(manifests.load_manifest(md, SID)) == "hidden"
    manifests.set_status(md, SID, "active")  # reversible
    assert manifests.get_status(manifests.load_manifest(md, SID)) == "active"


def test_validate_wiki_accepts_hidden(tmp_path):
    page = _rendered_source(tmp_path)
    manifests.set_status(tmp_path / "raw" / "manifests", SID, "hidden")
    wiki.generate_wiki(tmp_path, source_ids=[SID], rebuild_index=False, record_job=False)
    assert parse_frontmatter(page.read_text())["status"] == "hidden"
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_wiki.py"), str(tmp_path)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout


def test_hidden_excluded_by_default_status_filter():
    # retrieval/nav lever: hidden is NOT in the default set, but is a parseable explicit status.
    assert search._status_allowed("hidden", search.RETENTION_DEFAULT_STATUSES) is False
    assert search._status_allowed("active", search.RETENTION_DEFAULT_STATUSES) is True
    assert search.parse_statuses("hidden", graph.NODE_STATUSES, search.RETENTION_DEFAULT_STATUSES) \
        == ("hidden",)


# --- A1 projector ----------------------------------------------------------


def test_preview_hide_content(tmp_path):
    _write_manifest(tmp_path, SID, status="active")
    item = {"review_id": "rev_hide", "type": "hide_content", "status": "approved",
            "subject": {"source_id": SID}, "proposal": {"to_status": "hidden", "reason": "x"},
            "context": {}}
    out = review_read.preview_hide_content(item, gconn=None, wiki_dir=tmp_path / "wiki",
                                           manifests_dir=tmp_path / "raw" / "manifests")
    assert out["apply"]["supported"] is True
    assert out["proposed_status"] == "hidden" and out["node_ids"] == [SID]
    assert out["affected_paths"] == [f"Sources/{SID}.md"]
    assert out["current_status"] == "active" and out["apply"]["effect_status"] == "pending_apply"


def test_preview_hide_invalid_subject(tmp_path):
    item = {"review_id": "rev_hide", "type": "hide_content", "status": "approved",
            "subject": {"source_id": "not-canonical"}, "proposal": {"to_status": "hidden"}, "context": {}}
    out = review_read.preview_hide_content(item, gconn=None, wiki_dir=tmp_path / "wiki",
                                           manifests_dir=tmp_path / "raw" / "manifests")
    assert out["invalid_subject"] is True and out["apply"]["supported"] is False
    assert out["affected_paths"] == []


@pytest.mark.parametrize("ms,effect,warn", [
    ("hidden", "effected", False),                 # already hidden -> effected, no warning
    ("archive_candidate", "pending_apply", True),  # non-active -> executor no-ops -> warned
    ("deprecated_candidate", "pending_apply", True),
])
def test_preview_hide_reflects_current_status(tmp_path, ms, effect, warn):
    _write_manifest(tmp_path, SID, status=ms)
    item = {"review_id": "rev_hide", "type": "hide_content", "status": "approved",
            "subject": {"source_id": SID}, "proposal": {"to_status": "hidden"}, "context": {}}
    out = review_read.preview_hide_content(item, gconn=None, wiki_dir=tmp_path / "wiki",
                                           manifests_dir=tmp_path / "raw" / "manifests")
    assert out["current_status"] == ms and out["apply"]["effect_status"] == effect
    assert ("source_not_active" in out["apply"]["warnings"]) is warn


# --- end-to-end via the API (+ ADR-0040 dry-run) ---------------------------


def test_api_apply_hides_source_and_summary(client, tmp_path):
    _rendered_source(tmp_path)
    _approve_hide(tmp_path)
    body = client.post("/reviews/apply").json()
    assert body["summary"]["hidden"]["applied"] == 1
    md = tmp_path / "raw" / "manifests"
    assert manifests.get_status(manifests.load_manifest(md, SID)) == "hidden"


def test_dry_run_previews_hide_and_leaves_live_unchanged(client, tmp_path):
    _rendered_source(tmp_path)
    _graph_source_node(tmp_path)
    _approve_hide(tmp_path)
    dry = client.post("/reviews/apply/dry-run").json()
    assert dry["status"] == "ok"
    assert {"source_id": SID, "field": "status", "from": "active", "to": "hidden"} in dry["diff"]["manifests"]
    md = tmp_path / "raw" / "manifests"
    assert manifests.get_status(manifests.load_manifest(md, SID)) == "active"  # live unchanged


def test_html_apply_result_shows_hidden_summary(client, tmp_path):
    _rendered_source(tmp_path)
    _approve_hide(tmp_path)
    assert "hidden" in client.post("/ui/reviews/apply").text   # operator UI surfaces the hide result


def test_hide_with_reindex_failure_is_not_clean(client, tmp_path, monkeypatch):
    # ADR-0043 stricter posture: a hide applied while the keyword reindex fails is non-clean (the hidden
    # source may still surface via the stale index), surfaced as validation_failed + a warning.
    _rendered_source(tmp_path)
    _approve_hide(tmp_path)

    def boom(root):
        raise RuntimeError("reindex blew up")
    monkeypatch.setattr(main_module.retention, "reindex_keyword", boom)
    body = client.post("/reviews/apply").json()
    assert body["summary"]["hidden"]["applied"] == 1                 # the mutation still happened
    assert body["status"] == "validation_failed"
    assert "hide_retrieval_suppression_not_guaranteed" in body["warnings"]
    assert manifests.get_status(manifests.load_manifest(
        tmp_path / "raw" / "manifests", SID)) == "hidden"            # manifest authority is correct


# --- visibility: search exclusion (full round-trip) ------------------------


def _write_searchable_source(tmp_path: Path, sid: str, text: str) -> None:
    rec = {"chunk_id": f"{sid}::0000", "source_id": sid, "ordinal": 0, "kind": "prose",
           "heading_path": [], "section": None, "text": text, "char_start": 0, "char_end": len(text),
           "page": 1, "page_end": 1, "table_reference": None, "sheet_reference": None}
    (tmp_path / "normalized" / "chunks").mkdir(parents=True, exist_ok=True)
    (tmp_path / "normalized" / "chunks" / f"{sid}.jsonl").write_text(json.dumps(rec) + "\n",
                                                                     encoding="utf-8")
    (tmp_path / "normalized" / "markdown").mkdir(parents=True, exist_ok=True)
    (tmp_path / "normalized" / "markdown" / f"{sid}.md").write_text(text, encoding="utf-8")
    (tmp_path / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "raw" / "manifests" / f"{sid}.json").write_text(
        json.dumps({"source_id": sid}), encoding="utf-8")
    sp = tmp_path / "wiki" / "Sources" / f"{sid}.md"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(f"---\ntype: source\nsource_id: {sid}\ntitle: Doc\nstatus: active\nlanguage: en\n"
                  "---\n\n# Doc\n\n> [!summary]\n> doc\n", encoding="utf-8")


def test_hidden_source_excluded_from_default_search_findable_explicit(client, tmp_path):
    _write_searchable_source(tmp_path, SID, "summary first navigation reads the index before pages")
    keyword_index.reindex(tmp_path, force=True)
    cited = lambda r: {e["source_id"] for e in r.json()["evidence"]}  # noqa: E731
    assert SID in cited(client.get("/search", params={"q": "summary navigation index"}))  # baseline

    # hide it: manifest status -> page mirror -> reindex
    manifests.set_status(tmp_path / "raw" / "manifests", SID, "hidden")
    (tmp_path / "wiki" / "Sources" / f"{SID}.md").write_text(
        f"---\ntype: source\nsource_id: {SID}\ntitle: Doc\nstatus: hidden\nlanguage: en\n"
        "---\n\n# Doc\n\n> [!summary]\n> doc\n", encoding="utf-8")
    keyword_index.reindex(tmp_path, force=True)

    r2 = client.get("/search", params={"q": "summary navigation index"}).json()
    assert SID not in {e["source_id"] for e in r2["evidence"]}          # default excludes evidence
    assert SID not in {n.get("node_id") for n in r2["navigation"]}      # ...and the navigation group
    explicit = client.get("/search", params={"q": "summary navigation index", "source_status": "hidden"})
    assert SID in cited(explicit)   # explicit source_status=hidden surfaces it


class _CiteAllClient:
    def provider_available(self, model_ref):
        return True

    def parse(self, messages, schema, model_ref, **kwargs):
        pack = json.loads(messages[-1]["content"].split("EVIDENCE:\n", 1)[1])
        return {"claims": [{"text": f"Grounded claim for {e['evidence_id']}.",
                            "evidence_ids": [e["evidence_id"]]} for e in pack]}


def test_hidden_source_not_in_query_answer_by_default(client, tmp_path, monkeypatch):
    # /query evidence flows through the same source-status filter: a hidden (only) source -> no evidence
    # -> abstain by default; an explicit source_status=hidden surfaces it again.
    monkeypatch.setattr(main_module, "_query_client", lambda: _CiteAllClient())
    _write_searchable_source(tmp_path, SID, "summary first navigation reads the index before pages")
    manifests.set_status(tmp_path / "raw" / "manifests", SID, "hidden")
    (tmp_path / "wiki" / "Sources" / f"{SID}.md").write_text(
        f"---\ntype: source\nsource_id: {SID}\ntitle: Doc\nstatus: hidden\nlanguage: en\n"
        "---\n\n# Doc\n\n> [!summary]\n> doc\n", encoding="utf-8")
    keyword_index.reindex(tmp_path, force=True)

    default = client.post("/query", json={"question": "summary navigation index"}).json()
    assert default["abstained"] is True and default["citations"] == []   # hidden -> no evidence
    explicit = client.post(
        "/query", json={"question": "summary navigation index", "source_status": "hidden"}).json()
    assert SID in {c["source_id"] for c in explicit["citations"]}        # explicit surfaces it


def test_search_graph_channel_excludes_hidden_node(tmp_path):
    # ADR-0043 decision 1: the /search graph channel (search_subgraph) is node-status-filtered, so a
    # hidden source adjacent is excluded by default; the RAW graph inspection still sees it (get_node).
    from app.backend import graph_read
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    conn = graph.connect(gdb)
    itm = "itm_cccccccccccccccc"
    graph.upsert_node(conn, node_id=itm, node_type="item", item_type="method_technique",
                      slug="topic", status="active")
    graph.upsert_node(conn, node_id=SID, node_type="source", slug=SID, status="hidden")
    graph.upsert_assertion(conn, src_id=itm, dst_id=SID, edge_type="mentions",
                           asserted_by="deterministic", status="active")
    conn.commit()
    default = graph_read.search_subgraph(conn, [itm], depth=1,
                                         node_statuses=search.RETENTION_DEFAULT_STATUSES,
                                         node_cap=50, edge_cap=50)
    assert SID not in {n["node_id"] for n in default["nodes"]}        # graph channel excludes hidden
    incl = graph_read.search_subgraph(conn, [itm], depth=1, node_statuses=("active", "hidden"),
                                      node_cap=50, edge_cap=50)
    assert SID in {n["node_id"] for n in incl["nodes"]}              # explicit include surfaces it
    assert graph.get_node(conn, SID)["status"] == "hidden"          # raw inspection still sees it
    conn.close()


def test_dry_run_hide_reindex_failure_is_not_clean(client, tmp_path, monkeypatch):
    # The dry-run keys cleanliness on run_apply.status, so a sandbox reindex failure on a hide previews
    # as validation_failed (ADR-0043 stricter posture), even though the dry-run mutates no live state.
    _rendered_source(tmp_path)
    _approve_hide(tmp_path)

    def boom(root):
        raise RuntimeError("sandbox reindex blew up")
    monkeypatch.setattr(main_module.retention, "reindex_keyword", boom)
    dry = client.post("/reviews/apply/dry-run").json()
    assert dry["status"] == "validation_failed"
    assert {"source_id": SID, "field": "status", "from": "active", "to": "hidden"} in dry["diff"]["manifests"]
    assert manifests.get_status(manifests.load_manifest(
        tmp_path / "raw" / "manifests", SID)) == "active"   # live still unchanged
