"""Durable, all-or-nothing writes of file-backed authority (ADR-0074 decision 1).

Every write of file-backed authority — `raw/manifests/`, Item / Claim / Synthesis pages, everything
under `reviews/` (audit records and recovery journals included), `policies/` and the `db/*.json`
authority records — goes through this module, so no reader ever sees a truncated or half-written
authority file:

1. the parent directory chain is **established**: a missing directory is created one level at a
   time, each new entry made durable by an fsync of *its* parent before the next level is created;
   an existing directory is never trusted just because it is visible (its creator may have died
   between `mkdir` and that sync, or still be about to sync) — its parent is fsynced once per
   process before the first write relies on it (:func:`make_dirs`; callers that create authority
   directories themselves use it too, so no ancestor entry is left unsynced);
2. the content goes to a temporary file in the destination's own directory
   (`.<name>.tmp-<hex>`, never matching a `*.json` / `*.md` inventory), created with the existing
   destination's permission bits (so a replacement never widens access), flushed and fsynced;
3. it is moved over the destination with `os.replace` — or, for a **create-only** write, hard-linked
   into place so an existing destination is never replaced;
4. the directory is fsynced — always once the destination is installed, even when removing a
   create-only write's temporary link fails.

A failure in steps 1–3 leaves the destination exactly as it was and removes the temporary file (a
directory whose parent could not be synced is removed again). A failure in step 4 happens **after**
the installation: the new content is in place but may not survive a power loss. That is
:class:`UncertainCompletion` — an ``OSError``, so callers that treat a failed write as a failure keep
doing so — and the caller must handle it safely (its journal, retry or re-sync barrier), never as
success and never as "nothing happened".

Removals of authority go through :func:`remove` / :func:`remove_dir` (unlink or rmdir, then the
directory sync), so a removal reported as done is as durable as a write.

What this module does **not** do: it does not lock (the write lock, ADR-0068), it does not make
several files change together (the multi-file journals do), and it does not decide whether a write
is allowed (creation and ownership checks stay with the caller). It keeps a replaced file's
permission bits but not its owner (the vault is single-user). SQLite, lock files, staging and
projections are outside its scope.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

_COPY_CHUNK = 1 << 20
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
logger = logging.getLogger(__name__)

# Depth of durable-write operations in progress on this thread. The test suite's write guard reads
# it to tell this module's own temp-file / rename work from an unguarded authority write.
_active = threading.local()

# Directories whose entry this process has made durable, by (st_dev, st_ino): it fsynced the parent
# after creating — or after observing — the directory, and every ancestor before it. Visibility is
# no proof (POSIX makes an entry visible before it is durable), so a directory not in here gets its
# parent synced before a write relies on it. Durability, once reached, lasts until the directory is
# removed (a directory this module removes again was never recorded). Directories the current user
# does not own bound the walk: the system never creates those.
_established: set[tuple[int, int]] = set()
_established_lock = threading.Lock()
_EUID = os.geteuid() if hasattr(os, "geteuid") else None


class UncertainCompletion(OSError):
    """The destination was installed (or a directory created), but the directory holding it could
    not be synced: the change may or may not survive a power loss. Raised from the fsync error."""

    def __init__(self, path: Path, cause: OSError, action: str = "replaced") -> None:
        super().__init__(cause.errno, f"{action} {Path(path).name} but its directory could not be "
                                      f"synced ({cause.strerror or cause})")
        self.path = Path(path)


def in_progress() -> bool:
    """Whether this thread is inside a durable-write operation (for the test-suite write guard)."""
    return getattr(_active, "depth", 0) > 0


@contextmanager
def _operation() -> Iterator[None]:
    _active.depth = getattr(_active, "depth", 0) + 1
    try:
        yield
    finally:
        _active.depth -= 1


def _temp_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:12]}")


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_dir(directory: Path) -> None:
    """fsync a directory, so a rename, creation or deletion inside it is durable. Every directory
    sync of this module goes through here (one fault-injection point for tests)."""
    with _operation():
        _fsync_path(Path(directory))


def sync_existing(path: Path) -> None:
    """Repeat an existing file's durability barrier: fsync the file, then its directory, and
    establish the directory chain above it. Recovery calls it before acting on a file an earlier run
    wrote, since that run may have failed after its rename, during the flush."""
    path = Path(path)
    with _operation():
        _fsync_path(path)
        sync_dir(path.parent)
        _establish(path.parent)


def _key(directory: Path) -> tuple[int, int, int]:
    st = os.stat(directory)
    return st.st_dev, st.st_ino, st.st_uid


def _establish(directory: Path) -> None:
    """Make the entries of the existing `directory` and its ancestors durable: walking up until a
    directory this process already established, or one the current user does not own (the system
    never creates those, so their entries are not its to confirm), then fsync each remaining
    directory's parent, top-down, and remember it."""
    pending: list[tuple[Path, tuple[int, int]]] = []
    d = Path(directory)
    while d.parent != d:
        dev, ino, uid = _key(d)
        if (dev, ino) in _established or (_EUID is not None and uid != _EUID):
            break
        pending.append((d, (dev, ino)))
        d = d.parent
    for d, key in reversed(pending):
        sync_dir(d.parent)
        with _established_lock:
            _established.add(key)


