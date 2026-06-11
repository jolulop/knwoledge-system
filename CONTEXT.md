# Context

This document defines the shared language for the Knowledge System project.

## Core Terms

**Raw Source**
An original document, transcript, image, screenshot, HTML file, PDF, DOCX, spreadsheet, or Markdown file stored as the source of truth. Raw sources are not modified by agents.

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
