# SPDX-License-Identifier: Apache-2.0
"""Models already in ComfyUI's folders: header classification, scan, catalog entries (local_models)."""

import json
import struct
import tempfile
import unittest
from pathlib import Path

from openfill import catalog as cat
from openfill import local_models as lm

ENTRIES = cat.load_catalog()

SD15_INPAINT = {
    "model.diffusion_model.input_blocks.0.0.weight": [320, 9, 3, 3],
    "model.diffusion_model.input_blocks.1.1.transformer_blocks.0.attn2.to_k.weight": [320, 768],
}
SD15_PLAIN = {**SD15_INPAINT, "model.diffusion_model.input_blocks.0.0.weight": [320, 4, 3, 3]}
SD2 = {**SD15_INPAINT, "model.diffusion_model.input_blocks.1.1.transformer_blocks.0.attn2.to_k.weight": [320, 1024]}
SDXL = {
    "model.diffusion_model.input_blocks.0.0.weight": [320, 4, 3, 3],
    "model.diffusion_model.label_emb.0.0.weight": [1280, 2816],
}
SDXL_INPAINT = {**SDXL, "model.diffusion_model.input_blocks.0.0.weight": [320, 9, 3, 3]}
FLUX_FILL = {"img_in.weight": [3072, 384], "double_blocks.0.img_attn.qkv.weight": [9216, 3072]}
FLUX_DEV = {**FLUX_FILL, "img_in.weight": [3072, 64]}


def write_safetensors(path: Path, shapes: dict, metadata: dict | None = None) -> Path:
    header = {k: {"dtype": "F16", "shape": v, "data_offsets": [0, 0]} for k, v in shapes.items()}
    if metadata:
        header["__metadata__"] = metadata
    raw = json.dumps(header).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + b"\0" * 64)
    return path


def write_gguf(path: Path, shapes: dict) -> Path:
    """A minimal GGUF v3 header: one string and one array of metadata, then the tensor infos."""

    def s(text: str) -> bytes:
        b = text.encode()
        return struct.pack("<Q", len(b)) + b

    out = b"GGUF" + struct.pack("<IQQ", 3, len(shapes), 2)
    out += s("general.architecture") + struct.pack("<I", 8) + s("flux")
    out += s("general.tags") + struct.pack("<IIQ", 9, 4, 3) + struct.pack("<III", 1, 2, 3)
    for name, dims in shapes.items():
        out += s(name) + struct.pack("<I", len(dims)) + struct.pack(f"<{len(dims)}Q", *reversed(dims))
        out += struct.pack("<IQ", 2, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out + b"\0" * 32)
    return path


class ClassifyTests(unittest.TestCase):
    def test_families_and_refusals(self):
        self.assertEqual(lm.classify(SD15_INPAINT).family, "sd15-inpaint")
        self.assertEqual(lm.classify(SDXL).family, "sdxl")
        self.assertEqual(lm.classify(FLUX_FILL).family, "flux-fill")
        for shapes, words in (
            (SD15_PLAIN, "inpainting version"),
            (SD2, "Stable Diffusion 2"),
            (SDXL_INPAINT, "SDXL inpainting"),
            (FLUX_DEV, "not FLUX.1 Fill"),
            ({"some.tensor": [3]}, "not an SD 1.5"),
        ):
            found = lm.classify(shapes)
            self.assertEqual(found.family, "", words)
            self.assertIn(words, found.note)

    def test_prefixes_are_ignored(self):
        prefixed = {"model.diffusion_model." + k: v for k, v in FLUX_FILL.items()}
        self.assertEqual(lm.classify(prefixed).family, "flux-fill")


class HeaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_safetensors_and_gguf_headers(self):
        st = write_safetensors(self.dir / "a.safetensors", SD15_INPAINT, {"format": "pt"})
        self.assertEqual(lm.inspect(st).family, "sd15-inpaint")
        gg = write_gguf(self.dir / "b.gguf", FLUX_FILL)
        self.assertEqual(lm.gguf_shapes(gg)["img_in.weight"], [384, 3072])  # GGUF order
        self.assertEqual(lm.inspect(gg).family, "flux-fill")

    def test_broken_files_never_raise(self):
        (self.dir / "short.safetensors").write_bytes(b"12")
        (self.dir / "huge.safetensors").write_bytes(struct.pack("<Q", 10**12))
        (self.dir / "nogguf.gguf").write_bytes(b"NOPE" + b"\0" * 40)
        (self.dir / "truncated.gguf").write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 5, 0))
        for name in ("short.safetensors", "huge.safetensors", "nogguf.gguf", "truncated.gguf"):
            found = lm.inspect(self.dir / name)
            self.assertEqual(found.family, "", name)
            self.assertIn("unreadable", found.note)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "models"
        self.base = Path(self.tmp.name) / "profile"
        self.base.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_own_files_become_entries_copied_from_their_family(self):
        write_safetensors(self.root / "checkpoints" / "mine" / "myMix-inpainting.safetensors", SD15_INPAINT)
        write_safetensors(self.root / "checkpoints" / "myXL.safetensors", SDXL)
        write_gguf(self.root / "unet" / "fill-q5.gguf", FLUX_FILL)
        write_safetensors(self.root / "checkpoints" / "plain.safetensors", SD15_PLAIN)
        write_gguf(self.root / "checkpoints" / "flux-in-wrong-place.gguf", FLUX_FILL)
        (self.root / "checkpoints" / "pickled.ckpt").write_bytes(b"\x80\x04 never opened")
        found, skipped = lm.scan(self.root, ENTRIES, self.base)
        by_name = {e.files[0].dest: e for e in found}
        self.assertEqual(
            sorted(by_name),
            ["checkpoints/mine/myMix-inpainting.safetensors", "checkpoints/myXL.safetensors", "unet/fill-q5.gguf"],
        )
        sd15 = by_name["checkpoints/mine/myMix-inpainting.safetensors"]
        self.assertEqual((sd15.template, sd15.arch, sd15.source), ("sd15_inpaint", "sd15", "local"))
        self.assertTrue(sd15.files[0].optional)  # never downloaded
        self.assertEqual(cat.model_spec_for(sd15, ENTRIES).checkpoint, "mine/myMix-inpainting.safetensors")
        xl = by_name["checkpoints/myXL.safetensors"]
        self.assertEqual(xl.requires_models, cat.by_id(ENTRIES, "realvisxl-v5").requires_models)  # the Fooocus patch
        flux = by_name["unet/fill-q5.gguf"]
        self.assertEqual({f.role for f in flux.files}, {"unet", "clip1", "clip2", "vae", "preview"})
        self.assertEqual(flux.requires_nodes, cat.by_id(ENTRIES, "flux1-fill-dev-gguf-q4").requires_nodes)
        reasons = {s["file"]: s["reason"] for s in skipped}
        self.assertIn("inpainting version", reasons["checkpoints/plain.safetensors"])
        self.assertIn("unet or diffusion_models", reasons["checkpoints/flux-in-wrong-place.gguf"])
        self.assertNotIn("checkpoints/pickled.ckpt", reasons)
        # ids are stable, so "Set active" survives a restart
        again, _ = lm.scan(self.root, ENTRIES, self.base)
        self.assertEqual(sorted(e.id for e in again), sorted(e.id for e in found))

    def test_catalog_files_keep_their_own_entry(self):
        dream = cat.by_id(ENTRIES, "dreamshaper-8-inpainting").files[0]
        write_safetensors(self.root / dream.dest, SD15_INPAINT)
        found, skipped = lm.scan(self.root, ENTRIES, self.base)
        self.assertEqual((found, skipped), ([], []))

    def test_cache_skips_reading_unchanged_files(self):
        path = write_safetensors(self.root / "checkpoints" / "x.safetensors", SDXL)
        lm.scan(self.root, ENTRIES, self.base)
        self.assertTrue((self.base / lm.CACHE_FILENAME).is_file())
        original = lm.inspect
        lm.inspect = lambda p: self.fail(f"read {p} again")
        try:
            found, _ = lm.scan(self.root, ENTRIES, self.base)
        finally:
            lm.inspect = original
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].files[0].size, path.stat().st_size)

    def test_with_local_never_raises(self):
        self.assertEqual(lm.with_local(ENTRIES, Path(self.tmp.name) / "nowhere"), ENTRIES)
        self.assertEqual(lm.with_local(ENTRIES, None), ENTRIES)


