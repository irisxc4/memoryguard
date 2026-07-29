"""Render the desktop icon from interactive.py's `.brand-orb` design."""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


SIZE = 512
ORB_BOX = (48, 48, 464, 464)
ORB_CENTER = (256, 256)
ORB_RADIUS = 208
ACCENT = (110, 231, 196, 255)
OUT = Path(__file__).resolve().parents[1] / "src" / "memoryguard" / "static"


def _mix(left: tuple[int, ...], right: tuple[int, ...], amount: float) -> tuple[int, ...]:
    return tuple(round(a + (b - a) * amount) for a, b in zip(left, right))


def _sample_gradient(position: float) -> tuple[int, int, int, int]:
    # Same stops as `.brand-orb` in interactive.py.
    stops = (
        (0.00, (228, 255, 247, 255)),
        (0.06, (228, 255, 247, 255)),
        (0.08, ACCENT),
        (0.19, ACCENT),
        (0.44, (22, 61, 51, 255)),
        (0.72, (7, 18, 15, 255)),
        (1.00, (7, 18, 15, 255)),
    )
    for (start, start_color), (end, end_color) in zip(stops, stops[1:]):
        if position <= end:
            amount = 0.0 if end == start else (position - start) / (end - start)
            return _mix(start_color, end_color, max(0.0, min(1.0, amount)))
    return stops[-1][1]


def _draw_fading_ray(image: Image.Image, angle_degrees: float, length: int) -> None:
    angle = math.radians(angle_degrees)
    start_x, start_y = ORB_CENTER
    ray = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(ray)
    for step in range(length, 0, -1):
        fade = step / length
        draw.line(
            (
                start_x,
                start_y,
                start_x + math.cos(angle) * step,
                start_y + math.sin(angle) * step,
            ),
            fill=(*ACCENT[:3], round(210 * fade)),
            width=10,
        )
    image.alpha_composite(ray)


def render() -> tuple[Path, Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(ORB_BOX, fill=(110, 231, 196, 100))
    image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(34)))

    gradient = Image.new("RGBA", image.size, (0, 0, 0, 0))
    pixels = gradient.load()
    gradient_center = (
        ORB_BOX[0] + (ORB_BOX[2] - ORB_BOX[0]) * 0.38,
        ORB_BOX[1] + (ORB_BOX[3] - ORB_BOX[1]) * 0.34,
    )
    radius = ORB_RADIUS * 1.45
    for y in range(ORB_BOX[1], ORB_BOX[3] + 1):
        for x in range(ORB_BOX[0], ORB_BOX[2] + 1):
            if math.dist((x, y), ORB_CENTER) <= ORB_RADIUS:
                pixels[x, y] = _sample_gradient(
                    math.dist((x, y), gradient_center) / radius,
                )
    image.alpha_composite(gradient)

    _draw_fading_ray(image, 35, 268)
    _draw_fading_ray(image, 150, 224)
    ImageDraw.Draw(image).ellipse(
        ORB_BOX,
        outline=(188, 255, 235, 220),
        width=8,
    )

    png_path = OUT / "memoryguard-icon.png"
    ico_path = OUT / "memoryguard-icon.ico"
    image.save(png_path, optimize=True)
    image.save(
        ico_path,
        format="ICO",
        sizes=((16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)),
    )
    return png_path, ico_path


if __name__ == "__main__":
    for path in render():
        print(path)
