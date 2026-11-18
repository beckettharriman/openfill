# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Google Gemini image generation and editing backend via REST ``:generateContent``.

Subclasses :class:`~openfill.backends.cloud.CloudBackend` to reuse its threading,
cancellation, timeout deadline, size capping, strict pixel-mask compositing, and
URL validation while speaking Google Gemini's ``generateContent`` JSON protocol.

The API key is read from ``config.json`` (bound to Gemini and its endpoint) or from
``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` via :class:`GeminiCredentials`. It is sent
only in the ``x-goog-api-key`` header and never written to logs.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .. import __version__, imaging, quality
from ..backend import (
    CancelCallback,
    CloudAuthError,
    CloudCreditsError,
    CloudError,
    CloudRateLimited,
    CloudRefused,
    CloudUnreachable,
    GenerationCancelled,
    GenerationResult,
    HealthStatus,
    InpaintRequest,
    PreviewCallback,
    ProgressCallback,
)
from ..geometry import seed_or_random
from .cloud import (
    HEALTH_TIMEOUT,
    MAX_VARIATIONS,
    PROVIDERS,
    CloudBackend,
    Provider,
    _b64,
    _short,
)

DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-image"
STANDARD_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
EXTENDED_RATIOS = (*STANDARD_RATIOS, "1:4", "4:1", "1:8", "8:1")

SAFETY_FINISH_REASONS = frozenset(
    {
        "SAFETY",
        "IMAGE_SAFETY",
        "PROHIBITED_CONTENT",
        "IMAGE_PROHIBITED_CONTENT",
        "RECITATION",
        "IMAGE_RECITATION",
        "BLOCKLIST",
        "SPII",
        "OTHER",
    }
)


@dataclass(frozen=True)
class GeminiModelInfo:
    """Per-model configuration (aspect ratios, send size, max input images)."""

    id: str
    ratios: tuple[str, ...] = STANDARD_RATIOS
    max_send_side: int = 1536
    max_input_images: int = 2


GEMINI_MODELS: dict[str, GeminiModelInfo] = {
    "gemini-3.1-flash-image": GeminiModelInfo("gemini-3.1-flash-image", EXTENDED_RATIOS, 1536, 2),
    "gemini-3-pro-image": GeminiModelInfo("gemini-3-pro-image", STANDARD_RATIOS, 1536, 2),
    "gemini-3.1-flash-lite-image": GeminiModelInfo("gemini-3.1-flash-lite-image", STANDARD_RATIOS, 1536, 2),
    "gemini-2.5-flash-image": GeminiModelInfo("gemini-2.5-flash-image", STANDARD_RATIOS, 1536, 2),
}


def model_info(model_id: str) -> GeminiModelInfo:
    """Per-model limits and ratios; unknown model IDs fall back to standard defaults."""
    clean = (model_id or "").strip().removeprefix("models/") or DEFAULT_GEMINI_MODEL
    return GEMINI_MODELS.get(clean, GeminiModelInfo(clean, STANDARD_RATIOS, 1536, 2))


GEMINI_PROVIDER = Provider(
    "gemini",
    "Google Gemini",
    "Google Gemini",
    "https://generativelanguage.googleapis.com/v1beta",
    DEFAULT_GEMINI_MODEL,
    STANDARD_RATIOS,
    "https://aistudio.google.com/apikey",
    "https://ai.google.dev/gemini-api/docs/rate-limits",
    host="generativelanguage.googleapis.com",
)
PROVIDERS["gemini"] = GEMINI_PROVIDER


class GeminiCredentials:
    """API key lookup for Google Gemini: ``GEMINI_API_KEY``, then ``GOOGLE_API_KEY``,
    then an optional in-memory key for the current session only (never persisted)."""

    ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

    def __init__(self, environ: Mapping[str, str] | None = None, session_key: str = "") -> None:
        self._environ = environ
        self._session_key = session_key.strip()

    def __repr__(self) -> str:
        return f"GeminiCredentials(env={bool(self.env_var_name())}, session={self.has_session_key})"

    def _env(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ

    def env_var_name(self) -> str:
        """The first populated Gemini environment variable name, or ``""``."""
        env = self._env()
        for name in self.ENV_VARS:
            if (env.get(name) or "").strip():
                return name
        return ""

    def env_key(self) -> str:
        name = self.env_var_name()
        return (self._env().get(name) or "").strip() if name else ""

    @property
    def has_env_key(self) -> bool:
        return bool(self.env_var_name())

    @property
    def has_session_key(self) -> bool:
        return bool(self._session_key)

    def set_session_key(self, key: str) -> None:
        self._session_key = key.strip()

    def clear_session_key(self) -> None:
        self._session_key = ""

    def with_session_key(self, key: str) -> GeminiCredentials:
        """Return a new credentials object with ``key`` if non-empty, else ``self``."""
        cleaned = key.strip()
        if not cleaned:
            return self
        return GeminiCredentials(environ=self._environ, session_key=cleaned)

    def get(self) -> str:
        """Effective key: environment variable first, then session key."""
        return self.env_key() or self._session_key

    @property
    def usable(self) -> bool:
        return bool(self.get())

    def problem(self) -> str:
        if self.usable:
            return ""
        return (
            "No Gemini API key is set. Set the GEMINI_API_KEY environment variable (and restart OpenFill), "
            "or paste your key in the cloud settings for this session."
        )


def _extract_error_info(detail: str) -> tuple[str, list[dict[str, Any]]]:
    """Parse Google's ``{"error": {"message", "status", "details"}}`` payload when JSON."""
    try:
        parsed = json.loads(detail)
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err, dict):
            msg = str(err.get("message") or "").strip()
            status = str(err.get("status") or "").strip()
            details = err.get("details") if isinstance(err.get("details"), list) else []
            text = f"{msg} ({status})" if msg and status and status not in msg else (msg or status)
            return text, [d for d in details if isinstance(d, dict)]
        if isinstance(err, str):
            return err, []
    except (ValueError, AttributeError):
        pass
    return (detail if detail and "<html" not in detail.lower() else ""), []


def _has_zero_quota(detail: str, details: list[dict[str, Any]]) -> bool:
    """Best-effort heuristic for free/unbilled 429 RESOURCE_EXHAUSTED responses with limit 0."""
    lowered = detail.lower()
    if "limit: 0" in lowered or "limit:0" in lowered:
        return True
    for entry in details:
        for violation in entry.get("violations") or []:
            if not isinstance(violation, dict):
                continue
            if str(violation.get("quotaValue") or "") == "0":
                return True
            meta = violation.get("quotaDimensions") or {}
            if isinstance(meta, dict) and str(meta.get("quota_limit_value") or "") == "0":
                return True
        text = json.dumps(entry).lower()
        if '"quota_limit_value": "0"' in text or '"quota_limit_value":"0"' in text or "limit: 0" in text:
            return True
    return False


def _is_invalid_api_key(code: int, detail: str, details: list[dict[str, Any]]) -> bool:
    if code == 401:
        return True
    lowered = detail.lower()
    if any(str(d.get("reason") or "").upper() == "API_KEY_INVALID" for d in details):
        return True
    return code in (400, 403) and ("api_key_invalid" in lowered or "api key not valid" in lowered)


def gemini_instruction(mode: str, prompt: str, negative: str, mask_mode: str) -> str:
    """Prompt instruction for Gemini image editing (``overlay`` or ``second_image``)."""
    prompt, negative = prompt.strip(), negative.strip()
    if mask_mode != "overlay":
        where = "the area that is white in the second picture (the mask)"
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
        elif prompt:
            text = (
                f"In {where}, paint: {prompt}. Match the lighting, perspective, colours and grain of the rest of "
                "the picture."
            )
        else:
            text = (
                f"Fill {where} so it continues the surroundings seamlessly, matching the lighting, texture, "
                "colours and perspective."
            )
        text += (
            " Edit ONLY that marked area and keep everything outside it unchanged. Keep the background colour "
            "temperature, brightness and contrast inside the edited region identical to the surrounding pixels so "
            "no seam appears. Return the whole first picture at the same framing, without the mask."
        )
        if negative:
            text += f" Avoid: {negative}."
        return text

    where = "the area highlighted in bright magenta (#FF0080)"
    if mode == "remove":
        text = (
            f"Remove what is under {where} and fill that area with the background that would be behind it, "
            "continuing the surrounding textures, lines and lighting. Do not add any new object, person or text."
        )
    elif mode == "expand":
        text = (
            f"The picture is being made larger: {where} is new, empty canvas. Fill it so the picture continues "
            "naturally past its old edges, with the same scene, light, perspective and style."
        )
        if prompt:
            text += f" In the new area: {prompt}."
    elif prompt:
        text = (
            f"Replace {where} with: {prompt}. Match the lighting, perspective, colours and grain of the rest of "
            "the picture."
        )
    else:
        text = (
            f"Replace {where} so it continues the surroundings seamlessly, matching the lighting, texture, "
            "colours and perspective."
        )
    text += (
        " Edit ONLY the highlighted area, completely replace the magenta highlight and border so no magenta tint "
        "or outline remains, and keep everything outside that area unchanged. Keep the background colour "
        "temperature, brightness and contrast inside the edited region identical to the surrounding pixels so "
        "no seam appears. Return the whole picture at the same framing."
    )
    if negative:
        text += f" Avoid: {negative}."
    return text


class GeminiBackend(CloudBackend):
    """Google Gemini image generation and editing via ``POST /v1beta/models/{model}:generateContent``."""

    def __init__(
        self,
        api_key: str = "",
        model: str = "",
        url: str = "",
        mask_mode: str = "second_image",
        request_timeout: float = 0.0,
        logger: logging.Logger | None = None,
        decode: Callable[[bytes], tuple[int, int, bytes]] | None = None,
        opener: Any = None,
        key_problem: str = "",
        max_retries: int = 2,
        retry_base_delay: float = 1.0,
    ) -> None:
        problem = key_problem or (
            ""
            if api_key.strip()
            else (
                "No Gemini API key is set. Set the GEMINI_API_KEY environment variable (and restart OpenFill), "
                "or paste your key in the cloud settings for this session."
            )
        )
        super().__init__(
            "gemini",
            api_key=api_key,
            model=model,
            url=url,
            request_timeout=request_timeout,
            logger=logger,
            decode=decode,
            opener=opener,
            key_problem=problem,
        )
        self.mask_mode = "overlay" if mask_mode == "overlay" else "second_image"
        self.max_retries = max(0, int(max_retries))
        self.retry_base_delay = max(0.0, float(retry_base_delay))
        self._last_http_code = 0
        self._last_retry_after = 0.0

    def __repr__(self) -> str:
        return f"GeminiBackend(model={self.model!r}, url={self.url!r}, mask_mode={self.mask_mode!r})"

    def _model_path(self) -> str:
        clean = self.model.strip().removeprefix("models/")
        return f"/models/{quote(clean, safe='.-_:')}"

    def _max_send_side(self) -> int:
        return model_info(self.model).max_send_side

    def _ratios(self) -> tuple[str, ...]:
        return model_info(self.model).ratios

    def _headers(self, content_type: str = "application/json") -> dict[str, str]:
        headers = {
            "x-goog-api-key": self._key,
            "Accept": "application/json",
            "User-Agent": f"OpenFill/{__version__}",
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _http_error(self, code: int, detail: str, headers: Any) -> CloudError:
        self._last_http_code = code
        self._last_retry_after = 0.0
        message, details = _extract_error_info(detail)
        said = f' It said: "{_short(self._clean(message))}"' if message.strip() else ""
        if _is_invalid_api_key(code, detail, details):
            return CloudAuthError(
                f"{self.where} did not accept your API key ({code}). Check GEMINI_API_KEY or your session key, "
                f"or make a new key at {self.provider.keys_url}."
            )
        if code == 429:
            if _has_zero_quota(detail, details):
                return CloudCreditsError(
                    f"Your {self.where} project has no available quota for '{self.model}' (429). "
                    f"Free-tier keys may have a 0 limit for image generation; check billing and quotas at "
                    f"{self.provider.credits_url}, then try again. Nothing was changed in your image."
                )
            wait = ""
            try:
                after = float((headers or {}).get("Retry-After") or 0)
                if after > 0:
                    self._last_retry_after = after
                    wait = f" about {int(after)} seconds"
            except (TypeError, ValueError):
                pass
            return CloudRateLimited(
                f"{self.where} is limiting how fast you can send requests (429).{said} "
                f"Wait{wait or ' a moment'} and try again."
            )
        return super()._http_error(code, detail, headers)

    def _send(self, method: str, path: str, body: bytes | None, content_type: str, timeout: float) -> Any:
        for attempt in range(self.max_retries + 1):
            self._last_http_code = 0
            self._last_retry_after = 0.0
            try:
                return super()._send(method, path, body, content_type, timeout)
            except (CloudRateLimited, CloudUnreachable) as err:
                if isinstance(err, CloudUnreachable) and self._last_http_code < 500:
                    raise
                if attempt >= self.max_retries or self._cancelled.is_set():
                    raise
                delay = self._last_retry_after or (
                    self.retry_base_delay * (2**attempt) + random.uniform(0.0, 0.25 * self.retry_base_delay)
                )
                if self._deadline and time.monotonic() + delay + 0.1 >= self._deadline:
                    raise
                self._info(
                    "%s %s -> HTTP %s; retrying in %.1f s (attempt %d of %d)",
                    method,
                    path,
                    self._last_http_code,
                    delay,
                    attempt + 1,
                    self.max_retries,
                )
                if self._cancelled.wait(delay):
                    raise GenerationCancelled("cancelled before retry") from None

    def build_payload(self, prompt: str, images: list[bytes], ratio: str) -> dict[str, Any]:
        """Build a Gemini ``:generateContent`` request payload."""
        parts: list[dict[str, Any]] = [
            {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(img).decode("ascii")}} for img in images
        ]
        parts.append({"text": prompt})
        return {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {"aspectRatio": ratio},
            },
        }

    def parse_response(self, data: dict[str, Any]) -> bytes:
        """Extract the last non-thought image from a Gemini ``:generateContent`` response."""
        prompt_fb = data.get("promptFeedback") or data.get("prompt_feedback")
        if isinstance(prompt_fb, dict):
            block = prompt_fb.get("blockReason") or prompt_fb.get("block_reason")
            if block:
                raise CloudRefused(
                    f"{self.where} blocked the prompt or image ({self._clean(str(block))}). "
                    "Try a different prompt or selection."
                )
        candidates = data.get("candidates")
        if candidates is None or not isinstance(candidates, list):
            raise self._malformed("its candidates are missing or not a list")
        if not candidates:
            raise CloudRefused(
                f"{self.where} returned no image for this request. Try a different prompt or model."
            )
        cand = candidates[0]
        if not isinstance(cand, dict):
            raise self._malformed("a candidate entry is not an object")
        finish = str(cand.get("finishReason") or cand.get("finish_reason") or "").strip()
        content = cand.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if parts is not None and not isinstance(parts, list):
            raise self._malformed("candidate parts are not a list")
        images: list[bytes] = []
        texts: list[str] = []
        for part in parts or ():
            if not isinstance(part, dict):
                raise self._malformed("a candidate part is not an object")
            if part.get("thought"):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                mime = str(inline.get("mimeType") or inline.get("mime_type") or "")
                if not mime or mime.lower().startswith("image/"):
                    try:
                        images.append(_b64(inline["data"]))
                    except ValueError as err:
                        raise self._malformed(str(err)) from None
            elif isinstance(part.get("text"), str) and part["text"].strip():
                texts.append(part["text"].strip())
        if images:
            return images[-1]
        note = f' It said: "{_short(self._clean(" ".join(texts)))}"' if texts else ""
        if finish.upper() in SAFETY_FINISH_REASONS:
            raise CloudRefused(
                f"{self.where} blocked the generated image ({self._clean(finish)}).{note} "
                "Try a different prompt or selection."
            )
        raise CloudRefused(
            f"{self.where} answered without a picture.{note} The model may not make images, or it declined this "
            "request; try another prompt or model."
        )

    def _generate_variations(self, prompt: str, inputs: list[bytes], ratio: str, count: int) -> list[bytes]:
        payload = self.build_payload(prompt, inputs, ratio)
        body = json.dumps(payload).encode("utf-8")
        path = f"{self._model_path()}:generateContent"
        images: list[bytes] = []
        for index in range(count):
            if self._cancelled.is_set():
                break
            try:
                data = self._send("POST", path, body, "application/json", self.request_timeout)
                images.append(self.parse_response(data))
            except CloudError as err:
                if not images:
                    raise
                self._info("variation %d of %d failed, keeping %d: %s", index + 1, count, len(images), str(err))
                break
        return images

    def _inpaint_call(
        self,
        mode: str,
        prompt: str,
        negative: str,
        sent: imaging.Rgba,
        sent_mask: bytes,
        pad: imaging.Padding,
        count: int,
    ) -> tuple[Callable[[], list[bytes]], int, str]:
        effective_mode = (
            "overlay"
            if self.mask_mode == "overlay" or model_info(self.model).max_input_images < 2
            else "second_image"
        )
        full_prompt = gemini_instruction(mode, prompt, negative, effective_mode)
        if effective_mode == "overlay":
            inputs = [imaging.mask_overlay(sent, sent_mask)]
        else:
            inputs = [sent.png(), imaging.mask_rgb(sent_mask, pad.width, pad.height)]
        return (
            lambda: self._generate_variations(full_prompt, inputs, pad.ratio, count),
            count,
            f"generateContent ({effective_mode})",
        )

    def _txt2img_call(
        self, prompt: str, negative: str, ratio: str, count: int
    ) -> tuple[Callable[[], list[bytes]], int]:
        full_prompt = prompt.strip() + (f" Avoid: {negative.strip()}." if negative.strip() else "")
        return lambda: self._generate_variations(full_prompt, [], ratio, count), count

    def inpaint(
        self,
        request: InpaintRequest,
        progress: ProgressCallback | None = None,
        preview: PreviewCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> GenerationResult:
        """Fill, remove or expand, matching tone across the selection edge before compositing."""
        self._check_ready()
        original = imaging.decode_png(request.image_png)
        mask_image = imaging.decode_png(request.mask_png)
        if (original.width, original.height) != (mask_image.width, mask_image.height):
            raise CloudRefused("The picture and its selection mask differ in size; select the area again.")
        mask = imaging.gray_of(mask_image)
        w, h = original.width, original.height
        factor = max(1, math.ceil(max(w, h) / self._max_send_side()))
        small = imaging.shrink(original, factor)
        small_mask, mw, mh = imaging.shrink_gray(mask, w, h, factor)
        pad = imaging.pad_plan(small.width, small.height, self._ratios())
        sent = imaging.pad_edges(small, pad)
        sent_mask = imaging.pad_gray(small_mask, mw, mh, pad, 0)
        mode = request.mode if request.mode in ("remove", "expand") else "fill"
        count = max(1, min(MAX_VARIATIONS, int(request.sampling.batch)))
        call, requests, api_label = self._inpaint_call(
            mode, request.prompt, request.negative, sent, sent_mask, pad, count
        )
        self._info(
            "%s via %s %s, model %s: context %dx%d sent as %dx%d (ratio %s, 1/%d), %d variation(s)",
            mode,
            self.provider.id,
            api_label,
            self.model,
            w,
            h,
            pad.width,
            pad.height,
            pad.ratio,
            factor,
            count,
        )
        if progress:
            progress(0.02, f"Sending the picture to {self.where}...")
        raw = self._run("Waiting for", call, progress, cancel, requests)
        if not raw:
            raise CloudRefused(
                f"{self.where} answered without a picture. The model may not make images, or it declined this one; "
                "try another prompt or model."
            )
        feather = max(1, min(24, round(request.feather * w / max(1, request.work_width))))
        solid_mask = b"\xff" * (w * h)
        full_source = (0.0, 0.0, float(w), float(h))
        images = []
        for index, data in enumerate(raw):
            generated = self._image(data)
            sx, sy = generated.width / pad.width, generated.height / pad.height
            source = (pad.left * sx, pad.top * sy, w / factor * sx, h / factor * sy)
            if mode == "expand":
                images.append(imaging.overlay(generated, mask, w, h, source, feather).png())
                continue
            cropped = imaging.overlay(generated, solid_mask, w, h, source, 1)
            field = quality.seam_field(w, h, original.data, cropped.data, mask, 4, band=16, inside_band=16)
            if field is not None:
                largest = max(max(abs(v) for v in cell) for cell in field.offsets)
                cropped = imaging.Rgba(w, h, quality.apply_seam_field(cropped.data, w, h, 4, field))
                self._info("%s: seam corrected on variation %d (largest offset %.1f levels)", mode, index + 1, largest)
            images.append(imaging.overlay(cropped, mask, w, h, full_source, feather).png())
        if progress:
            progress(1.0, "Done")
        return GenerationResult(
            images, seed_or_random(-1), {"backend": self.name, "provider": self.provider.id, "model": self.model}
        )

    def health(self) -> HealthStatus:
        """Check the API key and model name against Google Gemini without sending any picture."""
        try:
            self._check_ready()
            limit = min(HEALTH_TIMEOUT, self.request_timeout)
            self._run(
                "Checking", lambda: self._send("GET", self._model_path(), None, "", limit), None, None, limit=limit
            )
            return HealthStatus(True, f"{self.where} accepted the key. Model: {self.model}", device_kind="cloud")
        except CloudError as err:
            return HealthStatus(False, str(err))
