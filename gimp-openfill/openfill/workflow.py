# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""ComfyUI workflow builders (API "prompt" format).

The graphs are assembled in code so the user never sees ComfyUI. Node ids
are short stable strings; ``/prompt`` errors reference them and are mapped
back to readable messages by the backend.

Graph shapes are documented in ``docs/design.md``. The builders only need
core ComfyUI nodes; when the optional ``comfyui-inpaint-nodes`` package is
present (``Capabilities.inpaint_nodes``) they use its better mask and
pre-fill nodes and can apply the Fooocus SDXL inpaint patch.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

from .backend import InpaintRequest, ModelSpec, Txt2ImgRequest

OUTPUT_NODE = "save"


@dataclass(frozen=True)
class Capabilities:
    inpaint_nodes: bool = False
    native_lama: bool = False  # OpenFill's own node pack (OpenFillLamaNative) is loaded
    bg_removal: bool = False  # ComfyUI's own BiRefNet nodes (RemoveBackground) are on the server
    seedvr2: bool = False  # ComfyUI's own SeedVR2 nodes (ComfyUI 0.39 and newer)


class Graph:
    """Tiny helper that numbers nodes and produces the API-format dict."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}

    def add(self, node_id: str, class_type: str, **inputs) -> str:
        if node_id in self.nodes:
            raise ValueError(f"duplicate node id {node_id}")
        self.nodes[node_id] = {"class_type": class_type, "inputs": inputs}
        return node_id

    @staticmethod
    def out(node_id: str, index: int = 0) -> list:
        return [node_id, index]

    def to_dict(self) -> dict:
        return dict(self.nodes)


def _checkpoint(g: Graph, name: str) -> tuple[list, list, list]:
    g.add("ckpt", "CheckpointLoaderSimple", ckpt_name=name)
    return g.out("ckpt", 0), g.out("ckpt", 1), g.out("ckpt", 2)


def _sampler_cfg(req: InpaintRequest) -> float:
    """KSampler guidance: FLUX takes its guidance through FluxGuidance and samples at 1.0."""
    return 1.0 if req.model.arch == "flux" else float(req.sampling.cfg)


def _loaders(g: Graph, spec: ModelSpec) -> tuple[list, list, list]:
    """Model, CLIP and VAE of a fill model: one checkpoint file, or (erasers) separate
    files. RORem is an SDXL inpainting UNet in diffusers layout with SDXL's two text
    encoders and VAE; the FLUX eraser is FLUX.1 Fill with the object-removal LoRA."""
    template = spec.effective_template
    if template == "sdxl_eraser":
        g.add("unet", "UNETLoader", unet_name=spec.unet, weight_dtype="default")
        g.add("clip", "DualCLIPLoader", clip_name1=spec.clip1, clip_name2=spec.clip2, type="sdxl")
        g.add("vae", "VAELoader", vae_name=spec.vae)
        return g.out("unet"), g.out("clip"), g.out("vae")
    if template == "flux_eraser":
        model, clip, vae = _flux_loaders(g, spec)
        if spec.lora:
            g.add("lora", "LoraLoaderModelOnly", model=model, lora_name=spec.lora, strength_model=1.0)
            model = g.out("lora")
        return model, clip, vae
    return _checkpoint(g, spec.checkpoint)


def _prompts(g: Graph, clip: list, prompt: str, negative: str) -> tuple[list, list]:
    g.add("pos", "CLIPTextEncode", text=prompt, clip=clip)
    g.add("neg", "CLIPTextEncode", text=negative, clip=clip)
    return g.out("pos"), g.out("neg")


def _scale_image(g: Graph, node_id: str, image: list, width: int, height: int) -> list:
    g.add(node_id, "ImageScale", image=image, upscale_method="lanczos", width=width, height=height, crop="disabled")
    return g.out(node_id)


def _hard_model_mask(req: InpaintRequest) -> bool:
    """The model mask stays hard (grown, not blurred). The LaMa/MAT node wants
    a hard mask. An SD 1.5 inpainting checkpoint sees the hole as grey in its
    conditioning image wherever the mask is 0.5 or more, while a blurred noise
    mask lets the sampler touch that band only in the last steps; there it
    reproduced the grey of the conditioning image as a pale frame around the
    fill. With a hard mask the whole hole is generated from the first step and
    the GIMP layer mask's feather makes the soft transition. Fills only: the
    expand path keeps its blur."""
    template = req.model.effective_template
    return template == "inpaint_model" or (template == "sd15_inpaint" and req.mode == "fill")


def _prepare_mask(g: Graph, mask_image: list, req: InpaintRequest, caps: Capabilities) -> list:
    """Mask image (gray PNG loaded as IMAGE) -> grown, feathered MASK."""
    if req.needs_scaling:
        mask_image = _scale_image(g, "mask_scale", mask_image, req.work_width, req.work_height)
    g.add("mask", "ImageToMask", image=mask_image, channel="red")
    mask = g.out("mask")
    grow = max(0, int(req.grow))
    # The blur must not exceed the growth, otherwise a small selection never
    # reaches mask value 1.0 and the model only partially repaints it. The
    # LaMa/MAT node wants a hard mask: no blur at all.
    hard = _hard_model_mask(req)
    blur = 0 if hard else max(0, min(int(req.feather), grow))
    if caps.inpaint_nodes:
        if grow or blur:
            g.add("mask_grow", "INPAINT_ExpandMask", mask=mask, grow=grow, blur=blur, blur_type="gaussian")
            mask = g.out("mask_grow")
        g.add("mask_stable", "INPAINT_StabilizeMask", mask=mask, epsilon=0.01)
        return g.out("mask_stable")
    if grow:
        g.add("mask_grow", "GrowMask", mask=mask, expand=grow, tapered_corners=True)
        mask = g.out("mask_grow")
    if blur:
        g.add("mask_img", "MaskToImage", mask=mask)
        radius = max(1, min(31, blur // 2))
        g.add("mask_blur", "ImageBlur", image=g.out("mask_img"), blur_radius=radius, sigma=max(1.0, blur / 3.0))
        g.add("mask_soft", "ImageToMask", image=g.out("mask_blur"), channel="red")
        mask = g.out("mask_soft")
    return mask


def _prefill(g: Graph, image: list, mask: list, req: InpaintRequest, caps: Capabilities) -> list:
    """Hide the old content under the mask so the sampler starts from a neutral
    image instead of the object being removed (fill) or hard transparent
    pixels (expand)."""
    if not (req.prefill and caps.inpaint_nodes):
        return image
    if req.mode == "expand" or req.prefill_style == "border":
        # Fill the hole from its border so nothing of the removed object (its
        # colour, its blurred shape) survives into the sampler's starting image.
        mode = req.prefill_mode if req.prefill_mode in ("navier-stokes", "telea", "neutral") else "navier-stokes"
        g.add("prefill", "INPAINT_MaskedFill", image=image, mask=mask, fill=mode, falloff=0)
        g.add("preblur", "INPAINT_MaskedBlur", image=g.out("prefill"), mask=mask, blur=65, falloff=0)
        return g.out("preblur")
    g.add("preblur", "INPAINT_MaskedBlur", image=image, mask=mask, blur=65, falloff=9)
    return g.out("preblur")


# Templates whose graph supports the tiled main pass and the refine pass.
REFINE_TEMPLATES = ("sd15_inpaint", "sdxl_fooocus")
# Templates whose graph supports the tiled main pass (erasers work at full image
# resolution in tiles of their own working size).
TILED_TEMPLATES = (*REFINE_TEMPLATES, "sdxl_eraser", "flux_eraser")


@dataclass
class PlannedInpaintRequest(InpaintRequest):
    """An inpaint request with an optional tiled main pass and an optional
    full-resolution refine pass (``geometry.FillPlan``).

    ``full_grow`` and ``full_blur`` are the mask growth and blur at context
    resolution (image px), used by the tiled and refine passes, which work on
    the context at its own size.
    """

    tiles: tuple = ()  # geometry.Tile, main pass (empty: one pass at work size)
    refine_tiles: tuple = ()  # geometry.Tile at scale 1 (empty: no refine)
    refine_denoise: float = 0.4
    # Below 1.0 (``FillPlan.layout``): the whole context is generated first in
    # one pass at the work size, then the tiles add detail at this denoise.
    tile_denoise: float = 1.0
    full_grow: int = 0
    full_blur: int = 0
    # Generation model for the refine pass of a prompt-free removal (LaMa/MAT
    # work at 256/512 px; the refine pass restores texture at full resolution).
    refine_model: ModelSpec | None = None
    refine_prompt: str = ""
    refine_negative: str = ""

    @property
    def work_pixels(self) -> int:
        """Pixels sampled over all passes (for the timeout estimate)."""
        main = sum(t.work_width * t.work_height for t in self.tiles) or self.work_width * self.work_height
        return main + sum(t.work_width * t.work_height for t in self.refine_tiles)


def _tiles(req: InpaintRequest) -> tuple:
    return tuple(getattr(req, "tiles", ()) or ())


def _refine_tiles(req: InpaintRequest) -> tuple:
    return tuple(getattr(req, "refine_tiles", ()) or ())


def _single_pass(g: Graph, req: InpaintRequest, caps: Capabilities, model, positive, negative, vae, seed: int) -> list:
    """The classic pass: the whole context at the working size; returns the
    decoded (colour matched) result at context size."""
    image = g.out("load_image", 0)
    if req.needs_scaling:
        image = _scale_image(g, "image_scale", image, req.work_width, req.work_height)
    mask = _prepare_mask(g, g.out("load_mask", 0), req, caps)
    image = _prefill(g, image, mask, req, caps)

    g.add("diff", "DifferentialDiffusion", model=model)
    model = g.out("diff")

    use_fooocus = req.model.uses_fooocus and caps.inpaint_nodes
    if use_fooocus:
        g.add(
            "encode",
            "INPAINT_VAEEncodeInpaintConditioning",
            positive=positive,
            negative=negative,
            vae=vae,
            pixels=image,
            mask=mask,
        )
        g.add("fooocus_patch", "INPAINT_LoadFooocusInpaint", head=req.model.fooocus_head, patch=req.model.fooocus_patch)
        g.add(
            "fooocus",
            "INPAINT_ApplyFooocusInpaint",
            model=model,
            patch=g.out("fooocus_patch"),
            latent=g.out("encode", 2),
        )
        model = g.out("fooocus")
        latent = g.out("encode", 3)
    else:
        g.add(
            "encode",
            "InpaintModelConditioning",
            positive=positive,
            negative=negative,
            vae=vae,
            pixels=image,
            mask=mask,
            noise_mask=True,
        )
        latent = g.out("encode", 2)
    positive, negative = g.out("encode", 0), g.out("encode", 1)

    batch = max(1, int(req.sampling.batch))
    if batch > 1:
        g.add("batch", "RepeatLatentBatch", samples=latent, amount=batch)
        latent = g.out("batch")

    g.add(
        "sampler",
        "KSampler",
        model=model,
        positive=positive,
        negative=negative,
        latent_image=latent,
        seed=int(seed),
        steps=int(req.sampling.steps),
        cfg=_sampler_cfg(req),
        sampler_name=req.sampling.sampler,
        scheduler=req.sampling.scheduler,
        denoise=float(req.sampling.denoise),
    )
    g.add("decode", "VAEDecode", samples=g.out("sampler"), vae=vae)
    out = g.out("decode")
    if caps.inpaint_nodes and req.color_match > 0:
        # Correct the global brightness/color drift of the decoded image using
        # the untouched pixels outside the mask as the reference.
        reference = g.out("image_scale") if req.needs_scaling else g.out("load_image", 0)
        g.add(
            "color_match",
            "INPAINT_ColorMatch",
            target=out,
            reference=reference,
            exclude_mask=mask,
            strength=float(req.color_match),
        )
        out = g.out("color_match")
    if req.needs_scaling:
        out = _scale_image(g, "out_scale", out, req.width, req.height)
    return out


def _full_mask(g: Graph, req: InpaintRequest) -> list:
    """The selection mask at context resolution, grown and blurred like the
    model mask of the single pass (needs comfyui-inpaint-nodes)."""
    g.add("fmask_raw", "ImageToMask", image=g.out("load_mask", 0), channel="red")
    grow = max(0, int(getattr(req, "full_grow", 0)))
    blur = 0 if _hard_model_mask(req) else max(0, min(int(getattr(req, "full_blur", 0)), grow))
    g.add("fmask_grow", "INPAINT_ExpandMask", mask=g.out("fmask_raw"), grow=grow, blur=blur, blur_type="gaussian")
    g.add("fmask", "INPAINT_StabilizeMask", mask=g.out("fmask_grow"), epsilon=0.01)
    return g.out("fmask")


def _tile_mask(g: Graph, p: str, full_mask: list, tile, overlap_blur: int) -> list:
    """The full mask cropped to ``tile``, faded out over the parts earlier
    tiles finished (``tile.done``), so the tile repaints only its own area and
    a blend band."""
    r = tile.rect
    g.add(f"{p}_mcrop", "CropMask", mask=full_mask, x=r.x, y=r.y, width=r.width, height=r.height)
    mask = g.out(f"{p}_mcrop")
    if not tile.done:
        return mask
    g.add(f"{p}_keep", "SolidMask", value=1.0, width=r.width, height=r.height)
    keep = g.out(f"{p}_keep")
    for i, d in enumerate(tile.done):
        g.add(f"{p}_done{i}", "SolidMask", value=1.0, width=d.width, height=d.height)
        g.add(
            f"{p}_keep{i}",
            "MaskComposite",
            destination=keep,
            source=g.out(f"{p}_done{i}"),
            x=d.x,
            y=d.y,
            operation="subtract",
        )
        keep = g.out(f"{p}_keep{i}")
    g.add(f"{p}_ramp", "INPAINT_ExpandMask", mask=keep, grow=0, blur=max(3, overlap_blur), blur_type="gaussian")
    g.add(f"{p}_mask", "MaskComposite", destination=mask, source=g.out(f"{p}_ramp"), x=0, y=0, operation="multiply")
    return g.out(f"{p}_mask")


def _scale_mask(g: Graph, p: str, mask: list, width: int, height: int) -> list:
    g.add(f"{p}_mimg", "MaskToImage", mask=mask)
    g.add(
        f"{p}_mscale",
        "ImageScale",
        image=g.out(f"{p}_mimg"),
        upscale_method="bilinear",
        width=width,
        height=height,
        crop="disabled",
    )
    g.add(f"{p}_mback", "ImageToMask", image=g.out(f"{p}_mscale"), channel="red")
    g.add(f"{p}_mstable", "INPAINT_StabilizeMask", mask=g.out(f"{p}_mback"), epsilon=0.01)
    return g.out(f"{p}_mstable")


def _batch_mask(g: Graph, p: str, mask: list, batch: int) -> list:
    """``mask`` repeated to ``batch`` masks (core ComfyUI has no mask repeat
    node, so through an image). Batch 1: the mask itself."""
    if batch <= 1:
        return mask
    g.add(f"{p}_bmimg", "MaskToImage", mask=mask)
    g.add(f"{p}_bmrep", "RepeatImageBatch", image=g.out(f"{p}_bmimg"), amount=int(batch))
    g.add(f"{p}_bmask", "ImageToMask", image=g.out(f"{p}_bmrep"), channel="red")
    return g.out(f"{p}_bmask")


def _tile_pass(
    g: Graph,
    p: str,
    req: InpaintRequest,
    caps: Capabilities,
    dest: list,
    full_mask: list,
    tile,
    model,
    positive,
    negative,
    vae,
    seed: int,
    denoise: float,
    refine: bool,
    batch: int,
) -> list:
    """One tile of a tiled pass: crop ``dest``, (scale,) pre-fill (generation
    only), sample, decode, (colour match, scale back) and composite back into
    ``dest`` through the tile mask. Returns the new ``dest``."""
    r = tile.rect
    g.add(f"{p}_crop", "ImageCrop", image=dest, width=r.width, height=r.height, x=r.x, y=r.y)
    crop = g.out(f"{p}_crop")
    tmask = _tile_mask(g, p, full_mask, tile, 65)
    scaled = (tile.work_width, tile.work_height) != (r.width, r.height)
    image, mask = crop, tmask
    if scaled:
        image = _scale_image(g, f"{p}_scale", crop, tile.work_width, tile.work_height)
        mask = _scale_mask(g, p, tmask, tile.work_width, tile.work_height)
    reference = image
    if not refine and req.prefill and caps.inpaint_nodes:
        style = "border" if req.prefill_style == "border" else "blur"
        if style == "border":
            mode = req.prefill_mode if req.prefill_mode in ("navier-stokes", "telea", "neutral") else "navier-stokes"
            # With variations the crop holds one canvas per variation (earlier
            # tiles differ between them), and INPAINT_MaskedFill wants one mask
            # per image: repeat the tile mask to the batch.
            fill_mask = _batch_mask(g, p, mask, batch)
            g.add(f"{p}_prefill", "INPAINT_MaskedFill", image=image, mask=fill_mask, fill=mode, falloff=0)
            g.add(f"{p}_preblur", "INPAINT_MaskedBlur", image=g.out(f"{p}_prefill"), mask=fill_mask, blur=65, falloff=0)
        else:
            g.add(f"{p}_preblur", "INPAINT_MaskedBlur", image=image, mask=mask, blur=65, falloff=9)
        image = g.out(f"{p}_preblur")
    use_fooocus = not refine and req.model.uses_fooocus and caps.inpaint_nodes
    inpaint_ckpt = req.model.arch == "sd15" or req.model.effective_template in (
        "sd15_inpaint",
        "sdxl_eraser",
        "flux_eraser",
    )
    if use_fooocus:
        first = image
        if batch > 1:
            g.add(f"{p}_first", "ImageFromBatch", image=image, batch_index=0, length=1)
            first = g.out(f"{p}_first")
        g.add(
            f"{p}_penc",
            "INPAINT_VAEEncodeInpaintConditioning",
            positive=positive,
            negative=negative,
            vae=vae,
            pixels=first,
            mask=mask,
        )
        g.add(
            f"{p}_patch",
            "INPAINT_ApplyFooocusInpaint",
            model=model,
            patch=g.out("fooocus_patch"),
            latent=g.out(f"{p}_penc", 2),
        )
        tile_model = g.out(f"{p}_patch")
        pos, neg = g.out(f"{p}_penc", 0), g.out(f"{p}_penc", 1)
        if batch > 1:
            g.add(f"{p}_venc", "VAEEncode", pixels=image, vae=vae)
            g.add(f"{p}_lat", "SetLatentNoiseMask", samples=g.out(f"{p}_venc"), mask=mask)
            latent = g.out(f"{p}_lat")
        else:
            latent = g.out(f"{p}_penc", 3)
    elif inpaint_ckpt and refine:
        # Refine on an inpainting checkpoint: its conditioning image shows the
        # whole tile (an empty conditioning mask), so the model reads the
        # generated pixels instead of a grey hole; the noise mask alone limits
        # the change to the regenerated area. With the hole in the
        # conditioning the refine came back greyer and softer.
        g.add(f"{p}_nomask", "SolidMask", value=0.0, width=tile.work_width, height=tile.work_height)
        g.add(
            f"{p}_enc",
            "InpaintModelConditioning",
            positive=positive,
            negative=negative,
            vae=vae,
            pixels=image,
            mask=g.out(f"{p}_nomask"),
            noise_mask=False,
        )
        g.add(f"{p}_lat", "SetLatentNoiseMask", samples=g.out(f"{p}_enc", 2), mask=mask)
        tile_model, pos, neg, latent = model, g.out(f"{p}_enc", 0), g.out(f"{p}_enc", 1), g.out(f"{p}_lat")
    elif inpaint_ckpt:
        g.add(
            f"{p}_enc",
            "InpaintModelConditioning",
            positive=positive,
            negative=negative,
            vae=vae,
            pixels=image,
            mask=mask,
            noise_mask=True,
        )
        tile_model, pos, neg, latent = model, g.out(f"{p}_enc", 0), g.out(f"{p}_enc", 1), g.out(f"{p}_enc", 2)
    else:
        g.add(f"{p}_venc", "VAEEncode", pixels=image, vae=vae)
        g.add(f"{p}_lat", "SetLatentNoiseMask", samples=g.out(f"{p}_venc"), mask=mask)
        tile_model, pos, neg, latent = model, positive, negative, g.out(f"{p}_lat")
    g.add(
        f"{p}_sampler",
        "KSampler",
        model=tile_model,
        positive=pos,
        negative=neg,
        latent_image=latent,
        seed=int(seed),
        steps=int(req.sampling.steps),
        cfg=_sampler_cfg(req),
        sampler_name=req.sampling.sampler,
        scheduler=req.sampling.scheduler,
        denoise=float(denoise),
    )
    g.add(f"{p}_decode", "VAEDecode", samples=g.out(f"{p}_sampler"), vae=vae)
    out = g.out(f"{p}_decode")
    if not refine and caps.inpaint_nodes and req.color_match > 0:
        g.add(
            f"{p}_cm",
            "INPAINT_ColorMatch",
            target=out,
            reference=reference,
            exclude_mask=mask,
            strength=float(req.color_match),
        )
        out = g.out(f"{p}_cm")
    if scaled:
        out = _scale_image(g, f"{p}_back", out, r.width, r.height)
    g.add(
        f"{p}_paste",
        "ImageCompositeMasked",
        destination=dest,
        source=out,
        x=r.x,
        y=r.y,
        resize_source=False,
        mask=tmask,
    )
    return g.out(f"{p}_paste")


def build_inpaint(
    req: InpaintRequest,
    image_ref: str,
    mask_ref: str,
    seed: int,
    caps: Capabilities,
    filename_prefix: str = "openfill/fill",
) -> dict:
    """Build the inpaint / outpaint graph.

    ``image_ref`` and ``mask_ref`` are the names returned by ``/upload/image``
    (``subfolder/filename``). Returns the API-format prompt dict.

    A ``PlannedInpaintRequest`` with ``tiles`` runs the main pass as a chain of
    overlapping tiles (each sees the result of the earlier ones); with
    ``refine_tiles`` the result is composited over the original at full
    resolution and repainted there at ``refine_denoise``, tile by tile. Both
    need comfyui-inpaint-nodes; without them the single pass is used.
    """
    g = Graph()
    model, clip, vae = _loaders(g, req.model)
    positive, negative = _prompts(g, clip, req.prompt, req.negative)
    if req.model.effective_template == "flux_eraser":
        g.add("guidance", "FluxGuidance", conditioning=positive, guidance=float(req.sampling.cfg or 30.0))
        positive = g.out("guidance")
    g.add("load_image", "LoadImage", image=image_ref)
    g.add("load_mask", "LoadImage", image=mask_ref)
    tiles, refine_tiles = _tiles(req), _refine_tiles(req)
    if not caps.inpaint_nodes:
        tiles, refine_tiles = (), ()
    batch = max(1, int(req.sampling.batch))
    tile_denoise = float(getattr(req, "tile_denoise", 1.0))
    layout = bool(tiles) and tile_denoise < 1.0
    if not tiles or layout:
        out = _single_pass(g, req, caps, model, positive, negative, vae, seed)
        if not tiles and not refine_tiles:
            g.add(OUTPUT_NODE, "SaveImage", images=out, filename_prefix=filename_prefix)
            return g.to_dict()
    full_mask = _full_mask(g, req)
    dest = g.out("load_image", 0)
    if batch > 1:
        g.add("dest_batch", "RepeatImageBatch", image=dest, amount=batch)
        dest = g.out("dest_batch")
    g.add("tdiff", "DifferentialDiffusion", model=model)
    diff_model = g.out("tdiff")
    if tiles and not layout:
        if req.model.uses_fooocus:
            g.add(
                "fooocus_patch",
                "INPAINT_LoadFooocusInpaint",
                head=req.model.fooocus_head,
                patch=req.model.fooocus_patch,
            )
        for i, tile in enumerate(tiles):
            dest = _tile_pass(
                g,
                f"t{i}",
                req,
                caps,
                dest,
                full_mask,
                tile,
                diff_model,
                positive,
                negative,
                vae,
                seed,
                float(req.sampling.denoise),
                False,
                batch,
            )
    else:
        g.add(
            "main_paste",
            "ImageCompositeMasked",
            destination=dest,
            source=out,
            x=0,
            y=0,
            resize_source=False,
            mask=full_mask,
        )
        dest = g.out("main_paste")
    if layout:
        # Detail tiles over the layout pass: no pre-fill and the whole tile in
        # the conditioning (the refine path), so they keep its layout.
        for i, tile in enumerate(tiles):
            dest = _tile_pass(
                g,
                f"t{i}",
                req,
                caps,
                dest,
                full_mask,
                tile,
                diff_model,
                positive,
                negative,
                vae,
                seed,
                tile_denoise,
                True,
                batch,
            )
    for i, tile in enumerate(refine_tiles):
        dest = _tile_pass(
            g,
            f"r{i}",
            req,
            caps,
            dest,
            full_mask,
            tile,
            diff_model,
            positive,
            negative,
            vae,
            seed,
            float(getattr(req, "refine_denoise", 0.4)),
            True,
            batch,
        )
    g.add(OUTPUT_NODE, "SaveImage", images=dest, filename_prefix=filename_prefix)
    return g.to_dict()


# Generative Expand prompts. With an empty prompt the positive stays empty and
# the negative steers away from what the model otherwise invents around a
# finished-looking picture: a frame, a border, a vignette or new subjects.
# The old default (a "seamless continuation of the surrounding background"
# positive and no frame terms) made the SDXL tier paint a matted picture frame
# around the old image (docs/evidence/daytime-2026-10-09/expand/README.md).
EXPAND_NEGATIVE = "new objects, people, text, frame, border, vignette, watermark, blurry"
# Appended to a user's own negative: only the frame terms, so a prompt that asks
# for people or text is not contradicted.
EXPAND_FRAME_NEGATIVE = "frame, border, vignette, picture in picture, watermark"


def expand_prompts(prompt: str, negative: str) -> tuple[str, str]:
    """Positive and negative prompt of a Generative Expand.

    Empty prompt ("continue the scene"): empty positive, ``EXPAND_NEGATIVE``
    plus the user's negative. A user prompt is kept and ``EXPAND_FRAME_NEGATIVE``
    is appended to the user's negative.
    """
    prompt, negative = (prompt or "").strip(), (negative or "").strip()
    extra = EXPAND_FRAME_NEGATIVE if prompt else EXPAND_NEGATIVE
    return prompt, f"{negative}, {extra}" if negative else extra


# Guidance of a Generative Expand on the SDXL tier (Fooocus inpaint patch). With
# the empty "continue the scene" positive the guidance only pushes away from the
# negative; at the general default 7 the new border came back over-saturated
# with hard bands (a bright turf strip, grey backdrop bands on a white product
# shot), at 4 (the Fooocus default) it blends. SD 1.5 keeps the setting.
EXPAND_SDXL_CFG = 4.0


def expand_cfg(arch: str, cfg: float) -> float:
    """Sampler guidance for an expand: at most ``EXPAND_SDXL_CFG`` on the SDXL tier."""
    return min(float(cfg), EXPAND_SDXL_CFG) if arch == "sdxl" else float(cfg)


def _is_gguf(name: str) -> bool:
    return name.lower().endswith(".gguf")


def _flux_loaders(g: Graph, spec) -> tuple[list, list, list]:
    """UNet / dual CLIP / VAE loaders for FLUX, GGUF or safetensors as needed."""
    if _is_gguf(spec.unet):
        g.add("unet", "UnetLoaderGGUF", unet_name=spec.unet)
    else:
        g.add("unet", "UNETLoader", unet_name=spec.unet, weight_dtype="fp8_e4m3fn")
    if _is_gguf(spec.clip1) or _is_gguf(spec.clip2):
        g.add("clip", "DualCLIPLoaderGGUF", clip_name1=spec.clip1, clip_name2=spec.clip2, type="flux")
    else:
        g.add("clip", "DualCLIPLoader", clip_name1=spec.clip1, clip_name2=spec.clip2, type="flux")
    g.add("vae", "VAELoader", vae_name=spec.vae)
    return g.out("unet"), g.out("clip"), g.out("vae")


def _load_inputs(g: Graph, req: InpaintRequest, image_ref: str, mask_ref: str, caps: Capabilities) -> tuple[list, list]:
    g.add("load_image", "LoadImage", image=image_ref)
    g.add("load_mask", "LoadImage", image=mask_ref)
    image = g.out("load_image", 0)
    if req.needs_scaling:
        image = _scale_image(g, "image_scale", image, req.work_width, req.work_height)
    mask = _prepare_mask(g, g.out("load_mask", 0), req, caps)
    return image, mask


def _finish(g: Graph, req: InpaintRequest, out: list, mask: list, caps: Capabilities, filename_prefix: str) -> dict:
    if caps.inpaint_nodes and req.color_match > 0:
        reference = g.out("image_scale") if req.needs_scaling else g.out("load_image", 0)
        g.add(
            "color_match",
            "INPAINT_ColorMatch",
            target=out,
            reference=reference,
            exclude_mask=mask,
            strength=float(req.color_match),
        )
        out = g.out("color_match")
    if req.needs_scaling:
        out = _scale_image(g, "out_scale", out, req.width, req.height)
    g.add(OUTPUT_NODE, "SaveImage", images=out, filename_prefix=filename_prefix)
    return g.to_dict()


def build_flux_fill(
    req: InpaintRequest,
    image_ref: str,
    mask_ref: str,
    seed: int,
    caps: Capabilities,
    filename_prefix: str = "openfill/flux",
) -> dict:
    """FLUX.1 Fill: UNet (GGUF or fp8) + T5/CLIP-L + FLUX VAE, InpaintModelConditioning,
    FluxGuidance, cfg 1.0."""
    g = Graph()
    model, clip, vae = _flux_loaders(g, req.model)
    positive, negative = _prompts(g, clip, req.prompt, req.negative)
    g.add("guidance", "FluxGuidance", conditioning=positive, guidance=float(req.sampling.cfg or 30.0))
    positive = g.out("guidance")
    image, mask = _load_inputs(g, req, image_ref, mask_ref, caps)
    image = _prefill(g, image, mask, req, caps)
    g.add("diff", "DifferentialDiffusion", model=model)
    g.add(
        "encode",
        "InpaintModelConditioning",
        positive=positive,
        negative=negative,
        vae=vae,
        pixels=image,
        mask=mask,
        noise_mask=True,
    )
    latent = g.out("encode", 2)
    batch = max(1, int(req.sampling.batch))
    if batch > 1:
        g.add("batch", "RepeatLatentBatch", samples=latent, amount=batch)
        latent = g.out("batch")
    g.add(
        "sampler",
        "KSampler",
        model=g.out("diff"),
        positive=g.out("encode", 0),
        negative=g.out("encode", 1),
        latent_image=latent,
        seed=int(seed),
        steps=int(req.sampling.steps),
        cfg=1.0,
        sampler_name="euler",
        scheduler="normal",
        denoise=float(req.sampling.denoise),
    )
    g.add("decode", "VAEDecode", samples=g.out("sampler"), vae=vae)
    return _finish(g, req, g.out("decode"), mask, caps, filename_prefix)


def build_qwen_edit(
    req: InpaintRequest,
    image_ref: str,
    mask_ref: str,
    seed: int,
    caps: Capabilities,
    filename_prefix: str = "openfill/qwen",
) -> dict:
    """Qwen-Image-Edit (2509/2511 class): instruction-driven edit of the context
    region, limited to the mask with a latent noise mask."""
    g = Graph()
    spec = req.model
    if _is_gguf(spec.unet):
        g.add("unet", "UnetLoaderGGUF", unet_name=spec.unet)
    else:
        g.add("unet", "UNETLoader", unet_name=spec.unet, weight_dtype="default")
    if _is_gguf(spec.clip1):
        g.add("clip", "CLIPLoaderGGUF", clip_name=spec.clip1, type="qwen_image")
    else:
        g.add("clip", "CLIPLoader", clip_name=spec.clip1, type="qwen_image")
    g.add("vae", "VAELoader", vae_name=spec.vae)
    model, clip, vae = g.out("unet"), g.out("clip"), g.out("vae")
    g.add("shift", "ModelSamplingAuraFlow", model=model, shift=3.0)
    g.add("cfgnorm", "CFGNorm", model=g.out("shift"), strength=1.0)
    model = g.out("cfgnorm")
    image, mask = _load_inputs(g, req, image_ref, mask_ref, caps)
    g.add("pos", "TextEncodeQwenImageEditPlus", clip=clip, prompt=req.prompt, vae=vae, image1=image)
    g.add("neg", "TextEncodeQwenImageEditPlus", clip=clip, prompt=req.negative or "", vae=vae, image1=image)
    g.add("encode", "VAEEncode", pixels=image, vae=vae)
    g.add("noise_mask", "SetLatentNoiseMask", samples=g.out("encode"), mask=mask)
    latent = g.out("noise_mask")
    batch = max(1, int(req.sampling.batch))
    if batch > 1:
        g.add("batch", "RepeatLatentBatch", samples=latent, amount=batch)
        latent = g.out("batch")
    g.add(
        "sampler",
        "KSampler",
        model=model,
        positive=g.out("pos"),
        negative=g.out("neg"),
        latent_image=latent,
        seed=int(seed),
        steps=int(req.sampling.steps),
        cfg=float(req.sampling.cfg if 1.0 < req.sampling.cfg <= 5.0 else 2.5),
        sampler_name="euler",
        scheduler="simple",
        denoise=float(req.sampling.denoise),
    )
    g.add("decode", "VAEDecode", samples=g.out("sampler"), vae=vae)
    return _finish(g, req, g.out("decode"), mask, caps, filename_prefix)


def _flux2_loaders(g: Graph, spec: ModelSpec) -> tuple[list, list, list]:
    """UNet / Qwen3 text encoder / FLUX.2 VAE loaders for FLUX.2 [klein], GGUF or safetensors."""
    if _is_gguf(spec.unet):
        g.add("unet", "UnetLoaderGGUF", unet_name=spec.unet)
    else:
        g.add("unet", "UNETLoader", unet_name=spec.unet, weight_dtype="default")
    if _is_gguf(spec.clip1):
        g.add("clip", "CLIPLoaderGGUF", clip_name=spec.clip1, type="flux2")
    else:
        g.add("clip", "CLIPLoader", clip_name=spec.clip1, type="flux2")
    g.add("vae", "VAELoader", vae_name=spec.vae)
    return g.out("unet"), g.out("clip"), g.out("vae")


def build_flux2_edit(
    req: InpaintRequest,
    image_ref: str,
    mask_ref: str,
    seed: int,
    caps: Capabilities,
    filename_prefix: str = "openfill/flux2",
) -> dict:
    """FLUX.2 [klein]: the context goes in as a reference image (ReferenceLatent, as in
    ComfyUI's own Klein edit graph) and new pixels are sampled only under the mask (a
    latent noise mask on the encoded context). Distilled Klein samples in 4 steps at
    guidance 1.0; ComfyUI's Flux2Scheduler sets the shift for the working size."""
    g = Graph()
    model, clip, vae = _flux2_loaders(g, req.model)
    positive, negative = _prompts(g, clip, req.prompt, req.negative)
    image, mask = _load_inputs(g, req, image_ref, mask_ref, caps)
    g.add("encode", "VAEEncode", pixels=image, vae=vae)
    g.add("pos_ref", "ReferenceLatent", conditioning=positive, latent=g.out("encode"))
    g.add("neg_ref", "ReferenceLatent", conditioning=negative, latent=g.out("encode"))
    g.add("noise_mask", "SetLatentNoiseMask", samples=g.out("encode"), mask=mask)
    latent = g.out("noise_mask")
    batch = max(1, int(req.sampling.batch))
    if batch > 1:
        g.add("batch", "RepeatLatentBatch", samples=latent, amount=batch)
        latent = g.out("batch")
    g.add(
        "guider",
        "CFGGuider",
        model=model,
        positive=g.out("pos_ref"),
        negative=g.out("neg_ref"),
        cfg=float(req.sampling.cfg),
    )
    g.add("sampler_select", "KSamplerSelect", sampler_name="euler")
    g.add(
        "scheduler",
        "Flux2Scheduler",
        steps=int(req.sampling.steps),
        width=int(req.work_width),
        height=int(req.work_height),
    )
    sigmas = g.out("scheduler")
    denoise = float(req.sampling.denoise)
    if 0.0 < denoise < 1.0:
        g.add("sigmas_split", "SplitSigmasDenoise", sigmas=sigmas, denoise=denoise)
        sigmas = g.out("sigmas_split", 1)
    g.add("noise", "RandomNoise", noise_seed=int(seed))
    g.add(
        "sampler",
        "SamplerCustomAdvanced",
        noise=g.out("noise"),
        guider=g.out("guider"),
        sampler=g.out("sampler_select"),
        sigmas=sigmas,
        latent_image=latent,
    )
    g.add("decode", "VAEDecode", samples=g.out("sampler"), vae=vae)
    return _finish(g, req, g.out("decode"), mask, caps, filename_prefix)


