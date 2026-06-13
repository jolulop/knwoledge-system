---
type: claim
claim_id: "{{claim_id}}"          # clm_<sha256(normalized_claim_text|primary_source_id)[:16]> (ADR-0021)
status: active                    # wiki lifecycle (ADR-0022)
review_status: none               # none | pending | approved | rejected | deferred
generation_status: enriched       # deterministic | enriched | human_edited
confidence: low
derived_from: []                  # source_ids this claim is drawn from
# citations: structured objects (ADR-0019/0020). (source_id, char_start, char_end) is
# the authoritative anchor; chunk_id is advisory only.
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
supports: []
contradicts: []
superseded_by: []
created: "{{created_at}}"
updated: "{{updated_at}}"
last_compiled_at: "{{last_compiled_at}}"
---

# Claim: {{short_claim_title}}

> [!summary]
> {{two_sentence_summary}}

## Claim

{{claim_text}}

## Evidence

<!-- Rendered view of frontmatter `citations` (the machine-readable record of truth). -->

| Source | Page / Section | Char range | Quote |
|---|---|---|---|
| [[Sources/{{source_id}}]] | {{page_or_section}} | {{char_start}}–{{char_end}} | {{short_quote}} |

## Supporting Claims

- [[Claims/{{supporting_claim_id}}]]

## Contradicting Claims

- [[Claims/{{contradicting_claim_id}}]]

## Status Notes

{{status_notes}}
