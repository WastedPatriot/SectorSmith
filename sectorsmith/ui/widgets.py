"""Reusable UI pieces: buttons, cards, status pills and tiles, the themed table, empty and loading states, popover
menus, toasts, the drive picker, progress panel, drop zone and the typed confirmation box."""
from __future__ import annotations

import math
import shutil
import time
import tkinter as tk
from tkinter import ttk

import customtkinter as ctk

from ..util import human_size, human_time
from . import icons, theme

P = theme.PALETTE


def bind_all(widget, seq, fn):
    widget.bind(seq, fn, add="+")
    for ch in widget.winfo_children():
        bind_all(ch, seq, fn)


def soft(tone: str) -> str:
    return tone + "_soft" if tone + "_soft" in P else "accent_soft"


def caption(master, text, **kw):
    """Small uppercase group header (table headers, sub-nav groups)."""
    return ctk.CTkLabel(master, text=text.upper(), font=theme.font_style("caption"), text_color=P["muted"],
                        anchor="w", height=16, **kw)


def divider(master, vertical=False, color="border"):
    """1 px rule. A plain Tk frame: CustomTkinter draws nothing at 1 px wide."""
    f = tk.Frame(master, width=1, height=20 if vertical else 1, bd=0, highlightthickness=0, background=theme.c(color))

    def recolor():
        f.configure(background=theme.c(color))
    theme.on_theme_change(recolor)
    return f


class AutoScroll(ctk.CTkScrollableFrame):
    """Scrollable frame whose scrollbar only shows when the content is taller than the view."""

    def __init__(self, master, **kw):
        kw.setdefault("fg_color", "transparent")
        kw.setdefault("corner_radius", 0)  # no inner inset, so content lines up with the page title
        super().__init__(master, **kw)
        self._parent_canvas.bind("<Configure>", lambda _e: self.after_idle(self._fit), add="+")
        self.bind("<Configure>", lambda _e: self.after_idle(self._fit), add="+")
        self.after(300, self._fit)

    def _fit(self):
        try:
            lo, hi = self._parent_canvas.yview()
            need = not (lo <= 0 and hi >= 1)
            if need == bool(self._scrollbar.winfo_manager()):
                return
            if need:
                self._scrollbar.grid()
                self._scrollbar.lift(self._parent_canvas)
            else:
                self._scrollbar.grid_remove()
                # Tk can leave a removed widget mapped when this happens mid-animation; keep it under the canvas
                self._scrollbar.lower(self._parent_canvas)
            self.after(80, self._fit)  # showing or hiding the bar changes the width, check again once it settles
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------- icons
class Icon(tk.Canvas):
    """A bare outline glyph that follows the theme."""

    def __init__(self, master, name, size=20, color="text_2", bg="surface"):
        super().__init__(master, width=size, height=size, highlightthickness=0, bd=0)
        self.name, self.size, self.color, self.bg = name, size, color, bg
        theme.on_theme_change(self.redraw)
        self.redraw()

    def set(self, color=None, bg=None, name=None):
        self.color, self.bg, self.name = color or self.color, bg or self.bg, name or self.name
        self.redraw()

    def redraw(self):
        self.delete("all")
        bg = theme.c(self.bg) if self.bg in P else self.bg
        self.configure(background=bg)
        icons.draw(self, self.name, self.size / 2, self.size / 2, self.size * .9, theme.c(self.color), bg)


class IconBadge(tk.Canvas):
    """Icon on a tinted 8 px radius tile."""

    def __init__(self, master, name, size=36, tone="accent", bg="surface"):
        super().__init__(master, width=size, height=size, highlightthickness=0, bd=0)
        self.name, self.size, self.tone = name, size, tone
        self.bg = bg
        self.offset = 0.0
        theme.on_theme_change(self.redraw)
        self.redraw()

    def set_bg(self, color):
        """Colour behind the tile; pass a resolved colour (old API) or a token name."""
        self.bg = color
        self.redraw()

    def redraw(self):
        bg = theme.c(self.bg) if self.bg in P else self.bg
        self.configure(background=bg)
        icons.draw_badge(self, self.name, self.size, theme.c(self.tone), theme.c(soft(self.tone)), self.offset)

    def bounce(self):
        if theme.reduced_motion():
            return
        start = time.monotonic()

        def step():
            t = time.monotonic() - start
            if t > .35:
                self.offset = 0
                self.redraw()
                return
            self.offset = -math.sin(t / .35 * math.pi) * 3
            try:
                self.redraw()
            except tk.TclError:
                return
            self.after(16, step)
        step()