# Upscale. Real-ESRGAN class models run through ComfyUI's own upscale nodes:
# ImageUpscaleWithModel works in 512 px tiles and halves the tile when the graphics memory
# runs out, so any image fits a small card. The result is then resized to exactly the size
# asked for (a 4x model doing a 2x upscale is resized down with lanczos).
SEEDVR2_TILE = 512  # VAE tile and overlap (px) of the SeedVR2 graph: ComfyUI's own template values
SEEDVR2_TILE_OVERLAP = 128
SEEDVR2_COLOR_FIX = "lab"  # SeedVR2PostProcessing: colour transfer from the resized original


def build_upscale(
    req: InpaintRequest,
    image_ref: str,
    mask_ref: str,
    seed: int,
    caps: Capabilities,
    filename_prefix: str = "openfill/upscale",
) -> dict:
    """Upscale the whole image (``req.width`` x ``req.height``) to ``req.work_width`` x
    ``req.work_height`` with an upscale model. The mask file and the seed are not used."""
    if not req.model.upscale_model:
        raise ValueError("Upscale needs an upscale model (Real-ESRGAN)")
    g = Graph()
    g.add("load_image", "LoadImage", image=image_ref)
    g.add("upscale_model", "UpscaleModelLoader", model_name=req.model.upscale_model)
    g.add("upscale", "ImageUpscaleWithModel", upscale_model=g.out("upscale_model"), image=g.out("load_image", 0))
    out = _scale_image(g, "out_scale", g.out("upscale"), req.work_width, req.work_height)
    g.add(OUTPUT_NODE, "SaveImage", images=out, filename_prefix=filename_prefix)
    return g.to_dict()


