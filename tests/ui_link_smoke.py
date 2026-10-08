"""Headless UI run of Connect → Move a user → Clone a disk to another PC, with a real agent subprocess.

Run after tests/test_link.py (it creates the fake profiles):
  xvfb-run -s "-screen 0 1360x860x24" python3.12 tests/ui_link_smoke.py <workdir> <shots>
"""
import gc
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
W, SHOTS = sys.argv[1], sys.argv[2]
os.makedirs(SHOTS, exist_ok=True)
old, new = os.path.join(W, "profiles_old"), os.path.join(W, "profiles_new")
shutil.rmtree(new, ignore_errors=True)
os.makedirs(os.path.join(new, "alice"))
os.makedirs(os.path.join(new, "bob"))
os.environ["SECTORSMITH_PROFILES_ROOT"] = old
target = os.path.join(W, "ui_remote_target.img")
with open(target, "wb") as f:
    f.truncate(256 * 1024 * 1024)
src_img = os.path.join(W, "ui_src.img")
shutil.copyfile(os.path.join(W, "disk.img"), src_img)

from sectorsmith.ui import main as M, theme  # noqa: E402
from sectorsmith.ui.guide import save_settings  # noqa: E402
from sectorsmith.ui import link_screens as L  # noqa: E402

save_settings(welcomed=True, mode="Light")
app = M.MainWindow()
app.geometry("1360x860+0+0")
errors = []
orig = app.toast
app.toast = lambda t, tone="success": (errors.append(t) if tone == "danger" else None, orig(t, tone))


# Dead CTkFonts are freed by the cyclic GC on whichever thread triggers it. On a job's worker thread,
# Font.__del__ makes a cross-thread Tk call, and with update() instead of mainloop() tkinter waits 1 s per font
# for a main loop that never runs. By the clone step there are hundreds, so the clone looked hung.
# Collect on this thread only.
gc.disable()


def pump(sec=.4):
    end = time.time() + sec
    while time.time() < end:
        app.update()
        gc.collect(0)
        time.sleep(.015)
    gc.collect()


def wait(t=180):
    pump(.4)
    end = time.time() + t
    while app.job is not None:
        assert time.time() < end, "job still running after %ds" % t
        pump(.1)
    pump(.6)


n = [0]


def shot(name):
    pump(.6)
    n[0] += 1
    subprocess.run(["import", "-window", "root", os.path.join(SHOTS, f"L{n[0]:02d}_{name}.png")], check=False)


def press(text):
    for w in app.screen.footer.winfo_children():
        if w.cget("text") == text or (w.cget("text").startswith(text + " (") and w.cget("state") != "disabled"):
            w.invoke()
            pump(.4)
            return
    raise AssertionError(f"{text}: {[w.cget('text') for w in app.screen.footer.winfo_children()]}")


def until(cond, t=30):
    end = time.time() + t
    while time.time() < end and not cond():
        pump(.2)
    assert cond(), "timed out"


app.add_image(src_img)
pump(1)
shot("home_light")
app.open_connect()
pump(1)
link = app.link
import atexit  # noqa: E402
ag = subprocess.Popen([sys.executable, "-m", "sectorsmith", "--agent", "--headless", "--no-elevate",
                       "--connect", f"127.0.0.1:{link.port}", "--token", link.token, "--pin", link.fingerprint,
                       "--profiles-root", new, "--image", target], cwd=ROOT)
atexit.register(lambda: ag.poll() is None and ag.kill())
until(lambda: len(app.machines()) == 2)
pump(1.5)
shot("connect_linked")
remote = app.machines()[1]

# --- Move a user --------------------------------------------------------------
app.go(L.MigrateWizard)
pump(.6)
scr = app.screen
scr.mpick.cards[0][0].click()
until(lambda: getattr(scr, "profile_cards", None))
scr.profile_cards[0][0].click()
pump(.3)
shot("migrate_from")
press("Next")
scr.mpick2.cards[1][0].click()
until(lambda: getattr(scr, "profile_cards", None) and len(scr.profile_cards) == 2)
assert scr.new_folder_entry.get().endswith("alice"), scr.new_folder_entry.get()  # prefilled with the source user
scr.new_folder_entry.delete(0, "end")
scr.new_folder_entry.insert(0, os.path.join(new, "carol"))
scr.new_folder_btn.invoke()
until(lambda: scr.dst_root == os.path.join(new, "carol"))
pump(.3)
shot("migrate_to")
press("Next")
until(lambda: scr.sizes)
pump(.5)
shot("migrate_what")
press("Next")
until(lambda: scr.apps_ready)
pump(.5)
shot("migrate_apps")
assert scr.install_apps.get(), "a linked PC's profile folder can get apps installed"
press("Start moving")
wait()
shot("migrate_done")
assert os.path.exists(os.path.join(new, "carol", "Desktop", "big video.mp4")), os.listdir(os.path.join(new, "carol"))
assert scr.title_lbl.cget("text") == "Move complete", scr.title_lbl.cget("text")

# Cancel in the middle of a move: "Cancelling..." at once, then a clear "Stopped" screen
from sectorsmith.ui.widgets import ProgressPanel  # noqa: E402
shutil.rmtree(os.path.join(new, "dan"), ignore_errors=True)
scr.dst_root = os.path.join(new, "dan")
[w for w in scr.footer.winfo_children() if w.cget("text") == "Run again"][0].invoke()
app.cancel_job()
panel = [w for w in scr.body.winfo_children() if isinstance(w, ProgressPanel)]
assert panel and panel[0].cancel.cget("text") == "Cancelling...", "panel shows Cancelling..."
wait(15)
shot("migrate_cancelled")
assert scr.title_lbl.cget("text") == "Stopped", scr.title_lbl.cget("text")
assert app.job is None

# --- Clone a disk to another PC ------------------------------------------------------
theme.set_mode("dark")
app.mode.set("Dark")
app.go(L.NetCloneWizard)
pump(.6)
scr = app.screen


def disk_picker():
    return scr.disk_picker


mp = [w for w in scr.body.winfo_children() if isinstance(w, L.MachinePicker)][0]
mp.cards[0][0].click()
until(lambda: disk_picker() is not None)
dp = disk_picker()
[c for c, b, d in dp.rows if d["path"] == src_img][0].click()
shot("netclone_source")
press("Next")
mp = [w for w in scr.body.winfo_children() if isinstance(w, L.MachinePicker)][0]
scr.disk_picker = None
mp.cards[1][0].click()
until(lambda: disk_picker() is not None)
dp = disk_picker()
[c for c, b, d in dp.rows if d["path"] == target][0].click()
shot("netclone_dest")
press("Next")
for w in scr.body.winfo_children():
    for c in w.winfo_children():
        if c.__class__.__name__ == "CTkEntry":
            c.insert(0, f"CLONE TO {remote.label.upper()}")
pump(.3)
shot("netclone_confirm")
press("Start cloning")
pump(1.2)
shot("netclone_running")
wait()
shot("netclone_done")
import hashlib  # noqa: E402
assert hashlib.sha256(open(target, "rb").read()).digest() == hashlib.sha256(open(src_img, "rb").read()).digest()

app.open_guide("migrate")
pump(.8)
shot("guide_migrate")
app._quit()
ag.wait(30)
print("toast errors:", errors)
print("ALL LINK UI CHECKS PASSED" if not errors else "LINK UI ERRORS")
