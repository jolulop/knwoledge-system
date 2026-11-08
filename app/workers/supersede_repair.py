#!/usr/bin/env python3
"""Supersede recovery for a human operator (ADR-0073 decision 11 as amended by decisions 18–23).

`scripts/repair_supersede.py` is a thin CLI over this module:

- :func:`inspect` captures ONE snapshot of everything a supersede's recovery depends on (every
  ledger copy of the review, its journal, both endpoint Claim pages, the pair's graph rows and every
  audit entry of the review), and derives the facts, the offered command, the wording and the
  `--expected-state` fingerprint from those same bytes and rows. Read-only: no lock, no run guard, no
  revision change, no store created.
- :func:`rollback_edge` is the one governed write. It takes the run guards (non-blocking, fixed
  order) and the write lock, captures a fresh snapshot, compares its fingerprint with
  `--expected-state`, repeats every check, queues publication, then hands over to
  :func:`supersede.initiate_rollback` — the same rollback Apply runs. It never publishes and never
  ends the journal: the next Apply finishes the recovery and closes it once publication is
  published; reopen becomes available after that.

With no journal nothing is written (decision 19): an owned edge then carries only a review id, and
which approval generation wrote it is unknown.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from app.backend import publication_queue, revision, run_guard, write_lock
from app.backend.manifests import iso_now
from app.workers import claim_marker, identity_journal, supersede

FINGERPRINT_VERSION = "supersede-state-v1"     # change with the payload OR the recovery policy
LEDGER_STATES = ("pending", "approved", "rejected")
# Runs whose multi-commit work can change what the fingerprint reads (decision 23), fixed order.
GUARDS = ("apply", "extract_claims", "detect_contradictions", "republish", "reconcile_reviews")

EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2
EXIT_BUSY = run_guard.EXIT_BUSY                # 3
EXIT_QUEUE_CORRUPT = run_guard.EXIT_QUEUE_CORRUPT  # 4
EXIT_STATE_CHANGED = 6
EXIT_REVISION_UNAVAILABLE = 9

INSPECT_COMMAND = "uv run python scripts/repair_supersede.py inspect {rid}"
LAST_RESORTS = ("Last resorts: restore from a backup (scripts/backup.py), or reverse the effect "
                "through a governed review.")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read(path: Path) -> dict[str, Any]:
    """`{state: absent|unreadable|present, raw}` — one read of one file."""
    try:
        return {"state": "present", "raw": Path(path).read_bytes()}
    except FileNotFoundError:
        return {"state": "absent", "raw": None}
    except OSError:
        return {"state": "unreadable", "raw": None}


def _json_obj(raw: bytes | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _text(raw: bytes | None) -> str | None:
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


# --- the snapshot ------------------------------------------------------------------------------


def _endpoints(ledger: dict[str, Any], journal: dict[str, Any]) -> list[str] | None:
    """The claim pair named by every readable source that names one; None when none does or they
    disagree."""
    pairs = set()
    for copy in ledger.values():
        item = copy.get("item")
        subj = (item or {}).get("subject") or {}
        a, b = subj.get("claim_a"), subj.get("claim_b")
        if isinstance(a, str) and isinstance(b, str):
            pairs.add(tuple(sorted((a, b))))
    entry = journal.get("entry")
    if entry is not None and isinstance(entry.get("nodes"), list):
        pairs.add(tuple(sorted(str(n) for n in entry["nodes"])))
    if len(pairs) != 1:
        return None
    pair = list(next(iter(pairs)))
    return pair if len(pair) == 2 and all(supersede._CLAIM.fullmatch(c) for c in pair) else None


def _graph_rows(gconn, pair: list[str] | None) -> dict[str, Any]:
    if gconn is None:
        return {"state": "absent"}
    if pair is None:
        return {"state": "present", "supersedes": [], "contradicts": [], "nodes": {}}
    a, b = pair
    try:
        sup = [dict(r) for r in gconn.execute(
            "SELECT edge_id, src_id, dst_id, status, review_id, asserted_by FROM edges "
            "WHERE edge_type = 'supersedes' AND ((src_id = ? AND dst_id = ?) OR "
            "(src_id = ? AND dst_id = ?)) ORDER BY edge_id", (a, b, b, a))]
        con = [dict(r) for r in gconn.execute(
            "SELECT * FROM edges WHERE edge_type = 'contradicts' AND asserted_by = 'llm' "
            "AND src_id = ? AND dst_id = ? ORDER BY edge_id", (a, b))]
        nodes = {}
        for cid in pair:
            row = gconn.execute("SELECT status FROM nodes WHERE node_id = ?", (cid,)).fetchone()
            nodes[cid] = row["status"] if row is not None else None
    except sqlite3.Error:
        return {"state": "unreadable"}
    return {"state": "present", "supersedes": sup, "contradicts": con, "nodes": nodes}


def capture(root: Path, review_id: str, gconn) -> dict[str, Any]:
    """One snapshot: every file read once, the graph rows read once. Every later decision and the
    fingerprint are derived from it, never from a second read."""
    root = Path(root).resolve()
    reviews_dir = root / "reviews"
    ledger: dict[str, Any] = {}
    for state in LEDGER_STATES:
        got = _read(reviews_dir / state / f"{review_id}.json")
        got["item"] = _json_obj(got["raw"])
        ledger[state] = got
    jgot = _read(reviews_dir / identity_journal.JOURNAL_DIR / f"{review_id}.json")
    entry = None
    if jgot["state"] == "present":
        try:
            entry = identity_journal.parse(jgot["raw"], review_id)
        except identity_journal.JournalUnreadable:
            entry = None
    journal = {"state": jgot["state"], "raw": jgot["raw"], "entry": entry}
    pair = _endpoints(ledger, journal)
    pages = {cid: _read(root / "wiki" / "Claims" / f"{cid}.md") for cid in (pair or [])}
    return {"root": str(root), "review_id": review_id, "pair": pair, "ledger": ledger,
            "journal": journal, "pages": pages, "graph": _graph_rows(gconn, pair),
            "audits": supersede.scan_review_audits(reviews_dir, review_id)}


def fingerprint(snap: dict[str, Any]) -> str:
    """SHA-256 over the canonical payload of decision 22 (byte hashes, not parsed facts; an
    unreadable or unscannable part is recorded as that state, never as absent)."""
    def file_part(got: dict[str, Any]) -> dict[str, Any]:
        return {"state": got["state"],
                "sha256": _sha(got["raw"]) if got["raw"] is not None else None}
    g = snap["graph"]
    payload = {
        "version": FINGERPRINT_VERSION,
        "target": {"root": snap["root"], "review_id": snap["review_id"], "endpoints": snap["pair"]},
        "review": {state: file_part(snap["ledger"][state]) for state in LEDGER_STATES},
        "journal": file_part(snap["journal"]),
        "graph": g["state"],
        "supersedes_edges": [[r["edge_id"], r["src_id"], r["dst_id"], r["status"], r["review_id"],
                              r["asserted_by"]] for r in g.get("supersedes", [])],
        "prerequisites": {"nodes": g.get("nodes", {}),
                          "contradicts": [[r["edge_id"], r["status"]]
                                          for r in g.get("contradicts", [])]},
        "pages": {cid: file_part(got) for cid, got in sorted(snap["pages"].items())},
        "audits": (None if snap["audits"] is None else
                   [[name, _sha(raw) if raw is not None else "unreadable"]
                    for name, raw in snap["audits"]]),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha(blob)


# --- the evaluation (from the snapshot only) ---------------------------------------------------


def _queued(root: Path, cid: str) -> bool | None:
    """Whether publication already holds `cid` (True/False), None when the queue can't be read."""
    try:
        entries = publication_queue.load(root)
    except publication_queue.QueueCorrupt:
        return None
    return any(e.get("full") or cid in (e.get("claims") or []) for e in entries)