def make_dirs(directory: Path) -> None:
    """Create `directory` and any missing ancestors durably, and establish the existing ones.

    Missing levels are created top-down, one at a time, each new entry fsynced into its parent
    before the next level is created. Existing levels are **not** trusted for being visible: the
    first time this process relies on one, its parent is fsynced (see :func:`_establish`), so a
    directory whose creator died between `mkdir` and the sync — or has not synced yet — is made
    durable by whoever uses it next. When a parent cannot be synced, a directory just created is
    removed again and the error raised; if it cannot be removed either, that is
    :class:`UncertainCompletion`, and a retry establishes the leftover directory before using it.
    A directory a concurrent creator made first is synced into its parent all the same."""
    directory = Path(directory)
    with _operation():
        missing: list[Path] = []
        d = directory
        while not os.path.isdir(d):
            missing.append(d)
            if d.parent == d:
                break
            d = d.parent
        else:
            _establish(d)                             # the deepest existing ancestor and above
        for d in reversed(missing):
            try:
                os.mkdir(d)
                created = True
            except FileExistsError:
                if not os.path.isdir(d):
                    raise
                created = False
            try:
                sync_dir(d.parent)
            except OSError as exc:
                if created:
                    try:
                        os.rmdir(d)
                    except OSError:
                        raise UncertainCompletion(d, exc, "created") from exc
                raise
            dev, ino, _uid = _key(d)
            with _established_lock:
                _established.add((dev, ino))


def _existing_mode(path: Path) -> int | None:
    """Permission bits of the file `path` resolves to, or None when there is none (absent, or a
    dangling link — which the replacement then replaces). Any other error propagates: a replacement
    whose permissions cannot be read is not attempted."""
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        return None


def _write_temp(tmp: Path, write: Any, mode: int | None = None) -> None:
    """Create the temporary file — with `mode`'s permission bits applied before any content is
    written, when given — then write and fsync it completely (one fault-injection point for tests)."""
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC, 0o666)
    try:
        if mode is not None:
            os.fchmod(fd, mode)                       # exact bits: never wider (nor narrower)
        fh = os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise
    with fh:
        write(fh)
        fh.flush()
        os.fsync(fh.fileno())


def _commit(tmp: Path, path: Path, create_only: bool) -> None:
    """Install a complete, synced temp file, then sync the directory. Installation and cleanup are
    separate steps: once the destination is installed, the directory sync runs whatever happens to
    the cleanup, and only that sync confirms durability."""
    try:
        if create_only:
            os.link(tmp, path)            # FileExistsError if the destination exists: never replaced
        else:
            os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    leftover: OSError | None = None
    if create_only:
        try:
            os.unlink(tmp)                # the installed link stays; only the temp name goes
        except OSError as exc:
            leftover = exc
    try:
        sync_dir(path.parent)
    except OSError as exc:
        err = UncertainCompletion(path, exc, "created" if create_only else "replaced")
        if leftover is not None:
            err.add_note(f"and its temporary link {tmp.name} could not be removed ({leftover})")
        raise err from exc
    if leftover is not None:
        # Durable and complete; the stray hidden name matches no inventory and holds the same bytes.
        logger.warning("created %s durably but could not remove its temporary link %s: %s",
                       path, tmp.name, leftover)


def _write(path: Path, write: Any, *, create_only: bool, mkdir: bool) -> None:
    path = Path(path)
    with _operation():
        if mkdir:
            make_dirs(path.parent)
        else:
            _establish(path.parent)                   # FileNotFoundError when it is missing
        if create_only:
            if os.path.lexists(path):
                raise FileExistsError(f"{path.name} already exists")
            mode = None
        else:
            mode = _existing_mode(path)
        tmp = _temp_path(path)
        try:
            _write_temp(tmp, write, mode)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        _commit(tmp, path, create_only)


def write_bytes(path: Path, data: bytes, *, create_only: bool = False, mkdir: bool = True) -> None:
    """Durably replace (or, with `create_only`, create) `path` with `data`."""
    _write(path, lambda fh: fh.write(data), create_only=create_only, mkdir=mkdir)


def write_text(path: Path, text: str, *, create_only: bool = False, mkdir: bool = True) -> None:
    """Durably replace (or create) `path` with UTF-8 `text`."""
    write_bytes(path, text.encode("utf-8"), create_only=create_only, mkdir=mkdir)


