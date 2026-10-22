# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Resumable, verified model downloads and custom-node installation.

Pure Python (urllib + subprocess). Used by the supervisor's task worker and by
unit tests; nothing here touches GIMP.
"""

from __future__ import annotations

import errno
import hashlib
import http.client
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

CHUNK = 1024 * 1024
USER_AGENT = "gimp-openfill/2.0 (+https://github.com/thndrbrrr/gimp-stable-boy)"

# The Hugging Face token is only ever sent to these hosts (and their subdomains),
# only over https, and never forwarded on a redirect (the CDN URLs are pre-signed).
HF_TOKEN_HOSTS: tuple[str, ...] = ("huggingface.co", "hf.co")

# Custom node repositories must be plain GitHub https URLs pinned to a full commit.
_REPO_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?(\.git)?/?$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]*$")
_CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")

ProgressFn = Callable[[int, "int | None", float], None]  # done, total, bytes/sec
CancelFn = Callable[[], bool]


class DownloadError(RuntimeError):
    """``code`` is one of: auth, notfound, disk, hash, network, cancelled, size."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class DownloadResult:
    """Outcome of :func:`download_file`."""

    path: Path
    bytes_written: int
    sha256: str | None
    resumed_from: int
    skipped: bool = False


# ------------------------------------------------------------- validation ----


def is_safe_name(name: str) -> bool:
    """True for a plain file or folder name: no separators, no ``..``, no drive."""
    return isinstance(name, str) and bool(_NAME_RE.match(name)) and name not in (".", "..")


def safe_relative_path(rel: str) -> str:
    """Validate a catalog ``dest`` (``subfolder/file``) and return it with ``/`` separators.

    Rejects absolute paths, drive letters, ``..`` and empty segments, and
    requires every segment to be a plain name. Raises ``ValueError``.
    """
    if not isinstance(rel, str) or not rel:
        raise ValueError("empty path")
    norm = rel.replace("\\", "/")
    if norm.startswith("/") or ":" in norm:
        raise ValueError(f"absolute path not allowed: {rel!r}")
    segments = norm.split("/")
    if any(not is_safe_name(seg) for seg in segments):
        raise ValueError(f"unsafe path: {rel!r}")
    return "/".join(PurePosixPath(norm).parts)


def safe_join(root: Path, rel: str) -> Path:
    """``root / rel`` after :func:`safe_relative_path`, verified to stay inside ``root``."""
    root = Path(root)
    path = root.joinpath(*safe_relative_path(rel).split("/"))
    resolved_root, resolved = root.resolve(), path.resolve()
    if resolved_root not in resolved.parents:
        raise ValueError(f"path escapes {root}: {rel!r}")
    return path


def validate_node_spec(repo: str, folder: str, commit: str) -> None:
    """Raise ``ValueError`` unless the custom node source is a GitHub https URL,
    the folder a plain name and the commit a full 40-character SHA-1."""
    if not isinstance(repo, str) or not _REPO_RE.match(repo):
        raise ValueError(f"custom node repo must be https://github.com/<owner>/<name>: {repo!r}")
    if not is_safe_name(folder):
        raise ValueError(f"custom node folder must be a plain name: {folder!r}")
    if not isinstance(commit, str) or not _COMMIT_RE.match(commit):
        raise ValueError(f"custom node commit must be a full 40-character hex SHA: {commit!r}")


