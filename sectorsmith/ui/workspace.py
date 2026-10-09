"""Landing pages for the rail categories: Drives (inventory table), Machines (linked PCs) and Jobs (history)."""
from __future__ import annotations

import os
import time
from tkinter import filedialog

import customtkinter as ctk

from .. import jobs
from ..util import human_size
from . import theme
from .screens import CloneWizard, HealthWizard, Screen, WipeWizard, _columns
from .integrations_ui import local_client_lines, machine_name, open_screenconnect, sc_instance
from .shell import machine_status
from .widgets import Card, DataTable, EmptyState, IconBadge, Popover, Skeleton, StatusPill, TaskCard, caption, \
    divider, link_button, secondary_button

P = theme.PALETTE


def _when(ts):
    if not ts:
        return ""
    t = time.localtime(ts)
    if time.strftime("%Y%m%d", t) == time.strftime("%Y%m%d"):
        return time.strftime("Today %H:%M", t)
    return time.strftime("%d %b %H:%M" if t.tm_year == time.localtime().tm_year else "%d %b %Y", t)


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
WHEN = ("Any time", "Today", "Last 7 days", "Last 30 days", "Custom range")
SHOW_MAX = 500  # rows drawn at once; the export always has every match


def _since_days(days):
    """Local midnight at the start of the day `days - 1` days ago, so 'Last 7 days' includes today."""
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday - (days - 1), 0, 0, 0, 0, 0, -1))


class MenuButton(ctk.CTkFrame):
    """Filter chip: 'Client: All' that opens a menu of choices. Reachable with Tab; Enter or Space opens it."""

    def __init__(self, master, label, options, value, on_pick, width=170):
        super().__init__(master, fg_color="transparent")
        self.label, self.options, self.on_pick = label, options, on_pick
        self.btn = secondary_button(self, "", self.open, width=width, height=34)
        self.btn.configure(anchor="w")
        self.btn.pack()
        self.set(value)

    def set(self, value):
        self.value = value
        shown = next((text for text, v in self.options if v == value), self.options[0][0])
        self.btn.configure(text=f"{self.label}: {shown}   ▾")

    def open(self):
        items = [(text, lambda v=v: self._pick(v), None, "current" if v == self.value else None)
                 for text, v in self.options]
        Popover(self.btn, items, width=max(220, self.btn.winfo_width()))

    def _pick(self, value):
        self.set(value)
        self.on_pick(value)


