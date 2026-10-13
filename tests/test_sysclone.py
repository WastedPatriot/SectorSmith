"""Clone this PC to a new drive: engine tests on disk images with a fake system layer (Linux, CI engine job).

Usage: python tests/test_sysclone.py <workdir>
Needs sgdisk, sfdisk, mkfs.vfat, fsck.vfat, mtools, mkntfs, ntfs-3g (root or sudo -n), ntfsfix, ntfscat, ntfsresize.
"""
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

W = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "/tmp/sssysclone")
shutil.rmtree(W, ignore_errors=True)
os.makedirs(W)
os.environ["LOCALAPPDATA"] = os.path.join(W, "appdata")

import sysclone_fake as F  # noqa: E402
from sectorsmith import partitions, sysclone  # noqa: E402
from sectorsmith.device import open_image  # noqa: E402
from sectorsmith.util import Cancelled, Progress, app_dir  # noqa: E402

MiB = F.MiB
OK = []
SMALL = dict(min_free=8 * MiB, shadow_min=0)   # thresholds sized for test images


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    OK.append(bool(cond))


def case(name):
    d = os.path.join(W, name)
    os.makedirs(d, exist_ok=True)
    return d


def source(work, **kw):
    p = os.path.join(work, "src.img")
    F.build_system_disk(p, **kw)
    return p


def clone(src_path, dst_path, layer, ss=512, prog=None, take_offline=False, expect_block=False, **kw):
    s, d = open_image(src_path, ss), open_image(dst_path, ss)
    pf = sysclone.preflight(s, d, layer, take_offline=take_offline, **SMALL)
    if pf.blocked:
        if not expect_block:
            print("   blocked:", [(c.title, c.detail) for c in pf.blocking()])
        return pf, None
    res = sysclone.run_clone(s, d, pf.plan, layer, prog or Progress(1), report_dir=os.path.dirname(dst_path), **kw)
    return pf, res


def fs_problems(img, plan, ss=512, files=None):
    """fsck every file system on the target and read the Windows files back with ntfs-3g's ntfscat."""
    probs = []
    work = os.path.dirname(img)
    for p in plan.parts:
        if p.action == "zero":
            continue
        part = F.extract(img, p.start, p.sectors, ss, os.path.join(work, f"_chk{p.src_slot}.img"))
        if p.fs == "FAT32":
            r = subprocess.run(["fsck.vfat", "-n", part], capture_output=True, text=True)
            if r.returncode != 0:
                probs.append(f"fsck.vfat {p.role}: {r.stdout[-300:]}")
        elif p.fs == "NTFS":
            r = subprocess.run(["ntfsfix", "-n", part], capture_output=True, text=True)
            if r.returncode != 0:
                probs.append(f"ntfsfix {p.role}: {(r.stdout + r.stderr)[-300:]}")
            if p.role == "windows":
                for rel in (files or F.WIN_FILES):
                    got = subprocess.run(["ntfscat", "-f", part, rel], capture_output=True).stdout
                    if hashlib.sha256(got).digest() != hashlib.sha256(F._data(rel, F.WIN_FILES[rel])).digest():
                        probs.append(f"{rel} differs on the target")
                r = subprocess.run(["ntfsresize", "-f", "-n", "-i", part], capture_output=True, text=True)
                if r.returncode != 0 or "error" in r.stdout.lower():
                    probs.append(f"ntfsresize consistency {p.role}: {(r.stdout + r.stderr)[-400:]}")
        os.remove(part)
    return probs


def sgdisk_ok(img):
    out = subprocess.run(["sgdisk", "-v", img], capture_output=True, text=True).stdout
    return "No problems found" in out, out[-300:]


def status(res):
    return (res or {}).get("verify", {}).get("status")


def failed_check(res):
    return next((c["name"] for c in (res or {}).get("checks", []) if c["status"] == "failed"), None)


