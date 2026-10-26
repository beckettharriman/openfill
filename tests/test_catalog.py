# SPDX-License-Identifier: Apache-2.0
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from openfill import catalog as cat
from openfill import downloads

REPO = Path(__file__).resolve().parent.parent
CATALOG = REPO / "models" / "catalog.json"


class ShippedCatalogTests(unittest.TestCase):
    def setUp(self):
        self.entries = cat.load_catalog(CATALOG)

    def test_loads_and_has_required_entries(self):
        ids = {e.id for e in self.entries}
        for required in (
            "sd15-inpainting",
            "sdxl-base",
            "fooocus-inpaint-patch",
            "juggernaut-xl-v9",
            "flux1-fill-dev-gguf-q4",
            "qwen-image-edit-2511-gguf-q4",
            "lama-big",
            "mat-places512",
        ):
            self.assertIn(required, ids)

    def test_every_entry_is_well_formed(self):
        for e in self.entries:
            self.assertTrue(e.license_name and e.license_url.startswith("https://"), e.id)
            self.assertTrue(e.page.startswith("https://"), e.id)
            self.assertIn(e.source_type, cat.SOURCE_TYPES)
            if e.source_type == "community-quantization":
                self.assertTrue(e.uploader, e.id)
            for f in e.files:
                self.assertTrue(
                    f.url.startswith("https://huggingface.co/") or f.url.startswith("https://github.com/"), f.url
                )
                self.assertTrue(f.size and f.size > 0, f.dest)
                self.assertIn("/", f.dest)
                self.assertEqual(f.filename, Path(f.filename).name, f.dest)
                self.assertTrue(f.sha256 and len(f.sha256) == 64, f"{e.id}: {f.dest} needs a sha256")
            for n in e.requires_nodes:
                if n.bundled:  # OpenFill's own pack: pinned to the version it ships
                    self.assertEqual(n.commit, downloads.bundled_node_version(), n.folder)
                    continue
                self.assertEqual(len(n.commit), 40, n.folder)

    def test_gated_flags(self):
        flux = cat.by_id(self.entries, "flux1-fill-dev-gguf-q4")
        self.assertTrue(flux.gated)
        self.assertTrue(flux.file_by_role("vae").gated)
        self.assertFalse(flux.file_by_role("unet").gated)
        self.assertFalse(cat.by_id(self.entries, "mat-places512").gated)

    def test_gguf_tiers_require_gguf_nodes(self):
        for mid in ("flux1-fill-dev-gguf-q4", "qwen-image-edit-2511-gguf-q4"):
            folders = [n.folder for n in cat.by_id(self.entries, mid).requires_nodes]
            self.assertIn("ComfyUI-GGUF", folders)

    def test_search_paths(self):
        paths = cat.catalog_search_paths(REPO / "gimp-openfill")
        self.assertEqual(paths[1], CATALOG)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(CATALOG.read_text(encoding="utf-8"))

    def _expect(self, mutate, fragment):
        data = copy.deepcopy(self.data)
        mutate(data)
        with self.assertRaises(cat.CatalogError) as ctx:
            cat.parse_catalog(data)
        self.assertIn(fragment, str(ctx.exception))

    def test_duplicate_id(self):
        self._expect(lambda d: d["models"].append(copy.deepcopy(d["models"][0])), "duplicate")

    def test_bad_tier_capability_template(self):
        self._expect(lambda d: d["models"][0].__setitem__("tier", "ultra"), "tier")
        self._expect(lambda d: d["models"][0]["capabilities"].append("fly"), "capability")
        self._expect(lambda d: d["models"][0].__setitem__("template", "nope"), "template")

    def test_unsafe_dest_and_http_url(self):
        self._expect(lambda d: d["models"][0]["files"][0].__setitem__("dest", "../x.ckpt"), "unsafe dest")
        self._expect(lambda d: d["models"][0]["files"][0].__setitem__("url", "http://x/y"), "https")

    def test_unsafe_dest_variants(self):
        for dest in (
            "..\\x.ckpt",
            "checkpoints/../../x.ckpt",
            "C:/x.ckpt",
            "\\\\server\\x",
            "/abs/x.ckpt",
            "checkpoints//x.ckpt",
        ):
            self._expect(lambda d, dest=dest: d["models"][0]["files"][0].__setitem__("dest", dest), "unsafe dest")

    def test_node_repo_and_folder_validated(self):
        self._expect(
            lambda d: d["models"][1]["requires_nodes"][0].__setitem__("repo", "https://example.com/x/y"), "github.com"
        )
        self._expect(lambda d: d["models"][1]["requires_nodes"][0].__setitem__("folder", "../evil"), "plain name")
        self._expect(lambda d: d["models"][1]["requires_nodes"][0].__setitem__("commit", "5b76817"), "pinned commit")

    def test_bad_sha_and_commit(self):
        self._expect(lambda d: d["models"][0]["files"][0].__setitem__("sha256", "abc"), "sha256")
        self._expect(lambda d: d["models"][1]["requires_nodes"][0].__setitem__("commit", "12"), "pinned commit")

    def test_unknown_dependency(self):
        self._expect(lambda d: d["models"][1].__setitem__("requires_models", ["ghost"]), "unknown model")

    def test_bundled_files_need_sha256_and_size(self):
        self._expect(lambda d: d["models"][0]["files"][0].pop("sha256"), "needs a sha256")
        self._expect(lambda d: d["models"][0]["files"][0].__setitem__("size", -1), "bad size")
        self._expect(lambda d: d["models"][0]["files"][0].__setitem__("size", "4 GB"), "bad size")


