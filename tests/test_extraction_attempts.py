"""ADR-0074 slice 6b — failed attempts, retry classification and last-good (decision 6).

The latest failed attempt is recorded beside (never inside) the extraction log; only explicit
deterministic rejections are cached, and only while their fingerprint — every bound of the attempt
— is unchanged; everything else retries on every run; `force` always retries; the document screen
and lint tell a retained prior extraction from no usable extraction, and a cached failure from one
that retries.
"""
from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import durable_write, manifests, review_html, review_read  # noqa: E402
from app.backend import extraction_install as xi  # noqa: E402
from app.workers import (  # noqa: E402
    extract,
    extract_attempts as attempts,
    extract_child,
    extract_parse,
    extract_runner,
    intake,
    lint,
)
from app.workers.extract_parse import ExtractionError  # noqa: E402
from tests import fixtures  # noqa: E402

RETRIED = [extract_runner.TIMEOUT, extract_runner.CRASHED, extract_runner.UNAVAILABLE,
           extract_runner.OUTPUT_INVALID, "missing_dependency", "unsupported", None]
CACHED = ["decompression_bomb", "invalid_archive", "output_too_large"]


def _vault(tmp_path: Path, name: str = "doc.md", write=fixtures.write_markdown) -> tuple[Path, str]:
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.copy_templates(tmp_path)
    write(inbox / name)
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    sid = manifests.scan_manifests(tmp_path / "raw" / "manifests").valid[0]["source_id"]
    return tmp_path, sid


def _run(root: Path, **kw) -> dict:
    return extract.extract_sources(root, jobs_db=root / "db" / "jobs.sqlite", **kw)


def _manifest(root: Path, sid: str) -> dict:
    return manifests.get_manifest(root / "raw" / "manifests", sid)


def _record(root: Path, sid: str) -> dict | None:
    return attempts.read(root / "normalized", sid).record


class _Parse:
    """`extract._parse` replaced: counts launches; raises `reason` (an ExtractionError, or a plain
    exception for None) or delegates to the real child."""

    def __init__(self, reason: object = "ok") -> None:
        self.reason, self.calls = reason, 0

    def __call__(self, path, sid, **kw):
        self.calls += 1
        if self.reason == "ok":
            return extract_runner.parse(path, sid, **kw)
        if self.reason is None:
            raise RuntimeError("simulated unclassified failure")
        raise ExtractionError(f"simulated {self.reason}", self.reason)


# --- classification and the fingerprint -----------------------------------------------------


@pytest.mark.parametrize("reason", RETRIED)
def test_environmental_and_ambiguous_failures_always_retry(reason):
    assert attempts.category(reason, "f" * 64) == attempts.RETRY


@pytest.mark.parametrize("reason", [*CACHED, "oversize"])
def test_only_explicit_deterministic_rejections_are_cached(reason):
    assert attempts.category(reason, "f" * 64) == attempts.CACHED
    assert attempts.category(reason, None) == attempts.RETRY      # no fingerprint: retry


def _fp(manifest, raw, **kw):
    args = {"max_file_mb": 50, "timeout_s": 120, "target_chars": 1000, "max_chars": 2000,
            "code_version": extract.EXTRACT_CODE_VERSION, **kw}
    return attempts.fingerprint(manifest, raw, **args)[0]


