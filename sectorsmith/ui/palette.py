"""Command palette (Ctrl+K or /): fuzzy search over every task, machine, client, job and setting.

Prefixes: '>' commands only, '@' clients, '!' machines, '#' set a ticket number. Destructive tasks only open their
wizard, the palette never runs them directly."""
from __future__ import annotations

import time
import tkinter as tk

import customtkinter as ctk

from . import nav, theme
from .widgets import Icon, Shadow, _install_overlay_handlers, bind_all, caption, divider, place_sized

P = theme.PALETTE
GROUPS = ("Recent", "Actions", "Machines", "Clients", "Jobs", "Settings")


def score(query: str, text: str) -> float:
    """Simple fuzzy score: substring beats word-start beats in-order subsequence. 0 means no match."""
    q, t = query.lower().strip(), text.lower()
    if not q:
        return 1.0
    if t.startswith(q):
        return 100 - len(t) / 100
    i = t.find(q)
    if i >= 0:
        return (80 if t[i - 1] in " .-_/(" else 60) - i / 100
    pos, gaps = 0, 0
    for ch in q:
        j = t.find(ch, pos)
        if j < 0:
            return 0.0
        gaps += j - pos
        pos = j + 1
    if gaps > 2 * len(q) + 2:  # letters scattered across unrelated words
        return 0.0
    return max(1.0, 40 - gaps)


class Entry:
    def __init__(self, group, label, run, icon="chevron_right", hint="", words=""):
        self.group, self.label, self.run, self.icon, self.hint, self.words = group, label, run, icon, hint, words


