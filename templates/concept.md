---
type: concept
concept_id: "{{concept_id}}"      # cpt_<sha256(normalized_canonical_name)[:16]>, frozen at creation (ADR-0021)
title: "{{concept_name}}"
aliases: []                       # synonyms / surface variants; drives dedup + Obsidian display (ADR-0017)
status: candidate                 # candidate | active | deprecated_candidate | archived (ADR-0018/0022)
review_status: none               # none | pending | approved | rejected | deferred
generation_status: enriched       # deterministic | enriched | human_edited
confidence: low
source_count: 0                   # count of INDEPENDENT sources; ≥2 auto-promotes (ADR-0018)
derived_from: []                  # source_ids evidencing this concept
related_concepts: []              # concept_ids
claims: []                        # claim_ids
created: "{{created_at}}"
updated: "{{updated_at}}"
last_compiled_at: "{{last_compiled_at}}"
---

# {{concept_name}}

> [!summary]
> {{two_sentence_summary_for_navigation}}

## Definition

{{definition}}

## Why It Matters

{{why_it_matters}}

## Source Evidence

- [[Sources/{{source_id}}]] — {{evidence_summary}}

## Related Claims

- [[Claims/{{claim_id}}]]

## Related Concepts

- [[Concepts/{{related_concept_slug}}]]

## Open Questions

- {{open_question}}

## Review Notes

{{review_notes}}
