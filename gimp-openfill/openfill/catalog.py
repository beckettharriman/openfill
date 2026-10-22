# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Model catalog: loading, validation, install status, template selection,
and the view model of the Models tab.

Two kinds of entries share one schema:

- *bundled* (``models/catalog.json``): the short verified list shipped with the
  plug-in; every file has a published SHA-256.
- *live* (``source`` "huggingface" or "civitai"): candidates found by
  ``hub.py`` within the model families the plug-in can drive. They are checked
  more strictly (:func:`parse_live_entries`): one main model file from a
  Hugging Face ``resolve`` URL or a Civitai download URL, an exact size, a
  data-only format (safetensors or GGUF), and every other file, custom node and
  companion model taken verbatim from the bundled list.

A live entry the user chose to download is remembered in
``<profile>/openfill/hub-selected.json`` (:func:`remember_live_entry`) so the
plug-in and the supervisor's task worker know it later (:func:`load_merged`).
Nothing is ever downloaded automatically. This module has no GIMP dependency.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .backend import ModelSpec
from .downloads import safe_relative_path, validate_node_spec

TIERS = ("fast", "quality", "edit", "removal")
CAPABILITIES = ("fill", "expand", "txt2img", "remove", "edit", "patch")
SOURCE_TYPES = ("first-party", "community-quantization")
TEMPLATES = ("sd15_inpaint", "sdxl_fooocus", "flux_fill", "qwen_edit", "inpaint_model")
ARCHS = {"sd15": "sd15", "sdxl": "sdxl", "flux": "flux", "qwen_image": "qwen_image", "inpaint_model": "inpaint_model"}
SOURCES = ("bundled", "huggingface", "civitai")

# Model families the plug-in can drive (one workflow template each), in display order.
FAMILIES: dict[str, str] = {
    "sd15-inpaint": "Stable Diffusion 1.5 inpainting",
    "sdxl": "Stable Diffusion XL",
    "flux-fill": "FLUX.1 Fill",
    "qwen-edit": "Qwen-Image-Edit",
    "removal": "Object removal",
}
FAMILY_OF_TEMPLATE = {
    "sd15_inpaint": "sd15-inpaint",
    "sdxl_fooocus": "sdxl",
    "flux_fill": "flux-fill",
    "qwen_edit": "qwen-edit",
    "inpaint_model": "removal",
}
TEMPLATE_ARCH = {"sd15_inpaint": "sd15", "sdxl_fooocus": "sdxl", "flux_fill": "flux", "qwen_edit": "qwen_image"}
# Operations of the plug-in and the capabilities that can serve them.
OPERATION_CAPABILITIES = {
    "fill": ("fill",),
    "expand": ("expand",),
    "txt2img": ("txt2img",),
    "edit": ("edit",),
    "remove": ("remove", "fill"),  # Remove object falls back to a prompt-led fill without LaMa/MAT
}
NO_MODEL_MESSAGE = "No AI model is installed yet. Download the two starting models below to begin."
HUB_SELECTED_FILENAME = "hub-selected.json"

# Live entries: where the main file may come from and where it may go.
_HF_RESOLVE_RE = re.compile(
    r"^https://huggingface\.co/([A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*)/resolve/"
    r"([0-9a-f]{40}|main)/((?:[A-Za-z0-9_][A-Za-z0-9_.+-]*/)*[A-Za-z0-9_][A-Za-z0-9_.+-]*)$"
)
_CIVITAI_RE = re.compile(r"^https://civitai\.com/api/download/models/(\d+)(\?[A-Za-z0-9=&%._-]*)?$")
_LIVE_ID_RE = re.compile(r"^(hf|civitai)-[A-Za-z0-9_.+-]{1,160}$")
LIVE_EXTENSIONS = (".safetensors", ".gguf")  # data-only formats; pickled .ckpt/.pt/.bin are never taken live
LIVE_MAIN_DEST = {"checkpoint": ("checkpoints",), "unet": ("unet", "diffusion_models")}
LIVE_PAGE_PREFIXES = ("https://huggingface.co/", "https://civitai.com/models/")


class CatalogError(ValueError):
    """The catalog file is missing, malformed or names an unknown model."""


# Role of the small TAESD decoder ComfyUI draws the live preview with (models/vae_approx). Downloaded
# with the model, but a model without it is still installed: only the preview is missing.
PREVIEW_ROLE = "preview"


def missing_previews(entry: ModelEntry, models_root: Path) -> list[FileSpec]:
    """The entry's preview decoders that are not on disk. ComfyUI takes any file named
    ``<decoder>.<ext>`` in the folder (newer ComfyUI builds ship ``taesd_decoder.safetensors``),
    so one under another extension counts too."""
    out = []
    for f in entry.files:
        if f.role != PREVIEW_ROLE or file_installed(models_root, f):
            continue
        folder = (Path(models_root) / f.dest).parent
        stem = f.filename.rsplit(".", 1)[0] + "."
        try:
            if any(p.name.startswith(stem) and p.stat().st_size > 0 for p in folder.iterdir()):
                continue
        except OSError:
            pass
        out.append(f)
    return out


@dataclass(frozen=True)
class FileSpec:
    """One downloadable file of a catalog entry."""

    role: str
    url: str
    dest: str  # relative to <ComfyUI>/models
    size: int | None = None
    sha256: str | None = None
    gated: bool = False
    optional: bool = False  # skip if another entry already provided it
    license: str = ""

    @property
    def filename(self) -> str:
        """Base name of ``dest``."""
        return self.dest.replace("\\", "/").rsplit("/", 1)[-1]

    @property
    def loader_name(self) -> str:
        """The name ComfyUI's loaders use: ``dest`` below its model folder (``/`` separators)."""
        return self.dest.replace("\\", "/").partition("/")[2] or self.filename

    @property
    def subfolder(self) -> str:
        """Folder part of ``dest`` (empty when it has none)."""
        parts = self.dest.replace("\\", "/").rsplit("/", 1)
        return parts[0] if len(parts) == 2 else ""


