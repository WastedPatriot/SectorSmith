"""Screens for SectorSmith Link: connect a machine, move a user to a new PC, clone disks between PCs."""
from __future__ import annotations

import os
import tkinter as tk

import customtkinter as ctk

from ..link import migrate, netclone
from ..util import human_size, human_time
from . import theme
from .screens import Screen
from .widgets import IconBadge, Pill, bind_all, ghost_button, primary_button

P = theme.PALETTE


# ---------------------------------------------------------------------------
class MachinePicker(ctk.CTkFrame):
    """Horizontal row of machine cards (This PC + linked machines)."""

    def __init__(self, master, app, on_select, exclude=None):
        super().__init__(master, fg_color="transparent")
        self.app, self.on_select, self.exclude = app, on_select, exclude
        self.selected = None
        self.cards = []
        self.render()

    def render(self):
        for w in self.winfo_children():
            w.destroy()
        self.cards = []
        for m in self.app.machines():
            card = ctk.CTkFrame(self, corner_radius=16, fg_color=P["card"], border_width=2, border_color=P["card"])
            card.pack(side="left", padx=(0, 10))
            b = IconBadge(card, "pc", 40, "violet" if m.is_local else "success")
            b.set_bg(theme.c("card"))
            b.pack(side="left", padx=(12, 8), pady=10)
            t = ctk.CTkFrame(card, fg_color="transparent")
            t.pack(side="left", padx=(0, 16))
            info = m.info_cache if not m.is_local else m.info()
            ctk.CTkLabel(t, text=m.label if not m.is_local else f"This PC ({info['hostname']})",
                         font=theme.font(14, "bold"), text_color=P["text"]).pack(anchor="w")
            ctk.CTkLabel(t, text=("linked · " + m.address) if not m.is_local else info.get("os", ""),
                         font=theme.font(11), text_color=P["muted"]).pack(anchor="w")
            self.cards.append((card, b, m))

            def click(_e=None, card=card, b=b, m=m):
                for c2, b2, _m in self.cards:
                    c2.configure(border_color=P["card"], fg_color=P["card"])
                    b2.set_bg(theme.c("card"))
                card.configure(border_color=P["accent"], fg_color=P["accent_soft"])
                b.set_bg(theme.c("accent_soft"))
                self.selected = m
                self.on_select(m)
            card.click = click
            bind_all(card, "<Button-1>", click)
        add = ctk.CTkButton(self, text="+  Link another PC", height=60, corner_radius=16, fg_color="transparent",
                            border_width=2, border_color=P["border"], hover_color=P["card_hover"],
                            text_color=P["accent"], font=theme.font(13, "bold"), command=self.app.open_connect)
        add.pack(side="left")


class EndpointDiskPicker(ctk.CTkScrollableFrame):
    def __init__(self, master, disks, on_select, writes=False, exclude_path=None, height=300):
        super().__init__(master, fg_color="transparent", height=height)
        self.rows = []
        if not disks:
            ctk.CTkLabel(self, text="No disks visible on this machine (is it running as administrator?)",
                         font=theme.font(13), text_color=P["muted"]).pack(pady=20)
        for d in disks:
            if d["path"] == exclude_path:
                continue
            locked = writes and d["is_system"]
            card = ctk.CTkFrame(self, corner_radius=14, fg_color=P["card"], border_width=2, border_color=P["card"])
            card.pack(fill="x", pady=3, padx=(0, 6))
            b = IconBadge(card, "image" if d.get("is_image") else "drive", 38, "violet")
            b.set_bg(theme.c("card"))
            b.grid(row=0, column=0, rowspan=2, padx=12, pady=8)
            top = ctk.CTkFrame(card, fg_color="transparent")
            top.grid(row=0, column=1, sticky="w", pady=(8, 0))
            ctk.CTkLabel(top, text=f"{d['name']}  ·  {d['model'] or 'Disk'}", font=theme.font(14, "bold"),
                         text_color=P["text"]).pack(side="left")
            if d["is_system"]:
                Pill(top, "WINDOWS RUNS HERE", "warn").pack(side="left", padx=6)
            parts = ", ".join(f"{(p['mount'][0].rstrip(chr(92)) + ' ') if p['mount'] else ''}{p['fs'] or '?'}"
                              for p in d["partitions"][:5])
            sub = "Protected — boot this PC from the SectorSmith USB to clone onto it" if locked else \
                f"{d['bus']} · {d['scheme']} · {parts or 'no partitions'}"
            ctk.CTkLabel(card, text=sub, font=theme.font(12), text_color=P["warn"] if locked else P["muted"]).grid(
                row=1, column=1, sticky="w", pady=(0, 8))
            ctk.CTkLabel(card, text=human_size(d["size"]), font=theme.font(14, "bold"), text_color=P["text"]).grid(
                row=0, column=2, rowspan=2, padx=14)
            card.grid_columnconfigure(1, weight=1)
            if locked or d.get("error"):
                continue
            self.rows.append((card, b, d))

            def click(_e=None, card=card, b=b, d=d):
                for c2, b2, _d in self.rows:
                    c2.configure(border_color=P["card"], fg_color=P["card"])
                    b2.set_bg(theme.c("card"))
                card.configure(border_color=P["accent"], fg_color=P["accent_soft"])
                b.set_bg(theme.c("accent_soft"))
                on_select(d)
            card.click = click
            bind_all(card, "<Button-1>", click)


