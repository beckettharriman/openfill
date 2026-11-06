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

Companion files are looked up too (see :func:`resolve_companion`): a FLUX.1 Fill file uses
the T5, CLIP-L and VAE files already in ComfyUI's folders, under any of the names and formats
its loaders take, so only what is really missing is downloaded.
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

# ComfyUI reads each of these model folders from both names (folder_paths.py), so a file in
# either one loads under the same name. In ComfyUI's order: for one name in both folders it
# loads the file in the first one.
FOLDER_ALIASES = {
    "clip": ("text_encoders", "clip"),
    "text_encoders": ("text_encoders", "clip"),
    "unet": ("unet", "diffusion_models"),
    "diffusion_models": ("unet", "diffusion_models"),
}
# FLUX's T5 v1.1 XXL: 32128-token vocabulary, 4096 wide (mT5 and UMT5 have 250k+ tokens)
T5_VOCAB, T5_WIDTH = 32128, 4096
T5_EMBEDDINGS = ("shared.weight", "embed_tokens.weight", "token_embd.weight")

# Which kind of file each companion role of a template is (roles not listed are taken as the
# catalog has them). The SD 1.5 and SDXL templates have no companions besides the preview
# decoder, which is optional and looked up by ComfyUI itself.
COMPANION_KINDS = {
    ("flux_fill", "clip1"): "t5xxl",
    ("flux_fill", "clip2"): "clip_l",
    ("flux_fill", "vae"): "flux_vae",
}
GGUF_NODE_FOLDER = "ComfyUI-GGUF"  # loads .gguf text encoders (DualCLIPLoaderGGUF)


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


def _shadowed(rel: str, earlier: Iterable[Path]) -> bool:
    """True when a root ComfyUI reads first already has a file under ``rel``'s loader name
    (its folder or the alias folder): ComfyUI would load that one, so the later file must
    not be counted."""
    folder, _, name = rel.replace("\\", "/").partition("/")
    for root in earlier:
        for alias in FOLDER_ALIASES.get(folder, (folder,)):
            if (Path(root) / alias / name).is_file():
                return True
    return False


def scan(
    models_root: Path | None,
    entries: Iterable[cat.ModelEntry],
    base: Path | None = None,
    shared: Iterable[Path] = (),
) -> tuple[list[cat.ModelEntry], list[dict[str, str]]]:
    """(catalog entries for recognised files, skipped files with the reason). Files the
    catalog already lists (bundled or chosen from the hubs) are left to their own entry.

    ``shared``: model folders of other ComfyUI installs (shared_models.py), read only and
    after ``models_root``, in the order ComfyUI reads them. Their files get entries too,
    marked with the folder (``FileSpec.root``); a file whose name a folder read earlier
    already has is left out (ComfyUI would load the other one).
    """
    roots: list[tuple[Path, str]] = []
    if models_root is not None and Path(models_root).is_dir():
        roots.append((Path(models_root), ""))
    for r in shared:
        if Path(r).is_dir():
            roots.append((Path(r), str(r)))
    if models_root is None or not roots:
        return [], []
    shared_roots = [r for r, tag in roots if tag]
    entries = list(entries)
    known = {f.dest.replace("\\", "/").lower() for e in entries for f in e.files}
    by_id = {e.id: e for e in entries}
    # the GGUF loader node, for a safetensors FLUX file whose text encoder found is a .gguf
    gguf_nodes = tuple(n for e in entries for n in e.requires_nodes if n.folder == GGUF_NODE_FOLDER)[:1]
    cache = _cache(base)
    fresh: dict[str, Any] = {}
    out: list[cat.ModelEntry] = []
    skipped: list[dict[str, str]] = []
    for index, (root, tag) in enumerate(roots):
        for path in candidates(root):
            rel = path.relative_to(root).as_posix()
            if rel.lower() in known:
                continue
            if tag and _shadowed(rel, [r for r, _t in roots[:index]]):  # the folders ComfyUI reads first
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            key = f"{tag}|{rel}" if tag else rel  # the cache is in OpenFill's profile, never in a root
            stamp = f"{st.st_size}:{int(st.st_mtime)}"
            hit = cache.get(key)
            if isinstance(hit, dict) and hit.get("stamp") == stamp:
                found = Found(str(hit.get("family", "")), str(hit.get("note", "")))
            else:
                found = inspect(path)
            fresh[key] = {"stamp": stamp, "family": found.family, "note": found.note}
            template_id = TEMPLATES.get((found.family, path.suffix.lower()))
            template = by_id.get(template_id or "")
            folder = rel.split("/", 1)[0]
            shown = str(path) if tag else rel
            if found.family and folder not in FAMILY_FOLDERS[found.family]:
                where = " or ".join(FAMILY_FOLDERS[found.family])
                skipped.append(
                    {
                        "file": shown,
                        "reason": f"{FAMILY_NAMES[found.family]} model in {folder}/; ComfyUI loads it from {where}/",
                    }
                )
                continue
            if not found.family or template is None:
                reason = found.note or "no matching bundled model to copy settings from"
                skipped.append({"file": shown, "reason": reason})
                continue
            out.append(
                entry_for(
                    path, rel, st.st_size, found.family, template, Path(models_root), gguf_nodes, shared_roots, tag
                )
            )
    _save_cache(base, fresh)
    return out, skipped


