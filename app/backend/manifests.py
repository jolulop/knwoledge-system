#!/usr/bin/env python3
"""Shared helpers for content-keyed source manifests.

Manifests at ``raw/manifests/<source_id>.json`` are the authoritative per-source
record (ADR-0008). Intake (Phase 1) creates them from inbox scans; extraction
(Phase 2) updates them with normalization state. This module centralizes the
identity, read, and write logic both stages share, so there is exactly one canonical
manifest writer (``save_manifest``) and one formatting convention.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.backend import authority_read, durable_write

_CHUNK = 1 << 20  # 1 MiB streaming read for checksums

# Canonical content-derived source id (ADR-0007): src_<first 16 hex chars of SHA256>. Manifest JSON is
# durable *untrusted* local input (CLAUDE.md rule 2), so a hostile/hand-edited source_id (absolute path,
# `..`, slashes) must never reach the filesystem — every manifest path is gated on this shape. Mirrors
# `app/workers/citations.py:_SOURCE_ID`.
_SOURCE_ID_RE = re.compile(r"src_[0-9a-f]{16}")


def is_source_id(value: Any) -> bool:
    """True iff `value` is a canonical `src_<16 hex>` source id.

    Uses `fullmatch` (NOT `match` + `^…$`, which accepts a trailing newline because `$` matches before
    it) — this gates untrusted ids before filesystem/graph use, so the whole string must be exact."""
    return isinstance(value, str) and bool(_SOURCE_ID_RE.fullmatch(value))


def _require_source_id(source_id: Any) -> str:
    """Raise on a non-canonical source id (the write-path guard)."""
    if not is_source_id(source_id):
        raise ValueError(f"invalid source_id {source_id!r}; expected src_<16 hex>")
    return source_id


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def source_id_for(sha256: str) -> str:
    """Deterministic content-derived source id: src_<first 16 hex chars>."""
    return f"src_{sha256[:16]}"


def manifest_path(manifests_dir: Path, source_id: str) -> Path:
    """The on-disk path for a manifest. Validated chokepoint: a non-canonical id raises (so nothing can
    build a manifest path that escapes the manifests dir, ADR-0009)."""
    _require_source_id(source_id)
    return Path(manifests_dir) / f"{source_id}.json"


# --------------------------------------------------------------------------- typed read states
# ADR-0074 decision 2. A manifest read has exactly one of four states; only `absent` is a verified
# "no manifest here", so only `absent` may ever lead to creating one, and a damaged manifest is never
# folded into "missing" or dropped from an inventory without being counted.
# The states and the byte reading are shared with every authority family (`authority_read`).
ABSENT, UNREADABLE, CORRUPT, OK = (authority_read.ABSENT, authority_read.UNREADABLE,
                                   authority_read.CORRUPT, authority_read.OK)
DAMAGED = authority_read.DAMAGED

# Identity quarantine reasons of `scan_manifests` (categorical; never the malformed text itself).
IDENTITY_REASONS = ("non_canonical_id", "filename_mismatch", "duplicate_source_id")

_UNSAFE = re.compile(r"[^\x21-\x7e]")


@dataclass(frozen=True)
class ManifestRead:
    """One manifest read: `state`, the parsed `record` (only when `ok`), and for a damaged read a
    categorical `error` (an exception class name or a fixed tag — never file contents)."""
    state: str
    path: Path
    record: dict[str, Any] | None = None
    error: str | None = None

    @property
    def damaged(self) -> bool:
        return self.state in DAMAGED


class ManifestUnavailable(Exception):
    """A manifest exists but is unreadable or corrupt (or, from an inventory, quarantined by identity
    validation): nothing may treat it as absent, recreate it or
    overwrite it (ADR-0074 decisions 2–3). The message is categorical."""

    def __init__(self, source_id: str, state: str) -> None:
        shown = f"quarantined ({state})" if state in IDENTITY_REASONS else state
        super().__init__(f"manifest {source_id} is {shown}; it is left as it is (no write, no "
                         "recreation) until an operator recovers it")
        self.source_id = source_id
        self.state = state


class IncompleteManifestInventory(Exception):
    """An action whose correctness depends on proving a document absent was refused because the
    manifest inventory is incomplete (a damaged manifest, or the directory could not be listed)."""


def safe_display(name: str, *, limit: int = 120) -> str:
    """`raw/manifests/<name>` with every non-printable or non-ASCII character escaped and the name
    truncated — a file name is untrusted input, so reports render it, never echo it raw."""
    return f"raw/manifests/{authority_read.safe_name(name, limit=limit)}"


_lexists = authority_read.lexists


def read_manifest_file(path: Path) -> ManifestRead:
    """The typed read of one manifest file (ADR-0074 decision 2). Never raises for the file's state."""
    read = authority_read.read_file(path)
    if read.state != OK:
        return ManifestRead(read.state, read.path, error=read.error)
    return manifest_from_bytes(read.path, read.data)


