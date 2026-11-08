#!/usr/bin/env python3
"""Synthesis versions and the decisions bound to them (ADR-0073 decisions 2–4, 6).

Three identities are kept apart:

- the **input fingerprint** (`synthesis._fingerprint`, 16 hex): the freshness and re-nag key; the
  `propose_synthesis` review id stays keyed on it;
- the **version digest** (this module, 64 hex): SHA-256 of canonical JSON over the input
  fingerprint and every artifact field the page renders (`title`, `summary`, `synthesis`,
  `confidence`). Regenerating under unchanged inputs can produce different prose under the same
  review id; the digest tells the versions apart;
- the **bound decision**: the digest of the version the human was shown, recorded on the decision
  (`decided_version_digest`).

The artifact is the authority. Its required fields are validated with no defaults and the digest is
always RECOMPUTED from them; a stored `version_digest` must agree (a mismatch is malformed). A valid
artifact WITHOUT the `version_digest` field (absent; an explicit null is malformed) is a
**legacy-format** artifact: displayable and decidable as is, with no migration and no write. Pure
reads; never raises on bad data.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from app.backend.paths import safe_child

FIELD = "version_digest"                    # on the artifact and the pending proposal
DECIDED = "decided_version_digest"          # on a recorded decision (and its audit entry)

_HEX16 = re.compile(r"[0-9a-f]{16}")
_HEX64 = re.compile(r"[0-9a-f]{64}")

# Reason codes (shared by Apply outcomes, decision refusals and the validator).
MISSING = "synthesis_artifact_missing"
UNREADABLE = "synthesis_artifact_unreadable"
INVALID = "synthesis_artifact_invalid"
DIGEST_MISMATCH = "synthesis_digest_mismatch"
INPUTS_CHANGED = "synthesis_inputs_changed"          # the artifact is for other inputs than the review
VERSION_CHANGED = "synthesis_version_changed"        # same inputs, other text than the one shown
GENERATION_INTERRUPTED = "synthesis_generation_interrupted"  # proposal and artifact disagree
VERSION_REPLACED = "synthesis_version_replaced"      # Apply: the decision is on a replaced version
DECISION_UNBOUND = "decision_unbound"                # Apply: a legacy decision with no digest


def is_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX64.fullmatch(value))


def _confidence_ok(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 <= value <= 1                  # exact; never converts a huge int to float
    return math.isfinite(value) and 0.0 <= value <= 1.0


def invalid_field(artifact: Any) -> str | None:
    """The first required field that is missing or malformed, or None when the artifact is valid
    (ADR-0073 decision 3). No defaults: a blank title is invalid, not "the topic id"."""
    if not isinstance(artifact, dict):
        return "artifact"
    fp = artifact.get("input_fingerprint")
    if not isinstance(fp, str) or not _HEX16.fullmatch(fp):
        return "input_fingerprint"
    for key in ("title", "summary", "synthesis"):
        v = artifact.get(key)
        if not isinstance(v, str) or not v.strip():
            return key
    if not _confidence_ok(artifact.get("confidence")):
        return "confidence"
    # Legacy format is the field's ABSENCE; present, it must be a digest (an explicit null is not).
    if FIELD in artifact and not is_digest(artifact[FIELD]):
        return FIELD
    return None


def compute_digest(artifact: dict[str, Any]) -> str:
    """The version digest of a VALID artifact: sha256 of deterministic canonical JSON (sorted keys,
    fixed separators). `confidence` is hashed as a float, so `1` and `1.0` are one version."""
    payload = {"input_fingerprint": artifact["input_fingerprint"], "title": artifact["title"],
               "summary": artifact["summary"], "synthesis": artifact["synthesis"],
               "confidence": float(artifact["confidence"])}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def check(artifact: Any) -> dict[str, Any]:
    """`{"ok": True, "digest", "artifact", "legacy_format"}` for a valid artifact whose stored digest
    (if any) agrees; else `{"ok": False, "reason", "field"?}`."""
    bad = invalid_field(artifact)
    if bad is not None:
        return {"ok": False, "reason": INVALID, "field": bad}
    digest = compute_digest(artifact)
    legacy = FIELD not in artifact
    if not legacy and artifact[FIELD] != digest:
        return {"ok": False, "reason": DIGEST_MISMATCH, "field": FIELD}
    return {"ok": True, "digest": digest, "artifact": artifact, "legacy_format": legacy}


def read(enrichment_dir: Path, topic_node_id: Any) -> dict[str, Any]:
    """Read and check `<enrichment_dir>/<topic>.synthesis.json` (the topic id is untrusted)."""
    if not isinstance(topic_node_id, str) or not topic_node_id:
        return {"ok": False, "reason": MISSING}
    path = safe_child(Path(enrichment_dir), f"{topic_node_id}.synthesis.json")
    if path is None:
        return {"ok": False, "reason": MISSING}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"ok": False, "reason": MISSING}
    except (OSError, UnicodeDecodeError):
        return {"ok": False, "reason": UNREADABLE}
    try:
        artifact = json.loads(text)
    except json.JSONDecodeError:
        return {"ok": False, "reason": UNREADABLE}
    return check(artifact)


def decision_problem(version: dict[str, Any], item: dict[str, Any],
                     shown_digest: Any) -> str | None:
    """Why a decision on `item` for the version the human was shown must NOT be recorded, or None
    (ADR-0073 decision 4). `version` is :func:`read` of the item's topic, taken under the write lock.
    The artifact is the authority; the pending proposal's digest is a consistency check."""
    if not version.get("ok"):
        return str(version.get("reason"))
    subject = item.get("subject") or {}
    if version["artifact"].get("input_fingerprint") != subject.get("fingerprint"):
        return INPUTS_CHANGED
    proposed = (item.get("proposal") or {}).get(FIELD)
    if proposed is not None and proposed != version["digest"]:
        return GENERATION_INTERRUPTED
    if shown_digest != version["digest"]:
        return VERSION_CHANGED
    return None


def apply_problem(version: dict[str, Any], item: dict[str, Any]) -> str | None:
    """Why Apply must not execute decision `item`, or None (ADR-0073 decision 6): the artifact is
    valid, it is for the decision's inputs, and the decision's recorded digest is its digest."""
    if not version.get("ok"):
        return str(version.get("reason"))
    subject = item.get("subject") or {}
    if version["artifact"].get("input_fingerprint") != subject.get("fingerprint"):
        return VERSION_REPLACED
    recorded = item.get(DECIDED)
    if recorded is None:
        return DECISION_UNBOUND
    if recorded != version["digest"]:
        return VERSION_REPLACED
    return None


def describe(version: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """What the review page and the JSON API show about the version (read-only). The text fields are
    present only for a valid artifact; the caller escapes them."""
    subject = item.get("subject") or {}
    out: dict[str, Any] = {"ok": bool(version.get("ok")),
                           "decided_version_digest": item.get(DECIDED)}
    if not version.get("ok"):
        out["problem"] = version.get("reason")
        if version.get("field"):
            out["field"] = version["field"]
        return out
    a = version["artifact"]
    proposed = (item.get("proposal") or {}).get(FIELD)
    out.update({
        "version_digest": version["digest"], "legacy_format": version["legacy_format"],
        "title": a["title"], "summary": a["summary"], "confidence": a["confidence"],
        "synthesis": a["synthesis"], "input_fingerprint": a["input_fingerprint"],
        "for_review_inputs": a["input_fingerprint"] == subject.get("fingerprint"),
        "proposal_agrees": None if proposed is None else proposed == version["digest"],
    })
    return out
