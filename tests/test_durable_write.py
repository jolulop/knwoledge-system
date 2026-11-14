"""ADR-0074 decision 1: the shared durable-write helper, under failure at every step.

A failure before the replacement leaves the destination exactly as it was and no temporary file
behind; a failure of the directory sync after the replacement is `UncertainCompletion` (an
`OSError`) with the new content in place; a create-only write never replaces anything; a hard kill
mid-write leaves the old content and, at most, a hidden temporary file no inventory matches.
New directories are created top-down, each synced into its parent (or removed again); a
replacement keeps the destination's permission bits from before any content is written; and a
create-only write's directory sync runs even when removing its temporary link fails."""
from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.backend import durable_write as dw

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh_process():
    """Each test starts like a new process: no directory established yet."""
    dw._established.clear()
    yield
    dw._established.clear()


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if ".tmp-" in p.name)


def _seed(tmp_path, content="old\n"):
    path = tmp_path / "d" / "rec.json"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    return path


# --- success paths ------------------------------------------------------------------------------


def test_writes_create_parents_and_leave_no_temporary_file(tmp_path):
    path = tmp_path / "a" / "b" / "page.md"
    dw.write_text(path, "Café ✓\n")
    assert path.read_bytes() == "Café ✓\n".encode("utf-8")
    assert _leftovers(path.parent) == []


def test_replace_overwrites_and_json_matches_the_canonical_format(tmp_path):
    path = _seed(tmp_path)
    obj = {"b": "ü", "a": [1, 2]}
    dw.write_json(path, obj)
    assert path.read_text(encoding="utf-8") == json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    dw.write_json(path, obj, indent=1, sort_keys=True, ensure_ascii=True)
    assert path.read_text() == json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


def test_write_stream_copies_more_than_one_chunk(tmp_path):
    data = os.urandom(dw._COPY_CHUNK * 2 + 17)
    path = tmp_path / "big.bin"
    dw.write_stream(path, io.BytesIO(data))
    assert path.read_bytes() == data and _leftovers(tmp_path) == []


def test_mkdir_false_requires_the_parent(tmp_path):
    with pytest.raises(FileNotFoundError):
        dw.write_text(tmp_path / "missing" / "x.json", "{}", mkdir=False)
    assert not (tmp_path / "missing").exists()


def test_new_file_permissions_match_a_plain_write(tmp_path):
    plain = tmp_path / "plain.txt"
    plain.write_text("x")
    durable = tmp_path / "durable.txt"
    dw.write_text(durable, "x")
    assert (durable.stat().st_mode & 0o777) == (plain.stat().st_mode & 0o777)


def test_temporary_names_never_match_an_inventory(tmp_path, monkeypatch):
    seen = []
    real = dw._write_temp

    def spy(tmp, write, mode=None):
        real(tmp, write, mode)
        seen.append(tmp)
        assert tmp.exists()                              # the complete temp file, before the move
        assert tmp not in list(tmp.parent.glob("*.json")) + list(tmp.parent.glob("*.md"))
    monkeypatch.setattr(dw, "_write_temp", spy)
    dw.write_text(tmp_path / "x.json", "{}")
    dw.write_text(tmp_path / "y.md", "# y")
    assert all(t.name.startswith(".") and ".tmp-" in t.name for t in seen)


