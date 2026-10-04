"""Generate the BD2HEVC application icon as PNG and multi-size ICO."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = ["seguisb.ttf" if bold else "segoeui.ttf", "arialbd.ttf" if bold else "arial.ttf"]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def build(png: Path, ico: Path) -> None:
    size = 1024
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((74, 88, 850, 864), fill=(9, 25, 44, 72))
    for radius in range(378, 0, -1):
        ratio = radius / 378
        color = (
            int(34 + 20 * (1 - ratio)),
            int(92 + 70 * (1 - ratio)),
            int(150 + 75 * (1 - ratio)),
            255,
        )
        cx, cy = 448, 456
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=color)
    for radius, alpha in ((330, 42), (270, 34), (210, 28), (145, 26)):
        draw.ellipse((448 - radius, 456 - radius, 448 + radius, 456 + radius), outline=(220, 245, 255, alpha), width=8)
    draw.ellipse((374, 382, 522, 530), fill=(13, 41, 66, 255), outline=(186, 235, 248, 255), width=16)
    draw.ellipse((420, 428, 476, 484), fill=(228, 244, 249, 255))
    draw.text((210, 258), "BD", font=font(132, bold=True), fill=(238, 250, 255, 255), stroke_width=5, stroke_fill=(9, 55, 91, 210))

    # The converging chevrons communicate both conversion and compaction.
    arrow = [(626, 315), (850, 456), (626, 597), (626, 515), (505, 515), (505, 397), (626, 397)]
    draw.polygon(arrow, fill=(44, 205, 153, 255), outline=(5, 94, 79, 255))
    draw.line(arrow + [arrow[0]], fill=(230, 255, 248, 255), width=10, joint="curve")

    badge = (366, 724, 938, 942)
    draw.rounded_rectangle(badge, radius=76, fill=(9, 59, 80, 255), outline=(81, 232, 196, 255), width=14)
    draw.text((405, 746), "H.265", font=font(134, bold=True), fill=(244, 255, 252, 255))
    png.parent.mkdir(parents=True, exist_ok=True)
    image.save(png, optimize=True)
    image.save(ico, format="ICO", sizes=[(value, value) for value in SIZES])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("png", type=Path)
    parser.add_argument("ico", type=Path)
    args = parser.parse_args()
    build(args.png, args.ico)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
