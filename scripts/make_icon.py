"""Draw brand/icon.png: someone putting a stick through their own front wheel.

The icon is code rather than a drawing so it can be adjusted and re-rendered. Run it
from the repository root:

    python scripts/make_icon.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024  # drawn large and downscaled, which is what keeps the edges clean
BLUE = (3, 169, 244, 255)
WHITE = (255, 255, 255, 255)
OUT = Path(__file__).resolve().parent.parent / "brand"


def _cap_line(draw, start, end, width, fill=WHITE):
    """A line with round ends, which PIL does not offer directly."""
    draw.line([start, end], fill=fill, width=int(width))
    for point in (start, end):
        draw.ellipse(
            [point[0] - width / 2, point[1] - width / 2, point[0] + width / 2, point[1] + width / 2],
            fill=fill,
        )


def _halo_line(draw, start, end, width, halo=28):
    """A line that knocks a gap out of whatever it crosses, so it reads as being in front."""
    _cap_line(draw, start, end, width + halo * 2, BLUE)
    _cap_line(draw, start, end, width)


def _wheel(draw, centre, radius, rim=32, spokes=9, hub=38):
    cx, cy = centre
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], outline=WHITE, width=rim)
    for i in range(spokes):
        angle = math.pi * 2 * i / spokes + 0.35
        draw.line(
            [(cx, cy), (cx + radius * math.cos(angle), cy + radius * math.sin(angle))],
            fill=WHITE,
            width=13,
        )
    draw.ellipse([cx - hub, cy - hub, cx + hub, cy + hub], fill=WHITE)


def render() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=int(SIZE * 0.22), fill=BLUE)

    hub = (335, 665)
    _wheel(draw, hub, 240)
    _cap_line(draw, hub, (505, 445), 26)  # a fork, so the wheel belongs to a bicycle

    draw.ellipse([700, 165, 856, 321], fill=WHITE)  # head
    _cap_line(draw, (772, 315), (850, 545), 94)  # torso, hunched forward
    _cap_line(draw, (850, 545), (745, 775), 60)  # leg
    _cap_line(draw, (762, 372), (648, 478), 56)  # upper arm
    _cap_line(draw, (648, 478), (548, 588), 46)  # forearm

    _halo_line(draw, (655, 512), (300, 706), 34)  # the stick, held mid-length
    draw.ellipse([492, 532, 604, 644], fill=WHITE)  # hand closed over it
    return img


if __name__ == "__main__":
    image = render()
    OUT.mkdir(exist_ok=True)
    for pixels, name in ((256, "icon.png"), (512, "icon@2x.png")):
        image.resize((pixels, pixels), Image.LANCZOS).save(OUT / name)
        print(f"wrote {OUT / name}")
