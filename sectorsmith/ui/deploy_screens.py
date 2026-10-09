"""Wizards and editors for Manage (SectorSmith Deploy): package builder, new deployment, client and task editors,
run maintenance and the session view. The list pages (Packages, Catalogue, Deployments, Clients, Tasks, Sessions)
are in manage_screens.py."""
from __future__ import annotations

import dataclasses
import os
import re
import time
import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk

from ..deploy import analyze, core, schedule
from ..util import Cancelled, get_logger, human_time
from . import theme
from .link_screens import MachinePicker
from .screens import Screen
from .widgets import DataTable, DropZone, IconBadge, OptionCard, Pill, bind_all, caption, ghost_button, segmented

log = get_logger()
P = theme.PALETTE

BADGE = "PREVIEW"
PAGES = ["Library", "Deployments", "Clients", "Tasks", "Sessions"]
INSTALLER_EXTS = (".msi", ".exe", ".msix", ".msixbundle", ".appx", ".appxbundle")

SOFTWARE_STATES = {"latest": "Keep up to date", "installed": "Installed", "version": "Set version",
                   "uninstalled": "Removed", "ignore": "Leave alone"}
STATE_HELP = {
    "latest": "Installs it where it's missing and updates older versions to the one in the library.",
    "installed": "Installs it where it's missing. Any version already there is left alone.",
    "version": "Installs exactly this version, updating or rolling back if needed.",
    "uninstalled": "Removes it wherever it's found.",
    "ignore": "Does nothing. Handy for switching a client or PC off a wider deployment.",
    "enforce": "Runs the check, and the fix if the check fails, then checks again.",
    "audit": "Runs the check and reports. Nothing is changed.",
}
TASK_STATES = {"enforce": "Check and fix", "audit": "Check only"}
DETECTION = {"registry": "Name in Add/Remove Programs", "msi_product_code": "MSI product code",
             "file": "A file exists", "script": "Detection script"}
LANGUAGES = {"powershell": "PowerShell", "shell": "Shell (sh)"}
KIND_LABEL = {"msi": "MSI", "exe": "EXE", "msix": "MSIX", "winget": "winget", "script": "Script"}
ACTION_LABEL = {"none": "Nothing to do", "install": "Install", "upgrade": "Update", "reinstall": "Roll back",
                "uninstall": "Remove", "set": "Fix", "audit": "Report only"}
STATUS_LABEL = {"compliant": "OK", "failed": "Failed", "pending": "Needs a change", "non-compliant": "Not OK",
                "cancelled": "Stopped"}
WINGET_INSTALL = core.WINGET_INSTALL
WINGET_UNINSTALL = core.WINGET_UNINSTALL


def _key(mapping, label):
    return next((k for k, v in mapping.items() if v == label), label)


def hostname(m):
    try:
        info = m.info() if m.is_local else m.info_cache
        return info.get("hostname") or m.label
    except Exception:  # noqa: BLE001
        return m.label


def machine_name(m):
    return f"This PC ({hostname(m)})" if m.is_local else m.label


def _when(ts):
    return time.strftime("%d %b %Y, %H:%M", time.localtime(ts)) if ts else ""


def _took(sec):
    return f"{sec:.1f} s" if sec < 60 else human_time(sec)


def _wanted(text):
    """plan() gives 'latest 1.2' or 'enforce'; show the same words the deployment screens use."""
    word, _, ver = (text or "").partition(" ")
    label = SOFTWARE_STATES.get(word) or TASK_STATES.get(word) or word
    return f"{label} ({ver})" if ver else label


def _tidy(note):
    # analyze() notes use a long dash between clauses; split them into sentences instead
    return re.sub(r"\s+\u2014\s+(\w)", lambda m: ". " + m.group(1).upper(), note)


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _short(text, n=90):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 3] + "..."


def attempt(app, what, fn):
    """Run a library call; on failure log it and show a toast instead of raising. Returns (ok, result)."""
    try:
        return True, fn()
    except Exception as e:  # noqa: BLE001
        log.error("Deploy: couldn't %s: %s", what, e)
        app.toast(f"Couldn't {what}: {e}", "danger")
        return False, None


def open_store(screen, body):
    """The shared library, or None after showing why it couldn't be opened."""
    try:
        return screen.app.deploy_store()
    except Exception as e:  # noqa: BLE001
        log.error("Deploy library failed to load: %s", e)
        screen.app.mascot.set_mood("sad", "error")
        screen.result_card(body, "warn", "danger", "Couldn't open the Deploy library",
                           [str(e), "", "It lives in %LOCALAPPDATA%\\SectorSmith\\deploy. Details are in the log."])
        screen.buttons(primary=("Back to home", screen.app.home))
        return None


def export_scripts(app, store, pkg):
    folder = filedialog.askdirectory(title=f"Export {pkg.name} to which folder?")
    if not folder:
        return

    def done(out):
        app.toast(f"Exported to {out}")
        app.mascot.say(text="Install, Uninstall and Detect scripts are ready.")
    app.background(lambda: store.export_package(pkg, folder), done,
                   lambda e: app.toast(f"Couldn't export {pkg.name}: {e}", "danger"))


def go_manage(app, page="Library", **kw):
    """Open a Manage list page: Library (Packages), Deployments, Clients, Tasks or Sessions."""
    from . import manage_screens as M
    cls = {"Library": M.PackagesScreen, "Deployments": M.DeploymentsScreen, "Clients": M.ClientsScreen,
           "Tasks": M.TasksScreen, "Sessions": M.SessionsScreen}.get(page, M.PackagesScreen)
    return app.go(cls, **kw)


def __getattr__(name):
    # the old tabbed Deploy screen is now the Manage pages; Packages is where it used to open
    if name == "DeployScreen":
        from .manage_screens import PackagesScreen
        return PackagesScreen
    raise AttributeError(name)


# ---------------------------------------------------------------------------
def badge(parent, icon, tone, size=36, bg="surface"):
    return IconBadge(parent, icon, size, tone, bg=bg)


def small_button(master, text, command, tone="text_2", width=70):
    return ctk.CTkButton(master, text=text, command=command, width=width, height=30, corner_radius=8,
                         fg_color="transparent", hover_color=P["danger_soft"] if tone == "danger" else P["hover"],
                         text_color=P[tone], font=theme.font_style("body_strong"))


def soft_button(master, text, command, width=None):
    return ctk.CTkButton(master, text=text, command=command, height=32, corner_radius=8,
                         width=width or max(90, len(text) * 8 + 24), fg_color=P["accent_soft"],
                         hover_color=P["selected"], text_color=P["accent"], font=theme.font_style("body_strong"))


def item_row(parent, icon, tone, title, sub="", pills=()):
    """A pickable card: icon tile, title with pills, one line of detail, and room for buttons on the right."""
    card = ctk.CTkFrame(parent, corner_radius=12, fg_color=P["surface"], border_width=1, border_color=P["border"])
    card.pack(fill="x", pady=3, padx=(0, 6))
    card.badge = badge(card, icon, tone)
    card.badge.pack(side="left", padx=(14, 12), pady=10)
    card.actions = ctk.CTkFrame(card, fg_color="transparent")
    card.actions.pack(side="right", padx=(0, 10))
    t = ctk.CTkFrame(card, fg_color="transparent")
    t.pack(side="left", fill="x", expand=True, pady=8)
    top = ctk.CTkFrame(t, fg_color="transparent")
    top.pack(anchor="w")
    ctk.CTkLabel(top, text=title, font=theme.font_style("body_strong"), text_color=P["text"], height=20).pack(
        side="left")
    for text, ptone in pills:
        Pill(top, text, ptone).pack(side="left", padx=(8, 0))
    if sub:
        ctk.CTkLabel(t, text=sub, font=theme.font_style("small"), text_color=P["muted"], anchor="w", justify="left",
                     wraplength=520, height=16).pack(anchor="w")
    return card


def paint_card(c, on):
    c.configure(border_color=P["accent"] if on else P["border"], fg_color=P["selected"] if on else P["surface"])
    c.badge.set_bg("selected" if on else "surface")


def make_selectable(rows, card, value, on_pick, multi=False):
    """Pickable cards in the same style as the drive and machine pickers. multi: clicking toggles."""
    rows.append(card)
    card.on = False

    def click(_e=None):
        if multi:
            card.on = not card.on
            paint_card(card, card.on)
        else:
            for c in rows:
                c.on = c is card
                paint_card(c, c.on)
        on_pick(value)
    card.click = click
    bind_all(card, "<Button-1>", click)


