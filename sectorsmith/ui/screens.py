"""Home screen and the step-by-step task wizards."""
from __future__ import annotations

import datetime as _dt
import os
import platform
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from .. import carver, ntfs, partitions, partscan, surface, wipe
from ..report import wipe_certificate
from ..util import get_logger, human_size, human_time, is_admin
from .shell import TICKETS
from . import theme
from .shell import branding, machine_status, technician
from .widgets import AutoScroll, Card, DrivePicker, DropZone, EmptyState, Icon, IconBadge, OptionCard, Pill, \
    Popover, \
    ProgressPanel, QuickAction, StatusPill, StatusTile, TypedConfirm, caption, divider, ghost_button, \
    primary_button, secondary_button, segmented, selected_value

log = get_logger()
P = theme.PALETTE
PAD = theme.PAGE_PAD

STRENGTHS = [
    ("Quick", "Zero fill (1 pass)", "One pass of zeros. Fast, fine when the drive stays in the business."),
    ("Recommended", "NIST 800-88 Clear (1 pass + verify)", "The modern standard, with read-back verification."),
    ("Thorough", "DoD 5220.22-M (3 pass + verify)", "Three passes plus verification. Takes about 4x longer."),
]

CATS = {
    "Photos": {"jpg", "jpeg", "png", "gif", "bmp", "webp", "heic", "tif", "tiff", "cr2", "nef", "arw", "dng", "psd",
               "raw", "svg"},
    "Documents": {"doc", "docx", "xls", "xlsx", "ppt", "pptx", "pdf", "txt", "rtf", "odt", "ods", "odp", "csv",
                  "msg", "pst", "ost", "eml", "dwg", "dxf", "rvt", "pln", "indd", "ai", "vsdx", "one"},
    "Video & audio": {"mp4", "mov", "avi", "mkv", "wmv", "m4v", "m4a", "mp3", "wav", "flac", "3gp", "aac", "wma"},
    "Archives": {"zip", "7z", "rar", "iso", "tar", "gz", "cab", "sqlite", "db"},
}


def category(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    for k, v in CATS.items():
        if ext in v:
            return k
    return "Other"


def greeting(who=None):
    h = _dt.datetime.now().hour
    who = (who if who is not None else technician()).replace(".", " ").split()
    who = who[0][:1].upper() + who[0][1:] if who else ""
    part = "Good morning" if h < 12 else "Good afternoon" if h < 18 else "Good evening"
    return f"{part}{', ' + who if who and who.lower() not in ('root', 'admin', 'administrator') else ''}"


# ---------------------------------------------------------------------------
class Screen(ctk.CTkFrame):
    guide_topic = None
    refreshable = False  # rebuilt in place when the client, personality or presentation mode changes

    def __init__(self, master, app, title="", subtitle="", steps=None, show_back=True, badge=None):
        super().__init__(master, fg_color=P["bg"], corner_radius=0)
        self.app = app
        self.steps = steps or []
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=PAD, pady=(22, 0))
        self.actions = ctk.CTkFrame(head, fg_color="transparent", width=1, height=1)  # page buttons, top right
        self.actions.pack(side="right", anchor="n", pady=(4, 0))
        self.stepper = ctk.CTkFrame(head, fg_color="transparent", height=1, width=1)
        self.stepper.pack(side="right", anchor="n", pady=8)
        titles = ctk.CTkFrame(head, fg_color="transparent")
        titles.pack(side="left", fill="x", expand=True, anchor="n")
        trow = ctk.CTkFrame(titles, fg_color="transparent")
        trow.pack(anchor="w")
        self.title_lbl = ctk.CTkLabel(trow, text=title, font=theme.font_style("h1"), text_color=P["text"],
                                      anchor="w", height=30)
        self.title_lbl.pack(side="left")
        if badge:  # e.g. PREVIEW on features that aren't finished yet
            Pill(trow, badge, "warn").pack(side="left", padx=(12, 0), pady=(2, 0))
        self.sub_lbl = ctk.CTkLabel(titles, text=subtitle, font=theme.font_style("body"), text_color=P["muted"],
                                    anchor="w", justify="left", wraplength=640, height=20)
        self.sub_lbl.pack(anchor="w", pady=(2, 0))
        titles.bind("<Configure>", lambda e: self.sub_lbl.configure(wraplength=max(240, min(720, e.width - 16))),
                    add="+")
        self.holder = ctk.CTkFrame(self, fg_color="transparent")
        self.holder.pack(fill="both", expand=True, padx=PAD, pady=(18, 0))
        if self.steps:
            divider(self).pack(fill="x", pady=(8, 0))
        self.footer = ctk.CTkFrame(self, fg_color="transparent", height=60)
        self.footer.pack(fill="x", padx=PAD, pady=(12, 16))
        self.body = None
        self._draw_steps(0)

    def header_action(self, text, command, primary=False, width=0):
        b = (primary_button if primary else secondary_button)(self.actions, text, command, width=width or 140)
        b.pack(side="right", padx=(10, 0))
        # packed right to left: stack each new one under the last, so Tab goes left to right (never under the
        # frame's own canvas, which would hide it)
        last = getattr(self, "_last_action", None)
        if last is not None and last.winfo_exists():
            b.lower(last)
        self._last_action = b
        return b

    def _open_guide(self):
        if self.app.job is not None:
            self.app.toast("Finish or cancel the current task first.", "warn")
            return
        from .guide import GuideScreen
        self.app.go(GuideScreen, topic=self.guide_topic)

    def go_home(self):
        if self.app.job is not None:
            if not messagebox.askyesno("Stop?", "A task is still running. Cancel it and go home?"):
                return
            self.app.cancel_job()
            self.app.toast("Stopping it safely in the background...", "warn")
        self.app.home()

    def cancelled_state(self, headline, lines, step=None, primary=None, secondary=None, extra=None):
        """Clear result after Cancel: what stopped and what state things were left in."""
        body = self.step(step if step is not None else max(0, len(self.steps) - 1), "Stopped", "") \
            if self.steps else self.new_body()
        if not self.steps:
            self.title_lbl.configure(text="Stopped")
            self.sub_lbl.configure(text="")
        self.result_card(body, "warn", "warn", headline, lines)
        self.buttons(primary=primary or ("Back to home", self.app.home), secondary=secondary, extra=extra)
        return body

    def _draw_steps(self, cur):
        for w in self.stepper.winfo_children():
            w.destroy()
        for i, name in enumerate(self.steps):
            done, now = i < cur, i == cur
            fill = P["success"] if done else P["accent"] if now else P["neutral_soft"]
            ctk.CTkLabel(self.stepper, text="✓" if done else str(i + 1), width=24, height=24, corner_radius=12,
                         fg_color=fill, text_color=P["on_accent"] if (done or now) else P["muted"],
                         font=theme.font(11, "bold")).pack(side="left", padx=(14 if i else 0, 6))
            ctk.CTkLabel(self.stepper, text=name, font=theme.font_style("body_strong" if now else "body"),
                         text_color=P["text"] if now else P["muted"]).pack(side="left")

    def step(self, i, title=None, subtitle=None):
        self._draw_steps(i)
        if title is not None:
            self.title_lbl.configure(text=title)
        if subtitle is not None:
            self.sub_lbl.configure(text=subtitle)
        return self.new_body()

    def new_body(self):
        old = self.body
        body = ctk.CTkFrame(self.holder, fg_color="transparent")
        self.body = body
        if old is not None:
            old.destroy()
        for w in self.footer.winfo_children():
            w.destroy()
        if theme.reduced_motion():
            body.place(relx=0, rely=0, relwidth=1, relheight=1)
            return body
        body.place(relx=0, rely=0, relwidth=1, relheight=1, y=16)
        start = time.monotonic()

        def rise():
            t = min(1.0, (time.monotonic() - start) / .22)
            try:
                body.place_configure(y=16 * (1 - t) ** 3)
            except tk.TclError:
                return
            if t < 1:
                self.after(12, rise)
        rise()
        return body

    def buttons(self, primary=None, secondary=None, extra=None):
        """primary/secondary: (text, command). Returns primary button."""
        # created left to right, so Tab visits them in the order they appear: extra, secondary, primary
        xb = secondary_button(self.footer, extra[0], extra[1], width=150, height=40) if extra else None
        sb = secondary_button(self.footer, secondary[0], secondary[1], width=130, height=40) if secondary else None
        pb = primary_button(self.footer, primary[0], primary[1], width=180, height=40) if primary else None
        if pb is not None:
            pb.pack(side="right")
        if sb is not None:
            sb.pack(side="right", padx=10)
        if xb is not None:
            xb.pack(side="left")
        return pb

    def progress(self, title, subtitle):
        body = self.new_body()
        self.title_lbl.configure(text=title)
        self.sub_lbl.configure(text=subtitle)
        panel = ProgressPanel(body, self.app.cancel_job)
        panel.pack(fill="x", pady=(10, 0))
        return body, panel

    def note(self, parent, text, tone="muted"):
        return ctk.CTkLabel(parent, text=text, font=theme.font_style("small"), text_color=P[tone], wraplength=780,
                            justify="left", anchor="w")

    def result_card(self, parent, icon, tone, headline, lines):
        card = ctk.CTkFrame(parent, corner_radius=12, fg_color=P["surface"], border_width=1, border_color=P["border"])
        card.pack(fill="x", pady=(6, 10))
        if icon == "check" and tone == "success" and theme.effective_personality() != "Off":
            from .mascot import MossbitView  # Subtle and Full: Mossbit's happy hop instead of a check icon
            b = MossbitView(card, "happy", 1, bg="surface", loop=False)
        else:
            b = IconBadge(card, icon, 44, tone)
        b.grid(row=0, column=0, rowspan=2, padx=20, pady=20, sticky="n")
        ctk.CTkLabel(card, text=headline, font=theme.font(18, "bold"), text_color=P["text"], anchor="w").grid(
            row=0, column=1, sticky="w", pady=(20, 2), padx=(0, 20))
        ctk.CTkLabel(card, text="\n".join(lines), font=theme.font_style("body"), text_color=P["text_2"],
                     justify="left", anchor="w", wraplength=760).grid(row=1, column=1, sticky="w", pady=(0, 20),
                                                                      padx=(0, 20))
        card.grid_columnconfigure(1, weight=1)
        return card

    def summary(self, parent, icon, tone, title, sub, fields, note=None):
        """Confirmation summary: icon, title, one line, then label and value pairs in two columns."""
        card = ctk.CTkFrame(parent, corner_radius=12, fg_color=P["surface"], border_width=1, border_color=P["border"])
        card.pack(fill="x", pady=(4, 12))
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(18, 10))
        IconBadge(top, icon, 40, tone).pack(side="left", padx=(0, 14))
        t = ctk.CTkFrame(top, fg_color="transparent")
        t.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(t, text=title, font=theme.font_style("h3"), text_color=P["text"], anchor="w").pack(fill="x")
        ctk.CTkLabel(t, text=sub, font=theme.font_style("small"), text_color=P["muted"], anchor="w").pack(fill="x")
        grid = ctk.CTkFrame(card, fg_color="transparent")
        grid.pack(fill="x", padx=20, pady=(0, 14))
        for i, (k, v) in enumerate(fields):
            r, c = divmod(i, 2)
            ctk.CTkLabel(grid, text=k, font=theme.font_style("small"), text_color=P["muted"], anchor="w",
                         width=110).grid(row=r, column=c * 2, sticky="w", pady=4)
            mono = k in ("Serial", "Ticket", "Hash")
            ctk.CTkLabel(grid, text=v, font=theme.mono(12) if mono else theme.font_style("body"),
                         text_color=P["text"], anchor="w").grid(row=r, column=c * 2 + 1, sticky="w", pady=4,
                                                                padx=(0, 24))
        grid.grid_columnconfigure((1, 3), weight=1)
        if note:
            divider(card).pack(fill="x")
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=20, pady=10)
            Icon(row, "warn", 16, "warn", "surface").pack(side="left", padx=(0, 10))
            ctk.CTkLabel(row, text=note, font=theme.font_style("small"), text_color=P["text_2"], anchor="w",
                         justify="left", wraplength=720).pack(side="left", fill="x")
        return card

    def confirm_box(self, parent, phrase, on_change):
        box = TypedConfirm(parent, phrase, on_change)
        box.pack(fill="x", pady=10)
        self.after(100, box.entry.focus_set)
        return box.var

    def done_actions(self, folder=None):
        self.buttons(primary=("Back to home", self.app.home),
                     secondary=("Open folder", lambda: self.app.open_folder(folder)) if folder else None)


