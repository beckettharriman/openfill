# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""OpenFill's look in Tk: the design's colours, type and sizes, its icons, and the controls
the bar is made of (segmented controls, buttons, inputs, the prompt box, selects, cards,
progress bars and message boxes).

Sizes are screen pixels (fonts too: a negative Tk font size is pixels, so text is the same
size whatever DPI Tk assumes). Rounded shapes and icons are drawn with Pillow at four times
the size and scaled down, so their edges are smooth; Tk only places them.
"""

from __future__ import annotations

import contextlib
import io
import itertools
import math
import re
import sys
import tkinter as tk
from collections.abc import Callable, Sequence
from tkinter import font as tkfont
from typing import Any

from PIL import Image, ImageDraw

BG, PANEL, LINE, TEXT, DIM, ACCENT, BAD, OK, WARN = (
    "#1e1f22",
    "#2b2d31",
    "#3a3c42",
    "#e8e8ea",
    "#9a9ba2",
    "#4a8ef0",
    "#e5534b",
    "#3fb950",
    "#e8a33d",
)
SEG_ON = "#45474e"  # the selected segment
WELL = "#191a1d"  # the Expand editor's background
PLACEHOLDER_FG = "#757575"  # a text box placeholder, as the browser draws one
ACCENT_HOVER = "#5d9cf5"
WHITE = "#ffffff"

W = 400  # window width
PAD = 16  # side padding
CW = W - 2 * PAD  # content width
LABEL_W = 84  # label column of a form row
FIELD_W = CW - LABEL_W - 12  # the control in a form row
SS = 4  # supersampling for drawn shapes


# ---- type ----------------------------------------------------------------------------------


class Fonts:
    """The design's type: 11 px labels, 12 px hints, 13 px body; 600 weight for titles."""

    def __init__(self, root: tk.Misc) -> None:
        families = set(tkfont.families(root))
        wanted = ("Segoe UI",) if sys.platform == "win32" else ("Noto Sans", "DejaVu Sans")
        family = next((f for f in wanted if f in families), tkfont.nametofont("TkDefaultFont").actual("family"))
        semibold = "Segoe UI Semibold" if sys.platform == "win32" and "Segoe UI Semibold" in families else ""
        self.family = family

        def make(px: int, strong: bool = False) -> tkfont.Font:
            if strong and semibold:
                return tkfont.Font(root, family=semibold, size=-px)
            return tkfont.Font(root, family=family, size=-px, weight="bold" if strong else "normal")

        self.body = make(13)
        self.bold = make(13, True)
        self.small = make(12)
        self.small_bold = make(12, True)
        self.label = make(11, True)
        self.tiny = make(11)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            try:
                tkfont.nametofont(name).configure(family=family, size=-13)
            except tk.TclError:
                pass


FONTS: Fonts | None = None


def fonts(root: tk.Misc) -> Fonts:
    global FONTS
    if FONTS is None or str(FONTS.body.cget("family")) == "":
        FONTS = Fonts(root)
    return FONTS


def ellipsize(text: str, font: tkfont.Font, width: int) -> str:
    """``text`` cut to ``width`` pixels with "..." (as CSS text-overflow: ellipsis)."""
    if font.measure(text) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid].rstrip() + "...") <= width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "..."


# ---- images ----------------------------------------------------------------------------------

_cache: dict[tuple, tk.PhotoImage] = {}


def photo(image: Image.Image) -> tk.PhotoImage:
    """A Tk image from a Pillow one, through PNG (Tk reads PNG itself; no ImageTk needed)."""
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return tk.PhotoImage(data=buf.getvalue())


def _rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def rrect_image(w: int, h: int, r: float, fill: str, bg: str, border: str | None = None, bw: int = 1) -> Image.Image:
    """A rounded rectangle on ``bg`` (antialiased), optionally with a ``bw`` px border."""
    big = Image.new("RGB", (max(1, w) * SS, max(1, h) * SS), _rgb(bg))
    d = ImageDraw.Draw(big)
    box = (0, 0, w * SS - 1, h * SS - 1)
    if border:
        d.rounded_rectangle(box, radius=r * SS, fill=_rgb(border))
        inset = bw * SS
        d.rounded_rectangle(
            (inset, inset, w * SS - 1 - inset, h * SS - 1 - inset), radius=max(0, (r - bw) * SS), fill=_rgb(fill)
        )
    else:
        d.rounded_rectangle(box, radius=r * SS, fill=_rgb(fill))
    return big.resize((max(1, w), max(1, h)), Image.BOX)


def rrect(w: int, h: int, r: float, fill: str, bg: str, border: str | None = None, bw: int = 1) -> tk.PhotoImage:
    key = ("rrect", w, h, r, fill, bg, border, bw)
    if key not in _cache:
        if len(_cache) > 600:
            _cache.clear()
        _cache[key] = photo(rrect_image(w, h, r, fill, bg, border, bw))
    return _cache[key]


def dot(color: str, bg: str, size: int = 6) -> tk.PhotoImage:
    return rrect(size, size, size / 2, color, bg)


# ---- icons (24 px grid, 1.5 px stroke at any size, as in the design) ---------------------------

ICONS: dict[str, list[tuple]] = {
    "gear": [
        ("circle", 12, 12, 3),
        (
            "path",
            "M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 "
            "1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 "
            "0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 "
            "1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 "
            "1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
        ),
    ],
    "collapse": [("path", "M6 9l6 6 6-6")],
    "chevron": [("path", "M6 9l6 6 6-6")],
    "back": [("path", "M19 12H5"), ("path", "M12 19l-7-7 7-7")],
    "check": [("path", "M20 6L9 17l-5-5")],
    "warn": [
        ("path", "M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"),
        ("path", "M12 9v4"),
        ("path", "M12 17h.01"),
    ],
    "error": [("circle", 12, 12, 10), ("path", "M12 8v4"), ("path", "M12 16h.01")],
    "info": [("circle", 12, 12, 10), ("path", "M12 16v-4"), ("path", "M12 8h.01")],
    "close": [("path", "M18 6L6 18"), ("path", "M6 6l12 12")],
    "folder": [("path", "M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z")],
    "selection": [("rect", 3, 3, 18, 18, 2, (4, 3))],
    "image": [("rect", 3, 3, 18, 18, 2, None), ("circle", 8.5, 8.5, 1.5), ("path", "M21 15l-5-5L5 21")],
    "trash": [("path", "M3 6h18"), ("path", "M8 6V4h8v2"), ("path", "M19 6l-1 14H6L5 6")],
    "download": [("path", "M12 3v12"), ("path", "M7 10l5 5 5-5"), ("path", "M5 21h14")],
    "arrow": [("path", "M5 12h14"), ("path", "M12 5l7 7-7 7")],
}

