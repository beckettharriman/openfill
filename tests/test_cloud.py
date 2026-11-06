# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""The optional cloud backend (OpenRouter / OpenAI-compatible), against a fake HTTP server.

No test here talks to a real service: every request goes to a local http.server.
"""

import base64
import email.parser
import email.policy
import json
import logging
import os
import random
import socket
import struct
import tempfile
import threading
import time
import traceback
import tracemalloc
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from openfill import imaging, png
from openfill.backend import (
    CloudAuthError,
    CloudCreditsError,
    CloudError,
    CloudRateLimited,
    CloudRefused,
    CloudTimeout,
    CloudUnreachable,
    GenerationCancelled,
    InpaintRequest,
    ModelSpec,
    Sampling,
    Txt2ImgRequest,
    describe_error,
)
from openfill.backends.cloud import CloudBackend, cloud_note, destination, key_scope, url_problem
from openfill.config import BACKENDS, Settings
from openfill.geometry import Rect
from openfill.log import RedactingFilter, redact

KEY = "sk-or-v1-" + "0123456789abcdef" * 4  # looks like an OpenRouter key; never real


def solid_png(width, height, rgba):
    return png.encode(width, height, bytes(rgba) * (width * height), 4)


def b64(data):
    return base64.b64encode(data).decode("ascii")


class FakeService:
    """A local stand-in for OpenRouter / an OpenAI-compatible server.

    ``routes`` maps a path to a function (handler, body) -> (status, headers, bytes);
    every request is recorded with its headers and body."""

    def __init__(self):
        self.requests = []
        self.routes = {}
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _any(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                service.requests.append(
                    {"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body}
                )
                route = service.routes.get(self.path)
                if route is None:
                    status, headers, data = 404, {}, b'{"error":{"code":404,"message":"no route"}}'
                else:
                    status, headers, data = route(self, body)
                try:
                    self.send_response(status)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            do_GET = do_POST = _any  # noqa: N815 - http.server names

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def json_route(self, path, payload, status=200, headers=None):
        self.routes[path] = lambda _h, _b: (
            status,
            {"Content-Type": "application/json", **(headers or {})},
            json.dumps(payload).encode(),
        )


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


def make_logger(name):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = ListHandler()
    logger.handlers = [handler]
    logger.filters = [RedactingFilter()]
    return logger, handler


def context(width=300, height=200, box=(100, 50, 220, 150)):
    """A gradient picture and a rectangular selection."""
    rng = random.Random(1)
    data = bytearray()
    for y in range(height):
        for x in range(width):
            data += bytes((x % 256, y % 256, (x + y) % 256, 255))
    noise = bytes(rng.randrange(256) for _ in range(16))
    data[: len(noise)] = noise  # not uniform at the corner either
    mask = bytearray(width * height)
    x0, y0, x1, y1 = box
    for y in range(y0, y1):
        mask[y * width + x0 : y * width + x1] = b"\xff" * (x1 - x0)
    return bytes(data), bytes(mask)


def inpaint_request(width=300, height=200, mode="fill", batch=1, prompt="a red ball", box=(100, 50, 220, 150)):
    rgba, mask = context(width, height, box)
    return (
        InpaintRequest(
            image_png=png.encode(width, height, rgba, 4),
            mask_png=png.encode(width, height, mask, 1),
            width=width,
            height=height,
            work_width=width,
            work_height=height,
            prompt=prompt,
            negative="",
            model=ModelSpec(checkpoint="m", arch="cloud", template="cloud"),
            sampling=Sampling(batch=batch),
            mode=mode,
            feather=8,
        ),
        rgba,
        mask,
    )


class CloudTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeService()
        self.addCleanup(self.fake.close)
        self.logger, self.logs = make_logger(f"openfill-cloud-test-{id(self)}")

    def backend(self, provider="openrouter", **kw):
        kw.setdefault("api_key", KEY)
        kw.setdefault("model", "test/image-model")
        return CloudBackend(
            provider, url=self.fake.url + ("/api/v1" if provider == "openrouter" else "/v1"), logger=self.logger, **kw
        )

    def assertNoKey(self, *texts):
        for text in texts:
            self.assertNotIn(KEY, str(text))
            self.assertNotIn(KEY[10:40], str(text))

    def tearDown(self):
        self.assertNoKey(*self.logs.lines)


# ------------------------------------------------------------- requests ----


class RequestShapeTests(CloudTestCase):
    def test_openrouter_images_request(self):
        self.fake.json_route("/api/v1/images", {"data": [{"b64_json": b64(solid_png(64, 48, (255, 0, 0, 255)))}]})
        request, _rgba, _mask = inpaint_request(batch=2)
        result = self.backend().inpaint(request)
        self.assertEqual(len(self.fake.requests), 1)
        sent = self.fake.requests[0]
        self.assertEqual((sent["method"], sent["path"]), ("POST", "/api/v1/images"))
        self.assertEqual(sent["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertEqual(sent["headers"]["X-Title"], "OpenFill")
        body = json.loads(sent["body"])
        self.assertEqual(body["model"], "test/image-model")
        self.assertEqual(body["n"], 2)
        self.assertEqual(body["aspect_ratio"], "3:2")  # 300 x 200
        self.assertIn("a red ball", body["prompt"])
        self.assertIn("white in the second picture", body["prompt"])
        refs = body["input_references"]
        self.assertEqual([r["type"] for r in refs], ["image_url", "image_url"])
        image = imaging.decode_png(base64.b64decode(refs[0]["image_url"]["url"].split(",", 1)[1]))
        mask = imaging.decode_png(base64.b64decode(refs[1]["image_url"]["url"].split(",", 1)[1]))
        self.assertEqual((image.width, image.height), (300, 200))
        self.assertEqual((mask.width, mask.height), (300, 200))
        gray = imaging.gray_of(mask)
        self.assertEqual(gray[60 * 300 + 110], 255)  # selected
        self.assertEqual(gray[10 * 300 + 10], 0)
        self.assertEqual(len(result.images), 1)  # the fake returned one
        self.assertEqual(png.read_header(result.images[0])[:2], (300, 200))

    def test_odd_shape_is_padded_to_a_ratio_the_service_offers(self):
        self.fake.json_route("/api/v1/images", {"data": [{"b64_json": b64(solid_png(32, 32, (0, 0, 255, 255)))}]})
        request, _rgba, _mask = inpaint_request(width=250, height=200, box=(50, 50, 150, 150))
        self.backend().inpaint(request)
        body = json.loads(self.fake.requests[0]["body"])
        self.assertEqual(body["aspect_ratio"], "5:4")
        image = imaging.decode_png(base64.b64decode(body["input_references"][0]["image_url"]["url"].split(",", 1)[1]))
        self.assertEqual((image.width, image.height), (250, 200))
        request, _rgba, _mask = inpaint_request(width=260, height=200, box=(50, 50, 150, 150))
        self.backend().inpaint(request)
        body = json.loads(self.fake.requests[1]["body"])
        self.assertEqual(body["aspect_ratio"], "4:3")
        image = imaging.decode_png(base64.b64decode(body["input_references"][0]["image_url"]["url"].split(",", 1)[1]))
        self.assertEqual((image.width, image.height), (267, 200))  # padded, not stretched

    def test_openrouter_chat_request(self):
        url = "data:image/png;base64," + b64(solid_png(30, 20, (0, 255, 0, 255)))
        self.fake.json_route(
            "/api/v1/chat/completions",
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "images": [{"type": "image_url", "image_url": {"url": url}}],
                        }
                    }
                ]
            },
        )
        request, _rgba, _mask = inpaint_request(batch=2, mode="remove", prompt="")
        result = self.backend(api="chat").inpaint(request)
        self.assertEqual(len(self.fake.requests), 2)  # one call per variation
        body = json.loads(self.fake.requests[0]["body"])
        self.assertEqual(body["modalities"], ["image", "text"])
        self.assertEqual(body["image_config"], {"aspect_ratio": "3:2"})
        parts = body["messages"][0]["content"]
        self.assertEqual([p["type"] for p in parts], ["text", "image_url", "image_url"])
        self.assertIn("Remove what is in", parts[0]["text"])
        self.assertEqual(len(result.images), 2)

    def test_openai_compatible_edit_sends_a_real_mask(self):
        self.fake.json_route("/v1/images/edits", {"data": [{"b64_json": b64(solid_png(48, 32, (9, 9, 9, 255)))}]})
        request, _rgba, _mask = inpaint_request()
        self.backend("openai", model="gpt-image-1.5").inpaint(request)
        sent = self.fake.requests[0]
        self.assertEqual(sent["path"], "/v1/images/edits")
        self.assertNotIn("X-Title", sent["headers"])
        content_type = sent["headers"]["Content-Type"]
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            f"Content-Type: {content_type}\r\n\r\n".encode() + sent["body"]
        )
        parts = {p.get_param("name", header="content-disposition"): p for p in message.iter_parts()}
        self.assertEqual(parts["model"].get_content().strip(), "gpt-image-1.5")
        self.assertEqual(parts["size"].get_content().strip(), "1536x1024")
        self.assertEqual(parts["n"].get_content().strip(), "1")
        self.assertIn("a red ball", parts["prompt"].get_content())
        image = imaging.decode_png(parts["image"].get_content())
        mask = imaging.decode_png(parts["mask"].get_content())
        self.assertEqual((image.width, image.height), (mask.width, mask.height))
        alpha = mask.data[3::4]
        self.assertEqual(alpha[60 * 300 + 110], 0)  # transparent = edit here
        self.assertEqual(alpha[10 * 300 + 10], 255)

    def test_create_requests(self):
        self.fake.json_route("/api/v1/images", {"data": [{"b64_json": b64(solid_png(40, 30, (1, 2, 3, 255)))}]})
        request = Txt2ImgRequest(768, 512, "a lighthouse", "fog", ModelSpec(arch="cloud"), Sampling(batch=1))
        result = self.backend().txt2img(request)
        body = json.loads(self.fake.requests[0]["body"])
        self.assertEqual(body["aspect_ratio"], "3:2")
        self.assertNotIn("input_references", body)
        self.assertEqual(body["prompt"], "a lighthouse Avoid: fog.")
        self.assertEqual(png.read_header(result.images[0])[:2], (40, 30))
        self.fake.json_route("/v1/images/generations", {"data": [{"b64_json": b64(solid_png(40, 30, (1, 2, 3, 255)))}]})
        self.backend("openai", model="gpt-image-1.5").txt2img(
            Txt2ImgRequest(512, 768, "x", "", ModelSpec(), Sampling())
        )
        body = json.loads(self.fake.requests[1]["body"])
        self.assertEqual(body["size"], "1024x1536")

    def test_large_context_is_sent_smaller(self):
        self.fake.json_route("/api/v1/images", {"data": [{"b64_json": b64(solid_png(16, 16, (5, 5, 5, 255)))}]})
        width = height = 1600
        rgba = bytes(width * height * 4)
        mask = bytearray(width * height)
        for y in range(700, 720):
            mask[y * width + 700 : y * width + 720] = b"\xff" * 20
        request = InpaintRequest(
            png.encode(width, height, rgba, 4),
            png.encode(width, height, bytes(mask), 1),
            width,
            height,
            1024,
            1024,
            "x",
            "",
            ModelSpec(arch="cloud"),
        )
        result = self.backend().inpaint(request)
        body = json.loads(self.fake.requests[0]["body"])
        image = imaging.decode_png(base64.b64decode(body["input_references"][0]["image_url"]["url"].split(",", 1)[1]))
        self.assertEqual((image.width, image.height), (800, 800))
        self.assertEqual(png.read_header(result.images[0])[:2], (1600, 1600))

    def test_health_checks_the_key_and_sends_no_picture(self):
        self.fake.json_route("/api/v1/key", {"data": {"label": "x", "limit_remaining": 4.5}})
        health = self.backend().health()
        self.assertTrue(health.ok, health.detail)
        self.assertIn("4.50 credits", health.detail)
        self.assertEqual(self.fake.requests[0]["method"], "GET")
        self.assertEqual(self.fake.requests[0]["body"], b"")
        self.fake.json_route("/v1/models", {"data": [{"id": "other"}]})
        health = self.backend("openai", model="gpt-image-1.5").health()
        self.assertTrue(health.ok)
        self.assertIn("does not list gpt-image-1.5", health.detail)


# ----------------------------------------------------------- compositing ----


class CompositeTests(CloudTestCase):
    def test_pixels_outside_the_selection_never_change(self):
        # the service returns a different size and a fully red picture
        self.fake.json_route("/api/v1/images", {"data": [{"b64_json": b64(solid_png(96, 64, (255, 0, 0, 255)))}]})
        request, rgba, mask = inpaint_request()
        out = imaging.decode_png(self.backend().inpaint(request).images[0])
        self.assertEqual((out.width, out.height), (300, 200))
        # the result is a layer: fully transparent wherever the selection is not
        opaque_outside = sum(1 for i in range(300 * 200) if mask[i] < 128 and out.data[i * 4 + 3])
        self.assertEqual(opaque_outside, 0)
        centre = (100 * 300 + 160) * 4
        self.assertEqual(out.data[centre : centre + 4], b"\xff\x00\x00\xff")
        edge = (100 * 300 + 100) * 4  # first selected column: fades in, not a hard line
        self.assertTrue(0 < out.data[edge + 3] < 255)
        # placed over the original it reproduces the blended result, and the original outside
        merged = imaging.composite(
            imaging.Rgba(300, 200, rgba),
            imaging.decode_png(solid_png(96, 64, (255, 0, 0, 255))),
            mask,
            (0, 0, 96, 64),
            4,
        )
        self.assertEqual(merged.data[:4], rgba[:4])

    def test_padding_is_cut_away_before_compositing(self):
        # 250 x 200 is padded to 250 x 200 (5:4); 260 x 200 to 267 x 200 (4:3), 3 px left
        width, height = 260, 200
        pad = imaging.pad_plan(width, height, ("4:3",))
        self.assertEqual((pad.width, pad.height, pad.left, pad.top), (267, 200, 3, 0))
        # returned picture: left padding green, the rest blue, at twice the size
        gen = bytearray()
        for _y in range(400):
            for x in range(534):
                gen += b"\x00\xff\x00\xff" if x < 6 else b"\x00\x00\xff\xff"
        self.fake.json_route("/api/v1/images", {"data": [{"b64_json": b64(png.encode(534, 400, bytes(gen), 4))}]})
        rgba = bytes(width * height * 4)
        mask = b"\xff" * (width * height)  # all selected
        request = InpaintRequest(
            png.encode(width, height, rgba, 4),
            png.encode(width, height, mask, 1),
            width,
            height,
            width,
            height,
            "x",
            "",
            ModelSpec(arch="cloud"),
            feather=1,
        )
        out = imaging.decode_png(self.backend(model="m").inpaint(request).images[0])
        self.assertEqual(out.data[4 * (100 * width + 1) : 4 * (100 * width + 1) + 3], b"\x00\x00\xff")  # no green

    def test_expand_fills_only_the_new_area(self):
        self.fake.json_route("/api/v1/images", {"data": [{"b64_json": b64(solid_png(30, 20, (0, 200, 0, 255)))}]})
        request, _rgba, _mask = inpaint_request(mode="expand", prompt="", box=(0, 0, 60, 200))
        out = imaging.decode_png(self.backend().outpaint(request).images[0])
        body = json.loads(self.fake.requests[0]["body"])
        self.assertIn("new, empty canvas", body["prompt"])
        self.assertEqual(out.data[(100 * 300 + 200) * 4 + 3], 0)  # the old picture: untouched (transparent)
        self.assertEqual(out.data[(100 * 300 + 20) * 4 :][:4], b"\x00\xc8\x00\xff")
        # the new area reaches the canvas edge: solid there, not fading out
        self.assertEqual(out.data[(100 * 300 + 0) * 4 :][:4], b"\x00\xc8\x00\xff")
        self.assertEqual(out.data[(0 * 300 + 30) * 4 :][:4], b"\x00\xc8\x00\xff")

    def test_jpeg_needs_the_editors_reader(self):
        # a JPEG header (APP0, then a baseline frame of 4 x 4) is enough: the reader is a stand-in
        jpeg = b"\xff\xd8\xff\xe0\x00\x04ab\xff\xc0\x00\x11\x08\x00\x04\x00\x04\x03" + b"\x00" * 64
        self.fake.json_route("/api/v1/images", {"data": [{"b64_json": b64(jpeg)}]})
        request, _rgba, _mask = inpaint_request()
        with self.assertRaises(CloudRefused) as caught:
            self.backend().inpaint(request)
        self.assertIn("JPEG", str(caught.exception))
        seen = []

        def decode(data):
            seen.append(data)
            return 4, 4, b"\x10\x20\x30\xff" * 16

        out = imaging.decode_png(self.backend(decode=decode).inpaint(request).images[0])
        self.assertEqual(seen, [jpeg])
        self.assertEqual(out.data[(100 * 300 + 160) * 4 :][:4], b"\x10\x20\x30\xff")

    def test_no_picture_in_the_answer(self):
        self.fake.json_route("/api/v1/images", {"data": []})
        request, _rgba, _mask = inpaint_request()
        with self.assertRaises(CloudRefused) as caught:
            self.backend().inpaint(request)
        self.assertIn("without a picture", str(caught.exception))


# --------------------------------------------------------------- errors ----


class ErrorTests(CloudTestCase):
    def fail_with(self, status, payload, headers=None):
        self.fake.json_route("/api/v1/images", payload, status, headers)
        request, _rgba, _mask = inpaint_request()
        with self.assertRaises(CloudError) as caught:
            self.backend().inpaint(request)
        err = caught.exception
        text = describe_error(err)
        self.assertNoKey(text, str(err), "".join(traceback.format_exception(type(err), err, err.__traceback__)))
        self.assertIsNone(err.__cause__)
        return err, text

    def test_401(self):
        err, text = self.fail_with(401, {"error": {"code": 401, "message": f"No auth credentials found {KEY}"}})
        self.assertIsInstance(err, CloudAuthError)
        self.assertIn("did not accept your API key", text)
        self.assertIn("openrouter.ai/keys", text)

    def test_402_out_of_credits(self):
        err, text = self.fail_with(402, {"error": {"code": 402, "message": "Insufficient credits"}})
        self.assertIsInstance(err, CloudCreditsError)
        self.assertIn("out of credits", text)
        self.assertIn("openrouter.ai/settings/credits", text)
        self.assertIn("Nothing was changed", text)

    def test_429(self):
        err, text = self.fail_with(429, {"error": {"code": 429, "message": "slow down"}}, {"Retry-After": "20"})
        self.assertIsInstance(err, CloudRateLimited)
        self.assertIn("Wait about 20 seconds", text)

    def test_403_moderation(self):
        payload = {"error": {"code": 403, "message": "Input flagged", "metadata": {"reasons": ["violence"]}}}
        err, text = self.fail_with(403, payload)
        self.assertIsInstance(err, CloudRefused)
        self.assertIn("flagged", text)
        self.assertIn("violence", text)

    def test_404_names_the_model(self):
        _err, text = self.fail_with(404, {"error": {"code": 404, "message": "No endpoints found"}})
        self.assertIn("test/image-model", text)

    def test_502_and_an_error_inside_a_200(self):
        err, text = self.fail_with(502, {"error": {"code": 502, "message": "provider down"}})
        self.assertIsInstance(err, CloudUnreachable)
        self.assertIn("not working right now", text)
        err, text = self.fail_with(200, {"error": {"code": 402, "message": "Insufficient credits"}})
        self.assertIsInstance(err, CloudCreditsError)

    def test_html_error_page_is_not_shown(self):
        self.fake.routes["/api/v1/images"] = lambda _h, _b: (
            500,
            {"Content-Type": "text/html"},
            b"<html><body>oops</body></html>",
        )
        request, _rgba, _mask = inpaint_request()
        with self.assertRaises(CloudUnreachable) as caught:
            self.backend().inpaint(request)
        self.assertNotIn("<html", str(caught.exception))

    def test_timeout(self):
        def slow(_h, _b):
            time.sleep(2)
            return 200, {}, b"{}"

        self.fake.routes["/api/v1/images"] = slow
        request, _rgba, _mask = inpaint_request()
        started = time.monotonic()
        with self.assertRaises(CloudTimeout) as caught:
            self.backend(request_timeout=0.5).inpaint(request)
        self.assertLess(time.monotonic() - started, 1.8)
        self.assertIn("did not answer within", describe_error(caught.exception))
        self.assertIn("Nothing was changed", str(caught.exception))

    def test_unreachable(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        backend = CloudBackend("openai", KEY, "m", f"http://127.0.0.1:{port}/v1", logger=self.logger)
        request, _rgba, _mask = inpaint_request()
        with self.assertRaises(CloudUnreachable) as caught:
            backend.inpaint(request)
        self.assertIn("Could not reach 127.0.0.1", str(caught.exception))
        self.assertIn("internet connection", str(caught.exception))

    def test_missing_key_or_model_is_said_before_sending(self):
        request, _rgba, _mask = inpaint_request()
        with self.assertRaises(CloudAuthError) as caught:
            self.backend(api_key="").inpaint(request)
        self.assertIn("No API key", str(caught.exception))
        with self.assertRaises(CloudRefused) as caught:
            CloudBackend("openai", KEY, "", self.fake.url + "/v1").inpaint(request)
        self.assertIn("No model", str(caught.exception))
        self.assertEqual(self.fake.requests, [])

    def test_cancel_stops_waiting(self):
        def slow(_h, _b):
            time.sleep(1.5)
            return 200, {}, b"{}"

        self.fake.routes["/api/v1/images"] = slow
        request, _rgba, _mask = inpaint_request()
        started = time.monotonic()
        with self.assertRaises(GenerationCancelled) as caught:
            self.backend().inpaint(request, cancel=lambda: time.monotonic() - started > 0.3)
        self.assertLess(time.monotonic() - started, 1.2)
        self.assertIn("may still finish", str(caught.exception))

    def test_redirect_is_not_followed_with_the_key(self):
        other = FakeService()
        self.addCleanup(other.close)
        other.json_route("/elsewhere", {"data": []})
        self.fake.routes["/api/v1/images"] = lambda _h, _b: (307, {"Location": other.url + "/elsewhere"}, b"")
        request, _rgba, _mask = inpaint_request()
        with self.assertRaises(CloudError):
            self.backend().inpaint(request)
        self.assertEqual(other.requests, [])

    def test_plain_http_only_on_this_computer_or_network(self):
        self.assertEqual(url_problem("https://openrouter.ai/api/v1"), "")
        self.assertEqual(url_problem("http://127.0.0.1:4000/v1"), "")
        self.assertEqual(url_problem("http://localhost:4000"), "")
        self.assertEqual(url_problem("http://192.168.1.20:4000/v1"), "")
        self.assertEqual(url_problem("http://[::1]:4000/v1"), "")
        self.assertEqual(url_problem("http://localhost:4000/v1", resolve=True), "")
        # names are trusted only when every address they resolve to is local, checked at request time
        from unittest import mock

        from openfill.backends import cloud

        def lookup(addresses):
            def fake(host, port):
                if addresses is None:
                    raise OSError("no such name")
                return [(2, 1, 6, "", (a, 0)) for a in addresses]

            return mock.patch.object(cloud.socket, "getaddrinfo", fake)

        for url in ("http://litellm:4000/v1", "http://proxy.lan/v1", "http://api.internal/v1"):
            self.assertIn("unencrypted", url_problem(url))  # a name alone is never enough
            with lookup(None):
                self.assertIn("unencrypted", url_problem(url, resolve=True))
            with lookup(["10.0.0.5", "8.8.8.8"]):  # one public address is enough to refuse
                self.assertIn("unencrypted", url_problem(url, resolve=True))
            with lookup(["10.0.0.5", "fd00::5"]):
                self.assertEqual(url_problem(url, resolve=True), "")
        self.assertIn("unencrypted", url_problem("http://example.com/v1"))
        self.assertIn("unencrypted", url_problem("http://8.8.8.8/v1"))
        self.assertIn("not a web address", url_problem("openrouter.ai"))
        self.assertIn("user name or password", url_problem("https://user:hunter2@litellm.example.com/v1"))
        self.assertNotIn("hunter2", url_problem("http://user:hunter2@example.com/v1"))
        request, _rgba, _mask = inpaint_request()
        with self.assertRaises(CloudRefused):
            CloudBackend("openai", KEY, "m", "http://example.com/v1").inpaint(request)


# ------------------------------------------------------------ key safety ----


class KeySafetyTests(CloudTestCase):
    def test_key_never_reaches_the_log_file(self):
        from openfill.log import get_logger

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "openfill.log"
            logger = get_logger(path, name=f"openfill-keytest-{id(self)}", debug=True)
            self.fake.json_route("/api/v1/images", {"data": [{"b64_json": b64(solid_png(8, 8, (1, 1, 1, 255)))}]})
            request, _rgba, _mask = inpaint_request()
            backend = CloudBackend("openrouter", KEY, "m", self.fake.url + "/api/v1", logger=logger)
            backend.inpaint(request)
            self.fake.json_route("/api/v1/images", {"error": {"code": 401, "message": f"bad key {KEY}"}}, 401)
            try:
                backend.inpaint(request)
            except CloudError as err:
                logger.warning("job failed: %s\n%s", err, traceback.format_exc())
            logger.info("settings %s", Settings(backend="cloud", cloud_api_key=KEY))
            logger.info("a stray line with the key %s and Bearer %s", KEY, KEY)  # the filter is the last guard
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)
            text = path.read_text(encoding="utf-8")
        self.assertIn("cloud:", text)
        self.assertNoKey(text)

    def test_settings_keep_the_key_owner_only_and_out_of_repr(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            s = Settings(backend="cloud", cloud_api_key=KEY, cloud_model="x/y")
            self.assertNoKey(repr(s))
            s.save(path)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            again = Settings.load(path)
            self.assertEqual(again.cloud_api_key, KEY)
            self.assertEqual(again.backend, "cloud")
            self.assertNoKey(repr(again), repr(CloudBackend("openrouter", KEY)))

    def test_redact_patterns(self):
        self.assertNoKey(redact(f"x {KEY} y"), redact(f'{{"cloud_api_key": "{KEY}"}}'), redact(f"api_key={KEY}"))
        self.assertNotIn("sk-proj-abcdefghijklmnopqrstuv", redact("sk-proj-abcdefghijklmnopqrstuv"))


# -------------------------------------------------------------- the notes ----


class NoteTests(unittest.TestCase):
    def test_note_names_who_gets_the_image(self):
        self.assertEqual(cloud_note("openrouter"), "Cloud: your image is sent to OpenRouter.")
        self.assertEqual(destination("openai", "http://localhost:4000/v1"), "localhost:4000")
        self.assertEqual(destination("openai"), "api.openai.com")
        s = Settings(backend="cloud", cloud_provider="openai", cloud_url="https://llm.example.org/v1")
        self.assertEqual(s.cloud_note(), "Cloud: your image is sent to llm.example.org.")
        self.assertEqual(Settings().cloud_note(), "")

    def test_backend_choices(self):
        ids = [b.id for b in BACKENDS]
        self.assertEqual(ids[0], "comfyui")  # local stays the default
        self.assertEqual(Settings().backend, "comfyui")
        self.assertEqual([b.id for b in BACKENDS if b.online], ["cloud"])


# ---------------------------------------------------------------- imaging ----


def _encode_filtered(width, height, rows, color_type, depth, channels, filters, extra=b""):
    """A PNG whose scanlines use the given filter types (to test every unfilter path)."""
    bpp = max(1, channels * depth // 8)
    stride = len(rows[0])
    raw = bytearray()
    prior = bytes(stride)
    for y, row in enumerate(rows):
        kind = filters[y % len(filters)]
        out = bytearray(stride)
        for i in range(stride):
            left = row[i - bpp] if i >= bpp else 0
            up = prior[i]
            ul = prior[i - bpp] if i >= bpp else 0
            pred = (0, left, up, (left + up) >> 1, imaging._paeth(left, up, ul))[kind]
            out[i] = (row[i] - pred) & 0xFF
        raw += bytes([kind]) + out
        prior = row

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, depth, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + extra
        + chunk(b"IDAT", zlib.compress(bytes(raw)))
        + chunk(b"IEND", b"")
    )


class ImagingTests(unittest.TestCase):
    def test_decode_every_filter_and_colour_type(self):
        rng = random.Random(3)
        w, h = 7, 5
        rgba = bytes(rng.randrange(256) for _ in range(w * h * 4))
        rows = [rgba[y * w * 4 : (y + 1) * w * 4] for y in range(h)]
        decoded = imaging.decode_png(_encode_filtered(w, h, rows, 6, 8, 4, [0, 1, 2, 3, 4]))
        self.assertEqual(decoded.data, rgba)
        rgb = bytes(b for i, b in enumerate(rgba) if i % 4 != 3)
        rows = [rgb[y * w * 3 : (y + 1) * w * 3] for y in range(h)]
        self.assertEqual(
            imaging.decode_png(_encode_filtered(w, h, rows, 2, 8, 3, [4, 3, 2, 1])).data,
            imaging.to_rgba(w, h, rgb, 3).data,
        )
        gray = rgba[0::4]
        rows = [gray[y * w : (y + 1) * w] for y in range(h)]
        self.assertEqual(imaging.decode_png(_encode_filtered(w, h, rows, 0, 8, 1, [1, 4])).data[0::4], gray)
        wide = b"".join(bytes((v, 0)) for v in rgb)  # 16-bit RGB: high bytes are the values
        rows = [wide[y * w * 6 : (y + 1) * w * 6] for y in range(h)]
        self.assertEqual(
            imaging.decode_png(_encode_filtered(w, h, rows, 2, 16, 3, [2, 4])).data, imaging.to_rgba(w, h, rgb, 3).data
        )
        palette = b"\x00\x00\x00\xff\x00\x00\x00\xff\x00\x00\x00\xff"
        trns = b"\xff\x80"
        idx = [rng.randrange(3) for _ in range(w * h)]
        packed = []
        for y in range(h):  # 2 bits per pixel
            row = [*idx[y * w : (y + 1) * w], 0]
            packed.append(
                bytes(
                    (
                        row[i] << 6
                        | row[i + 1] << 4
                        | (row[i + 2] if i + 2 < len(row) else 0) << 2
                        | (row[i + 3] if i + 3 < len(row) else 0)
                    )
                    for i in range(0, w, 4)
                )
            )

        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

        decoded = imaging.decode_png(
            _encode_filtered(w, h, packed, 3, 2, 1, [0, 1], chunk(b"PLTE", palette) + chunk(b"tRNS", trns))
        )
        expect = b"".join(palette[3 * i : 3 * i + 3] + bytes([trns[i] if i < 2 else 255]) for i in idx)
        self.assertEqual(decoded.data, expect)

    def test_decode_own_encoder_and_errors(self):
        data = bytes(range(48))
        self.assertEqual(imaging.decode_png(png.encode(4, 3, data, 4)).data, data)
        with self.assertRaises(png.PngError):
            imaging.decode_png(b"not a png")
        self.assertEqual(imaging.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "webp")

    def test_shrink_and_pad(self):
        image = imaging.to_rgba(4, 2, bytes(range(8)), 1)
        small = imaging.shrink(image, 2)
        self.assertEqual((small.width, small.height), (2, 1))
        self.assertEqual(small.data[0::4], bytes([0, 2]))
        pad = imaging.Padding(4, 4, 0, 1, "1:1")
        padded = imaging.pad_edges(image, pad)
        self.assertEqual(padded.data[0::4], bytes([0, 1, 2, 3, 0, 1, 2, 3, 4, 5, 6, 7, 4, 5, 6, 7]))
        self.assertEqual(
            imaging.pad_gray(b"\x01\x02", 2, 1, imaging.Padding(4, 3, 1, 1, "4:3")),
            b"\x00" * 4 + b"\x00\x01\x02\x00" + b"\x00" * 4,
        )
        self.assertEqual(imaging.nearest_ratio(1920, 1080, ("1:1", "16:9", "4:3")), "16:9")

    def test_composite_is_exact_outside_and_ramps_inside(self):
        w, h = 20, 10
        original = imaging.Rgba(w, h, bytes([50, 60, 70, 255]) * (w * h))
        generated = imaging.Rgba(2, 1, bytes([200, 0, 0, 255]) * 2)
        mask = bytearray(w * h)
        for y in range(2, 8):
            mask[y * w + 4 : y * w + 16] = b"\xff" * 12
        mask[0] = 100  # a soft pixel below 128 stays original
        out = imaging.composite(original, generated, bytes(mask), (0, 0, 2, 1), feather=3)
        for i in range(w * h):
            pixel = out.data[i * 4 : i * 4 + 4]
            if mask[i] < 128:
                self.assertEqual(pixel, bytes([50, 60, 70, 255]))
        centre = out.data[(5 * w + 10) * 4 :][:4]
        self.assertEqual(centre, bytes([200, 0, 0, 255]))
        rim = out.data[(5 * w + 4) * 4 :][:4]
        self.assertTrue(50 < rim[0] < 200)


# ---------------------------------------------------- the App: no fallback ----


class FakeImage:
    def __init__(self, width, height):
        self.width, self.height = width, height

    def get_width(self):
        return self.width

    def get_height(self):
        return self.height

    def undo_group_start(self):
        pass

    def undo_group_end(self):
        pass


class FakeRunner:
    def __init__(self, app, title, model_name, interactive=True):
        self.model_name = model_name

    def run(self, job):
        return job(lambda f, t: None, None, lambda: False)

    def next_round(self):
        pass

    def done(self, message="Done"):
        pass

    def fail(self, error):
        pass


class AppNoFallbackTests(unittest.TestCase):
    """Local and cloud never stand in for each other."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cloud = FakeService()
        self.addCleanup(self.cloud.close)
        self.comfy = FakeService()  # a "ComfyUI" that would answer anything
        self.addCleanup(self.comfy.close)
        self.profile = Path(self.tmp.name)
        (self.profile / "openfill").mkdir()
        self.inserted = []

    def app(self, **config):
        from openfill import app as app_mod

        base = {
            "comfy_dir": str(self.profile / "no-comfy"),
            "url": self.comfy.url,
            "cloud_url": self.cloud.url + "/api/v1",
            "cloud_api_key": KEY,
            "cloud_key_for": key_scope("openrouter", self.cloud.url + "/api/v1"),
            "seam_correction": False,
            "grain_match": False,
            "rank_variations": False,
            "autostart": False,
        }
        base.update(config)
        (self.profile / "openfill" / "config.json").write_text(json.dumps(base), encoding="utf-8")
        rgba, mask = context()

        def insert(image, images, rect, name, feather, temp, **kw):
            self.inserted.append((images, rect, name))
            return [SimpleNamespace(get_name=lambda: name)]

        io_ = SimpleNamespace(
            profile_dir=lambda: str(self.profile),
            message=lambda _t: None,
            is_indexed=lambda _i: False,
            selection_rect=lambda _i: Rect(100, 50, 120, 100),
            image_rect=lambda _i: Rect(0, 0, 300, 200),
            export_region=lambda _i, r, transparent_outside=False: self._crop(rgba, r, 4),
            export_selection_mask=lambda _i, r: self._crop(mask, r, 1),
            insert_results=insert,
            mask_png_from_bytes=lambda r, p: png.encode(r.width, r.height, p, 1),
            resize_canvas=lambda *a: None,
        )
        app = app_mod.App(interactive=False, io=io_, runner_cls=FakeRunner)
        self.addCleanup(self._close_log, app)
        return app

    @staticmethod
    def _close_log(app):
        for handler in list(app.log.handlers):
            handler.close()
            app.log.removeHandler(handler)

    @staticmethod
    def _crop(data, rect, channels):
        out = bytearray()
        for y in range(rect.y, rect.y + rect.height):
            for x in range(rect.x, rect.x + rect.width):
                xx, yy = min(max(x, 0), 299), min(max(y, 0), 199)
                out += data[(yy * 300 + xx) * channels : (yy * 300 + xx + 1) * channels]
        raw = bytes(out)
        return png.encode(rect.width, rect.height, raw, channels), raw

    def test_cloud_fill_end_to_end_without_comfyui(self):
        self.cloud.json_route("/api/v1/images", {"data": [{"b64_json": b64(solid_png(64, 64, (255, 0, 0, 255)))}]})
        app = self.app(backend="cloud")
        app.server.ensure_running = lambda *a, **k: self.fail("ComfyUI must not be started for a cloud job")
        app.generative_fill(FakeImage(300, 200), "a red ball", "", 1, 1.0, -1, "active")
        self.assertEqual(len(self.cloud.requests), 1)
        self.assertEqual(self.comfy.requests, [])
        images, rect, name = self.inserted[0]
        self.assertEqual(png.read_header(images[0])[:2], (rect.width, rect.height))
        self.assertTrue(name.startswith("Generative Fill"))
        self.assertTrue(app.has_model_for("remove"))
        self.assertIsNone(app.expand_suggestion("active"))
        app.generative_fill(FakeImage(300, 200), "", "", 1, 1.0, -1, "lama-big", remove=True, as_picked=True)
        body = json.loads(self.cloud.requests[1]["body"])
        self.assertIn("Remove what is in", body["prompt"])
        log = (self.profile / "openfill" / "openfill.log").read_text(encoding="utf-8")
        self.assertNotIn(KEY, log)

    def test_cloud_failure_never_falls_back_to_comfyui(self):
        self.cloud.json_route("/api/v1/images", {"error": {"code": 402, "message": "Insufficient credits"}}, 402)
        app = self.app(backend="cloud")
        app.server.ensure_running = lambda *a, **k: self.fail("no fallback to ComfyUI")
        with self.assertRaises(CloudCreditsError):
            app.generative_fill(FakeImage(300, 200), "a red ball", "", 1, 1.0, -1, "active")
        self.assertEqual(self.comfy.requests, [])
        self.assertEqual(self.inserted, [])

    def test_comfyui_failure_never_falls_back_to_the_cloud(self):
        from openfill.backend import BackendUnavailable

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead = f"http://127.0.0.1:{s.getsockname()[1]}"
        app = self.app(backend="comfyui", url=dead)  # a cloud key is set, but local is picked
        with self.assertRaises(BackendUnavailable):
            app.backend()
        with self.assertRaises(Exception) as caught:
            app.generative_fill(FakeImage(300, 200), "a red ball", "", 1, 1.0, -1, "active")
        self.assertNotIsInstance(caught.exception, CloudError)
        self.assertEqual(self.cloud.requests, [])

    def test_check_backend_for_the_cloud_sends_no_picture(self):
        self.cloud.json_route("/api/v1/key", {"data": {"limit_remaining": None}})
        app = self.app(backend="cloud")
        app.server.ensure_running = lambda *a, **k: self.fail("no ComfyUI")
        health = app.check_backend()
        self.assertTrue(health.ok, health.detail)
        self.assertEqual([r["path"] for r in self.cloud.requests], ["/api/v1/key"])


