# Environment Setup v0.1
## Windows 11 + WSL2 + VS Code + Claude Code

**Status:** Working setup document  
**Repository root:** `~/code/knowledge-system`  
**Current machine path:** `/home/jolulop/code/knowledge-system`

---

## 1. Environment Goal

The development environment must support:

- Local-first backend development.
- Document ingestion and indexing tools.
- Claude Code and Codex-compatible repository workflows.
- VS Code connected to WSL.
- Docker Desktop with WSL2 integration.
- Future GPU-enabled local AI tooling.

The environment uses Windows for the graphical tools and WSL2 Ubuntu for the runtime.

---

## 2. Runtime Split

```text
Windows 11 Host
├─ VS Code UI
├─ Claude Code extension UI
├─ Obsidian
├─ Browser
├─ Docker Desktop UI
└─ Optional Windows drop folder

WSL2 Ubuntu
├─ Repository root
├─ Python and uv
├─ Claude Code CLI
├─ Codex CLI if used
├─ Git
├─ Docker CLI integration
├─ Backend app
├─ Workers
├─ Scripts
└─ Indexes and local DB files
```

---

## 3. Repository Location

The repository must live under the WSL home directory, not under `/mnt/c/...`.

Recommended path:

```bash
~/code/knowledge-system
```

Current path:

```bash
/home/jolulop/code/knowledge-system
```

Reason:

- Faster Linux-native file I/O.
- Fewer permission issues.
- Better file watching behavior.
- Cleaner Docker and Python workflows.
- Better compatibility with Claude Code and shell tooling.

---

## 4. Required Windows Tools

Install on Windows:

- VS Code.
- Docker Desktop.
- Obsidian, optional but recommended.
- NVIDIA Windows driver with WSL CUDA support.
- Git for Windows, optional because Git in WSL is the primary tool.

Obsidian is optional for development. The system of record is the WSL repository and future browser backend.

---

## 5. Required WSL Packages

Install base tools inside WSL:

```bash
sudo apt update && sudo apt upgrade -y

sudo apt install -y \
  build-essential \
  curl \
  wget \
  git \
  git-lfs \
  unzip \
  zip \
  ca-certificates \
  gnupg \
  lsb-release \
  make \
  pkg-config \
  libssl-dev \
  sqlite3 \
  libsqlite3-dev \
  ripgrep \
  fd-find \
  jq \
  tree \
  htop \
  poppler-utils \
  tesseract-ocr \
  tesseract-ocr-eng \
  tesseract-ocr-spa \
  pandoc \
  ffmpeg \
  libmagic1
```

---

## 6. Python Policy

System Python is not the project Python.

The project must use:

```text
Python 3.12.x
```

managed through:

```text
uv
```

Correct verification command:

```bash
cd ~/code/knowledge-system
uv run python --version
```

Do not rely on:

```bash
python3 --version
```

That shows the system Python and may be a different version.

---

## 7. uv Setup

Install or verify `uv`:

```bash
uv --version
```

Pin Python 3.12:

```bash
cd ~/code/knowledge-system
uv python install 3.12
uv python pin 3.12
uv sync
uv run python --version
```

Expected:

```text
Python 3.12.x
```

Known working result:

```text
Using CPython 3.12.13
Creating virtual environment at: .venv
Dependencies installed successfully
```

---

## 8. .env File

The `.env` file lives at:

```bash
~/code/knowledge-system/.env
```

Recommended development values:

```env
KNOWLEDGE_SYSTEM_HOME=/home/jolulop/code/knowledge-system

ANTHROPIC_API_KEY=
OPENAI_API_KEY=

APP_HOST=127.0.0.1
APP_PORT=18000
```

API keys may remain empty during development if Claude Code is authenticated through user login.

Protect the file:

```bash
chmod 600 .env
```

`.env` must not be committed to Git.

---

## 9. Port Policy

Reserved project ports:

| Service | Port |
|---|---:|
| Knowledge System API | `18000` |
| Future MCP endpoint | `18001` |
| Future Web UI | `13000` |
| Future dev UI | `15173` |

Avoid default development ports already used by other projects:

```text
8000, 8080, 3000, 5000, 5173
```

---

## 10. VS Code WSL Setup

Open the project from WSL:

```bash
cd ~/code/knowledge-system
code .
```

Verify the bottom-left corner of VS Code shows:

```text
WSL: Ubuntu-24.04
```

or equivalent.

Use the VS Code integrated terminal as the primary development terminal.

---

## 11. Claude Code Setup

Claude Code is fundamentally a terminal application.

Run it inside WSL:

```bash
cd ~/code/knowledge-system
claude
```

The VS Code extension is a UI around Claude Code. It should be installed into the WSL VS Code environment.

Recommended check:

```bash
which claude
claude --version
```

Recommended first Claude Code prompt:

```text
Read CLAUDE.md and AGENTS.md. Summarize the repository structure and explain the purpose of each top-level directory.
```

---

## 12. Claude Code Role

Claude Code is used for:

- Development.
- Code editing.
- Repository-wide refactoring.
- Running scripts.
- Creating tests.
- Updating documentation.
- Manual or supervised ingest/query/lint workflows.

Claude Code is not required to be the unattended scheduled document-processing runtime.

Future scheduled document processing should run through backend workers and API-based LLM calls.

---

## 13. Docker Check

Verify Docker Desktop WSL integration:

```bash
docker version
docker run hello-world
```

If this fails, verify Docker Desktop settings:

```text
Settings → General → Use WSL 2 based engine
Settings → Resources → WSL Integration → Enable Ubuntu
```

---

## 14. GPU Check

Verify GPU visibility from WSL:

```bash
nvidia-smi
```

The GPU is not required for Phase 1, but it will be useful later for:

