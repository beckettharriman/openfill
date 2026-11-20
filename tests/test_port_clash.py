# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Issue #12: another ComfyUI on OpenFill's port, AMD settings next to an NVIDIA card, and the
slow-start hint. Sockets, processes and ComfyUI are all stand-ins (no ComfyUI needed)."""

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from openfill import hardware as hwmod
from openfill import server
from openfill.backend import BackendUnavailable
from openfill.comfy_api import ComfyApiError, ComfyClient
from openfill.config import Paths, Settings
from openfill.server import PortTaken, ServerManager
from openfill.supervisor import build_parser

FAKE_LAYOUT = {"python": Path("python"), "main": Path("/c/ComfyUI/main.py"), "cwd": Path("/c/ComfyUI")}
# What the supervisor runs (and records as "command") for port 8199 with shared folders.
OUR_COMMAND = [
    "/c/ComfyUI/venv/bin/python",
    "-s",
    "/c/ComfyUI/main.py",
    "--listen",
    "127.0.0.1",
    "--port",
    "8199",
    "--disable-auto-launch",
    "--dont-print-server",
    "--preview-method",
    "taesd",
    "--extra-model-paths-config",
    "/p/openfill/shared-model-paths.yaml",
]
OUR_ARGV = OUR_COMMAND[2:]  # ComfyUI's sys.argv: main.py and its options
USER_ARGV = ["/c/ComfyUI/main.py", "--port", "8199"]  # the user's own ComfyUI, same install


def stats(argv):
    return {"system": {"comfyui_version": "0.44.0", "argv": argv}, "devices": []}


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.paths = Paths(Path(tmp.name) / "openfill").ensure()
        self.settings = Settings(url="http://127.0.0.1:8199", startup_timeout=240)
        self.manager = ServerManager(self.settings, self.paths, mock.Mock())
        # pids in the state files below count as a running supervisor
        patcher = mock.patch.object(server, "pid_is_python", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_state(self, port=8199, command=OUR_COMMAND, **extra):
        state = {
            "supervisor_pid": 4241,
            "server_pid": 4242,
            "url": f"http://127.0.0.1:{port}",
            "port": port,
            "command": command,
            **extra,
        }
        self.paths.state.write_text(json.dumps(state), encoding="utf-8")
        return state


class HelperTests(unittest.TestCase):
    def test_same_command_tells_ours_from_the_users(self):
        self.assertTrue(server.same_comfy_command(OUR_ARGV, OUR_COMMAND))
        self.assertFalse(server.same_comfy_command(USER_ARGV, OUR_COMMAND))
        self.assertFalse(server.same_comfy_command(["main.py"], OUR_COMMAND))
        # the same options on another port (another OpenFill) are not ours either
        other = [*OUR_ARGV]
        other[other.index("8199")] = "8188"
        self.assertFalse(server.same_comfy_command(other, OUR_COMMAND))
        self.assertFalse(server.same_comfy_command([], OUR_COMMAND))

    def test_same_command_on_windows_paths(self):
        command = ["C:\\AI\\python_embeded\\python.exe", "-s", "C:\\AI\\ComfyUI\\main.py", "--port", "8188"]
        self.assertTrue(server.same_comfy_command(["ComfyUI\\main.py", "--port", "8188"], command))
        self.assertTrue(server.same_comfy_command(["C:\\AI\\ComfyUI\\MAIN.py", "--port", "8188"], command))

    def test_comfy_argv(self):
        self.assertEqual(server.comfy_argv(stats(["main.py", "--port", 8188])), ["main.py", "--port", "8188"])
        self.assertIsNone(server.comfy_argv({"system": {}}))  # a ComfyUI that does not show it
        self.assertIsNone(server.comfy_argv({"system": {"argv": "main.py"}}))
        self.assertIsNone(server.comfy_argv([]))

    def test_free_port_skips_taken_and_avoided_ports(self):
        taken = {8189, 8191}
        unbindable = {8192}
        found = server.free_port(
            "127.0.0.1",
            8188,
            avoid=(8190,),
            answers=lambda _h, p, timeout=0: p in taken,
            bindable=lambda _h, p: p not in unbindable,
        )
        self.assertEqual(found, 8193)

    def test_free_port_none_when_all_taken(self):
        self.assertIsNone(server.free_port("127.0.0.1", 8188, answers=lambda *_a, **_k: True))
        self.assertIsNone(server.free_port("127.0.0.1", 65535))

    def test_free_port_on_real_sockets(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        taken = listener.getsockname()[1]
        self.assertFalse(server.port_bindable("127.0.0.1", taken))
        found = server.free_port("127.0.0.1", taken - 1)
        self.assertIsNotNone(found)
        self.assertNotEqual(found, taken)
        self.assertTrue(server.port_bindable("127.0.0.1", found))

    def test_with_port(self):
        self.assertEqual(server.with_port("http://127.0.0.1:8188", 8189), "http://127.0.0.1:8189")
        self.assertEqual(server.with_port("http://localhost:8188/x", 8190), "http://localhost:8190/x")
        self.assertEqual(server.with_port("http://[::1]:8188", 8189), "http://[::1]:8189")

    def test_port_taken_is_a_backend_unavailable_in_plain_words(self):
        err = PortTaken("127.0.0.1", 8188)
        self.assertIsInstance(err, BackendUnavailable)
        self.assertIn("Port 8188 is used by another program", str(err))
        self.assertIn("another ComfyUI", str(PortTaken("127.0.0.1", 8188, comfy=True)))


class IdentityTests(Case):
    def ask(self, answer):
        with mock.patch.object(ComfyClient, "system_stats", **answer):
            return self.manager.answering_is_ours("http://127.0.0.1:8199")

    def test_ours_theirs_and_unknown(self):
        self.write_state()
        self.assertTrue(self.ask({"return_value": stats(OUR_ARGV)}))
        self.assertFalse(self.ask({"return_value": stats(USER_ARGV)}))
        self.assertIsNone(self.ask({"return_value": {"system": {}}}))  # an old ComfyUI: cannot tell
        self.assertIsNone(self.ask({"side_effect": ComfyApiError("timed out")}))

    def test_unknown_without_a_running_supervisor_or_command(self):
        self.assertIsNone(self.ask({"return_value": stats(USER_ARGV)}))  # no state
        self.write_state(command=None)
        self.assertIsNone(self.ask({"return_value": stats(USER_ARGV)}))
        self.write_state(port=8200)  # the state is about another address
        self.assertIsNone(self.ask({"return_value": stats(USER_ARGV)}))
        self.write_state()
        with mock.patch.object(server, "pid_is_python", return_value=False):  # its supervisor is gone
            self.assertIsNone(self.ask({"return_value": stats(USER_ARGV)}))


class WaitReadyTests(Case):
    def test_our_comfyui_is_ready(self):
        self.write_state()
        with (
            mock.patch.object(ComfyClient, "is_alive", return_value=True),
            mock.patch.object(ComfyClient, "system_stats", return_value=stats(OUR_ARGV)),
        ):
            self.assertEqual(self.manager._wait_ready(self.settings.url, None), self.settings.url)

    def test_the_users_comfyui_on_our_port_is_not_taken_for_ours(self):
        """The report: OpenFill said "ready" while the user's ComfyUI answered and its own then died."""
        self.write_state()
        with (
            mock.patch.object(ComfyClient, "is_alive", return_value=True),
            mock.patch.object(ComfyClient, "system_stats", return_value=stats(USER_ARGV)),
            mock.patch.object(ServerManager, "request_stop", return_value=True) as stop,
            mock.patch.object(ServerManager, "read_state", side_effect=[json.loads(self.paths.state.read_text()), {}]),
            self.assertRaises(PortTaken) as ctx,
        ):
            self.manager._wait_ready(self.settings.url, None)
        stop.assert_called_once()  # ours would fail to listen: it is stopped, not left loading
        self.assertTrue(ctx.exception.comfy)
        self.assertEqual(ctx.exception.port, 8199)

    def test_an_answer_before_our_state_exists_is_not_ready(self):
        calls = {"n": 0}

        def alive(_self, timeout=3.0):
            calls["n"] += 1
            if calls["n"] == 2:
                self.write_state()  # the supervisor writes its state a moment later
            return True

        with (
            mock.patch.object(ComfyClient, "is_alive", alive),
            mock.patch.object(ComfyClient, "system_stats", return_value=stats(OUR_ARGV)),
            mock.patch.object(server.time, "sleep"),
        ):
            self.assertEqual(self.manager._wait_ready(self.settings.url, None), self.settings.url)
        self.assertGreaterEqual(calls["n"], 3)

    def test_older_comfyui_without_argv_is_accepted_as_before(self):
        self.write_state()
        with (
            mock.patch.object(ComfyClient, "is_alive", return_value=True),
            mock.patch.object(ComfyClient, "system_stats", return_value={"system": {}}),
        ):
            self.assertEqual(self.manager._wait_ready(self.settings.url, None), self.settings.url)

    def test_port_taken_in_the_log_is_a_port_taken(self):
        self.paths.state.write_text(json.dumps({"status": "failed"}), encoding="utf-8")
        self.paths.server_log.write_text("[ERROR] Port 8199 is already in use on address 127.0.0.1.\n")
        with mock.patch.object(ComfyClient, "is_alive", return_value=False), self.assertRaises(PortTaken) as ctx:
            self.manager._wait_ready(self.settings.url, None)
        self.assertFalse(ctx.exception.comfy)


