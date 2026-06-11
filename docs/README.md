# Knowledge System Documentation v0.1

This folder contains the working documentation for the local-first agentic information management system.

## Documents

1. **Build Specification v0.1.md**  
   Product and technical build specification. This is the main system contract.

2. **Environment Setup v0.1.md**  
   Developer workstation setup for Windows 11 + WSL2 Ubuntu + VS Code + Claude Code.

3. **Architecture Overview v0.1.md**  
   High-level architecture, design principles, runtime split, data flow, and retrieval model.

4. **Phase 1 Plan.md**  
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
