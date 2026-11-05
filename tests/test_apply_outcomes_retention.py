"""ADR-0072 decisions 2-3: per-review Apply outcomes for the source-status executors
(`archive_source`, `hide_content`, `unhide_content` — the shared `_apply_source_status_transition`).

The reporting contract only — the executors' behaviour is covered by test_retention.py,
test_hide_content.py and test_unhide.py and is unchanged: one outcome per approved review handled,
counts consistent with the outcomes, `already_in_effect` only after the page mirror is verified, a
no-op whose postcondition doesn't hold reported `skipped`, typed skip reasons, the document title
captured before the change, the id when the title can't be read, a page that didn't re-render
reported `incomplete`.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import manifests
from app.workers import apply_outcomes as ao
from app.workers import retention, wiki
from app.workers.wiki_render import parse_frontmatter

OLD = "2020-01-01T00:00:00+00:00"
NOW = "2026-11-04T00:00:00+00:00"
SID = "src_000000000000000a"
SID2 = "src_000000000000000b"

_EXECUTORS = {
    "archive_source": (retention.apply_archive_sources, "archive_candidate"),
    "hide_content": (retention.apply_hidden_sources, "hidden"),
    "unhide_content": (retention.apply_unhidden_sources, "active"),
}


# --- helpers ---------------------------------------------------------------


def _write_manifest(root: Path, sid: str, *, status: str = "active", extracted: bool = True) -> None:
    md = root / "raw" / "manifests"
    md.mkdir(parents=True, exist_ok=True)
    rel = f"raw/inbox/{sid}.md"
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text("raw bytes", encoding="utf-8")
    m = {"source_id": sid, "sha256": sid + "0" * 8, "relative_raw_path": rel, "file_extension": ".md",
         "chunk_count": 1, "ingestion_status": "extracted" if extracted else "scanned",
         "normalized": {"markdown_path": f"normalized/markdown/{sid}.md"},
         "created_at": OLD, "discovered_at": OLD, "modified_at": OLD,
         "retention_class": "permanent", "occurrences": [{"relative_path": rel}]}
    if status != "active":
        m["status"] = status
    (md / f"{sid}.json").write_text(json.dumps(m), encoding="utf-8")
    norm = root / "normalized" / "markdown" / f"{sid}.md"
    norm.parent.mkdir(parents=True, exist_ok=True)
    norm.write_text(f"# {sid}\n\nReal prose body for the source.\n", encoding="utf-8")


def _source(root: Path, sid: str = SID, *, status: str = "active", render: bool = True,
            extracted: bool = True) -> Path:
    shutil.copytree(ROOT / "templates", root / "templates", dirs_exist_ok=True)
    _write_manifest(root, sid, status=status, extracted=extracted)
    if render:
        wiki.generate_wiki(root, source_ids=[sid], rebuild_index=False, record_job=False)
    return root / "wiki" / "Sources" / f"{sid}.md"


def _approve(root: Path, review_type: str, *, sid=SID, rid="rev_1", to_status=None,
             status="approved", subject=None) -> None:
    d = root / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    to_status = to_status or _EXECUTORS[review_type][1]
    (d / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": review_type, "status": status,
        "subject": {"source_id": sid} if subject is None else subject,
        "proposal": {"to_status": to_status}, "context": {}}), encoding="utf-8")


def _apply(root: Path, review_type: str) -> dict:
    fn, _ = _EXECUTORS[review_type]
    return fn(root, manifests_dir=root / "raw" / "manifests", reviews_dir=root / "reviews",
              wiki_dir=root / "wiki", graph_db=root / "db" / "graph.sqlite", now=NOW)


def _set_page_title(page: Path, title: str) -> None:
    text = page.read_text(encoding="utf-8")
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("title:"):
            lines[i] = f'title: "{title}"'
            break
    page.write_text("\n".join(lines), encoding="utf-8")


def _assert_consistent(res: dict) -> None:
    """Counts match the outcomes: applied (+ incomplete, still counted in `applied`) and
    `len(skipped)` (= skipped outcomes minus the two cases that stay silent in `skipped`)."""
    c = ao.counts(res["outcomes"])
    assert res["applied"] == c[ao.APPLIED] + c[ao.INCOMPLETE]
    silent = {"page_manifest_drift", "source_not_active", ao.VERIFICATION_UNAVAILABLE}
    typed_skips = [o for o in res["outcomes"]
                   if o["outcome"] == ao.SKIPPED and o["reason"] not in silent]
    assert sorted((s["review_id"], s["reason"]) for s in res["skipped"]) == \
        sorted((o["review_id"], o["reason"]) for o in typed_skips)


def _one(res: dict) -> dict:
    assert len(res["outcomes"]) == 1
    return res["outcomes"][0]


# --- applied / already_in_effect ------------------------------------------------


def test_archive_applied_then_rerun_already_in_effect(tmp_path):
    page = _source(tmp_path)
    _set_page_title(page, "Data Mesh Primer")
    _approve(tmp_path, "archive_source")

    res = _apply(tmp_path, "archive_source")
    rec = _one(res)
    assert rec["review_id"] == "rev_1" and rec["type"] == "archive_source"
    assert rec["outcome"] == ao.APPLIED and "reason" not in rec
    assert rec["details"]["subject"] == {"id": SID, "name": "Data Mesh Primer"}
    assert rec["details"]["before"] == {"status": "active"}
    assert rec["details"]["after"] == {"status": "archive_candidate"}
    assert res["applied"] == 1
    _assert_consistent(res)

    # Rerun: the approved review is read again; the postcondition (manifest + page) is verified.
    res2 = _apply(tmp_path, "archive_source")
    rec2 = _one(res2)
    assert rec2["outcome"] == ao.ALREADY_IN_EFFECT
    assert rec2["details"]["before"] == rec2["details"]["after"] == {"status": "archive_candidate"}
    assert res2["applied"] == 0 and res2["skipped"] == [] and res2["changed_pages"] == []
    _assert_consistent(res2)


def test_hide_and_unhide_report_applied(tmp_path):
    _source(tmp_path)
    _approve(tmp_path, "hide_content", rid="rev_h")
    rec = _one(_apply(tmp_path, "hide_content"))
    assert (rec["outcome"], rec["type"]) == (ao.APPLIED, "hide_content")
    assert (rec["details"]["before"], rec["details"]["after"]) == ({"status": "active"},
                                                                   {"status": "hidden"})

    _approve(tmp_path, "unhide_content", rid="rev_u")
    rec = _one(_apply(tmp_path, "unhide_content"))
    assert (rec["outcome"], rec["review_id"]) == (ao.APPLIED, "rev_u")
    assert (rec["details"]["before"], rec["details"]["after"]) == ({"status": "hidden"},
                                                                   {"status": "active"})


def test_title_is_captured_before_the_change(tmp_path):
    """The page is re-rendered by the apply (its title is reset from the manifest); the outcome keeps
    the title the page had BEFORE."""
    page = _source(tmp_path)
    _set_page_title(page, "Title Before Apply")
    _approve(tmp_path, "hide_content")
    rec = _one(_apply(tmp_path, "hide_content"))
    assert parse_frontmatter(page.read_text(encoding="utf-8")).get("title") != "Title Before Apply"
    assert rec["details"]["subject"]["name"] == "Title Before Apply"


def test_unreadable_title_falls_back_to_the_id(tmp_path):
    # Not extracted: no Source page exists (nor is one expected) -> the name is the id.
    _source(tmp_path, render=False, extracted=False)
    _approve(tmp_path, "archive_source")
    rec = _one(_apply(tmp_path, "archive_source"))
    assert rec["outcome"] == ao.APPLIED
    assert rec["details"]["subject"] == {"id": SID, "name": SID}


def test_blank_title_falls_back_to_the_id(tmp_path):
    page = _source(tmp_path)
    _set_page_title(page, "")
    _approve(tmp_path, "hide_content")
    rec = _one(_apply(tmp_path, "hide_content"))
    assert rec["details"]["subject"] == {"id": SID, "name": SID}


# --- no-op whose postcondition doesn't hold ------------------------------------


def test_target_status_with_drifted_page_is_skipped_not_already_in_effect(tmp_path):
    page = _source(tmp_path)                                  # page renders status: active
    manifests.set_status(tmp_path / "raw" / "manifests", SID, "hidden")   # manifest moved alone
    before = page.read_bytes()
    _approve(tmp_path, "hide_content")
    res = _apply(tmp_path, "hide_content")
    rec = _one(res)
    assert rec["outcome"] == ao.SKIPPED and rec["reason"] == "page_manifest_drift"
    assert rec["details"]["problem"]["page_status"] == "active"
    assert rec["details"]["problem"]["manifest_status"] == "hidden"
    # Behaviour unchanged: silent no-op, nothing written, `skipped` stays empty.
    assert res["applied"] == 0 and res["skipped"] == [] and page.read_bytes() == before
    _assert_consistent(res)


def test_target_status_with_missing_page_is_skipped(tmp_path):
    _source(tmp_path, status="archive_candidate", render=False)   # extracted, page never rendered
    _approve(tmp_path, "archive_source")
    rec = _one(_apply(tmp_path, "archive_source"))
    assert rec["outcome"] == ao.SKIPPED and rec["reason"] == "page_manifest_drift"
    assert rec["details"]["problem"]["page_status"] is None


def test_archive_of_third_state_source_is_skipped_source_not_active(tmp_path):
    _source(tmp_path, status="hidden")
    _approve(tmp_path, "archive_source")
    res = _apply(tmp_path, "archive_source")
    rec = _one(res)
    assert rec["outcome"] == ao.SKIPPED and rec["reason"] == "source_not_active"
    assert rec["details"]["before"] == rec["details"]["after"] == {"status": "hidden"}
    assert res["skipped"] == []                                   # silent in `skipped`, as before
    assert manifests.get_status(manifests.load_manifest(tmp_path / "raw" / "manifests", SID)) \
        == "hidden"
    _assert_consistent(res)


def test_unhide_of_non_hidden_source_is_typed_skip(tmp_path):
    _source(tmp_path, status="archive_candidate")
    _approve(tmp_path, "unhide_content")
    res = _apply(tmp_path, "unhide_content")
    rec = _one(res)
    assert rec["outcome"] == ao.SKIPPED and rec["reason"] == "source_not_hidden"
    assert res["skipped"] == [{"review_id": "rev_1", "reason": "source_not_hidden"}]
    _assert_consistent(res)


# --- skipped reasons -----------------------------------------------------------


def test_every_skip_reason_has_a_matching_outcome(tmp_path):
    _source(tmp_path)
    _approve(tmp_path, "archive_source", rid="rev_a_not_approved", status="pending")
    _approve(tmp_path, "archive_source", rid="rev_b_wrong_target", to_status="hidden")
    _approve(tmp_path, "archive_source", rid="rev_c_no_subject", subject={})
    _approve(tmp_path, "archive_source", rid="rev_d_bad_id", sid="../../etc/passwd")
    _approve(tmp_path, "archive_source", rid="rev_e_missing", sid=SID2)
    res = _apply(tmp_path, "archive_source")
    by_id = {o["review_id"]: o for o in res["outcomes"]}
    assert len(res["outcomes"]) == len(by_id) == 5
    assert {rid: o["reason"] for rid, o in by_id.items()} == {
        "rev_a_not_approved": "not_approved", "rev_b_wrong_target": "unexpected_to_status",
        "rev_c_no_subject": "missing_subject", "rev_d_bad_id": "invalid_source_id",
        "rev_e_missing": "source_missing"}
    assert all(o["outcome"] == ao.SKIPPED for o in by_id.values())
    assert by_id["rev_b_wrong_target"]["details"]["problem"] == {"to_status": "hidden",
                                                                 "expected": "archive_candidate"}
    # A non-canonical id is never used in a path: its name is the raw id itself.
    assert by_id["rev_d_bad_id"]["details"]["subject"] == {"id": "../../etc/passwd",
                                                          "name": "../../etc/passwd"}
    assert by_id["rev_e_missing"]["details"]["subject"] == {"id": SID2, "name": SID2}
    assert res["applied"] == 0 and len(res["skipped"]) == 5
    _assert_consistent(res)


def test_one_outcome_per_approved_review_and_reconcile_has_no_gaps(tmp_path):
    _source(tmp_path)
    _source(tmp_path, SID2, status="archive_candidate")
    _approve(tmp_path, "archive_source", rid="rev_1", sid=SID)              # applied
    _approve(tmp_path, "archive_source", rid="rev_2", sid=SID2)             # already in effect
    _approve(tmp_path, "archive_source", rid="rev_3", sid="src_000000000000000f")  # source_missing
    _approve(tmp_path, "hide_content", rid="rev_4", sid=SID)                # other type: not ours
    res = _apply(tmp_path, "archive_source")
    assert sorted(o["review_id"] for o in res["outcomes"]) == ["rev_1", "rev_2", "rev_3"]
    assert {o["review_id"]: o["outcome"] for o in res["outcomes"]} == {
        "rev_1": ao.APPLIED, "rev_2": ao.ALREADY_IN_EFFECT, "rev_3": ao.SKIPPED}
    _assert_consistent(res)
    handled = [{"review_id": r, "type": "archive_source"} for r in ("rev_1", "rev_2", "rev_3")]
    assert ao.reconcile(handled, res["outcomes"])["gaps"] == []


# --- incomplete ------------------------------------------------------------------


def test_page_render_failure_after_manifest_write_is_incomplete(tmp_path, monkeypatch):
    page = _source(tmp_path)
    _approve(tmp_path, "hide_content")
    real = wiki.generate_wiki

    def failing(root, *, source_ids=None, **kw):
        return {"errors": 1, "error_details": [{"source_id": SID, "reason": "render_failed",
                                                "error": "RuntimeError: boom"}]}

    monkeypatch.setattr(wiki, "generate_wiki", failing)
    res = _apply(tmp_path, "hide_content")
    rec = _one(res)
    assert rec["outcome"] == ao.INCOMPLETE and rec["reason"] == "source_page_refresh_failed"
    assert rec["details"]["problem"]["source_id"] == SID
    assert rec["details"]["source_id"] == SID            # explicit, for Apply's publication retry
    assert rec["details"]["subject"]["id"] == SID
    assert rec["details"]["after"] == {"status": "hidden"}
    # Behaviour unchanged: the manifest flipped and is still counted as applied.
    assert res["applied"] == 1 and res["changed_pages"] == [f"Sources/{SID}.md"]
    assert manifests.get_status(manifests.load_manifest(tmp_path / "raw" / "manifests", SID)) \
        == "hidden"
    assert parse_frontmatter(page.read_text(encoding="utf-8"))["status"] == "active"
    _assert_consistent(res)

    # Next run: manifest already at the target but the page still drifted -> not already_in_effect.
    monkeypatch.setattr(wiki, "generate_wiki", real)
    rec2 = _one(_apply(tmp_path, "hide_content"))
    assert rec2["outcome"] == ao.SKIPPED and rec2["reason"] == "page_manifest_drift"


def test_page_not_rewritten_without_reported_error_is_incomplete(tmp_path, monkeypatch):
    """The post-render check catches a page that silently kept its old status."""
    _source(tmp_path)
    _approve(tmp_path, "archive_source")
    monkeypatch.setattr(wiki, "generate_wiki", lambda root, **kw: {"errors": 0, "error_details": []})
    rec = _one(_apply(tmp_path, "archive_source"))
    assert rec["outcome"] == ao.INCOMPLETE and rec["reason"] == "source_page_refresh_failed"
    assert rec["details"]["source_id"] == SID
    assert rec["details"]["problem"]["page_status"] == "active"
    assert rec["details"]["problem"]["manifest_status"] == "archive_candidate"


# --- verified end state (B2a review round 1) -------------------------------------
# Reporting reads never raise and never justify `already_in_effect`; `after` is read back.

_MD = "raw/manifests"


def _boom(*a, **kw):
    raise OSError("unreadable")


def _status(root: Path) -> str:
    return manifests.get_status(manifests.load_manifest(root / _MD, SID))


def test_noop_page_check_failure_is_unavailable_not_already_in_effect(tmp_path, monkeypatch):
    _source(tmp_path)
    _approve(tmp_path, "hide_content")
    _apply(tmp_path, "hide_content")                              # now hidden, page mirrors it
    monkeypatch.setattr(retention, "_source_page_drift", _boom)   # the verification read fails
    res = _apply(tmp_path, "hide_content")                        # no exception
    rec = _one(res)
    assert (rec["outcome"], rec["reason"]) == (ao.SKIPPED, ao.VERIFICATION_UNAVAILABLE)
    assert res["applied"] == 0 and res["skipped"] == []           # silent no-op, as before
    _assert_consistent(res)
    monkeypatch.undo()
    assert _one(_apply(tmp_path, "hide_content"))["outcome"] == ao.ALREADY_IN_EFFECT   # verified


def test_post_write_page_check_failure_is_incomplete_and_result_stands(tmp_path, monkeypatch):
    page = _source(tmp_path)
    _approve(tmp_path, "archive_source")
    monkeypatch.setattr(retention, "_source_page_drift", _boom)
    res = _apply(tmp_path, "archive_source")
    rec = _one(res)
    assert (rec["outcome"], rec["reason"]) == (ao.INCOMPLETE, ao.VERIFICATION_UNAVAILABLE)
    assert rec["details"]["source_id"] == SID
    assert res["applied"] == 1 and res["changed_pages"] == [f"Sources/{SID}.md"]
    assert _status(tmp_path) == "archive_candidate"
    assert parse_frontmatter(page.read_text(encoding="utf-8"))["status"] == "archive_candidate"
    _assert_consistent(res)


def test_post_write_manifest_read_back_failure_is_incomplete(tmp_path, monkeypatch):
    _source(tmp_path)
    _approve(tmp_path, "hide_content")
    real, calls = retention.load_manifest, []

    def flaky(*a, **kw):                     # the executor's read works; the read-back fails
        calls.append(1)
        if len(calls) > 1:
            raise OSError("unreadable")
        return real(*a, **kw)

    monkeypatch.setattr(retention, "load_manifest", flaky)
    res = _apply(tmp_path, "hide_content")
    rec = _one(res)
    assert (rec["outcome"], rec["reason"]) == (ao.INCOMPLETE, ao.VERIFICATION_UNAVAILABLE)
    assert "after" not in rec["details"] and rec["details"]["source_id"] == SID
    assert res["applied"] == 1
    monkeypatch.undo()
    assert _status(tmp_path) == "hidden"


def test_applied_after_is_the_read_back_state(tmp_path):
    page = _source(tmp_path)
    _approve(tmp_path, "hide_content")
    rec = _one(_apply(tmp_path, "hide_content"))
    assert rec["outcome"] == ao.APPLIED and rec["details"]["after"] == {"status": "hidden"}
    assert _status(tmp_path) == "hidden"
    assert parse_frontmatter(page.read_text(encoding="utf-8"))["status"] == "hidden"


def test_manifest_read_back_mismatch_is_postcondition_not_met(tmp_path, monkeypatch):
    _source(tmp_path)
    _approve(tmp_path, "archive_source")
    monkeypatch.setattr(retention.manifests, "set_status", lambda *a, **kw: None)  # write lost
    res = _apply(tmp_path, "archive_source")
    rec = _one(res)
    assert (rec["outcome"], rec["reason"]) == (ao.INCOMPLETE, ao.POSTCONDITION_NOT_MET)
    assert rec["details"]["after"] == {"status": "active"}       # read back, not the target
    assert rec["details"]["problem"] == {"manifest_status": "active",
                                         "expected": "archive_candidate"}
    assert res["applied"] == 1                                   # legacy counter unchanged
    _assert_consistent(res)


def test_manifest_read_back_hidden_is_hidden_takes_precedence(tmp_path, monkeypatch):
    _source(tmp_path)
    _approve(tmp_path, "archive_source")
    real = manifests.set_status
    monkeypatch.setattr(retention.manifests, "set_status",        # a concurrent hide wins
                        lambda md, sid, status: real(md, sid, "hidden"))
    rec = _one(_apply(tmp_path, "archive_source"))
    assert (rec["outcome"], rec["reason"]) == (ao.INCOMPLETE, ao.HIDDEN_TAKES_PRECEDENCE)
    assert rec["details"]["after"] == {"status": "hidden"}
    monkeypatch.undo()
    assert _status(tmp_path) == "hidden"
