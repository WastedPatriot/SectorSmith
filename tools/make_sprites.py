"""Generates Mossbit's pixel-art sprite sheets and the app icon.

Pixel art is built on a 48x46 grid: shapes are rasterised, shaded in tone bands from a
top-left light source, given a selective outline, and then hand-designed feature stamps
(eyes, mouth, broom, sparkles…) are placed on top. Output is nearest-neighbour scaled.

Usage: python tools/make_sprites.py      (needs Pillow; writes sectorsmith/ui/assets/)
"""
from __future__ import annotations

import json
import os

from PIL import Image

OUT = os.path.join(os.path.dirname(__file__), "..", "sectorsmith", "ui", "assets")
W, H = 56, 46

C = {
    "OD": (20, 40, 31, 255), "OL": (37, 88, 60, 255),
    "G0": (40, 116, 74, 255), "G1": (66, 170, 104, 255), "G2": (112, 208, 132, 255), "G3": (190, 244, 186, 255),
    "BE": (234, 251, 218, 255), "BS": (196, 234, 184, 255),
    "F0": (32, 96, 60, 255), "F1": (52, 140, 88, 255),
    "E": (21, 23, 33, 255), "W": (255, 255, 255, 255), "P": (255, 136, 160, 255), "P2": (255, 178, 194, 255),
    "M": (112, 30, 50, 255), "T": (255, 106, 138, 255),
    "WD": (150, 94, 52, 255), "WD2": (104, 62, 34, 255), "WL": (190, 128, 74, 255),
    "Y": (246, 204, 84, 255), "y": (198, 146, 44, 255), "BAND": (196, 70, 98, 255),
    "DU": (168, 166, 186, 200), "DU2": (210, 208, 222, 170),
    "SH": (0, 0, 0, 56), "SW": (124, 204, 255, 255), "SW2": (210, 240, 255, 255),
    "ST": (255, 222, 96, 255), "ST2": (255, 250, 220, 255), "WR": (255, 182, 54, 255),
}
FX = {"SH", "DU", "DU2", "ST", "ST2", "SW", "SW2", "WR"}  # effects: not part of the outlined silhouette


class Frame:
    def __init__(self):
        self.px: dict[tuple[int, int], str] = {}
        self.fx: dict[tuple[int, int], str] = {}

    def put(self, x, y, c):
        x, y = int(x), int(y)
        if 0 <= x < W and 0 <= y < H:
            (self.fx if c in FX else self.px)[(x, y)] = c

    def ellipse(self, cx, cy, rx, ry, shade=None, color=None):
        for y in range(int(cy - ry - 1), int(cy + ry + 2)):
            for x in range(int(cx - rx - 1), int(cx + rx + 2)):
                nx, ny = (x + .5 - cx) / rx, (y + .5 - cy) / ry
                r2 = nx * nx + ny * ny
                if r2 <= 1.0:
                    self.put(x, y, shade(nx, ny, r2) if shade else color)

    def stamp(self, x0, y0, rows, key):
        for j, row in enumerate(rows):
            for i, ch in enumerate(row):
                if ch != "." and ch != " ":
                    self.put(x0 + i, y0 + j, key[ch])

    def line(self, x0, y0, x1, y1, c, w=1):
        n = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
        for k in range(n + 1):
            t = k / max(1, n)
            x, y = round(x0 + (x1 - x0) * t), round(y0 + (y1 - y0) * t)
            for dx in range(w):
                self.put(x + dx, y, c(k / n) if callable(c) else c)

    def outline(self, cy_split):
        sil = set(self.px)
        out = {}
        for (x, y) in sil:
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                p = (x + dx, y + dy)
                if p not in sil and 0 <= p[0] < W and 0 <= p[1] < H:
                    out[p] = "OL" if p[1] < cy_split and p[0] < 30 else "OD"
        self.px.update(out)

    def image(self, scale):
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        pix = im.load()
        for (x, y), c in self.fx.items():
            if C[c][3] < 255:
                pix[x, y] = C[c]
        for (x, y), c in self.px.items():
            pix[x, y] = C[c]
        for (x, y), c in self.fx.items():
            if C[c][3] == 255:
                pix[x, y] = C[c]
        return im.resize((W * scale, H * scale), Image.NEAREST)


