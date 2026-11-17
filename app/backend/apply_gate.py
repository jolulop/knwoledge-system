#!/usr/bin/env python3
"""Consistent Apply previews and the preview token (ADR-0068 decision 6).

One implementation behind every entry point (the API's JSON and UI routes, `scripts/apply_reviews.py`):

- :func:`preview` copies the vault state under the write lock (``changes=False``) together with the
  vault revision, copies the code and raw bytes after it, runs the same Apply orchestration on the
  copy outside the lock, and only for a clean (`ok`) result takes the lock again, checks the
  revision is unchanged and records an authorization (`app/backend/apply_authorization.py`). Its
  token is returned only once that record is durable.
- :func:`apply` checks a token under the write lock before its first write: the record exists, and
  its revision, code, settings and options all still match. It then consumes the record, advances the
  revision, and runs Apply. Every refusal (:class:`ApplyRefused`) changes no authority and consumes
  no token.

The caller passes the orchestration in (`evaluate` for the sandbox run, `run_apply` for the live
one), so this module stays free of the API. `loaded_code` is the fingerprint of the code a
long-running process imported at startup: while the disk differs, both refuse with
`restart_required` (a fresh script process passes None; it runs what is on disk).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.backend import apply_authorization, apply_sandbox, revision, write_lock
from app.backend.config import Settings, get_settings

CODE_DIRS = ("app", "scripts", "templates")
_SECRET = re.compile(r"api_key|secret|password|token", re.IGNORECASE)
# Settings Apply never reads: it is key-free and deterministic (no model, embedding or eval call).
# APP_HOST/APP_PORT are read: Source pages link the raw view through them (`wiki.raw_view_base`).
_NOT_APPLY_INPUTS = ("enrich_", "query_", "embedding_", "eval_", "extract_", "openai_",
                     "anthropic_", "response_cache")
# `.env` keys never carried into the sandbox: secrets (Apply is key-free) and absolute paths that
# could point outside it. Every vault path is derived from the root, so it is redirected.
_ENV_NOT_CARRIED = re.compile(r"API_KEY|SECRET|PASSWORD|TOKEN|_DIR$|_PATH$", re.IGNORECASE)

# reason -> (HTTP status, the action the caller must take)
REFUSALS: dict[str, tuple[int, str]] = {
    "preview_required": (422, "preview first, then submit its token"),
    "unknown_preview": (409, "preview again"),
    "stale_preview": (409, "the vault, code or settings changed since the preview; preview again"),
    "authorization_store_unavailable": (503, "retry; check db/ permissions and free space"),
    "restart_required": (503, "restart the API, then preview again"),
    # ADR-0074 decision 4 (slice 3b): an approved record is damaged or approved/ can't be listed.
    "review_records_unavailable": (409, "restore the listed damaged review records from backup "
                                         "(scripts/backup.py), or finish each listed interrupted "
                                         "decision or reopen (repeat the action on its review "
                                         "page), then preview again"),
}


class _SettingsNotCarried(RuntimeError):
    """The sandbox would execute with other effective settings than the live vault."""


class ApplyRefused(RuntimeError):
    """Apply (or a preview) refused before any authority change."""

    def __init__(self, reason: str, problems: dict[str, Any] | None = None) -> None:
        self.reason = reason
        self.problems = problems
        self.status_code, self.action = REFUSALS[reason]
        super().__init__(f"{reason}: {self.action}")

    def detail(self) -> dict[str, Any]:
        out: dict[str, Any] = {"reason": self.reason, "action": self.action}
        if self.problems is not None:
            out["problems"] = self.problems
        return out


def _approved_preflight(st: Settings) -> dict[str, Any] | None:
    """The shared Apply preflight (ADR-0074 decision 4, slice 3b), run under the write lock before
    anything is copied, authorized or executed: the problems when an approved record is damaged
    or `approved/` can't be listed, else None. A damaged approval's subject is unknown, so no other
    approval can be shown independent of it — the whole Apply is refused."""
    from app.workers import reviews                       # lazy: the review family lives in workers
    try:
        reviews.approved_preflight(st.reviews_dir)
    except reviews.ReviewRecordsUnavailable as exc:
        return exc.scan.problems()
    return None


def _blocked_preview(problems: dict[str, Any]) -> dict[str, Any]:
    return {"status": "blocked", "reason": "review_records_unavailable", "problems": problems,
            "diff": {"graph": apply_sandbox.empty_graph_diff(), "wiki": [], "reviews": [],
                     "manifests": []},
            "items": [], "not_appliable": [], "validators": {"passed": False, "failures": []},
            "warnings": ["review_records_unavailable"], "preview_token": None}


def code_fingerprint(base: Path) -> str:
    """A hash over the relative paths and contents of `app/`, `scripts/` and `templates/` under
    `base` (added and deleted files count; `__pycache__/` and compiled files do not)."""
    h = hashlib.sha256()
    base = Path(base)
    for d in CODE_DIRS:
        top = base / d
        if not top.is_dir():
            continue
        for p in sorted(top.rglob("*")):
            rel = p.relative_to(base)
            if (not p.is_file() or "__pycache__" in rel.parts
                    or p.suffix in (".pyc", ".pyo")):
                continue
            h.update(rel.as_posix().encode("utf-8") + b"\0")
            h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def settings_fingerprint(st: Settings) -> str:
    """A hash of the effective settings that can change what Apply does: the paths (relative to the
    vault root) and the rendering knobs. Secrets and settings Apply never reads are left out."""
    out: dict[str, Any] = {}
    for f in dataclasses.fields(st):
        if _SECRET.search(f.name) or f.name.startswith(_NOT_APPLY_INPUTS):
            continue
        v = getattr(st, f.name)
        if isinstance(v, Path):
            try:
                v = "<root>/" + Path(v).resolve().relative_to(Path(st.root).resolve()).as_posix()
            except ValueError:
                v = str(v)
        out[f.name] = v
    return hashlib.sha256(json.dumps(out, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def effective_settings_fingerprint(st: Settings) -> str:
    """What Apply's execution reads: the caller's settings and the ones its workers load afresh from
    the vault root (`.env` and the environment), e.g. the raw-view URL of Source pages."""
    both = settings_fingerprint(st) + settings_fingerprint(get_settings(st.root))
    return hashlib.sha256(both.encode("ascii")).hexdigest()


def carry_env(st: Settings, tmp_root: Path) -> None:
    """Give the sandbox the live vault's `.env` (without secrets and absolute paths), so the copy and
    its subprocesses (validators, index rebuild) execute with the same effective settings. The
    environment itself is inherited as is."""
    src = Path(st.root) / ".env"
    if not src.exists():
        return
    lines = []
    for line in src.read_text(encoding="utf-8").splitlines():
        key = line.partition("=")[0].strip()
        if "=" in line and not line.strip().startswith("#") and not _ENV_NOT_CARRIED.search(key):
            lines.append(line)
    (Path(tmp_root) / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def check_loaded_code(loaded_code: str | None, repo: Path | None) -> None:
    """Refuse with `restart_required` while the code on disk differs from what this process loaded."""
    if loaded_code is not None and repo is not None and code_fingerprint(repo) != loaded_code:
        raise ApplyRefused("restart_required")


def _failed(reason: str, exc: BaseException) -> dict[str, Any]:
    return {"status": "failed", "reason": reason, "error": f"{type(exc).__name__}: {exc}",
            "diff": None, "items": [], "not_appliable": [],
            "validators": {"passed": False, "failures": []}, "warnings": [reason],
            "preview_token": None}


# ADR-0074 change 7c (Q4): the preview's phase marks, additive diagnostics on every result. Each is a
# `time.monotonic_ns()` reading (CLOCK_MONOTONIC: system-wide, so comparable with another process's
# readings). `lock_released` is read right after the hold exits, so `lock_held` includes everything
# done before the release, the release itself included. No byte accounting happens here.
_PHASES = (("lock_wait", "lock_requested", "lock_acquired"),
           ("lock_held", "lock_acquired", "lock_released"),
           ("after_lock_copy", "lock_released", "after_lock_copied"),
           ("sandbox_run", "after_lock_copied", "sandbox_evaluated"),
           ("cleanup", "sandbox_evaluated", "sandbox_cleaned"),
           ("token_lock_wait", "token_lock_requested", "token_lock_acquired"),
           ("token_lock_held", "token_lock_acquired", "token_lock_released"),
           ("total", "started", "finished"))


def _mark(marks: dict[str, int], name: str) -> None:
    marks[name] = time.monotonic_ns()


def preview_timings(marks: dict[str, int], sandbox_phases_ms: dict[str, float] | None = None
                    ) -> dict[str, Any]:
    """The `timings` block: the raw marks and the derived phase durations (ms) whose marks exist."""
    phases = {name: round((marks[b] - marks[a]) / 1e6, 3) for name, a, b in _PHASES
              if a in marks and b in marks}
    out: dict[str, Any] = {"clock": "monotonic_ns", "marks_ns": dict(marks), "phases_ms": phases}
    if sandbox_phases_ms:
        out["sandbox_phases_ms"] = dict(sandbox_phases_ms)
    return out


def preview(st: Settings, *, evaluate: Callable[[Settings], dict[str, Any]],
            loaded_code: str | None = None, repo: Path | None = None,
            options: dict[str, Any] | None = None, now: str | None = None) -> dict[str, Any]:
    """Preview Apply on a copy; return the result with `preview_token` (a string only when `ok`).

    A result that is otherwise `ok` but gets no token says why in `token_withheld`:
    `code_changed`, `revision_changed`, `settings_changed` or `authorization_write_failed`. A failed copy is a failed
    preview (`copy_failed`), never `ok`. Every returned result carries `timings` (ADR-0074 7c)."""
    marks: dict[str, int] = {}
    _mark(marks, "started")
    result = _preview(st, evaluate=evaluate, loaded_code=loaded_code, repo=repo, options=options,
                      now=now, marks=marks)
    _mark(marks, "finished")
    result["timings"] = preview_timings(marks, result.pop("sandbox_phases_ms", None))
    return result


def _preview(st: Settings, *, evaluate: Callable[[Settings], dict[str, Any]],
             loaded_code: str | None, repo: Path | None, options: dict[str, Any] | None,
             now: str | None, marks: dict[str, int]) -> dict[str, Any]:
    options = dict(options or {})
    check_loaded_code(loaded_code, repo)
    tmp_root: Path | None = None
    try:
        _mark(marks, "lock_requested")
        try:
            with write_lock.hold(st.reviews_dir, authority=False, changes=False):
                _mark(marks, "lock_acquired")
                if (problems := _approved_preflight(st)) is not None:
                    return _blocked_preview(problems)          # no copy, no run, no token
                rev = revision.current(st.root)
                # The settings this preview executes with, captured with the snapshot: the sandbox
                # must reproduce them, and the authorization binds exactly these.
                live = settings_fingerprint(get_settings(st.root))
                settings_fp = effective_settings_fingerprint(st)
                tmp_root = apply_sandbox.copy_state(st)
        finally:
            if "lock_acquired" in marks:
                _mark(marks, "lock_released")
        apply_sandbox.copy_code_and_raw(st, tmp_root)
        carry_env(st, tmp_root)
        code = code_fingerprint(tmp_root)
        sandbox = get_settings(tmp_root)
        # The copy must execute with the captured settings (paths aside, root-relative).
        if settings_fingerprint(sandbox) != live:
            raise _SettingsNotCarried("the sandbox could not reproduce the live settings")
    except revision.RevisionUnavailable:
        if tmp_root is not None:
            apply_sandbox.cleanup_sandbox(tmp_root)
        raise
    except Exception as exc:  # noqa: BLE001 - a failed copy is a failed preview, never authorized
        if tmp_root is not None:
            apply_sandbox.cleanup_sandbox(tmp_root)
        return _failed("settings_not_carried" if isinstance(exc, _SettingsNotCarried)
                       else "copy_failed", exc)
    _mark(marks, "after_lock_copied")
    try:
        result = evaluate(sandbox)
    finally:
        _mark(marks, "sandbox_evaluated")
        apply_sandbox.cleanup_sandbox(tmp_root)
        _mark(marks, "sandbox_cleaned")
    result["preview_token"] = None
    # A recovered revision or authorization store is reported, never a blocker (its copy is evidence).
    if revision.damaged_copies(st.root):
        result.setdefault("warnings", []).append("revision_recovered")
    if apply_authorization.damaged_copies(st.root):
        result.setdefault("warnings", []).append("authorization_store_recovered")
    if result.get("status") != "ok":
        return result
    if code_fingerprint(st.root) != code:
        result["token_withheld"] = "code_changed"
        return result
    try:
        _mark(marks, "token_lock_requested")
        try:
            with write_lock.hold(st.reviews_dir, authority=False, changes=False):
                _mark(marks, "token_lock_acquired")
                if revision.current(st.root) != rev:
                    result["token_withheld"] = "revision_changed"
                    return result
                if effective_settings_fingerprint(st) != settings_fp:
                    result["token_withheld"] = "settings_changed"   # e.g. .env edited mid-preview
                    return result
                token = apply_authorization.issue(st.root, rev=rev, code=code,
                                                  settings=settings_fp, options=options, now=now)
        finally:
            if "token_lock_acquired" in marks:
                _mark(marks, "token_lock_released")
    except (apply_authorization.StoreUnavailable, revision.RevisionUnavailable):
        result["token_withheld"] = "authorization_write_failed"
        return result
    result["preview_token"] = token
    return result


def apply(st: Settings, token: str | None, *, run_apply: Callable[[Settings], dict[str, Any]],
          loaded_code: str | None = None, repo: Path | None = None,
          options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply under a preview token; raise :class:`ApplyRefused` before any write when it fails."""
    options = dict(options or {})
    if not token:
        raise ApplyRefused("preview_required")
    check_loaded_code(loaded_code, repo)
    with write_lock.hold(st.reviews_dir, changes=False):
        if (problems := _approved_preflight(st)) is not None:
            raise ApplyRefused("review_records_unavailable", problems)   # token not consumed
        try:
            rec = apply_authorization.find(st.root, token)
        except apply_authorization.StoreUnavailable:
            raise ApplyRefused("authorization_store_unavailable") from None
        if rec is None:
            raise ApplyRefused("unknown_preview")
        rev = revision.current(st.root)
        if ((rec["epoch"], rec["counter"]) != (rev["epoch"], rev["counter"])
                or rec["code"] != code_fingerprint(st.root)
                or rec["settings"] != effective_settings_fingerprint(st)
                or rec["options"] != options):
            raise ApplyRefused("stale_preview")
        try:
            apply_authorization.consume(st.root, token)
        except apply_authorization.StoreUnavailable:
            raise ApplyRefused("authorization_store_unavailable") from None
        write_lock.invalidate()                   # spent: the revision moves before any write
        return run_apply(st)