def manifest_from_bytes(path: Path, data: bytes, source_id: str | None = None) -> ManifestRead:
    """The typed state of manifest bytes already read (`ok` or `corrupt`). With `source_id`, a
    record naming another id is `corrupt` (`id_mismatch`), as :func:`read_manifest` reports it."""
    try:
        record = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        return ManifestRead(CORRUPT, path, error="invalid_utf8")
    except (ValueError, RecursionError):
        return ManifestRead(CORRUPT, path, error="invalid_json")
    if not isinstance(record, dict):
        return ManifestRead(CORRUPT, path, error="not_an_object")
    if source_id is not None and record.get("source_id") != source_id:
        return ManifestRead(CORRUPT, path, error="id_mismatch")
    return ManifestRead(OK, path, record=record)


def read_manifest(manifests_dir: Path, source_id: str) -> ManifestRead:
    """The typed read of `raw/manifests/<source_id>.json`. A non-canonical id raises ValueError (it
    names no manifest path). An `ok` record whose own `source_id` differs from the file's is
    reported as `corrupt` (`id_mismatch`): it is not this document's manifest."""
    path = manifest_path(manifests_dir, source_id)
    read = read_manifest_file(path)
    if read.state == OK and read.record.get("source_id") != source_id:
        return ManifestRead(CORRUPT, path, error="id_mismatch")
    return read


def get_manifest(manifests_dir: Path, source_id: str) -> dict[str, Any] | None:
    """The record when it is usable, None **only** for a verified absence; a damaged manifest raises
    :class:`ManifestUnavailable` (never folded into None). A non-canonical id raises ValueError."""
    read = read_manifest(manifests_dir, source_id)
    if read.state == ABSENT:
        return None
    if read.state != OK:
        raise ManifestUnavailable(source_id, read.state)
    return read.record


@dataclass
class ManifestInventory:
    """The manifest inventory (ADR-0074 decision 2): the identity-validated records, every skipped
    file by categorical reason, and whether the inventory is complete enough to prove an absence.

    - `valid` — canonical records (canonical id, filename stem equals it, no duplicate);
    - `skipped` — one categorical reason per skipped file: the identity reasons plus `unreadable`
      and `corrupt` (never the file's name or contents);
    - `damaged` — `(safe display path, state)` per unreadable / corrupt file, for operator reports;
    - `damaged_ids` — `{source_id: state}` for damaged files named like a canonical id;
    - `quarantined_ids` — `{source_id: identity reason}` for every canonical id an
      identity-quarantined file names (its filename stem, or the `source_id` its record claims):
      that id's authority is ambiguous, so it is neither usable nor provably absent;
    - `enumeration_error` — set when `raw/manifests/` itself could not be listed.

    Identity quarantine keeps its reason names in `skipped` and in the report, but a quarantined
    file is excluded authority like a damaged one: the inventory is not complete while one exists.
    """
    valid: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    damaged: list[tuple[str, str]] = field(default_factory=list)
    damaged_ids: dict[str, str] = field(default_factory=dict)
    quarantined_ids: dict[str, str] = field(default_factory=dict)
    enumeration_error: str | None = None

    @property
    def quarantined(self) -> int:
        """How many files identity validation excluded."""
        return sum(1 for r in self.skipped if r in IDENTITY_REASONS)

    @property
    def complete(self) -> bool:
        """Whether the inventory can prove a document absent: listed, nothing damaged and nothing
        identity-quarantined (excluded, ambiguous authority never establishes an absence)."""
        return self.enumeration_error is None and not self.damaged and not self.quarantined

    @property
    def coverage(self) -> str:
        if self.enumeration_error is not None:
            return "unavailable"
        return "complete" if self.complete else "incomplete"

    @property
    def valid_ids(self) -> set[str]:
        return {m["source_id"] for m in self.valid}

    def excluded_reason(self, source_id: str) -> str | None:
        """Why `source_id`'s authority is excluded by this inventory (`manifest_<state>` for a
        damaged file, `manifest_<identity reason>` for a quarantined one), or None."""
        if source_id in self.damaged_ids:
            return f"manifest_{self.damaged_ids[source_id]}"
        if source_id in self.quarantined_ids:
            return f"manifest_{self.quarantined_ids[source_id]}"
        return None

    def report(self) -> dict[str, Any]:
        """The job-result fields every runtime worker reports (counts only)."""
        counts = Counter(self.skipped)
        return {"manifests_skipped_invalid": sum(counts[r] for r in IDENTITY_REASONS),
                "manifests_unreadable": counts[UNREADABLE], "manifests_corrupt": counts[CORRUPT],
                "manifest_coverage": self.coverage}

    def require_complete(self, action: str) -> None:
        """Refuse an absence-dependent `action` on an incomplete inventory."""
        if self.enumeration_error is not None:
            raise IncompleteManifestInventory(
                f"raw/manifests could not be listed ({self.enumeration_error}); {action} needs a "
                "complete manifest inventory and was not done")
        if self.damaged:
            raise IncompleteManifestInventory(
                f"{len(self.damaged)} manifest(s) unreadable or corrupt; {action} needs a complete "
                "manifest inventory and was not done")
        if self.quarantined:
            raise IncompleteManifestInventory(
                f"{self.quarantined} manifest(s) quarantined by identity validation; {action} needs "
                "a complete manifest inventory and was not done")


