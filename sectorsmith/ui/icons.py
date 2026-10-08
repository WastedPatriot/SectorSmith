"""Outline icons drawn on a tk Canvas: one grid, one stroke weight, round caps. They recolour with the theme and
stay crisp at any DPI. Each glyph draws centred on (x, y) inside a box of size s."""
from __future__ import annotations

import math


def rounded_rect(cv, x0, y0, x1, y1, r, **kw):
    r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1, x0, y1,
           x0, y1 - r, x0, y0 + r, x0, y0]
    return cv.create_polygon(pts, smooth=True, **kw)


def _w(s):
    return max(1.25, s * .08)  # 1.6 px at a 20 px glyph


class _Pen:
    """Small helper so every glyph uses the same stroke, caps and joins."""

    def __init__(self, cv, x, y, s, col, bg):
        self.cv, self.x, self.y, self.s, self.col, self.bg = cv, x, y, s, col, bg
        self.w = _w(s)

    def p(self, *pts):
        return [c for a, b in zip(pts[::2], pts[1::2]) for c in (self.x + a * self.s, self.y + b * self.s)]

    def line(self, *pts, width=1.0, fill=None):
        self.cv.create_line(*self.p(*pts), fill=fill or self.col, width=self.w * width, capstyle="round",
                            joinstyle="round")

    def poly(self, *pts, fill=""):
        self.cv.create_polygon(*self.p(*pts), outline=self.col, width=self.w, fill=fill, joinstyle="round")

    def rect(self, x0, y0, x1, y1, r=.06, fill=""):
        rounded_rect(self.cv, self.x + x0 * self.s, self.y + y0 * self.s, self.x + x1 * self.s, self.y + y1 * self.s,
                     r * self.s, outline=self.col, width=self.w, fill=fill)

    def circle(self, cx, cy, r, fill="", outline=True):
        self.cv.create_oval(self.x + (cx - r) * self.s, self.y + (cy - r) * self.s, self.x + (cx + r) * self.s,
                            self.y + (cy + r) * self.s, outline=self.col if outline else "", width=self.w, fill=fill)

    def dot(self, cx, cy, r=.045):
        self.circle(cx, cy, r, fill=self.col, outline=False)

    def arc(self, cx, cy, r, start, extent):
        self.cv.create_arc(self.x + (cx - r) * self.s, self.y + (cy - r) * self.s, self.x + (cx + r) * self.s,
                           self.y + (cy + r) * self.s, start=start, extent=extent, style="arc", outline=self.col,
                           width=self.w)