# ---------------------------------------------------------------------------
def context_line(app) -> str:
    now = _dt.datetime.now()
    parts = [f"{now:%A} {now.day} {now:%B}", app.context.get("client") or "All clients"]
    if app.context.get("ticket"):
        parts.append(f"ticket #{app.context['ticket']}")
    n = len(app.machines()) - 1
    parts.append(f"{n} machine{'s' if n != 1 else ''} linked")
    return "  ·  ".join(parts)


START_GROUPS = [
    ("Recover", [("recover", "Recover files", "recover", "files"), ("partition", "Lost partitions", "recover",
                                                                    "partitions")]),
    ("Erase", [("wipe", "Erase and certify", "erase", "erase"), ("shred", "Shred files", "erase", "shred")]),
    ("Drives", [("health", "Health check", "drives", "health"), ("clone", "Image and clone", "drives", "clone")]),
    ("Machines", [("migrate", "Migrate user", "machines", "migrate"),
                  ("netclone", "Network clone", "machines", "netclone")]),
]
TONE = {"Recover": "accent", "Erase": "danger", "Drives": "accent", "Machines": "teal"}


class Home(Screen):
    """Workspace overview: status tiles, recent jobs, grouped job starts, what's running, linked machines."""

    refreshable = True

    def __init__(self, master, app):
        super().__init__(master, app, title=greeting(), subtitle=context_line(app), show_back=False)
        self.title_lbl.configure(font=theme.font_style("display"), height=36)
        self.new_job_btn = self.header_action("New job", self._new_job_menu, primary=True, width=120)
        self.header_action("Connect a machine", app.open_connect, width=160)
        body = self.new_body()
        page = AutoScroll(body)  # scrolls only on small windows
        page.pack(fill="both", expand=True)
        self.tiles_frame = tiles = ctk.CTkFrame(page, fg_color="transparent")
        tiles.pack(fill="x", padx=(0, 4))
        self._tiles(tiles)
        low = ctk.CTkFrame(page, fg_color="transparent")
        low.pack(fill="x", pady=(16, 0), padx=(0, 4))
        low.grid_columnconfigure(0, weight=7, uniform="h")
        low.grid_columnconfigure(1, weight=4, uniform="h")
        low.grid_rowconfigure(1, weight=1)
        self._recent(low).grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.start = self._start(low)
        self.start.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(16, 0))
        self._running(low).grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self._machines(low).grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(16, 0))
        app.drop_handlers.append(lambda kind, _p: self.start.configure(
            border_color=P["accent"] if kind == "enter" else P["border"]))
        self._wide = None
        page.bind("<Configure>", lambda e: self._reflow(e.width), add="+")

    def _reflow(self, width):
        """Four tiles and four job groups per row on wide windows, two on narrow ones."""
        wide = width >= 1000
        if wide == self._wide:
            return
        self._wide = wide
        per = 4 if wide else 2
        for i, t in enumerate(self.tile.values()):
            r, c = divmod(i, per)
            t.grid_configure(row=r, column=c, padx=(0 if c == 0 else 8, 0 if c == per - 1 else 8),
                             pady=(16 if r else 0, 0))
        for g, (cap, acts) in enumerate(self.groups):
            block, c = divmod(g, per)
            cap.grid_configure(row=block * 3, column=c, pady=(12 if block else 0, 6))
            for r, a in enumerate(acts, 1):
                a.grid_configure(row=block * 3 + r, column=c)
        for frame, name in ((self.tiles_frame, "t"), (self.start_grid, "g")):
            for c in range(4):
                frame.grid_columnconfigure(c, weight=1 if c < per else 0, uniform=name if c < per else "")

    def machines_changed(self):
        self.app._refresh_screen()

    # -- tiles ------------------------------------------------------------------
    def _tiles(self, row):
        app = self.app
        remote = app.machines()[1:]
        busy = sum(1 for m in remote if machine_status(m) == "Busy")
        today = [j for j in app.jobs if _dt.date.fromtimestamp(j["started"]) == _dt.date.today()]
        done = sum(1 for j in today if j["result"] == "Done")
        running = sum(1 for j in today if j["result"] == "Running")
        specs = [
            ("Linked machines", len(remote), (f"{busy} busy" if busy else "All idle") if remote else
             "Link a PC to work on it remotely",
             "success", lambda: app.open_target("machines", "linked")),
            ("Jobs today", len(today), f"{done} finished  ·  {running} running" if today else
             "Stamped with client and technician", "accent", lambda: app.open_target("jobs", "history")),
            ("Drives on This PC", "-", "Counting drives", "info", lambda: app.open_target("drives", "all")),
            ("Software library", "-", "Manage preview", "amber", lambda: app.open_target("manage", "library")),
        ]
        self.tile = {}
        for i, (label, value, sub, tone, cmd) in enumerate(specs):
            t = StatusTile(row, label, value, sub, tone, cmd)
            t.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0 if i == 3 else 8))
            row.grid_columnconfigure(i, weight=1, uniform="t")
            self.tile[label] = t
        app.background(self._count_drives, self._show_drives, error=lambda _e: None)
        try:
            st = app.deploy_store()
            self.tile["Software library"].value.configure(text=str(len(st.packages)))
            self.tile["Software library"].sub.configure(
                text=f"{len(st.clients)} client{'s' if len(st.clients) != 1 else ''}  ·  "
                     f"{len(st.deployments)} deployment{'s' if len(st.deployments) != 1 else ''}")
        except Exception:  # noqa: BLE001  library not readable
            self.tile["Software library"].sub.configure(text="Library not available")

    def _count_drives(self):
        from ..device import list_disks
        try:
            devs = list_disks()
        except Exception:  # noqa: BLE001
            devs = []
        for d in devs:
            d.close()
        return len(devs)

    def _show_drives(self, n):
        try:
            imgs = len(self.app.images)
            t = self.tile["Drives on This PC"]
            t.value.configure(text=str(n + imgs))
            if not is_admin() and not n:
                t.sub.configure(text="Run as administrator to see physical drives")
            else:
                t.sub.configure(text=f"{imgs} disk image{'s' if imgs != 1 else ''} open" if imgs else
                                "Physical drives, no images open")
        except tk.TclError:
            pass  # tile closed while the drive list refreshed

    # -- cards ------------------------------------------------------------------
    def _recent(self, parent):
        card = Card(parent, "Recent jobs", ("Open job history", lambda: self.app.open_target("jobs", "history")),
                    pad=0)
        card.head.pack_configure(padx=20)
        jobs = list(reversed(self.app.jobs))[:5]
        if not jobs:
            EmptyState(card.body, "No jobs yet", "Jobs you run show up here with their client and result.",
                       icon="jobs").pack(pady=(4, 18))
            return card
        cols = (("Task", 0), ("Machine", 140), ("Ticket" if TICKETS else "Client", 90), ("Result", 130))
        head = ctk.CTkFrame(card.body, fg_color=P["surface_2"], corner_radius=0, height=34)
        head.pack(fill="x")
        for i, (name, w) in enumerate(cols):
            caption(head, name).grid(row=0, column=i, sticky="w", padx=(20 if i == 0 else 0, 12), pady=9)
        _columns(head, cols)
        for j in jobs:
            divider(card.body).pack(fill="x")
            r = ctk.CTkFrame(card.body, fg_color="transparent", height=44)
            r.pack(fill="x")
            task = ctk.CTkFrame(r, fg_color="transparent")
            task.grid(row=0, column=0, sticky="w", padx=(20, 12), pady=7)
            IconBadge(task, _job_icon(j["title"]), 28, "danger" if j["title"] in ("Wipe", "Shred") else "accent").pack(
                side="left", padx=(0, 10))
            ctk.CTkLabel(task, text=j["task"], font=theme.font_style("body_strong"), text_color=P["text"]).pack(
                side="left")
            ctk.CTkLabel(r, text=j["machine"], font=theme.font_style("body"), text_color=P["text_2"], width=140,
                         anchor="w").grid(row=0, column=1, sticky="w", padx=(0, 12))
            third = (f"#{j['ticket']}" if j["ticket"] else "-") if TICKETS else (j.get("client") or "-")
            ctk.CTkLabel(r, text=third, font=theme.mono(12) if TICKETS else theme.font_style("body"),
                         text_color=P["text_2"], width=90, anchor="w").grid(row=0, column=2, sticky="w", padx=(0, 12))
            pill = ctk.CTkFrame(r, fg_color="transparent", width=130)
            pill.grid(row=0, column=3, sticky="w", padx=(0, 12))
            StatusPill(pill, j["result"]).pack(anchor="w")
            _columns(r, cols)
        return card

    def _start(self, parent):
        card = Card(parent, "Start a job")
        self.start_grid = grid = ctk.CTkFrame(card.body, fg_color="transparent")
        grid.pack(fill="x")
        self.groups = []
        for c, (group, acts) in enumerate(START_GROUPS):
            grid.grid_columnconfigure(c, weight=1, uniform="g")
            cap = caption(grid, group)
            cap.grid(row=0, column=c, sticky="w", padx=6, pady=(0, 6))
            tiles = []
            for r, (icon, label, cat, item) in enumerate(acts, 1):
                q = QuickAction(grid, icon, label, lambda cat=cat, item=item: self.app.open_target(cat, item),
                                TONE[group])
                q.grid(row=r, column=c, sticky="ew", padx=6, pady=4)
                tiles.append(q)
            self.groups.append((cap, tiles))
        ctk.CTkLabel(card.body, text="Tip: drop a disk image, an installer or files anywhere in the window.",
                     font=theme.font_style("small"), text_color=P["muted"], anchor="w").pack(fill="x", padx=6,
                                                                                            pady=(12, 0))
        return card

    def _running(self, parent):
        card = Card(parent, "Running", ("All jobs", lambda: self.app.open_target("jobs", "history")))
        job = self.app.job
        if job is None:
            row = ctk.CTkFrame(card.body, fg_color="transparent")
            row.pack(fill="x", pady=(4, 6))
            Icon(row, "clock", 20, "muted", "surface").pack(side="left", padx=(0, 10))
            ctk.CTkLabel(row, text="Nothing running. Progress for the job you start shows here.",
                         font=theme.font_style("small"), text_color=P["muted"], justify="left", anchor="w",
                         wraplength=280).pack(side="left", fill="x")
        else:
            rec = self.app.job_record or {}
            ctk.CTkLabel(card.body, text=rec.get("task", "Job"), font=theme.font_style("body_strong"),
                         text_color=P["text"], anchor="w").pack(fill="x")
            bar = ctk.CTkProgressBar(card.body, height=8)
            bar.set(job.snapshot()["pct"] / 100)
            bar.pack(fill="x", pady=8)
        return card

    def _machines(self, parent):
        card = Card(parent, "Linked machines", ("Connect", self.app.open_connect))
        for i, m in enumerate(self.app.machines()[:5]):
            row = ctk.CTkFrame(card.body, fg_color="transparent")
            row.pack(fill="x", pady=5)
            IconBadge(row, "pc", 32, "neutral").pack(side="left", padx=(0, 12))
            StatusPill(row, machine_status(m)).pack(side="right")  # packed first so long text can't squeeze it
            t = ctk.CTkFrame(row, fg_color="transparent")
            t.pack(side="left", fill="x", expand=True)
            info = m.info() if m.is_local else m.info_cache
            name = info.get("hostname", m.label) if m.is_local else m.label
            os_name = _short(info.get("os", "") or "", 28)
            sub = f"This PC  ·  {os_name}" if m.is_local else f"{getattr(m, 'address', '')}  ·  {os_name}"
            ctk.CTkLabel(t, text=name, font=theme.font_style("body_strong"), text_color=P["text"], anchor="w",
                         height=18).pack(fill="x")
            ctk.CTkLabel(t, text=sub, font=theme.font_style("small"), text_color=P["muted"], anchor="w",
                         height=16).pack(fill="x")
        if len(self.app.machines()) == 1:
            ctk.CTkLabel(card.body, text="No other PCs linked yet. Connect one with a single PowerShell command.",
                         font=theme.font_style("small"), text_color=P["muted"], wraplength=300, justify="left",
                         anchor="w").pack(fill="x", pady=(8, 0))
        if theme.effective_personality() == "Full":
            from .mascot import LINES, Mascot, SpeechBubble
            dock = ctk.CTkFrame(card.body, fg_color="transparent")
            dock.pack(side="bottom", fill="x")
            bubble = SpeechBubble(dock)
            bubble.pack(fill="x")
            bubble.say(LINES["hello"][0])
            m = Mascot(dock, bubble, width=300, scale=2, bg="surface")
            m.pack(pady=(4, 0))
            self.app.mascot.attach(m)
        return card

    def _new_job_menu(self):
        from . import nav
        items = []
        for cat in nav.CATEGORIES[1:5]:
            items.append(cat.label)
            for it in nav.items(cat):
                if it.target.startswith("app:") or it.key in ("all", "linked", "connect"):
                    continue
                items.append((it.label, lambda c=cat, i=it: self.app.open_item(c, i), it.icon, None))
        Popover(self.new_job_btn, items, width=260, align="right")