def test_in_progress_is_scoped_to_the_operation(tmp_path, monkeypatch):
    states = []
    real = dw._write_temp
    monkeypatch.setattr(dw, "_write_temp",
                        lambda tmp, write, mode=None: (states.append(dw.in_progress()),
                                                       real(tmp, write, mode)))
    assert dw.in_progress() is False
    dw.write_text(tmp_path / "x.json", "{}")
    assert states == [True] and dw.in_progress() is False
    monkeypatch.setattr(dw, "_write_temp", lambda tmp, write, mode=None: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        dw.write_text(tmp_path / "x.json", "{}")
    assert dw.in_progress() is False


# --- failure before the replacement: old content intact, nothing left behind --------------------


def _failing_stream(after: int):
    class Src(io.BytesIO):
        def read(self, n=-1):
            if self.tell() >= after:
                raise OSError(5, "I/O error reading the source")
            return super().read(min(n, after - self.tell()))
    return Src(b"n" * (after * 4))


@pytest.mark.parametrize("step", ["write", "file_fsync", "replace", "interrupt"])
def test_a_failure_before_the_replacement_keeps_the_old_content(tmp_path, monkeypatch, step):
    path = _seed(tmp_path)
    if step == "write":
        with pytest.raises(OSError):
            dw.write_stream(path, _failing_stream(10))
    elif step == "file_fsync":
        real = os.fsync
        monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError(5, "EIO")))
        with pytest.raises(OSError):
            dw.write_text(path, "new\n")
        monkeypatch.setattr(os, "fsync", real)
    elif step == "replace":
        monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(OSError(28, "ENOSPC")))
        with pytest.raises(OSError):
            dw.write_text(path, "new\n")
    else:                                               # Ctrl-C half way through the temp file
        def interrupted(fh):
            fh.write(b"ne")
            raise KeyboardInterrupt
        with pytest.raises(KeyboardInterrupt):
            dw._write(path, interrupted, create_only=False, mkdir=True)
    assert path.read_text() == "old\n"
    assert _leftovers(path.parent) == []


# --- failure after the replacement: uncertain completion ---------------------------------------


def test_a_directory_sync_failure_after_replacement_is_uncertain_completion(tmp_path, monkeypatch):
    path = _seed(tmp_path)
    dw.make_dirs(path.parent)                           # established; only the final sync fails
    monkeypatch.setattr(dw, "sync_dir", lambda d: (_ for _ in ()).throw(OSError(5, "EIO")))
    with pytest.raises(dw.UncertainCompletion) as exc:
        dw.write_text(path, "new\n")
    assert isinstance(exc.value, OSError) and exc.value.path == path and exc.value.errno == 5
    assert path.read_text() == "new\n"                  # replaced: the caller must not assume "nothing"
    assert _leftovers(path.parent) == []


def test_a_create_only_directory_sync_failure_is_uncertain_completion_too(tmp_path, monkeypatch):
    path = tmp_path / "new.json"
    dw.make_dirs(tmp_path)
    monkeypatch.setattr(dw, "sync_dir", lambda d: (_ for _ in ()).throw(OSError(5, "EIO")))
    with pytest.raises(dw.UncertainCompletion):
        dw.write_text(path, "x", create_only=True)
    assert path.read_text() == "x" and _leftovers(tmp_path) == []


# --- create-only never replaces -----------------------------------------------------------------


def test_create_only_creates_when_absent(tmp_path):
    path = tmp_path / "copy.json"
    dw.write_bytes(path, b"evidence", create_only=True)
    assert path.read_bytes() == b"evidence" and _leftovers(tmp_path) == []
    assert path.stat().st_nlink == 1                    # the temporary link is gone


@pytest.mark.parametrize("existing", ["file", "dangling_symlink"])
def test_create_only_refuses_an_existing_destination(tmp_path, existing):
    path = tmp_path / "copy.json"
    if existing == "file":
        path.write_text("first")
    else:
        path.symlink_to(tmp_path / "nowhere")
    with pytest.raises(FileExistsError):
        dw.write_bytes(path, b"second", create_only=True)
    if existing == "file":
        assert path.read_text() == "first"
    else:
        assert path.is_symlink() and not path.exists()
    assert _leftovers(tmp_path) == []


def test_create_only_loses_a_race_without_replacing(tmp_path, monkeypatch):
    """The destination appears between the existence check and the link: the link refuses."""
    path = tmp_path / "copy.json"
    real = dw._write_temp

    def racing(tmp, write, mode=None):
        real(tmp, write, mode)
        path.write_text("winner")                        # another writer got there first
    monkeypatch.setattr(dw, "_write_temp", racing)
    with pytest.raises(FileExistsError):
        dw.write_bytes(path, b"loser", create_only=True)
    assert path.read_text() == "winner" and _leftovers(tmp_path) == []


