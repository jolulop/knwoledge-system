---
name: vault-query
description: >-
  Answer questions from the existing vault using index.md, summary callouts,
  wikilinks, keyword search, vector chunks, graph relationships, and citations.
  Use when the user asks what they know about something, wants to find notes,
  compare sources, identify disagreements, or produce a cited answer from the vault.
---

# Vault Query Skill

## Purpose

Answer questions using the knowledge system without loading the whole vault into context.

## Rules

- Read `wiki/index.md` first.
- Prefer summary-first navigation for synthesis/discovery questions.
- Use keyword/vector chunks for exact lookup.
- Use graph/claim relationships for disagreement and relatedness questions.
- Open only necessary pages/chunks.
- Cite every factual claim.
- If no evidence exists, write `No source found in vault.`
- Do not invent wikilinks, file names, paths, page numbers, line numbers, or timestamps.

## Retrieval routing

- "What do I know about X?" → index → summaries → graph → selected pages.
- "Find exact quote/clause/date/number" → keyword + vector chunks.
- "How are X and Y related?" → graph + synthesis pages.
- "Which sources disagree?" → claim graph + contradictions.
- "What changed recently?" → `wiki/log.md` + metadata database.

## Budgets

- Start with at most 5 candidate items.
- Start with at most 8 candidate sources.
- Follow graph links selectively.
- Prefer opening summary callouts before full bodies.

## Output format

Return:

- Answer
- Citations
- Confidence
- Retrieval path
- Unsourced claims, if any
- Suggested follow-up or saved query path, if useful
