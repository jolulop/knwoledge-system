#!/usr/bin/env python3
"""Tagging history: has a document ever had a tag decision? (ADR-0070 §6, ADR-0072 decisions 4–5).

Two records, both under `reviews/tag_links/` (they travel with the ledgers through backup/restore):

- **Per ledger** — `tagging: {state: "tagged" | "unknown", first_decision_at, earliest_known_decision_at}`
  in `reviews/tag_links/<source_id>.json`. *Tagged, first time unknown* is `tagged` with
  `first_decision_at: null`. *Known never tagged* is never stored (derived, below).
- **Per vault** — `_tracking.json`: `{since, from_start}`, provenance saying from when tracking is
  complete. `from_start: true` is written only by an explicit fresh-vault initialization
  (:func:`init_fresh`, refused when any ledger or tag audit entry exists — a safety check, not a
  certification); the repair migration writes `from_start: false` only when the record is absent
  and never advances or downgrades an existing one. A missing or unreadable record never blocks
  tagging; it only reduces certainty.

**What sets the marker:** the first successfully recorded keep, drop, add, "use X" or "never suggest"
chosen on the document's screen (:func:`mark_decision`, inside that decision's commit). Undo never
clears it. **Evidence** for older ledgers: the ledger's kept/drop timestamps and **attributable** tag
audit entries (tag events that carry the `source_id`). An evidence timestamp gives
`earliest_known_decision_at`; it becomes `first_decision_at` only when tracking completeness
establishes nothing earlier was missed. **Display is read-only** (:func:`classify`).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

TRACKING_NAME = "_tracking.json"
TAGGED, UNKNOWN = "tagged", "unknown"
# Display states (classify): stored tagged / stored-or-derived unknown / derived never.
SHOWN_TAGGED, SHOWN_NEVER, SHOWN_UNKNOWN = "tagged", "never_tagged", "history_unknown"
# Tag audit events attributable to the document they name (`source_id` in the record).
TAG_EVENTS = frozenset({"tag-kept", "tag-dropped", "tag-resolved", "tag-blocked",
                        "tag-drop-undone", "tag-aliases-edited"})


def _dir(reviews_dir: Path) -> Path:
    return Path(reviews_dir) / "tag_links"


def tracking_path(reviews_dir: Path) -> Path:
    return _dir(reviews_dir) / TRACKING_NAME


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def tracking_problem(reviews_dir: Path) -> str | None:
    """Why an EXISTING `_tracking.json` is malformed (for `validate_tag_links`), or None (absent or
    well formed). An unreadable file counts as malformed here; display treats both as no record."""
    path = tracking_path(reviews_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"unreadable ({type(exc).__name__})"
    if not isinstance(data, dict) or set(data) != {"since", "from_start"}:
        return "not a {since, from_start} object"
    if _parse_time(data["since"]) is None:
        return "`since` is not an ISO timestamp with a time zone"
    if not isinstance(data["from_start"], bool):
        return "`from_start` is not true/false"
    return None


def load_tracking(reviews_dir: Path) -> dict[str, Any] | None:
    """The tracking record, or None when absent, unreadable or malformed (provenance only: no record
    means less certainty, never a refusal)."""
    if tracking_problem(reviews_dir) is not None or not tracking_path(reviews_dir).exists():
        return None
    return json.loads(tracking_path(reviews_dir).read_text(encoding="utf-8"))


def _write_tracking(reviews_dir: Path, record: dict[str, Any]) -> None:
    path = tracking_path(reviews_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _tag_audit_exists(reviews_dir: Path) -> bool:
    audit = Path(reviews_dir) / "audit_log"
    return audit.is_dir() and any(audit.glob("*-tag-*.json"))


def init_fresh(reviews_dir: Path, now: str) -> dict[str, Any]:
    """Explicit fresh-vault initialization (ADR-0072 decision 5): record `from_start: true`. Refused
    when a tracking record, any ledger or any tag audit entry already exists — the human running it
    certifies the vault is fresh; this check only catches an obvious mistake. The caller holds the
    write lock. Returns `{"outcome": "initialized" | "refused", "reason"?}`."""
    if tracking_path(reviews_dir).exists():
        return {"outcome": "refused", "reason": "tracking_record_exists"}
    ledgers = _dir(reviews_dir)
    if ledgers.is_dir() and any(p.name != TRACKING_NAME for p in ledgers.glob("*.json")):
        return {"outcome": "refused", "reason": "ledgers_exist"}
    if _tag_audit_exists(reviews_dir):
        return {"outcome": "refused", "reason": "tag_audit_exists"}
    _write_tracking(reviews_dir, {"since": now, "from_start": True})
    return {"outcome": "initialized"}


def ensure_migration_record(reviews_dir: Path, now: str) -> bool:
    """The repair migration's tracking record: `from_start: false` from `now`, written ONLY when no
    record exists (an existing one is preserved: never advanced, never downgraded). True if written."""
    if tracking_path(reviews_dir).exists():
        return False
    _write_tracking(reviews_dir, {"since": now, "from_start": False})
    return True


def complete_for(tracking: dict[str, Any] | None, discovered_at: Any) -> bool:
    """Whether tracking is known complete for a document: `from_start: true`, or the document was
    discovered strictly after `since` (an equal or unparsable time is ambiguous: not complete)."""
    if not tracking:
        return False
    if tracking.get("from_start") is True:
        return True
    since, found = _parse_time(tracking.get("since")), _parse_time(discovered_at)
    return since is not None and found is not None and found > since


NO_EVIDENCE: dict[str, Any] = {"times": [], "untimed": 0}


def audit_evidence(reviews_dir: Path) -> dict[str, dict[str, Any]]:
    """`{source_id: {times: [usable timestamps], untimed: <count>}}` of attributable tag audit events
    (B2c review round 1: an attributable event is evidence of a decision whether or not its
    timestamp is usable — existence and timing are tracked apart). Unreadable files are skipped."""
    out: dict[str, dict[str, Any]] = {}
    audit = Path(reviews_dir) / "audit_log"
    for path in sorted(audit.glob("*-tag-*.json")) if audit.is_dir() else []:
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(rec, dict) or rec.get("event") not in TAG_EVENTS:
            continue
        sid, at = rec.get("source_id"), rec.get("at")
        if not isinstance(sid, str) or not sid:
            continue                                          # not attributable to a document
        ev = out.setdefault(sid, {"times": [], "untimed": 0})
        if _parse_time(at) is not None:
            ev["times"].append(at)
        else:
            ev["untimed"] += 1
    return out


def tagging_problem(value: Any) -> str | None:
    """Why a ledger's `tagging` value is malformed, or None (a damaged record is never defaulted)."""
    if not isinstance(value, dict) or set(value) != {"state", "first_decision_at",
                                                     "earliest_known_decision_at"}:
        return "`tagging` is not a {state, first_decision_at, earliest_known_decision_at} object"
    state, first, earliest = (value["state"], value["first_decision_at"],
                              value["earliest_known_decision_at"])
    if state not in (TAGGED, UNKNOWN):
        return "`tagging.state` is not tagged/unknown"
    for t in (first, earliest):
        if t is not None and _parse_time(t) is None:
            return "`tagging` holds a timestamp that isn't ISO with a time zone"
    if state == UNKNOWN and (first is not None or earliest is not None):
        return "an unknown `tagging` history can't carry timestamps"
    if first is not None and earliest is None:
        return "`tagging.first_decision_at` without `earliest_known_decision_at`"
    return None


