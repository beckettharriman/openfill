# SPDX-License-Identifier: Apache-2.0
"""Resumable download logic against a local HTTP server that honours Range."""

import hashlib
import http.client
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from openfill import catalog, comfy_setup, downloads, supervisor, ui_text
from openfill.config import Settings

PAYLOAD = bytes(range(256)) * 4096  # 1 MiB, deterministic
SHA = hashlib.sha256(PAYLOAD).hexdigest()
COMMIT = "5b7681746fe19e563113d27366ba85236bc1ba5b"
REPO = "https://github.com/Acly/comfyui-inpaint-nodes"


class State:
    requests: list[dict] = []
    paths: list[str] = []
    gated = False
    ignore_range = False
    drop_after: int | None = None  # close connection after N bytes (simulates a cut)
    extra_bytes = 0  # append junk and omit Content-Length (a server sending more than the catalog size)
    range_offset_shift = 0  # answer a Range request from the wrong offset


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        State.requests.append(dict(self.headers))
        State.paths.append(self.path)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/file.bin")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if State.gated and self.headers.get("Authorization") != "Bearer secret":
            self.send_response(401)
            self.end_headers()
            return
        if self.path == "/missing":
            self.send_response(404)
            self.end_headers()
            return
        rng = self.headers.get("Range")
        start = 0
        status = 200
        if rng and not State.ignore_range:
            start = int(rng.split("=")[1].rstrip("-"))
            if start >= len(PAYLOAD):
                self.send_response(416)
                self.end_headers()
                return
            start -= State.range_offset_shift
            status = 206
        body = PAYLOAD[start:] + b"x" * State.extra_bytes
        if State.drop_after is not None:
            body = body[: State.drop_after]
        self.send_response(status)
        if not State.extra_bytes:  # without a length the client reads until the connection closes
            self.send_header("Content-Length", str(len(PAYLOAD) - start))  # full length even when we cut
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
        self.end_headers()
        self.wfile.write(body)


