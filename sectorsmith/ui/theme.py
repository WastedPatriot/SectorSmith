"""Palette, fonts and theme switching for the SectorSmith UI."""
from __future__ import annotations

import sys

import customtkinter as ctk

# (light, dark) pairs — CustomTkinter widgets take these directly
PALETTE = {
    "bg": ("#F5F3FA", "#121019"),
    "panel": ("#EDEAF5", "#191623"),
    "card": ("#FFFFFF", "#211D2E"),
    "card_hover": ("#FBEFF5", "#2B2640"),
    "border": ("#E4E0EE", "#2E2943"),
    "text": ("#1D1A2B", "#F4F1FF"),
    "muted": ("#6E6884", "#A49EBB"),
    "accent": ("#E0407A", "#FF5C99"),
    "accent_hover": ("#C93069", "#FF7AAD"),
    "accent_soft": ("#FCE3EE", "#3A2033"),
    "violet": ("#7656F5", "#9A82FF"),
    "violet_soft": ("#ECE7FF", "#2A2348"),
    "success": ("#14A97A", "#2EE0A8"),
    "success_soft": ("#DDF6EE", "#163A30"),
    "warn": ("#D98A00", "#FFB938"),
    "warn_soft": ("#FFF1D6", "#3D2F12"),
    "danger": ("#E0284A", "#FF5470"),
    "danger_soft": ("#FDE2E7", "#40171F"),
    "track": ("#E7E3F0", "#2C2740"),
}

_listeners: list = []


def is_dark() -> bool:
    return ctk.get_appearance_mode().lower() == "dark"


def c(name: str) -> str:
    """Resolve a palette colour for the current mode (for plain tk Canvas drawing)."""
    light, dark = PALETTE[name]
    return dark if is_dark() else light


def on_theme_change(fn):
    _listeners.append(fn)


def set_mode(mode: str):
    ctk.set_appearance_mode(mode)
    for fn in list(_listeners):
        try:
            fn()
        except Exception:  # widget may have been destroyed
            try:
                _listeners.remove(fn)
            except ValueError:
                pass


def family() -> str:
    if sys.platform == "win32":
        return "Segoe UI"
    if sys.platform == "darwin":
        return "SF Pro Display"
    return "Inter"


def font(size=13, weight="normal"):
    return ctk.CTkFont(family=family(), size=size, weight=weight)


def mono(size=11):
    return ctk.CTkFont(family="Consolas" if sys.platform == "win32" else "DejaVu Sans Mono", size=size)


def lerp_color(a: str, b: str, t: float) -> str:
    a, b = a.lstrip("#"), b.lstrip("#")
    a = "".join(ch * 2 for ch in a) if len(a) == 3 else a
    b = "".join(ch * 2 for ch in b) if len(b) == 3 else b
    ca = [int(a[i:i + 2], 16) for i in (0, 2, 4)]
    cb = [int(b[i:i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{int(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def style_ttk(root):
    """Make ttk Treeviews match the current theme (used for big result lists)."""
    from tkinter import ttk
    st = ttk.Style(root)
    if "clam" in st.theme_names():
        st.theme_use("clam")
    st.configure("Smith.Treeview", background=c("card"), fieldbackground=c("card"), foreground=c("text"),
                 rowheight=30, borderwidth=0, font=(family(), 10))
    st.map("Smith.Treeview", background=[("selected", c("accent"))], foreground=[("selected", "#ffffff")])
    st.configure("Smith.Treeview.Heading", background=c("panel"), foreground=c("muted"), relief="flat",
                 font=(family(), 10, "bold"), borderwidth=0, padding=6)
    st.map("Smith.Treeview.Heading", background=[("active", c("card_hover"))])
    st.configure("Smith.Vertical.TScrollbar", background=c("panel"), troughcolor=c("card"), borderwidth=0,
                 arrowcolor=c("muted"))
    st.layout("Smith.Treeview", [("Smith.Treeview.treearea", {"sticky": "nswe"})])
