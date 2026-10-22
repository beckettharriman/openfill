# SPDX-License-Identifier: Apache-2.0
"""ComfyUI setup logic: manifest, detection, pre-flight, install sequence (network and tar mocked)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from openfill import catalog, comfy_setup, downloads
from openfill.comfy_setup import SetupError
from openfill.hardware import Hardware

GB = 1024**3
ROOT = Path(__file__).resolve().parent.parent
RAW = json.loads((ROOT / "models" / "comfy_setup.json").read_text(encoding="utf-8"))


def make_portable(path: Path) -> Path:
    (path / "python_embeded").mkdir(parents=True)
    (path / "python_embeded" / "python.exe").write_bytes(b"")
    (path / "ComfyUI" / "custom_nodes").mkdir(parents=True)
    (path / "ComfyUI" / "main.py").write_text("")
    return path


class ManifestTests(unittest.TestCase):
    def test_shipped_manifest_parses(self):
        m = comfy_setup.load_manifest(ROOT / "models" / "comfy_setup.json")
        self.assertTrue(m.archive_url.endswith(m.archive_name))
        self.assertIn(m.version, m.archive_url)
        self.assertEqual(len(m.archive_sha256), 64)
        self.assertGreater(m.archive_size, 1_000_000_000)

    def test_nodes_are_the_catalog_pins(self):
        m = comfy_setup.load_manifest(ROOT / "models" / "comfy_setup.json")
        pins = {(n.repo, n.folder, n.commit) for e in catalog.load_catalog() for n in e.requires_nodes}
        for n in m.nodes:
            if n.folder == "comfyui-inpaint-nodes":
                self.assertIn((n.repo, n.folder, n.commit), pins)

    def _with(self, **changes):
        raw = json.loads(json.dumps(RAW))
        for path, value in changes.items():
            node = raw
            *head, last = path.split(".")
            for k in head:
                node = node[k]
            node[last] = value
        return raw

    def test_rejects_foreign_archive_host(self):
        with self.assertRaises(SetupError):
            comfy_setup.parse_manifest(self._with(**{"comfyui.asset.url": "https://evil.example/a.7z"}))

    def test_rejects_bad_hash_and_unpinned_pip(self):
        with self.assertRaises(SetupError):
            comfy_setup.parse_manifest(self._with(**{"comfyui.asset.sha256": "abc"}))
        with self.assertRaises(SetupError):
            comfy_setup.parse_manifest(self._with(pip=["opencv-python-headless"]))

    def test_rejects_unpinned_node(self):
        raw = self._with()
        raw["nodes"][0]["commit"] = "main"
        with self.assertRaises(SetupError):
            comfy_setup.parse_manifest(raw)

    def test_missing_keys_are_reported(self):
        with self.assertRaises(SetupError):
            comfy_setup.parse_manifest({"comfyui": {}})

    def test_notice_size(self):
        m = comfy_setup.parse_manifest(RAW)
        self.assertTrue(comfy_setup.download_notice(m).startswith("about 1."))


class PathTests(unittest.TestCase):
    def test_default_install_dir(self):
        self.assertEqual(
            comfy_setup.default_install_dir({"LOCALAPPDATA": "C:/Users/a/AppData/Local"}),
            Path("C:/Users/a/AppData/Local/OpenFill/ComfyUI"),
        )

    def test_overall_fraction_is_monotonic(self):
        steps = [
            ("downloading", 0, 10),
            ("downloading", 5, 10),
            ("downloading", 10, 10),
            ("verifying", 1, 1),
            ("unpacking", 0, 0),
            ("nodes", 0, 0),
            ("packages", 1, 1),
        ]
        values = [comfy_setup.overall_fraction(*s) for s in steps]
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[0], 0.0)
        self.assertLess(values[2], values[3] + 1e-9)


class DetectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.env = {
            "APPDATA": str(self.home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(self.home / "AppData" / "Local"),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def found(self):
        return comfy_setup.detect_comfy(self.env, self.home, fixed_roots=(), pinokio_roots=())

    def test_nothing_found(self):
        self.assertEqual(self.found(), [])

    def test_downloads_folder_and_double_nesting(self):
        make_portable(self.home / "Downloads" / "ComfyUI_windows_portable" / "ComfyUI_windows_portable")
        found = self.found()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].path.name, "ComfyUI_windows_portable")
        self.assertEqual(found[0].path.parent.name, "ComfyUI_windows_portable")

    def test_desktop_base_path_from_config(self):
        base = make_portable(self.home / "Elsewhere" / "ComfyUI")
        cfg = Path(self.env["APPDATA"]) / "ComfyUI" / "config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"basePath": str(base)}), encoding="utf-8")
        found = self.found()
        self.assertEqual([(c.path, c.source) for c in found], [(base, "ComfyUI Desktop")])

    def test_desktop_base_path_without_comfy_is_ignored(self):
        base = self.home / "Docs" / "ComfyUI"
        base.mkdir(parents=True)
        cfg = Path(self.env["APPDATA"]) / "ComfyUI" / "config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"basePath": str(base)}), encoding="utf-8")
        self.assertEqual(self.found(), [])

    def test_stability_matrix_default_and_moved_library(self):
        pkg = self.home / "AppData" / "Roaming" / "StabilityMatrix" / "Data" / "Packages" / "ComfyUI"
        (pkg / "venv" / "Scripts").mkdir(parents=True)
        (pkg / "venv" / "Scripts" / "python.exe").write_bytes(b"")
        (pkg / "main.py").write_text("")
        self.assertEqual([c.source for c in self.found()], ["Stability Matrix"])

    def test_stability_matrix_library_path(self):
        lib = self.home / "Lib"
        pkg = lib / "Data" / "Packages" / "ComfyUI"
        (pkg / "venv" / "Scripts").mkdir(parents=True)
        (pkg / "venv" / "Scripts" / "python.exe").write_bytes(b"")
        (pkg / "main.py").write_text("")
        cfg = Path(self.env["APPDATA"]) / "StabilityMatrix" / "library.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"LibraryPath": str(lib)}), encoding="utf-8")
        self.assertEqual([c.path for c in self.found()], [pkg])

    def test_pinokio(self):
        app = self.home / "pinokio" / "api" / "comfy.git" / "app"
        (app / "env" / "Scripts").mkdir(parents=True)
        (app / "env" / "Scripts" / "python.exe").write_bytes(b"")
        (app / "main.py").write_text("")
        self.assertEqual([(c.path, c.source) for c in self.found()], [(app, "Pinokio")])

    def test_openfill_install_is_listed_first_and_not_duplicated(self):
        make_portable(Path(self.env["LOCALAPPDATA"]) / "OpenFill" / "ComfyUI" / "ComfyUI_windows_portable")
        make_portable(self.home / "Desktop" / "ComfyUI_windows_portable")
        found = self.found()
        self.assertEqual([c.source for c in found], ["installed by OpenFill", "portable folder"])

    def test_checkout_in_home_folder(self):
        # The layout macOS and Linux users get from git clone + python3 -m venv venv.
        root = self.home / "ComfyUI"
        (root / "venv" / "bin").mkdir(parents=True)
        (root / "venv" / "bin" / "python3").write_bytes(b"")
        (root / "main.py").write_text("")
        self.assertEqual([(c.path, c.source) for c in self.found()], [(root, "ComfyUI folder")])

    def test_stability_matrix_on_macos(self):
        root = self.home / "Library" / "Application Support" / "StabilityMatrix" / "Packages" / "ComfyUI"
        (root / "venv" / "bin").mkdir(parents=True)
        (root / "venv" / "bin" / "python3").write_bytes(b"")
        (root / "main.py").write_text("")
        self.assertEqual([(c.path, c.source) for c in self.found()], [(root, "Stability Matrix")])

    def test_stability_matrix_on_linux(self):
        root = self.home / ".local" / "share" / "StabilityMatrix" / "Packages" / "ComfyUI"
        (root / "venv" / "bin").mkdir(parents=True)
        (root / "venv" / "bin" / "python3").write_bytes(b"")
        (root / "main.py").write_text("")
        self.assertEqual([(c.path, c.source) for c in self.found()], [(root, "Stability Matrix")])

    def test_desktop_config_on_macos_and_linux(self):
        for folder in (self.home / "Library" / "Application Support", self.home / ".config"):
            with self.subTest(folder=folder):
                base = make_portable(self.home / folder.name / "Desk")
                cfg = folder / "ComfyUI" / "config.json"
                cfg.parent.mkdir(parents=True)
                cfg.write_text(json.dumps({"basePath": str(base)}), encoding="utf-8")
                self.assertIn((base, "ComfyUI Desktop"), [(c.path, c.source) for c in self.found()])

    def test_broken_config_json_does_not_raise(self):
        cfg = Path(self.env["APPDATA"]) / "ComfyUI" / "config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.found(), [])


class PreflightTests(unittest.TestCase):
    m = comfy_setup.parse_manifest(RAW)

    def run_pf(self, adapters, hw=None, free=50 * GB, port_in_use=False):
        hw = hw or Hardware()
        return comfy_setup.preflight(self.m, hw, adapters, free, Path("C:/x"), 8188, port_in_use, platform="win32")

    def test_macos_blocks_the_windows_download(self):
        pf = comfy_setup.preflight(self.m, Hardware(), None, 50 * GB, Path("/x"), 8188, False, platform="darwin")
        self.assertTrue(pf.blocked)
        self.assertIn("does not run on macOS", pf.text())
        self.assertIn(comfy_setup.MANUAL_INSTALL_MAC, pf.text())
        self.assertIn("I already have ComfyUI", pf.text())

    def test_linux_blocks_the_windows_download(self):
        pf = comfy_setup.preflight(self.m, Hardware(), None, 50 * GB, Path("/x"), 8188, False, platform="linux")
        self.assertTrue(pf.blocked)
        self.assertIn("does not run on Linux", pf.text())
        self.assertIn(comfy_setup.MANUAL_INSTALL_LINUX, pf.text())

    def nvidia(self, vram=8):
        return Hardware(gpu_name="NVIDIA GeForce RTX 4070", vram_bytes=vram * GB, gpu_source="nvidia-smi")

    def test_nvidia_ok(self):
        pf = self.run_pf(["NVIDIA GeForce RTX 4070"], self.nvidia())
        self.assertEqual(pf.gpu, "nvidia")
        self.assertFalse(pf.needs_cpu_choice)
        self.assertFalse(pf.blocked)
        self.assertIn("RTX 4070", pf.text())
        self.assertIn("8 GB", pf.text())

    def test_low_vram_warns(self):
        pf = self.run_pf(["NVIDIA GT 1030"], self.nvidia(2))
        self.assertEqual(pf.checks[0].level, "warn")
        self.assertIn("2 GB", pf.checks[0].message)

    def test_amd_points_at_instructions_and_is_cpu_route(self):
        pf = self.run_pf(["AMD Radeon RX 7800 XT"])
        self.assertEqual(pf.gpu, "other")
        self.assertTrue(pf.needs_cpu_choice)
        self.assertIn(self.m.amd_instructions, pf.text())
        self.assertIn("very slow", pf.text())

    def test_no_gpu_says_very_slow(self):
        pf = self.run_pf(["Microsoft Basic Display Adapter"])
        self.assertEqual(pf.gpu, "none")
        self.assertIn("very slow", pf.text())

    def test_unknown_gpu_is_not_silently_nvidia(self):
        pf = self.run_pf(None)
        self.assertEqual(pf.gpu, "unknown")
        self.assertTrue(pf.needs_cpu_choice)

    def test_nvidia_from_adapter_name_alone(self):
        self.assertEqual(comfy_setup.gpu_kind(["NVIDIA RTX A2000"], False), "nvidia")

    def test_low_disk_blocks(self):
        pf = self.run_pf(["NVIDIA X"], self.nvidia(), free=5 * GB)
        self.assertTrue(pf.blocked)
        self.assertIn("5.0 GB are free", pf.text())

    def test_unknown_disk_warns_only(self):
        pf = self.run_pf(["NVIDIA X"], self.nvidia(), free=None)
        self.assertFalse(pf.blocked)

    def test_port_in_use(self):
        pf = self.run_pf(["NVIDIA X"], self.nvidia(), port_in_use=True)
        self.assertIn("Port 8188 is already in use", pf.text())
        self.assertNotIn("Port 8188 is free", self.run_pf(["NVIDIA X"], self.nvidia(), port_in_use=True).text())

    def test_video_controller_names_parses_output(self):
        r = SimpleNamespace(returncode=0, stdout="NVIDIA A\r\n\r\nIntel B\r\n")
        import sys

        if sys.platform == "win32":
            self.assertEqual(comfy_setup.video_controller_names(lambda *a, **k: r), ["NVIDIA A", "Intel B"])
            self.assertIsNone(
                comfy_setup.video_controller_names(lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
            )


class FakeProc:
    def __init__(self, on_run, returncode=0, hang=False):
        self.on_run, self.returncode, self.hang = on_run, returncode, hang
        self.killed = False

    def communicate(self, timeout=None):
        import subprocess

        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("tar", timeout)
        if not self.killed:
            self.on_run()
        return "", "boom" if self.returncode else ""

    def kill(self):
        self.killed = True


class RunSetupTests(unittest.TestCase):
    m = comfy_setup.parse_manifest(RAW)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.install = Path(self.tmp.name) / "ComfyUI"
        self.calls = []
        self.downloaded = []
        self.states = []
        self._orig = downloads.download_file
        downloads.download_file = self.fake_download
        self._orig_node = downloads.install_custom_node
        downloads.install_custom_node = self.fake_node

    def tearDown(self):
        downloads.download_file = self._orig
        downloads.install_custom_node = self._orig_node
        self.tmp.cleanup()

    def fake_download(self, url, dest, expected_size=None, expected_sha256=None, progress=None, **kw):
        self.downloaded.append((url, expected_size, expected_sha256, kw.get("min_free_bytes")))
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"7z")
        if progress:
            progress(2, 4, 1.0)

    def fake_node(self, repo, folder, commit, nodes_dir, python, log, runner, cancel, opener):
        self.calls.append(("node", folder, commit, Path(nodes_dir), Path(python)))

    def popen(self, unpack_into=None, rc=0, hang=False):
        def make(cmd, **kw):
            self.calls.append(("tar", cmd))
            dest = Path(cmd[cmd.index("-C") + 1])
            return FakeProc(lambda: make_portable(dest / self.m.folder) if unpack_into is None else None, rc, hang)

        return make

    def runner(self, rc=0):
        def run(cmd, **kw):
            self.calls.append(("run", cmd))
            return SimpleNamespace(returncode=rc, stdout="", stderr="pip said no")

        return run

    def go(self, **kw):
        return comfy_setup.run_setup(
            self.m,
            self.install,
            lambda s, d, t: self.states.append(s),
            kw.pop("cancel", None),
            None,
            kw.pop("runner", self.runner()),
            tar_exe="tar.exe",
            popen=kw.pop("popen", self.popen()),
        )

    def test_full_sequence(self):
        comfy = self.go()
        self.assertEqual(comfy, self.install / "ComfyUI_windows_portable")
        url, size, sha, _free = self.downloaded[0]
        self.assertEqual((url, size, sha), (self.m.archive_url, self.m.archive_size, self.m.archive_sha256))
        self.assertEqual(self.states[0], "downloading")
        self.assertEqual(
            [s for i, s in enumerate(self.states) if s not in self.states[:i]],
            ["downloading", "verifying", "unpacking", "nodes", "packages"],
        )
        tar = next(c for c in self.calls if c[0] == "tar")[1]
        self.assertEqual(tar[:2], ["tar.exe", "-xf"])
        node = next(c for c in self.calls if c[0] == "node")
        self.assertEqual(node[1], "comfyui-inpaint-nodes")
        self.assertEqual(node[3], comfy / "ComfyUI" / "custom_nodes")
        self.assertEqual(node[4], comfy / "python_embeded" / "python.exe")
        pip = next(c for c in self.calls if c[0] == "run")[1]
        self.assertEqual(pip[1:6], ["-s", "-m", "pip", "install", "--no-warn-script-location"])
        self.assertIn("opencv-python-headless==5.0.0.93", pip)
        self.assertFalse((self.install / ".download").exists())
        self.assertFalse((self.install / ".unpack").exists())

    def test_second_run_skips_download_and_unpack(self):
        self.go()
        self.downloaded.clear()
        self.calls.clear()
        self.go()
        self.assertEqual(self.downloaded, [])
        self.assertFalse(any(c[0] == "tar" for c in self.calls))

    def test_tar_failure_is_reported(self):
        with self.assertRaises(SetupError) as cm:
            self.go(popen=self.popen(rc=1))
        self.assertIn("Unpacking", str(cm.exception))
        # the cut-short unpack is flagged, so the next run removes the partial folder and unpacks again
        self.assertTrue((self.install / comfy_setup.UNPACK_MARKER).exists())
        self.calls.clear()
        comfy = self.go()
        self.assertFalse((self.install / comfy_setup.UNPACK_MARKER).exists())
        self.assertTrue(any(c[0] == "tar" for c in self.calls))
        self.assertEqual(comfy, self.install / "ComfyUI_windows_portable")

    def test_cancel_during_unpack_kills_tar(self):
        with self.assertRaises(downloads.DownloadError) as cm:
            self.go(popen=self.popen(hang=True), cancel=lambda: True)
        self.assertEqual(cm.exception.code, "cancelled")

    def test_archive_without_expected_folder(self):
        def make(cmd, **kw):
            return FakeProc(lambda: Path(cmd[cmd.index("-C") + 1]).joinpath("other").mkdir(parents=True))

        with self.assertRaises(SetupError):
            self.go(popen=make)

    def test_pip_failure_is_reported(self):
        with self.assertRaises(SetupError) as cm:
            self.go(runner=self.runner(rc=1))
        self.assertIn("pip said no", str(cm.exception))

    def test_missing_tar_is_explained(self):
        import os

        old = os.environ.get("SYSTEMROOT")
        os.environ["SYSTEMROOT"] = self.tmp.name  # no System32\tar.exe there
        try:
            with self.assertRaises(SetupError) as cm:
                comfy_setup.extract_archive(Path("a.7z"), Path(self.tmp.name) / "x")
        finally:
            if old is None:
                os.environ.pop("SYSTEMROOT", None)
            else:
                os.environ["SYSTEMROOT"] = old
        self.assertIn("7-Zip", str(cm.exception))


if __name__ == "__main__":
    unittest.main()


class SupervisorSetupTaskTests(unittest.TestCase):
    def setUp(self):
        from openfill import supervisor
        from openfill.config import Settings

        self.sup = supervisor
        self.tmp = tempfile.TemporaryDirectory()
        self.profile = Path(self.tmp.name) / "openfill"
        self.profile.mkdir()
        self.cfg = self.profile / "config.json"
        Settings(comfy_dir=r"C:\nowhere").save(self.cfg)
        self.worker = supervisor.TaskWorker(self.profile, self.cfg, supervisor.Logger(self.profile / "t.log", "t"))
        self._orig = comfy_setup.run_setup

    def tearDown(self):
        comfy_setup.run_setup = self._orig
        self.tmp.cleanup()

    def task(self, **extra):
        return {"id": "t1", "type": "setup_comfy", "install_dir": str(Path(self.tmp.name) / "ComfyUI"), **extra}

    def test_success_saves_comfy_dir_and_cpu_flag(self):
        from openfill.config import Settings

        target = Path(self.tmp.name) / "ComfyUI" / "ComfyUI_windows_portable"

        def fake(manifest, install_dir, progress, cancel, log, popen=None):
            progress("downloading", 5, 10)
            return target

        comfy_setup.run_setup = fake
        self.worker._setup_comfy(self.task(cpu=True))
        s = Settings.load(self.cfg, backup=False)
        self.assertEqual(s.comfy_dir, str(target))
        self.assertIn("--cpu", s.extra_server_args)
        done = self.worker.status["history"][0]
        self.assertEqual((done["state"], done["comfy_dir"]), ("done", str(target)))

    def test_relative_install_dir_is_refused(self):
        with self.assertRaises(SetupError):
            self.worker._setup_comfy(self.task(install_dir="relative/x"))

    def test_progress_status_fields(self):
        seen = []

        def fake(manifest, install_dir, progress, cancel, log, popen=None):
            progress("downloading", 5, 10)
            seen.append(dict(self.worker.status["current"]))
            return Path(self.tmp.name)

        comfy_setup.run_setup = fake
        self.worker._setup_comfy(self.task())
        self.assertEqual(seen[0]["type"], "setup_comfy")
        self.assertAlmostEqual(seen[0]["fraction"], 0.275)


@unittest.skipUnless(sys.platform == "win32", "install.ps1 is the Windows installer")
class InstallScriptDetectTests(unittest.TestCase):
    """install.ps1 -DetectComfy finds the same layouts as comfy_setup.detect_comfy."""

    def run_detect(self, home: Path) -> str:
        env = dict(os.environ)
        env.update(
            USERPROFILE=str(home),
            APPDATA=str(home / "AppData" / "Roaming"),
            LOCALAPPDATA=str(home / "AppData" / "Local"),
            OPENFILL_NO_FIXED_ROOTS="1",
        )
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "install.ps1"),
                "-DetectComfy",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_none_then_downloads_stability_matrix_and_desktop(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertIn("No ComfyUI found", self.run_detect(home))
            make_portable(home / "Downloads" / "ComfyUI_windows_portable")
            sm = home / "AppData" / "Roaming" / "StabilityMatrix" / "Data" / "Packages" / "ComfyUI"
            (sm / "venv" / "Scripts").mkdir(parents=True)
            (sm / "venv" / "Scripts" / "python.exe").write_bytes(b"")
            (sm / "main.py").write_text("")
            desk = make_portable(home / "Elsewhere")
            cfg = home / "AppData" / "Roaming" / "ComfyUI" / "config.json"
            cfg.parent.mkdir(parents=True)
            cfg.write_text(json.dumps({"basePath": str(desk)}), encoding="utf-8")
            out = self.run_detect(home)
            self.assertIn("[portable folder]", out)
            self.assertIn("[Stability Matrix]", out)
            self.assertIn("[ComfyUI Desktop]", out)
            expected = comfy_setup.detect_comfy(
                {"APPDATA": str(home / "AppData" / "Roaming"), "LOCALAPPDATA": str(home / "AppData" / "Local")},
                home,
                fixed_roots=(),
                pinokio_roots=(),
            )
            self.assertEqual(len(out.strip().splitlines()), len(expected))


class UnpackProgressTests(unittest.TestCase):
    def test_folder_bytes_counts_nested_files_and_missing_is_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a" / "b").mkdir(parents=True)
            (root / "a" / "x.bin").write_bytes(b"12345")
            (root / "a" / "b" / "y.bin").write_bytes(b"123")
            self.assertEqual(comfy_setup.folder_bytes(root), 8)
            self.assertEqual(comfy_setup.folder_bytes(root / "nope"), 0)

    def test_unpacking_fraction_follows_bytes(self):
        lo, hi = comfy_setup.STATE_SPAN["unpacking"]
        self.assertEqual(comfy_setup.overall_fraction("unpacking", 0, 100), lo)
        self.assertAlmostEqual(comfy_setup.overall_fraction("unpacking", 50, 100), (lo + hi) / 2)
        self.assertEqual(comfy_setup.overall_fraction("unpacking", 500, 100), hi)  # never past the span

    def test_extract_calls_tick_while_waiting(self):
        ticks = []
        proc = FakeProc(lambda: None, hang=True)
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] >= 3

        with self.assertRaises(downloads.DownloadError):
            comfy_setup.extract_archive(
                Path("a.7z"),
                Path(tempfile.gettempdir()) / "openfill-test-x",
                "tar.exe",
                cancel,
                lambda *a, **k: proc,
                lambda: ticks.append(1),
            )
        self.assertEqual(len(ticks), 3)


class OlderGpuBuildTests(unittest.TestCase):
    """Issue #2: a GTX 10 card needs the CUDA 12.6 build; the standard one's PyTorch starts at sm_75."""

    m = comfy_setup.parse_manifest(RAW)

    def hw(self, cap, name="NVIDIA GeForce GTX 1060 with Max-Q Design"):
        return Hardware(gpu_name=name, vram_bytes=6 * GB, gpu_source="nvidia-smi", compute_capability=cap)

    def pf(self, hw):
        return comfy_setup.preflight(
            self.m, hw, ["NVIDIA GeForce"], 50 * GB, Path("C:/x"), 8188, False, platform="win32"
        )

    def test_manifest_has_the_pinned_build_for_older_cards(self):
        older = self.m.for_older_gpus()
        self.assertEqual(older.archive_name, "ComfyUI_windows_portable_nvidia_cu126.7z")
        self.assertEqual(older.archive_size, 1895335518)
        self.assertEqual(older.folder, self.m.folder)
        self.assertEqual((older.nodes, older.pip), (self.m.nodes, self.m.pip))

    def test_pascal_gets_it_and_turing_does_not(self):
        pascal = self.pf(self.hw(6.1))
        self.assertTrue(pascal.older_gpu)
        self.assertIn("older than the RTX 20 series", pascal.text())
        for cap in (7.5, 8.9, None):
            with self.subTest(cap=cap):
                self.assertFalse(self.pf(self.hw(cap, "NVIDIA GeForce RTX 4070")).older_gpu)

    def test_unofficial_url_for_older_cards_is_refused(self):
        bad = json.loads(json.dumps(RAW))
        bad["comfyui"]["older_gpus"]["asset"]["url"] = "https://example.com/ComfyUI_windows_portable_nvidia_cu126.7z"
        with self.assertRaises(comfy_setup.SetupError):
            comfy_setup.parse_manifest(bad)
