#!/usr/bin/env python3
"""Validate required frontmatter and summary callouts for wiki pages."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# Single source of truth for the PAGE review_status set (ADR-0022): gate exactly what the renderer emits,
# so producer and validator can't drift. `deferred` is deliberately absent — it is a review-ledger state.
from app.workers.wiki_render import REVIEW_STATUSES as PAGE_REVIEW_STATUSES  # noqa: E402

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
WIKI_SUBDIRS = ["Sources", "Items", "Claims", "Tags", "Synthesis", "Queries"]
VALID_TYPES = {"source", "item", "claim", "tag", "synthesis", "query"}
# Field contracts per page type (ADR-0016/0020/0021/0022). Source pages use
# relative_raw_path (never absolute raw_path) and the shared lifecycle fields.
REQUIRED_BY_TYPE = {
    # ADR-0060: `aliases` is required on every id-titled family (the Obsidian quick-switcher
    # matches filenames and aliases, not `title:`) and Claims gain a display-only `title:` —
    # the hard backstop for the search-surface half of the display-alias contract.
    "source": [
        "type", "source_id", "title", "aliases", "relative_raw_path", "normalized_path",
        "sha256", "file_type", "status", "ingestion_status", "summary_status",
        "generation_status", "input_fingerprint",
    ],
    # `review_status` is required on every page type that renders it (ADR-0022) — NOT Source, which
    # intentionally does not carry it (its review state lives in the ledger, owned by no renderer).
    "item": ["type", "item_id", "item_type", "title", "status", "confidence", "review_status"],
    "claim": ["type", "claim_id", "title", "aliases", "status", "confidence", "review_status"],
    "synthesis": ["type", "synthesis_id", "title", "aliases", "status", "review_status"],
    # No wall-clock fields: a saved Query page is a deterministic derived artifact (ADR-0023/0034),
    # like claim/synthesis pages — byte-stable, so no `created`/`last_compiled_at`.
    "query": ["type", "query_id", "title", "aliases", "question", "status", "review_status"],
}


def parse_frontmatter(text: str) -> dict[str, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    data: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"\'')
    return data


def has_summary(text: str) -> bool:
    return any(line.strip().startswith("> [!summary]") for line in text.splitlines())


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0]).resolve() if argv else Path.cwd()
    wiki = root / "wiki"
    errors: list[str] = []
    for subdir in WIKI_SUBDIRS:
        folder = wiki / subdir
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            fm = parse_frontmatter(text)
            rel = path.relative_to(root)
            if not fm:
                errors.append(f"{rel}: missing frontmatter")
                continue
            page_type = fm.get("type")
            if page_type not in VALID_TYPES:
                errors.append(f"{rel}: invalid or missing type: {page_type!r}")
            required = REQUIRED_BY_TYPE.get(page_type or "", ["type", "title"])
            for key in required:
                if key not in fm or fm[key] == "":
                    errors.append(f"{rel}: missing required frontmatter field `{key}`")
            # review_status contract (ADR-0022): if present it must be in the PAGE set; `deferred` is a
            # review-ledger state, never a page value. Source pages must not carry the field at all.
            rs = fm.get("review_status")
            if rs and rs not in PAGE_REVIEW_STATUSES:
                errors.append(f"{rel}: review_status {rs!r} not in {sorted(PAGE_REVIEW_STATUSES)} "
                              "(ADR-0022; `deferred` is a review-ledger state, not a page value)")
            if page_type == "source" and "review_status" in fm:
                errors.append(f"{rel}: Source pages must not carry review_status "
                              "(review state lives in the ledger, owned by no renderer; ADR-0022)")
            if not has_summary(text):
                errors.append(f"{rel}: missing > [!summary] callout")

    if errors:
        print("Frontmatter validation failed:")
        for err in errors:
            print(f"- {err}")
        return 1
    print("Frontmatter validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
