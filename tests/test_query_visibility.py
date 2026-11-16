"""ADR-0074 change 7a-2 — the final visibility check before the model call (Q3) and the guarded
save of a query answer (Q4).

Q3: `answer_query` re-checks the evidence pack's documents, fresh, after the pack is built and
immediately before the provider request is prepared — shared by `/query` and its eval runner. Q4:
`save: true` re-checks every cited document under the write lock; any ineligible citation refuses
the whole save (no page, no log line, no revision advance) while the answer is still returned.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.backend import keyword_index, manifests, publication_queue, revision, search, write_lock  # noqa: E402,E501
from app.backend import main as main_module  # noqa: E402
from app.backend.config import get_settings  # noqa: E402
from app.workers import query  # noqa: E402

VIS = "src_00000000000000a1"
HID = "src_00000000000000b2"
Q = "lighthouse keeper logbook"


def _doc(root: Path, sid: str) -> None:
    text = f"The {Q} records storms for {sid}."
    rec = {"chunk_id": f"{sid}::0000", "source_id": sid, "ordinal": 0, "kind": "prose",
           "heading_path": [], "section": None, "text": text, "char_start": 0,
           "char_end": len(text), "page": 1, "page_end": 1, "table_reference": None,
           "sheet_reference": None}
    for sub in ("chunks", "markdown"):
        (root / "normalized" / sub).mkdir(parents=True, exist_ok=True)
    (root / "normalized" / "chunks" / f"{sid}.jsonl").write_text(json.dumps(rec) + "\n",
                                                                encoding="utf-8")
    (root / "normalized" / "markdown" / f"{sid}.md").write_text(text, encoding="utf-8")
    (root / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    (root / "raw" / "manifests" / f"{sid}.json").write_text(json.dumps({"source_id": sid}),
                                                            encoding="utf-8")
    sp = root / "wiki" / "Sources" / f"{sid}.md"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(f"---\ntype: source\nsource_id: {sid}\ntitle: Doc {sid[-2:]}\nstatus: active\n"
                  "language: en\n---\n\n# Doc\n\n> [!summary]\n> doc\n", encoding="utf-8")


class _Client:
    """Cites every pack item; records each prompt pack; `during` runs inside the model call."""

    def __init__(self, during=None) -> None:
        self.packs: list[list[dict]] = []
        self.during = during

    def provider_available(self, model_ref):
        return True

    def resolve_run_model(self, chain):
        return chain.split(",")[0].strip(), True

    def chain_available(self, chain):
        return True

    def parse(self, messages, schema, model_ref, **kwargs):
        pack = json.loads(messages[-1]["content"].split("EVIDENCE:\n", 1)[1])
        self.packs.append(pack)
        if self.during:
            self.during()
        return {"claims": [{"text": f"Grounded claim for {e['evidence_id']}.",
                            "evidence_ids": [e["evidence_id"]]} for e in pack]}


@pytest.fixture
def vault(tmp_path, monkeypatch):
    for sid in (VIS, HID):
        _doc(tmp_path, sid)
    keyword_index.reindex(tmp_path, force=True)
    settings = get_settings(tmp_path)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return tmp_path


def _use(monkeypatch, client: _Client) -> _Client:
    monkeypatch.setattr(main_module, "_query_client", lambda: client)
    return client


def _after_pack(monkeypatch, action) -> None:
    """Run `action` right after the evidence pack is built (its Markdown read)."""
    real = query.build_evidence_pack

    def build(*a, **kw):
        pack = real(*a, **kw)
        action()
        return pack
    monkeypatch.setattr(query, "build_evidence_pack", build)


def _hide(root: Path, sid: str = HID, status: str = "hidden"):
    return lambda: manifests.set_status(root / "raw" / "manifests", sid, status)


def _damage(root: Path, sid: str = HID):
    return lambda: (root / "raw" / "manifests" / f"{sid}.json").write_text("{bad", "utf-8")


def _ask(**body) -> dict:
    return TestClient(main_module.app).post(
        "/query", json={"question": Q, "mode": "keyword", **body}).json()


def _cited(r: dict) -> set[str]:
    return {c["source_id"] for c in r["citations"]}


def _pack_sources(client: _Client) -> set[str]:
    return {e["source_id"] for e in client.packs[-1]}


# --- Q3: the final visibility check ------------------------------------------------------------


@pytest.mark.parametrize("status", ["hidden", "archived"])
def test_a_hide_during_pack_construction_keeps_its_evidence_out_of_the_call(
        vault, monkeypatch, status):
    client = _use(monkeypatch, _Client())
    _after_pack(monkeypatch, _hide(vault, status=status))
    r = _ask()
    assert _pack_sources(client) == {VIS}                             # never sent
    assert [e["evidence_id"] for e in client.packs[-1]] == ["e1"]     # renumbered, compact
    assert _cited(r) == {VIS} and not r["abstained"]                  # lookup filtered with it
    assert any("1 evidence item(s) removed by the final visibility check (ADR-0074): a document's "
               "eligibility changed during the query" in n
               for n in r["notes"])
    assert HID not in json.dumps(r["notes"])                          # a count, never an identity


def test_complete_exclusion_abstains_without_a_model_call(vault, monkeypatch):
    client = _use(monkeypatch, _Client())
    _after_pack(monkeypatch, lambda: (_hide(vault)(), _hide(vault, VIS)()))
    r = _ask()
    assert client.packs == []                                         # no provider request
    assert r["abstained"] is True and r["citations"] == []
    assert any("2 evidence item(s) removed" in n for n in r["notes"])


def test_a_manifest_damaged_during_pack_construction_excludes_its_evidence(vault, monkeypatch):
    client = _use(monkeypatch, _Client())
    _after_pack(monkeypatch, _damage(vault))
    r = _ask()
    assert _pack_sources(client) == {VIS} and _cited(r) == {VIS}


def test_the_final_check_uses_the_requests_explicit_statuses(vault, monkeypatch):
    client = _use(monkeypatch, _Client())
    _after_pack(monkeypatch, _hide(vault))
    r = _ask(source_status="active,hidden")                           # hidden is still eligible
    assert _pack_sources(client) == {VIS, HID} and _cited(r) == {VIS, HID}
    assert not any("final visibility check" in n for n in r["notes"])


def test_the_final_check_refuses_a_status_outside_the_explicit_set(vault, monkeypatch):
    client = _use(monkeypatch, _Client())
    _after_pack(monkeypatch, _hide(vault, VIS, "archived"))
    r = _ask(source_status="active,hidden")                           # archived is not in it
    assert _pack_sources(client) == {HID} and _cited(r) == {HID}


def test_no_visibility_change_means_no_removal_and_no_note(vault, monkeypatch):
    client = _use(monkeypatch, _Client())
    r = _ask()
    assert _pack_sources(client) == {VIS, HID} and _cited(r) == {VIS, HID}
    assert not any("final visibility check" in n for n in r["notes"])
    assert r["save_refused"] is None


def test_the_eval_runner_shares_the_final_check(vault, monkeypatch):
    client = _Client()
    _after_pack(monkeypatch, _hide(vault))
    run = main_module._eval_query_fn(client, None, "fake/model")

    class Case:
        question, mode = Q, "keyword"
    out = run(Case())
    assert _pack_sources(client) == {VIS} and out["cited_source_ids"] == [VIS]


def test_answer_query_without_a_check_is_unchanged(vault):
    client = _Client()
    hits = [{"source_id": HID, "char_start": 0, "char_end": 10, "chunk_id": f"{HID}::0000"}]
    ans = query.answer_query(question=Q, evidence_hits=hits, client=client, model_ref="m",
                             markdown_dir=vault / "normalized" / "markdown")
    assert ans.visibility_removed == 0 and _pack_sources(client) == {HID}


# --- Q4: the guarded save ------------------------------------------------------------------------


def _queries(root: Path) -> list[Path]:
    return sorted((root / "wiki" / "Queries").glob("*.md"))


def _log(root: Path) -> str:
    path = root / "wiki" / "log.md"
    return path.read_text() if path.exists() else ""


def _assert_refused(root: Path, r: dict, count: int, before_rev: dict, before_log: str) -> None:
    assert r["query_id"] is None and r["navigation_stale"] is False
    assert r["save_refused"] == {"reason": "citation_visibility_changed", "count": count}
    assert any(f"query not saved: {count} cited document(s) no longer eligible" in n
               for n in r["notes"])
    assert _queries(root) == [] and _log(root) == before_log
    assert revision.current(root) == before_rev
    assert r["answer"] and r["citations"]                             # the answer is still returned


def test_a_hide_after_the_call_refuses_the_whole_save(vault, monkeypatch):
    _use(monkeypatch, _Client(during=_hide(vault)))                   # passes Q3, then hidden
    before_rev, before_log = revision.current(vault), _log(vault)
    r = _ask(save=True)
    assert _cited(r) == {VIS, HID}                                    # mixed: never trimmed
    _assert_refused(vault, r, 1, before_rev, before_log)


def test_a_manifest_damaged_after_the_call_refuses_the_save(vault, monkeypatch):
    _use(monkeypatch, _Client(during=_damage(vault)))
    before_rev, before_log = revision.current(vault), _log(vault)
    _assert_refused(vault, _ask(save=True), 1, before_rev, before_log)


def test_every_ineligible_citation_is_counted(vault, monkeypatch):
    _use(monkeypatch, _Client(during=lambda: (_hide(vault)(), _hide(vault, VIS, "archived")())))
    before_rev, before_log = revision.current(vault), _log(vault)
    _assert_refused(vault, _ask(save=True), 2, before_rev, before_log)


def test_current_eligibility_not_an_unchanged_status_decides(vault, monkeypatch):
    # active -> deprecated_candidate during the call: changed, yet still eligible by default.
    _use(monkeypatch, _Client(during=_hide(vault, status="deprecated_candidate")))
    r = _ask(save=True)
    assert r["save_refused"] is None and r["query_id"] and r["navigation_stale"] is True


def test_a_clean_save_writes_page_and_log_and_advances_the_revision_once(vault, monkeypatch):
    _use(monkeypatch, _Client())
    before = revision.current(vault)
    r = _ask(save=True)
    assert r["save_refused"] is None and r["query_id"]
    assert [p.stem for p in _queries(vault)] == [r["query_id"]]
    assert f"query saved: Queries/{r['query_id']}.md" in _log(vault)
    after = revision.current(vault)
    assert after["epoch"] == before["epoch"] and after["counter"] == before["counter"] + 1


def test_a_save_before_a_hide_stays_saved(vault, monkeypatch):
    # The documented limitation: 7a doesn't retract Query pages saved before a hide.
    _use(monkeypatch, _Client())
    r = _ask(save=True)
    assert r["query_id"]
    manifests.set_status(vault / "raw" / "manifests", HID, "hidden")
    assert [p.stem for p in _queries(vault)] == [r["query_id"]]
    assert HID in _queries(vault)[0].read_text()


def test_the_save_check_runs_under_the_write_lock(vault, monkeypatch):
    _use(monkeypatch, _Client())
    held = []
    real = search.visibility_check

    def check(*a, **kw):
        inner = real(*a, **kw)

        def wrapped(ids):
            held.append(write_lock.held())
            return inner(ids)
        return wrapped
    monkeypatch.setattr(search, "visibility_check", check)
    _ask(save=True)
    assert held == [False, True]                                      # Q3 outside, Q4 under it


@pytest.mark.parametrize("when", ["before", "during"])
def test_an_unreadable_queue_refuses_only_the_save_and_keeps_the_answer(vault, monkeypatch, when):
    # The hold stays authority-checked (never authority=False), but the refusal is caught: HTTP 200
    # with the paid answer, nothing written, the damaged queue left exactly as found.
    queue = publication_queue.queue_path(vault)
    queue.parent.mkdir(parents=True, exist_ok=True)
    damage = lambda: queue.write_text("{damaged", encoding="utf-8")  # noqa: E731
    if when == "before":
        damage()
    _use(monkeypatch, _Client(during=damage if when == "during" else None))
    before_rev, before_log = revision.current(vault), _log(vault)
    resp = TestClient(main_module.app).post(
        "/query", json={"question": Q, "mode": "keyword", "save": True})
    assert resp.status_code == 200
    r = resp.json()
    assert r["answer"] and r["citations"] and not r["abstained"]
    assert r["query_id"] is None and r["navigation_stale"] is False
    assert r["save_refused"] == {"reason": "publication_queue_unavailable"}
    assert any("query not saved: the publication queue is unreadable" in n
               and "republish.py --repair" in n for n in r["notes"])
    assert not any("cited document(s) no longer eligible" in n for n in r["notes"])
    assert _queries(vault) == [] and _log(vault) == before_log
    assert revision.current(vault) == before_rev
    assert queue.read_text(encoding="utf-8") == "{damaged"                # preserved, not reset
    assert publication_queue.status(vault)["state"] == "corrupt"


def test_no_save_requested_writes_nothing(vault, monkeypatch):
    _use(monkeypatch, _Client(during=_hide(vault)))
    r = _ask()
    assert r["save_refused"] is None and r["query_id"] is None and _queries(vault) == []


def test_removing_the_first_item_renumbers_the_pack_and_its_citation_lookup(vault):
    client = _Client()
    md = vault / "normalized" / "markdown"
    hits = [{"source_id": sid, "char_start": 0, "char_end": 20, "chunk_id": f"{sid}::0000"}
            for sid in (HID, VIS)]                                   # HID would be e1
    ans = query.answer_query(question=Q, evidence_hits=hits, client=client, model_ref="m",
                             markdown_dir=md, visible=lambda ids: ids - {HID})
    assert client.packs[-1] == [{"evidence_id": "e1", "source_id": VIS,
                                 "quote": (md / f"{VIS}.md").read_text()[:20]}]
    assert [c["source_id"] for c in ans.citations] == [VIS] and ans.visibility_removed == 1


def test_the_save_check_uses_the_requests_explicit_statuses(vault, monkeypatch):
    _use(monkeypatch, _Client(during=_hide(vault)))                   # hidden is in the set
    r = _ask(save=True, source_status="active,hidden")
    assert r["save_refused"] is None and r["query_id"]
    assert [p.stem for p in _queries(vault)] == [r["query_id"]]