_TOKEN = re.compile(r"[MmLlHhVvAaZzCc]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
_ARGS = {"M": 2, "L": 2, "H": 1, "V": 1, "A": 7, "C": 6, "Z": 0}


def _arc(x1, y1, rx, ry, phi, large, sweep, x2, y2) -> list[tuple[float, float]]:
    """Points along an SVG elliptical arc (endpoint form, SVG 1.1 appendix F.6.5)."""
    if not rx or not ry:
        return [(x2, y2)]
    cp, sp = math.cos(math.radians(phi)), math.sin(math.radians(phi))
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
    rx, ry = abs(rx), abs(ry)
    lam = x1p**2 / rx**2 + y1p**2 / ry**2
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        coef = -coef
    cxp, cyp = coef * rx * y1p / ry, -coef * ry * x1p / rx
    cx, cy = cp * cxp - sp * cyp + (x1 + x2) / 2, sp * cxp + cp * cyp + (y1 + y2) / 2

    def angle(ux, uy, vx, vy):
        return math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)

    t1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = angle((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    steps = max(4, int(abs(dt) / (math.pi / 24)))
    out = []
    for i in range(1, steps + 1):
        t = t1 + dt * i / steps
        out.append(
            (cp * rx * math.cos(t) - sp * ry * math.sin(t) + cx, sp * rx * math.cos(t) + cp * ry * math.sin(t) + cy)
        )
    return out


def path_polylines(d: str) -> list[tuple[list[tuple[float, float]], bool]]:
    """An SVG path (M L H V A C Z, absolute and relative) as polylines: [(points, closed)]."""
    tokens = _TOKEN.findall(d)
    out: list[tuple[list[tuple[float, float]], bool]] = []
    pts: list[tuple[float, float]] = []
    x = y = sx = sy = 0.0
    cmd = ""
    i = 0
    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
        up, rel = cmd.upper(), cmd.islower()
        n = _ARGS[up]
        if up == "Z":
            if pts:
                out.append((pts, True))
            pts, x, y = [], sx, sy
            continue
        args = [float(t) for t in tokens[i : i + n]]
        i += n
        if up == "M":
            if pts:
                out.append((pts, False))
            x, y = (x + args[0], y + args[1]) if rel else (args[0], args[1])
            sx, sy = x, y
            pts = [(x, y)]
            cmd = "l" if rel else "L"
        elif up == "L":
            x, y = (x + args[0], y + args[1]) if rel else (args[0], args[1])
            pts.append((x, y))
        elif up == "H":
            x = x + args[0] if rel else args[0]
            pts.append((x, y))
        elif up == "V":
            y = y + args[0] if rel else args[0]
            pts.append((x, y))
        elif up == "A":
            ex, ey = (x + args[5], y + args[6]) if rel else (args[5], args[6])
            pts += _arc(x, y, args[0], args[1], args[2], int(args[3]), int(args[4]), ex, ey)
            x, y = ex, ey
        elif up == "C":
            c = [(x + args[k], y + args[k + 1]) if rel else (args[k], args[k + 1]) for k in (0, 2, 4)]
            for s in range(1, 13):
                t = s / 12
                a, b = (1 - t), t
                px = a**3 * x + 3 * a * a * b * c[0][0] + 3 * a * b * b * c[1][0] + b**3 * c[2][0]
                py = a**3 * y + 3 * a * a * b * c[0][1] + 3 * a * b * b * c[1][1] + b**3 * c[2][1]
                pts.append((px, py))
            x, y = c[2]
    if pts:
        out.append((pts, False))
    return out


def _round_rect_points(x, y, w, h, r) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for cx, cy, start in (
        (x + w - r, y + r, -90),
        (x + w - r, y + h - r, 0),
        (x + r, y + h - r, 90),
        (x + r, y + r, 180),
    ):
        for k in range(7):
            a = math.radians(start + k * 15)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return [*pts, pts[0]]


def _dashes(points, on: float, off: float) -> list[list[tuple[float, float]]]:
    out, cur, pos, drawing = [], [points[0]], 0.0, True
    left = on
    for (ax, ay), (bx, by) in itertools.pairwise(points):
        seg = math.hypot(bx - ax, by - ay)
        t = 0.0
        while seg - t > 1e-9:
            step = min(left, seg - t)
            t += step
            left -= step
            p = (ax + (bx - ax) * t / seg, ay + (by - ay) * t / seg)
            if drawing:
                cur.append(p)
            if left <= 1e-9:
                if drawing:
                    out.append(cur)
                drawing = not drawing
                left = on if drawing else off
                cur = [p]
        pos += seg
    if drawing and len(cur) > 1:
        out.append(cur)
    return out


def icon_image(name: str, color: str, size: int = 16, bg: str | None = None) -> Image.Image:
    """Icon ``name`` at ``size`` px in ``color`` (1.5 px round strokes), on ``bg`` or transparent."""
    k = size * SS / 24
    width = 1.5 * SS
    mask = Image.new("L", (size * SS, size * SS), 0)
    d = ImageDraw.Draw(mask)

    def stroke(points: list[tuple[float, float]]) -> None:
        pts = [(px * k, py * k) for px, py in points]
        if len(pts) > 1:
            d.line(pts, fill=255, width=round(width), joint="curve")
        rad = width / 2
        for px, py in pts:
            d.ellipse((px - rad, py - rad, px + rad, py + rad), fill=255)

    for item in ICONS[name]:
        if item[0] == "path":
            for points, closed in path_polylines(item[1]):
                stroke([*points, points[0]] if closed else points)
        elif item[0] == "circle":
            _, cx, cy, r = item
            stroke(
                [(cx + r * math.cos(a / 48 * 2 * math.pi), cy + r * math.sin(a / 48 * 2 * math.pi)) for a in range(49)]
            )
        elif item[0] == "rect":
            _, x, y, w, h, r, dash = item
            points = _round_rect_points(x, y, w, h, r)
            for part in _dashes(points, *dash) if dash else [points]:
                stroke(part)
    mask = mask.resize((size, size), Image.BOX)
    out = Image.new("RGBA", (size, size), (*_rgb(color), 0))
    out.putalpha(mask)
    if bg:
        base = Image.new("RGBA", (size, size), (*_rgb(bg), 255))
        base.alpha_composite(out)
        return base.convert("RGB")
    return out


def icon(name: str, color: str, size: int = 16, bg: str = BG) -> tk.PhotoImage:
    key = ("icon", name, color, size, bg)
    if key not in _cache:
        _cache[key] = photo(icon_image(name, color, size, bg))
    return _cache[key]


def app_icon_image(size: int = 64) -> Image.Image:
    """OpenFill's icon: a white four-point sparkle (and a small one) on a blue rounded square."""
    big = size * SS
    im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, big - 1, big - 1), radius=big * 0.2, fill=(*_rgb(ACCENT), 255))
    for c, r, w in ((big / 2, big * 0.36, big * 0.08), (None, big * 0.12, big * 0.035)):
        cx, cy = (c, c) if c is not None else (big * 0.76, big * 0.76 - big * 0.48)
        d.polygon(
            [
                (cx, cy - r),
                (cx + w, cy - w),
                (cx + r, cy),
                (cx + w, cy + w),
                (cx, cy + r),
                (cx - w, cy + w),
                (cx - r, cy),
                (cx - w, cy - w),
            ],
            fill="white",
        )
    return im.resize((size, size), Image.LANCZOS)


def app_icon(size: int, bg: str) -> tk.PhotoImage:
    key = ("app", size, bg)
    if key not in _cache:
        base = Image.new("RGBA", (size, size), (*_rgb(bg), 255))
        base.alpha_composite(app_icon_image(size))
        _cache[key] = photo(base.convert("RGB"))
    return _cache[key]


# ---- simple parts ------------------------------------------------------------------------------


def frame(parent: tk.Misc, bg: str = BG, **kw) -> tk.Frame:
    return tk.Frame(parent, bg=bg, bd=0, highlightthickness=0, **kw)


LINE_HEIGHT = {11: 14, 12: 16, 13: 18}  # px line height for each font size, as in the design


