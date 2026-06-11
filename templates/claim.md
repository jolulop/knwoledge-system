---
type: claim
claim_id: "{{claim_id}}"
status: active
confidence: low
sources: []
supports: []
contradicts: []
superseded_by: []
created: "{{created_at}}"
updated: "{{updated_at}}"
---

# Claim: {{short_claim_title}}

> [!summary]
> {{two_sentence_summary}}

## Claim

{{claim_text}}

## Evidence

| Source | Location | Evidence |
|---|---|---|
| [[Sources/{{source_slug}}]] | {{page_section_line_or_timestamp}} | {{short_evidence}} |

## Supporting Claims

- [[Claims/{{claim_id}}]]

## Contradicting Claims

- [[Claims/{{claim_id}}]]

## Status Notes

{{status_notes}}
