"""Window chrome: the category rail, the sub-nav panel and the top context bar (client, ticket, breadcrumbs, search,
running jobs, presentation and admin status)."""
from __future__ import annotations

import os
import random
import tkinter as tk

import customtkinter as ctk

from ..util import is_admin
from . import icons, mark, nav, theme
from .widgets import Icon, Popover, StatusPill, bind_all, caption, divider, focusable, link_button

P = theme.PALETTE


def technician() -> str:
    from .guide import load_settings
    name = load_settings().get("technician") or os.environ.get("USERNAME") or os.environ.get("USER") or ""
    return name if name.lower() not in ("root", "admin", "administrator") else ""


def branding() -> dict:
    """The MSP's name and logo for wipe certificates (Settings, Branding)."""
    from .guide import load_settings
    s = load_settings()
    return {"company": s.get("brand_company") or "", "logo": s.get("brand_logo") or ""}


def initials(name: str) -> str:
    parts = [p for p in name.replace(".", " ").replace("_", " ").split() if p]
    return ("".join(p[0] for p in parts[:2]) or "?").upper()


def machine_status(m) -> str:
    if m.is_local:
        return "This PC"
    lock = getattr(m, "lock", None)
    if lock is not None and lock.locked():
        return "Busy"
    return "Linked"


class Clickable:
    """Hover and click handling for small frame-based buttons."""

    def wire(self, command, base="transparent", hot="hover"):
        self._base, self._hot = base, hot
        bind_all(self, "<Enter>", lambda _e: self._paint(True))
        bind_all(self, "<Leave>", lambda _e: self._paint(False))
        bind_all(self, "<Button-1>", lambda _e: command())
        self.configure(cursor="hand2")
        focusable(self, command)

    def _paint(self, on):
        if not on:
            x, y = self.winfo_pointerxy()
            w = self.winfo_containing(x, y)
            while w is not None:
                if w is self:
                    return
                w = w.master
        col = self._hot if on else self._base
        self.configure(fg_color=P[col] if col in P else col)
        for w in self.winfo_children():
            if isinstance(w, Icon):
                w.set(bg=col if col in P else self.bg_token)


class IconButton(ctk.CTkFrame, Clickable):
    def __init__(self, master, icon, command, size=32, bg="bg", color="text_2"):
        super().__init__(master, width=size, height=size, corner_radius=8, fg_color=P[bg])
        self.bg_token = bg
        self.pack_propagate(False)
        Icon(self, icon, 18, color, bg).place(relx=.5, rely=.5, anchor="center")
        self.wire(command, base=bg)


class Chip(ctk.CTkFrame, Clickable):
    """Rounded chip with an optional icon, used in the context bar."""

    def __init__(self, master, text, command=None, icon=None, tone=None, mono=False, bg="bg"):
        fill = (tone + "_soft") if tone else "surface"
        super().__init__(master, corner_radius=8, height=34, fg_color=P[fill], border_width=0 if tone else 1,
                         border_color=P["border"])
        self.bg_token = fill
        self.icon = None
        if icon:
            self.icon = Icon(self, icon, 16, tone or "text_2", fill)
            self.icon.pack(side="left", padx=(10, 0))
        self.lbl = ctk.CTkLabel(self, text=text, height=28, text_color=P[tone or "text"],
                                font=theme.mono(12) if mono else theme.font_style("body_strong"))
        self.lbl.pack(side="left", padx=10, pady=3)
        self.extra = None
        if command:
            self.wire(command, base=fill, hot="hover" if not tone else fill)

    def set(self, text):
        self.lbl.configure(text=text)


