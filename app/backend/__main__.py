#!/usr/bin/env python3
"""Blessed launch entrypoint: ``python -m app.backend`` (or ``uv run python -m app.backend``).

This is the ONLY supported way to serve the app. It binds Uvicorn to ``settings.app_host`` — the *same*
host that ``assert_safe_bind`` validates when ``app.backend.main`` is imported — so the loopback safety
guard can never drift from the interface actually bound (ADR-0009).

Running ``uvicorn app.backend.main:app --host 0.0.0.0`` directly is UNSUPPORTED: uvicorn's ``--host``
overrides the bind *without* re-checking the guard, so it can expose the unauthenticated API on a
non-loopback interface even though the import-time check saw ``APP_HOST=127.0.0.1``.
"""
from __future__ import annotations

import os

import uvicorn

from app.backend.config import get_settings


def main() -> None:
    settings = get_settings()
    # Importing main runs assert_safe_bind(settings.app_host, KS_ALLOW_INSECURE_BIND) at module load,
    # so a non-loopback host without the override aborts here before uvicorn binds anything.
    from app.backend.main import app
    # KS_RELOAD=1 enables hot-reload (e.g. the dev container): uvicorn reload re-imports the app in a
    # worker subprocess, so assert_safe_bind still runs there. Reload needs the import-string target.
    reload = os.environ.get("KS_RELOAD") == "1"
    target = "app.backend.main:app" if reload else app
    uvicorn.run(target, host=settings.app_host, port=settings.app_port, reload=reload)


if __name__ == "__main__":
    main()