class StatusTests(unittest.TestCase):
    def test_install_status_and_label(self):
        entries = cat.load_catalog(CATALOG)
        mat = cat.by_id(entries, "mat-places512")
        sdxl = cat.by_id(entries, "sdxl-base")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            nodes = Path(tmp) / "custom_nodes"
            st = cat.install_status(mat, root, nodes)
            self.assertFalse(st.installed)
            self.assertEqual(st.label, "Available")
            (root / "inpaint").mkdir(parents=True)
            (root / "inpaint" / "MAT_Places512_G_fp16.safetensors.part").write_bytes(b"x")
            self.assertEqual(cat.install_status(mat, root, nodes).label, "Partial")
            f = mat.files[0]
            with open(root / f.dest, "wb") as fh:
                fh.truncate(f.size)
            st = cat.install_status(mat, root, nodes)
            self.assertTrue(st.installed)
            self.assertEqual(st.label, "Needs node")
            (nodes / "comfyui-inpaint-nodes").mkdir(parents=True)
            self.assertEqual(cat.install_status(mat, root, nodes).label, "Installed")
            # companion model requirement
            (root / "checkpoints").mkdir(exist_ok=True)
            with open(root / sdxl.files[0].dest, "wb") as fh:
                fh.truncate(sdxl.files[0].size)
            st = cat.install_status(sdxl, root, nodes, entries)
            self.assertEqual(st.label, "Needs model")
            self.assertEqual(st.missing_models, ["fooocus-inpaint-patch"])

    def test_wrong_size_counts_as_missing(self):
        entries = cat.load_catalog(CATALOG)
        mat = cat.by_id(entries, "mat-places512")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "inpaint").mkdir()
            (root / mat.files[0].dest).write_bytes(b"short")
            self.assertFalse(cat.install_status(mat, root, None).installed)

    def test_server_listing(self):
        entries = cat.load_catalog(CATALOG)
        sd15 = cat.by_id(entries, "sd15-inpainting")
        with tempfile.TemporaryDirectory() as tmp:
            st = cat.install_status(sd15, Path(tmp), None, server_files=["sd-v1-5-inpainting.ckpt"])
            self.assertTrue(st.seen_by_server)
            st = cat.install_status(sd15, Path(tmp), None, server_files=[])
            self.assertFalse(st.seen_by_server)


class ModelSpecTests(unittest.TestCase):
    def test_specs(self):
        entries = cat.load_catalog(CATALOG)
        spec = cat.model_spec_for(cat.by_id(entries, "juggernaut-xl-v9"), entries)
        self.assertEqual(spec.checkpoint, "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors")
        self.assertEqual(spec.fooocus_patch, "inpaint_v26.fooocus.patch")
        self.assertTrue(spec.uses_fooocus)
        flux = cat.model_spec_for(cat.by_id(entries, "flux1-fill-dev-gguf-q4"), entries)
        self.assertEqual(
            (flux.unet, flux.clip2, flux.vae), ("flux1-fill-dev-Q4_0.gguf", "clip_l.safetensors", "ae.safetensors")
        )
        self.assertEqual(flux.effective_template, "flux_fill")
        lama = cat.model_spec_for(cat.by_id(entries, "lama-big"), entries)
        self.assertTrue(lama.prompt_free)
        with self.assertRaises(cat.CatalogError):
            cat.model_spec_for(cat.by_id(entries, "fooocus-inpaint-patch"), entries)

    def test_human_size(self):
        self.assertEqual(cat.human_size(0), "?")
        self.assertEqual(cat.human_size(4265437280), "4.0 GB")
        self.assertEqual(cat.human_size(52602), "51 KB")


