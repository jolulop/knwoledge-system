# Context

This document defines the shared language for the Knowledge System project.

## Core Terms

**Raw Source**
An original document, transcript, image, screenshot, HTML file, PDF, DOCX, spreadsheet, or Markdown file stored as the source of truth. Raw sources are not modified by agents.

**Source ID**
A deterministic identifier for a raw source derived from its content: `src_<first 16 hex characters of the SHA256>`. Because it is content-derived, byte-identical files share a single Source ID, which makes repeated intake scans idempotent.

**Manifest**
A JSON record at `raw/manifests/<source_id>.json` describing one unique raw source. There is one manifest per unique content, not per file. It holds the checksum, canonical file metadata, and an `occurrences[]` list. In Phase 1 the manifest files are the authoritative listing for the `/sources` endpoint and CLI.

**Occurrence**
One observed file path for a given Source ID. Byte-identical copies of the same content are recorded as additional occurrences inside the one manifest rather than as separate manifests. A redundant copy is an exact (SHA256) duplicate and does not require human review.

**Normalized Document**
A parsed representation of a raw source, usually Markdown or JSON, used for indexing, chunking, citation anchoring, and downstream processing.

**Wiki Page**
A derived Markdown page used for human browsing and agent navigation. Wiki pages are generated or maintained from raw sources and normalized documents.

**Source Page**
A wiki page representing one raw source.

**Concept Page**
A wiki page representing a durable recurring idea. A candidate concept should normally appear in at least two sources before promotion.

**Claim**
An atomic factual statement derived from one or more sources. Claims require citations where possible.

**Synthesis**
A higher-level explanation, comparison, or conclusion across multiple sources, concepts, or claims.

**Review Item**
A proposed semantic or destructive change requiring human approval, such as deletion, entity merging, contradiction resolution, deprecation, or archiving.

**Hybrid Retrieval**
The combination of keyword search, semantic/vector search, graph traversal, and summary-first wiki navigation.

**Summary-First Navigation**
The rule that every major wiki page includes a short `> [!summary]` callout so humans and agents can decide whether to open the full page.

**System of Record**
The canonical project repository under WSL at `~/code/knowledge-system`.
