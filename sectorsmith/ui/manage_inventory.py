"""Manage, Inventory: every PC the library knows with its last snapshot (Windows, model, serial, CPU, RAM, disks,
BitLocker, last restart, installed software), its client and tags. Searchable; tags and the snapshot feed
deployment targeting."""
from __future__ import annotations

import time

import customtkinter as ctk

from ..deploy import core, inventory
from . import theme
from .deploy_screens import ACTION_LABEL, RunWizard, _plural, _short, attempt, hostname
from .manage_common import HIDDEN, ManagePage, client_hidden, host_text
from .widgets import DataTable, caption, soft_button

P = theme.PALETTE


def _ago(ts):
    from .manage_screens import _ago as ago
    return ago(ts)


def _short_os(snap):
    os_name = (snap or {}).get("os") or ""
    return os_name.replace("Microsoft ", "") or "-"


class InventoryScreen(ManagePage):
    def __init__(self, master, app, select=None):
        super().__init__(master, app, "Inventory", "Every PC SectorSmith knows, from its last snapshot: Windows, "
                                                   "hardware and software. Tags let a deployment aim at some PCs.")
        if self.store is None:
            return
        self.header_action("Snapshot linked PCs", self._snap_all, primary=True, width=180)
        inner = self.page("PCs", search="Search PCs, models, serials, software or tags",
                          panel_empty=("No PC selected", "Select a PC to see its snapshot, software and tags.", "pc"))
        self.table = DataTable(inner, [("pc", "PC", 150), ("client", "Client", 120), ("os", "OS", 150),
                                       ("model", "Model", 140), ("tags", "Tags", 110), ("seen", "Last seen", 100)],
                               height=9, on_select=self._show,
                               empty=("No PCs yet", "PCs show up here once they're linked, in a client or tagged. "
                                                    "Linking a PC takes a snapshot.", None, "pc"))
        self.table.pack(fill="both", expand=True)
        self.snaps = inventory.all_snapshots(self.store)
        self.search_var.trace_add("write", lambda *_: self.fill())
        self.fill(select)

    def _client(self, host):
        c = self.store.client_of(host)
        if c is None:
            return "-"
        return HIDDEN if client_hidden(self.app, c) else c.name

    def _linked(self):
        return {hostname(m).lower(): m for m in self.app.machines()}

    def _hosts(self):
        seen = {h.lower(): h for h in self.store.known_hosts()}
        for snap in self.snaps.values():
            seen.setdefault(snap["hostname"].lower(), snap["hostname"])
        for low, m in self._linked().items():
            seen.setdefault(low, hostname(m))
        return sorted(seen.values(), key=str.lower)

    def _row(self, h):
        snap = self.snaps.get(h.lower())
        m = self.store.machine(h)
        seen = (snap or {}).get("taken") or (m.last_seen if m else 0)
        model = " ".join(x for x in ((snap or {}).get("manufacturer", "").split(" ")[0], (snap or {}).get("model"))
                         if x) or "-"
        return (host_text(self.app, self.store, h), self._client(h), _short(_short_os(snap), 26), _short(model, 22),
                ", ".join(self.store.tags_of(h)) or "-", _ago(seen) if seen else "-")

    def fill(self, select=None):
        self.table.clear()
        q = self.search_var.get()
        hosts = self._hosts()
        sel = None
        shown = 0
        for h in hosts:
            row = self._row(h)
            words = [w for w in q.lower().split() if w not in " ".join(map(str, row)).lower()]
            if words and not inventory.matches(self.snaps.get(h.lower()) or {}, " ".join(words)):
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
            self._show(self.table.rows[sel])
        else:
            self.panel.clear()

    def _show(self, h):
        from .manage_screens import dep_title
        snap = self.snaps.get(h.lower())
        fields = [("Client", self._client(h))] + inventory.summary(snap or {})
        if snap:
            fields.append(("Snapshot", _ago(snap.get("taken"))))
        sections = []
        exp = [r for r in inventory.expected(self.store, h, snap) if r["action"] not in ("none",)] if snap else []
        if exp:
            sections.append(("Next run, from this snapshot", "\n".join(
                f"{r['name']}: {ACTION_LABEL.get(r['action'], 'checked on the PC')} ({r['current']})"
                for r in exp[:8]), False))
        deps = [d for d in self.store.deployments if d.enabled and core.targets(self.store, d, h)]
        if deps:
            sections.append(("Deployments aimed at it", "\n".join(dep_title(self.store, d) for d in deps[:8]), False))
        soft = (snap or {}).get("software") or []
        if soft:
            sections.append((f"Software ({len(soft)})", "\n".join(
                f"{e['name']} {e.get('version') or ''}".strip() for e in soft[:20])
                + (f"\n...and {len(soft) - 20} more" if len(soft) > 20 else ""), False))
        linked = self._linked().get(h.lower())
        actions = []
        if linked is not None:
            actions.append(("Take a snapshot", lambda: self._snap([linked], h), "primary"))
            actions.append(("Run maintenance...", lambda: self.app.go(RunWizard), "secondary"))
        body = self.panel.show("pc", "teal", host_text(self.app, self.store, h),
                               "Linked now" if linked is not None else ("No snapshot yet" if not snap else "PC"),
                               pills=[("Linked", "success")] if linked is not None else [], fields=fields,
                               sections=sections, actions=actions)
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
        soft_button(body, "Save tags", lambda: self._save_tags(h), width=110).pack(anchor="w", pady=(8, 4))

    def _save_tags(self, h):
        ok, m = attempt(self.app, "save the tags", lambda: self.store.set_tags(h, self.tag_entry.get()))
        if ok:
            self.app.toast(f"Tags saved for {h}: {', '.join(m.tags) or 'none'}")
            self.fill(h)

    # -- snapshots -----------------------------------------------------------
    def _snap_all(self):
        ms = list(self.app.machines())
        if not ms:
            self.app.toast("No PCs are linked right now.", "warn")
            return
        self._snap(ms)

    def _snap(self, machines, select=None):
        store = self.store
        self.app.toast(f"Taking a snapshot of {_plural(len(machines), 'PC')}...", "info")

        def work():
            done = []
            for m in machines:
                snap = inventory.take(m, store)
                if snap:
                    done.append(snap["hostname"])
            return done

        def finished(done):
            if not self.winfo_exists():
                return
            self.snaps = inventory.all_snapshots(store)
            self.app.toast(f"Snapshot taken: {', '.join(done)}" if done else "No snapshot: the PC didn't answer.",
                           "success" if done else "warn")
            self.fill(select or (done[0] if len(done) == 1 else None))
        self.app.background(work, finished, lambda e: self.app.toast(f"Couldn't take a snapshot: {e}", "warn"))


def snapshot_on_connect(app, machines):
    """A newly linked PC: take its inventory snapshot in the background (quietly, the page shows it)."""
    import threading

    from ..util import get_logger
    log = get_logger()
    if not machines:
        return
    try:
        store = app.deploy_store()
    except Exception as e:  # noqa: BLE001
        log.info("Inventory on connect: no Deploy library (%s)", e)
        return

    def work():
        for m in machines:
            t0 = time.time()
            try:
                snap = inventory.take(m, store)
            except Exception as e:  # noqa: BLE001  (a Cancel or a dropped link: nothing to save)
                log.info("Inventory on connect of %s: %s", m.label, e)
                continue
            if snap:
                log.info("Inventory snapshot of %s in %.1f s", snap["hostname"], time.time() - t0)
    threading.Thread(target=work, daemon=True, name="inventory-snapshot").start()