@dataclass(frozen=True)
class NodeSpec:
    """A ComfyUI custom node pack, pinned to a commit."""

    repo: str
    folder: str
    commit: str
    license: str = ""


@dataclass(frozen=True)
class ModelEntry:
    """A validated catalog model (see ``models/catalog.json``)."""

    id: str
    name: str
    tier: str
    capabilities: tuple[str, ...]
    arch: str
    template: str | None
    page: str
    license_name: str
    license_url: str
    gated: bool
    source_type: str
    uploader: str
    min_vram_gb: float
    requires_nodes: tuple[NodeSpec, ...]
    requires_models: tuple[str, ...]
    files: tuple[FileSpec, ...]
    source: str = "bundled"  # bundled | huggingface | civitai
    family: str = ""  # key of FAMILIES ("" for companion files such as the Fooocus patch)
    downloads: int = 0  # live entries: recent downloads per month (see hub.py)
    updated: str = ""  # live entries: ISO date of the last upload
    repo: str = ""  # live entries: Hugging Face repository or Civitai model name

    @property
    def verified(self) -> bool:
        """Bundled entries are the hand-checked list."""
        return self.source == "bundled"

    @property
    def checksum_known(self) -> bool:
        """Every file has a SHA-256 to verify the download against."""
        return all(f.sha256 for f in self.files)

    @property
    def total_size(self) -> int:
        """Sum of the known file sizes in bytes."""
        return sum(f.size or 0 for f in self.files)

    @property
    def runnable(self) -> bool:
        """False for companion entries (patches, heads) that cannot generate on their own."""
        return self.template is not None

    def file_by_role(self, role: str) -> FileSpec | None:
        """The first file with ``role``, if any."""
        for f in self.files:
            if f.role == role:
                return f
        return None


@dataclass
class InstallStatus:
    """What is on disk for an entry, as shown in the Models tab."""

    installed: bool
    missing: list[str] = field(default_factory=list)  # dest paths not on disk
    partial: list[str] = field(default_factory=list)  # .part files present
    missing_nodes: list[str] = field(default_factory=list)
    missing_models: list[str] = field(default_factory=list)  # required companion entries
    seen_by_server: bool | None = None

    @property
    def label(self) -> str:
        """Short status text: Installed, Needs node, Needs model, Partial or Available."""
        if self.installed and not self.missing_nodes and not self.missing_models:
            return "Installed"
        if self.installed and (self.missing_nodes or self.missing_models):
            return "Needs node" if self.missing_nodes else "Needs model"
        if self.partial:
            return "Partial"
        return "Available"


# ------------------------------------------------------------------ load ----


def _req(d: dict[str, Any], key: str, where: str) -> Any:
    if key not in d:
        raise CatalogError(f"{where}: missing '{key}'")
    return d[key]


def parse_catalog(data: dict[str, Any], source: str = "bundled", known_ids: Iterable[str] = ()) -> list[ModelEntry]:
    """Validate the decoded catalog JSON and return its entries.

    Besides the schema this enforces the security rules: https URLs only, file
    destinations that are plain relative paths (no ``..``, no drive, no absolute
    path) ending in a plain file name, lowercase hex SHA-256 values, positive
    integer sizes, and custom node repos on GitHub pinned to a full
    40-character commit. Bundled entries must give every file a SHA-256.
    ``known_ids`` are ids defined elsewhere (the bundled list, for live
    entries) that ``requires_models`` may name.
    """
    if source not in SOURCES:
        raise CatalogError(f"unknown source '{source}'")
    if not isinstance(data, dict) or "models" not in data:
        raise CatalogError("catalog must be an object with a 'models' list")
    entries: list[ModelEntry] = []
    seen: set[str] = set()
    for raw in data["models"]:
        if not isinstance(raw, dict):
            raise CatalogError("every model must be an object")
        where = f"model '{raw.get('id', '?')}'"
        mid = _req(raw, "id", where)
        if not isinstance(mid, str) or not mid or any(c in mid for c in " /\\"):
            raise CatalogError(f"{where}: invalid id")
        if mid in seen:
            raise CatalogError(f"{where}: duplicate id")
        seen.add(mid)
        tier = _req(raw, "tier", where)
        if tier not in TIERS:
            raise CatalogError(f"{where}: unknown tier '{tier}'")
        caps = tuple(_req(raw, "capabilities", where))
        for c in caps:
            if c not in CAPABILITIES:
                raise CatalogError(f"{where}: unknown capability '{c}'")
        template = raw.get("template")
        if template is not None and template not in TEMPLATES:
            raise CatalogError(f"{where}: unknown template '{template}'")
        arch = _req(raw, "arch", where)
        if arch not in ARCHS:
            raise CatalogError(f"{where}: unknown arch '{arch}'")
        src = _req(raw, "source_type", where)
        if src not in SOURCE_TYPES:
            raise CatalogError(f"{where}: unknown source_type '{src}'")
        lic = _req(raw, "license", where)
        if not isinstance(lic, dict) or not lic.get("name") or not lic.get("url"):
            raise CatalogError(f"{where}: license needs name and url")
        files = []
        for fr in _req(raw, "files", where):
            url, dest = _req(fr, "url", where), _req(fr, "dest", where)
            if not isinstance(url, str) or not url.startswith("https://"):
                raise CatalogError(f"{where}: file url must be https: {url}")
            try:
                safe_relative_path(dest)
            except ValueError as err:
                raise CatalogError(f"{where}: unsafe dest '{dest}'") from err
            sha = fr.get("sha256")
            if sha is not None and (
                not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha)
            ):
                raise CatalogError(f"{where}: bad sha256 for {dest}")
            if sha is None and source == "bundled":
                raise CatalogError(f"{where}: bundled file {dest} needs a sha256")
            size = fr.get("size")
            if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size <= 0):
                raise CatalogError(f"{where}: bad size for {dest}")
            files.append(
                FileSpec(
                    fr.get("role", "file"),
                    url,
                    dest,
                    size,
                    sha,
                    bool(fr.get("gated", False)),
                    bool(fr.get("optional", False)),
                    fr.get("license", ""),
                )
            )
        if not files:
            raise CatalogError(f"{where}: no files")
        nodes = tuple(
            NodeSpec(_req(n, "repo", where), _req(n, "folder", where), _req(n, "commit", where), n.get("license", ""))
            for n in raw.get("requires_nodes", [])
        )
        for n in nodes:
            try:
                validate_node_spec(n.repo, n.folder, n.commit)
            except ValueError as err:
                raise CatalogError(f"{where}: node {n.folder} needs a pinned commit: {err}") from err
        try:
            downloads = int(raw.get("downloads") or 0)
        except (TypeError, ValueError):
            downloads = 0
        entries.append(
            ModelEntry(
                id=mid,
                name=_req(raw, "name", where),
                tier=tier,
                capabilities=caps,
                arch=arch,
                template=template,
                page=raw.get("page", ""),
                license_name=lic["name"],
                license_url=lic["url"],
                gated=bool(raw.get("gated", False)) or any(f.gated for f in files),
                source_type=src,
                uploader=raw.get("uploader", ""),
                min_vram_gb=float(raw.get("min_vram_gb", 0)),
                requires_nodes=nodes,
                requires_models=tuple(raw.get("requires_models", [])),
                files=tuple(files),
                source=source,
                family=str(raw.get("family") or FAMILY_OF_TEMPLATE.get(template or "", "")),
                downloads=max(0, downloads),
                updated=str(raw.get("updated") or "")[:10],
                repo=str(raw.get("repo") or ""),
            )
        )
    ids = {e.id for e in entries} | set(known_ids)
    for e in entries:
        for dep in e.requires_models:
            if dep not in ids:
                raise CatalogError(f"model '{e.id}' requires unknown model '{dep}'")
    return entries