def build_seedvr2(
    req: InpaintRequest,
    image_ref: str,
    mask_ref: str,
    seed: int,
    caps: Capabilities,
    filename_prefix: str = "openfill/seedvr2",
) -> dict:
    """Upscale with SeedVR2 (ComfyUI's own nodes, 0.39 and newer), as in ComfyUI's SeedVR2
    image upscale template: the image is resized to the new size, then SeedVR2 restores
    detail in one step, with tiled VAE encode and decode so a large image fits the
    graphics memory."""
    if not caps.seedvr2:
        raise ValueError("SeedVR2 needs ComfyUI 0.39 or newer (its SeedVR2 nodes)")
    spec = req.model
    if not (spec.unet and spec.vae):
        raise ValueError("SeedVR2 needs its model and its VAE")
    g = Graph()
    g.add("load_image", "LoadImage", image=image_ref)
    resized = _scale_image(g, "resize", g.out("load_image", 0), req.work_width, req.work_height)
    g.add("unet", "UNETLoader", unet_name=spec.unet, weight_dtype="default")
    g.add("vae", "VAELoader", vae_name=spec.vae)
    g.add("prepare", "SeedVR2Preprocess", resized_images=resized)
    tiles = {
        "tile_size": SEEDVR2_TILE,
        "overlap": SEEDVR2_TILE_OVERLAP,
        "temporal_size": 4096,
        "temporal_overlap": 8,
    }
    g.add("encode", "VAEEncodeTiled", pixels=g.out("prepare"), vae=g.out("vae"), **tiles)
    g.add("condition", "SeedVR2Conditioning", model=g.out("unet"), vae_conditioning=g.out("encode"))
    g.add(
        "sampler",
        "KSampler",
        model=g.out("unet"),
        positive=g.out("condition", 0),
        negative=g.out("condition", 1),
        latent_image=g.out("encode"),
        seed=int(seed),
        steps=1,
        cfg=1.0,
        sampler_name="euler",
        scheduler="simple",
        denoise=1.0,
    )
    g.add("decode", "VAEDecodeTiled", samples=g.out("sampler"), vae=g.out("vae"), **tiles)
    g.add(
        "finish",
        "SeedVR2PostProcessing",
        images=g.out("decode"),
        original_resized_images=resized,
        color_correction_method=SEEDVR2_COLOR_FIX,
    )
    g.add(OUTPUT_NODE, "SaveImage", images=g.out("finish"), filename_prefix=filename_prefix)
    return g.to_dict()