# 1. GPT/UEFI onto a bigger drive (grow) ------------------------------------------------------------------------
w = case("grow")
src = source(w)
ok, out = sgdisk_ok(src)
check(ok, "source image: valid GPT (sgdisk -v)")
dst = F.blank(os.path.join(w, "dst.img"), 400, b"old data on the target ")
L = F.FakeLayer(w)
pf, res = clone(src, dst, L)
check(pf.plan is not None and pf.plan.part("windows").resize == "grow", "preflight passes, plan grows Windows")
check(status(res) == "passed", f"clone to a bigger drive: verdict {status(res)} {failed_check(res)}")
ok, out = sgdisk_ok(dst)
check(ok, f"target GPT valid (sgdisk -v) {'' if ok else out}")
probs = fs_problems(dst, pf.plan)
check(not probs, f"target file systems check clean, Windows files intact {probs[:3]}")
spt, dpt = partitions.read_partition_table(open_image(src)), partitions.read_partition_table(open_image(dst))
check([p.type_id for p in spt.partitions] == [p.type_id for p in dpt.partitions], "same partition types and order")
check(spt.disk_guid != dpt.disk_guid, "new disk GUID")
raw = open(dst, "rb").read(34 * 512)
uniques = {str(uuid.UUID(bytes_le=raw[1024 + i * 128 + 16:1024 + i * 128 + 32])) for i in range(4)}
check(not uniques & {p.src_unique for p in pf.plan.parts}, "new partition GUIDs")
check(struct.unpack_from("<Q", raw, 1024 + 3 * 128 + 48)[0] == 0x8000000000000001, "Recovery attributes kept")
check(all(p.start % 2048 == 0 for p in pf.plan.parts), "every partition starts on a 1 MiB boundary")
rec = pf.plan.part("recovery")
bs = open_image(dst).read(rec.start * 512, 512)
check(rec.start != rec.src_start and struct.unpack_from("<I", bs, 0x1C)[0] == rec.start,
      "moved Recovery partition: NTFS boot sector records its new start")
msr = pf.plan.part("msr")
check(open_image(dst).read(msr.start * 512, msr.sectors * 512) == bytes(msr.sectors * 512), "MSR rebuilt as zeros")
check(("extend",) in L.calls and ("registry", 4, False) in L.calls and L.calls[-1][0] == "unmount",
      "boot fix-up: registry mapped, file system extended, temporary letters removed")
check(L.awake == [True, False], "sleep blocked for the clone and released after")
check(not F.FakeLayer.store and L.deleted, "snapshot set released")
check(not os.listdir(app_dir() / "sysclone"), "journal removed after a finished clone")
html = open(res["report"], encoding="utf-8").read()
check("Verdict: Verified" in html and "Layout before" in html and "Layout after" in html and "—" not in html
      and all(c["name"] in html for c in res["checks"]), "HTML report with verdict, layouts and every check")
check(set(res["verify"]) == {"status", "summary", "details"} and res["verify"]["details"], "shared verify shape")
check(not sysclone.is_marked_unusable(open_image(dst)), "finished clone has no 'unfinished' marker")
F.cleanup(w)

# 2. equal size target ------------------------------------------------------------------------------------------
w = case("equal")
src = source(w, disk_mib=298)   # partitions fill the disk, like a real Windows install
dst = F.blank(os.path.join(w, "dst.img"), 298)
pf, res = clone(src, dst, F.FakeLayer(w))
check(pf.plan and all(not p.resize for p in pf.plan.parts) and status(res) == "passed",
      f"clone to an equal drive: no resize, verdict {status(res)}")
check([(p.start, p.sectors) for p in pf.plan.parts] == [(p.src_start, p.src_sectors) for p in pf.plan.parts],
      "equal drive keeps every partition where it was")
check(not fs_problems(dst, pf.plan), "equal clone checks clean")
F.cleanup(w)

