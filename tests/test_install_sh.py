# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""install.sh (Linux, Flatpak, macOS) against a throw-away home and GIMP profile."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from openfill import comfy_setup

ROOT = Path(__file__).resolve().parents[1]


def make_checkout(path: Path) -> Path:
    """A ComfyUI git checkout with a venv, as `git clone` + `python3 -m venv venv` leaves it."""
    (path / "venv" / "bin").mkdir(parents=True)
    python = path / "venv" / "bin" / "python3"
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    (path / "main.py").write_text("")
    return path


@unittest.skipIf(sys.platform == "win32" or shutil.which("sh") is None, "install.sh needs a POSIX shell")
class InstallShTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.profile = Path(self.tmp.name) / "profile"
        self.config = self.profile / "openfill" / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_sh(self, *args: str) -> subprocess.CompletedProcess:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home)}
        r = subprocess.run(
            ["sh", str(ROOT / "install.sh"), "--profile-dir", str(self.profile), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_copies_plug_in_and_writes_config_without_comfy(self):
        r = self.run_sh()
        target = self.profile / "plug-ins" / "gimp-openfill"
        self.assertTrue((target / "gimp-openfill.py").is_file())
        self.assertTrue(os.access(target / "gimp-openfill.py", os.X_OK))
        self.assertTrue((target / "models" / "catalog.json").is_file())
        data = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertNotIn("comfy_dir", data)  # nothing found: the plug-in asks for it later
        self.assertNotIn("checkpoint", data)  # models come from the plug-in defaults and the Models tab
        self.assertEqual(data["url"], "http://127.0.0.1:8188")
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertIn("Install ComfyUI", r.stdout)

    def test_finds_checkout_in_home_folder(self):
        comfy = make_checkout(self.home / "ComfyUI")
        r = self.run_sh()
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["comfy_dir"], str(comfy))
        self.assertIn(f"Found ComfyUI: {comfy}", r.stdout)
        self.assertNotIn("Install ComfyUI", r.stdout)
        # The plug-in finds the same folder when it starts without a config.
        found = comfy_setup.detect_comfy({}, self.home, fixed_roots=(), pinokio_roots=())
        self.assertEqual([c.path for c in found], [comfy])

    def test_comfy_dir_is_json_escaped(self):
        comfy = make_checkout(Path(self.tmp.name) / 'my "AI" stuff\\Comfy')
        self.run_sh("--comfy-dir", str(comfy))
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["comfy_dir"], str(comfy))

    def test_missing_comfy_dir_warns(self):
        r = self.run_sh("--comfy-dir", str(self.home / "nope"))
        self.assertIn("no usable ComfyUI", r.stderr)

    def test_keeps_existing_config(self):
        comfy = make_checkout(self.home / "elsewhere")
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({"comfy_dir": str(comfy), "quality": "quality"}), encoding="utf-8")
        r = self.run_sh()
        self.assertIn("Keeping existing config", r.stdout)
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["quality"], "quality")
        self.assertNotIn("no usable ComfyUI", r.stderr)

    def test_uninstall_keeps_settings_unless_asked(self):
        self.run_sh()
        self.run_sh("--uninstall")
        self.assertFalse((self.profile / "plug-ins" / "gimp-openfill").exists())
        self.assertTrue(self.config.is_file())
        self.run_sh("--uninstall", "--remove-config")
        self.assertFalse(self.config.parent.exists())


if __name__ == "__main__":
    unittest.main()
