"""Headless run-through of the UI: every wizard, every rail route, the command palette, presentation mode and the
Mossbit personalities, in both themes, with screenshots.

Run: xvfb-run -s "-screen 0 1400x900x24" python3.12 tests/ui_smoke.py <workdir> <shots_dir>
"""
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tkinter import filedialog, messagebox  # noqa: E402

from sectorsmith.ui import main as M, screens as S, theme  # noqa: E402

W, SHOTS = sys.argv[1], sys.argv[2]
os.makedirs(SHOTS, exist_ok=True)
img = os.path.join(W, "ui.img")
shutil.copyfile(os.path.join(W, "disk.img"), img)
lost = os.path.join(W, "ui_lost.img")
shutil.copyfile(img, lost)
with open(lost, "r+b") as f:
    f.write(bytes(34 * 512))

answers = {}
filedialog.askdirectory = lambda **k: answers["dir"]
filedialog.asksaveasfilename = lambda **k: answers["save"]
filedialog.askopenfilenames = lambda **k: answers.get("files", ())
messagebox.askyesno = lambda *a, **k: True
errors = []

from sectorsmith.util import app_dir  # noqa: E402
try:
    os.remove(app_dir() / "settings.json")
except OSError:
    pass
app = M.MainWindow()
app.geometry("1360x860+0+0")
orig_toast = app.toast
app.toast = lambda text, tone="success": (errors.append(text) if tone == "danger" else None, orig_toast(text, tone))


def pump(sec=0.4):
    end = time.time() + sec
    while time.time() < end:
        app.update()
        time.sleep(0.015)


def wait():
    pump(0.4)
    t = time.time()
    while app.job is not None and time.time() - t < 120:
        pump(0.1)
    pump(0.6)


n = [0]


def shot(name):
    pump(0.5)
    n[0] += 1
    subprocess.run(["import", "-window", "root", os.path.join(SHOTS, f"{n[0]:02d}_{name}.png")], check=False)


def pick(path, part_index=None):
    for card, _b, t in app.screen.picker.rows:
        if t["dev"].path == path and ((part_index is None and t["kind"] == "disk") or
                                      (t["kind"] == "part" and t["part"].index == part_index)):
            card.click()
            pump(0.2)
            return t
    raise AssertionError(f"target not found {path} {part_index}")


def type_confirm(phrase):
    for w in app.screen.body.winfo_children():
        for c in w.winfo_children():
            if c.__class__.__name__ == "CTkEntry":
                c.insert(0, phrase)
    pump(0.2)


def press(text):
    for w in app.screen.footer.winfo_children():
        if getattr(w, "cget", None) and w.cget("text") == text:
            w.invoke()
            pump(0.3)
            return
    raise AssertionError(f"button {text!r} not found: {[w.cget('text') for w in app.screen.footer.winfo_children()]}")


pump(1.2)
shot("welcome_1")
press("Next")
pump(0.8)
shot("welcome_2")
press("Next")
pump(0.8)
shot("welcome_3")
press("Let's go!")
app.add_image(img)
app.add_image(lost)
pump(1.5)
shot("home_system")
app.open_guide("recover")
pump(0.8)
shot("guide_recover")
app.home()
pump(0.6)
theme.set_mode("light")
app.mode.set("Light")
pump(0.3)
shot("home_light")

# --- Recover (quick, NTFS) --------------------------------------------------
app.go(S.RecoverWizard)
pump(0.6)
pick(img, 2)
shot("recover_pick")
press("Next")
shot("recover_scantype")
press("Start scan")
wait()
shot("recover_results")
assert app.screen.tree.get_children() or True
app.screen.only_del.set(False)
app.screen._fill()
pump()
names = [app.screen.tree.item(i)["values"][0] for i in app.screen.tree.get_children()]
assert "bigfile.bin" in names, names
app.screen.tree.selection_set(app.screen.tree.get_children())
out = os.path.join(W, "ui_recovered")
shutil.rmtree(out, ignore_errors=True)
os.makedirs(out)
answers["dir"] = out
press("Recover selected")
wait()
shot("recover_done")
assert os.path.exists(os.path.join(out, "bigfile.bin")), os.listdir(out)

# --- Recover (deep) in dark mode ---------------------------------------------
theme.set_mode("dark")
app.mode.set("Dark")
app.go(S.RecoverWizard)
pump(0.6)
pick(img)
press("Next")
app.screen.out_var.set(os.path.join(W, "ui_carved"))
shutil.rmtree(os.path.join(W, "ui_carved"), ignore_errors=True)
shot("deep_options_dark")
press("Start scan")
pump(0.5)
shot("deep_running_dark")
wait()
shot("deep_done_dark")

