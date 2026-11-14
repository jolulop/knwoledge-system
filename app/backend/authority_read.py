"""Typed reads of file-backed authority (ADR-0074 decisions 2 and 4).

The shared mechanics every authority family reads through — manifests, Item / Claim / Synthesis
pages, and the other families as they adopt it. A read has exactly one of four states:

- ``absent`` — **verified**: no entry at the path, and its parent directory exists and is not a
  dangling link. Only this state can ever mean "nothing here", and only where the family's domain
  allows it does absence permit creating the file (decision 4.1);
- ``unreadable`` — an entry exists (or its existence can't be confirmed) but can't be read: a
  permission or I/O error, a symbolic link (never followed — ``symlink``, or ``dangling_symlink``
  when it points nowhere), a directory or other non-regular file, an unavailable parent
  directory. Possibly transient;
- ``corrupt`` — bytes were read but they are not what the family expects (invalid UTF-8 here;
  the family's parser adds its own reasons: bad JSON, no frontmatter, missing durable fields);
- ``ok`` — read and decoded; the family's validation still follows.

``unreadable`` and ``corrupt`` are never absence: never overwritten with defaults, never rebuilt
by a producer, always reported by category (``error`` is an exception class name or a fixed tag —
never file contents). Parsing and required-field validation stay with each family.
"""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

ABSENT = "absent"
UNREADABLE = "unreadable"
CORRUPT = "corrupt"
OK = "ok"
DAMAGED = (UNREADABLE, CORRUPT)

_UNSAFE = re.compile(r"[^\x21-\x7e]")
# O_NOFOLLOW (slice 3b, review round 2): an authority file that is itself a symbolic link is never
# followed — its target may lie outside the vault, and the durable writer would replace the link,
# not the target, so reads and writes would disagree. Directory links on the way are followed (a
# vault on another disk keeps working); only the final component is refused.
_O_READ = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
           | getattr(os, "O_NOFOLLOW", 0))
_CHUNK = 1 << 16


@dataclass(frozen=True)
class FileRead:
    """One authority file read: `state`, the bytes (`data`, only when `ok`), and for a damaged
    read a categorical `error`."""
    state: str
    path: Path
    data: bytes | None = None
    error: str | None = None

    @property
    def damaged(self) -> bool:
        return self.state in DAMAGED


@dataclass(frozen=True)
class TextRead:
    """A :class:`FileRead` decoded as strict UTF-8 (invalid UTF-8 is `corrupt`, never replaced)."""
    state: str
    path: Path
    text: str | None = None
    error: str | None = None

    @property
    def damaged(self) -> bool:
        return self.state in DAMAGED


def lexists(path: Path) -> bool | None:
    """True / False when an entry's existence is known, None when it can't be determined."""
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


def read_file(path: Path) -> FileRead:
    """Read one authority file's bytes. Never raises for the file's state. The open is non-blocking
    (a FIFO can't hang it), never follows a symbolic link at the path itself, and only a regular
    file is read."""
    path = Path(path)
    try:
        fd = os.open(path, _O_READ)
    except FileNotFoundError:
        here = lexists(path)
        if here is not False:                            # a dangling symlink, or can't confirm
            return FileRead(UNREADABLE, path,
                            error="dangling_symlink" if here else "unknown")
        parent = lexists(path.parent)
        if parent is None or (parent and not path.parent.is_dir()):
            return FileRead(UNREADABLE, path, error="directory_unavailable")
        return FileRead(ABSENT, path)
    except OSError as exc:
        if os.path.islink(path):                         # O_NOFOLLOW refused the final link
            return FileRead(UNREADABLE, path,
                            error="symlink" if os.path.exists(path) else "dangling_symlink")
        return FileRead(UNREADABLE, path, error=type(exc).__name__)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return FileRead(UNREADABLE, path, error="not_a_regular_file")
        chunks = []
        while chunk := os.read(fd, _CHUNK):
            chunks.append(chunk)
    except OSError as exc:
        return FileRead(UNREADABLE, path, error=type(exc).__name__)
    finally:
        os.close(fd)
    return FileRead(OK, path, data=b"".join(chunks))


def read_text(path: Path) -> TextRead:
    """:func:`read_file`, decoded as strict UTF-8."""
    read = read_file(path)
    if read.state != OK:
        return TextRead(read.state, read.path, error=read.error)
    try:
        return TextRead(OK, read.path, text=read.data.decode("utf-8"))
    except UnicodeDecodeError:
        return TextRead(CORRUPT, read.path, error="invalid_utf8")


def safe_name(name: str, *, limit: int = 120) -> str:
    """A file name rendered for a report: every non-printable or non-ASCII character escaped and the
    name truncated — a file name in the vault is untrusted input, never echoed raw."""
    shown = _UNSAFE.sub(lambda m: f"\\x{ord(m.group()):02x}" if ord(m.group()) < 256
                        else f"\\u{ord(m.group()):04x}", name)
    return shown[:limit] + "…" if len(shown) > limit else shown


def list_entries(directory: Path, suffix: str) -> tuple[list[Path], str | None]:
    """`(the entries of `directory` ending in `suffix`, sorted; enumeration error or None)`. A
    missing directory is a verified empty listing; one that can't be listed (permission, not a
    directory, a dangling link) is an error — never an empty listing."""
    directory = Path(directory)
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        here = lexists(directory)
        if here is False:
            return [], None
        return [], "dangling_symlink" if here else "unknown"
    except OSError as exc:
        return [], type(exc).__name__
    return [directory / n for n in sorted(names) if n.endswith(suffix)], None


def load_json(path: Path) -> object:
    """Drop-in, non-blocking replacement for ``json.loads(path.read_text())`` in lenient scanners of
    authority (slice 3b, review round 2): ``FileNotFoundError`` for a verified absence, ``OSError``
    for any other non-readable state (a FIFO, a link, a directory — never opened as a stream), and
    ``ValueError`` (``UnicodeDecodeError`` / ``json.JSONDecodeError``) for bad bytes."""
    read = read_file(path)
    if read.state == ABSENT:
        raise FileNotFoundError(str(path))
    if read.state != OK:
        raise OSError(f"{path.name}: {read.state} ({read.error})")
    return json.loads(read.data.decode("utf-8"))
