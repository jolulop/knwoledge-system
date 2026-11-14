"""ADR-0074 decision 1, integration coverage per authority family.

Real flows — intake, extraction, tagging, claims, synthesis, the review ledger, journals, the
blocked-tag policy — must write each family's files **through** `app.backend.durable_write`
(`write_guard.helper_writes`), and the autouse runtime guard (`tests/conftest.py`) fails any test in
which one of them is written outside it."""
from __future__ import annotations

from pathlib import Path

from app.backend import write_lock
from app.workers import identity_journal, items, reviews
from tests import write_guard
from tests.test_producer_journal import _generate, _synthesis_vault
from tests.test_producer_publication import _claims_vault, _extract_claims
from tests.test_claims import FakeAdapter as ClaimsAdapter
from tests.test_tag_links import _block, _build, _drop, _extract, _item, _keep, _sids


def _written(tmp_path: Path) -> dict[str, set[str]]:
    """family -> repo-shaped relative paths the helper wrote under this vault since the last call."""
    out: dict[str, set[str]] = {}
    for fam, path in write_guard.helper_writes:
        p = Path(path)
        if p.is_relative_to(tmp_path):
            out.setdefault(fam, set()).add(str(p.relative_to(tmp_path)))
    write_guard.helper_writes.clear()
    return out


def test_intake_extraction_tagging_and_the_policy_write_through_the_helper(tmp_path):
    write_guard.helper_writes.clear()
    _build(tmp_path, {"a.md": "alpha body", "b.md": "beta body"})
    sids = _sids(tmp_path)
    w = _written(tmp_path)
    assert {f"raw/manifests/{sid}.json" for sid in sids.values()} <= w["manifest"]

    _extract(tmp_path, {"alpha": [_item("Topic")], "beta": [_item("Topic")]})
    w = _written(tmp_path)
    assert "wiki/Items/topic.md" in w["page"]
    assert {"db/producer_journal.json", "db/publication_pending.json"} & w["db_json"]

    _keep(tmp_path, sids["a.md"], "Topic", description='My "own" words')
    w = _written(tmp_path)
    assert f"reviews/tag_links/{sids['a.md']}.json" in w["reviews"]       # the tag ledger
    assert any(p.startswith("reviews/audit_log/") for p in w["reviews"])  # its audit record
    assert "wiki/Items/topic.md" in w["page"] and "db/revision.json" in w["db_json"]

    assert _drop(tmp_path, sids["a.md"], "Topic")["effect"] in ("unused", "dropped")
    assert "reviews" in _written(tmp_path)

    _block(tmp_path, "Spam Tag")
    assert any(p.startswith("policies/") for p in _written(tmp_path)["policies"])


def test_claim_pages_write_through_the_helper(tmp_path):
    write_guard.helper_writes.clear()
    _claims_vault(tmp_path)
    write_guard.helper_writes.clear()
    _extract_claims(tmp_path, ClaimsAdapter())
    w = _written(tmp_path)
    assert any(p.startswith("wiki/Claims/clm_") for p in w["page"])
    assert "db/producer_journal.json" in w["db_json"]


def test_synthesis_pages_and_their_proposals_write_through_the_helper(tmp_path):
    jobs = _synthesis_vault(tmp_path)
    write_guard.helper_writes.clear()
    _generate(tmp_path, jobs)
    w = _written(tmp_path)
    assert any(p.startswith("wiki/Synthesis/syn_") for p in w["page"])
    assert any(p.startswith("reviews/pending/rev_") for p in w["reviews"])


def test_the_review_ledger_lifecycle_writes_through_the_helper(tmp_path):
    rdir = tmp_path / "reviews"
    write_guard.helper_writes.clear()
    with write_lock.hold(rdir):
        rid = reviews.create_review_item(rdir, review_type="archive_source",
                                         subject={"source_id": "src_0123456789abcdef"},
                                         proposal={"to_status": "archive_candidate"})
    assert f"reviews/pending/{rid}.json" in _written(tmp_path)["reviews"]
    reviews.defer_review_item(rdir, rid, note="later")
    w = _written(tmp_path)["reviews"]
    assert f"reviews/pending/{rid}.json" in w and any("-deferred-" in p for p in w)
    reviews.resolve_review_item(rdir, rid, decision="approved", decided_by="human")
    w = _written(tmp_path)["reviews"]
    assert f"reviews/approved/{rid}.json" in w
    assert any(p.startswith(f"reviews/audit_log/{rid}-approved-") for p in w)
    reviews.reopen_review_item(rdir, rid, reason="changed my mind")
    w = _written(tmp_path)["reviews"]
    assert f"reviews/pending/{rid}.json" in w and any("-reopened-" in p for p in w)


def test_recovery_journals_write_through_the_helper(tmp_path):
    rdir = tmp_path / "reviews"
    write_guard.helper_writes.clear()
    nid = items.node_id("Topic")
    identity_journal.begin(rdir, "rev_0123456789abcdef", op="merge", nodes=[nid])
    identity_journal.set_stage(rdir, "rev_0123456789abcdef", "edges_repointed")
    w = _written(tmp_path)["reviews"]
    assert "reviews/in_flight/rev_0123456789abcdef.json" in w
    identity_journal.end(rdir, "rev_0123456789abcdef")
    assert not (rdir / "in_flight" / "rev_0123456789abcdef.json").exists()


def test_a_fresh_vault_creates_its_authority_directories_durably(tmp_path):
    """Review round 1 (P1): on a vault with no authority directories yet, real flows create every
    one of them through `durable_write.make_dirs` (each synced into its parent); the runtime guard
    fails the test if any caller creates one itself."""
    write_guard.helper_dirs.clear()
    _build(tmp_path, {"a.md": "alpha body"})
    _extract(tmp_path, {"alpha": [_item("Topic")]})
    _keep(tmp_path, _sids(tmp_path)["a.md"], "Topic")
    made = {str(Path(p).relative_to(tmp_path)) for _f, p in write_guard.helper_dirs
            if Path(p).is_relative_to(tmp_path)}
    assert {"db", "raw/manifests", "wiki", "wiki/Items", "reviews", "reviews/tag_links",
            "reviews/audit_log"} <= made