class MoveToFreePortTests(Case):
    def test_race_restarts_ours_on_a_free_port(self):
        """Ours was loading on 8199 when another ComfyUI took the port: it starts again on a free one."""
        waits = []

        def wait(url, *_a):
            waits.append(url)
            if len(waits) == 1:
                raise PortTaken("127.0.0.1", 8199, comfy=True)
            return url

        progress = mock.Mock()
        with (
            mock.patch.object(server, "comfy_layout", return_value=FAKE_LAYOUT),
            mock.patch.object(server, "port_answers", return_value=False),
            mock.patch.object(server, "free_port", return_value=8200) as free,
            mock.patch.object(ServerManager, "_spawn_detached") as spawn,
            mock.patch.object(ServerManager, "_wait_ready", side_effect=wait),
        ):
            url = self.manager.start(progress)
        self.assertEqual(url, "http://127.0.0.1:8200")
        self.assertEqual(waits, ["http://127.0.0.1:8199", "http://127.0.0.1:8200"])
        ports = [build_parser().parse_args(c[0][0][2:]).port for c in spawn.call_args_list]
        self.assertEqual(ports, [8199, 8200])
        self.assertEqual(free.call_args.kwargs["avoid"], (8199,))
        self.assertIn("Port 8199 is used by another ComfyUI", progress.call_args[0][1])
        self.manager.log.warning.assert_called()

    def test_second_clash_gives_the_plain_message(self):
        with (
            mock.patch.object(server, "comfy_layout", return_value=FAKE_LAYOUT),
            mock.patch.object(server, "port_answers", return_value=False),
            mock.patch.object(server, "free_port", return_value=None),
            mock.patch.object(ServerManager, "_spawn_detached") as spawn,
            mock.patch.object(ServerManager, "_wait_ready", side_effect=PortTaken("127.0.0.1", 8199, comfy=True)),
            self.assertRaisesRegex(BackendUnavailable, "Port 8199 is used by another ComfyUI"),
        ):
            self.manager.start()
        spawn.assert_called_once()

    def test_ensure_running_waits_for_ours_when_another_answers(self):
        """A second job while ours still loads must not run on the user's ComfyUI either."""
        self.write_state()
        with (
            mock.patch.object(ServerManager, "is_healthy", return_value=True),
            mock.patch.object(ComfyClient, "system_stats", return_value=stats(USER_ARGV)),
            mock.patch.object(ServerManager, "_await_own", return_value="http://127.0.0.1:8200") as await_own,
        ):
            self.assertEqual(self.manager.ensure_running(), "http://127.0.0.1:8200")
        await_own.assert_called_once()

    def test_ensure_running_uses_ours(self):
        self.write_state()
        with (
            mock.patch.object(ServerManager, "is_healthy", return_value=True),
            mock.patch.object(ComfyClient, "system_stats", return_value=stats(OUR_ARGV)),
            mock.patch.object(ServerManager, "_await_own") as await_own,
        ):
            self.assertEqual(self.manager.ensure_running(), self.settings.url)
        await_own.assert_not_called()
        self.assertEqual(self.manager.note(), "")


