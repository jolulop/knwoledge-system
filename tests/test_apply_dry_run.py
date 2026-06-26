"""ADR-0040: review-apply dry-run / mutation preview tests.

Covers the sandbox builder + semantic differ (unit) and the POST /reviews/apply/dry-run endpoint
(integration): no-drift apply-on-a-copy, live left byte-identical, raw fidelity, scripts/-backed
validators actually running, graph-unavailable mirroring live 503, not-appliable record-only types,
and dry-run/apply parity.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.backend import apply_sandbox, graph
from app.backend import main as main_module
from app.backend.config import get_settings
from app.workers import wiki


# --- fixtures / helpers -----------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


def _copy_code(tmp_path: Path) -> None:
    """Copy the real scripts/ + app/ + policies/ so the sandbox can run the subprocess validators."""
    for d in ("scripts", "app", "policies"):
        shutil.copytree(ROOT / d, tmp_path / d, dirs_exist_ok=True)


def _write_manifest(tmp_path: Path, sid: str, *, content: bytes = b"raw body") -> None:
    """A coherent manifest + raw bytes + normalized markdown, with a real size/mtime so
    validate_raw_integrity's pre-filter passes."""
    rel = f"raw/inbox/{sid}.md"
    raw = tmp_path / rel
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(content)
    st = raw.stat()
    md = tmp_path / "raw" / "manifests"
    md.mkdir(parents=True, exist_ok=True)
    manifest = {
        "source_id": sid, "sha256": hashlib.sha256(content).hexdigest(), "relative_raw_path": rel,
        "size_bytes": st.st_size,
        "modified_at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
        "file_extension": ".md", "chunk_count": 1, "ingestion_status": "extracted",
        "normalized": {"markdown_path": f"normalized/markdown/{sid}.md"},
        "created_at": "2020-01-01T00:00:00+00:00", "discovered_at": "2020-01-01T00:00:00+00:00",
        "retention_class": "permanent", "occurrences": [{"relative_path": rel}],
    }
    (md / f"{sid}.json").write_text(json.dumps(manifest), encoding="utf-8")
    norm = tmp_path / "normalized" / "markdown" / f"{sid}.md"
    norm.parent.mkdir(parents=True, exist_ok=True)
    norm.write_text(f"# {sid}\n\nReal prose body for the source.\n", encoding="utf-8")


def _rendered_source(tmp_path: Path, sid: str) -> Path:
    shutil.copytree(ROOT / "templates", tmp_path / "templates", dirs_exist_ok=True)
    _write_manifest(tmp_path, sid)
    wiki.generate_wiki(tmp_path, source_ids=[sid], rebuild_index=False, record_job=False)
    return tmp_path / "wiki" / "Sources" / f"{sid}.md"


def _graph_source_node(tmp_path: Path, sid: str) -> None:
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    conn = graph.connect(gdb)
    graph.upsert_node(conn, node_id=sid, node_type="source", slug=sid, status="active")
    conn.commit()
    conn.close()


def _approve(tmp_path: Path, item: dict) -> None:
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{item['review_id']}.json").write_text(json.dumps(item), encoding="utf-8")


def _approve_archive(tmp_path: Path, sid: str, rid: str = "rev_arch") -> None:
    _approve(tmp_path, {"review_id": rid, "type": "archive_source", "status": "approved",
                        "subject": {"source_id": sid}, "proposal": {"to_status": "archive_candidate"},
                        "context": {}})


