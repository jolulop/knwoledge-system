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
status: active
ingestion_status: "{{ingestion_status}}"
summary_status: {{summary_status}}
generation_status: {{generation_status}}
created: "{{created_at}}"
ingested: "{{ingested_at}}"
input_fingerprint: "{{input_fingerprint}}"
tags: {{tags}}
concepts: {{concepts_fm}}
entities: {{entities_fm}}
people: {{people_fm}}
organizations: {{organizations_fm}}
projects: {{projects_fm}}
---

# {{title}}

> [!summary] {{summary_label}}
> {{summary_text}}

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

{{claims_block}}

## Concepts Mentioned

{{concepts_block}}

## Entities Mentioned

{{entities_block}}

## People Mentioned

{{people_block}}

## Organizations Mentioned

{{organizations_block}}

## Projects Mentioned

{{projects_block}}

## Notes

{{notes}}
