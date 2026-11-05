"""ADR-0072 B2b — the flat review queue: readable subject, grouping by type, local time, the detail
heading, and two-step bulk reject/defer with a stale-selection fingerprint (ADR-0070 §5)."""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import main as main_module  # noqa: E402
from app.backend import review_html  # noqa: E402
from tests.test_review_html import _write_review, client  # noqa: E402,F401

NID_A, NID_B = "itm_" + "a" * 16, "itm_" + "b" * 16


def _tag_page(root, nid, title, slug):
    page = root / "wiki" / "Items" / f"{slug}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f'---\ntype: item\nitem_id: "{nid}"\nitem_type: method_technique\n'
                    f'title: "{title}"\nstatus: active\naliases: []\n---\n', encoding="utf-8")


def _review(root, rid, rtype, subject, *, state="pending", created="2026-01-15T12:00:00+00:00",
            **extra):
    _write_review(root, state, {"review_id": rid, "type": rtype, "status": state,
                                "priority": "low", "created_at": created, "subject": subject,
                                "proposal": {"to_status": "active"}, "context": {}, **extra})


@pytest.fixture
def madrid(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Madrid")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


@pytest.fixture
def queue(tmp_path):
    _tag_page(tmp_path, NID_A, "Data Mesh", "data-mesh")
    _tag_page(tmp_path, NID_B, "Data-Mesh", "data-mesh--bbbbbbbb")
    _review(tmp_path, "rev_merge", "merge_items",
            {"survivor_node_id": NID_A, "absorbed_node_id": NID_B})
    _review(tmp_path, "rev_promote", "promote_candidate_node", {"node_id": NID_A},
            created="2026-07-15T12:00:00+00:00")
    _review(tmp_path, "rev_hide", "hide_content", {"source_id": "src_" + "0" * 16})
    return tmp_path


def _audits(root, rid):
    return sorted(p.name for p in (root / "reviews" / "audit_log").glob(f"{rid}-*.json"))


def _state_files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in (root / "reviews").rglob("*.json")}


# --- the queue ----------------------------------------------------------------------------------


def test_the_queue_shows_readable_subjects_grouped_by_type_in_local_time(client, queue, madrid):  # noqa: F811
    html = client.get("/ui/reviews").text
    assert "Data-Mesh into Data Mesh" in html                   # a merge reads "X into Y"
    assert ">Data Mesh</a>" in html                             # a tag by its title
    assert "src_0000000000000000" in html                      # no page: the id stands in
    for group in ("Merge tags (1)", "Promote tags (1)", "Hide documents (1)"):
        assert group in html
    assert "2026-01-15 13:00 CET" in html and "2026-07-15 14:00 CEST" in html   # DST per timestamp
    assert "name='rid' value='rev_merge'" in html and "checked" not in html     # none preselected


def test_local_time_names_the_zone_and_leaves_non_timestamps_alone(madrid):
    assert review_html.local_time("2026-03-29T00:30:00Z") == "2026-03-29 01:30 CET"
    assert review_html.local_time("2026-03-29T01:30:00Z") == "2026-03-29 03:30 CEST"
    assert review_html.local_time("yesterday") == "yesterday" and review_html.local_time(None) == ""


def test_the_detail_page_heads_with_the_subject_and_the_id(client, queue):  # noqa: F811
    html = client.get("/ui/reviews/rev_merge").text
    assert "<h1>Data-Mesh into Data Mesh <small>— Merge tags</small></h1>" in html
    assert "<code>rev_merge</code>" in html


def test_subjects_are_escaped(client, tmp_path):  # noqa: F811
    _tag_page(tmp_path, NID_A, "<script>x</script>", "evil")
    _review(tmp_path, "rev_x", "promote_candidate_node", {"node_id": NID_A})
    html = client.get("/ui/reviews").text
    assert "<script>x" not in html and "&lt;script&gt;x&lt;/script&gt;" in html


# --- bulk: confirmation writes nothing ----------------------------------------------------------


