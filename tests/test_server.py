# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""ServerManager: messages, the port check, argument passing and log handling (no ComfyUI needed)."""

import json
import os
import socket
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

from openfill import server
from openfill.backend import BackendUnavailable, has_next_step
from openfill.config import Paths, Settings
from openfill.server import ServerManager, port_answers, strip_ansi
from openfill.supervisor import build_parser

# Verbatim tail of a ComfyUI log whose port was taken (evidence rob-port-in-use-offline).
PORT_TAKEN_LOG = (
    "\x1b[32m[INFO]\x1b[0m \nImport times for custom nodes:\n"
    "\x1b[32m[INFO]\x1b[0m    0.0 seconds: C:\\AI\\ComfyUI\\custom_nodes\\websocket_image_save.py\n"
    "\x1b[32m[INFO]\x1b[0m Using RAM pressure cache.\n"
    "\x1b[1m\x1b[31m[ERROR]\x1b[0m Port 8199 is already in use on address 127.0.0.1. "
    "Please close the other application or use a different port with --port.\n"
)
FAKE_LAYOUT = {"python": Path("python.exe"), "main": Path("main.py"), "cwd": Path(".")}


class StripAnsiTests(unittest.TestCase):
    def test_removes_color_codes(self):
        self.assertEqual(strip_ansi("\x1b[1m\x1b[31m[ERROR]\x1b[0m bad"), "[ERROR] bad")

    def test_removes_cursor_and_osc_sequences(self):
        self.assertEqual(strip_ansi("a\x1b[2Kb\x1b[10;5Hc\x1b]0;title\x07d"), "abcd")

    def test_plain_text_and_backslashes_untouched(self):
        text = "C:\\AI\\ComfyUI [INFO] 50% done"
        self.assertEqual(strip_ansi(text), text)


class ManagerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.paths = Paths(Path(self._tmp.name) / "openfill").ensure()
        self.settings = Settings(url="http://127.0.0.1:8199", startup_timeout=240)
        self.manager = ServerManager(self.settings, self.paths)


class EnsureRunningTests(ManagerCase):
    def test_autostart_off_message_has_next_step(self):
        self.settings.autostart = False
        with (
            mock.patch.object(ServerManager, "is_healthy", return_value=False),
            self.assertRaises(BackendUnavailable) as ctx,
        ):
            self.manager.ensure_running()
        text = str(ctx.exception)
        self.assertIn("ComfyUI is not running at http://127.0.0.1:8199", text)
        self.assertIn("Start backend automatically", text)
        self.assertIn("Filters > OpenFill > Generative Fill Settings", text)
        self.assertIn("Check backend", text)


class LongPathTests(ManagerCase):
    def test_long_windows_path_stops_before_spawn(self):
        layout = dict(FAKE_LAYOUT, python=Path("C:/" + "deep/" * 40 + "python_embeded/python.exe"))
        with (
            mock.patch.object(server, "comfy_layout", return_value=layout),
            mock.patch("openfill.config.sys.platform", "win32"),
            mock.patch.object(ServerManager, "_spawn_detached") as spawn,
            self.assertRaisesRegex(BackendUnavailable, "short directory junction"),
        ):
            self.manager.start()
        spawn.assert_not_called()

    def test_short_junction_spelling_is_kept(self):
        from openfill.config import windows_path_problem

        with mock.patch.object(Path, "resolve", side_effect=AssertionError("do not resolve junctions")):
            self.assertEqual(windows_path_problem(Path("C:/AI/ComfyUI/python_embeded/python.exe"), "win32"), "")

    def test_long_linux_path_is_allowed(self):
        from openfill.config import windows_path_problem

        self.assertEqual(windows_path_problem(Path("/" + "deep/" * 50 + "venv/bin/python"), "linux"), "")


