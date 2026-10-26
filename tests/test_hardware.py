# SPDX-License-Identifier: Apache-2.0
"""Hardware detection parsing and the "fits this PC" verdict, with fake hardware."""

import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from openfill import hardware as hwmod
from openfill.catalog import FileSpec, load_catalog

GB = 1024**3

REAL_OS_ADAPTERS = hwmod.os_adapters
_no_adapters = mock.patch.object(hwmod, "os_adapters", lambda: None)  # this PC's own cards stay out of the tests


def setUpModule():
    _no_adapters.start()


def tearDownModule():
    _no_adapters.stop()


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


# DXGI_ADAPTER_DESC1 rows as dxgi_descs returns them: (description, vendor id, dedicated memory, flags).
DXGI_AMD_LAPTOP = [
    ("AMD Radeon(TM) 780M", 0x1002, 512 * 1024**2, 0),
    ("AMD Radeon RX 7900 XT", 0x1002, 20 * GB, 0),
    ("Microsoft Basic Render Driver", 0x1414, 0, 2),
]


class AdapterTests(unittest.TestCase):
    def test_dxgi_picks_the_radeon_with_the_most_memory(self):
        found = hwmod.adapters_from_dxgi(lambda: DXGI_AMD_LAPTOP)
        self.assertEqual([a.name for a in found], ["AMD Radeon(TM) 780M", "AMD Radeon RX 7900 XT"])  # no software
        best = hwmod.pick_adapter(found)
        self.assertEqual(
            (best.vendor, best.name, best.vram_bytes, best.source), ("amd", "AMD Radeon RX 7900 XT", 20 * GB, "dxgi")
        )

    def test_dxgi_reads_more_than_4_gb(self):
        found = hwmod.adapters_from_dxgi(lambda: [("AMD Radeon RX 7900 XTX", 0x1002, 24 * GB, 0)])
        self.assertEqual(found[0].vram_bytes, 24 * GB)

    def test_radeon_before_intel(self):
        found = [
            hwmod.Adapter("intel", "Intel Arc A770", 16 * GB, "dxgi"),
            hwmod.Adapter("amd", "AMD Radeon RX 7600", 8 * GB, "dxgi"),
        ]
        self.assertEqual(hwmod.pick_adapter(found).vendor, "amd")
        self.assertIsNone(hwmod.pick_adapter([hwmod.Adapter("", "Mystery", 4 * GB, "dxgi")]))
        self.assertIsNone(hwmod.pick_adapter(None))

    def test_registry_uses_the_64_bit_memory_size(self):
        keys = [
            {
                "DriverDesc": "AMD Radeon RX 7900 XT",
                "MatchingDeviceId": "PCI\\VEN_1002&DEV_744C&REV_CC",
                "HardwareInformation.qwMemorySize": 20 * GB,
                "HardwareInformation.MemorySize": 0xFFF00000,  # 32 bits: stops at 4 GB
            },
            {"DriverDesc": "Microsoft Basic Display Adapter"},
            {
                "HardwareInformation.AdapterString": "AMD Radeon RX 6600".encode("utf-16-le") + b"\x00\x00",
                "MatchingDeviceId": "pci\\ven_1002&dev_73ff",
                "HardwareInformation.MemorySize": (8 * GB - 1).to_bytes(8, "little")[:4],
            },
        ]
        found = hwmod.adapters_from_registry(lambda: keys)
        self.assertEqual(len(found), 2)
        self.assertEqual((found[0].vendor, found[0].vram_bytes, found[0].source), ("amd", 20 * GB, "registry"))
        self.assertEqual((found[1].vendor, found[1].name), ("amd", "AMD Radeon RX 6600"))
        self.assertEqual(found[1].vram_bytes, int.from_bytes((8 * GB - 1).to_bytes(8, "little")[:4], "little"))

    def test_registry_qword_as_bytes(self):
        keys = [
            {"DriverDesc": "AMD Radeon RX 9070 XT", "HardwareInformation.qwMemorySize": (16 * GB).to_bytes(8, "little")}
        ]
        found = hwmod.adapters_from_registry(lambda: keys)
        self.assertEqual((found[0].vendor, found[0].vram_bytes), ("amd", 16 * GB))

    def _card(self, root, n, vendor, device, vram=None, product=None):
        from pathlib import Path

        dev = Path(root) / f"card{n}" / "device"
        dev.mkdir(parents=True)
        (dev / "vendor").write_text(f"{vendor:#06x}\n")
        (dev / "device").write_text(f"{device:#06x}\n")
        if vram is not None:
            (dev / "mem_info_vram_total").write_text(f"{vram}\n")
        if product is not None:
            (dev / "product_name").write_text(product + "\n")
        (Path(root) / f"card{n}-DP-1").mkdir()  # connectors are not cards

    def test_sysfs_amd_card(self):
        with tempfile.TemporaryDirectory() as d:
            self._card(d, 0, 0x8086, 0x3E9B)
            self._card(d, 1, 0x1002, 0x744C, 20 * GB)
            names = {
                (0x1002, 0x744C): "Radeon RX 7900 XT/7900 XTX/7900 GRE/7900M",
                (0x8086, 0x3E9B): "UHD Graphics 630",
            }
            from pathlib import Path

            found = hwmod.adapters_from_sysfs(Path(d), lambda v, dv: names.get((v, dv), ""), lambda: {})
        self.assertEqual([a.vendor for a in found], ["intel", "amd"])
        best = hwmod.pick_adapter(found)
        self.assertEqual(best.name, "AMD Radeon RX 7900 XT/7900 XTX/7900 GRE/7900M")
        self.assertEqual((best.vram_bytes, best.source), (20 * GB, "sysfs"))

    def test_sysfs_prefers_product_name_then_rocm_smi(self):
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            self._card(d, 0, 0x1002, 0x744C, 20 * GB, product="Radeon RX 7900 XT")
            self._card(d, 1, 0x1002, 0x7550, 16 * GB)
            found = hwmod.adapters_from_sysfs(
                Path(d), lambda v, dv: "", lambda: {"card1": ("AMD Radeon RX 9070 XT", 16 * GB)}
            )
        self.assertEqual([a.name for a in found], ["AMD Radeon RX 7900 XT", "AMD Radeon RX 9070 XT"])

    def test_sysfs_missing_is_empty(self):
        from pathlib import Path

        self.assertEqual(hwmod.adapters_from_sysfs(Path("/nonexistent/drm")), [])

    def test_pci_ids_name(self):
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            ids = Path(d) / "pci.ids"
            ids.write_text(
                "# comment\n1002  Advanced Micro Devices, Inc. [AMD/ATI]\n\t7448  Navi 31 [Radeon Pro W7900]\n"
                "# a comment inside the vendor\n\t744c  Navi 31 [Radeon RX 7900 XT/7900 XTX/7900 GRE/7900M]\n"
                "\t\t1002 0e3b  RX 7900 XTX\n10de  NVIDIA Corporation\n\t744c  Not this one\n",
                encoding="utf-8",
            )
            self.assertEqual(hwmod.pci_ids_name(0x1002, 0x744C, (ids,)), "Radeon RX 7900 XT/7900 XTX/7900 GRE/7900M")
            self.assertEqual(hwmod.pci_ids_name(0x1002, 0x1234, (ids,)), "")
            self.assertEqual(hwmod.pci_ids_name(0x1002, 0x744C, (Path(d) / "missing",)), "")

    def test_parse_rocm_smi(self):
        text = (
            '{"card0": {"Card Series": "Radeon RX 7900 XTX", "Card Model": "0x744c", '
            '"VRAM Total Memory (B)": "25753026560", "VRAM Total Used Memory (B)": "1"}, "system": {}}'
        )
        self.assertEqual(hwmod.parse_rocm_smi(text), {"card0": ("Radeon RX 7900 XTX", 25753026560)})
        self.assertEqual(hwmod.parse_rocm_smi("not json"), {})

    def test_vendor_from_name(self):
        cases = {
            "cuda:0 AMD Radeon RX 7900 XTX : native": "amd",
            "NVIDIA GeForce RTX 4070": "nvidia",
            "Intel(R) Arc(TM) A770 Graphics": "intel",
            "Apple M3": "apple",
            "": "",
        }
        for name, vendor in cases.items():
            self.assertEqual(hwmod.vendor_from_name(name), vendor, name)


