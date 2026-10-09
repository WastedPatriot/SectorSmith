"""Manage: the software library (Packages), the app Catalogue, Deployments with schedules, Clients with their
onboarding baselines, upkeep Tasks and maintenance Sessions. Each page is a table with a details panel; the
wizards that create and run things live in deploy_screens.py."""
from __future__ import annotations

import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

from ..deploy import catalogue, core, schedule
from ..util import Cancelled, Progress, cancel_scope, get_logger
from . import theme
from .deploy_screens import ACTION_LABEL, DETECTION, INSTALLER_EXTS, KIND_LABEL, LANGUAGES, SOFTWARE_STATES, \
    STATUS_LABEL, TASK_STATES, ClientEditor, DeploymentWizard, PackageBuilder, RunWizard, SessionView, TaskEditor, \
    _plural, _short, _took, attempt, export_scripts
from .manage_common import HIDDEN, EntryText, ManagePage, RowTable, client_hidden, client_text, host_text
from .screens import Screen
from .widgets import AutoScroll, Card, DataTable, DropZone, Popover, StatusPill, caption, link_button, segmented, \
    soft_button

log = get_logger()
P = theme.PALETTE
SCHEDULE_KINDS = {"": "None", "daily": "Daily", "weekly": "Weekly", "connect": "On connect"}
TRIGGERS = {"manual": "You", "schedule": "Schedule", "onboarding": "Onboarding"}


def _ago(ts):
    if not ts:
        return "-"
    t, now = time.localtime(ts), time.localtime()
    if t.tm_year == now.tm_year and t.tm_yday == now.tm_yday:
        return time.strftime("Today %H:%M", t)
    if t.tm_year == now.tm_year and t.tm_yday == now.tm_yday - 1:
        return time.strftime("Yesterday %H:%M", t)
    return time.strftime("%d %b %H:%M", t)


def _detection_text(p):
    det = p.detection or {}
    m = det.get("method", "registry")
    if m == "file":
        return "File: " + os.path.basename((det.get("value") or "").replace("\\", "/"))
    if m == "script":
        return "Script"
    if m == "msi_product_code":
        return "MSI product code"
    value = det.get("value") or p.name
    return "Name pattern" if value.startswith("re:") else "Name: " + _short(value, 28)


# ---------------------------------------------------------------------------- deployment wording
def dep_items(store, d):
    """Names of what a deployment puts on a PC."""
    if d.item_type == "bundle":
        return [p.name for p in (store.get("packages", i) for i in d.items) if p is not None]
    obj = store.get("packages" if d.item_type == "software" else "tasks", d.item_id)
    return [obj.name if obj else ("Missing package" if d.item_type == "software" else "Missing task")]


def dep_title(store, d):
    names = dep_items(store, d)
    if d.item_type == "bundle":
        label = d.name or "Bundle"
        return f"{label}: {_plural(len(names), 'package')}"
    return names[0]


def dep_target(app, store, d):
    if d.target_kind == "client":
        c = store.get("clients", d.target_value)
        return "Client: " + (client_text(app, c.name) if c else "missing")
    if d.target_kind == "machine":
        return "PC: " + host_text(app, store, d.target_value)
    return "Every PC"


def dep_state(d):
    states = TASK_STATES if d.item_type == "task" else SOFTWARE_STATES
    return states.get(d.desired, d.desired) + (f" {d.version}" if d.desired == "version" else "")


def dep_pills(d, results):
    pills = []
    if d.baseline:
        pills.append(("Baseline", "info"))
    if not d.enabled:
        return pills + [("Off", "neutral")]
    r = results.get(d.id)
    if r is None:
        pills.append(("Not run yet", "neutral"))
    elif r["failed"]:
        pills.append((f"{r['failed']} failed", "danger"))
    elif r["cancelled"]:
        pills.append(("Stopped", "neutral"))
    elif r["pending"] or r["non_compliant"]:
        pills.append(("Needs a change", "warn"))
    else:
        pills.append(("OK", "success"))
    return pills[:2]


def session_pills(s):
    sm = s.get("summary") or {}
    if s.get("error"):
        return [("Failed", "danger")]
    out = []
    if sm.get("failed"):
        out.append((f"{sm['failed']} failed", "danger"))
    elif s.get("cancelled"):
        out.append(("Stopped", "neutral"))
    elif sm.get("pending"):
        out.append((f"{sm['pending']} to change", "warn"))
    elif sm.get("non_compliant"):
        out.append((f"{sm['non_compliant']} not OK", "warn"))
    else:
        out.append(("OK", "success"))
    if sm.get("reboot"):
        out.append(("Restart", "info"))
    return out


def _confirm_remove(question):
    return messagebox.askyesno("Remove?", question)


