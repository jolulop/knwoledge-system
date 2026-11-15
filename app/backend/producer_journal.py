#!/usr/bin/env python3
"""The producer commit journal, `db/producer_journal.json` (ADR-0068 slice 3).

A claims, items or synthesis commit is several writes: the graph, its pages, a proposal, the
artifact last. The artifact is what makes a subject *fresh*, but writing it last is not enough: a
forced re-extraction keeps the previous, still-matching artifact until the end, and superseding a
document's relationships removes the very links recovery would follow to its old claims and tags.

So, before its first write and under the write lock, the producer records the subject with the ids
the commit may affect (the document's current claims or tags; for a synthesis topic, the intended
evidence fingerprint). After its last write — graph, pages, proposal, artifact, with the remaining
publication already queued — it removes the record. A record left behind means the commit stopped:

- the subject is never fresh until a commit of it finishes;
- a resumed commit unions the saved ids with the current ones (:func:`begin`), so the original
  scope survives repeated failures and is never replaced by the smaller set left after one;
- run summaries and `scripts/validate_producer_journal.py` report it.

Publication retry never reads or clears it: retiring a claim or a tag that lost its evidence is
producer work. Same durability as the publication queue: schema-versioned, replaced atomically and
durably, backed up with `db/`. **An unreadable journal is an error, never an empty one**: :func:`load`
raises :class:`JournalCorrupt`, the three producers refuse to run, and
`scripts/repair_producer_journal.py` keeps a copy of the damaged file and rebuilds the records from
authority (:func:`rebuild`).
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.backend import authority_read, manifests
from app.backend import durable_write

JOURNAL_NAME = "producer_journal.json"
SCHEMA_VERSION = 1

# producer -> the kind of subject it commits (a document, or a synthesis topic tag)
PRODUCERS = {"extract_claims": "source", "extract_items": "source", "generate_synthesis": "item"}

_ITEM_ID = re.compile(r"itm_[0-9a-f]{16}")
_CLAIM_ID = re.compile(r"clm_[0-9a-f]{16}")
_FINGERPRINT = re.compile(r"[0-9a-f]{8,128}")
_ATTEMPT = re.compile(r"[0-9a-f]{32}")


class JournalCorrupt(RuntimeError):
    """The journal file exists but cannot be read as a valid journal. `reason` is categorical."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"producer journal unreadable ({reason}): repair it with "
                         "`uv run python scripts/repair_producer_journal.py`")
        self.reason = reason


def journal_path(root: Path) -> Path:
    return Path(root) / "db" / JOURNAL_NAME


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _subject_ok(producer: str, subject: Any) -> bool:
    if PRODUCERS.get(producer) == "source":
        return manifests.is_source_id(subject)
    return isinstance(subject, str) and bool(_ITEM_ID.fullmatch(subject))


def _record_problem(r: Any) -> str | None:
    if not isinstance(r, dict):
        return "record_not_object"
    if r.get("producer") not in PRODUCERS:
        return "bad_producer"
    if not _subject_ok(r["producer"], r.get("subject")):
        return "bad_subject"
    for key, pattern in (("claims", _CLAIM_ID), ("items", _ITEM_ID)):
        ids = r.get(key)
        if not isinstance(ids, list) or not all(isinstance(i, str) and pattern.fullmatch(i)
                                                for i in ids):
            return f"bad_{key}"
    fp = r.get("fingerprint")
    if fp is not None and not (isinstance(fp, str) and _FINGERPRINT.fullmatch(fp)):
        return "bad_fingerprint"
    attempt = r.get("attempt", "missing")
    if attempt is not None and not (isinstance(attempt, str) and _ATTEMPT.fullmatch(attempt)):
        return "bad_attempt"
    if not isinstance(r.get("started_at"), str):
        return "bad_started_at"
    if not isinstance(r.get("attempts"), int) or r["attempts"] < 1:
        return "bad_attempts"
    return None