# The prompts erase-trained models run with (positive, negative). RORem's authors found
# content-free quality words help; the FLUX Fill removal LoRA needs no prompt.
ERASER_PROMPTS = {
    "sdxl_eraser": (
        "4K, high quality, masterpiece, Highly detailed, Sharp focus, Professional, photorealistic, realistic",
        "low quality, worst, bad proportions, blurry, extra finger, Deformed, disfigured, unclear background",
    ),
    "flux_eraser": ("", ""),
}

# OpenFill's own node (comfy_nodes): LaMa at the image's own resolution.
NATIVE_LAMA_NODE = "OpenFillLamaNative"
NATIVE_LAMA_OVERLAP = 256  # px of overlap between windows when one pass does not fit


def build_removal(
    req: InpaintRequest,
    image_ref: str,
    mask_ref: str,
    seed: int,
    caps: Capabilities,
    filename_prefix: str = "openfill/remove",
) -> dict:
    """Prompt-free removal with LaMa / MAT. LaMa runs through OpenFill's own node at the
    crop's own resolution (one pass, or large overlapping windows at 1:1 when the
    graphics memory is short; see comfy_nodes); MAT, or LaMa on a server without that
    node, through comfyui-inpaint-nodes' Inpaint (using Model)."""
    if not caps.inpaint_nodes:
        raise ValueError("prompt-free removal needs comfyui-inpaint-nodes on the server")
    g = Graph()
    if req.model.native_lama and caps.native_lama and not _tiles(req):
        image, mask = _load_inputs(g, req, image_ref, mask_ref, caps)
        g.add("inpaint_model", "INPAINT_LoadInpaintModel", model_name=req.model.inpaint_model)
        g.add(
            "remove",
            NATIVE_LAMA_NODE,
            inpaint_model=g.out("inpaint_model"),
            image=image,
            mask=mask,
            seed=int(seed),
            max_megapixels=0.0,
            overlap=NATIVE_LAMA_OVERLAP,
        )
        out = g.out("remove")
        if req.needs_scaling:
            out = _scale_image(g, "out_scale", out, req.width, req.height)
        g.add(OUTPUT_NODE, "SaveImage", images=out, filename_prefix=filename_prefix)
        return g.to_dict()
    if _tiles(req):
        out = _removal_tiles(g, req, image_ref, mask_ref, seed)
        g.add(OUTPUT_NODE, "SaveImage", images=out, filename_prefix=filename_prefix)
        return g.to_dict()
    image, mask = _load_inputs(g, req, image_ref, mask_ref, caps)
    g.add("inpaint_model", "INPAINT_LoadInpaintModel", model_name=req.model.inpaint_model)
    g.add(
        "remove",
        "INPAINT_InpaintWithModel",
        inpaint_model=g.out("inpaint_model"),
        image=image,
        mask=mask,
        seed=int(seed),
    )
    out = g.out("remove")
    if req.needs_scaling:
        out = _scale_image(g, "out_scale", out, req.width, req.height)
    refine_model = getattr(req, "refine_model", None)
    refine_tiles = _refine_tiles(req)
    if refine_model is not None and refine_tiles:
        out = _removal_refine(g, req, caps, out, refine_model, refine_tiles, seed)
    g.add(OUTPUT_NODE, "SaveImage", images=out, filename_prefix=filename_prefix)
    return g.to_dict()


