"""ADR-0074 slice 6a — the extraction child (decisions 5, 7.1 and 7.2).

The parse runs in a spawned child under a wall-clock deadline that holds off the main thread too;
process and protocol failures get their own reasons while the child's own are preserved; the result
channel is drained and bounded; nothing of the child outlives a parse; the child inherits no lock or
database descriptor; and a watchdog terminates the child's group when the parent dies.
`tests/extract_child_stub.py` plays the misbehaving child.
"""
from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import manifests, run_guard, vault_lock  # noqa: E402
from app.workers import extract, extract_parse, extract_runner as runner, intake  # noqa: E402
from app.workers.extract_parse import ExtractionError  # noqa: E402
from tests import fixtures  # noqa: E402

STUB = ROOT / "tests" / "extract_child_stub.py"
SID = "src_0123456789abcdef"


def _stub(mode: str, out: Path) -> list[str]:
    return [sys.executable, "-I", str(STUB), mode, str(out)]


def _mark(out: Path, name: str, within: float = 10.0):
    end = time.monotonic() + within
    while time.monotonic() < end:
        if (out / name).exists():
            return json.loads((out / name).read_text())
        time.sleep(0.02)
    raise AssertionError(f"no {name} marker")


def _gone(pid: int, within: float = 5.0) -> bool:
    """Whether `pid` has exited (gone, or a zombie awaiting its new reaper) within `within`."""
    end = time.monotonic() + within
    while True:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            return True
        if state in ("Z", "X"):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.02)


def _children(ppid: int, needle: str) -> list[int]:
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        except OSError:
            continue
        fields = stat.rsplit(")", 1)[1].split()
        if int(fields[1]) == ppid and needle in cmd and fields[0] not in ("Z", "X"):
            out.append(int(entry.name))
    return out


def _parse(path: Path, *, mode: str | None = None, out: Path | None = None, **kw):
    command = _stub(mode, out) if mode else kw.pop("command", None)
    kw.setdefault("timeout_s", 30)
    return runner.parse(path, SID, command=command, **kw)


def _reason(path: Path, **kw) -> ExtractionError:
    with pytest.raises(ExtractionError) as info:
        _parse(path, **kw)
    return info.value


@pytest.fixture
def md(tmp_path) -> Path:
    return fixtures.write_markdown(tmp_path / "doc.md")


# --- the real child ------------------------------------------------------------------------


def test_the_child_returns_what_the_in_process_parse_returns(tmp_path):
    files = [fixtures.write_markdown(tmp_path / "a.md"), fixtures.write_html(tmp_path / "b.html"),
             fixtures.write_csv(tmp_path / "c.csv"), fixtures.write_docx(tmp_path / "d.docx"),
             fixtures.write_pdf(tmp_path / "e.pdf", ["first page text", "second page text"])]
    for path in files:
        assert _parse(path) == extract_parse.dispatch(path, SID), path.name


def test_the_child_bootstrap_imports_no_api_database_or_model_code(tmp_path):
    pdf = fixtures.write_pdf(tmp_path / "e.pdf", ["some readable text"])
    _parse(pdf, mode="modules", out=tmp_path)
    loaded = set(_mark(tmp_path, "modules"))
    forbidden = [m for m in loaded if m.startswith(("app.backend", "fastapi", "starlette",
                                                    "uvicorn", "torch", "FlagEmbedding",
                                                    "anthropic", "openai", "sqlite3", "lancedb"))]
    assert forbidden == []
    app = {m for m in loaded if m == "app" or m.startswith("app.")}
    assert app <= {"app", "app.workers", "app.workers.chunking", "app.workers.extract_child",
                   "app.workers.extract_parse", "app.workers.extractors",
                   "app.workers.extractors.pdf"}, sorted(app)
    assert "pypdf" in loaded                      # the format's extractor, imported lazily


