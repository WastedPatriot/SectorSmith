"""User folders on a Windows disk that isn't running: an old disk or NVMe in a USB caddy (mounted with a drive
letter), the internal disk of a PC booted from the SectorSmith USB, or an NTFS partition without a drive letter
(read raw, read-only).

A raw NTFS location is written as a path so every endpoint op (and Link) can carry it:
    ntfs:<start_lba>@<disk path>::Users/alice
"""
from __future__ import annotations

import calendar
import os
import string
import sys

from .util import Cancelled, check_cancel, get_logger

log = get_logger()

RAW_PREFIX = "ntfs:"
# folders in C:\Users that aren't people
NOT_USERS = {"public", "default", "default user", "all users", "defaultapppool", "wdagutilityaccount",
             "defaultuser0", "administrator.default"}
USER_MARKERS = ("ntuser.dat", "desktop", "documents", "appdata")


def is_raw(path: str) -> bool:
    return str(path).startswith(RAW_PREFIX)


def raw_root(disk: str, start_lba: int, inner: str = "") -> str:
    return f"{RAW_PREFIX}{int(start_lba)}@{disk}::{inner.replace(chr(92), '/').strip('/')}"


def parse_raw(path: str) -> tuple[str, int, str]:
    """(disk path, start LBA, path inside the volume with / separators)"""
    body = path[len(RAW_PREFIX):]
    lba, _, rest = body.partition("@")
    disk, _, inner = rest.rpartition("::")
    if not disk or not lba.isdigit():
        raise ValueError(f"Not a raw NTFS path: {path}")
    return disk, int(lba), inner.replace("\\", "/").strip("/")


def describe(path: str) -> str:
    """Short human name for a location (raw paths are long and ugly)."""
    if not is_raw(path):
        return path
    disk, lba, inner = parse_raw(path)
    return f"{inner.replace('/', chr(92))} on {os.path.basename(disk.rstrip(chr(92) + '/')) or disk} " \
           f"(partition at sector {lba:,}, no drive letter)"


# ---------------------------------------------------------------------------
def _win_volumes() -> list[dict]:
    import ctypes
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    sysdrive = os.environ.get("SystemDrive", "C:").upper().rstrip("\\")
    out = []
    old = k32.SetErrorMode(1)  # no "insert a disk" pop-ups for empty card readers
    try:
        for i, letter in enumerate(string.ascii_uppercase):
            if not mask & (1 << i):
                continue
            root = f"{letter}:\\"
            kind = k32.GetDriveTypeW(root)
            if kind not in (2, 3):  # removable, fixed
                continue
            name = ctypes.create_unicode_buffer(261)
            fs = ctypes.create_unicode_buffer(261)
            if not k32.GetVolumeInformationW(root, name, 261, None, None, None, fs, 261):
                continue
            free, total = ctypes.c_ulonglong(), ctypes.c_ulonglong()
            k32.GetDiskFreeSpaceExW(root, ctypes.byref(free), ctypes.byref(total), None)
            out.append({"path": root, "label": name.value, "fs": fs.value, "free": free.value, "total": total.value,
                        "removable": kind == 2, "system": f"{letter}:" == sysdrive})
    finally:
        k32.SetErrorMode(old)
    return out


def _posix_volumes() -> list[dict]:
    import shutil
    env = os.environ.get("SECTORSMITH_VOLUMES")
    roots = [p for p in env.split(os.pathsep) if p] if env is not None else []
    if env is None:
        try:
            with open("/proc/mounts", encoding="utf-8") as f:
                for line in f:
                    mnt = line.split()[1].replace("\\040", " ")
                    if mnt.startswith(("/media/", "/mnt/", "/run/media/")):
                        roots.append(mnt)
        except OSError:
            pass  # no /proc/mounts (not Linux): nothing extra to scan
    out = []
    for r in roots:
        try:
            u = shutil.disk_usage(r)
        except OSError:
            continue
        if not u.free and env is None:  # read-only system mounts
            continue
        out.append({"path": r, "label": os.path.basename(r.rstrip("/")), "fs": "", "free": u.free, "total": u.total,
                    "removable": True, "system": False})
    return out


def volumes() -> list[dict]:
    """Mounted drives: [{path, label, fs, free, total, removable, system}]."""
    try:
        return _win_volumes() if sys.platform == "win32" else _posix_volumes()
    except Exception as e:  # noqa: BLE001
        log.warning("Listing volumes failed: %s", e)
        return []


def _looks_like_user(path: str) -> bool:
    try:
        names = {n.lower() for n in os.listdir(path)}
    except OSError:
        return False
    return any(m in names for m in USER_MARKERS)


def profiles_on(volume: str) -> list[dict]:
    users = os.path.join(volume, "Users")
    try:
        names = sorted(os.listdir(users), key=str.lower)
    except OSError:
        return []
    out = []
    for n in names:
        p = os.path.join(users, n)
        if n.lower() in NOT_USERS or not os.path.isdir(p) or os.path.islink(p) or not _looks_like_user(p):
            continue
        out.append({"name": n, "path": p, "sid": "", "where": volume})
    return out


