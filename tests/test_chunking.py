from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.workers.chunking import Element, assemble

SID = "src_0123456789abcdef"


def _assemble(elements, target=1000, max_chars=2000):
    return assemble(SID, elements, target_chars=target, max_chars=max_chars)


def test_heading_starts_a_new_section_and_chunk():
    elements = [
        Element(kind="heading", text="Alpha", level=1),
        Element(kind="prose", text="First section body."),
        Element(kind="heading", text="Beta", level=2),
        Element(kind="prose", text="Second section body."),
    ]
    markdown, chunks = _assemble(elements)

    assert [c.section for c in chunks] == ["Alpha", "Beta"]
    assert chunks[0].heading_path == ["Alpha"]
    assert chunks[1].heading_path == ["Alpha", "Beta"]
    # Headings are present in the markdown but are not themselves chunks.
    assert "# Alpha" in markdown and "## Beta" in markdown
    assert all(c.kind == "prose" for c in chunks)


def test_anchors_slice_back_to_chunk_text():
    elements = [
        Element(kind="heading", text="Title", level=1),
        Element(kind="prose", text="Paragraph one."),
        Element(kind="prose", text="Paragraph two."),
    ]
    markdown, chunks = _assemble(elements, target=10)  # force one chunk per paragraph
    for chunk in chunks:
        assert markdown[chunk.char_start : chunk.char_end] == chunk.text
        assert 0 <= chunk.char_start < chunk.char_end <= len(markdown)


def test_ordinals_are_contiguous_and_ids_padded():
    elements = [Element(kind="prose", text=f"Para {i}.") for i in range(5)]
    _, chunks = _assemble(elements, target=1)  # each paragraph its own chunk
    assert [c.ordinal for c in chunks] == [0, 1, 2, 3, 4]
    assert chunks[0].chunk_id == f"{SID}::0000"
    assert chunks[4].chunk_id == f"{SID}::0004"


def test_chunking_is_deterministic():
    elements = [
        Element(kind="heading", text="H", level=1),
        Element(kind="prose", text="Some content here."),
        Element(kind="prose", text="More content follows."),
    ]
    first_md, first_chunks = _assemble(elements)
    second_md, second_chunks = _assemble(elements)
    assert first_md == second_md
    assert [c.to_dict() for c in first_chunks] == [c.to_dict() for c in second_chunks]


def test_size_cap_never_exceeds_max():
    # One huge paragraph (no headings) must split, and no chunk may exceed max.
    sentence = "This is a sentence. "
    big = sentence * 400  # ~8000 chars
    _, chunks = _assemble([Element(kind="prose", text=big)], target=1000, max_chars=2000)
    assert len(chunks) > 1
    assert all(c.char_end - c.char_start <= 2000 for c in chunks)
    # Splits land on sentence boundaries, never mid-sentence.
    for chunk in chunks[:-1]:
        assert chunk.text.rstrip().endswith(".")


def test_small_paragraphs_pack_together():
    elements = [Element(kind="prose", text="tiny.") for _ in range(6)]
    _, chunks = _assemble(elements, target=1000)
    # Six tiny paragraphs fit under the target and merge into a single chunk.
    assert len(chunks) == 1
    assert chunks[0].ordinal == 0


def test_table_element_becomes_a_table_chunk():
    elements = [
        Element(kind="heading", text="Sheet1", level=1),
        Element(
            kind="table",
            text="| a | b |\n| --- | --- |\n| 1 | 2 |",
            table_reference="normalized/tables/src_x/0.csv",
            sheet_reference="Sheet1",
        ),
    ]
    markdown, chunks = _assemble(elements)
    assert len(chunks) == 1
    table = chunks[0]
    assert table.kind == "table"
    assert table.table_reference == "normalized/tables/src_x/0.csv"
    assert table.sheet_reference == "Sheet1"
    assert markdown[table.char_start : table.char_end] == table.text
