# SPDX-License-Identifier: Apache-2.0
"""Ryzen Radeon graphics next to an Arc or an RX 6000 card, and AMD packages left over from an earlier torch."""

import tempfile
import unittest
from pathlib import Path

from openfill import comfy_setup
from openfill import hardware as hwmod
from openfill.hardware import Hardware

GB = 1024**3
ROOT = Path(__file__).resolve().parent.parent


class ArcBesideRadeonIgpu(unittest.TestCase):
    """Ryzen 7000/9000 desktops keep their Radeon iGPU on by default. With an Arc B580 in the
    slot, the Arc must be picked and the Intel build downloaded, not the AMD (ROCm) one."""

    def test_pick_adapter_takes_the_arc_over_a_radeon_igpu(self):
        found = [
            hwmod.Adapter("amd", "AMD Radeon(TM) Graphics", 512 * 1024**2, "dxgi"),  # Raphael iGPU
            hwmod.Adapter("intel", "Intel(R) Arc(TM) B580 Graphics", 12 * GB, "dxgi"),
        ]
        self.assertEqual(hwmod.pick_adapter(found).name, "Intel(R) Arc(TM) B580 Graphics")

    def test_preflight_downloads_the_intel_build(self):
        import json

        m = comfy_setup.parse_manifest(json.loads((ROOT / "models" / "comfy_setup.json").read_text("utf-8")))
        hw = Hardware(
            gpu_name="Intel(R) Arc(TM) B580 Graphics", vram_bytes=12 * GB, gpu_source="dxgi", gpu_vendor="intel"
        )
        pf = comfy_setup.preflight(
            m,
            hw,
            ["AMD Radeon(TM) Graphics", "Intel(R) Arc(TM) B580 Graphics"],
            50 * GB,
            Path("C:/x"),
            8188,
            False,
            platform="win32",
        )
        self.assertFalse(pf.amd_build)
        self.assertTrue(pf.intel_build)


class NativeKernelSkip(unittest.TestCase):
    def test_igpu_beside_an_rx_6700_does_not_bring_the_override_back(self):
        # Ryzen 7000 (gfx1036 iGPU) + RX 6700 XT (gfx1031) with AMD's torch[device-gfx1031], as
        # docs/install.md suggests: 10.3.0 would ask for gfx1030 kernels that are not installed.
        self.assertEqual(hwmod.rocm_env([100301, 100306], {}, "linux", native={"gfx1031"}), {})

    def test_left_over_device_package_after_a_pytorch_org_reinstall_keeps_the_override(self):
        # The user tried AMD's torch, then reinstalled torch from pytorch.org (rocm7.2). pip
        # replaces torch but leaves amd_torch_device_gfx1031 behind. pytorch.org's torch has no
        # gfx1031 code, so the override is still needed.
        with tempfile.TemporaryDirectory() as d:
            venv = Path(d) / "venv"
            site = venv / "lib" / "python3.12" / "site-packages"
            site.mkdir(parents=True)
            (site / "amd_torch_device_gfx1031-2.11.0+rocm10.0.0.dist-info").mkdir()
            (site / "torch-2.10.0+rocm7.2.dist-info").mkdir()
            native = hwmod.rocm_native_kernels(venv / "bin" / "python")
            self.assertEqual(
                hwmod.rocm_env([100301], {}, "linux", native=native), {"HSA_OVERRIDE_GFX_VERSION": "10.3.0"}
            )


if __name__ == "__main__":
    unittest.main()