def manifest_files(manifests_dir: Path) -> tuple[list[Path], str | None]:
    """`(the *.json entries of raw/manifests, sorted; enumeration error or None)`. A missing directory
    is a verified empty inventory; a directory that cannot be listed (permission, not a directory,
    a dangling link) is an error — never an empty inventory."""
    return authority_read.list_entries(manifests_dir, ".json")


def scan_manifests(manifests_dir: Path) -> ManifestInventory:
    """Read every manifest and partition it — the runtime-worker entry point.

    A manifest's `source_id` flows into filesystem paths (Source pages, normalized files) and graph
    node ids, and manifest JSON is untrusted local input (AGENTS.md). A record is **valid** only when
    all hold: the `source_id` is canonical `src_<16 hex>`, the **filename stem equals it** (ADR-0007:
    `raw/manifests/<source_id>.json`), and the id has **not already been seen**. This blocks a
    tampered / misnamed / duplicate manifest from driving a worker for some id and clobbering that
    id's artifacts. Unreadable and corrupt files are counted, never dropped (ADR-0074 decision 2)."""
    inv = ManifestInventory()
    paths, inv.enumeration_error = manifest_files(manifests_dir)
    records: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        read = read_manifest_file(path)
        if read.state == OK:
            records.append((path, read.record))
        elif read.state == ABSENT:                       # removed between the listing and the read
            continue
        else:
            inv.skipped.append(read.state)
            inv.damaged.append((safe_display(path.name), read.state))
            if is_source_id(path.stem):
                inv.damaged_ids[path.stem] = read.state
    # A canonical content source_id appearing in 2+ files is ambiguous authority — quarantine every
    # copy (order-independent), even the correctly-named one: with a tampered duplicate present, none
    # is trusted.
    id_counts: dict[str, int] = {}
    for _p, rec in records:
        sid = rec.get("source_id")
        if is_source_id(sid):
            id_counts[sid] = id_counts.get(sid, 0) + 1
    for path, rec in records:
        sid = rec.get("source_id")
        if not is_source_id(sid):
            reason = "non_canonical_id"
        elif id_counts[sid] > 1:
            reason = "duplicate_source_id"
        elif path.stem != sid:
            reason = "filename_mismatch"
        else:
            inv.valid.append(rec)
            continue
        inv.skipped.append(reason)
        for named in (path.stem, sid):                  # every canonical id the file names
            if is_source_id(named):
                inv.quarantined_ids.setdefault(named, reason)
    return inv


