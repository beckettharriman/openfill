#!/bin/sh
# Install OpenFill for PhotoCraft for this user (Linux or macOS): run  sh install.sh
# Options: --get-photocraft (download PhotoCraft without asking), --separate-icon, --uninstall
# SPDX-License-Identifier: Apache-2.0
DIR=$(cd "$(dirname "$0")" && pwd)
export PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}"
CANDIDATES="$OPENFILL_PYTHON /usr/bin/python3 python3 \
  $HOME/ComfyUI/venv/bin/python3 $HOME/ComfyUI/.venv/bin/python3 \
  $HOME/AI/ComfyUI/venv/bin/python3 $HOME/Documents/ComfyUI/venv/bin/python3 python"
for need in 'import tkinter' ''; do
  for py in $CANDIDATES; do
    command -v "$py" >/dev/null 2>&1 || [ -x "$py" ] || continue
    if "$py" -c "import sys, PIL; $need; sys.exit(sys.version_info < (3, 10))" 2>/dev/null; then
      exec "$py" -m openfill_photocraft.install "$@"
    fi
  done
done
echo "OpenFill needs Python 3.10 or newer with Pillow and Tk." >&2
if [ "$(uname -s)" = Darwin ]; then
  echo "Install Python from python.org (includes Tk), then run: python3 -m pip install Pillow" >&2
else
  echo "On Fedora: sudo dnf install python3-pillow python3-tkinter   (Ubuntu: sudo apt install python3-pil python3-tk)" >&2
fi
exit 1
