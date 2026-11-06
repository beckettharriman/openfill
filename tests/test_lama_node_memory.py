# SPDX-License-Identifier: Apache-2.0
"""OpenFill's LaMa node sizes its pieces to the free graphics memory on NVIDIA, AMD (ROCm)
and Intel (XPU), with fake torch modules (no torch needed)."""

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("ofnodes_mem", ROOT / "gimp-openfill/openfill/comfy_nodes/__init__.py")
nodes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nodes)


class FakeAcc:
    def __init__(self, peak=3 * 1024**2, with_stats=True):
        self.calls = []
        if with_stats:
            self.reset_peak_memory_stats = lambda d: self.calls.append(("reset", d))
            self.max_memory_allocated = lambda d: peak


class LamaMemoryTests(unittest.TestCase):
    def test_cuda_and_xpu_get_the_graphics_card_module(self):
        torch = SimpleNamespace(cuda="CUDA", xpu="XPU")
        self.assertEqual(nodes._accelerator(torch, SimpleNamespace(type="cuda")), "CUDA")  # NVIDIA, and AMD on ROCm
        self.assertEqual(nodes._accelerator(torch, SimpleNamespace(type="xpu")), "XPU")  # Intel
        self.assertIsNone(nodes._accelerator(torch, SimpleNamespace(type="cpu")))
        self.assertIsNone(nodes._accelerator(torch, SimpleNamespace(type="mps")))
        self.assertIsNone(nodes._accelerator(SimpleNamespace(cuda="CUDA"), SimpleNamespace(type="xpu")))  # no xpu

    def test_memory_budget(self):
        self.assertEqual(nodes._memory_budget(nodes.RESERVE + 700 * 1000), 1000)
        self.assertEqual(nodes._memory_budget(0), 0)
        # An 8 GB Arc with 6 GB free gets a budget well below the processor's fixed 64 MP.
        self.assertLess(nodes._memory_budget(6 * 1024**3), nodes.CPU_BUDGET)

    def test_stats_are_optional(self):
        acc = FakeAcc(peak=42)
        self.assertEqual(nodes._stat(acc, "max_memory_allocated", "xpu:0"), 42)
        nodes._stat(acc, "reset_peak_memory_stats", "xpu:0")
        self.assertEqual(acc.calls, [("reset", "xpu:0")])
        self.assertEqual(nodes._stat(FakeAcc(with_stats=False), "max_memory_allocated", "xpu:0"), 0)
        self.assertEqual(nodes._stat(None, "max_memory_allocated", "cpu"), 0)

        def broken(_d):
            raise RuntimeError("not supported on this device")

        self.assertEqual(nodes._stat(SimpleNamespace(max_memory_allocated=broken), "max_memory_allocated", 0), 0)


if __name__ == "__main__":
    unittest.main()