TONE_TAG = {"bad": ("danger",), "todo": ("warn",), "good": ("success",), "": ()}


def action_tree(parent, cols, height=8):
    """A themed table in a bordered card. Returns the DataTable; add rows with add_action_row."""
    wrap = ctk.CTkFrame(parent, corner_radius=12, fg_color=P["surface"], border_width=1, border_color=P["border"])
    wrap.pack(fill="both", expand=True)
    table = DataTable(wrap, cols, height=height)
    table.pack(fill="both", expand=True, padx=1, pady=(1, 8))
    return table


def add_action_row(table, values, tag=""):
    return table.add(values, tags=TONE_TAG.get(tag, ()))


def _tag(a):
    st = a.get("status")
    if st == "failed":
        return "bad"
    if st == "compliant":
        return "good"
    return "todo" if a.get("action") not in (None, "none") else ""


class MachineChecklist(ctk.CTkFrame):
    """Machine cards like MachinePicker, but you can tick several (or one, with single=True)."""

    def __init__(self, master, app, on_change, single=False):
        super().__init__(master, fg_color="transparent")
        self.app, self.on_change, self.single = app, on_change, single
        self.selected = []
        self.cards = []
        self.render()

    def render(self):
        for w in self.winfo_children():
            w.destroy()
        self.cards = []
        machines = self.app.machines()
        self.selected = [m for m in self.selected if m in machines]
        for i, m in enumerate(machines):
            card = ctk.CTkFrame(self, corner_radius=12, fg_color=P["surface"], border_width=1,
                                border_color=P["border"])
            card.grid(row=i // 3, column=i % 3, sticky="ew", padx=(0, 10), pady=4)
            b = badge(card, "pc", "neutral" if m.is_local else "teal", 32)
            b.pack(side="left", padx=(12, 10), pady=10)
            t = ctk.CTkFrame(card, fg_color="transparent")
            t.pack(side="left", padx=(0, 16))
            ctk.CTkLabel(t, text=machine_name(m), font=theme.font_style("body_strong"), text_color=P["text"],
                         height=18).pack(anchor="w")
            sub = ("Linked · " + m.address) if not m.is_local else "Where SectorSmith is running"
            ctk.CTkLabel(t, text=sub, font=theme.font_style("small"), text_color=P["muted"], height=16).pack(
                anchor="w")
            self.cards.append((card, b, m))

            def click(_e=None, card=card, b=b, m=m):
                on = m not in self.selected
                if self.single:
                    self.selected = [m] if on else []
                else:
                    (self.selected.append if on else self.selected.remove)(m)
                for c2, b2, m2 in self.cards:
                    self._paint(c2, b2, m2 in self.selected)
                self.on_change(list(self.selected))
            card.click = click
            bind_all(card, "<Button-1>", click)
            self._paint(card, b, m in self.selected)
        add = ctk.CTkButton(self, text="+  Link another PC", height=54, corner_radius=12, fg_color="transparent",
                            border_width=1, border_color=P["border_strong"], hover_color=P["hover"],
                            text_color=P["accent"], font=theme.font_style("body_strong"), command=self.app.open_connect)
        n = len(machines)
        add.grid(row=n // 3, column=n % 3, sticky="w", pady=4)

    @staticmethod
    def _paint(card, b, on):
        card.configure(border_color=P["accent"] if on else P["border"], fg_color=P["selected"] if on else P["surface"])
        b.set_bg("selected" if on else "surface")


# ---------------------------------------------------------------------------
class PackageBuilder(Screen):
    """Drop an installer (or start from winget / a script), check what analyze suggests, save it."""
    guide_topic = "deploy"

    def __init__(self, master, app, path=None, package=None, mode=None):
        super().__init__(master, app, "New package", steps=["Source", "Details", "Saved"], badge=BADGE)
        self.pkg = package
        self.path = None
        self.dz = None
        self.status = None
        self._saving = False
        self.store = open_store(self, self.new_body())
        if self.store is None:
            return
        if package is not None:
            self._details(dataclasses.asdict(package))
        elif path:
            self._source()
            self.accept_files([path])
        elif mode in ("winget", "script"):
            self._blank(mode)
        else:
            self._source()

    def _back_to_library(self):
        go_manage(self.app, "Library", select=self.pkg.id if self.pkg is not None else None)

    # -- step 1 -------------------------------------------------------------
    def _source(self):
        body = self.step(0, "New package", "Drop an installer, or start from the catalogue, a winget ID or your own "
                                           "script.")
        self.app.mascot.set_mood("idle", text="Got an installer for me?")
        self.dz = DropZone(body, "Drop an installer here", "MSI, EXE or MSIX", height=150, icon="upload")
        self.dz.pack(fill="x")
        self.app.drop_handlers.append(lambda kind, _p: self.dz is not None and self.dz.winfo_exists()
                                      and self.dz.hot(kind == "enter"))
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", pady=8)
        ghost_button(row, "Browse...", self._browse, width=130).pack(side="left")
        self.status = ctk.CTkLabel(row, text="", font=theme.font_style("body"), text_color=P["muted"],
                                   justify="left", wraplength=620, anchor="w")
        self.status.pack(side="left", padx=14)
        caption(body, "Or start from").pack(anchor="w", pady=(10, 4))
        grid = ctk.CTkFrame(body, fg_color="transparent")
        grid.pack(fill="x")
        grp = []
        cat = OptionCard(grid, "The catalogue", "Common MSP apps with the silent switches and detection worked "
                                                "out.", "catalogue", grp, icon="search", tone="info")
        wg = OptionCard(grid, "A winget package", "Installs from the Windows Package Manager by its ID, for "
                                                  "example 7zip.7zip. No file needed.", "winget", grp,
                        icon="deploy", tone="success")
        sc = OptionCard(grid, "A script or command", "Your own install command, plus a check that tells "
                                                     "SectorSmith it's there.", "script", grp, icon="task",
                        tone="accent")
        for i, o in enumerate((cat, wg, sc)):
            o.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0 if i == 2 else 6))
        grid.grid_columnconfigure((0, 1, 2), weight=1, uniform="o")
        wg.on_select = sc.on_select = self._blank
        cat.on_select = lambda _v: self._catalogue()
        self.buttons(secondary=("Back", self._back_to_library))

    def _catalogue(self):
        from .manage_screens import CatalogueScreen
        self.app.go(CatalogueScreen)

    def _browse(self):
        p = filedialog.askopenfilename(title="Pick an installer",
                                       filetypes=[("Installers", " ".join("*" + e for e in INSTALLER_EXTS)),
                                                  ("All files", "*")])
        if p:
            self.accept_files([p])

    def accept_files(self, paths):
        p = next((x for x in paths if x.lower().endswith(INSTALLER_EXTS) and os.path.isfile(x)), None)
        if p is None:
            self.app.toast("That isn't an installer. Drop an .msi, .exe or .msix file.", "warn")
            return
        if self.pkg is not None:
            self.app.toast("Use Replace installer to give this package a new file.", "warn")
            return
        if self.status is None or not self.status.winfo_exists():
            self._source()
        self.path = p
        self.status.configure(text=f"Reading {os.path.basename(p)}...", text_color=P["muted"])
        self.app.mascot.set_mood("working", text="Reading the installer...")
        self.app.background(lambda: analyze.analyze(p), lambda s: self._analysed(p, s),
                            lambda e: self._analyse_failed(p, e))

    def _analysed(self, p, s):
        if not self.winfo_exists() or p != self.path:
            return
        self.app.mascot.set_mood("happy", text="Here's what I found. Have a look before saving.")
        self._details(s)

    def _analyse_failed(self, p, e):
        if not self.winfo_exists() or p != self.path:
            return
        log.warning("Deploy: analysing %s failed: %s", p, e)
        self.path = None
        self.app.mascot.set_mood("sad", text="I couldn't make sense of that file.")
        if self.status is not None and self.status.winfo_exists():
            self.status.configure(text=f"Couldn't read {os.path.basename(p)} ({e}). Is it a real installer? You "
                                       "can also start from a script below.", text_color=P["danger"])

    def _blank(self, kind):
        self.path = None
        self.app.mascot.set_mood("idle", text="Starting from scratch. Fill in the blanks!")
        winget = kind == "winget"
        if winget:
            note = ("winget must be available on the PC. Detection looks for the app's name in Add/Remove "
                    "Programs, so make the name match what Windows shows.")
        else:
            note = ("The install command runs in cmd.exe on each PC (sh on Linux). The detection script should "
                    "print the installed version, or nothing if it's missing.")
        self._details({"name": "", "kind": kind, "version": "", "publisher": "", "installer": "",
                       "install": WINGET_INSTALL if winget else "", "uninstall": WINGET_UNINSTALL if winget else "",
                       "detection": {"method": "registry" if winget else "script", "value": ""},
                       "success_codes": [0, 3010, 1641], "language": "powershell", "product_code": "",
                       "winget_id": "", "notes": [note]})

    # -- step 2 -------------------------------------------------------------
    def _details(self, s):
        self.s = s
        self.kind = s.get("kind", "exe")
        editing = self.pkg is not None
        if editing:
            sub = "Changes apply the next time maintenance runs."
        elif s.get("installer"):
            sub = f"From {s['installer']}. These are suggestions: change anything that looks wrong, then save."
        else:
            sub = "Say how to install it and how to tell it's there."
        body = self.step(1, f"Edit {s['name']}" if editing else "Check the details", sub)
        cols = ctk.CTkFrame(body, fg_color="transparent")
        cols.pack(fill="both", expand=True)
        cols.grid_columnconfigure(0, weight=11, uniform="d")
        cols.grid_columnconfigure(1, weight=9, uniform="d")
        cols.grid_rowconfigure(0, weight=1)
        form = ctk.CTkFrame(cols, corner_radius=12, fg_color=P["surface"], border_width=1, border_color=P["border"])
        form.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        form.grid_columnconfigure(1, weight=1)
        self.form = form
        self.v = {k: tk.StringVar(value=str(s.get(k) or "")) for k in
                  ("name", "version", "publisher", "install", "uninstall", "winget_id", "product_code")}
        self.v["codes"] = tk.StringVar(value=", ".join(str(c) for c in s.get("success_codes") or [0]))
        self._row = 0
        self._field("Name", "name", placeholder="As it shows in Add/Remove Programs")
        self._field("Version", "version", placeholder="e.g. 24.08")
        self._field("Publisher", "publisher")
        if self.kind == "winget":
            self._field("winget ID", "winget_id", placeholder="e.g. 7zip.7zip")
        self._field("Install", "install", mono=True,
                    hint="{installer} becomes the copied installer's path." if s.get("installer") else None)
        self._field("Uninstall", "uninstall", mono=True,
                    hint="{registry_uninstall} runs the app's own uninstaller." if "{registry_uninstall}" in
                    (s.get("uninstall") or "") else None)
        det = s.get("detection") or {"method": "registry", "value": ""}
        self._label("Detect by")
        self.det_method = ctk.CTkOptionMenu(form, values=list(DETECTION.values()), height=32, width=260,
                                            font=theme.font_style("body"), command=lambda _v: self._det_changed())
        self.det_method.set(DETECTION.get(det.get("method"), DETECTION["registry"]))
        self.det_method.grid(row=self._row, column=1, sticky="w", padx=(0, 18), pady=4)
        self._row += 1
        self.det_box = ctk.CTkFrame(form, fg_color="transparent")
        self.det_box.grid(row=self._row, column=0, columnspan=2, sticky="ew")
        self.det_box.grid_columnconfigure(1, weight=1)
        self._row += 1
        self.det_value = tk.StringVar(value=det.get("value", "") if det.get("method") != "script" else "")
        self.det_script = det.get("value", "") if det.get("method") == "script" else ""
        self.lang = s.get("language") or "powershell"
        self._field("Success codes", "codes", hint="Installer exit codes that count as success.")
        self._det_changed()

        side = ctk.CTkFrame(cols, fg_color="transparent")
        side.grid(row=0, column=1, sticky="nsew")
        pills = ctk.CTkFrame(side, fg_color="transparent")
        pills.pack(fill="x")
        Pill(pills, KIND_LABEL.get(self.kind, self.kind), "accent").pack(side="left")
        if s.get("product_code"):
            Pill(pills, "MSI product code found", "success").pack(side="left", padx=6)
        notes = s.get("notes") or []
        if notes:
            guess = any("guess" in n.lower() for n in notes)
            box = ctk.CTkFrame(side, corner_radius=12, fg_color=P["warn_soft" if guess else "accent_soft"])
            box.pack(fill="x", pady=(8, 0))
            for n in notes:
                ctk.CTkLabel(box, text=_tidy(n), font=theme.font_style("small"), text_color=P["text"],
                             justify="left", anchor="w", wraplength=360).pack(anchor="w", padx=14, pady=(8, 8))
        caption(side, "Scripts preview").pack(anchor="w", pady=(12, 4))
        self.preview_tab = segmented(side, ["Install.ps1", "Uninstall.ps1", "Detect.ps1"],
                                     command=lambda _v: self._preview(), height=30)
        self.preview_tab.set("Install.ps1")
        self.preview_tab.pack(anchor="w")
        self.preview_box = ctk.CTkTextbox(side, font=theme.mono(11), wrap="none")
        self.preview_box.pack(fill="both", expand=True, pady=(6, 0))
        for var in self.v.values():
            var.trace_add("write", lambda *_: self._preview_soon())
        self.det_value.trace_add("write", lambda *_: self._preview_soon())
        self._preview()

        extra = None
        if editing and self.pkg.installer:
            extra = ("Replace installer...", self._replace)
        elif editing:
            extra = ("Export scripts...", lambda: export_scripts(self.app, self.store, self.pkg))
        self.save_btn = self.buttons(primary=("Save package", self._save),
                                     secondary=("Back", self._back_to_library if editing else self._source),
                                     extra=extra)

    def _label(self, text):
        ctk.CTkLabel(self.form, text=text, font=theme.font_style("body_strong"), text_color=P["text"], anchor="w",
                     width=110).grid(row=self._row, column=0, sticky="nw", padx=(18, 10),
                                     pady=(16 if self._row == 0 else 6, 0))

    def _field(self, label, key, placeholder=None, mono=False, hint=None):
        self._label(label)
        e = ctk.CTkEntry(self.form, textvariable=self.v[key], height=32,
                         font=theme.mono(12) if mono else theme.font_style("body"), placeholder_text=placeholder)
        e.grid(row=self._row, column=1, sticky="ew", padx=(0, 18), pady=(16 if self._row == 0 else 4, 0))
        self._row += 1
        if hint:
            ctk.CTkLabel(self.form, text=hint, font=theme.font_style("small"), text_color=P["muted"],
                         anchor="w").grid(row=self._row, column=1, sticky="w", padx=(0, 18))
            self._row += 1
        return e

    def _det_changed(self):
        if getattr(self, "det_text", None) is not None and self.det_text.winfo_exists():
            self.det_script = self.det_text.get("1.0", "end-1c")
        for w in self.det_box.winfo_children():
            w.destroy()
        self.det_text = None
        method = _key(DETECTION, self.det_method.get())
        lbl = {"registry": "Name has", "msi_product_code": "Product code", "file": "File path",
               "script": "Script"}[method]
        ctk.CTkLabel(self.det_box, text=lbl, font=theme.font_style("body_strong"), text_color=P["text"], anchor="w",
                     width=110).grid(row=0, column=0, sticky="nw", padx=(18, 10), pady=(6, 0))
        if method == "script":
            self.lang_seg = segmented(self.det_box, list(LANGUAGES.values()), height=28,
                                      command=lambda v: setattr(self, "lang", _key(LANGUAGES, v)))
            self.lang_seg.set(LANGUAGES.get(self.lang, LANGUAGES["powershell"]))
            self.lang_seg.grid(row=0, column=1, sticky="w", padx=(0, 18), pady=(4, 0))
            self.det_text = ctk.CTkTextbox(self.det_box, height=74, font=theme.mono(11),
                                           border_color=P["control_border"])
            self.det_text.insert("1.0", self.det_script)
            self.det_text.grid(row=1, column=1, sticky="ew", padx=(0, 18), pady=(4, 0))
            self.det_text.bind("<KeyRelease>", lambda _e: self._preview_soon())
            hint = "Print the installed version, or nothing if it's missing."
        elif method == "msi_product_code":
            ctk.CTkEntry(self.det_box, textvariable=self.v["product_code"], height=32,
                         font=theme.mono(12), placeholder_text="{GUID}").grid(row=0, column=1, sticky="ew",
                                                                              padx=(0, 18), pady=(4, 0))
            hint = "Matched against the product code Windows registered."
        else:
            ctk.CTkEntry(self.det_box, textvariable=self.det_value, height=32,
                         font=theme.mono(12) if method == "file" else theme.font_style("body"),
                         placeholder_text="C:\\Program Files\\App\\app.exe" if method == "file" else
                         "Leave empty to use the package name").grid(row=0, column=1, sticky="ew", padx=(0, 18),
                                                                     pady=(4, 0))
            hint = ("Its version is read from the file." if method == "file" else
                    "Matches part of the name. Start with re: for a regular expression.")
        ctk.CTkLabel(self.det_box, text=hint, font=theme.font_style("small"), text_color=P["muted"],
                     anchor="w").grid(row=2, column=1, sticky="w", padx=(0, 18))
        self._preview_soon()

    def _detection(self):
        method = _key(DETECTION, self.det_method.get())
        if method == "script":
            if self.det_text is not None and self.det_text.winfo_exists():
                self.det_script = self.det_text.get("1.0", "end-1c")
            return {"method": "script", "value": self.det_script.strip()}
        if method == "msi_product_code":
            return {"method": method, "value": ""}
        return {"method": method, "value": self.det_value.get().strip()}

    def _collect(self, strict=True):
        f = {k: v.get().strip() for k, v in self.v.items()}
        try:
            f["success_codes"] = [int(x) for x in re.split(r"[,\s]+", f.pop("codes")) if x]
        except ValueError:
            if strict:
                raise ValueError("Success codes should be numbers, like 0, 3010.") from None
            f["success_codes"] = [0]
        f["detection"] = self._detection()
        if strict:
            if not f["name"]:
                raise ValueError("Give the package a name.")
            if not f["install"]:
                raise ValueError("Add an install command.")
            if self.kind == "winget" and not f["winget_id"]:
                raise ValueError("Add the winget ID, for example 7zip.7zip.")
            if f["detection"]["method"] in ("file", "script") and not f["detection"]["value"]:
                raise ValueError("Say how to detect it: add the file path or detection script.")
            if f["detection"]["method"] == "msi_product_code" and not f["product_code"]:
                raise ValueError("Add the MSI product code, or pick another way to detect it.")
        return f

    def _apply(self, pkg, f):
        for k in ("name", "version", "publisher", "install", "uninstall", "winget_id", "product_code",
                  "success_codes", "detection"):
            setattr(pkg, k, f[k])
        pkg.language = self.lang
        return pkg

    def _preview_soon(self):
        if getattr(self, "_pv_job", None):
            self.after_cancel(self._pv_job)
        self._pv_job = self.after(250, self._preview)

    def _preview(self):
        self._pv_job = None
        if not getattr(self, "preview_box", None) or not self.preview_box.winfo_exists():
            return
        f = self._collect(strict=False)
        pkg = self._apply(core.Package(name=f["name"] or "New package", kind=self.kind,
                                       installer=self.s.get("installer", "")), f)
        try:
            text = core.render_scripts(pkg).get(self.preview_tab.get(), "")
        except Exception as e:  # noqa: BLE001
            text = f"# Couldn't build this script: {e}"
        self.preview_box.configure(state="normal")
        self.preview_box.delete("1.0", "end")
        self.preview_box.insert("1.0", text)
        self.preview_box.configure(state="disabled")

    def _save(self):
        if self._saving:
            return
        try:
            f = self._collect()
        except ValueError as e:
            self.app.toast(str(e), "warn")
            return
        store, path = self.store, self.path
        if self.pkg is not None:
            ok, pkg = attempt(self.app, "save the package", lambda: store.upsert("packages",
                                                                                 self._apply(self.pkg, f)))
            if ok:
                self._saved(pkg)
            return
        if path is None:
            ok, pkg = attempt(self.app, "save the package", lambda: store.upsert(
                "packages", self._apply(core.Package(name=f["name"], kind=self.kind), f)))
            if ok:
                self._saved(pkg)
            return
        # copying a big installer into the library can take a moment
        self._saving = True
        self.save_btn.configure(state="disabled", text="Saving...")

        def work():
            pkg = store.add_from_installer(path)
            return store.upsert("packages", self._apply(pkg, f))

        def failed(e):
            self._saving = False
            if self.save_btn.winfo_exists():
                self.save_btn.configure(state="normal", text="Save package")
            self.app.toast(f"Couldn't save the package: {e}", "danger")
        self.app.background(work, self._saved, failed)

    def _replace(self):
        p = filedialog.askopenfilename(title=f"New installer for {self.pkg.name}",
                                       filetypes=[("Installers", " ".join("*" + e for e in INSTALLER_EXTS)),
                                                  ("All files", "*")])
        if not p:
            return
        pkg, store = self.pkg, self.store

        def done(pkg):
            self.app.toast(f"{pkg.name} now uses {pkg.installer}")
            self._details(dataclasses.asdict(pkg))
        self.app.background(lambda: store.replace_installer(pkg, p), done,
                            lambda e: self.app.toast(f"Couldn't use that installer: {e}", "danger"))

    # -- step 3 -------------------------------------------------------------
    def _saved(self, pkg):
        self._saving = False
        if not self.winfo_exists():
            return
        self.pkg = pkg
        body = self.step(2, "Package saved", "")
        self.app.mascot.set_mood("happy", "done")
        det = pkg.detection or {}
        how = DETECTION.get(det.get("method"), det.get("method"))
        what = pkg.product_code if det.get("method") == "msi_product_code" else (det.get("value") or pkg.name)
        self.result_card(body, "check", "success", f"{pkg.name} {pkg.version}".strip() + " is in your library",
                         [f"Install: {_short(pkg.install)}",
                          f"Detect: {how}, {_short(what, 60)}",
                          "",
                          "Next, deploy it to some PCs, or export the scripts for another tool."])
        self.buttons(primary=("Deploy it", lambda: self.app.go(DeploymentWizard, item=("software", pkg.id))),
                     secondary=("Back to library", self._back_to_library),
                     extra=("Export scripts...", lambda: export_scripts(self.app, self.store, pkg)))


