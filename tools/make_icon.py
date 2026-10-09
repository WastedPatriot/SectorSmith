"""Draws the SectorSmith product mark (indigo tile, a disk with one sector struck out, resting on an anvil) as PNGs
and a multi-size Windows .ico. The geometry lives in sectorsmith/ui/mark.py, which the in-app mark draws too.

Usage: python tools/make_icon.py      (needs Pillow; writes sectorsmith/ui/assets/mark*.png and mark.ico)
"""
from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sectorsmith.ui import mark as M  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "sectorsmith", "ui", "assets")
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
SS = 8  # supersampling: draw big, scale down with a good filter for clean edges


def _hex(c: str):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def draw(size: int) -> Image.Image:
    n = size * SS
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    # tile: vertical indigo gradient under a rounded-square mask
    top, bottom = _hex(M.INDIGO), _hex(M.INDIGO_DARK)
    grad = Image.new("RGBA", (1, n))
    for y in range(n):
        t = y / max(1, n - 1)
        grad.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    grad = grad.resize((n, n))
    mask = Image.new("L", (n, n), 0)
    pad = 0 if size < 30 else round(n * .02)  # a hair of margin at large sizes, like other Windows icons
    ImageDraw.Draw(mask).rounded_rectangle((pad, pad, n - 1 - pad, n - 1 - pad), radius=round(n * M.CORNER),
                                           fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)
    disk = M.DISK_SMALL if M.simple(size) else M.DISK
    if not M.simple(size):
        d.polygon([(x * n, y * n) for x, y in M.ANVIL], fill=white)
    cx, cy, r = disk["cx"] * n, disk["cy"] * n, disk["r"] * n
    box = (cx - r, cy - r, cx + r, cy + r)
    d.ellipse(box, outline=white, width=round(disk["ring"] * n))
    start, extent = disk["sector"]
    # PIL angles run clockwise from 3 o'clock; the mark's sector runs clockwise from 12 to 3 o'clock
    a0 = -start
    d.pieslice(box, a0, a0 - extent, fill=white)
    h = disk["hole"] * n
    d.ellipse((cx - h, cy - h, cx + h, cy + h), fill=grad.getpixel((0, round(cy))))  # the tile colour there
    return img.resize((size, size), Image.LANCZOS)


def build():
    os.makedirs(OUT, exist_ok=True)
    imgs = {s: draw(s) for s in SIZES}
    for s, im in imgs.items():
        im.save(os.path.join(OUT, f"mark_{s}.png"), optimize=True)
    imgs[256].save(os.path.join(OUT, "mark.png"), optimize=True)
    ico = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    imgs[256].save(os.path.join(OUT, "mark.ico"), sizes=[(s, s) for s in ico],
                   append_images=[imgs[s] for s in ico if s != 256])
    print("product mark written to", os.path.abspath(OUT), SIZES)


if __name__ == "__main__":
    build()