def body_shade(nx, ny, r2):
    d = nx * -.6 + ny * -.8
    if r2 > .7 and d < -.1:
        return "G0"
    if d > .6 and .3 < r2 < .56:
        return "G3"
    if d > .18:
        return "G2"
    return "G1"


def ear_shade(nx, ny, r2):
    d = nx * -.6 + ny * -.8
    return "G2" if d > .35 else ("G0" if d < -.35 else "G1")


def foot_shade(nx, ny, r2):
    return "F1" if ny < -.1 and nx < .4 else "F0"


EYES = {
    "open": [".EEE.", "EWWEE", "EWEEE", "EEEEE", "EEEWE", ".EEE."],
    "blink": [".....", ".....", ".....", "EEEEE", ".....", "....."],
    "happy": [".....", ".EEE.", "E...E", ".....", ".....", "....."],
    "sad": [".....", ".....", "EEEEE", "EWEEE", "EEEWE", ".EEE."],
    "wide": [".EEE.", "EWWWE", "EWWEE", "EEEEE", "EEEWE", ".EEE."],
    "focus": [".....", ".....", "EEEEE", "EEEEE", "EWEEE", ".EEE."],
}
MOUTHS = {
    "smile": ["E..E", ".EE."],
    "open": ["MMMM", "MTTM", ".MM."],
    "frown": [".EE.", "E..E"],
    "o": [".M.", "M.M", ".M."],
    "flat": [".EE."],
}
STAR = ["..S..", "..S..", "SSWSS", "..S..", "..S.."]
STAR_SMALL = [".S.", "SWS", ".S."]
BRISTLES = [".BBBBB.", ".YYYYY.", "YYyYyYY", "YyYYYyY", "yYyYyYy"]