# ---------------------------------------------------------------------------
class DeploymentWizard(Screen):
    """What (one task, or one or more packages), where (every PC, a client, one PC) and how (wanted state,
    new PCs only, schedule)."""
    guide_topic = "deploy"

    def __init__(self, master, app, item=None, items=None):
        super().__init__(master, app, "New deployment", steps=["What", "Where", "How"], badge=BADGE)
        self.picked = list(items or ([item] if item else []))
        self.target = None
        self.store = open_store(self, self.new_body())
        if self.store is not None:
            self._what()

    @property
    def item(self):
        return self.picked[0] if len(self.picked) == 1 else None

    def _back(self):
        go_manage(self.app, "Deployments")

    def _what(self):
        body = self.step(0, "New deployment", "What should go out? Pick one or more packages, or an upkeep task.")
        self.app.mascot.set_mood("idle", text="What are we rolling out?")
        lst = ctk.CTkScrollableFrame(body, fg_color="transparent")
        lst.pack(fill="both", expand=True)
        rows = []
        nb = None
        cards = {}

        def pick(item):
            if item[0] == "task" or any(k == "task" for k, _i in self.picked):
                self.picked = [item] if item not in self.picked or item[0] != "task" else []
            elif item in self.picked:
                self.picked.remove(item)
            else:
                self.picked.append(item)
            for key, c in cards.items():
                c.on = key in self.picked
                paint_card(c, c.on)
            n = len(self.picked)
            nb.configure(state="normal" if n else "disabled",
                         text=f"Next: {_plural(n, 'package')}" if n > 1 else "Next")
        for title, kind, items in (("Software", "software", self.store.packages), ("Tasks", "task", self.store.tasks)):
            if not items:
                continue
            caption(lst, title).pack(anchor="w", pady=(6, 2))
            for x in sorted(items, key=lambda x: x.name.lower()):
                if kind == "software":
                    card = item_row(lst, "package", "accent", x.name, x.publisher or KIND_LABEL.get(x.kind, x.kind),
                                    [(x.version, "success")] if x.version else [])
                else:
                    card = item_row(lst, "task", "success", x.name, x.description, [])
                make_selectable(rows, card, (kind, x.id), pick, multi=True)
                cards[(kind, x.id)] = card
        if not rows:
            ctk.CTkLabel(lst, text="Nothing to deploy yet. Add a package or a task first.",
                         font=theme.font_style("body"), text_color=P["muted"]).pack(anchor="w", pady=12)
            soft_button(lst, "Add a package", lambda: self.app.go(PackageBuilder)).pack(anchor="w")
        nb = self.buttons(primary=("Next", self._where), secondary=("Back", self._back))
        start, self.picked = [p for p in self.picked if p in cards], []
        for key in start:
            pick(key)
        if not self.picked:
            nb.configure(state="disabled")
        self.cards = cards

    def _objs(self):
        return [self.store.get("packages" if k == "software" else "tasks", i) for k, i in self.picked]

    def _what_text(self):
        objs = [o for o in self._objs() if o is not None]
        if len(objs) == 1:
            return objs[0].name
        return f"{_plural(len(objs), 'package')} ({', '.join(o.name for o in objs[:3])}" \
               + (", ..." if len(objs) > 3 else "") + ")"

    def _where(self):
        body = self.step(1, "Which PCs?", f"Where should {self._what_text()} apply?")
        grp = []
        area = ctk.CTkFrame(body, fg_color="transparent")
        nb = None
        everyone = OptionCard(body, "Every PC", "Applies to every PC you run maintenance on.", "all", grp,
                              icon="pc", tone="accent")
        client = OptionCard(body, "A client", "Applies to the PCs in one of your clients.", "client", grp,
                            icon="building", tone="info")
        one = OptionCard(body, "One PC", "Applies to a single PC, by its name. Wins over Every PC and client "
                                         "deployments.", "machine", grp, icon="pc", tone="teal")
        for o in (everyone, client, one):
            o.pack(fill="x", pady=4)
        area.pack(fill="x", pady=(8, 0))
        clients = sorted(self.store.clients, key=lambda c: c.name.lower())
        if not clients:
            client.set_enabled(False, "You have no clients yet. Add one on the Clients page.")

        def set_target(t):
            self.target = t
            nb.configure(state="normal" if t[0] == "all" or t[1] else "disabled")

        def chosen(v):
            for w in area.winfo_children():
                w.destroy()
            if v == "all":
                set_target(("all", ""))
            elif v == "client":
                from .manage_common import client_text
                names = [client_text(self.app, c.name) for c in clients]
                labels = [f"{n} ({i + 1})" if names.count(n) > 1 else n for i, n in enumerate(names)]
                menu = ctk.CTkOptionMenu(area, values=labels, height=34, width=260, font=theme.font_style("body"),
                                         command=lambda n: set_target(("client", clients[labels.index(n)].id)))
                cur = next((i for i, c in enumerate(clients) if self.target == ("client", c.id)), 0)
                menu.set(labels[cur])
                menu.pack(anchor="w")
                set_target(("client", clients[cur].id))
            else:
                name = tk.StringVar(value=self.target[1] if self.target and self.target[0] == "machine" else "")
                name.trace_add("write", lambda *_: set_target(("machine", name.get().strip())))
                self.machine_picker = MachinePicker(area, self.app, lambda m: name.set(hostname(m)))
                self.machine_picker.pack(anchor="w")
                row = ctk.CTkFrame(area, fg_color="transparent")
                row.pack(fill="x", pady=(8, 0))
                ctk.CTkLabel(row, text="PC name:", font=theme.font_style("body_strong"), text_color=P["text"]).pack(
                    side="left")
                self.machine_entry = ctk.CTkEntry(row, textvariable=name, width=260, height=34,
                                                  placeholder_text="Pick one above, or type its name")
                self.machine_entry.pack(side="left", padx=10)
                set_target(("machine", name.get().strip()))
        for o in grp:
            o.on_select = chosen
        nb = self.buttons(primary=("Next", self._how), secondary=("Back", self._what))
        nb.configure(state="disabled")
        self.where_cards = {o.value: o for o in grp}
        start = self.target[0] if self.target else "all"
        self.where_cards[start].select()

    def _how(self):
        kind = "task" if self.picked[0][0] == "task" else "software"
        bundle = len(self.picked) > 1
        states = TASK_STATES if kind == "task" else SOFTWARE_STATES
        if bundle:  # one version can't fit several packages
            states = {k: v for k, v in states.items() if k != "version"}
        body = self.step(2, "What should happen?", f"{_short(self._what_text(), 70)}  ·  {self._target_text()}")
        self.app.mascot.set_mood("idle", text="Nearly there!")
        cols = ctk.CTkFrame(body, fg_color="transparent")
        cols.pack(fill="both", expand=True)
        cols.grid_columnconfigure(0, weight=3, uniform="h")
        cols.grid_columnconfigure(1, weight=2, uniform="h")
        left = ctk.CTkFrame(cols, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 24))
        caption(left, "Wanted state").pack(anchor="w", pady=(0, 4))
        seg = segmented(left, list(states.values()), height=34)
        seg.pack(anchor="w")
        help_lbl = ctk.CTkLabel(left, text="", font=theme.font_style("body"), text_color=P["muted"], anchor="w",
                                justify="left", wraplength=520)
        help_lbl.pack(anchor="w", pady=(8, 0))
        vrow = ctk.CTkFrame(left, fg_color="transparent")
        ctk.CTkLabel(vrow, text="Version:", font=theme.font_style("body_strong"), text_color=P["text"]).pack(
            side="left")
        self.version = ctk.CTkEntry(vrow, width=160, height=34, placeholder_text="e.g. 24.08")
        obj = self._objs()[0]
        if obj is not None and getattr(obj, "version", ""):
            self.version.insert(0, obj.version)
        self.version.pack(side="left", padx=10)
        opts = ctk.CTkFrame(left, fg_color="transparent")
        opts.pack(fill="x", pady=(16, 0))
        self.name = ctk.CTkEntry(opts, width=300, height=34, placeholder_text="Name for this group, e.g. Office apps")
        if bundle:
            caption(opts, "Name (optional)").pack(anchor="w", pady=(0, 4))
            self.name.pack(anchor="w", pady=(0, 12))
        self.onboarding = tk.BooleanVar(value=False)
        ctk.CTkSwitch(opts, text="Only on new PCs (onboarding runs)", variable=self.onboarding,
                      font=theme.font_style("body")).pack(anchor="w")
        right = ctk.CTkFrame(cols, corner_radius=12, fg_color=P["surface"], border_width=1, border_color=P["border"])
        right.grid(row=0, column=1, sticky="new")
        from .manage_screens import ScheduleEditor
        self.schedule_editor = ScheduleEditor(right, None, None, compact=True)
        caption(right, "When it runs").pack(anchor="w", padx=18, pady=(16, 4))
        self.schedule_editor.pack(fill="x", padx=18, pady=(0, 16))

        def changed(label):
            key = _key(states, label)
            help_lbl.configure(text=STATE_HELP.get(key, ""))
            if key == "version":
                vrow.pack(anchor="w", pady=(10, 0), before=opts)
            else:
                vrow.pack_forget()
        seg.configure(command=changed)
        first = next(iter(states.values()))
        seg.set(first)
        changed(first)
        self.state_seg = seg
        self.buttons(primary=("Save deployment", self._save), secondary=("Back", self._where))

    def _target_text(self):
        kind, value = self.target
        if kind == "client":
            from .manage_common import client_text
            c = self.store.get("clients", value)
            return f"client {client_text(self.app, c.name) if c else '?'}"
        return f"PC {value}" if kind == "machine" else "every PC"

    def _save(self):
        kind = "task" if self.picked[0][0] == "task" else "software"
        states = TASK_STATES if kind == "task" else SOFTWARE_STATES
        desired = _key(states, self.state_seg.get())
        version = self.version.get().strip() if desired == "version" else ""
        if desired == "version" and not version:
            self.app.toast("Type the version to keep, for example 24.08.", "warn")
            return
        try:
            sched = self.schedule_editor.value()
        except ValueError as e:
            self.app.toast(str(e), "warn")
            return
        common = dict(desired=desired, version=version, target_kind=self.target[0], target_value=self.target[1],
                      onboarding_only=bool(self.onboarding.get()), schedule=sched)
        if len(self.picked) > 1:
            d = core.Deployment(item_type="bundle", item_id="", items=[i for _k, i in self.picked],
                                name=self.name.get().strip(), **common)
        else:
            d = core.Deployment(item_type=kind, item_id=self.picked[0][1], **common)
        ok, _ = attempt(self.app, "save the deployment", lambda: self.store.upsert("deployments", d))
        if ok:
            when = schedule.describe(d)
            self.app.toast("Deployment saved. " + (f"It runs {when[0].lower() + when[1:]}." if when else
                                                   "It applies the next time maintenance runs."))
            self.app.mascot.set_mood("happy", "done")
            go_manage(self.app, "Deployments", select=d.id)


