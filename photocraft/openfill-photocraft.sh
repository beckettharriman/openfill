#!/bin/sh
# OpenFill for PhotoCraft (Linux and macOS): starts PhotoCraft with its control channel and
# opens the OpenFill page in your browser. Needs Python 3.10+ with Pillow; ComfyUI's own
# Python has it, so a ComfyUI venv is tried first.
# SPDX-License-Identifier: Apache-2.0
DIR=$(cd "$(dirname "$0")" && pwd)
if [ -d "$DIR/openfill_photocraft" ]; then
  export PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}"
fi
for py in "$OPENFILL_PYTHON" \
    "$HOME/ComfyUI/venv/bin/python3" "$HOME/ComfyUI/.venv/bin/python3" \
    "$HOME/AI/ComfyUI/venv/bin/python3" "$HOME/Documents/ComfyUI/venv/bin/python3" \
    python3 python; do
  [ -n "$py" ] || continue
  command -v "$py" >/dev/null 2>&1 || [ -x "$py" ] || continue
  if "$py" -c 'import sys, PIL; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    exec "$py" -m openfill_photocraft "$@"
  fi
done
echo "OpenFill needs Python 3.10 or newer with Pillow." >&2
echo "Install it (for example: python3 -m pip install --user pillow), or set OPENFILL_PYTHON." >&2
exit 1
