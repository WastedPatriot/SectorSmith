"""Headless run through the Deploy screens: every tab, a package built from a fake installer, script export,
clients, tasks, deployments and a real maintenance run on this PC (only touches files under <workdir>).

Run: timeout 600 xvfb-run -a -s "-screen 0 1360x860x24" python3.12 tests/ui_deploy_smoke.py <workdir> <shots>
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
W, SHOTS = os.path.abspath(sys.argv[1]), sys.argv[2]
os.makedirs(SHOTS, exist_ok=True)
# keep the library, settings and sessions away from the real ones
shutil.rmtree(os.path.join(W, "appdata"), ignore_errors=True)
os.environ["LOCALAPPDATA"] = os.path.join(W, "appdata")
fake_inv = os.path.join(W, "fake_software.json")
json.dump([{"key": "FakeApp", "name": "Fake App", "version": "1.0.0", "publisher": "Fake Co", "scope": "machine",
            "uninstall": "", "quiet_uninstall": "", "system_component": False}], open(fake_inv, "w"))
os.environ["SECTORSMITH_FAKE_SOFTWARE"] = fake_inv
pcs = os.path.join(W, "pc_state")
shutil.rmtree(pcs, ignore_errors=True)
os.makedirs(pcs)


def ver_entry(key, val):
    return key.encode("utf-16-le") + b"\0\0" + val.encode("utf-16-le") + b"\0\0"


# a fake NSIS installer: the marker analyze looks for, plus version-info strings
setup = os.path.join(W, "FakeAppSetup.exe")
with open(setup, "wb") as f:
    f.write(b"MZ" + bytes(100) + b"Nullsoft.NSIS\0" + bytes(64)
            + ver_entry("ProductName", "Fake App") + ver_entry("CompanyName", "Fake Co")
            + ver_entry("ProductVersion", "2.4.0") + bytes(256))
bogus = os.path.join(W, "broken.msi")
with open(bogus, "wb") as f:
    f.write(b"this is not an msi" * 50)

from tkinter import filedialog, messagebox  # noqa: E402

from sectorsmith.ui import deploy_screens as D, main as M, theme  # noqa: E402
from sectorsmith.ui.guide import save_settings  # noqa: E402

answers = {}
filedialog.askdirectory = lambda **k: answers["dir"]
messagebox.askyesno = lambda *a, **k: True

save_settings(welcomed=True, mode="Light")
app = M.MainWindow()
app.geometry("1360x860+0+0")
errors = []
orig = app.toast
app.toast = lambda t, tone="success": (errors.append(t) if tone == "danger" else None, orig(t, tone))


def pump(sec=.4):
    end = time.time() + sec
    while time.time() < end:
        app.update()
        time.sleep(.015)


def wait():
    pump(.4)
    t = time.time()
    while app.job is not None and time.time() - t < 120:
        pump(.1)
    assert app.job is None, "job timed out"
    pump(.6)


def until(cond, t=30):
    end = time.time() + t
    while time.time() < end and not cond():
        pump(.2)
    assert cond(), "timed out"


n = [0]


def shot(name):
    pump(.6)
    n[0] += 1
    subprocess.run(["import", "-window", "root", os.path.join(SHOTS, f"D{n[0]:02d}_{name}.png")], check=False)


def buttons(w):
    for c in w.winfo_children():
        if c.__class__.__name__ == "CTkButton":
            yield c
        yield from buttons(c)


def press(text, where=None):
    for b in buttons(where or app.screen.footer):
        if b.cget("text") == text and b.cget("state") != "disabled":
            b.invoke()
            pump(.4)
            return
    found = [b.cget("text") for b in buttons(where or app.screen.footer)]
    raise AssertionError(f"button {text!r} not found or disabled: {found}")


def type_confirm(phrase):
    def walk(w):
        for c in w.winfo_children():
            if c.__class__.__name__ == "CTkEntry":
                c.insert(0, phrase)
                return True
            if walk(c):
                return True
        return False
    assert walk(app.screen.body)
    pump(.2)


def store():
    return app.deploy_store()


host = socket.gethostname()
pump(1)
shot("home_light")

# --- every tab, empty ---------------------------------------------------------
app.go(D.DeployScreen)
pump(.6)
scr = app.screen
for tab in D.TABS:
    scr.tabs.set(tab)
    scr._tab()
    pump(.3)
shot("sessions_empty")

# --- a broken installer is reported, not crashed on ------------------------------
scr.tabs.set("Library")
scr._tab()
app.handle_drop([bogus])
pump(.3)
assert isinstance(app.screen, D.PackageBuilder)
until(lambda: "Couldn't read" in app.screen.status.cget("text"))
shot("builder_bad_file")

# --- package from the fake installer (dropped on Home) ---------------------------
app.home()
pump(.5)
app.handle_drop([setup])
pump(.3)
scr = app.screen
assert isinstance(scr, D.PackageBuilder)
until(lambda: getattr(scr, "form", None) is not None and scr.form.winfo_exists())
assert scr.v["name"].get() == "Fake App", scr.v["name"].get()
assert scr.v["version"].get() == "2.4.0", scr.v["version"].get()
assert "/S" in scr.v["install"].get(), scr.v["install"].get()
assert "Start-Process" in scr.preview_box.get("1.0", "end")
scr.v["version"].set("2.4.1")
shot("builder_details")
press("Save package")
until(lambda: scr.title_lbl.cget("text") == "Package saved")
shot("builder_saved")
pkg = next(p for p in store().packages if p.name == "Fake App")
assert pkg.version == "2.4.1" and os.path.exists(store().installer_path(pkg))
out = os.path.join(W, "exported")
shutil.rmtree(out, ignore_errors=True)
os.makedirs(out)
answers["dir"] = out
press("Export scripts...")
until(lambda: any(os.path.exists(os.path.join(out, d, "Detect.ps1")) for d in os.listdir(out)))
folder = os.path.join(out, os.listdir(out)[0])
for f in ("Install.ps1", "Uninstall.ps1", "Detect.ps1", "package.json", "FakeAppSetup.exe"):
    assert os.path.exists(os.path.join(folder, f)), (f, os.listdir(folder))
assert "/S" in open(os.path.join(folder, "Install.ps1"), encoding="utf-8-sig").read()

# --- winget and script packages ---------------------------------------------------
app.go(D.PackageBuilder, mode="winget")
pump(.5)
scr = app.screen
scr.v["name"].set("7-Zip")
scr.v["winget_id"].set("7zip.7zip")
press("Save package")
assert scr.title_lbl.cget("text") == "Package saved"
assert next(p for p in store().packages if p.name == "7-Zip").kind == "winget"

app.go(D.PackageBuilder)
pump(.5)
scr = app.screen
[o for o in scr.body.winfo_children()[-1].winfo_children() if getattr(o, "value", None) == "script"][0].select()
pump(.4)
tool = os.path.join(pcs, "tool.txt")
scr.v["name"].set("Handy tool")
scr.v["version"].set("1.0")
scr.v["install"].set(f'echo 1.0 > "{tool}"')
scr.det_method.set(D.DETECTION["file"])
scr._det_changed()
scr.det_value.set(tool)
pump(.5)
shot("builder_script")
press("Save package")
assert scr.title_lbl.cget("text") == "Package saved", scr.title_lbl.cget("text")
handy = next(p for p in store().packages if p.name == "Handy tool")
assert handy.kind == "script" and handy.detection == {"method": "file", "value": tool}

# --- a client and an upkeep task ---------------------------------------------------
app.go(D.ClientEditor)
pump(.5)
scr = app.screen
scr.name.insert(0, "Front office")
press(f"+ {host}", scr.quick)
shot("client_editor")
press("Save client")
assert store().client_of(host).name == "Front office"

app.go(D.TaskEditor)
pump(.5)
scr = app.screen
flag = os.path.join(pcs, "spooler.ok")
scr.name.insert(0, "Spooler running")
scr.desc.insert(0, "Makes sure the flag file is there")
scr.lang.set(D.LANGUAGES["shell"])
scr.test.insert("1.0", f'test -f "{flag}"')
scr.fix.insert("1.0", f'sleep 3; touch "{flag}"')  # slow enough to catch the live status
shot("task_editor")
press("Save task")
task = store().tasks[0]
assert task.language == "shell"

# --- deployments: script package to the client, task to this PC, Fake App installed ---
app.go(D.DeploymentWizard, item=("software", handy.id))
pump(.5)
scr = app.screen
press("Next")
scr.where_cards["client"].select()
pump(.3)
press("Next")
shot("deployment_how")
press("Save deployment")

app.go(D.DeploymentWizard)
pump(.5)
scr = app.screen
scr.cards[("task", task.id)].click()
press("Next")
scr.where_cards["machine"].select()
pump(.3)
scr.machine_picker.cards[0][0].click()
pump(.3)
assert scr.machine_entry.get() == host
shot("deployment_where")
press("Next")
press("Save deployment")

app.go(D.DeploymentWizard, item=("software", pkg.id))
pump(.5)
scr = app.screen
press("Next")
press("Next")
scr.state_seg.set("Installed")
press("Save deployment")
assert len(store().deployments) == 3
assert {(d.target_kind, d.desired) for d in store().deployments} == {("client", "latest"), ("machine", "enforce"),
                                                                     ("all", "installed")}

# --- tabs with content, both themes ---------------------------------------------------
scr = app.go(D.DeployScreen)
pump(.6)
for tab in D.TABS:
    scr.tabs.set(tab)
    scr._tab()
    pump(.3)
    if tab in ("Library", "Deployments"):
        shot(f"tab_{tab.lower()}_light")
theme.set_mode("dark")
app.mode.set("Dark")
scr.tabs.set("Library")
scr._tab()
scr.dz.hot(True)
shot("tab_library_dark_drop")
scr.dz.hot(False)

# --- maintenance: check, confirm, apply, re-check ---------------------------------------
app.go(D.RunWizard)
pump(.5)
scr = app.screen
scr.checklist.cards[0][0].click()
pump(.2)
shot("run_pick_dark")
press("Check 1 PC")
wait()
shot("run_checked_dark")
plans = {scr.tree.item(i)["values"][1]: scr.tree.item(i)["values"][4] for i in scr.tree.get_children()}
assert plans == {"Handy tool": "Install", "Spooler running": "Fix", "Fake App": "Nothing to do"}, plans
press("Apply changes")
type_confirm(f"APPLY ON {host.upper()}")
shot("run_confirm_dark")
press("Apply now")
pump(.6)
shot("run_applying_dark")
wait()
shot("run_done_dark")
assert os.path.exists(tool) and os.path.exists(flag)
status = {scr.tree.item(i)["values"][1]: scr.tree.item(i)["values"][3] for i in scr.tree.get_children()}
assert status["Handy tool"] == "OK" and status["Spooler running"] == "OK", status

# --- sessions -------------------------------------------------------------------------------
press("Back to Deploy")
pump(.4)
scr = app.screen
assert scr.tabs.get() == "Sessions"
assert len(store().sessions()) == 2
shot("tab_sessions_dark")
press("View", scr.area)
scr = app.screen
assert isinstance(scr, D.SessionView)
first = scr.tree.get_children()[0]
scr.tree.selection_set(first)
pump(.4)
assert scr.out.get("1.0", "end").strip() != ""
shot("session_view_dark")

theme.set_mode("light")
app.mode.set("Light")
app.open_guide("deploy")
pump(.8)
shot("guide_deploy")
press("Start: Deploy software")
assert isinstance(app.screen, D.DeployScreen)
app.home()
pump(.6)
shot("home_light_end")
app._quit()
print("toast errors:", errors)
print("ALL DEPLOY UI CHECKS PASSED" if not errors else "DEPLOY UI ERRORS")
sys.exit(1 if errors else 0)