# ---------------------------------------------------------------------------
def _form_card(body, expand=False):
    card = ctk.CTkFrame(body, corner_radius=12, fg_color=P["surface"], border_width=1, border_color=P["border"])
    card.pack(fill="both" if expand else "x", expand=expand)
    card.grid_columnconfigure(1, weight=1)
    return card


def _form_label(card, row, text, sticky="w", top=False):
    ctk.CTkLabel(card, text=text, font=theme.font_style("body_strong"), text_color=P["text"], width=110,
                 anchor="w").grid(row=row, column=0, sticky=sticky, padx=(20, 10), pady=(20 if top else 6, 6))


class ClientEditor(Screen):
    guide_topic = "deploy"

    def __init__(self, master, app, client=None):
        super().__init__(master, app, "Edit client" if client else "New client",
                         "A client is a named group of PCs. Deployments and a baseline can target the whole group.",
                         badge=BADGE)
        self.client = client
        body = self.new_body()
        self.store = open_store(self, body)
        if self.store is None:
            return
        card = _form_card(body)
        _form_label(card, 0, "Name", top=True)
        self.name = ctk.CTkEntry(card, height=34, placeholder_text="e.g. Smith & Co, or Front office")
        self.name.grid(row=0, column=1, sticky="ew", padx=(0, 20), pady=(20, 6))
        _form_label(card, 1, "PCs", "nw")
        self.pcs = ctk.CTkTextbox(card, height=150, font=theme.font_style("body"), border_color=P["control_border"])
        self.pcs.grid(row=1, column=1, sticky="ew", padx=(0, 20), pady=6)
        ctk.CTkLabel(card, text="One PC name per line, as Windows shows it (the hostname).",
                     font=theme.font_style("small"), text_color=P["muted"], anchor="w").grid(
            row=2, column=1, sticky="w", padx=(0, 20), pady=(0, 18))
        if client:
            self.name.insert(0, client.name)
            self.pcs.insert("1.0", "\n".join(client.machines))
        caption(body, "Add a linked PC").pack(anchor="w", pady=(16, 4))
        self.quick = ctk.CTkFrame(body, fg_color="transparent")
        self.quick.pack(fill="x")
        self._render_quick()
        self.app.machine_listeners.append(self._render_quick)
        self.buttons(primary=("Save client", self._save),
                     secondary=("Back", lambda: go_manage(self.app, "Clients")))

    def _machines(self):
        return [ln.strip() for ln in self.pcs.get("1.0", "end").splitlines() if ln.strip()]

    def _render_quick(self):
        if not self.quick.winfo_exists():
            raise tk.TclError("gone")
        for w in self.quick.winfo_children():
            w.destroy()
        for m in self.app.machines():
            h = hostname(m)
            soft_button(self.quick, f"+ {h}", lambda h=h: self._add(h)).pack(side="left", padx=(0, 8))
        ctk.CTkButton(self.quick, text="+  Link another PC", height=32, corner_radius=8, fg_color="transparent",
                      border_width=1, border_color=P["border_strong"], hover_color=P["hover"], text_color=P["accent"],
                      font=theme.font_style("body_strong"), command=self.app.open_connect).pack(side="left")

    def _add(self, h):
        if h.lower() in [m.lower() for m in self._machines()]:
            self.app.toast(f"{h} is already in the list.", "warn")
            return
        cur = self.pcs.get("1.0", "end").rstrip("\n")
        self.pcs.delete("1.0", "end")
        self.pcs.insert("1.0", (cur + "\n" if cur else "") + h)

    def _save(self):
        name = self.name.get().strip()
        if not name:
            self.app.toast("Give the client a name.", "warn")
            return
        c = self.client or core.Client(name=name)
        c.name, c.machines = name, list(dict.fromkeys(self._machines()))
        other = next((x for x in self.store.clients if x.id != c.id
                      and {m.lower() for m in x.machines} & {m.lower() for m in c.machines}), None)
        if other is not None:
            # client_of() returns the first match, so a PC in two clients would only get one client's deployments
            self.app.toast(f"Some of these PCs are already in {other.name}. A PC can only be in one client.", "warn")
            return
        ok, _ = attempt(self.app, "save the client", lambda: self.store.upsert("clients", c))
        if ok:
            self.app.toast(f"Saved {name}")
            go_manage(self.app, "Clients", select=c.id)