def _publication_route(root: Path, cid: str) -> str:
    queued = _queued(Path(root), cid)
    if queued is None:
        return ("The publication queue can't be read: run `uv run python scripts/republish.py "
                "--repair`, then Apply.")
    if queued:
        return ("This claim is already queued for publication: use Retry publishing (or `uv run "
                "python scripts/republish.py`), then Apply.")
    return "Run `uv run python scripts/republish.py --all`, then Apply."


def evaluate(snap: dict[str, Any]) -> dict[str, Any]:
    """The report: `{review_id, fingerprint, facts, findings, offer, command}`. `offer` is True only
    when every strict precondition holds (decisions 19 and 21); `findings` are plain sentences that
    report what was found without inferring a cause."""
    rid = snap["review_id"]
    report: dict[str, Any] = {"review_id": rid, "fingerprint": fingerprint(snap), "facts": {},
                              "findings": [], "offer": False, "command": None, "note": None}
    facts, say = report["facts"], report["findings"].append

    def refuse(sentence: str) -> dict[str, Any]:
        say(sentence)
        say("No safe repair determined.")
        return report

    # --- the review ledger ---
    present = [s for s in LEDGER_STATES if snap["ledger"][s]["state"] != "absent"]
    facts["ledger"] = {s: snap["ledger"][s]["state"] for s in LEDGER_STATES}
    if not present:
        return refuse(f"No review file for {rid} was found in pending/, approved/ or rejected/.")
    if len(present) > 1:
        return refuse(f"The review has copies in {', '.join(present)} (an interrupted move can "
                      "leave several); which one is authoritative is unknown.")
    copy = snap["ledger"][present[0]]
    item = copy["item"]
    if copy["state"] == "unreadable" or item is None:
        return refuse(f"The review file in {present[0]}/ can't be read.")
    facts["review_state"] = present[0]
    bind = supersede.binding(item)
    if (present[0] != "approved" or item.get("status") != "approved"
            or item.get("type") != "resolve_contradiction" or bind is None):
        return refuse("The review is not an approved supersede, so there is nothing to recover.")
    if not supersede._canonical(bind):
        return refuse("The review's ids are not canonical.")
    if not supersede._approval_ok(bind):
        return refuse("The approval has no approval id (it predates them), so a rollback could "
                      "not prevent the same approval running again; reopen and approve it again "
                      "once its effects are verified.")
    facts.update(winner=bind["winner"], loser=bind["loser"], approval_id=bind["approval_id"])

    # --- the graph ---
    g = snap["graph"]
    facts["graph"] = g["state"]
    if g["state"] != "present":
        return refuse("The graph database " + ("doesn't exist." if g["state"] == "absent"
                                              else "can't be read."))
    owned = [r for r in g["supersedes"] if r["review_id"] == rid]
    facts["owned_edges"] = [{k: r[k] for k in ("edge_id", "src_id", "dst_id", "status")}
                            for r in owned]
    facts["node_status"] = dict(g["nodes"])

    # --- the pages ---
    pages: dict[str, dict[str, Any]] = {}
    for cid in (bind["winner"], bind["loser"]):
        got = snap["pages"][cid]
        text = _text(got["raw"])
        if got["state"] == "absent":
            pages[cid] = supersede.page_from_text(None, cid)
        elif text is None:                         # unreadable file, or not UTF-8
            pages[cid] = {"state": "unreadable", "status": None, "marker_state": None,
                          "marker": None}
        else:
            pages[cid] = supersede.page_from_text(text, cid)
    loser_page = pages[bind["loser"]]
    facts["loser_page"] = {"state": loser_page["state"], "status": loser_page["status"],
                           "marker": loser_page["marker_state"]}
    loser_node = g["nodes"].get(bind["loser"])
    if (loser_page["state"] == "ok" and loser_page["status"] in ("active", "hidden",
                                                                 "deprecated_candidate")
            and loser_node is not None and loser_node != loser_page["status"]):
        say(f"The loser's graph status ({loser_node}) differs from its page "
            f"({loser_page['status']}); publication copies the page's status. "
            + _publication_route(Path(snap["root"]), bind["loser"]))

    # --- the journal ---
    j = snap["journal"]
    facts["journal"] = j["state"] if j["entry"] is not None or j["state"] != "present" \
        else "unreadable"
    if j["state"] == "absent":
        active_owned = [r for r in owned if r["status"] == "active"]
        if active_owned:
            say(f"Review {rid} owns an active supersede link "
                f"({active_owned[0]['src_id']} → {active_owned[0]['dst_id']}), but no unfinished "
                "operation is recorded. Which approval generation wrote the link is unknown, so "
                "the state can't be verified.")
        else:
            say("No unfinished operation is recorded for this review; Apply evaluates it as "
                "usual.")
        say("No safe repair determined: with no journal, no repair command applies. "
            + LAST_RESORTS)
        return report
    if j["entry"] is None:
        return refuse("The saved progress of this supersede can't be read. " + LAST_RESORTS)
    journal = j["entry"]
    facts["stage"] = journal.get("stage")
    if not supersede._journal_binding_ok(journal, bind):
        return refuse("The saved progress of this supersede doesn't match the current approval "
                      "(its winner, loser or approval id differ, or its stage is unknown).")
    if journal["stage"] == supersede.ROLLING_BACK:
        say("A rollback of this supersede is already recorded. Apply finishes it and closes the "
            "operation once publication completes; run Apply. No new write is offered.")
        return report
    if journal["stage"] == supersede.LOSER_DEPRECATED:
        say("The supersede is recorded as completed; Apply finishes its record and closes the "
            "operation. Run Apply. A rollback is not available.")
        return report

    # --- strict preconditions (decision 19) ---
    if loser_page["state"] != "ok" or claim_marker.is_unreadable(loser_page["marker_state"]):
        return refuse("The loser's Claim page can't be read (or names another claim), so the "
                      "completion evidence can't be checked.")
    if not (loser_page["status"] == "active" and loser_node == "active"):
        return refuse(f"The loser is not active in both its page ({loser_page['status']}) and "
                      f"the graph ({loser_node}); a rollback needs an active, consistent loser.")
    active_owned = [r for r in owned if r["status"] == "active"]
    if not active_owned:
        say("This review's supersede link isn't written yet; Apply resumes or rolls back the "
            "operation by itself. No write is offered.")
        return report
    if (len(active_owned) != 1 or active_owned[0]["src_id"] != bind["winner"]
            or active_owned[0]["dst_id"] != bind["loser"]):
        return refuse(f"This review owns {len(active_owned)} active supersede links for the pair "
                      "(or one in the wrong direction); exactly one, winner → loser, is required.")
    refusal = supersede.completion_evidence_from(bind, loser_page, journal, snap["audits"])
    if refusal is not None:
        reason, kind = refusal
        if reason == "completion_evidence_found":
            return refuse(f"Completion evidence exists ({kind}), so the supersede is not "
                          "withdrawn; reversing a completed effect needs a governed reversal.")
        return refuse(f"Evidence needed to check completion can't be read ({kind}), so nothing "
                      "can be withdrawn. Repair or restore it, then inspect again.")

    # --- offered (decision 21) ---
    marker = loser_page["marker"]
    if marker is not None and marker.get("review_id") != rid:
        report["note"] = (f"marker_conflict: the loser's page records review "
                          f"{marker['review_id']}'s supersede, so every Apply blocks at the page "
                          "write and never rolls this back by itself.")
    else:
        pages_snap = {cid: pages[cid] for cid in (bind["winner"], bind["loser"])}
        problem = supersede.prerequisite_problem_from(bind, g["contradicts"], pages_snap,
                                                      g["nodes"])
        if problem is not None:
            report["note"] = (f"The supersede's prerequisites no longer hold ({problem[0]}); the "
                              "next Apply rolls it back by itself. rollback-edge does the same "
                              "now.")
        else:
            report["note"] = ("Apply can attempt to complete this supersede. Rolling back "
                              "withdraws this approval; another attempt requires reopening and "
                              "deciding again.")
    say(report["note"])
    report["offer"] = True
    root_arg = "" if Path(snap["root"]) == Path.cwd().resolve() else f" --root {snap['root']}"
    report["command"] = (f"uv run python scripts/repair_supersede.py rollback-edge {rid} "
                         f"--expected-state {report['fingerprint']} --reason \"<why>\"{root_arg}")
    say("It withdraws only this review's supersede link and consumes this approval; Apply then "
        "finishes the recovery and closes the operation, and reopen is available after that.")
    return report