def _columns(frame, cols):
    """Same column widths for a header row and its data rows (column 0 takes the rest)."""
    frame.grid_columnconfigure(0, weight=1)
    for i, (_name, w) in enumerate(cols[1:], 1):
        frame.grid_columnconfigure(i, minsize=w + 12)


def _short(text, n):
    return text if len(text) <= n else text[:n - 3].rstrip() + "..."


def _job_icon(title):
    return {"Wipe": "wipe", "Shred": "shred", "Copy": "clone", "Clone": "netclone", "Migration": "migrate",
            "Health check": "health", "Partition search": "partition", "Restore partitions": "partition",
            "Deploy": "deploy", "Deploy check": "deploy"}.get(title, "recover")


# ---------------------------------------------------------------------------
class _PickMixin:
    def pick_step(self, idx, title, subtitle, mode="any", writes=False, exclude=None, next_text="Next"):
        body = self.step(idx, title, subtitle)
        self.app.mascot.set_mood("idle", "pick")
        self.target = None
        top = ctk.CTkFrame(body, fg_color="transparent")
        top.pack(fill="x")
        secondary_button(top, "Refresh", lambda: self._refresh_pick(body, mode, writes, exclude), width=90,
                         height=30).pack(side="right")
        self._pick_args = (body, mode, writes, exclude)
        self.picker = None
        self._build_picker(body, mode, writes, exclude)
        self.next_btn = self.buttons(primary=(next_text, self._after_pick))
        self.next_btn.configure(state="disabled")
        want = getattr(self.app, "preselect_path", None)  # set by the Drives table for the selected drive
        if want:
            self.app.preselect_path = None
            for card, _b, t in self.picker.rows:
                if t["kind"] == "disk" and t["dev"].path == want:
                    card.click()
                    break
        return body

    def _build_picker(self, body, mode, writes, exclude, refresh=False):
        if self.picker is not None:
            self.picker.destroy()

        def chosen(t):
            self.target = t
            self.next_btn.configure(state="normal")
        self.picker = DrivePicker(body, self.app.inventory(refresh), mode=mode, on_select=chosen, writes=writes,
                                  exclude_path=exclude, height=470)
        self.picker.pack(fill="both", expand=True, pady=(6, 0))

    def _refresh_pick(self, body, mode, writes, exclude):
        self.target = None
        self.next_btn.configure(state="disabled")
        self._build_picker(body, mode, writes, exclude, refresh=True)

    def refresh_drives(self):
        if getattr(self, "picker", None) is not None and self.picker.winfo_exists():
            self._refresh_pick(*self._pick_args)

    def target_range(self):
        t = self.target
        if t["kind"] == "part":
            return t["part"].start_lba, t["part"].sectors
        return 0, t["dev"].total_sectors

    def target_name(self):
        t = self.target
        d = t["dev"]
        if t["kind"] == "part":
            p = t["part"]
            letters = ", ".join(m.rstrip("\\") for m in p.mount)
            return f"{letters + ' ' if letters else ''}{p.label or p.name or p.fs} (partition #{p.index} on {d.name})"
        return f"{d.name} · {d.model or 'Disk'} · {human_size(d.size)}"


