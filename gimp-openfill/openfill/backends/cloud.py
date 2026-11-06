# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Cloud backend: OpenRouter, or any service that speaks OpenAI's images API
(OpenAI itself, a LiteLLM proxy, ...). Optional: only used when the user picks it.

Nothing here runs unless ``Settings.backend`` is "cloud", and nothing here ever
starts or calls ComfyUI: a failure is reported, never retried on another backend.

How each operation maps (docs/cloud.md has the details and the limits):

- OpenRouter (``POST /images``, or ``/chat/completions`` with ``modalities``): no
  model there takes a real mask, so the picture and a black and white mask are
  both sent as input images with an instruction to change only the white area.
- OpenAI-compatible (``POST /images/edits``): the mask is sent as the API's own
  mask (transparent = change here). OpenAI says its GPT image models treat it as
  guidance, not an exact shape.
- Either way the result is put back through the selection on this computer
  (``imaging.composite``): pixels outside the selection are copied from the
  original, never from the service's picture.
- Create (text to image) is ``/images`` or ``/images/generations`` with the prompt.

The API key goes only into the Authorization header. It is never logged, never
put in an error text, and requests are never redirected to another address.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import math
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .. import __version__, imaging
from ..backend import (
    Backend,
    CancelCallback,
    CloudAuthError,
    CloudCreditsError,
    CloudError,
    CloudRateLimited,
    CloudRefused,
    CloudTimeout,
    CloudUnreachable,
    GenerationCancelled,
    GenerationResult,
    HealthStatus,
    InpaintRequest,
    PreviewCallback,
    Progress,
    ProgressCallback,
    Txt2ImgRequest,
)
from ..geometry import seed_or_random
from ..log import redact
from ..png import PngError

# Longest side sent to the service. Larger contexts are sent at a fraction of their
# pixels (the selection is regenerated at that resolution either way).
MAX_SEND_SIDE = 1536
DEFAULT_TIMEOUT = 240.0  # seconds; image models take 10 to 120 s, slower when busy
EXPECTED_SECONDS = 30.0  # for the progress bar only
PROJECT_URL = "https://github.com/beckettharriman/openfill"


@dataclass(frozen=True)
class Provider:
    id: str
    label: str  # in menus
    name: str  # in sentences ("sent to OpenRouter")
    base_url: str
    default_model: str
    ratios: tuple[str, ...]  # aspect ratios the image API offers
    keys_url: str = ""
    credits_url: str = ""


PROVIDERS = {
    "openrouter": Provider(
        "openrouter",
        "OpenRouter",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        "google/gemini-2.5-flash-image",
        ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"),
        "https://openrouter.ai/keys",
        "https://openrouter.ai/settings/credits",
    ),
    "openai": Provider(
        "openai",
        "OpenAI-compatible (OpenAI, LiteLLM, ...)",
        "",  # the address's host name
        "https://api.openai.com/v1",
        "",  # depends on the service: the user names it
        ("1:1", "3:2", "2:3"),
    ),
}
OPENAI_SIZES = {"1:1": "1024x1024", "3:2": "1536x1024", "2:3": "1024x1536"}


def provider(provider_id: str) -> Provider:
    return PROVIDERS.get(provider_id, PROVIDERS["openrouter"])


def base_url(provider_id: str, url: str = "") -> str:
    return (url or provider(provider_id).base_url).strip().rstrip("/")


def destination(provider_id: str, url: str = "") -> str:
    """Who receives the image, in words: "OpenRouter", or the address's host."""
    p = provider(provider_id)
    if p.name and not url:
        return p.name
    host = urlparse(base_url(provider_id, url)).netloc or base_url(provider_id, url)
    return p.name if p.name and host.endswith("openrouter.ai") else host


def cloud_note(provider_id: str, url: str = "", what: str = "image") -> str:
    """The line every cloud screen shows."""
    return f"Cloud: your {what} is sent to {destination(provider_id, url)}."