# 3. smaller target: Windows shrinks ------------------------------------------------------------------------------
w = case("shrink")
spread = source(w)
dst = F.blank(os.path.join(w, "dst.img"), 260, b"\xa5")
pf, res = clone(spread, dst, F.FakeLayer(w), expect_block=True)
sp = next(c for c in pf.checks if c.key == "space")
check(res is None and sp.status == "block" and "Shrink C:" in sp.fix,
      f"files spread to the end of C: refused with shrink advice: {sp.detail}")
F.cleanup(spread)
src = os.path.join(w, "src.img")
F.build_system_disk(src, packed_mib=90)
L = F.FakeLayer(w)
pf, res = clone(src, dst, L)
win = pf.plan.part("windows") if pf.plan else None
check(win is not None and win.resize == "shrink" and win.sectors < win.src_sectors,
      f"plan shrinks Windows to fit ({(win.sectors * 512) >> 20 if win else '-'} MiB)")
check(status(res) == "passed", f"clone to a smaller drive: verdict {status(res)} {failed_check(res)}")
probs = fs_problems(dst, pf.plan)
check(not probs, f"shrunk NTFS checks clean (ntfsfix, ntfsresize) and files intact {probs[:2]}")
d = open_image(dst)
bs = d.read(win.start * 512, 512)
total = struct.unpack_from("<Q", bs, 40)[0]
check(total == win.sectors - 1 and d.read((win.start + total) * 512, 512) == bs,
      "shrunk volume: size field and backup boot sector at the new end")
check(("extend",) not in L.calls, "no extend after a shrink")
ok, _o = sgdisk_ok(dst)
check(ok, "shrunk target GPT valid")
F.cleanup(dst)

# too small: refused before anything is written, with the reason
dst = F.blank(os.path.join(w, "tiny.img"), 140, b"\x5a")
before = hashlib.sha256(open(dst, "rb").read()).hexdigest()
pf, res = clone(src, dst, F.FakeLayer(w), expect_block=True)
sp = next(c for c in pf.checks if c.key == "space")
check(res is None and sp.status == "block" and "too small" in sp.detail and sp.fix,
      f"too-small target refused: {sp.detail} / {sp.fix}")
