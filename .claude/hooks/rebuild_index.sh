#!/usr/bin/env bash
set -euo pipefail
PAYLOAD="$(cat)"
FILE_PATH="$(printf '%s' "$PAYLOAD" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("tool_input",{}).get("file_path", ""))')"
case "$FILE_PATH" in
  *wiki/Sources/*.md|*wiki/Items/*.md|*wiki/Claims/*.md|*wiki/Tags/*.md|*wiki/Synthesis/*.md|*wiki/Queries/*.md)
    cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
    python3 scripts/rebuild_index.py . >&2
    ;;
  *)
    :
    ;;
esac
exit 0
