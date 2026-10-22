# SPDX-License-Identifier: Apache-2.0
"""Generate tests/data/fabric-button.png: dark knit fabric with a small bright
round button (the Beckett bug: tiny selection on dark textured cloth)."""

import math
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gimp-openfill"))
from openfill import png  # noqa: E402

W = int(os.environ.get("FABRIC_W", "1600"))
H = int(os.environ.get("FABRIC_H", "1200"))
_bx, _by, _br = (int(v) for v in os.environ.get("FABRIC_BUTTON", "610,540,22").split(","))
BUTTON = (_bx, _by, _br)  # center x, y, radius
OUT_NAME = os.environ.get("FABRIC_OUT", "fabric-button.png")
rng = random.Random(7)
px = bytearray(W * H * 3)
for y in range(H):
    for x in range(W):
        # knit weave: two crossing sine ridges plus grain noise, dark navy/black
        weave = 0.5 + 0.25 * math.sin(x * 0.9 + y * 0.35) + 0.25 * math.sin(y * 0.9 - x * 0.35)
        grain = rng.random() * 0.12
        v = 14 + 26 * weave + 30 * grain
        r, g, b = v * 0.85, v * 0.9, v * 1.15
        # white shirt wedge top-right and a blue striped tie
        if x > W * 0.6875 + y * 0.3:
            r, g, b = 235, 236, 240
            if (x + y) % 90 < 28:
                r, g, b = 70, 80, 150
        cx, cy, rad = BUTTON
        d = math.hypot(x - cx, y - cy)
        if d < rad:
            ring = int(d / 4) % 2
            v = 230 if ring == 0 else 60
            if d > rad - 2.5:
                v = 210
            r, g, b = v, v * 0.96, v * 0.9
        i = (y * W + x) * 3
        px[i : i + 3] = bytes((int(max(0, min(255, r))), int(max(0, min(255, g))), int(max(0, min(255, b)))))
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", OUT_NAME)
with open(out, "wb") as fh:
    fh.write(png.encode(W, H, bytes(px), 3))
print("wrote", out, "button at", BUTTON)
