"""UI glue for the optional integrations: ScreenConnect actions (Machines page, Ctrl+K) and the domain account card
in Migrate user. Settings has its own Integrations section in settings.py."""
from __future__ import annotations

import threading
import time

import customtkinter as ctk

from ..integrations import ad, screenconnect
from ..util import Cancelled
from . import theme
from .widgets import IconBadge, Pill

P = theme.PALETTE


# ---------------------------------------------------------------------------- ScreenConnect
def sc_instance(app) -> str | None:
    """The instance for the current client, or None while ScreenConnect isn't set up."""
    from .guide import load_settings
    return screenconnect.instance_for(load_settings(), app.context.get("client"))


def machine_name(m) -> str:
    info = (m.info() if m.is_local else (m.info_cache or {})) or {}
    return str(info.get("hostname") or m.label)


def open_screenconnect(app, name: str):
    inst = sc_instance(app)
    if not inst:
        app.toast("Set your ScreenConnect address in Settings first.", "warn")
        return
    try:
        screenconnect.open_machine(inst, name)
    except (ValueError, OSError) as e:
        app.toast(f"Couldn't open ScreenConnect: {e}", "warn")
        return
    app.toast(f"Opened ScreenConnect, searching for {name}.")


def local_client_lines() -> list[str]:
    try:
        return [screenconnect.describe_client(c) for c in screenconnect.local_clients()]
    except Exception:  # noqa: BLE001  registry quirks never break a page
        return []


# ---------------------------------------------------------------------------- Active Directory
class DomainUserCard(ctk.CTkFrame):
    """Looks the picked user up in this PC's domain (background, cancellable, with a timeout) and shows who it is.
    Stays empty when the PC isn't domain joined. on_result(ADUser or None) lets the wizard keep the answer."""

    def __init__(self, master, app, profile_path: str, on_result=None):
        super().__init__(master, fg_color="transparent")
        self.app, self.on_result = app, on_result
        self.cancel = threading.Event()
        self.names = ad.candidates(profile_path)
        self.wait_lbl = None
        self.bind("<Destroy>", lambda e: e.widget is self and self.cancel.set(), add="+")
        if not self.names:
            return
        self.after(300, self._waiting)
        app.background(lambda: ad.lookup_user(profile_path, cancel=self.cancel), self._show, self._failed)

    def _waiting(self):
        if self.winfo_exists() and not self.winfo_children():
            self.wait_lbl = ctk.CTkLabel(self, text=f"Looking up {self.names[0]} in the domain...",
                                         font=theme.font_style("small"), text_color=P["muted"], anchor="w")
            self.wait_lbl.pack(anchor="w", pady=(6, 0))

    def _clear(self):
        if not self.winfo_exists():
            return False
        for w in self.winfo_children():
            w.destroy()
        return True

    def _failed(self, e):
        if isinstance(e, Cancelled) or not self._clear():
            return
        self._line(f"Domain lookup failed: {e}")

    def _line(self, text):
        ctk.CTkLabel(self, text=text, font=theme.font_style("small"), text_color=P["muted"], anchor="w",
                     justify="left", wraplength=760).pack(anchor="w", pady=(6, 0))

    def _show(self, res: ad.ADLookup):
        if self.cancel.is_set() or not self._clear():
            return
        if self.on_result:
            self.on_result(res.user if res.ok else None)
        if res.status == "not_joined" or (res.status == "unavailable" and res.message == "Needs Windows."):
            return  # nothing to say on a workgroup PC
        if not res.ok:
            where = f" on {res.domain}" if res.domain else ""
            self._line(f"Domain: {res.text()}{where}." if res.status == "not_found" else f"Domain: {res.text()}")
            return
        self._card(res)

    def _card(self, res):
        u = res.user
        card = ctk.CTkFrame(self, corner_radius=12, fg_color=P["surface"], border_width=1, border_color=P["border"])
        card.pack(fill="x", pady=(8, 0), padx=(0, 6))
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(10, 2))
        IconBadge(top, "user", 32, "info").pack(side="left", padx=(0, 10))
        t = ctk.CTkFrame(top, fg_color="transparent")
        t.pack(side="left", fill="x", expand=True)
        head = ctk.CTkFrame(t, fg_color="transparent")
        head.pack(anchor="w")
        ctk.CTkLabel(head, text=u.display_name or u.sam, font=theme.font_style("body_strong"),
                     text_color=P["text"]).pack(side="left")
        dom = res.domain.split(".")[0].upper() if res.domain else ""
        ctk.CTkLabel(head, text=f"{dom}\\{u.sam}" if dom else u.sam, font=theme.mono(12),
                     text_color=P["text_2"]).pack(side="left", padx=(8, 8))
        Pill(head, "Enabled" if u.enabled else "Disabled", "success" if u.enabled else "danger").pack(side="left")
        when = time.strftime("%d %b %Y", time.localtime(u.last_logon)) if u.last_logon else "never"
        bits = [b for b in (u.email, u.department, f"Last logon about {when}") if b]
        ctk.CTkLabel(t, text="  ·  ".join(bits), font=theme.font_style("small"), text_color=P["muted"],
                     anchor="w", height=18).pack(anchor="w")
        if u.groups:
            more = f" and {len(u.groups) - 6} more" if len(u.groups) > 6 else ""
            ctk.CTkLabel(t, text="Groups: " + ", ".join(u.groups[:6]) + more, font=theme.font_style("small"),
                         text_color=P["text_2"], anchor="w", justify="left", wraplength=700, height=18).pack(anchor="w")
        # home drive and logon script live on the server: say so, the migration doesn't copy them
        notes = u.migration_notes()
        if notes:
            ctk.CTkLabel(card, text="\n".join(notes), font=theme.font_style("small"), text_color=P["warn"],
                         anchor="w", justify="left", wraplength=720).pack(anchor="w", padx=(56, 14), pady=(2, 0))
        ctk.CTkFrame(card, fg_color="transparent", height=8).pack()