def _files(b, n):
    return f"{human_size(b)} · {n:,} file{'s' if n != 1 else ''}" if n else "empty"


def _loading(parent, text="Asking the machine…"):
    lbl = ctk.CTkLabel(parent, text=text, font=theme.font(13), text_color=P["muted"])
    lbl.pack(pady=20)
    return lbl


# ---------------------------------------------------------------------------
class ConnectScreen(Screen):
    guide_topic = "connect"

    def __init__(self, master, app):
        super().__init__(master, app, "Connect a machine",
                         "Link another PC on the same network to move a user or clone disks between them.")
        self.app.mascot.set_mood("idle", text="Let's link up another PC!")
        body = self.new_body()
        self.tabs = ctk.CTkSegmentedButton(body, values=["Paste a command", "PC booted from USB"],
                                           command=lambda _v: self._tab(), selected_color=P["accent"],
                                           selected_hover_color=P["accent_hover"], font=theme.font(13, "bold"),
                                           height=36, corner_radius=18)
        self.tabs.set("Paste a command")
        self.tabs.pack(anchor="w")
        self.area = ctk.CTkFrame(body, fg_color="transparent")
        self.area.pack(fill="x", pady=(12, 0))
        ctk.CTkLabel(body, text="LINKED MACHINES", font=theme.font(11, "bold"), text_color=P["muted"]).pack(
            anchor="w", pady=(16, 4))
        self.list = ctk.CTkFrame(body, fg_color="transparent")
        self.list.pack(fill="x")
        self.app.machine_listeners.append(self._render_list)
        self._render_list()
        self._tab()

    def _tab(self):
        for w in self.area.winfo_children():
            w.destroy()
        if self.tabs.get().startswith("Paste"):
            self._paste_tab()
        else:
            self._usb_tab()

    def _paste_tab(self):
        try:
            link = self.app.ensure_link()
        except Exception as e:  # noqa: BLE001
            self.result_card(self.area, "warn", "danger", "Couldn't start the link", [str(e)])
            return
        steps = ctk.CTkFrame(self.area, fg_color="transparent")
        steps.pack(fill="x")
        for i, t in enumerate(["Remote into the other PC, or open your RMM's PowerShell shell on it",
                               "Open PowerShell as administrator",
                               "Paste the command below and press Enter — the PC appears in a few seconds"], 1):
            row = ctk.CTkFrame(steps, fg_color="transparent")
            row.pack(anchor="w", pady=2)
            ctk.CTkLabel(row, text=str(i), width=26, height=26, corner_radius=13, fg_color=P["accent"],
                         text_color="#ffffff", font=theme.font(12, "bold")).pack(side="left", padx=(0, 10))
            ctk.CTkLabel(row, text=t, font=theme.font(13), text_color=P["text"]).pack(side="left")
        bar = ctk.CTkFrame(self.area, fg_color="transparent")
        bar.pack(fill="x", pady=(12, 4))
        ctk.CTkLabel(bar, text="This PC's address:", font=theme.font(13, "bold"), text_color=P["text"]).pack(
            side="left")
        ips = link.addresses()
        self.ip = ctk.CTkOptionMenu(bar, values=ips, width=180, height=32, corner_radius=12, fg_color=P["card"],
                                    button_color=P["violet"], text_color=P["text"],
                                    command=lambda _v: self._fill_cmd())
        self.ip.set(ips[0])
        self.ip.pack(side="left", padx=8)
        Pill(bar, f"port {link.port}", "violet").pack(side="left")
        box = ctk.CTkFrame(self.area, corner_radius=16, fg_color=P["card"])
        box.pack(fill="x", pady=6)
        self.cmd = ctk.CTkTextbox(box, height=96, font=theme.mono(12), fg_color=P["card"], wrap="char",
                                  text_color=P["text"])
        self.cmd.pack(side="left", fill="both", expand=True, padx=(12, 6), pady=10)
        primary_button(box, "Copy", self._copy, width=110).pack(side="right", padx=12)
        self._fill_cmd()
        self.note(self.area, "Encrypted with a one-time certificate and token. Only this exact command can link, and "
                             "the link ends when you close SectorSmith. Windows may ask to allow SectorSmith through "
                             "the firewall on this PC — choose Allow." if link.exe_path else
                  "Running from source: copy the SectorSmith folder to the other PC first, then run this command "
                  "there from that folder.").pack(anchor="w", pady=(4, 0))

    def _fill_cmd(self):
        self.cmd.configure(state="normal")
        self.cmd.delete("1.0", "end")
        self.cmd.insert("1.0", self.app.link.command(self.ip.get()))
        self.cmd.configure(state="disabled")

    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append(self.app.link.command(self.ip.get()))
        self.app.toast("Command copied — paste it on the other PC")
        self.app.mascot.say(text="Copied! Now paste it on the other PC.")

    def _usb_tab(self):
        self.note(self.area, "Boot the other PC from a SectorSmith USB stick (see the guide). Its screen shows an "
                             "address and a pairing code — type them here.").pack(anchor="w")
        row = ctk.CTkFrame(self.area, fg_color="transparent")
        row.pack(anchor="w", pady=10)
        self.u_ip = ctk.CTkEntry(row, width=200, height=40, corner_radius=12, placeholder_text="192.168.1.50",
                                 font=theme.font(14))
        self.u_ip.pack(side="left")
        self.u_code = ctk.CTkEntry(row, width=170, height=40, corner_radius=12, placeholder_text="ABCD-1234",
                                   font=theme.font(14, "bold"))
        self.u_code.pack(side="left", padx=10)
        primary_button(row, "Connect", self._usb_connect, width=130).pack(side="left")
        self.u_status = ctk.CTkLabel(self.area, text="", font=theme.font(12), text_color=P["muted"])
        self.u_status.pack(anchor="w")

    def _usb_connect(self):
        ip, code = self.u_ip.get().strip(), self.u_code.get().strip()
        if not ip or not code:
            self.app.toast("Enter the address and code shown on the other PC.", "warn")
            return
        link = self.app.ensure_link()
        self.u_status.configure(text=f"Connecting to {ip}…")

        def ok(ep):
            self.u_status.configure(text=f"Linked. Check code on both screens: {ep.check}")

        def bad(e):
            self.u_status.configure(text=f"Couldn't connect: {e}", text_color=P["danger"])
        self.app.background(lambda: link.connect_to_waiting(ip, code), ok, bad)

    def _render_list(self):
        if not self.list.winfo_exists():
            raise tk.TclError("gone")
        for w in self.list.winfo_children():
            w.destroy()
        remote = [m for m in self.app.machines() if not m.is_local]
        if not remote:
            ctk.CTkLabel(self.list, text="Nothing linked yet — waiting for the other PC…", font=theme.font(13),
                         text_color=P["muted"]).pack(anchor="w")
            return
        for m in remote:
            info = m.info_cache
            card = ctk.CTkFrame(self.list, corner_radius=16, fg_color=P["card"])
            card.pack(fill="x", pady=3)
            b = IconBadge(card, "pc", 42, "success")
            b.set_bg(theme.c("card"))
            b.pack(side="left", padx=12, pady=10)
            t = ctk.CTkFrame(card, fg_color="transparent")
            t.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(t, text=m.label, font=theme.font(15, "bold"), text_color=P["text"]).pack(anchor="w")
            ctk.CTkLabel(t, text=f"{info.get('os', '')} · {m.address} · signed in as {info.get('user', '?')}"
                                 f"{' · admin' if info.get('admin') else ' · NOT admin'}", font=theme.font(12),
                         text_color=P["muted"]).pack(anchor="w")
            ghost_button(card, "Disconnect", lambda m=m: (m.close(), self.app._machine_event(m, False)),
                         width=120).pack(side="right", padx=12)
        self.buttons(primary=("Move a user to a new PC", lambda: self.app.go(MigrateWizard)),
                     secondary=("Clone a disk", lambda: self.app.go(NetCloneWizard)))


