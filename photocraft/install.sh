#!/bin/sh
# Install OpenFill for PhotoCraft for this user (Linux or macOS): run  sh install.sh
# Options: --get-photocraft (download PhotoCraft without asking), --separate-icon, --uninstall
# SPDX-License-Identifier: Apache-2.0
DIR=$(cd "$(dirname "$0")" && pwd)
export PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}"
# An explicit interpreter wins, including a ComfyUI environment without Tk.
CHECK='import sys, PIL; sys.exit(sys.version_info < (3, 10))'
if [ -n "$OPENFILL_PYTHON" ]; then
  if "$OPENFILL_PYTHON" -c "$CHECK" 2>/dev/null; then
    exec "$OPENFILL_PYTHON" -m openfill_photocraft.install "$@"
  fi
  echo "OPENFILL_PYTHON must point to Python 3.10 or newer with Pillow." >&2
  exit 1
fi
for need in 'import tkinter; ' ''; do
  for py in /usr/bin/python3 python3 \
    "$HOME/ComfyUI/venv/bin/python3" "$HOME/ComfyUI/.venv/bin/python3" \
    "$HOME/AI/ComfyUI/venv/bin/python3" "$HOME/AI/ComfyUI/.venv/bin/python3" \
    "$HOME/Documents/ComfyUI/venv/bin/python3" "$HOME/Documents/ComfyUI/.venv/bin/python3" python; do
    command -v "$py" >/dev/null 2>&1 || [ -x "$py" ] || continue
    if "$py" -c "$need$CHECK" 2>/dev/null; then
      exec "$py" -m openfill_photocraft.install "$@"
    fi
  done
done
echo "OpenFill needs Python 3.10 or newer with Pillow. ComfyUI's Python can be used." >&2
echo 'Set OPENFILL_PYTHON to its full path and run this script again. Tk is optional.' >&2
exit 1
