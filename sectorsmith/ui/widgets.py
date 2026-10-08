"""Reusable UI pieces: task cards, option cards, drive picker, progress ring, drop zone, toasts."""
from __future__ import annotations

import math
import shutil
import time
import tkinter as tk

import customtkinter as ctk

from ..util import human_size, human_time
from . import icons, theme

P = theme.PALETTE


def bind_all(widget, seq, fn):
    widget.bind(seq, fn, add="+")
    for ch in widget.winfo_children():
        bind_all(ch, seq, fn)


class IconBadge(tk.Canvas):
    def __init__(self, master, name, size=52, tone="accent"):
        super().__init__(master, width=size, height=size, highlightthickness=0, bd=0)
        self.name, self.size, self.tone = name, size, tone
        self.offset = 0.0
        theme.on_theme_change(self.redraw)
        self.redraw()

    def set_bg(self, color):
        self.configure(background=color)

    def redraw(self):
        fg = theme.c(self.tone)
        soft = theme.c(self.tone + "_soft") if (self.tone + "_soft") in P else theme.c("accent_soft")
        icons.draw_badge(self, self.name, self.size, fg, soft, self.offset)

    def bounce(self):
        start = time.monotonic()

        def step():
            t = time.monotonic() - start
            if t > .45:
                self.offset = 0
                self.redraw()
                return
            self.offset = -math.sin(t / .45 * math.pi) * 6
            self.redraw()
            self.after(16, step)
        step()


class TaskCard(ctk.CTkFrame):
    def __init__(self, master, icon, title, desc, command, tone="accent"):
        super().__init__(master, corner_radius=22, fg_color=P["card"], border_width=2, border_color=P["card"])
        self.command = command
        self.badge = IconBadge(self, icon, 56, tone)
        self.badge.grid(row=0, column=0, rowspan=2, padx=(20, 14), pady=20, sticky="n")
        self.t = ctk.CTkLabel(self, text=title, font=theme.font(17, "bold"), text_color=P["text"], anchor="w")
        self.t.grid(row=0, column=1, sticky="sw", padx=(0, 18), pady=(22, 0))
        self.d = ctk.CTkLabel(self, text=desc, font=theme.font(13), text_color=P["muted"], anchor="nw",
                              justify="left", wraplength=250)
        self.d.grid(row=1, column=1, sticky="nw", padx=(0, 18), pady=(2, 20))
        self.grid_columnconfigure(1, weight=1)
        self.bind("<Configure>", lambda e: self.d.configure(wraplength=max(120, e.width - 130)), add="+")
        bind_all(self, "<Enter>", self._enter)
        bind_all(self, "<Leave>", self._leave)
        bind_all(self, "<Button-1>", lambda _e: self.command())
        theme.on_theme_change(self._sync)
        self._sync()

    def _sync(self):
        self.badge.set_bg(theme.c("card"))

    def _enter(self, _e=None):
        self.configure(fg_color=P["card_hover"], border_color=P["accent"])
        self.badge.set_bg(theme.c("card_hover"))
        self.badge.bounce()

    def _leave(self, e=None):
        x, y = self.winfo_pointerxy()
        w = self.winfo_containing(x, y)
        while w is not None:
            if w is self:
                return
            w = w.master
        self.configure(fg_color=P["card"], border_color=P["card"])
        self.badge.set_bg(theme.c("card"))


