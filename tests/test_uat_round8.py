"""UAT round 8 fixes (ADR-0066 part 2 UAT).

1. The duplicate check never proposes a merge involving a blocked tag (either side).
2. A kept tag's aliases can be edited on the document screen (and its JSON twin) — the only way to
   change them after the first keep, short of a merge; re-extraction never undoes the edit.
3. The document screen flags a name a tag shares with another live tag (what triggers a merge proposal).
4. The approved queue says whether apply carried out a merge / split / type change.
5. A blocked human merge says why in plain words (the typed reason stays at the end).

Review round 9: an untouched or stale alias box never undoes a newer edit; a re-approved item that
needs nothing more says "already in effect"; merge advice fits the tag's status.
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from fastapi.testclient import TestClient

from app.backend import graph
from app.backend import main as main_module
from app.backend import review_read
from app.backend.config import get_settings
from app.workers import identity_journal, items, merge_candidates, tag_links
from app.workers.wiki_render import render_item_page
from tests.test_merge_candidates import ITEM_TYPE, _graph, _item, _lint, _merge_items, _src
from tests.test_tag_links import _build, _keep, _sids, _view
from tests.test_tag_suggestions import _it, _page_fm, _run


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    settings.manifests_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


def _blocklist(tmp_path, *names):
    path = tmp_path / "policies" / tag_links.BLOCKLIST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(names) + "\n", encoding="utf-8")


def _audits(tmp_path, event):
    d = tmp_path / "reviews" / "audit_log"
    return [x for x in (json.loads(p.read_text()) for p in sorted(d.glob("*.json")))
            if x.get("event") == event]


def _alias_form(page):
    """What a browser submits for the page's alias editors: every box (collapsed or not) with its
    current text, plus the hidden list each box was shown with."""
    form = {}
    for name, text in re.findall(r"<textarea name='(edit_aliases_itm_[0-9a-f]+)'[^>]*>(.*?)</textarea>",
                                 page, re.S):
        form[name] = html.unescape(text)
    for name, value in re.findall(r"name='(shown_aliases_itm_[0-9a-f]+)' value='([^']*)'", page):
        form[name] = html.unescape(value)
    return form


# --- 1. no merge proposals involving a blocked tag ------------------------------------------------


def test_a_blocked_tag_takes_no_part_in_merge_proposals(tmp_path):
    gdb, conn = _graph(tmp_path)
    ai = _item(tmp_path, conn, "AI", aliases=["agentic AI"], sources=[_src(1), _src(2), _src(3)])
    agentic = _item(tmp_path, conn, "Agentic AI", sources=[_src(4)])
    agent = _item(tmp_path, conn, "AI Agent", aliases=["agentic AI"], sources=[_src(5)])
    conn.close()
    _blocklist(tmp_path, "AI")
    _lint(tmp_path, gdb)
    pairs = [(i["subject"]["survivor_node_id"], i["subject"]["absorbed_node_id"])
             for i in _merge_items(tmp_path)]
    # AI would have been the survivor (most support); now only the unblocked pair is proposed.
    assert pairs == [(agent, agentic)] or pairs == [(agentic, agent)]
    assert all(ai not in p for p in pairs)


def test_the_blocked_list_folds_like_the_merge_key(tmp_path):
    gdb, conn = _graph(tmp_path)
    a = _item(tmp_path, conn, "Large Language Models", aliases=["LLM"], sources=[_src(1)])
    _item(tmp_path, conn, "LLM", sources=[_src(2)])
    found = merge_candidates.find_merge_candidates(
        conn, wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews",
        blocked_keys=frozenset({merge_candidates.merge_key("llms")}))
    conn.close()
    assert found["proposals"] == [] and found["blocked"] == [items.node_id("LLM")]
    assert a not in found["blocked"]


# --- 2 + 3. alias edit of a kept tag, and the shared-name notes -----------------------------------


def _two_docs(tmp_path):
    body = "about the LLM, a large language model, also a foundation model"
    _build(tmp_path, {"a.md": "alpha " + body, "b.md": "beta " + body})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    _run(tmp_path, {"alpha": [_it("LLM", "model_family_architecture",
                                  ["Large Language Model", "Foundation Model"])]}, source_ids=[a])
    assert _keep(tmp_path, a, "LLM")["effect"] == "activated"
    return a, b, items.node_id("LLM")


def test_a_kept_tags_aliases_are_edited_on_the_document_screen(client, tmp_path):
    a, b, nid = _two_docs(tmp_path)
    page = client.get(f"/ui/reviews/sources/{a}").text
    assert f"name='edit_aliases_{nid}'" in page and "edit aliases" in page
    form = _alias_form(page)
    assert json.loads(form[f"shown_aliases_{nid}"]) == ["Large Language Model", "Foundation Model"]
    resp = client.post(f"/ui/reviews/sources/{a}/decide",
                       data=form | {f"edit_aliases_{nid}": "Large Language Model\n"})
    assert resp.status_code == 200 and "aliases updated" in resp.text
    assert _page_fm(tmp_path, "llm")["aliases"] == ["Large Language Model"]
    [rec] = _audits(tmp_path, "tag-aliases-edited")
    assert rec["from"] == ["Large Language Model", "Foundation Model"]
    assert rec["to"] == ["Large Language Model"] and rec["source_id"] == a
    # Re-extraction never brings the removed alias back.
    _run(tmp_path, {"beta": [_it("LLM", "model_family_architecture",
                                 ["Large Language Model", "Foundation Model"])]}, source_ids=[b])
    assert _page_fm(tmp_path, "llm")["aliases"] == ["Large Language Model"]
    # An unchanged box records nothing.
    resp = client.post(f"/ui/reviews/sources/{a}/decide",
                       data=_alias_form(client.get(f"/ui/reviews/sources/{a}").text))
    assert "No decisions were marked" in resp.text
    assert len(_audits(tmp_path, "tag-aliases-edited")) == 1


def test_a_drop_in_the_same_submit_wins_over_an_alias_edit(client, tmp_path):
    a, _b, nid = _two_docs(tmp_path)
    client.post(f"/ui/reviews/sources/{a}/decide",
                data={f"link_{nid}": "drop", f"edit_aliases_{nid}": ""})
    assert _audits(tmp_path, "tag-aliases-edited") == []
    assert _page_fm(tmp_path, "llm")["aliases"] == ["Large Language Model", "Foundation Model"]


def test_only_a_document_that_keeps_the_tag_edits_its_aliases(client, tmp_path):
    a, b, nid = _two_docs(tmp_path)
    # b only has it as a suggestion (or not at all): a forged edit box is ignored there.
    client.post(f"/ui/reviews/sources/{b}/decide", data={f"edit_aliases_{nid}": "x"})
    assert _audits(tmp_path, "tag-aliases-edited") == []
    resp = client.post(f"/reviews/sources/{b}/links", json={"node_id": nid, "action": "aliases",
                                                            "aliases": ["x"]})
    assert resp.status_code == 409
    # The JSON twin on the keeping document.
    resp = client.post(f"/reviews/sources/{a}/links", json={"node_id": nid, "action": "aliases",
                                                            "aliases": ["Foundation Model"]})
    assert resp.status_code == 200 and resp.json()["outcome"] == "aliases_updated"
    assert _page_fm(tmp_path, "llm")["aliases"] == ["Foundation Model"]
    bad = client.post(f"/reviews/sources/{a}/links", json={"node_id": nid, "action": "aliases",
                                                           "aliases": "Foundation Model"})
    assert bad.status_code == 400


def _kept_on_both(tmp_path):
    a, b, nid = _two_docs(tmp_path)
    _run(tmp_path, {"beta": [_it("LLM", "model_family_architecture",
                                 ["Large Language Model", "Foundation Model"])]}, source_ids=[b])
    assert _keep(tmp_path, b, "LLM")["outcome"] != "blocked"
    return a, b, nid


def test_an_untouched_stale_form_never_undoes_a_newer_edit(client, tmp_path):
    # Review round 9 (the reviewer's repro): open A's screen, remove "Foundation Model" through B,
    # then submit A's form without touching its alias box.
    a, b, nid = _kept_on_both(tmp_path)
    stale_a = _alias_form(client.get(f"/ui/reviews/sources/{a}").text)
    form_b = _alias_form(client.get(f"/ui/reviews/sources/{b}").text)
    client.post(f"/ui/reviews/sources/{b}/decide",
                data=form_b | {f"edit_aliases_{nid}": "Large Language Model"})
    assert _page_fm(tmp_path, "llm")["aliases"] == ["Large Language Model"]
    resp = client.post(f"/ui/reviews/sources/{a}/decide", data=stale_a)
    assert "No decisions were marked" in resp.text
    assert _page_fm(tmp_path, "llm")["aliases"] == ["Large Language Model"]
    assert len(_audits(tmp_path, "tag-aliases-edited")) == 1


def test_an_edit_on_a_stale_page_is_refused(client, tmp_path):
    a, b, nid = _kept_on_both(tmp_path)
    stale_a = _alias_form(client.get(f"/ui/reviews/sources/{a}").text)
    form_b = _alias_form(client.get(f"/ui/reviews/sources/{b}").text)
    client.post(f"/ui/reviews/sources/{b}/decide",
                data=form_b | {f"edit_aliases_{nid}": "Large Language Model"})
    resp = client.post(f"/ui/reviews/sources/{a}/decide",
                       data=stale_a | {f"edit_aliases_{nid}": "Foundation Model\nLLMs"})
    assert "the aliases changed after this page was shown (now: Large Language Model)" in resp.text
    assert "aliases_changed_since_shown" in resp.text
    assert _page_fm(tmp_path, "llm")["aliases"] == ["Large Language Model"]
    assert len(_audits(tmp_path, "tag-aliases-edited")) == 1


def test_a_form_without_the_shown_list_edits_nothing(client, tmp_path):
    # A page from before the hidden field existed (or a hand-built form): an edit is refused with a
    # reload hint; a box equal to the current aliases stays silent.
    a, _b, nid = _two_docs(tmp_path)
    resp = client.post(f"/ui/reviews/sources/{a}/decide",
                       data={f"edit_aliases_{nid}": "Large Language Model"})
    assert "this page is out of date" in resp.text
    resp = client.post(f"/ui/reviews/sources/{a}/decide",
                       data={f"edit_aliases_{nid}": "Large Language Model\nFoundation Model",
                             f"shown_aliases_{nid}": "not json"})
    assert "No decisions were marked" in resp.text
    assert _page_fm(tmp_path, "llm")["aliases"] == ["Large Language Model", "Foundation Model"]
    assert _audits(tmp_path, "tag-aliases-edited") == []


def test_the_json_twin_checks_expected_aliases(client, tmp_path):
    a, _b, nid = _two_docs(tmp_path)
    url = f"/reviews/sources/{a}/links"
    stale = client.post(url, json={"node_id": nid, "action": "aliases", "aliases": ["LLMs"],
                                   "expected_aliases": ["Large Language Model"]})
    assert stale.status_code == 409 and stale.json()["detail"] == "aliases_changed_since_shown"
    bad = client.post(url, json={"node_id": nid, "action": "aliases", "aliases": ["LLMs"],
                                 "expected_aliases": "Large Language Model"})
    assert bad.status_code == 400
    ok = client.post(url, json={"node_id": nid, "action": "aliases", "aliases": ["LLMs"],
                                "expected_aliases": ["Large Language Model", "Foundation Model"]})
    assert ok.status_code == 200 and _page_fm(tmp_path, "llm")["aliases"] == ["LLMs"]


def test_the_primitive_refuses_a_tag_not_in_use(tmp_path):
    body = "about the LLM, a large language model"
    _build(tmp_path, {"a.md": "alpha " + body})
    a = _sids(tmp_path)["a.md"]
    _run(tmp_path, {"alpha": [_it("LLM", "model_family_architecture", ["Large Language Model"])]},
         source_ids=[a])
    gconn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        out = tag_links.edit_aliases(gconn, source_id=a, node_id=items.node_id("LLM"), aliases=[],
                                     wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews")
    finally:
        gconn.close()
    assert out["outcome"] == "blocked" and out["reason"] == "item_not_in_use"


def test_a_name_shared_with_another_tag_is_flagged_on_the_document_screen(client, tmp_path):
    body = "about agentic AI and the AI agent"
    _build(tmp_path, {"a.md": "alpha " + body, "b.md": "beta " + body})
    sids = _sids(tmp_path)
    a, b = sids["a.md"], sids["b.md"]
    # As in UAT round 8: "Agentic AI" exists (suggested on b) before "AI Agent" arrives with the
    # alias "agentic AI" — an alias never resolves a different name, so both tags exist.
    _run(tmp_path, {"beta": [_it("Agentic AI", "ai_topic_area")]}, source_ids=[b])
    _run(tmp_path, {"alpha": [_it("AI Agent", "ai_topic_area", ["agentic AI"])]}, source_ids=[a])
    assert _keep(tmp_path, a, "AI Agent")["effect"] == "activated"
    agentic = items.node_id("Agentic AI")
    row = next(r for r in _view(tmp_path, b)["suggestions"] if r["node_id"] == agentic)
    assert [(c["name"], c["other_title"], c["other_field"]) for c in row["collisions"]] == [
        ("Agentic AI", "AI Agent", "alias")]
    kept_a = next(r for r in _view(tmp_path, a)["kept"] if r["title"] == "AI Agent")
    assert [(c["name"], c["other_title"], c["other_status"]) for c in kept_a["collisions"]] == [
        ("agentic AI", "Agentic AI", "candidate")]
    page = client.get(f"/ui/reviews/sources/{a}").text
    assert "is also the title of “Agentic AI” (suggested, not kept yet)" in page


def test_collision_lookup_ignores_the_tag_itself_and_dead_tags():
    names = review_read._name_index({
        "itm_a": {"title": "AI Agent", "status": "active", "aliases": ["agentic AI", "AI agents"]},
        "itm_b": {"title": "Agentic AI", "status": "merged", "aliases": []},
        "itm_c": {"title": "Agents", "status": "unused", "aliases": ["AI agent"]},
    })
    out = review_read.alias_collisions(names, "itm_a", "AI Agent", ["agentic AI", "AI agents"])
    assert {(c["name"], c["other_id"]) for c in out} == {("AI Agent", "itm_c"), ("AI agents", "itm_c")}


# --- 4. the approved queue marks what apply carried out --------------------------------------------


def _approved(tmp_path, rid, rtype, decided_at="2026-10-27T21:32:00+00:00"):
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": rtype, "status": "approved", "priority": "low",
        "created_at": "2026-10-27T15:45:49+00:00", "subject": {"node_id": "itm_0000000000000001"},
        "decided_by": "human", "decided_at": decided_at}), encoding="utf-8")


def test_the_approved_queue_says_what_apply_carried_out(client, tmp_path):
    _approved(tmp_path, "rev_aaaaaaaaaaaaaaaa", "change_item_type")
    _approved(tmp_path, "rev_bbbbbbbbbbbbbbbb", "merge_items")
    _approved(tmp_path, "rev_cccccccccccccccc", "archive_source")
    audit = tmp_path / "reviews" / "audit_log"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / "rev_aaaaaaaaaaaaaaaa-retyped-0a0a0a0a.json").write_text(json.dumps({
        "review_id": "rev_aaaaaaaaaaaaaaaa", "decision": "retyped", "decided_by": "system",
        "decided_at": "2026-10-27T21:33:54+00:00"}), encoding="utf-8")
    # A record older than the approval (an earlier approve → apply → reopen cycle) isn't "applied";
    # with no graph to check the live state, it can't be verified (review rounds 9–10).
    (audit / "rev_bbbbbbbbbbbbbbbb-merged-0b0b0b0b.json").write_text(json.dumps({
        "review_id": "rev_bbbbbbbbbbbbbbbb", "decision": "merged", "decided_by": "system",
        "decided_at": "2026-10-27T20:00:00+00:00"}), encoding="utf-8")
    page = client.get("/ui/reviews?status=approved").text
    assert "<th>applied</th>" in page
    assert "applied 2026-10-27T21:33:54+00:00</td>" in page
    assert ("cannot verify — Apply re-checks it (last applied 2026-10-27T20:00:00+00:00, before this "
            "approval)") in page
    assert "<td>—</td>" in page
    assert "<th>applied</th>" not in client.get("/ui/reviews").text      # pending view unchanged


def _approve(tmp_path, rid, rtype, subject, proposal=None, decided_at="2026-10-27T22:00:00+00:00"):
    d = tmp_path / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({
        "review_id": rid, "type": rtype, "status": "approved", "priority": "low",
        "created_at": "2026-10-27T15:45:49+00:00", "subject": subject, "proposal": proposal or {},
        "decided_by": "human", "decided_at": decided_at}), encoding="utf-8")


def _executor_record(tmp_path, rid, decision, at="2026-10-27T21:33:54+00:00"):
    audit = tmp_path / "reviews" / "audit_log"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / f"{rid}-{decision}-0c0c0c0c.json").write_text(json.dumps({
        "review_id": rid, "decision": decision, "decided_by": "system", "decided_at": at}),
        encoding="utf-8")


def _tombstone(tmp_path, conn, name, merged_into):
    """A merged tag as the merge executor leaves it: graph status `merged` + a tombstone page."""
    nid = _item(tmp_path, conn, name, status="merged")
    (tmp_path / "wiki" / "Items" / f"{items._slug(name)}.md").write_text(render_item_page({
        "node_id": nid, "item_type": ITEM_TYPE, "title": name, "aliases": [], "confidence": "low",
        "status": "merged", "merged_into": merged_into, "merged_at": "2026-10-27T21:00:00+00:00",
        "merge_review_id": "rev_0000000000000000"}), encoding="utf-8")
    return nid


def _retype(tmp_path, rid, nid, to_type):
    _approve(tmp_path, rid, "change_item_type", {"node_id": nid, "to_item_type": to_type},
             {"to_item_type": to_type})


def _merge(tmp_path, rid, survivor, absorbed):
    _approve(tmp_path, rid, "merge_items",
             {"survivor_node_id": survivor, "absorbed_node_id": absorbed}, {"to_status": "merged"})


def _cells(page):
    """review_id → the text of its "applied" cell (the row's last cell)."""
    out = {}
    for rid, row in re.findall(r"<a href='/ui/reviews/(rev_[0-9a-f]+)'>(.*?)</tr>", page, re.S):
        out[rid] = html.unescape(re.sub(r"<[^>]+>", "", row.rsplit("<td>", 1)[1])).strip()
    return out


def test_the_applied_column_uses_the_executors_completion_checks(client, tmp_path):
    # Review rounds 9–10: with no record after the approval (a re-approved item makes apply a
    # no-op), the cell reads the durable state through the executor's own checks — and says it
    # can't tell whenever that state is incomplete.
    _gdb, conn = _graph(tmp_path)
    llm = _item(tmp_path, conn, "LLM", sources=[_src(1)])
    agents = _item(tmp_path, conn, "Agents", sources=[_src(2)])
    pageless = _item(tmp_path, conn, "Pageless", sources=[_src(3)])
    done = _tombstone(tmp_path, conn, "Agent", merged_into=agents)
    elsewhere = _tombstone(tmp_path, conn, "Bot", merged_into=llm)
    unfinished = _tombstone(tmp_path, conn, "Robot", merged_into=agents)
    bare = _item(tmp_path, conn, "Droid", status="merged")              # no tombstone destination
    live = _item(tmp_path, conn, "Agentic", sources=[_src(4)])
    conn.close()
    (tmp_path / "wiki" / "Items" / "pageless.md").unlink()
    cases = {
        "rev_1000000000000001": ("in effect", lambda r: _retype(tmp_path, r, llm, ITEM_TYPE)),
        "rev_1000000000000002": ("not yet", lambda r: _retype(tmp_path, r, agents, "domain")),
        "rev_1000000000000003": ("cannot", lambda r: _retype(tmp_path, r, pageless, "domain")),
        "rev_1000000000000004": ("in effect", lambda r: _merge(tmp_path, r, agents, done)),
        "rev_1000000000000005": ("cannot", lambda r: _merge(tmp_path, r, agents, elsewhere)),
        "rev_1000000000000006": ("cannot", lambda r: _merge(tmp_path, r, agents, unfinished)),
        "rev_1000000000000007": ("cannot", lambda r: _merge(tmp_path, r, agents, bare)),
        "rev_1000000000000008": ("not yet", lambda r: _merge(tmp_path, r, agents, live)),
        "rev_1000000000000009": ("cannot", lambda r: _approve(tmp_path, r, "split_item",
                                                              {"node_id": llm})),
    }
    decisions = {"change_item_type": "retyped", "merge_items": "merged", "split_item": "split"}
    for rid, (_want, filed) in cases.items():
        filed(rid)
        rtype = json.loads((tmp_path / "reviews" / "approved" / f"{rid}.json").read_text())["type"]
        _executor_record(tmp_path, rid, decisions[rtype])              # all before the re-approval
    identity_journal.begin(tmp_path / "reviews", "rev_1000000000000006", op="merge_items",
                           nodes=[agents, unfinished])                  # interrupted, not finished
    cells = _cells(client.get("/ui/reviews?status=approved").text)
    want = {"in effect": "already in effect — nothing left to apply",
            "not yet": "not yet — run Apply",
            "cannot": ("cannot verify — Apply re-checks it (last applied 2026-10-27T21:33:54+00:00, "
                       "before this approval)")}
    assert {rid: cells[rid] for rid in cases} == {rid: want[w] for rid, (w, _f) in cases.items()}
    # Without the graph nothing can be checked.
    item = json.loads((tmp_path / "reviews" / "approved" / "rev_1000000000000004.json").read_text())
    assert review_read.applied_state(tmp_path / "reviews", item)["state"] == "cannot_verify"


# --- 5. plain-language merge errors ----------------------------------------------------------------


def test_a_blocked_merge_says_why_in_plain_words(client, tmp_path):
    _gdb, conn = _graph(tmp_path)
    _item(tmp_path, conn, "Financial Reporting", status="candidate")
    _item(tmp_path, conn, "FINREP", aliases=["Financial Reporting"], sources=[_src(1)])
    conn.close()
    resp = client.post("/reviews/merge", json={"survivor": "financial-reporting",
                                               "absorbed": "finrep"})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "'financial-reporting' is not in use yet (suggested, not kept)" in detail
    assert "keep it on a document first" in detail and detail.endswith("(survivor_not_active)")
    same = client.post("/reviews/merge", json={"survivor": "finrep", "absorbed": "finrep"})
    assert "the same tag" in same.json()["detail"]
    preview = client.get("/ui/reviews/merge/preview",
                         params={"survivor": "financial-reporting", "absorbed": "finrep"})
    assert "not in use yet" in preview.text


def test_merge_advice_fits_the_tags_status(client, tmp_path):
    # Review round 9: "keep it first" only helps a suggestion; hidden, merged and retired tags get
    # advice that can actually be followed.
    _gdb, conn = _graph(tmp_path)
    survivor = _item(tmp_path, conn, "Large Language Model", sources=[_src(1)])
    _item(tmp_path, conn, "Hidden Tag", status="hidden")
    _item(tmp_path, conn, "Old Tag", status="deprecated_candidate")
    gone = _item(tmp_path, conn, "LLMs", status="merged")
    conn.close()
    (tmp_path / "wiki" / "Items" / "llms.md").write_text(render_item_page({
        "node_id": gone, "item_type": ITEM_TYPE, "title": "LLMs", "aliases": [],
        "confidence": "low", "status": "merged", "merged_into": survivor,
        "merged_into_link": "Items/large-language-model", "merged_at": "2026-10-27T21:00:00+00:00",
        "merge_review_id": "rev_5555555555555555"}), encoding="utf-8")

    def detail(absorbed, survivor_ref="large-language-model"):
        resp = client.post("/reviews/merge", json={"survivor": survivor_ref, "absorbed": absorbed})
        assert resp.status_code == 409
        return resp.json()["detail"]

    hidden = detail("hidden-tag")
    assert "'hidden-tag' is hidden — it must be unhidden first" in hidden
    assert "keep it on a document" not in hidden and hidden.endswith("(absorbed_not_active)")
    assert "is already merged into 'Large Language Model' — nothing to do" in detail("llms")
    assert "is retired (status: deprecated_candidate)" in detail("old-tag")
    assert "was itself merged into 'Large Language Model'" in detail("hidden-tag", "llms")
