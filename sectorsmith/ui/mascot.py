"""Mossbit — SectorSmith's pixel-art helper. Plays pre-rendered sprite sheets (see tools/make_sprites.py)
in the sidebar, and sweeps across progress bars."""
from __future__ import annotations

import json
import math
import os
import random
import time
import tkinter as tk

import customtkinter as ctk

from . import theme

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

LINES = {
    "hello": ["Hi! I'm Mossbit. What are we doing today?", "Ready to tidy up some drives!",
              "Pick a task and I'll grab my broom.", "Hello again! Which job is it today?"],
    "pick": ["Which drive should we look at?", "Point me at a drive!", "Take your time — pick a drive."],
    "working": ["Sweep, sweep, sweep…", "Checking every sector for you.", "Working hard over here!",
                "Almost there… probably!", "Tidying up, one sector at a time."],
    "found": ["Found them! Your files are safe now.", "Look what I dug up!", "Ta-da! Here's what I found."],
    "none": ["Nothing turned up here. Want me to look deeper?", "All clear — nothing to find."],
    "confirm": ["Just checking — this one can't be undone.", "Double-check that's the right drive!",
                "Careful! This is permanent."],
    "wiped": ["All clean! Nothing left to find.", "Swept spotless!", "Squeaky clean. Nobody's reading that."],
    "healthy": ["This drive looks healthy!", "No bad sectors. Nice!"],
    "sick": ["Uh-oh, this drive has bad sectors. Back it up soon.", "This drive isn't well. Let's save your data."],
    "error": ["Oops, that didn't work. Details are below.", "Hmm, something went wrong."],
    "cancel": ["Okay, stopped!", "No problem, cancelled."],
    "done": ["All done!", "Finished! That was fun.", "Done and dusted!"],
    "drop": ["Ooh, what did you bring me?", "Got it! Let's see…"],
    "guide": ["Here's how everything works.", "Pick a topic — I'll walk you through it."],
}

MOOD_ANIM = {"idle": "idle", "working": "sweep", "happy": "happy", "sad": "sad", "warn": "warn"}


class Sprites:
    """Loads sprite strips once per Tk interpreter and slices them into frames (no Pillow needed)."""

    _cache: dict = {}

    @classmethod
    def meta(cls):
        if "_meta" not in cls._cache:
            cls._cache["_meta"] = json.load(open(os.path.join(ASSETS, "mossbit.json")))
        return cls._cache["_meta"]

    @classmethod
    def get(cls, widget, anim: str, scale: int, flip: bool = False):
        key = (str(widget.tk), anim, scale, flip)
        if key in cls._cache:
            return cls._cache[key]
        meta = cls.meta()
        n = meta["anims"][anim]["frames"]
        fps = meta["anims"][anim]["fps"]
        w, h = meta["w"] * scale, meta["h"] * scale
        suffix = "_L" if flip else ""
        strip = tk.PhotoImage(master=widget, file=os.path.join(ASSETS, f"mossbit_{anim}_x{scale}{suffix}.png"))
        frames = []
        for i in range(n):
            fr = tk.PhotoImage(master=widget, width=w, height=h)
            fr.tk.call(fr, "copy", strip, "-from", i * w, 0, (i + 1) * w, h, "-to", 0, 0)
            frames.append(fr)
        cls._cache[key] = (frames, fps, w, h)
        return cls._cache[key]


class SpeechBubble(ctk.CTkFrame):
    def __init__(self, master, **kw):
        super().__init__(master, corner_radius=18, fg_color=theme.PALETTE["card"],
                         border_width=1, border_color=theme.PALETTE["border"], **kw)
        self.lbl = ctk.CTkLabel(self, text="", font=theme.font(14, "bold"), wraplength=210, justify="left",
                                text_color=theme.PALETTE["text"], anchor="w")
        self.lbl.pack(padx=16, pady=14, fill="x")
        self._job = None

    def say(self, text: str):
        if self._job:
            self.after_cancel(self._job)
        self._full, self._i = text, 0
        self._type()

    def _type(self):
        self._i += 2
        self.lbl.configure(text=self._full[: self._i])
        if self._i < len(self._full):
            self._job = self.after(16, self._type)
        else:
            self._job = None


class _Pixels:
    """Square 'pixel' particles snapped to the sprite's pixel grid."""

    def _init_pixels(self, grid):
        self.grid = grid
        self.particles: list[list] = []

    def burst(self, cx, cy, n=36):
        cols = [theme.c("accent"), theme.c("violet"), "#FFDE60", "#52C98E", "#7CCCFF", "#ffffff"]
        for _ in range(n):
            a = random.uniform(0, math.tau)
            v = random.uniform(2.5, 7)
            self.particles.append([cx, cy, math.cos(a) * v, math.sin(a) * v - 3.5, random.uniform(.7, 1.3),
                                   random.choice(cols)])

    def _draw_particles(self, gravity=.3):
        g = self.grid
        alive = []
        for p in self.particles:
            p[0] += p[2]
            p[1] += p[3]
            p[3] += gravity
            p[4] -= .04
            if p[4] > 0:
                alive.append(p)
                x, y = round(p[0] / g) * g, round(p[1] / g) * g
                self.create_rectangle(x, y, x + g, y + g, fill=p[5], outline="")
        self.particles = alive


