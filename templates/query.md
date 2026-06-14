---
type: query
query_id: "{{query_id}}"
title: "{{question_title}}"
question: "{{question}}"
status: active
review_status: none
generation_status: enriched
confidence: low
retrieval_modes: []
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
last_compiled_at: "{{last_compiled_at}}"
---

# Query: {{question_title}}

> [!summary]
> {{two_sentence_summary_of_answer}}

## Question

{{question}}

## Answer

{{answer_with_citations}}

## Citations

<!-- Rendered view of frontmatter `citations` (the machine-readable record of truth). -->

| Source | Page / Section | Char range | Quote |
|---|---|---|---|
| [[Sources/{{source_id}}]] | {{page_or_section}} | {{char_start}}–{{char_end}} | {{short_quote}} |

## Retrieval Path

{{retrieval_path}}

## Unsourced Claims

{{unsourced_claims_or_none}}
