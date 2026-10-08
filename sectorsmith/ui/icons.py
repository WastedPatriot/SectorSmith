"""Small vector icons drawn on a tk Canvas (crisp at any DPI, recolour with the theme)."""
from __future__ import annotations

import math


def rounded_rect(cv, x0, y0, x1, y1, r, **kw):
    r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1, x0, y1,
           x0, y1 - r, x0, y0 + r, x0, y0]
    return cv.create_polygon(pts, smooth=True, **kw)


def _w(s):
    return max(2, s / 14)


def recover(cv, x, y, s, col, bg):
    w = _w(s)
    rounded_rect(cv, x - s * .32, y - s * .4, x + s * .18, y + s * .32, s * .06, outline=col, width=w, fill="")
    for i in range(3):
        cv.create_line(x - s * .22, y - s * .22 + i * s * .14, x + s * .06, y - s * .22 + i * s * .14, fill=col,
                       width=w * .8, capstyle="round")
    cv.create_oval(x + s * .0, y + s * .02, x + s * .32, y + s * .34, outline=col, width=w, fill=bg)
    cv.create_line(x + s * .28, y + s * .3, x + s * .42, y + s * .44, fill=col, width=w * 1.3, capstyle="round")


def partition(cv, x, y, s, col, bg):
    w = _w(s)
    r = s * .36
    cv.create_arc(x - r, y - r, x + r, y + r, start=70, extent=290, style="pieslice", outline=col, width=w,
                  fill="")
    r2 = r * 1.0
    dx, dy = s * .08, -s * .08
    cv.create_arc(x - r2 + dx, y - r2 + dy, x + r2 + dx, y + r2 + dy, start=0, extent=70, style="pieslice",
                  outline=col, width=w, fill=col)


def wipe(cv, x, y, s, col, bg):
    w = _w(s)

    def star(cx, cy, r):
        pts = []
        for i in range(8):
            ang = math.pi / 4 * i - math.pi / 2
            rr = r if i % 2 == 0 else r * .28
            pts += [cx + rr * math.cos(ang), cy + rr * math.sin(ang)]
        cv.create_polygon(pts, fill=col, outline="")
    star(x - s * .08, y + s * .02, s * .34)
    star(x + s * .26, y - s * .26, s * .14)
    star(x + s * .24, y + s * .3, s * .1)
    _ = w


def shred(cv, x, y, s, col, bg):
    w = _w(s)
    rounded_rect(cv, x - s * .26, y - s * .42, x + s * .26, y - s * .06, s * .05, outline=col, width=w, fill="")
    cv.create_line(x - s * .4, y - s * .04, x + s * .4, y - s * .04, fill=col, width=w * 1.4, capstyle="round")
    for i in range(5):
        xx = x - s * .22 + i * s * .11
        cv.create_line(xx, y + s * .06, xx + (s * .03 if i % 2 else -s * .03), y + s * .4, fill=col, width=w * .8,
                       capstyle="round")


def health(cv, x, y, s, col, bg):
    w = _w(s)
    pts = []
    for i in range(41):
        t = math.pi * 2 * i / 40
        hx = 16 * math.sin(t) ** 3
        hy = -(13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t))
        pts += [x + hx * s / 40, y + hy * s / 40 + s * .02]
    cv.create_polygon(pts, outline=col, width=w, fill="", smooth=True)
    cv.create_line(x - s * .3, y + s * .02, x - s * .1, y + s * .02, x - s * .03, y - s * .14, x + s * .06,
                   y + s * .16, x + s * .12, y + s * .02, x + s * .3, y + s * .02, fill=col, width=w * .9,
                   joinstyle="round", capstyle="round")


def clone(cv, x, y, s, col, bg):
    w = _w(s)
    rounded_rect(cv, x - s * .38, y - s * .38, x + s * .1, y + s * .1, s * .07, outline=col, width=w, fill="")
    rounded_rect(cv, x - s * .1, y - s * .1, x + s * .38, y + s * .38, s * .07, outline=col, width=w, fill=col)
    cv.create_line(x + s * .02, y + s * .14, x + s * .26, y + s * .14, fill=bg, width=w, arrow="last",
                   arrowshape=(s * .1, s * .12, s * .05))


