<!-- Illustrative page shape ONLY: item pages are code-rendered by
     app/workers/wiki_render.py::render_item_page (like every semantic page since Phase 3.5b).
     Whether semantic pages become formally code-rendered — and what the CLAUDE/AGENTS
     "use templates" rule then means — is the queued dead-surface/template cleanup slice. -->
---
type: item
item_id: "{{item_id}}"
item_type: {{item_type}}
title: "{{item_name}}"
aliases: []
status: candidate
review_status: none
generation_status: deterministic
confidence: low
---

# {{item_name}}

> [!summary] Candidate item — {{item_type_display}}
> {{one_sentence_summary_for_navigation}}

## Description

{{optional_human_description}}

## Aliases

- {{alias}}

## Mentioned By

- [[Sources/{{source_id}}]]

## Notes

{{notes}}
