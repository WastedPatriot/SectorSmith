"""Home screen and the step-by-step task wizards."""
from __future__ import annotations

import datetime as _dt
import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from .. import carver, ntfs, partitions, partscan, surface, wipe
from ..report import wipe_certificate
from ..util import get_logger, human_size, human_time
from . import theme
from .widgets import DrivePicker, DropZone, IconBadge, OptionCard, Pill, ProgressPanel, TaskCard, ghost_button, \
    primary_button, selected_value

log = get_logger()
P = theme.PALETTE

STRENGTHS = [
    ("Quick", "Zero fill (1 pass)", "One pass of zeros. Fast — fine when the drive stays in the business."),
    ("Recommended", "NIST 800-88 Clear (1 pass + verify)", "The modern standard, with read-back verification."),
    ("Thorough", "DoD 5220.22-M (3 pass + verify)", "Three passes plus verification. Takes about 4x longer."),
]

CATS = {
    "Photos": {"jpg", "jpeg", "png", "gif", "bmp", "webp", "heic", "tif", "tiff", "cr2", "nef", "arw", "dng", "psd",
               "raw", "svg"},
    "Documents": {"doc", "docx", "xls", "xlsx", "ppt", "pptx", "pdf", "txt", "rtf", "odt", "ods", "odp", "csv",
                  "msg", "pst", "ost", "eml", "dwg", "dxf", "rvt", "pln", "indd", "ai", "vsdx", "one"},
    "Video & audio": {"mp4", "mov", "avi", "mkv", "wmv", "m4v", "m4a", "mp3", "wav", "flac", "3gp", "aac", "wma"},
    "Archives": {"zip", "7z", "rar", "iso", "tar", "gz", "cab", "sqlite", "db"},
}


def category(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    for k, v in CATS.items():
        if ext in v:
            return k
    return "Other"


def greeting():
    h = _dt.datetime.now().hour
    who = (os.environ.get("USERNAME") or os.environ.get("USER") or "").split(".")[0].capitalize()
    part = "Good morning" if h < 12 else "Good afternoon" if h < 18 else "Good evening"
    return f"{part}{', ' + who if who and who.lower() not in ('root', 'admin', 'administrator') else ''}"


# ---------------------------------------------------------------------------
class Screen(ctk.CTkFrame):
    guide_topic = None

    def __init__(self, master, app, title="", subtitle="", steps=None, show_back=True, badge=None):
        super().__init__(master, fg_color=P["bg"], corner_radius=0)
        self.app = app
        self.steps = steps or []
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=44, pady=(30, 0))
        if show_back:
            nav = ctk.CTkFrame(head, fg_color="transparent")
            nav.pack(fill="x", pady=(0, 10))
            ctk.CTkButton(nav, text="←  Home", width=96, height=32, corner_radius=16, fg_color="transparent",
                          hover_color=P["card_hover"], text_color=P["muted"], font=theme.font(13, "bold"),
                          command=self.go_home).pack(side="left")
            if self.guide_topic:
                ctk.CTkButton(nav, text="?  How this works", width=150, height=32, corner_radius=16,
                              fg_color=P["violet_soft"], hover_color=P["card_hover"], text_color=P["violet"],
                              font=theme.font(12, "bold"), command=self._open_guide).pack(side="right")
        row = ctk.CTkFrame(head, fg_color="transparent")
        row.pack(fill="x")
        titles = ctk.CTkFrame(row, fg_color="transparent")
        titles.pack(side="left", fill="x", expand=True, anchor="n")
        trow = ctk.CTkFrame(titles, fg_color="transparent")
        trow.pack(anchor="w")
        self.title_lbl = ctk.CTkLabel(trow, text=title, font=theme.font(30, "bold"), text_color=P["text"],
                                      anchor="w")
        self.title_lbl.pack(side="left")
        if badge:  # e.g. PREVIEW on features that aren't finished yet
            Pill(trow, badge, "warn").pack(side="left", padx=(12, 0), pady=(6, 0))
        self.sub_lbl = ctk.CTkLabel(titles, text=subtitle, font=theme.font(14), text_color=P["muted"], anchor="w",
                                    justify="left", wraplength=640)
        self.sub_lbl.pack(anchor="w", pady=(2, 0))
        self.stepper = ctk.CTkFrame(row, fg_color="transparent", height=1, width=1)
        self.stepper.pack(side="right", anchor="n", pady=8)
        self.holder = ctk.CTkFrame(self, fg_color="transparent")
        self.holder.pack(fill="both", expand=True, padx=44, pady=(18, 0))
        self.footer = ctk.CTkFrame(self, fg_color="transparent", height=76)
        self.footer.pack(fill="x", padx=44, pady=(8, 24))
        self.body = None
        self._draw_steps(0)

    def _open_guide(self):
        if self.app.job is not None:
            self.app.toast("Finish or cancel the current task first.", "warn")
            return
        from .guide import GuideScreen
        self.app.go(GuideScreen, topic=self.guide_topic)

    def go_home(self):
        if self.app.job is not None:
            if not messagebox.askyesno("Stop?", "A task is still running. Cancel it and go home?"):
                return
            self.app.cancel_job()
            self.app.toast("Stopping it safely in the background...", "warn")
        self.app.home()

    def cancelled_state(self, headline, lines, step=None, primary=None, secondary=None, extra=None):
        """Clear result after Cancel: what stopped and what state things were left in."""
        body = self.step(step if step is not None else max(0, len(self.steps) - 1), "Stopped", "") \
            if self.steps else self.new_body()
        if not self.steps:
            self.title_lbl.configure(text="Stopped")
            self.sub_lbl.configure(text="")
        self.result_card(body, "warn", "warn", headline, lines)
        self.buttons(primary=primary or ("Back to home", self.app.home), secondary=secondary, extra=extra)
        return body

    def _draw_steps(self, cur):
        for w in self.stepper.winfo_children():
            w.destroy()
        for i, name in enumerate(self.steps):
            done, now = i < cur, i == cur
            fg = P["success"] if done else P["accent"] if now else P["track"]
            ctk.CTkLabel(self.stepper, text="✓" if done else str(i + 1), width=26, height=26, corner_radius=13,
                         fg_color=fg, text_color="#ffffff" if (done or now) else P["muted"],
                         font=theme.font(12, "bold")).pack(side="left", padx=(8 if i else 0, 4))
            ctk.CTkLabel(self.stepper, text=name, font=theme.font(12, "bold" if now else "normal"),
                         text_color=P["text"] if now else P["muted"]).pack(side="left")

    def step(self, i, title=None, subtitle=None):
        self._draw_steps(i)
        if title is not None:
            self.title_lbl.configure(text=title)
        if subtitle is not None:
            self.sub_lbl.configure(text=subtitle)
        return self.new_body()

    def new_body(self):
        old = self.body
        body = ctk.CTkFrame(self.holder, fg_color="transparent")
        body.place(relx=0, rely=0, relwidth=1, relheight=1, y=26)
        self.body = body
        if old is not None:
            old.destroy()
        start = time.monotonic()

        def rise():
            t = min(1.0, (time.monotonic() - start) / .25)
            try:
                body.place_configure(y=26 * (1 - t) ** 3)
            except tk.TclError:
                return
            if t < 1:
                self.after(12, rise)
        rise()
        for w in self.footer.winfo_children():
            w.destroy()
        return body

    def buttons(self, primary=None, secondary=None, extra=None):
        """primary/secondary: (text, command). Returns primary button."""
        pb = None
        if primary:
            pb = primary_button(self.footer, primary[0], primary[1], width=210)
            pb.pack(side="right")
        if secondary:
            ghost_button(self.footer, secondary[0], secondary[1], width=140).pack(side="right", padx=10)
        if extra:
            ghost_button(self.footer, extra[0], extra[1], width=180).pack(side="left")
        return pb

    def progress(self, title, subtitle):
        body = self.new_body()
        self.title_lbl.configure(text=title)
        self.sub_lbl.configure(text=subtitle)
        panel = ProgressPanel(body, self.app.cancel_job)
        panel.pack(fill="x", pady=(10, 0))
        return body, panel

    def note(self, parent, text, tone="muted"):
        return ctk.CTkLabel(parent, text=text, font=theme.font(12), text_color=P[tone], wraplength=780,
                            justify="left", anchor="w")

    def result_card(self, parent, icon, tone, headline, lines):
        card = ctk.CTkFrame(parent, corner_radius=24, fg_color=P["card"])
        card.pack(fill="x", pady=(6, 10))
        b = IconBadge(card, icon, 64, tone)
        b.set_bg(theme.c("card"))
        b.grid(row=0, column=0, rowspan=2, padx=24, pady=24, sticky="n")
        ctk.CTkLabel(card, text=headline, font=theme.font(22, "bold"), text_color=P["text"], anchor="w").grid(
            row=0, column=1, sticky="w", pady=(26, 2))
        ctk.CTkLabel(card, text="\n".join(lines), font=theme.font(13), text_color=P["muted"], justify="left",
                     anchor="w").grid(row=1, column=1, sticky="w", pady=(0, 24))
        card.grid_columnconfigure(1, weight=1)
        return card

    def confirm_box(self, parent, phrase, on_change):
        box = ctk.CTkFrame(parent, corner_radius=20, fg_color=P["danger_soft"])
        box.pack(fill="x", pady=10)
        ctk.CTkLabel(box, text=f"Type  {phrase}  to confirm", font=theme.font(15, "bold"),
                     text_color=P["danger"]).pack(anchor="w", padx=22, pady=(18, 6))
        var = tk.StringVar()
        e = ctk.CTkEntry(box, textvariable=var, height=46, corner_radius=14, font=theme.font(16, "bold"),
                         placeholder_text=phrase, border_color=P["danger"], fg_color=P["card"])
        e.pack(fill="x", padx=22, pady=(0, 20))
        var.trace_add("write", lambda *_: on_change(var.get().strip().upper() == phrase.upper()))
        self.after(100, e.focus_set)
        return var

    def done_actions(self, folder=None):
        self.buttons(primary=("Back to home", self.app.home),
                     secondary=("Open folder", lambda: self.app.open_folder(folder)) if folder else None)


