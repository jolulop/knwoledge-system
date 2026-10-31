#!/usr/bin/env python3
"""Which documents still await reconciliation after a provenance change (ADR-0066 part 3 slice 3).

Synthesis recomputes source independence from current provenance on every run, so a confirmation or
a Clear never changes a model fingerprint. What it records is which documents' independence inputs
it has **reconciled**, so the screens and the maintenance report can say what is still waiting
(slice-3 grill Q3). Contradictions no longer take part (ADR-0069 slice 2(a)): targeted contradiction
maintenance runs inside every Details save or clear, and what it couldn't do is reported from the
evidence itself (`contradictions.check_assertions`: lint and the Source screen), so acknowledgements
recorded by old `detect_contradictions` jobs are ignored.

- a run captures each document's independence digest from the provenance it used for its
  eligibility decisions (`capture`), not from provenance read when it finishes;
- when it completes (status `succeeded` or `saved_publication_pending`), it verifies those inputs
  under the write lock and acknowledges only the documents whose inputs are still the ones it used
  (`verify`); the acknowledged digests go in its job metadata (`independence_acknowledged`). A
  failed, partial or skipped run acknowledges nothing;
- a document is **awaiting** a producer when its current digest differs from the one that producer
  last acknowledged. A document with no provenance and no acknowledgement gets no notice; one with
  provenance and no (or pruned) history is not known to be reconciled.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.backend import manifests

PRODUCERS = {"generate_synthesis": "Synthesis"}
SCREEN_NOTICES = {
    "Synthesis": "Synthesis hasn't run since these details changed; run it to reconcile them.",
}
LINT_TEXT = {
    "generate_synthesis": ("Synthesis hasn't run since this document's provenance changed",
                           "run generate_synthesis"),
}
_EMPTY = manifests.independence_digest({})
_COMPLETE = ("succeeded", "saved_publication_pending")


def capture(provenance: dict[str, dict[str, Any]]) -> dict[str, str]:
    """The digests of the provenance a run uses (`{source_id: provenance}`)."""
    return {sid: manifests.independence_digest(prov) for sid, prov in provenance.items()}


def verify(manifests_dir: Path, captured: dict[str, str], status: str) -> dict[str, str]:
    """The digests a completed run may acknowledge: the captured ones still current. The caller
    holds the write lock, so no confirmation can land between this read and the job record."""
    if status not in _COMPLETE:
        return {}
    current = capture({m["source_id"]: manifests.get_provenance(m)
                       for m in manifests.valid_manifests(manifests_dir)[0]})
    return {sid: d for sid, d in captured.items() if current.get(sid) == d}


def _acknowledged(jobs_db: Path, producer: str) -> dict[str, str]:
    """The digest a producer last acknowledged **per document**: history is walked newest-first and
    each source keeps its latest acknowledgement. A newer run that left a document out (its inputs
    changed during the run) never erases that document's earlier acknowledgement, so the change
    stays awaiting (slice-3 review round 1)."""
    if not Path(jobs_db).exists():
        return {}
    conn = sqlite3.connect(jobs_db)
    try:
        rows = conn.execute("SELECT metadata_json FROM jobs WHERE job_type = ? "
                            "ORDER BY created_at DESC, rowid DESC", (producer,)).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    latest: dict[str, str] = {}
    for (raw,) in rows:
        try:
            ack = json.loads(raw or "{}").get("independence_acknowledged")
        except (ValueError, AttributeError):
            continue
        if isinstance(ack, dict):
            for sid, digest in ack.items():
                latest.setdefault(str(sid), str(digest))
    return latest


def awaiting(manifests_dir: Path, jobs_db: Path, *,
             source_id: str | None = None) -> dict[str, list[str]]:
    """`{producer: [source_id…]}` of documents awaiting reconciliation (only producers with any)."""
    current = capture({m["source_id"]: manifests.get_provenance(m)
                       for m in manifests.valid_manifests(manifests_dir)[0]
                       if source_id is None or m["source_id"] == source_id})
    out: dict[str, list[str]] = {}
    for producer in PRODUCERS:
        ack = _acknowledged(jobs_db, producer)
        waiting = []
        for sid, digest in sorted(current.items()):
            prior = ack.get(sid)
            if prior is None:
                if digest != _EMPTY:               # provenance, but never reconciled (or pruned)
                    waiting.append(sid)
            elif prior != digest:                  # changed since, Clear-to-empty included
                waiting.append(sid)
        if waiting:
            out[producer] = waiting
    return out
