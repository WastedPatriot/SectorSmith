"""Clone this PC to a new drive: pick the new drive (checks tick off one by one), see the plan, type to confirm,
watch the four phases, and get a verdict with a report."""
from __future__ import annotations

import time
import tkinter as tk

import customtkinter as ctk

from .. import sysclone
from .. import verify as V
from ..util import get_logger, human_size, human_time
from . import theme
from .icons import rounded_rect
from .screens import Screen
from .shell import technician
from .widgets import AutoScroll, Card, DrivePicker, EmptyState, Icon, IconBadge, Pill, ProgressPanel, caption, \
    divider, link_button, secondary_button

log = get_logger()
P = theme.PALETTE

ROLE_TONE = {"esp": "info", "msr": "neutral", "windows": "accent", "recovery": "teal", "data": "amber",
             "other": "neutral"}
ROLE_NAME = {"esp": "EFI System", "msr": "Reserved", "windows": "Windows", "recovery": "Recovery", "data": "Data",
             "other": "Other"}
CHECK_LOOK = {"ok": ("check", "success"), "warn": ("warn", "warn"), "block": ("close", "danger"),
              "wait": ("clock", "muted"), "run": ("refresh", "accent")}
VERDICT_LOOK = {V.PASSED: ("check", "success"), V.PROBLEMS: ("warn", "warn"), V.FAILED: ("close", "danger"),
                V.NOT_RUN: ("clock", "neutral")}
PHASE_TITLES = (("snapshot", "Snapshot"), ("copy", "Copy"), ("verify", "Verify"), ("boot", "Boot fix-up"))


def confirm_phrase(dev) -> str:
    serial = "".join(ch for ch in (dev.serial or "") if ch.isalnum())[-6:]
    return f"CLONE TO {dev.name.upper()}" + (f" {serial.upper()}" if serial else "")


# ---------------------------------------------------------------------------- pieces
class LayoutBar(tk.Canvas):
    """A disk as one bar: partitions to scale (small ones get a minimum width), free space as track, a caption
    under every partition that has room for one."""

    def __init__(self, master, parts, total, bg="surface", height=56):
        super().__init__(master, height=height, highlightthickness=0, bd=0)
        self.parts, self.total, self.bg = parts, max(1, total), bg
        self.bind("<Configure>", lambda _e: self.redraw(), add="+")
        theme.on_theme_change(self.redraw)
        self.redraw()

    def redraw(self):
        self.delete("all")
        self.configure(background=theme.c(self.bg))
        w = max(self.winfo_width(), 200)
        bar_h = 24
        rounded_rect(self, 1, 1, w - 1, bar_h, 6, fill=theme.c("track"), outline="")
        spans = []
        min_w = 8
        for role, start, size, _label in self.parts:
            x0 = (w - 2) * start / self.total + 1
            x1 = max(x0 + min_w, (w - 2) * (start + size) / self.total + 1)
            spans.append([x0, x1])
        for i in range(1, len(spans)):  # keep the minimum widths from overlapping
            if spans[i][0] < spans[i - 1][1] + 2:
                shift = spans[i - 1][1] + 2 - spans[i][0]
                spans[i][0] += shift
                spans[i][1] += shift
        font = (theme.family(), 9)
        for (role, _s, size, label), (x0, x1) in zip(self.parts, spans):
            x1 = min(x1, w - 1)
            rounded_rect(self, x0, 1, x1, bar_h, 5, fill=theme.c(ROLE_TONE.get(role, "neutral")), outline="")
            if x1 - x0 > 70:
                self.create_text(x0 + 2, bar_h + 6, text=label or ROLE_NAME.get(role, role), anchor="nw",
                                 fill=theme.c("text_2"), font=(theme.family(), 9, "bold"))
                self.create_text(x0 + 2, bar_h + 20, text=human_size(size), anchor="nw", fill=theme.c("muted"),
                                 font=font)


