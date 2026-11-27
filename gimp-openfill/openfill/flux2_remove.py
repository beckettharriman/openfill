# OpenFill - generative fill for GIMP 3
# SPDX-License-Identifier: Apache-2.0
"""Local ComfyUI graph for the optional fal FLUX.2 Klein removal adapter."""

from __future__ import annotations

from .backend import InpaintRequest
from .workflow import Capabilities, Graph, _finish, _flux2_loaders, _load_inputs

LORA_NAME = "fal-flux2-klein-4b-object-remove.safetensors"
PROMPT = "Remove the highlighted object from the scene"


def _flux2_remove_reference(g: Graph, req: InpaintRequest, image: list, box: tuple[int, int, int, int]) -> list:
    """Draw the adapter's red outline on the reference image only."""
    left, top, right, bottom = box
    x = max(0, min(req.work_width - 1, round(left * req.work_width / req.width)))
    y = max(0, min(req.work_height - 1, round(top * req.work_height / req.height)))
    width = max(1, min(req.work_width, round(right * req.work_width / req.width)) - x)
    height = max(1, min(req.work_height, round(bottom * req.work_height / req.height)) - y)
    edge = max(1, round(min(req.work_width, req.work_height) * 0.006))
    g.add("highlight_red", "EmptyImage", width=width, height=height, batch_size=1, color=0xFF0000)
    g.add("highlight_outer", "SolidMask", value=1.0, width=width, height=height)
    outline = g.out("highlight_outer")
    if width > 2 * edge and height > 2 * edge:
        g.add("highlight_inner", "SolidMask", value=1.0, width=width - 2 * edge, height=height - 2 * edge)
        g.add(
            "highlight_outline",
            "MaskComposite",
            destination=outline,
            source=g.out("highlight_inner"),
            x=edge,
            y=edge,
            operation="subtract",
        )
        outline = g.out("highlight_outline")
    g.add(
        "highlight_reference",
        "ImageCompositeMasked",
        destination=image,
        source=g.out("highlight_red"),
        mask=outline,
        x=x,
        y=y,
        resize_source=False,
    )
    return g.out("highlight_reference")


def build_remove(
    req: InpaintRequest,
    image_ref: str,
    mask_ref: str,
    seed: int,
    caps: Capabilities,
    filename_prefix: str,
) -> dict:
    """Mark the object in a reference and composite the sampled patch through the selection."""
    box = req.flux2_remove_box
    if box is None:
        raise ValueError("FLUX.2 object removal needs a selection box")

    g = Graph()
    model, clip, vae = _flux2_loaders(g, req.model)
    image, mask = _load_inputs(g, req, image_ref, mask_ref, caps)
    reference = _flux2_remove_reference(g, req, image, box)
    g.add("remove_lora", "LoraLoaderModelOnly", model=model, lora_name=req.model.lora, strength_model=1.1)
    g.add("pos", "CLIPTextEncode", text=req.prompt, clip=clip)
    g.add("encode", "VAEEncode", pixels=reference, vae=vae)
    g.add("pos_ref", "ReferenceLatent", conditioning=g.out("pos"), latent=g.out("encode"))
    g.add("guider", "BasicGuider", model=g.out("remove_lora"), conditioning=g.out("pos_ref"))
    batch = max(1, int(req.sampling.batch))
    g.add("empty", "EmptyFlux2LatentImage", width=req.work_width, height=req.work_height, batch_size=batch)
    g.add("scheduler", "Flux2Scheduler", steps=int(req.sampling.steps), width=req.work_width, height=req.work_height)
    g.add("sampler_select", "KSamplerSelect", sampler_name="euler")
    g.add("noise", "RandomNoise", noise_seed=int(seed))
    g.add(
        "sampler",
        "SamplerCustomAdvanced",
        noise=g.out("noise"),
        guider=g.out("guider"),
        sampler=g.out("sampler_select"),
        sigmas=g.out("scheduler"),
        latent_image=g.out("empty"),
    )
    g.add("decode", "VAEDecode", samples=g.out("sampler"), vae=vae)
    if batch > 1:
        g.add("reference_batch", "RepeatImageBatch", image=image, amount=batch)
        image = g.out("reference_batch")
    g.add(
        "remove_composite",
        "ImageCompositeMasked",
        destination=image,
        source=g.out("decode"),
        mask=mask,
        x=0,
        y=0,
        resize_source=False,
    )
    return _finish(g, req, g.out("remove_composite"), mask, caps, filename_prefix)