class ExistingComfyTests(Case):
    """A ComfyUI that already runs at the URL is used as before, and the status says so."""

    def test_already_running_comfyui_is_used_and_named(self):
        with (
            mock.patch.object(ServerManager, "is_healthy", return_value=True),
            mock.patch.object(ServerManager, "_spawn_detached") as spawn,
        ):
            self.assertEqual(self.manager.ensure_running(), self.settings.url)
        spawn.assert_not_called()
        note = self.manager.note()
        self.assertIn("Using the ComfyUI that was already running at http://127.0.0.1:8199", note)
        self.assertNotIn("Models from another ComfyUI", note)
        self.assertEqual(self.manager.short_note(), "")  # nothing to warn about on the status line

    def test_already_running_comfyui_with_shared_folders_warns(self):
        self.settings.shared_models = "/mnt/usb/ComfyUI"
        with mock.patch.object(ServerManager, "is_healthy", return_value=True):
            self.manager.ensure_running()
        self.assertIn("Models from another ComfyUI", self.manager.note())
        self.assertEqual(self.manager.short_note(), "Ready (a ComfyUI OpenFill did not start)")

    def test_autostart_off_is_unchanged(self):
        self.settings.autostart = False
        self.write_state()
        with (
            mock.patch.object(ServerManager, "is_healthy", return_value=True),
            mock.patch.object(ComfyClient, "system_stats", side_effect=AssertionError("not asked")),
        ):
            self.assertEqual(self.manager.ensure_running(), self.settings.url)

    def test_moved_port_note(self):
        self.write_state(port=8200)
        self.assertIn("so OpenFill started its own ComfyUI on port 8200", self.manager.note())
        self.assertEqual(self.manager.short_note(), "Ready on port 8200 (8199 is taken)")

    def test_remote_gpu_and_a1111_have_no_note(self):
        self.write_state(port=8200)
        self.settings.backend = "remote"
        self.assertEqual((self.manager.note(), self.manager.short_note()), ("", ""))
        self.settings.backend = "a1111"
        self.assertEqual((self.manager.note(), self.manager.short_note()), ("", ""))

    def test_remote_gpu_never_checks_the_local_port(self):
        self.settings.backend = "remote"
        link = mock.Mock(kind="gpu-server")
        link.ensure.return_value = "http://127.0.0.1:5555"
        self.manager._remote = link
        with (
            mock.patch.object(server, "port_answers", side_effect=AssertionError("no local port check")),
            mock.patch.object(ServerManager, "_spawn_detached", side_effect=AssertionError("nothing started")),
        ):
            self.assertEqual(self.manager.ensure_running(), "http://127.0.0.1:5555")