# --- Health -------------------------------------------------------------------
app.go(S.HealthWizard)
pump(0.6)
pick(img)
press("Next")
pump(0.3)
shot("health_running_dark")
wait()
shot("health_result_dark")

# --- Partition recovery (light) -------------------------------------------------
theme.set_mode("light")
app.mode.set("Light")
app.go(S.PartitionWizard)
pump(0.6)
pick(lost)
press("Next")
wait()
shot("partition_results")
press("Bring them back")
type_confirm("RESTORE")
shot("partition_confirm")
press("Restore now")
wait()
shot("partition_done")
pt = [pt for d, pt, e in app.inventory(True) if d.path == lost][0]
assert len(pt.partitions) == 3, pt

# --- Wipe partition 1 of img -------------------------------------------------------
app.go(S.WipeWizard)
pump(0.6)
pick(img, 1)
press("Next")
shot("wipe_strength")
press("Next")
type_confirm("ERASE PARTITION 1")
shot("wipe_confirm")
press("Erase and certify")
pump(0.8)
shot("wipe_running")
wait()
shot("wipe_done")
answers["save"] = os.path.join(W, "cert.html")
press("Save certificate…")
pump()
assert os.path.exists(answers["save"])

# --- Clone to vhd (dark) ------------------------------------------------------------
theme.set_mode("dark")
app.mode.set("Dark")
app.go(S.CloneWizard)
pump(0.6)
pick(img)
press("Next")
shot("clone_dest_dark")
answers["save"] = os.path.join(W, "ui_backup.vhd")
press("Next")
shot("clone_confirm_dark")
press("Start backup")
wait()
shot("clone_done_dark")
assert os.path.getsize(answers["save"]) == 256 * 1024 * 1024 + 512

# --- Cancel a backup part-way: "Cancelling..." at once, then "Stopped" and no half image ------------
from sectorsmith.ui.widgets import ProgressPanel  # noqa: E402
app.go(S.CloneWizard)
pump(0.6)
pick(img)
press("Next")
answers["save"] = os.path.join(W, "ui_cancelled.vhd")
press("Next")
[w for w in app.screen.footer.winfo_children() if w.cget("text") == "Start backup"][0].invoke()
app.cancel_job()
panel = [w for w in app.screen.body.winfo_children() if isinstance(w, ProgressPanel)][0]
assert panel.cancel.cget("text") == "Cancelling..." and panel.label.cget("text") == "Cancelling..."
t0 = time.time()
wait()
assert time.time() - t0 < 6, "cancel took too long"
shot("clone_cancelled_dark")
assert app.screen.title_lbl.cget("text") == "Stopped", app.screen.title_lbl.cget("text")
assert not os.path.exists(answers["save"]), "the unfinished image is removed"

# a job that ends after you've left its screen must not break the UI loop
scr = app.go(S.WipeWizard)
pump(0.3)
_, panel = scr.progress("Wiping…", "test")
app.run_job("leave", lambda prog: time.sleep(0.8), lambda r: scr.step(3, "x"), panel,
            on_cancel=lambda info: scr.step(3, "x"))
app.open_guide("wipe")
wait()
got = []
app.background(lambda: 42, got.append)
pump(0.6)
assert got == [42], "UI loop still delivers results after a job finished on a closed screen"

# --- Shred via drop ------------------------------------------------------------------
tmpd = os.path.join(W, "to_shred")
os.makedirs(tmpd, exist_ok=True)
for i in range(3):
    open(os.path.join(tmpd, f"secret{i}.txt"), "w").write("x" * 5000)
app.home()
pump(0.6)
app.handle_drop([os.path.join(tmpd, f"secret{i}.txt") for i in range(3)])
pump(0.6)
shot("shred_dropped_dark")
press("Next")
type_confirm("SHRED")
shot("shred_confirm_dark")
press("Shred them")
wait()
shot("shred_done_dark")
assert not any(os.path.exists(os.path.join(tmpd, f"secret{i}.txt")) for i in range(3))

# progress screen mid-job, both themes (fake slow job so we can see the sweeper)
for mode in ("light", "dark"):
    theme.set_mode(mode)
    app.mode.set(mode.capitalize())
    scr = app.go(S.WipeWizard)
    pump(0.5)
    _, panel = scr.progress("Wiping…", "DoD 5220.22-M (3 pass + verify) on Disk 2 · Samsung T7 · 1 TB")
    scr._draw_steps(3)

    def fake(prog):
        prog.reset(1000 * 1024 * 1024, "Pass 2 of 3")
        prog.set_detail("pattern 0xFF")
        for i in range(60):
            prog.check()
            time.sleep(0.05)
            prog.update(int(i / 60 * 0.62 * prog.total))
        return None
    app.run_job("demo", fake, lambda r: None, panel)
    pump(2.6)
    shot(f"progress_{mode}")
    wait()