def https_to_local(req, timeout=60.0):
    """Opener for tests: the URL says https (so the token rules apply) but the stub is plain http."""
    original = req.full_url
    req.full_url = original.replace("https://", "http://", 1)
    resp = urlopen(req, timeout=timeout)
    resp.url = original  # as if the https request was answered directly
    return resp


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/file.bin"
        cls.https_url = cls.url.replace("http://", "https://")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        State.requests = []
        State.paths = []
        State.gated = False
        State.ignore_range = False
        State.drop_after = None
        State.extra_bytes = 0
        State.range_offset_shift = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name) / "models" / "inpaint" / "file.bin"

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_download_verified_and_atomic(self):
        events = []
        r = downloads.download_file(
            self.url, self.dest, len(PAYLOAD), SHA, progress=lambda d, t, s: events.append((d, t))
        )
        self.assertEqual(self.dest.read_bytes(), PAYLOAD)
        self.assertFalse(downloads.part_path(self.dest).exists())
        self.assertEqual(r.sha256, SHA)
        self.assertEqual(r.resumed_from, 0)
        self.assertEqual(events[-1], (len(PAYLOAD), len(PAYLOAD)))
        self.assertNotIn("Range", State.requests[0])

    def test_resume_sends_range_and_appends(self):
        part = downloads.part_path(self.dest)
        part.parent.mkdir(parents=True)
        part.write_bytes(PAYLOAD[:300_000])
        r = downloads.download_file(self.url, self.dest, len(PAYLOAD), SHA)
        self.assertEqual(State.requests[0].get("Range"), "bytes=300000-")
        self.assertEqual(r.resumed_from, 300_000)
        self.assertEqual(r.bytes_written, len(PAYLOAD) - 300_000)
        self.assertEqual(self.dest.read_bytes(), PAYLOAD)

    def test_resume_from_wrong_offset_is_rejected(self):
        State.range_offset_shift = 10
        part = downloads.part_path(self.dest)
        part.parent.mkdir(parents=True)
        part.write_bytes(PAYLOAD[:300_000])
        with self.assertRaises(downloads.DownloadError) as ctx:
            downloads.download_file(self.url, self.dest, len(PAYLOAD), SHA)
        self.assertEqual(ctx.exception.code, "network")
        self.assertFalse(part.exists())
        self.assertFalse(self.dest.exists())

    def test_server_ignoring_range_restarts_from_zero(self):
        State.ignore_range = True
        part = downloads.part_path(self.dest)
        part.parent.mkdir(parents=True)
        part.write_bytes(b"garbage" * 1000)
        r = downloads.download_file(self.url, self.dest, len(PAYLOAD), SHA)
        self.assertEqual(r.resumed_from, 0)
        self.assertEqual(self.dest.read_bytes(), PAYLOAD)

    def test_cut_connection_keeps_part_for_resume(self):
        State.drop_after = 100_000
        with self.assertRaises(downloads.DownloadError) as ctx:
            downloads.download_file(self.url, self.dest, len(PAYLOAD), SHA)
        self.assertEqual(ctx.exception.code, "size")
        self.assertEqual(downloads.part_path(self.dest).stat().st_size, 100_000)
        State.drop_after = None
        downloads.download_file(self.url, self.dest, len(PAYLOAD), SHA)
        self.assertEqual(self.dest.read_bytes(), PAYLOAD)

    def test_oversized_body_is_cut_and_part_deleted(self):
        State.extra_bytes = 4096
        with self.assertRaises(downloads.DownloadError) as ctx:
            downloads.download_file(self.url, self.dest, len(PAYLOAD), SHA)
        self.assertEqual(ctx.exception.code, "size")
        self.assertFalse(downloads.part_path(self.dest).exists())
        self.assertFalse(self.dest.exists())

    def test_size_mismatch_with_catalog_is_refused_before_writing(self):
        with self.assertRaises(downloads.DownloadError) as ctx:
            downloads.download_file(self.url, self.dest, len(PAYLOAD) + 1, SHA)
        self.assertEqual(ctx.exception.code, "size")
        self.assertFalse(downloads.part_path(self.dest).exists())

    def test_hash_mismatch_deletes_file(self):
        with self.assertRaises(downloads.DownloadError) as ctx:
            downloads.download_file(self.url, self.dest, len(PAYLOAD), "0" * 64)
        self.assertEqual(ctx.exception.code, "hash")
        self.assertFalse(self.dest.exists())
        self.assertFalse(downloads.part_path(self.dest).exists())

    def test_https_to_http_redirect_is_refused(self):
        with self.assertRaises(downloads.DownloadError) as ctx:
            downloads.download_file(
                self.https_url, self.dest, len(PAYLOAD), SHA, opener=lambda req, timeout=60.0: urlopen(self.url)
            )
        self.assertIn("unencrypted", str(ctx.exception))
        self.assertFalse(self.dest.exists())

    def test_keep_existing_never_deletes_a_foreign_file(self):
        self.dest.parent.mkdir(parents=True, exist_ok=True)
        self.dest.write_bytes(b"my own file")
        with self.assertRaises(downloads.DownloadError) as ctx:
            downloads.download_file(self.url, self.dest, len(PAYLOAD), SHA, keep_existing=True)
        self.assertEqual(ctx.exception.code, "exists")
        self.assertEqual(self.dest.read_bytes(), b"my own file")
        downloads.download_file(self.url, self.dest, len(PAYLOAD), SHA)  # bundled entries still replace it
        self.assertEqual(self.dest.read_bytes(), PAYLOAD)

    def test_plain_messages_for_busy_server_and_dropped_connection(self):
        def busy(req, timeout=60.0):
            raise HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

        with self.assertRaises(downloads.DownloadError) as ctx:
            downloads.download_file(self.url, self.dest, opener=busy)
        self.assertIn("Wait a few minutes", str(ctx.exception))
        self.assertIn("press Download again", ui_text.with_next_step(str(ctx.exception)))
        self.assertNotIn("Verbose log", ui_text.with_next_step(str(ctx.exception)))

        def cut(req, timeout=60.0):
            raise http.client.IncompleteRead(b"x")

        with self.assertRaises(downloads.DownloadError) as ctx:
            downloads.download_file(self.url, self.dest, opener=cut)
        self.assertEqual(ctx.exception.code, "network")
        self.assertIn("Stop the AI backend", downloads.plain_os_message(PermissionError(13, "denied", "x.part")))

    def test_civitai_auth_message_does_not_ask_for_a_hugging_face_token(self):
        def denied(req, timeout=60.0):
            raise HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        with self.assertRaises(downloads.DownloadError) as ctx:
            downloads.download_file("https://civitai.com/api/download/models/1", self.dest, opener=denied)
        self.assertEqual(ctx.exception.code, "auth")
        self.assertNotIn("Hugging Face", str(ctx.exception))
        self.assertNotIn("Verbose log", ui_text.with_next_step(str(ctx.exception)))
        self.assertIn("Hugging Face", downloads.auth_message("https://huggingface.co/x", 401))

    def test_gated_without_token_and_with_token(self):
        State.gated = True
        hosts = ("127.0.0.1",)
        with self.assertRaises(downloads.DownloadError) as ctx:
            downloads.download_file(
                self.https_url, self.dest, len(PAYLOAD), SHA, opener=https_to_local, token_hosts=hosts
            )
        self.assertEqual(ctx.exception.code, "auth")
        downloads.download_file(
            self.https_url, self.dest, len(PAYLOAD), SHA, token="secret", opener=https_to_local, token_hosts=hosts
        )
        self.assertTrue(self.dest.exists())
        self.assertEqual(State.requests[-1].get("Authorization"), "Bearer secret")
        self.assertNotIn("secret", "".join(State.paths))  # never in the URL

    def test_token_not_sent_to_other_hosts(self):
        # Default token hosts are Hugging Face only, so the stub host never gets it.
        downloads.download_file(self.https_url, self.dest, len(PAYLOAD), SHA, token="secret", opener=https_to_local)
        self.assertNotIn("Authorization", State.requests[0])

    def test_token_not_sent_over_plain_http(self):
        downloads.download_file(self.url, self.dest, len(PAYLOAD), SHA, token="secret", token_hosts=("127.0.0.1",))
        self.assertNotIn("Authorization", State.requests[0])

    def test_token_not_forwarded_on_redirect(self):
        url = self.https_url.replace("file.bin", "redirect")
        downloads.download_file(
            url, self.dest, len(PAYLOAD), SHA, token="secret", opener=https_to_local, token_hosts=("127.0.0.1",)
        )
        self.assertEqual(State.paths, ["/redirect", "/file.bin"])
        self.assertEqual(State.requests[0].get("Authorization"), "Bearer secret")
        self.assertNotIn("Authorization", State.requests[1])
        self.assertEqual(self.dest.read_bytes(), PAYLOAD)

    def test_not_found(self):
        with self.assertRaises(downloads.DownloadError) as ctx:
            downloads.download_file(self.url.replace("file.bin", "missing"), self.dest)
        self.assertEqual(ctx.exception.code, "notfound")

    def test_existing_good_file_is_skipped(self):
        self.dest.parent.mkdir(parents=True)
        self.dest.write_bytes(PAYLOAD)
        r = downloads.download_file(self.url, self.dest, len(PAYLOAD), SHA)
        self.assertTrue(r.skipped)
        self.assertEqual(State.requests, [])

    def test_existing_wrong_size_is_replaced(self):
        self.dest.parent.mkdir(parents=True)
        self.dest.write_bytes(b"old")
        downloads.download_file(self.url, self.dest, len(PAYLOAD), SHA)
        self.assertEqual(self.dest.read_bytes(), PAYLOAD)

    def test_cancel(self):
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 1

        with self.assertRaises(downloads.DownloadError) as ctx:
            downloads.download_file(self.url, self.dest, len(PAYLOAD), SHA, cancel=cancel)
        self.assertEqual(ctx.exception.code, "cancelled")
        self.assertFalse(self.dest.exists())

    def test_unreachable(self):
        with self.assertRaises(downloads.DownloadError) as ctx:
            downloads.download_file("http://127.0.0.1:1/x", self.dest)
        self.assertEqual(ctx.exception.code, "network")
        # rob-download-network-off: plain words and a next step, no urlopen/WinError dump
        self.assertIn("Could not reach 127.0.0.1", str(ctx.exception))
        self.assertIn("press Download again", str(ctx.exception))
        self.assertNotIn("urlopen", str(ctx.exception))

    def test_corrupt_part_is_replaced_by_one_fresh_download(self):
        # rob-download-corrupt-part: a .part holding garbage resumes, fails the checksum, and is then
        # downloaded once more from zero instead of failing the whole task
        part = downloads.part_path(self.dest)
        part.parent.mkdir(parents=True)
        part.write_bytes(b"#garbage" * 1000)
        r = downloads.download_file(self.url, self.dest, len(PAYLOAD), SHA)
        self.assertEqual(self.dest.read_bytes(), PAYLOAD)
        self.assertEqual(State.requests[0].get("Range"), "bytes=8000-")
        self.assertNotIn("Range", State.requests[-1])
        self.assertEqual(r.resumed_from, 0)
        self.assertFalse(part.exists())

    # rob-disk-nearly-full (no free-space override exists, so this is checked here, not end to end)
    def test_disk_nearly_full_refuses_before_writing(self):
        from collections import namedtuple
        from unittest import mock

        usage = namedtuple("usage", "total used free")(10**9, 10**9 - 1000, 1000)
        with (
            mock.patch.object(downloads.shutil, "disk_usage", return_value=usage),
            self.assertRaises(downloads.DownloadError) as ctx,
        ):
            downloads.download_file(self.url, self.dest, len(PAYLOAD), SHA)
        self.assertEqual(ctx.exception.code, "disk")
        self.assertIn("Free up space", str(ctx.exception))
        self.assertIn("1.0 KB is free", str(ctx.exception))
        self.assertFalse(downloads.part_path(self.dest).exists())
        self.assertFalse(self.dest.exists())

    def test_disk_full_while_writing_keeps_part(self):
        import errno as errno_
        from unittest import mock

        real_open = open
        calls = {"n": 0}

        class Full:
            def __init__(self, fh):
                self.fh = fh

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                self.fh.close()

            def write(self, block):
                calls["n"] += 1
                self.fh.write(block[:1000])  # part of it lands, then the disk is full
                raise OSError(errno_.ENOSPC, "No space left on device")

        def fake_open(path, mode="r", *a, **k):
            fh = real_open(path, mode, *a, **k)
            return Full(fh) if str(path).endswith(".part") and "b" in mode and "r" not in mode else fh

        with mock.patch("builtins.open", fake_open), self.assertRaises(downloads.DownloadError) as ctx:
            downloads.download_file(self.url, self.dest, len(PAYLOAD), SHA)
        self.assertEqual(ctx.exception.code, "disk")
        self.assertIn("press Download again", str(ctx.exception))
        self.assertTrue(downloads.part_path(self.dest).exists())  # resumable once space is free
        self.assertFalse(self.dest.exists())


