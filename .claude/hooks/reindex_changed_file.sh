#!/usr/bin/env bash
set -euo pipefail
PAYLOAD="$(cat)"
FILE_PATH="$(printf '%s' "$PAYLOAD" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("tool_input",{}).get("file_path", ""))')"
# The keyword index is built from typed wiki pages (navigation) and per-source chunk JSONL
# (evidence), so reindex when either changes (ADR-0032 §7). Whole-file normalized markdown is no
# longer an index input. Vector reindexing (LanceDB) lands in Phase 4d.
case "$FILE_PATH" in
  *wiki/*.md|*wiki/*/*.md|*normalized/chunks/*.jsonl)
    cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
    python3 scripts/reindex_keyword.py . >&2 || true
    python3 scripts/validate_index_consistency.py . >&2 || true
    ;;
  *)
    :
    ;;
esac
exit 0