# ---------------------------------------------------------------------------- buttons
def primary_button(master, text, command, width=180, tone="accent", height=36):
    hover = {"accent": "accent_hover", "danger": "danger_hover"}.get(tone, tone)
    return ctk.CTkButton(master, text=text, command=command, width=width, height=height, corner_radius=8,
                         font=theme.font_style("body_strong"), fg_color=P[tone], hover_color=P[hover],
                         text_color=P["on_accent"], text_color_disabled=P["disabled"])


def secondary_button(master, text, command, width=120, height=36, text_tone="text"):
    return ctk.CTkButton(master, text=text, command=command, width=width, height=height, corner_radius=8,
                         font=theme.font_style("body_strong"), fg_color=P["surface"], hover_color=P["hover"],
                         text_color=P[text_tone], text_color_disabled=P["disabled"], border_width=1,
                         border_color=P["border_strong"])


def ghost_button(master, text, command, width=120, height=36):
    """Kept for older screens: a secondary (outlined) button."""
    return secondary_button(master, text, command, width=width, height=height)


def soft_button(master, text, command, width=120, height=32):
    return ctk.CTkButton(master, text=text, command=command, width=width, height=height, corner_radius=8,
                         font=theme.font_style("body_strong"), fg_color=P["accent_soft"], hover_color=P["selected"],
                         text_color=P["accent"])


def link_button(master, text, command, width=0, height=28, tone="accent", anchor="center"):
    """Text-only action (card header links, 'Open job history')."""
    return ctk.CTkButton(master, text=text, command=command, width=width, height=height, corner_radius=6,
                         font=theme.font_style("body_strong"), fg_color="transparent", hover_color=P["hover"],
                         text_color=P[tone], anchor=anchor)


def danger_button(master, text, command, width=180, height=36):
    return primary_button(master, text, command, width=width, tone="danger", height=height)


def segmented(master, values, command=None, height=32):
    return ctk.CTkSegmentedButton(master, values=values, command=command, height=height, corner_radius=8,
                                  font=theme.font_style("body_strong"))


# ---------------------------------------------------------------------------- pills
class Pill(ctk.CTkLabel):
    """Plain tag (FS type, SYSTEM, USB, PREVIEW)."""

    def __init__(self, master, text, tone="neutral"):
        super().__init__(master, text=text, font=theme.font(11, "bold"), corner_radius=6, height=20, padx=6,
                         fg_color=P[soft(tone)], text_color=P[tone if tone in P else "accent"])


STATUS_TONES = {
    "healthy": "success", "verified": "success", "done": "success", "online": "success", "linked": "success",
    "passed": "success", "bad sectors": "warn", "reboot pending": "warn", "waiting in usb mode": "warn",
    "update ready": "warn", "not administrator": "warn", "presenting": "warn", "system": "warn",
    "failed": "danger", "disk failing": "danger", "running": "info", "migrating": "info", "onboarding": "info",
    "busy": "info", "erasing": "info", "offline": "neutral", "cancelled": "neutral", "read only": "neutral",
    "this pc": "neutral", "usb": "neutral", "image": "neutral",
}


def status_tone(text: str) -> str:
    t = text.lower()
    if t in STATUS_TONES:
        return STATUS_TONES[t]
    return next((v for k, v in STATUS_TONES.items() if t.startswith(k)), "neutral")


class StatusPill(ctk.CTkFrame):
    """22 px pill with a dot and a fixed status word (never colour alone)."""

    def __init__(self, master, text, tone=None):
        tone = tone or status_tone(text)
        super().__init__(master, corner_radius=11, height=22, fg_color=P[soft(tone)])
        self.dot = ctk.CTkLabel(self, text="●", font=theme.font(8), text_color=P[tone], width=8, height=18)
        self.dot.pack(side="left", padx=(8, 4))
        self.lbl = ctk.CTkLabel(self, text=text, font=theme.font(11, "bold"), text_color=P[tone], height=18)
        self.lbl.pack(side="left", padx=(0, 9))

    def set(self, text, tone=None):
        tone = tone or status_tone(text)
        self.configure(fg_color=P[soft(tone)])
        self.dot.configure(text_color=P[tone])
        self.lbl.configure(text=text, text_color=P[tone])


# ---------------------------------------------------------------------------- cards and tiles
class Card(ctk.CTkFrame):
    """Surface with a 1 px border and an optional header (title, count, action link). Content goes in .body."""

    def __init__(self, master, title=None, action=None, count=None, pad=20, **kw):
        super().__init__(master, corner_radius=12, fg_color=P["surface"], border_width=1, border_color=P["border"],
                         **kw)
        self.head = None
        if title:
            self.head = ctk.CTkFrame(self, fg_color="transparent", height=36)
            self.head.pack(fill="x", padx=pad, pady=(14, 6))
            ctk.CTkLabel(self.head, text=title, font=theme.font_style("h3"), text_color=P["text"]).pack(side="left")
            if count is not None:
                ctk.CTkLabel(self.head, text=str(count), font=theme.font_style("small"), text_color=P["muted"]).pack(
                    side="left", padx=(8, 0), pady=(3, 0))
            if action:
                link_button(self.head, action[0], action[1]).pack(side="right")
        self.body = ctk.CTkFrame(self, fg_color="transparent")  # zero padding still keeps the border visible
        self.body.pack(fill="both", expand=True, padx=max(pad, 1), pady=(0 if title else max(pad, 10),
                                                                         max(pad, 10)))


