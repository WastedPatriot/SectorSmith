"""Headless run-through of "Clone this PC to a new drive" with the fake system layer, in light and dark, with a
screenshot of every step: checks ticking off, a refused target, the plan, the typed confirmation, the phases while
copying, each verdict, a cancel, and resuming an interrupted clone.

Run: xvfb-run -a -s "-screen 0 1400x900x24" python tests/ui_sysclone_smoke.py <workdir> <shots_dir>
"""
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
W, SHOTS = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
shutil.rmtree(W, ignore_errors=True)
os.makedirs(W)
os.makedirs(SHOTS, exist_ok=True)
os.environ["LOCALAPPDATA"] = os.path.join(W, "appdata")

from tkinter import messagebox  # noqa: E402

import sysclone_fake as F  # noqa: E402
from sectorsmith import sysclone  # noqa: E402
from sectorsmith.device import open_image  # noqa: E402
from sectorsmith.ui import main as M, theme  # noqa: E402
from sectorsmith.ui.guide import save_settings  # noqa: E402
from sectorsmith.util import Progress  # noqa: E402

M.list_disks = lambda: []          # only the image files below, never this machine's disks
messagebox.askyesno = lambda *a, **k: True
save_settings(welcomed=True)

SRC = os.path.join(W, "system.img")
F.build_system_disk(SRC)
BIG = F.blank(os.path.join(W, "new_ssd.img"), 400)
TINY = F.blank(os.path.join(W, "tiny_usb.img"), 140)
layer = F.FakeLayer(W, system_path=SRC)
sysclone.set_layer(layer)
sysclone.MIN_FREE = 8 * F.MiB      # sized for test images
sysclone.SHADOW_MIN = 0
_pf = sysclone.preflight
sysclone.preflight = lambda *a, **k: _pf(*a, **dict(k, min_free=8 * F.MiB, shadow_min=0))

app = M.MainWindow()
app.geometry("1360x860+0+0")
errors = []
orig_toast = app.toast
app.toast = lambda text, tone="success": (errors.append(text) if tone == "danger" else None, orig_toast(text, tone))
for p in (SRC, BIG, TINY):
    app.add_image(p)
n = [0]


def pump(sec=0.4):
    end = time.time() + sec
    while time.time() < end:
        app.update()
        time.sleep(0.015)


def wait_for(cond, timeout=60, what=""):
    t = time.time()
    while time.time() - t < timeout:
        pump(0.1)
        if cond():
            return
    raise AssertionError(f"timed out waiting for {what}")


def shot(name):
    pump(0.5)
    n[0] += 1
    subprocess.run(["import", "-window", "root", os.path.join(SHOTS, f"{n[0]:02d}_{name}.png")], check=False)


def press(text):
    for w in app.screen.footer.winfo_children():
        if getattr(w, "cget", None) and w.cget("text") == text:
            w.invoke()
            pump(0.3)
            return
    raise AssertionError(f"button {text!r} not in {[w.cget('text') for w in app.screen.footer.winfo_children()]}")


def pick(path):
    for card, _b, t in app.screen.picker.rows:
        if os.path.abspath(t["dev"].path) == os.path.abspath(path):
            card.click()
            pump(0.2)
            return
    raise AssertionError(f"{path} not offered as a target")


def type_confirm(phrase):
    def walk(w):
        yield w
        for c in w.winfo_children():
            yield from walk(c)
    for w in walk(app.screen.body):
        if w.__class__.__name__ == "CTkEntry":
            w.insert(0, phrase)
    pump(0.2)


def ready():
    return str(app.screen.next_btn.cget("state")) == "normal"


def open_wizard():
    app.open_target("drives", "sysclone")
    pump(0.8)
    assert type(app.screen).__name__ == "SysCloneWizard", type(app.screen)


def to_confirm(path):
    pick(path)
    wait_for(ready, 30, "the checks")
    press("Next")
    pump(0.5)
    press("Next")
    phrase = sysclone_phrase()
    type_confirm(phrase)


def sysclone_phrase():
    from sectorsmith.ui.sysclone_screens import confirm_phrase
    return confirm_phrase(app.screen.dst)


def finish(timeout=120):
    wait_for(lambda: app.job is None, timeout, "the clone")
    pump(0.8)
    return app.screen.res


def verdict_text():
    def walk(w):
        yield w
        for c in w.winfo_children():
            yield from walk(c)
    from sectorsmith.ui.sysclone_screens import VerdictBadge
    badge = next(w for w in walk(app.screen.body) if isinstance(w, VerdictBadge))
    return [c.cget("text") for c in badge.winfo_children() if c.__class__.__name__ == "CTkLabel"]


