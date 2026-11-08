# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Models of another ComfyUI install, used read-only (issue #6, docs/design/shared-models.md).

The user names folders of another install (Krita AI Diffusion's managed server, or any
ComfyUI) in the ``shared_models`` setting. OpenFill then:

- finds the model folders ("roots") in each: :func:`model_roots`;
- counts the files there as installed (``catalog.FileSpec.root``, set by
  ``local_models.build``), so it does not download them again;
- starts its own ComfyUI with ``--extra-model-paths-config`` naming a yaml file it writes
  into its own profile folder (:func:`write_config`), so ComfyUI loads them too.

Nothing is ever written into the other install: no models, no nodes, no packages, no yaml.
The supervisor refuses any download or delete that lands in a shared root
(:func:`inside`). Pure standard library, no GIMP.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILENAME = "shared-model-paths.yaml"  # in OpenFill's profile folder, never in the other install
SECTION_PREFIX = "openfill_shared_"
KRITA = "Krita AI Diffusion"

# Model folders OpenFill can use, as ComfyUI names them (the yaml keys). ComfyUI maps the
# legacy names (clip, unet) to text_encoders and diffusion_models itself.
MODEL_FOLDERS = (
    "checkpoints",
    "diffusion_models",
    "unet",
    "text_encoders",
    "clip",
    "vae",
    "vae_approx",
    "inpaint",
    "loras",
    "background_removal",  # BiRefNet for Remove Background (PR #11)
)


@dataclass(frozen=True)
class Candidate:
    """Another install whose models OpenFill can use: the folder to put in the setting."""

    path: Path
    source: str  # plain words: "Krita AI Diffusion", "ComfyUI folder", ...


# ------------------------------------------------------------------- setting --


def _split_typed(text: str) -> list[str]:
    """Folders typed on one line, separated by the system's list separator (``;`` on Windows,
    ``:`` elsewhere). A folder whose own name holds the separator is kept whole when it exists:
    the longest run of pieces that is an existing folder wins."""
    tokens = text.split(os.pathsep)
    out, i = [], 0
    while i < len(tokens):
        take = i + 1
        for j in range(len(tokens), i + 1, -1):
            if Path(os.path.expanduser(os.pathsep.join(tokens[i:j]).strip().strip('"'))).is_dir():
                take = j
                break
        out.append(os.pathsep.join(tokens[i:take]))
        i = take
    return out


def parse(text: str | None) -> list[str]:
    """The folders of the ``shared_models`` setting, blanks and repeats dropped. The setting
    keeps one folder per line (:func:`join`); a single typed line is split on the system's list
    separator, keeping existing folders whole (:func:`_split_typed`)."""
    text = text or ""
    pieces = text.split("\n") if "\n" in text else _split_typed(text)
    out: list[str] = []
    for part in pieces:
        part = part.strip().strip('"')
        if part and part not in out:
            out.append(part)
    return out


def join(folders: Iterable[str]) -> str:
    """The setting's text for ``folders``: one per line (never split again, whatever is in a
    folder's name). Empty for none."""
    out: list[str] = []
    for f in folders:
        f = str(f).strip()
        if f and f not in out:
            out.append(f)
    return "".join(f + "\n" for f in out)


def display(text: str | None) -> str:
    """The setting on one line, for a one-line text field (folders separated by ``;`` on
    Windows, ``:`` elsewhere); :func:`parse` reads it back."""
    return os.pathsep.join(parse(text))


def _utf8(path: Path | str) -> bool:
    """True when ``path`` can be written into the yaml: ComfyUI reads it as strict UTF-8, and a
    Linux folder name that is not UTF-8 (an old Latin-1 archive) cannot be."""
    try:
        str(path).encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def _lexically_inside(path: Path | str, folder: Path | str) -> bool:
    """``path`` is ``folder`` or below it, as written (no links followed)."""
    a = os.path.normcase(os.path.normpath(os.path.abspath(str(path))))
    b = os.path.normcase(os.path.normpath(os.path.abspath(str(folder))))
    try:
        return os.path.commonpath([a, b]) == b
    except ValueError:  # other drives
        return False


def _own_alias(folder: str, root: Path, own: Path) -> bool:
    """``root`` (a models root of the other install ``folder``) is OpenFill's own models folder
    by identity AND is the other install's own folder: OpenFill's folder is a link, junction
    or bind mount to it. A root that is OpenFill's folder because the other ComfyUI's
    ``extra_model_paths.yaml`` points at it (sharing the other way round) is not an alias:
    those files are OpenFill's."""
    return _same(root, own) and _lexically_inside(root, folder)


