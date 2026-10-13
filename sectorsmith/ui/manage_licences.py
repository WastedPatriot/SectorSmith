"""Manage, Licences: each client's licence keys and files for packages. Stored encrypted (Windows DPAPI), handed
to the installer at install time, and only ever shown masked."""
from __future__ import annotations

import os
from tkinter import filedialog, messagebox

import customtkinter as ctk

from ..deploy import core, licences
from . import theme
from .deploy_screens import BADGE, _form_card, _form_label, _plural, attempt, segmented
from .manage_common import HIDDEN, ManagePage, client_hidden
from .screens import Screen
from .widgets import DataTable, link_button

P = theme.PALETTE
EVERY_CLIENT = "Every client (fallback)"


def _client_name(app, store, client_id):
    if not client_id:
        return EVERY_CLIENT
    c = store.get("clients", client_id)
    if c is None:
        return "Missing client"
    return HIDDEN if client_hidden(app, c) else c.name


class LicencesScreen(ManagePage):
    def __init__(self, master, app, select=None):
        super().__init__(master, app, "Licences", "Licence keys and files per client, handed to a package's "
                                                  "installer when it installs. Always stored encrypted.")
        if self.store is None:
            return
        self.header_action("New licence", lambda: app.go(LicenceEditor), primary=True, width=140)
        inner = self.page("Licences", search="Search clients or packages",
                          panel_empty=("No licence selected", "Select one to see where it goes and how the installer "
                                                              "gets it.", "lock"))
        self.table = DataTable(inner, [("client", "Client", 180), ("pkg", "Package", 180), ("what", "Licence", 160)],
                               height=9, on_select=self._show, on_open=lambda x: app.go(LicenceEditor, licence=x),
                               empty=("No licences yet", "Add a client's licence key or file for a package. The "
                                                         "installer gets it as {licence_key} or {licence_file}.",
                                      ("New licence", lambda: app.go(LicenceEditor)), "lock"))
        self.table.pack(fill="both", expand=True)
        self.search_var.trace_add("write", lambda *_: self.fill())
        self.fill(select)

    def _pkg(self, lic):
        p = self.store.get("packages", lic.package_id)
        return p.name if p else "Missing package"

    def fill(self, select=None):
        self.table.clear()
        q = self.search_var.get().lower().split()
        sel = None
        rows = sorted(self.store.licences, key=lambda x: (_client_name(self.app, self.store, x.client_id).lower(),
                                                          self._pkg(x).lower()))
        for lic in rows:
            row = (_client_name(self.app, self.store, lic.client_id), self._pkg(lic), licences.masked(lic))
            if q and not all(w in " ".join(row).lower() for w in q):
                continue
            iid = self.table.add(row, data=lic)
            if select == lic.id:
                sel = iid
        self.table.done()
        self.card_count.configure(text=_plural(len(rows), "licence"))
        if sel:
            self.table.tree.selection_set(sel)
            self._show(self.table.rows[sel])
        else:
            self.panel.clear()

    def _show(self, lic):
        how = ("{licence_key} in the install command, and the environment variable SECTORSMITH_LICENCE_KEY"
               if lic.kind == "key" else
               "Copied next to the installer for the install, then removed. {licence_file} in the install command, "
               "and SECTORSMITH_LICENCE_FILE")
        fields = [("Client", _client_name(self.app, self.store, lic.client_id)), ("Package", self._pkg(lic)),
                  ("Licence", licences.masked(lic))]
        sections = [("How the installer gets it", how, False)]
        if lic.notes:
            sections.append(("Notes", lic.notes, False))
        self.panel.show("lock", "accent", self._pkg(lic), _client_name(self.app, self.store, lic.client_id),
                        pills=[("Encrypted", "success")], fields=fields, sections=sections,
                        actions=[("Edit", lambda: self.app.go(LicenceEditor, licence=lic), "primary"),
                                 ("Remove", lambda: self._remove(lic), "danger")])

    def _remove(self, lic):
        if not messagebox.askyesno("Remove?", f"Remove the licence for {self._pkg(lic)}?\n\nIts encrypted key or "
                                              "file is deleted from the library. PCs keep what's installed."):
            return
        ok, _ = attempt(self.app, "remove it", lambda: self.store.delete("licences", lic.id))
        if ok:
            self.app.toast("Licence removed")
            self.fill()


