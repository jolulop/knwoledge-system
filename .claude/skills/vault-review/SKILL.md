---
name: vault-review
description: >-
  Manage human review items for deletion, archiving, deprecation, contradiction
  resolution, entity/concept merge or split, duplicate detection, and low-confidence
  semantic changes. Use when the user asks to review, approve, reject, defer, merge,
  archive, deprecate, or inspect pending decisions.
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

- deletion
- archive
- deprecation
- contradiction
- merge
- split
- duplicate
- concept_promotion
- stale_summary
- citation_issue

## Output format

Return:

- Review items shown
- Decisions applied
- Files/pages updated
- Audit log path
- Validation results
