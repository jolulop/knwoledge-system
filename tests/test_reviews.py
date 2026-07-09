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


_INTENDED_REVIEW_TYPES = frozenset({
    "delete_raw_file", "archive_source", "deprecate_wiki_page", "resolve_contradiction",
    "merge_items", "split_item", "mark_semantic_duplicate", "hide_content",
    "hide_semantic_page", "unhide_content", "unhide_semantic_page", "hide_claim", "unhide_claim",
    "hide_synthesis", "unhide_synthesis",
    "promote_candidate_node", "change_item_type", "propose_synthesis",
    "missing_raw_source", "purge_response_cache",
})


def test_review_vocabulary_is_the_intended_set():
    # Pinned so a stray/removed type can't drift back in. ADR-0059 collapsed the typed-identity
    # vocabulary: merge_entities + merge_concepts -> merge_items, split_entity -> split_item,
    # change_entity_subtype -> change_item_type. `promote_single_source_claim_to_concept` stays
    # removed as dead vocabulary; promote_candidate_node is the canonical early/manual promotion type.
    assert reviews.REVIEW_TYPES == _INTENDED_REVIEW_TYPES
    assert "promote_single_source_claim_to_concept" not in reviews.REVIEW_TYPES
    for gone in ("merge_entities", "merge_concepts", "split_entity", "change_entity_subtype"):
        assert gone not in reviews.REVIEW_TYPES, gone


def test_review_policy_matches_vocabulary():
    # policies/review.yaml::requires_human_approval must stay in lockstep with REVIEW_TYPES.
    from app.backend.policy import load_yaml
    policy = load_yaml((ROOT / "policies" / "review.yaml").read_text(encoding="utf-8"))
    assert set(policy["requires_human_approval"]) == reviews.REVIEW_TYPES


def test_create_review_item_rejects_removed_type(tmp_path):
    with pytest.raises(ValueError):
        reviews.create_review_item(tmp_path / "reviews",
                                   review_type="promote_single_source_claim_to_concept",
                                   subject={}, proposal={}, now="t")


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


def test_withdraw_only_touches_pending_and_preserves_audit_history(tmp_path):
    rdir = tmp_path / "reviews"
    subj = {"claim_a": "clm_a", "claim_b": "clm_b"}
    # A withdraw/re-file/withdraw cycle keeps a distinct audit entry per withdrawal.
    for i in range(2):
        rid = reviews.create_review_item(rdir, review_type="resolve_contradiction",
                                         subject=subj, proposal={}, now=f"t{i}")
        assert reviews.withdraw_review_item(rdir, rid, reason="r", now=f"t{i}") is True
        assert not (rdir / "pending" / f"{rid}.json").exists()  # removed from pending
    audit = list((rdir / "audit_log").glob(f"{rid}-withdrawn-*.json"))
    assert len(audit) == 2  # both withdrawals recorded, neither overwritten
    # A withdraw is a no-op once nothing is pending (terminal human decisions are untouched).
    assert reviews.withdraw_review_item(rdir, rid) is False


def _file(rdir, rtype="promote_candidate_node", subj=None):
    return reviews.create_review_item(rdir, review_type=rtype,
                                      subject=subj or {"node_id": "cpt_x"}, proposal={}, now="t")


def test_defer_keeps_item_in_pending_with_deferred_status_and_audits(tmp_path):
    rdir = tmp_path / "reviews"
    rid = _file(rdir)
    assert reviews.defer_review_item(rdir, rid, note="later") is True
    page = rdir / "pending" / f"{rid}.json"
    assert page.exists()  # stays in pending/, no deferred/ dir
    data = json.loads(page.read_text())
    assert data["status"] == "deferred" and data["decided_by"] == "human"
    assert data["decision_note"] == "later"
    assert len(list((rdir / "audit_log").glob(f"{rid}-deferred-*.json"))) == 1


def test_defer_is_idempotent_no_duplicate_audit(tmp_path):
    rdir = tmp_path / "reviews"
    rid = _file(rdir)
    assert reviews.defer_review_item(rdir, rid) is True
    assert reviews.defer_review_item(rdir, rid) is False  # already deferred
    assert len(list((rdir / "audit_log").glob(f"{rid}-deferred-*.json"))) == 1


def test_deferred_item_can_still_be_resolved(tmp_path):
    rdir = tmp_path / "reviews"
    rid = _file(rdir)
    reviews.defer_review_item(rdir, rid)
    assert reviews.resolve_review_item(rdir, rid, decision="approved", now="t2") is True
    assert (rdir / "approved" / f"{rid}.json").exists()
    assert not (rdir / "pending" / f"{rid}.json").exists()


def test_defer_refuses_already_resolved_item(tmp_path):
    rdir = tmp_path / "reviews"
    rid = _file(rdir)
    reviews.resolve_review_item(rdir, rid, decision="approved", now="t2")
    assert reviews.defer_review_item(rdir, rid) is False  # terminal record untouched
    assert json.loads((rdir / "approved" / f"{rid}.json").read_text())["status"] == "approved"


def test_defer_no_op_when_not_pending(tmp_path):
    rdir = tmp_path / "reviews"
    assert reviews.defer_review_item(rdir, "rev_missing") is False
