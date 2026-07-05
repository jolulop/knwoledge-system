#!/usr/bin/env python3
"""The per-source enrichment artifact: the record of LLM output the renderer composes.

Phase 3.5a enrichment writes its validated summary + tags to
`normalized/enrichment/<source_id>.json` — it never edits `wiki/Sources/<id>.md` directly.
The deterministic Source-page renderer (wiki_render) composes a *fresh* artifact into the
page (ADR-0025). Freshness is an `input_fingerprint` over the normalized Markdown, the
prompt/template version, the schema version, and the resolved `model_ref` (ADR-0027): a
re-extraction that changes the normalized text makes the artifact stale, so the renderer
falls back to the deterministic stub until enrichment re-runs — never showing a summary for
text that has changed.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "enrich-summary-tags-v1"
PROMPT_VERSION = "enrich-summary-tags-prompt-v1"
# Phase 3.5b claim-extraction pass (separate artifact + versions, tier-2).
CLAIM_SCHEMA_VERSION = "enrich-claims-v1"
CLAIM_PROMPT_VERSION = "enrich-claims-prompt-v1"
# Phase 3.5b concept/entity extraction pass. v2 = the ADR-0055 tier-2 extraction contract
# (concept elicitation band + entity-noise boundary); the bump makes every v1 artifact stale,
# so the next extract_concepts run re-extracts opt-in — no --force machinery.
CONCEPT_SCHEMA_VERSION = "enrich-concepts-v1"
CONCEPT_PROMPT_VERSION = "enrich-concepts-prompt-v2"
# Phase 3.5c contradiction-detection pass (tier-3; per claim pair, response-cache replayed).
CONTRADICTION_SCHEMA_VERSION = "enrich-contradiction-v1"
CONTRADICTION_PROMPT_VERSION = "enrich-contradiction-prompt-v1"
# Phase 3.5c cross-source synthesis pass (tier-3; per active concept/entity).
SYNTHESIS_SCHEMA_VERSION = "enrich-synthesis-v1"
SYNTHESIS_PROMPT_VERSION = "enrich-synthesis-prompt-v1"


def synthesis_artifact_path(enrichment_dir: Path, node_id: str) -> Path:
    return Path(enrichment_dir) / f"{node_id}.synthesis.json"


def _fingerprint(
    normalized_markdown: str, model_ref: str, schema_version: str, prompt_version: str
) -> str:
    """Hash every input that should force re-enrichment (ADR-0027)."""
    h = hashlib.sha256()
    for part in (
        schema_version.encode("utf-8"),
        prompt_version.encode("utf-8"),
        model_ref.encode("utf-8"),
        normalized_markdown.encode("utf-8"),
    ):
        h.update(part)
        h.update(b"\0")
    return h.hexdigest()[:16]


def artifact_path(enrichment_dir: Path, source_id: str) -> Path:
    return Path(enrichment_dir) / f"{source_id}.json"


def artifact_fingerprint(normalized_markdown: str, model_ref: str) -> str:
    return _fingerprint(normalized_markdown, model_ref, SCHEMA_VERSION, PROMPT_VERSION)


def claims_artifact_path(enrichment_dir: Path, source_id: str) -> Path:
    return Path(enrichment_dir) / f"{source_id}.claims.json"


def claims_fingerprint(normalized_markdown: str, model_ref: str) -> str:
    return _fingerprint(normalized_markdown, model_ref, CLAIM_SCHEMA_VERSION, CLAIM_PROMPT_VERSION)


def concepts_artifact_path(enrichment_dir: Path, source_id: str) -> Path:
    return Path(enrichment_dir) / f"{source_id}.concepts.json"


def concepts_fingerprint(normalized_markdown: str, model_ref: str) -> str:
    return _fingerprint(normalized_markdown, model_ref, CONCEPT_SCHEMA_VERSION, CONCEPT_PROMPT_VERSION)


# --- concept starvation (ADR-0055) -----------------------------------------
#
# The F1 failure signature: a substantive document extracts zero concepts. The predicate reads
# existing artifact/claim state ONLY — never raw text length or normalized text shape (that would
# reopen the "substantive document" classifier problem). The threshold is a module constant, not
# config: a quality heuristic; configurability would add noise before operational evidence exists.
CONCEPT_STARVATION_ENTITY_THRESHOLD = 5
_ENTITY_FAMILY_TYPES = ("entity", "person", "organization", "project")


def concept_starved(nodes: list[dict[str, Any]], claim_count: int) -> bool:
    """True when a concepts artifact's node list shows the F1 pattern for a source.

    `concepts == 0 AND (entity_family >= threshold OR claims >= 1)` — one stored claim proves
    semantic substance; many entities without concepts is the starvation signature. A degenerate
    document (no entities, no claims) is not starved.
    """
    concepts = sum(1 for n in nodes if n.get("node_type") == "concept")
    entity_family = sum(1 for n in nodes if n.get("node_type") in _ENTITY_FAMILY_TYPES)
    return concepts == 0 and (entity_family >= CONCEPT_STARVATION_ENTITY_THRESHOLD
                              or claim_count >= 1)


def stored_claim_count(enrichment_dir: Path, source_id: str) -> int:
    """Claims recorded in the durable `<sid>.claims.json`, freshness-agnostic; 0 if missing/unreadable.

    Deliberately not `load_fresh_claims`: even a stale claims artifact proves the source carried
    extractable semantic substance, which is all the starvation predicate needs (ADR-0055). The
    artifact's internal `source_id` must match the filename (no spoofing) — same posture as the
    lint checks that consume this count.
    """
    path = claims_artifact_path(enrichment_dir, source_id)
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if data.get("source_id") != source_id:
        return 0
    claims = data.get("claims")
    return len(claims) if isinstance(claims, list) else 0


def _load_fresh(path: Path, normalized_markdown: str, fingerprint_fn) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if artifact.get("input_fingerprint") != fingerprint_fn(normalized_markdown, artifact.get("model_ref", "")):
        return None  # stale: normalized text / prompt / model changed since enrichment
    return artifact


def load_fresh(enrichment_dir: Path, source_id: str, normalized_markdown: str) -> dict[str, Any] | None:
    """Return the summary/tags artifact only if fresh for the current text; else None."""
    return _load_fresh(artifact_path(enrichment_dir, source_id), normalized_markdown, artifact_fingerprint)


def load_fresh_claims(enrichment_dir: Path, source_id: str, normalized_markdown: str) -> dict[str, Any] | None:
    """Return the claims artifact only if fresh for the current text; else None."""
    return _load_fresh(claims_artifact_path(enrichment_dir, source_id), normalized_markdown, claims_fingerprint)