class CheckRow(ctk.CTkFrame):
    """One preflight item: status icon, title, and the reason with its fix when it blocks."""

    def __init__(self, master, title):
        super().__init__(master, fg_color="transparent")
        self.icon = Icon(self, "clock", 18, "muted", "surface")
        self.icon.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 10), pady=(3, 0))
        self.title = ctk.CTkLabel(self, text=title, font=theme.font_style("body_strong"), text_color=P["text"],
                                  anchor="w", height=20)
        self.title.grid(row=0, column=1, sticky="w")
        self.detail = ctk.CTkLabel(self, text="Waiting for a target drive", font=theme.font_style("small"),
                                   text_color=P["muted"], anchor="w", justify="left", wraplength=300)
        self.detail.grid(row=1, column=1, sticky="w")
        self.grid_columnconfigure(1, weight=1)
        self.bind("<Configure>", lambda e: self.detail.configure(wraplength=max(160, e.width - 40)), add="+")

    def set(self, state, detail="", fix=""):
        name, tone = CHECK_LOOK[state]
        self.icon.set(color=tone, name=name)
        text = detail + (f"\nFix: {fix}" if fix and state == "block" else "")
        self.detail.configure(text=text, text_color=P["danger"] if state == "block" else
                              P["warn"] if state == "warn" else P["muted"])


class VerdictBadge(ctk.CTkFrame):
    """The large verdict on the finish screen."""

    def __init__(self, master, verdict: dict):
        st = verdict.get("status", V.NOT_RUN)
        icon, tone = VERDICT_LOOK.get(st, VERDICT_LOOK[V.NOT_RUN])
        soft = {"success": "success_soft", "warn": "warn_soft", "danger": "danger_soft"}.get(tone, "neutral_soft")
        super().__init__(master, corner_radius=14, fg_color=P[soft], border_width=2, border_color=P[tone])
        IconBadge(self, icon, 64, tone, bg=soft).grid(row=0, column=0, rowspan=2, padx=(22, 18), pady=20)
        ctk.CTkLabel(self, text=V.label(verdict), font=theme.font_style("display"), text_color=P[tone],
                     anchor="w").grid(row=0, column=1, sticky="sw", pady=(20, 0), padx=(0, 22))
        self.sub = ctk.CTkLabel(self, text=verdict.get("summary", ""), font=theme.font_style("body"),
                                text_color=P["text_2"], anchor="w", justify="left", wraplength=640)
        self.sub.grid(row=1, column=1, sticky="nw", pady=(2, 20), padx=(0, 22))
        self.grid_columnconfigure(1, weight=1)
        self.bind("<Configure>", lambda e: self.sub.configure(wraplength=max(240, e.width - 150)), add="+")


