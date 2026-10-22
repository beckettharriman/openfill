# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""GIMP-free helpers for the progress window: ComfyUI preview frame parsing,
ETA estimation and frame-rate throttling."""

from __future__ import annotations

import json
import struct
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

# ComfyUI binary websocket event types (server.py BinaryEventTypes). Only 1 and 4
# carry previews; 2 and 3 are listed so the protocol is documented in one place.
PREVIEW_IMAGE = 1
UNENCODED_PREVIEW_IMAGE = 2
TEXT = 3
PREVIEW_IMAGE_WITH_METADATA = 4


@dataclass(frozen=True)
class PreviewFrame:
    """A decoded preview image from the sampler."""

    mime: str  # image/jpeg or image/png
    data: bytes
    metadata: dict = field(default_factory=dict)


def parse_preview_frame(payload: bytes) -> PreviewFrame | None:
    """Decode a ComfyUI binary websocket frame into a :class:`PreviewFrame`.

    Layouts (big-endian):
      event 1: [event][image_type 1=JPEG 2=PNG][image bytes]
      event 4: [event][metadata_length][metadata json][image bytes]
    Anything else (text events, truncated frames) returns None.
    """
    if len(payload) < 8:
        return None
    event = struct.unpack(">I", payload[:4])[0]
    if event == PREVIEW_IMAGE:
        image_type = struct.unpack(">I", payload[4:8])[0]
        data = payload[8:]
        if not data:
            return None
        return PreviewFrame("image/png" if image_type == 2 else "image/jpeg", bytes(data))
    if event == PREVIEW_IMAGE_WITH_METADATA:
        length = struct.unpack(">I", payload[4:8])[0]
        if len(payload) < 8 + length + 1:
            return None
        try:
            metadata = json.loads(payload[8 : 8 + length].decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            metadata = {}
        mime = str(metadata.get("image_type") or "image/jpeg")
        return PreviewFrame(mime, bytes(payload[8 + length :]), metadata)
    return None


@dataclass
class Eta:
    """Remaining-time estimate from (elapsed, fraction) samples.

    Only the sampling phase is predictable, so the caller feeds it fractions
    from the progress callback; the estimate uses the rate since the first
    sample above ``min_fraction`` and is damped so it does not jump around.
    """

    min_fraction: float = 0.02
    _t0: float | None = None
    _f0: float = 0.0
    _estimate: float | None = None

    def update(self, elapsed: float, fraction: float) -> float | None:
        """Feed one sample; returns the remaining seconds or None while unknown."""
        fraction = max(0.0, min(1.0, fraction))
        if fraction < self.min_fraction:
            return None
        if self._t0 is None or fraction < self._f0:
            self._t0, self._f0 = elapsed, fraction
            return None
        progressed = fraction - self._f0
        if progressed <= 0.001 or elapsed <= self._t0:
            return self._estimate
        rate = progressed / (elapsed - self._t0)  # fraction per second
        remaining = (1.0 - fraction) / rate
        if self._estimate is None:
            self._estimate = remaining
        else:
            self._estimate = 0.6 * self._estimate + 0.4 * remaining
        return max(0.0, self._estimate)


class OverallProgress:
    """One progress bar over several backend runs (the rounds of a large
    Generative Expand).

    ``weights`` is the relative cost of each round (for example the canvas
    area); round ``i`` fills its share of the bar. The reported fraction never
    goes backwards, and with more than one round the text names the round.
    """

    def __init__(self, weights: Sequence[float]) -> None:
        cleaned = [max(0.0, float(w)) for w in weights] or [1.0]
        if sum(cleaned) <= 0:
            cleaned = [1.0] * len(cleaned)
        total = sum(cleaned)
        self.count = len(cleaned)
        self._starts = [sum(cleaned[:i]) / total for i in range(self.count)]
        self._shares = [w / total for w in cleaned]
        self.index = 0
        self._last = 0.0

    def begin(self, index: int) -> None:
        """Start round ``index`` (0-based)."""
        self.index = max(0, min(self.count - 1, int(index)))

    def update(self, fraction: float, text: str = "") -> tuple[float, str]:
        """Map a round's (fraction, text) to the overall (fraction, text)."""
        fraction = max(0.0, min(1.0, float(fraction)))
        overall = self._starts[self.index] + self._shares[self.index] * fraction
        self._last = max(self._last, min(1.0, overall))
        if self.count > 1 and text:
            text = f"{text} (part {self.index + 1} of {self.count})"
        return self._last, text

    def wrap(self, progress: Callable[[float, str], None]) -> Callable[[float, str], None]:
        """A progress callback that reports the overall values to ``progress``."""
        return lambda fraction, text="": progress(*self.update(fraction, text))


def format_duration(seconds: float | None) -> str:
    """``75`` -> ``"1:15"``, ``3725`` -> ``"1:02:05"``, None -> ``"--:--"``."""
    if seconds is None:
        return "--:--"
    seconds = round(max(0.0, seconds))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class FrameThrottle:
    """Admit at most one frame per ``min_interval`` seconds (drop the rest)."""

    def __init__(self, min_interval: float = 0.25, clock: Callable[[], float] = time.monotonic) -> None:
        self.min_interval = min_interval
        self._clock = clock
        self._last: float | None = None
        self.dropped = 0
        self.admitted = 0

    def admit(self) -> bool:
        """True when a frame may be shown now (and records it)."""
        now = self._clock()
        if self._last is not None and now - self._last < self.min_interval:
            self.dropped += 1
            return False
        self._last = now
        self.admitted += 1
        return True


def fit_within(width: int, height: int, long_side: int) -> tuple[int, int]:
    """Scale (width, height) so the longer side equals ``long_side`` (never upscale)."""
    if width <= 0 or height <= 0:
        return 1, 1
    longer = max(width, height)
    if longer <= long_side:
        return width, height
    scale = long_side / longer
    return max(1, round(width * scale)), max(1, round(height * scale))


def download_row(current: dict[str, Any]) -> str:
    """One-line summary of a supervisor download status entry."""
    done, total = current.get("done") or 0, current.get("total") or 0
    pct = f"{100.0 * done / total:.0f}%" if total else "..."
    speed = (current.get("speed") or 0) / 1_048_576
    name = str(current.get("file") or current.get("model_id") or "download").rsplit("/", 1)[-1]
    return f"{name}  {pct}  {speed:.1f} MB/s"
