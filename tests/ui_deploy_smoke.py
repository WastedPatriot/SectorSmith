"""Headless run through Manage: every page empty and full, a package built from a fake installer, script export,
the catalogue with a fake winget, clients and baselines, tasks, deployments with schedules, a real maintenance run
and an onboarding check on this PC, scheduled runs, presentation mode and both themes (only touches files under
<workdir>).

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
with open(fake_inv, "w") as fh:
    json.dump([{"key": "FakeApp", "name": "Fake App", "version": "1.0.0", "publisher": "Fake Co", "scope": "machine",
                "uninstall": "", "quiet_uninstall": "", "system_component": False}], fh)
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

from sectorsmith.deploy import schedule as sch  # noqa: E402
from sectorsmith.ui import deploy_screens as D, main as M, manage_schedule as MSch, manage_screens as MS, nav, \
    theme  # noqa: E402
from sectorsmith.ui.guide import save_settings  # noqa: E402
from sectorsmith.util import Cancelled, check_cancel  # noqa: E402

answers = {}
filedialog.askdirectory = lambda **k: answers["dir"]
messagebox.askyesno = lambda *a, **k: True

# a fake winget for the catalogue: never the network
WINGET_OUT = ("   - \r   \\ \r\r"
              "Name                 Id                          Version     Match       Source\n"
              "-------------------------------------------------------------------------------\n"
              "7-Zip                7zip.7zip                   24.08                   winget\n"
              "7-Zip ZS             mcmilk.7zip-zstd            24.08.0.1   Tag: 7zip   winget\n"
              "NanaZip Preview…     M2Team.NanaZip.Preview      5.0.1252.0  Tag: 7-zip  winget\n")
winget_calls, winget_cancelled = [], []


def fake_winget(args, timeout):
    winget_calls.append(args[2])
    if args[2] == "slow":
        try:
            while True:
                check_cancel()
                time.sleep(.05)
        except Cancelled:
            winget_cancelled.append(args[2])
            raise
    return 0, WINGET_OUT if "zip" in args[2] else "No package found matching input criteria.\n"


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


def cells(tree, iid):
    return [str(v).strip() for v in tree.item(iid)["values"]]


def table_text(scr):
    """Every row's cells, for DataTable and RowTable pages."""
    t = scr.table
    if hasattr(t, "tree"):
        return [cells(t.tree, i) for i in t.tree.get_children()]
    out = []
    for frame, _data in t.rows:
        out.append([w.cget("text") for w in frame.winfo_children() if w.__class__.__name__ == "CTkLabel"])
    return out


def store():
    return app.deploy_store()


PAGES = [("library", MS.PackagesScreen), ("catalogue", MS.CatalogueScreen), ("deployments", MS.DeploymentsScreen),
         ("clients", MS.ClientsScreen), ("tasks", MS.TasksScreen), ("sessions", MS.SessionsScreen)]
host = socket.gethostname()
pump(1)
shot("home_light")
assert D.DeployScreen is MS.PackagesScreen, "the old Deploy screen name opens Packages"

# --- every page, empty: the rail and sub-nav land on the right one --------------------------------------
for key, cls in PAGES:
    app.open_target("manage", key)
    pump(.5)
    assert type(app.screen) is cls, (key, type(app.screen))
    assert app.rail.items["manage"].selected and nav.locate(cls) == ("manage", key)
    if key != "catalogue":
        assert not app.screen.table.rows, key
shot("sessions_empty")

# --- a broken installer is reported, not crashed on ------------------------------
app.open_target("manage", "library")
pump(.4)
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
with open(os.path.join(folder, "Install.ps1"), encoding="utf-8-sig") as fh:
    install_ps1 = fh.read()
assert "/S" in install_ps1
press("Back to library")
assert isinstance(app.screen, MS.PackagesScreen) and app.screen.panel.buttons.get("Deploy..."), \
    "back on Packages with the new package selected"

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

