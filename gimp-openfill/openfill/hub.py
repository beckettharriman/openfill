# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Live model search: Hugging Face Hub and Civitai, within the model families
the plug-in can drive.

The bundled catalog is a short verified list that ages. This module asks the
two public model hubs which models of each family are popular and recent, and
maps every usable result into the catalog schema, so the Models tab can
recommend current models without anyone maintaining a list:

- ``sd15-inpaint``: Stable Diffusion 1.5 inpainting checkpoints (Hugging Face
  search, Civitai SD 1.5 checkpoints that publish an inpainting version);
- ``sdxl``: regular SDXL 1.0 checkpoints, used with the Fooocus inpaint patch
  (Turbo, Lightning, LCM, Hyper and DMD distillations are excluded: they need
  other sampler settings);
- ``flux-fill``: FLUX.1 Fill [dev], GGUF or single-file safetensors, with the
  verified text encoders and VAE of the bundled FLUX entry;
- ``qwen-edit``: Qwen-Image-Edit (GGUF or safetensors) with the verified text
  encoder and VAE of the bundled Qwen entry;
- ``removal`` (LaMa, MAT) stays bundled-only.

Rules for a result to become a candidate: a single data-only model file
(``.safetensors`` or ``.gguf``; pickled ``.ckpt`` files are never taken from
the hubs) whose exact size is known and lies in the family's size range, not
an excluded distillation, packaging or adult-content variant. Hugging Face
files carry their LFS SHA-256 and are pinned to the repository commit; Civitai
files carry the SHA-256 Civitai computed. Gated Hugging Face repositories are
marked (the API's ``gated`` field), and so are licenses.

Responses are cached in ``<profile>/openfill/hub-cache.json`` for 24 hours;
when the hubs cannot be reached the stale cache is used, and without a cache
the result is empty (the Models tab then shows the bundled list only). Nothing
is downloaded here except the JSON listings, and no token is ever sent.
Standard library only; call :func:`search` from a worker thread.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from . import catalog as cat
from . import hardware
from .downloads import is_safe_name

HF_API = "https://huggingface.co/api/models"
CIVITAI_API = "https://civitai.com/api/v1/models"
CACHE_FILENAME = "hub-cache.json"
CACHE_MAX_AGE = 24 * 3600
USER_AGENT = "openfill/2.0 (+https://github.com/beckettharriman/openfill)"
MAX_RESPONSE = 16 * 1024 * 1024
GB = 1024**3
DETAILS_PER_FAMILY = 10  # Hugging Face repositories looked at per family
KEEP_PER_FAMILY = 8  # candidates kept per family and source

# Lowercase substrings that rule a repository, model or file out.
EXCLUDE_SUBSTRINGS = (
    "nsfw",
    "uncensored",
    "porn",
    "hentai",
    "lewd",
    "lust",
    "ungated",
    "not-for-all-audiences",
    "turbo",
    "lightning",
    "hyper-sd",
    "hyper_sd",
    "hypersd",
    "svdq",
    "nunchaku",
    "dfloat11",
    "mflux",
    "rapid",
    "refiner",
    "instructpix2pix",
    "pix2pix",
    "controlnet",
    "ip-adapter",
    "ipadapter",
    "lora",
    "diffusion_pytorch_model",
    "-of-0",
)
# Whole words (split on anything that is not a letter or a digit) that rule a name out.
EXCLUDE_WORDS = (
    "lcm",
    "dmd",
    "dmd2",
    "aio",
    "mlx",
    "int4",
    "fp4",
    "nf4",
    "bnb",
    "awq",
    "onnx",
    "df11",
    "nude",
    "hyper",
)
NESTED_OK = ("diffusion_models", "unet", "checkpoints")  # sub folders a single-file model may sit in


@dataclass(frozen=True)
class Family:
    """How to find and map one model family."""

    key: str
    base_id: str  # bundled entry whose template, companions and nodes a candidate reuses
    role: str  # role of the main file: checkpoint or unet
    size_min: int
    size_max: int
    require: tuple[tuple[str, ...], ...]  # every group needs one of its substrings in repo + file name
    exclude: tuple[str, ...] = ()
    extensions: tuple[str, ...] = (".safetensors",)
    hf_queries: tuple[dict[str, str], ...] = ()
    civitai_base: str = ""  # Civitai baseModels value
    civitai_inpainting: bool = False  # take the inpainting version of each model


FAMILY_SPECS: dict[str, Family] = {
    "sd15-inpaint": Family(
        "sd15-inpaint",
        "sd15-inpainting",
        "checkpoint",
        int(1.5 * GB),
        int(5.5 * GB),
        (("inpaint",),),
        ("xl", "diffusion-2", "sd2", "sd-2", "sd3", "flux", "768-v"),
        hf_queries=(
            {"search": "inpainting", "sort": "downloads"},
            {"search": "inpainting", "sort": "trendingScore"},
        ),
        civitai_base="SD 1.5",
        civitai_inpainting=True,
    ),
    "sdxl": Family(
        "sdxl",
        "sdxl-base",
        "checkpoint",
        int(5.5 * GB),
        int(7.5 * GB),
        (),
        ("inpaint", "pony", "sd3", "flux", "edit"),
        hf_queries=(
            {"filter": "base_model:finetune:stabilityai/stable-diffusion-xl-base-1.0", "sort": "downloads"},
            {"filter": "base_model:finetune:stabilityai/stable-diffusion-xl-base-1.0", "sort": "trendingScore"},
        ),
        civitai_base="SDXL 1.0",
    ),
    "flux-fill": Family(
        "flux-fill",
        "flux1-fill-dev-gguf-q4",
        "unet",
        3 * GB,
        int(24.5 * GB),
        (("flux",), ("fill",)),
        ("kontext", "redux", "schnell"),
        extensions=(".gguf", ".safetensors"),
        hf_queries=(
            {"search": "FLUX.1-Fill", "sort": "downloads"},
            {"filter": "base_model:quantized:black-forest-labs/FLUX.1-Fill-dev", "sort": "downloads"},
            {"filter": "base_model:finetune:black-forest-labs/FLUX.1-Fill-dev", "sort": "downloads"},
        ),
    ),
    "qwen-edit": Family(
        "qwen-edit",
        "qwen-image-edit-2511-gguf-q4",
        "unet",
        6 * GB,
        42 * GB,
        (("qwen",), ("edit",)),
        ("vl-", "_vl", "text_encoder", "mmproj"),
        extensions=(".gguf", ".safetensors"),
        hf_queries=(
            {"search": "Qwen-Image-Edit", "filter": "gguf", "sort": "downloads"},
            {"search": "Qwen-Image-Edit", "sort": "downloads"},
            {"search": "Qwen-Image-Edit", "sort": "trendingScore"},
        ),
    ),
}

# GGUF quantizations in order of preference for a given amount of graphics memory.
QUANT_ORDER = {
    "large": ("Q8_0", "Q6_K", "Q5_K_M", "Q5_K_S", "Q5_0", "Q4_K_M", "Q4_K_S", "Q4_0"),
    "medium": ("Q5_K_M", "Q5_K_S", "Q4_K_M", "Q4_K_S", "Q4_0", "Q5_0", "Q6_K", "Q4_1"),
    "small": ("Q4_K_M", "Q4_K_S", "Q4_0", "Q4_1", "Q3_K_M", "Q3_K_S", "Q5_K_S", "Q5_0"),
}
_QUANT_RE = re.compile(r"(?i)(?:^|[-_.])((?:I?Q\d(?:_[0-9A-Z]+)*)|BF16|F16|FP16|F32|FP8(?:_E4M3FN)?)(?=[-_.]|$)")


class HubError(RuntimeError):
    """A hub request failed and nothing usable was cached."""


@dataclass
class HubResult:
    """What :func:`search` found."""

    entries: list[dict[str, Any]] = field(default_factory=list)  # raw entries in the catalog schema
    problems: list[str] = field(default_factory=list)  # plain-language notes (failed requests)
    offline: bool = False  # at least one source could not be reached (stale cache or nothing used)
    from_cache: bool = False  # every response came from the cache
    fetched_at: float = 0.0
    counts: dict[str, int] = field(default_factory=dict)  # "<family>/<source>" -> candidates
    repo_stats: dict[str, dict[str, Any]] = field(default_factory=dict)  # HF repo -> downloads, updated

    def summary(self) -> str:
        """One plain line for the Models tab footer."""
        total = len(self.entries)
        if self.offline and not total:
            return "Hugging Face and Civitai could not be reached; showing the verified list only."
        text = f"{total} more models found on Hugging Face and Civitai"
        if self.offline:
            text += " (some results are from an earlier search; the hubs could not be reached)"
        return text + "."


# ------------------------------------------------------------------ cache ----


def _valid_cache_item(url: object, item: object) -> bool:
    """True for a ``{"t": number, "data": ...}`` record under a string key."""
    if not isinstance(url, str) or not isinstance(item, dict) or "data" not in item:
        return False
    t = item.get("t")
    return isinstance(t, (int, float)) and not isinstance(t, bool)


class Cache:
    """URL -> decoded JSON with a fetch time, stored in one JSON file."""

    def __init__(
        self, path: str | Path | None, max_age: float = CACHE_MAX_AGE, now: Callable[[], float] = time.time
    ) -> None:
        self.path = Path(path) if path else None
        self.max_age = max_age
        self.now = now
        self.data: dict[str, dict[str, Any]] = {}
        self.dirty = False
        if self.path is not None:
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                responses = loaded.get("responses") if isinstance(loaded, dict) and loaded.get("version") == 1 else None
                if isinstance(responses, dict):
                    # keep every well-formed entry; one odd record must not drop the rest
                    self.data = {k: v for k, v in responses.items() if _valid_cache_item(k, v)}
            except (OSError, ValueError, RecursionError):
                self.data = {}

    def get(self, url: str, fresh_only: bool = True) -> Any:
        """The cached response, or None (also when older than ``max_age`` and ``fresh_only``)."""
        item = self.data.get(url)
        if not _valid_cache_item(url, item):
            return None
        if fresh_only and self.now() - float(item["t"]) > self.max_age:
            return None
        return item.get("data")

    def put(self, url: str, data: Any) -> None:
        """Store a response."""
        self.data[url] = {"t": self.now(), "data": data}
        self.dirty = True

    def save(self) -> None:
        """Write the file atomically (entries older than a week are dropped)."""
        if self.path is None or not self.dirty:
            return
        cutoff = self.now() - 7 * 86400
        keep = {k: v for k, v in self.data.items() if _valid_cache_item(k, v) and float(v["t"]) >= cutoff}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"version": 1, "responses": keep}), encoding="utf-8")
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError:
            pass