def lookup(manifests_dir: Path, source_id: str,
           inventory: ManifestInventory | None = None) -> dict[str, Any] | None:
    """One document's usable manifest **as the inventory sees it** — the lookup every per-document
    API surface shares, so `/sources/{id}`, `/raw/{id}`, `/sources` and search agree:

    - the identity-validated record when `source_id` is in the inventory's `valid` set;
    - :class:`ManifestUnavailable` when its file is damaged or identity validation quarantined it
      (a duplicate elsewhere included);
    - :class:`IncompleteManifestInventory` when `raw/manifests` can't be listed — an enumeration
      failure never establishes an absence, nor rules out a duplicate;
    - None only for a verified absence.

    Damage or quarantine of *other* documents never blocks this one. A non-canonical id raises
    ValueError (it names no manifest)."""
    if not is_source_id(source_id):
        raise ValueError(f"not a canonical source id: {safe_display(str(source_id))}")
    inv = inventory if inventory is not None else scan_manifests(manifests_dir)
    if inv.enumeration_error is not None:
        raise IncompleteManifestInventory(
            f"raw/manifests could not be listed ({inv.enumeration_error}); source {source_id} "
            "can't be confirmed present or absent")
    record = next((m for m in inv.valid if m.get("source_id") == source_id), None)
    if record is not None:
        return record
    if source_id in inv.damaged_ids:
        raise ManifestUnavailable(source_id, inv.damaged_ids[source_id])
    if source_id in inv.quarantined_ids:
        raise ManifestUnavailable(source_id, inv.quarantined_ids[source_id])
    return None


def recheck(manifests_dir: Path, source_id: str, inventory: ManifestInventory) -> ManifestRead:
    """Re-read one document's manifest under the write lock, before a mutation or a recovery
    acts on it: the fresh typed read, except that an id `inventory` quarantined stays excluded
    (`error` names the identity reason; the single file can't show a duplicate elsewhere). Callers
    act only on `ok`, and treat `absent` as an absence only when that is their branch's premise."""
    read = read_manifest(manifests_dir, source_id)
    if source_id in inventory.quarantined_ids:
        return ManifestRead(CORRUPT, read.path, error=inventory.quarantined_ids[source_id])
    return read


def read_reason(read: ManifestRead) -> str:
    """The categorical error reason for a non-`ok` read: `manifest_<identity reason>` for a
    quarantined id (see :func:`recheck`), else `manifest_<state>`."""
    if read.error in IDENTITY_REASONS:
        return f"manifest_{read.error}"
    return f"manifest_{read.state}"


def normalized_paths(source_id: str) -> dict[str, str]:
    """Repository-relative normalized-layer paths for a source (ADR-0011)."""
    return {
        "markdown_path": f"normalized/markdown/{source_id}.md",
        "chunks_path": f"normalized/chunks/{source_id}.jsonl",
        "tables_dir": f"normalized/tables/{source_id}",
        "extraction_log_path": f"normalized/extraction_logs/{source_id}.json",
    }


# The fields :func:`apply_extraction_state` owns. Extraction writes back only these, onto the
# manifest as it is on disk when it commits (ADR-0068: a status changed during the run survives).
EXTRACTION_FIELDS = ("ingestion_status", "normalized", "extracted_at", "extraction_tool",
                     "extraction_tool_version", "text_char_count", "chunk_count", "page_count")


def apply_extraction_state(
    manifest: dict[str, Any],
    *,
    ingestion_status: str,
    extracted_at: str | None,
    extraction_tool: str | None,
    extraction_tool_version: str | None,
    text_char_count: int,
    chunk_count: int,
    page_count: int | None,
) -> None:
    """Set Phase 2 extraction fields on a manifest in place (Phase 2 Plan §4).

    Phase 1 fields (occurrences, sha256, retention_class, …) are left untouched;
    ``retention_class`` deliberately stays whatever intake set (``unknown`` in Phase 2).
    """
    manifest["ingestion_status"] = ingestion_status
    manifest["normalized"] = normalized_paths(manifest["source_id"])
    manifest["extracted_at"] = extracted_at
    manifest["extraction_tool"] = extraction_tool
    manifest["extraction_tool_version"] = extraction_tool_version
    manifest["text_char_count"] = text_char_count
    manifest["chunk_count"] = chunk_count
    manifest["page_count"] = page_count


def save_manifest(manifests_dir: Path, manifest: dict[str, Any]) -> Path:
    """Write a manifest with the canonical formatting.

    This is the single place manifests are written: 2-space indent, UTF-8, trailing
    newline. Both intake and extraction route writes through here. The write is durable and
    all-or-nothing (ADR-0074 decision 1): an interruption leaves the previous manifest intact.
    """
    path = manifest_path(Path(manifests_dir), manifest["source_id"])
    durable_write.write_json(path, manifest)
    return path