class StatusTile(ctk.CTkFrame):
    """Home tile: label with a status dot, a big number, one line of context. Clicking opens the list behind it."""

    def __init__(self, master, label, value, sub="", tone="accent", command=None):
        super().__init__(master, corner_radius=12, fg_color=P["surface"], border_width=1, border_color=P["border"])
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(16, 0))
        ctk.CTkLabel(top, text="●", font=theme.font(9), text_color=P[tone], width=10, height=18).pack(side="left")
        ctk.CTkLabel(top, text=label, font=theme.font_style("body_strong"), text_color=P["text_2"],
                     height=18).pack(side="left", padx=(6, 0))
        self.value = ctk.CTkLabel(self, text=str(value), font=theme.font_style("metric"), text_color=P["text"],
                                  anchor="w", height=34)
        self.value.pack(fill="x", padx=20, pady=(6, 0))
        self.sub = ctk.CTkLabel(self, text=sub, font=theme.font_style("small"), text_color=P["muted"], anchor="w",
                                height=16)
        self.sub.pack(fill="x", padx=20, pady=(2, 16))
        if command:
            bind_all(self, "<Button-1>", lambda _e: command())
            bind_all(self, "<Enter>", lambda _e: self.configure(border_color=P["border_strong"]))
            bind_all(self, "<Leave>", lambda _e: self.configure(border_color=P["border"]))
            self.configure(cursor="hand2")


class QuickAction(ctk.CTkFrame):
    """Compact action tile for Home 'Start a job': icon tile plus a label on a well."""

    def __init__(self, master, icon, title, command, tone="accent"):
        super().__init__(master, corner_radius=8, fg_color=P["surface_2"], height=44)
        self.command = command
        self.badge = IconBadge(self, icon, 28, tone, bg="surface_2")
        self.badge.pack(side="left", padx=(10, 10), pady=8)
        self.t = ctk.CTkLabel(self, text=title, font=theme.font_style("body_strong"), text_color=P["text"],
                              anchor="w")
        self.t.pack(side="left", fill="x", expand=True)
        bind_all(self, "<Button-1>", lambda _e: self.command())
        bind_all(self, "<Enter>", lambda _e: self._hot(True))
        bind_all(self, "<Leave>", lambda _e: self._hot(False))

    def _hot(self, on):
        if not on:
            x, y = self.winfo_pointerxy()
            w = self.winfo_containing(x, y)
            while w is not None:
                if w is self:
                    return
                w = w.master
        self.configure(fg_color=P["hover"] if on else P["surface_2"])
        self.badge.set_bg("hover" if on else "surface_2")


class TaskCard(ctk.CTkFrame):
    """Compact 72 px task row card (icon tile, title, one-line description)."""

    def __init__(self, master, icon, title, desc, command, tone="accent"):
        super().__init__(master, corner_radius=12, fg_color=P["surface"], border_width=1, border_color=P["border"])
        self.command = command
        self.badge = IconBadge(self, icon, 36, tone)
        self.badge.grid(row=0, column=0, rowspan=2, padx=(16, 12), pady=16, sticky="n")
        self.t = ctk.CTkLabel(self, text=title, font=theme.font_style("body_strong"), text_color=P["text"], anchor="w",
                              height=18)
        self.t.grid(row=0, column=1, sticky="sw", padx=(0, 16), pady=(16, 0))
        self.d = ctk.CTkLabel(self, text=desc, font=theme.font_style("small"), text_color=P["muted"], anchor="nw",
                              justify="left", wraplength=250)
        self.d.grid(row=1, column=1, sticky="nw", padx=(0, 16), pady=(2, 14))
        self.grid_columnconfigure(1, weight=1)
        self.bind("<Configure>", lambda e: self.d.configure(wraplength=max(120, e.width - 90)), add="+")
        bind_all(self, "<Enter>", self._enter)
        bind_all(self, "<Leave>", self._leave)
        bind_all(self, "<Button-1>", lambda _e: self.command())

    def _enter(self, _e=None):
        self.configure(border_color=P["accent"])

    def _leave(self, e=None):
        x, y = self.winfo_pointerxy()
        w = self.winfo_containing(x, y)
        while w is not None:
            if w is self:
                return
            w = w.master
        self.configure(border_color=P["border"])