# ---------------------------------------------------------------- companions --


def _name(path: Path) -> str:
    """Lower-case file stem with ``-`` and spaces read as ``_``."""
    return path.name.rsplit(".", 1)[0].lower().replace("-", "_").replace(" ", "_")


def _shapes(path: Path) -> dict[str, list[int]] | None:
    """Tensor name -> shape of a header (GGUF dims reversed to PyTorch order); None if unreadable."""
    try:
        if path.suffix.lower() == ".gguf":
            return {k: list(reversed(d)) for k, d in gguf_shapes(path).items()}
        return safetensors_shapes(path)
    except (OSError, ValueError, UnicodeDecodeError, struct.error):
        return None


def compatible(kind: str, path: Path) -> bool:
    """True when ``path`` can stand in for a companion of ``kind``: by name, then by header.

    - ``t5xxl`` (FLUX's T5 v1.1 XXL encoder): ``.safetensors`` or ``.gguf`` whose name holds
      ``t5`` and ``xxl`` (t5xxl_fp16, t5xxl_fp8_e4m3fn, t5-v1_1-xxl-encoder-Q5_K_M, ...), not
      UMT5; the header has the token embedding (``shared.weight``, ``embed_tokens.weight`` or
      GGUF ``token_embd.weight``) with T5's 32128-token vocabulary at 4096 wide, so mT5 and
      UMT5 (250k+ tokens) are refused whatever their name.
    - ``clip_l`` (CLIP-L text encoder): ``.safetensors`` named ``clip_l*``; 768-wide tensors.
    - ``flux_vae``: ``.safetensors``/``.sft`` named ``ae`` or with ``flux`` and ``vae`` in it
      (not FLUX.2's); the decoder takes 16 latent channels.
    """
    name, ext = _name(path), path.suffix.lower()
    if kind == "t5xxl":
        ok = ext in (".safetensors", ".gguf") and "t5" in name and "xxl" in name and "umt5" not in name
    elif kind == "clip_l":
        ok = ext == ".safetensors" and name.startswith("clip_l")
    elif kind == "flux_vae":
        ok = ext in (".safetensors", ".sft") and (
            name == "ae" or ("flux" in name and "vae" in name and "flux2" not in name and "flux_2" not in name)
        )
    else:
        return False
    if not ok:
        return False
    shapes = _shapes(path)
    if not shapes:
        return False
    if kind == "t5xxl":
        embeddings = [v for k, v in shapes.items() if k.endswith(T5_EMBEDDINGS)]
        return any(sorted(v) == sorted([T5_VOCAB, T5_WIDTH]) for v in embeddings)
    if kind == "flux_vae":  # decoder.conv_in: 512 features from 16 latent channels
        return any(d[:2] == [512, 16] and len(d) == 4 for d in shapes.values())
    return any(768 in d for d in shapes.values())


def _folder_files(models_root: Path, folder: str) -> list[Path]:
    root = Path(models_root) / folder
    if not root.is_dir():
        return []
    try:
        return sorted(p for p in root.rglob("*") if p.is_file() and not p.name.startswith("."))
    except OSError:
        return []


def _loaded_first(models_root: Path, rel: str) -> bool:
    """True when ComfyUI loads ``rel`` for its name: no folder it reads earlier (``text_encoders``
    before ``clip``, ``unet`` before ``diffusion_models``) has a file of the same name."""
    folder, _, name = rel.replace("\\", "/").partition("/")
    for earlier in FOLDER_ALIASES.get(folder, (folder,)):
        if earlier == folder:
            return True
        if (Path(models_root) / earlier / name).is_file():
            return False
    return True


def _found(spec: cat.FileSpec, rel: str, size: int, root: str = "") -> cat.FileSpec:
    """A file found in place for ``spec``: no download address and optional, so it is never
    downloaded or replaced, and never deleted (supervisor.TaskWorker._remove)."""
    return cat.FileSpec(spec.role, "", rel, size, None, False, True, "", root)