def test_the_child_gets_a_minimal_environment_its_own_group_and_no_inherited_locks(
        tmp_path, md, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-for-the-child")
    monkeypatch.setenv("KS_ROOT", str(tmp_path))
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    conn = sqlite3.connect(db_dir / "jobs.sqlite")
    handle = vault_lock.acquire_shared(db_dir)
    try:
        with run_guard.guard(db_dir, "extract"):
            _parse(md, mode="env", out=tmp_path)
    finally:
        vault_lock.release(handle)
        conn.close()
    seen = _mark(tmp_path, "env")
    assert "ANTHROPIC_API_KEY" not in seen["environ"] and "KS_ROOT" not in seen["environ"]
    assert seen["cwd"] == "/" and seen["pgid"] == seen["pid"] != os.getpgrp()
    targets = list(seen["fds"].values())
    assert not [t for t in targets if str(db_dir) in t or t.endswith(".lock")], targets
    # stdin, stdout (redirected to stderr), stderr, the result channel, the listing itself.
    assert len(targets) <= 5, targets


# --- the deadline --------------------------------------------------------------------------


def test_the_deadline_holds_off_the_main_thread_and_the_child_is_gone(tmp_path, md):
    outcome = {}

    def work():
        started = time.monotonic()
        try:
            _parse(md, mode="stall", out=tmp_path, timeout_s=1, grace_s=0.5)
        except ExtractionError as exc:
            outcome["exc"] = exc
        outcome["elapsed"] = time.monotonic() - started

    worker = threading.Thread(target=work)
    worker.start()
    worker.join(15)
    assert not worker.is_alive()
    assert outcome["exc"].skip_reason == runner.TIMEOUT
    assert outcome["elapsed"] < 1 + 0.5 + 3
    assert _gone(_mark(tmp_path, "stalled"), within=0)
    assert _children(os.getpid(), "extract_watchdog") == []


def test_start_up_counts_against_the_deadline(tmp_path, md):
    started = time.monotonic()
    assert _reason(md, mode="slow_start", out=tmp_path, timeout_s=1,
                   grace_s=0.2).skip_reason == runner.TIMEOUT
    assert time.monotonic() - started < 5


def test_termination_escalates_to_sigkill_after_the_grace(tmp_path, md, monkeypatch):
    # The watchdog's own backstop is pushed out of the way: the parent escalates by itself.
    command = runner.watchdog_command
    monkeypatch.setattr(runner, "watchdog_command",
                        lambda fd, pgid, budget, grace: command(fd, pgid, 600, grace))
    started = time.monotonic()
    exc = _reason(md, mode="stall_ignore_term", out=tmp_path, timeout_s=1, grace_s=1)
    assert exc.skip_reason == runner.TIMEOUT
    assert 2 <= time.monotonic() - started < 6        # deadline + the grace TERM was ignored for
    assert _gone(_mark(tmp_path, "stalled"), within=0)


def test_the_deadline_must_be_positive(tmp_path, md):
    with pytest.raises(ValueError):
        runner.parse(md, SID, timeout_s=0)
    with pytest.raises(ValueError):
        extract.extract_sources(tmp_path, timeout_s=0, record_job=False)


def test_sigalrm_is_gone_from_extraction():
    assert not hasattr(extract, "_time_limit")
    for path in (ROOT / "app" / "workers").glob("extract*.py"):
        text = path.read_text()
        assert "signal.SIGALRM" not in text and "signal.alarm(" not in text, path.name


# --- process and protocol failures ---------------------------------------------------------


@pytest.mark.parametrize("mode, detail", [("crash", "exit code 3"),
                                          ("sigkill_self", "killed by SIGKILL"),
                                          ("truncated", "truncated result")])
def test_a_child_that_dies_without_a_result_crashed(tmp_path, md, mode, detail):
    exc = _reason(md, mode=mode, out=tmp_path)
    assert exc.skip_reason == runner.CRASHED and detail in str(exc)


@pytest.mark.parametrize("mode", ["garbage", "two_messages", "bad_schema", "bad_reason"])
def test_output_off_the_schema_is_invalid(tmp_path, md, mode):
    assert _reason(md, mode=mode, out=tmp_path).skip_reason == runner.OUTPUT_INVALID


@pytest.mark.parametrize("mode", ["reason_list", "reason_object", "deep_result"])
def test_malformed_messages_are_invalid_output_and_the_child_is_cleaned_up(tmp_path, md, mode):
    # An unhashable skip_reason and JSON nested too deep to decode are protocol errors, not
    # unclassified failures (a TypeError / RecursionError escaping the decoder).
    exc = _reason(md, mode=mode, out=tmp_path)
    assert exc.skip_reason == runner.OUTPUT_INVALID
    assert _gone(_mark(tmp_path, "pid"), within=0)
    assert not _children(os.getpid(), str(STUB))


def test_a_hello_nested_too_deep_fails_the_handshake(tmp_path, md, monkeypatch):
    # The real limit (1 KiB) is far below the nesting that breaks the decoder; raise it so the
    # decode path itself is exercised.
    monkeypatch.setattr(runner, "HELLO_MAX_BYTES", 1 << 20)
    started = time.monotonic()
    exc = _reason(md, mode="deep_hello", out=tmp_path, grace_s=0.5)
    assert exc.skip_reason == runner.UNAVAILABLE
    assert time.monotonic() - started < 10              # signalled, not waited out
    assert _gone(_mark(tmp_path, "pid"), within=0)
    assert not _children(os.getpid(), str(STUB))


def test_the_decoder_classifies_every_malformed_message(md):
    # Child-controlled values of every JSON type in every position must classify, never raise
    # anything but an ExtractionError with a protocol reason.
    values = [None, True, 0, -1, 1.5, "", "unsupported", [], [[]], {}, {"a": []}]
    shapes = []
    for v in values:
        shapes.append({"v": 1, "type": "error", "skip_reason": v, "message": "m"})
        shapes.append({"v": 1, "type": "error", "skip_reason": None, "message": v})
        shapes.append({"v": v, "type": "result", "extraction": {}})
        shapes.append({"v": 1, "type": v})
        shapes.append({"v": 1, "type": "result", "extraction": v})
        good = {"elements": [{"kind": "prose", "text": "t", "level": 0, "page": None,
                              "table_reference": None, "sheet_reference": None}],
                "tool": "t", "tool_version": "1", "page_count": None, "warnings": [],
                "tables": [], "status": "extracted", "metadata": {}}
        for key in good:
            shapes.append({"v": 1, "type": "result", "extraction": {**good, key: v}})
        for key in good["elements"][0]:
            element = {**good["elements"][0], key: v}
            shapes.append({"v": 1, "type": "result",
                           "extraction": {**good, "elements": [element]}})
        shapes.append({"v": 1, "type": "result",
                       "extraction": {**good, "tables": [[v, "x"]]}})
        shapes.append({"v": 1, "type": "result",
                       "extraction": {**good, "metadata": {"k": v}}})
    child = runner._Child(timeout_s=30, grace_s=0.1, command=[])
    child.closed = True                                  # nothing was started
    for shape in shapes:
        child.buf = bytearray((json.dumps(shape) + "\n").encode())
        try:
            child._decode()
        except ExtractionError as exc:
            assert exc.skip_reason in {runner.OUTPUT_INVALID} | runner.CHILD_REASONS | {None}, shape
            if exc.skip_reason != runner.OUTPUT_INVALID:
                assert shape["type"] == "error" and type(shape["message"]) is str, shape
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"{shape!r} escaped as {exc!r}") from None


