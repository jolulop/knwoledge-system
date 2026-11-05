"""ADR-0072 decisions 2 + 3: per-review Apply outcomes from the deprecation / visibility / duplicate
executors (`deprecate_wiki_page`, `hide_semantic_page`, `unhide_semantic_page`, `hide_claim`,
`unhide_claim`, `mark_semantic_duplicate`).

The reporting contract: one outcome per handled review, counts consistent with outcomes,
`already_in_effect` only for a verified no-op that wrote nothing (a repair is `applied` +
`normalized`), the VERIFIED end state reported (read back after the write: `hidden_takes_precedence` /
`postcondition_not_met` / `verification_unavailable` when it doesn't hold), reporting reads that never
raise, typed skip reasons mirrored 1:1, display names captured BEFORE mutation, and an unreadable name
falling back to the id. Assertions check the actual page/graph state, not only the record shape.
Execution behaviour is covered by each executor's own tests.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph
from app.workers import apply_outcomes, deprecations, duplicates, items, labels
from app.workers.wiki_render import parse_frontmatter
from tests.test_claim_visibility import CX, CY, _active_claim
from tests.test_deprecations import _graph, _write_item
from tests.test_duplicates import _dup_item, _seed_node

ITEM = "itm_00000000000000aa"      # tests.test_deprecations._write_item's default node (title "Thing")
ITEM_B = "itm_00000000000000bb"
A, B = "itm_aaaaaaaaaaaaaaaa", "itm_bbbbbbbbbbbbbbbb"


# --- helpers ---------------------------------------------------------------


def _approve(tmp_path: Path, rid: str, rtype: str, subject: dict, *, to_status: str | None = None,
             node_type: str | None = None) -> None:
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    item = {"review_id": rid, "type": rtype, "status": "approved", "subject": subject,
            "proposal": {"to_status": to_status} if to_status else {},
            "context": {"node_type": node_type} if node_type else {}}
    (d / f"{rid}.json").write_text(json.dumps(item), encoding="utf-8")


def _approved_ids(tmp_path: Path, rtype: str) -> set[str]:
    out = set()
    for p in (tmp_path / "reviews" / "approved").glob("*.json"):
        item = json.loads(p.read_text(encoding="utf-8"))
        if item.get("type") == rtype:
            out.add(item["review_id"])
    return out


def _by_id(res: dict) -> dict[str, dict]:
    return {o["review_id"]: o for o in res["outcomes"]}


# Postcondition reasons: the legacy counts still count these runs (applied/normalized), the legacy
# `skipped` list never lists them (execution is unchanged; only the report is honest).
_POSTCONDITION = {apply_outcomes.VERIFICATION_UNAVAILABLE, apply_outcomes.POSTCONDITION_NOT_MET,
                  apply_outcomes.HIDDEN_TAKES_PRECEDENCE}


def _check_contract(tmp_path: Path, res: dict, rtype: str) -> None:
    """One outcome per approved review of the type; counts consistent with the outcomes."""
    outs = res["outcomes"]
    ids = [o["review_id"] for o in outs]
    assert len(ids) == len(set(ids)), "a review reported more than once"
    assert set(ids) == _approved_ids(tmp_path, rtype), "an approved review was not reported"
    assert all(o["type"] == rtype for o in outs)
    assert all(o["outcome"] in apply_outcomes.OUTCOMES for o in outs)
    # a repair is `applied` + normalized, never already_in_effect (B2a review round 1)
    assert not any(o["outcome"] == apply_outcomes.ALREADY_IN_EFFECT and o["details"].get("normalized")
                   for o in outs)
    plain = sum(1 for o in outs if o["outcome"] == apply_outcomes.APPLIED
                and not o["details"].get("normalized"))
    repaired = sum(1 for o in outs if o["outcome"] == apply_outcomes.APPLIED
                   and o["details"].get("normalized"))
    unconfirmed = sum(1 for o in outs if o.get("reason") in _POSTCONDITION)
    assert res["applied"] >= plain and res["normalized"] >= repaired
    assert res["applied"] + res["normalized"] == plain + repaired + unconfirmed
    assert sorted((s["review_id"], s["reason"]) for s in res["skipped"]) == sorted(
        (o["review_id"], o["reason"]) for o in outs
        if o["outcome"] == apply_outcomes.SKIPPED and o["reason"] not in _POSTCONDITION)
    for o in outs:
        subj = o["details"]["subject"]
        assert subj["name"], "a subject name must never be blank"


def _fm(page: Path) -> dict:
    return parse_frontmatter(page.read_text(encoding="utf-8"))


def _deprecate(tmp_path, conn):
    return deprecations.apply_approved_deprecations(
        conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki",
        claims_dir=tmp_path / "wiki" / "Claims", markdown_dir=tmp_path / "normalized" / "markdown")


def _item_page(tmp_path, slug="thing") -> Path:
    return tmp_path / "wiki" / "Items" / f"{slug}.md"


def _rename_after(monkeypatch, module, attr: str, page_of) -> None:
    """Wrap a render seam so the page's title changes right after it writes — a name read after the
    mutation would then show the new title."""
    real = getattr(module, attr)

    def wrapped(*args, **kwargs):
        res = real(*args, **kwargs)
        page = page_of(kwargs)
        if page.exists():
            text = page.read_text(encoding="utf-8")
            page.write_text(text.replace('title: "Thing"', 'title: "Renamed"')
                            .replace("title: Thing", "title: Renamed")
                            .replace('title: "Alpha"', 'title: "Renamed"')
                            .replace("title: Alpha", "title: Renamed"), encoding="utf-8")
        return res
    monkeypatch.setattr(module, attr, wrapped)


# --- deprecate_wiki_page ---------------------------------------------------


def test_deprecation_one_outcome_per_review_and_counts(tmp_path):
    conn = _graph(tmp_path)
    _write_item(tmp_path, conn)                                               # -> applied
    _write_item(tmp_path, conn, node_id=ITEM_B, slug="other",
                node_status="deprecated_candidate", review_status="pending")  # -> normalization
    _approve(tmp_path, "rev_1", "deprecate_wiki_page", {"node_id": ITEM, "page": "Items/thing.md"},
             to_status="deprecated_candidate", node_type="item")
    _approve(tmp_path, "rev_2", "deprecate_wiki_page", {"node_id": ITEM_B, "page": "Items/other.md"},
             to_status="deprecated_candidate", node_type="item")
    _approve(tmp_path, "rev_3", "deprecate_wiki_page", {"node_id": "src_x", "page": "Sources/src_x.md"},
             to_status="deprecated_candidate")
    _approve(tmp_path, "rev_4", "deprecate_wiki_page", {"node_id": "itm_gone", "page": "Items/gone.md"},
             to_status="deprecated_candidate", node_type="item")
    _approve(tmp_path, "rev_5", "deprecate_wiki_page",
             {"node_id": ITEM, "page": "Items/../../raw/x.md"}, to_status="deprecated_candidate")
    _approve(tmp_path, "rev_6", "deprecate_wiki_page", {"node_id": ITEM, "page": "Items/thing.md"},
             to_status="active")
    assert _fm(_item_page(tmp_path, "other"))["review_status"] == "pending"
    res = _deprecate(tmp_path, conn)
    _check_contract(tmp_path, res, "deprecate_wiki_page")
    out = _by_id(res)
    assert out["rev_1"]["outcome"] == "applied"
    assert out["rev_1"]["details"]["subject"] == {"id": ITEM, "name": "Thing"}
    assert out["rev_1"]["details"]["before"] == {"status": "active"}
    assert out["rev_1"]["details"]["after"] == {"status": "deprecated_candidate"}
    # the review_status normalization WROTE the page: applied + normalized, and it really changed
    assert out["rev_2"]["outcome"] == "applied" and out["rev_2"]["details"]["normalized"] is True
    assert out["rev_2"]["details"]["after"] == {"status": "deprecated_candidate"}
    assert _fm(_item_page(tmp_path, "other"))["review_status"] == "approved"
    assert "Items/other.md" in res["changed_pages"]
    assert (out["rev_3"]["outcome"], out["rev_3"]["reason"]) == ("skipped", "out_of_scope")
    assert (out["rev_4"]["outcome"], out["rev_4"]["reason"]) == ("skipped", "node_missing")
    assert out["rev_4"]["details"]["subject"] == {"id": "itm_gone", "name": "itm_gone"}
    # an unsafe path is never read for a name: the id stands in
    assert (out["rev_5"]["outcome"], out["rev_5"]["reason"]) == ("skipped", "invalid_page_path")
    assert out["rev_5"]["details"]["subject"] == {"id": ITEM, "name": ITEM}
    assert (out["rev_6"]["outcome"], out["rev_6"]["reason"]) == ("skipped", "unexpected_to_status")
    assert res["applied"] == 1 and res["normalized"] == 1 and len(res["skipped"]) == 4


def test_deprecation_rerun_reports_already_in_effect_with_postcondition(tmp_path):
    conn = _graph(tmp_path)
    _write_item(tmp_path, conn)
    _approve(tmp_path, "rev_1", "deprecate_wiki_page", {"node_id": ITEM, "page": "Items/thing.md"},
             to_status="deprecated_candidate", node_type="item")
    assert _by_id(_deprecate(tmp_path, conn))["rev_1"]["outcome"] == "applied"
    before_bytes = _item_page(tmp_path).read_bytes()
    res = _deprecate(tmp_path, conn)
    _check_contract(tmp_path, res, "deprecate_wiki_page")
    o = _by_id(res)["rev_1"]
    assert o["outcome"] == "already_in_effect" and not o["details"].get("normalized")
    assert res["applied"] == res["normalized"] == 0 and res["changed_pages"] == []
    assert _item_page(tmp_path).read_bytes() == before_bytes     # verified no-op: nothing written
    # the postcondition the outcome claims really holds
    fm = parse_frontmatter(_item_page(tmp_path).read_text(encoding="utf-8"))
    assert fm["status"] == "deprecated_candidate" and fm["review_status"] == "approved"
    assert graph.get_node(conn, ITEM)["status"] == "deprecated_candidate"
    # ...and a drifted graph mirror is NOT reported already_in_effect: the mirror repair (page
    # unchanged, graph row rewritten) is applied, and the graph really changed
    graph.upsert_node(conn, node_id=ITEM, node_type="item", slug="thing", status="active")
    res = _deprecate(tmp_path, conn)
    _check_contract(tmp_path, res, "deprecate_wiki_page")
    o = _by_id(res)["rev_1"]
    assert o["outcome"] == "applied" and o["details"]["after"] == {"status": "deprecated_candidate"}
    assert graph.get_node(conn, ITEM)["status"] == "deprecated_candidate"
    assert res["changed_pages"] == []                             # only the graph row was written


def test_deprecation_failed_read_back_is_unverified_and_never_raises(tmp_path, monkeypatch):
    conn = _graph(tmp_path)
    _write_item(tmp_path, conn)
    _approve(tmp_path, "rev_1", "deprecate_wiki_page", {"node_id": ITEM, "page": "Items/thing.md"},
             to_status="deprecated_candidate", node_type="item")

    def boom(*_a, **_k):
        raise OSError("unreadable")
    monkeypatch.setattr(deprecations, "_page_frontmatter", boom)  # the post-write read-back
    res = _deprecate(tmp_path, conn)                              # no exception
    _check_contract(tmp_path, res, "deprecate_wiki_page")
    o = _by_id(res)["rev_1"]
    # the executor's result stands (it wrote) but the report can't confirm it
    assert (o["outcome"], o["reason"]) == ("incomplete", "verification_unavailable")
    assert "after" not in o["details"]
    assert res["applied"] == 1 and res["changed_pages"] == ["Items/thing.md"]
    assert _fm(_item_page(tmp_path))["status"] == "deprecated_candidate"
    # a re-run is a verified no-op from the executor's own pre-state read — the failed read-back
    # never justified already_in_effect on the run above
    monkeypatch.undo()
    assert _by_id(_deprecate(tmp_path, conn))["rev_1"]["outcome"] == "already_in_effect"


def test_deprecation_failed_name_read_keeps_the_executor_result(tmp_path, monkeypatch):
    conn = _graph(tmp_path)
    _write_item(tmp_path, conn)
    _approve(tmp_path, "rev_1", "deprecate_wiki_page", {"node_id": ITEM, "page": "Items/thing.md"},
             to_status="deprecated_candidate", node_type="item")
    _approve(tmp_path, "rev_2", "deprecate_wiki_page", {"node_id": ITEM, "page": "Items/thing.md"},
             to_status="deprecated_candidate", node_type="claim")   # page_node_mismatch refusal

    def boom(*_a, **_k):
        raise RuntimeError("label read failed")
    monkeypatch.setattr(labels, "_page_label", boom)              # every display-name read
    res = _deprecate(tmp_path, conn)
    _check_contract(tmp_path, res, "deprecate_wiki_page")
    out = _by_id(res)
    assert out["rev_1"]["outcome"] == "applied"
    assert out["rev_1"]["details"]["subject"] == {"id": ITEM, "name": ITEM}
    assert (out["rev_2"]["outcome"], out["rev_2"]["reason"]) == ("skipped", "page_node_mismatch")
    assert graph.get_node(conn, ITEM)["status"] == "deprecated_candidate"


def test_deprecation_read_back_mismatch_is_postcondition_not_met(tmp_path, monkeypatch):
    conn = _graph(tmp_path)
    _write_item(tmp_path, conn)
    _approve(tmp_path, "rev_1", "deprecate_wiki_page", {"node_id": ITEM, "page": "Items/thing.md"},
             to_status="deprecated_candidate", node_type="item")
    real = items.recompose_semantic_node_page

    def mirror_lost(*args, **kwargs):          # the page is written, then the graph row reverts
        res = real(*args, **kwargs)
        graph.upsert_node(conn, node_id=ITEM, node_type="item", slug="thing", status="active")
        return res
    monkeypatch.setattr(items, "recompose_semantic_node_page", mirror_lost)
    res = _deprecate(tmp_path, conn)
    _check_contract(tmp_path, res, "deprecate_wiki_page")
    o = _by_id(res)["rev_1"]
    assert (o["outcome"], o["reason"]) == ("incomplete", "postcondition_not_met")
    # `after` is the read-back, not the request
    assert o["details"]["after"] == {"status": "deprecated_candidate", "graph_status": "active"}
    assert _fm(_item_page(tmp_path))["status"] == "deprecated_candidate"
    assert graph.get_node(conn, ITEM)["status"] == "active"
    assert res["applied"] == 1                 # legacy count unchanged


def test_deprecating_a_hidden_claim_reports_hidden_takes_precedence(tmp_path):
    conn = _graph(tmp_path)
    _active_claim(tmp_path, conn, CX, text="The sky is blue today.")
    _claim_review(tmp_path, "rev_h", "hide_claim", CX, "hidden")
    assert _by_id(_claims(tmp_path, conn, hide=True))["rev_h"]["outcome"] == "applied"
    (tmp_path / "reviews" / "approved" / "rev_h.json").unlink()
    page = tmp_path / "wiki" / "Claims" / f"{CX}.md"
    assert _fm(page)["status"] == "hidden"
    _approve(tmp_path, "rev_d", "deprecate_wiki_page", {"node_id": CX, "page": f"Claims/{CX}.md"},
             to_status="deprecated_candidate", node_type="claim")
    for _ in range(2):                         # re-applied every run (execution unchanged)
        res = _deprecate(tmp_path, conn)
        _check_contract(tmp_path, res, "deprecate_wiki_page")
        o = _by_id(res)["rev_d"]
        # recompose_claim rewrote the page (this run changed state) but hidden wins
        assert (o["outcome"], o["reason"]) == ("incomplete", "hidden_takes_precedence")
        assert o["details"]["before"] == {"status": "hidden"}
        assert o["details"]["after"] == {"status": "hidden"}
        assert res["applied"] == 1 and res["changed_pages"] == [f"Claims/{CX}.md"]
        assert _fm(page)["status"] == "hidden"
        assert graph.get_node(conn, CX)["status"] == "hidden"


def test_deprecation_name_captured_before_mutation(tmp_path, monkeypatch):
    conn = _graph(tmp_path)
    _write_item(tmp_path, conn)
    _approve(tmp_path, "rev_1", "deprecate_wiki_page", {"node_id": ITEM, "page": "Items/thing.md"},
             to_status="deprecated_candidate", node_type="item")
    _rename_after(monkeypatch, items, "recompose_semantic_node_page",
                  lambda kw: kw["wiki_dir"] / "Items" / "thing.md")
    res = _deprecate(tmp_path, conn)
    assert parse_frontmatter(_item_page(tmp_path).read_text(encoding="utf-8"))["title"] == "Renamed"
    assert _by_id(res)["rev_1"]["details"]["subject"] == {"id": ITEM, "name": "Thing"}


def test_deprecation_unreadable_page_names_by_id_and_skips_typed(tmp_path):
    conn = _graph(tmp_path)
    _write_item(tmp_path, conn)
    _item_page(tmp_path).unlink()                 # graph node exists, page gone -> page_missing
    _approve(tmp_path, "rev_1", "deprecate_wiki_page", {"node_id": ITEM, "page": "Items/thing.md"},
             to_status="deprecated_candidate", node_type="item")
    res = _deprecate(tmp_path, conn)
    _check_contract(tmp_path, res, "deprecate_wiki_page")
    o = _by_id(res)["rev_1"]
    assert (o["outcome"], o["reason"]) == ("skipped", "page_missing")
    assert o["details"]["subject"] == {"id": ITEM, "name": ITEM}


# --- hide_semantic_page / unhide_semantic_page ------------------------------


def _hide(tmp_path, conn):
    return deprecations.apply_hidden_semantic_pages(conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")


def _unhide(tmp_path, conn):
    return deprecations.apply_unhidden_semantic_pages(conn, tmp_path / "reviews",
                                                      wiki_dir=tmp_path / "wiki")


def test_semantic_hide_outcomes_rerun_and_failed_postcondition(tmp_path):
    conn = _graph(tmp_path)
    _write_item(tmp_path, conn)
    _approve(tmp_path, "rev_h", "hide_semantic_page", {"node_id": ITEM, "page": "Items/thing.md"},
             to_status="hidden", node_type="item")
    _approve(tmp_path, "rev_x", "hide_semantic_page", {"node_id": ITEM, "page": "Claims/thing.md"},
             to_status="hidden", node_type="item")
    res = _hide(tmp_path, conn)
    _check_contract(tmp_path, res, "hide_semantic_page")
    out = _by_id(res)
    assert out["rev_h"]["outcome"] == "applied"
    assert out["rev_h"]["details"]["subject"] == {"id": ITEM, "name": "Thing"}
    assert out["rev_h"]["details"]["before"] == {"status": "active"}
    assert out["rev_h"]["details"]["after"] == {"status": "hidden"}
    assert (out["rev_x"]["outcome"], out["rev_x"]["reason"]) == ("skipped", "out_of_scope")

    # re-run: the verified fully-effected review reports already_in_effect (uncounted)
    res = _hide(tmp_path, conn)
    _check_contract(tmp_path, res, "hide_semantic_page")
    assert _by_id(res)["rev_h"]["outcome"] == "already_in_effect"
    assert graph.get_node(conn, ITEM)["status"] == "hidden"

    # postcondition broken (graph drifted to deprecated, page still hidden): never already_in_effect
    graph.upsert_node(conn, node_id=ITEM, node_type="item", slug="thing", status="deprecated_candidate")
    res = _hide(tmp_path, conn)
    _check_contract(tmp_path, res, "hide_semantic_page")
    o = _by_id(res)["rev_h"]
    assert (o["outcome"], o["reason"]) == ("skipped", "node_not_active")
    assert o["details"]["before"] == {"status": "deprecated_candidate"}


def test_semantic_hide_normalization_is_applied_normalized(tmp_path):
    conn = _graph(tmp_path)
    _write_item(tmp_path, conn, node_status="hidden", review_status="pending")
    _approve(tmp_path, "rev_h", "hide_semantic_page", {"node_id": ITEM, "page": "Items/thing.md"},
             to_status="hidden", node_type="item")
    assert _fm(_item_page(tmp_path))["review_status"] == "pending"
    res = _hide(tmp_path, conn)
    _check_contract(tmp_path, res, "hide_semantic_page")
    o = _by_id(res)["rev_h"]
    assert o["outcome"] == "applied" and o["details"]["normalized"] is True
    assert o["details"]["after"] == {"status": "hidden"}
    assert res["normalized"] == 1 and res["changed_pages"] == ["Items/thing.md"]
    fm = _fm(_item_page(tmp_path))
    assert fm["status"] == "hidden" and fm["review_status"] == "approved"   # the page really changed


def test_semantic_unhide_normalization_is_applied_normalized(tmp_path):
    conn = _graph(tmp_path)
    _write_item(tmp_path, conn, node_status="active", review_status="approved")  # review_status lags
    _approve(tmp_path, "rev_u", "unhide_semantic_page", {"node_id": ITEM, "page": "Items/thing.md"},
             to_status="active", node_type="item")
    res = _unhide(tmp_path, conn)
    _check_contract(tmp_path, res, "unhide_semantic_page")
    o = _by_id(res)["rev_u"]
    assert o["outcome"] == "applied" and o["details"]["normalized"] is True
    assert _fm(_item_page(tmp_path))["review_status"] == "none"


def test_semantic_hide_read_back_unavailable_never_raises(tmp_path, monkeypatch):
    conn = _graph(tmp_path)
    _write_item(tmp_path, conn)
    _approve(tmp_path, "rev_h", "hide_semantic_page", {"node_id": ITEM, "page": "Items/thing.md"},
             to_status="hidden", node_type="item")

    def boom(*_a, **_k):
        raise OSError("unreadable")
    monkeypatch.setattr(deprecations, "_page_frontmatter", boom)
    res = _hide(tmp_path, conn)
    _check_contract(tmp_path, res, "hide_semantic_page")
    o = _by_id(res)["rev_h"]
    assert (o["outcome"], o["reason"]) == ("incomplete", "verification_unavailable")
    assert res["applied"] == 1 and graph.get_node(conn, ITEM)["status"] == "hidden"


def test_semantic_unhide_outcomes(tmp_path):
    conn = _graph(tmp_path)
    _write_item(tmp_path, conn, node_status="hidden", review_status="approved")
    _write_item(tmp_path, conn, node_id=ITEM_B, slug="other",
                node_status="deprecated_candidate")                          # not hidden, not active
    _write_item(tmp_path, conn, node_id="itm_00000000000000cc", slug="third")  # already active + none
    _approve(tmp_path, "rev_u", "unhide_semantic_page", {"node_id": ITEM, "page": "Items/thing.md"},
             to_status="active", node_type="item")
    _approve(tmp_path, "rev_v", "unhide_semantic_page", {"node_id": ITEM_B, "page": "Items/other.md"},
             to_status="active", node_type="item")
    _approve(tmp_path, "rev_w", "unhide_semantic_page",
             {"node_id": "itm_00000000000000cc", "page": "Items/third.md"},
             to_status="active", node_type="item")
    res = _unhide(tmp_path, conn)
    _check_contract(tmp_path, res, "unhide_semantic_page")
    out = _by_id(res)
    assert out["rev_u"]["outcome"] == "applied"
    assert out["rev_u"]["details"]["before"] == {"status": "hidden"}
    assert out["rev_u"]["details"]["after"] == {"status": "active"}
    assert (out["rev_v"]["outcome"], out["rev_v"]["reason"]) == ("skipped", "node_not_hidden")
    assert out["rev_v"]["details"]["before"] == {"status": "deprecated_candidate"}
    # the target state (page active + review_status none + graph active) verified -> already_in_effect
    assert out["rev_w"]["outcome"] == "already_in_effect"
    res = _unhide(tmp_path, conn)
    assert _by_id(res)["rev_u"]["outcome"] == "already_in_effect"


def test_semantic_hide_name_captured_before_mutation(tmp_path, monkeypatch):
    conn = _graph(tmp_path)
    _write_item(tmp_path, conn)
    _approve(tmp_path, "rev_h", "hide_semantic_page", {"node_id": ITEM, "page": "Items/thing.md"},
             to_status="hidden", node_type="item")
    _rename_after(monkeypatch, items, "recompose_semantic_node_page",
                  lambda kw: kw["wiki_dir"] / "Items" / "thing.md")
    res = _hide(tmp_path, conn)
    assert parse_frontmatter(_item_page(tmp_path).read_text(encoding="utf-8"))["title"] == "Renamed"
    assert _by_id(res)["rev_h"]["details"]["subject"]["name"] == "Thing"


# --- hide_claim / unhide_claim ---------------------------------------------


def _claims(tmp_path, conn, hide: bool):
    fn = deprecations.apply_hidden_claims if hide else deprecations.apply_unhidden_claims
    return fn(conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki",
              markdown_dir=tmp_path / "normalized" / "markdown")


def _claim_review(tmp_path, rid, rtype, cid, to_status):
    _approve(tmp_path, rid, rtype, {"node_id": cid, "page": f"Claims/{cid}.md"},
             to_status=to_status, node_type="claim")


def test_claim_hide_unhide_outcomes(tmp_path):
    conn = _graph(tmp_path)
    _active_claim(tmp_path, conn, CX, text="The sky is blue today.")
    _claim_review(tmp_path, "rev_c", "hide_claim", CX, "hidden")
    _claim_review(tmp_path, "rev_m", "hide_claim", "clm_missing", "hidden")
    res = _claims(tmp_path, conn, hide=True)
    conn.commit()
    _check_contract(tmp_path, res, "hide_claim")
    out = _by_id(res)
    assert out["rev_c"]["outcome"] == "applied"
    name = out["rev_c"]["details"]["subject"]
    assert name["id"] == CX and name["name"] != CX            # the claim's own wording names it
    assert out["rev_c"]["details"]["before"] == {"status": "active"}
    assert out["rev_c"]["details"]["after"] == {"status": "hidden"}
    assert (out["rev_m"]["outcome"], out["rev_m"]["reason"]) == ("skipped", "node_missing")
    assert out["rev_m"]["details"]["subject"] == {"id": "clm_missing", "name": "clm_missing"}

    # re-run: verified (page AND graph hidden, review_status approved) -> already_in_effect
    res = _claims(tmp_path, conn, hide=True)
    _check_contract(tmp_path, res, "hide_claim")
    assert _by_id(res)["rev_c"]["outcome"] == "already_in_effect"

    # unhide: re-derived status reported as `after`
    (tmp_path / "reviews" / "approved" / "rev_c.json").unlink()
    (tmp_path / "reviews" / "approved" / "rev_m.json").unlink()
    _claim_review(tmp_path, "rev_u", "unhide_claim", CX, "active")
    res = _claims(tmp_path, conn, hide=False)
    _check_contract(tmp_path, res, "unhide_claim")
    o = _by_id(res)["rev_u"]
    assert o["outcome"] == "applied" and o["details"]["after"] == {"status": "active"}
    res = _claims(tmp_path, conn, hide=False)
    assert _by_id(res)["rev_u"]["outcome"] == "already_in_effect"


def test_claim_hide_review_status_normalization_is_applied_normalized(tmp_path):
    conn = _graph(tmp_path)
    _active_claim(tmp_path, conn, CX, text="The sky is blue today.")
    _claim_review(tmp_path, "rev_c", "hide_claim", CX, "hidden")
    _claims(tmp_path, conn, hide=True)
    page = tmp_path / "wiki" / "Claims" / f"{CX}.md"
    page.write_text(page.read_text(encoding="utf-8").replace("review_status: approved",
                                                             "review_status: pending"),
                    encoding="utf-8")
    assert _fm(page)["review_status"] == "pending"
    res = _claims(tmp_path, conn, hide=True)
    _check_contract(tmp_path, res, "hide_claim")
    o = _by_id(res)["rev_c"]
    assert o["outcome"] == "applied" and o["details"]["normalized"] is True
    assert o["details"]["after"] == {"status": "hidden"}
    assert res["normalized"] == 1
    fm = _fm(page)
    assert fm["status"] == "hidden" and fm["review_status"] == "approved"


def test_claim_partial_state_is_skipped_not_already_in_effect(tmp_path):
    conn = _graph(tmp_path)
    _active_claim(tmp_path, conn, CX, text="The sky is blue today.")
    _claim_review(tmp_path, "rev_c", "hide_claim", CX, "hidden")
    _claims(tmp_path, conn, hide=True)
    # the graph mirror drifts back to active while the page stays hidden: postcondition broken
    graph.upsert_node(conn, node_id=CX, node_type="claim", slug=CX, status="active")
    res = _claims(tmp_path, conn, hide=True)
    _check_contract(tmp_path, res, "hide_claim")
    o = _by_id(res)["rev_c"]
    assert (o["outcome"], o["reason"]) == ("skipped", "partial_hide_state")
    assert o["details"]["before"] == {"status": "active", "page_status": "hidden"}


def test_claim_hide_non_active_is_skipped_claim_not_active(tmp_path):
    conn = _graph(tmp_path)
    _active_claim(tmp_path, conn, CY, text="Water is wet.")
    claims_page = tmp_path / "wiki" / "Claims" / f"{CY}.md"
    claims_page.write_text(claims_page.read_text().replace("status: active", "status: deprecated_candidate"))
    graph.upsert_node(conn, node_id=CY, node_type="claim", slug=CY, status="deprecated_candidate")
    _claim_review(tmp_path, "rev_c", "hide_claim", CY, "hidden")
    res = _claims(tmp_path, conn, hide=True)
    _check_contract(tmp_path, res, "hide_claim")
    o = _by_id(res)["rev_c"]
    assert (o["outcome"], o["reason"]) == ("skipped", "claim_not_active")


# --- mark_semantic_duplicate -----------------------------------------------


def _dups(tmp_path, conn):
    return duplicates.apply_marked_duplicates(conn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")


def _write_dup(tmp_path, item):
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{item['review_id']}.json").write_text(json.dumps(item), encoding="utf-8")


def test_duplicate_outcomes_skips_and_names(tmp_path):
    conn = _graph(tmp_path)
    _seed_node(tmp_path, conn, node_id=A, slug="alpha", title="Alpha")
    _seed_node(tmp_path, conn, node_id=B, slug="bravo", title="Bravo")
    graph.upsert_node(conn, node_id="src_aaaaaaaaaaaaaaaa", node_type="source",
                      slug="src_aaaaaaaaaaaaaaaa", status="active")
    _write_dup(tmp_path, _dup_item(A, B, rid="rev_ok"))
    _write_dup(tmp_path, _dup_item(A, "itm_cccccccccccccccc", rid="rev_missing"))
    _write_dup(tmp_path, _dup_item(A, "src_aaaaaaaaaaaaaaaa", rid="rev_type"))
    _write_dup(tmp_path, _dup_item(A, "../evil", rid="rev_unsafe"))
    _write_dup(tmp_path, {"review_id": "rev_bad", "type": "mark_semantic_duplicate",
                          "status": "approved", "subject": {"node_ids": [A]}})
    res = _dups(tmp_path, conn)
    _check_contract(tmp_path, res, "mark_semantic_duplicate")
    out = _by_id(res)
    assert out["rev_ok"]["outcome"] == "applied"
    assert out["rev_ok"]["details"]["subject"] == {"id": A, "name": "Alpha"}
    assert out["rev_ok"]["details"]["duplicate_of"] == {"id": B, "name": "Bravo"}
    o = out["rev_missing"]
    assert (o["outcome"], o["reason"]) == ("skipped", "node_missing")
    assert o["details"]["subject"]["name"] == "Alpha"
    assert o["details"]["duplicate_of"] == {"id": "itm_cccccccccccccccc", "name": "itm_cccccccccccccccc"}
    assert (out["rev_type"]["outcome"], out["rev_type"]["reason"]) == ("skipped", "type_mismatch")
    o = out["rev_unsafe"]
    assert (o["outcome"], o["reason"]) == ("skipped", "invalid_node_id")
    # an unsafe id is never resolved: it stands in as its own name
    assert {"id": "../evil", "name": "../evil"} in (o["details"]["subject"], o["details"]["duplicate_of"])
    o = out["rev_bad"]
    assert (o["outcome"], o["reason"]) == ("skipped", "malformed_subject")
    assert o["details"]["subject"] == {"id": A, "name": A}


def test_duplicate_rerun_and_projection_repair(tmp_path):
    conn = _graph(tmp_path)
    pa = _seed_node(tmp_path, conn, node_id=A, slug="alpha", title="Alpha")
    pb = _seed_node(tmp_path, conn, node_id=B, slug="bravo", title="Bravo")
    _write_dup(tmp_path, _dup_item(A, B, rid="rev_ok"))
    assert _by_id(_dups(tmp_path, conn))["rev_ok"]["outcome"] == "applied"

    # re-run: edge active AND both pages project (verified), nothing written -> already_in_effect
    bytes_a, bytes_b = pa.read_bytes(), pb.read_bytes()
    res = _dups(tmp_path, conn)
    _check_contract(tmp_path, res, "mark_semantic_duplicate")
    o = _by_id(res)["rev_ok"]
    assert o["outcome"] == "already_in_effect" and not o["details"].get("normalized")
    assert res["normalized"] == 0 and res["changed_pages"] == []
    assert (pa.read_bytes(), pb.read_bytes()) == (bytes_a, bytes_b)

    # a stale projection (edge still active) is repaired: the page is rewritten -> applied +
    # normalized (counted normalized), and the page really projects the partner again
    stale = pa.read_text(encoding="utf-8").split("## Duplicates")[0]
    pa.write_text(stale, encoding="utf-8")
    assert not duplicates._projects(pa.read_text(encoding="utf-8"), "bravo", "item")
    res = _dups(tmp_path, conn)
    _check_contract(tmp_path, res, "mark_semantic_duplicate")
    o = _by_id(res)["rev_ok"]
    assert o["outcome"] == "applied" and o["details"]["normalized"] is True
    assert res["normalized"] == 1 and res["applied"] == 0
    assert res["changed_pages"] == ["Items/alpha.md"]
    repaired = pa.read_text(encoding="utf-8")
    assert repaired != stale and duplicates._projects(repaired, "bravo", "item")
    assert pb.read_bytes() == bytes_b                             # the partner page was already right


def test_duplicate_read_back_unavailable_or_not_met(tmp_path, monkeypatch):
    conn = _graph(tmp_path)
    pa = _seed_node(tmp_path, conn, node_id=A, slug="alpha", title="Alpha")
    _seed_node(tmp_path, conn, node_id=B, slug="bravo", title="Bravo")
    _write_dup(tmp_path, _dup_item(A, B, rid="rev_ok"))

    def boom(*_a, **_k):
        raise OSError("unreadable")
    monkeypatch.setattr(duplicates, "_projection_holds", boom)    # the post-write read-back
    res = _dups(tmp_path, conn)                                   # no exception
    _check_contract(tmp_path, res, "mark_semantic_duplicate")
    o = _by_id(res)["rev_ok"]
    assert (o["outcome"], o["reason"]) == ("incomplete", "verification_unavailable")
    assert res["applied"] == 1 and res["graph_changed"] is True   # the executor's result stands
    assert duplicates._edge_active(conn, A, B)
    monkeypatch.undo()

    # the page is written, then loses its projection before the read-back: postcondition_not_met
    pa.write_text(pa.read_text(encoding="utf-8").split("## Duplicates")[0], encoding="utf-8")
    real = items.recompose_semantic_node_page

    def strip_after(*args, **kwargs):
        r = real(*args, **kwargs)
        if kwargs["node_id"] == A:
            pa.write_text(pa.read_text(encoding="utf-8").split("## Duplicates")[0], encoding="utf-8")
        return r
    monkeypatch.setattr(items, "recompose_semantic_node_page", strip_after)
    res = _dups(tmp_path, conn)
    _check_contract(tmp_path, res, "mark_semantic_duplicate")
    o = _by_id(res)["rev_ok"]
    assert (o["outcome"], o["reason"]) == ("incomplete", "postcondition_not_met")
    assert res["normalized"] == 1                                 # legacy count unchanged
    assert not duplicates._projects(pa.read_text(encoding="utf-8"), "bravo", "item")


def test_duplicate_self_pair_name_read_failure_keeps_the_refusal(tmp_path, monkeypatch):
    conn = _graph(tmp_path)
    _seed_node(tmp_path, conn, node_id=A, slug="alpha", title="Alpha")
    _write_dup(tmp_path, _dup_item(A, A, rid="rev_self"))

    def boom(*_a, **_k):
        raise RuntimeError("graph read failed")
    monkeypatch.setattr(graph, "get_node", boom)       # only the self-pair's name read reaches it
    res = _dups(tmp_path, conn)
    _check_contract(tmp_path, res, "mark_semantic_duplicate")
    o = _by_id(res)["rev_self"]
    assert (o["outcome"], o["reason"]) == ("skipped", "self_duplicate")
    assert o["details"]["subject"] == {"id": A, "name": A}


def test_duplicate_name_captured_before_mutation(tmp_path, monkeypatch):
    conn = _graph(tmp_path)
    _seed_node(tmp_path, conn, node_id=A, slug="alpha", title="Alpha")
    _seed_node(tmp_path, conn, node_id=B, slug="bravo", title="Bravo")
    _write_dup(tmp_path, _dup_item(A, B, rid="rev_ok"))
    _rename_after(monkeypatch, items, "recompose_semantic_node_page",
                  lambda kw: kw["wiki_dir"] / "Items" / "alpha.md")
    res = _dups(tmp_path, conn)
    assert parse_frontmatter((tmp_path / "wiki" / "Items" / "alpha.md").read_text())["title"] == "Renamed"
    assert _by_id(res)["rev_ok"]["details"]["subject"] == {"id": A, "name": "Alpha"}


def test_duplicate_failed_rerender_reports_incomplete_or_skipped(tmp_path, monkeypatch):
    conn = _graph(tmp_path)
    _seed_node(tmp_path, conn, node_id=A, slug="alpha", title="Alpha")
    _seed_node(tmp_path, conn, node_id=B, slug="bravo", title="Bravo")
    _write_dup(tmp_path, _dup_item(A, B, rid="rev_ok"))
    real = items.recompose_semantic_node_page

    def conflict_on_b(*args, **kwargs):
        return "page_conflict" if kwargs["node_id"] == B else real(*args, **kwargs)
    monkeypatch.setattr(items, "recompose_semantic_node_page", conflict_on_b)

    # the edge is written on this run, then B's projection fails: state changed, review unfinished
    res = _dups(tmp_path, conn)
    o = _by_id(res)["rev_ok"]
    assert (o["outcome"], o["reason"]) == ("incomplete", "page_conflict")
    assert o["details"]["edge_written"] is True
    assert o["details"]["subject"]["name"] == "Alpha" and o["details"]["duplicate_of"]["name"] == "Bravo"
    assert res["applied"] == 1        # legacy count unchanged (documented divergence)

    # re-run: edge active, A already projects, B's render still fails and nothing is written ->
    # the postcondition does not hold, so it's a skip (never already_in_effect)
    res = _dups(tmp_path, conn)
    o = _by_id(res)["rev_ok"]
    assert (o["outcome"], o["reason"]) == ("skipped", "page_conflict")
    assert res["skipped"] == [] and res["normalized"] == 1   # legacy counts unchanged (documented)
