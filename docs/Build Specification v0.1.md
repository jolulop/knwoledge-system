# Build Specification v0.1
## Local-First Agentic Information Management System Using the LLM Wiki Pattern

**Status:** Draft v0.1  
**Primary user:** Individual user building a personal research and business knowledge base  
**Target environment:** Windows 11 + WSL2 Ubuntu + NVIDIA RTX 5090 24 GB GPU + 64 GB RAM + 1 TB storage  
**Repository root:** `~/code/knowledge-system`  
**Primary interfaces:** Browser app, optional Obsidian, optional mobile PWA  
**Primary development assistant:** Claude Code  
**Codex compatibility:** Required through `AGENTS.md`  
**Core pattern:** LLM Wiki + structured filing-cabinet layer + hybrid search + human review

---

## 1. Executive Summary

This project builds a local-first information management system that ingests documents, stores immutable raw sources, extracts normalized text and metadata, generates summaries and semantic structure, maintains a persistent wiki/knowledge graph, and provides cited keyword, semantic, and graph-based search.

The system is not merely an Obsidian vault and not merely a RAG pipeline. It combines:

1. **Immutable raw source repository**  
   Stores original files and transcripts as the source of truth.

2. **Structured LLM Wiki / filing-cabinet layer**  
   Generates durable Markdown pages for sources, concepts, entities, claims, projects, people, organizations, tags, queries, and syntheses.

3. **Hybrid retrieval layer**  
   Provides keyword search, vector search, graph traversal, and summary-first wiki navigation.

4. **Agentic maintenance loop**  
   Runs autonomous ingest, lint, stale-content review, contradiction detection, duplicate detection, citation verification, and index refresh.

5. **Human-in-the-loop review**  
   Requires approval for deletion, contradiction resolution, entity merging, and deprecation decisions.

The first version is designed for a single primary user, with a clean path toward a multi-user or team system later.

---

## 2. User Requirements and Assumptions

### 2.1 Primary Use Cases

The system supports:

- Personal research knowledge management.
- Business knowledge base management.
- Cross-document synthesis.
- Long-term accumulation of knowledge.
- Semantic and keyword search.
- Browsing and editing through Obsidian and a browser UI.
- Mobile-friendly search and browsing through a responsive web app or PWA.

### 2.2 Initial Scale

- Initial corpus: approximately **600 documents**.
- Expected growth: approximately **50 documents per month**.
- Largest files: approximately **50 MB**.
- Audio/video: store transcripts, not original media files.
- Expected storage: 1 TB local disk, with backup policy required.

### 2.3 File Formats

The system must support:

- PDF, high-quality text-based, not scanned.
- DOCX.
- HTML.
- Markdown.
- Spreadsheets.
- Images.
- Screenshots.
- Video/audio transcripts.
- Optional future support for additional formats.

### 2.4 Languages

- Primary language: English.
- Secondary language: Spanish.
- Generated tags, summaries, metadata, and wiki pages should default to English.
- Source language must be preserved in metadata.
- Spanish sources may be summarized in English while preserving important original terms.

### 2.5 AI Model Policy

- Cloud models are allowed.
- OpenAI and Anthropic are acceptable.
- Local-only AI is not required.
- Initially there are no strict sensitive document rules, but the architecture must be security-ready.
- API keys are optional during development.
- Claude Code may be authenticated through the user subscription login for development.

### 2.6 Claude Code Usage Model

Claude Code has two distinct roles:

| Role | Use in this project |
|---|---|
| Development assistant | Primary role. Used to edit code, run scripts, implement phases, create tests, and maintain repository documentation. |
| Manual wiki maintainer | Useful for supervised operations such as ingesting a small batch, querying the vault, or running lint interactively. |
| Production autonomous processor | Not the target runtime. Scheduled autonomous document processing should eventually run through backend workers and API-based LLM calls, not an interactive Claude Code terminal session. |

Practical rule:

