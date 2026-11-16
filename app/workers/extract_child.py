#!/usr/bin/env python3
"""The extraction child's entry point (ADR-0074 decision 5): parse one raw file, return the result.

Started by `app/workers/extract_runner.py` as a fresh interpreter (`python -I <this file>`: an exec,
never a fork of the API process), in its own process group, with a minimal environment and no
inherited descriptors beyond its standard streams. It receives the path of one raw file the parent
already confined, size-checked and sha-verified — no vault root, no output paths, no write
responsibilities — and writes nothing but its result. A process boundary does not make the file
system read-only; that is not claimed.

Bootstrap is minimal on purpose: this module imports only the standard library and
`app.workers.extract_parse` (which imports the extractor modules lazily, per format), so nothing
initialises the API, CUDA / embedding models, databases, locks or jobs.

Protocol (newline-delimited JSON, version 1, never pickle), on a private result descriptor — fd 1
is redirected to stderr first, so a parser's stray print can never corrupt the channel:

1. child → parent ``{"v": 1, "type": "hello", "pid": …, "pgid": …}`` before any parsing — the
   start-up handshake confirming the child leads its own process group;
2. parent → child, on stdin, ``{"v": 1, "type": "parse", "path": …, "source_id": …,
   "max_result_bytes": …}`` — sent only once the watchdog is armed; EOF instead means the parent is
   gone and the child exits without parsing;
3. child → parent one ``result`` (``{"v": 1, "type": "result", "extraction": {…}}``) or ``error``
   (``{"v": 1, "type": "error", "skip_reason": …, "message": …}``) message, then exit.
"""
from __future__ import annotations

import json
import os
import sys

PROTOCOL_VERSION = 1
REQUEST_MAX_BYTES = 64 * 1024
MESSAGE_MAX_CHARS = 2000


def _code_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _send(fd: int, message: dict) -> None:
    # ASCII JSON: one line (control characters and newlines are escaped), any str encodable.
    data = (json.dumps(message, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _error(skip_reason: str | None, message: str) -> dict:
    return {"v": PROTOCOL_VERSION, "type": "error", "skip_reason": skip_reason,
            "message": message[:MESSAGE_MAX_CHARS]}


def _read_request() -> dict | None:
    line = sys.stdin.buffer.readline(REQUEST_MAX_BYTES + 1)
    if not line.endswith(b"\n"):
        return None                       # EOF (the parent is gone) or an over-long request
    try:
        request = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if (not isinstance(request, dict) or request.get("v") != PROTOCOL_VERSION
            or request.get("type") != "parse" or not isinstance(request.get("path"), str)
            or not isinstance(request.get("source_id"), str)
            or type(request.get("max_result_bytes")) is not int):
        return None
    return request


def serialize(extraction) -> dict:
    """The protocol form of an `Extraction` (`extract_runner.decode_extraction` is its inverse)."""
    return {
        "elements": [{"kind": e.kind, "text": e.text, "level": e.level, "page": e.page,
                      "table_reference": e.table_reference,
                      "sheet_reference": e.sheet_reference} for e in extraction.elements],
        "tool": extraction.tool,
        "tool_version": extraction.tool_version,
        "page_count": extraction.page_count,
        "warnings": list(extraction.warnings),
        "tables": [[name, text] for name, text in extraction.tables],
        "status": extraction.status,
        "metadata": dict(extraction.metadata),
    }


def run(result_fd: int, parse) -> int:
    """Handshake, one request, one parse, one answer. `parse(path, source_id)` is the dispatch."""
    from app.workers.extract_parse import ExtractionError

    request = _read_request()
    if request is None:
        return 3
    try:
        extraction = parse(request["path"], request["source_id"])
        message = {"v": PROTOCOL_VERSION, "type": "result", "extraction": serialize(extraction)}
        data = (json.dumps(message, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
    except ExtractionError as exc:
        _send(result_fd, _error(exc.skip_reason, str(exc)))
        return 0
    except Exception as exc:  # noqa: BLE001 - any parse failure is reported, the parent classifies
        _send(result_fd, _error(None, f"{type(exc).__name__}: {exc}"))
        return 0
    cap = request["max_result_bytes"]
    if len(data) > cap:
        # A complete, well-formed result that is too large: an explicit, deterministic rejection.
        _send(result_fd, _error("output_too_large",
                                f"extraction result of {len(data)} bytes exceeds {cap}"))
        return 0
    view = memoryview(data)
    while view:
        view = view[os.write(result_fd, view):]
    return 0


def start() -> int:
    """Take the result descriptor, confirm the process group, then serve one request."""
    result_fd = os.dup(1)
    os.dup2(2, 1)                          # stray prints go to stderr, never the result channel
    if os.getpgrp() != os.getpid():
        return 2                           # not leading its own group: never parses
    _send(result_fd, {"v": PROTOCOL_VERSION, "type": "hello", "pid": os.getpid(),
                      "pgid": os.getpgrp()})
    root = _code_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    from pathlib import Path

    from app.workers import extract_parse
    return run(result_fd, lambda path, sid: extract_parse.dispatch(Path(path), sid))


def exit_now(code: int) -> None:
    """Exit without waiting for any thread a parser left behind (`os._exit` after flushing)."""
    try:
        sys.stderr.flush()
    finally:
        os._exit(code)


if __name__ == "__main__":
    try:
        code = start()
    except BaseException:  # noqa: BLE001 - a broken pipe (parent gone) or an interrupt: no result
        code = 4
    exit_now(code)
