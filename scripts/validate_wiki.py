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
    "sha256", "file_type", "language", "page_count", "chunk_count",
    "status", "ingestion_status", "summary_status", "generation_status",
    "input_fingerprint",
)
# Full lifecycle vocabulary (matches graph NODE_STATUSES + manifests.SOURCE_STATUSES, ADR-0036) so any
# valid manifest/page status round-trips through the validator.
_VALID_STATUS = {"active", "candidate", "stale_candidate", "deprecated_candidate", "archive_candidate",
                 "archived", "delete_candidate", "deleted", "hidden", "evidence_hidden"}
_VALID_GENERATION = {"deterministic", "enriched", "human_edited"}
_VALID_SUMMARY = {"stub", "enriched"}
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
        expected_norm = (manifest.get("normalized") or {}).get("markdown_path")
        if fm.get("normalized_path") != expected_norm:
            errors.append(f"{sid}: page normalized_path does not match manifest")
        expected_pages = "null" if manifest.get("page_count") is None else str(manifest["page_count"])
        if fm.get("page_count") != expected_pages:
            errors.append(f"{sid}: page_count {fm.get('page_count')} != manifest {expected_pages}")
        if fm.get("chunk_count") != str(manifest.get("chunk_count") or 0):
            errors.append(f"{sid}: chunk_count {fm.get('chunk_count')} != manifest")
        if fm.get("ingestion_status") != manifest.get("ingestion_status"):
            errors.append(f"{sid}: ingestion_status does not match manifest")
        # The manifest is the durable source lifecycle-status authority (ADR-0036 decision 13); the
        # page is a projection of it (default active when unset).
        if fm.get("status") != (manifest.get("status") or "active"):
            errors.append(f"{sid}: page status {fm.get('status')!r} != manifest "
                          f"{manifest.get('status') or 'active'!r}")

    # Lifecycle fields use the controlled vocabularies (ADR-0018/0022).
    if fm.get("status") and fm["status"] not in _VALID_STATUS:
        errors.append(f"{sid}: invalid status {fm['status']!r}")
    if fm.get("generation_status") and fm["generation_status"] not in _VALID_GENERATION:
        errors.append(f"{sid}: invalid generation_status {fm['generation_status']!r}")
    if fm.get("summary_status") and fm["summary_status"] not in _VALID_SUMMARY:
        errors.append(f"{sid}: invalid summary_status {fm['summary_status']!r}")

    # No absolute paths may leak (ADR-0009).
    if "raw_path" in fm:
        errors.append(f"{sid}: frontmatter has absolute 'raw_path' (use relative_raw_path)")
    for line in text.splitlines():
        if _ABSOLUTE.search(line) or "/home/" in line:
            errors.append(f"{sid}: leaks an absolute path: {line.strip()[:60]}")
            break

    # Summary callout present and labelled for what it is: an extractive stub, or a
    # generated/unverified LLM summary (ADR-0016/0026). The linter enforces the label so an
    # enriched summary can never silently claim authority it has not earned.
    summary_line = next((ln for ln in text.splitlines() if ln.strip().startswith("> [!summary]")), None)
    if summary_line is None:
        errors.append(f"{sid}: missing > [!summary] callout")
    elif fm.get("summary_status") == "stub" and "Extractive excerpt" not in summary_line:
        errors.append(f"{sid}: stub summary is not labelled as an extractive excerpt")
    elif fm.get("summary_status") == "enriched" and "unverified" not in summary_line.lower():
        errors.append(f"{sid}: enriched summary is not labelled as generated/unverified")

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
