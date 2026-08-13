from __future__ import annotations

import hashlib
from pathlib import Path
import struct
import unittest


ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "docs" / "assets" / "brand"
ORIGINAL_SHA256 = "24e302971f8c47a716de7b4c541866d8ea960f295beb6882b5377ada9266ac27"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_header(path: Path) -> tuple[int, int, int, int]:
    with path.open("rb") as stream:
        signature = stream.read(8)
        length = struct.unpack(">I", stream.read(4))[0]
        chunk_type = stream.read(4)
        payload = stream.read(length)
    if signature != PNG_SIGNATURE or chunk_type != b"IHDR" or length != 13:
        raise AssertionError(f"Invalid PNG header: {path}")
    width, height, bit_depth, color_type, _, _, _ = struct.unpack(">IIBBBBB", payload)
    return width, height, bit_depth, color_type


class BrandAssetTests(unittest.TestCase):
    def test_canonical_source_is_immutable_rgba_png(self) -> None:
        original = BRAND / "cw-logo-original.png"
        self.assertTrue(original.is_file())
        self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), ORIGINAL_SHA256)
        self.assertEqual(png_header(original), (1254, 1254, 8, 6))

    def test_required_brand_derivatives_exist_with_alpha(self) -> None:
        expected_sizes = {
            "cw-mark.png": None,
            "cw-logo-dark.png": None,
            "cw-logo-light.png": None,
            "cw-mark-32.png": (32, 32),
            "cw-mark-64.png": (64, 64),
        }
        for name, expected_size in expected_sizes.items():
            with self.subTest(asset=name):
                path = BRAND / name
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 0)
                width, height, bit_depth, color_type = png_header(path)
                self.assertEqual(bit_depth, 8)
                self.assertEqual(color_type, 6, "asset must be RGBA")
                self.assertGreater(width, 0)
                self.assertGreater(height, 0)
                if expected_size is not None:
                    self.assertEqual((width, height), expected_size)

    def test_brand_assets_are_not_python_runtime_package_data(self) -> None:
        self.assertFalse((ROOT / "cw" / "brand").exists())
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotIn("docs/assets/brand", pyproject)
        self.assertNotIn("Pillow", pyproject)


if __name__ == "__main__":
    unittest.main()