class Para(tk.Canvas):
    """Text with the design's line height (Tk's labels use the font's, a pixel or two more):
    wrapped at ``wrap`` px (0: one line, as wide as the text). ``configure(text=, fg=, font=)``
    and ``cget("text")`` work as on a label."""

    def __init__(
        self,
        parent: tk.Misc,
        value: str = "",
        font: tkfont.Font | None = None,
        fg: str = TEXT,
        bg: str = BG,
        wrap: int = 0,
        anchor: str = "w",
        justify: str = "left",
        **kw: Any,
    ) -> None:
        super().__init__(parent, width=1, height=1, bg=bg, bd=0, highlightthickness=0, **kw)
        self._text, self._fg, self._font = value, fg, font or fonts(parent).body
        self._wrap, self._center = wrap, anchor == "center" or justify == "center"
        self._render()

    def _lines(self) -> list[str]:
        f, out = self._font, []
        for para in self._text.split("\n"):
            if not self._wrap:
                out.append(para)
                continue
            line = ""
            for word in para.split(" "):
                trial = f"{line} {word}" if line else word
                if f.measure(trial) <= self._wrap or not line:
                    line = trial
                else:
                    out.append(line)
                    line = word
                while f.measure(line) > self._wrap and len(line) > 1:  # a word longer than the line
                    cut = len(line)
                    while cut > 1 and f.measure(line[:cut]) > self._wrap:
                        cut -= 1
                    out.append(line[:cut])
                    line = line[cut:]
            out.append(line)
        return out

    def _render(self) -> None:
        self.delete("all")
        size = abs(int(self._font.cget("size")))
        lh = LINE_HEIGHT.get(size, self._font.metrics("linespace"))
        lines = self._lines() if self._text else []
        width = self._wrap or max([self._font.measure(t) for t in lines] or [0])
        super().configure(width=max(1, width), height=max(1, lh * len(lines)) if lines else 1)
        for i, line in enumerate(lines):
            x, anchor = (width / 2, "center") if self._center else (0, "w")
            self.create_text(x, i * lh + lh / 2, text=line, font=self._font, fill=self._fg, anchor=anchor)

    def configure(self, cnf=None, **kw):  # type: ignore[override]
        changed = False
        for key, attr in (("text", "_text"), ("fg", "_fg"), ("foreground", "_fg"), ("font", "_font")):
            if key in kw:
                value = kw.pop(key)
                if getattr(self, attr) != value:
                    setattr(self, attr, value)
                    changed = True
        if "wraplength" in kw:
            self._wrap, changed = int(kw.pop("wraplength")), True
        if cnf or kw:
            super().configure(cnf, **kw)
        if changed:
            self._render()

    config = configure

    def cget(self, key: str) -> Any:
        if key == "text":
            return self._text
        if key in ("fg", "foreground"):
            return self._fg
        return super().cget(key)


def text(parent: tk.Misc, value: str = "", font=None, fg: str = TEXT, bg: str = BG, wrap: int = 0, **kw) -> Para:
    """Text, left aligned, wrapping at ``wrap`` px when given."""
    return Para(parent, value, font or fonts(parent).body, fg, bg, wrap, **kw)


def small(parent: tk.Misc, value: str = "", fg: str = DIM, bg: str = BG, wrap: int = CW, **kw) -> Para:
    """12 px text (hints and status lines), grey by default."""
    return Para(parent, value, fonts(parent).small, fg, bg, wrap, **kw)


def caption(parent: tk.Misc, value: str, bg: str = BG) -> Para:
    """An 11 px uppercase section label."""
    return Para(parent, value.upper(), fonts(parent).label, DIM, bg)


def divider(parent: tk.Misc, color: str = LINE) -> tk.Frame:
    return tk.Frame(parent, bg=color, height=1, bd=0, highlightthickness=0)


def gap(parent: tk.Misc, px: int, bg: str = BG) -> tk.Frame:
    return tk.Frame(parent, bg=bg, height=px, width=1, bd=0, highlightthickness=0)


def round_corners(widget: tk.Misc, r: int, inside: str, outside: str, border: str | None = None) -> list[tk.Label]:
    """Round a rectangular frame's corners: four small images placed over them."""
    size = 2 * r + 2
    whole = rrect_image(size, size, r, inside, outside, border)
    labels = []
    for ix, iy in ((0, 0), (1, 0), (0, 1), (1, 1)):
        part = whole.crop((ix * (size - r), iy * (size - r), ix * (size - r) + r, iy * (size - r) + r))
        key = ("corner", r, inside, outside, border, ix, iy)
        if key not in _cache:
            _cache[key] = photo(part)
        lab = tk.Label(widget, image=_cache[key], bd=0, highlightthickness=0, padx=0, pady=0, bg=outside)
        lab.place(relx=ix, rely=iy, x=-r * ix, y=-r * iy, width=r, height=r)
        labels.append(lab)
    return labels


class Card(tk.Frame):
    """The job and setup card: PANEL, 12 px padding, rounded corners."""

    def __init__(self, parent: tk.Misc, width: int = CW, bg: str = BG) -> None:
        super().__init__(parent, bg=PANEL, bd=0, highlightthickness=0, width=width)
        self.inner = frame(self, PANEL)
        self.inner.pack(fill="x", padx=12, pady=12)
        self._corners = round_corners(self, 4, PANEL, bg)

    def lift_corners(self) -> None:
        for c in self._corners:
            c.lift()


class Box(tk.Frame):
    """A list box: 1 px LINE border with rounded corners; rows go in ``inner``."""

    def __init__(self, parent: tk.Misc, bg: str = BG) -> None:
        super().__init__(parent, bg=LINE, bd=0, highlightthickness=0)
        self.inner = frame(self, bg)
        self.inner.pack(fill="x", padx=1, pady=1)
        self._corners = round_corners(self, 4, bg, BG, LINE)

    def lift_corners(self) -> None:
        for c in self._corners:
            c.lift()


# ---- controls -------------------------------------------------------------------------------------


class Seg(tk.Canvas):
    """A segmented control: one of ``items`` (key, label) is selected. 32 px tall."""

    def __init__(
        self,
        parent: tk.Misc,
        items: Sequence[tuple[Any, str]],
        command: Callable[[Any], None] | None = None,
        width: int = CW,
        size: int = 13,
        fit: bool = False,
        bg: str = BG,
    ) -> None:
        super().__init__(parent, width=width, height=32, bg=bg, bd=0, highlightthickness=0)
        self.items, self.command, self.w, self.size, self.fit, self.bgc = list(items), command, width, size, fit, bg
        self.value: Any = self.items[0][0] if self.items else None
        f = fonts(self)
        self.f_on, self.f_off = (f.bold, f.body) if size == 13 else (f.small_bold, f.small)
        self.bind("<Button-1>", self._click)
        self._cells: list[tuple[int, int]] = []
        self._draw()

    def _layout(self) -> list[tuple[int, int]]:
        n = len(self.items)
        inner = self.w - 4 - 2 * (n - 1)
        if not self.fit:
            edges = [round(inner * i / n) for i in range(n + 1)]
            return [(2 + edges[i] + 2 * i, edges[i + 1] - edges[i]) for i in range(n)]
        pad = 8 if self.size == 13 else 4
        base = [self.f_on.measure(label) + 2 * pad for _k, label in self.items]
        extra = (inner - sum(base)) / n
        widths, x, out = [b + extra for b in base], 2.0, []
        for w in widths:
            out.append((round(x), round(x + w) - round(x)))
            x += w + 2
        return out

    def _draw(self) -> None:
        self.delete("all")
        self.create_image(0, 0, image=rrect(self.w, 32, 4, PANEL, self.bgc), anchor="nw")
        self._cells = self._layout()
        for (key, label), (x, w) in zip(self.items, self._cells, strict=False):
            on = key == self.value
            if on:
                self.create_image(x, 2, image=rrect(w, 28, 3, SEG_ON, PANEL), anchor="nw")
            font = self.f_on if on else self.f_off
            self.create_text(
                x + w / 2, 16, text=ellipsize(label, font, w - 4), font=font, fill=TEXT if on else DIM, anchor="center"
            )

    def set(self, key: Any) -> None:
        if key != self.value:
            self.value = key
            self._draw()

    def get(self) -> Any:
        return self.value

    def invoke(self, key: Any) -> None:
        self.set(key)
        if self.command:
            self.command(key)

    def _click(self, event) -> None:
        for (key, _label), (x, w) in zip(self.items, self._cells, strict=False):
            if x - 1 <= event.x < x + w + 1:
                self.invoke(key)
                return


