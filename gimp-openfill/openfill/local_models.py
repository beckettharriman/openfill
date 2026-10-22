# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Models already in ComfyUI's folders that the catalog does not list (your own fine-tunes).

Only ``.safetensors`` and ``.gguf`` files are looked at, and only their headers are read:
both formats describe every tensor (name and shape) in a plain header, so the model family
follows from a few tell-tale tensors without loading the weights, and nothing in them can
run code. Pickled files (``.ckpt``, ``.pt``, ``.bin``) are never opened.

Families OpenFill can drive, and how they are recognised:

- SD 1.5 inpainting: the UNet's first convolution takes 9 channels (4 latent, 4 masked
  image, 1 mask) at 320 features, and cross-attention reads 768-wide CLIP text.
- SDXL: the UNet has ``label_emb`` (SDXL's size conditioning) and a 4-channel input; it
  runs with the Fooocus inpaint patch, like the bundled SDXL models.
- FLUX.1 Fill: a FLUX transformer (``double_blocks``) whose ``img_in`` takes 384 features
  (FLUX.1 dev and schnell take 64 and cannot fill).

Each recognised file becomes a catalog entry copied from the bundled model of its family
(settings, companion files such as the FLUX text encoders and VAE, custom nodes), with the
file itself as the main file. Results are cached by path, size and modification time.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import catalog as cat

FOLDERS = ("checkpoints", "unet", "diffusion_models")
EXTENSIONS = (".safetensors", ".gguf")
MAX_HEADER = 64 * 1024 * 1024  # bigger headers are not a single image model
CACHE_FILENAME = "local-models.json"
SOURCE = "local"

# Where each family is loaded from: checkpoints by CheckpointLoaderSimple, FLUX by the UNet loaders.
FAMILY_FOLDERS = {"sd15-inpaint": ("checkpoints",), "sdxl": ("checkpoints",), "flux-fill": ("unet", "diffusion_models")}

# Bundled entries the local ones are copied from, per family and file type.
TEMPLATES = {
    ("sd15-inpaint", ".safetensors"): "dreamshaper-8-inpainting",
    ("sdxl", ".safetensors"): "realvisxl-v5",
    ("flux-fill", ".gguf"): "flux1-fill-dev-gguf-q4",
    ("flux-fill", ".safetensors"): "flux1-fill-dev",
}
FAMILY_NAMES = {"sd15-inpaint": "SD 1.5 inpainting", "sdxl": "SDXL", "flux-fill": "FLUX.1 Fill"}


@dataclass(frozen=True)
class Found:
    """What a header says: ``family`` ("" when OpenFill cannot drive it) and why."""

    family: str
    note: str = ""


# ------------------------------------------------------------------- headers --


def safetensors_shapes(path: Path) -> dict[str, list[int]]:
    """Tensor name -> shape from a ``.safetensors`` header; raises ValueError if it is not one."""
    with open(path, "rb") as fh:
        raw = fh.read(8)
        if len(raw) != 8:
            raise ValueError("too short for a safetensors file")
        (length,) = struct.unpack("<Q", raw)
        if not 2 <= length <= MAX_HEADER:
            raise ValueError("not a safetensors header")
        header = json.loads(fh.read(length).decode("utf-8"))
    if not isinstance(header, dict):
        raise ValueError("not a safetensors header")
    return {
        k: [int(d) for d in v.get("shape", [])]
        for k, v in header.items()
        if k != "__metadata__" and isinstance(v, dict)
    }


_GGUF_SCALARS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}


