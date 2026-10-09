"""Shared pieces for the Manage screens: a row table that can hold status pills, the details side panel, the page
layout (toolbar, table card, side panel) and hiding other clients while presenting."""
from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from . import theme
from .screens import Screen
from .widgets import AutoScroll, EmptyState, Icon, IconBadge, Pill, StatusPill, bind_all, caption, divider, \
    link_button, primary_button, secondary_button

P = theme.PALETTE
HIDDEN = "Hidden client"
PANEL_WIDTH = 320


# ---------------------------------------------------------------------------- presentation mode
def client_text(app, names: str) -> str:
    """Client name(s) as shown: while presenting, every client but the one in the context bar is hidden."""
    if not names or not theme.presentation():
        return names
    cur = app.context.get("client")
    parts = [p.strip() for p in names.split(",") if p.strip()]
    shown = [p if p == cur else HIDDEN for p in parts]
    return ", ".join(dict.fromkeys(shown))


def client_hidden(app, client) -> bool:
    return theme.presentation() and client is not None and client.name != app.context.get("client")


def host_text(app, store, host: str) -> str:
    """A PC name, masked while presenting when it belongs to a hidden client (names often carry the client's)."""
    if not host or not theme.presentation() or store is None:
        return host
    clients = store.clients_of(host)
    if clients and all(client_hidden(app, c) for c in clients):
        return theme.mask(host)
    return host


# ---------------------------------------------------------------------------- row table
class RowTable(ctk.CTkFrame):
    """Table built from widgets so cells can hold status pills. Looks like DataTable: a quiet header row, 40 px rows,
    hover and selection.

    columns: [(key, title, width, kind)]; kind is text, strong, muted, mono or pills (a cell value of
    [(text, tone)] or a status word). The first column stretches."""

    def __init__(self, master, columns, on_select=None, on_open=None, empty=None):
        super().__init__(master, fg_color="transparent")
        self.columns, self.on_select, self.on_open = columns, on_select, on_open
        self._empty_spec, self._empty = empty, None
        head = ctk.CTkFrame(self, fg_color=P["surface_2"], corner_radius=0, height=34)
        head.pack(fill="x")
        for i, col in enumerate(columns):
            caption(head, col[1]).grid(row=0, column=i, sticky="w", padx=(16 if i == 0 else 0, 12), pady=9)
        self._cols(head)
        self.list = AutoScroll(self)
        self.list.pack(fill="both", expand=True)
        self.rows: list = []
        self.selected = None

    def _cols(self, frame):
        frame.grid_columnconfigure(0, weight=1)
        for i, col in enumerate(self.columns[1:], 1):
            frame.grid_columnconfigure(i, minsize=col[2] + 12)

    def clear(self):
        for w in self.list.winfo_children():
            w.destroy()
        self.rows, self.selected = [], None

    def add(self, values, data=None):
        if self.rows:
            divider(self.list).pack(fill="x")
        r = ctk.CTkFrame(self.list, fg_color="transparent", corner_radius=0, height=40)
        r.pack(fill="x")
        for i, (col, v) in enumerate(zip(self.columns, values)):
            kind = col[3] if len(col) > 3 else "text"
            pad = (16 if i == 0 else 0, 12)
            if kind == "pills":
                cell = ctk.CTkFrame(r, fg_color="transparent", width=col[2])
                cell.grid(row=0, column=i, sticky="w", padx=pad, pady=8)
                for text, tone in ([(v, None)] if isinstance(v, str) else v or []):
                    StatusPill(cell, text, tone).pack(side="left", padx=(0, 6))
                continue
            font = {"strong": theme.font_style("body_strong"), "mono": theme.mono(12)}.get(
                kind, theme.font_style("body"))
            color = {"muted": P["muted"], "strong": P["text"], "text": P["text_2"], "mono": P["text_2"]}[kind]
            ctk.CTkLabel(r, text=str(v), font=font, text_color=color, anchor="w", height=24,
                         width=0 if i == 0 else col[2]).grid(row=0, column=i, sticky="w", padx=pad, pady=8)
        self._cols(r)
        row = (r, data)
        self.rows.append(row)
        bind_all(r, "<Enter>", lambda _e: self._paint(row, hot=True))
        bind_all(r, "<Leave>", lambda _e: self._leave(row))
        bind_all(r, "<Button-1>", lambda _e: self.select(data))
        if self.on_open:
            bind_all(r, "<Double-Button-1>", lambda _e: self.on_open(data))
        return r

    def _leave(self, row):
        try:
            x, y = self.winfo_pointerxy()
            w = self.winfo_containing(x, y)
        except (tk.TclError, KeyError):
            w = None
        while w is not None:
            if w is row[0]:
                return
            w = getattr(w, "master", None)
        self._paint(row, hot=False)

    def _paint(self, row, hot=False):
        frame, data = row
        sel = self.selected is not None and data is self.selected
        try:
            frame.configure(fg_color=P["selected"] if sel else P["hover"] if hot else "transparent")
        except tk.TclError:
            pass

    def select(self, data, fire=True):
        self.selected = data
        for row in self.rows:
            self._paint(row)
        if fire and self.on_select and data is not None:
            self.on_select(data)

    def done(self):
        if self._empty is not None:
            self._empty.destroy()
            self._empty = None
        if not self.rows and self._empty_spec:
            title, text = self._empty_spec[:2]
            self._empty = EmptyState(self.list, title, text, *self._empty_spec[2:])
            self._empty.pack(pady=40)