# ---------------------------------------------------------------------------- packages
class PackagesScreen(ManagePage):
    """The software library. Drop an installer anywhere on the page to build a package."""

    def __init__(self, master, app, select=None):
        super().__init__(master, app, "Packages", "Apps in your library: how each installs silently and how "
                                                  "SectorSmith tells it's there.")
        self.dz = None
        if self.store is None:
            return
        self.new_btn = self.header_action("New package", self._new_menu, primary=True, width=140)
        self.header_action("Add from catalogue", lambda: app.go(CatalogueScreen), width=170)
        inner = self.page("Library", search="Search packages", filters=["All", "Installers", "winget", "Scripts"],
                          on_filter=lambda _v: self.fill(),
                          panel_empty=("No package selected", "Select a package to see how it installs and where "
                                                              "it's deployed.", "package"), top=self._drop_strip)
        self.card_action("Import folder...", self._import)
        self.table = DataTable(inner, [("name", "Name", 230), ("version", "Version", 90), ("kind", "Kind", 76),
                                       ("detect", "Detection", 170), ("used", "Used by", 76, "e")], height=8,
                               on_select=self._show, on_open=lambda p: app.go(PackageBuilder, package=p),
                               empty=("Your library is empty", "Drop an installer above, or add common apps from the "
                                                               "catalogue.",
                                      ("Open the catalogue", lambda: app.go(CatalogueScreen)), "package"))
        self.table.pack(fill="both", expand=True)
        self.search_var.trace_add("write", lambda *_: self.fill())
        app.drop_handlers.append(self._drop_hot)
        self.fill(select)

    def _drop_strip(self, body):
        self.dz = DropZone(body, "Drop an installer to build a package", "MSI, EXE or MSIX. SectorSmith suggests the "
                                                                          "silent switches and detection.",
                           height=72, icon="upload", wide=True)
        self.dz.pack(fill="x", pady=(0, 12))

    def _drop_hot(self, kind, _p):
        if self.dz is not None and self.dz.winfo_exists():
            self.dz.hot(kind == "enter")

    def accept_files(self, paths):
        found = [p for p in paths if p.lower().endswith(INSTALLER_EXTS)]
        if not found:
            self.app.toast("Drop an installer (.msi, .exe or .msix) to add it to the library.", "warn")
            return
        if len(found) > 1:
            self.app.toast(f"One at a time: starting with {os.path.basename(found[0])}.", "warn")
        self.app.mascot.say("drop")
        self.app.go(PackageBuilder, path=found[0])

    def _new_menu(self):
        Popover(self.new_btn, [("From an installer...", self._browse, "upload", None),
                               ("From the catalogue", lambda: self.app.go(CatalogueScreen), "search", None),
                               ("A winget ID", lambda: self.app.go(PackageBuilder, mode="winget"), "deploy", None),
                               ("A script or command", lambda: self.app.go(PackageBuilder, mode="script"), "task",
                                None),
                               None,
                               ("Import an exported folder...", self._import, "folder", None)], width=270,
                align="right")

    def _browse(self):
        p = filedialog.askopenfilename(title="Pick an installer",
                                       filetypes=[("Installers", " ".join("*" + e for e in INSTALLER_EXTS)),
                                                  ("All files", "*")])
        if p:
            self.app.go(PackageBuilder, path=p)

    def _import(self):
        folder = filedialog.askdirectory(title="Pick an exported package folder (it has a package.json)")
        if not folder:
            return
        if not os.path.exists(os.path.join(folder, "package.json")):
            self.app.toast("That folder has no package.json. Pick a folder made with Export.", "warn")
            return
        ok, pkg = attempt(self.app, "import that package", lambda: self.store.import_package(folder))
        if ok:
            self.app.toast(f"Imported {pkg.name}")
            self.fill(pkg.id)

    def _rows(self):
        q = self.search_var.get().lower().split()
        f = self.filter.get()
        kinds = {"Installers": ("msi", "exe", "msix"), "winget": ("winget",), "Scripts": ("script",)}.get(f)
        out = []
        for p in sorted(self.store.packages, key=lambda p: p.name.lower()):
            if kinds and p.kind not in kinds:
                continue
            hay = f"{p.name} {p.publisher} {p.winget_id} {p.category} {p.installer}".lower()
            if all(w in hay for w in q):
                out.append(p)
        return out

    def fill(self, select=None):
        self.table.clear()
        rows = self._rows()
        sel = None
        for p in rows:
            n = len(self.store.used_by(p.id))
            iid = self.table.add((p.name, p.version or "Newest", KIND_LABEL.get(p.kind, p.kind), _detection_text(p),
                                  n or "-"), data=p)
            if select is not None and p.id == select:
                sel = iid
        self.table.done()
        total = len(self.store.packages)
        self.card_count.configure(text=_plural(total, "package"))
        self.count_lbl.configure(text=f"Showing {len(rows)} of {total}" if len(rows) != total else "")
        if sel:
            self.table.tree.selection_set(sel)
            self.table.tree.see(sel)
            self._show(self.table.rows[sel])
        else:
            self.panel.clear()

    def _show(self, p):
        used = self.store.used_by(p.id)
        source = (p.installer or (f"winget {p.winget_id}" if p.winget_id else "")
                  or ("Chocolatey" if p.install.startswith("choco ") else "Command"))
        det = p.detection or {}
        how = DETECTION.get(det.get("method"), det.get("method", ""))
        what = p.product_code if det.get("method") == "msi_product_code" else \
            ("see the script" if det.get("method") == "script" else (det.get("value") or p.name))
        fields = [("Version", p.version or "Newest"), ("Kind", KIND_LABEL.get(p.kind, p.kind)),
                  ("Publisher", p.publisher or "-"), ("Source", source), ("Detect by", f"{how}: {_short(what, 60)}")]
        if p.category:
            fields.append(("Category", p.category))
        sections = [("Install", _short(p.install, 240), True)]
        if used:
            sections.append(("Used by", "\n".join(f"{dep_title(self.store, d)}  ·  {dep_target(self.app, self.store, d)}"
                                                  for d in used[:8]), False))
        if p.notes:
            sections.append(("Notes", "\n".join(_short(n, 220) for n in p.notes[:3]), False))
        pills = [(f"In {_plural(len(used), 'deployment')}", "success") if used else ("Not deployed", "neutral")]
        self.panel.show("package", "accent", p.name, p.publisher, pills=pills, fields=fields, sections=sections,
                        actions=[("Deploy...", lambda: self.app.go(DeploymentWizard, item=("software", p.id)),
                                  "primary"),
                                 ("Edit", lambda: self.app.go(PackageBuilder, package=p), "secondary"),
                                 ("Export scripts...", lambda: export_scripts(self.app, self.store, p), "secondary"),
                                 ("Remove", lambda: self._remove(p), "danger")])

    def _remove(self, p):
        if not _confirm_remove(f"Remove {p.name} from the library?\n\nIts installer copy goes, and it leaves its "
                               "deployments and baselines. Nothing is changed on any PC."):
            return
        ok, _ = attempt(self.app, "remove it", lambda: self.store.delete("packages", p.id))
        if ok:
            self.app.toast(f"Removed {p.name}")
            self.fill()


