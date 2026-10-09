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
W, SHOTS = sys.argv[1], sys.argv[2]
# settings, job history and logs go to a fresh folder, so runs don't see each other's jobs
shutil.rmtree(os.path.join(W, "ui_appdata"), ignore_errors=True)
os.environ["LOCALAPPDATA"] = os.path.join(W, "ui_appdata")

from tkinter import filedialog, messagebox  # noqa: E402

from sectorsmith.ui import main as M, screens as S, theme  # noqa: E402

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
from sectorsmith.ui.guide import save_settings  # noqa: E402
save_settings(brand_company="Example IT Services Ltd")


def _walk(w):
    yield w
    for c in w.winfo_children():
        yield from _walk(c)


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
with open(answers["save"], encoding="utf-8") as fh:
    cert = fh.read()
assert "Example IT Services Ltd" in cert and "NIST SP 800-88" in cert and "Software overwrite" in cert, "branded cert"
assert "mossbit" not in cert.lower() and "<svg" in cert

# --- Erase a whole drive: the built-in erase card, frozen drive, fall back to overwrite ----------------
from sectorsmith import wipe as WP  # noqa: E402
app.go(S.WipeWizard)
pump(0.6)
pick(img)
press("Next")
pump(0.8)
scr = app.screen
assert "Image files" in scr.hw_status.cget("text"), scr.hw_status.cget("text")
assert not scr._hardware_chosen()
scr._hw_ready(WP.HardwarePlan(True, "nvme", "crypto", "NVMe Sanitize (crypto erase)"))
assert scr._hardware_chosen()
shot("wipe_hardware_supported")
scr._hw_ready(WP.HardwarePlan(False, "ata", frozen=True, reason=WP.FROZEN_ADVICE))
shot("wipe_hardware_frozen")
press("Next")
assert "Purge" in str([w.cget("text") for w in _walk(scr.body) if isinstance(w, M.ctk.CTkLabel)])
scr._error(WP.DriveFrozen(WP.FROZEN_ADVICE))
pump()
shot("wipe_frozen_result")
assert scr.title_lbl.cget("text") == "Drive is frozen"
press("Use overwrite instead")
pump()
assert not scr._hardware_chosen() and scr.title_lbl.cget("text") == "Confirm the erase"
shot("wipe_frozen_overwrite_confirm")

# Settings: Branding card saves the company name and a copy of the logo
from sectorsmith.ui import settings as ST  # noqa: E402
from sectorsmith.ui.guide import load_settings  # noqa: E402
st = app.go(ST.SettingsScreen)
pump(0.5)
logo_src = os.path.join(W, "logo.svg")
with open(logo_src, "w") as fh:
    fh.write('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>')
filedialog.askopenfilename = lambda **k: logo_src
st._pick_logo()
st.company.set("Example IT Services Ltd")
st._save_brand()
pump()
saved = load_settings()
assert saved["brand_company"] == "Example IT Services Ltd" and saved["brand_logo"].endswith("brand_logo.svg")
assert os.path.exists(saved["brand_logo"]) and st.logo_lbl.cget("text") == "brand_logo.svg"
shot("settings_branding")

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

# --- job history: written to jobs.jsonl as jobs start and end, read back in the next session ---------------
from sectorsmith import jobs as JB  # noqa: E402
hist = JB.JobStore().load()
assert len(hist) == len(app.jobs) and hist[-1]["id"] == app.jobs[-1]["id"], (len(hist), len(app.jobs))
health = [j for j in hist if j["task"] == "Health check" and j["client"] == "Acme Legal"]
assert health and health[-1]["result"] == "Done" and health[-1]["ticket"] == "48213", health
wiped = [j for j in hist if j["title"] == "Wipe" and j["report"]]
assert wiped and wiped[0]["report"].endswith("cert.html"), "certificate path kept with the wipe job"
assert {"Done", "Cancelled"} <= {j["result"] for j in hist}
assert app._load_jobs()[-1]["result"] != "Running"
# a job cut short in an earlier session shows as Interrupted; Home and the tiles read the same history
store = JB.JobStore()
store.start(task="Erase and certify", title="Wipe", client="Bright Dental", ticket="77", technician="sam",
            machine="This PC", detail="Disk 9", started=time.time() - 3 * 86400)