def test_receipt_is_bounded_while_the_child_still_writes(tmp_path, md):
    started = time.monotonic()
    exc = _reason(md, mode="flood", out=tmp_path, max_result_bytes=1 << 20, grace_s=0.5)
    assert exc.skip_reason == runner.OUTPUT_INVALID
    assert time.monotonic() - started < 10


def test_a_complete_result_over_the_cap_is_rejected_by_the_child(md):
    exc = _reason(md, max_result_bytes=100)
    assert exc.skip_reason == runner.OUTPUT_TOO_LARGE and "exceeds 100" in str(exc)


def test_a_result_larger_than_the_pipe_is_drained_while_the_child_runs(tmp_path, md):
    out = _parse(md, mode="big_ok", out=tmp_path, timeout_s=20)
    assert len(out.elements[0].text) == 5_000_000


@pytest.mark.parametrize("mode", ["exit_before_hello", "bad_hello"])
def test_a_child_failing_its_handshake_is_unavailable(tmp_path, md, mode):
    started = time.monotonic()
    assert _reason(md, mode=mode, out=tmp_path, grace_s=0.5).skip_reason == runner.UNAVAILABLE
    assert time.monotonic() - started < 10              # the lying child was not waited out


def test_a_child_that_cannot_start_is_unavailable(md):
    exc = _reason(md, command=[str(md.parent / "no-such-python")])
    assert exc.skip_reason == runner.UNAVAILABLE