def url_problem(url: str) -> str:
    """Why ``url`` cannot carry an API key ("" when it can): https, or plain http only
    to this computer or the local network."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return f"'{url}' is not a web address. It should look like https://openrouter.ai/api/v1."
    if parsed.scheme == "https":
        return ""
    host = parsed.hostname
    try:
        ip = ipaddress.ip_address(host)
        local = ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        local = host == "localhost" or "." not in host or host.endswith((".local", ".lan", ".internal", ".home.arpa"))
    if local:
        return ""
    return (
        f"'{url}' uses plain http, which would send your API key unencrypted. Use an https address "
        "(plain http works only for an address on this computer or your own network)."
    )


class _NoRedirect(HTTPRedirectHandler):
    """A redirect would carry the Authorization header to another address: refuse it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102, PLR0913
        return None


def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _from_data_url(url: str) -> bytes:
    if not url.startswith("data:"):
        raise ValueError("not a data URL")
    return base64.b64decode(url.split(",", 1)[1])


def _short(text: str, limit: int = 200) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


class CloudBackend(Backend):
    """Generation through a cloud image API (see the module text)."""

    name = "cloud"

    def __init__(
        self,
        provider_id: str = "openrouter",
        api_key: str = "",
        model: str = "",
        url: str = "",
        api: str = "images",
        request_timeout: float = 0.0,
        logger: logging.Logger | None = None,
        decode: Callable[[bytes], tuple[int, int, bytes]] | None = None,
        opener: Any = None,
    ) -> None:
        self.provider = provider(provider_id)
        self._key = api_key.strip()
        self.model = (model or self.provider.default_model).strip()
        self.url = base_url(provider_id, url)
        self.api = "chat" if api == "chat" and self.provider.id == "openrouter" else "images"
        self.request_timeout = float(request_timeout or 0) or DEFAULT_TIMEOUT
        self.log = logger
        self.decode = decode  # the editor's reader for JPEG / WebP results: (width, height, RGBA bytes)
        self.opener = opener or build_opener(_NoRedirect)
        self.where = destination(provider_id, url)
        self._progress = Progress()
        self._cancelled = threading.Event()

    def __repr__(self) -> str:  # the key never appears, even in a traceback's locals
        return f"CloudBackend({self.provider.id!r}, model={self.model!r}, url={self.url!r})"

    # safety ----------------------------------------------------------------

    def _clean(self, text: str) -> str:
        text = redact(str(text))
        if self._key:
            text = text.replace(self._key, "[redacted]")
        return text

    def _info(self, fmt: str, *args: Any) -> None:
        if self.log:
            self.log.info("cloud: " + fmt, *(self._clean(a) if isinstance(a, str) else a for a in args))

    def _check_ready(self) -> None:
        problem = url_problem(self.url)
        if problem:
            raise CloudRefused(problem + " Fix the address in the cloud settings.")
        if not self._key:
            raise CloudAuthError(
                f"No API key is set for {self.where}. Paste your key in the cloud settings, then try again."
            )
        if not self.model:
            raise CloudRefused(
                f"No model is set for {self.where}. Type the model name in the cloud settings (for example "
                "gpt-image-1.5 for OpenAI), then try again."
            )

    # http ------------------------------------------------------------------

    def _headers(self, content_type: str = "application/json") -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
            "User-Agent": f"OpenFill/{__version__}",
        }
        if content_type:
            headers["Content-Type"] = content_type
        if self.provider.id == "openrouter":
            headers["HTTP-Referer"] = PROJECT_URL  # OpenRouter's optional app attribution
            headers["X-Title"] = "OpenFill"
        return headers

    def _send(self, method: str, path: str, body: bytes | None, content_type: str, timeout: float) -> Any:
        req = Request(self.url + path, data=body, method=method, headers=self._headers(content_type))
        started = time.monotonic()
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                status = resp.status
        except HTTPError as err:
            try:
                detail = err.read()[:4000].decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - the status alone is enough
                detail = ""
            finally:
                err.close()
            self._info("%s %s -> HTTP %s after %.1f s", method, path, err.code, time.monotonic() - started)
            raise self._http_error(err.code, detail, err.headers) from None
        except TimeoutError:
            raise self._timeout_error(timeout) from None
        except URLError as err:
            if isinstance(err.reason, TimeoutError) or "timed out" in str(err.reason):
                raise self._timeout_error(timeout) from None
            reason = self._clean(getattr(err.reason, "strerror", None) or err.reason)
            raise CloudUnreachable(
                f"Could not reach {self.where} ({reason}). Check your internet connection and the address in "
                "the cloud settings, then try again."
            ) from None
        except OSError as err:
            if "timed out" in str(err):
                raise self._timeout_error(timeout) from None
            raise CloudUnreachable(
                f"The connection to {self.where} failed ({self._clean(err)}). Check your internet connection, "
                "then try again."
            ) from None
        self._info("%s %s -> HTTP %s after %.1f s", method, path, status, time.monotonic() - started)
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            raise CloudRefused(
                f"{self.where} sent an answer OpenFill cannot read. Check the address in the cloud settings "
                "(it should end in /v1 or /api/v1)."
            ) from None
        if isinstance(data, dict) and isinstance(data.get("error"), dict):  # an error inside a 200 answer
            raise self._http_error(int(data["error"].get("code") or 502), json.dumps(data), {})
        return data

    def _timeout_error(self, timeout: float) -> CloudTimeout:
        return CloudTimeout(
            f"{self.where} did not answer within {timeout:.0f} seconds. Nothing was changed in your image. "
            "Try again, or try a faster model."
        )

    def _http_error(self, code: int, detail: str, headers: Any) -> CloudError:
        message = ""
        try:
            parsed = json.loads(detail)
            error = parsed.get("error") if isinstance(parsed, dict) else None
            if isinstance(error, dict):
                message = str(error.get("message") or "")
                reasons = (
                    (error.get("metadata") or {}).get("reasons") if isinstance(error.get("metadata"), dict) else None
                )
                if reasons:
                    message += f" ({', '.join(map(str, reasons))})"
            elif isinstance(error, str):
                message = error
        except (ValueError, AttributeError):
            message = detail if detail and "<html" not in detail.lower() else ""
        said = f' It said: "{_short(self._clean(message))}"' if message.strip() else ""
        where = self.where
        if code == 401:
            keys = f" You can make a new key at {self.provider.keys_url}." if self.provider.keys_url else ""
            return CloudAuthError(
                f"{where} did not accept your API key (401). Check that the key in the cloud settings is "
                f"complete and still active.{keys}"
            )
        if code == 402:
            more = f" Add credits at {self.provider.credits_url}" if self.provider.credits_url else " Add credits"
            return CloudCreditsError(
                f"Your {where} account is out of credits (402).{more}, then try again. Nothing was changed in "
                "your image."
            )
        if code == 429:
            wait = ""
            try:
                after = int(float((headers or {}).get("Retry-After") or 0))
                wait = f" about {after} seconds" if after > 0 else ""
            except (TypeError, ValueError):
                pass
            return CloudRateLimited(
                f"{where} is limiting how fast you can send requests (429). Wait{wait or ' a minute'} and try again."
            )
        if code in (408, 504, 524):
            return CloudTimeout(
                f"{where} took too long to answer ({code}). Nothing was changed in your image. Try again in a moment."
            )
        if code == 403:
            return CloudRefused(
                f"{where} refused this request (403): it may have flagged the picture or the prompt, or your key "
                f"may not be allowed to use {self.model}.{said} Try a different prompt or model."
            )
        if code == 404:
            return CloudRefused(
                f"{where} does not offer the model '{self.model}' at this address (404).{said} Check the model name "
                "and the address in the cloud settings."
            )
        if code in (400, 413, 415, 422):
            return CloudRefused(
                f"{where} did not accept the request ({code}).{said} Try a smaller selection or another model in "
                "the cloud settings."
            )
        if code >= 500:
            return CloudUnreachable(
                f"{where} or the model {self.model} is not working right now ({code}).{said} Try again later, or "
                "pick another model in the cloud settings. Nothing was changed in your image."
            )
        return CloudRefused(f"{where} answered with an error ({code}).{said} Check the cloud settings and try again.")

    # running a request ---------------------------------------------------------

    def _run(
        self,
        label: str,
        call: Callable[[], list[bytes]],
        progress: ProgressCallback | None,
        cancel: CancelCallback | None,
    ) -> list[bytes]:
        """``call`` in a worker thread; progress by elapsed time, Cancel stops waiting at once."""
        self._cancelled.clear()
        result: list[list[bytes]] = []
        error: list[BaseException] = []

        def worker() -> None:
            try:
                result.append(call())
            except BaseException as err:  # noqa: BLE001 - handed to the caller
                error.append(err)

        thread = threading.Thread(target=worker, name="openfill-cloud", daemon=True)
        thread.start()
        started = time.monotonic()
        while thread.is_alive():
            thread.join(0.25)
            if self._cancelled.is_set() or (cancel and cancel()):
                self._progress = Progress(0.0, "cancelled", False)
                raise GenerationCancelled(
                    f"cancelled; {self.where} may still finish (and charge for) the request it already received"
                )
            elapsed = time.monotonic() - started
            fraction = 0.05 + 0.85 * (1.0 - math.exp(-elapsed / EXPECTED_SECONDS))
            text = f"{label} {self.where} ({elapsed:.0f} s)..."
            self._progress = Progress(fraction, text, True)
            if progress:
                progress(fraction, text)
        self._progress = Progress(1.0, "done", False)
        if error:
            raise error[0]
        return result[0]

    def _image(self, data: bytes) -> imaging.Rgba:
        kind = imaging.sniff(data)
        if kind == "png":
            try:
                return imaging.decode_png(data)
            except PngError as err:
                if self.decode is None:
                    raise CloudRefused(f"{self.where} sent a picture OpenFill cannot read ({err}).") from None
        elif self.decode is None:
            raise CloudRefused(
                f"{self.where} sent a {kind.upper() or 'picture'} file, which OpenFill cannot read here. Pick a model "
                "that returns PNG in the cloud settings."
            )
        try:
            width, height, rgba = self.decode(data)
            return imaging.Rgba(width, height, rgba)
        except Exception as err:  # noqa: BLE001 - said in plain words
            raise CloudRefused(f"{self.where} sent a picture OpenFill cannot read ({self._clean(err)}).") from None

    # provider calls ----------------------------------------------------------------

    def _collect(self, data: Any) -> list[bytes]:
        """Image bytes from an images API answer (``data[].b64_json`` or ``data[].url``)."""
        out = []
        for item in (data or {}).get("data") or []:
            if item.get("b64_json"):
                out.append(base64.b64decode(item["b64_json"]))
            elif str(item.get("url", "")).startswith("data:"):
                out.append(_from_data_url(item["url"]))
            elif item.get("url"):
                out.append(self._download(str(item["url"])))
        return out

    def _download(self, url: str) -> bytes:
        """A result the service left at a URL (never sent the key: it is another address)."""
        if urlparse(url).scheme != "https":
            raise CloudRefused(f"{self.where} returned the picture at an address OpenFill does not trust.")
        try:
            with build_opener().open(Request(url, headers={"User-Agent": f"OpenFill/{__version__}"}), timeout=60) as r:
                return r.read()
        except (OSError, URLError) as err:
            raise CloudUnreachable(
                f"The picture from {self.where} could not be downloaded ({self._clean(err)})."
            ) from None

    def _openrouter(self, prompt: str, inputs: list[bytes], ratio: str, count: int) -> list[bytes]:
        refs = [{"type": "image_url", "image_url": {"url": _data_url(png)}} for png in inputs]
        if self.api == "chat":
            images: list[bytes] = []
            for _ in range(count):
                body = {
                    "model": self.model,
                    "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}, *refs]}],
                    "modalities": ["image", "text"],
                    "image_config": {"aspect_ratio": ratio},
                }
                data = self._send(
                    "POST", "/chat/completions", json.dumps(body).encode(), "application/json", self.request_timeout
                )
                for choice in data.get("choices") or []:
                    for img in (choice.get("message") or {}).get("images") or []:
                        url = str((img.get("image_url") or {}).get("url", ""))
                        if url.startswith("data:"):
                            images.append(_from_data_url(url))
                        elif url:
                            images.append(self._download(url))
            return images
        body: dict[str, Any] = {"model": self.model, "prompt": prompt, "n": count, "aspect_ratio": ratio}
        if refs:
            body["input_references"] = refs
        data = self._send("POST", "/images", json.dumps(body).encode(), "application/json", self.request_timeout)
        return self._collect(data)

    def _openai_edit(self, prompt: str, image: bytes, mask: bytes, ratio: str, count: int) -> list[bytes]:
        boundary = "openfill-" + uuid.uuid4().hex
        parts = []
        fields = {"model": self.model, "prompt": prompt, "n": str(count), "size": OPENAI_SIZES.get(ratio, "1024x1024")}
        for name, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        for name, data in (("image", image), ("mask", mask)):
            head = (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{name}.png"\r\n'
                "Content-Type: image/png\r\n\r\n"
            )
            parts.append(head.encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        content_type = f"multipart/form-data; boundary={boundary}"
        data = self._send("POST", "/images/edits", b"".join(parts), content_type, self.request_timeout)
        return self._collect(data)

    def _openai_generate(self, prompt: str, ratio: str, count: int) -> list[bytes]:
        body = {"model": self.model, "prompt": prompt, "n": count, "size": OPENAI_SIZES.get(ratio, "1024x1024")}
        data = self._send(
            "POST", "/images/generations", json.dumps(body).encode(), "application/json", self.request_timeout
        )
        return self._collect(data)

    # prompts --------------------------------------------------------------------------

    @staticmethod
    def instruction(mode: str, prompt: str, negative: str, true_mask: bool) -> str:
        """What the model is asked to do. With ``true_mask`` the API carries the mask;
        otherwise the second picture is the mask and the text says how to read it."""
        prompt, negative = prompt.strip(), negative.strip()
        where = "the masked area" if true_mask else "the area that is white in the second picture (the mask)"
        if mode == "remove":
            text = (
                f"Remove what is in {where} and fill it with the background that would be behind it, continuing "
                "the surrounding textures, lines and lighting. Do not add any new object, person or text."
            )
        elif mode == "expand":
            text = (
                f"The picture is being made larger: {where} is new, empty canvas. Fill it so the picture continues "
                "naturally past its old edges, with the same scene, light, perspective and style."
            )
            if prompt:
                text += f" In the new area: {prompt}."
        else:
            text = f"In {where}, paint: {prompt or 'what fits the surroundings'}."
            text += " Match the lighting, perspective, colours and grain of the rest of the picture."
        if not true_mask:
            text += (
                " Keep everything outside that area exactly as it is. Return the whole first picture at the same "
                "framing, without the mask."
            )
        if negative:
            text += f" Avoid: {negative}."
        return text

    # backend interface -----------------------------------------------------------------

    def health(self) -> HealthStatus:
        """Check the key and the address without sending any picture."""
        try:
            self._check_ready()
            if self.provider.id == "openrouter":
                data = self._send("GET", "/key", None, "", 20)
                info = data.get("data") or {}
                left = info.get("limit_remaining")
                credit = f", {left:.2f} credits left on this key" if isinstance(left, (int, float)) else ""
                return HealthStatus(
                    True, f"{self.where} accepted the key{credit}. Model: {self.model}", device_kind="cloud"
                )
            data = self._send("GET", "/models", None, "", 20)
            ids = [str(m.get("id")) for m in (data.get("data") or []) if isinstance(m, dict)]
            known = "" if not ids or self.model in ids else f" (it does not list {self.model}; check the model name)"
            return HealthStatus(True, f"{self.where} accepted the key{known}. Model: {self.model}", device_kind="cloud")
        except CloudError as err:
            return HealthStatus(False, str(err))

    def progress(self) -> Progress:
        return self._progress

    def cancel(self) -> None:
        """Stop waiting; the service may still finish the request it received."""
        self._cancelled.set()

    def inpaint(
        self,
        request: InpaintRequest,
        progress: ProgressCallback | None = None,
        preview: PreviewCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> GenerationResult:
        """Fill, remove or expand: send the context and mask, put the result back through the mask."""
        self._check_ready()
        original = imaging.decode_png(request.image_png)
        mask_image = imaging.decode_png(request.mask_png)
        if (original.width, original.height) != (mask_image.width, mask_image.height):
            raise CloudRefused("The picture and its selection mask differ in size; select the area again.")
        mask = imaging.gray_of(mask_image)
        w, h = original.width, original.height
        factor = max(1, math.ceil(max(w, h) / MAX_SEND_SIDE))
        small = imaging.shrink(original, factor)
        small_mask, mw, mh = imaging.shrink_gray(mask, w, h, factor)
        pad = imaging.pad_plan(small.width, small.height, self.provider.ratios)
        sent = imaging.pad_edges(small, pad)
        sent_mask = imaging.pad_gray(small_mask, mw, mh, pad, 0)
        true_mask = self.provider.id == "openai"
        mode = request.mode if request.mode in ("remove", "expand") else "fill"
        prompt = self.instruction(mode, request.prompt, request.negative, true_mask)
        count = max(1, min(4, int(request.sampling.batch)))
        self._info(
            "%s via %s %s, model %s: context %dx%d sent as %dx%d (ratio %s, 1/%d), %d variation(s)",
            mode,
            self.provider.id,
            self.api if self.provider.id == "openrouter" else "images/edits",
            self.model,
            w,
            h,
            pad.width,
            pad.height,
            pad.ratio,
            factor,
            count,
        )
        image_png = sent.png()
        if true_mask:
            mask_png = imaging.edit_mask_png(sent_mask, pad.width, pad.height)
            call = lambda: self._openai_edit(prompt, image_png, mask_png, pad.ratio, count)  # noqa: E731
        else:
            mask_png = imaging.mask_rgb(sent_mask, pad.width, pad.height)
            call = lambda: self._openrouter(prompt, [image_png, mask_png], pad.ratio, count)  # noqa: E731
        if progress:
            progress(0.02, f"Sending the picture to {self.where}...")
        raw = self._run("Waiting for", call, progress, cancel)
        if not raw:
            raise CloudRefused(
                f"{self.where} answered without a picture. The model may not make images, or it declined this one; "
                "try another prompt or model."
            )
        feather = max(1, min(8, round(request.feather * w / max(1, request.work_width) / 2)))
        images = []
        for data in raw:
            generated = self._image(data)
            sx, sy = generated.width / pad.width, generated.height / pad.height
            source = (pad.left * sx, pad.top * sy, w / factor * sx, h / factor * sy)
            images.append(imaging.composite(original, generated, mask, source, feather).png())
        if progress:
            progress(1.0, "Done")
        return GenerationResult(
            images, seed_or_random(-1), {"backend": self.name, "provider": self.provider.id, "model": self.model}
        )

    def txt2img(
        self,
        request: Txt2ImgRequest,
        progress: ProgressCallback | None = None,
        preview: PreviewCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> GenerationResult:
        """Create: the prompt only; the picture comes back at the service's size for the nearest ratio."""
        self._check_ready()
        ratio = imaging.nearest_ratio(max(1, request.width), max(1, request.height), self.provider.ratios)
        count = max(1, min(4, int(request.sampling.batch)))
        prompt = request.prompt.strip() + (f" Avoid: {request.negative.strip()}." if request.negative.strip() else "")
        self._info("create via %s, model %s: ratio %s, %d variation(s)", self.provider.id, self.model, ratio, count)
        if self.provider.id == "openai":
            call = lambda: self._openai_generate(prompt, ratio, count)  # noqa: E731
        else:
            call = lambda: self._openrouter(prompt, [], ratio, count)  # noqa: E731
        if progress:
            progress(0.02, f"Sending the prompt to {self.where}...")
        raw = self._run("Waiting for", call, progress, cancel)
        if not raw:
            raise CloudRefused(f"{self.where} answered without a picture. Try another prompt or model.")
        images = [data if imaging.sniff(data) == "png" else self._image(data).png() for data in raw]
        return GenerationResult(
            images, seed_or_random(-1), {"backend": self.name, "provider": self.provider.id, "model": self.model}
        )
