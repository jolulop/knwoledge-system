from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import db, manifests
from app.workers import extract, intake, wiki
from tests import fixtures

TEMPLATES = ROOT / "templates"


def _build(tmp_path: Path) -> Path:
    """Intake + extract a small corpus: an extracted md, a partial (empty) html, an
    unsupported .txt that stays `new`."""
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.write_markdown(inbox / "doc.md")
    (inbox / "empty.html").write_text("<html><body></body></html>", encoding="utf-8")
    (inbox / "notes.txt").write_text("unsupported format\n", encoding="utf-8")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    extract.extract_sources(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    return tmp_path


def _gen(tmp_path: Path, **kw):
    return wiki.generate_wiki(
        tmp_path,
        jobs_db=tmp_path / "db" / "jobs.sqlite",
        templates_dir=TEMPLATES,
        rebuild_index=False,
        **kw,
    )


def _status(tmp_path, name):
    for m in manifests.list_manifests(tmp_path / "raw" / "manifests"):
        if m["original_filename"] == name:
            return m
    return None


def test_generates_pages_for_extracted_and_partial_only(tmp_path):
    _build(tmp_path)
    summary = _gen(tmp_path)

    # md is extracted, empty.html is partial → 2 considered/generated; .txt is new.
    assert summary["sources_considered"] == 2
    assert summary["generated"] == 2
    assert summary["skipped_not_extracted"] == 1
    assert summary["errors"] == 0

    pages = sorted((tmp_path / "wiki" / "Sources").glob("*.md"))
    assert len(pages) == 2

    txt = _status(tmp_path, "notes.txt")
    assert not (tmp_path / "wiki" / "Sources" / f"{txt['source_id']}.md").exists()

    # Each page's frontmatter is clean and consistent with its manifest.
    for page in pages:
        from app.workers.wiki_render import parse_frontmatter
        fm = parse_frontmatter(page.read_text(encoding="utf-8"))
        assert fm["status"] == "active"
        assert fm["ingestion_status"] in {"extracted", "partial"}
        assert "/home/" not in page.read_text(encoding="utf-8")


def test_idempotent_skip_and_force(tmp_path):
    _build(tmp_path)
    first = _gen(tmp_path)
    assert first["generated"] == 2

    second = _gen(tmp_path)
    assert second["generated"] == 0
    assert second["skipped_unchanged"] == 2

    forced = _gen(tmp_path, force=True)
    assert forced["generated"] == 2
    assert forced["skipped_unchanged"] == 0


def test_pages_are_byte_stable(tmp_path):
    _build(tmp_path)
    sid = _status(tmp_path, "doc.md")["source_id"]
    page = tmp_path / "wiki" / "Sources" / f"{sid}.md"

    _gen(tmp_path, force=True)
    first = page.read_text(encoding="utf-8")
    _gen(tmp_path, force=True)
    second = page.read_text(encoding="utf-8")
    # Deterministic: no wall-clock timestamp, so force-regen is byte-identical (ADR-0023).
    assert first == second
    assert "last_compiled_at" not in first


def test_generate_job_recorded_and_log_appended(tmp_path):
    _build(tmp_path)
    summary = _gen(tmp_path)

    conn = db.connect(tmp_path / "db" / "jobs.sqlite")
    try:
        job = db.get_job(conn, summary["job_id"])
    finally:
        conn.close()
    assert job is not None
    assert job["job_type"] == "generate_wiki"
    assert job["status"] == "succeeded"
    assert job["metadata"]["generated"] == 2

    log = (tmp_path / "wiki" / "log.md").read_text(encoding="utf-8")
    assert "generate_wiki" in log


def test_does_not_modify_raw_or_normalized(tmp_path):
    _build(tmp_path)
    raw = tmp_path / "raw" / "inbox"
    before_raw = {p.name: p.read_bytes() for p in raw.iterdir() if p.is_file()}
    before_norm = {p.name: p.read_bytes()
                   for p in (tmp_path / "normalized" / "markdown").glob("*.md")}
    _gen(tmp_path)
    after_raw = {p.name: p.read_bytes() for p in raw.iterdir() if p.is_file()}
    after_norm = {p.name: p.read_bytes()
                  for p in (tmp_path / "normalized" / "markdown").glob("*.md")}
    assert before_raw == after_raw
    assert before_norm == after_norm
