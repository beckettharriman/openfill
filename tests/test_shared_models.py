# SPDX-License-Identifier: Apache-2.0
"""Models of another ComfyUI install, used read only (issue #6, shared_models.py): detection,
model folders, the yaml for ComfyUI, the catalog, and the guarantee that nothing is written there."""

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openfill import catalog as cat
from openfill import downloads, supervisor
from openfill import local_models as lm
from openfill import shared_models as sm
from openfill.backend import BackendError
from openfill.config import Settings
from tests.test_local_models import (
    CLIP_L,
    FLUX_FILL,
    FLUX_VAE,
    SD15_PLAIN,
    SDXL,
    T5_GGUF,
    T5_ST,
    write_gguf,
    write_safetensors,
)

ENTRIES = cat.load_catalog()


def sized(path: Path, size: int) -> Path:
    """Pad a fake file to the catalog's size (sparse: costs nothing)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(b"x")
    with open(path, "r+b") as fh:
        fh.truncate(size)
    return path


def catalog_file(model_id: str, role: str) -> cat.FileSpec:
    return cat.by_id(ENTRIES, model_id).file_by_role(role)


def make_krita(server: Path) -> Path:
    """A Krita AI Diffusion managed server as its installer lays it out (server.py, models.json):
    ComfyUI/ with main.py, custom_nodes and its extra_model_paths.yaml, and models/ next to it."""
    comfy = server / "ComfyUI"
    (comfy / "custom_nodes" / "comfyui-inpaint-nodes").mkdir(parents=True)
    (comfy / "main.py").write_text("# ComfyUI\n")
    (comfy / "extra_model_paths.yaml").write_text(
        "krita-managed:\n    base_path: ../models\n    checkpoints: checkpoints\n"
    )
    (comfy / "models" / "checkpoints").mkdir(parents=True)
    (server / ".version").write_text("1.40.0 cuda\n")
    models = server / "models"
    for name in sm.MODEL_FOLDERS:
        if name not in ("unet", "clip", "vae_approx"):
            (models / name).mkdir(parents=True, exist_ok=True)
    sized(models / "checkpoints" / "RealVisXL_V5.0_fp16.safetensors", catalog_file("realvisxl-v5", "checkpoint").size)
    for role in ("fooocus_head", "fooocus_patch"):
        f = catalog_file("fooocus-inpaint-patch", role)
        sized(models / f.dest, f.size)
    mat = catalog_file("mat-places512", "inpaint_model")
    sized(models / mat.dest, mat.size)
    write_safetensors(models / "text_encoders" / "clip_l.safetensors", CLIP_L)
    t5 = catalog_file("flux1-fill-dev-gguf-q4", "clip1")
    sized(write_gguf(models / "text_encoders" / t5.filename, T5_GGUF), t5.size)
    write_safetensors(models / "vae" / "flux_vae.safetensors", FLUX_VAE)
    write_safetensors(models / "vae" / "flux2-vae.safetensors", {"decoder.conv_in.weight": [512, 32, 3, 3]})
    write_safetensors(models / "checkpoints" / "dreamshaper_8.safetensors", SD15_PLAIN)  # not usable
    write_safetensors(models / "checkpoints" / "myXL.safetensors", SDXL)  # an own SDXL model
    return server


def snapshot(root: Path) -> dict:
    """Everything that could change under ``root``: every path (files, folders, links) with its
    type, size, modification time, mode, link target and, for files, the SHA-256. Access times
    are left out: reading a file changes them."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames + filenames):
            p = Path(dirpath) / name
            st = p.lstat()
            entry = {"mode": st.st_mode, "size": st.st_size, "mtime": st.st_mtime_ns}
            if stat.S_ISLNK(st.st_mode):
                entry["link"] = os.readlink(p)
            elif stat.S_ISREG(st.st_mode):
                # the fixtures are headers padded with sparse zeros to the catalog sizes: the
                # written part is at the start, so the first and last MB stand for the content
                h = hashlib.sha256()
                with open(p, "rb") as fh:
                    h.update(fh.read(1 << 20))
                    if st.st_size > 2 << 20:
                        fh.seek(-(1 << 20), os.SEEK_END)
                    h.update(fh.read(1 << 20))
                entry["sha256"] = h.hexdigest()
            out[p.relative_to(root).as_posix()] = entry
    st = root.lstat()
    out["."] = {"mode": st.st_mode, "mtime": st.st_mtime_ns}
    return out


def _yaml(path: Path):
    """What ComfyUI's loader gets from the file (PyYAML when there, else the '{}' form only)."""
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # noqa: PLC0415 - only to read the text back
    except ImportError:
        body = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
        return {} if body == ["{}"] else text
    return yaml.safe_load(text)


class SettingTests(unittest.TestCase):
    def test_parse_and_join(self):
        sep = os.pathsep
        # typed on one line: split on the list separator
        self.assertEqual(sm.parse(f' /a/b {sep}{sep}"/c d"{sep}/a/b{sep}/e '), ["/a/b", "/c d", "/e"])
        # stored: one folder per line, never split again
        self.assertEqual(sm.parse(f"/a/b\n /c{sep}d \n\n/a/b\n"), ["/a/b", f"/c{sep}d"])
        self.assertEqual(sm.parse(""), [])
        self.assertEqual(sm.parse(None), [])
        self.assertEqual(sm.join(["/a", "/a", " /b "]), "/a\n/b\n")
        self.assertEqual(sm.join([]), "")
        self.assertEqual(sm.parse(sm.join(["/x y", "/z"])), ["/x y", "/z"])
        self.assertEqual(sm.display(sm.join(["/x y", "/z"])), f"/x y{sep}/z")

    def test_a_folder_with_the_list_separator_survives_the_setting(self):
        """Review B5: ':' (Linux, Mac) and ';' (Windows) are legal in folder names."""
        folder = f"/media/me/Krita{os.pathsep} AI/server"
        self.assertEqual(sm.parse(sm.join([folder])), [folder])
        with tempfile.TemporaryDirectory() as tmp:
            odd = Path(tmp) / f"Krita{os.pathsep} AI"
            odd.mkdir()
            other = Path(tmp) / "other"
            other.mkdir()
            typed = sm.display(sm.join([str(odd), str(other)]))  # what a one-line field shows
            self.assertEqual(sm.parse(typed), [str(odd), str(other)])

    def test_check_wants_full_paths_of_existing_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = make_krita(Path(tmp) / "server")
            own = Path(tmp) / "own" / "models"
            own.mkdir(parents=True)
            self.assertEqual(sm.check(str(server), own), ([str(server)], ""))
            self.assertEqual(sm.check("", own), ([], ""))
            for text, words in (
                ("relative/folder", "full path"),
                (str(Path(tmp) / "missing"), "not found"),
                (str(own), "OpenFill's own models folder"),
            ):
                folders, problem = sm.check(text, own)
                self.assertEqual(folders, [], text)
                self.assertIn(words, problem, text)
            with mock.patch.dict(os.environ, {"HOME": tmp, "USERPROFILE": tmp}):
                self.assertEqual(sm.check("~/server", own), ([str(server)], ""))
            if os.name == "posix":  # OpenFill's own models folder linked to Krita's (review B1)
                link = Path(tmp) / "linked" / "models"
                link.parent.mkdir()
                link.symlink_to(server / "models", target_is_directory=True)
                _folders, problem = sm.check(str(server), link)
                self.assertIn("link", problem)

    def test_settings_keep_the_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            Settings(shared_models="/k/server").save(path)
            self.assertEqual(Settings.load(path).shared_models, "/k/server")
            self.assertEqual(Settings().shared_models, "")


class RootsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_krita_server_has_two_roots(self):
        server = make_krita(self.dir / "ai_diffusion" / "server")
        expect = [server / "models", server / "ComfyUI" / "models"]
        self.assertEqual(sm.model_roots(server), expect)
        # Krita's user data folder (one up) works too
        self.assertEqual(sm.model_roots(self.dir / "ai_diffusion"), expect)

    def test_comfy_checkout_portable_and_bare_folder(self):
        checkout = self.dir / "ComfyUI"
        (checkout / "models" / "checkpoints").mkdir(parents=True)
        (checkout / "main.py").write_text("")
        (self.dir / "a1111" / "models").mkdir(parents=True)
        (self.dir / "my models").mkdir()
        (checkout / "extra_model_paths.yaml").write_text(
            "# comment\n"
            "a111:\n"
            "    base_path: ../a1111/models   # trailing comment\n"
            "    checkpoints: Stable-diffusion\n"
            "mine:\n"
            f'    base_path: "{self.dir / "my models"}"\n'
            "#   base_path: /commented/out\n"
            "gone:\n"
            "    base_path: /does/not/exist\n"
        )
        self.assertEqual(
            sm.model_roots(checkout),
            [checkout / "models", checkout / ".." / "a1111" / "models", self.dir / "my models"],
        )
        portable = self.dir / "ComfyUI_windows_portable"
        (portable / "ComfyUI" / "models").mkdir(parents=True)
        (portable / "ComfyUI" / "main.py").write_text("")
        self.assertEqual(sm.model_roots(portable), [portable / "ComfyUI" / "models"])
        self.assertEqual(sm.model_roots(self.dir / "my models"), [self.dir / "my models"])
        self.assertEqual(sm.model_roots(self.dir / "missing"), [])

    def test_roots_for_leaves_out_openfills_own_folder_and_repeats(self):
        server = make_krita(self.dir / "server")
        own = self.dir / "own" / "models"
        own.mkdir(parents=True)
        setting = sm.join([str(server), str(server / "models"), str(own), str(self.dir / "missing")])
        self.assertEqual(sm.roots_for(setting, own), [server / "models", server / "ComfyUI" / "models"])
        self.assertEqual(sm.roots_for("", own), [])

    def test_inside(self):
        server = make_krita(self.dir / "server")
        self.assertTrue(sm.inside(server / "models" / "vae" / "x", [server / "models"]))
        self.assertTrue(sm.inside(server / "models", [server / "models"]))
        self.assertFalse(sm.inside(server / "models2" / "x", [server / "models"]))
        self.assertFalse(sm.inside(self.dir / "own", []))
        if os.name == "posix":
            link = self.dir / "link"
            link.symlink_to(server / "models")
            self.assertTrue(sm.inside(link / "vae" / "y", [server / "models"]))


class DetectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_places_per_platform(self):
        cases = (
            ("linux", {}, self.home / ".local/share/krita/ai_diffusion"),
            ("linux", {"XDG_DATA_HOME": str(self.home / "xdg")}, self.home / "xdg/krita/ai_diffusion"),
            ("linux", {}, self.home / ".var/app/org.kde.krita/data/krita/ai_diffusion"),
            ("win32", {"APPDATA": str(self.home / "Roaming")}, self.home / "Roaming/krita/ai_diffusion"),
            ("darwin", {}, self.home / "Library/Application Support/krita/ai_diffusion"),
        )
        for platform, env, data in cases:
            with self.subTest(platform=platform, data=data), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                data = home / data.relative_to(self.home)
                env = {k: str(home / Path(v).relative_to(self.home)) for k, v in env.items()}
                make_krita(data / "server")
                found = sm.detect_krita(env, home, platform)
                self.assertEqual([(c.path, c.source) for c in found], [(data / "server", sm.KRITA)])

    def test_moved_server_from_settings_with_comments(self):
        data = self.home / ".local/share/krita/ai_diffusion"
        data.mkdir(parents=True)
        moved = make_krita(self.home / "big disk" / "krita-server")
        (data / "settings.json").write_text(
            '// Krita AI Diffusion settings\n{\n  "server_mode": "managed",\n'
            f'  // where it lives\n  "server_path": {json.dumps(str(moved))}\n}}\n'
        )
        self.assertEqual([c.path for c in sm.detect_krita({}, self.home, "linux")], [moved])

    def test_nothing_without_a_server(self):
        data = self.home / ".local/share/krita/ai_diffusion"
        (data / "server" / "models").mkdir(parents=True)  # no ComfyUI/main.py: not installed
        (data / "settings.json").write_text("{not json")
        self.assertEqual(sm.detect_krita({}, self.home, "linux"), [])

    def test_detect_adds_other_comfyui_but_not_openfills_own(self):
        server = make_krita(self.home / ".local/share/krita/ai_diffusion/server")
        others = [
            sm.Candidate(self.home / "own", "installed by OpenFill"),
            sm.Candidate(server / "ComfyUI", "ComfyUI folder"),  # Krita's, already listed
            sm.Candidate(self.home / "AI" / "ComfyUI", "ComfyUI folder"),
        ]
        found = sm.detect(self.home / "own", {}, self.home, "linux", comfy_candidates=others)
        self.assertEqual([c.path for c in found], [server, self.home / "AI" / "ComfyUI"])