# --- barriers -----------------------------------------------------------------------------------


def test_sync_existing_flushes_the_file_then_its_directory(tmp_path, monkeypatch):
    path = _seed(tmp_path)
    order = []
    real = dw._fsync_path
    monkeypatch.setattr(dw, "_fsync_path", lambda p: (order.append(Path(p)), real(p)))
    dw.sync_existing(path)
    assert order[:2] == [path, path.parent]
    assert order[-1] == tmp_path                         # ...then establishes the chain above it
    order.clear()
    dw.sync_existing(path)
    assert order == [path, path.parent]


@pytest.mark.parametrize("fails", ["file", "dir"])
def test_barrier_failures_propagate(tmp_path, monkeypatch, fails):
    path = _seed(tmp_path)
    target = path if fails == "file" else path.parent
    real = dw._fsync_path

    def flaky(p):
        if Path(p) == target:
            raise OSError(5, "EIO")
        return real(p)
    monkeypatch.setattr(dw, "_fsync_path", flaky)
    with pytest.raises(OSError) as exc:
        dw.sync_existing(path)
    assert not isinstance(exc.value, dw.UncertainCompletion)    # a barrier replaces nothing
    if fails == "dir":
        with pytest.raises(OSError):
            dw.sync_dir(path.parent)


def test_sync_existing_of_a_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        dw.sync_existing(tmp_path / "gone.json")


# --- durable directory creation (review round 1, P1) -----------------------------------------


def _trace(monkeypatch, root: Path, *, fail_sync=None, fail_rmdir=False):
    """Record mkdir / dir-sync / temp-write / replace / link events under `root`, in order."""
    events: list[str] = []
    real_mkdir, real_sync, real_write = os.mkdir, dw.sync_dir, dw._write_temp
    real_replace, real_link, real_rmdir = os.replace, os.link, os.rmdir

    def rel(p):
        p = Path(p)
        return "." if p == root else str(p.relative_to(root))

    def ours(p):
        return Path(p) == root or Path(p).is_relative_to(root)

    def mkdir(p, *a, **k):
        real_mkdir(p, *a, **k)
        events.append(f"mkdir {rel(p)}")

    def sync(d):
        if fail_sync is not None and Path(d) == root / fail_sync:
            events.append(f"sync {rel(d)} FAILS")
            raise OSError(5, "EIO")
        real_sync(d)
        if ours(d):                                  # the chain above the test root is not traced
            events.append(f"sync {rel(d)}")

    def write(tmp, w, mode=None):
        real_write(tmp, w, mode)
        events.append(f"temp {rel(tmp.parent)}")

    def rmdir(p, *a, **k):
        if fail_rmdir:
            raise OSError(16, "EBUSY")
        real_rmdir(p, *a, **k)
        events.append(f"rmdir {rel(p)}")

    monkeypatch.setattr(os, "mkdir", mkdir)
    monkeypatch.setattr(os, "rmdir", rmdir)
    monkeypatch.setattr(dw, "sync_dir", sync)
    monkeypatch.setattr(dw, "_write_temp", write)
    monkeypatch.setattr(os, "replace", lambda a, b: (real_replace(a, b),
                                                      events.append(f"replace {rel(b)}"))[0])
    monkeypatch.setattr(os, "link", lambda a, b: (real_link(a, b),
                                                   events.append(f"link {rel(b)}"))[0])
    return events


def test_new_directories_are_created_top_down_each_synced_into_its_parent(tmp_path, monkeypatch):
    events = _trace(monkeypatch, tmp_path)
    dw.write_text(tmp_path / "a" / "b" / "rec.json", "{}")
    assert events == ["mkdir a", "sync .",            # a's entry is durable before b is created
                      "mkdir a/b", "sync a",
                      "temp a/b", "replace a/b/rec.json", "sync a/b"]