# ---------------------------------------------------------------------------- details panel
class DetailPanel(ctk.CTkFrame):
    """Right-hand panel with the selected row's details and its actions."""

    def __init__(self, master, empty_title, empty_text, icon="manage"):
        super().__init__(master, corner_radius=12, fg_color=P["surface"], border_width=1, border_color=P["border"],
                         width=PANEL_WIDTH)
        self.empty = (empty_title, empty_text, icon)
        self.body = None
        self.clear()

    def _reset(self):
        for w in self.winfo_children():
            w.destroy()

    def clear(self):
        self._reset()
        title, text, icon = self.empty
        box = ctk.CTkFrame(self, fg_color="transparent")
        box.place(relx=.5, rely=.4, anchor="center")
        Icon(box, icon, 40, "muted", "surface").pack(pady=(0, 10))
        ctk.CTkLabel(box, text=title, font=theme.font_style("h3"), text_color=P["text"]).pack()
        ctk.CTkLabel(box, text=text, font=theme.font_style("small"), text_color=P["muted"], wraplength=240,
                     justify="center").pack(pady=(4, 0))

    def show(self, icon, tone, title, sub="", pills=(), fields=(), sections=(), actions=()):
        """fields: [(label, value)]; sections: [(caption, text, mono)]; actions: [(text, command, style)], style
        primary, secondary or danger. Returns a frame for extra content (switches, the schedule editor)."""
        self._reset()
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(18, 0))
        IconBadge(top, icon, 40, tone).pack(side="left", anchor="n", padx=(0, 12))
        t = ctk.CTkFrame(top, fg_color="transparent")
        t.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(t, text=title, font=theme.font_style("h3"), text_color=P["text"], anchor="w", justify="left",
                     wraplength=PANEL_WIDTH - 110).pack(anchor="w")
        if sub:
            ctk.CTkLabel(t, text=sub, font=theme.font_style("small"), text_color=P["muted"], anchor="w",
                         justify="left", wraplength=PANEL_WIDTH - 110).pack(anchor="w")
        if pills:
            row = ctk.CTkFrame(self, fg_color="transparent")
            row.pack(fill="x", padx=20, pady=(12, 0))
            for text, tone_ in pills:
                (StatusPill(row, text, tone_) if tone_ != "tag" else Pill(row, text, "neutral")).pack(
                    side="left", padx=(0, 6))
        acts = ctk.CTkFrame(self, fg_color="transparent")
        acts.pack(side="bottom", fill="x", padx=20, pady=(8, 18))
        scroll = AutoScroll(self)
        scroll.pack(fill="both", expand=True, padx=(20, 6), pady=(14, 0))
        if fields:
            grid = ctk.CTkFrame(scroll, fg_color="transparent")
            grid.pack(fill="x", padx=(0, 12))
            for i, (k, v) in enumerate(fields):
                ctk.CTkLabel(grid, text=k, font=theme.font_style("small"), text_color=P["muted"], anchor="w",
                             width=92, height=20).grid(row=i, column=0, sticky="nw", pady=4)
                ctk.CTkLabel(grid, text=str(v), font=theme.font_style("body"), text_color=P["text"], anchor="w",
                             justify="left", wraplength=PANEL_WIDTH - 150, height=20).grid(row=i, column=1,
                                                                                           sticky="nw", pady=4)
            grid.grid_columnconfigure(1, weight=1)
        # custom content (switches, the schedule editor) sits right under the fields; height=1 so an empty one
        # doesn't reserve CTkFrame's default 200 px
        self.body = ctk.CTkFrame(scroll, fg_color="transparent", height=1)
        self.body.pack(fill="x", padx=(0, 12), pady=(8, 0))
        for cap, text, mono in sections:
            caption(scroll, cap).pack(fill="x", pady=(12, 4))
            box = ctk.CTkFrame(scroll, fg_color=P["surface_2"], corner_radius=8)
            box.pack(fill="x", padx=(0, 12))
            ctk.CTkLabel(box, text=text, font=theme.mono(11) if mono else theme.font_style("small"),
                         text_color=P["text_2"], anchor="w", justify="left",
                         wraplength=PANEL_WIDTH - 90).pack(anchor="w", padx=10, pady=8)
        self.buttons = {}
        grid = ctk.CTkFrame(acts, fg_color="transparent", height=1)
        grid.grid_columnconfigure((0, 1), weight=1, uniform="a")
        n = 0
        for text, cmd, style in actions:
            if style == "primary":
                b = primary_button(acts, text, cmd, width=0, height=36)
                b.pack(fill="x", pady=(6, 0), before=grid if grid.winfo_manager() else None)
            else:
                b = secondary_button(grid, text, cmd, width=0, height=32,
                                     text_tone="danger" if style == "danger" else "text")
                b.grid(row=n // 2, column=n % 2, sticky="ew", padx=(0 if n % 2 == 0 else 4, 0 if n % 2 else 4),
                       pady=(8, 0))
                n += 1
                if not grid.winfo_manager():
                    grid.pack(fill="x")
                last = b
            self.buttons[text] = b
        if n % 2:
            last.grid_configure(columnspan=2, padx=0)
        return self.body


