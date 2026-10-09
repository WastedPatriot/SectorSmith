"""Design tokens, fonts, theme switching and the personality / presentation settings for the SectorSmith UI."""
from __future__ import annotations

import sys
import time

import customtkinter as ctk

# (light, dark) pairs, CustomTkinter widgets take these directly
TOKENS = {
    # surfaces
    "bg": ("#F6F7F9", "#0D0E12"),            # window canvas
    "rail": ("#FFFFFF", "#101116"),          # category rail
    "subnav": ("#FBFBFC", "#121318"),        # second level panel
    "surface": ("#FFFFFF", "#15161C"),       # cards, tables
    "surface_2": ("#F3F4F7", "#1A1B22"),     # table header, wells, quick actions
    "raised": ("#FFFFFF", "#1C1E26"),        # menus, palette, toasts
    "hover": ("#F1F2F6", "#1F212A"),
    "selected": ("#EEECFD", "#221F3D"),
    "border": ("#E3E6EC", "#252833"),
    "border_strong": ("#CDD2DB", "#343846"),
    "control_border": ("#8A91A0", "#646B7A"),  # inputs and checkboxes, 3:1 against surface
    "scrim": ("#0D0E12", "#000000"),
    # text
    "text": ("#14161F", "#ECEEF3"),
    "text_2": ("#454C5C", "#B4BAC7"),
    "muted": ("#687082", "#8A91A0"),
    "disabled": ("#A3A9B6", "#555B69"),
    # brand
    "accent": ("#5747E0", "#8B7FFF"),
    "accent_hover": ("#4636C8", "#A197FF"),
    "accent_soft": ("#EEECFD", "#221F3D"),
    "on_accent": ("#FFFFFF", "#0D0E12"),
    "brand_pink": ("#E0407A", "#FF5C99"),    # Mossbit's cheeks only, never UI chrome
    "focus": ("#5747E0", "#A197FF"),
    "focus_ring": ("#3B2DC4", "#B8B0FF"),      # keyboard focus ring, 2 px
    "focus_ring_fill": ("#14161F", "#FFFFFF"),   # the ring on filled buttons, in contrast to their fill
    # status
    "success": ("#0A7650", "#3DD39B"),
    "success_soft": ("#E3F5EC", "#10291F"),
    "warn": ("#A15C00", "#F2B34C"),
    "warn_soft": ("#FDF0DB", "#2E2412"),
    "danger": ("#C8243F", "#FF6B81"),
    "danger_soft": ("#FCE6EA", "#331820"),
    "danger_hover": ("#A81C33", "#FF8A9B"),
    "info": ("#1F66D1", "#5B9BFF"),
    "info_soft": ("#E4EEFC", "#14223A"),
    "neutral": ("#5B6272", "#A3A9B6"),
    "neutral_soft": ("#EEF0F3", "#22242C"),
    "track": ("#E7E9EE", "#262935"),
    # category tile hues, small icon tiles only
    "teal": ("#0E8A7A", "#39CDB8"),
    "teal_soft": ("#E0F4F1", "#0F2A27"),
    "amber": ("#A15C00", "#F2B34C"),
    "amber_soft": ("#FDF0DB", "#2E2412"),
}

# names the older screens still use, mapped onto the new tokens
LEGACY = {
    "panel": "subnav",
    "card": "surface",
    "card_hover": "hover",
    "violet": "accent",
    "violet_soft": "accent_soft",
}

PALETTE = dict(TOKENS)
PALETTE.update({old: TOKENS[new] for old, new in LEGACY.items()})

CATEGORY = {"disk": "accent", "machines": "teal", "manage": "info", "jobs": "amber", "settings": "neutral",
            "erase": "danger"}

TYPE = {  # name: (size px at 100%, weight, line height)
    "display": (28, "bold", 34),
    "h1": (22, "bold", 28),
    "h2": (17, "bold", 24),
    "h3": (15, "bold", 20),
    "body": (13, "normal", 20),
    "body_strong": (13, "bold", 20),
    "small": (12, "normal", 16),
    "caption": (11, "bold", 14),
    "mono": (12, "normal", 18),
    "metric": (26, "bold", 30),
}
SPACE = (0, 4, 8, 12, 16, 20, 24, 32, 40, 48)
RADIUS = {"xs": 4, "sm": 6, "md": 8, "lg": 12, "xl": 14, "pill": 999}
PAGE_PAD = 32

PERSONALITIES = ("Full", "Subtle", "Off")
TAGLINE = "The bench toolkit for MSP technicians."

