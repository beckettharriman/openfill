# SPDX-License-Identifier: Apache-2.0
"""One copy of the OpenFill core: gimp-openfill/openfill. The PhotoCraft companion imports it
from there in the repository, and its release zip carries a copy so it works on its own."""

import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import make_photocraft_zip  # noqa: E402

CORE = ROOT / "gimp-openfill" / "openfill"


def core_files() -> set[str]:
    return {
        p.relative_to(CORE).as_posix()
        for p in CORE.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".pyo")
    }


class SingleCoreTests(unittest.TestCase):
    def test_no_second_copy_of_the_core_in_the_repository(self):
        skip = {".git", "__pycache__", "dist", "dist-smoke", "docs"}
        copies = [
            p.parent.relative_to(ROOT).as_posix()
            for p in ROOT.rglob("openfill/app.py")
            if not skip & set(p.relative_to(ROOT).parts)
        ]
        self.assertEqual(copies, ["gimp-openfill/openfill"])


class PhotoCraftZipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.zip = make_photocraft_zip.build(Path(cls.tmp.name))
        cls.top = cls.zip.stem
        with zipfile.ZipFile(cls.zip) as zf:
            cls.names = {n[len(cls.top) + 1 :] for n in zf.namelist()}
            zf.extractall(cls.tmp.name)
        cls.unpacked = Path(cls.tmp.name) / cls.top

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_carries_the_core_without_the_gimp_only_modules(self):
        want = {f for f in core_files() if Path(f).name not in make_photocraft_zip.GIMP_ONLY}
        have = {n[len("openfill/") :] for n in self.names if n.startswith("openfill/")}
        self.assertEqual(have, want)
        self.assertIn("comfy_nodes/__init__.py", have)  # the LaMa node goes into ComfyUI from here
        self.assertNotIn("gimp_io.py", have)

    def test_needed_files(self):
        for need in (
            "LICENSE",
            "README.md",
            "CHANGELOG.md",
            "install.sh",
            "Install OpenFill for PhotoCraft.cmd",
            "openfill-photocraft.sh",
            "models/catalog.json",
            "models/comfy_setup.json",
            "openfill_photocraft/__main__.py",
        ):
            self.assertIn(need, self.names)
        self.assertFalse([n for n in self.names if "/tests/" in n or "/e2e/" in n or n.endswith(".pyc")])

    def test_the_core_copy_is_the_repository_core(self):
        for rel in ("app.py", "supervisor.py", "comfy_setup.py"):
            self.assertEqual((self.unpacked / "openfill" / rel).read_bytes(), (CORE / rel).read_bytes())

    def test_imports_from_the_unpacked_folder_alone(self):
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "import openfill.app, openfill.comfy_setup, openfill_photocraft.install; "
            "import pathlib; root = pathlib.Path(sys.argv[1]).resolve(); "
            "assert pathlib.Path(openfill.app.__file__).resolve().is_relative_to(root), openfill.app.__file__"
        )
        r = subprocess.run(
            [sys.executable, "-I", "-c", code, str(self.unpacked)],
            cwd=self.tmp.name,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(r.returncode, 0, r.stderr)


class ReleaseTitleTests(unittest.TestCase):
    """OpenFill's 0.2.x was read as a PhotoCraft version: the title names both."""

    def test_title_names_openfill_and_the_photocraft_range(self):
        sys.path.insert(0, str(ROOT / "photocraft"))
        from openfill_photocraft import PHOTOCRAFT_TESTED, __version__

        title = make_photocraft_zip.release_title()
        oldest, newest = PHOTOCRAFT_TESTED
        self.assertEqual(title, f"OpenFill for PhotoCraft {__version__} (works with PhotoCraft {oldest} to {newest})")
        self.assertEqual(make_photocraft_zip.photocraft_range(), PHOTOCRAFT_TESTED)

    def test_title_reads_a_tree_of_its_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            init = Path(tmp) / "photocraft" / "openfill_photocraft" / "__init__.py"
            init.parent.mkdir(parents=True)
            init.write_text('__version__ = "9.8.7"\nPHOTOCRAFT_TESTED = ("0.30", "0.41")\n', encoding="utf-8")
            self.assertEqual(
                make_photocraft_zip.release_title(Path(tmp)),
                "OpenFill for PhotoCraft 9.8.7 (works with PhotoCraft 0.30 to 0.41)",
            )


class DocsTests(unittest.TestCase):
    """The PhotoCraft range in the docs is the one the panel shows (issue #20: users on 0.21 and
    newer read "0.12 to 0.20" as "not supported"), and each app's download link is its own."""

    DOCS = ("README.md", "photocraft/README.md", "docs/install.md")

    def setUp(self):
        sys.path.insert(0, str(ROOT / "photocraft"))
        from openfill_photocraft import PHOTOCRAFT_TESTED, __version__

        self.oldest, self.newest = PHOTOCRAFT_TESTED
        self.version = __version__

    def test_every_doc_gives_the_tested_range(self):
        import re

        want = f"{self.oldest} to {self.newest}"
        for name in self.DOCS:
            text = (ROOT / name).read_text(encoding="utf-8")
            ranges = set(re.findall(r"works with PhotoCraft (\d+\.\d+ to \d+\.\d+)", text))
            self.assertEqual(ranges, {want}, name)
        badge = f"works%20with%20PhotoCraft-{self.oldest}%20to%20{self.newest}"
        self.assertIn(badge, (ROOT / "README.md").read_text(encoding="utf-8"))

    def test_the_readme_links_each_app_to_its_own_release(self):
        sys.path.insert(0, str(ROOT / "gimp-openfill"))
        from openfill import __version__ as gimp

        text = (ROOT / "README.md").read_text(encoding="utf-8")
        base = "https://github.com/beckettharriman/openfill/releases"
        photocraft = f"{base}/download/photocraft-v{self.version}/openfill-photocraft-{self.version}.zip"
        self.assertIn(photocraft, text)
        self.assertIn(f"{base}/download/v{gimp}/openfill-{gimp}.zip", text)
        # the PhotoCraft steps come first and never send people to the GIMP release
        pc_steps = text[text.index("**PhotoCraft: OpenFill for PhotoCraft") : text.index("**GIMP 3.2**")]
        self.assertNotIn(f"/v{gimp}", pc_steps)
        self.assertIn(f"{base}/tag/photocraft-v{self.version}", pc_steps)


if __name__ == "__main__":
    unittest.main()