def token_allowed(url: str, hosts: Iterable[str] = HF_TOKEN_HOSTS) -> bool:
    """True when a bearer token may be sent to ``url`` (https and an allowed host)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return False
    return any(host == h or host.endswith("." + h) for h in hosts)


# -------------------------------------------------------------- download ----


def _human(n: int) -> str:
    """``1536`` -> ``"1.5 KB"`` (decimal units, like the Models tab)."""
    value = float(n)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} GB"


def network_message(url: str, err: BaseException) -> str:
    """Plain words for a connection failure, with the next step."""
    reason = getattr(err, "reason", err)
    text = str(reason)
    if isinstance(reason, socket.gaierror) or "getaddrinfo" in text:
        why = "the address could not be looked up (no internet connection?)"
    elif isinstance(reason, ConnectionRefusedError) or "10061" in text or "refused" in text.lower():
        why = "the connection was refused (a proxy or firewall may be blocking it)"
    elif isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in text.lower():
        why = "the connection timed out"
    else:
        why = text or type(err).__name__
    host = urlparse(url).hostname or url
    return (
        f"Could not reach {host}: {why}. Check the internet connection and any proxy or firewall, "
        "then press Download again (a partly downloaded file is kept and resumes)."
    )


def auth_message(url: str, code: int) -> str:
    """Plain words for a 401/403, worded for the host that answered."""
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("civitai.com"):
        return (
            f"HTTP {code}: Civitai only allows this download when signed in (or the model is early access). "
            "Pick another model, or download it from its Civitai page into the models folder yourself."
        )
    return (
        f"HTTP {code}: this file is gated. Accept the license on the model page "
        "and enter a Hugging Face access token in the settings."
    )


def _in_use_message(path: Path, err: BaseException) -> str:
    return (
        f"{path.name} cannot be changed ({err.__class__.__name__}); another program, often the AI backend, "
        "may be using it. Stop the AI backend, then press Download again."
    )


def plain_os_message(err: BaseException) -> str:
    """Plain words for an error that escaped a download (file in use, dropped connection)."""
    if isinstance(err, PermissionError):
        return (
            f"A file could not be changed ({err.filename or 'permission denied'}); another program, often the AI "
            "backend, may be using it. Stop the AI backend, then press Download again."
        )
    return "The connection dropped during the download. Press Download again; the part already downloaded is kept."


def part_path(dest: Path) -> Path:
    """The temporary ``<name>.part`` file a download is written to before the rename."""
    return dest.with_name(dest.name + ".part")


def _sha256_of(path: Path, cancel: CancelFn | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK * 4)
            if not block:
                break
            h.update(block)
            if cancel and cancel():
                raise DownloadError("cancelled", "cancelled during verification")
    return h.hexdigest()


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _open(
    url: str,
    headers: dict[str, str],
    opener: Callable[..., Any] = urlopen,
    timeout: float = 60.0,
    authorization: str | None = None,
) -> Any:
    req = Request(url, headers=headers, method="GET")
    if authorization:
        # Unredirected: urllib must not forward the token to the redirect target.
        req.add_unredirected_header("Authorization", authorization)
    return opener(req, timeout=timeout)


def download_file(
    url: str,
    dest: Path,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    token: str | None = None,
    progress: ProgressFn | None = None,
    cancel: CancelFn | None = None,
    opener: Callable[..., Any] = urlopen,
    min_free_bytes: int = 256 * 1024 * 1024,
    token_hosts: Iterable[str] = HF_TOKEN_HOSTS,
    _fresh_retry: bool = False,
    keep_existing: bool = False,
) -> DownloadResult:
    """Download ``url`` to ``dest`` with resume (HTTP Range), verification and
    atomic rename. Raises :class:`DownloadError` with a user-facing ``code``.

    ``token`` is sent as ``Authorization: Bearer`` only when ``url`` is https on
    one of ``token_hosts``; it is never put in the URL and never forwarded on a
    redirect. Data goes to ``<dest>.part`` first; the size and the SHA-256 (when
    known) are checked before the ``.part`` file is renamed onto ``dest``.

    A file already at ``dest`` that does not match is deleted and fetched again,
    unless ``keep_existing`` is set (live hub entries): then it may be the user's
    own file, so it is left alone and the download stops with an explanation.
    """
    dest = Path(dest)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_file():
            size_ok = expected_size is None or dest.stat().st_size == expected_size
            if size_ok and (expected_sha256 is None or _sha256_of(dest, cancel) == expected_sha256):
                return DownloadResult(dest, 0, expected_sha256, 0, skipped=True)
            if keep_existing:
                raise DownloadError(
                    "exists",
                    f"{dest.name} is already in {dest.parent} but it is not the file this model lists, so it was "
                    "left alone. Move or delete it yourself, then press Download again.",
                )
            dest.unlink()  # wrong size or hash: start over
    except PermissionError as err:
        raise DownloadError("disk", _in_use_message(dest, err)) from err

    part = part_path(dest)
    offset = part.stat().st_size if part.is_file() else 0
    if expected_size and offset > expected_size:
        part.unlink()
        offset = 0

    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    authorization = f"Bearer {token}" if token and token_allowed(url, token_hosts) else None
    if offset:
        headers["Range"] = f"bytes={offset}-"

    try:
        resp = _open(url, headers, opener, authorization=authorization)
    except HTTPError as err:
        err.close()  # release the connection; only the status code is used
        if err.code in (401, 403):
            raise DownloadError("auth", auth_message(url, err.code)) from err
        if err.code == 404:
            raise DownloadError("notfound", f"HTTP 404: the file is no longer at {url}") from err
        if err.code == 416 and offset:
            # The partial file is already complete (or the server disagrees); verify below.
            resp = None
        elif err.code == 429 or err.code >= 500:
            host = urlparse(url).hostname or url
            raise DownloadError(
                "network",
                f"{host} is busy or limiting downloads right now (HTTP {err.code}). Wait a few minutes, "
                "then press Download again (a partly downloaded file is kept and resumes).",
            ) from err
        else:
            raise DownloadError("network", f"HTTP {err.code} while fetching {url}") from err
    except (URLError, OSError, http.client.HTTPException) as err:
        raise DownloadError("network", network_message(url, err)) from err
    if resp is not None and url.startswith("https://"):
        final = str(getattr(resp, "url", None) or (resp.geturl() if hasattr(resp, "geturl") else "") or url)
        if not final.startswith("https://"):
            resp.close()
            raise DownloadError(
                "network", "the server redirected the download to an unencrypted address, so it was refused."
            )

    written = 0
    resumed_from = offset
    if resp is not None:
        with resp:
            status = getattr(resp, "status", None) or resp.getcode()
            length = resp.headers.get("Content-Length")
            total: int | None = None
            if status == 206 and offset:
                match = _CONTENT_RANGE_RE.match(resp.headers.get("Content-Range") or "")
                if match and int(match.group(1)) != offset:
                    _unlink_quietly(part)
                    raise DownloadError(
                        "network",
                        f"server resumed at byte {match.group(1)}, expected {offset}; "
                        "the partial file was deleted, retry to start over",
                    )
                mode = "ab"
                total = offset + int(length) if length else expected_size
            else:
                mode = "wb"
                resumed_from = 0
                offset = 0
                total = int(length) if length else expected_size
            if expected_size and total and total != expected_size:
                raise DownloadError("size", f"server reports {total} bytes, catalog expects {expected_size}")
            if total:
                try:
                    free: int | None = shutil.disk_usage(dest.parent).free
                except OSError:
                    free = None
                if free is not None and free < (total - offset) + min_free_bytes:
                    raise DownloadError(
                        "disk",
                        f"not enough disk space on {dest.parent.anchor or dest.parent}: the download needs "
                        f"{_human(total - offset + min_free_bytes)} (with a safety margin), {_human(free)} is free. "
                        "Free up space on that drive, then press Download again.",
                    )
            limit = expected_size or total
            started = time.monotonic()
            last_report = 0.0
            try:
                with open(part, mode) as fh:
                    while True:
                        if cancel and cancel():
                            raise DownloadError("cancelled", "download cancelled")
                        block = resp.read(CHUNK)
                        if not block:
                            break
                        if limit and offset + written + len(block) > limit:
                            raise DownloadError("size", f"server sent more than the expected {limit} bytes")
                        fh.write(block)
                        written += len(block)
                        now = time.monotonic()
                        if progress and (now - last_report >= 0.5 or (total and offset + written >= total)):
                            last_report = now
                            speed = written / max(1e-6, now - started)
                            progress(offset + written, total, speed)
            except DownloadError as err:
                if err.code == "size":
                    _unlink_quietly(part)  # oversized data is never resumable
                raise
            except http.client.HTTPException as err:
                raise DownloadError(
                    "network",
                    "The connection dropped during the download. Press Download again; the part already "
                    "downloaded is kept and resumes.",
                ) from err
            except PermissionError as err:
                raise DownloadError("disk", _in_use_message(part, err)) from err
            except OSError as err:
                if err.errno in (errno.ENOSPC, 28) or "No space" in str(err):
                    raise DownloadError(
                        "disk",
                        f"the disk filled up during the download ({dest.parent.anchor or dest.parent}). Free up "
                        "space on that drive, then press Download again; the part already downloaded is kept.",
                    ) from err
                raise DownloadError("network", f"read/write error: {err}") from err

    final_size = part.stat().st_size if part.is_file() else 0
    if expected_size and final_size != expected_size:
        raise DownloadError(
            "size", f"downloaded {final_size} bytes, expected {expected_size}; the partial file is kept for resume"
        )
    digest = None
    if expected_sha256:
        digest = _sha256_of(part, cancel)
        if digest != expected_sha256:
            part.unlink()
            if resumed_from and not _fresh_retry:
                # the bytes kept from an earlier attempt were bad (a corrupt .part): fetch once from zero
                return download_file(
                    url,
                    dest,
                    expected_size,
                    expected_sha256,
                    token,
                    progress,
                    cancel,
                    opener,
                    min_free_bytes,
                    token_hosts,
                    _fresh_retry=True,
                    keep_existing=keep_existing,
                )
            raise DownloadError(
                "hash",
                f"checksum mismatch for {dest.name}: got {digest[:12]}..., "
                f"expected {expected_sha256[:12]}...; the file was deleted",
            )
    elif part.is_file():
        digest = _sha256_of(part, cancel)
    os.replace(part, dest)
    return DownloadResult(dest, written, digest, resumed_from)


# ---------------------------------------------------------- custom nodes ----


def _is_file(p: Path) -> bool:
    """``p.is_file()``, but False for a PATH entry Windows refuses to read (WinError 448 and the like)."""
    try:
        return p.is_file()
    except OSError:
        return False


def git_executable() -> str:
    """Path of ``git`` (PATH first, then the default Git for Windows locations)."""
    for name in ("git", "git.exe"):
        for d in os.environ.get("PATH", "").split(os.pathsep):
            p = Path(d) / name
            if _is_file(p):
                return str(p)
    for p in (r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files\Git\bin\git.exe"):
        if _is_file(Path(p)):
            return p
    return "git"


NODE_MARKER = ".openfill-commit"


def _safe_zip_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Members of ``zf`` after refusing absolute paths, ``..``, drive letters and symlinks."""
    out = []
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        parts = PurePosixPath(name).parts
        if name.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
            raise RuntimeError(f"the archive holds an unsafe path: {info.filename!r}")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise RuntimeError(f"the archive holds a symbolic link: {info.filename!r}")
        out.append(info)
    return out