class OptionCard(ctk.CTkFrame):
    """Selectable big option (radio-style)."""

    def __init__(self, master, title, desc, value, group, icon=None, tone="accent", badge_text=None):
        super().__init__(master, corner_radius=18, fg_color=P["card"], border_width=2, border_color=P["border"])
        self.value, self.group = value, group
        group.append(self)
        self.enabled = True
        col = 0
        if icon:
            self.badge = IconBadge(self, icon, 46, tone)
            self.badge.grid(row=0, column=0, rowspan=2, padx=(16, 12), pady=16, sticky="n")
            theme.on_theme_change(lambda: self.badge.set_bg(theme.c("card")))
            self.badge.set_bg(theme.c("card"))
            col = 1
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=col, sticky="w", padx=(16 if not icon else 0, 16), pady=(16, 0))
        ctk.CTkLabel(head, text=title, font=theme.font(15, "bold"), text_color=P["text"]).pack(side="left")
        if badge_text:
            ctk.CTkLabel(head, text=f"  {badge_text}  ", font=theme.font(11, "bold"), text_color="#ffffff",
                         fg_color=P["accent"], corner_radius=10).pack(side="left", padx=8)
        self.desc = ctk.CTkLabel(self, text=desc, font=theme.font(12), text_color=P["muted"], justify="left",
                                 anchor="w", wraplength=330)
        self.desc.grid(row=1, column=col, sticky="w", padx=(16 if not icon else 0, 16), pady=(2, 16))
        self.grid_columnconfigure(col, weight=1)
        bind_all(self, "<Button-1>", lambda _e: self.select())
        self.on_select = None

    def select(self):
        if not self.enabled:
            return
        for o in self.group:
            o.configure(border_color=P["border"], fg_color=P["card"])
        self.configure(border_color=P["accent"], fg_color=P["accent_soft"])
        if getattr(self, "badge", None):
            self.badge.set_bg(theme.c("accent_soft"))
        for o in self.group:
            if o is not self and getattr(o, "badge", None):
                o.badge.set_bg(theme.c("card"))
        for o in self.group:
            o.selected = o is self
        if self.on_select:
            self.on_select(self.value)

    def set_enabled(self, ok: bool, why: str = ""):
        self.enabled = ok
        if not ok:
            self.desc.configure(text=why, text_color=P["warn"])


def selected_value(group):
    for o in group:
        if getattr(o, "selected", False):
            return o.value
    return None


class Pill(ctk.CTkLabel):
    def __init__(self, master, text, tone="violet"):
        super().__init__(master, text=f" {text} ", font=theme.font(11, "bold"), corner_radius=9,
                         fg_color=P[tone + "_soft"] if tone + "_soft" in P else P["violet_soft"],
                         text_color=P[tone])