# ---------------------------------------------------------------------------
class RecoverWizard(Screen, _PickMixin):
    guide_topic = "recover"

    def __init__(self, master, app):
        super().__init__(master, app, "Recover files", steps=["Drive", "Scan type", "Scan", "Recover"])
        self.pick_step(0, "Recover files", "Where did the files live? Pick the partition (or whole drive).")

    def _after_pick(self):
        t = self.target
        body = self.step(1, "How deep should I look?", f"Scanning {self.target_name()}")
        grp = []
        ntfs_ok = t["kind"] == "part" and t["part"].fs == "NTFS"
        quick = OptionCard(body, "Quick scan", "Reads the drive's file table and recovers deleted files with "
                                               "their original names, folders and dates. Usually takes seconds.",
                           "mft", grp, icon="recover", badge_text="Best first try")
        quick.pack(fill="x", pady=6)
        deep = OptionCard(body, "Deep scan", "Searches every sector for photos, documents, videos and more. Works "
                                             "on formatted or RAW drives. Original file names aren't kept.",
                          "deep", grp, icon="health", tone="violet")
        deep.pack(fill="x", pady=6)
        if not ntfs_ok:
            quick.set_enabled(False, "Quick scan needs an NTFS partition. Pick one on the previous step, or use "
                                     "Deep scan.")
        opts = ctk.CTkFrame(body, fg_color="transparent")
        opts.pack(fill="x", pady=(10, 0))
        self.types = {}
        row = ctk.CTkFrame(opts, fg_color="transparent")
        ctk.CTkLabel(row, text="Look for:", font=theme.font_style("body_strong"), text_color=P["text"]).pack(
            side="left")
        for grp_name in dict.fromkeys(s.group for s in carver.SIGNATURES):
            v = tk.BooleanVar(value=True)
            self.types[grp_name] = v
            ctk.CTkCheckBox(row, text=grp_name, variable=v, font=theme.font_style("body"), checkbox_width=20,
                            checkbox_height=20).pack(side="left", padx=10)
        self.out_var = tk.StringVar()
        orow = ctk.CTkFrame(opts, fg_color="transparent")
        ctk.CTkLabel(orow, text="Save found files to:", font=theme.font_style("body_strong"),
                     text_color=P["text"]).pack(side="left")
        ctk.CTkEntry(orow, textvariable=self.out_var, width=380, height=36,
                     placeholder_text="Choose a folder on a different drive").pack(side="left", padx=10)
        secondary_button(orow, "Browse...", lambda: self.out_var.set(filedialog.askdirectory() or self.out_var.get()),
                         width=90).pack(side="left")

        def chosen(v):
            if v == "deep":
                row.pack(fill="x", pady=4)
                orow.pack(fill="x", pady=8)
            else:
                row.pack_forget()
                orow.pack_forget()
            go.configure(state="normal")
        quick.on_select = deep.on_select = chosen
        self._grp = grp
        go = self.buttons(primary=("Start scan", self._start), secondary=("Back", lambda: self.pick_step(
            0, "Recover files", "Where did the files live?")))
        go.configure(state="disabled")
        (quick if ntfs_ok else deep).select()

    def _start(self):
        how = selected_value(self._grp)
        dev = self.app.clone(self.target["dev"])
        start, count = self.target_range()
        if how == "mft":
            _, panel = self.progress("Reading the file table…", "Looking for deleted files with their names.")
            self._draw_steps(2)

            def job(prog):
                vol = ntfs.NTFSVolume(dev, start * dev.sector_size)
                return vol, vol.scan(prog)
            self.app.run_job("Quick scan", job, self._mft_results, panel, on_error=self._error,
                             on_cancel=lambda info: self.cancelled_state(
                                 "Scan stopped", ["Nothing on the drive was changed."], step=2,
                                 secondary=("Scan again", self._start)))
        else:
            out = self.out_var.get().strip()
            if not out:
                self.app.toast("Choose where to save the files first.", "warn")
                return
            if not self._check_dest(out):
                return
            sigs = [s for s in carver.SIGNATURES if self.types[s.group].get()]
            _, panel = self.progress("Deep scanning…", "Searching every sector for files.")
            self._draw_steps(2)
            self._found_count = 0

            def on_file(name, ln, ext, lba):
                self._found_count += 1

            def job(prog):
                return carver.carve(dev, out, prog, sigs=sigs, start_lba=start, end_lba=start + count,
                                    on_file=on_file)
            self.app.run_job("Deep scan", job, lambda r: self._deep_done(r, out), panel, on_error=self._error,
                             on_cancel=lambda info: self._deep_done({"files": self._found_count, "by_type": {},
                                                                     "out_dir": out}, out, cancelled=True))

    def _check_dest(self, out):
        t = self.target
        mounts = []
        if t["kind"] == "part":
            mounts = t["part"].mount
        else:
            inv = {d.path: pt for d, pt, _e in self.app.inventory()}
            pt = inv.get(t["dev"].path)
            mounts = [m for p in (pt.partitions if pt else []) for m in p.mount]
        same = any(os.path.abspath(out).lower().startswith(m.lower()) for m in mounts)
        if same:
            return messagebox.askyesno("Same drive", "You're saving to the drive you're recovering from. That can "
                                                     "overwrite the very files you want back.\n\nContinue anyway?")
        return True

    def _error(self, e):
        body = self.step(2, "That didn't work", "")
        self.result_card(body, "warn", "danger", "Scan failed", [str(e), "", "Details are in the log file."])
        self.buttons(primary=("Back to home", self.app.home))

    def _mft_results(self, res):
        self.vol, entries = res
        self.entries = [e for e in entries if not e.is_dir and not e.path.startswith("\\$Extend")]
        deleted = [e for e in self.entries if e.deleted]
        if deleted:
            body = self.step(3, f"Found {len(deleted):,} deleted file{'s' if len(deleted) != 1 else ''}",
                             "Select the files you want back, then press Recover.")
        else:
            body = self.step(3, "No deleted files found", "Showing every file on the partition instead. You can "
                                                         "still copy any of them out, or try a Deep scan for more.")
        self.app.mascot.set_mood("happy" if deleted else "idle", "found" if deleted else "none")
        bar = ctk.CTkFrame(body, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 8))
        ent = ctk.CTkEntry(bar, width=260, height=34, placeholder_text="Search names or folders")
        ent.pack(side="left")
        self.filter = ent
        ent.bind("<KeyRelease>", lambda _e: self._fill())
        self.cat = segmented(bar, ["All", "Photos", "Documents", "Video & audio", "Other"],
                             command=lambda _v: self._fill(), height=34)
        self.cat.set("All")
        self.cat.pack(side="left", padx=12)
        self.only_del = tk.BooleanVar(value=bool(deleted))
        ctk.CTkSwitch(bar, text="Deleted only", variable=self.only_del, command=self._fill,
                      font=theme.font_style("body")).pack(side="left")
        self.count_lbl = ctk.CTkLabel(bar, text="", font=theme.font_style("small"), text_color=P["muted"])
        self.count_lbl.pack(side="right")
        wrap = ctk.CTkFrame(body, corner_radius=12, fg_color=P["surface"], border_width=1, border_color=P["border"])
        wrap.pack(fill="both", expand=True)
        cols = ("name", "folder", "size", "modified", "state")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", style="Smith.Treeview",
                                 selectmode="extended")
        for c, w, t in zip(cols, (240, 330, 90, 140, 150), ("Name", "Folder", "Size", "Modified", "Chance")):
            self.tree.heading(c, text=t.upper(), anchor="w")
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview, style="Smith.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(1, 0), pady=(8, 8))
        sb.pack(side="right", fill="y", pady=8, padx=(0, 3))
        self._fill()
        self.buttons(primary=("Recover selected", lambda: self._recover(False)),
                     secondary=("Recover all shown", lambda: self._recover(True)),
                     extra=("Select all", lambda: self.tree.selection_set(self.tree.get_children())))

    def _fill(self):
        t = self.tree
        t.delete(*t.get_children())
        flt = self.filter.get().lower()
        cat = self.cat.get()
        self._idx = {}
        n = 0
        for e in self.entries:
            if self.only_del.get() and not e.deleted:
                continue
            if cat != "All":
                ce = category(e.name)
                if (cat == "Other" and ce not in ("Other", "Archives")) or (cat != "Other" and ce != cat):
                    continue
            if flt and flt not in e.name.lower() and flt not in e.path.lower():
                continue
            n += 1
            if n > 50000:
                break
            iid = str(e.recno)
            self._idx[iid] = e
            chance = {"Good": "Excellent", "Possible": "Good"}.get(e.recoverability, e.recoverability)
            t.insert("", "end", iid=iid, values=(e.name, e.path, human_size(e.size),
                                                 e.mtime.strftime("%d %b %Y %H:%M") if e.mtime else "", chance))
        self.count_lbl.configure(text=f"{n:,} shown")

    def _recover(self, all_shown):
        ids = self.tree.get_children() if all_shown else self.tree.selection()
        ents = [self._idx[i] for i in ids if i in self._idx]
        if not ents:
            self.app.toast("Select some files first.", "warn")
            return
        out = filedialog.askdirectory(title="Save recovered files to (pick a different drive)")
        if not out or not self._check_dest(out):
            return
        vol = self.vol
        _, panel = self.progress("Recovering files…", f"Saving {len(ents):,} file(s) to {out}")

        def job(prog):
            return ntfs.recover_entries(vol, ents, out, prog)

        def done(res):
            body = self.step(3, "Your files are back", "")
            self.app.mascot.set_mood("happy", "found")
            lines = [f"Saved to {out}"]
            if res["failed"]:
                lines.append(f"{len(res['failed'])} couldn't be recovered (e.g. {res['failed'][0]})")
            self.result_card(body, "check", "success", f"{res['recovered']:,} file(s) recovered", lines)
            self.done_actions(out)
        def stopped(info):
            n = info.get("recovered", 0)
            self.cancelled_state(f"Stopped after {n:,} of {info.get('total', len(ents)):,} file(s)",
                                 [f"The files already saved are in {out}.",
                                  "The file being saved when you pressed Cancel was removed (it was incomplete).",
                                  "Nothing on the source drive was changed."], step=3,
                                 secondary=("Open folder", lambda: self.app.open_folder(out)))
        self.app.run_job("Recover", job, done, panel, on_error=self._error, on_cancel=stopped)

    def _deep_done(self, res, out, cancelled=False):
        body = self.step(3, "Deep scan finished" if not cancelled else "Deep scan stopped", "")
        n = res["files"]
        self.app.mascot.set_mood("happy" if n else "idle", "found" if n else "none")
        self.result_card(body, "check" if n else "warn", "success" if n else "warn", f"{n:,} file(s) found",
                         [f"Saved into folders by type in {out}"]
                         + (["Stopped by Cancel: the rest of the drive wasn't searched. Nothing on it was "
                             "changed."] if cancelled else []))
        chips = ctk.CTkFrame(body, fg_color="transparent")
        chips.pack(fill="x")
        for ext, cnt in sorted(res.get("by_type", {}).items(), key=lambda x: -x[1]):
            Pill(chips, f"{ext.upper()}  {cnt:,}", "accent").pack(side="left", padx=4, pady=4)
        self.done_actions(out)


