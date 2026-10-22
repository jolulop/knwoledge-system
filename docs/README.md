# Knowledge System Documentation

This folder contains the working documentation for the local-first agentic information management system.

**Current implementation snapshot:** Phases 1-7 are complete; post-Phase-7 hardening through
ADR-0063 is implemented. The current semantic ontology is ADR-0059 knowledge items:
one flat `wiki/Items/` family with stable `itm_` ids and governed `item_type` metadata. Older
planning docs that mention Concepts/Entities/People/Organizations/Projects are historical unless
they explicitly carry an ADR-0059 overlay.

## Documents

1. **Build Specification v0.1.md**
   Product and technical build specification. It preserves the original v0.1 target and carries
   implementation-status overlays where later ADRs changed the shape.

2. **Environment Setup v0.1.md**
   Developer workstation setup for Windows 11 + WSL2 Ubuntu + VS Code + Claude Code/Codex,
   including the current BGE-M3 embedding overlay.

3. **Architecture Overview v0.1.md**
   High-level architecture, design principles, runtime split, data flow, and retrieval model,
   updated with the current Items taxonomy and implemented retrieval/review surfaces.

4. **Workflow.md**
   Practical operator guide for day-to-day ingest, enrichment, review, and query workflows.

5. **Operations.md**
   Maintenance and runtime operations guide.

6. **UAT Guide.md**
   Thin user-acceptance checklist — disposable-vault by default, scope-checked review apply; links
   to Workflow.md/Operations.md for canonical behavior.

7. **Local Models.md**
   How to route enrichment/query tiers to a local OpenAI-compatible model server with hosted fallback.

8. **Phase plan files**
   Historical implementation plans for Phases 1-7 and the Phase 4d/4e retrieval slices. They are
   retained as design trail; status blocks call out completed work and later ADR supersession.

9. **adr/**
   Architecture Decision Records. The high-numbered ADRs are the most recent overlays; ADR-0059
   supersedes the old concept/entity ontology, ADR-0060 adds display aliases, ADR-0061 hardens
   prompt encoding, ADR-0062 adds `item_type` retrieval faceting, and ADR-0063 adds model chains.

## Current working assumptions

- Repository root: `~/code/knowledge-system`
- Primary runtime: WSL2 Ubuntu
- Primary UI: Browser app and optional Obsidian
- Primary development assistants: Claude Code and Codex-compatible agents
- Production autonomous processing: API-based workers, not interactive Claude Code sessions
- API port: `18000`
- Future MCP port: `18001`
- Future web UI port: `13000` (current review UI is server-rendered inside the FastAPI app)
- Project Python: Python `3.12.x`, pinned with `uv`
- Embedding backend: code default is unconfigured `local_http`; recommended GPU `.env` selects
  in-process **FlagEmbedding + PyTorch CUDA** running **BAAI/bge-m3** (dense, dim 1024) on the
  RTX 5090 (ADR-0053). The TEI/`local_http` HTTP embedder is a CPU-fallback option. Setup + smoke:
  see *Environment Setup v0.1.md* §14.1.