class YamlTests(unittest.TestCase):
    def test_yaml_lists_existing_folders_quoted(self):
        with tempfile.TemporaryDirectory() as tmp:
            odd = Path(tmp) / "a #b: $HOME" / "models"
            for name in ("checkpoints", "text_encoders", "inpaint"):
                (odd / name).mkdir(parents=True)
            plain = Path(tmp) / "plain"
            (plain / "vae").mkdir(parents=True)
            text = sm.yaml_text([odd, Path(tmp) / "empty", plain])
            self.assertNotIn("base_path", text)
            lines = [line for line in text.splitlines() if line and not line.startswith("#")]
            self.assertEqual(lines[0], "openfill_shared_1:")
            values = {}
            for line in lines:
                if line.startswith("  "):
                    key, _, value = line.strip().partition(": ")
                    values.setdefault(key, []).append(json.loads(value))
            self.assertEqual(values["checkpoints"], [str((odd / "checkpoints").resolve())])
            self.assertEqual(values["vae"], [str((plain / "vae").resolve())])
            self.assertNotIn("diffusion_models", values)
            self.assertIn("openfill_shared_3:", text)  # numbered by root, the empty one has no section
            self.assertNotIn("openfill_shared_2:", text)

    def test_yaml_round_trips_through_a_yaml_parser(self):
        try:
            import yaml  # noqa: PLC0415 - only to check the text; OpenFill does not need it
        except ImportError:
            self.skipTest("PyYAML is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'we"ird \\ #: $HOME' / "models"
            (root / "checkpoints").mkdir(parents=True)
            data = yaml.safe_load(sm.yaml_text([root]))
            self.assertEqual(data, {"openfill_shared_1": {"checkpoints": str((root / "checkpoints").resolve())}})

    def test_openfills_own_inpaint_folder_comes_first(self):
        """comfyui-inpaint-nodes appends ComfyUI's own models/inpaint after what the yaml gave;
        so the yaml names OpenFill's own first, only when a shared root has an inpaint folder."""
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "own" / "models"
            shared = Path(tmp) / "krita" / "models"
            (shared / "inpaint").mkdir(parents=True)
            (shared / "vae").mkdir()
            text = sm.yaml_text([shared], own)
            lines = [line for line in text.splitlines() if line and not line.startswith("#")]
            self.assertEqual(lines[:2], ["openfill_own:", f"  inpaint: {json.dumps(str(own.resolve() / 'inpaint'))}"])
            self.assertEqual(lines[2], "openfill_shared_1:")
            (shared / "inpaint").rmdir()
            self.assertNotIn("openfill_own", sm.yaml_text([shared], own))

    def test_write_config_goes_to_the_profile_and_removes_a_stale_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "profile"
            server = make_krita(Path(tmp) / "server")
            path = sm.write_config(base, sm.model_roots(server))
            self.assertEqual(path, base / sm.CONFIG_FILENAME)
            self.assertIn(str((server / "models" / "vae").resolve()), path.read_text())
            self.assertIsNone(sm.write_config(base, []))
            self.assertEqual(_yaml(path), {})  # kept as an empty mapping
            self.assertIsNone(sm.write_config(Path(tmp) / "never", []))
            self.assertFalse((Path(tmp) / "never" / sm.CONFIG_FILENAME).exists())  # not made when never used


class CatalogTests(unittest.TestCase):
    """The catalog counts the files of a shared folder as installed, read only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.server = make_krita(self.dir / "server")
        self.own = self.dir / "own" / "models"
        (self.own / "checkpoints").mkdir(parents=True)
        self.base = self.dir / "profile"
        self.base.mkdir()
        self.shared = sm.roots_for(str(self.server), self.own)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self):
        merged, found, skipped = lm.build(ENTRIES, self.own, self.base, self.shared)
        return {e.id: e for e in merged}, found, skipped

    def status(self, entry, entries):
        return cat.install_status(entry, self.own, None, list(entries.values()))

    def test_krita_models_count_as_installed(self):
        entries, _found, _skipped = self.build()
        for model_id in ("realvisxl-v5", "fooocus-inpaint-patch", "mat-places512"):
            with self.subTest(model_id):
                entry = entries[model_id]
                status = self.status(entry, entries)
                self.assertTrue(status.installed, status.missing)
                self.assertEqual(status.missing_models, [])
                main = [f for f in entry.files if f.role != cat.PREVIEW_ROLE]
                for f in main:
                    self.assertEqual(Path(f.root), self.server / "models")
                    self.assertEqual((f.url, f.optional), ("", True))
                    self.assertTrue(cat.spec_path(self.own, f).is_file())
        xl = entries["realvisxl-v5"]
        self.assertEqual(cat.model_spec_for(xl, list(entries.values())).checkpoint, "RealVisXL_V5.0_fp16.safetensors")
        self.assertTrue(xl.checksum_known)  # nothing to download, nothing to verify
        row = cat.model_row(xl, self.status(xl, entries))
        self.assertEqual(row["shared_from"], [str(self.server / "models")])
        self.assertIn("another ComfyUI (read only)", cat.row_details(row))
        self.assertNotIn("realvisxl-v5", [e.id for e in ENTRIES if e.files[0].root])  # the shipped list is untouched

    def test_flux_only_downloads_the_unet(self):
        entries, _found, _skipped = self.build()
        flux = entries["flux1-fill-dev-gguf-q4"]
        status = self.status(flux, entries)
        self.assertEqual(status.missing, ["unet/flux1-fill-dev-Q4_0.gguf"])
        d = {f.role: (f.root, f.dest) for f in flux.files}
        krita = str(self.server / "models")
        self.assertEqual(d["clip1"], (krita, "text_encoders/t5-v1_1-xxl-encoder-Q5_K_M.gguf"))
        self.assertEqual(d["clip2"], (krita, "text_encoders/clip_l.safetensors"))
        self.assertEqual(d["vae"], (krita, "vae/flux_vae.safetensors"))  # not FLUX.2's
        spec = cat.model_spec_for(flux, list(entries.values()))
        self.assertEqual(
            (spec.clip1, spec.clip2, spec.vae),
            ("t5-v1_1-xxl-encoder-Q5_K_M.gguf", "clip_l.safetensors", "flux_vae.safetensors"),
        )
        # what is still missing keeps its download address
        self.assertEqual(flux.file_by_role("unet"), catalog_file("flux1-fill-dev-gguf-q4", "unet"))

    def test_own_folder_comes_first(self):
        own_xl = sized(self.own / "checkpoints" / "RealVisXL_V5.0_fp16.safetensors", 1)  # another file, same name
        entries, _found, _skipped = self.build()
        xl = entries["realvisxl-v5"]
        self.assertFalse(self.status(xl, entries).installed)  # ComfyUI would load the own file: not counted
        self.assertEqual(xl.files[0], catalog_file("realvisxl-v5", "checkpoint"))
        own_xl.unlink()
        sized(
            self.own / "checkpoints" / "RealVisXL_V5.0_fp16.safetensors",
            catalog_file("realvisxl-v5", "checkpoint").size,
        )
        entries, _found, _skipped = self.build()
        self.assertEqual(entries["realvisxl-v5"].files[0], catalog_file("realvisxl-v5", "checkpoint"))  # own, as before

    def test_own_models_in_a_shared_folder(self):
        _entries, found, skipped = self.build()
        mine = [e for e in found if e.files[0].dest == "checkpoints/myXL.safetensors"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(Path(mine[0].files[0].root), self.server / "models")
        self.assertIn(str(self.server / "models" / "checkpoints"), mine[0].uploader)
        # the same name in OpenFill's folder is another model with another id
        write_safetensors(self.own / "checkpoints" / "myXL.safetensors", SDXL)
        _entries, found2, _skipped = self.build()
        own = [e for e in found2 if e.files[0].dest == "checkpoints/myXL.safetensors"]
        self.assertEqual([e.files[0].root for e in own], [""])  # the shared one is shadowed now
        self.assertNotEqual(own[0].id, mine[0].id)
        reasons = {s["file"]: s["reason"] for s in skipped}
        dream = str(self.server / "models" / "checkpoints" / "dreamshaper_8.safetensors")
        self.assertIn("inpainting version", reasons[dream])  # Krita's plain SD 1.5: reported, not used

    def test_local_flux_file_uses_krita_encoders(self):
        write_gguf(self.own / "unet" / "my-fill.gguf", FLUX_FILL)
        _entries, found, _skipped = self.build()
        (flux,) = [e for e in found if e.files[0].dest == "unet/my-fill.gguf"]
        self.assertEqual(
            {f.role: bool(f.root) for f in flux.files if f.role != cat.PREVIEW_ROLE},
            {"unet": False, "clip1": True, "clip2": True, "vae": True},
        )

    def test_no_shared_folders_no_change_for_complete_entries(self):
        merged, _found, _skipped = lm.build(ENTRIES, self.own, self.base, [])
        self.assertEqual([e for e in merged if e.source != lm.SOURCE], ENTRIES)


class WorkerFixture(unittest.TestCase):
    """A Krita server, OpenFill's own ComfyUI naming it in config.json, and a task worker."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.server = make_krita(base / "krita" / "server")
        self.comfy = base / "ComfyUI_windows_portable"
        (self.comfy / "python_embeded").mkdir(parents=True)
        (self.comfy / "python_embeded" / "python.exe").write_bytes(b"")
        (self.comfy / "ComfyUI" / "models").mkdir(parents=True)
        (self.comfy / "ComfyUI" / "custom_nodes").mkdir(parents=True)
        (self.comfy / "ComfyUI" / "main.py").write_text("")
        self.models_root = self.comfy / "ComfyUI" / "models"
        self.profile = base / "openfill"
        self.profile.mkdir()
        Settings(comfy_dir=str(self.comfy), shared_models=str(self.server)).save(self.profile / "config.json")
        self.worker = supervisor.TaskWorker(
            self.profile, self.profile / "config.json", supervisor.Logger(self.profile / "test.log", "test")
        )

    def tearDown(self):
        for dirpath, dirnames, _files in os.walk(self.tmp.name):
            for name in dirnames:
                os.chmod(Path(dirpath) / name, 0o755)
        self.tmp.cleanup()

    def entry(self, model_id):
        return cat.by_id(self.worker._catalog(), model_id)


class WorkerTests(WorkerFixture):
    """The supervisor (the trust point) never downloads into or deletes from a shared folder."""

    def test_shared_files_are_never_downloaded(self):
        xl = self.entry("realvisxl-v5")
        f = xl.files[0]
        self.assertTrue(f.root)
        task = {
            "type": "download",
            "model_id": xl.id,
            "models_root": str(self.models_root),
            "files": [{"url": f.url, "dest": f.dest, "size": f.size}],
        }
        with self.assertRaises(ValueError):
            self.worker.checked_download_files(task)
        # the catalog's download address with the shared file's place is not a catalog download either
        task["files"][0]["url"] = catalog_file("realvisxl-v5", "checkpoint").url
        with self.assertRaises(ValueError):
            self.worker.checked_download_files(task)

    def test_a_download_into_a_shared_folder_is_refused(self):
        # a shared folder inside OpenFill's own models folder (odd, but possible): no writes there
        Settings(comfy_dir=str(self.comfy), shared_models=str(self.models_root / "inpaint")).save(
            self.profile / "config.json"
        )
        (self.models_root / "inpaint").mkdir()
        lama = self.entry("lama-big")
        f = lama.files[0]
        task = {
            "type": "download",
            "model_id": lama.id,
            "models_root": str(self.models_root),
            "files": [{"url": f.url, "dest": f.dest, "size": f.size}],
        }
        with self.assertRaises(ValueError):
            self.worker.checked_download_files(task)

    def test_remove_skips_a_shared_folder_inside_openfills_own(self):
        """Delete joins every path under OpenFill's own folder, so a file of another install is
        out of its reach anyway; a folder named shared inside OpenFill's own is guarded too."""
        lama = catalog_file("lama-big", "inpaint_model")
        target = sized(self.models_root / lama.dest, lama.size)
        Settings(comfy_dir=str(self.comfy), shared_models=str(self.models_root / "inpaint")).save(
            self.profile / "config.json"
        )
        task = {
            "id": "t",
            "model_id": "lama-big",
            "type": "remove",
            "models_root": str(self.models_root),
            "files": [lama.dest],
        }
        self.worker._remove(task)
        self.assertTrue(target.exists())
        Settings(comfy_dir=str(self.comfy)).save(self.profile / "config.json")
        self.worker._remove(task)
        self.assertFalse(target.exists())  # without the setting it is OpenFill's download, deleted as before

    def test_remove_never_deletes_shared_files(self):
        before = snapshot(self.server)
        for model_id in ("realvisxl-v5", "mat-places512", "fooocus-inpaint-patch", "flux1-fill-dev-gguf-q4"):
            e = self.entry(model_id)
            task = {
                "id": "t",
                "model_id": e.id,
                "type": "remove",
                "models_root": str(self.models_root),
                "files": [f.dest for f in e.files],
            }
            self.worker._remove(task)
        mine = next(e for e in self.worker._catalog() if e.source == lm.SOURCE and e.files[0].root)
        self.worker._remove(
            {
                "id": "t",
                "model_id": mine.id,
                "type": "remove",
                "models_root": str(self.models_root),
                "files": [f.dest for f in mine.files],
            }
        )
        self.assertEqual(snapshot(self.server), before)

    def test_backend_command_line_gets_the_yaml_from_the_profile(self):
        logs = []
        path = supervisor.shared_model_paths(self.profile, self.profile / "config.json", lambda m, *_: logs.append(m))
        self.assertEqual(path, self.profile / sm.CONFIG_FILENAME)
        self.assertIn(str((self.server / "models" / "inpaint").resolve()), path.read_text())
        Settings(comfy_dir=str(self.comfy)).save(self.profile / "config.json")
        self.assertIsNone(supervisor.shared_model_paths(self.profile, self.profile / "config.json", logs.append))
        self.assertEqual(_yaml(path), {})  # kept for a ComfyUI started by hand, with no folders


class NeverWriteTests(WorkerFixture):
    """Everything OpenFill does with a shared folder leaves it exactly as it was: same paths,
    sizes, times, modes and contents. Then again with the folder read only, so a write that a
    snapshot could miss (one undone before the end) fails loudly instead."""

    def everything(self):
        """Detection, settings, catalog, status, downloads of what is missing, deletes, the yaml."""
        self.downloaded = []
        sm.detect_krita({}, Path(self.tmp.name) / "krita", "linux")
        sm.model_roots(self.server)
        entries = self.worker._catalog()
        for e in entries:
            cat.install_status(e, self.models_root, self.comfy / "ComfyUI" / "custom_nodes", entries)
            cat.missing_previews(e, self.models_root)
            if e.runnable:
                cat.model_spec_for(e, entries, self.models_root)
        supervisor.shared_model_paths(self.profile, self.profile / "config.json", lambda *_: None)

        def fake_download(url, dest, *args, **kwargs):
            dest = Path(dest)
            self.downloaded.append(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"downloaded")
            return SimpleNamespace(sha256="0" * 64, skipped=False, resumed_from=0, bytes_written=10)

        with mock.patch.object(downloads, "download_file", side_effect=fake_download):
            for model_id in ("flux1-fill-dev-gguf-q4", "realvisxl-v5", "lama-big"):
                e = cat.by_id(entries, model_id)
                files = [
                    {"url": f.url, "dest": f.dest, "size": f.size}
                    for f in e.files
                    if f.url and not cat.file_installed(self.models_root, f)
                ]
                self.worker._download(
                    {
                        "id": "d",
                        "model_id": e.id,
                        "type": "download",
                        "models_root": str(self.models_root),
                        "files": files,
                    }
                )
        for e in self.worker._catalog():
            self.worker._remove(
                {
                    "id": "r",
                    "model_id": e.id,
                    "type": "remove",
                    "models_root": str(self.models_root),
                    "files": [f.dest for f in e.files],
                }
            )

    def test_nothing_changes_in_the_other_install(self):
        before = snapshot(self.server.parent)
        self.everything()
        self.assertEqual(snapshot(self.server.parent), before)
        # what was missing was downloaded, into OpenFill's own folder only; what Krita has was not
        got = sorted(p.relative_to(self.models_root).as_posix() for p in self.downloaded)
        self.assertIn("unet/flux1-fill-dev-Q4_0.gguf", got)
        self.assertIn("inpaint/big-lama.pt", got)
        for name in ("clip_l", "t5-v1_1", "ae.safetensors", "RealVisXL"):
            self.assertFalse([g for g in got if name in g], (name, got))
        self.assertFalse((self.models_root / "unet" / "flux1-fill-dev-Q4_0.gguf").exists())  # deleted again
        self.assertTrue((self.profile / sm.CONFIG_FILENAME).is_file())

    @unittest.skipIf(os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs POSIX, not root")
    def test_nothing_changes_with_the_other_install_read_only(self):
        for dirpath, _dirnames, filenames in os.walk(self.server.parent):
            for name in filenames:
                os.chmod(Path(dirpath) / name, 0o444)
        for dirpath, dirnames, _filenames in os.walk(self.server.parent, topdown=False):
            for name in dirnames:
                os.chmod(Path(dirpath) / name, 0o555)
        os.chmod(self.server.parent, 0o555)
        before = snapshot(self.server.parent)
        self.everything()  # a write there would raise PermissionError
        self.assertEqual(snapshot(self.server.parent), before)
        os.chmod(self.server.parent, 0o755)


class AppTests(unittest.TestCase):
    """The App saves the list, rebuilds its catalog and never deletes shared files."""

    def setUp(self):
        from openfill import app as app_mod

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.server = make_krita(root / "krita" / "server")
        comfy = root / "ComfyUI_windows_portable"
        (comfy / "python_embeded").mkdir(parents=True)
        (comfy / "python_embeded" / "python.exe").write_text("")
        (comfy / "ComfyUI" / "custom_nodes").mkdir(parents=True)
        (comfy / "ComfyUI" / "models").mkdir(parents=True)
        (comfy / "ComfyUI" / "main.py").write_text("")
        profile = root / "profile"
        (profile / "openfill").mkdir(parents=True)
        (profile / "openfill" / "config.json").write_text(
            json.dumps({"comfy_dir": str(comfy), "url": "http://127.0.0.1:9"}), encoding="utf-8"
        )
        io = SimpleNamespace(profile_dir=lambda: str(profile), message=lambda _t: None)
        self.app = app_mod.App(interactive=False, io=io, runner_cls=object)
        self.log_file = str(profile / "openfill" / "openfill.log")

    def tearDown(self):
        for handler in list(self.app.log.handlers):
            if getattr(handler, "baseFilename", None) == self.log_file:
                handler.close()
                self.app.log.removeHandler(handler)
        self.tmp.cleanup()

    def installed(self, model_id):
        e = cat.by_id(self.app.catalog, model_id)
        return cat.install_status(e, self.app.models_root, None, self.app.catalog).installed

    def test_set_shared_models_and_remove(self):
        self.assertFalse(self.installed("realvisxl-v5"))
        with self.assertRaises(BackendError):
            self.app.set_shared_models(str(Path(self.tmp.name) / "missing"))
        self.assertIn("read only", self.app.set_shared_models(str(self.server)))
        self.assertEqual(sm.parse(Settings.load(self.app.paths.config).shared_models), [str(self.server)])
        # the yaml is there at once, for a ComfyUI started by hand too (review B8)
        self.assertTrue((self.app.paths.base / sm.CONFIG_FILENAME).is_file())
        self.assertTrue(self.installed("realvisxl-v5"))
        queued = []
        with mock.patch.object(self.app.server, "queue_task", side_effect=lambda t: queued.append(t) or "id"):
            self.app.remove_model("realvisxl-v5")
        self.assertEqual(queued[0]["files"], [])  # nothing of the other install is ever named for deletion
        self.assertEqual(self.app.set_shared_models(""), "No other ComfyUI folders are used.")
        self.assertFalse(self.installed("realvisxl-v5"))
        self.assertEqual(_yaml(self.app.paths.base / sm.CONFIG_FILENAME), {})  # kept, empty (review 2, N3)

    def test_changing_the_list_does_not_kill_a_running_job(self):
        """Review B2: saving the list never stops the engine; the next job restarts it only
        when nothing runs (no task, nothing queued in ComfyUI)."""
        stale = {"supervisor_pid": os.getpid(), "server_pid": 1, "url": "http://127.0.0.1:9", "shared_models": ""}
        busy = {"current": {"type": "download", "model_id": "flux1-fill-dev-gguf-q4"}}
        with (
            mock.patch.object(self.app.server, "read_state", return_value=stale),
            mock.patch.object(self.app.server, "task_status", return_value=busy),
            mock.patch.object(self.app.server, "pending_tasks", return_value=[]),
            mock.patch.object(self.app.server, "request_stop", return_value=True) as stop,
        ):
            self.app.set_shared_models(str(self.server))
            stop.assert_not_called()
            self.app._restart_for_shared_models()  # the next job, while the download runs
            stop.assert_not_called()
        idle_queue = {"queue_running": [], "queue_pending": []}
        busy_queue = {"queue_running": [["x"]], "queue_pending": []}
        for queue, expect_stop in ((busy_queue, False), (idle_queue, True)):
            with (
                mock.patch.object(self.app.server, "read_state", side_effect=[stale, {}]),
                mock.patch.object(self.app.server, "task_status", return_value={}),
                mock.patch.object(self.app.server, "pending_tasks", return_value=[]),
                mock.patch.object(self.app.server, "is_healthy", return_value=False),
                mock.patch("openfill.app.ComfyClient") as client,
                mock.patch.object(self.app.server, "request_stop", return_value=True) as stop,
            ):
                client.return_value.queue.return_value = queue
                self.app._restart_for_shared_models()
                self.assertEqual(stop.called, expect_stop, queue)
        # started with the current list: nothing to do
        current = {**stale, "shared_models": self.app.shared_config_text()}
        with (
            mock.patch.object(self.app.server, "read_state", return_value=current),
            mock.patch.object(self.app.server, "request_stop", return_value=True) as stop,
        ):
            self.app._restart_for_shared_models()
            stop.assert_not_called()

    def test_suggestions_skip_folders_already_used(self):
        cand = sm.Candidate(self.server, sm.KRITA)
        with mock.patch.object(sm, "detect", return_value=[cand]):
            self.assertEqual(self.app.shared_suggestions(), [cand])
            self.app.settings.shared_models = str(self.server)
            self.assertEqual(self.app.shared_suggestions(), [])


class ReviewFixesTests(WorkerFixture):
    """The adversarial review's cases, kept (review-models.md, 2026-11-06)."""

    @unittest.skipIf(os.name != "posix", "needs symlinks")
    def test_own_models_folder_linked_to_the_shared_one_stays_read_only(self):
        """B1: OpenFill's ComfyUI/models is a link to Krita's models (the old way to share);
        with Krita named in the setting, neither Delete nor a download touches Krita's files."""
        import shutil

        shutil.rmtree(self.models_root)
        self.models_root.symlink_to(self.server / "models", target_is_directory=True)
        xl = catalog_file("realvisxl-v5", "checkpoint")
        target = self.server / "models" / xl.dest
        before = snapshot(self.server)
        self.worker._remove(
            {
                "id": "t",
                "model_id": "realvisxl-v5",
                "type": "remove",
                "models_root": str(self.models_root),
                "files": [xl.dest],
            }
        )
        self.assertTrue(target.is_file(), "Krita's RealVisXL was deleted")
        lama = catalog_file("lama-big", "inpaint_model")
        task = {
            "type": "download",
            "model_id": "lama-big",
            "models_root": str(self.models_root),
            "files": [{"url": lama.url, "dest": lama.dest, "size": lama.size}],
        }
        with self.assertRaises(ValueError) as cm:
            self.worker.checked_download_files(task)
        self.assertIn("only reads", str(cm.exception))
        self.assertEqual(snapshot(self.server), before)

    def test_a_shared_parent_folder_does_not_block_openfills_own_downloads(self):
        """B3: a shared folder that contains OpenFill's own models folder (C:\\AI holding both
        installs) guards only its own part: OpenFill still downloads and deletes its files."""
        parent = self.comfy.parent  # holds krita/ and ComfyUI_windows_portable/
        Settings(comfy_dir=str(self.comfy), shared_models=sm.join([str(parent)])).save(self.profile / "config.json")
        lama = catalog_file("lama-big", "inpaint_model")
        task = {
            "type": "download",
            "model_id": "lama-big",
            "models_root": str(self.models_root),
            "files": [{"url": lama.url, "dest": lama.dest, "size": lama.size}],
        }
        ((_spec, dest),) = self.worker.checked_download_files(task)
        self.assertEqual(dest, self.models_root / lama.dest)
        sized(dest, lama.size)
        self.worker._remove(
            {
                "id": "t",
                "model_id": "lama-big",
                "type": "remove",
                "models_root": str(self.models_root),
                "files": [lama.dest],
            }
        )
        self.assertFalse(dest.exists())
        # what is outside OpenFill's folder in that parent stays guarded
        self.assertTrue(self.worker._is_shared(parent / "krita" / "server" / "models" / "x"))
        self.assertFalse(self.worker._is_shared(self.models_root / "inpaint" / "x"))


class ReviewYamlAndScanTests(unittest.TestCase):
    def test_no_surrogate_escapes_in_the_yaml(self):
        """B4: a folder name outside the BMP (an emoji) is written as is, not as a UTF-16 pair
        PyYAML would split into two lone surrogates."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Krita \U0001f3a8 é" / "models"
            (root / "checkpoints").mkdir(parents=True)
            text = sm.yaml_text([root])
            self.assertNotIn("\\ud", text.lower())
            self.assertIn("Krita \U0001f3a8 é", text)
            try:
                import yaml  # noqa: PLC0415 - only to check the text
            except ImportError:
                return
            data = yaml.safe_load(text)
            self.assertTrue(Path(data["openfill_shared_1"]["checkpoints"]).is_dir())

    def test_first_shared_root_is_not_shadowed_by_itself(self):
        """B6: with OpenFill's own models folder missing, the first shared folder's models are
        listed (and a later folder is only checked against the ones before it)."""
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a", Path(tmp) / "b"
            write_safetensors(a / "checkpoints" / "myXL.safetensors", SDXL)
            write_safetensors(b / "checkpoints" / "myXL.safetensors", SDXL)  # same name: a wins
            write_safetensors(b / "checkpoints" / "otherXL.safetensors", SDXL)
            base = Path(tmp) / "profile"
            base.mkdir()
            found, _ = lm.scan(Path(tmp) / "own-missing", ENTRIES, base, [a, b])
            got = sorted((Path(e.files[0].root).name, e.files[0].dest) for e in found)
            self.assertEqual(got, [("a", "checkpoints/myXL.safetensors"), ("b", "checkpoints/otherXL.safetensors")])

    def test_one_file_one_row(self):
        """B7: a catalog file in the alias folder (diffusion_models/ for unet/) is one model."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            base = Path(tmp) / "p"
            base.mkdir()
            u = catalog_file("flux1-fill-dev-gguf-q4", "unet")
            sized(write_gguf(root / "diffusion_models" / u.filename, FLUX_FILL), u.size)
            merged, _found, _skipped = lm.build(ENTRIES, root, base, [])
            users = [e.id for e in merged if any(f.dest == f"diffusion_models/{u.filename}" for f in e.files)]
            self.assertEqual(users, ["flux1-fill-dev-gguf-q4"])


class BundledSizeLimitTests(unittest.TestCase):
    """A bundled FLUX entry takes a found T5 only up to 1.2 times its catalog file (it was sized
    for it); the user's own FLUX file takes any compatible one, the closest in size first."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "models"
        self.base = Path(self.tmp.name) / "profile"
        self.base.mkdir()
        write_safetensors(self.root / "text_encoders" / "clip_l.safetensors", CLIP_L)

    def tearDown(self):
        self.tmp.cleanup()

    def t5(self, name, size):
        writer = write_gguf if name.endswith(".gguf") else write_safetensors
        return sized(writer(self.root / "text_encoders" / name, T5_GGUF if name.endswith(".gguf") else T5_ST), size)

    def build(self):
        merged, found, _ = lm.build(ENTRIES, self.root, self.base, [])
        return {e.id: e for e in merged}, found

    def test_bundled_q4_keeps_its_gguf_t5_over_a_much_bigger_one(self):
        q5 = catalog_file("flux1-fill-dev-gguf-q4", "clip1")
        self.t5("t5xxl_fp16.safetensors", 9_787_841_024)
        fp8 = self.t5("t5xxl_fp8_e4m3fn.safetensors", 4_893_934_904)  # 1.44x the Q5_K_M
        entries, _found = self.build()
        self.assertEqual(entries["flux1-fill-dev-gguf-q4"].file_by_role("clip1"), q5)  # downloaded as sized
        # the safetensors FLUX entry was made for the fp8: it takes it
        self.assertEqual(entries["flux1-fill-dev"].file_by_role("clip1").dest, "text_encoders/" + fp8.name)
        # a variant within 1.2x is taken by the Q4 entry too
        q6 = self.t5("t5-v1_1-xxl-encoder-Q6_K.gguf", int(q5.size * 1.15))
        entries, _found = self.build()
        self.assertEqual(entries["flux1-fill-dev-gguf-q4"].file_by_role("clip1").dest, "text_encoders/" + q6.name)

    def test_your_own_flux_file_takes_any_compatible_t5(self):
        self.t5("t5xxl_fp16.safetensors", 9_787_841_024)
        write_gguf(self.root / "unet" / "my-fill.gguf", FLUX_FILL)
        _entries, found = self.build()
        (mine,) = [e for e in found if e.files[0].dest == "unet/my-fill.gguf"]
        self.assertEqual(mine.file_by_role("clip1").dest, "text_encoders/t5xxl_fp16.safetensors")
        self.t5("t5xxl_fp8_e4m3fn.safetensors", 4_893_934_904)
        _entries, found = self.build()
        (mine,) = [e for e in found if e.files[0].dest == "unet/my-fill.gguf"]
        self.assertEqual(mine.file_by_role("clip1").dest, "text_encoders/t5xxl_fp8_e4m3fn.safetensors")  # closest


class Review2AppTests(AppTests):
    """The second review's cases (review-models2.md, 2026-11-06), kept."""

    test_set_shared_models_and_remove = None
    test_changing_the_list_does_not_kill_a_running_job = None
    test_suggestions_skip_folders_already_used = None

    def api(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "photocraft"))
        from openfill_photocraft import web

        api = web.Api.__new__(web.Api)
        api.service = mock.Mock(app=self.app)
        return api

    def test_clearing_the_list_keeps_a_yaml_comfyui_can_read(self):
        """N3: a ComfyUI started by hand names the file on its command line; it must stay readable."""
        self.app.set_shared_models(str(self.server))
        path = self.app.paths.base / sm.CONFIG_FILENAME
        self.app.set_shared_models("")
        self.assertEqual(_yaml(path), {})

    def test_an_unplugged_shared_folder_does_not_block_other_settings(self):
        """N2: the page sends the unchanged list on every save; it is not checked again."""
        self.app.set_shared_models(str(self.server))
        stored = self.app.settings.shared_models
        moved = self.server.with_name("server-unplugged")
        self.server.rename(moved)
        try:
            self.api().post("/api/settings", {"steps": 31, "shared_models": sm.display(stored)})
            self.assertEqual(Settings.load(self.app.paths.config).steps, 31)
            self.assertEqual(Settings.load(self.app.paths.config).shared_models, stored)
        finally:
            moved.rename(self.server)

    def test_web_save_with_a_new_comfyui_folder_is_all_or_nothing(self):
        """N8: the list is checked against the ComfyUI folder saved with it."""
        other = Path(self.tmp.name) / "OtherComfy"
        (other / "python_embeded").mkdir(parents=True)
        (other / "python_embeded" / "python.exe").write_text("")
        (other / "ComfyUI" / "custom_nodes").mkdir(parents=True)
        (other / "ComfyUI" / "models" / "checkpoints").mkdir(parents=True)
        (other / "ComfyUI" / "main.py").write_text("")
        before = Settings.load(self.app.paths.config).comfy_dir
        if os.name == "posix":  # the new ComfyUI folder's models are a link to the folder in the list
            krita_models = self.server / "models"
            shutil.rmtree(other / "ComfyUI" / "models")
            (other / "ComfyUI" / "models").symlink_to(krita_models, target_is_directory=True)
            body = {"comfy_dir": str(other), "shared_models": str(self.server)}
            with self.assertRaises(BackendError):
                self.api().post("/api/settings", body)
            self.assertEqual(Settings.load(self.app.paths.config).comfy_dir, before, "half the save was applied")
        # a good pair is saved together, and the yaml follows the new ComfyUI folder (N11)
        (other / "ComfyUI" / "models").unlink() if os.name == "posix" else None
        (other / "ComfyUI" / "models" / "checkpoints").mkdir(parents=True, exist_ok=True)
        self.api().post("/api/settings", {"comfy_dir": str(other), "shared_models": str(self.server)})
        saved = Settings.load(self.app.paths.config)
        self.assertEqual((saved.comfy_dir, sm.parse(saved.shared_models)), (str(other), [str(self.server)]))
        text = (self.app.paths.base / sm.CONFIG_FILENAME).read_text(encoding="utf-8")
        self.assertIn(json.dumps(str((other / "ComfyUI" / "models" / "inpaint").resolve())), text)

    def test_a_deferred_restart_does_not_promise_models_the_engine_cannot_load(self):
        """N7: while the running engine has not read the new list, the catalog keeps the list
        it runs with, and a job for a model only the new list has says why it waits."""
        running = {
            "supervisor_pid": os.getpid(),
            "server_pid": 1,
            "url": "http://127.0.0.1:9",
            "shared_models": "",
            "shared_roots": [],
        }
        busy = {"current": {"type": "download", "model_id": "flux1-fill-dev-gguf-q4"}}
        with (
            mock.patch.object(self.app.server, "read_state", return_value=running),
            mock.patch.object(self.app.server, "task_status", return_value=busy),
            mock.patch.object(self.app.server, "pending_tasks", return_value=[]),
            mock.patch.object(self.app.server, "request_stop", return_value=True) as stop,
        ):
            self.app.set_shared_models(str(self.server))
            stop.assert_not_called()
            self.assertTrue(self.app.shared_pending())
            self.assertFalse(self.installed("realvisxl-v5"))  # the running ComfyUI cannot load it
            with self.assertRaises(BackendError) as cm:
                self.app.resolve_model("realvisxl-v5")
            self.assertIn("reads when it starts again", str(cm.exception))
        # once the engine runs with the new list (or is not running), Krita's model counts
        self.app.rescan()
        self.assertTrue(self.installed("realvisxl-v5"))

    def test_a_stale_state_file_does_not_stall_every_job(self):
        """N6: a state file whose supervisor is gone is not waited on."""
        self.app.set_shared_models(str(self.server))
        stale = {"supervisor_pid": 999999, "server_pid": 999998, "url": "http://127.0.0.1:9", "shared_models": ""}
        clock = [0.0]
        with (
            mock.patch.object(self.app.server, "read_state", return_value=stale),
            mock.patch.object(self.app.server, "task_status", return_value={}),
            mock.patch.object(self.app.server, "pending_tasks", return_value=[]),
            mock.patch.object(self.app.server, "is_healthy", return_value=True),
            mock.patch.object(self.app.server, "request_stop", return_value=True) as stop,
            mock.patch("openfill.app.time.monotonic", side_effect=lambda: clock[0]),
            mock.patch("openfill.app.time.sleep", side_effect=lambda s: clock.__setitem__(0, clock[0] + s)),
        ):
            self.app._restart_for_shared_models()
        self.assertLess(clock[0], 5)
        stop.assert_not_called()

    def test_the_restart_waits_for_the_supervisor_to_be_gone(self):
        """N10: not only for ComfyUI to stop answering: for the state file to go."""
        self.app.set_shared_models(str(self.server))
        state = {"supervisor_pid": os.getpid(), "server_pid": 1, "url": "http://127.0.0.1:9", "shared_models": ""}
        reads = iter([state, state, state, state, {}])
        with (
            mock.patch.object(self.app.server, "read_state", side_effect=lambda: next(reads, {})),
            mock.patch.object(self.app.server, "task_status", return_value={}),
            mock.patch.object(self.app.server, "pending_tasks", return_value=[]),
            mock.patch.object(self.app.server, "is_healthy", return_value=False),  # ComfyUI already silent
            mock.patch.object(self.app.server, "request_stop", return_value=True),
            mock.patch("openfill.app.ComfyClient") as client,
            mock.patch("openfill.app.time.sleep") as sleep,
        ):
            client.return_value.queue.return_value = {"queue_running": [], "queue_pending": []}
            self.app._restart_for_shared_models()
        self.assertGreaterEqual(sleep.call_count, 2)  # kept waiting while the state file was there


class Review2NamesTests(unittest.TestCase):
    @unittest.skipIf(sys.platform != "linux", "Linux file names are bytes")
    def test_a_non_utf8_folder_name_does_not_stop_the_engine_from_starting(self):
        """N4: refused with a plain message; a hand-edited setting never kills the supervisor."""
        with tempfile.TemporaryDirectory() as tmp:
            raw = os.path.join(os.fsencode(tmp), b"caf\xe9")
            os.mkdir(raw)
            server = make_krita(Path(os.fsdecode(raw)) / "server")
            folders, problem = sm.check(str(server))
            self.assertEqual(folders, [])
            self.assertIn("not UTF-8", problem)
            profile = Path(tmp) / "profile"
            profile.mkdir()
            Settings(shared_models=sm.join([str(server)])).save(profile / "config.json")
            # no exception, and no undecodable name in the file ComfyUI reads
            path = supervisor.shared_model_paths(profile, profile / "config.json", lambda *a: None)
            if path is not None:
                path.read_text(encoding="utf-8")

    def test_another_comfyui_that_reads_openfills_models_can_still_be_shared(self):
        """N5: its own extra_model_paths.yaml points at OpenFill's models: accepted, that root
        left out (those files are OpenFill's) and never guarded against OpenFill."""
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "OpenFill" / "ComfyUI" / "models"
            (own / "checkpoints").mkdir(parents=True)
            other = Path(tmp) / "ComfyUI"
            (other / "models" / "checkpoints").mkdir(parents=True)
            (other / "main.py").write_text("")
            (other / "extra_model_paths.yaml").write_text(
                f"openfill:\n    base_path: {own}\n    checkpoints: checkpoints\n", encoding="utf-8"
            )
            folders, problem = sm.check(str(other), own)
            self.assertEqual(problem, "")
            self.assertEqual(sm.roots_for(sm.join(folders), own), [other / "models"])
            guard = sm.guard_roots(sm.join(folders), own)
            self.assertEqual(guard, [other / "models"])
            self.assertFalse(sm.blocked(own / "checkpoints" / "x.safetensors", guard, own))
            self.assertTrue(sm.blocked(other / "models" / "checkpoints" / "x.safetensors", guard, own))

    def test_same_folder_by_identity(self):
        """N1: folders are compared by identity, not by how their path is written."""
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a"
            (a / "sub").mkdir(parents=True)
            self.assertTrue(sm._same(a, Path(tmp) / "x" / ".." / "a"))
            self.assertTrue(sm.inside(a / "sub" / "not-there-yet.bin", [a]))
            self.assertFalse(sm.inside(Path(tmp) / "b" / "f", [a]))
            if os.name == "posix":
                link = Path(tmp) / "link"
                link.symlink_to(a, target_is_directory=True)
                self.assertTrue(sm._same(link, a))
                self.assertTrue(sm.inside(link / "sub" / "f", [a]))


class Review2DocsTests(unittest.TestCase):
    def test_docs_match_the_size_rule(self):
        """N9: the list's GGUF FLUX entries keep their T5 GGUF over a t5xxl_fp8; the docs say so."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            fp8 = catalog_file("flux1-fill-dev", "clip1")
            sized(write_safetensors(root / "text_encoders" / fp8.filename, T5_ST), fp8.size)
            (entry,) = [e for e in lm.share(ENTRIES, root) if e.id == "flux1-fill-dev-gguf-q4"]
            self.assertTrue(entry.file_by_role("clip1").url)  # downloaded, not taken
        doc = (Path(__file__).resolve().parents[1] / "docs" / "models.md").read_text(encoding="utf-8")
        self.assertNotIn("or one from the list", doc)
        self.assertIn("1.2 times", doc)


if __name__ == "__main__":
    unittest.main()