class Mascot(tk.Canvas, _Pixels):
    """Sidebar Mossbit. Wanders around on its own, sweeps back and forth while working,
    hops when something finishes, and reacts to clicks."""

    SCALE = 4

    def __init__(self, master, bubble: SpeechBubble | None = None, width=280):
        frames, _fps, w, h = Sprites.get(master, "idle", self.SCALE)
        super().__init__(master, width=width, height=h, highlightthickness=0, bd=0)
        self.W, self.H, self.fw = width, h, w
        b0, b1 = Sprites.meta().get("body", [6, 36])
        self.body = (b0 * self.SCALE, b1 * self.SCALE)
        self.bubble = bubble
        self.mood = "idle"
        self.mood_t = time.monotonic()
        self.progress = 0.0
        self.facing = 1  # 1 = right, -1 = left
        self.x = (width - w) / 2 + 20
        self.state = "rest"
        self.state_until = time.monotonic() + 2.5
        self.target = self.x
        self.last = time.monotonic()
        self._init_pixels(self.SCALE)
        theme.on_theme_change(lambda: self.configure(background=theme.c("panel")))
        self.configure(background=theme.c("panel"))
        self.bind("<Button-1>", lambda _e: self.set_mood("happy", "done"))
        self._tick()

    # --- API ------------------------------------------------------------
    def set_mood(self, mood: str, line_key: str | None = None, text: str | None = None):
        if mood != self.mood or mood == "happy":
            self.mood = mood
            self.mood_t = time.monotonic()
            if mood == "happy":
                self.burst(self.x + self._body_mid(), self.H * .45)
        if self.bubble and (text or line_key):
            self.bubble.say(text or random.choice(LINES.get(line_key, LINES["hello"])))

    def say(self, line_key: str | None = None, text: str | None = None):
        if self.bubble:
            self.bubble.say(text or random.choice(LINES[line_key]))

    # --- movement -------------------------------------------------------
    def _bounds(self):
        """Allowed range for self.x so the body stays inside the canvas."""
        b0, b1 = self.body if self.facing == 1 else (self.fw - self.body[1], self.fw - self.body[0])
        return -b0 + 4, self.W - b1 - 4

    def _body_mid(self):
        b0, b1 = self.body if self.facing == 1 else (self.fw - self.body[1], self.fw - self.body[0])
        return (b0 + b1) / 2

    def _turn(self, facing):
        if facing != self.facing:
            mid = self.x + self._body_mid()
            self.facing = facing
            self.x = mid - self._body_mid()

    def _tick(self):
        try:
            self._step()
            self._draw()
        except tk.TclError:
            return
        self.after(40, self._tick)

    def _step(self):
        now = time.monotonic()
        dt = min(.1, now - self.last)
        self.last = now
        lo, hi = self._bounds()
        if self.mood == "working":  # sweep back and forth
            self.x += self.facing * 34 * dt
            if self.x > hi:
                self.x = hi
                self._turn(-1)
            elif self.x < self._bounds()[0]:
                self.x = self._bounds()[0]
                self._turn(1)
            return
        if self.mood != "idle" or now - self.mood_t < .2:
            self.state = "rest"
            self.state_until = now + random.uniform(2, 4)
            return
        if self.state == "rest" and now > self.state_until:
            # pick somewhere new to stroll to
            mid_now = self.x + self._body_mid()
            dest_mid = random.uniform(self.body[1] - self.body[0], self.W - (self.body[1] - self.body[0]))
            if abs(dest_mid - mid_now) < 30:
                dest_mid = self.W - dest_mid
            self._turn(1 if dest_mid > mid_now else -1)
            self.target = dest_mid - self._body_mid()
            self.state = "walk"
        elif self.state == "walk":
            step = self.facing * 42 * dt
            self.x += step
            lo, hi = self._bounds()
            if (self.facing == 1 and self.x >= self.target) or (self.facing == -1 and self.x <= self.target) \
                    or not lo <= self.x <= hi:
                self.x = max(lo, min(hi, self.x))
                self.state = "rest"
                self.state_until = now + random.uniform(3, 7)
                if random.random() < .35:
                    self._turn(-self.facing)  # glance back

    def _draw(self):
        mt = time.monotonic() - self.mood_t
        anim = MOOD_ANIM.get(self.mood, "idle")
        if self.mood == "happy" and mt > 3.0:
            anim = "idle"
        if self.mood == "idle" and self.state == "walk":
            anim = "walk"
        frames, fps, _w, _h = Sprites.get(self, anim, self.SCALE, flip=self.facing == -1)
        i = int(mt * fps) % len(frames) if anim != "walk" else int(time.monotonic() * fps) % len(frames)
        self.delete("all")
        self.create_image(round(self.x), 0, image=frames[i], anchor="nw")
        self._draw_particles()


