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
- Write review outcomes to `reviews/audit_log/`.
- Update affected wiki pages, graph edges, and indexes after approved changes.

## Review item types

- delete_raw_file
- archive_source
- deprecate_wiki_page
- resolve_contradiction
- merge_items
- split_item
- change_item_type
- mark_semantic_duplicate
- promote_candidate_node
- propose_synthesis

## Output format

Return:

- Review items shown
- Decisions applied
- Files/pages updated
- Audit log path
- Validation results
