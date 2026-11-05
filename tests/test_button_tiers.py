"""ADR-0070 B3 — button styling: three tiers (primary solid Rojo, at most one per form; secondary
the plain default; destructive Rojo outline naming the loss, never the Enter default), WCAG 2.1 AA
contrast on every background, forms/routes/behaviour unchanged."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import review_html  # noqa: E402
from tests.test_details import client as details_client  # noqa: E402,F401
from tests.test_details import vault  # noqa: E402,F401
from tests.test_flat_queue import _confirm, queue  # noqa: E402,F401
from tests.test_review_html import _approved_concept_deprecation, client  # noqa: E402,F401

CSS = review_html._STYLE
VAR = dict(re.findall(r"--([a-z-]+):#([0-9A-Fa-f]{6})", CSS))
BACKGROUNDS = {"page": "FFFFFF", "banner": "FFF7E3", "err": "FBEBEF", "ok": "EDF5EC"}


def _lum(hex_):
    c = [int(hex_[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _ratio(a, b):
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _color(value):
    m = re.fullmatch(r"var\(--([a-z-]+)\)", value)
    return VAR[m.group(1)] if m else value.lstrip("#").upper()


def _rule(selector):
    body = re.search(re.escape(selector) + r"\{([^}]*)\}", CSS).group(1)
    return dict(kv.split(":", 1) for kv in body.split(";") if ":" in kv)


@pytest.mark.parametrize("selector", ["button", "button.primary", "button.danger"])
def test_each_tier_meets_wcag_aa(selector):
    base, rule = _rule("button"), _rule(selector)
    style = {**base, **rule}
    text, bg = _color(style["color"]), _color(style["background"])
    border = _color(style["border-color"]) if "border-color" in style else \
        _color(style["border"].split()[-1])
    assert _ratio(text, bg) >= 4.5, (selector, text, bg)             # label on the button
    for name, page_bg in BACKGROUNDS.items():                         # the button's edge
        edge = bg if _ratio(bg, page_bg) >= 3 else border
        assert _ratio(edge, page_bg) >= 3, (selector, name, edge, page_bg)


def _forms(html):
    return re.findall(r"<form\b.*?</form>", html, flags=re.S)


def _buttons(form):
    return re.findall(r"<button\b([^>]*)>(.*?)</button>", form, flags=re.S)


def _check(html):
    """Every form: at most one primary; a destructive button is never the Enter default (the first
    submit button is not destructive — a disabled guard or another button comes first)."""
    for form in _forms(html):
        buttons = _buttons(form)
        assert sum("class='primary'" in attrs for attrs, _l in buttons) <= 1, form
        if any("class='danger'" in attrs for attrs, _l in buttons):
            first_attrs = buttons[0][0]
            assert "class='danger'" not in first_attrs, form
            assert "disabled" in first_attrs or "danger" not in first_attrs


def test_the_queue_and_the_bulk_pages(client, queue):  # noqa: F811
    page = client.get("/ui/reviews").text
    _check(page)
    assert "class='danger' name='bulk_action' value='reject'>Reject selected" in page
    reject = _confirm(client, "reject", ["rev_merge", "rev_promote"]).text
    _check(reject)
    assert "class='danger'>Reject 2 reviews</button>" in reject
    defer = _confirm(client, "defer", ["rev_promote"]).text
    _check(defer)
    assert "class='primary'>Defer 1 review</button>" in defer


def test_a_review_page_approve_is_primary_and_reject_destructive(client, queue):  # noqa: F811
    html = client.get("/ui/reviews/rev_promote").text
    _check(html)
    assert "class='primary' name='action' value='approve'" in html
    assert "class='danger' name='action' value='reject'" in html


def test_the_apply_pages(client, tmp_path):  # noqa: F811
    _approved_concept_deprecation(tmp_path)
    preview = client.post("/ui/reviews/apply/preview").text
    _check(preview)
    assert "<button type='submit' class='primary'>Apply now</button>" in preview


def test_delete_unused_names_the_loss():
    html = review_html.render_unused_tags([{"title": "A", "node_id": "itm_a"},
                                           {"title": "B", "node_id": "itm_b"},
                                           {"title": "C", "node_id": "itm_c", "protected": "merge"}])
    _check(html)
    assert "class='danger'>Delete 2 unused tags</button>" in html
    one = review_html.render_unused_tags([{"title": "A", "node_id": "itm_a"}])
    assert "Delete 1 unused tag</button>" in one


def test_the_document_screen_has_one_primary_save(details_client, vault):  # noqa: F811
    root, sid = vault
    html = details_client.get(f"/ui/reviews/sources/{sid}").text
    _check(html)
    assert html.count("class='primary'") == 1 and "Save all changes on " in html
    assert "class='danger'" not in html                     # nothing destructive on the screen


def test_an_invalid_subject_review_guards_enter_before_reject(client, tmp_path):  # noqa: F811
    """With approval disabled, Reject is the first decision button — the disabled guard comes first."""
    from tests.test_flat_queue import _review
    _review(tmp_path, "rev_bad", "hide_content", {"source_id": "../../etc/passwd"})
    html = client.get("/ui/reviews/rev_bad").text
    assert "approval is disabled" in html
    _check(html)
    form = next(f for f in _forms(html) if "value='reject'" in f)
    assert "disabled" in _buttons(form)[0][0]