```text
Claude Code = build, debug, supervise, and manually maintain.
Backend workers = scheduled autonomous processing.
```

### 2.7 Hardware and Runtime

- Windows 11 host.
- WSL2 Ubuntu primary runtime.
- NVIDIA RTX 5090 with 24 GB VRAM.
- 64 GB RAM.
- 1 TB storage.
- Docker Desktop with WSL2 backend is acceptable.
- WSL2/GPU setup is assumed to be working.

---

## 3. Design Principles

### 3.1 Raw Sources Are the Source of Truth

Raw files must not be modified by AI agents.

Raw sources may be copied, parsed, indexed, summarized, archived, or marked as deprecated, but the original source file must remain available unless a human explicitly approves deletion.

### 3.2 The Wiki Is Derived, Reviewable, and Regenerable

The wiki is a generated interpretation layer. It should be useful, browsable, and editable, but it is not the source of truth.

Every generated factual claim must cite raw evidence where possible.

### 3.3 The System Should Compile Knowledge, Not Rediscover It Every Time

New sources should update existing source pages, concept pages, entity pages, claim pages, and synthesis pages. Knowledge should compound over time.

### 3.4 Summary-First Navigation Is Mandatory

Every major generated page must include a short summary callout designed for both human readers and future agents.

Required pattern:

```markdown
> [!summary]
> Two-sentence summary written for the next agent. It should explain what this page is,
> when it matters, and whether the full page should be opened.
```

### 3.5 Bidirectional Links Are Part of the Data Model

If a source mentions a concept, the source page must link to the concept and the concept page must link back to the source.

This applies to sources, concepts, entities, claims, projects, people, organizations, and synthesis pages.

### 3.6 Hybrid Retrieval Is Required from v0.1

Because the initial corpus is already 600 documents, the system must not rely only on `index.md` and agent file reading.

The system must support:

- Keyword search.
- Semantic/vector search.
- Graph traversal.
- Summary-first wiki navigation.
- LLM answer synthesis with citations.

### 3.7 Human Judgment Is Required for Destructive or Semantic Decisions

The system may suggest deletion, deprecation, contradiction resolution, and entity merging, but it must not execute those decisions without human approval.

---

## 4. Deployment Architecture

### 4.1 Runtime Split

```text
Windows 11 Host
├─ Obsidian
├─ Browser UI
├─ VS Code UI
├─ Optional watched inbox folder
└─ Mobile access through browser/PWA over local network or VPN

WSL2 Ubuntu
├─ System of record
├─ Backend API
├─ Extraction workers
├─ Agent workers
├─ Search indexes
├─ Vector index
├─ Graph metadata
├─ Databases
├─ Claude Code / Codex CLI
└─ Docker services
```

### 4.2 Canonical Repository Location

The repository root is:

```text
/home/<user>/code/knowledge-system
```

For the current development machine:

```text
/home/jolulop/code/knowledge-system
```

All commands and examples assume execution from this directory unless stated otherwise.

### 4.3 Reserved Ports

| Service | Port |
|---|---:|
| Knowledge System API | `18000` |
| Future MCP endpoint | `18001` |
| Future browser UI | `13000` |
| Future development UI | `15173` |

Avoid using these common ports unless explicitly required:

```text
8000, 8080, 3000, 5000, 5173
```

---

## 5. Repository Layout