LIVE_ID = "hf-someone--FLUX.1-Fill-dev-GGUF--flux1-fill-dev-Q5_K_S"


def live_raw(**over):
    """A valid live entry: one Hugging Face GGUF plus the verified FLUX companions."""
    entries = cat.load_catalog(CATALOG)
    base = cat.by_id(entries, "flux1-fill-dev-gguf-q4")
    companions = [
        {
            "role": f.role,
            "url": f.url,
            "dest": f.dest,
            "size": f.size,
            "sha256": f.sha256,
            "gated": f.gated,
            "optional": True,
        }
        for f in base.files
        if f.role != "unet"
    ]
    raw = {
        "id": LIVE_ID,
        "name": "someone/FLUX.1-Fill-dev-GGUF (Q5_K_S)",
        "tier": "quality",
        "capabilities": ["fill", "expand"],
        "arch": "flux",
        "template": "flux_fill",
        "page": "https://huggingface.co/someone/FLUX.1-Fill-dev-GGUF",
        "license": {"name": "flux-1-dev-non-commercial-license", "url": "https://huggingface.co/someone/x"},
        "gated": False,
        "source_type": "community-quantization",
        "uploader": "someone",
        "min_vram_gb": 9.8,
        "requires_nodes": [{"repo": n.repo, "folder": n.folder, "commit": n.commit} for n in base.requires_nodes],
        "requires_models": [],
        "files": [
            {
                "role": "unet",
                "url": "https://huggingface.co/someone/FLUX.1-Fill-dev-GGUF/resolve/"
                + "a" * 40
                + "/flux1-fill-dev-Q5_K_S.gguf",
                "dest": f"unet/{cat.live_folder(LIVE_ID)}/flux1-fill-dev-Q5_K_S.gguf",
                "size": 8_000_000_000,
                "sha256": "c" * 64,
            },
            *companions,
        ],
        "source": "huggingface",
        "family": "flux-fill",
        "downloads": 1234,
        "updated": "2026-09-01T10:00:00.000Z",
        "repo": "someone/FLUX.1-Fill-dev-GGUF",
    }
    raw.update(over)
    return raw


