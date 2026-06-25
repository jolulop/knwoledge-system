from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import db, manifests
from app.workers import extract, intake
from app.workers.extract import MissingExtractionDependency
from tests import fixtures


def _build_project(tmp_path: Path, *, with_pdf=True) -> Path:
    """Create raw/inbox fixtures and run intake so manifests exist, then return root."""
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.write_markdown(inbox / "doc.md")
    fixtures.write_html(inbox / "page.html")
    fixtures.write_csv(inbox / "data.csv")
    fixtures.write_docx(inbox / "report.docx")
    if with_pdf:
        fixtures.write_pdf(inbox / "paper.pdf", ["PDF page one has plenty of words here.",
                                                 "PDF page two also carries readable text."])
    (inbox / "notes.txt").write_text("unsupported format\n", encoding="utf-8")  # unsupported
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    return tmp_path


def _run(tmp_path: Path, **kwargs):
    return extract.extract_sources(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite", **kwargs)


def test_extracts_all_supported_formats(tmp_path):
    _build_project(tmp_path)
    summary = _run(tmp_path)

    # 5 supported sources (md, html, csv, docx, pdf); the .txt is unsupported.
    assert summary["sources_considered"] == 5
    assert summary["extracted"] == 5
    assert summary["errors"] == 0
    assert summary["skipped_unsupported"] == 1

    md_dir = tmp_path / "normalized" / "markdown"
    assert len(list(md_dir.glob("*.md"))) == 5

    # Every extracted source has its manifest updated and its log written.
    for manifest in manifests.list_manifests(tmp_path / "raw" / "manifests"):
        if manifest["file_extension"] == ".txt":
            assert manifest["ingestion_status"] == "new"
            continue
        assert manifest["ingestion_status"] == "extracted"
        assert manifest["normalized"]["markdown_path"].endswith(f"{manifest['source_id']}.md")
        assert manifest["extracted_at"] is not None
        assert manifest["retention_class"] == "unknown"  # unchanged in Phase 2
        log = tmp_path / "normalized" / "extraction_logs" / f"{manifest['source_id']}.json"
        assert log.exists()


def test_pdf_pages_and_tables_dir(tmp_path):
    _build_project(tmp_path)
    _run(tmp_path)
    records = {m["original_filename"]: m for m in
              manifests.list_manifests(tmp_path / "raw" / "manifests")}

    pdf = records["paper.pdf"]
    assert pdf["page_count"] == 2

    # CSV produces a structured table file under the per-source tables dir.
    csv = records["data.csv"]
    table_dir = tmp_path / "normalized" / "tables" / csv["source_id"]
    assert (table_dir / "0.csv").exists()

    # tables_dir is present even for sources with no tables (e.g. the markdown doc).
    md = records["doc.md"]
    assert (tmp_path / "normalized" / "tables" / md["source_id"]).is_dir()


def test_idempotent_skip_and_force(tmp_path):
    _build_project(tmp_path)
    first = _run(tmp_path)
    assert first["extracted"] == 5

    second = _run(tmp_path)
    assert second["extracted"] == 0
    assert second["skipped_unchanged"] == 5

    forced = _run(tmp_path, force=True)
    assert forced["extracted"] == 5
    assert forced["skipped_unchanged"] == 0


def test_oversize_file_is_an_error(tmp_path):
    _build_project(tmp_path, with_pdf=False)
    # max_file_mb=0 makes every non-empty file oversize.
    summary = _run(tmp_path, max_file_mb=0)
    assert summary["errors"] >= 1
    assert any(e["skip_reason"] == "oversize" for e in summary["error_details"])
    # The run still completes and records the failures, never crashing.
    bad = [m for m in manifests.list_manifests(tmp_path / "raw" / "manifests")
           if m["ingestion_status"] == "error"]
    assert bad and all(m["extracted_at"] is None for m in bad)


def test_zero_text_pdf_is_partial_needs_ocr(tmp_path):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.write_pdf(inbox / "scan.pdf", ["", ""])  # paginated but no embedded text
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")

    summary = _run(tmp_path)
    assert summary["partial"] == 1
    assert any(w["warning"] == "needs_ocr" for w in summary["warnings"])
    manifest = manifests.list_manifests(tmp_path / "raw" / "manifests")[0]
    assert manifest["ingestion_status"] == "partial"


def test_empty_non_paginated_source_is_partial_needs_ocr(tmp_path):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "empty.html").write_text("<html><body></body></html>", encoding="utf-8")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")

    summary = _run(tmp_path)
    assert summary["partial"] == 1
    assert any(w["warning"] == "needs_ocr" for w in summary["warnings"])
    manifest = manifests.list_manifests(tmp_path / "raw" / "manifests")[0]
    assert manifest["ingestion_status"] == "partial"


