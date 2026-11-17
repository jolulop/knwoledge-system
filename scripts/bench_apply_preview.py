#!/usr/bin/env python3
"""Apply preview benchmark runner (ADR-0074 change 7c).

Usage:
    uv run python scripts/bench_apply_preview.py run FIXTURE --profile heavy|light [options]

FIXTURE is a directory built by `scripts/bench_fixture.py build` (it holds `fixture.json` and the
profile vaults). The fixture is never modified: every repetition restores a benchmark-owned work copy
from the profile vault, flushes it (`os.sync`), then measures the **real** preview path
(`app.backend.main.dry_run_apply`, the body of `POST /reviews/apply/dry-run`).

Before anything is created or measured (7c-1 review), the runner refuses (exit 3) a fixture whose
declaration schema is not the current one, a profile vault that is not a real directory or holds a
symbolic link or a non-regular file, and a profile whose content no longer matches the fingerprint
declared at build time. It records whether the code copied into the fixture (which the preview's
sandbox executes) equals the code executing the runner; a difference blocks acceptance.

Scratch ownership: each run creates its own directory `FIXTURE/_work/<profile>-<random>` (exclusive
`mkdtemp`, mode 0700, with an ownership marker naming the run). `FIXTURE/_work` must be a real
directory owned by the user, never a link. The runner deletes only inside its own run directory,
after checking the marker, that no path component is a link and that the target is a real
directory; concurrent runners never touch each other's directories, and a leftover directory from
an interrupted run is never deleted automatically. Each restored work copy must match the declared
fingerprint (hashed outside every timed interval), or the trial is invalid.

Measured sets (7c Q2, Q4):
- **warm** — `--warmup` untimed runs, then `--warm` measured runs: lock wait / hold, end to end and
  the phases, from the preview's own monotonic `timings`.
- **contention** — `--contention` trials. Each restores the vault; a separate keeper process waits for
  the preview's sandbox directory, then issues a real keep through `POST /reviews/sources/{id}/links`
  on an undecided suggestion and times it. A trial counts only if the keep attempt provably started
  while the snapshot held the lock (comparable CLOCK_MONOTONIC readings). Plus `--uncontended` keeps
  with no preview, as a baseline.
- **storage** — `--storage` runs with a sampler process walking the benchmark-owned temporary
  directory every `--interval-ms` (allocated = st_blocks x 512, logical = st_size). The judged figure
  is the maximum observed allocation; sampling can miss a shorter peak (reported as a limitation).
- **cold** — `--cold` runs after dropping the page cache, only if `sudo -n` allows it; never judged.

Every trial checks validity (status `ok`, a token, an `applied` outcome for every queued approval, the
overlap for contention); invalid trials are reported and excluded. The report goes to
`evals/reports/bench/` (gitignored), with no absolute path, and its sha256 digest is printed.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPORT_SCHEMA = 1
REPORT_DIR = ROOT / "evals" / "reports" / "bench"
SANDBOX_PREFIX = "ks-dryrun-"

# ADR-0074 7c Q2 — fixed before measuring. Times are judged at the nearest-rank p95 of the valid warm
# (or contention) runs; storage at the maximum observed peak.
LIMITS = {"lock_held_ms": 2000.0, "keep_during_copy_ms": 3000.0, "end_to_end_ms": 60000.0,
          "storage_bytes": 5 * 1024 ** 3, "storage_free_fraction": 0.10}
REQUIRED_VALID_RUNS = 20
BASELINE_SCALE = 600
# The settings a judged run must use (7c Q2; review of 7c-1): an untimed warm-up, 20 warm and 20
# contention trials (so 20 valid ones are possible), storage runs, and the sampling interval.
REQUIRED_VALID_WARMUPS = 1
REQUIRED_VALID_STORAGE_RUNS = 5        # confirmed by the user (7c-1 review round 2)
BASELINE_SETTINGS_MIN = {"warmup": REQUIRED_VALID_WARMUPS, "warm": REQUIRED_VALID_RUNS,
                         "contention": REQUIRED_VALID_RUNS, "storage": REQUIRED_VALID_STORAGE_RUNS}
BASELINE_INTERVAL_MS_MAX = 50
DECLARATION_SCHEMA = 2                 # must equal bench_fixture.DECLARATION_SCHEMA
RUN_MARKER = ".bench-run.json"
FOLLOW_UP = "preview storage optimization grill (ADR-0074 deferred table)"
PERCENTILE_METHOD = ("nearest-rank: the value at rank ceil(0.95 * n) of the n sorted valid runs "
                     "(the 19th of 20)")
SAMPLING_LIMITATION = ("the sampler walks the temporary directory at a fixed interval; a peak "
                       "shorter than one interval plus one walk can be missed, and a walk racing "
                       "deletions can under-count")


# --- pure helpers (unit-tested) ------------------------------------------------------------------


def nearest_rank(values: list[float], pct: float = 95.0) -> float | None:
    """The nearest-rank percentile: the value at rank ceil(pct/100 * n) of the sorted values."""
    if not values:
        return None
    s = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(s)))
    return s[rank - 1]


def summarize(values: list[float]) -> dict[str, Any]:
    s = sorted(values)
    return {"n": len(s), "p95": nearest_rank(s), "median": nearest_rank(s, 50.0),
            "min": s[0] if s else None, "max": s[-1] if s else None, "values": s}


class BenchRefused(RuntimeError):
    """The runner refuses to start: nothing was created, deleted or measured."""


def settings_shortfall(settings: dict[str, Any]) -> list[str]:
    """Why `settings` fall short of the baseline settings a judged run requires (empty if none)."""
    out = [f"setting {k}={settings.get(k)} is below the required {v}"
           for k, v in BASELINE_SETTINGS_MIN.items() if (settings.get(k) or 0) < v]
    interval = settings.get("interval_ms")
    if interval is None or interval > BASELINE_INTERVAL_MS_MAX:
        out.append(f"setting interval_ms={interval} exceeds the required {BASELINE_INTERVAL_MS_MAX}")
    return out


def classify(lock_held: list[float], end_to_end: list[float], keep_contended: list[float],
             peak_alloc: int | None, free_before: int | None, *, judged: bool,
             valid_warmups: int, valid_storage_runs: int,
             blockers: list[str] | None = None,
             required: int = REQUIRED_VALID_RUNS) -> dict[str, Any]:
    """Pass / fail per limit, from valid trials only, and the verdict.

    `pass` / `fail` need a complete result: every timed limit with `required` valid runs, at least
    `REQUIRED_VALID_WARMUPS` valid warm-ups, a storage peak from at least
    `REQUIRED_VALID_STORAGE_RUNS` valid storage runs and a free-space reading, and no blocker (settings below the baseline, run errors, fixture
    code differing from the executing code). Anything less is `incomplete`: its exceedances stay
    visible as **provisional** breaches but never trigger the follow-up, which only a complete,
    valid, judged failure does (7c Q2)."""
    results: dict[str, Any] = {}

    def timed(name: str, values: list[float], limit: float) -> None:
        p95 = nearest_rank(values)
        results[name] = {"statistic": "p95", "value": p95, "limit": limit, "n": len(values),
                         "enough_runs": len(values) >= required,
                         "pass": None if p95 is None else p95 <= limit}

    timed("lock_held_ms", lock_held, LIMITS["lock_held_ms"])
    timed("keep_during_copy_ms", keep_contended, LIMITS["keep_during_copy_ms"])
    timed("end_to_end_ms", end_to_end, LIMITS["end_to_end_ms"])
    storage_limit = None
    if free_before is not None:
        storage_limit = min(LIMITS["storage_bytes"], int(LIMITS["storage_free_fraction"] * free_before))
    results["storage_bytes"] = {
        "statistic": "maximum observed peak", "value": peak_alloc, "limit": storage_limit,
        "limit_rule": "min(5 GiB, 10% of the free space measured before the runs)",
        "n": valid_storage_runs, "required": REQUIRED_VALID_STORAGE_RUNS,
        "enough_runs": valid_storage_runs >= REQUIRED_VALID_STORAGE_RUNS,
        "pass": None if peak_alloc is None or storage_limit is None else peak_alloc <= storage_limit}
    incomplete = [f"{k}: {r['n']} valid runs, {r.get('required', required)} required"
                  for k, r in results.items() if not r.get("enough_runs", True)]
    if valid_warmups < REQUIRED_VALID_WARMUPS:
        incomplete.append(f"warm-up: {valid_warmups} valid runs, {REQUIRED_VALID_WARMUPS} required")
    incomplete += [f"{k}: no valid measurement" for k, r in results.items() if r["pass"] is None]
    incomplete += list(blockers or [])
    breached = sorted(k for k, r in results.items() if r["pass"] is False)
    if not judged:
        verdict = "not_judged"
    elif incomplete:
        verdict = "incomplete"
    else:
        verdict = "fail" if breached else "pass"
    return {"limits": dict(LIMITS), "results": results, "valid_warmups": valid_warmups,
            "verdict": verdict, "breached": breached,
            "breach_status": ("confirmed" if verdict == "fail" else "provisional") if breached else None,
            "incomplete_reasons": incomplete,
            "triggered_follow_up": FOLLOW_UP if verdict == "fail" else None}


_ABS = re.compile(r"(?<![\w.<>])/[^\s'\"<>:,()\[\]/]+(?:/[^\s'\"<>:,()\[\]/]*)*")


def sanitize(value: Any, roots: list[str] | None = None) -> Any:
    """Strip absolute paths from any string inside `value` (errors, metadata)."""
    if isinstance(value, dict):
        return {k: sanitize(v, roots) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v, roots) for v in value]
    if isinstance(value, str):
        out = value
        for r in sorted(roots or [], key=len, reverse=True):
            out = out.replace(r, "<root>")
        return _ABS.sub("<path>", out)
    return value


def canonical(report: dict[str, Any]) -> bytes:
    return (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")


def digest(report: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical(report)).hexdigest()


def trial_validity(result: dict[str, Any], expected_ids: set[str]) -> str | None:
    """None if a preview result is a valid trial, else the reason it is invalid."""
    if result.get("status") != "ok":
        return f"status_{result.get('status')}"
    if "timings" not in result or "lock_held" not in result["timings"].get("phases_ms", {}):
        return "no_timings"
    got = {o["review_id"]: o for o in result.get("outcomes") or [] if o["review_id"] in expected_ids}
    if set(got) != expected_ids:
        return "outcome_missing"
    for o in got.values():
        if o["outcome"] != "applied" or (o.get("details") or {}).get("normalized"):
            return f"outcome_{o['outcome']}"
    return None


def overlap(marks: dict[str, int], keep_start_ns: int) -> bool:
    """The keep attempt started while the snapshot held the write lock."""
    return marks.get("lock_acquired", 1 << 62) <= keep_start_ns <= marks.get("lock_released", -1)


# --- fixture verification and owned scratch space (7c-1 review) ---------------------------------


def _fx() -> Any:
    """The generator module: its fingerprint functions define the fixture's content identity."""
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    import bench_fixture
    return bench_fixture