# ---------------------------------------------------------------------------- catalogue
class CatalogueScreen(ManagePage):
    """Curated MSP apps plus a live winget search when winget is on this PC."""
    runner = None          # tests put a fake winget here: runner(args, timeout) -> (code, text)
    debounce_ms = 450

    def __init__(self, master, app, query=""):
        super().__init__(master, app, "Catalogue", "Common apps with the silent install, uninstall and detection "
                                                   "worked out. Add one and it's ready to deploy.")
        self.apps = catalogue.load()
        self.live, self.live_query, self.live_note = [], "", ""
        self._prog, self._job = None, None
        self.winget = type(self).runner is not None or bool(catalogue.winget_path())
        if self.store is None:
            return
        self.header_action("Packages", lambda: app.go(PackagesScreen), width=120)
        inner = self.page("Apps", search="Search apps, publishers or winget IDs",
                          panel_empty=("Pick an app", "Select an app to see how it installs, then add it to your "
                                                      "library.", "search"))
        self.cats = ["All categories"] + catalogue.categories(self.apps)
        self.cat = ctk.CTkOptionMenu(self.bar, values=self.cats, width=180, height=34,
                                     font=theme.font_style("body"), command=lambda _v: self.fill())
        self.cat.pack(side="left", padx=(12, 0))
        self.status = ctk.CTkLabel(self.card_head, text="", font=theme.font_style("small"), text_color=P["muted"])
        self.status.pack(side="right")
        self.table = DataTable(inner, [("name", "App", 230), ("category", "Category", 120),
                                       ("id", "winget ID", 220), ("lib", "Library", 80)], height=9,
                               on_select=self._show, on_open=self._add,
                               empty=("No apps match", "Try another word. With winget on this PC, matches from the "
                                                       "winget source show here too.", None, "search"))
        self.table.pack(fill="both", expand=True)
        if query:
            self.search_var.set(query)
        self.search_var.trace_add("write", lambda *_: self._typed())
        self.fill()
        if query:
            self._typed()
        self._winget_status()

    def destroy(self):
        if self._prog is not None:
            self._prog.cancel()
        super().destroy()

    def _winget_status(self, text=None):
        if text is None:
            text = ("winget found: searches include the winget source" if self.winget else
                    "winget isn't on this PC, so this is the curated list")
        self.status.configure(text=text)

    # -- live search ---------------------------------------------------------
    def _typed(self):
        self.fill()
        if self._job is not None:
            self.after_cancel(self._job)
        self._job = self.after(self.debounce_ms, self._live)

    def _live(self):
        self._job = None
        q = self.search_var.get().strip()
        if self._prog is not None:
            self._prog.cancel()  # a newer search replaces the one still running
            self._prog = None
        if not self.winget or len(q) < 2:
            self.live, self.live_query = [], ""
            self._winget_status()
            self.fill()
            return
        prog = self._prog = Progress(1, f"winget search {q}")
        self._winget_status(f"Searching winget for '{_short(q, 24)}'...")
        runner = type(self).runner  # read from the class so a plain function stays unbound

        def work():
            with cancel_scope(prog.check):
                return catalogue.winget_search(q, runner=runner)

        def done(rows):
            if prog is not self._prog or not self.winfo_exists():
                return
            self._prog = None
            self.live, self.live_query = rows, q
            extra = len([r for r in rows if not self._curated(r)])
            self._winget_status(f"winget: {_plural(extra, 'more match')}" if extra else "winget: nothing new")
            self.fill()

        def failed(e):
            if prog is not self._prog or isinstance(e, Cancelled) or not self.winfo_exists():
                return
            self._prog = None
            if isinstance(e, FileNotFoundError):
                self.winget = False
            self.live = []
            self._winget_status(f"winget search didn't work ({_short(str(e), 60)}), showing the curated list")
            self.fill()
        self.app.background(work, done, failed)

    def _curated(self, row):
        wid = (row.get("winget") or "").lower()
        return any((a.get("winget") or "").lower() == wid for a in self.apps)

    # -- table ---------------------------------------------------------------
    def _rows(self):
        q = self.search_var.get()
        cat = self.cat.get()
        cat = "" if cat == self.cats[0] else cat
        rows = catalogue.search(self.apps, q, cat)
        if not cat and self.live and q.strip() == self.live_query:
            rows += [r for r in self.live if not self._curated(r)]
        return rows

    def fill(self, select=None):
        self.table.clear()
        rows = self._rows()
        sel = None
        for a in rows:
            have = catalogue.in_library(self.store, a)
            ident = a.get("winget") or ("Your own installer" if a.get("placeholder") else a.get("choco") or "-")
            iid = self.table.add((a["name"], a.get("category") or "-", ident, "Added" if have else ""), data=a,
                                 tags=("muted",) if a.get("placeholder") else ())
            if select is not None and a["name"] == select:
                sel = iid
        self.table.done()
        self.card_count.configure(text=_plural(len(rows), "app"))
        if sel:
            self.table.tree.selection_set(sel)
            self._show(self.table.rows[sel])

    def _show(self, a):
        have = catalogue.in_library(self.store, a)
        det = a.get("detect") or {}
        val = det.get("value") or a["name"]
        rule = ("Matches the pattern " + val[3:]) if val.startswith("re:") else f"Name has '{val}'"
        fields = [("Publisher", a.get("publisher") or "-"), ("Category", a.get("category") or "-"),
                  ("winget", a.get("winget") or "-"), ("Chocolatey", a.get("choco") or "-"),
                  ("Detect by", "Your installer's details" if a.get("placeholder") else rule)]
        if a.get("version"):
            fields.insert(2, ("Newest", a["version"]))
        sections = []
        if a.get("note"):
            sections.append(("Note", a["note"], False))
        if not a.get("placeholder") and a.get("winget"):
            sections.append(("Installs with", core.WINGET_INSTALL.replace("{winget_id}", a["winget"]), True))
        pills = [("In library", "success")] if have else []
        if a.get("source") == "winget":
            pills.append(("winget search", "info"))
        if a.get("placeholder"):
            actions = [("Build it from your installer", lambda: self.app.go(PackageBuilder), "primary")]
        elif have is not None:
            actions = [("Deploy...", lambda: self.app.go(DeploymentWizard, item=("software", have.id)), "primary"),
                       ("Open in Packages", lambda: self.app.go(PackagesScreen, select=have.id), "secondary")]
        else:
            actions = [("Add to library", lambda: self._add(a), "primary")]
            if a.get("choco"):
                actions.append(("Add with Chocolatey", lambda: self._add(a, "choco"), "secondary"))
        self.panel.show("package", "info" if a.get("source") == "winget" else "accent", a["name"],
                        a.get("publisher") or "", pills=pills, fields=fields, sections=sections, actions=actions)

    def _add(self, a, source="winget"):
        if a.get("placeholder"):
            self.app.toast(a.get("note") or "Use your own installer for this one.", "info")
            self.app.go(PackageBuilder)
            return
        if not a.get("winget") and source == "winget":
            source = "choco"
        ok, pkg = attempt(self.app, f"add {a['name']}", lambda: catalogue.add(self.store, a, source))
        if ok:
            self.app.toast(f"{pkg.name} is in your library. Deploy it from here or from Packages.")
            self.fill(select=a["name"])