class TokenHeaderTests(unittest.TestCase):
    def test_token_allowed(self):
        self.assertTrue(downloads.token_allowed("https://huggingface.co/a/b/resolve/main/x"))
        self.assertTrue(downloads.token_allowed("https://cdn-lfs.huggingface.co/x"))
        self.assertTrue(downloads.token_allowed("https://hf.co/x"))
        self.assertFalse(downloads.token_allowed("http://huggingface.co/x"))
        self.assertFalse(downloads.token_allowed("https://github.com/x"))
        self.assertFalse(downloads.token_allowed("https://huggingface.co.evil.example/x"))
        self.assertFalse(downloads.token_allowed("https://evilhuggingface.co/x"))

    def test_authorization_is_unredirected(self):
        seen = []

        def opener(req: Request, timeout=60.0):
            seen.append(req)
            raise OSError("offline")

        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(downloads.DownloadError):
            downloads.download_file(
                "https://huggingface.co/x/y/resolve/main/f.bin", Path(tmp) / "f.bin", token="hf_secret", opener=opener
            )
        req = seen[0]
        self.assertEqual(req.unredirected_hdrs.get("Authorization"), "Bearer hf_secret")
        self.assertNotIn("Authorization", req.headers)
        self.assertNotIn("hf_secret", req.full_url)