class PortProbeTests(ManagerCase):
    def setUp(self):
        super().setUp()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.addCleanup(self.listener.close)
        self.port = self.listener.getsockname()[1]
        self.settings.url = f"http://127.0.0.1:{self.port}"

    def test_port_answers(self):
        self.assertTrue(port_answers("127.0.0.1", self.port))
        self.listener.close()
        self.assertFalse(port_answers("127.0.0.1", self.port, timeout=0.5))

    def test_taken_port_fails_fast_with_plain_message(self):
        with (
            mock.patch.object(server, "comfy_layout", return_value=FAKE_LAYOUT),
            mock.patch.object(server.ComfyClient, "is_alive", return_value=False),
            mock.patch.object(ServerManager, "_spawn_detached") as spawn,
        ):
            started = time.monotonic()
            with self.assertRaises(BackendUnavailable) as ctx:
                self.manager.start()
        self.assertLess(time.monotonic() - started, 5)
        spawn.assert_not_called()
        text = str(ctx.exception)
        self.assertIn(f"Port {self.port} is used by another program (it is not ComfyUI)", text)
        self.assertIn(
            "Close that program, or set a free port in the ComfyUI URL in Filters > OpenFill > Generative Fill Settings",
            text,
        )
        self.assertIn("http://127.0.0.1:8190", text)

    def test_slow_comfyui_on_the_port_is_used_not_rejected(self):
        with (
            mock.patch.object(server, "comfy_layout", return_value=FAKE_LAYOUT),
            mock.patch.object(server.ComfyClient, "is_alive", return_value=True),
            mock.patch.object(ServerManager, "_spawn_detached") as spawn,
        ):
            self.assertEqual(self.manager.start(), self.settings.url)
        spawn.assert_not_called()


class LaunchArgumentTests(ManagerCase):
    def test_extra_args_starting_with_dash_reach_the_supervisor(self):
        self.settings.extra_server_args = " --cpu "
        with (
            mock.patch.object(server, "comfy_layout", return_value=FAKE_LAYOUT),
            mock.patch.object(server, "port_answers", return_value=False),
            mock.patch.object(ServerManager, "_spawn_detached") as spawn,
            mock.patch.object(ServerManager, "_wait_ready", return_value="ok"),
        ):
            self.manager.start()
        cmd = spawn.call_args[0][0]
        self.assertIn("--extra-args=--cpu", cmd)
        self.assertNotIn("--extra-args", cmd)  # never as a separate item
        args = build_parser().parse_args(cmd[2:])
        self.assertEqual(args.extra_args, "--cpu")
        self.assertEqual(args.port, 8199)

    def test_no_extra_args_item_when_empty(self):
        with (
            mock.patch.object(server, "comfy_layout", return_value=FAKE_LAYOUT),
            mock.patch.object(server, "port_answers", return_value=False),
            mock.patch.object(ServerManager, "_spawn_detached") as spawn,
            mock.patch.object(ServerManager, "_wait_ready", return_value="ok"),
        ):
            self.manager.start()
        self.assertFalse([a for a in spawn.call_args[0][0] if a.startswith("--extra-args")])

    def test_supervisor_stderr_is_appended_to_a_log(self):
        log = self.manager.supervisor_stderr_log
        log.write_text("earlier run\n", encoding="utf-8")
        with warnings.catch_warnings():  # the detached child outlives its Popen object on purpose
            warnings.simplefilter("ignore", ResourceWarning)
            self.manager._spawn_detached([sys.executable, "-c", "import sys; sys.stderr.write('argparse said no')"])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and "argparse said no" not in log.read_text(encoding="utf-8"):
            time.sleep(0.2)
        text = log.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("earlier run\n"))
        self.assertIn("argparse said no", text)
        self.assertEqual(log.parent, self.paths.base)
        self.assertEqual(log.name, "supervisor-stderr.log")