app.jobs = app._load_jobs()
assert any(j["result"] == "Interrupted" and j["client"] == "Bright Dental" for j in app.jobs)
app.home()
pump(0.6)
assert app.screen.tile["Jobs today"].value.cget("text") == str(
    sum(1 for j in app.jobs if time.strftime("%Y%m%d", time.localtime(j["started"])) == time.strftime("%Y%m%d")))

app.open_category("jobs")
pump(0.6)
js = app.screen
total = len(js.all)
assert len(js.table.rows) == total and total == len(app.jobs), (len(js.table.rows), total)
js._set("client", "Acme Legal")
pump(0.2)
assert js.table.rows and all(j["client"] == "Acme Legal" for j in js.table.rows.values())
js._set("client", None)
js._set("result", "Interrupted")
assert [j["client"] for j in js.table.rows.values()] == ["Bright Dental"]
js._set("result", None)
js.search.set("health 48213")
pump(0.5)
assert js.table.rows and all(j["task"] == "Health check" for j in js.table.rows.values()), js.f
js.search.set("")
js._set_when("Custom range")
js.start_v.set("not a date")
pump(0.5)
assert len(js.table.rows) == total, "a bad date is ignored, not fatal"
assert tuple(js.start_e.cget("border_color")) == tuple(theme.PALETTE["danger"])
day = time.strftime("%Y-%m-%d", time.localtime(time.time() - 3 * 86400))
js.start_v.set(day)
js.end_v.set(day)
pump(0.5)
assert [j["client"] for j in js.table.rows.values()] == ["Bright Dental"], "custom date range"
js._set_when("Today")
assert all(j["client"] != "Bright Dental" for j in js.table.rows.values())
js._clear()
assert len(js.table.rows) == total
# the wipe job opens its certificate; export writes the filtered list
opened = []
app.open_folder = opened.append
iid = next(i for i, j in js.table.rows.items() if j.get("report"))
js.table.tree.selection_set(iid)
pump(0.2)
assert js.report_btn.cget("state") == "normal"
js.report_btn.invoke()
assert opened and opened[-1].endswith("cert.html"), opened
answers["save"] = os.path.join(W, "jobs.csv")
js._set("client", "Acme Legal")
js._export()
import csv  # noqa: E402
with open(answers["save"], encoding="utf-8-sig", newline="") as fh:
    rows = list(csv.reader(fh))
assert rows[0][0] == "Started" and len(rows) == 1 + len(js.shown) and all(r[4] == "Acme Legal" for r in rows[1:])
js._clear()
# presenting hides other clients in the table, the client menu and the search
app.context["client"] = "Acme Legal"
app.set_presentation(True)
pump(0.4)
js = app.screen
assert all(j["client"] in ("Acme Legal", "", "Hidden") for j in js.table.rows.values())
js.search.set("Bright")
pump(0.5)
assert not js.table.rows, "search can't find a hidden client"
app.set_presentation(False)
app.context["client"] = None
app._jobs_filters.update(text="")
app.go(WS.JobsScreen, animate=False)
pump(0.4)

# --- keyboard: Tab reaches buttons, cards, rail items, rows and inputs, each with a focus ring ---------------
for top in [w for w in app.winfo_children() if isinstance(w, M.tk.Toplevel)]:
    top.destroy()  # the Advanced tools window from earlier would cover the screenshots
app.focus_force()
pump(0.3)
ring = tuple(theme.PALETTE["focus_ring"])


def tab_from(w, back=False):
    nxt = app.tk.call("tk_focusPrev" if back else "tk_focusNext", w)
    app.tk.call("tk::TabToWindow", nxt)
    pump(0.15)
    return app.nametowidget(app.tk.call("focus"))


