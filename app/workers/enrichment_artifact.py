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
# Phase 3.5b concept/entity extraction pass.
CONCEPT_SCHEMA_VERSION = "enrich-concepts-v1"
CONCEPT_PROMPT_VERSION = "enrich-concepts-prompt-v1"


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