def test_extract_job_is_recorded(tmp_path):
    _build_project(tmp_path)
    summary = _run(tmp_path)

    conn = db.connect(tmp_path / "db" / "jobs.sqlite")
    try:
        job = db.get_job(conn, summary["job_id"])
    finally:
        conn.close()
    assert job is not None
    assert job["job_type"] == "extract"
    assert job["status"] == "succeeded"
    assert job["metadata"]["extracted"] == 5


def test_path_escape_is_rejected(tmp_path):
    # A hand-edited manifest whose raw path escapes raw/ must never be read.
    (tmp_path / "raw" / "manifests").mkdir(parents=True)
    (tmp_path / "outside.md").write_text("secret outside the repo\n", encoding="utf-8")
    sid = "src_0123456789abcdef"  # canonical id; the path escape (not the id) is under test
    manifests.save_manifest(
        tmp_path / "raw" / "manifests",
        {
            "source_id": sid,
            "original_filename": "outside.md",
            "relative_raw_path": "../outside.md",  # escapes <root>/raw
            "sha256": "0" * 64,
            "file_extension": ".md",
            "ingestion_status": "new",
        },
    )
    summary = _run(tmp_path)
    assert summary["errors"] == 1
    assert summary["error_details"][0]["skip_reason"] == "path_escape"
    assert not list((tmp_path / "normalized" / "markdown").glob("*.md"))


def test_checksum_mismatch_errors(tmp_path):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    doc = inbox / "doc.md"
    doc.write_text("original content here\n", encoding="utf-8")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    # Tamper with the bytes without re-scanning: manifest sha is now stale.
    doc.write_text("TAMPERED — different content entirely\n", encoding="utf-8")

    summary = _run(tmp_path)
    assert summary["errors"] == 1
    assert summary["error_details"][0]["skip_reason"] == "checksum_mismatch"
    # No normalized evidence is produced under the manifest's (stale) source_id.
    assert not list((tmp_path / "normalized" / "markdown").glob("*.md"))


def test_failed_force_reextract_preserves_last_good(tmp_path, monkeypatch):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "doc.md").write_text("# Title\n\nA solid paragraph of body text.\n", encoding="utf-8")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")

    first = _run(tmp_path)
    assert first["extracted"] == 1
    sid = manifests.list_manifests(tmp_path / "raw" / "manifests")[0]["source_id"]
    md_path = tmp_path / "normalized" / "markdown" / f"{sid}.md"
    good_markdown = md_path.read_text(encoding="utf-8")

    # Force a re-extraction, but make the extractor blow up after the checksum passes.
    def boom(path, source_id):
        raise RuntimeError("simulated extractor crash")

    monkeypatch.setattr(extract, "_dispatch", boom)
    second = _run(tmp_path, force=True)

    assert second["errors"] == 1
    assert second["error_details"][0]["preserved_prior"] is True
    # Last-good artifacts and manifest survive untouched.
    manifest = manifests.load_manifest(tmp_path / "raw" / "manifests", sid)
    assert manifest["ingestion_status"] == "extracted"
    assert md_path.read_text(encoding="utf-8") == good_markdown


def test_load_extractor_missing_dependency_message():
    with pytest.raises(MissingExtractionDependency) as excinfo:
        extract._load_extractor("module_that_does_not_exist_xyz")
    assert excinfo.value.skip_reason == "missing_dependency"
    assert "uv sync --extra extraction" in str(excinfo.value)


def test_missing_dependency_surfaces_as_error(tmp_path, monkeypatch):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.write_pdf(inbox / "p.pdf", ["some readable text on the page"])
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")

    def fail(module_name):
        raise MissingExtractionDependency("No module named 'pypdf'")

    monkeypatch.setattr(extract, "_load_extractor", fail)
    summary = _run(tmp_path)
    assert summary["errors"] == 1
    assert summary["error_details"][0]["skip_reason"] == "missing_dependency"