def _ledger_evidence(ledger: dict[str, Any]) -> dict[str, Any]:
    """Every kept and drop entry is evidence of a decision; `times` holds the usable timestamps."""
    entries = [v for v in (ledger.get("kept") or {}).values() if isinstance(v, dict)]
    entries += [d for d in ledger.get("drops") or [] if isinstance(d, dict)]
    times = [e.get("at") for e in entries if _parse_time(e.get("at")) is not None]
    return {"times": times, "untimed": len(entries) - len(times)}


def _combined(*evidence: dict[str, Any] | None) -> dict[str, Any]:
    evs = [e for e in evidence if e]
    return {"times": [t for e in evs for t in e.get("times") or []],
            "untimed": sum(int(e.get("untimed") or 0) for e in evs)}


def _exists(ev: dict[str, Any]) -> bool:
    return bool(ev["times"]) or ev["untimed"] > 0


def _from_evidence(ev: dict[str, Any], *, complete: bool) -> dict[str, Any]:
    """Tagged from evidence: the earliest USABLE time is the earliest known; it is the first decision
    only when tracking is complete AND no untimed evidence could precede it."""
    earliest = _earliest(ev["times"])
    first = earliest if complete and earliest is not None and ev["untimed"] == 0 else None
    return {"state": TAGGED, "first_decision_at": first, "earliest_known_decision_at": earliest}


