#!/usr/bin/env python3
"""Run one document's parse in an extraction child under a wall-clock deadline (ADR-0074
decisions 5 and 7.1–7.2).

`parse` is the one parse path for the CLI and the API — it does not depend on which thread runs the
job (the former `SIGALRM` limit worked only on the main thread). Per document:

1. **Spawn** `app/workers/extract_child.py` as a fresh interpreter (``python -I``: an exec, never a
   fork of the threaded, CUDA-holding API process) in **its own process group**
   (``process_group=0``), working directory ``/``, a minimal environment (no API keys, no vault
   configuration) and **no inherited descriptors** but its standard streams (``close_fds``; every
   descriptor this process opens is non-inheritable anyway): an orphaned child can't keep the vault
   lock, a producer guard or a database open.
2. **Handshake.** The child reports its pid and process group before any parsing; the parent
   requires the group to be the child's own — never the parent's — and signals only the child's
   pid until it is confirmed.
3. **Watchdog.** `app/workers/extract_watchdog.py` (``python -I -S``) holds the read end of a
   control pipe whose only writer is this process: on EOF (this process died) or the deadline
   (+ grace) it terminates the child's group without depending on the parsing interpreter.
4. **Request.** Only then is the path sent; a child whose parent died before that reads EOF and
   exits without parsing.
5. **Receipt.** The result channel is drained while the child runs and its size bounded during
   receipt; the message is schema-validated JSON (never pickle).
6. **Cleanup on every exit path** — success, timeout, invalid output, a parent exception: the group
   is terminated (SIGTERM, the grace, SIGKILL; a successful child's leftover helpers included), the
   watchdog stood down and reaped, the pipes closed, the child reaped.

**Cancellation** (decision 7.5): a :class:`CancelToken` passed to `parse` wakes every wait (its
self-pipe is polled with the child's descriptors) and raises :class:`Cancelled` — after the same
cleanup, so the child and its group are gone before the call returns. A cancellation is not a
document failure.

The deadline (`timeout_s`, ``EXTRACT_TIMEOUT_S``) is wall-clock time from launch through complete
result receipt — start-up, imports, parsing and serialization count; the termination grace is a
separate, bounded cleanup allowance. Failures raise `ExtractionError`: the child's own reasons are
preserved (`missing_dependency`, `decompression_bomb`, `invalid_archive`, `unsupported`,
`output_too_large`, or None for an unclassified parse failure); process and protocol failures get
`timeout`, `extractor_crashed`, `extractor_output_invalid` or `extractor_unavailable`.
"""
from __future__ import annotations

import json
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from app.workers.chunking import Element
from app.workers.extract_child import PROTOCOL_VERSION
from app.workers.extract_parse import ExtractionError
from app.workers.extractors import Extraction

TIMEOUT = "timeout"
CRASHED = "extractor_crashed"
OUTPUT_INVALID = "extractor_output_invalid"
UNAVAILABLE = "extractor_unavailable"
OUTPUT_TOO_LARGE = "output_too_large"
PROCESS_FAILURES = frozenset({TIMEOUT, CRASHED, OUTPUT_INVALID, UNAVAILABLE})
# The reasons a child may report itself; anything else is invalid output.
CHILD_REASONS = frozenset({"missing_dependency", "decompression_bomb", "invalid_archive",
                           "unsupported", OUTPUT_TOO_LARGE})

MAX_RESULT_BYTES = 256 * 1024 * 1024      # the fixed output cap (the child enforces it exactly)
ENVELOPE_BYTES = 64 * 1024                # receipt slack over the cap before the parent stops
HELLO_MAX_BYTES = 1024
KILL_GRACE_S = 5.0
_READ_BYTES = 1 << 20

_HERE = Path(__file__).resolve().parent
CHILD = _HERE / "extract_child.py"
WATCHDOG = _HERE / "extract_watchdog.py"
STAND_DOWN = b"x"
READY = b"R"
# What the child's environment keeps; nothing else (secrets, vault configuration) reaches it.
_ENV_KEEP = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR")
# The same table-name rule as the install journal (`app/backend/extraction_install.py`).
_TABLE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,127}")
_EXTRACTION_KEYS = frozenset({"elements", "tool", "tool_version", "page_count", "warnings",
                              "tables", "status", "metadata"})
_ELEMENT_KEYS = frozenset({"kind", "text", "level", "page", "table_reference", "sheet_reference"})


class Cancelled(Exception):
    """The run was cancelled while this parse was in progress (ADR-0074 decision 7.5); its child
    has been cleaned up. Not a document failure: nothing is recorded for the document."""