def check(text: str | None, own_models_root: Path | str | None = None) -> tuple[list[str], str]:
    """(the folders, written out in full, "") or ([], the problem in plain words) for what the
    user typed: every folder must be a full path to an existing folder (``~`` is expanded)
    whose name ComfyUI can read, and OpenFill's own models folder must not be a link to one of
    its model folders. A root that already is OpenFill's own folder (the other ComfyUI reads
    OpenFill's models) is fine: it is left out (:func:`roots_for`)."""
    out = []
    for raw in parse(text):
        folder = os.path.expanduser(raw)
        if not os.path.isabs(folder):
            return [], f"Use the full path of the folder, not {raw!r}."
        if not os.path.isdir(folder):
            return [], f"Folder not found: {raw}"
        if not _utf8(folder):
            return [], f"The name of the folder {raw!r} is not UTF-8, which ComfyUI cannot read. Rename it first."
        folder = os.path.normpath(folder)
        if own_models_root is not None:
            own = Path(own_models_root)
            for root in model_roots(folder) or [Path(folder)]:
                if _own_alias(folder, root, own):
                    return [], (
                        f"OpenFill's own models folder is the same folder as {root} (a link to it). "
                        "OpenFill would write into the other ComfyUI there: remove that link first."
                    )
        out.append(folder)
    return out, ""


def _same(a: Path | str, b: Path | str) -> bool:
    """The same folder, by identity (device and inode: links, junctions, bind mounts and a
    different letter case on a case-insensitive disk all count); by path when one is missing."""
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def _ancestors(path: Path | str):
    """``path`` and its parents that exist, as written and with links resolved (a file that
    is not there yet, e.g. a download, is judged by the folder it would land in)."""
    seen = set()
    for start in (os.path.abspath(str(path)), os.path.realpath(str(path))):
        q = start
        while True:
            if q not in seen and os.path.exists(q):
                seen.add(q)
                yield q
            parent = os.path.dirname(q)
            if parent == q:
                break
            q = parent


def _within(path: Path | str, root: Path | str) -> bool:
    """``path`` is ``root`` or below it, by identity of ``path``'s folders."""
    try:
        target = os.stat(root)
    except OSError:
        return False
    for q in _ancestors(path):
        try:
            st = os.stat(q)
        except OSError:
            continue
        if (st.st_dev, st.st_ino) == (target.st_dev, target.st_ino):
            return True
    return False


def inside(path: Path | str, roots: Iterable[Path | str]) -> bool:
    """True when ``path`` is one of ``roots`` or below one, by folder identity (links,
    junctions and bind mounts count)."""
    return any(_within(path, root) for root in roots)


def blocked(path: Path | str, guard: Iterable[Path | str], own_models_root: Path | str | None) -> bool:
    """True when OpenFill must not write or delete ``path``: it is (by folder identity) in a
    shared root of :func:`guard_roots`. OpenFill's own models folder is never blocked when a
    shared folder merely contains it (a parent such as ``C:\\AI`` holding both installs); it
    is when it IS a shared root (a link or bind mount from OpenFill's folder to the other's)."""
    own = own_models_root if own_models_root is not None and os.path.isdir(own_models_root) else None
    for root in guard:
        if not _within(path, root):
            continue
        if own is not None and not _same(own, root) and _within(own, root) and _within(path, own):
            continue  # OpenFill's own folder below a shared parent: only the rest is shared
        return True
    return False


# --------------------------------------------------------------------- roots --


def _is_krita_server(folder: Path) -> bool:
    return (folder / "ComfyUI" / "main.py").is_file() and (folder / "models").is_dir()


def _comfy_models(folder: Path) -> Path | None:
    """The ``models`` folder of a ComfyUI checkout or portable build at ``folder``."""
    for comfy in (folder / "ComfyUI", folder):
        if (comfy / "main.py").is_file() and (comfy / "models").is_dir():
            return comfy / "models"
    return None


def _yaml_base_paths(yaml_path: Path) -> list[Path]:
    """The ``base_path`` of each section of a ComfyUI ``extra_model_paths.yaml``, read only.

    Only that one key is read (OpenFill is standard library only: no YAML parser). A base
    path is taken as a models root when it is a folder; sections without one are skipped.
    """
    try:
        text = yaml_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s+base_path\s*:\s*(.+?)\s*$", line)
        if not m or line.lstrip().startswith("#"):
            continue
        value = m.group(1)
        if value[:1] in "\"'" and value[-1:] == value[:1]:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()
        if not value:
            continue
        p = Path(os.path.expanduser(value))
        if not p.is_absolute():
            p = yaml_path.parent / p
        out.append(p)
    return out


