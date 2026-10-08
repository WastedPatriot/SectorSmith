"""SectorSmith Link end-to-end test: controller in this process, agents in subprocesses over TLS on localhost.

Usage: python tests/test_link.py <workdir-from-build_test_disk.py>
"""
import hashlib
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
        and "~$" not in k}
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
try:
    netclone.clone(local, src_img, remote, "/nonexistent", Progress(1))
    check(False, "bad target rejected")
except Exception as e:  # noqa: BLE001
    check(True, f"bad target rejected ({type(e).__name__})")

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
