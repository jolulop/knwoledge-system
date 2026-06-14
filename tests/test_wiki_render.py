from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.workers import wiki_render

NOW = "2026-01-02T03:04:05+00:00"


def _manifest(**over):
    base = {
        "source_id": "src_0123456789abcdef",
        "original_filename": "Big Report-2026_final.pdf",
        "relative_raw_path": "raw/inbox/Big Report-2026_final.pdf",
        "sha256": "a" * 64,
        "file_extension": ".pdf",
        "page_count": 4,
        "chunk_count": 12,
        "ingestion_status": "extracted",
        "created_at": "2026-01-01T00:00:00+00:00",
        "discovered_at": "2026-01-01T12:00:00+00:00",
        "normalized": {"markdown_path": "normalized/markdown/src_0123456789abcdef.md"},
    }
    base.update(over)
    return base


def test_render_template_strict_unknown_token_errors():
    with pytest.raises(KeyError):
        wiki_render.render_template("hello {{missing}}", {"present": "x"})
    assert wiki_render.render_template("a {{x}} b", {"x": 1}) == "a 1 b"


def test_title_from_filename():
    assert wiki_render.title_from_filename("ai-is_rewriting.pdf") == "ai is rewriting"
    assert wiki_render.title_from_filename("report.final.docx") == "report.final"


def test_parse_frontmatter_strips_inline_comments():
    text = '---\nstatus: active   # a comment\ntags: []\n---\nbody\n'
    fm = wiki_render.parse_frontmatter(text)
    assert fm["status"] == "active"
    assert fm["tags"] == []


def test_summary_excerpt_extractive():
    md = "# Heading\n\nThis is the first real paragraph with enough text to use. More.\n"
    out = wiki_render.summary_excerpt(md, "T", 2, 5, max_chars=320, min_chars=20)
    assert out.startswith("This is the first real paragraph")
    assert "#" not in out


def test_summary_excerpt_structural_fallback_for_sparse_text():
    out = wiki_render.summary_excerpt("", "My Doc", None, 0, max_chars=320, min_chars=40)
    assert out == "Source: My Doc. unknown pages, 0 chunks."


def test_summary_excerpt_truncates_on_sentence_boundary():
    md = "First sentence here. " + ("padding word " * 50)
    out = wiki_render.summary_excerpt(md, "T", 1, 1, max_chars=40, min_chars=10)
    assert out.endswith(".") or out.endswith("…")
    assert len(out) <= 41


def test_render_source_page_is_clean_and_complete():
    template = (ROOT / "templates" / "source.md").read_text(encoding="utf-8")
    md = "# Title\n\nA sufficiently long opening paragraph of real prose content here.\n"
    page = wiki_render.render_source_page(
        template, _manifest(), md, summary_max=320, summary_min=40, now=NOW,
    )
    # No unrendered tokens, no absolute paths, relative path present.
    assert "{{" not in page and "}}" not in page
    assert "/home/" not in page
    # No absolute raw_path field (relative_raw_path contains the substring, so match a line).
    assert not any(ln.startswith("raw_path:") for ln in page.splitlines())
    assert "relative_raw_path:" in page
    assert "> [!summary] Extractive excerpt" in page
    fm = wiki_render.parse_frontmatter(page)
    assert fm["source_id"] == "src_0123456789abcdef"
    assert fm["status"] == "active"
    assert fm["ingestion_status"] == "extracted"
    assert fm["summary_status"] == "stub"
    assert fm["generation_status"] == "deterministic"


def test_render_is_deterministic_for_fixed_now():
    template = (ROOT / "templates" / "source.md").read_text(encoding="utf-8")
    md = "# T\n\nStable body paragraph long enough for an extractive summary stub.\n"
    a = wiki_render.render_source_page(template, _manifest(), md, summary_max=320, summary_min=40, now=NOW)
    b = wiki_render.render_source_page(template, _manifest(), md, summary_max=320, summary_min=40, now=NOW)
    assert a == b