def _earliest(times: list[str]) -> str | None:
    parsed = [(_parse_time(t), t) for t in times if _parse_time(t) is not None]
    return min(parsed)[1] if parsed else None


def backfill(ledger: dict[str, Any], *, complete: bool,
             audit: dict[str, Any] | None) -> dict[str, Any]:
    """The evidence-based `tagging` of an existing ledger that has none (the repair migration and a
    decision on an unmigrated ledger share it). ANY evidence — a kept/drop entry or an attributable
    audit event, timed or not — means tagged (:func:`_from_evidence` for the times); none means
    unknown."""
    ev = _combined(_ledger_evidence(ledger), audit)
    if not _exists(ev):
        return {"state": UNKNOWN, "first_decision_at": None, "earliest_known_decision_at": None}
    return _from_evidence(ev, complete=complete)


def mark_decision(ledger: dict[str, Any], *, ledger_existed: bool, complete: bool,
                  audit: dict[str, Any] | None, now: str) -> dict[str, Any]:
    """The `tagging` a successfully recorded decision leaves (computed under the lock, persisted with
    that decision's ledger write only). Never clears. The action itself is DATED evidence: it fills
    `earliest_known_decision_at` when every older piece of evidence is untimed, while
    `first_decision_at` stays null — unknown or untimed history never becomes a precise first time.
    Only a document with no earlier evidence at all, under complete tracking, gets `now` as its
    first decision."""
    current = ledger.get("tagging")
    if isinstance(current, dict) and current.get("state") == TAGGED:
        if current.get("earliest_known_decision_at") is None:     # all earlier evidence untimed
            return {**current, "earliest_known_decision_at": now}
        return current
    if isinstance(current, dict):                            # stored unknown
        return {"state": TAGGED, "first_decision_at": None, "earliest_known_decision_at": now}
    audit = audit or NO_EVIDENCE
    if not ledger_existed and not _exists(_combined(audit)) and complete:
        return {"state": TAGGED, "first_decision_at": now, "earliest_known_decision_at": now}
    found = backfill(ledger, complete=complete, audit=audit)
    if found["state"] == TAGGED:
        if found["earliest_known_decision_at"] is None:
            found = {**found, "earliest_known_decision_at": now}
        return found
    return {"state": TAGGED, "first_decision_at": None, "earliest_known_decision_at": now}


def classify(*, ledger: dict[str, Any] | None, complete: bool,
             audit: dict[str, Any] | None) -> dict[str, Any]:
    """Read-only display state of one document: `{state: tagged | never_tagged | history_unknown,
    first_decision_at, earliest_known_decision_at}`. `ledger` is None when no ledger file exists.

    - a ledger with `tagging` → its stored state;
    - a ledger without it (not yet migrated) → the same evidence backfill the migration would store;
    - no ledger, attributable audit evidence (timed or not) → tagged (a first time only when tracking
      is complete and no evidence is untimed);
    - no ledger, no evidence, tracking complete for it → known never tagged;
    - anything else → tagging history unknown."""
    audit = audit or NO_EVIDENCE
    if ledger is not None:
        tagging = ledger.get("tagging")
        if not isinstance(tagging, dict):
            tagging = backfill(ledger, complete=complete, audit=audit)
        shown = SHOWN_TAGGED if tagging["state"] == TAGGED else SHOWN_UNKNOWN
        return {"state": shown, "first_decision_at": tagging["first_decision_at"],
                "earliest_known_decision_at": tagging["earliest_known_decision_at"]}
    if _exists(_combined(audit)):
        found = _from_evidence(_combined(audit), complete=complete)
        return {"state": SHOWN_TAGGED, "first_decision_at": found["first_decision_at"],
                "earliest_known_decision_at": found["earliest_known_decision_at"]}
    return {"state": SHOWN_NEVER if complete else SHOWN_UNKNOWN, "first_decision_at": None,
            "earliest_known_decision_at": None}


def discovered_at(reviews_dir: Path, source_id: str) -> Any:
    """The document's `discovered_at` from its manifest (vault layout: `raw/manifests` beside
    `reviews/`), or None when it can't be read."""
    from app.backend import manifests
    try:
        m = manifests.load_manifest(Path(reviews_dir).parent / "raw" / "manifests", source_id)
    except Exception:  # noqa: BLE001 - an unreadable manifest only reduces certainty
        return None
    return (m or {}).get("discovered_at")