# ---------------------------------------------------------------------------- deployments
class DeploymentsScreen(ManagePage):
    def __init__(self, master, app, select=None):
        super().__init__(master, app, "Deployments", "What each PC should have, where it applies and when it's "
                                                     "checked.")
        if self.store is None:
            return
        self.header_action("New deployment", lambda: app.go(DeploymentWizard), primary=True, width=150)
        self.header_action("Run maintenance", lambda: app.go(RunWizard), width=150)
        inner = self.page("Deployments", search="Search deployments",
                          filters=["All", "Scheduled", "Baselines", "Off"], on_filter=lambda _v: self.fill(),
                          panel_empty=("No deployment selected", "Select one to see its target, schedule and last "
                                                                 "result.", "deploy"), top=self._running)
        _ok, sessions = attempt(app, "read past sessions", lambda: self.store.sessions(limit=150))
        self.results = core.deployment_results(sessions or [])
        self.table = RowTable(inner, [("what", "Deployment", 0, "strong"), ("target", "Target", 150),
                                      ("next", "Next run", 100, "muted"), ("status", "Status", 190, "pills")],
                              on_select=self._show,
                              empty=("No deployments yet", "A deployment says what should be on which PCs, for "
                                                           "example: keep 7-Zip up to date on every PC.",
                                     ("New deployment", lambda: app.go(DeploymentWizard)), "deploy"))
        self.table.pack(fill="both", expand=True)
        self.search_var.trace_add("write", lambda *_: self.fill())
        self.fill(select)

    def _running(self, body):
        from .manage_schedule import running
        prog = running(self.app)
        if prog is None:
            return
        bar = ctk.CTkFrame(body, corner_radius=12, fg_color=P["info_soft"])
        bar.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(bar, text=f"Scheduled maintenance is running: {prog.label}", font=theme.font_style("body"),
                     text_color=P["text"]).pack(side="left", padx=16, pady=10)
        link_button(bar, "Stop it", prog.cancel, tone="danger").pack(side="right", padx=10)

    def _rows(self):
        q = self.search_var.get().lower().split()
        f = self.filter.get()
        out = []
        for d in self.store.deployments:
            if f == "Scheduled" and not (d.schedule or {}).get("kind"):
                continue
            if f == "Baselines" and not d.baseline:
                continue
            if f == "Off" and d.enabled:
                continue
            hay = " ".join(dep_items(self.store, d) + [dep_target(self.app, self.store, d), d.name]).lower()
            if all(w in hay for w in q):
                out.append(d)
        return out

    def fill(self, select=None):
        self.table.clear()
        rows = self._rows()
        pick = None
        for d in rows:
            self.table.add((_short(dep_title(self.store, d), 26), dep_target(self.app, self.store, d),
                            schedule.next_text(d), dep_pills(d, self.results)), data=d)
            if select is not None and d.id == select:
                pick = d
        self.table.done()
        total = len(self.store.deployments)
        self.card_count.configure(text=_plural(total, "deployment"))
        self.count_lbl.configure(text=f"Showing {len(rows)} of {total}" if len(rows) != total else "")
        if pick is not None:
            self.table.select(pick)
        else:
            self.panel.clear()

    def _show(self, d):
        r = self.results.get(d.id)
        last = f"{_ago(r['when'])} on {host_text(self.app, self.store, r['machine'])}" if r else "Not run yet"
        fields = [("Wants", dep_state(d)), ("Target", dep_target(self.app, self.store, d)),
                  ("New PCs only", "Yes" if d.onboarding_only else "No"),
                  ("Schedule", schedule.describe(d) or "When you run maintenance"),
                  ("Next run", schedule.next_text(d)), ("Last result", last)]
        sections = []
        if d.item_type == "bundle":
            sections.append(("Packages", "\n".join(dep_items(self.store, d)) or "None left", False))
        actions = [("Run now...", lambda: self.app.go(RunWizard, only=[d.id]), "primary")]
        if d.baseline:
            c = self.store.get("clients", d.target_value)
            if c is not None:
                actions.append(("Edit baseline", lambda: self.app.go(BaselineEditor, client=c), "secondary"))
        actions.append(("Remove", lambda: self._remove(d), "danger"))
        title = dep_title(self.store, d)
        icon, tone = ("task", "success") if d.item_type == "task" else ("deploy", "info")
        body = self.panel.show(icon, tone, title, "Baseline for new PCs" if d.baseline else
                               ("Upkeep task" if d.item_type == "task" else "Software"),
                               pills=dep_pills(d, self.results), fields=fields, sections=sections, actions=actions)
        on = tk.BooleanVar(value=d.enabled)
        ctk.CTkSwitch(body, text="Switched on", variable=on, font=theme.font_style("body"),
                      command=lambda: self._toggle(d, on)).pack(anchor="w", pady=(0, 6))
        ScheduleEditor(body, d, lambda s: self._save_schedule(d, s)).pack(fill="x")

    def _toggle(self, d, var):
        d.enabled = bool(var.get())
        ok, _ = attempt(self.app, "save that change", lambda: self.store.upsert("deployments", d))
        if ok:
            self.fill(d.id)

    def _save_schedule(self, d, s):
        if (s or {}).get("kind") == (d.schedule or {}).get("kind") and s:
            s["since"] = (d.schedule or {}).get("since", s["since"])  # same kind: keep its run history
        d.schedule = s
        if not s:
            d.last_runs = {}
        ok, _ = attempt(self.app, "save the schedule", lambda: self.store.upsert("deployments", d))
        if ok:
            self.app.toast(f"Schedule saved: {schedule.describe(d)}" if s else "Schedule removed")
            self.fill(d.id)

    def _remove(self, d):
        what = dep_title(self.store, d)
        if not _confirm_remove(f"Remove the deployment {what}?\n\nPCs keep whatever is installed now."):
            return
        ok, _ = attempt(self.app, "remove it", lambda: self.store.delete("deployments", d.id))
        if ok:
            self.app.toast("Deployment removed")
            self.fill()