class AmdDetectTests(unittest.TestCase):
    def test_amd_card_after_nvidia_tools(self):
        cards = lambda: [hwmod.Adapter("amd", "AMD Radeon RX 7900 XT", 20 * GB, "dxgi")]  # noqa: E731
        got = hwmod.detect(
            None, "http://x", nvidia=lambda: None, nvml=lambda: None, adapters=cards, comfy=lambda _u: None
        )
        self.assertEqual(
            (got.gpu_name, got.vram_bytes, got.gpu_source, got.gpu_vendor),
            ("AMD Radeon RX 7900 XT", 20 * GB, "dxgi", "amd"),
        )
        self.assertTrue(got.is_amd)
        self.assertIsNone(got.compute_capability)
        self.assertIn("20 GB graphics memory", got.summary())

    def test_nvidia_tools_win(self):
        cards = lambda: [hwmod.Adapter("amd", "AMD Radeon RX 7900 XT", 20 * GB, "dxgi")]  # noqa: E731
        got = hwmod.detect(None, None, nvidia=lambda: ("NVIDIA RTX 4090", 24 * GB), adapters=cards, compute=lambda: 8.9)
        self.assertEqual((got.gpu_vendor, got.gpu_source), ("nvidia", "nvidia-smi"))

    def test_card_without_memory_waits_for_comfyui(self):
        cards = lambda: [hwmod.Adapter("amd", "AMD Radeon RX 7900 XT", None, "sysfs")]  # noqa: E731
        got = hwmod.detect(
            None,
            "http://x",
            nvidia=lambda: None,
            nvml=lambda: None,
            adapters=cards,
            comfy=lambda _u: ("AMD Radeon RX 7900 XT", 20 * GB, 64 * GB),
        )
        self.assertEqual((got.gpu_source, got.gpu_vendor, got.vram_bytes), ("comfyui", "amd", 20 * GB))
        got = hwmod.detect(None, None, nvidia=lambda: None, nvml=lambda: None, adapters=cards, apple=lambda: None)
        self.assertEqual((got.gpu_name, got.vram_bytes, got.gpu_vendor), ("AMD Radeon RX 7900 XT", None, "amd"))

    def test_none_and_unknown(self):
        got = hwmod.detect(None, None, nvidia=lambda: None, nvml=lambda: None, adapters=lambda: [], apple=lambda: None)
        self.assertEqual(got.gpu_vendor, "none")
        got = hwmod.detect(
            None, None, nvidia=lambda: None, nvml=lambda: None, adapters=lambda: None, apple=lambda: None
        )
        self.assertEqual(got.gpu_vendor, "")

        def boom():
            raise OSError("no dxgi")

        got = hwmod.detect(None, None, nvidia=lambda: None, nvml=lambda: None, adapters=boom, apple=lambda: None)
        self.assertEqual(got.gpu_vendor, "")

    def test_os_adapters_falls_back_to_the_registry(self):
        reg = [hwmod.Adapter("amd", "AMD Radeon RX 7600", 8 * GB, "registry")]
        with (
            mock.patch.object(hwmod, "adapters_from_dxgi", side_effect=OSError("no dxgi.dll")),
            mock.patch.object(hwmod, "adapters_from_registry", return_value=reg),
        ):
            self.assertEqual(REAL_OS_ADAPTERS("win32"), reg)
        with (
            mock.patch.object(hwmod, "adapters_from_dxgi", side_effect=OSError("x")),
            mock.patch.object(hwmod, "adapters_from_registry", side_effect=OSError("y")),
        ):
            self.assertIsNone(REAL_OS_ADAPTERS("win32"))
        self.assertIsNone(REAL_OS_ADAPTERS("darwin"))

    def test_fit_works_for_an_amd_card(self):
        amd = hwmod.Hardware("AMD Radeon RX 7900 XT", 20 * GB, 32 * GB, 400 * GB, "Windows 11", "dxgi", None, "amd")
        self.assertEqual(hwmod.fit(entry("sdxl", 8.0), amd).level, hwmod.COMFORTABLE)

    def test_to_json_has_the_vendor(self):
        self.assertEqual(hwmod.Hardware(gpu_vendor="amd").to_json()["gpu_vendor"], "amd")


