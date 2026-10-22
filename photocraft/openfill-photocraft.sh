#!/bin/sh
# OpenFill for PhotoCraft (Linux and macOS): starts PhotoCraft with its control channel and
# shows the OpenFill bar next to it. Needs Python 3.10+ with Pillow. The system Python is
# tried first when it has Tk (its Tk draws proper fonts), then ComfyUI's own Python.
# SPDX-License-Identifier: Apache-2.0
DIR=$(cd "$(dirname "$0")" && pwd)
if [ -d "$DIR/openfill_photocraft" ]; then
  export PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}"
fi
CANDIDATES="$OPENFILL_PYTHON /usr/bin/python3 python3 \
  $HOME/ComfyUI/venv/bin/python3 $HOME/ComfyUI/.venv/bin/python3 \
  $HOME/AI/ComfyUI/venv/bin/python3 $HOME/Documents/ComfyUI/venv/bin/python3 python"
for need in 'import tkinter' ''; do
  for py in $CANDIDATES; do
    command -v "$py" >/dev/null 2>&1 || [ -x "$py" ] || continue
    if "$py" -c "import sys, PIL; $need; sys.exit(sys.version_info < (3, 10))" 2>/dev/null; then
      exec "$py" -m openfill_photocraft "$@"
    fi
  done
done
echo "OpenFill needs Python 3.10 or newer with Pillow." >&2
echo "Install it (for example: sudo dnf install python3-pillow python3-tkinter), or set OPENFILL_PYTHON." >&2
exit 1
