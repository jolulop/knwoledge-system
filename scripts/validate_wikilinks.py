#!/usr/bin/env python3
"""Validate Obsidian-style wikilinks in wiki pages."""
from __future__ import annotations

import re
import sys
from pathlib import Path

LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
WIKI_SUBDIRS = ["Sources", "Items", "Claims", "Tags", "Synthesis", "Queries"]


def normalize_target(raw: str) -> str:
    # Drop alias and heading, e.g. [[Items/foo#Bar|alias]] -> Items/foo
    target = raw.split("|", 1)[0].split("#", 1)[0].strip()
    return target


def build_stem_index(wiki_root: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for path in wiki_root.rglob("*.md"):
        if path.name in {"index.md", "log.md"}:
            continue
        index.setdefault(path.stem, []).append(path)
        index.setdefault(path.relative_to(wiki_root).with_suffix("").as_posix(), []).append(path)
    return index


def resolve_link(target: str, wiki_root: Path, stem_index: dict[str, list[Path]]) -> bool:
    if not target:
        return True
    direct = wiki_root / f"{target}.md"
    if direct.exists():
        return True
    return target in stem_index and len(stem_index[target]) >= 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0]).resolve() if argv else Path.cwd()
    wiki_root = root / "wiki"
    stem_index = build_stem_index(wiki_root)
    errors: list[str] = []

    for subdir in WIKI_SUBDIRS:
        folder = wiki_root / subdir
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in LINK_RE.finditer(text):
                target = normalize_target(match.group(1))
                if not resolve_link(target, wiki_root, stem_index):
                    errors.append(f"{path.relative_to(root)}: broken wikilink [[{match.group(1)}]]")

    if errors:
        print("Wikilink validation failed:")
        for err in errors:
            print(f"- {err}")
        return 1
    print("Wikilink validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