# ------------------------------------------------- review findings (2026-11-06) ----


def _chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def bomb_png(inflated=200_000_000):
    """A 1x1 RGBA PNG whose data inflates to ``inflated`` bytes (about 200 KB on the wire)."""
    comp = zlib.compressobj(9)
    block = b"\0" * 10_000_000
    data = b"".join(comp.compress(block) for _ in range(inflated // len(block))) + comp.flush()
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", data) + _chunk(b"IEND", b"")


class CostTests(CloudTestCase):
    def chat_route(self, delay):
        url = "data:image/png;base64," + b64(solid_png(30, 20, (0, 255, 0, 255)))
        answer = json.dumps({"choices": [{"message": {"images": [{"image_url": {"url": url}}]}}]}).encode()

        def slow(_h, _b):
            time.sleep(delay)
            return 200, {"Content-Type": "application/json"}, answer

        self.fake.routes["/api/v1/chat/completions"] = slow

    def test_cancel_in_chat_mode_sends_no_further_paid_requests(self):
        self.chat_route(0.5)
        request, _rgba, _mask = inpaint_request(batch=4)
        started = time.monotonic()
        with self.assertRaises(GenerationCancelled):
            self.backend(api="chat").inpaint(request, cancel=lambda: time.monotonic() - started > 0.2)
        time.sleep(1.5)  # long enough for a worker that went on to send the next three
        self.assertEqual(len(self.fake.requests), 1, "paid requests sent after Cancel")

    def test_pictures_already_paid_for_are_kept_when_a_later_one_fails(self):
        url = "data:image/png;base64," + b64(solid_png(30, 20, (0, 255, 0, 255)))
        ok = json.dumps({"choices": [{"message": {"images": [{"image_url": {"url": url}}]}}]}).encode()
        calls = []

        def route(_h, _b):
            calls.append(1)
            if len(calls) == 1:
                return 200, {}, ok
            return 502, {}, b'{"error": {"code": 502, "message": "down"}}'

        self.fake.routes["/api/v1/chat/completions"] = route
        request, _rgba, _mask = inpaint_request(batch=3)
        self.assertEqual(len(self.backend(api="chat").inpaint(request).images), 1)

    def test_one_deadline_for_the_whole_job(self):
        def drip(handler, _b):  # answers a byte at a time, each within the per-read timeout
            handler.send_response(200)
            handler.send_header("Content-Length", "100")
            handler.end_headers()
            try:
                for _ in range(100):
                    handler.wfile.write(b" ")
                    handler.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass  # the client gave up, as it should
            handler.close_connection = True
            return 200, {}, b""

        self.fake.routes["/api/v1/images"] = drip
        request, _rgba, _mask = inpaint_request()
        started = time.monotonic()
        with self.assertRaises(CloudTimeout):
            self.backend(request_timeout=1.0).inpaint(request)
        self.assertLess(time.monotonic() - started, 4.0)


class ResponseTests(CloudTestCase):
    def test_png_inflating_past_its_header_is_refused(self):
        data = bomb_png()
        tracemalloc.start()
        try:
            with self.assertRaises(png.PngError):
                imaging.decode_png(data)
            _now, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 50_000_000, f"decode_png peaked at {peak / 1e6:.0f} MB for a 1x1 picture")

    def test_png_bomb_from_the_service_is_not_accepted(self):
        self.fake.json_route("/api/v1/images", {"data": [{"b64_json": b64(bomb_png(50_000_000))}]})
        request, _rgba, _mask = inpaint_request()
        with self.assertRaises(CloudRefused):
            self.backend().inpaint(request)

    def test_huge_picture_sizes_are_refused_before_decoding(self):
        ihdr = struct.pack(">IIBBBBB", 20000, 20000, 8, 6, 0, 0, 0)
        big = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(b"")) + _chunk(b"IEND", b"")
        self.fake.json_route("/api/v1/images", {"data": [{"b64_json": b64(big)}]})
        request, _rgba, _mask = inpaint_request()
        with self.assertRaises(CloudRefused) as caught:
            self.backend().inpaint(request)
        self.assertIn("larger than OpenFill accepts", str(caught.exception))

    def test_answers_over_the_size_limit_are_refused(self):
        from openfill.backends import cloud

        old, cloud.MAX_ANSWER_BYTES = cloud.MAX_ANSWER_BYTES, 1000
        self.addCleanup(setattr, cloud, "MAX_ANSWER_BYTES", old)
        rng = random.Random(5)
        noise = bytes(rng.randrange(256) for _ in range(64 * 64 * 4))
        self.fake.json_route("/api/v1/images", {"data": [{"b64_json": b64(png.encode(64, 64, noise, 4))}]})
        request, _rgba, _mask = inpaint_request()
        with self.assertRaises(CloudRefused) as caught:
            self.backend().inpaint(request)
        self.assertIn("more than OpenFill accepts", str(caught.exception))

    def test_error_with_a_string_code_inside_a_200_is_a_plain_cloud_error(self):
        self.fake.json_route("/api/v1/images", {"error": {"code": "content_policy_violation", "message": "no"}})
        request, _rgba, _mask = inpaint_request()
        with self.assertRaises(CloudRefused) as caught:
            self.backend().inpaint(request)
        self.assertIn("refused", str(caught.exception))

    def test_odd_answers_are_plain_cloud_errors(self):
        for payload in (
            {"data": ["not-an-object"]},
            {"data": [{"b64_json": "@@@not base64"}]},
            {"data": {"x": 1}},
            [1, 2, 3],
            {"data": [{"b64_json": b64(b"GIF89a....")}]},
        ):
            with self.subTest(payload=payload):
                self.fake.json_route("/api/v1/images", payload)
                request, _rgba, _mask = inpaint_request()
                with self.assertRaises(CloudError):
                    self.backend().inpaint(request)

    def test_unknown_content_never_reaches_the_editors_reader(self):
        seen = []
        eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 4 4\nshowpage\n"
        for data in (eps, b"RIFF\x00\x00\x00\x00WAVEfmt ", b"BM" + b"\0" * 60):
            with self.subTest(data=data[:8]):
                self.fake.json_route("/api/v1/images", {"data": [{"b64_json": b64(data)}]})
                request, _rgba, _mask = inpaint_request()
                with self.assertRaises(CloudRefused):
                    self.backend(decode=lambda d: seen.append(d)).inpaint(request)
        self.assertEqual(seen, [])

    def test_create_results_are_always_written_again_as_png(self):
        self.fake.json_route("/api/v1/images", {"data": [{"b64_json": b64(bomb_png(20_000_000))}]})
        with self.assertRaises(CloudRefused):
            self.backend().txt2img(Txt2ImgRequest(512, 512, "x", "", ModelSpec(), Sampling()))

    def test_picture_sizes_from_headers(self):
        jpeg = b"\xff\xd8\xff\xe0\x00\x04ab\xff\xc0\x00\x11\x08\x01\x00\x02\x00\x03" + b"\0" * 20
        self.assertEqual(imaging.picture_size(jpeg, "jpeg"), (512, 256))
        lossy = b"RIFF\0\0\0\0WEBPVP8 \0\0\0\0\0\0\0\x9d\x01\x2a" + struct.pack("<HH", 640, 480) + b"\0" * 8
        self.assertEqual(imaging.picture_size(lossy, "webp"), (640, 480))
        extended = b"RIFF\0\0\0\0WEBPVP8X\0\0\0\0\0\0\0\0" + (99).to_bytes(3, "little") + (49).to_bytes(3, "little")
        self.assertEqual(imaging.picture_size(extended, "webp"), (100, 50))
        with self.assertRaises(png.PngError):
            imaging.picture_size(b"\xff\xd8\xff", "jpeg")


