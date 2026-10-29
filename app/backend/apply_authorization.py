#!/usr/bin/env python3
"""Apply authorizations, `db/apply_authorizations.json` (ADR-0068 decision 6).

Only a clean (`ok`) Apply preview authorizes an Apply. After its sandbox run succeeds and the vault
revision is confirmed unchanged, the preview records an authorization and returns its opaque random
id, the **preview token**. A record binds the token to everything that decides what Apply would do:
the vault revision the snapshot captured, the code and settings fingerprints, and the Apply options.
Apply looks the token up under the write lock, checks every bound value, then consumes the record
before its first write, so a token is single-use.

`{"schema_version", "records": [{"token", "epoch", "counter", "code", "settings", "options",
"created_at"}]}`. Each write prunes the records whose revision is no longer current; Apply rejects a
stale record on its own whether or not it was pruned yet. The file is never backed up, so a restore
cannot bring back old records.

Losing a record can only refuse an Apply, so malformed contents self-heal: the damaged bytes are
copied aside (flushed) and the file is replaced with an empty set, which refuses every outstanding
token as unknown. An I/O failure raises :class:`StoreUnavailable` (Apply refuses with
`authorization_store_unavailable`; a preview returns no token). Every function here runs under the
exclusive write lock (the caller holds it).
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.backend.publication_queue import _sync_dir, _write_durably

STORE_NAME = "apply_authorizations.json"
SCHEMA_VERSION = 1
TOKEN = re.compile(r"apa_[0-9a-f]{32}")
_EPOCH = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_FIELDS = {"token", "epoch", "counter", "code", "settings", "options", "created_at"}


class StoreUnavailable(RuntimeError):
    """The authorization store could not be read or durably written (an I/O failure)."""

    def __init__(self, op: str, exc: OSError) -> None:
        super().__init__(f"apply authorization store unavailable ({op}: {type(exc).__name__}: "
                         f"{exc}); check db/ permissions and free space")
        self.op = op


def store_path(root: Path) -> Path:
    return Path(root) / "db" / STORE_NAME


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid(r: Any) -> bool:
    return (isinstance(r, dict) and set(r) == _FIELDS
            and isinstance(r["token"], str) and bool(TOKEN.fullmatch(r["token"]))
            and isinstance(r["epoch"], str) and bool(_EPOCH.fullmatch(r["epoch"]))
            and type(r["counter"]) is int and r["counter"] >= 0
            and isinstance(r["code"], str) and bool(_HEX64.fullmatch(r["code"]))
            and isinstance(r["settings"], str) and bool(_HEX64.fullmatch(r["settings"]))
            and isinstance(r["options"], dict) and isinstance(r["created_at"], str))


def _parse(data: bytes) -> list[dict[str, Any]] | None:
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (not isinstance(body, dict) or set(body) != {"schema_version", "records"}
            or body["schema_version"] != SCHEMA_VERSION or not isinstance(body["records"], list)
            or not all(_valid(r) for r in body["records"])
            or len({r["token"] for r in body["records"]}) != len(body["records"])):
        return None
    return body["records"]


def _save(root: Path, records: list[dict[str, Any]]) -> None:
    path = store_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp-{uuid.uuid4().hex[:8]}")
        _write_durably(tmp, (json.dumps({"schema_version": SCHEMA_VERSION, "records": records},
                                        indent=2) + "\n").encode("utf-8"))
        tmp.replace(path)
        _sync_dir(path.parent)
    except OSError as exc:
        raise StoreUnavailable("write", exc) from exc


def load(root: Path) -> list[dict[str, Any]]:
    """Every record. A missing file is empty; malformed contents are preserved and replaced."""
    path = store_path(root)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise StoreUnavailable("read", exc) from exc
    records = _parse(data)
    if records is not None:
        return records
    stamp = _iso_now().replace(":", "").replace("-", "")
    try:
        _write_durably(path.with_name(
            f"apply_authorizations.damaged-{stamp}-{uuid.uuid4().hex[:6]}.json"), data)
        _sync_dir(path.parent)
    except OSError as exc:
        raise StoreUnavailable("preserve", exc) from exc
    _save(root, [])
    return []


def issue(root: Path, *, rev: dict[str, Any], code: str, settings: str,
          options: dict[str, Any], now: str | None = None) -> str:
    """Durably record an authorization for `rev` and return its token. Prunes stale records."""
    token = f"apa_{uuid.uuid4().hex}"
    records = [r for r in load(root) if (r["epoch"], r["counter"]) == (rev["epoch"], rev["counter"])]
    records.append({"token": token, "epoch": rev["epoch"], "counter": rev["counter"], "code": code,
                    "settings": settings, "options": dict(options),
                    "created_at": now or _iso_now()})
    _save(root, records)
    return token


def find(root: Path, token: str) -> dict[str, Any] | None:
    """The record a token names, or None (unknown, consumed or pruned)."""
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        return None
    return next((r for r in load(root) if r["token"] == token), None)


def consume(root: Path, token: str) -> None:
    """Durably remove a token's record (Apply spends it before its first write)."""
    _save(root, [r for r in load(root) if r["token"] != token])


def clear(root: Path) -> None:
    """Durably drop every record (a restore: no earlier preview may authorize the restored vault)."""
    _save(root, [])


def damaged_copies(root: Path) -> list[Path]:
    db = Path(root) / "db"
    return sorted(db.glob("apply_authorizations.damaged-*.json")) if db.exists() else []
