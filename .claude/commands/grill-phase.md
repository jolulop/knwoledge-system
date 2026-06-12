---
description: Phase-gate review that grills a plan and updates CONTEXT.md/ADRs only
argument-hint: [plan or phase description]
allowed-tools: Read, Grep, Glob, LS, Write, Edit
---

You are performing a planning and documentation gate for this project.

Read:
- CONTEXT.md
- CLAUDE.md
- AGENTS.md
- docs/
- docs/adr/

Task:
Grill the following plan: $ARGUMENTS

Rules:
- Ask one question at a time.
- Check the repository before asking if the answer may already exist.
- Recommend a likely answer when useful.
- Update `CONTEXT.md` only when project language or terminology changes.
- Create or update ADRs in `docs/adr/` only when a decision is durable, surprising, hard to reverse, or has a meaningful tradeoff.
- Do not write production code.
- Do not modify application logic.
- Do not create migrations.
- Do not change tests.
- Do not implement Phase work.
- Stop after the grilling/documentation pass and summarize:
  1. resolved decisions,
  2. unresolved questions,
  3. documentation files changed,
  4. recommended next implementation step.