class OptionCard(ctk.CTkFrame):
    """Selectable option (radio-style)."""

    def __init__(self, master, title, desc, value, group, icon=None, tone="accent", badge_text=None):
        super().__init__(master, corner_radius=12, fg_color=P["surface"], border_width=1, border_color=P["border"])
        self.value, self.group = value, group
        group.append(self)
        self.enabled = True
        col = 0
        if icon:
            self.badge = IconBadge(self, icon, 36, tone)
            self.badge.grid(row=0, column=0, rowspan=2, padx=(16, 12), pady=16, sticky="n")
            col = 1
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=col, sticky="w", padx=(16 if not icon else 0, 16), pady=(14, 0))
        ctk.CTkLabel(head, text=title, font=theme.font_style("body_strong"), text_color=P["text"],
                     height=20).pack(side="left")
        if badge_text:
            Pill(head, badge_text, "accent").pack(side="left", padx=8)
        self.desc = ctk.CTkLabel(self, text=desc, font=theme.font_style("small"), text_color=P["muted"],
                                 justify="left", anchor="w", wraplength=330)
        self.desc.grid(row=1, column=col, sticky="w", padx=(16 if not icon else 0, 16), pady=(2, 14))
        self.grid_columnconfigure(col, weight=1)
        bind_all(self, "<Button-1>", lambda _e: self.select())
        self.on_select = None

    def select(self):
        if not self.enabled:
            return
        for o in self.group:
            o.selected = o is self
            o.configure(border_color=P["accent"] if o is self else P["border"],
                        fg_color=P["selected"] if o is self else P["surface"])
            if getattr(o, "badge", None):
                o.badge.set_bg("selected" if o is self else "surface")
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


# ---------------------------------------------------------------------------- table
class DataTable(ctk.CTkFrame):
    """Themed ttk.Treeview with a header row, hover, selection, thin scrollbar and an empty state.

    columns: [(key, title, width, anchor)], anchor optional ('w', 'e', 'center'); the first column stretches."""

    def __init__(self, master, columns, height=8, on_open=None, on_select=None, empty=None):
        super().__init__(master, fg_color="transparent")
        self.columns = columns
        keys = [c[0] for c in columns]
        self.tree = ttk.Treeview(self, columns=keys, show="headings", style="Smith.Treeview", height=height,
                                 selectmode="browse")
        for i, col in enumerate(columns):
            key, title, width = col[0], col[1], col[2]
            anchor = col[3] if len(col) > 3 else "w"
            self.tree.heading(key, text=title.upper(), anchor=anchor)
            self.tree.column(key, width=width, minwidth=40, anchor=anchor, stretch=i == 0)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview, style="Smith.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=self._scroll)
        self.tree.pack(side="left", fill="both", expand=True)
        self.rows: dict = {}
        self._hover = None
        self._empty_spec = empty
        self._empty = None
        self.tree.bind("<Motion>", self._motion, add="+")
        self.tree.bind("<Leave>", lambda _e: self._set_hover(None), add="+")
        if on_open:
            self.tree.bind("<Double-1>", lambda _e: self._fire(on_open), add="+")
            self.tree.bind("<Return>", lambda _e: self._fire(on_open), add="+")
        if on_select:
            self.tree.bind("<<TreeviewSelect>>", lambda _e: self._fire(on_select), add="+")
        theme.on_theme_change(self._tags)
        self._tags()

    def _scroll(self, a, b):
        self.sb.set(a, b)
        if float(a) <= 0 and float(b) >= 1:
            self.sb.pack_forget()
        elif not self.sb.winfo_ismapped():
            self.sb.pack(side="right", fill="y", padx=(0, 2), before=self.tree)

    def _tags(self):
        self.tree.tag_configure("hover", background=theme.c("hover"))
        for tone in ("success", "warn", "danger", "info", "muted"):
            self.tree.tag_configure(tone, foreground=theme.c(tone))

    def _fire(self, fn):
        sel = self.tree.selection()
        if sel and sel[0] in self.rows:
            fn(self.rows[sel[0]])

    def _motion(self, e):
        self._set_hover(self.tree.identify_row(e.y) or None)

    def _set_hover(self, iid):
        if iid == self._hover:
            return
        for i in (self._hover, iid):
            if i and self.tree.exists(i):
                tags = [t for t in self.tree.item(i, "tags") if t != "hover"]
                if i == iid:
                    tags.append("hover")
                self.tree.item(i, tags=tags)
        self._hover = iid

    def clear(self):
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        self._hover = None

    def add(self, values, data=None, tags=()):
        # Treeview cells have no padding option: a little space lines the text up with the headings
        values = [f"  {v}" if self.tree.column(k, "anchor") == "w" else f"{v}  "
                  for v, k in zip(values, [c[0] for c in self.columns])]
        iid = self.tree.insert("", "end", values=values, tags=tags)
        self.rows[iid] = data if data is not None else values
        return iid

    def done(self):
        """Call after filling: shows the empty state when there are no rows."""
        if self._empty is not None:
            self._empty.destroy()
            self._empty = None
        if not self.rows and self._empty_spec:
            title, text = self._empty_spec[:2]
            self._empty = EmptyState(self.tree, title, text, *self._empty_spec[2:])
            self._empty.place(relx=.5, rely=.55, anchor="center")


