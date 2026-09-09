"""Draw brand/icon.png from the same glyph the sidebar uses (mdi:pin).

Kept as a script so the icon can be re-rendered if the colours or padding change.
Run it from the repository root:

    python scripts/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024  # drawn large and downscaled, which is what keeps the edges clean
BLUE = (3, 169, 244, 255)
WHITE = (255, 255, 255, 255)
GLYPH_SCALE = 0.62  # how much of the tile the pin occupies
OUT = Path(__file__).resolve().parent.parent / "brand"

# mdi:pin, viewBox 0 0 24 24. Every segment is a straight line, so the path is a
# plain polygon: https://raw.githubusercontent.com/Templarian/MaterialDesign/master/svg/pin.svg
PIN_PATH = "M16,12V4H17V2H7V4H8V12L6,14V16H11.2V22H12.8V16H18V14L16,12Z"


def parse_path(path: str) -> list[tuple[float, float]]:
    """Read an SVG path made only of absolute M, H, V, L and Z commands."""
    points: list[tuple[float, float]] = []
    x = y = 0.0
    index = 0
    while index < len(path):
        command = path[index]
        index += 1
        if command == "Z":
            break
        end = index
        while end < len(path) and path[end] not in "MHVLZ":
            end += 1
        numbers = [float(n) for n in path[index:end].replace(",", " ").split()]
        index = end
        if command == "M" or command == "L":
            x, y = numbers[0], numbers[1]
        elif command == "H":
            x = numbers[0]
        elif command == "V":
            y = numbers[0]
        points.append((x, y))
    return points


def render() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=int(SIZE * 0.22), fill=BLUE)

    scale = SIZE * GLYPH_SCALE / 24
    offset = (SIZE - 24 * scale) / 2
    draw.polygon([(offset + px * scale, offset + py * scale) for px, py in parse_path(PIN_PATH)], fill=WHITE)
    return img


if __name__ == "__main__":
    image = render()
    OUT.mkdir(exist_ok=True)
    for pixels, name in ((256, "icon.png"), (512, "icon@2x.png")):
        image.resize((pixels, pixels), Image.LANCZOS).save(OUT / name)
        print(f"wrote {OUT / name}")
