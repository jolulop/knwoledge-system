---
type: synthesis
synthesis_id: "{{synthesis_id}}"  # syn_<sha256(normalized_title)[:16]>, frozen at creation (ADR-0021)
title: "{{title}}"
status: candidate                 # candidate | active | deprecated_candidate | archived (ADR-0022)
review_status: none               # none | pending | approved | rejected | deferred
generation_status: enriched       # deterministic | enriched | human_edited
confidence: low
source_count: 0                   # count of independent sources synthesized
derived_from: []                  # source_ids
# citations: structured objects (ADR-0019/0020); authoritative anchor is (source_id, char_start, char_end).
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
concepts: []                      # concept_ids
claims: []                        # claim_ids
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