# ---------------------------------------------------------------------------- empty and loading states
class EmptyState(ctk.CTkFrame):
    """Centred: Mossbit (Subtle and Full) or an outline icon (Off), a title, one line and one action."""

    def __init__(self, master, title, text, action=None, icon="jobs", bg="surface"):
        super().__init__(master, fg_color=P[bg])
        if theme.effective_personality() != "Off":
            from .mascot import MossbitView
            MossbitView(self, "idle", 1, bg=bg).pack(pady=(0, 8))
        else:
            Icon(self, icon, 44, "muted", bg).pack(pady=(0, 8))
        ctk.CTkLabel(self, text=title, font=theme.font_style("h3"), text_color=P["text"], height=20).pack()
        ctk.CTkLabel(self, text=text, font=theme.font_style("small"), text_color=P["muted"], wraplength=360,
                     justify="center").pack(pady=(2, 0))
        if action:
            primary_button(self, action[0], action[1], width=0, height=32).pack(pady=(12, 0))


class Skeleton(tk.Canvas):
    """Loading placeholder: rows of quiet bars at the real row height with a slow shimmer."""

    def __init__(self, master, rows=4, row_height=44, bg="surface"):
        super().__init__(master, height=rows * row_height, highlightthickness=0, bd=0)
        self.rows, self.rh, self.bg_token = rows, row_height, bg
        self.t0 = time.monotonic()
        self._tick()

    def _tick(self):
        try:
            self._draw()
        except tk.TclError:
            return
        if not theme.reduced_motion():
            self.after(50, self._tick)

    def _draw(self):
        self.delete("all")
        w = max(100, self.winfo_width())
        self.configure(background=theme.c(self.bg_token))
        base = theme.c("surface_2")
        band = theme.lerp_color(base, theme.c("surface"), .6)
        phase = ((time.monotonic() - self.t0) % 1.2) / 1.2
        bx = -200 + (w + 400) * phase
        for r in range(self.rows):
            y = r * self.rh + self.rh / 2
            for x0, x1 in ((16, 16 + w * .3), (w * .42, w * .58), (w * .7, w * .82)):
                self.create_rectangle(x0, y - 6, x1, y + 6, fill=base, outline="")
                lo, hi = max(x0, bx), min(x1, bx + 120)
                if hi > lo:
                    self.create_rectangle(lo, y - 6, hi, y + 6, fill=band, outline="")


# ---------------------------------------------------------------------------- popovers and toasts
def _install_overlay_handlers(root):
    """One root-level click and Escape handler closes whatever popover or palette is open."""
    if getattr(root, "_smith_overlays", None) is not None:
        return
    root._smith_overlays = []

    def inside(w, box):
        while w is not None:
            if w is box:
                return True
            w = getattr(w, "master", None)
        return False

    def click(e):
        for ov in list(root._smith_overlays):
            try:
                hit = root.winfo_containing(e.x_root, e.y_root)
            except (tk.TclError, KeyError):
                hit = None
            if not inside(hit, ov) and not inside(hit, getattr(ov, "anchor", None)):
                ov.close()

    def esc(_e):
        if root._smith_overlays:
            root._smith_overlays[-1].close()
            return "break"
    root.bind("<Button-1>", click, add="+")
    root.bind("<Escape>", esc, add="+")


def place_sized(widget, x, y, width, height=None):
    """Place a CustomTkinter widget at a fixed width. CTk refuses width in place(), but redraws to whatever size
    Tk gives it, so go through Tk's own place."""
    kw = dict(x=x, y=y, width=width)
    if height is not None:
        kw["height"] = height
    tk.Frame.place_configure(widget, **kw)