```text
knowledge-system/
├─ raw/
│  ├─ inbox/
│  ├─ permanent/
│  ├─ ephemeral/
│  ├─ assets/
│  ├─ transcripts/
│  └─ manifests/
├─ normalized/
│  ├─ markdown/
│  ├─ chunks/
│  ├─ tables/
│  ├─ images/
│  └─ extraction_logs/
├─ wiki/
│  ├─ index.md
│  ├─ log.md
│  ├─ Sources/
│  ├─ Concepts/
│  ├─ Claims/
│  ├─ Entities/
│  ├─ People/
│  ├─ Organizations/
│  ├─ Projects/
│  ├─ Tags/
│  ├─ Synthesis/
│  └─ Queries/
├─ reviews/
│  ├─ pending/
│  ├─ approved/
│  ├─ rejected/
│  └─ audit_log/
├─ indexes/                     # ADR-0032 §7: derived & gitignored. keyword/keyword.sqlite
│  ├─ keyword/                  #   (FTS5), vector/ (LanceDB, Phase 4d), graph/ reserved.
│  ├─ vector/
│  └─ graph/
├─ db/                          # ADR-0032 §7 supersedes the line below: the keyword index
│  ├─ metadata.sqlite           #   moved to indexes/keyword/; db/ now holds jobs.sqlite,
│  └─ jobs.sqlite               #   graph.sqlite, llm_cache.sqlite (metadata.sqlite retired).
├─ app/
│  ├─ backend/
│  ├─ frontend/
│  └─ workers/
├─ scripts/
├─ policies/
├─ evals/
├─ .claude/
├─ CLAUDE.md
├─ AGENTS.md
├─ docker-compose.yml
├─ pyproject.toml
└─ README.md
```

---

## 6. Core Data Model

### 6.1 Node Types

| Node Type | Description |
|---|---|
| `source` | Original document or transcript. |
| `entity` | Named object or topic not necessarily a person/org. |
| `concept` | Abstract idea, pattern, framework, or recurring theme. |
| `claim` | Atomic factual statement extracted from one or more sources. |
| `project` | Internal project, initiative, or workstream. |
| `person` | Individual mentioned in sources. |
| `organization` | Company, institution, customer, vendor, agency, or group. |
| `tag` | Classification label. |
| `query` | Saved user question and answer. |
| `synthesis` | Higher-level analysis across multiple sources. |

### 6.2 Relationship Types

| Edge Type | Meaning |
|---|---|
| `mentions` | A source or page mentions a node. |
| `supports` | A source or claim supports another claim or synthesis. |
| `contradicts` | A claim conflicts with another claim or synthesis. |
| `supersedes` | A newer source or claim supersedes an older one. |
| `duplicates` | Two sources or nodes appear substantially equivalent. |
| `derived_from` | A wiki page, claim, or synthesis is derived from a source. |
| `related_to` | General semantic relation. |
| `needs_review` | The relationship or node requires human review. |

---

## 7. Document Ingestion Pipeline

```text
New file detected
        ↓
Create source manifest
        ↓
Compute checksum and deduplicate
        ↓
Extract text/assets/tables
        ↓
Normalize to Markdown/JSON
        ↓
Chunk using heading-aware strategy
        ↓
Generate summary, tags, entities, candidate claims
        ↓
Create/update Source page
        ↓
Create/update Claim pages
        ↓
Create/update Concept/Entity/Person/Organization/Project pages
        ↓
Update graph edges
        ↓
Update keyword/vector indexes
        ↓
Rebuild index.md
        ↓
Append log.md entry
        ↓
Create review items when confidence is low or changes are semantic/destructive
```

---

## 8. Search and Retrieval

### 8.1 Search Modes

| Search Mode | Purpose |
|---|---|
| Keyword search | Exact names, acronyms, file names, dates, quotes. |
| Semantic search | Conceptual similarity and fuzzy topic retrieval. |
| Graph traversal | Related sources, concepts, people, organizations, projects, claims. |
| Wiki navigation | `index.md`, summary callouts, backlinks, source/concept pages. |
| Hybrid answer synthesis | LLM-generated answer with citations from retrieved evidence. |

### 8.2 Query Routing

| Query Type | Retrieval Strategy |
|---|---|
| “What do I know about X?” | `index.md` → summary callouts → graph traversal → selected pages. |
| “How are X and Y related?” | Graph traversal + synthesis pages + selected evidence. |
| “Find exact quote/clause/number/date.” | Keyword search + vector search over chunks. |
| “Which documents mention X?” | Keyword + graph filters. |
| “Which sources disagree?” | Claim graph + contradiction edges. |
| “What changed recently?” | `log.md` + metadata DB. |
| “What should be archived?” | Retention metadata + stale-content agent. |