class Fetcher:
    """GET JSON with the cache in front; falls back to a stale cache entry when offline."""

    def __init__(
        self,
        cache: Cache,
        opener: Callable[..., Any] = urlopen,
        timeout: float = 30.0,
        record: Callable[[str, Any], None] | None = None,
        trim: Callable[[str, Any], Any] | None = None,
    ) -> None:
        self.cache = cache
        self.opener = opener
        self.timeout = timeout
        self.record = record
        self.trim = trim or (lambda _url, data: data)
        self.network_used = False
        self.failures: list[str] = []
        self.stale_used = False

    def get(self, url: str) -> Any:
        """Decoded JSON for ``url``; raises :class:`HubError` when neither the network nor the cache has it."""
        cached = self.cache.get(url)
        if cached is not None:
            return cached
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with self.opener(req, timeout=self.timeout) as resp:
                body = resp.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                raise ValueError("response too large")
            data = json.loads(body.decode("utf-8"))
        except (HTTPError, URLError, OSError, ValueError, RecursionError) as err:
            if isinstance(err, HTTPError):
                err.close()
            stale = self.cache.get(url, fresh_only=False)
            self.failures.append(f"{_host(url)}: {getattr(err, 'reason', None) or err}")
            if stale is not None:
                self.stale_used = True
                return stale
            raise HubError(str(err)) from err
        self.network_used = True
        if self.record is not None:
            self.record(url, data)
        data = self.trim(url, data)
        self.cache.put(url, data)
        return data


