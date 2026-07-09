#!/usr/bin/env python3
"""The knowledge-item taxonomy (ADR-0059): the single source of truth for `item_type`.

Semantic nodes are one structural family (`node_type: "item"`, type-neutral `itm_` id);
classification is the mutable, governed `item_type` drawn from the 15-value taxonomy below,
plus one QA-only sentinel. Everything that enumerates item types — the extraction schema,
the graph write gate, validators, the promote/retype executors, the review UI — imports
from here so the vocabulary cannot drift. The type list is ADR-gated, never a config knob.
"""
from __future__ import annotations

# The 15 production types (lowercase snake_case; TitleCase is a display convention).
ITEM_TYPES = frozenset({
    "domain",
    "ai_topic_area",
    "problem_risk",
    "use_case",
    "method_technique",
    "architecture_pattern",
    "technology_capability",
    "model",
    "model_family_architecture",
    "product_tool_platform",
    "data_ontology_asset",
    "standard_protocol_interface",
    "infrastructure_hardware",
    "governance_regulation",
    "provider_institution",
})

# QA-only sentinel (ADR-0059 decision 5): allowed in extraction output and on candidate
# nodes; forbidden on active nodes; excluded from recurrence auto-promotion; never a public
# navigation group. A promote approval must amend a real `item_type` before it can apply.
UNCLASSIFIED = "unclassified_review_required"

# Everything the extraction schema / graph gate accepts.
ITEM_TYPES_ALL = ITEM_TYPES | {UNCLASSIFIED}

# The user-supplied 15-step classifier priority order (ADR-0059 decision 4) — the prompt's
# tie-break when several types could apply. Every production type appears exactly once.
PRIORITY_ORDER: tuple[str, ...] = (
    "domain",
    "model",
    "ai_topic_area",
    "architecture_pattern",
    "model_family_architecture",
    "method_technique",
    "technology_capability",
    "use_case",
    "problem_risk",
    "product_tool_platform",
    "standard_protocol_interface",
    "data_ontology_asset",
    "governance_regulation",
    "infrastructure_hardware",
    "provider_institution",
)

# Prompt-level grouping (ADR-0059 decision 1): drives band guidance and the topic-starvation
# guard. The sentinel counts toward NEITHER group.
THEMATIC_TYPES = frozenset({
    "domain",
    "ai_topic_area",
    "problem_risk",
    "use_case",
    "method_technique",
    "architecture_pattern",
    "technology_capability",
    "model_family_architecture",
    "governance_regulation",
})
NAMED_TYPES = ITEM_TYPES - THEMATIC_TYPES


# Human-facing labels (TitleCase display convention). The sentinel's label deliberately
# reads as a QA bucket, never as a taxonomy category (ADR-0059 decision 5).
_DISPLAY_OVERRIDES = {"ai_topic_area": "AI Topic Area"}
UNCLASSIFIED_DISPLAY = "Unclassified (review required)"


def display_name(item_type: str) -> str:
    if item_type == UNCLASSIFIED:
        return UNCLASSIFIED_DISPLAY
    return _DISPLAY_OVERRIDES.get(item_type, item_type.replace("_", " ").title())


# Deterministic grouping order for navigation surfaces (Source pages, index.md): the
# production types in priority order, then the QA bucket last.
GROUP_ORDER: tuple[str, ...] = PRIORITY_ORDER + (UNCLASSIFIED,)


def is_item_type(value: object) -> bool:
    """True iff `value` is a valid stored item_type (production or sentinel)."""
    return isinstance(value, str) and value in ITEM_TYPES_ALL


def is_production_item_type(value: object) -> bool:
    """True iff `value` is one of the 15 production types (sentinel excluded)."""
    return isinstance(value, str) and value in ITEM_TYPES
