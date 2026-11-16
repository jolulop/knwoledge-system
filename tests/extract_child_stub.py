#!/usr/bin/env python3
"""A misbehaving extraction child for the ADR-0074 slice 6a tests (`tests/test_extract_child.py`).

Run as ``python -I tests/extract_child_stub.py <mode> <out_dir>`` in place of
`app/workers/extract_child.py`. Modes that keep the protocol reuse the real child (`start`) with a
patched dispatch; the others speak it by hand and then break it. Markers (pids, module lists, the
environment) are written to ``<out_dir>`` for the test to inspect.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.workers import extract_child, extract_parse  # noqa: E402

MODE, OUT = sys.argv[1], Path(sys.argv[2])
_DISPATCH = extract_parse.dispatch
DEEP = 200_000          # JSON nesting that makes `json.loads` raise RecursionError (3.12: ~100k)


def _mark(name: str, value) -> None:
    tmp = OUT / f".{name}.tmp"
    tmp.write_text(json.dumps(value))
    tmp.replace(OUT / name)


def _stall(path, sid):
    _mark("stalled", os.getpid())
    while True:
        time.sleep(0.05)


def _handshake() -> int:
    result_fd = os.dup(1)
    os.dup2(2, 1)
    extract_child._send(result_fd, {"v": 1, "type": "hello", "pid": os.getpid(),
                                    "pgid": os.getpgrp()})
    return result_fd


def _raw(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _real(path, sid):
    return _DISPATCH(Path(path), sid)


def main() -> int:
    if MODE == "slow_start":
        time.sleep(60)
    if MODE == "exit_before_hello":
        return 5
    if MODE == "deep_hello":
        # A hello nested too deeply to decode (the test raises the parent's hello limit).
        fd = os.dup(1)
        _mark("pid", os.getpid())
        _raw(fd, b"[" * DEEP + b"\n")
        time.sleep(60)
        return 0
    if MODE == "bad_hello":
        fd = os.dup(1)
        extract_child._send(fd, {"v": 1, "type": "hello", "pid": os.getpid(),
                                 "pgid": os.getpid() + 1})
        time.sleep(60)
        return 0
    patched = {
        "stall": _stall,
        "missing_dep": lambda p, s: (_ for _ in ()).throw(
            extract_parse.MissingExtractionDependency("No module named 'pypdf'")),
        "generic_error": lambda p, s: (_ for _ in ()).throw(RuntimeError("simulated crash")),
    }
    if MODE == "stall_ignore_term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        patched[MODE] = _stall
    if MODE == "helper":
        def _helper(p, s):
            import subprocess
            helper = subprocess.Popen(["sleep", "60"])
            _mark("helper", helper.pid)
            return _real(p, s)
        patched[MODE] = _helper
    if MODE == "big_ok":
        from app.workers.chunking import Element
        from app.workers.extractors import Extraction
        patched[MODE] = lambda p, s: Extraction(
            elements=[Element("prose", "word " * 1_000_000)], tool="stub", tool_version="1")
    if MODE == "modules":
        def _modules(p, s):
            out = _real(p, s)
            _mark("modules", sorted(sys.modules))
            return out
        patched[MODE] = _modules
    if MODE == "env":
        def _env(p, s):
            fds = {}
            for name in os.listdir("/proc/self/fd"):
                try:
                    fds[name] = os.readlink(f"/proc/self/fd/{name}")
                except OSError:
                    pass
            _mark("env", {"environ": sorted(os.environ), "fds": fds, "cwd": os.getcwd(),
                          "pgid": os.getpgrp(), "pid": os.getpid()})
            return _real(p, s)
        patched[MODE] = _env
    if MODE in patched:
        extract_parse.dispatch = lambda path, sid: patched[MODE](path, sid)
        return extract_child.start()
    # Modes that break the protocol after a valid handshake and request.
    fd = _handshake()
    request = extract_child._read_request()
    if request is None:
        return 3
    if MODE == "crash":
        return 3
    if MODE == "sigkill_self":
        os.kill(os.getpid(), signal.SIGKILL)
    if MODE == "truncated":
        _raw(fd, b'{"v": 1, "type": "result", "extr')
        return 0
    if MODE == "garbage":
        _raw(fd, b"this is not json\n")
        return 0
    if MODE == "two_messages":
        extract_child._send(fd, extract_child._error("unsupported", "one"))
        extract_child._send(fd, extract_child._error("unsupported", "two"))
        return 0
    if MODE in ("reason_list", "reason_object"):
        _mark("pid", os.getpid())
        reason = [] if MODE == "reason_list" else {"unsupported": True}
        extract_child._send(fd, {"v": 1, "type": "error", "skip_reason": reason, "message": "x"})
        return 0
    if MODE == "deep_result":
        _mark("pid", os.getpid())
        _raw(fd, b'{"v": 1, "type": "result", "extraction": ' + b"[" * DEEP + b"]" * DEEP
             + b"}\n")
        return 0
    if MODE == "bad_reason":
        extract_child._send(fd, extract_child._error("checksum_mismatch", "forged"))
        return 0
    if MODE == "bad_schema":
        extraction = extract_child.serialize(_real(request["path"], request["source_id"]))
        extraction["elements"][0]["kind"] = "script"
        extract_child._send(fd, {"v": 1, "type": "result", "extraction": extraction})
        return 0
    if MODE == "flood":
        block = b"a" * (1 << 16)
        while True:
            _raw(fd, block)
    raise SystemExit(f"unknown mode {MODE}")


if __name__ == "__main__":
    try:
        code = main()
    except BrokenPipeError:
        code = 4
    extract_child.exit_now(code)