def _host(url: str) -> str:
    return "Civitai" if "civitai.com" in url else "Hugging Face"


def _trim(url: str, data: Any) -> Any:
    """Keep only the fields the mapping uses (the cache stays small)."""
    if url.startswith(CIVITAI_API) and isinstance(data, dict):
        items = []
        for it in data.get("items") or []:
            if not isinstance(it, dict):
                continue
            keep = {k: it.get(k) for k in ("id", "name", "type", "nsfw", "poi", "availability", "tags")}
            keep["allowCommercialUse"] = it.get("allowCommercialUse")
            keep["creator"] = {"username": (it.get("creator") or {}).get("username")}
            keep["stats"] = {"downloadCount": (it.get("stats") or {}).get("downloadCount")}
            keep["modelVersions"] = [
                {
                    **{
                        k: v.get(k) for k in ("id", "name", "baseModel", "baseModelType", "publishedAt", "availability")
                    },
                    "stats": {"downloadCount": (v.get("stats") or {}).get("downloadCount")},
                    "files": [
                        {
                            k: f.get(k)
                            for k in (
                                "id",
                                "name",
                                "sizeKB",
                                "type",
                                "primary",
                                "metadata",
                                "hashes",
                                "downloadUrl",
                                "pickleScanResult",
                                "virusScanResult",
                            )
                        }
                        for f in v.get("files") or []
                        if isinstance(f, dict)
                    ],
                }
                for v in it.get("modelVersions") or []
                if isinstance(v, dict)
            ]
            items.append(keep)
        return {"items": items}
    if isinstance(data, dict) and "siblings" in data:  # Hugging Face model details
        keep = {k: data.get(k) for k in ("id", "author", "sha", "gated", "downloads", "lastModified", "tags")}
        card = data.get("cardData")
        card = card if isinstance(card, dict) else {}
        keep["cardData"] = {k: card.get(k) for k in ("license", "license_name", "license_link")}
        keep["siblings"] = [
            {"rfilename": s.get("rfilename"), "size": s.get("size"), "lfs": s.get("lfs")}
            for s in data.get("siblings") or []
            if isinstance(s, dict) and str(s.get("rfilename", "")).lower().endswith(cat.LIVE_EXTENSIONS)
        ]
        return keep
    return data


