---
type: query
query_id: "{{query_id}}"          # qry_<sha256(normalized_question)[:16]>, frozen at creation (ADR-0021)
title: "{{question_title}}"
question: "{{question}}"
status: active                    # wiki lifecycle (ADR-0022)
review_status: none               # none | pending | approved | rejected | deferred
generation_status: enriched       # deterministic | enriched | human_edited
confidence: low
retrieval_modes: []               # which retrieval paths were used (policies/retrieval.yaml)
derived_from: []                  # source_ids cited in the answer
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