---

## 9. Agentic Architecture

### 9.1 Human-Facing Operations

| Operation | Description |
|---|---|
| `ingest` | Process new raw sources into normalized data, wiki pages, indexes, and review items. |
| `query` | Answer questions using wiki, search, graph, and citations. |
| `review` | Show pending semantic decisions requiring human approval. |
| `lint` | Check wiki health, graph health, stale content, contradictions, and citations. |

### 9.2 Autonomous Agents

| Agent | Cadence | Responsibility |
|---|---:|---|
| Watcher agent | Continuous or polling | Detect new files and create ingestion jobs. |
| Extraction agent | Daily / queued | Convert files to normalized text/assets/chunks. |
| Wiki compiler agent | Daily | Create/update source, concept, entity, claim, and synthesis pages. |
| Citation verifier | During ingest/query | Validate that generated claims cite real sources. |
| Graph curator | Daily/weekly | Suggest links, duplicates, merges, splits, and review items. |
| Lint agent | Weekly | Detect broken links, orphan nodes, stale stubs, missing metadata, uncited claims. |
| Contradiction agent | Weekly | Detect conflicting claims and create review items. |
| Stale-content agent | Monthly | Rank old documents and pages for archive/deprecation review. |
| Retention agent | Monthly | Apply retention policy and propose archive/delete candidates. |
| Backup agent | Daily/weekly | Snapshot raw manifests, DB (incl. graph), wiki, and policies. (ADR-0032 §7 supersedes "indexes": the keyword index is never backed up — cheap rebuild — and the vector index is opt-in only.) |
| Evaluation agent | Weekly | Run golden questions and citation/graph tests. |

### 9.3 Agent State

Agents must persist job state to disk/database.

No long-running ingest operation may depend on chat context as the only record of progress.

---

## 10. Deterministic Hooks and Validators

Agents may propose and write content, but deterministic scripts must enforce structural rules.

| Hook | Trigger | Action |
|---|---|---|
| Rebuild index | Source/Concept/Entity/Claim/Synthesis page changed | Rebuild `wiki/index.md`. |
| Validate frontmatter | Any wiki page changed | Ensure required fields exist. |
| Validate wikilinks | Any wiki page changed | Ensure wikilinks resolve to existing pages or create review item. |
| Validate citations | Any generated claim or answer | Ensure cited source exists. |
| Reindex changed file | Any normalized/wiki Markdown changed | Update keyword indexes. (ADR-0032 §7 / ADR-0033 §5 supersede "vector/graph": the per-file hook reindexes only the cheap keyword index; the **vector** index is refreshed by an explicit `reindex_vector.py` — embedding is GPU/latency-heavy and must not depend on the embedding server being up on every edit.) |

---

## 11. Human Review Workflow

The system must create review items for:

| Review Type | Requires Human Approval |
|---|---|
| Delete raw file | Always. |
| Archive raw file | Yes. |
| Deprecate wiki page | Yes. |
| Resolve contradiction | Yes. |
| Merge entities | Yes. |
| Split entity or concept | Yes. |
| Mark source as duplicate | Yes, except exact SHA duplicate. |
| Promote claim to concept | Optional if concept appears in two or more sources; otherwise yes. |
| Hide low-quality content | Yes. |

---

## 12. Retention and Archiving

### 12.1 Status Values

```text
active
stale_candidate
deprecated_candidate
archive_candidate
archived
delete_candidate
deleted
```

### 12.2 Default Policy

- Raw files are never automatically deleted.
- Documents older than 3 years become `archive_candidate`.
- Superseded documents become `deprecated_candidate`.
- Low-value ephemeral inbox items may become `delete_candidate` after a configurable period.
- Physical deletion requires human approval.
- Derived wiki pages may be regenerated or hidden after approval.
- Contradictory claims must remain visible until reviewed.
- Deprecated content should remain searchable unless explicitly hidden.