def _real_dir(path: Path, what: str) -> None:
    """`path` is a directory owned by this user, not a link (checked without following it)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise BenchRefused(f"{what} does not exist") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise BenchRefused(f"{what} is not a real directory (a link or another file type)")
    if st.st_uid != os.getuid():
        raise BenchRefused(f"{what} is not owned by this user")


def check_pristine(fixture: Path, profile: str) -> Path:
    """The profile vault: a real directory directly under the fixture, holding only real
    directories and regular files (no link can lead a restore or a preview outside it)."""
    if profile not in ("heavy", "light"):
        raise BenchRefused(f"unknown profile {profile!r}")
    pristine = fixture / profile
    _real_dir(pristine, f"the {profile} profile vault")
    if pristine.resolve() != pristine:
        raise BenchRefused(f"the {profile} profile vault resolves outside the fixture")
    for dirpath, dirnames, filenames in os.walk(pristine):
        for name in dirnames + filenames:
            mode = os.lstat(os.path.join(dirpath, name)).st_mode
            if stat.S_ISLNK(mode):
                raise BenchRefused(f"the {profile} profile vault contains a symbolic link")
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise BenchRefused(f"the {profile} profile vault contains a non-regular file")
    return pristine


def verify_fixture(fixture: Path, profile: str, decl: dict[str, Any]) -> dict[str, Any]:
    """Refuses a fixture that is not what its declaration says; returns the provenance record:
    the verified content fingerprint and the fixture / executing code fingerprints."""
    fx = _fx()
    if decl.get("schema_version") != DECLARATION_SCHEMA or fx.DECLARATION_SCHEMA != DECLARATION_SCHEMA:
        raise BenchRefused(f"fixture declaration schema {decl.get('schema_version')!r} is not "
                           f"{DECLARATION_SCHEMA}; rebuild the fixture")
    pdecl = (decl.get("profiles") or {}).get(profile)
    if not pdecl or not pdecl.get("fingerprint"):
        raise BenchRefused(f"the fixture declares no {profile} profile fingerprint")
    pristine = check_pristine(fixture, profile)
    t0 = time.monotonic()
    got = fx.content_fingerprint(pristine)
    if got != pdecl["fingerprint"]:
        raise BenchRefused(f"the {profile} profile's content does not match its declared fingerprint "
                           "(modified after the build); rebuild the fixture")
    fixture_code = fx.code_fingerprint(pristine)
    executing_code = fx.code_fingerprint(ROOT)
    return {"fingerprint": got, "fingerprint_verified": True,
            "verify_seconds": round(time.monotonic() - t0, 2),
            "code": {"fixture": fixture_code, "executing": executing_code,
                     "declared": decl.get("code_fingerprint"),
                     "match": fixture_code == executing_code}}


def open_work(fixture: Path, profile: str) -> tuple[Path, str]:
    """Creates this run's own scratch directory under `FIXTURE/_work` and its ownership marker."""
    base = fixture / "_work"
    for _ in range(3):
        try:
            os.mkdir(base, 0o700)             # never follows a link at `_work`: it already exists
        except FileExistsError:
            pass
        _real_dir(base, "the fixture's _work directory")
        if base.resolve() != base:
            raise BenchRefused("the fixture's _work directory resolves elsewhere")
        try:
            run_dir = Path(tempfile.mkdtemp(prefix=f"{profile}-", dir=base))
            break
        except FileNotFoundError:             # a concurrent runner removed the empty `_work`
            continue
    else:
        raise BenchRefused("could not create a run directory under _work")
    run_id = run_dir.name
    with open(run_dir / RUN_MARKER, "x", encoding="utf-8") as f:
        json.dump({"run_id": run_id, "pid": os.getpid(), "profile": profile}, f)
    return run_dir, run_id