def test_an_existing_directory_is_established_once_per_process(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()                             # visible, but nobody synced its entry
    events = _trace(monkeypatch, tmp_path)
    dw.write_text(tmp_path / "a" / "rec.json", "{}", create_only=True)
    assert events == ["sync .", "temp a", "link a/rec.json", "sync a"]
    events.clear()
    dw.write_text(tmp_path / "a" / "rec.json", "{}")
    dw.make_dirs(tmp_path / "a")
    assert events == ["temp a", "replace a/rec.json", "sync a"]   # established: no repeat


def test_mkdir_false_establishes_the_existing_parent_too(tmp_path, monkeypatch):
    (tmp_path / "a" / "b").mkdir(parents=True)
    events = _trace(monkeypatch, tmp_path)
    dw.write_text(tmp_path / "a" / "b" / "rec.json", "{}", mkdir=False)
    assert events == ["sync .", "sync a", "temp a/b", "replace a/b/rec.json", "sync a/b"]


def test_establishment_stops_at_a_directory_the_user_does_not_own(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    monkeypatch.setattr(dw, "_EUID", -1)                 # as if nothing here were ours
    events = _trace(monkeypatch, tmp_path)
    dw.write_text(tmp_path / "a" / "rec.json", "{}")
    assert events == ["temp a", "replace a/rec.json", "sync a"]


def test_a_failed_establishment_sync_stops_the_write_and_a_retry_repeats_it(tmp_path,
                                                                           monkeypatch):
    (tmp_path / "a").mkdir()
    events = _trace(monkeypatch, tmp_path, fail_sync=".")
    with pytest.raises(OSError) as exc:
        dw.write_text(tmp_path / "a" / "rec.json", "{}")
    assert not isinstance(exc.value, dw.UncertainCompletion)
    assert events == ["sync . FAILS"] and not (tmp_path / "a" / "rec.json").exists()
    monkeypatch.undo()
    events = _trace(monkeypatch, tmp_path)
    dw.write_text(tmp_path / "a" / "rec.json", "{}")
    assert events[0] == "sync ."                         # not recorded as established


_DIES_BEFORE_PARENT_SYNC = """
import os, signal, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from app.backend import durable_write as dw
root = Path(sys.argv[2])
real = dw.sync_dir
def sync(d):
    if Path(d) == root:                     # mkdir(root/a) done; its entry is not synced yet
        os.kill(os.getpid(), signal.SIGKILL)
    real(d)
dw.sync_dir = sync
dw.write_text(root / "a" / "rec.json", "{}")
"""


def test_a_process_death_between_mkdir_and_parent_sync_is_repaired_by_the_retry(tmp_path,
                                                                               monkeypatch):
    proc = subprocess.run([sys.executable, "-c", _DIES_BEFORE_PARENT_SYNC, str(ROOT),
                           str(tmp_path)], timeout=30)
    assert proc.returncode == -signal.SIGKILL
    assert (tmp_path / "a").is_dir() and not (tmp_path / "a" / "rec.json").exists()
    events = _trace(monkeypatch, tmp_path)               # the retry, in another process
    dw.write_text(tmp_path / "a" / "rec.json", "{}")
    assert events == ["sync .", "temp a", "replace a/rec.json", "sync a"]


def test_a_writer_that_sees_a_directory_before_its_creator_syncs_it_syncs_it_itself(tmp_path,
                                                                                   monkeypatch):
    import threading
    created, release = threading.Event(), threading.Event()
    real_sync = dw.sync_dir
    mine: list[str] = []
    creator_name = "creator"

    def sync(d):
        if threading.current_thread().name == creator_name and Path(d) == tmp_path:
            created.set()                                # mkdir(a) done, sync not yet
            assert release.wait(10)
        elif threading.current_thread() is threading.main_thread() \
                and Path(d).is_relative_to(tmp_path):
            mine.append("." if Path(d) == tmp_path else str(Path(d).relative_to(tmp_path)))
        real_sync(d)
    monkeypatch.setattr(dw, "sync_dir", sync)
    creator = threading.Thread(target=dw.make_dirs, args=(tmp_path / "a",), name=creator_name)
    creator.start()
    try:
        assert created.wait(10)
        dw.write_text(tmp_path / "a" / "rec.json", "{}")   # the observer, while the creator waits
        assert mine == [".", "a"]                       # it synced a's entry before its own file
    finally:
        release.set()
        creator.join(10)
    assert not creator.is_alive()


def test_a_retry_after_an_uncertain_directory_creation_establishes_it(tmp_path, monkeypatch):
    _trace(monkeypatch, tmp_path, fail_sync=".", fail_rmdir=True)
    with pytest.raises(dw.UncertainCompletion):
        dw.write_text(tmp_path / "a" / "rec.json", "{}")
    assert (tmp_path / "a").is_dir()                      # left in place, entry unconfirmed
    monkeypatch.undo()
    events = _trace(monkeypatch, tmp_path)
    dw.write_text(tmp_path / "a" / "rec.json", "{}")
    assert events == ["sync .", "temp a", "replace a/rec.json", "sync a"]


def test_make_dirs_creates_a_chain_for_callers(tmp_path, monkeypatch):
    events = _trace(monkeypatch, tmp_path)
    dw.make_dirs(tmp_path / "reviews" / "audit_log")
    assert events == ["mkdir reviews", "sync .", "mkdir reviews/audit_log", "sync reviews"]
    assert (tmp_path / "reviews" / "audit_log").is_dir()


def test_an_unsynced_new_directory_is_removed_and_the_write_not_attempted(tmp_path, monkeypatch):
    events = _trace(monkeypatch, tmp_path, fail_sync=".")
    with pytest.raises(OSError) as exc:
        dw.write_text(tmp_path / "a" / "b" / "rec.json", "{}")
    assert not isinstance(exc.value, dw.UncertainCompletion)     # nothing is left in place
    assert events == ["mkdir a", "sync . FAILS", "rmdir a"]
    assert not (tmp_path / "a").exists()
    monkeypatch.undo()
    events = _trace(monkeypatch, tmp_path)                       # a retry creates *and* syncs it
    dw.write_text(tmp_path / "a" / "b" / "rec.json", "{}")
    assert events[:4] == ["mkdir a", "sync .", "mkdir a/b", "sync a"]


def test_a_mid_chain_sync_failure_keeps_only_the_durable_levels(tmp_path, monkeypatch):
    events = _trace(monkeypatch, tmp_path, fail_sync="a")
    with pytest.raises(OSError):
        dw.make_dirs(tmp_path / "a" / "b" / "c")
    assert events == ["mkdir a", "sync .", "mkdir a/b", "sync a FAILS", "rmdir a/b"]
    assert (tmp_path / "a").is_dir() and not (tmp_path / "a" / "b").exists()


def test_a_new_directory_that_cannot_be_synced_or_removed_is_uncertain(tmp_path, monkeypatch):
    _trace(monkeypatch, tmp_path, fail_sync=".", fail_rmdir=True)
    with pytest.raises(dw.UncertainCompletion) as exc:
        dw.make_dirs(tmp_path / "a")
    assert exc.value.path == tmp_path / "a" and "created a" in str(exc.value)


def test_a_directory_a_concurrent_creator_made_is_still_synced(tmp_path, monkeypatch):
    events = _trace(monkeypatch, tmp_path)
    traced_mkdir = os.mkdir

    def lost_race(p, *a, **k):
        traced_mkdir(p, *a, **k)                         # the other creator wins...
        raise FileExistsError(17, "File exists")         # ...and our mkdir sees it exist
    monkeypatch.setattr(os, "mkdir", lost_race)
    dw.make_dirs(tmp_path / "a")
    assert events == ["mkdir a", "sync ."]


def test_a_file_in_the_way_of_a_directory_fails_before_any_sync(tmp_path, monkeypatch):
    (tmp_path / "a").write_text("not a directory")
    events = _trace(monkeypatch, tmp_path)
    with pytest.raises(FileExistsError):
        dw.write_text(tmp_path / "a" / "rec.json", "{}")
    assert events == [] and (tmp_path / "a").read_text() == "not a directory"


# --- a replacement keeps the destination's permissions (review round 1, P2) ------------------


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o400, 0o755])
@pytest.mark.parametrize("how", ["text", "json", "bytes", "stream"])
def test_a_replacement_keeps_the_existing_permissions(tmp_path, mode, how):
    path = _seed(tmp_path)
    path.chmod(mode)
    {"text": lambda: dw.write_text(path, "new\n"),
     "json": lambda: dw.write_json(path, {"new": 1}),
     "bytes": lambda: dw.write_bytes(path, b"new\n"),
     "stream": lambda: dw.write_stream(path, io.BytesIO(b"new\n"))}[how]()
    assert (path.stat().st_mode & 0o7777) == mode
    assert path.read_bytes() != b"old\n"


