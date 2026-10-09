"""Settings: appearance, Mossbit personality, presentation mode, technician name, branding, shortcuts and About."""
from __future__ import annotations

import os
import shutil
import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk

from ..report import LOGO_MAX, LOGO_TYPES
from ..util import APP_VERSION, app_dir
from . import theme
from .guide import load_settings, save_settings
from .screens import Screen
from .shell import ProductMark, branding, initials, technician
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

        card = Card(col, "Branding")
        card.pack(fill="x", pady=(0, 16))
        brand = branding()
        row = ctk.CTkFrame(card.body, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkLabel(row, text="Company name", font=theme.font_style("body_strong"), text_color=P["text"], width=140,
                     anchor="w").pack(side="left")
        self.company = tk.StringVar(value=brand["company"])
        ctk.CTkEntry(row, textvariable=self.company, width=260, height=34,
                     placeholder_text="Your MSP's name").pack(side="left")
        row = ctk.CTkFrame(card.body, fg_color="transparent")
        row.pack(fill="x", pady=(10, 0))
        ctk.CTkLabel(row, text="Logo", font=theme.font_style("body_strong"), text_color=P["text"], width=140,
                     anchor="w").pack(side="left")
        self.logo = brand["logo"]
        self.logo_lbl = ctk.CTkLabel(row, text="", font=theme.font_style("body"), text_color=P["text_2"], width=260,
                                     anchor="w")
        self.logo_lbl.pack(side="left")
        secondary_button(row, "Choose…", self._pick_logo, width=90, height=34).pack(side="left", padx=(10, 0))
        secondary_button(row, "Remove", self._clear_logo, width=80, height=34).pack(side="left", padx=(8, 0))
        self._show_logo()
        row = ctk.CTkFrame(card.body, fg_color="transparent")
        row.pack(fill="x", pady=(10, 0))
        primary_button(row, "Save", self._save_brand, width=80, height=34).pack(side="left", padx=(140, 0))
        ctk.CTkLabel(card.body, text="Printed at the top of wipe certificates. PNG, JPG or SVG, up to 3 MB. The "
                                     "certificate stays plain black and white, with no SectorSmith colours or mascot.",
                     font=theme.font_style("small"), text_color=P["muted"], anchor="w", justify="left",
                     wraplength=640).pack(fill="x", pady=(8, 0))

        integrations_section(col, app)

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

    def _show_logo(self):
        self.logo_lbl.configure(text=os.path.basename(self.logo) if self.logo else "No logo")

    def _pick_logo(self):
        p = filedialog.askopenfilename(title="Choose your logo", filetypes=[
            ("Images", " ".join("*" + e for e in LOGO_TYPES)), ("All files", "*.*")])
        if not p:
            return
        ext = os.path.splitext(p)[1].lower()
        if ext not in LOGO_TYPES or os.path.getsize(p) > LOGO_MAX:
            self.app.toast("Use a PNG, JPG, GIF, WebP or SVG image up to 3 MB.", "warn")
            return
        self.logo = p
        self._show_logo()

    def _clear_logo(self):
        self.logo = ""
        self._show_logo()

    def _save_brand(self):
        logo = self.logo
        if logo and os.path.isfile(logo):
            # keep a copy next to the settings, so certificates still get it when the original moves
            keep = app_dir() / f"brand_logo{os.path.splitext(logo)[1].lower()}"
            if os.path.abspath(logo) != os.path.abspath(keep):
                try:
                    shutil.copyfile(logo, keep)
                    logo = str(keep)
                except OSError:
                    pass  # couldn't copy it: keep using the original file
        self.logo = logo
        save_settings(brand_company=self.company.get().strip(), brand_logo=logo)
        self._show_logo()
        self.app.toast("Saved. New wipe certificates carry this name and logo.")

    def _save_tech(self):
        name = self.tech.get().strip()
        save_settings(technician=name)
        self.app.rail.avatar.configure(text=initials(name or "Technician"))
        self.app.toast("Saved. New jobs are stamped with this name.")


# ---------------------------------------------------------------------------- integrations
def integrations_section(col, app):
    """ScreenConnect instance (plus a per-client one) and the Active Directory check. Everything is off until set
    up, and nothing here stores a password or API key."""
    from ..integrations import ad, screenconnect as sc
    from .integrations_ui import local_client_lines
    from .widgets import Pill, divider

    card = Card(col, "Integrations")
    card.pack(fill="x", pady=(0, 16))
    st = load_settings()
    client = app.context.get("client")

    def label(row, text):
        ctk.CTkLabel(row, text=text, font=theme.font_style("body_strong"), text_color=P["text"], width=140,
                     anchor="w").pack(side="left")

    def small(text, tone="muted", pady=(8, 0)):
        ctk.CTkLabel(card.body, text=text, font=theme.font_style("small"), text_color=P[tone], anchor="w",
                     justify="left", wraplength=640).pack(fill="x", pady=pady)

    head = ctk.CTkFrame(card.body, fg_color="transparent")
    head.pack(fill="x", pady=(0, 8))
    ctk.CTkLabel(head, text="ScreenConnect", font=theme.font_style("body_strong"), text_color=P["text"]).pack(
        side="left")
    state = Pill(head, "On" if sc.configured(st) else "Off", "success" if sc.configured(st) else "neutral")
    state.pack(side="left", padx=8)

    def entry_row(text, value, placeholder, on_save, on_clear):
        row = ctk.CTkFrame(card.body, fg_color="transparent")
        row.pack(fill="x", pady=(0, 8))
        label(row, text)
        var = tk.StringVar(value=value or "")
        ctk.CTkEntry(row, textvariable=var, width=320, height=34, placeholder_text=placeholder).pack(side="left")
        primary_button(row, "Save", lambda: on_save(var), width=80, height=34).pack(side="left", padx=(10, 0))
        secondary_button(row, "Clear", lambda: on_clear(var), width=70, height=34).pack(side="left", padx=(8, 0))

    def refresh_state():
        on = sc.configured(load_settings())
        state.configure(text="On" if on else "Off", fg_color=P["success_soft" if on else "neutral_soft"],
                        text_color=P["success" if on else "neutral"])

    def save_default(var):
        try:
            url = sc.normalize_url(var.get())
        except ValueError as e:
            app.toast(str(e), "warn")
            return
        var.set(url)
        save_settings(**{sc.KEY_URL: url})
        refresh_state()
        app.toast("Saved. Machines and Ctrl+K now offer Connect with ScreenConnect.")

    def clear_default(var):
        var.set("")
        save_settings(**{sc.KEY_URL: ""})
        refresh_state()
        app.toast("ScreenConnect address removed.")

    def save_client(var):
        per = dict(load_settings().get(sc.KEY_CLIENTS) or {})
        try:
            per[client] = sc.normalize_url(var.get())
        except ValueError as e:
            app.toast(str(e), "warn")
            return
        var.set(per[client])
        save_settings(**{sc.KEY_CLIENTS: per})
        refresh_state()
        app.toast(f"Saved. {client} machines open on this instance.")

    def clear_client(var):
        per = dict(load_settings().get(sc.KEY_CLIENTS) or {})
        per.pop(client, None)
        var.set("")
        save_settings(**{sc.KEY_CLIENTS: per})
        refresh_state()
        app.toast(f"{client} uses the default instance again.")

    entry_row("Instance address", st.get(sc.KEY_URL), "https://control.example.com", save_default, clear_default)
    if client:
        entry_row(f"For {client}"[:20], (st.get(sc.KEY_CLIENTS) or {}).get(client), "Uses the address above",
                  save_client, clear_client)
    small("Adds Connect with ScreenConnect to Machines and Ctrl+K. It opens your host page in the browser and "
          "searches for the machine name; you sign in to ScreenConnect as usual. No API key is stored."
          + ("" if client else " Choose a client (Ctrl+Shift+C) to give that client its own instance."), pady=(0, 0))
    for line in local_client_lines():
        small(f"This PC has the ScreenConnect client:  {line}", "text_2")

    divider(card.body).pack(fill="x", pady=16)
    head = ctk.CTkFrame(card.body, fg_color="transparent")
    head.pack(fill="x")
    ctk.CTkLabel(head, text="Active Directory", font=theme.font_style("body_strong"), text_color=P["text"]).pack(
        side="left")
    Pill(head, "This PC's domain", "neutral").pack(side="left", padx=8)
    result = ctk.CTkLabel(head, text="", font=theme.font_style("small"), text_color=P["muted"])

    def check():
        result.configure(text="Checking...", text_color=P["muted"])
        name = os.environ.get("USERNAME") or os.environ.get("USER") or ""

        def done(res):
            if result.winfo_exists():
                ok = res.status in ("ok", "not_found")
                text = f"Domain {res.domain} answers" if ok and res.domain else res.text()
                result.configure(text=text, text_color=P["success" if ok else "muted"])
        app.background(lambda: ad.lookup_user(name, timeout=20), done,
                       lambda e: result.winfo_exists() and result.configure(text=str(e), text_color=P["warn"]))
    secondary_button(head, "Check this PC", check, width=120, height=30).pack(side="right")
    result.pack(side="right", padx=10)
    small("Migrate user shows the domain account it is moving: name, email, department, groups, last logon, home "
          "drive and logon script. It asks the domain this PC is joined to, as you, through ADSI. No credentials "
          "are stored, and it stays quiet on PCs that aren't domain joined.")
