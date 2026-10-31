"""ADR-0068 slice 1: the vault write lock, per-producer run guards, and commit phases that re-validate
under the lock.

Covers acceptance tests 1 (one governance outcome under concurrency), 2 (hide/archive during an
extraction model call survives the commit) and 5 (one run per producer; tagging stays available
during model calls)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from fastapi.testclient import TestClient

from app.backend import graph, manifests, run_guard, write_lock
from app.backend import main as main_module
from app.backend.config import get_settings
from app.llm.cache import ResponseCache
from app.llm.client import LLMClient
from app.workers import claims, extract, intake, retention, reviews, synthesis, tag_links
from app.workers.wiki_render import parse_frontmatter
from tests import fixtures
from tests import test_claims as claims_t
from tests import test_synthesis as syn_t
from tests import test_items as items_t

@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app)


def _in_thread(fn):
    """Run `fn` in a thread; returns (thread, result dict with "value" or "error")."""
    out: dict = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as exc:                      # noqa: BLE001 - reported to the test
            out["error"] = exc
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


def _hold_write_lock_briefly(reviews_dir: Path) -> threading.Thread:
    """A thread that takes the write lock (as a tag action would), then releases it."""
    def run():
        with write_lock.hold(reviews_dir):
            pass
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _try_guard(db_dir: Path, producer: str) -> BaseException | None:
    """Try `producer`'s run guard from another thread; the error it raised, or None."""
    def run():
        with run_guard.guard(db_dir, producer):
            pass
    t, out = _in_thread(run)
    t.join(5)
    return out.get("error")


# --- the write lock ------------------------------------------------------------------------------


def test_write_lock_is_reentrant_per_thread(tmp_path):
    with write_lock.hold(tmp_path / "reviews"):
        with write_lock.hold(tmp_path / "reviews"):
            assert write_lock.held()
        assert write_lock.held()
    assert not write_lock.held()


def test_the_tag_ledger_lock_is_the_write_lock():
    assert tag_links.ledger_lock is write_lock.hold


def test_write_lock_excludes_another_thread(tmp_path):
    with write_lock.hold(tmp_path / "reviews"):
        t = _hold_write_lock_briefly(tmp_path / "reviews")
        t.join(0.3)
        assert t.is_alive()                               # waits while this thread holds it
    t.join(5)
    assert not t.is_alive()


def test_write_lock_excludes_another_process(tmp_path):
    probe = ("import fcntl, sys\nfh = open(sys.argv[1], 'a')\n"
             "try:\n    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
             "except BlockingIOError:\n    sys.exit(7)\n")
    path = write_lock.lock_path(tmp_path / "reviews")
    with write_lock.hold(tmp_path / "reviews"):
        assert subprocess.run([sys.executable, "-c", probe, str(path)]).returncode == 7
    assert subprocess.run([sys.executable, "-c", probe, str(path)]).returncode == 0


def test_promotion_and_backup_cannot_deadlock(tmp_path, monkeypatch):
    """Review round 1, P1: promotion took the write lock before its job row took the vault lock,
    while backup takes the vault lock (exclusive) before the write lock. Pause promotion exactly
    between the two: with the order enforced by `hold`, it already holds the vault lock shared, so a
    backup started now refuses after its timeout instead of deadlocking."""
    from app.backend import db
    from app.workers import promote
    paused, go = threading.Event(), threading.Event()
    real_insert = db.insert_job

    def pausing(conn, **kwargs):
        if kwargs.get("job_type") == "promote":
            paused.set()
            go.wait(30)
        return real_insert(conn, **kwargs)
    monkeypatch.setattr(db, "insert_job", pausing)
    t, out = _in_thread(lambda: promote.promote_candidates(
        tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite", rebuild_index=False))
    assert paused.wait(10)                                # inside the write lock, before the job row
    proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "backup.py"), str(tmp_path),
                             "--lock-timeout", "1"], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        rc = proc.wait(timeout=20)                        # a deadlock would time out here
    finally:
        if proc.poll() is None:
            proc.kill()
        go.set()
    t.join(30)
    assert not t.is_alive() and "error" not in out
    assert rc == 2                                        # BackupRefused: a writer held the vault lock


