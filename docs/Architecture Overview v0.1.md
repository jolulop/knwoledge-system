# Architecture Overview v0.1
## Local-First Agentic Information Management System

**Status:** Draft v0.1  
**Repository root:** `~/code/knowledge-system`  
**Primary pattern:** LLM Wiki + structured filing cabinet + hybrid retrieval

---

## 1. Architecture Thesis

The system is built around a distinction between:

1. **Raw source memory** — immutable files and transcripts.
2. **Compiled knowledge** — generated wiki pages, summaries, concepts, claims, and syntheses.
3. **Retrieval infrastructure** — keyword search, vector search, graph traversal, and summary-first navigation.
4. **Maintenance loop** — deterministic validators, agent workflows, and human review.

A folder of Markdown files alone is not enough. The system becomes useful when the files are structured, indexed, linked, validated, searched, and maintained.

---

## 2. High-Level System Diagram

```text
                       ┌────────────────────────────┐
                       │ Human + Auto Collectors     │
                       │ browser clipper, folders,   │
                       │ manual uploads, transcripts │
                       └─────────────┬──────────────┘
                                     │
                                     ▼
┌────────────────────────────────────────────────────────────────┐
│ Raw Repository                                                  │
│ raw/inbox/ raw/permanent/ raw/ephemeral/ raw/assets/            │
│ raw/transcripts/ raw/manifests/                                 │
└────────────────────────────┬───────────────────────────────────┘
                             │
                             ▼
┌────────────────────────────────────────────────────────────────┐
│ Extraction + Normalization Pipeline                            │
│ PDF/DOCX/HTML/MD/XLSX/image/screenshot/transcript handling      │
└───────────────┬──────────────────────────────┬────────────────┘
                │                              │
                ▼                              ▼
┌────────────────────────────┐      ┌────────────────────────────┐
│ Normalized Text Store       │      │ Derived Artifact Store      │
│ markdown, chunks, tables    │      │ thumbnails, OCR, captions   │
└───────────────┬────────────┘      └────────────────────────────┘
                │
                ▼
┌────────────────────────────────────────────────────────────────┐
│ Filing-Cabinet Wiki Compiler                                   │
│ summaries, source pages, concept pages, claims, entities,       │
│ backlinks, synthesis, review proposals                          │
└───────────────┬──────────────────────────────┬────────────────┘
                │                              │
                ▼                              ▼
┌────────────────────────────┐      ┌────────────────────────────┐
│ Markdown Wiki               │      │ Search + Graph Indexes      │
│ index.md, log.md, Sources,  │      │ keyword, vector, graph       │
│ Concepts, Claims, etc.      │      │ metadata DB                  │
└───────────────┬────────────┘      └────────────────────────────┘
                │
                ▼
┌────────────────────────────────────────────────────────────────┐
│ API + Tool Layer                                                │
│ search, query, graph, review, ingest, lint, reindex             │
└───────────────┬──────────────────────────────┬────────────────┘
                │                              │
                ▼                              ▼
┌────────────────────────────┐      ┌────────────────────────────┐
│ Browser / PWA UI            │      │ Agent Clients               │
│ search, browse, review      │      │ Claude Code, Codex, CLI     │
└────────────────────────────┘      └────────────────────────────┘
```

---

## 3. Runtime Model

```text
Windows 11
├─ Human interface
│  ├─ VS Code UI
│  ├─ Claude Code extension UI
│  ├─ Obsidian
│  └─ Browser
└─ Docker Desktop UI

WSL2 Ubuntu
├─ System of record
├─ Repository
├─ Backend API
├─ Workers
├─ Scripts
├─ Local databases
├─ Search indexes
├─ Claude Code CLI
└─ Docker runtime integration
```

The repository must live in WSL:

```text
/home/jolulop/code/knowledge-system
```

---

## 4. Storage Layers

### 4.1 Raw Repository

Raw files are source of truth.

```text
raw/
├─ inbox/        new incoming files
├─ permanent/    retained originals
├─ ephemeral/    low-value temporary captures
├─ assets/       images and extracted assets
├─ transcripts/  audio/video transcripts
└─ manifests/    metadata and checksums
```

Rules:

- Agents must not modify raw originals.
- Raw files are not deleted automatically.
- Raw deletion requires human approval.
- Every raw file must have a manifest.

### 4.2 Normalized Store

```text
normalized/
├─ markdown/
├─ chunks/
├─ tables/
├─ images/
└─ extraction_logs/
```

Purpose:

- Store extracted text.
- Preserve citation anchors.
- Provide input for wiki generation and indexing.

### 4.3 Wiki Layer

```text
wiki/
├─ index.md
├─ log.md
├─ Sources/
├─ Concepts/
├─ Claims/
├─ Entities/
├─ People/
├─ Organizations/
├─ Projects/
├─ Tags/
├─ Synthesis/
└─ Queries/
```

Purpose:

- Human browsing.
- Agent navigation.
- Obsidian graph view.
- Long-lived synthesized knowledge.

### 4.4 Indexes and Databases

```text
indexes/                  # ADR-0032 §7: derived & gitignored
├─ keyword/               #   keyword.sqlite (FTS5: evidence chunks + wiki navigation)
├─ vector/                #   LanceDB (Phase 4d)
└─ graph/                 #   reserved (graph authority is db/graph.sqlite)

db/                       # ADR-0032 §7 supersedes metadata.sqlite below:
├─ metadata.sqlite        #   keyword index moved to indexes/keyword/; db/ now holds
└─ jobs.sqlite            #   jobs.sqlite, graph.sqlite, llm_cache.sqlite
```