# ---------------------------------------------------------------------------
class TaskEditor(Screen):
    guide_topic = "deploy"

    def __init__(self, master, app, task=None):
        super().__init__(master, app, "Edit task" if task else "New upkeep task",
                         "A check script says whether the PC is fine (exit code 0). The fix script runs when it "
                         "isn't.", badge=BADGE)
        self.task = task
        body = self.new_body()
        self.store = open_store(self, body)
        if self.store is None:
            return
        card = _form_card(body, expand=True)
        card.grid_rowconfigure((3, 4), weight=1)
        _form_label(card, 0, "Name", top=True)
        self.name = ctk.CTkEntry(card, height=34, placeholder_text="e.g. Print Spooler running")
        self.name.grid(row=0, column=1, sticky="ew", padx=(0, 20), pady=(20, 6))
        _form_label(card, 1, "Description")
        self.desc = ctk.CTkEntry(card, height=34, placeholder_text="Optional, one line")
        self.desc.grid(row=1, column=1, sticky="ew", padx=(0, 20), pady=6)
        _form_label(card, 2, "Language")
        self.lang = segmented(card, list(LANGUAGES.values()), height=30)
        self.lang.set(LANGUAGES.get(task.language if task else "powershell", LANGUAGES["powershell"]))
        self.lang.grid(row=2, column=1, sticky="w", padx=(0, 20), pady=6)
        _form_label(card, 3, "Check", "nw")
        self.test = ctk.CTkTextbox(card, height=110, font=theme.mono(12), border_color=P["control_border"])
        self.test.grid(row=3, column=1, sticky="nsew", padx=(0, 20), pady=6)
        _form_label(card, 4, "Fix", "nw")
        self.fix = ctk.CTkTextbox(card, height=110, font=theme.mono(12), border_color=P["control_border"])
        self.fix.grid(row=4, column=1, sticky="nsew", padx=(0, 20), pady=6)
        ctk.CTkLabel(card, text="Both scripts run as administrator on the PC. Exit with 0 from Check when "
                                "everything is fine.", font=theme.font_style("small"), text_color=P["muted"],
                     anchor="w").grid(row=5, column=1, sticky="w", padx=(0, 20), pady=(0, 16))
        if task:
            self.name.insert(0, task.name)
            self.desc.insert(0, task.description)
            self.test.insert("1.0", task.test)
            self.fix.insert("1.0", task.set)
        self.buttons(primary=("Save task", self._save),
                     secondary=("Back", lambda: go_manage(self.app, "Tasks")))

    def _save(self):
        name, test = self.name.get().strip(), self.test.get("1.0", "end-1c").strip()
        if not name:
            self.app.toast("Give the task a name.", "warn")
            return
        if not test:
            self.app.toast("Add a check script. It should exit with 0 when the PC is fine.", "warn")
            return
        t = self.task or core.Task(name=name)
        t.name, t.test, t.set = name, test, self.fix.get("1.0", "end-1c").strip()
        t.description, t.language = self.desc.get().strip(), _key(LANGUAGES, self.lang.get())
        ok, _ = attempt(self.app, "save the task", lambda: self.store.upsert("tasks", t))
        if ok:
            self.app.toast(f"Saved {name}")
            go_manage(self.app, "Tasks", select=t.id)


