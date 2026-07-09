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

from app.backend import taxonomy

SCHEMA_VERSION = "enrich-summary-tags-v1"
PROMPT_VERSION = "enrich-summary-tags-prompt-v1"
# Phase 3.5b claim-extraction pass (separate artifact + versions, tier-2).
# v2 = the ADR-0056 segment-framed prompt (claims are extracted per claim window; each call
# states "segment i of N" + local section context).
CLAIM_SCHEMA_VERSION = "enrich-claims-v1"
CLAIM_PROMPT_VERSION = "enrich-claims-prompt-v2"
# Tier-2 knowledge-item extraction pass (ADR-0059): ONE items array classified by
# knowledge-object role, replacing the concepts/entities two-array contract of
# ADR-0055/0056 (fresh version lineage — the clean-repository restart means there is no
# prior artifact to stay compatible with). The elicitation contract, noise boundaries, and
# full-doc coverage strategy carry over. Each bump makes every prior artifact stale, so the
# next extract_items run re-extracts opt-in — no --force machinery.
ITEMS_SCHEMA_VERSION = "enrich-items-v2"      # v2: label renames ai_topic_area / model_family_architecture
ITEMS_PROMPT_VERSION = "enrich-items-prompt-v2"

# ADR-0056 extraction-strategy identity. The strategy ref is the explicit coverage component
# of tier-2 identity — composed alongside (never folded into) schema/prompt versions in both
# the artifact freshness fingerprint and the response-cache key. The knob VALUES are part of
# the ref: changing `ENRICH_CLAIM_WINDOW_CHARS` / `ENRICH_ITEMS_INPUT_MAX_CHARS` restales
# that pass vault-wide (cost-bearing semantic knobs, ADR-0033 config-ref precedent).
CLAIM_WINDOW_STRATEGY = "chunk-greedy-v1"
ITEMS_COVERAGE_STRATEGY = "full-doc-v1"


def claims_strategy_ref(window_chars: int) -> str:
    return f"{CLAIM_WINDOW_STRATEGY}:{window_chars}"


def items_strategy_ref(input_max_chars: int) -> str:
    return f"{ITEMS_COVERAGE_STRATEGY}:{input_max_chars}"
# Phase 3.5c contradiction-detection pass (tier-3; per claim pair, response-cache replayed).
CONTRADICTION_SCHEMA_VERSION = "enrich-contradiction-v1"
CONTRADICTION_PROMPT_VERSION = "enrich-contradiction-prompt-v1"
# Phase 3.5c cross-source synthesis pass (tier-3; per active knowledge item).
SYNTHESIS_SCHEMA_VERSION = "enrich-synthesis-v1"
SYNTHESIS_PROMPT_VERSION = "enrich-synthesis-prompt-v1"


def synthesis_artifact_path(enrichment_dir: Path, node_id: str) -> Path:
    return Path(enrichment_dir) / f"{node_id}.synthesis.json"


def _fingerprint(
    normalized_markdown: str, model_ref: str, schema_version: str, prompt_version: str,
    strategy_ref: str | None = None,
) -> str:
    """Hash every input that should force re-enrichment (ADR-0027).

    `strategy_ref` (ADR-0056) is the composed extraction-strategy component — appended only
    when present so passes without one (summary/contradiction/synthesis) keep their
    pre-ADR-0056 fingerprints.
    """
    parts = [
        schema_version.encode("utf-8"),
        prompt_version.encode("utf-8"),
        model_ref.encode("utf-8"),
        normalized_markdown.encode("utf-8"),
    ]
    if strategy_ref is not None:
        parts.append(strategy_ref.encode("utf-8"))
    h = hashlib.sha256()
    for part in parts:
        h.update(part)
        h.update(b"\0")
    return h.hexdigest()[:16]


def artifact_path(enrichment_dir: Path, source_id: str) -> Path:
    return Path(enrichment_dir) / f"{source_id}.json"


