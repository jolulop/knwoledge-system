"""The roll-forward install journal of extraction (ADR-0074 decision 8 and 7.4, slice 5a).

An extraction attempt stages its complete generation — the document's Markdown, chunks and table
files — durably in `normalized/.staging/<source_id>-<attempt_id>/`, outside the write lock. Under
the write lock (after its publication intent is queued) it writes a per-document **install journal**
`normalized/.install/<source_id>.json` and only then moves files into place. The journal binds the
attempt to the source checksum and the extraction configuration and records the **exact output
inventory** (each file's size and sha256, an empty table set included), the obsolete tables, the
extraction log to write and the extraction-owned manifest fields to apply.

Stages: ``installing`` (renames in progress; the outputs are not usable) → ``installed`` (persisted
only after every output, the log and the manifest fields are verified and durable; readers may use
the generation) → closed (the journal removed after the document's publication succeeded). A
generation that can't be completed is marked ``unrecoverable``: the journal and every remaining
artifact are preserved, the document stays blocked, and only a fresh successful attempt supersedes
it (its journal lists the superseded attempts, whose artifacts are retired once it is installed).

Every step verifies bytes, not presence: a staged file is moved only when its size and sha256 match
the journal, an already-installed file counts only when its bytes match. Table entries not in the
new inventory move into a journal-named holding directory `normalized/.install/<source_id>-
<attempt_id>.holding/`, deleted once the generation is installed. Every directory a rename touched
is fsynced (:func:`app.backend.durable_write.move`), and again at the end of every install, so a
recovery after a failed directory sync repeats the barrier. Paths are never read from a journal:
they derive from the validated source id, attempt id and table names.

Recovery (:func:`recover`) runs at extraction start, under the write lock, before any new attempt:
it completes every ``installing`` journal (refusing an incompatible one — another checksum or
configuration — as ``unrecoverable``), retires and publishes every ``installed`` one, and leaves
``unrecoverable`` and damaged journals in place. Staging cleanup (:func:`cleanup_staging`) then
removes a staging or holding directory only when a complete, readable journal inventory proves no
journal refers to it; any damaged journal blocks all cleanup.

Consumers (slice 5b) read a document's normalized outputs through :func:`consistent_read` (or
:func:`read_text`): the journal check and the read happen under the write lock together, so they
can't interleave with an install; a document whose journal is not usable is skipped as
:data:`INCOMPLETE`. Exclusion gates over bytes read elsewhere use :func:`usable`.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from collections.abc import Iterator
from typing import Any, Callable

from app.backend import authority_read, durable_write, manifests, write_lock

JOURNAL_VERSION = 1
INSTALLING, INSTALLED, UNRECOVERABLE = "installing", "installed", "unrecoverable"
STAGES = (INSTALLING, INSTALLED, UNRECOVERABLE)
ABSENT, UNREADABLE, CORRUPT = authority_read.ABSENT, authority_read.UNREADABLE, authority_read.CORRUPT
# Journal states under which a document's normalized outputs may be read.
USABLE = frozenset({ABSENT, INSTALLED})
INCOMPLETE = "extraction_install_incomplete"           # the skip reason consumers report
# The extraction configuration a generation is bound to (decision 8): what shapes its bytes.
CONFIG_KEYS = ("extract_code_version", "target_chars", "max_chars")

INSTALL_DIR, STAGING_DIR, HOLDING_SUFFIX = ".install", ".staging", ".holding"
_ATTEMPT = re.compile(r"att_[0-9a-f]{12}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_INTENT = re.compile(r"pub_[0-9a-f]{12}")
_TABLE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,127}")
_FIELDS = ("version", "source_id", "attempt_id", "state", "reason", "source_sha256", "config",
           "intent", "outputs", "obsolete_tables", "log", "log_sha256", "manifest_fields",
           "supersedes", "created_at", "updated_at")


class InstallBlocked(RuntimeError):
    """The install can't proceed now and the journal stays as it is (e.g. the manifest is damaged)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Layout:
    """The normalized layer's directories (`normalized_dir` may be configured elsewhere)."""
    normalized: Path

    @property
    def markdown(self) -> Path:
        return self.normalized / "markdown"

    @property
    def chunks(self) -> Path:
        return self.normalized / "chunks"

    @property
    def tables(self) -> Path:
        return self.normalized / "tables"

    @property
    def logs(self) -> Path:
        return self.normalized / "extraction_logs"

    @property
    def install(self) -> Path:
        return self.normalized / INSTALL_DIR

    @property
    def staging(self) -> Path:
        return self.normalized / STAGING_DIR

    def journal(self, source_id: str) -> Path:
        manifests._require_source_id(source_id)
        return self.install / f"{source_id}.json"

    def stage(self, source_id: str, attempt_id: str) -> Path:
        return self.staging / _attempt_name(source_id, attempt_id)

    def holding(self, source_id: str, attempt_id: str) -> Path:
        return self.install / (_attempt_name(source_id, attempt_id) + HOLDING_SUFFIX)

    def outputs(self, source_id: str) -> dict[str, Path]:
        manifests._require_source_id(source_id)
        return {"markdown": self.markdown / f"{source_id}.md",
                "chunks": self.chunks / f"{source_id}.jsonl",
                "tables": self.tables / source_id,
                "log": self.logs / f"{source_id}.json"}