def _check_owned(run_dir: Path, run_id: str) -> None:
    _real_dir(run_dir, "the run directory")
    if run_dir.resolve() != run_dir or run_dir.name != run_id:
        raise BenchRefused("the run directory is not this run's")
    marker = run_dir / RUN_MARKER
    try:
        st = os.lstat(marker)
        owner = json.loads(marker.read_text(encoding="utf-8")) if stat.S_ISREG(st.st_mode) else {}
    except (OSError, ValueError):
        owner = {}
    if owner.get("run_id") != run_id:
        raise BenchRefused("the run directory's ownership marker does not name this run")


def remove_owned(target: Path, run_dir: Path, run_id: str) -> None:
    """Deletes `target` only if it is this run's directory or a real directory inside it, reached
    through no link. Anything else is refused, never deleted."""
    _check_owned(run_dir, run_id)
    if target != run_dir and run_dir not in target.parents:
        raise BenchRefused("refusing to delete outside this run's directory")
    if not os.path.lexists(target):
        return
    if target.resolve() != target:
        raise BenchRefused("refusing to delete through a symbolic link")
    _real_dir(target, "the deletion target")
    if not shutil.rmtree.avoids_symlink_attacks:
        raise BenchRefused("this platform's rmtree is not safe against symlink attacks")
    shutil.rmtree(target)