class CancelToken:
    """A run's cancellation flag, safe to set from any thread or a signal handler. A self-pipe
    makes a waiting parse wake at once (`fileno`, polled with the child's descriptors)."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self.reason: str | None = None
        self._r, self._w = os.pipe()                  # non-inheritable (PEP 446)
        os.set_blocking(self._w, False)

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str = "cancelled") -> None:
        if self._event.is_set():
            return
        self.reason = reason
        self._event.set()
        # Not under `_lock`: a signal handler may run while this thread holds it. `_w` is only
        # closed after the run (and its parse) ended, when no wait polls the pipe any more.
        w = self._w
        if w is not None:
            try:
                os.write(w, b"c")
            except OSError:
                pass                                  # full (already woken) or closed

    def fileno(self) -> int | None:
        return self._r

    def close(self) -> None:
        with self._lock:
            for fd in (self._r, self._w):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            self._r = self._w = None


def child_command() -> list[str]:
    return [sys.executable, "-I", str(CHILD)]


def watchdog_command(fd: int, pgid: int, budget: float, grace: float) -> list[str]:
    return [sys.executable, "-I", "-S", str(WATCHDOG), str(fd), str(pgid), f"{budget:.3f}",
            f"{grace:.3f}"]


def child_env() -> dict[str, str]:
    return {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}


def parse(path: Path | str, source_id: str, *, timeout_s: float, grace_s: float = KILL_GRACE_S,
          max_result_bytes: int = MAX_RESULT_BYTES, command: list[str] | None = None,
          cancel: CancelToken | None = None) -> Extraction:
    """Parse one already-confined, size-checked, sha-verified raw file in a child; the
    `Extraction`, or `ExtractionError` — or `Cancelled` when `cancel` is set meanwhile. Nothing of
    the child outlives the call."""
    if not timeout_s > 0:
        raise ValueError(f"the extraction deadline must be positive, not {timeout_s!r}")
    if cancel is not None and cancel.cancelled:
        raise Cancelled(cancel.reason or "cancelled")
    child = _Child(timeout_s=timeout_s, grace_s=grace_s,
                   command=list(command) if command is not None else child_command(),
                   cancel=cancel)
    try:
        return child.run(str(path), source_id, max_result_bytes)
    finally:
        child.close()


class _Child:
    def __init__(self, *, timeout_s: float, grace_s: float, command: list[str],
                 cancel: CancelToken | None = None) -> None:
        self.cancel = cancel
        self.timeout_s = timeout_s
        self.grace = grace_s
        self.command = command
        self.deadline = time.monotonic() + timeout_s          # from launch (start-up counts)
        self.proc: subprocess.Popen | None = None
        self.pgid: int | None = None                           # set only once confirmed
        self.watchdog: subprocess.Popen | None = None
        self.ctrl_w: int | None = None
        self.buf = bytearray()
        self.closed = False

    # -- the run ---------------------------------------------------------------------------

    def run(self, path: str, source_id: str, max_result_bytes: int) -> Extraction:
        try:
            self.proc = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
                close_fds=True, process_group=0, cwd="/", env=child_env())
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise ExtractionError(f"extraction child could not start: {exc}", UNAVAILABLE) from exc
        self._read(self.proc.stdout, until_newline=True, limit=HELLO_MAX_BYTES,
                   over=UNAVAILABLE)
        if b"\n" not in self.buf:
            raise ExtractionError("extraction child exited during start-up (no handshake)",
                                  UNAVAILABLE)
        hello, _, rest = bytes(self.buf).partition(b"\n")
        self.buf = bytearray(rest)
        self._confirm(hello)
        self._arm_watchdog()
        request = {"v": PROTOCOL_VERSION, "type": "parse", "path": path, "source_id": source_id,
                   "max_result_bytes": max_result_bytes}
        try:
            self.proc.stdin.write((json.dumps(request) + "\n").encode("ascii"))
            self.proc.stdin.close()
        except OSError:
            pass                       # it is gone: no result follows, reported as a crash below
        self._read(self.proc.stdout, until_newline=False,
                   limit=max_result_bytes + ENVELOPE_BYTES, over=OUTPUT_INVALID)
        return self._decode()

    def _poller(self, fd: int) -> select.poll:
        poller = select.poll()                     # not select(): fds may be numbered > 1024
        poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
        wake = self.cancel.fileno() if self.cancel is not None else None
        if wake is not None:
            poller.register(wake, select.POLLIN)
        return poller

    def _wait(self, poller: select.poll, fd: int, remaining: float) -> bool:
        """Wait for `fd` within `remaining` seconds; raise `Cancelled` once the run is cancelled
        (the self-pipe wakes the wait at once)."""
        events = poller.poll(max(1, int(remaining * 1000)))
        if self.cancel is not None and self.cancel.cancelled:
            raise Cancelled(self.cancel.reason or "cancelled")
        return any(f == fd for f, _ in events)

    def _expired(self) -> ExtractionError:
        return ExtractionError(f"extraction exceeded {self.timeout_s:g}s", TIMEOUT)

    def _read(self, stream, *, until_newline: bool, limit: int, over: str) -> None:
        """Drain `stream` into the buffer until a newline (the handshake) or EOF, within the
        deadline and `limit` bytes."""
        fd = stream.fileno()
        poller = self._poller(fd)
        while not (until_newline and b"\n" in self.buf):
            if len(self.buf) > limit:
                raise ExtractionError(f"extraction child sent more than {limit} bytes", over)
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise self._expired()
            if not self._wait(poller, fd, remaining):
                continue
            chunk = os.read(fd, _READ_BYTES)
            if not chunk:
                return
            self.buf += chunk

    def _confirm(self, line: bytes) -> None:
        try:
            hello = json.loads(line)
        except (ValueError, RecursionError):  # RecursionError: nesting too deep to decode
            hello = None
        pid = self.proc.pid
        if (not isinstance(hello, dict) or hello.get("v") != PROTOCOL_VERSION
                or hello.get("type") != "hello" or hello.get("pid") != pid
                or hello.get("pgid") != pid or pid == os.getpgrp()):
            raise ExtractionError("extraction child failed its start-up handshake", UNAVAILABLE)
        self.pgid = pid

    def _arm_watchdog(self) -> None:
        read_end, write_end = os.pipe()
        try:
            budget = max(self.deadline - time.monotonic(), 0.0) + self.grace
            try:
                self.watchdog = subprocess.Popen(
                    watchdog_command(read_end, self.pgid, budget, self.grace),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=None,
                    close_fds=True, pass_fds=(read_end,), cwd="/", env=child_env())
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                os.close(write_end)
                raise ExtractionError(f"extraction watchdog could not start: {exc}",
                                      UNAVAILABLE) from exc
            self.ctrl_w = write_end
        finally:
            os.close(read_end)
        ready = bytearray()
        fd = self.watchdog.stdout.fileno()
        poller = self._poller(fd)
        while not ready:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise self._expired()
            if self._wait(poller, fd, remaining):
                chunk = os.read(fd, 1)
                if not chunk:
                    break
                ready += chunk
        if bytes(ready) != READY:
            raise ExtractionError("extraction watchdog failed to start", UNAVAILABLE)

    def _decode(self) -> Extraction:
        data = bytes(self.buf)
        if not data.endswith(b"\n"):
            if time.monotonic() >= self.deadline:
                raise self._expired()
            code = self.close()
            what = "a truncated result" if data else "no result"
            raise ExtractionError(f"extraction child exited ({_describe(code)}) with {what}",
                                  CRASHED)
        if data.count(b"\n") != 1:
            raise ExtractionError("extraction child sent more than one message", OUTPUT_INVALID)
        try:
            message = json.loads(data)
        except (ValueError, RecursionError) as exc:  # RecursionError: nesting too deep
            raise ExtractionError(f"extraction child sent invalid JSON: {exc}",
                                  OUTPUT_INVALID) from None
        if not isinstance(message, dict) or message.get("v") != PROTOCOL_VERSION:
            raise ExtractionError("extraction child sent an unknown message", OUTPUT_INVALID)
        kind = message.get("type")
        if kind == "error" and set(message) == {"v", "type", "skip_reason", "message"}:
            reason, text = message["skip_reason"], message["message"]
            # The type first: an unhashable reason (a list, an object) must not reach the set.
            if ((reason is None or (type(reason) is str and reason in CHILD_REASONS))
                    and type(text) is str):
                raise ExtractionError(text, reason)
        elif kind == "result" and set(message) == {"v", "type", "extraction"}:
            try:
                return decode_extraction(message["extraction"])
            except ValueError as exc:
                raise ExtractionError(f"extraction child sent an invalid result: {exc}",
                                      OUTPUT_INVALID) from None
        raise ExtractionError("extraction child sent an invalid message", OUTPUT_INVALID)

    # -- cleanup ---------------------------------------------------------------------------

    def close(self) -> int | None:
        """Terminate the child's group, stand the watchdog down, reap both; idempotent. The
        child is reaped last, so its pid (the group id) can't be reused while the watchdog is
        still able to signal it."""
        if self.closed:
            return self.proc.returncode if self.proc is not None else None
        self.closed = True
        proc = self.proc
        try:
            if proc is not None:
                self._terminate(proc)
        except BaseException:
            # The termination itself was interrupted (a second Ctrl-C during the grace): the
            # child may still be alive, so the watchdog must NOT stand down. Kill the group now
            # and close the control pipe without the stand-down byte — its EOF makes the watchdog
            # finish the kill even if this process exits at once. No waiting; the interrupt goes on.
            self._abandon(proc)
            raise
        else:
            try:
                self._stand_down()
            finally:
                if proc is not None:
                    for stream in (proc.stdin, proc.stdout):
                        try:
                            if stream is not None:
                                stream.close()
                        except OSError:
                            pass
                    try:
                        proc.wait(timeout=self.grace)
                    except subprocess.TimeoutExpired:
                        pass
        return proc.returncode if proc is not None else None

    def _signal(self, pid: int, signum: int) -> None:
        try:
            if self.pgid is not None:
                os.killpg(self.pgid, signum)       # the confirmed group: the child and helpers
            else:
                os.kill(pid, signum)               # unconfirmed: the child alone
        except (ProcessLookupError, PermissionError):
            pass

    def _terminate(self, proc: subprocess.Popen) -> None:
        self._signal(proc.pid, signal.SIGTERM)
        end = time.monotonic() + self.grace
        while not _exited(proc.pid) and time.monotonic() < end:
            time.sleep(0.01)
        self._signal(proc.pid, signal.SIGKILL)     # helpers that outlived the leader, or ignored TERM
        end = time.monotonic() + self.grace
        while not _exited(proc.pid) and time.monotonic() < end:
            time.sleep(0.01)

    def _abandon(self, proc: subprocess.Popen | None) -> None:
        try:
            if proc is not None:
                self._signal(proc.pid, signal.SIGKILL)
        finally:
            if self.ctrl_w is not None:
                try:
                    os.close(self.ctrl_w)              # EOF: the watchdog terminates the group
                except OSError:
                    pass
                self.ctrl_w = None

    def _stand_down(self) -> None:
        if self.ctrl_w is not None:
            try:
                os.write(self.ctrl_w, STAND_DOWN)
            except OSError:
                pass
            os.close(self.ctrl_w)
            self.ctrl_w = None
        watchdog = self.watchdog
        if watchdog is None:
            return
        try:
            watchdog.wait(timeout=self.grace + 1)
        except subprocess.TimeoutExpired:
            watchdog.kill()
            watchdog.wait()
        finally:
            if watchdog.stdout is not None:
                watchdog.stdout.close()


def _exited(pid: int) -> bool:
    """Whether the child has exited, without reaping it (``WNOWAIT``: Popen reaps it later)."""
    try:
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True


def _describe(code: int | None) -> str:
    if code is None:
        return "status unknown"
    if code < 0:
        try:
            return f"killed by {signal.Signals(-code).name}"
        except ValueError:
            return f"killed by signal {-code}"
    return f"exit code {code}"


# -- the result schema ---------------------------------------------------------------------


def _check(ok: bool, what: str) -> None:
    if not ok:
        raise ValueError(what)


def _opt(value: Any, kind: type) -> bool:
    return value is None or (type(value) is kind)


def decode_extraction(obj: Any) -> Extraction:
    """Rebuild an `Extraction` from the child's message, refusing anything off-schema."""
    _check(isinstance(obj, dict) and set(obj) == _EXTRACTION_KEYS, "unexpected fields")
    elements = obj["elements"]
    _check(type(elements) is list, "elements")
    out: list[Element] = []
    for e in elements:
        _check(isinstance(e, dict) and set(e) == _ELEMENT_KEYS, "element fields")
        _check(e["kind"] in ("heading", "prose", "table"), "element kind")
        _check(type(e["text"]) is str, "element text")
        _check(type(e["level"]) is int and 0 <= e["level"] <= 6, "element level")
        _check(_opt(e["page"], int) and (e["page"] is None or e["page"] >= 1), "element page")
        _check(_opt(e["table_reference"], str) and _opt(e["sheet_reference"], str),
               "element references")
        out.append(Element(kind=e["kind"], text=e["text"], level=e["level"], page=e["page"],
                           table_reference=e["table_reference"],
                           sheet_reference=e["sheet_reference"]))
    _check(type(obj["tool"]) is str and type(obj["tool_version"]) is str, "tool")
    page_count = obj["page_count"]
    _check(_opt(page_count, int) and (page_count is None or page_count >= 0), "page_count")
    warnings = obj["warnings"]
    _check(type(warnings) is list and all(type(w) is str for w in warnings), "warnings")
    tables = obj["tables"]
    _check(type(tables) is list, "tables")
    names: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for t in tables:
        _check(type(t) is list and len(t) == 2 and type(t[0]) is str and type(t[1]) is str,
               "table")
        _check(_TABLE.fullmatch(t[0]) is not None and t[0] not in names, "table name")
        names.add(t[0])
        pairs.append((t[0], t[1]))
    _check(obj["status"] in ("extracted", "partial"), "status")
    metadata = obj["metadata"]
    _check(isinstance(metadata, dict)
           and all(type(k) is str and type(v) is str for k, v in metadata.items()), "metadata")
    return Extraction(elements=out, tool=obj["tool"], tool_version=obj["tool_version"],
                      page_count=page_count, warnings=list(warnings), tables=pairs,
                      status=obj["status"], metadata=dict(metadata))
