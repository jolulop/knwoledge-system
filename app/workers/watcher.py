#!/usr/bin/env python3
"""Polling watcher placeholder for raw/inbox.

Later phases should turn this into a proper queued worker. For now it prints files
that would be ingested.
"""
from __future__ import annotations

import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INBOX = ROOT / "raw" / "inbox"


def main() -> None:
    seen: set[Path] = set()
    print(f"watching {INBOX}")
    while True:
        for path in INBOX.glob("*"):
            if path.is_file() and path not in seen:
                seen.add(path)
                print(f"new file detected: {path}")
        time.sleep(5)


if __name__ == "__main__":
    main()
