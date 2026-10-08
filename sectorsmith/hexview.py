"""Sector / hex editor widget with a data inspector."""
from __future__ import annotations

import struct
import tkinter as tk
from tkinter import messagebox, ttk

from .partitions import detect_fs

HEX_COL = 14  # "XXXXXXXXXXXX  "
ASC_COL = HEX_COL + 47 + 2  # hex area is 47 chars, then two spaces
PRINTABLE = {i: chr(i) for i in range(32, 127)}


def describe_sector(lba: int, data: bytes) -> str:
    if len(data) < 512:
        return ""
    if data[:8] == b"EFI PART":
        return "GPT header"
    if data[:4] == b"FILE":
        return "NTFS MFT record"
    if data[:4] == b"INDX":
        return "NTFS index record"
    fs, _ = detect_fs(data + b"\0" * 4096)
    if fs:
        return f"{fs} boot sector / superblock"
    if data[510:512] == b"\x55\xaa":
        if lba == 0:
            return "MBR (with boot signature)"
        return "Boot sector / EBR (55AA signature)"
    if data == bytes(len(data)):
        return "All zeros"
    if data == b"\xff" * len(data):
        return "All 0xFF"
    return ""


class HexView(ttk.Frame):
    """Shows one sector at a time. ``app`` provides confirm/run_job/clone helpers."""

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.dev = None
        self.lba = 0
        self.data = b""
        self.edit = bytearray()
        self.dirty: set[int] = set()
        self.edit_mode = tk.BooleanVar(value=False)
        self._build()

    # ------------------------------------------------------------------ UI
    def _build(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text="LBA").pack(side="left")
        self.lba_var = tk.StringVar(value="0")
        e = ttk.Entry(bar, textvariable=self.lba_var, width=14)
        e.pack(side="left", padx=2)
        e.bind("<Return>", lambda _e: self.goto_entry())
        ttk.Button(bar, text="Go", width=4, command=self.goto_entry).pack(side="left")
        for txt, fn in (("|<", lambda: self.goto(0)), ("<", lambda: self.goto(self.lba - 1)),
                        (">", lambda: self.goto(self.lba + 1)),
                        (">|", lambda: self.goto(self.dev.total_sectors - 1) if self.dev else None)):
            ttk.Button(bar, text=txt, width=3, command=fn).pack(side="left", padx=1)
        ttk.Button(bar, text="Partition start", command=self.goto_partition).pack(side="left", padx=(8, 1))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(bar, text="Find").pack(side="left")
        self.find_var = tk.StringVar()
        fe = ttk.Entry(bar, textvariable=self.find_var, width=22)
        fe.pack(side="left", padx=2)
        fe.bind("<Return>", lambda _e: self.find_next())
        self.find_mode = tk.StringVar(value="text")
        ttk.Combobox(bar, textvariable=self.find_mode, values=("text", "hex"), width=5,
                     state="readonly").pack(side="left")
        ttk.Button(bar, text="Find next", command=self.find_next).pack(side="left", padx=2)

        bar2 = ttk.Frame(self)
        bar2.pack(fill="x", pady=(0, 4))
        ttk.Checkbutton(bar2, text="Edit mode", variable=self.edit_mode,
                        command=self._toggle_edit).pack(side="left")
        self.write_btn = ttk.Button(bar2, text="Write sector to disk", command=self.write_sector, state="disabled")
        self.write_btn.pack(side="left", padx=4)
        ttk.Button(bar2, text="Revert", command=self.revert).pack(side="left")
        ttk.Button(bar2, text="Save sector as…", command=self.save_sector).pack(side="left", padx=4)
        self.info = ttk.Label(bar2, text="", foreground="#555")
        self.info.pack(side="right")

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.text = tk.Text(body, font=("Consolas", 10), wrap="none", width=82, undo=False,
                            background="#fbfbfb", insertbackground="#000")
        sb = ttk.Scrollbar(body, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.text.tag_configure("off", foreground="#2b5797")
        self.text.tag_configure("dirty", foreground="#c00000", font=("Consolas", 10, "bold"))
        self.text.tag_configure("hit", background="#ffe08a")
        self.text.tag_configure("cur", background="#cfe3ff")
        self.text.bind("<Key>", self._on_key)
        self.text.bind("<ButtonRelease-1>", lambda _e: self._update_inspector())
        self.text.bind("<KeyRelease>", lambda _e: self._update_inspector())

        insp = ttk.LabelFrame(body, text="Data inspector", padding=6)
        insp.pack(side="left", fill="y", padx=(6, 0))
        self.insp_vars = {}
        for k in ("Offset", "UInt8", "Int8", "UInt16 LE", "Int16 LE", "UInt32 LE", "Int32 LE", "UInt64 LE",
                  "UInt16 BE", "UInt32 BE", "Binary", "As LBA (u32)"):
            row = ttk.Frame(insp)
            row.pack(fill="x")
            ttk.Label(row, text=k, width=12).pack(side="left")
            v = tk.StringVar()
            ttk.Entry(row, textvariable=v, width=22, state="readonly").pack(side="left")
            self.insp_vars[k] = v
        self.kind_var = tk.StringVar()
        ttk.Label(insp, textvariable=self.kind_var, foreground="#2b5797", wraplength=230).pack(fill="x", pady=8)

    # ------------------------------------------------------------- loading
    def set_device(self, dev):
        self.dev = dev
        self.goto(0)

    def goto_entry(self):
        s = self.lba_var.get().strip().lower()
        try:
            v = int(s, 16) if s.startswith("0x") else int(s)
        except ValueError:
            return
        self.goto(v)

    def goto_partition(self):
        p = self.app.cur_part
        if p is not None:
            self.goto(p.start_lba)

    def goto(self, lba: int, highlight: tuple[int, int] | None = None):
        if self.dev is None:
            return
        if self.dirty and not messagebox.askyesno("Unsaved edits", "Discard unsaved changes to this sector?"):
            return
        lba = max(0, min(lba, self.dev.total_sectors - 1))
        try:
            self.data = self.dev.read_sectors(lba, 1)
        except OSError as e:
            self.data = b""
            self.text.delete("1.0", "end")
            self.text.insert("end", f"Cannot read LBA {lba}:\n{e}")
            self.lba = lba
            self.lba_var.set(str(lba))
            return
        self.lba = lba
        self.lba_var.set(str(lba))
        self.edit = bytearray(self.data)
        self.dirty.clear()
        self.render()
        if highlight:
            self._tag_range(*highlight, "hit")
        self.info.configure(text=f"Sector {lba:,} of {self.dev.total_sectors - 1:,}   "
                                 f"byte offset 0x{lba * self.dev.sector_size:X}")
        self.kind_var.set(describe_sector(lba, self.data))
        self.write_btn.configure(state="disabled")

    def render(self):
        ss = self.dev.sector_size if self.dev else 512
        base = self.lba * ss
        y = self.text.yview()[0]
        cur = self.text.index("insert")
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        lines = []
        for row in range(0, len(self.edit), 16):
            chunk = self.edit[row: row + 16]
            hx = " ".join(f"{b:02X}" for b in chunk).ljust(47)
            asc = "".join(PRINTABLE.get(b, ".") for b in chunk)
            lines.append(f"{base + row:012X}  {hx}  {asc}")
        self.text.insert("1.0", "\n".join(lines))
        for ln in range(1, len(lines) + 1):
            self.text.tag_add("off", f"{ln}.0", f"{ln}.12")
        for i in self.dirty:
            self._tag_range(i, 1, "dirty")
        self.text.yview_moveto(y)
        self.text.mark_set("insert", cur)

    def _tag_range(self, start: int, length: int, tag: str):
        for i in range(start, min(start + length, len(self.edit))):
            ln, col = i // 16 + 1, i % 16
            self.text.tag_add(tag, f"{ln}.{HEX_COL + 3 * col}", f"{ln}.{HEX_COL + 3 * col + 2}")
            self.text.tag_add(tag, f"{ln}.{ASC_COL + col}", f"{ln}.{ASC_COL + col + 1}")

    # ------------------------------------------------------------- editing
    def _cursor_byte(self):
        ln, col = map(int, self.text.index("insert").split("."))
        row = (ln - 1) * 16
        if HEX_COL <= col < HEX_COL + 48:
            rel = col - HEX_COL
            return row + rel // 3, "hex", rel % 3
        if ASC_COL <= col < ASC_COL + 16:
            return row + col - ASC_COL, "asc", 0
        return None, None, None

    def _on_key(self, ev):
        nav = {"Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next"}
        if ev.keysym in nav or (ev.state & 0x4 and ev.keysym.lower() in ("c", "a")):
            return None
        if ev.keysym == "Next" or ev.keysym == "Prior":
            return None
        if not self.edit_mode.get():
            return "break"
        idx, area, nib = self._cursor_byte()
        if idx is None or idx >= len(self.edit):
            return "break"
        ch = ev.char
        if area == "hex":
            if nib == 2:
                idx += 1
                nib = 0
                if idx >= len(self.edit):
                    return "break"
            if ch and ch.lower() in "0123456789abcdef":
                v = int(ch, 16)
                old = self.edit[idx]
                self.edit[idx] = (v << 4) | (old & 0x0F) if nib == 0 else (old & 0xF0) | v
                self._mark(idx)
                ln, col = idx // 16 + 1, idx % 16
                nxt = HEX_COL + 3 * col + (1 if nib == 0 else 3)
                if nib == 1 and col == 15:
                    ln, nxt = ln + 1, HEX_COL
                self.render()
                self.text.mark_set("insert", f"{ln}.{nxt}")
        elif area == "asc" and ch and 32 <= ord(ch) < 127:
            self.edit[idx] = ord(ch)
            self._mark(idx)
            ln, col = idx // 16 + 1, idx % 16
            self.render()
            self.text.mark_set("insert", f"{ln}.{ASC_COL + 1 + col}" if col < 15 else f"{ln + 1}.{ASC_COL}")
        self._update_inspector()
        return "break"

    def _mark(self, idx):
        if self.edit[idx] != self.data[idx]:
            self.dirty.add(idx)
        else:
            self.dirty.discard(idx)
        self.write_btn.configure(state="normal" if self.dirty else "disabled")

    def _toggle_edit(self):
        if self.edit_mode.get():
            if self.dev is None:
                self.edit_mode.set(False)
                return
            if self.dev.is_system:
                messagebox.showwarning("System disk", "Editing the system disk is blocked for safety.")
                self.edit_mode.set(False)
                return
            messagebox.showinfo("Edit mode",
                                "Type hex digits over the hex columns, or characters over the text column.\n"
                                "Changes stay in memory (red) until you press 'Write sector to disk'.")

    def revert(self):
        self.edit = bytearray(self.data)
        self.dirty.clear()
        self.render()
        self.write_btn.configure(state="disabled")

    def write_sector(self):
        if not self.dirty or self.dev is None:
            return
        msg = (f"Write {len(self.dirty)} changed byte(s) to LBA {self.lba} on {self.dev.describe()}?\n\n"
               "On Windows, volumes on this disk are locked and dismounted while writing.")
        if not self.app.confirm_typed("Write sector", msg, "WRITE"):
            return
        wdev = self.app.clone_device(self.dev)
        try:
            wdev.open(writable=True)
            wdev.write_sectors(self.lba, bytes(self.edit))
            wdev.flush()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Write failed", str(e))
            return
        finally:
            wdev.close()
        self.dev.close()  # drop any cached read handle
        self.dirty.clear()
        self.goto(self.lba)
        self.app.status(f"Sector {self.lba} written.")

    def save_sector(self):
        from tkinter import filedialog
        p = filedialog.asksaveasfilename(defaultextension=".bin", initialfile=f"LBA_{self.lba}.bin")
        if p:
            with open(p, "wb") as f:
                f.write(bytes(self.edit))

    # ------------------------------------------------------------- inspector
    def _update_inspector(self):
        idx, _area, _ = self._cursor_byte()
        if idx is None or idx >= len(self.edit):
            return
        b = bytes(self.edit[idx: idx + 8]).ljust(8, b"\0")
        ss = self.dev.sector_size if self.dev else 512
        vals = {
            "Offset": f"{idx} (0x{idx:X}) abs 0x{self.lba * ss + idx:X}",
            "UInt8": b[0], "Int8": struct.unpack("<b", b[:1])[0],
            "UInt16 LE": struct.unpack("<H", b[:2])[0], "Int16 LE": struct.unpack("<h", b[:2])[0],
            "UInt32 LE": struct.unpack("<I", b[:4])[0], "Int32 LE": struct.unpack("<i", b[:4])[0],
            "UInt64 LE": struct.unpack("<Q", b)[0], "UInt16 BE": struct.unpack(">H", b[:2])[0],
            "UInt32 BE": struct.unpack(">I", b[:4])[0], "Binary": f"{b[0]:08b}",
            "As LBA (u32)": struct.unpack("<I", b[:4])[0],
        }
        for k, v in vals.items():
            self.insp_vars[k].set(str(v))
        self.text.tag_remove("cur", "1.0", "end")
        self._tag_range(idx, 1, "cur")

    # ------------------------------------------------------------- search
    def find_next(self):
        if self.dev is None or not self.find_var.get():
            return
        try:
            pat = bytes.fromhex(self.find_var.get()) if self.find_mode.get() == "hex" else \
                self.find_var.get().encode("utf-8")
        except ValueError:
            messagebox.showerror("Find", "Invalid hex string.")
            return
        ss = self.dev.sector_size
        start = (self.lba + 1) * ss
        dev = self.app.clone_device(self.dev)
        label = f"Searching for {self.find_var.get()!r}"

        def job(prog):  # runs in a worker thread: no tkinter calls in here
            total = dev.usable_size
            prog.reset(total - start, label)
            pos = start
            chunk = 8 * 1024 * 1024
            try:
                while pos < total:
                    prog.check()
                    buf = dev.read(pos, chunk + len(pat) - 1)
                    i = buf.find(pat)
                    if i != -1:
                        return pos + i
                    pos += chunk
                    prog.update(pos - start)
            finally:
                dev.close()
            return None

        def done(res):
            if res is None:
                messagebox.showinfo("Find", "Not found before the end of the disk.")
                return
            lba, off = divmod(res, ss)
            self.goto(lba, highlight=(off, min(len(pat), ss - off)))
            self.app.status(f"Found at byte 0x{res:X} (LBA {lba}, offset {off}).")

        self.app.run_job("Search", job, done)
