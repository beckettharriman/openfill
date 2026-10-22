# SPDX-License-Identifier: Apache-2.0
"""The release zip holds what a user needs and nothing else."""

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import make_release_zip  # noqa: E402


class ReleaseZipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.zip = make_release_zip.build(Path(self.tmp.name), "9.9.9")
        self.names = zipfile.ZipFile(self.zip).namelist()

    def tearDown(self):
        self.tmp.cleanup()

    def test_single_top_folder_with_the_needed_files(self):
        self.assertEqual(self.zip.name, "openfill-9.9.9.zip")
        for need in (
            "Install OpenFill.cmd",
            "install.ps1",
            "install.sh",
            "README.md",
            "LICENSE",
            "models/catalog.json",
            "models/comfy_setup.json",
            "gimp-openfill/gimp-openfill.py",
            "gimp-openfill/openfill/comfy_setup.py",
        ):
            self.assertIn(need, self.names)

    def test_plugin_folder_is_complete_on_its_own(self):
        for need in (
            "gimp-openfill/gimp-openfill.py",
            "gimp-openfill/openfill/__init__.py",
            "gimp-openfill/models/catalog.json",
            "gimp-openfill/models/comfy_setup.json",
            "gimp-openfill/README.md",
            "gimp-openfill/LICENSE",
        ):
            self.assertIn(need, self.names)

    def test_bundled_catalog_is_found_and_valid(self):
        sys.path.insert(0, str(ROOT / "gimp-openfill"))
        from openfill import catalog

        with tempfile.TemporaryDirectory() as out:
            with zipfile.ZipFile(self.zip) as zf:
                zf.extractall(out)
            plugin = Path(out) / "gimp-openfill"
            self.assertEqual(catalog.catalog_search_paths(plugin)[0], plugin / "models" / "catalog.json")
            self.assertTrue(catalog.load_catalog(plugin_dir=plugin))

    def test_no_tests_evidence_or_caches(self):
        for n in self.names:
            self.assertNotRegex(n, r"/(tests|evidence|screenshots|__pycache__)/")
            self.assertFalse(n.endswith(".pyc"))
            self.assertNotIn("RESUME", n)

    def test_scripts_are_crlf(self):
        data = zipfile.ZipFile(self.zip).read("Install OpenFill.cmd")
        self.assertIn(b"\r\n", data)
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))

    def test_entry_point_and_install_sh_stay_executable(self):
        # GIMP on Linux and macOS does not load a plug-in whose entry point is not executable.
        with zipfile.ZipFile(self.zip) as zf:
            for name in ("gimp-openfill/gimp-openfill.py", "install.sh"):
                # Archive Utility (Finder) applies the mode only with the regular-file type bits set.
                self.assertEqual(zf.getinfo(name).external_attr >> 16, 0o100755, name)
            self.assertEqual(zf.getinfo("README.md").external_attr >> 16, 0o100644)

    def test_version_comes_from_the_package(self):
        self.assertRegex(make_release_zip.read_version(), r"^\d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