def _tree_hash(root: Path) -> dict[str, str]:
    """Map every file (rel path) under root to a content hash — for a live-unchanged assertion."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# --- unit: sandbox builder --------------------------------------------------


def test_build_sandbox_copies_writes_readonly_and_referenced_raw(tmp_path):
    _rendered_source(tmp_path, "src_000000000000aa01")
    _graph_source_node(tmp_path, "src_000000000000aa01")
    (tmp_path / "db" / "llm_cache.sqlite").write_text("cache", encoding="utf-8")
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "validate_x.py").write_text("# v", encoding="utf-8")
    (tmp_path / "app").mkdir(exist_ok=True)
    (tmp_path / "app" / "marker.py").write_text("# m", encoding="utf-8")
    # an un-manifested staging file must NOT be copied
    (tmp_path / "raw" / "inbox" / "staging.md").write_text("staging", encoding="utf-8")

    settings = get_settings(tmp_path)
    tmp_root, sandbox = apply_sandbox.build_sandbox(settings)
    try:
        assert (tmp_root / "db" / "graph.sqlite").exists()
        assert not (tmp_root / "db" / "llm_cache.sqlite").exists()      # cache excluded
        assert (tmp_root / "scripts" / "validate_x.py").exists()        # scripts copied (validators)
        assert (tmp_root / "app" / "marker.py").exists()                # app copied (imports)
        assert (tmp_root / "raw" / "manifests" / "src_000000000000aa01.json").exists()
        assert (tmp_root / "raw" / "inbox" / "src_000000000000aa01.md").exists()  # catalogued raw
        assert not (tmp_root / "raw" / "inbox" / "staging.md").exists()  # un-manifested excluded
        # No live path reachable: nothing in the sandbox is a symlink.
        assert not any(p.is_symlink() for p in tmp_root.rglob("*"))
        assert sandbox.root == tmp_root
    finally:
        apply_sandbox.cleanup_sandbox(tmp_root)
    assert not tmp_root.exists()


def test_diff_states_graph_wiki_manifest_and_noop(tmp_path):
    before = apply_sandbox.StateSnapshot(
        nodes={"cpt_x": {"type": "concept", "status": "active"}},
        edges={"e1": {"src": "clm_a", "rel": "contradicts", "dst": "clm_b",
                      "status": "proposed", "review_id": "rev_1"}},
        wiki={"wiki/Concepts/x.md": "old\n"},
        reviews={"rev_d": "approved"},
        manifests={"src_b": "active"})
    after = apply_sandbox.StateSnapshot(
        nodes={"cpt_x": {"type": "concept", "status": "deprecated_candidate"}},
        # same edge_id, status flipped proposed -> rejected (NOT an active-set change)
        edges={"e1": {"src": "clm_a", "rel": "contradicts", "dst": "clm_b",
                      "status": "rejected", "review_id": "rev_1"}},
        wiki={"wiki/Concepts/x.md": "new\n"},
        reviews={"rev_d": None},  # moved out of approved
        manifests={"src_b": "archive_candidate"})
    diff = apply_sandbox.diff_states(before, after)
    assert diff["graph"]["nodes_status_changed"] == [
        {"id": "cpt_x", "type": "concept", "from": "active", "to": "deprecated_candidate"}]
    # the governed status transition is reported even though the active set never changed
    assert diff["graph"]["edges_status_changed"] == [
        {"src": "clm_a", "rel": "contradicts", "dst": "clm_b",
         "from": "proposed", "to": "rejected", "review_id": "rev_1"}]
    assert diff["manifests"] == [{"source_id": "src_b", "field": "status",
                                  "from": "active", "to": "archive_candidate"}]
    assert diff["wiki"][0]["path"] == "wiki/Concepts/x.md" and "+new" in diff["wiki"][0]["unified_diff"]
    assert not apply_sandbox.diff_is_empty(diff)
    # identical snapshots -> empty
    assert apply_sandbox.diff_is_empty(apply_sandbox.diff_states(before, before))


def test_build_sandbox_rejects_tampered_manifest_paths(tmp_path):
    # Untrusted manifest paths must be containment-checked (ADR-0009): absolute and ../ escapes are
    # neither stat'd nor copied; a valid raw path is copied. No file appears outside the sandbox.
    (tmp_path / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "raw" / "inbox").mkdir(parents=True, exist_ok=True)
    (tmp_path / "raw" / "inbox" / "ok.md").write_text("ok", encoding="utf-8")
    outside = tmp_path.parent / "ks_secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "raw" / "manifests" / "m.json").write_text(json.dumps({
        "source_id": "src_x", "sha256": "0" * 64,
        "relative_raw_path": "raw/inbox/ok.md",
        "occurrences": [{"relative_path": str(outside.resolve())},        # absolute escape
                        {"relative_path": "../../ks_secret.txt"}],         # parent-traversal escape
    }), encoding="utf-8")

    tmp_root, _ = apply_sandbox.build_sandbox(get_settings(tmp_path))
    try:
        assert (tmp_root / "raw" / "inbox" / "ok.md").exists()             # valid path copied
        # neither escape produced a file anywhere under the sandbox
        assert not any(p.name == "ks_secret.txt" for p in tmp_root.rglob("*"))
    finally:
        apply_sandbox.cleanup_sandbox(tmp_root)


def test_build_sandbox_survives_malformed_manifest(tmp_path):
    # A non-dict / unparseable manifest is untrusted on-disk state: build must not crash; the malformed
    # file is carried into the sandbox so validators surface it.
    md = tmp_path / "raw" / "manifests"
    md.mkdir(parents=True, exist_ok=True)
    (md / "bad.json").write_text("[1, 2, 3]", encoding="utf-8")      # valid JSON, not a dict
    (md / "broken.json").write_text("{not json", encoding="utf-8")  # unparseable
    tmp_root, _ = apply_sandbox.build_sandbox(get_settings(tmp_path))
    try:
        assert (tmp_root / "raw" / "manifests" / "bad.json").exists()
        assert (tmp_root / "raw" / "manifests" / "broken.json").exists()
    finally:
        apply_sandbox.cleanup_sandbox(tmp_root)


# --- integration: dry-run endpoint ------------------------------------------


def test_dry_run_archive_previews_and_leaves_live_unchanged(client, tmp_path):
    _rendered_source(tmp_path, "src_0000000000000a11")
    _graph_source_node(tmp_path, "src_0000000000000a11")
    _approve_archive(tmp_path, "src_0000000000000a11")
    before_hash = _tree_hash(tmp_path)

    resp = client.post("/reviews/apply/dry-run")
    assert resp.status_code == 200
    dry = resp.json()
    assert dry["status"] == "ok"
    # the manifest status flip is previewed...
    assert {"source_id": "src_0000000000000a11", "field": "status",
            "from": "active", "to": "archive_candidate"} in dry["diff"]["manifests"]
    # ...and the review move + provenance are present
    assert any(it["review_id"] == "rev_arch" and "manifests" in it["effects"] for it in dry["items"])
    # ...but LIVE is byte-identical (no executor touched live state).
    assert _tree_hash(tmp_path) == before_hash


def test_dry_run_parity_with_real_apply(client, tmp_path):
    _rendered_source(tmp_path, "src_0000000000000b22")
    _graph_source_node(tmp_path, "src_0000000000000b22")
    _approve_archive(tmp_path, "src_0000000000000b22")

    dry = client.post("/reviews/apply/dry-run").json()
    predicted = dry["diff"]["manifests"]
    # Now actually apply, and confirm the real mutation matches the prediction.
    applied = client.post("/reviews/apply").json()
    assert applied["summary"]["archives"]["applied"] == 1
    md = tmp_path / "raw" / "manifests" / "src_0000000000000b22.json"
    assert json.loads(md.read_text())["status"] == "archive_candidate"
    assert predicted == [{"source_id": "src_0000000000000b22", "field": "status",
                          "from": "active", "to": "archive_candidate"}]


def test_dry_run_graph_unavailable_blocked_mirrors_503(client, tmp_path):
    # An approved graph-required item with NO graph: dry-run is blocked, live apply 503s — same refusal.
    page = tmp_path / "wiki" / "Concepts" / "thing.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text('---\ntype: concept\nconcept_id: "cpt_x"\ntitle: "T"\nstatus: active\n'
                    "review_status: none\n---\n", encoding="utf-8")
    _approve(tmp_path, {"review_id": "rev_d", "type": "deprecate_wiki_page", "status": "approved",
                        "subject": {"node_id": "cpt_x", "page": "Concepts/thing.md"},
                        "proposal": {"to_status": "deprecated_candidate", "reason": "x"},
                        "context": {"node_type": "concept"}})

    dry = client.post("/reviews/apply/dry-run").json()
    assert dry["status"] == "blocked" and dry["reason"] == "graph_unavailable"
    assert apply_sandbox.diff_is_empty(dry["diff"])
    assert any(n["review_id"] == "rev_d" and n["reason"] == "graph_unavailable"
               for n in dry["not_appliable"])
    # live apply refuses identically
    assert client.post("/reviews/apply").status_code == 503


def test_dry_run_not_appliable_record_only(client, tmp_path):
    _approve(tmp_path, {"review_id": "rev_m", "type": "merge_entities", "status": "approved",
                        "subject": {}, "proposal": {}, "context": {}})
    dry = client.post("/reviews/apply/dry-run").json()
    assert dry["status"] == "ok"
    assert any(n["review_id"] == "rev_m" and n["reason"] == "no_executor_in_phase_6"
               for n in dry["not_appliable"])
    assert dry["items"] == []                      # nothing appliable -> no fabricated diff
    assert apply_sandbox.diff_is_empty(dry["diff"])


def test_dry_run_noop_empty_diff(client, tmp_path):
    dry = client.post("/reviews/apply/dry-run").json()
    assert dry["status"] == "ok"
    assert apply_sandbox.diff_is_empty(dry["diff"])
    assert dry["items"] == [] and dry["not_appliable"] == []


def test_dry_run_runs_scripts_validators_and_catches_failure(client, tmp_path):
    # scripts/ + app/ copied -> the subprocess validator suite actually runs in the sandbox; a planted
    # frontmatter-invalid page makes it fail, proving full-fidelity validation (ADR-0040 #2/#5).
    _copy_code(tmp_path)
    _rendered_source(tmp_path, "src_0000000000000c33")
    _graph_source_node(tmp_path, "src_0000000000000c33")
    _approve_archive(tmp_path, "src_0000000000000c33")  # a real change -> rebuild + validate run
    bad = tmp_path / "wiki" / "Concepts" / "bad.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("---\ntype: concept\n---\n\nNo summary, missing required fields.\n", encoding="utf-8")

    dry = client.post("/reviews/apply/dry-run").json()
    assert dry["status"] == "validation_failed"
    assert dry["validators"]["passed"] is False
    names = {f["name"] for f in dry["validators"]["failures"]}
    assert "validate_frontmatter.py" in names      # validators genuinely ran in the sandbox


def test_dry_run_contradiction_reject_shows_edge_status_change(client, tmp_path):
    # A rejected resolve_contradiction flips a proposed contradicts edge -> rejected (never touches the
    # active set), which the edge_id-keyed snapshot reports as a status change (ADR-0040 #4).
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    conn = graph.connect(gdb)
    graph.upsert_node(conn, node_id="clm_a", node_type="claim", slug="clm_a", status="active")
    graph.upsert_node(conn, node_id="clm_b", node_type="claim", slug="clm_b", status="active")
    graph.upsert_assertion(conn, src_id="clm_a", dst_id="clm_b", edge_type="contradicts",
                           asserted_by="llm", status="proposed", review_id="rev_c")
    conn.commit()
    conn.close()
    d = tmp_path / "reviews" / "rejected"
    d.mkdir(parents=True, exist_ok=True)
    (d / "rev_c.json").write_text(json.dumps({
        "review_id": "rev_c", "type": "resolve_contradiction", "status": "rejected",
        "subject": {"claim_a": "clm_a", "claim_b": "clm_b"}, "proposal": {}, "context": {}}),
        encoding="utf-8")

    dry = client.post("/reviews/apply/dry-run").json()
    assert dry["status"] == "ok"
    changes = dry["diff"]["graph"]["edges_status_changed"]
    assert any(c["rel"] == "contradicts" and c["from"] == "proposed" and c["to"] == "rejected"
               for c in changes), dry["diff"]["graph"]


def test_dry_run_sandbox_build_failure_is_structured(client, monkeypatch):
    # A sandbox build failure is an inability to produce a preview, not a 500 (ADR-0040 #6).
    def boom(_settings):
        raise OSError("disk full")
    monkeypatch.setattr(main_module.apply_sandbox, "build_sandbox", boom)
    resp = client.post("/reviews/apply/dry-run")
    assert resp.status_code == 200
    dry = resp.json()
    assert dry["status"] == "failed" and dry["reason"] == "sandbox_build_error"
    assert "OSError" in dry["error"] and dry["diff"] is None


def test_dry_run_raw_integrity_passes_and_live_raw_unchanged(client, tmp_path):
    # validate_raw_integrity reads raw/**; the sandbox copies the catalogued raw, so it passes, and
    # live raw is never touched (ADR-0040 #1).
    _copy_code(tmp_path)
    _rendered_source(tmp_path, "src_0000000000000d44")
    _graph_source_node(tmp_path, "src_0000000000000d44")
    _approve_archive(tmp_path, "src_0000000000000d44")
    raw = tmp_path / "raw" / "inbox" / "src_0000000000000d44.md"
    raw_before = raw.read_bytes()

    dry = client.post("/reviews/apply/dry-run").json()
    failed = {f["name"] for f in dry["validators"]["failures"]}
    assert "validate_raw_integrity.py" not in failed   # copied raw matches its manifest
    assert raw.read_bytes() == raw_before              # live raw byte-identical
