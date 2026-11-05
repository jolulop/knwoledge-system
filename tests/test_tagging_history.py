"""ADR-0072 B2c — tagging progress: the per-ledger marker, the vault tracking record and its migration,
undecided rows and Next navigation (ADR-0070 §6, ADR-0072 decisions 4–5)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import manifests, review_html, review_read, write_lock  # noqa: E402
from app.workers import items, tag_history, tag_links  # noqa: E402
from tests.test_tag_links import _build, _call, _extract, _item, _rewrite_md, _sids  # noqa: E402

NOW = "2026-11-05T10:00:00+00:00"


def _flow(root):
    data = review_read.source_review_index(
        root / "reviews", graph_db=root / "db" / "graph.sqlite", wiki_dir=root / "wiki",
        manifests_dir=root / "raw" / "manifests", enrichment_dir=root / "normalized" / "enrichment")
    return {s["source_id"]: s for s in data["sources"]}, data["totals"]


def _state(root, sid):
    return _flow(root)[0][sid]["tagging"]["state"]


def _ledger_file(root, sid):
    return root / "reviews" / "tag_links" / f"{sid}.json"


def _ledger_tagging(root, sid):
    return json.loads(_ledger_file(root, sid).read_text(encoding="utf-8")).get("tagging")


def _init_fresh(root):
    with write_lock.hold(root / "reviews"):
        return tag_history.init_fresh(root / "reviews", NOW)


def _keep(root, sid, name):
    return _call(root, tag_links.keep, source_id=sid, node_id=items.node_id(name))


def _drop(root, sid, name):
    return _call(root, tag_links.drop, source_id=sid, node_id=items.node_id(name))


def _block(root, name, sid=None):
    return _call(root, tag_links.block, name=name, policies_dir=root / "policies",
                 **({"source_id": sid} if sid else {}))


@pytest.fixture
def fresh(tmp_path):
    """A fresh vault certified explicitly (from_start: true) before any decision: a, b, c."""
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body", "c.md": "gamma body"})
    assert _init_fresh(tmp_path)["outcome"] == "initialized"
    _extract(tmp_path, {"alpha": [_item("Alpha Tag"), _item("Shared")],
                        "beta": [_item("Beta Tag")], "gamma": [_item("Gamma Tag")]})
    s = _sids(tmp_path)
    return tmp_path, s["a.md"], s["b.md"], s["c.md"]


# --- the marker ---------------------------------------------------------------------------------


def test_documents_start_not_yet_tagged_and_a_keep_records_the_first_decision(fresh):
    root, a, b, c = fresh
    assert {_state(root, x) for x in (a, b, c)} == {"never_tagged"}
    assert _keep(root, a, "Alpha Tag")["outcome"] == "kept"
    assert _state(root, a) == "tagged" and _state(root, b) == "never_tagged"
    t = _ledger_tagging(root, a)
    assert t["state"] == "tagged" and t["first_decision_at"] == t["earliest_known_decision_at"]


def test_all_drops_count_and_undoing_the_only_drop_keeps_tagged(fresh):
    root, a, b, c = fresh
    assert _drop(root, b, "Beta Tag")["outcome"] == "dropped"
    assert _state(root, b) == "tagged"
    key = tag_links.load_ledger(root / "reviews", b)["drops"][0]["key"]
    assert _call(root, tag_links.undo_drop, source_id=b, key=key)["outcome"] == "undone"
    assert _state(root, b) == "tagged"                       # never cleared by an undo
    assert _ledger_tagging(root, b)["state"] == "tagged"


def test_never_suggest_on_the_document_counts_but_a_vault_wide_block_does_not(fresh):
    root, a, b, c = fresh
    assert _block(root, "Gamma Tag", sid=c)["outcome"] == "blocked_tag"
    assert _state(root, c) == "tagged" and _ledger_tagging(root, c)["state"] == "tagged"
    assert _block(root, "Beta Tag")["outcome"] == "blocked_tag"           # not from a document
    assert _state(root, b) == "never_tagged"


def test_a_refused_action_sets_nothing(fresh):
    root, a, b, c = fresh
    out = _call(root, tag_links.keep, source_id=a, node_id="itm_" + "0" * 16)
    assert out["outcome"] == "blocked"
    assert not _ledger_file(root, a).exists() and _state(root, a) == "never_tagged"


def test_a_details_only_save_does_not_count(fresh):
    root, a, b, c = fresh
    manifests.confirm_details(root / "raw" / "manifests", a, title="A Title", authors=[],
                              publisher=None, baseline="b" * 16, now=NOW)
    assert _state(root, a) == "never_tagged"


def test_the_marker_survives_backup_and_restore(fresh, tmp_path_factory):
    root, a, b, c = fresh
    _keep(root, a, "Alpha Tag")
    before = _ledger_tagging(root, a)
    restored = tmp_path_factory.mktemp("restored")
    shutil.copytree(root, restored, dirs_exist_ok=True)       # reviews/ travels whole
    assert _ledger_tagging(restored, a) == before
    assert _state(restored, a) == "tagged" and _state(restored, b) == "never_tagged"


def test_a_malformed_marker_is_a_damaged_ledger(fresh):
    root, a, b, c = fresh
    _keep(root, a, "Alpha Tag")
    data = json.loads(_ledger_file(root, a).read_text(encoding="utf-8"))
    _ledger_file(root, a).write_text(json.dumps({**data, "tagging": {"state": "maybe"}}),
                                     encoding="utf-8")
    with pytest.raises(tag_links.LedgerCorrupt):
        tag_links.load_ledger(root / "reviews", a)


# --- tracking completeness and unknown history -------------------------------------------------


def test_without_a_tracking_record_untouched_documents_are_unknown_not_untagged(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    _extract(tmp_path, {"alpha": [_item("Alpha Tag")]})
    a = _sids(tmp_path)["a.md"]
    assert _state(tmp_path, a) == "history_unknown"          # a code clone alone proves nothing
    assert _keep(tmp_path, a, "Alpha Tag")["outcome"] == "kept"
    t = _ledger_tagging(tmp_path, a)
    assert t["state"] == "tagged" and t["first_decision_at"] is None     # tagged now, not "first"
    assert t["earliest_known_decision_at"] is not None


def test_fresh_initialization_is_explicit_and_refused_once_history_exists(fresh):
    root, a, b, c = fresh
    assert _init_fresh(root) == {"outcome": "refused", "reason": "tracking_record_exists"}
    tag_history.tracking_path(root / "reviews").unlink()
    _keep(root, a, "Alpha Tag")
    assert _init_fresh(root)["outcome"] == "refused"            # ledgers / tag audit exist


def test_attributable_audit_evidence_without_a_ledger_means_tagged(fresh):
    root, a, b, c = fresh
    _keep(root, a, "Alpha Tag")
    _ledger_file(root, a).unlink()                              # an incomplete restore
    assert _state(root, a) == "tagged"


# --- the repair migration -----------------------------------------------------------------------


def _strip_marker(root, sid):
    data = json.loads(_ledger_file(root, sid).read_text(encoding="utf-8"))
    data.pop("tagging", None)
    _ledger_file(root, sid).write_text(json.dumps(data), encoding="utf-8")


def _migrate(root, apply):
    if apply:                     # `since` = the migration's real time, after every discovery here
        with write_lock.hold(root / "reviews"):
            return tag_links.migrate_tagging(root / "reviews", apply=True)
    return tag_links.migrate_tagging(root / "reviews", apply=False)


def _old_vault(tmp_path):
    """Decisions recorded before the marker existed, and no tracking record."""
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body", "c.md": "gamma body"})
    _extract(tmp_path, {"alpha": [_item("Alpha Tag")], "beta": [_item("Beta Tag")],
                        "gamma": [_item("Gamma Tag")]})
    s = _sids(tmp_path)
    _keep(tmp_path, s["a.md"], "Alpha Tag")
    _drop(tmp_path, s["b.md"], "Beta Tag")
    for x in (s["a.md"], s["b.md"]):
        _strip_marker(tmp_path, x)
    return tmp_path, s["a.md"], s["b.md"], s["c.md"]


def test_the_migration_dry_run_writes_nothing(tmp_path):
    root, a, b, c = _old_vault(tmp_path)
    snapshot = {p.name: p.read_bytes() for p in (root / "reviews" / "tag_links").glob("*.json")}
    out = _migrate(root, apply=False)
    assert {x["source_id"] for x in out["backfilled"]} == {a, b} and out["tracking_written"]
    assert {p.name: p.read_bytes() for p in (root / "reviews" / "tag_links").glob("*.json")} == snapshot
    assert not tag_history.tracking_path(root / "reviews").exists()


def test_the_migration_backfills_from_evidence_and_repeating_it_changes_nothing(tmp_path):
    root, a, b, c = _old_vault(tmp_path)
    out = _migrate(root, apply=True)
    assert len(out["backfilled"]) == 2
    for x in (a, b):
        t = _ledger_tagging(root, x)
        # from_start: false and the documents predate `since`: tagged, first time unknown
        assert t["state"] == "tagged" and t["first_decision_at"] is None
        assert t["earliest_known_decision_at"] is not None
    assert tag_history.load_tracking(root / "reviews")["from_start"] is False
    assert _state(root, c) == "history_unknown"               # no ledger, no evidence, older doc
    snapshot = {p.name: p.read_bytes() for p in (root / "reviews" / "tag_links").glob("*.json")}
    again = _migrate(root, apply=True)
    assert again["backfilled"] == [] and not again["tracking_written"]
    assert {p.name: p.read_bytes() for p in (root / "reviews" / "tag_links").glob("*.json")} == snapshot


def test_the_migration_never_downgrades_an_existing_record(fresh):
    root, a, b, c = fresh
    before = tag_history.tracking_path(root / "reviews").read_bytes()
    _keep(root, a, "Alpha Tag")
    _strip_marker(root, a)
    _migrate(root, apply=True)
    assert tag_history.tracking_path(root / "reviews").read_bytes() == before   # still from_start
    t = _ledger_tagging(root, a)
    assert t["first_decision_at"] == t["earliest_known_decision_at"]   # complete: the first time


def _untime_ledgers(root, *sids):
    for x in sids:
        data = json.loads(_ledger_file(root, x).read_text(encoding="utf-8"))
        for v in data["kept"].values():
            v["at"] = "not a time"
        for d in data["drops"]:
            d["at"] = "not a time"
        _ledger_file(root, x).write_text(json.dumps(data), encoding="utf-8")


def _untime_audit(root):
    for path in (root / "reviews" / "audit_log").glob("*-tag-*.json"):
        rec = json.loads(path.read_text(encoding="utf-8"))
        rec.pop("at", None)
        path.write_text(json.dumps(rec), encoding="utf-8")


def test_untimed_keeps_and_drops_are_tagged_with_unknown_times(tmp_path):
    """B2c review round 1 (ADR-0070 §6 "decisions but no timestamp"): evidence without a usable
    time still means tagged; it gives no time at all."""
    root, a, b, c = _old_vault(tmp_path)
    _untime_ledgers(root, a, b)
    shutil.rmtree(root / "reviews" / "audit_log")
    out = {x["source_id"]: x["tagging"] for x in _migrate(root, apply=True)["backfilled"]}
    for x in (a, b):
        assert out[x] == {"state": "tagged", "first_decision_at": None,
                          "earliest_known_decision_at": None}
        assert _state(root, x) == "tagged"
    _keep(root, a, "Alpha Tag")              # the action is dated evidence; still no first time
    t = _ledger_tagging(root, a)
    assert t["state"] == "tagged" and t["first_decision_at"] is None
    assert t["earliest_known_decision_at"] is not None


def test_an_untimed_audit_event_without_a_ledger_is_never_not_yet_tagged(fresh):
    root, a, b, c = fresh                                    # tracking complete (from_start)
    _keep(root, a, "Alpha Tag")
    _ledger_file(root, a).unlink()
    _untime_audit(root)
    flow = _flow(root)[0][a]["tagging"]
    assert flow == {"state": "tagged", "first_decision_at": None,
                    "earliest_known_decision_at": None}


def test_mixed_evidence_keeps_the_known_time_without_inventing_a_first_one(fresh):
    root, a, b, c = fresh
    _keep(root, a, "Alpha Tag")
    _drop(root, a, "Shared")
    _strip_marker(root, a)
    data = json.loads(_ledger_file(root, a).read_text(encoding="utf-8"))
    known = data["drops"][0]["at"]
    for v in data["kept"].values():
        v["at"] = "not a time"                               # the keep is now untimed
    _ledger_file(root, a).write_text(json.dumps(data), encoding="utf-8")
    shutil.rmtree(root / "reviews" / "audit_log")
    flow = _flow(root)[0][a]["tagging"]
    assert flow["state"] == "tagged" and flow["first_decision_at"] is None
    assert flow["earliest_known_decision_at"] == known


def test_timed_evidence_under_complete_tracking_gives_the_first_time(fresh):
    root, a, b, c = fresh
    _keep(root, a, "Alpha Tag")
    first = _ledger_tagging(root, a)["first_decision_at"]
    _strip_marker(root, a)
    flow = _flow(root)[0][a]["tagging"]
    assert flow["first_decision_at"] is not None and flow["first_decision_at"] <= first


def test_the_untagged_link_says_none_known_when_histories_are_unknown(tmp_path):
    _build(tmp_path, {"a.md": "alpha body"})
    _extract(tmp_path, {"alpha": [_item("Alpha Tag")]})
    data = review_read.source_review_index(
        tmp_path / "reviews", graph_db=tmp_path / "db" / "graph.sqlite", wiki_dir=tmp_path / "wiki",
        manifests_dir=tmp_path / "raw" / "manifests",
        enrichment_dir=tmp_path / "normalized" / "enrichment")
    html = review_html.render_sources_index(data)
    assert "none known to be untagged" in html and "1 with tagging history unknown" in html


def test_a_decision_on_an_unmigrated_ledger_backfills_in_the_same_commit(tmp_path):
    root, a, b, c = _old_vault(tmp_path)
    earliest_before = min(v["at"] for v in tag_links.load_ledger(root / "reviews", a)["kept"].values())
    _drop(root, a, "Alpha Tag")
    t = _ledger_tagging(root, a)
    assert t["state"] == "tagged" and t["earliest_known_decision_at"] <= earliest_before


def test_validation_reports_unmarked_ledgers_as_advisory_and_a_bad_record_as_an_error(tmp_path):
    root, a, b, c = _old_vault(tmp_path)
    run = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_tag_links.py"), str(root)],
                         capture_output=True, text=True)
    assert run.returncode == 0 and "no tagging-history marker" in run.stdout
    tag_history.tracking_path(root / "reviews").write_text("{bad", encoding="utf-8")
    run = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_tag_links.py"), str(root)],
                         capture_output=True, text=True)
    assert run.returncode == 1 and "_tracking.json is malformed" in run.stdout
    assert _state(root, c) == "history_unknown"               # an unreadable record: less certainty


# --- undecided rows and Next navigation ---------------------------------------------------------


def _pending_retype(root, sid, rid, *, status="pending", name="Alpha Tag"):
    d = root / "reviews" / ("deferred" if status == "deferred" else "pending")
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}-{status}.json").write_text(json.dumps({
        "review_id": rid, "type": "change_item_type", "status": status, "created_at": NOW,
        "subject": {"node_id": items.node_id(name), "to_item_type": "domain"},
        "context": {"source_id": sid, "from_item_type": "method_technique", "name": name}}),
        encoding="utf-8")


def test_undecided_rows_split_by_kind_count_each_review_once_and_skip_deferred(fresh):
    root, a, b, c = fresh
    _pending_retype(root, a, "rev_t1")
    (root / "reviews" / "pending" / "rev_t1-copy.json").write_text(       # a second association
        (root / "reviews" / "pending" / "rev_t1-pending.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    _pending_retype(root, a, "rev_t2", status="deferred")
    u = _flow(root)[0][a]["undecided"]
    assert u == {"tags": 2, "type_changes": 1, "retirements": 0, "total": 3}
    assert review_html.undecided_text(u) == "2 tags, 1 type change still undecided"


def test_new_suggestions_on_a_reviewed_document_are_undecided_again(fresh):
    root, a, b, c = fresh
    _keep(root, a, "Alpha Tag")
    _drop(root, a, "Shared")
    assert _flow(root)[0][a]["undecided"]["total"] == 0
    _rewrite_md(root, a, "alpha body, revised")              # a new request (no cache replay)
    _extract(root, {"alpha": [_item("Alpha Tag"), _item("Fresh Idea")]}, force=True, source_ids=[a])
    flow = _flow(root)[0][a]
    assert flow["undecided"]["tags"] == 1 and flow["tagging"]["state"] == "tagged"


def test_next_navigation_moves_forward_wraps_once_and_reports_none_remaining(fresh):
    root, a, b, c = fresh
    order = list(_flow(root)[0])
    sources = list(_flow(root)[0].values())
    assert review_read.next_source(sources, order[0], review_read._has_undecided) == order[1]
    assert review_read.next_source(sources, order[2], review_read._has_undecided) == order[0]
    for sid, name in ((a, "Alpha Tag"), (a, "Shared"), (b, "Beta Tag"), (c, "Gamma Tag")):
        _keep(root, sid, name)
    sources = list(_flow(root)[0].values())
    assert review_read.next_source(sources, order[0], review_read._has_undecided) is None
    assert review_read.next_source(sources, None, review_read._untagged) is None
    _, totals = _flow(root)
    assert totals["not_yet_tagged"] == 0 and totals["first_untagged_source"] is None


def test_the_save_result_warns_without_blocking_and_offers_the_next_document(fresh):
    root, a, b, c = fresh
    _keep(root, a, "Alpha Tag")                               # a partial save: Shared untouched
    view = review_read.source_review_view(
        root / "reviews", a, graph_db=root / "db" / "graph.sqlite", wiki_dir=root / "wiki",
        manifests_dir=root / "raw" / "manifests", enrichment_dir=root / "normalized" / "enrichment")
    html = review_html.render_source_decide_result(a, [{"action": "keep", "recorded": True}],
                                                   progress=view)
    assert "1 row on this document is still undecided (1 tag still undecided)" in html
    assert f"/ui/reviews/sources/{view['next_source']}'>Next document with undecided rows" in html
    screen = review_html.render_source_screen(view)
    assert "1 tag still undecided" in screen and "Next untagged document" in screen


def _script(*args):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "rebuild_tag_links.py"), *args],
                          capture_output=True, text=True)


def test_the_repair_script_runs_the_migration_and_the_explicit_initialization(tmp_path,
                                                                              tmp_path_factory):
    root, a, b, c = _old_vault(tmp_path)
    dry = _script(str(root))
    assert "ledgers to backfill: 2" in dry.stdout and "would be written" in dry.stdout
    assert not tag_history.tracking_path(root / "reviews").exists()
    applied = _script(str(root), "--apply")
    assert "ledgers backfilled: 2" in applied.stdout
    assert tag_history.load_tracking(root / "reviews")["from_start"] is False
    assert _script(str(root), "--init-fresh-tracking").returncode == 1        # history exists
    new = tmp_path_factory.mktemp("new")
    _build(new, {"n.md": "new body"})
    out = _script(str(new), "--init-fresh-tracking")
    assert out.returncode == 0 and tag_history.load_tracking(new / "reviews")["from_start"] is True
