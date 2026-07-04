"""ADR-0054: PDF line-break de-hyphenation at extraction.

Unit matrix for the ``dehyphenate`` contract plus an end-to-end worker run proving the
normalized Markdown is born clean, the citation anchor contract survives the repair, and
every extraction log records ``extract_code_version``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import manifests
from app.workers import extract, intake
from app.workers.extractors import dehyphenate
from tests import fixtures

SOFT = chr(0xAD)  # U+00AD soft hyphen (kept out of source literals deliberately)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Rule 1: both boundary chars lowercase letters -> drop hyphen and break.
        ("con-\ntributions", "contributions"),
        ("tecno-\nlogía", "tecnología"),  # Unicode lowercase (Spanish, spec §2.4)
        # Rule 2: anything else -> keep hyphen, drop only the break.
        ("COVID-\n19", "COVID-19"),
        ("anti-\nAmerican", "anti-American"),
        ("5-\n3", "5-3"),
        # Whitespace legs around the break are absorbed.
        ("con- \n tributions", "contributions"),
        ("con-\t\n\ttributions", "contributions"),
        # Consecutive hyphenated lines repair in one pass (lookahead capture).
        ("in-\nter-\nnal", "internal"),
        # Paragraph-bounded: a blank-line paragraph break is never crossed.
        ("ends with hyphen-\n\nnew paragraph", "ends with hyphen-\n\nnew paragraph"),
        # No word-char on a side -> untouched (spaced dash stays prose punctuation).
        ("a - dash\nnot a split", "a - dash\nnot a split"),
        # Underscore is not a word-char per the ADR-0054 contract (Unicode alphanumeric).
        ("snake_-\ncase", "snake_-\ncase"),
        # Soft hyphens strip anywhere; at a line break they repair like a hyphen —
        # both branches: lowercase drops the hyphen, otherwise a hard "-" is kept.
        (SOFT.join(["dis", "cuss"]), "discuss"),
        ("con" + SOFT + "\ntributions", "contributions"),
        ("COVID" + SOFT + "\n19", "COVID-19"),
        # Accepted error class (pinned deliberately, ADR-0054): a lowercase compound
        # split at its real hyphen loses it — the documented tradeoff, not a bug.
        ("best-\nknown", "bestknown"),
    ],
)
def test_dehyphenate_contract(raw, expected):
    assert dehyphenate(raw) == expected


def test_pdf_extraction_dehyphenates_end_to_end(tmp_path):
    pytest.importorskip("pypdf")
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.write_pdf(inbox / "paper.pdf", [[
        "This paper aims to make three primary con-",
        "tributions to the field of study today.",
    ]])
    fixtures.write_markdown(inbox / "doc.md")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")

    summary = extract.extract_sources(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    assert summary["extracted"] == 2

    records = {m["original_filename"]: m for m in
               manifests.list_manifests(tmp_path / "raw" / "manifests")}
    pdf_id = records["paper.pdf"]["source_id"]

    markdown = (tmp_path / "normalized" / "markdown" / f"{pdf_id}.md").read_text(encoding="utf-8")
    assert "contributions" in markdown
    assert "con- tributions" not in markdown and "con-\ntributions" not in markdown

    # The citation anchor contract survives the repair: chunk text is a verbatim slice.
    chunk_lines = (tmp_path / "normalized" / "chunks" / f"{pdf_id}.jsonl").read_text(
        encoding="utf-8").splitlines()
    assert chunk_lines
    for line in chunk_lines:
        rec = json.loads(line)
        assert markdown[rec["char_start"]:rec["char_end"]] == rec["text"]

    # Every extraction log (PDF and non-PDF alike) records extractor provenance
    # (ADR-0054 decision 4: observability only).
    for m in records.values():
        log = json.loads((tmp_path / "normalized" / "extraction_logs" /
                          f"{m['source_id']}.json").read_text(encoding="utf-8"))
        assert log["extract_code_version"] == extract.EXTRACT_CODE_VERSION


def test_error_extraction_log_carries_extract_code_version(tmp_path):
    # The marker is set in the initial log dict, before any extraction work — so a failed
    # extraction's log still records which extractor implementation produced it.
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.write_markdown(inbox / "doc.md")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")

    # max_file_mb=0 makes every non-empty file oversize -> error path, log still written.
    summary = extract.extract_sources(
        tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite", max_file_mb=0)
    assert summary["errors"] == 1

    source_id = manifests.list_manifests(tmp_path / "raw" / "manifests")[0]["source_id"]
    log = json.loads((tmp_path / "normalized" / "extraction_logs" /
                      f"{source_id}.json").read_text(encoding="utf-8"))
    assert log["status"] == "error"
    assert log["extract_code_version"] == extract.EXTRACT_CODE_VERSION