# ---------------------------------------------------------------- helpers ----


def _words(text: str) -> set[str]:
    return set(re.split(r"[^a-z0-9]+", text.lower()))


def excluded(text: str, extra: Iterable[str] = ()) -> bool:
    """True when ``text`` (a repo, model or file name, or tags) names an excluded variant."""
    low = text.lower()
    if any(s in low for s in EXCLUDE_SUBSTRINGS):
        return True
    words = _words(low)
    if any(w in words for w in EXCLUDE_WORDS):
        return True
    return any(s in low for s in extra)


def required(text: str, groups: Iterable[Iterable[str]]) -> bool:
    """Every group has one substring in ``text``."""
    low = text.lower()
    return all(any(s in low for s in group) for group in groups)


def _bad_tags(fam: Family, tags: Any) -> bool:
    """Hugging Face tags: the adult-content tag, or a family exclusion (an SDXL base model for SD 1.5...)."""
    text = " ".join(map(str, tags or [])).lower()
    return "not-for-all-audiences" in text or any(s in text for s in fam.exclude)


def quant_of(name: str) -> str:
    """``flux1-fill-dev-Q4_K_S.gguf`` -> ``Q4_K_S`` ("" when the name carries none)."""
    stem = name.rsplit(".", 1)[0]
    found = _QUANT_RE.findall(stem)
    return found[-1].upper() if found else ""


def vram_class(hw: hardware.Hardware | None) -> str:
    """small (< 10 GB or unknown), medium (10-15 GB) or large (16 GB and more)."""
    vram = hw.vram_gb if hw else None
    if vram is None or vram < 9.5:
        return "small"
    return "medium" if vram < 15.5 else "large"


def pick_file(files: list[dict[str, Any]], hw: hardware.Hardware | None) -> dict[str, Any] | None:
    """The best file of one repository for this PC: a GGUF quantization by the
    preference for its graphics memory, else the largest safetensors that fits
    the graphics memory (with room for offloading), else the smallest one."""
    gguf = [f for f in files if f["name"].lower().endswith(".gguf")]
    if gguf:
        by_quant = {quant_of(f["name"]): f for f in gguf}
        for q in QUANT_ORDER[vram_class(hw)]:
            if q in by_quant:
                return by_quant[q]
        return min(gguf, key=lambda f: f["size"])
    if not files:
        return None
    budget = (hw.vram_bytes * 1.6) if hw and hw.vram_bytes else 9 * GB
    fitting = [f for f in files if f["size"] <= budget]
    if fitting:
        return max(fitting, key=lambda f: (f["size"], f["name"]))
    return min(files, key=lambda f: f["size"])


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.+-]+", "_", text).strip("_.")


def _monthly(count: Any, since_iso: str, now: float) -> int:
    """Downloads per month since ``since_iso`` (Civitai counts are all-time)."""
    try:
        total = int(count or 0)
        months = max(1.0, (now - time.mktime(time.strptime(since_iso[:10], "%Y-%m-%d"))) / (30.44 * 86400))
    except (TypeError, ValueError, OverflowError):
        return 0
    return int(total / months)


def _bundled_parts(base: cat.ModelEntry) -> tuple[list[dict], list[dict], list[str]]:
    """Companion files (marked optional), nodes and companion models of a bundled entry."""
    files = [
        {
            "role": f.role,
            "url": f.url,
            "dest": f.dest,
            "size": f.size,
            "sha256": f.sha256,
            "gated": f.gated,
            "optional": True,
            **({"license": f.license} if f.license else {}),
        }
        for f in base.files
        if f.role not in ("checkpoint", "unet")
    ]
    nodes = [
        {"repo": n.repo, "folder": n.folder, "commit": n.commit, "license": n.license} for n in base.requires_nodes
    ]
    return files, nodes, list(base.requires_models)


