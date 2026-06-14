#!/usr/bin/env python3
"""Validate the Phase 3 wiki Source-page layer against its manifests.

Enforces the deterministic Source-page contract (ADR-0015/0016/0022): required
frontmatter, filename == source_id, consistency with the manifest, full coverage of
extracted/partial sources, no orphan/stale pages, a labelled summary stub, no leaked
absolute paths, and no dangling wikilinks. The wiki layer is gitignored local data: if
there are no manifests and no pages, there is nothing to validate (a pass).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.workers.wiki_render import parse_frontmatter

_EXTRACTED = {"extracted", "partial"}
_REQUIRED_FIELDS = (
    "type", "source_id", "title", "relative_raw_path", "normalized_path",
    "sha256", "status", "ingestion_status", "summary_status", "generation_status",
    "input_fingerprint",
)
_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
_ABSOLUTE = re.compile(r':\s*"?/')  # a frontmatter value beginning with "/"


def _manifest_index(manifests_dir: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not manifests_dir.exists():
        return out
    for path in sorted(manifests_dir.glob("*.json")):
        try:
            m = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if m.get("source_id"):
            out[m["source_id"]] = m
    return out


def _check_page(root: Path, path: Path, manifests: dict[str, dict]) -> list[str]:
    sid = path.stem
    errors: list[str] = []
    text = path.read_text(encoding="utf-8")
    fm = parse_frontmatter(text)
    body = text.split("\n---\n", 1)[-1]

    for field in _REQUIRED_FIELDS:
        if not fm.get(field):
            errors.append(f"{sid}: missing frontmatter field '{field}'")
    if fm.get("source_id") and fm["source_id"] != sid:
        errors.append(f"{sid}: frontmatter source_id {fm['source_id']} != filename")

    manifest = manifests.get(sid)
    if manifest is None:
        errors.append(f"{sid}: orphan Source page (no manifest)")
    elif manifest.get("ingestion_status") not in _EXTRACTED:
        errors.append(
            f"{sid}: stale Source page for {manifest.get('ingestion_status')!r} manifest"
        )
    else:
        if fm.get("sha256") != manifest.get("sha256"):
            errors.append(f"{sid}: page sha256 does not match manifest")
        if fm.get("relative_raw_path") != manifest.get("relative_raw_path"):
            errors.append(f"{sid}: page relative_raw_path does not match manifest")

    # No absolute paths may leak (ADR-0009).
    if "raw_path" in fm:
        errors.append(f"{sid}: frontmatter has absolute 'raw_path' (use relative_raw_path)")
    for line in text.splitlines():
        if _ABSOLUTE.search(line) or "/home/" in line:
            errors.append(f"{sid}: leaks an absolute path: {line.strip()[:60]}")
            break

    # Summary callout present and, when a stub, labelled as an extractive excerpt.
    summary_line = next((ln for ln in text.splitlines() if ln.strip().startswith("> [!summary]")), None)
    if summary_line is None:
        errors.append(f"{sid}: missing > [!summary] callout")
    elif fm.get("summary_status") == "stub" and "Extractive excerpt" not in summary_line:
        errors.append(f"{sid}: stub summary is not labelled as an extractive excerpt")

    # No dangling wikilinks.
    for target in _WIKILINK.findall(body):
        target = target.split("|", 1)[0].split("#", 1)[0].strip()
        if not (root / "wiki" / f"{target}.md").exists():
            errors.append(f"{sid}: dangling wikilink [[{target}]]")

    return errors


def main(argv: list[str]) -> int:
    root = Path(argv[0]).resolve() if argv else Path.cwd()
    sources_dir = root / "wiki" / "Sources"
    manifests = _manifest_index(root / "raw" / "manifests")

    errors: list[str] = []
    pages = sorted(sources_dir.glob("*.md")) if sources_dir.exists() else []
    page_ids = {p.stem for p in pages}
    for path in pages:
        errors.extend(_check_page(root, path, manifests))

    # Coverage: every extracted/partial source must have a Source page.
    for sid, manifest in manifests.items():
        if manifest.get("ingestion_status") in _EXTRACTED and sid not in page_ids:
            errors.append(f"{sid}: extracted source has no Source page (coverage gap)")

    if errors:
        print("Wiki validation failed:")
        for err in errors:
            print(f"- {err}")
        return 1
    print(f"Wiki validation passed ({len(pages)} Source page(s) checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
