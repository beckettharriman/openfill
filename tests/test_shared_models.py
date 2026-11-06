# SPDX-License-Identifier: Apache-2.0
"""Models of another ComfyUI install, used read only (issue #6, shared_models.py): detection,
model folders, the yaml for ComfyUI, the catalog, and the guarantee that nothing is written there."""

import hashlib
import json
import os
import stat
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


class SettingTests(unittest.TestCase):
    def test_parse_and_join(self):
        sep = os.pathsep
        text = f' /a/b {sep}{sep}"/c d"\n/a/b{sep}/e '
        self.assertEqual(sm.parse(text), ["/a/b", "/c d", "/e"])
        self.assertEqual(sm.parse(""), [])
        self.assertEqual(sm.parse(None), [])
        self.assertEqual(sm.join(["/a", "/a", " /b "]), f"/a{sep}/b")
        self.assertEqual(sm.parse(sm.join(["/x y", "/z"])), ["/x y", "/z"])

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
            self.assertFalse(path.exists())


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
        self.assertFalse(path.exists())


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
        self.assertEqual(Settings.load(self.app.paths.config).shared_models, str(self.server))
        self.assertTrue(self.installed("realvisxl-v5"))
        queued = []
        with mock.patch.object(self.app.server, "queue_task", side_effect=lambda t: queued.append(t) or "id"):
            self.app.remove_model("realvisxl-v5")
        self.assertEqual(queued[0]["files"], [])  # nothing of the other install is ever named for deletion
        self.assertEqual(self.app.set_shared_models(""), "No other ComfyUI folders are used.")
        self.assertFalse(self.installed("realvisxl-v5"))

    def test_suggestions_skip_folders_already_used(self):
        cand = sm.Candidate(self.server, sm.KRITA)
        with mock.patch.object(sm, "detect", return_value=[cand]):
            self.assertEqual(self.app.shared_suggestions(), [cand])
            self.app.settings.shared_models = str(self.server)
            self.assertEqual(self.app.shared_suggestions(), [])


if __name__ == "__main__":
    unittest.main()