def _entry(
    fam: Family,
    base: cat.ModelEntry,
    *,
    source: str,
    mid: str,
    name: str,
    page: str,
    license_name: str,
    license_url: str,
    gated: bool,
    uploader: str,
    main: dict[str, Any],
    downloads: int,
    updated: str,
    repo: str,
) -> dict[str, Any]:
    companions, nodes, models = _bundled_parts(base)
    is_gguf = main["name"].lower().endswith(".gguf")
    folder = cat.live_folder(mid[:170])  # one folder per model: same file names never collide
    if fam.role == "checkpoint":
        dest = f"checkpoints/{folder}/{main['name']}"
    else:
        dest = f"{'unet' if is_gguf else 'diffusion_models'}/{folder}/{main['name']}"
    main_file = {"role": fam.role, "url": main["url"], "dest": dest, "size": main["size"]}
    if main.get("sha256"):
        main_file["sha256"] = main["sha256"]
    if gated:
        main_file["gated"] = True
    stub = type(
        "Stub", (), {"arch": base.arch, "min_vram_gb": 0, "files": [cat.FileSpec(fam.role, "", dest, main["size"])]}
    )
    return {
        "id": mid[:170],
        "name": name,
        "tier": base.tier,
        "capabilities": list(base.capabilities),
        "arch": base.arch,
        "template": base.template,
        "page": page,
        "license": {"name": license_name, "url": license_url},
        "gated": gated,
        "source_type": "community-quantization" if is_gguf else "first-party",
        "uploader": uploader,
        "min_vram_gb": hardware.vram_needed_gb(stub),
        "requires_nodes": nodes,
        "requires_models": models,
        "files": [main_file, *companions],
        "source": source,
        "family": fam.key,
        "downloads": int(downloads),
        "updated": updated[:10],
        "repo": repo,
    }


# ----------------------------------------------------------- hugging face ----


def hf_search_url(params: dict[str, str], limit: int = 30) -> str:
    """The model search URL (sorted descending, with the fields the filter needs)."""
    query = dict(params)
    query.setdefault("sort", "downloads")
    query["direction"] = "-1"
    query["limit"] = str(limit)
    pairs = list(query.items()) + [("expand[]", k) for k in ("downloads", "lastModified", "gated", "tags")]
    return f"{HF_API}?{urlencode(pairs)}"


def hf_details_url(repo: str) -> str:
    """Model details with file sizes and LFS SHA-256 (``blobs=true``)."""
    return f"{HF_API}/{quote(repo, safe='/')}?blobs=true"


