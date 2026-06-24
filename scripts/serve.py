#!/usr/bin/env python3
"""Convenience launcher for the API — delegates to the canonical ``python -m app.backend`` entrypoint.

Binds Uvicorn to ``settings.app_host`` through the import-time ``assert_safe_bind`` guard, so the bind
can't drift from the loopback check (ADR-0009). Direct ``uvicorn ... --host 0.0.0.0`` is unsupported.

Usage: ``uv run python scripts/serve.py``
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend.__main__ import main

if __name__ == "__main__":
    main()