class SweepTrack(tk.Canvas, _Pixels):
    """Pixel-style progress bar: Mossbit sweeps left to right, the % floats above, dust ahead is cleared."""

    SCALE = 3
    BROOM_X = 46  # broom tip x in sprite pixels (see make_sprites)
    FEET_Y = 42

    def __init__(self, master, width=720):
        frames, _fps, sw, sh = Sprites.get(master, "sweep", self.SCALE)
        self.sw, self.sh = sw, sh
        self.H = sh + 70
        super().__init__(master, width=width, height=self.H, highlightthickness=0, bd=0)
        self.Wd = width
        self.value = 0.0
        self.target = 0.0
        self.label = ""
        self.t0 = time.monotonic()
        rnd = random.Random(11)
        self.dust = [(rnd.random(), rnd.randint(0, 3), rnd.choice((1, 2))) for _ in range(70)]
        self._init_pixels(self.SCALE)
        theme.on_theme_change(lambda: self.configure(background=theme.c("bg")))
        self.configure(background=theme.c("bg"))
        self.bind("<Configure>", lambda e: setattr(self, "Wd", e.width))
        self._tick()

    def set(self, frac: float, label: str = ""):
        self.target = max(0.0, min(1.0, frac))
        self.label = label

    def _tick(self):
        try:
            self._draw()
        except tk.TclError:
            return
        self.after(40, self._tick)

    def _draw(self):
        t = time.monotonic() - self.t0
        self.value += (self.target - self.value) * .12
        self.delete("all")
        g = self.SCALE * 2  # bar block size
        x0 = 30
        x1 = self.Wd - 30
        x1 = x0 + ((x1 - x0) // g) * g
        bar_top = self.H - 30
        bar_h = g * 3
        px = x0 + (x1 - x0) * self.value
        dark = theme.lerp_color(theme.c("track"), "#000000", .25)

        # frame of the bar (pixel border)
        self.create_rectangle(x0 - 3, bar_top - 3, x1 + 3, bar_top + bar_h + 3, fill=dark, outline="")
        self.create_rectangle(x0, bar_top, x1, bar_top + bar_h, fill=theme.c("track"), outline="")
        # swept blocks with gradient + shine row
        nblocks = int((px - x0) // g)
        a, b = "#52C98E", theme.c("violet")
        for k in range(nblocks):
            bx = x0 + k * g
            col = theme.lerp_color(a, b, k * g / max(1, x1 - x0))
            self.create_rectangle(bx, bar_top, bx + g - 1, bar_top + bar_h, fill=col, outline="")
            self.create_rectangle(bx, bar_top, bx + g - 1, bar_top + g * .5,
                                  fill=theme.lerp_color(col, "#ffffff", .35), outline="")
        sparkle_k = int(t * 14) % max(1, nblocks) if nblocks else -1
        if sparkle_k >= 0:
            sx = x0 + sparkle_k * g
            self.create_rectangle(sx + g * .25, bar_top + g, sx + g * .75, bar_top + g * 1.5, fill="#ffffff",
                                  outline="")
        # dust ahead of the broom
        muted = theme.c("muted")
        for fx, row, size in self.dust:
            dx = x0 + (x1 - x0) * fx
            if dx > px + 18:
                bob = (math.sin(t * 2 + fx * 20) > .6) * self.SCALE
                y = bar_top - 6 - row * self.SCALE * 2 - bob
                s = self.SCALE * size
                self.create_rectangle(dx, y - s, dx + s, y, fill=muted, outline="")

        # Mossbit
        frames, fps, _w, _h = Sprites.get(self, "sweep", self.SCALE)
        fr = frames[int(t * fps) % len(frames)]
        left = px - self.BROOM_X * self.SCALE
        left = max(-6 * self.SCALE, left)
        top = bar_top - 3 - self.FEET_Y * self.SCALE
        self.create_image(left, top, image=fr, anchor="nw")
        self._draw_particles(.2)

        # % badge (pixel style) above the head
        txt = self.label or f"{self.value * 100:.0f}%"
        bx = left + 22 * self.SCALE
        by = top - 4
        w = 18 + 10 * len(txt)
        acc = theme.c("accent")
        edge = theme.lerp_color(acc, "#000000", .3)
        self.create_rectangle(bx - w / 2 - 3, by - 30, bx + w / 2 + 3, by + 3, fill=edge, outline="")
        self.create_rectangle(bx - w / 2, by - 27, bx + w / 2, by, fill=acc, outline="")
        self.create_rectangle(bx - 4, by, bx + 4, by + 6, fill=edge, outline="")
        self.create_text(bx, by - 13, text=txt, fill="#ffffff", font=(theme.family(), 13, "bold"))
