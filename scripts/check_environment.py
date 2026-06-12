#!/usr/bin/env python3
"""Check the local environment is ready: Python, working dir, .env, core folders."""
from __future__ import annotations

import sys
from pathlib import Path

REQUIRED_DIRS = ["raw", "wiki", "normalized"]


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()

    print(f"Python version: {sys.version.split()[0]}")
    print(f"Working directory: {Path.cwd()}")
    print(f"Project root: {root}")

    errors: list[str] = []

    env_path = root / ".env"
    if env_path.exists():
        print(f"OK   .env present: {env_path}")
    else:
        errors.append(f".env missing: {env_path}")

    for name in REQUIRED_DIRS:
        path = root / name
        if path.is_dir():
            print(f"OK   {name}/ present")
        else:
            errors.append(f"{name}/ directory missing: {path}")

    if errors:
        print("Environment check failed:")
        for err in errors:
            print(f"- {err}")
        return 1
    print("Environment check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