class NoteAndAddressTests(unittest.TestCase):
    def test_note_names_the_real_host_not_a_lookalike(self):
        self.assertIn("evilopenrouter.ai", cloud_note("openrouter", "https://evilopenrouter.ai/api/v1"))
        self.assertIn("openrouter.ai.example.com", cloud_note("openrouter", "https://openrouter.ai.example.com/v1"))
        self.assertEqual(
            cloud_note("openrouter", "https://eu.openrouter.ai/api/v1"), "Cloud: your image is sent to OpenRouter."
        )
        self.assertEqual(destination("openai", "http://localhost:4000/v1"), "localhost:4000")
        self.assertEqual(destination("openai", "https://LLM.Example.org:443/v1"), "llm.example.org")

    def test_note_never_shows_credentials_in_the_address(self):
        note = cloud_note("openai", "https://user:hunter2@litellm.example.com/v1")
        self.assertNotIn("hunter2", note)
        self.assertNotIn("user", note)

    def test_credentials_in_the_address_never_reach_an_error(self):
        backend = CloudBackend("openai", KEY, "m", "https://user:hunter2@127.0.0.1:9/v1")
        request, _rgba, _mask = inpaint_request()
        with self.assertRaises(CloudRefused) as caught:
            backend.inpaint(request)
        self.assertNotIn("hunter2", str(caught.exception))


