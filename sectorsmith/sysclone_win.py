"""Windows side of "Clone this PC": power and sleep, VSS snapshot sets, BitLocker state, drive letters for the
target, bcdboot, reading the target's BCD store and patching its MountedDevices. Used through sysclone.SystemLayer.
"""
from __future__ import annotations

import ctypes
import os
import string
import subprocess
import tempfile
import time
import uuid

from .device import Device, windows_volume_map
from .sysclone import Snapshot, SysCloneError, SystemLayer
from .util import check_cancel, get_logger, is_admin

log = get_logger()
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SYSTEM32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")

ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
VSS_CTX_CLIENT_ACCESSIBLE = 0x1D   # persistent, client accessible, no auto release, no writers
VSS_BT_FULL = 1
VSS_S_ASYNC_FINISHED = 0x0004230A
BCD_BOOTMGR, BCD_OSLOADER = 0x10100002, 0x10200003


def _run(args, timeout=120, inp=None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=inp,
                          creationflags=NO_WINDOW, errors="replace")


def _ps(script: str, timeout=120) -> str:
    r = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
             timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[-400:])
    return r.stdout.strip()


def diskpart(script: str, timeout=180) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(script)
    try:
        r = _run([os.path.join(SYSTEM32, "diskpart.exe"), "/s", f.name], timeout=timeout)
    finally:
        os.remove(f.name)
    return r.returncode == 0, (r.stdout + r.stderr)[-1500:]