def _attempt_name(source_id: str, attempt_id: str) -> str:
    manifests._require_source_id(source_id)
    if not _ATTEMPT.fullmatch(attempt_id):
        raise ValueError(f"not an attempt id: {attempt_id!r}")
    return f"{source_id}-{attempt_id}"


def new_attempt_id() -> str:
    return "att_" + uuid.uuid4().hex[:12]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def log_bytes(log: dict[str, Any]) -> bytes:
    """The extraction log's canonical bytes (what the journal's `log_sha256` covers)."""
    return (json.dumps(log, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _spec(data: bytes) -> dict[str, Any]:
    return {"size": len(data), "sha256": _sha(data)}


# --- staging (outside the write lock) ----------------------------------------------------------


def stage_generation(layout: Layout, source_id: str, attempt_id: str, *, markdown: bytes,
                     chunks: bytes, tables: list[tuple[str, bytes]]) -> dict[str, Any]:
    """Write one attempt's complete generation durably into its own staging directory (files and
    directories fsynced) and return its output inventory. A failure removes the staging directory
    again (best effort: an unreferenced one is cleaned up at the next run start)."""
    stage = layout.stage(source_id, attempt_id)
    names = [name for name, _ in tables]
    if len(set(names)) != len(names) or any(not _TABLE.fullmatch(n) for n in names):
        raise ValueError("table file names must be unique and simple")
    try:
        durable_write.make_dirs(stage / "tables")
        durable_write.write_bytes(stage / "markdown.md", markdown, create_only=True)
        durable_write.write_bytes(stage / "chunks.jsonl", chunks, create_only=True)
        for name, content in tables:
            durable_write.write_bytes(stage / "tables" / name, content, create_only=True)
    except BaseException:
        discard_stage(layout, source_id, attempt_id)
        raise
    return {"markdown": _spec(markdown), "chunks": _spec(chunks),
            "tables": {name: _spec(content) for name, content in sorted(tables)}}


def discard_stage(layout: Layout, source_id: str, attempt_id: str) -> None:
    """Remove an attempt's staging directory that no journal refers to (best effort)."""
    try:
        durable_write.remove_tree(layout.stage(source_id, attempt_id))
    except OSError:
        pass                                         # cleaned up at the next run start


# --- the journal -------------------------------------------------------------------------------


@dataclass(frozen=True)
class JournalRead:
    """`state`: absent | unreadable | corrupt | installing | installed | unrecoverable."""
    state: str
    path: Path
    error: str | None = None
    entry: dict[str, Any] | None = None

    @property
    def usable(self) -> bool:
        return self.state in USABLE


def _time(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return dt.tzinfo is not None


def _nonneg_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _spec_ok(value: Any) -> bool:
    return (isinstance(value, dict) and set(value) == {"size", "sha256"}
            and _nonneg_int(value["size"]) and isinstance(value["sha256"], str)
            and bool(_HEX64.fullmatch(value["sha256"])))


def _entry_name(value: Any) -> bool:
    return (isinstance(value, str) and value not in ("", ".", "..") and "/" not in value
            and "\x00" not in value)


def check_journal(entry: dict[str, Any], source_id: str) -> str | None:
    """None when `entry` is a journal this module writes for `source_id`, else
    `invalid_field:<name>` for the first problem."""
    missing = [k for k in _FIELDS if k not in entry]
    if missing:
        return f"invalid_field:{missing[0]}"
    extra = sorted(set(entry) - set(_FIELDS))
    if extra:
        return f"invalid_field:{authority_read.safe_name(str(extra[0]))}"
    if entry["version"] != JOURNAL_VERSION or isinstance(entry["version"], bool):
        return "invalid_field:version"
    if entry["source_id"] != source_id:
        return "invalid_field:source_id"
    if not (isinstance(entry["attempt_id"], str) and _ATTEMPT.fullmatch(entry["attempt_id"])):
        return "invalid_field:attempt_id"
    if entry["state"] not in STAGES:
        return "invalid_field:state"
    reason = entry["reason"]
    if entry["state"] == UNRECOVERABLE:
        if not (isinstance(reason, str) and reason):
            return "invalid_field:reason"
    elif reason is not None:
        return "invalid_field:reason"
    if not (isinstance(entry["source_sha256"], str) and _HEX64.fullmatch(entry["source_sha256"])
            and manifests.source_id_for(entry["source_sha256"]) == source_id):
        return "invalid_field:source_sha256"
    config = entry["config"]
    if not (isinstance(config, dict) and set(config) == set(CONFIG_KEYS)
            and all(_nonneg_int(config[k]) for k in CONFIG_KEYS)):
        return "invalid_field:config"
    if entry["intent"] is not None and not (isinstance(entry["intent"], str)
                                            and _INTENT.fullmatch(entry["intent"])):
        return "invalid_field:intent"
    outputs = entry["outputs"]
    if not (isinstance(outputs, dict) and set(outputs) == {"markdown", "chunks", "tables"}
            and _spec_ok(outputs["markdown"]) and _spec_ok(outputs["chunks"])
            and isinstance(outputs["tables"], dict)
            and all(isinstance(n, str) and _TABLE.fullmatch(n) and _spec_ok(s)
                    for n, s in outputs["tables"].items())):
        return "invalid_field:outputs"
    obsolete = entry["obsolete_tables"]
    if not (isinstance(obsolete, list) and all(_entry_name(n) for n in obsolete)
            and len(set(obsolete)) == len(obsolete)
            and not set(obsolete) & set(outputs["tables"])):
        return "invalid_field:obsolete_tables"
    if not isinstance(entry["log"], dict):
        return "invalid_field:log"
    if entry["log_sha256"] != _sha(log_bytes(entry["log"])):
        return "invalid_field:log_sha256"
    fields = entry["manifest_fields"]
    if not (isinstance(fields, dict) and set(fields) == set(manifests.EXTRACTION_FIELDS)):
        return "invalid_field:manifest_fields"
    sup = entry["supersedes"]
    if not (isinstance(sup, list) and all(isinstance(a, str) and _ATTEMPT.fullmatch(a) for a in sup)
            and len(set(sup)) == len(sup) and entry["attempt_id"] not in sup):
        return "invalid_field:supersedes"
    for key in ("created_at", "updated_at"):
        if not _time(entry[key]):
            return f"invalid_field:{key}"
    return None


def read_journal(layout: Layout, source_id: str) -> JournalRead:
    """The typed read of a document's install journal. Never raises for the file's state."""
    path = layout.journal(source_id)
    read = authority_read.read_file(path)
    if read.state != authority_read.OK:
        return JournalRead(read.state, path, read.error)
    try:
        entry = json.loads(read.data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return JournalRead(CORRUPT, path, "invalid_json")
    if not isinstance(entry, dict):
        return JournalRead(CORRUPT, path, "not_an_object")
    problem = check_journal(entry, source_id)
    if problem is not None:
        return JournalRead(CORRUPT, path, problem)
    return JournalRead(entry["state"], path, None, entry)


@dataclass
class Inventory:
    """Every install journal: `journals` by source id, `problems` (listing errors, damaged or
    misnamed journals). `complete` only when every journal was listed, named and read."""
    journals: dict[str, JournalRead] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.problems

    def blocking(self) -> dict[str, JournalRead]:
        """The documents whose outputs must not be read (installing, unrecoverable, damaged)."""
        return {sid: j for sid, j in self.journals.items() if not j.usable}


def inventory(layout: Layout) -> Inventory:
    inv = Inventory()
    paths, error = authority_read.list_entries(layout.install, ".json")
    if error is not None:
        inv.problems.append(f"{INSTALL_DIR} can't be listed ({error})")
        return inv
    for path in paths:
        sid = path.name[:-len(".json")]
        if not manifests.is_source_id(sid):
            inv.problems.append(f"unknown install journal {authority_read.safe_name(path.name)}")
            continue
        read = read_journal(layout, sid)
        inv.journals[sid] = read
        if read.state in (UNREADABLE, CORRUPT):
            inv.problems.append(f"install journal {path.name} is {read.state} ({read.error})")
    return inv


# --------------------------------------------------------------------------- consistent reads


def _reviews_dir(layout: Layout) -> Path:
    """The write lock's directory: the vault's `reviews/`, beside `normalized/`."""
    return layout.normalized.parent / "reviews"


@contextlib.contextmanager
def consistent_read(normalized_dir: Path, source_id: str, *,
                    reviews_dir: Path | None = None) -> Iterator[JournalRead]:
    """Decision 8 consistent reads: hold the write lock while the caller checks the yielded journal
    state and reads the document's normalized bytes (Markdown, chunks, tables, extraction log), so
    an install can't run between the check and the read. Unusable journal (`installing`,
    `unrecoverable`, damaged) → the caller reads nothing and skips the document with
    :data:`INCOMPLETE`. The hold changes no authority and no preview input, so it neither checks the
    publication queue nor advances the vault revision; it is re-entrant, so a commit phase that
    already holds the lock re-checks for free. Model calls stay outside it (ADR-0068)."""
    layout = Layout(Path(normalized_dir))
    with write_lock.hold(Path(reviews_dir) if reviews_dir else _reviews_dir(layout),
                         authority=False, changes=False):
        yield read_journal(layout, source_id)


def _output_text(path: Path, errors: str) -> str:
    """A normalized output's text; "" for an absent file (the pre-journal convention)."""
    try:
        return path.read_bytes().decode("utf-8", errors=errors)
    except FileNotFoundError:
        return ""


def read_text(normalized_dir: Path, source_id: str, kind: str, *, errors: str = "strict",
              reviews_dir: Path | None = None) -> str | None:
    """One normalized output (`kind`: ``markdown`` or ``chunks``) read consistently with the
    document's journal: None when its install is incomplete, "" when the file is absent. A
    non-canonical id names no extraction output (extraction writes canonical ids only, and no path
    is built from it): "", as an absent file."""
    if not manifests.is_source_id(source_id):
        return ""
    with consistent_read(normalized_dir, source_id, reviews_dir=reviews_dir) as journal:
        if not journal.usable:
            return None
        return _output_text(Layout(Path(normalized_dir)).outputs(source_id)[kind], errors)


def usable(normalized_dir: Path, source_id: str) -> bool:
    """Whether the document's journal allows reading its outputs. Takes no lock itself: for a
    commit phase's re-check under the write lock it already holds, for exclusion gates over bytes
    read elsewhere (search hits from the index), and for report-only checks. A reader of the
    normalized files themselves uses :func:`consistent_read`."""
    return read_journal(Layout(Path(normalized_dir)), source_id).usable


def new_journal(source_id: str, attempt_id: str, *, source_sha256: str, config: dict[str, int],
                intent: str | None, outputs: dict[str, Any], obsolete_tables: list[str],
                log: dict[str, Any], manifest_fields: dict[str, Any], supersedes: list[str],
                now: str) -> dict[str, Any]:
    return {"version": JOURNAL_VERSION, "source_id": source_id, "attempt_id": attempt_id,
            "state": INSTALLING, "reason": None, "source_sha256": source_sha256,
            "config": {k: config[k] for k in CONFIG_KEYS}, "intent": intent, "outputs": outputs,
            "obsolete_tables": sorted(obsolete_tables), "log": log,
            "log_sha256": _sha(log_bytes(log)),
            "manifest_fields": {k: manifest_fields.get(k) for k in manifests.EXTRACTION_FIELDS},
            "supersedes": supersedes, "created_at": now, "updated_at": now}


def _write_journal(layout: Layout, entry: dict[str, Any], *, create: bool = False) -> None:
    problem = check_journal(entry, entry["source_id"])
    if problem is not None:
        raise ValueError(f"refusing to write an invalid install journal: {problem}")
    durable_write.write_json(layout.journal(entry["source_id"]), entry, create_only=create)


def _table_entries(directory: Path) -> list[str]:
    try:
        return sorted(os.listdir(directory))
    except FileNotFoundError:
        return []


def begin(layout: Layout, entry: dict[str, Any], previous: JournalRead | None = None) -> None:
    """Write the journal of a new attempt (under the write lock, after the publication intent and
    before the first rename). `previous` is the document's current journal: absent, or an
    `unrecoverable` / `installed` one this attempt supersedes (listed in `entry['supersedes']`)."""
    state = previous.state if previous is not None else ABSENT
    if state not in (ABSENT, UNRECOVERABLE, INSTALLED):
        raise InstallBlocked(INCOMPLETE if state == INSTALLING else f"install_journal_{state}")
    _write_journal(layout, entry, create=state == ABSENT)


def supersedes_for(previous: JournalRead) -> list[str]:
    """The attempts a new attempt supersedes: the previous journal's own and everything it did."""
    if previous.entry is None:
        return []
    return [*previous.entry["supersedes"], previous.entry["attempt_id"]]


def obsolete_tables(layout: Layout, source_id: str, new_tables: dict[str, Any]) -> list[str]:
    """The entries of the document's tables directory the new inventory does not contain."""
    return [n for n in _table_entries(layout.outputs(source_id)["tables"]) if n not in new_tables]


# --- installing --------------------------------------------------------------------------------


def _matches(path: Path, spec: dict[str, Any]) -> tuple[bool, str]:
    """`(the file holds exactly spec's bytes, its read state)`."""
    got = authority_read.hash_file(path)
    if got.state != authority_read.OK:
        return False, got.state
    return got.sha256 == spec["sha256"] and got.size == spec["size"], got.state


def _check_output(staged: Path, dest: Path, spec: dict[str, Any], label: str) -> tuple[bool, str | None]:
    """`(still to move, unrecoverable reason or None)`: the staged file holds the journal's bytes
    (to move), or it is gone and `dest` already holds them (done); anything else can't complete."""
    ok, state = _matches(staged, spec)
    if state == authority_read.OK:
        return True, None if ok else f"staged_{label}_mismatch"
    if state != ABSENT:
        return False, f"staged_{label}_{state}"
    ok, _ = _matches(dest, spec)
    return False, None if ok else f"{label}_missing"


def _sync_dirs(*dirs: Path) -> None:
    for d in dirs:
        if os.path.isdir(d):
            durable_write.sync_dir(d)


def mark_unrecoverable(layout: Layout, entry: dict[str, Any], reason: str, now: str) -> dict:
    """The explicit unrecoverable transition: only the journal changes; every artifact stays."""
    entry = {**entry, "state": UNRECOVERABLE, "reason": reason, "updated_at": now}
    _write_journal(layout, entry)
    return entry


def incompatibility(entry: dict[str, Any], manifest: dict[str, Any],
                    config: dict[str, int] | None) -> str | None:
    if manifest.get("sha256") != entry["source_sha256"]:
        return "incompatible_source"
    if config is not None and {k: config[k] for k in CONFIG_KEYS} != entry["config"]:
        return "incompatible_configuration"
    return None


def install(layout: Layout, manifests_dir: Path, entry: dict[str, Any], *, now: str,
            config: dict[str, int] | None = None) -> dict[str, Any]:
    """Advance an `installing` journal to `installed` (or `unrecoverable`); idempotent, so recovery
    repeats it after a crash at any point. Returns the journal as written. Raises
    :class:`InstallBlocked` (journal unchanged: e.g. the manifest is damaged) or ``OSError`` (an I/O
    failure or an uncertain directory sync: the journal stays `installing` for recovery). `config`
    is the current extraction configuration: given (recovery), another one is incompatible."""
    sid, attempt = entry["source_id"], entry["attempt_id"]
    read = manifests.read_manifest(Path(manifests_dir), sid)
    if read.state != manifests.OK:
        raise InstallBlocked(f"manifest_{read.state}")
    problem = incompatibility(entry, read.record, config)
    if problem is not None:
        return mark_unrecoverable(layout, entry, problem, now)
    out = layout.outputs(sid)
    stage, holding = layout.stage(sid, attempt), layout.holding(sid, attempt)
    new_tables = entry["outputs"]["tables"]
    tables_dir = out["tables"]
    try:
        st = os.lstat(tables_dir)
    except FileNotFoundError:
        durable_write.make_dirs(tables_dir)
    else:
        if not stat.S_ISDIR(st.st_mode):
            return mark_unrecoverable(layout, entry, "tables_dir_not_a_directory", now)
    # 1. verify every output before the first rename of this pass: a generation that can't
    #    complete is refused with nothing moved (the artifacts stay exactly as they are).
    pending = []
    for label, staged, dest, spec in (
            ("markdown", stage / "markdown.md", out["markdown"], entry["outputs"]["markdown"]),
            ("chunks", stage / "chunks.jsonl", out["chunks"], entry["outputs"]["chunks"]),
            *((f"table:{n}", stage / "tables" / n, tables_dir / n, s)
              for n, s in sorted(new_tables.items()))):
        to_move, reason = _check_output(staged, dest, spec, label)
        if reason is not None:
            return mark_unrecoverable(layout, entry, reason, now)
        if to_move:
            pending.append((staged, dest))
    # 2. every table entry outside the new inventory moves into the journal's holding directory.
    leftovers = [n for n in _table_entries(tables_dir) if n not in new_tables]
    if leftovers:
        durable_write.make_dirs(holding)
        for name in leftovers:
            durable_write.move(tables_dir / name, holding / name)
    # 3. each verified output moves into place.
    for staged, dest in pending:
        durable_write.make_dirs(dest.parent)
        durable_write.move(staged, dest)
    # 4. repeat the directory barrier of every rename (a failed sync of an earlier run included).
    _sync_dirs(out["markdown"].parent, out["chunks"].parent, tables_dir, holding, stage,
               stage / "tables")
    # 5. the extraction log.
    want = log_bytes(entry["log"])
    if _matches(out["log"], _spec(want))[0]:
        durable_write.sync_existing(out["log"])
    else:
        durable_write.write_bytes(out["log"], want)
    # 6. the extraction-owned manifest fields, onto the manifest as it is now (governance and
    #    provenance kept).
    record = read.record
    fields = entry["manifest_fields"]
    if any(record.get(k) != v for k, v in fields.items()):
        record.update(fields)
        manifests.save_manifest(Path(manifests_dir), record)
    else:
        durable_write.sync_existing(manifests.manifest_path(Path(manifests_dir), sid))
    # 7. verify the whole generation before declaring it installed.
    reason = verify(layout, manifests_dir, entry)
    if reason is not None:
        return mark_unrecoverable(layout, entry, reason, now)
    entry = {**entry, "state": INSTALLED, "updated_at": now}
    _write_journal(layout, entry)
    retire(layout, entry)
    return entry


def verify(layout: Layout, manifests_dir: Path, entry: dict[str, Any]) -> str | None:
    """None when the installed generation is exactly the journal's: outputs, tables set, log and the
    manifest's extraction fields; else the reason."""
    sid = entry["source_id"]
    out = layout.outputs(sid)
    for label in ("markdown", "chunks"):
        if not _matches(out[label], entry["outputs"][label])[0]:
            return f"{label}_not_installed"
    tables = entry["outputs"]["tables"]
    if sorted(_table_entries(out["tables"])) != sorted(tables):
        return "tables_differ"
    for name, spec in tables.items():
        if not _matches(out["tables"] / name, spec)[0]:
            return f"table:{name}_not_installed"
    if not _matches(out["log"], _spec(log_bytes(entry["log"])))[0]:
        return "log_not_installed"
    read = manifests.read_manifest(Path(manifests_dir), sid)
    if read.state != manifests.OK:
        return f"manifest_{read.state}"
    if any(read.record.get(k) != v for k, v in entry["manifest_fields"].items()):
        return "manifest_fields_not_applied"
    return None


def retire(layout: Layout, entry: dict[str, Any]) -> None:
    """Once installed: delete the attempt's holding and staging directories and every superseded
    attempt's (idempotent; the journal keeps naming them until it closes)."""
    sid = entry["source_id"]
    for attempt in (entry["attempt_id"], *entry["supersedes"]):
        durable_write.remove_tree(layout.holding(sid, attempt))
        durable_write.remove_tree(layout.stage(sid, attempt))


def close(layout: Layout, source_id: str) -> None:
    """Remove an installed journal once the document's publication succeeded."""
    durable_write.remove(layout.journal(source_id), missing_ok=True)


# --- recovery and cleanup (extraction run start, under the write lock) -------------------------


def recover(layout: Layout, manifests_dir: Path, config: dict[str, int], *, now: str,
            publish: Callable[[str, str | None], bool]) -> dict[str, Any]:
    """Complete every open journal. `publish(source_id, intent)` publishes one document and says
    whether it succeeded (the journal closes only then). Returns `{completed, closed,
    unrecoverable: {sid: reason}, blocked: {sid: reason}, problems}`."""
    report: dict[str, Any] = {"completed": [], "closed": [], "unrecoverable": {}, "blocked": {},
                              "problems": []}
    inv = inventory(layout)
    report["problems"] = list(inv.problems)
    for sid, read in sorted(inv.journals.items()):
        entry = read.entry
        if read.state in (UNREADABLE, CORRUPT):
            report["blocked"][sid] = f"install_journal_{read.state}"
            continue
        if read.state == UNRECOVERABLE:
            report["unrecoverable"][sid] = entry["reason"]
            continue
        try:
            if read.state == INSTALLING:
                entry = install(layout, manifests_dir, entry, now=now, config=config)
                if entry["state"] == UNRECOVERABLE:
                    report["unrecoverable"][sid] = entry["reason"]
                    continue
                report["completed"].append(sid)
            else:
                retire(layout, entry)
            if publish(sid, entry["intent"]):
                close(layout, sid)
                report["closed"].append(sid)
        except InstallBlocked as exc:
            report["blocked"][sid] = exc.reason
        except OSError as exc:
            report["blocked"][sid] = f"install_io_error:{type(exc).__name__}"
    return report


def cleanup_staging(layout: Layout) -> dict[str, Any]:
    """Remove staging and holding directories no journal refers to — only from a complete, readable
    inventory (decision 7.4). Returns `{removed: [names], blocked: reason | None}`."""
    inv = inventory(layout)
    if not inv.complete:
        return {"removed": [], "blocked": "; ".join(inv.problems)}
    referenced: set[str] = set()
    for sid, read in inv.journals.items():
        for attempt in (read.entry["attempt_id"], *read.entry["supersedes"]):
            referenced.add(_attempt_name(sid, attempt))
            referenced.add(_attempt_name(sid, attempt) + HOLDING_SUFFIX)
    removed: list[str] = []
    for directory, wanted in ((layout.staging, lambda n: True),
                              (layout.install, lambda n: n.endswith(HOLDING_SUFFIX))):
        try:
            names = sorted(os.listdir(directory))
        except FileNotFoundError:
            continue
        except OSError as exc:
            return {"removed": removed, "blocked": f"{directory.name} can't be listed "
                                                   f"({type(exc).__name__})"}
        for name in names:
            if not wanted(name) or name in referenced:
                continue
            path = directory / name
            if os.path.isdir(path) and not os.path.islink(path):
                durable_write.remove_tree(path)
            else:
                durable_write.remove(path)
            removed.append(name)
    return {"removed": removed, "blocked": None}
