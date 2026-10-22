# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""The bar's Expand editor: the GIMP plug-in's visual canvas editor, drawn with Tk.

A thumbnail of the image sits inside the dashed frame of the new canvas. Drag an
edge or a corner outward to add space (inward crops), drag inside the frame to move
it; ratio buttons with a Landscape/Portrait switch set the frame's shape. All the
geometry (margins, presets, handles, drag math) is ``openfill.ui_expand``, shared
with the GIMP dialog; this module only draws it and turns pointer events into calls.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from PIL import Image

from openfill import ui_expand as ue

if TYPE_CHECKING:
    from .bar import Bar

AREA = (392, 250)
ROOM = 0.45  # the image takes this share of the editor at zoom 1, leaving room to drag outward
ZOOM_STEP = 1.2
TK_CURSORS = {
    "left": "sb_h_double_arrow",
    "right": "sb_h_double_arrow",
    "top": "sb_v_double_arrow",
    "bottom": "sb_v_double_arrow",
    "top-left": "top_left_corner",
    "bottom-right": "bottom_right_corner",
    "top-right": "top_right_corner",
    "bottom-left": "bottom_left_corner",
    "move": "fleur",
}


class ExpandView(ttk.Frame):
    """Ratio buttons, the Landscape/Portrait switch and the drag editor for one image."""

    def __init__(self, bar: Bar, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.bar = bar
        self.state = ue.ExpandState(1, 1, listener=self._changed)
        self.image_key: tuple = ()  # (document name, width, height) the state is for
        self.thumbnail: Image.Image | None = None
        self._photo: tk.PhotoImage | None = None
        self._photo_size: tuple[int, int] = (0, 0)
        self._drag: tuple[str, float, float, ue.Margins, ue.View] | None = None
        self.zoom = 1.0
        self._preview: tk.PhotoImage | None = None  # a running expand's live preview, drawn in place

        ratios = ttk.Frame(self)
        ratios.pack(fill="x")
        self.ratio_buttons: dict[str, ttk.Button] = {}
        for button, label in ue.RATIO_BUTTONS:
            b = ttk.Button(ratios, text=label, style="Chip.TButton", command=lambda k=button: self._ratio(k))
            b.pack(side="left", padx=(0, 3))
            self.ratio_buttons[button] = b
        turn = ttk.Frame(self)
        turn.pack(fill="x", pady=(5, 0))
        self.turn_buttons: dict[bool, ttk.Button] = {}
        for portrait, label in ((False, "Landscape"), (True, "Portrait")):
            b = ttk.Button(turn, text=label, style="Chip.TButton", command=lambda p=portrait: self._turn(p))
            b.pack(side="left", padx=(0, 3))
            self.turn_buttons[portrait] = b
        self.size = ttk.Label(turn, style="Dim.TLabel")
        self.size.pack(side="right")

        self.canvas = tk.Canvas(self, width=AREA[0], height=AREA[1], bg=bar.colors["BG"], highlightthickness=0, bd=0)
        self.canvas.pack(fill="x", pady=(6, 0))
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion_drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<MouseWheel>", lambda e: self._zoom_by(ZOOM_STEP if e.delta > 0 else 1 / ZOOM_STEP))
        self.canvas.bind("<Button-4>", lambda _e: self._zoom_by(ZOOM_STEP))
        self.canvas.bind("<Button-5>", lambda _e: self._zoom_by(1 / ZOOM_STEP))
        self.hint = ttk.Label(
            self,
            text="Drag the frame's edges or corners outward, or pick a ratio. Mouse wheel zooms.",
            style="Dim.TLabel",
            wraplength=bar.wrap,
        )
        self.hint.pack(anchor="w", pady=(4, 0))
        self._changed()

    # state ---------------------------------------------------------------------------

    def set_image(self, name: str, width: int, height: int) -> None:
        """A new document (or a new size after an expand): start from an unchanged canvas."""
        key = (name, int(width), int(height))
        if key == self.image_key:
            return
        self.image_key = key
        self.thumbnail = None
        self._photo = None
        self.state = ue.ExpandState(int(width), int(height), listener=self._changed)
        self.zoom = 1.0
        self._changed()

    def set_thumbnail(self, image: Image.Image) -> None:
        self.thumbnail = image
        self._photo = None
        self._draw()

    def margins(self) -> tuple[int, int, int, int]:
        m = self.state.margins
        return m.left, m.top, m.right, m.bottom

    def _ratio(self, button: str) -> None:
        self.state.apply_ratio(button)

    def _turn(self, portrait: bool) -> None:
        self.state.set_portrait(portrait)

    def _changed(self) -> None:
        s = self.state
        current = ue.ratio_button(s.preset)
        for key, b in self.ratio_buttons.items():
            b.configure(style="ChipOn.TButton" if key == current else "Chip.TButton")
        for portrait, b in self.turn_buttons.items():
            b.configure(style="ChipOn.TButton" if portrait == s.portrait else "Chip.TButton")
        w, h = s.canvas
        if s.margins.is_empty():
            self.size.configure(text=f"{s.image_w} x {s.image_h} px")
        else:
            self.size.configure(text=f"{s.image_w} x {s.image_h}  >  {w} x {h} px")
        self._draw()

    # geometry ------------------------------------------------------------------------

    def _fit(self, scale_cap: float) -> ue.View:
        """The image and the frame centred in the editor, at most ``scale_cap``, smaller when
        the frame would not fit (so it never leaves the editor)."""
        s = self.state
        thumb = max(1.0, scale_cap * max(s.image_w, s.image_h))
        return ue.fit_view(AREA[0], AREA[1], s.image_w, s.image_h, s.margins, thumb_px=thumb, pad=10)

    def base_scale(self) -> float:
        s = self.state
        return min(AREA[0] * ROOM / s.image_w, AREA[1] * ROOM / s.image_h)

    def view(self) -> ue.View:
        """What is drawn: the zoomed view, zoomed out further while the frame needs it."""
        if self._drag is not None:
            s, start = self.state, self._drag[4]
            v = ue.view_at(start.scale, start.ix, start.iy, s.image_w, s.image_h, s.margins)
            if v.fx >= 0 and v.fy >= 0 and v.fx + v.fw <= AREA[0] and v.fy + v.fh <= AREA[1]:
                return v  # the image stays under the pointer
            return self._fit(start.scale)  # past the edge: zoom out to follow the frame
        return self._fit(self.base_scale() * self.zoom)

    def _zoom_by(self, factor: float) -> None:
        self.zoom = max(0.2, min(5.0, self.zoom * factor))
        self._draw()

    def show_preview(self, image: Image.Image | None) -> None:
        """A running expand's frames drawn where the editor is (None: back to the editor)."""
        if image is None:
            if self._preview is not None:
                self._preview = None
                self._draw()
            return
        from .bar import photo

        frame = image.convert("RGB")
        frame.thumbnail((AREA[0] - 8, AREA[1] - 8))
        self._preview = photo(frame)
        c = self.canvas
        c.delete("all")
        c.create_image(AREA[0] // 2, AREA[1] // 2, image=self._preview)

    # pointer -------------------------------------------------------------------------

    def _press(self, event) -> None:
        if self._preview is not None:
            return
        handle = ue.hit_region(self.view(), event.x, event.y)
        if handle:
            self._drag = (handle, event.x, event.y, self.state.margins, self.view())

    def _motion_drag(self, event) -> None:
        if self._drag is None:
            return
        handle, x0, y0, start, view = self._drag
        s = self.state
        dx, dy = event.x - x0, event.y - y0
        if handle == "move":
            s.move_from(start, dx / view.scale, dy / view.scale)
        elif "-" in handle and s.ratio is not None:
            s.resize_from(start, handle, dx / view.scale, dy / view.scale)
        else:
            s.set_margins(ue.drag_margins(start, handle, dx, dy, view.scale, s.limit))

    def _release(self, _event) -> None:
        if self._drag is not None:
            # keep the zoom the drag ended at, so the frame stays where it was let go
            self.zoom = max(0.2, min(5.0, self.view().scale / self.base_scale()))
        self._drag = None
        self._draw()

    def _hover(self, event) -> None:
        if self._drag is None and self._preview is None:
            handle = ue.hit_region(self.view(), event.x, event.y)
            self.canvas.configure(cursor=TK_CURSORS.get(handle, ""))

    # drawing -------------------------------------------------------------------------

    def _thumb_photo(self, width: int, height: int) -> tk.PhotoImage | None:
        if self.thumbnail is None or width < 2 or height < 2:
            return None
        if self._photo is None or self._photo_size != (width, height):
            from .bar import photo

            self._photo = photo(self.thumbnail.convert("RGB").resize((width, height), Image.BILINEAR))
            self._photo_size = (width, height)
        return self._photo

    def _draw(self) -> None:
        if self._preview is not None:
            return  # a job's preview is showing
        c = self.canvas
        c.delete("all")
        v, m = self.view(), self.state.margins
        c.create_rectangle(v.fx, v.fy, v.fx + v.fw, v.fy + v.fh, fill="#2c4a6e", outline="")
        image = self._thumb_photo(round(v.iw), round(v.ih))
        if image is not None:
            c.create_image(round(v.ix), round(v.iy), image=image, anchor="nw")
        else:
            c.create_rectangle(v.ix, v.iy, v.ix + v.iw, v.iy + v.ih, fill="#6b6b6b", outline="")
        c.create_rectangle(v.ix, v.iy, v.ix + v.iw, v.iy + v.ih, outline="#c8c8c8")
        for sx, sy, sw, sh in ue.crop_strips(v):  # what a crop cuts, darkened
            c.create_rectangle(sx, sy, sx + sw, sy + sh, fill="black", stipple="gray50", outline="")
        sides = (
            (min(v.fx, v.ix), v.iy, abs(v.ix - v.fx), v.ih, m.left),
            (min(v.ix + v.iw, v.fx + v.fw), v.iy, abs(v.fx + v.fw - v.ix - v.iw), v.ih, m.right),
            (v.ix, min(v.fy, v.iy), v.iw, abs(v.iy - v.fy), m.top),
            (v.ix, min(v.iy + v.ih, v.fy + v.fh), v.iw, abs(v.fy + v.fh - v.iy - v.ih), m.bottom),
        )
        for x, y, w, h, amount in sides:
            if amount and w > 34 and h > 16:
                text = f"+{amount}" if amount > 0 else f"-{-amount}"
                c.create_text(x + w / 2, y + h / 2, text=text, fill="white", font=self.bar.small)
        c.create_rectangle(v.fx, v.fy, v.fx + v.fw, v.fy + v.fh, outline="white", dash=(6, 4), width=1.5)
        half = ue.HANDLE_PX / 2
        for _name, (hx, hy) in ue.handle_points(v).items():
            c.create_rectangle(hx - half, hy - half, hx + half, hy + half, fill="white", outline="#1a1a1a")
