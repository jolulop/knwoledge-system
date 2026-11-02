"""UAT part 3 finding #10 (2026-11-02): the Apply screen explains an empty approved queue, and the
result lists only what changed."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.backend import main as main_module  # noqa: E402
from app.backend import review_html  # noqa: E402
from app.backend.config import get_settings  # noqa: E402


def test_the_apply_screen_explains_an_empty_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "settings", get_settings(tmp_path))
    html = TestClient(main_module.app).get("/ui/reviews/apply").text
    assert "Nothing is waiting to be applied." in html and ">Preview anyway<" in html
    full = review_html.render_apply_confirm({"executor_backed": {"merge_items": 1},
                                             "record_only": {}})
    assert "Nothing is waiting" not in full and ">Preview<" in full


def test_the_apply_result_lists_only_what_changed():
    empty = review_html.render_apply_result({"status": "applied", "validators_ok": True,
                                             "summary": {"merged": 0, "retyped": 0,
                                                         "pages_changed": False}})
    assert "Nothing was changed" in empty and "retyped" not in empty
    some = review_html.render_apply_result({"status": "applied", "validators_ok": True,
                                            "summary": {"merged": 1, "retyped": 0}})
    assert "merged" in some and "retyped" not in some and "Nothing was changed" not in some