def test_permissions_are_restricted_before_any_content_is_written(tmp_path):
    path = _seed(tmp_path)
    path.chmod(0o600)
    seen = []

    def write(fh):
        seen.append(os.fstat(fh.fileno()).st_mode & 0o777)   # the temp file, still empty
        assert fh.tell() == 0
        fh.write(b"secret\n")
    old = os.umask(0)                                    # the default would be 0666 here
    try:
        dw._write(path, write, create_only=False, mkdir=True)
    finally:
        os.umask(old)
    assert seen == [0o600] and (path.stat().st_mode & 0o777) == 0o600


def test_a_replacement_whose_permissions_cannot_be_read_is_not_attempted(tmp_path, monkeypatch):
    path = _seed(tmp_path)
    real_stat = os.stat

    def stat(p, *a, **k):
        if Path(p) == path:
            raise PermissionError(13, "Permission denied")
        return real_stat(p, *a, **k)
    monkeypatch.setattr(os, "stat", stat)
    with pytest.raises(PermissionError):
        dw.write_text(path, "new\n")
    monkeypatch.undo()
    assert path.read_text() == "old\n" and _leftovers(path.parent) == []


def test_replacing_a_dangling_symlink_uses_new_file_permissions(tmp_path):
    path = tmp_path / "rec.json"
    path.symlink_to(tmp_path / "nowhere")
    plain = tmp_path / "plain"
    plain.write_text("x")
    dw.write_text(path, "{}")
    assert not path.is_symlink()
    assert (path.stat().st_mode & 0o777) == (plain.stat().st_mode & 0o777)


