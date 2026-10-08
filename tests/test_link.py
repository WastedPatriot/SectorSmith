"""SectorSmith Link end-to-end test: controller in this process, agents in subprocesses over TLS on localhost.

Usage: python tests/test_link.py <workdir-from-build_test_disk.py>
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from sectorsmith.link import migrate, netclone  # noqa: E402
from sectorsmith.link.endpoint import LocalEndpoint  # noqa: E402
from sectorsmith.link.proto import LinkError  # noqa: E402
from sectorsmith.link.server import LinkServer  # noqa: E402
from sectorsmith.util import Progress  # noqa: E402

W = sys.argv[1] if len(sys.argv) > 1 else "/tmp/sstest"
OK = []


def check(c, m):
    print(("PASS " if c else "FAIL ") + m, flush=True)
    OK.append(bool(c))


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def tree(root):
    out = {}
    for d, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(d, f)
            out[os.path.relpath(p, root)] = sha(p)
    return out


# --- fake profiles -------------------------------------------------------------------
old, new = os.path.join(W, "profiles_old"), os.path.join(W, "profiles_new")
shutil.rmtree(old, ignore_errors=True)
shutil.rmtree(new, ignore_errors=True)
A = os.path.join(old, "alice")
files = {
    "Desktop/todo.txt": b"buy milk\n",
    "Desktop/big video.mp4": os.urandom(19 * 1024 * 1024 + 77),
    "Documents/Projects/2026/plan.docx": os.urandom(250_000),
    "Documents/Projects/2026/budget.xlsx": os.urandom(1_500_000),
    "Pictures/Holiday/IMG_0001.jpg": os.urandom(800_000),
    "AppData/Local/Google/Chrome/User Data/Default/Bookmarks": b'{"roots": {}}',
    "AppData/Local/Google/Chrome/User Data/Default/Cache/data_0": os.urandom(5000),     # skipped (cache)
    "AppData/Roaming/Microsoft/Signatures/Alice.htm": b"<p>Alice</p>",
    "NTUSER.DAT": os.urandom(1000),                                                      # skipped (hive)
    "Documents/~$locked.docx": b"owner file",                                            # skipped (temp)
    "OneDrive - Contoso/Shared.docx": b"cloud",
    "Desktop/desktop.ini": b"[.ShellClassInfo]",                                         # skipped (per-PC)
    "AppData/Local/Microsoft/Edge/User Data/Default/Network/Cookies": b"x",              # skipped (PC-bound)
    "AppData/Local/Microsoft/Edge/User Data/Default/LOCK": b"",                          # skipped (lock)
    "AppData/Local/Microsoft/Edge/User Data/Default/Bookmarks": b"{}",
    "Documents/Recipes/Cookies": b"chocolate chip",                                     # kept (not app data)
}
for rel, data in files.items():
    p = os.path.join(A, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(data)
os.makedirs(os.path.join(new, "alice", "Desktop"))
open(os.path.join(new, "alice", "Desktop", "todo.txt"), "wb").write(b"buy milk\n")
os.utime(os.path.join(new, "alice", "Desktop", "todo.txt"),
         (os.path.getmtime(os.path.join(A, "Desktop/todo.txt")),) * 2)
os.environ["SECTORSMITH_PROFILES_ROOT"] = old

target_img = os.path.join(W, "remote_target.img")
with open(target_img, "wb") as f:
    f.truncate(256 * 1024 * 1024)

# --- controller + agent ------------------------------------------------------------------
connected = []
srv = LinkServer(on_connect=connected.append, port=47410)
srv.start()
check(srv.fingerprint and len(srv.token) == 32, "controller listening with fresh cert + token")
cmd = srv.command("127.0.0.1")
print("command:", cmd[:120], "…")


def agent(*extra):
    return subprocess.Popen([sys.executable, "-m", "sectorsmith", "--agent", "--headless", "--no-elevate",
                             "--profiles-root", new, "--image", target_img, *extra], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


# wrong pin must be refused by the agent (MITM protection)
bad = agent("--connect", f"127.0.0.1:{srv.port}", "--token", srv.token, "--pin", "00" * 32)
check(bad.wait(30) == 2 and not connected, "agent refuses a controller with the wrong certificate")
# wrong token must be refused by the controller
bad2 = agent("--connect", f"127.0.0.1:{srv.port}", "--token", "f" * 32, "--pin", srv.fingerprint)
time.sleep(4)
check(not connected, "controller rejects a wrong token")
bad2.kill()

ag = agent("--connect", f"127.0.0.1:{srv.port}", "--token", srv.token, "--pin", srv.fingerprint)
t = time.time()
while not connected and time.time() - t < 30:
    time.sleep(.2)
check(len(connected) == 1, "agent linked")
remote = connected[0]
info = remote.info()
check(info["hostname"] and "version" in info, f"remote info: {info['hostname']} {info['os']}")
profs = remote.list_profiles()
check([p["name"] for p in profs] == ["alice"], "remote profiles listed")
disks = remote.list_disks()
rimg = [d for d in disks if d["path"] == target_img]
check(rimg and rimg[0]["size"] == 256 * 1024 * 1024, "remote disk image listed")

# --- migration local -> remote ---------------------------------------------------------------
local = LocalEndpoint(extra_images=[os.path.join(W, "disk.img")])
src_root = local.list_profiles()[0]["path"]
dst_root = profs[0]["path"]
items = [i for i in migrate.ITEMS if i.default] + migrate.onedrive_items(local, src_root)
check(any(i.group == "OneDrive" and not i.default for i in items), "OneDrive folder detected (off by default)")
sel = [i for i in items if i.default]
sizes = migrate.measure(local, src_root, sel)
check(sizes["desktop"][1] == 2 and sizes["chrome"][1] == 1, f"sizes measured (cache + temp skipped) {sizes['desktop']}")
plan = migrate.Plan(local, src_root, remote, dst_root, sel)
r = migrate.run(plan, Progress(1))
print("migration:", {k: v for k, v in r.items() if k != "failed"}, r["failed"][:3])
want = {k: v for k, v in tree(A).items() if not k.startswith(("OneDrive", "NTUSER")) and "Cache" not in k
        and "~$" not in k and not k.endswith(("desktop.ini", "Network/Cookies", "Default/LOCK"))}
got = tree(os.path.join(new, "alice"))
check(got == want, f"remote profile matches source ({len(got)} files, byte-exact)")
check(r["skipped_unchanged"] == 1 and not r["failed"], "identical existing file skipped, nothing failed")
check(os.path.getmtime(os.path.join(new, "alice", "Documents/Projects/2026/budget.xlsx")) ==
      int(os.path.getmtime(os.path.join(A, "Documents/Projects/2026/budget.xlsx"))), "modified times preserved")
r2 = migrate.run(plan, Progress(1))
check(r2["copied"] == 0 and r2["skipped_unchanged"] == len(want), "second run copies only changes (none)")
open(os.path.join(A, "Desktop", "new.txt"), "w").write("added later")
r3 = migrate.run(plan, Progress(1))
check(r3["copied"] == 1, "delta run copies just the new file")
check(os.path.exists(r3["report"]), "migration report written")
check("Documents/Recipes/Cookies" in got and "Desktop/desktop.ini" not in got,
      "desktop.ini + browser lock/cookie files skipped, user files with the same names kept")
h = migrate.explain_failures(["AppData/Local/Microsoft/Edge/User Data/Default/x: Permission denied"], "OLDPC")
check(h and "Microsoft Edge" in h[0] and "OLDPC" in h[0], "report explains locked files (close Edge on OLDPC)")
nf = os.path.join(new, "bob")
mk = remote.make_folder(path=nf)
check(os.path.isdir(nf) and not mk["existed"] and remote.make_folder(path=nf)["existed"],
      "new destination folder created on the linked PC")
r4 = migrate.run(migrate.Plan(local, src_root, remote, nf, sel), Progress(1))
check(not r4["failed"] and tree(nf) == tree(os.path.join(new, "alice")), "migration into a brand-new folder")

# --- network disk clone, both directions -----------------------------------------------------
src_img = os.path.join(W, "disk.img")
pr = Progress(1)
t0 = time.time()
res = netclone.clone(local, src_img, remote, target_img, pr)
print("clone →", res, f"{256 / (time.time() - t0):.0f} MB/s")
check(sha(target_img) == sha(src_img) and res["verify_mismatches"] == 0, "local → remote clone identical")
check(res["sent_bytes"] < res["bytes"], f"empty space skipped/compressed ({res['sent_bytes'] / res['bytes']:.0%} sent)")
back = os.path.join(W, "local_back.img")
with open(back, "wb") as f:
    f.truncate(256 * 1024 * 1024)
local2 = LocalEndpoint(extra_images=[back])
res = netclone.clone(remote, target_img, local2, back, Progress(1))
check(sha(back) == sha(src_img), "remote → local clone identical")
# one source -> many targets (local + remote), used-space only, onto random junk
sys.path.insert(0, os.path.dirname(__file__))
from fscheck import garbage_file, verify_disk  # noqa: E402
junk_l = os.path.join(W, "many_local.img")
garbage_file(junk_l, 256 * 1024 * 1024)
garbage_file(target_img, 256 * 1024 * 1024)
lmany = LocalEndpoint(extra_images=[src_img, junk_l])
res = netclone.clone_many(lmany, src_img, [(lmany, junk_l), (remote, target_img)], Progress(1), smart=True)
print("clone_many:", {k: v for k, v in res.items() if k not in ("plan",)})
check(all(t["ok"] for t in res["targets"]) and len(res["targets"]) == 2, "one → two clone finished, both verified")
check(not verify_disk(junk_l, json.load(open(os.path.join(W, "meta.json"))), None) and
      not verify_disk(target_img, json.load(open(os.path.join(W, "meta.json"))), None),
      "both clones have clean filesystems with intact files")
check(res["copied_bytes"] < res["bytes"] * .5, f"only used space copied ({res['copied_bytes'] >> 20} MiB)")
big_r = os.path.join(W, "remote_bigger.img")
with open(big_r, "wb") as f:
    f.truncate(400 * 1024 * 1024)
lbig = LocalEndpoint(extra_images=[src_img, big_r])
res = netclone.clone(lbig, src_img, lbig, big_r, Progress(1))
v = subprocess.run(["sgdisk", "-v", big_r], capture_output=True, text=True).stdout
check(res["grown"] and "No problems found" in v, "clone onto a bigger disk fixes the GPT")
try:
    netclone.clone(local, src_img, remote, "/nonexistent", Progress(1))
    check(False, "bad target rejected")
except Exception as e:  # noqa: BLE001
    check(True, f"bad target rejected ({type(e).__name__})")

# --- Cancel over Link ----------------------------------------------------------------------------
import threading  # noqa: E402
from sectorsmith.util import Cancelled, cancel_scope  # noqa: E402


def run_cancel(fn, after=None, delay=None):
    """fn(prog) in a thread like the UI runs it; Cancel once `after` bytes are done or after `delay` seconds.
    Returns (result or exception, seconds from Cancel to the job ending)."""
    pressed = []

    class Prog(Progress):
        def update(self, done):
            super().update(done)
            if after is not None and done >= after:
                press()
    prog = Prog(1)

    def press():
        if not pressed:
            pressed.append(time.time())
            prog.cancel()
    out = {}

    def work():
        try:
            with cancel_scope(prog.check):
                out["r"] = fn(prog)
        except Exception as e:  # noqa: BLE001
            out["r"] = e
        out["t"] = time.time()
    th = threading.Thread(target=work, daemon=True)
    th.start()
    if delay is not None:
        time.sleep(delay)
        press()
    th.join(90)
    return (out.get("r", "still running"), (out["t"] - pressed[0]) if pressed and "t" in out else 999)


marker = os.path.join(W, "remote_cancel_marker")
if os.path.exists(marker):
    os.remove(marker)
r, dt = run_cancel(lambda prog: remote.run_command(cmd=f"(sleep 2; touch '{marker}') & sleep 60"), delay=0.6)
time.sleep(2.5)
check(isinstance(r, Cancelled) and dt < 3 and not os.path.exists(marker),
      f"Cancel reaches a command running on the linked PC: stopped in {dt:.2f}s, and what it started")
check(remote.ping() == "pong" and remote.alive, "link still up after a remote Cancel")
r = remote.run_command(cmd="echo hi")
check(r["code"] == 0 and "hi" in r["out"], "remote commands still work after Cancel")

# migration local -> remote, cancelled part-way, then finished by Run again
cf = os.path.join(new, "cancelled_move")
plan_c = migrate.Plan(local, src_root, remote, cf, sel)
r, dt = run_cancel(lambda prog: migrate.run(plan_c, prog), after=1)
rep_c = ""
if isinstance(r, Cancelled) and os.path.exists(r.info.get("report", "")):
    with open(r.info["report"]) as fh:
        rep_c = fh.read()
check(isinstance(r, Cancelled) and dt < 3 and r.info.get("cancelled") and "STOPPED" in rep_c,
      f"migration: Cancel stops it in {dt:.2f}s and writes a report ({getattr(r, 'info', {}).get('copied')} "
      "files copied before)")
r2 = migrate.run(plan_c, Progress(1))
check(not r2["failed"] and tree(cf) == tree(os.path.join(new, "alice")), "Run again after Cancel finishes the move")

# disk clone local -> remote, cancelled part-way
r, dt = run_cancel(lambda prog: netclone.clone_many(local, src_img, [(remote, target_img)], prog, smart=False),
                   after=16 << 20)
st = getattr(r, "info", {}).get("targets") or [{}]
check(isinstance(r, Cancelled) and dt < 5 and st[0].get("state") == "part-written",
      f"network clone: Cancel stops it in {dt:.2f}s and says the target is part-written")
res = netclone.clone(local, src_img, remote, target_img, Progress(1))
check(sha(target_img) == sha(src_img), "the cancelled target can be cloned again (disks were closed cleanly)")

# --- old disk attached by USB: user folders on other drives ---------------------------------------
vol = os.path.join(W, "usb_old_disk")
shutil.rmtree(vol, ignore_errors=True)
bob = os.path.join(vol, "Users", "bob")
for rel, data in {"Desktop/note.txt": b"from the old disk", "Documents/cv.docx": os.urandom(70_000),
                  "AppData/Roaming/Mozilla/Firefox/profiles.ini": b"[General]", "NTUSER.DAT": b"hive"}.items():
    os.makedirs(os.path.dirname(os.path.join(bob, rel)), exist_ok=True)
    with open(os.path.join(bob, rel), "wb") as fh:
        fh.write(data)
os.makedirs(os.path.join(vol, "Users", "Public", "Documents"))
for d in ("Program Files/Mozilla Firefox", "Program Files/7-Zip", "Program Files (x86)/Acme Payroll",
          "Program Files/Common Files", "Users/bob/AppData/Local/Programs/Microsoft VS Code"):
    os.makedirs(os.path.join(vol, d), exist_ok=True)
os.environ["SECTORSMITH_VOLUMES"] = vol
usb = LocalEndpoint()
offp = usb.offline_profiles()
check([p["name"] for p in offp] == ["bob"] and offp[0]["path"] == bob, f"user on an attached old disk found {offp}")
vols = usb.list_volumes()
check(vols and vols[0]["path"] == vol and vols[0]["free"] > 0, "attached drives listed with free space")
from sectorsmith.link import apps  # noqa: E402
found = usb.apps_on_disk(profile=bob)
names = [a["name"] for a in found]
check({"Mozilla Firefox", "7-Zip", "Microsoft Visual Studio Code", "Acme Payroll"} <= set(names)
      and "Common Files" not in names, f"apps on the old disk found by their folders {names}")
rows = apps.match(found)
by = {r_["name"]: r_ for r_ in rows}
check(by["Mozilla Firefox"]["winget_id"] == "Mozilla.Firefox" and by["Acme Payroll"]["source"] == "manual",
      "old-disk apps matched to winget, unknown ones listed to install by hand")
drive_dst = os.path.join(W, "usb_backup_drive", "SectorSmith", "bob")
shutil.rmtree(os.path.dirname(drive_dst), ignore_errors=True)
mk = usb.make_folder(path=drive_dst)
r = migrate.run(migrate.Plan(usb, bob, remote, os.path.join(new, "bob_from_usb"), sel, apps=rows,
                             install_apps=False), Progress(1))
with open(os.path.join(new, "bob_from_usb", "Desktop", "note.txt"), "rb") as fh:
    note = fh.read()
check(not r["failed"] and note == b"from the old disk", "old disk -> linked PC migration")
r = migrate.run(migrate.Plan(usb, bob, usb, drive_dst, sel, apps=rows, install_apps=False), Progress(1))
check(not r["failed"] and os.path.exists(os.path.join(drive_dst, "Documents", "cv.docx"))
      and os.path.exists(os.path.join(drive_dst, "SectorSmith - apps on the old PC.txt")),
      "old disk -> folder on another drive, with the app list saved next to the files")
os.environ.pop("SECTORSMITH_VOLUMES")

# --- NTFS partition without a drive letter, read raw -------------------------------------------------
raw_disk = os.path.join(W, "raw_old.img")
part = os.path.join(W, "raw_part.img")
mnt = os.path.join(W, "raw_mnt")
raw_files = {"Desktop/todo.txt": b"raw read", "Documents/Big/report.bin": os.urandom(3 * 1024 * 1024 + 4321),
             "Documents/empty.txt": b"", "Pictures/p.jpg": os.urandom(150_000),
             "AppData/Local/Google/Chrome/User Data/Default/Bookmarks": b'{"roots":{}}',
             "AppData/Local/Google/Chrome/User Data/Default/Cache/x": b"skip me"}
ok_raw = False
try:
    with open(part, "wb") as f:
        f.truncate(48 * 1024 * 1024)
    subprocess.run(["mkntfs", "-F", "-Q", "-q", "-s", "512", "-p", "2048", part], check=True,
                   capture_output=True)
    os.makedirs(mnt, exist_ok=True)
    subprocess.run(["ntfs-3g", part, mnt], check=True, capture_output=True, timeout=30)
    try:
        for rel, data in raw_files.items():
            pth = os.path.join(mnt, "Users", "dave", rel)
            os.makedirs(os.path.dirname(pth), exist_ok=True)
            with open(pth, "wb") as fh:
                fh.write(data)
        os.makedirs(os.path.join(mnt, "Users", "Default", "Desktop"))
        os.makedirs(os.path.join(mnt, "Program Files", "Notepad++"))
    finally:
        subprocess.run(["umount", mnt], capture_output=True, timeout=30)
    with open(raw_disk, "wb") as f:
        f.truncate(64 * 1024 * 1024)
    subprocess.run(["sgdisk", "-Z", raw_disk], capture_output=True)
    subprocess.run(["sgdisk", "-n1:2048:+48M", "-t1:0700", raw_disk], check=True, capture_output=True)
    with open(part, "rb") as a_, open(raw_disk, "r+b") as b_:
        b_.seek(2048 * 512)
        b_.write(a_.read())
    ok_raw = True
except (OSError, subprocess.SubprocessError) as e:
    print("SKIP raw NTFS checks (needs mkntfs + ntfs-3g/FUSE):", e)
if ok_raw:
    rawep = LocalEndpoint(extra_images=[raw_disk])
    rp = rawep.offline_profiles(raw=True)
    check([p["name"] for p in rp] == ["dave"] and rp[0]["path"].startswith("ntfs:"),
          f"user found in an NTFS partition without a drive letter {[p['name'] for p in rp]}")
    rroot = rp[0]["path"]
    check(set(rawep.list_dir(path=rroot)) >= {"Desktop", "Documents", "AppData"}, "raw NTFS folders listed")
    raw_dst = os.path.join(new, "dave")
    shutil.rmtree(raw_dst, ignore_errors=True)
    r = migrate.run(migrate.Plan(rawep, rroot, remote, raw_dst, sel), Progress(1))
    want_raw = {k.replace("/", os.sep): hashlib.sha256(v).hexdigest() for k, v in raw_files.items()
                if "Cache" not in k}
    check(not r["failed"] and tree(raw_dst) == want_raw,
          f"raw NTFS -> linked PC: every file byte-exact {r['failed'][:2]}")
    a_ = rawep.apps_on_disk(profile=rroot)
    check([x["name"] for x in a_] == ["Notepad++"], "apps on a raw NTFS disk found by folder")
    try:
        rawep.write_files(root=rroot, items=[], blob=b"")
        check(False, "raw NTFS is read-only")
    except LinkError:
        check(True, "raw NTFS is read-only")
    rawep.close()

remote.close()
ag.wait(30)
check(ag.returncode == 0, "agent exits cleanly when the controller disconnects")

# --- listen mode (USB-booted machine) -----------------------------------------------------------
lag = agent("--listen", "--port", "47420", "--code", "ABCD-EFGH")
time.sleep(3)
try:
    srv.connect_to_waiting("127.0.0.1", "WRONG-CODE", port=47420)
    check(False, "wrong code rejected")
except LinkError:
    check(True, "wrong pairing code rejected")
ep = srv.connect_to_waiting("127.0.0.1", "abcd-efgh", port=47420)
check(ep.list_disks() and len(ep.check) == 6, f"listen-mode machine linked (check code {ep.check})")
ep.close()
lag.kill()
srv.stop()
print(f"\n{sum(OK)}/{len(OK)} link checks passed")
sys.exit(0 if all(OK) else 1)
