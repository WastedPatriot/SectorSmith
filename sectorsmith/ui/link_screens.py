"""Screens for SectorSmith Link: connect a machine, move a user to a new PC, clone disks between PCs."""
from __future__ import annotations

import os
import sys
import tkinter as tk

import customtkinter as ctk

from .. import offline
from ..link import apps, migrate, netclone
from ..util import get_logger, human_size, human_time
from . import theme
from .screens import Screen
from .widgets import IconBadge, Pill, bind_all, ghost_button, primary_button

P = theme.PALETTE
log = get_logger()


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
    def __init__(self, master, disks, on_select, writes=False, exclude_path=None, height=300, multi=False,
                 preselected=()):
        super().__init__(master, fg_color="transparent", height=height)
        self.rows = []
        self.multi = multi
        self.sel = set(preselected)
        if not disks:
            ctk.CTkLabel(self, text="No disks visible on this machine (is it running as administrator?)",
                         font=theme.font(13), text_color=P["muted"]).pack(pady=20)
        if writes:
            disks = sorted(disks, key=lambda d: not d.get("removable"))
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
            elif d.get("removable"):
                Pill(top, "USB", "success").pack(side="left", padx=6)
            parts = ", ".join(f"{(p['mount'][0].rstrip(chr(92)) + ' ') if p['mount'] else ''}{p['fs'] or '?'}"
                              for p in d["partitions"][:5])
            sub = "Protected: boot this PC from the SectorSmith USB to clone onto it" if locked else \
                f"{d['bus']} · {d['scheme']} · {parts or 'no partitions'}"
            ctk.CTkLabel(card, text=sub, font=theme.font(12), text_color=P["warn"] if locked else P["muted"]).grid(
                row=1, column=1, sticky="w", pady=(0, 8))
            ctk.CTkLabel(card, text=human_size(d["size"]), font=theme.font(14, "bold"), text_color=P["text"]).grid(
                row=0, column=2, rowspan=2, padx=14)
            card.grid_columnconfigure(1, weight=1)
            if locked or d.get("error"):
                continue
            self.rows.append((card, b, d))

            def paint(card, b, on):
                card.configure(border_color=P["accent"] if on else P["card"],
                               fg_color=P["accent_soft"] if on else P["card"])
                b.set_bg(theme.c("accent_soft") if on else theme.c("card"))

            def click(_e=None, card=card, b=b, d=d):
                if self.multi:
                    on = d["path"] not in self.sel
                    (self.sel.add if on else self.sel.discard)(d["path"])
                    paint(card, b, on)
                    on_select(d, on)
                    return
                for c2, b2, _d in self.rows:
                    paint(c2, b2, False)
                paint(card, b, True)
                on_select(d)
            card.click = click
            bind_all(card, "<Button-1>", click)
            if d["path"] in self.sel:
                paint(card, b, True)


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
                               "Paste the command below and press Enter. The PC appears in a few seconds"], 1):
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
                             "the firewall on this PC: choose Allow." if link.exe_path else
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
        self.app.toast("Command copied. Paste it on the other PC.")
        self.app.mascot.say(text="Copied! Now paste it on the other PC.")

    def _usb_tab(self):
        self.note(self.area, "Boot the other PC from a SectorSmith USB stick (see the guide). Its screen shows an "
                             "address and a pairing code. Type them here.").pack(anchor="w")
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
            ctk.CTkLabel(self.list, text="Nothing linked yet. Waiting for the other PC…", font=theme.font(13),
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
        self.buttons(primary=("Migrate user", lambda: self.app.go(MigrateWizard)),
                     secondary=("Clone a disk", lambda: self.app.go(NetCloneWizard)))


# ---------------------------------------------------------------------------
class MigrateWizard(Screen):
    """Move a user: from this PC, a linked PC or an old disk attached by USB, to a linked PC, a profile, or a
    folder on any drive. Apps come across too (Deploy library or winget), the rest are listed."""
    guide_topic = "migrate"

    def __init__(self, master, app):
        super().__init__(master, app, "Migrate user", steps=["From", "To", "What", "Apps", "Copy"])
        self.src = self.dst = None
        self.src_root = self.dst_root = None
        self.src_offline = False      # old disk / folder: Windows there isn't running
        self.dst_kind = "profile"     # profile | offline | folder
        self.app_rows, self.app_store = [], None
        self._from()

    def _user(self):
        return os.path.basename(self.src_root.rstrip("\\/").replace("\\", "/")) if self.src_root else "user"

    # -- shared: pick a profile ----------------------------------------------------
    def _profile_list(self, parent, ep, on_pick, dest=False):
        holder = ctk.CTkFrame(parent, fg_color="transparent")
        holder.pack(fill="both", expand=True, pady=(12, 0))
        lbl = _loading(holder)
        self.profile_cards = None
        self.offline_cards = []
        rows = []

        def row(lst, pr, kind):
            card = ctk.CTkFrame(lst, corner_radius=14, fg_color=P["card"], border_width=2, border_color=P["card"])
            card.pack(fill="x", pady=3, padx=(0, 6))
            b = IconBadge(card, "migrate" if kind == "profile" else "drive", 36,
                          "accent" if kind == "profile" else "violet")
            b.set_bg(theme.c("card"))
            b.pack(side="left", padx=10, pady=8)
            ctk.CTkLabel(card, text=pr["name"], font=theme.font(14, "bold"), text_color=P["text"]).pack(side="left")
            sub = pr["path"] if kind == "profile" else f"{offline.describe(pr['path'])}  ·  {pr.get('where', '')}"
            ctk.CTkLabel(card, text=sub, font=theme.font(12), text_color=P["muted"]).pack(side="left", padx=12)
            rows.append((card, b))

            def click(_e=None, card=card, b=b, pr=pr):
                for c2, b2 in rows:
                    if c2.winfo_exists():
                        c2.configure(border_color=P["card"], fg_color=P["card"])
                        b2.set_bg(theme.c("card"))
                card.configure(border_color=P["accent"], fg_color=P["accent_soft"])
                b.set_bg(theme.c("accent_soft"))
                on_pick(pr["path"], kind)
            card.click = click
            bind_all(card, "<Button-1>", click)
            return card, b

        def show(res):
            profs, others = res
            lbl.destroy()
            lst = ctk.CTkScrollableFrame(holder, fg_color="transparent", height=200 if dest else 250)
            lst.pack(fill="both", expand=True)
            ctk.CTkLabel(lst, text=f"USER PROFILES ON {ep.label.upper()}", font=theme.font(11, "bold"),
                         text_color=P["muted"]).pack(anchor="w")
            cards = [row(lst, pr, "profile") for pr in profs]
            if not profs:
                ctk.CTkLabel(lst, text="No user profiles found.", text_color=P["muted"]).pack(anchor="w", pady=6)
            head = ctk.CTkFrame(lst, fg_color="transparent")
            head.pack(fill="x", pady=(10, 0))
            ctk.CTkLabel(head, text="ON OTHER DRIVES", font=theme.font(11, "bold"), text_color=P["muted"]).pack(
                side="left")
            oth = ctk.CTkFrame(lst, fg_color="transparent")
            oth.pack(fill="x")

            def render_others(items, searching_raw=False):
                for w in oth.winfo_children():
                    w.destroy()
                self.offline_cards = [row(oth, pr, "offline") for pr in items]
                if not items:
                    self.note(oth, "None found. Old disk or NVMe? Connect it to this PC by USB (a caddy or adapter "
                                   "works), wait for its drive letter, then press Look again."
                              if not dest else "None found.").pack(anchor="w", pady=4)

            def look(raw=False):
                for w in oth.winfo_children():
                    w.destroy()
                _loading(oth, "Reading NTFS partitions without a drive letter (can take a minute)..." if raw
                         else "Looking at the other drives...")

                def got(items):
                    if oth.winfo_exists():
                        render_others(self._dest_ok(items) if dest else items)
                self.app.background(lambda: ep.offline_profiles(raw=raw), got,
                                    lambda e: oth.winfo_exists() and render_others([]))
            render_others(others)
            ghost_button(head, "Look again", lambda: look(False), width=110).pack(side="right")
            if not dest:
                ghost_button(head, "Drives without a letter", lambda: look(True), width=190).pack(side="right",
                                                                                                padx=6)
            self.profile_cards = cards
            if dest:
                self._new_folder_row(holder, ep, profs, on_pick)
            else:
                self._custom_source_row(holder, ep, profs, on_pick)

        def load():
            profs = ep.list_profiles()
            try:
                others = ep.offline_profiles()
            except Exception as e:  # noqa: BLE001 (an older agent, or no drives to look at)
                log.info("offline_profiles on %s: %s", ep.label, e)
                others = []
            return profs, (self._dest_ok(others) if dest else others)
        self.app.background(load, show, lambda e: lbl.configure(text=f"Error: {e}"))

    @staticmethod
    def _dest_ok(items):
        return [i for i in items if not offline.is_raw(i["path"])]  # raw partitions are read-only

    def _custom_source_row(self, holder, ep, profs, on_pick):
        box = ctk.CTkFrame(holder, corner_radius=14, fg_color=P["card"])
        box.pack(fill="x", pady=(10, 0), padx=(0, 6))
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=10)
        ctk.CTkLabel(row, text="Or a folder:", font=theme.font(13, "bold"), text_color=P["text"]).pack(side="left")
        ent = ctk.CTkEntry(row, height=34, corner_radius=12, placeholder_text="e.g. E:\\Users\\alice")
        ent.pack(side="left", fill="x", expand=True, padx=8)
        self.src_folder_entry = ent
        known = {os.path.normcase(p["path"]) for p in profs}

        def use():
            path = ent.get().strip()
            if not path:
                return
            path = path if len(path) <= 3 else path.rstrip("\\/")

            def done(ok):
                if not ok:
                    self.app.toast(f"{path} isn't there on {ep.label}.", "warn")
                    return
                on_pick(path, "profile" if os.path.normcase(path) in known else "offline")
                self.app.toast(f"Source: {path}")
            self.app.background(lambda: ep.path_exists(path=path), done)
        self.src_folder_btn = ghost_button(row, "Use", use, width=90)
        self.src_folder_btn.pack(side="left")

    def _new_folder_row(self, holder, ep, profs, on_pick):
        """'New folder' on the destination: a profile folder, or a folder on any drive (USB disk, NVMe...)."""
        info = getattr(ep, "info_cache", None) or {"os": sys.platform}
        win = str(info.get("os", "")).lower().startswith("win")
        sep = "\\" if (profs and "\\" in profs[0]["path"]) or win else "/"
        if profs:
            parent = profs[0]["path"].rstrip("\\/").rsplit(sep, 1)[0]
        else:
            parent = "C:\\Users" if sep == "\\" else "/home"
        user = self._user()
        box = ctk.CTkFrame(holder, corner_radius=14, fg_color=P["card"])
        box.pack(fill="x", pady=(10, 0), padx=(0, 6))
        top = ctk.CTkFrame(box, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(top, text="+ New folder", font=theme.font(14, "bold"), text_color=P["text"]).pack(side="left")
        ctk.CTkLabel(top, text="on any drive, or type an existing folder", font=theme.font(11),
                     text_color=P["muted"]).pack(side="left", padx=10)
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(fill="x", padx=12)
        ent = ctk.CTkEntry(row, height=34, corner_radius=12)
        ent.insert(0, f"{parent}{sep}{user}")
        ent.pack(side="left", fill="x", expand=True)
        self.new_folder_entry = ent
        drives = ctk.CTkFrame(box, fg_color="transparent")
        drives.pack(fill="x", padx=12, pady=(6, 0))
        self.drive_buttons = []

        def create():
            path = ent.get().strip()
            # keep "E:\" a root: "E:" alone would mean "the current folder on E:"
            path = path if len(path) <= 3 and path[1:2] == ":" else path.rstrip("\\/")
            if len(path) == 2 and path[1] == ":":
                path += "\\"
            if not path:
                return

            def done(r):
                self.app.toast(("Using existing folder " if r["existed"] else "Created ") + r["path"])
                # a (new) folder next to the other profiles is that user's profile on this PC
                up = r["path"].rstrip("\\/").rsplit(sep, 1)[0]
                on_pick(r["path"], "profile" if os.path.normcase(up) == os.path.normcase(parent) else "folder")
            self.app.background(lambda: ep.make_folder(path=path), done,
                                lambda e: self.app.toast(f"Couldn't create that folder: {e}", "danger"))
        self.new_folder_btn = ghost_button(row, "Create & use", create, width=140)
        self.new_folder_btn.pack(side="left", padx=(8, 0))

        def show_drives(vols):
            if not drives.winfo_exists():
                return
            vols = [v for v in vols if not v.get("system")]
            if not vols:
                return
            ctk.CTkLabel(drives, text="Drives:", font=theme.font(12, "bold"), text_color=P["muted"]).pack(side="left")
            for v in vols[:4]:
                vsep = "\\" if "\\" in v["path"] else "/"
                dest_path = v["path"].rstrip("\\/") + vsep + "SectorSmith" + vsep + user

                def pick(dp=dest_path):
                    ent.delete(0, "end")
                    ent.insert(0, dp)
                    create()
                name = f"{v['path'].rstrip(chr(92))} {v.get('label') or ''}".strip()
                name = name if len(name) <= 24 else name[:23] + "…"
                btn = ctk.CTkButton(drives, text=f"{name} · {human_size(v['free'])} free", height=28, corner_radius=14,
                                    fg_color=P["violet_soft"], hover_color=P["card_hover"], text_color=P["violet"],
                                    font=theme.font(12, "bold"), command=pick)
                btn.pack(side="left", padx=4)
                self.drive_buttons.append(btn)
        self.app.background(lambda: ep.list_volumes(), show_drives, lambda e: None)
        ctk.CTkLabel(box, justify="left", wraplength=640, font=theme.font(11), text_color=P["muted"],
                     text=("A folder on a drive (USB disk, NVMe in a caddy) keeps a copy you can move in later. For a "
                           "profile on this PC: if " + user + " has never signed in here, Windows makes its own "
                           "folder at first sign-in (e.g. " + user + ".DOMAIN) and won't use this one, so sign in "
                           "once first and pick the profile above.")
                     ).pack(anchor="w", padx=12, pady=(6, 10))

    # -- step 1 ---------------------------------------------------------------
    def _from(self):
        body = self.step(0, "Migrate user", "Which PC (or old disk) and which user are we moving from?")
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

        def pick_profile(path, kind):
            self.src_root = path
            self.src_offline = kind != "profile"
            nb.configure(state="normal")
        self.mpick = MachinePicker(body, self.app, pick_machine)
        self.mpick.pack(anchor="w")
        area.pack(fill="both", expand=True)
        self.note(area, "Old PC won't start? Put its disk or NVMe in a USB caddy, plug it into this PC (or the new "
                        "one), pick that PC, then the user under 'On other drives'. "
                  + ("Tip: link the new PC first (+ Link another PC)." if len(self.app.machines()) == 1 else ""),
                  "violet").pack(anchor="w", pady=10)
        nb = self.buttons(primary=("Next", self._to))
        nb.configure(state="disabled")

    # -- step 2 ---------------------------------------------------------------
    def _to(self):
        body = self.step(1, "Where to?", f"From {self.src.label}: {offline.describe(self.src_root)}")
        area = ctk.CTkFrame(body, fg_color="transparent")
        nb = None

        def pick_machine(m):
            self.dst = m
            self.dst_root = None
            nb.configure(state="disabled")
            for w in area.winfo_children():
                w.destroy()
            self._profile_list(area, m, pick_profile, dest=True)

        def pick_profile(path, kind):
            if self.dst is self.src and os.path.normcase(path) == os.path.normcase(self.src_root):
                self.app.toast("That's the same profile you're copying from.", "warn")
                return
            self.dst_root = path
            self.dst_kind = kind
            nb.configure(state="normal")
            self.app.toast(f"Destination: {path}")
        self.mpick2 = MachinePicker(body, self.app, pick_machine)
        self.mpick2.pack(anchor="w")
        area.pack(fill="both", expand=True)
        nb = self.buttons(primary=("Next", self._what), secondary=("Back", self._from))
        nb.configure(state="disabled")

    # -- step 3 ---------------------------------------------------------------
    def _what(self):
        body = self.step(2, "What should come across?", f"{self.src.label} → {self.dst.label}  ·  {self._user()}")
        self.items = list(migrate.ITEMS)
        self.vars = {}
        self.size_lbls = {}
        self.free = None
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
        self.buttons(primary=("Next", self._apps), secondary=("Back", self._to))
        src, root, dst, droot = self.src, self.src_root, self.dst, self.dst_root

        def measure():
            extra = migrate.onedrive_items(src, root)
            try:
                free = dst.free_space(path=droot)
            except Exception:  # noqa: BLE001
                free = None
            return extra, migrate.measure(src, root, self.items + extra), free

        def show(res):
            extra, sizes, free = res
            self.sizes, self.free = sizes, free
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
        text = f"Selected: {human_size(b)} · {n:,} files"
        short = self.free is not None and b > self.free
        if self.free is not None:
            text += f" · {human_size(self.free)} free there" + ("  (NOT ENOUGH SPACE)" if short else "")
        self.total_lbl.configure(text=text, text_color=P["danger"] if short else P["text"])

    # -- step 4: apps ---------------------------------------------------------
    def _apps(self):
        chosen = [it for it in self.items if self.vars[it.key].get()]
        if not chosen:
            self.app.toast("Pick at least one thing to copy.", "warn")
            return
        self.chosen_items = chosen
        self.only_changed_v = self.only_changed.get()
        body = self.step(3, "Apps", f"What {self.src.label} has installed, and what can go onto {self.dst.label} "
                                    "by itself")
        self.app.mascot.set_mood("idle", text="Let's bring their apps too!")
        self.apps_ready = False
        self.app_vars = {}
        lst = ctk.CTkScrollableFrame(body, fg_color="transparent", height=270)
        lst.pack(fill="both", expand=True)
        lbl = _loading(lst, "Looking at the installed apps…")
        opts = ctk.CTkFrame(body, fg_color="transparent")
        opts.pack(fill="x", pady=(8, 0))
        can_install = self.dst_kind != "offline"
        self.install_apps = tk.BooleanVar(value=self.dst_kind == "profile")
        sw = ctk.CTkSwitch(opts, text=f"Install the ticked apps on {self.dst.label}", variable=self.install_apps,
                           progress_color=P["accent"], font=theme.font(12))
        sw.pack(anchor="w")
        self.apps_first = tk.BooleanVar(value=True)
        ctk.CTkSwitch(opts, text="Install apps before copying files, so the copied settings are used",
                      variable=self.apps_first, progress_color=P["accent"], font=theme.font(12)).pack(anchor="w",
                                                                                                     pady=(4, 0))
        if not can_install:
            sw.configure(state="disabled")
            self.note(opts, "The destination is a Windows that isn't running, so nothing can be installed there. "
                            "The app list is saved with the files.", "violet").pack(anchor="w", pady=(4, 0))
        elif self.dst_kind == "folder":
            self.note(opts, f"Copying to a folder, so apps are only installed on {self.dst.label} if you switch "
                            "that on. Otherwise the app list is saved with the files.", "violet").pack(
                anchor="w", pady=(4, 0))
        else:
            self.note(opts, "Installs run silently on the new PC with SectorSmith Deploy, then get checked. winget "
                            "apps need winget there (Windows 10 1809+ or 11).", "muted").pack(anchor="w",
                                                                                              pady=(4, 0))
        self.buttons(primary=("Start moving", self._go), secondary=("Back", self._what))
        src, sroot, dst, offline_src, want_dst = self.src, self.src_root, self.dst, self.src_offline, can_install
        app = self.app

        def load():
            if offline_src:
                inv = src.apps_on_disk(profile=sroot)
            else:
                inv = apps.clean_inventory(src.installed_software())
            dinv = None
            if want_dst:
                try:
                    dinv = dst.installed_software()
                except Exception as e:  # noqa: BLE001
                    log.info("installed_software on %s: %s", dst.label, e)
            try:
                store = app.deploy_store()
            except Exception as e:  # noqa: BLE001
                log.warning("Deploy library unreadable: %s", e)
                store = None
            return apps.match(inv, store, dinv), store

        def show(res):
            if not lst.winfo_exists():
                return
            self.app_rows, self.app_store = res
            lbl.destroy()
            self._draw_apps(lst)
            self.apps_ready = True

        def failed(e):
            if lst.winfo_exists():
                lbl.configure(text=f"Couldn't list the apps ({e}). The files can still be copied.")
            self.app_rows, self.app_store = [], None
            self.apps_ready = True
        self.app.background(load, show, failed)

    def _draw_apps(self, lst):
        rows = self.app_rows
        if not rows:
            ctk.CTkLabel(lst, text="No apps found.", text_color=P["muted"]).pack(pady=10)
            return
        groups = [("auto", "INSTALLED FOR YOU", [r for r in rows if r["source"] in ("library", "winget")]),
                  ("manual", "INSTALL THESE YOURSELF", [r for r in rows if r["source"] == "manual"]),
                  ("have", "ALREADY ON THE NEW PC", [r for r in rows if r["source"] == "installed"])]
        if self.src_offline:
            self.note(lst, "Found by their folders on the old disk, so versions aren't known.", "violet").pack(
                anchor="w", pady=(0, 4))
        for key, title, items in groups:
            if not items:
                continue
            ctk.CTkLabel(lst, text=f"{title}  ({len(items)})", font=theme.font(11, "bold"),
                         text_color=P["muted"]).pack(anchor="w", pady=(8, 2))
            if key == "have":  # nothing to do for these: one line is enough
                names = ", ".join(r["name"] for r in items[:40]) + (" and more" if len(items) > 40 else "")
                ctk.CTkLabel(lst, text=names, font=theme.font(12), text_color=P["muted"], wraplength=860,
                             justify="left", anchor="w").pack(anchor="w", padx=4)
                continue
            for r in items:
                row = ctk.CTkFrame(lst, corner_radius=12, fg_color=P["card"])
                row.pack(fill="x", pady=2, padx=(0, 6))
                text = f"{r['name']}  {r.get('version') or ''}".strip()
                if key == "auto":
                    v = tk.BooleanVar(value=True)
                    self.app_vars[id(r)] = v
                    ctk.CTkCheckBox(row, text=text, variable=v, font=theme.font(13, "bold"), fg_color=P["accent"],
                                    hover_color=P["accent_hover"], corner_radius=6).pack(side="left", padx=12, pady=7)
                else:
                    ctk.CTkLabel(row, text=text, font=theme.font(13, "bold"),
                                 text_color=P["text"] if key == "manual" else P["muted"]).pack(side="left", padx=14,
                                                                                              pady=7)
                ctk.CTkLabel(row, text=r["note"], font=theme.font(11),
                             text_color=P["violet"] if key == "auto" else P["muted"]).pack(side="right", padx=12)

    # -- step 5 ---------------------------------------------------------------
    def _go(self, again=False):
        if not getattr(self, "apps_ready", True):
            self.app.toast("Still looking at the apps, one moment.", "warn")
            return
        rows = []
        for r in self.app_rows:
            v = self.app_vars.get(id(r))
            rows.append(r if v is None or v.get() else dict(r, source="skipped", note="not chosen"))
        plan = migrate.Plan(self.src, self.src_root, self.dst, self.dst_root, self.chosen_items,
                            self.only_changed_v if not again else True, apps=rows, store=self.app_store,
                            apps_first=self.apps_first.get(),
                            install_apps=self.install_apps.get() and self.dst_kind != "offline")
        self._plan = plan
        _, panel = self.progress("Moving…", f"{self.src.label} → {self.dst.label}")
        self._draw_steps(4)
        self.app.run_job("Migration", lambda prog: migrate.run(plan, prog), self._done, panel,
                         on_cancel=self._stopped)

    def _app_lines(self, res):
        lines = []
        got = res.get("apps") or []
        if got:
            ok = [a for a in got if a["status"] == "compliant"]
            bad = [a for a in got if a["status"] != "compliant"]
            lines.append(f"Apps: {len(ok)} of {len(got)} installed and checked"
                         + (f". Didn't work: {', '.join(a['name'] for a in bad[:5])}" if bad else ""))
        manual = res.get("manual_apps") or []
        if manual:
            lines.append("Install yourself: " + ", ".join(manual[:8]) + (" and more" if len(manual) > 8 else ""))
        return lines

    def _done(self, res):
        ok = not res["failed"] and all(a["status"] == "compliant" for a in res.get("apps") or [])
        body = self.step(4, "Move complete" if ok else "Moved, with a few problems", "")
        self.app.mascot.set_mood("happy" if ok else "sad", "done" if ok else "error")
        lines = [f"{res['copied']:,} files copied ({human_size(res['bytes'])}) in {human_time(res['seconds'])}",
                 f"{res['skipped_unchanged']:,} already up to date"]
        if res["failed"]:
            lines.append(f"{len(res['failed'])} couldn't be copied.")
            lines += res.get("hints") or ["Usually files open on the old PC. Close apps or sign the user out, "
                                          "then Run again."]
        lines += self._app_lines(res)
        where = self.dst.label if self.dst_kind == "profile" else offline.describe(self.dst_root)
        self.result_card(body, "check" if ok else "warn", "success" if ok else "warn",
                         f"{self._user()} is on {where}", lines)
        self.note(body, "Run again any time before the switch-over: only new or changed files are copied.",
                  "violet").pack(anchor="w")
        rep = res["report"]
        self.buttons(primary=("Back to home", self.app.home), secondary=("Run again", lambda: self._go(True)),
                     extra=("Open report", lambda: self.app.open_folder(rep)))

    def _stopped(self, info):
        n, b = info.get("copied", 0), info.get("bytes", 0)
        lines = [f"{n:,} file(s) ({human_size(b)}) were copied before you pressed Cancel. They're kept."
                 if n else "No files were copied yet."]
        lines += self._app_lines(info)
        lines += ["Nothing was deleted on either side, and the file being copied is redone next time.",
                  "Run again to carry on: only what's missing is copied."]
        rep = info.get("report")
        self.cancelled_state("Move stopped", lines, step=4, secondary=("Run again", lambda: self._go(True)),
                             extra=("Open report", lambda: self.app.open_folder(rep)) if rep else None)


# ---------------------------------------------------------------------------
class NetCloneWizard(Screen):
    """Clone one disk to one or many disks, on this PC and/or linked PCs, any OS, full or used-space only."""
    guide_topic = "netclone"

    def __init__(self, master, app):
        super().__init__(master, app, "Network clone", steps=["Source", "Destinations", "Confirm", "Clone"])
        self.targets = {}  # (id(machine), path) -> (machine, disk)
        self._source()

    def _source(self):
        self.app.mascot.set_mood("idle", text="Which disk are we copying?")
        body = self.step(0, "Clone disks", "Pick the machine and the disk to copy from.")
        area = ctk.CTkFrame(body, fg_color="transparent")
        chosen = {}
        nb = None
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

                def pick_disk(d):
                    chosen["d"] = d
                    nb.configure(state="normal")
                self.disk_picker = EndpointDiskPicker(area, disks, pick_disk, height=330)
                self.disk_picker.pack(fill="both", expand=True, pady=(10, 0))
            self.app.background(lambda: m.list_disks(), show, lambda e: lbl.configure(text=f"Error: {e}"))
        MachinePicker(body, self.app, pick_machine).pack(anchor="w")
        area.pack(fill="both", expand=True)
        nb = self.buttons(primary=("Next", lambda: self._dests(chosen["m"], chosen["d"])))
        nb.configure(state="disabled")

    def _dests(self, m=None, d=None):
        if m is not None:
            self.src, self.src_disk = m, d
            self.targets = {}
        m, d = self.src, self.src_disk
        body = self.step(1, "Copy it onto…", f"From {m.label}: {d['name']} · {human_size(d['size'])}  ·  "
                                             "pick one or more disks, on any machine")
        area = ctk.CTkFrame(body, fg_color="transparent")
        summary = ctk.CTkLabel(body, text="", font=theme.font(13, "bold"), text_color=P["accent"], anchor="w")
        nb = None
        self.disk_picker = None

        def refresh_summary():
            n = len(self.targets)
            summary.configure(text=("Selected: " + ", ".join(f"{mm.label} · {dd['name']}"
                                                            for mm, dd in self.targets.values())) if n else
                              "Nothing selected yet. Click disks to select (you can pick several, on any PC).")
            nb.configure(state="normal" if n else "disabled",
                         text=f"Next ({n} disk{'s' if n != 1 else ''})" if n else "Next")

        def pick_machine(mm):
            for w in area.winfo_children():
                w.destroy()
            lbl = _loading(area)

            def show(disks):
                lbl.destroy()
                ex = d["path"] if mm is m else None
                pre = [p for (mid, p) in self.targets if mid == id(mm)]

                def toggle(dd, on):
                    key = (id(mm), dd["path"])
                    if on:
                        self.targets[key] = (mm, dd)
                    else:
                        self.targets.pop(key, None)
                    refresh_summary()
                self.disk_picker = EndpointDiskPicker(area, disks, toggle, writes=True, exclude_path=ex,
                                                      height=290, multi=True, preselected=pre)
                self.disk_picker.pack(fill="both", expand=True, pady=(10, 0))
            self.app.background(lambda: mm.list_disks(), show, lambda e: lbl.configure(text=f"Error: {e}"))
        self.dest_machines = MachinePicker(body, self.app, pick_machine)
        self.dest_machines.pack(anchor="w")
        area.pack(fill="both", expand=True)
        summary.pack(fill="x", pady=(6, 0))
        self.note(body, "USB-attached SSD/NVMe drives are marked USB. To overwrite a PC's own Windows disk, boot "
                        "that PC from the SectorSmith USB stick and link it with 'PC booted from USB'.",
                  "violet").pack(anchor="w", pady=(4, 0))
        nb = self.buttons(primary=("Next", self._confirm), secondary=("Back", self._source))
        refresh_summary()

    def _confirm(self):
        need = self.src_disk["usable"]
        body = self.step(2, "Last check", "Everything on the selected destination disks will be replaced.")
        small = [(m, d) for m, d in self.targets.values() if d["usable"] < need]
        if small:
            self.result_card(body, "warn", "danger", "Some destinations are too small",
                             [f"{m.label} · {d['name']}: {human_size(d['usable'])} (need {human_size(need)})"
                              for m, d in small])
            self.buttons(primary=("Back", self._dests))
            return
        self.app.mascot.set_mood("warn", "confirm")
        lines = [f"{m.label} · {d['name']} · {d['model'] or 'Disk'} · {human_size(d['size'])}"
                 + ("  (bigger, so extend C: afterwards)" if d["usable"] > need else "")
                 for m, d in self.targets.values()]
        if self.src_disk["is_system"] or any(p["mount"] for p in self.src_disk["partitions"]):
            lines.append("The source is in use, so a snapshot (VSS) is taken first so the copy is consistent.")
        n = len(self.targets)
        self.result_card(body, "clone", "danger",
                         f"{self.src.label} · {self.src_disk['name']}  →  {n} disk{'s' if n != 1 else ''}", lines)
        self.smart = tk.BooleanVar(value=True)
        ctk.CTkSwitch(body, text="Copy used space only, much faster. Works for NTFS, FAT, exFAT and ext; any other "
                                 "filesystem or OS is copied sector by sector.", variable=self.smart,
                      progress_color=P["accent"], font=theme.font(12)).pack(anchor="w", pady=(0, 4))
        only = next(iter(self.targets.values()))[0]
        phrase = f"CLONE TO {only.label.upper()}" if n == 1 else f"CLONE TO {n} DISKS"
        go = self.buttons(primary=("Start cloning", self._go), secondary=("Back", self._dests))
        go.configure(state="disabled", fg_color=P["danger"])
        self.confirm_box(body, phrase, lambda ok: go.configure(state="normal" if ok else "disabled"))

    def _go(self):
        src, sp = self.src, self.src_disk["path"]
        targets = [(m, d["path"]) for m, d in self.targets.values()]
        smart = self.smart.get()
        _, panel = self.progress("Cloning…", f"{src.label} → {len(targets)} disk(s)"
                                             + (" · used space only" if smart else ""))
        self._draw_steps(3)

        def done(res):
            ok = all(t["ok"] for t in res["targets"]) and res["bad_sectors"] == 0
            body = self.step(3, "Clone complete" if ok else "Clone finished with problems", "")
            self.app.mascot.set_mood("happy" if ok else "sad", "done" if ok else "sick")
            lines = [f"Copied {human_size(res['copied_bytes'])} of {human_size(res['bytes'])} "
                     f"({'used space only' if res['copied_bytes'] < res['bytes'] else 'every sector'}) "
                     f"in {human_time(res['seconds'])}",
                     f"Sent {human_size(res['sent_bytes'])} after skipping empty blocks and compressing",
                     f"Unreadable source sectors: {res['bad_sectors']}"]
            if res.get("snapshot"):
                lines.append(f"Copied from a snapshot of {', '.join(res['snapshot'])}")
            for t in res["targets"]:
                state = "✓ verified" if t["ok"] else f"✗ {t['error'] or str(t['verify_mismatches']) + ' block(s) differ'}"
                lines.append(f"{t['machine']} · {t['path']}: {state}" + (f" ({t['grown']})" if t.get("grown") else ""))
            self.result_card(body, "check" if ok else "warn", "success" if ok else "warn",
                             f"{len(res['targets'])} disk(s) cloned from {src.label}", lines)
            self.buttons(primary=("Back to home", self.app.home))
        def stopped(info):
            lines = []
            for t in info.get("targets") or [{"machine": m.label, "path": p, "state": "untouched"}
                                             for m, p in targets]:
                if t["state"] == "untouched":
                    lines.append(f"{t['machine']} · {t['path']}: nothing written, as it was")
                else:
                    lines.append(f"{t['machine']} · {t['path']}: only partly written "
                                 f"({human_size(t.get('written', 0))}). It won't start or open reliably: clone "
                                 "again, or wipe it before other use.")
            lines += ["Every disk was closed and any snapshot of the source removed. The source wasn't changed."]
            self.cancelled_state("Clone stopped", lines, step=3, secondary=("Clone again", self._confirm))
        self.app.run_job("Clone", lambda prog: netclone.clone_many(src, sp, targets, prog, smart=smart), done,
                         panel, on_cancel=stopped)