class DrivePicker(ctk.CTkScrollableFrame):
    """Lists disks and their partitions as selectable cards.

    targets: list of dicts with keys kind ('disk'|'part'), dev, part, disabled (str|None)
    mode: 'disk' | 'part' | 'any'
    """

    def __init__(self, master, inventory, mode="any", on_select=None, exclude_path=None, height=360,
                 writes=False):
        super().__init__(master, fg_color="transparent", height=height)
        self.on_select = on_select
        self.rows = []
        self.selected = None
        if not inventory:
            ctk.CTkLabel(self, text="No drives found. Run as administrator, or drop a disk image (.img/.vhd) "
                                    "onto the window.", font=theme.font(13), text_color=P["muted"],
                         wraplength=520).pack(pady=30)
        def is_usb(d):
            return d.removable or d.bus in ("USB", "SD", "MMC")
        if writes:  # external drives first when choosing somewhere to write
            inventory = sorted(inventory, key=lambda x: not is_usb(x[0]))
        for dev, pt, err in inventory:
            if exclude_path and dev.path == exclude_path:
                continue
            disk_disabled = None
            if writes and dev.is_system:
                disk_disabled = "Windows runs from this disk — protected"
            sub = f"{dev.bus} · {pt.scheme if pt else '?'} · {len(pt.partitions) if pt else 0} partition(s)"
            if dev.serial:
                sub += f" · S/N {dev.serial}"
            if err:
                sub = f"Can't read: {err}"
            self._row(dict(kind="disk", dev=dev, part=None), "image" if dev.is_image else "drive",
                      f"{dev.name}  ·  {dev.model or 'Disk'}", sub, human_size(dev.size),
                      selectable=mode in ("disk", "any") and not disk_disabled and not err,
                      tag="SYSTEM" if dev.is_system else ("USB" if is_usb(dev) else None), indent=0,
                      disabled_text=disk_disabled)
            if pt and mode in ("part", "any"):
                for p in pt.partitions:
                    if p.type_id in ("0x05", "0x0F", "0x85"):
                        continue
                    name = p.label or p.name or p.type_name
                    letters = ", ".join(m.rstrip("\\") for m in p.mount)
                    title = f"{letters}  {name}" if letters else name
                    sub = f"{p.fs or 'Unknown'} · partition #{p.index} · starts at sector {p.start_lba:,}"
                    usage = None
                    if p.mount:
                        try:
                            u = shutil.disk_usage(p.mount[0])
                            usage = u.used / u.total if u.total else None
                            sub += f" · {human_size(u.free)} free"
                        except OSError:
                            pass
                    locked = writes and dev.is_system
                    self._row(dict(kind="part", dev=dev, part=p), "partition", title, sub,
                              human_size(p.size_bytes(dev.sector_size)), selectable=not locked, indent=34,
                              usage=usage, fs=p.fs, disabled_text="Protected (system disk)" if locked else None)

    def _row(self, target, icon, title, sub, size, selectable, indent=0, tag=None, usage=None, fs=None,
             disabled_text=None):
        card = ctk.CTkFrame(self, corner_radius=16, fg_color=P["card"], border_width=2, border_color=P["card"])
        card.pack(fill="x", padx=(indent, 6), pady=4)
        badge = IconBadge(card, icon, 40, "violet" if target["kind"] == "disk" else "accent")
        badge.grid(row=0, column=0, rowspan=2, padx=(12, 12), pady=10)
        badge.set_bg(theme.c("card"))
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.grid(row=0, column=1, sticky="w", pady=(10, 0))
        ctk.CTkLabel(top, text=title, font=theme.font(14, "bold"), text_color=P["text"]).pack(side="left")
        if fs:
            Pill(top, fs, "violet").pack(side="left", padx=6)
        if tag:
            Pill(top, tag, "warn" if tag == "SYSTEM" else "success").pack(side="left", padx=6)
        ctk.CTkLabel(card, text=disabled_text or sub, font=theme.font(12),
                     text_color=P["warn"] if disabled_text else P["muted"]).grid(row=1, column=1, sticky="w",
                                                                                 pady=(0, 10))
        ctk.CTkLabel(card, text=size, font=theme.font(14, "bold"), text_color=P["text"]).grid(
            row=0, column=2, rowspan=2, padx=16)
        if usage is not None:
            bar = ctk.CTkProgressBar(card, width=90, height=6, progress_color=P["accent"], fg_color=P["track"])
            bar.set(usage)
            bar.grid(row=0, column=3, rowspan=2, padx=(0, 16))
        card.grid_columnconfigure(1, weight=1)
        if not selectable:
            return
        self.rows.append((card, badge, target))

        def click(_e=None):
            for c2, b2, _t in self.rows:
                c2.configure(border_color=P["card"], fg_color=P["card"])
                b2.set_bg(theme.c("card"))
            card.configure(border_color=P["accent"], fg_color=P["accent_soft"])
            badge.set_bg(theme.c("accent_soft"))
            self.selected = target
            if self.on_select:
                self.on_select(target)

        def enter(_e=None):
            if self.selected is not target:
                card.configure(border_color=P["border"])

        def leave(_e=None):
            if self.selected is not target:
                card.configure(border_color=P["card"])
        card.click = click
        bind_all(card, "<Button-1>", click)
        card.bind("<Enter>", enter)
        card.bind("<Leave>", leave)


class ProgressPanel(ctk.CTkFrame):
    def __init__(self, master, on_cancel):
        super().__init__(master, fg_color="transparent")
        from .mascot import SweepTrack
        self.ring = SweepTrack(self, width=720)
        self.ring.pack(pady=(4, 0), fill="x")
        self.label = ctk.CTkLabel(self, text="", font=theme.font(18, "bold"), text_color=P["text"])
        self.label.pack()
        self.detail = ctk.CTkLabel(self, text="", font=theme.font(12), text_color=P["muted"], wraplength=560)
        self.detail.pack(pady=(2, 10))
        chips = ctk.CTkFrame(self, fg_color="transparent")
        chips.pack()
        self.chips = {}
        for k, title in (("speed", "SPEED"), ("eta", "TIME LEFT"), ("done", "PROCESSED"), ("elapsed", "ELAPSED")):
            f = ctk.CTkFrame(chips, corner_radius=14, fg_color=P["card"])
            f.pack(side="left", padx=5)
            ctk.CTkLabel(f, text=title, font=theme.font(10, "bold"), text_color=P["muted"]).pack(padx=16,
                                                                                                 pady=(8, 0))
            v = ctk.CTkLabel(f, text="—", font=theme.font(15, "bold"), text_color=P["text"])
            v.pack(padx=16, pady=(0, 8))
            self.chips[k] = v
        self.cancel = ctk.CTkButton(self, text="Cancel", width=140, height=38, corner_radius=19,
                                    fg_color=P["card"], hover_color=P["danger_soft"], text_color=P["danger"],
                                    border_width=2, border_color=P["danger"], font=theme.font(13, "bold"),
                                    command=on_cancel)
        self.cancel.pack(pady=16)

    def update_from(self, s: dict):
        self.ring.set(s["pct"] / 100)
        self.label.configure(text=s["label"])
        self.detail.configure(text=s["detail"])
        self.chips["speed"].configure(text=f"{human_size(s['speed'])}/s" if s["speed"] else "—")
        self.chips["eta"].configure(text=human_time(s["eta"]))
        self.chips["done"].configure(text=human_size(s["done"]) if s["total"] > 4096 else f"{s['done']}/{s['total']}")
        self.chips["elapsed"].configure(text=human_time(s["elapsed"]))