def install_node_from_zip(
    repo: str,
    folder: str,
    commit: str,
    custom_nodes_dir: Path,
    cancel: CancelFn | None = None,
    opener: Callable[..., Any] = urlopen,
) -> Path:
    """Install the GitHub zip archive of ``commit`` as ``custom_nodes_dir/folder`` (no git needed).

    The archive of a commit carries that commit's id as its zip comment, which
    is checked against the pin before anything is unpacked.
    """
    validate_node_spec(repo, folder, commit)
    base = repo.rstrip("/")
    base = base[:-4] if base.endswith(".git") else base
    nodes = Path(custom_nodes_dir)
    nodes.mkdir(parents=True, exist_ok=True)
    zip_path = nodes / f".{folder}.zip"
    work = nodes / f".{folder}.unpack"
    shutil.rmtree(work, ignore_errors=True)
    try:
        download_file(f"{base}/archive/{commit}.zip", zip_path, cancel=cancel, opener=opener)
        with zipfile.ZipFile(zip_path) as zf:
            comment = zf.comment.decode("ascii", "replace").strip()
            if comment != commit:
                raise RuntimeError(f"the archive for {folder} is for commit {comment or 'unknown'}, expected {commit}")
            members = _safe_zip_members(zf)
            tops = {PurePosixPath(m.filename).parts[0] for m in members if m.filename.strip("/")}
            if len(tops) != 1:
                raise RuntimeError(f"the archive for {folder} does not have a single top folder")
            zf.extractall(work, members)
        shutil.move(str(work / tops.pop()), str(nodes / folder))
        (nodes / folder / NODE_MARKER).write_text(commit + "\n", encoding="utf-8")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        _unlink_quietly(zip_path)
    return nodes / folder


