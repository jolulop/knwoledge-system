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


def artifact_path(enrichment_dir: Path, source_id: str) -> Path:
    return Path(enrichment_dir) / f"{source_id}.json"


def artifact_fingerprint(normalized_markdown: str, model_ref: str) -> str:
    """Hash every input that should force re-enrichment (ADR-0027)."""
    h = hashlib.sha256()
    for part in (
        SCHEMA_VERSION.encode("utf-8"),
        PROMPT_VERSION.encode("utf-8"),
        model_ref.encode("utf-8"),
        normalized_markdown.encode("utf-8"),
    ):
        h.update(part)
        h.update(b"\0")
    return h.hexdigest()[:16]


def load_fresh(
    enrichment_dir: Path, source_id: str, normalized_markdown: str
) -> dict[str, Any] | None:
    """Return the enrichment artifact only if it is fresh for the current text; else None."""
    path = artifact_path(enrichment_dir, source_id)
    if not path.exists():
        return None
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    model_ref = artifact.get("model_ref", "")
    expected = artifact_fingerprint(normalized_markdown, model_ref)
    if artifact.get("input_fingerprint") != expected:
        return None  # stale: normalized text / prompt / model changed since enrichment
    return artifact