# Optional source-provenance fields (manifest-owned) that the promotion lifecycle uses to
# judge source independence (ADR-0018, Phase 3.5b slice 5). All null/absent by default; not
# auto-derived — a human sets them (ADR-0066 part 3: the document screen's Details confirmation).
# The graph never owns these. ADR-0066 part 3 slice 3 adds the confirmed `title`, `authors` (an
# ordered list of {name, show_page}), `publisher` as {name, show_page} (a legacy string still reads),
# and the audit fields `confirmed_at` / `confirmed_baseline`; the legacy string `author` is read only
# when `authors` is absent.
PROVENANCE_FIELDS = ("title", "authors", "author", "publisher", "report_family", "canonical_url",
                     "confirmed_at", "confirmed_baseline")
# The Details a confirmation sets and a Clear removes (report_family / canonical_url are kept).
DETAILS_FIELDS = ("title", "authors", "author", "publisher", "confirmed_at", "confirmed_baseline")
_MAX_NAME = 200
_WS = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _clean(value: Any, max_chars: int = _MAX_NAME) -> str:
    if not isinstance(value, str):
        return ""
    import unicodedata
    text = _WS.sub(" ", _CONTROL.sub(" ", unicodedata.normalize("NFKC", value))).strip()
    return text[:max_chars].strip()


def name_identity(name: Any) -> str:
    """The normalized identity of a provenance name (ADR-0066 part 3 grill 2 Q3): NFKC, control
    characters and whitespace normalized, casefold. Not `merge_key` (no singularization); accents
    and non-Latin characters stay. Shared by independence, its digest and the Authors pages."""
    return _clean(name).casefold()


def get_provenance(manifest: dict[str, Any]) -> dict[str, Any]:
    """Read a manifest's provenance sub-dict (missing keys -> absent)."""
    prov = manifest.get("provenance") or {}
    return {k: prov.get(k) for k in PROVENANCE_FIELDS if prov.get(k) is not None}


def confirmed_details(prov: dict[str, Any]) -> dict[str, Any]:
    """The confirmed Details in their current shape, legacy values read (never rewritten):
    `{title, authors: [{name, show_page}], publisher: {name, show_page} | None, confirmed_at}`.
    `authors` wins whenever present, even empty; a legacy string `author` reads as one name."""
    if "authors" in prov:
        authors = [{"name": a["name"], "show_page": bool(a.get("show_page"))}
                   for a in prov.get("authors") or [] if isinstance(a, dict) and a.get("name")]
    elif isinstance(prov.get("author"), str) and prov["author"].strip():
        authors = [{"name": prov["author"].strip(), "show_page": False}]
    else:
        authors = []
    pub = prov.get("publisher")
    if isinstance(pub, dict) and pub.get("name"):
        publisher = {"name": pub["name"], "show_page": bool(pub.get("show_page"))}
    elif isinstance(pub, str) and pub.strip():
        publisher = {"name": pub.strip(), "show_page": False}
    else:
        publisher = None
    return {"title": prov.get("title") if isinstance(prov.get("title"), str) else None,
            "authors": authors, "publisher": publisher, "confirmed_at": prov.get("confirmed_at")}


def _canon_url(value: str) -> str:
    return " ".join(value.split()).strip().casefold().split("#", 1)[0].rstrip("/")


def independence_inputs(prov: dict[str, Any]) -> dict[str, Any]:
    """The one normalization of a source's independence inputs (ADR-0018; ADR-0066 part 3 slice 3):
    the author identity set, the publisher identity, `report_family` and `canonical_url`. Order,
    spelling variants covered by `name_identity`, `show_page`, the title and the confirmation audit
    fields never change it. Empty values are omitted."""
    details = confirmed_details(prov)
    out: dict[str, Any] = {}
    authors = sorted({name_identity(a["name"]) for a in details["authors"]} - {""})
    if authors:
        out["authors"] = authors
    if details["publisher"] and name_identity(details["publisher"]["name"]):
        out["publisher"] = name_identity(details["publisher"]["name"])
    family = _clean(prov.get("report_family")).casefold()
    if family:
        out["report_family"] = family
    url = _canon_url(prov["canonical_url"]) if isinstance(prov.get("canonical_url"), str) else ""
    if url:
        out["canonical_url"] = url
    return out