def test_the_childs_own_reasons_are_preserved(tmp_path):
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"not a zip at all")
    assert _reason(bad).skip_reason == "invalid_archive"
    bomb = tmp_path / "bomb.docx"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"\0" * (8 << 20))
    assert _reason(bomb).skip_reason == "decompression_bomb"
    other = tmp_path / "x.txt"
    other.write_text("text")
    assert _reason(other).skip_reason == "unsupported"
    md = fixtures.write_markdown(tmp_path / "doc.md")
    assert _reason(md, mode="missing_dep", out=tmp_path).skip_reason == "missing_dependency"
    generic = _reason(md, mode="generic_error", out=tmp_path)
    assert generic.skip_reason is None and str(generic) == "RuntimeError: simulated crash"


# --- cleanup -------------------------------------------------------------------------------


def test_a_successful_parse_leaves_no_descendants(tmp_path, md):
    _parse(md, mode="helper", out=tmp_path)
    assert _gone(_mark(tmp_path, "helper"))
    assert _children(os.getpid(), "extract_watchdog") == []
    assert _children(os.getpid(), "extract_child_stub") == []


def test_a_parent_exception_still_cleans_up(tmp_path, md, monkeypatch):
    def interrupted(obj):
        raise KeyboardInterrupt
    monkeypatch.setattr(runner, "decode_extraction", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _parse(md, mode="env", out=tmp_path)
    assert _gone(_mark(tmp_path, "env")["pid"], within=0)
    assert _children(os.getpid(), "extract_watchdog") == []


# --- the watchdog --------------------------------------------------------------------------


def _group() -> subprocess.Popen:
    return subprocess.Popen(["sleep", "60"], process_group=0)


def _watchdog(pgid: int, budget: float, grace: float = 0.3):
    read_end, write_end = os.pipe()
    proc = subprocess.Popen(runner.watchdog_command(read_end, pgid, budget, grace),
                            stdout=subprocess.PIPE, pass_fds=(read_end,))
    os.close(read_end)
    assert proc.stdout.read(1) == runner.READY
    return proc, write_end


def _reaped_gone(proc: subprocess.Popen, within: float) -> bool:
    try:
        proc.wait(timeout=within)
    except subprocess.TimeoutExpired:
        return False
    return True


def test_the_watchdog_terminates_the_group_when_the_parent_end_closes():
    target = _group()
    dog, write_end = _watchdog(target.pid, budget=60)
    os.close(write_end)                                 # what the parent's death looks like
    assert _reaped_gone(target, 5)
    assert dog.wait(5) == 0


def test_the_watchdog_enforces_the_deadline_by_itself():
    target = _group()
    dog, write_end = _watchdog(target.pid, budget=0.5)
    try:
        assert _reaped_gone(target, 5)
    finally:
        os.close(write_end)
        dog.wait(5)


def test_the_watchdog_ignores_an_interrupt_and_terminates_the_group_on_sigterm():
    target = _group()
    dog, write_end = _watchdog(target.pid, budget=60)
    try:
        dog.send_signal(signal.SIGINT)                   # a terminal's Ctrl-C reaches the parent
        time.sleep(0.3)
        assert dog.poll() is None and target.poll() is None
        dog.terminate()
        assert _reaped_gone(target, 5)
        assert dog.wait(5) == 0
    finally:
        os.close(write_end)
        if target.poll() is None:
            target.kill()
            target.wait()


def test_the_watchdog_stands_down_and_never_signals_its_own_group():
    target = _group()
    dog, write_end = _watchdog(target.pid, budget=60)
    os.write(write_end, runner.STAND_DOWN)
    os.close(write_end)
    assert dog.wait(5) == 0
    assert target.poll() is None                         # left alone
    target.kill()
    target.wait()
    read_end, write_end = os.pipe()
    own = subprocess.Popen(runner.watchdog_command(read_end, os.getpgrp(), 0.1, 0.1),
                           pass_fds=(read_end,))
    os.close(read_end)
    assert own.wait(5) == 2                              # refused; this process is still here
    os.close(write_end)


# --- descriptor isolation and orphan termination (decision 7.1 / 7.2) ----------------------

_DRIVER = r"""
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from app.backend import db, run_guard, vault_lock
from app.workers import extract_runner
root, md, stub, out = (Path(a) for a in sys.argv[2:6])
db.init_db(root / "db" / "jobs.sqlite")
conn = db.connect(root / "db" / "jobs.sqlite")
vault_lock.acquire_shared(root / "db")
with run_guard.guard(root / "db", "extract"):
    extract_runner.parse(md, "src_0123456789abcdef", timeout_s=60, grace_s=3,
                         command=[sys.executable, "-I", str(stub), "stall_ignore_term", str(out)])
"""


def _fd_targets(pid: int) -> list[str]:
    out = []
    for fd in Path(f"/proc/{pid}/fd").iterdir():
        try:
            out.append(os.readlink(fd))
        except OSError:
            pass
    return out


def test_a_killed_parent_leaves_no_lock_held_and_its_child_is_terminated(tmp_path, md):
    (tmp_path / "db").mkdir()
    driver = subprocess.Popen([sys.executable, "-c", _DRIVER, str(ROOT), str(tmp_path), str(md),
                               str(STUB), str(tmp_path)])
    try:
        child = _mark(tmp_path, "stalled", within=30)
        dogs = _children(driver.pid, "extract_watchdog")
        assert len(dogs) == 1
        # Neither the child nor its watchdog holds a lock, a producer guard or the database.
        for pid in (child, dogs[0]):
            held = [t for t in _fd_targets(pid) if str(tmp_path / "db") in t]
            assert held == [], (pid, held)
        assert any(str(tmp_path / "db") in t for t in _fd_targets(driver.pid))
        driver.kill()
        driver.wait()
        # The child ignores SIGTERM, so it lives through the watchdog's grace: the locks are
        # re-acquirable while it is alive.
        for name in (vault_lock.LOCK_NAME, run_guard.lock_path(tmp_path / "db", "extract").name):
            with open(tmp_path / "db" / name, "a+") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh, fcntl.LOCK_UN)
        assert not _gone(child, within=0)
        # ... and the watchdog terminates its group within the grace (well inside the deadline).
        assert _gone(child, within=3 + 3)
    finally:
        if driver.poll() is None:
            driver.kill()
            driver.wait()
        for pid in _children(1, "extract_child_stub"):
            os.kill(pid, signal.SIGKILL)


