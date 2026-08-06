"""Derive the packaged multi-size Windows icon from a high-resolution logo."""

from __future__ import annotations

import argparse
import math
import statistics
from pathlib import Path

from PIL import Image

ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
DEFAULT_PADDING_FRACTION = 0.06
BACKGROUND_TOLERANCE = 24


def _key_background(image: Image.Image, background: tuple[int, int, int, int]) -> Image.Image:
    keyed = Image.new("RGBA", image.size)
    output = []
    has_source_transparency = background[3] < 255
    for red, green, blue, alpha in image.get_flattened_data():
        if has_source_transparency:
            output.append((0, 0, 0, 0) if alpha == 0 else (red, green, blue, alpha))
            continue
        distance = max(
            abs(red - background[0]),
            abs(green - background[1]),
            abs(blue - background[2]),
        )
        if distance <= BACKGROUND_TOLERANCE:
            output.append((0, 0, 0, 0))
            continue
        if distance < 2 * BACKGROUND_TOLERANCE:
            alpha = round(alpha * (distance - BACKGROUND_TOLERANCE) / BACKGROUND_TOLERANCE)
        output.append((red, green, blue, alpha))
    keyed.putdata(output)
    return keyed


def _prepared_square(source: Path, *, padding_fraction: float) -> Image.Image:
    if not 0.0 <= padding_fraction < 0.25:
        raise ValueError("padding_fraction must be at least 0 and less than 0.25")
    with Image.open(source) as opened:
        image = opened.convert("RGBA")
    patch = max(1, min(image.size) // 16)
    samples = []
    for left, top in (
        (0, 0),
        (image.width - patch, 0),
        (0, image.height - patch),
        (image.width - patch, image.height - patch),
    ):
        samples.extend(image.crop((left, top, left + patch, top + patch)).getdata())
    background = tuple(
        round(statistics.median(pixel[index] for pixel in samples)) for index in range(4)
    )
    image = _key_background(image, background)
    content_box = image.getchannel("A").getbbox()
    if content_box is None:
        raise ValueError("source image contains no logo distinct from its corner background")

    content = image.crop(content_box)
    visible_box = content.getchannel("A").getbbox()
    if visible_box is None:
        raise ValueError("source image contains no visible logo")
    content = content.crop(visible_box)

    side = math.ceil(max(content.size) / (1.0 - 2.0 * padding_fraction))
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    offset = ((side - content.width) // 2, (side - content.height) // 2)
    square.alpha_composite(content, offset)
    return square


def build_icon(
    source: Path,
    output: Path,
    *,
    padding_fraction: float = DEFAULT_PADDING_FRACTION,
) -> None:
    """Write one PNG-compressed ICO containing every supported Windows size."""
    square = _prepared_square(source, padding_fraction=padding_fraction)
    output.parent.mkdir(parents=True, exist_ok=True)
    square.save(
        output,
        format="ICO",
        sizes=[(size, size) for size in ICON_SIZES],
        bitmap_format="png",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--padding-fraction", type=float, default=DEFAULT_PADDING_FRACTION)
    args = parser.parse_args()
    build_icon(args.source, args.output, padding_fraction=args.padding_fraction)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