def install_custom_node(
    repo: str,
    folder: str,
    commit: str,
    custom_nodes_dir: Path,
    python: Path,
    log: Callable[[str], None] | None = None,
    runner: Callable[..., Any] = subprocess.run,
    cancel: CancelFn | None = None,
    opener: Callable[..., Any] = urlopen,
) -> Path:
    """Install ``repo`` at ``commit`` into ``custom_nodes_dir/folder`` and its
    requirements with ``python``. Idempotent.

    A new install uses the GitHub zip archive of the commit (no git needed). An
    existing git clone is reset to the pinned commit (needs git). Every command
    is an argument list (never a shell); ``repo``, ``folder`` and ``commit`` are
    checked with :func:`validate_node_spec` first.
    """
    validate_node_spec(repo, folder, commit)
    log = log or (lambda _msg: None)
    target = Path(custom_nodes_dir) / folder
    kw: dict[str, Any] = {"capture_output": True, "text": True}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    marker = target / NODE_MARKER
    if (target / ".git").is_dir():
        git = git_executable()
        log(f"checking out {commit}")
        runner([git, "-C", str(target), "fetch", "--quiet", "origin"], **kw)  # best effort; offline is fine
        r = runner([git, "-C", str(target), "checkout", "--quiet", commit], **kw)
        if r.returncode != 0:
            raise RuntimeError(f"git checkout {commit} failed: {r.stderr.strip() or r.stdout.strip()}")
        r = runner([git, "-C", str(target), "rev-parse", "HEAD"], **kw)
        head = (r.stdout or "").strip()
        if r.returncode != 0 or head != commit:
            raise RuntimeError(f"{folder} is at {head or 'an unknown commit'}, expected the pinned {commit}")
    elif marker.is_file() and marker.read_text(encoding="utf-8").strip() == commit:
        log(f"{folder} is already at {commit}")
    elif target.exists():
        raise RuntimeError(
            f"{target} already exists but was not installed by OpenFill at the pinned commit. "
            "Move or delete that folder, then try again."
        )
    else:
        log(f"downloading {repo} @ {commit[:10]}")
        install_node_from_zip(repo, folder, commit, Path(custom_nodes_dir), cancel, opener)
    req = target / "requirements.txt"
    if req.is_file():
        log(f"installing requirements with {python}")
        r = runner([*pip_install_command(python, log, runner), "-r", str(req)], **kw)
        if r.returncode != 0:
            raise RuntimeError(f"pip install failed: {(r.stderr or r.stdout).strip()[-800:]}")
    return target


