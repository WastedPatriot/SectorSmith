"""Clone this PC on real Windows (CI windows job, run as Administrator).

Builds a source VHDX like a Windows system disk (GPT: EFI, MSR, an NTFS "Windows" volume with a Windows-like tree,
Recovery), gives it a real BCD with bcdboot, then clones it with the real engine and WindowsLayer while files keep
being written to its NTFS volume: once to a bigger VHDX that still has a mounted volume (taken offline, Windows
grown) and once to a smaller one (Windows shrunk). Each clone is then checked independently: bcdedit /store,
chkdsk, the target's MountedDevices, the files, and that no shadow copy is left behind. Both disks are detached and
deleted even when something fails.
"""
import hashlib
import os
import shutil
import string
import subprocess
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sectorsmith import sysclone, verify  # noqa: E402
from sectorsmith.device import list_disks, windows_volume_map  # noqa: E402
from sectorsmith.sysclone_win import WindowsLayer, diskpart  # noqa: E402
from sectorsmith.util import Progress, is_admin  # noqa: E402

assert sys.platform == "win32" and is_admin(), "run on Windows as Administrator"
OK = []
RT = os.environ.get("RUNNER_TEMP", tempfile.gettempdir())
REPORTS = os.path.join(RT, "sysclone_reports")
os.makedirs(REPORTS, exist_ok=True)
SRC_VHD, BIG_VHD, SMALL_VHD = (os.path.join(RT, n) for n in ("ss_src.vhdx", "ss_big.vhdx", "ss_small.vhdx"))
MiB = 1024 * 1024
SYS = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    OK.append(bool(cond))


def run(args, **kw):
    r = subprocess.run(args, capture_output=True, text=True, errors="replace", **kw)
    return r


def ps(script):
    r = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])
    return r.stdout.strip()


def dp(script):
    ok, out = diskpart(script)
    print(out[-500:])
    return ok


def free_letters(n):
    out = [c for c in reversed(string.ascii_uppercase[4:]) if not os.path.exists(f"{c}:\\")]
    return out[:n]


def disk_of(vhd) -> int:
    return int(ps(f"(Get-DiskImage -ImagePath '{vhd}' | Get-Disk).Number"))


def device(n):
    return next(d for d in list_disks() if d.disk_number == n)


def partitions_of(n):
    out = ps(f"Get-Partition -DiskNumber {n} | ForEach-Object {{ \"$($_.PartitionNumber)|$($_.Offset)|$($_.Size)|"
             f"$($_.DriveLetter)|$($_.GptType)\" }}")
    res = []
    for line in out.splitlines():
        p = line.strip().split("|")
        if len(p) == 5 and p[0].isdigit():
            res.append({"number": int(p[0]), "offset": int(p[1]), "size": int(p[2]), "letter": p[3].strip(),
                        "type": p[4].strip("{}").lower()})
    return res


def detach(vhd):
    if os.path.exists(vhd):
        dp(f'select vdisk file="{vhd}"\ndetach vdisk noerr\n')
        try:
            os.remove(vhd)
        except OSError as e:
            print("INFO couldn't delete", vhd, e)


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(4 * MiB), b""):
            h.update(b)
    return h.hexdigest()


def reg_hive_with(key, values: dict, out):
    """Save HKCU\\Software\\<key> (with MountedDevices binary values) as a hive file, like a real SYSTEM hive."""
    run(["reg", "delete", f"HKCU\\Software\\{key}", "/f"])
    for name, data in values.items():
        r = run(["reg", "add", f"HKCU\\Software\\{key}\\MountedDevices", "/v", name, "/t", "REG_BINARY", "/d",
                 data.hex(), "/f"])
        assert r.returncode == 0, r.stderr
    run(["reg", "add", f"HKCU\\Software\\{key}\\Microsoft\\Windows\\CurrentVersion\\RunOnce", "/f"])
    if os.path.exists(out):
        os.remove(out)
    r = run(["reg", "save", f"HKCU\\Software\\{key}", out, "/y"])
    assert r.returncode == 0, r.stderr
    run(["reg", "delete", f"HKCU\\Software\\{key}", "/f"])


