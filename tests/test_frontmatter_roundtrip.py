"""External review 2026-11-11, finding 2: a page-owned item description must survive repeated
render → read → render cycles with its value AND its bytes unchanged.

The renderer escapes `\\`, `"`, `[`, `]` (`_fm_quote`); the readers used `parse_frontmatter`, which only
strips the outer quotes, so every re-render added another layer of backslashes. Every reader of an
item page's description (and the review screens' title) now decodes it like the title
(`frontmatter_string`, the inverse of `_fm_quote`)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.backend import review_read
from app.workers import items, promote
from app.workers.wiki_render import _fm_quote, frontmatter_string, render_item_page
from tests.test_tag_links import _build, _drop, _extract, _item, _keep, _sids

# Already whitespace-collapsed: the renderer collapses runs of whitespace by design.
VALUES = [
    'Say "hi" to C:\\path [x]',
    "a\\\\b",                                   # two backslashes
    'ends with a quote"',
    '"starts with a quote',
    "the users'",                               # trailing apostrophe (parse_frontmatter strips it)
    "'single-quoted'",
    "commas, inside, [[not a link]]",
    "trailing backslash \\",
    "\\\" escaped-looking \\\\\"",
    "Café — naïve 東京 🚀 ∑",
    "colon: and # hash and key: value",
    "Bias, Variance",
]
CYCLES = 5


def _node(description, title='A "tag" \\ [x]'):
    return {"node_id": "itm_0123456789abcdef", "item_type": "domain", "title": title,
            "aliases": ["Alias, one"], "confidence": "low", "source_ids": [], "status": "active",
            "duplicates": [], "description": description}


@pytest.mark.parametrize("value", VALUES)
def test_frontmatter_string_is_the_inverse_of_fm_quote(value):
    text = f'---\ntype: item\ndescription: "{_fm_quote(value)}"\n---\n\n# body\n'
    assert frontmatter_string(text, "description") == value
    assert frontmatter_string(text.replace("\n", "\r\n"), "description") == value   # CRLF tolerated
    assert frontmatter_string(text, "missing") is None


def test_frontmatter_string_reads_only_the_frontmatter():
    text = '---\ntype: item\n---\n\ndescription: "body text"\n'
    assert frontmatter_string(text, "description") is None


def _readers(page: Path, wiki_dir: Path):
    yield "items", items._read_node_meta(page)
    yield "promote", promote._read_meta(page)
    yield "review_read", review_read._family_page_meta(wiki_dir)["itm_0123456789abcdef"]


@pytest.mark.parametrize("value", VALUES)
def test_repeated_render_read_cycles_keep_value_and_bytes(tmp_path, value):
    wiki_dir = tmp_path / "wiki"
    page = wiki_dir / "Items" / "a-tag.md"
    page.parent.mkdir(parents=True)
    node = _node(value)
    first = render_item_page(node)
    page.write_text(first, encoding="utf-8")
    for _ in range(CYCLES):
        for name, meta in _readers(page, wiki_dir):
            assert meta["description"] == value, name
            assert meta["title"] == node["title"], name
            # Re-render from what the reader returned: byte-identical to the first render.
            again = render_item_page({**node, "title": meta["title"], "description": meta["description"]})
            assert again == first, name
        page.write_text(again, encoding="utf-8")
    assert page.read_text(encoding="utf-8") == first


def test_description_survives_the_real_drop_and_revive_rerenders(tmp_path):
    """End to end through the tag writers: keep with a hand-written description, then drop (unused)
    and keep again (revived) twice — each a re-render from the page's own fields."""
    value = 'My "own" words \\ with [brackets], commas and ünïcode'
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _extract(tmp_path, {"alpha": [_item("Topic")], "beta": [_item("Topic")]})
    _keep(tmp_path, a, "Topic", description=value)
    page = tmp_path / "wiki" / "Items" / "topic.md"
    active = page.read_text(encoding="utf-8")
    assert items._read_node_meta(page)["description"] == value

    assert _drop(tmp_path, a, "Topic")["effect"] == "unused"
    assert items._read_node_meta(page)["description"] == value
    seen: dict[str, str] = {}
    for cycle in range(3):
        assert _keep(tmp_path, b, "Topic")["effect"] == "revived"
        assert items._read_node_meta(page)["description"] == value
        revived = page.read_text(encoding="utf-8")
        assert _drop(tmp_path, b, "Topic")["effect"] == "unused"
        assert items._read_node_meta(page)["description"] == value
        unused = page.read_text(encoding="utf-8")
        if cycle:                                                  # same state → same bytes
            assert (revived, unused) == (seen["revived"], seen["unused"])
        seen.update(revived=revived, unused=unused)
    for text in (active, revived, unused):
        assert text.count("\\\\") == 1                             # one escaped backslash, never more