class LiveEntryTests(unittest.TestCase):
    def setUp(self):
        self.bundled = cat.load_catalog(CATALOG)

    def refused(self, raw):
        problems = []
        self.assertEqual(cat.parse_live_entries([raw], self.bundled, problems), [], raw.get("id"))
        return problems

    def main_file(self, **over):
        raw = live_raw()
        raw["files"][0].update(over)
        return raw

    def test_valid_entry(self):
        (e,) = cat.parse_live_entries([live_raw()], self.bundled)
        self.assertEqual((e.source, e.family, e.downloads, e.updated), ("huggingface", "flux-fill", 1234, "2026-09-01"))
        self.assertFalse(e.verified)
        self.assertTrue(e.checksum_known)
        self.assertTrue(e.gated)  # the FLUX VAE companion is gated
        self.assertEqual(cat.live_main_file(e).dest, f"unet/{LIVE_ID}/flux1-fill-dev-Q5_K_S.gguf")
        spec = cat.model_spec_for(e, [*self.bundled, e])
        self.assertEqual((spec.unet, spec.clip2), (f"{LIVE_ID}/flux1-fill-dev-Q5_K_S.gguf", "clip_l.safetensors"))

    def test_main_file_rules(self):
        cases = {
            "foreign host": self.main_file(
                url="https://evil.example/someone/x/resolve/main/flux1-fill-dev-Q5_K_S.gguf"
            ),
            "http": self.main_file(url="http://huggingface.co/someone/x/resolve/main/flux1-fill-dev-Q5_K_S.gguf"),
            "name mismatch": self.main_file(url="https://huggingface.co/someone/x/resolve/main/other.gguf"),
            "pickle": self.main_file(
                url="https://huggingface.co/someone/x/resolve/main/flux1.ckpt", dest="unet/flux1.ckpt"
            ),
            "folder": self.main_file(dest="custom_nodes/flux1-fill-dev-Q5_K_S.gguf"),
            "nested dest": self.main_file(dest="unet/sub/flux1-fill-dev-Q5_K_S.gguf"),
            "bare dest": self.main_file(dest="unet/flux1-fill-dev-Q5_K_S.gguf"),
            "other model's folder": self.main_file(dest="unet/hf-other/flux1-fill-dev-Q5_K_S.gguf"),
            "no size": self.main_file(size=None),
            "role": self.main_file(role="vae"),
            "optional main": self.main_file(optional=True),
            "id": live_raw(id="someone-flux"),
            "source": live_raw(source="bundled"),
            "page": live_raw(page="https://evil.example/x"),
            "template": live_raw(template="inpaint_model", arch="inpaint_model"),
            "arch mismatch": live_raw(arch="sdxl"),
        }
        for why, raw in cases.items():
            with self.subTest(why):
                self.assertTrue(self.refused(raw))

    def test_companions_and_nodes_must_be_verbatim(self):
        raw = live_raw()
        raw["files"][1]["url"] = "https://huggingface.co/evil/x/resolve/main/t5.gguf"  # now a second main file
        self.refused(raw)
        raw = live_raw()
        raw["files"][1]["sha256"] = "d" * 64
        self.refused(raw)
        evil_node = {"repo": "https://github.com/evil/nodes", "folder": "nodes", "commit": "e" * 40}
        self.refused(live_raw(requires_nodes=[evil_node]))
        self.refused(live_raw(requires_models=["ghost"]))

    def test_civitai_url(self):
        raw = live_raw(id="civitai-1-2-3", source="civitai", page="https://civitai.com/models/1")
        raw["files"][0]["url"] = "https://civitai.com/api/download/models/2?type=Model&format=SafeTensor"
        raw["files"][0]["dest"] = f"unet/{cat.live_folder('civitai-1-2-3')}/flux1-fill-dev-Q5_K_S.gguf"
        self.assertEqual(len(cat.parse_live_entries([raw], self.bundled)), 1)
        raw["files"][0]["url"] = "https://civitai.com/api/download/models/2/../x"
        self.refused(raw)

    def test_duplicates_of_the_verified_list_and_each_other_are_dropped(self):
        same_sha = self.main_file(sha256=cat.by_id(self.bundled, "flux1-fill-dev-gguf-q4").files[0].sha256)
        self.assertEqual(cat.parse_live_entries([same_sha], self.bundled), [])
        self.assertEqual(len(cat.parse_live_entries([live_raw(), live_raw()], self.bundled)), 1)

    def test_same_file_name_gets_its_own_folder(self):
        other_id = "hf-other--FLUX.1-Fill-dev-GGUF--flux1-fill-dev-Q5_K_S"
        other = live_raw(id=other_id)
        other["files"][0]["dest"] = f"unet/{cat.live_folder(other_id)}/flux1-fill-dev-Q5_K_S.gguf"
        other["files"][0]["sha256"] = "d" * 64
        entries = cat.parse_live_entries([live_raw(), other], self.bundled)
        self.assertEqual(len(entries), 2)
        dests = {cat.live_main_file(e).dest for e in entries}
        self.assertEqual(len(dests), 2)
        # a case-only difference of a taken destination is refused too
        clash = live_raw()
        clash["id"] = "hf-someone--clash"
        clash["files"][0]["dest"] = f"unet/{cat.live_folder(clash['id'])}/x.gguf"
        clash["files"][0]["url"] = clash["files"][0]["url"].replace("flux1-fill-dev-Q5_K_S.gguf", "x.gguf")
        self.assertEqual(len(cat.parse_live_entries([clash], self.bundled)), 1)

    def test_long_ids_get_a_short_unique_folder(self):
        a, b = "hf-" + "a" * 100, "hf-" + "a" * 99 + "b"
        self.assertLessEqual(len(cat.live_folder(a)), 60)
        self.assertNotEqual(cat.live_folder(a), cat.live_folder(b))

    def test_malformed_records_do_not_raise(self):
        for bad in (
            live_raw(license="MIT"),
            live_raw(files=["x"]),
            live_raw(requires_nodes=["x"]),
            live_raw(license=["x"]),
        ):
            self.refused(bad)
        with tempfile.TemporaryDirectory() as tmp:
            good = live_raw()
            cat._write_selected(cat.hub_selected_path(tmp), [live_raw(license="MIT"), good, "junk"])
            merged = cat.load_merged(path=CATALOG, base=tmp)
            self.assertEqual(merged[-1].id, good["id"])
            cat.hub_selected_path(tmp).write_text("[" * 100000, encoding="utf-8")
            self.assertEqual(len(cat.load_merged(path=CATALOG, base=tmp)), len(self.bundled))

    def test_remember_merge_and_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = self.main_file(sha256=None)
            entry = cat.remember_live_entry(tmp, raw, self.bundled)
            self.assertFalse(entry.checksum_known)
            merged = cat.load_merged(path=CATALOG, base=tmp)
            self.assertEqual(merged[-1].id, raw["id"])
            self.assertEqual(len(merged), len(self.bundled) + 1)
            cat.remember_live_entry(tmp, raw, self.bundled)  # replaces, does not duplicate
            self.assertEqual(len(cat.load_merged(path=CATALOG, base=tmp)), len(self.bundled) + 1)
            dest = raw["files"][0]["dest"]
            self.assertFalse(cat.record_live_checksum(tmp, raw["id"], dest, "not-a-sha"))
            self.assertTrue(cat.record_live_checksum(tmp, raw["id"], dest, "f" * 64))
            self.assertFalse(cat.record_live_checksum(tmp, raw["id"], dest, "e" * 64))  # never overwritten
            (again,) = cat.load_selected(tmp, self.bundled)
            self.assertEqual(cat.live_main_file(again).sha256, "f" * 64)
            # a tampered record is skipped, the rest still loads
            path = cat.hub_selected_path(tmp)
            data = json.loads(path.read_text(encoding="utf-8"))
            bad = copy.deepcopy(data["models"][0])
            bad["id"] = "hf-bad"
            bad["files"][0]["url"] = "https://evil.example/x.gguf"
            data["models"].append(bad)
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual([e.id for e in cat.load_selected(tmp, self.bundled)], [raw["id"]])
            with self.assertRaises(cat.CatalogError):
                cat.remember_live_entry(tmp, bad, self.bundled)
        self.assertEqual(cat.load_selected(None, self.bundled), [])