def test_mutated_raw_after_extraction_is_not_silently_skipped(tmp_path):
    import time

    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "doc.md").write_text("original content for extraction\n", encoding="utf-8")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    assert _run(tmp_path)["extracted"] == 1

    # Mutate the raw bytes in place without re-scanning (size + mtime drift).
    time.sleep(0.01)
    (inbox / "doc.md").write_text("MUTATED content of a clearly different length\n", encoding="utf-8")
    second = _run(tmp_path)  # no force

    # The drifted source is re-verified, not skipped: surfaced as checksum_mismatch.
    assert second["skipped_unchanged"] == 0
    assert second["errors"] == 1
    assert second["error_details"][0]["skip_reason"] == "checksum_mismatch"


def test_raw_files_are_not_modified(tmp_path):
    _build_project(tmp_path)
    inbox = tmp_path / "raw" / "inbox"
    before = {p.name: p.read_bytes() for p in inbox.iterdir() if p.is_file()}
    _run(tmp_path)
    after = {p.name: p.read_bytes() for p in inbox.iterdir() if p.is_file()}
    assert before == after


def test_already_extracted_rejects_escaping_path(tmp_path):
    # The idempotent-skip fast path must enforce the same raw containment as _extract_one (ADR-0009):
    # a tampered relative_raw_path that escapes raw/ is NOT a valid skip, even when the external file's
    # size+mtime match the manifest (without the guard this would stat it and skip as "unchanged").
    outside = tmp_path / "outside.md"
    outside.write_text("secret outside the repo\n", encoding="utf-8")
    markdown_dir = tmp_path / "normalized" / "markdown"
    markdown_dir.mkdir(parents=True)
    sid = "src_0123456789abcdef"
    (markdown_dir / f"{sid}.md").write_text("prior extracted output\n", encoding="utf-8")
    st = outside.stat()
    manifest = {
        "source_id": sid,
        "ingestion_status": "extracted",
        "relative_raw_path": str(outside),          # absolute path -> escapes <root>/raw
        "size_bytes": st.st_size,                   # matches the external file...
        "modified_at": extract._iso_mtime(outside),  # ...as does the mtime -> would skip without the guard
    }
    assert extract._already_extracted(manifest, tmp_path, markdown_dir) is False


def test_tampered_manifest_after_extraction_is_not_skipped(tmp_path):
    # End-to-end: a previously-extracted source whose manifest is later tampered to point outside raw/
    # (at a real file with matching size/mtime) must be diverted from the skip path to a path_escape
    # error. Prior artifacts + extracted status are preserved and the manifest is NOT rewritten (it is
    # left as-is, still carrying the tampered path — extraction never mutates the untrusted manifest).
    _build_project(tmp_path)
    assert _run(tmp_path)["extracted"] == 5

    manifests_dir = tmp_path / "raw" / "manifests"
    target = next(m for m in manifests.list_manifests(manifests_dir) if m["original_filename"] == "doc.md")
    sid = target["source_id"]
    outside = tmp_path / "outside.md"
    outside.write_text("secret outside the repo\n", encoding="utf-8")
    st = outside.stat()
    target["relative_raw_path"] = str(outside)      # absolute escape, real file, matching metadata
    target["size_bytes"] = st.st_size
    target["modified_at"] = extract._iso_mtime(outside)
    manifests.save_manifest(manifests_dir, target)

    second = _run(tmp_path)  # non-force: the OLD skip path would have stat'd + skipped this
    assert second["skipped_unchanged"] == 4         # the tampered source is NOT skipped
    escapes = [e for e in second["error_details"] if e["skip_reason"] == "path_escape"]
    assert [e["source_id"] for e in escapes] == [sid]
    assert escapes[0]["preserved_prior"] is True
    # prior state preserved (persisted=False): ingestion_status NOT flipped to error, markdown intact.
    # The manifest is not rewritten — it still carries the tampered path; extraction doesn't "fix" it.
    reloaded = next(m for m in manifests.list_manifests(manifests_dir) if m["source_id"] == sid)
    assert reloaded["ingestion_status"] == "extracted"
    assert reloaded["relative_raw_path"] == str(outside)  # untouched (not restored, not rewritten)
    assert (tmp_path / "normalized" / "markdown" / f"{sid}.md").exists()