def advanced(cv, x, y, s, col, bg):
    w = _w(s)
    for i, pos in enumerate((-.25, 0, .25)):
        yy = y + s * pos
        cv.create_line(x - s * .36, yy, x + s * .36, yy, fill=col, width=w, capstyle="round")
        kx = x + s * (.18, -.16, .06)[i]
        cv.create_oval(kx - s * .08, yy - s * .08, kx + s * .08, yy + s * .08, fill=bg, outline=col, width=w)


def drive(cv, x, y, s, col, bg):
    w = _w(s)
    rounded_rect(cv, x - s * .4, y - s * .26, x + s * .4, y + s * .26, s * .08, outline=col, width=w, fill="")
    cv.create_line(x - s * .4, y + s * .04, x + s * .4, y + s * .04, fill=col, width=w * .8)
    cv.create_oval(x + s * .2, y + s * .1, x + s * .28, y + s * .18, fill=col, outline="")


def image(cv, x, y, s, col, bg):
    w = _w(s)
    cv.create_oval(x - s * .38, y - s * .38, x + s * .38, y + s * .38, outline=col, width=w)
    cv.create_oval(x - s * .1, y - s * .1, x + s * .1, y + s * .1, outline=col, width=w)


def check(cv, x, y, s, col, bg):
    cv.create_line(x - s * .3, y, x - s * .08, y + s * .22, x + s * .32, y - s * .22, fill=col, width=_w(s) * 1.4,
                   capstyle="round", joinstyle="round")


def warn(cv, x, y, s, col, bg):
    cv.create_polygon(x, y - s * .38, x + s * .4, y + s * .32, x - s * .4, y + s * .32, outline=col,
                      width=_w(s), fill="", joinstyle="round")
    cv.create_line(x, y - s * .1, x, y + s * .1, fill=col, width=_w(s) * 1.2, capstyle="round")
    cv.create_oval(x - s * .03, y + s * .18, x + s * .03, y + s * .24, fill=col, outline=col)


def pc(cv, x, y, s, col, bg):
    w = _w(s)
    rounded_rect(cv, x - s * .4, y - s * .34, x + s * .4, y + s * .18, s * .06, outline=col, width=w, fill="")
    cv.create_line(x, y + s * .18, x, y + s * .32, fill=col, width=w)
    cv.create_line(x - s * .2, y + s * .34, x + s * .2, y + s * .34, fill=col, width=w, capstyle="round")


def link(cv, x, y, s, col, bg):
    w = _w(s)
    for dx, dy in ((-.14, .1), (.14, -.1)):
        cv.create_oval(x + s * dx - s * .22, y + s * dy - s * .13, x + s * dx + s * .22, y + s * dy + s * .13,
                       outline=col, width=w)


def migrate(cv, x, y, s, col, bg):
    w = _w(s)
    cv.create_oval(x - s * .32, y - s * .34, x - s * .08, y - s * .1, outline=col, width=w)
    cv.create_arc(x - s * .4, y - s * .04, x + s * .0, y + s * .4, start=0, extent=180, style="arc", outline=col,
                  width=w)
    cv.create_line(x + s * .02, y + s * .02, x + s * .4, y + s * .02, fill=col, width=w, arrow="last",
                   arrowshape=(s * .14, s * .16, s * .07))


ICONS = {"recover": recover, "partition": partition, "wipe": wipe, "shred": shred, "health": health,
         "clone": clone, "advanced": advanced, "drive": drive, "image": image, "check": check, "warn": warn, "pc": pc, "link": link, "migrate": migrate}


def draw_badge(cv, name, size, fg, bg, offset=0.0):
    """Rounded tinted square with an icon in the middle."""
    cv.delete("all")
    rounded_rect(cv, 2, 2 + offset, size - 2, size - 2 + offset, size * .28, fill=bg, outline="")
    ICONS[name](cv, size / 2, size / 2 + offset, size * .62, fg, bg)