# ---------------------------------------------------------------------------
class PartitionWizard(Screen, _PickMixin):
    guide_topic = "partition"

    def __init__(self, master, app):
        super().__init__(master, app, "Find lost partitions", steps=["Drive", "Search", "Restore"])
        self.pick_step(0, "Find lost partitions", "Which drive is missing a partition (or shows as empty / RAW)?",
                       mode="disk")

    def _after_pick(self, mode="quick"):
        dev = self.app.clone(self.target["dev"])
        _, panel = self.progress("Searching for partitions…", "Quick search: checks the usual partition "
                                                              "positions." if mode == "quick" else
                                 "Deep search: checking every sector. This takes a while on big drives.")
        self._draw_steps(1)
        self.app.run_job("Partition search", lambda prog: partscan.scan_partitions(dev, prog, mode=mode),
                         lambda r: self._results(r, mode), panel,
                         on_cancel=lambda info: self.cancelled_state("Search stopped",
                                                                     ["Nothing on the drive was changed."]))

    def _results(self, found, mode):
        lost = [f for f in found if f.status == "Lost"]
        body = self.step(2, f"Found {len(found)} partition{'s' if len(found) != 1 else ''}"
                            + (f", {len(lost)} missing" if lost else ""),
                         "Tick the ones to bring back." if lost else "Everything found is already in the "
                                                                      "partition table.")
        self.app.mascot.set_mood("happy" if lost else "idle", "found" if lost else "none")
        ss = self.target["dev"].sector_size
        self.checks = []
        lst = ctk.CTkScrollableFrame(body, fg_color="transparent", height=360)
        lst.pack(fill="both", expand=True)
        for f in found:
            card = ctk.CTkFrame(lst, corner_radius=10, fg_color=P["surface"], border_width=1,
                                border_color=P["border"])
            card.pack(fill="x", pady=3, padx=(0, 8))
            v = tk.BooleanVar(value=f.status == "Lost")
            if f.status == "Lost":
                ctk.CTkCheckBox(card, text="", variable=v, width=24, checkbox_width=20,
                                checkbox_height=20).pack(side="left", padx=(16, 4))
                self.checks.append((v, f))
            b = IconBadge(card, "partition", 32, "accent" if f.status == "Lost" else "neutral")
            b.pack(side="left", padx=10, pady=10)
            txt = ctk.CTkFrame(card, fg_color="transparent")
            txt.pack(side="left", fill="x", expand=True)
            top = ctk.CTkFrame(txt, fg_color="transparent")
            top.pack(anchor="w")
            ctk.CTkLabel(top, text=f"{f.fs}  {f.label}", font=theme.font_style("body_strong"),
                         text_color=P["text"]).pack(side="left")
            Pill(top, "Missing" if f.status == "Lost" else "In table",
                 "danger" if f.status == "Lost" else "success").pack(side="left", padx=8)
            ctk.CTkLabel(txt, text=f"Starts at sector {f.start_lba:,} · found via {f.source} · confidence "
                                   f"{f.confidence}", font=theme.font_style("small"), text_color=P["muted"]).pack(
                anchor="w")
            ctk.CTkLabel(card, text=human_size(f.sectors * ss), font=theme.font_style("body_strong"),
                         text_color=P["text"]).pack(side="right", padx=18)
        if mode == "quick":
            self.note(body, "Not what you expected? A deep search checks every single sector.").pack(anchor="w",
                                                                                                      pady=6)
        self.buttons(primary=("Bring them back", self._confirm) if lost else ("Back to home", self.app.home),
                     extra=("Deep search", lambda: self._after_pick("full")) if mode == "quick" else None)

    def _confirm(self):
        chosen = [f for v, f in self.checks if v.get()]
        if not chosen:
            self.app.toast("Tick at least one partition.", "warn")
            return
        dev = self.target["dev"]
        if dev.is_system:
            self.app.toast("The system disk is protected.", "danger")
            return
        body = self.step(2, "Restore partitions?", "I'll back up the partition table first, then add these "
                                                   "entries.")
        self.app.mascot.set_mood("warn", "confirm")
        self.result_card(body, "partition", "accent", f"{len(chosen)} partition(s) to {dev.name}",
                         [f"{f.fs} {f.label} at sector {f.start_lba:,}" for f in chosen])
        gpt = tk.BooleanVar(value=dev.usable_size > 2 * 1024 ** 4)
        ctk.CTkSwitch(body, text="If the drive has no partition table, create GPT (otherwise MBR)",
                      variable=gpt, font=theme.font_style("body")).pack(anchor="w", pady=6)
        go = self.buttons(primary=("Restore now", lambda: self._go(chosen, gpt.get())))
        go.configure(state="disabled")
        self.confirm_box(body, "RESTORE", lambda ok: go.configure(state="normal" if ok else "disabled"))

    def _go(self, chosen, gpt):
        wdev = self.app.clone(self.target["dev"])
        _, panel = self.progress("Restoring…", "Writing the partition table.")

        def job(prog):
            prog.reset(len(chosen) + 1, "Backing up the partition table")
            bk = partitions.backup_table_area(wdev)
            wdev.close()
            wdev.open(writable=True)
            out = []
            try:
                for i, f in enumerate(chosen):
                    out.append(partitions.add_partition_entry(wdev, f.start_lba, f.sectors, f.fs, prefer_gpt=gpt))
                    prog.update(i + 2)
            finally:
                wdev.close()
            return bk, out

        def done(res):
            bk, out = res
            self.app._inventory = None
            body = self.step(2, "Partitions restored", "")
            self.app.mascot.set_mood("happy", "done")
            self.result_card(body, "check", "success", f"{len(out)} partition(s) are back",
                             out + ["", f"Backup saved: {bk}",
                                    "Windows may need a rescan: Disk Management > Action > Rescan Disks."])
            self.done_actions(os.path.dirname(bk))
        self.app.run_job("Restore partitions", job, done, panel)


# ---------------------------------------------------------------------------
NO_METHOD = "Use the choice above"


class _StrengthMixin:
    def strength_picker(self, parent):
        grp = []
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x")
        for i, (name, mkey, desc) in enumerate(STRENGTHS):
            oc = OptionCard(row, name, desc, mkey, grp, badge_text="Most people" if name == "Recommended" else None)
            oc.grid(row=0, column=i, sticky="nsew", padx=6)
            oc.desc.configure(wraplength=230)
            row.grid_columnconfigure(i, weight=1, uniform="s")
        more = ctk.CTkFrame(parent, fg_color="transparent")
        more.pack(fill="x", pady=(14, 0))
        ctk.CTkLabel(more, text="More methods:", font=theme.font_style("body_strong"), text_color=P["text"]).pack(
            side="left")
        self._more = ctk.CTkOptionMenu(more, values=[NO_METHOD] + list(wipe.METHODS), width=340, height=34,
                                       font=theme.font_style("body"), dropdown_font=theme.font_style("body"),
                                       command=lambda v: self._pick_more(v, grp))
        self._more.pack(side="left", padx=10)
        self.note(parent, "SSDs: overwriting can't reach spare flash cells. For SSDs leaving the business, erase the "
                          "whole drive with its built-in erase (Erase and certify), or physically destroy them.",
                  "warn").pack(
            anchor="w", pady=(14, 0))
        grp[1].select()
        self._sgrp = grp
        return grp

    def _pick_more(self, v, grp):
        if v != NO_METHOD:
            for o in grp:
                o.configure(border_color=P["border"], fg_color=P["surface"])
                o.selected = False

    def chosen_method(self):
        m = self._more.get()
        if m in wipe.METHODS and not any(getattr(o, "selected", False) for o in self._sgrp):
            return wipe.METHODS[m]
        return wipe.METHODS[selected_value(self._sgrp)]


def erase_phrase(target) -> str:
    """What the operator types to confirm: the disk name plus the end of its serial, or the partition number."""
    dev = target["dev"]
    if target["kind"] == "part":
        return f"ERASE PARTITION {target['part'].index}"
    serial = "".join(ch for ch in (dev.serial or "") if ch.isalnum())[-6:]
    return f"ERASE {dev.name.upper()}" + (f" {serial.upper()}" if serial else "")