def test_a_backup_thread_and_a_writer_thread_cannot_deadlock(tmp_path, monkeypatch):
    """Review round 2: the process lock used to be taken before the vault lock, so a writer thread
    could hold it while waiting for a backup thread that held the vault lock exclusively and was
    about to take the write lock. Start them in exactly that order; both must finish."""
    import backup
    vault_held, writer_started = threading.Event(), threading.Event()
    real = backup._writer_exclusion

    def paused_exclusion(root):                           # backup: vault lock held, write lock next
        vault_held.set()
        assert writer_started.wait(10)
        time.sleep(0.3)                                   # let the writer reach its wait
        return real(root)
    monkeypatch.setattr(backup, "_writer_exclusion", paused_exclusion)
    (tmp_path / "db").mkdir()
    t_backup, b = _in_thread(lambda: backup.create_backup_report(tmp_path, lock_timeout=5))
    assert vault_held.wait(10)

    def writer():
        writer_started.set()
        with write_lock.hold(tmp_path / "reviews"):
            pass
    t_writer, w = _in_thread(writer)
    t_backup.join(15)
    t_writer.join(15)
    assert not t_backup.is_alive() and not t_writer.is_alive()
    assert "error" not in b and "error" not in w


# --- the run guard -------------------------------------------------------------------------------


def test_a_second_run_of_the_same_producer_refuses(tmp_path):
    db_dir = tmp_path / "db"
    with run_guard.guard(db_dir, "extract_claims"):
        busy = _try_guard(db_dir, "extract_claims")
        assert isinstance(busy, run_guard.ProducerBusy) and busy.producer == "extract_claims"
        assert _try_guard(db_dir, "enrich") is None       # a different producer runs alongside
    with run_guard.guard(db_dir, "extract_claims"):       # released: the next run starts
        pass


def test_the_run_guard_is_reentrant_in_one_thread(tmp_path):
    with run_guard.guard(tmp_path / "db", "lint"):
        with run_guard.guard(tmp_path / "db", "lint"):
            pass


def test_a_producer_name_never_becomes_a_path(tmp_path):
    with pytest.raises(ValueError):
        run_guard.lock_path(tmp_path / "db", "../x")


def test_run_cli_refuses_with_its_own_exit_code(tmp_path, capsys):
    hold, release = threading.Event(), threading.Event()

    def holder():
        with run_guard.guard(tmp_path / "db", "promote"):
            hold.set()
            release.wait(10)
    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert hold.wait(5)
    ran = []
    try:
        assert run_guard.run_cli(tmp_path / "db", "promote", lambda: ran.append(1) or 0) \
            == run_guard.EXIT_BUSY
    finally:
        release.set()
        t.join(5)
    assert ran == [] and "promote is already running" in capsys.readouterr().err


def test_a_producer_script_refuses_while_another_run_holds_its_guard(tmp_path):
    env = {**os.environ, "KNOWLEDGE_SYSTEM_HOME": str(tmp_path)}
    with run_guard.guard(tmp_path / "db", "generate_wiki"):
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_wiki.py")],
                              cwd=tmp_path, env=env, capture_output=True, text=True)
    assert proc.returncode == run_guard.EXIT_BUSY, proc.stderr
    assert "generate_wiki is already running" in proc.stderr
    assert not (tmp_path / "wiki" / "Sources").exists()  # wrote nothing


def test_an_api_job_route_refuses_a_second_run(client, tmp_path):
    with run_guard.guard(tmp_path / "db", "lint"):        # the route runs in another thread
        r = client.post("/jobs/lint")
    assert r.status_code == 409 and r.json()["detail"] == "already_running: lint"


# --- acceptance test 1: one governance outcome ----------------------------------------------------


