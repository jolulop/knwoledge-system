"""Runtime half of the ADR-0074 decision-1 structural guard: an audit hook that sees every file write.

`sys.addaudithook` reports each `open` for writing, rename/replace, link and truncate, whatever
wrapper, alias or helper the code went through. A write whose destination (or rename source) is
file-backed **authority** must happen inside `app.backend.durable_write` (`in_progress()`); any other
writer is recorded as a violation and fails the test that caused it (`tests/conftest.py`). Writes by
test code itself (fixtures seeding damaged files) are attributed to `tests/` and allowed.

Families, by path shape (relative structure under any vault root, so pytest's `tmp_path` vaults and
Apply's sandbox copies both count):

- `raw/manifests/*.json`                         manifest
- `wiki/Items|Claims|Synthesis/*.md`             page
- `reviews/**` except lock files                 reviews (records, ledgers, audit, journals)
- `policies/**`                                  policies
- `db/*.json`                                    db_json

Lock files (`*.lock`) are never authority. Directory creation is checked too (`os.mkdir`, which
`Path.mkdir` and `os.makedirs` go through): an authority directory — `raw/`, `raw/manifests/`,
`wiki/`, `wiki/Items|Claims|Synthesis/`, `reviews/**`, `policies/**`, `db/` — must be created by
`durable_write.make_dirs`, so its entry is synced into its parent. Removal is checked the same way
(`os.remove` / `os.unlink` / `Path.unlink`, `os.rmdir`, `shutil.rmtree` of an authority directory):
an authority entry must be removed by `durable_write.remove` / `remove_dir`, which sync the
directory after the removal (slice 3b, review round 2). The static half is
`tests/test_authority_write_guard.py`.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from app.backend import durable_write

ROOT = Path(__file__).resolve().parents[1]
_APP, _SCRIPTS, _TESTS = ROOT / "app", ROOT / "scripts", ROOT / "tests"
_THIS = Path(__file__).resolve()
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

# Sanctioned non-helper writers of authority-shaped paths: (repo-relative file, function) -> reason.
RUNTIME_ALLOWED = {
    ("app/backend/apply_sandbox.py", "copy_state"):
        "copies live state into the throwaway Apply-preview sandbox (ADR-0040); not live authority",
    ("app/backend/apply_sandbox.py", "copy_code_and_raw"):
        "copies code and raw files into the throwaway Apply-preview sandbox",
    ("scripts/eval_contradictions.py", "evaluate"):
        "copies the evaluation's base vault into a scratch vault (outside any live vault)",
    ("scripts/eval_contradictions.py", "materialize"):
        "builds the evaluation's scratch base vault",
    ("scripts/eval_retrieval.py", "_build_corpus_vault"):
        "builds the retrieval evaluation's scratch vault",
}

_state = threading.local()
violations: list[str] = []
helper_writes: list[tuple[str, str]] = []        # (family, path) written through the helper
helper_dirs: list[tuple[str, str]] = []          # (family, path) directories the helper created
helper_removals: list[tuple[str, str]] = []      # (family, path) entries the helper removed
# Events whose path is a directory (classified with `directory_family`).
_DIR_EVENTS = frozenset({"os.mkdir", "os.rmdir", "shutil.rmtree"})
_enabled = False


def family(path: str | os.PathLike | bytes) -> str | None:
    """The authority family of `path`, or None when it is not file-backed authority."""
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    parts = Path(path).parts
    if not parts:
        return None
    name = parts[-1]
    if name.endswith(".lock"):
        return None
    for i in range(len(parts) - 1, -1, -1):
        head = parts[i]
        rest = parts[i + 1:]
        if head == "manifests" and i > 0 and parts[i - 1] == "raw" and len(rest) == 1:
            return "manifest" if name.endswith(".json") else None
        if head in ("Items", "Claims", "Synthesis") and i > 0 and parts[i - 1] == "wiki" \
                and len(rest) == 1:
            return "page"
        if head == "reviews" and rest:
            return "reviews"
        if head == "policies" and rest:
            return "policies"
        if head == "db" and len(rest) == 1:
            return "db_json" if name.endswith(".json") or ".json.tmp-" in name else None
    return None


_VAULT_DIRS = {"raw": "manifest", "wiki": "page", "reviews": "reviews", "policies": "policies",
               "db": "db_json"}


def directory_family(path: str | os.PathLike | bytes) -> str | None:
    """The authority family a directory at `path` holds (or is the vault-level ancestor of), or None.
    """
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    parts = Path(path).parts
    if not parts:
        return None
    name, parent = parts[-1], (parts[-2] if len(parts) > 1 else "")
    if name in _VAULT_DIRS:
        return _VAULT_DIRS[name]
    if parent == "raw" and name == "manifests":
        return "manifest"
    if parent == "wiki" and name in ("Items", "Claims", "Synthesis"):
        return "page"
    for anc in parts[:-1]:
        if anc in ("reviews", "policies"):
            return anc
    return None


def _writer() -> tuple[str, str] | None:
    """(repo-relative file, function) of the first project frame that is not this module."""
    frame = sys._getframe(1)
    while frame is not None:
        fn = Path(frame.f_code.co_filename)
        try:
            resolved = fn.resolve()
        except OSError:
            resolved = fn
        if resolved != _THIS and (resolved.is_relative_to(_APP) or resolved.is_relative_to(_SCRIPTS)
                                  or resolved.is_relative_to(_TESTS)):
            return str(resolved.relative_to(ROOT)), frame.f_code.co_name
        frame = frame.f_back
    return None


def _check(event: str, path) -> None:
    if not isinstance(path, (str, bytes, os.PathLike)):
        return
    fam = directory_family(path) if event in _DIR_EVENTS else family(path)
    if fam is None:
        return
    if durable_write.in_progress():
        shown = os.fsdecode(path) if isinstance(path, bytes) else os.fspath(path)
        if event in ("os.rename", "os.link"):
            helper_writes.append((fam, shown))
        elif event == "os.mkdir":
            helper_dirs.append((fam, shown))
        elif event in ("os.remove", "os.rmdir"):
            helper_removals.append((fam, shown))
        return
    writer = _writer()
    if writer is None or writer[0].startswith("tests/") or writer in RUNTIME_ALLOWED:
        return
    what = f"{fam} directory" if event in _DIR_EVENTS else fam
    violations.append(f"{event} of {what} {os.fspath(path)!s} by {writer[0]}:{writer[1]}"
                      " — route it through app.backend.durable_write")


def _hook(event: str, args: tuple) -> None:
    if not _enabled or getattr(_state, "busy", False):
        return
    if event not in ("open", "os.rename", "os.link", "os.symlink", "os.truncate", "os.mkdir",
                     "os.remove", "os.rmdir", "shutil.rmtree"):
        return
    _state.busy = True                            # our own path work must not re-enter
    try:
        if event == "open":
            path, mode, flags = args
            writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                     (mode is None and isinstance(flags, int) and flags & _WRITE_FLAGS)
            if writes:
                _check(event, path)
        elif event in ("os.rename", "os.link"):
            _check(event, args[1])                # the destination...
            if event == "os.rename":
                _check(event + " (source)", args[0])   # ...and a rename's source
        elif event in ("os.mkdir", "os.remove", "os.rmdir", "shutil.rmtree"):
            if event != "shutil.rmtree" and args[-1] not in (None, -1):   # last argument: dir_fd
                return                # relative to a dir_fd (rmtree internals): the tree is checked
            _check(event, args[0])
        elif event == "os.symlink":
            _check(event, args[1])
        else:
            _check(event, args[0])
    finally:
        _state.busy = False


_installed = False


def install() -> None:
    global _installed, _enabled
    if not _installed:
        sys.addaudithook(_hook)
        _installed = True
    _enabled = True


def take_violations() -> list[str]:
    out = list(violations)
    violations.clear()
    return out
