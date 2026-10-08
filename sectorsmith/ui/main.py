"""SectorSmith main window: sidebar with Patti, animated screen stack, job runner, drag-and-drop."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import filedialog

import customtkinter as ctk

from .. import partitions
from ..device import list_disks, open_image, volume_letters_for
from ..util import APP_NAME, APP_VERSION, Cancelled, Progress, get_logger, is_admin
from . import theme
from .mascot import Mascot, SpeechBubble
from .widgets import Toast, ghost_button

log = get_logger()
P = theme.PALETTE

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _DND = True
except Exception:  # noqa: BLE001 — drag-and-drop is optional
    _DND = False
    TkinterDnD = None
    DND_FILES = None

_Base = (ctk.CTk, TkinterDnD.DnDWrapper) if _DND else (ctk.CTk,)
IMAGE_EXTS = (".img", ".vhd", ".dd", ".raw", ".bin", ".001", ".iso")


class MainWindow(*_Base):
    def __init__(self):
        super().__init__()
        if _DND:
            try:
                self.TkdndVersion = TkinterDnD._require(self)
            except Exception:  # noqa: BLE001
                globals()["_DND"] = False
        ctk.set_default_color_theme("blue")
        self.title(f"{APP_NAME}")
        self.geometry("1320x860")
        self.minsize(1120, 740)
        self.configure(fg_color=P["bg"])
        theme.style_ttk(self)
        theme.on_theme_change(lambda: theme.style_ttk(self))

        self.images = []
        self._inventory = None
        from ..link.endpoint import LocalEndpoint
        self.local_ep = LocalEndpoint(label="This PC")
        self.link = None
        self.machine_listeners = []
        self.q: queue.Queue = queue.Queue()
        self.job: Progress | None = None
        self.job_panel = None
        self.screen = None
        self.drop_handlers = []

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._sidebar()
        self.stage = ctk.CTkFrame(self, fg_color=P["bg"], corner_radius=0)
        self.stage.grid(row=0, column=1, sticky="nsew")

        if _DND:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<DropEnter>>", self._drop_enter)
            self.dnd_bind("<<DropLeave>>", self._drop_leave)
            self.dnd_bind("<<Drop>>", self._drop)

        from .guide import load_settings
        from .screens import Home
        settings = load_settings()
        if settings.get("mode") in ("Light", "Dark", "System"):
            self.mode.set(settings["mode"])
            theme.set_mode(settings["mode"].lower())
        if settings.get("welcomed"):
            self.go(Home, animate=False)
        else:
            from .guide import WelcomeScreen
            self.go(WelcomeScreen, animate=False)
        self._set_icon()
        self.after(120, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._quit)
        self.after(400, lambda: self.mascot.set_mood("idle", "hello"))
        if not is_admin():
            self.after(1500, lambda: self.toast("Not running as administrator — physical drives will be hidden. "
                                                "Disk images still work.", "warn"))

    # ------------------------------------------------------------------ sidebar
    def _sidebar(self):
        sb = ctk.CTkFrame(self, width=300, corner_radius=0, fg_color=P["panel"])
        sb.grid(row=0, column=0, sticky="ns")
        sb.grid_propagate(False)
        logo = ctk.CTkFrame(sb, fg_color="transparent")
        logo.pack(fill="x", padx=24, pady=(26, 6))
        ctk.CTkLabel(logo, text="Sector", font=theme.font(26, "bold"), text_color=P["text"]).pack(side="left")
        ctk.CTkLabel(logo, text="Smith", font=theme.font(26, "bold"), text_color=P["accent"]).pack(side="left")
        ctk.CTkLabel(sb, text=f"disk toolkit · v{APP_VERSION}", font=theme.font(11), text_color=P["muted"]).pack(
            anchor="w", padx=26)

        self.bubble = SpeechBubble(sb)
        self.bubble.pack(fill="x", padx=20, pady=(24, 0))
        self.mascot = Mascot(sb, self.bubble, width=284)
        self.mascot.pack(pady=(8, 0), padx=8)

        self.mach_box = ctk.CTkFrame(sb, fg_color="transparent")
        self.mach_box.pack(fill="x", padx=20, pady=(4, 0))
        self._render_machines()

        bottom = ctk.CTkFrame(sb, fg_color="transparent")
        bottom.pack(side="bottom", fill="x", padx=20, pady=20)
        ctk.CTkLabel(bottom, text="APPEARANCE", font=theme.font(10, "bold"), text_color=P["muted"]).pack(anchor="w")
        self.mode = ctk.CTkSegmentedButton(bottom, values=["Light", "Dark", "System"], command=self._set_mode,
                                           font=theme.font(12, "bold"), selected_color=P["accent"],
                                           selected_hover_color=P["accent_hover"], height=34, corner_radius=17)
        self.mode.set("System")
        self.mode.pack(fill="x", pady=(4, 14))
        ghost_button(bottom, "How to use", lambda: self.open_guide(), width=260).pack(fill="x", pady=3)
        ghost_button(bottom, "Open disk image…", self.open_image_dialog, width=260).pack(fill="x", pady=3)
        ghost_button(bottom, "Advanced tools", self.open_advanced, width=260).pack(fill="x", pady=3)
        adm = is_admin()
        ctk.CTkLabel(bottom, text=("●  Administrator" if adm else "●  Not administrator"),
                     font=theme.font(11, "bold"), text_color=P["success"] if adm else P["warn"]).pack(anchor="w",
                                                                                                      pady=(12, 0))

    # ------------------------------------------------------------------ linked machines
    def ensure_link(self):
        if self.link is None:
            from ..link.server import LinkServer
            self.link = LinkServer(on_connect=lambda ep: self.call_soon(self._machine_event, ep, True),
                                   on_disconnect=lambda ep: self.call_soon(self._machine_event, ep, False))
        if not self.link.running:
            self.link.start()
        return self.link

    def machines(self):
        self.local_ep.extra_images = [d.path for d in self.images]
        remote = [m for m in (self.link.machines if self.link else []) if m.alive]
        return [self.local_ep] + remote

    def _machine_event(self, ep, connected):
        if connected:
            self.toast(f"{ep.label} is linked")
            self.mascot.set_mood("happy", text=f"Say hi to {ep.label}!")
        else:
            self.toast(f"{ep.label} disconnected", "warn")
        self._render_machines()
        for fn in list(self.machine_listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001
                self.machine_listeners.remove(fn)

    def _render_machines(self):
        for w in self.mach_box.winfo_children():
            w.destroy()
        ctk.CTkLabel(self.mach_box, text="MACHINES", font=theme.font(10, "bold"), text_color=P["muted"]).pack(
            anchor="w")
        for m in self.machines():
            row = ctk.CTkFrame(self.mach_box, fg_color="transparent")
            row.pack(fill="x", pady=1)
            ctk.CTkLabel(row, text="●", font=theme.font(12), text_color=P["success"] if not m.is_local
                         else P["violet"], width=14).pack(side="left")
            name = m.label if m.is_local else f"{m.label}  ·  {m.address}"
            ctk.CTkLabel(row, text=name, font=theme.font(12, "bold"), text_color=P["text"]).pack(side="left", padx=4)
        ctk.CTkButton(self.mach_box, text="+  Connect a machine", height=28, corner_radius=14, anchor="w",
                      fg_color="transparent", hover_color=P["card_hover"], text_color=P["accent"],
                      font=theme.font(12, "bold"), command=self.open_connect).pack(fill="x", pady=(2, 0))

    def open_connect(self):
        from .link_screens import ConnectScreen
        self.go(ConnectScreen)

    def background(self, fn, done, error=None):
        """Run fn() in a thread; deliver its result (or exception) on the UI thread."""
        def work():
            try:
                r = fn()
            except Exception as e:  # noqa: BLE001
                log.warning("background task failed: %s", e)
                self.call_soon(error or (lambda ex: self.toast(str(ex), "danger")), e)
                return
            self.call_soon(done, r)
        threading.Thread(target=work, daemon=True).start()

    def _quit(self):
        try:
            if self.link:
                self.link.stop()
        finally:
            self.destroy()

    def _set_icon(self):
        from .mascot import ASSETS
        try:
            self._icon = tk.PhotoImage(file=os.path.join(ASSETS, "icon.png"))
            self.iconphoto(True, self._icon)
            if sys.platform == "win32":  # CustomTkinter resets the icon shortly after start
                self.after(300, lambda: self.iconbitmap(os.path.join(ASSETS, "icon.ico")))
        except tk.TclError:
            pass

    def open_guide(self, topic="start"):
        from .guide import GuideScreen
        self.go(GuideScreen, topic=topic)

    def _set_mode(self, v):
        from .guide import save_settings
        save_settings(mode=v)
        theme.set_mode(v.lower())
        self.mascot.say(text="Ooh, cosy dark mode!" if v == "Dark" else
                        "Nice and bright!" if v == "Light" else "I'll match your Windows setting.")

    # ------------------------------------------------------------------ navigation
    def go(self, screen_cls, animate=True, **kw):
        old = self.screen
        self.drop_handlers = []
        new = screen_cls(self.stage, self, **kw)
        self.screen = new
        if not animate or old is None:
            new.place(relx=0, rely=0, relwidth=1, relheight=1)
            if old is not None:
                old.destroy()
            return new
        new.place(relx=1, rely=0, relwidth=1, relheight=1)
        start = time.monotonic()

        def step():
            t = min(1.0, (time.monotonic() - start) / .32)
            e = 1 - (1 - t) ** 3
            try:
                new.place_configure(relx=1 - e)
                old.place_configure(relx=-.25 * e)
            except tk.TclError:
                return
            if t < 1:
                self.after(12, step)
            else:
                old.destroy()
        step()
        return new

    def home(self):
        from .screens import Home
        self.mascot.set_mood("idle", "hello")
        self.go(Home)

    # ------------------------------------------------------------------ inventory
    def inventory(self, refresh=False):
        """[(Device, PartitionTable|None, error|None)] for disks + opened images."""
        if self._inventory is not None and not refresh:
            return self._inventory
        res = []
        try:
            devs = list_disks()
        except Exception as e:  # noqa: BLE001
            log.error("list_disks failed: %s", e)
            devs = []
        for d in devs + self.images:
            pt, err = None, None
            try:
                pt = partitions.read_partition_table(d)
                letters = volume_letters_for(d)
                for p in pt.partitions:
                    p.mount = letters.get(p.start_lba * d.sector_size, [])
            except OSError as e:
                err = e.strerror or str(e)
            finally:
                d.close()
            res.append((d, pt, err))
        self._inventory = res
        return res

    def add_image(self, path):
        for d in self.images:
            if os.path.abspath(d.path) == os.path.abspath(path):
                return d
        d = open_image(path)
        self.images.append(d)
        self._inventory = None
        self.toast(f"Added {os.path.basename(path)} to your drives")
        return d

    def open_image_dialog(self):
        p = filedialog.askopenfilename(title="Open disk image",
                                       filetypes=[("Disk images", "*.img *.dd *.bin *.raw *.vhd *.001"),
                                                  ("All", "*")])
        if p:
            self.add_image(p)
            if hasattr(self.screen, "refresh_drives"):
                self.screen.refresh_drives()

    def open_advanced(self):
        from ..app import AdvancedWindow
        w = AdvancedWindow(self)
        for d in self.images:
            if all(x.path != d.path for x in w.devices):
                w.devices.append(open_image(d.path))
        w.refresh_disks()
        self.mascot.say(text="Expert tools are open in a new window.")

    # ------------------------------------------------------------------ helpers
    def toast(self, text, tone="success"):
        Toast(self, text, tone)

    @staticmethod
    def clone(dev):
        from ..app import ExpertMixin
        return ExpertMixin.clone_device(dev)

    @staticmethod
    def open_folder(path):
        try:
            if sys.platform == "win32":
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except OSError:
            pass

    def call_soon(self, fn, *args):
        """Thread-safe: run fn(*args) on the UI thread."""
        self.q.put(("call", fn, args))

    # ------------------------------------------------------------------ jobs
    def run_job(self, title, func, on_done, panel, on_error=None, on_cancel=None):
        if self.job is not None:
            self.toast("Hang on — something's already running.", "warn")
            return
        prog = Progress(1, title)
        self.job = prog
        self.job_panel = panel
        self._job_cbs = (on_done, on_error, on_cancel)
        self.mascot.set_mood("working", "working")
        log.info("UI job start: %s", title)

        def worker():
            try:
                self.q.put(("done", func(prog)))
            except Cancelled:
                self.q.put(("cancelled", None))
            except Exception as e:  # noqa: BLE001
                log.error("UI job %s failed: %s", title, traceback.format_exc())
                self.q.put(("error", e))
        threading.Thread(target=worker, daemon=True).start()

    def cancel_job(self):
        if self.job:
            self.job.cancel()

    def _poll(self):
        try:
            for _ in range(3000):
                kind, *rest = self.q.get_nowait()
                if kind == "call":
                    try:
                        rest[0](*rest[1])
                    except Exception:  # noqa: BLE001
                        log.error("UI callback failed: %s", traceback.format_exc())
                    continue
                on_done, on_error, on_cancel = self._job_cbs
                self.job = None
                self.mascot.progress = 0
                if kind == "done":
                    try:
                        on_done(rest[0])
                    except Exception as e:  # noqa: BLE001
                        log.error("on_done failed: %s", traceback.format_exc())
                        self.mascot.set_mood("sad", "error")
                        self.toast(f"Error: {e}", "danger")
                elif kind == "cancelled":
                    self.mascot.set_mood("sad", "cancel")
                    (on_cancel or (lambda: None))()
                else:
                    self.mascot.set_mood("sad", "error")
                    if on_error:
                        on_error(rest[0])
                    else:
                        self.toast(f"{type(rest[0]).__name__}: {rest[0]}", "danger")
        except queue.Empty:
            pass
        if self.job is not None and self.job_panel is not None:
            s = self.job.snapshot()
            try:
                self.job_panel.update_from(s)
            except tk.TclError:
                pass
            self.mascot.progress = s["pct"] / 100
        self.after(120, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._quit)

    # ------------------------------------------------------------------ drag & drop
    def _drop_enter(self, e):
        for h in self.drop_handlers:
            h("enter", None)
        return e.action

    def _drop_leave(self, e):
        for h in self.drop_handlers:
            h("leave", None)
        return e.action

    def _drop(self, e):
        paths = list(self.tk.splitlist(e.data))
        for h in self.drop_handlers:
            h("leave", None)
        self.handle_drop(paths)
        return e.action

    def handle_drop(self, paths):
        imgs = [p for p in paths if os.path.isfile(p) and p.lower().endswith(IMAGE_EXTS)]
        others = [p for p in paths if p not in imgs]
        for p in imgs:
            self.add_image(p)
        if imgs and hasattr(self.screen, "refresh_drives"):
            self.screen.refresh_drives()
        if others:
            if hasattr(self.screen, "accept_files"):
                self.screen.accept_files(others)
            else:
                from .screens import ShredWizard
                self.mascot.say("drop")
                scr = self.go(ShredWizard)
                scr.accept_files(others)


def run():
    win = MainWindow()
    win.mainloop()
