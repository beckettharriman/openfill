# SPDX-License-Identifier: Apache-2.0
import struct
import unittest
import zlib

from openfill import png


def _decode_idat(data: bytes) -> bytes:
    """Concatenate and inflate the IDAT chunks (tests only)."""
    pos = 8
    idat = b""
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        crc = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])[0]
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF, "crc mismatch"
        if kind == b"IDAT":
            idat += body
        pos += 12 + length
    return zlib.decompress(idat)


class PngTests(unittest.TestCase):
    def test_roundtrip_rgba(self):
        pixels = bytes(range(256)) * 4  # 16x16 RGBA = 1024 bytes
        data = png.encode(16, 16, pixels, 4)
        self.assertEqual(png.read_header(data), (16, 16, 8, 6))
        raw = _decode_idat(data)
        rows = [raw[i * 65 : (i + 1) * 65] for i in range(16)]
        self.assertTrue(all(r[0] == 0 for r in rows))
        self.assertEqual(b"".join(r[1:] for r in rows), pixels)

    def test_gray_and_rgb_color_types(self):
        self.assertEqual(png.read_header(png.encode(2, 2, bytes(4), 1))[3], 0)
        self.assertEqual(png.read_header(png.encode(2, 2, bytes(12), 3))[3], 2)
        self.assertEqual(png.read_header(png.encode(2, 2, bytes(8), 2))[3], 4)

    def test_rejects_bad_input(self):
        with self.assertRaises(png.PngError):
            png.encode(2, 2, bytes(3), 1)
        with self.assertRaises(png.PngError):
            png.encode(2, 2, bytes(20), 5)
        with self.assertRaises(png.PngError):
            png.read_header(b"not a png")


if __name__ == "__main__":
    unittest.main()
