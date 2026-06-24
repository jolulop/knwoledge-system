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

import validate_normalized as v  # noqa: E402

SID = "src_00000000000000fa"


def _make_extracted(root: Path, *, page: int | None = None, page_count: int | None = None):
    """Lay down a minimal, internally-consistent extracted source on disk."""
    (root / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    for sub in ("markdown", "chunks", "extraction_logs", "tables"):
        (root / "normalized" / sub).mkdir(parents=True, exist_ok=True)

    markdown = "# H\n\nHello world body text.\n"
    text = "Hello world body text."
    start = markdown.index(text)
    (root / "normalized" / "markdown" / f"{SID}.md").write_text(markdown, encoding="utf-8")
    chunk = {
        "chunk_id": f"{SID}::0000", "source_id": SID, "ordinal": 0, "kind": "prose",
        "heading_path": ["H"], "section": "H", "text": text,
        "char_start": start, "char_end": start + len(text),
        "page": page, "page_end": page, "table_reference": None, "sheet_reference": None,
    }
    (root / "normalized" / "chunks" / f"{SID}.jsonl").write_text(
        json.dumps(chunk) + "\n", encoding="utf-8"
    )
    (root / "normalized" / "extraction_logs" / f"{SID}.json").write_text("{}\n", encoding="utf-8")
    (root / "normalized" / "tables" / SID).mkdir(exist_ok=True)

    manifest = {
        "source_id": SID, "ingestion_status": "extracted",
        "page_count": page_count, "chunk_count": 1,
        "normalized": {
            "markdown_path": f"normalized/markdown/{SID}.md",
            "chunks_path": f"normalized/chunks/{SID}.jsonl",
            "tables_dir": f"normalized/tables/{SID}",
            "extraction_log_path": f"normalized/extraction_logs/{SID}.json",
        },
    }
    (root / "raw" / "manifests" / f"{SID}.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def _write_manifest(root: Path, manifest: dict):
    (root / "raw" / "manifests" / f"{manifest['source_id']}.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def test_valid_layout_passes(tmp_path):
    _make_extracted(tmp_path)
    assert v.main([str(tmp_path)]) == 0


def test_missing_extraction_log_fails(tmp_path):
    _make_extracted(tmp_path)
    (tmp_path / "normalized" / "extraction_logs" / f"{SID}.json").unlink()
    assert v.main([str(tmp_path)]) == 1


def test_missing_tables_dir_fails(tmp_path):
    _make_extracted(tmp_path)
    (tmp_path / "normalized" / "tables" / SID).rmdir()
    assert v.main([str(tmp_path)]) == 1


def test_bad_page_range_fails(tmp_path):
    # Paginated source claims 1 page but a chunk cites page 5.
    _make_extracted(tmp_path, page=5, page_count=1)
    assert v.main([str(tmp_path)]) == 1


def test_missing_normalized_paths_fails(tmp_path):
    manifest = _make_extracted(tmp_path)
    manifest["normalized"] = {"markdown_path": f"normalized/markdown/{SID}.md"}  # incomplete
    _write_manifest(tmp_path, manifest)
    assert v.main([str(tmp_path)]) == 1


def test_orphan_output_fails(tmp_path):
    _make_extracted(tmp_path)
    # A normalized markdown file with no manifest at all.
    (tmp_path / "normalized" / "markdown" / "src_orphan00000000.md").write_text("# x\n", encoding="utf-8")
    assert v.main([str(tmp_path)]) == 1


def test_stale_output_for_error_manifest_fails(tmp_path):
    manifest = _make_extracted(tmp_path)
    # Manifest flips to error but its normalized artifacts are still on disk.
    manifest["ingestion_status"] = "error"
    _write_manifest(tmp_path, manifest)
    assert v.main([str(tmp_path)]) == 1


def test_no_manifests_is_a_pass(tmp_path):
    assert v.main([str(tmp_path)]) == 0


def test_escaping_normalized_path_rejected(tmp_path, capsys):
    _make_extracted(tmp_path)
    (tmp_path / "outside.md").write_text("secret outside normalized/\n", encoding="utf-8")
    m = json.loads((tmp_path / "raw" / "manifests" / f"{SID}.json").read_text())
    m["normalized"]["markdown_path"] = "../outside.md"  # escapes; != the fixed content-keyed layout
    _write_manifest(tmp_path, m)
    assert v.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "does not match fixed layout" in out   # derive-and-equal rejects it; outside.md never read
    assert "secret" not in out


def test_contained_but_wrong_cross_source_path_fails(tmp_path, capsys):
    # A path that stays under normalized/ but points at ANOTHER source's file must still fail.
    _make_extracted(tmp_path)
    m = json.loads((tmp_path / "raw" / "manifests" / f"{SID}.json").read_text())
    m["normalized"]["chunks_path"] = "normalized/chunks/src_00000000000000ff.jsonl"
    _write_manifest(tmp_path, m)
    assert v.main([str(tmp_path)]) == 1
    assert "does not match fixed layout" in capsys.readouterr().out


def test_noncanonical_manifest_source_id_fails(tmp_path):
    _make_extracted(tmp_path)
    bad = json.loads((tmp_path / "raw" / "manifests" / f"{SID}.json").read_text())
    bad["source_id"] = "../evil"
    (tmp_path / "raw" / "manifests" / "evil.json").write_text(json.dumps(bad), encoding="utf-8")
    assert v.main([str(tmp_path)]) == 1