def _confirm(http, action, ids):
    return http.post("/ui/reviews/bulk/confirm",
                     data={"bulk_action": action, "rid": ids})


def _form_of(html):
    """The confirmation page's form, as the browser would submit it."""
    ids = re.findall(r"name='rid' value='([^']+)'", html)
    fps = dict(re.findall(r"name='fp_([^']+)' value='([0-9a-f]{64})'", html))
    return {"rid": ids, **{f"fp_{k}": v for k, v in fps.items()}}


def test_the_confirmation_lists_lasting_effects_and_writes_nothing(client, queue):  # noqa: F811
    before = _state_files(queue)
    html = _confirm(client, "reject", ["rev_merge", "rev_promote"]).text
    assert _state_files(queue) == before
    assert "Data-Mesh into Data Mesh" in html and "Merge tags" in html
    assert "blocks filing this merge, or the reverse merge" in html
    assert len(_form_of(html)["rid"]) == 2


def test_the_bulk_post_is_origin_guarded(client, queue):  # noqa: F811
    resp = client.post("/ui/reviews/bulk", data={"bulk_action": "reject", "rid": ["rev_merge"]},
                       headers={"origin": "http://evil.example"})
    assert resp.status_code == 403
    assert (queue / "reviews" / "pending" / "rev_merge.json").exists()


# --- bulk: recording ----------------------------------------------------------------------------


def _record(http, action, form, note=""):
    return http.post("/ui/reviews/bulk", data={"bulk_action": action, "note": note, **form})


def test_mixed_eligibility_duplicates_and_stale_selections(client, queue):  # noqa: F811
    _review(queue, "rev_done", "promote_candidate_node", {"node_id": NID_B}, state="approved")
    form = _form_of(_confirm(client, "reject",
                             ["rev_merge", "rev_promote", "rev_hide", "rev_done", "rev_gone"]).text)
    form["rid"] = form["rid"] + ["rev_merge"]                    # a duplicate counts once
    stale = json.loads((queue / "reviews" / "pending" / "rev_hide.json").read_text())
    stale["proposal"] = {"to_status": "hidden", "reason": "re-proposed"}   # changed meanwhile
    (queue / "reviews" / "pending" / "rev_hide.json").write_text(json.dumps(stale))
    html = _record(client, "reject", form, note="bulk <b>cleanup</b>").text
    assert (queue / "reviews" / "rejected" / "rev_merge.json").exists()
    assert (queue / "reviews" / "rejected" / "rev_promote.json").exists()
    assert (queue / "reviews" / "pending" / "rev_hide.json").exists()    # changed: not recorded
    assert "changed — review again" in html and "no longer open (it is approved)" in html
    assert _audits(queue, "rev_merge") == ["rev_merge-rejected.json"]   # once, despite duplicate
    note = json.loads((queue / "reviews" / "audit_log" / "rev_merge-rejected.json").read_text())
    assert note["note"] == "bulk <b>cleanup</b>"
    assert "<b>cleanup" not in html                               # result page escapes
    assert "/ui/reviews/rev_merge'" in html and "Nothing was applied" in html
    assert not (queue / "reviews" / "approved" / "rev_merge.json").exists()   # no Apply ran


def test_an_interrupted_batch_keeps_what_it_recorded_and_a_retry_never_duplicates(
        client, queue, monkeypatch):  # noqa: F811
    form = _form_of(_confirm(client, "reject", ["rev_merge", "rev_promote"]).text)
    real = main_module._record_decision
    calls = []

    def flaky(rid, decision, body):
        calls.append(rid)
        if len(calls) == 2:
            raise RuntimeError("interrupted")
        return real(rid, decision, body)
    monkeypatch.setattr(main_module, "_record_decision", flaky)
    with pytest.raises(RuntimeError):
        _record(client, "reject", form)
    first = calls[0]
    assert (queue / "reviews" / "rejected" / f"{first}.json").exists()   # stays recorded
    monkeypatch.setattr(main_module, "_record_decision", real)
    html = _record(client, "reject", form).text
    assert "already rejected" in html
    for rid in ("rev_merge", "rev_promote"):
        assert _audits(queue, rid) == [f"{rid}-rejected.json"]