---

## 13. Security Requirements

- Default access: local machine only.
- Optional LAN access must require authentication.
- Mobile access should use VPN, Tailscale, WireGuard, or equivalent secure channel.
- No public exposure in v0.1.
- Imported documents are untrusted.
- Agents must treat source content as data, not commands.
- Agents must not obey instructions found inside imported documents.
- `.env` must not be committed to git.

---

## 14. Recommended Technology Stack

| Layer | Recommended v0.1 choice |
|---|---|
| Backend | Python + FastAPI + Pydantic |
| Project environment | `uv` + Python 3.12.x |
| Metadata DB | SQLite |
| Job DB | SQLite |
| Keyword search | SQLite FTS5 first; upgrade later if needed |
| Vector store | LanceDB or ChromaDB |
| Graph store | SQLite graph tables first |
| Frontend | Browser app; React or server-rendered UI |
| Development assistant | Claude Code |
| Compatibility agent | Codex through `AGENTS.md` |
| UI/editor | VS Code WSL + optional Obsidian |

---

## 15. API Requirements

Initial API endpoints:

```text
GET    /health
GET    /sources
GET    /sources/{source_id}
POST   /sources/upload
POST   /sources/rescan
GET    /wiki/pages
GET    /wiki/pages/{page_id}
POST   /wiki/pages/{page_id}/validate
GET    /search
POST   /query
GET    /graph/node/{node_id}
GET    /graph/neighborhood/{node_id}
GET    /reviews
POST   /reviews/{review_id}/approve
POST   /reviews/{review_id}/reject
POST   /reviews/{review_id}/defer
POST   /jobs/ingest
POST   /jobs/lint
POST   /jobs/reindex
POST   /jobs/stale-check
GET    /jobs
GET    /jobs/{job_id}
GET    /evals/results
POST   /evals/run
```

Note: this is the original v0.1 API *target*; the phase ADRs supersede it where the implementation
diverged (the spec is kept as historical intent, not rewritten):
- **Ingest surface** — `POST /sources/upload`, `POST /sources/rescan`, and `POST /jobs/ingest` were
  superseded by the file-drop intake model: drop into `raw/inbox/` then `POST /jobs/intake-scan` →
  `/jobs/extract` → `/jobs/generate-wiki` (ADR-0002/0009/0011). No HTTP upload endpoint exists (uploads
  would cross the loopback-only, no-auth boundary — ADR-0009).
- **Eval surface** — `POST /evals/run` + `GET /evals/results` are **implemented** (ADR-0042, the
  deterministic real-vault answer-quality eval that closed the ADR-0036 decision-14 deferral):
  key-required, loopback-only, cost-gated (`confirm_cost`/`dry_run`/hard-cap), read-only over vault SoT,
  scoring the `POST /query` cited answers against a curated **gitignored** local corpus
  (`evals/golden_answers.local.yaml`; committed `…example.yaml` schema). `evals/golden_questions.yaml`
  remains the fake-adapter CI fixture (structural regression, key-free). An LLM-as-judge "analysis lane",
  scheduled runs, and baseline-diff gating are the remaining out-of-scope items.

---

## 16. Evaluation and Success Tests

v0.1 is successful when:

- 600 initial documents can be ingested in batches.
- At least 95% of successfully parsed sources receive Source pages.
- Every Source page has required frontmatter and summary callout.
- Every generated claim has at least one citation or is marked unsourced.
- Query answers include citations.
- `index.md` rebuilds deterministically.
- `log.md` records ingests, queries, lint passes, and review actions.
- Keyword search works.
- Semantic search works.
- Graph neighborhood browsing works.
- Review UI supports approval/rejection/defer.
- Deletion and entity merges cannot happen without approval.
- Weekly lint produces actionable reports.
- Monthly stale check identifies archive candidates.
- At least 20 golden questions run automatically in CI. Runtime `/evals/run` (real-vault
  answer-quality eval) shipped in ADR-0042, closing the ADR-0036 decision-14 deferral.

