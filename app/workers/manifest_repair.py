#!/usr/bin/env python3
"""Operator recovery of a damaged manifest (ADR-0074 decision 3, slice 4).

`scripts/repair_manifest.py` is a thin CLI over this module, in the `repair_supersede.py` pattern
(ADR-0073 decisions 18–23):

- :func:`inspect` captures ONE snapshot of everything a recovery depends on — the live manifest's
  bytes, its identity in the manifest inventory, an open recovery journal, the audit records that
  bear on the document, its published status in the graph, and every backup archive's copy of the
  manifest — and derives the report, the offered command and the `--expected-state` fingerprint
  from that snapshot only. Read-only: no lock, no run guard, no revision change, nothing created.
- :func:`restore` is the only write path for a **corrupt** manifest. It takes the run guards, then
  the write lock (vault lock → write lock), captures a fresh snapshot, compares its fingerprint with
  `--expected-state`, repeats every check, and only then writes, in this order: (1) a durable copy
  of the damaged bytes in `raw/manifests/quarantine/` (kept, never deleted, ignored by every
  manifest inventory); (2) the recovery intent — publication queued and a recovery journal in
  `db/manifest_recovery/`; (3) the atomic replacement of the live manifest with the validated
  backup copy; (4) publication, the audit record, then the journal closed. There is never an
  absence window: the manifest is damaged or restored, never missing.
- :func:`finish` completes a recovery whose journal is still open (a crash after step 2), rolling
  forward from the journal; it is offered by `inspect` and needs the same fingerprint. The journal
  must validate against the requested document (and its quarantine copy hold the damaged bytes); a
  copy not installed yet must still be compatible, as for `restore`; and `finish` repeats the
  durability barriers of the files the interrupted run wrote before it publishes or closes.

An **unreadable** manifest has no write path (fix the I/O or permission problem and inspect again).
A backup copy is usable only when it validates (one member, bounded size, parses, canonical
identity, a well-formed full SHA-256 agreeing with the id and — when the raw file is present and
trustworthy — with the raw bytes) and is **compatible** with every later human decision: no
governance decision (archive / hide / unhide) and no Details change at or after the backup's
time, a backup made by code that records Details history (`details_history` marker), a complete and
readable audit history, and a status that agrees with the published graph status. Unknown is not
compatible; there is no override.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import stat
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.backend import (authority_read, durable_write, manifests, publication_queue, revision,
                         run_guard, write_lock)
from app.backend.paths import safe_under

FINGERPRINT_VERSION = "manifest-recovery-v1"   # change with the payload OR the recovery policy
# Runs that write manifests or publish across several commits, taken in this fixed order.
GUARDS = ("apply", "intake_scan", "extract", "republish")

EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2
EXIT_BUSY = run_guard.EXIT_BUSY                     # 3
EXIT_QUEUE_CORRUPT = run_guard.EXIT_QUEUE_CORRUPT   # 4
EXIT_STATE_CHANGED = 6
EXIT_REVISION_UNAVAILABLE = 9

SIDECAR = "BACKUP_MANIFEST.json"                    # scripts/backup.py's archive sidecar
MAX_MEMBER_BYTES = 4 << 20                          # a manifest or sidecar larger than this is refused
QUARANTINE_DIR = "quarantine"                       # under raw/manifests/
JOURNAL_DIR = "manifest_recovery"                   # under db/
ACTOR = "repair_manifest"
# Reviews whose execution writes a manifest's lifecycle status (retention executors).
GOVERNANCE_TYPES = ("archive_source", "hide_content", "unhide_content")
# Audit entries of those reviews that record no change of the document.
NO_EFFECT = frozenset({"rejected", "withdrawn", "deferred", "proposal_refreshed"})
# Audit files named for another subject (tags, claims, syntheses, other documents' events).
_SUBJECT_NAMED = re.compile(r"(itm|clm|syn|src)_[0-9a-f]{16}-")
_ARCHIVE_STAMP = "%Y%m%dT%H%M%SZ"

INSPECT_COMMAND = "uv run python scripts/repair_manifest.py inspect {sid}"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def manifests_dir(root: Path) -> Path:
    return Path(root) / "raw" / "manifests"


def quarantine_dir(root: Path) -> Path:
    return manifests_dir(root) / QUARANTINE_DIR


def journal_path(root: Path, source_id: str) -> Path:
    manifests._require_source_id(source_id)
    return Path(root) / "db" / JOURNAL_DIR / f"{source_id}.json"


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def _archive_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, _ARCHIVE_STAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _stamp(now: str) -> str:
    dt = _parse_time(now) or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).strftime(_ARCHIVE_STAMP)


def _json_obj(raw: bytes | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


# --- reading the inputs ------------------------------------------------------------------------


def _hash_raw(path: Path) -> dict[str, Any]:
    """`{state: absent | untrusted | ok, sha256}` of a raw file — a regular file only, never through
    a link, non-blocking (a FIFO or a device is untrusted, never opened as a stream)."""
    got = authority_read.hash_file(path)
    if got.state == authority_read.ABSENT:
        return {"state": "absent", "sha256": None}
    if got.state != authority_read.OK:
        return {"state": "untrusted", "sha256": None}
    return {"state": "ok", "sha256": got.sha256}


def _read_member(zf: zipfile.ZipFile, name: str) -> tuple[bytes | None, str | None]:
    """`(bytes, None)` for exactly one bounded, unencrypted member; `(None, None)` when the archive
    has none; `(None, reason)` when the member makes the archive unusable."""
    infos = [i for i in zf.infolist() if i.filename == name]
    if not infos:
        return None, None
    if len(infos) > 1:
        return None, "duplicate_member"
    info = infos[0]
    if info.flag_bits & 0x1:
        return None, "encrypted_member"
    if info.file_size > MAX_MEMBER_BYTES:
        return None, "oversized_member"
    try:
        with zf.open(info) as fh:
            data = fh.read(MAX_MEMBER_BYTES + 1)
    except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError, RuntimeError):
        return None, "unreadable_member"
    if len(data) > MAX_MEMBER_BYTES:
        return None, "oversized_member"
    return data, None


def _validate_copy(root: Path, source_id: str, data: bytes,
                   raw_cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The backup copy's validation (decision 3): `{usable, reason, record, raw}`."""
    out: dict[str, Any] = {"usable": False, "reason": None, "record": None,
                           "raw": {"state": "unavailable", "path": None}}
    read = manifests.manifest_from_bytes(Path(f"{source_id}.json"), data, source_id)
    if read.state != manifests.OK:
        out["reason"] = f"copy_{read.error}"
        return out
    record = read.record
    sha = record.get("sha256")
    if not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha)):
        out["reason"] = "copy_sha256_malformed"
        return out
    if manifests.source_id_for(sha) != source_id:
        out["reason"] = "copy_sha256_disagrees_with_id"
        return out
    if "status" in record and record["status"] not in manifests.SOURCE_STATUSES:
        out["reason"] = "copy_status_invalid"                 # would silently read as active
        return out
    if manifests.provenance_problem(record.get("provenance") or {}) is not None:
        out["reason"] = "copy_provenance_invalid"
        return out
    rel = record.get("relative_raw_path")
    if isinstance(rel, str) and rel:
        dest = safe_under(root, root / "raw", rel)
        if dest is None:
            out["reason"] = "copy_raw_path_escapes_raw"
            return out
        got = raw_cache.get(rel)
        if got is None:
            got = raw_cache[rel] = _hash_raw(dest)
        if got["state"] == "ok" and got["sha256"] != sha:
            out["reason"] = "copy_disagrees_with_raw_bytes"
            return out
        out["raw"] = {"state": "verified" if got["state"] == "ok" else got["state"], "path": rel}
    out.update(usable=True, record=record)
    return out