class ScheduleEditor(ctk.CTkFrame):
    """None, daily, weekly or on connect, a time and 'check only'. on_save(schedule dict or {})."""

    def __init__(self, master, d, on_save, compact=False):
        super().__init__(master, fg_color="transparent")
        s = (d.schedule or {}) if d is not None else {}
        self.on_save = on_save
        if not compact:
            caption(self, "Schedule").pack(fill="x", pady=(8, 4))
        self.kind = segmented(self, list(SCHEDULE_KINDS.values()), command=lambda _v: self._kind_changed(),
                              height=30)
        self.kind.set(SCHEDULE_KINDS.get(s.get("kind", ""), "None"))
        self.kind.pack(anchor="w")
        self.when = ctk.CTkFrame(self, fg_color="transparent")
        self.time = ctk.CTkEntry(self.when, width=70, height=30, font=theme.font_style("body"))
        self.time.insert(0, s.get("time", "09:00"))
        self.day = ctk.CTkOptionMenu(self.when, values=list(schedule.DAYS), width=130, height=30,
                                     font=theme.font_style("body"))
        self.day.set(schedule.DAYS[int(s.get("day", 0)) % 7])
        self.check_only = tk.BooleanVar(value=s.get("mode") == "detect")
        self.mode = ctk.CTkCheckBox(self, text="Check only, change nothing", variable=self.check_only,
                                    font=theme.font_style("small"))
        self.help = ctk.CTkLabel(self, text="", font=theme.font_style("small"), text_color=P["muted"], anchor="w",
                                 justify="left", wraplength=260)
        self.save_btn = None
        if not compact:
            self.save_btn = soft_button(self, "Save schedule", self.save, width=130)
        self._kind_changed()

    def _kind(self):
        label = self.kind.get()
        return next((k for k, v in SCHEDULE_KINDS.items() if v == label), "")

    def _kind_changed(self):
        k = self._kind()
        for w in (self.when, self.mode, self.help) + ((self.save_btn,) if self.save_btn else ()):
            w.pack_forget()
        for w in self.when.winfo_children():
            w.pack_forget()
        if k in ("daily", "weekly"):
            self.when.pack(anchor="w", pady=(8, 0))
            if k == "weekly":
                self.day.pack(side="left", padx=(0, 8))
            self.time.pack(side="left")
        if k:
            self.mode.pack(anchor="w", pady=(8, 0))
        self.help.configure(text={
            "": "Runs only when you start maintenance yourself.",
            "daily": "SectorSmith isn't a service: a missed time runs when the app next starts, or when the PC "
                     "next connects.",
            "weekly": "SectorSmith isn't a service: a missed time runs when the app next starts, or when the PC "
                      "next connects.",
            "connect": "Runs each time a linked PC it applies to connects.",
        }[k] + ("" if not k or self.check_only.get() else " Changes apply without asking, so test it on one PC "
                                                         "first."))
        self.help.pack(anchor="w", pady=(6, 0))
        if self.save_btn is not None:
            self.save_btn.pack(anchor="w", pady=(10, 0))

    def value(self):
        """The schedule dict, or {} for none. Raises ValueError for a bad time."""
        k = self._kind()
        if not k:
            return {}
        return schedule.make(k, self.time.get(), schedule.DAYS.index(self.day.get()),
                             "detect" if self.check_only.get() else "full")

    def save(self):
        try:
            s = self.value()
        except ValueError as e:
            self.winfo_toplevel().toast(str(e), "warn")
            return
        self.on_save(s)