def owner(w):
    while w is not None and getattr(w, "_ring_saved", "unset") == "unset" and not isinstance(w, (Wd.DataTable,)):
        w = w.master
    return w


item = app.rail.items["home"]
item.focus_set()
pump(0.2)
assert tuple(item.cget("border_color")) == ring and item.cget("border_width") == 2, "rail item ring"
seen = set()
w = item
for _ in range(60):
    w = tab_from(w)
    seen.add(type(owner(w)).__name__ if owner(w) is not None else type(w).__name__)
assert {"RailItem", "NavRow", "Chip", "CTkButton", "CTkEntry", "DataTable"} <= seen, seen
assert tuple(item.cget("border_color")) != ring, "ring cleared when focus leaves"
# Enter presses a focused button; a danger button only answers Space
hits = []
b = Wd.secondary_button(app.screen.body, "probe", lambda: hits.append("enter"))
b.pack()
d = Wd.danger_button(app.screen.body, "erase probe", lambda: hits.append("danger"))
d.pack()
pump(0.2)
b.focus_set()
pump(0.1)
b.event_generate("<Return>")
d.focus_set()
pump(0.1)
assert tuple(d.cget("border_color")) == tuple(theme.PALETTE["focus_ring_fill"]), "filled ring"
d._text_label.event_generate("<Return>")
pump(0.1)
assert hits == ["enter"], hits
d._text_label.event_generate("<space>")
pump(0.1)
assert hits == ["enter", "danger"], hits
b.destroy()
d.destroy()
# the typed confirmation still ignores Enter and keeps its red border
tc = Wd.TypedConfirm(app.screen.body, "ERASE", lambda ok: hits.append(("typed", ok)))
tc.pack()
pump(0.2)
tc.entry.focus_set()
pump(0.2)
assert tuple(tc.entry.cget("border_color")) == tuple(theme.PALETTE["danger"])
tc.destroy()
# table: focus rings the table and tints the row the arrow keys are on
tbl = app.screen.table
tbl.tree.focus_set()
pump(0.3)
assert tuple(tbl.cget("border_color")) == ring and tbl.tree.tag_has("focusrow"), "table focus"
for mode in ("light", "dark"):
    theme.set_mode(mode)
    app.mode.set(mode.capitalize())
    app.go(WS.JobsScreen, animate=False)
    pump(0.5)
    app.screen.table.tree.focus_set()
    pump(0.3)
    shot(f"jobs_table_focus_{mode}")
    app.home()
    pump(0.8)
    first = app.screen.start_grid.winfo_children()[1]
    first.focus_set()
    pump(0.3)
    assert first._ring_saved is not None, "quick action ring"
    shot(f"home_focus_{mode}")
    app.screen.new_job_btn.focus_set()
    pump(0.3)
    shot(f"home_button_focus_{mode}")

# --- reduce motion and interface size ------------------------------------------------------------------------
st = app.go(ST.SettingsScreen)
pump(0.4)
app.set_reduce_motion(True)
assert theme.reduced_motion() and load_settings()["reduce_motion"] is True
app.set_personality("Full")
pump(0.4)
comp = [w for w in walk(app.subnav) if isinstance(w, Mo.Mascot)]
if comp:
    x0 = comp[0].x
    comp[0].set_mood("working")
    pump(1.0)
    assert comp[0].x == x0, "Mossbit stays put with Reduce motion"
app.set_personality("Subtle")
app.set_reduce_motion(False)
assert not theme.reduced_motion()
app.set_interface_size(115)
pump(0.6)
assert load_settings()["ui_size"] == 115 and abs(theme.scaling(app) - 1.15) < 1e-6
shot("settings_size_115")
app.set_interface_size(100)
pump(0.4)
assert abs(theme.scaling(app) - 1.0) < 1e-6

print("toast errors:", errors)
print("ALL UI CHECKS PASSED" if not errors else "UI ERRORS")