class JobsScreen(Screen):
    """Every job run on this PC, across sessions (sectorsmith/jobs.py), with filters, reports and CSV export."""

    guide_topic = "start"
    refreshable = True

    def __init__(self, master, app):
        super().__init__(master, app, "Job history", "Every job run on this PC, stamped with client, ticket and "
                                                     "technician.", show_back=False)
        self.header_action("Export CSV", self._export, width=120)
        # filters last for the session, so leaving the page and coming back keeps them
        self.f = getattr(app, "_jobs_filters", None) or dict(client=None, result=None, when="Any time", text="",
                                                             start="", end="")
        app._jobs_filters = self.f
        self.all = self._records()
        self.shown = []
        self._after = None
        body = self.new_body()
        self._filters(body)
        card = Card(body, "History", pad=0)
        card.head.pack_configure(padx=20)
        card.pack(fill="both", expand=True)
        self.count = ctk.CTkLabel(card.head, text="", font=theme.font_style("small"), text_color=P["muted"])
        self.count.pack(side="left", padx=(8, 0), pady=(3, 0))
        self.table = DataTable(card.body, [("when", "When", 130), ("task", "Task", 180), ("detail", "Target", 190),
                                           ("client", "Client", 120), ("ticket", "Ticket", 75),
                                           ("tech", "Technician", 100), ("result", "Result", 90),
                                           ("report", "Report", 90)], height=9,
                               on_open=self._open_report, on_select=self._select)
        self.table.pack(fill="both", expand=True)
        bar = ctk.CTkFrame(card.body, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=(8, 12))
        self.sel_lbl = ctk.CTkLabel(bar, text="Select a job to open its certificate or report.",
                                    font=theme.font_style("small"), text_color=P["muted"], anchor="w")
        self.sel_lbl.pack(side="left", fill="x", expand=True)
        self.folder_btn = secondary_button(bar, "Show in folder", self._open_folder, width=130, height=32)
        self.report_btn = secondary_button(bar, "Open report", lambda: self._open_report(self.selected), width=120,
                                           height=32)
        self.report_btn.pack(side="right", padx=(8, 0))
        self.folder_btn.pack(side="right")
        self.selected = None
        self._select(None)
        self._deploy(body)
        self._fill()

    # -- data ---------------------------------------------------------------------------------------------------
    def _records(self):
        """The history as shown: while presenting, other clients' names are already hidden, so neither the table,
        the search nor the export can reveal them."""
        cur = self.app.context.get("client")
        out = []
        for j in self.app.jobs:
            if theme.presentation() and j.get("client") and j["client"] != cur:
                j = dict(j, client="Hidden")
            out.append(j)
        return out

    def _range(self):
        when = self.f["when"]
        if when == "Today":
            return _since_days(1), None
        if when == "Last 7 days":
            return _since_days(7), None
        if when == "Last 30 days":
            return _since_days(30), None
        if when == "Custom range":
            return jobs.day_range(self.f["start"], self.f["end"])
        return None, None

    def _matches(self):
        try:
            since, until = self._range()
            bad = False
        except ValueError:
            since = until = None
            bad = True
        for e in (self.start_e, self.end_e):
            e.configure(border_color=P["danger"] if bad else P["control_border"])
        return jobs.filter_jobs(self.all, client=self.f["client"], result=self.f["result"], since=since,
                                until=until, text=self.f["text"])

    # -- filters ------------------------------------------------------------------------------------------------
    def _filters(self, body):
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", pady=(0, 12))
        self.search = _Field(row, self.f["text"], "Search task, target, ticket, technician", 280, self._soon)
        self.search.entry.pack(side="left")
        clients = sorted({j.get("client") for j in self.all if j.get("client")}, key=str.lower)
        if theme.presentation():
            cur = self.app.context.get("client")
            clients = [cur] if cur in clients else []
        opts = [("All", None)] + [(c, c) for c in clients] + [("No client", "")]
        if self.f["client"] not in [v for _t, v in opts]:
            self.f["client"] = None
        self.client_m = MenuButton(row, "Client", opts, self.f["client"], lambda v: self._set("client", v))
        self.client_m.pack(side="left", padx=(8, 0))
        self.result_m = MenuButton(row, "Result", [("Any", None)] + [(r, r) for r in jobs.RESULTS], self.f["result"],
                                   lambda v: self._set("result", v), width=150)
        self.result_m.pack(side="left", padx=(8, 0))
        self.when_m = MenuButton(row, "When", [(w, w) for w in WHEN], self.f["when"], self._set_when, width=190)
        self.when_m.pack(side="left", padx=(8, 0))
        self.range_row = ctk.CTkFrame(row, fg_color="transparent")
        self.start_v = _Field(self.range_row, self.f["start"], "From YYYY-MM-DD", 130, self._soon, mono=True)
        self.start_e = self.start_v.entry
        self.start_e.pack(side="left", padx=(8, 0))
        ctk.CTkLabel(self.range_row, text="to", font=theme.font_style("small"), text_color=P["muted"]).pack(
            side="left", padx=6)
        self.end_v = _Field(self.range_row, self.f["end"], "To YYYY-MM-DD", 130, self._soon, mono=True)
        self.end_e = self.end_v.entry
        self.end_e.pack(side="left")
        if self.f["when"] == "Custom range":
            self.range_row.pack(side="left")
        self.clear_btn = link_button(row, "Clear filters", self._clear)
        self.clear_btn.pack(side="right")

    def _set(self, key, value):
        self.f[key] = value
        self._fill()

    def _set_when(self, value):
        self.f["when"] = value
        if value == "Custom range":
            self.range_row.pack(side="left", before=self.clear_btn)
            if not self.start_v.get():
                self.start_v.set(time.strftime("%Y-%m-%d", time.localtime(_since_days(30))))
            self.start_e.focus_set()
        else:
            self.range_row.pack_forget()
        self._fill()

    def _clear(self):
        self.f.update(client=None, result=None, when="Any time", text="", start="", end="")
        self.client_m.set(None)
        self.result_m.set(None)
        self.when_m.set("Any time")
        self.range_row.pack_forget()
        self.search.set("")
        self.start_v.set("")
        self.end_v.set("")
        self._fill()

    def _soon(self):
        """Typing filters after a short pause, not on every key."""
        if self._after is not None:
            self.after_cancel(self._after)
        self._after = self.after(200, self._fill)

    # -- table --------------------------------------------------------------------------------------------------
    def _fill(self):
        self._after = None
        if not self.winfo_exists():
            return
        self.f.update(text=self.search.get(), start=self.start_v.get(), end=self.end_v.get())
        self.shown = self._matches()
        t = self.table
        t.clear()
        filtered = len(self.shown) != len(self.all)
        t._empty_spec = ("No matching jobs", "Change or clear the filters to see more.", ("Clear filters", self._clear),
                         "jobs") if self.all else \
            ("No jobs yet", "Every job you run is listed here with its client, ticket and result, and stays after "
                            "SectorSmith closes.", None, "jobs")
        for j in reversed(self.shown[-SHOW_MAX:]):
            t.add((_when(j["started"]), j["task"], j.get("detail") or "-", j.get("client") or "-",
                   f"#{j['ticket']}" if j.get("ticket") else "-", j.get("technician") or "-", j["result"],
                   _report_kind(j.get("report"))), data=j)
        t.done()
        n, total = len(self.shown), len(self.all)
        text = f"{n} of {total} job{'s' if total != 1 else ''}" if filtered else \
            f"{total} job{'s' if total != 1 else ''}"
        if n > SHOW_MAX:
            text += f", newest {SHOW_MAX} shown (the export has them all)"
        self.count.configure(text=text)
        self._select(None)

    def _select(self, job):
        self.selected = job
        has = bool(job and job.get("report"))
        for b in (self.report_btn, self.folder_btn):
            b.configure(state="normal" if has else "disabled")
        if job is None:
            self.sel_lbl.configure(text="Select a job to open its certificate or report.", text_color=P["muted"])
        else:
            what = os.path.basename(job["report"]) if has else "No certificate or report saved for this job"
            self.sel_lbl.configure(text=f"{job['task']}  ·  {what}", text_color=P["text"] if has else P["muted"])

    def _open_report(self, job):
        path = (job or {}).get("report")
        if not path:
            self.app.toast("This job has no saved certificate or report.", "info")
        elif not os.path.exists(path):
            self.app.toast(f"The file has moved or been deleted: {path}", "warn")
        else:
            self.app.open_folder(path)

    def _open_folder(self):
        path = (self.selected or {}).get("report")
        folder = os.path.dirname(path) if path else ""
        if folder and os.path.isdir(folder):
            self.app.open_folder(folder)
        else:
            self.app.toast("That folder isn't there any more.", "warn")

    def _export(self):
        rows = self.shown
        if not rows:
            self.app.toast("No jobs to export. Change or clear the filters.", "warn")
            return
        path = filedialog.asksaveasfilename(title="Export job history", defaultextension=".csv",
                                            initialfile=f"SectorSmith jobs {time.strftime('%Y-%m-%d')}.csv",
                                            filetypes=[("CSV (Excel)", "*.csv")])
        if not path:
            return
        try:
            n = jobs.export_csv(rows, path)
        except OSError as e:
            self.app.toast(f"Couldn't save the CSV: {e.strerror or e}", "danger")
            return
        self.app.toast(f"Exported {n} job{'s' if n != 1 else ''} to {os.path.basename(path)}.")

    # -- Deploy sessions ----------------------------------------------------------------------------------------
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
        card.pack(fill="x", pady=(16, 0))
        t = DataTable(card.body, [("when", "When", 130), ("machine", "Machine", 200), ("mode", "Mode", 130),
                                  ("client", "Client", 150), ("result", "Result", 200)], height=4,
                      on_open=self._open_session)
        t.pack(fill="x", pady=(0, 8))
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


class _Field:
    """A CTkEntry that keeps its placeholder (a textvariable hides it) with get() and set() like a StringVar."""

    def __init__(self, master, value, placeholder, width, on_change, mono=False):
        self.entry = ctk.CTkEntry(master, width=width, height=34, placeholder_text=placeholder,
                                  font=theme.mono(12) if mono else theme.font_style("body"))
        if value:
            self.entry.insert(0, value)
        self.on_change = on_change
        self.entry.bind("<KeyRelease>", lambda _e: on_change())
        self.entry.bind("<<Paste>>", lambda _e: self.entry.after_idle(on_change))

    def get(self):
        return self.entry.get()

    def set(self, value):
        self.entry.delete(0, "end")  # puts the placeholder back when empty and not focused
        if value:
            self.entry.insert(0, value)
        self.on_change()


def _report_kind(path):
    if not path:
        return "-"
    return "Certificate" if "certificate" in os.path.basename(path).lower() else "Report"