# --- through the extraction run ------------------------------------------------------------


def _vault(tmp_path) -> Path:
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.copy_templates(tmp_path)
    fixtures.write_markdown(inbox / "doc.md")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    return tmp_path


def test_the_api_route_times_out_a_stalled_parse(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.backend import main as main_module
    from app.backend.config import get_settings

    _vault(tmp_path)
    settings = dataclasses.replace(get_settings(tmp_path), extract_timeout_s=1)
    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(runner, "child_command", lambda: _stub("stall", tmp_path))
    started = time.monotonic()
    body = TestClient(main_module.app).post("/jobs/extract").json()   # a worker thread
    assert time.monotonic() - started < 1 + runner.KILL_GRACE_S + 5
    assert body["errors"] == 1 and body["error_details"][0]["skip_reason"] == runner.TIMEOUT
    sid = manifests.scan_manifests(tmp_path / "raw" / "manifests").valid[0]["source_id"]
    assert manifests.get_manifest(tmp_path / "raw" / "manifests", sid)["ingestion_status"] == "error"
    assert _gone(_mark(tmp_path, "stalled"), within=0)


def test_a_process_failure_on_re_extraction_keeps_the_last_good_generation(tmp_path, monkeypatch):
    root = _vault(tmp_path)
    jobs = root / "db" / "jobs.sqlite"
    assert extract.extract_sources(root, jobs_db=jobs)["extracted"] == 1
    sid = manifests.scan_manifests(root / "raw" / "manifests").valid[0]["source_id"]
    md_path = root / "normalized" / "markdown" / f"{sid}.md"
    good = md_path.read_bytes()
    log = (root / "normalized" / "extraction_logs" / f"{sid}.json").read_bytes()
    for mode, reason in (("crash", runner.CRASHED), ("garbage", runner.OUTPUT_INVALID),
                         ("reason_list", runner.OUTPUT_INVALID),
                         ("deep_result", runner.OUTPUT_INVALID)):
        monkeypatch.setattr(runner, "child_command", lambda mode=mode: _stub(mode, tmp_path))
        out = extract.extract_sources(root, jobs_db=jobs, force=True)
        assert out["error_details"][0]["skip_reason"] == reason
        assert out["error_details"][0]["preserved_prior"] is True
        assert md_path.read_bytes() == good
        assert (root / "normalized" / "extraction_logs" / f"{sid}.json").read_bytes() == log
        assert manifests.get_manifest(root / "raw" / "manifests",
                                      sid)["ingestion_status"] == "extracted"
    assert not list((root / "normalized" / ".staging").glob("*"))
