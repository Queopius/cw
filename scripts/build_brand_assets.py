#!/usr/bin/env python3
"""Build CW brand derivatives without altering the canonical source image.

Pillow is a development-only requirement for this script. It is deliberately
not part of the CW runtime dependency set.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys

try:
    from PIL import Image
except ImportError as exc:  # pragma: no cover - developer environment guard
    raise SystemExit(
        "Pillow is required to build brand assets. Install it with "
        "`python -m pip install Pillow`."
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
BRAND_DIR = ROOT / "docs" / "assets" / "brand"
ORIGINAL = BRAND_DIR / "cw-logo-original.png"
CANONICAL_SHA256 = "24e302971f8c47a716de7b4c541866d8ea960f295beb6882b5377ada9266ac27"
EXPECTED_SOURCE_SIZE = (1254, 1254)
MARK_NAME = "cw-mark.png"
VARIANT_NAMES = ("cw-logo-dark.png", "cw-logo-light.png")
ICON_SIZES = (32, 64)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_source() -> "Image.Image":
    if not ORIGINAL.is_file():
        raise SystemExit(f"Canonical source is missing: {ORIGINAL}")
    actual_hash = sha256(ORIGINAL)
    if actual_hash != CANONICAL_SHA256:
        raise SystemExit(
            "Canonical source hash mismatch; refusing to generate derivatives.\n"
            f"Expected: {CANONICAL_SHA256}\nActual:   {actual_hash}"
        )
    source = Image.open(ORIGINAL)
    source.load()
    if source.mode != "RGBA" or source.size != EXPECTED_SOURCE_SIZE:
        raise SystemExit(
            f"Canonical source must be RGBA {EXPECTED_SOURCE_SIZE}, got "
            f"{source.mode} {source.size}"
        )
    if source.getchannel("A").getextrema() != (0, 255):
        raise SystemExit("Canonical source must contain transparent and opaque pixels")
    return source


def canonical_crop(source: "Image.Image") -> "Image.Image":
    """Crop around the visible mark while retaining transparent safe space.

    The owner-provided canvas contains isolated alpha=1 export noise far from
    the visible monogram. The crop bounds use alpha>1 only to locate the mark;
    pixels inside the resulting rectangle are copied unchanged.
    """
    alpha = source.getchannel("A")
    visible = alpha.point(tuple(255 if value > 1 else 0 for value in range(256)))
    bbox = visible.getbbox()
    if bbox is None:
        raise SystemExit("Canonical source does not contain a visible mark")
    padding = round(max(bbox[2] - bbox[0], bbox[3] - bbox[1]) * 0.055)
    crop_box = (
        max(0, bbox[0] - padding),
        max(0, bbox[1] - padding),
        min(source.width, bbox[2] + padding),
        min(source.height, bbox[3] + padding),
    )
    return source.crop(crop_box)


def adjust_luminance(mark: "Image.Image", *, mode: str) -> "Image.Image":
    """Adjust RGB luminance only; alpha and therefore geometry stay exact."""
    red, green, blue, alpha = mark.split()
    if mode == "dark-background":
        table = tuple(round(value + (255 - value) * 0.08) for value in range(256))
    elif mode == "light-background":
        table = tuple(round(value * 0.82) for value in range(256))
    else:  # pragma: no cover - internal programming guard
        raise ValueError(f"Unknown luminance mode: {mode}")
    return Image.merge("RGBA", (red.point(table), green.point(table), blue.point(table), alpha))


def icon_from_mark(mark: "Image.Image", size: int) -> "Image.Image":
    padding = max(2, round(size * 0.08))
    available = size - (padding * 2)
    scale = min(available / mark.width, available / mark.height)
    dimensions = (
        max(1, round(mark.width * scale)),
        max(1, round(mark.height * scale)),
    )
    resampling = getattr(Image, "Resampling", None)
    lanczos = resampling.LANCZOS if resampling is not None else Image.LANCZOS
    resized = mark.resize(dimensions, lanczos)
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    position = ((size - resized.width) // 2, (size - resized.height) // 2)
    icon.alpha_composite(resized, position)
    return icon


def save_png(image: "Image.Image", destination: Path) -> None:
    image.save(destination, format="PNG", optimize=False, compress_level=9)


def build() -> None:
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    source = load_source()
    mark = canonical_crop(source)
    save_png(mark, BRAND_DIR / MARK_NAME)
    save_png(
        adjust_luminance(mark, mode="dark-background"),
        BRAND_DIR / "cw-logo-dark.png",
    )
    save_png(
        adjust_luminance(mark, mode="light-background"),
        BRAND_DIR / "cw-logo-light.png",
    )
    for size in ICON_SIZES:
        save_png(icon_from_mark(mark, size), BRAND_DIR / f"cw-mark-{size}.png")
    verify()


def verify() -> None:
    source = load_source()
    del source
    required = [MARK_NAME, *VARIANT_NAMES, *(f"cw-mark-{size}.png" for size in ICON_SIZES)]
    images: dict[str, Image.Image] = {}
    for name in required:
        path = BRAND_DIR / name
        if not path.is_file() or path.stat().st_size == 0:
            raise SystemExit(f"Brand derivative is missing or empty: {path}")
        image = Image.open(path)
        image.load()
        if image.mode != "RGBA" or image.getchannel("A").getextrema()[0] != 0:
            raise SystemExit(f"Brand derivative must preserve transparency: {path}")
        images[name] = image
    mark = images[MARK_NAME]
    if mark.width < 512 or mark.height < 256:
        raise SystemExit(f"Canonical mark resolution is too small: {mark.size}")
    mark_alpha = mark.getchannel("A").tobytes()
    for name in VARIANT_NAMES:
        if images[name].size != mark.size:
            raise SystemExit(f"Variant dimensions differ from canonical mark: {name}")
        if images[name].getchannel("A").tobytes() != mark_alpha:
            raise SystemExit(f"Variant geometry/alpha differs from canonical mark: {name}")
    for size in ICON_SIZES:
        if images[f"cw-mark-{size}.png"].size != (size, size):
            raise SystemExit(f"Icon has incorrect dimensions: cw-mark-{size}.png")
    for name in [ORIGINAL.name, *required]:
        path = BRAND_DIR / name
        with Image.open(path) as image:
            print(f"{name:24} {image.width:4}x{image.height:<4} {sha256(path)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate existing assets without regenerating them",
    )
    args = parser.parse_args()
    verify() if args.check else build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