class Button(tk.Canvas):
    """A button. kind: primary (36 px, the bottom actions), secondary, accent (a small primary)."""

    def __init__(
        self,
        parent: tk.Misc,
        label: str,
        command: Callable[[], Any] | None = None,
        kind: str = "secondary",
        width: int | None = None,
        height: int | None = None,
        bg: str = BG,
    ) -> None:
        self.kind, self.label, self.command, self.bgc = kind, label, command, bg
        self.h = height or (36 if kind == "primary" else 32)
        f = fonts(parent)
        self.font = f.bold if kind in ("primary", "accent") else f.body
        self.w = width or (self.font.measure(label) + 24)
        super().__init__(parent, width=self.w, height=self.h, bg=bg, bd=0, highlightthickness=0, cursor="hand2")
        self.disabled = False
        self.hover = False
        self.bind("<Enter>", lambda _e: self._hover(True))
        self.bind("<Leave>", lambda _e: self._hover(False))
        self.bind("<ButtonRelease-1>", self._release)
        self._draw()

    def _colors(self) -> tuple[str, str, str | None]:
        if self.kind in ("primary", "accent"):
            if self.disabled:
                return LINE, DIM, None
            return (ACCENT_HOVER if self.hover else ACCENT), WHITE, None
        if self.disabled:
            return self.bgc, DIM, LINE
        return (SEG_ON if self.hover else PANEL), TEXT, LINE

    def _draw(self) -> None:
        self.delete("all")
        fill, fg, border = self._colors()
        self.create_image(0, 0, image=rrect(self.w, self.h, 4, fill, self.bgc, border), anchor="nw")
        self.create_text(
            self.w / 2, self.h / 2, text=ellipsize(self.label, self.font, self.w - 16), font=self.font, fill=fg
        )
        self.configure(cursor="" if self.disabled else "hand2")

    def _hover(self, on: bool) -> None:
        self.hover = on
        self._draw()

    def _release(self, event) -> None:
        if 0 <= event.x < self.w and 0 <= event.y < self.h:
            self.invoke()

    def invoke(self) -> Any:
        if not self.disabled and self.command:
            return self.command()
        return None

    def set(self, label: str | None = None, width: int | None = None, disabled: bool | None = None) -> None:
        changed = False
        if label is not None and label != self.label:
            self.label, changed = label, True
        if width is not None and width != self.w:
            self.w, changed = width, True
            self.configure(width=width)
        if disabled is not None and disabled != self.disabled:
            self.disabled, changed = disabled, True
        if changed:
            self._draw()

    def cget_text(self) -> str:
        return self.label


class IconButton(tk.Canvas):
    """A 28 px square button with a 16 px icon; ``active`` gives it the panel colour."""

    def __init__(
        self, parent: tk.Misc, name: str, command: Callable[[], Any] | None = None, bg: str = BG, active: bool = False
    ) -> None:
        super().__init__(parent, width=28, height=28, bg=bg, bd=0, highlightthickness=0, cursor="hand2")
        self.name, self.command, self.bgc, self.active, self.hover = name, command, bg, active, False
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", lambda e: self.invoke() if 0 <= e.x < 28 and 0 <= e.y < 28 else None)
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        filled = self.active or self.hover
        back = PANEL if filled else self.bgc
        if filled:
            self.create_image(0, 0, image=rrect(28, 28, 4, PANEL, self.bgc), anchor="nw")
        self.create_image(
            6, 6, image=icon(self.name, TEXT if self.active or self.hover else DIM, 16, back), anchor="nw"
        )

    def _set_hover(self, on: bool) -> None:
        self.hover = on
        self._draw()

    def set_active(self, on: bool) -> None:
        if on != self.active:
            self.active = on
            self._draw()

    def invoke(self) -> Any:
        return self.command() if self.command else None


