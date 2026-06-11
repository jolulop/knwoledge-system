#!/usr/bin/env bash
set -euo pipefail
PAYLOAD="$(cat)"
FILE_PATH="$(printf '%s' "$PAYLOAD" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("tool_input",{}).get("file_path", ""))')"
case "$FILE_PATH" in
  *wiki/*.md|*wiki/*/*.md)
    cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
    python3 scripts/validate_wikilinks.py . >&2 || true
    ;;
  *)
    :
    ;;
esac
exit 0