def model_roots(folder: Path | str) -> list[Path]:
    """The model folders ("roots": folders holding ``checkpoints``, ``vae`` ...) of ``folder``.

    - Krita AI Diffusion's server folder (``ComfyUI/main.py`` and ``models`` in it):
      ``models``, then ``ComfyUI/models`` (older Krita versions kept models there).
    - A ComfyUI folder (checkout or portable): its ``models``, then the base paths of its
      own ``extra_model_paths.yaml`` that are folders.
    - Anything else: the folder itself, taken as a models folder.

    Folders that do not exist are left out; never raises.
    """
    folder = Path(folder)
    out: list[Path] = []
    try:
        if not folder.is_dir():
            return []
        if _is_krita_server(folder):
            out = [folder / "models", folder / "ComfyUI" / "models"]
        elif (models := _comfy_models(folder)) is not None:
            out = [models, *_yaml_base_paths(models.parent / "extra_model_paths.yaml")]
        elif (folder / "server").is_dir() and _is_krita_server(folder / "server"):
            out = [folder / "server" / "models", folder / "server" / "ComfyUI" / "models"]  # ai_diffusion
        else:
            out = [folder]
    except OSError:
        return []
    unique: list[Path] = []
    for p in out:
        try:
            if p.is_dir() and not any(_same(p, q) for q in unique):
                unique.append(p)
        except OSError:
            continue
    return unique


def guard_roots(setting: str | None, own_models_root: Path | str | None = None) -> list[Path]:
    """Every root of the folders in the setting: the places the supervisor never writes to
    (:func:`blocked`). OpenFill's own models folder is one of them when a link or mount makes
    it the other install's folder; a root that is OpenFill's folder because the other ComfyUI
    reads OpenFill's models is left out (those files are OpenFill's)."""
    out: list[Path] = []
    for raw in parse(setting):
        folder = os.path.expanduser(raw)
        for root in model_roots(folder) or [Path(folder)]:
            if (
                own_models_root is not None
                and _same(root, own_models_root)
                and not _own_alias(folder, root, Path(own_models_root))
            ):
                continue
            if not any(_same(root, r) for r in out):
                out.append(root)
    return out


def roots_for(setting: str | None, own_models_root: Path | str | None) -> list[Path]:
    """All roots of the folders in the setting, in order, without OpenFill's own models
    folder (that one is read and written as before)."""
    out: list[Path] = []
    for folder in parse(setting):
        for root in model_roots(os.path.expanduser(folder)):
            if own_models_root is not None and _same(root, Path(own_models_root)):
                continue
            if not any(_same(root, r) for r in out):
                out.append(root)
    return out


# ---------------------------------------------------------------- detection --


def krita_data_dirs(env: Mapping[str, str], home: Path, platform: str) -> list[Path]:
    """Where Krita AI Diffusion keeps its user data (settings.json, the server by default)."""
    out: list[Path] = []
    if platform == "win32":
        appdata = Path(env["APPDATA"]) if env.get("APPDATA") else home / "AppData" / "Roaming"
        local = Path(env["LOCALAPPDATA"]) if env.get("LOCALAPPDATA") else home / "AppData" / "Local"
        out += [appdata / "krita" / "ai_diffusion", local / "krita-ai-diffusion", appdata / "krita-ai-diffusion"]
    elif platform == "darwin":
        support = home / "Library" / "Application Support"
        out += [support / "krita" / "ai_diffusion", support / "krita-ai-diffusion"]
    else:
        data = Path(env["XDG_DATA_HOME"]) if env.get("XDG_DATA_HOME") else home / ".local" / "share"
        flatpak = home / ".var" / "app" / "org.kde.krita" / "data"
        out += [data / "krita" / "ai_diffusion", flatpak / "krita" / "ai_diffusion", data / "krita-ai-diffusion"]
    return out


