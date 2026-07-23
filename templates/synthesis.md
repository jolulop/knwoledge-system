<!-- Illustrative page shape ONLY: synthesis pages are code-rendered by
     app/workers/wiki_render.py::render_synthesis_page (like every semantic page since Phase 3.5b).
     The authoritative frontmatter is whatever that renderer emits (e.g. `derived_from:` claim ids,
     `topic_node:`, `aliases:`) — this template is reference shape, not a spec. See item.md. -->
---
type: synthesis
synthesis_id: "{{synthesis_id}}"
title: "{{title}}"
status: candidate
review_status: none
generation_status: enriched
confidence: low
source_count: 0
derived_from: []
citations:
  - source_id: "{{source_id}}"
    char_start: {{char_start}}
    char_end: {{char_end}}
    page: {{page_or_null}}
    page_end: {{page_end_or_null}}
    section: "{{section_or_null}}"
    table_reference: {{table_reference_or_null}}
    sheet_reference: {{sheet_reference_or_null}}
    chunk_id: "{{chunk_id_advisory}}"
    quote: "{{short_quote}}"
created: "{{created_at}}"
updated: "{{updated_at}}"
last_compiled_at: "{{last_compiled_at}}"
---

# {{title}}

> [!summary]
> {{two_sentence_summary}}

## Synthesis

{{synthesis_text}}

## Supporting Evidence

- [[Claims/{{claim_id}}]]
- [[Sources/{{source_id}}]]

## Disagreements or Contradictions

- [[Claims/{{claim_id}}]] contradicts [[Claims/{{other_claim_id}}]]

## Confidence

{{confidence_explanation}}

## Review Notes

{{review_notes}}