# ---------------------------------------------------------------------------
class MigrateWizard(Screen):
    guide_topic = "migrate"

    def __init__(self, master, app):
        super().__init__(master, app, "Move a user to a new PC", steps=["From", "To", "What", "Copy"])
        self.src = self.dst = None
        self.src_root = self.dst_root = None
        self._from()

    # -- step 1 ---------------------------------------------------------------
    def _profile_list(self, parent, ep, on_pick, allow_custom=False):
        holder = ctk.CTkFrame(parent, fg_color="transparent")
        holder.pack(fill="both", expand=True, pady=(12, 0))
        lbl = _loading(holder)

        def show(profs):
            lbl.destroy()
            ctk.CTkLabel(holder, text=f"USER PROFILES ON {ep.label.upper()}", font=theme.font(11, "bold"),
                         text_color=P["muted"]).pack(anchor="w")
            lst = ctk.CTkScrollableFrame(holder, fg_color="transparent", height=260)
            lst.pack(fill="both", expand=True)
            rows = []
            for pr in profs:
                card = ctk.CTkFrame(lst, corner_radius=14, fg_color=P["card"], border_width=2, border_color=P["card"])
                card.pack(fill="x", pady=3, padx=(0, 6))
                b = IconBadge(card, "migrate", 36, "accent")
                b.set_bg(theme.c("card"))
                b.pack(side="left", padx=10, pady=8)
                ctk.CTkLabel(card, text=pr["name"], font=theme.font(14, "bold"), text_color=P["text"]).pack(
                    side="left")
                ctk.CTkLabel(card, text=pr["path"], font=theme.font(12), text_color=P["muted"]).pack(side="left",
                                                                                                    padx=12)
                rows.append((card, b))

                def click(_e=None, card=card, b=b, pr=pr):
                    for c2, b2 in rows:
                        c2.configure(border_color=P["card"], fg_color=P["card"])
                        b2.set_bg(theme.c("card"))
                    card.configure(border_color=P["accent"], fg_color=P["accent_soft"])
                    b.set_bg(theme.c("accent_soft"))
                    on_pick(pr["path"])
                card.click = click
                bind_all(card, "<Button-1>", click)
            if not profs:
                ctk.CTkLabel(lst, text="No user profiles found.", text_color=P["muted"]).pack(pady=10)
            if allow_custom:
                row = ctk.CTkFrame(holder, fg_color="transparent")
                row.pack(fill="x", pady=(8, 0))
                ctk.CTkLabel(row, text="…or a folder on that PC:", font=theme.font(12, "bold"),
                             text_color=P["text"]).pack(side="left")
                ent = ctk.CTkEntry(row, width=360, height=34, corner_radius=12,
                                   placeholder_text=r"C:\Users\alice  (user hasn't signed in yet? sign in once first)")
                ent.pack(side="left", padx=8)
                ghost_button(row, "Use this folder", lambda: on_pick(ent.get().strip()) if ent.get().strip() else None,
                             width=150).pack(side="left")
            self.profile_cards = rows
        self.app.background(lambda: ep.list_profiles(), show, lambda e: lbl.configure(text=f"Error: {e}"))

    def _from(self):
        body = self.step(0, "Move a user to a new PC", "Which PC and which user are we moving from?")
        self.app.mascot.set_mood("idle", text="Moving day! Who are we moving?")
        nb = None
        area = ctk.CTkFrame(body, fg_color="transparent")

        def pick_machine(m):
            self.src = m
            self.src_root = None
            nb.configure(state="disabled")
            for w in area.winfo_children():
                w.destroy()
            self._profile_list(area, m, pick_profile)

        def pick_profile(path):
            self.src_root = path
            nb.configure(state="normal")
        self.mpick = MachinePicker(body, self.app, pick_machine)
        self.mpick.pack(anchor="w")
        area.pack(fill="both", expand=True)
        if len(self.app.machines()) == 1:
            self.note(area, "Tip: link the new PC first (+ Link another PC), then choose it on the next step.",
                      "violet").pack(anchor="w", pady=10)
        nb = self.buttons(primary=("Next", self._to))
        nb.configure(state="disabled")

    # -- step 2 ---------------------------------------------------------------
    def _to(self):
        body = self.step(1, "Where to?", f"From {self.src.label}: {self.src_root}")
        area = ctk.CTkFrame(body, fg_color="transparent")
        nb = None

        def pick_machine(m):
            self.dst = m
            self.dst_root = None
            nb.configure(state="disabled")
            for w in area.winfo_children():
                w.destroy()
            self._profile_list(area, m, pick_profile, allow_custom=True)

        def pick_profile(path):
            if self.dst is self.src and os.path.normcase(path) == os.path.normcase(self.src_root):
                self.app.toast("That's the same profile you're copying from.", "warn")
                return
            self.dst_root = path
            nb.configure(state="normal")
            self.app.toast(f"Destination: {path}")
        self.mpick2 = MachinePicker(body, self.app, pick_machine)
        self.mpick2.pack(anchor="w")
        area.pack(fill="both", expand=True)
        nb = self.buttons(primary=("Next", self._what), secondary=("Back", self._from))
        nb.configure(state="disabled")

    # -- step 3 ---------------------------------------------------------------
    def _what(self):
        body = self.step(2, "What should come across?", f"{self.src.label} → {self.dst.label}  ·  "
                                                        f"{os.path.basename(self.src_root.rstrip(chr(92) + '/'))}")
        self.items = list(migrate.ITEMS)
        self.vars = {}
        self.size_lbls = {}
        lst = ctk.CTkScrollableFrame(body, fg_color="transparent", height=330)
        lst.pack(fill="both", expand=True)
        self._items_frame = lst
        self._draw_items()
        opts = ctk.CTkFrame(body, fg_color="transparent")
        opts.pack(fill="x", pady=(8, 0))
        self.only_changed = tk.BooleanVar(value=True)
        ctk.CTkSwitch(opts, text="Only copy new or changed files (safe to run again later)",
                      variable=self.only_changed, progress_color=P["accent"], font=theme.font(12)).pack(side="left")
        self.total_lbl = ctk.CTkLabel(opts, text="Measuring…", font=theme.font(13, "bold"), text_color=P["text"])
        self.total_lbl.pack(side="right")
        self.buttons(primary=("Start moving", self._go), secondary=("Back", self._to))
        src, root = self.src, self.src_root

        def measure():
            extra = migrate.onedrive_items(src, root)
            return extra, migrate.measure(src, root, self.items + extra)

        def show(res):
            extra, sizes = res
            self.sizes = sizes
            if extra:
                self.items += extra
                self._draw_items()
            for it in self.items:
                b, n = sizes.get(it.key, (0, 0))
                if it.key in self.size_lbls:
                    self.size_lbls[it.key].configure(text=_files(b, n))
            self._update_total()
        self.sizes = {}
        self.app.background(measure, show)

    def _draw_items(self):
        for w in self._items_frame.winfo_children():
            w.destroy()
        group = None
        for it in self.items:
            if it.group != group:
                group = it.group
                ctk.CTkLabel(self._items_frame, text=group.upper(), font=theme.font(11, "bold"),
                             text_color=P["muted"]).pack(anchor="w", pady=(8, 2))
            if it.key not in self.vars:
                self.vars[it.key] = tk.BooleanVar(value=it.default)
            row = ctk.CTkFrame(self._items_frame, corner_radius=12, fg_color=P["card"])
            row.pack(fill="x", pady=2, padx=(0, 6))
            ctk.CTkCheckBox(row, text=it.label, variable=self.vars[it.key], font=theme.font(13, "bold"),
                            fg_color=P["accent"], hover_color=P["accent_hover"], corner_radius=6,
                            command=self._update_total).pack(side="left", padx=12, pady=8)
            if it.note:
                ctk.CTkLabel(row, text=it.note, font=theme.font(11), text_color=P["muted"]).pack(side="left")
            b, n = self.sizes.get(it.key, (None, None)) if hasattr(self, "sizes") else (None, None)
            lbl = ctk.CTkLabel(row, text="…" if b is None else _files(b, n),
                               font=theme.font(12, "bold"), text_color=P["text"])
            lbl.pack(side="right", padx=12)
            self.size_lbls[it.key] = lbl

    def _update_total(self):
        if not getattr(self, "sizes", None):
            return
        b = sum(self.sizes.get(it.key, (0, 0))[0] for it in self.items if self.vars[it.key].get())
        n = sum(self.sizes.get(it.key, (0, 0))[1] for it in self.items if self.vars[it.key].get())
        self.total_lbl.configure(text=f"Selected: {human_size(b)} · {n:,} files")

    # -- step 4 ---------------------------------------------------------------
    def _go(self, again=False):
        chosen = [it for it in self.items if self.vars[it.key].get()]
        if not chosen:
            self.app.toast("Pick at least one thing to copy.", "warn")
            return
        plan = migrate.Plan(self.src, self.src_root, self.dst, self.dst_root, chosen, self.only_changed.get()
                            if not again else True)
        self._plan = plan
        _, panel = self.progress("Moving files…", f"{self.src.label} → {self.dst.label}")
        self._draw_steps(3)
        self.app.run_job("Migration", lambda prog: migrate.run(plan, prog), self._done, panel,
                         on_cancel=self.app.home)

    def _done(self, res):
        ok = not res["failed"]
        body = self.step(3, "Move complete" if ok else "Moved, with a few problems", "")
        self.app.mascot.set_mood("happy" if ok else "sad", "done" if ok else "error")
        lines = [f"{res['copied']:,} files copied ({human_size(res['bytes'])}) in {human_time(res['seconds'])}",
                 f"{res['skipped_unchanged']:,} already up to date"]
        if res["failed"]:
            lines.append(f"{len(res['failed'])} couldn't be copied — often files open on the old PC. "
                         f"Close apps / sign the user out, then Run again.")
        self.result_card(body, "check" if ok else "warn", "success" if ok else "warn",
                         f"{os.path.basename(self.src_root.rstrip(chr(92) + '/'))} is on {self.dst.label}", lines)
        self.note(body, "Run again any time before the switch-over: only new or changed files are copied.",
                  "violet").pack(anchor="w")
        rep = res["report"]
        self.buttons(primary=("Back to home", self.app.home), secondary=("Run again", lambda: self._go(True)),
                     extra=("Open report", lambda: self.app.open_folder(rep)))