def test_absolute_relative_raw_path_inside_raw_is_rejected(tmp_path):
    # Schema hardening: relative_raw_path must be RELATIVE. An absolute path is rejected as path_escape
    # even when it points at a real file inside root/raw (previously accepted by is_relative_to).
    _build_project(tmp_path, with_pdf=False)
    manifests_dir = tmp_path / "raw" / "manifests"
    target = next(m for m in manifests.list_manifests(manifests_dir) if m["original_filename"] == "doc.md")
    sid = target["source_id"]
    abs_inside = (tmp_path / target["relative_raw_path"]).resolve()   # the real raw file, absolute path
    assert abs_inside.is_file() and abs_inside.is_relative_to((tmp_path / "raw").resolve())
    target["relative_raw_path"] = str(abs_inside)
    manifests.save_manifest(manifests_dir, target)

    summary = _run(tmp_path)  # status 'new' -> straight to _extract_one
    assert any(e["source_id"] == sid and e["skip_reason"] == "path_escape"
               for e in summary["error_details"])


def test_symlink_escape_under_raw_is_rejected(tmp_path):
    # Extraction's containment is independent of intake's symlink rejection: a manifest pointing at a
    # symlink UNDER raw/ whose resolved target escapes raw/ must be path_escape (safe_under resolves
    # before the containment check), and the skip path must not accept it either.
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    external = tmp_path / "secret.txt"
    external.write_text("secret outside the repo\n", encoding="utf-8")
    (inbox / "link.md").symlink_to(external)             # raw/inbox/link.md -> <root>/secret.txt
    manifests_dir = tmp_path / "raw" / "manifests"
    manifests_dir.mkdir(parents=True)
    sid = "src_0123456789abcdef"
    manifests.save_manifest(manifests_dir, {
        "source_id": sid, "original_filename": "link.md",
        "relative_raw_path": "raw/inbox/link.md",        # not absolute / no '..': only resolution escapes
        "sha256": "0" * 64, "file_extension": ".md", "ingestion_status": "new",
    })
    summary = _run(tmp_path)
    assert any(e["source_id"] == sid and e["skip_reason"] == "path_escape"
               for e in summary["error_details"])
    # skip path is independently guarded
    markdown_dir = tmp_path / "normalized" / "markdown"
    markdown_dir.mkdir(parents=True, exist_ok=True)
    (markdown_dir / f"{sid}.md").write_text("prior\n", encoding="utf-8")
    assert extract._already_extracted(
        {"source_id": sid, "ingestion_status": "extracted", "relative_raw_path": "raw/inbox/link.md"},
        tmp_path, markdown_dir) is False


def test_missing_relative_raw_path_is_clean_path_escape(tmp_path):
    # A corrupted manifest with a missing/empty relative_raw_path must fail cleanly as path_escape, not
    # an unhandled KeyError (the shared resolver uses .get(..., "") -> safe_under -> None).
    manifests_dir = tmp_path / "raw" / "manifests"
    manifests_dir.mkdir(parents=True)
    sid = "src_0123456789abcdef"
    manifests.save_manifest(manifests_dir, {
        "source_id": sid, "original_filename": "doc.md",  # relative_raw_path intentionally OMITTED
        "sha256": "0" * 64, "file_extension": ".md", "ingestion_status": "new",
    })
    summary = _run(tmp_path)
    assert summary["errors"] == 1
    assert [e["skip_reason"] for e in summary["error_details"]] == ["path_escape"]


def test_nested_parent_traversal_is_rejected(tmp_path):
    # Pin the contract at extraction level: any '..' segment is rejected up-front by safe_under, even one
    # that resolves to an existing file, surfaced as path_escape.
    (tmp_path / "outside.md").write_text("secret\n", encoding="utf-8")
    manifests_dir = tmp_path / "raw" / "manifests"
    manifests_dir.mkdir(parents=True)
    sid = "src_0123456789abcdef"
    manifests.save_manifest(manifests_dir, {
        "source_id": sid, "original_filename": "outside.md",
        "relative_raw_path": "raw/inbox/../../outside.md",   # escapes <root>/raw via traversal
        "sha256": "0" * 64, "file_extension": ".md", "ingestion_status": "new",
    })
    summary = _run(tmp_path)
    assert [e["skip_reason"] for e in summary["error_details"]] == ["path_escape"]