# ---------------------------------------------------------------------------
class RunWizard(Screen):
    """Maintenance: check the chosen PCs (read-only), show the plan, then apply it after a typed confirmation.

    only: limit the run to these deployments. onboard: a client id; the chosen PCs join that client and get its
    baseline (an onboarding run)."""
    guide_topic = "deploy"

    def __init__(self, master, app, only=None, onboard=None):
        title = "Onboard a new PC" if onboard else "Run maintenance"
        super().__init__(master, app, title, steps=["PCs", "Check", "Apply", "Done"], badge=BADGE)
        self.chosen = []
        self.onboarding = tk.BooleanVar(value=bool(onboard))
        self.results = []
        self.store = open_store(self, self.new_body())
        self.client = self.store.get("clients", onboard) if (self.store is not None and onboard) else None
        self.base = self.store.baseline_for(onboard) if self.client is not None else None
        self.only = list(only) if only else ([self.base.id] if self.base is not None else None)
        if self.store is not None:
            self._pcs()

    def _scope_text(self):
        if self.base is not None:
            from .manage_common import client_text
            names = [p.name for p in (self.store.get("packages", i) for i in self.base.items) if p]
            return (f"The PC joins {client_text(self.app, self.client.name)} and gets its baseline: "
                    f"{_short(', '.join(names), 120)}.")
        if self.only:
            from .manage_screens import dep_title
            deps = [d for d in (self.store.get("deployments", i) for i in self.only) if d is not None]
            return "Only this deployment runs: " + "; ".join(dep_title(self.store, d) for d in deps) + "."
        return ""

    def _pcs(self):
        if self.base is not None:
            body = self.step(0, "Onboard a new PC", "Pick the new PC. Nothing changes until you confirm.")
        else:
            body = self.step(0, self.title_lbl.cget("text") if self.only else "Run maintenance",
                             "Pick the PCs to bring into line. Nothing changes until you confirm.")
        self.app.mascot.set_mood("idle", text="Which PCs need a tidy-up?")
        nb = None

        def changed(sel):
            self.chosen = sel
            n = len(sel)
            nb.configure(state="normal" if n else "disabled",
                         text=f"Check {_plural(n, 'PC')}" if n else "Check now")
        self.checklist = MachineChecklist(body, self.app, changed, single=self.base is not None)
        self.checklist.selected = list(self.chosen)
        self.checklist.render()
        self.checklist.pack(fill="x")
        self.app.machine_listeners.append(self._machines_changed)
        if self.base is None:
            ctk.CTkSwitch(body, text="Treat these as new PCs (include onboarding-only deployments)",
                          variable=self.onboarding, font=theme.font_style("body")).pack(anchor="w", pady=(14, 0))
        scope = self._scope_text()
        on = [d for d in self.store.deployments if d.enabled]
        if scope:
            self.note(body, scope, "accent").pack(anchor="w", pady=(10, 0))
        elif on:
            self.note(body, f"{_plural(len(on), 'deployment')} in the library. Each PC gets the ones aimed at it: "
                            "a single PC beats its client, which beats Every PC.", "accent").pack(anchor="w",
                                                                                                  pady=(10, 0))
        else:
            self.note(body, "There are no deployments switched on yet, so there's nothing to check. Add one on "
                            "the Deployments page first.", "warn").pack(anchor="w", pady=(10, 0))
        back = ("Clients", {"select": self.client.id}) if self.client is not None else ("Sessions", {})
        nb = self.buttons(primary=("Check now", self._check),
                          secondary=("Back", lambda: go_manage(self.app, back[0], **back[1])))
        changed(list(self.chosen))

    def _join_client(self, machines):
        """Onboarding: put the chosen PCs in the client first, so its baseline applies to them."""
        if self.client is None:
            return True
        hosts = [hostname(m) for m in machines]
        for h in hosts:
            other = next((c for c in self.store.clients_of(h) if c.id != self.client.id), None)
            if other is not None:
                from .manage_common import client_text
                self.app.toast(f"{h} is already in {client_text(self.app, other.name)}. A PC can only be in one "
                               "client.", "warn")
                return False
        have = {m.lower() for m in self.client.machines}
        new = [h for h in hosts if h.lower() not in have]
        if new:
            self.client.machines += new
            ok, _ = attempt(self.app, "add the PC to the client", lambda: self.store.upsert("clients", self.client))
            return ok
        return True

    def _machines_changed(self):
        if not self.checklist.winfo_exists():
            raise tk.TclError("gone")
        self.checklist.render()

    # -- live status under the progress panel --------------------------------
    def _live_list(self, body, machines):
        caption(body, "PCs").pack(anchor="w", pady=(0, 4))
        lst = ctk.CTkScrollableFrame(body, fg_color="transparent", height=170)
        lst.pack(fill="both", expand=True)
        self.live = {}
        for m in machines:
            row = ctk.CTkFrame(lst, corner_radius=12, fg_color=P["surface"], border_width=1, border_color=P["border"])
            row.pack(fill="x", pady=3, padx=(0, 6))
            badge(row, "pc", "neutral" if m.is_local else "teal", 32).pack(side="left", padx=12, pady=8)
            ctk.CTkLabel(row, text=machine_name(m), font=theme.font_style("body_strong"), text_color=P["text"]).pack(
                side="left")
            st = ctk.CTkLabel(row, text="Waiting", font=theme.font_style("body_strong"), text_color=P["muted"])
            st.pack(side="right", padx=14)
            self.live[id(m)] = st

    def _set_live(self, m, text, tone="muted"):
        def apply():
            lbl = getattr(self, "live", {}).get(id(m))
            if lbl is not None and lbl.winfo_exists():
                lbl.configure(text=text, text_color=P[tone])
        self.app.call_soon(apply)

    def _job(self, machines, mode):
        store, onboarding, only = self.store, bool(self.onboarding.get()), self.only
        trigger = "onboarding" if self.base is not None else "manual"

        def job(prog):
            out = []
            for m in machines:
                prog.check()
                self._set_live(m, "Checking..." if mode == "detect" else "Working...", "accent")
                try:
                    sess = core.run_session(m, store, prog, mode=mode, onboarding=onboarding, only=only,
                                            trigger=trigger)
                except Cancelled:
                    self._set_live(m, "Stopped", "warn")
                    raise
                except Exception as e:  # noqa: BLE001
                    log.error("Deploy session on %s failed: %s", m.label, e)
                    self._set_live(m, "Couldn't reach it", "danger")
                    out.append((m, {"machine": hostname(m), "error": str(e), "actions": [], "summary": {}}))
                    continue
                sm = sess["summary"]
                if mode == "detect":
                    todo = sum(1 for a in sess["actions"] if a["action"] not in ("none", "audit"))
                    self._set_live(m, f"{_plural(todo, 'change')} needed" if todo else "All good",
                                   "warn" if todo else "success")
                else:
                    self._set_live(m, f"{sm['failed']} failed" if sm["failed"] else "Done",
                                   "danger" if sm["failed"] else "success")
                out.append((m, sess))
            return out
        return job

    # -- step 2: check -------------------------------------------------------
    def _check(self):
        machines = list(self.chosen)
        if not self._join_client(machines):
            return
        body, panel = self.progress("Checking...", f"Looking at {_plural(len(machines), 'PC')}. Nothing is "
                                                   "changed in this step.")
        self._draw_steps(1)
        self._live_list(body, machines)
        self.app.run_job("Deploy check", self._job(machines, "detect"), self._checked, panel,
                         on_error=self._error, on_cancel=lambda info: self.cancelled_state(
                             "Check stopped", ["Checking only reads, so nothing was changed on any PC."], step=1,
                             secondary=("Back", self._pcs)))

    def _changes(self, sess):
        return [a for a in sess.get("actions", []) if a.get("action") not in ("none", "audit")]

    def _checked(self, results):
        self.results = results
        self.todo = [(m, s) for m, s in results if self._changes(s)]
        n = sum(len(self._changes(s)) for _m, s in results)
        errors = [(m, s) for m, s in results if s.get("error")]
        if n:
            body = self.step(1, f"{_plural(n, 'change')} needed", "Here's what each PC needs. Nothing has been "
                                                                  "changed yet.")
            self.app.mascot.set_mood("idle", text="Here's the plan. Happy with it?")
        else:
            body = self.step(1, "Nothing to change" if not errors else "Couldn't check every PC",
                             "Every checked PC already matches its deployments." if not errors else "")
            self.app.mascot.set_mood("happy" if not errors else "sad", text="All tidy already!" if not errors
                                     else "Some PCs didn't answer.")
        table = action_tree(body, [("pc", "PC", 150), ("item", "Item", 200), ("want", "Wanted", 140),
                                   ("found", "Found", 140), ("plan", "Plan", 150)])
        for m, s in results:
            if s.get("error"):
                add_action_row(table, (s["machine"], "Couldn't check", "", "", _short(s["error"], 60)), "bad")
            for a in s.get("actions", []):
                add_action_row(table, (s["machine"], a["name"], _wanted(a["desired"]), a["current"],
                                       ACTION_LABEL.get(a["action"], a["action"])), _tag(a))
            if not s.get("actions") and not s.get("error"):
                add_action_row(table, (s["machine"], "No deployments for this PC", "", "", ""))
        self.table, self.tree = table, table.tree
        if n:
            self.buttons(primary=("Apply changes", self._confirm), secondary=("Back", self._pcs))
        else:
            self.buttons(primary=("Back to Sessions", lambda: go_manage(self.app, "Sessions")),
                         secondary=("Check again", self._check))

    # -- step 3: confirm -----------------------------------------------------
    def _confirm(self):
        body = self.step(2, "Last check", "These changes run on the PCs straight away.")
        self.app.mascot.set_mood("warn", text="Double-check the PCs and the changes. This runs straight away.")
        lines = []
        for m, s in self.todo[:6]:
            acts = ", ".join(f"{ACTION_LABEL.get(a['action'], a['action'])} {a['name']}" for a in self._changes(s))
            lines.append(f"{s['machine']}: {_short(acts, 80)}")
        if len(self.todo) > 6:
            lines.append(f"...and {len(self.todo) - 6} more")
        lines += ["", "Deploy is a preview. With a new package, try it on one PC first."]
        n = sum(len(self._changes(s)) for _m, s in self.todo)
        self.result_card(body, "deploy", "warn", f"Apply {_plural(n, 'change')} on {_plural(len(self.todo), 'PC')}",
                         lines)
        phrase = (f"APPLY ON {hostname(self.todo[0][0]).upper()}" if len(self.todo) == 1
                  else f"APPLY ON {len(self.todo)} PCS")
        go = self.buttons(primary=("Apply now", self._apply), secondary=("Back", lambda: self._checked(self.results)))
        go.configure(state="disabled", fg_color=P["danger"])
        self.confirm_box(body, phrase, lambda ok: go.configure(state="normal" if ok else "disabled"))

    # -- step 4: apply -------------------------------------------------------
    def _apply(self):
        machines = [m for m, _s in self.todo]
        body, panel = self.progress("Applying changes...", "Installing, fixing, then checking each PC again.")
        self._draw_steps(3)
        self._live_list(body, machines)
        self.app.run_job("Deploy", self._job(machines, "full"), self._done, panel, on_error=self._error,
                         on_cancel=self._stopped)

    def _stopped(self, info):
        sess = info.get("session") or {}
        lines = []
        acts = sess.get("actions", [])
        ran = [a for a in acts if a.get("status") in ("compliant", "failed")
               and a.get("action") not in ("none", "audit")]
        part = [a for a in acts if a.get("status") == "cancelled" and "part-way" in a.get("result", "")]
        left = [a for a in acts if a.get("status") == "cancelled" and a not in part]
        if sess:
            lines.append(f"On {sess.get('machine', '?')}: {_plural(len(ran), 'change')} finished before you "
                         "pressed Cancel.")
        for a in part:
            lines.append(f"{a['name']} was stopped while it ran, so it may be half done. Check it on that PC, or "
                         "run maintenance again to finish it.")
        if left:
            lines.append(f"Not started: {', '.join(a['name'] for a in left[:6])}"
                         + (" and more" if len(left) > 6 else ""))
        lines.append("PCs after that one weren't touched. The session is saved on the Sessions page.")
        self.cancelled_state("Maintenance stopped", lines, step=3,
                             primary=("Back to Sessions", lambda: go_manage(self.app, "Sessions")))

    def _done(self, results):
        acts = [a for _m, s in results for a in s.get("actions", []) if a.get("action") not in ("none", "audit")]
        failed = [a for a in acts if a.get("status") == "failed"]
        errors = [s for _m, s in results if s.get("error")]
        reboot = any(s.get("summary", {}).get("reboot") for _m, s in results)
        ok = not failed and not errors
        body = self.step(3, "All done" if ok else "Done, with problems", "")
        self.app.mascot.set_mood("happy" if ok else "sad", "done" if ok else "error")
        lines = [f"{len(acts) - len(failed)} of {_plural(len(acts), 'change')} worked and checked out."]
        if failed:
            lines.append(f"{len(failed)} failed. Select a row on the Sessions page to see the installer output.")
        if errors:
            lines.append(f"Couldn't reach {_plural(len(errors), 'PC')}.")
        if reboot:
            lines.append("Some installers asked for a restart.")
        self.result_card(body, "check" if ok else "warn", "success" if ok else "warn",
                         f"Maintenance finished on {_plural(len(results), 'PC')}", lines)
        table = action_tree(body, [("pc", "PC", 150), ("item", "Item", 200), ("plan", "Plan", 120),
                                   ("status", "Status", 110), ("result", "Result", 220)], height=6)
        for _m, s in results:
            if s.get("error"):
                add_action_row(table, (s["machine"], "Couldn't run", "", "Failed", _short(s["error"], 60)), "bad")
            for a in s.get("actions", []):
                add_action_row(table, (s["machine"], a["name"], ACTION_LABEL.get(a["action"], a["action"]),
                                       STATUS_LABEL.get(a.get("status"), a.get("status", "")),
                                       _short(a.get("result", ""), 60)), _tag(a))
        self.table, self.tree = table, table.tree
        self.buttons(primary=("Back to Sessions", lambda: go_manage(self.app, "Sessions")),
                     secondary=("Check again", self._check))

    def _error(self, e):
        body = self.step(1, "That didn't work", "")
        self.result_card(body, "warn", "danger", "Maintenance stopped", [str(e), "", "Details are in the log file."])
        self.buttons(primary=("Back to Sessions", lambda: go_manage(self.app, "Sessions")))