# ---------------------------------------------------------------------------
class Home(Screen):
    def __init__(self, master, app):
        super().__init__(master, app, title=greeting(), subtitle="What are we doing today?", show_back=False)
        body = self.new_body()
        grid = ctk.CTkFrame(body, fg_color="transparent")
        grid.pack(fill="x")
        cards = [
            ("recover", "Recover files", "Bring back deleted or lost files — names and folders included.",
             RecoverWizard, "accent"),
            ("partition", "Find lost partitions", "A drive shows as empty or RAW? Find and restore its partitions.",
             PartitionWizard, "violet"),
            ("wipe", "Wipe a drive", "Securely erase a disk or partition before reuse or disposal.", WipeWizard,
             "danger"),
            ("shred", "Shred files", "Permanently destroy specific files, or clean a drive's free space.",
             ShredWizard, "warn"),
            ("health", "Check drive health", "Test every sector and get a plain-English verdict.", HealthWizard,
             "success"),
            ("clone", "Back up / clone", "Copy a whole drive to an image file or to another drive.", CloneWizard,
             "violet"),
        ]
        from .link_screens import ConnectScreen, MigrateWizard
        cards += [
            ("migrate", "Move a user", "Copy a user's files, browser data and more to a new PC.",
             MigrateWizard, "success"),
            ("link", "Connect a machine", "Link another PC with one copy-paste command to work on both.",
             ConnectScreen, "accent"),
        ]
        from .deploy_screens import DeployScreen
        cards.append(("deploy", "Deploy software", "Install apps and run upkeep tasks on linked PCs. (Preview)",
                      DeployScreen, "violet"))
        for i, (ic, t, d, cls, tone) in enumerate(cards):
            card = TaskCard(grid, ic, t, d, lambda c=cls: self.app.go(c), tone=tone)
            card.grid(row=i // 3, column=i % 3, sticky="nsew", padx=8, pady=8)
        for c in range(3):
            grid.grid_columnconfigure(c, weight=1, uniform="c")
        self.dz = DropZone(grid, "Drop files to shred", "Disk images (.img / .vhd) open as drives, and installers "
                                                        "become Deploy packages.", height=96, wide=True)
        self.dz.grid(row=3, column=0, columnspan=3, sticky="nsew", padx=8, pady=8)
        app.drop_handlers.append(lambda kind, _p: self.dz.hot(kind == "enter"))
        hint = ctk.CTkFrame(body, fg_color="transparent")
        hint.pack(fill="x", padx=8, pady=(14, 0))
        ctk.CTkLabel(hint, text="New here?", font=theme.font(13, "bold"), text_color=P["text"]).pack(side="left")
        ctk.CTkButton(hint, text="Read the 2-minute guide →", fg_color="transparent", hover_color=P["card_hover"],
                      text_color=P["accent"], font=theme.font(13, "bold"), width=200, height=30,
                      command=lambda: self.app.open_guide()).pack(side="left")


# ---------------------------------------------------------------------------
class _PickMixin:
    def pick_step(self, idx, title, subtitle, mode="any", writes=False, exclude=None, next_text="Next"):
        body = self.step(idx, title, subtitle)
        self.app.mascot.set_mood("idle", "pick")
        self.target = None
        top = ctk.CTkFrame(body, fg_color="transparent")
        top.pack(fill="x")
        ctk.CTkButton(top, text="↻  Refresh", width=100, height=30, corner_radius=15, fg_color="transparent",
                      hover_color=P["card_hover"], text_color=P["muted"], font=theme.font(12, "bold"),
                      command=lambda: self._refresh_pick(body, mode, writes, exclude)).pack(side="right")
        self._pick_args = (body, mode, writes, exclude)
        self.picker = None
        self._build_picker(body, mode, writes, exclude)
        self.next_btn = self.buttons(primary=(next_text, self._after_pick))
        self.next_btn.configure(state="disabled")
        return body

    def _build_picker(self, body, mode, writes, exclude, refresh=False):
        if self.picker is not None:
            self.picker.destroy()

        def chosen(t):
            self.target = t
            self.next_btn.configure(state="normal")
        self.picker = DrivePicker(body, self.app.inventory(refresh), mode=mode, on_select=chosen, writes=writes,
                                  exclude_path=exclude, height=470)
        self.picker.pack(fill="both", expand=True, pady=(6, 0))

    def _refresh_pick(self, body, mode, writes, exclude):
        self.target = None
        self.next_btn.configure(state="disabled")
        self._build_picker(body, mode, writes, exclude, refresh=True)

    def refresh_drives(self):
        if getattr(self, "picker", None) is not None and self.picker.winfo_exists():
            self._refresh_pick(*self._pick_args)

    def target_range(self):
        t = self.target
        if t["kind"] == "part":
            return t["part"].start_lba, t["part"].sectors
        return 0, t["dev"].total_sectors

    def target_name(self):
        t = self.target
        d = t["dev"]
        if t["kind"] == "part":
            p = t["part"]
            letters = ", ".join(m.rstrip("\\") for m in p.mount)
            return f"{letters + ' ' if letters else ''}{p.label or p.name or p.fs} (partition #{p.index} on {d.name})"
        return f"{d.name} · {d.model or 'Disk'} · {human_size(d.size)}"


# ---------------------------------------------------------------------------
class RecoverWizard(Screen, _PickMixin):
    guide_topic = "recover"

    def __init__(self, master, app):
        super().__init__(master, app, "Recover files", steps=["Drive", "Scan type", "Scan", "Recover"])
        self.pick_step(0, "Recover files", "Where did the files live? Pick the partition (or whole drive).")

    def _after_pick(self):
        t = self.target
        body = self.step(1, "How deep should I look?", f"Scanning {self.target_name()}")
        grp = []
        ntfs_ok = t["kind"] == "part" and t["part"].fs == "NTFS"
        quick = OptionCard(body, "Quick scan", "Reads the drive's file table — recovers deleted files with their "
                                               "original names, folders and dates. Usually takes seconds.",
                           "mft", grp, icon="recover", badge_text="Best first try")
        quick.pack(fill="x", pady=6)
        deep = OptionCard(body, "Deep scan", "Searches every sector for photos, documents, videos and more. Works "
                                             "on formatted or RAW drives. Original file names aren't kept.",
                          "deep", grp, icon="health", tone="violet")
        deep.pack(fill="x", pady=6)
        if not ntfs_ok:
            quick.set_enabled(False, "Quick scan needs an NTFS partition. Pick one on the previous step, or use "
                                     "Deep scan.")
        opts = ctk.CTkFrame(body, fg_color="transparent")
        opts.pack(fill="x", pady=(10, 0))
        self.types = {}
        row = ctk.CTkFrame(opts, fg_color="transparent")
        ctk.CTkLabel(row, text="Look for:", font=theme.font(13, "bold"), text_color=P["text"]).pack(side="left")
        for grp_name in dict.fromkeys(s.group for s in carver.SIGNATURES):
            v = tk.BooleanVar(value=True)
            self.types[grp_name] = v
            ctk.CTkCheckBox(row, text=grp_name, variable=v, font=theme.font(13), fg_color=P["accent"],
                            hover_color=P["accent_hover"], corner_radius=6).pack(side="left", padx=10)
        self.out_var = tk.StringVar()
        orow = ctk.CTkFrame(opts, fg_color="transparent")
        ctk.CTkLabel(orow, text="Save found files to:", font=theme.font(13, "bold"), text_color=P["text"]).pack(
            side="left")
        ctk.CTkEntry(orow, textvariable=self.out_var, width=380, height=36, corner_radius=12,
                     placeholder_text="Choose a folder on a different drive").pack(side="left", padx=10)
        ctk.CTkButton(orow, text="Browse…", width=90, height=36, corner_radius=12, fg_color=P["violet"],
                      command=lambda: self.out_var.set(filedialog.askdirectory() or self.out_var.get())).pack(
            side="left")

        def chosen(v):
            if v == "deep":
                row.pack(fill="x", pady=4)
                orow.pack(fill="x", pady=8)
            else:
                row.pack_forget()
                orow.pack_forget()
            go.configure(state="normal")
        quick.on_select = deep.on_select = chosen
        self._grp = grp
        go = self.buttons(primary=("Start scan", self._start), secondary=("Back", lambda: self.pick_step(
            0, "Recover files", "Where did the files live?")))
        go.configure(state="disabled")
        (quick if ntfs_ok else deep).select()

    def _start(self):
        how = selected_value(self._grp)
        dev = self.app.clone(self.target["dev"])
        start, count = self.target_range()
        if how == "mft":
            _, panel = self.progress("Reading the file table…", "Looking for deleted files with their names.")
            self._draw_steps(2)

            def job(prog):
                vol = ntfs.NTFSVolume(dev, start * dev.sector_size)
                return vol, vol.scan(prog)
            self.app.run_job("Quick scan", job, self._mft_results, panel, on_error=self._error,
                             on_cancel=lambda info: self.cancelled_state(
                                 "Scan stopped", ["Nothing on the drive was changed."], step=2,
                                 secondary=("Scan again", self._start)))
        else:
            out = self.out_var.get().strip()
            if not out:
                self.app.toast("Choose where to save the files first.", "warn")
                return
            if not self._check_dest(out):
                return
            sigs = [s for s in carver.SIGNATURES if self.types[s.group].get()]
            _, panel = self.progress("Deep scanning…", "Searching every sector for files.")
            self._draw_steps(2)
            self._found_count = 0

            def on_file(name, ln, ext, lba):
                self._found_count += 1

            def job(prog):
                return carver.carve(dev, out, prog, sigs=sigs, start_lba=start, end_lba=start + count,
                                    on_file=on_file)
            self.app.run_job("Deep scan", job, lambda r: self._deep_done(r, out), panel, on_error=self._error,
                             on_cancel=lambda info: self._deep_done({"files": self._found_count, "by_type": {},
                                                                     "out_dir": out}, out, cancelled=True))

    def _check_dest(self, out):
        t = self.target
        mounts = []
        if t["kind"] == "part":
            mounts = t["part"].mount
        else:
            inv = {d.path: pt for d, pt, _e in self.app.inventory()}
            pt = inv.get(t["dev"].path)
            mounts = [m for p in (pt.partitions if pt else []) for m in p.mount]
        same = any(os.path.abspath(out).lower().startswith(m.lower()) for m in mounts)
        if same:
            return messagebox.askyesno("Same drive", "You're saving to the drive you're recovering from. That can "
                                                     "overwrite the very files you want back.\n\nContinue anyway?")
        return True

    def _error(self, e):
        body = self.step(2, "That didn't work", "")
        self.result_card(body, "warn", "danger", "Scan failed", [str(e), "", "Details are in the log file."])
        self.buttons(primary=("Back to home", self.app.home))

    def _mft_results(self, res):
        self.vol, entries = res
        self.entries = [e for e in entries if not e.is_dir and not e.path.startswith("\\$Extend")]
        deleted = [e for e in self.entries if e.deleted]
        if deleted:
            body = self.step(3, f"Found {len(deleted):,} deleted file{'s' if len(deleted) != 1 else ''}",
                             "Select the files you want back, then press Recover.")
        else:
            body = self.step(3, "No deleted files found", "Showing every file on the partition instead — you can "
                                                         "still copy any of them out. Try a Deep scan for more.")
        self.app.mascot.set_mood("happy" if deleted else "idle", "found" if deleted else "none")
        bar = ctk.CTkFrame(body, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 8))
        ent = ctk.CTkEntry(bar, width=260, height=36, corner_radius=18, placeholder_text="Search names or folders…")
        ent.pack(side="left")
        self.filter = ent
        ent.bind("<KeyRelease>", lambda _e: self._fill())
        self.cat = ctk.CTkSegmentedButton(bar, values=["All", "Photos", "Documents", "Video & audio", "Other"],
                                          command=lambda _v: self._fill(), selected_color=P["accent"],
                                          selected_hover_color=P["accent_hover"], font=theme.font(12, "bold"),
                                          height=34, corner_radius=17)
        self.cat.set("All")
        self.cat.pack(side="left", padx=12)
        self.only_del = tk.BooleanVar(value=bool(deleted))
        ctk.CTkSwitch(bar, text="Deleted only", variable=self.only_del, command=self._fill,
                      progress_color=P["accent"], font=theme.font(12)).pack(side="left")
        self.count_lbl = ctk.CTkLabel(bar, text="", font=theme.font(12), text_color=P["muted"])
        self.count_lbl.pack(side="right")
        wrap = ctk.CTkFrame(body, corner_radius=18, fg_color=P["card"])
        wrap.pack(fill="both", expand=True)
        cols = ("name", "folder", "size", "modified", "state")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", style="Smith.Treeview",
                                 selectmode="extended")
        for c, w, t in zip(cols, (240, 330, 90, 140, 150), ("Name", "Folder", "Size", "Modified", "Chance")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview, style="Smith.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        sb.pack(side="right", fill="y", pady=10, padx=4)
        self._fill()
        self.buttons(primary=("Recover selected", lambda: self._recover(False)),
                     secondary=("Recover all shown", lambda: self._recover(True)),
                     extra=("Select all", lambda: self.tree.selection_set(self.tree.get_children())))

    def _fill(self):
        t = self.tree
        t.delete(*t.get_children())
        flt = self.filter.get().lower()
        cat = self.cat.get()
        self._idx = {}
        n = 0
        for e in self.entries:
            if self.only_del.get() and not e.deleted:
                continue
            if cat != "All":
                ce = category(e.name)
                if (cat == "Other" and ce not in ("Other", "Archives")) or (cat != "Other" and ce != cat):
                    continue
            if flt and flt not in e.name.lower() and flt not in e.path.lower():
                continue
            n += 1
            if n > 50000:
                break
            iid = str(e.recno)
            self._idx[iid] = e
            chance = {"Good": "Excellent", "Possible": "Good"}.get(e.recoverability, e.recoverability)
            t.insert("", "end", iid=iid, values=(e.name, e.path, human_size(e.size),
                                                 e.mtime.strftime("%d %b %Y %H:%M") if e.mtime else "", chance))
        self.count_lbl.configure(text=f"{n:,} shown")

    def _recover(self, all_shown):
        ids = self.tree.get_children() if all_shown else self.tree.selection()
        ents = [self._idx[i] for i in ids if i in self._idx]
        if not ents:
            self.app.toast("Select some files first.", "warn")
            return
        out = filedialog.askdirectory(title="Save recovered files to (pick a different drive)")
        if not out or not self._check_dest(out):
            return
        vol = self.vol
        _, panel = self.progress("Recovering files…", f"Saving {len(ents):,} file(s) to {out}")

        def job(prog):
            return ntfs.recover_entries(vol, ents, out, prog)

        def done(res):
            body = self.step(3, "Your files are back", "")
            self.app.mascot.set_mood("happy", "found")
            lines = [f"Saved to {out}"]
            if res["failed"]:
                lines.append(f"{len(res['failed'])} couldn't be recovered (e.g. {res['failed'][0]})")
            self.result_card(body, "check", "success", f"{res['recovered']:,} file(s) recovered", lines)
            self.done_actions(out)
        def stopped(info):
            n = info.get("recovered", 0)
            self.cancelled_state(f"Stopped after {n:,} of {info.get('total', len(ents)):,} file(s)",
                                 [f"The files already saved are in {out}.",
                                  "The file being saved when you pressed Cancel was removed (it was incomplete).",
                                  "Nothing on the source drive was changed."], step=3,
                                 secondary=("Open folder", lambda: self.app.open_folder(out)))
        self.app.run_job("Recover", job, done, panel, on_error=self._error, on_cancel=stopped)

    def _deep_done(self, res, out, cancelled=False):
        body = self.step(3, "Deep scan finished" if not cancelled else "Deep scan stopped", "")
        n = res["files"]
        self.app.mascot.set_mood("happy" if n else "idle", "found" if n else "none")
        self.result_card(body, "check" if n else "warn", "success" if n else "warn", f"{n:,} file(s) found",
                         [f"Saved into folders by type in {out}"]
                         + (["Stopped by Cancel: the rest of the drive wasn't searched. Nothing on it was "
                             "changed."] if cancelled else []))
        chips = ctk.CTkFrame(body, fg_color="transparent")
        chips.pack(fill="x")
        for ext, cnt in sorted(res.get("by_type", {}).items(), key=lambda x: -x[1]):
            Pill(chips, f"{ext.upper()}  {cnt:,}", "violet").pack(side="left", padx=4, pady=4)
        self.done_actions(out)


# ---------------------------------------------------------------------------
class PartitionWizard(Screen, _PickMixin):
    guide_topic = "partition"

    def __init__(self, master, app):
        super().__init__(master, app, "Find lost partitions", steps=["Drive", "Search", "Restore"])
        self.pick_step(0, "Find lost partitions", "Which drive is missing a partition (or shows as empty / RAW)?",
                       mode="disk")

    def _after_pick(self, mode="quick"):
        dev = self.app.clone(self.target["dev"])
        _, panel = self.progress("Searching for partitions…", "Quick search: checks the usual partition "
                                                              "positions." if mode == "quick" else
                                 "Deep search: checking every sector. This takes a while on big drives.")
        self._draw_steps(1)
        self.app.run_job("Partition search", lambda prog: partscan.scan_partitions(dev, prog, mode=mode),
                         lambda r: self._results(r, mode), panel,
                         on_cancel=lambda info: self.cancelled_state("Search stopped",
                                                                     ["Nothing on the drive was changed."]))

    def _results(self, found, mode):
        lost = [f for f in found if f.status == "Lost"]
        body = self.step(2, f"Found {len(found)} partition{'s' if len(found) != 1 else ''}"
                            + (f" — {len(lost)} missing" if lost else ""),
                         "Tick the ones to bring back." if lost else "Everything found is already in the "
                                                                      "partition table.")
        self.app.mascot.set_mood("happy" if lost else "idle", "found" if lost else "none")
        ss = self.target["dev"].sector_size
        self.checks = []
        lst = ctk.CTkScrollableFrame(body, fg_color="transparent", height=360)
        lst.pack(fill="both", expand=True)
        for f in found:
            card = ctk.CTkFrame(lst, corner_radius=16, fg_color=P["card"])
            card.pack(fill="x", pady=4)
            v = tk.BooleanVar(value=f.status == "Lost")
            if f.status == "Lost":
                ctk.CTkCheckBox(card, text="", variable=v, width=24, fg_color=P["accent"],
                                hover_color=P["accent_hover"]).pack(side="left", padx=(16, 4))
                self.checks.append((v, f))
            b = IconBadge(card, "partition", 42, "accent" if f.status == "Lost" else "violet")
            b.set_bg(theme.c("card"))
            b.pack(side="left", padx=10, pady=10)
            txt = ctk.CTkFrame(card, fg_color="transparent")
            txt.pack(side="left", fill="x", expand=True)
            top = ctk.CTkFrame(txt, fg_color="transparent")
            top.pack(anchor="w")
            ctk.CTkLabel(top, text=f"{f.fs}  {f.label}", font=theme.font(15, "bold"), text_color=P["text"]).pack(
                side="left")
            Pill(top, "Missing" if f.status == "Lost" else "In table",
                 "danger" if f.status == "Lost" else "success").pack(side="left", padx=8)
            ctk.CTkLabel(txt, text=f"Starts at sector {f.start_lba:,} · found via {f.source} · confidence "
                                   f"{f.confidence}", font=theme.font(12), text_color=P["muted"]).pack(anchor="w")
            ctk.CTkLabel(card, text=human_size(f.sectors * ss), font=theme.font(15, "bold"),
                         text_color=P["text"]).pack(side="right", padx=18)
        if mode == "quick":
            self.note(body, "Not what you expected? A deep search checks every single sector.").pack(anchor="w",
                                                                                                      pady=6)
        self.buttons(primary=("Bring them back", self._confirm) if lost else ("Back to home", self.app.home),
                     extra=("Deep search", lambda: self._after_pick("full")) if mode == "quick" else None)

    def _confirm(self):
        chosen = [f for v, f in self.checks if v.get()]
        if not chosen:
            self.app.toast("Tick at least one partition.", "warn")
            return
        dev = self.target["dev"]
        if dev.is_system:
            self.app.toast("The system disk is protected.", "danger")
            return
        body = self.step(2, "Restore partitions?", "I'll back up the partition table first, then add these "
                                                   "entries.")
        self.app.mascot.set_mood("warn", "confirm")
        self.result_card(body, "partition", "violet", f"{len(chosen)} partition(s) → {dev.name}",
                         [f"{f.fs} {f.label} at sector {f.start_lba:,}" for f in chosen])
        gpt = tk.BooleanVar(value=dev.usable_size > 2 * 1024 ** 4)
        ctk.CTkSwitch(body, text="If the drive has no partition table, create GPT (otherwise MBR)",
                      variable=gpt, progress_color=P["accent"], font=theme.font(12)).pack(anchor="w", pady=6)
        go = self.buttons(primary=("Restore now", lambda: self._go(chosen, gpt.get())))
        go.configure(state="disabled")
        self.confirm_box(body, "RESTORE", lambda ok: go.configure(state="normal" if ok else "disabled"))

    def _go(self, chosen, gpt):
        wdev = self.app.clone(self.target["dev"])
        _, panel = self.progress("Restoring…", "Writing the partition table.")

        def job(prog):
            prog.reset(len(chosen) + 1, "Backing up the partition table")
            bk = partitions.backup_table_area(wdev)
            wdev.close()
            wdev.open(writable=True)
            out = []
            try:
                for i, f in enumerate(chosen):
                    out.append(partitions.add_partition_entry(wdev, f.start_lba, f.sectors, f.fs, prefer_gpt=gpt))
                    prog.update(i + 2)
            finally:
                wdev.close()
            return bk, out

        def done(res):
            bk, out = res
            self.app._inventory = None
            body = self.step(2, "Partitions restored", "")
            self.app.mascot.set_mood("happy", "done")
            self.result_card(body, "check", "success", f"{len(out)} partition(s) are back",
                             out + ["", f"Backup saved: {bk}",
                                    "Windows may need a rescan: Disk Management → Action → Rescan Disks."])
            self.done_actions(os.path.dirname(bk))
        self.app.run_job("Restore partitions", job, done, panel)


# ---------------------------------------------------------------------------
class _StrengthMixin:
    def strength_picker(self, parent):
        grp = []
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x")
        for i, (name, mkey, desc) in enumerate(STRENGTHS):
            oc = OptionCard(row, name, desc, mkey, grp, badge_text="Most people" if name == "Recommended" else None)
            oc.grid(row=0, column=i, sticky="nsew", padx=6)
            oc.desc.configure(wraplength=230)
            row.grid_columnconfigure(i, weight=1, uniform="s")
        more = ctk.CTkFrame(parent, fg_color="transparent")
        more.pack(fill="x", pady=(14, 0))
        ctk.CTkLabel(more, text="Other standards:", font=theme.font(13, "bold"), text_color=P["text"]).pack(
            side="left")
        self._more = ctk.CTkOptionMenu(more, values=["—"] + list(wipe.METHODS), width=340, height=34,
                                       corner_radius=12, fg_color=P["card"], button_color=P["violet"],
                                       text_color=P["text"], command=lambda v: self._pick_more(v, grp))
        self._more.pack(side="left", padx=10)
        self.note(parent, "SSDs: overwriting can't reach spare flash cells. For SSDs leaving the business, also use "
                          "the manufacturer's Secure Erase, or physically destroy them.", "warn").pack(
            anchor="w", pady=(14, 0))
        grp[1].select()
        self._sgrp = grp
        return grp

    def _pick_more(self, v, grp):
        if v != "—":
            for o in grp:
                o.configure(border_color=P["border"], fg_color=P["card"])
                o.selected = False

    def chosen_method(self):
        m = self._more.get()
        if m in wipe.METHODS and not any(getattr(o, "selected", False) for o in self._sgrp):
            return wipe.METHODS[m]
        return wipe.METHODS[selected_value(self._sgrp)]


class WipeWizard(Screen, _PickMixin, _StrengthMixin):
    guide_topic = "wipe"

    def __init__(self, master, app):
        super().__init__(master, app, "Wipe a drive", steps=["Drive", "Strength", "Confirm", "Wipe"])
        self.pick_step(0, "Wipe a drive", "Pick a whole drive or a single partition to erase.", writes=True)

    def _after_pick(self):
        body = self.step(1, "How thorough?", f"Erasing {self.target_name()}")
        self.strength_picker(body)
        self.buttons(primary=("Next", self._confirm))

    def _confirm(self):
        self.method = self.chosen_method()
        t = self.target
        dev = t["dev"]
        start, count = self.target_range()
        body = self.step(2, "Last check", "Everything on this target will be permanently destroyed.")
        self.app.mascot.set_mood("warn", "confirm")
        phrase = f"WIPE {dev.name.upper()}" if t["kind"] == "disk" else f"WIPE PARTITION {t['part'].index}"
        self.result_card(body, "wipe", "danger", self.target_name(),
                         [f"Size: {human_size(count * dev.sector_size)}", f"Method: {self.method.name}",
                          "This can't be undone. Recovery tools — including this one — won't get it back."])
        go = self.buttons(primary=("Wipe it", self._go), secondary=("Back", self._after_pick))
        go.configure(state="disabled", fg_color=P["danger"])
        self.confirm_box(body, phrase, lambda ok: go.configure(state="normal" if ok else "disabled"))

    def _go(self):
        dev = self.target["dev"]
        wdev = self.app.clone(dev)
        start, count = self.target_range()
        method = self.method
        _, panel = self.progress("Wiping…", f"{method.name} on {self.target_name()}")
        self._draw_steps(3)
        self.t_start = time.time()
        self.app.run_job("Wipe", lambda prog: wipe.wipe_disk(wdev, method, prog, start_lba=start, sectors=count),
                         self._done, panel, on_cancel=self._stopped)

    def _stopped(self, info):
        self.app._inventory = None
        done, total = info.get("overwritten", 0), info.get("length") or 1
        pas = f"pass {info.get('pass_no', 1)} of {info.get('passes', 1)}"
        if done <= 0:
            lines = ["It stopped before anything was written. The drive is as it was."]
        else:
            lines = [(f"It stopped in {pas}. About {human_size(done)} of {human_size(total)} "
                      f"({100 * done / total:.0f}%) has been overwritten at least once."),
                     ("The drive is now part-erased: its partitions are probably damaged, and data in the part not "
                      "reached yet may still be readable."),
                     "Don't reuse or hand it on like this. Run the wipe again to finish it.",
                     "No wipe certificate was made."]
        self.cancelled_state("Wipe stopped part-way" if done > 0 else "Wipe stopped", lines, step=3,
                             secondary=("Wipe again", self._confirm))

    def _done(self, res):
        self.res = res
        self.res["end"] = time.time()
        self.app._inventory = None
        ok = res["write_errors"] == 0 and res["verify_mismatched_blocks"] == 0
        body = self.step(3, "Wipe complete" if ok else "Wipe finished with problems", "")
        self.app.mascot.set_mood("happy" if ok else "sad", "wiped" if ok else "sick")
        lines = [f"{human_size(res['bytes'])} overwritten · {res['passes']} pass(es)",
                 "Verified by reading it back" if res["verified"] else "Not verified (method has no verify pass)",
                 f"Took {human_time(self.res['end'] - self.t_start)}"]
        if not ok:
            lines.append(f"Unwritable sectors: {res['write_errors']} · verify mismatches: "
                         f"{res['verify_mismatched_blocks']} — this drive may be failing.")
        self.result_card(body, "check" if ok else "warn", "success" if ok else "warn",
                         "Drive is clean" if ok else "Some sectors couldn't be wiped", lines)
        self.buttons(primary=("Back to home", self.app.home), secondary=("Save certificate…", self._cert))

    def _cert(self):
        dev = self.target["dev"]
        name = f"Wipe certificate - {dev.model or dev.name} - {time.strftime('%Y-%m-%d')}.html".replace("/", "-")
        p = filedialog.asksaveasfilename(defaultextension=".html", initialfile=name,
                                         filetypes=[("Web page", "*.html")])
        if not p:
            return
        info = dict(self.res, target=dev.path, model=dev.model, serial=dev.serial, size=dev.size, bus=dev.bus,
                    method=self.method.name, start=self.t_start, scope=self.target_name())
        cid = wipe_certificate(p, info)
        self.app.toast(f"Certificate {cid} saved")
        self.app.open_folder(p)


# ---------------------------------------------------------------------------
class ShredWizard(Screen, _StrengthMixin):
    guide_topic = "shred"

    def __init__(self, master, app):
        super().__init__(master, app, "Shred files", steps=["Choose", "Confirm", "Shred"])
        self.items: list[str] = []
        self.free_folder = None
        self._build()

    def _build(self):
        body = self.step(0, "Shred files", "Drop files and folders below. They'll be overwritten, then deleted "
                                           "for good.")
        self.mode = ctk.CTkSegmentedButton(body, values=["Files & folders", "Free space on a drive"],
                                           command=lambda _v: self._mode_changed(), selected_color=P["accent"],
                                           selected_hover_color=P["accent_hover"], font=theme.font(13, "bold"),
                                           height=36, corner_radius=18)
        self.mode.set("Files & folders")
        self.mode.pack(anchor="w", pady=(0, 12))
        self.mode_area = ctk.CTkFrame(body, fg_color="transparent")
        self.mode_area.pack(fill="x")
        self.files_frame = ctk.CTkFrame(self.mode_area, fg_color="transparent")
        self.dz = DropZone(self.files_frame, "Drop files or folders here", "or use Add files / Add folder below",
                           height=140)
        self.dz.pack(fill="x")
        self.app.drop_handlers.append(lambda kind, _p: self.dz.hot(kind == "enter"))
        btns = ctk.CTkFrame(self.files_frame, fg_color="transparent")
        btns.pack(fill="x", pady=8)
        ghost_button(btns, "Add files…", lambda: self.accept_files(list(filedialog.askopenfilenames())),
                     width=130).pack(side="left")
        ghost_button(btns, "Add folder…", lambda: self.accept_files([filedialog.askdirectory()]),
                     width=130).pack(side="left", padx=8)
        self.list = ctk.CTkScrollableFrame(self.files_frame, fg_color="transparent", height=120)
        self.list.pack(fill="x")
        self.free_frame = ctk.CTkFrame(self.mode_area, fg_color="transparent")
        self.note(self.free_frame, "Fills the drive's empty space with wipe data and then frees it again. Your "
                                   "existing files are kept — this just destroys traces of files deleted earlier."
                  ).pack(anchor="w")
        fr = ctk.CTkFrame(self.free_frame, fg_color="transparent")
        fr.pack(fill="x", pady=10)
        self.free_lbl = ctk.CTkLabel(fr, text="No drive chosen", font=theme.font(14, "bold"), text_color=P["text"])
        self.free_lbl.pack(side="left")
        ghost_button(fr, "Choose drive / folder…", self._pick_free, width=200).pack(side="left", padx=12)
        ctk.CTkLabel(body, text="How thorough?", font=theme.font(15, "bold"), text_color=P["text"]).pack(
            anchor="w", pady=(16, 6))
        self.str_holder = ctk.CTkFrame(body, fg_color="transparent")
        self.str_holder.pack(fill="x")
        self.strength_picker(self.str_holder)
        self._mode_changed()
        self.buttons(primary=("Next", self._confirm))

    def _mode_changed(self):
        files = self.mode.get().startswith("Files")
        self.files_frame.pack_forget()
        self.free_frame.pack_forget()
        (self.files_frame if files else self.free_frame).pack(fill="x")

    def _pick_free(self):
        d = filedialog.askdirectory(title="Pick the drive (or any folder on it)")
        if d:
            self.free_folder = d
            self.free_lbl.configure(text=d)

    def accept_files(self, paths):
        for p in paths:
            if p and p not in self.items and os.path.exists(p):
                self.items.append(p)
        self.mode.set("Files & folders")
        self._mode_changed()
        self._render_items()
        if paths:
            self.app.mascot.say("drop")

    def _render_items(self):
        for w in self.list.winfo_children():
            w.destroy()
        for p in self.items:
            row = ctk.CTkFrame(self.list, corner_radius=12, fg_color=P["card"])
            row.pack(fill="x", pady=2)
            kind = "Folder" if os.path.isdir(p) else human_size(os.path.getsize(p))
            ctk.CTkLabel(row, text=os.path.basename(p.rstrip("/\\")) or p, font=theme.font(13, "bold"),
                         text_color=P["text"]).pack(side="left", padx=12, pady=6)
            ctk.CTkLabel(row, text=f"{kind} · {os.path.dirname(p)}", font=theme.font(11),
                         text_color=P["muted"]).pack(side="left")
            ctk.CTkButton(row, text="✕", width=30, height=26, corner_radius=13, fg_color="transparent",
                          hover_color=P["danger_soft"], text_color=P["danger"],
                          command=lambda x=p: (self.items.remove(x), self._render_items())).pack(side="right",
                                                                                                padx=8)

    def _confirm(self):
        files = self.mode.get().startswith("Files")
        self.method = self.chosen_method()
        if files and not self.items:
            self.app.toast("Add some files or folders first.", "warn")
            return
        if not files and not self.free_folder:
            self.app.toast("Choose a drive first.", "warn")
            return
        body = self.step(1, "Last check", "")
        self.app.mascot.set_mood("warn", "confirm")
        if files:
            self.result_card(body, "shred", "warn", f"Shred {len(self.items)} item(s)",
                             [os.path.basename(p.rstrip('/\\')) for p in self.items[:8]]
                             + (["…"] if len(self.items) > 8 else []) + [f"Method: {self.method.name}"])
            go = self.buttons(primary=("Shred them", lambda: self._go(True)), secondary=("Back", self._build))
            go.configure(state="disabled", fg_color=P["danger"])
            self.confirm_box(body, "SHRED", lambda ok: go.configure(state="normal" if ok else "disabled"))
        else:
            self.result_card(body, "wipe", "violet", f"Clean free space on {self.free_folder}",
                             [f"Method: {self.method.name}", "Your files stay. The drive will look full for a "
                                                             "while, then go back to normal."])
            self.buttons(primary=("Start", lambda: self._go(False)), secondary=("Back", self._build))

    def _go(self, files):
        method = self.method
        _, panel = self.progress("Shredding…" if files else "Cleaning free space…", method.name)
        self._draw_steps(2)
        items, folder = list(self.items), self.free_folder
        job = (lambda prog: wipe.shred_paths(items, method, prog)) if files else (
            lambda prog: wipe.wipe_free_space(folder, method, prog))

        def done(res):
            body = self.step(2, "All done", "")
            self.app.mascot.set_mood("happy", "wiped")
            if files:
                lines = [f"Method: {method.name}"] + ([f"{len(res['failed'])} failed: {res['failed'][0]}"]
                                                      if res["failed"] else [])
                self.result_card(body, "check", "success", f"{res['files']} file(s) shredded", lines)
            else:
                self.result_card(body, "check", "success", f"{human_size(res['bytes'])} of free space cleaned",
                                 [f"Method: {method.name}"])
            self.buttons(primary=("Back to home", self.app.home))
        def stopped(info):
            if files:
                lines = [f"{info.get('shredded', 0)} of {info.get('total', 0)} file(s) were shredded and are gone.",
                         "The file being shredded when you pressed Cancel is partly overwritten but still there: "
                         + str(info.get("current", "")), "The rest weren't touched."]
            else:
                lines = ["The temporary fill file was deleted, so the drive's free space is back.",
                         "Your files weren't touched. Free space is only partly cleaned: run it again to finish."]
            self.cancelled_state("Shredding stopped" if files else "Cleaning stopped", lines, step=2)
        self.app.run_job("Shred", job, done, panel, on_cancel=stopped)


# ---------------------------------------------------------------------------
class HealthWizard(Screen, _PickMixin):
    guide_topic = "health"

    COLORS = ["#2BD9A0", "#8BD45A", "#FFD34D", "#FF9F40", "#FF6A3D", "#FF3B5C"]

    def __init__(self, master, app):
        super().__init__(master, app, "Check drive health", steps=["Drive", "Test", "Result"])
        self.pick_step(0, "Check drive health", "Pick a drive (or partition) to test. Reading only — nothing is "
                                                "changed.")

    def _after_pick(self, repair=False):
        dev = self.app.clone(self.target["dev"])
        start, count = self.target_range()
        body, panel = self.progress("Testing every sector…" if not repair else "Repairing bad sectors…",
                                    "Each square is a slice of the drive. Green is fast, red is unreadable.")
        self._draw_steps(1)
        self.map = tk.Canvas(body, height=150, highlightthickness=0, bd=0, background=theme.c("card"))
        self.map.pack(fill="x", pady=(14, 0))
        body.update_idletasks()
        W = max(600, self.map.winfo_width())
        blocks = -(-count // 2048)
        self.cells = min(blocks, 2400)
        side = max(6, int((W * 150 / self.cells) ** .5))
        while (W // side) * (150 // side) < self.cells and side > 4:
            side -= 1
        self.side, self.cols, self.blocks = side, max(1, W // side), blocks
        worst = [-1] * self.cells
        pending = {}
        last = [time.monotonic()]

        def on_block(b, total, cls, ms):
            i = b * self.cells // total
            if cls > worst[i]:
                worst[i] = cls
                pending[i] = cls
            now = time.monotonic()
            if now - last[0] > .2 and pending:
                last[0] = now
                snap = dict(pending)
                pending.clear()
                self.app.call_soon(self._paint, snap)

        def job(prog):
            r = surface.surface_scan(dev, prog, start_lba=start, sectors=count, on_block=on_block, repair=repair)
            self.app.call_soon(self._paint, dict(pending))
            return r
        self.app.run_job("Health check", job, self._result, panel, on_cancel=lambda info: self.cancelled_state(
            "Health check stopped", ["Sectors that were rewritten so far stay repaired. Nothing else was changed."]
            if repair else ["It only reads, so nothing on the drive was changed."], step=2))

    def _paint(self, cells):
        try:
            for i, cls in cells.items():
                x, y = (i % self.cols) * self.side, (i // self.cols) * self.side
                self.map.create_rectangle(x + 1, y + 1, x + self.side - 1, y + self.side - 1,
                                          fill=self.COLORS[cls], outline="")
        except tk.TclError:
            pass

    def _result(self, res):
        c = res["counts"]
        bad = res["bad_sectors"]
        slow = c[">=600ms"] + c["<600ms"]
        total = max(1, res["blocks"])
        if bad:
            head, icon, tone, key = f"{bad} bad sector(s) found", "warn", "danger", "sick"
            lines = ["Some parts of this drive can't be read. It may be failing.",
                     "Back it up now (Back up / clone), then replace it."]
        elif slow / total > .02:
            head, icon, tone, key = "Readable, but slow in places", "warn", "warn", "sick"
            lines = [f"{slow:,} slow area(s). Often an early sign of wear — keep an eye on it."]
        else:
            head, icon, tone, key = "This drive is healthy", "check", "success", "healthy"
            lines = ["Every sector read back fine, with no slow spots."]
        if res.get("repaired"):
            lines.append(f"{res['repaired']} sector(s) were rewritten and now read correctly.")
        body = self.step(2, "Health check result", self.target_name())
        self.app.mascot.set_mood("happy" if tone == "success" else "sad", key)
        self.result_card(body, icon, tone, head, lines)
        bar = tk.Canvas(body, height=26, highlightthickness=0, bd=0, background=theme.c("bg"))
        bar.pack(fill="x", pady=(4, 2))
        body.update_idletasks()
        W = max(400, bar.winfo_width())
        x = 0
        for i, (k, v) in enumerate(c.items()):
            w = W * v / total
            if w > 0:
                bar.create_rectangle(x, 4, x + max(w, 2), 22, fill=self.COLORS[i], outline="")
                x += max(w, 2)
        legend = ctk.CTkFrame(body, fg_color="transparent")
        legend.pack(fill="x")
        for i, (k, v) in enumerate(c.items()):
            ctk.CTkLabel(legend, text=f"■ {k}: {v:,}", font=theme.font(12), text_color=self.COLORS[i]).pack(
                side="left", padx=(0, 14))
        if res["bad_lbas"]:
            self.note(body, "First bad sectors: " + ", ".join(map(str, res["bad_lbas"][:12]))).pack(anchor="w",
                                                                                                    pady=8)
        dev = self.target["dev"]
        extra = None
        if bad and not dev.is_system:
            extra = ("Try repairing…", self._repair_confirm)
        self.buttons(primary=("Back to home", self.app.home),
                     secondary=("Back it up", lambda: self.app.go(CloneWizard)), extra=extra)

    def _repair_confirm(self):
        body = self.step(2, "Repair bad sectors?", "Unreadable sectors are rewritten so the drive swaps them for "
                                                   "spares. Whatever was in them is already lost.")
        self.app.mascot.set_mood("warn", "confirm")
        go = self.buttons(primary=("Repair", lambda: self._after_pick(repair=True)))
        go.configure(state="disabled")
        self.confirm_box(body, "REPAIR", lambda ok: go.configure(state="normal" if ok else "disabled"))


# ---------------------------------------------------------------------------
class CloneWizard(Screen, _PickMixin):
    guide_topic = "clone"

    def __init__(self, master, app):
        super().__init__(master, app, "Back up / clone", steps=["Source", "Destination", "Confirm", "Copy"])
        self.pick_step(0, "Back up / clone", "What should I copy? A whole drive or a single partition.")

    def _after_pick(self):
        body = self.step(1, "Where to?", f"Copying {self.target_name()}")
        grp = []
        a = OptionCard(body, "Image file (.vhd)", "A single file you can open later — Windows can mount it "
                                                 "directly in Disk Management.", "vhd", grp, icon="image",
                       badge_text="Recommended")
        a.pack(fill="x", pady=5)
        b = OptionCard(body, "Raw image (.img)", "Exact sector-by-sector copy. Works with any recovery tool.",
                       "img", grp, icon="image", tone="violet")
        b.pack(fill="x", pady=5)
        c = OptionCard(body, "Another drive", "Clone straight onto a second drive — e.g. a new SSD / NVMe in a USB "
                                              "enclosure. Everything on it will be replaced.", "disk", grp,
                       icon="clone", tone="danger")
        c.pack(fill="x", pady=5)
        dnet = OptionCard(body, "Several disks or another PC", "Clone to many disks at once — on this PC and/or "
                                                              "linked PCs over the network.", "net", grp,
                          icon="link", tone="violet")
        dnet.pack(fill="x", pady=5)
        self._dgrp = grp
        a.select()
        self.buttons(primary=("Next", self._dest))

    def _dest(self):
        kind = selected_value(self._dgrp)
        if kind == "net":
            from .link_screens import NetCloneWizard
            self.app.go(NetCloneWizard)
            return
        self.kind = kind
        src = self.target
        if kind in ("vhd", "img"):
            name = f"{src['dev'].name.replace(' ', '')}_{time.strftime('%Y%m%d')}.{kind}"
            p = filedialog.asksaveasfilename(defaultextension=f".{kind}", initialfile=name,
                                             filetypes=[("Disk image", f"*.{kind}")])
            if not p:
                return
            self.dest = p
            self._confirm()
        else:
            self.src_target = self.target
            body = self.step(1, "Pick the destination drive", "Everything on it will be replaced.")
            self.dest_target = None

            def chosen(t):
                self.dest_target = t
                nb.configure(state="normal")
            DrivePicker(body, self.app.inventory(), mode="disk", on_select=chosen, writes=True,
                        exclude_path=src["dev"].path, height=470).pack(fill="both", expand=True)
            nb = self.buttons(primary=("Next", self._confirm), secondary=("Back", self._after_pick))
            nb.configure(state="disabled")

    def _confirm(self):
        src = self.target if self.kind != "disk" else self.src_target
        start, count = (src["part"].start_lba, src["part"].sectors) if src["kind"] == "part" else (
            0, src["dev"].total_sectors)
        size = count * src["dev"].sector_size
        body = self.step(2, "Ready to copy", "")
        if self.kind == "disk":
            dst = self.dest_target["dev"]
            if dst.usable_size < size:
                self.result_card(body, "warn", "danger", "Destination is too small",
                                 [f"Need {human_size(size)}, it has {human_size(dst.usable_size)}."])
                self.buttons(primary=("Back", self._dest))
                return
            self.app.mascot.set_mood("warn", "confirm")
            lines = [f"Copy {human_size(size)}", f"ALL data on {dst.describe()} will be replaced."]
            if self._live(src["dev"]):
                lines.append("Windows is using this disk — a snapshot is taken first so the copy is consistent.")
            if src["kind"] == "disk" and dst.usable_size > size:
                lines.append(f"The new disk is {human_size(dst.usable_size - size)} bigger — that space will be "
                             f"unallocated; extend C: in Disk Management afterwards.")
            self.result_card(body, "clone", "danger", f"{src['dev'].name} → {dst.name}", lines)
            self._smart_switch(body)
            go = self.buttons(primary=("Start cloning", lambda: self._go(src, start, count)))
            go.configure(state="disabled", fg_color=P["danger"])
            self.confirm_box(body, f"CLONE TO {dst.name.upper()}",
                             lambda ok: go.configure(state="normal" if ok else "disabled"))
        else:
            lines = [f"Copy {human_size(size)} from {self.target_name()}", f"Saving to {self.dest}",
                     "Unreadable sectors are skipped and logged. A SHA-256 hash is recorded."]
            if self._live(src["dev"]):
                lines.append("Windows is using this disk — a snapshot is taken first so the copy is consistent.")
            self.result_card(body, "image", "violet", os.path.basename(self.dest), lines)
            self._smart_switch(body)
            self.buttons(primary=("Start backup", lambda: self._go(src, start, count)),
                         secondary=("Back", self._after_pick))

    def _smart_switch(self, body):
        self.smart = tk.BooleanVar(value=True)
        ctk.CTkSwitch(body, text="Copy used space only — much faster (NTFS, FAT, exFAT, ext). Other filesystems and "
                                 "OSes are copied sector by sector.", variable=self.smart,
                      progress_color=P["accent"], font=theme.font(12)).pack(anchor="w", pady=(4, 4))

    @staticmethod
    def _live(dev):
        try:
            from ..vss import needs_snapshot
            return needs_snapshot(dev)
        except Exception:  # noqa: BLE001
            return False

    def _go(self, src, start, count):
        sdev = self.app.clone(src["dev"])
        snap = self._live(src["dev"])
        smart = self.smart.get() if hasattr(self, "smart") else False
        if self.kind == "disk":
            dst = self.app.clone(self.dest_target["dev"])
            fmt = "raw"
        else:
            dst, fmt = self.dest, self.kind if self.kind == "vhd" else "raw"
        _, panel = self.progress("Copying…", "Fast pass first, then any tricky sectors one by one.")
        self._draw_steps(3)

        def done(res):
            self.app._inventory = None
            body = self.step(3, "Copy complete", "")
            ok = res["bad_sectors"] == 0
            self.app.mascot.set_mood("happy" if ok else "sad", "done" if ok else "sick")
            lines = [f"{human_size(res.get('copied_bytes', res['bytes']))} copied"
                     + (f" (used space of {human_size(res['bytes'])})" if res.get("copied_bytes", 0) < res["bytes"]
                        else ""), f"Unreadable sectors: {res['bad_sectors']}"]
            if res.get("snapshot"):
                lines.append(f"Copied from a snapshot of {', '.join(res['snapshot'])}")
            if res.get("grown"):
                lines.append(res["grown"])
            if res.get("sha256"):
                lines.append(f"SHA-256: {res['sha256']}")
            self.result_card(body, "check" if ok else "warn", "success" if ok else "warn",
                             "Backup finished" if ok else "Finished — some sectors were unreadable", lines)
            self.done_actions(os.path.dirname(self.dest) if self.kind != "disk" else None)
        def stopped(info):
            self.app._inventory = None
            st = info.get("state")
            if self.kind == "disk":
                lines = (["Nothing was written to the destination drive."] if st == "untouched" else
                         [f"The destination drive {self.dest_target['dev'].name} is only partly written "
                          f"({human_size(info.get('written', 0))} copied).",
                          "It won't start Windows or open reliably like this. Clone again to finish, or wipe it "
                          "before using it for something else."])
            elif st == "complete, not hashed":
                lines = [f"The image {self.dest} is complete. Only the SHA-256 check was skipped."]
            else:
                lines = [f"The unfinished image file was deleted ({os.path.basename(str(self.dest))})."
                         if st == "removed" else f"The image file {self.dest} is incomplete: delete it."]
            if snap:
                lines.append("The snapshot of the running disk was removed.")
            lines.append("The source drive wasn't changed.")
            self.cancelled_state("Copy stopped", lines, step=3)
        self.app.run_job("Copy", lambda prog: surface.image_copy(sdev, dst, prog, start_lba=start, sectors=count,
                                                                 fmt=fmt, snapshot=snap, smart=smart), done, panel,
                         on_cancel=stopped)
