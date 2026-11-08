#!/usr/bin/env python3
"""Validate synthesis artifacts and the versions their proposals name (ADR-0073 decision 8).

Three cases, kept apart:

- **advisory** (passes): a valid legacy-format artifact without the `version_digest` field. It is
  displayable and decidable as is; the generator writes the field on its next write.
- **error** (fails): an unreadable artifact, an invalid required field (`input_fingerprint` 16 hex;
  `title`, `summary`, `synthesis` non-blank; `confidence` finite in [0, 1], not a bool), or a stored
  digest that differs from the one recomputed from the fields.
- **interrupted generation** (fails): an open `propose_synthesis` proposal for the artifact's inputs
  names another version than the artifact holds — a regeneration stopped between its writes. Run the
  producer recovery (`scripts/generate_synthesis.py`). Decisions on it are refused until then.

Read-only; no enrichment directory is a pass.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.workers import synthesis_version as sv

_SUFFIX = ".synthesis.json"


def _check(root: Path) -> tuple[list[str], list[str]]:
    enrichment = root / "normalized" / "enrichment"
    errors: list[str] = []
    advisories: list[str] = []
    versions: dict[str, dict] = {}
    for path in sorted(enrichment.glob(f"*{_SUFFIX}")) if enrichment.exists() else []:
        topic = path.name[:-len(_SUFFIX)]
        version = sv.read(enrichment, topic)
        versions[topic] = version
        if version.get("ok"):
            if version["legacy_format"]:
                advisories.append(f"{path.name}: legacy format (no stored version digest); it is "
                                  "usable as is and gets one on its next generation")
            continue
        reason = version.get("reason")
        if reason == sv.UNREADABLE:
            errors.append(f"{path.name}: unreadable")
        elif reason == sv.DIGEST_MISMATCH:
            errors.append(f"{path.name}: the stored version digest doesn't match its text")
        else:
            errors.append(f"{path.name}: invalid field `{version.get('field')}`")
    pending = root / "reviews" / "pending"
    for path in sorted(pending.glob("*.json")) if pending.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue                                     # the review validators report it
        if not isinstance(item, dict) or item.get("type") != "propose_synthesis":
            continue
        topic = (item.get("subject") or {}).get("topic_node_id")
        version = versions.get(topic) if isinstance(topic, str) else None
        if not version or not version.get("ok"):
            continue
        problem = sv.decision_problem(version, item, version["digest"])
        if problem == sv.GENERATION_INTERRUPTED:
            errors.append(f"{path.name}: interrupted generation — the proposal and the synthesis "
                          f"text for {topic} disagree; rerun `uv run python "
                          "scripts/generate_synthesis.py`")
    return errors, advisories


def main(argv: list[str]) -> int:
    root = Path(argv[0]).resolve() if argv else Path.cwd()
    errors, advisories = _check(root)
    for adv in advisories:
        print(f"advisory: {adv}")
    if errors:
        print("Synthesis validation failed:")
        for err in errors:
            print(f"- {err}")
        return 1
    print("Synthesis validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