def test_the_fingerprint_covers_every_bound(tmp_path, monkeypatch):
    raw = fixtures.write_docx(tmp_path / "a.docx")
    manifest = {"source_id": "src_0123456789abcdef", "sha256": "a" * 64}
    base = _fp(manifest, raw)
    assert base is not None and _fp(manifest, raw) == base           # deterministic
    changed = [
        _fp({**manifest, "sha256": "b" * 64}, raw),
        _fp(manifest, raw, max_file_mb=51), _fp(manifest, raw, timeout_s=121),
        _fp(manifest, raw, target_chars=999), _fp(manifest, raw, max_chars=1999),
        _fp(manifest, raw, code_version=extract.EXTRACT_CODE_VERSION + 1),
    ]
    for module, name in ((extract_parse, "MAX_ZIP_ENTRIES"), (extract_parse, "MAX_ZIP_RATIO"),
                         (extract_parse, "MAX_ZIP_UNCOMPRESSED"),
                         (extract_runner, "MAX_RESULT_BYTES"), (extract_runner, "ENVELOPE_BYTES"),
                         (extract_child, "PROTOCOL_VERSION")):
        with monkeypatch.context() as m:
            m.setattr(module, name, getattr(module, name) + 1)
            changed.append(_fp(manifest, raw))
    with monkeypatch.context() as m:
        m.setattr(attempts, "_dist_version", lambda name: "999.0")
        changed.append(_fp(manifest, raw))                           # a dependency version
    with monkeypatch.context() as m:
        m.setattr(sys, "version", "3.99.0 (other)")
        changed.append(_fp(manifest, raw))                           # the interpreter
    st = raw.stat()
    os.utime(raw, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    changed.append(_fp(manifest, raw))                               # the raw file touched
    raw.write_bytes(raw.read_bytes() + b"\0")
    changed.append(_fp(manifest, raw))                               # ... or grown
    assert None not in changed and base not in changed and len(set(changed)) == len(changed)


def test_missing_fingerprint_information_means_retry(tmp_path, monkeypatch):
    raw = fixtures.write_docx(tmp_path / "a.docx")
    manifest = {"source_id": "src_0123456789abcdef", "sha256": "a" * 64}
    assert _fp(manifest, None) is None                               # no confined raw file
    assert _fp(manifest, tmp_path / "gone.docx") is None             # can't stat it
    assert _fp({**manifest, "sha256": None}, raw) is None            # no recorded checksum
    assert _fp(manifest, tmp_path / "a.txt") is None                 # no dependency map
    monkeypatch.setattr(attempts, "_dist_version", lambda name: None)
    assert _fp(manifest, raw) is None                                # a dependency unreadable
    assert _fp(manifest, fixtures.write_markdown(tmp_path / "a.md")) is not None  # needs none


# --- the run --------------------------------------------------------------------------------


@pytest.mark.parametrize("reason", CACHED)
def test_a_cached_rejection_is_skipped_until_force(tmp_path, monkeypatch, reason):
    root, sid = _vault(tmp_path)
    parse = _Parse(reason)
    monkeypatch.setattr(extract, "_parse", parse)
    first = _run(root)
    assert first["errors"] == 1 and first["error_details"][0]["retry"] == attempts.CACHED
    record = _record(root, sid)
    assert record["reason"] == reason and record["category"] == attempts.CACHED
    assert record["prior_retained"] is False and record["fingerprint"]
    second = _run(root)
    assert parse.calls == 1                                          # no child launched
    assert second["skipped_known_failure"] == 1 and second["errors"] == 0
    assert second["known_failures"] == [{"source_id": sid, "skip_reason": reason,
                                         "error": f"simulated {reason}",
                                         "attempted_at": record["attempted_at"],
                                         "preserved_prior": False}]
    assert second["status"] == "succeeded"
    forced = _run(root, force=True)                                  # an unconditional retry
    assert parse.calls == 2 and forced["errors"] == 1


@pytest.mark.parametrize("reason", RETRIED)
def test_every_other_failure_retries_on_every_run(tmp_path, monkeypatch, reason):
    root, sid = _vault(tmp_path)
    parse = _Parse(reason)
    monkeypatch.setattr(extract, "_parse", parse)
    for n in (1, 2, 3):
        out = _run(root)
        assert parse.calls == n and out["errors"] == 1 and out["skipped_known_failure"] == 0
        assert out["error_details"][0]["retry"] == attempts.RETRY
    assert _record(root, sid)["category"] == attempts.RETRY
    assert _record(root, sid)["reason"] == reason


@pytest.mark.parametrize("change", ["limit", "config", "raw", "dependency"])
def test_a_changed_bound_retries_a_cached_rejection(tmp_path, monkeypatch, change):
    root, sid = _vault(tmp_path)
    parse = _Parse("invalid_archive")
    monkeypatch.setattr(extract, "_parse", parse)
    _run(root)
    kw = {}
    if change == "limit":
        monkeypatch.setattr(extract_parse, "MAX_ZIP_ENTRIES", extract_parse.MAX_ZIP_ENTRIES + 1)
    elif change == "config":
        kw = {"target_chars": 900}
    elif change == "raw":
        raw = root / _manifest(root, sid)["relative_raw_path"]
        st = raw.stat()
        os.utime(raw, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    else:
        monkeypatch.setattr(extract, "EXTRACT_CODE_VERSION", extract.EXTRACT_CODE_VERSION + 1)
    out = _run(root, **kw)
    assert parse.calls == 2 and out["skipped_known_failure"] == 0 and out["errors"] == 1


def test_oversize_is_cached_and_a_raised_limit_extracts(tmp_path):
    def big(path: Path) -> Path:
        path.write_text("# Big\n\n" + ("All work and no play. " * 80_000), encoding="utf-8")
        return path
    root, sid = _vault(tmp_path, write=big)
    assert _run(root, max_file_mb=1)["error_details"][0]["skip_reason"] == "oversize"
    assert _record(root, sid)["category"] == attempts.CACHED
    assert _run(root, max_file_mb=1)["skipped_known_failure"] == 1
    out = _run(root, max_file_mb=2)                    # the file-size limit is in the fingerprint
    assert out["extracted"] == 1 and _manifest(root, sid)["ingestion_status"] == "extracted"
    assert _record(root, sid) is None                  # a success clears the latest failure
    assert lint._check_extraction_failures(root / "raw" / "manifests", root / "normalized") == []


def test_a_real_child_rejection_is_cached_end_to_end(tmp_path):
    def bad(path: Path) -> Path:
        path.write_bytes(b"not a zip at all")
        return path
    root, sid = _vault(tmp_path, name="bad.docx", write=bad)
    first = _run(root)
    assert first["error_details"][0]["skip_reason"] == "invalid_archive"
    second = _run(root)
    assert second["skipped_known_failure"] == 1 and second["errors"] == 0
    assert _manifest(root, sid)["ingestion_status"] == "error"


def test_a_failure_after_a_success_is_recorded_beside_the_good_log(tmp_path, monkeypatch):
    root, sid = _vault(tmp_path)
    assert _run(root)["extracted"] == 1
    log_path = root / "normalized" / "extraction_logs" / f"{sid}.json"
    good_log, good_md = log_path.read_bytes(), (root / "normalized" / "markdown" /
                                                 f"{sid}.md").read_bytes()
    extracted_at = _manifest(root, sid)["extracted_at"]
    monkeypatch.setattr(extract, "_parse", _Parse("output_too_large"))
    out = _run(root, force=True)
    assert out["error_details"][0]["preserved_prior"] is True
    record = _record(root, sid)
    assert record["prior_retained"] is True and record["extracted_at"] == extracted_at
    assert record_path_is_beside(root, sid)
    assert log_path.read_bytes() == good_log                         # never inside the good log
    assert (root / "normalized" / "markdown" / f"{sid}.md").read_bytes() == good_md
    assert _manifest(root, sid)["ingestion_status"] == "extracted"
    # The good extraction stands; the cached failure is skipped (never "unchanged") while its
    # fingerprint matches.
    assert _run(root)["skipped_known_failure"] == 1
    (finding,) = lint._check_extraction_failures(root / "raw" / "manifests", root / "normalized")
    assert finding["check"] == "extraction_attempt_failed" and finding["severity"] == "low"
    assert finding["detail"] == ("usable prior extraction retained; latest attempt failed: "
                                 "output_too_large — awaits changed inputs or a forced "
                                 "re-extraction")
    monkeypatch.setattr(extract, "_parse", _Parse())
    assert _run(root, force=True)["extracted"] == 1
    assert _record(root, sid) is None
    assert lint._check_extraction_failures(root / "raw" / "manifests", root / "normalized") == []


def record_path_is_beside(root: Path, sid: str) -> bool:
    path = attempts.record_path(root / "normalized", sid)
    return path.parent.name == "extraction_attempts" and path.is_file()


def test_a_superseded_or_damaged_record_is_no_record(tmp_path, monkeypatch):
    root, sid = _vault(tmp_path)
    parse = _Parse("invalid_archive")
    monkeypatch.setattr(extract, "_parse", parse)
    _run(root)
    manifest = _manifest(root, sid)
    assert attempts.current(root / "normalized", manifest) is not None
    # Bound to the manifest's state: another extracted_at, or a retained prior, retires it.
    assert attempts.current(root / "normalized", {**manifest, "extracted_at": "x"}) is None
    assert attempts.current(root / "normalized",
                            {**manifest, "ingestion_status": "extracted"}) is None
    assert attempts.current(root / "normalized", {**manifest, "ingestion_status": "new"}) is None
    path = attempts.record_path(root / "normalized", sid)
    for damage in (b"{not json", b"[]", json.dumps({**_record(root, sid),
                                                    "category": "forever"}).encode(),
                   json.dumps({**_record(root, sid), "reason": "timeout"}).encode()):
        path.write_bytes(damage)
        assert attempts.read(root / "normalized", sid).state == "corrupt"
        calls = parse.calls
        assert _run(root)["skipped_known_failure"] == 0              # damaged: retried
        assert parse.calls == calls + 1 and path.read_bytes() != damage
        assert _record(root, sid) is not None


def test_a_record_that_cannot_be_written_is_a_warning_and_a_retry(tmp_path, monkeypatch):
    root, sid = _vault(tmp_path)
    parse = _Parse("invalid_archive")
    monkeypatch.setattr(extract, "_parse", parse)

    def refuse(*a, **k):
        raise PermissionError("read-only")
    monkeypatch.setattr(attempts, "write", refuse)
    out = _run(root)
    assert out["errors"] == 1 and out["error_details"][0]["retry"] == attempts.RETRY
    assert {"source_id": sid, "warning": "attempt_record_unwritten: PermissionError"} in out[
        "warnings"]
    assert _run(root)["skipped_known_failure"] == 0 and parse.calls == 2


def test_an_unusable_install_journal_is_never_skipped_as_a_known_failure(tmp_path, monkeypatch):
    root, sid = _vault(tmp_path)
    parse = _Parse("invalid_archive")
    monkeypatch.setattr(extract, "_parse", parse)
    _run(root)

    class Unrecoverable:
        state, usable, entry, error = "unrecoverable", False, {"reason": "x"}, None
    real = extract.xi.read_journal
    monkeypatch.setattr(extract.xi, "read_journal",
                        lambda layout, s: Unrecoverable() if s == sid else real(layout, s))
    _run(root)
    assert parse.calls == 2


# --- what the screen and lint show ----------------------------------------------------------


def _screen(root: Path, sid: str) -> tuple[dict, str]:
    view = review_read.source_review_view(
        root / "reviews", sid, graph_db=root / "db" / "graph.sqlite", wiki_dir=root / "wiki",
        manifests_dir=root / "raw" / "manifests",
        enrichment_dir=root / "normalized" / "enrichment")
    return view, review_html.render_source_screen(view)


def test_the_screen_and_lint_tell_the_two_situations_and_the_two_futures(tmp_path, monkeypatch):
    root, sid = _vault(tmp_path)
    monkeypatch.setattr(extract, "_parse", _Parse(extract_runner.TIMEOUT))
    _run(root)
    view, html = _screen(root, sid)
    assert view["extraction_attempt"]["situation"] == attempts.NO_USABLE
    assert "No usable extraction</strong>: no usable extraction: timeout — retries on the next " \
           "run" in html
    (finding,) = lint._check_extraction_failures(root / "raw" / "manifests", root / "normalized")
    assert finding["check"] == "extraction_failed" and finding["severity"] == "medium"
    assert finding["data"]["cached"] is False and "retries on the next run" in finding["detail"]

    monkeypatch.setattr(extract, "_parse", _Parse("decompression_bomb"))
    _run(root)
    view, html = _screen(root, sid)
    assert "no usable extraction: decompression_bomb — awaits changed inputs or a forced " \
           "re-extraction" in html
    (finding,) = lint._check_extraction_failures(root / "raw" / "manifests", root / "normalized")
    assert finding["data"]["cached"] is True and "--force" in finding["data"]["remediation"]

    monkeypatch.setattr(extract, "_parse", _Parse())
    _run(root, force=True)
    view, html = _screen(root, sid)
    assert view["extraction_attempt"] is None and "No usable extraction" not in html
    monkeypatch.setattr(extract, "_parse", _Parse(None))
    _run(root, force=True)
    view, html = _screen(root, sid)
    assert "Latest extraction attempt failed</strong>: usable prior extraction retained; latest " \
           "attempt failed: unclassified parse failure — retries on the next run" in html
    assert "RuntimeError: simulated unclassified failure" in html


def test_a_failure_known_only_from_its_log_retries(tmp_path, monkeypatch):
    # A failure recorded before attempt records existed (or whose record was lost): the log says
    # why, and nothing is cached.
    root, sid = _vault(tmp_path)
    monkeypatch.setattr(extract, "_parse", _Parse("invalid_archive"))
    _run(root)
    attempts.record_path(root / "normalized", sid).unlink()
    state = attempts.failure_state(root / "normalized", _manifest(root, sid))
    assert state == {"situation": attempts.NO_USABLE, "reason": "invalid_archive",
                     "error": "simulated invalid_archive", "attempted_at": None, "cached": False,
                     "journal": None}
    (root / "normalized" / "extraction_logs" / f"{sid}.json").write_bytes(b"\xff")
    state = attempts.failure_state(root / "normalized", _manifest(root, sid))
    assert state["reason"] is None and "unreadable" in state["error"]
    assert "unclassified parse failure — retries on the next run" in attempts.describe(state)


def test_lint_reports_them_without_failing(tmp_path, monkeypatch):
    root, sid = _vault(tmp_path)
    monkeypatch.setattr(extract, "_parse", _Parse("invalid_archive"))
    _run(root)
    report = lint.run_lint(root, record_job=False, file_review_items=False)
    assert report["by_check"].get("extraction_failed") == 1
    assert report["status"] != "failing" or any(f["severity"] == "high" and
                                                f["check"] != "extraction_failed"
                                                for f in report["findings"])
    assert not any(f["check"].startswith("extraction") and f["severity"] == "high"
                   for f in report["findings"])


def test_a_zip_bomb_through_the_real_child_is_cached(tmp_path):
    def bomb(path: Path) -> Path:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("word/document.xml", b"\0" * (8 << 20))
        return path
    root, sid = _vault(tmp_path, name="bomb.docx", write=bomb)
    assert _run(root)["error_details"][0]["skip_reason"] == "decompression_bomb"
    assert _run(root)["skipped_known_failure"] == 1


# --- review round 1: lifecycle -------------------------------------------------------------


def test_a_failed_re_extraction_retries_instead_of_skipping_as_unchanged(tmp_path, monkeypatch):
    # Success, then a forced attempt that times out: the prior outputs are kept (and would read
    # as unchanged), but the failure is environmental — every ordinary run retries it.
    root, sid = _vault(tmp_path)
    assert _run(root)["extracted"] == 1
    parse = _Parse(extract_runner.TIMEOUT)
    monkeypatch.setattr(extract, "_parse", parse)
    assert _run(root, force=True)["error_details"][0]["preserved_prior"] is True
    for n in (2, 3):
        out = _run(root)
        assert parse.calls == n and out["skipped_unchanged"] == 0 and out["errors"] == 1
        assert out["error_details"][0]["retry"] == attempts.RETRY
    _view, html = _screen(root, sid)
    assert "latest attempt failed: timeout — retries on the next run" in html
    monkeypatch.setattr(extract, "_parse", _Parse())                 # the retry now succeeds
    assert _run(root)["extracted"] == 1 and _record(root, sid) is None
    assert _run(root)["skipped_unchanged"] == 1                      # and only now is unchanged


@pytest.mark.parametrize("change", ["limit", "config", "code"])
def test_a_cached_failed_re_extraction_retries_when_a_bound_changes(tmp_path, monkeypatch, change):
    root, sid = _vault(tmp_path)
    assert _run(root)["extracted"] == 1
    parse = _Parse("output_too_large")
    monkeypatch.setattr(extract, "_parse", parse)
    _run(root, force=True)
    assert _run(root)["skipped_known_failure"] == 1 and parse.calls == 1
    kw = {}
    if change == "limit":
        monkeypatch.setattr(extract_runner, "MAX_RESULT_BYTES", extract_runner.MAX_RESULT_BYTES * 2)
    elif change == "config":
        kw = {"max_chars": 2500}
    else:
        monkeypatch.setattr(extract, "EXTRACT_CODE_VERSION", extract.EXTRACT_CODE_VERSION + 1)
    out = _run(root, **kw)
    assert parse.calls == 2 and out["skipped_known_failure"] == 0 and out["errors"] == 1
    assert _run(root, **kw)["skipped_known_failure"] == 1           # cached again, new bounds


def test_a_same_second_success_retires_the_failure_even_if_its_removal_fails(tmp_path,
                                                                             monkeypatch):
    # Every timestamp in one second: the binding is the generation (the log's attempt id).
    root, sid = _vault(tmp_path)
    monkeypatch.setattr(extract, "iso_now", lambda: "2026-11-16T12:00:00+00:00")
    assert _run(root)["extracted"] == 1
    first = attempts.generation(root / "normalized", sid)
    monkeypatch.setattr(extract, "_parse", _Parse("output_too_large"))
    _run(root, force=True)
    assert _record(root, sid)["generation"] == first
    assert attempts.current(root / "normalized", _manifest(root, sid)) is not None

    def refuse(*a, **k):
        raise PermissionError("read-only")
    monkeypatch.setattr(attempts, "clear", refuse)
    monkeypatch.setattr(extract, "_parse", _Parse())
    out = _run(root, force=True)
    assert out["extracted"] == 1
    assert {"source_id": sid, "warning": "attempt_record_unwritten: PermissionError"} in out[
        "warnings"]
    assert _record(root, sid) is not None                            # still on disk ...
    assert _manifest(root, sid)["extracted_at"] == _record(root, sid)["extracted_at"]
    assert attempts.generation(root / "normalized", sid) != first
    assert attempts.current(root / "normalized", _manifest(root, sid)) is None   # ... but retired
    assert _screen(root, sid)[0]["extraction_attempt"] is None
    assert lint._check_extraction_failures(root / "raw" / "manifests", root / "normalized") == []
    assert _run(root)["skipped_unchanged"] == 1


class _Crash(BaseException):
    pass


def test_kept_outputs_behind_an_unrecoverable_install_are_not_a_usable_prior(tmp_path,
                                                                             monkeypatch):
    root, sid = _vault(tmp_path)
    assert _run(root)["extracted"] == 1
    real = durable_write.move

    def move(src, dst):
        real(src, dst)
        if Path(dst).suffix == ".md":
            raise _Crash("after the markdown rename")
    with monkeypatch.context() as m:
        m.setattr(durable_write, "move", move)
        with pytest.raises(_Crash):
            _run(root, force=True)
    layout = xi.Layout(root / "normalized")
    read = xi.read_journal(layout, sid)
    xi.mark_unrecoverable(layout, read.entry, "staged_file_missing", manifests.iso_now())
    md = (root / "normalized" / "markdown" / f"{sid}.md").read_bytes()
    log = (root / "normalized" / "extraction_logs" / f"{sid}.json").read_bytes()
    parse = _Parse(extract_runner.TIMEOUT)
    monkeypatch.setattr(extract, "_parse", parse)
    out = _run(root)
    assert out["error_details"][0]["preserved_prior"] is True        # the bytes are kept ...
    assert (root / "normalized" / "markdown" / f"{sid}.md").read_bytes() == md
    assert (root / "normalized" / "extraction_logs" / f"{sid}.json").read_bytes() == log
    record = _record(root, sid)
    assert record["prior_retained"] is True and record["prior_usable"] is False  # ... not usable
    state = attempts.failure_state(root / "normalized", _manifest(root, sid))
    assert state["situation"] == attempts.RETAINED_UNUSABLE and state["journal"] == "unrecoverable"
    view, html = _screen(root, sid)
    assert "No usable extraction</strong>: no usable extraction (earlier outputs kept, but their " \
           "install is unrecoverable); latest attempt failed: timeout — retries on the next run" \
           in html
    assert "usable prior extraction retained" not in html
    findings = lint._check_extraction_failures(root / "raw" / "manifests", root / "normalized")
    (finding,) = [f for f in findings if f["subject"] == sid]
    assert finding["check"] == "extraction_failed" and finding["severity"] == "medium"
    assert finding["data"]["situation"] == attempts.RETAINED_UNUSABLE
    _run(root)
    assert parse.calls == 2                                          # and it retries