check(hashlib.sha256(open(dst, "rb").read()).hexdigest() == before, "refused target untouched")
lay = sysclone.read_layout(open_image(src), F.FakeLayer(w).volumes(open_image(src)))
# used space only known after the snapshot (BitLocker style): the engine re-checks before writing
plan, probs = sysclone.make_plan(lay, 140 * MiB // 512, 512, None, 8 * MiB)
s, d = open_image(src), open_image(dst)
L = F.FakeLayer(w)
try:
    sysclone.run_clone(s, d, plan, L, Progress(1), report_dir=w)
    check(False, "engine re-check refuses a shrink that no longer fits")
except sysclone.SysCloneError as e:
    check("Nothing was written" in e.fix and hashlib.sha256(open(dst, "rb").read()).hexdigest() == before
          and not F.FakeLayer.store, f"after the snapshot, a shrink that doesn't fit stops before writing: {e.reason}")
F.cleanup(w)

# 4. MBR/BIOS ---------------------------------------------------------------------------------------------------------
w = case("mbr")
src = source(w, scheme="MBR")
dst = F.blank(os.path.join(w, "dst.img"), 400)
L = F.FakeLayer(w)
pf, res = clone(src, dst, L)
check(status(res) == "passed", f"MBR/BIOS clone: verdict {status(res)} {failed_check(res)}")
probs = fs_problems(dst, pf.plan)
check(not probs, f"MBR target file systems clean {probs[:2]}")
sm, dm = open(src, "rb").read(512), open(dst, "rb").read(512)
check(sm[:440] == dm[:440] and sm[440:444] != dm[440:444] and dm[440:444] != bytes(4),
      "boot code copied, new disk signature")
dpt = partitions.read_partition_table(open_image(dst))
check([p.bootable for p in dpt.partitions] == [True, False, False] and
      [p.type_id for p in dpt.partitions] == ["0x07", "0x07", "0x27"], "active System Reserved, types kept")
sf = subprocess.run(["sfdisk", "-d", dst], capture_output=True, text=True)
check(sf.returncode == 0 and sf.stdout.count("type=") == 3, "sfdisk reads the MBR")
check(("bcdboot", "BIOS") in L.calls, "bcdboot /f BIOS for an MBR disk")
reps = sysclone.registry_replacements(pf.plan)
check(len(reps) == 3 and all(len(a) == 12 and len(b) == 12 for a, b in reps), "MountedDevices ids: signature+offset")
F.cleanup(w)

# 5. 4Kn ---------------------------------------------------------------------------------------------------------------
w = case("4kn")
src = source(w, ss=4096)
dst = F.blank(os.path.join(w, "dst.img"), 400)
pf, res = clone(src, dst, F.FakeLayer(w), ss=4096)
check(status(res) == "passed", f"4Kn to 4Kn clone: verdict {status(res)} {failed_check(res)}")
check(not fs_problems(dst, pf.plan, ss=4096), "4Kn target file systems clean")
pt = partitions.read_partition_table(open_image(dst, 4096))
check(pt.scheme == "GPT" and not pt.notes and pt.partitions[0].start_lba == 256, "4Kn GPT valid, 1 MiB = LBA 256")
F.cleanup(dst)
src512 = os.path.join(w, "src512.img")
F.build_system_disk(src512)
dst = F.blank(os.path.join(w, "dst4k.img"), 400)
pf, res = clone(src512, dst, F.FakeLayer(w), ss=512, expect_block=True)
sec = next(c for c in sysclone.preflight(open_image(src512), open_image(dst, 4096), F.FakeLayer(w), **SMALL).checks
           if c.key == "sectors")
check(sec.status == "block" and "4096" in sec.detail and "512" in sec.fix, f"512 to 4Kn refused: {sec.detail}")
F.cleanup(w)

# 6. interrupt and resume -----------------------------------------------------------------------------------------------
w = case("resume")
src = source(w)
dst = F.blank(os.path.join(w, "dst.img"), 400)


class Crash(BaseException):
    """Stands in for a power cut: nothing gets to clean up."""


class CrashAt(Progress):
    def __init__(self, at):
        super().__init__(1)
        self.at = at

    def update(self, done):
        super().update(done)
        if self.label.startswith(sysclone.PHASE_LABELS["copy"]) and done >= self.at:
            raise Crash()


s, d = open_image(src), open_image(dst)
L = F.FakeLayer(w)
pf = sysclone.preflight(s, d, L, **SMALL)
try:
    sysclone.run_clone(s, d, pf.plan, L, CrashAt(60 * MiB), report_dir=w, journal_every=4)
    check(False, "crash simulated")
except Crash:
    pass
js = sysclone.interrupted(idle=0)
check(len(js) == 1 and js[0].done() and F.FakeLayer.store, "after a crash: journal with verified chunks, snapshot kept")
check(sysclone.is_marked_unusable(open_image(dst)), "interrupted target is marked unusable meanwhile")
j = js[0]
d2 = open_image(dst)
check(sysclone.resume_target_ok(j, d2), "resume recognises its target")
L2 = F.FakeLayer(w)
res = sysclone.run_clone(open_image(src), d2, sysclone.Plan.from_dict(j.data["plan"]), L2, Progress(1), journal=j,
                         report_dir=w)
check(status(res) == "passed" and any("Resumed" in n and "already copied" in n for n in res["notes"]),
      f"resumed clone finishes and verifies: {status(res)} {res['notes']}")
check(not fs_problems(dst, sysclone.Plan.from_dict(j.data["plan"])), "resumed target checks clean")
check(not sysclone.interrupted(idle=0) and not F.FakeLayer.store, "resume cleaned up journal and snapshot")
F.cleanup(w)

# 7. read errors on the source ----------------------------------------------------------------------------------------
w = case("badsectors")
src = source(w)
lay = sysclone.read_layout(open_image(src), F.FakeLayer(w).volumes(open_image(src)))
winp = next(p for p in lay.parts if p.role == "windows")
vol = F.NTFSVolume(open_image(src), winp.start_lba * 512)
vol.scan(Progress(1))
runs = {e.name: e.runs[0][0] * 8 + winp.start_lba for e in vol.entries.values() if e.name in ("holiday.jpg",
                                                                                              "cache.bin")}
bad = {runs["holiday.jpg"] + 100, runs["holiday.jpg"] + 101, runs["cache.bin"] + 4000}
dst = F.blank(os.path.join(w, "dst.img"), 320)
L = F.FakeLayer(w, bad_lbas=bad)
pf, res = clone(src, dst, L)
check(status(res) == "problems" and res["bad_sectors"] == 3 and
      sorted(b["source"] for b in res["bad_lbas"]) == sorted(bad),
      f"unreadable sectors: verdict {status(res)}, mapped {res and res['bad_lbas']}")
d = open_image(dst)
check(all(d.read(b["target"] * 512, 512) == bytes(512) for b in res["bad_lbas"]), "bad sectors copied as zeros")
html = open(res["report"], encoding="utf-8").read()
check("Passed with problems" in html and str(sorted(bad)[0]) in html, "report lists the bad sectors")
F.cleanup(dst)
many = set(range(runs["cache.bin"], runs["cache.bin"] + 200))
dst = F.blank(os.path.join(w, "dst2.img"), 320)
pf, res = clone(src, dst, F.FakeLayer(w, bad_lbas=many), max_bad=50)
check(status(res) == "failed" and "failing" in res.get("error", "") and sysclone.is_marked_unusable(open_image(dst)),
      "a failing source stops the clone, verdict failed, target marked unusable")
F.cleanup(w)

# 8. cancel mid-copy ----------------------------------------------------------------------------------------------------
w = case("cancel")
src = source(w)
dst = F.blank(os.path.join(w, "dst.img"), 400, b"previous contents")


class CancelAt(Progress):
    def update(self, done):
        super().update(done)
        if self.label.startswith(sysclone.PHASE_LABELS["copy"]) and done >= 40 * MiB:
            self.cancel()


L = F.FakeLayer(w)
s, d = open_image(src), open_image(dst)
pf = sysclone.preflight(s, d, L, **SMALL)
t = time.time()
try:
    sysclone.run_clone(s, d, pf.plan, L, CancelAt(1), report_dir=w)
    check(False, "cancel raises Cancelled")
except Cancelled as c:
    info = c.info
    check(info["state"] == "marked-unusable" and info["written"] > 0 and time.time() - t < 30,
          f"cancel stops the clone: {info['state']}, {info['written'] >> 20} MiB written")
dd = open_image(dst)
check(sysclone.is_marked_unusable(dd) and partitions.read_partition_table(dd).scheme == "None" and
      dd.read(dd.usable_size - MiB, MiB) == bytes(MiB), "cancelled target: no partition table, marked unusable")
check(not F.FakeLayer.store and not os.listdir(app_dir() / "sysclone") and L.awake[-1] is False,
      "cancel released the snapshot and removed the journal")
F.cleanup(w)

# 9. Windows keeps writing to C: during the copy ------------------------------------------------------------------------
w = case("livewrites")
src = source(w)
dst = F.blank(os.path.join(w, "dst.img"), 400)
lay = sysclone.read_layout(open_image(src), F.FakeLayer(w).volumes(open_image(src)))
winp = next(p for p in lay.parts if p.role == "windows")
live = {"writes": 0}


class Scribble(Progress):
    def update(self, done):
        super().update(done)
        if self.label.startswith(sysclone.PHASE_LABELS["copy"]) and not live["writes"] and done > 20 * MiB:
            with open(src, "r+b") as f:  # overwrite the whole Windows partition except its boot sector
                junk = os.urandom(MiB)
                for off in range(winp.start_lba * 512 + MiB, (winp.start_lba + winp.sectors) * 512, MiB):
                    f.seek(off)
                    f.write(junk)
            live["writes"] = 1


pf, res = clone(src, dst, F.FakeLayer(w), prog=Scribble(1))
check(live["writes"] and status(res) == "passed", f"writes to C: during the copy: verdict {status(res)}")
check(not fs_problems(dst, pf.plan), "the copy is the snapshot, not the scribbled live disk")
check(F.ntfs_file(open_image(src), winp.start_lba * 512, "Windows/System32/config/SYSTEM") !=
      F._data("Windows/System32/config/SYSTEM", F.WIN_FILES["Windows/System32/config/SYSTEM"]),
      "(the live source really changed)")
F.cleanup(w)

# 10. verification catches a corrupted target byte ----------------------------------------------------------------------
w = case("corrupt")
src = source(w)


def corrupting(dev, once):
    hit = {"n": 0, "at": None}
    orig = dev.write

    def write(off, data):
        if len(data) >= MiB and 2 * MiB <= off < 200 * MiB and (hit["at"] is None or (not once and off == hit["at"])):
            if hit["at"] is None:
                hit["at"] = off
            hit["n"] += 1
            b = bytearray(data)
            b[12345] ^= 0x01
            data = bytes(b)
        return orig(off, data)
    dev.write = write
    return hit


dst = F.blank(os.path.join(w, "dst.img"), 400)
s, d = open_image(src), open_image(dst)
L = F.FakeLayer(w)
pf = sysclone.preflight(s, d, L, **SMALL)
hit = corrupting(d, once=True)
res = sysclone.run_clone(s, d, pf.plan, L, Progress(1), report_dir=w)
check(hit["n"] == 1 and status(res) == "passed" and any("copied again" in n for n in res["notes"]),
      f"one flipped bit is caught by the read-back and copied again: {res['notes']}")
F.cleanup(dst)
dst = F.blank(os.path.join(w, "dst2.img"), 400)
s, d = open_image(src), open_image(dst)
L = F.FakeLayer(w)
pf = sysclone.preflight(s, d, L, **SMALL)
hit = corrupting(d, once=False)
res = sysclone.run_clone(s, d, pf.plan, L, Progress(1), report_dir=w)
check(status(res) == "failed" and failed_check(res) == "Every copied block matches the snapshot" and
      sysclone.is_marked_unusable(open_image(dst)) and res["target_state"] == "marked-unusable",
      f"a drive that keeps storing a wrong byte: verdict {status(res)} ({failed_check(res)}), target marked unusable")
check("Failed" in open(res["report"], encoding="utf-8").read(), "failed clone still writes its report")
F.cleanup(dst)

# boot configuration pointing at the old drive, extend failing, snapshot lost
dst = F.blank(os.path.join(w, "dst3.img"), 400)
pf, res = clone(src, dst, F.FakeLayer(w, bcd_wrong=True))
check(status(res) == "failed" and failed_check(res) == "Boot configuration points at the new drive" and
      sysclone.is_marked_unusable(open_image(dst)), "BCD still pointing at the old drive fails the clone")
F.cleanup(dst)
dst = F.blank(os.path.join(w, "dst4.img"), 400)
pf, res = clone(src, dst, F.FakeLayer(w, extend_ok=False))
check(status(res) == "problems" and "Extend it" in json.dumps(res["checks"]),
      "extend failing: passed with problems, with what to do")
F.cleanup(dst)
dst = F.blank(os.path.join(w, "dst5.img"), 400)
L = F.FakeLayer(w)


class LoseSnapshot(Progress):
    def update(self, done):
        super().update(done)
        if self.label.startswith(sysclone.PHASE_LABELS["copy"]) and done > 30 * MiB and L.alive_flag:
            L.alive_flag = False
            for _o, _n, r in s._overlay:
                r._raw_read = lambda off, n: (_ for _ in ()).throw(OSError(1117, "The device is not ready"))


s, d = open_image(src), open_image(dst)
pf = sysclone.preflight(s, d, L, **SMALL)
res = sysclone.run_clone(s, d, pf.plan, L, LoseSnapshot(1), report_dir=w)
check(status(res) == "failed" and "deleted the snapshot" in res["error"] and sysclone.is_marked_unusable(d),
      f"snapshot removed by Windows mid-copy: failed, not copied as bad sectors ({res.get('error', '')[:60]})")
F.cleanup(w)

# 11. preflight refusals ---------------------------------------------------------------------------------------------------
w = case("preflight")
src = source(w)
dst = F.blank(os.path.join(w, "dst.img"), 400)


def pf_with(key, **kw):
    s, d = open_image(src), kw.pop("dst", None) or open_image(dst)
    take = kw.pop("take_offline", False)
    r = sysclone.preflight(s, d, F.FakeLayer(w, **kw), take_offline=take, **SMALL)
    return next(c for c in r.checks if c.key == key), r


for key, kw, words in (
        ("admin", {"admin": False}, "administrator"),
        ("power", {"power": {"ac": False, "percent": 12}}, "battery"),
        ("vss", {"vss": (False, "The Volume Shadow Copy service is disabled.")}, "disabled"),
        ("source", {"flags": {"dynamic": True}}, "dynamic"),
        ("source", {"flags": {"storage_spaces": True}}, "Storage Spaces"),
        ("source", {"flags": {"raid_volume": True, "detail": "Intel RAID 0 volume"}}, "RAID"),
        ("target", {"usage": {"mounted": ["E:"], "busy": [], "pagefile": []}}, "E:"),
        ("target", {"usage": {"mounted": [], "busy": [], "pagefile": ["E:"]}}, "page file"),
        ("shadow", {"free": 100 * MiB}, "free space")):
    c, r = pf_with(key, **kw)
    if key == "shadow":
        r = sysclone.preflight(open_image(src), open_image(dst), F.FakeLayer(w, **kw), min_free=8 * MiB,
                               shadow_min=2 * 1024 ** 3)
        c = next(x for x in r.checks if x.key == key)
    check(c.status == "block" and words.lower() in (c.detail + c.fix).lower() and r.plan is None,
          f"preflight blocks ({key}): {c.detail} / {c.fix}")
c, r = pf_with("target", usage={"mounted": ["E:"], "busy": [], "pagefile": []}, take_offline=True)
check(c.status == "ok" and "offline" in c.detail and r.plan is not None, "mounted target allowed when taken offline")
off = sysclone.read_layout(open_image(src)).parts[2].start_lba * 512
c, r = pf_with("bitlocker", bitlocker={off: "locked"})
check(c.status == "block" and "Unlock" in c.fix, f"BitLocker locked: {c.detail}")
c, r = pf_with("bitlocker", bitlocker={off: "converting"})
check(c.status == "block" and "finishes" in c.fix, f"BitLocker converting: {c.detail}")
c, r = pf_with("bitlocker", bitlocker={off: "unlocked"})
check(c.status == "warn" and "not be encrypted" in c.detail and r.plan is not None,
      "BitLocker unlocked: warns the clone won't be encrypted, still allowed")
s = open_image(src)
c = next(x for x in sysclone.preflight(s, open_image(src), F.FakeLayer(w), **SMALL).checks if x.key == "target")
check(c.status == "block" and "runs from" in c.detail, "target = source refused")
sysd = open_image(dst)
sysd.is_system = True
c = next(x for x in sysclone.preflight(s, sysd, F.FakeLayer(w), **SMALL).checks if x.key == "target")
check(c.status == "block" and "system" in c.detail, "system disk as target refused")
c = next(x for x in sysclone.preflight(s, None, F.FakeLayer(w), **SMALL).checks if x.key == "target")
check(c.status == "block", "no target refused")
# a check that crashes blocks instead of passing


class Broken(F.FakeLayer):
    def vss_status(self):
        raise RuntimeError("WMI not responding")


c = next(x for x in sysclone.preflight(s, open_image(dst), Broken(w), **SMALL).checks if x.key == "vss")
check(c.status == "block" and "WMI not responding" in c.detail, "a check that errors blocks (fails closed)")
# GPT without an EFI partition, MBR with an extended partition
noesp = os.path.join(w, "noesp.img")
shutil.copyfile(src, noesp)
subprocess.run(["sgdisk", "-t1:0700", noesp], capture_output=True)
c = next(x for x in sysclone.preflight(open_image(noesp), open_image(dst), F.FakeLayer(w), **SMALL).checks
         if x.key == "source")
check(c.status == "block" and "EFI" in c.detail, f"no EFI partition refused: {c.detail}")
os.remove(noesp)
F.cleanup(src)
src = source(w, scheme="MBR")
with open(src, "r+b") as f:
    f.seek(446 + 48)
    f.write(struct.pack("<B3sB3sII", 0, b"\xfe\xff\xff", 0x05, b"\xfe\xff\xff", 700000, 2048))
c = next(x for x in sysclone.preflight(open_image(src), open_image(dst), F.FakeLayer(w), **SMALL).checks
         if x.key == "source")
check(c.status == "block" and "extended" in c.detail, f"MBR extended partition refused: {c.detail}")
item_keys = [k for k, _t in sysclone.PREFLIGHT_ITEMS]
seen = []
sysclone.preflight(open_image(src), open_image(dst), F.FakeLayer(w), on_check=lambda c: seen.append(c.key), **SMALL)
check(seen == item_keys, "preflight reports every item in order (the wizard ticks them off)")
F.cleanup(w)

# 12. BitLocker re-enable option and leftover snapshots at start-up ---------------------------------------------------------
w = case("misc")
src = source(w)
dst = F.blank(os.path.join(w, "dst.img"), 400)
L = F.FakeLayer(w)
pf, res = clone(src, dst, L, bitlocker_on_boot=True)
check(("registry", 4, True) in L.calls and any("BitLocker" in n for n in res["notes"]),
      "BitLocker re-enable on first sign-in is passed to the registry fix-up")
F.cleanup(dst)
folder = app_dir() / "sysclone"
os.makedirs(folder, exist_ok=True)
L = F.FakeLayer(w)
s = open_image(src)
snap = L.create_snapshot(s, [{"offset": 0, "length": MiB}], lambda i: None)
old = {"id": "old1", "created": time.time() - 72 * 3600, "state": "running", "heartbeat": 0,
       "snapshot": snap.info}
recent = {"id": "new1", "created": time.time() - 600, "state": "running", "heartbeat": time.time() - 600,
          "snapshot": {"set_id": "keep-me"}}
for j in (old, recent):
    with open(folder / f"{j['id']}.json", "w") as f:
        json.dump(j, f)
done = sysclone.cleanup_stale(L)
check(snap.info["set_id"] in L.deleted and not os.path.exists(folder / "old1.json") and
      os.path.exists(folder / "new1.json"), f"start-up clean-up: old snapshot deleted, recent one kept {done}")
check([j.id for j in sysclone.interrupted()] == ["new1"], "the recent interrupted clone is offered for resume")
sysclone.discard(sysclone.interrupted()[0], L)
check(not os.listdir(folder) and "keep-me" in L.deleted, "discarding it releases its snapshot")
F.cleanup(w)

print(f"\n{sum(OK)}/{len(OK)} checks passed")
shutil.rmtree(W, ignore_errors=True)
sys.exit(0 if all(OK) else 1)