def build_bgremove(
    req: InpaintRequest,
    image_ref: str,
    mask_ref: str,
    seed: int,
    caps: Capabilities,
    filename_prefix: str = "openfill/bgremove",
) -> dict:
    """Remove Background with BiRefNet (ComfyUI's own nodes, v0.39 and newer): the whole image
    goes in, a PNG with the background made transparent comes out. The model reads the image
    file (flattened over its mean colour); the mask file is the image as it is (RGBA). Its alpha
    is multiplied in, so the cutout is never more opaque than the original, and its colours are
    the cutout's, so see-through pixels keep their own colour. The seed is not used (the model
    is deterministic)."""
    if not caps.bg_removal:
        raise ValueError("Remove Background needs ComfyUI 0.39 or newer (the RemoveBackground node)")
    if not req.model.bg_removal_model:
        raise ValueError("Remove Background needs a background removal model (BiRefNet)")
    g = Graph()
    g.add("load_image", "LoadImage", image=image_ref)
    g.add("bg_model", "LoadBackgroundRemovalModel", bg_removal_name=req.model.bg_removal_model)
    g.add("foreground", "RemoveBackground", bg_removal_model=g.out("bg_model"), image=g.out("load_image", 0))
    g.add("original", "LoadImage", image=mask_ref)
    # LoadImage's mask is the file's transparency (1 - alpha); inverted, it is the image's opacity
    g.add("alpha", "InvertMask", mask=g.out("original", 1))
    g.add(
        "opacity",
        "MaskComposite",
        destination=g.out("foreground"),
        source=g.out("alpha"),
        x=0,
        y=0,
        operation="multiply",
    )
    # SaveImage cuts 255 x opacity down to a whole number, so a subject the model is 99.9 % sure
    # of came back at 254 (a little see-through). Half a level more makes that a rounding.
    g.add("half_level", "SolidMask", value=0.5 / 255, width=min(req.width, 16384), height=min(req.height, 16384))
    g.add(
        "rounded", "MaskComposite", destination=g.out("opacity"), source=g.out("half_level"), x=0, y=0, operation="add"
    )
    # JoinImageWithAlpha reads its mask as transparency, so the opacity is inverted first
    g.add("transparency", "InvertMask", mask=g.out("rounded"))
    g.add("cutout", "JoinImageWithAlpha", image=g.out("original", 0), alpha=g.out("transparency"))
    g.add(OUTPUT_NODE, "SaveImage", images=g.out("cutout"), filename_prefix=filename_prefix)
    return g.to_dict()


