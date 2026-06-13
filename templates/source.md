---
type: source
source_id: "{{source_id}}"
title: "{{title}}"
aliases: ["{{title}}"]
relative_raw_path: "{{relative_raw_path}}"
normalized_path: "{{normalized_path}}"
sha256: "{{sha256}}"
file_type: "{{file_type}}"
language: "{{language}}"
page_count: {{page_count}}
chunk_count: {{chunk_count}}
status: active                 # wiki lifecycle (ADR-0022); not extraction state
ingestion_status: "{{ingestion_status}}"   # read-only mirror of the manifest (ADR-0011/0022)
summary_status: stub           # stub | enriched (ADR-0016)
generation_status: deterministic   # deterministic | enriched | human_edited (ADR-0022)
created: "{{created_at}}"
ingested: "{{ingested_at}}"
last_compiled_at: "{{last_compiled_at}}"
tags: []
concepts: []
entities: []
people: []
organizations: []
projects: []
---

# {{title}}

> [!summary] Extractive excerpt (auto-generated, unverified)
> {{extractive_excerpt}}

## Source Details

- Raw file: `{{relative_raw_path}}`
- Normalized file: `{{normalized_path}}`
- Type: {{file_type}}
- Language: {{language}}
- Pages: {{page_count}}
- Chunks: {{chunk_count}}
- Checksum: {{sha256}}

## Key Points

_Pending semantic enrichment._

## Claims

_Pending semantic enrichment._

## Concepts Mentioned

_Pending semantic enrichment._

## Entities Mentioned

_Pending semantic enrichment._

## Notes

{{notes}}