class WipeWizard(Screen, _PickMixin, _StrengthMixin):
    guide_topic = "wipe"

    def __init__(self, master, app):
        super().__init__(master, app, "Erase and certify", steps=["Drive", "Method", "Confirm", "Erase"])
        self.pick_step(0, "Erase and certify", "Pick a whole drive or a single partition to erase.", writes=True)

    def _after_pick(self):
        body = self.step(1, "Choose a method", f"Erasing {self.target_name()}")
        self.hw_plan = None
        self.use_hw = tk.BooleanVar(value=False)
        if self.target["kind"] == "disk":
            self._hw_card(body)
        self.strength_picker(body)
        self.buttons(primary=("Next", self._confirm))

    def _hw_card(self, body):
        """The drive's own erase (ATA Secure Erase or NVMe Sanitize): what it supports, checked in the background."""
        card = Card(body, "Drive's built-in erase", pad=16)
        card.pack(fill="x", pady=(0, 12))
        self.hw_switch = ctk.CTkSwitch(card.body, text="Use the drive's own erase first (best for SSD and NVMe)",
                                       variable=self.use_hw, font=theme.font_style("body_strong"), state="disabled")
        self.hw_switch.pack(anchor="w")
        self.hw_status = ctk.CTkLabel(card.body, text="Checking what this drive supports\u2026",
                                      font=theme.font_style("small"), text_color=P["muted"], anchor="w",
                                      justify="left", wraplength=760)
        self.hw_status.pack(fill="x", pady=(6, 0))
        dev = self.app.clone(self.target["dev"])
        target = self.target

        def ready(plan):
            if self.target is target and self.hw_status.winfo_exists():
                self._hw_ready(plan)
        self.app.background(lambda: wipe.hardware_plan(dev), ready,
                            lambda e: ready(wipe.HardwarePlan(False, reason=f"Capability check failed: {e}")))

    def _hw_ready(self, plan):
        self.hw_plan = plan
        if plan.available:
            self.use_hw.set(True)
            self.hw_switch.configure(state="normal")
            self.hw_status.configure(text=f"Supported: {plan.name}. NIST SP 800-88 Purge. It reaches spare cells an "
                                          "overwrite can't, and takes seconds to minutes. If the drive refuses it, the "
                                          "overwrite below runs instead.", text_color=P["text_2"])
        elif plan.frozen:
            self.use_hw.set(True)
            self.hw_switch.configure(state="normal")
            self.hw_status.configure(text=plan.reason, text_color=P["warn"])
        else:
            self.use_hw.set(False)
            self.hw_switch.configure(state="disabled")
            self.hw_status.configure(text=f"Not available: {plan.reason} The overwrite below is used (NIST SP 800-88 "
                                          "Clear).", text_color=P["muted"])

    def _hardware_chosen(self):
        p = self.hw_plan
        return bool(self.target["kind"] == "disk" and p is not None and (p.available or p.frozen) and self.use_hw.get())

    def _confirm(self, use_hw=None):
        if use_hw is not None:
            self.use_hw.set(use_hw)
        if getattr(self, "_more", None) is not None and self._more.winfo_exists():
            self.method = self.chosen_method()  # else: back from a result screen, keep the method chosen before
        t = self.target
        dev = t["dev"]
        start, count = self.target_range()
        hw = self._hardware_chosen()
        body = self.step(2, "Confirm the erase", "Everything on this target is destroyed. This cannot be undone.")
        self.app.mascot.set_mood("warn", "confirm")
        ctx = self.app.context
        flash = dev.bus == "NVMe" or "SSD" in (dev.model or "").upper()
        method = (f"{self.hw_plan.name or 'Built-in erase'} (Purge), else {self.method.name}" if hw
                  else self.method.name + ("" if "Clear" in self.method.name else " (NIST Clear)"))
        self.summary(body, "wipe", "danger", self.target_name(),
                     f"{dev.bus}  \u00b7  {human_size(count * dev.sector_size)} will be erased",
                     [("Serial", theme.mask(dev.serial) if dev.serial else "-"), ("Method", method),
                      ("Client", ctx.get("client") or "Not set"),
                      *([("Ticket", f"#{ctx['ticket']}" if ctx.get("ticket") else "Not set")] if TICKETS else []),
                      ("Certificate", "Saved from the last step"), ("Technician", technician() or "-")],
                     note="The drive erases itself and this can't be stopped once it starts. Sample sectors are "
                          "marked first and checked afterwards." if hw else
                     "This looks like flash storage. Overwriting cannot reach spare cells, so use the drive's "
                     "built-in erase where it is supported, or physically destroy it." if flash else None)
        go = self.buttons(primary=("Erase and certify", self._go), secondary=("Back", self._after_pick))
        go.configure(state="disabled", fg_color=P["danger"], hover_color=P["danger_hover"])
        self.confirm_box(body, erase_phrase(t), lambda ok: go.configure(state="normal" if ok else "disabled"))

    def _go(self):
        dev = self.target["dev"]
        wdev = self.app.clone(dev)
        start, count = self.target_range()
        method = self.method
        hw = self._hardware_chosen()
        what = self.hw_plan.name if hw and self.hw_plan.name else method.name
        _, panel = self.progress("Erasing\u2026", f"{what} on {self.target_name()}")
        self._draw_steps(3)
        self.t_start = time.time()
        self.app.run_job("Wipe", lambda prog: wipe.erase_disk(wdev, method, prog, start_lba=start, sectors=count,
                                                              hardware=hw),
                         self._done, panel, on_error=self._error, on_cancel=self._stopped)
        self.record = dict(self.app.job_record or {})  # client, ticket and technician this job was stamped with

    def _error(self, e):
        self.app._inventory = None
        if isinstance(e, wipe.DriveFrozen):
            body = self.step(3, "Drive is frozen", "")
            self.result_card(body, "warn", "warn", "The drive refused its built-in erase for now",
                             [str(e), "Nothing has been erased."])
            self.buttons(primary=("Try again", self._after_pick),
                         secondary=("Use overwrite instead", lambda: self._confirm(use_hw=False)))
            return
        body = self.step(3, "Erase failed", "")
        self.result_card(body, "warn", "danger", "The erase didn't finish",
                         [str(e), "Don't reuse or hand on this drive until an erase completes.",
                          "No wipe certificate was made."])
        self.buttons(primary=("Back to home", self.app.home), secondary=("Try again", self._confirm))

    def _stopped(self, info):
        self.app._inventory = None
        done, total = info.get("overwritten", 0), info.get("length") or 1
        pas = f"pass {info.get('pass_no', 1)} of {info.get('passes', 1)}"
        if done <= 0:
            lines = ["It stopped before anything was written. The drive is as it was."]
        else:
            lines = [(f"It stopped in {pas}. About {human_size(done)} of {human_size(total)} "
                      f"({100 * done / total:.0f}%) has been overwritten at least once."),
                     ("The drive is now part-erased: its partitions are probably damaged, and data in the part not "
                      "reached yet may still be readable."),
                     "Don't reuse or hand it on like this. Run the wipe again to finish it.",
                     "No wipe certificate was made."]
        self.cancelled_state("Wipe stopped part-way" if done > 0 else "Wipe stopped", lines, step=3,
                             secondary=("Wipe again", self._confirm))

    def _done(self, res):
        self.res = res
        self.res["end"] = time.time()
        self.app._inventory = None
        ok = res["write_errors"] == 0 and res["verify_mismatched_blocks"] == 0
        body = self.step(3, "Erase complete" if ok else "Erase finished with problems", "")
        self.app.mascot.set_mood("happy" if ok else "sad", "wiped" if ok else "sick")
        took = f"Took {human_time(self.res['end'] - self.t_start)}"
        if res.get("hardware"):
            lines = [f"{res['method']} · NIST SP 800-88 Purge", f"Check: {res['verification']}", took]
            if not ok:
                lines.append(f"{res['verify_mismatched_blocks']} marked sector(s) still readable. The drive said it "
                             "erased but didn't. Overwrite it or destroy it.")
        else:
            lines = [f"{human_size(res['bytes'])} overwritten · {res['passes']} pass(es) · NIST SP 800-88 Clear",
                     "Verified by reading it back" if res["verified"] else "Not verified (method has no verify pass)",
                     took]
            if res.get("fallback_reason") and res["fallback_reason"] != "Overwrite chosen.":
                lines.insert(0, res["fallback_reason"])
            if not ok:
                lines.append(f"Unwritable sectors: {res['write_errors']} · verify mismatches: "
                             f"{res['verify_mismatched_blocks']}. This drive may be failing.")
        self.result_card(body, "check" if ok else "warn", "success" if ok else "warn",
                         "Drive is clean" if ok else "Some sectors couldn't be wiped", lines)
        self.buttons(primary=("Save certificate…", self._cert), secondary=("Back to home", self.app.home))

    def certificate_info(self) -> dict:
        """Everything the certificate states: the result, the drive, and the job's client, ticket and technician."""
        dev = self.target["dev"]
        rec = getattr(self, "record", None) or {}
        machine = rec.get("machine") or ""
        if machine in ("", "This PC"):
            machine = platform.node()
        return dict(self.res, target=dev.path, model=dev.model, serial=dev.serial, size=dev.size, bus=dev.bus,
                    method=self.res.get("method") or self.method.name, start=self.t_start, scope=self.target_name(),
                    client=rec.get("client") or self.app.context.get("client") or "",
                    ticket=rec.get("ticket") or self.app.context.get("ticket") or "",
                    technician=rec.get("technician") or technician(), machine=machine)

    def _cert(self):
        dev = self.target["dev"]
        name = f"Wipe certificate - {dev.model or dev.name} - {time.strftime('%Y-%m-%d')}.html".replace("/", "-")
        p = filedialog.asksaveasfilename(defaultextension=".html", initialfile=name,
                                         filetypes=[("Web page (print to PDF)", "*.html")])
        if not p:
            return
        cid = wipe_certificate(p, self.certificate_info(), branding())
        self.app.attach_job_file(p, getattr(self, "record", {}).get("id"))  # Jobs can open it again later
        self.app.toast(f"Certificate {cid} saved. Open it and print to PDF.")
        self.app.open_folder(p)