def test_bulk_defer_then_defer_again_is_already_deferred(client, queue):  # noqa: F811
    form = _form_of(_confirm(client, "defer", ["rev_promote"]).text)
    _record(client, "defer", form)
    item = json.loads((queue / "reviews" / "pending" / "rev_promote.json").read_text())
    assert item["status"] == "deferred"
    html = _record(client, "defer", _form_of(_confirm(client, "defer", ["rev_promote"]).text)).text
    assert "already deferred" in html
    # a deferred review can still be bulk-rejected
    _record(client, "reject", _form_of(_confirm(client, "reject", ["rev_promote"]).text))
    assert (queue / "reviews" / "rejected" / "rev_promote.json").exists()


def test_the_queue_offers_bulk_only_where_decisions_are_possible(client, queue):  # noqa: F811
    _review(queue, "rev_done", "promote_candidate_node", {"node_id": NID_B}, state="approved")
    assert "Reject selected" in client.get("/ui/reviews").text
    assert "Reject selected" not in client.get("/ui/reviews?status=approved").text


# --- B2b review round 1: the evidence producers keep in `context` is part of the fingerprint ------


def _edit(root, rid, fn):
    path = root / "reviews" / "pending" / f"{rid}.json"
    item = json.loads(path.read_text(encoding="utf-8"))
    fn(item)
    path.write_text(json.dumps(item), encoding="utf-8")


def test_changed_merge_match_evidence_is_changed_review_again(client, queue):  # noqa: F811
    _edit(queue, "rev_merge", lambda it: it["context"].update(
        match_evidence={"rule": "merge_key", "matched": ["data mesh"]}))
    form = _form_of(_confirm(client, "reject", ["rev_merge"]).text)
    _edit(queue, "rev_merge", lambda it: it["context"]["match_evidence"].update(
        matched=["data mesh", "data-mesh"]))                     # only the evidence changed
    html = _record(client, "reject", form).text
    assert "changed — review again" in html
    assert (queue / "reviews" / "pending" / "rev_merge.json").exists()
    assert not (queue / "reviews" / "rejected" / "rev_merge.json").exists()


@pytest.mark.parametrize("change", [
    lambda it: it["context"]["judged_evidence"].update(window="12000 chars"),
    lambda it: it["context"].update(scope={"documents": ["src_" + "1" * 16]}),
])
def test_changed_contradiction_evidence_or_scope_is_skipped(client, tmp_path, change):  # noqa: F811
    _write_review(tmp_path, "pending", {
        "review_id": "rev_c", "type": "resolve_contradiction", "status": "pending",
        "priority": "low", "created_at": "2026-01-15T12:00:00+00:00",
        "subject": {"claim_a": "clm_a", "claim_b": "clm_b"}, "proposal": {},
        "context": {"judged_evidence": {"window": "4000 chars"}, "scope": {"documents": []}}})
    form = _form_of(_confirm(client, "reject", ["rev_c"]).text)
    _edit(tmp_path, "rev_c", change)
    html = _record(client, "reject", form).text
    assert "changed — review again" in html
    assert not (tmp_path / "reviews" / "rejected" / "rev_c.json").exists()


def test_a_retiring_review_is_refused_while_the_rest_of_the_batch_proceeds(client, queue):  # noqa: F811
    form = _form_of(_confirm(client, "reject", ["rev_merge", "rev_promote", "rev_hide"]).text)
    _edit(queue, "rev_promote", lambda it: it.update(retiring={"started_at": "2026-01-15T12:00:00Z"}))
    html = _record(client, "reject", form).text
    assert (queue / "reviews" / "rejected" / "rev_merge.json").exists()
    assert (queue / "reviews" / "rejected" / "rev_hide.json").exists()
    assert (queue / "reviews" / "pending" / "rev_promote.json").exists()   # refused, untouched
    assert "being retired" in html
