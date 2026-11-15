"""ADR-0074 decision 4, slice 3c: the read discipline for policies, journals and the `db/*.json`
authority records.

Every family reads through `authority_read` — non-blocking (a FIFO can't hang a reader holding the
write lock), never through a symbolic link, and only a **verified absence** is "nothing here". A
damaged policy refuses the operation that depends on it (never defaults); a damaged journal is an
unknown operation (never "nothing in flight"); a directory that can't be listed is never an empty
inventory; and the written recovery contracts (revision, Apply authorizations, contradiction
estimates, tag labels, tagging-history provenance) keep their behaviour but never replace an entry
they can't read."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.backend import (apply_authorization, durable_write, policy, producer_journal,
                         publication_queue, revision)
from app.backend import main as main_module
from app.backend.config import get_settings
from app.workers import contradiction_check, identity_journal, lint, retention, tag_history
from app.workers import tag_links, tag_projection

ROOT = Path(__file__).resolve().parents[1]
SID = "src_0000000000000c3c"
NODE = "itm_00000000000000c3"
NOW = "2026-11-14T00:00:00Z"


def _dangling(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(path.parent / "nowhere", path)


def _linked(path: Path, body: bytes) -> Path:
    """`path` as a symbolic link to a perfectly valid file outside the vault directory."""
    target = path.parent.parent / f"outside-{path.name}"
    target.write_bytes(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, path)
    return target


# --- policies ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("damage, state, error", [
    ("dangling", "unreadable", "dangling_symlink"),
    ("symlink", "unreadable", "symlink"),
    ("directory", "unreadable", "not_a_regular_file"),
    ("utf8", "corrupt", "invalid_utf8"),
    ("sequence", "corrupt", "not_a_mapping"),
])
def test_a_damaged_policy_is_refused_never_defaults(tmp_path, damage, state, error):
    path = tmp_path / "policies" / "retrieval.yaml"
    path.parent.mkdir()
    if damage == "dangling":
        _dangling(path)
    elif damage == "symlink":
        _linked(path, b"budgets:\n  final_context_chunks: 3\n")
    elif damage == "directory":
        path.mkdir()
    elif damage == "utf8":
        path.write_bytes(b"budgets:\n  final_context_chunks: \xff\n")
    else:
        path.write_text("- a\n- b\n")
    with pytest.raises(policy.PolicyUnavailable) as exc:
        policy.load_retrieval_policy(path)
    assert (exc.value.state, exc.value.error) == (state, error)
    assert "budgets" not in str(exc.value)                 # categorical, never the contents


def test_an_absent_policy_means_defaults_and_an_empty_one_is_an_empty_mapping(tmp_path):
    assert policy.read_policy(tmp_path / "retrieval.yaml") is None
    assert policy.load_retrieval_policy(tmp_path / "retrieval.yaml").cap("rrf_k") == policy._DEFAULT_CAPS["rrf_k"]
    (tmp_path / "review.yaml").write_text("# nothing tuned yet\n")
    assert policy.read_policy(tmp_path / "review.yaml") == {}


# Review round 1: the strict YAML subset and per-family field validation. A present value that is
# malformed, of the wrong type or out of range refuses; it never silently selects a default.


@pytest.mark.parametrize("text", [
    "[\n",                                          # was {"[": None} → every default
    "caps:\n  max_evidence_hits: [\n",             # was the string "[" → the cap discarded
    "caps:\n    max_graph_nodes: 7\n  max_graph_edges: 9\n",   # dedent to no open block: dropped
    "caps:\n  max_graph_nodes: 7\n    max_graph_edges: 9\n",   # deeper than its block: dropped
    "caps:\n  max_graph_nodes: 7\n  max_graph_nodes: 9\n",     # duplicate key
    "caps:\n\tmax_graph_nodes: 7\n",               # tab indentation
    "weights:\n  item_type_boost: '0.001\n",       # unterminated quote
    "caps: {max_graph_nodes: 7}\n",                 # flow mapping
    "caps:\n  max_graph_nodes: 7\n- stray\n",      # a sequence item inside a mapping
])
def test_text_outside_the_policy_yaml_subset_is_corrupt(tmp_path, text):
    path = tmp_path / "retrieval.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(policy.PolicyUnavailable) as exc:
        policy.load_retrieval_policy(path)
    assert (exc.value.state, exc.value.error) == ("corrupt", "invalid_yaml")


@pytest.mark.parametrize("text, field", [
    ("raw_files: yes\n", "raw_files"),
    ("raw_files:\n  older_than_years_archive_candidate: true\n",
     "raw_files.older_than_years_archive_candidate"),
    ("raw_files:\n  older_than_years_archive_candidate: -1\n",
     "raw_files.older_than_years_archive_candidate"),
    ("ephemeral:\n  enabled: 1\n", "ephemeral.enabled"),
    ("ephemeral:\n  delete_candidate_after_days: 1.5\n", "ephemeral.delete_candidate_after_days"),
    ("response_cache:\n  enabled: \"no\"\n", "response_cache.enabled"),
    ("response_cache:\n  cache_ttl_days: false\n", "response_cache.cache_ttl_days"),
    ("response_cache:\n  cache_max_mb: lots\n", "response_cache.cache_max_mb"),
])
def test_retention_refuses_a_present_value_of_the_wrong_type_before_any_job(tmp_path, text, field):
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "retention.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(policy.PolicyUnavailable) as exc:
        retention.run_stale_check(tmp_path, now=NOW)
    assert exc.value.error == f"invalid_field:{field}"
    assert not (tmp_path / "db" / "jobs.sqlite").exists()
    assert not (tmp_path / "reviews" / "pending").exists()


def test_a_flow_sequence_in_retention_is_refused_not_a_traceback(tmp_path):
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "retention.yaml").write_text("raw_files: []\n", encoding="utf-8")
    with pytest.raises(policy.PolicyUnavailable) as exc:     # was AttributeError mid-check
        retention.run_stale_check(tmp_path, now=NOW)
    assert exc.value.error == "invalid_yaml"
    assert not (tmp_path / "db" / "jobs.sqlite").exists()


def test_the_shipped_policies_pass_their_validators_and_missing_fields_keep_defaults(tmp_path):
    for name, check in [("retrieval", policy.check_retrieval), ("retention", policy.check_retention),
                        ("review", policy.check_review), ("citation", policy.check_citation)]:
        assert policy.read_policy(ROOT / "policies" / f"{name}.yaml", check)
    path = tmp_path / "retention.yaml"
    path.write_text("ephemeral:\n  enabled: true\nfuture_section:\n  anything: [x]\n",
                    encoding="utf-8")
    with pytest.raises(policy.PolicyUnavailable):            # still outside the subset
        policy.read_policy(path, policy.check_retention)
    path.write_text("ephemeral:\n  enabled: true\nfuture_section:\n  anything: x\n",
                    encoding="utf-8")
    assert policy.read_policy(path, policy.check_retention)["future_section"] == {"anything": "x"}


def test_an_empty_abstention_text_is_refused_not_defaulted(tmp_path):
    path = tmp_path / "citation.yaml"
    path.write_text('when_no_source_found: ""\n', encoding="utf-8")
    with pytest.raises(policy.PolicyUnavailable) as exc:
        policy.read_policy(path, policy.check_citation)
    assert exc.value.error == "invalid_field:when_no_source_found"


def test_search_refuses_with_409_on_a_damaged_retrieval_policy(tmp_path, monkeypatch):
    st = get_settings(tmp_path)
    _dangling(st.retrieval_policy_path)
    monkeypatch.setattr(main_module, "settings", st)
    res = TestClient(main_module.app).get("/search", params={"q": "anything"})
    assert res.status_code == 409
    assert res.json()["detail"]["reason"] == "policy_unreadable"
    assert res.json()["detail"]["policy"] == "retrieval.yaml"


def test_retention_refuses_before_recording_a_job_on_a_damaged_policy(tmp_path):
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "retention.yaml").write_bytes(b"raw_files: \xff\n")
    with pytest.raises(policy.PolicyUnavailable):
        retention.run_stale_check(tmp_path, now=NOW)
    assert not (tmp_path / "db" / "jobs.sqlite").exists()
    assert not (tmp_path / "reviews" / "pending").exists()


def test_lint_reports_a_damaged_review_policy_and_computes_no_merge_proposals(tmp_path):
    (tmp_path / "policies").mkdir()
    _dangling(tmp_path / "policies" / "review.yaml")
    findings = lint._check_merge_candidates(None, tmp_path / "wiki", tmp_path / "reviews",
                                            policy_path=tmp_path / "policies" / "review.yaml",
                                            file_items=True, now=NOW, filed=[], existing=[])
    assert [(f["check"], f["severity"], f["subject"]) for f in findings] == [
        ("policy_unreadable", "high", "policies/review.yaml")]


def test_a_damaged_blocked_tag_list_is_never_read_as_empty_nor_rewritten(tmp_path):
    pdir = tmp_path / "policies"
    path = tag_links.blocklist_path(pdir)
    target = _linked(path, b"Alpha\nBeta\n")
    with pytest.raises(policy.PolicyUnavailable):
        tag_links.load_blocked(pdir)
    with pytest.raises(policy.PolicyUnavailable):
        tag_links._add_blocked(pdir, "Gamma")             # the first write of a block
    with pytest.raises(policy.PolicyUnavailable):
        tag_links.unblock(key="alpha", policies_dir=pdir, enrichment_dir=tmp_path / "enrichment",
                          reviews_dir=tmp_path / "reviews")
    assert path.is_symlink() and target.read_bytes() == b"Alpha\nBeta\n"
    path.unlink()
    path.write_bytes(b"Alpha\n\xff\n")                    # invalid UTF-8 is corrupt, never replaced
    with pytest.raises(policy.PolicyUnavailable) as exc:
        tag_links.load_blocked(pdir)
    assert exc.value.state == "corrupt"


def test_a_missing_blocked_tag_list_is_empty_and_a_block_creates_it(tmp_path):
    pdir = tmp_path / "policies"
    assert tag_links.load_blocked(pdir) == {}
    assert tag_links._add_blocked(pdir, "Gamma") is True
    assert tag_links.load_blocked(pdir) == {"gamma": "Gamma"}


# --- the identity journal (reviews/in_flight) -------------------------------------------------------


def test_a_dangling_in_flight_entry_is_an_open_unreadable_operation(tmp_path):
    rdir = tmp_path / "reviews"
    _dangling(rdir / "in_flight" / "rev_x.json")
    with pytest.raises(identity_journal.JournalUnreadable) as exc:
        identity_journal.read(rdir, "rev_x")
    assert (exc.value.state, exc.value.error) == ("unreadable", "dangling_symlink")
    assert identity_journal.is_open(rdir, "rev_x")
    assert identity_journal.open_operations(rdir) == [
        {"review_id": "rev_x", "op": None, "nodes": None}]
    assert identity_journal.operation_for(rdir, [NODE]) == "rev_x"


def test_an_in_flight_directory_that_cant_be_listed_puts_everything_in_flight(tmp_path):
    rdir = tmp_path / "reviews"
    rdir.mkdir()
    (rdir / "in_flight").write_text("not a directory")
    assert identity_journal.open_operations(rdir) == [
        {"review_id": identity_journal.UNLISTED, "op": None, "nodes": None}]
    assert identity_journal.operation_for(rdir, [NODE]) == identity_journal.UNLISTED
    with pytest.raises(ValueError):                       # never a path built from it
        identity_journal.read(rdir, identity_journal.UNLISTED)


def test_a_readable_in_flight_entry_still_reads(tmp_path):
    rdir = tmp_path / "reviews"
    identity_journal.begin(rdir, "rev_y", op="merge", nodes=[NODE], now=NOW)
    assert identity_journal.read(rdir, "rev_y")["nodes"] == [NODE]
    assert identity_journal.open_operations(rdir) == [
        {"review_id": "rev_y", "op": "merge", "nodes": [NODE]}]
    assert identity_journal.operation_for(rdir, ["itm_ffffffffffffffff"]) is None


# --- the producer journal and the publication queue -------------------------------------------------


@pytest.mark.parametrize("module, load, error_type, name", [
    (producer_journal, producer_journal.load, producer_journal.JournalCorrupt, "producer_journal"),
    (publication_queue, publication_queue.load, publication_queue.QueueCorrupt,
     "publication_pending"),
])
@pytest.mark.parametrize("damage", ["dangling", "symlink", "directory"])
def test_a_db_journal_that_cant_be_read_is_never_empty(tmp_path, module, load, error_type, name,
                                                       damage):
    path = tmp_path / "db" / f"{name}.json"
    path.parent.mkdir()
    if damage == "dangling":
        _dangling(path)
    elif damage == "symlink":
        _linked(path, json.dumps({"schema_version": 1, "records": [], "entries": []}).encode())
    else:
        path.mkdir()
    with pytest.raises(error_type) as exc:
        load(tmp_path)
    assert exc.value.reason.startswith("unreadable:")


@pytest.mark.parametrize("module, name", [(producer_journal, "producer_journal"),
                                          (publication_queue, "publication_pending")])
def test_repair_keeps_an_unreadable_journal_entry_as_a_link_to_itself(tmp_path, module, name):
    path = tmp_path / "db" / f"{name}.json"
    target = _linked(path, b"whatever it holds")
    kept = module.preserve_damaged(tmp_path, now=NOW)
    assert kept is not None and kept.name.startswith(f"{name}.damaged-")
    assert kept.is_symlink() and os.readlink(kept) == str(target)   # the entry itself, not a copy
    assert path.is_symlink()                              # still in place until the replacement


def test_preserve_entry_never_replaces_an_existing_target(tmp_path):
    (tmp_path / "a").write_text("a")
    (tmp_path / "b").write_text("b")
    with pytest.raises(FileExistsError):
        durable_write.preserve_entry(tmp_path / "a", tmp_path / "b")
    assert (tmp_path / "b").read_text() == "b"


# --- the explicit recovery contracts (decision 4.5) -------------------------------------------------


def test_revision_refuses_an_entry_it_cant_read_and_never_replaces_it(tmp_path):
    path = revision.revision_path(tmp_path)
    target = _linked(path, json.dumps({"schema_version": 1, "epoch": "a" * 32,
                                       "counter": 4}).encode())
    with pytest.raises(revision.RevisionUnavailable):
        revision.current(tmp_path)
    assert path.is_symlink() and not revision.damaged_copies(tmp_path)
    assert json.loads(target.read_text())["counter"] == 4


def test_revision_still_recovers_malformed_contents_with_a_new_epoch(tmp_path):
    path = revision.revision_path(tmp_path)
    path.parent.mkdir()
    path.write_text("{")
    rev = revision.current(tmp_path)
    assert rev["counter"] == 0 and len(revision.damaged_copies(tmp_path)) == 1


def test_apply_authorizations_refuse_an_entry_they_cant_read(tmp_path):
    path = apply_authorization.store_path(tmp_path)
    _dangling(path)
    with pytest.raises(apply_authorization.StoreUnavailable):
        apply_authorization.load(tmp_path)
    assert path.is_symlink() and not apply_authorization.damaged_copies(tmp_path)


def test_estimates_and_tag_labels_keep_their_unknown_means_conservative_contract(tmp_path):
    _linked(contradiction_check.store_path(tmp_path),
            json.dumps({"schema_version": 1, "records": []}).encode())
    assert contradiction_check._load(tmp_path) == []      # losing an estimate only refuses a run
    _dangling(tag_projection.snapshot_path(tmp_path))
    assert tag_projection.load_published(tmp_path) is None   # unknown → every Source page refreshed


# --- tag ledgers, tagging history, resolving and deletion records -----------------------------------


def test_a_dangling_ledger_is_corrupt_never_empty(tmp_path):
    rdir = tmp_path / "reviews"
    _dangling(tag_links.ledger_path(rdir, SID))
    with pytest.raises(tag_links.LedgerCorrupt) as exc:
        tag_links.load_ledger(rdir, SID)
    assert exc.value.problem == "unreadable (dangling_symlink)"
    assert tag_links.corrupt_ledgers(rdir) == [
        {"source_id": SID, "problem": "unreadable (dangling_symlink)"}]
    assert tag_links.kept_anywhere(rdir) is None


def test_a_ledger_directory_that_cant_be_listed_is_never_an_empty_inventory(tmp_path):
    rdir = tmp_path / "reviews"
    rdir.mkdir()
    (rdir / "tag_links").write_text("not a directory")
    assert [c["source_id"] for c in tag_links.corrupt_ledgers(rdir)] == [tag_links.UNLISTED_LEDGERS]
    assert tag_links.kept_anywhere(rdir) is None          # any tag could be kept
    with pytest.raises(tag_links.LedgerCorrupt):
        tag_links.ledger_keeps(rdir)
    with pytest.raises(tag_links.LedgerCorrupt):
        tag_links.deletion_intents(rdir)
    with pytest.raises(tag_links.LedgerCorrupt):
        tag_links.pending_resolutions(rdir)


def test_unlisted_ledgers_leave_tag_pages_pending_in_publication(tmp_path):
    from app.workers import publication
    (tmp_path / "reviews").mkdir()
    (tmp_path / "reviews" / "tag_links").write_text("not a directory")
    failed: dict[str, str] = {}
    publication._render_items(None, [NODE], wiki_dir=tmp_path / "wiki", root=tmp_path, now=NOW,
                              failed=failed)
    assert failed == {NODE: "ledger_corrupt"}


def test_a_damaged_resolving_or_deletion_record_still_counts(tmp_path):
    rdir = tmp_path / "reviews"
    _dangling(tag_links._resolving_path(rdir, SID, NODE))
    _dangling(tag_links._deletion_path(rdir, NODE))
    assert tag_links.pending_resolutions(rdir) == [
        {"source_id": SID, "node_id": NODE, "chosen": None}]     # it can only be cancelled
    assert tag_links.deletion_intents(rdir) == {NODE: {"node_id": NODE, "reason": None}}


def test_a_damaged_tracking_record_is_reported_and_never_replaced(tmp_path):
    rdir = tmp_path / "reviews"
    path = tag_history.tracking_path(rdir)
    _dangling(path)
    assert tag_history.tracking_problem(rdir) == "unreadable (dangling_symlink)"
    assert tag_history.load_tracking(rdir) is None        # display: less certainty, no refusal
    assert tag_history.ensure_migration_record(rdir, NOW) is False
    assert tag_history.init_fresh(rdir, NOW) == {"outcome": "refused",
                                                  "reason": "tracking_record_exists"}
    out = tag_links.migrate_tagging(rdir, apply=False, now=NOW)
    assert out["tracking_written"] is False
    assert path.is_symlink()


def test_a_file_in_place_of_a_documents_resolving_folder_is_never_no_operations(tmp_path):
    rdir = tmp_path / "reviews"
    base = rdir / "tag_links" / tag_links.RESOLVING_DIR
    base.mkdir(parents=True)
    (base / SID).write_text("not a directory")
    (base / "notes.txt").write_text("not ours")              # a non-canonical name is ignored
    with pytest.raises(tag_links.LedgerCorrupt):
        tag_links.pending_resolutions(rdir)                  # the whole-vault scan
    with pytest.raises(tag_links.LedgerCorrupt):
        tag_links.pending_resolutions(rdir, SID)             # the document's own scan
    (base / SID).unlink()
    assert tag_links.pending_resolutions(rdir) == []
    assert tag_links.pending_resolutions(rdir, SID) == []    # a verified absence: none


def test_a_directory_in_place_of_a_deletion_record_still_counts(tmp_path):
    rdir = tmp_path / "reviews"
    tag_links._deletion_path(rdir, NODE).mkdir(parents=True)
    assert tag_links.deletion_intents(rdir) == {NODE: {"node_id": NODE, "reason": None}}


def _repair_vault(tmp_path):
    """A vault the tag-link repair would migrate (a ledger without `tagging`, no tracking record)
    and repair (a graph), with a dangling blocked-tag list."""
    from app.backend import graph
    rdir = tmp_path / "reviews"
    ledger = tag_links.ledger_path(rdir, SID)
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"source_id": SID, "kept": {}, "drops": []}), encoding="utf-8")
    (tmp_path / "db").mkdir()
    graph.connect(tmp_path / "db" / "graph.sqlite").close()
    _dangling(tag_links.blocklist_path(tmp_path / "policies"))
    return ledger


def _authority_snapshot(tmp_path):
    return {str(p.relative_to(tmp_path)): (p.read_bytes() if p.is_file() and not p.is_symlink()
                                           else None)
            for p in sorted(tmp_path.rglob("*")) if p.name not in {".lock", ".vault.lock"}}


@pytest.mark.parametrize("apply", [True, False])
def test_tag_link_repair_refuses_a_damaged_blocklist_before_any_write(tmp_path, apply, capsys):
    sys.path.insert(0, str(ROOT / "scripts"))
    import rebuild_tag_links
    _repair_vault(tmp_path)
    before = _authority_snapshot(tmp_path)
    argv = [str(tmp_path), *(["--apply"] if apply else [])]
    assert rebuild_tag_links.main(argv) == 1
    assert "Refused: policies/blocked_tags.txt is unreadable" in capsys.readouterr().err
    assert _authority_snapshot(tmp_path) == before   # no tracking record, no migration, no queue
    assert not tag_history.tracking_path(tmp_path / "reviews").exists()
    assert not publication_queue.queue_path(tmp_path).exists()
    assert not revision.revision_path(tmp_path).exists()      # the revision never advanced


@pytest.mark.parametrize("damage", ["resolving", "deletion", "audit_log"])
def test_tag_link_repair_refuses_an_unlistable_inventory_before_any_write(tmp_path, damage):
    sys.path.insert(0, str(ROOT / "scripts"))
    import rebuild_tag_links
    _repair_vault(tmp_path)
    tag_links.blocklist_path(tmp_path / "policies").unlink()  # the list itself is fine (absent)
    target = {"resolving": tmp_path / "reviews" / "tag_links" / tag_links.RESOLVING_DIR,
              "deletion": tag_links._deletion_path(tmp_path / "reviews", NODE).parent,
              "audit_log": tmp_path / "reviews" / "audit_log"}[damage]
    target.write_text("not a directory")
    before = _authority_snapshot(tmp_path)
    assert rebuild_tag_links.main([str(tmp_path), "--apply"]) == 1
    assert _authority_snapshot(tmp_path) == before
    assert not revision.revision_path(tmp_path).exists()


def test_init_fresh_refusal_never_advances_the_revision(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import rebuild_tag_links
    _repair_vault(tmp_path)                                  # a ledger exists: not fresh
    assert rebuild_tag_links.main([str(tmp_path), "--init-fresh-tracking"]) == 1
    assert not revision.revision_path(tmp_path).exists()
    assert not tag_history.tracking_path(tmp_path / "reviews").exists()


# Review round 1: tagging-history evidence that can't be fully scanned lowers certainty.


def _fresh_tracking(rdir):
    tag_history.tracking_path(rdir).parent.mkdir(parents=True, exist_ok=True)
    tag_history.tracking_path(rdir).write_text(json.dumps({"since": NOW, "from_start": True}))


def test_an_absent_audit_log_is_complete_evidence(tmp_path):
    rdir = tmp_path / "reviews"
    _fresh_tracking(rdir)
    evidence = tag_history.audit_evidence(rdir)
    assert evidence.complete is True
    shown = tag_history.classify(ledger=None, complete=True, audit=evidence.get(SID))
    assert shown["state"] == tag_history.SHOWN_NEVER         # a fresh vault: known never tagged


@pytest.mark.parametrize("damage", ["unlistable", "dangling_entry", "not_a_record"])
def test_unavailable_audit_evidence_is_never_more_certain(tmp_path, damage):
    rdir = tmp_path / "reviews"
    _fresh_tracking(rdir)
    log = rdir / "audit_log"
    if damage == "unlistable":
        log.write_text("not a directory")
    elif damage == "dangling_entry":
        _dangling(log / f"{SID}-tag-keep-1.json")
    else:
        log.mkdir()
        (log / f"{SID}-tag-keep-1.json").write_text("[]")
    evidence = tag_history.audit_evidence(rdir)
    assert evidence.complete is False
    # classification: from_start: true and no evidence seen is NOT "never tagged"
    shown = tag_history.classify(ledger=None, complete=True, audit=evidence.get(SID))
    assert shown["state"] == tag_history.SHOWN_UNKNOWN
    # backfill: timed ledger evidence never becomes a precise first decision
    ledger = {"source_id": SID, "kept": {NODE: {"at": NOW, "by": "human"}}, "drops": []}
    found = tag_history.backfill(ledger, complete=True, audit=evidence.get(SID))
    assert found["first_decision_at"] is None and found["earliest_known_decision_at"] == NOW
    # decision timestamps: this action is never dated as the first decision
    later = "2026-11-15T00:00:00Z"
    marked = tag_links._decision_tagging(rdir, SID, {"source_id": SID, "kept": {}, "drops": []},
                                         later)
    assert marked == {"state": "tagged", "first_decision_at": None,
                      "earliest_known_decision_at": later}


# --- FIFOs never hang a reader (subprocess, so a regression fails instead of hanging the suite) ----


_FIFO_PROBE = r"""
import json, os, sys
from pathlib import Path
from app.backend import (apply_authorization, policy, producer_journal, publication_queue,
                         revision)
