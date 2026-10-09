"""Landing pages for the rail categories: Drives (inventory table), Machines (linked PCs) and Jobs (history)."""
from __future__ import annotations

import time

import customtkinter as ctk

from ..util import human_size
from . import theme
from .screens import CloneWizard, HealthWizard, Screen, WipeWizard, _columns
from .integrations_ui import local_client_lines, machine_name, open_screenconnect, sc_instance
from .shell import machine_status
from .widgets import Card, DataTable, EmptyState, IconBadge, Skeleton, StatusPill, TaskCard, caption, divider, \
    secondary_button

P = theme.PALETTE


def _when(ts):
    if not ts:
        return ""
    t = time.localtime(ts)
    return time.strftime("Today %H:%M" if time.strftime("%Y%m%d", t) == time.strftime("%Y%m%d") else "%d %b %H:%M",
                         t)


def _tasks_row(parent, specs):
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x")
    for i, (icon, title, desc, cmd, tone) in enumerate(specs):
        TaskCard(row, icon, title, desc, cmd, tone).grid(row=0, column=i, sticky="nsew",
                                                         padx=(0 if i == 0 else 8, 0 if i == len(specs) - 1 else 8))
        row.grid_columnconfigure(i, weight=1, uniform="q")
    return row


# ---------------------------------------------------------------------------- drives
class DrivesScreen(Screen):
    guide_topic = "health"
    refreshable = True

    def __init__(self, master, app):
        super().__init__(master, app, "Drives", "Every drive on This PC and in opened disk images. Pick one to "
                                                "check, image or erase it.", show_back=False)
        self.header_action("Open disk image", app.open_image_dialog, width=150)
        body = self.new_body()
        _tasks_row(body, [
            ("health", "Health check", "SMART verdict and a full surface scan", lambda: self._task(HealthWizard),
             "accent"),
            ("clone", "Image and clone", "Smart copy to VHD, IMG or another drive", lambda: self._task(CloneWizard),
             "accent"),
            ("netclone", "Network clone", "One disk to many PCs, in Machines",
             lambda: app.open_target("machines", "netclone"), "teal"),
        ])
        self.card = Card(body, "This PC", ("Refresh", self.refresh_drives), pad=0)
        self.card.pack(fill="both", expand=True, pady=(16, 0))
        self.card.head.pack_configure(padx=20)
        self.count = ctk.CTkLabel(self.card.head, text="", font=theme.font_style("small"), text_color=P["muted"])
        self.count.pack(side="left", padx=(8, 0), pady=(3, 0))
        self.area = ctk.CTkFrame(self.card.body, fg_color="transparent")
        self.area.pack(fill="both", expand=True)
        self.bar = ctk.CTkFrame(self.card.body, fg_color="transparent")
        self.bar.pack(fill="x", padx=16, pady=(8, 12))
        self.sel_lbl = ctk.CTkLabel(self.bar, text="Select a drive to work on it.", font=theme.font_style("small"),
                                    text_color=P["muted"])
        self.sel_lbl.pack(side="left")
        self.acts = []
        for text, cls in (("Erase and certify", WipeWizard), ("Image and clone", CloneWizard),
                          ("Health check", HealthWizard)):
            b = secondary_button(self.bar, text, lambda c=cls: self._task(c, self.selected), width=140, height=32,
                                 text_tone="danger" if cls is WipeWizard else "text")
            b.pack(side="right", padx=(8, 0))
            b.configure(state="disabled")
            self.acts.append(b)
        self.selected = None
        Skeleton(self.area, rows=4, row_height=40).pack(fill="x", padx=16, pady=8)
        self.after(60, self._fill)

    def _task(self, cls, dev=None):
        self.app.preselect_path = dev.path if dev is not None else None
        self.app.go(cls)

    def refresh_drives(self):
        self.app._inventory = None
        for w in self.area.winfo_children():
            w.destroy()
        Skeleton(self.area, rows=4, row_height=40).pack(fill="x", padx=16, pady=8)
        self.after(60, self._fill)

    def _fill(self):
        try:
            inv = self.app.inventory()
        except Exception as e:  # noqa: BLE001
            inv = []
            self.app.toast(f"Could not list drives: {e}", "warn")
        if not self.winfo_exists():
            return
        for w in self.area.winfo_children():
            w.destroy()
        self.count.configure(text=f"{len(inv)} drive{'s' if len(inv) != 1 else ''}")
        t = DataTable(self.area, [("drive", "Drive", 300), ("serial", "Serial", 150), ("bus", "Bus", 70),
                                  ("size", "Size", 90, "e"), ("parts", "Layout", 130), ("status", "Status", 120)],
                      height=8, on_select=self._select, on_open=lambda d: self._task(HealthWizard, d),
                      empty=("No drives found", "Run as administrator to see physical drives, or open a disk image.",
                             ("Open disk image", self.app.open_image_dialog), "drive"))
        t.pack(fill="both", expand=True)
        for dev, pt, err in inv:
            usb = dev.removable or dev.bus in ("USB", "SD", "MMC")
            # Treeview colours whole rows only, so status is a word in its own column
            status = "Can't read" if err else "System disk" if dev.is_system else "Disk image" if dev.is_image \
                else "USB" if usb else "Ready"
            layout = f"{pt.scheme} · {len(pt.partitions)} partition{'s' if len(pt.partitions) != 1 else ''}" \
                if pt else "-"
            t.add((f"{dev.name}  ·  {dev.model or 'Disk'}", theme.mask(dev.serial) or "-", dev.bus,
                   human_size(dev.size), layout, status), data=dev)
        t.done()

    def _select(self, dev):
        self.selected = dev
        self.sel_lbl.configure(text=f"{dev.name}  ·  {dev.model or 'Disk'}", text_color=P["text"])
        for b in self.acts:
            b.configure(state="normal")