# A bundled FLUX entry takes a found text encoder or VAE only up to this times the size of the
# catalog's file: the entry was sized for that file (a 9.8 GB fp16 T5 must not replace the
# 3.4 GB GGUF the Q4 entry was made for). The user's own FLUX file takes any compatible one.
BUNDLED_MAX_RATIO = 1.2


def resolve_companion(
    models_root: Path,
    spec: cat.FileSpec,
    kind: str,
    shared: Iterable[Path] = (),
    max_ratio: float | None = None,
) -> cat.FileSpec:
    """The file to use for the companion ``spec`` of kind ``kind`` (see :func:`compatible`).

    Preference, first match wins:

    1. the catalog's own file, where the catalog puts it with the catalog size (``spec``
       itself, unchanged), if ComfyUI loads it for that name;
    2. compatible files in the catalog's folder or its ComfyUI alias (``text_encoders`` and
       ``clip``), in OpenFill's models folder and then in the ``shared`` folders of other
       installs (read only), ordered by: the catalog's file name first, then OpenFill's own
       folder before shared ones (in their order), then the size closest to the catalog
       file's (the variant nearest to what the bundled model was set up with, so an fp8 T5
       wins over fp16 when both are there), then ComfyUI's folder order, then the path
       (alphabetical) so the pick never changes between runs. A file ComfyUI would not load
       for its name (the same name is in a folder or a root it reads first) is never picked,
       and with ``max_ratio`` none larger than that times the catalog file;
    3. ``spec`` unchanged, to be downloaded, when nothing compatible is there.

    A found file is returned as an optional file without a download address, like the
    user's own main file: it is never downloaded or replaced.
    """
    if cat.file_installed(models_root, spec) and _loaded_first(models_root, spec.dest):
        return spec
    folder = spec.dest.replace("\\", "/").split("/", 1)[0]
    folders = FOLDER_ALIASES.get(folder, (folder,))
    roots = [Path(models_root), *[Path(r) for r in shared]]
    limit = max_ratio * spec.size if max_ratio and spec.size else None
    best: tuple[tuple[Any, ...], str, int, str] | None = None
    for r_index, root in enumerate(roots):
        for index, name in enumerate(folders):
            for path in _folder_files(root, name):
                rel = path.relative_to(root).as_posix()
                if not _loaded_first(root, rel) or (r_index and _shadowed(rel, roots[:r_index])):
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if limit is not None and size > limit:
                    continue
                if not compatible(kind, path):
                    continue
                key = (path.name != spec.filename, r_index, abs(size - (spec.size or 0)), index, rel.lower())
                if best is None or key < best[0]:
                    best = (key, rel, size, str(root) if r_index else "")
    if best is None:
        return spec
    _key, rel, size, tag = best
    return _found(spec, rel, size, tag)


def _in_shared(models_root: Path, spec: cat.FileSpec, shared: list[Path]) -> cat.FileSpec | None:
    """``spec``'s own file (same name, catalog size) in OpenFill's alias folder (``text_encoders``
    for ``clip``, ...) or in a shared folder (its folder or the alias), unless a folder ComfyUI
    reads first has that name. Keeps the checksum: it is the catalog's file, found by name and
    size like one in OpenFill's own folder."""
    folder, _, name = spec.dest.replace("\\", "/").partition("/")
    roots = [Path(models_root), *shared]
    for index, root in enumerate(roots):
        tag = str(root) if index else ""
        for alias in FOLDER_ALIASES.get(folder, (folder,)):
            rel = f"{alias}/{name}"
            if not index and rel == spec.dest:
                continue  # the catalog's own place, checked by the caller
            candidate = replace(spec, dest=rel, root=tag)
            if not cat.file_installed(models_root, candidate):
                continue
            if index and _shadowed(rel, roots[:index]):
                return None
            try:
                size = cat.spec_path(models_root, candidate).stat().st_size
            except OSError:
                continue
            return replace(_found(spec, rel, size, tag), sha256=spec.sha256, license=spec.license)
    return None