class Field(tk.Canvas):
    """A one-line input, 32 px tall, with a grey placeholder while it is empty."""

    def __init__(
        self,
        parent: tk.Misc,
        width: int = CW,
        placeholder: str = "",
        secret: bool = False,
        value: str = "",
        bg: str = BG,
        on_enter: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(parent, width=width, height=32, bg=bg, bd=0, highlightthickness=0)
        self.w, self.bgc, self.placeholder, self.secret = width, bg, placeholder, secret
        f = fonts(self)
        self.entry = tk.Entry(
            self,
            bg=PANEL,
            fg=TEXT,
            insertbackground=TEXT,
            selectbackground=ACCENT,
            selectforeground=WHITE,
            disabledbackground=PANEL,
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=f.body,
        )
        self._bgimg = self.create_image(0, 0, anchor="nw")
        self.create_window(10, 16, window=self.entry, anchor="w", width=width - 20, height=20)
        self.showing_placeholder = False
        self.entry.bind("<FocusIn>", lambda _e: self._focus(True), add="+")
        self.entry.bind("<FocusOut>", lambda _e: self._focus(False), add="+")
        self.entry.bind("<KeyPress>", self._key, add="+")
        self.entry.bind("<<Paste>>", lambda _e: self._clear_placeholder(), add="+")
        self.entry.bind("<Button-1>", self._click, add="+")
        self.bind("<Button-1>", lambda _e: self.entry.focus_force())
        if on_enter:
            self.entry.bind("<Return>", lambda _e: on_enter())
            self.entry.bind("<KP_Enter>", lambda _e: on_enter())
        self._focused = False
        self._draw()
        self.set(value)

    def _draw(self) -> None:
        self.itemconfigure(self._bgimg, image=rrect(self.w, 32, 4, PANEL, self.bgc, ACCENT if self._focused else LINE))

    def _focus(self, on: bool) -> None:
        self._focused = on
        self._draw()
        if on and self.showing_placeholder:
            self.entry.icursor(0)

    def _click(self, _event) -> str | None:
        self.entry.focus_force()
        if self.showing_placeholder:
            self.entry.icursor(0)
            return "break"
        return None

    def _show_placeholder(self) -> None:
        self.showing_placeholder = bool(self.placeholder)
        if self.placeholder:
            self.entry.configure(show="", fg=DIM)
            self.entry.insert(0, self.placeholder)
            self.entry.icursor(0)

    def _clear_placeholder(self) -> None:
        if self.showing_placeholder:
            self.showing_placeholder = False
            self.entry.delete(0, "end")
            self.entry.configure(fg=TEXT, show="*" if self.secret else "")

    def _key(self, event) -> str | None:
        if event.keysym in ("Return", "KP_Enter", "Tab", "Escape", "Up", "Down", "Shift_L", "Shift_R"):
            return None
        if self.showing_placeholder:
            if event.keysym in ("BackSpace", "Delete", "Left", "Right", "Home", "End"):
                return "break"
            if event.char and event.char.isprintable():
                self._clear_placeholder()
            return None
        if event.keysym in ("BackSpace", "Delete"):
            self.after_idle(self._maybe_empty)
        return None

    def _maybe_empty(self) -> None:
        if not self.entry.get() and not self.showing_placeholder:
            self._show_placeholder()

    def get(self) -> str:
        return "" if self.showing_placeholder else self.entry.get()

    def set(self, value: str) -> None:
        self.showing_placeholder = False
        self.entry.delete(0, "end")
        self.entry.configure(fg=TEXT, show="*" if self.secret else "")
        if value:
            self.entry.insert(0, value)
        else:
            self._show_placeholder()


class PromptBox(tk.Canvas):
    """The prompt: three lines, PANEL fill, 1 px LINE border, a grey placeholder inside.

    Enter submits (``on_submit``), Shift+Enter adds a line; Up on the first line and Down
    on the last go through the earlier prompts (``on_recall(step)``)."""

    def __init__(
        self,
        parent: tk.Misc,
        on_submit: Callable[[], Any],
        on_recall: Callable[[int], Any],
        width: int = CW,
        height: int = 76,
        bg: str = BG,
    ) -> None:
        super().__init__(parent, width=width, height=height, bg=bg, bd=0, highlightthickness=0)
        self.w, self.h, self.bgc = width, height, bg
        f = fonts(self)
        self.text = tk.Text(
            self,
            bg=PANEL,
            fg=TEXT,
            insertbackground=TEXT,
            selectbackground=ACCENT,
            selectforeground=WHITE,
            inactiveselectbackground=SEG_ON,
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=0,
            pady=0,
            wrap="word",
            font=f.body,
            spacing1=0,
            spacing3=0,
            undo=True,
        )
        self.text.tag_configure("placeholder", foreground=PLACEHOLDER_FG)
        self._bgimg = self.create_image(0, 0, anchor="nw")
        self.create_window(10, 8, window=self.text, anchor="nw", width=width - 20, height=height - 16)
        self.placeholder = ""
        self.showing_placeholder = False
        self._focused = False
        self.on_submit, self.on_recall = on_submit, on_recall
        t = self.text
        t.bind("<FocusIn>", lambda _e: self._focus(True), add="+")
        t.bind("<FocusOut>", lambda _e: self._focus(False), add="+")
        t.bind("<Return>", lambda _e: (self.on_submit(), "break")[1])
        t.bind("<KP_Enter>", lambda _e: (self.on_submit(), "break")[1])
        t.bind("<Shift-Return>", self._newline)
        t.bind("<Up>", lambda _e: self._arrow(1))
        t.bind("<Down>", lambda _e: self._arrow(-1))
        t.bind("<KeyPress>", self._key, add="+")
        t.bind("<<Paste>>", lambda _e: self._clear_placeholder(), add="+")
        t.bind("<Button-1>", self._click, add="+")
        t.bind("<Tab>", lambda _e: (t.tk_focusNext().focus_set(), "break")[1])
        self.bind("<Button-1>", lambda _e: t.focus_force())
        self._draw()
        self._show_placeholder()

    def _draw(self) -> None:
        self.itemconfigure(self._bgimg, image=rrect(self.w, self.h, 4, PANEL, self.bgc, LINE))

    def _focus(self, on: bool) -> None:
        self._focused = on
        if on and self.showing_placeholder:
            self.text.mark_set("insert", "1.0")

    def _click(self, _event) -> str | None:
        self.text.focus_force()
        if self.showing_placeholder:
            self.text.mark_set("insert", "1.0")
            return "break"
        return None

    def _newline(self, _event) -> str:
        self._clear_placeholder()
        self.text.insert("insert", "\n")
        return "break"

    def _arrow(self, step: int) -> str | None:
        if self.showing_placeholder:
            return self.on_recall(step)
        line = int(self.text.index("insert").split(".")[0])
        last = int(self.text.index("end-1c").split(".")[0])
        if (step > 0 and line == 1) or (step < 0 and line == last):
            return self.on_recall(step)
        return None

    def set_placeholder(self, value: str) -> None:
        self.placeholder = value
        if self.showing_placeholder or not self.get():
            self._show_placeholder()

    def _show_placeholder(self) -> None:
        self.text.delete("1.0", "end")
        self.showing_placeholder = bool(self.placeholder)
        if self.placeholder:
            self.text.insert("1.0", self.placeholder, "placeholder")
            self.text.mark_set("insert", "1.0")
        self.text.edit_reset()

    def _clear_placeholder(self) -> None:
        if self.showing_placeholder:
            self.showing_placeholder = False
            self.text.delete("1.0", "end")

    def _key(self, event) -> str | None:
        if event.keysym in ("Return", "KP_Enter", "Tab", "Escape", "Up", "Down", "Shift_L", "Shift_R"):
            return None
        if self.showing_placeholder:
            if event.keysym in ("BackSpace", "Delete", "Left", "Right", "Home", "End", "Prior", "Next"):
                return "break"
            if event.char and event.char.isprintable():
                self._clear_placeholder()
            return None
        if event.keysym in ("BackSpace", "Delete") or (event.state & 4 and event.keysym.lower() in ("x", "z")):
            self.after_idle(self._maybe_empty)
        return None

    def _maybe_empty(self) -> None:
        if not self.showing_placeholder and not self.text.get("1.0", "end-1c"):
            self._show_placeholder()

    def get(self) -> str:
        return "" if self.showing_placeholder else self.text.get("1.0", "end-1c")

    def set(self, value: str) -> None:
        self.showing_placeholder = False
        self.text.delete("1.0", "end")
        if value:
            self.text.insert("1.0", value)
            self.text.mark_set("insert", "end-1c")
        else:
            self._show_placeholder()

    def select_all(self) -> None:
        if not self.showing_placeholder:
            self.text.tag_add("sel", "1.0", "end-1c")
            self.text.mark_set("insert", "end-1c")

    def focus(self) -> None:
        self.text.focus_set()


class Checkbox(tk.Canvas):
    """A 16 px check box and its text, 32 px tall."""

    def __init__(self, parent: tk.Misc, label: str, width: int = CW, bg: str = BG) -> None:
        super().__init__(parent, width=width, height=32, bg=bg, bd=0, highlightthickness=0, cursor="hand2")
        self.var = tk.BooleanVar(self, False)
        self.label, self.bgc = label, bg
        self.bind("<Button-1>", lambda _e: self.var.set(not self.var.get()))
        self.var.trace_add("write", lambda *_a: self._draw())
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        on = self.var.get()
        if on:
            self.create_image(0, 8, image=rrect(16, 16, 3, ACCENT, self.bgc), anchor="nw")
            self.create_image(2, 10, image=icon("check", WHITE, 12, ACCENT), anchor="nw")
        else:
            self.create_image(0, 8, image=rrect(16, 16, 3, PANEL, self.bgc, LINE), anchor="nw")
        self.create_text(26, 16, text=self.label, font=fonts(self).body, fill=TEXT, anchor="w")

    def get(self) -> bool:
        return bool(self.var.get())

    def set(self, value: bool) -> None:
        self.var.set(bool(value))


class Progress(tk.Canvas):
    """A 4 px progress bar (LINE track, rounded ends)."""

    def __init__(self, parent: tk.Misc, width: int, bg: str = PANEL) -> None:
        super().__init__(parent, width=width, height=4, bg=bg, bd=0, highlightthickness=0)
        self.w, self.bgc = width, bg
        self.value, self.color = -1.0, ACCENT
        self.set(0.0)

    def set(self, fraction: float, color: str = ACCENT) -> None:
        fraction = max(0.0, min(1.0, fraction))
        if abs(fraction - self.value) < 0.002 and color == self.color:
            return
        self.value, self.color = fraction, color
        self.delete("all")
        self.create_image(0, 0, image=rrect(self.w, 4, 2, LINE, self.bgc), anchor="nw")
        fill = round(self.w * fraction)
        if fill >= 1:
            # the filled part is cut by the track's rounded ends, as overflow: hidden does
            key = ("bar", self.w, fill, color, self.bgc)
            if key not in _cache:
                track = rrect_image(self.w, 4, 2, LINE, self.bgc)
                full = rrect_image(self.w, 4, 2, color, self.bgc)
                track.paste(full.crop((0, 0, fill, 4)), (0, 0))
                if len(_cache) > 600:
                    _cache.clear()
                _cache[key] = photo(track)
            self.create_image(0, 0, image=_cache[key], anchor="nw")


class Select(tk.Canvas):
    """A select, 32 px tall: the current choice and a chevron; a click opens the list of
    choices below it (``manage`` adds a divider and "Manage models...")."""

    def __init__(
        self,
        parent: tk.Misc,
        width: int = FIELD_W,
        command: Callable[[str], Any] | None = None,
        manage: Callable[[], Any] | None = None,
        bg: str = BG,
    ) -> None:
        super().__init__(parent, width=width, height=32, bg=bg, bd=0, highlightthickness=0, cursor="hand2")
        self.w, self.bgc, self.command, self.manage = width, bg, command, manage
        self.values: list[str] = []
        self.value = ""
        self.popdown: Popdown | None = None
        self.bind("<Button-1>", lambda _e: self.toggle())
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        self.create_image(
            0, 0, image=rrect(self.w, 32, 4, PANEL, self.bgc, ACCENT if self.popdown else LINE), anchor="nw"
        )
        f = fonts(self).body
        self.create_text(10, 16, text=ellipsize(self.value, f, self.w - 38), font=f, fill=TEXT, anchor="w")
        self.create_image(self.w - 24, 8, image=icon("chevron", DIM, 16, PANEL), anchor="nw")

    def set_values(self, values: list[str]) -> None:
        self.values = list(values)

    def set(self, value: str) -> None:
        if value != self.value:
            self.value = value
            self._draw()

    def get(self) -> str:
        return self.value

    def toggle(self) -> None:
        if self.popdown is not None:
            self.close()
            return
        if not self.values and not self.manage:
            return
        self.popdown = Popdown(self)
        self._draw()

    def close(self) -> None:
        if self.popdown is not None:
            pop, self.popdown = self.popdown, None
            pop.destroy_quietly()
            self._draw()

    def pick(self, value: str) -> None:
        self.close()
        if value == MANAGE:
            if self.manage:
                self.manage()
            return
        self.set(value)
        if self.command:
            self.command(value)


MANAGE = "Manage models..."


class Popdown(tk.Toplevel):
    """A select's open list: 28 px rows, the current one in the accent colour."""

    ROW = 28

    def __init__(self, select: Select) -> None:
        super().__init__(select, bg=BG)
        self.select = select
        self.withdraw()
        self.tk.call("wm", "overrideredirect", self._w, True)
        try:
            self.attributes("-topmost", True)
        except tk.TclError:
            pass
        rows = [*select.values]
        self.rows = rows + ([MANAGE] if select.manage else [])
        self.w = select.w + 2
        extra = 7 if select.manage and rows else 0
        self.h = 8 + 2 + len(self.rows) * self.ROW + max(0, len(self.rows) - 1) + extra
        self.canvas = tk.Canvas(self, width=self.w, height=self.h, bg=BG, bd=0, highlightthickness=0)
        self.canvas.pack()
        self.hover = -1
        self._tops: list[int] = []
        y = 5
        for i, _row in enumerate(self.rows):
            if select.manage and i == len(rows) and rows:
                y += 7  # a divider before "Manage models..."
            self._tops.append(y)
            y += self.ROW + 1
        self._draw()
        x, top = select.winfo_rootx() - 1, select.winfo_rooty() + 34
        if top + self.h > select.winfo_screenheight():
            top = select.winfo_rooty() - 2 - self.h
        self.geometry(f"{self.w}x{self.h}+{x}+{top}")
        self.deiconify()
        self.lift()
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda _e: self._set_hover(-1))
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.bind("<ButtonPress-1>", self._press_outside)
        self.bind("<Escape>", lambda _e: select.close())
        self.bind("<Return>", lambda _e: self._choose(self.hover))
        self.bind("<Up>", lambda _e: self._set_hover(max(0, self.hover - 1)))
        self.bind("<Down>", lambda _e: self._set_hover(min(len(self.rows) - 1, self.hover + 1)))
        self.after(10, self._grab)

    def _grab(self) -> None:
        try:
            self.grab_set()
            self.focus_force()
        except tk.TclError:
            pass

    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        c.create_image(0, 0, image=rrect(self.w, self.h, 4, PANEL, BG, LINE), anchor="nw")
        f = fonts(self).body
        current = self.select.value
        n_values = len(self.select.values)
        if self.select.manage and n_values:
            y = self._tops[n_values] - 4
            c.create_line(5, y, self.w - 5, y, fill=LINE)
        for i, (row, top) in enumerate(zip(self.rows, self._tops, strict=False)):
            manage = row == MANAGE and i >= n_values
            on = row == current and not manage
            if on or i == self.hover:
                c.create_image(
                    5, top, image=rrect(self.w - 10, self.ROW, 3, ACCENT if on else SEG_ON, PANEL), anchor="nw"
                )
            color = WHITE if on else (DIM if manage and i != self.hover else TEXT)
            c.create_text(15, top + self.ROW / 2, text=ellipsize(row, f, self.w - 30), font=f, fill=color, anchor="w")

    def _row_at(self, y: int) -> int:
        for i, top in enumerate(self._tops):
            if top <= y < top + self.ROW:
                return i
        return -1

    def _set_hover(self, index: int) -> None:
        if index != self.hover:
            self.hover = index
            self._draw()

    def _motion(self, event) -> None:
        self._set_hover(self._row_at(event.y))

    def _release(self, event) -> None:
        self._choose(self._row_at(event.y))

    def _choose(self, index: int) -> None:
        if 0 <= index < len(self.rows):
            self.select.pick(self.rows[index])

    def _press_outside(self, event) -> None:
        x, y = event.x_root - self.winfo_rootx(), event.y_root - self.winfo_rooty()
        if not (0 <= x < self.w and 0 <= y < self.h):
            sx, sy = event.x_root - self.select.winfo_rootx(), event.y_root - self.select.winfo_rooty()
            self.select.close()
            if 0 <= sx < self.select.w and 0 <= sy < 32:
                return  # a click on the select itself only closes it

    def destroy_quietly(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass


# ---- borderless windows --------------------------------------------------------------------------


def make_borderless(window: tk.Wm) -> str:
    """No title bar or frame: OpenFill's own header is the drag handle.

    On X11 (and XWayland) the window manager is asked through _MOTIF_WM_HINTS to leave out
    its decorations, so the window stays a normal managed window (it gets keyboard focus,
    a taskbar entry and Alt+Tab). Where that cannot be set, and on Windows and macOS, Tk's
    override-redirect is used; on Windows the bar is then tied to PhotoCraft's window (an
    owned window), which keeps it above PhotoCraft and minimizes and restores it with it.
    Returns "motif" or "override". Call it before the window is first shown."""
    if sys.platform.startswith("linux") or "bsd" in sys.platform:
        try:
            if str(window.tk.call("tk", "windowingsystem")) == "x11" and _motif_hints(window):
                return "motif"
        except Exception:  # noqa: BLE001 - fall back to override-redirect
            pass
    # Aqua Tk can return "None" for this setter; tkinter tries to parse it as a boolean.
    window.tk.call("wm", "overrideredirect", window._w, True)
    return "override"


def _motif_hints(window: tk.Wm) -> bool:
    """Set _MOTIF_WM_HINTS (no decorations) on ``window``'s frame now and whenever it maps."""
    import ctypes
    import ctypes.util

    name = ctypes.util.find_library("X11") or "libX11.so.6"
    x11 = ctypes.cdll.LoadLibrary(name)
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XInternAtom.restype = ctypes.c_ulong
    x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x11.XChangeProperty.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    x11.XFlush.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    display = x11.XOpenDisplay(str(window.winfo_screen()).encode())
    if not display:
        return False
    x11.XCloseDisplay(display)

    x11.XQueryTree.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
        ctypes.POINTER(ctypes.c_uint),
    ]
    x11.XFree.argtypes = [ctypes.c_void_p]

    def wrapper(dpy) -> int:
        """Tk's wrapper window (the parent of the toplevel's own window): the one the window
        manager manages. ``wm frame`` names the window manager's frame once it has reparented."""
        root, parent = ctypes.c_ulong(), ctypes.c_ulong()
        children, count = ctypes.POINTER(ctypes.c_ulong)(), ctypes.c_uint()
        if not x11.XQueryTree(
            dpy,
            window.winfo_id(),
            ctypes.byref(root),
            ctypes.byref(parent),
            ctypes.byref(children),
            ctypes.byref(count),
        ):
            return 0
        if children:
            x11.XFree(children)
        return parent.value if parent.value and parent.value != root.value else window.winfo_id()

    def apply(_event=None) -> None:
        dpy = x11.XOpenDisplay(str(window.winfo_screen()).encode())
        if not dpy:
            return
        try:
            frame_id = wrapper(dpy)
            if not frame_id:
                return
            atom = x11.XInternAtom(dpy, b"_MOTIF_WM_HINTS", 0)
            hints = (ctypes.c_long * 5)(2, 0, 0, 0, 0)  # flags: decorations; decorations: none
            x11.XChangeProperty(dpy, frame_id, atom, atom, 32, 0, ctypes.cast(hints, ctypes.c_void_p), 5)
            x11.XFlush(dpy)
        finally:
            x11.XCloseDisplay(dpy)

    own_tag(window).bind_class(own_tag_name(window), "<Map>", lambda _e: apply(), add="+")
    window.update_idletasks()
    apply()
    return True