class KeyDestinationTests(CloudTestCase):
    def test_saved_key_is_not_sent_to_a_new_address(self):
        from openfill.config import Settings

        s = Settings(backend="cloud")
        s.set_cloud("openrouter", "", "", KEY)
        self.assertTrue(s.cloud_key_usable)
        # later: OpenAI-compatible for a LiteLLM proxy, key field left empty
        s.set_cloud("openai", self.fake.url + "/v1", "m", "")
        self.assertEqual(s.cloud_api_key, KEY)  # still saved, for OpenRouter only
        self.assertFalse(s.cloud_key_usable)
        self.assertIn("another service or address", s.cloud_key_problem())
        self.fake.json_route("/v1/models", {"data": [{"id": "m"}]})
        backend = CloudBackend(
            s.cloud_provider,
            s.cloud_api_key if s.cloud_key_usable else "",
            s.cloud_model,
            s.cloud_url,
            logger=self.logger,
            key_problem=s.cloud_key_problem(),
        )
        health = backend.health()
        self.assertFalse(health.ok)
        self.assertIn("Paste the API key for this service", health.detail)
        self.assertEqual(self.fake.requests, [])  # nothing at all was sent there
        s.set_cloud("openai", self.fake.url + "/v1", "m", "sk-litellm-0123456789abcdef")
        self.assertTrue(s.cloud_key_usable)

    def test_scope_is_service_scheme_host_and_port(self):
        self.assertEqual(key_scope("openrouter"), "openrouter|https://openrouter.ai:443")
        self.assertEqual(key_scope("openrouter", "https://OpenRouter.ai/api/v1/"), key_scope("openrouter"))
        self.assertNotEqual(
            key_scope("openai", "http://localhost:4000/v1"), key_scope("openai", "http://localhost:4001/v1")
        )
        self.assertNotEqual(
            key_scope("openai", "https://x.example/v1"), key_scope("openrouter", "https://x.example/v1")
        )
        self.assertNotEqual(key_scope("openai", "http://x.example/v1"), key_scope("openai", "https://x.example/v1"))