class DropZone(ctk.CTkFrame):
    def __init__(self, master, text="Drop files or folders here", sub="or use the buttons below", height=150,
                 icon="shred", tone="accent", wide=False):
        super().__init__(master, corner_radius=22, fg_color=P["card"], border_width=3, border_color=P["border"],
                         height=height)
        self.pack_propagate(False)
        self.grid_propagate(False)
        self.badge = IconBadge(self, icon, 54, tone)
        self.badge.set_bg(theme.c("card"))
        theme.on_theme_change(lambda: self.badge.set_bg(theme.c("card")))
        title = ctk.CTkLabel(self, text=text, font=theme.font(16, "bold"), text_color=P["text"])
        subl = ctk.CTkLabel(self, text=sub, font=theme.font(12), text_color=P["muted"])
        if wide:
            # badge beside the text, so a short strip across the page doesn't clip the second line
            self.grid_columnconfigure((0, 3), weight=1)
            self.grid_rowconfigure((0, 1), weight=1)
            self.badge.grid(row=0, column=1, rowspan=2, padx=(0, 16))
            title.grid(row=0, column=2, sticky="sw")
            subl.grid(row=1, column=2, sticky="nw")
        else:
            self.badge.pack(pady=(22, 6))
            title.pack()
            subl.pack()

    def hot(self, on: bool):
        self.configure(border_color=P["accent"] if on else P["border"],
                       fg_color=P["accent_soft"] if on else P["card"])
        self.badge.set_bg(theme.c("accent_soft") if on else theme.c("card"))
        if on:
            self.badge.bounce()


class Toast(ctk.CTkFrame):
    def __init__(self, root, text, tone="success"):
        super().__init__(root, corner_radius=16, fg_color=P[tone], border_width=0)
        ctk.CTkLabel(self, text=text, font=theme.font(13, "bold"), text_color="#ffffff", wraplength=360).pack(
            padx=18, pady=12)
        self.place(relx=1.0, rely=1.0, x=-24, y=80, anchor="se")
        self._anim(80, -24, lambda: self.after(3200, lambda: self._anim(-24, 90, self.destroy)))

    def _anim(self, y0, y1, done):
        start = time.monotonic()

        def step():
            t = min(1.0, (time.monotonic() - start) / .28)
            e = 1 - (1 - t) ** 3
            try:
                self.place_configure(y=y0 + (y1 - y0) * e)
            except tk.TclError:
                return
            if t < 1:
                self.after(15, step)
            else:
                done()
        step()


def primary_button(master, text, command, width=180, tone="accent"):
    return ctk.CTkButton(master, text=text, command=command, width=width, height=44, corner_radius=22,
                         font=theme.font(14, "bold"), fg_color=P[tone],
                         hover_color=P["accent_hover"] if tone == "accent" else P[tone], text_color="#ffffff")


def ghost_button(master, text, command, width=120):
    return ctk.CTkButton(master, text=text, command=command, width=width, height=44, corner_radius=22,
                         font=theme.font(14, "bold"), fg_color="transparent", hover_color=P["card_hover"],
                         text_color=P["muted"], border_width=2, border_color=P["border"])