def home(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.line(-.4, -.02, 0, -.38, .4, -.02)
    p.line(-.28, -.12, -.28, .38, .28, .38, .28, -.12)
    p.line(-.08, .38, -.08, .14, .08, .14, .08, .38)


def recover(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.line(-.06, .36, -.3, .36, -.3, -.4, .06, -.4, .22, -.24, .22, -.08)
    p.circle(.1, .14, .15, fill=bg)
    p.line(.21, .25, .37, .41)


def partition(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.arc(0, 0, .36, 90, 270)
    p.line(0, -.36, 0, 0, .36, 0)
    p.arc(.07, -.07, .32, 0, 90)
    p.line(.07, -.39, .07, -.07, .39, -.07)


def wipe(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    cx, cy, r, ri = -.06, .06, .34, .09
    pts = []
    for i in range(8):
        ang = math.pi / 4 * i - math.pi / 2
        rr = r if i % 2 == 0 else ri
        pts += [cx + rr * math.cos(ang), cy + rr * math.sin(ang)]
    p.poly(*pts)
    p.line(.3, -.4, .3, -.18)
    p.line(.19, -.29, .41, -.29)


def shred(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.line(-.24, -.08, -.24, -.4, .24, -.4, .24, -.08)
    p.line(-.4, -.06, .4, -.06)
    for i in range(4):
        xx = -.2 + i * .133
        p.line(xx, .08, xx, .38)


def health(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.line(-.42, .02, -.2, .02, -.1, -.26, .06, .3, .16, .02, .42, .02)


def clone(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.rect(-.38, -.38, .1, .1, .07)
    p.rect(-.1, -.1, .38, .38, .07, fill=bg)


def advanced(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    for i, pos in enumerate((-.26, 0, .26)):
        p.line(-.38, pos, .38, pos)
        kx = (.18, -.16, .06)[i]
        p.circle(kx, pos, .08, fill=bg)


def terminal(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.rect(-.42, -.34, .42, .34, .07)
    p.line(-.24, -.12, -.1, 0, -.24, .12)
    p.line(.02, .14, .22, .14)


def drive(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.rect(-.42, -.24, .42, .24, .07)
    p.line(-.26, .04, .02, .04)
    p.dot(.24, .04, .05)


def image(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.circle(0, 0, .38)
    p.circle(0, 0, .1)


def check(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.line(-.3, 0, -.08, .22, .32, -.22, width=1.25)


def warn(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.poly(0, -.38, .42, .34, -.42, .34)
    p.line(0, -.1, 0, .1)
    p.dot(0, .22, .04)


def pc(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.rect(-.42, -.34, .42, .16, .06)
    p.line(0, .16, 0, .32)
    p.line(-.2, .34, .2, .34)


def machines(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.rect(-.4, -.4, -.06, -.08, .06)
    p.rect(.06, .08, .4, .4, .06)
    p.line(-.23, -.08, -.23, .24, .06, .24)


def manage(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    for cx, cy in ((-.22, -.22), (.22, -.22), (-.22, .22), (.22, .22)):
        p.rect(cx - .15, cy - .15, cx + .15, cy + .15, .05)


def jobs(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.rect(-.3, -.4, .3, .4, .07)
    for yy in (-.18, 0, .18):
        p.line(-.14, yy, .14, yy)


def settings(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    pts = []
    for k in range(8):
        a = math.radians(k * 45)
        for da, r in ((-22.5, .29), (-11, .29), (-8, .42), (8, .42), (11, .29)):
            pts += [r * math.cos(a + math.radians(da)), r * math.sin(a + math.radians(da))]
    p.poly(*pts)
    p.circle(0, 0, .12)


def search(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.circle(-.06, -.06, .27)
    p.line(.14, .14, .38, .38)


def help_(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.circle(0, 0, .4)
    p.arc(0, -.1, .13, -50, 230)
    p.line(.08, 0, 0, .06, 0, .1)
    p.dot(0, .22, .04)


def building(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.rect(-.32, -.4, .16, .4, .04)
    p.line(.16, -.06, .34, -.06, .34, .4)
    for yy in (-.2, 0, .2):
        p.line(-.16, yy, -.12, yy)
        p.line(.0, yy, .02, yy)


client = building


def ticket(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.rect(-.42, -.26, .42, .26, .06)
    for yy in (-.14, 0, .14):
        p.line(.12, yy - .03, .12, yy + .03)


def chevron_down(cv, x, y, s, col, bg):
    _Pen(cv, x, y, s, col, bg).line(-.2, -.08, 0, .12, .2, -.08)


def chevron_right(cv, x, y, s, col, bg):
    _Pen(cv, x, y, s, col, bg).line(-.08, -.2, .12, 0, -.08, .2)


def more(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    for xx in (-.24, 0, .24):
        p.dot(xx, 0, .05)


def plus(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.line(-.3, 0, .3, 0)
    p.line(0, -.3, 0, .3)


def close(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.line(-.24, -.24, .24, .24)
    p.line(-.24, .24, .24, -.24)


def folder(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.poly(-.42, -.3, -.12, -.3, -.02, -.18, .42, -.18, .42, .32, -.42, .32)


def external(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.line(-.02, -.34, -.36, -.34, -.36, .36, .34, .36, .34, .02)
    p.line(.06, -.06, .38, -.38)
    p.line(.12, -.38, .38, -.38, .38, -.12)


def eye(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.arc(0, .3, .5, 37, 106)
    p.arc(0, -.3, .5, 217, 106)
    p.circle(0, 0, .12)


def play(cv, x, y, s, col, bg):
    _Pen(cv, x, y, s, col, bg).poly(-.2, -.3, .3, 0, -.2, .3)


def clock(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.circle(0, 0, .4)
    p.line(0, -.22, 0, 0, .16, .1)


def shield(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.poly(0, -.42, .34, -.28, .3, .1, 0, .42, -.3, .1, -.34, -.28)


def upload(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.line(0, .14, 0, -.36)
    p.line(-.18, -.18, 0, -.36, .18, -.18)
    p.line(-.38, .14, -.38, .36, .38, .36, .38, .14)


def refresh(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.arc(0, 0, .34, 40, 290)
    p.line(.26, -.42, .27, -.2, .05, -.2)


def user(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.circle(0, -.16, .17)
    p.arc(0, .42, .36, 20, 140)


def lock(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.rect(-.3, -.06, .3, .4, .06)
    p.arc(0, -.12, .18, 0, 180)
    p.line(-.18, -.12, -.18, -.06)
    p.line(.18, -.12, .18, -.06)


def link(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.rect(-.44, -.15, .08, .15, .15)
    p.rect(-.08, -.15, .44, .15, .15)


def migrate(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.circle(-.2, -.18, .13)
    p.arc(-.2, .32, .26, 20, 140)
    p.line(.06, .02, .4, .02)
    p.line(.26, -.12, .4, .02, .26, .16)


def netclone(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.rect(-.42, -.12, -.14, .12, .05)
    p.rect(.14, -.4, .42, -.16, .05)
    p.rect(.14, .16, .42, .4, .05)
    p.line(-.14, 0, 0, 0, 0, -.28, .14, -.28)
    p.line(0, 0, 0, .28, .14, .28)


def usb(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.rect(-.18, -.08, .18, .42, .06)
    p.line(-.1, -.08, -.1, -.38, .1, -.38, .1, -.08)


def package(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.poly(0, -.4, .36, -.2, .36, .2, 0, .4, -.36, .2, -.36, -.2)
    p.line(-.36, -.2, 0, 0, .36, -.2)
    p.line(0, 0, 0, .4)


def deploy(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.line(0, -.4, 0, .12)
    p.line(-.16, -.04, 0, .12, .16, -.04)
    p.line(-.38, .1, -.38, .36, .38, .36, .38, .1)


def task(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    for i in range(3):
        yy = -.26 + i * .26
        p.line(-.38, yy, -.29, yy + .08, -.15, yy - .08)
        p.line(-.02, yy, .38, yy)


def present(cv, x, y, s, col, bg):
    p = _Pen(cv, x, y, s, col, bg)
    p.rect(-.42, -.34, .42, .2, .06)
    p.line(0, .2, 0, .4)
    p.poly(-.08, -.16, .12, -.07, -.08, .02)


ICONS = {
    "home": home, "recover": recover, "partition": partition, "wipe": wipe, "erase": wipe, "shred": shred,
    "health": health, "clone": clone, "advanced": advanced, "terminal": terminal, "drive": drive, "drives": drive,
    "image": image, "check": check, "warn": warn, "pc": pc, "monitor": pc, "machines": machines, "manage": manage,
    "jobs": jobs, "settings": settings, "search": search, "help": help_, "building": building, "client": client,
    "ticket": ticket, "chevron_down": chevron_down, "chevron_right": chevron_right, "more": more, "plus": plus,
    "close": close, "folder": folder, "external": external, "eye": eye, "play": play, "clock": clock,
    "shield": shield, "upload": upload, "refresh": refresh, "user": user, "lock": lock, "link": link,
    "migrate": migrate, "netclone": netclone, "usb": usb, "package": package, "deploy": deploy, "task": task,
    "present": present,
}


def draw(cv, name, x, y, s, col, bg=""):
    ICONS.get(name, jobs)(cv, x, y, s, col, bg)


def draw_badge(cv, name, size, fg, bg, offset=0.0, square=True):
    """Tinted tile with an outline icon in the middle (8 px radius at 36 px)."""
    cv.delete("all")
    r = size * .22 if square else size / 2
    rounded_rect(cv, 0, offset, size, size + offset, r, fill=bg, outline="")
    draw(cv, name, size / 2, size / 2 + offset, size * .52, fg, bg)