def catalog_search_paths(plugin_dir: str | Path) -> list[Path]:
    """Where ``catalog.json`` may live: next to the plug-in (copied install) or in the repo."""
    plugin_dir = Path(plugin_dir)
    return [plugin_dir / "models" / "catalog.json", plugin_dir.parent / "models" / "catalog.json"]


def load_catalog(path: str | Path | None = None, plugin_dir: str | Path | None = None) -> list[ModelEntry]:
    """Load and validate the bundled catalog from ``path`` or the default search paths."""
    candidates = [Path(path)] if path else catalog_search_paths(plugin_dir or Path(__file__).resolve().parent.parent)
    for candidate in candidates:
        if candidate.is_file():
            with open(candidate, encoding="utf-8-sig") as fh:
                return parse_catalog(json.load(fh))
    raise CatalogError("catalog.json not found in " + ", ".join(str(c) for c in candidates))


def by_id(entries: Iterable[ModelEntry], model_id: str) -> ModelEntry:
    """The entry with ``model_id``; raises :class:`CatalogError` when unknown."""
    for e in entries:
        if e.id == model_id:
            return e
    raise CatalogError(f"unknown model id '{model_id}'")


# ------------------------------------------------------------ live entries ----


def live_folder(mid: str) -> str:
    """The folder inside checkpoints/ or unet/ that holds one live model, so two hub
    models with the same file name never share a destination."""
    if len(mid) <= 60:
        return mid
    return mid[:47] + "-" + hashlib.sha1(mid.encode("utf-8")).hexdigest()[:12]  # noqa: S324 - a name, not security


def _live_checks(raw: dict[str, Any], bundled: list[ModelEntry]) -> None:
    """The extra rules for a live entry (see the module docstring); raises CatalogError."""
    mid = raw.get("id")
    where = f"live model '{mid}'"
    if not isinstance(mid, str) or not _LIVE_ID_RE.match(mid):
        raise CatalogError(f"{where}: live ids start with hf- or civitai- and use plain characters")
    source = raw.get("source")
    if source not in ("huggingface", "civitai"):
        raise CatalogError(f"{where}: source must be huggingface or civitai")
    template = raw.get("template")
    if template not in TEMPLATE_ARCH or raw.get("arch") != TEMPLATE_ARCH[template]:
        raise CatalogError(f"{where}: template and arch must be one of the generative families")
    page = raw.get("page", "")
    if not isinstance(page, str) or not page.startswith(LIVE_PAGE_PREFIXES):
        raise CatalogError(f"{where}: page must be on huggingface.co or civitai.com")
    lic = raw.get("license") or {}
    if not isinstance(lic, dict) or not all(isinstance(f, dict) for f in raw.get("files") or []):
        raise CatalogError(f"{where}: license and files must be objects")
    if not all(isinstance(n, dict) for n in raw.get("requires_nodes") or []):
        raise CatalogError(f"{where}: requires_nodes must be objects")
    lic_url = lic.get("url", "")
    if not isinstance(lic_url, str) or not lic_url.startswith("https://"):
        raise CatalogError(f"{where}: license url must be https")
    bundled_files = {(f.url, f.dest, f.size, f.sha256): f for e in bundled for f in e.files}
    bundled_nodes = {(n.repo, n.folder, n.commit) for e in bundled for n in e.requires_nodes}
    bundled_ids = {e.id for e in bundled}
    main = 0
    for fr in raw.get("files") or []:
        key = (fr.get("url"), fr.get("dest"), fr.get("size"), fr.get("sha256"))
        if key in bundled_files:
            continue  # a companion (text encoder, VAE) copied verbatim from the bundled list
        main += 1
        role, url, dest = fr.get("role"), str(fr.get("url") or ""), str(fr.get("dest") or "")
        if role not in LIVE_MAIN_DEST:
            raise CatalogError(f"{where}: the live file must be a checkpoint or a diffusion model")
        folder, _, rest = dest.replace("\\", "/").partition("/")
        sub, _, name = rest.rpartition("/")
        if folder not in LIVE_MAIN_DEST[role] or sub != live_folder(mid) or not name:
            raise CatalogError(
                f"{where}: {role} files go to {' or '.join(LIVE_MAIN_DEST[role])}/{live_folder(mid)}/<file>"
            )
        if not name.lower().endswith(LIVE_EXTENSIONS):
            raise CatalogError(f"{where}: only {', '.join(LIVE_EXTENSIONS)} files are taken from the hubs")
        hf = _HF_RESOLVE_RE.match(url)
        if source == "huggingface" and (hf is None or hf.group(3).rsplit("/", 1)[-1] != name):
            raise CatalogError(f"{where}: not a Hugging Face file URL for {name}: {url}")
        if source == "civitai" and not _CIVITAI_RE.match(url):
            raise CatalogError(f"{where}: not a Civitai download URL: {url}")
        size = fr.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise CatalogError(f"{where}: the file size must be known")
        if fr.get("optional"):
            raise CatalogError(f"{where}: the main file cannot be optional")
    if main != 1:
        raise CatalogError(f"{where}: needs exactly one model file besides the verified companions")
    for n in raw.get("requires_nodes") or []:
        if (n.get("repo"), n.get("folder"), n.get("commit")) not in bundled_nodes:
            raise CatalogError(f"{where}: custom node {n.get('folder')!r} is not one of the verified list")
    for dep in raw.get("requires_models") or []:
        if dep not in bundled_ids:
            raise CatalogError(f"{where}: companion model {dep!r} is not one of the verified list")