def test_concurrent_approve_and_reject_record_one_outcome(tmp_path, monkeypatch):
    rd = tmp_path / "reviews"
    rid = reviews.create_review_item(rd, review_type="archive_source",
                                     subject={"source_id": "src_0123456789abcdef"},
                                     proposal={"to_status": "archive_candidate"})
    real_now = reviews.iso_now

    def slow_now():                                       # widens the check-then-write window
        time.sleep(0.2)
        return real_now()
    monkeypatch.setattr(reviews, "iso_now", slow_now)
    barrier = threading.Barrier(2)

    def decide(decision):
        barrier.wait(5)
        return reviews.resolve_review_item(rd, rid, decision=decision, decided_by="human")
    ta, a = _in_thread(lambda: decide("approved"))
    tb, b = _in_thread(lambda: decide("rejected"))
    ta.join(10)
    tb.join(10)
    assert sorted([a["value"], b["value"]]) == [False, True]
    outcomes = [d for d in ("approved", "rejected") if (rd / d / f"{rid}.json").exists()]
    assert len(outcomes) == 1
    assert len(list((rd / "audit_log").glob(f"{rid}-*.json"))) == 1
    assert not (rd / "pending" / f"{rid}.json").exists()


def test_a_reopen_waits_for_a_running_apply(client, tmp_path, monkeypatch):
    for d in ("scripts", "app", "policies", "templates"):  # Apply's index rebuild + validators
        shutil.copytree(ROOT / d, tmp_path / d, dirs_exist_ok=True)
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.write_markdown(inbox / "doc.md")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    md = tmp_path / "raw" / "manifests"
    sid = manifests.list_manifests(md)[0]["source_id"]
    rid = reviews.create_review_item(tmp_path / "reviews", review_type="archive_source",
                                     subject={"source_id": sid},
                                     proposal={"to_status": "archive_candidate"})
    assert reviews.resolve_review_item(tmp_path / "reviews", rid, decision="approved",
                                       decided_by="human")

    started, go = threading.Event(), threading.Event()
    real = retention.apply_archive_sources

    def blocking(*args, **kwargs):                        # Apply's source-status phase, which ran
        started.set()                                     # outside the lock before ADR-0068
        assert go.wait(10)
        return real(*args, **kwargs)
    monkeypatch.setattr(retention, "apply_archive_sources", blocking)

    t_apply, applied = _in_thread(lambda: main_module.run_apply(main_module.settings))
    assert started.wait(10)
    t_reopen, reopened = _in_thread(
        lambda: client.post(f"/reviews/{rid}/reopen", json={"reason": "not yet"}))
    t_reopen.join(0.5)
    assert t_reopen.is_alive()                            # the reopen waits for the Apply
    go.set()
    t_apply.join(30)
    t_reopen.join(30)
    assert "error" not in applied and "error" not in reopened
    # One outcome: Apply archived the source, so the reopen that followed it is refused.
    assert manifests.get_status(manifests.load_manifest(md, sid)) == "archive_candidate"
    r = reopened["value"]
    assert r.status_code == 409, r.text
    assert (tmp_path / "reviews" / "approved" / f"{rid}.json").exists()


# --- acceptance test 2: decisions during a model call survive the commit -------------------------


def test_an_archive_during_a_claims_model_call_survives_the_commit(tmp_path):
    claims_t._build(tmp_path)
    claims_t._gen_wiki(tmp_path)
    sid = claims_t._sids(tmp_path)["doc.md"]
    gdb = tmp_path / "db" / "graph.sqlite"
    graph.init_db(gdb)

    class ArchivingAdapter(claims_t.FakeAdapter):
        def parse(self, *args, **kwargs):
            if not self.calls:                            # the archive executor, mid-call
                with write_lock.hold(tmp_path / "reviews"):
                    manifests.set_status(tmp_path / "raw" / "manifests", sid, "archive_candidate")
                    gconn = graph.connect(gdb)
                    graph.upsert_node(gconn, node_id=sid, node_type="source", slug=sid,
                                      status="archive_candidate")
                    gconn.close()
            return super().parse(*args, **kwargs)

    assert claims_t._extract(tmp_path, ArchivingAdapter())["claims_written"] == 2
    assert manifests.get_status(manifests.load_manifest(tmp_path / "raw" / "manifests", sid)) \
        == "archive_candidate"
    gconn = graph.connect(gdb)
    assert graph.get_node(gconn, sid)["status"] == "archive_candidate"
    gconn.close()


