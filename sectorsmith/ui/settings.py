"""Settings: appearance, Mossbit personality, presentation mode, technician name, shortcuts and About."""
from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from ..util import APP_VERSION
from . import theme
from .guide import load_settings, save_settings
from .screens import Screen
from .shell import ProductMark, initials, technician
from .widgets import AutoScroll, Card, OptionCard, primary_button, secondary_button, segmented

P = theme.PALETTE

PERSONALITY_HELP = {
    "Full": "Mossbit lives in the side panel, talks in a speech bubble, sweeps progress bars with sparkles and "
            "celebrates when a job is done.",
    "Subtle": "Mossbit sweeps progress bars and shows up on success and empty screens. No speech, no companion.",
    "Off": "No mascot. Plain progress bars, outline icons and neutral system copy.",
}
SHORTCUTS = [("Ctrl+K or /", "Search or jump to anything"), ("Ctrl+1 to Ctrl+7", "Home, Recover, Erase, Drives, "
                                                                                  "Machines, Manage, Jobs"),
             ("Ctrl+B", "Show or hide the side panel"), ("Ctrl+Shift+C", "Choose a client"),
             ("Ctrl+Shift+P", "Start or stop presenting"), ("F5", "Refresh the drive list"),
             ("Esc", "Close a menu or the search")]


class SettingsScreen(Screen):
    guide_topic = "start"
    refreshable = True

    def __init__(self, master, app):
        super().__init__(master, app, "Settings", "Preferences for this technician on this PC.", show_back=False)
        body = self.new_body()
        page = AutoScroll(body)
        page.pack(fill="both", expand=True)
        col = ctk.CTkFrame(page, fg_color="transparent")
        col.pack(fill="x", padx=(0, 12))

        card = Card(col, "Appearance")
        card.pack(fill="x", pady=(0, 16))
        row = ctk.CTkFrame(card.body, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkLabel(row, text="Theme", font=theme.font_style("body_strong"), text_color=P["text"], width=140,
                     anchor="w").pack(side="left")
        seg = segmented(row, ["Light", "Dark", "System"], command=app.set_appearance)
        seg.set(app.mode.get())
        seg.pack(side="left")
        app.mode.widgets.append(seg)

        card = Card(col, "Mossbit personality")
        card.pack(fill="x", pady=(0, 16))
        grp = []
        opts = ctk.CTkFrame(card.body, fg_color="transparent")
        opts.pack(fill="x")
        cur = theme.personality()
        for i, name in enumerate(theme.PERSONALITIES):
            oc = OptionCard(opts, name, PERSONALITY_HELP[name], name, grp,
                            badge_text="Default" if name == "Subtle" else None)
            oc.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0 if i == 2 else 6))
            oc.desc.configure(wraplength=220)
            opts.grid_columnconfigure(i, weight=1, uniform="p")
        for oc in grp:
            if oc.value == cur:
                oc.select()
            oc.on_select = lambda v: v != theme.personality() and app.set_personality(v)
        ctk.CTkLabel(card.body, text="Mossbit never appears on certificates, reports or anything you export.",
                     font=theme.font_style("small"), text_color=P["muted"], anchor="w").pack(fill="x", pady=(10, 0))

        card = Card(col, "Presentation mode")
        card.pack(fill="x", pady=(0, 16))
        row = ctk.CTkFrame(card.body, fg_color="transparent")
        row.pack(fill="x")
        var = tk.BooleanVar(value=theme.presentation())
        ctk.CTkSwitch(row, text="Presenting to a client", variable=var, font=theme.font_style("body_strong"),
                      command=lambda: app.set_presentation(var.get())).pack(side="left")
        ctk.CTkLabel(row, text="Ctrl+Shift+P", font=theme.mono(12), text_color=P["muted"]).pack(side="right")
        ctk.CTkLabel(card.body, text="Turns Mossbit off, hides other clients' names and shows only the last four "
                                     "characters of serial numbers. It lasts until you turn it off or close "
                                     "SectorSmith.", font=theme.font_style("small"), text_color=P["muted"],
                     anchor="w", justify="left", wraplength=640).pack(fill="x", pady=(8, 0))

        card = Card(col, "Technician")
        card.pack(fill="x", pady=(0, 16))
        row = ctk.CTkFrame(card.body, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkLabel(row, text="Your name", font=theme.font_style("body_strong"), text_color=P["text"], width=140,
                     anchor="w").pack(side="left")
        self.tech = tk.StringVar(value=load_settings().get("technician") or technician())
        ctk.CTkEntry(row, textvariable=self.tech, width=260, height=34).pack(side="left")
        primary_button(row, "Save", self._save_tech, width=80, height=34).pack(side="left", padx=10)
        ctk.CTkLabel(card.body, text="Every job is stamped with this name, the client and the ticket.",
                     font=theme.font_style("small"), text_color=P["muted"], anchor="w").pack(fill="x", pady=(8, 0))

        card = Card(col, "Keyboard")
        card.pack(fill="x", pady=(0, 16))
        for keys, what in SHORTCUTS:
            r = ctk.CTkFrame(card.body, fg_color="transparent")
            r.pack(fill="x", pady=2)
            ctk.CTkLabel(r, text=keys, font=theme.mono(12), text_color=P["text"], width=170, anchor="w").pack(
                side="left")
            ctk.CTkLabel(r, text=what, font=theme.font_style("body"), text_color=P["text_2"], anchor="w").pack(
                side="left")

        card = Card(col, "About")
        card.pack(fill="x", pady=(0, 16))
        row = ctk.CTkFrame(card.body, fg_color="transparent")
        row.pack(fill="x")
        ProductMark(row, 40, bg="surface").pack(side="left", padx=(0, 14))
        t = ctk.CTkFrame(row, fg_color="transparent")
        t.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(t, text=f"SectorSmith Toolkit {APP_VERSION}", font=theme.font_style("h3"), text_color=P["text"],
                     anchor="w").pack(fill="x")
        ctk.CTkLabel(t, text=theme.TAGLINE, font=theme.font_style("body"), text_color=P["muted"], anchor="w").pack(
            fill="x")
        secondary_button(row, "How to use", lambda: app.open_guide(), width=120).pack(side="right")

    def _save_tech(self):
        name = self.tech.get().strip()
        save_settings(technician=name)
        self.app.rail.avatar.configure(text=initials(name or "Technician"))
        self.app.toast("Saved. New jobs are stamped with this name.")