# --- the catalogue: curated list, live winget search (fake), cancel, add to library ---------------------------
MS.CatalogueScreen.runner = fake_winget
MS.CatalogueScreen.debounce_ms = 50
app.open_target("manage", "catalogue")
pump(.5)
scr = app.screen
assert len(scr.table.rows) >= 60 and "winget found" in scr.status.cget("text")
scr.search_var.set("slow")
until(lambda: "slow" in winget_calls, 10)
scr.search_var.set("zip")
until(lambda: scr.live_query == "zip", 10)
assert winget_cancelled == ["slow"], "typing again cancels the search still running"
names = [r[0] for r in table_text(scr)]
assert names[0] == "7-Zip" and "7-Zip ZS" in names and "NanaZip Preview" in names, names
assert [r[3] for r in table_text(scr)][0] == "Added", "7-Zip is already in the library"
zs = next(d for d in scr.table.rows.values() if d["name"] == "7-Zip ZS")
scr._show(zs)
shot("catalogue_light")
press("Add to library", scr.panel)
added = next(p for p in store().packages if p.winget_id == "mcmilk.7zip-zstd")
assert added.kind == "winget" and added.detection == {"method": "registry", "value": "7-Zip ZS"}
scr.search_var.set("greenshot")
pump(.6)
gs = next(iter(scr.table.rows.values()))
scr._show(gs)
pump(.2)
press("Add with Chocolatey", scr.panel)
assert next(p for p in store().packages if p.name == "Greenshot").install.startswith("choco install greenshot")
scr.search_var.set("rmm")
pump(.6)
rmm = next(iter(scr.table.rows.values()))
assert rmm.get("placeholder")
scr._show(rmm)
pump(.2)
assert "Build it from your installer" in scr.panel.buttons and "Add to library" not in scr.panel.buttons
MS.CatalogueScreen.runner = None
app.open_target("manage", "catalogue")
pump(.5)
if not __import__("shutil").which("winget"):
    assert "curated list" in app.screen.status.cget("text"), "no winget here: the curated list only"

# --- a client and an upkeep task ---------------------------------------------------
app.go(D.ClientEditor)
pump(.5)
scr = app.screen
scr.name.insert(0, "Front office")
press(f"+ {host}", scr.quick)
shot("client_editor")
press("Save client")
assert store().client_of(host).name == "Front office"
assert isinstance(app.screen, MS.ClientsScreen)

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

# --- deployments: script package to the client, task to this PC, Fake App installed, a two-package bundle ---
app.go(D.DeploymentWizard, item=("software", handy.id))
pump(.5)
scr = app.screen
press("Next")
scr.where_cards["client"].select()
pump(.3)
press("Next")
shot("deployment_how")
press("Save deployment")
assert isinstance(app.screen, MS.DeploymentsScreen)

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

seven = next(p for p in store().packages if p.name == "7-Zip")
app.go(D.DeploymentWizard)
pump(.5)
scr = app.screen
scr.cards[("software", seven.id)].click()
scr.cards[("software", added.id)].click()
pump(.2)
press("Next: 2 packages")
press("Next")
scr.name.insert(0, "Archivers")
scr.onboarding.set(True)
scr.schedule_editor.kind.set("Weekly")
scr.schedule_editor._kind_changed()
scr.schedule_editor.day.set("Friday")
scr.schedule_editor.time.delete(0, "end")
scr.schedule_editor.time.insert(0, "7:30")
shot("deployment_bundle_how")
press("Save deployment")
bundle = next(d for d in store().deployments if d.item_type == "bundle")
assert bundle.items == [seven.id, added.id] and bundle.name == "Archivers" and bundle.onboarding_only
assert bundle.schedule["kind"] == "weekly" and bundle.schedule["time"] == "07:30" and bundle.schedule["day"] == 4
assert {(d.target_kind, d.desired) for d in store().deployments} == {
    ("client", "latest"), ("machine", "enforce"), ("all", "installed"), ("all", "latest")}

# --- Deployments page: pills, filter, switch off and on, schedule from the side panel ----------------------------
scr = app.screen
assert isinstance(scr, MS.DeploymentsScreen) and scr.table.selected is bundle, "the new deployment is selected"
rows = table_text(scr)
assert any(r[0] == "Archivers: 2 packages" and "07:30" in r[2] for r in rows), rows
scr.filter.set("Scheduled")
scr.fill()
assert [r[0] for r in table_text(scr)] == ["Archivers: 2 packages"]
scr.filter.set("All")
scr.fill()
tdep = next(d for d in store().deployments if d.item_type == "task")
scr.table.select(tdep)
pump(.3)
editor = next(w for w in scr.panel.body.winfo_children() if isinstance(w, MS.ScheduleEditor))
editor.kind.set("Daily")
editor._kind_changed()
editor.time.delete(0, "end")
editor.time.insert(0, "25:99")
editor.save()
assert not (store().get("deployments", tdep.id).schedule or {}), "a bad time isn't saved"
editor.time.delete(0, "end")
editor.time.insert(0, "06:45")
editor.save()
pump(.3)
assert store().get("deployments", tdep.id).schedule["time"] == "06:45"
scr = app.screen
scr.table.select(store().get("deployments", tdep.id))
pump(.3)
shot("deployments_light")
editor = next(w for w in scr.panel.body.winfo_children() if isinstance(w, MS.ScheduleEditor))
editor.kind.set("None")
editor._kind_changed()
editor.save()
assert not store().get("deployments", tdep.id).schedule

