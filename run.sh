#!/bin/sh
# Container entrypoint for the RST WYSIWYG editor.
#   RST_EDITOR_PORT  (default 8000)
#   RST_WORK_ROOT    (default /work — repo clone cache; persist with a volume)
set -eu
export RST_EDITOR_HOST="0.0.0.0"
export RST_EDITOR_PORT="${RST_EDITOR_PORT:-8000}"
export RST_WORK_ROOT="${RST_WORK_ROOT:-/work}"
mkdir -p "$RST_WORK_ROOT"
exec python3 /opt/rst-editor/app.py
