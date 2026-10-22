# SPDX-License-Identifier: Apache-2.0
"""Hardware detection parsing and the "fits this PC" verdict, with fake hardware."""

import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from openfill import hardware as hwmod
from openfill.catalog import FileSpec, load_catalog

GB = 1024**3

# Shape of a real ComfyUI 0.39 /system_stats answer (RTX 4070 Laptop, 2026-10-09), trimmed.
SYSTEM_STATS = {
    "system": {"os": "win32", "ram_total": 33598619648, "ram_free": 13332160512, "comfyui_version": "0.39.0"},
    "devices": [
        {
            "name": "cuda:0 NVIDIA GeForce RTX 4070 Laptop GPU : cudaMallocAsync",
            "type": "cuda",
            "index": 0,
            "vram_total": 8585216000,
            "vram_free": 609890216,
        }
    ],
}


class AppleMemoryTests(unittest.TestCase):
    def test_unified_memory_is_budgeted(self):
        data = {
            "system": {"ram_total": 16 * GB},
            "devices": [{"name": "mps", "type": "mps", "vram_total": 16 * GB}],
        }
        _name, vram, ram = hwmod.parse_system_stats(data)
        self.assertEqual(vram, int(16 * GB * 0.7))
        self.assertEqual(vram + ram, 16 * GB)


def hw(vram_gb=None, ram_gb=32, disk_gb=400):
    return hwmod.Hardware(
        "Test GPU" if vram_gb else None,
        int(vram_gb * GB) if vram_gb else None,
        int(ram_gb * GB) if ram_gb else None,
        int(disk_gb * GB) if disk_gb is not None else None,
        "Windows 11",
        "nvidia-smi" if vram_gb else "",
    )


def entry(arch="sdxl", min_vram=0.0, sizes=((("checkpoint"), 7 * GB),)):
    files = tuple(FileSpec(role, "", f"x/{i}.safetensors", size) for i, (role, size) in enumerate(sizes))
    return SimpleNamespace(arch=arch, min_vram_gb=min_vram, files=files, total_size=sum(f.size for f in files), id="t")


class ParseTests(unittest.TestCase):
    def test_nvidia_smi_picks_largest_card(self):
        text = "NVIDIA GeForce RTX 4070 Laptop GPU, 8188\nNVIDIA RTX A6000, 49140\ngarbage line\n"
        name, size = hwmod.parse_nvidia_smi(text)
        self.assertEqual(name, "NVIDIA RTX A6000")
        self.assertEqual(size, 49140 * 1024 * 1024)
        self.assertIsNone(hwmod.parse_nvidia_smi(""))
        self.assertIsNone(hwmod.parse_nvidia_smi("No devices were found"))

    def test_system_stats(self):
        name, vram, ram = hwmod.parse_system_stats(SYSTEM_STATS)
        self.assertEqual(name, "NVIDIA GeForce RTX 4070 Laptop GPU")
        self.assertEqual(vram, 8585216000)
        self.assertEqual(ram, 33598619648)
        cpu_only = {"system": {"ram_total": 1}, "devices": [{"name": "cpu", "type": "cpu", "vram_total": 0}]}
        self.assertIsNone(hwmod.parse_system_stats(cpu_only))
        self.assertIsNone(hwmod.parse_system_stats({}))

    def test_nvidia_smi_failure_is_unknown(self):
        def runner(*_a, **_k):
            raise OSError("not found")

        self.assertIsNone(hwmod.gpu_from_nvidia_smi(runner=runner))

    def test_comfyui_not_running_is_unknown(self):
        def opener(*_a, **_k):
            raise OSError("refused")

        self.assertIsNone(hwmod.gpu_from_comfyui("http://127.0.0.1:1", opener=opener))
        self.assertIsNone(hwmod.gpu_from_comfyui(""))


class _FakeNvml:
    """Stands in for libnvidia-ml: fills the ctypes out-parameters like the real library."""

    def __init__(self, cards, init=0):
        self.cards, self.init, self.shut_down = cards, init, False

    def nvmlInit_v2(self):
        return self.init

    def nvmlShutdown(self):
        self.shut_down = True
        return 0

    def nvmlDeviceGetCount_v2(self, count):
        count._obj.value = len(self.cards)
        return 0

    def nvmlDeviceGetHandleByIndex_v2(self, i, handle):
        handle._obj.value = i + 1
        return 0

    def nvmlDeviceGetMemoryInfo(self, handle, mem):
        mem._obj.total = self.cards[handle.value - 1][1]
        return 0

    def nvmlDeviceGetName(self, handle, buf, size):
        buf.value = self.cards[handle.value - 1][0].encode()
        return 0