class LicenceEditor(Screen):
    guide_topic = "deploy"

    def __init__(self, master, app, licence=None):
        super().__init__(master, app, "Edit licence" if licence else "New licence",
                         "A licence key or file for one package, for one client or as the fallback for every client.",
                         badge=BADGE)
        from ..integrations import dpapi
        self.lic, self.file_path = licence, None
        body = self.new_body()
        from .deploy_screens import open_store
        self.store = open_store(self, body)
        if self.store is None:
            return
        card = _form_card(body)
        self.clients = sorted(self.store.clients, key=lambda c: c.name.lower())
        self.packages = sorted(self.store.packages, key=lambda p: p.name.lower())
        names = [EVERY_CLIENT] + [HIDDEN if client_hidden(app, c) else c.name for c in self.clients]
        _form_label(card, 0, "Client", top=True)
        self.client = ctk.CTkOptionMenu(card, values=names, width=300, height=34, font=theme.font_style("body"))
        self.client.grid(row=0, column=1, sticky="w", padx=(0, 20), pady=(20, 6))
        _form_label(card, 1, "Package")
        self.package = ctk.CTkOptionMenu(card, values=[p.name for p in self.packages] or ["No packages yet"],
                                         width=300, height=34, font=theme.font_style("body"))
        self.package.grid(row=1, column=1, sticky="w", padx=(0, 20), pady=6)
        _form_label(card, 2, "Kind")
        self.kind = segmented(card, ["Licence key", "Licence file"], command=lambda _v: self._kind(), height=30)
        self.kind.grid(row=2, column=1, sticky="w", padx=(0, 20), pady=6)
        _form_label(card, 3, "Licence")
        self.value_row = ctk.CTkFrame(card, fg_color="transparent")
        self.value_row.grid(row=3, column=1, sticky="ew", padx=(0, 20), pady=6)
        self.key = ctk.CTkEntry(self.value_row, width=360, height=34, show="•", font=theme.mono(12),
                                placeholder_text="Leave empty to keep the stored key" if licence else
                                "Paste the licence key")
        self.file_lbl = ctk.CTkLabel(self.value_row, text="", font=theme.font_style("body"), text_color=P["text_2"])
        self.file_btn = link_button(self.value_row, "Choose file...", self._choose)
        _form_label(card, 4, "Notes")
        self.notes = ctk.CTkEntry(card, height=34, placeholder_text="Optional, e.g. 25 seats, renews March")
        self.notes.grid(row=4, column=1, sticky="ew", padx=(0, 20), pady=6)
        self.available = dpapi.available()
        msg = ("Stored encrypted for your Windows account (DPAPI). Install commands use {licence_key} or "
               "{licence_file}; installers also get SECTORSMITH_LICENCE_KEY or SECTORSMITH_LICENCE_FILE."
               if self.available else "Licences are only stored encrypted with Windows DPAPI, so they can't be "
                                      "saved on this computer.")
        ctk.CTkLabel(card, text=msg, font=theme.font_style("small"), text_color=P["muted"] if self.available else
                     P["warn"], anchor="w", justify="left", wraplength=620).grid(row=5, column=1, sticky="w",
                                                                                 padx=(0, 20), pady=(4, 18))
        if licence:
            self.client.set(_client_name(app, self.store, licence.client_id))
            pkg = self.store.get("packages", licence.package_id)
            if pkg:
                self.package.set(pkg.name)
            self.kind.set("Licence file" if licence.kind == "file" else "Licence key")
            if licence.notes:
                self.notes.insert(0, licence.notes)
        else:
            self.kind.set("Licence key")
        self._kind()
        self.save_btn = self.buttons(primary=("Save licence", self._save),
                                     secondary=("Back", lambda: go_manage_licences(app)))
        if not self.available or not self.packages:
            self.save_btn.configure(state="disabled")

    def _kind(self):
        for w in self.value_row.winfo_children():
            w.pack_forget()
        if self.kind.get() == "Licence key":
            self.key.pack(side="left")
        else:
            name = os.path.basename(self.file_path) if self.file_path else (self.lic.file_name if self.lic and
                                                                           self.lic.kind == "file" else "")
            self.file_lbl.configure(text=name or "No file chosen")
            self.file_lbl.pack(side="left")
            self.file_btn.pack(side="left", padx=10)

    def _choose(self):
        p = filedialog.askopenfilename(title="Pick the licence file")
        if p:
            self.file_path = p
            self._kind()

    def _save(self):
        try:
            ci = [HIDDEN if client_hidden(self.app, c) else c.name for c in self.clients].index(self.client.get())
            client_id = self.clients[ci].id
        except ValueError:
            client_id = ""
        pkg = next((p for p in self.packages if p.name == self.package.get()), None)
        if pkg is None:
            self.app.toast("Pick the package this licence is for.", "warn")
            return
        lic = self.lic or core.Licence(package_id=pkg.id)
        lic.package_id, lic.client_id = pkg.id, client_id
        lic.kind = "file" if self.kind.get() == "Licence file" else "key"
        lic.notes = self.notes.get().strip()
        key = self.key.get() if lic.kind == "key" else None
        try:
            saved = licences.save(self.store, lic, key=key or None, file_path=self.file_path if lic.kind == "file"
                                  else None)
        except Exception as e:  # noqa: BLE001  (a missing value, a duplicate, or DPAPI refusing)
            self.app.toast(str(e), "warn")
            return
        self.app.toast(f"Licence saved: {licences.masked(saved)}")
        go_manage_licences(self.app, select=saved.id)


def go_manage_licences(app, select=None):
    return app.go(LicencesScreen, select=select)