_frozen_depth = 0


class _Curtain:
    """A still picture of the screen around a window, shown just above it while it changes
    (Windows): what is seen stays as it was until the window is completely repainted, then
    the curtain goes in one step. Click-through, never activated, and placed directly above
    the window in z-order, so it never covers anything that covers the window."""

    def __init__(self, window: tk.Misc) -> None:
        import ctypes
        from ctypes import wintypes

        self.ok = False
        self.ct, self.wt = ctypes, wintypes
        u, g = ctypes.windll.user32, ctypes.windll.gdi32
        self.u, self.g = u, g
        vp = ctypes.c_void_p
        u.GetDC.restype, u.GetDC.argtypes = vp, [vp]
        u.ReleaseDC.argtypes = [vp, vp]
        g.CreateCompatibleDC.restype, g.CreateCompatibleDC.argtypes = vp, [vp]
        g.CreateCompatibleBitmap.restype, g.CreateCompatibleBitmap.argtypes = vp, [vp, ctypes.c_int, ctypes.c_int]
        g.SelectObject.restype, g.SelectObject.argtypes = vp, [vp, vp]
        g.BitBlt.argtypes = [
            vp,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            vp,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
        ]
        g.DeleteObject.argtypes = [vp]
        g.DeleteDC.argtypes = [vp]
        u.CreateWindowExW.restype = vp
        u.CreateWindowExW.argtypes = (
            [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD] + [ctypes.c_int] * 4 + [vp, vp, vp, vp]
        )
        u.DestroyWindow.argtypes = [vp]
        u.GetWindowRect.argtypes = [vp, ctypes.POINTER(wintypes.RECT)]
        u.GetWindow.restype, u.GetWindow.argtypes = vp, [vp, wintypes.UINT]
        u.GetAncestor.restype, u.GetAncestor.argtypes = vp, [vp, wintypes.UINT]
        u.SetWindowPos.argtypes = [vp, vp, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.UpdateLayeredWindow.argtypes = [vp, vp, vp, vp, vp, vp, wintypes.DWORD, vp, wintypes.DWORD]
        self.hwnd = self.mem = None
        try:
            self.old_ctx = u.SetThreadDpiAwarenessContext(vp(-4))  # per-monitor aware: real pixels
        except (AttributeError, OSError):
            self.old_ctx = None
        try:
            self._show(window)
        except Exception:
            self.release()  # never leave a picture standing over the panel
            raise
        finally:
            if self.old_ctx:
                u.SetThreadDpiAwarenessContext(self.old_ctx)
        if self.ok:
            # a safety net: the curtain goes after 400 ms even if the change never finishes
            # (a nested event loop, an error that skips the normal path)
            window.after(400, self.release)

    def _show(self, window: tk.Misc) -> None:
        ctypes, wintypes, u, g, vp = self.ct, self.wt, self.u, self.g, self.ct.c_void_p
        frame = u.GetAncestor(vp(window.winfo_id()), 2)  # GA_ROOT: the window Windows manages
        r = wintypes.RECT()
        u.GetWindowRect(frame, ctypes.byref(r))
        sh = u.GetSystemMetrics(1)
        # the window's column, top to bottom of the screen: its new size is not known yet
        # (it keeps its bottom edge and grows upward, or starts at the top of the screen)
        x, y, w, h = r.left, 0, r.right - r.left, sh
        if w <= 0 or h <= 0:
            return
        self.frame = frame
        screen = u.GetDC(None)
        self.mem = g.CreateCompatibleDC(screen)
        self.bmp = g.CreateCompatibleBitmap(screen, w, h)
        self.old = g.SelectObject(self.mem, self.bmp)
        g.BitBlt(self.mem, 0, 0, w, h, screen, x, y, 0x00CC0020 | 0x40000000)  # SRCCOPY | CAPTUREBLT
        u.ReleaseDC(None, screen)
        # WS_POPUP; WS_EX_LAYERED | TRANSPARENT | TOOLWINDOW | NOACTIVATE
        self.hwnd = u.CreateWindowExW(
            0x00080000 | 0x00000020 | 0x00000080 | 0x08000000,
            "Static",
            "OpenFill curtain",
            0x80000000,
            x,
            y,
            w,
            h,
            None,
            None,
            None,
            None,
        )
        if not self.hwnd:
            self.release()
            return
        pos, size, src = wintypes.POINT(x, y), wintypes.SIZE(w, h), wintypes.POINT(0, 0)
        shown = u.UpdateLayeredWindow(
            self.hwnd, None, ctypes.byref(pos), ctypes.byref(size), self.mem, ctypes.byref(src), 0, None, 0x00000004
        )  # ULW_OPAQUE
        above = u.GetWindow(frame, 3)  # GW_HWNDPREV: the window right above the panel
        # just above the panel: SWP_NOMOVE | NOSIZE | NOACTIVATE | SHOWWINDOW
        u.SetWindowPos(self.hwnd, above or vp(0), 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0010 | 0x0040)
        if above:
            u.SetWindowPos(self.hwnd, above, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0010)
        self.ok = bool(shown)
        if not self.ok:
            self.release()

    def release(self) -> None:
        """Take the curtain away (again is harmless)."""
        u, g = self.u, self.g
        if getattr(self, "hwnd", None):
            hwnd, self.hwnd = self.hwnd, None
            try:
                self.ct.windll.dwmapi.DwmFlush()  # the repainted window has been composed once
            except OSError:
                pass
            u.DestroyWindow(hwnd)
        if getattr(self, "mem", None):
            g.SelectObject(self.mem, self.old)
            g.DeleteObject(self.bmp)
            g.DeleteDC(self.mem)
            self.mem = None


@contextlib.contextmanager
def frozen(window: tk.Misc):
    """Change a window's contents and size as one visible step (Windows).

    Every Tk widget is a child window on Windows and repaints on its own, and a resize
    first shows the window's old pixels moved, or black where it grew. So a switch showed
    several partial states. A curtain (a still picture of what is on screen there) is put
    just above the window; drawing is suspended on Tk's own window (WM_SETREDRAW, always
    turned back on) while the contents and size change, so the layout pass is quick; then
    the whole window is repainted in one pass under the curtain, and the curtain goes once
    the new picture has been composed: one visible change. Without a curtain (it could
    not be made) the suspended drawing alone still keeps most partial states out of
    sight. The toplevel's frame is never touched: Tk reads its visibility as the window's
    state. Elsewhere this only runs the body."""
    global _frozen_depth
    if sys.platform != "win32" or _frozen_depth or not window.winfo_viewable():
        _frozen_depth += 1
        try:
            yield
        finally:
            _frozen_depth -= 1
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.RedrawWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _frozen_depth += 1
    curtain = None
    try:
        curtain = _Curtain(window)
    except Exception:  # noqa: BLE001 - without it, suspended drawing alone
        curtain = None
    inner = window.winfo_id()  # Tk's toplevel window, inside the frame Windows manages
    suspended = bool(inner) and bool(user32.IsWindowVisible(inner))
    if suspended:
        user32.SendMessageW(inner, 0x000B, 0, 0)  # WM_SETREDRAW off
    try:
        yield
        window.update_idletasks()  # layout and the new geometry
    finally:
        _frozen_depth -= 1
        if suspended:
            user32.SendMessageW(inner, 0x000B, 1, 0)  # WM_SETREDRAW on (always)
            # RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW
            user32.RedrawWindow(inner, None, None, 0x0001 | 0x0004 | 0x0080 | 0x0100)
        try:
            window.update()  # Tk repaints every widget now, before the curtain goes
        except tk.TclError:
            pass
        if curtain is not None:
            curtain.release()


def own_tag_name(window: tk.Misc) -> str:
    return f"OpenFillTop{id(window)}"


def own_tag(window: tk.Misc) -> tk.Misc:
    """Give a toplevel a bind tag of its own (first in its bindtags), so a <Map> or <Configure>
    binding fires for the window itself only: a binding on the toplevel's name would run for
    every widget inside it (each is tagged with its toplevel), dozens of times per view switch."""
    tag = own_tag_name(window)
    tags = window.bindtags()
    if tag not in tags:
        window.bindtags((tag, *tags))
    return window


def drag_handle(widgets: Sequence[tk.Misc], window: tk.Wm, on_moved: Callable[[], Any] | None = None) -> None:
    """Dragging any of ``widgets`` moves ``window`` (the header of a window without a title bar)."""
    grab: dict[str, int] = {}

    def press(event) -> None:
        grab.update(x=event.x_root, y=event.y_root, wx=window.winfo_x(), wy=window.winfo_y())

    def motion(event) -> None:
        if grab:
            window.geometry(f"+{grab['wx'] + event.x_root - grab['x']}+{grab['wy'] + event.y_root - grab['y']}")

    def release(_event) -> None:
        if grab and on_moved:
            on_moved()
        grab.clear()

    for w in widgets:
        w.bind("<ButtonPress-1>", press, add="+")
        w.bind("<B1-Motion>", motion, add="+")
        w.bind("<ButtonRelease-1>", release, add="+")


# ---- message boxes -------------------------------------------------------------------------------


class Dialog(tk.Toplevel):
    """OpenFill's own message box: its header (icon, name, close), an icon and the text, and
    buttons on the right (the last is the default, in the accent colour)."""

    WIDTH = 380

    def __init__(
        self,
        master: tk.Misc,
        message: str,
        buttons: Sequence[str] = ("OK",),
        kind: str = "info",
        title: str = "OpenFill",
    ) -> None:
        super().__init__(master, bg=LINE)
        self.withdraw()
        self.title(title)
        self.result: str | None = None
        f = fonts(self)
        make_borderless(self)
        try:
            self.attributes("-topmost", True)
        except tk.TclError:
            pass
        try:
            self.iconphoto(False, photo(app_icon_image(32)))
        except tk.TclError:
            pass
        box = frame(self)  # inside a 1 px LINE border
        box.pack(padx=1, pady=1)
        head = frame(box)
        head.pack(fill="x")
        head.configure(height=40, width=self.WIDTH - 2)
        head.pack_propagate(False)
        logo = tk.Label(head, image=app_icon(16, BG), bg=BG, bd=0, highlightthickness=0)
        logo.pack(side="left", padx=(16, 0))
        name = text(head, title, f.bold)
        name.pack(side="left", padx=(10, 0))
        IconButton(head, "close", lambda: self._done(None)).pack(side="right", padx=(0, 6))
        drag_handle((head, logo, name), self)
        divider(box).pack(fill="x")
        body = frame(box)
        body.pack(fill="x", padx=16, pady=16)
        color = {"warn": WARN, "error": BAD}.get(kind, ACCENT)
        mark = tk.Label(body, image=icon({"ask": "warn"}.get(kind, kind), color, 20, BG), bg=BG, bd=0)
        mark.pack(side="left", anchor="n", pady=(1, 0))
        paras = frame(body)
        paras.pack(side="left", fill="x", padx=(12, 0))
        for i, para in enumerate(p for p in message.split("\n\n") if p.strip()):
            text(paras, para.strip(), f.body, wrap=self.WIDTH - 2 - 32 - 32).pack(anchor="w", pady=(10 if i else 0, 0))
        row = frame(box)
        row.pack(fill="x", padx=16, pady=(0, 16))
        self.buttons: dict[str, Button] = {}
        for label in reversed(buttons):
            kind_ = "accent" if label == buttons[-1] else "secondary"
            b = Button(row, label, lambda v=label: self._done(v), kind_, width=96)
            b.pack(side="right", padx=(8, 0) if label != buttons[-1] else 0)
            self.buttons[label] = b
        self.protocol("WM_DELETE_WINDOW", lambda: self._done(None))
        self.bind("<Return>", lambda _e: self._done(buttons[-1]))
        self.bind("<KP_Enter>", lambda _e: self._done(buttons[-1]))
        self.bind("<Escape>", lambda _e: self._done(None))
        self._place(master)

    def _place(self, master: tk.Misc) -> None:
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        try:
            viewable = master.winfo_viewable()
        except tk.TclError:
            viewable = False
        if viewable:
            x = master.winfo_rootx() + (master.winfo_width() - w) // 2
            y = master.winfo_rooty() + (master.winfo_height() - h) // 3
        else:
            x = (self.winfo_screenwidth() - w) // 2
            y = (self.winfo_screenheight() - h) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _done(self, value: str | None) -> None:
        self.result = value
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    def show(self) -> str | None:
        self.deiconify()
        self.lift()
        try:
            self.focus_force()
            self.grab_set()
        except tk.TclError:
            pass
        self.wait_window()
        return self.result


def message(master: tk.Misc, text_: str, kind: str = "info") -> None:
    """An OpenFill message with OK."""
    Dialog(master, text_, ("OK",), kind).show()


def ask(master: tk.Misc, text_: str, ok: str = "OK", kind: str = "warn") -> bool:
    """An OpenFill question with Cancel and ``ok``; True for ``ok``."""
    return Dialog(master, text_, ("Cancel", ok), kind).show() == ok
