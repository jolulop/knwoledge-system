#!/usr/bin/env python3
"""Run every validator and exit non-zero if any of them fail.

This is the single entry point for the lint/maintenance pass: it discovers all
``scripts/validate_*.py`` checks (except this runner), runs each against the given
project root, and aggregates their exit codes. Adding a new ``validate_*.py`` script
wires it into the suite automatically — no edit here is required.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SELF = Path(__file__).resolve().name


def discover_validators() -> list[Path]:
    return sorted(p for p in SCRIPTS_DIR.glob("validate_*.py") if p.name != SELF)


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    validators = discover_validators()
    if not validators:
        print("No validators found.")
        return 0

    failed: list[str] = []
    for script in validators:
        print(f"\n=== {script.name} ===", flush=True)
        result = subprocess.run([sys.executable, str(script), str(root)])
        if result.returncode != 0:
            failed.append(script.name)

    print("\n=== Validation summary ===")
    print(f"Ran {len(validators)} validators: {', '.join(p.name for p in validators)}")
    if failed:
        print(f"FAILED ({len(failed)}): {', '.join(failed)}")
        return 1
    print("All validators passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