def target_view(n, plan):
    """Give the clone's boot and Windows partitions letters so they can be inspected; returns (boot, win, undo)."""
    parts = {p["offset"]: p for p in partitions_of(n)}
    ss = plan.sector_size
    letters = {}
    undo = []
    for role, part in (("boot", plan.boot_part()), ("windows", plan.part("windows"))):
        gp = parts[part.start * ss]
        if gp["letter"]:
            letters[role] = gp["letter"]
            continue
        lt = free_letters(1)[0]
        dp(f"select disk {n}\nselect partition {gp['number']}\nassign letter={lt}\n")
        letters[role] = lt
        undo.append((gp["number"], lt))
    return letters["boot"], letters["windows"], undo


def mounted_devices(hive):
    import winreg
    key = f"SS_T_{uuid.uuid4().hex[:6]}"
    r = run(["reg", "load", f"HKLM\\{key}", hive])
    assert r.returncode == 0, r.stderr
    vals = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{key}\\MountedDevices") as k:
            i = 0
            while True:
                try:
                    name, data, _t = winreg.EnumValue(k, i)
                except OSError:
                    break
                vals[name] = bytes(data)
                i += 1
        runonce = None
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{key}\\Microsoft\\Windows\\CurrentVersion\\RunOnce") as k:
                runonce = winreg.QueryValueEx(k, "SectorSmithBitLocker")[0]
        except OSError:
            pass
    finally:
        import gc
        gc.collect()
        for _ in range(10):
            if run(["reg", "unload", f"HKLM\\{key}"]).returncode == 0:
                break
            time.sleep(1)
    return vals, runonce


class Writer(threading.Thread):
    """Keeps writing new files to the source volume, like Windows does while it runs."""

    def __init__(self, root):
        super().__init__(daemon=True)
        self.root, self.stop, self.n = root, threading.Event(), 0

    def run(self):
        os.makedirs(self.root, exist_ok=True)
        while not self.stop.is_set():
            try:
                with open(os.path.join(self.root, f"live_{self.n}.bin"), "wb") as f:
                    f.write(os.urandom(256 * 1024))
                if self.n > 4:
                    os.remove(os.path.join(self.root, f"live_{self.n - 4}.bin"))
                self.n += 1
            except OSError:
                pass
            time.sleep(0.05)