# ---------------------------------------------------------------------------- clients and baselines
class ClientsScreen(ManagePage):
    def __init__(self, master, app, select=None):
        super().__init__(master, app, "Clients", "Named groups of PCs, each with a baseline: the packages every new "
                                                 "PC for that client gets.")
        if self.store is None:
            return
        self.header_action("New client", lambda: app.go(ClientEditor), primary=True, width=130)
        inner = self.page("Clients", search="Search clients or PCs",
                          panel_empty=("No client selected", "Select a client to see its PCs and baseline.",
                                       "building"))
        self.table = DataTable(inner, [("name", "Client", 220), ("pcs", "PCs", 60, "e"),
                                       ("baseline", "Baseline", 130), ("deps", "Deployments", 124, "e")], height=8,
                               on_select=self._show, on_open=lambda c: app.go(ClientEditor, client=c),
                               empty=("No clients yet", "A client is a customer or an office. Give it a baseline and "
                                                        "every new PC for it gets the same apps.",
                                      ("New client", lambda: app.go(ClientEditor)), "building"))
        self.table.pack(fill="both", expand=True)
        self.search_var.trace_add("write", lambda *_: self.fill())
        self.fill(select)

    def fill(self, select=None):
        self.table.clear()
        q = self.search_var.get().lower().split()
        sel = None
        clients = sorted(self.store.clients, key=lambda c: c.name.lower())
        for c in clients:
            hidden = client_hidden(self.app, c)
            hay = "" if hidden else f"{c.name} {' '.join(c.machines)}".lower()
            if q and not all(w in hay for w in q):
                continue
            base = self.store.baseline_for(c.id)
            deps = [d for d in self.store.deployments if d.target_kind == "client" and d.target_value == c.id]
            iid = self.table.add((HIDDEN if hidden else c.name, len(c.machines),
                                  _plural(len(base.items), "package") if base else "Not set", len(deps) or "-"),
                                 data=c)
            if select is not None and c.id == select:
                sel = iid
        self.table.done()
        self.card_count.configure(text=_plural(len(clients), "client"))
        if sel:
            self.table.tree.selection_set(sel)
            self._show(self.table.rows[sel])
        else:
            self.panel.clear()

    def _show(self, c):
        hidden = client_hidden(self.app, c)
        base = self.store.baseline_for(c.id)
        names = [p.name for p in (self.store.get("packages", i) for i in (base.items if base else [])) if p]
        pcs = "Hidden while presenting" if hidden else (", ".join(c.machines) or "None yet")
        fields = [("PCs", pcs), ("Baseline", _plural(len(names), "package") if base else "Not set")]
        if base:
            fields.append(("Wants", SOFTWARE_STATES.get(base.desired, base.desired)))
        sections = [("Baseline packages", "\n".join(names), False)] if names else []
        if base and base.items:
            actions = [("Onboard a new PC...", lambda: self.app.go(RunWizard, onboard=c.id), "primary"),
                       ("Edit baseline", lambda: self.app.go(BaselineEditor, client=c), "secondary")]
        else:
            actions = [("Set up a baseline", lambda: self.app.go(BaselineEditor, client=c), "primary")]
        actions += [("Edit client", lambda: self.app.go(ClientEditor, client=c), "secondary"),
                    ("Remove", lambda: self._remove(c), "danger")]
        self.panel.show("building", "accent", HIDDEN if hidden else c.name,
                        _plural(len(c.machines), "PC"), pills=[("Baseline set", "success")] if base else
                        [("No baseline", "neutral")], fields=fields, sections=sections, actions=actions)

    def _remove(self, c):
        name = HIDDEN if client_hidden(self.app, c) else c.name
        if not _confirm_remove(f"Remove the client {name}?\n\nIts deployments and baseline go too. Its PCs "
                               "aren't changed."):
            return
        ok, _ = attempt(self.app, "remove it", lambda: self.store.delete("clients", c.id))
        if ok:
            self.app.toast(f"Removed {name}")
            self.fill()