class DetectTests(unittest.TestCase):
    def test_nvidia_first(self):
        with tempfile.TemporaryDirectory() as d:
            got = hwmod.detect(d, "http://x", nvidia=lambda: ("Card", 8 * GB), comfy=lambda _u: ("Other", 1, 1))
        self.assertEqual((got.gpu_name, got.vram_bytes, got.gpu_source), ("Card", 8 * GB, "nvidia-smi"))
        self.assertIsNotNone(got.disk_free_bytes)
        self.assertTrue(got.os_name)

    def test_comfyui_second_then_unknown(self):
        got = hwmod.detect(
            None, "http://x", nvidia=lambda: None, nvml=lambda: None, comfy=lambda _u: ("Arc", 16 * GB, 64 * GB)
        )
        self.assertEqual((got.gpu_name, got.vram_bytes, got.gpu_source), ("Arc", 16 * GB, "comfyui"))
        got = hwmod.detect(None, None, nvidia=lambda: None, nvml=lambda: None, apple=lambda: None)
        self.assertIsNone(got.vram_bytes)
        self.assertEqual(got.gpu_source, "")
        self.assertIn("not detected", got.summary())

    def test_detect_never_raises(self):
        def boom(*_a):
            raise RuntimeError("x")

        got = hwmod.detect(None, "http://x", nvidia=boom, nvml=boom, comfy=boom, apple=boom)
        self.assertIsNone(got.vram_bytes)

    def test_nvml_when_nvidia_smi_is_missing(self):
        got = hwmod.detect(
            None, "http://x", nvidia=lambda: None, nvml=lambda: ("RTX 2070", 8 * GB), comfy=lambda _u: ("x", 1, 1)
        )
        self.assertEqual((got.gpu_name, got.vram_bytes, got.gpu_source), ("RTX 2070", 8 * GB, "nvml"))

    def test_gpu_from_nvml(self):
        lib = _FakeNvml([("Small", 4 * GB), ("NVIDIA GeForce RTX 2070", 8 * GB)])
        self.assertEqual(hwmod.gpu_from_nvml(lambda: lib), ("NVIDIA GeForce RTX 2070", 8 * GB))
        self.assertTrue(lib.shut_down)
        self.assertIsNone(hwmod.gpu_from_nvml(lambda: None))
        self.assertIsNone(hwmod.gpu_from_nvml(lambda: _FakeNvml([], init=9)))
        self.assertIsNone(hwmod.gpu_from_nvml(lambda: _FakeNvml([])))

        def missing():
            raise OSError("no libnvidia-ml")

        self.assertIsNone(hwmod.gpu_from_nvml(missing))

    def test_apple_silicon_after_comfyui(self):
        chip = ("Apple M3", 11 * GB, 5 * GB)
        got = hwmod.detect(None, None, nvidia=lambda: None, nvml=lambda: None, apple=lambda: chip)
        self.assertEqual((got.gpu_name, got.vram_bytes, got.ram_bytes, got.gpu_source), (*chip, "apple-silicon"))
        got = hwmod.detect(
            None,
            "http://x",
            nvidia=lambda: None,
            nvml=lambda: None,
            comfy=lambda _u: ("mps", 9 * GB, 4 * GB),
            apple=lambda: chip,
        )
        self.assertEqual(got.gpu_source, "comfyui")  # a running ComfyUI knows best

    def test_gpu_from_apple_silicon(self):
        out = SimpleNamespace(returncode=0, stdout="Apple M3\n17179869184\n")
        got = hwmod.gpu_from_apple_silicon(lambda *a, **k: out, "darwin", "arm64")
        self.assertEqual(got, ("Apple M3", int(16 * GB * 0.7), 16 * GB - int(16 * GB * 0.7)))
        self.assertIsNone(hwmod.gpu_from_apple_silicon(lambda *a, **k: out, "darwin", "x86_64"))  # Intel Mac
        self.assertIsNone(hwmod.gpu_from_apple_silicon(lambda *a, **k: out, "linux", "arm64"))
        bad = SimpleNamespace(returncode=0, stdout="garbage")
        self.assertIsNone(hwmod.gpu_from_apple_silicon(lambda *a, **k: bad, "darwin", "arm64"))

    def test_disk_free_uses_existing_parent(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNotNone(hwmod.disk_free(f"{d}/not/yet/created"))
        self.assertIsNone(hwmod.disk_free(None))

    def test_summary(self):
        text = hw(8, 32, 400).summary()
        self.assertIn("Test GPU", text)
        self.assertIn("8 GB graphics memory", text)
        self.assertIn("400 GB free disk space", text)


class FitTests(unittest.TestCase):
    def test_declared_vram_comfortable_and_offload(self):
        e = entry("sdxl", 8.0)
        self.assertEqual(hwmod.fit(e, hw(8)).level, hwmod.COMFORTABLE)  # 8 GB cards report a little less
        self.assertEqual(hwmod.fit(e, hw(7.8)).level, hwmod.COMFORTABLE)
        self.assertEqual(hwmod.fit(e, hw(6)).level, hwmod.OFFLOAD)
        self.assertEqual(hwmod.fit(e, hw(3)).level, hwmod.TOO_BIG)

    def test_offload_needs_system_memory(self):
        e = entry("qwen_image", 12.0, (("unet", 13 * GB),))
        self.assertEqual(hwmod.fit(e, hw(10, ram_gb=32)).level, hwmod.OFFLOAD)
        self.assertEqual(hwmod.fit(e, hw(8, ram_gb=32)).level, hwmod.TOO_BIG)  # Qwen needs more than 8 GB
        verdict = hwmod.fit(e, hw(10, ram_gb=6))
        self.assertEqual(verdict.level, hwmod.TOO_BIG)
        self.assertIn("system memory", verdict.reason)

    def test_unknown_gpu(self):
        verdict = hwmod.fit(entry("sd15", 4.0), hw(None))
        self.assertEqual(verdict.level, hwmod.UNKNOWN)
        self.assertFalse(verdict.fits)
        self.assertEqual(verdict.label, "Could not check this PC")

    def test_disk_space(self):
        e = entry("sd15", 4.0, (("checkpoint", 4 * GB),))
        verdict = hwmod.fit(e, hw(24, disk_gb=3))
        self.assertEqual(verdict.level, hwmod.TOO_BIG)
        self.assertIn("disk space", verdict.reason)
        # most of it already downloaded: fits
        self.assertEqual(hwmod.fit(e, hw(24, disk_gb=3), already_on_disk=3 * GB).level, hwmod.COMFORTABLE)

    def test_estimate_without_declared_vram(self):
        flux = entry("flux", 0, (("unet", int(6.8 * GB)), ("clip1", 3 * GB)))
        need = hwmod.vram_needed_gb(flux)
        self.assertAlmostEqual(need, 8.8, places=1)  # diffusion weights + working memory; encoders not counted
        self.assertEqual(hwmod.fit(flux, hw(12)).level, hwmod.COMFORTABLE)
        self.assertEqual(hwmod.fit(flux, hw(8)).level, hwmod.OFFLOAD)
        self.assertEqual(hwmod.fit(flux, hw(4)).level, hwmod.TOO_BIG)

    def test_shipped_catalog_on_an_8gb_laptop(self):
        laptop = hw(8, 32, 400)
        verdicts = {e.id: hwmod.fit(e, laptop).level for e in load_catalog()}
        self.assertEqual(verdicts["sd15-inpainting"], hwmod.COMFORTABLE)
        self.assertEqual(verdicts["lama-big"], hwmod.COMFORTABLE)
        self.assertEqual(verdicts["qwen-image-edit-2511-gguf-q4"], hwmod.TOO_BIG)  # needs more than 8 GB
        self.assertEqual(verdicts["flux1-fill-dev"], hwmod.OFFLOAD)
        self.assertEqual({k for k, v in verdicts.items() if v == hwmod.TOO_BIG}, {"qwen-image-edit-2511-gguf-q4"})
        tiny = hw(2, 8, 400)
        self.assertEqual(hwmod.fit(next(e for e in load_catalog() if e.id == "flux1-fill-dev"), tiny).level, "too_big")


if __name__ == "__main__":
    unittest.main()


class BackendDeviceTests(unittest.TestCase):
    def test_cpu_gpu_and_unknown(self):
        cpu = {"devices": [{"name": "cpu", "type": "cpu", "vram_total": 32 * GB}]}
        self.assertEqual(hwmod.backend_device(cpu), ("cpu", "cpu (cpu, 32.0 GB)"))
        cuda = {
            "devices": [
                {"name": "cuda:0 NVIDIA GeForce RTX 3060 : cudaMallocAsync", "type": "cuda", "vram_total": 12 * GB}
            ]
        }
        kind, desc = hwmod.backend_device(cuda)
        self.assertEqual(kind, "gpu")
        self.assertIn("RTX 3060", desc)
        self.assertEqual(hwmod.backend_device({}), ("unknown", ""))


class ComputeCapabilityTests(unittest.TestCase):
    def test_parse_takes_the_lowest_card(self):
        self.assertEqual(hwmod.parse_compute_capability("8.6\n6.1\n"), 6.1)
        self.assertIsNone(hwmod.parse_compute_capability("[N/A]\n"))

    def test_detect_reads_it_for_nvidia_only(self):
        got = hwmod.detect(None, None, nvidia=lambda: ("GTX 1060", 6 * GB), compute=lambda: 6.1)
        self.assertEqual(got.compute_capability, 6.1)
        got = hwmod.detect(None, None, nvidia=lambda: None, nvml=lambda: None, apple=lambda: None, compute=lambda: 6.1)
        self.assertIsNone(got.compute_capability)

    def test_from_nvidia_smi(self):
        with mock.patch.object(hwmod.shutil, "which", return_value="/usr/bin/nvidia-smi"):
            cap = hwmod.gpu_compute_capability(runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="6.1\n"))
        self.assertEqual(cap, 6.1)