def test_a_hide_during_a_claims_model_call_survives_the_commit(tmp_path):
    claims_t._build(tmp_path)
    claims_t._gen_wiki(tmp_path)
    claims_t._extract(tmp_path, claims_t.FakeAdapter())
    gdb = tmp_path / "db" / "graph.sqlite"
    gconn = graph.connect(gdb)
    cid = sorted(graph.active_node_ids_of_type(gconn, "claim"))[0]
    gconn.close()

    class HidingAdapter(claims_t.FakeAdapter):
        def parse(self, *args, **kwargs):
            if not self.calls:                            # the hide executor, mid-call
                with write_lock.hold(tmp_path / "reviews"):
                    c = graph.connect(gdb)
                    claims.recompose_claim(
                        c, cid=cid, claims_dir=tmp_path / "wiki" / "Claims",
                        reviews_dir=tmp_path / "reviews", now="2026-10-28T00:00:00+00:00",
                        markdown_dir=tmp_path / "normalized" / "markdown", hide=True)
                    c.close()
            return super().parse(*args, **kwargs)

    # A fresh response cache, so the forced re-run really calls the (hiding) model.
    claims.extract_claims(
        tmp_path, client=LLMClient({"anthropic": HidingAdapter()},
                                   cache=ResponseCache(tmp_path / "db" / "cache-rerun.sqlite")),
        model_ref=claims_t.MODEL_REF, jobs_db=tmp_path / "db" / "jobs.sqlite", force=True)
    gconn = graph.connect(gdb)
    assert graph.get_node(gconn, cid)["status"] == "hidden"
    gconn.close()
    page = (tmp_path / "wiki" / "Claims" / f"{cid}.md").read_text(encoding="utf-8")
    assert parse_frontmatter(page)["status"] == "hidden"


def test_an_archive_during_a_tags_model_call_survives_the_commit(tmp_path):
    items_t._build(tmp_path)
    items_t._gen_wiki(tmp_path)
    sid = items_t._sids(tmp_path)["doc.md"]
    gdb = tmp_path / "db" / "graph.sqlite"
    graph.init_db(gdb)

    class ArchivingAdapter(items_t.FakeAdapter):
        def parse(self, *args, **kwargs):
            if not self.calls:
                with write_lock.hold(tmp_path / "reviews"):
                    manifests.set_status(tmp_path / "raw" / "manifests", sid, "archive_candidate")
                    gconn = graph.connect(gdb)
                    graph.upsert_node(gconn, node_id=sid, node_type="source", slug=sid,
                                      status="archive_candidate")
                    gconn.close()
            return super().parse(*args, **kwargs)

    items_t._extract(tmp_path, ArchivingAdapter())
    gconn = graph.connect(gdb)
    assert graph.get_node(gconn, sid)["status"] == "archive_candidate"
    gconn.close()


def test_an_archive_during_extraction_survives_the_manifest_write(tmp_path, monkeypatch):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.write_markdown(inbox / "doc.md")
    jobs = tmp_path / "db" / "jobs.sqlite"
    intake.scan_inbox(tmp_path, jobs_db=jobs)
    md = tmp_path / "raw" / "manifests"
    sid = manifests.list_manifests(md)[0]["source_id"]
    real = extract._extract_one

    def archiving(manifest, *args, **kwargs):             # an Apply lands while the file extracts
        out = real(manifest, *args, **kwargs)
        with write_lock.hold(tmp_path / "reviews"):
            manifests.set_status(md, sid, "archive_candidate")
        return out
    monkeypatch.setattr(extract, "_extract_one", archiving)
    assert extract.extract_sources(tmp_path, jobs_db=jobs)["extracted"] == 1
    m = manifests.load_manifest(md, sid)
    assert manifests.get_status(m) == "archive_candidate" and m["ingestion_status"] == "extracted"