def _read_json_with_comments(path: Path) -> dict:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads("\n".join(line for line in lines if not line.lstrip().startswith("//")))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def detect_krita(
    env: Mapping[str, str] | None = None, home: Path | None = None, platform: str | None = None
) -> list[Candidate]:
    """Krita AI Diffusion's managed ComfyUI servers on this computer (its ``settings.json``
    ``server_path``, else the default ``<user data>/server``). Never raises."""
    env = os.environ if env is None else env
    home = home or Path.home()
    platform = platform or sys.platform
    found: list[Candidate] = []
    for data in krita_data_dirs(env, home, platform):
        try:
            settings = _read_json_with_comments(data / "settings.json")
            servers = []
            server_path = settings.get("server_path")
            if isinstance(server_path, str) and server_path and Path(server_path).is_absolute():
                servers.append(Path(server_path))
            servers.append(data / "server")
            for server in servers:
                if _is_krita_server(server) and not any(_same(server, c.path) for c in found):
                    found.append(Candidate(server, KRITA))
        except OSError:
            continue
    return found


def detect(
    own_comfy_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    platform: str | None = None,
    comfy_candidates: Iterable[Candidate] | None = None,
) -> list[Candidate]:
    """Other installs to offer: Krita AI Diffusion first, then the ComfyUI folders
    ``comfy_setup.detect_comfy`` finds, except OpenFill's own (``own_comfy_dir``)."""
    out = detect_krita(env, home, platform)
    if comfy_candidates is None:
        from .comfy_setup import detect_comfy

        comfy_candidates = [Candidate(c.path, c.source) for c in detect_comfy(env, home)]
    for c in comfy_candidates:
        if own_comfy_dir and _same(c.path, Path(own_comfy_dir)):
            continue
        if any(_same(c.path, o.path) or _same(c.path, o.path / "ComfyUI") for o in out):
            continue
        out.append(Candidate(Path(c.path), c.source))
    return out


# ---------------------------------------------------------------------- yaml --


# Folders a custom node pack registers when it loads, after ComfyUI read the yaml:
# comfyui-inpaint-nodes keeps the list it finds and appends ComfyUI's own models/inpaint after
# it, which would put a shared folder first. Listing OpenFill's own folder first in the yaml
# keeps the order ComfyUI uses for its built-in folders (its own models, then the extra ones).
NODE_FOLDERS = ("inpaint",)


def yaml_text(roots: Iterable[Path], own_models_root: Path | str | None = None) -> str:
    """ComfyUI ``extra_model_paths.yaml`` text for ``roots``: one section per root, absolute
    paths written as JSON strings (valid YAML: backslashes, ``#``, ``:`` and spaces are safe),
    no ``base_path`` (ComfyUI expands ``$VARS`` in that one only), only folders that exist.
    With ``own_models_root`` (the ``models`` folder of OpenFill's ComfyUI), an ``openfill_own``
    section first lists the :data:`NODE_FOLDERS` there, so OpenFill's own files come first."""
    sections = []
    for index, root in enumerate(roots, 1):
        entries = []
        for name in MODEL_FOLDERS:
            folder = Path(root) / name
            if folder.is_dir() and _utf8(folder.resolve()):
                entries.append((name, str(folder.resolve())))
        if entries:
            sections.append((f"{SECTION_PREFIX}{index}", entries))
    lines = ["# Written by OpenFill: models of other ComfyUI installs, read only.", ""]
    used = {name for _head, entries in sections for name, _path in entries}
    own = [n for n in NODE_FOLDERS if n in used]
    if own_models_root is not None and own:
        sections.insert(0, ("openfill_own", [(n, str(Path(own_models_root).resolve() / n)) for n in own]))
    for head, entries in sections:
        lines.append(f"{head}:")
        lines += [f"  {name}: {json.dumps(path, ensure_ascii=False)}" for name, path in entries]
        lines.append("")
    return "\n".join(lines)


def config_path(base: Path | str) -> Path:
    """``<profile>/openfill/shared-model-paths.yaml``."""
    return Path(base) / CONFIG_FILENAME


def write_config(base: Path | str, roots: Iterable[Path], own_models_root: Path | str | None = None) -> Path | None:
    """Write the yaml for ``roots`` into OpenFill's profile folder ``base`` and return it. With
    no root holding a model folder it returns None; a file already there (a ComfyUI you start
    yourself may name it on its command line) is kept as an empty mapping, ``{}``, which
    ComfyUI reads as "no extra folders" (a missing or empty file would stop it starting)."""
    path = config_path(base)
    text = yaml_text(list(roots), own_models_root)
    if SECTION_PREFIX not in text:
        if path.exists():
            tmp = path.with_suffix(".tmp")
            tmp.write_text("# Written by OpenFill: no other ComfyUI folders.\n{}\n", encoding="utf-8")
            os.replace(tmp, path)
        return None
    Path(base).mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path