_listeners: list = []
_state = {"personality": None, "presentation": False, "reduce_motion": None, "system_motion": (None, 0.0)}
UI_SIZES = (90, 100, 115, 130)
_families: dict = {}


def is_dark() -> bool:
    return ctk.get_appearance_mode().lower() == "dark"


def c(name: str) -> str:
    """Resolve a palette colour for the current mode (for plain tk Canvas drawing)."""
    light, dark = PALETTE[name]
    return dark if is_dark() else light


def on_theme_change(fn):
    _listeners.append(fn)


def _notify():
    for fn in list(_listeners):
        try:
            fn()
        except Exception:  # widget may have been destroyed
            try:
                _listeners.remove(fn)
            except ValueError:
                pass


def set_mode(mode: str):
    ctk.set_appearance_mode(mode)
    _notify()


# ---------------------------------------------------------------- personality and presentation
def _settings():
    from .guide import load_settings
    return load_settings()


def personality() -> str:
    """The Mossbit setting the user picked: Full, Subtle (default) or Off."""
    if _state["personality"] is None:
        p = _settings().get("personality")
        _state["personality"] = p if p in PERSONALITIES else "Subtle"
    return _state["personality"]


def effective_personality() -> str:
    """What to show right now. Presentation mode always hides Mossbit."""
    return "Off" if presentation() else personality()


def set_personality(value: str):
    from .guide import save_settings
    if value not in PERSONALITIES:
        return
    _state["personality"] = value
    save_settings(personality=value)
    _notify()


def presentation() -> bool:
    return _state["presentation"]


def set_presentation(on: bool):
    """Not saved: presentation mode is for this session's screen share only."""
    _state["presentation"] = bool(on)
    _notify()


def mask(text: str, keep: int = 4) -> str:
    """Hide all but the last characters of a serial number while presenting."""
    if not text or not presentation():
        return text
    return "•" * max(0, min(8, len(text) - keep)) + text[-keep:]


def reduce_motion_setting() -> bool:
    """Settings > Accessibility > Reduce motion."""
    if _state["reduce_motion"] is None:
        _state["reduce_motion"] = bool(_settings().get("reduce_motion"))
    return _state["reduce_motion"]


def set_reduce_motion(on: bool):
    from .guide import save_settings
    _state["reduce_motion"] = bool(on)
    save_settings(reduce_motion=bool(on))
    _notify()


def _system_reduced_motion() -> bool:
    """Windows 'Show animations in Windows' turned off. Checked at most every few seconds."""
    if sys.platform != "win32":
        return False
    val, at = _state["system_motion"]
    if val is not None and time.monotonic() - at < 5:
        return val
    try:
        import ctypes
        on = ctypes.c_bool(True)
        ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(on), 0)  # SPI_GETCLIENTAREAANIMATION
        val = not on.value
    except Exception:  # noqa: BLE001
        val = False
    _state["system_motion"] = (val, time.monotonic())
    return val


def reduced_motion() -> bool:
    """No slides, sweeps or roaming: the Reduce motion setting, or Windows animations turned off."""
    return reduce_motion_setting() or _system_reduced_motion()


# ---------------------------------------------------------------- interface size and DPI
def ui_size() -> int:
    """Settings > Interface size, in percent."""
    try:
        v = int(_settings().get("ui_size") or 100)
    except (TypeError, ValueError):
        v = 100
    return v if v in UI_SIZES else 100


def apply_ui_size(percent: int | None = None):
    """Scale every CustomTkinter widget by the interface size. It multiplies the monitor's own DPI scaling, which
    CustomTkinter tracks per monitor."""
    ctk.set_widget_scaling((percent or ui_size()) / 100)


def scaling(widget=None) -> float:
    """Pixels per design pixel right now: monitor DPI times the interface size."""
    try:
        return ctk.ScalingTracker.get_widget_scaling(widget)
    except Exception:  # noqa: BLE001  widget not tracked yet
        return ctk.ScalingTracker.widget_scaling


