"""ADR-0072 B2a — the readable Apply preview and result (pure rendering of per-review outcomes).

The executors' reporting contract is tested per executor (tests/test_apply_outcomes_*.py); here the
grouping, wording, escaping, name collisions, reporting gaps and the publication-pending result.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import apply_readable, review_html  # noqa: E402
from app.workers import apply_outcomes as ao  # noqa: E402


def _o(rid, t, kind, reason=None, **details):
    return ao.outcome(rid, t, kind, reason=reason, **details)


def _tag(i, name):
    return ao.named(i, name)


MIXED = [
    _o("rev_m", "merge_items", ao.APPLIED, survivor=_tag("itm_a", "Data Mesh"),
       absorbed=_tag("itm_b", "Data-Mesh"), subject=_tag("itm_a", "Data Mesh")),
    _o("rev_t", "change_item_type", ao.APPLIED, subject=_tag("itm_c", "Kafka"),
       before={"item_type": "ai_topic_area"}, after={"item_type": "tool_platform"}),
    _o("rev_e", "promote_candidate_node", ao.ALREADY_IN_EFFECT, subject=_tag("itm_d", "Old")),
    _o("rev_s", "promote_candidate_node", ao.SKIPPED, reason="name_taken",
       subject=_tag("itm_e", "Second"),
       problem={"conflict_title": "Renamed Tag", "conflict_node_id": "itm_f"}),
    _o("rev_i", "split_item", ao.INCOMPLETE, reason="identity_operation_in_flight",
       subject=_tag("itm_g", "Primary"), spinoff=_tag("itm_h", "Spin"), documents=2),
]
HANDLED = [{"review_id": r["review_id"], "type": r["type"]} for r in MIXED] + [
    {"review_id": "rev_gap", "type": "hide_claim"}]


def _page(outcomes=MIXED, handled=HANDLED, preview=True, advisories=None):
    return apply_readable.render(apply_readable.model(handled, outcomes), advisories=advisories,
                                 preview=preview)


def test_mixed_outcomes_land_in_distinct_labelled_groups():
    html = _page()
    changes, rest = html.split("Attention needed")
    assert "Merge “Data-Mesh” into “Data Mesh”" in changes
    assert "Change type of “Kafka”: <code>ai_topic_area</code> → <code>tool_platform</code>" in changes
    assert "1 decided review already in effect" in changes
    assert "<strong>Blocked</strong> (nothing changed)" in rest and "“Renamed Tag” already exists" in rest
    assert "name_taken" not in rest.split("<details>")[0]            # the code only in the details
    assert "<strong>Incomplete</strong> (some changes were made" in rest
    assert "<strong>Outcome not reported</strong> (the result is uncertain)" in rest
    assert "rev_gap" in rest and "Hide claims" in rest


def test_a_duplicate_report_is_a_gap_not_a_guess():
    twice = [*MIXED, _o("rev_m", "merge_items", ao.APPLIED, subject=_tag("itm_a", "Data Mesh"))]
    groups = apply_readable.model(HANDLED, twice)
    assert [g["review_id"] for g in groups["not_reported"]] == ["rev_m", "rev_gap"]
    assert groups["not_reported"][0]["gap"] == ao.REPORTED_TWICE
    assert len(groups["not_reported"][0]["reports"]) == 2           # every report kept
    assert "rev_m" not in {r["review_id"] for r in groups["changes"]}


def test_names_need_escaping():
    evil = [_o("rev_x", "hide_content", ao.APPLIED, subject=_tag("src_1", "<script>x</script>"))]
    html = _page(evil, [{"review_id": "rev_x", "type": "hide_content"}])
    assert "<script>" not in html and "&lt;script&gt;x&lt;/script&gt;" in html


def test_identical_names_of_different_ids_show_their_ids_across_groups():
    a = _o("rev_1", "change_item_type", ao.APPLIED, subject=_tag("itm_1", "Data Mesh"),
           before={"item_type": "x"}, after={"item_type": "y"})
    b = _o("rev_2", "promote_candidate_node", ao.SKIPPED, reason="name_taken",
           subject=_tag("itm_2", "data  MESH"))                       # same after sanitization + case
    same = _o("rev_3", "hide_semantic_page", ao.APPLIED, subject=_tag("itm_1", "Data Mesh"))
    html = _page([a, b, same], [{"review_id": r["review_id"], "type": r["type"]} for r in (a, b, same)])
    assert "<code>itm_1</code>" in html and "<code>itm_2</code>" in html
    solo = _page([a, same], [{"review_id": r["review_id"], "type": r["type"]} for r in (a, same)])
    assert "<code>itm_1</code>" not in solo                          # one id twice: no collision


def test_both_ends_of_a_merge_count_for_collisions():
    m = _o("rev_m", "merge_items", ao.APPLIED, survivor=_tag("itm_a", "Mesh"),
           absorbed=_tag("itm_b", "mesh"))
    html = _page([m], [{"review_id": "rev_m", "type": "merge_items"}])
    assert "<code>itm_a</code>" in html and "<code>itm_b</code>" in html


def test_unreadable_and_deleted_names_come_from_the_outcome_details():
    """The renderer never reads the vault: a deleted page's name is what the executor captured, and an
    unreadable one is its id."""
    gone = _o("rev_d", "merge_items", ao.APPLIED, survivor=_tag("itm_a", "Kept"),
              absorbed=_tag("itm_b", "Absorbed, now a tombstone"))
    unknown = _o("rev_u", "hide_claim", ao.APPLIED, subject=ao.named("clm_123", None))
    html = _page([gone, unknown], [{"review_id": "rev_d", "type": "merge_items"},
                                   {"review_id": "rev_u", "type": "hide_claim"}])
    assert "“Absorbed, now a tombstone”" in html and "Hide claim “clm_123”" in html


def test_a_renamed_promotion_names_both_titles():
    r = _o("rev_p", "promote_candidate_node", ao.APPLIED, subject=_tag("itm_p", "Old Name"),
           before={"title": "Old Name"}, after={"title": "New Name", "item_type": "domain"})
    assert "Promote “Old Name” as “New Name”" in _page([r], [{"review_id": "rev_p",
                                                               "type": "promote_candidate_node"}])


def test_advisories_are_their_own_group_never_a_refusal():
    adv = [{"type": "split_item", "kind": "similar_tags", "name": "Data Mesh", "node_id": "itm_s",
            "review_id": "rev_s", "matches": [{"node_id": "itm_a", "title": "Data-Mesh",
                                               "matched": "Data-Mesh", "status": "active"}]}]
    html = _page([], [], advisories=adv)
    assert "<h2>Advisories</h2>" in html and "Information only" in html
    assert "Blocked" not in html and "Attention needed" not in html


def test_not_appliable_reviews_are_blocked_with_their_reason():
    groups = apply_readable.model([], [], [{"review_id": "rev_n", "type": "archive_claim",
                                            "reason": "no_executor_in_phase_6"}])
    assert [r["review_id"] for r in groups["blocked"]] == ["rev_n"]


def test_the_result_page_keeps_recorded_changes_apart_from_unfinished_publication():
    result = {"status": "saved_publication_pending", "applied": True, "validators_ok": True,
              "failed_validators": [], "warnings": [], "summary": {},
              "publication": {"status": "saved_publication_pending", "pending": {}},
              "handled": [{"review_id": "rev_t", "type": "change_item_type"}],
              "outcomes": [MIXED[1]], "advisories": []}
    html = review_html.render_apply_result(result)
    assert "The changes below were recorded; publishing them is unfinished" in html
    assert "Retry publishing" in html
    assert "<h2>Changes made</h2>" in html and "Change type of “Kafka”" in html
    clean = review_html.render_apply_result({**result, "status": "applied",
                                             "publication": {"status": "published"}})
    assert "publishing them is unfinished" not in clean and "Change type of “Kafka”" in clean


def test_the_preview_folds_the_technical_diff_and_shows_validators():
    dry = {"status": "ok", "diff": {"graph": {}, "wiki": [], "reviews": [], "manifests": []},
           "items": [], "not_appliable": [], "validators": {"passed": True, "failures": []},
           "warnings": [], "handled": HANDLED[:1], "outcomes": MIXED[:1], "preview_token": "tok"}
    html = review_html.render_apply_dry_run({}, dry)
    assert html.index("Merge “Data-Mesh” into “Data Mesh”") < html.index("<details><summary>Technical")
    assert "Validators: <strong>passed</strong>" in html and "Apply now" in html
    failed = review_html.render_apply_dry_run({}, {**dry, "status": "failed", "reason": "executor_error",
                                                   "preview_token": None})
    assert "Validators: <strong>not run</strong>" in failed and "Apply now" not in failed
    assert "Changes this Apply would make" not in failed          # no sentences from a failed run


# --- B2a review round 1 -------------------------------------------------------------------------


def test_every_change_sentence_links_its_review():
    html = _page()
    changes = html.split("Attention needed")[0]
    assert "<a href='/ui/reviews/rev_m'>review</a>" in changes
    assert "<a href='/ui/reviews/rev_t'>review</a>" in changes


def test_recovery_wording_depends_on_the_reason():
    """Apply resumes authority work; Retry publishing repairs projections; nothing else promises a
    recovery."""
    cases = [
        (_o("r1", "hide_content", ao.INCOMPLETE, reason="source_page_refresh_failed",
            subject=_tag("src_1", "Doc")), "Retry publishing repairs the page"),
        (_o("r2", "merge_items", ao.INCOMPLETE, reason="page_owner_unknown", stage="started",
            survivor=_tag("itm_a", "A"), absorbed=_tag("itm_b", "B")), "Apply again: it resumes"),
        (_o("r3", "split_item", ao.INCOMPLETE, reason="partial_split_state",
            subject=_tag("itm_c", "C")), "be resumed automatically"),
        (_o("r4", "deprecate_wiki_page", ao.INCOMPLETE, reason=ao.HIDDEN_TAKES_PRECEDENCE,
            subject=_tag("clm_1", "Claim")), "unhide it first"),
        (_o("r5", "mark_semantic_duplicate", ao.INCOMPLETE, reason="weird_code",
            subject=_tag("itm_d", "D")), "does not resume on its own"),
    ]
    for rec, words in cases:
        html = _page([rec], [{"review_id": rec["review_id"], "type": rec["type"]}])
        assert words in html, (rec["reason"], html)
        assert "the next Apply resumes" not in html


def test_promotion_after_titles_and_advisories_count_for_collisions():
    renamed = _o("rev_p", "promote_candidate_node", ao.APPLIED, subject=_tag("itm_p", "Old"),
                 before={"title": "Old"}, after={"title": "Data Mesh"})
    other = _o("rev_q", "hide_semantic_page", ao.APPLIED, subject=_tag("itm_q", "data mesh"))
    html = _page([renamed, other], [{"review_id": "rev_p", "type": "promote_candidate_node"},
                                    {"review_id": "rev_q", "type": "hide_semantic_page"}])
    assert "<code>itm_p</code>" in html and "<code>itm_q</code>" in html
    adv = [{"type": "split_item", "name": "Mesh", "node_id": "itm_s", "review_id": "rev_s",
            "matches": [{"node_id": "itm_t", "title": "MESH", "matched": "MESH"}]}]
    html = _page([], [], advisories=adv)
    assert "<code>itm_s</code>" in html and "<code>itm_t</code>" in html


def test_unreadable_details_are_flagged_not_guessed():
    rec = _o("rev_r", "change_item_type", ao.SKIPPED, reason="page_owner_unknown",
             subject=ao.named("itm_r", None), before={"item_type": None},
             after={"item_type": "domain"}, unavailable=["before.item_type"])
    html = _page([rec], [{"review_id": "rev_r", "type": "change_item_type"}])
    assert "some details couldn" in html and "<code>?</code> →" in html
