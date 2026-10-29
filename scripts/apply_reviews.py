#!/usr/bin/env python3
"""Preview, then apply, approved review decisions (ADR-0040, ADR-0068 decision 6).

Usage:
    uv run python scripts/apply_reviews.py preview [ROOT] [--json]
    uv run python scripts/apply_reviews.py apply --token <id> [ROOT]

Two separate commands, the same review boundary as the UI: `preview` runs Apply on a throwaway copy
of the vault and prints what it would change; only a clean (`ok`) preview prints a token. Read the
result, then `apply --token <id>` applies exactly that preview. Any change to the vault, the code or
the settings since the preview refuses it with `stale_preview`; this never previews again by itself,
because changed effects deserve another look. A token works once.

It runs the API's own implementation directly (`app/backend/apply_gate.py`), so tokens from either
work in the other. `apply` holds the Apply run guard; `preview` holds no lock while the copy runs.
`--json` prints the full preview result. Never touches raw/.

Exit codes:
  0  applied (or, for `preview`, a token was issued)
  1  Apply ran but is `validation_failed` or `saved_publication_pending`; for `preview`, no token
  2  usage
  3  another Apply is running
  4  the publication queue is unreadable (`scripts/republish.py --repair`)
  5  the producer journal is unreadable (`scripts/repair_producer_journal.py`)
  6  `preview_required`, `unknown_preview` or `stale_preview`: preview again
  7  `restart_required` (only an API caller sees it: this script runs the code on disk)
  8  `authorization_store_unavailable`: check db/ permissions and free space
  9  `revision_unavailable`: the vault revision could not be read or advanced
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import apply_gate, revision, run_guard  # noqa: E402
from app.backend.config import get_settings  # noqa: E402

EXIT_USAGE = 2
EXIT_CODES = {"preview_required": 6, "unknown_preview": 6, "stale_preview": 6,
              "restart_required": 7, "authorization_store_unavailable": 8}
EXIT_REVISION_UNAVAILABLE = 9


def _parse(argv: list[str]) -> tuple[str | None, str | None, Path, bool]:
    args = list(argv)
    command = args.pop(0) if args and args[0] in ("preview", "apply") else None
    token = None
    if "--token" in args:
        i = args.index("--token")
        token = args[i + 1] if i + 1 < len(args) else ""
        del args[i:i + 2]
    as_json = "--json" in args
    positional = [a for a in args if not a.startswith("-")]
    root = Path(positional[0]).resolve() if positional else Path.cwd()
    return command, token, root, as_json


def _print_preview(out: dict) -> None:
    print(f"Preview: {out['status']}" + (f" ({out['reason']})" if out.get("reason") else ""))
    if out.get("error"):
        print(f"  error: {out['error']}")
    diff = out.get("diff") or {}
    g = diff.get("graph") or {}
    for key in ("nodes_added", "nodes_status_changed", "edges_added", "edges_removed",
                "edges_status_changed", "edges_repointed"):
        if g.get(key):
            print(f"  graph {key}: {len(g[key])}")
    for m in diff.get("manifests") or []:
        print(f"  manifest {m['source_id']}: {m['field']} {m['from']} -> {m['to']}")
    for r in diff.get("reviews") or []:
        print(f"  review {r}")
    for page in diff.get("wiki") or []:
        print(f"  page {page.get('path')}")
    for it in out.get("not_appliable") or []:
        print(f"  not appliable: {it['review_id']} ({it['type']}): {it['reason']}")
    v = out.get("validators") or {}
    print(f"  validators would pass: {v.get('passed')}")
    for f in v.get("failures") or []:
        print(f"    failing: {f}")
    for w in out.get("warnings") or []:
        print(f"  warning: {w}")
    if out.get("preview_token"):
        print(f"Token: {out['preview_token']}")
        print("Apply it with: uv run python scripts/apply_reviews.py apply --token "
              f"{out['preview_token']}")
    else:
        why = out.get("token_withheld") or f"the preview is not clean ({out['status']})"
        print(f"No token: {why}. Resolve it, then preview again.")


def main(argv: list[str]) -> int:
    command, token, root, as_json = _parse(argv)
    if command is None:
        print(__doc__, file=sys.stderr)
        return EXIT_USAGE
    from app.backend import main as api                   # the shared Apply orchestration
    st = get_settings(root)
    try:
        if command == "preview":
            out = apply_gate.preview(st, evaluate=api._evaluate_preview)
            if as_json:
                print(json.dumps(out, indent=2))
            else:
                _print_preview(out)
            return 0 if out.get("preview_token") else 1
        if not token:
            print("Refused: preview_required: preview first, then pass its --token.",
                  file=sys.stderr)
            return EXIT_CODES["preview_required"]
        return run_guard.run_cli(st.db_dir, "apply", lambda: _apply(st, token, api))
    except apply_gate.ApplyRefused as exc:
        print(f"Refused: {exc.reason}: {exc.action}. Nothing was changed.", file=sys.stderr)
        return EXIT_CODES[exc.reason]
    except revision.RevisionUnavailable as exc:
        print(f"Refused: revision_unavailable: {exc}.", file=sys.stderr)
        return EXIT_REVISION_UNAVAILABLE


def _apply(st, token: str, api) -> int:
    try:
        out = apply_gate.apply(st, token, run_apply=api.run_apply)
    except api.GraphUnavailable:
        print("Refused: graph_unavailable: rebuild db/graph.sqlite before applying reviews.",
              file=sys.stderr)
        return 1
    print(f"Apply: {out['status']}")
    for w in out.get("warnings") or []:
        print(f"  warning: {w}")
    for f in out.get("failed_validators") or []:
        print(f"  failing validator: {f}")
    if out["status"] == "saved_publication_pending":
        print("  Decisions saved; publication incomplete: run scripts/republish.py "
              "(don't apply or decide again).")
    return 0 if out["status"] == "applied" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