class PathValidationTests(unittest.TestCase):
    def test_safe_relative_path(self):
        self.assertEqual(downloads.safe_relative_path("checkpoints/a.ckpt"), "checkpoints/a.ckpt")
        self.assertEqual(downloads.safe_relative_path("clip\\b.safetensors"), "clip/b.safetensors")
        for bad in (
            "",
            "../x",
            "a/../../x",
            "..\\x",
            "/etc/passwd",
            "\\x",
            "C:/x",
            "C:x",
            "a//b",
            "a/./b",
            "a/",
        ):
            with self.assertRaises(ValueError, msg=bad):
                downloads.safe_relative_path(bad)

    def test_safe_join_stays_inside(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(downloads.safe_join(root, "inpaint/x.pt"), root / "inpaint" / "x.pt")
            with self.assertRaises(ValueError):
                downloads.safe_join(root, "../x.pt")

    def test_validate_node_spec(self):
        downloads.validate_node_spec(REPO, "comfyui-inpaint-nodes", COMMIT)
        downloads.validate_node_spec(REPO + ".git", "ComfyUI-GGUF", COMMIT)
        bad = [
            ("http://github.com/a/b", "b", COMMIT),
            ("https://gitlab.com/a/b", "b", COMMIT),
            ("https://github.com/a/b;rm -rf", "b", COMMIT),
            ("--upload-pack=x", "b", COMMIT),
            ("https://github.com/a/b", "../b", COMMIT),
            ("https://github.com/a/b", "a/b", COMMIT),
            ("https://github.com/a/b", "b", "abc1234"),
            ("https://github.com/a/b", "b", "main"),
        ]
        for repo, folder, commit in bad:
            with self.assertRaises(ValueError, msg=(repo, folder, commit)):
                downloads.validate_node_spec(repo, folder, commit)


class NodeInstallTests(unittest.TestCase):
    class R:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def _zip_bytes(self, comment=COMMIT, extra=None):
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(f"Repo-{COMMIT}/__init__.py", "x = 1\n")
            zf.writestr(f"Repo-{COMMIT}/requirements.txt", "gguf\n")
            for name in extra or []:
                zf.writestr(name, "bad")
            zf.comment = comment.encode()
        return buf.getvalue()

    def _install_zip(self, tmp, data, runner=None):
        def fake_download(url, dest, **kw):
            self.urls.append(url)
            Path(dest).write_bytes(data)

        self.urls = []
        orig = downloads.download_file
        downloads.download_file = fake_download
        try:
            return downloads.install_custom_node(
                REPO, "Repo", COMMIT, Path(tmp), Path("py.exe"), runner=runner or (lambda c, **k: self.R())
            )
        finally:
            downloads.download_file = orig

    def test_new_install_uses_zip_not_git(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            target = self._install_zip(tmp, self._zip_bytes(), lambda c, **k: calls.append(c) or self.R())
            self.assertEqual(self.urls, [f"{REPO}/archive/{COMMIT}.zip"])
            self.assertTrue((target / "__init__.py").is_file())
            self.assertEqual((target / ".openfill-commit").read_text().strip(), COMMIT)
            self.assertEqual(calls[0][:4], ["py.exe", "-s", "-m", "pip"])
            self.assertFalse(any("git" in str(c[0]).lower() for c in calls))
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["Repo"])  # no temp leftovers
            # second run: marker matches, nothing downloaded
            self.urls.clear()
            self._install_zip(tmp, b"")
            self.assertEqual(self.urls, [])

    def test_zip_for_another_commit_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(RuntimeError):
            self._install_zip(tmp, self._zip_bytes(comment="0" * 40))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse((Path(tmp) / "Repo").exists())

    def test_zip_with_path_traversal_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(RuntimeError):
            self._install_zip(tmp, self._zip_bytes(extra=["../evil.py"]))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse((Path(tmp) / "Repo").exists())

    def test_unmarked_existing_folder_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Repo").mkdir()
            with self.assertRaises(RuntimeError):
                self._install_zip(tmp, self._zip_bytes())

    def test_unreadable_path_entry_is_skipped(self):
        from unittest import mock

        real_is_file = Path.is_file

        def is_file(p):
            if "bad" in str(p):
                raise OSError(448, "The path cannot be traversed because it contains an untrusted mount point")
            return real_is_file(p)

        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good"
            good.mkdir()
            (good / "git").write_text("")
            path = os.pathsep.join([str(Path(tmp) / "bad"), str(good)])
            with mock.patch.dict(os.environ, {"PATH": path}), mock.patch.object(Path, "is_file", is_file):
                self.assertEqual(downloads.git_executable(), str(good / "git"))

    def test_existing_git_clone_is_reset_to_the_pin(self):
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            self.assertNotIn("shell", kw)
            return self.R(stdout=COMMIT + "\n" if "rev-parse" in cmd else "")

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Repo" / ".git").mkdir(parents=True)
            (Path(tmp) / "Repo" / "requirements.txt").write_text("gguf\n")
            downloads.install_custom_node(REPO, "Repo", COMMIT, Path(tmp), Path("py.exe"), runner=runner)
        self.assertIn("checkout", calls[1])
        self.assertEqual(calls[1][-1], COMMIT)
        self.assertEqual(calls[3][:4], ["py.exe", "-s", "-m", "pip"])

    def test_wrong_head_stops_before_pip(self):
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            return self.R(stdout="0" * 40 if "rev-parse" in cmd else "")

        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(RuntimeError):
            (Path(tmp) / "Repo" / ".git").mkdir(parents=True)
            (Path(tmp) / "Repo" / "requirements.txt").write_text("x\n")
            downloads.install_custom_node(REPO, "Repo", COMMIT, Path(tmp), Path("py.exe"), runner=runner)
        self.assertFalse(any("pip" in c for c in calls))

    def test_invalid_spec_runs_nothing(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            downloads.install_custom_node(
                "https://example/repo",
                "Repo",
                COMMIT,
                Path(tmp),
                Path("py.exe"),
                runner=lambda *a, **k: calls.append(a),
            )
        self.assertEqual(calls, [])

    def test_failure_raises(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(RuntimeError):
            (Path(tmp) / "Repo" / ".git").mkdir(parents=True)
            downloads.install_custom_node(
                REPO, "Repo", COMMIT, Path(tmp), Path("py.exe"), runner=lambda *a, **k: self.R(1, "", "boom")
            )


class WorkerFixture(unittest.TestCase):
    """A ComfyUI folder, a profile with config.json and a task worker (no tests of its own)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.comfy = base / "ComfyUI_windows_portable"
        (self.comfy / "python_embeded").mkdir(parents=True)
        (self.comfy / "python_embeded" / "python.exe").write_bytes(b"")
        (self.comfy / "ComfyUI" / "models").mkdir(parents=True)
        (self.comfy / "ComfyUI" / "custom_nodes").mkdir(parents=True)
        (self.comfy / "ComfyUI" / "main.py").write_text("")
        self.profile = base / "openfill"
        self.profile.mkdir()
        Settings(comfy_dir=str(self.comfy)).save(self.profile / "config.json")
        self.worker = supervisor.TaskWorker(
            self.profile, self.profile / "config.json", supervisor.Logger(self.profile / "test.log", "test")
        )
        self.models_root = self.comfy / "ComfyUI" / "models"
        self.entries = catalog.load_catalog()

    def tearDown(self):
        self.tmp.cleanup()


class SupervisorTaskValidationTests(WorkerFixture):
    """The task worker re-checks queued tasks against the catalog and config.json."""

    def _download_task(self, **file_overrides):
        f = catalog.by_id(self.entries, "mat-places512").files[0]
        entry = {"url": f.url, "dest": f.dest, "size": f.size, "sha256": f.sha256, "gated": False}
        entry.update(file_overrides)
        return {"type": "download", "model_id": "mat-places512", "models_root": str(self.models_root), "files": [entry]}

    def test_catalog_download_is_accepted_with_catalog_values(self):
        task = self._download_task(size=1, sha256=None)  # task values are ignored
        ((spec, dest),) = self.worker.checked_download_files(task)
        self.assertEqual(dest, self.models_root / "inpaint" / "MAT_Places512_G_fp16.safetensors")
        self.assertEqual(len(spec.sha256), 64)
        self.assertGreater(spec.size, 1)

    def test_foreign_url_or_dest_is_refused(self):
        for override in (
            {"url": "https://evil.example/x.safetensors"},
            {"dest": "../../evil.safetensors"},
            {"dest": "inpaint/other.safetensors"},
        ):
            with self.assertRaises(ValueError, msg=override):
                self.worker.checked_download_files(self._download_task(**override))

    def test_file_of_another_entry_is_refused(self):
        task = self._download_task()
        task["model_id"] = "lama-big"
        with self.assertRaises(ValueError):
            self.worker.checked_download_files(task)
        task["model_id"] = "ghost"
        with self.assertRaises(catalog.CatalogError):
            self.worker.checked_download_files(task)

    def test_other_models_root_is_refused(self):
        task = self._download_task()
        task["models_root"] = self.tmp.name
        with self.assertRaises(ValueError):
            self.worker.checked_download_files(task)

    def _node_task(self, **node_overrides):
        node = {"repo": REPO, "folder": "comfyui-inpaint-nodes", "commit": COMMIT}
        node.update(node_overrides)
        return {
            "type": "install_nodes",
            "custom_nodes_dir": str(self.comfy / "ComfyUI" / "custom_nodes"),
            "python": str(self.comfy / "python_embeded" / "python.exe"),
            "nodes": [node],
        }

    def test_catalog_node_is_accepted(self):
        nodes_dir, python, nodes = self.worker.checked_node_install(self._node_task())
        self.assertEqual(nodes[0].commit, COMMIT)
        self.assertEqual(python.name, "python.exe")
        self.assertEqual(nodes_dir.name, "custom_nodes")

    def test_foreign_node_or_interpreter_is_refused(self):
        for override in (
            {"repo": "https://github.com/evil/comfyui-inpaint-nodes"},
            {"commit": "0" * 40},
            {"folder": "other"},
        ):
            with self.assertRaises(ValueError, msg=override):
                self.worker.checked_node_install(self._node_task(**override))
        task = self._node_task()
        task["python"] = str(Path(self.tmp.name) / "evil.exe")
        with self.assertRaises(ValueError):
            self.worker.checked_node_install(task)

    def test_remove_only_deletes_catalog_files_inside_models_root(self):
        (self.models_root / "inpaint").mkdir()
        target = self.models_root / "inpaint" / "MAT_Places512_G_fp16.safetensors"
        target.write_bytes(b"x")
        stray = self.models_root / "inpaint" / "mine.safetensors"
        stray.write_bytes(b"x")
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_bytes(b"x")
        task = {
            "id": "t",
            "model_id": "mat-places512",
            "type": "remove",
            "models_root": str(self.models_root),
            "files": ["inpaint/MAT_Places512_G_fp16.safetensors", "inpaint/mine.safetensors", "../../outside.txt"],
        }
        self.worker._remove(task)
        self.assertFalse(target.exists())
        self.assertTrue(stray.exists())
        self.assertTrue(outside.exists())


class SupervisorLiveEntryTests(WorkerFixture):
    """Live (hub) entries are accepted only after the Models tab remembered them,
    and only with the strict live rules; a computed checksum is pinned afterwards."""

    LIVE_URL = "https://civitai.com/api/download/models/1759168"
    LIVE_DEST = f"checkpoints/{catalog.live_folder('civitai-133005-1759168-1659952')}/juggernautXL_ragnarok.safetensors"

    def _live_raw(self, sha256=None):
        base = catalog.by_id(self.entries, "sdxl-base")
        main = {"role": "checkpoint", "url": self.LIVE_URL, "dest": self.LIVE_DEST, "size": len(PAYLOAD)}
        if sha256:
            main["sha256"] = sha256
        return {
            "id": "civitai-133005-1759168-1659952",
            "name": "Juggernaut XL (Ragnarok)",
            "tier": base.tier,
            "capabilities": list(base.capabilities),
            "arch": "sdxl",
            "template": "sdxl_fooocus",
            "page": "https://civitai.com/models/133005?modelVersionId=1759168",
            "license": {"name": "Creator's terms on Civitai", "url": "https://civitai.com/models/133005"},
            "gated": False,
            "source_type": "first-party",
            "uploader": "KandooAI",
            "min_vram_gb": 6.8,
            "requires_nodes": [{"repo": n.repo, "folder": n.folder, "commit": n.commit} for n in base.requires_nodes],
            "requires_models": list(base.requires_models),
            "files": [main],
            "source": "civitai",
            "family": "sdxl",
        }

    def _live_task(self):
        return {
            "id": "live",
            "type": "download",
            "model_id": "civitai-133005-1759168-1659952",
            "models_root": str(self.models_root),
            "files": [{"url": self.LIVE_URL, "dest": self.LIVE_DEST, "size": 1, "sha256": None}],
        }

    def test_live_entry_needs_to_be_remembered(self):
        with self.assertRaises(catalog.CatalogError):
            self.worker.checked_download_files(self._live_task())
        catalog.remember_live_entry(self.profile, self._live_raw(), self.entries)
        ((spec, dest),) = self.worker.checked_download_files(self._live_task())
        self.assertEqual(spec.size, len(PAYLOAD))  # from the remembered entry, not the task
        self.assertEqual(dest, self.models_root.joinpath(*self.LIVE_DEST.split("/")))

    def test_tampered_live_record_is_ignored(self):
        catalog.remember_live_entry(self.profile, self._live_raw(), self.entries)
        path = catalog.hub_selected_path(self.profile)
        text = path.read_text(encoding="utf-8").replace(self.LIVE_URL, "https://evil.example/x.safetensors")
        path.write_text(text, encoding="utf-8")
        with self.assertRaises(catalog.CatalogError):
            self.worker.checked_download_files(self._live_task())

    def test_checksum_is_recorded_after_download(self):
        catalog.remember_live_entry(self.profile, self._live_raw(), self.entries)
        calls = []

        def fake_download(url, dest, size, sha, token, progress, cancel, **_kw):
            calls.append((url, size, sha, token))
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(PAYLOAD)
            return downloads.DownloadResult(Path(dest), len(PAYLOAD), SHA, 0)

        original = supervisor.downloads.download_file
        supervisor.downloads.download_file = fake_download
        try:
            self.worker._download(self._live_task())
        finally:
            supervisor.downloads.download_file = original
        self.assertEqual(calls, [(self.LIVE_URL, len(PAYLOAD), None, None)])  # no token for Civitai
        last = self.worker.status["history"][0]
        self.assertEqual(last["state"], "done")
        self.assertEqual(last["files"][0]["sha256"], SHA)
        self.assertFalse(last["files"][0]["verified"])
        self.assertTrue(last["files"][0]["sha256_recorded"])
        (entry,) = catalog.load_selected(self.profile, self.entries)
        self.assertEqual(entry.files[0].sha256, SHA)  # later downloads are verified against it


class ProcessIdentityTests(unittest.TestCase):
    def test_own_process_is_python(self):
        self.assertTrue(supervisor.pid_is_python(os.getpid()))
        name = supervisor.process_name(os.getpid())
        self.assertTrue(name is None or "python" in name)

    def test_dead_pid(self):
        self.assertFalse(supervisor.pid_is_python(0))
        self.assertIsNone(supervisor.process_name(0))

    def test_loopback(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            self.assertTrue(supervisor.is_loopback(host), host)
        for host in ("0.0.0.0", "192.168.1.5", "example.com"):
            self.assertFalse(supervisor.is_loopback(host), host)


if __name__ == "__main__":
    unittest.main()


class OrphanedTaskTests(WorkerFixture):
    """A worker killed or shut down with GIMP mid-download leaves a ``.running.json`` behind."""

    def test_next_worker_requeues_it(self):
        tasks = self.profile / "tasks"
        (tasks / "1-download.running.json").write_text('{"id": "1-download", "type": "download"}')
        (tasks / "2-download.json").write_text('{"id": "2-download", "type": "download"}')
        self.assertEqual(self.worker.requeue_orphans(), ["1-download.json"])
        self.assertEqual([p.name for p in self.worker.pending()], ["1-download.json", "2-download.json"])
        self.assertEqual(self.worker.requeue_orphans(), [])


def _ran(rc=0, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


class PipTests(unittest.TestCase):
    def test_pip_is_used_as_is_when_present(self):
        calls = []
        cmd = downloads.pip_install_command("py", runner=lambda c, **k: calls.append(c) or _ran())
        self.assertEqual(cmd, ["py", "-s", "-m", "pip", "install", "--no-warn-script-location"])
        self.assertEqual(calls, [["py", "-s", "-m", "pip", "--version"]])

    def test_venv_without_pip_gets_it_from_ensurepip(self):
        calls = []

        def runner(c, **_k):
            calls.append(c)
            return _ran(1 if c[-1] == "--version" else 0)

        downloads.pip_install_command("py", runner=runner)
        self.assertEqual(calls[1], ["py", "-s", "-m", "ensurepip", "--upgrade"])

    def test_no_pip_and_no_ensurepip_says_so(self):
        with self.assertRaises(RuntimeError) as cm:
            downloads.pip_install_command("py", runner=lambda c, **k: _ran(1, err="No module named ensurepip"))
        self.assertIn("ensurepip", str(cm.exception))

    def test_install_pip_packages(self):
        calls = []
        downloads.install_pip_packages(
            "py", ["opencv-python-headless==5.0.0.93"], runner=lambda c, **k: calls.append(c) or _ran()
        )
        self.assertEqual(calls[-1][-1], "opencv-python-headless==5.0.0.93")
        with self.assertRaises(RuntimeError):
            downloads.install_pip_packages(
                "py", ["x==1"], runner=lambda c, **k: _ran(1 if "x==1" in c else 0, err="no")
            )
        calls.clear()
        downloads.install_pip_packages("py", [], runner=lambda c, **k: calls.append(c) or _ran())
        self.assertEqual(calls, [])

    def test_has_module(self):
        self.assertTrue(downloads.has_module("py", "cv2", runner=lambda c, **k: _ran(0)))
        self.assertFalse(downloads.has_module("py", "cv2", runner=lambda c, **k: _ran(1)))
        self.assertFalse(downloads.has_module(sys.executable, "no_such_module_here"))
        self.assertTrue(downloads.has_module(sys.executable, "json"))
        with self.assertRaises(ValueError):
            downloads.has_module("py", "os; import evil")


class PipTaskTests(WorkerFixture):
    def _task(self, **over):
        task = {
            "type": "install_pip",
            "python": str(self.comfy / "python_embeded" / "python.exe"),
            "packages": list(comfy_setup.load_manifest().pip),
        }
        task.update(over)
        return task

    def test_only_the_comfy_interpreter_and_the_pinned_packages(self):
        self.assertEqual(self.worker.checked_pip_install(self._task()).name, "python.exe")
        for over in ({"python": "/usr/bin/evil"}, {"packages": ["evil==1"]}, {"packages": []}):
            with self.assertRaises(ValueError, msg=over):
                self.worker.checked_pip_install(self._task(**over))

    def test_install_runs_pip_only_when_cv2_is_missing(self):
        with (
            mock.patch.object(downloads, "has_module", return_value=True),
            mock.patch.object(downloads, "install_pip_packages") as pip,
            mock.patch.object(self.worker, "_finish") as finish,
        ):
            self.worker._install_pip(self._task(id="t"))
        pip.assert_not_called()
        self.assertIn("already", finish.call_args[0][2])
        with (
            mock.patch.object(downloads, "has_module", return_value=False),
            mock.patch.object(downloads, "install_pip_packages") as pip,
            mock.patch.object(self.worker, "_finish") as finish,
        ):
            self.worker._install_pip(self._task(id="t"))
        pip.assert_called_once()
        self.assertEqual(finish.call_args[0][1], "done")

    def test_node_install_also_brings_opencv(self):
        with (
            mock.patch.object(downloads, "install_custom_node"),
            mock.patch.object(downloads, "has_module", return_value=False),
            mock.patch.object(downloads, "install_pip_packages") as pip,
            mock.patch.object(self.worker, "_finish"),
        ):
            self.worker._install_nodes(
                {
                    "type": "install_nodes",
                    "custom_nodes_dir": str(self.comfy / "ComfyUI" / "custom_nodes"),
                    "python": str(self.comfy / "python_embeded" / "python.exe"),
                    "nodes": [{"repo": REPO, "folder": "comfyui-inpaint-nodes", "commit": COMMIT}],
                    "restart_backend": False,
                }
            )
        pip.assert_called_once()
        self.assertIn("opencv-python-headless==5.0.0.93", pip.call_args[0][1])

    def test_failed_opencv_install_does_not_fail_the_nodes(self):
        with (
            mock.patch.object(downloads, "has_module", return_value=False),
            mock.patch.object(downloads, "install_pip_packages", side_effect=RuntimeError("offline")),
        ):
            self.worker._ensure_opencv(Path("py"), {"id": "t", "type": "install_nodes"})