class AmdBuildSupportTests(unittest.TestCase):
    def test_generations(self):
        cases = {
            "AMD Radeon RX 7900 XTX": "yes",
            "AMD Radeon RX 7600S": "yes",
            "Radeon RX 9070/9070 XT/9070 GRE": "yes",
            "AMD Radeon PRO W7800": "yes",
            "AMD Radeon AI PRO R9700": "yes",
            "AMD Radeon(TM) 890M Graphics": "yes",
            "AMD Radeon(TM) 8060S Graphics": "yes",
            "AMD Radeon RX 6700 XT": "no",
            "AMD Radeon RX 5700": "no",
            "Radeon RX 580 Series": "no",
            "AMD Radeon RX Vega 56": "no",
            "AMD Radeon 780M Graphics": "no",
            "AMD Radeon R9 390": "no",
            "AMD Radeon HD 7970": "no",
            "AMD Radeon(TM) Graphics": "unknown",
            "": "unknown",
        }
        for name, want in cases.items():
            self.assertEqual(hwmod.amd_build_support(name), want, name)


class RocmEnvTests(unittest.TestCase):
    def test_overrides_from_comfyui_readme(self):
        self.assertEqual(hwmod.rocm_env([100301], {}, "linux"), {"HSA_OVERRIDE_GFX_VERSION": "10.3.0"})  # RX 6700
        self.assertEqual(hwmod.rocm_env([100302], {}, "linux"), {"HSA_OVERRIDE_GFX_VERSION": "10.3.0"})  # RX 6600
        self.assertEqual(hwmod.rocm_env([110003], {}, "linux"), {"HSA_OVERRIDE_GFX_VERSION": "11.0.0"})  # 780M

    def test_not_needed_or_not_ours_to_set(self):
        self.assertEqual(hwmod.rocm_env([100300], {}, "linux"), {})  # RX 6800/6900: PyTorch has its code
        self.assertEqual(hwmod.rocm_env([110000], {}, "linux"), {})  # RX 7900
        self.assertEqual(hwmod.rocm_env([120001], {}, "linux"), {})  # RX 9070
        self.assertEqual(hwmod.rocm_env([], {}, "linux"), {})
        self.assertEqual(hwmod.rocm_env([100301], {"HSA_OVERRIDE_GFX_VERSION": "10.3.1"}, "linux"), {})
        self.assertEqual(hwmod.rocm_env([100301, 110000], {}, "linux"), {})  # two kinds of card: leave it
        self.assertEqual(hwmod.rocm_env([100301], {}, "win32"), {})

    def test_reads_the_kfd_topology(self):
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            for n, target in ((0, 0), (1, 100301)):
                node = Path(d) / str(n)
                node.mkdir()
                (node / "properties").write_text(f"cpu_cores_count 8\ngfx_target_version {target}\n")
            self.assertEqual(hwmod.amd_gfx_targets(Path(d)), [100301])
        self.assertEqual(hwmod.amd_gfx_targets(Path("/nonexistent/kfd")), [])


@unittest.skipUnless(sys.platform == "win32", "DXGI and the display registry keys are Windows")
class WindowsAdapterSmokeTests(unittest.TestCase):
    """On a real Windows (the CI runner has only Microsoft's software adapter): the DXGI
    calls and the registry walk run without raising and give plain values."""

    def test_dxgi_descs(self):
        descs = hwmod.dxgi_descs()
        self.assertIsInstance(descs, list)
        self.assertTrue(descs, "DXGI lists at least the Microsoft Basic Render Driver")
        for name, vendor_id, vram, flags in descs:
            self.assertIsInstance(name, str)
            self.assertGreaterEqual(vendor_id, 0)
            self.assertGreaterEqual(vram, 0)
            self.assertIsInstance(flags, int)

    def test_registry_and_os_adapters(self):
        self.assertIsInstance(hwmod.registry_display_keys(), list)
        found = REAL_OS_ADAPTERS("win32")
        self.assertTrue(found is None or isinstance(found, list))
        for adapter in found or []:
            self.assertNotIn("basic render", adapter.name.lower())