# ---------------------------------------------------------------------------
class NetCloneWizard(Screen):
    guide_topic = "netclone"

    def __init__(self, master, app):
        super().__init__(master, app, "Clone a disk to another PC", steps=["Source", "Destination", "Confirm", "Clone"])
        self._source()

    def _disk_step(self, idx, title, sub, writes, on_done, exclude=None):
        body = self.step(idx, title, sub)
        area = ctk.CTkFrame(body, fg_color="transparent")
        nb = None
        chosen = {}

        self.disk_picker = None

        def pick_machine(m):
            chosen.clear()
            chosen["m"] = m
            nb.configure(state="disabled")
            for w in area.winfo_children():
                w.destroy()
            lbl = _loading(area)

            def show(disks):
                lbl.destroy()
                ex = exclude[1] if exclude and exclude[0] is m else None

                def pick_disk(d):
                    chosen["d"] = d
                    nb.configure(state="normal")
                self.disk_picker = EndpointDiskPicker(area, disks, pick_disk, writes=writes, exclude_path=ex,
                                                      height=330)
                self.disk_picker.pack(fill="both", expand=True, pady=(10, 0))
            self.app.background(lambda: m.list_disks(), show, lambda e: lbl.configure(text=f"Error: {e}"))
        MachinePicker(body, self.app, pick_machine).pack(anchor="w")
        area.pack(fill="both", expand=True)
        nb = self.buttons(primary=("Next", lambda: on_done(chosen["m"], chosen["d"])))
        nb.configure(state="disabled")
        return body

    def _source(self):
        self.app.mascot.set_mood("idle", text="Which disk are we copying?")
        self._disk_step(0, "Clone a disk to another PC", "Pick the machine and disk to copy from.", False,
                        self._picked_source)

    def _picked_source(self, m, d):
        self.src, self.src_disk = m, d
        body = self._disk_step(1, "Copy it onto…", f"From {m.label}: {d['name']} · {human_size(d['size'])}", True,
                               self._picked_dest, exclude=(m, d["path"]))
        self.note(body, "Cloning onto a PC's own Windows disk? Boot that PC from the SectorSmith USB stick, then "
                        "link it with 'PC booted from USB'. The disk will then be selectable here.", "violet").pack(
            anchor="w", pady=(8, 0))

    def _picked_dest(self, m, d):
        self.dst, self.dst_disk = m, d
        need = self.src_disk["usable"]
        body = self.step(2, "Last check", "Everything on the destination disk will be replaced.")
        if d["usable"] < need:
            self.result_card(body, "warn", "danger", "Destination is too small",
                             [f"Need {human_size(need)}, it has {human_size(d['usable'])}."])
            self.buttons(primary=("Back", self._source))
            return
        self.app.mascot.set_mood("warn", "confirm")
        self.result_card(body, "clone", "danger", f"{self.src.label} → {m.label}",
                         [f"Copy {self.src_disk['name']} ({human_size(need)}) onto {d['name']} · {d['model']}",
                          "Every sector is copied; empty areas are skipped on the wire and the copy is verified.",
                          "If the destination PC has different hardware, Windows may need drivers on first boot."])
        phrase = f"CLONE TO {m.label.upper()}"
        go = self.buttons(primary=("Start cloning", self._go))
        go.configure(state="disabled", fg_color=P["danger"])
        self.confirm_box(body, phrase, lambda ok: go.configure(state="normal" if ok else "disabled"))

    def _go(self):
        src, dst, sp, dp = self.src, self.dst, self.src_disk["path"], self.dst_disk["path"]
        _, panel = self.progress("Cloning over the network…", f"{src.label} → {dst.label}")
        self._draw_steps(3)

        def done(res):
            ok = res["verify_mismatches"] == 0 and res["bad_sectors"] == 0
            body = self.step(3, "Clone complete" if ok else "Clone finished with problems", "")
            self.app.mascot.set_mood("happy" if ok else "sad", "done" if ok else "sick")
            self.result_card(body, "check" if ok else "warn", "success" if ok else "warn",
                             f"{human_size(res['bytes'])} cloned to {dst.label}",
                             [f"Sent {human_size(res['sent_bytes'])} over the network "
                              f"({res['sent_bytes'] / max(1, res['bytes']):.0%} of the disk)",
                              f"Verified: {'every block matched' if not res['verify_mismatches'] else str(res['verify_mismatches']) + ' block(s) differ'}",
                              f"Unreadable source sectors: {res['bad_sectors']}",
                              f"Took {human_time(res['seconds'])}"])
            self.buttons(primary=("Back to home", self.app.home))
        self.app.run_job("Network clone", lambda prog: netclone.clone(src, sp, dst, dp, prog), done, panel,
                         on_cancel=self.app.home)
