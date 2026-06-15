from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.workers import reviews


def _pending(reviews_dir):
    return sorted((reviews_dir / "pending").glob("*.json"))


def test_create_review_item_is_idempotent(tmp_path):
    rdir = tmp_path / "reviews"
    rid = reviews.create_review_item(rdir, review_type="promote_candidate_node",
                                     subject={"node_id": "cpt_x"}, proposal={}, now="t")
    page = rdir / "pending" / f"{rid}.json"
    assert page.exists() and json.loads(page.read_text())["status"] == "pending"

    rid2 = reviews.create_review_item(rdir, review_type="promote_candidate_node",
                                      subject={"node_id": "cpt_x"}, proposal={"changed": True}, now="t2")
    assert rid2 == rid and len(_pending(rdir)) == 1
    assert json.loads(page.read_text())["proposal"] == {}  # original not overwritten


def test_deferred_item_is_not_recreated(tmp_path):
    rdir = tmp_path / "reviews"
    rid = reviews.create_review_item(rdir, review_type="deprecate_wiki_page",
                                     subject={"node_id": "clm_x"}, proposal={}, now="t")
    page = rdir / "pending" / f"{rid}.json"
    # A human defers it: status flips, file stays in pending/ (no deferred/ dir).
    data = json.loads(page.read_text())
    data["status"] = "deferred"
    page.write_text(json.dumps(data), encoding="utf-8")

    rid2 = reviews.create_review_item(rdir, review_type="deprecate_wiki_page",
                                      subject={"node_id": "clm_x"}, proposal={}, now="t3")
    assert rid2 == rid
    assert len(_pending(rdir)) == 1
    assert json.loads(page.read_text())["status"] == "deferred"  # deferral preserved


def test_unknown_review_type_rejected(tmp_path):
    with pytest.raises(ValueError):
        reviews.create_review_item(tmp_path / "reviews", review_type="bogus",
                                   subject={}, proposal={})