def enable_dpi_awareness():
    """Make the process per-monitor DPI aware before Tk starts, so text stays sharp on scaled displays and
    CustomTkinter rescales when the window moves to another monitor. Windows 8.1 and later get per-monitor
    awareness; older Windows falls back to system awareness. Safe to call more than once."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except (AttributeError, OSError):
        pass  # no shcore.dll before Windows 8.1
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


# ---------------------------------------------------------------- fonts
def _pick(kind: str, wanted: tuple, fallback: str) -> str:
    if kind in _families:
        return _families[kind]
    try:
        import tkinter.font as tkfont
        have = set(tkfont.families())
    except Exception:  # noqa: BLE001  no Tk root yet, try again later
        return wanted[0] if wanted else fallback
    _families[kind] = next((f for f in wanted if f in have), fallback)
    return _families[kind]


def family() -> str:
    if sys.platform == "win32":
        return _pick("ui", ("Segoe UI Variable Text", "Segoe UI"), "Segoe UI")
    if sys.platform == "darwin":
        return _pick("ui", ("SF Pro Text", "Helvetica Neue"), "Helvetica")
    return _pick("ui", ("Inter", "Noto Sans", "DejaVu Sans"), "TkDefaultFont")


def mono_family() -> str:
    if sys.platform == "win32":
        return _pick("mono", ("Cascadia Mono", "Consolas"), "Consolas")
    if sys.platform == "darwin":
        return _pick("mono", ("SF Mono", "Menlo"), "Menlo")
    return _pick("mono", ("DejaVu Sans Mono", "Liberation Mono"), "TkFixedFont")


def font(size=13, weight="normal"):
    return ctk.CTkFont(family=family(), size=size, weight=weight)


def font_style(name: str):
    size, weight, _lh = TYPE[name]
    return font(size, weight)


def mono(size=12, weight="normal"):
    return ctk.CTkFont(family=mono_family(), size=size, weight=weight)


def lerp_color(a: str, b: str, t: float) -> str:
    a, b = a.lstrip("#"), b.lstrip("#")
    a = "".join(ch * 2 for ch in a) if len(a) == 3 else a
    b = "".join(ch * 2 for ch in b) if len(b) == 3 else b
    ca = [int(a[i:i + 2], 16) for i in (0, 2, 4)]
    cb = [int(b[i:i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{int(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


# ---------------------------------------------------------------- widget defaults
def apply_ctk_defaults():
    """Point CustomTkinter's own defaults at our tokens, so any widget that does not set a colour
    (checkboxes, scrollbars, entries, menus) still matches. Call once before building widgets."""
    t = ctk.ThemeManager.theme
    p = PALETTE

    def put(widget, **kw):
        t.setdefault(widget, {}).update({k: list(v) if isinstance(v, tuple) else v for k, v in kw.items()})
    put("CTkButton", fg_color=p["accent"], hover_color=p["accent_hover"], text_color=p["on_accent"],
        text_color_disabled=p["disabled"], border_color=p["border_strong"], corner_radius=8, border_width=0)
    put("CTkEntry", fg_color=p["surface"], border_color=p["control_border"], text_color=p["text"],
        placeholder_text_color=p["muted"], corner_radius=6, border_width=1)
    put("CTkTextbox", fg_color=p["surface"], border_color=p["border"], text_color=p["text"], corner_radius=8,
        border_width=1, scrollbar_button_color=p["border_strong"], scrollbar_button_hover_color=p["control_border"])
    put("CTkScrollbar", fg_color="transparent", button_color=p["border_strong"],
        button_hover_color=p["control_border"], corner_radius=1000, border_spacing=2)
    put("CTkScrollableFrame", label_fg_color=p["surface_2"])
    put("CTkCheckBox", fg_color=p["accent"], hover_color=p["accent_hover"], border_color=p["control_border"],
        checkmark_color=p["on_accent"], text_color=p["text"], text_color_disabled=p["disabled"], corner_radius=4,
        border_width=1)
    put("CTkRadioButton", fg_color=p["accent"], hover_color=p["accent_hover"], border_color=p["control_border"],
        text_color=p["text"], text_color_disabled=p["disabled"], border_width_unchecked=1, border_width_checked=5)
    put("CTkSwitch", fg_color=p["border_strong"], progress_color=p["accent"], button_color=("#FFFFFF", "#ECEEF3"),
        button_hover_color=("#FFFFFF", "#FFFFFF"), text_color=p["text"], text_color_disabled=p["disabled"])
    put("CTkProgressBar", fg_color=p["track"], progress_color=p["accent"], border_color=p["border"],
        corner_radius=1000)
    put("CTkSlider", fg_color=p["track"], progress_color=p["accent"], button_color=p["accent"],
        button_hover_color=p["accent_hover"])
    put("CTkSegmentedButton", fg_color=p["surface_2"], selected_color=p["surface"], selected_hover_color=p["surface"],
        unselected_color=p["surface_2"], unselected_hover_color=p["hover"], text_color=p["text"],
        text_color_disabled=p["disabled"], corner_radius=8, border_width=3)
    put("CTkOptionMenu", fg_color=p["surface"], button_color=p["surface_2"], button_hover_color=p["hover"],
        text_color=p["text"], text_color_disabled=p["disabled"], corner_radius=6)
    put("CTkComboBox", fg_color=p["surface"], border_color=p["control_border"], button_color=p["surface_2"],
        button_hover_color=p["hover"], text_color=p["text"], text_color_disabled=p["disabled"], corner_radius=6,
        border_width=1)
    put("DropdownMenu", fg_color=p["raised"], hover_color=p["hover"], text_color=p["text"])
    put("CTkLabel", text_color=p["text"])
    put("CTkFrame", fg_color=p["surface"], top_fg_color=p["surface_2"], border_color=p["border"])
    put("CTkToplevel", fg_color=p["bg"])
    put("CTk", fg_color=p["bg"])
    _patch_segmented()
    _patch_scrollbar()


def _patch_scrollbar():
    """Thinner scrollbars everywhere (CTk's default is 16 px and has no theme key for it)."""
    sb = ctk.CTkScrollbar
    if getattr(sb, "_smith_patched", False):
        return
    orig = sb.__init__

    def init(self, *args, width=None, height=None, **kw):
        vertical = kw.get("orientation", "vertical") == "vertical"
        if vertical and width is None:
            width = 12
        if not vertical and height is None:
            height = 12
        orig(self, *args, width=width, height=height, **kw)
    sb.__init__ = init
    sb._smith_patched = True


def _patch_segmented():
    """CTkSegmentedButton has one text colour for every segment. Give the selected segment the accent text on a
    raised surface and the rest secondary text, for every segmented control in the app, including older screens
    that still ask for an accent fill."""
    seg = ctk.CTkSegmentedButton
    if getattr(seg, "_smith_patched", False):
        return
    orig_sel, orig_unsel, orig_create = seg._select_button_by_value, seg._unselect_button_by_value, seg._create_button
    accent = tuple(PALETTE["accent"])

    def select(self, value):
        if tuple(self._sb_selected_color) == accent:
            self._sb_selected_color = PALETTE["surface"]
            self._sb_selected_hover_color = PALETTE["surface"]
        orig_sel(self, value)
        btn = self._buttons_dict.get(value)
        if btn is not None:
            btn.configure(text_color=PALETTE["accent"])

    def unselect(self, value):
        orig_unsel(self, value)
        btn = self._buttons_dict.get(value)
        if btn is not None:
            btn.configure(text_color=PALETTE["text_2"])

    def create(self, index, value):
        btn = orig_create(self, index, value)
        btn.configure(text_color=PALETTE["text_2"])
        return btn
    seg._select_button_by_value, seg._unselect_button_by_value, seg._create_button = select, unselect, create
    seg._smith_patched = True


def style_ttk(root):
    """Theme ttk Treeviews (big lists) and their scrollbars to match the current mode."""
    from tkinter import ttk
    st = ttk.Style(root)
    if "clam" in st.theme_names():
        st.theme_use("clam")
    surf, surf2, text, muted = c("surface"), c("surface_2"), c("text"), c("muted")
    k = scaling(root)  # negative font sizes are pixels, so tables follow DPI and the interface size like CTk
    st.configure("Smith.Treeview", background=surf, fieldbackground=surf, foreground=text, rowheight=round(36 * k),
                 borderwidth=0, relief="flat", font=(family(), -round(13 * k)))
    st.map("Smith.Treeview", background=[("selected", c("selected"))], foreground=[("selected", text)])
    st.configure("Smith.Treeview.Heading", background=surf2, foreground=muted, relief="flat",
                 font=(family(), -round(12 * k), "bold"), borderwidth=0, padding=(round(8 * k), round(8 * k)),
                 lightcolor=surf2, darkcolor=surf2,
                 bordercolor=c("border"))
    st.map("Smith.Treeview.Heading", background=[("active", c("hover"))])
    st.layout("Smith.Treeview", [("Smith.Treeview.treearea", {"sticky": "nswe"})])
    # scrollbars without arrows: a thin thumb on a quiet track
    thumb = c("border_strong")
    for orient, side in (("Vertical", "ns"), ("Horizontal", "we")):
        name = f"Smith.{orient}.TScrollbar"
        st.layout(name, [(f"{orient}.Scrollbar.trough", {"sticky": side, "children": [
            (f"{orient}.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        st.configure(name, background=thumb, troughcolor=surf, bordercolor=surf, lightcolor=thumb, darkcolor=thumb,
                     arrowcolor=muted, gripcount=0, relief="flat", borderwidth=0, width=round(8 * k),
                     arrowsize=round(8 * k))
        st.map(name, background=[("active", c("control_border")), ("pressed", c("control_border"))],
               lightcolor=[("active", c("control_border"))], darkcolor=[("active", c("control_border"))])
