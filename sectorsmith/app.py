"""SectorSmith main window (tkinter)."""
from __future__ import annotations

import dataclasses
import os
import queue
import threading
import time
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, ttk

from . import carver, ntfs, partitions, partscan, surface, wipe
from .device import Device, list_disks, mounted_partitions_linux, open_image, volume_letters_for
from .hexview import HexView
from .util import APP_NAME, APP_VERSION, Cancelled, Progress, app_dir, cancel_scope, get_logger, human_size, \
    human_time, is_admin

log = get_logger()

FS_COLORS = {"NTFS": "#4a86c5", "FAT32": "#58a55c", "FAT16": "#7fbf7f", "FAT12": "#9fd19f", "exFAT": "#3aa3a3",
             "ext4": "#d38a32", "ext3": "#dc9b50", "ext2": "#e3ad6e", "BitLocker": "#8b5cc4", "ReFS": "#5b6fc9",
             "Linux swap": "#a0a0a0", "LVM2 PV": "#b07a50", "APFS": "#999", "HFS+": "#999"}
SURF_COLORS = ["#2e9e44", "#8bc34a", "#ffd54f", "#ff9800", "#e65100", "#c62828"]


class ExpertMixin:
    """The classic tabbed 'expert' interface. Hosted as its own window (classic mode)
    or as the Advanced tools window of the new UI."""

    def _init_expert(self, hosted: bool = False):
        self.title(f"{APP_NAME} {APP_VERSION} — " + ("Advanced tools" if hosted else "disk toolkit"))
        self.geometry("1280x820")
        self.minsize(1050, 680)
        style = ttk.Style(self)
        if hosted:
            self._style_hosted(style)
        elif "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Danger.TButton", foreground="#b00020" if not hosted else self._pal("danger"))
        style.configure("Hdr.TLabel", font=("Segoe UI", 11, "bold"))

        self.devices: list[Device] = []
        self.tables: dict[str, partitions.PartitionTable] = {}
        self.cur_dev: Device | None = None
        self.cur_part: partitions.Partition | None = None
        self.job_prog: Progress | None = None
        self.q: queue.Queue = queue.Queue()
        self.found_parts: list[partscan.FoundPartition] = []
        self.ntfs_vol: ntfs.NTFSVolume | None = None
        self.ntfs_entries: list[ntfs.MFTEntry] = []

        self._build()
        self.after(100, self._poll)
        self.refresh_disks()
        if not is_admin():
            self.status("Not running as administrator — physical disks will be unreadable. "
                        "Image files still work. Restart as admin for full access.", warn=True)

    # ================================================================ layout
    def _build(self):
        top = ttk.Frame(self, padding=(8, 6))
        top.pack(fill="x")
        ttk.Button(top, text="⟳ Refresh disks", command=self.refresh_disks).pack(side="left")
        ttk.Button(top, text="Open image file…", command=self.open_image_dialog).pack(side="left", padx=4)
        ttk.Label(top, text="  Admin: " + ("yes" if is_admin() else "NO"),
                  foreground="#2e7d32" if is_admin() else "#b00020").pack(side="left", padx=10)
        ttk.Label(top, text=f"Logs & backups: {app_dir()}", foreground="#777").pack(side="right")

        pw = ttk.PanedWindow(self, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=8)

        left = ttk.Frame(pw, width=330)
        pw.add(left, weight=0)
        ttk.Label(left, text="Disks", style="Hdr.TLabel").pack(anchor="w")
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
        self.tree.column("#0", width=330, stretch=True)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        right = ttk.Frame(pw)
        pw.add(right, weight=4)
        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True)
        self._tab_overview()
        self._tab_wipe()
        self._tab_hex()
        self._tab_surface()
        self._tab_imaging()
        self._tab_partrec()
        self._tab_filerec()

        bottom = ttk.Frame(self, padding=(8, 4))
        bottom.pack(fill="x")
        self.job_label = ttk.Label(bottom, text="Ready", style="Hdr.TLabel")
        self.job_label.pack(anchor="w")
        row = ttk.Frame(bottom)
        row.pack(fill="x")
        self.pbar = ttk.Progressbar(row, maximum=1000)
        self.pbar.pack(side="left", fill="x", expand=True)
        self.cancel_btn = ttk.Button(row, text="Cancel", command=self.cancel_job, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.stats = ttk.Label(bottom, text="", font=("Consolas", 10))
        self.stats.pack(anchor="w")
        self.status_lbl = ttk.Label(bottom, text="", foreground="#555")
        self.status_lbl.pack(anchor="w")

    def _tab(self, title):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text=title)
        return f

    # ---------------------------------------------------------------- overview
    def _tab_overview(self):
        f = self._tab("Overview")
        self.ov_info = ttk.Label(f, text="Select a disk on the left.", justify="left", font=("Segoe UI", 10))
        self.ov_info.pack(anchor="w")
        self.map = tk.Canvas(f, height=54, background="#eee", highlightthickness=0)
        self.map.pack(fill="x", pady=8)
        cols = ("#", "start", "end", "size", "fs", "label", "type", "mount")
        self.ptree = ttk.Treeview(f, columns=cols, show="headings", height=10)
        for c, w, t in zip(cols, (40, 110, 110, 90, 80, 120, 200, 120),
                           ("#", "Start LBA", "End LBA", "Size", "File system", "Label/Name", "Type", "Mounted")):
            self.ptree.heading(c, text=t)
            self.ptree.column(c, width=w, anchor="w")
        self.ptree.pack(fill="both", expand=True)
        self.ptree.bind("<<TreeviewSelect>>", self._on_ptree_select)
        self.ptree.bind("<Double-1>", lambda _e: (self.nb.select(2), self.hex.goto_partition()))
        self.ov_notes = ttk.Label(f, text="", foreground="#b00020")
        self.ov_notes.pack(anchor="w", pady=4)
        btns = ttk.Frame(f)
        btns.pack(fill="x")
        ttk.Button(btns, text="Back up partition table…", command=self.backup_ptable).pack(side="left")
        ttk.Button(btns, text="Restore partition-table backup…", command=self.restore_ptable,
                   style="Danger.TButton").pack(side="left", padx=6)

    def _draw_map(self):
        c = self.map
        c.delete("all")
        dev = self.cur_dev
        if not dev:
            return
        pt = self.tables.get(dev.path)
        w = max(c.winfo_width(), 600)
        total = max(1, dev.total_sectors)
        c.create_rectangle(0, 0, w, 54, fill="#d6d6d6", outline="")
        if pt:
            for p in pt.partitions:
                if p.type_id in ("0x05", "0x0F", "0x85"):
                    continue
                x0 = w * p.start_lba / total
                x1 = max(x0 + 3, w * (p.end_lba + 1) / total)
                col = FS_COLORS.get(p.fs, "#7d8fa8")
                outline = "#000" if self.cur_part is p else "#fff"
                c.create_rectangle(x0, 2, x1, 52, fill=col, outline=outline, width=2)
                if x1 - x0 > 60:
                    c.create_text((x0 + x1) / 2, 18, text=(p.label or p.name or p.fs or "?")[:20], fill="white",
                                  font=("Segoe UI", 9, "bold"))
                    c.create_text((x0 + x1) / 2, 36, text=f"{p.fs} {human_size(p.size_bytes(dev.sector_size))}",
                                  fill="white", font=("Segoe UI", 8))

    # ---------------------------------------------------------------- wipe
    def _tab_wipe(self):
        f = self._tab("Secure Wipe")
        tgt = ttk.LabelFrame(f, text="What to erase", padding=8)
        tgt.pack(fill="x")
        self.wipe_target = tk.StringVar(value="disk")
        for val, txt in (("disk", "Entire selected disk"), ("part", "Selected partition only"),
                         ("free", "Free space on a drive/folder (keeps existing files)"),
                         ("files", "Specific files / folders (shred)")):
            ttk.Radiobutton(tgt, text=txt, value=val, variable=self.wipe_target,
                            command=self._wipe_target_changed).pack(anchor="w")
        row = ttk.Frame(tgt)
        row.pack(fill="x", pady=(6, 0))
        self.wipe_path = tk.StringVar()
        self.wipe_path_entry = ttk.Entry(row, textvariable=self.wipe_path, state="disabled")
        self.wipe_path_entry.pack(side="left", fill="x", expand=True)
        self.wipe_browse = ttk.Button(row, text="Browse…", command=self._wipe_browse, state="disabled")
        self.wipe_browse.pack(side="left", padx=4)
        self.wipe_target_lbl = ttk.Label(tgt, text="", foreground="#2b5797")
        self.wipe_target_lbl.pack(anchor="w", pady=(6, 0))

        m = ttk.LabelFrame(f, text="Method", padding=8)
        m.pack(fill="x", pady=8)
        self.wipe_method = tk.StringVar(value="NIST 800-88 Clear (1 pass + verify)")
        cb = ttk.Combobox(m, textvariable=self.wipe_method, values=list(wipe.METHODS) + ["Custom pattern"],
                          state="readonly", width=48)
        cb.pack(anchor="w")
        cb.bind("<<ComboboxSelected>>", lambda _e: self._wipe_method_changed())
        self.wipe_note = ttk.Label(m, text="", foreground="#555", wraplength=800)
        self.wipe_note.pack(anchor="w", pady=4)
        crow = ttk.Frame(m)
        crow.pack(anchor="w")
        ttk.Label(crow, text="Custom hex pattern").pack(side="left")
        self.wipe_custom = tk.StringVar(value="00")
        ttk.Entry(crow, textvariable=self.wipe_custom, width=16).pack(side="left", padx=4)
        ttk.Label(crow, text="passes").pack(side="left")
        self.wipe_custom_n = tk.IntVar(value=1)
        ttk.Spinbox(crow, from_=1, to=99, textvariable=self.wipe_custom_n, width=4).pack(side="left", padx=4)
        self.wipe_verify = tk.BooleanVar(value=True)
        ttk.Checkbutton(crow, text="verify last pass", variable=self.wipe_verify).pack(side="left", padx=8)
        ttk.Label(f, text="SSD note: overwriting can't reach an SSD's spare/over-provisioned cells. For SSDs being "
                          "disposed of, also run the manufacturer's Secure Erase / Sanitize, or physically destroy.",
                  foreground="#8a6d00", wraplength=900).pack(anchor="w")
        ttk.Button(f, text="Start wipe", style="Danger.TButton", command=self.start_wipe).pack(anchor="w", pady=10)
        self.wipe_result = tk.Text(f, height=8, font=("Consolas", 9))
        self.wipe_result.pack(fill="both", expand=True)
        self._wipe_method_changed()

    def _wipe_target_changed(self):
        t = self.wipe_target.get()
        st = "normal" if t in ("free", "files") else "disabled"
        self.wipe_path_entry.configure(state=st)
        self.wipe_browse.configure(state=st)
        self._update_wipe_label()

    def _update_wipe_label(self):
        t = self.wipe_target.get()
        if t == "disk":
            txt = self.cur_dev.describe() if self.cur_dev else "No disk selected"
        elif t == "part":
            txt = (f"Partition #{self.cur_part.index} ({self.cur_part.fs}, "
                   f"{human_size(self.cur_part.size_bytes(self.cur_dev.sector_size))}) on {self.cur_dev.name}"
                   if self.cur_part and self.cur_dev else "No partition selected (pick one in the tree / Overview)")
        else:
            txt = ""
        self.wipe_target_lbl.configure(text=txt)

    def _wipe_browse(self):
        if self.wipe_target.get() == "free":
            p = filedialog.askdirectory(title="Pick any folder on the drive whose free space to wipe")
        else:
            p = filedialog.askopenfilenames(title="Files to shred")
            p = ";".join(p) if p else ""
            if not p:
                d = filedialog.askdirectory(title="…or a folder to shred")
                p = d or ""
        if p:
            self.wipe_path.set(p)

    def _wipe_method_changed(self):
        name = self.wipe_method.get()
        m = wipe.METHODS.get(name)
        self.wipe_note.configure(text=(f"{len(m.passes)} pass(es), verify: {m.verify}. {m.note}" if m else
                                       "Repeat your own byte pattern N times."))

    def _get_method(self):
        name = self.wipe_method.get()
        if name == "Custom pattern":
            return wipe.custom_method(self.wipe_custom.get(), int(self.wipe_custom_n.get()), self.wipe_verify.get())
        return wipe.METHODS[name]

    def start_wipe(self):
        try:
            method = self._get_method()
        except ValueError:
            messagebox.showerror("Wipe", "Custom pattern must be hex, e.g. 00 or 55AA.")
            return
        t = self.wipe_target.get()
        if t in ("disk", "part"):
            dev = self.cur_dev
            if not dev:
                messagebox.showerror("Wipe", "Select a disk first.")
                return
            if dev.is_system:
                messagebox.showerror("Wipe", "This is the disk Windows is running from. Wiping it is blocked.\n"
                                             "Boot from a USB stick to wipe a system disk.")
                return
            if t == "part" and not self.cur_part:
                messagebox.showerror("Wipe", "Select a partition first.")
                return
            if t == "disk":
                start, count, what = 0, dev.total_sectors, f"ENTIRE {dev.describe()}"
                phrase = f"WIPE {dev.name.upper()}"
            else:
                p = self.cur_part
                start, count = p.start_lba, p.sectors
                what = f"partition #{p.index} ({p.fs} {p.label}) on {dev.describe()}"
                phrase = f"WIPE PARTITION {p.index}"
            mounts = mounted_partitions_linux(dev)
            extra = f"\nMounted here: {', '.join(mounts)}" if mounts else ""
            if not self.confirm_typed("Confirm secure wipe",
                                      f"This permanently destroys all data on:\n\n{what}\n\nMethod: {method.name}"
                                      f"\nSize: {human_size(count * dev.sector_size)}{extra}\n\n"
                                      "There is no undo. Data will NOT be recoverable.", phrase):
                return
            wdev = self.clone_device(dev)
            self.hex.dev and self.hex.dev.close()

            def job(prog):
                return wipe.wipe_disk(wdev, method, prog, start_lba=start, sectors=count)
        elif t == "free":
            folder = self.wipe_path.get().strip()
            if not folder or not os.path.isdir(folder):
                messagebox.showerror("Wipe", "Pick a folder on the drive to clean.")
                return
            if not messagebox.askyesno("Free-space wipe", f"Fill all free space on the drive holding\n{folder}\n"
                                                          f"with '{method.name}' and then release it?\n\n"
                                                          "Existing files are kept. The drive will briefly be full."):
                return

            def job(prog):
                return wipe.wipe_free_space(folder, method, prog)
        else:
            paths = [p for p in self.wipe_path.get().split(";") if p.strip()]
            if not paths:
                messagebox.showerror("Wipe", "Pick files or a folder to shred.")
                return
            if not self.confirm_typed("Shred files", "Permanently shred and delete:\n\n" + "\n".join(paths[:15]) +
                                      ("\n…" if len(paths) > 15 else ""), "SHRED"):
                return

            def job(prog):
                return wipe.shred_paths(paths, method, prog)

        def done(res):
            self.wipe_result.insert("end", time.strftime("[%H:%M:%S] ") + repr(res) + "\n")
            self.wipe_result.see("end")
            if t in ("disk", "part"):
                self.refresh_disks(keep=True)
            messagebox.showinfo("Wipe complete", self._format_result(res))

        self.run_job("Secure wipe", job, done)

    @staticmethod
    def _format_result(res: dict) -> str:
        lines = []
        for k, v in res.items():
            if k in ("bad_lbas",):
                continue
            if k == "bytes":
                v = human_size(v)
            lines.append(f"{k.replace('_', ' ')}: {v}")
        return "\n".join(lines)

    # ---------------------------------------------------------------- hex
    def _tab_hex(self):
        f = self._tab("Sector Editor")
        self.hex = HexView(f, self)
        self.hex.pack(fill="both", expand=True)

    # ---------------------------------------------------------------- surface
    def _tab_surface(self):
        f = self._tab("Surface Test")
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.surf_scope = tk.StringVar(value="disk")
        ttk.Radiobutton(row, text="Whole disk", value="disk", variable=self.surf_scope).pack(side="left")
        ttk.Radiobutton(row, text="Selected partition", value="part", variable=self.surf_scope).pack(side="left",
                                                                                                    padx=8)
        self.surf_repair = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Repair bad sectors (rewrite them so the drive remaps; destroys those sectors)",
                        variable=self.surf_repair).pack(side="left", padx=12)
        ttk.Button(row, text="Start scan", command=self.start_surface).pack(side="right")
        self.surf_canvas = tk.Canvas(f, background="#fafafa", highlightthickness=1, highlightbackground="#ccc")
        self.surf_canvas.pack(fill="both", expand=True, pady=8)
        leg = ttk.Frame(f)
        leg.pack(fill="x")
        for col, name in zip(SURF_COLORS, ["<25 ms", "<75 ms", "<200 ms", "<600 ms", "≥600 ms (slow)", "Bad"]):
            tk.Canvas(leg, width=14, height=14, background=col, highlightthickness=0).pack(side="left", padx=(8, 2))
            ttk.Label(leg, text=name).pack(side="left")
        self.surf_stats = ttk.Label(f, text="Per-cell = 1 MiB block read time.", font=("Consolas", 10))
        self.surf_stats.pack(anchor="w", pady=4)
        self._surf_cells = 0
        self._surf_counts = [0] * 6

    def start_surface(self):
        dev = self.cur_dev
        if not dev:
            messagebox.showerror("Surface test", "Select a disk first.")
            return
        if self.surf_scope.get() == "part":
            if not self.cur_part:
                messagebox.showerror("Surface test", "Select a partition first.")
                return
            start, count = self.cur_part.start_lba, self.cur_part.sectors
        else:
            start, count = 0, dev.total_sectors
        repair = self.surf_repair.get()
        if repair:
            if dev.is_system:
                messagebox.showerror("Surface test", "Repair mode is blocked on the system disk.")
                return
            if not self.confirm_typed("Repair mode", "Unreadable sectors will be overwritten with zeros so the "
                                                     "drive remaps them. Volumes on this disk are dismounted.\n"
                                                     "Image the disk first if you need its data.", "REPAIR"):
                return
        blocks = -(-count // 2048)
        c = self.surf_canvas
        c.delete("all")
        c.update_idletasks()
        W, H = max(400, c.winfo_width()), max(200, c.winfo_height())
        cells = min(blocks, 20000)
        side = max(3, int((W * H / max(cells, 1)) ** 0.5))
        while (W // side) * (H // side) < cells and side > 3:
            side -= 1
        cols = max(1, W // side)
        self._surf_geom = (cells, blocks, cols, side)
        self._surf_worst = [-1] * cells
        self._surf_counts = [0] * 6
        wdev = self.clone_device(dev)

        def on_block(b, total, cls, ms):
            self.q.put(("surf", b, cls))

        def job(prog):
            return surface.surface_scan(wdev, prog, start_lba=start, sectors=count, on_block=on_block, repair=repair)

        def done(res):
            self._flush_surface()
            c = res["counts"]
            txt = (f"Blocks: {res['blocks']:,}   Bad sectors: {res['bad_sectors']}   Repaired: {res['repaired']}\n"
                   + "   ".join(f"{k}: {v:,}" for k, v in c.items()))
            if res["bad_lbas"]:
                txt += "\nFirst bad LBAs: " + ", ".join(map(str, res["bad_lbas"][:20]))
            self.surf_stats.configure(text=txt)
            messagebox.showinfo("Surface test finished", txt)

        self.run_job("Surface test", job, done)

    def _surface_cell(self, b, cls):
        cells, blocks, cols, side = self._surf_geom
        i = b * cells // blocks
        self._surf_counts[cls] += 1
        if cls > self._surf_worst[i]:
            self._surf_worst[i] = cls
            x, y = (i % cols) * side, (i // cols) * side
            self.surf_canvas.create_rectangle(x, y, x + side - 1, y + side - 1, fill=SURF_COLORS[cls], outline="")

    def _flush_surface(self):
        self.surf_stats.configure(text="   ".join(f"{n}: {v}" for n, v in zip(surface.CLASSES, self._surf_counts)))

    # ---------------------------------------------------------------- imaging
    def _tab_imaging(self):
        f = self._tab("Imaging / Clone")
        self.img_mode = tk.StringVar(value="toimage")
        box = ttk.LabelFrame(f, text="Operation", padding=8)
        box.pack(fill="x")
        for v, t in (("toimage", "Selected disk (or partition) → image file (.img raw or .vhd for Windows)"),
                     ("clone", "Selected disk (or partition) → another disk (sector clone)"),
                     ("restore", "Image file → selected disk")):
            ttk.Radiobutton(box, text=t, value=v, variable=self.img_mode).pack(anchor="w")
        self.img_part_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="Selected partition only (instead of whole disk)",
                        variable=self.img_part_only).pack(anchor="w", pady=(6, 0))
        r = ttk.Frame(f)
        r.pack(fill="x", pady=8)
        ttk.Label(r, text="Image file").pack(side="left")
        self.img_path = tk.StringVar()
        ttk.Entry(r, textvariable=self.img_path).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(r, text="Browse…", command=self._img_browse).pack(side="left")
        r2 = ttk.Frame(f)
        r2.pack(fill="x")
        ttk.Label(r2, text="Clone target disk").pack(side="left")
        self.img_target = tk.StringVar()
        self.img_target_cb = ttk.Combobox(r2, textvariable=self.img_target, state="readonly", width=70)
        self.img_target_cb.pack(side="left", padx=4)
        ttk.Label(f, text="Unreadable sectors are skipped on the first pass, retried one-by-one on a second pass, "
                          "and filled with zeros. A SHA-256 hash and a log (.log.txt) are written next to the image.",
                  foreground="#555", wraplength=900).pack(anchor="w", pady=8)
        ttk.Button(f, text="Start", command=self.start_imaging).pack(anchor="w")
        self.img_result = tk.Text(f, height=10, font=("Consolas", 9))
        self.img_result.pack(fill="both", expand=True, pady=8)

    def _img_browse(self):
        if self.img_mode.get() == "restore":
            p = filedialog.askopenfilename(filetypes=[("Disk images", "*.img *.dd *.bin *.raw *.vhd"), ("All", "*")])
        else:
            p = filedialog.asksaveasfilename(defaultextension=".img",
                                             filetypes=[("Raw image", "*.img"), ("Fixed VHD", "*.vhd")])
        if p:
            self.img_path.set(p)

    def start_imaging(self):
        mode = self.img_mode.get()
        dev = self.cur_dev
        if not dev:
            messagebox.showerror("Imaging", "Select a disk first.")
            return
        start, count = 0, None
        if self.img_part_only.get():
            if not self.cur_part:
                messagebox.showerror("Imaging", "Select a partition first.")
                return
            start, count = self.cur_part.start_lba, self.cur_part.sectors
        if mode == "toimage":
            path = self.img_path.get().strip()
            if not path:
                messagebox.showerror("Imaging", "Choose where to save the image.")
                return
            src = self.clone_device(dev)
            fmt = "vhd" if path.lower().endswith(".vhd") else "raw"

            def job(prog):
                return surface.image_copy(src, path, prog, start_lba=start, sectors=count, fmt=fmt)
        elif mode == "clone":
            tgt = self._lookup_target()
            if tgt is None:
                return
            if tgt.path == dev.path:
                messagebox.showerror("Clone", "Source and target are the same disk.")
                return
            if tgt.is_system:
                messagebox.showerror("Clone", "The target is the system disk — blocked.")
                return
            size = (count or dev.total_sectors) * dev.sector_size
            if tgt.usable_size < size:
                messagebox.showerror("Clone", f"Target ({human_size(tgt.usable_size)}) is smaller than the "
                                              f"source ({human_size(size)}).")
                return
            if not self.confirm_typed("Confirm clone", f"Everything on the TARGET will be overwritten:\n\n"
                                                       f"{tgt.describe()}\n\nSource: {dev.describe()}",
                                      f"CLONE TO {tgt.name.upper()}"):
                return
            src, dst = self.clone_device(dev), self.clone_device(tgt)

            def job(prog):
                return surface.image_copy(src, dst, prog, start_lba=start, sectors=count)
        else:
            path = self.img_path.get().strip()
            if not path or not os.path.isfile(path):
                messagebox.showerror("Restore", "Choose an image file to restore.")
                return
            if dev.is_system:
                messagebox.showerror("Restore", "Restoring over the system disk is blocked.")
                return
            img = open_image(path)
            tdev = self.clone_device(dev)
            offset_lba = start
            if img.usable_size > (count or dev.total_sectors) * dev.sector_size:
                messagebox.showerror("Restore", "The image is larger than the target.")
                return
            what = f"partition #{self.cur_part.index}" if self.img_part_only.get() else "the whole disk"
            if not self.confirm_typed("Confirm restore", f"Overwrite {what} on\n{dev.describe()}\nwith\n{path}?",
                                      "RESTORE"):
                return

            def job(prog):
                # restore = copy image sectors onto the device at the chosen offset
                tdev.open(writable=True)
                try:
                    ss = tdev.sector_size
                    total = img.usable_size
                    prog.reset(total, "Restoring image")
                    pos = 0
                    while pos < total:
                        prog.check()
                        d = img.read(pos, min(4 * 1024 * 1024, total - pos))
                        if len(d) % ss:
                            d = d.ljust(-(-len(d) // ss) * ss, b"\0")
                        tdev.write(offset_lba * ss + pos, d)
                        pos += len(d)
                        prog.update(pos)
                    tdev.flush()
                finally:
                    tdev.close()
                    img.close()
                return {"bytes": total, "target": tdev.path}

        def done(res):
            self.img_result.insert("end", time.strftime("[%H:%M:%S] ") + self._format_result(res).replace("\n", " | ")
                                   + "\n")
            self.refresh_disks(keep=True)
            messagebox.showinfo("Done", self._format_result(res))

        self.run_job("Imaging", job, done)

    def _lookup_target(self):
        name = self.img_target.get()
        for d in self.devices:
            if d.describe() == name:
                return d
        messagebox.showerror("Clone", "Pick a target disk.")
        return None

    # ---------------------------------------------------------------- partition recovery
    def _tab_partrec(self):
        f = self._tab("Partition Recovery")
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.ps_mode = tk.StringVar(value="quick")
        ttk.Radiobutton(row, text="Quick (1 MiB-aligned & classic boundaries — minutes)", value="quick",
                        variable=self.ps_mode).pack(side="left")
        ttk.Radiobutton(row, text="Full (every sector — slow, finds everything)", value="full",
                        variable=self.ps_mode).pack(side="left", padx=10)
        ttk.Button(row, text="Search for lost partitions", command=self.start_partscan).pack(side="right")
        cols = ("start", "end", "size", "fs", "label", "status", "source", "conf")
        self.fp_tree = ttk.Treeview(f, columns=cols, show="headings", height=12)
        for c, w, t in zip(cols, (110, 110, 90, 70, 110, 80, 220, 140),
                           ("Start LBA", "End LBA", "Size", "FS", "Label", "Status", "Found via", "Confidence")):
            self.fp_tree.heading(c, text=t)
            self.fp_tree.column(c, width=w, anchor="w")
        self.fp_tree.pack(fill="both", expand=True, pady=8)
        b = ttk.Frame(f)
        b.pack(fill="x")
        ttk.Button(b, text="Write selected to partition table…", style="Danger.TButton",
                   command=self.restore_found).pack(side="left")
        ttk.Button(b, text="Browse files in selected (NTFS)", command=self.browse_found).pack(side="left", padx=6)
        ttk.Button(b, text="Deep-scan selected for files", command=self.carve_found).pack(side="left")
        self.ps_gpt = tk.BooleanVar(value=True)
        ttk.Checkbutton(b, text="Create GPT if disk has no table (else MBR)", variable=self.ps_gpt).pack(side="left",
                                                                                                          padx=10)
        ttk.Label(f, text="A backup of the first and last 1 MiB of the disk is saved automatically before anything "
                          "is written. Restore it from the Overview tab if needed.",
                  foreground="#555").pack(anchor="w", pady=4)

    def start_partscan(self):
        dev = self.cur_dev
        if not dev:
            messagebox.showerror("Partition recovery", "Select a disk first.")
            return
        self.fp_tree.delete(*self.fp_tree.get_children())
        self.found_parts = []
        sdev = self.clone_device(dev)
        mode = self.ps_mode.get()

        def job(prog):
            return partscan.scan_partitions(sdev, prog, mode=mode, on_found=lambda fp: self.q.put(("found", fp)))

        def done(res):
            self.found_parts = res
            self.fp_tree.delete(*self.fp_tree.get_children())
            for i, fp in enumerate(res):
                self._add_found_row(i, fp)
            lost = sum(1 for x in res if x.status == "Lost")
            messagebox.showinfo("Search finished", f"Found {len(res)} partition(s), {lost} not in the current table.")

        self.run_job("Partition search", job, done)

    def _add_found_row(self, i, fp):
        ss = self.cur_dev.sector_size if self.cur_dev else 512
        self.fp_tree.insert("", "end", iid=str(i), values=(
            fp.start_lba, fp.end_lba, human_size(fp.sectors * ss), fp.fs, fp.label, fp.status, fp.source,
            fp.confidence), tags=(fp.status,))
        self.fp_tree.tag_configure("Lost", foreground="#b00020")

    def _sel_found(self):
        sel = self.fp_tree.selection()
        if not sel or not self.found_parts:
            messagebox.showerror("Partition recovery", "Select a found partition (run a search first).")
            return []
        return [self.found_parts[int(s)] for s in sel]

    def restore_found(self):
        fps = [f for f in self._sel_found() if f.status == "Lost"]
        if not fps:
            messagebox.showinfo("Partition recovery", "Select one or more partitions marked 'Lost'.")
            return
        dev = self.cur_dev
        if dev.is_system:
            messagebox.showerror("Partition recovery", "Writing the system disk's table is blocked.")
            return
        desc = "\n".join(f"  {f.fs} at LBA {f.start_lba}, {human_size(f.sectors * dev.sector_size)}" for f in fps)
        if not self.confirm_typed("Restore partitions", f"Add these entries to the partition table of\n"
                                                        f"{dev.describe()}:\n\n{desc}", "RESTORE"):
            return
        wdev = self.clone_device(dev)
        gpt = self.ps_gpt.get()

        def job(prog):
            prog.reset(len(fps) + 1, "Backing up partition table")
            wdev.open(writable=False)
            bk = partitions.backup_table_area(wdev)
            wdev.close()
            wdev.open(writable=True)
            out = [f"Backup: {bk}"]
            try:
                for i, f in enumerate(fps):
                    out.append(partitions.add_partition_entry(wdev, f.start_lba, f.sectors, f.fs, prefer_gpt=gpt))
                    prog.update(i + 2)
            finally:
                wdev.close()
            return out

        def done(res):
            self.refresh_disks(keep=True)
            messagebox.showinfo("Partition recovery", "\n".join(res) + "\n\nWindows may need a rescan "
                                                                       "(Disk Management → Action → Rescan Disks).")

        self.run_job("Restoring partitions", job, done)

    def browse_found(self):
        fps = self._sel_found()
        if fps:
            if fps[0].fs != "NTFS":
                messagebox.showinfo("Browse", "File browsing is available for NTFS. Use Deep scan for other types.")
                return
            self.nb.select(6)
            self.fr_lba.set(str(fps[0].start_lba))
            self.start_ntfs_scan()

    def carve_found(self):
        fps = self._sel_found()
        if fps:
            self.nb.select(6)
            self.fr_lba.set(str(fps[0].start_lba))
            self.carve_range.set(f"{fps[0].start_lba}-{fps[0].end_lba}")

    # ---------------------------------------------------------------- file recovery
    def _tab_filerec(self):
        f = self._tab("File Recovery")
        top = ttk.LabelFrame(f, text="1) Recover deleted files with names & folders (NTFS)", padding=8)
        top.pack(fill="both", expand=True)
        r = ttk.Frame(top)
        r.pack(fill="x")
        ttk.Label(r, text="Partition start LBA").pack(side="left")
        self.fr_lba = tk.StringVar()
        ttk.Entry(r, textvariable=self.fr_lba, width=14).pack(side="left", padx=4)
        ttk.Button(r, text="Use selected partition",
                   command=lambda: self.fr_lba.set(str(self.cur_part.start_lba)) if self.cur_part else None
                   ).pack(side="left")
        ttk.Button(r, text="Scan MFT", command=self.start_ntfs_scan).pack(side="left", padx=6)
        self.fr_deleted = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text="Deleted only", variable=self.fr_deleted, command=self._fill_ntfs).pack(side="left")
        ttk.Label(r, text="Filter").pack(side="left", padx=(10, 2))
        self.fr_filter = tk.StringVar()
        fe = ttk.Entry(r, textvariable=self.fr_filter, width=20)
        fe.pack(side="left")
        fe.bind("<KeyRelease>", lambda _e: self._fill_ntfs())
        cols = ("name", "path", "size", "modified", "state", "rec")
        self.fr_tree = ttk.Treeview(top, columns=cols, show="headings", height=9, selectmode="extended")
        for c, w, t in zip(cols, (220, 330, 90, 140, 170, 70),
                           ("Name", "Folder", "Size", "Modified", "Recoverability", "MFT #")):
            self.fr_tree.heading(c, text=t, command=lambda c=c: self._sort_ntfs(c))
            self.fr_tree.column(c, width=w, anchor="w")
        self.fr_tree.pack(fill="both", expand=True, pady=4)
        r2 = ttk.Frame(top)
        r2.pack(fill="x")
        ttk.Button(r2, text="Recover selected…", command=lambda: self.recover_ntfs(False)).pack(side="left")
        ttk.Button(r2, text="Recover all listed…", command=lambda: self.recover_ntfs(True)).pack(side="left", padx=6)
        self.fr_count = ttk.Label(r2, text="")
        self.fr_count.pack(side="left", padx=10)

        bot = ttk.LabelFrame(f, text="2) Deep scan — find files by signature (any filesystem, formatted, RAW)",
                             padding=8)
        bot.pack(fill="x", pady=(8, 0))
        g = ttk.Frame(bot)
        g.pack(fill="x")
        self.carve_groups = {}
        for grp in dict.fromkeys(s.group for s in carver.SIGNATURES):
            v = tk.BooleanVar(value=True)
            self.carve_groups[grp] = v
            ttk.Checkbutton(g, text=grp, variable=v).pack(side="left", padx=(0, 10))
        r3 = ttk.Frame(bot)
        r3.pack(fill="x", pady=4)
        ttk.Label(r3, text="LBA range (blank = whole disk)").pack(side="left")
        self.carve_range = tk.StringVar()
        ttk.Entry(r3, textvariable=self.carve_range, width=20).pack(side="left", padx=4)
        ttk.Button(r3, text="Use selected partition",
                   command=lambda: self.carve_range.set(f"{self.cur_part.start_lba}-{self.cur_part.end_lba}")
                   if self.cur_part else None).pack(side="left")
        ttk.Label(r3, text="Save to").pack(side="left", padx=(10, 2))
        self.carve_out = tk.StringVar()
        ttk.Entry(r3, textvariable=self.carve_out, width=22).pack(side="left")
        ttk.Button(r3, text="…", width=3, command=lambda: self.carve_out.set(
            filedialog.askdirectory() or self.carve_out.get())).pack(side="left")
        ttk.Button(r3, text="Start deep scan", command=self.start_carve).pack(side="right")
        self.carve_status = ttk.Label(bot, text="Tip: save recovered files to a DIFFERENT disk.", foreground="#555")
        self.carve_status.pack(anchor="w")

    def start_ntfs_scan(self):
        dev = self.cur_dev
        if not dev:
            messagebox.showerror("File recovery", "Select a disk first.")
            return
        try:
            lba = int(self.fr_lba.get())
        except ValueError:
            messagebox.showerror("File recovery", "Enter the NTFS partition's start LBA (or select a partition).")
            return
        sdev = self.clone_device(dev)

        def job(prog):
            vol = ntfs.NTFSVolume(sdev, lba * sdev.sector_size)
            return vol, vol.scan(prog)

        def done(res):
            self.ntfs_vol, self.ntfs_entries = res
            self._fill_ntfs()

        self.run_job("Reading MFT", job, done)

    def _fill_ntfs(self):
        t = self.fr_tree
        t.delete(*t.get_children())
        flt = self.fr_filter.get().lower()
        shown = 0
        self._ntfs_index = {}
        for e in self.ntfs_entries:
            if e.is_dir or (self.fr_deleted.get() and not e.deleted) or e.path.startswith("\\$Extend"):
                continue
            if flt and flt not in e.name.lower() and flt not in e.path.lower():
                continue
            shown += 1
            if shown > 50000:
                break
            iid = str(e.recno)
            self._ntfs_index[iid] = e
            t.insert("", "end", iid=iid, values=(e.name, e.path, human_size(e.size),
                                                 e.mtime.strftime("%Y-%m-%d %H:%M") if e.mtime else "",
                                                 e.recoverability, e.recno),
                     tags=("del",) if e.deleted else ())
        t.tag_configure("del", foreground="#b00020")
        total_del = sum(1 for e in self.ntfs_entries if e.deleted and not e.is_dir)
        self.fr_count.configure(text=f"{shown:,} shown — {total_del:,} deleted files on this volume")

    def _sort_ntfs(self, col):
        key = {"name": lambda e: e.name.lower(), "path": lambda e: e.path.lower(), "size": lambda e: -e.size,
               "modified": lambda e: e.mtime or 0, "state": lambda e: e.recoverability, "rec": lambda e: e.recno}[col]
        try:
            self.ntfs_entries.sort(key=key)
        except TypeError:
            self.ntfs_entries.sort(key=lambda e: str(key(e)))
        self._fill_ntfs()

    def recover_ntfs(self, all_listed: bool):
        if not self.ntfs_vol:
            messagebox.showerror("Recover", "Scan an NTFS partition first.")
            return
        ids = self.fr_tree.get_children() if all_listed else self.fr_tree.selection()
        ents = [self._ntfs_index[i] for i in ids if i in self._ntfs_index]
        if not ents:
            messagebox.showerror("Recover", "Nothing selected.")
            return
        out = filedialog.askdirectory(title="Save recovered files to (use a different disk!)")
        if not out:
            return
        vol = self.ntfs_vol

        def job(prog):
            return ntfs.recover_entries(vol, ents, out, prog)

        def done(res):
            msg = f"Recovered {res['recovered']} file(s) to\n{out}"
            if res["failed"]:
                msg += f"\n\n{len(res['failed'])} failed, e.g.:\n" + "\n".join(res["failed"][:5])
            messagebox.showinfo("Recovery finished", msg)

        self.run_job("Recovering files", job, done)

    def start_carve(self):
        dev = self.cur_dev
        if not dev:
            messagebox.showerror("Deep scan", "Select a disk first.")
            return
        out = self.carve_out.get().strip()
        if not out:
            messagebox.showerror("Deep scan", "Choose an output folder (on a different disk).")
            return
        rng = self.carve_range.get().strip()
        start, end = 0, None
        if rng:
            try:
                a, b = rng.split("-")
                start, end = int(a), int(b) + 1
            except ValueError:
                messagebox.showerror("Deep scan", "Range must look like 2048-1050623")
                return
        sigs = [s for s in carver.SIGNATURES if self.carve_groups[s.group].get()]
        if not sigs:
            return
        sdev = self.clone_device(dev)
        self._carve_n = 0

        def on_file(name, ln, ext, lba):
            self.q.put(("carved", name))

        def job(prog):
            return carver.carve(sdev, out, prog, sigs=sigs, start_lba=start, end_lba=end, on_file=on_file)

        def done(res):
            self.carve_status.configure(text=f"Done: {res['files']} files → {out}   " +
                                             ", ".join(f"{k}: {v}" for k, v in sorted(res["by_type"].items())))
            messagebox.showinfo("Deep scan finished", f"{res['files']} files saved to\n{out}")

        self.run_job("Deep scan", job, done)

    # ================================================================ disks
    def refresh_disks(self, keep: bool = False):
        prev = self.cur_dev.path if (keep and self.cur_dev) else None
        prev_part = None
        if keep and self.cur_dev and self.cur_part and self.cur_dev.path in self.tables:
            try:
                prev_part = self.tables[self.cur_dev.path].partitions.index(self.cur_part)
            except ValueError:
                prev_part = None
        images = [d for d in self.devices if d.is_image]
        for d in self.devices:
            d.close()
        try:
            self.devices = list_disks() + images
        except Exception as e:  # noqa: BLE001
            self.devices = images
            self.status(f"Disk enumeration failed: {e}", warn=True)
        self.tables.clear()
        self.tree.delete(*self.tree.get_children())
        for di, d in enumerate(self.devices):
            label = f"{d.name}  {human_size(d.size)}  {d.model}" + ("  [SYSTEM]" if d.is_system else "")
            node = self.tree.insert("", "end", iid=f"d{di}", text=label, open=True)
            try:
                pt = partitions.read_partition_table(d)
                self.tables[d.path] = pt
                letters = volume_letters_for(d)
                for pi, p in enumerate(pt.partitions):
                    p.mount = letters.get(p.start_lba * d.sector_size, [])
                    m = f" ({', '.join(p.mount)})" if p.mount else ""
                    self.tree.insert(node, "end", iid=f"d{di}p{pi}",
                                     text=f"#{p.index} {p.label or p.name or p.type_name}{m} — {p.fs or '?'} "
                                          f"{human_size(p.size_bytes(d.sector_size))}")
            except OSError as e:
                self.tree.insert(node, "end", text=f"(cannot read: {e.strerror or e})")
            finally:
                d.close()
        self.img_target_cb.configure(values=[d.describe() for d in self.devices])
        if prev:
            for di, d in enumerate(self.devices):
                if d.path == prev:
                    iid = f"d{di}p{prev_part}" if prev_part is not None else f"d{di}"
                    self.tree.selection_set(iid if self.tree.exists(iid) else f"d{di}")
                    return
        self.cur_dev = None
        self.cur_part = None
        self._show_overview()

    def open_image_dialog(self):
        p = filedialog.askopenfilename(title="Open disk image",
                                       filetypes=[("Disk images", "*.img *.dd *.bin *.raw *.vhd *.001"), ("All", "*")])
        if not p:
            return
        self.devices.append(open_image(p))
        imgs = [d for d in self.devices if d.is_image]
        self.devices = [d for d in self.devices if not d.is_image]
        self.devices += imgs
        self.refresh_disks()
        self.tree.selection_set(f"d{len(self.devices) - 1}")

    def _on_tree_select(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        di = int(iid[1:].split("p")[0])
        dev = self.devices[di]
        changed = dev is not self.cur_dev
        self.cur_dev = dev
        pt = self.tables.get(dev.path)
        self.cur_part = pt.partitions[int(iid.split("p")[1])] if ("p" in iid and pt) else None
        if changed:
            self.hex.set_device(self.clone_device(dev))
        self._show_overview()
        self._update_wipe_label()

    def _on_ptree_select(self, _e=None):
        sel = self.ptree.selection()
        if not sel or not self.cur_dev:
            return
        pt = self.tables.get(self.cur_dev.path)
        self.cur_part = pt.partitions[int(sel[0])]
        self._draw_map()
        self._update_wipe_label()

    def _show_overview(self):
        dev = self.cur_dev
        self.ptree.delete(*self.ptree.get_children())
        if not dev:
            self.ov_info.configure(text="Select a disk on the left.")
            self.map.delete("all")
            return
        pt = self.tables.get(dev.path)
        self.ov_info.configure(text=(
            f"{dev.name}   {dev.model}   {human_size(dev.size)} ({dev.size:,} bytes)\n"
            f"Bus: {dev.bus}   Sector: {dev.sector_size} B   Sectors: {dev.total_sectors:,}   "
            f"Serial: {dev.serial or '-'}   Table: {pt.scheme if pt else '?'}  ID: {pt.disk_guid if pt else ''}"
            + ("\n⚠ SYSTEM DISK — write operations are blocked." if dev.is_system else "")))
        if pt:
            for i, p in enumerate(pt.partitions):
                self.ptree.insert("", "end", iid=str(i), values=(
                    p.index, p.start_lba, p.end_lba, human_size(p.size_bytes(dev.sector_size)), p.fs,
                    p.label or p.name, p.type_name, ", ".join(p.mount)))
            if self.cur_part in pt.partitions:
                self.ptree.selection_set(str(pt.partitions.index(self.cur_part)))
            self.ov_notes.configure(text="  ".join(pt.notes))
        self.after(50, self._draw_map)

    def backup_ptable(self):
        if not self.cur_dev:
            return
        folder = filedialog.askdirectory(title="Save partition-table backup in…") or None
        try:
            d = self.clone_device(self.cur_dev)
            p = partitions.backup_table_area(d, folder)
            d.close()
            messagebox.showinfo("Backup", f"Saved first + last 1 MiB to\n{p}")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Backup failed", str(e))

    def restore_ptable(self):
        dev = self.cur_dev
        if not dev:
            return
        if dev.is_system:
            messagebox.showerror("Restore", "Blocked on the system disk.")
            return
        p = filedialog.askopenfilename(title="Partition-table backup", initialdir=str(app_dir() / "backups"),
                                       filetypes=[("SectorSmith backup", "*_ptable.bin"), ("All", "*")])
        if not p or not self.confirm_typed("Restore table backup", f"Overwrite the first/last 1 MiB of\n"
                                                                   f"{dev.describe()}\nwith\n{p}?", "RESTORE"):
            return
        try:
            d = self.clone_device(dev)
            d.open(writable=True)
            partitions.restore_table_backup(d, p)
            d.close()
            self.refresh_disks(keep=True)
            messagebox.showinfo("Restore", "Partition-table area restored.")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Restore failed", str(e))

    # ================================================================ helpers
    @staticmethod
    def clone_device(dev: Device) -> Device:
        """Independent handle to the same disk, so jobs and the UI never share one."""
        return dataclasses.replace(dev, _io=None, _writable=False, _lock=threading.Lock(), _locker=None,
                                   _overlay=[])

    def status(self, msg: str, warn: bool = False):
        self.status_lbl.configure(text=msg, foreground="#b00020" if warn else "#555")

    def confirm_typed(self, title: str, message: str, phrase: str) -> bool:
        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)
        ttk.Label(dlg, text=message, justify="left", wraplength=520, padding=12).pack()
        ttk.Label(dlg, text=f"Type  {phrase}  to confirm:", font=("Segoe UI", 10, "bold"),
                  foreground="#b00020", padding=(12, 0)).pack(anchor="w")
        v = tk.StringVar()
        e = ttk.Entry(dlg, textvariable=v, width=40)
        e.pack(padx=12, pady=6, anchor="w")
        e.focus_set()
        res = {"ok": False}
        btns = ttk.Frame(dlg, padding=12)
        btns.pack(fill="x")
        ok = ttk.Button(btns, text="Proceed", style="Danger.TButton", state="disabled",
                        command=lambda: (res.update(ok=True), dlg.destroy()))
        ok.pack(side="right")
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right", padx=6)
        v.trace_add("write", lambda *_: ok.configure(state="normal" if v.get().strip() == phrase else "disabled"))
        e.bind("<Return>", lambda _e: ok.invoke() if v.get().strip() == phrase else None)
        self.wait_window(dlg)
        return res["ok"]

    # ================================================================ jobs
    def run_job(self, title: str, func, on_done=None):
        if self.job_prog is not None:
            messagebox.showwarning("Busy", "Another operation is running. Cancel it or wait for it to finish.")
            return
        prog = Progress(1, title)
        self.job_prog = prog
        self._last_prog = prog
        self.cancel_btn.configure(state="normal")
        self.job_label.configure(text=title)
        self.status("")
        log.info("Job start: %s", title)

        def worker():
            try:
                with cancel_scope(prog.check):
                    res = func(prog)
                self.q.put(("done", on_done, res))
            except Cancelled:
                self.q.put(("cancelled", title))
            except Exception as e:  # noqa: BLE001
                log.error("Job %s failed: %s", title, traceback.format_exc())
                self.q.put(("error", title, e))

        threading.Thread(target=worker, daemon=True).start()

    def cancel_job(self):
        if self.job_prog:
            self.job_prog.cancel()
            self.cancel_btn.configure(state="disabled")
            self.status("Cancelling…")

    def _poll(self):
        try:
            n = 0
            while n < 2000:
                item = self.q.get_nowait()
                n += 1
                kind = item[0]
                if kind == "surf":
                    self._surface_cell(item[1], item[2])
                elif kind == "found":
                    self.found_parts.append(item[1])
                    self._add_found_row(len(self.found_parts) - 1, item[1])
                elif kind == "carved":
                    self._carve_n += 1
                    self.carve_status.configure(text=f"{self._carve_n} files found — {os.path.basename(item[1])}")
                elif kind in ("done", "cancelled", "error"):
                    self._finish(item)
        except queue.Empty:
            pass
        p = self.job_prog
        if p is not None:
            s = p.snapshot()
            self.pbar["value"] = s["pct"] * 10
            self.job_label.configure(text=s["label"])
            spd = f"{human_size(s['speed'])}/s" if s["speed"] else "—"
            self.stats.configure(text=f"{s['pct']:6.2f}%   {human_size(s['done'])} / {human_size(s['total'])}   "
                                      f"speed {spd}   ETA {human_time(s['eta'])}   elapsed {human_time(s['elapsed'])}"
                                      f"   {s['detail']}")
        self.after(150, self._poll)

    def _finish(self, item):
        kind = item[0]
        self.job_prog = None
        self.cancel_btn.configure(state="disabled")
        if kind == "done":
            self.pbar["value"] = 1000
            lp = getattr(self, "_last_prog", None)
            if lp is not None:
                el = lp.snapshot()["elapsed"]
                self.job_label.configure(text=f"Finished: {lp.label}")
                self.stats.configure(text=f"100%   completed in {human_time(el)}")
            on_done, res = item[1], item[2]
            if on_done:
                try:
                    on_done(res)
                except Exception as e:  # noqa: BLE001
                    log.error("on_done failed: %s", traceback.format_exc())
                    messagebox.showerror("Error", str(e))
        elif kind == "cancelled":
            self.job_label.configure(text=f"{item[1]} — cancelled")
            self.status("Operation cancelled.")
        else:
            self.job_label.configure(text=f"{item[1]} — failed")
            messagebox.showerror(item[1], f"{type(item[2]).__name__}: {item[2]}\n\nDetails in "
                                          f"{app_dir() / 'sectorsmith.log'}")


    # --- theming when hosted inside the new UI ----------------------------------------
    @staticmethod
    def _pal(name):
        from .ui import theme
        return theme.c(name)

    def _style_hosted(self, style):
        from .ui import theme
        style.theme_use("clam")
        bg, card, text, muted, acc = (theme.c(k) for k in ("bg", "card", "text", "muted", "accent"))
        self.configure(background=bg)
        f = theme.family()
        style.configure(".", background=bg, foreground=text, fieldbackground=card, bordercolor=theme.c("border"),
                        lightcolor=bg, darkcolor=bg, troublecolor=card, font=(f, 10))
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=text)
        style.configure("TLabelframe", background=bg, bordercolor=theme.c("border"))
        style.configure("TLabelframe.Label", background=bg, foreground=muted, font=(f, 10, "bold"))
        style.configure("TButton", background=card, foreground=text, padding=(10, 5), borderwidth=1)
        style.map("TButton", background=[("active", theme.c("card_hover"))])
        style.configure("TNotebook", background=bg, borderwidth=0)
        style.configure("TNotebook.Tab", background=theme.c("panel"), foreground=muted, padding=(14, 6))
        style.map("TNotebook.Tab", background=[("selected", card)], foreground=[("selected", acc)])
        style.configure("Treeview", background=card, fieldbackground=card, foreground=text, rowheight=24)
        style.map("Treeview", background=[("selected", acc)], foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=theme.c("panel"), foreground=muted)
        style.configure("TEntry", fieldbackground=card, foreground=text, insertcolor=text)
        style.configure("TCombobox", fieldbackground=card, foreground=text)
        style.configure("TCheckbutton", background=bg, foreground=text)
        style.configure("TRadiobutton", background=bg, foreground=text)
        style.configure("Horizontal.TProgressbar", background=acc, troughcolor=theme.c("track"))
        self.option_add("*Text.background", card)
        self.option_add("*Text.foreground", text)


class App(ExpertMixin, tk.Tk):
    """Classic expert UI as the main window (python -m sectorsmith --classic)."""

    def __init__(self):
        tk.Tk.__init__(self)
        self._init_expert(hosted=False)


class AdvancedWindow(ExpertMixin, tk.Toplevel):
    """Expert UI opened from the new interface's 'Advanced tools' button."""

    def __init__(self, master):
        tk.Toplevel.__init__(self, master)
        self._init_expert(hosted=True)
        for cv in (self.map, self.surf_canvas):
            cv.configure(background=self._pal("card"), highlightthickness=0)


from .util import relaunch_as_admin  # noqa: E402,F401  (kept for compatibility)


def main():
    import sys
    if "--agent" in sys.argv:
        if "--no-elevate" not in sys.argv and relaunch_as_admin():
            return
        from .link.agent import main as agent_main
        sys.exit(agent_main(sys.argv[1:]))
    if "--no-elevate" not in sys.argv and relaunch_as_admin():
        return
    if "--classic" in sys.argv:
        App().mainloop()
        return
    try:
        from .ui.main import run
    except ImportError as e:  # customtkinter missing -> fall back to the classic UI
        log.warning("New UI unavailable (%s); starting classic UI", e)
        App().mainloop()
        return
    run()