# ---------------------------------------------------------------------------- page layout
class EntryText:
    """StringVar-like access to an entry's text. A CTkEntry with a textvariable never shows its placeholder,
    so the search boxes use this instead."""

    def __init__(self, entry):
        self.entry, self.callbacks, self._last = entry, [], ""
        entry.bind("<KeyRelease>", lambda _e: self._changed(), add="+")
        entry.bind("<<Paste>>", lambda _e: entry.after_idle(self._changed), add="+")

    def get(self):
        return self.entry.get()

    def set(self, text):
        self.entry.delete(0, "end")
        if text:
            self.entry.insert(0, text)
        self._changed()

    def trace_add(self, _mode, fn):
        self.callbacks.append(fn)

    def _changed(self):
        text = self.entry.get()
        if text == self._last:
            return
        self._last = text
        for fn in self.callbacks:
            fn()



class ManagePage(Screen):
    """A Manage list page: header with actions, a toolbar (search and filters), the table card and the side panel."""
    guide_topic = "deploy"
    refreshable = True

    def __init__(self, master, app, title, subtitle):
        super().__init__(master, app, title, subtitle, show_back=False)
        self.body_frame = self.new_body()
        from .deploy_screens import open_store
        self.store = open_store(self, self.body_frame)
        self.search_var = None
        self.table = None
        self.panel = None

    def page(self, card_title, count=None, search=None, filters=None, filter_value=None, on_filter=None,
             panel_empty=("Nothing selected", "Select a row to see its details.", "manage"), top=None):
        """Build the page; returns the card body for the table."""
        body = self.body_frame
        if top is not None:
            top(body)
        if search or filters:
            bar = self.bar = ctk.CTkFrame(body, fg_color="transparent")
            bar.pack(fill="x", pady=(0, 12))
            if search:
                wrap = ctk.CTkFrame(bar, fg_color=P["surface"], corner_radius=8, border_width=1,
                                    border_color=P["control_border"], height=36)
                wrap.pack(side="left")
                Icon(wrap, "search", 16, "muted", "surface").pack(side="left", padx=(10, 0))
                self.search = ctk.CTkEntry(wrap, width=280, height=32, border_width=0, fg_color=P["surface"],
                                           placeholder_text=search, font=theme.font_style("body"))
                self.search.pack(side="left", padx=(4, 6), pady=2)
                self.search_var = EntryText(self.search)
            if filters:
                self.filter = ctk.CTkSegmentedButton(bar, values=filters, height=34, font=theme.font_style("body"),
                                                     command=lambda v: on_filter and on_filter(v))
                self.filter.set(filter_value or filters[0])
                self.filter.pack(side="left", padx=(12, 0))
            self.count_lbl = ctk.CTkLabel(bar, text="", font=theme.font_style("small"), text_color=P["muted"])
            self.count_lbl.pack(side="right")
        main = ctk.CTkFrame(body, fg_color="transparent")
        main.pack(fill="both", expand=True)
        main.grid_columnconfigure(0, weight=1)
        main.grid_columnconfigure(1, minsize=PANEL_WIDTH)
        main.grid_rowconfigure(0, weight=1)
        self.card = ctk.CTkFrame(main, corner_radius=12, fg_color=P["surface"], border_width=1,
                                 border_color=P["border"])
        self.card.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        head = ctk.CTkFrame(self.card, fg_color="transparent", height=36)
        head.pack(fill="x", padx=20, pady=(14, 6))
        self.card_title = ctk.CTkLabel(head, text=card_title, font=theme.font_style("h3"), text_color=P["text"])
        self.card_title.pack(side="left")
        self.card_count = ctk.CTkLabel(head, text="" if count is None else str(count), font=theme.font_style("small"),
                                       text_color=P["muted"])
        self.card_count.pack(side="left", padx=(8, 0), pady=(3, 0))
        self.card_head = head
        inner = ctk.CTkFrame(self.card, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=1, pady=(0, 10))
        self.panel = DetailPanel(main, *panel_empty)
        self.panel.grid(row=0, column=1, sticky="nsew")
        return inner

    def card_action(self, text, command):
        link_button(self.card_head, text, command).pack(side="right")