def _gen_fresh(tmp_path, adapter, jobs, cache_name, **kw):
    """Synthesis generation with its own response cache, so the model is really called."""
    return synthesis.generate_syntheses(
        tmp_path, client=LLMClient({"anthropic": adapter},
                                   cache=ResponseCache(tmp_path / "db" / cache_name)),
        model_ref=syn_t.HEAVY, jobs_db=jobs, rebuild_index=False, **kw)


def _topic(tmp_path) -> dict:
    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        [nid] = graph.active_node_ids_of_type(conn, "item")
        return dict(graph.get_node(conn, nid))
    finally:
        conn.close()


@pytest.mark.parametrize("change", ["drop_tag_link", "hide_claim", "hide_topic"])
def test_evidence_changed_during_a_synthesis_model_call_discards_the_result(tmp_path, change):
    """Review round 1, P1: the result is valid only for the evidence the model saw."""
    jobs = syn_t._build(tmp_path)
    topic = _topic(tmp_path)
    sid_b = syn_t._sid(tmp_path, "b.md")
    rd, wd = tmp_path / "reviews", tmp_path / "wiki"

    class ChangingAdapter(syn_t.SynthAdapter):
        def parse(self, *args, **kwargs):
            with write_lock.hold(rd):
                conn = graph.connect(tmp_path / "db" / "graph.sqlite")
                if change == "drop_tag_link":             # document B no longer keeps the topic
                    assert tag_links.drop(conn, source_id=sid_b, node_id=topic["node_id"],
                                          wiki_dir=wd, reviews_dir=rd)["outcome"] == "dropped"
                elif change == "hide_claim":              # B's supporting claim hidden
                    [cid] = graph.claims_for_source(conn, sid_b)
                    claims.recompose_claim(conn, cid=cid, claims_dir=wd / "Claims", reviews_dir=rd,
                                           now="2026-10-28T00:00:00+00:00",
                                           markdown_dir=tmp_path / "normalized" / "markdown",
                                           hide=True)
                else:                                     # the topic itself hidden
                    graph.upsert_node(conn, node_id=topic["node_id"], node_type="item",
                                      slug=topic["slug"], status="hidden")
                conn.close()
            return super().parse(*args, **kwargs)

    summary = syn_t._gen(tmp_path, ChangingAdapter(), jobs)
    assert summary["skipped_changed_during_call"] == 1 and summary["syntheses_written"] == 0
    assert not (wd / "Synthesis").exists() or not list((wd / "Synthesis").glob("*.md"))
    assert all(json.loads(f.read_text())["type"] != "propose_synthesis"
               for f in (rd / "pending").glob("*.json"))


def test_an_approval_during_a_synthesis_model_call_keeps_the_approved_prose(tmp_path):
    """Review round 2, P1: Apply promotes the artifact, so prose the model returns after the human
    approved the current proposal must not replace the prose they read."""
    from app.workers import enrichment_artifact as art
    jobs = syn_t._build(tmp_path)
    syn_t._gen(tmp_path, syn_t.SynthAdapter(), jobs)
    rid = syn_t._propose_rid(tmp_path)
    rd = tmp_path / "reviews"
    artifact = art.synthesis_artifact_path(tmp_path / "normalized" / "enrichment",
                                           _topic(tmp_path)["node_id"])
    approved_bytes = artifact.read_bytes()
    page = syn_t._syn_page(tmp_path)
    page_bytes = page.read_bytes()

    class ApprovingAdapter(syn_t.SynthAdapter):
        def parse(self, *args, **kwargs):
            reviews.resolve_review_item(rd, rid, decision="approved", decided_by="human")
            return {"summary": "Different prose the human never read.",
                    "synthesis": "A rewritten synthesis.", "confidence": 0.8}

    summary = _gen_fresh(tmp_path, ApprovingAdapter(), jobs, "rerun.sqlite", force=True)
    assert summary["skipped_reviewed"] == 1 and summary["syntheses_written"] == 0
    assert artifact.read_bytes() == approved_bytes and page.read_bytes() == page_bytes
    assert (rd / "approved" / f"{rid}.json").exists()