def _read_archive(root: Path, path: Path, source_id: str,
                  raw_cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """One archive's copy of this manifest: `{name, path, state: copy | no_copy | rejected, reason,
    member_sha256, sidecar_sha256, created_at, details_history, copy}`."""
    out: dict[str, Any] = {"name": path.name, "path": str(path), "state": "rejected", "reason": None,
                           "member_sha256": None, "sidecar_sha256": None, "created_at": None,
                           "details_history": None, "copy": None, "bytes": None}
    try:
        st = os.lstat(path)
    except OSError:
        out["reason"] = "archive_unreadable"
        return out
    if not stat.S_ISREG(st.st_mode):
        out["reason"] = "archive_not_a_regular_file"
        return out
    try:
        with zipfile.ZipFile(path) as zf:
            member, problem = _read_member(zf, f"raw/manifests/{source_id}.json")
            if problem is not None:
                out["reason"] = problem
                return out
            if member is None:
                out["state"] = "no_copy"
                return out
            sidecar, sproblem = _read_member(zf, SIDECAR)
    except (zipfile.BadZipFile, OSError, ValueError):
        out["reason"] = "archive_unreadable"
        return out
    if sproblem is not None:
        out["reason"] = sproblem
        return out
    out["member_sha256"] = _sha(member)
    out["bytes"] = member
    if sidecar is not None:
        out["sidecar_sha256"] = _sha(sidecar)
        meta = _json_obj(sidecar) or {}
        out["created_at"] = meta.get("created_at") if isinstance(meta.get("created_at"), str) else None
        history = meta.get("details_history")
        out["details_history"] = history if isinstance(history, int) and not isinstance(
            history, bool) else None
    copy = _validate_copy(root, source_id, member, raw_cache)
    out["copy"] = copy
    out["state"] = "copy" if copy["usable"] else "rejected"
    out["reason"] = copy["reason"]
    return out


def _scan_audit(root: Path, source_id: str) -> dict[str, Any]:
    """The audit records that bear on this document: `{complete, problems, details: [time],
    governance: [{time, type, decision}], parts: [[name, sha256 | state]]}`. A log that can't be
    listed, a relevant record that can't be read or parsed, or one without a usable time makes the
    history incomplete (decision 3: unknown is not compatible). A verified absent log is complete."""
    audit_dir = Path(root) / "reviews" / "audit_log"
    out: dict[str, Any] = {"complete": True, "problems": [], "details": [], "governance": [],
                           "parts": []}
    paths, error = authority_read.list_entries(audit_dir, ".json")
    if error is not None:
        out["complete"] = False
        out["problems"].append(f"reviews/audit_log can't be listed ({error})")
        return out
    details_prefix = f"{source_id}{manifests.DETAILS_AUDIT_INFIX}"
    for path in paths:
        name = path.name
        if name.startswith(details_prefix):
            kind = "details"
        elif name.startswith("rev_"):
            kind = "review"
        elif _SUBJECT_NAMED.match(name):
            continue                       # another subject, or this document's tag events
        else:
            kind = "unknown"
        read = authority_read.read_file(path)
        if read.state == authority_read.ABSENT:
            continue
        safe = authority_read.safe_name(name)
        if read.state != authority_read.OK:
            out["complete"] = False
            out["problems"].append(f"audit record {safe} is {read.state} ({read.error})")
            out["parts"].append([name, read.state])
            continue
        rec = _json_obj(read.data)
        if rec is None:
            out["complete"] = False
            out["problems"].append(f"audit record {safe} is corrupt")
            out["parts"].append([name, "corrupt"])
            continue
        if kind == "details":
            at = _parse_time(rec.get("at"))
            if rec.get("source_id") != source_id or at is None:
                out["complete"] = False
                out["problems"].append(f"Details record {safe} has no usable source or time")
            else:
                out["details"].append(at)
            out["parts"].append([name, _sha(read.data)])
            continue
        subject = rec.get("subject") if isinstance(rec.get("subject"), dict) else {}
        names_doc = subject.get("source_id") == source_id or rec.get("source_id") == source_id
        if kind == "unknown":
            if names_doc:                  # an unrecognized record about this document
                out["complete"] = False
                out["problems"].append(f"audit record {safe} names this document in an unknown form")
                out["parts"].append([name, _sha(read.data)])
            continue
        if rec.get("type") not in GOVERNANCE_TYPES or subject.get("source_id") != source_id:
            continue
        decision = rec.get("decision") or rec.get("event")
        if decision in NO_EFFECT:
            continue
        at = _parse_time(rec.get("decided_at") or rec.get("at"))
        out["parts"].append([name, _sha(read.data)])
        if at is None:
            out["complete"] = False
            out["problems"].append(f"governance record {safe} has no usable time")
            continue
        out["governance"].append({"time": at, "type": rec.get("type"), "decision": decision})
    return out


def _graph_status(root: Path, source_id: str) -> dict[str, Any]:
    """The published status of the document's graph node: `{state: absent | unreadable | ok,
    status}` (`ok` with status None when the graph has no node for it)."""
    path = Path(root) / "db" / "graph.sqlite"
    if not path.exists():
        return {"state": "absent", "status": None}
    try:
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT status FROM nodes WHERE node_id = ?",
                               (source_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return {"state": "unreadable", "status": None}
    return {"state": "ok", "status": row[0] if row is not None else None}


_JOURNAL_FIELDS = ("recovery_id", "source_id", "archive", "backup_created_at", "installed_sha256",
                   "damaged_sha256", "quarantine", "intent", "actor", "reason", "expected_state",
                   "started_at")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_RECOVERY_ID = re.compile(r"mrc_[0-9a-f]{16}")
_INTENT_ID = re.compile(r"pub_[0-9a-f]{12}")
_ARCHIVE_NAME = re.compile(r"[^/\\\x00]+\.zip")


def _check_journal(entry: dict[str, Any], source_id: str) -> str | None:
    """None when `entry` is a recovery journal `_write_restore` writes for `source_id` (the journal's
    own file name), else `invalid_field:<name>` for the first problem. The binding fields are
    recomputed, never trusted: a journal can only finish the recovery of the document it names."""
    missing = sorted(set(_JOURNAL_FIELDS) - set(entry))
    if missing:
        return f"invalid_field:{missing[0]}"
    extra = sorted(set(entry) - set(_JOURNAL_FIELDS))
    if extra:
        return f"invalid_field:{authority_read.safe_name(str(extra[0]))}"
    if entry["source_id"] != source_id:
        return "invalid_field:source_id"
    archive = entry["archive"]
    if not (isinstance(archive, str) and _ARCHIVE_NAME.fullmatch(archive)
            and Path(archive).name == archive):
        return "invalid_field:archive"
    if _archive_time(entry["backup_created_at"]) is None:
        return "invalid_field:backup_created_at"
    for key in ("installed_sha256", "damaged_sha256", "expected_state"):
        if not (isinstance(entry[key], str) and _HEX64.fullmatch(entry[key])):
            return f"invalid_field:{key}"
    if entry["installed_sha256"] == entry["damaged_sha256"]:
        return "invalid_field:installed_sha256"
    rid = entry["recovery_id"]
    if not (isinstance(rid, str) and _RECOVERY_ID.fullmatch(rid)
            and rid == _recovery_id(source_id, archive, entry["expected_state"])):
        return "invalid_field:recovery_id"
    quarantine = entry["quarantine"]
    if not (isinstance(quarantine, str)
            and re.fullmatch(re.escape(source_id) + r"-\d{8}T\d{6}Z\.json", quarantine)):
        return "invalid_field:quarantine"
    if not (isinstance(entry["intent"], str) and _INTENT_ID.fullmatch(entry["intent"])):
        return "invalid_field:intent"
    if entry["actor"] != ACTOR:
        return "invalid_field:actor"
    if not (isinstance(entry["reason"], str) and entry["reason"].strip()):
        return "invalid_field:reason"
    if _parse_time(entry["started_at"]) is None:
        return "invalid_field:started_at"
    return None


def _read_journal(root: Path, source_id: str) -> dict[str, Any]:
    """The open recovery journal: `{state: absent | unreadable | corrupt | invalid | ok, error,
    sha256, entry, quarantine}`. `ok` only for a journal that validates against this document AND
    whose quarantine copy reads (typed reader) and holds exactly the damaged bytes it names."""
    read = authority_read.read_file(journal_path(root, source_id))
    out: dict[str, Any] = {"state": read.state, "error": read.error,
                           "sha256": _sha(read.data) if read.data is not None else None,
                           "entry": None, "quarantine": None}
    if read.state != authority_read.OK:
        return out
    entry = _json_obj(read.data)
    if entry is None:
        out.update(state="corrupt", error="not_a_json_object")
        return out
    problem = _check_journal(entry, source_id)
    if problem is not None:
        out.update(state="invalid", error=problem)
        return out
    qread = authority_read.read_file(quarantine_dir(root) / entry["quarantine"])
    qsha = _sha(qread.data) if qread.state == authority_read.OK else None
    out["quarantine"] = [qread.state, qsha]
    if qsha != entry["damaged_sha256"]:
        out.update(state="invalid", error=f"quarantine_unverified:{qread.state}"
                   + (":sha256_mismatch" if qsha is not None else ""))
        return out
    out.update(state="ok", error=None, entry=entry)
    return out


def _identity(mdir: Path, source_id: str) -> dict[str, Any]:
    """Other manifest files that claim this document (`strays`: their safe names) — the identity
    quarantine a restore can't resolve. The live file's own damage (e.g. a record naming another id)
    is not a stray: it is what restore replaces."""
    own = manifests.manifest_path(mdir, source_id).name
    paths, error = manifests.manifest_files(mdir)
    strays = []
    for path in paths:
        if path.name == own:
            continue
        read = manifests.read_manifest_file(path)
        if read.state == manifests.OK and read.record.get("source_id") == source_id:
            strays.append(manifests.safe_display(path.name))
    return {"strays": strays, "enumeration_error": error}


def capture(root: Path, source_id: str, backups_dir: Path | None = None) -> dict[str, Any]:
    """One snapshot: every file read once. The report and the fingerprint derive from it only."""
    root = Path(root).resolve()
    backups = Path(backups_dir).resolve() if backups_dir else root / "backups"
    mdir = manifests_dir(root)
    read = authority_read.read_file(manifests.manifest_path(mdir, source_id))
    if read.state == authority_read.OK:
        typed = manifests.manifest_from_bytes(read.path, read.data, source_id)
        live = {"state": typed.state, "error": typed.error, "bytes": read.data}
    else:
        live = {"state": read.state, "error": read.error, "bytes": None}
    raw_cache: dict[str, dict[str, Any]] = {}
    archive_paths, archive_error = authority_read.list_entries(backups, ".zip")
    archives = [_read_archive(root, p, source_id, raw_cache) for p in archive_paths]
    archives.sort(key=lambda a: (_archive_time(a["created_at"]) is not None,
                                 a["created_at"] or "", a["name"]), reverse=True)
    return {"root": str(root), "source_id": source_id, "backups_dir": str(backups),
            "live": live,
            "identity": _identity(mdir, source_id),
            "journal": _read_journal(root, source_id),
            "audit": _scan_audit(root, source_id),
            "graph": _graph_status(root, source_id),
            "archives": archives, "archives_error": archive_error, "raw": raw_cache}


def fingerprint(snap: dict[str, Any]) -> str:
    """SHA-256 over the canonical payload: byte hashes and states, never parsed facts alone."""
    live = snap["live"]
    payload = {
        "version": FINGERPRINT_VERSION,
        "target": {"root": snap["root"], "source_id": snap["source_id"],
                   "backups_dir": snap["backups_dir"]},
        "manifest": {"state": live["state"], "error": live["error"],
                     "sha256": _sha(live["bytes"]) if live["bytes"] is not None else None},
        "identity": snap["identity"],
        "journal": {"state": snap["journal"]["state"], "error": snap["journal"]["error"],
                    "sha256": snap["journal"]["sha256"],
                    "quarantine": snap["journal"]["quarantine"]},
        "audit": {"complete": snap["audit"]["complete"], "parts": snap["audit"]["parts"]},
        "graph": snap["graph"],
        "archives_error": snap["archives_error"],
        "archives": [[a["name"], a["state"], a["reason"], a["member_sha256"], a["sidecar_sha256"]]
                     for a in snap["archives"]],
        "raw": {rel: [got["state"], got["sha256"]] for rel, got in sorted(snap["raw"].items())},
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha(blob)


# --- the evaluation (from the snapshot only) ---------------------------------------------------


def incompatibility(snap: dict[str, Any], archive: dict[str, Any]) -> list[str]:
    """Why this usable copy can't be restored without losing a later human decision (empty when it
    is compatible). Unknown is not compatible."""
    reasons: list[str] = []
    if archive["details_history"] is None or archive["details_history"] < 1:
        reasons.append("the backup predates Details auditing, so a later Details change could be "
                       "unrecorded (details history unknown)")
    created = _archive_time(archive["created_at"])
    if created is None:
        reasons.append("the backup's creation time is unknown")
    audit = snap["audit"]
    if not audit["complete"]:
        reasons.append("the audit history is incomplete: " + "; ".join(audit["problems"]))
    if created is not None:
        if any(t >= created for t in audit["details"]):
            reasons.append("a Details change was recorded at or after this backup")
        newer = sorted({g["type"] for g in audit["governance"] if g["time"] >= created})
        if newer:
            reasons.append(f"a governance decision ({', '.join(newer)}) was recorded at or after "
                           "this backup")
    graph = snap["graph"]
    status = manifests.get_status(archive["copy"]["record"])
    if graph["state"] == "unreadable":
        reasons.append("the published status can't be read (db/graph.sqlite)")
    elif graph["state"] == "ok" and graph["status"] is not None and graph["status"] != status:
        reasons.append(f"the copy's status ({status}) disagrees with the published status "
                       f"({graph['status']})")
    return reasons


def _command(snap: dict[str, Any], verb: str, extra: str, fp: str, *, reason: bool = True) -> str:
    root_arg = "" if Path(snap["root"]) == Path.cwd().resolve() else f" --root {snap['root']}"
    backups_arg = ("" if Path(snap["backups_dir"]) == Path(snap["root"]) / "backups"
                   else f" --backups {snap['backups_dir']}")
    return (f"uv run python scripts/repair_manifest.py {verb} {snap['source_id']}{extra} "
            f"--expected-state {fp}{' --reason "<why>"' if reason else ''}{root_arg}{backups_arg}")


def _journal_route(snap: dict[str, Any]) -> str | None:
    """How an open journal can be finished: `installed` (the copy is live), `pending_install` (the
    damaged bytes are still live), or None (anything else: refuse)."""
    entry = snap["journal"]["entry"]
    live = snap["live"]["bytes"]
    if entry is None or live is None:
        return None
    if _sha(live) == entry.get("installed_sha256"):
        return "installed"
    if _sha(live) == entry.get("damaged_sha256"):
        return "pending_install"
    return None


def _journal_copy(snap: dict[str, Any]) -> dict[str, Any] | None:
    """The archive copy the open journal names, while it is present, usable and the same bytes."""
    entry = snap["journal"]["entry"]
    return next((a for a in snap["archives"] if a["name"] == entry["archive"]
                 and a["state"] == "copy" and a["member_sha256"] == entry["installed_sha256"]),
                None)


def _pending_install_problem(snap: dict[str, Any]) -> str | None:
    """Why a journal whose copy is not installed yet can't install it now (None when it can): the
    identity and compatibility checks `restore` makes, against the current snapshot, so a human
    decision recorded after the journal blocks `finish` exactly as it blocks `restore`."""
    if snap["identity"]["enumeration_error"]:
        return "raw/manifests can't be listed, so the document's identity can't be established"
    if snap["identity"]["strays"]:
        return ("another file in raw/manifests claims this document ("
                + ", ".join(snap["identity"]["strays"]) + ")")
    copy = _journal_copy(snap)
    if copy is None:
        return "the journal's backup copy is no longer available and valid"
    reasons = incompatibility(snap, copy)
    if reasons:
        return "the journal's backup copy is no longer compatible: " + "; ".join(reasons)
    return None


def evaluate(snap: dict[str, Any]) -> dict[str, Any]:
    """The report: `{source_id, fingerprint, facts, findings, copies, offers, finish}`. `offers`
    maps an archive name to its restore command; `finish` is the finish command, or None."""
    sid = snap["source_id"]
    fp = fingerprint(snap)
    live = snap["live"]
    report: dict[str, Any] = {"source_id": sid, "fingerprint": fp, "facts": {}, "findings": [],
                              "copies": [], "offers": {}, "finish": None}
    facts, findings = report["facts"], report["findings"]
    facts["manifest"] = (live["state"] if live["error"] is None
                         else f"{live['state']} ({live['error']})")
    if live["bytes"] is not None:
        facts["manifest bytes"] = f"{len(live['bytes'])} bytes, sha256 {_sha(live['bytes'])}"
    else:
        facts["manifest bytes"] = "unavailable"
    if snap["identity"]["enumeration_error"]:
        facts["manifest inventory"] = (f"raw/manifests can't be listed "
                                       f"({snap['identity']['enumeration_error']})")
    elif snap["identity"]["strays"]:
        facts["manifest inventory"] = ("other files claim this document: "
                                       + ", ".join(snap["identity"]["strays"]))
    verified = sorted({c["copy"]["raw"]["path"] for c in snap["archives"]
                       if c["state"] == "copy" and c["copy"]["raw"]["state"] == "verified"})
    facts["raw file"] = (", ".join(f"{p} (sha256 verified)" for p in verified) if verified
                         else "unavailable (no trustworthy evidence)")
    audit = snap["audit"]
    facts["audit history"] = ("complete" if audit["complete"]
                              else "incomplete — " + "; ".join(audit["problems"]))
    facts["decisions on record"] = (
        f"{len(audit['details'])} Details change(s), {len(audit['governance'])} governance "
        "decision(s)")
    graph = snap["graph"]
    facts["published status"] = (graph["status"] or "no graph node") if graph["state"] == "ok" \
        else f"graph {graph['state']}"
    if snap["archives_error"]:
        facts["backups"] = f"{snap['backups_dir']} can't be listed ({snap['archives_error']})"
    else:
        counts = {s: sum(a["state"] == s for a in snap["archives"])
                  for s in ("copy", "no_copy", "rejected")}
        facts["backups"] = (f"{len(snap['archives'])} archive(s) in {snap['backups_dir']}: "
                            f"{counts['copy']} with a usable copy, {counts['rejected']} rejected, "
                            f"{counts['no_copy']} without this manifest")
    journal = snap["journal"]
    facts["recovery journal"] = ("none" if journal["state"] == authority_read.ABSENT
                                 else journal["state"] if journal["error"] is None
                                 else f"{journal['state']} ({journal['error']})")

    for a in snap["archives"]:
        if a["state"] == "no_copy":
            continue
        row: dict[str, Any] = {"archive": a["name"], "state": a["state"], "reason": a["reason"],
                               "created_at": a["created_at"], "details_history": a["details_history"]}
        if a["state"] == "copy":
            record = a["copy"]["record"]
            row["status"] = manifests.get_status(record)
            row["details"] = manifests.confirmed_details(manifests.get_provenance(record))
            row["raw"] = a["copy"]["raw"]
            row["incompatible"] = incompatibility(snap, a)
        report["copies"].append(row)

    # What may be written.
    if journal["state"] != authority_read.ABSENT:
        route = _journal_route(snap) if journal["state"] == "ok" else None
        if route is None:
            findings.append("A recovery journal is open but doesn't match the live manifest (or "
                            "can't be read or validated): no safe repair determined. Leave both as "
                            "they are and restore db/manifest_recovery/ and the manifest from a "
                            "backup.")
            return report
        if route == "installed":
            findings.append("A recovery was recorded and not finished (the backup copy is live).")
            report["finish"] = _command(snap, "finish", "", fp, reason=False)
            return report
        blocked = _pending_install_problem(snap)
        if blocked is not None:
            findings.append("A recovery was recorded and not finished, and the copy is not "
                            f"installed yet, but it can't be installed now: {blocked}. The journal "
                            "and the damaged manifest are left as they are.")
            return report
        findings.append("A recovery was recorded and not finished (the copy is not installed yet).")
        report["finish"] = _command(snap, "finish", "", fp, reason=False)
        return report
    if live["state"] == authority_read.ABSENT:
        findings.append("The manifest is absent — nothing to recover. Intake re-creates a manifest "
                        "from the raw file (absence is never damage).")
        return report
    if live["state"] == authority_read.UNREADABLE:
        findings.append("The manifest is unreadable — there is no write path for that: fix the I/O "
                        "or permission problem (or the link / non-file entry) and inspect again.")
        return report
    if live["state"] == authority_read.OK:
        findings.append("The manifest reads and is well formed — nothing to restore.")
        return report
    if snap["identity"]["enumeration_error"]:
        findings.append("raw/manifests can't be listed, so the document's identity can't be "
                        "established: no safe repair determined.")
        return report
    if snap["identity"]["strays"]:
        findings.append("Another file in raw/manifests claims this document (identity quarantine): "
                        "move the stray file out of raw/manifests first, then inspect again.")
        return report
    compatible = [a for a in snap["archives"]
                  if a["state"] == "copy" and not incompatibility(snap, a)]
    if not compatible:
        findings.append("No compatible backup copy: no safe repair determined. Restore needs a "
                        "copy that validates and is newer than every later human decision "
                        "(rebuilding a manifest without a backup is deferred, ADR-0074 decision 3).")
        return report
    for a in compatible:
        report["offers"][a["name"]] = _command(snap, "restore", f" --archive {a['path']}", fp)
    findings.append(f"{len(compatible)} compatible backup cop{'y' if len(compatible) == 1 else 'ies'}"
                    f"; the newest is {compatible[0]['name']}.")
    return report


def inspect(root: Path, source_id: str, backups_dir: Path | None = None) -> dict[str, Any]:
    """The read-only diagnostic report."""
    return evaluate(capture(root, source_id, backups_dir))


# --- the commands ------------------------------------------------------------------------------


def _recovery_id(source_id: str, archive_name: str, expected_state: str) -> str:
    return "mrc_" + _sha(f"{source_id}\0{archive_name}\0{expected_state}".encode())[:16]


def _audit_path(root: Path, source_id: str, recovery_id: str) -> Path:
    return Path(root) / "reviews" / "audit_log" / f"{source_id}-manifest-restored-{recovery_id}.json"


def _guarded(root: Path, out: dict[str, Any], body) -> dict[str, Any]:
    try:
        with contextlib.ExitStack() as guards:
            for name in GUARDS:                       # fixed order; unwound on any failure
                guards.enter_context(run_guard.guard(root / "db", name))
            # ADR-0074 decision 3: vault lock → write lock (hold takes both). `changes=False`: the
            # revision advances only right before the first write, so a refusal changes nothing.
            with write_lock.hold(root / "reviews", changes=False):
                return body()
    except run_guard.ProducerBusy as exc:
        out.update(exit=EXIT_BUSY, message=f"Refused: {exc}. Wait for it to finish, then inspect "
                                           "again. Nothing was changed.")
    except publication_queue.QueueCorrupt as exc:
        out.update(exit=EXIT_QUEUE_CORRUPT,
                   message=f"Refused: {exc}. Run `uv run python scripts/republish.py --repair`. "
                           "Nothing was changed.")
    except revision.RevisionUnavailable as exc:
        out.update(exit=EXIT_REVISION_UNAVAILABLE,
                   message=f"Refused: the vault revision can't be advanced ({exc}). Nothing was "
                           "changed.")
    return out


def _fresh(root: Path, source_id: str, backups_dir: Path | None, expected_state: str,
           out: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    snap = capture(root, source_id, backups_dir)
    report = evaluate(snap)
    if report["fingerprint"] != expected_state:
        out.update(exit=EXIT_STATE_CHANGED,
                   message="State changed since it was inspected — inspect again. Nothing was "
                           "changed.")
        return None
    return snap, report


def restore(root: Path, source_id: str, *, archive: Path, expected_state: str, reason: str,
            backups_dir: Path | None = None, now: str | None = None) -> dict[str, Any]:
    """`{exit, message, wrote}` — the one governed restore of a corrupt manifest."""
    root = Path(root).resolve()
    now = now or manifests.iso_now()
    out: dict[str, Any] = {"exit": EXIT_REFUSED, "message": "", "wrote": False}
    if not reason.strip():
        out.update(exit=EXIT_USAGE, message="--reason must say why.")
        return out
    archive = Path(archive).resolve()

    def body() -> dict[str, Any]:
        got = _fresh(root, source_id, backups_dir, expected_state, out)
        if got is None:
            return out
        snap, report = got
        chosen = next((a for a in snap["archives"] if Path(a["path"]) == archive), None)
        if chosen is None or chosen["name"] not in report["offers"]:
            why = (" ".join(report["findings"]) if not report["offers"] else
                   "this archive is not one of the compatible copies inspect offers")
            if chosen is not None and chosen["state"] == "copy":
                why = "; ".join(incompatibility(snap, chosen)) or why
            elif chosen is not None and chosen["reason"]:
                why = f"the archive's copy is unusable ({chosen['reason']})"
            out["message"] = f"Refused before any write: {why}. Nothing was changed."
            return out
        return _write_restore(root, snap, chosen, expected_state, reason.strip(), now, out)

    return _guarded(root, out, body)


def _uncertain(sid: str, stage: str, exc: OSError, out: dict[str, Any]) -> dict[str, Any]:
    """A write failed, or completed with uncertain durability, part-way through a recovery. The
    message names only what is known at that stage; it never promises a journal that may not
    exist, and recommends `finish` only through `inspect`."""
    known = {
        "quarantine": "No recovery journal was created; the damaged manifest is unchanged.",
        "intent": "The quarantine copy was written; no recovery journal was created; the damaged "
                  "manifest is unchanged.",
        "journal": "The recovery journal may or may not have been recorded durably; the damaged "
                   "manifest is unchanged.",
        "install": "The recovery journal is recorded; the manifest replacement may or may not "
                   "have completed durably.",
        "barrier": "The recovery journal is recorded; the earlier writes could not be confirmed "
                   "durable.",
        "complete": "The manifest is restored; its audit record or the journal's close could not "
                    "be confirmed.",
    }[stage]
    out.update(exit=EXIT_REFUSED, wrote=True, message=(
        f"Stopped: {type(exc).__name__}: {exc}. {known} Inspect again "
        f"(`{INSPECT_COMMAND.format(sid=sid)}`): it offers `finish` only when that path is safe."))
    return out


def _write_restore(root: Path, snap: dict[str, Any], chosen: dict[str, Any], expected_state: str,
                   reason: str, now: str, out: dict[str, Any]) -> dict[str, Any]:
    sid = snap["source_id"]
    damaged = snap["live"]["bytes"]
    rid = _recovery_id(sid, chosen["name"], expected_state)
    qpath = quarantine_dir(root) / f"{sid}-{_stamp(now)}.json"
    write_lock.invalidate()                          # the first write follows
    stage = "quarantine"
    try:
        # 1. copy (never move) the damaged bytes durably; kept, never deleted.
        try:
            durable_write.write_bytes(qpath, damaged, create_only=True)
        except FileExistsError:
            out["message"] = (f"Refused: a quarantine copy named {qpath.name} already exists. Wait "
                              "a second and inspect again. Nothing was changed.")
            return out
        out["wrote"] = True
        # 2. the recovery intent: publication queued, then the journal.
        stage = "intent"
        intent = publication_queue.record(root, origin="repair_manifest", sources=[sid], now=now)
        entry = {"recovery_id": rid, "source_id": sid, "archive": chosen["name"],
                 "backup_created_at": chosen["created_at"],
                 "installed_sha256": chosen["member_sha256"], "damaged_sha256": _sha(damaged),
                 "quarantine": qpath.name, "intent": intent, "actor": ACTOR, "reason": reason,
                 "expected_state": expected_state, "started_at": now}
        stage = "journal"
        durable_write.write_json(journal_path(root, sid), entry, create_only=True)
        # 3. atomically replace the live manifest with the validated copy.
        stage = "install"
        durable_write.write_bytes(manifests.manifest_path(manifests_dir(root), sid),
                                  chosen["bytes"])
        stage = "complete"
        return _complete(root, sid, entry, now, out)
    except OSError as exc:
        return _uncertain(sid, stage, exc, out)


def _audit_record(sid: str, entry: dict[str, Any], now: str) -> dict[str, Any]:
    return {"source_id": sid, "event": "manifest_restored", "at": now, "actor": ACTOR,
            "recovery_id": entry["recovery_id"], "reason": entry["reason"],
            "archive": entry["archive"], "backup_created_at": entry["backup_created_at"],
            "installed_sha256": entry["installed_sha256"],
            "damaged_sha256": entry["damaged_sha256"],
            "quarantine": f"raw/manifests/{QUARANTINE_DIR}/{entry['quarantine']}",
            "expected_state": entry["expected_state"]}


def _audit_matches(existing: dict[str, Any] | None, expected: dict[str, Any]) -> bool:
    """An earlier attempt's audit record is this recovery's: every field equal except `at` (the
    retry's time differs), which must still be a valid timestamp."""
    if existing is None or set(existing) != set(expected):
        return False
    if _parse_time(existing["at"]) is None:
        return False
    return all(existing[k] == v for k, v in expected.items() if k != "at")


def _complete(root: Path, sid: str, entry: dict[str, Any], now: str,
              out: dict[str, Any]) -> dict[str, Any]:
    """Step 4 (idempotent): publication, the audit record, then the journal closed. `sid` is the
    requested document (the journal was validated against it); the entry never names the target."""
    from app.workers import publication
    warnings: list[str] = []
    try:
        pub = publication.publish(root, intent=entry["intent"], sources=[sid], index=True,
                                  keyword=True, origin="repair_manifest", now=now)
        warnings = list(pub.get("warnings") or [])
    except Exception as exc:  # noqa: BLE001 - the queued intent keeps it for a later publish
        warnings = [f"publication_failed:{type(exc).__name__}"]
    audit = _audit_path(root, sid, entry["recovery_id"])
    record = _audit_record(sid, entry, now)
    existing = authority_read.read_file(audit)
    if existing.state == authority_read.ABSENT:
        durable_write.write_json(audit, record, create_only=True)
    elif existing.state != authority_read.OK or not _audit_matches(_json_obj(existing.data),
                                                                    record):
        out.update(exit=EXIT_REFUSED, wrote=True, message=(
            f"The manifest of {sid} was restored, but its audit record {audit.name} exists and "
            "doesn't match this recovery; the recovery journal stays open. Inspect again."))
        return out
    else:
        durable_write.sync_existing(audit)
    durable_write.remove(journal_path(root, sid), missing_ok=True)
    note = (" Publication is still pending (" + ", ".join(warnings) + "); it is queued and "
            "retried by the next publish or `scripts/republish.py`." if warnings else "")
    out.update(exit=EXIT_OK, wrote=True, message=(
        f"Manifest of {sid} restored from {entry['archive']}; the damaged bytes are kept in "
        f"raw/manifests/{QUARANTINE_DIR}/{entry['quarantine']}.{note}"))
    return out


def finish(root: Path, source_id: str, *, expected_state: str, backups_dir: Path | None = None,
           now: str | None = None) -> dict[str, Any]:
    """Roll an open recovery forward from its journal (a crash after the intent was recorded).
    Every refusal (journal invalid, copy gone or no longer compatible) precedes the first write."""
    root = Path(root).resolve()
    now = now or manifests.iso_now()
    out: dict[str, Any] = {"exit": EXIT_REFUSED, "message": "", "wrote": False}

    def body() -> dict[str, Any]:
        got = _fresh(root, source_id, backups_dir, expected_state, out)
        if got is None:
            return out
        snap, report = got
        if report["finish"] is None:
            out["message"] = ("Refused before any write: " + " ".join(report["findings"])
                              + " Nothing was changed.")
            return out
        entry = snap["journal"]["entry"]
        route = _journal_route(snap)
        copy = _journal_copy(snap) if route == "pending_install" else None
        manifest = manifests.manifest_path(manifests_dir(root), source_id)
        write_lock.invalidate()
        stage = "barrier"
        try:
            # The earlier run may have failed after a rename, during its flush: repeat each
            # barrier before anything relies on those files.
            durable_write.sync_existing(journal_path(root, source_id))
            durable_write.sync_existing(quarantine_dir(root) / entry["quarantine"])
            if route == "pending_install":
                stage = "install"
                durable_write.write_bytes(manifest, copy["bytes"])
            else:
                durable_write.sync_existing(manifest)
            stage = "complete"
            return _complete(root, source_id, entry, now, out)
        except OSError as exc:
            return _uncertain(source_id, stage, exc, out)

    return _guarded(root, out, body)