# --- packages page: search, filter, details ---------------------------------------------------------------
app.open_target("manage", "library")
pump(.5)
scr = app.screen
assert len(scr.table.rows) == 5, table_text(scr)
scr.search_var.set("zip")
pump(.2)
assert sorted(r[0] for r in table_text(scr)) == ["7-Zip", "7-Zip ZS"]
scr.search_var.set("")
scr.filter.set("Scripts")
scr.fill()
assert sorted(r[0] for r in table_text(scr)) == ["Greenshot", "Handy tool"], "Chocolatey and script packages"
scr.filter.set("All")
scr.fill(pkg.id)
used = {r[0]: r[4] for r in table_text(scr)}
assert used["Fake App"] == "1" and used["7-Zip"] == "1" and used["Greenshot"] == "-", used
shot("packages_light")

# --- baseline for the client, then onboard This PC with it (check only) ------------------------------------
app.open_target("manage", "clients")
pump(.4)
scr = app.screen
client = store().client_of(host)
scr.fill(client.id)
pump(.2)
press("Set up a baseline", scr.panel)
scr = app.screen
assert isinstance(scr, MS.BaselineEditor)
scr.vars[pkg.id].set(True)
scr._tick(pkg.id, scr.vars[pkg.id])
scr.q.set("greenshot")
pump(.2)
shot("baseline_light")
press("Save baseline")
base = store().baseline_for(client.id)
assert base is not None and base.items == [pkg.id] and base.onboarding_only
scr = app.screen
assert isinstance(scr, MS.ClientsScreen) and table_text(scr)[0][2] == "1 package"
press("Onboard a new PC...", scr.panel)
scr = app.screen
assert isinstance(scr, D.RunWizard) and scr.only == [base.id]
scr.checklist.cards[0][0].click()
pump(.2)
press("Check 1 PC")
wait()
plans = {cells(scr.tree, i)[1]: cells(scr.tree, i)[4] for i in scr.tree.get_children()}
assert plans == {"Fake App": "Nothing to do"}, plans
last = store().sessions()[0]
assert last["trigger"] == "onboarding" and last["onboarding"] and last["only"] == [base.id]

# --- maintenance: check, confirm, apply, re-check ---------------------------------------
theme.set_mode("dark")
app.mode.set("Dark")
app.go(D.RunWizard)
pump(.5)
scr = app.screen
scr.checklist.cards[0][0].click()
pump(.2)
shot("run_pick_dark")
press("Check 1 PC")
wait()
shot("run_checked_dark")
plans = {cells(scr.tree, i)[1]: cells(scr.tree, i)[4] for i in scr.tree.get_children()}
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
status = {cells(scr.tree, i)[1]: cells(scr.tree, i)[3] for i in scr.tree.get_children()}
assert status["Handy tool"] == "OK" and status["Spooler running"] == "OK", status

# --- sessions -------------------------------------------------------------------------------
press("Back to Sessions")
pump(.4)
scr = app.screen
assert isinstance(scr, MS.SessionsScreen)
assert len(store().sessions()) == 3
scr.filter.set("Onboarding")
scr.fill()
assert len(scr.table.rows) == 1
scr.filter.set("All")
scr.fill()
scr.table.select(scr.table.rows[0][1])
pump(.3)
shot("sessions_dark")
press("Open session", scr.panel)
scr = app.screen
assert isinstance(scr, D.SessionView)
first = scr.tree.get_children()[0]
scr.tree.selection_set(first)
pump(.4)
assert scr.out.get("1.0", "end").strip() != ""
shot("session_view_dark")

# --- Deployments page shows the latest result of each deployment --------------------------------------------
app.open_target("manage", "deployments")
pump(.5)
scr = app.screen
handy_dep = next(d for d in store().deployments if d.item_id == handy.id)
assert scr.results[handy_dep.id]["compliant"] == 1
shot("deployments_dark")