def offline_profiles(exclude: set[str] | None = None) -> list[dict]:
    """User folders on mounted drives other than the running Windows (old disks in a USB caddy, or the internal
    disk when booted from the SectorSmith USB)."""
    ex = {os.path.normcase(os.path.normpath(p)) for p in (exclude or ())}
    out = []
    for v in volumes():
        if v["system"]:
            continue
        check_cancel()
        for pr in profiles_on(v["path"]):
            if os.path.normcase(os.path.normpath(pr["path"])) not in ex:
                pr["where"] = f"{v['path']} {v['label']}".strip()
                out.append(pr)
    return out


# ---------------------------------------------------------------------------
class _ScanProgress:
    """Just enough of Progress for NTFSVolume.scan, wired to the caller's Cancel."""

    def reset(self, *a, **k):
        pass

    def update(self, *a):
        pass

    def check(self):
        check_cancel()


class RawNTFS:
    """Read-only file tree of an NTFS partition read straight from the disk."""

    def __init__(self, dev, start_lba: int):
        from .ntfs import ROOT_RECORD, NTFSVolume
        self.dev = dev
        self.vol = NTFSVolume(dev, start_lba * dev.sector_size)
        self.vol.scan(_ScanProgress())
        self.root = ROOT_RECORD
        self.children: dict[int, dict[str, object]] = {}
        for e in self.vol.entries.values():
            if e.deleted or e.recno == ROOT_RECORD or (e.recno < 16):
                continue
            if not self.vol._parent_ok(e) or (e.name.startswith("$") and e.parent == ROOT_RECORD):
                continue
            self.children.setdefault(e.parent, {})[e.name.lower()] = e

    def find(self, inner: str):
        """Entry at a /-separated path (case-insensitive), or None. '' is the root folder."""
        cur = self.vol.entries.get(self.root)
        for part in [p for p in inner.replace("\\", "/").split("/") if p]:
            if cur is None or not cur.is_dir:
                return None
            cur = self.children.get(cur.recno, {}).get(part.lower())
        return cur

    def list(self, entry) -> list:
        return list(self.children.get(entry.recno, {}).values())

    @staticmethod
    def mtime(e) -> int:
        return int(calendar.timegm(e.mtime.timetuple())) if e.mtime else 0

    def read(self, e, offset: int, size: int) -> bytes:
        if e.is_dir:
            raise IsADirectoryError(e.name)
        if e.flags & 0x0001:
            raise OSError("NTFS-compressed file: copy it from the running Windows instead")
        if e.flags & 0x4000 or e.attrs & 0x4000:
            raise OSError("EFS-encrypted file: only the old Windows account can open it")
        end = min(e.size, offset + size)
        if offset >= end:
            return b""
        if e.resident is not None:
            return bytes(e.resident[offset:end])
        valid = e.size if e.init_size is None else min(e.size, e.init_size)
        cl = self.vol.cluster
        out = bytearray()
        vpos = 0  # byte position of the current run inside the file
        for lcn, clusters in e.runs:
            rlen = clusters * cl
            a, b = max(offset, vpos), min(end, vpos + rlen)
            if b > a:
                keep = max(0, min(b, valid) - a)
                if lcn is None or not keep:
                    out += bytes(b - a)
                else:
                    out += self.dev.read(self.vol.base + lcn * cl + (a - vpos), keep).ljust(b - a, b"\0")
            vpos += rlen
            if vpos >= end:
                break
        if len(out) < end - offset:
            out += bytes(end - offset - len(out))
        return bytes(out)


def raw_profiles(disks: list[dict], opener) -> list[dict]:
    """User folders in NTFS partitions without a drive letter. ``opener(disk_path, start_lba)`` returns a RawNTFS."""
    out = []
    for d in disks:
        if d.get("is_system"):
            continue
        for p in d.get("partitions", []):
            if p.get("fs") != "NTFS" or p.get("mount"):
                continue
            check_cancel()
            try:
                fs = opener(d["path"], p["start"])
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001 (BitLocker, damaged, not really NTFS)
                log.info("Raw NTFS at %s:%s unreadable: %s", d["path"], p["start"], e)
                continue
            users = fs.find("Users")
            if users is None:
                continue
            for e in sorted(fs.list(users), key=lambda x: x.name.lower()):
                if not e.is_dir or e.name.lower() in NOT_USERS or e.attrs & 0x400:
                    continue
                kids = {k.name.lower() for k in fs.list(e)}
                if not any(m in kids for m in USER_MARKERS):
                    continue
                out.append({"name": e.name, "path": raw_root(d["path"], p["start"], f"Users/{e.name}"), "sid": "",
                            "where": f"{d['name']} partition {p['index']} (no drive letter, read directly)"})
    return out