- Local embeddings.
- OCR/image models.
- Local rerankers.
- Local LLM experiments.

### 14.1 Embedding backend — BGE-M3 on CUDA (ADR-0053)

The default GPU embedding backend is **in-process FlagEmbedding + PyTorch CUDA** running **BAAI/bge-m3**
(`EMBEDDING_PROVIDER=flagembedding_bge_m3`). We moved off the TEI/Candle HTTP embedder because **TEI/Candle
falls back to CPU on the RTX 5090** even though its container sees the GPU via `nvidia-smi`; PyTorch +
FlagEmbedding runs BGE-M3 on CUDA in-process. The old TEI (`local_http`) seam remains supported as an
**optional CPU fallback only** (ADR-0033), never the default.

**Install (uv, Python 3.12).** `torch` must come from the **CUDA 12.8** wheel index — a plain PyPI resolve
pulls a CPU/other-CUDA build. Do **not** install `torchvision`/`torchaudio` (they caused a
`torchvision::nms` import error and are not needed):

```bash
cd ~/code/knowledge-system
source .venv/bin/activate

uv pip uninstall -y torchvision torchaudio          # ensure they are absent

uv pip install torch --index-url https://download.pytorch.org/whl/cu128
uv pip install -U FlagEmbedding sentence-transformers transformers accelerate
```

This GPU stack is a **local accelerator overlay installed out-of-lock** — it is intentionally **not** a
`pyproject`/`uv.lock` dependency group (a cu128 Torch build can't be locked from PyPI without pinning the whole
lock to one GPU platform). The commands above are the source of truth; `uv sync` for the normal ingest/review/
test workflows stays portable and Torch-free.

PyTorch's official installer says to select the CUDA compute-platform suited to the machine and use the
generated wheel command; CUDA 12.8 (`cu128`) is one of the available options and matches this box.

**Verify torch + CUDA:**

```bash
uv run python - <<'PY'
import torch
print("torch:", torch.__version__)
print("cuda available:", torch.cuda.is_available())
print("cuda version:", torch.version.cuda)
print("device:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)
PY
```

**BGE-M3 dense smoke** (expect `dense_vecs shape: (3, 1024)`):

```bash
uv run python - <<'PY'
from FlagEmbedding import BGEM3FlagModel
model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=True, device="cuda")
out = model.encode(
    ["hello world", "hola mundo", "semantic search over enterprise documents"],
    batch_size=3, max_length=8192,
    return_dense=True, return_sparse=False, return_colbert_vecs=False,
)
print("dense_vecs shape:", out["dense_vecs"].shape)
PY
```

Or, the repeatable project entry point (reads the `EMBEDDING_*` config, fails fast if CUDA is requested but
unavailable, prints the same shape; `--json` emits a health block):

```bash
uv run python scripts/check_embedding.py            # → dense_vecs shape: (3, 1024)
```

When `EMBEDDING_PROVIDER=flagembedding_bge_m3` and `EMBEDDING_DEVICE=cuda`, the FastAPI app **loads BGE-M3
once at startup and fails fast** if CUDA/model load fails; non-vector app roles (ingest/review/lint) never
import Torch. `EMBEDDING_CACHE_DIR` is unset by default → the HuggingFace cache (`~/.cache/huggingface`) is
used; the repo `.cache/` here is root-owned (a docker leftover) so point a repo-local cache elsewhere only
after `chown`-ing it.

---

## 15. Scaffold Validation Commands

From the repository root:

```bash
cd ~/code/knowledge-system

uv run python scripts/rebuild_index.py .
uv run python scripts/validate_frontmatter.py .
uv run python scripts/validate_wikilinks.py .
uv run python scripts/validate_citations.py .
```

Known successful output:

```text
rebuilt /home/jolulop/code/knowledge-system/wiki/index.md with 4 pages
Frontmatter validation passed.
Wikilink validation passed.
Citation validation passed.
```

---

## 16. Git Checkpoint

Before Phase 1 begins, commit the clean environment/scaffold state:

```bash
cd ~/code/knowledge-system

git status
git add .
git commit -m "Environment ready and scaffold validated"
```

Recommended `.gitignore` items:

```gitignore
.env
.venv/
__pycache__/
*.pyc
indexes/vector/
indexes/keyword/
raw/assets/
backups/
*.sqlite
*.db
```

---

## 17. Environment Acceptance Checklist

The environment is ready when all of these are true:

- [x] WSL2 is installed.
- [x] Repository is under `/home/jolulop/code/knowledge-system`.
- [x] Python 3.12 is pinned with `uv`.
- [x] `uv sync` succeeds.
- [x] `.venv` is created.
- [x] `.env` exists and uses `APP_PORT=18000`.
- [x] `rebuild_index.py` succeeds.
- [x] Frontmatter validation passes.
- [x] Wikilink validation passes.
- [x] Citation validation passes.
- [x] VS Code opens in WSL mode.
- [x] Claude Code panel works.
- [ ] Docker check passes.
- [ ] GPU check passes.
- [ ] Git checkpoint commit exists.

---

## 18. Troubleshooting Notes

### Obsidian winget installer error

If the Obsidian installer fails through WinGet, bypass WinGet and install Obsidian manually from the official installer. Obsidian is optional for Phase 1.

### Python 3.14 appears as system Python

Do not remove system Python just because `python3 --version` shows Python 3.14.

The project uses:

```bash
uv run python --version
```

### Wrong repository nesting

Correct structure:

```text
/home/jolulop/code/knowledge-system
├─ .claude/
├─ .env
├─ .git/
├─ AGENTS.md
├─ CLAUDE.md
├─ app/
├─ raw/
├─ scripts/
└─ wiki/
```

There should not be an extra nested `knowledge-system-scaffold-v0.1/` folder.
