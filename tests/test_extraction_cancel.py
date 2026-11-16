"""ADR-0074 slice 6c — stale extraction jobs and cancellation (decisions 7.3 and 7.5).

A run holds the extract guard for its whole duration and, under it and before creating its own
job, marks every extract job still `running` as `failed` (`interrupted`) — lost execution after a
hard kill. A run is cancellable (the API, the CLI's first Ctrl-C, a server shutdown): honoured
between documents and before an install starts, a parse in progress abandoned with its child
cleaned up, an install that has begun finished; the run stays registered, and holds its guard,
until its cleanup is complete.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import db, manifests, run_guard  # noqa: E402
from app.backend import extraction_install as xi  # noqa: E402
from app.workers import extract, extract_attempts, extract_runner, intake  # noqa: E402
from tests import fixtures  # noqa: E402

STUB = ROOT / "tests" / "extract_child_stub.py"


def _stub(mode: str, out: Path) -> list[str]:
    return [sys.executable, "-I", str(STUB), mode, str(out)]


def _vault(tmp_path: Path, n: int = 1) -> Path:
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.copy_templates(tmp_path)
    for i in range(n):
        path = fixtures.write_markdown(inbox / f"doc{i}.md")
        path.write_text(path.read_text() + f"\n\nDocument number {i}.\n", encoding="utf-8")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    return tmp_path


def _sids(root: Path) -> list[str]:
    return [m["source_id"] for m in manifests.scan_manifests(root / "raw" / "manifests").valid]


def _mark(out: Path, name: str, within: float = 20.0):
    end = time.monotonic() + within
    while time.monotonic() < end:
        if (out / name).exists():
            return json.loads((out / name).read_text())
        time.sleep(0.02)
    raise AssertionError(f"no {name} marker")


def _gone(pid: int, within: float = 10.0) -> bool:
    end = time.monotonic() + within
    while True:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            return True
        if state in ("Z", "X") or time.monotonic() >= end:
            return state in ("Z", "X")
        time.sleep(0.02)


def _job(root: Path, job_id: str) -> dict:
    conn = db.connect(root / "db" / "jobs.sqlite")
    try:
        return db.get_job(conn, job_id)
    finally:
        conn.close()


def _stale_row(root: Path, job_id: str, job_type: str = "extract", status: str = "running"):
    """A row as a hard-killed run leaves it (no process, no lock)."""
    db.init_db(root / "db" / "jobs.sqlite")
    conn = sqlite3.connect(root / "db" / "jobs.sqlite")
    conn.execute("INSERT INTO jobs (job_id, job_type, status, created_at, started_at) "
                 "VALUES (?, ?, ?, ?, ?)", (job_id, job_type, status, "2026-11-01T00:00:00+00:00",
                                            "2026-11-01T00:00:00+00:00"))
    conn.commit()
    conn.close()


def _guard_busy(root: Path) -> bool:
    """Whether another thread or process holds the extract guard (tried from a fresh thread)."""
    out = {}

    def probe():
        try:
            with run_guard.guard(root / "db", "extract"):
                out["busy"] = False
        except run_guard.ProducerBusy:
            out["busy"] = True
    t = threading.Thread(target=probe)
    t.start()
    t.join()
    return out["busy"]


class _Background:
    """`extract_sources` on a thread; `.result` / `.error` once joined."""

    def __init__(self, root: Path, **kw) -> None:
        self.result = self.error = None

        def go():
            try:
                self.result = extract.extract_sources(root, jobs_db=root / "db" / "jobs.sqlite",
                                                      **kw)
            except BaseException as exc:  # noqa: BLE001
                self.error = exc
        self.thread = threading.Thread(target=go)
        self.thread.start()

    def join(self, within: float = 30.0) -> dict:
        self.thread.join(within)
        assert not self.thread.is_alive(), "the run did not stop"
        assert self.error is None, self.error
        return self.result


# --- decision 7.3: stale `running` extraction jobs -----------------------------------------


def test_a_stale_running_extract_job_is_marked_interrupted_by_the_next_run(tmp_path):
    root = _vault(tmp_path)
    _stale_row(root, "job_stale_extract")
    _stale_row(root, "job_stale_enrich", job_type="enrich")          # another producer
    _stale_row(root, "job_done_extract", status="succeeded")
    out = extract.extract_sources(root, jobs_db=root / "db" / "jobs.sqlite")
    assert out["interrupted_jobs"] == [{"job_id": "job_stale_extract",
                                        "started_at": "2026-11-01T00:00:00+00:00"}]
    stale = _job(root, "job_stale_extract")
    assert stale["status"] == "failed" and stale["error_message"] == db.INTERRUPTED_MESSAGE
    assert stale["metadata"]["reconciled"] == {"reason": "interrupted", "by_job": out["job_id"],
                                               "at": out["extracted_at"]}
    assert stale["finished_at"] == out["extracted_at"]
    assert _job(root, "job_stale_enrich")["status"] == "running"     # not switched to it here
    assert _job(root, "job_done_extract")["status"] == "succeeded"
    assert _job(root, out["job_id"])["status"] == "succeeded"
    assert extract.extract_sources(root, jobs_db=root / "db" / "jobs.sqlite")[
        "interrupted_jobs"] == []                                     # reconciled once


def test_a_job_this_process_still_runs_is_never_reconciled(tmp_path):
    root = _vault(tmp_path)
    db.init_db(root / "db" / "jobs.sqlite")
    conn = db.connect(root / "db" / "jobs.sqlite")
    try:
        db.insert_job(conn, job_id="job_live", job_type="extract", status="running",
                      created_at="2026-11-01T00:00:00+00:00")
        assert db.reconcile_interrupted(conn, "extract", now="x", by_job="job_new") == []
        assert db.get_job(conn, "job_live")["status"] == "running"
        db.update_job(conn, "job_live", status="succeeded")
    finally:
        conn.close()
    with pytest.raises(ValueError):
        db.reconcile_interrupted(db.connect(root / "db" / "jobs.sqlite"), "nope", now="x",
                                 by_job="y")


def test_no_reconciliation_without_the_guard(tmp_path):
    # Another run holds the extract guard: this one refuses before touching any job row.
    root = _vault(tmp_path)
    _stale_row(root, "job_maybe_alive")
    held, release = threading.Event(), threading.Event()

    def hold():
        with run_guard.guard(root / "db", "extract"):
            held.set()
            release.wait(10)
    t = threading.Thread(target=hold)
    t.start()
    held.wait(10)
    try:
        with pytest.raises(run_guard.ProducerBusy):
            extract.extract_sources(root, jobs_db=root / "db" / "jobs.sqlite")
        assert _job(root, "job_maybe_alive")["status"] == "running"
    finally:
        release.set()
        t.join()


_KILLED_RUN = r"""
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from app.workers import extract, extract_runner
root, stub, out = (Path(a) for a in sys.argv[2:5])
extract_runner.child_command = lambda: [sys.executable, "-I", str(stub), "stall", str(out)]
extract.extract_sources(root, jobs_db=root / "db" / "jobs.sqlite", timeout_s=120)
"""


def test_a_hard_killed_run_is_reconciled_and_its_document_extracted_next_time(tmp_path):
    root = _vault(tmp_path)
    driver = subprocess.Popen([sys.executable, "-c", _KILLED_RUN, str(ROOT), str(root),
                               str(STUB), str(tmp_path)])
    try:
        child = _mark(tmp_path, "stalled")
        driver.kill()                                                # no chance to record it
        driver.wait()
        assert _gone(child), "the watchdog terminates the orphaned child"
        conn = db.connect(root / "db" / "jobs.sqlite")
        (killed,) = [j for j in db.list_jobs(conn) if j["job_type"] == "extract"]
        conn.close()
        assert killed["status"] == "running"                          # lost execution
        out = extract.extract_sources(root, jobs_db=root / "db" / "jobs.sqlite")
        assert [j["job_id"] for j in out["interrupted_jobs"]] == [killed["job_id"]]
        assert _job(root, killed["job_id"])["error_message"] == db.INTERRUPTED_MESSAGE
        assert out["extracted"] == 1 and out["status"] == "succeeded"
    finally:
        if driver.poll() is None:
            driver.kill()
            driver.wait()


# --- decision 7.5: cancellation ------------------------------------------------------------


def test_cancelling_abandons_the_parse_in_progress_and_records_nothing_for_it(tmp_path,
                                                                             monkeypatch):
    root = _vault(tmp_path, n=2)
    monkeypatch.setattr(extract_runner, "child_command", lambda: _stub("stall", tmp_path))
    run = _Background(root)
    child = _mark(tmp_path, "stalled")
    (job_id,) = extract.active_runs()
    started = time.monotonic()
    assert extract.cancel() == [job_id]
    out = run.join()
    assert time.monotonic() - started < extract_runner.KILL_GRACE_S + 5
    assert out["cancelled"] is True and out["cancel_reason"] == "cancelled"
    assert out["status"] == "failed" and out["errors"] == 0
    assert out["not_attempted"] == 2 and out["extracted"] == 0
    assert _gone(child, within=0)
    job = _job(root, job_id)
    assert job["status"] == "failed" and job["error_message"] == "interrupted (cancelled)"
    for sid in _sids(root):                                          # nothing recorded for them
        m = manifests.get_manifest(root / "raw" / "manifests", sid)
        assert m["ingestion_status"] not in ("error", "extracted")
        assert not (root / "normalized" / "extraction_logs" / f"{sid}.json").exists()
        assert extract_attempts.read(root / "normalized", sid).state == "absent"
    assert not list((root / "normalized" / ".staging").glob("*"))
    assert extract.active_runs() == [] and not _guard_busy(root)
    monkeypatch.undo()
    assert extract.extract_sources(root, jobs_db=root / "db" / "jobs.sqlite")["extracted"] == 2


def test_the_run_keeps_its_guard_and_registration_until_its_cleanup_completes(tmp_path,
                                                                            monkeypatch):
    # The child ignores SIGTERM: cleanup takes the whole grace, and only then is the run gone.
    root = _vault(tmp_path)
    monkeypatch.setattr(extract_runner, "child_command",
                        lambda: _stub("stall_ignore_term", tmp_path))
    run = _Background(root)
    child = _mark(tmp_path, "stalled")
    extract.cancel()
    time.sleep(1.0)
    assert run.thread.is_alive() and extract.active_runs() != []      # still cleaning up ...
    assert _guard_busy(root)                                         # ... and holding the guard
    assert not _gone(child, within=0)
    out = run.join()
    assert out["cancelled"] is True and _gone(child, within=0)
    assert extract.active_runs() == [] and not _guard_busy(root)


def test_a_cancel_before_an_install_starts_installs_nothing(tmp_path, monkeypatch):
    root = _vault(tmp_path)
    real = extract._parse

    def parse_then_cancel(path, sid, **kw):
        out = real(path, sid, **kw)
        extract.cancel()                                             # the parse completed
        return out
    monkeypatch.setattr(extract, "_parse", parse_then_cancel)
    out = extract.extract_sources(root, jobs_db=root / "db" / "jobs.sqlite")
    assert out["cancelled"] is True and out["not_attempted"] == 1 and out["extracted"] == 0
    (sid,) = _sids(root)
    assert manifests.get_manifest(root / "raw" / "manifests", sid)["ingestion_status"] != "extracted"
    assert not (root / "normalized" / "markdown" / f"{sid}.md").exists()
    assert xi.read_journal(xi.Layout(root / "normalized"), sid).state == xi.ABSENT
    assert not list((root / "normalized" / ".staging").glob("*"))


def test_an_install_that_has_begun_finishes_and_the_next_document_waits(tmp_path, monkeypatch):
    root = _vault(tmp_path, n=2)
    real = xi.install
    calls = []

    def install_then_cancel(*a, **kw):
        calls.append(1)
        extract.cancel()                                             # mid-install
        return real(*a, **kw)
    monkeypatch.setattr(xi, "install", install_then_cancel)
    out = extract.extract_sources(root, jobs_db=root / "db" / "jobs.sqlite")
    assert len(calls) == 1 and out["cancelled"] is True
    assert out["extracted"] == 1 and out["not_attempted"] == 1       # between documents
    done = [s for s in _sids(root) if manifests.get_manifest(
        root / "raw" / "manifests", s)["ingestion_status"] == "extracted"]
    assert len(done) == 1
    assert xi.read_journal(xi.Layout(root / "normalized"), done[0]).state == xi.ABSENT  # closed
    assert (root / "wiki" / "Sources" / f"{done[0]}.md").exists()   # published


def test_a_cancel_while_waiting_for_the_write_lock_installs_nothing(tmp_path, monkeypatch):
    # The parse completes; another writer holds the write lock; the cancel comes while the run
    # waits for it. Acquiring the lock is the install boundary: the cancel is honoured there.
    root = _vault(tmp_path)
    from app.backend import write_lock
    held, release, waiting = threading.Event(), threading.Event(), threading.Event()

    def holder():
        with real_hold(root / "reviews"):                            # not counted below
            held.set()
            release.wait(20)
    real_parse = extract._parse

    def parse_then_block(path, sid, **kw):
        out = real_parse(path, sid, **kw)
        threading.Thread(target=holder, daemon=True).start()
        assert held.wait(10)
        return out
    monkeypatch.setattr(extract, "_parse", parse_then_block)
    real_hold, holds = write_lock.hold, []

    def watched_hold(*a, **kw):
        holds.append(1)
        if len(holds) == 2:                                          # 1: recovery, 2: the commit
            waiting.set()
        return real_hold(*a, **kw)
    monkeypatch.setattr(extract.write_lock, "hold", watched_hold)
    run = _Background(root)
    assert waiting.wait(20)
    time.sleep(0.3)                                                  # blocked on the lock
    (job_id,) = extract.active_runs()
    assert extract.cancel() == [job_id]
    release.set()
    out = run.join()
    assert out["cancelled"] is True and out["not_attempted"] == 1
    assert out["extracted"] == 0 and out["errors"] == 0 and out["status"] == "failed"
    (sid,) = _sids(root)
    assert manifests.get_manifest(root / "raw" / "manifests", sid)["ingestion_status"] != "extracted"
    assert not (root / "normalized" / "markdown" / f"{sid}.md").exists()
    assert not (root / "normalized" / "extraction_logs" / f"{sid}.json").exists()
    assert xi.read_journal(xi.Layout(root / "normalized"), sid).state == xi.ABSENT
    assert extract_attempts.read(root / "normalized", sid).state == "absent"
    assert not list((root / "normalized" / ".staging").glob("*"))
    assert _job(root, job_id)["error_message"] == "interrupted (cancelled)"
    monkeypatch.undo()
    assert extract.extract_sources(root, jobs_db=root / "db" / "jobs.sqlite")["extracted"] == 1


def _assert_cancelled_after_installing_all(root: Path, out: dict) -> None:
    assert out["cancelled"] is True and out["cancel_reason"] == "cancelled"
    assert out["extracted"] == 1 and out["not_attempted"] == 0       # nothing left to reach
    assert out["status"] == "failed"
    job = _job(root, out["job_id"])
    assert job["status"] == "failed" and job["error_message"] == "interrupted (cancelled)"
    (sid,) = _sids(root)                                             # the install is kept
    assert manifests.get_manifest(root / "raw" / "manifests", sid)["ingestion_status"] == "extracted"
    assert xi.read_journal(xi.Layout(root / "normalized"), sid).state == xi.ABSENT
    assert (root / "wiki" / "Sources" / f"{sid}.md").exists()


def test_a_cancel_during_the_final_documents_install_is_reported(tmp_path, monkeypatch):
    root = _vault(tmp_path)                                          # one document: no next loop
    real = xi.install

    def install_then_cancel(*a, **kw):
        assert extract.cancel() != []
        return real(*a, **kw)
    monkeypatch.setattr(xi, "install", install_then_cancel)
    _assert_cancelled_after_installing_all(
        root, extract.extract_sources(root, jobs_db=root / "db" / "jobs.sqlite"))


def test_a_cancel_during_the_end_refresh_is_reported_and_one_after_settling_is_refused(
        tmp_path, monkeypatch):
    from app.workers import publication
    root = _vault(tmp_path)
    real_finish, real_update = publication.Run.finish, db.update_job
    late = {}

    def finish_then_cancel(self):
        assert extract.cancel() != []
        return real_finish(self)

    def update_with_a_late_cancel(conn, job_id, **kw):
        if kw.get("finished_at") and "late" not in late:             # recording the outcome
            late["late"] = extract.cancel(reason="too late")
            late["active"] = extract.active_runs()
        return real_update(conn, job_id, **kw)
    monkeypatch.setattr(publication.Run, "finish", finish_then_cancel)
    monkeypatch.setattr(db, "update_job", update_with_a_late_cancel)
    out = extract.extract_sources(root, jobs_db=root / "db" / "jobs.sqlite")
    _assert_cancelled_after_installing_all(root, out)
    assert late == {"late": [], "active": [out["job_id"]]}           # refused, still waited for
    assert extract.active_runs() == []


def test_a_cancel_after_settling_is_refused_and_the_run_reports_success(tmp_path, monkeypatch):
    root = _vault(tmp_path)
    real_update, late = db.update_job, {}

    def update_with_a_late_cancel(conn, job_id, **kw):
        if kw.get("finished_at") and "late" not in late:
            late["late"] = extract.cancel()
        return real_update(conn, job_id, **kw)
    monkeypatch.setattr(db, "update_job", update_with_a_late_cancel)
    out = extract.extract_sources(root, jobs_db=root / "db" / "jobs.sqlite")
    assert late == {"late": []}
    assert out["cancelled"] is False and out["status"] == "succeeded"
    assert _job(root, out["job_id"])["status"] == "succeeded"


def test_a_cancel_during_start_up_recovery_reaches_no_document(tmp_path, monkeypatch):
    # Recovery (an install) finishes; then not even an unchanged document is looked at.
    root = _vault(tmp_path, n=2)
    assert extract.extract_sources(root, jobs_db=root / "db" / "jobs.sqlite")["extracted"] == 2
    real = xi.recover

    def recover_then_cancel(*a, **kw):
        extract.cancel()
        return real(*a, **kw)
    monkeypatch.setattr(xi, "recover", recover_then_cancel)
    out = extract.extract_sources(root, jobs_db=root / "db" / "jobs.sqlite")
    assert out["cancelled"] is True and out["not_attempted"] == 2
    assert out["skipped_unchanged"] == 0 and out["sources_considered"] == 0


def test_cancel_with_nothing_running_and_after_a_run(tmp_path):
    root = _vault(tmp_path)
    assert extract.cancel() == [] and extract.wait_idle(0.1)
    out = extract.extract_sources(root, jobs_db=root / "db" / "jobs.sqlite")
    assert out["cancelled"] is False and out["not_attempted"] == 0
    assert extract.cancel() == []                                    # a finished run is gone


def test_a_cancelled_token_stops_a_parse_before_it_spawns(tmp_path):
    token = extract_runner.CancelToken()
    token.cancel("cancelled")
    with pytest.raises(extract_runner.Cancelled):
        extract_runner.parse(fixtures.write_markdown(tmp_path / "a.md"), "src_0123456789abcdef",
                             timeout_s=30, cancel=token)
    token.close()
    token.cancel("again")                                            # harmless once closed
    token.close()


# --- the API, a server shutdown, the CLI ---------------------------------------------------


def _client(root: Path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.backend import main as main_module
    from app.backend.config import get_settings
    monkeypatch.setattr(main_module, "settings", get_settings(root))
    return TestClient, main_module


def test_the_api_cancels_a_running_extraction(tmp_path, monkeypatch):
    root = _vault(tmp_path)
    TestClient, main_module = _client(root, monkeypatch)
    monkeypatch.setattr(extract_runner, "child_command", lambda: _stub("stall", tmp_path))
    client = TestClient(main_module.app)
    assert client.post("/jobs/extract/cancel").json() == {"cancelled": [],
                                                          "status": "not_running"}
    box = {}
    t = threading.Thread(target=lambda: box.update(r=TestClient(main_module.app).post(
        "/jobs/extract").json()))
    t.start()
    child = _mark(tmp_path, "stalled")
    (job_id,) = extract.active_runs()
    assert client.post("/jobs/extract/cancel").json() == {"cancelled": [job_id],
                                                          "status": "cancelling"}
    t.join(30)
    assert box["r"]["cancelled"] is True and box["r"]["status"] == "failed"
    assert _gone(child, within=0)
    assert client.get(f"/jobs/{job_id}").json()["error_message"] == "interrupted (cancelled)"


def test_a_server_shutdown_cancels_the_run_and_waits_for_its_cleanup(tmp_path, monkeypatch):
    root = _vault(tmp_path)
    TestClient, main_module = _client(root, monkeypatch)
    monkeypatch.setattr(extract_runner, "child_command", lambda: _stub("stall", tmp_path))
    run = _Background(root)
    child = _mark(tmp_path, "stalled")
    with TestClient(main_module.app):
        pass                                                         # startup, then shutdown
    assert extract.active_runs() == []                               # it waited for the run
    out = run.join(0.5)
    assert out["cancelled"] is True and out["cancel_reason"] == "shutdown"
    assert _gone(child, within=0)


def test_a_server_shutdown_still_waits_for_a_run_past_its_settle_point(tmp_path, monkeypatch):
    # Refused by cancel (its outcome is being recorded), yet still active: waited for.
    root = _vault(tmp_path)
    TestClient, main_module = _client(root, monkeypatch)
    token = extract_runner.CancelToken()
    with extract._ACTIVE_LOCK:
        extract._ACTIVE["job_settling"] = token
        extract._SETTLED.add("job_settling")

    def finish_later():
        time.sleep(0.6)
        with extract._IDLE:
            extract._ACTIVE.pop("job_settling", None)
            extract._SETTLED.discard("job_settling")
            extract._IDLE.notify_all()
    t = threading.Thread(target=finish_later)
    try:
        t.start()
        started = time.monotonic()
        with TestClient(main_module.app):
            pass
        assert time.monotonic() - started >= 0.5 and extract.active_runs() == []
        assert not token.cancelled
    finally:
        t.join()
        token.close()


def test_a_shutdown_signal_cancels_first_then_does_what_it_did(monkeypatch):
    seen = []
    prev = signal.signal(signal.SIGUSR1, lambda s, f: seen.append(("server", s)))
    token = extract_runner.CancelToken()
    with extract._ACTIVE_LOCK:
        extract._ACTIVE["job_fake"] = token
    try:
        restore = extract.chain_shutdown_cancel((signal.SIGUSR1,))
        signal.raise_signal(signal.SIGUSR1)
        assert token.cancelled and token.reason == "shutdown"
        assert seen == [("server", signal.SIGUSR1)]
        restore()
        signal.raise_signal(signal.SIGUSR1)
        assert seen == [("server", signal.SIGUSR1)] * 2              # the server's own again
    finally:
        with extract._ACTIVE_LOCK:
            extract._ACTIVE.pop("job_fake", None)
        token.close()
        signal.signal(signal.SIGUSR1, prev)
    out = {}
    t = threading.Thread(target=lambda: out.update(r=extract.chain_shutdown_cancel()))
    t.start()
    t.join()
    out["r"]()                                                       # off the main thread: no-op


def _cli(root: Path, tmp_path: Path, mode: str) -> subprocess.Popen:
    code = (f"import sys; sys.path.insert(0, {str(ROOT)!r})\n"
            "from app.workers import extract_runner\n"
            f"extract_runner.child_command = lambda: [sys.executable, '-I', {str(STUB)!r}, "
            f"{mode!r}, {str(tmp_path)!r}]\n"
            "import runpy\n"
            "sys.argv = ['extract_sources.py']\n"
            f"runpy.run_path({str(ROOT / 'scripts' / 'extract_sources.py')!r}, "
            "run_name='__main__')\n")
    env = {**os.environ, "KNOWLEDGE_SYSTEM_HOME": str(root)}
    return subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def test_the_cli_stops_cleanly_on_the_first_ctrl_c(tmp_path):
    root = _vault(tmp_path)
    proc = _cli(root, tmp_path, "stall")
    try:
        child = _mark(tmp_path, "stalled", within=60)
        proc.send_signal(signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=30)
        assert proc.returncode == 130, (stdout, stderr)
        assert "Stopping after the current step" in stderr
        assert "Extraction cancelled (interrupted by SIGINT): 1 document(s) not attempted." in stdout
        assert _gone(child, within=0)
        conn = db.connect(root / "db" / "jobs.sqlite")
        (job,) = [j for j in db.list_jobs(conn) if j["job_type"] == "extract"]
        conn.close()
        assert job["status"] == "failed"
        assert job["error_message"] == "interrupted (interrupted by SIGINT)"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def test_a_second_ctrl_c_aborts_at_once(tmp_path):
    root = _vault(tmp_path)
    proc = _cli(root, tmp_path, "stall_ignore_term")             # cleanup would take the grace
    try:
        child = _mark(tmp_path, "stalled", within=60)
        proc.send_signal(signal.SIGINT)
        time.sleep(0.5)
        proc.send_signal(signal.SIGINT)
        _stdout, stderr = proc.communicate(timeout=30)
        assert proc.returncode != 0 and "KeyboardInterrupt" in stderr
        assert _gone(child, within=10)                           # the cleanup still ran
        conn = db.connect(root / "db" / "jobs.sqlite")
        (job,) = [j for j in db.list_jobs(conn) if j["job_type"] == "extract"]
        conn.close()
        assert job["status"] == "failed"
        assert job["error_message"] == "interrupted (KeyboardInterrupt)"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def test_the_clis_first_ctrl_c_while_the_run_is_settling_does_not_abort(capsys):
    # Past the settle point a cancel is refused; the CLI's first signal then only says the run is
    # finishing (it never interrupts the outcome being recorded); a second one still aborts.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "extract_sources_cli", ROOT / "scripts" / "extract_sources.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    token = extract_runner.CancelToken()
    with extract._ACTIVE_LOCK:
        extract._ACTIVE["job_settling"] = token
        extract._SETTLED.add("job_settling")
    restore = cli._cooperative_signals()
    try:
        signal.raise_signal(signal.SIGINT)                           # refused, not an abort
        assert not token.cancelled
        assert "The run is finishing" in capsys.readouterr().err
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGINT)
    finally:
        restore()
        with extract._ACTIVE_LOCK:
            extract._ACTIVE.pop("job_settling", None)
            extract._SETTLED.discard("job_settling")
        token.close()
    restore = cli._cooperative_signals()
    try:
        with pytest.raises(KeyboardInterrupt):                        # no run at all: aborts
            signal.raise_signal(signal.SIGINT)
    finally:
        restore()