# Blend band (px) of a removal tile into the parts earlier tiles finished; it
# lies inside the band the tile repaints (half the overlap).
REMOVAL_BLEND = 24


def _removal_tiles(g: Graph, req: InpaintRequest, image_ref: str, mask_ref: str, seed: int) -> list:
    """Tiled removal at 1:1 (``geometry.plan_removal``): each window of the
    context goes through the removal node on its own, with the parts earlier
    windows finished as known pixels, and is blended back over a short ramp."""
    g.add("load_image", "LoadImage", image=image_ref)
    g.add("load_mask", "LoadImage", image=mask_ref)
    full_mask = _full_mask(g, req)
    g.add("inpaint_model", "INPAINT_LoadInpaintModel", model_name=req.model.inpaint_model)
    dest = g.out("load_image", 0)
    for i, tile in enumerate(_tiles(req)):
        p = f"rm{i}"
        r = tile.rect
        g.add(f"{p}_crop", "ImageCrop", image=dest, width=r.width, height=r.height, x=r.x, y=r.y)
        g.add(f"{p}_mcrop", "CropMask", mask=full_mask, x=r.x, y=r.y, width=r.width, height=r.height)
        hole, blend = g.out(f"{p}_mcrop"), g.out(f"{p}_mcrop")
        if tile.done:
            g.add(f"{p}_done", "SolidMask", value=0.0, width=r.width, height=r.height)
            done = g.out(f"{p}_done")
            for j, d in enumerate(tile.done):
                g.add(f"{p}_d{j}", "SolidMask", value=1.0, width=d.width, height=d.height)
                g.add(
                    f"{p}_dadd{j}",
                    "MaskComposite",
                    destination=done,
                    source=g.out(f"{p}_d{j}"),
                    x=d.x,
                    y=d.y,
                    operation="add",
                )
                done = g.out(f"{p}_dadd{j}")
            g.add(f"{p}_hole", "MaskComposite", destination=hole, source=done, x=0, y=0, operation="subtract")
            hole = g.out(f"{p}_hole")
            # ramp from 0 at the finished part to 1 inside the repainted band
            g.add(
                f"{p}_dgrow",
                "INPAINT_ExpandMask",
                mask=done,
                grow=REMOVAL_BLEND // 2,
                blur=REMOVAL_BLEND + 1,
                blur_type="gaussian",
            )
            g.add(
                f"{p}_blend",
                "MaskComposite",
                destination=hole,
                source=g.out(f"{p}_dgrow"),
                x=0,
                y=0,
                operation="subtract",
            )
            blend = g.out(f"{p}_blend")
        g.add(
            f"{p}_remove",
            "INPAINT_InpaintWithModel",
            inpaint_model=g.out("inpaint_model"),
            image=g.out(f"{p}_crop"),
            mask=hole,
            seed=int(seed),
        )
        g.add(
            f"{p}_paste",
            "ImageCompositeMasked",
            destination=dest,
            source=g.out(f"{p}_remove"),
            x=r.x,
            y=r.y,
            resize_source=False,
            mask=blend,
        )
        dest = g.out(f"{p}_paste")
    return dest