def independence_digest(prov: dict[str, Any]) -> str:
    """A digest of :func:`independence_inputs` (reconciliation notices, ADR-0066 part 3 slice 3)."""
    data = json.dumps(independence_inputs(prov), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def independent_sources(p1: dict[str, Any], p2: dict[str, Any]) -> bool:
    """Two sources are independent iff >=1 *comparable* independence input (known on both) differs
    and no comparable input is shared (ADR-0018). Authors compare as identity sets: **any shared
    author** is shared provenance ({Alice, Bob} vs {Alice, Carol} are not independent), and only
    disjoint sets differ. Non-comparable/unknown inputs never prove independence, so the gate stays
    conservative until provenance is populated. The single home for the independence rule, shared
    by the promotion lifecycle, contradiction detection and synthesis."""
    a, b = independence_inputs(p1), independence_inputs(p2)
    comparable = [k for k in a if k in b]
    if not comparable:
        return False
    for key in comparable:
        if key == "authors":
            if set(a[key]) & set(b[key]):
                return False
        elif a[key] == b[key]:
            return False
    return True


def _check_field(key: str, value: Any) -> Any:
    """Validate (and clean) one provenance value; raise ValueError on malformed input."""
    if key == "title":
        text = _clean(value, 300)
        if not text:
            raise ValueError("a confirmed title must not be empty")
        return text
    if key == "authors":
        if not isinstance(value, list):
            raise ValueError("authors must be a list")
        out, seen = [], set()
        for a in value:
            if not isinstance(a, dict) or set(a) != {"name", "show_page"}:
                raise ValueError("each author is {name, show_page}")
            if not isinstance(a["show_page"], bool):
                raise ValueError("show_page must be a boolean")
            name = _clean(a["name"])
            if not name:
                raise ValueError("an author name must not be empty")
            if name_identity(name) in seen:
                raise ValueError(f"duplicate author: {name}")
            seen.add(name_identity(name))
            out.append({"name": name, "show_page": a["show_page"]})
        return out
    if key == "publisher":
        if isinstance(value, str):                 # the legacy shape, still accepted
            text = _clean(value)
            if not text:
                raise ValueError("a publisher name must not be empty")
            return text
        if not isinstance(value, dict) or set(value) != {"name", "show_page"}:
            raise ValueError("publisher is {name, show_page}")
        if not isinstance(value["show_page"], bool):
            raise ValueError("show_page must be a boolean")
        name = _clean(value["name"])
        if not name:
            raise ValueError("a publisher name must not be empty")
        return {"name": name, "show_page": value["show_page"]}
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def provenance_problem(prov: dict[str, Any]) -> str | None:
    """Why a stored provenance block is malformed (a shape `set_provenance` would refuse), or None.
    Readers that act on confirmed names (the Authors pages) refuse to act on such a block."""
    for key in ("title", "authors", "author", "publisher", "report_family", "canonical_url"):
        if prov.get(key) is not None:
            try:
                _check_field(key, prov[key])
            except ValueError as exc:
                return f"provenance {key}: {exc}"
    return None


# ADR-0074 slice 4: Details history. Every Details change (confirm, clear, or any other write of a
# Details field) writes a `reviews/audit_log/<source_id>-details-<kind>-<id>.json` record durably
# BEFORE the manifest changes, under the caller's write lock. Backups made by code that does this
# carry `details_history: DETAILS_HISTORY_VERSION` in their sidecar: only such a backup can prove
# that no Details change after it went unrecorded (`scripts/repair_manifest.py` restore). An
# interrupted attempt leaves a record without the change — conservative: it may block a restore.
DETAILS_HISTORY_VERSION = 1
DETAILS_AUDIT_INFIX = "-details-"


def details_audit_dir(manifests_dir: Path) -> Path:
    """`reviews/audit_log` of the vault whose manifests live in `manifests_dir` (vault layout)."""
    return Path(manifests_dir).parent.parent / "reviews" / "audit_log"


def _record_details_change(manifests_dir: Path, source_id: str, kind: str, now: str) -> None:
    import uuid
    name = f"{source_id}{DETAILS_AUDIT_INFIX}{kind}-{uuid.uuid4().hex[:8]}.json"
    durable_write.write_json(details_audit_dir(manifests_dir) / name, {
        "source_id": source_id, "event": f"details_{kind}", "at": now, "actor": "human",
        "details_history": DETAILS_HISTORY_VERSION}, create_only=True)


def _details_kind(fields: dict[str, Any]) -> str | None:
    touched = set(fields) & set(DETAILS_FIELDS)
    if not touched:
        return None
    if all(fields[k] is None for k in touched):
        return "cleared"
    return "confirmed" if fields.get("confirmed_at") is not None else "changed"


def set_provenance(manifests_dir: Path, source_id: str, **fields: Any) -> dict[str, Any] | None:
    """Set provenance fields on a manifest (the single write path); returns it or None.

    Values are validated and cleaned (`_check_field`); malformed input raises ValueError and
    writes nothing. An explicit None clears a field back to unknown, which is distinct from
    malformed input. The caller holds the write lock (a screen action's commit phase). A write that
    touches a Details field records its Details audit entry first (ADR-0074 slice 4)."""
    _require_source_id(source_id)  # write path: a non-canonical id raises
    unknown = set(fields) - set(PROVENANCE_FIELDS)
    if unknown:
        raise ValueError(f"unknown provenance field(s) {sorted(unknown)}; allowed: {PROVENANCE_FIELDS}")
    checked = {k: (None if v is None else _check_field(k, v)) for k, v in fields.items()}
    manifest = get_manifest(manifests_dir, source_id)    # damaged: raises, never overwritten
    if manifest is None:
        return None
    kind = _details_kind(fields)
    if kind is not None:                                  # audit first, durably (slice 4)
        _record_details_change(manifests_dir, source_id, kind,
                               str(fields.get("confirmed_at") or iso_now()))
    prov = dict(manifest.get("provenance") or {})
    for key, value in checked.items():
        if value is None:
            prov.pop(key, None)   # explicit None clears the field back to unknown
        else:
            prov[key] = value
    manifest["provenance"] = prov
    save_manifest(manifests_dir, manifest)
    return manifest


def confirm_details(manifests_dir: Path, source_id: str, *, title: str, authors: list[dict[str, Any]],
                    publisher: dict[str, Any] | None, baseline: str, now: str) -> dict[str, Any] | None:
    """Record the human-confirmed Details (ADR-0066 part 3 slice 3): writes the new keys and
    removes the legacy `author`; `report_family` / `canonical_url` are untouched."""
    return set_provenance(manifests_dir, source_id, title=title, authors=authors,
                          publisher=publisher, author=None, confirmed_at=now,
                          confirmed_baseline=baseline)


def clear_details(manifests_dir: Path, source_id: str) -> dict[str, Any] | None:
    """Clear confirmed Details: title, authors, publisher, the legacy author and the audit fields.
    `report_family`, `canonical_url` and every other field stay, so independence may remain
    established through them."""
    return set_provenance(manifests_dir, source_id, **{k: None for k in DETAILS_FIELDS})


# Source lifecycle status — the durable retrieval-visibility authority (ADR-0036 decision 13). The
# manifest carries it (default `active`); the Source page reads it and stays a pure projection, so a
# wiki regen preserves it. Distinct from `retention_class` (policy category). Matches graph NODE_STATUSES.
SOURCE_STATUSES = ("active", "stale_candidate", "deprecated_candidate", "archive_candidate",
                   "archived", "delete_candidate", "deleted", "hidden")


def get_status(manifest: dict[str, Any]) -> str:
    """A manifest's source lifecycle status (default ``active`` when unset)."""
    status = manifest.get("status")
    return status if status in SOURCE_STATUSES else "active"


def set_status(manifests_dir: Path, source_id: str, status: str) -> dict[str, Any] | None:
    """Set a manifest's source lifecycle status (the single write path); return it or None.

    The only writer of `manifest["status"]` (ADR-0036). Raw bytes are never touched. Reversible:
    setting `active` un-archives. Validated against SOURCE_STATUSES.
    """
    _require_source_id(source_id)  # write path: a non-canonical id raises, never returns None
    if status not in SOURCE_STATUSES:
        raise ValueError(f"unknown source status {status!r}; allowed: {list(SOURCE_STATUSES)}")
    manifest = get_manifest(manifests_dir, source_id)    # damaged: raises, never overwritten
    if manifest is None:
        return None
    manifest["status"] = status
    save_manifest(manifests_dir, manifest)
    return manifest