class FirstRunTests(unittest.TestCase):
    def test_runnable_installed_and_suggest_active(self):
        entries = cat.load_catalog(CATALOG)
        self.assertEqual(
            cat.NO_MODEL_MESSAGE, "No AI model is installed yet. Download the two starting models below to begin."
        )
        with tempfile.TemporaryDirectory() as tmp:
            root, nodes = Path(tmp) / "models", Path(tmp) / "custom_nodes"
            self.assertEqual(cat.runnable_installed(entries, root, nodes, "fill"), [])
            mat = cat.by_id(entries, "mat-places512")
            (root / "inpaint").mkdir(parents=True)
            with open(root / mat.files[0].dest, "wb") as fh:
                fh.truncate(mat.files[0].size)
            self.assertEqual(cat.runnable_installed(entries, root, nodes, "remove"), [])  # node missing
            (nodes / "comfyui-inpaint-nodes").mkdir(parents=True)
            self.assertEqual([e.id for e in cat.runnable_installed(entries, root, nodes, "remove")], ["mat-places512"])
            self.assertEqual(cat.runnable_installed(entries, root, nodes, "fill"), [])
            sd15 = cat.by_id(entries, "sd15-inpainting")
            (root / "checkpoints").mkdir()
            with open(root / sd15.files[0].dest, "wb") as fh:
                fh.truncate(sd15.files[0].size)
            fill = cat.runnable_installed(entries, root, nodes, "fill")
            self.assertEqual([e.id for e in fill], ["sd15-inpainting"])
            self.assertEqual(len(cat.runnable_installed(entries, root, nodes, "remove")), 2)
            self.assertIsNone(cat.suggest_active(fill, "sd15-inpainting"))
            self.assertEqual(cat.suggest_active(fill, "flux1-fill-dev"), "sd15-inpainting")
            self.assertIsNone(cat.suggest_active([], "flux1-fill-dev"))