class OutsideSelectionTests(AppNoFallbackTests):
    """A cloud result inserted in GIMP or PhotoCraft can only change the selection, also where
    the image is transparent (the export is flattened over the mean colour)."""

    test_cloud_fill_end_to_end_without_comfyui = None
    test_cloud_failure_never_falls_back_to_comfyui = None
    test_comfyui_failure_never_falls_back_to_the_cloud = None
    test_check_backend_for_the_cloud_sends_no_picture = None

    def test_transparent_pixels_near_the_selection_stay_transparent(self):
        from openfill import geometry

        self.cloud.json_route("/api/v1/images", {"data": [{"b64_json": b64(solid_png(64, 64, (255, 0, 0, 255)))}]})
        app = self.app(backend="cloud")
        app.server.ensure_running = lambda *a, **k: self.fail("no ComfyUI")
        w, h = 300, 200
        rgba = bytearray(b"\x00\x00\x00\x00" * (w * h))
        for y in range(h):
            for x in range(w):
                if x < 60 or y < 20:
                    rgba[(y * w + x) * 4 : (y * w + x + 1) * 4] = bytes((10, 200, 30, 255))
        rgba = bytes(rgba)
        mask = bytearray(w * h)
        for y in range(50, 150):
            mask[y * w + 100 : y * w + 220] = b"\xff" * 120
        mask = bytes(mask)
        masks = []

        def export(_i, r, transparent_outside=False):
            _png, raw = self._crop(rgba, r, 4)
            flat = geometry.flatten_over_mean(raw)  # what gimp_io.export_region does
            return png.encode(r.width, r.height, flat, 4), flat

        def insert(image, images, rect, name, feather, temp, **kw):
            self.inserted.append((images, rect, name))
            masks.append((feather, kw.get("grow", 0)))
            return []

        app.io.export_region = export
        app.io.export_selection_mask = lambda _i, r: self._crop(mask, r, 1)
        app.io.insert_results = insert
        app.generative_fill(FakeImage(w, h), "a red ball", "", 1, 1.0, -1, "active")
        images, rect, _name = self.inserted[0]
        layer = imaging.decode_png(images[0])
        self.assertEqual(masks[0], (0.0, 0))  # the layer mask is the selection itself, not grown or feathered
        for x, y in ((99, 100), (221, 100), (160, 49), (160, 150), (30, 100)):  # outside, transparent or not
            lx, ly = x - rect.x, y - rect.y
            if 0 <= lx < layer.width and 0 <= ly < layer.height:
                self.assertEqual(layer.data[(ly * layer.width + lx) * 4 + 3], 0, (x, y))
        lx, ly = 160 - rect.x, 100 - rect.y
        self.assertEqual(layer.data[(ly * layer.width + lx) * 4 : (ly * layer.width + lx) * 4 + 4], b"\xff\x00\x00\xff")


if __name__ == "__main__":
    unittest.main()
