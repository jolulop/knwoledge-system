from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import validate_wiki as v  # noqa: E402
from app.workers import wiki_render  # noqa: E402

SID = "src_validatewiki01"
TEMPLATE = (ROOT / "templates" / "source.md").read_text(encoding="utf-8")
NOW = "2026-01-01T00:00:00+00:00"


def _setup(tmp: Path, *, status: str = "extracted") -> dict:
    (tmp / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    (tmp / "wiki" / "Sources").mkdir(parents=True, exist_ok=True)
    manifest = {
        "source_id": SID,
        "original_filename": "doc.pdf",
        "relative_raw_path": "raw/inbox/doc.pdf",
        "sha256": "a" * 64,
        "file_extension": ".pdf",
        "page_count": 2,
        "chunk_count": 3,
        "ingestion_status": status,
        "created_at": NOW,
        "discovered_at": NOW,
        "normalized": {"markdown_path": f"normalized/markdown/{SID}.md"},
    }
    (tmp / "raw" / "manifests" / f"{SID}.json").write_text(json.dumps(manifest), encoding="utf-8")
    page = wiki_render.render_source_page(
        TEMPLATE, manifest, "A sufficiently long opening paragraph of real prose here.",
        summary_max=320, summary_min=40,
    )
    (tmp / "wiki" / "Sources" / f"{SID}.md").write_text(page, encoding="utf-8")
    return manifest


def _page(tmp: Path) -> Path:
    return tmp / "wiki" / "Sources" / f"{SID}.md"


def test_valid_layout_passes(tmp_path):
    _setup(tmp_path)
    assert v.main([str(tmp_path)]) == 0


def test_no_manifests_passes(tmp_path):
    assert v.main([str(tmp_path)]) == 0


def test_missing_frontmatter_field_fails(tmp_path):
    _setup(tmp_path)
    page = _page(tmp_path)
    text = "\n".join(ln for ln in page.read_text().splitlines() if not ln.startswith("sha256:"))
    page.write_text(text, encoding="utf-8")
    assert v.main([str(tmp_path)]) == 1


def test_id_filename_mismatch_fails(tmp_path):
    _setup(tmp_path)
    _page(tmp_path).rename(tmp_path / "wiki" / "Sources" / "src_wrongwrong00000.md")
    assert v.main([str(tmp_path)]) == 1


def test_orphan_page_without_manifest_fails(tmp_path):
    _setup(tmp_path)
    (tmp_path / "wiki" / "Sources" / "src_orphan000000000.md").write_text(
        _page(tmp_path).read_text(), encoding="utf-8"
    )
    assert v.main([str(tmp_path)]) == 1


def test_stale_page_for_error_manifest_fails(tmp_path):
    manifest = _setup(tmp_path, status="extracted")
    manifest["ingestion_status"] = "error"
    (tmp_path / "raw" / "manifests" / f"{SID}.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert v.main([str(tmp_path)]) == 1


def test_coverage_gap_fails(tmp_path):
    _setup(tmp_path)
    _page(tmp_path).unlink()
    assert v.main([str(tmp_path)]) == 1


def test_absolute_path_leak_fails(tmp_path):
    _setup(tmp_path)
    page = _page(tmp_path)
    page.write_text(page.read_text() + "\nSee /home/user/secret.pdf\n", encoding="utf-8")
    assert v.main([str(tmp_path)]) == 1


def test_unlabelled_stub_summary_fails(tmp_path):
    _setup(tmp_path)
    page = _page(tmp_path)
    text = page.read_text().replace("> [!summary] Extractive excerpt (auto-generated, unverified)", "> [!summary]")
    page.write_text(text, encoding="utf-8")
    assert v.main([str(tmp_path)]) == 1


def test_dangling_wikilink_fails(tmp_path):
    _setup(tmp_path)
    page = _page(tmp_path)
    page.write_text(page.read_text() + "\n- [[Concepts/does-not-exist]]\n", encoding="utf-8")
    assert v.main([str(tmp_path)]) == 1


def test_normalized_path_mismatch_fails(tmp_path):
    _setup(tmp_path)
    page = _page(tmp_path)
    page.write_text(page.read_text().replace("normalized/markdown/", "normalized/WRONG/"), encoding="utf-8")
    assert v.main([str(tmp_path)]) == 1


def test_page_count_mismatch_fails(tmp_path):
    _setup(tmp_path)  # manifest page_count == 2
    page = _page(tmp_path)
    page.write_text(page.read_text().replace("page_count: 2", "page_count: 99"), encoding="utf-8")
    assert v.main([str(tmp_path)]) == 1


def test_invalid_lifecycle_status_fails(tmp_path):
    _setup(tmp_path)
    page = _page(tmp_path)
    page.write_text(page.read_text().replace("status: active", "status: bogus"), encoding="utf-8")
    assert v.main([str(tmp_path)]) == 1