def _quiet_kw() -> dict[str, Any]:
    kw: dict[str, Any] = {"capture_output": True, "text": True}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kw


def pip_install_command(
    python: str | Path, log: Callable[[str], None] | None = None, runner: Callable[..., Any] = subprocess.run
) -> list[str]:
    """``python -m pip install`` as an argument list, after making sure ``python`` has pip.

    A venv made with ``uv venv`` (or ``python -m venv --without-pip``) has no pip;
    ``ensurepip`` installs the copy that ships with Python, offline.
    """
    log = log or (lambda _msg: None)
    kw = _quiet_kw()
    if runner([str(python), "-s", "-m", "pip", "--version"], **kw).returncode != 0:
        log(f"{python} has no pip; installing it with ensurepip")
        r = runner([str(python), "-s", "-m", "ensurepip", "--upgrade"], **kw)
        if r.returncode != 0:
            raise RuntimeError(
                f"{python} has no pip, and ensurepip could not add it: {(r.stderr or r.stdout or '').strip()[-400:]}"
            )
    return [str(python), "-s", "-m", "pip", "install", "--no-warn-script-location"]


def has_module(python: str | Path, module: str, runner: Callable[..., Any] = subprocess.run) -> bool:
    """``python`` can import ``module`` (a plain dotted name)."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", module):
        raise ValueError(f"not a module name: {module!r}")
    try:
        return runner([str(python), "-s", "-c", f"import {module}"], timeout=120, **_quiet_kw()).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def install_pip_packages(
    python: str | Path,
    packages: Iterable[str],
    log: Callable[[str], None] | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    """Install the pinned ``packages`` into ``python``'s environment; raises RuntimeError on failure."""
    packages = list(packages)
    if not packages:
        return
    log = log or (lambda _msg: None)
    log(f"installing {', '.join(packages)} with {python}")
    r = runner([*pip_install_command(python, log, runner), *packages], **_quiet_kw())
    if r.returncode != 0:
        raise RuntimeError(f"pip install failed: {(r.stderr or r.stdout or '').strip()[-800:]}")