# --- the commands ------------------------------------------------------------------------------


def _graph_ro(path: Path):
    """A read-only graph connection, or None when the database doesn't exist (never created)."""
    if not Path(path).exists():
        return None
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def inspect(root: Path, review_id: str) -> dict[str, Any]:
    """The read-only diagnostic report."""
    root = Path(root).resolve()
    gconn = _graph_ro(root / "db" / "graph.sqlite")
    try:
        return evaluate(capture(root, review_id, gconn))
    finally:
        if gconn is not None:
            gconn.close()


def _repair_id(review_id: str, approval_id: str, expected_state: str) -> str:
    return "rpr_" + _sha(f"{review_id}\0{approval_id}\0{expected_state}".encode())[:16]


def rollback_edge(root: Path, review_id: str, *, expected_state: str, reason: str,
                  now: str | None = None) -> dict[str, Any]:
    """`{exit, message, wrote, intent_recorded}` — the governed write of decision 18."""
    root = Path(root).resolve()
    now = now or iso_now()
    out: dict[str, Any] = {"exit": EXIT_REFUSED, "message": "", "wrote": False,
                           "intent_recorded": False}
    if not reason.strip():
        out.update(exit=EXIT_USAGE, message="--reason must say why.")
        return out
    try:
        with contextlib.ExitStack() as guards:
            for name in GUARDS:                       # fixed order; unwound on any failure
                guards.enter_context(run_guard.guard(root / "db", name))
            return _locked_rollback(root, review_id, expected_state, reason, now, out)
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


