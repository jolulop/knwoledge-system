#!/usr/bin/env python3
"""Validate display aliases on generated wiki links (ADR-0060 — alias SHAPE only).

Every wikilink in a generated page body (the wiki page families plus index.md) whose
TARGET page exists and has a resolvable display label must carry a non-empty alias:
`[[Claims/clm_x|Readable claim]]`. A bare link is legal only when no label resolves;
a missing target stays validate_wikilinks' dangling-link failure (never double-reported
here); alias == current label is NOT required — that drift is the report-only
`display_alias_rot` lint check, so a retitle never fails this validator.

Label resolution is deterministic and page-local, mirroring app/workers/labels.py:
the target page's frontmatter `title:`; a Claim page without one derives the label
from `claim_text` exactly as the renderer does. Frontmatter and fenced code blocks in
the REFERRING page are excluded from the scan; `wiki/log.md` is out of scope
(append-only audit surface). Dependency-free by design, like the other validators.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
# ADR-0060 scope exactly: the five generated page families + index.md. Tags is deliberately
# absent (out of this ADR's scope — validate_wikilinks still covers its link integrity).
WIKI_SUBDIRS = ["Sources", "Items", "Claims", "Synthesis", "Queries"]
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
FENCE_RE = re.compile(r"(?ms)^```.*?^```[ \t]*$")
UNESCAPE_RE = re.compile(r"\\(.)")
SENTENCE_RE = re.compile(r"[.!?]")
WS_RE = re.compile(r"\s+")


def _fm_scalar(text: str, key: str) -> str:
    """A frontmatter scalar, quoted or bare — mirrors wiki_render.parse_frontmatter's value
    handling (labels._page_label's resolver) so producer and validator agree by construction."""
    match = FRONTMATTER_RE.match(text)
    if not match:
        return ""
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, value = line.split(":", 1)
        if k.strip() == key:
            raw = value.split("  #", 1)[0].strip().strip("\"'")
            return UNESCAPE_RE.sub(r"\1", raw).strip()
    return ""


def _claim_title(claim_text: str) -> str:
    # Local copy of wiki_render._claim_title (dependency-free; parity-pinned by tests).
    flat = WS_RE.sub(" ", claim_text).strip()
    head = SENTENCE_RE.split(flat, 1)[0].strip() if SENTENCE_RE.search(flat) else flat
    head = head if head else flat
    return (head[:77].rstrip() + "…") if len(head) > 78 else head


def display_label(page: Path, target: str) -> str:
    """The target's resolvable display label, or "" (page-local, same rules as the renderer)."""
    try:
        text = page.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    label = _fm_scalar(text, "title")
    if not label and target.startswith("Claims/"):
        claim_text = _fm_scalar(text, "claim_text")
        label = _claim_title(claim_text) if claim_text else ""
    return label


def scan_body(text: str) -> str:
    """The scannable body: frontmatter and fenced code blocks removed."""
    body = FRONTMATTER_RE.sub("", text, count=1)
    return FENCE_RE.sub("", body)


def check_page(path: Path, root: Path, wiki_root: Path,
               label_cache: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for match in LINK_RE.finditer(scan_body(path.read_text(encoding="utf-8", errors="replace"))):
        raw = match.group(1)
        target_part, _, alias = raw.partition("|")
        target = target_part.split("#", 1)[0].strip()
        if not target:
            continue
        target_page = wiki_root / f"{target}.md"
        if not target_page.exists():
            continue  # dangling / stem-form links are validate_wikilinks' contract
        if target not in label_cache:
            label_cache[target] = display_label(target_page, target)
        if not label_cache[target]:
            continue  # no resolvable label -> bare link is legal
        if not alias.strip():
            kind = "missing" if "|" not in raw else "blank"
            errors.append(f"{path.relative_to(root)}: {kind} display alias on [[{raw}]] "
                          f"(target has a resolvable label)")
    return errors


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0]).resolve() if argv else Path.cwd()
    wiki_root = root / "wiki"
    label_cache: dict[str, str] = {}
    errors: list[str] = []

    pages: list[Path] = []
    for subdir in WIKI_SUBDIRS:
        folder = wiki_root / subdir
        if folder.exists():
            pages.extend(sorted(folder.rglob("*.md")))
    index_page = wiki_root / "index.md"
    if index_page.exists():
        pages.append(index_page)

    for path in pages:
        errors.extend(check_page(path, root, wiki_root, label_cache))

    if errors:
        print("Link-alias validation failed:")
        for err in errors:
            print(f"- {err}")
        return 1
    print("Link-alias validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