class SlowStartTests(Case):
    def test_hint_after_the_usual_start_time(self):
        self.write_state(shared_roots=["/mnt/usb/models"])
        clock = {"t": 1000.0}

        def monotonic():
            clock["t"] += 30.0
            return clock["t"]

        progress = mock.Mock()
        with (
            mock.patch.object(server.time, "monotonic", monotonic),
            mock.patch.object(server.time, "sleep"),
            mock.patch.object(ComfyClient, "is_alive", side_effect=[False, False, False, True]),
            mock.patch.object(ComfyClient, "system_stats", return_value=stats(OUR_ARGV)),
            mock.patch.object(server, "unresponsive_folders", return_value=["/mnt/usb/models"]) as probe,
        ):
            self.manager._wait_ready(self.settings.url, progress)
        probe.assert_called_once_with(["/mnt/usb/models"])
        texts = [c[0][1] for c in progress.call_args_list]
        self.assertNotIn("Slower than usual", texts[0])
        self.assertTrue(any("the model folder /mnt/usb/models does not answer" in t for t in texts), texts)
        self.manager.log.warning.assert_called()

    def test_generic_hint(self):
        text = server.slow_start_hint()
        self.assertTrue(text.startswith("Slower than usual."), text)
        self.assertIn("unplugged drive", text)
        self.assertNotIn("\u2014", text)

    def test_unresponsive_folders_names_a_folder_that_hangs(self):
        release = threading.Event()
        self.addCleanup(release.set)
        real_stat = os.stat

        def stat(path, *a, **k):
            if str(path) == "/mnt/asleep":
                release.wait(10)  # an automount waiting for a drive that is not there
            return real_stat(tempfile.gettempdir())

        with mock.patch.object(server.os, "stat", stat):
            started = time.monotonic()
            slow = server.unresponsive_folders(["/mnt/asleep", tempfile.gettempdir()], timeout=0.5)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(slow, ["/mnt/asleep"])