def mossbit(dy=0, sx=1.0, sy=1.0, eyes="open", mouth="smile", sway=0, feet=(0, 0), arms="down", broom=None,
            dust=(), sparkles=(), sweat=None, warn=None, droop=False, shadow=1.0):
    f = Frame()
    cx = 21
    ground = 41
    ry = 12 * sy
    cy = ground - 3 - ry - 1 + dy
    rx = 13 * sx
    # ground shadow
    sw = 11 * shadow
    for x in range(int(cx - sw), int(cx + sw) + 1):
        f.put(x, ground + 1, "SH")
        if abs(x - cx) < sw - 3:
            f.put(x, ground + 2, "SH")
    # feet
    for i, side in enumerate((-1, 1)):
        f.ellipse(cx + side * 5.5, ground - 1 - feet[i] + (dy if dy < 0 else 0), 3.7, 2.4, shade=foot_shade)
    # back arm
    if arms == "up":
        f.ellipse(cx - 13, cy - 6, 2.0, 2.0, shade=lambda *a: "G1")
    else:
        f.ellipse(cx - 12.5, cy + 4, 2.0, 2.0, shade=lambda *a: "G0")
    # ears
    for side in (-1, 1):
        ex, ey = cx + side * 9, cy - ry + 1.5
        f.ellipse(ex, ey, 3.6, 3.6, shade=ear_shade)
        f.ellipse(ex + .3, ey + .3, 1.6, 1.6, color="BS")
    # body
    f.ellipse(cx, cy, rx, ry, shade=body_shade)
    # belly
    f.ellipse(cx + 1, cy + 5 * sy, 7.8 * sx, 5.8 * sy,
              shade=lambda nx, ny, r2: "BS" if (nx * .6 + ny * .8) > .45 and r2 > .45 else "BE")
    # sprout
    top = int(cy - ry)
    sx0 = cx + 1
    if droop:
        pts = [(sx0, top), (sx0 + 1, top - 1), (sx0 + 2, top - 2), (sx0 + 3, top - 2), (sx0 + 4, top - 1)]
        for p in pts:
            f.put(*p, "G0")
        f.stamp(sx0 + 4, top - 1, ["LL.", "LLL", ".L."], {"L": "G2"})
    else:
        pts = [(sx0, top), (sx0, top - 1), (sx0 + (1 if sway > 0 else 0), top - 2),
               (sx0 + sway, top - 3)]
        for p in pts:
            f.put(*p, "G0")
        lx, ly = sx0 + sway, top - 6
        f.stamp(lx - 1, ly, [".LLH", "LLLL", "VLL.", "V..."], {"L": "G2", "H": "G3", "V": "G0"})
    # face
    ex = cx + 1
    eye_y = int(cy - 5)
    for side in (-1, 1):
        f.stamp(ex + (-7 if side < 0 else 3), eye_y, EYES[eyes], {"E": "E", "W": "W"})
    if eyes == "sad":
        f.stamp(ex - 7, eye_y, ["...EE", "EEE.."], {"E": "G0"})
        f.stamp(ex + 3, eye_y, ["EE...", "..EEE"], {"E": "G0"})
    for bx in (ex - 10, ex + 9):
        f.stamp(bx, eye_y + 6, ["PQ"], {"P": "P", "Q": "P2"})
    m = MOUTHS[mouth]
    f.stamp(ex - len(m[0]) // 2, eye_y + 7, m, {"E": "E", "M": "M", "T": "T"})
    # front arm / broom
    if broom is not None:
        swing = broom
        hx, hy = cx + 11, int(cy + 2)
        tipx, tipy = cx + 21 + swing, ground - 4
        f.line(hx, hy - 8, tipx, tipy, lambda t: "WL" if t < .25 else "WD")
        f.line(hx + 1, hy - 8, tipx + 1, tipy, lambda t: "WD2")
        f.stamp(tipx - 3, tipy, BRISTLES, {"B": "BAND", "Y": "Y", "y": "y"})
        f.ellipse(hx, hy, 2.3, 2.3, shade=lambda *a: "G1")
    elif arms == "up":
        f.ellipse(cx + 13, cy - 6, 2.0, 2.0, shade=lambda *a: "G1")
    else:
        f.ellipse(cx + 12.5, cy + 4, 2.0, 2.0, shade=lambda *a: "G1")
    f.outline(cy_split=int(cy))
    # effects
    for (x, y, big) in dust:
        f.put(x, y, "DU")
        if big:
            f.put(x + 1, y, "DU2")
            f.put(x, y - 1, "DU2")
    for (x, y, big) in sparkles:
        f.stamp(x, y, STAR if big else STAR_SMALL, {"S": "ST", "W": "ST2"})
    if sweat is not None:
        f.stamp(cx - 15, int(cy - 8 + sweat), [".A", "AB", "AA"], {"A": "SW", "B": "SW2"})
    if warn is not None:
        x0, y0 = cx + 15, int(cy - ry - 4 + warn)
        f.stamp(x0, y0, ["RR", "RR", "RR", "RR", "..", "RR"], {"R": "WR"})
    return f


def build():
    os.makedirs(OUT, exist_ok=True)
    anims = {}

    # idle: gentle breathing, one blink, leaf sway
    idle = []
    breath = [0, 0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    for i in range(16):
        b = breath[i]
        idle.append(mossbit(dy=b * .5, sy=1 - .035 * b, sx=1 + .02 * b, sway=[0, 0, 1, 1, 1, 0, 0, -1][i % 8],
                            eyes="blink" if i == 12 else "open"))
    anims["idle"] = (idle, 7)

    # sweep: walking + broom swing + dust kicked forward
    sweep = []
    swings = [-2, 0, 2, 3, 1, -1]
    for i in range(6):
        tipx = 21 + 21 + swings[i]
        dust = []
        if swings[i] >= 0:
            k = swings[i] + 2
            dust = [(tipx + 4 + k, 40 - k // 2, True), (tipx + 7 + k, 38 - k // 2, False),
                    (tipx + 5 + 2 * k, 36 - k, False), (tipx + 9 + k, 41, True)]
        sweep.append(mossbit(dy=-1 if i in (1, 4) else 0, eyes="focus", mouth="flat", broom=swings[i],
                             feet=(2, 0) if i < 3 else (0, 2), dust=[d for d in dust if d[0] < W - 1],
                             sway=[1, 0, -1][i % 3]))
    anims["sweep"] = (sweep, 12)

    # happy: hop with sparkles
    happy = []
    jump = [0, -2, -4, -6, -7, -6, -4, -2, 0, 0]
    stars = [[], [(4, 6, False)], [(3, 4, True), (38, 8, False)], [(2, 3, True), (39, 5, True)],
             [(5, 1, False), (40, 4, True), (36, 14, False)], [(41, 2, False), (6, 10, True)],
             [(4, 12, False), (40, 10, False)], [(38, 14, False)], [], []]
    for i in range(10):
        land = i in (0, 9)
        happy.append(mossbit(dy=jump[i], sy=.9 if land else 1.0, sx=1.07 if land else 1.0, eyes="happy",
                             mouth="open", arms="up" if 1 <= i <= 7 else "down", sparkles=stars[i],
                             shadow=1 + jump[i] / 14))
    anims["happy"] = (happy, 12)

    # walk: bouncy stroll for roaming around
    walk = []
    for i in range(6):
        walk.append(mossbit(dy=[0, -1, -1, 0, -1, -1][i], feet=[(2, 0), (1, 0), (0, 0), (0, 2), (0, 1), (0, 0)][i],
                            sway=[1, 0, -1, -1, 0, 1][i], eyes="blink" if i == 5 and False else "open"))
    anims["walk"] = (walk, 10)

    # look: glance around while standing (eyes shift via sway + blink)
    # sad: droop + sweat
    sad = []
    for i in range(6):
        sad.append(mossbit(dy=.5 if i in (2, 3) else 0, eyes="sad", mouth="frown", droop=True, sweat=i * 1.2))
    anims["sad"] = (sad, 6)

    # warn: wide eyes + bouncing "!"
    warn = []
    for i in range(4):
        warn.append(mossbit(eyes="wide", mouth="o", warn=[0, -1, -2, -1][i], sway=[0, 1, 0, -1][i]))
    anims["warn"] = (warn, 6)

    meta = {"w": W, "h": H, "anims": {}}
    for name, (frames, fps) in anims.items():
        meta["anims"][name] = {"frames": len(frames), "fps": fps}
        for scale in (5, 4, 3):
            strip = Image.new("RGBA", (W * scale * len(frames), H * scale), (0, 0, 0, 0))
            flip = Image.new("RGBA", strip.size, (0, 0, 0, 0))
            for i, fr in enumerate(frames):
                im = fr.image(scale)
                strip.paste(im, (i * W * scale, 0))
                flip.paste(im.transpose(Image.FLIP_LEFT_RIGHT), (i * W * scale, 0))
            strip.save(os.path.join(OUT, f"mossbit_{name}_x{scale}.png"), optimize=True)
            flip.save(os.path.join(OUT, f"mossbit_{name}_x{scale}_L.png"), optimize=True)
    meta["body"] = [6, 36]  # x-range (sprite pixels) of the character's body, excluding the broom
    with open(os.path.join(OUT, "mossbit.json"), "w") as fh:
        json.dump(meta, fh, indent=1)

    # icon: idle frame cropped to the character
    base = idle[0].image(1)
    bbox = base.getbbox()
    crop = base.crop(bbox)
    side = max(crop.size) + 2
    sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    sq.paste(crop, ((side - crop.size[0]) // 2, (side - crop.size[1]) // 2))
    big = sq.resize((side * 8, side * 8), Image.NEAREST)
    big.save(os.path.join(OUT, "icon.png"))
    big.save(os.path.join(OUT, "icon.ico"), sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    # preview sheet for docs
    prev = Image.new("RGBA", (W * 5 * 5, H * 5), (0, 0, 0, 0))
    for i, name in enumerate(["idle", "sweep", "happy", "sad", "warn"]):
        prev.paste(anims[name][0][min(3, len(anims[name][0]) - 1)].image(5), (i * W * 5, 0))
    prev.save(os.path.join(OUT, "preview.png"))
    print("sprites written to", os.path.abspath(OUT), {k: v["frames"] for k, v in meta["anims"].items()})


if __name__ == "__main__":
    build()