def write_json(path: Path, obj: Any, *, indent: int | None = 2, ensure_ascii: bool = False,
               sort_keys: bool = False, create_only: bool = False, mkdir: bool = True) -> None:
    """Durably replace (or create) `path` with `obj` as JSON plus a trailing newline."""
    text = json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii, sort_keys=sort_keys) + "\n"
    write_text(path, text, create_only=create_only, mkdir=mkdir)


def write_stream(path: Path, src: IO[bytes], *, create_only: bool = False,
                 mkdir: bool = True) -> None:
    """Durably replace (or create) `path` with the bytes read from `src` (copied in chunks)."""
    def copy(fh: IO[bytes]) -> None:
        while chunk := src.read(_COPY_CHUNK):
            fh.write(chunk)
    _write(path, copy, create_only=create_only, mkdir=mkdir)


def remove(path: Path, *, missing_ok: bool = False) -> bool:
    """Durably remove the authority file `path` (ADR-0074 slice 3b, review round 2): unlink it, then
    fsync its directory, so a removal a caller reports as done survives a power loss. Returns True
    when this call removed an entry, False when it was already absent (with `missing_ok`; else
    ``FileNotFoundError``). An absent entry's directory is synced all the same: an earlier run may
    have removed it and failed before its sync, so the absence proves nothing durable. A failed sync
    after the unlink is :class:`UncertainCompletion` (the entry is gone but may come back)."""
    path = Path(path)
    with _operation():
        try:
            os.unlink(path)
            removed = True
        except FileNotFoundError:
            if not missing_ok:
                raise
            removed = False
        try:
            sync_dir(path.parent)
        except FileNotFoundError:
            if removed or os.path.lexists(path.parent):
                raise
            return False                              # no directory at all: nothing was there
        except OSError as exc:
            if removed:
                raise UncertainCompletion(path, exc, "removed") from exc
            raise
        return removed


def remove_dir(directory: Path) -> None:
    """Durably remove the empty authority directory `directory`: rmdir, then fsync its parent. A
    failed sync after the rmdir is :class:`UncertainCompletion`."""
    directory = Path(directory)
    with _operation():
        st = os.stat(directory)
        os.rmdir(directory)
        with _established_lock:
            _established.discard((st.st_dev, st.st_ino))
        try:
            sync_dir(directory.parent)
        except OSError as exc:
            raise UncertainCompletion(directory, exc, "removed") from exc


def preserve_entry(path: Path, target: Path) -> None:
    """Durably keep the directory entry `path` under the new name `target` as well, without reading
    it (ADR-0074 slice 3c): a hard link to the entry itself — a symbolic link stays a link, an
    unreadable file keeps its bytes and mode — so repair can set aside a damaged record it can't
    copy, while `path` stays in place (a crash before the replacement still finds it damaged).
    Never replaces an existing `target`; a directory can't be preserved this way (``OSError``).
    A failed directory sync after the link is :class:`UncertainCompletion`."""
    path, target = Path(path), Path(target)
    with _operation():
        _establish(target.parent)
        os.link(path, target, follow_symlinks=False)
        try:
            sync_dir(target.parent)
        except OSError as exc:
            raise UncertainCompletion(target, exc, "linked") from exc


def move(src: Path, dst: Path) -> None:
    """Durably rename the file `src` to `dst`, replacing `dst` (ADR-0074 decision 8: an extraction
    install moves a staged, already fsynced file into place, and an obsolete table into holding).
    `dst`'s directory is established first; after the rename both directories are fsynced (the
    entry left one and joined the other). A failed sync after the rename is
    :class:`UncertainCompletion` — the file has moved but the move may not survive a power loss."""
    src, dst = Path(src), Path(dst)
    with _operation():
        _establish(dst.parent)
        os.replace(src, dst)
        try:
            sync_dir(dst.parent)
            if src.parent != dst.parent:
                sync_dir(src.parent)
        except OSError as exc:
            raise UncertainCompletion(dst, exc, "moved") from exc


def remove_tree(directory: Path) -> bool:
    """Durably remove the directory tree `directory` (derived extraction state: a staging or holding
    directory): delete it, then fsync its parent. Returns False when it was already absent (its
    parent is synced all the same: an earlier run may have removed it and failed before the sync).
    A link is refused (``OSError``), never followed. A failed sync after the removal is
    :class:`UncertainCompletion`."""
    directory = Path(directory)
    with _operation():
        try:
            st = os.lstat(directory)
        except FileNotFoundError:
            removed = False
        else:
            if not stat.S_ISDIR(st.st_mode):
                raise NotADirectoryError(f"{directory.name} is not a directory")
            shutil.rmtree(directory)
            with _established_lock:
                _established.discard((st.st_dev, st.st_ino))
            removed = True
        try:
            sync_dir(directory.parent)
        except FileNotFoundError:
            if removed:
                raise
            return False
        except OSError as exc:
            if removed:
                raise UncertainCompletion(directory, exc, "removed") from exc
            raise
        return removed