---

## 17. Build Phases

Phase 3 was split into a deterministic backbone (Phase 3) and an LLM-dependent semantic
layer (Phase 3.5) per [ADR-0013](adr/0013-phase-3-deterministic-wiki-backbone.md), so
that all offline/deterministic work is complete and tested before any LLM, API key, or
prompt-injection surface is introduced.

| Phase | Goal | Status |
|---|---|---|
| Phase 0 | Architecture spike and scaffold. | Complete |
| Phase 0.5 | Development environment validation. | Complete |
| Phase 1 | File Intake and Raw Repository. | Complete |
| Phase 2 | Extraction and Normalization. | Complete |
| Phase 3 | Filing-Cabinet Wiki Layer — deterministic Source-page backbone. | Complete |
| Phase 3.5 | LLM semantic layer: enriched summaries, tags, concepts, entities, claims, synthesis, and bidirectional backlinks. Sub-phased 3.5a/3.5b/3.5c (ADR-0028). | **Complete** — 3.5a (per-source LLM summary + tags), 3.5b (grounding gate, graph store, claim/concept-entity extraction, promotion lifecycle), 3.5c (contradiction detection + supersede executor + cross-source synthesis). |
| Phase 4 | Search and Graph. | **Complete** — 4a keyword/nav index, 4b graph read API, 4c router + GET /search, 4d LanceDB vector channel, 4e RRF hybrid fusion + retrieval evals (ADR-0032/0033). |
| Phase 5 | Query and Cited Answering. | **Complete** — 5-1 answer-synthesis core, 5-2 POST /query, 5-3 saved Queries pages, 5-4 golden-question eval harness (ADR-0034). |
| Phase 6 | Human Review UI. | **Complete** (ADR-0035): server-rendered HTML over a deterministic JSON review read model; type-complete record-only decision ledger; deterministic `POST /reviews/apply` (synthesis/promotion/contradiction + scoped deprecation/archive); loopback-only; key-free tests. Extended by the **apply dry-run preview** (ADR-0040) and the first non-rekeying **governance executor** `mark_semantic_duplicate` (ADR-0041). |
| Phase 7 | Autonomous Maintenance. | **Complete** — per ADR-0036: `/jobs/lint`, `/jobs/reindex`, `/jobs/stale-check`, reversible `archive_source`, cache-purge candidate detection, cron/no-daemon operations docs; lint quality heuristics (ADR-0037); backup/restore durability (ADR-0039). The real-vault answer-quality eval `/evals/run` shipped in **ADR-0042** (the decision-14 deferral is closed). |
| Phase 8 | Mobile and Hardening (auth / CSRF / API-worker). | **Deferred** — no concrete non-loopback exposure requirement exists; the app is loopback-only (ADR-0009). Picked up only when an exposure path is on the table. |

---

## 18. Current Environment Acceptance Status

Known completed setup items:

- WSL2 configured.
- Repository located at `/home/jolulop/code/knowledge-system`.
- Python 3.12 pinned through `uv`.
- Virtual environment created.
- Dependencies installed.
- Scaffold validators passed.
- VS Code connected to WSL.
- Claude Code panel working.

Before Phase 1, commit the clean scaffold state.

---

## 19. v0.1 Decision Summary

The system will be built as:

```text
LLM Wiki pattern
+ structured filing-cabinet wiki layer
+ deterministic index/hooks
+ summary-first navigation
+ bidirectional backlinks
+ hybrid keyword/vector/graph search from day one
+ human review workflow
+ retention governance
+ WSL2 backend
+ Windows browser/Obsidian UI
+ Claude Code for development and supervised maintenance
+ API workers for future scheduled autonomous processing
```