def hf_license(details: dict[str, Any], repo: str) -> tuple[str, str]:
    """(name, https url) of a repository's license from its card data and tags."""
    card = details.get("cardData") or {}
    lic = card.get("license") or ""
    if not lic:
        for t in details.get("tags") or []:
            if isinstance(t, str) and t.startswith("license:"):
                lic = t.split(":", 1)[1]
    if isinstance(lic, list):
        lic = lic[0] if lic else ""
    name = str(lic or "not stated")
    if name == "other" and card.get("license_name"):
        name = str(card["license_name"])
    link = str(card.get("license_link") or "")
    page = f"https://huggingface.co/{repo}"
    if link.startswith("https://"):
        url = link
    elif link and re.match(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$", link):
        url = f"{page}/blob/main/{link}"
    else:
        url = page
    return name, url


def hf_candidate(
    fam: Family, details: dict[str, Any], base: cat.ModelEntry, hw: hardware.Hardware | None
) -> dict[str, Any] | None:
    """Map one repository's details to a raw catalog entry, or None when no file qualifies."""
    repo = str(details.get("id") or "")
    if "/" not in repo or excluded(repo, fam.exclude) or _bad_tags(fam, details.get("tags")):
        return None
    sha = str(details.get("sha") or "")
    if not re.match(r"^[0-9a-f]{40}$", sha):
        return None  # no commit to pin: a moving branch is not downloaded
    rev = sha
    files = []
    for s in details.get("siblings") or []:
        if not isinstance(s, dict):
            continue
        path = str(s.get("rfilename") or "")
        name = path.rsplit("/", 1)[-1]
        folder = path.rsplit("/", 1)[0] if "/" in path else ""
        if folder and folder.rsplit("/", 1)[-1] not in NESTED_OK:
            continue
        if not name.lower().endswith(fam.extensions) or not is_safe_name(name):
            continue
        if any(not is_safe_name(seg) for seg in path.split("/")):
            continue
        lfs = s.get("lfs")
        lfs = lfs if isinstance(lfs, dict) else {}
        size = lfs.get("size") or s.get("size")
        if not isinstance(size, int) or not fam.size_min <= size <= fam.size_max:
            continue
        if excluded(name, fam.exclude) or not required(repo + " " + name, fam.require):
            continue
        digest = str(lfs.get("sha256") or "").lower()
        files.append(
            {
                "name": name,
                "size": size,
                "sha256": digest if re.match(r"^[0-9a-f]{64}$", digest) else None,
                "url": f"https://huggingface.co/{repo}/resolve/{rev}/{path}",
            }
        )
    main = pick_file(files, hw)
    if main is None:
        return None
    gated = details.get("gated") not in (False, None, "false", "")
    lic_name, lic_url = hf_license(details, repo)
    if lic_name == "not stated":
        return None  # nobody can tell the user what they may do with it
    quant = quant_of(main["name"])
    label = quant or main["name"].rsplit(".", 1)[0][:48]
    return _entry(
        fam,
        base,
        source="huggingface",
        mid="hf-" + _slug(repo.replace("/", "--") + "--" + main["name"].rsplit(".", 1)[0]),
        name=f"{repo} ({label})",
        page=f"https://huggingface.co/{repo}",
        license_name=lic_name,
        license_url=lic_url,
        gated=gated,
        uploader=str(details.get("author") or repo.split("/", 1)[0]),
        main=main,
        downloads=int(details.get("downloads") or 0),
        updated=str(details.get("lastModified") or ""),
        repo=repo,
    )


def hf_family(
    fam: Family,
    fetch: Fetcher,
    base: cat.ModelEntry,
    hw: hardware.Hardware | None,
    workers: int = 4,
    stats: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Search, pre-filter by name, fetch details of the top repositories, map them.
    ``stats`` collects downloads and dates of every listed repository."""
    repos: list[str] = []
    for params in fam.hf_queries:
        try:
            listing = fetch.get(hf_search_url(params))
        except HubError:
            continue
        for m in listing if isinstance(listing, list) else []:
            if not isinstance(m, dict):
                continue
            rid = str(m.get("id") or "")
            if stats is not None and rid:
                stats[rid] = {"downloads": m.get("downloads") or 0, "updated": str(m.get("lastModified") or "")[:10]}
            if rid and rid not in repos and not excluded(rid, fam.exclude) and not _bad_tags(fam, m.get("tags")):
                repos.append(rid)
    repos = repos[:DETAILS_PER_FAMILY]

    def one(repo: str) -> dict[str, Any] | None:
        try:
            details = fetch.get(hf_details_url(repo))
        except HubError:
            return None
        if not isinstance(details, dict):
            return None
        try:
            return hf_candidate(fam, details, base, hw)
        except (AttributeError, TypeError, ValueError, KeyError):
            return None  # one odd repository must not remove every result

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return [e for e in pool.map(one, repos) if e]


# ---------------------------------------------------------------- civitai ----


def civitai_url(base_model: str, limit: int = 30, query: str = "") -> str:
    """Most downloaded public checkpoints of one base model, without adult content."""
    params = [
        ("types", "Checkpoint"),
        ("baseModels", base_model),
        ("sort", "Most Downloaded"),
        ("nsfw", "false"),
        ("limit", str(limit)),
    ]
    if query:
        params.append(("query", query))
    return f"{CIVITAI_API}?{urlencode(params)}"


def _civitai_file(fam: Family, version: dict[str, Any]) -> dict[str, Any] | None:
    best = None
    for f in version.get("files") or []:
        if not isinstance(f, dict):
            continue
        meta = f.get("metadata") if isinstance(f.get("metadata"), dict) else {}
        name = str(f.get("name") or "")
        if f.get("type") != "Model" or meta.get("format") != "SafeTensor":
            continue
        if not name.lower().endswith(".safetensors") or not is_safe_name(name):
            continue
        if f.get("pickleScanResult") not in (None, "Success") or f.get("virusScanResult") not in (None, "Success"):
            continue
        size_kb = f.get("sizeKB")
        if not isinstance(size_kb, (int, float)):
            continue
        size_f = float(size_kb) * 1024
        if abs(size_f - round(size_f)) > 1e-6:
            continue  # the exact byte count is not recoverable
        size = round(size_f)
        if not fam.size_min <= size <= fam.size_max:
            continue
        digest = str((f.get("hashes") or {}).get("SHA256") or "").lower()
        if not re.match(r"^[0-9a-f]{64}$", digest):
            continue
        url = str(f.get("downloadUrl") or "")
        if not cat._CIVITAI_RE.match(url):
            continue
        rank = (bool(f.get("primary")), meta.get("fp") == "fp16", meta.get("size") == "pruned")
        if best is None or rank > best[0]:
            best = (rank, {"name": name, "size": size, "sha256": digest, "url": url, "file_id": f.get("id")})
    return best[1] if best else None


def civitai_candidate(
    fam: Family, item: dict[str, Any], base: cat.ModelEntry, now: float | None = None
) -> dict[str, Any] | None:
    """Map one Civitai model to a raw catalog entry (its newest suitable version), or None."""
    now = time.time() if now is None else now
    name = str(item.get("name") or "")
    if item.get("type") != "Checkpoint" or item.get("nsfw") or item.get("poi"):
        return None
    if item.get("availability") not in (None, "Public"):
        return None
    # Civitai tags say what a model can do ("nsfw" on most general models); its own
    # nsfw flag (and the nsfw=false query) decides, so only the names are checked here.
    if excluded(name, tuple(x for x in fam.exclude if x != "inpaint")):
        return None
    for version in item.get("modelVersions") or []:
        vname = str(version.get("name") or "")
        if version.get("baseModel") != fam.civitai_base or version.get("availability") not in (None, "Public"):
            continue
        is_inpaint = version.get("baseModelType") == "Inpainting" or "inpaint" in vname.lower()
        if fam.civitai_inpainting and not (is_inpaint or "inpaint" in name.lower()):
            continue
        if not fam.civitai_inpainting and (is_inpaint or "inpaint" in name.lower()):
            continue
        if excluded(vname, tuple(x for x in fam.exclude if x != "inpaint")):
            continue
        main = _civitai_file(fam, version)
        if main is None or excluded(main["name"], tuple(x for x in fam.exclude if x != "inpaint")):
            continue
        if fam.civitai_inpainting and "inpaint" not in (name + vname + main["name"]).lower():
            continue
        commercial = item.get("allowCommercialUse")
        allowed = (isinstance(commercial, list) and "Image" in commercial) or commercial in ("Image", "Sell")
        published = str(version.get("publishedAt") or "")
        mid = item.get("id")
        return _entry(
            fam,
            base,
            source="civitai",
            mid=f"civitai-{mid}-{version.get('id')}-{main.get('file_id')}",
            name=f"{name} ({vname})" if vname else name,
            page=f"https://civitai.com/models/{mid}?modelVersionId={version.get('id')}",
            license_name="Creator's terms on Civitai"
            + (" (selling generated images allowed)" if allowed else " (no commercial use of images)"),
            license_url=f"https://civitai.com/models/{mid}",
            gated=False,
            uploader=str((item.get("creator") or {}).get("username") or "Civitai creator"),
            main=main,
            downloads=_monthly((version.get("stats") or {}).get("downloadCount"), published, now),
            updated=published,
            repo=name,
        )
    return None


def civitai_family(fam: Family, fetch: Fetcher, base: cat.ModelEntry, now: float | None = None) -> list[dict[str, Any]]:
    """Most downloaded checkpoints of the family's base model (plus an inpainting query for SD 1.5)."""
    if not fam.civitai_base:
        return []
    urls = [civitai_url(fam.civitai_base)]
    if fam.civitai_inpainting:
        urls.append(civitai_url(fam.civitai_base, query="inpainting"))
    out: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for url in urls:
        try:
            data = fetch.get(url)
        except HubError:
            continue
        for item in (data or {}).get("items") or []:
            if not isinstance(item, dict) or item.get("id") in seen:
                continue
            seen.add(item.get("id"))
            try:
                entry = civitai_candidate(fam, item, base, now)
            except (AttributeError, TypeError, ValueError, KeyError):
                continue
            if entry:
                out.append(entry)
    return out


# ------------------------------------------------------------------ search ----


def _prerank(raw: dict[str, Any], hw: hardware.Hardware | None, now: float) -> float:
    entry = type(
        "Stub",
        (),
        {
            "arch": raw["arch"],
            "min_vram_gb": raw["min_vram_gb"],
            "files": [cat.FileSpec(f["role"], "", f["dest"], f["size"]) for f in raw["files"]],
            "total_size": sum(f["size"] or 0 for f in raw["files"]),
        },
    )
    verdict = hardware.fit(entry, hw) if hw else hardware.FitVerdict(hardware.UNKNOWN)
    row = {
        "fit": verdict.level,
        "downloads": raw.get("downloads"),
        "updated": raw.get("updated"),
        "origin": raw["source"],
        "checksum_known": all(f.get("sha256") for f in raw["files"]),
    }
    return cat.rank_score(row, now)


def search(
    bundled: list[cat.ModelEntry],
    hw: hardware.Hardware | None = None,
    cache_path: str | Path | None = None,
    *,
    opener: Callable[..., Any] = urlopen,
    families: Iterable[str] | None = None,
    sources: Iterable[str] = ("huggingface", "civitai"),
    now: float | None = None,
    max_age: float = CACHE_MAX_AGE,
    record: Callable[[str, Any], None] | None = None,
    keep: int = KEEP_PER_FAMILY,
) -> HubResult:
    """Find live candidates for every family (see the module docstring).

    Returns raw catalog entries, validated with
    :func:`catalog.parse_live_entries` against ``bundled`` (invalid ones and
    duplicates of the verified list are dropped), best first within each family
    and source, at most ``keep`` per family and source. Never raises for network
    problems; they end up in ``problems`` and ``offline``.
    """
    clock = (lambda: now) if now is not None else time.time
    t_now = clock()
    cache = Cache(cache_path, max_age, clock)
    fetch = Fetcher(cache, opener, record=record, trim=_trim)
    sources = set(sources)
    result = HubResult(fetched_at=t_now)
    raws: list[dict[str, Any]] = []
    for key in families or FAMILY_SPECS:
        fam = FAMILY_SPECS.get(key)
        if fam is None:
            continue
        try:
            base = cat.by_id(bundled, fam.base_id)
        except cat.CatalogError:
            result.problems.append(f"{key}: the verified list has no {fam.base_id} entry to build on")
            continue
        for source, found in (
            (
                "huggingface",
                hf_family(fam, fetch, base, hw, stats=result.repo_stats) if "huggingface" in sources else [],
            ),
            ("civitai", civitai_family(fam, fetch, base, t_now) if "civitai" in sources else []),
        ):
            # one candidate per main file (mirrors share the SHA-256), best first
            found.sort(key=lambda r: -_prerank(r, hw, t_now))
            unique: list[dict[str, Any]] = []
            shas: set[str] = set()
            for r in found:
                sha = r["files"][0].get("sha256")
                if sha and sha in shas:
                    continue
                if sha:
                    shas.add(sha)
                unique.append(r)
            raws.extend(unique[:keep])
            result.counts[f"{key}/{source}"] = min(len(unique), keep)
    problems: list[str] = []
    valid_ids = {e.id for e in cat.parse_live_entries(raws, bundled, problems)}
    result.entries = [r for r in raws if r["id"] in valid_ids]
    for k in list(result.counts):
        fam_key, source = k.split("/")
        result.counts[k] = sum(1 for r in result.entries if r["family"] == fam_key and r["source"] == source)
    result.problems += problems
    if fetch.failures:
        hosts = sorted({f.split(":", 1)[0] for f in fetch.failures})
        result.problems.append(f"Could not reach {' and '.join(hosts)}: {fetch.failures[0].split(': ', 1)[-1]}")
    result.offline = bool(fetch.failures)
    result.from_cache = not fetch.network_used
    cache.save()
    return result


def cache_path_for(base: str | Path) -> Path:
    """``<profile>/openfill/hub-cache.json``."""
    return Path(base) / CACHE_FILENAME


def _main(argv: list[str]) -> int:
    """``python -m openfill.hub [--record DIR] [--out FILE] [--models DIR]``: run a real search and print it."""
    import argparse

    parser = argparse.ArgumentParser(description="Search Hugging Face and Civitai for models the plug-in can run")
    parser.add_argument("--record", default="", help="save every raw API response into this folder")
    parser.add_argument("--cache", default="", help="cache file (default: none, always fetch)")
    parser.add_argument("--models", default="", help="ComfyUI models folder (for the free disk space)")
    parser.add_argument("--comfy-url", default="", help="ComfyUI address for /system_stats when nvidia-smi is absent")
    parser.add_argument("--out", default="", help="write the mapped candidates as JSON to this file")
    args = parser.parse_args(argv)
    record_dir = Path(args.record) if args.record else None
    index: list[dict[str, Any]] = []
    lock = threading.Lock()  # details are fetched by several threads

    def record(url: str, data: Any) -> None:
        if record_dir is None:
            return
        with lock:
            record_dir.mkdir(parents=True, exist_ok=True)
            name = f"{len(index):03d}-{'civitai' if 'civitai.com' in url else 'hf'}.json"
            index.append({"file": name, "url": url, "fetched": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        (record_dir / name).write_text(json.dumps(data, indent=1, ensure_ascii=True) + "\n", encoding="utf-8")

    bundled = cat.load_catalog()
    hw = hardware.detect(args.models or None, args.comfy_url or None)
    started = time.monotonic()
    result = search(bundled, hw, args.cache or None, record=record)
    elapsed = time.monotonic() - started
    if record_dir is not None:
        (record_dir / "index.json").write_text(json.dumps(index, indent=1) + "\n", encoding="utf-8")
    entries = cat.parse_live_entries(result.entries, bundled)
    print(f"hardware: {hw.summary()}  ({hw.gpu_source or 'not detected'})")
    print(f"{len(entries)} candidates in {elapsed:.1f} s; offline={result.offline} from_cache={result.from_cache}")
    for p in result.problems:
        print("  problem:", p)
    rows = []
    for e in entries:
        verdict = hardware.fit(e, hw)
        row = cat.model_row(e, cat.InstallStatus(False), fit=verdict)
        row["score"] = round(cat.rank_score(row), 2)
        rows.append(row)
    for fam in cat.FAMILIES:
        fam_rows = sorted((r for r in rows if r["family"] == fam), key=lambda r: -r["score"])
        if not fam_rows:
            continue
        print(f"\n== {fam}: {len(fam_rows)}")
        for r in fam_rows:
            flags = ("gated " if r["gated"] else "") + ("" if r["checksum_known"] else "no-sha ")
            print(
                f"  {r['score']:5.2f} {r['origin'][:7]:7} {r['size']:>8} {r['fit']:11} {flags}{r['name']}"
                f"  [{r['license']}] dl/mo={r['downloads']} upd={r['updated']}"
            )
    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    "hardware": hw.to_json(),
                    "offline": result.offline,
                    "problems": result.problems,
                    "counts": result.counts,
                    "candidates": result.entries,
                    "rows": rows,
                },
                indent=1,
                ensure_ascii=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