def vm_row(mid, **over):
    row = {
        "id": mid,
        "name": mid,
        "installed": False,
        "partial": [],
        "active": False,
        "runnable": True,
        "origin": "bundled",
        "family": "sdxl",
        "fit": "comfortable",
        "downloads": 0,
        "updated": "",
        "checksum_known": True,
        "capabilities": ["fill"],
        "missing_models": [],
        "missing_nodes": [],
        "status": "Available",
    }
    row.update(over)
    return row


class ViewModelTests(unittest.TestCase):
    NOW = 1791590400.0  # 2026-10-09

    def test_model_row_and_labels(self):
        entries = cat.load_catalog(CATALOG)
        flux = cat.by_id(entries, "flux1-fill-dev-gguf-q4")
        st = cat.InstallStatus(False)
        verdict = SimpleNamespace(level="offload", label="Runs on this PC, slower", reason="r")
        row = cat.model_row(flux, st, "sd15-inpainting", False, verdict, True, ["https://huggingface.co/a/b"])
        self.assertEqual(row["status"], "Needs token")
        self.assertTrue(row["needs_login"])
        self.assertEqual(row["origin"], "bundled")
        self.assertEqual(row["fit"], "offload")
        self.assertEqual(cat.row_status(row), "Needs a free Hugging Face login")
        details = cat.row_details(row)
        self.assertIn("10.0 GB", details)
        self.assertIn("Non-Commercial", details)
        self.assertIn("Runs on this PC, slower", details)
        row = cat.model_row(flux, st, "", False, None, False)  # gated file already on disk: no login needed
        self.assertEqual(row["status"], "Available")
        self.assertEqual(cat.row_status(row), "")
        self.assertEqual(row["fit"], "unknown")

    def test_row_status_states(self):
        self.assertEqual(cat.row_status(vm_row("a", installed=True, active=True)), "Active")
        self.assertEqual(cat.row_status(vm_row("a", installed=True)), "Installed")
        self.assertEqual(cat.row_status(vm_row("a", installed=True, missing_models=["p"])), "Needs one more download")
        self.assertEqual(cat.row_status(vm_row("a", partial=["x"])), "Partly downloaded")
        self.assertEqual(cat.row_status(vm_row("a"), pending=["a"]), "Waiting")
        cur = {"model_id": "a", "type": "download", "done": 43, "total": 100}
        self.assertEqual(cat.row_status(vm_row("a"), cur), "Downloading 43%")

    def test_row_details_flags(self):
        text = cat.row_details(
            vm_row("a", size="2.0 GB", license="L", origin="civitai", checksum_known=False, capabilities=[])
        )
        self.assertTrue(text.startswith("2.0 GB"))
        self.assertIn("from Civitai", text)
        self.assertIn("unverified checksum", text)
        self.assertIn("helper file", cat.row_details(vm_row("p", runnable=False)))

    def test_job_hint_offers_every_job_a_model_can_run(self):
        row = vm_row("x", size="6.5 GB", capabilities=["fill", "expand", "txt2img"])
        self.assertEqual(cat.job_hint(row), "Fill, expand, create, remove")  # any fill model can remove
        self.assertTrue(cat.row_details(row).startswith("Fill, expand, create, remove  ·  6.5 GB"))
        self.assertEqual(cat.job_hint(vm_row("lama-big", capabilities=["remove"], template="inpaint_model")), "Remove")
        eraser = vm_row("rorem-sdxl", capabilities=["remove"], template="sdxl_eraser")
        self.assertEqual(cat.job_hint(eraser), "Remove (erase-trained)")
        self.assertEqual(cat.job_hint(vm_row("p", capabilities=["patch"], runnable=False)), "")

    def test_state_button(self):
        removal = vm_row("mat", installed=True, capabilities=["remove"])
        self.assertEqual(cat.state_button(removal), ("Ready", None, False))
        self.assertEqual(cat.state_button(vm_row("x", installed=True))[1], "activate")
        gated = vm_row("g", gated=True, needs_login=True)
        self.assertEqual(cat.state_button(gated, has_token=False)[1], "token")
        self.assertEqual(cat.state_button(vm_row("g", gated=True, needs_login=False), has_token=False)[1], "download")

    def test_recommended_one_per_family_fit_only(self):
        rows = [
            vm_row("sdxl-a", family="sdxl", downloads=100000, updated="2026-09-01", origin="huggingface"),
            vm_row("sdxl-b", family="sdxl", downloads=10),
            vm_row("sd15-a", family="sd15-inpaint"),
            vm_row("flux-big", family="flux-fill", fit="too_big", downloads=10**6),
            vm_row("qwen-a", family="qwen-edit", fit="offload"),
            vm_row("lama", family="removal"),
            vm_row("patch", family="", runnable=False),
            vm_row("done", family="sd15-inpaint", installed=True),
        ]
        rec = [r["id"] for r in cat.recommended(rows, 5, self.NOW)]
        self.assertEqual(rec[0], "sdxl-a")
        self.assertEqual(set(rec), {"sdxl-a", "sd15-a", "lama", "sdxl-b"})  # qwen is opt-in
        self.assertEqual(len(cat.recommended(rows, 3, self.NOW)), 3)

    def test_amd_notes_mark_gguf_and_keep_it_out_of_recommended(self):
        entries = cat.load_catalog(CATALOG)
        notes = {e.id: cat.amd_note(e) for e in entries}
        for mid in ("flux1-fill-dev-gguf-q4", "flux1-fill-dev-gguf-q6", "qwen-image-edit-2511-gguf-q4"):
            self.assertIn("GGUF", notes[mid], mid)
        for mid in (
            "lama-big",
            "mat-places512",
            "sd15-inpainting",
            "sdxl-base",
            "fooocus-inpaint-patch",
            "dreamshaper-8-inpainting",
            "realvisxl-v5",
            "flux1-fill-dev",
        ):
            self.assertEqual(notes[mid], "", mid)  # LaMa, MAT, SD 1.5, SDXL and the Fooocus patch run on ROCm
        flux = cat.by_id(entries, "flux1-fill-dev-gguf-q4")
        st = cat.InstallStatus(False)
        self.assertEqual(cat.model_row(flux, st)["amd_note"], "")  # not an AMD card
        row = cat.model_row(flux, st, amd=True)
        self.assertIn("GGUF", cat.row_details(row))
        live_gguf = vm_row("gguf-sdxl", family="sdxl", amd_note=notes["flux1-fill-dev-gguf-q4"])
        rec = [r["id"] for r in cat.recommended([live_gguf, vm_row("sdxl-a")], 5, self.NOW)]
        self.assertEqual(rec, ["sdxl-a"])
        rows = [{"id": "flux1-fill-dev-gguf-q4"}, {"id": "lama-big"}, {"id": "not-in-catalog"}]
        cat.add_amd_notes(rows, entries)
        self.assertEqual([bool(r["amd_note"]) for r in rows], [True, False, False])

    def test_rank_score(self):
        new_popular = vm_row("a", origin="huggingface", downloads=50000, updated="2026-09-01")
        old_quiet = vm_row("b", origin="huggingface", downloads=50, updated="2023-01-01")
        self.assertGreater(cat.rank_score(new_popular, self.NOW), cat.rank_score(old_quiet, self.NOW))
        live = vm_row("l", origin="civitai")
        self.assertGreater(cat.rank_score(vm_row("v"), self.NOW), cat.rank_score(live, self.NOW))
        self.assertLess(cat.rank_score(vm_row("t", fit="too_big"), self.NOW), 0)
        unverified = vm_row("u", origin="civitai", checksum_known=False)
        self.assertLess(cat.rank_score(unverified, self.NOW), cat.rank_score(live, self.NOW))

    def test_sections(self):
        rows = [
            vm_row("inst", installed=True),
            vm_row("act", installed=True, active=True),
            vm_row("part", partial=["x"], family="flux-fill"),
            vm_row("sd15", family="sd15-inpaint"),
            vm_row("v2", family="sd15-inpaint", downloads=1),
            vm_row("hf1", family="qwen-edit", origin="huggingface", fit="too_big"),
            vm_row("civ", family="sdxl", origin="civitai"),
            vm_row("patch", runnable=False, family=""),
        ]
        sections = cat.build_sections(rows, 5, self.NOW)
        self.assertEqual([k for k, _t, _r in sections], ["installed", "recommended", "verified", "more"])
        by = {k: [r["id"] for r in rs] for k, _t, rs in sections}
        self.assertEqual(by["installed"], ["act", "inst", "part"])
        self.assertNotIn("hf1", by["recommended"])
        self.assertIn("hf1", by["more"])
        self.assertEqual(by["verified"][-1], "patch")  # helper files last
        flat = [i for ids in by.values() for i in ids]
        self.assertEqual(sorted(flat), sorted(r["id"] for r in rows))  # every row exactly once
        self.assertEqual({k: t for k, t, _r in sections}["recommended"], "Recommended for this PC")
        self.assertEqual(cat.build_sections([], 5, self.NOW), [])

    def test_login_pages_and_steps(self):
        entries = cat.load_catalog(CATALOG)
        flux = cat.by_id(entries, "flux1-fill-dev-gguf-q4")
        self.assertEqual(cat.login_pages(flux), ["https://huggingface.co/black-forest-labs/FLUX.1-schnell"])
        first_party = cat.by_id(entries, "flux1-fill-dev")
        self.assertEqual(cat.login_pages(first_party), ["https://huggingface.co/black-forest-labs/FLUX.1-Fill-dev"])
        self.assertEqual(cat.login_pages(cat.by_id(entries, "sd15-inpainting")), [])
        steps = cat.gated_steps({"name": "M", "login_pages": cat.login_pages(flux)})
        self.assertEqual(len(steps), 4)
        self.assertEqual(steps[0][2], "https://huggingface.co/black-forest-labs/FLUX.1-schnell")
        self.assertIn("tokens/new", steps[2][2])
        for text, _button, _url in steps:
            for jargon in ("token", "API", "LFS", "gated", "repository"):
                self.assertNotIn(jargon, text)
        two = cat.gated_steps(
            {"name": "M", "login_pages": ["https://huggingface.co/a/b", "https://huggingface.co/c/d"]}
        )
        self.assertEqual([s[2] for s in two[1:3]], ["https://huggingface.co/a/b", "https://huggingface.co/c/d"])

    def test_apply_repo_stats(self):
        page = "https://huggingface.co/RunDiffusion/Juggernaut-XL-v9"
        rows = [vm_row("b", page=page), vm_row("l", origin="huggingface", page=page)]
        cat.apply_repo_stats(rows, {"RunDiffusion/Juggernaut-XL-v9": {"downloads": 284237, "updated": "2026-05-08"}})
        self.assertEqual((rows[0]["downloads"], rows[0]["updated"]), (284237, "2026-05-08"))
        self.assertEqual(rows[1]["downloads"], 0)  # live rows carry their own numbers