# ---------------------------------------------------------------------------
class ShredWizard(Screen, _StrengthMixin):
    guide_topic = "shred"

    def __init__(self, master, app, mode=None):
        super().__init__(master, app, "Shred files", steps=["Choose", "Confirm", "Shred"])
        self.items: list[str] = []
        self.free_folder = None
        self.start_mode = mode
        self._build()

    def _build(self):
        body = self.step(0, "Shred files", "Drop files and folders below. They'll be overwritten, then deleted "
                                           "for good.")
        self.mode = segmented(body, ["Files & folders", "Free space on a drive"],
                              command=lambda _v: self._mode_changed(), height=34)
        self.mode.set("Free space on a drive" if self.start_mode == "free" else "Files & folders")
        if self.start_mode == "free":
            self.title_lbl.configure(text="Clean free space")
            self.sub_lbl.configure(text="Destroy traces of files deleted earlier. Your existing files are kept.")
        self.mode.pack(anchor="w", pady=(0, 12))
        self.mode_area = ctk.CTkFrame(body, fg_color="transparent")
        self.mode_area.pack(fill="x")
        self.files_frame = ctk.CTkFrame(self.mode_area, fg_color="transparent")
        self.dz = DropZone(self.files_frame, "Drop files or folders here", "or use Add files and Add folder below",
                           height=140, icon="shred", tone="danger")
        self.dz.pack(fill="x")
        self.app.drop_handlers.append(lambda kind, _p: self.dz.hot(kind == "enter"))
        btns = ctk.CTkFrame(self.files_frame, fg_color="transparent")
        btns.pack(fill="x", pady=8)
        ghost_button(btns, "Add files...", lambda: self.accept_files(list(filedialog.askopenfilenames())),
                     width=130).pack(side="left")
        ghost_button(btns, "Add folder...", lambda: self.accept_files([filedialog.askdirectory()]),
                     width=130).pack(side="left", padx=8)
        self.list = ctk.CTkScrollableFrame(self.files_frame, fg_color="transparent", height=120)
        self.list.pack(fill="x")
        self.free_frame = ctk.CTkFrame(self.mode_area, fg_color="transparent")
        self.note(self.free_frame, "Fills the drive's empty space with wipe data and then frees it again. Your "
                                   "existing files are kept. This only destroys traces of files deleted earlier."
                  ).pack(anchor="w")
        fr = ctk.CTkFrame(self.free_frame, fg_color="transparent")
        fr.pack(fill="x", pady=10)
        self.free_lbl = ctk.CTkLabel(fr, text="No drive chosen", font=theme.font_style("body_strong"),
                                     text_color=P["text"])
        self.free_lbl.pack(side="left")
        ghost_button(fr, "Choose drive or folder...", self._pick_free, width=200).pack(side="left", padx=12)
        ctk.CTkLabel(body, text="How thorough?", font=theme.font_style("h3"), text_color=P["text"]).pack(
            anchor="w", pady=(16, 6))
        self.str_holder = ctk.CTkFrame(body, fg_color="transparent")
        self.str_holder.pack(fill="x")
        self.strength_picker(self.str_holder)
        self._mode_changed()
        self.buttons(primary=("Next", self._confirm))

    def _mode_changed(self):
        files = self.mode.get().startswith("Files")
        self.files_frame.pack_forget()
        self.free_frame.pack_forget()
        (self.files_frame if files else self.free_frame).pack(fill="x")

    def _pick_free(self):
        d = filedialog.askdirectory(title="Pick the drive (or any folder on it)")
        if d:
            self.free_folder = d
            self.free_lbl.configure(text=d)

    def accept_files(self, paths):
        for p in paths:
            if p and p not in self.items and os.path.exists(p):
                self.items.append(p)
        self.mode.set("Files & folders")
        self._mode_changed()
        self._render_items()
        if paths:
            self.app.mascot.say("drop")

    def _render_items(self):
        for w in self.list.winfo_children():
            w.destroy()
        for p in self.items:
            row = ctk.CTkFrame(self.list, corner_radius=8, fg_color=P["surface"], border_width=1,
                               border_color=P["border"])
            row.pack(fill="x", pady=2, padx=(0, 8))
            kind = "Folder" if os.path.isdir(p) else human_size(os.path.getsize(p))
            ctk.CTkLabel(row, text=os.path.basename(p.rstrip("/\\")) or p, font=theme.font_style("body_strong"),
                         text_color=P["text"]).pack(side="left", padx=12, pady=6)
            ctk.CTkLabel(row, text=f"{kind} \u00b7 {os.path.dirname(p)}", font=theme.font_style("small"),
                         text_color=P["muted"]).pack(side="left")
            ctk.CTkButton(row, text="Remove", width=70, height=26, corner_radius=6, fg_color="transparent",
                          hover_color=P["danger_soft"], text_color=P["danger"], font=theme.font_style("small"),
                          command=lambda x=p: (self.items.remove(x), self._render_items())).pack(side="right",
                                                                                                padx=8)

    def _confirm(self):
        files = self.mode.get().startswith("Files")
        self.method = self.chosen_method()
        if files and not self.items:
            self.app.toast("Add some files or folders first.", "warn")
            return
        if not files and not self.free_folder:
            self.app.toast("Choose a drive first.", "warn")
            return
        body = self.step(1, "Last check", "")
        self.app.mascot.set_mood("warn", "confirm")
        if files:
            self.result_card(body, "shred", "warn", f"Shred {len(self.items)} item(s)",
                             [os.path.basename(p.rstrip('/\\')) for p in self.items[:8]]
                             + (["…"] if len(self.items) > 8 else []) + [f"Method: {self.method.name}"])
            go = self.buttons(primary=("Shred them", lambda: self._go(True)), secondary=("Back", self._build))
            go.configure(state="disabled", fg_color=P["danger"], hover_color=P["danger_hover"])
            self.confirm_box(body, "SHRED", lambda ok: go.configure(state="normal" if ok else "disabled"))
        else:
            self.result_card(body, "wipe", "accent", f"Clean free space on {self.free_folder}",
                             [f"Method: {self.method.name}", "Your files stay. The drive will look full for a "
                                                             "while, then go back to normal."])
            self.buttons(primary=("Start", lambda: self._go(False)), secondary=("Back", self._build))

    def _go(self, files):
        method = self.method
        _, panel = self.progress("Shredding…" if files else "Cleaning free space…", method.name)
        self._draw_steps(2)
        items, folder = list(self.items), self.free_folder
        job = (lambda prog: wipe.shred_paths(items, method, prog)) if files else (
            lambda prog: wipe.wipe_free_space(folder, method, prog))

        def done(res):
            body = self.step(2, "All done", "")
            self.app.mascot.set_mood("happy", "wiped")
            if files:
                lines = [f"Method: {method.name}"] + ([f"{len(res['failed'])} failed: {res['failed'][0]}"]
                                                      if res["failed"] else [])
                self.result_card(body, "check", "success", f"{res['files']} file(s) shredded", lines)
            else:
                self.result_card(body, "check", "success", f"{human_size(res['bytes'])} of free space cleaned",
                                 [f"Method: {method.name}"])
            self.buttons(primary=("Back to home", self.app.home))
        def stopped(info):
            if files:
                lines = [f"{info.get('shredded', 0)} of {info.get('total', 0)} file(s) were shredded and are gone.",
                         "The file being shredded when you pressed Cancel is partly overwritten but still there: "
                         + str(info.get("current", "")), "The rest weren't touched."]
            else:
                lines = ["The temporary fill file was deleted, so the drive's free space is back.",
                         "Your files weren't touched. Free space is only partly cleaned: run it again to finish."]
            self.cancelled_state("Shredding stopped" if files else "Cleaning stopped", lines, step=2)
        self.app.run_job("Shred", job, done, panel, on_cancel=stopped)