# ---------------------------------------------------------------------------
class SessionView(Screen):
    guide_topic = "deploy"

    def __init__(self, master, app, session):
        from .manage_common import client_text, host_text
        s = session
        mode = "Check only" if s.get("mode") == "detect" else "Check and fix"
        try:
            store = app.deploy_store()
        except Exception:  # noqa: BLE001  only used to hide other clients' PC names while presenting
            store = None
        super().__init__(master, app, f"Session on {host_text(app, store, s.get('machine', '?'))}",
                         " · ".join(x for x in (_when(s.get("started")), mode, client_text(app, s.get("client") or ""))
                                    if x), badge=BADGE)
        self.s = s
        body = self.new_body()
        table = action_tree(body, [("item", "Item", 200), ("want", "Wanted", 140), ("found", "Found", 130),
                                   ("plan", "Plan", 110), ("status", "Status", 110), ("result", "Result", 200)],
                            height=7)
        self.rows = {}
        for a in s.get("actions", []):
            iid = add_action_row(table, (a.get("name", "?"), _wanted(a.get("desired")), a.get("current", ""),
                                         ACTION_LABEL.get(a.get("action"), a.get("action", "")),
                                         STATUS_LABEL.get(a.get("status"), a.get("status", "")),
                                         _short(a.get("result", ""), 60)), _tag(a))
            self.rows[iid] = a
        if not self.rows:
            add_action_row(table, ("No deployments applied to this PC", "", "", "", "", ""))
        table.tree.bind("<<TreeviewSelect>>", lambda _e: self._show(table.tree.selection()))
        self.table, self.tree = table, table.tree
        caption(body, "Output").pack(anchor="w", pady=(12, 4))
        self.out = ctk.CTkTextbox(body, height=150, font=theme.mono(11), wrap="none")
        self.out.pack(fill="x")
        self._set_out("Select a row to see what ran and what it printed.")
        self.buttons(primary=("Back to Sessions", lambda: go_manage(self.app, "Sessions")))

    def _set_out(self, text):
        self.out.configure(state="normal")
        self.out.delete("1.0", "end")
        self.out.insert("1.0", text)
        self.out.configure(state="disabled")

    def _show(self, sel):
        a = self.rows.get(sel[0]) if sel else None
        if a is None:
            return
        parts = [f"{a.get('name')}: {ACTION_LABEL.get(a.get('action'), a.get('action'))}"]
        if a.get("result"):
            parts.append(f"Result: {a['result']}")
        if a.get("seconds") is not None:
            parts.append(f"Took {_took(a['seconds'])}")
        if a.get("test_output"):
            parts += ["", "Check output:", a["test_output"]]
        if a.get("log"):
            parts += ["", a["log"]]
        self._set_out("\n".join(parts))