def _q(s: str) -> str:
    return s.replace("'", "''")


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]

    def __str__(self):
        return str(uuid.UUID(bytes_le=bytes(self)))


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def _vcall(obj, index, argtypes, *args) -> int:
    """Call method ``index`` of a COM interface pointer; returns the HRESULT."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])
    return fn(obj, *args)


def _hr(hr: int, what: str):
    if hr < 0:
        raise OSError(hr & 0xFFFFFFFF, f"{what} failed (0x{hr & 0xFFFFFFFF:08X})")


def _wait_async(async_ptr, what: str):
    """IVssAsync: Wait (4), QueryStatus (5), Release (2)."""
    try:
        _hr(_vcall(async_ptr, 4, [ctypes.c_ulong], 0xFFFFFFFF), what + " wait")
        status = ctypes.c_long(0)
        _hr(_vcall(async_ptr, 5, [ctypes.POINTER(ctypes.c_long), ctypes.c_void_p], ctypes.byref(status), None),
            what + " status")
        if status.value < 0 or status.value != VSS_S_ASYNC_FINISHED:
            raise OSError(status.value & 0xFFFFFFFF, f"{what} ended with 0x{status.value & 0xFFFFFFFF:08X}")
    finally:
        _vcall(async_ptr, 2, [])


def create_snapshot_set(volume_names: list[str], on_set=None) -> tuple[str, list[str]]:
    """One VSS snapshot set over every volume in ``volume_names`` ('\\\\?\\Volume{...}\\'), taken at the same
    instant. Returns (set id, [volume names that are in the set]). IVssBackupComponents through its vtable:
    InitializeForBackup 5, SetBackupState 6, PrepareForBackup 14, SetContext 35, StartSnapshotSet 36,
    AddToSnapshotSet 37, DoSnapshotSet 38, Release 2."""
    ole32 = ctypes.WinDLL("ole32")
    vssapi = ctypes.WinDLL("vssapi")
    hr = ole32.CoInitializeEx(None, 0)  # multithreaded; this is the job's own thread
    inited = hr in (0, 1)
    ole32.CoInitializeSecurity(None, -1, None, None, 6, 2, None, 0x20, None)  # may fail if already set: fine
    comp = ctypes.c_void_p()
    try:
        _hr(vssapi.CreateVssBackupComponentsInternal(ctypes.byref(comp)), "CreateVssBackupComponents")
        _hr(_vcall(comp, 5, [ctypes.c_void_p], None), "InitializeForBackup")
        _hr(_vcall(comp, 35, [ctypes.c_long], VSS_CTX_CLIENT_ACCESSIBLE), "SetContext")
        hr = _vcall(comp, 6, [ctypes.c_bool, ctypes.c_bool, ctypes.c_int, ctypes.c_bool], False, False, VSS_BT_FULL,
                    False)
        if hr < 0:
            log.info("VSS SetBackupState returned 0x%08X (ignored without writers)", hr & 0xFFFFFFFF)
        set_id = _GUID()
        _hr(_vcall(comp, 36, [ctypes.POINTER(_GUID)], ctypes.byref(set_id)), "StartSnapshotSet")
        if on_set:
            on_set(str(set_id))
        added = []
        for name in volume_names:
            snap_id = _GUID()
            hr = _vcall(comp, 37, [ctypes.c_wchar_p, _GUID, ctypes.POINTER(_GUID)], name, _GUID(),
                        ctypes.byref(snap_id))
            if hr < 0:
                log.warning("VSS AddToSnapshotSet %s: 0x%08X", name, hr & 0xFFFFFFFF)
                continue
            added.append(name)
        if not added:
            raise OSError(0, "no volume could be added to the snapshot set")
        a = ctypes.c_void_p()
        _hr(_vcall(comp, 14, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(a)), "PrepareForBackup")
        _wait_async(a, "PrepareForBackup")
        a = ctypes.c_void_p()
        _hr(_vcall(comp, 38, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(a)), "DoSnapshotSet")
        _wait_async(a, "DoSnapshotSet")
        return str(set_id), added
    finally:
        if comp.value:
            _vcall(comp, 2, [])
        if inited:
            ole32.CoUninitialize()


def shadows(set_id: str | None = None, ids=None) -> list[dict]:
    """Shadow copies (by set or by id): {"id", "set", "device", "volume"}."""
    out = _ps("Get-CimInstance Win32_ShadowCopy | ForEach-Object { $_.ID + '|' + $_.SetID + '|' + $_.DeviceObject + "
              "'|' + $_.VolumeName }")
    res = []
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 4:
            continue
        sid, setid, dev, vol = parts
        if (set_id and setid.strip("{}").lower() == set_id.strip("{}").lower()) or \
                (ids and sid.strip("{}").lower() in {i.strip("{}").lower() for i in ids}):
            res.append({"id": sid, "set": setid, "device": dev, "volume": vol})
    return res


def delete_shadows(ids) -> None:
    for sid in ids:
        try:
            _ps(f"Get-CimInstance Win32_ShadowCopy | Where-Object {{ $_.ID -eq '{_q(sid)}' }} | Remove-CimInstance",
                timeout=120)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not delete shadow copy %s: %s", sid, e)


def _volume_info(guid: str) -> tuple[str, int | None, int | None]:
    k32 = ctypes.windll.kernel32
    fs = ctypes.create_unicode_buffer(64)
    ok = k32.GetVolumeInformationW(guid, None, 0, None, None, None, fs, 64)
    free, total = ctypes.c_ulonglong(0), ctypes.c_ulonglong(0)
    got = k32.GetDiskFreeSpaceExW(guid, ctypes.byref(free), ctypes.byref(total), None)
    return (fs.value if ok else ""), (free.value if got else None), (total.value if got else None)


def _system_volume_guid() -> str:
    k32 = ctypes.windll.kernel32
    root = os.environ.get("SystemRoot", r"C:\Windows")
    mp, g = ctypes.create_unicode_buffer(260), ctypes.create_unicode_buffer(260)
    if not k32.GetVolumePathNameW(root, mp, 260) or not k32.GetVolumeNameForVolumeMountPointW(mp.value, g, 260):
        return ""
    return g.value


def _free_letter(exclude=()) -> str:
    used = set(exclude)
    for c in reversed(string.ascii_uppercase[3:]):
        if c not in used and not os.path.exists(f"{c}:\\"):
            return c
    raise SysCloneError("No free drive letter for the new drive's partitions.", "Disconnect a drive and try again.")


class WindowsLayer(SystemLayer):
    name = "windows"

    def __init__(self, bcdboot_source: str | None = None, windows_letter: str | None = None):
        self.bcdboot_source = bcdboot_source   # tests: build the boot files from another Windows folder
        self.windows_letter = windows_letter   # tests: treat this volume as the one Windows runs from
        self.last_bcdedit = ""

    # -- checks ------------------------------------------------------------------------------------------------
    def is_admin(self):
        return is_admin()

    def power(self):
        st = _SYSTEM_POWER_STATUS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            return {"ac": None, "percent": None}
        if st.BatteryFlag == 128 or st.BatteryLifePercent == 255:  # no battery: a desktop
            return {"ac": True if st.ACLineStatus != 0 else None, "percent": None}
        return {"ac": {0: False, 1: True}.get(st.ACLineStatus), "percent": int(st.BatteryLifePercent)}

    def keep_awake(self, on):
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags)))

    def vss_status(self):
        out = _ps("foreach ($n in 'VSS','swprv') { $s = Get-CimInstance Win32_Service -Filter \"Name='$n'\"; "
                  "if ($s) { $n + '|' + $s.StartMode + '|' + $s.State } else { $n + '|missing|' } }")
        rows = dict((ln.split("|", 1)[0], ln.split("|")[1:]) for ln in out.splitlines() if "|" in ln)
        for name, title in (("VSS", "Volume Shadow Copy"), ("swprv", "Microsoft Software Shadow Copy Provider")):
            mode, _state = (rows.get(name) or ["missing", ""])[:2]
            if mode.lower() in ("disabled", "missing"):
                return False, f"The {title} service is {mode.lower()}."
        return True, "Volume Shadow Copy is available (it starts when needed)."

    def disk_flags(self, dev):
        model = (dev.model or "").lower()
        return {"storage_spaces": dev.bus == "Storage Spaces" or "storage space" in model,
                "raid_volume": dev.bus == "RAID" and ("raid" in model or "volume" in model),
                "detail": dev.model or dev.bus}

    def volumes(self, dev):
        sys_guid = _system_volume_guid().lower()
        if self.windows_letter:
            sys_guid = ""
        locks = self._bitlocker()
        out = []
        for v in windows_volume_map():
            if v["disk"] != dev.disk_number:
                continue
            fs, free, size = _volume_info(v["guid"])
            out.append({"offset": v["offset"], "length": v["length"], "letters": v["letters"], "guid": v["guid"],
                        "fs": fs, "free": free, "size": size,
                        "windows": v["guid"].lower() == sys_guid or (bool(self.windows_letter) and any(
                            lt.upper().startswith(self.windows_letter.upper()) for lt in v["letters"])),
                        "bitlocker": locks.get(v["guid"].lower(), "none")})
        return out

    def _bitlocker(self) -> dict:
        try:
            out = _ps("Get-CimInstance -Namespace root\\cimv2\\Security\\MicrosoftVolumeEncryption "
                      "-ClassName Win32_EncryptableVolume -ErrorAction Stop | ForEach-Object { "
                      "$l = (Invoke-CimMethod -InputObject $_ -MethodName GetLockStatus).LockStatus; "
                      "$_.DeviceID + '|' + $_.ConversionStatus + '|' + $l }")
        except Exception as e:  # noqa: BLE001  no BitLocker on this edition
            log.info("BitLocker state not available: %s", e)
            return {}
        res = {}
        for line in out.splitlines():
            parts = line.strip().split("|")
            if len(parts) != 3:
                continue
            dev, conv, lock = parts
            conv = int(conv or 0)
            if lock.strip() == "1":
                st = "locked"
            elif conv in (2, 3, 4, 5):
                st = "converting"
            elif conv == 1:
                st = "unlocked"
            else:
                st = "none"
            res[dev.lower()] = st
        return res

    def target_usage(self, dev):
        mounted, busy, pagefile = [], [], []
        vols = [v for v in windows_volume_map() if v["disk"] == dev.disk_number]
        try:
            pf = _ps("Get-CimInstance Win32_PageFileUsage | ForEach-Object { $_.Name }").upper()
        except Exception:  # noqa: BLE001
            pf = ""
        from .device import FSCTL_LOCK_VOLUME, FSCTL_UNLOCK_VOLUME, GENERIC_READ, GENERIC_WRITE, _open_handle
        k32 = ctypes.windll.kernel32
        for v in vols:
            name = (v["letters"] or [v["guid"]])[0].rstrip("\\")
            mounted.append(name)
            if any(lt.rstrip("\\").upper() + "\\" in pf for lt in v["letters"]):
                pagefile.append(name)
            try:
                h = _open_handle(v["guid"].rstrip("\\"), GENERIC_READ | GENERIC_WRITE)
            except OSError:
                busy.append(name)
                continue
            ret = ctypes.c_ulong(0)
            if k32.DeviceIoControl(h, FSCTL_LOCK_VOLUME, None, 0, None, 0, ctypes.byref(ret), None):
                k32.DeviceIoControl(h, FSCTL_UNLOCK_VOLUME, None, 0, None, 0, ctypes.byref(ret), None)
            else:
                busy.append(name)
            k32.CloseHandle(h)
        return {"mounted": [m for m in mounted if m not in busy], "busy": busy, "pagefile": pagefile}

    # -- snapshots ---------------------------------------------------------------------------------------------
    def create_snapshot(self, dev, volumes, on_id):
        vmap = {v["offset"]: v for v in windows_volume_map() if v["disk"] == dev.disk_number}
        wanted = [(vol, vmap[vol["offset"]]) for vol in volumes if vol["offset"] in vmap]
        names = [v["guid"] for _w, v in wanted]
        info = {"mode": "set", "set_id": None, "ids": [], "devices": {}, "sector_size": dev.sector_size}
        notes = []
        try:
            def on_set(sid):
                info["set_id"] = sid
                on_id(dict(info))
            check_cancel()
            set_id, _added = create_snapshot_set(names, on_set)
            found = shadows(set_id=set_id)
        except Exception as e:  # noqa: BLE001  fall back to one shadow copy per volume
            log.warning("VSS snapshot set failed, using one shadow copy per volume: %s", e)
            if info["set_id"]:
                self.delete_snapshot(info)
            info = {"mode": "per volume", "set_id": None, "ids": [], "devices": {}, "sector_size": dev.sector_size}
            notes.append(f"Snapshots were taken one volume at a time ({str(e)[:80]}); each volume is consistent "
                         "on its own.")
            from .vss import create_shadow
            found = []
            for _w, v in wanted:
                check_cancel()
                try:
                    sid, devobj = create_shadow(v["guid"])
                except Exception as e2:  # noqa: BLE001
                    notes.append(f"{(v['letters'] or [v['guid']])[0]}: no snapshot ({str(e2)[:80]})")
                    continue
                info["ids"].append(sid)
                on_id(dict(info))
                found.append({"id": sid, "device": devobj, "volume": v["guid"]})
        overlay, vols = [], []
        by_guid = {v["guid"].lower(): (w, v) for w, v in wanted}
        for s in found:
            hit = by_guid.get(s["volume"].lower())
            if not hit:
                continue
            _w, v = hit
            if s["id"] not in info["ids"]:
                info["ids"].append(s["id"])
            info["devices"][str(v["offset"])] = s["device"]
            r = Device(path=s["device"], name=f"snapshot {(v['letters'] or [v['guid']])[0]}", size=v["length"],
                       sector_size=dev.sector_size)
            r.open(writable=False)
            overlay.append((v["offset"], v["length"], r))
            vols.append((v["letters"] or [v["guid"]])[0].rstrip("\\"))
        on_id(dict(info))
        ids = list(info["ids"])
        return Snapshot(info, overlay, vols, notes, release=lambda: delete_shadows(ids),
                        alive=lambda: len(shadows(ids=ids)) == len(ids))

    def attach_snapshot(self, dev, info):
        ids = info.get("ids") or []
        found = shadows(set_id=info.get("set_id")) if info.get("set_id") else shadows(ids=ids)
        if not found or len(found) < len(ids):
            return None
        vmap = {v["guid"].lower(): v for v in windows_volume_map() if v["disk"] == dev.disk_number}
        overlay, devices = [], {}
        for s in found:
            v = vmap.get(s["volume"].lower())
            if v is None:
                return None
            r = Device(path=s["device"], name="snapshot", size=v["length"], sector_size=dev.sector_size)
            r.open(writable=False)
            overlay.append((v["offset"], v["length"], r))
            devices[str(v["offset"])] = s["device"]
        ids = [s["id"] for s in found]
        info = dict(info, ids=ids, devices=devices, sector_size=dev.sector_size)
        return Snapshot(info, overlay, [], [], release=lambda: delete_shadows(ids),
                        alive=lambda: len(shadows(ids=ids)) == len(ids))

    def delete_snapshot(self, info):
        ids = list(info.get("ids") or [])
        if info.get("set_id"):
            ids += [s["id"] for s in shadows(set_id=info["set_id"]) if s["id"] not in ids]
        delete_shadows(ids)

    # -- target fix-up -------------------------------------------------------------------------------------------
    def _partitions(self, disk: int) -> list[dict]:
        out = _ps(f"Update-Disk -Number {int(disk)} -ErrorAction SilentlyContinue; "
                  f"Get-Partition -DiskNumber {int(disk)} -ErrorAction SilentlyContinue | ForEach-Object "
                  "{ \"$($_.PartitionNumber)|$($_.Offset)|$($_.DriveLetter)\" }")
        res = []
        for line in out.splitlines():
            parts = line.strip().split("|")
            if len(parts) == 3 and parts[0].isdigit():
                res.append({"number": int(parts[0]), "offset": int(parts[1]), "letter": parts[2].strip("\0 ")})
        return res

    def mount_target(self, dst, plan):
        if dst.disk_number is None:
            raise SysCloneError("The target is not a physical disk.")
        ss = plan.sector_size
        want = {p.start * ss for p in plan.parts}
        parts = []
        for _ in range(30):  # Windows takes a moment to see the new partition table
            check_cancel()
            parts = self._partitions(dst.disk_number)
            if want <= {p["offset"] for p in parts}:
                break
            time.sleep(1)
        else:
            raise SysCloneError("Windows doesn't see the new partitions on the target.",
                                "Disconnect and reconnect the drive, then run the boot fix-up again.")
        by_off = {p["offset"]: p for p in parts}
        ctx = {"disk": dst.disk_number, "letters": {}, "assigned": [], "numbers": {}}
        taken = set()
        for role, part in (("boot", plan.boot_part()), ("windows", plan.part("windows"))):
            gp = by_off[part.start * ss]
            ctx["numbers"][role] = gp["number"]
            if gp["letter"] and gp["letter"].isalpha():
                ctx["letters"][role] = gp["letter"]
                continue
            if role == "boot" and ctx["letters"].get("windows") and plan.boot_part() is plan.part("windows"):
                continue
            letter = _free_letter(taken)
            ok, out = diskpart(f"select disk {dst.disk_number}\nselect partition {gp['number']}\n"
                               f"assign letter={letter}\n")
            if not ok or not os.path.exists(f"{letter}:\\"):
                time.sleep(2)
                if not os.path.exists(f"{letter}:\\"):
                    raise SysCloneError(f"Couldn't give the new {role} partition a temporary drive letter.",
                                        out.strip()[-200:])
            taken.add(letter)
            ctx["letters"][role] = letter
            ctx["assigned"].append((gp["number"], letter))
        if plan.boot_part() is plan.part("windows"):
            ctx["letters"]["boot"] = ctx["letters"]["windows"]
        return ctx

    def open_source_file(self, snap, part, relpath):
        info = snap.info or {}
        dev = (info.get("devices") or {}).get(str(part.src_start * int(info.get("sector_size") or 512)))
        if not dev:
            return None
        try:
            return open(dev + "\\" + relpath, "rb")
        except OSError:
            return None

    def open_target_file(self, ctx, part, relpath):
        try:
            return open(f"{ctx['letters']['windows']}:\\{relpath}", "rb")
        except OSError:
            return None

    def bcdboot(self, ctx, plan):
        win, boot = ctx["letters"]["windows"], ctx["letters"]["boot"]
        source = self.bcdboot_source or f"{win}:\\Windows"
        # the copied store still describes the old drive; bcdboot would keep its entries next to the new one, so
        # set it aside and let bcdboot write a fresh one
        store = self._store(ctx, plan)
        for suffix in ("", ".LOG", ".LOG1", ".LOG2"):
            if os.path.exists(store + suffix):
                old = store + suffix + ".sectorsmith-old"
                if os.path.exists(old):
                    os.remove(old)
                os.replace(store + suffix, old)
        r = _run([os.path.join(SYSTEM32, "bcdboot.exe"), source, "/s", f"{boot}:", "/f", plan.firmware],
                 timeout=300)
        return r.returncode == 0, (r.stdout + r.stderr).strip() or f"bcdboot exit code {r.returncode}"

    def _store(self, ctx, plan) -> str:
        boot = ctx["letters"]["boot"]
        return f"{boot}:\\EFI\\Microsoft\\Boot\\BCD" if plan.firmware == "UEFI" else f"{boot}:\\Boot\\BCD"

    def read_bcd(self, ctx, plan):
        import winreg
        store = self._store(ctx, plan)
        if not os.path.exists(store):
            raise SysCloneError(f"There is no BCD store at {store}.")
        r = _run([os.path.join(SYSTEM32, "bcdedit.exe"), "/store", store, "/enum", "all"])
        self.last_bcdedit = r.stdout
        key = f"SS_BCD_{uuid.uuid4().hex[:8]}"
        with _hive(key, store):
            objs = []
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{key}\\Objects") as root:
                for i in range(winreg.QueryInfoKey(root)[0]):
                    name = winreg.EnumKey(root, i)
                    try:
                        with winreg.OpenKey(root, f"{name}\\Description") as d:
                            typ = winreg.QueryValueEx(d, "Type")[0]
                    except OSError:
                        continue
                    kind = {BCD_BOOTMGR: "bootmgr", BCD_OSLOADER: "osloader"}.get(typ, f"0x{typ:08X}")
                    els = {}
                    for el in ("11000001", "21000001"):
                        try:
                            with winreg.OpenKey(root, f"{name}\\Elements\\{el}") as e:
                                els[el] = bytes(winreg.QueryValueEx(e, "Element")[0])
                        except OSError:
                            pass
                    objs.append({"kind": kind, "id": name, "elements": els})
        return objs

    def patch_registry(self, ctx, plan, replacements, bitlocker):
        import winreg
        win = ctx["letters"]["windows"]
        notes = []
        hive = f"{win}:\\Windows\\System32\\config\\SYSTEM"
        if not os.path.exists(hive):
            return False, [f"{hive} not found"]
        key = f"SS_SYS_{uuid.uuid4().hex[:8]}"
        changed = 0
        with _hive(key, hive):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{key}\\MountedDevices", 0,
                                    winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
                    values = []
                    i = 0
                    while True:
                        try:
                            values.append(winreg.EnumValue(k, i))
                        except OSError:
                            break
                        i += 1
                    for name, data, typ in values:
                        if typ != winreg.REG_BINARY:
                            continue
                        for old, new in replacements:
                            if bytes(data) == old:
                                winreg.SetValueEx(k, name, 0, winreg.REG_BINARY, new)
                                changed += 1
                                break
                notes.append(f"MountedDevices: {changed} entr{'y' if changed == 1 else 'ies'} moved to the new "
                             "partitions")
            except FileNotFoundError:
                notes.append("No MountedDevices key (Windows creates it at first start)")
        if bitlocker:
            sw = f"{win}:\\Windows\\System32\\config\\SOFTWARE"
            key = f"SS_SW_{uuid.uuid4().hex[:8]}"
            with _hive(key, sw):
                with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE,
                                        f"{key}\\Microsoft\\Windows\\CurrentVersion\\RunOnce") as k:
                    winreg.SetValueEx(k, "SectorSmithBitLocker", 0, winreg.REG_SZ,
                                      "control.exe /name Microsoft.BitLockerDriveEncryption")
            notes.append("BitLocker settings open at the first sign-in")
        return True, notes

    def extend_filesystem(self, ctx, plan):
        win = ctx["letters"]["windows"]
        ok, out = diskpart(f"select volume {win}\nextend filesystem\n")
        return ok, out.strip().splitlines()[-1] if out.strip() else ""

    def unmount_target(self, ctx):
        for number, letter in ctx.get("assigned", []):
            ok, out = diskpart(f"select disk {ctx['disk']}\nselect partition {number}\nremove letter={letter}\n")
            if not ok:
                log.warning("Couldn't remove letter %s: %s", letter, out[-200:])


class _hive:
    """reg load / reg unload around a block (unload retried: Windows can hold the hive a moment longer)."""

    def __init__(self, key, path):
        self.key, self.path = key, path

    def __enter__(self):
        r = _run([os.path.join(SYSTEM32, "reg.exe"), "load", f"HKLM\\{self.key}", self.path])
        if r.returncode != 0:
            raise SysCloneError(f"Couldn't open {self.path}: {(r.stderr or r.stdout).strip()[-200:]}")
        return self

    def __exit__(self, *exc):
        import gc
        for _ in range(10):
            gc.collect()
            r = _run([os.path.join(SYSTEM32, "reg.exe"), "unload", f"HKLM\\{self.key}"])
            if r.returncode == 0:
                return
            time.sleep(1)
        log.warning("reg unload %s failed: %s", self.key, r.stderr)