for mode in ("light", "dark"):
    theme.set_mode(mode)
    app.mode.set(mode.capitalize())
    layer.__init__(W, system_path=SRC)
    # 1. pick: the checks tick off one by one --------------------------------------------------------------------
    open_wizard()
    shot(f"{mode}_pick")
    rows = app.screen.picker.rows
    assert not any(os.path.abspath(t["dev"].path) == SRC for _c, _b, t in rows), "the system drive is not a target"
    pick(BIG)
    pump(0.35)
    shot(f"{mode}_checks_running")
    wait_for(ready, 30, "the checks")
    shot(f"{mode}_checks_ready")
    assert app.screen.pf and not app.screen.pf.blocked
    # a target that's too small is refused with the reason and nothing else happens
    pick(TINY)
    wait_for(lambda: app.screen.pf is not None and app.screen.pf.blocked, 30, "the refusal")
    pump(1.5)
    shot(f"{mode}_checks_blocked")
    assert "sort out" in app.screen.summary_lbl.cget("text") and not ready()
    # 2. plan and 3. confirm ---------------------------------------------------------------------------------------
    pick(BIG)
    wait_for(ready, 30, "the checks")
    press("Next")
    shot(f"{mode}_plan")
    press("Next")
    shot(f"{mode}_confirm")
    type_confirm(sysclone_phrase())
    shot(f"{mode}_confirm_typed")
    # 4. clone: phases, then the verdict ---------------------------------------------------------------------------
    layer.delay = 0.02
    press("Clone this PC")
    wait_for(lambda: app.job is not None and app.job.label.startswith("Copying") and app.job.snapshot()["pct"] > 15,
             60, "the copy")
    shot(f"{mode}_copying")
    wait_for(lambda: app.job is None or app.job.label.startswith(sysclone.PHASE_LABELS["boot"]), 120, "the fix-up")
    if app.job is not None:
        shot(f"{mode}_bootfix")
    res = finish()
    layer.delay = 0
    shot(f"{mode}_verified")
    assert res["verify"]["status"] == "passed", res["verify"]
    assert verdict_text()[0] == "Verified", verdict_text()
    assert app.jobs[-1]["result"] == "Done" and app.jobs[-1]["report"] == res["report"], app.jobs[-1]
    # passed with problems: unreadable sectors on this PC's drive
    vol = F.NTFSVolume(open_image(SRC), 116736 * 512)
    vol.scan(Progress(1))
    lba = next(e.runs[0][0] * 8 + 116736 for e in vol.entries.values() if e.name == "holiday.jpg")
    layer.__init__(W, system_path=SRC, bad_lbas={lba + 5, lba + 6})
    open_wizard()
    to_confirm(BIG)
    press("Clone this PC")
    res = finish()
    shot(f"{mode}_problems")
    assert res["verify"]["status"] == "problems" and verdict_text()[0] == "Passed with problems"
    # failed: the boot configuration still points at the old drive
    layer.__init__(W, system_path=SRC, bcd_wrong=True)
    open_wizard()
    to_confirm(BIG)
    press("Clone this PC")
    res = finish()
    shot(f"{mode}_failed")
    assert res["verify"]["status"] == "failed" and verdict_text()[0] == "Failed"
    assert sysclone.is_marked_unusable(open_image(BIG))
    # cancel part-way
    layer.__init__(W, system_path=SRC, delay=0.03)
    open_wizard()
    to_confirm(BIG)
    press("Clone this PC")
    wait_for(lambda: app.job is not None and app.job.label.startswith("Copying") and app.job.snapshot()["pct"] > 10,
             60, "the copy")
    app.cancel_job()
    pump(0.3)
    shot(f"{mode}_cancelling")
    wait_for(lambda: app.job is None, 60, "the cancel")
    pump(0.8)
    shot(f"{mode}_cancelled")
    assert sysclone.is_marked_unusable(open_image(BIG)) and not F.FakeLayer.store
    assert app.screen.title_lbl.cget("text") == "Stopped"
    # an interrupted clone (power cut) is offered for resume and finishes
    layer.__init__(W, system_path=SRC)

    class Crash(BaseException):
        pass

    class CrashAt(Progress):
        def update(self, done):
            super().update(done)
            if self.label.startswith("Copying") and done > 50 * F.MiB:
                raise Crash()
    s, d = open_image(SRC), open_image(BIG)
    pf = sysclone.preflight(s, d, layer)
    try:
        sysclone.run_clone(s, d, pf.plan, layer, CrashAt(1), journal_every=2)
    except Crash:
        pass
    for j in sysclone.Journal.all():   # pretend it has been quiet for a while
        j.data["heartbeat"] = time.time() - 600
        j.save()
    open_wizard()
    shot(f"{mode}_resume_offer")

    def resume_btn():
        def walk(w):
            yield w
            for c in w.winfo_children():
                yield from walk(c)
        return next(w for w in walk(app.screen.body) if getattr(w, "cget", None) and
                    w.__class__.__name__ == "CTkButton" and w.cget("text") == "Resume")
    resume_btn().invoke()
    res = finish()
    shot(f"{mode}_resumed")
    assert res["verify"]["status"] == "passed" and any("Resumed" in x for x in res["notes"]), res["notes"]
    assert not sysclone.interrupted(idle=0)

# Home and Drives offer it too
app.home()
pump(0.8)
shot("dark_home")
app.open_target("drives", "all")
pump(1.0)
shot("dark_drives")
theme.set_mode("light")
app.mode.set("Light")
pump(0.5)
shot("light_drives")
assert not errors, errors
print(f"UI sysclone smoke OK, {n[0]} screenshots in {SHOTS}")
app.destroy()
shutil.rmtree(W, ignore_errors=True)