class ProductMark(tk.Canvas):
    """The product mark (see mark.py): indigo tile, a disk with one sector struck out, on an anvil. The same in
    both themes, like the app icon."""

    def __init__(self, master, size=36, bg="rail"):
        super().__init__(master, width=size, height=size, highlightthickness=0, bd=0)
        self.size, self.bg = size, bg
        theme.on_theme_change(self.redraw)
        self.redraw()

    def redraw(self):
        s = self.size
        self.delete("all")
        self.configure(background=theme.c(self.bg))
        icons.rounded_rect(self, 0, 0, s, s, s * mark.CORNER, fill=mark.INDIGO, outline="")
        fg = "#FFFFFF"
        disk = mark.DISK_SMALL if mark.simple(s) else mark.DISK
        if not mark.simple(s):
            self.create_polygon([v * s for pt in mark.ANVIL for v in pt], fill=fg, outline="")
        cx, cy, r = disk["cx"] * s, disk["cy"] * s, disk["r"] * s
        w = disk["ring"] * s
        # Tk centres the outline on the oval's edge, so pull it in by half the width to match the icon files
        self.create_oval(cx - r + w / 2, cy - r + w / 2, cx + r - w / 2, cy + r - w / 2, outline=fg, width=w)
        start, extent = disk["sector"]
        self.create_arc(cx - r, cy - r, cx + r, cy + r, start=start, extent=extent, style="pieslice", fill=fg,
                        outline="")
        h = disk["hole"] * s
        self.create_oval(cx - h, cy - h, cx + h, cy + h, fill=mark.INDIGO, outline="")


# ---------------------------------------------------------------------------- rail
class RailItem(ctk.CTkFrame, Clickable):
    def __init__(self, master, cat, command):
        super().__init__(master, width=62, height=58, corner_radius=10, fg_color=P["rail"])
        self.bg_token = "rail"
        self.pack_propagate(False)
        self.icon = Icon(self, cat.icon, 20, "text_2", "rail")
        self.icon.pack(pady=(9, 2))
        self.lbl = ctk.CTkLabel(self, text=cat.label, font=theme.font(11), text_color=P["text_2"], height=14)
        self.lbl.pack()
        self.selected = False
        self.wire(command, base="rail")

    def set_selected(self, on):
        self.selected = on
        self._base = "selected" if on else "rail"
        self.bg_token = self._base
        self.configure(fg_color=P[self._base])
        self.icon.set(color="accent" if on else "text_2", bg=self._base)
        self.lbl.configure(text_color=P["accent"] if on else P["text_2"],
                           font=theme.font(11, "bold" if on else "normal"))


class Rail(ctk.CTkFrame):
    WIDTH = 76

    def __init__(self, master, app):
        super().__init__(master, width=self.WIDTH, corner_radius=0, fg_color=P["rail"])
        self.app = app
        self.grid_propagate(False)
        self.pack_propagate(False)
        divider(self, vertical=True).place(relx=1, x=-1, y=0, relheight=1)
        mark = ProductMark(self)
        mark.pack(pady=(14, 12))
        mark.bind("<Button-1>", lambda _e: app.open_category("home"))
        self.items = {}
        groups = [["home"], ["recover", "erase", "drives"], ["machines", "manage"], ["jobs"]]
        for gi, keys in enumerate(groups):
            if gi:
                sep = divider(self)
                sep.configure(width=36)
                sep.pack(pady=6)
            for k in keys:
                it = RailItem(self, nav.BY_KEY[k], lambda k=k: app.open_category(k))
                it.pack(pady=2)
                self.items[k] = it
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(side="bottom", pady=(0, 14))
        self.items["settings"] = RailItem(bottom, nav.SETTINGS, lambda: app.open_category("settings"))
        self.items["settings"].pack(pady=(0, 8))
        self.avatar = ctk.CTkLabel(bottom, text=initials(technician() or "Technician"), width=32, height=32,
                                   corner_radius=16, fg_color=P["accent_soft"], text_color=P["accent"],
                                   font=theme.font(12, "bold"), cursor="hand2")
        self.avatar.pack()
        self.avatar.bind("<Button-1>", lambda _e: app.open_category("settings"))
        self.bar = ctk.CTkFrame(self, width=3, height=30, corner_radius=2, fg_color=P["accent"])
        self.key = None
        self.bind("<Configure>", lambda _e: self._put_bar(), add="+")

    def select(self, key):
        self.key = key
        for k, it in self.items.items():
            it.set_selected(k == key)
        self.after_idle(self._put_bar)

    def _put_bar(self):
        """The accent bar on the rail's left edge, level with the selected item."""
        it = self.items.get(self.key)
        try:
            if it is None or not it.winfo_ismapped():
                self.bar.place_forget()
                return
            y = it.winfo_rooty() - self.winfo_rooty() + it.winfo_height() // 2 - 15
            self.bar.place(x=0, y=y)
        except tk.TclError:
            pass  # rail item destroyed mid-redraw