def _removal_refine(
    g: Graph, req: InpaintRequest, caps: Capabilities, removed: list, spec: ModelSpec, tiles, seed: int
) -> list:
    """Repaint the removal model's (smooth, low-resolution) fill at full
    resolution with a generation checkpoint at ``refine_denoise``."""
    from dataclasses import replace as dc_replace

    model, clip, vae = _checkpoint(g, spec.checkpoint)
    positive, negative = _prompts(g, clip, getattr(req, "refine_prompt", ""), getattr(req, "refine_negative", ""))
    full_mask = _full_mask(g, req)
    g.add("tdiff", "DifferentialDiffusion", model=model)
    dest = removed
    rreq = dc_replace(req, model=spec, mode="fill")
    for i, tile in enumerate(tiles):
        dest = _tile_pass(
            g,
            f"r{i}",
            rreq,
            caps,
            dest,
            full_mask,
            tile,
            g.out("tdiff"),
            positive,
            negative,
            vae,
            seed,
            float(getattr(req, "refine_denoise", 0.4)),
            True,
            1,
        )
    return dest


def build_custom(
    req: InpaintRequest,
    image_ref: str,
    mask_ref: str,
    seed: int,
    caps: Capabilities,
    filename_prefix: str = "openfill/custom",
) -> dict:
    """The user's own ComfyUI workflow (``custom_workflow``) with this request's inputs."""
    from . import custom_workflow

    wf = req.model.custom
    if not isinstance(wf, custom_workflow.CustomWorkflow):
        raise ValueError("no custom workflow is loaded")
    return custom_workflow.build(
        wf,
        image_ref,
        mask_ref,
        req.prompt,
        req.negative,
        seed,
        (req.width, req.height),
        (req.work_width, req.work_height),
        filename_prefix,
    )


BUILDERS = {
    "sd15_inpaint": build_inpaint,
    "sdxl_fooocus": build_inpaint,
    "flux_fill": build_flux_fill,
    "flux2_edit": build_flux2_edit,
    "qwen_edit": build_qwen_edit,
    "inpaint_model": build_removal,
    "sdxl_eraser": build_inpaint,
    "flux_eraser": build_inpaint,
    "bgremove": build_bgremove,
    "upscale_model": build_upscale,
    "seedvr2": build_seedvr2,
    "custom_workflow": build_custom,
}


