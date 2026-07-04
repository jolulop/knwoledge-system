# Knowledge System Documentation v0.1

This folder contains the working documentation for the local-first agentic information management system.

## Documents

1. **Build Specification v0.1.md**  
   Product and technical build specification. This is the main system contract.

2. **Environment Setup v0.1.md**  
   Developer workstation setup for Windows 11 + WSL2 Ubuntu + VS Code + Claude Code.

3. **Architecture Overview v0.1.md**  
   High-level architecture, design principles, runtime split, data flow, and retrieval model.

4. **Workflow.md**  
   Practical operator guide for day-to-day ingest, enrichment, review, and query workflows.

5. **Operations.md**  
   Maintenance and runtime operations guide.

6. **UAT Guide.md**  
   Thin user-acceptance checklist — disposable-vault by default, scope-checked review apply; links
   to Workflow.md/Operations.md for canonical behavior.

7. **Phase 1 Plan.md**  
   Implementation plan for File Intake and Raw Repository.

## Current working assumptions

- Repository root: `~/code/knowledge-system`
- Primary runtime: WSL2 Ubuntu
- Primary UI: Browser app and optional Obsidian
- Primary development assistant: Claude Code
- Production autonomous processing: API-based workers, not interactive Claude Code sessions
- API port: `18000`
- Future MCP port: `18001`
- Future web UI port: `13000`
- Project Python: Python `3.12.x`, pinned with `uv`
- Embedding backend: in-process **FlagEmbedding + PyTorch CUDA** running **BAAI/bge-m3** (dense, dim 1024)
  on the RTX 5090 (ADR-0053); the TEI/`local_http` HTTP embedder is a CPU-fallback option only. Setup +
  smoke: see *Environment Setup v0.1.md* §14.1.