# ---------------------------------------------------------------------------- sub-nav
class NavRow(ctk.CTkFrame, Clickable):
    def __init__(self, master, item, command, count=None):
        super().__init__(master, height=34, corner_radius=8, fg_color=P["subnav"])
        self.bg_token = "subnav"
        self.item = item
        self.icon = Icon(self, item.icon, 16, "text_2", "subnav")
        self.icon.pack(side="left", padx=(10, 10))
        self.lbl = ctk.CTkLabel(self, text=item.label, font=theme.font_style("body"), text_color=P["text"],
                                anchor="w", height=30)
        self.lbl.pack(side="left", fill="x", expand=True)
        if count is not None:
            ctk.CTkLabel(self, text=str(count), font=theme.font_style("small"), text_color=P["muted"],
                         height=30).pack(side="right", padx=10)
        self.wire(command, base="subnav")

    def set_selected(self, on):
        self._base = "selected" if on else "subnav"
        self.bg_token = self._base
        self.configure(fg_color=P[self._base])
        self.icon.set(color="accent" if on else "text_2", bg=self._base)
        self.lbl.configure(text_color=P["accent"] if on else P["text"],
                           font=theme.font_style("body_strong" if on else "body"))


class SubNav(ctk.CTkFrame):
    WIDTH = 236

    def __init__(self, master, app):
        super().__init__(master, width=self.WIDTH, corner_radius=0, fg_color=P["subnav"])
        self.app = app
        self.grid_propagate(False)
        self.pack_propagate(False)
        self.cat = None
        self.rows = {}
        divider(self, vertical=True).place(relx=1, x=-1, y=0, relheight=1)
        self.top = ctk.CTkFrame(self, fg_color="transparent")
        self.top.pack(fill="x", padx=(10, 11), pady=(16, 0))
        self.bottom = ctk.CTkFrame(self, fg_color="transparent")
        self.bottom.pack(side="bottom", fill="x", padx=(10, 11), pady=(0, 14))

    def show(self, cat_key, item_key):
        if cat_key != self.cat:
            self.cat = cat_key
            self._build()
        for k, r in self.rows.items():
            r.set_selected(k == item_key)

    def _build(self):
        for w in self.top.winfo_children():
            w.destroy()
        self.rows = {}
        cat = nav.BY_KEY[self.cat]
        ctk.CTkLabel(self.top, text=cat.title, font=theme.font_style("h2"), text_color=P["text"], anchor="w",
                     height=24).pack(fill="x", padx=8)
        ctk.CTkLabel(self.top, text=cat.desc, font=theme.font_style("small"), text_color=P["muted"], anchor="w",
                     justify="left", wraplength=200).pack(fill="x", padx=8, pady=(0, 12))
        for cap, its in cat.groups:
            if cap:
                caption(self.top, cap).pack(fill="x", padx=8, pady=(12, 4))
            for it in its:
                r = NavRow(self.top, it, lambda c=cat, i=it: self.app.open_item(c, i))
                r.pack(fill="x", pady=1)
                self.rows[it.key] = r
        self.refresh_bottom()

    def refresh_bottom(self):
        for w in self.bottom.winfo_children():
            w.destroy()
        cat = nav.BY_KEY.get(self.cat)
        if theme.effective_personality() == "Full":
            self._companion()
        if cat is not None and cat.working_on:
            self._machines()

    def _companion(self):
        from .mascot import LINES, Mascot, SpeechBubble
        bubble = SpeechBubble(self.bottom)
        bubble.pack(fill="x", padx=4)
        bubble.say(random.choice(LINES["hello"]))
        m = Mascot(self.bottom, bubble, width=self.WIDTH - 24, scale=2, bg="subnav")
        m.pack(pady=(4, 10))
        self.app.mascot.attach(m)

    def _machines(self):
        ms = self.app.machines()
        caption(self.bottom, "Working on").pack(fill="x", padx=8, pady=(0, 6))
        card = ctk.CTkFrame(self.bottom, corner_radius=10, fg_color=P["surface"], border_width=1,
                            border_color=P["border"])
        card.pack(fill="x")
        for i, m in enumerate(ms[:4]):
            if i:
                divider(card).pack(fill="x", padx=10)
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=8)
            Icon(row, "pc", 18, "text_2", "surface").pack(side="left", padx=(0, 10))
            t = ctk.CTkFrame(row, fg_color="transparent")
            t.pack(side="left", fill="x", expand=True)
            info = m.info() if m.is_local else m.info_cache
            host = info.get("hostname", "") if isinstance(info, dict) else ""
            name = "This PC" if m.is_local else m.label
            sub = host if m.is_local else getattr(m, "address", "")
            ctk.CTkLabel(t, text=name, font=theme.font_style("body_strong"), text_color=P["text"], anchor="w",
                         height=16).pack(fill="x")
            ctk.CTkLabel(t, text=sub, font=theme.font_style("small"), text_color=P["muted"], anchor="w",
                         height=14).pack(fill="x")
            if not m.is_local:
                StatusPill(row, machine_status(m)).pack(side="right")
        if len(ms) > 4:
            link_button(card, f"{len(ms) - 4} more", lambda: self.app.open_category("machines"),
                        anchor="w").pack(fill="x", padx=6, pady=(0, 6))
        link_button(self.bottom, "+ Connect a machine", self.app.open_connect, anchor="w").pack(fill="x",
                                                                                               pady=(6, 0))


