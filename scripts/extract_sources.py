#!/usr/bin/env python3
"""CLI: extract and normalize catalogued raw sources (Phase 2).

Usage:
    uv run python scripts/extract_sources.py             # extract all pending sources
    uv run python scripts/extract_sources.py --force     # re-extract everything
    uv run python scripts/extract_sources.py <source_id> # extract one source (repeatable)

Requires the optional extraction dependencies: ``uv sync --extra extraction``. No raw
file is modified; writes go only to normalized/, raw/manifests/, and db/jobs.sqlite.
"""
from __future__ import annotations

import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend.config import get_settings
from app.workers import extract, publication


EXIT_CANCELLED = 130


def _cooperative_signals():
    """ADR-0074 decision 7.5: the first Ctrl-C (or SIGTERM) stops the run at its next safe point —
    the parse in progress is abandoned, an install that has begun finishes; a second one aborts at
    once (an interrupted install is completed by the next run). Returns the restore function."""
    count = 0

    def handler(signum, frame):
        nonlocal count
        count += 1
        if count > 1:
            raise KeyboardInterrupt                   # a second signal aborts at once
        if not extract.cancel(reason=f"interrupted by {signal.Signals(signum).name}"):
            if not extract.active_runs():
                raise KeyboardInterrupt               # no run to stop (yet, or any more)
            # The run is past its settle point: it is only recording its outcome.
            print("The run is finishing (press Ctrl-C again to abort at once)...",
                  file=sys.stderr, flush=True)
            return
        print("Stopping after the current step (press Ctrl-C again to abort at once)...",
              file=sys.stderr, flush=True)
    previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGINT, signal.SIGTERM)}
    return lambda: [signal.signal(sig, prev) for sig, prev in previous.items()]


def main(argv: list[str]) -> int:
    force = "--force" in argv
    source_ids = [a for a in argv if not a.startswith("--")]

    settings = get_settings()
    restore = _cooperative_signals()
    try:
        summary = _run(settings, source_ids, force)
    finally:
        restore()
    return _report(summary)


def _run(settings, source_ids: list[str], force: bool) -> dict:
    return extract.extract_sources(
        settings.root,
        source_ids=source_ids or None,
        force=force,
        manifests_dir=settings.manifests_dir,
        jobs_db=settings.jobs_db_path,
        normalized_dir=settings.normalized_dir,
        max_file_mb=settings.extract_max_file_mb,
        timeout_s=settings.extract_timeout_s,
        target_chars=settings.chunk_target_chars,
        max_chars=settings.chunk_max_chars,
    )


def _report(summary: dict) -> int:
    if summary.get("cancelled"):
        print(f"Extraction cancelled ({summary.get('cancel_reason')}): "
              f"{summary['not_attempted']} document(s) not attempted.")
    else:
        print("Extraction complete.")
    print(f"Sources considered: {summary['sources_considered']}")
    print(f"Extracted: {summary['extracted']}")
    print(f"Partial (needs_ocr): {summary['partial']}")
    print(f"Errors: {summary['errors']}")
    print(f"Skipped (unchanged): {summary['skipped_unchanged']}")
    print(f"Skipped (unsupported): {summary['skipped_unsupported']}")
    if summary["known_failures"]:
        print(f"Skipped (known failure — awaits changed inputs or --force): "
              f"{summary['skipped_known_failure']}")
        for known in summary["known_failures"]:
            print(f"  - {known['source_id']}: {known['skip_reason']}")
    rec = summary["install_recovery"]
    if rec["completed"] or rec["unrecoverable"] or rec["blocked"] or rec["problems"]:
        print(f"Interrupted installs completed: {len(rec['completed'])}")
        for sid, why in sorted({**rec["unrecoverable"], **rec["blocked"]}.items()):
            print(f"  - {sid}: no usable extraction (interrupted install: {why})")
        for problem in rec["problems"]:
            print(f"  - install journals: {problem}")
    if summary["staging_cleanup"]["blocked"]:
        print(f"Staging cleanup skipped: {summary['staging_cleanup']['blocked']}")
    if summary["error_details"]:
        for err in summary["error_details"]:
            then = {"cached": " (cached: awaits changed inputs or --force)",
                    "retry": " (retries on the next run)"}.get(err.get("retry"), "")
            print(f"  - {err['source_id']}: {err['error']}{then}")
    for line in publication.report(summary["publication"]):
        print(line)
    for job in summary.get("interrupted_jobs") or []:
        print(f"Earlier run {job['job_id']} (started {job['started_at']}) was interrupted; "
              "recorded as failed.")
    print(f"Job: {summary['job_id']}")
    if summary.get("cancelled"):
        return EXIT_CANCELLED
    published = (summary["publication"] or {}).get("status") != publication.PENDING
    return 1 if summary["errors"] or not published else 0


if __name__ == "__main__":
    from app.backend import run_guard  # noqa: E402 - one run at a time (ADR-0068)
    raise SystemExit(run_guard.run_cli(get_settings().db_dir, "extract", lambda: main(sys.argv[1:])))
