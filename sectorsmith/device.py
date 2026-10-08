"""Raw block-device access for Windows physical drives, Linux block devices and image files.

Everything above this module talks to a :class:`Device`, which offers byte-addressed
``read``/``write`` and takes care of sector alignment, bad-sector errors, and (on
Windows) locking + dismounting the volumes that live on a disk before writing to it.
"""
from __future__ import annotations

import os
import re
import struct
import sys
import threading
from dataclasses import dataclass, field

from .util import get_logger, is_windows

log = get_logger()

MAX_IO = 8 * 1024 * 1024  # largest single read/write we hand to the OS

BUS_TYPES = {
    0: "Unknown", 1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "1394", 5: "SSA", 6: "Fibre",
    7: "USB", 8: "RAID", 9: "iSCSI", 10: "SAS", 11: "SATA", 12: "SD", 13: "MMC",
    14: "Virtual", 15: "File Virtual", 16: "Storage Spaces", 17: "NVMe",
}


class DeviceError(Exception):
    pass


# ---------------------------------------------------------------------------
# Windows plumbing (ctypes)
# ---------------------------------------------------------------------------
if is_windows():
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 1
    FILE_SHARE_WRITE = 2
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x000700A0
    IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
    IOCTL_DISK_UPDATE_PROPERTIES = 0x00070140
    IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
    IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
    IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x002D1080
    FSCTL_LOCK_VOLUME = 0x00090018
    FSCTL_UNLOCK_VOLUME = 0x0009001C
    FSCTL_DISMOUNT_VOLUME = 0x00090020

    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                     ctypes.c_void_p]
    _k32.DeviceIoControl.restype = wintypes.BOOL
    _k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _k32.ReadFile.restype = wintypes.BOOL
    _k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _k32.WriteFile.restype = wintypes.BOOL
    _k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
                                      ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
    _k32.SetFilePointerEx.restype = wintypes.BOOL
    _k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    _k32.FindFirstVolumeW.argtypes = [wintypes.LPWSTR, wintypes.DWORD]
    _k32.FindFirstVolumeW.restype = wintypes.HANDLE
    _k32.FindNextVolumeW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD]
    _k32.FindVolumeClose.argtypes = [wintypes.HANDLE]
    _k32.GetVolumePathNamesForVolumeNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                                                      ctypes.POINTER(wintypes.DWORD)]
    _k32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    _k32.GetVolumeNameForVolumeMountPointW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]

    def _win_err(msg: str) -> OSError:
        code = ctypes.get_last_error()
        return OSError(code, f"{msg}: {ctypes.FormatError(code).strip()} (winerror {code})")

    def _open_handle(path: str, access: int) -> int:
        h = _k32.CreateFileW(path, access, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
        if h in (None, INVALID_HANDLE_VALUE):
            raise _win_err(f"Cannot open {path}")
        return h

    def _ioctl(h, code: int, inbuf: bytes | None = None, outsize: int = 4096) -> bytes:
        out = ctypes.create_string_buffer(outsize)
        ret = wintypes.DWORD(0)
        inb = ctypes.create_string_buffer(inbuf, len(inbuf)) if inbuf else None
        ok = _k32.DeviceIoControl(h, code, inb, len(inbuf) if inbuf else 0, out, outsize, ctypes.byref(ret), None)
        if not ok:
            raise _win_err(f"DeviceIoControl 0x{code:08X}")
        return out.raw[: ret.value]

    class _WinIO:
        """Positioned I/O on a Win32 handle with a page-aligned bounce buffer."""

        def __init__(self, path: str, writable: bool):
            access = GENERIC_READ | (GENERIC_WRITE if writable else 0)
            self.h = _open_handle(path, access)
            self._raw = ctypes.create_string_buffer(MAX_IO + 4096)
            base = ctypes.addressof(self._raw)
            self._addr = (base + 4095) & ~4095

        def _seek(self, off: int):
            if not _k32.SetFilePointerEx(self.h, off, None, 0):
                raise _win_err("Seek failed")

        def pread(self, off: int, n: int) -> bytes:
            self._seek(off)
            got = wintypes.DWORD(0)
            if not _k32.ReadFile(self.h, self._addr, n, ctypes.byref(got), None):
                raise _win_err(f"Read error at byte {off}")
            return ctypes.string_at(self._addr, got.value)

        def pwrite(self, off: int, data: bytes) -> int:
            self._seek(off)
            ctypes.memmove(self._addr, data, len(data))
            done = wintypes.DWORD(0)
            if not _k32.WriteFile(self.h, self._addr, len(data), ctypes.byref(done), None):
                raise _win_err(f"Write error at byte {off}")
            return done.value

        def flush(self):
            _k32.FlushFileBuffers(self.h)

        def close(self):
            if self.h:
                _k32.CloseHandle(self.h)
                self.h = None

    def _volume_guids() -> list[str]:
        buf = ctypes.create_unicode_buffer(1024)
        h = _k32.FindFirstVolumeW(buf, 1024)
        if h in (None, INVALID_HANDLE_VALUE):
            return []
        vols = [buf.value]
        while _k32.FindNextVolumeW(h, buf, 1024):
            vols.append(buf.value)
        _k32.FindVolumeClose(h)
        return vols

    def _volume_letters(guid: str) -> list[str]:
        buf = ctypes.create_unicode_buffer(4096)
        n = wintypes.DWORD(0)
        if not _k32.GetVolumePathNamesForVolumeNameW(guid, buf, 4096, ctypes.byref(n)):
            return []
        return [p for p in ctypes.wstring_at(buf, n.value).split("\0") if p]

    def _volume_extents(h) -> list[tuple[int, int, int]]:
        raw = _ioctl(h, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, outsize=8 + 24 * 32)
        count = struct.unpack_from("<I", raw, 0)[0]
        res = []
        for i in range(count):
            disk, start, length = struct.unpack_from("<I4xqq", raw, 8 + 24 * i)
            res.append((disk, start, length))
        return res

    def windows_volume_map() -> list[dict]:
        """All mounted volumes with their disk extents and drive letters/mount points."""
        result = []
        for guid in _volume_guids():
            path = guid.rstrip("\\")
            try:
                h = _open_handle(path, 0)
            except OSError:
                continue
            try:
                exts = _volume_extents(h)
            except OSError:
                exts = []
            finally:
                _k32.CloseHandle(h)
            for disk, start, length in exts:
                result.append({"guid": guid, "disk": disk, "offset": start, "length": length,
                               "letters": _volume_letters(guid)})
        return result

    def windows_system_disk() -> int | None:
        root = os.environ.get("SystemRoot", r"C:\Windows")
        buf = ctypes.create_unicode_buffer(260)
        if not _k32.GetVolumePathNameW(root, buf, 260):
            return None
        mp = buf.value
        gbuf = ctypes.create_unicode_buffer(260)
        if not _k32.GetVolumeNameForVolumeMountPointW(mp, gbuf, 260):
            return None
        try:
            h = _open_handle(gbuf.value.rstrip("\\"), 0)
            try:
                exts = _volume_extents(h)
            finally:
                _k32.CloseHandle(h)
            return exts[0][0] if exts else None
        except OSError:
            return None

    class VolumeLocker:
        """Locks and dismounts every volume on a physical disk so raw writes are allowed."""

        def __init__(self, disk_number: int, force: bool = True):
            self.disk_number = disk_number
            self.force = force
            self.handles: list[int] = []
            self.locked: list[str] = []

        def __enter__(self):
            self.lock()
            return self

        def __exit__(self, *exc):
            self.release()

        def lock(self):
            for vol in windows_volume_map():
                if vol["disk"] != self.disk_number:
                    continue
                path = vol["guid"].rstrip("\\")
                if any(v == path for v in self.locked):
                    continue
                h = _open_handle(path, GENERIC_READ | GENERIC_WRITE)
                ret = wintypes.DWORD(0)
                locked = _k32.DeviceIoControl(h, FSCTL_LOCK_VOLUME, None, 0, None, 0, ctypes.byref(ret), None)
                if not locked and not self.force:
                    _k32.CloseHandle(h)
                    raise DeviceError(f"Volume {vol['letters'] or path} is in use and could not be locked. "
                                      "Close programs using it and try again.")
                _k32.DeviceIoControl(h, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0, ctypes.byref(ret), None)
                if not locked:  # after a forced dismount the lock normally succeeds
                    _k32.DeviceIoControl(h, FSCTL_LOCK_VOLUME, None, 0, None, 0, ctypes.byref(ret), None)
                self.handles.append(h)
                self.locked.append(path)
                log.info("Locked+dismounted volume %s %s", path, vol["letters"])

        def release(self):
            ret = wintypes.DWORD(0)
            for h in self.handles:
                _k32.DeviceIoControl(h, FSCTL_UNLOCK_VOLUME, None, 0, None, 0, ctypes.byref(ret), None)
                _k32.CloseHandle(h)
            self.handles.clear()
            self.locked.clear()


class _PosixIO:
    def __init__(self, path: str, writable: bool):
        flags = os.O_RDWR if writable else os.O_RDONLY
        flags |= getattr(os, "O_BINARY", 0)
        self.fd = os.open(path, flags)

    def pread(self, off: int, n: int) -> bytes:
        return os.pread(self.fd, n, off)

    def pwrite(self, off: int, data: bytes) -> int:
        return os.pwrite(self.fd, data, off)

    def flush(self):
        try:
            os.fsync(self.fd)
        except OSError:
            pass

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class _PortableFileIO:
    """Image files on Windows (no os.pread there)."""

    def __init__(self, path: str, writable: bool):
        self.f = open(path, "r+b" if writable else "rb", buffering=0)

    def pread(self, off, n):
        self.f.seek(off)
        return self.f.read(n)

    def pwrite(self, off, data):
        self.f.seek(off)
        return self.f.write(data)

    def flush(self):
        self.f.flush()
        try:
            os.fsync(self.f.fileno())
        except OSError:
            pass

    def close(self):
        self.f.close()


# ---------------------------------------------------------------------------
# Device
# ---------------------------------------------------------------------------
@dataclass
class Device:
    path: str
    name: str
    size: int
    sector_size: int = 512
    model: str = ""
    bus: str = ""
    serial: str = ""
    is_image: bool = False
    is_system: bool = False
    removable: bool = False
    disk_number: int | None = None
    data_size: int | None = None  # for VHD images: size excluding footer
    _io: object = field(default=None, repr=False)
    _writable: bool = field(default=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _locker: object = field(default=None, repr=False)

    # --- lifecycle ------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._io is not None

    @property
    def writable(self) -> bool:
        return self._writable

    def open(self, writable: bool = False):
        if self._io is not None and (self._writable or not writable):
            return self
        self.close()
        if writable and self.is_system:
            raise DeviceError("Refusing to open the system disk for writing.")
        if writable and is_windows() and not self.is_image and self.disk_number is not None:
            self._locker = VolumeLocker(self.disk_number)
            self._locker.lock()
        if self.is_image:
            self._io = _PortableFileIO(self.path, writable) if is_windows() else _PosixIO(self.path, writable)
        elif is_windows():
            self._io = _WinIO(self.path, writable)
        else:
            self._io = _PosixIO(self.path, writable)
        self._writable = writable
        log.info("Opened %s (%s)", self.path, "rw" if writable else "ro")
        return self

    def close(self):
        if self._io is not None:
            try:
                self._io.flush() if self._writable else None
            finally:
                self._io.close()
                self._io = None
        if self._locker is not None:
            self._locker.release()
            self._locker = None
        if self._writable and is_windows() and not self.is_image:
            refresh_disk_properties(self)
        self._writable = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- geometry --------------------------------------------------------
    @property
    def usable_size(self) -> int:
        return self.data_size if self.data_size is not None else self.size

    @property
    def total_sectors(self) -> int:
        return self.usable_size // self.sector_size

    # --- I/O ------------------------------------------------------------
    def _raw_read(self, off: int, n: int) -> bytes:
        if self._io is None:
            self.open(False)
        with self._lock:
            return self._io.pread(off, n)

    def read(self, offset: int, length: int) -> bytes:
        """Read ``length`` bytes at ``offset`` (any alignment). Short at end of device."""
        end = min(offset + length, self.usable_size)
        if offset >= end:
            return b""
        ss = self.sector_size
        a_start = offset - offset % ss
        a_end = -(-end // ss) * ss  # image files may have a short tail; the OS returns a short read
        out = bytearray()
        pos = a_start
        while pos < a_end:
            n = min(MAX_IO, a_end - pos)
            chunk = self._raw_read(pos, n)
            if not chunk:
                break
            out += chunk
            pos += len(chunk)
            if len(chunk) < n:
                break
        rel = offset - a_start
        return bytes(out[rel: rel + (end - offset)])

    def read_sectors(self, lba: int, count: int = 1) -> bytes:
        return self.read(lba * self.sector_size, count * self.sector_size)

    def write(self, offset: int, data: bytes):
        """Write at ``offset``; unaligned edges are handled read-modify-write."""
        if not self._writable:
            raise DeviceError("Device is not open for writing.")
        if offset + len(data) > self.usable_size:
            raise DeviceError("Write would go past the end of the device.")
        ss = self.sector_size
        if offset % ss or len(data) % ss:
            a_start = offset - offset % ss
            a_end = -(-(offset + len(data)) // ss) * ss
            buf = bytearray(self.read(a_start, a_end - a_start))
            buf[offset - a_start: offset - a_start + len(data)] = data
            offset, data = a_start, bytes(buf)
        mv = memoryview(data)
        pos = 0
        while pos < len(data):
            n = min(MAX_IO, len(data) - pos)
            with self._lock:
                w = self._io.pwrite(offset + pos, bytes(mv[pos: pos + n]))
            if not w:
                raise DeviceError(f"Write returned 0 bytes at {offset + pos}")
            pos += w

    def write_sectors(self, lba: int, data: bytes):
        self.write(lba * self.sector_size, data)

    def flush(self):
        if self._io is not None:
            with self._lock:
                self._io.flush()

    # --- display ---------------------------------------------------------
    def describe(self) -> str:
        from .util import human_size
        tag = " [SYSTEM]" if self.is_system else ""
        return f"{self.name} — {self.model or 'Unknown'} ({human_size(self.size)}){tag}"


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------
def _win_disk_info(n: int) -> Device | None:
    path = rf"\\.\PhysicalDrive{n}"
    try:
        h = _open_handle(path, 0)
    except OSError:
        return None
    try:
        geo = _ioctl(h, IOCTL_DISK_GET_DRIVE_GEOMETRY_EX, outsize=256)
        bps = struct.unpack_from("<I", geo, 20)[0] or 512
        size = struct.unpack_from("<q", geo, 24)[0]
        model = serial = ""
        bus = "Unknown"
        removable = False
        try:
            q = struct.pack("<II4x", 0, 0)
            d = _ioctl(h, IOCTL_STORAGE_QUERY_PROPERTY, q, outsize=1024)
            removable = bool(d[10])
            v_off, p_off, _r_off, s_off, bus_type = struct.unpack_from("<IIIII", d, 12)

            def _s(off):
                if not off or off >= len(d):
                    return ""
                end = d.find(b"\0", off)
                return d[off:end if end >= 0 else None].decode("ascii", "replace").strip()

            model = " ".join(x for x in (_s(v_off), _s(p_off)) if x)
            serial = _s(s_off)
            bus = BUS_TYPES.get(bus_type, str(bus_type))
        except OSError:
            pass
        return Device(path=path, name=f"Disk {n}", size=size, sector_size=bps, model=model, bus=bus,
                      serial=serial, removable=removable, disk_number=n)
    except OSError:
        return None
    finally:
        _k32.CloseHandle(h)


def _linux_disks() -> list[Device]:
    devs = []
    root_dev = None
    try:
        for line in open("/proc/mounts"):
            parts = line.split()
            if len(parts) > 1 and parts[1] == "/":
                root_dev = os.path.basename(os.path.realpath(parts[0]))
    except OSError:
        pass
    for name in sorted(os.listdir("/sys/block")) if os.path.isdir("/sys/block") else []:
        if re.match(r"(loop|ram|zram|dm-|sr|fd)", name):
            continue
        base = f"/sys/block/{name}"
        try:
            sectors = int(open(f"{base}/size").read())
        except (OSError, ValueError):
            continue
        if sectors == 0:
            continue
        try:
            lbs = int(open(f"{base}/queue/logical_block_size").read())
        except (OSError, ValueError):
            lbs = 512
        model = ""
        for f in ("device/model", "device/name"):
            try:
                model = open(f"{base}/{f}").read().strip()
                break
            except OSError:
                pass
        removable = False
        try:
            removable = open(f"{base}/removable").read().strip() == "1"
        except OSError:
            pass
        is_sys = bool(root_dev and root_dev.startswith(name))
        bus = "NVMe" if name.startswith("nvme") else "MMC" if name.startswith("mmc") else (
            "Virtual" if name.startswith("vd") else "SCSI/SATA")
        devs.append(Device(path=f"/dev/{name}", name=name, size=sectors * 512, sector_size=lbs,
                           model=model, bus=bus, removable=removable, is_system=is_sys))
    return devs


def list_disks() -> list[Device]:
    if is_windows():
        sysdisk = windows_system_disk()
        disks = []
        misses = 0
        for n in range(64):
            d = _win_disk_info(n)
            if d is None:
                misses += 1
                if misses > 8:
                    break
                continue
            misses = 0
            d.is_system = (n == sysdisk)
            disks.append(d)
        return disks
    if sys.platform.startswith("linux"):
        return _linux_disks()
    return []


def open_image(path: str, sector_size: int = 512) -> Device:
    """Wrap a raw .img/.dd/.bin or fixed .vhd file as a Device."""
    size = os.path.getsize(path)
    data_size = None
    with open(path, "rb") as f:
        if size >= 512:
            f.seek(size - 512)
            footer = f.read(512)
            if footer[:8] == b"conectix" and struct.unpack_from(">I", footer, 60)[0] == 2:  # fixed VHD
                data_size = size - 512
    return Device(path=path, name=os.path.basename(path), size=data_size or size, sector_size=sector_size,
                  model="Image file" + (" (VHD)" if data_size else ""), bus="File", is_image=True,
                  data_size=data_size)


def create_image_file(path: str, size: int) -> Device:
    with open(path, "wb") as f:
        f.truncate(size)
    return open_image(path)


def refresh_disk_properties(dev: Device):
    """Ask Windows to re-read the partition table after we changed it."""
    if not is_windows() or dev.is_image:
        return
    try:
        h = _open_handle(dev.path, GENERIC_READ | GENERIC_WRITE)
        try:
            _ioctl(h, IOCTL_DISK_UPDATE_PROPERTIES, outsize=0)
        finally:
            _k32.CloseHandle(h)
    except OSError:
        pass


def volume_letters_for(dev: Device) -> dict[int, list[str]]:
    """Map partition byte offset -> mount points (Windows only)."""
    if not is_windows() or dev.disk_number is None:
        return {}
    out: dict[int, list[str]] = {}
    for v in windows_volume_map():
        if v["disk"] == dev.disk_number:
            out.setdefault(v["offset"], []).extend(v["letters"])
    return out


def mounted_partitions_linux(dev: Device) -> list[str]:
    if is_windows() or dev.is_image:
        return []
    base = os.path.basename(dev.path)
    res = []
    try:
        for line in open("/proc/mounts"):
            src = line.split()[0]
            if os.path.basename(os.path.realpath(src)).startswith(base):
                res.append(line.split()[1])
    except OSError:
        pass
    return res
