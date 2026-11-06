#!/bin/sh
# OpenFill for PhotoCraft (Linux and macOS): starts PhotoCraft with its control channel and
# shows the OpenFill bar next to it. Needs Python 3.10+ with Pillow. The system Python is
# tried first when it has Tk (its Tk draws proper fonts), then ComfyUI's own Python.
# SPDX-License-Identifier: Apache-2.0
DIR=$(cd "$(dirname "$0")" && pwd)
if [ -d "$DIR/openfill_photocraft" ]; then
  export PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}"
fi
# An explicit interpreter wins, including a ComfyUI environment without Tk.
CHECK='import sys, PIL; sys.exit(sys.version_info < (3, 10))'
if [ -n "$OPENFILL_PYTHON" ]; then
  # Said plainly rather than run: macOS's own Python is 3.9, and may open a developer tools dialog.
  if [ "$(uname -s)" = Darwin ]; then
    case "$(command -v "$OPENFILL_PYTHON" 2>/dev/null)" in
      /usr/bin/*)
        echo "OPENFILL_PYTHON is macOS's own Python 3.9, which OpenFill can't use." >&2
        echo "Leave OPENFILL_PYTHON unset, or point it to Python 3.10 or newer with Pillow." >&2
        exit 1
        ;;
    esac
  fi
  if "$OPENFILL_PYTHON" -c "$CHECK" 2>/dev/null; then
    exec "$OPENFILL_PYTHON" -m openfill_photocraft "$@"
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
    # macOS's own /usr/bin/python3 is 3.9, and without the developer tools running it opens an
    # install dialog: never try it there.
    if [ "$(uname -s)" = Darwin ]; then
      case "$(command -v "$py")" in /usr/bin/*) continue ;; esac
    fi
    if "$py" -c "$need$CHECK" 2>/dev/null; then
      exec "$py" -m openfill_photocraft "$@"
    fi
  done
done
echo "OpenFill needs Python 3.10 or newer with Pillow, and none was found." >&2
if [ "$(uname -s)" = Darwin ]; then
  echo "  Install Python from https://www.python.org/downloads/macos/, then run: python3 -m pip install Pillow" >&2
else
  echo "  Fedora:          sudo dnf install python3-pillow python3-tkinter" >&2
  echo "  Ubuntu, Debian:  sudo apt install python3-pil python3-pil.imagetk python3-tk" >&2
  echo "  Arch:            sudo pacman -S python-pillow tk" >&2
fi
echo "Then run this again. Or use ComfyUI's Python (Tk is optional):" >&2
echo "  OPENFILL_PYTHON=~/ComfyUI/venv/bin/python sh openfill-photocraft.sh" >&2
exit 1