# ---------------------------------------------------------------------------- the wizard
class SysCloneWizard(Screen):
    guide_topic = "clone"

    def __init__(self, master, app):
        super().__init__(master, app, "Clone this PC to a new drive", steps=["New drive", "Plan", "Confirm", "Clone"])
        self.layer = sysclone.get_layer()
        self.src = None
        self.dst = None
        self.pf = None
        self.take_offline = tk.BooleanVar(value=False)
        self.bitlocker = tk.BooleanVar(value=True)
        self._pf_token = 0
        self.res = None
        if self.layer is None:
            body = self.step(0, "Clone this PC to a new drive", "")
            EmptyState(body, "Needs Windows", "Cloning the running system works on Windows, where SectorSmith can "
                                              "take a snapshot of the drive Windows runs from.", icon="pc",
                       bg="bg").pack(pady=40)
            self.buttons(primary=("Back to home", app.home))
            return
        app.background(lambda: sysclone.cleanup_stale(self.layer), lambda _r: None, error=lambda _e: None)
        self.pick()

    # -- step 1: the new drive and the checks ----------------------------------------------------------------------
    def _source(self, inv):
        devs = [d for d, _pt, err in inv if not err]
        return self.layer.system_disk(devs)

    def pick(self):
        body = self.step(0, "Clone this PC to a new drive", "Windows keeps running while it copies. Pick the drive "
                                                             "to copy it to; everything on that drive is replaced.")
        self.app.mascot.set_mood("idle", "pick")
        self.dst = None
        self.pf = None
        inv = self.app.inventory()
        self.src = self._source(inv)
        if self.src is None:
            EmptyState(body, "Couldn't find the drive Windows runs from",
                       "Run SectorSmith as administrator so it can see the physical drives.", icon="drive",
                       bg="bg").pack(pady=40)
            self.buttons(primary=("Back to home", self.app.home))
            return
        self._resume_card(body)
        cols = ctk.CTkFrame(body, fg_color="transparent")
        cols.pack(fill="both", expand=True)
        cols.grid_columnconfigure(0, weight=11, uniform="c")
        cols.grid_columnconfigure(1, weight=9, uniform="c")
        cols.grid_rowconfigure(0, weight=1)
        left = ctk.CTkFrame(cols, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        src_card = Card(left, "This PC's system drive", pad=16)
        src_card.pack(fill="x")
        row = ctk.CTkFrame(src_card.body, fg_color="transparent")
        row.pack(fill="x")
        IconBadge(row, "pc", 36, "accent").pack(side="left", padx=(0, 12))
        t = ctk.CTkFrame(row, fg_color="transparent")
        t.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(t, text=f"{self.src.name}  ·  {self.src.model or 'Disk'}", font=theme.font_style("body_strong"),
                     text_color=P["text"], anchor="w").pack(fill="x")
        ctk.CTkLabel(t, text=f"{self.src.bus}  ·  {human_size(self.src.size)}  ·  "
                             f"{self.src.sector_size}-byte sectors", font=theme.font_style("small"),
                     text_color=P["muted"], anchor="w").pack(fill="x")
        Pill(row, "SOURCE", "accent").pack(side="right")
        top = ctk.CTkFrame(left, fg_color="transparent")
        top.pack(fill="x", pady=(14, 0))
        caption(top, "Copy it to").pack(side="left")
        secondary_button(top, "Refresh", self.refresh_drives, width=90, height=28).pack(side="right")
        self.picker = DrivePicker(left, inv, mode="disk", on_select=self._chosen, writes=True,
                                  exclude_path=self.src.path, height=330)
        self.picker.pack(fill="both", expand=True, pady=(6, 0))
        right = Card(cols, "Before anything is written", pad=16)
        right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        self.rows = {}
        scroll = AutoScroll(right.body, fg_color="transparent")
        scroll.pack(fill="both", expand=True)
        for key, title in sysclone.PREFLIGHT_ITEMS:
            r = CheckRow(scroll, title)
            r.pack(fill="x", pady=(0, 10), padx=(0, 6))
            self.rows[key] = r
        self.offline_box = ctk.CTkCheckBox(right.body, text="Take the target's volumes offline",
                                           variable=self.take_offline, font=theme.font_style("body"),
                                           command=self._rerun)
        self.summary_lbl = ctk.CTkLabel(right.body, text="Pick a drive to check it.", font=theme.font_style("small"),
                                        text_color=P["muted"], anchor="w", justify="left")
        self.summary_lbl.pack(fill="x", side="bottom", pady=(6, 0))
        self.next_btn = self.buttons(primary=("Next", self.plan_step))
        self.next_btn.configure(state="disabled")

    def refresh_drives(self):
        self.app._inventory = None
        self.pick()

    def _resume_card(self, body):
        try:
            js = sysclone.interrupted()
        except Exception:  # noqa: BLE001
            js = []
        if not js:
            return
        j = js[0]
        card = ctk.CTkFrame(body, corner_radius=12, fg_color=P["warn_soft"], border_width=1, border_color=P["warn"])
        card.pack(fill="x", pady=(0, 12))
        Icon(card, "warn", 22, "warn", "warn_soft").pack(side="left", padx=(16, 12), pady=14)
        dst = j.data.get("dst") or {}
        when = time.strftime("%d %b %H:%M", time.localtime(j.data.get("created", 0)))
        done = len(j.done())
        total = len(j.data.get("ranges") or []) or 1
        ctk.CTkLabel(card, text=f"A clone to {dst.get('name', '?')} ({human_size(dst.get('size', 0))}) stopped on "
                                f"{when}, {100 * done // total}% copied. It can carry on from there.",
                     font=theme.font_style("body"), text_color=P["text"], anchor="w", justify="left").pack(
            side="left", fill="x", expand=True)
        secondary_button(card, "Discard", lambda: self._discard(j), width=100, height=32).pack(side="right",
                                                                                               padx=(6, 16))
        secondary_button(card, "Resume", lambda: self._resume(j), width=100, height=32).pack(side="right")

    def _discard(self, j):
        def work():
            sysclone.discard(j, self.layer)
        self.app.background(work, lambda _r: self.pick(), error=lambda e: self.app.toast(str(e), "danger"))

    def _resume(self, j):
        want = j.data.get("dst") or {}
        dst = next((d for d, _pt, _e in self.app.inventory(refresh=True) if d.path == want.get("path")), None)
        if dst is None or not sysclone.resume_target_ok(j, dst):
            self.app.toast("That drive isn't connected, or something else was written to it. Discard and start "
                           "again.", "warn")
            return
        self.dst = dst
        plan = sysclone.Plan.from_dict(j.data["plan"])
        self._go(plan, journal=j)

    def _chosen(self, target):
        self.dst = target["dev"]
        self._rerun()

    def _rerun(self):
        if self.dst is None:
            return
        self._pf_token += 1
        token = self._pf_token
        self.next_btn.configure(state="disabled")
        self.offline_box.pack_forget()
        for r in self.rows.values():
            r.set("wait", "Waiting")
        self.rows["admin"].set("run", "Checking")
        self.summary_lbl.configure(text="Checking…", text_color=P["muted"])
        src, dst = self.app.clone(self.src), self.app.clone(self.dst)
        queue_ = []
        keys = [k for k, _t in sysclone.PREFLIGHT_ITEMS]
        offline = self.take_offline.get()   # tk variables are read on the UI thread only

        def on_check(c):
            queue_.append(c)

        def work():
            try:
                return sysclone.preflight(src, dst, self.layer, take_offline=offline, on_check=on_check)
            finally:
                src.close()
                dst.close()

        def show(i=0):
            if token != self._pf_token or not self.winfo_exists():
                return
            if i < len(queue_):
                c = queue_[i]
                self.rows[c.key].set(c.status, c.detail, c.fix)
                nxt = keys.index(c.key) + 1
                if nxt < len(keys) and i + 1 >= len(queue_):
                    self.rows[keys[nxt]].set("run", "Checking")
                self.after(0 if theme.reduced_motion() else 110, lambda: show(i + 1))
            elif getattr(self, "_pf_result", (None, None))[0] == token:
                self._preflight_done(self._pf_result[1])
            else:
                self.after(60, lambda: show(i))

        def done(pf):
            self._pf_result = (token, pf)

        def failed(e):
            self._pf_result = (token, None)
            self.app.toast(f"The checks couldn't run: {e}", "danger")
        self._pf_result = (None, None)
        self.app.background(work, done, error=failed)
        show()

    def _preflight_done(self, pf):
        self.pf = pf
        if pf is None:
            return
        blocks = pf.blocking()
        tc = next(c for c in pf.checks if c.key == "target")
        if (tc.status == "block" and "offline" in tc.fix) or self.take_offline.get():
            self.offline_box.pack(fill="x", side="bottom", pady=(8, 0))
        if blocks:
            self.summary_lbl.configure(text=f"{len(blocks)} thing{'s' if len(blocks) != 1 else ''} to sort out "
                                            "first. Nothing has been written.", text_color=P["danger"])
            self.app.mascot.set_mood("sad", "sick")
        else:
            warns = sum(1 for c in pf.checks if c.status == "warn")
            self.summary_lbl.configure(text="Ready." + (f" Read the {warns} note{'s' if warns != 1 else ''} above."
                                                        if warns else ""), text_color=P["success"])
            self.next_btn.configure(state="normal")
            self.app.mascot.set_mood("idle", "pick")

    # -- step 2: the plan ------------------------------------------------------------------------------------------
    def plan_step(self):
        pf = self.pf
        plan = pf.plan
        ss = plan.sector_size
        body = self.step(1, "The plan", f"{self.src.name} ({human_size(self.src.size)}) to {self.dst.name} "
                                        f"({human_size(self.dst.size)}). Nothing is written until you confirm.")
        page = AutoScroll(body)
        page.pack(fill="both", expand=True)
        bars = Card(page, "Layout", pad=16)
        bars.pack(fill="x", padx=(0, 4))
        for title, parts, total in (
                ("Now, on " + self.src.name, [(p.role, p.src_start * ss, p.src_sectors * ss, ROLE_NAME.get(p.role))
                                              for p in plan.parts], self.src.size),
                ("After, on " + self.dst.name, [(p.role, p.start * ss, p.sectors * ss, ROLE_NAME.get(p.role))
                                                for p in plan.parts], self.dst.size)):
            caption(bars.body, title).pack(anchor="w", pady=(4, 4))
            LayoutBar(bars.body, parts, total).pack(fill="x", pady=(0, 6))
        tbl = Card(page, "What happens to each partition", pad=0)
        tbl.pack(fill="x", pady=(12, 0), padx=(0, 4))
        tbl.head.pack_configure(padx=16)
        cols = (("Partition", 0), ("Now", 100), ("After", 100), ("What happens", 330))
        head = ctk.CTkFrame(tbl.body, fg_color=P["surface_2"], corner_radius=0, height=32)
        head.pack(fill="x")
        for i, (name, _w) in enumerate(cols):
            caption(head, name).grid(row=0, column=i, sticky="w", padx=(16 if i == 0 else 0, 12), pady=8)
        self._cols(head, cols)
        for p in plan.parts:
            divider(tbl.body).pack(fill="x")
            r = ctk.CTkFrame(tbl.body, fg_color="transparent")
            r.pack(fill="x")
            name = ctk.CTkFrame(r, fg_color="transparent")
            name.grid(row=0, column=0, sticky="w", padx=(16, 12), pady=8)
            ctk.CTkLabel(name, text="■", font=theme.font(14), text_color=P[ROLE_TONE.get(p.role, "neutral")],
                         width=14).pack(side="left", padx=(0, 8))
            lt = ", ".join(x.rstrip("\\") for x in p.letters)
            ctk.CTkLabel(name, text=ROLE_NAME.get(p.role, p.role) + (f" ({lt})" if lt else ""),
                         font=theme.font_style("body_strong"), text_color=P["text"]).pack(side="left")
            if p.fs:
                Pill(name, p.fs, "neutral").pack(side="left", padx=(8, 0))
            for i, size in ((1, p.src_sectors * ss), (2, p.sectors * ss)):
                ctk.CTkLabel(r, text=human_size(size), font=theme.font_style("body"), text_color=P["text_2"],
                             anchor="w").grid(row=0, column=i, sticky="w", padx=(0, 12))
            what = {"used": "Used space copied from the snapshot", "raw": "Copied block for block",
                    "zero": "Rebuilt empty"}[p.action]
            if p.resize == "grow":
                what += ", grown to fill the drive"
            elif p.resize == "shrink":
                what += ", shrunk to fit"
            ctk.CTkLabel(r, text=what, font=theme.font_style("body"), text_color=P["text_2"], anchor="w").grid(
                row=0, column=3, sticky="w", padx=(0, 12))
            self._cols(r, cols)
        notes = Card(page, "Good to know", pad=16)
        notes.pack(fill="x", pady=(12, 0), padx=(0, 4))
        lines = ["Windows keeps running. A snapshot freezes the drive as it is when the copy starts; anything saved "
                 "after that is not on the new drive.",
                 "Every block is read back from the new drive and compared. Then the new drive gets its own boot "
                 "files and identity, and the report says what was checked.",
                 f"New partition table: {plan.scheme} ({plan.firmware}), new disk and partition IDs, so both drives "
                 "can be connected at the same time."]
        lines += plan.notes
        for line in lines:
            self._bullet(notes.body, line)
        if any(p.bitlocker == "unlocked" for p in (pf.layout.parts if pf.layout else [])):
            self._bullet(notes.body, "BitLocker: the new drive is not encrypted.", tone="warn")
            ctk.CTkCheckBox(notes.body, text="Open the BitLocker settings at the first sign-in on the new drive",
                            variable=self.bitlocker, font=theme.font_style("body")).pack(anchor="w", pady=(6, 0))
        else:
            self.bitlocker.set(False)
        self.buttons(primary=("Next", self.confirm_step), secondary=("Back", self.pick))

    @staticmethod
    def _cols(frame, cols):
        frame.grid_columnconfigure(0, weight=1)
        for i, (_n, w) in enumerate(cols[1:], 1):
            frame.grid_columnconfigure(i, minsize=w + 12, weight=1 if i == len(cols) - 1 else 0)

    def _bullet(self, parent, text, tone="text_2"):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ctk.CTkLabel(row, text="•", font=theme.font_style("body"), text_color=P[tone], width=14).pack(
            side="left", anchor="n")
        lbl = ctk.CTkLabel(row, text=text, font=theme.font_style("body"), text_color=P[tone], anchor="w",
                           justify="left", wraplength=760)
        lbl.pack(side="left", fill="x", expand=True)
        row.bind("<Configure>", lambda e: lbl.configure(wraplength=max(200, e.width - 30)), add="+")

    # -- step 3: confirm -------------------------------------------------------------------------------------------
    def confirm_step(self):
        plan, dst = self.pf.plan, self.dst
        body = self.step(2, "Confirm the clone", "Everything on the new drive is replaced. This PC's drive is only "
                                                 "read.")
        self.app.mascot.set_mood("warn", "confirm")
        win = plan.part("windows")
        ctx = self.app.context
        self.summary(body, "clone", "danger", f"{self.src.name} to {dst.name}  ·  {dst.model or 'Disk'}",
                     f"{dst.bus}  ·  {human_size(dst.size)}  ·  everything on it is replaced",
                     [("Serial", theme.mask(dst.serial) if dst.serial else "-"),
                      ("Windows", f"{human_size(win.src_sectors * plan.sector_size)} to "
                                  f"{human_size(win.sectors * plan.sector_size)}"),
                      ("Client", ctx.get("client") or "Not set"), ("Technician", technician() or "-"),
                      ("Checks", "Every block read back, then boot files checked"),
                      ("Report", "Saved when it finishes")],
                     note="Keep this PC plugged in. You can keep working; files saved from now on won't be on the "
                          "new drive.")
        go = self.buttons(primary=("Clone this PC", lambda: self._go(plan)), secondary=("Back", self.plan_step))
        go.configure(state="disabled", fg_color=P["danger"], hover_color=P["danger_hover"])
        self.confirm_box(body, confirm_phrase(dst), lambda ok: go.configure(state="normal" if ok else "disabled"))

    # -- step 4: run -------------------------------------------------------------------------------------------------
    def _go(self, plan, journal=None):
        src, dst = self.app.clone(self.src), self.app.clone(self.dst)
        body = self.step(3, "Cloning this PC…", f"{self.src.name} to {self.dst.name}. You can keep using the "
                                                       "PC.")
        self.chips = {}
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", pady=(4, 6))
        for i, (key, title) in enumerate(PHASE_TITLES):
            chip = ctk.CTkFrame(row, corner_radius=10, fg_color=P["surface"], border_width=1, border_color=P["border"])
            chip.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 6, 0 if i == 3 else 6))
            row.grid_columnconfigure(i, weight=1, uniform="ph")
            ic = Icon(chip, "clock", 18, "muted", "surface")
            ic.pack(side="left", padx=(14, 8), pady=12)
            lbl = ctk.CTkLabel(chip, text=f"{i + 1}. {title}", font=theme.font_style("body_strong"),
                               text_color=P["muted"])
            lbl.pack(side="left")
            self.chips[key] = (chip, ic, lbl)
        panel = ProgressPanel(body, self.app.cancel_job)
        panel.pack(fill="x", pady=(6, 0))
        self.note(body, "Time left covers copying and reading everything back; the boot fix-up after that takes "
                        "about a minute.").pack(anchor="center")
        self._set_phase("snapshot")
        bitlocker, offline = self.bitlocker.get(), self.take_offline.get()

        def on_event(kind, value):
            if kind == "phase":
                self.app.call_soon(self._set_phase, value)

        def job(prog):
            return sysclone.run_clone(src, dst, plan, self.layer, prog, journal=journal, bitlocker_on_boot=bitlocker,
                                      take_offline=offline, on_event=on_event)
        self.app.run_job("Clone this PC", job, self._done, panel, on_error=self._error, on_cancel=self._stopped)

    def _set_phase(self, key):
        keys = [k for k, _t in PHASE_TITLES]
        if key not in keys or not getattr(self, "chips", None):
            return
        cur = keys.index(key)
        try:
            for i, k in enumerate(keys):
                chip, ic, lbl = self.chips[k]
                if i < cur:
                    chip.configure(fg_color=P["success_soft"], border_color=P["success"])
                    ic.set(color="success", bg="success_soft", name="check")
                    lbl.configure(text_color=P["success"])
                elif i == cur:
                    chip.configure(fg_color=P["accent_soft"], border_color=P["accent"])
                    ic.set(color="accent", bg="accent_soft", name="refresh")
                    lbl.configure(text_color=P["accent"])
                else:
                    chip.configure(fg_color=P["surface"], border_color=P["border"])
                    ic.set(color="muted", bg="surface", name="clock")
                    lbl.configure(text_color=P["muted"])
        except tk.TclError:
            pass

    # -- results -----------------------------------------------------------------------------------------------------
    def _done(self, res):
        self.res = res
        self.app._inventory = None
        v = res.get("verify") or V.not_run()
        body = self.step(3, "Clone finished" if v["status"] in (V.PASSED, V.PROBLEMS) else "Clone failed",
                         f"{self.src.name} to {self.dst.name}  ·  took {human_time(res.get('seconds', 0))}")
        self.app.mascot.set_mood({V.PASSED: "happy"}.get(v["status"], "sad"),
                                 {V.PASSED: "done"}.get(v["status"], "sick"))
        VerdictBadge(body, v).pack(fill="x", pady=(0, 12))
        card = Card(body, "Checks", pad=0)
        card.pack(fill="both", expand=True)
        card.head.pack_configure(padx=16)
        if res.get("report"):
            link_button(card.head, "Open report", lambda: self.app.open_folder(res["report"])).pack(side="right")
        lst = AutoScroll(card.body, fg_color="transparent")
        lst.pack(fill="both", expand=True, padx=(16, 6))
        for c in res.get("checks", []):
            st = {"passed": "ok", "problems": "warn", "failed": "block"}.get(c["status"], "wait")
            r = CheckRow(lst, c["name"])
            r.set(st, c.get("detail", ""))
            r.pack(fill="x", pady=(8, 2))
        lines = []
        if res.get("bad_sectors"):
            lines.append(f"{res['bad_sectors']} unreadable sector(s) on this PC's drive were copied as zeros; the "
                         "report lists them. Files there may be damaged.")
        if v["status"] == V.FAILED:
            lines.append("The new drive was marked unusable so it can't be mistaken for a working copy. Fix what "
                         "failed and clone again.")
        elif v["status"] in (V.PASSED, V.PROBLEMS):
            lines.append("Shut down, disconnect the old drive (or pick the new one in the boot menu) and start the "
                         "PC from the new drive.")
        for line in lines:
            self._bullet(body, line, tone="text_2")
        self.buttons(primary=("Back to home", self.app.home),
                     secondary=("Open report", lambda: self.app.open_folder(res["report"])) if res.get("report")
                     else None)

    def _error(self, e):
        self.app._inventory = None
        body = self.step(3, "Clone didn't start", "")
        if isinstance(e, sysclone.SysCloneError):
            lines = [e.reason] + ([f"Fix: {e.fix}"] if e.fix else []) + ["Nothing was written to the new drive."]
        else:
            lines = [str(e), "Details are in the log."]
        self.result_card(body, "warn", "danger", "Stopped before writing anything", lines)
        self.buttons(primary=("Back to home", self.app.home), secondary=("Try again", self.pick))

    def _stopped(self, info):
        self.app._inventory = None
        st = info.get("state")
        lines = {"marked-unusable": [f"The new drive {self.dst.name} was marked unusable: its partition table is "
                                     "cleared, so it can't be mistaken for a working copy.",
                                     "Clone again to finish, or erase it before using it for something else."],
                 "untouched": ["It stopped before anything was written. The new drive is as it was."],
                 "part-written": [f"The new drive {self.dst.name} is partly written and couldn't be marked. Don't "
                                  "use it; clone again or erase it."]}.get(st, [])
        lines += ["The snapshot was removed. This PC's drive wasn't changed."]
        self.cancelled_state("Clone stopped", lines, step=3, secondary=("Start again", self.pick))
