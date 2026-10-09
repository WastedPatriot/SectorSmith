"""Manage, Audit log: every library change and every session action, who did it and when. Searchable and
exportable to CSV."""
from __future__ import annotations

import time
from tkinter import filedialog

from ..deploy import audit
from . import theme
from .deploy_screens import _plural, _short, attempt
from .manage_common import HIDDEN, ManagePage, client_hidden, client_text, host_text
from .widgets import DataTable

P = theme.PALETTE
FILTERS = {"All": None, "Library": ("packages", "tasks", "clients", "deployments", "machines", "licences"),
           "Sessions": ("session",)}
LIMIT = 2000


def _when(ts):
    return time.strftime("%d %b %Y %H:%M:%S", time.localtime(ts)) if ts else "-"


class AuditScreen(ManagePage):
    def __init__(self, master, app):
        super().__init__(master, app, "Audit log", "Every change to the library and every action a maintenance "
                                                   "session took: who, when and what.")
        if self.store is None:
            return
        self.header_action("Export CSV...", self._export, width=140)
        inner = self.page("Changes", search="Search people, items or details", filters=list(FILTERS),
                          on_filter=lambda _v: self.fill(),
                          panel_empty=("Nothing selected", "Select a line to see it in full.", "jobs"))
        self.table = DataTable(inner, [("when", "When", 150), ("who", "Who", 100), ("what", "What", 150),
                                       ("name", "Item", 240)], height=10, on_select=self._show,
                               empty=("Nothing logged yet", "Changes to packages, deployments, clients, tasks, "
                                                            "licences and tags show here, with every session.",
                                      None, "jobs"))
        self.table.pack(fill="both", expand=True)
        _ok, rows = attempt(app, "read the audit log", lambda: audit.read(self.store, LIMIT))
        self.rows = rows or []
        self.search_var.trace_add("write", lambda *_: self.fill())
        self.fill()

    def _shown(self):
        kinds = FILTERS.get(self.filter.get())
        q = self.search_var.get()
        return [r for r in self.rows if (kinds is None or r.get("kind") in kinds) and audit.matches(r, q)]

    def _mask(self, text):
        """While presenting, other clients' names (and their PCs) don't show, wherever they appear."""
        if not theme.presentation() or not text:
            return text
        for c in self.store.clients:
            if client_hidden(self.app, c):
                text = text.replace(c.name, HIDDEN)
                for h in c.machines:
                    text = text.replace(h, host_text(self.app, self.store, h))
        return text

    def _item(self, r):
        name = r.get("name") or ""
        if r.get("kind") == "session":
            head, _sep, host = name.rpartition(" on ")
            name = f"{head} on {host_text(self.app, self.store, host)}" if head else \
                host_text(self.app, self.store, name)
        elif r.get("kind") == "clients":
            name = client_text(self.app, name)
        return self._mask(name)

    def fill(self):
        self.table.clear()
        rows = self._shown()
        for r in rows[:500]:
            what = r.get("action", "") if r.get("kind") == "session" else \
                f"{r.get('action', '')} {audit.KIND_LABEL.get(r.get('kind'), r.get('kind', '')).lower()}"
            self.table.add((_when(r.get("when")), r.get("who") or "-", what.strip(), _short(self._item(r), 48)),
                           data=r)
        self.table.done()
        self.card_count.configure(text=_plural(len(self.rows), "line") + (" (newest)" if len(self.rows) >= LIMIT
                                                                           else ""))
        self.count_lbl.configure(text=f"Showing {min(len(rows), 500)} of {len(self.rows)}"
                                 if len(rows) != len(self.rows) or len(rows) > 500 else "")
        self.panel.clear()

    def _show(self, r):
        fields = [("When", _when(r.get("when"))), ("Who", r.get("who") or "-"), ("Action", r.get("action") or "-"),
                  ("Kind", audit.KIND_LABEL.get(r.get("kind"), r.get("kind") or "-")), ("Item", self._item(r))]
        sections = [("Details", self._mask(r["detail"]), False)] if r.get("detail") else []
        self.panel.show("jobs", "info", r.get("action", "").capitalize() or "Change", self._item(r), fields=fields,
                        sections=sections)

    def _export(self):
        path = filedialog.asksaveasfilename(title="Export the audit log", defaultextension=".csv",
                                            initialfile=f"SectorSmith deploy audit {time.strftime('%Y-%m-%d')}.csv",
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        rows = self._shown()
        ok, out = attempt(self.app, "export the audit log", lambda: audit.export_csv(rows, path))
        if ok:
            self.app.toast(f"Exported {_plural(len(rows), 'line')} to {out}")