# --- create-only: cleanup cannot bypass the barrier (review round 1, P3) ---------------------


def _fail_temp_unlink(monkeypatch):
    real = os.unlink

    def unlink(p, *a, **k):
        if ".tmp-" in Path(p).name:
            raise OSError(1, "Operation not permitted")
        return real(p, *a, **k)
    monkeypatch.setattr(os, "unlink", unlink)


def test_a_temp_cleanup_failure_after_linking_still_syncs_the_directory(tmp_path, monkeypatch,
                                                                         caplog):
    events = _trace(monkeypatch, tmp_path)
    _fail_temp_unlink(monkeypatch)
    path = tmp_path / "copy.json"
    with caplog.at_level("WARNING", logger="app.backend.durable_write"):
        dw.write_bytes(path, b"evidence", create_only=True)     # installed and durable: no error
    assert events == ["temp .", "link copy.json", "sync ."]    # the sync ran after the link
    assert path.read_bytes() == b"evidence"
    left = _leftovers(tmp_path)
    assert len(left) == 1 and (tmp_path / left[0]).stat().st_ino == path.stat().st_ino
    assert "could not remove its temporary link" in caplog.text


def test_a_temp_cleanup_failure_and_a_sync_failure_is_uncertain_completion(tmp_path, monkeypatch):
    _trace(monkeypatch, tmp_path, fail_sync=".")
    _fail_temp_unlink(monkeypatch)
    path = tmp_path / "copy.json"
    with pytest.raises(dw.UncertainCompletion) as exc:
        dw.write_bytes(path, b"evidence", create_only=True)
    assert exc.value.path == path and "created copy.json" in str(exc.value)
    assert any("temporary link" in n for n in exc.value.__notes__)
    assert path.read_bytes() == b"evidence"


