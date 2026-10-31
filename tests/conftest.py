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

from app.backend import db, embeddings


@pytest.fixture(autouse=True)
def _no_embedding_warmup(monkeypatch):
    monkeypatch.setattr(embeddings, "warmup_provider", lambda settings: None)


@pytest.fixture(autouse=True)
def _test_client_host(monkeypatch):
    """Starlette's TestClient sends `Host: testserver`; the request-origin guard (ADR-0067) only
    accepts loopback names plus KS_ALLOWED_HOSTS. The guard's own tests clear this."""
    monkeypatch.setenv("KS_ALLOWED_HOSTS", "testserver")


@pytest.fixture(autouse=True)
def _own_llm_attribution():
    """Each test starts with no current job (ADR-0069 ledger attribution), as each CLI process and
    each API request does: a test that interrupts a producer leaves its job on this session's
    attribution stack, which would otherwise leak into later tests."""
    token = db._JOB_STACK.set(())
    yield
    db._JOB_STACK.reset(token)