# ---------------------------------------------------------------------------
class HealthWizard(Screen, _PickMixin):
    guide_topic = "health"

    COLORS = ["#2BD9A0", "#8BD45A", "#FFD34D", "#FF9F40", "#FF6A3D", "#FF3B5C"]

    def __init__(self, master, app):
        super().__init__(master, app, "Health check", steps=["Drive", "Test", "Result"])
        self.pick_step(0, "Health check", "Pick a drive (or partition) to test. It only reads, nothing is "
                                          "changed.")

    def _after_pick(self, repair=False):
        dev = self.app.clone(self.target["dev"])
        start, count = self.target_range()
        body, panel = self.progress("Testing every sector…" if not repair else "Repairing bad sectors…",
                                    "Each square is a slice of the drive. Green is fast, red is unreadable.")
        self._draw_steps(1)
        self.map = tk.Canvas(body, height=150, highlightthickness=0, bd=0, background=theme.c("card"))
        self.map.pack(fill="x", pady=(14, 0))
        body.update_idletasks()
        W = max(600, self.map.winfo_width())
        blocks = -(-count // 2048)
        self.cells = min(blocks, 2400)
        side = max(6, int((W * 150 / self.cells) ** .5))
        while (W // side) * (150 // side) < self.cells and side > 4:
            side -= 1
        self.side, self.cols, self.blocks = side, max(1, W // side), blocks
        worst = [-1] * self.cells
        pending = {}
        last = [time.monotonic()]

        def on_block(b, total, cls, ms):
            i = b * self.cells // total
            if cls > worst[i]:
                worst[i] = cls
                pending[i] = cls
            now = time.monotonic()
            if now - last[0] > .2 and pending:
                last[0] = now
                snap = dict(pending)
                pending.clear()
                self.app.call_soon(self._paint, snap)

        def job(prog):
            r = surface.surface_scan(dev, prog, start_lba=start, sectors=count, on_block=on_block, repair=repair)
            self.app.call_soon(self._paint, dict(pending))
            return r
        self.app.run_job("Health check", job, self._result, panel, on_cancel=lambda info: self.cancelled_state(
            "Health check stopped", ["Sectors that were rewritten so far stay repaired. Nothing else was changed."]
            if repair else ["It only reads, so nothing on the drive was changed."], step=2))

    def _paint(self, cells):
        try:
            for i, cls in cells.items():
                x, y = (i % self.cols) * self.side, (i // self.cols) * self.side
                self.map.create_rectangle(x + 1, y + 1, x + self.side - 1, y + self.side - 1,
                                          fill=self.COLORS[cls], outline="")
        except tk.TclError:
            pass

    def _result(self, res):
        c = res["counts"]
        bad = res["bad_sectors"]
        slow = c[">=600ms"] + c["<600ms"]
        total = max(1, res["blocks"])
        if bad:
            head, icon, tone, key = f"{bad} bad sector(s) found", "warn", "danger", "sick"
            lines = ["Some parts of this drive can't be read. It may be failing.",
                     "Image it now (Image and clone), then replace it."]
        elif slow / total > .02:
            head, icon, tone, key = "Readable, but slow in places", "warn", "warn", "sick"
            lines = [f"{slow:,} slow area(s). Often an early sign of wear, so keep an eye on it."]
        else:
            head, icon, tone, key = "This drive is healthy", "check", "success", "healthy"
            lines = ["Every sector read back fine, with no slow spots."]
        if res.get("repaired"):
            lines.append(f"{res['repaired']} sector(s) were rewritten and now read correctly.")
        body = self.step(2, "Health check result", self.target_name())
        self.app.mascot.set_mood("happy" if tone == "success" else "sad", key)
        self.result_card(body, icon, tone, head, lines)
        bar = tk.Canvas(body, height=26, highlightthickness=0, bd=0, background=theme.c("bg"))
        bar.pack(fill="x", pady=(4, 2))
        body.update_idletasks()
        W = max(400, bar.winfo_width())
        x = 0
        for i, (k, v) in enumerate(c.items()):
            w = W * v / total
            if w > 0:
                bar.create_rectangle(x, 4, x + max(w, 2), 22, fill=self.COLORS[i], outline="")
                x += max(w, 2)
        legend = ctk.CTkFrame(body, fg_color="transparent")
        legend.pack(fill="x")
        for i, (k, v) in enumerate(c.items()):
            ctk.CTkLabel(legend, text=f"\u25a0 {k}: {v:,}", font=theme.font_style("small"),
                         text_color=self.COLORS[i]).pack(
                side="left", padx=(0, 14))
        if res["bad_lbas"]:
            self.note(body, "First bad sectors: " + ", ".join(map(str, res["bad_lbas"][:12]))).pack(anchor="w",
                                                                                                    pady=8)
        dev = self.target["dev"]
        extra = None
        if bad and not dev.is_system:
            extra = ("Try repairing...", self._repair_confirm)
        self.buttons(primary=("Back to home", self.app.home),
                     secondary=("Image it now", self._image_now), extra=extra)

    def _image_now(self):
        self.app.preselect_path = self.target["dev"].path
        self.app.go(CloneWizard)

    def _repair_confirm(self):
        body = self.step(2, "Repair bad sectors?", "Unreadable sectors are rewritten so the drive swaps them for "
                                                   "spares. Whatever was in them is already lost.")
        self.app.mascot.set_mood("warn", "confirm")
        go = self.buttons(primary=("Repair", lambda: self._after_pick(repair=True)))
        go.configure(state="disabled")
        self.confirm_box(body, "REPAIR", lambda ok: go.configure(state="normal" if ok else "disabled"))


# ---------------------------------------------------------------------------
class CloneWizard(Screen, _PickMixin):
    guide_topic = "clone"

    def __init__(self, master, app):
        super().__init__(master, app, "Image and clone", steps=["Source", "Destination", "Confirm", "Copy"])
        self.pick_step(0, "Image and clone", "What should be copied? A whole drive or a single partition.")

    def _after_pick(self):
        body = self.step(1, "Where to?", f"Copying {self.target_name()}")
        grp = []
        a = OptionCard(body, "Image file (.vhd)", "A single file you can open later. Windows can mount it "
                                                 "directly in Disk Management.", "vhd", grp, icon="image",
                       badge_text="Recommended")
        a.pack(fill="x", pady=5)
        b = OptionCard(body, "Raw image (.img)", "Exact sector-by-sector copy. Works with any recovery tool.",
                       "img", grp, icon="image", tone="accent")
        b.pack(fill="x", pady=5)
        c = OptionCard(body, "Another drive", "Clone straight onto a second drive, for example a new SSD or NVMe in "
                                              "a USB enclosure. Everything on it will be replaced.", "disk", grp,
                       icon="clone", tone="danger")
        c.pack(fill="x", pady=5)
        dnet = OptionCard(body, "Several disks or another PC", "Clone to many disks at once, on this PC or on "
                                                              "linked PCs over the network.", "net", grp,
                          icon="netclone", tone="teal")
        dnet.pack(fill="x", pady=5)
        self._dgrp = grp
        a.select()
        self.buttons(primary=("Next", self._dest))

    def _dest(self):
        kind = selected_value(self._dgrp)
        if kind == "net":
            from .link_screens import NetCloneWizard
            self.app.go(NetCloneWizard)
            return
        self.kind = kind
        src = self.target
        if kind in ("vhd", "img"):
            name = f"{src['dev'].name.replace(' ', '')}_{time.strftime('%Y%m%d')}.{kind}"
            p = filedialog.asksaveasfilename(defaultextension=f".{kind}", initialfile=name,
                                             filetypes=[("Disk image", f"*.{kind}")])
            if not p:
                return
            self.dest = p
            self._confirm()
        else:
            self.src_target = self.target
            body = self.step(1, "Pick the destination drive", "Everything on it will be replaced.")
            self.dest_target = None

            def chosen(t):
                self.dest_target = t
                nb.configure(state="normal")
            DrivePicker(body, self.app.inventory(), mode="disk", on_select=chosen, writes=True,
                        exclude_path=src["dev"].path, height=470).pack(fill="both", expand=True)
            nb = self.buttons(primary=("Next", self._confirm), secondary=("Back", self._after_pick))
            nb.configure(state="disabled")

    def _confirm(self):
        src = self.target if self.kind != "disk" else self.src_target
        start, count = (src["part"].start_lba, src["part"].sectors) if src["kind"] == "part" else (
            0, src["dev"].total_sectors)
        size = count * src["dev"].sector_size
        body = self.step(2, "Ready to copy", "")
        if self.kind == "disk":
            dst = self.dest_target["dev"]
            if dst.usable_size < size:
                self.result_card(body, "warn", "danger", "Destination is too small",
                                 [f"Need {human_size(size)}, it has {human_size(dst.usable_size)}."])
                self.buttons(primary=("Back", self._dest))
                return
            self.app.mascot.set_mood("warn", "confirm")
            lines = [f"Copy {human_size(size)}", f"ALL data on {dst.describe()} will be replaced."]
            if self._live(src["dev"]):
                lines.append("Windows is using this disk, so a snapshot is taken first to keep the copy consistent.")
            if src["kind"] == "disk" and dst.usable_size > size:
                lines.append(f"The new disk is {human_size(dst.usable_size - size)} bigger. That space will be "
                             f"unallocated; extend C: in Disk Management afterwards.")
            self.result_card(body, "clone", "danger", f"{src['dev'].name} to {dst.name}", lines)
            self._smart_switch(body)
            go = self.buttons(primary=("Start cloning", lambda: self._go(src, start, count)))
            go.configure(state="disabled", fg_color=P["danger"], hover_color=P["danger_hover"])
            self.confirm_box(body, f"CLONE TO {dst.name.upper()}",
                             lambda ok: go.configure(state="normal" if ok else "disabled"))
        else:
            lines = [f"Copy {human_size(size)} from {self.target_name()}", f"Saving to {self.dest}",
                     "Unreadable sectors are skipped and logged. A SHA-256 hash is recorded."]
            if self._live(src["dev"]):
                lines.append("Windows is using this disk, so a snapshot is taken first to keep the copy consistent.")
            self.result_card(body, "image", "accent", os.path.basename(self.dest), lines)
            self._smart_switch(body)
            self.buttons(primary=("Start backup", lambda: self._go(src, start, count)),
                         secondary=("Back", self._after_pick))

    def _smart_switch(self, body):
        self.smart = tk.BooleanVar(value=True)
        ctk.CTkSwitch(body, text="Copy used space only, much faster (NTFS, FAT, exFAT, ext). Other filesystems and "
                                 "OSes are copied sector by sector.", variable=self.smart,
                      font=theme.font_style("body")).pack(anchor="w", pady=(4, 4))

    @staticmethod
    def _live(dev):
        try:
            from ..vss import needs_snapshot
            return needs_snapshot(dev)
        except Exception:  # noqa: BLE001
            return False

    def _go(self, src, start, count):
        sdev = self.app.clone(src["dev"])
        snap = self._live(src["dev"])
        smart = self.smart.get() if hasattr(self, "smart") else False
        if self.kind == "disk":
            dst = self.app.clone(self.dest_target["dev"])
            fmt = "raw"
        else:
            dst, fmt = self.dest, self.kind if self.kind == "vhd" else "raw"
        _, panel = self.progress("Copying\u2026", "Fast pass first, then any tricky sectors one by one.")
        self._draw_steps(3)

        def done(res):
            self.app._inventory = None
            body = self.step(3, "Copy complete", "")
            ok = res["bad_sectors"] == 0
            self.app.mascot.set_mood("happy" if ok else "sad", "done" if ok else "sick")
            lines = [f"{human_size(res.get('copied_bytes', res['bytes']))} copied"
                     + (f" (used space of {human_size(res['bytes'])})" if res.get("copied_bytes", 0) < res["bytes"]
                        else ""), f"Unreadable sectors: {res['bad_sectors']}"]
            if res.get("snapshot"):
                lines.append(f"Copied from a snapshot of {', '.join(res['snapshot'])}")
            if res.get("grown"):
                lines.append(res["grown"])
            if res.get("sha256"):
                lines.append(f"SHA-256: {res['sha256']}")
            self.result_card(body, "check" if ok else "warn", "success" if ok else "warn",
                             "Copy finished" if ok else "Finished, but some sectors were unreadable", lines)
            self.done_actions(os.path.dirname(self.dest) if self.kind != "disk" else None)
        def stopped(info):
            self.app._inventory = None
            st = info.get("state")
            if self.kind == "disk":
                lines = (["Nothing was written to the destination drive."] if st == "untouched" else
                         [f"The destination drive {self.dest_target['dev'].name} is only partly written "
                          f"({human_size(info.get('written', 0))} copied).",
                          "It won't start Windows or open reliably like this. Clone again to finish, or wipe it "
                          "before using it for something else."])
            elif st == "complete, not hashed":
                lines = [f"The image {self.dest} is complete. Only the SHA-256 check was skipped."]
            else:
                lines = [f"The unfinished image file was deleted ({os.path.basename(str(self.dest))})."
                         if st == "removed" else f"The image file {self.dest} is incomplete: delete it."]
            if snap:
                lines.append("The snapshot of the running disk was removed.")
            lines.append("The source drive wasn't changed.")
            self.cancelled_state("Copy stopped", lines, step=3)
        self.app.run_job("Copy", lambda prog: surface.image_copy(sdev, dst, prog, start_lba=start, sectors=count,
                                                                 fmt=fmt, snapshot=snap, smart=smart), done, panel,
                         on_cancel=stopped)
