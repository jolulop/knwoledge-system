from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.workers import extract, intake
from app.backend import manifests
from tests import fixtures


def _extract(tmp_path: Path) -> Path:
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.write_markdown(inbox / "doc.md")
    fixtures.write_html(inbox / "page.html")
    fixtures.write_csv(inbox / "data.csv")
    fixtures.write_pdf(inbox / "paper.pdf", ["Page one prose with several words.",
                                             "Page two prose with several more words."])
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    extract.extract_sources(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    return tmp_path


def _records(tmp_path: Path) -> dict:
    return {m["original_filename"]: m for m in
            manifests.list_manifests(tmp_path / "raw" / "manifests")}


def _read(tmp_path: Path, manifest: dict):
    sid = manifest["source_id"]
    markdown = (tmp_path / "normalized" / "markdown" / f"{sid}.md").read_text(encoding="utf-8")
    chunks = [json.loads(line) for line in
              (tmp_path / "normalized" / "chunks" / f"{sid}.jsonl")
              .read_text(encoding="utf-8").splitlines() if line.strip()]
    return markdown, chunks


def test_char_anchors_resolve_for_every_format(tmp_path):
    _extract(tmp_path)
    for manifest in _records(tmp_path).values():
        markdown, chunks = _read(tmp_path, manifest)
        assert chunks, f"{manifest['original_filename']} produced no chunks"
        for chunk in chunks:
            s, e = chunk["char_start"], chunk["char_end"]
            assert 0 <= s < e <= len(markdown)
            assert markdown[s:e] == chunk["text"]


def test_pdf_chunks_carry_real_page_numbers(tmp_path):
    _extract(tmp_path)
    paper = _records(tmp_path)["paper.pdf"]
    _, chunks = _read(tmp_path, paper)
    pages = {c["page"] for c in chunks}
    assert pages == {1, 2}  # both source pages are represented, mechanically
    for chunk in chunks:
        assert chunk["page"] == chunk["page_end"]  # no chunk spans a page boundary here
        assert chunk["page"] in (1, 2)


def test_non_paginated_formats_have_null_pages(tmp_path):
    _extract(tmp_path)
    records = _records(tmp_path)
    for name in ("doc.md", "page.html", "data.csv"):
        _, chunks = _read(tmp_path, records[name])
        assert all(c["page"] is None and c["page_end"] is None for c in chunks)


def test_csv_table_chunk_references_existing_file(tmp_path):
    _extract(tmp_path)
    csv = _records(tmp_path)["data.csv"]
    _, chunks = _read(tmp_path, csv)
    table_chunks = [c for c in chunks if c["kind"] == "table"]
    assert table_chunks
    for chunk in table_chunks:
        assert chunk["table_reference"]
        assert (tmp_path / chunk["table_reference"]).exists()
        assert chunk["sheet_reference"]