src_vols = []
try:
    for v in (SRC_VHD, BIG_VHD, SMALL_VHD):
        detach(v)
    W, S = free_letters(2)
    # --- source: a Windows-like GPT system disk -------------------------------------------------------------------
    check(dp(f'create vdisk file="{SRC_VHD}" maximum=1200 type=expandable\nselect vdisk file="{SRC_VHD}"\n'
             'attach vdisk\nconvert gpt noerr\n'
             'create partition efi size=100\nformat quick fs=fat32 label=SYSTEM\n'
             'create partition msr size=16 noerr\n'
             f'create partition primary size=850\nformat quick fs=ntfs label=Windows\nassign letter={W}\n'
             'create partition primary\nformat quick fs=ntfs label=Recovery\n'
             'set id="de94bba4-06d1-4d40-a16a-bfd50179d6ac"\ngpt attributes=0x8000000000000001\n'),
          "source VHDX created (EFI, MSR, Windows, Recovery)")
    time.sleep(3)
    n_src = disk_of(SRC_VHD)
    src = device(n_src)
    src_vols = [v["guid"] for v in windows_volume_map() if v["disk"] == n_src]
    root = f"{W}:\\"
    files = {}
    for rel in ("Windows\\System32\\ntoskrnl.exe", "Windows\\System32\\hal.dll", "Windows\\System32\\winload.efi",
                "Windows\\System32\\drivers\\ntfs.sys", "Windows\\explorer.exe",
                "Windows\\System32\\config\\BCD-Template"):
        os.makedirs(os.path.dirname(root + rel), exist_ok=True)
        try:
            shutil.copyfile(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), rel.split("\\", 1)[1]),
                            root + rel)
        except OSError as e:
            print("INFO not copied", rel, e)
    shutil.copytree(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Boot"), root + "Windows\\Boot",
                    dirs_exist_ok=True)
    for i in range(12):
        p = root + f"Users\\Bob\\Documents\\file_{i}.bin"
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(os.urandom((3 + i) * MiB))
        files[p[3:]] = sha(p)
    lay = sysclone.read_layout(src)
    print([(p.role, p.start_lba, p.sectors, p.fs, p.unique) for p in lay.parts])
    win_part = next(p for p in lay.parts if p.fs == "NTFS" and p.type_id == sysclone.GUID_BASIC)
    esp_part = next(p for p in lay.parts if p.role == "esp")
    md = {f"\\DosDevices\\{W}:": b"DMIO:ID:" + uuid.UUID(win_part.unique).bytes_le,
          "\\??\\Volume{00000000-1111-2222-3333-444444444444}": b"DMIO:ID:" + uuid.UUID(esp_part.unique).bytes_le}
    os.makedirs(root + "Windows\\System32\\config", exist_ok=True)
    reg_hive_with("SSCloneSys", md, root + "Windows\\System32\\config\\SYSTEM")
    reg_hive_with("SSCloneSw", {}, root + "Windows\\System32\\config\\SOFTWARE")
    for rel in ("Windows\\System32\\config\\SYSTEM", "Windows\\System32\\config\\SOFTWARE"):
        files[rel] = sha(root + rel)
    # a real BCD on the source's ESP, pointing at the source (the clone must not keep pointing there)
    esp_num = next(p["number"] for p in partitions_of(n_src) if p["type"] == sysclone.GUID_ESP)
    dp(f"select disk {n_src}\nselect partition {esp_num}\nassign letter={S}\n")
    r = run([os.path.join(SYS, "bcdboot.exe"), f"{W}:\\Windows", "/s", f"{S}:", "/f", "UEFI"])
    own_boot = r.returncode == 0
    print("bcdboot on the source:", r.returncode, r.stdout[-300:], r.stderr[-300:])
    print("INFO boot files come from", "the source's own Windows folder" if own_boot else "the runner's C:\\Windows")
    dp(f"select disk {n_src}\nselect partition {esp_num}\nremove letter={S}\n")

    def one_clone(vhd, size_mib, label, take_offline, bitlocker):
        script = f'create vdisk file="{vhd}" maximum={size_mib} type=expandable\nselect vdisk file="{vhd}"\nattach vdisk\n'
        if take_offline:   # an old volume with a letter on the target, as a drive bought second hand would have
            t = free_letters(3)[2]
            script += f"convert gpt noerr\ncreate partition primary\nformat quick fs=ntfs label=OLD\nassign letter={t}\n"
        dp(script)
        time.sleep(3)
        n = disk_of(vhd)
        dst = device(n)
        print(f"   {label}: target is disk {n}, {dst.size >> 20} MiB")
        layer = WindowsLayer(windows_letter=W, bcdboot_source=None if own_boot else r"C:\Windows")
        min_free = 32 * MiB
        pf = sysclone.preflight(src, dst, layer, take_offline=False, min_free=min_free, shadow_min=0)
        for c in pf.checks:
            print(f"   {c.status:5} {c.title}: {c.detail} {c.fix}")
        if take_offline:
            tc = next(c for c in pf.checks if c.key == "target")
            check(tc.status == "block" and "offline" in tc.fix, f"{label}: target with a mounted volume is refused "
                                                                  "until it is taken offline")
            pf = sysclone.preflight(src, dst, layer, take_offline=True, min_free=min_free, shadow_min=0)
        check(not pf.blocked, f"{label}: preflight passes {[c.detail for c in pf.blocking()]}")
        if pf.blocked:
            return None
        w = Writer(root + "Users\\Live")
        w.start()
        t0 = time.time()
        res = sysclone.run_clone(src, dst, pf.plan, layer, Progress(1), report_dir=REPORTS,
                                 bitlocker_on_boot=bitlocker)
        w.stop.set()
        w.join(5)
        print(f"   {label}: {time.time() - t0:.1f}s, {w.n} files written to the source meanwhile")
        for c in res["checks"]:
            print(f"   {c['status']:8} {c['name']}: {c['detail']}")
        print("   notes:", res["notes"], "snapshot:", res["snapshot"], res["snapshot_mode"])
        print("   bcdedit:", layer.last_bcdedit[-1500:])
        check(w.n > 5, f"{label}: the source volume kept changing during the clone ({w.n} files)")
        check(res["snapshot_mode"] == "set", f"{label}: one VSS snapshot set (mode {res['snapshot_mode']})")
        ok_status = res["verify"]["status"] == verify.PASSED
        check(ok_status, f"{label}: verdict {res['verify']['status']}: {res['verify']['summary']}")
        check(res.get("report") and os.path.exists(res["report"]), f"{label}: report written {res.get('report')}")
        left = [line for line in ps("Get-CimInstance Win32_ShadowCopy | ForEach-Object { $_.VolumeName }").splitlines()
                if line.strip() in src_vols]
        check(not left, f"{label}: no shadow copy left behind {left}")
        if not ok_status:
            check(sysclone.is_marked_unusable(device(n)), f"{label}: a failed clone leaves the target marked unusable")
            return res
        plan = pf.plan
        boot, win, undo = target_view(n, plan)
        try:
            out = run([os.path.join(SYS, "bcdedit.exe"), "/store", f"{boot}:\\EFI\\Microsoft\\Boot\\BCD", "/enum",
                       "all"]).stdout
            print(out[-2500:])
            check(f"partition={boot}:" in out and f"partition={win}:" in out and f"partition={W}:" not in out,
                  f"{label}: bcdedit /store shows the boot manager on {boot}: and Windows on {win}:")
            r = run(["chkdsk", f"{win}:"])
            print(r.stdout[-800:])
            check(r.returncode == 0, f"{label}: chkdsk {win}: finds no problems (exit {r.returncode})")
            vals, runonce = mounted_devices(f"{win}:\\Windows\\System32\\config\\SYSTEM")
            new_win = uuid.UUID(plan.part("windows").unique).bytes_le
            check(vals.get(f"\\DosDevices\\{W}:") == b"DMIO:ID:" + new_win and
                  not any(uuid.UUID(win_part.unique).bytes_le in v for v in vals.values()),
                  f"{label}: the clone's MountedDevices names its own Windows partition")
            if bitlocker:
                vals, runonce = mounted_devices(f"{win}:\\Windows\\System32\\config\\SOFTWARE")
                check(runonce and "BitLocker" in runonce, f"{label}: BitLocker settings open at first sign-in")
            bad = [rel for rel, h in files.items() if rel.startswith("Users") and sha(f"{win}:\\{rel}") != h]
            check(not bad, f"{label}: files on the clone match the source {bad[:3]}")
            total = shutil.disk_usage(f"{win}:\\").total
            want = plan.part("windows").sectors * plan.sector_size
            check(total > want - 64 * MiB, f"{label}: Windows volume fills its partition ({total >> 20} of "
                                           f"{want >> 20} MiB)")
            gpt = partitions_of(n)
            check([p["offset"] for p in gpt] == [p.start * plan.sector_size for p in plan.parts],
                  f"{label}: Windows reads the partition table as planned")
        finally:
            for num, lt in undo:
                dp(f"select disk {n}\nselect partition {num}\nremove letter={lt}\n")
        return res

    res_big = one_clone(BIG_VHD, 1600, "bigger target", take_offline=True, bitlocker=True)
    if res_big and res_big["verify"]["status"] == verify.PASSED:
        check(any(p["resize"] == "grow" for p in res_big["plan"]["parts"]), "bigger target: Windows grown")
    detach(BIG_VHD)
    # smaller: only as much smaller as the used space on the source allows
    usage = sysclone.ntfs_usage(src, win_part.start_lba * src.sector_size)
    shrink_to = ((usage["extent"] + 96 * MiB) // MiB) * MiB
    small = (src.size - win_part.sectors * src.sector_size + shrink_to) // MiB + 2
    print(f"   used extent of the Windows volume {usage['extent'] >> 20} MiB, smaller target {small} MiB")
    if small < src.size // MiB - 16:
        res_small = one_clone(SMALL_VHD, small, "smaller target", take_offline=False, bitlocker=False)
        if res_small and res_small["verify"]["status"] == verify.PASSED:
            check(any(p["resize"] == "shrink" for p in res_small["plan"]["parts"]), "smaller target: Windows shrunk")
    else:
        print("INFO Windows placed files near the end of the volume; shrink clone not possible on this runner")
finally:
    for sid in [line.split("|")[0] for line in ps("Get-CimInstance Win32_ShadowCopy | ForEach-Object "
                                                  "{ $_.ID + '|' + $_.VolumeName }").splitlines()
                if line.strip().split("|")[-1] in src_vols]:
        ps(f"Get-CimInstance Win32_ShadowCopy | Where-Object {{ $_.ID -eq '{sid}' }} | Remove-CimInstance")
    for v in (BIG_VHD, SMALL_VHD, SRC_VHD):
        detach(v)

print(f"\n{sum(OK)}/{len(OK)} checks passed")
sys.exit(0 if all(OK) else 1)