# ---------------------------------------------------------------------------- machines
class MachinesScreen(Screen):
    guide_topic = "connect"
    refreshable = True

    def __init__(self, master, app):
        super().__init__(master, app, "Linked machines", "PCs linked to this one over SectorSmith Link. Move a user "
                                                         "or clone a disk between them.", show_back=False)
        self.header_action("Connect a machine", app.open_connect, primary=True, width=170)
        body = self.new_body()
        _tasks_row(body, [
            ("migrate", "Migrate user", "Move a user's files and settings to a new PC",
             lambda: app.open_target("machines", "migrate"), "teal"),
            ("netclone", "Network clone", "Clone one disk to one or many disks across PCs",
             lambda: app.open_target("machines", "netclone"), "teal"),
            ("usb", "USB boot stick", "Link a PC that will not start Windows", app.open_connect, "accent"),
        ])
        ms = app.machines()
        card = Card(body, "Machines", count=len(ms), pad=0)
        card.head.pack_configure(padx=20)
        card.pack(fill="both", expand=True, pady=(16, 0))
        head = ctk.CTkFrame(card.body, fg_color=P["surface_2"], corner_radius=0, height=34)
        head.pack(fill="x")
        sc = sc_instance(app)  # ScreenConnect column only once it is set up in Settings
        cols = (("Machine", 0), ("Address", 150), ("User", 130), ("Status", 120)) + ((("Remote", 210),) if sc else ())
        for i, (name, _w) in enumerate(cols):
            caption(head, name).grid(row=0, column=i, sticky="w", padx=(20 if i == 0 else 0, 12), pady=9)
        _columns(head, cols)
        for m in ms:
            divider(card.body).pack(fill="x")
            info = m.info() if m.is_local else (m.info_cache or {})
            r = ctk.CTkFrame(card.body, fg_color="transparent", height=52)
            r.pack(fill="x")
            who = ctk.CTkFrame(r, fg_color="transparent")
            who.grid(row=0, column=0, sticky="w", padx=(20, 12), pady=8)
            IconBadge(who, "pc", 32, "teal" if not m.is_local else "neutral").pack(side="left", padx=(0, 12))
            t = ctk.CTkFrame(who, fg_color="transparent")
            t.pack(side="left")
            ctk.CTkLabel(t, text=info.get("hostname", m.label), font=theme.font_style("body_strong"),
                         text_color=P["text"], anchor="w", height=18).pack(anchor="w")
            ctk.CTkLabel(t, text=info.get("os", ""), font=theme.font_style("small"), text_color=P["muted"],
                         anchor="w", height=16).pack(anchor="w")
            ctk.CTkLabel(r, text="This PC" if m.is_local else getattr(m, "address", ""), font=theme.mono(12),
                         text_color=P["text_2"], width=150, anchor="w").grid(row=0, column=1, sticky="w",
                                                                             padx=(0, 12))
            ctk.CTkLabel(r, text=info.get("user", ""), font=theme.font_style("body"), text_color=P["text_2"],
                         width=130, anchor="w").grid(row=0, column=2, sticky="w", padx=(0, 12))
            pill = ctk.CTkFrame(r, fg_color="transparent", width=120)
            pill.grid(row=0, column=3, sticky="w", padx=(0, 12))
            StatusPill(pill, machine_status(m)).pack(anchor="w")
            if sc:
                secondary_button(r, "Connect with ScreenConnect", lambda n=machine_name(m): open_screenconnect(app, n),
                                 width=210, height=30).grid(row=0, column=4, sticky="w", padx=(0, 12))
            _columns(r, cols)
        for line in local_client_lines() if sc else ():
            divider(card.body).pack(fill="x")
            ctk.CTkLabel(card.body, text=f"ScreenConnect client on This PC:  {line}", font=theme.font_style("small"),
                         text_color=P["muted"], anchor="w", justify="left", wraplength=860).pack(fill="x", padx=20,
                                                                                                  pady=8)
        if len(ms) == 1:
            divider(card.body).pack(fill="x")
            EmptyState(card.body, "No other machines linked yet", "Run one PowerShell command on another PC, or "
                                                                  "boot it from the SectorSmith USB stick.",
                       ("Connect a machine", app.open_connect), icon="machines").pack(pady=24)

    def machines_changed(self):
        self.app._refresh_screen()