def share(
    entries: Iterable[cat.ModelEntry], models_root: Path | None, shared: Iterable[Path] = ()
) -> list[cat.ModelEntry]:
    """Catalog entries (bundled and from the hubs) with each missing file taken from what is
    already there: the same file in a ``shared`` folder of another install, or, for the FLUX
    text encoders and VAE, a compatible file in OpenFill's folder or a shared one
    (:func:`resolve_companion`). What is still missing keeps its download. Local entries
    (resolved by :func:`entry_for`) and entries with everything in place stay as they are."""
    entries = list(entries)
    if models_root is None:
        return entries
    shared = [Path(r) for r in shared if Path(r).is_dir()]
    gguf_nodes = tuple(n for x in entries for n in x.requires_nodes if n.folder == GGUF_NODE_FOLDER)[:1]
    out = []
    for e in entries:
        if e.source == SOURCE:
            out.append(e)
            continue
        files = []
        for f in e.files:
            new = f
            if not cat.file_installed(models_root, f):
                new = _in_shared(models_root, f, shared) or f
                kind = COMPANION_KINDS.get((e.template or "", f.role))
                if new is f and kind:
                    new = resolve_companion(models_root, f, kind, shared, BUNDLED_MAX_RATIO)
            files.append(new)
        if any(a is not b for a, b in zip(files, e.files, strict=True)):
            nodes = e.requires_nodes
            if any(f.dest.lower().endswith(".gguf") and not f.url for f in files) and not any(
                n.folder == GGUF_NODE_FOLDER for n in nodes
            ):
                nodes = nodes + gguf_nodes
            e = replace(e, files=tuple(files), requires_nodes=nodes)
        out.append(e)
    return out


def build(
    entries: Iterable[cat.ModelEntry],
    models_root: Path | None,
    base: Path | None = None,
    shared: Iterable[Path] = (),
) -> tuple[list[cat.ModelEntry], list[cat.ModelEntry], list[dict[str, str]]]:
    """(the whole catalog, the local entries in it, the skipped files): ``entries`` with what
    is already on disk taken into account (:func:`share`) plus the user's own model files
    (:func:`scan`), in OpenFill's models folder and the ``shared`` folders. May raise for a
    broken folder; :func:`with_local` is the form that never raises."""
    entries = list(entries)
    shared = list(shared)
    adopted = share(entries, models_root, shared)
    # files a catalog entry already took (also from an alias folder) are not "your file" again
    found, skipped = scan(models_root, adopted, base, shared)
    return cat.merge_entries(adopted, found), found, skipped


def with_local(
    entries: list[cat.ModelEntry],
    models_root: Path | None,
    base: Path | None = None,
    shared: Iterable[Path] = (),
) -> list[cat.ModelEntry]:
    """:func:`build`'s catalog, or ``entries`` unchanged if anything goes wrong (never raises)."""
    try:
        merged, _found, _skipped = build(entries, models_root, base, shared)
    except Exception:  # noqa: BLE001 - a broken folder must not hide the catalog
        return entries
    return merged


def local_id(rel: str) -> str:
    return "local-" + hashlib.sha1(rel.lower().encode("utf-8")).hexdigest()[:12]


def entry_for(
    path: Path,
    rel: str,
    size: int,
    family: str,
    template: cat.ModelEntry,
    models_root: Path | None = None,
    gguf_nodes: tuple[cat.NodeSpec, ...] = (),
    shared: Iterable[Path] = (),
    root: str = "",
) -> cat.ModelEntry:
    """The template entry with ``path`` as its main file. With ``models_root``, each companion
    file is the compatible one already there, if any (:func:`resolve_companion`, also in the
    ``shared`` folders); a ``.gguf`` text encoder found for a template without the GGUF node
    adds ``gguf_nodes``. ``root``: the shared folder ``path`` is in ("" = ``models_root``)."""
    main = cat.live_main_file(template) or template.files[0]
    folder = rel.split("/", 1)[0]
    role = main.role
    # optional: an existing file is never downloaded (it has no download address anyway)
    files = [cat.FileSpec(role, "", rel, size, None, False, True, "", root)]
    shared = [Path(r) for r in shared]
    for f in template.files:
        if f is main:
            continue
        kind = COMPANION_KINDS.get((template.template or "", f.role))
        if models_root is None:
            files.append(f)
        elif kind:
            files.append(resolve_companion(models_root, f, kind, shared))
        elif cat.file_installed(models_root, f):
            files.append(f)
        else:
            files.append(_in_shared(models_root, f, shared) or f)
    nodes = template.requires_nodes
    if any(f.dest.lower().endswith(".gguf") for f in files[1:]) and not any(
        n.folder == GGUF_NODE_FOLDER for n in nodes
    ):
        nodes = nodes + tuple(gguf_nodes)
    stem = path.stem.removesuffix(".inpainting")
    where = f"found in {Path(root) / folder}" if root else f"found in ComfyUI's {folder} folder"
    return replace(
        template,
        id=local_id(f"{root}|{rel}" if root else rel),
        name=f"{stem} ({FAMILY_NAMES[family]}, your file)",
        page="",
        license_name="your own file",
        license_url="",
        gated=False,
        source_type="local",
        uploader=where,
        files=tuple(files),
        requires_nodes=nodes,
        source=SOURCE,
        family=template.family or family,
        repo=rel,
    )