def artifact_fingerprint(normalized_markdown: str, model_ref: str,
                         strategy_ref: str | None = None) -> str:
    # The summary pass has no strategy ref; the parameter exists so `_load_fresh` can pass a
    # STRAY `strategy_ref` from a malformed/tampered artifact and get "stale" (fingerprint
    # mismatch) instead of a TypeError crash (ADR-0056 review round 2).
    return _fingerprint(normalized_markdown, model_ref, SCHEMA_VERSION, PROMPT_VERSION,
                        strategy_ref)


def claims_artifact_path(enrichment_dir: Path, source_id: str) -> Path:
    return Path(enrichment_dir) / f"{source_id}.claims.json"


def claims_fingerprint(normalized_markdown: str, model_ref: str,
                       strategy_ref: str | None = None) -> str:
    return _fingerprint(normalized_markdown, model_ref, CLAIM_SCHEMA_VERSION,
                        CLAIM_PROMPT_VERSION, strategy_ref)


def items_artifact_path(enrichment_dir: Path, source_id: str) -> Path:
    return Path(enrichment_dir) / f"{source_id}.items.json"


def items_fingerprint(normalized_markdown: str, model_ref: str,
                      strategy_ref: str | None = None) -> str:
    return _fingerprint(normalized_markdown, model_ref, ITEMS_SCHEMA_VERSION,
                        ITEMS_PROMPT_VERSION, strategy_ref)


# --- topic starvation (ADR-0059, redefining ADR-0055's concept starvation) --
#
# The F1 failure signature generalized: a substantive document extracts no thematic topic
# layer. The predicate reads existing artifact/claim state ONLY — never raw text length or
# normalized text shape (that would reopen the "substantive document" classifier problem).
# The threshold is a module constant, not config: a quality heuristic; configurability would
# add noise before operational evidence exists.
TOPIC_STARVATION_NAMED_THRESHOLD = 5


def topic_starved(nodes: list[dict[str, Any]], claim_count: int) -> bool:
    """True when an items artifact's node list shows the starvation pattern for a source.

    `thematic == 0 AND (named >= threshold OR claims >= 1)` — one stored claim proves
    semantic substance; many named items without a thematic layer is the starvation
    signature. The sentinel counts toward NEITHER group; a degenerate document (no named
    items, no claims) is not starved.
    """
    thematic = sum(1 for n in nodes if n.get("item_type") in taxonomy.THEMATIC_TYPES)
    named = sum(1 for n in nodes if n.get("item_type") in taxonomy.NAMED_TYPES)
    return thematic == 0 and (named >= TOPIC_STARVATION_NAMED_THRESHOLD
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
    # Recompute with the artifact's OWN recorded model_ref/strategy_ref (ADR-0056): freshness
    # here means "does this artifact still describe the current text under its recorded
    # parameters" — the producer-side skip check is where a changed knob/model restales.
    kwargs = {}
    if artifact.get("strategy_ref") is not None:
        kwargs["strategy_ref"] = artifact["strategy_ref"]
    if artifact.get("input_fingerprint") != fingerprint_fn(
            normalized_markdown, artifact.get("model_ref", ""), **kwargs):
        return None  # stale: normalized text / prompt / schema changed since enrichment
    return artifact


def load_fresh(enrichment_dir: Path, source_id: str, normalized_markdown: str) -> dict[str, Any] | None:
    """Return the summary/tags artifact only if fresh for the current text; else None."""
    return _load_fresh(artifact_path(enrichment_dir, source_id), normalized_markdown, artifact_fingerprint)


def load_fresh_claims(enrichment_dir: Path, source_id: str, normalized_markdown: str) -> dict[str, Any] | None:
    """Return the claims artifact only if fresh for the current text; else None."""
    return _load_fresh(claims_artifact_path(enrichment_dir, source_id), normalized_markdown, claims_fingerprint)