# --- a hard kill mid-write ----------------------------------------------------------------------


_KILLED_WRITER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from app.backend import durable_write as dw

class Slow:
    def __init__(self):
        self.n = 0
    def read(self, size):
        self.n += 1
        if self.n == 2:
            print("writing", flush=True)
            time.sleep(60)                  # killed here, half way through the temporary file
        return b"x" * 4096

dw.write_stream(Path(sys.argv[2]), Slow())
"""


def test_a_hard_kill_mid_write_keeps_the_old_content(tmp_path):
    path = _seed(tmp_path, content='{"status": "hidden"}\n')
    proc = subprocess.Popen([sys.executable, "-c", _KILLED_WRITER, str(ROOT), str(path)],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "writing"
        time.sleep(0.1)
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert path.read_text() == '{"status": "hidden"}\n'          # never truncated
    left = _leftovers(path.parent)
    assert len(left) == 1 and left[0].startswith(".rec.json.tmp-")   # hidden, no inventory sees it
    assert [p.name for p in path.parent.glob("*.json")] == ["rec.json"]


# --- durable removal (slice 3b, review round 2) ---------------------------------------------------


def test_remove_unlinks_then_syncs_the_directory(tmp_path, monkeypatch):
    path = _seed(tmp_path)
    seen = []
    real = dw._fsync_path
    monkeypatch.setattr(dw, "_fsync_path",
                        lambda p: (seen.append((Path(p), Path(path).exists())), real(p)))
    assert dw.remove(path) is True
    assert seen == [(path.parent, False)]                 # the sync runs after the unlink
    assert not path.exists()


def test_a_failed_sync_after_a_removal_is_uncertain_completion(tmp_path, monkeypatch):
    path = _seed(tmp_path)

    def fail(_p):
        raise OSError(5, "EIO")
    monkeypatch.setattr(dw, "_fsync_path", fail)
    with pytest.raises(dw.UncertainCompletion) as exc:
        dw.remove(path)
    assert isinstance(exc.value, OSError) and "removed rec.json" in str(exc.value)
    assert not path.exists()


def test_remove_of_an_absent_entry(tmp_path, monkeypatch):
    path = _seed(tmp_path)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        dw.remove(path)
    synced = []
    monkeypatch.setattr(dw, "_fsync_path", lambda p: synced.append(Path(p)))
    # An absence proves nothing durable (an earlier run may have died before its sync): synced.
    assert dw.remove(path, missing_ok=True) is False and synced == [path.parent]
    assert dw.remove(tmp_path / "nowhere" / "x.json", missing_ok=True) is False


def test_remove_dir_rmdirs_then_syncs_the_parent(tmp_path, monkeypatch):
    d = tmp_path / "a" / "b"
    d.mkdir(parents=True)
    synced = []
    monkeypatch.setattr(dw, "_fsync_path", lambda p: synced.append((Path(p), d.exists())))
    dw.remove_dir(d)
    assert synced == [(d.parent, False)]
    (d / "x").parent.mkdir()
    (d / "x").write_text("y")
    with pytest.raises(OSError):                          # not empty: nothing removed
        dw.remove_dir(d)
    assert (d / "x").exists()
