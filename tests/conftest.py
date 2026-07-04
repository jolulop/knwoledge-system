"""Shared pytest fixtures.

The app reads the real project ``.env`` at import (module-level ``settings``), which may select the
in-process FlagEmbedding backend (``EMBEDDING_PROVIDER=flagembedding_bge_m3``). No test should load the
~2 GB BGE-M3 model at app startup (ADR-0053) — app-boot tests must stay fast and GPU-independent — so we
neutralize the startup warmup everywhere. The real warmup is validated separately by the opt-in GPU
smoke (``tests/test_flagembedding_provider.py`` + ``scripts/check_embedding.py``).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import embeddings


@pytest.fixture(autouse=True)
def _no_embedding_warmup(monkeypatch):
    monkeypatch.setattr(embeddings, "warmup_provider", lambda settings: None)