if __name__ == "__main__":
    unittest.main()


class PreviewFileTests(unittest.TestCase):
    def test_generative_models_bring_their_preview_decoder(self):
        entries = cat.load_catalog(CATALOG)
        want = {"sd15": "taesd_decoder.pth", "sdxl": "taesdxl_decoder.pth", "flux": "taef1_decoder.pth"}
        for e in entries:
            if e.arch in want and cat.live_main_file(e) is not None:
                previews = [f for f in e.files if f.role == cat.PREVIEW_ROLE]
                self.assertEqual([f.filename for f in previews], [want[e.arch]], e.id)
                self.assertTrue(previews[0].optional and previews[0].sha256, e.id)
                self.assertEqual(previews[0].dest, "vae_approx/" + want[e.arch])

    def test_a_missing_preview_does_not_uninstall_the_model(self):
        entry = cat.by_id(cat.load_catalog(CATALOG), "dreamshaper-8-inpainting")
        with tempfile.TemporaryDirectory() as tmp:
            main = cat.live_main_file(entry)
            path = Path(tmp) / main.dest
            path.parent.mkdir(parents=True)
            with open(path, "wb") as fh:
                fh.truncate(main.size)
            self.assertTrue(cat.install_status(entry, Path(tmp), None).installed)
            self.assertEqual([f.filename for f in cat.missing_previews(entry, Path(tmp))], ["taesd_decoder.pth"])
            (Path(tmp) / "vae_approx").mkdir()
            (Path(tmp) / "vae_approx" / "taesd_decoder.safetensors").write_bytes(b"x")  # what ComfyUI ships
            self.assertEqual(cat.missing_previews(entry, Path(tmp)), [])