def close_work(run_dir: Path, run_id: str) -> None:
    """Removes this run's directory, then `_work` itself only if it is now empty."""
    remove_owned(run_dir, run_dir, run_id)
    try:
        run_dir.parent.rmdir()             # fails harmlessly while another run's directory exists
    except OSError:
        pass


# --- child roles (each a separate process bound to the work vault) --------------------------------


def _reply(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _restore(pristine: Path, run_dir: Path, run_id: str) -> tuple[Path, float]:
    vault = run_dir / "vault"
    t0 = time.monotonic()
    remove_owned(vault, run_dir, run_id)
    shutil.copytree(pristine, vault, symlinks=True)   # a link is copied as a link, never followed
    os.sync()
    return vault, round((time.monotonic() - t0) * 1000.0, 1)


def _vault_state(vault: Path) -> dict[str, Any]:
    from app.backend import revision
    rev = revision.current(vault)
    approved = sorted(p.name for p in (vault / "reviews" / "approved").glob("*.json"))
    return {"revision": [rev["epoch"], rev["counter"]],
            "approved": hashlib.sha256("\n".join(approved).encode()).hexdigest()}


def role_driver(pristine: Path, run_dir: Path, run_id: str) -> None:
    """Process A: restores the vault (then hashes it, outside every timed interval) and runs the
    real preview on command."""
    from app.backend import main as api
    fx = _fx()
    for line in sys.stdin:
        cmd = json.loads(line)
        try:
            if cmd["op"] == "restore":
                vault, ms = _restore(pristine, run_dir, run_id)
                t0 = time.monotonic()
                fp = fx.content_fingerprint(vault)
                _reply({"ok": True, "restore_ms": ms, "state": _vault_state(vault),
                        "fingerprint": fp, "verify_ms": round((time.monotonic() - t0) * 1000.0, 1)})
            elif cmd["op"] == "preview":
                t0 = time.monotonic_ns()
                res = api.dry_run_apply()
                t1 = time.monotonic_ns()
                _reply({"ok": True, "wall_ns": [t0, t1], "status": res.get("status"),
                        "reason": res.get("reason"), "token": bool(res.get("preview_token")),
                        "token_withheld": res.get("token_withheld"),
                        "timings": res.get("timings"), "outcomes": res.get("outcomes") or [],
                        "warnings": res.get("warnings") or [],
                        "validators": res.get("validators") or {}})
            elif cmd["op"] == "logical_bytes":
                _reply({"ok": True, "bytes": _logical_copied_bytes(api.settings)})
            elif cmd["op"] == "quit":
                _reply({"ok": True})
                return
        except Exception as exc:  # noqa: BLE001 - reported (sanitized) as an invalid trial
            _reply({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def _logical_copied_bytes(st: Any) -> int:
    """The bytes a preview copies (the same selection as `apply_sandbox`): db files except the
    never-copied ones, the state directories, the code and the catalogued raw files."""
    from app.backend import apply_sandbox
    from app.backend.paths import safe_under
    root = Path(st.root)
    total = 0
    for p in (root / "db").iterdir() if (root / "db").exists() else []:
        if p.is_file() and p.name not in apply_sandbox._DB_NEVER_COPIED \
                and not p.name.endswith(apply_sandbox._SQLITE_SIDE):
            total += p.stat().st_size
    for rel in apply_sandbox._COPY_STATE + apply_sandbox._COPY_CODE:
        base = root / rel
        if base.exists():
            total += sum(p.stat().st_size for p in base.rglob("*")
                         if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    for rel in apply_sandbox._catalogued_raw_rels(root / "raw" / "manifests"):
        src = safe_under(root, root / "raw", rel)
        if src is not None and src.is_file():
            total += src.stat().st_size
    return total


def role_keeper(tmpdir: Path) -> None:
    """Process B: on `arm`, waits for the preview's sandbox directory, then issues a real keep; on
    `keep`, keeps at once (the uncontended baseline)."""
    from fastapi.testclient import TestClient

    from app.backend import main as api
    client = TestClient(api.app, base_url="http://127.0.0.1")
    for line in sys.stdin:
        cmd = json.loads(line)
        if cmd["op"] == "quit":
            _reply({"ok": True})
            return
        try:
            if cmd["op"] == "arm":
                _reply({"ok": True, "armed": True})
                deadline = time.monotonic() + cmd.get("timeout_s", 600)
                seen = False
                while time.monotonic() < deadline:
                    with os.scandir(tmpdir) as it:
                        if any(e.name.startswith(SANDBOX_PREFIX) for e in it):
                            seen = True
                            break
                    time.sleep(0.0005)
                if not seen:
                    _reply({"ok": False, "error": "sandbox_never_appeared"})
                    continue
            t0 = time.monotonic_ns()
            resp = client.post(f"/reviews/sources/{cmd['source_id']}/links",
                               json={"node_id": cmd["node_id"], "action": "keep"})
            t1 = time.monotonic_ns()
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            _reply({"ok": True, "start_ns": t0, "end_ns": t1, "http": resp.status_code,
                    "outcome": body.get("outcome"), "activated": body.get("activated")})
        except Exception as exc:  # noqa: BLE001
            _reply({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def _walk_bytes(path: Path) -> tuple[int, int]:
    alloc = logical = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for name in filenames + dirnames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            alloc += st.st_blocks * 512
            logical += st.st_size if name in filenames else 0
    return alloc, logical


def role_sampler(tmpdir: Path) -> None:
    """The storage sampler: walks the benchmark-owned temporary directory between `start` and
    `stop`, from its own process (never holding the write lock)."""
    for line in sys.stdin:
        cmd = json.loads(line)
        if cmd["op"] == "quit":
            _reply({"ok": True})
            return
        if cmd["op"] != "start":
            continue
        interval = cmd["interval_ms"] / 1000.0
        cpu0 = time.process_time()
        samples: list[tuple[int, int, int]] = []
        walks: list[float] = []
        _reply({"ok": True, "started": True})
        while True:
            w0 = time.monotonic_ns()
            alloc, logical = _walk_bytes(tmpdir)
            w1 = time.monotonic_ns()
            samples.append((w1, alloc, logical))
            walks.append((w1 - w0) / 1e6)
            r, _, _ = select.select([sys.stdin], [], [], interval)
            if r:
                sys.stdin.readline()          # "stop"
                break
        peak = max(samples, key=lambda s: s[1])
        _reply({"ok": True, "samples": len(samples), "peak_alloc": peak[1], "peak_ns": peak[0],
                "peak_logical": max(s[2] for s in samples),
                "mean_walk_ms": round(sum(walks) / len(walks), 3), "max_walk_ms": round(max(walks), 3),
                "cpu_s": round(time.process_time() - cpu0, 3)})


# --- orchestration -------------------------------------------------------------------------------


class _Child:
    def __init__(self, role: str, env: dict[str, str], *args: str) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "_role", role, *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env, cwd=str(ROOT))

    def send(self, obj: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def recv(self) -> dict[str, Any]:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("child process exited")
        return json.loads(line)

    def call(self, obj: dict[str, Any]) -> dict[str, Any]:
        self.send(obj)
        return self.recv()

    def close(self) -> None:
        try:
            self.call({"op": "quit"})
        except Exception:  # noqa: BLE001
            pass
        self.proc.wait(timeout=60)


def _drop_caches() -> bool:
    proc = subprocess.run(["sudo", "-n", "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return proc.returncode == 0


def _fs_type(path: Path) -> str | None:
    """The filesystem type holding `path` (from /proc/mounts; the mount point is not reported)."""
    try:
        best, kind = "", None
        target = str(path.resolve())
        for line in Path("/proc/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 3 and (target == parts[1] or target.startswith(parts[1].rstrip("/") + "/")):
                if len(parts[1]) >= len(best):
                    best, kind = parts[1], parts[2]
        return kind
    except OSError:
        return None


def machine(tmpdir: Path, vault: Path) -> dict[str, Any]:
    cpu = None
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    mem = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                mem = int(line.split()[1]) * 1024
    except OSError:
        pass
    return {"cpu_model": cpu, "cpu_count": os.cpu_count(), "mem_total_bytes": mem,
            "kernel": platform.release(), "system": platform.system(),
            "python": platform.python_version(), "tmp_fs_type": _fs_type(tmpdir),
            "vault_fs_type": _fs_type(vault), "wsl": "microsoft" in platform.release().lower()}


def code_revision() -> dict[str, Any]:
    def git(*a: str) -> str:
        return subprocess.run(["git", *a], cwd=str(ROOT), stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, check=False).stdout.strip()
    dirty = bool(git("status", "--porcelain", "--", "app", "scripts", "templates"))
    return {"commit": git("rev-parse", "HEAD") or None, "dirty_code": dirty}


def run(fixture: Path, profile: str, *, warmup: int, warm: int, contention: int, uncontended: int,
        storage: int, cold: int, interval_ms: int, out_dir: Path = REPORT_DIR) -> tuple[Path, str]:
    fixture = fixture.resolve()
    try:
        decl = json.loads((fixture / "fixture.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchRefused(f"unreadable fixture declaration ({type(exc).__name__})") from None
    # Everything is verified before anything is created: a refusal leaves the fixture untouched.
    provenance = verify_fixture(fixture, profile, decl)
    pdecl = decl["profiles"][profile]
    pristine = fixture / profile
    try:
        expected = {rid for v in pdecl["review_ids"].values() for rid in v}
        targets = list(pdecl["contention_targets"])
    except (KeyError, AttributeError, TypeError):
        raise BenchRefused(f"the fixture's {profile} declaration has no review ids or contention "
                           "targets") from None
    if (contention or uncontended) and not targets:
        raise BenchRefused(f"the fixture's {profile} declaration has no contention targets")
    run_dir, run_id = open_work(fixture, profile)
    vault, tmpdir = run_dir / "vault", run_dir / "tmp"
    machine_info: dict[str, Any] = {}
    env = {k: v for k, v in os.environ.items()
           if not re.search(r"API_KEY|SECRET|TOKEN|PASSWORD", k, re.IGNORECASE)}
    env.update(KNOWLEDGE_SYSTEM_HOME=str(vault), TMPDIR=str(tmpdir))
    roots = [str(fixture), str(ROOT), str(Path.home())]
    errors: list[str] = []
    trials: list[dict[str, Any]] = []
    driver = keeper = sampler = None

    def preview_trial(kind: str, *, with_keep: dict[str, str] | None = None,
                      with_sampler: bool = False) -> dict[str, Any]:
        assert driver is not None
        rs = driver.call({"op": "restore"})
        if not rs["ok"]:
            raise RuntimeError(rs["error"])
        if rs["fingerprint"] != provenance["fingerprint"]:
            return {"set": kind, "valid": False, "reason": "restored_content_mismatch",
                    "restore_ms": rs["restore_ms"], "state": rs["state"]}
        free = shutil.disk_usage(tmpdir).free
        if kind == "cold" and not _drop_caches():
            return {"set": kind, "valid": False, "reason": "drop_caches_unavailable"}
        if with_sampler:
            assert sampler is not None
            sampler.call({"op": "start", "interval_ms": interval_ms})
        if with_keep:
            assert keeper is not None
            keeper.call({"op": "arm", **with_keep})
        res = driver.call({"op": "preview"})
        keep = keeper.recv() if with_keep and keeper is not None else None
        samp = sampler.call({"op": "stop"}) if with_sampler and sampler is not None else None
        t: dict[str, Any] = {"set": kind, "free_bytes_before": free, "restore_ms": rs["restore_ms"],
                             "state": rs["state"]}
        if not res.get("ok"):
            t.update(valid=False, reason="driver_error", error=res.get("error"))
            return t
        reason = trial_validity(res, expected)
        tim = res.get("timings") or {}
        t.update(status=res["status"], token=res["token"], token_withheld=res["token_withheld"],
                 phases_ms=tim.get("phases_ms"), sandbox_phases_ms=tim.get("sandbox_phases_ms"),
                 wall_ms=round((res["wall_ns"][1] - res["wall_ns"][0]) / 1e6, 3))
        if with_keep:
            if keep is None or not keep.get("ok"):
                reason = reason or "keep_error"
                t["keep_error"] = (keep or {}).get("error")
            else:
                t["keep_ms"] = round((keep["end_ns"] - keep["start_ns"]) / 1e6, 3)
                t["keep_outcome"] = keep.get("outcome")
                t["overlap"] = overlap(tim.get("marks_ns") or {}, keep["start_ns"])
                if keep.get("http") != 200 or keep.get("outcome") != "kept":
                    reason = reason or f"keep_not_real_{keep.get('outcome')}"
                elif not t["overlap"]:
                    reason = reason or "keep_did_not_overlap_lock"
                # A keep may legitimately invalidate the preview's authorization (7c Q4): recorded.
                t["token_outcome"] = "issued" if res["token"] else res["token_withheld"]
        elif reason is None and not res["token"]:
            reason = f"no_token_{res['token_withheld']}"
        if samp is not None:
            t["storage"] = {k: samp.get(k) for k in ("samples", "peak_alloc", "peak_logical",
                                                     "mean_walk_ms", "max_walk_ms", "cpu_s")}
            marks = tim.get("marks_ns") or {}
            t["storage"]["peak_phase"] = _phase_at(marks, samp.get("peak_ns"))
        t.update(valid=reason is None, reason=reason)
        return t

    try:
        os.mkdir(tmpdir, 0o700)
        machine_info = machine(tmpdir, fixture)
        driver = _Child("driver", env, str(pristine), str(run_dir), run_id)
        rs = driver.call({"op": "restore"})
        if not rs.get("ok"):
            raise RuntimeError(rs.get("error"))
        logical = driver.call({"op": "logical_bytes"})["bytes"]
        if contention or uncontended:
            keeper = _Child("keeper", env, str(tmpdir))
        if storage:
            sampler = _Child("sampler", env, str(tmpdir))
        for _ in range(warmup):               # untimed, but its validity is reported and required
            trials.append(preview_trial("warmup"))
        for _ in range(warm):
            trials.append(preview_trial("warm"))
        for i in range(contention):
            trials.append(preview_trial("contention", with_keep=targets[i % len(targets)]))
        for i in range(uncontended):
            rs = driver.call({"op": "restore"})
            if not rs.get("ok"):
                raise RuntimeError(rs.get("error"))
            if rs["fingerprint"] != provenance["fingerprint"]:
                trials.append({"set": "uncontended_keep", "valid": False,
                               "reason": "restored_content_mismatch"})
                continue
            assert keeper is not None
            k = keeper.call({"op": "keep", **targets[i % len(targets)]})
            ok = k.get("ok") and k.get("http") == 200 and k.get("outcome") == "kept"
            trials.append({"set": "uncontended_keep", "valid": bool(ok), "state": rs["state"],
                           "reason": None if ok else f"keep_not_real_{k.get('outcome')}",
                           "keep_ms": round((k["end_ns"] - k["start_ns"]) / 1e6, 3) if k.get("ok") else None})
        for _ in range(storage):
            trials.append(preview_trial("storage", with_sampler=True))
        for _ in range(cold):
            trials.append(preview_trial("cold"))
    except Exception as exc:  # noqa: BLE001 - the report records the failure (sanitized)
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        for child in (driver, keeper, sampler):
            if child is not None:
                child.close()
        try:
            close_work(run_dir, run_id)
        except BenchRefused as exc:     # never forces a deletion it cannot prove is its own
            errors.append(f"scratch not removed: {exc}")
    report = build_report(decl, profile, trials, errors, logical_bytes=locals().get("logical"),
                          settings={"warmup": warmup, "warm": warm, "contention": contention,
                                    "uncontended": uncontended, "storage": storage, "cold": cold,
                                    "interval_ms": interval_ms},
                          machine_info=machine_info, revision=code_revision(),
                          provenance=provenance)
    report = sanitize(report, roots)
    out_dir.mkdir(parents=True, exist_ok=True)
    dg = digest(report)
    path = out_dir / f"{time.strftime('%Y%m%dT%H%M%S')}-{profile}-{dg[7:19]}.json"
    path.write_bytes(canonical(report))
    return path, dg


def _phase_at(marks: dict[str, int], t_ns: int | None) -> str | None:
    if t_ns is None or not marks:
        return None
    order = sorted(marks.items(), key=lambda kv: kv[1])
    current = None
    for name, ts in order:
        if ts <= t_ns:
            current = name
    return f"after:{current}" if current else "before:started"


def build_report(decl: dict[str, Any], profile: str, trials: list[dict[str, Any]],
                 errors: list[str], *, logical_bytes: int | None, settings: dict[str, Any],
                 machine_info: dict[str, Any], revision: dict[str, Any],
                 provenance: dict[str, Any]) -> dict[str, Any]:
    # 7c Q5: every repetition starts from equivalent fixture state (same revision, same approved
    # queue); a trial that did not is invalid.
    states = [t["state"] for t in trials if t.get("state")]
    for t in trials:
        if t.get("valid") and t.get("state") and states and t["state"] != states[0]:
            t.update(valid=False, reason="state_not_equivalent")
    valid = [t for t in trials if t.get("valid")]

    def vals(kind: str, key: str) -> list[float]:
        return [t["phases_ms"][key] for t in valid if t["set"] == kind and t.get("phases_ms")
                and key in t["phases_ms"]]
    warm = [t for t in valid if t["set"] == "warm"]
    lock_held = vals("warm", "lock_held")
    e2e = vals("warm", "total")
    keep_contended = [t["keep_ms"] for t in valid if t["set"] == "contention"]
    keep_uncontended = [t["keep_ms"] for t in valid if t["set"] == "uncontended_keep"]
    storage = [t for t in valid if t["set"] == "storage" and t.get("storage")]
    peak = max((t["storage"]["peak_alloc"] for t in storage), default=None)
    peak_logical = max((t["storage"]["peak_logical"] for t in storage), default=None)
    free = min((t["free_bytes_before"] for t in trials if "free_bytes_before" in t), default=None)
    phases = sorted({k for t in warm for k in (t.get("phases_ms") or {})})
    sphases = sorted({k for t in warm for k in (t.get("sandbox_phases_ms") or {})})
    storage_e2e = [t["phases_ms"]["total"] for t in storage if t.get("phases_ms")]
    invalid: dict[str, int] = {}
    for t in trials:
        if not t.get("valid"):
            invalid[f"{t['set']}:{t.get('reason')}"] = invalid.get(f"{t['set']}:{t.get('reason')}", 0) + 1
    # Only the heavy profile at the baseline preset and the 600-document target is judged (7c Q2).
    judged = (profile == "heavy" and decl.get("preset") == "baseline"
              and (decl.get("scale") or 0) >= BASELINE_SCALE)
    pdecl = decl["profiles"][profile]
    blockers = settings_shortfall(settings) + [f"run error: {e}" for e in errors]
    if not provenance["code"]["match"]:
        blockers.append("the fixture's copied code differs from the executing code; rebuild the "
                        "fixture from this revision")
    return {
        "report_schema": REPORT_SCHEMA,
        "label": decl.get("label"), "profile": profile,
        "profile_label": ("backlog stress baseline (judged)" if profile == "heavy"
                          else "small interactive batch (not judged)"),
        "judged": judged, "per_preview": True,
        "fixture": {"generator_version": decl.get("generator_version"), "seed": decl.get("seed"),
                    "scale": decl.get("scale"), "preset": decl.get("preset"),
                    "fingerprint": provenance["fingerprint"],
                    "fingerprint_verified": provenance["fingerprint_verified"],
                    "restored_trials_verified": "every restore is hashed and must match the "
                                                "fingerprint, or the trial is invalid",
                    "verify_seconds": provenance["verify_seconds"],
                    "corpus_fingerprint": decl.get("corpus_fingerprint"),
                    "declaration_schema": decl.get("schema_version"),
                    "queue_counts_by_type": pdecl.get("counts_by_type"),
                    "queue_total": pdecl.get("total")},
        "code_revision": revision, "code_fingerprints": provenance["code"], "machine": machine_info, "settings": settings,
        "percentile_method": PERCENTILE_METHOD,
        "metrics": {
            "lock_held_ms": summarize(lock_held), "lock_wait_ms": summarize(vals("warm", "lock_wait")),
            "end_to_end_ms": summarize(e2e),
            "keep_during_copy_ms": summarize(keep_contended),
            "keep_uncontended_ms": summarize(keep_uncontended),
            "phases_ms_p95": {k: nearest_rank(vals("warm", k)) for k in phases},
            "sandbox_phases_ms_p95": {k: nearest_rank([t["sandbox_phases_ms"][k] for t in warm
                                                       if k in (t.get("sandbox_phases_ms") or {})])
                                      for k in sphases},
            "cold_end_to_end_ms": summarize(vals("cold", "total")),
        },
        "storage": {"max_observed_allocated_bytes": peak, "max_observed_logical_bytes": peak_logical,
                    "logical_copied_bytes": logical_bytes,
                    "amplification": (round(peak / logical_bytes, 4)
                                      if peak and logical_bytes else None),
                    "amplification_definition": "maximum observed allocated temporary storage divided "
                                                "by the logical bytes the preview copies",
                    "free_bytes_before_min": free,
                    "peak_phases": sorted({t["storage"].get("peak_phase") for t in storage}
                                          - {None}),
                    "sampling": {"interval_ms": settings["interval_ms"],
                                 "samples": sum(t["storage"]["samples"] for t in storage),
                                 "mean_walk_ms": (round(sum(t["storage"]["mean_walk_ms"] for t in storage)
                                                        / len(storage), 3) if storage else None),
                                 "limitation": SAMPLING_LIMITATION}},
        "overhead": {"sampler_cpu_s_total": round(sum(t["storage"]["cpu_s"] for t in storage), 3),
                     "storage_runs_e2e_median_ms": nearest_rank(storage_e2e, 50.0),
                     "warm_runs_e2e_median_ms": nearest_rank(e2e, 50.0),
                     "note": "storage runs carry the sampler; warm runs carry only the additive "
                             "phase timings"},
        "contention": {"token_outcomes": sorted({str(t.get("token_outcome")) for t in valid
                                                 if t["set"] == "contention"}),
                       "keep_outcomes": sorted({str(t.get("keep_outcome")) for t in trials
                                                if t["set"] == "contention"})},
        "cold": {"available": any(t["set"] == "cold" and t.get("valid") for t in trials),
                 "judged": False},
        "trials": {"total": len(trials), "valid": len(valid), "invalid_by_reason": invalid,
                   "detail": trials},
        "acceptance": classify(lock_held, e2e, keep_contended, peak, free, judged=judged,
                               valid_warmups=sum(1 for t in valid if t["set"] == "warmup"),
                               valid_storage_runs=len(storage), blockers=blockers),
        "errors": errors,
    }


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "_role":
        role, args = argv[1], argv[2:]
        if role == "driver":
            role_driver(Path(args[0]), Path(args[1]), args[2])
        elif role == "keeper":
            role_keeper(Path(args[0]))
        elif role == "sampler":
            role_sampler(Path(args[0]))
        return 0
    if len(argv) < 2 or argv[0] != "run":
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    opts = {"--profile": "heavy", "--warmup": "1", "--warm": "20", "--contention": "20",
            "--uncontended": "5", "--storage": "5", "--cold": "3", "--interval-ms": "50"}
    rest = argv[2:]
    while rest:
        if rest[0] not in opts or len(rest) < 2:
            print(f"unknown or incomplete option {rest[0]!r}", file=sys.stderr)
            return 2
        opts[rest[0]] = rest[1]
        rest = rest[2:]
    try:
        path, dg = run(Path(argv[1]), opts["--profile"], warmup=int(opts["--warmup"]),
                       warm=int(opts["--warm"]), contention=int(opts["--contention"]),
                       uncontended=int(opts["--uncontended"]), storage=int(opts["--storage"]),
                       cold=int(opts["--cold"]), interval_ms=int(opts["--interval-ms"]))
    except BenchRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    report = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps({"report": path.relative_to(ROOT).as_posix(), "digest": dg,
                      "verdict": report["acceptance"]["verdict"],
                      "breached": report["acceptance"]["breached"],
                      "breach_status": report["acceptance"]["breach_status"],
                      "incomplete_reasons": report["acceptance"]["incomplete_reasons"],
                      "valid_trials": report["trials"]["valid"],
                      "invalid": report["trials"]["invalid_by_reason"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
