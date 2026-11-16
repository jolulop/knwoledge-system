---
name: vault-review
description: >-
  Manage human review items for deletion, archiving, deprecation, contradiction
  resolution, knowledge-item merge or split, item retype, duplicate detection, and
  low-confidence semantic changes. Use when the user asks to review, approve, reject,
  defer, merge, archive, deprecate, retype, or inspect pending decisions.
---

# Vault Review Skill

## Purpose

Prepare and apply human-reviewed decisions while preserving auditability.

## Rules

- Never perform destructive or semantic governance actions without explicit human approval.
- Show evidence before asking for or applying a decision.
- Review outcomes are recorded in `reviews/audit_log/` by the decision and Apply operations.
- A decision (approve, reject, defer, reopen) records intent only; **Apply** executes approved
  decisions and publishes the affected pages, graph edges and indexes (`POST /reviews/apply` with a
  clean preview's token, or `scripts/apply_reviews.py apply --token <id>`). Never edit `wiki/` pages
  or the graph directly (ADR-0074 change 7b). If Apply reports `publication_pending`, retry
  publication (`POST /publication/retry` or `scripts/republish.py`).

## Review item types

- delete_raw_file
- archive_source
- deprecate_wiki_page
- resolve_contradiction
- merge_items
- split_item
- change_item_type
- mark_semantic_duplicate
- promote_candidate_node (flat queue only; ADR-0066 per-document tag keep/drop replaces it for extraction suggestions)
- propose_synthesis

## Output format

Return:

- Review items shown
- Decisions applied
- Files/pages updated
- Audit log path
- Validation results
