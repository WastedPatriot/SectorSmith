"""Headless GUI smoke test: drives every tab against the test image and screenshots each.

Run: xvfb-run -s "-screen 0 1400x900x24" python3.12 tests/gui_smoke.py <workdir> <shots_dir>
"""
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tkinter import filedialog, messagebox  # noqa: E402

from sectorsmith import app as appmod  # noqa: E402

W, SHOTS = sys.argv[1], sys.argv[2]
os.makedirs(SHOTS, exist_ok=True)
img = os.path.join(W, "gui.img")
shutil.copyfile(os.path.join(W, "disk.img"), img)
# destroy table on a second copy for partition recovery
lost = os.path.join(W, "gui_lost.img")
shutil.copyfile(img, lost)
with open(lost, "r+b") as f:
    f.write(bytes(34 * 512))

msgs = []
for name in ("showinfo", "showerror", "showwarning"):
    setattr(messagebox, name, lambda t, m="", _n=name, **k: msgs.append((_n, t, m)))
messagebox.askyesno = lambda *a, **k: True
appmod.messagebox = messagebox
answers = {}
filedialog.askdirectory = lambda **k: answers.get("dir", W)
filedialog.askopenfilename = lambda **k: answers.get("open", img)
appmod.filedialog = filedialog

a = appmod.App()
a.confirm_typed = lambda *x: True
a.geometry("1360x860+0+0")


def pump(sec=0.3):
    end = time.time() + sec
    while time.time() < end:
        a.update()
        time.sleep(0.02)


def wait_job(timeout=60):
    t = time.time()
    pump(0.3)
    while a.job_prog is not None and time.time() - t < timeout:
        pump(0.1)
    pump(0.4)


def shot(name):
    pump(0.5)
    subprocess.run(["import", "-window", "root", os.path.join(SHOTS, name + ".png")], check=False)


answers["open"] = img
a.open_image_dialog()
pump()
a.devices[-1]  # noqa: B018
idx = len(a.devices) - 1
a.tree.selection_set(f"d{idx}p1")  # NTFS partition
pump()
a.nb.select(0)
shot("01_overview")
assert a.cur_part.fs == "NTFS", a.cur_part

# hex editor: go to NTFS boot sector, edit a byte, write, read back
a.nb.select(2)
a.hex.goto_partition()
pump()
assert a.hex.data[3:11] == b"NTFS    "
a.hex.text.mark_set("insert", "1.14")
a.hex._update_inspector()
shot("02_hex")
a.hex.goto(a.cur_part.start_lba + 1000)
a.hex.edit_mode.set(True)
a.hex.text.mark_set("insert", "1.14")


class Ev:
    def __init__(self, ch):
        self.char, self.keysym, self.state = ch, ch, 0


for ch in "CAFE":
    a.hex._on_key(Ev(ch))
pump()
shot("03_hex_edit")
a.hex.write_sector()
pump()
assert a.hex.data[:2] == b"\xca\xfe", a.hex.data[:4]
print("hex edit+write OK")
a.hex.find_var.set("FILE0")
a.hex.find_next()
wait_job()
print("find ->", a.hex.lba)

# surface
a.nb.select(3)
a.surf_scope.set("disk")
a.start_surface()
wait_job()
shot("04_surface")

# wipe partition 1 (FAT32) with DoD
a.tree.selection_set(f"d{idx}p0")
pump()
a.nb.select(1)
a.wipe_target.set("part")
a._wipe_target_changed()
a.wipe_method.set("DoD 5220.22-M (3 pass + verify)")
a._wipe_method_changed()
a.start_wipe()
pump(0.6)
shot("05_wipe_running")
wait_job()
shot("06_wipe_done")
print("wipe msgs:", msgs[-1])

# imaging to vhd
a.nb.select(4)
a.img_mode.set("toimage")
a.img_path.set(os.path.join(W, "gui_out.vhd"))
a.start_imaging()
wait_job()
shot("07_imaging")
assert os.path.getsize(os.path.join(W, "gui_out.vhd")) == 256 * 1024 * 1024 + 512

# partition recovery on the destroyed copy
answers["open"] = lost
a.open_image_dialog()
pump()
lidx = len(a.devices) - 1
a.tree.selection_set(f"d{lidx}")
pump()
a.nb.select(5)
a.ps_mode.set("full")
a.start_partscan()
wait_job()
shot("08_partscan")
assert len(a.found_parts) == 3, a.found_parts
a.fp_tree.selection_set(a.fp_tree.get_children())
a.restore_found()
wait_job()
pump(0.5)
newidx = [i for i, d in enumerate(a.devices) if d.path == lost][0]
assert a.tables[lost].scheme in ("MBR", "GPT") and len(a.tables[lost].partitions) == 3, a.tables[lost]
print("restored table:", a.tables[lost].scheme)

# NTFS undelete browse on recovered disk
a.tree.selection_set(f"d{newidx}p1")
pump()
a.nb.select(6)
a.fr_lba.set(str(a.cur_part.start_lba))
a.fr_deleted.set(False)
a.start_ntfs_scan()
wait_job()
shot("09_ntfs")
names = [a.fr_tree.item(i)["values"][0] for i in a.fr_tree.get_children()]
assert "bigfile.bin" in names, names[:20]

# carving
out = os.path.join(W, "gui_carve")
shutil.rmtree(out, ignore_errors=True)
a.carve_out.set(out)
a.carve_range.set("")
a.start_carve()
wait_job()
shot("10_carve")
print("carve:", a.carve_status.cget("text"))

errors = [m for m in msgs if m[0] == "showerror"]
print("errors:", errors)
print("ALL GUI CHECKS PASSED" if not errors else "GUI ERRORS")
a.destroy()