# Companion files of FLUX.1 Fill, as their headers look (only the tell-tale tensors).
T5_ST = {"encoder.block.0.layer.0.SelfAttention.q.weight": [4096, 4096], "shared.weight": [32128, 4096]}
T5_GGUF = {"enc.blk.0.attn_q.weight": [4096, 4096], "token_embd.weight": [32128, 4096]}
CLIP_L = {"text_model.encoder.layers.0.self_attn.q_proj.weight": [768, 768]}
FLUX_VAE = {"decoder.conv_in.weight": [512, 16, 3, 3]}


class CompanionTests(unittest.TestCase):
    """A local FLUX.1 Fill file reuses the text encoders and VAE already in ComfyUI's folders
    (issue #8): other names, formats and the alias folders count, not only the catalog's file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "models"
        self.base = Path(self.tmp.name) / "profile"
        self.base.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def flux_entry(self, main="unet/flux1-fill-dev-Q4_K_S.gguf"):
        if main.endswith(".gguf"):
            write_gguf(self.root / main, FLUX_FILL)
        else:
            write_safetensors(self.root / main, FLUX_FILL)
        found, _ = lm.scan(self.root, ENTRIES, self.base)
        (entry,) = [e for e in found if e.files[0].dest == main]
        return entry

    def dests(self, entry):
        return {f.role: f.dest for f in entry.files}

    def test_issue_8_layout_is_installed(self):
        """unet/ GGUF, T5 fp8 and CLIP-L safetensors in text_encoders/, vae/ae.safetensors."""
        write_safetensors(self.root / "text_encoders" / "t5xxl_fp8_e4m3fn.safetensors", T5_ST)
        write_safetensors(self.root / "text_encoders" / "clip_l.safetensors", CLIP_L)
        write_safetensors(self.root / "vae" / "ae.safetensors", FLUX_VAE)
        entry = self.flux_entry()
        status = cat.install_status(entry, self.root, None, ENTRIES)
        self.assertEqual(status.missing, [])
        self.assertTrue(status.installed)
        d = self.dests(entry)
        self.assertEqual(d["clip1"], "text_encoders/t5xxl_fp8_e4m3fn.safetensors")
        self.assertEqual(d["clip2"], "text_encoders/clip_l.safetensors")
        self.assertEqual(d["vae"], "vae/ae.safetensors")
        for role in ("clip1", "clip2", "vae"):
            f = entry.file_by_role(role)
            self.assertEqual((f.url, f.optional), ("", True))  # found, never downloaded
            self.assertEqual(f.size, (self.root / f.dest).stat().st_size)
        spec = cat.model_spec_for(entry, ENTRIES)
        self.assertEqual(
            (spec.unet, spec.clip1, spec.clip2, spec.vae),
            ("flux1-fill-dev-Q4_K_S.gguf", "t5xxl_fp8_e4m3fn.safetensors", "clip_l.safetensors", "ae.safetensors"),
        )
        self.assertEqual(entry.requires_nodes, cat.by_id(ENTRIES, "flux1-fill-dev-gguf-q4").requires_nodes)

    def sized(self, path, size):
        """Pad a fake file to ``size`` bytes (sparse, so the catalog's sizes cost nothing)."""
        with open(path, "r+b") as fh:
            fh.truncate(size)
        return path

    def test_catalog_layout_is_unchanged(self):
        template = cat.by_id(ENTRIES, "flux1-fill-dev-gguf-q4")
        for f in template.files:
            if f.role in ("clip1", "clip2", "vae"):
                shapes = {"clip1": T5_GGUF, "clip2": CLIP_L, "vae": FLUX_VAE}[f.role]
                writer = write_gguf if f.dest.endswith(".gguf") else write_safetensors
                self.sized(writer(self.root / f.dest, shapes), f.size)
        # a second compatible T5 does not replace the catalog's own file
        write_safetensors(self.root / "text_encoders" / "t5xxl_fp8_e4m3fn.safetensors", T5_ST)
        entry = self.flux_entry()
        self.assertEqual(entry.files[1:], template.files[1:])  # the very same specs, download addresses kept
        self.assertTrue(cat.install_status(entry, self.root, None, ENTRIES).installed)

    def test_nothing_there_falls_back_to_the_download(self):
        write_safetensors(self.root / "text_encoders" / "umt5_xxl_fp8_e4m3fn_scaled.safetensors", T5_ST)  # Wan's
        write_safetensors(self.root / "clip" / "clip_g.safetensors", {"x": [1280, 1280]})
        write_safetensors(self.root / "vae" / "flux2-vae.safetensors", {"decoder.conv_in.weight": [512, 32, 3, 3]})
        write_safetensors(self.root / "vae" / "sdxl_vae.safetensors", {"decoder.conv_in.weight": [512, 4, 3, 3]})
        write_safetensors(self.root / "text_encoders" / "t5xxl_tiny.safetensors", {"x": [512, 512]})  # not XXL
        (self.root / "text_encoders" / "t5xxl_fp16.safetensors").write_bytes(b"broken")
        entry = self.flux_entry()
        template = cat.by_id(ENTRIES, "flux1-fill-dev-gguf-q4")
        self.assertEqual(entry.files[1:], template.files[1:])
        status = cat.install_status(entry, self.root, None, ENTRIES)
        self.assertEqual(status.missing, [f.dest for f in template.files[1:] if f.role != cat.PREVIEW_ROLE])

    def test_mixed_folders_and_formats(self):
        """A GGUF T5 under another name in clip/, CLIP-L in text_encoders/ (in a sub folder)."""
        write_gguf(self.root / "clip" / "t5-v1_1-xxl-encoder-Q8_0.gguf", T5_GGUF)
        write_safetensors(self.root / "text_encoders" / "flux" / "clip_l.safetensors", CLIP_L)
        write_safetensors(self.root / "vae" / "flux_vae.safetensors", FLUX_VAE)
        entry = self.flux_entry()
        d = self.dests(entry)
        self.assertEqual(d["clip1"], "clip/t5-v1_1-xxl-encoder-Q8_0.gguf")
        self.assertEqual(d["clip2"], "text_encoders/flux/clip_l.safetensors")
        self.assertEqual(d["vae"], "vae/flux_vae.safetensors")
        self.assertTrue(cat.install_status(entry, self.root, None, ENTRIES).installed)
        spec = cat.model_spec_for(entry, ENTRIES)
        self.assertEqual((spec.clip1, spec.clip2), ("t5-v1_1-xxl-encoder-Q8_0.gguf", "flux/clip_l.safetensors"))

    def test_only_the_missing_companions_are_downloaded(self):
        write_safetensors(self.root / "text_encoders" / "clip_l.safetensors", CLIP_L)
        entry = self.flux_entry()
        template = cat.by_id(ENTRIES, "flux1-fill-dev-gguf-q4")
        self.assertEqual(entry.file_by_role("clip1"), template.file_by_role("clip1"))
        self.assertEqual(entry.file_by_role("clip2").dest, "text_encoders/clip_l.safetensors")
        missing = cat.install_status(entry, self.root, None, ENTRIES).missing
        self.assertEqual(missing, ["clip/t5-v1_1-xxl-encoder-Q5_K_M.gguf", "vae/ae.safetensors"])

    def test_several_candidates_pick_the_same_file_every_time(self):
        q5 = cat.by_id(ENTRIES, "flux1-fill-dev-gguf-q4").file_by_role("clip1")
        fp16 = self.sized(write_safetensors(self.root / "clip" / "t5xxl_fp16.safetensors", T5_ST), 9_787_841_024)
        fp8 = self.sized(write_safetensors(self.root / "text_encoders" / "t5xxl_fp8.safetensors", T5_ST), 4_893_934_904)
        # closest in size to the catalog's Q5_K_M: the fp8 beats fp16, whatever the folder
        self.assertEqual(lm.resolve_companion(self.root, q5, "t5xxl").dest, "text_encoders/t5xxl_fp8.safetensors")
        # same size: the catalog's folder (clip/) first, then alphabetical
        self.sized(write_safetensors(self.root / "clip" / "t5xxl_fp8_b.safetensors", T5_ST), 4_893_934_904)
        self.sized(write_safetensors(self.root / "clip" / "t5xxl_fp8_a.safetensors", T5_ST), 4_893_934_904)
        for _ in range(3):
            self.assertEqual(lm.resolve_companion(self.root, q5, "t5xxl").dest, "clip/t5xxl_fp8_a.safetensors")
        # the catalog's file name wins over a closer size, wherever it is
        named = write_gguf(self.root / "text_encoders" / q5.filename, T5_GGUF)
        self.assertEqual(lm.resolve_companion(self.root, q5, "t5xxl").dest, "text_encoders/" + q5.filename)
        named.unlink()
        fp8.unlink()
        fp16.unlink()
        self.assertEqual(lm.resolve_companion(self.root, q5, "t5xxl").dest, "clip/t5xxl_fp8_a.safetensors")

    def test_safetensors_flux_with_a_gguf_text_encoder_needs_the_gguf_node(self):
        write_gguf(self.root / "clip" / "t5-v1_1-xxl-encoder-Q5_K_M.gguf", T5_GGUF)
        entry = self.flux_entry("diffusion_models/my-fill.safetensors")
        self.assertEqual(entry.file_by_role("clip1").dest, "clip/t5-v1_1-xxl-encoder-Q5_K_M.gguf")
        self.assertEqual(cat.by_id(ENTRIES, "flux1-fill-dev").requires_nodes, ())
        self.assertEqual([n.folder for n in entry.requires_nodes], ["ComfyUI-GGUF"])
        # with safetensors encoders it stays on ComfyUI's own loaders
        (self.root / "clip" / "t5-v1_1-xxl-encoder-Q5_K_M.gguf").unlink()
        write_safetensors(self.root / "clip" / "t5xxl_fp16.safetensors", T5_ST)
        entry = self.flux_entry("diffusion_models/my-fill.safetensors")
        self.assertEqual(entry.requires_nodes, ())

    def test_the_removal_lora_runs_on_the_local_file_and_its_encoders(self):
        write_safetensors(self.root / "text_encoders" / "t5xxl_fp8_e4m3fn.safetensors", T5_ST)
        write_safetensors(self.root / "text_encoders" / "clip_l.safetensors", CLIP_L)
        write_safetensors(self.root / "vae" / "ae.safetensors", FLUX_VAE)
        entry = self.flux_entry()
        entries = [*ENTRIES, entry]
        lora = cat.by_id(entries, "flux1-fill-object-removal-lora")
        spec = cat.model_spec_for(lora, entries, self.root)
        self.assertEqual((spec.unet, spec.clip1), ("flux1-fill-dev-Q4_K_S.gguf", "t5xxl_fp8_e4m3fn.safetensors"))

    def test_compatible_by_name_and_header(self):
        ok = {
            "t5xxl": [
                ("text_encoders/t5xxl_fp16.safetensors", T5_ST),
                ("text_encoders/T5XXL-FP8-e4m3fn-scaled.safetensors", T5_ST),
                ("clip/t5-v1_1-xxl-encoder-Q3_K_S.gguf", T5_GGUF),
            ],
            "clip_l": [("clip/clip_l.safetensors", CLIP_L), ("clip/clip_l_fp16.safetensors", CLIP_L)],
            "flux_vae": [("vae/ae.safetensors", FLUX_VAE), ("vae/flux1-vae.safetensors", FLUX_VAE)],
        }
        bad = {
            "t5xxl": [("text_encoders/umt5-xxl-enc-bf16.safetensors", T5_ST), ("clip/t5xxl.ckpt", T5_ST)],
            "clip_l": [("clip/clip_g.safetensors", CLIP_L), ("clip/clip_l.safetensors", {"x": [1280, 1280]})],
            "flux_vae": [("vae/sd3_vae.safetensors", FLUX_VAE), ("vae/ae.safetensors", {"x": [3]})],
        }
        for expected, table in ((True, ok), (False, bad)):
            for kind, files in table.items():
                for rel, shapes in files:
                    path = self.root / "case" / str(expected) / rel
                    if path.suffix == ".gguf":
                        write_gguf(path, shapes)
                    else:
                        write_safetensors(path, shapes)
                    self.assertEqual(lm.compatible(kind, path), expected, (kind, rel))


if __name__ == "__main__":
    unittest.main()