def parse_live_entries(
    raws: Iterable[dict[str, Any]], bundled: list[ModelEntry], problems: list[str] | None = None
) -> list[ModelEntry]:
    """Validate live (hub) entries one by one; invalid ones are skipped (and
    described in ``problems``). Ids and main files already in ``bundled`` are dropped."""
    out: list[ModelEntry] = []
    taken = {e.id for e in bundled}
    bundled_urls = {f.url.split("/resolve/", 1)[-1] for e in bundled for f in e.files}
    bundled_shas = {f.sha256 for e in bundled for f in e.files if f.sha256}
    taken_dests = {f.dest.lower() for e in bundled for f in e.files}
    for raw in raws:
        try:
            if not isinstance(raw, dict):
                raise CatalogError("not an object")
            _live_checks(raw, bundled)
            (entry,) = parse_catalog({"models": [raw]}, source=raw["source"], known_ids=taken)
        except (CatalogError, KeyError, TypeError, ValueError, AttributeError) as err:
            if problems is not None:
                problems.append(str(err))
            continue
        main = live_main_file(entry)
        if entry.id in taken or main is None:
            continue
        if (
            main.url.split("/resolve/", 1)[-1] in bundled_urls
            or (main.sha256 and main.sha256 in bundled_shas)
            or main.dest.lower() in taken_dests
        ):
            continue  # the same file is already on the verified list
        taken.add(entry.id)
        taken_dests.add(main.dest.lower())
        out.append(entry)
    return out


def live_main_file(entry: ModelEntry) -> FileSpec | None:
    """The diffusion model file of an entry (checkpoint or unet)."""
    return entry.file_by_role("checkpoint") or entry.file_by_role("unet")


def hub_selected_path(base: str | Path) -> Path:
    """``<profile>/openfill/hub-selected.json``: live entries the user chose to download."""
    return Path(base) / HUB_SELECTED_FILENAME


