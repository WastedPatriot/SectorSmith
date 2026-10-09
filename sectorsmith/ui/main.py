"""SectorSmith main window: category rail, sub-nav, context bar, animated screen stack, job runner, command palette,
presentation mode and drag-and-drop."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .. import partitions
from ..device import list_disks, open_image, volume_letters_for
from ..jobs import JobStore
from ..util import APP_NAME, Cancelled, Progress, cancel_scope, get_logger
from . import nav, theme
from .mascot import MascotHub
from .shell import ContextBar, Rail, SubNav, technician
from .widgets import Toast, install_keyboard_support

log = get_logger()
P = theme.PALETTE

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _DND = True
except Exception:  # noqa: BLE001  drag-and-drop is optional
    _DND = False
    TkinterDnD = None
    DND_FILES = None

_Base = (ctk.CTk, TkinterDnD.DnDWrapper) if _DND else (ctk.CTk,)
IMAGE_EXTS = (".img", ".vhd", ".dd", ".raw", ".bin", ".001", ".iso")
INSTALLER_EXTS = (".msi", ".exe", ".msix", ".msixbundle", ".appx", ".appxbundle")
TASK_NAMES = {"Wipe": "Erase and certify", "Copy": "Image and clone", "Clone": "Network clone",
              "Migration": "Migrate user", "Shred": "Shred files", "Quick scan": "Recover files (quick scan)",
              "Deep scan": "Recover files (deep scan)", "Recover": "Recover files",
              "Partition search": "Find lost partitions", "Deploy": "Run maintenance", "Deploy check": "Deploy check"}
SUBNAV_MIN_WIDTH = 1180


class ModeVar:
    """app.mode: the Light / Dark / System choice. Settings shows it as a segmented control."""

    def __init__(self, value="System"):
        self.value = value
        self.widgets = []

    def set(self, value):
        self.value = value
        for w in list(self.widgets):
            try:
                w.set(value)
            except tk.TclError:
                self.widgets.remove(w)

    def get(self):
        return self.value


class MainWindow(*_Base):
    def __init__(self):
        theme.enable_dpi_awareness()  # before Tk starts; CustomTkinter then scales each monitor itself
        theme.apply_ui_size()
        install_keyboard_support()
        super().__init__()
        if _DND:
            try:
                self.TkdndVersion = TkinterDnD._require(self)
            except Exception:  # noqa: BLE001
                globals()["_DND"] = False
        ctk.set_default_color_theme("blue")
        theme.apply_ctk_defaults()
        self.title(APP_NAME)
        self.geometry("1360x860")
        self.minsize(1120, 720)
        self.configure(fg_color=P["bg"])
        theme.style_ttk(self)
        theme.on_theme_change(lambda: theme.style_ttk(self))
        # tables are ttk, which CustomTkinter doesn't scale: restyle them when the monitor or interface size changes
        ctk.ScalingTracker.add_widget(lambda *_a: theme.style_ttk(self), self)

        from .guide import load_settings
        settings = load_settings()
        self.images = []
        self._inventory = None
        self._deploy_store = None
        from ..link.endpoint import LocalEndpoint
        self.local_ep = LocalEndpoint(label="This PC")
        self.link = None
        self.machine_listeners = []
        self.q: queue.Queue = queue.Queue()
        self.job: Progress | None = None
        self.job_panel = None
        self.job_record = None
        self.job_store = JobStore()
        self.jobs: list[dict] = self._load_jobs()  # history across sessions, newest last
        self.recent: list[str] = []         # palette entries opened, newest last
        self._job_screen = None
        self._job_title = ""
        self._quitting = None
        self.screen = None
        self._route = (None, {})
        self.drop_handlers = []
        self.mascot = MascotHub()
        self.mode = ModeVar(settings.get("mode") if settings.get("mode") in ("Light", "Dark", "System") else "System")
        self.context = {"client": settings.get("client") or None, "ticket": ""}
        self._subnav_user = True            # Ctrl+B
        self._cat = None

        self.grid_columnconfigure(2, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.rail = Rail(self, self)
        self.rail.grid(row=0, column=0, sticky="ns")
        self.subnav = SubNav(self, self)
        self.subnav.grid(row=0, column=1, sticky="ns")
        main = ctk.CTkFrame(self, fg_color=P["bg"], corner_radius=0)
        main.grid(row=0, column=2, sticky="nsew")
        self.bar = ContextBar(main, self)
        self.bar.pack(fill="x")
        self.stage = ctk.CTkFrame(main, fg_color=P["bg"], corner_radius=0)
        self.stage.pack(fill="both", expand=True)

        if _DND:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<DropEnter>>", self._drop_enter)
            self.dnd_bind("<<DropLeave>>", self._drop_leave)
            self.dnd_bind("<<Drop>>", self._drop)
        self._shortcuts()
        self.bind("<Configure>", self._resized, add="+")

        from .screens import Home
        theme.set_mode(self.mode.get().lower())
        if settings.get("welcomed"):
            self.go(Home, animate=False)
        else:
            from .guide import WelcomeScreen
            self.go(WelcomeScreen, animate=False)
        self._set_icon()
        self.after(120, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._quit)
        self.after(400, lambda: self.mascot.set_mood("idle", "hello"))

    # ------------------------------------------------------------------ shortcuts and layout
    def _shortcuts(self):
        self.bind("<Control-k>", lambda _e: self.open_palette())
        self.bind("<Control-K>", lambda _e: self.open_palette())
        self.bind("<slash>", self._slash)
        self.bind("<Control-P>", lambda _e: self.set_presentation(not theme.presentation()))
        self.bind("<Control-C>", lambda _e: self.client_menu())
        self.bind("<Control-b>", lambda _e: self.toggle_subnav())
        self.bind("<F5>", lambda _e: self._refresh())
        keys = [c.key for c in nav.CATEGORIES]
        for i, k in enumerate(keys[:7], 1):
            self.bind(f"<Control-Key-{i}>", lambda _e, k=k: self.open_category(k))

    def _slash(self, _e):
        if isinstance(self.focus_get(), (tk.Entry, tk.Text)):
            return None
        self.open_palette()
        return "break"

    def _resized(self, e):
        if e.widget is self:
            self._layout()

    def _layout(self):
        w = self.winfo_width()
        wide = w >= SUBNAV_MIN_WIDTH or w < 50  # under 50 means the window is not mapped yet
        show = self._cat not in (None, "home") and self._subnav_user and wide
        managed = bool(self.subnav.winfo_manager())  # ismapped is False until the window first shows
        if show and not managed:
            self.subnav.grid()
        elif not show and managed:
            self.subnav.grid_remove()

    def toggle_subnav(self):
        self._subnav_user = not self._subnav_user
        self._layout()

    def _refresh(self):
        if hasattr(self.screen, "refresh_drives"):
            self._inventory = None
            self.screen.refresh_drives()

    # ------------------------------------------------------------------ routes
    def open_category(self, key):
        if key == "home":
            if self._may_leave():
                self.home()
            return
        cat = nav.BY_KEY[key]
        self.open_item(cat, nav.first_item(cat))

    def open_target(self, cat_key, item_key):
        cat = nav.BY_KEY[cat_key]
        it = next(i for i in nav.items(cat) if i.key == item_key)
        self.open_item(cat, it)

    def open_item(self, cat, item):
        if item.target.startswith("app:"):
            getattr(self, item.target.split(":")[1])()
            return
        if not self._may_leave():
            return
        self.go(nav.resolve(item.target), **item.kw)

    def _may_leave(self):
        if self.job is None:
            return True
        self.toast("A job is still running. Cancel it first, or wait for it to finish.", "warn")
        return False

    def _sync_nav(self, screen_cls, kw):
        cat_key, item_key = nav.locate(screen_cls, kw)
        if cat_key is None:
            cat_key, item_key = self._cat, None
        self._cat = cat_key
        self.rail.select(cat_key)
        crumbs = [("Home", None)]
        if cat_key and cat_key != "home":
            cat = nav.BY_KEY[cat_key]
            self.subnav.show(cat_key, item_key)
            it = next((i for i in nav.items(cat) if i.key == item_key), None)
            crumbs = [(cat.title, lambda c=cat_key: self.open_category(c))]
            if it is not None:
                crumbs.append((it.label, None))
            elif getattr(self.screen, "title_lbl", None) is not None:
                crumbs.append((self.screen.title_lbl.cget("text"), None))
        self.bar.set_crumbs(crumbs)
        self._layout()

    def open_palette(self):
        from .palette import CommandPalette
        CommandPalette(self)

    def client_menu(self):
        self.bar.client_menu()

    def open_help(self):
        topic = getattr(self.screen, "guide_topic", None) or "start"
        if self._may_leave():
            self.open_guide(topic)

    # ------------------------------------------------------------------ context: client, ticket, presentation
    def client_names(self):
        try:
            return sorted((c.name for c in self.deploy_store().clients), key=str.lower)
        except Exception:  # noqa: BLE001  no Deploy library yet
            return []

    def set_client(self, name):
        from .guide import save_settings
        if name != self.context.get("client"):
            self.context["ticket"] = self._tickets().get(name or "", "")
        self.context["client"] = name
        save_settings(client=name or "")
        self.bar.update_context()
        self._refresh_screen()

    def _tickets(self):
        if not hasattr(self, "_ticket_by_client"):
            self._ticket_by_client = {}
        return self._ticket_by_client

    def set_ticket(self, ticket):
        self.context["ticket"] = ticket
        self._tickets()[self.context.get("client") or ""] = ticket
        self.bar.update_context()
        self._refresh_screen()

    def set_presentation(self, on):
        theme.set_presentation(on)
        self.bar.update_presenting()
        self.subnav.refresh_bottom()
        self._refresh_screen()
        self.toast("Presenting. Mossbit is hidden, other clients and serial numbers are masked." if on
                   else "Stopped presenting.", "info" if on else "success")

    def set_personality(self, value):
        theme.set_personality(value)
        self.subnav.refresh_bottom()
        self._refresh_screen()
        self.toast(f"Mossbit personality set to {value}.")

    def set_appearance(self, value):
        from .guide import save_settings
        save_settings(mode=value)
        self.mode.set(value)
        theme.set_mode(value.lower())
        self.mascot.say(text="Ooh, cosy dark mode!" if value == "Dark" else
                        "Nice and bright!" if value == "Light" else "I'll match your Windows setting.")

    def _set_mode(self, value):  # older name
        self.set_appearance(value)

    def set_reduce_motion(self, on):
        theme.set_reduce_motion(on)
        self.subnav.refresh_bottom()
        self.toast("Motion reduced. Screens switch without sliding and Mossbit stays still." if on
                   else "Animations are back on.")

    def set_interface_size(self, percent):
        from .guide import save_settings
        if percent not in theme.UI_SIZES:
            return
        save_settings(ui_size=percent)
        theme.apply_ui_size(percent)
        theme.style_ttk(self)
        self._refresh_screen()
        self.toast(f"Interface size set to {percent}%.")

    def _refresh_screen(self):
        """Rebuild the current screen if it only shows state (Home, lists, Settings); wizards keep their place."""
        cls, kw = self._route
        if cls is not None and getattr(self.screen, "refreshable", False):
            self.go(cls, animate=False, **kw)

    def restart_as_admin(self):
        from ..util import relaunch_as_admin
        if relaunch_as_admin():
            self._quit()

    def show_job_screen(self):
        pass  # the running job's screen is always the current one: leaving is blocked while it runs

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
        self.subnav.refresh_bottom()
        if hasattr(self.screen, "machines_changed"):
            self.screen.machines_changed()
        for fn in list(self.machine_listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001
                self.machine_listeners.remove(fn)

    def open_connect(self):
        from .link_screens import ConnectScreen
        if self._may_leave():
            self.go(ConnectScreen)

    def deploy_store(self):
        """The Deploy library, loaded on first use. Raises if its files can't be read."""
        if self._deploy_store is None:
            from ..deploy.core import Store
            self._deploy_store = Store()
        return self._deploy_store

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
        if self.job is not None and self._quitting is None:
            if not messagebox.askyesno("Stop and quit?", "A task is still running. Stop it safely and quit?"):
                return
            self._quitting = time.monotonic()
            self.cancel_job()
            self._quit_when_stopped()
            return
        try:
            if self.link:
                self.link.stop()
        finally:
            self.destroy()

    def _quit_when_stopped(self):
        # give the job a moment to close disks and remove snapshots before the process ends
        if self.job is None or time.monotonic() - self._quitting > 30:
            self.job = None
            self._quit()
        else:
            self.after(200, self._quit_when_stopped)

    def _set_icon(self):
        """The product mark (tools/make_icon.py), never Mossbit: it shows in the taskbar and Alt+Tab."""
        from .mascot import ASSETS
        try:
            self._icons = [tk.PhotoImage(file=os.path.join(ASSETS, f"mark_{s}.png")) for s in (256, 64, 48, 32, 16)]
            self.iconphoto(True, *self._icons)
            if sys.platform == "win32":  # CustomTkinter resets the icon shortly after start
                self.after(300, lambda: self.iconbitmap(os.path.join(ASSETS, "mark.ico")))
        except tk.TclError:
            pass

    def open_guide(self, topic="start"):
        from .guide import GuideScreen
        self.go(GuideScreen, topic=topic)

    # ------------------------------------------------------------------ navigation
    def go(self, screen_cls, animate=True, **kw):
        for ov in list(getattr(self, "_smith_overlays", [])):
            ov.close()
        old = self.screen
        self.drop_handlers = []
        new = screen_cls(self.stage, self, **kw)
        self.screen = new
        self._route = (screen_cls, kw)
        self._sync_nav(screen_cls, kw)
        if not animate or old is None or theme.reduced_motion():
            new.place(relx=0, rely=0, relwidth=1, relheight=1)
            if old is not None:
                old.destroy()
            return new
        new.place(relx=1, rely=0, relwidth=1, relheight=1)
        start = time.monotonic()

        def step():
            t = min(1.0, (time.monotonic() - start) / .26)
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
            self.toast("Another job is already running. Wait for it to finish first.", "warn")
            return
        prog = Progress(1, title)
        self.job = prog
        self.job_panel = panel
        self._job_cbs = (on_done, on_error, on_cancel)
        detail = ""
        try:
            detail = self.screen.target_name() if hasattr(self.screen, "target_name") else ""
        except Exception:  # noqa: BLE001  the screen may not have a target yet
            detail = ""
        # every job is stamped with client, ticket and technician, and written to the history when it starts
        self.job_record = self.job_store.start(task=TASK_NAMES.get(title, title), title=title, started=time.time(),
                                               client=self.context.get("client") or "",
                                               ticket=self.context.get("ticket") or "", technician=technician(),
                                               machine="This PC", detail=detail)
        self.jobs.append(self.job_record)
        del self.jobs[:-self.job_store.cap]
        self._job_screen = self.screen
        self._job_title = title
        self.mascot.set_mood("working", "working")
        log.info("UI job start: %s", title)

        def worker():
            try:
                # everything this thread calls (disk I/O, Link calls, installers, VSS) checks Cancel
                with cancel_scope(prog.check):
                    r = func(prog)
                self.q.put(("done", r))
            except Cancelled as c:
                log.info("UI job %s cancelled: %s", title, {k: v for k, v in c.info.items() if k != "failed"})
                self.q.put(("cancelled", dict(c.info, progress=prog.snapshot())))
            except Exception as e:  # noqa: BLE001
                log.error("UI job %s failed: %s", title, traceback.format_exc())
                self.q.put(("error", e))
        threading.Thread(target=worker, daemon=True, name=f"job-{title}").start()

    def cancel_job(self):
        """Ask the running job to stop. It stops at its next safe point; the panel shows Cancelling... until then."""
        if self.job and not self.job.cancelled:
            log.info("UI job %s: cancel requested", self._job_title)
            self.job.cancel()
            try:
                if self.job_panel is not None:
                    self.job_panel.set_cancelling()
            except tk.TclError:
                pass  # panel already gone; the cancel still goes through
            self.mascot.say(text="Stopping safely, one moment...")

    def _load_jobs(self):
        try:
            return self.job_store.load()
        except Exception as e:  # noqa: BLE001  a bad history file must not stop the app
            log.warning("job history not readable: %s", e)
            return []

    def _finish_record(self, result, value=None):
        """Write the job's end to the history. A result that names a report file (migration) is kept with it."""
        if self.job_record is not None:
            report = value.get("report") if isinstance(value, dict) else None
            self.job_store.finish(self.job_record, result, report=report if isinstance(report, str) else None)
            self.job_record = None

    def attach_job_file(self, path, job_id=None):
        """Remember a report or certificate a job produced after it ended (Save certificate). job_id: the 'id' of
        the job record; the latest job when not given."""
        if job_id:
            rec = next((j for j in reversed(self.jobs) if j.get("id") == job_id), None)
        else:
            rec = self.jobs[-1] if self.jobs else None
        if rec is not None and path:
            self.job_store.attach(rec, os.path.abspath(path))

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
                self._job_finished(kind, rest[0])
        except queue.Empty:
            pass
        finally:
            self.after(120, self._poll)  # always, or the UI stops hearing from jobs and background tasks
        if self.job is not None and self.job_panel is not None:
            s = self.job.snapshot()
            try:
                self.job_panel.update_from(s)
            except tk.TclError:
                pass
            self.mascot.progress = s["pct"] / 100
        if self.job is not None:
            self.bar.update_jobs(self.job)
        self.protocol("WM_DELETE_WINDOW", self._quit)

    def _job_finished(self, kind, value):
        on_done, on_error, on_cancel = self._job_cbs
        title = self._job_title
        self.job = None
        self.job_panel = None
        self.mascot.progress = 0
        self._finish_record({"done": "Done", "cancelled": "Cancelled"}.get(kind, "Failed"), value)
        self.bar.update_jobs(None)
        scr = self._job_screen
        # the screen that started the job may be gone (Home, guide, Connect): don't draw on a dead widget
        here = scr is not None and scr is self.screen and scr.winfo_exists()
        try:
            if kind == "done":
                if here:
                    on_done(value)
                else:
                    self.toast(f"{title} finished")
            elif kind == "cancelled":
                self.mascot.set_mood("sad", "cancel")
                if not here:
                    self.toast(f"{title} stopped", "warn")
                elif on_cancel:
                    on_cancel(value)
                else:
                    scr.cancelled_state(f"{title} stopped", ["It stopped before finishing."])
            else:
                self.mascot.set_mood("sad", "error")
                if here and on_error:
                    on_error(value)
                else:
                    self.toast(f"{type(value).__name__}: {value}", "danger")
        except Exception as e:  # noqa: BLE001  (a broken result screen must not stop the UI loop)
            log.error("Job %s %s handler failed: %s", title, kind, traceback.format_exc())
            self.mascot.set_mood("sad", "error")
            self.toast(f"Error: {e}", "danger")

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
            elif all(p.lower().endswith(INSTALLER_EXTS) for p in others):
                # installers dropped anywhere else go to the package builder, not the shredder
                from .deploy_screens import PackageBuilder
                self.go(PackageBuilder, path=others[0])
            else:
                from .screens import ShredWizard
                self.mascot.say("drop")
                scr = self.go(ShredWizard)
                scr.accept_files(others)


def run():
    theme.enable_dpi_awareness()
    win = MainWindow()
    win.mainloop()