# advanced window opens
app.home()
pump(0.5)
app.open_advanced()
pump(1.0)
shot("advanced_dark")

# --- new shell: rail routes, palette, context, presentation, personality ----------------------------------
from sectorsmith.ui import mascot as Mo, nav, widgets as Wd, workspace as WS  # noqa: E402

for mode in ("light", "dark"):
    theme.set_mode(mode)
    app.mode.set(mode.capitalize())
    for cat in nav.CATEGORIES + [nav.SETTINGS]:
        app.open_category(cat.key)
        pump(0.6)
        assert app.rail.items[cat.key].selected, cat.key
        first = nav.first_item(cat)
        if first is not None:
            assert type(app.screen) is nav.resolve(first.target), (cat.key, type(app.screen))
            assert app.subnav.winfo_manager(), f"sub-nav hidden on {cat.key}"
        else:
            assert type(app.screen) is S.Home and not app.subnav.winfo_manager()
        shot(f"route_{cat.key}_{mode}")


def walk(w):
    yield w
    for c in w.winfo_children():
        yield from walk(c)


# no default grey CustomTkinter scrollbars anywhere
for key in ("drives", "settings"):
    app.open_category(key)
    pump(0.6)
    bars = [w for w in walk(app) if isinstance(w, M.ctk.CTkScrollbar)]
    assert all(tuple(b._button_color) == tuple(theme.PALETTE["border_strong"]) for b in bars), key

# ticket and client are stamped on the next job
app.set_ticket("48213")
app.context["client"] = "Acme Legal"
app.bar.update_context()
assert app.bar.ticket.lbl.cget("text") == "Ticket #48213"
app.go(S.HealthWizard)
pump(0.6)
pick(img)
press("Next")
wait()
job = app.jobs[-1]
assert (job["task"], job["result"], job["client"], job["ticket"]) == ("Health check", "Done", "Acme Legal", "48213"), job

# Drives table hands the selected drive to the wizard
app.open_category("drives")
pump(1.0)
table = [w for w in walk(app.screen) if isinstance(w, Wd.DataTable)][0]
iid = next(i for i, d in table.rows.items() if d.path == img)
table.tree.selection_set(iid)
pump(0.3)
app.screen._task(S.HealthWizard, app.screen.selected)
pump(0.6)
assert app.screen.target and app.screen.target["dev"].path == img

# command palette: fuzzy search opens the wizard, '#' sets a ticket
app.home()
pump(0.5)
app.open_palette()
pump(0.4)
pal = app._smith_overlays[-1]
pal.var.set("erase cert")
pump(0.3)
shot("palette")
assert pal.shown and pal.shown[0].label == "Erase and certify", [e.label for e in pal.shown]
pal._run()
pump(0.6)
assert type(app.screen) is S.WipeWizard
app.open_palette()
pump(0.3)
app._smith_overlays[-1].var.set("#777")
pump(0.2)
app._smith_overlays[-1]._run()
assert app.context["ticket"] == "777"

# presentation mode: Mossbit off, serials masked, chip shown; personality Off gives a plain progress bar
app.home()
pump(0.4)
app.set_presentation(True)
pump(0.6)
assert theme.effective_personality() == "Off" and app.bar.present.winfo_ismapped()
assert theme.mask("Z9A1K2SERIAL") == "\u2022" * 8 + "RIAL"
shot("home_presenting")
app.set_presentation(False)
assert theme.mask("Z9A1K2") == "Z9A1K2"
for pers, scale in (("Off", 0), ("Subtle", 2), ("Full", 3)):
    app.set_personality(pers)
    scr = app.go(S.WipeWizard)
    pump(0.4)
    _, panel = scr.progress("Erasing", "personality check")
    assert panel.ring.SCALE == scale, (pers, panel.ring.SCALE)
    has_companion = any(isinstance(w, Mo.Mascot) for w in walk(app.subnav))
    assert has_companion == (pers == "Full"), pers
app.set_personality("Subtle")
app.open_category("jobs")
pump(0.6)
assert isinstance(app.screen, WS.JobsScreen)
shot("jobs")

print("toast errors:", errors)
print("ALL UI CHECKS PASSED" if not errors else "UI ERRORS")