# ---------------------------------------------------------------------------- context bar
class ContextBar(ctk.CTkFrame):
    HEIGHT = 56

    def __init__(self, master, app):
        super().__init__(master, height=self.HEIGHT, corner_radius=0, fg_color=P["bg"])
        self.app = app
        self.pack_propagate(False)
        self.edge = divider(self)
        self.edge.place(x=0, rely=1, y=-1, relwidth=1)
        left = ctk.CTkFrame(self, fg_color="transparent")
        left.pack(side="left", fill="y", padx=(20, 0))
        self.client = Chip(left, "All clients", self.client_menu, icon="building")
        self.client.pack(side="left", pady=11)
        self.client_chev = Icon(self.client, "chevron_down", 14, "muted", "surface")
        self.client_chev.pack(side="left", padx=(0, 10))
        self.ticket = Chip(left, "+ Ticket", self.ticket_menu, mono=False)
        self.ticket.pack(side="left", padx=(8, 0), pady=11)
        divider(left, vertical=True).pack(side="left", padx=14, pady=18)
        self.crumbs = ctk.CTkFrame(left, fg_color="transparent")
        self.crumbs.pack(side="left", fill="y")

        right = ctk.CTkFrame(self, fg_color="transparent")
        right.pack(side="right", fill="y", padx=(0, 20))
        adm = is_admin()
        self.admin = Chip(right, "Admin" if adm else "Not administrator", self.admin_menu, icon="shield",
                          tone="success" if adm else "warn")
        self.admin.pack(side="right", pady=11)
        self.help = IconButton(right, "help", app.open_help)
        self.help.pack(side="right", padx=(8, 8), pady=12)
        self.present = Chip(right, "Presenting", lambda: app.set_presentation(False), icon="present", tone="warn")
        self.jobs = Chip(right, "1 running", self.jobs_menu, icon="clock", tone="info")
        self.search = ctk.CTkFrame(right, corner_radius=8, height=34, width=250, fg_color=P["surface"],
                                   border_width=1, border_color=P["border_strong"])
        self.search.pack(side="right", pady=11, padx=(0, 4))
        self.search.pack_propagate(False)
        Icon(self.search, "search", 16, "muted", "surface").pack(side="left", padx=(10, 8))
        ctk.CTkLabel(self.search, text="Search or jump to", font=theme.font_style("body"), text_color=P["muted"],
                     height=28).pack(side="left")
        ctk.CTkLabel(self.search, text=" Ctrl K ", font=theme.font(10, "bold"), text_color=P["muted"],
                     fg_color=P["surface_2"], corner_radius=4, height=20).pack(side="right", padx=8)
        bind_all(self.search, "<Button-1>", lambda _e: app.open_palette())
        self.search.configure(cursor="hand2")
        focusable(self.search, app.open_palette)
        self.edge.lift()  # keep the bottom rule above the packed halves
        self.update_context()

    # -- state ---------------------------------------------------------------
    def set_crumbs(self, crumbs):
        for w in self.crumbs.winfo_children():
            w.destroy()
        for i, (label, cmd) in enumerate(crumbs):
            last = i == len(crumbs) - 1
            if i:
                Icon(self.crumbs, "chevron_right", 12, "muted", "bg").pack(side="left", padx=2)
            if cmd and not last:
                b = ctk.CTkButton(self.crumbs, text=label, command=cmd, width=0, height=28, corner_radius=6,
                                  fg_color="transparent", hover_color=P["hover"], text_color=P["text_2"],
                                  font=theme.font_style("body"))
                b.pack(side="left")
            else:
                ctk.CTkLabel(self.crumbs, text=label, font=theme.font_style("body_strong" if last else "body"),
                             text_color=P["text"] if last else P["text_2"]).pack(side="left", padx=6)

    def update_context(self):
        ctx = self.app.context
        self.client.set(ctx.get("client") or "All clients")
        t = ctx.get("ticket")
        self.ticket.set(f"Ticket #{t}" if t else "+ Ticket")
        self.ticket.lbl.configure(font=theme.font_style("body_strong") if not t else theme.mono(12),
                                  text_color=P["text"] if t else P["muted"])

    def update_jobs(self, job):
        if job is None:
            self.jobs.pack_forget()
            return
        pct = int(job.snapshot()["pct"])
        self.jobs.set(f"Running {pct}%")
        if not self.jobs.winfo_ismapped():
            self.jobs.pack(side="right", padx=(8, 0), pady=11, before=self.help)

    def update_presenting(self):
        if theme.presentation():
            self.present.pack(side="right", padx=(8, 0), pady=11, before=self.help)
        else:
            self.present.pack_forget()

    # -- menus ---------------------------------------------------------------
    def client_menu(self):
        app = self.app
        cur = app.context.get("client")
        names = app.client_names()
        items = [("All clients", lambda: app.set_client(None), "building", "current" if not cur else None)]
        if theme.presentation():
            if cur:
                items.append((cur, lambda: None, "building", "current"))
            items += [None, "Other clients are hidden while presenting"]
        else:
            if names:
                items.append("Clients")
            for n in names[:12]:
                items.append((n, lambda n=n: app.set_client(n), "building", "current" if n == cur else None))
            if not names:
                items += [None, "No clients yet. Add them in Manage > Clients"]
        items += [None, ("Manage clients", lambda: app.open_target("manage", "clients"), "settings", None)]
        Popover(self.client, items, width=280)

    def ticket_menu(self):
        app = self.app

        def build(pop):
            ctk.CTkLabel(pop, text="Ticket number", font=theme.font_style("body_strong"), text_color=P["text"],
                         anchor="w").pack(fill="x", padx=14, pady=(12, 2))
            ctk.CTkLabel(pop, text="New jobs are stamped with the client, ticket and technician.",
                         font=theme.font_style("small"), text_color=P["muted"], wraplength=230, justify="left",
                         anchor="w").pack(fill="x", padx=14)
            var = tk.StringVar(value=app.context.get("ticket", ""))
            e = ctk.CTkEntry(pop, textvariable=var, height=32, font=theme.mono(13), placeholder_text="48213")
            e.pack(fill="x", padx=14, pady=(8, 8))
            row = ctk.CTkFrame(pop, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=(0, 12))

            def apply(_e=None):
                app.set_ticket(var.get().strip().lstrip("#"))
                pop.close()
            e.bind("<Return>", apply)
            ctk.CTkButton(row, text="Set", width=70, height=30, command=apply,
                          font=theme.font_style("body_strong")).pack(side="right")
            ctk.CTkButton(row, text="Clear", width=70, height=30, fg_color=P["surface"], hover_color=P["hover"],
                          text_color=P["text"], border_width=1, border_color=P["border_strong"],
                          font=theme.font_style("body_strong"),
                          command=lambda: (app.set_ticket(""), pop.close())).pack(side="right", padx=8)
            e.after(50, e.focus_set)
        Popover(self.ticket, build=build, width=260)

    def jobs_menu(self):
        app = self.app
        if app.job is None:
            return
        s = app.job.snapshot()
        items = [(f"{app.job_record['task']}  {int(s['pct'])}%", app.show_job_screen, "clock", None), None,
                 ("Cancel job", app.cancel_job, "close", None)]
        Popover(self.jobs, items, width=280, align="right")

    def admin_menu(self):
        if is_admin():
            Popover(self.admin, ["Running as administrator", ("Physical drives are available", lambda: None,
                                                              "check", None)], width=280, align="right")
            return
        items = ["Not running as administrator",
                 ("Physical drives are hidden. Disk images still work.", lambda: None, "warn", None)]
        from ..util import is_windows
        if is_windows():
            items += [None, ("Restart as administrator", self.app.restart_as_admin, "shield", None)]
        Popover(self.admin, items, width=320, align="right")
