#!/usr/bin/env python3
"""Canonical node answer-eligibility policy (ADR-0032 decision 2).

Which node types' *active* prose may back an answer. This is a retrieval/eligibility policy, not
an implementation detail of any one index, so it lives in a neutral module shared by the keyword
navigation index (Phase 4a, `keyword_index.py`) and the graph read projection (Phase 4b,
`graph_read.py`). Keeping one home avoids the two layers drifting apart.

Source/query/tag pages are navigation aids only and are never answer_eligible; a non-`active`
node (candidate/deprecated/archived/…) is never answer_eligible regardless of type.
"""
from __future__ import annotations

ANSWER_ELIGIBLE_TYPES = frozenset(
    {"concept", "entity", "person", "organization", "project", "synthesis", "claim"}
)


def is_answer_eligible(node_type: str | None, status: str | None) -> bool:
    return status == "active" and node_type in ANSWER_ELIGIBLE_TYPES