class CommandPalette(ctk.CTkFrame):
    WIDTH = 640
    MAX_PER_GROUP = 5

    def __init__(self, app):
        _install_overlay_handlers(app)
        for ov in list(app._smith_overlays):
            ov.close()
        super().__init__(app, corner_radius=14, fg_color=P["raised"], border_width=1, border_color=P["border_strong"],
                         width=self.WIDTH)
        self.app = app
        self.anchor = None
        self.shadow = Shadow(app, radius=14, layers=(12, 7, 3))
        self.entries = self._collect()
        self.shown: list[Entry] = []
        self.rows = []
        self.cur = 0
        top = ctk.CTkFrame(self, fg_color="transparent", height=54)
        top.pack(fill="x", padx=16)
        top.pack_propagate(False)
        Icon(top, "search", 18, "accent", "raised").pack(side="left")
        self.var = tk.StringVar()
        self.entry = ctk.CTkEntry(top, textvariable=self.var, border_width=0, fg_color=P["raised"],
                                  font=theme.font(16), placeholder_text="Search tasks, machines, clients and jobs",
                                  height=40)
        self.entry.pack(side="left", fill="x", expand=True, padx=10)
        ctk.CTkLabel(top, text=" Esc ", font=theme.font(10, "bold"), text_color=P["muted"], fg_color=P["surface_2"],
                     corner_radius=4, height=20).pack(side="right")
        divider(self).pack(fill="x")
        self.list = ctk.CTkFrame(self, fg_color="transparent")
        self.list.pack(fill="x", padx=8, pady=6)
        divider(self).pack(fill="x")
        ctk.CTkLabel(self, text="Up and Down to move  ·  Enter to open  ·  > commands  ·  @ clients  "
                                "·  ! machines  ·  # ticket", font=theme.font_style("small"),
                     text_color=P["muted"], anchor="w", height=34).pack(fill="x", padx=16)
        self.var.trace_add("write", lambda *_: self._filter())
        e = self.entry._entry
        e.bind("<Down>", lambda _e: self._move(1))
        e.bind("<Up>", lambda _e: self._move(-1))
        e.bind("<Return>", lambda _e: self._run())
        e.bind("<KP_Enter>", lambda _e: self._run())
        self._filter()
        self._place()
        app._smith_overlays.append(self)
        self.entry.after(30, self.entry.focus_set)

    def _place(self):
        self.app.update_idletasks()
        x = max(16, (self.app.winfo_width() - self.WIDTH) // 2)
        y = 96
        self.shadow.place(x, y, self.WIDTH, self.winfo_reqheight())
        place_sized(self, x, y, self.WIDTH)
        self.lift()

    # -- data -----------------------------------------------------------------
    def _collect(self):
        app = self.app
        out = []
        for cat, it in nav.all_items():
            out.append(Entry("Actions" if cat.key != "settings" else "Settings", it.label,
                             lambda c=cat, i=it: app.open_item(c, i), it.icon, cat.label, it.hint))
        for cat in nav.CATEGORIES:
            out.append(Entry("Actions", f"Go to {cat.label}", lambda k=cat.key: app.open_category(k), cat.icon,
                             cat.shortcut, "open"))
        pres = theme.presentation()
        cmds = [
            ("Stop presenting" if pres else "Start presenting", lambda: app.set_presentation(not pres), "present",
             "Ctrl+Shift+P", "presentation mode screen share hide"),
            ("Switch to dark mode", lambda: app.set_appearance("Dark"), "eye", "Appearance", "theme"),
            ("Switch to light mode", lambda: app.set_appearance("Light"), "eye", "Appearance", "theme"),
            ("Match the Windows theme", lambda: app.set_appearance("System"), "eye", "Appearance", "system theme"),
            ("Toggle the side panel", app.toggle_subnav, "manage", "Ctrl+B", "sub-nav sidebar collapse"),
            ("Choose a client", app.client_menu, "building", "Ctrl+Shift+C", "switch client"),
        ]
        for p in theme.PERSONALITIES:
            cmds.append((f"Mossbit personality: {p}", lambda p=p: app.set_personality(p), "user", "Personality",
                         "mascot mossbit"))
        out += [Entry("Settings", label, run, icon, hint, words) for label, run, icon, hint, words in cmds]
        for m in app.machines():
            label = "This PC" if m.is_local else m.label
            sub = "" if m.is_local else getattr(m, "address", "")
            out.append(Entry("Machines", label, lambda: app.open_target("machines", "linked"), "pc",
                             "Machines", f"{sub} machine"))
        from .integrations_ui import machine_name, open_screenconnect, sc_instance
        if sc_instance(app):  # only once ScreenConnect is set up in Settings
            for m in app.machines():
                name = machine_name(m)
                out.append(Entry("Machines", f"Connect with ScreenConnect: {name}",
                                 lambda n=name: open_screenconnect(app, n), "link", "ScreenConnect",
                                 "remote control connectwise screenconnect"))
        cur = app.context.get("client")
        for n in app.client_names():
            if pres and n != cur:
                continue
            out.append(Entry("Clients", n, lambda n=n: app.set_client(n), "building", "Client", "customer"))
        for j in reversed(app.jobs[-20:]):
            when = time.strftime("%H:%M", time.localtime(j["started"]))
            out.append(Entry("Jobs", f"{j['task']}  ·  {j['result']}", lambda: app.open_target("jobs", "history"),
                             "jobs", f"Today {when}", f"{j.get('ticket', '')} {j.get('client', '')} {j.get('detail')}"))
        return out

    # -- filtering ------------------------------------------------------------
    def _filter(self):
        q = self.var.get()
        groups = None
        if q[:1] in (">", "@", "!", "#"):
            pre, q = q[0], q[1:]
            if pre == "#":
                t = q.strip().lstrip("#")
                self._show([Entry("Actions", f"Set ticket #{t}" if t else "Type a ticket number",
                                  (lambda: self.app.set_ticket(t)) if t else (lambda: None), "ticket", "Ticket")])
                return
            groups = {">": ("Actions", "Settings"), "@": ("Clients",), "!": ("Machines",)}[pre]
        if not q.strip() and groups is None:
            recent = [Entry("Recent", e.label, e.run, e.icon, e.hint) for e in self._recent()]
            base = [e for e in self.entries if e.group == "Actions" and not e.label.startswith("Go to")]
            self._show(recent + base[:6])
            return
        scored = []
        for e in self.entries:
            if groups and e.group not in groups:
                continue
            s = max(score(q, e.label), score(q, e.words) * .7 if e.words else 0, score(q, e.hint) * .5)
            if s > 0:
                scored.append((s, e))
        scored.sort(key=lambda x: -x[0])
        per, picked = {}, []
        for _s, e in scored:
            if per.get(e.group, 0) < self.MAX_PER_GROUP:
                per[e.group] = per.get(e.group, 0) + 1
                picked.append(e)
        picked.sort(key=lambda e: GROUPS.index(e.group))
        self._show(picked)

    def _recent(self):
        seen, out = set(), []
        for label in reversed(self.app.recent):
            e = next((x for x in self.entries if x.label == label), None)
            if e and label not in seen:
                seen.add(label)
                out.append(e)
        return out[:4]

    def _show(self, entries):
        for w in self.list.winfo_children():
            w.destroy()
        self.shown, self.rows = entries[:14], []
        if not self.shown:
            ctk.CTkLabel(self.list, text="Nothing matches. Try fewer letters, or > for commands.",
                         font=theme.font_style("body"), text_color=P["muted"], height=44).pack(fill="x")
        group = None
        for i, e in enumerate(self.shown):
            if e.group != group:
                group = e.group
                caption(self.list, group).pack(fill="x", padx=10, pady=(8, 2))
            row = ctk.CTkFrame(self.list, fg_color="transparent", corner_radius=8, height=36)
            row.pack(fill="x")
            row.pack_propagate(False)
            ic = Icon(row, e.icon, 16, "text_2", "raised")
            ic.pack(side="left", padx=(10, 10))
            lbl = ctk.CTkLabel(row, text=e.label, font=theme.font_style("body"), text_color=P["text"], anchor="w")
            lbl.pack(side="left", fill="x", expand=True)
            if e.hint:
                ctk.CTkLabel(row, text=e.hint, font=theme.font_style("small"), text_color=P["muted"]).pack(
                    side="right", padx=12)
            bind_all(row, "<Button-1>", lambda _e, i=i: self._run(i))
            bind_all(row, "<Motion>", lambda _e, i=i: self.cur != i and self._select(i))
            self.rows.append((row, ic, lbl))
        self.cur = 0
        self._select(0)
        if self.winfo_ismapped():
            self.after_idle(self._place)

    def _select(self, i):
        if not self.rows:
            return
        self.cur = max(0, min(i, len(self.rows) - 1))
        for k, (row, ic, lbl) in enumerate(self.rows):
            on = k == self.cur
            row.configure(fg_color=P["selected"] if on else "transparent")
            ic.set(color="accent" if on else "text_2", bg="selected" if on else "raised")
            lbl.configure(text_color=P["accent"] if on else P["text"])

    def _move(self, d):
        self._select(self.cur + d)
        return "break"

    def _run(self, i=None):
        if i is not None:
            self.cur = i
        if not self.shown:
            return "break"
        e = self.shown[self.cur]
        self.close()
        self.app.recent.append(e.label)
        e.run()
        return "break"

    def close(self):
        try:
            self.app._smith_overlays.remove(self)
        except ValueError:
            pass  # already closed
        self.shadow.destroy()
        self.destroy()