def gguf_shapes(path: Path) -> dict[str, list[int]]:
    """Tensor name -> dims (GGUF order, innermost first) from a ``.gguf`` header."""
    with open(path, "rb") as fh:
        data = fh.read(MAX_HEADER)
    pos = 0

    def take(fmt: str) -> Any:
        nonlocal pos
        size = struct.calcsize(fmt)
        if pos + size > len(data):
            raise ValueError("GGUF header longer than expected")
        (value,) = struct.unpack_from(fmt, data, pos)
        pos += size
        return value

    def string() -> str:
        nonlocal pos
        n = take("<Q")
        if n > 1_000_000 or pos + n > len(data):
            raise ValueError("GGUF string too long")
        s = data[pos : pos + n].decode("utf-8", "replace")
        pos += n
        return s

    def value(kind: int) -> None:
        nonlocal pos
        if kind in _GGUF_SCALARS:
            take(_GGUF_SCALARS[kind])
        elif kind == 8:
            string()
        elif kind == 9:
            inner, count = take("<I"), take("<Q")
            if count > 10_000_000:
                raise ValueError("GGUF array too long")
            if inner in _GGUF_SCALARS:
                pos += struct.calcsize(_GGUF_SCALARS[inner]) * count
            else:
                for _ in range(count):
                    value(inner)
        else:
            raise ValueError(f"unknown GGUF value type {kind}")

    if data[:4] != b"GGUF":
        raise ValueError("not a GGUF file")
    pos = 4
    version = take("<I")
    if version < 2:
        raise ValueError("GGUF version 1 is not supported")
    tensors, kvs = take("<Q"), take("<Q")
    if tensors > 100_000 or kvs > 100_000:
        raise ValueError("implausible GGUF header")
    for _ in range(kvs):
        string()
        value(take("<I"))
    shapes: dict[str, list[int]] = {}
    for _ in range(tensors):
        name = string()
        dims = [take("<Q") for _ in range(take("<I"))]
        take("<I")  # type
        take("<Q")  # offset
        shapes[name] = dims
    return shapes


def classify(shapes: dict[str, list[int]]) -> Found:
    """The family of a model from its tensor shapes (any key prefix)."""

    def find(suffix: str) -> list[int] | None:
        for k, v in shapes.items():
            if k.endswith(suffix):
                return v
        return None

    img_in = find("img_in.weight")
    if img_in is not None and find("double_blocks.0.img_attn.qkv.weight") is not None:
        if 384 in img_in and 3072 in img_in:
            return Found("flux-fill")
        return Found("", "a FLUX model that is not FLUX.1 Fill (dev and schnell cannot fill a selection)")
    conv_in = find("diffusion_model.input_blocks.0.0.weight")
    if conv_in is None or len(conv_in) != 4:
        return Found("", "not an SD 1.5, SDXL or FLUX.1 Fill model")
    channels = conv_in[1]
    if find("diffusion_model.label_emb.0.0.weight") is not None:
        if channels == 4:
            return Found("sdxl")
        return Found(
            "", "an SDXL inpainting checkpoint (OpenFill uses regular SDXL checkpoints with the Fooocus patch)"
        )
    attn = find("diffusion_model.input_blocks.1.1.transformer_blocks.0.attn2.to_k.weight")
    if attn is not None and attn[-1] != 768:
        return Found("", "a Stable Diffusion 2 model")
    if conv_in[0] == 320 and channels == 9:
        return Found("sd15-inpaint")
    if conv_in[0] == 320 and channels == 4:
        return Found("", "a regular SD 1.5 checkpoint (OpenFill needs the inpainting version)")
    return Found("", "an unknown Stable Diffusion variant")


def inspect(path: Path) -> Found:
    """Classify one file by its header; never raises."""
    try:
        if path.suffix.lower() == ".gguf":
            return classify(gguf_shapes(path))
        return classify(safetensors_shapes(path))
    except (OSError, ValueError, UnicodeDecodeError, struct.error) as err:
        return Found("", f"unreadable header ({err})")


# ---------------------------------------------------------------------- scan --