def _locked_rollback(root: Path, rid: str, expected_state: str, reason: str, now: str,
                     out: dict[str, Any]) -> dict[str, Any]:
    from app.backend import graph
    reviews_dir = root / "reviews"
    graph_path = root / "db" / "graph.sqlite"
    with write_lock.hold(reviews_dir):
        if not graph_path.exists():
            out["message"] = "Refused: the graph database doesn't exist. Nothing was changed."
            return out
        gconn = graph.connect(graph_path)
        try:
            snap = capture(root, rid, gconn)
            report = evaluate(snap)
            if report["fingerprint"] != expected_state:
                out.update(exit=EXIT_STATE_CHANGED,
                           message="State changed since it was inspected — inspect again. "
                                   "Nothing was changed.")
                return out
            if not report["offer"]:
                out["message"] = ("Refused before any write: " + " ".join(report["findings"])
                                  + " Nothing was changed.")
                return out
            copy = snap["ledger"]["approved"]
            item = copy["item"]
            bind = supersede.binding(item)
            repair = {"repair_id": _repair_id(rid, bind["approval_id"], expected_state),
                      "actor": supersede.REPAIR_ACTOR, "reason": reason.strip(),
                      "expected_state": expected_state, "initiated_at": now}
            # Decision 18 step 2: the derived views are queued BEFORE the first mutation.
            publication_queue.record(root, origin="repair_supersede",
                                     claims=[bind["winner"], bind["loser"]], now=now)
            try:
                supersede.initiate_rollback(gconn, reviews_dir, item,
                                            claims_dir=root / "wiki" / "Claims", repair=repair,
                                            now=now)
            except supersede.Blocked as blk:
                return _after_failure(reviews_dir, rid, repair, out, f"refused ({blk.reason})")
            except Exception as exc:  # noqa: BLE001 - reported with what was recorded
                return _after_failure(reviews_dir, rid, repair, out,
                                      f"failed ({type(exc).__name__}: {exc})")
        finally:
            gconn.close()
    out.update(exit=EXIT_OK, wrote=True, intent_recorded=True, message=(
        f"Rollback recorded for {rid}: its supersede link was withdrawn and this approval "
        "consumed. Publication is pending; the next Apply publishes, finishes the recovery and "
        "closes the operation. Reopen the review after that if another attempt is wanted."))
    return out