def load(root: Path) -> list[dict[str, Any]]:
    """Every record, oldest first. No file means no unfinished commit; a file that is unreadable,
    not this schema, holds an invalid record or the same subject twice raises :class:`JournalCorrupt`.
    ADR-0074 slice 3c: only a verified absence is "no file" — a dangling link, a FIFO, a directory or
    a file that can't be opened is ``unreadable:<category>`` (non-blocking, links never followed)."""
    read = authority_read.read_file(journal_path(root))
    if read.state == authority_read.ABSENT:
        return []
    if read.state != authority_read.OK:
        raise JournalCorrupt(f"unreadable:{read.error}")
    try:
        data = json.loads(read.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise JournalCorrupt("unparseable") from None
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise JournalCorrupt("schema_version")
    records = data.get("records")
    if not isinstance(records, list):
        raise JournalCorrupt("records_not_list")
    seen: set[tuple[str, str]] = set()
    for r in records:
        problem = _record_problem(r)
        if problem:
            raise JournalCorrupt(problem)
        if (r["producer"], r["subject"]) in seen:
            raise JournalCorrupt("duplicate_subject")
        seen.add((r["producer"], r["subject"]))
    return [dict(r) for r in records]


def save(root: Path, records: list[dict[str, Any]]) -> None:
    """Replace the journal atomically and durably. The caller holds the write lock."""
    durable_write.write_bytes(journal_path(root), (json.dumps(
        {"schema_version": SCHEMA_VERSION, "records": records},
        ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _require_lock() -> None:
    from app.backend import write_lock
    if not write_lock.held():
        raise RuntimeError("the producer journal is written under the write lock")


def pending(root: Path, producer: str) -> dict[str, dict[str, Any]]:
    """The producer's unfinished commits, by subject (raises :class:`JournalCorrupt`)."""
    return {r["subject"]: r for r in load(root) if r["producer"] == producer}


def begin(root: Path, producer: str, subject: str, *, claims: Any = (), items: Any = (),
          fingerprint: str | None = None, now: str | None = None) -> dict[str, Any]:
    """Record a commit before its first write and return the record. A subject already recorded
    (an earlier attempt stopped) keeps its ids, unioned with these; the fingerprint is this
    attempt's intended one. Every call gets a new `attempt` id, which a producer can stamp on its
    last write to show that this attempt — not an earlier one — finished."""
    _require_lock()
    claims = {c for c in claims if isinstance(c, str) and _CLAIM_ID.fullmatch(c)}
    items = {i for i in items if isinstance(i, str) and _ITEM_ID.fullmatch(i)}
    records = load(root)
    for r in records:
        if r["producer"] == producer and r["subject"] == subject:
            r.update(claims=sorted(set(r["claims"]) | claims), items=sorted(set(r["items"]) | items),
                     fingerprint=fingerprint if fingerprint is not None else r["fingerprint"],
                     attempt=uuid.uuid4().hex, attempts=r["attempts"] + 1)
            break
    else:
        r = {"producer": producer, "subject": subject, "claims": sorted(claims),
             "items": sorted(items), "fingerprint": fingerprint, "attempt": uuid.uuid4().hex,
             "started_at": now or _iso_now(), "attempts": 1}
        records.append(r)
    problem = _record_problem(r)
    if problem:
        raise ValueError(f"invalid producer journal record: {problem}")
    save(root, records)
    return dict(r)


def finish(root: Path, producer: str, subject: str) -> None:
    """Remove the record after the commit's last write (nothing to do if there is none)."""
    _require_lock()
    records = load(root)
    kept = [r for r in records if not (r["producer"] == producer and r["subject"] == subject)]
    if len(kept) != len(records):
        save(root, kept)


def describe(records: list[dict[str, Any]]) -> dict[str, list[str]]:
    """The unfinished subjects by producer (for summaries and the validator)."""
    out: dict[str, list[str]] = {}
    for r in records:
        out.setdefault(r["producer"], []).append(r["subject"])
    return {p: sorted(s) for p, s in sorted(out.items())}


def status(root: Path) -> dict[str, Any]:
    """The journal's state for a screen or validator, never raising."""
    try:
        records = load(root)
    except JournalCorrupt as exc:
        return {"state": "corrupt", "reason": exc.reason}
    if not records:
        return {"state": "empty"}
    return {"state": "pending", "unfinished": describe(records)}


def preserve_damaged(root: Path, now: str | None = None) -> Path | None:
    """Copy the damaged journal's bytes beside it, flushed, and return the copy's path. The damaged
    file stays in place until :func:`rebuild` replaces it, so a crash in between leaves the
    producers refusing, never treating the journal as empty. An entry that can't be read (a link,
    a FIFO, a permission error) is kept as a hard link to itself (ADR-0074 slice 3c)."""
    path = journal_path(root)
    read = authority_read.read_file(path)
    if read.state == authority_read.ABSENT:
        return None
    stamp = (now or _iso_now()).replace(":", "").replace("-", "")
    target = path.with_name(f"producer_journal.damaged-{stamp}-{uuid.uuid4().hex[:6]}.json")
    if read.state == authority_read.OK:
        durable_write.write_bytes(target, read.data, create_only=True)
    else:
        durable_write.preserve_entry(path, target)
    return target


def rebuild(root: Path, *, now: str | None = None) -> list[dict[str, Any]]:
    """Replace the journal with a record for every subject that may hold an unfinished commit, and
    return them. The lost records can't be told apart, and an interrupted *first* commit has no
    artifact yet, so the inventory is conservative, from every place a commit leaves a trace:

    - claims and items: every extracted document (manifest), every document with an artifact, and
      every document the graph links claims or tags to (active or superseded), each with its current
      claims or tags plus those its superseded relationships left without evidence
      (:func:`app.backend.graph.orphans`);
    - synthesis: every active tag (any could be an eligible topic), every topic with an artifact, a
      synthesis page or a synthesis node's topic link. Fingerprint and attempt are unknown, so the
      next run verifies each against the whole expected state, and clears topics that never reached
      the graph.

    The next runs redo or verify those subjects, from the response cache where the input is
    unchanged."""
    from app.backend import graph
    _require_lock()
    root = Path(root)
    enrichment = root / "normalized" / "enrichment"
    graph_db = root / "db" / "graph.sqlite"
    gconn = graph.connect(graph_db) if graph_db.exists() else None
    stamp = now or _iso_now()

    def _new(producer: str, subject: str, *, claims: Any = (), items: Any = ()) -> dict[str, Any]:
        return {"producer": producer, "subject": subject, "claims": sorted(claims),
                "items": sorted(items), "fingerprint": None, "attempt": None, "started_at": stamp,
                "attempts": 1}

    def _glob(suffix: str) -> set[str]:
        return {p.name[:-len(suffix)] for p in enrichment.glob(f"*{suffix}")} if enrichment.exists() \
            else set()

    records: list[dict[str, Any]] = []
    try:
        inventory = manifests.scan_manifests(root / "raw" / "manifests")
        # ADR-0074 decision 2: a document whose manifest can't be read may be extracted — it is
        # kept for redo (its producer skips it until the manifest is recovered), never dropped;
        # likewise one whose id identity validation quarantined (ambiguous authority).
        extracted = {m["source_id"] for m in inventory.valid
                     if m.get("ingestion_status") in ("extracted", "partial")}
        extracted |= set(inventory.damaged_ids) | set(inventory.quarantined_ids)
        for producer, suffix, edge in (("extract_claims", ".claims.json", "derived_from"),
                                       ("extract_items", ".items.json", "mentions")):
            docs = extracted | _glob(suffix)
            if gconn is not None:
                src = "dst_id" if edge == "derived_from" else "src_id"
                docs |= {r[0] for r in gconn.execute(
                    f"SELECT DISTINCT {src} FROM edges WHERE edge_type = ?", (edge,))}
            for sid in sorted(d for d in docs if manifests.is_source_id(d)):
                ids: set[str] = set()
                if gconn is not None:
                    current = (graph.claims_for_source(gconn, sid) if edge == "derived_from"
                               else graph.item_ids_for_source(gconn, sid))
                    ids = set(current) | set(graph.orphans(gconn, edge_type=edge, source_id=sid))
                records.append(_new(producer, sid, **({"claims": ids} if edge == "derived_from"
                                                      else {"items": ids})))
        topics = _glob(".synthesis.json")
        pages = root / "wiki" / "Synthesis"
        for page in sorted(pages.glob("*.md")) if pages.exists() else []:
            read = authority_read.read_file(page)         # non-blocking (slice 3c); a damaged page's
            text = (read.data or b"").decode("utf-8", errors="replace")   # topic is still covered
            m = re.search(r"^topic_node:\s*\"?(itm_[0-9a-f]{16})", text, re.M)  # by the graph scan
            if m:
                topics.add(m.group(1))
        if gconn is not None:
            topics |= set(graph.active_node_ids_of_type(gconn, "item"))
            topics |= {r[0] for r in gconn.execute(
                "SELECT DISTINCT e.dst_id FROM edges e JOIN nodes n ON n.node_id = e.src_id "
                "WHERE n.node_type = 'synthesis' AND e.edge_type = 'related_to'")}
        records += [_new("generate_synthesis", tid) for tid in sorted(topics)
                    if _ITEM_ID.fullmatch(tid)]
    finally:
        if gconn is not None:
            gconn.close()
    save(root, records)
    return records
