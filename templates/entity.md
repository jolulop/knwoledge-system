---
type: entity
entity_id: "{{entity_id}}"        # ent_<sha256(normalized_canonical_name)[:16]>, frozen at creation (ADR-0021)
title: "{{entity_name}}"
aliases: []                       # synonyms / surface variants; drives dedup + Obsidian display (ADR-0017)
status: candidate                 # candidate | active | deprecated_candidate | archived (ADR-0022)
review_status: none               # none | pending | approved | rejected | deferred
generation_status: enriched       # deterministic | enriched | human_edited
confidence: low
source_count: 0                   # count of independent sources (ADR-0018)
derived_from: []                  # source_ids evidencing this entity
related_entities: []              # entity_ids
claims: []                        # claim_ids
created: "{{created_at}}"
updated: "{{updated_at}}"
last_compiled_at: "{{last_compiled_at}}"
---

# {{entity_name}}

> [!summary]
> {{two_sentence_summary}}

## Description

{{description}}

## Source Evidence

- [[Sources/{{source_id}}]] — {{evidence_summary}}

## Related Claims

- [[Claims/{{claim_id}}]]

## Related Entities

- [[Entities/{{related_entity_slug}}]]

## Review Notes

{{review_notes}}