class Shadow:
    """Soft shadow behind a placed overlay: a few rounded layers that fade into the canvas colour."""

    def __init__(self, root, radius=12, layers=(8, 5, 2)):
        self.root = root
        self.frames = []
        for i, spread in enumerate(layers):
            f = ctk.CTkFrame(root, corner_radius=radius + spread, border_width=0)
            f.spread = spread
            f.strength = (i + 1) / len(layers)
            self.frames.append(f)
        self.color()

    def color(self):
        bg = theme.c("bg")
        dark = theme.is_dark()
        for f in self.frames:
            f.configure(fg_color=theme.lerp_color(bg, "#000000", (.2 if dark else .045) * f.strength))

    def place(self, x, y, w, h):
        for f in self.frames:
            s = f.spread
            place_sized(f, x - s, y - s + s // 2 + 2, w + 2 * s, h + 2 * s)
            f.lift()

    def destroy(self):
        for f in self.frames:
            f.destroy()


class Popover(ctk.CTkFrame):
    """Raised menu under an anchor widget. items: [(label, command, icon, hint)] or None for a divider;
    or pass build=fn(body) to fill it yourself."""

    def __init__(self, anchor, items=None, width=260, build=None, align="left"):
        root = anchor.winfo_toplevel()
        _install_overlay_handlers(root)
        for ov in list(root._smith_overlays):
            ov.close()
        super().__init__(root, corner_radius=12, fg_color=P["raised"], border_width=1,
                         border_color=P["border_strong"], width=width)
        self.root, self.anchor = root, anchor
        self.shadow = Shadow(root)
        if build:
            build(self)
        for it in items or []:
            if it is None:
                divider(self).pack(fill="x", padx=8, pady=4)
                continue
            if isinstance(it, str):  # short text is a group caption, longer text a note
                if len(it) <= 28:
                    caption(self, it).pack(fill="x", padx=14, pady=(8, 2))
                else:
                    ctk.CTkLabel(self, text=it, font=theme.font_style("small"), text_color=P["muted"], anchor="w",
                                 justify="left", wraplength=width - 32).pack(fill="x", padx=14, pady=(6, 4))
                continue
            label, cmd, icon, hint = (list(it) + [None, None])[:4]
            self.row(label, cmd, icon, hint)
        root.update_idletasks()
        x = anchor.winfo_rootx() - root.winfo_rootx()
        y = anchor.winfo_rooty() - root.winfo_rooty() + anchor.winfo_height() + 6
        h = max(40, self.winfo_reqheight())
        if align == "right":
            x = x + anchor.winfo_width() - width
        x = max(8, min(x, root.winfo_width() - width - 8))
        self.shadow.place(x, y, width, h)
        place_sized(self, x, y, width)
        self.lift()
        root._smith_overlays.append(self)

    def row(self, label, cmd, icon=None, hint=None, enabled=True):
        b = ctk.CTkFrame(self, fg_color="transparent", corner_radius=8, height=34)
        b.pack(fill="x", padx=6, pady=1)
        if icon:
            Icon(b, icon, 16, "text_2" if enabled else "disabled", "raised").pack(side="left", padx=(10, 0))
        ctk.CTkLabel(b, text=label, font=theme.font_style("body"), anchor="w", height=30,
                     text_color=P["text"] if enabled else P["disabled"]).pack(side="left", padx=10, fill="x",
                                                                              expand=True)
        if hint:
            ctk.CTkLabel(b, text=hint, font=theme.font_style("small"), text_color=P["muted"], height=30).pack(
                side="right", padx=10)
        if not enabled:
            return b

        def hot(on):
            b.configure(fg_color=P["hover"] if on else "transparent")
            for w in b.winfo_children():
                if isinstance(w, Icon):
                    w.set(bg="hover" if on else "raised")

        def fire(_e=None):
            self.close()
            cmd()
        bind_all(b, "<Enter>", lambda _e: hot(True))
        bind_all(b, "<Leave>", lambda _e: hot(False))
        bind_all(b, "<Button-1>", fire)
        return b

    def close(self):
        try:
            self.root._smith_overlays.remove(self)
        except (ValueError, AttributeError):
            pass
        self.shadow.destroy()
        self.destroy()


class Toast(ctk.CTkFrame):
    """Raised toast, bottom right: icon tile and text. Stacks up to three, each stays about four seconds."""

    _stack: list = []

    def __init__(self, root, text, tone="success"):
        super().__init__(root, corner_radius=12, fg_color=P["raised"], border_width=1, border_color=P["border_strong"])
        icon = {"success": "check", "warn": "warn", "danger": "warn", "info": "clock"}.get(tone, "check")
        IconBadge(self, icon, 28, tone if tone in P else "accent", bg="raised").pack(side="left", padx=(12, 10),
                                                                                    pady=10)
        ctk.CTkLabel(self, text=text, font=theme.font_style("body"), text_color=P["text"], wraplength=320,
                     justify="left", anchor="w").pack(side="left", padx=(0, 16), pady=10)
        live = [t for t in Toast._stack if t.winfo_exists()]
        for t in live[:-2]:
            t.destroy()
        Toast._stack = [t for t in live if t.winfo_exists()] + [self]
        self._restack()
        self.after(4200, self._gone)

    def _restack(self):
        y = -24
        for t in reversed(Toast._stack):
            try:
                t.place(relx=1.0, rely=1.0, x=-24, y=y, anchor="se")
                t.lift()
                t.update_idletasks()
                y -= t.winfo_reqheight() + 10
            except tk.TclError:
                pass

    def _gone(self):
        try:
            Toast._stack.remove(self)
        except ValueError:
            pass
        try:
            self.destroy()
        except tk.TclError:
            return
        self._restack()


# ---------------------------------------------------------------------------- drive picker
class DrivePicker(AutoScroll):
    """Lists disks and their partitions as selectable rows.

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
            EmptyState(self, "No drives found", "Run as administrator to see physical drives, or drop a disk image "
                                                "(.img, .vhd) onto the window.", bg="bg").pack(pady=30)

        def is_usb(d):
            return d.removable or d.bus in ("USB", "SD", "MMC")
        if writes:  # external drives first when choosing somewhere to write
            inventory = sorted(inventory, key=lambda x: not is_usb(x[0]))
        for dev, pt, err in inventory:
            if exclude_path and dev.path == exclude_path:
                continue
            disk_disabled = None
            if writes and dev.is_system:
                disk_disabled = "Windows runs from this disk, so it is protected"
            sub = f"{dev.bus} · {pt.scheme if pt else '?'} · {len(pt.partitions) if pt else 0} partition(s)"
            if dev.serial:
                sub += f" · S/N {theme.mask(dev.serial)}"
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
                              human_size(p.size_bytes(dev.sector_size)), selectable=not locked, indent=28,
                              usage=usage, fs=p.fs, disabled_text="Protected (system disk)" if locked else None)

    def _row(self, target, icon, title, sub, size, selectable, indent=0, tag=None, usage=None, fs=None,
             disabled_text=None):
        card = ctk.CTkFrame(self, corner_radius=10, fg_color=P["surface"], border_width=1, border_color=P["border"])
        card.pack(fill="x", padx=(indent, 8), pady=3)
        badge = IconBadge(card, icon, 32, "accent" if target["kind"] == "disk" else "info")
        badge.grid(row=0, column=0, rowspan=2, padx=(12, 12), pady=10)
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.grid(row=0, column=1, sticky="w", pady=(10, 0))
        ctk.CTkLabel(top, text=title, font=theme.font_style("body_strong"), text_color=P["text"], height=18).pack(
            side="left")
        if fs:
            Pill(top, fs, "neutral").pack(side="left", padx=6)
        if tag:
            Pill(top, tag, "warn" if tag == "SYSTEM" else "neutral").pack(side="left", padx=6)
        ctk.CTkLabel(card, text=disabled_text or sub, font=theme.font_style("small"), height=16,
                     text_color=P["warn"] if disabled_text else P["muted"]).grid(row=1, column=1, sticky="w",
                                                                                 pady=(2, 10))
        ctk.CTkLabel(card, text=size, font=theme.font_style("body_strong"), text_color=P["text"]).grid(
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
                c2.configure(border_color=P["border"], fg_color=P["surface"])
                b2.set_bg("surface")
            card.configure(border_color=P["accent"], fg_color=P["selected"])
            badge.set_bg("selected")
            self.selected = target
            if self.on_select:
                self.on_select(target)

        def enter(_e=None):
            if self.selected is not target:
                card.configure(border_color=P["border_strong"])

        def leave(_e=None):
            if self.selected is not target:
                card.configure(border_color=P["border"])
        card.click = click
        bind_all(card, "<Button-1>", click)
        card.bind("<Enter>", enter)
        card.bind("<Leave>", leave)


# ---------------------------------------------------------------------------- progress
class ProgressPanel(ctk.CTkFrame):
    def __init__(self, master, on_cancel):
        super().__init__(master, fg_color="transparent")
        from .mascot import SweepTrack
        self.ring = SweepTrack(self, width=720)
        self.ring.pack(pady=(4, 0), fill="x")
        self.label = ctk.CTkLabel(self, text="", font=theme.font_style("h3"), text_color=P["text"])
        self.label.pack(pady=(6, 0))
        self.detail = ctk.CTkLabel(self, text="", font=theme.font_style("small"), text_color=P["muted"],
                                   wraplength=560)
        self.detail.pack(pady=(2, 12))
        chips = ctk.CTkFrame(self, fg_color="transparent")
        chips.pack()
        self.chips = {}
        for k, title in (("speed", "Speed"), ("eta", "Time left"), ("done", "Processed"), ("elapsed", "Elapsed")):
            f = ctk.CTkFrame(chips, corner_radius=10, fg_color=P["surface"], border_width=1, border_color=P["border"])
            f.pack(side="left", padx=5)
            caption(f, title).pack(padx=16, pady=(10, 0))
            v = ctk.CTkLabel(f, text="-", font=theme.font_style("h3"), text_color=P["text"],
                             width=96)
            v.pack(padx=16, pady=(0, 8))
            self.chips[k] = v
        self.cancel = secondary_button(self, "Cancel", on_cancel, width=120, text_tone="danger")
        self.cancel.pack(pady=18)

    def update_from(self, s: dict):
        self.ring.set(s["pct"] / 100)
        self.label.configure(text=s["label"])
        self.detail.configure(text=s["detail"])
        self.chips["speed"].configure(text=f"{human_size(s['speed'])}/s" if s["speed"] else "-")
        self.chips["eta"].configure(text=human_time(s["eta"]))
        self.chips["done"].configure(text=human_size(s["done"]) if s["total"] > 4096 else f"{s['done']}/{s['total']}")
        self.chips["elapsed"].configure(text=human_time(s["elapsed"]))


class DropZone(ctk.CTkFrame):
    def __init__(self, master, text="Drop files or folders here", sub="or use the buttons below", height=150,
                 icon="upload", tone="accent", wide=False):
        super().__init__(master, corner_radius=12, fg_color=P["surface"], border_width=1,
                         border_color=P["border_strong"], height=height)
        self.pack_propagate(False)
        self.grid_propagate(False)
        self.badge = IconBadge(self, icon, 40, tone)
        title = ctk.CTkLabel(self, text=text, font=theme.font_style("h3"), text_color=P["text"])
        subl = ctk.CTkLabel(self, text=sub, font=theme.font_style("small"), text_color=P["muted"])
        if wide:
            # badge beside the text, so a short strip across the page doesn't clip the second line
            self.grid_columnconfigure((0, 3), weight=1)
            self.grid_rowconfigure((0, 1), weight=1)
            self.badge.grid(row=0, column=1, rowspan=2, padx=(0, 16))
            title.grid(row=0, column=2, sticky="sw")
            subl.grid(row=1, column=2, sticky="nw")
        else:
            self.badge.pack(pady=(20, 6))
            title.pack()
            subl.pack()

    def hot(self, on: bool):
        self.configure(border_color=P["accent"] if on else P["border_strong"],
                       fg_color=P["accent_soft"] if on else P["surface"])
        self.badge.set_bg("accent_soft" if on else "surface")
        if on:
            self.badge.bounce()


# ---------------------------------------------------------------------------- typed confirmation
class TypedConfirm(ctk.CTkFrame):
    """The serious last step before anything permanent. The phrase is shown in mono with no copy button, paste is
    blocked and Enter does nothing, so the operator has to read and type it."""

    def __init__(self, master, phrase, on_change):
        super().__init__(master, corner_radius=12, fg_color=P["danger_soft"], border_width=2, border_color=P["danger"])
        self.phrase = phrase
        ctk.CTkLabel(self, text="Type the phrase below to confirm", font=theme.font_style("h3"), text_color=P["text"],
                     anchor="w").pack(anchor="w", padx=22, pady=(18, 0))
        ctk.CTkLabel(self, text="The final button unlocks only when it matches exactly.",
                     font=theme.font_style("small"), text_color=P["text_2"], anchor="w").pack(anchor="w", padx=22)
        self.chip = ctk.CTkLabel(self, text=f"  {phrase}  ", font=theme.mono(13, "bold"), text_color=P["danger"],
                                 fg_color=P["surface"], corner_radius=6, height=30)
        self.chip.pack(anchor="w", padx=22, pady=(10, 10))
        self.var = tk.StringVar()
        self.entry = ctk.CTkEntry(self, textvariable=self.var, height=42, corner_radius=8, font=theme.mono(14),
                                  border_width=2, border_color=P["danger"], fg_color=P["surface"],
                                  text_color=P["text"])
        self.entry.pack(fill="x", padx=22, pady=(0, 4))
        self.left = ctk.CTkLabel(self, text="", font=theme.font_style("small"), text_color=P["danger"], anchor="w")
        self.left.pack(anchor="w", padx=22, pady=(0, 14))
        for seq in ("<<Paste>>", "<Control-v>", "<Control-V>", "<Shift-Insert>", "<Button-2>", "<Return>",
                    "<KP_Enter>"):
            self.entry._entry.bind(seq, lambda _e: "break")
        self.var.trace_add("write", lambda *_: self._changed(on_change))
        self._changed(on_change)

    def _changed(self, on_change):
        typed = self.var.get().strip().upper()
        ok = typed == self.phrase.upper()
        if ok:
            self.left.configure(text="Matches", text_color=P["success"])
        else:
            n = max(0, len(self.phrase) - len(typed))
            self.left.configure(text=f"{n} character{'s' if n != 1 else ''} to go" if n else "Does not match yet",
                                text_color=P["danger"])
        on_change(ok)