# --- Cancel while a fix hangs on a PC: Cancelling... then Stopped, and the session is kept ---------------
from sectorsmith.deploy.core import Deployment, Task  # noqa: E402
st = store()
slow = st.upsert("tasks", Task(name="Slow fix", test="exit 1", set="sleep 60", language="shell"))
slow_dep = st.upsert("deployments", Deployment("task", slow.id, "enforce"))
n_sessions = len(st.sessions())
app.go(D.RunWizard)
pump(.5)
scr = app.screen
scr.checklist.cards[0][0].click()
pump(.2)
press("Check 1 PC")
wait()
press("Apply changes")
type_confirm(f"APPLY ON {host.upper()}")
press("Apply now")
until(lambda: app.job is not None and app.job.label.startswith("Set: Slow fix"), 20)
pump(.5)
app.cancel_job()
t0 = time.time()
wait()
assert time.time() - t0 < 6, "cancel took too long"
shot("run_cancelled_dark")
assert scr.title_lbl.cget("text") == "Stopped", scr.title_lbl.cget("text")
last = st.sessions()[0]
assert len(st.sessions()) == n_sessions + 2 and last.get("cancelled"), last.get("summary")  # check + apply
assert {a["name"]: a["status"] for a in last["actions"]}["Slow fix"] == "cancelled"
press("Back to Sessions")
st.delete("tasks", slow.id)

# --- schedules run at start-up and when a PC connects, without a click --------------------------------------
scheduler = MSch.start(app)
assert scheduler is app._deploy_scheduler, "the main window started the scheduler"
os.remove(flag)
tdep = st.get("deployments", tdep.id)
tdep.schedule = sch.make("daily", "00:00", now=time.time() - 3 * 86400)
st.upsert("deployments", tdep)
n_sessions = len(st.sessions())
assert scheduler.check("start", app.machines()), "a due schedule on This PC (named directly) starts a run"
assert MSch.running(app) is not None
until(lambda: scheduler.prog is None, 60)
assert os.path.exists(flag) and scheduler.last["changes"] == 1 and not scheduler.last["failed"], scheduler.last
pump(.4)
assert host.lower() in st.get("deployments", tdep.id).last_runs
ran = st.sessions()[0]
assert ran["trigger"] == "schedule" and len(st.sessions()) == n_sessions + 1
assert [a["name"] for a in ran["actions"]] == ["Spooler running"], "only the scheduled deployment ran"
assert not scheduler.check("start", app.machines()), "not due again until the next slot"
assert app.jobs[-1]["task"] == "Scheduled maintenance" and app.jobs[-1]["result"] == "Done"
tdep = st.get("deployments", tdep.id)
tdep.schedule = sch.make("connect")
st.upsert("deployments", tdep)
assert not scheduler.check("start", app.machines()), "an on-connect schedule waits for a PC to connect"
assert scheduler.check("connect", [app.local_ep])
until(lambda: scheduler.prog is None, 60)
evr = st.upsert("deployments", Deployment("software", handy.id, "installed", schedule=sch.make(
    "daily", "00:00", now=time.time() - 3 * 86400)))
tdep.schedule = {}
st.upsert("deployments", tdep)
assert not scheduler.check("start", app.machines()), "Every PC schedules don't run on the technician's own PC"
st.delete("deployments", evr.id)

# --- presentation mode hides other clients on every Manage page ---------------------------------------------
app.context["client"] = None
app.set_presentation(True)
for key, cls in PAGES:
    app.open_target("manage", key)
    pump(.4)
    text = json.dumps(table_text(app.screen))
    assert "Front office" not in text, (key, text)
    if key == "clients":
        assert table_text(app.screen)[0][0] == "Hidden client"
        app.screen.table.tree.selection_set(next(iter(app.screen.table.rows)))
        pump(.3)
        shot("clients_presenting_dark")
    if key == "deployments":
        assert any("Client: Hidden client" in r for r in table_text(app.screen))
app.set_presentation(False)
app.open_target("manage", "clients")
pump(.4)
assert table_text(app.screen)[0][0] == "Front office"

theme.set_mode("light")
app.mode.set("Light")
app.open_target("manage", "library")
pump(.4)
app.screen.dz.hot(True)
shot("packages_drop_light")
app.screen.dz.hot(False)
app.open_guide("deploy")
pump(.8)
shot("guide_deploy")
press("Start: Deploy software")
assert isinstance(app.screen, MS.PackagesScreen)
app.home()
pump(.6)
shot("home_light_end")
app._quit()
print("toast errors:", errors)
print("ALL DEPLOY UI CHECKS PASSED" if not errors else "DEPLOY UI ERRORS")
sys.exit(1 if errors else 0)