def _cache(base: Path | None) -> dict[str, Any]:
    if base is None:
        return {}
    try:
        data = json.loads((Path(base) / CACHE_FILENAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(base: Path | None, data: dict[str, Any]) -> None:
    if base is None:
        return
    try:
        tmp = Path(base) / (CACHE_FILENAME + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, Path(base) / CACHE_FILENAME)
    except OSError:
        pass


def candidates(models_root: Path) -> list[Path]:
    """Model files in the folders a checkpoint or diffusion model can be loaded from."""
    out = []
    for folder in FOLDERS:
        root = Path(models_root) / folder
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.suffix.lower() in EXTENSIONS and not p.name.startswith("."):
                out.append(p)
    return out


def scan(
    models_root: Path | None, entries: Iterable[cat.ModelEntry], base: Path | None = None
) -> tuple[list[cat.ModelEntry], list[dict[str, str]]]:
    """(catalog entries for recognised files, skipped files with the reason). Files the
    catalog already lists (bundled or chosen from the hubs) are left to their own entry."""
    if models_root is None or not Path(models_root).is_dir():
        return [], []
    entries = list(entries)
    known = {f.dest.replace("\\", "/").lower() for e in entries for f in e.files}
    by_id = {e.id: e for e in entries}
    cache = _cache(base)
    fresh: dict[str, Any] = {}
    out: list[cat.ModelEntry] = []
    skipped: list[dict[str, str]] = []
    for path in candidates(Path(models_root)):
        rel = path.relative_to(models_root).as_posix()
        if rel.lower() in known:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        stamp = f"{st.st_size}:{int(st.st_mtime)}"
        hit = cache.get(rel)
        if isinstance(hit, dict) and hit.get("stamp") == stamp:
            found = Found(str(hit.get("family", "")), str(hit.get("note", "")))
        else:
            found = inspect(path)
        fresh[rel] = {"stamp": stamp, "family": found.family, "note": found.note}
        template_id = TEMPLATES.get((found.family, path.suffix.lower()))
        template = by_id.get(template_id or "")
        folder = rel.split("/", 1)[0]
        if found.family and folder not in FAMILY_FOLDERS[found.family]:
            where = " or ".join(FAMILY_FOLDERS[found.family])
            skipped.append(
                {
                    "file": rel,
                    "reason": f"{FAMILY_NAMES[found.family]} model in {folder}/; ComfyUI loads it from {where}/",
                }
            )
            continue
        if not found.family or template is None:
            skipped.append({"file": rel, "reason": found.note or "no matching bundled model to copy settings from"})
            continue
        out.append(entry_for(path, rel, st.st_size, found.family, template))
    _save_cache(base, fresh)
    return out, skipped


def with_local(
    entries: list[cat.ModelEntry], models_root: Path | None, base: Path | None = None
) -> list[cat.ModelEntry]:
    """``entries`` plus the recognised local files (see :func:`scan`); never raises."""
    try:
        found, _skipped = scan(models_root, entries, base)
    except Exception:  # noqa: BLE001 - a broken folder must not hide the catalog
        return entries
    return cat.merge_entries(entries, found)


def local_id(rel: str) -> str:
    return "local-" + hashlib.sha1(rel.lower().encode("utf-8")).hexdigest()[:12]


def entry_for(path: Path, rel: str, size: int, family: str, template: cat.ModelEntry) -> cat.ModelEntry:
    """The template entry with ``path`` as its main file."""
    main = cat.live_main_file(template) or template.files[0]
    folder = rel.split("/", 1)[0]
    role = main.role
    # optional: an existing file is never downloaded (it has no download address anyway)
    files = [cat.FileSpec(role, "", rel, size, None, False, True, "")]
    files += [f for f in template.files if f is not main]
    stem = path.stem.removesuffix(".inpainting")
    return replace(
        template,
        id=local_id(rel),
        name=f"{stem} ({FAMILY_NAMES[family]}, your file)",
        page="",
        license_name="your own file",
        license_url="",
        gated=False,
        source_type="local",
        uploader=f"found in ComfyUI's {folder} folder",
        files=tuple(files),
        source=SOURCE,
        family=template.family or family,
        repo=rel,
    )