Purpose:

- Fast search.
- Graph traversal.
- Job state persistence.
- API query support.

---

## 5. Data Flow

### 5.1 Ingest Flow

```text
File appears in raw/inbox
        ↓
Manifest created
        ↓
Checksum computed
        ↓
Duplicate check
        ↓
Extraction
        ↓
Normalization
        ↓
Chunking
        ↓
Source page generated
        ↓
Claims/entities/concepts proposed
        ↓
Wiki pages updated
        ↓
Backlinks updated
        ↓
Indexes updated
        ↓
Review items created if needed
```

### 5.2 Query Flow

```text
User asks question
        ↓
Query router classifies question
        ↓
Select retrieval path
        ↓
Retrieve candidate evidence
        ↓
Validate citations
        ↓
Generate answer
        ↓
Return answer + citations + retrieval path
        ↓
Optionally save query page
```

### 5.3 Lint Flow

```text
Scheduled or manual lint
        ↓
Validate frontmatter
        ↓
Validate summary callouts
        ↓
Validate wikilinks
        ↓
Validate citations
        ↓
Detect orphan nodes
        ↓
Detect weak concepts
        ↓
Detect stale summaries
        ↓
Detect contradictions
        ↓
Create review items
```

---

## 6. Retrieval Architecture

The system uses multiple retrieval paths.

| Retrieval Path | Best For |
|---|---|
| `index.md` + summary callouts | Broad discovery and “what do I know?” questions. |
| Keyword search | Exact terms, filenames, acronyms, quotes, numbers. |
| Vector search | Semantic similarity and fuzzy conceptual search. |
| Graph traversal | Relationships between sources, claims, people, orgs, projects, and concepts. |
| Claim graph | Contradictions, support, supersession, and evidence chains. |

---

## 7. Query Routing Rules

| Query Type | Route |
|---|---|
| Synthesis/discovery | Wiki navigation + graph traversal. |
| Exact lookup | Keyword search + vector chunks. |
| Relationship question | Graph traversal + selected wiki pages. |
| Contradiction question | Claim graph + review items. |
| Recent change question | `log.md` + metadata DB. |
| Archive/stale question | Retention policy + metadata DB. |

The system should not blindly run every query through every retrieval method. It should use the cheapest reliable path first and escalate when needed.

---

## 8. Agent Architecture

### 8.1 Development Agents

Claude Code and Codex are used to build and maintain the codebase.

Claude Code is primary. Codex compatibility is preserved through `AGENTS.md`.

### 8.2 Runtime Workers

Future scheduled work should be implemented as backend workers.

Examples:

- Watcher worker.
- Extractor worker.
- Indexer worker.
- Lint worker.
- Retention worker.
- Backup worker.
- Evaluation worker.

### 8.3 Human-Facing Agent Operations

Keep the user-facing operations simple:

```text
ingest
query
review
lint
```

---

## 9. Deterministic Enforcement

Some things should not depend only on LLM instructions.

Required deterministic checks:

- Rebuild `index.md`.
- Validate frontmatter.
- Validate summary callout presence.
- Validate wikilinks.
- Validate citations.
- Validate raw paths.
- Validate bidirectional backlinks.
- Reindex changed files.

---

## 10. Human Review Boundaries

The following require human approval:

- Raw deletion.
- Raw archiving.
- Entity merge.
- Entity split.
- Concept split.
- Contradiction resolution.
- Deprecation.
- Semantic duplicate merge.
- Low-confidence concept promotion.

---

## 11. Security Architecture

Default security posture:

- Localhost only for v0.1.
- No public internet exposure.
- Authentication required before LAN/mobile access.
- Imported documents are untrusted.
- Document text is data, not instructions.
- No secrets in Git.
- API keys optional during development.

---

## 12. v0.1 Technology Choices

| Component | v0.1 Decision |
|---|---|
| Runtime | WSL2 Ubuntu |
| Host OS | Windows 11 |
| Development UI | VS Code WSL |
| Primary coding assistant | Claude Code |
| Project Python | Python 3.12 via uv |
| Backend | FastAPI |
| Metadata DB | SQLite |
| Job DB | SQLite |
| Keyword search | SQLite FTS5 or simple file index first |
| Vector store | LanceDB or ChromaDB later |
| Graph | SQLite graph tables first |
| UI | Browser app later; Obsidian optional |
| API port | 18000 |
| Future MCP port | 18001 |
| Future UI port | 13000 |

---

## 13. Architecture Risks

| Risk | Mitigation |
|---|---|
| Hallucinated citations | Citation verifier and answer validation. |
| Summary rot | Lint compares summaries with page bodies. |
| Concept promiscuity | Promote concepts only after recurrence or review. |
| Too much capture | Permanent vs ephemeral raw separation. |
| Context loss in long sessions | Persist job state to DB/manifests. |
| Broken Obsidian links | Wikilink validator. |
| Search noise | Hybrid routing and evaluation. |
| Raw deletion mistakes | Human approval required. |
| Port collisions | Reserved ports: 18000, 18001, 13000. |

---

## 14. Architecture Decision Summary

The system architecture is:

```text
Local-first raw repository
+ normalized extraction layer
+ generated wiki/filing cabinet
+ summary-first navigation
+ bidirectional backlinks
+ keyword/vector/graph retrieval
+ deterministic validators
+ review workflow
+ future API workers for autonomous processing
+ Claude Code for development and supervised maintenance
```