class WaitReadyTests(ManagerCase):
    def _fail_fast(self):
        self.paths.state.write_text(json.dumps({"status": "failed"}), encoding="utf-8")

    def test_port_taken_in_log_gives_port_message(self):
        self._fail_fast()
        self.paths.server_log.write_text(PORT_TAKEN_LOG, encoding="utf-8")
        with (
            mock.patch.object(server.ComfyClient, "is_alive", return_value=False),
            self.assertRaises(BackendUnavailable) as ctx,
        ):
            self.manager._wait_ready(self.settings.url, None)
        text = str(ctx.exception)
        self.assertIn("Port 8199 is used by another program", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("within", text)

    def test_reports_real_elapsed_time_not_the_timeout(self):
        self._fail_fast()
        self.paths.server_log.write_text("\x1b[31m[ERROR]\x1b[0m something broke\n", encoding="utf-8")
        with (
            mock.patch.object(server.ComfyClient, "is_alive", return_value=False),
            self.assertRaises(BackendUnavailable) as ctx,
        ):
            self.manager._wait_ready(self.settings.url, None)
        text = str(ctx.exception)
        self.assertNotIn("240", text)
        self.assertIn("gave up after 0 s", text)
        self.assertIn("[ERROR] something broke", text)
        self.assertNotIn("\x1b", text)
        self.assertTrue(has_next_step(text))

    def test_log_tail_strips_ansi(self):
        self.paths.server_log.write_text(PORT_TAKEN_LOG, encoding="utf-8")
        tail = self.manager.server_log_tail(2)
        self.assertNotIn("\x1b", tail)
        self.assertTrue(tail.endswith("use a different port with --port."))


if __name__ == "__main__":
    unittest.main()


class CancelAndWatcherTests(ManagerCase):
    """rob-cancel-starting-backend and rob-two-requests-first-gimp-exits."""

    def test_cancel_while_starting_returns_at_once(self):
        from openfill.backend import GenerationCancelled

        with (
            mock.patch.object(server.ComfyClient, "is_alive", return_value=False),
            self.assertRaises(GenerationCancelled),
        ):
            self.manager._wait_ready(self.settings.url, None, cancel=lambda: True)

    def test_touch_registers_this_gimp_with_a_managed_backend(self):
        self.manager._write_state({"supervisor_pid": 1, "url": self.settings.url})
        self.manager.touch()
        watchers = self.paths.base / "watchers"
        self.assertTrue((watchers / str(os.getppid())).exists())

    def test_no_registration_without_a_managed_backend(self):
        self.manager.touch()
        self.assertFalse((self.paths.base / "watchers").exists())


class HelperPythonTests(unittest.TestCase):
    """macOS: GIMP.app's Python has a launch constraint, so helpers run on another interpreter."""

    GIMP_PY = "/Applications/GIMP.app/Contents/MacOS/python3"

    def test_other_platforms_keep_the_plug_in_interpreter(self):
        for platform in ("win32", "linux"):
            self.assertEqual(
                server.helper_python(r"C:\GIMP\bin\pythonw.exe", platform, "/c/venv/bin/python3"),
                r"C:\GIMP\bin\pythonw.exe",
            )

    def test_macos_outside_an_app_bundle_keeps_it(self):
        self.assertEqual(server.helper_python("/opt/homebrew/bin/python3", "darwin", None), "/opt/homebrew/bin/python3")

    def test_macos_uses_comfyui_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            venv_py = Path(tmp) / "venv" / "bin" / "python3"
            venv_py.parent.mkdir(parents=True)
            venv_py.write_text("")
            self.assertEqual(server.helper_python(self.GIMP_PY, "darwin", venv_py), str(venv_py))

    def test_macos_falls_back_to_path_python_outside_the_bundle(self):
        seen = {}

        def which(name, path=None):
            seen["path"] = path
            return "/opt/homebrew/bin/python3"

        path_env = os.pathsep.join(["/Applications/GIMP.app/Contents/MacOS", "/opt/homebrew/bin", "/usr/bin"])
        got = server.helper_python(self.GIMP_PY, "darwin", "/missing/venv/bin/python3", which, path_env)
        self.assertEqual(got, "/opt/homebrew/bin/python3")
        self.assertNotIn("GIMP.app", seen["path"])

    def test_macos_without_any_other_python_keeps_it(self):
        self.assertEqual(server.helper_python(self.GIMP_PY, "darwin", None, lambda *a, **k: None, ""), self.GIMP_PY)

    def test_in_app_bundle(self):
        self.assertTrue(server.in_app_bundle(self.GIMP_PY))
        self.assertFalse(server.in_app_bundle("/Users/me/ComfyUI/venv/bin/python3"))


class StaleTaskStatusTests(ManagerCase):
    """tasks-status.json and *.running.json written by a worker that is gone."""

    def setUp(self):
        super().setUp()
        status = {"worker_pid": 999999, "current": {"type": "download", "model_id": "mat", "done": 1}, "history": []}
        (self.paths.base / "tasks-status.json").write_text(json.dumps(status), encoding="utf-8")
        self.manager.tasks_dir.mkdir(parents=True, exist_ok=True)
        (self.manager.tasks_dir / "1-download.running.json").write_text('{"model_id": "mat"}', encoding="utf-8")
        (self.manager.tasks_dir / "2-download.json").write_text('{"model_id": "lama"}', encoding="utf-8")

    def test_dead_worker_shows_nothing_running(self):
        self.assertIsNone(self.manager.task_status()["current"])
        self.assertEqual([t["model_id"] for t in self.manager.pending_tasks()], ["lama"])

    def test_live_worker_keeps_its_task(self):
        with mock.patch.object(ServerManager, "task_worker_alive", return_value=True):
            self.assertEqual(self.manager.task_status()["current"]["model_id"], "mat")
            self.assertEqual([t["model_id"] for t in self.manager.pending_tasks()], ["mat", "lama"])
