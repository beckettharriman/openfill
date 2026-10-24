# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""OpenFill's own ComfyUI nodes. OpenFill copies this folder to
``ComfyUI/custom_nodes/openfill-nodes`` (Models > Install nodes) and checks its VERSION.

OpenFillLamaNative: LaMa (big-lama, loaded by comfyui-inpaint-nodes' Load Inpaint
Model) at the crop's own resolution. comfyui-inpaint-nodes' Inpaint (using Model)
resizes every input to a 256 px square for LaMa, so large areas came back soft. big-lama
is fully convolutional (Fourier convolutions) and works at high resolution, so this node
pads the crop to a multiple of 8 and runs it 1:1 in one pass when it fits the graphics
memory, else in a few large overlapping windows at 1:1 with feathered blending (see
plan.py), else once at the largest scale that fits.

torch and ComfyUI are imported only when the node runs, so OpenFill's own tests can
import plan.py without them.
"""

from __future__ import annotations

import os
import time

VERSION = "1"

# Graphics memory per working pixel of big-lama in fp32: about 650 bytes measured on an
# RTX 2070 Max-Q (peak allocated minus the weights, 1.8 to 4.8 MP passes), with a margin.
# Half precision is not offered: big-lama in fp16 returns a flat dark patch (spandrel
# also marks LaMa as not supporting it).
BYTES_PER_PX = 700
RESERVE = 512 * 1024**2


def _plan_module():
    try:
        from . import plan
    except ImportError:  # loaded as a plain module by a test
        import plan  # type: ignore[no-redef]
    return plan


class OpenFillLamaNative:
    CATEGORY = "inpaint"
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("inpainted image",)
    FUNCTION = "run"
    DESCRIPTION = "LaMa at the image's own resolution (one pass, or large overlapping windows when memory is short)."

    @classmethod
    def INPUT_TYPES(cls):  # noqa: N802 - ComfyUI's name
        return {
            "required": {
                "inpaint_model": ("INPAINT_MODEL",),
                "image": ("IMAGE",),
                "mask": ("MASK",),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
                "max_megapixels": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 400.0, "step": 0.1}),
                "overlap": ("INT", {"default": 256, "min": 64, "max": 1024, "step": 8}),
            }
        }

    def run(self, inpaint_model, image, mask, seed, max_megapixels=0.0, overlap=256):
        import comfy.model_management as mm
        import torch
        import torch.nn.functional as F  # noqa: N812

        plan = _plan_module()
        model = getattr(inpaint_model, "model", None)
        arch = getattr(getattr(inpaint_model, "architecture", None), "id", "")
        if arch != "LaMa" or model is None:
            raise ValueError("OpenFill LaMa (native) runs LaMa only; use Inpaint (using Model) for MAT")
        device = mm.get_torch_device()
        dtype = torch.float32

        if image.dim() == 3:
            image = image.unsqueeze(0)
        if mask.dim() == 2:
            mask = mask.unsqueeze(0)
        img = image.permute(0, 3, 1, 2)[:, :3].float()  # BHWC -> BCHW
        msk = (mask.unsqueeze(1).float() >= 0.99).float()  # a hard hole, as the node it replaces
        if msk.shape[0] != img.shape[0]:
            msk = msk[:1].repeat(img.shape[0], 1, 1, 1)
        if msk.shape[2:] != img.shape[2:]:
            raise ValueError(f"image and mask sizes differ: {tuple(img.shape[2:])} != {tuple(msk.shape[2:])}")
        _b, _c, height, width = img.shape

        mm.free_memory(mm.module_size(model) + 2 * 1024**3, device)
        inpaint_model.to(device)
        if device.type == "cuda":
            torch.cuda.reset_peak_memory_stats(device)
            free = mm.get_free_memory(device)
            budget = max(0, int(free) - RESERVE) // BYTES_PER_PX
        else:
            budget = 64 * 1024**2  # processor: memory is rarely the limit
        if max_megapixels > 0:
            budget = min(budget, int(max_megapixels * 1e6))
        env = os.environ.get("OPENFILL_LAMA_MAX_MP")
        if env:
            budget = min(budget, int(float(env) * 1e6))

        started = time.monotonic()
        out = []
        try:
            for i in range(img.shape[0]):
                out.append(self._one(img[i : i + 1], msk[i : i + 1], model, device, dtype, budget, overlap, plan, mm))
        finally:
            inpaint_model.cpu()
        peak = torch.cuda.max_memory_allocated(device) if device.type == "cuda" else 0
        print(
            f"[OpenFill] LaMa native {width}x{height} budget {budget / 1e6:.1f} MP "
            f"peak {peak / 1024**2:.0f} MiB in {time.monotonic() - started:.1f} s",
            flush=True,
        )
        result = torch.cat(out, dim=0).clamp(0, 1)
        del F
        return (result.permute(0, 2, 3, 1).contiguous(),)

    # ------------------------------------------------------------------ passes ----

    @staticmethod
    def _bbox(mask2d):
        import torch

        ys = torch.nonzero(mask2d.any(dim=1)).flatten()
        xs = torch.nonzero(mask2d.any(dim=0)).flatten()
        if len(ys) == 0:
            return None
        return int(xs[0]), int(ys[0]), int(xs[-1]) + 1 - int(xs[0]), int(ys[-1]) + 1 - int(ys[0])

    def _one(self, img, msk, model, device, dtype, budget, overlap, plan, mm):
        bbox = self._bbox(msk[0, 0] > 0.5)
        if bbox is None:
            return img
        height, width = img.shape[2:]
        tries = 0
        while True:
            p = plan.plan_native(width, height, plan.Box(*bbox), budget, overlap)
            print(
                f"[OpenFill] LaMa native plan: {width}x{height}, scale {p.scale:.3f}, "
                f"{len(p.windows) or 1} pass(es) of {p.work_w}x{p.work_h}",
                flush=True,
            )
            try:
                if p.windows:
                    return self._windows(img, msk, p, model, device, dtype, plan)
                return self._pass(img, msk, model, device, dtype, p.scale)
            except mm.OOM_EXCEPTION:
                tries += 1
                if tries > 4:
                    raise
                budget = int(budget * 0.6)
                mm.soft_empty_cache(True)
                print(f"[OpenFill] LaMa native: out of memory, retrying with {budget / 1e6:.1f} MP", flush=True)

    @staticmethod
    def _pass(img, msk, model, device, dtype, scale=1.0):
        """One LaMa pass over ``img`` (1:1 unless ``scale`` < 1), padded to a multiple of 8."""
        import torch
        import torch.nn.functional as F  # noqa: N812

        height, width = img.shape[2:]
        x, m = img, msk
        if scale < 0.999:
            sw = max(8, int(width * scale) // 8 * 8)
            sh = max(8, int(height * scale) // 8 * 8)
            x = F.interpolate(img, size=(sh, sw), mode="area")
            m = (F.interpolate(msk, size=(sh, sw), mode="area") > 0.0).float()  # never shrink the hole
        h, w = x.shape[2:]
        ph, pw = (-h) % 8, (-w) % 8
        if ph or pw:
            mode = "reflect" if ph < h and pw < w else "replicate"
            x = F.pad(x, (0, pw, 0, ph), mode=mode)
            m = F.pad(m, (0, pw, 0, ph), mode="constant", value=0.0)
        with torch.no_grad():
            y = model(x.to(device, dtype), m.to(device, dtype)).float()
        y = y[:, :, :h, :w].cpu()
        if scale < 0.999:
            y = F.interpolate(y, size=(height, width), mode="bicubic", align_corners=False).clamp(0, 1)
        return img + (y - img) * msk  # known pixels stay exactly as they were

    def _windows(self, img, msk, p, model, device, dtype, plan):
        """Overlapping windows at 1:1. The parts earlier windows filled count as known
        for later ones, except a band along their unfinished edge that the later
        window repaints and blends over a ramp of ``feather`` px."""
        import torch
        import torch.nn.functional as F  # noqa: N812

        dev = device if device.type == "cuda" else torch.device("cpu")
        cur = img.clone()
        hole = msk.clone()  # still to fill
        windows = list(p.windows)
        feather = max(16, min(128, min(min(b.w, b.h) for b in windows) // 8))
        bar = _progress(len(windows))

        def known(i):
            b = windows[i]
            return 1.0 - float(hole[:, :, b.y : b.bottom, b.x : b.right].mean())

        left = list(range(len(windows)))
        while left:
            i = max(left, key=known)
            left.remove(i)
            b = windows[i]
            sl = (slice(None), slice(None), slice(b.y, b.bottom), slice(b.x, b.right))
            win_hole = hole[sl].to(dev)
            if float(win_hole.sum()) == 0:
                bar.update(1)
                continue
            filled = msk[sl].to(dev) * (1 - win_hole)  # done by earlier windows
            # repaint a band of the finished part next to what is still open, blend over it
            repaint = torch.clamp(win_hole + filled * _dilate(win_hole, feather), 0, 1)
            res = self._pass(cur[sl], repaint.cpu(), model, device, dtype).to(dev)
            ramp = _box(win_hole, feather)
            alpha = torch.where(win_hole > 0, torch.ones_like(ramp), torch.clamp(2 * ramp, 0, 1) * filled)
            part = cur[sl].to(dev)
            cur[sl] = (part + (res - part) * alpha).cpu()
            hole[sl] = 0
            bar.update(1)
        del plan, F
        return cur


class _NoProgress:
    """Stands in for ComfyUI's progress bar outside ComfyUI."""

    def __init__(self, _total: int) -> None:
        pass

    def update(self, _n: int) -> None:
        pass