# ---------------------------------------------------------------------------- jobs
class JobsScreen(Screen):
    guide_topic = "start"
    refreshable = True

    def __init__(self, master, app):
        super().__init__(master, app, "Job history", "Jobs run since SectorSmith started, stamped with client, "
                                                     "ticket and technician.",
                         show_back=False)
        body = self.new_body()
        card = Card(body, "This session", count=len(app.jobs), pad=0)
        card.head.pack_configure(padx=20)
        card.pack(fill="both", expand=True)
        t = DataTable(card.body, [("when", "When", 130), ("task", "Task", 220), ("detail", "Target", 240),
                                  ("client", "Client", 130), ("ticket", "Ticket", 90), ("tech", "Technician", 110),
                                  ("result", "Result", 100)], height=7,
                      empty=("No jobs yet", "Every job you run is listed here with its client, ticket and result.",
                             None, "jobs"))
        t.pack(fill="both", expand=True, pady=(0, 8))
        cur = app.context.get("client")
        for j in reversed(app.jobs):
            client = j["client"]
            if theme.presentation() and client and client != cur:
                client = "Hidden"
            t.add((_when(j["started"]), j["task"], j.get("detail") or "-", client or "-",
                   f"#{j['ticket']}" if j["ticket"] else "-", j["technician"] or "-", j["result"]), data=j)
        t.done()
        self._deploy(body)

    def _deploy(self, body):
        try:
            sessions = self.app.deploy_store().sessions(limit=50)
        except Exception:  # noqa: BLE001  no Deploy library yet
            return
        if not sessions:
            return
        card = Card(body, "Maintenance sessions", ("Open in Manage", lambda: self.app.open_target("manage", "sessions")),
                    count=len(sessions), pad=0)
        card.head.pack_configure(padx=20)
        card.pack(fill="both", expand=True, pady=(16, 0))
        t = DataTable(card.body, [("when", "When", 130), ("machine", "Machine", 200), ("mode", "Mode", 130),
                                  ("client", "Client", 150), ("result", "Result", 200)], height=5,
                      on_open=self._open_session)
        t.pack(fill="both", expand=True, pady=(0, 8))
        cur = self.app.context.get("client")
        for s in sessions:
            sm = s.get("summary") or {}
            bad = sm.get("failed", 0)
            client = s.get("client") or "-"
            if theme.presentation() and client not in ("-", cur):
                client = "Hidden"
            t.add((_when(s.get("started")), s.get("machine", "?"),
                   "Check only" if s.get("mode") == "detect" else "Check and fix", client,
                   f"{bad} failed" if bad else "Done"), data=s)
        t.done()

    def _open_session(self, s):
        from .deploy_screens import SessionView
        self.app.go(SessionView, session=s)