def test_a_rejection_during_a_synthesis_model_call_wins(tmp_path):
    jobs = syn_t._build(tmp_path)
    syn_t._gen(tmp_path, syn_t.SynthAdapter(), jobs)
    rid = syn_t._propose_rid(tmp_path)
    rd = tmp_path / "reviews"

    class RejectingAdapter(syn_t.SynthAdapter):
        def parse(self, *args, **kwargs):
            reviews.resolve_review_item(rd, rid, decision="rejected", decided_by="human")
            return super().parse(*args, **kwargs)

    summary = _gen_fresh(tmp_path, RejectingAdapter(), jobs, "rerun.sqlite", force=True)
    assert summary["skipped_reviewed"] == 1 and summary["syntheses_written"] == 0
    assert (rd / "rejected" / f"{rid}.json").exists()


# --- extraction outputs and producer inputs (review round 1, P2) ----------------------------------


def _install_new_text(tmp_path, sid):
    """What a re-extraction's install does, under the write lock: new normalized text in place."""
    md = tmp_path / "normalized" / "markdown" / f"{sid}.md"
    with write_lock.hold(tmp_path / "reviews"):
        md.write_text(md.read_text(encoding="utf-8") + "\nA paragraph added by re-extraction.\n",
                      encoding="utf-8")


def test_new_text_installed_during_a_claims_model_call_discards_the_claims(tmp_path):
    claims_t._build(tmp_path)
    claims_t._gen_wiki(tmp_path)
    sid = claims_t._sids(tmp_path)["doc.md"]

    class ReextractingAdapter(claims_t.FakeAdapter):
        def parse(self, *args, **kwargs):
            if not self.calls:
                _install_new_text(tmp_path, sid)
            return super().parse(*args, **kwargs)

    summary = claims_t._extract(tmp_path, ReextractingAdapter())
    assert summary["skipped_input_changed"] == 1 and summary["claims_written"] == 0
    assert not (tmp_path / "wiki" / "Claims").exists() or \
        not list((tmp_path / "wiki" / "Claims").glob("*.md"))


def test_new_text_installed_during_a_tags_model_call_writes_no_suggestions(tmp_path):
    items_t._build(tmp_path)
    items_t._gen_wiki(tmp_path)
    sid = items_t._sids(tmp_path)["doc.md"]

    class ReextractingAdapter(items_t.FakeAdapter):
        def parse(self, *args, **kwargs):
            if not self.calls:
                _install_new_text(tmp_path, sid)
            return super().parse(*args, **kwargs)

    summary = items_t._extract(tmp_path, ReextractingAdapter())
    assert summary["input_changed_sources"] == [sid]
    conn = graph.connect(tmp_path / "db" / "graph.sqlite")
    try:
        assert not conn.execute("SELECT 1 FROM edges WHERE edge_type = 'mentions'").fetchall()
    finally:
        conn.close()


def test_new_text_installed_during_an_enrich_model_call_writes_no_artifact(tmp_path):
    from tests import test_enrich as enrich_t
    enrich_t._build(tmp_path)
    sid = enrich_t._sid(tmp_path, "doc.md")

    class ReextractingAdapter(enrich_t.FakeAdapter):
        def parse(self, *args, **kwargs):
            if not self.calls:
                _install_new_text(tmp_path, sid)
            return super().parse(*args, **kwargs)

    summary = enrich_t._enrich(tmp_path, ReextractingAdapter(), source_ids=[sid])
    assert summary["skipped_input_changed"] == 1 and summary["enriched"] == 0
    assert not (tmp_path / "normalized" / "enrichment" / f"{sid}.json").exists()