def select_builder(template: str):
    """Return the graph builder for a catalog template id."""
    try:
        return BUILDERS[template]
    except KeyError:
        raise ValueError(f"unknown workflow template '{template}'") from None


def build_fill(
    req: InpaintRequest,
    image_ref: str,
    mask_ref: str,
    seed: int,
    caps: Capabilities,
    filename_prefix: str = "openfill/fill",
) -> dict:
    """Dispatch on the active model's template."""
    return select_builder(req.model.effective_template)(req, image_ref, mask_ref, seed, caps, filename_prefix)


def build_txt2img(req: Txt2ImgRequest, seed: int, filename_prefix: str = "openfill/txt2img") -> dict:
    g = Graph()
    if req.model.effective_template == "flux_fill":
        raise ValueError("FLUX.1 Fill is an inpainting model; use it for Generative Fill or Expand")
    if req.model.effective_template in ("qwen_edit", "inpaint_model", "flux2_edit", "custom_workflow"):
        raise ValueError("the active model does not support text to image")
    if req.model.upscaler or req.model.background_removal:
        raise ValueError("the active model does not support text to image")
    model, clip, vae = _checkpoint(g, req.model.checkpoint)
    positive, negative = _prompts(g, clip, req.prompt, req.negative)
    g.add(
        "latent",
        "EmptyLatentImage",
        width=int(req.width),
        height=int(req.height),
        batch_size=max(1, int(req.sampling.batch)),
    )
    g.add(
        "sampler",
        "KSampler",
        model=model,
        positive=positive,
        negative=negative,
        latent_image=g.out("latent"),
        seed=int(seed),
        steps=int(req.sampling.steps),
        cfg=float(req.sampling.cfg),
        sampler_name=req.sampling.sampler,
        scheduler=req.sampling.scheduler,
        denoise=1.0,
    )
    g.add("decode", "VAEDecode", samples=g.out("sampler"), vae=vae)
    g.add(OUTPUT_NODE, "SaveImage", images=g.out("decode"), filename_prefix=filename_prefix)
    return g.to_dict()


STEP_NODES = ("KSampler", "KSamplerAdvanced", "Flux2Scheduler", "BasicScheduler")


def total_steps(graph: dict) -> int:
    """Sum of sampler steps in a graph (for progress estimation)."""
    total = 0
    for node in graph.values():
        if node.get("class_type") in STEP_NODES:
            try:
                total += int((node.get("inputs") or {}).get("steps", 0))
            except (TypeError, ValueError):
                continue  # a step count from a link (a custom workflow): not known here
    return total


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# ComfyUI's LoadImage decodes uploads with PyAV (FFmpeg), which guesses the file
# format from the first 2048 bytes. A mask with identical rows (an outpaint
# strip) deflates into a periodic byte stream that the MPEG-TS probe scores
# higher than PNG, and the load fails with "End of file". A text chunk of
# spaces right after IHDR fills the probe window, so only the PNG signature
# is recognisable there.
PROBE_PADDING = 2048


def probe_safe_png(data: bytes) -> bytes:
    """Return ``data`` with a tEXt chunk of ``PROBE_PADDING`` spaces inserted after IHDR.

    Decoders ignore the chunk; the pixels are unchanged. Input that is not a PNG
    starting with IHDR, or already padded, is returned as is.
    """
    if not data.startswith(_PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return data
    end_ihdr = 8 + 12 + int.from_bytes(data[8:12], "big")
    if data[end_ihdr + 4 : end_ihdr + 8] == b"tEXt":
        return data
    body = b"Comment\x00" + b" " * PROBE_PADDING
    chunk = len(body).to_bytes(4, "big") + b"tEXt" + body + (zlib.crc32(b"tEXt" + body) & 0xFFFFFFFF).to_bytes(4, "big")
    return data[:end_ihdr] + chunk + data[end_ihdr:]


def model_display_name(name: str) -> str:
    """The user-facing part of a catalog model name.

    Catalog names carry packaging details for the Models tab, e.g. "FLUX.1 Fill
    [dev] (first-party safetensors, fp8 at load time)" or "MAT Places512 (fp16)
    - prompt-free object removal". Messages about a missing or running model
    only need the model itself: everything from the first " (" or " - " on is
    dropped. A name that would become empty is returned unchanged.
    """
    text = (name or "").strip()
    for marker in (" (", " - "):
        index = text.find(marker)
        if index > 0:
            text = text[:index]
    return text.strip() or (name or "").strip()


def describe_node_errors(node_errors: dict, graph: dict) -> str:
    """Turn ComfyUI ``node_errors`` into one readable line per problem."""
    lines = []
    for node_id, info in (node_errors or {}).items():
        class_type = graph.get(node_id, {}).get("class_type", node_id)
        for err in info.get("errors", []):
            msg = err.get("message", "")
            details = err.get("details", "")
            extra = err.get("extra_info", {}) or {}
            name = extra.get("input_name")
            text = f"{class_type}: {msg}"
            if name:
                text += f" ({name})"
            if details:
                text += f" - {details}"
            lines.append(text)
    return "\n".join(lines)


# Every node type the workflows above use. The OpenFill GPU server (Remote GPU) runs only jobs
# made of these, so its pairing code runs OpenFill's jobs, not any node a pack offers. A test
# checks that every Graph.add in this file names one of them: add new types here.
NODE_CLASSES = frozenset(
    {
        "CFGGuider",
        "CFGNorm",
        "CLIPLoader",
        "CLIPLoaderGGUF",
        "CLIPTextEncode",
        "CheckpointLoaderSimple",
        "CropMask",
        "DifferentialDiffusion",
        "DualCLIPLoader",
        "DualCLIPLoaderGGUF",
        "EmptyLatentImage",
        "Flux2Scheduler",
        "FluxGuidance",
        "GrowMask",
        "INPAINT_ApplyFooocusInpaint",
        "INPAINT_ColorMatch",
        "INPAINT_ExpandMask",
        "INPAINT_InpaintWithModel",
        "INPAINT_LoadFooocusInpaint",
        "INPAINT_LoadInpaintModel",
        "INPAINT_MaskedBlur",
        "INPAINT_MaskedFill",
        "INPAINT_StabilizeMask",
        "INPAINT_VAEEncodeInpaintConditioning",
        "ImageBlur",
        "ImageCompositeMasked",
        "ImageCrop",
        "ImageFromBatch",
        "ImageScale",
        "ImageToMask",
        "ImageUpscaleWithModel",
        "InpaintModelConditioning",
        "InvertMask",
        "JoinImageWithAlpha",
        "KSampler",
        "KSamplerSelect",
        "LoadBackgroundRemovalModel",
        "LoadImage",
        "LoraLoaderModelOnly",
        "MaskComposite",
        "MaskToImage",
        "ModelSamplingAuraFlow",
        NATIVE_LAMA_NODE,
        "RandomNoise",
        "ReferenceLatent",
        "RemoveBackground",
        "RepeatImageBatch",
        "RepeatLatentBatch",
        "SamplerCustomAdvanced",
        "SaveImage",
        "SeedVR2Conditioning",
        "SeedVR2PostProcessing",
        "SeedVR2Preprocess",
        "SetLatentNoiseMask",
        "SolidMask",
        "SplitSigmasDenoise",
        "TextEncodeQwenImageEditPlus",
        "UNETLoader",
        "UnetLoaderGGUF",
        "UpscaleModelLoader",
        "VAEDecode",
        "VAEDecodeTiled",
        "VAEEncode",
        "VAEEncodeTiled",
        "VAELoader",
    }
)