class BaselineEditor(Screen):
    """Pick the packages every new PC for a client gets; quick-add common apps from the catalogue."""
    guide_topic = "deploy"

    def __init__(self, master, app, client):
        name = HIDDEN if client_hidden(app, client) else client.name
        super().__init__(master, app, f"Baseline for {name}", "Every new PC for this client gets these packages when "
                                                              "you onboard it, or at the end of a migration.")
        self.client = client
        body = self.new_body()
        from .deploy_screens import open_store
        self.store = open_store(self, body)
        if self.store is None:
            return
        base = self.store.baseline_for(client.id)
        self.vars: dict = {}
        self.picked = set(base.items if base else [])
        cols = ctk.CTkFrame(body, fg_color="transparent")
        cols.pack(fill="both", expand=True)
        cols.grid_columnconfigure(0, weight=3, uniform="b")
        cols.grid_columnconfigure(1, weight=2, uniform="b")
        cols.grid_rowconfigure(0, weight=1)
        left = Card(cols, "Library packages", pad=0)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        left.head.pack_configure(padx=20)
        self.count = ctk.CTkLabel(left.head, text="", font=theme.font_style("small"), text_color=P["muted"])
        self.count.pack(side="left", padx=(8, 0), pady=(3, 0))
        self.list = AutoScroll(left.body)
        self.list.pack(fill="both", expand=True, padx=(20, 6), pady=(0, 6))
        row = ctk.CTkFrame(left.body, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(4, 8))
        ctk.CTkLabel(row, text="Each package", font=theme.font_style("small"), text_color=P["muted"]).pack(
            side="left")
        self.desired = segmented(row, [SOFTWARE_STATES["installed"], SOFTWARE_STATES["latest"]], height=30)
        self.desired.set(SOFTWARE_STATES.get(base.desired if base else "installed", SOFTWARE_STATES["installed"]))
        self.desired.pack(side="left", padx=(10, 0))
        right = Card(cols, "Add common apps", pad=0)
        right.grid(row=0, column=1, sticky="nsew")
        right.head.pack_configure(padx=20)
        entry = ctk.CTkEntry(right.body, height=34, placeholder_text="Search the catalogue",
                             font=theme.font_style("body"))
        entry.pack(fill="x", padx=20, pady=(0, 8))
        self.q = EntryText(entry)
        self.cat_list = AutoScroll(right.body)
        self.cat_list.pack(fill="both", expand=True, padx=(20, 6))
        self.q.trace_add("write", lambda *_: self._catalogue())
        self._library()
        self._catalogue()
        self.buttons(primary=("Save baseline", self._save),
                     secondary=("Back", lambda: app.go(ClientsScreen, select=client.id)))

    def _library(self):
        for w in self.list.winfo_children():
            w.destroy()
        self.vars = {}
        pkgs = sorted(self.store.packages, key=lambda p: (p.id not in self.picked, p.name.lower()))
        if not pkgs:
            ctk.CTkLabel(self.list, text="Your library is empty. Add apps from the catalogue on the right.",
                         font=theme.font_style("body"), text_color=P["muted"]).pack(anchor="w", pady=12)
        for p in pkgs:
            v = tk.BooleanVar(value=p.id in self.picked)
            r = ctk.CTkFrame(self.list, fg_color="transparent")
            r.pack(fill="x", pady=2)
            ctk.CTkCheckBox(r, text=p.name, variable=v, font=theme.font_style("body_strong"),
                            command=lambda p=p, v=v: self._tick(p.id, v)).pack(side="left")
            ctk.CTkLabel(r, text=" · ".join(x for x in (KIND_LABEL.get(p.kind, p.kind), p.version) if x),
                         font=theme.font_style("small"), text_color=P["muted"]).pack(side="right", padx=8)
            self.vars[p.id] = v
        self.count.configure(text=f"{len(self.picked)} chosen")

    def _tick(self, pid, v):
        (self.picked.add if v.get() else self.picked.discard)(pid)
        self.count.configure(text=f"{len(self.picked)} chosen")

    def _catalogue(self):
        for w in self.cat_list.winfo_children():
            w.destroy()
        apps = [a for a in catalogue.search(catalogue.load(), self.q.get()) if not a.get("placeholder")][:12]
        for a in apps:
            r = ctk.CTkFrame(self.cat_list, fg_color="transparent")
            r.pack(fill="x", pady=3, padx=(0, 8))
            t = ctk.CTkFrame(r, fg_color="transparent")
            t.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(t, text=a["name"], font=theme.font_style("body_strong"), text_color=P["text"],
                         anchor="w", height=18).pack(anchor="w")
            ctk.CTkLabel(t, text=a.get("category", ""), font=theme.font_style("small"), text_color=P["muted"],
                         anchor="w", height=16).pack(anchor="w")
            have = catalogue.in_library(self.store, a)
            if have is not None and have.id in self.picked:
                StatusPill(r, "Chosen", "success").pack(side="right", padx=8)
            else:
                link_button(r, "+ Add", lambda a=a: self._add(a)).pack(side="right", padx=4)

    def _add(self, a):
        ok, pkg = attempt(self.app, f"add {a['name']}", lambda: catalogue.add(self.store, a))
        if ok:
            self.picked.add(pkg.id)
            self._library()
            self._catalogue()

    def _save(self):
        desired = "latest" if self.desired.get() == SOFTWARE_STATES["latest"] else "installed"
        ids = [p.id for p in self.store.packages if p.id in self.picked]
        ok, d = attempt(self.app, "save the baseline", lambda: self.store.set_baseline(self.client.id, ids, desired))
        if ok:
            self.app.toast(f"Baseline saved: {_plural(len(ids), 'package')}" if d else "Baseline removed")
            self.app.go(ClientsScreen, select=self.client.id)