def test_a_forced_re_extraction_installs_only_after_a_source_page_render(tmp_path, monkeypatch):
    """A page render sees all-old or all-new normalized outputs: extraction stages its outputs
    outside the lock and installs them (with the log and manifest) only under it."""
    from app.workers import wiki_render
    claims_t._build(tmp_path)
    sid = claims_t._sids(tmp_path)["doc.md"]
    md_path = tmp_path / "normalized" / "markdown" / f"{sid}.md"
    old_md = md_path.read_text(encoding="utf-8")
    real_assemble = extract.assemble

    def new_text(*args, **kwargs):                        # the re-extraction yields different text
        text, chunks = real_assemble(*args, **kwargs)
        return text + "\nRe-extracted paragraph.\n", chunks
    monkeypatch.setattr(extract, "assemble", new_text)
    rendering, go, seen = threading.Event(), threading.Event(), {}
    real_render = wiki_render.render_source_page

    def slow_render(template, manifest, markdown, **kwargs):
        seen.setdefault("markdown", []).append(markdown)
        rendering.set()
        assert go.wait(10)
        return real_render(template, manifest, markdown, **kwargs)
    monkeypatch.setattr(wiki_render, "render_source_page", slow_render)

    t_gen, gen = _in_thread(lambda: claims_t._gen_wiki(tmp_path))
    assert rendering.wait(10)
    t_ext, ext = _in_thread(lambda: extract.extract_sources(
        tmp_path, force=True, jobs_db=tmp_path / "db" / "jobs.sqlite"))
    t_ext.join(1.0)
    assert t_ext.is_alive()                               # its install waits for the render
    assert md_path.read_text(encoding="utf-8") == old_md  # nothing replaced yet
    go.set()
    t_gen.join(30)
    t_ext.join(30)
    assert "error" not in gen and "error" not in ext
    # The page rendered the old text whole; then the extraction's own publication (ADR-0068 decision
    # 5) rendered the new text, after its install.
    first, *later = seen["markdown"]
    assert first == old_md
    assert later and later[-1].endswith("Re-extracted paragraph.\n")
    assert md_path.read_text(encoding="utf-8").endswith("Re-extracted paragraph.\n")
    assert not (tmp_path / "normalized" / ".staging" / sid).exists()


# --- acceptance test 1: conflicting merges ---------------------------------------------------------


def test_concurrent_opposite_merges_file_one(client, tmp_path, monkeypatch):
    """A <- B and B <- A filed at the same moment: exactly one is filed, the other is refused."""
    from app.workers import human_merge
    from tests import test_human_merge as hm_t
    conn = hm_t._graph(tmp_path)
    a = hm_t._item(tmp_path, conn, "Agentic AI")
    b = hm_t._item(tmp_path, conn, "AI Agents")
    conn.close()
    real_create = human_merge.reviews.create_review_item

    def slow_create(*args, **kwargs):                     # widens the check-then-file window
        time.sleep(0.3)
        return real_create(*args, **kwargs)
    monkeypatch.setattr(human_merge.reviews, "create_review_item", slow_create)
    barrier = threading.Barrier(2)

    def file(survivor, absorbed):
        barrier.wait(5)
        return client.post("/reviews/merge", json={"survivor": survivor, "absorbed": absorbed})
    t1, r1 = _in_thread(lambda: file(a, b))
    t2, r2 = _in_thread(lambda: file(b, a))
    t1.join(30)
    t2.join(30)
    codes = sorted([r1["value"].status_code, r2["value"].status_code])
    assert codes == [200, 409], (r1["value"].text, r2["value"].text)
    approved = list((tmp_path / "reviews" / "approved").glob("*.json"))
    assert len(approved) == 1


# --- acceptance test 5: one run per producer; tagging stays available ------------------------------


def test_tagging_stays_available_and_a_second_run_refuses_during_a_model_call(tmp_path):
    claims_t._build(tmp_path)
    claims_t._gen_wiki(tmp_path)
    seen: dict = {}

    class ProbingAdapter(claims_t.FakeAdapter):
        def parse(self, *args, **kwargs):
            if not self.calls:
                # A tag action needs the write lock: it must get it while the model call runs.
                t = _hold_write_lock_briefly(tmp_path / "reviews")
                t.join(5)
                seen["write_lock"] = not t.is_alive()
                # A second run of the same producer refuses at once.
                seen["second_run"] = _try_guard(tmp_path / "db", "extract_claims")
            return super().parse(*args, **kwargs)

    with run_guard.guard(tmp_path / "db", "extract_claims"):   # this run's entry point
        assert claims_t._extract(tmp_path, ProbingAdapter())["claims_written"] == 2
    assert seen["write_lock"] is True
    assert isinstance(seen["second_run"], run_guard.ProducerBusy)

