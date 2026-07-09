from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import validate_frontmatter as vf  # noqa: E402


def _write(wiki: Path, subdir: str, name: str, fm: dict) -> None:
    folder = wiki / subdir
    folder.mkdir(parents=True, exist_ok=True)
    body = "---\n" + "".join(f"{k}: {v}\n" for k, v in fm.items()) + "---\n\n> [!summary] s\n\nBody.\n"
    (folder / name).write_text(body, encoding="utf-8")


def _item_fm(**over):
    fm = {"type": "item", "item_id": "itm_x", "item_type": "method_technique", "title": "Thing",
          "status": "active", "confidence": "low", "review_status": "none"}
    fm.update(over)
    return fm


def _source_fm(**over):
    fm = {"type": "source", "source_id": "src_0123456789abcdef", "title": "Doc",
          "relative_raw_path": "raw/inbox/doc.pdf", "normalized_path": "normalized/markdown/x.md",
          "sha256": "a" * 64, "file_type": "pdf", "status": "active", "ingestion_status": "extracted",
          "summary_status": "stub", "generation_status": "deterministic", "input_fingerprint": "fp"}
    fm.update(over)
    return fm


def test_valid_item_with_review_status_passes(tmp_path):
    _write(tmp_path / "wiki", "Items", "c.md", _item_fm())
    assert vf.main([str(tmp_path)]) == 0


def test_item_out_of_set_review_status_fails(tmp_path):
    # `deferred` is a review-ledger state, never a page value (ADR-0022).
    _write(tmp_path / "wiki", "Items", "c.md", _item_fm(review_status="deferred"))
    assert vf.main([str(tmp_path)]) == 1


def test_rendering_type_missing_review_status_fails(tmp_path):
    fm = _item_fm()
    del fm["review_status"]
    _write(tmp_path / "wiki", "Items", "c.md", fm)
    assert vf.main([str(tmp_path)]) == 1


def test_item_missing_item_type_fails(tmp_path):
    # ADR-0059: the governed classification is a REQUIRED item-page field.
    fm = _item_fm()
    del fm["item_type"]
    _write(tmp_path / "wiki", "Items", "c.md", fm)
    assert vf.main([str(tmp_path)]) == 1


def test_source_without_review_status_passes(tmp_path):
    _write(tmp_path / "wiki", "Sources", "s.md", _source_fm())
    assert vf.main([str(tmp_path)]) == 0


def test_source_with_review_status_fails(tmp_path):
    # Strict: Source pages must not carry review_status at all, even an in-set value (it is owned by no
    # renderer; review state lives in the ledger).
    _write(tmp_path / "wiki", "Sources", "s.md", _source_fm(review_status="approved"))
    assert vf.main([str(tmp_path)]) == 1


# Every page type that renders review_status (NOT source/tag), with its other required fields + subdir.
_RENDERING_PAGES = {
    "claim": ("Claims", {"type": "claim", "claim_id": "clm_x", "status": "active", "confidence": "low"}),
    "item": ("Items", {"type": "item", "item_id": "itm_x", "item_type": "method_technique",
                       "title": "T", "status": "active", "confidence": "low"}),
    "synthesis": ("Synthesis", {"type": "synthesis", "synthesis_id": "syn_x", "title": "T",
                                "status": "candidate"}),
    "query": ("Queries", {"type": "query", "query_id": "qry_x", "title": "T",
                          "question": "Q?", "status": "active"}),
}


@pytest.mark.parametrize("ptype", sorted(_RENDERING_PAGES))
def test_rendering_type_requires_review_status(tmp_path, ptype):
    # Pins the whole declared contract: each rendering type passes WITH review_status and fails WITHOUT.
    subdir, base = _RENDERING_PAGES[ptype]
    _write(tmp_path / "wiki", subdir, "p.md", {**base, "review_status": "none"})
    assert vf.main([str(tmp_path)]) == 0, f"{ptype} should pass with review_status"
    _write(tmp_path / "wiki", subdir, "p.md", base)
    assert vf.main([str(tmp_path)]) == 1, f"{ptype} should fail without review_status"


@pytest.mark.parametrize("value", ["none", "pending", "approved", "rejected"])
def test_all_page_review_status_values_pass(tmp_path, value):
    _write(tmp_path / "wiki", "Items", "c.md", _item_fm(review_status=value))
    assert vf.main([str(tmp_path)]) == 0