# ---------------------------------------------------------------------------- tasks
class TasksScreen(ManagePage):
    def __init__(self, master, app, select=None):
        super().__init__(master, app, "Tasks", "Upkeep scripts: a check that says whether a PC is fine, and a fix "
                                               "that runs when it isn't.")
        if self.store is None:
            return
        self.header_action("New task", lambda: app.go(TaskEditor), primary=True, width=120)
        inner = self.page("Tasks", search="Search tasks",
                          panel_empty=("No task selected", "Select a task to see its check and fix scripts.",
                                       "task"))
        self.table = DataTable(inner, [("name", "Task", 240), ("lang", "Language", 110), ("check", "Check", 200),
                                       ("used", "Used by", 76, "e")], height=8, on_select=self._show,
                               on_open=lambda t: app.go(TaskEditor, task=t),
                               empty=("No tasks yet", "For example: make sure the Print Spooler is running, and "
                                                      "start it when it isn't.",
                                      ("New task", lambda: app.go(TaskEditor)), "task"))
        self.table.pack(fill="both", expand=True)
        self.search_var.trace_add("write", lambda *_: self.fill())
        self.fill(select)

    def fill(self, select=None):
        self.table.clear()
        q = self.search_var.get().lower().split()
        tasks = sorted(self.store.tasks, key=lambda t: t.name.lower())
        for t in tasks:
            if not all(w in f"{t.name} {t.description}".lower() for w in q):
                continue
            first = next((ln.strip() for ln in t.test.splitlines() if ln.strip()), "")
            used = [d for d in self.store.deployments if d.item_type == "task" and d.item_id == t.id]
            iid = self.table.add((t.name, LANGUAGES.get(t.language, t.language), _short(first, 32), len(used) or "-"),
                                 data=t)
            if select == t.id:
                self.table.tree.selection_set(iid)
        self.table.done()
        self.card_count.configure(text=_plural(len(tasks), "task"))
        self.panel.clear()

    def _show(self, t):
        used = [d for d in self.store.deployments if d.item_type == "task" and d.item_id == t.id]
        sections = [("Check", _short(t.test, 400), True)]
        if t.set:
            sections.append(("Fix", _short(t.set, 400), True))
        self.panel.show("task", "success", t.name, t.description,
                        pills=[(f"In {_plural(len(used), 'deployment')}", "success") if used else
                               ("Not deployed", "neutral")],
                        fields=[("Language", LANGUAGES.get(t.language, t.language))], sections=sections,
                        actions=[("Deploy...", lambda: self.app.go(DeploymentWizard, item=("task", t.id)), "primary"),
                                 ("Edit", lambda: self.app.go(TaskEditor, task=t), "secondary"),
                                 ("Remove", lambda: self._remove(t), "danger")])

    def _remove(self, t):
        if not _confirm_remove(f"Remove the task {t.name}?\n\nIts deployments go too."):
            return
        ok, _ = attempt(self.app, "remove it", lambda: self.store.delete("tasks", t.id))
        if ok:
            self.app.toast(f"Removed {t.name}")
            self.fill()


# ---------------------------------------------------------------------------- sessions
class SessionsScreen(ManagePage):
    LIMIT = 80

    def __init__(self, master, app):
        super().__init__(master, app, "Sessions", "Every maintenance run: what each PC needed, what changed and how "
                                                  "it went.")
        if self.store is None:
            return
        self.header_action("Run maintenance", lambda: app.go(RunWizard), primary=True, width=160)
        inner = self.page("Sessions", search="Search PCs or clients",
                          filters=["All", "Problems", "Scheduled", "Onboarding"], on_filter=lambda _v: self.fill(),
                          panel_empty=("No session selected", "Select a run to see what it found and changed.",
                                       "clock"))
        _ok, sessions = attempt(app, "read past sessions", lambda: self.store.sessions(limit=self.LIMIT))
        self.sessions = sessions or []
        self.table = RowTable(inner, [("machine", "PC", 0, "strong"), ("when", "When", 120, "muted"),
                                      ("client", "Client", 130), ("result", "Result", 190, "pills")],
                              on_select=self._show, on_open=lambda s: app.go(SessionView, session=s),
                              empty=("Nothing has run yet", "Run maintenance checks your PCs, shows what they need "
                                                            "and applies it once you confirm.",
                                     ("Run maintenance", lambda: app.go(RunWizard)), "clock"))
        self.table.pack(fill="both", expand=True)
        self.search_var.trace_add("write", lambda *_: self.fill())
        self.fill()

    def fill(self):
        self.table.clear()
        q = self.search_var.get().lower().split()
        f = self.filter.get()
        shown = 0
        for s in self.sessions:
            sm = s.get("summary") or {}
            if f == "Problems" and not (sm.get("failed") or s.get("cancelled")):
                continue
            if f in ("Scheduled", "Onboarding") and s.get("trigger") != f.lower().replace("scheduled", "schedule"):
                continue
            machine = host_text(self.app, self.store, s.get("machine", "?"))
            client = client_text(self.app, s.get("client") or "") or "-"
            if q and not all(w in f"{machine} {client}".lower() for w in q):
                continue
            self.table.add((machine, _ago(s.get("started")), client, session_pills(s)), data=s)
            shown += 1
        self.table.done()
        self.card_count.configure(text=f"latest {len(self.sessions)}" if len(self.sessions) >= self.LIMIT else
                                  _plural(len(self.sessions), "session"))
        self.count_lbl.configure(text=f"Showing {shown}" if shown != len(self.sessions) else "")
        self.panel.clear()

    def _show(self, s):
        mode = "Check only" if s.get("mode") == "detect" else "Check and fix"
        took = _took(s["finished"] - s["started"]) if s.get("finished") and s.get("started") else "-"
        fields = [("When", _ago(s.get("started"))), ("Mode", mode),
                  ("Started by", TRIGGERS.get(s.get("trigger") or "manual", "You")),
                  ("Client", client_text(self.app, s.get("client") or "") or "-"), ("Took", took)]
        lines = [f"{a.get('name', '?')}: {STATUS_LABEL.get(a.get('status'), a.get('status', ''))}"
                 + (f" ({ACTION_LABEL.get(a.get('action'), a.get('action'))})"
                    if a.get("action") not in (None, "none") else "") for a in s.get("actions", [])[:12]]
        if len(s.get("actions", [])) > 12:
            lines.append(f"...and {len(s['actions']) - 12} more")
        sections = [("Items", "\n".join(lines) or "No deployments applied to this PC", False)]
        self.panel.show("clock", "danger" if (s.get("summary") or {}).get("failed") else "success",
                        host_text(self.app, self.store, s.get("machine", "?")), mode, pills=session_pills(s),
                        fields=fields, sections=sections,
                        actions=[("Open session", lambda: self.app.go(SessionView, session=s), "primary"),
                                 ("Run maintenance again", lambda: self.app.go(RunWizard), "secondary")])
