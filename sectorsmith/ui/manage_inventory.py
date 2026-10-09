"""Manage, Inventory: every PC the library knows, its client and tags (for targeting deployments)."""
from __future__ import annotations

import customtkinter as ctk

from ..deploy import core
from . import theme
from .deploy_screens import _plural, attempt
from .manage_common import HIDDEN, ManagePage, client_hidden, host_text
from .widgets import DataTable, caption, soft_button

P = theme.PALETTE


def _ago(ts):
    from .manage_screens import _ago as ago
    return ago(ts)


class InventoryScreen(ManagePage):
    def __init__(self, master, app, select=None):
        super().__init__(master, app, "Inventory", "Every PC SectorSmith knows: its client and tags. Tags let a "
                                                   "deployment aim at some PCs only.")
        if self.store is None:
            return
        inner = self.page("PCs", search="Search PCs, clients or tags",
                          panel_empty=("No PC selected", "Select a PC to see its details and tags.", "pc"))
        self.table = DataTable(inner, [("pc", "PC", 180), ("client", "Client", 140), ("tags", "Tags", 180),
                                       ("seen", "Last seen", 110)], height=9, on_select=self._show,
                               empty=("No PCs yet", "PCs show up here once they're in a client, tagged, or linked.",
                                      None, "pc"))
        self.table.pack(fill="both", expand=True)
        self.search_var.trace_add("write", lambda *_: self.fill())
        self.fill(select)

    def _client(self, host):
        c = self.store.client_of(host)
        if c is None:
            return "-"
        return HIDDEN if client_hidden(self.app, c) else c.name

    def _hosts(self):
        hosts = self.store.known_hosts()
        seen = {h.lower() for h in hosts}
        from .deploy_screens import hostname
        for m in self.app.machines():
            h = hostname(m)
            if h.lower() not in seen:
                hosts.append(h)
                seen.add(h.lower())
        return sorted(hosts, key=str.lower)

    def _row(self, h):
        m = self.store.machine(h)
        return (host_text(self.app, self.store, h), self._client(h), ", ".join(self.store.tags_of(h)) or "-",
                _ago(m.last_seen) if m and m.last_seen else "-")

    def fill(self, select=None):
        self.table.clear()
        q = self.search_var.get().lower().split()
        hosts = self._hosts()
        sel = None
        shown = 0
        for h in hosts:
            row = self._row(h)
            if q and not all(w in " ".join(map(str, row)).lower() for w in q):
                continue
            iid = self.table.add(row, data=h)
            shown += 1
            if select is not None and h.lower() == select.lower():
                sel = iid
        self.table.done()
        self.card_count.configure(text=_plural(len(hosts), "PC"))
        self.count_lbl.configure(text=f"Showing {shown} of {len(hosts)}" if shown != len(hosts) else "")
        if sel:
            self.table.tree.selection_set(sel)
            self._show(h=self.table.rows[sel])
        else:
            self.panel.clear()

    def _deployments(self, h):
        return [d for d in self.store.deployments if d.enabled and core.targets(self.store, d, h)]

    def _show(self, h):
        from .manage_screens import dep_title
        deps = self._deployments(h)
        fields = [("Client", self._client(h)), ("Tags", ", ".join(self.store.tags_of(h)) or "None")]
        sections = []
        if deps:
            sections.append(("Deployments aimed at it", "\n".join(dep_title(self.store, d) for d in deps[:10]),
                             False))
        body = self.panel.show("pc", "teal", host_text(self.app, self.store, h), "PC", fields=fields,
                               sections=sections)
        caption(body, "Tags").pack(fill="x", pady=(4, 4))
        self.tag_entry = ctk.CTkEntry(body, height=32, font=theme.font_style("body"),
                                      placeholder_text="e.g. Laptops, Reception")
        tags = ", ".join(self.store.tags_of(h))
        if tags:
            self.tag_entry.insert(0, tags)
        self.tag_entry.pack(fill="x")
        known = self.store.all_tags()
        if known:
            ctk.CTkLabel(body, text="In use: " + ", ".join(known[:10]), font=theme.font_style("small"),
                         text_color=P["muted"], anchor="w", justify="left", wraplength=260).pack(anchor="w")
        soft_button(body, "Save tags", lambda: self._save_tags(h), width=110).pack(anchor="w", pady=(8, 0))

    def _save_tags(self, h):
        ok, m = attempt(self.app, "save the tags", lambda: self.store.set_tags(h, self.tag_entry.get()))
        if ok:
            self.app.toast(f"Tags saved for {h}: {', '.join(m.tags) or 'none'}")
            self.fill(h)