def _progress(total: int):
    """ComfyUI's progress bar, or a stand-in outside ComfyUI (tests)."""
    try:
        from comfy.utils import ProgressBar
    except ImportError:
        return _NoProgress(total)
    return ProgressBar(total)


def _dilate(mask, radius: int):
    """Square dilation by ``radius`` px (separable max pooling)."""
    import torch.nn.functional as F  # noqa: N812

    k = 2 * radius + 1
    out = F.max_pool2d(mask, (k, 1), stride=1, padding=(radius, 0))
    return F.max_pool2d(out, (1, k), stride=1, padding=(0, radius))


def _box(mask, radius: int):
    """Mean over a (2 radius + 1) square, from an integral image (edges count only
    the pixels inside)."""
    import torch
    import torch.nn.functional as F  # noqa: N812

    def along(x, dim):
        n = x.shape[dim]
        c = torch.cumsum(F.pad(x, (1, 0) if dim == 3 else (0, 0, 1, 0)), dim=dim)
        idx = torch.arange(n, device=x.device)
        hi = torch.clamp(idx + radius + 1, max=n)
        lo = torch.clamp(idx - radius, min=0)
        return c.index_select(dim, hi) - c.index_select(dim, lo), (hi - lo).to(x.dtype)

    s1, n1 = along(mask.double(), 3)
    s2, n2 = along(s1, 2)
    return (s2 / (n2.view(1, 1, -1, 1) * n1.view(1, 1, 1, -1))).to(mask.dtype)


NODE_CLASS_MAPPINGS = {"OpenFillLamaNative": OpenFillLamaNative}
NODE_DISPLAY_NAME_MAPPINGS = {"OpenFillLamaNative": "OpenFill LaMa (native resolution)"}