from app.workers import identity_journal, tag_history, tag_links
root = Path(sys.argv[1])
rdir = root / "reviews"
sid, node = "src_0000000000000c3c", "itm_00000000000000c3"
paths = {
    "retrieval": root / "policies" / "retrieval.yaml",
    "blocklist": tag_links.blocklist_path(root / "policies"),
    "producer": producer_journal.journal_path(root),
    "queue": publication_queue.queue_path(root),
    "revision": revision.revision_path(root),
    "authorizations": apply_authorization.store_path(root),
    "in_flight": rdir / "in_flight" / "rev_x.json",
    "ledger": tag_links.ledger_path(rdir, sid),
    "tracking": tag_history.tracking_path(rdir),
    "deletion": tag_links._deletion_path(rdir, node),
}
for p in paths.values():
    p.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(p)
out = {}
def probe(name, fn):
    try:
        fn()
        out[name] = "returned"
    except Exception as exc:
        out[name] = type(exc).__name__
probe("retrieval", lambda: policy.load_retrieval_policy(paths["retrieval"]))
probe("blocklist", lambda: tag_links.load_blocked(root / "policies"))
probe("producer", lambda: producer_journal.load(root))
probe("queue", lambda: publication_queue.load(root))
probe("revision", lambda: revision.current(root))
probe("authorizations", lambda: apply_authorization.load(root))
probe("in_flight", lambda: identity_journal.read(rdir, "rev_x"))
out["in_flight_ops"] = identity_journal.open_operations(rdir)
probe("ledger", lambda: tag_links.load_ledger(rdir, sid))
out["tracking"] = tag_history.tracking_problem(rdir)
out["deletion"] = tag_links.deletion_intents(rdir)
print(json.dumps(out))
"""


def test_fifos_never_hang_a_policy_journal_or_db_record_reader(tmp_path):
    probe = tmp_path / "probe" / "fifo_probe.py"
    probe.parent.mkdir()
    probe.write_text(_FIFO_PROBE)
    vault = tmp_path / "vault"
    vault.mkdir()
    done = subprocess.run([sys.executable, str(probe), str(vault)], cwd=ROOT, capture_output=True,
                          text=True, timeout=120, env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out == {
        "retrieval": "PolicyUnavailable", "blocklist": "PolicyUnavailable",
        "producer": "JournalCorrupt", "queue": "QueueCorrupt",
        "revision": "RevisionUnavailable", "authorizations": "StoreUnavailable",
        "in_flight": "JournalUnreadable",
        "in_flight_ops": [{"review_id": "rev_x", "op": None, "nodes": None}],
        "ledger": "LedgerCorrupt", "tracking": "unreadable (not_a_regular_file)",
        "deletion": {NODE: {"node_id": NODE, "reason": None}},
    }
