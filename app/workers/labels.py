"""Display-label resolution for wiki link aliases (ADR-0060).

The worker-side IO seam behind the two-layer label contract: renderers stay IO-free and
receive a ``{link_target: label}`` map ("Sources/src_x", "Claims/clm_x", "Items/<slug>" →
display label); this module builds it. Resolution is **page-local** — the target page's
frontmatter ``title:``, with one fallback: a Claim page without ``title:`` (pre-ADR-0060)
derives it from ``claim_text`` exactly as the renderer would. `scripts/validate_link_aliases.py`
resolves labels the same way, so a producer that passes `display_labels(...)` output and the
validator agree by construction: a target this returns no entry for is exactly a target the
validator permits a bare link to.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from app.workers.wiki_render import _claim_title, parse_frontmatter

_UNESCAPE = re.compile(r"\\(.)")


def _page_label(page: Path, target: str) -> str:
    try:
        text = page.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    fm = parse_frontmatter(text)
    label = _UNESCAPE.sub(r"\1", str(fm.get("title") or "")).strip()
    if not label and target.startswith("Claims/"):
        claim_text = _UNESCAPE.sub(r"\1", str(fm.get("claim_text") or "")).strip()
        label = _claim_title(claim_text) if claim_text else ""
    return label


def display_labels(wiki_dir: Path, targets: Iterable[str]) -> dict[str, str]:
    """Resolve display labels for link targets, page-locally; unresolvable targets are absent."""
    out: dict[str, str] = {}
    for target in dict.fromkeys(targets):
        if not target:
            continue
        label = _page_label(wiki_dir / f"{target}.md", target)
        if label:
            out[target] = label
    return out