def _read_selected(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return []
    models = data.get("models") if isinstance(data, dict) else None
    return [m for m in models or [] if isinstance(m, dict)]


def _write_selected(path: Path, models: list[dict[str, Any]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"version": 1, "models": models}, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def remember_live_entry(base: str | Path, raw: dict[str, Any], bundled: list[ModelEntry]) -> ModelEntry:
    """Validate ``raw`` and store it in ``hub-selected.json`` (replacing an entry
    with the same id) so it can be downloaded, resolved and removed later."""
    problems: list[str] = []
    parsed = parse_live_entries([raw], bundled, problems)
    if not parsed:
        raise CatalogError(problems[0] if problems else f"'{raw.get('id')}' duplicates a verified model")
    path = hub_selected_path(base)
    models = [m for m in _read_selected(path) if m.get("id") != raw["id"]]
    models.append(raw)
    _write_selected(path, models)
    return parsed[0]


def record_live_checksum(base: str | Path, model_id: str, dest: str, sha256: str) -> bool:
    """After a download of a file without a published checksum, pin the computed
    SHA-256 so later downloads and repairs are verified. True when recorded."""
    if not isinstance(sha256, str) or not re.match(r"^[0-9a-f]{64}$", sha256):
        return False
    path = hub_selected_path(base)
    models = _read_selected(path)
    changed = False
    for m in models:
        if m.get("id") != model_id:
            continue
        for f in m.get("files") or []:
            if f.get("dest") == dest and not f.get("sha256"):
                f["sha256"] = sha256
                f["sha256_recorded"] = time.strftime("%Y-%m-%d")
                changed = True
    if changed:
        _write_selected(path, models)
    return changed


def load_selected(base: str | Path | None, bundled: list[ModelEntry]) -> list[ModelEntry]:
    """The remembered live entries, validated (invalid ones are left out)."""
    if not base:
        return []
    return parse_live_entries(_read_selected(hub_selected_path(base)), bundled)


def load_merged(
    plugin_dir: str | Path | None = None, base: str | Path | None = None, path: str | Path | None = None
) -> list[ModelEntry]:
    """Bundled catalog plus the remembered live entries of the profile folder ``base``."""
    bundled = load_catalog(path, plugin_dir)
    return bundled + load_selected(base, bundled)


def merge_entries(*groups: Iterable[ModelEntry]) -> list[ModelEntry]:
    """Concatenate entry lists, keeping the first entry of each id."""
    seen: set[str] = set()
    out = []
    for group in groups:
        for e in group:
            if e.id not in seen:
                seen.add(e.id)
                out.append(e)
    return out


# ---------------------------------------------------------------- status ----


def file_installed(models_root: Path, spec: FileSpec) -> bool:
    """True when ``spec.dest`` exists under ``models_root`` with the catalog size."""
    path = Path(models_root) / spec.dest
    if not path.is_file():
        return False
    if spec.size:
        try:
            return path.stat().st_size == spec.size
        except OSError:
            return False
    return True


def install_status(
    entry: ModelEntry,
    models_root: Path,
    custom_nodes_dir: Path | None,
    catalog: Iterable[ModelEntry] = (),
    server_files: Iterable[str] | None = None,
) -> InstallStatus:
    """Compute the :class:`InstallStatus` of ``entry`` (files, nodes, companion models)."""
    missing, partial = [], []
    for f in entry.files:
        if f.role == PREVIEW_ROLE or file_installed(models_root, f):
            continue  # a missing live-preview decoder only costs the preview
        missing.append(f.dest)
        if (Path(models_root) / (f.dest + ".part")).exists():
            partial.append(f.dest)
    missing_nodes = []
    if custom_nodes_dir is not None:
        for n in entry.requires_nodes:
            if not (Path(custom_nodes_dir) / n.folder).is_dir():
                missing_nodes.append(n.folder)
    missing_models = []
    catalog = list(catalog)
    for dep in entry.requires_models:
        try:
            dep_entry = by_id(catalog, dep)
        except CatalogError:
            missing_models.append(dep)
            continue
        if any(not file_installed(models_root, f) for f in dep_entry.files if f.role != PREVIEW_ROLE):
            missing_models.append(dep)
    seen = None
    if server_files is not None:
        names = set(server_files)
        names = {n.replace("\\", "/") for n in names}
        seen = all(f.loader_name in names for f in entry.files if f.role in ("checkpoint", "unet"))
    return InstallStatus(not missing, missing, partial, missing_nodes, missing_models, seen)


def runnable_installed(
    entries: Iterable[ModelEntry],
    models_root: Path,
    custom_nodes_dir: Path | None,
    operation: str = "fill",
) -> list[ModelEntry]:
    """Installed models that can run ``operation`` (fill, expand, txt2img, edit,
    remove) right now: files, companion models and custom nodes all present."""
    entries = list(entries)
    wanted = OPERATION_CAPABILITIES.get(operation, (operation,))
    out = []
    for e in entries:
        if not e.runnable or not any(c in e.capabilities for c in wanted):
            continue
        st = install_status(e, models_root, custom_nodes_dir, entries)
        if st.installed and not st.missing_models and not st.missing_nodes:
            out.append(e)
    return out


# ------------------------------------------------------- default models ----
# Per-operation defaults, best first, from the judged benchmark of 2026-10-09
# (docs/models.md has the numbers). A tier falls back along its list to the
# first installed model; models outside the lists are never picked for a tier.
FAST_PREFERENCE = ("dreamshaper-8-inpainting", "sd15-inpainting")
QUALITY_PREFERENCE = ("realvisxl-v5", "juggernaut-xl-v9", "sdxl-base")
REMOVE_PREFERENCE = ("lama-big", "mat-places512")
TIER_PREFERENCE: dict[str, tuple[str, ...]] = {
    "fast": FAST_PREFERENCE,
    "quality": QUALITY_PREFERENCE,
    "remove": REMOVE_PREFERENCE,
}
# Generative Expand per tier, re-benchmarked separately from fills (docs/models.md,
# docs/evidence/daytime-2026-10-09/expand-models): SD 1.5 inpainting continued
# the scene best, DreamShaper 8 was second, RealVisXL invented new scenery.
EXPAND_SD15_FIRST = ("sd15-inpainting", "dreamshaper-8-inpainting")
EXPAND_PREFERENCE: dict[str, tuple[str, ...]] = {
    "fast": EXPAND_SD15_FIRST,
    "quality": EXPAND_SD15_FIRST,
}
# The automatic active model (no model set active by hand): the Quality default
# first, then the fills that were acceptable most often on the judged cases.
AUTO_ACTIVE_ORDER = ("realvisxl-v5", "dreamshaper-8-inpainting", "sd15-inpainting", "juggernaut-xl-v9", "sdxl-base")
# Families that are only used when picked by hand: FLUX Fill is slow on 8 GB
# (about 4x RealVisXL for the same results), Qwen Edit needs more than 8 GB.
OPT_IN_ARCHS = ("flux", "qwen_image")
# First run: the small starting set, then the quality upgrade.
STARTER_SET = ("lama-big", "dreamshaper-8-inpainting")
QUALITY_UPGRADE = "realvisxl-v5"
OPT_IN_FAMILIES = ("flux-fill", "qwen-edit")
FIRST_RUN_HINT = (
    "Start with LaMa (object removal, 0.2 GB) and DreamShaper 8 Inpainting (fill and expand, 2 GB). "
    "For better prompted fills add RealVisXL V5 (6.5 GB) later."
)


def is_opt_in(entry: ModelEntry) -> bool:
    """True for models the plug-in never picks on its own (FLUX, Qwen)."""
    return entry.arch in OPT_IN_ARCHS


def tier_label(entry: ModelEntry) -> str:
    """Short tier name for menus: Fast, Quality, Quality (slow), Edit or Removal."""
    if entry.arch == "flux":
        return "Quality (slow)"
    return {"fast": "Fast", "quality": "Quality", "edit": "Edit", "removal": "Removal"}.get(entry.tier, "")


def pick_preferred(installed: Iterable[ModelEntry], preference: Iterable[str]) -> ModelEntry | None:
    """The first id of ``preference`` that is among ``installed``, or None."""
    by = {e.id: e for e in installed}
    for mid in preference:
        if mid in by:
            return by[mid]
    return None


def tier_default(installed: Iterable[ModelEntry], tier: str, operation: str = "fill") -> ModelEntry | None:
    """Default model of ``tier`` (fast, quality, remove) for ``operation``
    among ``installed``, following the tier's fallback list; None when none of
    the listed models is installed. Opt-in models are never returned."""
    table = EXPAND_PREFERENCE if operation == "expand" and tier in EXPAND_PREFERENCE else TIER_PREFERENCE
    return pick_preferred([e for e in installed if not is_opt_in(e)], table.get(tier, ()))


def expand_suggestion(installed: Iterable[ModelEntry], picked: ModelEntry | None) -> ModelEntry | None:
    """The Expand default when it is installed and differs from the model the
    user picked; None when there is nothing better to suggest (the pick is the
    default, is unknown, or no default is installed)."""
    best = tier_default(installed, "quality", "expand")
    if best is None or picked is None or best.id == picked.id:
        return None
    return best


def removal_substitute(installed: Iterable[ModelEntry], entry: ModelEntry | None) -> ModelEntry | None:
    """Model for a removal (empty prompt or Remove object) when no LaMa/MAT is
    installed: ``entry`` itself unless it is opt-in (FLUX, Qwen repaint the
    object they were asked to remove); then the Fast, else the Quality default.
    None when only opt-in models are installed."""
    if entry is not None and not is_opt_in(entry):
        return entry
    installed = list(installed)
    return tier_default(installed, "fast") or tier_default(installed, "quality")


def auto_active(installed: Iterable[ModelEntry]) -> ModelEntry | None:
    """The model used when none was set active by hand: ``AUTO_ACTIVE_ORDER``,
    then any other installed generative model, then a removal model. Never an
    opt-in model (FLUX, Qwen)."""
    usable = [e for e in installed if e.runnable and not is_opt_in(e)]
    best = pick_preferred(usable, AUTO_ACTIVE_ORDER)
    if best is not None:
        return best
    generative = [e for e in usable if "fill" in e.capabilities and e.template != "inpaint_model"]
    if generative:
        return generative[0]
    return pick_preferred(usable, REMOVE_PREFERENCE) or (usable[0] if usable else None)


def effective_active(
    entries: Iterable[ModelEntry], models_root: Path, custom_nodes_dir: Path | None, active_id: str
) -> str:
    """The model id a dialog's "active model" means: ``active_id`` when one was
    set by hand, else the automatic choice among the installed models ("" when
    nothing usable is installed)."""
    if active_id:
        return active_id
    best = auto_active(runnable_installed(entries, models_root, custom_nodes_dir, "remove"))
    return best.id if best is not None else ""


def suggest_active(installed: list[ModelEntry], active_id: str) -> str | None:
    """A new active model id when the model set active by hand is no longer
    usable but another installed model is; else None. With no model set by hand
    (``active_id`` empty) the automatic choice applies and nothing is suggested."""
    if not active_id or not installed or any(e.id == active_id for e in installed):
        return None
    best = auto_active(installed)
    return best.id if best is not None else None


# -------------------------------------------------------------- model spec ----


def model_spec_for(entry: ModelEntry, catalog: Iterable[ModelEntry] = ()) -> ModelSpec:
    """Translate a catalog entry into the backend's ModelSpec."""
    if not entry.runnable:
        raise CatalogError(f"model '{entry.id}' is not a runnable model")
    names = {f.role: f.loader_name for f in entry.files}
    spec = ModelSpec(checkpoint=names.get("checkpoint", ""), arch=entry.arch, template=entry.template or "")
    spec.unet = names.get("unet", "")
    spec.clip1 = names.get("clip1", "")
    spec.clip2 = names.get("clip2", "")
    spec.vae = names.get("vae", "")
    spec.inpaint_model = names.get("inpaint_model", "")
    if entry.template == "sdxl_fooocus":
        for dep in entry.requires_models:
            dep_entry = by_id(list(catalog), dep)
            spec.fooocus_head = (dep_entry.file_by_role("fooocus_head") or FileSpec("", "", "")).filename or None
            spec.fooocus_patch = (dep_entry.file_by_role("fooocus_patch") or FileSpec("", "", "")).filename or None
    return spec


def human_size(num: float | None) -> str:
    """``4265437280`` -> ``"4.0 GB"``; ``"?"`` for unknown or zero."""
    if not num:
        return "?"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def default_models_root(comfy_dir: str | Path) -> Path:
    """``<ComfyUI>/models`` for the portable layout or a plain checkout."""
    root = Path(comfy_dir)
    if (root / "ComfyUI" / "models").is_dir():
        return root / "ComfyUI" / "models"
    return root / "models"


def default_custom_nodes_dir(comfy_dir: str | Path) -> Path:
    """``<ComfyUI>/custom_nodes`` for the portable layout or a plain checkout."""
    root = Path(comfy_dir)
    if (root / "ComfyUI" / "custom_nodes").is_dir():
        return root / "ComfyUI" / "custom_nodes"
    return root / "custom_nodes"


# ------------------------------------------------------------- view model ----
# Pure functions behind the Models tab (models_ui.py draws them with GTK).

SECTION_INSTALLED = "installed"
SECTION_RECOMMENDED = "recommended"
SECTION_VERIFIED = "verified"
SECTION_MORE = "more"
SECTION_TITLES = {
    SECTION_INSTALLED: "Installed",
    SECTION_RECOMMENDED: "Recommended for this PC",
    SECTION_VERIFIED: "Verified",
    SECTION_MORE: "More from Hugging Face and Civitai",
}
SOURCE_LABELS = {"bundled": "verified", "huggingface": "Hugging Face", "civitai": "Civitai"}


def model_row(
    entry: ModelEntry,
    status: InstallStatus,
    active_id: str = "",
    has_token: bool = False,
    fit: Any = None,
    gated_files_missing: bool | None = None,
    login_pages: list[str] | None = None,
) -> dict[str, Any]:
    """One row of the Models tab: the keys of ``App.model_rows`` plus origin, family, fit and checksum."""
    label = status.label
    needs_login = entry.gated if gated_files_missing is None else gated_files_missing
    if label == "Available" and needs_login and not has_token:
        label = "Needs token"
    return {
        "id": entry.id,
        "name": entry.name,
        "tier": entry.tier,
        "status": label,
        "installed": status.installed,
        "size": human_size(entry.total_size),
        "bytes": entry.total_size,
        "license": entry.license_name,
        "license_url": entry.license_url,
        "page": entry.page,
        "source": "first-party" if entry.source_type == "first-party" else "community quant",
        "uploader": entry.uploader,
        "gated": entry.gated,
        "needs_login": bool(needs_login),
        "login_pages": list(login_pages if login_pages is not None else ([entry.page] if entry.gated else [])),
        "runnable": entry.runnable,
        "active": entry.id == active_id,
        "min_vram_gb": entry.min_vram_gb,
        "missing_nodes": status.missing_nodes,
        "missing_models": status.missing_models,
        "partial": status.partial,
        "capabilities": list(entry.capabilities),
        "seen_by_server": status.seen_by_server,
        "origin": entry.source,
        "family": entry.family,
        "checksum_known": entry.checksum_known,
        "downloads": entry.downloads,
        "updated": entry.updated,
        "fit": getattr(fit, "level", "unknown") if fit is not None else "unknown",
        "fit_label": getattr(fit, "label", "") if fit is not None else "",
        "fit_reason": getattr(fit, "reason", "") if fit is not None else "",
    }


def _hf_repo_page(url: str) -> str | None:
    m = _HF_RESOLVE_RE.match(url)
    return f"https://huggingface.co/{m.group(1)}" if m else None


def login_pages(entry: ModelEntry, models_root: Path | None = None) -> list[str]:
    """Hugging Face pages whose license must be accepted before the missing gated
    files can be downloaded (the repository of each file, not only the entry's page)."""
    files = [f for f in entry.files if models_root is None or not file_installed(models_root, f)]
    gated = [f for f in files if f.gated]
    if not gated and entry.gated and files and not any(f.gated for f in entry.files):
        return [entry.page] if entry.page.startswith("https://huggingface.co/") else []
    pages: list[str] = []
    for f in gated:
        page = _hf_repo_page(f.url) or entry.page
        if page and page not in pages:
            pages.append(page)
    return pages


def gated_files_missing(entry: ModelEntry, models_root: Path) -> bool:
    """True when a file that needs a Hugging Face login is still to be downloaded."""
    missing = [f for f in entry.files if not file_installed(models_root, f)]
    if any(f.gated for f in entry.files):
        return any(f.gated for f in missing)
    return entry.gated and bool(missing)  # gated as a whole, no per-file flags


def row_status(row: dict[str, Any], current: dict[str, Any] | None = None, pending: Iterable[str] = ()) -> str:
    """Short state shown next to the model name."""
    current = current or {}
    if current.get("model_id") == row["id"]:
        kind = current.get("type")
        if kind == "download":
            total = current.get("total") or 0
            pct = 100.0 * (current.get("done") or 0) / total if total else 0.0
            return f"Downloading {pct:.0f}%"
        if kind == "install_nodes":
            return "Installing extra parts"
        if kind == "remove":
            return "Removing"
        return "Working"
    if row["id"] in set(pending):
        return "Waiting"
    if row.get("active") and row.get("installed") and not row.get("missing_models") and not row.get("missing_nodes"):
        return "Active"
    if row.get("installed"):
        if row.get("missing_models"):
            return "Needs one more download"
        if row.get("missing_nodes"):
            return "Needs extra parts"
        if row.get("seen_by_server") is False:
            return "Installed, restart the AI backend to use it"
        return "Installed"
    if row.get("partial"):
        return "Partly downloaded"
    if row.get("needs_login") and row.get("status") == "Needs token":
        return "Needs a free Hugging Face login"
    return ""


def model_role(row: dict[str, Any]) -> str:
    """Why a model is (or is not) suggested: starting set, quality upgrade, opt-in."""
    if row["id"] in STARTER_SET:
        return "recommended start"
    if row["id"] == QUALITY_UPGRADE:
        return "quality upgrade"
    if row.get("family") == "flux-fill":
        return "Quality (slow), used only when picked"
    return ""


def row_details(row: dict[str, Any]) -> str:
    """The small grey line under the name: size, license, fit, origin, checksum."""
    parts = [str(row.get("size") or "?")]
    role = model_role(row)
    if role:
        parts.insert(0, role)
    if row.get("license"):
        parts.append(str(row["license"]))
    if not row.get("installed") and row.get("fit_label"):
        parts.append(str(row["fit_label"]))
    origin = row.get("origin", "bundled")
    if origin != "bundled":
        parts.append("from " + SOURCE_LABELS.get(origin, origin))
    if not row.get("checksum_known", True):
        parts.append("unverified checksum")
    if not row.get("runnable", True):
        parts.append("helper file for another model")
    return "  ·  ".join(parts)


def _months_since(iso_date: str, now: float | None = None) -> float | None:
    try:
        then = time.mktime(time.strptime(iso_date[:10], "%Y-%m-%d"))
    except (ValueError, OverflowError):
        return None
    return max(0.0, ((now if now is not None else time.time()) - then) / (30.44 * 86400))


FIT_POINTS = {"comfortable": 3.0, "offload": 1.0, "unknown": 0.5, "too_big": -10.0}


def rank_score(row: dict[str, Any], now: float | None = None) -> float:
    """Recommendation score: fits this PC (dominant), popularity (log of monthly
    downloads), recency (up to 2 points, fading over two years), and a bonus
    for the verified list. Higher is better."""
    score = FIT_POINTS.get(row.get("fit", "unknown"), 0.0)
    score += 0.6 * math.log10(1 + max(0, int(row.get("downloads") or 0)))
    months = _months_since(str(row.get("updated") or ""), now)
    if months is not None:
        score += max(0.0, 2.0 - months / 12.0)
    if row.get("origin", "bundled") == "bundled":
        score += 1.0
    if not row.get("checksum_known", True):
        score -= 1.0
    return score


def recommended(rows: list[dict[str, Any]], limit: int = 5, now: float | None = None) -> list[dict[str, Any]]:
    """Up to ``limit`` (3 to 5) not-installed models for this PC: the starting
    set (LaMa, DreamShaper 8) and the quality upgrade (RealVisXL V5) first, in
    that order, then the best fitting one per family, then the next best
    overall. Too-big and opt-in (FLUX, Qwen) models never qualify."""
    candidates = [
        r
        for r in rows
        if r.get("runnable", True)
        and not r.get("installed")
        and not r.get("partial")
        and r.get("fit", "unknown") != "too_big"
        and r.get("family") not in OPT_IN_FAMILIES
    ]
    ranked = sorted(candidates, key=lambda r: -rank_score(r, now))
    first = (*STARTER_SET, QUALITY_UPGRADE)
    picked = [r for mid in first for r in candidates if r["id"] == mid]
    families: set[str] = {r.get("family") or r["id"] for r in picked}
    for r in ranked:
        fam = r.get("family") or r["id"]
        if fam not in families:
            families.add(fam)
            picked.append(r)
    for r in ranked:
        if len(picked) >= limit:
            break
        if r not in picked:
            picked.append(r)
    head = [r for r in picked if r["id"] in first]
    rest = sorted((r for r in picked if r["id"] not in first), key=lambda r: -rank_score(r, now))
    return (head + rest)[:limit]


def build_sections(
    rows: list[dict[str, Any]], limit: int = 5, now: float | None = None
) -> list[tuple[str, str, list[dict[str, Any]]]]:
    """``[(key, title, rows)]`` in display order: Installed (active first, then
    partly downloaded ones), Recommended for this PC, Verified (the bundled list
    not shown above), More from Hugging Face and Civitai (by family, best first).
    Every row appears exactly once; empty sections are left out."""
    installed = [r for r in rows if r.get("installed") or r.get("partial")]
    installed.sort(key=lambda r: (0 if r.get("active") else 1 if r.get("installed") else 2))
    shown = {r["id"] for r in installed}
    rec = [r for r in recommended([r for r in rows if r["id"] not in shown], limit, now)]
    shown |= {r["id"] for r in rec}
    verified = [r for r in rows if r["id"] not in shown and r.get("origin", "bundled") == "bundled"]
    verified.sort(key=lambda r: (0 if r.get("runnable", True) else 1, -rank_score(r, now)))
    shown |= {r["id"] for r in verified}
    order = list(FAMILIES)
    more = [r for r in rows if r["id"] not in shown]
    more.sort(key=lambda r: (order.index(r["family"]) if r.get("family") in order else 99, -rank_score(r, now)))
    sections = [
        (SECTION_INSTALLED, installed),
        (SECTION_RECOMMENDED, rec),
        (SECTION_VERIFIED, verified),
        (SECTION_MORE, more),
    ]
    return [(key, SECTION_TITLES[key], items) for key, items in sections if items]


# ---------------------------------------------------------- login steps ----

TOKEN_PAGE = "https://huggingface.co/settings/tokens"
TOKEN_NEW_PAGE = "https://huggingface.co/settings/tokens/new?tokenType=read"


def gated_steps(row: dict[str, Any]) -> list[tuple[str, str | None, str | None]]:
    """The step-by-step panel for a model that needs a Hugging Face login:
    ``[(text, button label or None, url or None)]`` in plain words."""
    name = row.get("name") or row.get("id")
    pages = list(row.get("login_pages") or ([row["page"]] if row.get("page") else []))
    if len(pages) <= 1:
        first = [
            (
                f"Open the page of {name} and sign in to Hugging Face there (creating an account is free).",
                "Open the model page",
                pages[0] if pages else None,
            )
        ]
        agree = "On that page, read the license and press the button to agree to it."
    else:
        first = [
            (
                f"{name} uses files from {len(pages)} pages. Open each one and sign in to Hugging Face there "
                "(creating an account is free).",
                None,
                None,
            )
        ] + [(f"Page {i + 1}: {page.split('huggingface.co/', 1)[-1]}", "Open", page) for i, page in enumerate(pages)]
        agree = "On each page, read the license and press the button to agree to it."
    return [
        *first,
        (agree + " It may take a few minutes until access is granted.", None, None),
        (
            "Create an access key: on the next page give it any name, keep the type 'Read', "
            "press Create, then copy the key (it starts with hf_).",
            "Create an access key",
            TOKEN_NEW_PAGE,
        ),
        ("Paste the key here and press Save. It stays on this computer.", None, None),
    ]


def state_button(
    row: dict[str, Any], current: dict[str, Any] | None = None, pending: Iterable[str] = (), has_token: bool = False
) -> tuple[str, str | None, bool]:
    """(label, action, sensitive) of a row's single state button.

    ``ui_text.primary_action`` with two adjustments: the login question only
    comes up while a file that needs it is still missing, and prompt-free
    removal models (LaMa, MAT) are never made the active model - Remove object
    picks them up by itself - so they show "Ready" instead of "Set active".
    """
    from . import ui_text

    label, action, sensitive = ui_text.primary_action(
        {**row, "gated": row.get("needs_login", row.get("gated"))}, current, list(pending), has_token
    )
    caps = row.get("capabilities") or []
    if action == "activate" and "remove" in caps and "fill" not in caps:
        return "Ready", None, False
    return label, action, sensitive


def apply_repo_stats(rows: list[dict[str, Any]], stats: dict[str, dict[str, Any]]) -> None:
    """Give bundled rows the download count and date the live search saw for
    their Hugging Face repository, so verified and live models rank on the same
    scale. ``stats`` maps ``owner/name`` to ``{"downloads": int, "updated": "YYYY-MM-DD"}``."""
    for r in rows:
        if r.get("origin", "bundled") != "bundled":
            continue
        page = str(r.get("page") or "")
        repo = page.split("https://huggingface.co/", 1)[-1] if page.startswith("https://huggingface.co/") else ""
        found = stats.get(repo)
        if found:
            r["downloads"] = max(int(r.get("downloads") or 0), int(found.get("downloads") or 0))
            r["updated"] = r.get("updated") or str(found.get("updated") or "")[:10]