class AmdEnvTests(unittest.TestCase):
    """The AMD override only when ComfyUI computes on the AMD card (issue #12: RTX 5090 + Radeon 610M)."""

    def amd(self, build="", nvidia=False, extra="", platform="linux"):
        env = mock.Mock(return_value={"HSA_OVERRIDE_GFX_VERSION": "10.3.0"})
        got = hwmod.comfy_amd_env(
            "/c/venv/bin/python",
            extra,
            platform,
            build=lambda _p: build,
            nvidia=lambda: nvidia,
            native=lambda _p: set(),
            env=env,
        )
        return got

    def test_nvidia_with_amd_igpu_gets_nothing(self):
        env, why = self.amd(build="", nvidia=True)
        self.assertEqual(env, {})
        self.assertIn("NVIDIA", why)
        env, why = self.amd(build="cuda", nvidia=True)
        self.assertEqual(env, {})
        self.assertIn("CUDA", why)

    def test_amd_only_or_rocm_pytorch_gets_the_override(self):
        self.assertEqual(self.amd(build="", nvidia=False), ({"HSA_OVERRIDE_GFX_VERSION": "10.3.0"}, ""))
        # the user picked the AMD card (PyTorch for ROCm) although an NVIDIA card is there too
        self.assertEqual(self.amd(build="rocm", nvidia=True), ({"HSA_OVERRIDE_GFX_VERSION": "10.3.0"}, ""))

    def test_processor_and_other_systems_get_nothing(self):
        self.assertEqual(self.amd(build="rocm", extra="--lowvram --cpu")[0], {})
        self.assertEqual(self.amd(build="cpu")[0], {})
        self.assertEqual(self.amd(build="xpu")[0], {})
        self.assertEqual(self.amd(build="rocm", platform="win32"), ({}, ""))

    def test_real_rocm_env_for_a_610m_next_to_nvidia(self):
        with mock.patch.object(hwmod, "amd_gpu_nodes", return_value=[(100306, 512 * 1024**2)]):
            self.assertEqual(
                hwmod.rocm_env(native=set(), environ={}, platform_name="linux"), {"HSA_OVERRIDE_GFX_VERSION": "10.3.0"}
            )  # what was set before, whatever card ComfyUI used
            env, _why = hwmod.comfy_amd_env(
                "/c/venv/bin/python", "", "linux", build=lambda _p: "", nvidia=lambda: True, native=lambda _p: set()
            )
        self.assertEqual(env, {})

    def make_venv(self, root, torch_name):
        site = Path(root) / "venv" / "lib" / "python3.12" / "site-packages"
        (site / torch_name).mkdir(parents=True)
        (site / "torchvision-0.20.1+rocm6.2.dist-info").mkdir()
        return Path(root) / "venv" / "bin" / "python"

    def test_torch_build(self):
        for name, kind in (
            ("torch-2.5.1+rocm6.2.dist-info", "rocm"),
            ("torch-2.9.0+cu128.dist-info", "cuda"),
            ("torch-2.9.0+cpu.dist-info", "cpu"),
            ("torch-2.9.0+xpu.dist-info", "xpu"),
            ("torch-2.9.0.dist-info", ""),  # PyPI's build (CUDA on Linux) has no local part
        ):
            with tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(hwmod.torch_build(self.make_venv(tmp, name)), kind, name)
        self.assertEqual(hwmod.torch_build(None), "")
        self.assertEqual(hwmod.torch_build("/nonexistent/venv/bin/python"), "")

    def test_nvidia_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            drm, proc = Path(tmp) / "drm", Path(tmp) / "proc"
            for card, vendor in (("card0", "0x1002"), ("card0-eDP-1", "0x10de"), ("card1", "0x10de")):
                (drm / card / "device").mkdir(parents=True)
                (drm / card / "device" / "vendor").write_text(vendor + "\n")
            self.assertTrue(hwmod.nvidia_present(drm, proc))
            (drm / "card1" / "device" / "vendor").write_text("0x8086\n")
            self.assertFalse(hwmod.nvidia_present(drm, proc))  # card0-eDP-1 is a connector, not a card
            (proc / "0000:01:00.0").mkdir(parents=True)  # the NVIDIA driver lists a card
            self.assertTrue(hwmod.nvidia_present(drm, proc))
            self.assertFalse(hwmod.nvidia_present(Path(tmp) / "none", Path(tmp) / "none"))


if __name__ == "__main__":
    unittest.main()
