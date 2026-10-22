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


if __name__ == "__main__":
    unittest.main()
