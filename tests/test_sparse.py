# SPDX-License-Identifier: Apache-2.0
"""The fake model files the tests lay out cost no disk and no time on every system (on
Windows, plain truncate wrote every byte: the core tests took an hour there)."""

import os
import tempfile
import time
import unittest
from pathlib import Path

from tests.sparse import grow


class SparseTests(unittest.TestCase):
    def test_a_catalog_sized_file_is_instant_and_reads_as_zeros(self):
        size = 6 << 30  # about RealVisXL's size
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.safetensors"
            path.write_bytes(b"head")
            start = time.monotonic()
            grow(path, size)
            self.assertLess(time.monotonic() - start, 10)
            self.assertEqual(path.stat().st_size, size)
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(4), b"head")  # what was written stays
                fh.seek(size - 4096)
                self.assertEqual(fh.read(), bytes(4096))
            grow(path, 10)  # shrinks too
            self.assertEqual(path.read_bytes(), b"head" + bytes(6))
            if hasattr(os.stat_result, "st_blocks"):
                self.assertLess(grow(Path(tmp) / "new", size).stat().st_blocks * 512, 1 << 20)
