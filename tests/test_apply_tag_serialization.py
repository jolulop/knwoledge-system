"""ADR-0066 review round 10: Apply and tagging may overlap (two tabs, a script), so every item-page
writer is serialized with tag actions by the ledger lock — from reading the page to writing it.

1. A retype Apply and an alias edit at the same moment: the edit waits, and both changes survive.
2. Two processes editing the same tag's aliases from the same displayed list: exactly one wins; the
   other is refused (the cross-process `flock` makes the compare-and-set atomic).
3. Apply takes the ledger lock before its first graph write (the same order as every tag action).
"""
from __future__ import annotations

import json
import multiprocessing
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.backend import graph
from app.backend import main as main_module
from app.backend.config import get_settings
from app.workers import retypes, tag_links
from tests import fixtures
from tests.test_tag_suggestions import _page_fm
from tests.test_uat_round8 import _retype, _two_docs


def _graph_conn(tmp_path):
    return graph.connect(tmp_path / "db" / "graph.sqlite")


def _edit(tmp_path, source_id, nid, aliases, expected=None):
    gconn = _graph_conn(tmp_path)
    try:
        out = tag_links.edit_aliases(gconn, source_id=source_id, node_id=nid, aliases=aliases,
                                     wiki_dir=tmp_path / "wiki", reviews_dir=tmp_path / "reviews",
                                     expected=expected)
        gconn.commit()
        return out
    finally:
        gconn.close()


def test_an_alias_edit_during_a_retype_apply_waits_and_both_survive(tmp_path, monkeypatch):
    a, _b, nid = _two_docs(tmp_path)              # LLM kept on a: aliases [Large Language Model, Foundation Model]
    _retype(tmp_path, "rev_2000000000000001", nid, "domain")
    edit: dict = {}
    real_check = retypes.retype_check

    def check_then_race(gconn, item, *, wiki_dir):
        # The retype has read the page; an alias edit now arrives from another thread (another
        # request). Under the lock it must wait until the retype has written the page.
        state = real_check(gconn, item, wiki_dir=wiki_dir)
        worker = threading.Thread(target=lambda: edit.update(
            _edit(tmp_path, a, nid, ["Large Language Model"])))
        worker.start()
        worker.join(timeout=0.5)
        edit["waited"] = worker.is_alive()
        edit["worker"] = worker
        return state

    monkeypatch.setattr(retypes, "retype_check", check_then_race)
    gconn = _graph_conn(tmp_path)
    try:
        out = retypes.apply_retypes(gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")
        gconn.commit()
    finally:
        gconn.close()
    edit["worker"].join(timeout=10)
    assert out["applied"] == 1
    assert edit["waited"] is True                  # the edit waited for the retype to finish
    assert edit["outcome"] == "aliases_updated"
    fm = _page_fm(tmp_path, "llm")
    assert fm["item_type"] == "domain"             # the retype survived the edit …
    assert fm["aliases"] == ["Large Language Model"]   # … and the edit survived the retype


def _compete(root, source_id, nid, aliases, expected, barrier, results):
    barrier.wait()
    out = _edit(Path(root), source_id, nid, aliases, expected)
    results.put((aliases[0], out["outcome"], out.get("reason")))


def test_two_processes_editing_the_same_aliases_one_wins(tmp_path):
    a, _b, nid = _two_docs(tmp_path)
    shown = ["Large Language Model", "Foundation Model"]
    ctx = multiprocessing.get_context("fork")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    procs = [ctx.Process(target=_compete, args=(str(tmp_path), a, nid, [new], shown, barrier, results))
             for new in ("LLMs", "Foundation Models")]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=30)
    outcomes = sorted(results.get(timeout=5)[1:] for _ in procs)
    assert outcomes == [("aliases_updated", None), ("blocked", "aliases_changed_since_shown")]
    winners = _page_fm(tmp_path, "llm")["aliases"]
    assert winners in (["LLMs"], ["Foundation Models"])
    edits = [json.loads(p.read_text()) for p in (tmp_path / "reviews" / "audit_log").glob("*.json")]
    assert [e["to"] for e in edits if e.get("event") == "tag-aliases-edited"] == [winners]


def test_apply_takes_the_ledger_lock_before_its_first_graph_write(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    settings.manifests_dir.mkdir(parents=True, exist_ok=True)
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    graph.init_db(gdb)
    monkeypatch.setattr(main_module, "settings", settings)
    seen = []
    real = main_module.synthesis.apply_resolved_syntheses

    def first_executor(*args, **kwargs):               # the first graph executor Apply runs
        seen.append(getattr(tag_links._held, "depth", 0))
        return real(*args, **kwargs)

    monkeypatch.setattr(main_module.synthesis, "apply_resolved_syntheses", first_executor)
    fixtures.apply(TestClient(main_module.app))
    assert seen and seen[0] >= 1
