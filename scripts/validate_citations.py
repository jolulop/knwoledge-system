#!/usr/bin/env python3
"""Structured-citation validation for claim (and query) pages (ADR-0019/0020/0026).

Replaces the Phase-3 scaffold. For every claim page it parses the structured
`citations:` frontmatter list and mechanically grounds each citation against the cited
source's normalized Markdown: the `(source_id, char_start, char_end)` range must be in
bounds and the evidence quote must occur verbatim (whitespace-normalized) at that range
(claims require a quote). A missing normalized source, an unknown source, an out-of-bounds
range, or a quote mismatch all fail; `chunk_id` is advisory and never grounds. A claim may
instead carry the explicit `No source found in vault.` marker.

Saved Query pages are grounded identically (ADR-0034): every frontmatter citation must resolve, or
the page carries the `No source found in vault.` marker.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.workers.citations import ground_citation, is_source_id, parse_citations
from app.workers.wiki_render import parse_frontmatter

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
NO_SOURCE = "No source found in vault."
# A claim with these lifecycle statuses is a tombstone (evidence superseded, pending
# review) and legitimately carries no active citations (ADR-0030).
_TOMBSTONE_STATUSES = {"deprecated_candidate", "archived"}


def _frontmatter(text: str) -> str:
    match = FRONTMATTER_RE.match(text)
    return match.group(1) if match else ""


def _ground_citations(root: Path, label: str, citations: list[dict]) -> list[str]:
    """Mechanically ground each structured citation against its source's normalized Markdown:
    the source must have a manifest (a real source, ADR-0020), the normalized Markdown must exist,
    and `(source_id, char_start, char_end)` must be in bounds with the quote verbatim. Shared by
    claim and query pages so saved queries are audited identically (ADR-0034)."""
    errors: list[str] = []
    markdown_dir = root / "normalized" / "markdown"
    manifests_dir = root / "raw" / "manifests"
    for i, citation in enumerate(citations):
        source_id = citation.get("source_id")
        # Validate the canonical src_<16hex> shape before constructing any path (defence in depth —
        # generated pages are safe, but a malformed frontmatter must fail cleanly, not traverse).
        if not is_source_id(source_id):
            errors.append(f"{label}: citation[{i}] malformed source_id: {source_id!r}")
            continue
        if not (manifests_dir / f"{source_id}.json").exists():
            errors.append(f"{label}: citation[{i}] cites source with no manifest: {source_id!r}")
            continue
        md_path = markdown_dir / f"{source_id}.md"
        if not md_path.exists():
            errors.append(f"{label}: citation[{i}] source {source_id} has no normalized Markdown")
            continue
        normalized = md_path.read_text(encoding="utf-8", errors="replace")
        for problem in ground_citation(citation, normalized, require_quote=True):
            errors.append(f"{label}: citation[{i}] {problem}")
    return errors


def _check_claim(root: Path, path: Path) -> list[str]:
    sid = path.stem
    text = path.read_text(encoding="utf-8", errors="replace")
    status = parse_frontmatter(text).get("status")
    citations = parse_citations(_frontmatter(text))

    if not citations:
        # A tombstone (deprecated/archived) legitimately has no active citations.
        if status not in _TOMBSTONE_STATUSES and NO_SOURCE not in text:
            return [f"{sid}: claim has no citations and no '{NO_SOURCE}' marker"]
        return []

    errors = _ground_citations(root, sid, citations)
    if "## Evidence" not in text:
        errors.append(f"{sid}: missing Evidence section")
    return errors


def _check_query(root: Path, path: Path) -> list[str]:
    sid = path.stem
    text = path.read_text(encoding="utf-8", errors="replace")
    citations = parse_citations(_frontmatter(text))

    if not citations:
        # An abstained answer legitimately carries no citations — require the no-source marker.
        if NO_SOURCE not in text:
            return [f"{sid}: query answer has no citations and no '{NO_SOURCE}' marker"]
        return []

    # A saved query is citation-audited like a claim: every frontmatter citation must ground (ADR-0034).
    errors = _ground_citations(root, sid, citations)
    if "## Citations" not in text:
        errors.append(f"{sid}: missing Citations section")
    return errors


def main(argv: list[str]) -> int:
    root = Path(argv[0]).resolve() if argv else Path.cwd()
    errors: list[str] = []

    claims_dir = root / "wiki" / "Claims"
    for path in sorted(claims_dir.rglob("*.md")) if claims_dir.exists() else []:
        errors.extend(_check_claim(root, path))

    queries_dir = root / "wiki" / "Queries"
    for path in sorted(queries_dir.rglob("*.md")) if queries_dir.exists() else []:
        errors.extend(_check_query(root, path))

    if errors:
        print("Citation validation failed:")
        for err in errors:
            print(f"- {err}")
        return 1
    print("Citation validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