def _after_failure(reviews_dir: Path, rid: str, repair: dict[str, Any], out: dict[str, Any],
                   what: str) -> dict[str, Any]:
    """Report honestly how far a refused or failed write got: never "nothing changed" once the
    intent is durable."""
    try:
        entry = identity_journal.read(reviews_dir, rid)
    except identity_journal.JournalUnreadable:
        entry = None
        recorded = None
    else:
        recorded = (entry is not None and entry.get("stage") == supersede.ROLLING_BACK
                    and (entry.get("repair") or {}).get("repair_id") == repair["repair_id"])
    out["intent_recorded"] = bool(recorded)
    out["wrote"] = bool(recorded)
    if recorded:
        out["message"] = (f"The rollback {what} after its intent was durably recorded. Changes "
                          "may have been made: run Apply — it finishes the recorded rollback from "
                          "the saved intent and closes the operation after publication. Then "
                          "inspect again.")
    elif recorded is None:
        out["message"] = (f"The rollback {what}, and the saved progress can't be read, so it is "
                          "unknown whether the intent was recorded. Inspect again before "
                          "anything else.")
    else:
        out["message"] = (f"The rollback {what} before its intent was recorded: no authority "
                          "changed (a harmless publication refresh may be queued). Inspect "
                          "again.")
    return out
